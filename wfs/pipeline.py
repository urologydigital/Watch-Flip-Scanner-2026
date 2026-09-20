"""Scan orchestration: search -> normalise -> match -> persist -> pre-filter."""
from __future__ import annotations

from typing import Any, Callable, Iterable

from . import db
from .analysis import (VERDICT_ORDER, Candidate, analyse, apply_ai, candidate_row)
from .ai import default_client
from .asking_market import (AskingMarketProvider, default_chrono24_provider,
                            default_dealer_provider)
from .config import AI_ENABLED, SETTINGS
from .ebay import EbayClient, EbayError, normalise_item
from .prefilter import PrefilterResult, evaluate
from .sold_market import SoldMarketProvider, default_provider
from .watchlist import WatchRef, build_queries, match_reference

ProgressFn = Callable[[str, float], None]


def run_scan(conn, refs: list[WatchRef], client: EbayClient | None = None,
             mode: str = "EBAY_ONLY", progress: ProgressFn | None = None,
             results_per_query: int | None = None,
             sold_provider: SoldMarketProvider | None = None,
             chrono24_provider: AskingMarketProvider | None = None,
             dealer_provider: AskingMarketProvider | None = None,
             ai_client: Any | None = None,
             use_ai: bool | None = None) -> dict[str, Any]:
    """Execute one manually triggered scan. Returns a summary dict."""
    client = client or EbayClient()
    limit = results_per_query or SETTINGS.results_per_query
    run_id = db.start_scan(conn, mode)

    def report(msg: str, frac: float) -> None:
        if progress:
            progress(msg, min(max(frac, 0.0), 1.0))

    seen_items: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    total_refs = max(len(refs), 1)

    for idx, ref in enumerate(refs):
        report(f"Searching {ref.brand} {ref.reference}…", idx / total_refs * 0.7)
        for query in build_queries(ref):
            try:
                items = client.search(query, limit=limit)
            except EbayError as exc:
                errors.append(f"{query}: {exc}")
                continue
            for item in items:
                listing = normalise_item(item)
                if not listing.get("item_id"):
                    continue
                seen_items.setdefault(listing["item_id"], listing)

    # Record what this scan actually covered, so that a listing's absence can be
    # interpreted later. Absence from a scan that never searched its reference
    # means nothing.
    searched_references = [r.reference for r in refs]
    db.record_scan_references(conn, run_id, searched_references)
    delisted = db.mark_absent_listings(conn, searched_references, seen_items.keys())

    report("Pre-filtering listings…", 0.75)

    scores: list[PrefilterResult] = []
    price_drops: dict[str, Any] = {}
    reappeared: list[str] = []
    for listing in seen_items.values():
        matched = match_reference(listing.get("title") or "", refs)
        if matched is None:
            continue
        listing.update({
            "reference": matched.reference,
            "brand": matched.brand,
            "model": matched.model,
        })
        change = db.upsert_listing(conn, listing, scan_run_id=run_id)
        drop = db.latest_price_drop(conn, listing["item_id"])
        if drop:
            price_drops[listing["item_id"]] = drop
        listing["is_new"] = change["is_new"]
        listing["reappeared"] = change.get("reappeared", False)
        if change.get("reappeared"):
            reappeared.append(listing["item_id"])
        scores.append(evaluate(listing, matched))

    db.save_candidate_scores(conn, run_id, [s.as_dict() for s in scores])

    passed = [s for s in scores if s.passed]

    # --- Phase 2: deep analysis of the shortlist only -----------------------
    provider = sold_provider or default_provider()
    chrono = chrono24_provider if chrono24_provider is not None else default_chrono24_provider()
    dealer = dealer_provider if dealer_provider is not None else default_dealer_provider()
    ref_by_reference = {r.reference: r for r in refs}
    candidates: list[Candidate] = []

    report("Checking market evidence…", 0.80)
    for i, score in enumerate(passed):
        listing = seen_items[score.item_id]
        ref = ref_by_reference.get(score.reference)
        if ref is None:
            continue
        report(f"Analysing {ref.brand} {ref.reference}…",
               0.80 + (i / max(len(passed), 1)) * 0.15)
        candidates.append(
            analyse(conn, listing, ref, provider, seen_items,
                    chrono24_provider=chrono, dealer_provider=dealer,
                    scan_run_id=run_id)
        )

    # --- Phase 3: AI analysis, capped and shortlist-only --------------------
    ai_calls = 0
    ai_enabled = AI_ENABLED if use_ai is None else use_ai
    client = ai_client if ai_client is not None else (default_client() if ai_enabled else None)
    if client is not None:
        cap = SETTINGS.max_ai_candidates_per_scan
        # Spend the budget on the strongest deterministic candidates first.
        ranked = sorted(candidates, key=lambda c: (
            VERDICT_ORDER.get(c.deterministic_verdict, 3),
            -((c.scenarios.get("BASE").gross_profit or 0)
              if c.scenarios.get("BASE") else 0),
        ))
        for i, cand in enumerate(ranked[:cap]):
            report(f"AI analysis {i + 1}/{min(cap, len(ranked))}…",
                   0.90 + (i / max(min(cap, len(ranked)), 1)) * 0.05)
            apply_ai(conn, cand, client=client, scan_run_id=run_id)
            ai_calls += 1

    candidates.sort(key=lambda c: (
        VERDICT_ORDER.get(c.verdict, 3),
        -((c.scenarios.get("BASE").gross_profit or 0) if c.scenarios.get("BASE") else 0),
    ))

    report("Preparing report…", 0.97)

    db.finish_scan(
        conn, run_id,
        listings_scanned=len(seen_items),
        prefilter_candidates=len(passed),
        deep_analysed=len(candidates),
        ai_calls=ai_calls,
        notes="; ".join(errors[:5]) if errors else None,
    )
    report("Done", 1.0)

    return {
        "run_id": run_id,
        "listings_scanned": len(seen_items),
        "matched": len(scores),
        "candidates": len(passed),
        "analysed": candidates,
        "ai_calls": ai_calls,
        "sold_provider": provider.name,
        "chrono24_provider": chrono.name,
        "dealer_provider": dealer.name,
        "scores": scores,
        "listings": seen_items,
        "price_drops": price_drops,
        "delisted_since_last_scan": delisted,
        "reappeared": reappeared,
        "errors": errors,
    }


def report_rows(result: dict[str, Any], include_pass: bool = False) -> list[dict[str, Any]]:
    """Ranked final report: BUY first, WATCH second, PASS last (spec s.16)."""
    rows = []
    for cand in result.get("analysed", []):
        if cand.verdict == "PASS" and not include_pass:
            continue
        rows.append(candidate_row(cand, result["price_drops"].get(cand.item_id)))
    return rows


def candidate_rows(result: dict[str, Any], include_pass: bool = False) -> list[dict[str, Any]]:
    """Flatten scan output into report rows (spec s.16, Phase 1 columns)."""
    rows: list[dict[str, Any]] = []
    for score in result["scores"]:
        if not score.passed and not include_pass:
            continue
        listing = result["listings"].get(score.item_id, {})
        drop = result["price_drops"].get(score.item_id)
        rows.append({
            "Verdict": "SHORTLIST" if score.passed else "PASS",
            "Brand": listing.get("brand"),
            "Model": listing.get("model"),
            "Reference": score.reference,
            "Price": listing.get("price"),
            "Shipping": listing.get("shipping"),
            "Acquisition": score.total_acquisition,
            "Market mid (seed)": score.market_mid,
            "Price ratio": score.price_ratio,
            "Gross spread": score.gross_spread,
            "Required spread": score.required_spread,
            "Format": listing.get("buying_format"),
            "Best offer": listing.get("best_offer"),
            "Condition": listing.get("condition"),
            "Seller": listing.get("seller"),
            "Seller %": listing.get("seller_feedback_pct"),
            "Auth Guarantee": listing.get("authenticity_guarantee"),
            "New this scan": listing.get("is_new"),
            "Price drop": f"£{drop['drop']:,.0f} ({drop['drop_pct']}%)" if drop else "",
            "Link": listing.get("url"),
            "_reasons": score.reasons,
            "_item_id": score.item_id,
        })
    rows.sort(key=lambda r: (r["Verdict"] != "SHORTLIST",
                             -(r["Gross spread"] or 0)))
    return rows
