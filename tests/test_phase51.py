"""Phase 5.1 tests: seller profiles, corrected economics, evidence architecture."""
from __future__ import annotations

import json

import pytest

from wfs import db, manual_evidence, observation, settings_store
from wfs.condition import assess_condition
from wfs.config5 import (EVIDENCE_OBSERVED_ACTIVITY, EVIDENCE_REFERENCE_EXACT,
                         EVIDENCE_SEED, Phase5Config)
from wfs.decision import BUY, PASS, WATCH
from wfs.economics import (CUSTOM, UK_BUSINESS, UK_PRIVATE, EconomicsEngine,
                           ExitCosts, SellerProfile, get_profile,
                           uk_business_profile, uk_private_profile)
from wfs.evidence import from_sold_evidence, score_confidence
from wfs.flip import BASE, cost_breakdown, effective_acquisition
from wfs.flip_analysis import analyse_flip
from wfs.sold_market import (COVERAGE_PARTIAL, COVERAGE_UNAVAILABLE,
                             CompositeSoldProvider, MarketplaceInsightsProvider,
                             NullSoldProvider, SoldEvidence)
from wfs.watchlist import WatchRef


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "p51.sqlite3")
    db.init_db(c)
    manual_evidence.init_manual_schema(c)
    return c


def sold(count=14, prices=None):
    prices = prices or [2700 + i * 10 for i in range(count)]
    return SoldEvidence("79030N", 90, "manual_sold_evidence", COVERAGE_PARTIAL,
                        exact_sale_count=count, family_sale_count=count,
                        prices=prices[:count])


def listing(**kw):
    base = {
        "item_id": "v1|1|0",
        "title": "Tudor Black Bay 58 79030N 2023 full set box and papers serviced",
        "price": 1850.0, "shipping": 0.0, "total_acquisition": 1850.0,
        "buying_format": "BUY_IT_NOW", "condition": "Pre-owned",
        "seller_feedback_pct": 99.9, "seller_feedback_score": 1200,
        "item_location": "London, GB", "authenticity_guarantee": True,
        "returns_accepted": True, "image_url": "http://img",
        "brand": "TUDOR", "model": "Black Bay 58",
    }
    base.update(kw)
    return base


REF = WatchRef("TUDOR", "Black Bay 58", "79030N", 2600, 2750, 2900)


# --- UK private seller fees -------------------------------------------------

def test_uk_private_has_no_transaction_or_payment_fee():
    p = uk_private_profile()
    assert p.transaction_fee_pct == 0.0
    assert p.payment_processing_fee_pct == 0.0
    assert p.total_percentage_fees == 0.0
    assert p.is_estimate is False


def test_uk_private_still_charges_real_seller_costs():
    costs = EconomicsEngine(uk_private_profile()).exit_costs(1300.0)
    assert costs.shipping > 0 and costs.insurance > 0 and costs.packaging > 0
    assert costs.transaction_fee == 0.0
    assert costs.net_proceeds < 1300.0


def test_default_profile_is_uk_private():
    assert get_profile().name == UK_PRIVATE
    assert settings_store.DEFAULT_SETTINGS["seller_profile"] == UK_PRIVATE


def test_mode_label_is_visible_and_marks_estimates():
    assert EconomicsEngine(uk_private_profile()).mode_label == "UK Private Seller"
    assert "estimate" in EconomicsEngine(uk_business_profile()).mode_label.lower()


# --- SPEC §24: the headline regression --------------------------------------

def test_uk_private_does_not_deduct_legacy_percentage_fees():
    """Buy £1,000, resell £1,300, UK private, service reserve applied.

    The engine must NOT deduct roughly £190 of legacy percentage fees. Only
    postage £12 + insurance £15 + packaging £5 + service reserve £26 apply.
    """
    engine = EconomicsEngine(uk_private_profile().with_overrides(
        shipping_cost_gbp=12.0, insurance_cost_gbp=15.0, packaging_cost_gbp=5.0,
        service_reserve_pct=0.02))
    result = engine.profit(sale_price=1300.0, listing_price=1000.0,
                           needs_service_reserve=True)

    assert result.costs.transaction_fee == 0.0
    assert result.costs.payment_processing_fee == 0.0
    # 12 + 15 + 5 + (1300 * 2%) = 58
    assert result.costs.total == pytest.approx(58.0, abs=0.01)
    assert result.net_profit == pytest.approx(242.0, abs=0.01)
    # The old model would have left roughly £52 here.
    assert result.net_profit > 200


def test_same_transaction_under_business_profile_costs_more():
    private = EconomicsEngine(uk_private_profile()).profit(1300.0, 1000.0, True)
    business = EconomicsEngine(uk_business_profile()).profit(1300.0, 1000.0, True)
    assert business.costs.total > private.costs.total
    assert business.net_profit < private.net_profit
    assert business.costs.transaction_fee > 0


# --- business and custom profiles -------------------------------------------

def test_uk_business_profile_is_flagged_as_estimate():
    p = uk_business_profile()
    assert p.is_estimate is True
    assert "ESTIMATE" in p.notes.upper()
    assert p.transaction_fee_pct > 0


def test_custom_profile_fully_configurable():
    p = get_profile(CUSTOM, transaction_fee_pct=0.08,
                    payment_processing_fee_pct=0.0,
                    promoted_listing_enabled=True, promoted_listing_fee_pct=0.025)
    assert p.name == CUSTOM
    assert p.total_percentage_fees == pytest.approx(0.105)


def test_unknown_profile_falls_back_to_private():
    assert get_profile("NOT_A_PROFILE").name == UK_PRIVATE


# --- promoted listings ------------------------------------------------------

def test_promoted_listing_costs_nothing_when_disabled():
    p = uk_private_profile().with_overrides(promoted_listing_enabled=False,
                                            promoted_listing_fee_pct=0.05)
    costs = EconomicsEngine(p).exit_costs(1000.0)
    assert costs.promoted_listing_fee == 0.0
    assert p.total_percentage_fees == 0.0


def test_promoted_listing_applies_when_enabled():
    p = uk_private_profile().with_overrides(promoted_listing_enabled=True,
                                            promoted_listing_fee_pct=0.05)
    costs = EconomicsEngine(p).exit_costs(1000.0)
    assert costs.promoted_listing_fee == pytest.approx(50.0)


# --- international ----------------------------------------------------------

def test_international_fee_off_by_default():
    p = uk_private_profile()
    assert p.international_sale is False
    assert EconomicsEngine(p).exit_costs(1000.0).international_fee == 0.0


def test_international_fee_applies_when_enabled():
    p = uk_private_profile().with_overrides(international_sale=True,
                                            international_fee_pct=0.013)
    assert EconomicsEngine(p).exit_costs(1000.0).international_fee == pytest.approx(13.0)


# --- SPEC §6: no double counting --------------------------------------------

def test_each_cost_line_appears_exactly_once():
    costs = EconomicsEngine(uk_business_profile()).exit_costs(1000.0, True)
    manual_sum = sum(getattr(costs, k) for k in ExitCosts.LINE_ITEMS)
    assert costs.total == pytest.approx(manual_sum, abs=0.01)
    assert len(ExitCosts.LINE_ITEMS) == len(set(ExitCosts.LINE_ITEMS))


def test_payment_fee_not_bundled_into_transaction_fee():
    p = get_profile(CUSTOM, transaction_fee_pct=0.10,
                    payment_processing_fee_pct=0.02)
    costs = EconomicsEngine(p).exit_costs(1000.0)
    assert costs.transaction_fee == pytest.approx(100.0)
    assert costs.payment_processing_fee == pytest.approx(20.0)
    assert costs.total == pytest.approx(120.0 + p.fixed_physical_costs, abs=0.01)


def test_shipping_counted_once():
    p = uk_private_profile().with_overrides(shipping_cost_gbp=12.0)
    costs = EconomicsEngine(p).exit_costs(1000.0)
    assert costs.shipping == 12.0
    assert sum(1 for label, _ in costs.lines() if label == "Postage") == 1


def test_negotiation_allowance_applied_once_not_twice():
    """It reduces the acquisition price; it must not also inflate MAX BUY.

    Before Phase 5.1, MAX BUY was divided by (1 - negotiation) while the
    acquisition price was separately multiplied by it — counting the same
    benefit twice.
    """
    p = uk_private_profile().with_overrides(negotiation_allowance_pct=0.10)
    engine = EconomicsEngine(p)
    assert engine.effective_acquisition(1000.0) == pytest.approx(900.0)

    no_neg = EconomicsEngine(uk_private_profile())
    # MAX BUY depends only on resale economics, not on the negotiation allowance.
    assert engine.max_buy(2000.0, 0.12) == pytest.approx(no_neg.max_buy(2000.0, 0.12))


def test_service_reserve_only_in_costs_not_in_risk_buffer():
    engine = EconomicsEngine(uk_private_profile())
    with_reserve = engine.exit_costs(1000.0, True)
    without = engine.exit_costs(1000.0, False)
    assert with_reserve.service_reserve > 0
    assert without.service_reserve == 0.0
    # The risk buffer is a separate multiplier on MAX BUY, not a cost line.
    assert "service_reserve" in ExitCosts.LINE_ITEMS
    assert not any("risk" in k for k in ExitCosts.LINE_ITEMS)


# --- SPEC §7: MAX BUY differs by seller profile -----------------------------

def test_max_buy_private_exceeds_business_all_else_equal():
    """Same watch, resale, risk and market data — only the seller profile differs."""
    private_cfg = Phase5Config().with_profile(UK_PRIVATE)
    business_cfg = Phase5Config().with_profile(UK_BUSINESS)

    a_private = analyse_flip(listing(), REF, sold(), active_listing_count=2,
                             config=private_cfg)
    a_business = analyse_flip(listing(), REF, sold(), active_listing_count=2,
                              config=business_cfg)

    assert a_private.max_buy.standard > a_business.max_buy.standard
    assert a_private.max_buy.conservative > a_business.max_buy.conservative
    assert a_private.evidence.median_sold_price == a_business.evidence.median_sold_price
    assert a_private.risk.total == a_business.risk.total


def test_max_buy_uses_engine_not_hardcoded_fees():
    engine_free = EconomicsEngine(get_profile(
        CUSTOM, shipping_cost_gbp=0.0, insurance_cost_gbp=0.0,
        packaging_cost_gbp=0.0, service_reserve_pct=0.0))
    # Zero costs, zero margin, zero buffers -> MAX BUY equals the anchor.
    assert engine_free.max_buy(1000.0, 0.0, 0.0, 0.0) == pytest.approx(1000.0)


# --- SPEC §8 / §25: corrected economics flow through everything -------------

def test_net_profit_differs_by_profile():
    private = analyse_flip(listing(), REF, sold(), 2,
                           config=Phase5Config().with_profile(UK_PRIVATE))
    business = analyse_flip(listing(), REF, sold(), 2,
                            config=Phase5Config().with_profile(UK_BUSINESS))
    assert private.net_profit > business.net_profit
    assert private.net_roi > business.net_roi


def test_capital_velocity_uses_corrected_net_profit():
    private = analyse_flip(listing(), REF, sold(), 2,
                           config=Phase5Config().with_profile(UK_PRIVATE))
    business = analyse_flip(listing(), REF, sold(), 2,
                            config=Phase5Config().with_profile(UK_BUSINESS))
    assert private.velocity.profit_per_30d > business.velocity.profit_per_30d
    assert private.velocity.score >= business.velocity.score


def test_flip_score_uses_corrected_economics():
    # A thin-margin listing, so the profit component is not saturated at 100 for
    # both profiles and the difference is actually visible.
    thin = listing(price=2350.0, total_acquisition=2350.0)
    private = analyse_flip(thin, REF, sold(), 2,
                           config=Phase5Config().with_profile(UK_PRIVATE))
    business = analyse_flip(thin, REF, sold(), 2,
                            config=Phase5Config().with_profile(UK_BUSINESS))
    assert private.flip_score.score > business.flip_score.score
    assert (private.flip_score.components["profit_quality"]
            > business.flip_score.components["profit_quality"])


def test_verdict_can_change_with_seller_profile():
    """The FINAL CHECK: every dependent metric moves together."""
    price = 2150.0
    private = analyse_flip(listing(price=price, total_acquisition=price), REF,
                           sold(), 2, config=Phase5Config().with_profile(UK_PRIVATE))
    business = analyse_flip(listing(price=price, total_acquisition=price), REF,
                            sold(), 2, config=Phase5Config().with_profile(UK_BUSINESS))
    for attr in ("net_profit", "net_roi"):
        assert getattr(private, attr) > getattr(business, attr)
    assert private.max_buy.standard > business.max_buy.standard
    assert private.base.costs.total < business.base.costs.total
    # Private clears MAX BUY here; business does not.
    assert private.acquisition <= private.max_buy.standard
    assert business.acquisition > business.max_buy.standard
    assert business.verdict in (WATCH, PASS)


def test_seller_mode_is_recorded_on_the_analysis():
    a = analyse_flip(listing(), REF, sold(), 2,
                     config=Phase5Config().with_profile(UK_PRIVATE))
    assert a.seller_mode == "UK Private Seller"
    assert "seller_mode" in a.max_buy.as_dict()


def test_strategies_expose_itemised_cost_lines():
    a = analyse_flip(listing(), REF, sold(), 2,
                     config=Phase5Config().with_profile(UK_PRIVATE))
    lines = a.base.as_dict()["cost_lines"]
    assert any(label == "Postage" for label, _ in lines)
    assert a.base.costs.total == pytest.approx(sum(v for _, v in lines), abs=0.01)


# --- settings persistence ---------------------------------------------------

def test_settings_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    saved = settings_store.save_settings(
        {"seller_profile": UK_BUSINESS,
         "overrides": {"shipping_cost_gbp": 20.0}}, path)
    loaded = settings_store.load_settings(path)
    assert loaded["seller_profile"] == UK_BUSINESS
    assert loaded["overrides"]["shipping_cost_gbp"] == 20.0
    assert saved["seller_profile"] == UK_BUSINESS


def test_settings_never_persist_secrets(tmp_path):
    path = tmp_path / "settings.json"
    settings_store.save_settings(
        {"seller_profile": UK_PRIVATE, "ebay_client_secret": "SECRET",
         "anthropic_api_key": "KEY", "overrides": {}}, path)
    raw = path.read_text()
    assert "SECRET" not in raw and "KEY" not in raw
    assert "ebay_client_secret" not in raw


def test_corrupt_settings_falls_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ not json", encoding="utf-8")
    assert settings_store.load_settings(path)["seller_profile"] == UK_PRIVATE


def test_unknown_override_keys_are_dropped(tmp_path):
    path = tmp_path / "settings.json"
    settings_store.save_settings(
        {"seller_profile": UK_PRIVATE,
         "overrides": {"shipping_cost_gbp": 9.0, "evil_key": 1}}, path)
    loaded = settings_store.load_settings(path)
    assert "evil_key" not in loaded["overrides"]


# --- manual sold evidence import --------------------------------------------

GOOD_CSV = (
    "brand,reference,model,sold_price_gbp,sold_date,condition,full_set,source,notes\n"
    "Longines,L3.781.4.56.6,HydroConquest,1125,2026-08-20,excellent,yes,eBay UK,\n"
    "Tudor,79030N,Black Bay 58,2680,2026-08-14,very good,yes,eBay UK,full set\n"
)


def test_valid_csv_imports(conn):
    report = manual_evidence.import_csv_text(conn, GOOD_CSV)
    assert report.imported == 2
    assert report.invalid == 0 and report.duplicates == 0
    assert set(report.references_affected) == {"L3.781.4.56.6", "79030N"}


def test_duplicate_records_rejected(conn):
    manual_evidence.import_csv_text(conn, GOOD_CSV)
    second = manual_evidence.import_csv_text(conn, GOOD_CSV)
    assert second.imported == 0
    assert second.duplicates == 2
    assert "duplicate" in second.errors[0].reason


def test_malformed_price_rejected(conn):
    bad = ("brand,reference,model,sold_price_gbp,sold_date,condition,full_set,source,notes\n"
           "Tudor,79030N,BB58,not-a-number,2026-08-14,good,yes,eBay UK,\n")
    report = manual_evidence.import_csv_text(conn, bad)
    assert report.imported == 0 and report.invalid == 1


def test_invalid_date_rejected(conn):
    bad = ("brand,reference,model,sold_price_gbp,sold_date,condition,full_set,source,notes\n"
           "Tudor,79030N,BB58,2680,not-a-date,good,yes,eBay UK,\n")
    report = manual_evidence.import_csv_text(conn, bad)
    assert report.invalid == 1
    assert "date" in report.errors[0].reason


def test_future_date_rejected(conn):
    bad = ("brand,reference,model,sold_price_gbp,sold_date,condition,full_set,source,notes\n"
           "Tudor,79030N,BB58,2680,2099-01-01,good,yes,eBay UK,\n")
    assert manual_evidence.import_csv_text(conn, bad).invalid == 1


def test_missing_required_column_rejected(conn):
    bad = "brand,model,sold_date\nTudor,BB58,2026-08-14\n"
    report = manual_evidence.import_csv_text(conn, bad)
    assert report.invalid == 1
    assert "missing required column" in report.errors[0].reason


def test_missing_brand_value_rejected(conn):
    bad = ("brand,reference,model,sold_price_gbp,sold_date,condition,full_set,source,notes\n"
           ",79030N,BB58,2680,2026-08-14,good,yes,eBay UK,\n")
    report = manual_evidence.import_csv_text(conn, bad)
    assert report.invalid == 1 and "brand" in report.errors[0].reason


def test_price_formats_tolerated(conn):
    csv_text = ("brand,reference,model,sold_price_gbp,sold_date,condition,full_set,source,notes\n"
                "Tudor,79030N,BB58,\"£2,680.50\",14/08/2026,good,yes,eBay UK,\n")
    report = manual_evidence.import_csv_text(conn, csv_text)
    assert report.imported == 1
    assert manual_evidence.stored_records(conn)[0]["sold_price_gbp"] == 2680.50


def test_imported_records_become_sold_evidence(conn):
    manual_evidence.import_csv_text(conn, GOOD_CSV)
    provider = manual_evidence.DatabaseManualSoldProvider(conn, period_days=100000)
    ev = provider.get_recent_sales("79030N")
    assert ev.has_evidence
    assert ev.exact_sale_count == 1
    assert ev.source == "manual_sold_evidence"


def test_template_has_all_columns():
    header = manual_evidence.TEMPLATE_CSV.splitlines()[0].split(",")
    for col in manual_evidence.REQUIRED_COLUMNS:
        assert col in header


# --- local observation classification ---------------------------------------

def _add_listing(conn, item_id, price, reference="79030N"):
    return db.upsert_listing(conn, {
        "item_id": item_id, "title": "Tudor Black Bay 58 79030N",
        "price": price, "shipping": 0.0, "total_acquisition": price,
        "reference": reference, "brand": "TUDOR", "model": "Black Bay 58",
        "url": f"http://ebay/{item_id}",
    })


def test_stable_listing_that_vanishes_is_likely_sold(conn):
    _add_listing(conn, "a", 2700)
    _add_listing(conn, "a", 2700)
    db.mark_absent_listings(conn, ["79030N"], set())
    obs = observation.observations_for(conn, "79030N")[0]
    assert obs.status == observation.LIKELY_SOLD
    assert "unconfirmed" in obs.rationale


def test_repeatedly_discounted_listing_is_not_called_sold(conn):
    for price in (2700, 2600, 2500, 2400):
        _add_listing(conn, "b", price)
    db.mark_absent_listings(conn, ["79030N"], set())
    obs = observation.observations_for(conn, "79030N")[0]
    assert obs.status == observation.DELISTED_UNKNOWN


def test_single_sighting_is_unknown(conn):
    _add_listing(conn, "c", 2700)
    db.mark_absent_listings(conn, ["79030N"], set())
    assert observation.observations_for(conn, "79030N")[0].status == \
        observation.DELISTED_UNKNOWN


def test_active_listing_is_not_classified_as_sold(conn):
    _add_listing(conn, "d", 2700)
    _add_listing(conn, "d", 2700)
    assert observation.observations_for(conn, "79030N")[0].status == observation.ACTIVE


def test_observed_activity_language_is_not_confirmed_sales(conn):
    _add_listing(conn, "e", 2700)
    _add_listing(conn, "e", 2700)
    db.mark_absent_listings(conn, ["79030N"], set())
    text = observation.observed_activity(conn, "79030N").describe()
    assert "Observed market activity" in text
    assert "not confirmed as a sale" in text.lower()


def test_thin_observation_history_yields_no_evidence(conn):
    _add_listing(conn, "f", 2700)
    provider = observation.LocalObservationProvider(conn)
    assert provider.get_recent_sales("79030N").coverage_status == COVERAGE_UNAVAILABLE


def test_observation_evidence_is_weaker_tier(conn):
    for item in ("g", "h", "i"):
        _add_listing(conn, item, 2700)
        _add_listing(conn, item, 2700)
    db.mark_absent_listings(conn, ["79030N"], set())
    ev_obj = observation.LocalObservationProvider(conn).get_recent_sales("79030N")
    evidence = from_sold_evidence(ev_obj, "79030N", active_listing_count=1)
    assert evidence.evidence_level == EVIDENCE_OBSERVED_ACTIVITY
    assert evidence.has_sold_evidence is False       # never counts as sold
    assert evidence.has_observed_activity is True
    assert score_confidence(evidence).score <= 55    # cannot reach the BUY gate


# --- evidence source hierarchy and labels -----------------------------------

def test_evidence_source_labels_are_unambiguous():
    manual = from_sold_evidence(sold(), "79030N", 2)
    assert manual.source_label == "Exact reference sold evidence — Manual Sold Evidence"

    asking = from_sold_evidence(
        SoldEvidence("79030N", 90, "none", COVERAGE_UNAVAILABLE), "79030N",
        asking_prices=[2900, 3000, 3100])
    assert "Active Asking Prices" in asking.source_label

    seed = from_sold_evidence(
        SoldEvidence("79030N", 90, "none", COVERAGE_UNAVAILABLE), "79030N",
        seed_mid=2750)
    assert seed.source_label == "Seed / Unverified"


def test_insights_evidence_labelled_as_such():
    ev = from_sold_evidence(
        SoldEvidence("79030N", 90, "ebay_marketplace_insights", COVERAGE_PARTIAL,
                     8, 8, [2700] * 8), "79030N", 2)
    assert "Marketplace Insights" in ev.source_label


def test_marketplace_insights_is_optional_not_required():
    provider = MarketplaceInsightsProvider()
    assert provider.enabled is False
    ev = provider.get_recent_sales("79030N")
    assert ev.coverage_status == COVERAGE_UNAVAILABLE
    assert "not granted" in ev.notes.lower()
    assert "OPTIONAL" in MarketplaceInsightsProvider.__doc__.upper()


# --- composite provider priority --------------------------------------------

class _Stub:
    def __init__(self, name, evidence):
        self.name = name
        self.evidence = evidence

    def get_recent_sales(self, reference, country="GB", period_days=90):
        return self.evidence


def test_composite_prefers_the_strongest_available_source():
    strong = _Stub("manual_sold_evidence", sold())
    weak = _Stub("local_observation",
                 SoldEvidence("79030N", 180, "local_observation", COVERAGE_PARTIAL,
                              3, 3, [2600, 2650, 2700]))
    composite = CompositeSoldProvider([strong, weak])
    result = composite.get_recent_sales("79030N")
    assert result.source == "manual_sold_evidence"
    assert composite.last_source == "manual_sold_evidence"


def test_composite_falls_through_to_observation():
    empty = _Stub("manual_sold_evidence",
                  SoldEvidence("79030N", 90, "manual_sold_evidence",
                               COVERAGE_UNAVAILABLE))
    weak = _Stub("local_observation",
                 SoldEvidence("79030N", 180, "local_observation", COVERAGE_PARTIAL,
                              3, 3, [2600, 2650, 2700]))
    assert CompositeSoldProvider([empty, weak]).get_recent_sales("79030N").source \
        == "local_observation"


def test_composite_returns_unavailable_when_nothing_has_evidence():
    empty = _Stub("x", SoldEvidence("79030N", 90, "x", COVERAGE_UNAVAILABLE))
    result = CompositeSoldProvider([empty]).get_recent_sales("79030N")
    assert result.coverage_status == COVERAGE_UNAVAILABLE


# --- SPEC §16: BUY safety preserved -----------------------------------------

def test_asking_prices_alone_cannot_produce_buy():
    a = analyse_flip(listing(price=1500.0, total_acquisition=1500.0), REF,
                     SoldEvidence("79030N", 90, "none", COVERAGE_UNAVAILABLE),
                     active_listing_count=5,
                     asking_prices=[2900, 3000, 3100, 2950, 3050])
    assert a.verdict != BUY


def test_seed_values_alone_cannot_produce_buy():
    a = analyse_flip(listing(price=900.0, total_acquisition=900.0), REF,
                     SoldEvidence("79030N", 90, "none", COVERAGE_UNAVAILABLE),
                     active_listing_count=0)
    assert a.verdict != BUY
    assert a.evidence.unverified is True


def test_observation_only_evidence_cannot_produce_buy(conn):
    for item in ("j", "k", "l", "m"):
        _add_listing(conn, item, 2700)
        _add_listing(conn, item, 2700)
    db.mark_absent_listings(conn, ["79030N"], set())
    ev_obj = observation.LocalObservationProvider(conn).get_recent_sales("79030N")
    a = analyse_flip(listing(price=1500.0, total_acquisition=1500.0), REF,
                     ev_obj, active_listing_count=1)
    assert a.verdict != BUY
    assert a.decision.qualifier == "Needs Market Confirmation" or a.verdict == PASS


def test_watch_carries_needs_market_confirmation_qualifier():
    a = analyse_flip(listing(price=2000.0, total_acquisition=2000.0), REF,
                     SoldEvidence("79030N", 90, "none", COVERAGE_UNAVAILABLE),
                     active_listing_count=5,
                     asking_prices=[2900, 3000, 3100, 2950, 3050])
    if a.verdict == WATCH:
        assert a.decision.qualifier == "Needs Market Confirmation"
        assert "Needs Market Confirmation" in a.decision.display_verdict


def test_real_sold_evidence_carries_no_qualifier():
    a = analyse_flip(listing(), REF, sold(), 2,
                     config=Phase5Config().with_profile(UK_PRIVATE))
    assert a.decision.qualifier == ""


# --- packaging --------------------------------------------------------------

def test_pyproject_configures_pytest_path():
    import pathlib
    import tomllib
    root = pathlib.Path(__file__).resolve().parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text())
    assert "." in data["tool"]["pytest"]["ini_options"]["pythonpath"]
    assert data["tool"]["setuptools"]["packages"] == ["wfs"]


def test_wfs_package_importable_from_root():
    import wfs.economics
    assert wfs.economics.UK_PRIVATE == "UK_PRIVATE"
