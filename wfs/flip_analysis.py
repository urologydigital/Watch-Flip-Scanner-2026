"""Phase 5 orchestration: the complete Flip Analysis (spec s.1, s.14, s.17).

Pulls the engines together into one object per candidate, ranks opportunities by
genuine attractiveness rather than headline discount, and offers capital
allocation across a budget.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .active_market import ActiveBenchmark
from .condition import ConditionAssessment, assess_condition
from .cross_check import (CROSS_CHECK, CROSS_CHECK_CONFIG, CrossCheckConfig,
                          CrossCheckDecision, evaluate_cross_check)
from .market_links import MarketLinks, build_links
from .config5 import CONFIG, Phase5Config
from .decision import BUY, PASS, VERDICT_ORDER, WATCH, Decision, decide
from .evidence import (ConfidenceScore, MarketEvidence, from_sold_evidence,
                       score_confidence)
from .flip import (BASE, PATIENT, QUICK, CapitalVelocity, DaysToSell, FlipScore,
                   LiquidityProfile, MaxBuyResult, Strategy, assess_liquidity,
                   build_strategies, calculate_flip_score, calculate_max_buy,
                   capital_velocity, effective_acquisition, estimate_days_to_sell)
from .risk import RiskScore, score_risk
from .watchlist import WatchRef


@dataclass
class FlipAnalysis:
    """The complete answer for one candidate watch."""

    item_id: str
    listing: dict[str, Any]
    ref: WatchRef
    evidence: MarketEvidence
    confidence: ConfidenceScore
    liquidity: LiquidityProfile
    condition: ConditionAssessment
    risk: RiskScore
    days: DaysToSell
    strategies: dict[str, Strategy]
    max_buy: MaxBuyResult
    velocity: CapitalVelocity
    flip_score: FlipScore
    decision: Decision
    acquisition: float
    ai: Any | None = None
    ai_notes: list[str] = field(default_factory=list)
    deterministic_verdict: str = PASS
    # Phase 5.1: the seller economics these figures were produced with.
    seller_mode: str = ""
    # Phase 5.2: active-market benchmark, cross-check outcome and research links.
    benchmark: ActiveBenchmark | None = None
    cross_check: CrossCheckDecision | None = None
    links: MarketLinks | None = None
    manual_benchmark: Any | None = None
    # Phase 5.3: the actual collected records behind this valuation.
    evidence_aggregate: Any | None = None
    # Final 6.0: which comparables were used or excluded, and why (spec s.16).
    comparable_audit: Any | None = None

    @property
    def verdict(self) -> str:
        """Final status, including the Phase 5.2 CROSS-CHECK tier.

        CROSS-CHECK is only ever promoted from PASS, never from BUY or WATCH: it
        marks a listing the active market says is cheap but which has no
        confirmed evidence behind it. It is a prompt to investigate, not a
        recommendation to buy.
        """
        if (self.decision.verdict == PASS and self.cross_check is not None
                and self.cross_check.should_cross_check):
            return CROSS_CHECK
        return self.decision.verdict

    @property
    def discount_pct(self) -> float | None:
        """Discount to the active eBay median, in percent."""
        return self.cross_check.discount_pct if self.cross_check else None

    @property
    def discount_to_watchcharts_benchmark_pct(self) -> float | None:
        """Discount to a manually verified LEVEL B market benchmark.

        Context only. This never feeds the valuation, the confidence score or
        the BUY gate — a benchmark someone read off a chart is not a sale.
        """
        return self.cross_check.benchmark_discount_pct if self.cross_check else None

    @property
    def benchmark_value(self) -> float | None:
        return self.cross_check.benchmark_value if self.cross_check else None

    @property
    def benchmarks_agree(self) -> bool | None:
        return self.cross_check.benchmarks_agree if self.cross_check else None

    @property
    def listing_url(self) -> str | None:
        return self.links.listing if self.links else self.listing.get("url")

    @property
    def base(self) -> Strategy | None:
        return self.strategies.get(BASE)

    @property
    def net_profit(self) -> float | None:
        return self.base.net_profit if self.base else None

    @property
    def net_roi(self) -> float | None:
        return self.base.net_roi_pct if self.base else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "brand": self.listing.get("brand"),
            "model": self.listing.get("model"),
            "reference": self.ref.reference,
            "listing_price": self.listing.get("price"),
            "acquisition": self.acquisition,
            "evidence": self.evidence.as_dict(),
            "confidence": self.confidence.as_dict(),
            "liquidity": self.liquidity.as_dict(),
            "condition": self.condition.as_dict(),
            "risk": self.risk.as_dict(),
            "days_to_sell": self.days.as_dict(),
            "strategies": {k: v.as_dict() for k, v in self.strategies.items()},
            "max_buy": self.max_buy.as_dict(),
            "capital_velocity": self.velocity.as_dict(),
            "flip_score": self.flip_score.as_dict(),
            "decision": self.decision.as_dict(),
            "verdict": self.verdict,
            "seller_mode": self.seller_mode,
            "evidence_source": self.evidence.source_label,
            "active_benchmark": self.benchmark.as_dict() if self.benchmark else None,
            "cross_check": self.cross_check.as_dict() if self.cross_check else None,
            "discount_to_active_median_pct": self.discount_pct,
            "discount_to_watchcharts_benchmark_pct":
                self.discount_to_watchcharts_benchmark_pct,
            "links": self.links.as_dict() if self.links else None,
            "url": self.listing_url,
        }


def analyse_flip(listing: dict[str, Any], ref: WatchRef, sold_evidence,
                 active_listing_count: int = 0,
                 asking_prices: list[float] | None = None,
                 config: Phase5Config = CONFIG,
                 benchmark: ActiveBenchmark | None = None,
                 cross_check_config: CrossCheckConfig = CROSS_CHECK_CONFIG,
                 manual_benchmark: Any | None = None,
                 collected_evidence: Any | None = None,
                 evidence_aggregate: Any | None = None,
                 comparable_audit: Any | None = None) -> FlipAnalysis:
    """Run the full Phase 5 pipeline for one listing."""
    reference = ref.reference

    evidence = from_sold_evidence(
        sold_evidence, reference,
        active_listing_count=active_listing_count,
        asking_prices=asking_prices,
        seed_mid=ref.market_mid,
    )
    # Phase 5.3: automatically collected evidence supersedes the legacy provider
    # chain only when genuinely stronger — real sold data beating seed or
    # asking-only input. It never downgrades existing evidence.
    if collected_evidence is not None and collected_evidence.has_sold_evidence:
        if (not evidence.has_sold_evidence
                or collected_evidence.sold_price_sample_size
                > evidence.sold_price_sample_size):
            evidence = collected_evidence

    confidence = score_confidence(evidence)
    liquidity = assess_liquidity(evidence, ref.brand)
    condition = assess_condition(listing)

    _, market_mid, _ = evidence.valuation_band()
    risk = score_risk(listing, condition, market_mid, confidence.score, liquidity.score)

    days = estimate_days_to_sell(evidence, liquidity, condition)
    listed = listing.get("total_acquisition") or listing.get("price") or 0.0
    acquisition = effective_acquisition(listed, config)

    strategies = build_strategies(evidence, condition, days, acquisition, config)
    max_buy = calculate_max_buy(evidence, condition, risk, confidence, liquidity, config)
    velocity = capital_velocity(strategies.get(BASE), config)
    flip_score = calculate_flip_score(strategies.get(BASE), liquidity, velocity,
                                      confidence, condition, risk, evidence,
                                      acquisition, config)
    decision = decide(evidence, confidence, liquidity, condition, risk, max_buy,
                      strategies, velocity, flip_score, acquisition, config)

    # --- Phase 5.2: active-market comparison and research links -----------
    links = build_links(listing, ref.brand, ref.reference, ref.model)
    cross = evaluate_cross_check(acquisition, benchmark, evidence, condition, risk,
                                 cross_check_config,
                                 manual_benchmark=manual_benchmark)

    return FlipAnalysis(
        item_id=listing["item_id"], listing=listing, ref=ref, evidence=evidence,
        confidence=confidence, liquidity=liquidity, condition=condition, risk=risk,
        days=days, strategies=strategies, max_buy=max_buy, velocity=velocity,
        flip_score=flip_score, decision=decision, acquisition=acquisition,
        deterministic_verdict=decision.verdict,
        seller_mode=config.engine.mode_label,
        benchmark=benchmark, cross_check=cross, links=links,
        manual_benchmark=manual_benchmark,
        evidence_aggregate=evidence_aggregate,
        comparable_audit=comparable_audit,
    )


# --- AI triage (spec s.20) --------------------------------------------------

def needs_ai(analysis: FlipAnalysis, config: Phase5Config = CONFIG) -> tuple[bool, str]:
    """Whether this candidate is worth an AI call.

    Deterministic filtering runs first. AI is reserved for cases where free text
    genuinely carries decision-relevant information the code cannot read.
    """
    d = config.decision

    if analysis.max_buy.standard is None:
        return False, "No valuation — nothing for the AI to add."
    if analysis.base is None or analysis.base.net_profit <= 0:
        return False, "Economics fail outright; no text can rescue this."

    if analysis.decision.verdict == BUY:
        return True, "BUY candidate — verify before committing capital."
    if analysis.condition.reference_unclear:
        return True, "Reference identification needs text interpretation."
    if analysis.condition.authenticity_doubt:
        return True, "Listing language raises authenticity questions."
    if analysis.condition.completeness == "UNKNOWN":
        return True, "Completeness is ambiguous and materially affects value."
    if abs(analysis.flip_score.score - d.buy_min_flip_score) <= 10:
        return True, "Flip Score sits near the BUY threshold."
    if analysis.decision.target_offer is not None:
        return True, "A negotiated purchase could make this work."
    if (analysis.base.net_profit >= d.min_net_profit_gbp * 2
            and analysis.confidence.score >= d.min_confidence):
        return True, "Unusually attractive economics — worth a second opinion."

    return False, "Deterministic analysis is conclusive; no AI call needed."


# --- ranking (spec s.14) ----------------------------------------------------

# Phase 5.2.1 ordering: actionable BUYs first, then CROSS-CHECK candidates worth
# investigating now, then WATCH (blocked, usually on price), then PASS.
#
# CROSS-CHECK outranks WATCH deliberately. A WATCH is already understood and is
# waiting on a price move; a CROSS-CHECK is an unexplained discount that will be
# gone if nobody looks at it today. The time-sensitive item goes first.
VERDICT_RANK_52 = {BUY: 0, CROSS_CHECK: 1, WATCH: 2, PASS: 3}


def _rank_key(a: FlipAnalysis) -> int:
    return VERDICT_RANK_52.get(a.verdict, 3)


SORT_MODES: dict[str, Callable[[FlipAnalysis], tuple]] = {
    "Best Flip Opportunities": lambda a: (
        _rank_key(a), -a.flip_score.score, -a.confidence.score,
        -a.velocity.score, -(a.net_profit or 0), -a.liquidity.score),
    "Highest NET Profit": lambda a: (-(a.net_profit or 0),),
    "Highest ROI": lambda a: (-(a.net_roi or 0),),
    "Fastest Sale": lambda a: ((a.base.mid_days if a.base and a.base.mid_days
                                else float("inf")),),
    "Highest Liquidity": lambda a: (-a.liquidity.score,),
    "Highest Confidence": lambda a: (-a.confidence.score,),
    "Largest Discount": lambda a: (-a.flip_score.components.get("discount", 0),),
    "Lowest Risk": lambda a: (a.risk.total,),
    "Largest Discount to Active Market": lambda a: (-(a.discount_pct or -999),),
}


def top_opportunities(analyses: list[FlipAnalysis],
                      limit: int = 30) -> list[FlipAnalysis]:
    """Ranked, PASS excluded — the list to act on.

    Ordered BUY, then CROSS-CHECK, then WATCH; within a tier by Flip Score,
    market confidence, capital velocity and finally net profit.
    """
    actionable = [a for a in analyses if a.verdict != PASS]
    ordered = sorted(actionable, key=lambda a: (
        VERDICT_RANK_52.get(a.verdict, 3),
        -a.flip_score.score,
        -a.confidence.score,
        -a.velocity.score,
        -(a.net_profit or 0),
    ))
    return ordered[:limit]


def rank(analyses: list[FlipAnalysis],
         mode: str = "Best Flip Opportunities") -> list[FlipAnalysis]:
    """Rank opportunities. The default is deliberately not 'biggest discount'."""
    key = SORT_MODES.get(mode, SORT_MODES["Best Flip Opportunities"])
    return sorted(analyses, key=key)


# --- capital allocation (spec s.17) -----------------------------------------

@dataclass
class AllocationPlan:
    budget: float
    selected: list[FlipAnalysis]
    total_capital: float
    total_expected_profit: float
    remaining: float
    note: str

    @property
    def blended_roi_pct(self) -> float | None:
        if self.total_capital <= 0:
            return None
        return round(self.total_expected_profit / self.total_capital * 100, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "budget": self.budget,
            "total_capital": self.total_capital,
            "total_expected_profit": self.total_expected_profit,
            "blended_roi_pct": self.blended_roi_pct,
            "remaining": self.remaining,
            "count": len(self.selected),
            "note": self.note,
        }


def allocate_capital(analyses: list[FlipAnalysis], budget: float,
                     include_watch: bool = False) -> AllocationPlan:
    """Suggest which opportunities best use a given budget.

    Greedy on profit per 30 days of capital, which favours money that comes back
    quickly over a single large slow position. This is a suggestion for you to
    consider — the tool never buys anything.
    """
    eligible = [a for a in analyses
                if a.verdict == BUY or (include_watch and a.verdict == WATCH)]
    eligible = [a for a in eligible if a.base and a.base.profit_per_30d]
    eligible.sort(key=lambda a: -(a.base.profit_per_30d or 0))

    selected: list[FlipAnalysis] = []
    spent = 0.0
    profit = 0.0
    for a in eligible:
        if spent + a.acquisition > budget:
            continue
        selected.append(a)
        spent += a.acquisition
        profit += a.net_profit or 0.0

    if not selected:
        note = ("No opportunities fit this budget. Either nothing currently "
                "qualifies, or the qualifying watches cost more than the budget.")
    else:
        note = (f"{len(selected)} opportunit(y/ies) selected by profit per 30 days of "
                "capital, which favours faster turnover over a single large slow "
                "position. This is a suggestion, not an instruction to buy.")

    return AllocationPlan(
        budget=round(budget, 2),
        selected=selected,
        total_capital=round(spent, 2),
        total_expected_profit=round(profit, 2),
        remaining=round(budget - spent, 2),
        note=note,
    )


# --- dashboard card (spec s.15) ---------------------------------------------

def dashboard_card(a: FlipAnalysis) -> str:
    """Plain-text card, used in the CLI sample output and as UI source of truth."""
    lines: list[str] = []
    lines.append(f"{(a.listing.get('brand') or '').upper()} "
                 f"{a.listing.get('model') or ''}".strip())
    lines.append(f"Ref: {a.ref.reference}")
    lines.append("")
    lines.append(f"Listing: £{a.listing.get('price', 0):,.0f}")
    lines.append(f"Seller economics: {a.seller_mode}")
    lines.append("")

    low, mid, high = a.evidence.valuation_band()
    if mid is None:
        lines.append("Market: Insufficient Market Evidence")
    else:
        marker = "  [UNVERIFIED]" if a.evidence.unverified else ""
        lines.append(f"Market: £{low:,.0f}–£{high:,.0f}{marker}")
    lines.append("")

    lines.append(f"Evidence: {a.evidence.source_label}")
    if a.evidence.has_sold_evidence:
        lines.append(f"UK sold: {a.evidence.sold_count_90d} / 90 days")
    elif a.evidence.has_observed_activity:
        lines.append(f"Observed activity: {a.evidence.sold_count_90d} likely sold "
                     "(unconfirmed)")
    else:
        lines.append("UK sold: no observed sales")
    lines.append(f"Liquidity: {a.liquidity.band} ({a.liquidity.score:.0f}/100)")
    lines.append("")

    if a.strategies:
        lines.append("Expected sale:")
        for name in (QUICK, BASE, PATIENT):
            s = a.strategies[name]
            lines.append(f"  {name.title():<8} £{s.sale_price:,.0f} | "
                         f"{DaysToSell.format(s.days)} | net £{s.net_profit:,.0f} "
                         f"({s.net_roi_pct:.0f}% ROI)" if s.net_roi_pct is not None
                         else f"  {name.title():<8} £{s.sale_price:,.0f}")
        lines.append("")

    base = a.strategies.get(BASE)
    if base:
        lines.append("Estimated costs (Base sale):")
        for label, value in base.costs.lines():
            lines.append(f"  {label:<32} £{value:,.2f}")
        lines.append(f"  {'TOTAL EXIT COSTS':<32} £{base.costs.total:,.2f}")
        lines.append(f"  {'NET PROFIT':<32} £{base.net_profit:,.2f}")
        lines.append("")

    mb = a.max_buy
    lines.append("MAX BUY:")
    lines.append(f"  Conservative £{mb.conservative:,.0f}" if mb.conservative
                 else "  Conservative n/a")
    lines.append(f"  Standard     £{mb.standard:,.0f}" if mb.standard
                 else "  Standard     n/a")
    lines.append(f"  Aggressive   £{mb.aggressive:,.0f}" if mb.aggressive
                 else "  Aggressive   not permitted (confidence/liquidity gate)")
    lines.append("")

    lines.append(f"Capital Velocity: {a.velocity.score:.0f}/100")
    lines.append(f"Market Confidence: {a.confidence.score:.0f}/100 ({a.confidence.band})")
    lines.append(f"Risk: {a.risk.total:.0f}/100 ({a.risk.band})")
    lines.append(f"FLIP SCORE: {a.flip_score.score:.0f}/100")
    lines.append("")
    lines.append(f"{a.decision.icon} {a.decision.display_verdict}")
    if a.decision.target_offer:
        o = a.decision.target_offer
        lines.append(f"Suggested offer: £{o.low:,.0f}–£{o.high:,.0f}")
    lines.append("")
    lines.append(f"WHY {a.verdict}")
    lines.append(a.decision.why)
    return "\n".join(lines)
