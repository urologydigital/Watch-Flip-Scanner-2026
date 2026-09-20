"""Bridge: collected evidence → the existing valuation model (Phase 5.3 §14, §15).

Phase 5.2.1's `MarketEvidence`, `score_confidence`, liquidity, MAX BUY and
verdict logic all stay exactly as they are. This module simply feeds them better
inputs.

The evidence hierarchy (§14) is applied here, strongest first. Crucially, the
tier is decided by what the evidence IS, not by how it arrived: automatically
collected exact-reference sales outrank manually recorded model-family ones,
and asking prices never outrank either.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .collectors.base import (BEST_OFFER_UNCERTAIN, MATCH_EXACT, MATCH_FAMILY,
                              MATCH_NORMALIZED, days_since)
from .config5 import (EVIDENCE_ASKING_ONLY, EVIDENCE_COLLECTED_SOLD,
                      EVIDENCE_MODEL_FAMILY, EVIDENCE_NONE,
                      EVIDENCE_REFERENCE_EXACT, EVIDENCE_SEED, SOURCE_COLLECTED,
                      SOURCE_MANUAL, SOURCE_SEED)
from .comparables import ComparableAudit, filter_comparables
from .evidence import MarketEvidence
from .evidence_store import AggregatedEvidence, aggregate

# Sources whose sold records the user has personally verified.
# Level A: evidence the user or a licensed API actually confirmed.
# Product Research counts because the figures come from eBay's own transaction
# research, captured from the user's authenticated session — not parsed off a
# public page or inferred from a listing vanishing.
VERIFIED_SOURCES = {"ebay_sold_stored", "manual_sold_evidence",
                    "ebay_sold_insights", "ebay_product_research"}


def _choose_level(agg: AggregatedEvidence) -> tuple[str, str]:
    """(evidence_level, source_kind) per the §14 hierarchy."""
    exact = agg.exact_sales
    if exact:
        verified = any(r.source in VERIFIED_SOURCES for r in exact)
        if verified:
            return EVIDENCE_REFERENCE_EXACT, SOURCE_MANUAL
        return EVIDENCE_COLLECTED_SOLD, SOURCE_COLLECTED
    if agg.family_sales:
        return EVIDENCE_MODEL_FAMILY, SOURCE_COLLECTED
    if agg.asking_records:
        return EVIDENCE_ASKING_ONLY, SOURCE_COLLECTED
    return EVIDENCE_NONE, SOURCE_SEED


def build_market_evidence(conn: sqlite3.Connection, reference: str,
                          active_listing_count: int = 0,
                          seed_mid: float | None = None,
                          lookback_days: int = 365,
                          brand: str = "", model: str = "",
                          audit_out: list | None = None
                          ) -> tuple[MarketEvidence, AggregatedEvidence]:
    """Turn stored collected evidence into the MarketEvidence the engine expects.

    Returns the evidence plus the aggregate, so the UI can show the actual
    records a valuation rests on.
    """
    agg = aggregate(conn, reference, max_age_days=lookback_days)
    level, source_kind = _choose_level(agg)

    if level in (EVIDENCE_NONE, EVIDENCE_SEED):
        if seed_mid:
            return (MarketEvidence(
                reference=reference, evidence_level=EVIDENCE_SEED,
                current_active_listing_count=active_listing_count,
                source="seed", source_kind=SOURCE_SEED, unverified=True,
                notes=("UNVERIFIED seed placeholder. Cannot support a BUY.")),
                agg)
        return (MarketEvidence(
            reference=reference, evidence_level=EVIDENCE_NONE,
            current_active_listing_count=active_listing_count,
            notes="Insufficient Market Evidence."), agg)

    if level == EVIDENCE_ASKING_ONLY:
        prices = [r.price_gbp for r in agg.asking_records if r.price_gbp]
        return (MarketEvidence(
            reference=reference, evidence_level=EVIDENCE_ASKING_ONLY,
            current_active_listing_count=active_listing_count or len(prices),
            current_asking_prices=prices, source="collected",
            source_kind=SOURCE_COLLECTED,
            evidence_last_updated=_latest(agg),
            notes="Active asking prices only. No sold evidence."), agg)

    # --- sold evidence -----------------------------------------------------
    # Only priced sales inform the valuation; Best Offer sales are counted for
    # liquidity but contribute no price (§6).
    pool = agg.exact_sales if agg.exact_sales else agg.family_sales

    # Spec 6.0 s.8: never take a median over dirty data. Every comparable is
    # judged and the verdict recorded so the user can inspect it.
    audit = filter_comparables(pool, reference, brand, model,
                               require_material_match=bool(agg.exact_sales))
    if audit_out is not None:
        audit_out.append(audit)
    priced = [r for r in audit.included_records
              if r.usable_for_valuation and r.price_gbp]

    evidence = MarketEvidence(
        reference=reference,
        evidence_level=level,
        sold_count_30d=agg.sales_within(30, exact_only=bool(agg.exact_sales)),
        sold_count_90d=agg.sales_within(90, exact_only=bool(agg.exact_sales)),
        sold_count_180d=agg.sales_within(180, exact_only=bool(agg.exact_sales)),
        sold_prices=[r.price_gbp for r in priced],
        current_active_listing_count=active_listing_count,
        current_asking_prices=[r.price_gbp for r in agg.asking_records
                               if r.price_gbp],
        evidence_last_updated=_latest(agg),
        source=_primary_source(pool),
        source_kind=source_kind,
        notes=_notes(agg),
    )
    return evidence, agg


def _latest(agg: AggregatedEvidence) -> str | None:
    stamps = [r.retrieved_at for r in
              (agg.sold_records + agg.asking_records + agg.context_records)
              if r.retrieved_at]
    return max(stamps) if stamps else None


def _primary_source(records: list[Any]) -> str:
    if not records:
        return "collected"
    counts: dict[str, int] = {}
    for r in records:
        counts[r.source] = counts.get(r.source, 0) + 1
    return max(counts, key=counts.get)


def _notes(agg: AggregatedEvidence) -> str:
    parts = [f"{len(agg.exact_sales)} exact-reference sale(s)"]
    if agg.family_sales:
        parts.append(f"{len(agg.family_sales)} model-family sale(s), counted separately")
    if agg.best_offer_count:
        parts.append(f"{agg.best_offer_count} Best Offer sale(s) with undisclosed "
                     "price — counted for liquidity only")
    if agg.asking_records:
        parts.append(f"{len(agg.asking_records)} active asking comparable(s)")
    return "; ".join(parts) + "."


# --- liquidity inputs (§15) -------------------------------------------------

def liquidity_facts(agg: AggregatedEvidence, active_supply: int) -> dict[str, Any]:
    """Structured liquidity numbers, kept separate by match quality."""
    exact_30 = agg.sales_within(30)
    exact_90 = agg.sales_within(90)
    exact_365 = agg.sales_within(365)
    sufficient = exact_90 >= 2 or exact_365 >= 4

    sales_per_month = round(exact_90 / 3, 2) if exact_90 else 0.0
    sell_through = None
    if exact_90 or active_supply:
        denominator = exact_90 + active_supply
        sell_through = round(exact_90 / denominator, 3) if denominator else None

    return {
        "confirmed_sales_30d": exact_30,
        "confirmed_sales_90d": exact_90,
        "confirmed_sales_365d": exact_365,
        "family_sales_90d": agg.sales_within(90, exact_only=False) - exact_90,
        "active_uk_supply": active_supply,
        "sales_per_month": sales_per_month,
        "sell_through_proxy": sell_through,
        "best_offer_sales": agg.best_offer_count,
        "sufficient": sufficient,
        "status": "OK" if sufficient else "INSUFFICIENT DATA",
    }


# --- adaptive weighted valuation (spec 6.0 §9) ------------------------------

@dataclass
class BlendedValuation:
    """Explicit, auditable blend of the evidence levels.

    Target weighting when evidence is good is ~70% confirmed sold, ~20% active
    asking, ~10% independent valuation — but the weights ADAPT: a source with no
    data contributes nothing and its weight is redistributed, and a thin sold
    sample loses weight rather than pretending to certainty.

    One hard guard: when confirmed sold evidence exists, the blended mid is
    never allowed above the sold mid. Asking prices are what sellers hope for,
    and letting them lift a resale estimate is exactly how a scanner talks you
    into overpaying.
    """

    market_low: float | None = None
    market_mid: float | None = None
    market_high: float | None = None
    weights: dict[str, float] = field(default_factory=dict)
    inputs: dict[str, float | None] = field(default_factory=dict)
    capped_by_sold: bool = False
    basis: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"market_low": self.market_low, "market_mid": self.market_mid,
                "market_high": self.market_high, "weights": self.weights,
                "inputs": self.inputs, "capped_by_sold": self.capped_by_sold,
                "basis": self.basis}


def blended_valuation(agg: AggregatedEvidence,
                      sold_band: tuple[float | None, float | None, float | None],
                      asking_prices: list[float] | None = None,
                      independent_value: float | None = None
                      ) -> BlendedValuation:
    """Combine Level A / C / B with adaptive weights."""
    import statistics

    asking_prices = [p for p in (asking_prices or []) if p and p > 0]
    sold_low, sold_mid, sold_high = sold_band

    # Asking prices are haircut before they are allowed anywhere near a
    # valuation: the lower half, less 7%.
    asking_mid = None
    if len(asking_prices) >= 2:
        ordered = sorted(asking_prices)
        lower_half = ordered[: max(1, len(ordered) // 2)]
        asking_mid = round(statistics.median(lower_half) * 0.93, 2)

    raw: dict[str, float] = {}
    if sold_mid:
        # Sold weight scales with the effective sample: one sale is not seven.
        sample = agg.effective_sample or 0.0
        raw["confirmed_sold"] = 0.70 * min(1.0, max(0.25, sample / 6.0))
    if asking_mid:
        raw["active_asking"] = 0.20
    if independent_value:
        raw["independent_valuation"] = 0.10

    if not raw:
        return BlendedValuation(basis="No usable evidence for a valuation.")

    total = sum(raw.values())
    weights = {k: round(v / total, 3) for k, v in raw.items()}
    inputs = {"confirmed_sold": sold_mid, "active_asking": asking_mid,
              "independent_valuation": independent_value}

    mid = sum(inputs[k] * w for k, w in weights.items() if inputs.get(k))
    mid = round(mid, 2)

    capped = False
    if sold_mid and mid > sold_mid:
        mid = sold_mid
        capped = True

    if sold_low and sold_high:
        spread_low, spread_high = sold_low / sold_mid, sold_high / sold_mid
    else:
        spread_low, spread_high = 0.94, 1.06

    basis_parts = [f"{k.replace('_', ' ')} {w:.0%}" for k, w in weights.items()]
    basis = "Weighted: " + ", ".join(basis_parts) + "."
    if capped:
        basis += (" Capped at the confirmed-sold mid so asking prices cannot "
                  "inflate the estimate.")

    return BlendedValuation(
        market_low=round(mid * spread_low, 2),
        market_mid=mid,
        market_high=round(mid * spread_high, 2),
        weights=weights, inputs=inputs, capped_by_sold=capped, basis=basis)
