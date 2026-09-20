from __future__ import annotations

import pytest

from wfs import cache, db
from wfs.condition import (BOX_ONLY, FULL_SET, PAPERS_ONLY, WATCH_ONLY,
                           assess_condition)
from wfs.config5 import (CONFIG, EVIDENCE_ASKING_ONLY, EVIDENCE_MODEL_FAMILY,
                         EVIDENCE_REFERENCE_EXACT, EVIDENCE_SEED,
                         DecisionConfig, Phase5Config, confidence_band)
from wfs.decision import BUY, PASS, WATCH
from wfs.evidence import MarketEvidence, from_sold_evidence, score_confidence
from wfs.flip import (BASE, PATIENT, QUICK, assess_liquidity, build_strategies,
                      calculate_flip_score, calculate_max_buy, capital_velocity,
                      cost_breakdown, estimate_days_to_sell)
from wfs.flip_analysis import (allocate_capital, analyse_flip, dashboard_card,
                               needs_ai, rank)
from wfs.risk import score_risk
from wfs.sold_market import COVERAGE_PARTIAL, SoldEvidence
from wfs.watchlist import WatchRef, build_queries


# --- fixtures ---------------------------------------------------------------

def sold(count=8, prices=None, period=90):
    prices = prices or [1480, 1520, 1550, 1500, 1575, 1460, 1530, 1510][:count]
    return SoldEvidence("A17366", period, "test", COVERAGE_PARTIAL,
                        exact_sale_count=count, family_sale_count=count + 6,
                        prices=prices)


def no_sold():
    return SoldEvidence("A17366", 90, "none", "UNAVAILABLE")


@pytest.fixture
def ref():
    return WatchRef("BREITLING", "SuperOcean Automatic 42", "A17366", 1400, 1520, 1650)


def listing(**kw):
    base = {
        "item_id": "v1|1|0",
        "title": "Breitling Superocean A17366 2022 full set box and papers serviced",
        "price": 1000.0, "shipping": 0.0, "total_acquisition": 1000.0,
        "buying_format": "BUY_IT_NOW", "condition": "Pre-owned",
        "seller_feedback_pct": 99.8, "seller_feedback_score": 800,
        "item_location": "London, GB", "authenticity_guarantee": True,
        "returns_accepted": True, "image_url": "http://img",
        "brand": "BREITLING", "model": "SuperOcean Automatic 42",
    }
    base.update(kw)
    return base


def evidence_from(count=8, active=3, prices=None):
    return from_sold_evidence(sold(count, prices), "A17366", active_listing_count=active)


# --- market evidence and confidence -----------------------------------------

def test_evidence_computes_all_required_statistics():
    ev = evidence_from(8, active=4)
    d = ev.as_dict()
    for key in ("sold_count_30d", "sold_count_90d", "sold_count_180d",
                "median_sold_price", "mean_sold_price", "low_sold_price",
                "high_sold_price", "sold_price_sample_size",
                "current_active_listing_count", "sell_through_rate",
                "evidence_last_updated"):
        assert key in d
    assert ev.sold_count_90d == 8
    assert ev.median_sold_price == pytest.approx(1515.0)
    assert ev.sell_through_rate == pytest.approx(8 / 12, abs=0.01)


def test_evidence_level_hierarchy_is_explicit():
    assert evidence_from().evidence_level == EVIDENCE_REFERENCE_EXACT
    asking = from_sold_evidence(no_sold(), "A17366", asking_prices=[1600, 1700, 1800])
    assert asking.evidence_level == EVIDENCE_ASKING_ONLY
    seed = from_sold_evidence(no_sold(), "A17366", seed_mid=1520)
    assert seed.evidence_level == EVIDENCE_SEED
    assert seed.unverified is True


def test_seed_evidence_can_never_produce_high_confidence():
    seed = from_sold_evidence(no_sold(), "A17366", seed_mid=1520)
    score = score_confidence(seed)
    assert score.score <= 25
    assert score.band == "INSUFFICIENT"
    assert score.is_sufficient_for_buy is False


def test_asking_only_confidence_is_capped_weak():
    ev = from_sold_evidence(no_sold(), "A17366",
                            asking_prices=[1600, 1700, 1800, 1650])
    score = score_confidence(ev)
    assert score.score <= 45
    assert ev.has_sold_evidence is False


def test_strong_evidence_scores_well():
    score = score_confidence(evidence_from(12, active=4))
    assert score.score >= 75
    assert score.band in ("STRONG", "EXCELLENT")


def test_model_family_evidence_scores_below_exact():
    exact = evidence_from(10)
    family = evidence_from(10)
    family.evidence_level = EVIDENCE_MODEL_FAMILY
    assert score_confidence(family).score < score_confidence(exact).score


def test_inconsistent_prices_lower_confidence():
    tight = evidence_from(8, prices=[1500, 1510, 1505, 1495, 1508, 1502, 1512, 1498])
    wild = evidence_from(8, prices=[1100, 1900, 1300, 1750, 1250, 1850, 1400, 1600])
    assert score_confidence(tight).score > score_confidence(wild).score


def test_confidence_bands():
    assert confidence_band(95) == "EXCELLENT"
    assert confidence_band(80) == "STRONG"
    assert confidence_band(65) == "MODERATE"
    assert confidence_band(45) == "WEAK"
    assert confidence_band(20) == "INSUFFICIENT"


def test_insufficient_evidence_message():
    ev = from_sold_evidence(no_sold(), "A17366")
    assert "Insufficient Market Evidence" in ev.describe()
    assert ev.valuation_band() == (None, None, None)


# --- condition and full set -------------------------------------------------

def test_full_set_detected_and_rewarded():
    a = assess_condition(listing(title="Breitling A17366 full set box and papers"))
    assert a.completeness == FULL_SET
    assert a.value_multiplier == pytest.approx(1.0)
    assert a.condition_score > 50


def test_watch_only_is_discounted():
    a = assess_condition(listing(
        title="Breitling Superocean A17366 automatic diver watch in good order"))
    assert a.completeness == WATCH_ONLY
    assert a.value_multiplier < 1.0


def test_missing_papers_lowers_value_versus_full_set():
    full = assess_condition(listing(title="Breitling A17366 full set"))
    box = assess_condition(listing(title="Breitling A17366 with box only no papers"))
    assert box.completeness in (BOX_ONLY, WATCH_ONLY)
    assert box.value_multiplier < full.value_multiplier


def test_damage_and_aftermarket_flagged():
    a = assess_condition(listing(
        title="Breitling A17366 cracked crystal aftermarket bezel spares or repairs"))
    assert a.damage and a.aftermarket_parts
    assert a.needs_reserve
    assert a.value_multiplier < 0.9


def test_service_needed_triggers_reserve():
    a = assess_condition(listing(title="Breitling A17366 running slow needs a service"))
    assert a.service_needed and a.needs_reserve


def test_used_is_not_assumed_good():
    a = assess_condition(listing(title="Breitling watch", condition="Used"))
    assert a.condition_score <= 50
    assert a.value_multiplier < 1.0


# --- cost and net profit ----------------------------------------------------

def test_cost_breakdown_includes_every_component():
    """Phase 5.1: costs are itemised, and a UK private seller pays no platform fee."""
    cond = assess_condition(listing())
    costs = cost_breakdown(1500.0, cond)
    assert costs.shipping > 0 and costs.insurance > 0 and costs.packaging > 0
    assert costs.transaction_fee == 0.0          # UK private seller default
    assert costs.payment_processing_fee == 0.0
    assert costs.net_proceeds < 1500.0
    assert costs.total == pytest.approx(1500.0 - costs.net_proceeds, abs=0.01)


def test_net_profit_is_not_gross_spread():
    ev = evidence_from()
    cond = assess_condition(listing())
    liq = assess_liquidity(ev, "BREITLING")
    days = estimate_days_to_sell(ev, liq, cond)
    s = build_strategies(ev, cond, days, 1000.0)[BASE]
    assert s.gross_spread > s.net_profit
    assert s.net_profit == pytest.approx(s.costs.net_proceeds - 1000.0, abs=0.01)


def test_service_reserve_only_when_needed():
    ok = assess_condition(listing(title="Breitling A17366 full set serviced"))
    bad = assess_condition(listing(title="Breitling A17366 needs a service"))
    assert cost_breakdown(1500.0, ok).service_reserve == 0
    assert cost_breakdown(1500.0, bad).service_reserve > 0


def test_fee_assumptions_are_configurable():
    cond = assess_condition(listing())
    cheap = Phase5Config().with_profile(
        "CUSTOM", shipping_cost_gbp=0.0, insurance_cost_gbp=0.0,
        packaging_cost_gbp=0.0)
    expensive = Phase5Config().with_profile("CUSTOM", transaction_fee_pct=0.10)
    assert (cost_breakdown(1500.0, cond, cheap).net_proceeds
            > cost_breakdown(1500.0, cond, expensive).net_proceeds)


# --- three strategies -------------------------------------------------------

def test_three_strategies_ordered_and_evidence_derived():
    ev = evidence_from()
    cond = assess_condition(listing())
    liq = assess_liquidity(ev, "BREITLING")
    days = estimate_days_to_sell(ev, liq, cond)
    s = build_strategies(ev, cond, days, 1000.0)
    assert s[QUICK].sale_price <= s[BASE].sale_price <= s[PATIENT].sale_price
    assert s[QUICK].net_profit < s[PATIENT].net_profit
    # Prices come from the observed sold distribution, not a fixed percentage.
    low, mid, high = ev.valuation_band()
    assert s[BASE].sale_price == pytest.approx(mid * cond.value_multiplier, abs=1)


def test_strategies_empty_without_evidence():
    ev = from_sold_evidence(no_sold(), "A17366")
    cond = assess_condition(listing())
    liq = assess_liquidity(ev, "BREITLING")
    days = estimate_days_to_sell(ev, liq, cond)
    assert build_strategies(ev, cond, days, 1000.0) == {}


def test_quick_sells_faster_than_patient():
    ev = evidence_from()
    cond = assess_condition(listing())
    liq = assess_liquidity(ev, "BREITLING")
    days = estimate_days_to_sell(ev, liq, cond)
    assert days.quick[1] < days.patient[1]


def test_days_to_sell_unavailable_without_evidence():
    ev = from_sold_evidence(no_sold(), "A17366")
    liq = assess_liquidity(ev, "BREITLING")
    days = estimate_days_to_sell(ev, liq, assess_condition(listing()))
    assert days.base is None
    assert "Insufficient" in days.basis


def test_more_competition_slows_sale():
    cond = assess_condition(listing())
    few = evidence_from(8, active=1)
    many = evidence_from(8, active=20)
    d_few = estimate_days_to_sell(few, assess_liquidity(few, "BREITLING"), cond)
    d_many = estimate_days_to_sell(many, assess_liquidity(many, "BREITLING"), cond)
    assert d_many.base[1] > d_few.base[1]


# --- MAX BUY ----------------------------------------------------------------

def _bundle(count=8, active=3, l=None, brand="BREITLING"):
    l = l or listing()
    ev = evidence_from(count, active)
    conf = score_confidence(ev)
    liq = assess_liquidity(ev, brand)
    cond = assess_condition(l)
    _, mid, _ = ev.valuation_band()
    risk = score_risk(l, cond, mid, conf.score, liq.score)
    return ev, conf, liq, cond, risk


def test_max_buy_three_stances_ordered():
    ev, conf, liq, cond, risk = _bundle(14, active=1)
    mb = calculate_max_buy(ev, cond, risk, conf, liq)
    assert mb.conservative < mb.standard
    if mb.aggressive:
        assert mb.standard < mb.aggressive


def test_aggressive_gated_on_confidence_and_liquidity():
    ev, conf, liq, cond, risk = _bundle(2, active=25)
    mb = calculate_max_buy(ev, cond, risk, conf, liq)
    assert mb.aggressive_permitted is False
    assert mb.aggressive is None


def test_max_buy_none_without_evidence():
    ev = from_sold_evidence(no_sold(), "A17366")
    conf = score_confidence(ev)
    liq = assess_liquidity(ev, "BREITLING")
    cond = assess_condition(listing())
    risk = score_risk(listing(), cond, None, conf.score, liq.score)
    mb = calculate_max_buy(ev, cond, risk, conf, liq)
    assert mb.standard is None
    assert "Insufficient Market Evidence" in mb.explanation


def test_max_buy_does_not_rise_with_auction_bid():
    """Spec s.8: MAX BUY must not move upward as an auction becomes competitive."""
    low_bid = listing(total_acquisition=600.0, buying_format="AUCTION", price=600.0)
    high_bid = listing(total_acquisition=1400.0, buying_format="AUCTION", price=1400.0)
    results = []
    for l in (low_bid, high_bid):
        ev, conf, liq, cond, risk = _bundle(l=l)
        results.append(calculate_max_buy(ev, cond, risk, conf, liq).standard)
    assert results[0] == results[1]


def test_worse_condition_lowers_max_buy():
    good = _bundle(l=listing(title="Breitling A17366 full set mint"))
    poor = _bundle(l=listing(title="Breitling A17366 polished aftermarket bezel"))
    mb_good = calculate_max_buy(good[0], good[3], good[4], good[1], good[2])
    mb_poor = calculate_max_buy(poor[0], poor[3], poor[4], poor[1], poor[2])
    assert mb_poor.standard < mb_good.standard


# --- risk -------------------------------------------------------------------

def test_risk_score_bounds_and_bands():
    ev, conf, liq, cond, risk = _bundle()
    assert 0 <= risk.total <= 100
    assert risk.band in ("LOW", "MODERATE", "ELEVATED", "HIGH")


def test_bad_seller_raises_risk():
    good = _bundle()[4]
    bad = _bundle(l=listing(seller_feedback_pct=91.0, seller_feedback_score=4))[4]
    assert bad.total > good.total


def test_very_low_price_increases_scrutiny_not_opportunity():
    normal = _bundle(l=listing(total_acquisition=1000.0, price=1000.0))
    cheap = _bundle(l=listing(total_acquisition=600.0, price=600.0))
    assert cheap[4].scrutiny > normal[4].scrutiny
    # Structural risk unchanged, so MAX BUY is untouched by the low price.
    assert cheap[4].structural == normal[4].structural


def test_missing_image_and_short_title_raise_risk():
    thin = _bundle(l=listing(title="Breitling", image_url=None))[4]
    assert thin.structural > _bundle()[4].structural


def test_weak_evidence_raises_risk():
    ev = from_sold_evidence(no_sold(), "A17366", seed_mid=1520)
    cond = assess_condition(listing())
    risk = score_risk(listing(), cond, 1520, 10.0, 0.0)
    assert risk.total > _bundle()[4].total


# --- capital velocity -------------------------------------------------------

def _strategies(count=8, active=3, acquisition=1000.0, l=None, brand="BREITLING"):
    ev, conf, liq, cond, risk = _bundle(count, active, l, brand)
    days = estimate_days_to_sell(ev, liq, cond)
    return build_strategies(ev, cond, days, acquisition), ev, conf, liq, cond, risk


def test_capital_velocity_prefers_fast_turnover():
    fast, *_ = _strategies(count=16, active=1, acquisition=1000.0)
    slow, *_ = _strategies(count=2, active=20, acquisition=1000.0)
    v_fast = capital_velocity(fast[BASE])
    v_slow = capital_velocity(slow[BASE])
    assert v_fast.score > v_slow.score
    assert v_fast.expected_holding_days < v_slow.expected_holding_days


def test_velocity_reports_profit_per_30d_and_turns():
    s, *_ = _strategies(count=14, active=1)
    v = capital_velocity(s[BASE])
    assert v.profit_per_30d is not None
    assert v.annual_turns is not None
    assert v.capital_locked == 1000.0


def test_velocity_unknown_without_holding_period():
    assert capital_velocity(None).score == 0.0


def test_annualised_return_is_reported_but_caveated():
    s, *_ = _strategies(count=14, active=1)
    v = capital_velocity(s[BASE])
    assert "optimistic" in v.note


# --- flip score -------------------------------------------------------------

def _flip(count=8, active=3, acquisition=1000.0, l=None, brand="BREITLING"):
    strategies, ev, conf, liq, cond, risk = _strategies(count, active, acquisition,
                                                        l, brand)
    vel = capital_velocity(strategies.get(BASE))
    return calculate_flip_score(strategies.get(BASE), liq, vel, conf, cond, risk,
                                ev, acquisition)


def test_flip_score_bounded_and_has_components():
    fs = _flip()
    assert 0 <= fs.score <= 100
    assert set(fs.components) == {"profit_quality", "liquidity", "capital_velocity",
                                  "market_confidence", "discount", "condition", "risk"}


def test_weights_sum_to_one():
    assert CONFIG.weights.total == pytest.approx(1.0)


def test_profit_alone_cannot_dominate_flip_score():
    """Huge margin with terrible liquidity must not produce a top score."""
    fat_but_illiquid = _flip(count=1, active=30, acquisition=400.0)
    assert fat_but_illiquid.score < 65


def test_insufficient_evidence_caps_flip_score():
    ev = from_sold_evidence(no_sold(), "A17366", seed_mid=1520)
    conf = score_confidence(ev)
    liq = assess_liquidity(ev, "BREITLING")
    cond = assess_condition(listing())
    risk = score_risk(listing(), cond, None, conf.score, liq.score)
    fs = calculate_flip_score(None, liq, capital_velocity(None), conf, cond, risk,
                              ev, 1000.0)
    assert fs.score <= 35


def test_implausible_discount_does_not_score_unboundedly():
    modest = _flip(acquisition=1150.0).components["discount"]
    huge = _flip(acquisition=200.0).components["discount"]
    assert huge == 100.0 and modest < 100.0
    # Both cap at 100 — an absurd discount earns no extra credit.
    assert _flip(acquisition=50.0).components["discount"] == 100.0


# --- KEY REGRESSION (spec s.22) ---------------------------------------------

def test_large_discount_poor_liquidity_must_not_outrank_liquid_watch():
    """The single most important Phase 5 guarantee.

    A watch with a huge apparent discount but almost no observed sales must not
    outrank a smaller-margin, highly liquid, well-evidenced watch.
    """
    illiquid = analyse_flip(
        listing(item_id="illiquid", price=700.0, total_acquisition=700.0),
        WatchRef("RADO", "Captain Cook", "R32505203", 900, 1100, 1250),
        sold(count=1, prices=[1150]), active_listing_count=25)

    liquid = analyse_flip(
        listing(item_id="liquid", price=1150.0, total_acquisition=1150.0),
        WatchRef("TUDOR", "Black Bay 58", "79030N", 1400, 1520, 1650),
        sold(count=16, prices=[1500, 1520, 1510, 1530, 1495, 1515, 1525, 1505,
                               1512, 1508, 1518, 1522, 1498, 1502, 1516, 1509]),
        active_listing_count=2)

    ranked = rank([illiquid, liquid])
    assert ranked[0].item_id == "liquid", (
        f"illiquid={illiquid.flip_score.score} liquid={liquid.flip_score.score}")
    assert liquid.flip_score.score > illiquid.flip_score.score
    assert liquid.liquidity.score > illiquid.liquidity.score


def test_lower_margin_faster_watch_beats_higher_margin_slow_watch():
    slow = analyse_flip(
        listing(item_id="slow", price=900.0, total_acquisition=900.0),
        WatchRef("RADO", "Captain Cook", "R32505203", 1400, 1520, 1650),
        sold(count=2, prices=[1500, 1520]), active_listing_count=18)
    fast = analyse_flip(
        listing(item_id="fast", price=1150.0, total_acquisition=1150.0),
        WatchRef("TUDOR", "Black Bay 58", "79030N", 1400, 1520, 1650),
        sold(count=18, prices=[1500 + i for i in range(18)]), active_listing_count=1)
    assert fast.velocity.score > slow.velocity.score
    assert rank([slow, fast])[0].item_id == "fast"


# --- decision engine --------------------------------------------------------

def test_buy_requires_all_gates():
    a = analyse_flip(
        listing(price=1050.0, total_acquisition=1050.0),
        WatchRef("TUDOR", "Black Bay 58", "79030N", 1400, 1520, 1650),
        sold(count=18, prices=[1500 + i for i in range(18)]), active_listing_count=1)
    assert a.verdict == BUY
    assert a.decision.why.startswith("Listing is approximately")
    assert "net profit" in a.decision.why


def test_seed_evidence_alone_cannot_trigger_buy():
    a = analyse_flip(listing(price=500.0, total_acquisition=500.0),
                     WatchRef("BREITLING", "SuperOcean", "A17366", 1400, 1520, 1650),
                     no_sold(), active_listing_count=0)
    assert a.verdict != BUY
    assert a.evidence.unverified is True


def test_asking_only_evidence_cannot_trigger_buy():
    a = analyse_flip(listing(price=900.0, total_acquisition=900.0),
                     WatchRef("BREITLING", "SuperOcean", "A17366", 1400, 1520, 1650),
                     no_sold(), active_listing_count=5,
                     asking_prices=[1600, 1700, 1750, 1680, 1720])
    assert a.verdict != BUY


def test_watch_verdict_carries_target_offer():
    a = analyse_flip(
        listing(price=1150.0, total_acquisition=1150.0),
        WatchRef("BREITLING", "SuperOcean", "A17366", 1400, 1520, 1650),
        sold(count=10), active_listing_count=3)
    if a.verdict == WATCH and a.decision.target_offer:
        o = a.decision.target_offer
        assert o.low < o.high <= a.max_buy.standard
        assert "MAX BUY" in o.rationale


def test_target_offer_absent_when_far_too_expensive():
    a = analyse_flip(
        listing(price=2400.0, total_acquisition=2400.0),
        WatchRef("BREITLING", "SuperOcean", "A17366", 1400, 1520, 1650),
        sold(count=10), active_listing_count=3)
    assert a.verdict == PASS
    assert a.decision.target_offer is None


def test_pass_when_holding_period_too_long():
    a = analyse_flip(
        listing(price=800.0, total_acquisition=800.0),
        WatchRef("RADO", "Captain Cook", "R32505203", 1400, 1520, 1650),
        sold(count=1, prices=[1500]), active_listing_count=30)
    assert a.verdict == PASS


def test_damaged_watch_never_buys():
    a = analyse_flip(
        listing(price=700.0, total_acquisition=700.0,
                title="Breitling A17366 cracked crystal spares or repairs"),
        WatchRef("BREITLING", "SuperOcean", "A17366", 1400, 1520, 1650),
        sold(count=14), active_listing_count=2)
    assert a.verdict != BUY


def test_every_verdict_has_an_explanation():
    for price in (900.0, 1150.0, 2400.0):
        a = analyse_flip(listing(price=price, total_acquisition=price),
                         WatchRef("BREITLING", "SuperOcean", "A17366", 1400, 1520, 1650),
                         sold(count=10), active_listing_count=3)
        assert a.decision.why and len(a.decision.why) > 40


def test_insufficient_evidence_explanation_is_explicit():
    a = analyse_flip(listing(), WatchRef("BREITLING", "SuperOcean", "A17366"),
                     no_sold(), active_listing_count=0)
    assert "Insufficient Market Evidence" in a.decision.why


def test_thresholds_are_configurable():
    strict = Phase5Config().with_overrides(
        decision=DecisionConfig(min_net_profit_gbp=5000.0))
    a = analyse_flip(
        listing(price=1050.0, total_acquisition=1050.0),
        WatchRef("TUDOR", "Black Bay 58", "79030N", 1400, 1520, 1650),
        sold(count=18, prices=[1500 + i for i in range(18)]),
        active_listing_count=1, config=strict)
    assert a.verdict != BUY


# --- AI triage --------------------------------------------------------------

def test_ai_skipped_when_economics_fail():
    a = analyse_flip(listing(price=2400.0, total_acquisition=2400.0),
                     WatchRef("BREITLING", "SuperOcean", "A17366", 1400, 1520, 1650),
                     sold(count=10), active_listing_count=3)
    should, _ = needs_ai(a)
    assert should is False


def test_ai_requested_for_buy_candidates():
    a = analyse_flip(
        listing(price=1050.0, total_acquisition=1050.0),
        WatchRef("TUDOR", "Black Bay 58", "79030N", 1400, 1520, 1650),
        sold(count=18, prices=[1500 + i for i in range(18)]), active_listing_count=1)
    should, reason = needs_ai(a)
    assert should is True and "BUY" in reason


def test_ai_requested_when_reference_unclear():
    a = analyse_flip(
        listing(price=1100.0, total_acquisition=1100.0,
                title="Breitling A17366 diver, unsure of the model, full set"),
        WatchRef("BREITLING", "SuperOcean", "A17366", 1400, 1520, 1650),
        sold(count=10), active_listing_count=2)
    assert needs_ai(a)[0] is True


# --- capital allocation -----------------------------------------------------

def test_capital_allocation_respects_budget():
    good = analyse_flip(
        listing(item_id="a", price=1050.0, total_acquisition=1050.0),
        WatchRef("TUDOR", "Black Bay 58", "79030N", 1400, 1520, 1650),
        sold(count=18, prices=[1500 + i for i in range(18)]), active_listing_count=1)
    plan = allocate_capital([good], budget=5000.0)
    assert plan.total_capital <= plan.budget
    assert plan.remaining == pytest.approx(plan.budget - plan.total_capital)


def test_capital_allocation_empty_when_nothing_qualifies():
    poor = analyse_flip(listing(price=2400.0, total_acquisition=2400.0),
                        WatchRef("BREITLING", "SuperOcean", "A17366", 1400, 1520, 1650),
                        sold(count=10), active_listing_count=3)
    plan = allocate_capital([poor], budget=5000.0)
    assert plan.selected == []
    assert "No opportunities" in plan.note


# --- sorting ----------------------------------------------------------------

def test_alternative_sort_modes_work():
    a = analyse_flip(listing(item_id="a", price=1050.0, total_acquisition=1050.0),
                     WatchRef("TUDOR", "BB58", "79030N", 1400, 1520, 1650),
                     sold(count=18, prices=[1500 + i for i in range(18)]), 1)
    b = analyse_flip(listing(item_id="b", price=1200.0, total_acquisition=1200.0),
                     WatchRef("BREITLING", "SuperOcean", "A17366", 1400, 1520, 1650),
                     sold(count=8), 4)
    assert rank([a, b], "Highest NET Profit")[0].item_id == "a"
    assert rank([a, b], "Lowest Risk")[0].risk.total <= rank([a, b], "Lowest Risk")[1].risk.total


# --- caching (spec s.19) ----------------------------------------------------

@pytest.fixture
def cconn(tmp_path):
    c = db.connect(tmp_path / "cache.sqlite3")
    db.init_db(c)
    cache.init_cache(c)
    return c


def test_cache_stores_and_returns(cconn):
    cache.put(cconn, cache.NS_LISTINGS, [{"itemId": "1"}], "query")
    assert cache.get(cconn, cache.NS_LISTINGS, "query") == [{"itemId": "1"}]


def test_cache_miss_returns_none(cconn):
    assert cache.get(cconn, cache.NS_LISTINGS, "never-searched") is None


def test_cached_call_runs_function_once(cconn):
    calls = []

    def fetch():
        calls.append(1)
        return ["result"]

    first, cached_first = cache.cached_call(cconn, cache.NS_LISTINGS, ("q",), fetch)
    second, cached_second = cache.cached_call(cconn, cache.NS_LISTINGS, ("q",), fetch)
    assert first == second == ["result"]
    assert cached_first is False and cached_second is True
    assert len(calls) == 1


def test_expired_cache_entry_is_a_miss(cconn):
    cache.put(cconn, cache.NS_LISTINGS, ["old"], "q", ttl_seconds=-1)
    assert cache.get(cconn, cache.NS_LISTINGS, "q") is None


def test_cache_can_be_disabled(cconn):
    off = Phase5Config().with_overrides(
        cache=type(CONFIG.cache)(market_evidence_ttl_hours=24,
                                 listing_search_ttl_minutes=45, enabled=False))
    cache.put(cconn, cache.NS_LISTINGS, ["x"], "q", config=off)
    assert cache.get(cconn, cache.NS_LISTINGS, "q", config=off) is None


def test_query_pruning_drops_only_proven_dead_variants(cconn):
    queries = ["BREITLING A17366", "BREITLING SuperOcean A17366", "A17366"]
    for _ in range(6):
        cache.record_query(cconn, "A17366", queries[1], matches=0)
        cache.record_query(cconn, "A17366", queries[2], matches=2)
    kept = cache.prune_queries(cconn, "A17366", queries)
    assert queries[0] in kept          # primary is never dropped
    assert queries[1] not in kept      # six runs, zero matches
    assert queries[2] in kept


def test_query_pruning_waits_for_enough_evidence(cconn):
    queries = ["a", "b"]
    cache.record_query(cconn, "REF", "b", matches=0)
    assert cache.prune_queries(cconn, "REF", queries) == queries


def test_market_ttl_longer_than_listing_ttl():
    assert (CONFIG.cache.market_evidence_ttl_hours * 60
            > CONFIG.cache.listing_search_ttl_minutes)


# --- dashboard card ---------------------------------------------------------

def test_dashboard_card_contains_key_fields():
    a = analyse_flip(
        listing(price=1050.0, total_acquisition=1050.0),
        WatchRef("TUDOR", "Black Bay 58", "79030N", 1400, 1520, 1650),
        sold(count=18, prices=[1500 + i for i in range(18)]), active_listing_count=1)
    card = dashboard_card(a)
    for token in ("MAX BUY", "FLIP SCORE", "Capital Velocity", "Market Confidence",
                  "Risk", "WHY"):
        assert token in card


def test_unverified_marked_on_card():
    a = analyse_flip(listing(), WatchRef("BREITLING", "SuperOcean", "A17366",
                                         1400, 1520, 1650),
                     no_sold(), active_listing_count=0)
    assert "Insufficient Market Evidence" in dashboard_card(a)


# --- pipeline integration ---------------------------------------------------

class FakeEbay:
    def __init__(self, items):
        self.items = items
        self.calls = 0

    def search(self, query, limit=50, exclusions=()):
        self.calls += 1
        return self.items


class FakeSoldProvider:
    name = "fake"

    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = 0

    def get_recent_sales(self, reference, country="GB", period_days=90):
        self.calls += 1
        return self.evidence


def _ebay_item(item_id, price, title="Tudor Black Bay 58 79030N full set"):
    return {"itemId": item_id, "title": title,
            "price": {"value": str(price), "currency": "GBP"},
            "buyingOptions": ["FIXED_PRICE"],
            "seller": {"username": "s1", "feedbackScore": 900,
                       "feedbackPercentage": "99.9"},
            "itemLocation": {"city": "London", "country": "GB"},
            "condition": "Pre-owned",
            "image": {"imageUrl": "http://img"}}


@pytest.fixture
def pconn(tmp_path):
    c = db.connect(tmp_path / "p5.sqlite3")
    db.init_db(c)
    cache.init_cache(c)
    return c


def test_flip_scan_end_to_end(pconn):
    from wfs.pipeline5 import run_flip_scan
    ref = WatchRef("TUDOR", "Black Bay 58", "79030N", 1400, 1520, 1650)
    provider = FakeSoldProvider(sold(count=18, prices=[1500 + i for i in range(18)]))
    result = run_flip_scan(pconn, [ref], client=FakeEbay([_ebay_item("a", 1050)]),
                           sold_provider=provider, chrono24_provider=None,
                           use_ai=False)
    assert result["stats"].analysed == 1
    a = result["analyses"][0]
    assert a.verdict in (BUY, WATCH, PASS)
    assert a.max_buy.standard is not None


def test_sold_evidence_fetched_once_per_reference(pconn):
    """Efficiency: many listings for one reference must not multiply lookups."""
    from wfs.pipeline5 import run_flip_scan
    ref = WatchRef("TUDOR", "Black Bay 58", "79030N", 1400, 1520, 1650)
    provider = FakeSoldProvider(sold(count=10))
    items = [_ebay_item(f"i{i}", 1050) for i in range(6)]
    result = run_flip_scan(pconn, [ref], client=FakeEbay(items),
                           sold_provider=provider, chrono24_provider=None,
                           use_ai=False)
    assert result["stats"].analysed == 6
    assert provider.calls == 1


def test_second_scan_uses_cache(pconn):
    from wfs.pipeline5 import run_flip_scan
    ref = WatchRef("TUDOR", "Black Bay 58", "79030N", 1400, 1520, 1650)
    ebay = FakeEbay([_ebay_item("a", 1050)])
    provider = FakeSoldProvider(sold(count=10))
    first = run_flip_scan(pconn, [ref], client=ebay, sold_provider=provider,
                          chrono24_provider=None, use_ai=False)
    calls_after_first = ebay.calls
    second = run_flip_scan(pconn, [ref], client=ebay, sold_provider=provider,
                           chrono24_provider=None, use_ai=False)
    assert second["stats"].cache_hits > 0
    assert second["stats"].api_calls == 0
    assert ebay.calls == calls_after_first


def test_inactive_references_are_not_scanned(pconn):
    from wfs.pipeline5 import run_flip_scan
    active = WatchRef("TUDOR", "Black Bay 58", "79030N", 1400, 1520, 1650)
    inactive = WatchRef("OMEGA", "Aqua Terra", "220.10.38.20", 2900, 3200, 3550)
    inactive.active = False
    ebay = FakeEbay([_ebay_item("a", 1050)])
    run_flip_scan(pconn, [active, inactive], client=ebay,
                  sold_provider=FakeSoldProvider(sold(count=10)),
                  chrono24_provider=None, use_ai=False)
    # Only the active reference generated queries.
    assert ebay.calls == len(build_queries(active))


def test_ai_only_called_for_triaged_candidates(pconn):
    from wfs.pipeline5 import run_flip_scan

    class FakeAI:
        model = "fake"

        def __init__(self):
            self.calls = 0

        def complete(self, payload):
            self.calls += 1
            import json as _json
            return _json.dumps({
                "reference_verified": True, "likely_reference": "79030N",
                "authenticity_risk": "LOW", "condition_risk": "LOW",
                "document_risk": "LOW", "liquidity_comment": "ok",
                "pricing_comment": "ok", "mispricing_reason": "ok",
                "verdict": "BUY", "confidence": "HIGH", "warnings": []})

    ref = WatchRef("TUDOR", "Black Bay 58", "79030N", 1400, 1520, 1650)
    # One strong candidate and one hopeless one.
    items = [_ebay_item("good", 1050), _ebay_item("bad", 2400)]
    ai = FakeAI()
    result = run_flip_scan(pconn, [ref], client=FakeEbay(items),
                           sold_provider=FakeSoldProvider(sold(count=18,
                               prices=[1500 + i for i in range(18)])),
                           chrono24_provider=None, ai_client=ai)
    assert ai.calls < len(result["analyses"])
    assert result["stats"].ai_skipped >= 1


def test_scan_stats_reports_api_efficiency(pconn):
    from wfs.pipeline5 import run_flip_scan
    ref = WatchRef("TUDOR", "Black Bay 58", "79030N", 1400, 1520, 1650)
    result = run_flip_scan(pconn, [ref], client=FakeEbay([_ebay_item("a", 1050)]),
                           sold_provider=FakeSoldProvider(sold(count=10)),
                           chrono24_provider=None, use_ai=False)
    d = result["stats"].as_dict()
    assert d["api_calls"] >= 1
    assert "api_calls_avoided" in d
    assert "listings scanned" in result["stats"].summary_line()


# --- no false precision (spec s.6) ------------------------------------------

def test_day_ranges_capped_to_avoid_false_precision():
    from wfs.flip import DaysToSell as D
    assert D.format((10, 24)) == "10–24 days"
    assert D.format((300, 900)) == "300–365+ days"
    assert D.format((1241, 2836)) == "over 365 days"
    assert D.format(None) == "cannot be estimated"


def test_illiquid_watch_reports_over_a_year_not_a_precise_number():
    a = analyse_flip(
        listing(price=620.0, total_acquisition=620.0),
        WatchRef("RADO", "Captain Cook", "R32505203", 950, 1100, 1250),
        sold(count=1, prices=[1080]), active_listing_count=19)
    from wfs.flip import DaysToSell as D
    label = D.format(a.base.days) if a.base else ""
    assert "over 365" in label or "365+" in label
    assert a.verdict == PASS
