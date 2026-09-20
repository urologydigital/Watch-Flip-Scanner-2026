"""Collected-evidence storage and aggregation (Phase 5.3 §11, §12, §14, §15).

Additive only. The migration creates two new tables and touches nothing that
already exists, so every scan, listing, price observation and manual record from
Phase 5.2.1 survives untouched.

Deduplication is by fingerprint: collecting the same sale twice refreshes one
row rather than inventing a second transaction. That distinction is the whole
point — a duplicated sale would inflate both the valuation sample and the
liquidity estimate.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .collectors.base import (BEST_OFFER_UNCERTAIN, EV_ACTIVE_ASKING,
                              EV_MARKET_CONTEXT, EV_OBSERVATION, EV_SOLD,
                              MATCH_EXACT, MATCH_FAMILY, MATCH_NORMALIZED,
                              CollectedEvidence, days_since, recency_band,
                              recency_weight)
from .watchlist import normalise

EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS collected_evidence (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    reference TEXT NOT NULL,
    reference_key TEXT NOT NULL,
    brand TEXT,
    model TEXT,
    title TEXT,
    price_gbp REAL,
    original_price REAL,
    original_currency TEXT DEFAULT 'GBP',
    sale_date TEXT,
    condition TEXT,
    full_set INTEGER,
    listing_url TEXT,
    item_id TEXT,
    seller_location TEXT,
    retrieved_at TEXT NOT NULL,
    first_collected_at TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    price_certainty TEXT NOT NULL,
    match_type TEXT NOT NULL,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_evidence_ref ON collected_evidence (reference_key);
CREATE INDEX IF NOT EXISTS idx_evidence_type ON collected_evidence (evidence_type);
CREATE INDEX IF NOT EXISTS idx_evidence_source ON collected_evidence (source);
CREATE INDEX IF NOT EXISTS idx_evidence_date ON collected_evidence (sale_date);

CREATE TABLE IF NOT EXISTS source_health (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    record_count INTEGER DEFAULT 0,
    last_success TEXT,
    last_attempt TEXT,
    cache_status TEXT,
    error TEXT,
    detail TEXT
);
"""


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Create the Phase 5.3 tables. Idempotent, additive, data-preserving."""
    before = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.executescript(EVIDENCE_SCHEMA)
    conn.commit()
    after = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    return sorted(after - before)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- persistence ------------------------------------------------------------

def store_evidence(conn: sqlite3.Connection,
                   records: list[CollectedEvidence]) -> dict[str, int]:
    """Insert or refresh. Returns counts of inserted vs refreshed."""
    migrate(conn)
    inserted = refreshed = 0
    now = _now()

    for rec in records:
        fp = rec.fingerprint()
        existing = conn.execute(
            "SELECT id, first_collected_at FROM collected_evidence "
            "WHERE fingerprint = ?", (fp,)).fetchone()

        payload = {
            "fingerprint": fp,
            "source": rec.source,
            "evidence_type": rec.evidence_type,
            "reference": rec.reference,
            "reference_key": normalise(rec.reference),
            "brand": rec.brand, "model": rec.model, "title": rec.title,
            "price_gbp": rec.price_gbp,
            "original_price": rec.original_price,
            "original_currency": rec.original_currency,
            "sale_date": rec.sale_date, "condition": rec.condition,
            "full_set": None if rec.full_set is None else int(rec.full_set),
            "listing_url": rec.listing_url, "item_id": rec.item_id,
            "seller_location": rec.seller_location,
            "retrieved_at": rec.retrieved_at or now,
            "confidence": rec.confidence,
            "price_certainty": rec.price_certainty,
            "match_type": rec.match_type, "notes": rec.notes,
        }

        if existing:
            # Refresh in place — never a second apparent transaction.
            conn.execute(
                """UPDATE collected_evidence SET
                    price_gbp = :price_gbp, original_price = :original_price,
                    original_currency = :original_currency, sale_date = :sale_date,
                    condition = :condition, full_set = :full_set,
                    listing_url = :listing_url, seller_location = :seller_location,
                    retrieved_at = :retrieved_at, confidence = :confidence,
                    price_certainty = :price_certainty, match_type = :match_type,
                    notes = :notes, title = :title
                   WHERE fingerprint = :fingerprint""", payload)
            refreshed += 1
        else:
            payload["first_collected_at"] = now
            conn.execute(
                """INSERT INTO collected_evidence
                   (fingerprint, source, evidence_type, reference, reference_key,
                    brand, model, title, price_gbp, original_price,
                    original_currency, sale_date, condition, full_set,
                    listing_url, item_id, seller_location, retrieved_at,
                    first_collected_at, confidence, price_certainty, match_type,
                    notes)
                   VALUES (:fingerprint, :source, :evidence_type, :reference,
                    :reference_key, :brand, :model, :title, :price_gbp,
                    :original_price, :original_currency, :sale_date, :condition,
                    :full_set, :listing_url, :item_id, :seller_location,
                    :retrieved_at, :first_collected_at, :confidence,
                    :price_certainty, :match_type, :notes)""", payload)
            inserted += 1

    conn.commit()
    return {"inserted": inserted, "refreshed": refreshed,
            "total": inserted + refreshed}


def load_evidence(conn: sqlite3.Connection, reference: str,
                  evidence_type: str | None = None,
                  max_age_days: int | None = None) -> list[CollectedEvidence]:
    migrate(conn)
    query = "SELECT * FROM collected_evidence WHERE reference_key = ?"
    params: list[Any] = [normalise(reference)]
    if evidence_type:
        query += " AND evidence_type = ?"
        params.append(evidence_type)
    rows = conn.execute(query, params).fetchall()

    out: list[CollectedEvidence] = []
    for r in rows:
        if max_age_days is not None:
            age = days_since(r["sale_date"] or r["retrieved_at"])
            if age is not None and age > max_age_days:
                continue
        out.append(CollectedEvidence(
            source=r["source"], evidence_type=r["evidence_type"],
            reference=r["reference"], price_gbp=r["price_gbp"],
            original_price=r["original_price"],
            original_currency=r["original_currency"] or "GBP",
            brand=r["brand"], model=r["model"], title=r["title"],
            sale_date=r["sale_date"], condition=r["condition"],
            full_set=None if r["full_set"] is None else bool(r["full_set"]),
            listing_url=r["listing_url"], item_id=r["item_id"],
            seller_location=r["seller_location"],
            retrieved_at=r["retrieved_at"], confidence=r["confidence"] or 1.0,
            price_certainty=r["price_certainty"], match_type=r["match_type"],
            notes=r["notes"]))
    return out


def record_source_health(conn: sqlite3.Connection, status) -> None:
    migrate(conn)
    now = _now()
    conn.execute(
        """INSERT INTO source_health
           (source, status, record_count, last_success, last_attempt,
            cache_status, error, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (source) DO UPDATE SET
             status = excluded.status,
             record_count = excluded.record_count,
             last_success = COALESCE(excluded.last_success, source_health.last_success),
             last_attempt = excluded.last_attempt,
             cache_status = excluded.cache_status,
             error = excluded.error,
             detail = excluded.detail""",
        (status.source, status.status, status.record_count, status.last_success,
         now, status.cache_status, status.error, status.detail))
    conn.commit()


def source_health(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    migrate(conn)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM source_health ORDER BY source").fetchall()]


def evidence_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    migrate(conn)
    row = conn.execute(
        """SELECT COUNT(*) AS n, COUNT(DISTINCT reference_key) AS refs,
                  MAX(retrieved_at) AS latest FROM collected_evidence""").fetchone()
    by_type = {r["evidence_type"]: r["n"] for r in conn.execute(
        "SELECT evidence_type, COUNT(*) AS n FROM collected_evidence "
        "GROUP BY evidence_type").fetchall()}
    by_source = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) AS n FROM collected_evidence "
        "GROUP BY source").fetchall()}
    return {"records": row["n"], "references": row["refs"],
            "last_updated": row["latest"], "by_type": by_type,
            "by_source": by_source}


# --- aggregation (§14, §15) -------------------------------------------------

@dataclass
class AggregatedEvidence:
    """Robust statistics over collected sold evidence for one reference."""

    reference: str
    sold_records: list[CollectedEvidence] = field(default_factory=list)
    asking_records: list[CollectedEvidence] = field(default_factory=list)
    context_records: list[CollectedEvidence] = field(default_factory=list)

    # --- sold statistics ---------------------------------------------------
    @property
    def priced_sales(self) -> list[CollectedEvidence]:
        """Sales we can actually put a number on (excludes Best Offer)."""
        return [r for r in self.sold_records if r.usable_for_valuation]

    @property
    def liquidity_sales(self) -> list[CollectedEvidence]:
        """Sales that prove demand, including price-undisclosed Best Offers."""
        return [r for r in self.sold_records if r.counts_for_liquidity]

    @property
    def best_offer_count(self) -> int:
        return sum(1 for r in self.sold_records
                   if r.price_certainty == BEST_OFFER_UNCERTAIN)

    @property
    def exact_sales(self) -> list[CollectedEvidence]:
        return [r for r in self.liquidity_sales
                if r.match_type in (MATCH_EXACT, MATCH_NORMALIZED)]

    @property
    def family_sales(self) -> list[CollectedEvidence]:
        """Kept separate from exact matches, never merged into them (§10)."""
        return [r for r in self.liquidity_sales if r.match_type == MATCH_FAMILY]

    def sales_within(self, days: int, exact_only: bool = True) -> int:
        pool = self.exact_sales if exact_only else self.liquidity_sales
        return sum(1 for r in pool
                   if (days_since(r.sale_date) or 1e9) <= days)

    def _weighted_prices(self) -> list[tuple[float, float]]:
        """(price, weight) pairs combining certainty, match quality and recency."""
        out = []
        for r in self.priced_sales:
            weight = r.valuation_weight * recency_weight(r.sale_date)
            if weight > 0:
                out.append((r.price_gbp, weight))
        return out

    @property
    def effective_sample(self) -> float:
        """Sum of weights — the sample size after discounting."""
        return round(sum(w for _, w in self._weighted_prices()), 2)

    def robust_band(self) -> tuple[float | None, float | None, float | None]:
        """(low, mid, high) from weighted, outlier-trimmed sold prices.

        Uses the weighted median and IQR. With very few points the band is a
        narrow envelope around the median rather than an invented spread.
        """
        pairs = self._weighted_prices()
        if not pairs:
            return (None, None, None)

        prices = _trim_outliers([p for p, _ in pairs])
        if not prices:
            return (None, None, None)
        kept = {p for p in prices}
        pairs = [(p, w) for p, w in pairs if p in kept] or pairs

        mid = _weighted_median(pairs)
        if mid is None:
            return (None, None, None)
        if len(pairs) >= 4:
            ordered = sorted(p for p, _ in pairs)
            lo = ordered[max(0, int(len(ordered) * 0.25) - 1)]
            hi = ordered[min(len(ordered) - 1, int(len(ordered) * 0.75))]
        else:
            lo, hi = mid * 0.94, mid * 1.06
        return (round(lo, 2), round(mid, 2), round(hi, 2))

    @property
    def dispersion(self) -> float | None:
        prices = [r.price_gbp for r in self.priced_sales]
        if len(prices) < 3:
            return None
        mean = statistics.mean(prices)
        return round(statistics.pstdev(prices) / mean, 3) if mean else None

    @property
    def newest_sale_days(self) -> float | None:
        ages = [days_since(r.sale_date) for r in self.liquidity_sales]
        ages = [a for a in ages if a is not None]
        return round(min(ages), 1) if ages else None

    def best_match_type(self) -> str | None:
        for level in (MATCH_EXACT, MATCH_NORMALIZED, MATCH_FAMILY):
            if any(r.match_type == level for r in self.liquidity_sales):
                return level
        return None

    def as_dict(self) -> dict[str, Any]:
        low, mid, high = self.robust_band()
        return {
            "reference": self.reference,
            "sold_records": len(self.sold_records),
            "priced_sales": len(self.priced_sales),
            "liquidity_sales": len(self.liquidity_sales),
            "best_offer_uncertain": self.best_offer_count,
            "exact_sales": len(self.exact_sales),
            "family_sales": len(self.family_sales),
            "sales_30d": self.sales_within(30),
            "sales_90d": self.sales_within(90),
            "sales_365d": self.sales_within(365),
            "effective_sample": self.effective_sample,
            "market_low": low, "market_mid": mid, "market_high": high,
            "dispersion": self.dispersion,
            "newest_sale_days": self.newest_sale_days,
            "best_match_type": self.best_match_type(),
            "asking_records": len(self.asking_records),
            "context_records": len(self.context_records),
        }


def _weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    if not pairs:
        return None
    ordered = sorted(pairs, key=lambda p: p[0])
    total = sum(w for _, w in ordered)
    if total <= 0:
        return None
    running = 0.0
    for price, weight in ordered:
        running += weight
        if running >= total / 2:
            return price
    return ordered[-1][0]


def _trim_outliers(prices: list[float], threshold: float = 3.5) -> list[float]:
    """Median absolute deviation trim — robust to a handful of wild prices."""
    if len(prices) < 4:
        return prices
    median = statistics.median(prices)
    mad = statistics.median([abs(p - median) for p in prices])
    if mad <= 0:
        return prices
    kept = [p for p in prices if abs(p - median) / (1.4826 * mad) <= threshold]
    return kept if len(kept) >= 3 else prices


def aggregate(conn: sqlite3.Connection, reference: str,
              max_age_days: int | None = 365) -> AggregatedEvidence:
    """Pull everything stored for a reference and split it by evidence type."""
    records = load_evidence(conn, reference, max_age_days=max_age_days)
    agg = AggregatedEvidence(reference=reference)
    for r in records:
        if r.evidence_type == EV_SOLD:
            agg.sold_records.append(r)
        elif r.evidence_type == EV_ACTIVE_ASKING:
            agg.asking_records.append(r)
        elif r.evidence_type in (EV_MARKET_CONTEXT, EV_OBSERVATION):
            agg.context_records.append(r)
    return agg
