"""Phase 5 scan pipeline (spec s.19, s.20).

Key efficiency change over Phase 4: work is grouped by reference rather than by
listing. Sold evidence and asking data are fetched once per reference and reused
across every listing for that reference, and eBay search results are cached.
"""
from __future__ import annotations

from typing import Any, Callable

from . import ai as ai_module
from . import benchmarks as benchmark_store
from . import cache, db, evidence_bridge, evidence_store
from .evidence_collection import (CollectionSettings, CollectorRegistry,
                                  load_collection_settings, source_health_report)
from .active_market import ActiveBenchmark, build_benchmark
from .cross_check import CROSS_CHECK, CROSS_CHECK_CONFIG, CrossCheckConfig, shortlist
from .asking_market import AskingMarketProvider, default_chrono24_provider
from .config import SETTINGS
from .config5 import CONFIG, Phase5Config
from .ebay import EbayClient, EbayError, normalise_item
from .flip_analysis import FlipAnalysis, analyse_flip, needs_ai, rank
from .sold_market import SoldMarketProvider, build_provider
from .watchlist import WatchRef, build_queries, match_reference

ProgressFn = Callable[[str, float], None]


class ScanStats:
    """API and cost accounting for one scan (spec s.21 reporting)."""

    def __init__(self) -> None:
        self.queries_planned = 0
        self.queries_pruned = 0
        self.api_calls = 0
        self.cache_hits = 0
        self.listings_scanned = 0
        self.matched = 0
        self.analysed = 0
        self.ai_calls = 0
        self.ai_skipped = 0
        # Phase 5.2: how much manual work the scan is handing back.
        self.benchmarks_built = 0
        # Phase 5.3 evidence collection accounting.
        self.evidence_references = 0
        self.evidence_records = 0
        self.evidence_cache_hits = 0
        self.evidence_calls = 0
        self.cross_check_candidates = 0
        self.cross_check_suppressed = 0
        self.errors: list[str] = []

    def as_dict(self) -> dict[str, Any]:
        saved = self.cache_hits + self.queries_pruned
        return {
            "queries_planned": self.queries_planned,
            "queries_pruned": self.queries_pruned,
            "api_calls": self.api_calls,
            "cache_hits": self.cache_hits,
            "api_calls_avoided": saved,
            "listings_scanned": self.listings_scanned,
            "matched": self.matched,
            "analysed": self.analysed,
            "ai_calls": self.ai_calls,
            "ai_skipped": self.ai_skipped,
            "benchmarks_built": self.benchmarks_built,
            "evidence_references": self.evidence_references,
            "evidence_records": self.evidence_records,
            "evidence_cache_hits": self.evidence_cache_hits,
            "evidence_calls": self.evidence_calls,
            "cross_check_candidates": self.cross_check_candidates,
            "cross_check_suppressed": self.cross_check_suppressed,
            "errors": self.errors,
        }

    def summary_line(self) -> str:
        return (f"{self.listings_scanned} listings scanned → {self.matched} matched → "
                f"{self.analysed} fully analysed → "
                f"{self.evidence_references} reference(s) evidence-collected → "
                f"{self.cross_check_candidates} flagged for cross-check → "
                f"{self.ai_calls} AI call(s). "
                f"{self.api_calls} eBay API call(s), "
                f"{self.cache_hits + self.queries_pruned} avoided.")


def run_flip_scan(conn, refs: list[WatchRef], client: EbayClient | None = None,
                  sold_provider: SoldMarketProvider | None = None,
                  chrono24_provider: AskingMarketProvider | None = None,
                  ai_client: Any | None = None, use_ai: bool | None = None,
                  progress: ProgressFn | None = None,
                  config: Phase5Config = CONFIG,
                  results_per_query: int | None = None,
                  mode: str = "FLIP",
                  cross_check_config: CrossCheckConfig = CROSS_CHECK_CONFIG,
                  collection_settings: CollectionSettings | None = None,
                  registry: CollectorRegistry | None = None,
                  collect_evidence: bool = True,
                  force_refresh_evidence: bool = False) -> dict[str, Any]:
    """Run a full Phase 5 flip-intelligence scan."""
    cache.init_cache(conn)
    client = client or EbayClient()
    # Phase 5.1: full provider chain — manual records, then optional Marketplace
    # Insights, then legacy CSV, then the scanner's own observation history.
    provider = sold_provider or build_provider(conn)
    chrono = (chrono24_provider if chrono24_provider is not None
              else default_chrono24_provider())
    limit = results_per_query or SETTINGS.results_per_query

    active_refs = [r for r in refs if r.active]
    stats = ScanStats()
    run_id = db.start_scan(conn, mode)

    def report(msg: str, frac: float) -> None:
        if progress:
            progress(msg, min(max(frac, 0.0), 1.0))

    # --- discovery ---------------------------------------------------------
    seen: dict[str, dict[str, Any]] = {}
    total = max(len(active_refs), 1)

    for idx, ref in enumerate(active_refs):
        report(f"Searching {ref.brand} {ref.reference}…", idx / total * 0.55)
        queries = build_queries(ref)
        stats.queries_planned += len(queries)
        pruned = cache.prune_queries(conn, ref.reference, queries)
        stats.queries_pruned += len(queries) - len(pruned)

        for query in pruned:
            def fetch(q=query):
                return client.search(q, limit=limit)

            try:
                items, was_cached = cache.cached_call(
                    conn, cache.NS_LISTINGS, (query, limit), fetch, config=config)
            except EbayError as exc:
                stats.errors.append(f"{query}: {exc}")
                continue

            if was_cached:
                stats.cache_hits += 1
            else:
                stats.api_calls += 1

            matches = 0
            for item in items:
                listing = normalise_item(item)
                if not listing.get("item_id"):
                    continue
                if match_reference(listing.get("title") or "", [ref]):
                    matches += 1
                seen.setdefault(listing["item_id"], listing)
            cache.record_query(conn, ref.reference, query, matches)

    stats.listings_scanned = len(seen)

    # --- match and persist -------------------------------------------------
    report("Matching listings to the watchlist…", 0.60)
    by_reference: dict[str, list[dict[str, Any]]] = {}
    ref_lookup = {r.reference: r for r in active_refs}
    price_drops: dict[str, Any] = {}

    for listing in seen.values():
        matched = match_reference(listing.get("title") or "", active_refs)
        if matched is None:
            continue
        listing.update({"reference": matched.reference, "brand": matched.brand,
                        "model": matched.model})
        change = db.upsert_listing(conn, listing, scan_run_id=run_id)
        listing["is_new"] = change["is_new"]
        drop = db.latest_price_drop(conn, listing["item_id"])
        if drop:
            price_drops[listing["item_id"]] = drop
        by_reference.setdefault(matched.reference, []).append(listing)

    stats.matched = sum(len(v) for v in by_reference.values())

    searched = [r.reference for r in active_refs]
    db.record_scan_references(conn, run_id, searched)
    delisted = db.mark_absent_listings(conn, searched, seen.keys())

    # --- Phase 5.3: automatic evidence collection, once per reference ------
    # Runs on the reference shortlist, not per listing, so a scan of 189
    # listings performs a handful of collections rather than 189.
    evidence_store.migrate(conn)
    collection = collection_settings or load_collection_settings()
    collector_registry = registry
    evidence_run = None

    if collect_evidence and collection.master_enabled:
        if collector_registry is None:
            collector_registry = CollectorRegistry(
                conn, collection, listings_by_reference=by_reference)
        elif hasattr(collector_registry, "set_listings"):
            collector_registry.set_listings(by_reference)
        # Shortlist references by how much live supply we found — the ones with
        # real activity are the ones worth spending collection effort on.
        ordered_refs = sorted(by_reference.items(),
                              key=lambda kv: -len(kv[1]))
        targets = [(ref, ref_lookup[ref].brand, ref_lookup[ref].model)
                   for ref, _ in ordered_refs if ref in ref_lookup]

        report("Collecting market evidence…", 0.64)
        try:
            evidence_run = collector_registry.collect_for_references(
                targets, force_refresh=force_refresh_evidence)
            stats.evidence_references = len(evidence_run.references)
            stats.evidence_records = evidence_run.records_collected
            stats.evidence_cache_hits = evidence_run.cache_hits
            stats.evidence_calls = evidence_run.collection_calls
        except Exception as exc:   # noqa: BLE001
            # Spec 5.3 s.19: one failed source must never crash the scan, and
            # neither must the collection subsystem as a whole. The scan falls
            # back to whatever evidence is already stored.
            evidence_run = None
            stats.errors.append(f"evidence collection failed: "
                                f"{type(exc).__name__}: {exc}")

    # --- evidence and analysis, grouped by reference -----------------------
    report("Checking market evidence…", 0.68)
    analyses: list[FlipAnalysis] = []
    refs_done = 0

    for reference, listings in by_reference.items():
        ref = ref_lookup[reference]
        refs_done += 1
        report(f"Analysing {ref.brand} {reference}…",
               0.68 + refs_done / max(len(by_reference), 1) * 0.22)

        # Fetched once per reference, not once per listing.
        sold_evidence = provider.get_recent_sales(reference)

        # Phase 5.3: prefer collected evidence when it is genuinely stronger.
        collected_evidence = None
        aggregate = None
        comparable_audit = None
        if collect_evidence and collection.master_enabled:
            try:
                audit_box: list = []
                collected_evidence, aggregate = evidence_bridge.build_market_evidence(
                    conn, reference,
                    active_listing_count=len(listings),
                    seed_mid=ref.market_mid,
                    lookback_days=collection.lookback_days,
                    brand=ref.brand, model=ref.model,
                    audit_out=audit_box)
                comparable_audit = audit_box[0] if audit_box else None
            except Exception as exc:   # collection must never break a scan
                stats.errors.append(f"evidence bridge {reference}: {exc}")
                collected_evidence = None
                comparable_audit = None

        asking_prices: list[float] = []
        if chrono is not None:
            asking = chrono.get_asking_prices(ref.brand, ref.model, reference)
            if asking.has_evidence:
                asking_prices = asking.prices

        # Phase 5.2: robust active-market benchmark from the live listings we
        # already fetched. Costs no extra API calls.
        benchmark = build_benchmark(reference, listings, ref.brand)
        if benchmark.is_usable:
            stats.benchmarks_built += 1

        # A manually verified benchmark (WatchCharts etc.) if the user recorded one.
        manual = None
        try:
            manual = benchmark_store.latest_benchmark(conn, reference)
        except Exception:
            manual = None

        active_count = len(listings)
        for listing in listings:
            analyses.append(analyse_flip(
                listing, ref, sold_evidence,
                active_listing_count=active_count,
                asking_prices=asking_prices,
                config=config,
                benchmark=benchmark,
                cross_check_config=cross_check_config,
                manual_benchmark=manual,
                collected_evidence=collected_evidence,
                evidence_aggregate=aggregate,
                comparable_audit=comparable_audit,
            ))

    stats.analysed = len(analyses)

    # --- Phase 5.2: cap the manual-investigation shortlist -----------------
    # Cross-check evaluation itself is free (it uses listings already fetched),
    # but the OUTPUT is human work. An uncapped shortlist is the same as no
    # shortlist, so only the strongest candidates keep the status.
    flagged = [(a, a.cross_check) for a in analyses if a.cross_check is not None]
    keep = set(id(a) for a in shortlist(flagged, cross_check_config))
    for a, decision in flagged:
        if decision.should_cross_check and id(a) not in keep:
            decision.should_cross_check = False
            decision.blockers.append(
                "Below the top candidates for this scan — not flagged to keep the "
                "manual shortlist workable.")
            stats.cross_check_suppressed += 1
    stats.cross_check_candidates = sum(
        1 for a in analyses if a.verdict == CROSS_CHECK)

    # --- AI triage (spec s.20) --------------------------------------------
    ai_enabled = (SETTINGS.max_ai_candidates_per_scan > 0
                  if use_ai is None else use_ai)
    client_ai = ai_client if ai_client is not None else (
        ai_module.default_client() if ai_enabled else None)

    if client_ai is not None:
        eligible: list[tuple[FlipAnalysis, str]] = []
        for a in analyses:
            should, reason = needs_ai(a, config)
            if should:
                eligible.append((a, reason))
            else:
                stats.ai_skipped += 1

        eligible.sort(key=lambda pair: -pair[0].flip_score.score)
        cap = SETTINGS.max_ai_candidates_per_scan
        for i, (a, reason) in enumerate(eligible[:cap]):
            report(f"AI review {i + 1}/{min(cap, len(eligible))}…",
                   0.90 + i / max(min(cap, len(eligible)), 1) * 0.07)
            a.ai = ai_module.analyse_candidate(_ai_shim(a), client=client_ai)
            verdict, notes = ai_module.reconcile(a.deterministic_verdict, a.ai)
            a.ai_notes = [f"Sent for AI review: {reason}"] + notes
            # The AI may only make the outcome more cautious.
            a.decision.verdict = verdict
            stats.ai_calls += 1
        stats.ai_skipped += max(0, len(eligible) - cap)

    ranked = rank(analyses)

    report("Preparing report…", 0.98)
    db.finish_scan(conn, run_id,
                   listings_scanned=stats.listings_scanned,
                   prefilter_candidates=stats.matched,
                   deep_analysed=stats.analysed,
                   ai_calls=stats.ai_calls,
                   notes="; ".join(stats.errors[:5]) if stats.errors else None)
    report("Done", 1.0)

    return {
        "run_id": run_id,
        "analyses": ranked,
        "stats": stats,
        "seller_mode": config.engine.mode_label,
        "cross_check_count": stats.cross_check_candidates,
        "evidence_run": evidence_run.as_dict() if evidence_run else None,
        "source_health": source_health_report(conn),
        "price_drops": price_drops,
        "delisted_since_last_scan": delisted,
        "sold_provider": provider.name,
        "chrono24_provider": getattr(chrono, "name", "none"),
        "errors": stats.errors,
    }


class _AIShim:
    """Adapts a FlipAnalysis to the payload shape the Phase 3 AI module expects."""

    def __init__(self, a: FlipAnalysis):
        self.listing = a.listing
        self.ref = a.ref
        self.sold = _SoldShim(a)
        self.market = _MarketShim(a)
        self.liquidity = _LiquidityShim(a)
        self.risk = _RiskShim(a)
        self.maxbuy = _MaxBuyShim(a)
        self.scenarios = a.strategies
        self.verdict = a.decision.verdict
        self.asking_notes = [a.evidence.describe()]


class _SoldShim:
    def __init__(self, a): self._a = a
    def describe(self): return self._a.evidence.describe()


class _MarketShim:
    def __init__(self, a):
        low, mid, high = a.evidence.valuation_band()
        self.market_low, self.market_mid, self.market_high = low, mid, high
        self.confidence = a.confidence.band
        self.confidence_reason = "; ".join(a.confidence.reasons[:3])
        self.seed_only = a.evidence.unverified


class _LiquidityShim:
    def __init__(self, a):
        self.rating = a.liquidity.band
        self.basis = a.liquidity.basis
        self.competing_listings = a.liquidity.competing_listings


class _RiskShim:
    def __init__(self, a):
        self.level = a.risk.band
        self.factors = a.risk.factors
        self.scrutiny_factors = a.risk.scrutiny_factors


class _MaxBuyShim:
    def __init__(self, a):
        self.value = a.max_buy.standard


def _ai_shim(a: FlipAnalysis) -> Any:
    return _AIShim(a)
