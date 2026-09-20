"""Listing and authenticity risk, scored 0-100 (spec s.11). Higher = riskier.

Two properties matter here and both are enforced:

1. An extremely low price INCREASES scrutiny. It never improves the opportunity.
2. Price-driven scrutiny is tracked separately from structural risk, because
   MAX BUY must not move as an auction bid climbs. Only structural risk feeds
   the MAX BUY arithmetic — the same separation established in Phase 2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .condition import ConditionAssessment

RISK_BANDS = [(70, "HIGH"), (45, "ELEVATED"), (25, "MODERATE"), (0, "LOW")]


def risk_band(score: float) -> str:
    for threshold, label in RISK_BANDS:
        if score >= threshold:
            return label
    return "LOW"


@dataclass
class RiskScore:
    structural: float                  # 0-100, price-independent
    scrutiny: float                    # 0-100, price-dependent
    band: str = "LOW"
    factors: list[str] = field(default_factory=list)
    scrutiny_factors: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return round(min(100.0, self.structural + self.scrutiny), 1)

    @property
    def structural_buffer(self) -> float:
        """Structural risk expressed as a valuation haircut fraction (0-0.30)."""
        return round(min(self.structural / 100.0 * 0.30, 0.30), 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.total,
            "structural": self.structural,
            "scrutiny": self.scrutiny,
            "band": self.band,
            "factors": self.factors,
            "scrutiny_factors": self.scrutiny_factors,
        }


def score_risk(listing: dict[str, Any], condition: ConditionAssessment,
               market_mid: float | None, confidence_score: float,
               liquidity_score: float) -> RiskScore:
    """Combine seller, listing, condition, evidence and price signals."""
    structural = 0.0
    scrutiny = 0.0
    factors: list[str] = []
    scrutiny_factors: list[str] = []

    def add(points: float, why: str) -> None:
        nonlocal structural
        structural += points
        factors.append(why)

    def add_scrutiny(points: float, why: str) -> None:
        nonlocal scrutiny
        scrutiny += points
        scrutiny_factors.append(why)

    # --- seller -----------------------------------------------------------
    pct = listing.get("seller_feedback_pct")
    score = listing.get("seller_feedback_score")
    if pct is None and score is None:
        add(8, "Seller history not available")
    else:
        if pct is not None:
            if pct < 95.0:
                add(18, f"Seller feedback {pct}% is poor")
            elif pct < 98.0:
                add(10, f"Seller feedback {pct}% is below 98%")
            elif pct < 99.0:
                add(4, f"Seller feedback {pct}%")
        if score is not None:
            if score < 10:
                add(14, f"Very thin seller history ({score} feedback)")
            elif score < 50:
                add(8, f"Thin seller history ({score} feedback)")

    # --- listing quality --------------------------------------------------
    if not listing.get("image_url"):
        add(8, "No listing image available")
    title = listing.get("title") or ""
    if len(title) < 25:
        add(5, "Very short listing title — little information given")
    if condition.reference_unclear:
        add(12, "Reference not clearly identified in the listing")
    if condition.authenticity_doubt:
        add(15, "Listing language raises authenticity questions")

    # --- condition and completeness --------------------------------------
    if condition.aftermarket_parts:
        add(14, "Aftermarket or non-original parts indicated")
    if condition.damage:
        add(12, "Damage or fault indicated")
    if condition.completeness in ("WATCH_ONLY", "UNKNOWN"):
        add(6, "No papers or warranty card evident")
    if condition.service_needed:
        add(6, "Service appears to be needed")

    # --- protections ------------------------------------------------------
    if not listing.get("authenticity_guarantee"):
        add(6, "No eBay Authenticity Guarantee on this listing")
    returns = listing.get("returns_accepted")
    if returns is False:
        add(8, "Seller does not accept returns")
    elif returns is None:
        add(2, "Returns policy not available from search data")

    # --- location ---------------------------------------------------------
    location = (listing.get("item_location") or "").upper()
    if location and "GB" not in location and "UNITED KINGDOM" not in location:
        add(8, f"Item located outside GB ({listing.get('item_location')})")

    # --- evidence and liquidity ------------------------------------------
    if confidence_score < 40:
        add(14, "Market evidence is insufficient to value this reliably")
    elif confidence_score < 60:
        add(7, "Market evidence is weak")
    if liquidity_score < 30:
        add(10, "Very low observed liquidity for this reference")
    elif liquidity_score < 50:
        add(5, "Modest observed liquidity")

    # --- price-dependent scrutiny (never feeds MAX BUY) -------------------
    price = listing.get("total_acquisition") or listing.get("price") or 0
    if market_mid and price:
        ratio = price / market_mid
        if ratio <= 0.45:
            add_scrutiny(25, f"Price is only {ratio:.0%} of market — treat as suspect "
                             "until authenticity is confirmed")
        elif ratio <= 0.60:
            add_scrutiny(14, f"Price is {ratio:.0%} of market — unusually low, "
                             "warrants extra checks")
        elif ratio <= 0.70:
            add_scrutiny(6, f"Price is {ratio:.0%} of market — verify before committing")
    if listing.get("buying_format") == "AUCTION":
        add_scrutiny(5, "Auction — final acquisition price is not settled")

    structural = round(min(structural, 100.0), 1)
    scrutiny = round(min(scrutiny, 100.0), 1)
    total = min(structural + scrutiny, 100.0)
    return RiskScore(structural, scrutiny, risk_band(total), factors, scrutiny_factors)
