"""Generates watchlist.json from the specified watchlist.

The market_low / market_mid / market_high figures written here are UNVERIFIED
PLACEHOLDERS so that the deterministic pre-filter has something to work against
on first run. They are NOT market evidence. Every entry is stamped
value_source = "SEED_PLACEHOLDER_UNVERIFIED" and confidence = "LOW", and the app
displays a warning until you replace them with your own figures (or until the
Phase 2 market evidence engine overwrites them).

Run:  python seed_watchlist.py
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "watchlist.json"

# (brand, model, [(reference, low, mid, high), ...])
DATA = [
    ("TUDOR", "Black Bay 58", [
        ("79030N", 2150, 2400, 2650),
        ("79030B", 2250, 2500, 2750),
    ]),
    ("TUDOR", "Black Bay GMT", [
        ("79830RB", 2800, 3100, 3450),
    ]),
    ("TUDOR", "Heritage Black Bay", [
        ("79220N", 2050, 2300, 2550),
        ("79220R", 2050, 2300, 2550),
        ("79220B", 2050, 2300, 2550),
        ("79230N", 2150, 2400, 2650),
        ("79230R", 2150, 2400, 2650),
        ("79230B", 2150, 2400, 2650),
    ]),
    ("OMEGA", "Aqua Terra 38", [
        ("220.10.38.20", 2900, 3200, 3550),
    ]),
    ("OMEGA", "Aqua Terra 41", [
        ("220.10.41.21", 3050, 3400, 3750),
    ]),
    ("OMEGA", "Seamaster Diver 300M", [
        ("210.30.42.20", 3250, 3600, 3950),
    ]),
    ("OMEGA", "Speedmaster Moonwatch", [
        ("310.30.42.50", 4100, 4600, 5100),
    ]),
    ("LONGINES", "HydroConquest Automatic Ceramic 43", [
        ("L3.781.4.56.6", 980, 1120, 1260),
        ("L3.781.4.96.6", 980, 1120, 1260),
        ("L3.781.4.06.6", 980, 1120, 1260),
    ]),
    ("LONGINES", "HydroConquest Automatic Ceramic 41", [
        ("L3.780.4.56.6", 900, 1030, 1160),
        ("L3.780.4.96.6", 900, 1030, 1160),
    ]),
    ("TAG HEUER", "Aquaracer", [
        ("WAY2015.BA0827", 780, 900, 1030),
        ("WAY2010.BA0927", 700, 820, 940),
        ("WAY2012.BA0927", 720, 840, 960),
        ("WAY201B.BA0927", 1120, 1300, 1480),
    ]),
    ("BREITLING", "SuperOcean Automatic 42", [
        ("A17366", 2100, 2400, 2700),
        ("A17366021B1A1", 2100, 2400, 2700),
    ]),
    ("BREITLING", "SuperOcean Automatic 44", [
        ("A17367", 2300, 2600, 2900),
        ("A17367D71B1A1", 2300, 2600, 2900),
        ("A17375", 2500, 2800, 3150),
    ]),
    ("BREITLING", "Avenger Automatic 43", [
        ("A17318", 2200, 2500, 2800),
        ("A17318101B1A1", 2200, 2500, 2800),
    ]),
    ("ORIS", "Aquis Date", [
        ("01 733 7732", 800, 950, 1100),
        ("01 733 7730", 800, 950, 1100),
        ("01 733 7766", 850, 1000, 1150),
    ]),
    ("ORIS", "Aquis Calibre 400", [
        ("01 400 7769 4157", 1500, 1750, 2000),
        ("01 400 7769 4135", 1500, 1750, 2000),
        ("01 400 7763 4135", 1500, 1750, 2000),
    ]),
    ("RADO", "Captain Cook Automatic", [
        ("R32105153", 820, 950, 1080),
        ("R32505203", 950, 1100, 1250),
        ("R32505313", 950, 1100, 1250),
        ("R32105313", 900, 1050, 1200),
        ("R32154208", 1150, 1350, 1550),
    ]),
    ("BAUME & MERCIER", "Riviera Baumatic", [
        ("MOA10616", 1450, 1700, 1950),
        ("MOA10617", 1450, 1700, 1950),
        ("MOA10660", 1550, 1800, 2050),
        ("MOA10717", 1550, 1800, 2050),
    ]),
    ("SEIKO", "Prospex", [
        ("SPB143", 620, 720, 820),
        ("SPB147", 650, 760, 870),
        ("SPB149", 780, 900, 1030),
        ("SPB185", 600, 700, 800),
        ("SPB187", 640, 750, 860),
        ("SPB239", 600, 700, 800),
        ("SPB297", 700, 810, 920),
    ]),
]

# Additional query phrasings per reference, beyond the auto-generated ones.
EXTRA_TERMS = {
    "79030N": ["Black Bay Fifty Eight"],
    "79030B": ["Black Bay Fifty Eight Blue"],
    "79830RB": ["Black Bay GMT Pepsi"],
    "210.30.42.20": ["Seamaster Professional 300M"],
    "310.30.42.50": ["Speedmaster Professional Moonwatch"],
}


def build() -> dict:
    refs = []
    for brand, model, entries in DATA:
        for reference, low, mid, high in entries:
            refs.append({
                "brand": brand,
                "model": model,
                "reference": reference,
                "market_low": low,
                "market_mid": mid,
                "market_high": high,
                "value_source": "SEED_PLACEHOLDER_UNVERIFIED",
                "confidence": "LOW",
                "extra_terms": EXTRA_TERMS.get(reference, []),
            })
    return {
        "_warning": (
            "market_low/mid/high are unverified placeholders, not market evidence. "
            "Replace them with your own figures before trusting any verdict."
        ),
        "references": refs,
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2), encoding="utf-8")
    print(f"Wrote {len(build()['references'])} references to {OUT}")
