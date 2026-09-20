"""Active asking-market cross-check (spec s.5, s.6).

Chrono24 publishes no public marketplace API. This module therefore defines a
provider interface with three implementations, and the default is OFF:

  NullAskingProvider     - returns UNAVAILABLE. The scan continues normally.
  ManualAskingProvider   - reads asking prices YOU have recorded in a CSV.
                           Unambiguously permitted; this is the recommended route.
  Chrono24WebProvider    - opt-in only (WFS_CHRONO24_ENABLED=1). Checks robots.txt
                           before every fetch, rate-limits to one request at a time,
                           and refuses to run if disallowed. Automated collection may
                           still breach Chrono24's terms of use regardless of what
                           robots.txt permits; enabling it is your decision and your
                           responsibility.

Everything produced here is ACTIVE ASKING DATA. It is never labelled as sold, and
sales volume is never derived from it.
"""
from __future__ import annotations

import csv
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import robotparser

from .config import (CHRONO24_ASKING_PATH, CHRONO24_ENABLED, CHRONO24_MIN_INTERVAL,
                     DEALER_PRICES_PATH, SETTINGS)
from .watchlist import normalise

LABEL_CHRONO24 = "CHRONO24 ACTIVE ASKING DATA"
LABEL_DEALER = "UK DEALER / INDEX ASKING CONTEXT"

COVERAGE_UNAVAILABLE = "UNAVAILABLE"
COVERAGE_PARTIAL = "PARTIAL"


@dataclass
class AskingEvidence:
    """Observed ACTIVE asking prices. Never sold data, never a sales count."""

    reference: str
    source: str
    label: str
    coverage_status: str = COVERAGE_UNAVAILABLE
    prices: list[float] = field(default_factory=list)
    listings_observed: int = 0
    dealer_count: int = 0
    private_count: int = 0
    countries: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def has_evidence(self) -> bool:
        return self.coverage_status != COVERAGE_UNAVAILABLE and len(self.prices) >= 2

    @property
    def asking_range(self) -> tuple[float | None, float | None]:
        if not self.prices:
            return (None, None)
        return (round(min(self.prices), 2), round(max(self.prices), 2))

    def describe(self) -> str:
        if not self.has_evidence:
            return f"{self.label}: unavailable. {self.notes}".strip()
        lo, hi = self.asking_range
        return (
            f"{self.label}: {self.listings_observed} competing listing(s) observed, "
            f"asking £{lo:,.0f}–£{hi:,.0f}. These are asking prices, not sales."
        )


class AskingMarketProvider:
    name = "none"
    label = LABEL_CHRONO24

    def get_asking_prices(self, brand: str, model: str, reference: str) -> AskingEvidence:
        raise NotImplementedError


class NullAskingProvider(AskingMarketProvider):
    """Default. The scan must never fail because this data is missing."""

    def __init__(self, label: str = LABEL_CHRONO24, note: str = ""):
        self.label = label
        self.note = note or "No asking-market source configured."

    def get_asking_prices(self, brand: str, model: str, reference: str) -> AskingEvidence:
        return AskingEvidence(reference, self.name, self.label,
                              COVERAGE_UNAVAILABLE, notes=self.note)


class ManualAskingProvider(AskingMarketProvider):
    """Reads asking prices you have recorded yourself.

    Columns: observed_date,reference,price_gbp,seller_type,country,year,condition,box_papers
    seller_type is 'dealer' or 'private'. Rows older than max_age_days are ignored,
    because a stale asking price is not market context.
    """

    name = "manual_csv"

    def __init__(self, path: Path | str | None = None, label: str = LABEL_CHRONO24,
                 max_age_days: int = 60):
        self.path = Path(path or CHRONO24_ASKING_PATH)
        self.label = label
        self.max_age_days = max_age_days
        self._rows: list[dict[str, Any]] | None = None

    def _load(self) -> list[dict[str, Any]]:
        if self._rows is not None:
            return self._rows
        rows: list[dict[str, Any]] = []
        if self.path.exists():
            with self.path.open(newline="", encoding="utf-8") as fh:
                for raw in csv.DictReader(fh):
                    try:
                        price = float(raw["price_gbp"])
                        seen = datetime.fromisoformat(raw["observed_date"].strip())
                        if seen.tzinfo is None:
                            seen = seen.replace(tzinfo=timezone.utc)
                    except (KeyError, ValueError, TypeError):
                        continue
                    rows.append({
                        "observed": seen,
                        "reference": normalise(raw.get("reference", "")),
                        "price": price,
                        "seller_type": (raw.get("seller_type") or "").strip().lower(),
                        "country": (raw.get("country") or "").strip().upper(),
                    })
        self._rows = rows
        return rows

    def get_asking_prices(self, brand: str, model: str, reference: str) -> AskingEvidence:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.max_age_days)
        key = normalise(reference)
        matched = [r for r in self._load()
                   if r["observed"] >= cutoff and key and key in r["reference"]]
        if len(matched) < 2:
            return AskingEvidence(
                reference, self.name, self.label, COVERAGE_UNAVAILABLE,
                notes=(f"Fewer than 2 recent observations in {self.path.name} "
                       f"(within {self.max_age_days} days)."),
            )
        return AskingEvidence(
            reference=reference,
            source=self.name,
            label=self.label,
            coverage_status=COVERAGE_PARTIAL,
            prices=[r["price"] for r in matched],
            listings_observed=len(matched),
            dealer_count=sum(1 for r in matched if r["seller_type"] == "dealer"),
            private_count=sum(1 for r in matched if r["seller_type"] == "private"),
            countries=sorted({r["country"] for r in matched if r["country"]}),
            notes="Self-recorded active asking observations. Partial coverage.",
        )


class Chrono24WebProvider(AskingMarketProvider):
    """Opt-in public-web lookup. Disabled unless WFS_CHRONO24_ENABLED=1.

    Deliberately conservative: one request per reference, a mandatory delay between
    requests, a robots.txt check before each fetch, and a hard fail-open — any error
    returns UNAVAILABLE and the scan carries on.
    """

    name = "chrono24_web"
    label = LABEL_CHRONO24
    BASE = "https://www.chrono24.co.uk"
    SEARCH = BASE + "/search/index.htm"

    _lock = threading.Lock()
    _last_request = 0.0

    def __init__(self, enabled: bool | None = None, client=None):
        self.enabled = CHRONO24_ENABLED if enabled is None else enabled
        self.client = client
        self._robots: robotparser.RobotFileParser | None = None

    # -- politeness ---------------------------------------------------------
    def _robots_allow(self, url: str) -> bool:
        if self._robots is None:
            rp = robotparser.RobotFileParser()
            rp.set_url(f"{self.BASE}/robots.txt")
            try:
                rp.read()
            except Exception:
                return False  # cannot verify -> do not fetch
            self._robots = rp
        try:
            return self._robots.can_fetch("*", url)
        except Exception:
            return False

    def _throttle(self) -> None:
        with Chrono24WebProvider._lock:
            elapsed = time.time() - Chrono24WebProvider._last_request
            if elapsed < CHRONO24_MIN_INTERVAL:
                time.sleep(CHRONO24_MIN_INTERVAL - elapsed)
            Chrono24WebProvider._last_request = time.time()

    # -- fetch --------------------------------------------------------------
    def _fetch(self, reference: str) -> str:
        import httpx

        params = {"query": reference, "currencyId": "GBP", "pageSize": "60"}
        url = self.SEARCH
        if not self._robots_allow(url):
            raise PermissionError("Disallowed by chrono24 robots.txt")
        self._throttle()
        http = self.client or httpx
        resp = http.get(url, params=params, timeout=SETTINGS.request_timeout,
                        headers={"User-Agent": "WatchFlipScannerUK/0.3 (personal use)"},
                        follow_redirects=True)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return resp.text

    PRICE_RE = re.compile(r"£\s?([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,6})")

    def parse_prices(self, html: str) -> list[float]:
        out: list[float] = []
        for match in self.PRICE_RE.finditer(html or ""):
            try:
                value = float(match.group(1).replace(",", ""))
            except ValueError:
                continue
            if 200 <= value <= 100000:  # discard page furniture and outliers
                out.append(value)
        return out

    def get_asking_prices(self, brand: str, model: str, reference: str) -> AskingEvidence:
        if not self.enabled:
            return AskingEvidence(
                reference, self.name, self.label, COVERAGE_UNAVAILABLE,
                notes=("Chrono24 web lookup is disabled. Set WFS_CHRONO24_ENABLED=1 "
                       "only if you accept responsibility for their terms of use."),
            )
        try:
            html = self._fetch(reference)
            prices = self.parse_prices(html)
        except Exception as exc:
            return AskingEvidence(reference, self.name, self.label,
                                  COVERAGE_UNAVAILABLE,
                                  notes=f"Lookup unavailable: {exc}")
        if len(prices) < 2:
            return AskingEvidence(reference, self.name, self.label,
                                  COVERAGE_UNAVAILABLE,
                                  notes="No usable asking prices parsed.")
        return AskingEvidence(
            reference=reference,
            source=self.name,
            label=self.label,
            coverage_status=COVERAGE_PARTIAL,
            prices=prices,
            listings_observed=len(prices),
            notes=("Parsed from a public search page. Reference match is by search "
                   "term only, so treat as approximate market context."),
        )


class ManualDealerProvider(ManualAskingProvider):
    """UK dealer / WatchCharts context you have recorded.

    WatchCharts has no public API. Record figures you look up yourself.
    Dealer asking price is NOT transaction value (spec s.6) — it is weighted at 10%
    and used only as context.
    """

    name = "manual_dealer_csv"

    def __init__(self, path: Path | str | None = None):
        super().__init__(path or DEALER_PRICES_PATH, label=LABEL_DEALER,
                         max_age_days=90)


def default_chrono24_provider() -> AskingMarketProvider:
    if CHRONO24_ENABLED:
        return Chrono24WebProvider(enabled=True)
    manual = ManualAskingProvider()
    if manual.path.exists() and manual._load():
        return manual
    return NullAskingProvider(LABEL_CHRONO24,
                              "Chrono24 lookup disabled and no manual observations recorded.")


def default_dealer_provider() -> AskingMarketProvider:
    manual = ManualDealerProvider()
    if manual.path.exists() and manual._load():
        return manual
    return NullAskingProvider(LABEL_DEALER, "No dealer observations recorded.")
