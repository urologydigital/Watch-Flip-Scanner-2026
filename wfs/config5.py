"""Phase 5 configuration (spec s.23).

Every threshold, cost assumption and weighting used by the flip-intelligence
engine lives here. Nothing in the Phase 5 modules hard-codes a number.

All values are overridable from the environment, and the whole config is a
dataclass so tests and the UI can construct variants without touching globals.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# --- evidence levels (spec s.3) ---------------------------------------------

EVIDENCE_REFERENCE_EXACT = "REFERENCE_EXACT"
EVIDENCE_REFERENCE_VARIANT = "REFERENCE_VARIANT"
EVIDENCE_MODEL_FAMILY = "MODEL_FAMILY"
# Phase 5.3: automatically collected exact-reference sold data. Real sales, but
# parsed/aggregated by the scanner rather than verified by the user, so it sits
# below manually confirmed evidence and above model-family inference.
EVIDENCE_COLLECTED_SOLD = "COLLECTED_SOLD"
# Phase 5.1: the scanner's own observation history. Weaker than any real sold
# record because a disappearance is never a confirmed sale.
EVIDENCE_OBSERVED_ACTIVITY = "OBSERVED_ACTIVITY"
EVIDENCE_ASKING_ONLY = "ASKING_ONLY"
EVIDENCE_SEED = "SEED_PLACEHOLDER"
EVIDENCE_NONE = "NONE"

# Ordered strongest to weakest. Used for comparison, never mixed silently.
EVIDENCE_ORDER = [
    EVIDENCE_REFERENCE_EXACT,
    EVIDENCE_REFERENCE_VARIANT,
    EVIDENCE_COLLECTED_SOLD,
    EVIDENCE_MODEL_FAMILY,
    EVIDENCE_OBSERVED_ACTIVITY,
    EVIDENCE_ASKING_ONLY,
    EVIDENCE_SEED,
    EVIDENCE_NONE,
]

EVIDENCE_LABEL = {
    EVIDENCE_REFERENCE_EXACT: "Exact reference sold evidence",
    EVIDENCE_REFERENCE_VARIANT: "Reference variant sold evidence",
    EVIDENCE_COLLECTED_SOLD: "Collected exact-reference sold evidence",
    EVIDENCE_MODEL_FAMILY: "Model-family sold evidence",
    EVIDENCE_OBSERVED_ACTIVITY: "Observed listing activity",
    EVIDENCE_ASKING_ONLY: "Asking prices only",
    EVIDENCE_SEED: "UNVERIFIED seed placeholder",
    EVIDENCE_NONE: "Insufficient Market Evidence",
}

# Ceiling that each evidence level may contribute to the confidence score.
# Seed values can never produce high confidence (spec s.4, s.21).
EVIDENCE_CONFIDENCE_CEILING = {
    EVIDENCE_REFERENCE_EXACT: 100,
    EVIDENCE_REFERENCE_VARIANT: 85,
    EVIDENCE_COLLECTED_SOLD: 85,
    EVIDENCE_MODEL_FAMILY: 65,
    EVIDENCE_OBSERVED_ACTIVITY: 55,
    EVIDENCE_ASKING_ONLY: 45,
    EVIDENCE_SEED: 25,
    EVIDENCE_NONE: 0,
}

SOURCE_MANUAL = "MANUAL_SOLD"
SOURCE_INSIGHTS = "MARKETPLACE_INSIGHTS"
SOURCE_SCANNER = "SCANNER_HISTORY"
SOURCE_COLLECTED = "AUTO_COLLECTED"
SOURCE_ASKING = "ACTIVE_ASKING"
SOURCE_SEED = "SEED"
SOURCE_UNKNOWN = "UNKNOWN"
SOURCE_MOCK = "MOCK"

SOURCE_LABEL = {
    SOURCE_MANUAL: "Manual Sold Evidence",
    SOURCE_INSIGHTS: "Marketplace Insights",
    SOURCE_SCANNER: "Historical Scanner Evidence",
    SOURCE_COLLECTED: "Automatically Collected Evidence",
    SOURCE_ASKING: "Active Asking Prices",
    SOURCE_SEED: "Seed / Unverified",
    SOURCE_UNKNOWN: "Unattributed",
    SOURCE_MOCK: "MOCK DATA — not real evidence",
}

# Maps a provider's `name` onto a source kind for display.
PROVIDER_SOURCE_KIND = {
    "manual_sold_evidence": SOURCE_MANUAL,
    "manual_csv": SOURCE_MANUAL,
    "ebay_marketplace_insights": SOURCE_INSIGHTS,
    "local_observation": SOURCE_SCANNER,
    "ebay_sold_stored": SOURCE_MANUAL,
    "ebay_sold_insights": SOURCE_INSIGHTS,
    "ebay_sold_web": SOURCE_COLLECTED,
    "collected": SOURCE_COLLECTED,
    "local_history": SOURCE_SCANNER,
    "active_listings": SOURCE_ASKING,
    "seed": SOURCE_SEED,
    "MOCK_DATA": SOURCE_MOCK,
}

CONFIDENCE_BANDS = [
    (90, "EXCELLENT"),
    (75, "STRONG"),
    (60, "MODERATE"),
    (40, "WEAK"),
    (0, "INSUFFICIENT"),
]


def confidence_band(score: float) -> str:
    for threshold, label in CONFIDENCE_BANDS:
        if score >= threshold:
            return label
    return "INSUFFICIENT"


# --- cost assumptions (spec s.8, s.9, s.23) ---------------------------------

# Phase 5.1: cost modelling moved wholesale into wfs/economics.py, which is now
# the single authoritative source for every fee. CostConfig remains only as a
# thin read-only view for older call sites and reporting; it computes nothing.
@dataclass(frozen=True)
class CostConfig:
    """Deprecated view over the active SellerProfile. Do not compute with this.

    Kept so Phase 5 call sites and tests that only *read* cost assumptions keep
    working. All arithmetic belongs in EconomicsEngine.
    """

    @property
    def _profile(self):
        from .settings_store import profile_from_settings
        return profile_from_settings()

    @property
    def total_fee_pct(self) -> float:
        return self._profile.total_percentage_fees

    @property
    def fixed_costs(self) -> float:
        return self._profile.fixed_physical_costs

    @property
    def service_reserve_pct(self) -> float:
        return self._profile.service_reserve_pct

    @property
    def negotiation_pct(self) -> float:
        return self._profile.negotiation_allowance_pct


# --- MAX BUY stances (spec s.8) ---------------------------------------------

@dataclass(frozen=True)
class MaxBuyStance:
    """One of three risk stances. Each demands a different margin and buffer."""

    name: str
    required_margin: float      # profit demanded, as a fraction of net proceeds
    uncertainty_buffer: float   # extra haircut for market uncertainty
    resale_anchor: str          # 'low' | 'mid' — never 'high'


@dataclass(frozen=True)
class MaxBuyConfig:
    conservative: MaxBuyStance = field(default_factory=lambda: MaxBuyStance(
        "CONSERVATIVE",
        _f("WFS5_CONSERVATIVE_MARGIN", 0.18),
        _f("WFS5_CONSERVATIVE_BUFFER", 0.06),
        "low"))
    standard: MaxBuyStance = field(default_factory=lambda: MaxBuyStance(
        "STANDARD",
        _f("WFS5_STANDARD_MARGIN", 0.12),
        _f("WFS5_STANDARD_BUFFER", 0.03),
        "low"))
    aggressive: MaxBuyStance = field(default_factory=lambda: MaxBuyStance(
        "AGGRESSIVE",
        _f("WFS5_AGGRESSIVE_MARGIN", 0.08),
        _f("WFS5_AGGRESSIVE_BUFFER", 0.0),
        "mid"))

    # Aggressive is only permitted on genuinely liquid, well-evidenced watches.
    aggressive_min_confidence: float = field(
        default_factory=lambda: _f("WFS5_AGGRESSIVE_MIN_CONFIDENCE", 80.0))
    aggressive_min_liquidity: float = field(
        default_factory=lambda: _f("WFS5_AGGRESSIVE_MIN_LIQUIDITY", 70.0))


# --- flip score weighting (spec s.12) ---------------------------------------

@dataclass(frozen=True)
class FlipScoreWeights:
    profit_quality: float = field(default_factory=lambda: _f("WFS5_W_PROFIT", 0.25))
    liquidity: float = field(default_factory=lambda: _f("WFS5_W_LIQUIDITY", 0.20))
    capital_velocity: float = field(default_factory=lambda: _f("WFS5_W_VELOCITY", 0.15))
    market_confidence: float = field(default_factory=lambda: _f("WFS5_W_CONFIDENCE", 0.15))
    discount: float = field(default_factory=lambda: _f("WFS5_W_DISCOUNT", 0.10))
    condition: float = field(default_factory=lambda: _f("WFS5_W_CONDITION", 0.10))
    risk: float = field(default_factory=lambda: _f("WFS5_W_RISK", 0.05))

    def as_dict(self) -> dict[str, float]:
        return {
            "profit_quality": self.profit_quality,
            "liquidity": self.liquidity,
            "capital_velocity": self.capital_velocity,
            "market_confidence": self.market_confidence,
            "discount": self.discount,
            "condition": self.condition,
            "risk": self.risk,
        }

    @property
    def total(self) -> float:
        return round(sum(self.as_dict().values()), 6)


# --- decision thresholds (spec s.13) ----------------------------------------

@dataclass(frozen=True)
class DecisionConfig:
    min_net_profit_gbp: float = field(default_factory=lambda: _f("WFS5_MIN_NET_PROFIT", 150.0))
    min_net_roi_pct: float = field(default_factory=lambda: _f("WFS5_MIN_NET_ROI", 10.0))
    min_confidence: float = field(default_factory=lambda: _f("WFS5_MIN_CONFIDENCE", 60.0))
    min_liquidity: float = field(default_factory=lambda: _f("WFS5_MIN_LIQUIDITY", 40.0))
    max_holding_days: int = field(default_factory=lambda: _i("WFS5_MAX_HOLDING_DAYS", 90))
    max_risk_score: float = field(default_factory=lambda: _f("WFS5_MAX_RISK_SCORE", 45.0))
    buy_min_flip_score: float = field(default_factory=lambda: _f("WFS5_BUY_MIN_FLIP", 65.0))
    watch_min_flip_score: float = field(default_factory=lambda: _f("WFS5_WATCH_MIN_FLIP", 40.0))
    # A WATCH is worth an offer only if it is within this much of MAX BUY.
    watch_max_overprice_pct: float = field(
        default_factory=lambda: _f("WFS5_WATCH_MAX_OVERPRICE_PCT", 25.0))


# --- capital velocity (spec s.7) --------------------------------------------

@dataclass(frozen=True)
class VelocityConfig:
    # Profit per 30 days that scores 100. £400/month on one watch is excellent.
    excellent_profit_per_30d: float = field(
        default_factory=lambda: _f("WFS5_EXCELLENT_PROFIT_PER_30D", 400.0))
    # Holding period beyond which velocity is heavily penalised.
    slow_holding_days: int = field(default_factory=lambda: _i("WFS5_SLOW_HOLDING_DAYS", 120))


# --- caching (spec s.19) ----------------------------------------------------

@dataclass(frozen=True)
class CacheConfig:
    # Market evidence changes slowly; listings change fast. Refresh separately.
    market_evidence_ttl_hours: int = field(
        default_factory=lambda: _i("WFS5_MARKET_TTL_HOURS", 24))
    listing_search_ttl_minutes: int = field(
        default_factory=lambda: _i("WFS5_LISTING_TTL_MINUTES", 45))
    enabled: bool = field(default_factory=lambda:
                          os.getenv("WFS5_CACHE_ENABLED", "1").strip() in {"1", "true", "yes"})


# --- top-level --------------------------------------------------------------

@dataclass(frozen=True)
class Phase5Config:
    costs: CostConfig = field(default_factory=CostConfig)
    # The authoritative economics engine. Every fee, net profit and MAX BUY in
    # the application is produced by this object (spec 5.1 §26).
    economics: Any = None
    max_buy: MaxBuyConfig = field(default_factory=MaxBuyConfig)
    weights: FlipScoreWeights = field(default_factory=FlipScoreWeights)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    velocity: VelocityConfig = field(default_factory=VelocityConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)

    def with_overrides(self, **kwargs: Any) -> "Phase5Config":
        return replace(self, **kwargs)

    @property
    def engine(self):
        """Resolve the economics engine, falling back to saved settings."""
        if self.economics is not None:
            return self.economics
        from .settings_store import engine_from_settings
        return engine_from_settings()

    def with_profile(self, profile_name: str, **overrides: Any) -> "Phase5Config":
        """Convenience: swap the seller profile for this config."""
        from .economics import EconomicsEngine, get_profile
        return replace(self,
                       economics=EconomicsEngine(get_profile(profile_name, **overrides)))


CONFIG = Phase5Config()

# --- scheduled scan times (spec s.19) ---------------------------------------
SCAN_TIMES_UK = ["06:00", "12:00", "17:00", "21:00"]
