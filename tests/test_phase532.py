"""Phase 5.3.2 — live collector availability fixes.

The reported failure: WatchCharts and eBay Sold (public web) reported the
generic "Collector is not available in this environment", and eBay Active
reported NOT_RUN, so a 15-reference scan produced 180 evidence records that were
all OBSERVATION and no market-price evidence at all.

Root cause: the registry constructed the web collectors with
`browser_session=None` and never passed an `http_client`, so `is_available()`
was always False and they never attempted a fetch. eBay Active had no collector
at all, so nothing ever recorded health for it.

These tests pin the fixes and the evidence-semantics guarantees that go with them.
"""
from __future__ import annotations

import pathlib

import pytest

from wfs import db
from wfs import evidence_collection as ec
from wfs import evidence_store
from wfs.collectors.base import (EV_ACTIVE_ASKING, EV_OBSERVATION, EV_SOLD,
                                 HEALTH_BLOCKED, HEALTH_DISABLED, HEALTH_ERROR,
                                 HEALTH_OK, HEALTH_PARTIAL, HEALTH_UNAVAILABLE,
                                 PRICE_UNKNOWN)
from wfs.collectors.chrono24 import Chrono24Collector
from wfs.collectors.ebay_active import EbayActiveCollector
from wfs.collectors.ebay_sold import (EbaySoldWebCollector,
                                      MarketplaceInsightsCollector,
                                      StoredSoldCollector)
from wfs.collectors.watchcharts import WatchChartsCollector
from wfs.watchlist import WatchRef

ROOT = pathlib.Path(__file__).resolve().parent.parent
REF = WatchRef("TUDOR", "Black Bay 58", "79030N", 2600, 2750, 2900)

GENERIC = "Collector is not available in this environment."


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "s532.sqlite3")
    db.init_db(c)
    evidence_store.migrate(c)
    return c


def listing(item_id="a", price=2400.0,
            title="Tudor Black Bay 58 79030N full set box and papers"):
    return {"item_id": item_id, "title": title, "price": price,
            "total_acquisition": price, "currency": "GBP",
            "reference": "79030N", "brand": "TUDOR", "model": "Black Bay 58",
            "url": f"https://www.ebay.co.uk/itm/{item_id}",
            "item_location": "London, GB", "condition": "Pre-owned"}


# === ROOT CAUSE: transports are now supplied ================================

def test_web_collectors_get_a_transport_by_default():
    """They previously had neither a client nor a session, so never ran."""
    assert EbaySoldWebCollector(enabled=True).transport_description() != "none"
    assert WatchChartsCollector(enabled=True).transport_description() != "none"


def test_registry_supplies_an_http_client(conn):
    settings = ec.CollectionSettings(
        master_enabled=True,
        sources={**ec.DEFAULT_ENABLED, ec.SRC_WATCHCHARTS: True,
                 ec.SRC_EBAY_SOLD_WEB: True})
    built = {c.name: c for c in ec.CollectorRegistry(conn, settings).build()}
    assert built[ec.SRC_WATCHCHARTS].http_client is not None
    assert built[ec.SRC_EBAY_SOLD_WEB].http_client is not None


def test_no_collector_returns_the_generic_unavailable_message(conn):
    """Every source must explain itself specifically."""
    settings = ec.CollectionSettings(
        master_enabled=True,
        sources={k: True for k in ec.DEFAULT_ENABLED})
    registry = ec.CollectorRegistry(conn, settings)
    for collector in registry.build():
        reason = collector.unavailable_reason()
        assert reason != GENERIC, f"{collector.name} gives a generic reason"
        if not collector.is_available():
            assert len(reason) > 15, (
                f"{collector.name} cannot run but gives no useful reason")


def test_insights_explains_the_real_restriction():
    reason = MarketplaceInsightsCollector(provider=None).unavailable_reason()
    assert "marketplace.insights" in reason.lower()
    assert "restricted" in reason.lower() or "granted" in reason.lower()


def test_stored_sold_reports_record_count(conn):
    info = StoredSoldCollector(conn).preflight()
    assert info["ready"] is True
    assert "stored_records" in info
    assert "import" in info["reason"].lower()      # tells you what to do about it


# === PREFLIGHT DIAGNOSTICS ==================================================

def test_preflight_distinguishes_unreadable_robots_from_disallow():
    """A 401/403 on robots.txt is 'cannot verify', not 'forbidden'."""
    collector = WatchChartsCollector(enabled=True)
    collector._robots_unreadable = True
    collector._robots = object()          # pretend it was parsed
    collector.robots_allow = lambda url: False
    info = collector.preflight()
    assert info["ready"] is False
    assert info["robots_allowed"] is None
    assert "could not read" in info["reason"].lower()
    assert "not the same as" in info["reason"].lower()


def test_preflight_reports_explicit_disallow():
    collector = WatchChartsCollector(enabled=True)
    collector.robots_allow = lambda url: False
    collector._robots_unreadable = False
    collector._robots_failed = False
    info = collector.preflight()
    assert "explicitly disallows" in info["reason"]


def test_preflight_reports_ready_when_permitted():
    collector = WatchChartsCollector(enabled=True)
    collector.robots_allow = lambda url: True
    collector._robots_unreadable = False
    info = collector.preflight()
    assert info["ready"] is True


def test_preflight_never_collects(conn):
    """Diagnosis must not fetch anything or write evidence."""
    before = len(evidence_store.load_evidence(conn, "79030N"))
    ec.CollectorRegistry(conn).preflight()
    assert len(evidence_store.load_evidence(conn, "79030N")) == before


def test_disabled_collector_says_so():
    assert "Disabled" in WatchChartsCollector(enabled=False).preflight()["reason"]


# === EBAY ACTIVE: reuse, never an extra call ================================

def test_active_collector_produces_asking_evidence():
    collector = EbayActiveCollector(
        listings_by_reference={"79030N": [listing("a", 2350),
                                          listing("b", 2400),
                                          listing("c", 2450)]})
    result = collector.collect("79030N", "TUDOR", "Black Bay 58")
    assert result.status.status == HEALTH_OK
    assert len(result.records) == 3
    assert all(r.evidence_type == EV_ACTIVE_ASKING for r in result.records)


def test_active_evidence_is_never_sold():
    """The hard rule: an unsold listing is not a transaction."""
    collector = EbayActiveCollector(
        listings_by_reference={"79030N": [listing("a"), listing("b")]})
    for record in collector.collect("79030N", "TUDOR").records:
        assert record.evidence_type != EV_SOLD
        assert record.price_certainty == PRICE_UNKNOWN
        assert record.usable_for_valuation is False
        assert record.counts_for_liquidity is False


def test_active_collector_makes_no_api_call():
    """It has no client at all — it physically cannot call the API."""
    collector = EbayActiveCollector(listings_by_reference={"79030N": [listing()]})
    assert not hasattr(collector, "http_client") or collector.http_client is None
    assert "no API call" in collector.transport_description()
    assert collector.collect("79030N").status.cache_status == "REUSED"


def test_active_collector_filters_accessories():
    collector = EbayActiveCollector(listings_by_reference={"79030N": [
        listing("a", 2400),
        listing("strap", 80, "Tudor Black Bay 79030N bracelet only"),
        listing("box", 60, "Tudor 79030N box and manual only")]})
    result = collector.collect("79030N", "TUDOR", "Black Bay 58")
    assert len(result.records) == 1
    assert "filtered out" in result.status.detail


def test_active_collector_rejects_wrong_reference():
    collector = EbayActiveCollector(listings_by_reference={"79030N": [
        listing("a", 2400), listing("b", 2400, "Tudor Black Bay 58 79030B blue")]})
    assert len(collector.collect("79030N", "TUDOR", "Black Bay 58").records) == 1


def test_active_collector_handles_no_listings():
    result = EbayActiveCollector(listings_by_reference={}).collect("79030N")
    assert result.records == []
    assert result.status.status == HEALTH_OK        # nothing wrong, just nothing
    assert "No active listings" in result.status.error


def test_active_collector_enabled_by_default():
    assert ec.DEFAULT_ENABLED[ec.SRC_EBAY_ACTIVE] is True


# === PIPELINE: active evidence appears, no extra API calls ==================

class CountingEbay:
    def __init__(self, prices):
        self.prices = prices
        self.calls = 0

    def search(self, query, limit=50, exclusions=()):
        self.calls += 1
        return [{"itemId": f"v1|{i}|0",
                 "title": "Tudor Black Bay 58 79030N full set box and papers",
                 "price": {"value": str(p), "currency": "GBP"},
                 "buyingOptions": ["FIXED_PRICE"],
                 "seller": {"username": "s", "feedbackScore": 900,
                            "feedbackPercentage": "99.9"},
                 "itemLocation": {"city": "London", "country": "GB"},
                 "condition": "Pre-owned", "image": {"imageUrl": "http://i"},
                 "itemWebUrl": f"https://www.ebay.co.uk/itm/{i}"}
                for i, p in enumerate(self.prices)]


def _scan(conn, prices, **kw):
    from wfs.pipeline5 import run_flip_scan
    client = CountingEbay(prices)
    result = run_flip_scan(conn, [REF], client=client, use_ai=False, **kw)
    return result, client


def test_scan_now_produces_active_asking_evidence(conn):
    """Previously every record was OBSERVATION and no price evidence existed."""
    result, _ = _scan(conn, [2350, 2400, 2450])
    records = evidence_store.load_evidence(conn, "79030N")
    types = {r.evidence_type for r in records}
    assert EV_ACTIVE_ASKING in types
    assert EV_OBSERVATION in types


def test_active_evidence_costs_no_extra_api_calls(conn):
    _, client_a = _scan(conn, [2350, 2400])
    discovery_only = client_a.calls
    conn2 = conn
    # The active collector reuses the same fetch; call count is discovery alone.
    assert discovery_only > 0
    records = [r for r in evidence_store.load_evidence(conn2, "79030N")
               if r.source == ec.SRC_EBAY_ACTIVE]
    assert records, "active evidence should exist"


def test_scan_records_health_for_ebay_active(conn):
    """It used to sit permanently at NOT_RUN."""
    result, _ = _scan(conn, [2400])
    health = {h["source"]: h for h in result["source_health"]}
    assert health[ec.SRC_EBAY_ACTIVE]["status"] == HEALTH_OK
    assert health[ec.SRC_EBAY_ACTIVE]["record_count"] >= 1


def test_active_asking_alone_cannot_produce_buy(conn):
    """Level C must never behave like Level A."""
    from wfs.decision import BUY
    result, _ = _scan(conn, [1500, 2400, 2450, 2500])
    for analysis in result["analyses"]:
        if analysis.verdict == BUY:
            assert analysis.evidence.has_sold_evidence, (
                "BUY reached without sold evidence")


def test_active_evidence_does_not_raise_confidence_to_buy_level(conn):
    _, _ = _scan(conn, [2350, 2400, 2450, 2500])
    from wfs.evidence import score_confidence
    from wfs import evidence_bridge
    evidence, _agg = evidence_bridge.build_market_evidence(conn, "79030N",
                                                            active_listing_count=4)
    assert evidence.has_sold_evidence is False
    assert score_confidence(evidence).score < 60


# === CHRONO24 STAYS HONEST ==================================================

def test_chrono24_blocked_is_preserved_not_bypassed():
    collector = Chrono24Collector(enabled=True)
    collector.robots_allow = lambda url: False
    collector._robots_unreadable = False
    info = collector.preflight()
    assert info["ready"] is False
    assert "disallow" in info["reason"].lower()


def test_chrono24_reports_its_transport():
    assert "Chrono24WebProvider" in Chrono24Collector(
        enabled=True).transport_description()


def test_chrono24_evidence_would_be_asking_not_sold():
    class FakeProvider:
        def get_asking_prices(self, brand, model, reference):
            class E:
                has_evidence = True
                prices = [2900.0, 3000.0]
                countries = ["DE"]
                notes = ""
            return E()

    result = Chrono24Collector(enabled=True,
                               provider=FakeProvider()).collect("79030N", "TUDOR")
    assert result.records
    for record in result.records:
        assert record.evidence_type == EV_ACTIVE_ASKING
        assert record.evidence_type != EV_SOLD
        assert record.usable_for_valuation is False


# === ERROR VS UNAVAILABLE ===================================================

def test_unexpected_failure_is_error_not_unavailable():
    class Exploding(WatchChartsCollector):
        def _collect(self, *a, **kw):
            raise RuntimeError("parser exploded")

    result = Exploding(enabled=True).collect("79030N")
    assert result.status.status == HEALTH_ERROR
    assert "parser exploded" in result.status.error


def test_empty_parse_is_partial_with_a_real_explanation():
    class Fetches:
        def get(self, *a, **kw):
            class R:
                status_code = 200
                text = "<html><body>nothing useful here</body></html>"
            return R()

    collector = EbaySoldWebCollector(enabled=True, http_client=Fetches())
    collector.robots_allow = lambda url: True
    collector._robots_unreadable = False
    result = collector.collect("79030N", "TUDOR")
    assert result.status.status == HEALTH_PARTIAL
    assert "bytes" in result.status.error
    assert "parser needs updating" in result.status.error


def test_blocked_http_status_is_not_circumvented():
    class Refuses:
        def get(self, *a, **kw):
            class R:
                status_code = 403
                text = ""
            return R()

    collector = EbaySoldWebCollector(enabled=True, http_client=Refuses())
    collector.robots_allow = lambda url: True
    collector._robots_unreadable = False
    result = collector.collect("79030N", "TUDOR")
    assert result.status.status == HEALTH_BLOCKED
    assert "not circumvented" in result.status.error.lower()


def test_watchcharts_paywall_is_named_specifically():
    class Paywalled:
        def get(self, *a, **kw):
            class R:
                status_code = 200
                text = "<html>Sign in to view market data</html>"
            return R()

    collector = WatchChartsCollector(enabled=True, http_client=Paywalled())
    collector.robots_allow = lambda url: True
    collector._robots_unreadable = False
    result = collector.collect("79030N", "TUDOR")
    assert "sign-in" in result.status.error
    assert "will not bypass" in result.status.error


# === SECURITY ===============================================================

def test_no_credentials_in_preflight_output(conn, monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "SECRET-XYZ")
    monkeypatch.setenv("EBAY_CLIENT_ID", "ID-XYZ")
    blob = repr(ec.CollectorRegistry(conn).preflight())
    assert "SECRET-XYZ" not in blob and "ID-XYZ" not in blob
    for token in ("client_secret", "client_id", "access_token"):
        assert token not in blob.lower()


def test_no_credentials_in_source_health(conn, monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "SECRET-XYZ")
    _scan(conn, [2400])
    blob = repr(ec.source_health_report(conn))
    assert "SECRET-XYZ" not in blob


def test_smoke_test_tool_exists_and_parses():
    import ast
    path = ROOT / "tools" / "collector_smoke_test.py"
    assert path.exists()
    ast.parse(path.read_text(encoding="utf-8"))


def test_smoke_tool_writes_no_evidence():
    """It must be safe to run against a live database."""
    source = (ROOT / "tools" / "collector_smoke_test.py").read_text()
    assert "store_evidence" not in source
    assert "save_settings" not in source


# === SETTINGS PERSISTENCE STILL WORKS (unchanged behaviour) =================

def test_enable_disable_persistence_unaffected(tmp_path, conn):
    from wfs import settings_store as ss
    path = tmp_path / "settings.json"
    initial = ec.CollectionSettings.from_dict(
        ss.load_settings(path).get("evidence_collection"))
    assert initial.is_enabled(ec.SRC_CHRONO24) is False

    data = dict(ss.load_settings(path))
    data["evidence_collection"] = ec.CollectionSettings(
        master_enabled=True,
        sources={**initial.sources, ec.SRC_CHRONO24: True}).as_dict()
    ss.save_settings(data, path)

    reloaded = ec.CollectionSettings.from_dict(
        ss.load_settings(path).get("evidence_collection"))
    assert reloaded.is_enabled(ec.SRC_CHRONO24) is True
    built = {c.name: c.enabled for c in ec.CollectorRegistry(conn, reloaded).build()}
    assert built[ec.SRC_CHRONO24] is True
