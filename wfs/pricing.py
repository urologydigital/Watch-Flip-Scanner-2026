"""LEGACY PHASE 2 PRICING MODULE — SUPERSEDED BY THE PHASE 5.1 ECONOMICS ENGINE.

    ┌─────────────────────────────────────────────────────────────────────┐
    │  DO NOT USE THIS MODULE FOR NEW PHASE 5+ DEVELOPMENT.                │
    │                                                                     │
    │  All Flip Intelligence economics — net profit, ROI, MAX BUY, the     │
    │  Quick/Base/Patient strategies, capital velocity, Flip Score and     │
    │  BUY/WATCH/PASS — MUST come from wfs.economics.EconomicsEngine.      │
    └─────────────────────────────────────────────────────────────────────┘

Why this module still exists
----------------------------
It is retained ONLY for backward compatibility with the Phase 1-4 pipeline
(`wfs/analysis.py`, exercised by `tests/test_phase2.py`). Deleting it would
break the classic scan, which remains a working feature.

Why it must not be reused
-------------------------
The fee assumptions here are the obsolete Phase 2 model: a flat ~13% selling
fee plus fixed costs, applied to every seller regardless of circumstance. That
model is wrong for this project's primary user, a UK private seller, who pays
no eBay transaction or payment processing fee. Phase 5.1 replaced it with
configurable seller profiles.

Any new import of this module from a Phase 5+ code path is a bug. There is an
architecture test (`tests/test_phase511.py`) that fails if one appears.

Original Phase 2 documentation
------------------------------
MAX BUY engine (spec s.13) and the three resale scenarios (spec s.12).

MAX BUY is derived only from conservative resale assumptions. It is
deterministic and it never rises because an auction becomes competitive.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config import (FIXED_SELLING_COSTS, MAX_RISK_BUFFER, REQUIRED_PROFIT_MARGIN,
                     SELLING_FEE_PCT)
from .liquidity import Liquidity
from .market import LOW as CONF_LOW
from .market import MEDIUM as CONF_MEDIUM
from .market import MarketValue

# (condition, buffer, description) — additive, capped at MAX_RISK_BUFFER.
RISK_FACTORS: list[tuple[str, float]] = []


@dataclass
class RiskAssessment:
    """Risk is split in two, and the split matters.

    ``buffer`` holds only PRICE-INDEPENDENT risk (documents, condition, seller,
    location, evidence quality, liquidity). MAX BUY uses this alone, which is what
    guarantees MAX BUY cannot drift upward as an auction bid climbs.

    ``scrutiny_buffer`` holds price-dependent flags — chiefly "this is far too
    cheap". These raise the risk LEVEL and can veto a BUY verdict, but they never
    feed the MAX BUY arithmetic.
    """

    buffer: float                       # structural, price-independent
    scrutiny_buffer: float = 0.0        # price-dependent, verdict only
    level: str = "LOW"                  # LOW | MEDIUM | HIGH
    factors: list[str] = field(default_factory=list)
    scrutiny_factors: list[str] = field(default_factory=list)

    @property
    def total_buffer(self) -> float:
        return round(min(self.buffer + self.scrutiny_buffer, MAX_RISK_BUFFER), 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "buffer": self.buffer,
            "scrutiny_buffer": self.scrutiny_buffer,
            "total_buffer": self.total_buffer,
            "level": self.level,
            "factors": self.factors,
            "scrutiny_factors": self.scrutiny_factors,
        }


_PAPERS_RE = re.compile(r"\b(papers?|full set|warranty card|card|box and papers|b&p)\b", re.I)
_BOX_RE = re.compile(r"\b(box|full set|boxed|complete set)\b", re.I)
_POLISH_RE = re.compile(r"\b(polished|refinish\w*|buffed)\b", re.I)
_AFTERMARKET_RE = re.compile(r"\b(aftermarket|custom|modded|mod\b|replacement dial|"
                             r"non-original|generic strap)\b", re.I)
_SERVICE_RE = re.compile(r"\b(serviced|service history|full service)\b", re.I)
_DAMAGE_RE = re.compile(r"\b(spares|repair|not working|faulty|damaged|scratched|"
                        r"missing links?|no links?)\b", re.I)


def assess_risk(listing: dict[str, Any], market: MarketValue,
                liq: Liquidity | None) -> RiskAssessment:
    """Deterministic risk buffer. A low price increases scrutiny, never lowers it."""
    title = listing.get("title") or ""
    buffer = 0.0
    scrutiny = 0.0
    factors: list[str] = []
    scrutiny_factors: list[str] = []

    def add(amount: float, why: str) -> None:
        nonlocal buffer
        buffer += amount
        factors.append(why)

    def add_scrutiny(amount: float, why: str) -> None:
        """Price-dependent. Affects the risk level and verdict, never MAX BUY."""
        nonlocal scrutiny
        scrutiny += amount
        scrutiny_factors.append(why)

    if not _PAPERS_RE.search(title):
        add(0.03, "No papers or warranty card evident from the listing")
    if not _BOX_RE.search(title):
        add(0.02, "No box evident from the listing")
    if _POLISH_RE.search(title):
        add(0.03, "Case described as polished")
    if _AFTERMARKET_RE.search(title):
        add(0.05, "Possible aftermarket or non-original parts")
    if _DAMAGE_RE.search(title):
        add(0.06, "Damage, missing links or fault indicated")
    if not _SERVICE_RE.search(title):
        add(0.02, "No service history stated")

    pct = listing.get("seller_feedback_pct")
    score = listing.get("seller_feedback_score")
    if pct is not None and pct < 98.0:
        add(0.04, f"Seller feedback {pct}% is below 98%")
    if score is not None and score < 50:
        add(0.03, f"Thin seller history ({score} feedback)")
    if pct is None and score is None:
        add(0.02, "Seller history not available")

    location = (listing.get("item_location") or "").upper()
    if location and "GB" not in location and "UNITED KINGDOM" not in location:
        add(0.03, f"Item located outside GB ({listing.get('item_location')})")

    condition = (listing.get("condition") or "").upper()
    if not condition or "PARTS" in condition:
        add(0.05, "Condition unstated or listed for parts")

    price = listing.get("total_acquisition") or 0
    if price >= 2000 and not listing.get("authenticity_guarantee"):
        add_scrutiny(0.02, "Above £2,000 without eBay Authenticity Guarantee")

    if listing.get("buying_format") == "AUCTION":
        add(0.02, "Auction — final acquisition price not yet settled")

    if market.seed_only:
        add(0.06, "Market value is a seed placeholder, not evidence")
    elif market.confidence == CONF_LOW:
        add(0.05, "Market value confidence is LOW")
    elif market.confidence == CONF_MEDIUM:
        add(0.02, "Market value confidence is MEDIUM")

    if liq is None or liq.rating == "UNKNOWN":
        add(0.05, "Liquidity unknown — no observed sales for this reference")
    elif liq.rating == "LOW":
        add(0.04, "Low observed liquidity")

    # A suspiciously low price raises scrutiny; it never improves the case.
    mid = market.market_mid
    if mid and price and price <= mid * 0.55:
        add_scrutiny(0.06,
                     "Price far below market — elevated authenticity scrutiny required")

    buffer = round(min(buffer, MAX_RISK_BUFFER), 4)
    scrutiny = round(min(scrutiny, MAX_RISK_BUFFER), 4)
    total = min(buffer + scrutiny, MAX_RISK_BUFFER)
    level = "LOW" if total <= 0.10 else ("MEDIUM" if total <= 0.20 else "HIGH")
    return RiskAssessment(buffer, scrutiny, level, factors, scrutiny_factors)


@dataclass
class Scenario:
    name: str
    price: float
    net_proceeds: float
    gross_profit: float | None
    days_low: int | None
    days_high: int | None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def net_of_selling_costs(sale_price: float) -> float:
    """LEGACY (Phase 2). Proceeds after the obsolete flat-fee model.

    Phase 5+ must use EconomicsEngine.net_proceeds() instead.
    """
    return round(sale_price * (1 - SELLING_FEE_PCT) - FIXED_SELLING_COSTS, 2)


def build_scenarios(market: MarketValue, liq: Liquidity | None,
                    acquisition: float | None) -> dict[str, Scenario]:
    """QUICK / BASE / PATIENT resale prices, proceeds and profit."""
    lo = market.market_low
    mid = market.market_mid
    hi = market.market_high
    if mid is None:
        return {}

    quick_price = round(lo if lo else mid * 0.93, 2)
    base_price = round(mid, 2)
    patient_price = round(hi if hi else mid * 1.07, 2)
    # Guard against inverted bands from thin evidence.
    quick_price = min(quick_price, base_price)
    patient_price = max(patient_price, base_price)

    days = liq.days_to_sale if liq else {"QUICK": None, "BASE": None, "PATIENT": None}
    out: dict[str, Scenario] = {}
    for name, price in (("QUICK", quick_price), ("BASE", base_price),
                        ("PATIENT", patient_price)):
        net = net_of_selling_costs(price)
        band = days.get(name)
        out[name] = Scenario(
            name=name,
            price=price,
            net_proceeds=net,
            gross_profit=round(net - acquisition, 2) if acquisition else None,
            days_low=band[0] if band else None,
            days_high=band[1] if band else None,
        )
    return out


@dataclass
class MaxBuy:
    value: float | None
    base_expected_resale: float | None
    selling_fee_pct: float
    fixed_costs: float
    required_margin: float
    risk_buffer: float
    headroom: float | None          # MAX BUY minus actual acquisition
    within_max_buy: bool
    explanation: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def max_buy(market: MarketValue, risk: RiskAssessment,
            acquisition: float | None) -> MaxBuy:
    """MAX_BUY = conservative resale, net of fees, profit requirement and risk buffer.

    The conservative resale anchor is market_low where available, never the high.
    """
    anchor = market.market_low or market.market_mid
    if anchor is None:
        return MaxBuy(None, None, SELLING_FEE_PCT, FIXED_SELLING_COSTS,
                      REQUIRED_PROFIT_MARGIN, risk.buffer, None, False,
                      "No market value available — MAX BUY cannot be calculated.")

    net = anchor * (1 - SELLING_FEE_PCT) - FIXED_SELLING_COSTS
    value = net * (1 - REQUIRED_PROFIT_MARGIN) * (1 - risk.buffer)
    value = round(max(value, 0.0), 2)

    headroom = round(value - acquisition, 2) if acquisition is not None else None
    within = acquisition is not None and acquisition <= value

    return MaxBuy(
        value=value,
        base_expected_resale=round(anchor, 2),
        selling_fee_pct=SELLING_FEE_PCT,
        fixed_costs=FIXED_SELLING_COSTS,
        required_margin=REQUIRED_PROFIT_MARGIN,
        risk_buffer=risk.buffer,
        headroom=headroom,
        within_max_buy=within,
        explanation=(
            f"Conservative resale £{anchor:,.0f}, less {SELLING_FEE_PCT:.0%} selling "
            f"fees and £{FIXED_SELLING_COSTS:,.0f} fixed costs, less "
            f"{REQUIRED_PROFIT_MARGIN:.0%} required margin, less "
            f"{risk.buffer:.0%} risk buffer."
        ),
    )
