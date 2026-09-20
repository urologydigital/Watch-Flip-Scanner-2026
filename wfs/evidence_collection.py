"""Collector registry and orchestration (Phase 5.3 §3, §19, §20, §22).

Decides which sources run, in what order, how often, and records how each one
behaved. The guiding constraints:

* **Safe sources are ON by default.** Local history and stored sold records need
  no network and no permission, so they run automatically. Optional public-web
  collectors are off until switched on in Settings.
* **Collection is per reference, not per listing.** A shortlist of ~15
  references serves every matching listing, so 189 analysed listings do not
  produce 189 collection runs.
* **One failed source never stops a scan.** Failures become source health.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import evidence_store
from .cache import get as cache_get
from .cache import init_cache
from .cache import put as cache_put
from .collectors.base import (EV_ACTIVE_ASKING, EV_MARKET_CONTEXT, EV_SOLD,
                              HEALTH_DISABLED, HEALTH_NOT_RUN, HEALTH_OK,
                              CollectedEvidence, CollectionResult, SourceStatus)

# --- source identifiers -----------------------------------------------------

SRC_EBAY_ACTIVE = "ebay_active_api"
SRC_EBAY_SOLD_INSIGHTS = "ebay_sold_insights"
SRC_EBAY_SOLD_STORED = "ebay_sold_stored"
SRC_EBAY_SOLD_WEB = "ebay_sold_web"
SRC_CHRONO24 = "chrono24"
SRC_WATCHCHARTS = "watchcharts"
SRC_LOCAL_HISTORY = "local_history"
SRC_PRODUCT_RESEARCH = "ebay_product_research"
SRC_WATCHCHARTS_API = "watchcharts_api"
SRC_AI = "ai"

SOURCE_LABEL = {
    SRC_EBAY_ACTIVE: "eBay Active API",
    SRC_EBAY_SOLD_INSIGHTS: "eBay Sold API (Marketplace Insights)",
    SRC_EBAY_SOLD_STORED: "Stored sold records",
    SRC_EBAY_SOLD_WEB: "eBay Sold (public web)",
    SRC_CHRONO24: "Chrono24",
    SRC_WATCHCHARTS: "WatchCharts",
    SRC_LOCAL_HISTORY: "Local History",
    SRC_PRODUCT_RESEARCH: "eBay Product Research (assisted)",
    SRC_WATCHCHARTS_API: "WatchCharts API",
    SRC_AI: "AI",
}

# Defaults (§20). Safe, permissionless sources are on; public-web ones are not.
DEFAULT_ENABLED = {
    # Reuses listings the scan already fetched — zero extra API calls.
    SRC_EBAY_ACTIVE: True,
    SRC_EBAY_SOLD_INSIGHTS: True,    # gated on actual API access anyway
    SRC_EBAY_SOLD_STORED: True,
    SRC_LOCAL_HISTORY: True,
    # Reads only what the user has already captured — no network, no automation.
    SRC_PRODUCT_RESEARCH: True,
    # Optional licensed API; reports NOT_CONFIGURED without a key.
    SRC_WATCHCHARTS_API: True,
    SRC_EBAY_SOLD_WEB: False,
    SRC_CHRONO24: False,
    SRC_WATCHCHARTS: False,
}

# Cache TTLs in seconds (§22).
DEFAULT_TTL = {
    # Not cached: the data arrives fresh with every scan at no cost, and caching
    # it would risk serving last scan's supply picture.
    SRC_EBAY_ACTIVE: 0,
    SRC_EBAY_SOLD_INSIGHTS: 24 * 3600,
    SRC_EBAY_SOLD_STORED: 3600,
    SRC_EBAY_SOLD_WEB: 24 * 3600,
    SRC_CHRONO24: 8 * 3600,
    SRC_WATCHCHARTS: 24 * 3600,
    SRC_LOCAL_HISTORY: 0,            # persistent; always recomputed, no network
    SRC_PRODUCT_RESEARCH: 0,         # local capture; always current
    SRC_WATCHCHARTS_API: 3 * 24 * 3600,   # valuation moves slowly
}

NS_EVIDENCE = "collected_evidence"


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class CollectionSettings:
    """Persisted, user-controllable collector configuration (§20)."""

    master_enabled: bool = True          # "AUTOMATIC MARKET EVIDENCE"
    sources: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_ENABLED))
    shortlist_cap: int = field(
        default_factory=lambda: _i("WFS53_EVIDENCE_SHORTLIST_CAP", 15))
    lookback_days: int = field(
        default_factory=lambda: _i("WFS53_EVIDENCE_LOOKBACK_DAYS", 365))

    def is_enabled(self, source: str) -> bool:
        if not self.master_enabled:
            return False
        return bool(self.sources.get(source, DEFAULT_ENABLED.get(source, False)))

    def as_dict(self) -> dict[str, Any]:
        return {"master_enabled": self.master_enabled, "sources": dict(self.sources),
                "shortlist_cap": self.shortlist_cap,
                "lookback_days": self.lookback_days}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CollectionSettings":
        data = data or {}
        sources = dict(DEFAULT_ENABLED)
        for key, value in (data.get("sources") or {}).items():
            if key in DEFAULT_ENABLED:
                sources[key] = bool(value)
        return cls(
            master_enabled=bool(data.get("master_enabled", True)),
            sources=sources,
            shortlist_cap=int(data.get("shortlist_cap", 15)),
            lookback_days=int(data.get("lookback_days", 365)),
        )


def load_collection_settings(settings: dict[str, Any] | None = None
                             ) -> CollectionSettings:
    if settings is None:
        from .settings_store import load_settings
        settings = load_settings()
    return CollectionSettings.from_dict(settings.get("evidence_collection"))


def save_collection_settings(collection: CollectionSettings,
                             settings: dict[str, Any] | None = None
                             ) -> dict[str, Any]:
    from .settings_store import load_settings, save_settings
    data = dict(settings if settings is not None else load_settings())
    data["evidence_collection"] = collection.as_dict()
    return save_settings(data)


# --- registry ---------------------------------------------------------------

@dataclass
class EvidenceRun:
    """Everything one collection pass produced."""

    references: list[str] = field(default_factory=list)
    statuses: dict[str, SourceStatus] = field(default_factory=dict)
    stored: dict[str, int] = field(default_factory=dict)
    cache_hits: int = 0
    collection_calls: int = 0
    records_collected: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "references": len(self.references),
            "cache_hits": self.cache_hits,
            "collection_calls": self.collection_calls,
            "records_collected": self.records_collected,
            "stored": self.stored,
            "sources": {k: v.as_dict() for k, v in self.statuses.items()},
        }


class CollectorRegistry:
    """Builds the collector set and runs it over a reference shortlist."""

    def __init__(self, conn: sqlite3.Connection,
                 settings: CollectionSettings | None = None,
                 collectors: list[Any] | None = None,
                 browser_session=None, insights_provider=None,
                 http_client: Any = None,
                 listings_by_reference: dict[str, list[dict[str, Any]]] | None = None):
        self.conn = conn
        self.settings = settings or load_collection_settings()
        self.browser_session = browser_session
        self._custom = collectors
        self.insights_provider = insights_provider
        # Phase 5.3 fix: supply a real HTTP transport. Previously neither a
        # client nor a session was passed, so every web collector reported
        # itself unavailable and never attempted a fetch.
        if http_client is None:
            try:
                import httpx
                http_client = httpx
            except Exception:
                http_client = None
        self.http_client = http_client
        self.listings_by_reference = listings_by_reference or {}
        self._built: list[Any] | None = None

    def set_listings(self, listings_by_reference) -> None:
        """Hand the collector set the listings discovery already fetched."""
        self.listings_by_reference = listings_by_reference or {}
        for collector in (self._built or []):
            if hasattr(collector, "set_listings"):
                collector.set_listings(self.listings_by_reference)

    # -- construction ------------------------------------------------------
    def build(self) -> list[Any]:
        if self._custom is not None:
            return self._custom
        if self._built is not None:
            return self._built

        from .collectors.chrono24 import Chrono24Collector
        from .collectors.ebay_active import EbayActiveCollector
        from .collectors.ebay_sold import (EbaySoldWebCollector,
                                           MarketplaceInsightsCollector,
                                           StoredSoldCollector)
        from .collectors.local_history import LocalHistoryCollector
        from .collectors.product_research import ProductResearchCollector
        from .collectors.watchcharts import WatchChartsCollector
        from .collectors.watchcharts_api import WatchChartsAPICollector

        s = self.settings
        provider = self.insights_provider
        if provider is None:
            try:
                from .sold_market import MarketplaceInsightsProvider
                provider = MarketplaceInsightsProvider()
            except Exception:
                provider = None

        self._built = [
            EbayActiveCollector(
                enabled=s.is_enabled(SRC_EBAY_ACTIVE),
                listings_by_reference=self.listings_by_reference),
            MarketplaceInsightsCollector(
                provider=provider,
                enabled=s.is_enabled(SRC_EBAY_SOLD_INSIGHTS)),
            StoredSoldCollector(self.conn,
                                enabled=s.is_enabled(SRC_EBAY_SOLD_STORED)),
            ProductResearchCollector(
                self.conn, enabled=s.is_enabled(SRC_PRODUCT_RESEARCH)),
            WatchChartsAPICollector(enabled=s.is_enabled(SRC_WATCHCHARTS_API),
                                    http_client=self.http_client),
            LocalHistoryCollector(self.conn,
                                  enabled=s.is_enabled(SRC_LOCAL_HISTORY),
                                  lookback_days=s.lookback_days),
            EbaySoldWebCollector(enabled=s.is_enabled(SRC_EBAY_SOLD_WEB),
                                 http_client=self.http_client,
                                 browser_session=self.browser_session),
            Chrono24Collector(enabled=s.is_enabled(SRC_CHRONO24),
                              browser_session=self.browser_session),
            WatchChartsCollector(enabled=s.is_enabled(SRC_WATCHCHARTS),
                                 http_client=self.http_client,
                                 browser_session=self.browser_session),
        ]
        return self._built

    def preflight(self) -> list[dict[str, Any]]:
        """Diagnose every collector without collecting anything."""
        out = []
        for collector in self.build():
            try:
                out.append(collector.preflight())
            except Exception as exc:
                out.append({"source": getattr(collector, "name", "?"),
                            "ready": False,
                            "reason": f"preflight failed: {exc}"})
        return out

    # -- running -----------------------------------------------------------
    def collect_for_reference(self, reference: str, brand: str = "",
                              model: str = "", force_refresh: bool = False,
                              run: EvidenceRun | None = None
                              ) -> list[CollectedEvidence]:
        """Run every enabled collector for one reference, with caching."""
        init_cache(self.conn)
        evidence_store.migrate(self.conn)
        run = run if run is not None else EvidenceRun()
        collected: list[CollectedEvidence] = []

        for collector in self.build():
            name = getattr(collector, "name", "unknown")
            ttl = DEFAULT_TTL.get(name, 6 * 3600)

            if not getattr(collector, "enabled", False):
                status = SourceStatus(name, HEALTH_DISABLED, 0, None, "MISS",
                                      "Disabled in settings.")
                run.statuses[name] = status
                evidence_store.record_source_health(self.conn, status)
                continue

            # Cache check — a second scan soon after the first reuses evidence.
            if ttl and not force_refresh:
                cached = cache_get(self.conn, NS_EVIDENCE, name, reference)
                if cached is not None:
                    run.cache_hits += 1
                    status = SourceStatus(name, HEALTH_OK, len(cached), None,
                                          "HIT", None, "Served from cache.")
                    run.statuses[name] = status
                    evidence_store.record_source_health(self.conn, status)
                    collected.extend(
                        CollectedEvidence(**rec) for rec in cached)
                    continue

            listings = self.listings_by_reference.get(reference)
            result = (collector.collect(reference, brand, model, listings=listings)
                      if listings and hasattr(collector, "set_listings")
                      else collector.collect(reference, brand, model))
            run.collection_calls += 1
            run.statuses[name] = result.status
            evidence_store.record_source_health(self.conn, result.status)

            if result.records:
                collected.extend(result.records)
                run.records_collected += len(result.records)
                if ttl:
                    cache_put(self.conn, NS_EVIDENCE,
                              [r.as_dict() for r in result.records],
                              name, reference, ttl_seconds=ttl)

        if collected:
            counts = evidence_store.store_evidence(self.conn, collected)
            for key, value in counts.items():
                run.stored[key] = run.stored.get(key, 0) + value
        return collected

    def collect_for_references(self, references: list[tuple[str, str, str]],
                               force_refresh: bool = False,
                               progress=None) -> EvidenceRun:
        """Collect once per reference across a capped shortlist (§3)."""
        run = EvidenceRun()
        capped = references[:max(0, self.settings.shortlist_cap)]
        for i, (reference, brand, model) in enumerate(capped):
            if progress:
                progress(f"Collecting evidence for {brand} {reference}…",
                         i / max(len(capped), 1))
            self.collect_for_reference(reference, brand, model,
                                       force_refresh=force_refresh, run=run)
            run.references.append(reference)
        return run


def source_health_report(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Health of every known source, including ones never run (§19)."""
    stored = {row["source"]: row for row in evidence_store.source_health(conn)}
    out = []
    for source, label in SOURCE_LABEL.items():
        row = stored.get(source)
        out.append({
            "source": source,
            "label": label,
            "status": row["status"] if row else HEALTH_NOT_RUN,
            "record_count": row["record_count"] if row else 0,
            "last_success": row["last_success"] if row else None,
            "last_attempt": row["last_attempt"] if row else None,
            "cache_status": row["cache_status"] if row else None,
            "error": row["error"] if row else None,
            "detail": row["detail"] if row else "",
        })
    return out
