from __future__ import annotations

import pytest

from wfs import db
from wfs.config import required_spread
from wfs.ebay import normalise_item
from wfs.pipeline import run_scan
from wfs.prefilter import effective_ratio_threshold, evaluate
from wfs.watchlist import (WatchRef, build_queries, looks_like_accessory,
                           match_reference, normalise)


@pytest.fixture
def refs():
    return [
        WatchRef("TUDOR", "Black Bay 58", "79030N", 2150, 2400, 2650),
        WatchRef("TUDOR", "Heritage Black Bay", "79230N", 2150, 2400, 2650),
        WatchRef("LONGINES", "HydroConquest", "L3.781.4.56.6", 980, 1120, 1260),
        WatchRef("RADO", "Captain Cook", "R32505203", 950, 1100, 1250),
    ]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.sqlite3")
    db.init_db(c)
    return c


def listing(**kw):
    base = {
        "item_id": "v1|1|0", "title": "Tudor Black Bay 58 79030N full set",
        "price": 1800.0, "shipping": 0.0, "total_acquisition": 1800.0,
        "currency": "GBP", "buying_format": "BUY_IT_NOW",
    }
    base.update(kw)
    return base


# --- reference parsing ------------------------------------------------------

def test_normalise_strips_separators():
    assert normalise("l3.781.4.56.6") == normalise("L3 781 4 56 6") == "L37814566"


def test_match_reference_finds_dotted_longines(refs):
    r = match_reference("Longines HydroConquest L3.781.4.56.6 ceramic 43mm", refs)
    assert r is not None and r.reference == "L3.781.4.56.6"


def test_match_reference_prefers_longest(refs):
    r = match_reference("Tudor 79230N Black Bay", refs)
    assert r.reference == "79230N"


def test_match_reference_none_for_unrelated(refs):
    assert match_reference("Casio F91W digital", refs) is None


def test_build_queries_produces_variants(refs):
    qs = build_queries(refs[0])
    assert "TUDOR 79030N" in qs
    assert len(qs) == len(set(q.lower() for q in qs))


def test_accessory_exclusion():
    assert looks_like_accessory("Tudor Black Bay 79030N bezel insert", "TUDOR")
    assert not looks_like_accessory("Tudor Black Bay 58 79030N 2022", "TUDOR")


# --- price / spread calculation ---------------------------------------------

def test_required_spread_bands():
    assert required_spread(750) == 200.0
    assert required_spread(1500) == 325.0
    assert required_spread(3000) == 400.0
    assert required_spread(5000) == 500.0


def test_illiquid_brand_needs_deeper_discount():
    assert effective_ratio_threshold("RADO") < effective_ratio_threshold("TUDOR")


def test_prefilter_passes_clear_bargain(refs):
    res = evaluate(listing(), refs[0])
    assert res.passed
    assert res.price_ratio == pytest.approx(0.75)
    assert res.gross_spread == pytest.approx(600.0)


def test_prefilter_rejects_marginal_price(refs):
    res = evaluate(listing(price=2200.0, total_acquisition=2200.0), refs[0])
    assert not res.passed


def test_prefilter_counts_shipping_in_acquisition(refs):
    res = evaluate(listing(price=1900.0, shipping=50.0, total_acquisition=1950.0), refs[0])
    assert res.total_acquisition == 1950.0
    assert res.gross_spread == pytest.approx(450.0)


def test_prefilter_rejects_accessory(refs):
    res = evaluate(listing(title="Tudor 79030N bracelet only", price=200.0,
                           total_acquisition=200.0), refs[0])
    assert not res.passed


def test_prefilter_handles_missing_market_value():
    ref = WatchRef("ORIS", "Aquis", "01 400 7769 4157")
    res = evaluate(listing(), ref)
    assert not res.passed and "Missing" in res.reasons[0]


# --- ebay normalisation -----------------------------------------------------

def test_normalise_item_totals_cheapest_shipping():
    raw = {
        "itemId": "v1|123|0", "title": "Tudor 79030N",
        "price": {"value": "1800.00", "currency": "GBP"},
        "shippingOptions": [
            {"shippingCost": {"value": "25.00", "currency": "GBP"}},
            {"shippingCost": {"value": "10.00", "currency": "GBP"}},
        ],
        "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"],
        "seller": {"username": "abc", "feedbackScore": 500, "feedbackPercentage": "99.5"},
    }
    out = normalise_item(raw)
    assert out["shipping"] == 10.0
    assert out["total_acquisition"] == 1810.0
    assert out["buying_format"] == "BUY_IT_NOW"
    assert out["best_offer"] is True
    assert out["seller_feedback_pct"] == 99.5


# --- persistence: duplicates and price drops --------------------------------

def test_duplicate_listing_not_treated_as_new(conn):
    first = db.upsert_listing(conn, listing())
    second = db.upsert_listing(conn, listing())
    assert first["is_new"] is True
    assert second["is_new"] is False
    row = conn.execute("SELECT COUNT(*) c FROM listings").fetchone()
    assert row["c"] == 1


def test_price_drop_tracked(conn):
    db.upsert_listing(conn, listing(price=1450.0, total_acquisition=1450.0))
    db.upsert_listing(conn, listing(price=1295.0, total_acquisition=1295.0))
    drop = db.latest_price_drop(conn, "v1|1|0")
    assert drop["drop"] == pytest.approx(155.0)
    assert drop["drop_pct"] == pytest.approx(10.7, abs=0.1)


def test_price_increase_is_not_a_drop(conn):
    db.upsert_listing(conn, listing(price=1295.0))
    db.upsert_listing(conn, listing(price=1450.0))
    assert db.latest_price_drop(conn, "v1|1|0") is None


# --- pipeline with mocked eBay ----------------------------------------------

class FakeClient:
    """Mock eBay client — no network calls in tests."""

    def __init__(self, items):
        self.items = items
        self.queries = []

    def search(self, query, limit=50, exclusions=()):
        self.queries.append(query)
        return self.items


def test_run_scan_end_to_end(conn, refs):
    items = [
        {"itemId": "v1|9|0", "title": "Tudor Black Bay 58 79030N 2022 full set",
         "price": {"value": "1800.00", "currency": "GBP"},
         "buyingOptions": ["FIXED_PRICE"], "seller": {"username": "s1"}},
        {"itemId": "v1|8|0", "title": "Tudor 79030N bezel insert spare",
         "price": {"value": "90.00", "currency": "GBP"},
         "buyingOptions": ["FIXED_PRICE"], "seller": {"username": "s2"}},
        {"itemId": "v1|7|0", "title": "Casio F91W",
         "price": {"value": "12.00", "currency": "GBP"},
         "buyingOptions": ["FIXED_PRICE"], "seller": {"username": "s3"}},
    ]
    result = run_scan(conn, refs, client=FakeClient(items), progress=None)
    assert result["listings_scanned"] == 3
    assert result["matched"] == 2          # Casio does not match the watchlist
    assert result["candidates"] == 1       # accessory is filtered out
    run = conn.execute("SELECT * FROM scan_runs WHERE id = ?",
                       (result["run_id"],)).fetchone()
    assert run["prefilter_candidates"] == 1
    assert run["ai_calls"] == 0


def test_second_scan_marks_listing_not_new(conn, refs):
    items = [{"itemId": "v1|9|0", "title": "Tudor Black Bay 58 79030N",
              "price": {"value": "1800.00", "currency": "GBP"},
              "buyingOptions": ["FIXED_PRICE"], "seller": {"username": "s1"}}]
    run_scan(conn, refs, client=FakeClient(items))
    second = run_scan(conn, refs, client=FakeClient(items))
    assert second["listings"]["v1|9|0"]["is_new"] is False
