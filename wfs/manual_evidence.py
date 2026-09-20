"""Manual sold-evidence import with validation (spec 5.1 §13, §14).

CSV format:

    brand,reference,model,sold_price_gbp,sold_date,condition,full_set,source,notes
    Longines,L3.781.4.56.6,HydroConquest,1125,2026-08-20,excellent,yes,eBay UK,

Manually recorded sales are the highest-quality evidence most users can obtain,
so this importer validates strictly and tells you exactly what it rejected
rather than silently dropping rows.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .watchlist import normalise

REQUIRED_COLUMNS = ["brand", "reference", "sold_price_gbp", "sold_date"]
TEMPLATE_COLUMNS = ["brand", "reference", "model", "sold_price_gbp", "sold_date",
                    "condition", "full_set", "source", "notes"]

TEMPLATE_CSV = (
    ",".join(TEMPLATE_COLUMNS) + "\n"
    "Longines,L3.781.4.56.6,HydroConquest,1125,2026-08-20,excellent,yes,eBay UK,\n"
    "Tudor,79030N,Black Bay 58,2680,2026-08-14,very good,yes,eBay UK,full set\n"
    "Breitling,A17366,SuperOcean 42,2290,2026-08-02,good,no,eBay UK,watch only\n"
)

MANUAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS manual_sold_evidence (
    id INTEGER PRIMARY KEY,
    brand TEXT NOT NULL,
    reference TEXT NOT NULL,
    reference_key TEXT NOT NULL,
    model TEXT,
    sold_price_gbp REAL NOT NULL,
    sold_date TEXT NOT NULL,
    condition TEXT,
    full_set INTEGER,
    source TEXT,
    notes TEXT,
    imported_at TEXT NOT NULL,
    UNIQUE (reference_key, sold_price_gbp, sold_date)
);

CREATE INDEX IF NOT EXISTS idx_manual_ref ON manual_sold_evidence (reference_key);
"""

TRUTHY = {"yes", "y", "true", "1", "full set", "fullset"}


def init_manual_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(MANUAL_SCHEMA)
    conn.commit()


@dataclass
class RowError:
    line: int
    reason: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImportReport:
    imported: int = 0
    duplicates: int = 0
    invalid: int = 0
    errors: list[RowError] = field(default_factory=list)
    references_affected: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return self.imported + self.duplicates + self.invalid

    def summary(self) -> str:
        return (f"{self.imported} imported, {self.duplicates} duplicate(s) rejected, "
                f"{self.invalid} invalid row(s), "
                f"{len(self.references_affected)} reference(s) affected.")

    def as_dict(self) -> dict[str, Any]:
        return {
            "imported": self.imported,
            "duplicates_rejected": self.duplicates,
            "invalid": self.invalid,
            "references_affected": self.references_affected,
            "errors": [{"line": e.line, "reason": e.reason} for e in self.errors],
        }


def _parse_price(raw: str) -> float:
    cleaned = (raw or "").strip().replace("£", "").replace(",", "").replace(" ", "")
    if not cleaned:
        raise ValueError("sold_price_gbp is empty")
    value = float(cleaned)
    if value <= 0:
        raise ValueError("sold_price_gbp must be greater than zero")
    if value > 1_000_000:
        raise ValueError("sold_price_gbp is implausibly large")
    return round(value, 2)


def _parse_date(raw: str) -> str:
    cleaned = (raw or "").strip()
    if not cleaned:
        raise ValueError("sold_date is empty")
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    else:
        try:
            parsed = datetime.fromisoformat(cleaned)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            raise ValueError(f"sold_date '{cleaned}' is not a recognised date")
    if parsed > datetime.now(timezone.utc):
        raise ValueError("sold_date is in the future")
    return parsed.date().isoformat()


def validate_row(row: dict[str, Any], line: int) -> dict[str, Any]:
    """Return a cleaned row, or raise ValueError describing the problem."""
    missing = [c for c in REQUIRED_COLUMNS if not (row.get(c) or "").strip()]
    if missing:
        raise ValueError(f"missing required value(s): {', '.join(missing)}")

    reference = (row.get("reference") or "").strip()
    key = normalise(reference)
    if len(key) < 3:
        raise ValueError(f"reference '{reference}' is too short to be usable")

    full_set_raw = (row.get("full_set") or "").strip().lower()
    return {
        "brand": (row.get("brand") or "").strip(),
        "reference": reference,
        "reference_key": key,
        "model": (row.get("model") or "").strip() or None,
        "sold_price_gbp": _parse_price(row.get("sold_price_gbp", "")),
        "sold_date": _parse_date(row.get("sold_date", "")),
        "condition": (row.get("condition") or "").strip() or None,
        "full_set": 1 if full_set_raw in TRUTHY else 0,
        "source": (row.get("source") or "").strip() or None,
        "notes": (row.get("notes") or "").strip() or None,
    }


def import_rows(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]
                ) -> ImportReport:
    init_manual_schema(conn)
    report = ImportReport()
    affected: set[str] = set()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for i, raw in enumerate(rows, start=2):  # line 1 is the header
        try:
            clean = validate_row(raw, i)
        except ValueError as exc:
            report.invalid += 1
            report.errors.append(RowError(i, str(exc), dict(raw)))
            continue

        existing = conn.execute(
            """SELECT 1 FROM manual_sold_evidence
               WHERE reference_key = ? AND sold_price_gbp = ? AND sold_date = ?""",
            (clean["reference_key"], clean["sold_price_gbp"], clean["sold_date"]),
        ).fetchone()
        if existing:
            report.duplicates += 1
            report.errors.append(RowError(
                i, f"duplicate of an existing record for {clean['reference']} "
                   f"at £{clean['sold_price_gbp']:,.0f} on {clean['sold_date']}"))
            continue

        conn.execute(
            """INSERT INTO manual_sold_evidence
               (brand, reference, reference_key, model, sold_price_gbp, sold_date,
                condition, full_set, source, notes, imported_at)
               VALUES (:brand, :reference, :reference_key, :model, :sold_price_gbp,
                       :sold_date, :condition, :full_set, :source, :notes, :imported_at)""",
            {**clean, "imported_at": now},
        )
        report.imported += 1
        affected.add(clean["reference"])

    conn.commit()
    report.references_affected = sorted(affected)
    return report


def import_csv_text(conn: sqlite3.Connection, text: str) -> ImportReport:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        report = ImportReport()
        report.invalid = 1
        report.errors.append(RowError(1, "file is empty or has no header row"))
        return report
    header = {(f or "").strip().lower() for f in reader.fieldnames}
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        report = ImportReport()
        report.invalid = 1
        report.errors.append(RowError(
            1, f"header is missing required column(s): {', '.join(missing)}"))
        return report
    normalised = ({(k or "").strip().lower(): v for k, v in row.items()}
                  for row in reader)
    return import_rows(conn, normalised)


def import_csv_file(conn: sqlite3.Connection, path: Path | str) -> ImportReport:
    return import_csv_text(conn, Path(path).read_text(encoding="utf-8"))


def stored_records(conn: sqlite3.Connection, reference: str | None = None
                   ) -> list[dict[str, Any]]:
    init_manual_schema(conn)
    if reference:
        rows = conn.execute(
            "SELECT * FROM manual_sold_evidence WHERE reference_key = ? "
            "ORDER BY sold_date DESC", (normalise(reference),)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM manual_sold_evidence ORDER BY sold_date DESC").fetchall()
    return [dict(r) for r in rows]


def evidence_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    init_manual_schema(conn)
    row = conn.execute(
        """SELECT COUNT(*) AS n, COUNT(DISTINCT reference_key) AS refs,
                  MIN(sold_date) AS earliest, MAX(sold_date) AS latest
           FROM manual_sold_evidence""").fetchone()
    return {"records": row["n"], "references": row["refs"],
            "earliest": row["earliest"], "latest": row["latest"]}


class DatabaseManualSoldProvider:
    """Sold evidence from records you imported. The strongest realistic source."""

    name = "manual_sold_evidence"

    def __init__(self, conn: sqlite3.Connection, period_days: int = 90):
        self.conn = conn
        self.period_days = period_days

    def get_recent_sales(self, reference: str, country: str = "GB",
                         period_days: int | None = None):
        from .sold_market import COVERAGE_PARTIAL, COVERAGE_UNAVAILABLE, SoldEvidence

        window = period_days or self.period_days
        init_manual_schema(self.conn)
        cutoff = (datetime.now(timezone.utc).date()
                  - __import__("datetime").timedelta(days=window)).isoformat()
        rows = self.conn.execute(
            """SELECT sold_price_gbp, sold_date FROM manual_sold_evidence
               WHERE reference_key = ? AND sold_date >= ?""",
            (normalise(reference), cutoff)).fetchall()
        if not rows:
            return SoldEvidence(reference, window, self.name, COVERAGE_UNAVAILABLE,
                                notes="No manually recorded sales in this window.")
        prices = [r["sold_price_gbp"] for r in rows]
        return SoldEvidence(
            reference=reference, period_days=window, source=self.name,
            coverage_status=COVERAGE_PARTIAL,
            exact_sale_count=len(prices), family_sale_count=len(prices),
            prices=prices,
            notes=("Manually recorded sales. Coverage is partial by nature — it "
                   "reflects what you observed, not the whole market."),
        )
