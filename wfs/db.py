"""SQLite persistence. Schema covers spec sections 18 and 19."""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_references (
    id INTEGER PRIMARY KEY,
    brand TEXT NOT NULL,
    model TEXT NOT NULL,
    reference TEXT NOT NULL,
    market_low REAL,
    market_mid REAL,
    market_high REAL,
    value_source TEXT,
    confidence TEXT DEFAULT 'LOW',
    active INTEGER DEFAULT 1,
    UNIQUE (brand, reference)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT,
    listings_scanned INTEGER DEFAULT 0,
    prefilter_candidates INTEGER DEFAULT 0,
    deep_analysed INTEGER DEFAULT 0,
    ai_calls INTEGER DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT 'EBAY_GB',
    reference TEXT,
    brand TEXT,
    model TEXT,
    title TEXT,
    price REAL,
    currency TEXT,
    shipping REAL,
    total_acquisition REAL,
    buying_format TEXT,
    best_offer INTEGER DEFAULT 0,
    condition TEXT,
    seller TEXT,
    seller_feedback_score INTEGER,
    seller_feedback_pct REAL,
    item_location TEXT,
    url TEXT,
    image_url TEXT,
    authenticity_guarantee INTEGER DEFAULT 0,
    returns_accepted INTEGER,
    listing_created TEXT,
    first_seen TEXT,
    last_seen TEXT,
    times_seen INTEGER DEFAULT 1,
    first_price REAL,
    disappeared_at TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS scan_references (
    id INTEGER PRIMARY KEY,
    scan_run_id INTEGER NOT NULL,
    reference TEXT NOT NULL,
    UNIQUE (scan_run_id, reference)
);

CREATE TABLE IF NOT EXISTS listing_price_history (
    id INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    price REAL,
    total_acquisition REAL,
    scan_run_id INTEGER,
    FOREIGN KEY (item_id) REFERENCES listings (item_id)
);

CREATE TABLE IF NOT EXISTS market_observations (
    id INTEGER PRIMARY KEY,
    reference TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,          -- ASKING | DEALER | INDEX
    low REAL, mid REAL, high REAL,
    sample_size INTEGER,
    coverage_status TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS sold_observations (
    id INTEGER PRIMARY KEY,
    reference TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    period_days INTEGER,
    match_scope TEXT,            -- EXACT_REFERENCE | MODEL_FAMILY
    observed_sale_count INTEGER,
    median_price REAL,
    coverage_status TEXT NOT NULL DEFAULT 'PARTIAL',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS candidate_scores (
    id INTEGER PRIMARY KEY,
    scan_run_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    reference TEXT,
    total_acquisition REAL,
    market_mid REAL,
    price_ratio REAL,
    gross_spread REAL,
    required_spread REAL,
    passed_prefilter INTEGER,
    reasons TEXT,
    created_at TEXT,
    FOREIGN KEY (scan_run_id) REFERENCES scan_runs (id)
);

CREATE TABLE IF NOT EXISTS ai_analysis (
    id INTEGER PRIMARY KEY,
    scan_run_id INTEGER,
    item_id TEXT NOT NULL,
    model TEXT,
    created_at TEXT,
    verdict TEXT,
    confidence TEXT,
    payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_price_hist_item ON listing_price_history (item_id);
CREATE INDEX IF NOT EXISTS idx_listings_ref ON listings (reference);
CREATE INDEX IF NOT EXISTS idx_scan_refs ON scan_references (scan_run_id);
CREATE INDEX IF NOT EXISTS idx_cand_run ON candidate_scores (scan_run_id);
"""


def backup_database(path: Path | str | None = None,
                    backup_dir: Path | str | None = None) -> Path | None:
    """Timestamped copy of the SQLite file, taken BEFORE any migration.

    Returns the backup path, or None when there is no database yet (a first run
    has nothing to lose). Backups live in `db_backups/` which is excluded from
    the release archive — they are your data, not part of the product.
    """
    source = Path(path or DB_PATH)
    if not source.exists():
        return None
    target_dir = Path(backup_dir or (source.parent / "db_backups"))
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / f"{source.stem}.{stamp}.sqlite3"
    shutil.copy2(source, target)
    return target


def table_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row count per table — used to prove a migration lost nothing."""
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
    counts: dict[str, int] = {}
    for name in names:
        try:
            counts[name] = int(conn.execute(
                f"SELECT COUNT(*) AS c FROM {name}").fetchone()["c"])
        except sqlite3.Error:
            counts[name] = -1
    return counts


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    # check_same_thread=False: Streamlit executes each rerun on a fresh
    # ScriptRunner thread while @st.cache_resource keeps one connection alive
    # across them. Without this, any rerun on a new thread raises
    # "SQLite objects created in a thread can only be used in that same thread".
    # Access is effectively serialised by Streamlit's single-script execution.
    conn = sqlite3.connect(str(path or DB_PATH), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the first release. Existing databases are migrated in place
# so that a Phase 1-3 database keeps its history rather than being rebuilt.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("listings", "times_seen", "INTEGER DEFAULT 1"),
    ("listings", "first_price", "REAL"),
    ("listings", "disappeared_at", "TEXT"),
]


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Add any columns missing from an older database. Returns what was added."""
    applied: list[str] = []
    for table, column, decl in MIGRATIONS:
        existing = {r["name"] for r in
                    conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not existing:
            continue
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            applied.append(f"{table}.{column}")
    # Backfill first_price for rows that predate the column.
    conn.execute(
        """UPDATE listings SET first_price = (
               SELECT price FROM listing_price_history h
               WHERE h.item_id = listings.item_id AND h.price IS NOT NULL
               ORDER BY h.observed_at, h.id LIMIT 1)
           WHERE first_price IS NULL""")
    conn.commit()
    return applied


# --- watch references -------------------------------------------------------

def upsert_reference(conn: sqlite3.Connection, ref: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO watch_references
            (brand, model, reference, market_low, market_mid, market_high,
             value_source, confidence, active)
        VALUES (:brand, :model, :reference, :market_low, :market_mid, :market_high,
                :value_source, :confidence, 1)
        ON CONFLICT (brand, reference) DO UPDATE SET
            model = excluded.model,
            market_low = excluded.market_low,
            market_mid = excluded.market_mid,
            market_high = excluded.market_high,
            value_source = excluded.value_source,
            confidence = excluded.confidence
        """,
        {
            "brand": ref["brand"],
            "model": ref.get("model", ""),
            "reference": ref["reference"],
            "market_low": ref.get("market_low"),
            "market_mid": ref.get("market_mid"),
            "market_high": ref.get("market_high"),
            "value_source": ref.get("value_source", "SEED_PLACEHOLDER"),
            "confidence": ref.get("confidence", "LOW"),
        },
    )
    conn.commit()


def get_references(conn: sqlite3.Connection, active_only: bool = True) -> list[sqlite3.Row]:
    q = "SELECT * FROM watch_references"
    if active_only:
        q += " WHERE active = 1"
    q += " ORDER BY brand, model, reference"
    return conn.execute(q).fetchall()


# --- scan runs --------------------------------------------------------------

def start_scan(conn: sqlite3.Connection, mode: str) -> int:
    cur = conn.execute(
        "INSERT INTO scan_runs (started_at, mode) VALUES (?, ?)", (now(), mode)
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_scan(conn: sqlite3.Connection, run_id: int, **counts: Any) -> None:
    conn.execute(
        """UPDATE scan_runs SET finished_at = ?, listings_scanned = ?,
                  prefilter_candidates = ?, deep_analysed = ?, ai_calls = ?, notes = ?
           WHERE id = ?""",
        (
            now(),
            counts.get("listings_scanned", 0),
            counts.get("prefilter_candidates", 0),
            counts.get("deep_analysed", 0),
            counts.get("ai_calls", 0),
            counts.get("notes"),
            run_id,
        ),
    )
    conn.commit()


def scan_history(conn: sqlite3.Connection, limit: int = 25) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


# --- listings ---------------------------------------------------------------

def upsert_listing(conn: sqlite3.Connection, listing: dict[str, Any],
                   scan_run_id: int | None = None) -> dict[str, Any]:
    """Insert or update a listing. Returns dict describing novelty/price change."""
    ts = now()
    existing = conn.execute(
        "SELECT item_id, price, total_acquisition, first_seen FROM listings WHERE item_id = ?",
        (listing["item_id"],),
    ).fetchone()

    result: dict[str, Any] = {"is_new": existing is None, "previous_price": None,
                              "price_change": None, "reappeared": False}

    if existing is None:
        conn.execute(
            """INSERT INTO listings (item_id, source, reference, brand, model, title,
                price, currency, shipping, total_acquisition, buying_format, best_offer,
                condition, seller, seller_feedback_score, seller_feedback_pct,
                item_location, url, image_url, authenticity_guarantee, returns_accepted,
                listing_created, first_seen, last_seen, raw_json)
               VALUES (:item_id, :source, :reference, :brand, :model, :title, :price,
                :currency, :shipping, :total_acquisition, :buying_format, :best_offer,
                :condition, :seller, :seller_feedback_score, :seller_feedback_pct,
                :item_location, :url, :image_url, :authenticity_guarantee,
                :returns_accepted, :listing_created, :first_seen, :last_seen, :raw_json)""",
            _listing_params(listing, ts, first_seen=ts),
        )
        conn.execute(
            "UPDATE listings SET first_price = ?, times_seen = 1 WHERE item_id = ?",
            (listing.get("price"), listing["item_id"]),
        )
    else:
        result["previous_price"] = existing["price"]
        if existing["price"] is not None and listing.get("price") is not None:
            delta = float(listing["price"]) - float(existing["price"])
            if abs(delta) >= 0.01:
                result["price_change"] = delta
        was_gone = conn.execute(
            "SELECT disappeared_at FROM listings WHERE item_id = ?",
            (listing["item_id"],)).fetchone()["disappeared_at"]
        result["reappeared"] = was_gone is not None
        conn.execute(
            """UPDATE listings SET times_seen = COALESCE(times_seen, 1) + 1,
                   disappeared_at = NULL WHERE item_id = ?""",
            (listing["item_id"],),
        )
        conn.execute(
            """UPDATE listings SET reference = :reference, brand = :brand, model = :model,
                title = :title, price = :price, currency = :currency, shipping = :shipping,
                total_acquisition = :total_acquisition, buying_format = :buying_format,
                best_offer = :best_offer, condition = :condition, seller = :seller,
                seller_feedback_score = :seller_feedback_score,
                seller_feedback_pct = :seller_feedback_pct, item_location = :item_location,
                url = :url, image_url = :image_url,
                authenticity_guarantee = :authenticity_guarantee,
                returns_accepted = :returns_accepted, listing_created = :listing_created,
                last_seen = :last_seen, raw_json = :raw_json
               WHERE item_id = :item_id""",
            _listing_params(listing, ts, first_seen=existing["first_seen"]),
        )

    conn.execute(
        """INSERT INTO listing_price_history (item_id, observed_at, price,
               total_acquisition, scan_run_id) VALUES (?, ?, ?, ?, ?)""",
        (listing["item_id"], ts, listing.get("price"),
         listing.get("total_acquisition"), scan_run_id),
    )
    conn.commit()
    return result


def _listing_params(listing: dict[str, Any], ts: str, first_seen: str) -> dict[str, Any]:
    p = {
        "item_id": listing["item_id"],
        "source": listing.get("source", "EBAY_GB"),
        "reference": listing.get("reference"),
        "brand": listing.get("brand"),
        "model": listing.get("model"),
        "title": listing.get("title"),
        "price": listing.get("price"),
        "currency": listing.get("currency"),
        "shipping": listing.get("shipping"),
        "total_acquisition": listing.get("total_acquisition"),
        "buying_format": listing.get("buying_format"),
        "best_offer": int(bool(listing.get("best_offer"))),
        "condition": listing.get("condition"),
        "seller": listing.get("seller"),
        "seller_feedback_score": listing.get("seller_feedback_score"),
        "seller_feedback_pct": listing.get("seller_feedback_pct"),
        "item_location": listing.get("item_location"),
        "url": listing.get("url"),
        "image_url": listing.get("image_url"),
        "authenticity_guarantee": int(bool(listing.get("authenticity_guarantee"))),
        "returns_accepted": listing.get("returns_accepted"),
        "listing_created": listing.get("listing_created"),
        "first_seen": first_seen,
        "last_seen": ts,
        "raw_json": json.dumps(listing.get("raw", {}))[:200000],
    }
    return p


def record_scan_references(conn: sqlite3.Connection, run_id: int,
                           references: Iterable[str]) -> None:
    """Record which references a scan actually searched.

    Needed so that absence can be interpreted: a listing missing from a scan that
    never searched its reference tells us nothing.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO scan_references (scan_run_id, reference) VALUES (?, ?)",
        [(run_id, r) for r in references],
    )
    conn.commit()


def mark_absent_listings(conn: sqlite3.Connection, references: Iterable[str],
                         seen_item_ids: Iterable[str]) -> int:
    """Mark listings that were searched for but no longer appear.

    IMPORTANT: this records that a listing is DELISTED, not that it sold. A
    listing can vanish because it sold, because the seller ended it, or because
    it simply expired. Nothing here is ever counted as sold evidence.
    """
    refs = list(references)
    seen = set(seen_item_ids)
    if not refs:
        return 0
    ts = now()
    rows = conn.execute(
        f"""SELECT item_id FROM listings
            WHERE reference IN ({','.join('?' * len(refs))})
              AND disappeared_at IS NULL""",
        refs,
    ).fetchall()
    gone = [r["item_id"] for r in rows if r["item_id"] not in seen]
    if gone:
        conn.executemany(
            "UPDATE listings SET disappeared_at = ? WHERE item_id = ?",
            [(ts, item_id) for item_id in gone],
        )
        conn.commit()
    return len(gone)


def price_history(conn: sqlite3.Connection, item_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM listing_price_history WHERE item_id = ? ORDER BY observed_at",
        (item_id,),
    ).fetchall()


def latest_price_drop(conn: sqlite3.Connection, item_id: str) -> dict[str, Any] | None:
    """Compare the two most recent distinct observed prices for a listing."""
    rows = conn.execute(
        """SELECT price FROM listing_price_history
           WHERE item_id = ? AND price IS NOT NULL
           ORDER BY observed_at DESC, id DESC LIMIT 50""",
        (item_id,),
    ).fetchall()
    prices = [r["price"] for r in rows]
    if len(prices) < 2:
        return None
    current = prices[0]
    previous = next((p for p in prices[1:] if abs(p - current) >= 0.01), None)
    if previous is None or current >= previous:
        return None
    drop = previous - current
    return {
        "previous_price": previous,
        "current_price": current,
        "drop": round(drop, 2),
        "drop_pct": round(drop / previous * 100, 1) if previous else None,
    }


# --- candidate scores -------------------------------------------------------

def save_candidate_scores(conn: sqlite3.Connection, scan_run_id: int,
                          scores: Iterable[dict[str, Any]]) -> None:
    ts = now()
    conn.executemany(
        """INSERT INTO candidate_scores (scan_run_id, item_id, reference,
            total_acquisition, market_mid, price_ratio, gross_spread, required_spread,
            passed_prefilter, reasons, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (scan_run_id, s["item_id"], s.get("reference"), s.get("total_acquisition"),
             s.get("market_mid"), s.get("price_ratio"), s.get("gross_spread"),
             s.get("required_spread"), int(bool(s.get("passed"))),
             json.dumps(s.get("reasons", [])), ts)
            for s in scores
        ],
    )
    conn.commit()
