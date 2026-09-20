"""Automatic market-evidence collectors (Phase 5.3).

Each collector answers one question from one source and returns normalised
`CollectedEvidence`. None of them can crash a scan: failures surface as source
health, not exceptions.
"""
from .base import (BEST_OFFER_UNCERTAIN, CONFIRMED_PRICE, DISPLAYED_SOLD_PRICE,
                   EV_ACTIVE_ASKING, EV_MARKET_CONTEXT, EV_OBSERVATION, EV_SOLD,
                   MATCH_AMBIGUOUS, MATCH_EXACT, MATCH_FAMILY, MATCH_NORMALIZED,
                   MATCH_REJECTED, PRICE_UNKNOWN, BaseCollector,
                   CollectedEvidence, CollectionResult, SourceStatus,
                   classify_match, normalise_reference, recency_weight)

__all__ = [
    "BaseCollector", "CollectedEvidence", "CollectionResult", "SourceStatus",
    "classify_match", "normalise_reference", "recency_weight",
    "EV_SOLD", "EV_ACTIVE_ASKING", "EV_MARKET_CONTEXT", "EV_OBSERVATION",
    "CONFIRMED_PRICE", "DISPLAYED_SOLD_PRICE", "BEST_OFFER_UNCERTAIN",
    "PRICE_UNKNOWN", "MATCH_EXACT", "MATCH_NORMALIZED", "MATCH_FAMILY",
    "MATCH_AMBIGUOUS", "MATCH_REJECTED",
]
