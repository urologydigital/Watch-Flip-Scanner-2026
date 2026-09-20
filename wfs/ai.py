"""AI analysis of shortlisted candidates only (spec s.15, s.21).

Structured JSON in, structured JSON out. The AI is a second opinion on
authenticity, condition and documents — the areas where free text in a listing
carries signal that deterministic code cannot read.

It is explicitly NOT permitted to move MAX BUY, and it can only downgrade a
verdict, never upgrade one. See reconcile().
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .config import SETTINGS

VERDICTS = ("BUY", "WATCH", "PASS")
VERDICT_RANK = {"BUY": 0, "WATCH": 1, "PASS": 2}

REQUIRED_FIELDS = (
    "reference_verified", "likely_reference", "authenticity_risk", "condition_risk",
    "document_risk", "liquidity_comment", "pricing_comment", "mispricing_reason",
    "verdict", "confidence", "warnings",
)

SYSTEM_PROMPT = """You are a cautious UK watch-trading analyst assisting a decision-support tool.

You will receive one eBay listing plus the tool's own deterministic analysis.

Your job is to scrutinise what the deterministic code cannot read: the free text of
the listing, internal consistency of the reference against the described watch, and
what the documents and condition claims imply.

Rules you must follow:
- A low price is a reason for MORE scrutiny, never a reason to be more positive.
- Never propose a purchase price. The tool's MAX BUY is fixed and not yours to change.
- If the listing text is thin or ambiguous, say so and lower your confidence.
- Do not invent sales data, market values, or facts absent from the input.
- Reply with a single JSON object and nothing else. No prose, no markdown fences.

JSON schema:
{
  "reference_verified": true|false,
  "likely_reference": "string — the reference the watch actually appears to be",
  "authenticity_risk": "LOW"|"MEDIUM"|"HIGH",
  "condition_risk": "LOW"|"MEDIUM"|"HIGH",
  "document_risk": "LOW"|"MEDIUM"|"HIGH",
  "liquidity_comment": "one sentence",
  "pricing_comment": "one sentence",
  "mispricing_reason": "why this may be underpriced, or why the discount looks explicable",
  "verdict": "BUY"|"WATCH"|"PASS",
  "confidence": "HIGH"|"MEDIUM"|"LOW",
  "warnings": ["short strings"]
}"""


@dataclass
class AIResult:
    ok: bool
    model: str
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def verdict(self) -> str | None:
        v = self.payload.get("verdict")
        return v if v in VERDICTS else None

    @property
    def confidence(self) -> str | None:
        return self.payload.get("confidence")

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "model": self.model, "error": self.error, **self.payload}


def build_payload(cand) -> dict[str, Any]:
    """Compact structured input. Only what the model needs to judge."""
    l = cand.listing
    scenarios = {
        name: {"price": s.price, "profit": s.gross_profit,
               "days_low": s.days_low, "days_high": s.days_high}
        for name, s in cand.scenarios.items()
    }
    return {
        "listing": {
            "title": l.get("title"),
            "price_gbp": l.get("price"),
            "shipping_gbp": l.get("shipping"),
            "total_acquisition_gbp": l.get("total_acquisition"),
            "buying_format": l.get("buying_format"),
            "best_offer": l.get("best_offer"),
            "condition": l.get("condition"),
            "seller_feedback_pct": l.get("seller_feedback_pct"),
            "seller_feedback_score": l.get("seller_feedback_score"),
            "item_location": l.get("item_location"),
            "authenticity_guarantee": l.get("authenticity_guarantee"),
        },
        "expected_watch": {
            "brand": cand.ref.brand,
            "model": cand.ref.model,
            "reference": cand.ref.reference,
        },
        "deterministic_analysis": {
            "market_low": cand.market.market_low,
            "market_mid": cand.market.market_mid,
            "market_high": cand.market.market_high,
            "market_confidence": cand.market.confidence,
            "market_confidence_reason": cand.market.confidence_reason,
            "market_is_seed_placeholder": cand.market.seed_only,
            "observed_sold_evidence": cand.sold.describe(),
            "liquidity_rating": cand.liquidity.rating,
            "liquidity_basis": cand.liquidity.basis,
            "competing_uk_listings": cand.liquidity.competing_listings,
            "risk_level": cand.risk.level,
            "risk_factors": cand.risk.factors + cand.risk.scrutiny_factors,
            "max_buy_gbp": cand.maxbuy.value,
            "max_buy_is_fixed": True,
            "deterministic_verdict": cand.verdict,
            "resale_scenarios": scenarios,
        },
        "asking_market": cand.asking_notes if hasattr(cand, "asking_notes") else [],
    }


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
        cleaned = cleaned.replace("json\n", "", 1)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in response")
    return json.loads(cleaned[start:end + 1])


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    """Reject or normalise anything the model returned out of schema."""
    missing = [f for f in REQUIRED_FIELDS if f not in payload]
    if missing:
        raise ValueError(f"Missing fields: {', '.join(missing)}")
    if payload["verdict"] not in VERDICTS:
        raise ValueError(f"Invalid verdict: {payload['verdict']!r}")
    for key in ("authenticity_risk", "condition_risk", "document_risk"):
        if payload[key] not in ("LOW", "MEDIUM", "HIGH"):
            payload[key] = "HIGH"  # unreadable risk is treated as high, not ignored
    if payload.get("confidence") not in ("HIGH", "MEDIUM", "LOW"):
        payload["confidence"] = "LOW"
    if not isinstance(payload.get("warnings"), list):
        payload["warnings"] = [str(payload.get("warnings"))]
    # Strip any attempt to restate pricing the model is not allowed to set.
    for banned in ("max_buy", "max_buy_gbp", "recommended_price", "suggested_max_buy"):
        payload.pop(banned, None)
    return payload


# --- clients ----------------------------------------------------------------

class AnthropicClient:
    name = "anthropic"
    model = "claude-sonnet-4-6"

    def complete(self, payload: dict[str, Any]) -> str:
        import httpx

        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": SETTINGS.anthropic_api_key or "",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 1000,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": json.dumps(payload)}],
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        blocks = resp.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


class OpenAIClient:
    name = "openai"
    model = "gpt-4o-mini"

    def complete(self, payload: dict[str, Any]) -> str:
        import httpx

        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {SETTINGS.openai_api_key or ''}",
                     "Content-Type": "application/json"},
            json={
                "model": self.model,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload)},
                ],
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def default_client():
    if SETTINGS.anthropic_api_key:
        return AnthropicClient()
    if SETTINGS.openai_api_key:
        return OpenAIClient()
    return None


def analyse_candidate(cand, client=None) -> AIResult:
    client = client or default_client()
    if client is None:
        return AIResult(False, "none", error="No AI API key configured.")
    try:
        raw = client.complete(build_payload(cand))
        payload = validate(_extract_json(raw))
    except Exception as exc:
        return AIResult(False, getattr(client, "model", "unknown"), error=str(exc))
    return AIResult(True, getattr(client, "model", "unknown"), payload)


# --- reconciliation ---------------------------------------------------------

def reconcile(deterministic_verdict: str, ai: AIResult) -> tuple[str, list[str]]:
    """Combine the two verdicts. The AI may only make the outcome more cautious.

    An AI upgrade (e.g. deterministic WATCH -> AI BUY) is recorded for your
    attention but never applied, because the deterministic downgrades exist for
    reasons the model cannot verify — missing sold evidence, unknown liquidity,
    an unsettled auction price.
    """
    notes: list[str] = []
    if not ai.ok:
        notes.append(f"AI analysis unavailable ({ai.error}). Deterministic verdict stands.")
        return deterministic_verdict, notes

    ai_verdict = ai.verdict
    if ai_verdict is None:
        notes.append("AI returned no usable verdict. Deterministic verdict stands.")
        return deterministic_verdict, notes

    det_rank = VERDICT_RANK[deterministic_verdict]
    ai_rank = VERDICT_RANK[ai_verdict]

    if ai_rank > det_rank:
        notes.append(f"AI downgraded {deterministic_verdict} to {ai_verdict}: "
                     f"{ai.payload.get('mispricing_reason', 'see AI detail')}.")
        return ai_verdict, notes

    if ai_rank < det_rank:
        notes.append(f"AI suggested {ai_verdict}, but the deterministic verdict "
                     f"{deterministic_verdict} stands — an AI upgrade is never applied.")
        return deterministic_verdict, notes

    notes.append(f"AI agrees: {ai_verdict}.")
    return deterministic_verdict, notes
