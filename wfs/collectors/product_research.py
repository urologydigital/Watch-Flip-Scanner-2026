"""eBay Product Research — assisted Level A sold evidence (spec 6.0 §3B).

The problem this solves: Marketplace Insights is restricted for most keysets and
the public completed-listings pages are disallowed to automated access. That
leaves the scanner with no Level A evidence, which is precisely the evidence a
BUY verdict depends on.

The compliant route is the one you already have a right to use: **eBay Product
Research in Seller Hub, in your own authenticated browser.** This module prepares
the exact search, then accepts what you paste or export back.

What this module will never do, by construction:

* read, copy or store your eBay cookies or session
* ask for or store your eBay password
* log in on your behalf, or bypass a login, CAPTCHA or bot check
* drive a hidden browser to impersonate you
* fabricate a sale, a Best Offer price or a date

There is no network client in this module at all. It builds a URL for you to
open, and parses text you choose to give it.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

from ..watchlist import normalise
from .base import (BEST_OFFER_UNCERTAIN, CONFIRMED_PRICE, EV_SOLD,
                   HEALTH_OK, HEALTH_USER_ACTION, BaseCollector,
                   CollectedEvidence, CollectionResult, classify_match, now_iso)

SOURCE = "ebay_product_research"

# Seller Hub research. Opened BY THE USER in their own signed-in browser.
RESEARCH_URL = "https://www.ebay.co.uk/sh/research"

TRUTHY = {"yes", "y", "true", "1", "with papers", "full set"}

# Column aliases, so a pasted export works whatever eBay calls the columns.
COLUMN_ALIASES = {
    "title": {"title", "item title", "product", "name"},
    "sold_price": {"sold price", "avg sold price", "average sold price", "price",
                   "sale price", "sold for"},
    "asking_price": {"asking price", "list price", "start price",
                     "original price", "buy it now price"},
    "sale_date": {"date sold", "sold date", "last sold date", "date", "end date"},
    "condition": {"condition", "item condition"},
    "shipping": {"shipping", "postage", "shipping cost"},
    "url": {"url", "link", "item url", "view item"},
    "item_id": {"item id", "item number", "itemid", "legacy item id"},
    "seller": {"seller", "seller name", "seller id"},
    "best_offer": {"best offer", "best offer accepted", "offer accepted"},
    "format": {"format", "listing type", "buying format"},
}

# A GBP amount, with or without thousands separators.
# The earlier pattern used [0-9]{1,3}(?:,[0-9]{3})* which, with no separator
# present, matched only the FIRST THREE DIGITS — silently turning £2650.00 into
# £265.00 and corrupting every valuation built on it. The comma-grouped form is
# tried first so it wins where separators exist; otherwise a plain digit run.
MONEY_RE = re.compile(
    r"£?\s?([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)")
PRICE_RE = MONEY_RE
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y",
                "%b %d, %Y", "%Y/%m/%d")


def research_url(brand: str, reference: str, model: str = "") -> str:
    """The Seller Hub research URL for one reference, for the USER to open.

    Uses brand + reference only — a full listing title returns noise.
    """
    term = " ".join(p for p in (brand, reference) if p).strip() or model
    return (f"{RESEARCH_URL}?marketplace=EBAY-GB&keywords={quote_plus(term)}"
            "&dayRange=90&categoryId=31387&tabName=SOLD")


def instructions(brand: str, reference: str) -> list[str]:
    """Plain steps. The user stays in control of their own session throughout."""
    return [
        "Open the prepared research link below in your normal browser, signed in "
        "to eBay as usual.",
        "Seller Hub → Research → Sold items. Check the date range and that the "
        "results really are the reference you searched for.",
        "Select the results and copy them, or use eBay's export.",
        f"Paste them into the capture box for {brand} {reference} and press "
        "Import.",
        "Anything that does not clearly match the reference is rejected rather "
        "than guessed at — check the rejected rows before trusting the import.",
    ]


# --- parsing ----------------------------------------------------------------

def _canonical_columns(fieldnames: list[str]) -> dict[str, str]:
    """Map whatever the export called its columns onto our canonical names."""
    mapping: dict[str, str] = {}
    for raw in fieldnames or []:
        key = (raw or "").strip().lower()
        for canonical, aliases in COLUMN_ALIASES.items():
            if key in aliases:
                mapping[canonical] = raw
                break
    return mapping


def parse_price(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    match = PRICE_RE.search(text.replace(",", ","))
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return round(value, 2) if 0 < value <= 500000 else None


def parse_date(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    else:
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if parsed > datetime.now(timezone.utc):
        return None                      # a future sale date is bad data
    return parsed.date().isoformat()


def _flag(text: str, *patterns: str) -> bool:
    lowered = (text or "").lower()
    return any(p in lowered for p in patterns)


@dataclass
class CaptureRow:
    """One parsed research row, with why it was accepted or rejected."""

    title: str = ""
    sold_price_gbp: float | None = None
    original_asking_price_gbp: float | None = None
    best_offer_price_gbp: float | None = None
    sale_date: str | None = None
    condition: str | None = None
    box: bool | None = None
    papers: bool | None = None
    warranty: str | None = None
    shipping_gbp: float | None = None
    seller: str | None = None
    listing_url: str | None = None
    item_id: str | None = None
    match_type: str = "AMBIGUOUS"
    accepted: bool = False
    reason: str = ""
    price_certainty: str = CONFIRMED_PRICE

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class CaptureReport:
    reference: str
    rows: list[CaptureRow] = field(default_factory=list)
    stored: int = 0
    duplicates: int = 0
    captured_at: str = field(default_factory=now_iso)

    @property
    def accepted(self) -> list[CaptureRow]:
        return [r for r in self.rows if r.accepted]

    @property
    def rejected(self) -> list[CaptureRow]:
        return [r for r in self.rows if not r.accepted]

    def summary(self) -> str:
        return (f"{len(self.accepted)} accepted, {len(self.rejected)} rejected, "
                f"{self.stored} stored, {self.duplicates} duplicate(s).")

    def as_dict(self) -> dict[str, Any]:
        return {"reference": self.reference, "accepted": len(self.accepted),
                "rejected": len(self.rejected), "stored": self.stored,
                "duplicates": self.duplicates, "captured_at": self.captured_at}


def parse_capture(text: str, reference: str, brand: str = "",
                  model: str = "") -> CaptureReport:
    """Parse a pasted Product Research table (CSV or tab-separated).

    Every row is matched against the reference. A row that cannot be tied to the
    watch is rejected with a reason rather than quietly averaged in.
    """
    report = CaptureReport(reference=reference)
    text = (text or "").strip()
    if not text:
        return report

    sample = text.splitlines()[0]
    delimiter = "\t" if sample.count("\t") >= sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    columns = _canonical_columns(reader.fieldnames or [])

    if "title" not in columns:
        row = CaptureRow(reason="No recognisable title column — paste the table "
                                "including its header row.")
        report.rows.append(row)
        return report

    for raw in reader:
        def value(name: str) -> Any:
            column = columns.get(name)
            return raw.get(column) if column else None

        title = str(value("title") or "").strip()
        row = CaptureRow(title=title)

        row.match_type = classify_match(title, reference, model, brand)
        if row.match_type not in ("EXACT_REFERENCE", "NORMALIZED_REFERENCE",
                                  "HIGH_CONFIDENCE_VARIANT"):
            row.reason = (f"Title does not carry the exact reference "
                          f"({row.match_type}). Not counted as Level A evidence.")
            report.rows.append(row)
            continue

        # Bad comparables must not become sold evidence (spec s.8).
        if _flag(title, "strap only", "bracelet only", "box only", "for parts",
                 "spares or repair", "not working", "faulty", "replica", "fake",
                 "homage", "empty box", "manual only", "dial only", "bezel only"):
            row.reason = "Excluded comparable (parts, accessory or non-genuine)."
            report.rows.append(row)
            continue

        row.sold_price_gbp = parse_price(value("sold_price"))
        row.original_asking_price_gbp = parse_price(value("asking_price"))
        row.shipping_gbp = parse_price(value("shipping"))
        row.sale_date = parse_date(value("sale_date"))
        row.condition = (str(value("condition")).strip() or None
                         if value("condition") else None)
        row.seller = (str(value("seller")).strip() or None
                      if value("seller") else None)
        row.listing_url = (str(value("url")).strip() or None
                           if value("url") else None)
        row.item_id = (str(value("item_id")).strip() or None
                       if value("item_id") else None)

        row.box = True if _flag(title, "box") else None
        row.papers = True if _flag(title, "papers", "warranty card",
                                   "certificate") else None
        if _flag(title, "full set"):
            row.box = row.papers = True
        if _flag(title, "warranty"):
            row.warranty = "warranty mentioned in title"

        # Best Offer: eBay shows the asking price, not what was paid. We record
        # the sale but never invent the accepted figure (spec s.3B).
        best_offer_flag = str(value("best_offer") or "").strip().lower() in TRUTHY \
            or _flag(title, "best offer accepted", "offer accepted")
        if best_offer_flag:
            row.price_certainty = BEST_OFFER_UNCERTAIN
            row.best_offer_price_gbp = None   # never invented
            row.reason = ("Best Offer accepted — the displayed figure is the "
                          "asking price, so it counts for liquidity only.")
            row.accepted = True
            report.rows.append(row)
            continue

        if row.sold_price_gbp is None:
            row.reason = "No usable sold price in this row."
            report.rows.append(row)
            continue

        row.accepted = True
        row.reason = "Accepted as confirmed sold evidence."
        report.rows.append(row)

    return report


# --- storage ----------------------------------------------------------------

def to_evidence(report: CaptureReport, brand: str = "",
                model: str = "") -> list[CollectedEvidence]:
    """Convert accepted rows into Level A evidence records."""
    out: list[CollectedEvidence] = []
    for row in report.accepted:
        out.append(CollectedEvidence(
            source=SOURCE,
            evidence_type=EV_SOLD,
            reference=report.reference,
            brand=brand or None,
            model=model or None,
            title=row.title or None,
            price_gbp=(row.sold_price_gbp
                       if row.price_certainty == CONFIRMED_PRICE else None),
            original_price=row.original_asking_price_gbp or row.sold_price_gbp,
            original_currency="GBP",
            sale_date=row.sale_date,
            condition=row.condition,
            full_set=(True if (row.box and row.papers) else None),
            listing_url=row.listing_url,
            item_id=row.item_id or None,
            seller_location=None,
            retrieved_at=report.captured_at,
            price_certainty=row.price_certainty,
            match_type=row.match_type,
            notes=("eBay Product Research (Seller Hub), captured by the user. "
                   + row.reason),
        ))
    return out


def import_capture(conn: sqlite3.Connection, text: str, reference: str,
                   brand: str = "", model: str = "") -> CaptureReport:
    """Parse, store and report. Deduplicates through the evidence store."""
    from ..evidence_store import load_evidence, store_evidence

    report = parse_capture(text, reference, brand, model)
    records = to_evidence(report, brand, model)
    if not records:
        return report

    before = {r.fingerprint() for r in load_evidence(conn, reference)}
    counts = store_evidence(conn, records)
    report.stored = counts.get("inserted", 0)
    report.duplicates = counts.get("refreshed", 0)
    return report


def capture_summary(conn: sqlite3.Connection,
                    reference: str | None = None) -> dict[str, Any]:
    from ..evidence_store import migrate
    migrate(conn)
    query = ("SELECT COUNT(*) n, MAX(retrieved_at) latest FROM collected_evidence "
             "WHERE source = ?")
    params: list[Any] = [SOURCE]
    if reference:
        query += " AND reference_key = ?"
        params.append(normalise(reference))
    row = conn.execute(query, params).fetchone()
    return {"records": row["n"], "last_capture": row["latest"]}


class ProductResearchCollector(BaseCollector):
    """Reports what has been captured. It never fetches anything itself.

    Level A evidence arrives only when the user completes the research step, so
    this collector's job is to say clearly whether that has happened for a given
    reference and, if not, that action is required.
    """

    name = SOURCE
    evidence_type = EV_SOLD

    def __init__(self, conn: sqlite3.Connection, enabled: bool = True):
        super().__init__(enabled=enabled)
        self.conn = conn

    def is_available(self) -> bool:
        return self.conn is not None

    def transport_description(self) -> str:
        return "user-assisted capture (no automated request)"

    def unavailable_reason(self) -> str:
        return "No database connection." if self.conn is None else "Ready."

    def preflight(self) -> dict[str, Any]:
        summary = capture_summary(self.conn) if self.conn else {"records": 0}
        return {
            "source": self.name, "enabled": bool(self.enabled),
            "transport": self.transport_description(), "ready": True,
            "captured_records": summary.get("records", 0),
            "reason": (f"{summary.get('records', 0)} captured sold record(s). "
                       "Open the prepared research page in Market Evidence to add "
                       "more."),
        }

    def _collect(self, reference: str, brand: str, model: str,
                 **kwargs: Any) -> CollectionResult:
        from ..evidence_store import load_evidence

        records = [r for r in load_evidence(self.conn, reference)
                   if r.source == SOURCE]
        if not records:
            return CollectionResult(
                self.name, reference, [],
                __import__("wfs.collectors.base", fromlist=["SourceStatus"])
                .SourceStatus(
                    self.name, HEALTH_USER_ACTION, 0, None, "MISS",
                    ("No Product Research data captured for this reference yet. "
                     "Open the prepared Seller Hub research page and paste the "
                     "sold results in Market Evidence."),
                    "Level A evidence requires one manual step."))
        return CollectionResult.ok(
            self.name, reference, records, cache_status="REUSED",
            detail=f"{len(records)} captured Product Research record(s).")
