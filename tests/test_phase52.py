"""Phase 5.2 tests — cross-check, direct links, active benchmark, evidence types."""
from __future__ import annotations

import pathlib

import pytest

from wfs import benchmarks, db, market_links, observation
from wfs.active_market import LABEL, build_benchmark
from wfs.condition import assess_condition
from wfs.config5 import Phase5Config
from wfs.cross_check import (CROSS_CHECK, CrossCheckConfig, evaluate_cross_check,
                             shortlist)
from wfs.decision import BUY, PASS, WATCH
from wfs.ebay import normalise_item
from wfs.economics import UK_PRIVATE
from wfs.evidence import from_sold_evidence, score_confidence
from wfs.flip_analysis import analyse_flip, rank
from wfs.risk import score_risk
from wfs.sold_market import COVERAGE_PARTIAL, COVERAGE_UNAVAILABLE, SoldEvidence
from wfs.watchlist import WatchRef

REF = WatchRef("TUDOR", "Black Bay 58", "79030N", 2600, 2750, 2900)
ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "p52.sqlite3")
    db.init_db(c)
    benchmarks.init_benchmark_schema(c)
    return c


def listing(item_id="cand", price=1950.0,
            title="Tudor Black Bay 58 79030N full set box and papers", **kw):
    base = {
        "item_id": item_id, "title": title, "price": price, "shipping": 0.0,
        "total_acquisition": price, "buying_format": "BUY_IT_NOW",
        "condition": "Pre-owned", "seller_feedback_pct": 99.6,
        "seller_feedback_score": 700, "item_location": "London, GB",
        "authenticity_guarantee": True, "returns_accepted": True,
        "image_url": "http://img", "brand": "TUDOR", "model": "Black Bay 58",
        "url": f"https://www.ebay.co.uk/itm/{item_id}",
    }
    base.update(kw)
    return base


def market_listings(prices=(2350, 2400, 2450, 2390, 2500, 2420)):
    return [listing(f"m{i}", p) for i, p in enumerate(prices)]


def no_sold():
    return SoldEvidence("79030N", 90, "none", COVERAGE_UNAVAILABLE)


def strong_sold(count=14):
    return SoldEvidence("79030N", 90, "manual_sold_evidence", COVERAGE_PARTIAL,
                        count, count, [2700 + i * 10 for i in range(count)])


# === 1-3. DIRECT LINKS ======================================================

def test_ebay_url_preserved_from_browse_api():
    """The canonical URL eBay returns must be used verbatim."""
    raw = {"itemId": "v1|123456789012|0", "title": "Tudor 79030N",
           "price": {"value": "1950.00", "currency": "GBP"},
           "buyingOptions": ["FIXED_PRICE"],
           "itemWebUrl": "https://www.ebay.co.uk/itm/123456789012?hash=abc",
           "seller": {"username": "s"}}
    normalised = normalise_item(raw)
    assert normalised["url"] == raw["itemWebUrl"]
    assert market_links.ebay_listing_url(normalised) == raw["itemWebUrl"]


def test_url_not_reconstructed_when_ebay_provides_one():
    l = listing(url="https://www.ebay.co.uk/itm/999?custom=1")
    assert market_links.ebay_listing_url(l) == "https://www.ebay.co.uk/itm/999?custom=1"


def test_url_reconstructed_only_as_last_resort():
    l = listing(item_id="v1|123456789012|0")
    l.pop("url")
    assert market_links.ebay_listing_url(l) == \
        "https://www.ebay.co.uk/itm/123456789012"


def test_candidate_exposes_working_url_field():
    a = analyse_flip(listing(), REF, no_sold(), 6,
                     benchmark=build_benchmark("79030N", market_listings(), "TUDOR"))
    assert a.listing_url and a.listing_url.startswith("https://")
    assert a.as_dict()["url"] == a.listing_url
    assert a.links is not None and a.links.listing == a.listing_url


def test_search_links_use_brand_and_reference_not_the_title():
    noisy = ("Tudor Black Bay 58 79030N 39mm Mens Automatic Dive Watch Box "
             "Papers 2023 MINT RARE")
    links = market_links.build_links(listing(title=noisy), "TUDOR", "79030N",
                                     "Black Bay 58")
    assert links.term == "TUDOR 79030N"
    assert "MINT" not in links.ebay_search and "39mm" not in links.chrono24
    assert "Tudor+79030N" in links.ebay_search.replace("TUDOR", "Tudor")


def test_all_market_links_present_and_absolute():
    links = market_links.build_links(listing(), "TUDOR", "79030N")
    for label, url in links.buttons():
        assert url.startswith("https://"), f"{label} is not absolute"
    labels = [label for label, _ in links.buttons()]
    assert "Open eBay Listing" in labels
    assert "Chrono24 Comparables" in labels
    assert "Check WatchCharts" in labels


def test_sold_search_link_uses_completed_filter():
    url = market_links.ebay_search_url("TUDOR", "79030N", sold=True)
    assert "LH_Sold=1" in url and "LH_Complete=1" in url


def test_search_term_falls_back_to_model_without_reference():
    assert market_links.search_term("TUDOR", None, "Black Bay 58") == \
        "TUDOR Black Bay 58"


# === 4-6. EVIDENCE TYPES CANNOT BE CONFUSED =================================

def test_chrono24_benchmark_is_never_sold_evidence(conn):
    b = benchmarks.record_benchmark(conn, "79030N", benchmarks.MANUAL_CHRONO24, 2520)
    assert b.evidence_kind == benchmarks.KIND_ACTIVE_ASKING
    assert b.is_confirmed_sold is False
    assert b.evidence_level == benchmarks.LEVEL_C


def test_watchcharts_benchmark_is_not_confirmed_sold(conn):
    b = benchmarks.record_benchmark(conn, "79030N", benchmarks.MANUAL_WATCHCHARTS,
                                    2310, notes="market value tab")
    assert b.evidence_kind == benchmarks.KIND_BENCHMARK
    assert b.is_confirmed_sold is False
    assert b.evidence_level == benchmarks.LEVEL_B


def test_manual_ebay_sold_is_confirmed_sold(conn):
    b = benchmarks.record_benchmark(conn, "79030N", benchmarks.MANUAL_EBAY_SOLD, 2260)
    assert b.is_confirmed_sold is True
    assert b.evidence_level == benchmarks.LEVEL_A


def test_evidence_kind_cannot_be_overridden_by_caller():
    """The kind is derived from the source — there is no parameter to abuse."""
    import inspect
    params = inspect.signature(benchmarks.record_benchmark).parameters
    assert "evidence_kind" not in params and "kind" not in params
    assert benchmarks.kind_for_source(benchmarks.MANUAL_CHRONO24) != benchmarks.KIND_SOLD


def test_unknown_benchmark_source_rejected(conn):
    with pytest.raises(ValueError):
        benchmarks.record_benchmark(conn, "79030N", "MADE_UP_SOURCE", 1000)


def test_evidence_source_types_remain_distinct():
    kinds = {benchmarks.kind_for_source(s) for s in
             (benchmarks.MANUAL_EBAY_SOLD, benchmarks.MANUAL_WATCHCHARTS,
              benchmarks.MANUAL_CHRONO24, benchmarks.LOCAL_OBSERVATION)}
    assert len(kinds) == 4       # four sources, four distinct meanings


def test_disappeared_listing_is_not_confirmed_sold(conn):
    item = listing("gone", 2400)
    item["reference"] = "79030N"
    db.upsert_listing(conn, item)
    db.upsert_listing(conn, item)
    db.mark_absent_listings(conn, ["79030N"], set())
    all_obs = observation.observations_for(conn, "79030N")
    assert all_obs, "listing was not recorded against the reference"
    obs = all_obs[0]
    assert obs.status == observation.LIKELY_SOLD
    assert obs.status != "CONFIRMED_SOLD"
    assert "unconfirmed" in obs.rationale.lower()

    ev = from_sold_evidence(
        observation.LocalObservationProvider(conn).get_recent_sales("79030N"),
        "79030N")
    assert ev.has_sold_evidence is False


def test_active_benchmark_is_labelled_not_sold():
    bench = build_benchmark("79030N", market_listings(), "TUDOR")
    assert bench.as_dict()["is_sold_evidence"] is False
    assert "asking" in bench.describe().lower()
    assert "not sales" in bench.describe().lower()
    assert LABEL == "eBay UK Active Asking Market"


# === 7-8. ACTIVE MARKET BENCHMARK AND OUTLIERS ==============================

def test_active_median_calculation():
    bench = build_benchmark("79030N", market_listings((2000, 2400, 2800)), "TUDOR")
    assert bench.active_listing_count == 3
    assert bench.active_median_price == 2400
    assert bench.active_low_price == 2000
    assert bench.active_high_price == 2800
    assert bench.is_usable


def test_discount_to_active_median():
    bench = build_benchmark("79030N", market_listings((2350, 2400, 2450)), "TUDOR")
    # 1900 against a 2400 median = 20.8% below
    assert bench.discount_to_median_pct(1900.0) == pytest.approx(20.8, abs=0.1)


def test_extreme_outliers_excluded():
    prices = (2350, 2400, 2450, 2390, 2500, 2420, 60, 25000)
    bench = build_benchmark("79030N", market_listings(prices), "TUDOR")
    assert bench.active_listing_count == 6
    assert bench.excluded_count == 2
    assert bench.active_low_price >= 2000


def test_accessories_excluded_from_benchmark():
    items = market_listings((2400, 2450, 2350))
    items.append(listing("strap", 90, title="Tudor Black Bay 79030N bracelet only"))
    items.append(listing("box", 60, title="Tudor 79030N box and manual only"))
    bench = build_benchmark("79030N", items, "TUDOR")
    assert bench.active_listing_count == 3
    assert bench.excluded_count == 2
    assert "accessory or parts listing" in bench.exclusion_reasons


def test_benchmark_unusable_below_minimum_sample():
    bench = build_benchmark("79030N", market_listings((2400, 2450)), "TUDOR")
    assert bench.is_usable is False
    assert bench.discount_to_median_pct(1800) is None
    assert "too few" in bench.describe()


def test_benchmark_handles_empty_input():
    bench = build_benchmark("79030N", [], "TUDOR")
    assert bench.is_usable is False
    assert bench.active_median_price is None


# === 9-10. CROSS-CHECK STATUS ===============================================

def _cc(price, prices=(2350, 2400, 2450, 2390, 2500, 2420), **listing_kw):
    bench = build_benchmark("79030N", market_listings(prices), "TUDOR")
    l = listing(price=price, **listing_kw)
    ev = from_sold_evidence(no_sold(), "79030N", active_listing_count=len(prices))
    cond = assess_condition(l)
    risk = score_risk(l, cond, bench.active_median_price,
                      score_confidence(ev).score, 0.0)
    return evaluate_cross_check(price, bench, ev, cond, risk), bench


def test_cross_check_when_attractive_but_no_sold_evidence():
    decision, _ = _cc(1950.0)
    assert decision.should_cross_check is True
    assert decision.discount_pct == pytest.approx(19.1, abs=0.3)
    assert "no confirmed sold evidence" in decision.summary.lower()


def test_cross_check_status_appears_on_the_analysis():
    bench = build_benchmark("79030N", market_listings(), "TUDOR")
    a = analyse_flip(listing(price=1950.0), REF, no_sold(), 6, benchmark=bench)
    assert a.verdict == CROSS_CHECK
    assert a.deterministic_verdict == PASS      # promoted from PASS only


def test_cross_check_is_not_buy():
    bench = build_benchmark("79030N", market_listings(), "TUDOR")
    a = analyse_flip(listing(price=1950.0), REF, no_sold(), 6, benchmark=bench)
    assert a.verdict != BUY
    assert a.confidence.score < 60              # cannot clear the BUY gate
    assert a.evidence.has_sold_evidence is False


def test_modest_discount_does_not_trigger_cross_check():
    decision, _ = _cc(2300.0)
    assert decision.should_cross_check is False
    assert "need" in decision.blockers[0]


def test_implausible_discount_is_a_red_flag_not_an_opportunity():
    decision, _ = _cc(900.0)
    assert decision.should_cross_check is False
    assert any("implausible" in b for b in decision.blockers)


def test_damaged_watch_not_flagged_for_cross_check():
    decision, _ = _cc(1950.0,
                      title="Tudor 79030N cracked crystal spares or repairs")
    assert decision.should_cross_check is False


def test_high_risk_listing_not_flagged_for_cross_check():
    decision, _ = _cc(1950.0, seller_feedback_pct=88.0, seller_feedback_score=2,
                      item_location="Shanghai, CN", authenticity_guarantee=False,
                      returns_accepted=False, image_url=None,
                      title="Tudor 79030N sold as seen no returns")
    assert decision.should_cross_check is False


def test_no_benchmark_means_no_cross_check():
    bench = build_benchmark("79030N", market_listings((2400, 2450)), "TUDOR")
    a = analyse_flip(listing(price=1500.0), REF, no_sold(), 2, benchmark=bench)
    assert a.verdict == PASS
    assert a.cross_check.should_cross_check is False


def test_buy_still_requires_market_confidence():
    """Strong sold evidence still required — CROSS-CHECK never substitutes."""
    bench = build_benchmark("79030N", market_listings(), "TUDOR")
    with_sold = analyse_flip(listing(price=1950.0), REF, strong_sold(), 6,
                             benchmark=bench,
                             config=Phase5Config().with_profile(UK_PRIVATE))
    without = analyse_flip(listing(price=1950.0), REF, no_sold(), 6, benchmark=bench)
    assert with_sold.verdict == BUY
    assert with_sold.confidence.score >= 60
    assert without.verdict == CROSS_CHECK


def test_cross_check_never_overrides_buy_or_watch():
    bench = build_benchmark("79030N", market_listings(), "TUDOR")
    a = analyse_flip(listing(price=1950.0), REF, strong_sold(), 6, benchmark=bench)
    assert a.verdict == BUY                    # not downgraded to CROSS-CHECK
    assert a.cross_check.should_cross_check is True   # would have qualified


# === 11-13. NO PAID DEPENDENCIES ============================================

def test_no_paid_watchcharts_api_dependency():
    """WatchCharts must appear only as a link, never as an API client.

    Checked structurally: no Phase 5.2 module may import an HTTP client or
    reference a credential identifier. Prose in docstrings is ignored, since a
    comment saying "no subscription needed" is not a subscription.
    """
    import ast as _ast
    for name in ("market_links.py", "benchmarks.py", "cross_check.py",
                 "active_market.py"):
        tree = _ast.parse((ROOT / "wfs" / name).read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.Import, _ast.ImportFrom)):
                mod = (getattr(node, "module", None) or "")
                names = [a.name for a in node.names]
                for candidate in [mod, *names]:
                    assert "httpx" not in candidate and "requests" not in candidate, (
                        f"{name} imports an HTTP client — WatchCharts and Chrono24 "
                        "must remain link-only in Phase 5.2")
            # No credential identifiers anywhere in the code itself.
            if isinstance(node, _ast.Name):
                lowered = node.id.lower()
                for banned in ("api_key", "apikey", "bearer", "secret"):
                    assert banned not in lowered, f"{name} references {node.id}"


def test_watchcharts_link_requires_no_credentials():
    url = market_links.watchcharts_search_url("TUDOR", "79030N")
    assert url.startswith("https://watchcharts.com")
    assert "key=" not in url and "token=" not in url


def test_runs_without_any_watchcharts_data(conn):
    bench = build_benchmark("79030N", market_listings(), "TUDOR")
    a = analyse_flip(listing(price=1950.0), REF, no_sold(), 6, benchmark=bench,
                     manual_benchmark=None)
    assert a.verdict == CROSS_CHECK
    assert a.manual_benchmark is None
    assert a.links.watchcharts.startswith("https://")


def test_runs_without_chrono24_provider():
    bench = build_benchmark("79030N", market_listings(), "TUDOR")
    a = analyse_flip(listing(price=1950.0), REF, no_sold(), 6,
                     asking_prices=None, benchmark=bench)
    assert a.verdict in (BUY, WATCH, CROSS_CHECK, PASS)
    assert a.links.chrono24.startswith("https://www.chrono24")


def test_manual_benchmark_recalculates_nothing_silently(conn):
    """A recorded benchmark is stored and surfaced, never folded into sold evidence."""
    benchmarks.record_benchmark(conn, "79030N", benchmarks.MANUAL_WATCHCHARTS, 2310)
    stored = benchmarks.latest_benchmark(conn, "79030N")
    assert stored.evidence_kind == benchmarks.KIND_BENCHMARK
    bench = build_benchmark("79030N", market_listings(), "TUDOR")
    a = analyse_flip(listing(price=1950.0), REF, no_sold(), 6, benchmark=bench,
                     manual_benchmark=stored)
    assert a.evidence.has_sold_evidence is False
    assert a.verdict != BUY


def test_stale_benchmark_is_ignored(conn):
    benchmarks.record_benchmark(conn, "79030N", benchmarks.MANUAL_WATCHCHARTS, 2310)
    conn.execute("UPDATE market_benchmarks SET recorded_at = '2020-01-01T00:00:00+00:00'")
    conn.commit()
    assert benchmarks.latest_benchmark(conn, "79030N", max_age_days=120) is None


# === 14-15. ECONOMICS UNCHANGED =============================================

def test_uk_private_economics_unchanged():
    from wfs.economics import get_profile
    p = get_profile(UK_PRIVATE)
    assert p.transaction_fee_pct == 0.0
    assert p.payment_processing_fee_pct == 0.0
    assert Phase5Config().engine.profile.name == UK_PRIVATE


def test_legacy_pricing_cannot_influence_phase52(monkeypatch):
    from wfs import pricing as legacy
    bench = build_benchmark("79030N", market_listings(), "TUDOR")
    before = analyse_flip(listing(price=1950.0), REF, strong_sold(), 6,
                          benchmark=bench)
    monkeypatch.setattr(legacy, "SELLING_FEE_PCT", 0.95)
    monkeypatch.setattr(legacy, "FIXED_SELLING_COSTS", 9999.0)
    after = analyse_flip(listing(price=1950.0), REF, strong_sold(), 6,
                         benchmark=bench)
    assert after.net_profit == before.net_profit
    assert after.max_buy.standard == before.max_buy.standard
    assert after.verdict == before.verdict


def test_phase52_modules_do_not_import_legacy_pricing():
    import ast as _ast
    for name in ("market_links.py", "active_market.py", "cross_check.py",
                 "benchmarks.py"):
        tree = _ast.parse((ROOT / "wfs" / name).read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                assert (node.module or "").split(".")[-1] != "pricing", name


# === 16. TARGET OFFER =======================================================

def test_target_offer_never_exceeds_max_buy():
    bench = build_benchmark("79030N", market_listings(), "TUDOR")
    for price in (2100.0, 2300.0, 2500.0, 2750.0):
        a = analyse_flip(listing(price=price), REF, strong_sold(), 6,
                         benchmark=bench)
        offer = a.decision.target_offer
        if offer is not None:
            assert offer.high <= a.max_buy.standard
            assert offer.low <= offer.high


def test_asking_offer_and_max_buy_are_distinct():
    bench = build_benchmark("79030N", market_listings(), "TUDOR")
    a = analyse_flip(listing(price=2500.0), REF, strong_sold(), 6, benchmark=bench)
    if a.decision.target_offer:
        assert a.acquisition != a.decision.target_offer.high
        assert a.decision.target_offer.high <= a.max_buy.standard < a.acquisition


# === 17-18. CROSS-CHECK ONLY FOR SHORTLISTED ================================

def test_shortlist_is_capped():
    class Stub:
        def __init__(self, score):
            self.score = score
            self.should_cross_check = True

    pairs = [(i, Stub(float(i))) for i in range(100)]
    config = CrossCheckConfig(max_candidates_per_scan=10)
    kept = shortlist(pairs, config)
    assert len(kept) == 10
    assert kept[0] == 99          # highest score first


def test_pass_listings_do_not_get_flagged():
    decision, _ = _cc(2400.0)     # at the median, not cheap
    assert decision.should_cross_check is False
    assert decision.score == 0.0


def test_only_a_small_subset_is_flagged_in_a_realistic_scan():
    """189 analysed listings must not produce 189 cross-checks."""
    prices = list(range(2300, 2500, 10))          # 20 ordinary listings
    items = market_listings(tuple(prices))
    bench = build_benchmark("79030N", items, "TUDOR")
    flagged = 0
    for l in items:
        a = analyse_flip(l, REF, no_sold(), len(items), benchmark=bench)
        if a.verdict == CROSS_CHECK:
            flagged += 1
    assert flagged == 0, "ordinary mid-market listings must not be flagged"

    cheap = analyse_flip(listing(price=1900.0), REF, no_sold(), len(items),
                         benchmark=bench)
    assert cheap.verdict == CROSS_CHECK


def test_cross_check_thresholds_are_configurable():
    strict = CrossCheckConfig(min_discount_pct=40.0)
    bench = build_benchmark("79030N", market_listings(), "TUDOR")
    a = analyse_flip(listing(price=1950.0), REF, no_sold(), 6, benchmark=bench,
                     cross_check_config=strict)
    assert a.verdict == PASS


# === 19-20. RANKING AND SECURITY ============================================

def test_ranking_places_cross_check_below_buy_and_watch():
    bench = build_benchmark("79030N", market_listings(), "TUDOR")
    buy = analyse_flip(listing("buy", 1950.0), REF, strong_sold(), 6,
                       benchmark=bench)
    cross = analyse_flip(listing("cross", 1950.0), REF, no_sold(), 6,
                         benchmark=bench)
    ordered = rank([cross, buy])
    assert ordered[0].item_id == "buy"
    assert ordered[1].verdict == CROSS_CHECK


def test_sort_by_discount_available():
    from wfs.flip_analysis import SORT_MODES
    assert "Largest Discount to Active Market" in SORT_MODES


def test_no_secrets_in_analysis_output():
    import os
    os.environ["EBAY_CLIENT_SECRET"] = "SUPER-SECRET-VALUE"
    try:
        bench = build_benchmark("79030N", market_listings(), "TUDOR")
        a = analyse_flip(listing(price=1950.0), REF, no_sold(), 6, benchmark=bench)
        blob = repr(a.as_dict()) + "".join(u for _, u in a.links.buttons())
        assert "SUPER-SECRET-VALUE" not in blob
        for token in ("client_secret", "access_token", "authorization"):
            assert token not in blob.lower()
    finally:
        os.environ.pop("EBAY_CLIENT_SECRET", None)


def test_links_contain_no_credentials():
    links = market_links.build_links(listing(), "TUDOR", "79030N")
    for _, url in links.buttons():
        lowered = url.lower()
        for token in ("secret", "token", "apikey", "api_key", "password"):
            assert token not in lowered


def test_version_is_at_least_520():
    """Minimum pin, so a patch release is not a test failure."""
    import tomllib
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    parts = tuple(int(x) for x in data["project"]["version"].split("."))
    assert parts >= (5, 2, 0), f"version {data['project']['version']} predates 5.2.0"
