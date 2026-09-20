"""Phase 5.3 — automatic market evidence collection.

Every external source is mocked. No test touches a live website.
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from wfs import browser, db, evidence_bridge, evidence_store, fx
from wfs import evidence_collection as ec
from wfs.collectors import local_history
from wfs.collectors.base import (BEST_OFFER_UNCERTAIN, CONFIRMED_PRICE,
                                 DISPLAYED_SOLD_PRICE, EV_ACTIVE_ASKING,
                                 EV_MARKET_CONTEXT, EV_OBSERVATION, EV_SOLD,
                                 HEALTH_BLOCKED, HEALTH_DISABLED, HEALTH_OK,
                                 MATCH_AMBIGUOUS, MATCH_EXACT, MATCH_FAMILY,
                                 MATCH_NORMALIZED, MATCH_REJECTED, PRICE_UNKNOWN,
                                 CollectedEvidence, CollectionResult,
                                 classify_match, days_since, normalise_reference,
                                 recency_band, recency_weight)
from wfs.collectors.chrono24 import Chrono24Collector
from wfs.collectors.ebay_sold import (EbaySoldWebCollector,
                                      MarketplaceInsightsCollector,
                                      StoredSoldCollector)
from wfs.collectors.local_history import LocalHistoryCollector
from wfs.collectors.watchcharts import WatchChartsCollector
from wfs.config5 import EVIDENCE_COLLECTED_SOLD, EVIDENCE_SEED
from wfs.decision import (BUY, GATE_INSUFFICIENT_SOLD_EVIDENCE,
                          GATE_MARGIN_TOO_SMALL, PASS, WATCH)
from wfs.evidence import score_confidence
from wfs.manual_evidence import import_csv_text
from wfs.watchlist import WatchRef

ROOT = pathlib.Path(__file__).resolve().parent.parent
REF = WatchRef("TUDOR", "Black Bay 58", "79030N", 2600, 2750, 2900)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "p53.sqlite3")
    db.init_db(c)
    evidence_store.migrate(c)
    return c


def ev(price=2700.0, certainty=CONFIRMED_PRICE, match=MATCH_EXACT,
       date="2026-09-01", source="test", etype=EV_SOLD, item_id=None, **kw):
    return CollectedEvidence(
        source=source, evidence_type=etype, reference="79030N",
        price_gbp=price, original_price=price, sale_date=date,
        price_certainty=certainty, match_type=match,
        item_id=item_id or f"t{price}{date}", **kw)


# === REFERENCE NORMALISATION & MATCHING (§10) ===============================

def test_normalisation_strips_separators():
    assert normalise_reference("l3.781.4.56.6") == normalise_reference("L3 781 4 56 6")
    assert normalise_reference("79030-N") == "79030N"


def test_exact_reference_match():
    assert classify_match("Tudor Black Bay 58 79030N full set", "79030N") == MATCH_EXACT


def test_normalised_reference_match():
    assert classify_match("Longines L3 781 4 56 6 diver", "L3.781.4.56.6") == \
        MATCH_NORMALIZED


def test_model_family_match_when_reference_absent():
    result = classify_match("Tudor Black Bay 58 automatic", "79030N",
                            model="Black Bay 58", brand="TUDOR")
    assert result == MATCH_FAMILY


def test_different_variant_is_rejected_not_merged():
    """79030N and 79030B are different watches, however similar the names."""
    assert classify_match("Tudor Black Bay 58 79030B blue", "79030N") == MATCH_REJECTED


def test_unrelated_listing_rejected():
    assert classify_match("Casio F91W digital", "79030N") == MATCH_REJECTED


def test_family_match_scores_below_exact():
    from wfs.collectors.base import MATCH_WEIGHT
    assert MATCH_WEIGHT[MATCH_FAMILY] < MATCH_WEIGHT[MATCH_EXACT]


def test_ambiguous_and_rejected_carry_no_weight():
    from wfs.collectors.base import MATCH_WEIGHT
    assert MATCH_WEIGHT[MATCH_AMBIGUOUS] == 0.0
    assert MATCH_WEIGHT[MATCH_REJECTED] == 0.0


# === BEST OFFER (§6) ========================================================

def test_best_offer_contributes_nothing_to_valuation():
    record = ev(price=2700.0, certainty=BEST_OFFER_UNCERTAIN)
    assert record.usable_for_valuation is False
    assert record.valuation_weight == 0.0


def test_best_offer_still_counts_for_liquidity():
    """It proves the watch sold, even though the price is not disclosed."""
    record = ev(price=None, certainty=BEST_OFFER_UNCERTAIN)
    assert record.counts_for_liquidity is True


def test_confirmed_price_outweighs_displayed_price():
    assert ev(certainty=CONFIRMED_PRICE).valuation_weight > \
        ev(certainty=DISPLAYED_SOLD_PRICE).valuation_weight


def test_price_unknown_counts_for_nothing():
    record = ev(price=None, certainty=PRICE_UNKNOWN)
    assert record.usable_for_valuation is False
    assert record.counts_for_liquidity is False


def test_best_offer_excluded_from_price_band_but_counted_in_liquidity(conn):
    records = [ev(price=2700, item_id="a"), ev(price=2720, item_id="b"),
               ev(price=2680, item_id="c"),
               ev(price=9999, certainty=BEST_OFFER_UNCERTAIN, item_id="d")]
    evidence_store.store_evidence(conn, records)
    agg = evidence_store.aggregate(conn, "79030N")
    assert agg.best_offer_count == 1
    assert len(agg.priced_sales) == 3
    assert len(agg.liquidity_sales) == 4
    _, mid, _ = agg.robust_band()
    assert mid is not None and mid < 3000     # the 9999 never reaches valuation


def test_web_parser_flags_best_offer():
    collector = EbaySoldWebCollector(enabled=True)
    html = ('<li class="s-item"><div class="s-item__title">Tudor Black Bay 58 '
            '79030N</div><span>£2,700.00</span><span>Best offer accepted</span></li>')
    records = collector.parse(html, "79030N", "TUDOR", "Black Bay 58")
    assert len(records) == 1
    assert records[0].price_certainty == BEST_OFFER_UNCERTAIN
    assert records[0].price_gbp is None
    assert records[0].counts_for_liquidity is True


def test_web_parser_normal_sale_is_priced():
    collector = EbaySoldWebCollector(enabled=True)
    html = ('<li class="s-item"><div class="s-item__title">Tudor Black Bay 58 '
            '79030N</div><span>£2,700.00</span></li>')
    records = collector.parse(html, "79030N", "TUDOR", "Black Bay 58")
    assert records[0].price_certainty == DISPLAYED_SOLD_PRICE
    assert records[0].price_gbp == 2700.0


# === PRICE & DATE PARSING ===================================================

def test_price_parsing_handles_thousands_separator():
    collector = EbaySoldWebCollector(enabled=True)
    html = ('<li class="s-item"><div class="s-item__title">Tudor 79030N</div>'
            '<span>£12,450.00</span></li>')
    assert collector.parse(html, "79030N", "TUDOR")[0].price_gbp == 12450.0


def test_implausible_prices_discarded():
    collector = EbaySoldWebCollector(enabled=True)
    html = ('<li class="s-item"><div class="s-item__title">Tudor 79030N</div>'
            '<span>£12.00</span></li>')
    assert collector.parse(html, "79030N", "TUDOR") == []


def test_date_parsing_and_age():
    assert days_since("2020-01-01") > 1000
    assert days_since(None) is None
    assert days_since("not-a-date") is None


# === RECENCY (§13) ==========================================================

def test_recency_bands_descend():
    from datetime import datetime, timedelta, timezone
    def ago(days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    assert recency_weight(ago(10)) == 1.00
    assert recency_weight(ago(60)) == 0.85
    assert recency_weight(ago(120)) == 0.60
    assert recency_weight(ago(300)) == 0.35
    assert recency_weight(ago(500)) == 0.15


def test_recency_band_labels():
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    assert recency_band(recent) == "0-30 days"
    assert recency_band(None) == "undated"


def test_old_and_new_evidence_not_blindly_averaged(conn):
    from datetime import datetime, timedelta, timezone
    def ago(days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    # Three recent sales at ~2700, three old ones at ~2000.
    records = [ev(price=2700 + i, date=ago(10), item_id=f"new{i}") for i in range(3)]
    records += [ev(price=2000 + i, date=ago(400), item_id=f"old{i}") for i in range(3)]
    evidence_store.store_evidence(conn, records)
    agg = evidence_store.aggregate(conn, "79030N", max_age_days=None)
    _, mid, _ = agg.robust_band()
    plain_mean = sum(r.price_gbp for r in agg.priced_sales) / 6
    assert mid > plain_mean      # recent sales dominate


# === DEDUPLICATION (§12) ====================================================

def test_same_item_id_never_counted_twice(conn):
    record = ev(item_id="v1|12345|0")
    first = evidence_store.store_evidence(conn, [record])
    second = evidence_store.store_evidence(conn, [record])
    assert first["inserted"] == 1
    assert second["inserted"] == 0 and second["refreshed"] == 1
    assert len(evidence_store.load_evidence(conn, "79030N")) == 1


def test_recollection_refreshes_rather_than_duplicates(conn):
    evidence_store.store_evidence(conn, [ev(price=2700, item_id="x")])
    evidence_store.store_evidence(conn, [ev(price=2750, item_id="x")])
    records = evidence_store.load_evidence(conn, "79030N")
    assert len(records) == 1
    assert records[0].price_gbp == 2750.0


def test_fingerprint_prefers_item_id_then_url():
    assert "id:" in ev(item_id="abc").fingerprint()
    no_id = CollectedEvidence(source="s", evidence_type=EV_SOLD,
                              reference="79030N",
                              listing_url="https://x/itm/1?utm=a")
    assert "url:" in no_id.fingerprint()
    assert "?" not in no_id.fingerprint()


def test_content_fingerprint_when_no_id_or_url():
    rec = CollectedEvidence(source="s", evidence_type=EV_SOLD, reference="79030N",
                            title="Tudor", price_gbp=2700, sale_date="2026-09-01")
    assert "fp:" in rec.fingerprint()


# === OUTLIERS & STATISTICS (§14) ============================================

def test_outliers_trimmed_from_band(conn):
    prices = [2650, 2680, 2700, 2720, 2750, 60, 90000]
    evidence_store.store_evidence(
        conn, [ev(price=p, item_id=f"o{i}") for i, p in enumerate(prices)])
    low, mid, high = evidence_store.aggregate(conn, "79030N").robust_band()
    assert 2500 < mid < 2900
    assert low > 2000 and high < 5000


def test_band_is_none_without_priced_sales(conn):
    evidence_store.store_evidence(
        conn, [ev(price=None, certainty=BEST_OFFER_UNCERTAIN, item_id="b")])
    assert evidence_store.aggregate(conn, "79030N").robust_band() == (None, None, None)


def test_effective_sample_discounts_weak_evidence(conn):
    evidence_store.store_evidence(conn, [
        ev(price=2700, item_id="strong"),
        ev(price=2700, match=MATCH_FAMILY, item_id="weak")])
    agg = evidence_store.aggregate(conn, "79030N")
    assert agg.effective_sample < 2.0     # two records, less than two units of weight


def test_exact_and_family_counts_kept_separate(conn):
    evidence_store.store_evidence(conn, [
        ev(item_id="e1"), ev(item_id="e2"),
        ev(match=MATCH_FAMILY, item_id="f1")])
    agg = evidence_store.aggregate(conn, "79030N")
    assert len(agg.exact_sales) == 2
    assert len(agg.family_sales) == 1


# === MIGRATION (§11) ========================================================

def test_migration_creates_tables_and_is_idempotent(tmp_path):
    c = db.connect(tmp_path / "m.sqlite3")
    db.init_db(c)
    added = evidence_store.migrate(c)
    assert "collected_evidence" in added and "source_health" in added
    assert evidence_store.migrate(c) == []


def test_migration_preserves_existing_data(tmp_path):
    """The whole point: no existing row may be lost."""
    c = db.connect(tmp_path / "pre.sqlite3")
    db.init_db(c)
    db.upsert_listing(c, {"item_id": "keep", "title": "Tudor 79030N",
                          "price": 2400.0, "reference": "79030N"})
    import_csv_text(c, "brand,reference,model,sold_price_gbp,sold_date,condition,"
                       "full_set,source,notes\nTudor,79030N,BB58,2680,2026-08-14,"
                       "good,yes,eBay UK,\n")
    before_listings = c.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"]
    before_manual = c.execute(
        "SELECT COUNT(*) c FROM manual_sold_evidence").fetchone()["c"]

    evidence_store.migrate(c)

    assert c.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"] == before_listings
    assert c.execute("SELECT COUNT(*) c FROM manual_sold_evidence"
                     ).fetchone()["c"] == before_manual


def test_indexes_created(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert "idx_evidence_ref" in names


# === COLLECTORS =============================================================

def test_stored_sold_collector_reads_manual_records(conn):
    import_csv_text(conn, "brand,reference,model,sold_price_gbp,sold_date,condition,"
                          "full_set,source,notes\n"
                          "Tudor,79030N,BB58,2680,2026-08-14,good,yes,eBay UK,\n")
    result = StoredSoldCollector(conn).collect("79030N", "TUDOR", "Black Bay 58")
    assert result.status.status == HEALTH_OK
    assert len(result.records) == 1
    assert result.records[0].price_certainty == CONFIRMED_PRICE


def test_insights_collector_unavailable_without_access():
    result = MarketplaceInsightsCollector(provider=None).collect("79030N")
    assert result.records == []
    assert result.status.status in ("UNAVAILABLE", "DISABLED")


def test_insights_collector_used_when_access_granted():
    class FakeProvider:
        enabled = True
        def get_recent_sales(self, reference, country="GB", period_days=90):
            class E:
                has_evidence = True
                prices = [2700.0, 2720.0]
                notes = ""
            return E()

    result = MarketplaceInsightsCollector(
        provider=FakeProvider(), enabled=True).collect("79030N", "TUDOR")
    assert len(result.records) == 2
    assert all(r.price_certainty == CONFIRMED_PRICE for r in result.records)


def test_collectors_disabled_by_default():
    assert Chrono24Collector().collect("79030N").status.status == HEALTH_DISABLED
    assert WatchChartsCollector().collect("79030N").status.status == HEALTH_DISABLED
    assert EbaySoldWebCollector().collect("79030N").status.status == HEALTH_DISABLED


def test_local_history_enabled_by_default(conn):
    assert LocalHistoryCollector(conn).enabled is True
    assert ec.DEFAULT_ENABLED[ec.SRC_LOCAL_HISTORY] is True
    assert ec.DEFAULT_ENABLED[ec.SRC_CHRONO24] is False


# === FAILURE HANDLING (§19) =================================================

def test_collector_failure_never_raises():
    class Exploding(EbaySoldWebCollector):
        def _collect(self, *a, **kw):
            raise RuntimeError("network on fire")

    result = Exploding(enabled=True, http_client=object()).collect("79030N")
    assert result.records == []
    assert "network on fire" in result.status.error


def test_blocking_response_is_reported_not_circumvented():
    class Blocked:
        def get(self, *a, **kw):
            class R:
                status_code = 403
                text = ""
            return R()

    collector = EbaySoldWebCollector(enabled=True, http_client=Blocked())
    collector.robots_allow = lambda url: True
    result = collector.collect("79030N", "TUDOR")
    assert result.status.status == HEALTH_BLOCKED
    assert "403" in result.status.error
    assert "not circumvented" in result.status.error.lower()


def test_robots_disallow_blocks_collection():
    collector = EbaySoldWebCollector(enabled=True, http_client=object())
    collector.robots_allow = lambda url: False
    result = collector.collect("79030N", "TUDOR")
    assert result.status.status == HEALTH_BLOCKED
    assert "robots.txt" in result.status.error


def test_unreadable_robots_means_no_fetch():
    """Not being able to check permission is not the same as having it."""
    collector = EbaySoldWebCollector(enabled=True)
    collector._robots_failed = True
    assert collector.robots_allow("https://www.ebay.co.uk/x") is False


def test_chrono24_unavailable_does_not_break_collection(conn):
    class FailingProvider:
        def get_asking_prices(self, brand, model, reference):
            class E:
                has_evidence = False
                notes = "Lookup unavailable: network down"
            return E()

    result = Chrono24Collector(enabled=True,
                               provider=FailingProvider()).collect("79030N")
    assert result.records == []
    assert result.status.is_healthy is False


def test_watchcharts_unavailable_is_labelled(conn):
    class Refused:
        def get(self, *a, **kw):
            class R:
                status_code = 403
                text = ""
            return R()

    collector = WatchChartsCollector(enabled=True, http_client=Refused())
    collector.robots_allow = lambda url: True
    result = collector.collect("79030N", "TUDOR")
    assert "WATCHCHARTS_UNAVAILABLE" in result.status.error


def test_watchcharts_paywall_yields_nothing():
    collector = WatchChartsCollector(enabled=True)
    assert collector.parse("<div>Sign in to view market data</div>", "79030N") == []


def test_one_failing_source_does_not_stop_others(conn):
    class Boom:
        name = "boom"
        enabled = True
        def collect(self, reference, brand="", model="", **kw):
            return CollectionResult.unavailable("boom", reference, "down")

    good = StoredSoldCollector(conn)
    import_csv_text(conn, "brand,reference,model,sold_price_gbp,sold_date,condition,"
                          "full_set,source,notes\n"
                          "Tudor,79030N,BB58,2680,2026-08-14,good,yes,eBay UK,\n")
    registry = ec.CollectorRegistry(conn, collectors=[Boom(), good])
    records = registry.collect_for_reference("79030N", "TUDOR")
    assert len(records) == 1       # the healthy source still delivered


# === PLAYWRIGHT (§21) =======================================================

def test_browser_status_never_raises():
    status = browser.status()
    assert set(status) >= {"installed", "enabled", "usable"}


def test_browser_disabled_by_default(monkeypatch):
    monkeypatch.delenv("WFS53_BROWSER_ENABLED", raising=False)
    assert browser.is_enabled() is False


def test_browser_session_is_safe_when_unavailable(monkeypatch):
    monkeypatch.setattr(browser, "is_available", lambda: False)
    with browser.BrowserSession() as session:
        assert session.available is False
        result = session.fetch("https://example.com")
        assert result.ok is False


def test_app_functions_without_playwright(monkeypatch, conn):
    """The whole pipeline must work with Playwright absent."""
    monkeypatch.setattr(browser, "is_available", lambda: False)
    registry = ec.CollectorRegistry(conn)
    run = registry.collect_for_references([("79030N", "TUDOR", "Black Bay 58")])
    assert run is not None


def test_playwright_is_not_a_hard_dependency():
    text = (ROOT / "pyproject.toml").read_text()
    dependencies = text.split("[project.optional-dependencies]")[0]
    assert "playwright" not in dependencies.lower()


# === FX (§7) ================================================================

def test_fx_converts_known_currency():
    assert fx.to_gbp(1170.0, "EUR") == pytest.approx(1000.0, abs=1.0)
    assert fx.to_gbp(1000.0, "GBP") == 1000.0


def test_fx_returns_none_for_unknown_currency():
    """A guessed rate is worse than no number."""
    assert fx.to_gbp(1000.0, "JPY") is None


def test_fx_override_from_environment(monkeypatch):
    monkeypatch.setenv("WFS53_FX_EUR", "2.0")
    assert fx.to_gbp(1000.0, "EUR") == 500.0


# === CACHE (§22) ============================================================

def test_second_collection_uses_cache(conn):
    import_csv_text(conn, "brand,reference,model,sold_price_gbp,sold_date,condition,"
                          "full_set,source,notes\n"
                          "Tudor,79030N,BB58,2680,2026-08-14,good,yes,eBay UK,\n")
    registry = ec.CollectorRegistry(conn)
    first = registry.collect_for_references([("79030N", "TUDOR", "Black Bay 58")])
    second = registry.collect_for_references([("79030N", "TUDOR", "Black Bay 58")])
    assert second.cache_hits > first.cache_hits


def test_force_refresh_bypasses_cache(conn):
    import_csv_text(conn, "brand,reference,model,sold_price_gbp,sold_date,condition,"
                          "full_set,source,notes\n"
                          "Tudor,79030N,BB58,2680,2026-08-14,good,yes,eBay UK,\n")
    registry = ec.CollectorRegistry(conn)
    registry.collect_for_references([("79030N", "TUDOR", "Black Bay 58")])
    refreshed = registry.collect_for_references(
        [("79030N", "TUDOR", "Black Bay 58")], force_refresh=True)
    assert refreshed.cache_hits == 0


def test_shortlist_cap_limits_collection(conn):
    settings = ec.CollectionSettings(shortlist_cap=2)
    registry = ec.CollectorRegistry(conn, settings)
    targets = [(f"REF{i}", "TUDOR", "Model") for i in range(10)]
    run = registry.collect_for_references(targets)
    assert len(run.references) == 2


# === LOCAL HISTORY (§9) =====================================================

def _add(conn, item_id, price, reference="79030N"):
    db.upsert_listing(conn, {
        "item_id": item_id, "title": "Tudor Black Bay 58 79030N",
        "price": price, "shipping": 0.0, "total_acquisition": price,
        "reference": reference, "brand": "TUDOR", "model": "Black Bay 58"})


def test_disappearance_is_ended_unknown_not_sold(conn):
    """The rule that matters: vanishing is not selling."""
    _add(conn, "a", 2700)
    _add(conn, "a", 2700)
    db.mark_absent_listings(conn, ["79030N"], set())
    histories = local_history.listing_histories(conn, "79030N")
    assert histories[0].status == local_history.ENDED_UNKNOWN
    assert histories[0].status != local_history.CONFIRMED_SOLD


def test_confirmed_sold_requires_corroboration(conn):
    import_csv_text(conn, "brand,reference,model,sold_price_gbp,sold_date,condition,"
                          "full_set,source,notes\n"
                          "Tudor,79030N,BB58,2700,2026-09-10,good,yes,eBay UK,\n")
    _add(conn, "b", 2700)
    _add(conn, "b", 2700)
    db.mark_absent_listings(conn, ["79030N"], set())
    histories = local_history.listing_histories(conn, "79030N")
    statuses = {h.status for h in histories}
    # Corroborated by a matching sold record on a nearby date.
    assert local_history.CONFIRMED_SOLD in statuses or \
        local_history.ENDED_UNKNOWN in statuses
    confirmed = [h for h in histories if h.status == local_history.CONFIRMED_SOLD]
    for h in confirmed:
        assert h.confirmed_by


def test_price_mismatch_does_not_confirm_a_sale(conn):
    import_csv_text(conn, "brand,reference,model,sold_price_gbp,sold_date,condition,"
                          "full_set,source,notes\n"
                          "Tudor,79030N,BB58,1000,2026-09-10,good,yes,eBay UK,\n")
    _add(conn, "c", 2700)
    _add(conn, "c", 2700)
    db.mark_absent_listings(conn, ["79030N"], set())
    histories = local_history.listing_histories(conn, "79030N")
    assert all(h.status != local_history.CONFIRMED_SOLD for h in histories)


def test_long_running_active_listing_becomes_stale(conn):
    from datetime import datetime, timedelta, timezone
    _add(conn, "d", 2700)
    _add(conn, "d", 2700)
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    conn.execute("UPDATE listings SET first_seen = ? WHERE item_id = 'd'", (old,))
    conn.commit()
    histories = local_history.listing_histories(conn, "79030N")
    assert histories[0].status == local_history.STALE


def test_history_tracks_price_lifecycle(conn):
    _add(conn, "e", 2800)
    _add(conn, "e", 2700)
    _add(conn, "e", 2600)
    h = local_history.listing_histories(conn, "79030N")[0]
    assert h.initial_price == 2800
    assert h.latest_price == 2600
    assert h.lowest_price_seen == 2600
    assert h.price_change_count == 2
    assert h.total_drop_pct < 0


def test_market_history_language_is_honest(conn):
    _add(conn, "f", 2700)
    _add(conn, "f", 2700)
    db.mark_absent_listings(conn, ["79030N"], set())
    text = local_history.market_history(conn, "79030N").describe()
    assert "not a sale" in text.lower()


def test_local_collector_emits_observations_not_sales(conn):
    _add(conn, "g", 2700)
    _add(conn, "g", 2700)
    db.mark_absent_listings(conn, ["79030N"], set())
    result = LocalHistoryCollector(conn).collect("79030N", "TUDOR", "Black Bay 58")
    assert all(r.evidence_type == EV_OBSERVATION for r in result.records)


# === VALUATION BRIDGE (§14) =================================================

def test_collected_sold_evidence_feeds_valuation(conn):
    evidence_store.store_evidence(
        conn, [ev(price=2650 + i * 20, item_id=f"v{i}") for i in range(8)])
    evidence, agg = evidence_bridge.build_market_evidence(conn, "79030N",
                                                          active_listing_count=3)
    assert evidence.has_sold_evidence
    assert evidence.sold_price_sample_size == 8
    assert score_confidence(evidence).score >= 60


def test_verified_source_outranks_collected(conn):
    evidence_store.store_evidence(
        conn, [ev(price=2700, source="ebay_sold_stored", item_id="m1"),
               ev(price=2700, source="ebay_sold_stored", item_id="m2")])
    evidence, _ = evidence_bridge.build_market_evidence(conn, "79030N")
    from wfs.config5 import EVIDENCE_REFERENCE_EXACT
    assert evidence.evidence_level == EVIDENCE_REFERENCE_EXACT


def test_web_collected_uses_its_own_tier(conn):
    evidence_store.store_evidence(
        conn, [ev(price=2700, source="ebay_sold_web", certainty=DISPLAYED_SOLD_PRICE,
                  item_id=f"w{i}") for i in range(4)])
    evidence, _ = evidence_bridge.build_market_evidence(conn, "79030N")
    assert evidence.evidence_level == EVIDENCE_COLLECTED_SOLD
    assert score_confidence(evidence).score <= 85


def test_seed_used_only_when_nothing_collected(conn):
    evidence, _ = evidence_bridge.build_market_evidence(conn, "79030N",
                                                        seed_mid=2750)
    assert evidence.evidence_level == EVIDENCE_SEED
    assert evidence.unverified is True
    assert evidence.has_sold_evidence is False


def test_asking_only_when_no_sales(conn):
    evidence_store.store_evidence(conn, [
        ev(price=2900, etype=EV_ACTIVE_ASKING, certainty=PRICE_UNKNOWN,
           item_id=f"a{i}") for i in range(3)])
    evidence, _ = evidence_bridge.build_market_evidence(conn, "79030N")
    assert evidence.has_sold_evidence is False
    assert score_confidence(evidence).score <= 45


def test_liquidity_facts_separate_exact_and_family(conn):
    evidence_store.store_evidence(conn, [
        ev(item_id="x1"), ev(item_id="x2"),
        ev(match=MATCH_FAMILY, item_id="fam1")])
    agg = evidence_store.aggregate(conn, "79030N")
    facts = evidence_bridge.liquidity_facts(agg, active_supply=4)
    assert facts["confirmed_sales_90d"] == 2
    assert facts["family_sales_90d"] == 1
    assert facts["active_uk_supply"] == 4


def test_liquidity_reports_insufficient_when_thin(conn):
    evidence_store.store_evidence(conn, [ev(item_id="only")])
    agg = evidence_store.aggregate(conn, "79030N")
    assert evidence_bridge.liquidity_facts(agg, 2)["status"] == "INSUFFICIENT DATA"


# === VERDICT GATING (§18) ===================================================

class FakeEbay:
    def __init__(self, prices):
        self.prices = prices

    def search(self, query, limit=50, exclusions=()):
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
    return run_flip_scan(conn, [REF], client=FakeEbay(prices), use_ai=False, **kw)


def test_seed_alone_cannot_produce_buy(conn):
    result = _scan(conn, [1200, 2400, 2500])
    assert all(a.verdict != BUY for a in result["analyses"])


def test_collected_evidence_enables_buy(conn):
    import_csv_text(conn, "brand,reference,model,sold_price_gbp,sold_date,condition,"
                          "full_set,source,notes\n"
                    + "".join(f"Tudor,79030N,BB58,{2650 + i * 15},2026-09-{1 + i:02d},"
                              f"good,yes,eBay UK,\n" for i in range(9)))
    result = _scan(conn, [1900, 2450, 2500])
    verdicts = [a.verdict for a in result["analyses"]]
    assert BUY in verdicts


def test_gate_codes_reported(conn):
    result = _scan(conn, [2400])
    analysis = result["analyses"][0]
    assert analysis.decision.gates
    assert all(isinstance(g, str) for g in analysis.decision.gates)


def test_insufficient_evidence_gate_present(conn):
    result = _scan(conn, [1200])
    gates = result["analyses"][0].decision.gates
    assert GATE_INSUFFICIENT_SOLD_EVIDENCE in gates


def test_asking_prices_alone_cannot_produce_buy(conn):
    evidence_store.store_evidence(conn, [
        ev(price=3500, etype=EV_ACTIVE_ASKING, certainty=PRICE_UNKNOWN,
           item_id=f"ask{i}") for i in range(5)])
    result = _scan(conn, [1500])
    assert all(a.verdict != BUY for a in result["analyses"])


def test_best_offer_only_evidence_cannot_produce_buy(conn):
    """Sales with undisclosed prices prove demand but cannot value the watch."""
    evidence_store.store_evidence(conn, [
        ev(price=None, certainty=BEST_OFFER_UNCERTAIN, item_id=f"bo{i}")
        for i in range(10)])
    result = _scan(conn, [1500])
    assert all(a.verdict != BUY for a in result["analyses"])


# === PIPELINE INTEGRATION ===================================================

def test_scan_collects_evidence_once_per_reference(conn):
    result = _scan(conn, [1900, 2400, 2450, 2500])
    assert result["stats"].evidence_references == 1     # not once per listing


def test_scan_reports_source_health(conn):
    result = _scan(conn, [2400])
    health = {h["source"]: h for h in result["source_health"]}
    assert ec.SRC_LOCAL_HISTORY in health
    assert ec.SRC_CHRONO24 in health


def test_scan_survives_total_collection_failure(conn, monkeypatch):
    class Exploding:
        def __init__(self, *a, **kw): pass
        def collect_for_references(self, *a, **kw):
            raise RuntimeError("collector subsystem down")

    from wfs import pipeline5
    # Even a catastrophic registry failure must not lose the scan.
    try:
        result = _scan(conn, [2400], registry=Exploding())
    except RuntimeError:
        pytest.fail("A collection failure must not propagate out of the scan")
    assert result["analyses"]


def test_collection_can_be_disabled(conn):
    result = _scan(conn, [2400], collect_evidence=False)
    assert result["stats"].evidence_references == 0
    assert result["analyses"]


def test_existing_data_survives_a_scan(conn):
    db.upsert_listing(conn, {"item_id": "pre-existing", "title": "old",
                             "price": 1.0, "reference": "OTHER"})
    before = conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"]
    _scan(conn, [2400])
    after = conn.execute(
        "SELECT COUNT(*) c FROM listings WHERE item_id = 'pre-existing'"
    ).fetchone()["c"]
    assert after == 1 and before >= 1


# === SETTINGS PERSISTENCE (§20) =============================================

def test_collection_settings_round_trip(tmp_path):
    from wfs.settings_store import load_settings, save_settings
    path = tmp_path / "s.json"
    settings = ec.CollectionSettings(
        master_enabled=True,
        sources={**ec.DEFAULT_ENABLED, ec.SRC_CHRONO24: True},
        shortlist_cap=7)
    save_settings({"seller_profile": "UK_PRIVATE", "overrides": {},
                   "evidence_collection": settings.as_dict()}, path)
    restored = ec.CollectionSettings.from_dict(
        load_settings(path)["evidence_collection"])
    assert restored.is_enabled(ec.SRC_CHRONO24) is True
    assert restored.shortlist_cap == 7


def test_master_switch_disables_everything():
    settings = ec.CollectionSettings(master_enabled=False)
    assert settings.is_enabled(ec.SRC_LOCAL_HISTORY) is False


def test_unknown_source_keys_ignored():
    settings = ec.CollectionSettings.from_dict({"sources": {"evil": True}})
    assert "evil" not in settings.sources


# === SECURITY (§26) =========================================================

def test_no_secrets_in_collected_evidence(conn, monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "TOP-SECRET-VALUE")
    evidence_store.store_evidence(conn, [ev(item_id="s1")])
    records = evidence_store.load_evidence(conn, "79030N")
    blob = repr([r.as_dict() for r in records])
    assert "TOP-SECRET-VALUE" not in blob


def test_source_health_contains_no_credentials(conn):
    report = ec.source_health_report(conn)
    blob = repr(report).lower()
    for token in ("secret", "token", "client_id", "password"):
        assert token not in blob


def test_collector_modules_reference_no_credentials():
    import ast as _ast
    for path in (ROOT / "wfs" / "collectors").glob("*.py"):
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Name):
                lowered = node.id.lower()
                assert "secret" not in lowered and "client_secret" not in lowered


# === ARCHITECTURE ===========================================================

def test_collectors_do_not_import_legacy_pricing():
    import ast as _ast
    paths = list((ROOT / "wfs" / "collectors").glob("*.py"))
    paths += [ROOT / "wfs" / "evidence_store.py",
              ROOT / "wfs" / "evidence_bridge.py",
              ROOT / "wfs" / "evidence_collection.py"]
    for path in paths:
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                assert (node.module or "").split(".")[-1] != "pricing", path.name


def test_economics_engine_still_authoritative(conn):
    from wfs.economics import UK_PRIVATE, get_profile
    assert get_profile(UK_PRIVATE).transaction_fee_pct == 0.0
    result = _scan(conn, [2400])
    assert result["analyses"][0].seller_mode == "UK Private Seller"


def test_version_is_at_least_530():
    import tomllib
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    parts = tuple(int(x) for x in data["project"]["version"].split("."))
    assert parts >= (5, 3, 0)
