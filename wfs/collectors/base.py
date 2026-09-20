"""Collector foundation (Phase 5.3 §6, §10, §11, §19).

Every automatic evidence source produces `CollectedEvidence` records through the
same interface, so valuation never has to care where a number came from — only
how much it can be trusted.

Three classification axes travel with every record and none of them is optional:

* `evidence_type` — is this a sale, an asking price, or market context?
* `price_certainty` — do we actually know what it sold for?
* `match_type`      — is this the same watch, or merely a similar one?

A collector that cannot answer one of these must return nothing rather than
guess. Nothing here fabricates data: a source that is blocked, disabled or
broken reports that fact and the scan continues.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib import robotparser

# --- evidence types ---------------------------------------------------------

EV_SOLD = "SOLD"                 # a completed transaction
EV_ACTIVE_ASKING = "ACTIVE_ASKING"   # what someone hopes to get
EV_MARKET_CONTEXT = "MARKET_CONTEXT"  # an index or benchmark figure
EV_OBSERVATION = "OBSERVATION"   # our own listing-lifecycle inference

# --- price certainty (§6) ---------------------------------------------------
# The Best Offer problem: eBay shows a crossed-out asking price on accepted-offer
# sales without revealing what was actually paid. Treating that struck-through
# number as the sale price systematically overstates the market.

CONFIRMED_PRICE = "CONFIRMED_PRICE"          # verified transaction price
DISPLAYED_SOLD_PRICE = "DISPLAYED_SOLD_PRICE"  # shown as sold at this price
BEST_OFFER_UNCERTAIN = "BEST_OFFER_UNCERTAIN"  # sold, but price not disclosed
PRICE_UNKNOWN = "PRICE_UNKNOWN"

# Weight each certainty carries in VALUATION. Note BEST_OFFER_UNCERTAIN is zero:
# it proves the watch sold (so it counts for liquidity) but says nothing reliable
# about price.
PRICE_CERTAINTY_WEIGHT = {
    CONFIRMED_PRICE: 1.0,
    DISPLAYED_SOLD_PRICE: 0.85,
    BEST_OFFER_UNCERTAIN: 0.0,
    PRICE_UNKNOWN: 0.0,
}

# Whether the record counts as a sale for LIQUIDITY purposes.
COUNTS_FOR_LIQUIDITY = {
    CONFIRMED_PRICE: True,
    DISPLAYED_SOLD_PRICE: True,
    BEST_OFFER_UNCERTAIN: True,
    PRICE_UNKNOWN: False,
}

# --- match quality (§10) ----------------------------------------------------

MATCH_EXACT = "EXACT_REFERENCE"
MATCH_NORMALIZED = "NORMALIZED_REFERENCE"
# Final 6.0 (spec s.7): a reference that differs only in a way that does not
# change the watch materially (e.g. a strap/bracelet suffix), as opposed to a
# different dial, size or generation — which must never be merged.
MATCH_VARIANT = "HIGH_CONFIDENCE_VARIANT"
MATCH_FAMILY = "MODEL_FAMILY"
# A title that mentions the brand and looks plausible but carries no usable
# reference. Displayed, never allowed to move MAX BUY.
MATCH_WEAK = "WEAK_MATCH"
MATCH_AMBIGUOUS = "AMBIGUOUS"
MATCH_REJECTED = "REJECTED"

MATCH_WEIGHT = {
    MATCH_EXACT: 1.0,
    MATCH_NORMALIZED: 0.95,
    MATCH_VARIANT: 0.75,
    MATCH_FAMILY: 0.45,
    MATCH_WEAK: 0.0,
    MATCH_AMBIGUOUS: 0.0,
    MATCH_REJECTED: 0.0,
}

MATCH_LABEL = {
    MATCH_EXACT: "Exact reference",
    MATCH_NORMALIZED: "Exact reference (normalised)",
    MATCH_VARIANT: "High-confidence variant",
    MATCH_FAMILY: "Model family",
    MATCH_WEAK: "Weak match",
    MATCH_AMBIGUOUS: "Ambiguous",
    MATCH_REJECTED: "Rejected",
}

# Only these may materially influence MAX BUY (spec s.7).
MATCH_MATERIAL = {MATCH_EXACT, MATCH_NORMALIZED, MATCH_VARIANT}

USABLE_MATCHES = {MATCH_EXACT, MATCH_NORMALIZED, MATCH_VARIANT, MATCH_FAMILY}

# --- source health (§19) ----------------------------------------------------

HEALTH_OK = "OK"
HEALTH_PARTIAL = "PARTIAL"
HEALTH_BLOCKED = "BLOCKED"
HEALTH_UNAVAILABLE = "UNAVAILABLE"
HEALTH_DISABLED = "DISABLED"
HEALTH_NOT_RUN = "NOT_RUN"
# Phase 5.3 fix: a collector that ran and threw is distinct from one that was
# never usable. "ERROR" means it tried and failed unexpectedly.
HEALTH_ERROR = "ERROR"
# Final 6.0 statuses (spec s.18). "Enabled" and "operational" are different
# states, and the user must be able to tell them apart at a glance.
HEALTH_NOT_CONFIGURED = "NOT_CONFIGURED"      # needs a key/credential
HEALTH_USER_ACTION = "USER_ACTION_REQUIRED"   # needs the user to do something
HEALTH_REUSED = "REUSED"                      # served from cache, not re-collected
HEALTH_STALE = "STALE"                        # cached data past its freshness window

# Which statuses mean "this source contributed something this run".
HEALTH_PRODUCTIVE = {HEALTH_OK, HEALTH_REUSED, HEALTH_PARTIAL}

HEALTH_EXPLANATION = {
    HEALTH_OK: "Collected successfully.",
    HEALTH_REUSED: "Served from cache — not re-collected this scan.",
    HEALTH_PARTIAL: "Ran, but returned less than expected.",
    HEALTH_STALE: "Cached data is past its freshness window.",
    HEALTH_BLOCKED: "Access is restricted and was not circumvented.",
    HEALTH_UNAVAILABLE: "Source could not be reached or returned nothing usable.",
    HEALTH_NOT_CONFIGURED: "Requires configuration before it can run.",
    HEALTH_USER_ACTION: "Waiting for the user to complete a step.",
    HEALTH_DISABLED: "Switched off in Settings.",
    HEALTH_NOT_RUN: "Has not run yet.",
    HEALTH_ERROR: "Failed unexpectedly.",
}

SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CollectedEvidence:
    """One normalised evidence record (§11)."""

    source: str
    evidence_type: str
    reference: str
    price_gbp: float | None = None
    original_price: float | None = None
    original_currency: str = "GBP"
    brand: str | None = None
    model: str | None = None
    title: str | None = None
    sale_date: str | None = None
    condition: str | None = None
    full_set: bool | None = None
    listing_url: str | None = None
    item_id: str | None = None
    seller_location: str | None = None
    retrieved_at: str = field(default_factory=now_iso)
    confidence: float = 1.0
    price_certainty: str = PRICE_UNKNOWN
    match_type: str = MATCH_AMBIGUOUS
    notes: str | None = None

    @property
    def is_sold(self) -> bool:
        return self.evidence_type == EV_SOLD

    @property
    def usable_for_valuation(self) -> bool:
        """Only a priced, usably-matched sale informs a valuation."""
        return (self.price_gbp is not None and self.price_gbp > 0
                and PRICE_CERTAINTY_WEIGHT.get(self.price_certainty, 0.0) > 0
                and self.match_type in USABLE_MATCHES
                and MATCH_WEIGHT.get(self.match_type, 0.0) > 0)

    @property
    def counts_for_liquidity(self) -> bool:
        """A Best Offer sale proves demand even when the price is hidden."""
        return (self.is_sold
                and COUNTS_FOR_LIQUIDITY.get(self.price_certainty, False)
                and self.match_type in USABLE_MATCHES)

    @property
    def valuation_weight(self) -> float:
        return round(
            PRICE_CERTAINTY_WEIGHT.get(self.price_certainty, 0.0)
            * MATCH_WEIGHT.get(self.match_type, 0.0)
            * max(0.0, min(self.confidence, 1.0)), 4)

    def fingerprint(self) -> str:
        """Stable identity for deduplication (§12).

        Prefers the item id, then the canonical URL, then a content hash. Two
        collections of the same sale must refresh one row, never create a second
        apparent transaction.
        """
        import hashlib

        if self.item_id:
            return f"{self.source}:id:{self.item_id}"
        if self.listing_url:
            return f"{self.source}:url:{self.listing_url.split('?')[0]}"
        raw = "|".join([
            self.source, self.reference or "", (self.title or "")[:80],
            f"{self.price_gbp or 0:.2f}", self.sale_date or "",
        ])
        return f"{self.source}:fp:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceStatus:
    """How one collector behaved on this run (§19)."""

    source: str
    status: str = HEALTH_NOT_RUN
    record_count: int = 0
    last_success: str | None = None
    cache_status: str = "MISS"
    error: str | None = None
    detail: str = ""

    @property
    def is_healthy(self) -> bool:
        return self.status in (HEALTH_OK, HEALTH_PARTIAL)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CollectionResult:
    """What one collector returned for one reference."""

    source: str
    reference: str
    records: list[CollectedEvidence] = field(default_factory=list)
    status: SourceStatus | None = None

    def __post_init__(self) -> None:
        if self.status is None:
            self.status = SourceStatus(self.source, HEALTH_NOT_RUN)

    @classmethod
    def unavailable(cls, source: str, reference: str, reason: str,
                    status: str = HEALTH_UNAVAILABLE) -> "CollectionResult":
        """No data, and an honest reason. Never an exception to the caller."""
        return cls(source, reference, [],
                   SourceStatus(source, status, 0, None, "MISS", reason,
                                SOURCE_UNAVAILABLE))

    @classmethod
    def error(cls, source: str, reference: str, reason: str) -> "CollectionResult":
        return cls(source, reference, [],
                   SourceStatus(source, HEALTH_ERROR, 0, None, "MISS", reason,
                                "Unexpected collector failure."))

    @classmethod
    def disabled(cls, source: str, reference: str,
                 reason: str = "Disabled in settings.") -> "CollectionResult":
        return cls(source, reference, [],
                   SourceStatus(source, HEALTH_DISABLED, 0, None, "MISS", reason))

    @classmethod
    def ok(cls, source: str, reference: str, records: list[CollectedEvidence],
           cache_status: str = "MISS", detail: str = "") -> "CollectionResult":
        return cls(source, reference, records,
                   SourceStatus(source, HEALTH_OK, len(records), now_iso(),
                                cache_status, None, detail))


class EvidenceCollector(Protocol):
    """Interface every collector implements."""

    name: str
    evidence_type: str

    def is_available(self) -> bool: ...

    def collect(self, reference: str, brand: str = "", model: str = "",
                **kwargs: Any) -> CollectionResult: ...


class BaseCollector:
    """Shared politeness machinery: robots.txt, throttling, safe failure."""

    name = "base"
    evidence_type = EV_MARKET_CONTEXT
    base_url = ""
    min_interval_seconds = 6.0
    user_agent = "WatchFlipScannerUK/5.3 (personal research use)"

    _locks: dict[str, threading.Lock] = {}
    _last_request: dict[str, float] = {}

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self._robots: robotparser.RobotFileParser | None = None
        self._robots_failed = False
        # Distinguishes "robots.txt says no" from "robots.txt could not be read".
        # urllib's parser treats an HTTP error on robots.txt as disallow-all,
        # which is correct behaviour but produces a misleading message.
        self._robots_unreadable = False

    # -- availability ------------------------------------------------------
    def is_available(self) -> bool:
        return bool(self.enabled)

    def unavailable_reason(self) -> str:
        """Why this collector cannot run. Override with something specific.

        The generic fallback exists only as a last resort; every collector that
        can diagnose itself should say what is actually wrong.
        """
        if not self.enabled:
            return "Disabled in settings."
        return "Collector reported itself unavailable without a specific reason."

    def transport_description(self) -> str:
        """Which fetch mechanism this collector would use, for diagnostics."""
        if getattr(self, "browser_session", None) is not None:
            return "browser"
        if getattr(self, "http_client", None) is not None:
            return "http"
        return "none"

    def preflight(self) -> dict[str, Any]:
        """Check readiness WITHOUT collecting. Used by Source Health and the
        local smoke test so a failure can be diagnosed before a full scan."""
        info: dict[str, Any] = {
            "source": self.name,
            "enabled": bool(self.enabled),
            "transport": self.transport_description(),
            "base_url": self.base_url,
        }
        if not self.enabled:
            info.update(ready=False, reason="Disabled in settings.")
            return info
        if not self.base_url:
            info.update(ready=bool(self.is_available()),
                        reason="" if self.is_available() else self.unavailable_reason())
            return info

        target = getattr(self, "preflight_url", lambda: self.base_url)()
        try:
            allowed = self.robots_allow(target)
        except Exception as exc:
            info.update(ready=False, robots_allowed=None,
                        reason=f"robots.txt check failed: {exc}")
            return info

        info["robots_allowed"] = allowed
        info["robots_url"] = f"{self.base_url}/robots.txt"
        info["checked_url"] = target
        info["robots_readable"] = not self._robots_unreadable
        if self._robots_failed or self._robots_unreadable:
            info.update(ready=False, robots_allowed=None,
                        reason=(f"Could not read {self.base_url}/robots.txt "
                                "(network error, or the host returned 401/403). "
                                "Access is refused when permission cannot be "
                                "verified — this is not the same as robots.txt "
                                "forbidding access."))
        elif not allowed:
            info.update(ready=False,
                        reason=f"robots.txt explicitly disallows {target}")
        elif self.transport_description() == "none":
            info.update(ready=False,
                        reason="No HTTP client or browser session configured.")
        else:
            info.update(ready=True, reason="Ready.")
        return info

    # -- robots ------------------------------------------------------------
    def robots_allow(self, url: str) -> bool:
        """Refuse to fetch unless robots.txt positively permits it.

        A failure to READ robots.txt returns False. Not being able to check
        permission is not the same as having it.
        """
        if self._robots_failed:
            return False
        if self._robots is None:
            rp = robotparser.RobotFileParser()
            rp.set_url(f"{self.base_url}/robots.txt")
            try:
                rp.read()
            except Exception:
                self._robots_failed = True
                self._robots_unreadable = True
                return False
            # disallow_all is set when robots.txt returned 401/403 — i.e. we were
            # not permitted to read the rules, not that the rules forbid us.
            if getattr(rp, "disallow_all", False) and not getattr(rp, "entries", []):
                self._robots_unreadable = True
            self._robots = rp
        try:
            return self._robots.can_fetch("*", url)
        except Exception:
            return False

    # -- throttling --------------------------------------------------------
    def throttle(self) -> None:
        lock = BaseCollector._locks.setdefault(self.name, threading.Lock())
        with lock:
            elapsed = time.time() - BaseCollector._last_request.get(self.name, 0.0)
            if elapsed < self.min_interval_seconds:
                time.sleep(self.min_interval_seconds - elapsed)
            BaseCollector._last_request[self.name] = time.time()

    # -- entry point -------------------------------------------------------
    def collect(self, reference: str, brand: str = "", model: str = "",
                **kwargs: Any) -> CollectionResult:
        """Never raises. A broken source degrades the scan, it does not stop it."""
        if not self.enabled:
            return CollectionResult.disabled(self.name, reference)
        if not self.is_available():
            return CollectionResult.unavailable(
                self.name, reference, self.unavailable_reason())
        try:
            return self._collect(reference, brand, model, **kwargs)
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            return CollectionResult.error(
                self.name, reference, f"{type(exc).__name__}: {exc}")

    def _collect(self, reference: str, brand: str, model: str,
                 **kwargs: Any) -> CollectionResult:
        raise NotImplementedError


# --- matching (§10) ---------------------------------------------------------

def normalise_reference(text: str) -> str:
    """Uppercase, strip every separator. 'l3.781.4.56.6' == 'L3 781 4 56 6'."""
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _reference_tokens(text: str) -> list[str]:
    """Reference-like tokens from the RAW text.

    Tokenising the separator-stripped string would collapse an entire title into
    one token, which silently defeated conflicting-reference detection.
    """
    upper = (text or "").upper()
    # Keep dots and hyphens inside a token (L3.781.4.56.6, 79030N-0001), then
    # normalise each token separately.
    raw = re.findall(r"[A-Z0-9][A-Z0-9.\-/]{3,}", upper)
    return [normalise_reference(t) for t in raw if normalise_reference(t)]


def classify_match(candidate_text: str, reference: str,
                   model: str = "", brand: str = "") -> str:
    """How confidently does this record refer to the SAME watch? (spec 6.0 s.7)

      EXACT_REFERENCE          the reference appears as its own token
      HIGH_CONFIDENCE_VARIANT  a token that EXTENDS our reference — the same
                               model line with a fuller factory code
      MODEL_FAMILY             brand + model wording, no usable reference
      WEAK_MATCH               brand only, or an untieable reference-like token
      AMBIGUOUS / REJECTED     unusable, or a materially different reference

    Two references differing by one character are usually different watches
    (dial, bezel, size, generation), so a conflicting reference is REJECTED
    rather than demoted to family.
    """
    text = candidate_text or ""
    key = normalise_reference(reference)
    if not key or not text.strip():
        return MATCH_AMBIGUOUS

    tokens = _reference_tokens(text)

    # Exact: our reference stands as its own token.
    if key in tokens:
        return MATCH_EXACT

    # A different reference of the same shape means a different watch.
    if _carries_conflicting_reference(tokens, key):
        return MATCH_REJECTED

    # Variant: a token that extends our reference with a fuller factory code.
    if any(t.startswith(key) and len(t) > len(key) for t in tokens):
        return MATCH_VARIANT

    # Fallback for titles with no clean tokens (reference run together).
    if key in normalise_reference(text):
        return MATCH_NORMALIZED

    if model:
        model_key = normalise_reference(model)
        if model_key and model_key in normalise_reference(text):
            if brand and normalise_reference(brand) not in normalise_reference(text):
                return MATCH_AMBIGUOUS
            return MATCH_FAMILY

    if brand and normalise_reference(brand) in normalise_reference(text):
        return MATCH_WEAK

    return MATCH_REJECTED


def _carries_conflicting_reference(tokens: list[str], key: str) -> bool:
    """True when a DIFFERENT reference of the same shape is present.

    79030B alongside a search for 79030N is a different watch, even though the
    model name matches.
    """
    if len(key) < 5:
        return False
    stem = key[:-1]
    for token in tokens:
        if token == key:
            return False
        if len(token) == len(key) and token.startswith(stem) and token != key:
            return True
    return False


# --- recency (§13) ----------------------------------------------------------

RECENCY_BANDS = [
    (30, 1.00, "0-30 days"),
    (90, 0.85, "31-90 days"),
    (180, 0.60, "91-180 days"),
    (365, 0.35, "181-365 days"),
]
HISTORICAL_WEIGHT = 0.15


def days_since(date_str: str | None) -> float | None:
    if not date_str:
        return None
    try:
        when = datetime.fromisoformat(date_str)
    except (ValueError, TypeError):
        try:
            when = datetime.strptime(date_str, "%Y-%m-%d")
        except (ValueError, TypeError):
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds() / 86400)


def recency_weight(date_str: str | None) -> float:
    """Recent sales describe today's market; old ones describe a past one."""
    age = days_since(date_str)
    if age is None:
        return 0.50          # undated evidence: usable, discounted
    for limit, weight, _ in RECENCY_BANDS:
        if age <= limit:
            return weight
    return HISTORICAL_WEIGHT


def recency_band(date_str: str | None) -> str:
    age = days_since(date_str)
    if age is None:
        return "undated"
    for limit, _, label in RECENCY_BANDS:
        if age <= limit:
            return label
    return ">365 days"
