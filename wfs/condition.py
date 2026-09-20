"""Condition and completeness assessment (spec s.10).

Parses what a listing actually claims, rather than assuming 'used' means good.
Anything not positively stated is treated as absent, because in this market an
unmentioned box is usually a missing box.

Condition feeds valuation, MAX BUY, risk and the final recommendation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

FULL_SET = "FULL_SET"
BOX_AND_PAPERS = "BOX_AND_PAPERS"
BOX_ONLY = "BOX_ONLY"
PAPERS_ONLY = "PAPERS_ONLY"
WATCH_ONLY = "WATCH_ONLY"
UNKNOWN = "UNKNOWN"

# Multiplier applied to the market valuation for each completeness level.
# Full set commands a premium; a bare watch sells at a discount.
COMPLETENESS_MULTIPLIER = {
    FULL_SET: 1.00,
    BOX_AND_PAPERS: 1.00,
    BOX_ONLY: 0.95,
    PAPERS_ONLY: 0.96,
    WATCH_ONLY: 0.90,
    UNKNOWN: 0.92,
}

COMPLETENESS_LABEL = {
    FULL_SET: "Full set",
    BOX_AND_PAPERS: "Box and papers",
    BOX_ONLY: "Box only",
    PAPERS_ONLY: "Papers only",
    WATCH_ONLY: "Watch only",
    UNKNOWN: "Completeness not stated",
}

PATTERNS: dict[str, re.Pattern] = {
    "full_set": re.compile(r"\b(full\s*set|complete\s*set|box\s*(?:and|&|\+)\s*papers|b\s*&\s*p)\b", re.I),
    "box": re.compile(r"\b(box|boxed|inner\s*box|outer\s*box|presentation\s*box)\b", re.I),
    "papers": re.compile(r"\b(papers?|warranty\s*card|guarantee\s*card|certificate|"
                         r"warranty\s*booklet)\b", re.I),
    "receipt": re.compile(r"\b(receipt|proof\s*of\s*purchase|invoice)\b", re.I),
    "warranty_remaining": re.compile(r"\b(warranty\s*(?:until|valid|remaining|to\s*20\d\d)|"
                                     r"under\s*warranty|in\s*warranty)\b", re.I),
    "recent_purchase": re.compile(r"\b(20(?:2[3-9]|3\d))\b"),
    "service_history": re.compile(r"\b(serviced|service\s*history|full\s*service|"
                                  r"recently\s*serviced)\b", re.I),
    "service_needed": re.compile(r"\b(needs?\s*(?:a\s*)?service|due\s*(?:a\s*)?service|"
                                 r"running\s*fast|running\s*slow|gaining|losing\s*time|"
                                 r"not\s*keeping\s*time)\b", re.I),
    "all_links": re.compile(r"\b(all\s*links?|full\s*length\s*bracelet|complete\s*bracelet|"
                            r"all\s*removed\s*links?\s*included)\b", re.I),
    "missing_links": re.compile(r"\b(missing\s*links?|no\s*(?:spare\s*)?links?|"
                                r"links?\s*missing|short\s*bracelet|sized\s*down)\b", re.I),
    "scratches": re.compile(r"\b(scratch\w*|scuff\w*|mark[s]?\s*(?:on|to)\s*|dent\w*|"
                            r"ding\w*|chip\w*|wear\s*(?:and|&)\s*tear|signs?\s*of\s*wear)\b", re.I),
    "mint": re.compile(r"\b(mint|as\s*new|unworn|pristine|immaculate|like\s*new|"
                       r"excellent\s*condition)\b", re.I),
    "polished": re.compile(r"\b(polished|refinish\w*|buffed|re-?lumed)\b", re.I),
    "unpolished": re.compile(r"\b(unpolished|never\s*polished|original\s*finish|"
                             r"factory\s*finish)\b", re.I),
    "damage": re.compile(r"\b(cracked?|chipped?\s*(?:bezel|crystal)|damaged?|broken|"
                         r"faulty|not\s*working|spares?\s*(?:or|/)\s*repairs?|for\s*parts)\b", re.I),
    "aftermarket": re.compile(r"\b(aftermarket|non-?original|generic|replacement\s*(?:dial|"
                              r"bezel|hands|bracelet)|custom|modded|mod\b|franken)\b", re.I),
    "strap_worn": re.compile(r"\b(strap\s*(?:worn|tired|needs\s*replacing)|worn\s*strap)\b", re.I),
    # Negated claims. "no papers" must never be read as "papers".
    "no_papers": re.compile(r"\b(no|without|missing|lacking|not?\s+inc\w*|minus)\s+"
                            r"(?:the\s+)?(?:original\s+)?(papers?|warranty\s*card|"
                            r"guarantee\s*card|certificate)\b", re.I),
    "no_box": re.compile(r"\b(no|without|missing|lacking|not?\s+inc\w*|minus)\s+"
                         r"(?:the\s+)?(?:original\s+|inner\s+|outer\s+)?box\b", re.I),
    "no_reference": re.compile(r"\b(reference\s*unknown|ref\s*unknown|unsure\s*(?:of\s*)?"
                               r"(?:the\s*)?(?:model|reference))\b", re.I),
    "authenticity_doubt": re.compile(r"\b(sold\s*as\s*seen|no\s*returns|unable\s*to\s*"
                                     r"(?:verify|authenticate)|not\s*guaranteed|"
                                     r"cannot\s*confirm|genuine\?|believed\s*(?:to\s*be\s*)?"
                                     r"genuine)\b", re.I),
}


@dataclass
class ConditionAssessment:
    completeness: str = UNKNOWN
    has_box: bool = False
    has_papers: bool = False
    has_receipt: bool = False
    warranty_remaining: bool = False
    recent_purchase: bool = False
    service_history: bool = False
    service_needed: bool = False
    all_links: bool = False
    missing_links: bool = False
    visible_scratches: bool = False
    mint: bool = False
    polished: bool = False
    unpolished: bool = False
    damage: bool = False
    aftermarket_parts: bool = False
    strap_worn: bool = False
    reference_unclear: bool = False
    authenticity_doubt: bool = False

    positives: list[str] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)

    # 0-100. 50 is a neutral, unremarkable pre-owned watch.
    condition_score: float = 50.0
    # Multiplier applied to market valuation.
    value_multiplier: float = 1.0

    @property
    def completeness_label(self) -> str:
        return COMPLETENESS_LABEL[self.completeness]

    @property
    def needs_reserve(self) -> bool:
        """Whether to hold back a service/repair reserve."""
        return self.service_needed or self.damage or self.missing_links

    def as_dict(self) -> dict[str, Any]:
        return {
            "completeness": self.completeness,
            "completeness_label": self.completeness_label,
            "condition_score": self.condition_score,
            "value_multiplier": self.value_multiplier,
            "positives": self.positives,
            "negatives": self.negatives,
            "service_needed": self.service_needed,
            "damage": self.damage,
            "aftermarket_parts": self.aftermarket_parts,
            "reference_unclear": self.reference_unclear,
        }


def _hit(pattern_key: str, text: str) -> bool:
    return bool(PATTERNS[pattern_key].search(text))


def assess_condition(listing: dict[str, Any]) -> ConditionAssessment:
    """Parse title, condition field and any description text available."""
    parts = [listing.get("title") or "", listing.get("condition") or "",
             listing.get("description") or "", listing.get("short_description") or ""]
    text = " ".join(p for p in parts if p)

    a = ConditionAssessment()

    # A negated mention cancels the positive one: "box only, no papers" has a box
    # but not papers. Without this the parser would inflate completeness and, in
    # turn, the valuation.
    denies_box = _hit("no_box", text)
    denies_papers = _hit("no_papers", text)
    claims_full_set = _hit("full_set", text) and not (denies_box or denies_papers)

    a.has_box = (claims_full_set or _hit("box", text)) and not denies_box
    a.has_papers = (claims_full_set or _hit("papers", text)) and not denies_papers
    a.has_receipt = _hit("receipt", text)
    a.warranty_remaining = _hit("warranty_remaining", text)
    a.recent_purchase = _hit("recent_purchase", text)
    a.service_history = _hit("service_history", text)
    a.service_needed = _hit("service_needed", text)
    a.all_links = _hit("all_links", text)
    a.missing_links = _hit("missing_links", text)
    a.visible_scratches = _hit("scratches", text)
    a.mint = _hit("mint", text)
    a.polished = _hit("polished", text)
    a.unpolished = _hit("unpolished", text)
    a.damage = _hit("damage", text)
    a.aftermarket_parts = _hit("aftermarket", text)
    a.strap_worn = _hit("strap_worn", text)
    a.reference_unclear = _hit("no_reference", text)
    a.authenticity_doubt = _hit("authenticity_doubt", text)

    # Completeness
    if claims_full_set or (a.has_box and a.has_papers):
        a.completeness = FULL_SET if claims_full_set else BOX_AND_PAPERS
    elif a.has_box:
        a.completeness = BOX_ONLY
    elif a.has_papers:
        a.completeness = PAPERS_ONLY
    elif text.strip():
        # Something was said, but nothing about box or papers.
        a.completeness = WATCH_ONLY if len(text) > 40 else UNKNOWN
    else:
        a.completeness = UNKNOWN

    # Score, starting neutral.
    score = 50.0

    def up(points: float, label: str) -> None:
        nonlocal score
        score += points
        a.positives.append(label)

    def down(points: float, label: str) -> None:
        nonlocal score
        score -= points
        a.negatives.append(label)

    if a.completeness in (FULL_SET, BOX_AND_PAPERS):
        up(15, "Box and papers present")
    elif a.completeness == BOX_ONLY:
        up(5, "Box only")
    elif a.completeness == PAPERS_ONLY:
        up(6, "Papers only")
    elif a.completeness == WATCH_ONLY:
        down(8, "Watch only — no box or papers mentioned")
    else:
        down(5, "Completeness not stated")

    if a.has_receipt:
        up(4, "Receipt or proof of purchase")
    if a.warranty_remaining:
        up(8, "Manufacturer warranty appears to remain")
    if a.recent_purchase:
        up(3, "Recent-year watch")
    if a.service_history:
        up(7, "Service history stated")
    if a.mint:
        up(8, "Described as mint or unworn")
    if a.unpolished:
        up(5, "Described as unpolished")
    if a.all_links:
        up(4, "Full bracelet with all links")

    if a.service_needed:
        down(14, "Service appears to be needed")
    if a.damage:
        down(22, "Damage, fault or spares/repairs indicated")
    if a.aftermarket_parts:
        down(18, "Aftermarket or non-original parts indicated")
    if a.missing_links:
        down(8, "Bracelet links missing")
    if a.visible_scratches and not a.mint:
        down(6, "Visible scratches or wear described")
    if a.polished and not a.unpolished:
        down(7, "Case appears polished")
    if a.strap_worn:
        down(3, "Strap described as worn")
    if a.reference_unclear:
        down(10, "Reference not clearly identified")
    if a.authenticity_doubt:
        down(12, "Listing language raises authenticity questions")

    a.condition_score = round(max(0.0, min(100.0, score)), 1)

    # Value multiplier: completeness baseline, adjusted for condition extremes.
    multiplier = COMPLETENESS_MULTIPLIER[a.completeness]
    if a.damage:
        multiplier -= 0.10
    if a.service_needed:
        multiplier -= 0.05
    if a.aftermarket_parts:
        multiplier -= 0.08
    if a.mint:
        multiplier += 0.03
    if a.polished and not a.unpolished:
        multiplier -= 0.03
    a.value_multiplier = round(max(0.60, min(1.05, multiplier)), 4)

    return a
