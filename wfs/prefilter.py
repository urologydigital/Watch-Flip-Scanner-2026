"""Deterministic first-stage filter (spec s.9). No AI is used here."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import ILLIQUID_BRANDS, SETTINGS, required_spread
from .watchlist import WatchRef, looks_like_accessory


@dataclass
class PrefilterResult:
    item_id: str
    reference: str | None
    total_acquisition: float | None
    market_mid: float | None
    price_ratio: float | None
    gross_spread: float | None
    required_spread: float | None
    passed: bool
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["required_spread"] = self.required_spread
        return d


def effective_ratio_threshold(brand: str) -> float:
    """Illiquid brands must be cheaper still before we look at them."""
    base = SETTINGS.prefilter_price_ratio
    if brand.upper() in ILLIQUID_BRANDS:
        return round(base - SETTINGS.illiquid_brand_extra_discount, 4)
    return base


def evaluate(listing: dict[str, Any], ref: WatchRef) -> PrefilterResult:
    reasons: list[str] = []
    total = listing.get("total_acquisition")
    mid = ref.market_mid

    if looks_like_accessory(listing.get("title") or "", ref.brand):
        reasons.append("Title matches parts/accessory exclusion terms")
        return PrefilterResult(listing["item_id"], ref.reference, total, mid,
                               None, None, None, False, reasons)

    if total is None or mid is None or mid <= 0:
        reasons.append("Missing price or market estimate — cannot score")
        return PrefilterResult(listing["item_id"], ref.reference, total, mid,
                               None, None, None, False, reasons)

    ratio = round(total / mid, 4)
    spread = round(mid - total, 2)
    needed = required_spread(total)
    threshold = effective_ratio_threshold(ref.brand)

    ratio_ok = ratio <= threshold
    spread_ok = spread >= needed

    if ratio_ok:
        reasons.append(f"Acquisition {ratio:.0%} of market mid (threshold {threshold:.0%})")
    else:
        reasons.append(f"Acquisition {ratio:.0%} of market mid — above {threshold:.0%} threshold")

    if spread_ok:
        reasons.append(f"Gross spread £{spread:,.0f} meets required £{needed:,.0f}")
    else:
        reasons.append(f"Gross spread £{spread:,.0f} below required £{needed:,.0f}")

    if listing.get("buying_format") == "AUCTION":
        reasons.append("Auction — current bid, not a settled acquisition price")

    # Both conditions must hold: favour false negatives over false positives (spec s.26).
    passed = bool(ratio_ok and spread_ok)
    return PrefilterResult(listing["item_id"], ref.reference, total, mid, ratio,
                           spread, needed, passed, reasons)


# --- sold market provider (interface only in Phase 1; spec s.4) -------------

class SoldMarketProvider:
    """Interface for sold/transaction evidence.

    Phase 1 ships the null implementation only. It reports honestly that no
    permitted sold-data source is configured rather than inventing numbers.
    """

    name = "none"

    def get_recent_sales(self, reference: str, country: str = "GB") -> dict[str, Any]:
        return {
            "reference": reference,
            "country": country,
            "sales": [],
            "coverage_status": "UNAVAILABLE",
            "source": self.name,
            "note": "No permitted sold-data source configured.",
        }

    def get_observed_sale_prices(self, reference: str) -> list[float]:
        return []

    def get_sale_count(self, reference: str, period_days: int = 90) -> dict[str, Any]:
        return {
            "reference": reference,
            "period_days": period_days,
            "observed_sale_count": None,
            "coverage_status": "UNAVAILABLE",
            "source": self.name,
        }


NULL_SOLD_PROVIDER = SoldMarketProvider()
