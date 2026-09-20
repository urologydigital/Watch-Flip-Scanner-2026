"""Sold / transaction evidence providers (spec s.4).

The eBay Browse API does NOT contain completed-transaction history. Rather than
invent sales counts, this module defines a provider interface and ships three
implementations:

  NullSoldProvider          - always reports coverage_status = "UNAVAILABLE"
  ManualSoldProvider        - reads sales YOU have observed and recorded in a CSV
  MarketplaceInsightsProvider - eBay Marketplace Insights API (restricted access;
                                you must be approved by eBay). Inactive until
                                credentials with that scope are configured.

Every result carries coverage_status and the period measured. Counts are always
described as OBSERVED, never as total UK sales.
"""
from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import OBSERVED_SALES_PATH
from .watchlist import normalise

COVERAGE_UNAVAILABLE = "UNAVAILABLE"
COVERAGE_PARTIAL = "PARTIAL"

SCOPE_EXACT = "EXACT_REFERENCE"
SCOPE_FAMILY = "MODEL_FAMILY"


@dataclass
class SoldEvidence:
    """Observed sold-market evidence for one reference. Never a complete record."""

    reference: str
    period_days: int
    source: str
    coverage_status: str = COVERAGE_UNAVAILABLE
    exact_sale_count: int = 0
    family_sale_count: int = 0
    prices: list[float] = field(default_factory=list)
    notes: str = ""

    @property
    def median_price(self) -> float | None:
        return round(statistics.median(self.prices), 2) if self.prices else None

    @property
    def price_dispersion(self) -> float | None:
        """Coefficient of variation of observed sale prices."""
        if len(self.prices) < 3:
            return None
        mean = statistics.mean(self.prices)
        if mean <= 0:
            return None
        return round(statistics.pstdev(self.prices) / mean, 3)

    @property
    def low_high(self) -> tuple[float | None, float | None]:
        if len(self.prices) < 3:
            return (None, None)
        ordered = sorted(self.prices)
        lo = ordered[max(0, int(len(ordered) * 0.25) - 1)]
        hi = ordered[min(len(ordered) - 1, int(len(ordered) * 0.75))]
        return (round(lo, 2), round(hi, 2))

    @property
    def has_evidence(self) -> bool:
        return self.coverage_status != COVERAGE_UNAVAILABLE and self.exact_sale_count > 0

    def describe(self) -> str:
        if not self.has_evidence:
            return "No permitted sold-data source configured — no sold evidence."
        return (
            f"Observed exact-reference sales: {self.exact_sale_count} in last "
            f"{self.period_days} days. Broader model-family sales: "
            f"{self.family_sale_count}. Coverage: {self.coverage_status.title()}."
        )


class SoldMarketProvider:
    """Interface. Subclasses must never fabricate sales."""

    name = "none"

    def get_recent_sales(self, reference: str, country: str = "GB",
                         period_days: int = 90) -> SoldEvidence:
        raise NotImplementedError

    def get_observed_sale_prices(self, reference: str, period_days: int = 90) -> list[float]:
        return self.get_recent_sales(reference, period_days=period_days).prices

    def get_sale_count(self, reference: str, period_days: int = 90) -> dict[str, Any]:
        ev = self.get_recent_sales(reference, period_days=period_days)
        return {
            "reference": reference,
            "period_days": period_days,
            "observed_sale_count": ev.exact_sale_count if ev.has_evidence else None,
            "coverage_status": ev.coverage_status,
            "source": ev.source,
        }


class NullSoldProvider(SoldMarketProvider):
    """Default. Reports honestly that no sold data source exists."""

    name = "none"

    def get_recent_sales(self, reference: str, country: str = "GB",
                         period_days: int = 90) -> SoldEvidence:
        return SoldEvidence(
            reference=reference,
            period_days=period_days,
            source=self.name,
            coverage_status=COVERAGE_UNAVAILABLE,
            notes="No permitted sold-data source configured.",
        )


class ManualSoldProvider(SoldMarketProvider):
    """Reads sales you have observed yourself and recorded in observed_sales.csv.

    Columns: sold_date,reference,model_family,price_gbp,source,notes
    This is your own record-keeping, so coverage is always PARTIAL by definition.
    """

    name = "manual_csv"

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or OBSERVED_SALES_PATH)
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
                        sold = datetime.fromisoformat(raw["sold_date"].strip())
                        if sold.tzinfo is None:
                            sold = sold.replace(tzinfo=timezone.utc)
                    except (KeyError, ValueError, TypeError):
                        continue
                    rows.append({
                        "sold_date": sold,
                        "reference": normalise(raw.get("reference", "")),
                        "model_family": (raw.get("model_family") or "").strip().upper(),
                        "price": price,
                    })
        self._rows = rows
        return rows

    def get_recent_sales(self, reference: str, country: str = "GB",
                         period_days: int = 90) -> SoldEvidence:
        cutoff = datetime.now(timezone.utc) - timedelta(days=period_days)
        key = normalise(reference)
        exact, family = [], []
        for row in self._load():
            if row["sold_date"] < cutoff:
                continue
            if key and (row["reference"] == key or key in row["reference"]):
                exact.append(row["price"])
            elif row["model_family"] and key.startswith(normalise(row["model_family"])[:4]):
                family.append(row["price"])

        if not exact and not family:
            return SoldEvidence(reference, period_days, self.name,
                                COVERAGE_UNAVAILABLE,
                                notes=f"No recorded observations in {self.path.name}.")
        return SoldEvidence(
            reference=reference,
            period_days=period_days,
            source=self.name,
            coverage_status=COVERAGE_PARTIAL,
            exact_sale_count=len(exact),
            family_sale_count=len(exact) + len(family),
            prices=exact,
            notes="Self-recorded observations — partial coverage by definition.",
        )


class MarketplaceInsightsProvider(SoldMarketProvider):
    """eBay Marketplace Insights API — OPTIONAL ENHANCEMENT, NOT A DEPENDENCY.

    Access to this API is restricted. eBay grants it case by case, and most
    developer accounts will never receive it. Do not plan around having it.

    The scanner is designed to work fully without it: manually imported sold
    records and the scanner's own observation history are the realistic primary
    sources. If access is granted, this provider slots in as the strongest
    evidence tier; if not, nothing breaks and nothing is fabricated.
    """

    name = "ebay_marketplace_insights"
    ENDPOINT = "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"
    SCOPE = "https://api.ebay.com/oauth/api_scope/buy.marketplace.insights"

    def __init__(self, client=None, enabled: bool = False):
        self.client = client
        self.enabled = enabled

    def get_recent_sales(self, reference: str, country: str = "GB",
                         period_days: int = 90) -> SoldEvidence:
        if not (self.enabled and self.client):
            return SoldEvidence(
                reference, period_days, self.name, COVERAGE_UNAVAILABLE,
                notes="Marketplace Insights access not granted for this keyset.",
            )
        try:
            items = self.client.search_sold(reference, period_days=period_days)
        except Exception as exc:  # network / auth / scope failure
            return SoldEvidence(reference, period_days, self.name,
                                COVERAGE_UNAVAILABLE, notes=f"Lookup failed: {exc}")
        prices = []
        key = normalise(reference)
        for item in items:
            title = normalise(item.get("title", ""))
            value = (item.get("lastSoldPrice") or {}).get("value")
            if value is None or key not in title:
                continue
            try:
                prices.append(float(value))
            except (TypeError, ValueError):
                continue
        if not prices:
            return SoldEvidence(reference, period_days, self.name,
                                COVERAGE_UNAVAILABLE,
                                notes="No matching sales returned.")
        return SoldEvidence(
            reference=reference,
            period_days=period_days,
            source=self.name,
            coverage_status=COVERAGE_PARTIAL,
            exact_sale_count=len(prices),
            family_sale_count=len(items),
            prices=prices,
            notes="eBay-reported sales. Coverage is partial: private listings and "
                  "off-platform sales are not included.",
        )


def default_provider() -> SoldMarketProvider:
    """Manual CSV if you have recorded observations, otherwise null."""
    if Path(OBSERVED_SALES_PATH).exists():
        provider = ManualSoldProvider()
        if provider._load():
            return provider
    return NullSoldProvider()


class CompositeSoldProvider(SoldMarketProvider):
    """Tries providers in strength order and returns the first real evidence.

    Priority (spec 5.1 s.12, s.18) — strongest first:

      1. Manually imported sold records   (you saw the sale; strongest realistic)
      2. Marketplace Insights             (optional; access is often restricted)
      3. Legacy observed_sales.csv        (older manual format, still supported)
      4. Local observation history        (inference, never confirmed sales)

    Marketplace Insights is an OPTIONAL enhancement. The scanner is fully
    functional without it and never assumes access has been granted.
    """

    name = "composite"

    def __init__(self, providers: list[SoldMarketProvider]):
        self.providers = providers
        self.last_source: str | None = None

    def get_recent_sales(self, reference: str, country: str = "GB",
                         period_days: int = 90) -> SoldEvidence:
        weakest: SoldEvidence | None = None
        for provider in self.providers:
            try:
                evidence = provider.get_recent_sales(reference, country=country,
                                                     period_days=period_days)
            except Exception:
                continue
            if evidence.has_evidence:
                self.last_source = provider.name
                return evidence
            weakest = weakest or evidence
        self.last_source = None
        return weakest or SoldEvidence(reference, period_days, self.name,
                                       COVERAGE_UNAVAILABLE,
                                       notes="No evidence from any provider.")


def build_provider(conn=None, include_observation: bool = True) -> SoldMarketProvider:
    """Assemble the default provider chain for this installation."""
    providers: list[SoldMarketProvider] = []

    if conn is not None:
        from .manual_evidence import DatabaseManualSoldProvider
        providers.append(DatabaseManualSoldProvider(conn))

    insights = MarketplaceInsightsProvider()
    if insights.enabled:
        providers.append(insights)

    if Path(OBSERVED_SALES_PATH).exists():
        legacy = ManualSoldProvider()
        if legacy._load():
            providers.append(legacy)

    if conn is not None and include_observation:
        from .observation import LocalObservationProvider
        providers.append(LocalObservationProvider(conn))

    if not providers:
        return NullSoldProvider()
    return CompositeSoldProvider(providers)
