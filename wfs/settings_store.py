"""Local settings persistence (spec 5.1 §22).

Seller profile and economics assumptions are saved to `settings.json` so they
survive a restart. Credentials are NOT stored here — they stay in `.env`, which
is gitignored. This file is safe to keep alongside the project.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config5 import ROOT_DIR
from .economics import (CUSTOM, PROFILE_BUILDERS, UK_BUSINESS, UK_PRIVATE,
                        EconomicsEngine, SellerProfile, get_profile)

SETTINGS_PATH = Path(ROOT_DIR) / "settings.json"

# Keys that must never appear in settings.json.
SECRET_KEYS = {"ebay_client_id", "ebay_client_secret", "openai_api_key",
               "anthropic_api_key", "api_key", "client_secret", "password",
               "token"}

DEFAULT_SETTINGS: dict[str, Any] = {
    "seller_profile": UK_PRIVATE,
    "overrides": {},
    # Phase 5.3: which evidence collectors run. Persisted so the user never has
    # to re-enable an optional source after a restart.
    "evidence_collection": {},
    "_note": ("Economics settings only. Never store API credentials here — "
              "they belong in .env."),
}

# Fields the settings panel is allowed to override on a profile.
EDITABLE_FIELDS = [
    "transaction_fee_pct", "payment_processing_fee_pct",
    "regulatory_operating_fee_pct", "fixed_fee_gbp",
    "promoted_listing_enabled", "promoted_listing_fee_pct",
    "international_sale", "international_fee_pct",
    "shipping_cost_gbp", "insurance_cost_gbp", "packaging_cost_gbp",
    "authentication_cost_gbp", "miscellaneous_cost_gbp",
    "service_reserve_pct", "negotiation_allowance_pct",
]


def _strip_secrets(data: dict[str, Any]) -> dict[str, Any]:
    """Defensive: never persist anything that looks like a credential."""
    return {k: v for k, v in data.items()
            if k.lower() not in SECRET_KEYS
            and not any(s in k.lower() for s in ("secret", "token", "api_key"))}


def load_settings(path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path or SETTINGS_PATH)
    if not p.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_SETTINGS)
    merged = dict(DEFAULT_SETTINGS)
    merged.update(_strip_secrets(data))
    if merged.get("seller_profile") not in PROFILE_BUILDERS:
        merged["seller_profile"] = UK_PRIVATE
    merged["overrides"] = {
        k: v for k, v in (merged.get("overrides") or {}).items()
        if k in EDITABLE_FIELDS
    }
    if not isinstance(merged.get("evidence_collection"), dict):
        merged["evidence_collection"] = {}
    return merged


def save_settings(settings: dict[str, Any],
                  path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path or SETTINGS_PATH)
    clean = _strip_secrets(dict(settings))
    clean.setdefault("_note", DEFAULT_SETTINGS["_note"])
    clean["overrides"] = {k: v for k, v in (clean.get("overrides") or {}).items()
                          if k in EDITABLE_FIELDS}
    if not isinstance(clean.get("evidence_collection"), dict):
        clean["evidence_collection"] = {}
    p.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    return clean


def profile_from_settings(settings: dict[str, Any] | None = None) -> SellerProfile:
    s = settings if settings is not None else load_settings()
    return get_profile(s.get("seller_profile", UK_PRIVATE),
                       **(s.get("overrides") or {}))


def engine_from_settings(settings: dict[str, Any] | None = None) -> EconomicsEngine:
    """The application's economics engine, built from saved settings."""
    return EconomicsEngine(profile_from_settings(settings))
