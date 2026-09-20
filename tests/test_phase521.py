"""Phase 5.2.1 — manual benchmark participation and Top Opportunities ranking."""
from __future__ import annotations

import pathlib

import pytest

from wfs import benchmarks, db
from wfs.active_market import build_benchmark
from wfs.condition import assess_condition
from wfs.config5 import Phase5Config
from wfs.cross_check import (CROSS_CHECK, CrossCheckConfig,
                             discount_to_benchmark_pct, evaluate_cross_check)
from wfs.decision import BUY, PASS, WATCH
from wfs.economics import UK_PRIVATE
from wfs.evidence import from_sold_evidence, score_confidence
from wfs.flip_analysis import (VERDICT_RANK_52, analyse_flip, rank,
                               top_opportunities)
from wfs.risk import score_risk
from wfs.sold_market import COVERAGE_PARTIAL, COVERAGE_UNAVAILABLE, SoldEvidence
from wfs.watchlist import WatchRef

ROOT = pathlib.Path(__file__).resolve().parent.parent
REF = WatchRef("TUDOR", "Black Bay 58", "79030N", 2600, 2750, 2900)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "p521.sqlite3")
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


def active_bench(prices=(2350, 2400, 2450, 2390, 2500, 2420)):
    return build_benchmark("79030N", market_listings(prices), "TUDOR")


def no_sold():
    return SoldEvidence("79030N", 90, "none", COVERAGE_UNAVAILABLE)


def strong_sold(count=14):
    return SoldEvidence("79030N", 90, "manual_sold_evidence", COVERAGE_PARTIAL,
                        count, count, [2700 + i * 10 for i in range(count)])


def wc(conn, value=2310.0, source=None):
    """Record and return a manual WatchCharts benchmark."""
    return benchmarks.record_benchmark(
        conn, "79030N", source or benchmarks.MANUAL_WATCHCHARTS, value,
        brand="TUDOR")


# === 1. discount_to_watchcharts_benchmark_pct ===============================

def test_discount_to_benchmark_calculation():
    # asking 1950 against a 2310 benchmark = 15.6% below
    assert discount_to_benchmark_pct(1950.0, 2310.0) == pytest.approx(15.6, abs=0.05)


def test_discount_to_benchmark_negative_when_asking_is_higher():
    assert discount_to_benchmark_pct(2500.0, 2310.0) == pytest.approx(-8.2, abs=0.1)


def test_discount_to_benchmark_none_without_inputs():
    assert discount_to_benchmark_pct(None, 2310.0) is None
    assert discount_to_benchmark_pct(1950.0, None) is None
    assert discount_to_benchmark_pct(1950.0, 0) is None


def test_analysis_exposes_benchmark_discount(conn):
    a = analyse_flip(listing(price=1950.0), REF, no_sold(), 6,
                     benchmark=active_bench(), manual_benchmark=wc(conn))
    assert a.discount_to_watchcharts_benchmark_pct == pytest.approx(15.6, abs=0.05)
    assert a.benchmark_value == 2310.0
    assert a.as_dict()["discount_to_watchcharts_benchmark_pct"] == pytest.approx(
        15.6, abs=0.05)


# === 2. benchmark materially affects cross-check ranking ====================

def _decision(price, manual=None, prices=(2350, 2400, 2450, 2390, 2500, 2420)):
    bench = active_bench(prices)
    l = listing(price=price)
    ev = from_sold_evidence(no_sold(), "79030N", active_listing_count=len(prices))
    cond = assess_condition(l)
    risk = score_risk(l, cond, bench.active_median_price,
                      score_confidence(ev).score, 0.0)
    return evaluate_cross_check(price, bench, ev, cond, risk,
                                manual_benchmark=manual)


def test_agreeing_benchmark_raises_cross_check_score(conn):
    without = _decision(1950.0)
    with_bench = _decision(1950.0, manual=wc(conn, 2310.0))
    assert with_bench.should_cross_check is True
    assert with_bench.score > without.score
    assert with_bench.benchmarks_agree is True


def test_disagreeing_benchmark_lowers_score_and_warns(conn):
    # A £1,500 benchmark against a £2,410 active median is a 38% divergence.
    without = _decision(1950.0)
    with_bench = _decision(1950.0, manual=wc(conn, 1500.0))
    assert with_bench.benchmarks_agree is False
    assert with_bench.score < without.score
    assert any("benchmarks disagree" in w.lower() for w in with_bench.warnings)
    assert "verify manually" in " ".join(with_bench.warnings).lower()


def test_benchmark_above_asking_is_flagged_as_negative_evidence(conn):
    """If the benchmark says the watch is not cheap, say so."""
    decision = _decision(2450.0, manual=wc(conn, 2400.0))
    assert decision.benchmark_discount_pct is not None
    assert decision.benchmark_discount_pct < 0
    assert any("ABOVE" in w for w in decision.warnings)


def test_benchmark_appears_in_cross_check_summary(conn):
    decision = _decision(1950.0, manual=wc(conn, 2310.0))
    assert "not confirmed sold evidence" in decision.summary.lower()
    assert "2,310" in decision.summary


def test_benchmark_changes_shortlist_ranking(conn):
    """Two identical candidates rank differently once a benchmark corroborates one."""
    corroborated = _decision(1950.0, manual=wc(conn, 2310.0))
    plain = _decision(1950.0)
    assert corroborated.score > plain.score
    ordered = sorted([plain, corroborated], key=lambda d: -d.score)
    assert ordered[0] is corroborated


# === 3. benchmark can NEVER create a BUY ====================================

def test_benchmark_alone_cannot_produce_buy(conn):
    a = analyse_flip(listing(price=1500.0), REF, no_sold(), 6,
                     benchmark=active_bench(), manual_benchmark=wc(conn, 2800.0))
    assert a.verdict != BUY
    assert a.verdict == CROSS_CHECK
    assert a.evidence.has_sold_evidence is False


def test_benchmark_does_not_raise_market_confidence(conn):
    without = analyse_flip(listing(price=1950.0), REF, no_sold(), 6,
                           benchmark=active_bench())
    with_bench = analyse_flip(listing(price=1950.0), REF, no_sold(), 6,
                              benchmark=active_bench(), manual_benchmark=wc(conn))
    assert with_bench.confidence.score == without.confidence.score
    assert with_bench.confidence.score < 60      # below the BUY gate


def test_benchmark_does_not_change_valuation_or_max_buy(conn):
    without = analyse_flip(listing(price=1950.0), REF, strong_sold(), 6,
                           benchmark=active_bench())
    with_bench = analyse_flip(listing(price=1950.0), REF, strong_sold(), 6,
                              benchmark=active_bench(),
                              manual_benchmark=wc(conn, 3500.0))
    assert with_bench.max_buy.standard == without.max_buy.standard
    assert with_bench.net_profit == without.net_profit
    assert with_bench.evidence.valuation_band() == without.evidence.valuation_band()


def test_benchmark_is_never_blended_into_sold_evidence(conn):
    a = analyse_flip(listing(price=1950.0), REF, no_sold(), 6,
                     benchmark=active_bench(), manual_benchmark=wc(conn))
    assert a.evidence.has_sold_evidence is False
    assert a.evidence.sold_price_sample_size == 0
    # The two figures are reported separately, never averaged.
    assert a.benchmark_value == 2310.0
    assert a.benchmark.active_median_price == 2410.0


def test_confirmed_sold_benchmark_is_not_used_as_context(conn):
    """A CONFIRMED_SOLD record must go through the sold path, not this one."""
    sold_bench = benchmarks.record_benchmark(
        conn, "79030N", benchmarks.MANUAL_EBAY_SOLD, 2260, brand="TUDOR")
    decision = _decision(1950.0, manual=sold_bench)
    assert decision.benchmark_value is None


# === 4. stale benchmarks ignored ============================================

def test_stale_benchmark_not_returned_by_store(conn):
    wc(conn, 2310.0)
    conn.execute("UPDATE market_benchmarks SET recorded_at = "
                 "'2020-01-01T00:00:00+00:00'")
    conn.commit()
    assert benchmarks.latest_benchmark(conn, "79030N", max_age_days=120) is None


def test_stale_benchmark_does_not_affect_analysis(conn):
    wc(conn, 2310.0)
    conn.execute("UPDATE market_benchmarks SET recorded_at = "
                 "'2020-01-01T00:00:00+00:00'")
    conn.commit()
    stale = benchmarks.latest_benchmark(conn, "79030N", max_age_days=120)
    baseline = analyse_flip(listing(price=1950.0), REF, no_sold(), 6,
                            benchmark=active_bench())
    with_stale = analyse_flip(listing(price=1950.0), REF, no_sold(), 6,
                              benchmark=active_bench(), manual_benchmark=stale)
    assert with_stale.discount_to_watchcharts_benchmark_pct is None
    assert with_stale.cross_check.score == baseline.cross_check.score


def test_fresh_benchmark_is_returned(conn):
    wc(conn, 2310.0)
    assert benchmarks.latest_benchmark(conn, "79030N") is not None


# === 5. no HTTP introduced ==================================================

def test_benchmark_integration_added_no_http_client():
    import ast as _ast
    tree = _ast.parse((ROOT / "wfs" / "cross_check.py").read_text(encoding="utf-8"))
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            names = [(getattr(node, "module", None) or "")] + \
                    [a.name for a in node.names]
            for n in names:
                assert "httpx" not in n and "requests" not in n and "urllib" not in n


# === 6. Top Flip Opportunities ranking ======================================

def test_verdict_rank_order_is_buy_crosscheck_watch_pass():
    assert VERDICT_RANK_52[BUY] == 0
    assert VERDICT_RANK_52[CROSS_CHECK] == 1
    assert VERDICT_RANK_52[WATCH] == 2
    assert VERDICT_RANK_52[PASS] == 3


def _make(item_id, price, sold, prices=(2350, 2400, 2450, 2390, 2500, 2420)):
    return analyse_flip(listing(item_id, price), REF, sold, len(prices),
                        benchmark=active_bench(prices),
                        config=Phase5Config().with_profile(UK_PRIVATE))


def test_top_opportunities_orders_buy_then_crosscheck_then_watch():
    buy = _make("buy", 1950.0, strong_sold())
    cross = _make("cross", 1950.0, no_sold())
    watch = _make("watch", 2500.0, strong_sold())
    assert buy.verdict == BUY
    assert cross.verdict == CROSS_CHECK
    assert watch.verdict == WATCH

    # Deliberately supplied in the wrong order.
    ordered = top_opportunities([watch, cross, buy])
    assert [a.item_id for a in ordered] == ["buy", "cross", "watch"]


def test_top_opportunities_excludes_pass():
    buy = _make("buy", 1950.0, strong_sold())
    dull = _make("dull", 2400.0, no_sold())       # at the median, not cheap
    assert dull.verdict == PASS
    ordered = top_opportunities([dull, buy])
    assert [a.item_id for a in ordered] == ["buy"]


def test_top_opportunities_uses_ranking_not_discovery_order():
    """The bug this release fixes: the list was displayed in analysis order."""
    weak = _make("weak", 2500.0, strong_sold())    # WATCH
    strong = _make("strong", 1950.0, strong_sold())  # BUY
    discovery_order = [weak, strong]
    ranked = top_opportunities(discovery_order)
    assert ranked[0].item_id == "strong"
    assert [a.item_id for a in ranked] != [a.item_id for a in discovery_order]


def test_top_opportunities_ties_broken_by_flip_score():
    a1 = _make("a1", 1950.0, strong_sold())
    a2 = _make("a2", 2100.0, strong_sold())
    ordered = top_opportunities([a2, a1])
    assert ordered[0].flip_score.score >= ordered[1].flip_score.score


def test_top_opportunities_respects_limit():
    items = [_make(f"i{i}", 1950.0, strong_sold()) for i in range(5)]
    assert len(top_opportunities(items, limit=3)) == 3


def test_default_rank_places_crosscheck_above_watch():
    cross = _make("cross", 1950.0, no_sold())
    watch = _make("watch", 2500.0, strong_sold())
    ordered = rank([watch, cross])
    assert ordered[0].verdict == CROSS_CHECK
    assert ordered[1].verdict == WATCH


def test_app_uses_top_opportunities_helper():
    """The dashboard must call the ranking helper, not slice raw analyses."""
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "top_opportunities(analyses" in source
    assert "top = [a for a in analyses" not in source
