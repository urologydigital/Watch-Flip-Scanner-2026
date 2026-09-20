"""WatchCharts market context (Phase 5.3 §8).

No paid subscription and no API key. This collector uses only publicly visible
information and refuses to operate behind a login or paywall — bypassing access
control is not an option this project takes.

Whatever it produces is MARKET_CONTEXT: an index-style benchmark, not a
transaction. It informs cross-check ranking and can never produce a BUY.

The adapter is shaped so a licensed WatchCharts API could be added later by
implementing `_fetch` differently, with no change to the valuation architecture.
"""
from __future__ import annotations

import os
import re
from typing import Any

from .base import (EV_MARKET_CONTEXT, HEALTH_BLOCKED, HEALTH_PARTIAL,
                   PRICE_UNKNOWN, BaseCollector, CollectedEvidence,
                   CollectionResult, classify_match)

WATCHCHARTS_UNAVAILABLE = "WATCHCHARTS_UNAVAILABLE"


class WatchChartsCollector(BaseCollector):
    """Public market-context collector. OFF by default."""

    name = "watchcharts"
    evidence_type = EV_MARKET_CONTEXT
    base_url = "https://watchcharts.com"
    min_interval_seconds = 8.0

    # See product_research.MONEY_RE: guards against truncating unseparated
    # amounts to three digits.
    PRICE_RE = re.compile(
        r"£\s?([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{3,6})")

    def __init__(self, enabled: bool | None = None, http_client=None,
                 browser_session=None):
        if enabled is None:
            enabled = os.getenv("WFS53_WATCHCHARTS_ENABLED", "0").strip().lower() \
                in {"1", "true", "yes", "on"}
        super().__init__(enabled=enabled)
        # Phase 5.3 fix: default to a real httpx transport. Previously the
        # registry supplied neither, so this collector never attempted a fetch
        # and reported a generic "not available in this environment".
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
        return f"{self.base_url}/watches/search?q={quote_plus(term)}"

    def parse(self, html: str, reference: str, brand: str = "",
              model: str = "") -> list[CollectedEvidence]:
        if not html:
            return []
        # A login wall is a refusal, not a puzzle to solve.
        if re.search(r"sign\s*in\s*to\s*view|subscribe\s*to\s*view|paywall",
                     html, re.I):
            return []

        prices = []
        for m in self.PRICE_RE.finditer(html):
            try:
                value = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if 200 <= value <= 200000:
                prices.append(value)
        if not prices:
            return []

        import statistics
        mid = round(statistics.median(prices), 2)
        return [CollectedEvidence(
            source=self.name, evidence_type=EV_MARKET_CONTEXT,
            reference=reference, brand=brand, model=model,
            price_gbp=mid, original_price=mid, original_currency="GBP",
            listing_url=self.search_url(brand, reference),
            item_id=f"watchcharts:{reference}",
            price_certainty=PRICE_UNKNOWN,
            match_type=classify_match(reference, reference, model, brand),
            notes=("WatchCharts public market context — an index benchmark, "
                   "not a confirmed transaction."))]

    def _collect(self, reference: str, brand: str, model: str,
                 **kwargs: Any) -> CollectionResult:
        url = self.search_url(brand, reference)
        if not self.robots_allow(url):
            return CollectionResult.unavailable(
                self.name, reference,
                f"{WATCHCHARTS_UNAVAILABLE}: robots.txt does not permit this path.",
                status=HEALTH_BLOCKED)

        self.throttle()
        html = ""
        if self.http_client is not None:
            resp = self.http_client.get(
                url, timeout=20.0, headers={"User-Agent": self.user_agent},
                follow_redirects=True)
            code = getattr(resp, "status_code", 0)
            if code in (401, 403, 429):
                return CollectionResult.unavailable(
                    self.name, reference,
                    f"{WATCHCHARTS_UNAVAILABLE}: HTTP {code} — access refused.",
                    status=HEALTH_BLOCKED)
            if code != 200:
                return CollectionResult.unavailable(
                    self.name, reference, f"{WATCHCHARTS_UNAVAILABLE}: HTTP {code}.")
            html = getattr(resp, "text", "")
        elif self.browser_session is not None:
            result = self.browser_session.fetch(url)
            if not result.ok:
                return CollectionResult.unavailable(
                    self.name, reference,
                    f"{WATCHCHARTS_UNAVAILABLE}: {result.error}",
                    status=HEALTH_BLOCKED if result.blocked else None or HEALTH_PARTIAL)
            html = result.html

        records = self.parse(html, reference, brand, model)
        if not records:
            paywalled = bool(re.search(
                r"sign\s*in|log\s*in|subscribe|create\s*(an\s*)?account",
                html or "", re.I))
            return CollectionResult.unavailable(
                self.name, reference,
                (f"{WATCHCHARTS_UNAVAILABLE}: fetched {len(html):,} bytes but no "
                 + ("public benchmark was visible — the figure appears to sit "
                    "behind a sign-in, which this collector will not bypass."
                    if paywalled else
                    "price matched the expected structure. Either the reference "
                    "is not covered, or the page markup changed.")),
                status=HEALTH_PARTIAL)
        return CollectionResult.ok(self.name, reference, records,
                                   detail="Public market context benchmark.")
