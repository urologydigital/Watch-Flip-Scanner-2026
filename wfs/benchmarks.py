"""Manually verified market benchmarks (spec 5.2 §2, §16).

No paid API is required anywhere in this module. The user clicks "Check
WatchCharts", reads the figure themselves, and types it in. That is slower than
an API call and considerably cheaper than a subscription.

The semantic rule this module enforces: **a benchmark is not a sale.** A
WatchCharts market value and a Chrono24 asking price tell you different things,
and neither is a confirmed transaction. The evidence kind is derived from the
source, not chosen by the user, so a Chrono24 figure can never be filed as sold.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .watchlist import normalise

# --- source types and their FIXED semantic meaning --------------------------

MANUAL_EBAY_SOLD = "MANUAL_EBAY_SOLD"
MANUAL_WATCHCHARTS = "MANUAL_WATCHCHARTS"
MANUAL_CHRONO24 = "MANUAL_CHRONO24"
OTHER_CONFIRMED = "OTHER_CONFIRMED"
LOCAL_OBSERVATION = "LOCAL_OBSERVATION"

KIND_SOLD = "CONFIRMED_SOLD"
KIND_BENCHMARK = "MARKET_BENCHMARK"
KIND_ACTIVE_ASKING = "ACTIVE_ASKING"
KIND_OBSERVATION = "LOCAL_OBSERVATION"

# Source -> evidence kind. This mapping is NOT user-editable: it is what stops a
# Chrono24 asking price being recorded as a sale.
SOURCE_KIND: dict[str, str] = {
    MANUAL_EBAY_SOLD: KIND_SOLD,
    OTHER_CONFIRMED: KIND_SOLD,
    MANUAL_WATCHCHARTS: KIND_BENCHMARK,
    MANUAL_CHRONO24: KIND_ACTIVE_ASKING,
    LOCAL_OBSERVATION: KIND_OBSERVATION,
}

SOURCE_LABEL = {
    MANUAL_EBAY_SOLD: "Manual eBay sold record",
    MANUAL_WATCHCHARTS: "Manual WatchCharts benchmark",
    MANUAL_CHRONO24: "Manual Chrono24 asking price",
    OTHER_CONFIRMED: "Other confirmed sale",
    LOCAL_OBSERVATION: "Local scanner observation",
}

# Evidence levels from spec §9. Only LEVEL A is a confirmed sale.
LEVEL_A, LEVEL_B, LEVEL_C, LEVEL_D = "A", "B", "C", "D"

KIND_LEVEL = {
    KIND_SOLD: LEVEL_A,
    KIND_BENCHMARK: LEVEL_B,
    KIND_ACTIVE_ASKING: LEVEL_C,
    KIND_OBSERVATION: LEVEL_D,
}

BENCHMARK_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_benchmarks (
    id INTEGER PRIMARY KEY,
    reference TEXT NOT NULL,
    reference_key TEXT NOT NULL,
    brand TEXT,
    source TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    value REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'GBP',
    low_value REAL,
    high_value REAL,
    sample_size INTEGER,
    recorded_at TEXT NOT NULL,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_benchmark_ref ON market_benchmarks (reference_key);
"""


def init_benchmark_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(BENCHMARK_SCHEMA)
    conn.commit()


def kind_for_source(source: str) -> str:
    """Derive the evidence kind. Never accepts a caller-supplied kind."""
    kind = SOURCE_KIND.get((source or "").upper())
    if kind is None:
        raise ValueError(f"Unknown benchmark source: {source!r}")
    return kind


def is_sold_evidence(source: str) -> bool:
    return kind_for_source(source) == KIND_SOLD


@dataclass
class Benchmark:
    reference: str
    source: str
    value: float
    currency: str = "GBP"
    low_value: float | None = None
    high_value: float | None = None
    sample_size: int | None = None
    recorded_at: str | None = None
    brand: str | None = None
    notes: str | None = None

    @property
    def evidence_kind(self) -> str:
        return kind_for_source(self.source)

    @property
    def evidence_level(self) -> str:
        return KIND_LEVEL[self.evidence_kind]

    @property
    def is_confirmed_sold(self) -> bool:
        return self.evidence_kind == KIND_SOLD

    @property
    def source_label(self) -> str:
        return SOURCE_LABEL.get(self.source, self.source)

    @property
    def age_days(self) -> float | None:
        if not self.recorded_at:
            return None
        try:
            when = datetime.fromisoformat(self.recorded_at)
        except (ValueError, TypeError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - when).total_seconds() / 86400, 1)

    def describe(self) -> str:
        age = f", recorded {self.age_days:.0f} days ago" if self.age_days else ""
        return (f"{self.source_label}: £{self.value:,.0f} "
                f"[{self.evidence_kind}{age}]")

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "brand": self.brand,
            "source": self.source,
            "source_label": self.source_label,
            "evidence_kind": self.evidence_kind,
            "evidence_level": self.evidence_level,
            "is_confirmed_sold": self.is_confirmed_sold,
            "value": self.value,
            "currency": self.currency,
            "low_value": self.low_value,
            "high_value": self.high_value,
            "sample_size": self.sample_size,
            "recorded_at": self.recorded_at,
            "age_days": self.age_days,
            "notes": self.notes,
        }


def record_benchmark(conn: sqlite3.Connection, reference: str, source: str,
                     value: float, currency: str = "GBP",
                     low_value: float | None = None,
                     high_value: float | None = None,
                     sample_size: int | None = None, brand: str | None = None,
                     notes: str | None = None) -> Benchmark:
    """Store a manually verified benchmark. The kind is derived, never supplied."""
    kind = kind_for_source(source)          # raises on an unknown source
    if value is None or value <= 0:
        raise ValueError("Benchmark value must be greater than zero.")
    if currency.upper() != "GBP":
        # Kept explicit rather than silently converting at an invented rate.
        raise ValueError("Only GBP benchmarks are supported; convert first.")

    init_benchmark_schema(conn)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO market_benchmarks
           (reference, reference_key, brand, source, evidence_kind, value,
            currency, low_value, high_value, sample_size, recorded_at, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (reference, normalise(reference), brand, source.upper(), kind,
         round(float(value), 2), currency.upper(), low_value, high_value,
         sample_size, now, notes))
    conn.commit()
    return Benchmark(reference, source.upper(), round(float(value), 2), currency.upper(),
                     low_value, high_value, sample_size, now, brand, notes)


def latest_benchmark(conn: sqlite3.Connection, reference: str,
                     source: str | None = None,
                     max_age_days: int | None = 120) -> Benchmark | None:
    """Most recent benchmark for a reference, optionally filtered by source."""
    init_benchmark_schema(conn)
    query = ("SELECT * FROM market_benchmarks WHERE reference_key = ?")
    params: list[Any] = [normalise(reference)]
    if source:
        query += " AND source = ?"
        params.append(source.upper())
    query += " ORDER BY recorded_at DESC LIMIT 1"
    row = conn.execute(query, params).fetchone()
    if row is None:
        return None
    bench = Benchmark(
        reference=row["reference"], source=row["source"], value=row["value"],
        currency=row["currency"], low_value=row["low_value"],
        high_value=row["high_value"], sample_size=row["sample_size"],
        recorded_at=row["recorded_at"], brand=row["brand"], notes=row["notes"])
    if max_age_days is not None and bench.age_days is not None:
        if bench.age_days > max_age_days:
            return None       # a stale benchmark is not market context
    return bench


def all_benchmarks(conn: sqlite3.Connection,
                   reference: str | None = None) -> list[dict[str, Any]]:
    init_benchmark_schema(conn)
    if reference:
        rows = conn.execute(
            "SELECT * FROM market_benchmarks WHERE reference_key = ? "
            "ORDER BY recorded_at DESC", (normalise(reference),)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM market_benchmarks ORDER BY recorded_at DESC").fetchall()
    return [dict(r) for r in rows]


def benchmark_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    init_benchmark_schema(conn)
    rows = conn.execute(
        """SELECT evidence_kind, COUNT(*) AS n FROM market_benchmarks
           GROUP BY evidence_kind""").fetchall()
    counts = {r["evidence_kind"]: r["n"] for r in rows}
    total = conn.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT reference_key) AS refs "
                         "FROM market_benchmarks").fetchone()
    return {"records": total["n"], "references": total["refs"], "by_kind": counts}
