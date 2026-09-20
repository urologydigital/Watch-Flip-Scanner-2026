"""Watchlist loading, query generation and reference matching (spec s.7, s.8)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import WATCHLIST_PATH


@dataclass
class WatchRef:
    brand: str
    model: str
    reference: str
    market_low: float | None = None
    market_mid: float | None = None
    market_high: float | None = None
    value_source: str = "SEED_PLACEHOLDER_UNVERIFIED"
    confidence: str = "LOW"
    extra_terms: list[str] = field(default_factory=list)
    # Phase 5: brands and references can be deactivated rather than deleted, so
    # Omega / TAG Heuer / Seiko support is preserved and re-enabled in one edit.
    # Declared last so existing positional construction stays valid.
    active: bool = True

    def as_dict(self) -> dict:
        return {
            "brand": self.brand, "model": self.model, "reference": self.reference,
            "active": self.active,
            "market_low": self.market_low, "market_mid": self.market_mid,
            "market_high": self.market_high, "value_source": self.value_source,
            "confidence": self.confidence,
        }


def load_watchlist(path: Path | str | None = None,
                   active_only: bool = False) -> list[WatchRef]:
    """Load the watchlist.

    A reference is scanned only if both it and its brand are active. Inactive
    entries are retained in the file so they can be switched back on.
    """
    p = Path(path or WATCHLIST_PATH)
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found. Run `python seed_watchlist.py` first."
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    active_brands = data.get("active_brands")

    refs = []
    for r in data["references"]:
        ref = WatchRef(**{k: v for k, v in r.items() if k in WatchRef.__annotations__})
        if active_brands is not None and ref.brand not in active_brands:
            ref.active = False
        refs.append(ref)

    return [r for r in refs if r.active] if active_only else refs


def brand_status(path: Path | str | None = None) -> dict[str, bool]:
    """Which brands are currently active."""
    p = Path(path or WATCHLIST_PATH)
    data = json.loads(p.read_text(encoding="utf-8"))
    brands = sorted({r["brand"] for r in data["references"]})
    active = data.get("active_brands")
    return {b: (True if active is None else b in active) for b in brands}


def set_brand_active(brand: str, active: bool,
                     path: Path | str | None = None) -> dict[str, bool]:
    """Enable or disable a whole brand without deleting its references."""
    p = Path(path or WATCHLIST_PATH)
    data = json.loads(p.read_text(encoding="utf-8"))
    all_brands = sorted({r["brand"] for r in data["references"]})
    current = set(data.get("active_brands") or all_brands)
    if active:
        current.add(brand)
    else:
        current.discard(brand)
    data["active_brands"] = sorted(current)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {b: b in current for b in all_brands}


# --- query generation -------------------------------------------------------

# Terms that indicate parts/accessories rather than a complete watch (spec s.8).
GENERIC_EXCLUSIONS = [
    "strap", "bracelet only", "box only", "manual", "booklet", "dial only",
    "bezel", "insert", "spare", "parts", "repair", "case only", "crystal",
    "crown", "hands", "link", "clasp", "sticker", "poster", "catalogue",
    "replica", "homage", "for parts", "not working", "empty box",
]

BRAND_EXCLUSIONS = {
    "SEIKO": ["mod", "modded", "nh35", "seikomod", "aftermarket"],
    "OMEGA": ["seamaster strap", "poster"],
    "TUDOR": ["rolex only"],
    "RADO": ["quartz"],
    "TAG HEUER": ["formula 1 quartz"],
}


def build_queries(ref: WatchRef) -> list[str]:
    """Multiple eBay query variants for a single reference."""
    brand = ref.brand
    r = ref.reference
    variants = [
        f"{brand} {r}",
        f"{brand} {ref.model} {r}",
    ]
    # A reference-only search is useful for long alphanumeric refs that are unique.
    if len(r.replace(" ", "").replace(".", "")) >= 6:
        variants.append(r)
    for term in ref.extra_terms:
        variants.append(f"{brand} {term} {r}")
    # De-duplicate, preserve order, collapse whitespace.
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        v = re.sub(r"\s+", " ", v).strip()
        key = v.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def exclusion_terms(brand: str) -> list[str]:
    return GENERIC_EXCLUSIONS + BRAND_EXCLUSIONS.get(brand.upper(), [])


def looks_like_accessory(title: str, brand: str) -> bool:
    t = f" {title.lower()} "
    return any(f" {term} " in t or t.startswith(f" {term}") or term in t
               for term in exclusion_terms(brand))


# --- reference matching -----------------------------------------------------

def normalise(text: str) -> str:
    """Uppercase, strip separators — so 'l3.781.4.56.6' == 'L3 781 4 56 6'."""
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def match_reference(text: str, refs: list[WatchRef]) -> WatchRef | None:
    """Return the most specific watchlist reference appearing in the text.

    Longest reference wins, so '79030N' is preferred over a shorter family stub.
    """
    hay = normalise(text)
    if not hay:
        return None
    matches = [r for r in refs if normalise(r.reference) and normalise(r.reference) in hay]
    if not matches:
        return None
    return max(matches, key=lambda r: len(normalise(r.reference)))
