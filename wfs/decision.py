"""BUY / WATCH / PASS engine, target offers and explanations (spec s.13, s.16).

Every recommendation carries a human-readable reason. The scanner explains its
reasoning rather than emitting an opaque score.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .condition import ConditionAssessment
from .config5 import CONFIG, Phase5Config
from .evidence import ConfidenceScore, MarketEvidence
from .flip import (BASE, CapitalVelocity, DaysToSell, FlipScore, LiquidityProfile,
                   MaxBuyResult, Strategy)
from .risk import RiskScore

BUY, WATCH, PASS = "BUY", "WATCH", "PASS"

# Phase 5.3 §18: machine-readable reasons a candidate did not reach BUY. These
# accompany the existing prose blockers so the UI can show WHY, consistently.
GATE_INSUFFICIENT_SOLD_EVIDENCE = "INSUFFICIENT_SOLD_EVIDENCE"
GATE_PRICE_UNCERTAIN = "PRICE_UNCERTAIN"
GATE_LOW_LIQUIDITY = "LOW_LIQUIDITY"
GATE_REFERENCE_AMBIGUOUS = "REFERENCE_AMBIGUOUS"
GATE_AUTHENTICITY_RISK = "AUTHENTICITY_RISK"
GATE_CONDITION_RISK = "CONDITION_RISK"
GATE_MARGIN_TOO_SMALL = "MARGIN_TOO_SMALL"
GATE_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"

GATE_LABEL = {
    GATE_INSUFFICIENT_SOLD_EVIDENCE: "Insufficient sold evidence",
    GATE_PRICE_UNCERTAIN: "Sale prices not verifiable",
    GATE_LOW_LIQUIDITY: "Low liquidity",
    GATE_REFERENCE_AMBIGUOUS: "Reference match ambiguous",
    GATE_AUTHENTICITY_RISK: "Authenticity risk",
    GATE_CONDITION_RISK: "Condition risk",
    GATE_MARGIN_TOO_SMALL: "Margin too small",
    GATE_SOURCE_UNAVAILABLE: "Evidence source unavailable",
}
VERDICT_ORDER = {BUY: 0, WATCH: 1, PASS: 2}
VERDICT_ICON = {BUY: "🟢", WATCH: "🟡", PASS: "🔴"}


@dataclass
class TargetOffer:
    low: float
    high: float
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Decision:
    verdict: str
    flip_score: float
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    target_offer: TargetOffer | None = None
    why: str = ""
    # Phase 5.1: qualifier shown beside a WATCH, e.g. "Needs Market Confirmation".
    qualifier: str = ""
    # Phase 5.3: structured gate codes explaining why BUY was not reached.
    gates: list[str] = field(default_factory=list)

    @property
    def display_verdict(self) -> str:
        return f"{self.verdict} — {self.qualifier}" if self.qualifier else self.verdict

    @property
    def icon(self) -> str:
        return VERDICT_ICON.get(self.verdict, "")

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "flip_score": self.flip_score,
            "reasons": self.reasons,
            "blockers": self.blockers,
            "target_offer": self.target_offer.as_dict() if self.target_offer else None,
            "why": self.why,
            "qualifier": self.qualifier,
            "display_verdict": self.display_verdict,
            "gates": self.gates,
            "gate_labels": [GATE_LABEL.get(g, g) for g in self.gates],
        }


def _target_offer(max_buy: MaxBuyResult, acquisition: float,
                  config: Phase5Config) -> TargetOffer | None:
    """What to offer on a watch that is attractive but priced too high.

    Anchored below Standard MAX BUY, never above it, so a successful negotiation
    still leaves the required margin intact.
    """
    ceiling = max_buy.standard
    if ceiling is None or acquisition <= 0:
        return None
    if acquisition <= ceiling:
        return None  # already within MAX BUY; no offer needed

    overprice_pct = (acquisition - ceiling) / ceiling * 100
    if overprice_pct > config.decision.watch_max_overprice_pct:
        return None  # too far out for an offer to be credible

    low = round(min(ceiling * 0.94, max_buy.conservative or ceiling * 0.94), 2)
    high = round(ceiling, 2)
    return TargetOffer(
        low=low, high=high,
        rationale=(f"Listed at £{acquisition:,.0f}, which is {overprice_pct:.0f}% above "
                   f"Standard MAX BUY of £{ceiling:,.0f}. An offer of "
                   f"£{low:,.0f}–£{high:,.0f} keeps the required margin intact."),
    )


def _why_buy(base: Strategy, evidence: MarketEvidence, confidence: ConfidenceScore,
             liquidity: LiquidityProfile, max_buy: MaxBuyResult,
             condition: ConditionAssessment, acquisition: float) -> str:
    _, mid, _ = evidence.valuation_band()
    adjusted = (mid or 0) * condition.value_multiplier
    discount = ((adjusted - acquisition) / adjusted * 100) if adjusted else 0
    return (
        f"Listing is approximately {discount:.0f}% below the evidence-backed UK market "
        f"of £{adjusted:,.0f}. {evidence.sold_count_90d} example(s) sold in the last 90 days "
        f"({evidence.level_label.lower()}). Expected Base resale is £{base.sale_price:,.0f} "
        f"with estimated net profit of £{base.net_profit:,.0f} "
        f"({base.net_roi_pct:.0f}% ROI) after all costs. Expected holding period is "
        f"{DaysToSell.format(base.days)}. Purchase price is below Standard MAX BUY of "
        f"£{max_buy.standard:,.0f}. Market confidence {confidence.score:.0f}/100, "
        f"liquidity {liquidity.band.lower()}. Completeness: {condition.completeness_label.lower()}."
    )


def _why_watch(blockers: list[str], base: Strategy | None, offer: TargetOffer | None,
               evidence: MarketEvidence, acquisition: float) -> str:
    lead = "Attractive but not yet actionable. "
    detail = " ".join(blockers[:3])
    if offer:
        detail += f" {offer.rationale}"
    elif base and base.net_profit:
        detail += (f" At the current price the Base case nets "
                   f"£{base.net_profit:,.0f}.")
    if not evidence.has_sold_evidence:
        detail += (" No sold evidence is available for this reference, so the "
                   "valuation cannot be trusted enough to commit capital.")
    return lead + detail.strip()


def _why_pass(blockers: list[str], evidence: MarketEvidence,
              base: Strategy | None, liquidity: LiquidityProfile) -> str:
    detail = " ".join(blockers[:3])
    if not evidence.has_sold_evidence and evidence.current_active_listing_count:
        detail += (" The apparent discount is measured against asking prices rather "
                   "than achieved sales, which routinely overstates upside.")
    if liquidity.band == "LOW" and base and base.days:
        detail += (f" Expected holding period of {DaysToSell.format(base.days)} ties up "
                   "capital for too long relative to the return.")
    return detail.strip() or "Economics do not justify the purchase."


def decide(evidence: MarketEvidence, confidence: ConfidenceScore,
           liquidity: LiquidityProfile, condition: ConditionAssessment,
           risk: RiskScore, max_buy: MaxBuyResult, strategies: dict[str, Strategy],
           velocity: CapitalVelocity, flip_score: FlipScore, acquisition: float,
           config: Phase5Config = CONFIG) -> Decision:
    """Apply the decision rules. Every gate produces an explanation."""
    d = config.decision
    base = strategies.get(BASE)
    reasons: list[str] = []
    blockers: list[str] = []

    # --- hard stops -------------------------------------------------------
    if base is None or max_buy.standard is None:
        return Decision(
            PASS, flip_score.score,
            reasons=["Insufficient Market Evidence to value this watch."],
            blockers=["No usable market evidence."],
            why=("Insufficient Market Evidence. Without observed UK sales or "
                 "credible asking data this watch cannot be valued, so no "
                 "recommendation is possible."),
            gates=[GATE_INSUFFICIENT_SOLD_EVIDENCE],
        )

    if evidence.unverified:
        blockers.append("Valuation rests on an UNVERIFIED seed placeholder, not "
                        "market observation.")

    if evidence.has_observed_activity:
        blockers.append("Valuation rests on the scanner's own observed listing "
                        "activity. Disappearances are not confirmed sales, so this "
                        "needs market confirmation.")
    elif not evidence.has_sold_evidence:
        blockers.append("No sold evidence — valuation is based on asking prices only.")

    # --- economics --------------------------------------------------------
    within_standard = acquisition <= max_buy.standard
    within_conservative = (max_buy.conservative is not None
                           and acquisition <= max_buy.conservative)

    if within_conservative:
        reasons.append(f"Price £{acquisition:,.0f} is within Conservative MAX BUY "
                       f"£{max_buy.conservative:,.0f}.")
    elif within_standard:
        reasons.append(f"Price £{acquisition:,.0f} is within Standard MAX BUY "
                       f"£{max_buy.standard:,.0f}.")
    else:
        blockers.append(f"Price £{acquisition:,.0f} exceeds Standard MAX BUY "
                        f"£{max_buy.standard:,.0f}.")

    if base.net_profit < d.min_net_profit_gbp:
        blockers.append(f"Base net profit £{base.net_profit:,.0f} is below the "
                        f"£{d.min_net_profit_gbp:,.0f} minimum.")
    else:
        reasons.append(f"Base net profit £{base.net_profit:,.0f} after all costs.")

    if base.net_roi_pct is None or base.net_roi_pct < d.min_net_roi_pct:
        blockers.append(f"Net ROI {base.net_roi_pct or 0:.1f}% is below the "
                        f"{d.min_net_roi_pct:.0f}% minimum.")
    else:
        reasons.append(f"Net ROI {base.net_roi_pct:.0f}%.")

    # --- evidence, liquidity, holding period, risk ------------------------
    if confidence.score < d.min_confidence:
        blockers.append(f"Market confidence {confidence.score:.0f}/100 is below the "
                        f"{d.min_confidence:.0f} minimum.")
    else:
        reasons.append(f"Market confidence {confidence.score:.0f}/100 "
                       f"({confidence.band.lower()}).")

    if liquidity.score < d.min_liquidity:
        blockers.append(f"Liquidity {liquidity.score:.0f}/100 is below the "
                        f"{d.min_liquidity:.0f} minimum.")
    else:
        reasons.append(f"Liquidity {liquidity.score:.0f}/100 ({liquidity.band.lower()}).")

    if base.days and base.days[1] > d.max_holding_days:
        blockers.append(f"Expected holding period of up to {base.days[1]} days exceeds "
                        f"the {d.max_holding_days}-day limit.")
    elif base.days:
        reasons.append(f"Expected holding period {DaysToSell.format(base.days)}.")

    if risk.total > d.max_risk_score:
        blockers.append(f"Risk score {risk.total:.0f}/100 exceeds the "
                        f"{d.max_risk_score:.0f} limit ({risk.band.lower()}).")
    else:
        reasons.append(f"Risk {risk.total:.0f}/100 ({risk.band.lower()}).")

    if condition.damage or condition.aftermarket_parts:
        blockers.append("Condition flags (damage or non-original parts) undermine "
                        "the resale assumption.")

    # --- Phase 5.3: structured gate codes mirroring the prose blockers -----
    gates: list[str] = []
    if not evidence.has_sold_evidence or evidence.unverified:
        gates.append(GATE_INSUFFICIENT_SOLD_EVIDENCE)
    if confidence.score < d.min_confidence:
        gates.append(GATE_INSUFFICIENT_SOLD_EVIDENCE)
    if liquidity.score < d.min_liquidity or liquidity.band == "UNKNOWN":
        gates.append(GATE_LOW_LIQUIDITY)
    if risk.total > d.max_risk_score:
        gates.append(GATE_AUTHENTICITY_RISK)
    if condition.damage or condition.aftermarket_parts or condition.reference_unclear:
        gates.append(GATE_CONDITION_RISK)
    if condition.reference_unclear:
        gates.append(GATE_REFERENCE_AMBIGUOUS)
    if base.net_profit < d.min_net_profit_gbp or (
            base.net_roi_pct is not None and base.net_roi_pct < d.min_net_roi_pct):
        gates.append(GATE_MARGIN_TOO_SMALL)
    if acquisition > max_buy.standard:
        gates.append(GATE_MARGIN_TOO_SMALL)
    gates = sorted(set(gates))

    # --- verdict ----------------------------------------------------------
    offer = _target_offer(max_buy, acquisition, config)

    if not blockers and flip_score.score >= d.buy_min_flip_score:
        return Decision(
            BUY, flip_score.score, reasons, blockers, None,
            _why_buy(base, evidence, confidence, liquidity, max_buy, condition,
                     acquisition),
        )

    if not blockers:
        blockers.append(f"Flip Score {flip_score.score:.0f}/100 is below the "
                        f"{d.buy_min_flip_score:.0f} required for a BUY.")

    # WATCH if it is close: either a credible offer would fix it, or the only
    # problems are evidence and price rather than fundamentals.
    fixable = offer is not None or (within_standard and flip_score.score
                                    >= d.watch_min_flip_score)
    fundamentally_broken = (
        risk.total > d.max_risk_score + 20
        or (base.days and base.days[1] > d.max_holding_days * 1.5)
        or base.net_profit <= 0
        or condition.damage
    )

    qualifier = ("Needs Market Confirmation"
                 if not evidence.has_sold_evidence else "")

    if fixable and not fundamentally_broken:
        return Decision(WATCH, flip_score.score, reasons, blockers, offer,
                        _why_watch(blockers, base, offer, evidence, acquisition),
                        qualifier, gates)

    if (flip_score.score >= d.watch_min_flip_score and not fundamentally_broken
            and offer is not None):
        return Decision(WATCH, flip_score.score, reasons, blockers, offer,
                        _why_watch(blockers, base, offer, evidence, acquisition),
                        qualifier, gates)

    return Decision(PASS, flip_score.score, reasons, blockers, None,
                    _why_pass(blockers, evidence, base, liquidity), "", gates)
