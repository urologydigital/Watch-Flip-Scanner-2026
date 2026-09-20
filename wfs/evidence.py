"""Structured market evidence and the Market Confidence Score (spec s.3, s.4).

The central rule: evidence levels are never silently mixed. A MarketEvidence
object carries exactly one level, and that level caps how confident the system
is permitted to be. Seed placeholders can never produce high confidence and can
never independently trigger a BUY.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config5 import (EVIDENCE_ASKING_ONLY, EVIDENCE_CONFIDENCE_CEILING,
                      EVIDENCE_LABEL, EVIDENCE_MODEL_FAMILY, EVIDENCE_NONE,
                      EVIDENCE_OBSERVED_ACTIVITY, EVIDENCE_ORDER,
                      EVIDENCE_REFERENCE_EXACT, EVIDENCE_REFERENCE_VARIANT,
                      EVIDENCE_SEED, PROVIDER_SOURCE_KIND, SOURCE_LABEL,
                      SOURCE_MOCK, SOURCE_SEED, SOURCE_UNKNOWN, confidence_band,
                      EVIDENCE_COLLECTED_SOLD)

INSUFFICIENT = "Insufficient Market Evidence"


@dataclass
class MarketEvidence:
    """All market observation for one reference, at exactly one evidence level."""

    reference: str
    evidence_level: str = EVIDENCE_NONE

    sold_count_30d: int = 0
    sold_count_90d: int = 0
    sold_count_180d: int = 0

    sold_prices: list[float] = field(default_factory=list)
    current_active_listing_count: int = 0
    current_asking_prices: list[float] = field(default_factory=list)

    evidence_last_updated: str | None = None
    source: str = "none"
    # Phase 5.1: where the evidence actually came from, for unambiguous labelling.
    source_kind: str = SOURCE_UNKNOWN
    notes: str = ""
    # Set when the figures came from a seed placeholder rather than observation.
    unverified: bool = False

    # -- derived price statistics ------------------------------------------
    @property
    def sold_price_sample_size(self) -> int:
        return len(self.sold_prices)

    @property
    def median_sold_price(self) -> float | None:
        return round(statistics.median(self.sold_prices), 2) if self.sold_prices else None

    @property
    def mean_sold_price(self) -> float | None:
        return round(statistics.mean(self.sold_prices), 2) if self.sold_prices else None

    @property
    def low_sold_price(self) -> float | None:
        return round(min(self.sold_prices), 2) if self.sold_prices else None

    @property
    def high_sold_price(self) -> float | None:
        return round(max(self.sold_prices), 2) if self.sold_prices else None

    @property
    def median_current_asking_price(self) -> float | None:
        if not self.current_asking_prices:
            return None
        return round(statistics.median(self.current_asking_prices), 2)

    @property
    def price_dispersion(self) -> float | None:
        """Coefficient of variation. High means an inconsistent market."""
        if len(self.sold_prices) < 3:
            return None
        mean = statistics.mean(self.sold_prices)
        if mean <= 0:
            return None
        return round(statistics.pstdev(self.sold_prices) / mean, 3)

    @property
    def sell_through_rate(self) -> float | None:
        """Observed 90-day sales against sales plus current competing listings.

        A partial-coverage figure by nature: we only see the sales we observed.
        """
        denominator = self.sold_count_90d + self.current_active_listing_count
        if denominator <= 0:
            return None
        return round(self.sold_count_90d / denominator, 3)

    @property
    def has_sold_evidence(self) -> bool:
        """True only for genuine sold records.

        Observed listing activity is deliberately excluded: a disappearance is
        not a confirmed sale, so it must never satisfy the sold-evidence gate
        that guards a BUY verdict.
        """
        return (not self.unverified
                and self.evidence_level in (EVIDENCE_REFERENCE_EXACT,
                                            EVIDENCE_REFERENCE_VARIANT,
                                            EVIDENCE_COLLECTED_SOLD,
                                            EVIDENCE_MODEL_FAMILY)
                and self.sold_price_sample_size > 0)

    @property
    def has_observed_activity(self) -> bool:
        return (self.evidence_level == EVIDENCE_OBSERVED_ACTIVITY
                and self.sold_price_sample_size > 0)

    @property
    def is_valuable(self) -> bool:
        """Whether there is enough to produce a valuation band at all."""
        return self.has_sold_evidence or self.has_observed_activity

    @property
    def source_label(self) -> str:
        """Unambiguous 'level — source' label (spec 5.1 s.15)."""
        source = SOURCE_LABEL.get(self.source_kind, SOURCE_LABEL[SOURCE_UNKNOWN])
        if self.unverified or self.evidence_level == EVIDENCE_SEED:
            return SOURCE_LABEL[SOURCE_SEED]
        if self.evidence_level == EVIDENCE_NONE:
            return INSUFFICIENT
        return f"{self.level_label} — {source}"

    @property
    def is_sufficient(self) -> bool:
        """Whether this evidence may drive a recommendation at all."""
        return self.has_sold_evidence

    @property
    def level_label(self) -> str:
        return EVIDENCE_LABEL.get(self.evidence_level, self.evidence_level)

    # -- valuation band ----------------------------------------------------
    def valuation_band(self) -> tuple[float | None, float | None, float | None]:
        """Conservative (low, mid, high) achievable UK resale.

        Built from sold evidence where available. Asking prices are used only as
        a last resort and are haircut, because asking is not achieving.
        """
        if self.is_valuable and len(self.sold_prices) >= 3:
            ordered = sorted(self.sold_prices)
            lo = ordered[max(0, int(len(ordered) * 0.25) - 1)]
            hi = ordered[min(len(ordered) - 1, int(len(ordered) * 0.75))]
            return (round(lo, 2), self.median_sold_price, round(hi, 2))
        if self.is_valuable:
            mid = self.median_sold_price
            return (round(mid * 0.94, 2), mid, round(mid * 1.06, 2)) if mid else (None, None, None)
        if self.evidence_level == EVIDENCE_ASKING_ONLY and len(self.current_asking_prices) >= 2:
            ordered = sorted(self.current_asking_prices)
            lower_half = ordered[: max(1, len(ordered) // 2)]
            # Asking prices overstate achievable resale — take the lower half and
            # haircut it further.
            mid = round(statistics.median(lower_half) * 0.93, 2)
            return (round(mid * 0.94, 2), mid, round(mid * 1.06, 2))
        return (None, None, None)

    def describe(self) -> str:
        if self.unverified:
            return f"{INSUFFICIENT} — UNVERIFIED seed value, not market observation."
        if self.has_observed_activity:
            return (f"{self.source_label}: {self.sold_count_90d} listing(s) likely "
                    f"sold from local observation. Not confirmed sales.")
        if not self.has_sold_evidence:
            if self.evidence_level == EVIDENCE_ASKING_ONLY:
                return (f"{INSUFFICIENT} for valuation. "
                        f"{self.current_active_listing_count} active asking listing(s) "
                        "observed; asking prices are not sales.")
            return INSUFFICIENT
        return (f"{self.source_label}: {self.sold_count_90d} observed UK sale(s) in "
                f"90 days ({self.sold_price_sample_size} priced), median "
                f"£{self.median_sold_price:,.0f}.")

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "evidence_level": self.evidence_level,
            "evidence_label": self.level_label,
            "source_label": self.source_label,
            "source_kind": self.source_kind,
            "unverified": self.unverified,
            "sold_count_30d": self.sold_count_30d,
            "sold_count_90d": self.sold_count_90d,
            "sold_count_180d": self.sold_count_180d,
            "sold_price_sample_size": self.sold_price_sample_size,
            "median_sold_price": self.median_sold_price,
            "mean_sold_price": self.mean_sold_price,
            "low_sold_price": self.low_sold_price,
            "high_sold_price": self.high_sold_price,
            "current_active_listing_count": self.current_active_listing_count,
            "median_current_asking_price": self.median_current_asking_price,
            "sell_through_rate": self.sell_through_rate,
            "price_dispersion": self.price_dispersion,
            "evidence_last_updated": self.evidence_last_updated,
            "source": self.source,
        }


# --- Market Confidence Score (spec s.4) -------------------------------------

@dataclass
class ConfidenceScore:
    score: float                 # 0-100
    band: str
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def is_sufficient_for_buy(self) -> bool:
        return self.band in ("EXCELLENT", "STRONG", "MODERATE")

    def as_dict(self) -> dict[str, Any]:
        return {"score": self.score, "band": self.band,
                "components": self.components, "reasons": self.reasons}


def _recency_points(evidence: MarketEvidence) -> tuple[float, str]:
    """Recent sales are worth more than old ones."""
    if evidence.sold_count_30d >= 3:
        return 20.0, "Multiple sales observed within the last 30 days."
    if evidence.sold_count_30d >= 1:
        return 14.0, "At least one sale observed within the last 30 days."
    if evidence.sold_count_90d >= 1:
        return 8.0, "Sales observed within 90 days but none in the last 30."
    if evidence.sold_count_180d >= 1:
        return 3.0, "Only older sales observed (90-180 days)."
    return 0.0, "No dated sales observed."


def _volume_points(evidence: MarketEvidence) -> tuple[float, str]:
    n = evidence.sold_count_90d
    if n >= 12:
        return 30.0, f"{n} sales observed in 90 days — a well-traded reference."
    if n >= 8:
        return 25.0, f"{n} sales observed in 90 days."
    if n >= 5:
        return 19.0, f"{n} sales observed in 90 days."
    if n >= 3:
        return 12.0, f"Only {n} sales observed in 90 days."
    if n >= 1:
        return 6.0, f"Only {n} sale(s) observed in 90 days — thin evidence."
    return 0.0, "No sales observed in the last 90 days."


def _consistency_points(evidence: MarketEvidence) -> tuple[float, str]:
    dispersion = evidence.price_dispersion
    if dispersion is None:
        return 4.0, "Too few priced sales to judge price consistency."
    if dispersion <= 0.06:
        return 20.0, "Observed sale prices are tightly clustered."
    if dispersion <= 0.12:
        return 15.0, "Observed sale prices are reasonably consistent."
    if dispersion <= 0.20:
        return 8.0, "Observed sale prices vary noticeably."
    return 2.0, "Observed sale prices are highly inconsistent."


def _sample_points(evidence: MarketEvidence) -> tuple[float, str]:
    n = evidence.sold_price_sample_size
    if n >= 10:
        return 15.0, f"{n} priced observations available."
    if n >= 6:
        return 12.0, f"{n} priced observations available."
    if n >= 3:
        return 8.0, f"{n} priced observations available."
    if n >= 1:
        return 3.0, f"Only {n} priced observation(s)."
    return 0.0, "No priced observations."


def _corroboration_points(evidence: MarketEvidence) -> tuple[float, str]:
    """Active listings corroborate that a market exists, weakly."""
    if evidence.current_active_listing_count >= 3:
        return 10.0, "Multiple current UK listings corroborate an active market."
    if evidence.current_active_listing_count >= 1:
        return 6.0, "Some current UK listings observed."
    return 2.0, "No current competing UK listings observed."


def score_confidence(evidence: MarketEvidence) -> ConfidenceScore:
    """Market Confidence Score, 0-100, capped by evidence level."""
    ceiling = EVIDENCE_CONFIDENCE_CEILING.get(evidence.evidence_level, 0)

    if evidence.unverified or evidence.evidence_level in (EVIDENCE_SEED, EVIDENCE_NONE):
        reason = ("Seed placeholder value, not market observation."
                  if evidence.unverified or evidence.evidence_level == EVIDENCE_SEED
                  else "No market evidence available.")
        score = min(10.0, ceiling)
        return ConfidenceScore(score, confidence_band(score), {}, [reason])

    components: dict[str, float] = {}
    reasons: list[str] = []
    for name, fn in (("volume", _volume_points), ("recency", _recency_points),
                     ("consistency", _consistency_points), ("sample", _sample_points),
                     ("corroboration", _corroboration_points)):
        points, reason = fn(evidence)
        components[name] = points
        reasons.append(reason)

    raw = sum(components.values())

    # Weaker evidence levels are scaled down as well as capped, so that
    # model-family data never masquerades as exact-reference data.
    scale = {
        EVIDENCE_REFERENCE_EXACT: 1.0,
        EVIDENCE_REFERENCE_VARIANT: 0.88,
        EVIDENCE_COLLECTED_SOLD: 0.88,
        EVIDENCE_MODEL_FAMILY: 0.70,
        EVIDENCE_OBSERVED_ACTIVITY: 0.55,
        EVIDENCE_ASKING_ONLY: 0.45,
    }.get(evidence.evidence_level, 0.3)

    score = round(min(raw * scale, ceiling), 1)
    if evidence.evidence_level != EVIDENCE_REFERENCE_EXACT:
        reasons.append(f"Evidence level is {evidence.level_label} — confidence capped "
                       f"at {ceiling}.")
    return ConfidenceScore(score, confidence_band(score), components, reasons)


# --- building evidence from the existing providers --------------------------

def _source_kind_for(provider_name: str) -> str:
    return PROVIDER_SOURCE_KIND.get(provider_name, SOURCE_UNKNOWN)


def from_sold_evidence(sold, reference: str, active_listing_count: int = 0,
                       asking_prices: list[float] | None = None,
                       seed_mid: float | None = None) -> MarketEvidence:
    """Adapt a Phase 2 SoldEvidence object into the Phase 5 evidence model.

    Falls back down the hierarchy honestly: sold -> asking -> seed -> none.
    """
    asking_prices = asking_prices or []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    source_name = getattr(sold, "source", "unknown")
    kind = _source_kind_for(source_name)

    if getattr(sold, "has_evidence", False) and sold.exact_sale_count > 0:
        period = getattr(sold, "period_days", 90) or 90
        exact = sold.exact_sale_count
        # Scanner-derived evidence is inference from disappearance, not a sale
        # record, so it gets its own weaker tier.
        level = (EVIDENCE_OBSERVED_ACTIVITY if source_name == "local_observation"
                 else EVIDENCE_REFERENCE_EXACT)
        # Scale the observed window onto the standard reporting periods without
        # inventing sales: these are proportional estimates of what we observed.
        return MarketEvidence(
            reference=reference,
            evidence_level=level,
            sold_count_30d=int(exact * min(30 / period, 1.0)),
            sold_count_90d=exact if period <= 90 else int(exact * 90 / period),
            sold_count_180d=exact,
            sold_prices=list(sold.prices),
            current_active_listing_count=active_listing_count,
            current_asking_prices=asking_prices,
            evidence_last_updated=now,
            source=source_name,
            source_kind=kind,
            notes=getattr(sold, "notes", ""),
        )

    if len(asking_prices) >= 2:
        return MarketEvidence(
            reference=reference,
            evidence_level=EVIDENCE_ASKING_ONLY,
            current_active_listing_count=active_listing_count or len(asking_prices),
            current_asking_prices=asking_prices,
            evidence_last_updated=now,
            source="active_listings",
            source_kind=_source_kind_for("active_listings"),
            notes="Asking prices only. No sold evidence available.",
        )

    if seed_mid:
        return MarketEvidence(
            reference=reference,
            evidence_level=EVIDENCE_SEED,
            current_active_listing_count=active_listing_count,
            evidence_last_updated=now,
            source="seed",
            source_kind=SOURCE_SEED,
            unverified=True,
            notes="UNVERIFIED seed placeholder. Cannot support a BUY.",
        )

    return MarketEvidence(reference=reference, evidence_level=EVIDENCE_NONE,
                          current_active_listing_count=active_listing_count,
                          evidence_last_updated=now, notes=INSUFFICIENT)


def strongest_level(*levels: str) -> str:
    """Return the strongest of several evidence levels."""
    present = [l for l in levels if l in EVIDENCE_ORDER]
    if not present:
        return EVIDENCE_NONE
    return min(present, key=EVIDENCE_ORDER.index)
