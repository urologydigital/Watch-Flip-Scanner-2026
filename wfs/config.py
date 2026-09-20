"""Configuration loaded from environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover - optional dependency at import time
    pass

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("WFS_DB_PATH", ROOT / "watch_flip_scanner.sqlite3"))
WATCHLIST_PATH = Path(os.getenv("WFS_WATCHLIST_PATH", ROOT / "watchlist.json"))

OBSERVED_SALES_PATH = Path(os.getenv("WFS_OBSERVED_SALES_PATH", ROOT / "observed_sales.csv"))
CHRONO24_ASKING_PATH = Path(os.getenv("WFS_CHRONO24_ASKING_PATH", ROOT / "chrono24_asking.csv"))
DEALER_PRICES_PATH = Path(os.getenv("WFS_DEALER_PRICES_PATH", ROOT / "dealer_prices.csv"))

EBAY_MARKETPLACE = "EBAY_GB"
EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_SCOPE = "https://api.ebay.com/oauth/api_scope"


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


@dataclass
class Settings:
    ebay_client_id: str | None = field(default_factory=lambda: os.getenv("EBAY_CLIENT_ID"))
    ebay_client_secret: str | None = field(default_factory=lambda: os.getenv("EBAY_CLIENT_SECRET"))
    openai_api_key: str | None = field(default_factory=lambda: os.getenv("OPENAI_API_KEY"))
    anthropic_api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY"))

    max_ai_candidates_per_scan: int = field(
        default_factory=lambda: _i("MAX_AI_CANDIDATES_PER_SCAN", 10)
    )
    results_per_query: int = field(default_factory=lambda: _i("WFS_RESULTS_PER_QUERY", 50))
    # First-stage trigger: asking price at or below this fraction of market mid.
    prefilter_price_ratio: float = field(
        default_factory=lambda: _f("WFS_PREFILTER_PRICE_RATIO", 0.80)
    )
    # Extra discount demanded on lower-liquidity brands (see spec s.9).
    illiquid_brand_extra_discount: float = field(
        default_factory=lambda: _f("WFS_ILLIQUID_EXTRA_DISCOUNT", 0.07)
    )
    request_timeout: float = field(default_factory=lambda: _f("WFS_HTTP_TIMEOUT", 20.0))

    @property
    def ebay_configured(self) -> bool:
        return bool(self.ebay_client_id and self.ebay_client_secret)


SETTINGS = Settings()

# Brands with thinner UK secondary demand -> require a deeper discount (spec s.9).
ILLIQUID_BRANDS = {"RADO", "BAUME & MERCIER"}

# Minimum absolute gross spread required, by acquisition price band (spec s.9).
# (lower_bound_inclusive, upper_bound_exclusive, minimum_required_spread_gbp)
SPREAD_BANDS: list[tuple[float, float, float]] = [
    (0.0, 500.0, 120.0),
    (500.0, 1000.0, 200.0),
    (1000.0, 2000.0, 325.0),
    (2000.0, 3500.0, 400.0),
    (3500.0, float("inf"), 500.0),
]


# --- Phase 2: market value, selling costs, MAX BUY --------------------------

# Base weighting per spec s.10. Redistributed dynamically when a source is absent.
BASE_SOURCE_WEIGHTS: dict[str, float] = {
    "SOLD_EBAY_UK": 0.70,
    "ASKING_CHRONO24": 0.20,
    "DEALER_INDEX": 0.10,
}

# --- LEGACY PHASE 2 FEE CONSTANTS — DO NOT USE IN PHASE 5+ CODE -------------
# These drive wfs/pricing.py only, which serves the Phase 1-4 classic scan.
# They encode the obsolete flat-fee model (~13% of every sale) that Phase 5.1
# replaced with configurable seller profiles. A UK private seller pays no eBay
# transaction or payment processing fee, so this default is wrong for the
# project's primary user and is retained purely for backward compatibility.
#
# Phase 5+ economics live in wfs/economics.py (EconomicsEngine).
SELLING_FEE_PCT = _f("WFS_SELLING_FEE_PCT", 0.13)
FIXED_SELLING_COSTS = _f("WFS_FIXED_SELLING_COSTS", 25.0)
REQUIRED_PROFIT_MARGIN = _f("WFS_REQUIRED_PROFIT_MARGIN", 0.12)
# --- end legacy constants ---------------------------------------------------
# Ceiling on the additive risk buffer.
MAX_RISK_BUFFER = _f("WFS_MAX_RISK_BUFFER", 0.30)
# Sold-evidence lookback window.
SOLD_PERIOD_DAYS = _i("WFS_SOLD_PERIOD_DAYS", 90)

# --- Phase 3: asking-market cross-check and AI ------------------------------

# Chrono24 automated lookup is OFF by default. Enabling it is your decision;
# automated collection may breach Chrono24's terms of use.
CHRONO24_ENABLED = os.getenv("WFS_CHRONO24_ENABLED", "0").strip() in {"1", "true", "yes"}
# Seconds between Chrono24 requests. Do not lower this.
CHRONO24_MIN_INTERVAL = _f("WFS_CHRONO24_MIN_INTERVAL", 6.0)
# Run AI analysis on the shortlist.
AI_ENABLED = os.getenv("WFS_AI_ENABLED", "1").strip() in {"1", "true", "yes"}


def required_spread(acquisition_price: float) -> float:
    """Minimum gross spread (GBP) required for a listing to be shortlisted."""
    for low, high, spread in SPREAD_BANDS:
        if low <= acquisition_price < high:
            return spread
    return SPREAD_BANDS[-1][2]
