from __future__ import annotations

import pytest

from wfs import db, market as market_engine
from wfs.analysis import analyse, decide_verdict
from wfs.liquidity import assess as assess_liquidity
from wfs.market import SourceInput, asking_to_source, estimate
from wfs.pipeline import report_rows, run_scan
from wfs.pricing import (assess_risk, build_scenarios, max_buy,
                         net_of_selling_costs)
from wfs.sold_market import (COVERAGE_PARTIAL, COVERAGE_UNAVAILABLE,
                             ManualSoldProvider, NullSoldProvider, SoldEvidence)
from wfs.watchlist import WatchRef


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "p2.sqlite3")
    db.init_db(c)
    return c


@pytest.fixture
def ref():
    return WatchRef("TUDOR", "Black Bay 58", "79030N", 2150, 2400, 2650)


def sold(count=8, prices=None, period=90):
    prices = prices or [2300, 2350, 2400, 2400, 2450, 2500, 2380, 2420][:count]
    return SoldEvidence("79030N", period, "test", COVERAGE_PARTIAL,
                        exact_sale_count=count, family_sale_count=count + 6,
                        prices=prices)


def no_sold():
    return SoldEvidence("79030N", 90, "none", COVERAGE_UNAVAILABLE)


def listing(**kw):
    base = {
        "item_id": "v1|1|0",
        "title": "Tudor Black Bay 58 79030N 2022 full set box and papers serviced",
        "price": 1700.0, "shipping": 0.0, "total_acquisition": 1700.0,
        "buying_format": "BUY_IT_NOW", "condition": "Pre-owned",
        "seller": "goodseller", "seller_feedback_pct": 99.8,
        "seller_feedback_score": 800, "item_location": "London, GB",
        "authenticity_guarantee": True, "brand": "TUDOR", "reference": "79030N",
    }
    base.update(kw)
    return base


# --- sold market providers --------------------------------------------------

def test_null_provider_never_invents_counts():
    ev = NullSoldProvider().get_recent_sales("79030N")
    assert ev.coverage_status == COVERAGE_UNAVAILABLE
    assert ev.has_evidence is False
    assert NullSoldProvider().get_sale_count("79030N")["observed_sale_count"] is None


def test_manual_provider_reads_csv(tmp_path):
    csv_path = tmp_path / "observed_sales.csv"
    csv_path.write_text(
        "sold_date,reference,model_family,price_gbp,source,notes\n"
        "2026-08-01,79030N,Black Bay,2400,ebay,\n"
        "2026-08-10,79030N,Black Bay,2350,ebay,\n"
        "2019-01-01,79030N,Black Bay,1900,ebay,too old\n",
        encoding="utf-8",
    )
    ev = ManualSoldProvider(csv_path).get_recent_sales("79030N", period_days=90)
    assert ev.exact_sale_count == 2          # the 2019 sale is outside the window
    assert ev.coverage_status == COVERAGE_PARTIAL
    assert ev.median_price == pytest.approx(2375.0)


def test_missing_csv_reports_unavailable(tmp_path):
    ev = ManualSoldProvider(tmp_path / "nope.csv").get_recent_sales("79030N")
    assert ev.coverage_status == COVERAGE_UNAVAILABLE


def test_describe_says_observed_not_total():
    text = sold().describe()
    assert "Observed exact-reference sales" in text
    assert "Total UK sales" not in text


# --- market value engine ----------------------------------------------------

def test_estimate_falls_back_to_seed_when_no_evidence():
    mv = estimate(no_sold(), [], 2150, 2400, 2650)
    assert mv.seed_only is True
    assert mv.market_mid == 2400
    assert mv.confidence == "LOW"


def test_confidence_high_with_eight_sales():
    mv = estimate(sold(8), [])
    assert mv.confidence == "HIGH"
    assert "8 exact-reference" in mv.confidence_reason


def test_confidence_medium_then_low():
    assert estimate(sold(5, [2300, 2400, 2450, 2350, 2380]), []).confidence == "MEDIUM"
    assert estimate(sold(2, [2300, 2400]), []).confidence == "LOW"


def test_weights_redistribute_when_source_missing():
    mv = estimate(sold(8), [])
    assert mv.weights["SOLD_EBAY_UK"] == pytest.approx(1.0)
    mv2 = estimate(sold(8), [asking_to_source("ASKING_CHRONO24",
                                              [2600, 2700, 2800, 2900])])
    assert sum(mv2.weights.values()) == pytest.approx(1.0, abs=0.01)
    assert mv2.weights["SOLD_EBAY_UK"] > mv2.weights["ASKING_CHRONO24"]


def test_asking_source_is_conservative():
    src = asking_to_source("ASKING_CHRONO24", [2500, 2700, 2900, 3100])
    assert src.mid < 2700     # drawn from the lower half, not the mean


def test_asking_only_estimate_is_haircut():
    mv = estimate(no_sold(), [asking_to_source("ASKING_CHRONO24",
                                               [2500, 2700, 2900, 3100])])
    assert mv.market_mid < 2600
    assert mv.confidence == "LOW"
    assert "asking prices only" in mv.confidence_reason


def test_asking_source_ignored_below_two_points():
    assert asking_to_source("ASKING_CHRONO24", [2500]).usable is False


# --- liquidity --------------------------------------------------------------

def test_liquidity_unknown_without_sold_evidence():
    liq = assess_liquidity("79030N", "TUDOR", no_sold(), competing_listings=4)
    assert liq.rating == "UNKNOWN"
    assert liq.days_to_sale["BASE"] is None
    assert "cannot be estimated" in liq.basis


def test_liquidity_rates_and_estimates_days():
    liq = assess_liquidity("79030N", "TUDOR", sold(8), competing_listings=3)
    assert liq.monthly_sale_rate == pytest.approx(2.67, abs=0.02)
    assert liq.rating in {"HIGH", "MEDIUM"}
    lo, hi = liq.days_to_sale["BASE"]
    assert 0 < lo < hi


def test_more_competition_slows_estimated_sale():
    fast = assess_liquidity("79030N", "TUDOR", sold(8), competing_listings=1)
    slow = assess_liquidity("79030N", "TUDOR", sold(8), competing_listings=20)
    assert slow.absorption_days > fast.absorption_days
    assert slow.days_to_sale["BASE"][1] > fast.days_to_sale["BASE"][1]


def test_illiquid_brand_never_rated_high():
    liq = assess_liquidity("R32505203", "RADO", sold(20, [1000] * 20), 1)
    assert liq.rating != "HIGH"


def test_liquidity_wording_is_an_estimate():
    assert "Estimated based on observed UK sales frequency" in \
        assess_liquidity("79030N", "TUDOR", sold(8), 3).basis


# --- risk -------------------------------------------------------------------

def test_clean_listing_is_low_risk():
    mv = estimate(sold(10), [])
    liq = assess_liquidity("79030N", "TUDOR", sold(10), 2)
    risk = assess_risk(listing(), mv, liq)
    assert risk.level == "LOW"


def test_missing_papers_raises_buffer():
    mv = estimate(sold(10), [])
    liq = assess_liquidity("79030N", "TUDOR", sold(10), 2)
    clean = assess_risk(listing(), mv, liq)
    bare = assess_risk(listing(title="Tudor 79030N watch"), mv, liq)
    assert bare.buffer > clean.buffer


def test_very_low_price_increases_scrutiny():
    mv = estimate(sold(10), [])
    liq = assess_liquidity("79030N", "TUDOR", sold(10), 2)
    cheap = assess_risk(listing(total_acquisition=1100.0), mv, liq)
    normal = assess_risk(listing(total_acquisition=1700.0), mv, liq)
    assert any("elevated authenticity scrutiny" in f for f in cheap.scrutiny_factors)
    assert cheap.total_buffer > normal.total_buffer
    # ...but scrutiny must not change the structural buffer MAX BUY depends on.
    assert cheap.buffer == normal.buffer


def test_seed_only_market_raises_risk():
    liq = assess_liquidity("79030N", "TUDOR", no_sold(), 2)
    seed_mv = estimate(no_sold(), [], 2150, 2400, 2650)
    risk = assess_risk(listing(), seed_mv, liq)
    assert any("seed placeholder" in f for f in risk.factors)


def test_risk_buffer_is_capped():
    liq = assess_liquidity("79030N", "TUDOR", no_sold(), 50)
    mv = estimate(no_sold(), [], 2150, 2400, 2650)
    bad = listing(title="Tudor 79030N polished modded aftermarket damaged spares repair",
                  seller_feedback_pct=80.0, seller_feedback_score=2,
                  item_location="Shanghai, CN", condition="",
                  authenticity_guarantee=False, total_acquisition=900.0)
    assert assess_risk(bad, mv, liq).buffer <= 0.30


# --- MAX BUY and scenarios --------------------------------------------------

def test_net_of_selling_costs():
    assert net_of_selling_costs(2400) == pytest.approx(2400 * 0.87 - 25, abs=0.01)


def test_max_buy_anchors_on_market_low_not_high():
    mv = estimate(sold(10), [])
    liq = assess_liquidity("79030N", "TUDOR", sold(10), 2)
    risk = assess_risk(listing(), mv, liq)
    mb = max_buy(mv, risk, 1700.0)
    assert mb.base_expected_resale == mv.market_low
    assert mb.value < mv.market_low


def test_max_buy_does_not_move_with_auction_competition():
    """MAX BUY depends only on resale assumptions, never on the current bid."""
    mv = estimate(sold(10), [])
    liq = assess_liquidity("79030N", "TUDOR", sold(10), 2)
    low_bid = listing(total_acquisition=900.0, buying_format="AUCTION")
    high_bid = listing(total_acquisition=2200.0, buying_format="AUCTION")
    a = max_buy(mv, assess_risk(low_bid, mv, liq), 900.0)
    b = max_buy(mv, assess_risk(high_bid, mv, liq), 2200.0)
    assert b.value == a.value


def test_higher_risk_lowers_max_buy():
    mv = estimate(sold(10), [])
    liq = assess_liquidity("79030N", "TUDOR", sold(10), 2)
    clean = max_buy(mv, assess_risk(listing(), mv, liq), 1700.0)
    risky = max_buy(mv, assess_risk(listing(title="Tudor 79030N polished"),
                                    mv, liq), 1700.0)
    assert risky.value < clean.value


def test_max_buy_none_without_market_value():
    mv = estimate(no_sold(), [])
    liq = assess_liquidity("79030N", "TUDOR", no_sold(), 0)
    mb = max_buy(mv, assess_risk(listing(), mv, liq), 1700.0)
    assert mb.value is None and mb.within_max_buy is False


def test_three_scenarios_are_ordered():
    mv = estimate(sold(10), [])
    liq = assess_liquidity("79030N", "TUDOR", sold(10), 2)
    sc = build_scenarios(mv, liq, 1700.0)
    assert sc["QUICK"].price <= sc["BASE"].price <= sc["PATIENT"].price
    assert sc["QUICK"].gross_profit < sc["PATIENT"].gross_profit
    assert sc["BASE"].days_low < sc["PATIENT"].days_low


def test_scenario_profit_is_net_of_fees():
    mv = estimate(sold(10), [])
    liq = assess_liquidity("79030N", "TUDOR", sold(10), 2)
    sc = build_scenarios(mv, liq, 1700.0)
    assert sc["BASE"].gross_profit == pytest.approx(
        net_of_selling_costs(sc["BASE"].price) - 1700.0, abs=0.01)


def test_scenarios_have_no_day_estimates_without_liquidity():
    mv = estimate(no_sold(), [], 2150, 2400, 2650)
    liq = assess_liquidity("79030N", "TUDOR", no_sold(), 3)
    sc = build_scenarios(mv, liq, 1700.0)
    assert sc["BASE"].days_low is None


# --- verdicts ---------------------------------------------------------------

def test_verdict_buy_with_strong_evidence(conn, ref):
    provider = type("P", (), {"get_recent_sales": lambda self, r, country="GB",
                              period_days=90: sold(10)})()
    l = listing(total_acquisition=1500.0)
    cand = analyse(conn, l, ref, provider, {"v1|1|0": l})
    assert cand.verdict == "BUY"


def test_verdict_watch_when_evidence_is_seed_only(conn, ref):
    l = listing(total_acquisition=1400.0)
    cand = analyse(conn, l, ref, NullSoldProvider(), {"v1|1|0": l})
    assert cand.verdict == "WATCH"
    assert any("seed placeholder" in r for r in cand.verdict_reasons)


def test_verdict_pass_above_max_buy(conn, ref):
    provider = type("P", (), {"get_recent_sales": lambda self, r, country="GB",
                              period_days=90: sold(10)})()
    l = listing(total_acquisition=2300.0)
    cand = analyse(conn, l, ref, provider, {"v1|1|0": l})
    assert cand.verdict == "PASS"
    assert "exceeds MAX BUY" in cand.verdict_reasons[0]


def test_auction_downgraded_to_watch(conn, ref):
    provider = type("P", (), {"get_recent_sales": lambda self, r, country="GB",
                              period_days=90: sold(10)})()
    l = listing(total_acquisition=1500.0, buying_format="AUCTION")
    cand = analyse(conn, l, ref, provider, {"v1|1|0": l})
    assert cand.verdict == "WATCH"


def test_analysis_persists_evidence(conn, ref):
    l = listing(total_acquisition=1500.0)
    analyse(conn, l, ref, NullSoldProvider(), {"v1|1|0": l})
    assert conn.execute("SELECT COUNT(*) c FROM sold_observations").fetchone()["c"] == 1
    row = conn.execute("SELECT * FROM sold_observations").fetchone()
    assert row["observed_sale_count"] is None       # never fabricated
    assert row["coverage_status"] == COVERAGE_UNAVAILABLE


# --- end to end -------------------------------------------------------------

class FakeClient:
    def __init__(self, items):
        self.items = items

    def search(self, query, limit=50, exclusions=()):
        return self.items


def test_full_scan_produces_ranked_report(conn, ref):
    items = [
        {"itemId": "v1|9|0",
         "title": "Tudor Black Bay 58 79030N full set box and papers serviced",
         "price": {"value": "1300.00", "currency": "GBP"},
         "buyingOptions": ["FIXED_PRICE"],
         "seller": {"username": "s1", "feedbackScore": 900,
                    "feedbackPercentage": "99.9"},
         "itemLocation": {"city": "London", "country": "GB"},
         "condition": "Pre-owned"},
    ]
    result = run_scan(conn, [ref], client=FakeClient(items),
                      sold_provider=NullSoldProvider())
    assert result["analysed"]
    rows = report_rows(result)
    assert rows[0]["Reference"] == "79030N"
    assert rows[0]["MAX BUY"] is not None
    assert rows[0]["Observed UK sales"] == "none"
    assert rows[0]["Est. time to sale"] == "unknown"
    run = conn.execute("SELECT * FROM scan_runs WHERE id = ?",
                       (result["run_id"],)).fetchone()
    assert run["deep_analysed"] == 1


def test_report_hides_pass_by_default(conn, ref):
    items = [
        {"itemId": "v1|9|0", "title": "Tudor Black Bay 58 79030N full set",
         "price": {"value": "2300.00", "currency": "GBP"},
         "buyingOptions": ["FIXED_PRICE"], "seller": {"username": "s1"}},
    ]
    result = run_scan(conn, [ref], client=FakeClient(items),
                      sold_provider=NullSoldProvider())
    assert report_rows(result, include_pass=False) == [] or \
        all(r["Verdict"] != "PASS" for r in report_rows(result))
