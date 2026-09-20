"""Local observation evidence (spec 5.1 §12, §17).

The scanner accumulates its own market picture from listings it has watched:
when each appeared, how the price moved, and whether it vanished. Over time this
becomes the most reliable evidence available without a paid data feed.

The hard rule: **a disappearance is not a sale.** Listings vanish because they
sold, because the seller ended them, or because they expired. Every observation
is classified conservatively and the language used throughout is "observed market
activity", never "confirmed sales".
"""
from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

LIKELY_SOLD = "LIKELY_SOLD"
POSSIBLY_SOLD = "POSSIBLY_SOLD"
DELISTED_UNKNOWN = "DELISTED_UNKNOWN"
RELISTED = "RELISTED"
ACTIVE = "ACTIVE"

CLASSIFICATION_LABEL = {
    LIKELY_SOLD: "Likely sold",
    POSSIBLY_SOLD: "Possibly sold",
    DELISTED_UNKNOWN: "Delisted — outcome unknown",
    RELISTED: "Relisted",
    ACTIVE: "Still active",
}

# Only LIKELY_SOLD contributes to a price estimate, and even then it is weighted
# below genuine sold data.
PRICE_ELIGIBLE = {LIKELY_SOLD}


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class Observation:
    item_id: str
    reference: str | None
    first_seen_at: str | None
    last_seen_at: str | None
    disappearance_date: str | None
    last_price: float | None
    first_price: float | None
    times_seen: int
    status: str
    active_duration_days: float | None
    price_drops: int
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "status_label": CLASSIFICATION_LABEL.get(self.status,
                                                                          self.status)}


def classify(row: sqlite3.Row, price_history: list[float],
             reappeared: bool = False) -> tuple[str, str]:
    """Conservatively classify one listing's outcome.

    A short, stable listing that vanishes is the strongest available hint of a
    sale — but it remains a hint. Anything ambiguous is DELISTED_UNKNOWN.
    """
    if not row["disappeared_at"]:
        return ACTIVE, "Listing is still visible."

    if reappeared:
        return RELISTED, ("Listing vanished and later returned — a relist, not a "
                          "sale.")

    duration = None
    start, end = _dt(row["first_seen"]), _dt(row["disappeared_at"])
    if start and end:
        duration = (end - start).total_seconds() / 86400

    drops = sum(1 for a, b in zip(price_history, price_history[1:]) if b < a - 0.009)
    times_seen = row["times_seen"] or 1

    # Seen in several scans then gone, without repeated discounting: the most
    # sale-like pattern we can observe from outside.
    if times_seen >= 2 and drops == 0 and (duration is None or duration <= 60):
        return LIKELY_SOLD, ("Observed across multiple scans at a stable price, then "
                             "disappeared — consistent with a sale, but unconfirmed.")

    if times_seen >= 2 and drops <= 1:
        return POSSIBLY_SOLD, ("Disappeared after limited price movement — may have "
                               "sold or may have been ended.")

    if drops >= 2:
        return DELISTED_UNKNOWN, ("Price was cut repeatedly before the listing "
                                  "vanished — an unsold ending is as likely as a sale.")

    if times_seen <= 1:
        return DELISTED_UNKNOWN, ("Seen only once before disappearing — too little "
                                  "observation to infer anything.")

    return DELISTED_UNKNOWN, "Insufficient pattern to classify the outcome."


def observations_for(conn: sqlite3.Connection, reference: str,
                     lookback_days: int = 180) -> list[Observation]:
    """Every listing the scanner has tracked for one reference."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days))
    rows = conn.execute(
        "SELECT * FROM listings WHERE reference = ? ORDER BY first_seen DESC",
        (reference,)).fetchall()

    out: list[Observation] = []
    for row in rows:
        first = _dt(row["first_seen"])
        if first and first < cutoff:
            continue
        history = [r["price"] for r in conn.execute(
            """SELECT price FROM listing_price_history
               WHERE item_id = ? AND price IS NOT NULL
               ORDER BY observed_at, id""", (row["item_id"],)).fetchall()]

        status, rationale = classify(row, history)
        end = row["disappeared_at"] or row["last_seen"]
        duration = None
        if first and _dt(end):
            duration = round((_dt(end) - first).total_seconds() / 86400, 2)

        out.append(Observation(
            item_id=row["item_id"],
            reference=row["reference"],
            first_seen_at=row["first_seen"],
            last_seen_at=row["last_seen"],
            disappearance_date=row["disappeared_at"],
            last_price=history[-1] if history else row["price"],
            first_price=row["first_price"] if row["first_price"] is not None
            else (history[0] if history else None),
            times_seen=row["times_seen"] or 1,
            status=status,
            active_duration_days=duration,
            price_drops=sum(1 for a, b in zip(history, history[1:])
                            if b < a - 0.009),
            rationale=rationale,
        ))
    return out


@dataclass
class ObservedActivity:
    """Aggregate market activity for one reference, from local observation only."""

    reference: str
    lookback_days: int
    total_tracked: int = 0
    still_active: int = 0
    likely_sold: int = 0
    possibly_sold: int = 0
    delisted_unknown: int = 0
    relisted: int = 0
    likely_sold_prices: list[float] = field(default_factory=list)
    median_active_duration_days: float | None = None
    observations: list[Observation] = field(default_factory=list)

    @property
    def has_activity(self) -> bool:
        """Enough signal to be worth using at all."""
        return self.likely_sold >= 2

    @property
    def median_likely_sold_price(self) -> float | None:
        if not self.likely_sold_prices:
            return None
        return round(statistics.median(self.likely_sold_prices), 2)

    def describe(self) -> str:
        if self.total_tracked == 0:
            return "No local observation history for this reference yet."
        return (f"Observed market activity over {self.lookback_days} days: "
                f"{self.total_tracked} listing(s) tracked, {self.likely_sold} likely "
                f"sold, {self.possibly_sold} possibly sold, "
                f"{self.delisted_unknown} delisted with unknown outcome, "
                f"{self.still_active} still active. Disappearance is not confirmed "
                "as a sale.")

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "lookback_days": self.lookback_days,
            "total_tracked": self.total_tracked,
            "still_active": self.still_active,
            "likely_sold": self.likely_sold,
            "possibly_sold": self.possibly_sold,
            "delisted_unknown": self.delisted_unknown,
            "relisted": self.relisted,
            "median_likely_sold_price": self.median_likely_sold_price,
            "median_active_duration_days": self.median_active_duration_days,
        }


def observed_activity(conn: sqlite3.Connection, reference: str,
                      lookback_days: int = 180) -> ObservedActivity:
    """Aggregate the scanner's own history into a market-activity picture."""
    obs = observations_for(conn, reference, lookback_days)
    activity = ObservedActivity(reference=reference, lookback_days=lookback_days,
                                total_tracked=len(obs), observations=obs)

    durations: list[float] = []
    for o in obs:
        if o.status == ACTIVE:
            activity.still_active += 1
        elif o.status == LIKELY_SOLD:
            activity.likely_sold += 1
            if o.last_price:
                activity.likely_sold_prices.append(o.last_price)
        elif o.status == POSSIBLY_SOLD:
            activity.possibly_sold += 1
        elif o.status == RELISTED:
            activity.relisted += 1
        else:
            activity.delisted_unknown += 1
        if o.status != ACTIVE and o.active_duration_days:
            durations.append(o.active_duration_days)

    if len(durations) >= 3:
        activity.median_active_duration_days = round(statistics.median(durations), 1)
    return activity


class LocalObservationProvider:
    """Sold-evidence provider backed by the scanner's own observation history.

    Deliberately weaker than manual or Marketplace Insights evidence: it reports
    LIKELY_SOLD counts, never confirmed sales, and the confidence engine caps it
    accordingly.
    """

    name = "local_observation"

    def __init__(self, conn: sqlite3.Connection, lookback_days: int = 180):
        self.conn = conn
        self.lookback_days = lookback_days

    def get_activity(self, reference: str) -> ObservedActivity:
        return observed_activity(self.conn, reference, self.lookback_days)

    def get_recent_sales(self, reference: str, country: str = "GB",
                         period_days: int = 90):
        """Return a SoldEvidence-shaped object built from observed activity."""
        from .sold_market import COVERAGE_PARTIAL, COVERAGE_UNAVAILABLE, SoldEvidence

        activity = self.get_activity(reference)
        if not activity.has_activity:
            return SoldEvidence(
                reference, period_days, self.name, COVERAGE_UNAVAILABLE,
                notes=("Local observation history is too thin to infer activity. "
                       + activity.describe()),
            )
        return SoldEvidence(
            reference=reference,
            period_days=self.lookback_days,
            source=self.name,
            coverage_status=COVERAGE_PARTIAL,
            exact_sale_count=activity.likely_sold,
            family_sale_count=activity.likely_sold + activity.possibly_sold,
            prices=list(activity.likely_sold_prices),
            notes=activity.describe(),
        )
