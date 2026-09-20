"""Active eBay market benchmark (spec 5.2 §7).

We already pull live UK listings for every reference. This module turns that
data into a robust benchmark so a candidate can be compared against what the
market is *currently asking*.

The critical labelling rule: this is an **ACTIVE MARKET SIGNAL, not sold
evidence**. Asking prices tell you what sellers hope for, not what buyers pay.
A 20% discount to active median is a reason to look harder, never a reason to
buy.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .watchlist import looks_like_accessory

LABEL = "eBay UK Active Asking Market"

# Minimum listings before a median means anything.
MIN_SAMPLE = 3
# Listings this far from the median are treated as a different product
# (a parts lot at one end, a full collection or mispriced listing at the other).
OUTLIER_LOW_RATIO = 0.35
OUTLIER_HIGH_RATIO = 2.5
# Robust outlier rejection via median absolute deviation.
MAD_THRESHOLD = 3.5


@dataclass
class ActiveBenchmark:
    """What the live UK market is currently asking for one reference."""

    reference: str
    active_listing_count: int = 0
    active_median_price: float | None = None
    active_mean_price: float | None = None
    active_low_price: float | None = None
    active_high_price: float | None = None
    excluded_count: int = 0
    exclusion_reasons: list[str] = field(default_factory=list)
    observed_at: str | None = None
    label: str = LABEL

    @property
    def is_usable(self) -> bool:
        return (self.active_listing_count >= MIN_SAMPLE
                and self.active_median_price is not None
                and self.active_median_price > 0)

    def discount_to_median_pct(self, price: float | None) -> float | None:
        """Positive means the candidate is cheaper than the active median."""
        if not self.is_usable or not price or price <= 0:
            return None
        median = self.active_median_price
        return round((median - price) / median * 100, 1)

    def describe(self) -> str:
        if not self.is_usable:
            return (f"{LABEL}: only {self.active_listing_count} usable listing(s) — "
                    "too few for a benchmark.")
        return (f"{LABEL}: {self.active_listing_count} listing(s), median "
                f"£{self.active_median_price:,.0f}, range "
                f"£{self.active_low_price:,.0f}–£{self.active_high_price:,.0f}. "
                "These are asking prices, not sales.")

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "label": self.label,
            "active_listing_count": self.active_listing_count,
            "active_median_price": self.active_median_price,
            "active_mean_price": self.active_mean_price,
            "active_low_price": self.active_low_price,
            "active_high_price": self.active_high_price,
            "excluded_count": self.excluded_count,
            "observed_at": self.observed_at,
            "is_sold_evidence": False,
        }


def _mad_filter(prices: list[float]) -> tuple[list[float], int]:
    """Median absolute deviation filter — robust to a few wild prices."""
    if len(prices) < 4:
        return prices, 0
    median = statistics.median(prices)
    deviations = [abs(p - median) for p in prices]
    mad = statistics.median(deviations)
    if mad <= 0:
        return prices, 0
    kept = [p for p in prices if abs(p - median) / (1.4826 * mad) <= MAD_THRESHOLD]
    if len(kept) < MIN_SAMPLE:
        return prices, 0
    return kept, len(prices) - len(kept)


def build_benchmark(reference: str, listings: list[dict[str, Any]],
                    brand: str = "") -> ActiveBenchmark:
    """Median asking price for one reference, with accessories and outliers removed."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    bench = ActiveBenchmark(reference=reference, observed_at=now)

    prices: list[float] = []
    excluded = 0
    reasons: list[str] = []

    for listing in listings:
        title = listing.get("title") or ""
        price = listing.get("total_acquisition") or listing.get("price")

        if price is None or price <= 0:
            excluded += 1
            reasons.append("no usable price")
            continue
        # Straps, boxes, parts and replicas would drag the median down hard.
        if looks_like_accessory(title, brand or listing.get("brand") or ""):
            excluded += 1
            reasons.append("accessory or parts listing")
            continue
        prices.append(float(price))

    if not prices:
        bench.excluded_count = excluded
        bench.exclusion_reasons = sorted(set(reasons))
        return bench

    # Hard ratio guard first: catches a £50 "parts" lot or a £20,000 collection
    # that slipped past the title filter.
    provisional_median = statistics.median(prices)
    ratio_kept = [p for p in prices
                  if provisional_median * OUTLIER_LOW_RATIO <= p
                  <= provisional_median * OUTLIER_HIGH_RATIO]
    if len(ratio_kept) >= MIN_SAMPLE:
        excluded += len(prices) - len(ratio_kept)
        if len(prices) != len(ratio_kept):
            reasons.append("price implausible versus the reference median")
        prices = ratio_kept

    prices, mad_excluded = _mad_filter(prices)
    if mad_excluded:
        excluded += mad_excluded
        reasons.append("statistical outlier")

    bench.active_listing_count = len(prices)
    bench.active_median_price = round(statistics.median(prices), 2)
    bench.active_mean_price = round(statistics.mean(prices), 2)
    bench.active_low_price = round(min(prices), 2)
    bench.active_high_price = round(max(prices), 2)
    bench.excluded_count = excluded
    bench.exclusion_reasons = sorted(set(reasons))
    return bench


def benchmarks_by_reference(listings_by_reference: dict[str, list[dict[str, Any]]]
                            ) -> dict[str, ActiveBenchmark]:
    out: dict[str, ActiveBenchmark] = {}
    for reference, listings in listings_by_reference.items():
        brand = listings[0].get("brand", "") if listings else ""
        out[reference] = build_benchmark(reference, listings, brand)
    return out
