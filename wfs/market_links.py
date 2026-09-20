"""Direct market-research links (spec 5.2 §4, §5, §14).

The fastest path from "this looks interesting" to "I have checked it myself" is
a link. No API key, no subscription, no scraping — just take the user straight
to the listing and to the comparables.

Search links are built from BRAND + REFERENCE, never the noisy listing title,
because a title like "Tudor Black Bay 58 79030N 39mm Mens Automatic Dive Watch
Box Papers 2023 MINT" produces useless comparables.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

EBAY_SEARCH = "https://www.ebay.co.uk/sch/i.html"
EBAY_ITEM = "https://www.ebay.co.uk/itm/"
CHRONO24_SEARCH = "https://www.chrono24.co.uk/search/index.htm"
WATCHCHARTS_SEARCH = "https://watchcharts.com/watches/search"
GOOGLE_SEARCH = "https://www.google.com/search"

# eBay category 31387 = Wristwatches. Keeps straps and parts out of comparables.
EBAY_WATCH_CATEGORY = "31387"


def search_term(brand: str | None, reference: str | None,
                model: str | None = None) -> str:
    """Normalised BRAND + REFERENCE search term.

    Falls back to brand + model only when there is no reference, since a search
    with neither is worse than useless.
    """
    parts: list[str] = []
    if brand:
        parts.append(str(brand).strip())
    if reference and str(reference).strip():
        parts.append(str(reference).strip())
    elif model:
        parts.append(str(model).strip())
    return " ".join(p for p in parts if p).strip()


def ebay_listing_url(listing: dict[str, Any]) -> str | None:
    """The canonical URL from the Browse API.

    eBay's own URL is used whenever present. A URL is only reconstructed from
    the item ID as a last resort, because a hand-built URL can silently rot.
    """
    url = listing.get("url")
    if url:
        return url
    item_id = listing.get("item_id")
    if not item_id:
        return None
    # Browse API IDs look like "v1|123456789012|0"; the middle part is the
    # legacy item number the public /itm/ route expects.
    parts = str(item_id).split("|")
    legacy = parts[1] if len(parts) >= 2 else parts[0]
    if not legacy.isdigit():
        return None
    return f"{EBAY_ITEM}{legacy}"


def ebay_search_url(brand: str | None, reference: str | None,
                    model: str | None = None, sold: bool = False) -> str:
    term = search_term(brand, reference, model)
    query = f"_nkw={quote_plus(term)}&_sacat={EBAY_WATCH_CATEGORY}"
    if sold:
        # Public completed/sold filter — the same view a person would use.
        query += "&LH_Sold=1&LH_Complete=1"
    return f"{EBAY_SEARCH}?{query}"


def chrono24_search_url(brand: str | None, reference: str | None,
                        model: str | None = None) -> str:
    term = search_term(brand, reference, model)
    return f"{CHRONO24_SEARCH}?query={quote_plus(term)}&currencyId=GBP"


def watchcharts_search_url(brand: str | None, reference: str | None,
                           model: str | None = None) -> str:
    term = search_term(brand, reference, model)
    return f"{WATCHCHARTS_SEARCH}?q={quote_plus(term)}"


def google_search_url(brand: str | None, reference: str | None,
                      model: str | None = None) -> str:
    term = search_term(brand, reference, model)
    return f"{GOOGLE_SEARCH}?q={quote_plus(term + ' watch review specifications')}"


@dataclass
class MarketLinks:
    """Every research action offered for one candidate."""

    listing: str | None
    ebay_search: str
    ebay_sold_search: str
    chrono24: str
    watchcharts: str
    google: str
    term: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    def buttons(self) -> list[tuple[str, str]]:
        """(label, url) pairs in the order they should be presented."""
        out: list[tuple[str, str]] = []
        if self.listing:
            out.append(("Open eBay Listing", self.listing))
        out.extend([
            ("Search eBay", self.ebay_search),
            ("eBay Sold/Completed", self.ebay_sold_search),
            ("Chrono24 Comparables", self.chrono24),
            ("Check WatchCharts", self.watchcharts),
        ])
        return out


def build_links(listing: dict[str, Any], brand: str | None = None,
                reference: str | None = None,
                model: str | None = None) -> MarketLinks:
    brand = brand or listing.get("brand")
    reference = reference or listing.get("reference")
    model = model or listing.get("model")
    return MarketLinks(
        listing=ebay_listing_url(listing),
        ebay_search=ebay_search_url(brand, reference, model),
        ebay_sold_search=ebay_search_url(brand, reference, model, sold=True),
        chrono24=chrono24_search_url(brand, reference, model),
        watchcharts=watchcharts_search_url(brand, reference, model),
        google=google_search_url(brand, reference, model),
        term=search_term(brand, reference, model),
    )
