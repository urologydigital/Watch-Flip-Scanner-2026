"""Comparable filtering with a full audit trail (spec 6.0 §8, §16).

A median over dirty data is worse than no median: one strap-only listing at £90
drags an entire valuation down, and one "watch head only" comparable makes a
complete watch look overpriced.

Every comparable is judged and the verdict is recorded. Nothing is dropped
silently — the user can open any valuation and see exactly which records were
used, which were excluded, and why.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .collectors.base import (MATCH_LABEL, MATCH_MATERIAL, CollectedEvidence,
                              classify_match)

# Each rule: (code, human reason, compiled pattern).
# Ordered so the most decisive exclusions are reported first.
EXCLUSION_RULES: list[tuple[str, str, re.Pattern]] = [
    ("REPLICA", "Replica, fake or homage wording",
     re.compile(r"\b(replica|fake|copy\s*watch|homage|clone|not\s*genuine)\b", re.I)),
    ("PARTS_ONLY", "Parts, spares or repair listing",
     re.compile(r"\b(for\s*parts|spares?\s*(or|/|and)\s*repair|parts\s*only|"
                r"not\s*working|non[-\s]?runner|doesn'?t\s*work|faulty)\b", re.I)),
    ("STRAP_ONLY", "Strap or bracelet only — not a watch",
     re.compile(r"\b(strap\s*only|bracelet\s*only|band\s*only|clasp\s*only|"
                r"buckle\s*only)\b", re.I)),
    ("BOX_ONLY", "Box, papers or accessories only",
     re.compile(r"\b(box\s*only|empty\s*box|papers\s*only|manual\s*only|"
                r"booklet\s*only|case\s*only|accessor(y|ies)\s*only)\b", re.I)),
    ("PART_COMPONENT", "Single component listing (dial, bezel, hands, movement)",
     re.compile(r"\b(dial\s*only|bezel\s*only|hands\s*only|movement\s*only|"
                r"crystal\s*only|crown\s*only|insert\s*only)\b", re.I)),
    ("HEAD_ONLY", "Watch head only where a complete watch is expected",
     re.compile(r"\b(head\s*only|watch\s*head\s*only|no\s*bracelet\s*or\s*strap)\b",
                re.I)),
    ("AFTERMARKET", "Materially modified or aftermarket parts",
     re.compile(r"\b(aftermarket\s*(dial|bezel|case|hands)|custom\s*(dial|bezel)|"
                r"modded|mod\s*watch|franken|refinished\s*dial|re-?dial)\b", re.I)),
    ("BROKEN", "Damaged in a way that changes the economics",
     re.compile(r"\b(cracked\s*(crystal|glass|case)|water\s*damage|"
                r"smashed|shattered)\b", re.I)),
    ("BULK_LOT", "Job lot or multiple watches in one listing",
     re.compile(r"\b(job\s*lot|bulk\s*lot|bundle\s*of|x\s*\d+\s*watches|"
                r"\d+\s*watches)\b", re.I)),
]

# Exclusion codes that are about match quality rather than listing content.
CODE_WRONG_REFERENCE = "WRONG_REFERENCE"
CODE_WEAK_MATCH = "WEAK_MATCH"
CODE_NO_PRICE = "NO_PRICE"
CODE_DUPLICATE = "DUPLICATE"
CODE_PRICE_OUTLIER = "PRICE_OUTLIER"


@dataclass
class ComparableDecision:
    """Whether one comparable was used, and the reason either way."""

    evidence: CollectedEvidence
    included: bool
    code: str = ""
    reason: str = ""

    @property
    def price(self) -> float | None:
        return self.evidence.price_gbp

    def as_dict(self) -> dict[str, Any]:
        e = self.evidence
        return {
            "included": self.included,
            "exclusion_code": self.code or "",
            "reason": self.reason,
            "source": e.source,
            "evidence_type": e.evidence_type,
            "reference": e.reference,
            "title": e.title,
            "price_gbp": e.price_gbp,
            "sale_date": e.sale_date,
            "match_type": e.match_type,
            "match_label": MATCH_LABEL.get(e.match_type, e.match_type),
            "price_certainty": e.price_certainty,
            "listing_url": e.listing_url,
            "item_id": e.item_id,
            "retrieved_at": e.retrieved_at,
        }


@dataclass
class ComparableAudit:
    """The full included/excluded picture behind one valuation."""

    reference: str
    decisions: list[ComparableDecision] = field(default_factory=list)

    @property
    def included(self) -> list[ComparableDecision]:
        return [d for d in self.decisions if d.included]

    @property
    def excluded(self) -> list[ComparableDecision]:
        return [d for d in self.decisions if not d.included]

    @property
    def included_records(self) -> list[CollectedEvidence]:
        return [d.evidence for d in self.included]

    def exclusion_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self.excluded:
            counts[d.code] = counts.get(d.code, 0) + 1
        return counts

    def summary(self) -> str:
        if not self.decisions:
            return "No comparables to assess."
        parts = [f"{len(self.included)} of {len(self.decisions)} comparable(s) used"]
        counts = self.exclusion_counts()
        if counts:
            parts.append("excluded: " + ", ".join(
                f"{code.lower().replace('_', ' ')} {n}"
                for code, n in sorted(counts.items())))
        return "; ".join(parts) + "."

    def as_rows(self) -> list[dict[str, Any]]:
        return [d.as_dict() for d in self.decisions]


def _content_exclusion(title: str) -> tuple[str, str] | None:
    for code, reason, pattern in EXCLUSION_RULES:
        if pattern.search(title or ""):
            return code, reason
    return None


def filter_comparables(records: list[CollectedEvidence], reference: str,
                       brand: str = "", model: str = "",
                       require_material_match: bool = True,
                       outlier_factor: float = 3.5) -> ComparableAudit:
    """Judge every comparable and record the verdict.

    `require_material_match` keeps only EXACT / VARIANT / NORMALIZED matches for
    anything that will move MAX BUY. Family and weak matches are retained in the
    audit, marked excluded, so they can still be displayed separately.
    """
    audit = ComparableAudit(reference=reference)
    seen_fingerprints: set[str] = set()
    survivors: list[ComparableDecision] = []

    for record in records:
        title = record.title or ""

        # Re-derive the match from the title where we have one, so a record
        # stored with an optimistic match type cannot slip through.
        match = record.match_type
        if title:
            match = classify_match(title, reference, model, brand)

        content = _content_exclusion(title)
        if content:
            audit.decisions.append(ComparableDecision(
                record, False, content[0], content[1]))
            continue

        if match == "REJECTED":
            audit.decisions.append(ComparableDecision(
                record, False, CODE_WRONG_REFERENCE,
                "A different reference — materially a different watch."))
            continue

        if require_material_match and match not in MATCH_MATERIAL:
            audit.decisions.append(ComparableDecision(
                record, False, CODE_WEAK_MATCH,
                f"{MATCH_LABEL.get(match, match)} — shown separately, not used "
                "for valuation."))
            continue

        if record.price_gbp is None or record.price_gbp <= 0:
            audit.decisions.append(ComparableDecision(
                record, False, CODE_NO_PRICE,
                ("Best Offer accepted — the shown figure is the asking price, "
                 "so it counts for liquidity only."
                 if record.price_certainty == "BEST_OFFER_UNCERTAIN"
                 else "No usable price.")))
            continue

        fingerprint = record.fingerprint()
        if fingerprint in seen_fingerprints:
            audit.decisions.append(ComparableDecision(
                record, False, CODE_DUPLICATE,
                "Duplicate of a comparable already counted."))
            continue
        seen_fingerprints.add(fingerprint)

        survivors.append(ComparableDecision(record, True, "", "Used in valuation."))

    # --- price outliers, judged only against survivors --------------------
    prices = [d.price for d in survivors if d.price]
    if len(prices) >= 4:
        import statistics
        median = statistics.median(prices)
        deviations = [abs(p - median) for p in prices]
        mad = statistics.median(deviations)
        if mad > 0:
            for decision in survivors:
                score = abs(decision.price - median) / (1.4826 * mad)
                if score > outlier_factor:
                    decision.included = False
                    decision.code = CODE_PRICE_OUTLIER
                    decision.reason = (
                        f"£{decision.price:,.0f} is far from the £{median:,.0f} "
                        "median of the other comparables.")
            if not any(d.included for d in survivors):
                # Never exclude everything — that would be worse than noise.
                for decision in survivors:
                    decision.included = True
                    decision.code = ""
                    decision.reason = "Used in valuation."

    audit.decisions.extend(survivors)
    return audit
