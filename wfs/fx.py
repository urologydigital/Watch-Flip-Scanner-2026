"""Currency normalisation to GBP (Phase 5.3 §7).

Chrono24 lists in many currencies. Comparing a EUR asking price with a GBP one
without conversion produces nonsense, but inventing a rate is worse.

This module uses explicitly configured rates only. There is NO network call and
no live FX feed: an unknown currency returns None and the record is skipped
rather than converted at a guessed rate.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

# Configure via WFS53_FX_<CCY> (units of that currency per 1 GBP).
# These defaults are indicative only and should be reviewed; a stale rate
# quietly distorts every converted comparable.
DEFAULT_RATES_PER_GBP: dict[str, float] = {
    "GBP": 1.0,
    "EUR": 1.17,
    "USD": 1.27,
    "CHF": 1.12,
}


def _env_rate(currency: str) -> float | None:
    raw = os.getenv(f"WFS53_FX_{currency.upper()}")
    if raw is None:
        return None
    try:
        value = float(raw)
        return value if value > 0 else None
    except ValueError:
        return None


def rate_per_gbp(currency: str) -> float | None:
    """Units of `currency` per 1 GBP, or None if we have no configured rate."""
    ccy = (currency or "GBP").upper()
    return _env_rate(ccy) or DEFAULT_RATES_PER_GBP.get(ccy)


def to_gbp(amount: float | None, currency: str = "GBP") -> float | None:
    """Convert to GBP. Returns None for an unknown currency — never a guess."""
    if amount is None or amount <= 0:
        return None
    rate = rate_per_gbp(currency)
    if not rate:
        return None
    return round(amount / rate, 2)


def describe() -> dict[str, Any]:
    return {
        "rates_per_gbp": {c: rate_per_gbp(c) for c in DEFAULT_RATES_PER_GBP},
        "source": "configured constants / WFS53_FX_<CCY> environment overrides",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": ("No live FX feed. Unknown currencies are skipped rather than "
                 "converted at an assumed rate."),
    }
