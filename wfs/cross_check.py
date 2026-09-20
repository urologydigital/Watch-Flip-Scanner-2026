"""CROSS-CHECK status and trigger logic (spec 5.2 §6, §8, §11).

The problem this solves: without sold evidence almost everything becomes PASS,
so a genuinely underpriced watch looks identical to a boring one. That makes the
scanner useless in exactly the situation the user is in today.

The solution is a third status between WATCH and PASS:

    CROSS-CHECK = "the active market says this is unusually cheap, but I cannot
                   confirm it. Go and look yourself — here are the links."

CROSS-CHECK is emphatically **not** a BUY. It carries no confirmed valuation and
never claims one. It is a shortlist for human attention, and the cross-check
trigger is deliberately strict so that 189 analysed listings do not become 189
things to check by hand.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .active_market import ActiveBenchmark
from .condition import ConditionAssessment
from .evidence import MarketEvidence
from .risk import RiskScore

CROSS_CHECK = "CROSS-CHECK"


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class CrossCheckConfig:
    """Every threshold that decides whether a listing is worth your attention."""

    # Minimum discount to the active eBay median before a listing is interesting.
    min_discount_pct: float = field(
        default_factory=lambda: _f("WFS52_MIN_DISCOUNT_PCT", 12.0))
    # Active benchmark needs this many comparable listings to be trusted at all.
    min_benchmark_sample: int = field(
        default_factory=lambda: _i("WFS52_MIN_BENCHMARK_SAMPLE", 3))
    # Above this risk score a cheap watch is a warning, not an opportunity.
    max_risk_score: float = field(
        default_factory=lambda: _f("WFS52_MAX_RISK_SCORE", 55.0))
    # Below this condition score the discount is probably explained by the watch.
    min_condition_score: float = field(
        default_factory=lambda: _f("WFS52_MIN_CONDITION_SCORE", 35.0))
    # Hard cap on how many candidates a single scan may flag for manual work.
    max_candidates_per_scan: int = field(
        default_factory=lambda: _i("WFS52_MAX_CROSS_CHECKS", 25))
    # A discount this extreme is a red flag, not a bargain.
    implausible_discount_pct: float = field(
        default_factory=lambda: _f("WFS52_IMPLAUSIBLE_DISCOUNT_PCT", 60.0))
    # Phase 5.2.1: how far the active eBay median and a manual WatchCharts
    # benchmark may diverge before the disagreement is worth surfacing.
    benchmark_disagreement_pct: float = field(
        default_factory=lambda: _f("WFS521_BENCHMARK_DISAGREEMENT_PCT", 20.0))
    # Ranking bonus when an independent benchmark corroborates the discount.
    benchmark_agreement_bonus: float = field(
        default_factory=lambda: _f("WFS521_BENCHMARK_BONUS", 10.0))


CROSS_CHECK_CONFIG = CrossCheckConfig()


@dataclass
class CrossCheckDecision:
    """Whether a listing earns manual investigation, and why."""

    should_cross_check: bool
    score: float                      # 0-100, for ranking the shortlist
    discount_pct: float | None        # versus the active eBay median
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    summary: str = ""
    # --- Phase 5.2.1: manual WatchCharts benchmark context -----------------
    # LEVEL B evidence. Informs the cross-check shortlist and its ranking.
    # It is NEVER blended into a valuation and can NEVER produce a BUY.
    benchmark_value: float | None = None
    benchmark_source: str | None = None
    benchmark_discount_pct: float | None = None
    benchmarks_agree: bool | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "should_cross_check": self.should_cross_check,
            "score": self.score,
            "discount_pct": self.discount_pct,
            "reasons": self.reasons,
            "blockers": self.blockers,
            "summary": self.summary,
            "benchmark_value": self.benchmark_value,
            "benchmark_source": self.benchmark_source,
            "benchmark_discount_pct": self.benchmark_discount_pct,
            "benchmarks_agree": self.benchmarks_agree,
            "warnings": self.warnings,
        }


def discount_to_benchmark_pct(price: float | None,
                              benchmark_value: float | None) -> float | None:
    """Positive means the asking price is below the manual benchmark."""
    if not price or price <= 0 or not benchmark_value or benchmark_value <= 0:
        return None
    return round((benchmark_value - price) / benchmark_value * 100, 1)


def _apply_manual_benchmark(decision: CrossCheckDecision, price: float,
                            active: ActiveBenchmark, manual: Any,
                            config: CrossCheckConfig) -> None:
    """Fold a LEVEL B benchmark into the cross-check CONTEXT only.

    This adjusts how the shortlist is ranked and what the user is told. It never
    touches the valuation, the confidence score or the BUY gate — a manually
    read benchmark is not a transaction record.
    """
    value = getattr(manual, "value", None)
    if not value or value <= 0:
        return
    # Defensive: only a LEVEL B market benchmark belongs here. Anything claiming
    # to be sold evidence must go through the proper sold-evidence path.
    kind = getattr(manual, "evidence_kind", None)
    if kind is not None and kind == "CONFIRMED_SOLD":
        return

    decision.benchmark_value = round(float(value), 2)
    decision.benchmark_source = getattr(manual, "source_label", None) or \
        getattr(manual, "source", None)
    decision.benchmark_discount_pct = discount_to_benchmark_pct(price, value)

    active_median = active.active_median_price
    if not active_median:
        return

    divergence = abs(active_median - value) / value * 100
    decision.benchmarks_agree = divergence <= config.benchmark_disagreement_pct

    if decision.benchmarks_agree:
        decision.reasons.append(
            f"{decision.benchmark_source} of £{value:,.0f} corroborates the active "
            f"eBay median of £{active_median:,.0f} "
            f"({divergence:.0f}% apart). Discount to benchmark: "
            f"{decision.benchmark_discount_pct:.1f}%.")
        if decision.should_cross_check:
            decision.score = round(
                min(100.0, decision.score + config.benchmark_agreement_bonus), 1)
    else:
        decision.warnings.append(
            f"Market benchmarks disagree — verify manually. Active eBay median "
            f"£{active_median:,.0f} versus {decision.benchmark_source} "
            f"£{value:,.0f} ({divergence:.0f}% apart).")
        if decision.should_cross_check:
            decision.score = round(
                max(0.0, decision.score - config.benchmark_agreement_bonus), 1)

    # A benchmark showing the watch is NOT cheap is useful negative evidence.
    if (decision.benchmark_discount_pct is not None
            and decision.benchmark_discount_pct < 0):
        decision.warnings.append(
            f"Asking price is {abs(decision.benchmark_discount_pct):.1f}% ABOVE the "
            f"{decision.benchmark_source}. The eBay discount may reflect optimistic "
            "asking prices rather than a genuine bargain.")
        if decision.should_cross_check:
            decision.score = round(max(0.0, decision.score - 15.0), 1)


def evaluate_cross_check(price: float | None, benchmark: ActiveBenchmark | None,
                         evidence: MarketEvidence, condition: ConditionAssessment,
                         risk: RiskScore,
                         config: CrossCheckConfig = CROSS_CHECK_CONFIG,
                         manual_benchmark: Any | None = None
                         ) -> CrossCheckDecision:
    """Decide whether this listing is worth a human going to look.

    Deliberately conservative: everything must line up. A cheap watch with a bad
    seller, poor condition or an implausible price is not a cross-check
    candidate, it is a warning.
    """
    reasons: list[str] = []
    blockers: list[str] = []

    if price is None or price <= 0:
        return CrossCheckDecision(False, 0.0, None, [], ["No usable price."],
                                  "No price to evaluate.")

    if benchmark is None or not benchmark.is_usable:
        count = benchmark.active_listing_count if benchmark else 0
        return CrossCheckDecision(
            False, 0.0, None, [],
            [f"Only {count} comparable active listing(s) — no benchmark."],
            "Not enough live comparables to judge whether this price is unusual.")

    discount = benchmark.discount_to_median_pct(price)
    if discount is None:
        return CrossCheckDecision(False, 0.0, None, [], ["Benchmark unusable."],
                                  "No active benchmark available.")

    # --- gates ------------------------------------------------------------
    if discount < config.min_discount_pct:
        blockers.append(
            f"Only {discount:.1f}% below the active median of "
            f"£{benchmark.active_median_price:,.0f} "
            f"(need {config.min_discount_pct:.0f}%).")
    else:
        reasons.append(
            f"{discount:.1f}% below the active eBay median of "
            f"£{benchmark.active_median_price:,.0f} across "
            f"{benchmark.active_listing_count} listing(s).")

    if discount >= config.implausible_discount_pct:
        blockers.append(
            f"A {discount:.0f}% discount is implausible for a genuine example — "
            "treat as a red flag rather than an opportunity.")

    if risk.total > config.max_risk_score:
        blockers.append(f"Risk score {risk.total:.0f}/100 is too high "
                        f"({risk.band.lower()}) to be worth investigating.")
    else:
        reasons.append(f"Risk {risk.total:.0f}/100 ({risk.band.lower()}).")

    if condition.condition_score < config.min_condition_score:
        blockers.append(f"Condition score {condition.condition_score:.0f}/100 — "
                        "the low price is probably explained by the watch itself.")
    elif condition.completeness in ("FULL_SET", "BOX_AND_PAPERS"):
        reasons.append(f"{condition.completeness_label} supports resale value.")

    if condition.damage or condition.aftermarket_parts:
        blockers.append("Damage or non-original parts indicated.")

    # --- score for ranking the shortlist ----------------------------------
    # Discount dominates but saturates; condition and low risk break ties.
    discount_component = min(discount / 35.0, 1.0) * 60
    condition_component = condition.condition_score / 100 * 20
    risk_component = max(0.0, (100 - risk.total) / 100) * 12
    sample_component = min(benchmark.active_listing_count / 10.0, 1.0) * 8
    score = round(min(100.0, discount_component + condition_component
                      + risk_component + sample_component), 1)

    should = not blockers

    if should:
        summary = (f"Asking £{price:,.0f} against an active median of "
                   f"£{benchmark.active_median_price:,.0f} ({discount:.1f}% below). "
                   "No confirmed sold evidence, so this needs manual verification "
                   "before it can become a BUY.")
    else:
        summary = blockers[0]

    decision = CrossCheckDecision(should, score if should else 0.0, discount,
                                  reasons, blockers, summary)

    # Phase 5.2.1: a fresh manual benchmark refines the ranking and the message.
    if manual_benchmark is not None:
        _apply_manual_benchmark(decision, price, benchmark, manual_benchmark, config)
        if decision.should_cross_check and decision.benchmark_discount_pct is not None:
            decision.summary += (
                f" Manual benchmark {decision.benchmark_source} "
                f"£{decision.benchmark_value:,.0f} "
                f"({decision.benchmark_discount_pct:.1f}% below asking) — "
                "market benchmark, not confirmed sold evidence.")
        if decision.warnings:
            decision.summary += " " + decision.warnings[0]

    return decision


def shortlist(decisions: list[tuple[Any, CrossCheckDecision]],
              config: CrossCheckConfig = CROSS_CHECK_CONFIG) -> list[Any]:
    """Cap and rank the cross-check shortlist.

    Without the cap, a scan of 189 listings could hand back 189 things to check
    by hand, which is the same as handing back nothing.
    """
    eligible = [(obj, d) for obj, d in decisions if d.should_cross_check]
    eligible.sort(key=lambda pair: -pair[1].score)
    return [obj for obj, _ in eligible[:config.max_candidates_per_scan]]
