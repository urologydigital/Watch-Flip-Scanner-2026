"""API efficiency: caching and refresh separation (spec s.19).

The Phase 4 scan issued roughly 130 eBay calls for 51 references because every
query ran on every scan. Two changes cut that substantially:

1. **Query result caching.** Identical searches inside the TTL are served from
   SQLite instead of the API. With scans at 06:00, 12:00, 17:00 and 21:00 the
   listing TTL (45 min default) mostly protects against re-running a scan you
   just ran, while still giving each scheduled scan fresh listings.

2. **Separating market refresh from listing scans.** Market evidence changes
   slowly and is cached for 24 hours by default, so three of the four daily
   scans reuse it. Listings are what actually need to be fresh.

3. **Query narrowing.** References whose extra query variants have never
   produced a matching listing get dropped to a single primary query, so the
   long tail of the watchlist stops costing three calls each.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .config5 import CONFIG, Phase5Config

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_cache (
    cache_key TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    hits INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS query_effectiveness (
    id INTEGER PRIMARY KEY,
    reference TEXT NOT NULL,
    query TEXT NOT NULL,
    times_run INTEGER DEFAULT 0,
    matches_found INTEGER DEFAULT 0,
    last_run TEXT,
    UNIQUE (reference, query)
);

CREATE INDEX IF NOT EXISTS idx_cache_ns ON api_cache (namespace);
"""

NS_LISTINGS = "ebay_search"
NS_MARKET = "market_evidence"


def init_cache(conn: sqlite3.Connection) -> None:
    conn.executescript(CACHE_SCHEMA)
    conn.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(namespace: str, *parts: Any) -> str:
    raw = "|".join([namespace, *(str(p) for p in parts)])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def get(conn: sqlite3.Connection, namespace: str, *parts: Any,
        config: Phase5Config = CONFIG) -> Any | None:
    """Return a cached payload if present and unexpired."""
    if not config.cache.enabled:
        return None
    key = _key(namespace, *parts)
    row = conn.execute("SELECT payload_json, expires_at FROM api_cache "
                       "WHERE cache_key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except (ValueError, TypeError):
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= _now():
        return None
    conn.execute("UPDATE api_cache SET hits = hits + 1 WHERE cache_key = ?", (key,))
    conn.commit()
    return json.loads(row["payload_json"])


def put(conn: sqlite3.Connection, namespace: str, payload: Any, *parts: Any,
        ttl_seconds: int | None = None, config: Phase5Config = CONFIG) -> None:
    if not config.cache.enabled:
        return
    if ttl_seconds is None:
        ttl_seconds = (config.cache.market_evidence_ttl_hours * 3600
                       if namespace == NS_MARKET
                       else config.cache.listing_search_ttl_minutes * 60)
    now = _now()
    conn.execute(
        """INSERT INTO api_cache (cache_key, namespace, payload_json, created_at,
               expires_at, hits)
           VALUES (?, ?, ?, ?, ?, 0)
           ON CONFLICT (cache_key) DO UPDATE SET
               payload_json = excluded.payload_json,
               created_at = excluded.created_at,
               expires_at = excluded.expires_at""",
        (_key(namespace, *parts), namespace, json.dumps(payload),
         now.isoformat(timespec="seconds"),
         (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")),
    )
    conn.commit()


def cached_call(conn: sqlite3.Connection, namespace: str, parts: tuple,
                fn: Callable[[], Any], config: Phase5Config = CONFIG) -> tuple[Any, bool]:
    """Return (value, was_cached). Runs fn only on a miss."""
    hit = get(conn, namespace, *parts, config=config)
    if hit is not None:
        return hit, True
    value = fn()
    put(conn, namespace, value, *parts, config=config)
    return value, False


def purge_expired(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM api_cache WHERE expires_at <= ?",
                       (_now().isoformat(timespec="seconds"),))
    conn.commit()
    return cur.rowcount or 0


def cache_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT namespace, COUNT(*) AS entries, SUM(hits) AS hits
           FROM api_cache GROUP BY namespace""").fetchall()
    return {r["namespace"]: {"entries": r["entries"], "hits": r["hits"] or 0}
            for r in rows}


# --- query effectiveness ----------------------------------------------------

def record_query(conn: sqlite3.Connection, reference: str, query: str,
                 matches: int) -> None:
    """Track which query variants actually find matching watches."""
    conn.execute(
        """INSERT INTO query_effectiveness (reference, query, times_run,
               matches_found, last_run)
           VALUES (?, ?, 1, ?, ?)
           ON CONFLICT (reference, query) DO UPDATE SET
               times_run = times_run + 1,
               matches_found = matches_found + excluded.matches_found,
               last_run = excluded.last_run""",
        (reference, query, matches, _now().isoformat(timespec="seconds")),
    )
    conn.commit()


def prune_queries(conn: sqlite3.Connection, reference: str, queries: list[str],
                  min_runs: int = 6) -> list[str]:
    """Drop query variants with a proven zero hit rate.

    A variant is only dropped after `min_runs` attempts with no match ever, and
    the first (primary) query is always kept so a reference is never dropped
    entirely.
    """
    if len(queries) <= 1:
        return queries
    rows = conn.execute(
        "SELECT query, times_run, matches_found FROM query_effectiveness "
        "WHERE reference = ?", (reference,)).fetchall()
    stats = {r["query"]: (r["times_run"], r["matches_found"]) for r in rows}

    kept = [queries[0]]
    for q in queries[1:]:
        runs, matches = stats.get(q, (0, 0))
        if runs >= min_runs and matches == 0:
            continue
        kept.append(q)
    return kept


def estimated_calls_saved(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT SUM(hits) AS h FROM api_cache WHERE namespace = ?",
        (NS_LISTINGS,)).fetchone()
    return int(row["h"] or 0)
