"""Phase 2 deep analysis for shortlisted candidates.

Runs only on listings that survived the deterministic pre-filter, so the expensive
evidence lookups stay cheap (spec s.21).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import ai as ai_module
from . import db, market as market_engine
from .asking_market import AskingEvidence, AskingMarketProvider
from .config import SOLD_PERIOD_DAYS
from .liquidity import Liquidity, assess as assess_liquidity
from .market import MarketValue, SourceInput, asking_to_source
from .pricing import MaxBuy, RiskAssessment, Scenario, assess_risk, build_scenarios, max_buy
from .sold_market import SoldEvidence, SoldMarketProvider
from .watchlist import WatchRef


@dataclass
class Candidate:
    item_id: str
    listing: dict[str, Any]
    ref: WatchRef
    sold: SoldEvidence
    market: MarketValue
    liquidity: Liquidity
    risk: RiskAssessment
    maxbuy: MaxBuy
    scenarios: dict[str, Scenario] = field(default_factory=dict)
    verdict: str = "PASS"
    verdict_reasons: list[str] = field(default_factory=list)
    chrono24: AskingEvidence | None = None
    dealer: AskingEvidence | None = None
    ai: ai_module.AIResult | None = None
    deterministic_verdict: str = "PASS"
    ai_notes: list[str] = field(default_factory=list)

    @property
    def asking_notes(self) -> list[str]:
        return [e.describe() for e in (self.chrono24, self.dealer) if e is not None]

    @property
    def acquisition(self) -> float | None:
        return self.listing.get("total_acquisition")


def _competing_listings(all_listings: dict[str, dict[str, Any]], reference: str) -> int:
    """Current UK listings observed for the same reference in this scan."""
    return sum(1 for l in all_listings.values() if l.get("reference") == reference)


def _asking_prices(all_listings: dict[str, dict[str, Any]], reference: str,
                   exclude_item_id: str) -> list[float]:
    return [
        l["total_acquisition"] for l in all_listings.values()
        if l.get("reference") == reference
        and l.get("item_id") != exclude_item_id
        and l.get("total_acquisition")
    ]


def decide_verdict(cand: Candidate) -> tuple[str, list[str]]:
    """BUY / WATCH / PASS from deterministic evidence only (spec s.15, s.26)."""
    reasons: list[str] = []
    mb = cand.maxbuy
    acq = cand.acquisition

    if mb.value is None or acq is None:
        return "PASS", ["No market value or price available."]

    base = cand.scenarios.get("BASE")
    base_profit = base.gross_profit if base else None

    if not mb.within_max_buy:
        reasons.append(
            f"Acquisition £{acq:,.0f} exceeds MAX BUY £{mb.value:,.0f} "
            f"by £{abs(mb.headroom or 0):,.0f}."
        )
        return "PASS", reasons

    reasons.append(f"Acquisition £{acq:,.0f} sits £{mb.headroom:,.0f} under MAX BUY.")

    if base_profit is not None:
        reasons.append(f"Base-case net profit £{base_profit:,.0f} after fees.")

    blockers: list[str] = []
    if cand.market.seed_only:
        blockers.append("market value is a seed placeholder, not evidence")
    if cand.market.confidence == "LOW":
        blockers.append("market confidence is LOW")
    if cand.liquidity.rating == "UNKNOWN":
        blockers.append("liquidity unknown — no observed sales")
    if cand.risk.level == "HIGH":
        blockers.append("risk level is HIGH")
    if cand.listing.get("buying_format") == "AUCTION":
        blockers.append("auction price is not settled")

    if blockers:
        reasons.append("Downgraded to WATCH because " + "; ".join(blockers) + ".")
        return "WATCH", reasons

    if cand.risk.level == "MEDIUM" and cand.liquidity.rating == "LOW":
        reasons.append("Downgraded to WATCH: medium risk combined with low liquidity.")
        return "WATCH", reasons

    reasons.append("Evidence, liquidity and risk all within thresholds.")
    return "BUY", reasons


def analyse(conn, listing: dict[str, Any], ref: WatchRef,
            provider: SoldMarketProvider,
            all_listings: dict[str, dict[str, Any]],
            chrono24_provider: AskingMarketProvider | None = None,
            dealer_provider: AskingMarketProvider | None = None,
            scan_run_id: int | None = None) -> Candidate:
    """Full analysis for one shortlisted listing (Phases 2 and 3)."""
    reference = ref.reference

    sold = provider.get_recent_sales(reference, period_days=SOLD_PERIOD_DAYS)

    # Chrono24 and dealer data are ACTIVE ASKING context only. Neither can fail
    # the scan, and neither ever contributes a sales count.
    chrono24 = dealer = None
    sources: list[SourceInput] = []
    if chrono24_provider is not None:
        chrono24 = chrono24_provider.get_asking_prices(ref.brand, ref.model, reference)
        if chrono24.has_evidence:
            sources.append(asking_to_source("ASKING_CHRONO24", chrono24.prices))
    if dealer_provider is not None:
        dealer = dealer_provider.get_asking_prices(ref.brand, ref.model, reference)
        if dealer.has_evidence:
            sources.append(asking_to_source("DEALER_INDEX", dealer.prices, kind="INDEX"))

    mv = market_engine.estimate(
        sold, sources,
        seed_low=ref.market_low, seed_mid=ref.market_mid, seed_high=ref.market_high,
    )

    competing = _competing_listings(all_listings, reference)
    liq = assess_liquidity(reference, ref.brand, sold, competing)
    risk = assess_risk(listing, mv, liq)
    mb = max_buy(mv, risk, listing.get("total_acquisition"))
    scenarios = build_scenarios(mv, liq, listing.get("total_acquisition"))

    cand = Candidate(listing["item_id"], listing, ref, sold, mv, liq, risk, mb, scenarios)
    cand.chrono24, cand.dealer = chrono24, dealer
    cand.verdict, cand.verdict_reasons = decide_verdict(cand)
    cand.deterministic_verdict = cand.verdict

    _persist(conn, cand, scan_run_id)
    return cand


def apply_ai(conn, cand: Candidate, client=None, scan_run_id: int | None = None) -> Candidate:
    """Run AI analysis and reconcile. MAX BUY is never touched here."""
    before = cand.maxbuy.value
    cand.ai = ai_module.analyse_candidate(cand, client=client)
    cand.verdict, cand.ai_notes = ai_module.reconcile(cand.deterministic_verdict, cand.ai)
    assert cand.maxbuy.value == before, "AI must never alter MAX BUY"

    conn.execute(
        """INSERT INTO ai_analysis (scan_run_id, item_id, model, created_at,
            verdict, confidence, payload_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (scan_run_id, cand.item_id, cand.ai.model, db.now(),
         cand.ai.verdict, cand.ai.confidence, json.dumps(cand.ai.as_dict())),
    )
    conn.commit()
    return cand


def _persist(conn, cand: Candidate, scan_run_id: int | None) -> None:
    ts = db.now()
    conn.execute(
        """INSERT INTO sold_observations (reference, observed_at, source, period_days,
            match_scope, observed_sale_count, median_price, coverage_status, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cand.ref.reference, ts, cand.sold.source, cand.sold.period_days,
         "EXACT_REFERENCE", cand.sold.exact_sale_count if cand.sold.has_evidence else None,
         cand.sold.median_price, cand.sold.coverage_status, cand.sold.notes),
    )
    conn.execute(
        """INSERT INTO market_observations (reference, observed_at, source, kind,
            low, mid, high, sample_size, coverage_status, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cand.ref.reference, ts, ",".join(cand.market.sources) or "SEED",
         "SOLD" if not cand.market.seed_only else "SEED",
         cand.market.market_low, cand.market.market_mid, cand.market.market_high,
         cand.market.evidence_count, cand.market.confidence,
         cand.market.confidence_reason),
    )
    for evidence in (cand.chrono24, cand.dealer):
        if evidence is None or not evidence.has_evidence:
            continue
        lo, hi = evidence.asking_range
        conn.execute(
            """INSERT INTO market_observations (reference, observed_at, source, kind,
                low, mid, high, sample_size, coverage_status, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cand.ref.reference, ts, evidence.source, "ASKING", lo, None, hi,
             evidence.listings_observed, evidence.coverage_status, evidence.label),
        )
    conn.commit()


def candidate_row(cand: Candidate, price_drop: dict[str, Any] | None = None) -> dict[str, Any]:
    """One row of the ranked final report (spec s.16)."""
    l = cand.listing
    q, b, p = (cand.scenarios.get(k) for k in ("QUICK", "BASE", "PATIENT"))

    def days(s: Scenario | None) -> str:
        if not s or s.days_low is None:
            return "unknown"
        return f"{s.days_low}–{s.days_high}d"

    return {
        "Verdict": cand.verdict,
        "Brand": l.get("brand"),
        "Model": l.get("model"),
        "Reference": cand.ref.reference,
        "Listing price": l.get("price"),
        "Acquisition": cand.acquisition,
        "MAX BUY": cand.maxbuy.value,
        "Headroom": cand.maxbuy.headroom,
        "Quick resale": q.price if q else None,
        "Base resale": b.price if b else None,
        "Patient resale": p.price if p else None,
        "Quick profit": q.gross_profit if q else None,
        "Base profit": b.gross_profit if b else None,
        "Patient profit": p.gross_profit if p else None,
        "Observed UK sales": (cand.sold.exact_sale_count
                              if cand.sold.has_evidence else "none"),
        "Est. time to sale": days(b),
        "Liquidity": cand.liquidity.rating,
        "Risk": cand.risk.level,
        "Confidence": cand.market.confidence,
        "AI verdict": (cand.ai.verdict if cand.ai and cand.ai.ok else "n/a"),
        "AI confidence": (cand.ai.confidence if cand.ai and cand.ai.ok else "n/a"),
        "Chrono24 asking": (
            "£{:,.0f}–£{:,.0f}".format(*cand.chrono24.asking_range)
            if cand.chrono24 and cand.chrono24.has_evidence else "n/a"),
        "Seller": l.get("seller"),
        "Seller %": l.get("seller_feedback_pct"),
        "Auth Guarantee": l.get("authenticity_guarantee"),
        "Price drop": (f"£{price_drop['drop']:,.0f} ({price_drop['drop_pct']}%)"
                       if price_drop else ""),
        "Link": l.get("url"),
        "_item_id": cand.item_id,
    }


VERDICT_ORDER = {"BUY": 0, "WATCH": 1, "PASS": 2}
