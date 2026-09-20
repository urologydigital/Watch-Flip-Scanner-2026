"""eBay active asking evidence (Level C) — zero additional API calls.

Source Health reported `eBay Active API: NOT_RUN` because nothing ever recorded
health for it: the Browse API fetch happens in the scan pipeline, and no
collector represented it.

This collector closes that gap without issuing a single extra request. The scan
has already fetched and matched the active UK listings for each reference; this
turns that same data into normalised `ACTIVE_ASKING` evidence records.

**These are asking prices and are classified as such.** Every record carries
`evidence_type=ACTIVE_ASKING` and `price_certainty=PRICE_UNKNOWN`, so the
valuation engine treats them as Level C context. An active listing can never
become sold evidence — a watch nobody has bought tells you what a seller hopes
for, not what the market pays.
"""
from __future__ import annotations

from typing import Any

from ..watchlist import looks_like_accessory
from .base import (EV_ACTIVE_ASKING, HEALTH_OK, MATCH_EXACT, MATCH_NORMALIZED,
                   PRICE_UNKNOWN, BaseCollector, CollectedEvidence,
                   CollectionResult, classify_match)


class EbayActiveCollector(BaseCollector):
    """Reuses listings the scan already fetched. Never calls the API itself."""

    name = "ebay_active_api"
    evidence_type = EV_ACTIVE_ASKING

    def __init__(self, enabled: bool = True,
                 listings_by_reference: dict[str, list[dict[str, Any]]] | None = None):
        super().__init__(enabled=enabled)
        # Supplied by the pipeline after discovery — never fetched here.
        self.listings_by_reference = listings_by_reference or {}

    def is_available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return "Disabled in settings." if not self.enabled else "Ready."

    def transport_description(self) -> str:
        return "reuses scan results (no API call)"

    def preflight(self) -> dict[str, Any]:
        total = sum(len(v) for v in self.listings_by_reference.values())
        return {
            "source": self.name,
            "enabled": bool(self.enabled),
            "transport": self.transport_description(),
            "ready": True,
            "references_available": len(self.listings_by_reference),
            "listings_available": total,
            "reason": (f"{total} already-fetched listing(s) across "
                       f"{len(self.listings_by_reference)} reference(s)."
                       if total else
                       "No listings supplied yet — runs during a scan, after "
                       "eBay discovery."),
        }

    def set_listings(self, listings_by_reference: dict[str, list[dict[str, Any]]]
                     ) -> None:
        self.listings_by_reference = listings_by_reference or {}

    def _collect(self, reference: str, brand: str, model: str,
                 **kwargs: Any) -> CollectionResult:
        listings = (kwargs.get("listings")
                    or self.listings_by_reference.get(reference) or [])
        if not listings:
            return CollectionResult.unavailable(
                self.name, reference,
                "No active listings were fetched for this reference in this scan.",
                status=HEALTH_OK)

        records: list[CollectedEvidence] = []
        skipped = 0
        for listing in listings:
            title = listing.get("title") or ""
            price = listing.get("total_acquisition") or listing.get("price")
            if price is None or price <= 0:
                skipped += 1
                continue
            # Straps, boxes and parts would distort the asking picture.
            if looks_like_accessory(title, brand or listing.get("brand") or ""):
                skipped += 1
                continue

            match = classify_match(title, reference, model, brand)
            # Only the same reference counts as an asking comparable. A
            # model-family match here means a different variant (79030B when we
            # asked for 79030N), which would distort the asking picture.
            if match not in (MATCH_EXACT, MATCH_NORMALIZED):
                skipped += 1
                continue

            records.append(CollectedEvidence(
                source=self.name,
                evidence_type=EV_ACTIVE_ASKING,       # never SOLD
                reference=reference,
                brand=brand or listing.get("brand"),
                model=model or listing.get("model"),
                title=title or None,
                price_gbp=round(float(price), 2),
                original_price=round(float(price), 2),
                original_currency=(listing.get("currency") or "GBP").upper(),
                listing_url=listing.get("url"),
                item_id=listing.get("item_id"),
                seller_location=listing.get("item_location"),
                condition=listing.get("condition"),
                price_certainty=PRICE_UNKNOWN,        # asking, not achieved
                match_type=match,
                notes=("eBay UK active listing — asking price, not a sale. "
                       "Reused from scan discovery at no extra API cost."),
            ))

        if not records:
            return CollectionResult.unavailable(
                self.name, reference,
                f"All {len(listings)} fetched listing(s) were filtered out "
                "(accessories, unusable prices, or a different reference).",
                status=HEALTH_OK)

        return CollectionResult.ok(
            self.name, reference, records,
            cache_status="REUSED",
            detail=(f"{len(records)} active asking comparable(s) reused from scan "
                    f"discovery, {skipped} filtered out. 0 extra API calls."))
