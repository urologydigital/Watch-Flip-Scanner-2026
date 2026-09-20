"""eBay Browse API client (official API only — no HTML scraping). Spec s.3."""
from __future__ import annotations

import base64
import time
from typing import Any, Iterable

import httpx

from .config import (EBAY_BROWSE_URL, EBAY_MARKETPLACE, EBAY_OAUTH_URL,
                     EBAY_SCOPE, SETTINGS)

WATCH_CATEGORY_ID = "31387"  # Wristwatches


class EbayError(RuntimeError):
    pass


class EbayClient:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 timeout: float | None = None):
        self.client_id = client_id or SETTINGS.ebay_client_id
        self.client_secret = client_secret or SETTINGS.ebay_client_secret
        self.timeout = timeout or SETTINGS.request_timeout
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # -- auth ---------------------------------------------------------------
    def _fetch_token(self) -> str:
        if not (self.client_id and self.client_secret):
            raise EbayError(
                "eBay credentials missing. Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET in .env"
            )
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        resp = httpx.post(
            EBAY_OAUTH_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": EBAY_SCOPE},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise EbayError(f"eBay OAuth failed ({resp.status_code}): {resp.text[:300]}")
        payload = resp.json()
        self._token = payload["access_token"]
        # Refresh a minute early.
        self._token_expiry = time.time() + int(payload.get("expires_in", 7200)) - 60
        return self._token

    def token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        return self._fetch_token()

    # -- search -------------------------------------------------------------
    def search(self, query: str, limit: int = 50,
               exclusions: Iterable[str] = ()) -> list[dict[str, Any]]:
        """Search current EBAY_GB listings. Returns raw itemSummaries."""
        params: dict[str, Any] = {
            "q": query,
            "limit": min(limit, 200),
            "category_ids": WATCH_CATEGORY_ID,
            "filter": "buyingOptions:{FIXED_PRICE|AUCTION|BEST_OFFER},"
                      "itemLocationCountry:GB",
            "sort": "newlyListed",
        }
        headers = {
            "Authorization": f"Bearer {self.token()}",
            "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE,
            "Content-Type": "application/json",
        }
        resp = httpx.get(EBAY_BROWSE_URL, params=params, headers=headers,
                         timeout=self.timeout)
        if resp.status_code == 401:
            self._token = None
            headers["Authorization"] = f"Bearer {self.token()}"
            resp = httpx.get(EBAY_BROWSE_URL, params=params, headers=headers,
                             timeout=self.timeout)
        if resp.status_code != 200:
            raise EbayError(f"eBay Browse failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json().get("itemSummaries") or []


# --- normalisation ----------------------------------------------------------

def _money(node: dict[str, Any] | None) -> tuple[float | None, str | None]:
    if not node:
        return None, None
    try:
        return float(node.get("value")), node.get("currency")
    except (TypeError, ValueError):
        return None, node.get("currency")


def normalise_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert an eBay itemSummary into our internal listing shape."""
    price, currency = _money(item.get("price"))
    shipping = None
    for opt in item.get("shippingOptions") or []:
        s, _ = _money(opt.get("shippingCost"))
        if s is not None:
            shipping = s if shipping is None else min(shipping, s)
    if shipping is None:
        shipping = 0.0

    buying_options = item.get("buyingOptions") or []
    if "AUCTION" in buying_options:
        buying_format = "AUCTION"
    elif "FIXED_PRICE" in buying_options:
        buying_format = "BUY_IT_NOW"
    else:
        buying_format = ",".join(buying_options) or "UNKNOWN"

    seller = item.get("seller") or {}
    location = item.get("itemLocation") or {}

    total = None if price is None else round(price + shipping, 2)

    return {
        "item_id": item.get("itemId"),
        "source": "EBAY_GB",
        "title": item.get("title"),
        "price": price,
        "currency": currency,
        "shipping": shipping,
        "total_acquisition": total,
        "buying_format": buying_format,
        "best_offer": "BEST_OFFER" in buying_options,
        "condition": item.get("condition"),
        "seller": seller.get("username"),
        "seller_feedback_score": seller.get("feedbackScore"),
        "seller_feedback_pct": _pct(seller.get("feedbackPercentage")),
        "item_location": ", ".join(
            filter(None, [location.get("city"), location.get("country")])
        ) or None,
        "url": item.get("itemWebUrl"),
        "image_url": (item.get("image") or {}).get("imageUrl"),
        "authenticity_guarantee": bool(item.get("qualifiedPrograms")
                                       and "AUTHENTICITY_GUARANTEE"
                                       in item["qualifiedPrograms"]),
        "returns_accepted": None,  # only exposed on the item detail endpoint
        "listing_created": item.get("itemCreationDate"),
        "raw": item,
    }


def _pct(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
