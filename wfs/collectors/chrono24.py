"""Chrono24 active asking comparables (Phase 5.3 §7).

Reuses the existing Phase 3 `Chrono24WebProvider` rather than duplicating its
robots.txt checking and throttling. This collector adds normalisation: GBP
conversion, match classification and structured records.

Chrono24 is ACTIVE ASKING DATA. It is never sold evidence, never a transaction,
and cannot on its own support a BUY.
"""
from __future__ import annotations

import os
from typing import Any

from ..fx import to_gbp
from .base import (EV_ACTIVE_ASKING, HEALTH_BLOCKED, HEALTH_PARTIAL,
                   HEALTH_UNAVAILABLE,
                   PRICE_UNKNOWN, BaseCollector, CollectedEvidence,
                   CollectionResult, classify_match)


class Chrono24Collector(BaseCollector):
    """Wraps the existing provider; adds normalisation and record shaping."""

    name = "chrono24"
    evidence_type = EV_ACTIVE_ASKING
    base_url = "https://www.chrono24.co.uk"
    min_interval_seconds = 6.0

    def __init__(self, enabled: bool | None = None, provider=None,
                 browser_session=None):
        if enabled is None:
            enabled = os.getenv("WFS_CHRONO24_ENABLED", "0").strip().lower() in {
                "1", "true", "yes", "on"}
        super().__init__(enabled=enabled)
        self._provider = provider
        self.browser_session = browser_session

    def _get_provider(self):
        if self._provider is None:
            from ..asking_market import Chrono24WebProvider
            self._provider = Chrono24WebProvider(enabled=self.enabled)
        return self._provider

    def is_available(self) -> bool:
        return bool(self.enabled)

    def transport_description(self) -> str:
        if self.browser_session is not None:
            return "browser"
        return "delegated to Chrono24WebProvider (httpx)"

    def unavailable_reason(self) -> str:
        return ("Disabled in settings." if not self.enabled
                else "Chrono24 provider could not be constructed.")

    def preflight_url(self) -> str:
        from ..market_links import chrono24_search_url
        return chrono24_search_url("Tudor", "79030N")

    def _collect(self, reference: str, brand: str, model: str,
                 **kwargs: Any) -> CollectionResult:
        provider = self._get_provider()
        evidence = provider.get_asking_prices(brand, model, reference)

        if not getattr(evidence, "has_evidence", False):
            note = getattr(evidence, "notes", "") or "No asking data available."
            lowered = note.lower()
            if "robots" in lowered or "refused" in lowered or "disallow" in lowered:
                status = HEALTH_BLOCKED
            elif any(w in lowered for w in ("unavailable", "failed", "error",
                                            "disabled", "timeout")):
                # The source did not answer. That is not partial success.
                status = HEALTH_UNAVAILABLE
            else:
                status = HEALTH_PARTIAL      # answered, but nothing usable
            return CollectionResult.unavailable(self.name, reference, note,
                                                status=status)

        currency = kwargs.get("currency", "GBP")
        records: list[CollectedEvidence] = []
        for i, price in enumerate(getattr(evidence, "prices", [])):
            gbp = to_gbp(price, currency)
            if gbp is None:
                continue  # unknown currency: skip rather than guess a rate
            records.append(CollectedEvidence(
                source=self.name, evidence_type=EV_ACTIVE_ASKING,
                reference=reference, brand=brand, model=model,
                price_gbp=gbp, original_price=round(float(price), 2),
                original_currency=currency.upper(),
                item_id=f"chrono24:{reference}:{i}",
                seller_location=(getattr(evidence, "countries", None) or [None])[0],
                price_certainty=PRICE_UNKNOWN,   # asking, never a paid price
                match_type=classify_match(reference, reference, model, brand),
                notes="Chrono24 active asking price — not a sale."))

        if not records:
            return CollectionResult.unavailable(
                self.name, reference, "No convertible asking prices.",
                status=HEALTH_PARTIAL)
        return CollectionResult.ok(self.name, reference, records,
                                   detail=f"{len(records)} active asking comparable(s).")
