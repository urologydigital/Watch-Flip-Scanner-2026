"""eBay sold/completed evidence (Phase 5.3 §5, §6).

Priority order, strongest first:

  A. Marketplace Insights API — if legitimate access is configured
  B. Manually imported sold records (Phase 5.1 store)
  C. The scanner's own confirmed observations
  D. Optional public-web collection — only where legitimately permitted

Tier D is **disabled by default and gated on robots.txt**. eBay's terms cover
automated access to their web pages; the Browse API is the licensed route and
remains the only thing this project uses for discovery. If public collection
cannot operate legitimately, the collector reports SOURCE_UNAVAILABLE and the
scan continues.

Nothing here ever fabricates a sale.
"""
from __future__ import annotations

import os
import re
import sqlite3
from typing import Any

from ..fx import to_gbp
from .base import (BEST_OFFER_UNCERTAIN, CONFIRMED_PRICE, DISPLAYED_SOLD_PRICE,
                   EV_SOLD, HEALTH_BLOCKED, HEALTH_DISABLED, HEALTH_ERROR,
                   HEALTH_OK, HEALTH_PARTIAL, HEALTH_UNAVAILABLE, BaseCollector,
                   CollectedEvidence, CollectionResult, SourceStatus,
                   classify_match, now_iso)

# Best Offer markers. When eBay shows a struck-through price on an
# accepted-offer sale, the displayed number is what the seller asked, not what
# the buyer paid. Treating it as the sale price inflates every valuation.
BEST_OFFER_MARKERS = re.compile(
    r"best\s*offer\s*accepted|offer\s*accepted|obo\s*accepted", re.I)


class MarketplaceInsightsCollector(BaseCollector):
    """Tier A — licensed sold data, when eBay has granted access."""

    name = "ebay_sold_insights"
    evidence_type = EV_SOLD

    def __init__(self, provider=None, enabled: bool | None = None):
        # Enabled only when a provider is supplied AND it reports access.
        self.provider = provider
        available = bool(provider is not None and getattr(provider, "enabled", False))
        super().__init__(enabled=available if enabled is None else enabled)

    def is_available(self) -> bool:
        return bool(self.provider is not None
                    and getattr(self.provider, "enabled", False))

    def unavailable_reason(self) -> str:
        if self.provider is None:
            return ("Marketplace Insights provider not configured. This API is "
                    "restricted: eBay grants the buy.marketplace.insights scope "
                    "case by case and most keysets never receive it.")
        return ("eBay has not granted the buy.marketplace.insights scope to this "
                "keyset, so no licensed sold data is available. Apply via the "
                "eBay developer programme, or use manual sold records.")

    def preflight(self) -> dict[str, Any]:
        return {"source": self.name, "enabled": bool(self.enabled),
                "transport": "licensed API",
                "ready": self.is_available(),
                "reason": "Ready." if self.is_available()
                          else self.unavailable_reason()}

    def _collect(self, reference: str, brand: str, model: str,
                 **kwargs: Any) -> CollectionResult:
        evidence = self.provider.get_recent_sales(reference, period_days=365)
        if not getattr(evidence, "has_evidence", False):
            return CollectionResult.unavailable(
                self.name, reference,
                getattr(evidence, "notes", "No sales returned."))

        records = [
            CollectedEvidence(
                source=self.name, evidence_type=EV_SOLD, reference=reference,
                brand=brand, model=model, price_gbp=round(float(price), 2),
                original_price=round(float(price), 2), original_currency="GBP",
                price_certainty=CONFIRMED_PRICE,
                match_type=classify_match(reference, reference, model, brand),
                item_id=f"insights:{reference}:{i}",
                notes="eBay Marketplace Insights (licensed sold data).")
            for i, price in enumerate(getattr(evidence, "prices", []))
        ]
        return CollectionResult.ok(self.name, reference, records,
                                   detail="Licensed API sold data.")


class StoredSoldCollector(BaseCollector):
    """Tier B/C — sold records already in the database (manual imports).

    Always available and always enabled: reading our own table needs no
    network, no permission and no browser.
    """

    name = "ebay_sold_stored"
    evidence_type = EV_SOLD

    def __init__(self, conn: sqlite3.Connection, enabled: bool = True):
        super().__init__(enabled=enabled)
        self.conn = conn

    def is_available(self) -> bool:
        return self.conn is not None

    def unavailable_reason(self) -> str:
        return "No database connection."

    def preflight(self) -> dict[str, Any]:
        ready = self.is_available()
        count = 0
        if ready:
            try:
                from ..manual_evidence import init_manual_schema
                init_manual_schema(self.conn)
                count = self.conn.execute(
                    "SELECT COUNT(*) c FROM manual_sold_evidence").fetchone()["c"]
            except Exception:
                count = 0
        return {"source": self.name, "enabled": bool(self.enabled),
                "transport": "local database", "ready": ready,
                "stored_records": count,
                "reason": (f"{count} stored sold record(s)." if count else
                           "No sold records imported yet — import a CSV in "
                           "Market Evidence to give the valuation real sales.")}

    def _collect(self, reference: str, brand: str, model: str,
                 **kwargs: Any) -> CollectionResult:
        from ..manual_evidence import init_manual_schema
        from ..watchlist import normalise

        init_manual_schema(self.conn)
        rows = self.conn.execute(
            """SELECT * FROM manual_sold_evidence WHERE reference_key = ?
               ORDER BY sold_date DESC""", (normalise(reference),)).fetchall()
        if not rows:
            return CollectionResult.unavailable(
                self.name, reference, "No stored sold records for this reference.",
                status=HEALTH_OK)

        records = []
        for r in rows:
            records.append(CollectedEvidence(
                source=self.name, evidence_type=EV_SOLD,
                reference=r["reference"], brand=r["brand"], model=r["model"],
                price_gbp=r["sold_price_gbp"],
                original_price=r["sold_price_gbp"], original_currency="GBP",
                sale_date=r["sold_date"], condition=r["condition"],
                full_set=bool(r["full_set"]) if r["full_set"] is not None else None,
                item_id=f"manual:{r['id']}",
                price_certainty=CONFIRMED_PRICE,
                match_type=classify_match(r["reference"], reference, model, brand),
                notes="Manually recorded sale — you observed this transaction."))
        return CollectionResult.ok(self.name, reference, records,
                                   detail="Stored manual sold records.")


class EbaySoldWebCollector(BaseCollector):
    """Tier D — optional public-web collection. OFF by default.

    Gated three ways: an explicit setting, a robots.txt check, and a hard refusal
    to interpret blocking responses as anything other than "stop". This exists so
    the architecture is complete, not because it is expected to run.
    """

    name = "ebay_sold_web"
    evidence_type = EV_SOLD
    base_url = "https://www.ebay.co.uk"
    min_interval_seconds = 8.0

    # See product_research.MONEY_RE: the previous pattern truncated any price
    # without a thousands separator to its first three digits.
    PRICE_RE = re.compile(
        r"£\s?([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)")

    def __init__(self, enabled: bool | None = None, http_client=None,
                 browser_session=None):
        if enabled is None:
            enabled = os.getenv("WFS53_EBAY_SOLD_WEB", "0").strip().lower() in {
                "1", "true", "yes", "on"}
        super().__init__(enabled=enabled)
        # Phase 5.3 fix: previously the registry passed neither a client nor a
        # session, so this collector reported itself unavailable and never even
        # attempted a fetch. It now defaults to a real httpx transport.
        if http_client is None and browser_session is None:
            try:
                import httpx
                http_client = httpx
            except Exception:
                http_client = None
        self.http_client = http_client
        self.browser_session = browser_session

    def is_available(self) -> bool:
        return self.enabled and (self.http_client is not None
                                 or self.browser_session is not None)

    def unavailable_reason(self) -> str:
        if not self.enabled:
            return "Disabled in settings."
        return ("No HTTP transport available: httpx could not be imported and no "
                "browser session was supplied.")

    def preflight_url(self) -> str:
        return self.search_url("Tudor", "79030N")

    def search_url(self, brand: str, reference: str) -> str:
        from urllib.parse import quote_plus
        term = " ".join(p for p in (brand, reference) if p).strip()
        return (f"{self.base_url}/sch/i.html?_nkw={quote_plus(term)}"
                "&_sacat=31387&LH_Sold=1&LH_Complete=1")

    def parse(self, html: str, reference: str, brand: str = "",
              model: str = "") -> list[CollectedEvidence]:
        """Parse sold rows conservatively.

        Anything that looks like an accepted Best Offer is recorded as a SALE
        with BEST_OFFER_UNCERTAIN certainty: it counts toward liquidity and
        contributes nothing to the valuation.
        """
        records: list[CollectedEvidence] = []
        if not html:
            return records

        blocks = re.split(r"<li[^>]*s-item", html)[1:] or [html]
        for i, block in enumerate(blocks[:60]):
            title_match = re.search(r"s-item__title[^>]*>(.*?)<", block, re.S)
            title = re.sub(r"<[^>]+>", " ", title_match.group(1)).strip() \
                if title_match else ""
            match_type = classify_match(title, reference, model, brand)
            if match_type in ("REJECTED", "AMBIGUOUS"):
                continue

            price_match = self.PRICE_RE.search(block)
            if not price_match:
                continue
            try:
                price = float(price_match.group(1).replace(",", ""))
            except ValueError:
                continue
            if not (100 <= price <= 200000):
                continue

            best_offer = bool(BEST_OFFER_MARKERS.search(block))
            certainty = BEST_OFFER_UNCERTAIN if best_offer else DISPLAYED_SOLD_PRICE

            url_match = re.search(r'href="(https://www\.ebay\.co\.uk/itm/[^"?]+)',
                                  block)
            id_match = re.search(r"/itm/(\d{9,15})", block)

            records.append(CollectedEvidence(
                source=self.name, evidence_type=EV_SOLD, reference=reference,
                brand=brand, model=model, title=title or None,
                price_gbp=None if best_offer else price,
                original_price=price, original_currency="GBP",
                listing_url=url_match.group(1) if url_match else None,
                item_id=id_match.group(1) if id_match else None,
                price_certainty=certainty, match_type=match_type,
                notes=("Accepted Best Offer — displayed price is the asking "
                       "price, not the amount paid."
                       if best_offer else "Public completed-listing figure."),
            ))
        return records

    def _collect(self, reference: str, brand: str, model: str,
                 **kwargs: Any) -> CollectionResult:
        url = self.search_url(brand, reference)

        if not self.robots_allow(url):
            return CollectionResult.unavailable(
                self.name, reference,
                "robots.txt does not permit automated collection of this path.",
                status=HEALTH_BLOCKED)

        self.throttle()

        html = ""
        if self.http_client is not None:
            resp = self.http_client.get(
                url, timeout=20.0, headers={"User-Agent": self.user_agent},
                follow_redirects=True)
            status_code = getattr(resp, "status_code", 0)
            if status_code in (401, 403, 429):
                return CollectionResult.unavailable(
                    self.name, reference,
                    f"HTTP {status_code} — access refused. Not circumvented.",
                    status=HEALTH_BLOCKED)
            if status_code != 200:
                return CollectionResult.unavailable(
                    self.name, reference, f"HTTP {status_code}.")
            html = getattr(resp, "text", "")
        elif self.browser_session is not None:
            result = self.browser_session.fetch(url)
            if not result.ok:
                return CollectionResult.unavailable(
                    self.name, reference, result.error or "Browser fetch failed.",
                    status=HEALTH_BLOCKED if result.blocked else HEALTH_UNAVAILABLE)
            html = result.html

        records = self.parse(html, reference, brand, model)
        if not records:
            # Distinguish "page fetched, nothing matched" from "could not fetch".
            # A silently empty parse usually means the page structure changed.
            return CollectionResult.unavailable(
                self.name, reference,
                (f"Fetched {len(html):,} bytes but no sold rows matched the "
                 "expected structure. Either there are no completed listings for "
                 "this reference, or eBay's markup changed and the parser needs "
                 "updating."),
                status=HEALTH_PARTIAL)

        priced = sum(1 for r in records if r.usable_for_valuation)
        return CollectionResult.ok(
            self.name, reference, records,
            detail=(f"{len(records)} sold row(s), {priced} with a usable price, "
                    f"{len(records) - priced} Best Offer / unpriced."))
