"""Local historical market evidence (Phase 5.3 §9).

The scanner has been watching UK listings since Phase 4. This collector turns
that accumulated history into structured evidence — supply, price drops, time on
market — and it is enabled by default because it needs no network, no permission
and no browser.

**The load-bearing rule: a listing disappearing does not mean it sold.**

Phase 5.3 tightens the Phase 4 vocabulary accordingly. Phase 4's `LIKELY_SOLD`
was an inference from a listing vanishing at a stable price. That inference is
still useful for liquidity, but it is not a confirmed sale, so it maps to
`ENDED_UNKNOWN` here. `CONFIRMED_SOLD` is reserved for listings corroborated by
an actual sold record — a manual import or licensed API row that matches the
reference and price.

The Phase 4 statuses are left untouched in `observation.py`; this module
translates rather than rewrites, so existing behaviour and tests stand.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import observation as obs
from ..watchlist import normalise
from .base import (EV_OBSERVATION, EV_SOLD, HEALTH_OK, PRICE_UNKNOWN,
                   CONFIRMED_PRICE, BaseCollector, CollectedEvidence,
                   CollectionResult, classify_match, days_since)

# --- Phase 5.3 lifecycle statuses (§9) --------------------------------------

ACTIVE = "ACTIVE"
ENDED_UNKNOWN = "ENDED_UNKNOWN"
CONFIRMED_SOLD = "CONFIRMED_SOLD"
RELISTED = "RELISTED"
STALE = "STALE"

STATUS_LABEL = {
    ACTIVE: "Active",
    ENDED_UNKNOWN: "Ended — outcome unknown",
    CONFIRMED_SOLD: "Confirmed sold",
    RELISTED: "Relisted",
    STALE: "Stale — listed a long time without selling",
}

# Phase 4 status -> Phase 5.3 status. Note that every inference-based Phase 4
# status collapses to ENDED_UNKNOWN: none of them is corroborated.
PHASE4_STATUS_MAP = {
    obs.ACTIVE: ACTIVE,
    obs.RELISTED: RELISTED,
    obs.LIKELY_SOLD: ENDED_UNKNOWN,
    obs.POSSIBLY_SOLD: ENDED_UNKNOWN,
    obs.DELISTED_UNKNOWN: ENDED_UNKNOWN,
}

# An active listing older than this has demonstrably not sold at its asking price.
STALE_AFTER_DAYS = 90
# Corroboration tolerance when matching an ended listing to a known sale.
PRICE_MATCH_TOLERANCE = 0.08
DATE_MATCH_WINDOW_DAYS = 45


@dataclass
class ListingHistory:
    """One listing's full lifecycle (§9)."""

    item_id: str
    reference: str | None
    first_seen: str | None
    last_seen: str | None
    initial_price: float | None
    latest_price: float | None
    lowest_price_seen: float | None
    price_change_count: int
    status: str
    days_on_market: float | None
    confirmed_by: str | None = None
    url: str | None = None

    @property
    def status_label(self) -> str:
        return STATUS_LABEL.get(self.status, self.status)

    @property
    def total_drop_pct(self) -> float | None:
        if not self.initial_price or not self.latest_price:
            return None
        return round((self.latest_price - self.initial_price)
                     / self.initial_price * 100, 2)

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "status_label": self.status_label,
                "total_drop_pct": self.total_drop_pct}


def _confirmed_sales(conn: sqlite3.Connection, reference: str
                     ) -> list[tuple[float, str]]:
    """Known real sales for corroboration: (price, date)."""
    try:
        from ..manual_evidence import init_manual_schema
        init_manual_schema(conn)
        rows = conn.execute(
            "SELECT sold_price_gbp, sold_date FROM manual_sold_evidence "
            "WHERE reference_key = ?", (normalise(reference),)).fetchall()
        return [(r["sold_price_gbp"], r["sold_date"]) for r in rows]
    except Exception:
        return []


def _corroborates(price: float | None, ended_at: str | None,
                  sales: list[tuple[float, str]]) -> str | None:
    """Does a known sale match this ended listing closely enough to confirm it?

    Requires BOTH a price within tolerance and a date within the window. Either
    alone is coincidence, not corroboration.
    """
    if price is None or not sales:
        return None
    ended_age = days_since(ended_at)
    for sale_price, sale_date in sales:
        if not sale_price:
            continue
        if abs(sale_price - price) / max(price, 1) > PRICE_MATCH_TOLERANCE:
            continue
        sale_age = days_since(sale_date)
        if ended_age is not None and sale_age is not None:
            if abs(sale_age - ended_age) > DATE_MATCH_WINDOW_DAYS:
                continue
        return f"matched sold record £{sale_price:,.0f} on {sale_date}"
    return None


def listing_histories(conn: sqlite3.Connection, reference: str,
                      lookback_days: int = 365) -> list[ListingHistory]:
    """Full lifecycle for every tracked listing of one reference."""
    observations = obs.observations_for(conn, reference, lookback_days)
    sales = _confirmed_sales(conn, reference)

    out: list[ListingHistory] = []
    for o in observations:
        prices = [r["price"] for r in conn.execute(
            """SELECT price FROM listing_price_history
               WHERE item_id = ? AND price IS NOT NULL
               ORDER BY observed_at, id""", (o.item_id,)).fetchall()]

        status = PHASE4_STATUS_MAP.get(o.status, ENDED_UNKNOWN)
        confirmed_by = None

        if status == ENDED_UNKNOWN:
            confirmed_by = _corroborates(o.last_price, o.disappearance_date, sales)
            if confirmed_by:
                status = CONFIRMED_SOLD
        elif status == ACTIVE:
            age = days_since(o.first_seen_at)
            if age is not None and age > STALE_AFTER_DAYS:
                status = STALE

        out.append(ListingHistory(
            item_id=o.item_id, reference=o.reference,
            first_seen=o.first_seen_at, last_seen=o.last_seen_at,
            initial_price=o.first_price, latest_price=o.last_price,
            lowest_price_seen=round(min(prices), 2) if prices else o.last_price,
            price_change_count=o.price_drops,
            status=status, days_on_market=o.active_duration_days,
            confirmed_by=confirmed_by, url=None))
    return out


@dataclass
class LocalMarketHistory:
    """Aggregate UK supply and turnover picture from our own scans."""

    reference: str
    lookback_days: int
    histories: list[ListingHistory] = field(default_factory=list)

    def _count(self, status: str) -> int:
        return sum(1 for h in self.histories if h.status == status)

    @property
    def active(self) -> int:
        return self._count(ACTIVE) + self._count(STALE)

    @property
    def stale(self) -> int:
        return self._count(STALE)

    @property
    def confirmed_sold(self) -> int:
        return self._count(CONFIRMED_SOLD)

    @property
    def ended_unknown(self) -> int:
        return self._count(ENDED_UNKNOWN)

    @property
    def relisted(self) -> int:
        return self._count(RELISTED)

    @property
    def median_days_on_market(self) -> float | None:
        import statistics
        values = [h.days_on_market for h in self.histories
                  if h.days_on_market and h.status != ACTIVE]
        return round(statistics.median(values), 1) if len(values) >= 3 else None

    @property
    def median_price_drop_pct(self) -> float | None:
        import statistics
        drops = [h.total_drop_pct for h in self.histories
                 if h.total_drop_pct is not None and h.total_drop_pct < 0]
        return round(statistics.median(drops), 2) if len(drops) >= 3 else None

    def describe(self) -> str:
        if not self.histories:
            return "No local listing history for this reference yet."
        return (f"Local UK history over {self.lookback_days} days: "
                f"{len(self.histories)} listing(s) tracked — {self.active} active "
                f"({self.stale} stale), {self.confirmed_sold} confirmed sold, "
                f"{self.ended_unknown} ended with unknown outcome, "
                f"{self.relisted} relisted. Ending a listing is not a sale.")

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "tracked": len(self.histories),
            "active": self.active, "stale": self.stale,
            "confirmed_sold": self.confirmed_sold,
            "ended_unknown": self.ended_unknown, "relisted": self.relisted,
            "median_days_on_market": self.median_days_on_market,
            "median_price_drop_pct": self.median_price_drop_pct,
        }


def market_history(conn: sqlite3.Connection, reference: str,
                   lookback_days: int = 365) -> LocalMarketHistory:
    return LocalMarketHistory(reference, lookback_days,
                              listing_histories(conn, reference, lookback_days))


class LocalHistoryCollector(BaseCollector):
    """Enabled by default — our own data, no network, no permission needed."""

    name = "local_history"
    evidence_type = EV_OBSERVATION

    def __init__(self, conn: sqlite3.Connection, enabled: bool = True,
                 lookback_days: int = 365):
        super().__init__(enabled=enabled)
        self.conn = conn
        self.lookback_days = lookback_days

    def is_available(self) -> bool:
        return self.conn is not None

    def _collect(self, reference: str, brand: str, model: str,
                 **kwargs: Any) -> CollectionResult:
        history = market_history(self.conn, reference, self.lookback_days)
        if not history.histories:
            return CollectionResult.unavailable(
                self.name, reference, "No local history for this reference yet.",
                status=HEALTH_OK)

        records: list[CollectedEvidence] = []
        for h in history.histories:
            if h.status == CONFIRMED_SOLD:
                # Corroborated by a real sold record, so it is a sale.
                records.append(CollectedEvidence(
                    source=self.name, evidence_type=EV_SOLD,
                    reference=reference, brand=brand, model=model,
                    price_gbp=h.latest_price, original_price=h.latest_price,
                    sale_date=h.last_seen, item_id=f"local:{h.item_id}",
                    price_certainty=CONFIRMED_PRICE,
                    match_type=classify_match(reference, reference, model, brand),
                    notes=f"Local history confirmed sold ({h.confirmed_by})."))
            else:
                # Everything else is observation, never a sale.
                records.append(CollectedEvidence(
                    source=self.name, evidence_type=EV_OBSERVATION,
                    reference=reference, brand=brand, model=model,
                    price_gbp=None, original_price=h.latest_price,
                    item_id=f"local:{h.item_id}",
                    price_certainty=PRICE_UNKNOWN,
                    match_type=classify_match(reference, reference, model, brand),
                    notes=f"Local observation: {h.status_label}."))

        return CollectionResult.ok(
            self.name, reference, records, detail=history.describe())
