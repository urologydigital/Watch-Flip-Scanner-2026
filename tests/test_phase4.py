from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from wfs import analytics, db
from wfs.pipeline import run_scan
from wfs.sold_market import COVERAGE_PARTIAL, NullSoldProvider, SoldEvidence
from wfs.watchlist import WatchRef


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "p4.sqlite3")
    db.init_db(c)
    return c


@pytest.fixture
def ref():
    return WatchRef("TUDOR", "Black Bay 58", "79030N", 2150, 2400, 2650)


def listing(item_id="v1|1|0", price=2000.0, **kw):
    base = {
        "item_id": item_id, "title": "Tudor Black Bay 58 79030N",
        "price": price, "shipping": 0.0, "total_acquisition": price,
        "reference": "79030N", "brand": "TUDOR", "model": "Black Bay 58",
        "url": f"https://ebay.co.uk/itm/{item_id}",
    }
    base.update(kw)
    return base


def backdate(conn: sqlite3.Connection, item_id: str, days: int) -> None:
    """Shift a listing's first_seen back in time to simulate elapsed history."""
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    conn.execute("UPDATE listings SET first_seen = ? WHERE item_id = ?", (when, item_id))
    conn.execute(
        """UPDATE listing_price_history SET observed_at = ?
           WHERE id = (SELECT MIN(id) FROM listing_price_history WHERE item_id = ?)""",
        (when, item_id))
    conn.commit()


# --- migration --------------------------------------------------------------

def test_migration_adds_columns_to_old_database(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(str(path))
    old.execute("""CREATE TABLE listings (
        id INTEGER PRIMARY KEY, item_id TEXT UNIQUE, reference TEXT, price REAL,
        first_seen TEXT, last_seen TEXT)""")
    old.execute("CREATE TABLE listing_price_history (id INTEGER PRIMARY KEY, "
                "item_id TEXT, observed_at TEXT, price REAL)")
    old.execute("INSERT INTO listings (item_id, reference, price) VALUES ('a','79030N',2000)")
    old.execute("INSERT INTO listing_price_history (item_id, observed_at, price) "
                "VALUES ('a','2026-08-01T00:00:00+00:00',2100)")
    old.commit()
    old.close()

    c = db.connect(path)
    applied = db.migrate(c)
    assert "listings.disappeared_at" in applied
    assert "listings.times_seen" in applied
    # first_price is backfilled from the earliest recorded observation.
    assert c.execute("SELECT first_price FROM listings WHERE item_id='a'"
                     ).fetchone()["first_price"] == 2100


def test_migration_is_idempotent(conn):
    assert db.migrate(conn) == []


# --- lifecycle tracking -----------------------------------------------------

def test_times_seen_increments(conn):
    db.upsert_listing(conn, listing())
    db.upsert_listing(conn, listing())
    db.upsert_listing(conn, listing())
    row = conn.execute("SELECT times_seen FROM listings").fetchone()
    assert row["times_seen"] == 3


def test_first_price_is_preserved(conn):
    db.upsert_listing(conn, listing(price=2000.0))
    db.upsert_listing(conn, listing(price=1800.0))
    row = conn.execute("SELECT first_price, price FROM listings").fetchone()
    assert row["first_price"] == 2000.0
    assert row["price"] == 1800.0


def test_absent_listing_marked_delisted(conn):
    db.upsert_listing(conn, listing("v1|1|0"))
    db.upsert_listing(conn, listing("v1|2|0"))
    gone = db.mark_absent_listings(conn, ["79030N"], {"v1|1|0"})
    assert gone == 1
    statuses = {r["item_id"]: r["disappeared_at"] for r in
                conn.execute("SELECT item_id, disappeared_at FROM listings")}
    assert statuses["v1|1|0"] is None
    assert statuses["v1|2|0"] is not None


def test_absence_ignored_for_unsearched_references(conn):
    db.upsert_listing(conn, listing("v1|1|0"))
    # A scan that searched only Omega tells us nothing about a Tudor listing.
    assert db.mark_absent_listings(conn, ["210.30.42.20"], set()) == 0
    assert conn.execute("SELECT disappeared_at FROM listings"
                        ).fetchone()["disappeared_at"] is None


def test_reappearing_listing_clears_delisted_flag(conn):
    db.upsert_listing(conn, listing())
    db.mark_absent_listings(conn, ["79030N"], set())
    result = db.upsert_listing(conn, listing())
    assert result["reappeared"] is True
    assert conn.execute("SELECT disappeared_at FROM listings"
                        ).fetchone()["disappeared_at"] is None


# --- analytics --------------------------------------------------------------

def test_lifecycle_reports_price_movement(conn):
    db.upsert_listing(conn, listing(price=2000.0))
    db.upsert_listing(conn, listing(price=1900.0))
    db.upsert_listing(conn, listing(price=1800.0))
    lc = analytics.listing_lifecycles(conn)[0]
    assert lc.status == "ACTIVE"
    assert lc.first_price == 2000.0
    assert lc.latest_price == 1800.0
    assert lc.total_change == -200.0
    assert lc.total_change_pct == pytest.approx(-10.0)
    assert lc.drop_count == 2
    assert lc.times_seen == 3


def test_lifecycle_days_tracked(conn):
    db.upsert_listing(conn, listing())
    backdate(conn, "v1|1|0", 14)
    lc = analytics.listing_lifecycles(conn)[0]
    assert 13 <= lc.days_tracked <= 15


def test_reference_performance_suppresses_tiny_samples(conn):
    db.upsert_listing(conn, listing("v1|1|0", 2000.0))
    db.mark_absent_listings(conn, ["79030N"], set())
    stats = analytics.reference_performance(conn)[0]
    assert stats.delisted == 1
    assert stats.median_days_to_delist is None      # one data point is not a median
    assert "not enough" in stats.sample_warning


def test_reference_performance_reports_with_enough_data(conn):
    for i in range(4):
        db.upsert_listing(conn, listing(f"v1|{i}|0", 2000.0))
        db.upsert_listing(conn, listing(f"v1|{i}|0", 1800.0))
        backdate(conn, f"v1|{i}|0", 10 + i)
    db.mark_absent_listings(conn, ["79030N"], set())
    stats = analytics.reference_performance(conn)[0]
    assert stats.tracked == 4 and stats.delisted == 4 and stats.active == 0
    assert stats.median_days_to_delist is not None
    assert stats.median_discount_pct == pytest.approx(-10.0)
    assert stats.sample_warning is None


def test_price_drift_separates_delisted_from_active(conn):
    for i in range(3):
        db.upsert_listing(conn, listing(f"gone{i}", 2000.0))
        db.upsert_listing(conn, listing(f"gone{i}", 1700.0))
    db.mark_absent_listings(conn, ["79030N"], set())
    for i in range(3):
        db.upsert_listing(conn, listing(f"live{i}", 2000.0))
        db.upsert_listing(conn, listing(f"live{i}", 1950.0))
    drift = analytics.price_drift(conn)
    assert drift["delisted"]["count"] == 3
    assert drift["still_active"]["count"] == 3
    assert drift["delisted"]["median_change_pct"] == pytest.approx(-15.0)
    assert drift["still_active"]["median_change_pct"] == pytest.approx(-2.5)


def test_price_drift_carries_the_delisted_caveat(conn):
    assert "does not mean sold" in analytics.price_drift(conn)["caveat"]


def test_active_price_drops_ranked(conn):
    db.upsert_listing(conn, listing("small", 2000.0))
    db.upsert_listing(conn, listing("small", 1960.0))     # -2%, below threshold
    db.upsert_listing(conn, listing("big", 2000.0))
    db.upsert_listing(conn, listing("big", 1700.0))       # -15%
    drops = analytics.active_price_drops(conn, min_pct=3.0)
    assert len(drops) == 1
    assert drops[0]["Drop %"] == pytest.approx(15.0)
    assert drops[0]["Drop"] == pytest.approx(300.0)


def test_delisted_listings_excluded_from_active_drops(conn):
    db.upsert_listing(conn, listing("big", 2000.0))
    db.upsert_listing(conn, listing("big", 1700.0))
    db.mark_absent_listings(conn, ["79030N"], set())
    assert analytics.active_price_drops(conn) == []


def test_persistent_listings(conn):
    for _ in range(4):
        db.upsert_listing(conn, listing("sticky", 2000.0))
    db.upsert_listing(conn, listing("fresh", 2000.0))
    rows = analytics.persistent_listings(conn, min_scans=3)
    assert len(rows) == 1
    assert rows[0]["Scans seen in"] == 4


def test_database_summary(conn):
    db.upsert_listing(conn, listing("a"))
    db.upsert_listing(conn, listing("b"))
    db.mark_absent_listings(conn, ["79030N"], {"a"})
    summary = analytics.database_summary(conn)
    assert summary["listings_tracked"] == 2
    assert summary["listings_active"] == 1
    assert summary["listings_delisted"] == 1


# --- integration ------------------------------------------------------------

class FakeClient:
    def __init__(self, items):
        self.items = items

    def search(self, query, limit=50, exclusions=()):
        return self.items


def item(item_id, price):
    return {"itemId": item_id, "title": "Tudor Black Bay 58 79030N full set",
            "price": {"value": str(price), "currency": "GBP"},
            "buyingOptions": ["FIXED_PRICE"], "seller": {"username": "s1"}}


def test_consecutive_scans_track_disappearance(conn, ref):
    run_scan(conn, [ref], client=FakeClient([item("a", 2000), item("b", 2000)]),
             sold_provider=NullSoldProvider(), use_ai=False)
    result = run_scan(conn, [ref], client=FakeClient([item("a", 1900)]),
                      sold_provider=NullSoldProvider(), use_ai=False)
    assert result["delisted_since_last_scan"] == 1

    lifecycles = {l.item_id: l for l in analytics.listing_lifecycles(conn)}
    assert lifecycles["a"].status == "ACTIVE"
    assert lifecycles["a"].total_change == -100.0
    assert lifecycles["b"].status == "DELISTED"


def test_scan_references_recorded(conn, ref):
    result = run_scan(conn, [ref], client=FakeClient([item("a", 2000)]),
                      sold_provider=NullSoldProvider(), use_ai=False)
    rows = conn.execute("SELECT reference FROM scan_references WHERE scan_run_id = ?",
                        (result["run_id"],)).fetchall()
    assert [r["reference"] for r in rows] == ["79030N"]


def test_scan_trends_computes_candidate_rate(conn, ref):
    run_scan(conn, [ref], client=FakeClient([item("a", 1300), item("b", 2400)]),
             sold_provider=NullSoldProvider(), use_ai=False)
    trends = analytics.scan_trends(conn)
    assert len(trends) == 1
    assert trends[0]["listings_scanned"] == 2
    assert trends[0]["candidate_rate_pct"] == pytest.approx(50.0)


def test_delisting_never_becomes_sold_evidence(conn, ref):
    """Delisted listings must not leak into the sold-evidence tables."""
    run_scan(conn, [ref], client=FakeClient([item("a", 2000), item("b", 2000)]),
             sold_provider=NullSoldProvider(), use_ai=False)
    run_scan(conn, [ref], client=FakeClient([]), sold_provider=NullSoldProvider(),
             use_ai=False)
    rows = conn.execute("SELECT * FROM sold_observations").fetchall()
    assert all(r["observed_sale_count"] is None for r in rows)
