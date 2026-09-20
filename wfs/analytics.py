"""Historical analytics over stored scans (spec s.18, s.19, Phase 4).

Everything here runs on data already collected by previous scans, so it costs no
API calls and gets better the longer you run the tool.

One honesty constraint governs this whole module. A listing disappearing from
eBay does NOT mean it sold. It may have sold, been ended early, or simply
expired. Every metric below therefore talks about listings being DELISTED, and
none of it is ever fed back into the sold-evidence engine. If you want sold
evidence, record it in observed_sales.csv where it can be trusted.
"""
from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Below this many tracked listings a per-reference statistic is not worth
# reporting as a number.
MIN_SAMPLE = 3


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _days_between(start: str | None, end: str | None) -> float | None:
    a, b = _dt(start), _dt(end)
    if a is None or b is None:
        return None
    return round((b - a).total_seconds() / 86400, 2)


# --- listing lifecycle ------------------------------------------------------

@dataclass
class Lifecycle:
    item_id: str
    reference: str | None
    brand: str | None
    title: str | None
    status: str                  # ACTIVE | DELISTED
    first_seen: str | None
    last_seen: str | None
    days_tracked: float | None
    times_seen: int
    first_price: float | None
    latest_price: float | None
    total_change: float | None
    total_change_pct: float | None
    drop_count: int
    url: str | None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def listing_lifecycles(conn: sqlite3.Connection, reference: str | None = None,
                       limit: int = 500) -> list[Lifecycle]:
    """Per-listing history: how long it has been on sale and how the price moved."""
    query = "SELECT * FROM listings"
    params: list[Any] = []
    if reference:
        query += " WHERE reference = ?"
        params.append(reference)
    query += " ORDER BY first_seen DESC LIMIT ?"
    params.append(limit)

    out: list[Lifecycle] = []
    for row in conn.execute(query, params).fetchall():
        prices = [r["price"] for r in conn.execute(
            """SELECT price FROM listing_price_history
               WHERE item_id = ? AND price IS NOT NULL
               ORDER BY observed_at, id""", (row["item_id"],)).fetchall()]
        first = row["first_price"] if row["first_price"] is not None else (
            prices[0] if prices else None)
        latest = prices[-1] if prices else row["price"]

        change = change_pct = None
        if first and latest is not None:
            change = round(latest - first, 2)
            change_pct = round(change / first * 100, 2) if first else None

        drops = sum(1 for a, b in zip(prices, prices[1:]) if b < a - 0.009)
        end = row["disappeared_at"] or row["last_seen"]

        out.append(Lifecycle(
            item_id=row["item_id"],
            reference=row["reference"],
            brand=row["brand"],
            title=row["title"],
            status="DELISTED" if row["disappeared_at"] else "ACTIVE",
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            days_tracked=_days_between(row["first_seen"], end),
            times_seen=row["times_seen"] or 1,
            first_price=first,
            latest_price=latest,
            total_change=change,
            total_change_pct=change_pct,
            drop_count=drops,
            url=row["url"],
        ))
    return out


# --- per-reference performance ----------------------------------------------

@dataclass
class ReferenceStats:
    reference: str
    brand: str | None
    tracked: int
    active: int
    delisted: int
    median_days_to_delist: float | None
    median_discount_pct: float | None
    listings_with_a_drop: int
    median_asking: float | None
    sample_warning: str | None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def reference_performance(conn: sqlite3.Connection) -> list[ReferenceStats]:
    """How listings for each reference have behaved across all scans.

    'Days to delist' is time on your radar before the listing vanished. It is a
    weak proxy for time-to-sale and nothing more: some of those listings were
    pulled or expired unsold.
    """
    lifecycles = listing_lifecycles(conn, limit=10000)
    by_ref: dict[str, list[Lifecycle]] = {}
    for lc in lifecycles:
        if lc.reference:
            by_ref.setdefault(lc.reference, []).append(lc)

    out: list[ReferenceStats] = []
    for reference, items in sorted(by_ref.items()):
        delisted = [i for i in items if i.status == "DELISTED"]
        active = [i for i in items if i.status == "ACTIVE"]

        days = [i.days_tracked for i in delisted if i.days_tracked is not None]
        discounts = [i.total_change_pct for i in delisted
                     if i.total_change_pct is not None and i.total_change_pct < 0]
        asking = [i.latest_price for i in active if i.latest_price]

        warning = None
        if len(delisted) < MIN_SAMPLE:
            warning = (f"Only {len(delisted)} delisted listing(s) tracked — "
                       "not enough to read anything into.")

        out.append(ReferenceStats(
            reference=reference,
            brand=items[0].brand,
            tracked=len(items),
            active=len(active),
            delisted=len(delisted),
            median_days_to_delist=(round(statistics.median(days), 1)
                                   if len(days) >= MIN_SAMPLE else None),
            median_discount_pct=(round(statistics.median(discounts), 2)
                                 if len(discounts) >= MIN_SAMPLE else None),
            listings_with_a_drop=sum(1 for i in items if i.drop_count > 0),
            median_asking=(round(statistics.median(asking), 2) if asking else None),
            sample_warning=warning,
        ))
    return out


# --- price drift ------------------------------------------------------------

def price_drift(conn: sqlite3.Connection, reference: str | None = None) -> dict[str, Any]:
    """How far asking prices move before a listing disappears.

    A large typical drift tells you sellers of that reference are starting high,
    which is a reason to wait rather than to buy at the opening ask.
    """
    lifecycles = listing_lifecycles(conn, reference=reference, limit=10000)
    delisted = [l for l in lifecycles if l.status == "DELISTED"
                and l.total_change_pct is not None]
    active = [l for l in lifecycles if l.status == "ACTIVE"
              and l.total_change_pct is not None]

    def summarise(items: list[Lifecycle]) -> dict[str, Any]:
        changes = [i.total_change_pct for i in items]
        dropped = [c for c in changes if c < 0]
        return {
            "count": len(items),
            "median_change_pct": (round(statistics.median(changes), 2)
                                  if len(changes) >= MIN_SAMPLE else None),
            "share_that_dropped": (round(len(dropped) / len(changes) * 100, 1)
                                   if changes else None),
            "median_drop_pct": (round(statistics.median(dropped), 2)
                                if len(dropped) >= MIN_SAMPLE else None),
        }

    return {
        "reference": reference or "ALL",
        "delisted": summarise(delisted),
        "still_active": summarise(active),
        "caveat": ("Delisted does not mean sold. Some of these listings were ended "
                   "or expired unsold, so treat this as asking-price behaviour only."),
    }


# --- current price drops ----------------------------------------------------

def active_price_drops(conn: sqlite3.Connection, min_pct: float = 3.0,
                       limit: int = 50) -> list[dict[str, Any]]:
    """Live listings that have fallen since you first saw them (spec s.19)."""
    out: list[dict[str, Any]] = []
    for lc in listing_lifecycles(conn, limit=5000):
        if lc.status != "ACTIVE" or lc.total_change_pct is None:
            continue
        if lc.total_change_pct > -min_pct:
            continue
        out.append({
            "Reference": lc.reference,
            "Brand": lc.brand,
            "Title": lc.title,
            "First price": lc.first_price,
            "Current price": lc.latest_price,
            "Drop": round((lc.first_price or 0) - (lc.latest_price or 0), 2),
            "Drop %": round(abs(lc.total_change_pct), 1),
            "Drops observed": lc.drop_count,
            "Days tracked": lc.days_tracked,
            "Link": lc.url,
        })
    out.sort(key=lambda r: r["Drop %"], reverse=True)
    return out[:limit]


# --- scan trends ------------------------------------------------------------

def scan_trends(conn: sqlite3.Connection, limit: int = 60) -> list[dict[str, Any]]:
    """Scan-over-scan counts, oldest first, for charting."""
    rows = conn.execute(
        """SELECT id, started_at, finished_at, mode, listings_scanned,
                  prefilter_candidates, deep_analysed, ai_calls
           FROM scan_runs WHERE finished_at IS NOT NULL
           ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for row in reversed(rows):
        record = dict(row)
        record["duration_s"] = _days_between(row["started_at"], row["finished_at"])
        if record["duration_s"] is not None:
            record["duration_s"] = round(record["duration_s"] * 86400, 1)
        hit_rate = None
        if row["listings_scanned"]:
            hit_rate = round(row["prefilter_candidates"] / row["listings_scanned"] * 100, 2)
        record["candidate_rate_pct"] = hit_rate
        out.append(record)
    return out


def verdict_history(conn: sqlite3.Connection, limit: int = 60) -> list[dict[str, Any]]:
    """BUY / WATCH / PASS counts per scan, from stored AI and candidate records."""
    rows = conn.execute(
        """SELECT scan_run_id, verdict, COUNT(*) AS n FROM ai_analysis
           WHERE scan_run_id IS NOT NULL
           GROUP BY scan_run_id, verdict ORDER BY scan_run_id DESC LIMIT ?""",
        (limit * 3,)).fetchall()
    by_run: dict[int, dict[str, int]] = {}
    for row in rows:
        by_run.setdefault(row["scan_run_id"], {})[row["verdict"] or "UNKNOWN"] = row["n"]
    return [{"scan_run_id": run, **counts} for run, counts in sorted(by_run.items())]


# --- repeat offenders -------------------------------------------------------

def persistent_listings(conn: sqlite3.Connection, min_scans: int = 3,
                        limit: int = 50) -> list[dict[str, Any]]:
    """Listings seen repeatedly and still unsold.

    A watch that has sat through several scans is telling you something: either
    it is priced above what the market will pay, or demand for that reference is
    thinner than the asking prices suggest. Both are useful before you bid.
    """
    rows = conn.execute(
        """SELECT * FROM listings
           WHERE disappeared_at IS NULL AND COALESCE(times_seen, 1) >= ?
           ORDER BY times_seen DESC, first_seen LIMIT ?""",
        (min_scans, limit)).fetchall()
    out = []
    for row in rows:
        out.append({
            "Reference": row["reference"],
            "Brand": row["brand"],
            "Title": row["title"],
            "Scans seen in": row["times_seen"],
            "Days on radar": _days_between(row["first_seen"], row["last_seen"]),
            "First price": row["first_price"],
            "Current price": row["price"],
            "Link": row["url"],
        })
    return out


def database_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Headline numbers for how much history the tool has accumulated."""
    def count(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    first = conn.execute(
        "SELECT MIN(started_at) FROM scan_runs").fetchone()[0]
    return {
        "scans_run": count("SELECT COUNT(*) FROM scan_runs WHERE finished_at IS NOT NULL"),
        "listings_tracked": count("SELECT COUNT(*) FROM listings"),
        "listings_active": count("SELECT COUNT(*) FROM listings WHERE disappeared_at IS NULL"),
        "listings_delisted": count(
            "SELECT COUNT(*) FROM listings WHERE disappeared_at IS NOT NULL"),
        "price_observations": count("SELECT COUNT(*) FROM listing_price_history"),
        "ai_analyses": count("SELECT COUNT(*) FROM ai_analysis"),
        "tracking_since": first,
    }
