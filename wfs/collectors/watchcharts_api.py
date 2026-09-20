"""WatchCharts official API adapter — Level B market valuation (spec 6.0 §4).

Configuration is environment-based and entirely optional:

    WFS_WATCHCHARTS_API_ENABLED=1
    WFS_WATCHCHARTS_API_KEY=<your key>
    WFS_WATCHCHARTS_API_BASE=<override, only if WatchCharts changes it>

No key is hard-coded, no key is logged, and the key never appears in a URL — it
is sent as a header. With no key the collector reports NOT_CONFIGURED and the
scanner carries on exactly as before: WatchCharts is context, never a
requirement.

What it returns is a **market valuation**, not a confirmed sale. It is stored as
MARKET_CONTEXT and can never satisfy the Level A gate that a BUY depends on.
"""
from __future__ import annotations

import os
from typing import Any

from ..fx import to_gbp
from .base import (EV_MARKET_CONTEXT, HEALTH_BLOCKED, HEALTH_ERROR,
                   HEALTH_NOT_CONFIGURED, HEALTH_PARTIAL, HEALTH_UNAVAILABLE,
                   PRICE_UNKNOWN, BaseCollector, CollectedEvidence,
                   CollectionResult, SourceStatus, classify_match, now_iso)

DEFAULT_BASE = "https://api.watchcharts.com/v2"


def api_key() -> str | None:
    key = (os.getenv("WFS_WATCHCHARTS_API_KEY") or "").strip()
    return key or None


def api_enabled() -> bool:
    return (os.getenv("WFS_WATCHCHARTS_API_ENABLED", "0").strip().lower()
            in {"1", "true", "yes", "on"})


class WatchChartsAPICollector(BaseCollector):
    """Official API. Level B valuation context, GBP where supported."""

    name = "watchcharts_api"
    evidence_type = EV_MARKET_CONTEXT
    min_interval_seconds = 2.0

    def __init__(self, enabled: bool | None = None, http_client=None,
                 base_url: str | None = None):
        super().__init__(enabled=api_enabled() if enabled is None else enabled)
        self.base = (base_url or os.getenv("WFS_WATCHCHARTS_API_BASE")
                     or DEFAULT_BASE).rstrip("/")
        if http_client is None:
            try:
                import httpx
                http_client = httpx
            except Exception:
                http_client = None
        self.http_client = http_client

    # -- availability ------------------------------------------------------
    def is_available(self) -> bool:
        return bool(self.enabled and api_key() and self.http_client)

    def unavailable_reason(self) -> str:
        if not self.enabled:
            return ("WatchCharts API is switched off. Set "
                    "WFS_WATCHCHARTS_API_ENABLED=1 to use it.")
        if not api_key():
            return ("WatchCharts API key not configured. Set "
                    "WFS_WATCHCHARTS_API_KEY in your .env.")
        if not self.http_client:
            return "httpx is not available, so the API cannot be called."
        return "Ready."

    def transport_description(self) -> str:
        return "official API (key in header)"

    def preflight(self) -> dict[str, Any]:
        configured = bool(api_key())
        return {
            "source": self.name,
            "enabled": bool(self.enabled),
            "transport": self.transport_description(),
            "ready": self.is_available(),
            "key_configured": configured,      # never the key itself
            "base_url": self.base,
            "reason": self.unavailable_reason(),
        }

    # -- collection --------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        # Header, never a query parameter — a key in a URL ends up in logs.
        return {"Authorization": f"Bearer {api_key()}",
                "Accept": "application/json",
                "User-Agent": self.user_agent}

    def collect(self, reference: str, brand: str = "", model: str = "",
                **kwargs: Any) -> CollectionResult:
        """Overrides the base so a missing key reports NOT_CONFIGURED."""
        if not self.enabled:
            return CollectionResult.disabled(self.name, reference)
        if not api_key():
            return CollectionResult(
                self.name, reference, [],
                SourceStatus(self.name, HEALTH_NOT_CONFIGURED, 0, None, "MISS",
                             self.unavailable_reason(),
                             "Optional — the scanner works without it."))
        if not self.http_client:
            return CollectionResult.unavailable(
                self.name, reference, self.unavailable_reason())
        try:
            return self._collect(reference, brand, model, **kwargs)
        except Exception as exc:
            return CollectionResult.error(
                self.name, reference, f"{type(exc).__name__}: {exc}")

    def parse(self, payload: Any, reference: str, brand: str = "",
              model: str = "") -> list[CollectedEvidence]:
        """Map a WatchCharts response onto one Level B record.

        Tolerant of the exact field names, because the response shape is not
        something this project can verify without a key. If no value can be
        found the result is nothing — never a guess.
        """
        if not isinstance(payload, dict):
            return []
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

        value = None
        for key in ("market_value", "marketValue", "median_price", "price",
                    "value", "estimated_value"):
            candidate = data.get(key)
            if isinstance(candidate, (int, float)) and candidate > 0:
                value = float(candidate)
                break
        if value is None:
            return []

        currency = str(data.get("currency") or data.get("currency_code")
                       or "GBP").upper()
        gbp = to_gbp(value, currency)
        if gbp is None:
            return []          # unknown currency: skip rather than assume a rate

        api_reference = str(data.get("reference") or data.get("reference_number")
                            or reference)
        match = classify_match(api_reference, reference, model, brand)

        low = data.get("low") or data.get("min_price")
        high = data.get("high") or data.get("max_price")
        notes = ["WatchCharts API market valuation (Level B) — not a confirmed sale."]
        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
            notes.append(f"Range {low}–{high} {currency}.")
        if data.get("as_of") or data.get("updated_at"):
            notes.append(f"As of {data.get('as_of') or data.get('updated_at')}.")

        return [CollectedEvidence(
            source=self.name,
            evidence_type=EV_MARKET_CONTEXT,
            reference=reference,
            brand=brand or (data.get("brand") or None),
            model=model or (data.get("model") or None),
            price_gbp=gbp,
            original_price=round(value, 2),
            original_currency=currency,
            item_id=f"watchcharts_api:{api_reference}",
            listing_url=data.get("url") or data.get("source_url"),
            retrieved_at=now_iso(),
            price_certainty=PRICE_UNKNOWN,     # a valuation, not a transaction
            match_type=match,
            notes=" ".join(notes),
        )]

    def _collect(self, reference: str, brand: str, model: str,
                 **kwargs: Any) -> CollectionResult:
        self.throttle()
        url = f"{self.base}/watches/market-value"
        response = self.http_client.get(
            url, params={"reference": reference, "brand": brand,
                         "currency": "GBP"},
            headers=self._headers(), timeout=20.0)
        code = getattr(response, "status_code", 0)

        if code in (401, 403):
            return CollectionResult(
                self.name, reference, [],
                SourceStatus(self.name, HEALTH_BLOCKED, 0, None, "MISS",
                             f"WatchCharts API rejected the key (HTTP {code}). "
                             "Check the key and your plan's access.",
                             "Key present but not accepted."))
        if code == 429:
            return CollectionResult(
                self.name, reference, [],
                SourceStatus(self.name, HEALTH_UNAVAILABLE, 0, None, "MISS",
                             "WatchCharts API rate limit reached (HTTP 429).",
                             "Try again later."))
        if code != 200:
            return CollectionResult.unavailable(
                self.name, reference, f"WatchCharts API returned HTTP {code}.")

        try:
            payload = response.json()
        except Exception as exc:
            return CollectionResult.error(
                self.name, reference, f"Malformed API response: {exc}")

        records = self.parse(payload, reference, brand, model)
        if not records:
            return CollectionResult.unavailable(
                self.name, reference,
                "API responded but contained no usable market value for this "
                "reference.", status=HEALTH_PARTIAL)
        return CollectionResult.ok(self.name, reference, records,
                                   detail="WatchCharts API market valuation.")
