"""Market value engine (spec s.10) and confidence scoring (spec s.20).

Produces a CONSERVATIVE achievable resale estimate, never the highest asking price.
Weights are dynamic: a source that returns nothing contributes nothing, and its
weight is redistributed. Confidence falls when evidence is thin.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from .config import BASE_SOURCE_WEIGHTS
from .sold_market import SoldEvidence

HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"


@dataclass
class SourceInput:
    """One evidence source contributing to the market estimate."""

    name: str          # SOLD_EBAY_UK | ASKING_CHRONO24 | DEALER_INDEX
    low: float | None
    mid: float | None
    high: float | None
    sample_size: int = 0
    kind: str = "ASKING"   # SOLD | ASKING | INDEX

    @property
    def usable(self) -> bool:
        return self.mid is not None and self.mid > 0 and self.sample_size > 0


@dataclass
class MarketValue:
    market_low: float | None
    market_mid: float | None
    market_high: float | None
    confidence: str
    confidence_reason: str
    evidence_count: int
    sources: list[str] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)
    seed_only: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "market_low": self.market_low,
            "market_mid": self.market_mid,
            "market_high": self.market_high,
            "confidence": self.confidence,
            "confidence_reason": self.confidence_reason,
            "evidence_count": self.evidence_count,
            "sources": self.sources,
            "weights": self.weights,
            "seed_only": self.seed_only,
        }


def sold_to_source(evidence: SoldEvidence) -> SourceInput:
    """Convert observed sold evidence into a weighted source input."""
    if not evidence.has_evidence:
        return SourceInput("SOLD_EBAY_UK", None, None, None, 0, "SOLD")
    lo, hi = evidence.low_high
    mid = evidence.median_price
    if lo is None or hi is None:
        # Too few points for a spread: use the median with a conservative band.
        lo = round(mid * 0.94, 2) if mid else None
        hi = round(mid * 1.06, 2) if mid else None
    return SourceInput("SOLD_EBAY_UK", lo, mid, hi, evidence.exact_sale_count, "SOLD")


def asking_to_source(name: str, prices: list[float], kind: str = "ASKING") -> SourceInput:
    """Convert a set of ACTIVE asking prices into a conservative source input.

    Asking prices overstate achievable resale, so the midpoint is taken from the
    lower half of the distribution, not the mean.
    """
    clean = sorted(p for p in prices if p and p > 0)
    if len(clean) < 2:
        return SourceInput(name, None, None, None, 0, kind)
    lower_half = clean[: max(1, len(clean) // 2)]
    mid = round(statistics.median(lower_half), 2)
    lo = round(clean[0], 2)
    hi = round(statistics.median(clean), 2)
    return SourceInput(name, lo, mid, hi, len(clean), kind)


def _confidence(sold: SoldEvidence, usable_sources: list[SourceInput]) -> tuple[str, str]:
    exact = sold.exact_sale_count if sold.has_evidence else 0
    if exact >= 8:
        return HIGH, f"{exact} exact-reference UK sales observed in {sold.period_days} days."
    if exact >= 4:
        return MEDIUM, f"{exact} exact-reference UK sales observed in {sold.period_days} days."
    if exact >= 1:
        return LOW, (f"Only {exact} exact-reference UK sale(s) observed in "
                     f"{sold.period_days} days — sparse evidence.")
    if usable_sources:
        return LOW, ("No sold evidence available. Estimate rests on active asking "
                     "prices only, which overstate achievable resale.")
    return LOW, "No market evidence available. Seed placeholder value only."


def estimate(sold: SoldEvidence, sources: list[SourceInput],
             seed_low: float | None = None, seed_mid: float | None = None,
             seed_high: float | None = None) -> MarketValue:
    """Blend available evidence into a conservative market estimate."""
    all_sources = [sold_to_source(sold), *sources]
    usable = [s for s in all_sources if s.usable]

    if not usable:
        conf, reason = _confidence(sold, [])
        return MarketValue(seed_low, seed_mid, seed_high, LOW,
                           reason, 0, ["SEED_PLACEHOLDER"], {}, seed_only=True)

    # Dynamic weighting: redistribute the weight of any missing source.
    raw = {s.name: BASE_SOURCE_WEIGHTS.get(s.name, 0.05) for s in usable}
    total = sum(raw.values()) or 1.0
    weights = {k: round(v / total, 3) for k, v in raw.items()}

    def blend(attr: str) -> float | None:
        num = den = 0.0
        for s in usable:
            value = getattr(s, attr)
            if value is None:
                continue
            w = weights[s.name]
            num += value * w
            den += w
        return round(num / den, 2) if den else None

    low, mid, high = blend("low"), blend("mid"), blend("high")

    # Conservatism guard: the mid must never exceed the blended high, and where
    # only asking data exists we haircut further (spec: never use the top price).
    if mid and high and mid > high:
        mid = high
    if not any(s.kind == "SOLD" for s in usable) and mid:
        mid = round(mid * 0.95, 2)
        if low:
            low = round(min(low, mid * 0.94), 2)

    conf, reason = _confidence(sold, usable)
    return MarketValue(
        market_low=low,
        market_mid=mid,
        market_high=high,
        confidence=conf,
        confidence_reason=reason,
        evidence_count=sum(s.sample_size for s in usable),
        sources=[s.name for s in usable],
        weights=weights,
        seed_only=False,
    )
