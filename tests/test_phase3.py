from __future__ import annotations

import json

import pytest

from wfs import ai as ai_module
from wfs import db
from wfs.analysis import analyse, apply_ai
from wfs.asking_market import (COVERAGE_UNAVAILABLE, AskingEvidence,
                               Chrono24WebProvider, ManualAskingProvider,
                               ManualDealerProvider, NullAskingProvider)
from wfs.pipeline import report_rows, run_scan
from wfs.sold_market import COVERAGE_PARTIAL, NullSoldProvider, SoldEvidence
from wfs.watchlist import WatchRef


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "p3.sqlite3")
    db.init_db(c)
    return c


@pytest.fixture
def ref():
    return WatchRef("TUDOR", "Black Bay 58", "79030N", 2150, 2400, 2650)


def sold(count=10):
    prices = [2300, 2350, 2400, 2400, 2450, 2500, 2380, 2420, 2360, 2440][:count]
    return SoldEvidence("79030N", 90, "test", COVERAGE_PARTIAL, count, count + 5, prices)


class FakeSold:
    name = "fake"

    def __init__(self, evidence):
        self.evidence = evidence

    def get_recent_sales(self, reference, country="GB", period_days=90):
        return self.evidence


def listing(**kw):
    base = {
        "item_id": "v1|1|0",
        "title": "Tudor Black Bay 58 79030N 2022 full set box and papers serviced",
        "price": 1500.0, "shipping": 0.0, "total_acquisition": 1500.0,
        "buying_format": "BUY_IT_NOW", "condition": "Pre-owned",
        "seller": "s1", "seller_feedback_pct": 99.8, "seller_feedback_score": 800,
        "item_location": "London, GB", "authenticity_guarantee": True,
        "brand": "TUDOR", "model": "Black Bay 58", "reference": "79030N",
    }
    base.update(kw)
    return base


AI_OK = {
    "reference_verified": True, "likely_reference": "79030N",
    "authenticity_risk": "LOW", "condition_risk": "LOW", "document_risk": "LOW",
    "liquidity_comment": "Moves regularly in the UK.",
    "pricing_comment": "Below observed sold range.",
    "mispricing_reason": "Seller listed without model name in the title.",
    "verdict": "BUY", "confidence": "HIGH", "warnings": [],
}


class FakeAI:
    model = "fake-model"

    def __init__(self, payload=None, raw=None, raises=False):
        self.payload, self.raw, self.raises = payload, raw, raises
        self.calls = 0
        self.last_input = None

    def complete(self, payload):
        self.calls += 1
        self.last_input = payload
        if self.raises:
            raise RuntimeError("API down")
        return self.raw if self.raw is not None else json.dumps(self.payload or AI_OK)


# --- Chrono24 / asking market -----------------------------------------------

def test_null_asking_provider_is_unavailable():
    ev = NullAskingProvider().get_asking_prices("TUDOR", "BB58", "79030N")
    assert ev.coverage_status == COVERAGE_UNAVAILABLE
    assert ev.has_evidence is False


def test_manual_asking_provider_reads_csv(tmp_path):
    p = tmp_path / "chrono24_asking.csv"
    p.write_text(
        "observed_date,reference,price_gbp,seller_type,country,year,condition,box_papers\n"
        "2026-08-20,79030N,2750,dealer,DE,2022,very good,full set\n"
        "2026-08-21,79030N,2850,private,GB,2021,good,watch only\n"
        "2026-08-22,79030N,2650,dealer,IT,2023,very good,full set\n",
        encoding="utf-8")
    ev = ManualAskingProvider(p).get_asking_prices("TUDOR", "BB58", "79030N")
    assert ev.has_evidence
    assert ev.listings_observed == 3
    assert ev.dealer_count == 2 and ev.private_count == 1
    assert ev.asking_range == (2650.0, 2850.0)
    assert set(ev.countries) == {"DE", "GB", "IT"}


def test_stale_asking_observations_ignored(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text(
        "observed_date,reference,price_gbp,seller_type,country,year,condition,box_papers\n"
        "2020-01-01,79030N,2750,dealer,DE,2022,,\n"
        "2020-01-02,79030N,2850,dealer,DE,2022,,\n", encoding="utf-8")
    assert not ManualAskingProvider(p).get_asking_prices("T", "M", "79030N").has_evidence


def test_asking_data_is_labelled_never_as_sold(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text(
        "observed_date,reference,price_gbp,seller_type,country,year,condition,box_papers\n"
        "2026-08-20,79030N,2750,dealer,DE,,,\n"
        "2026-08-21,79030N,2850,dealer,DE,,,\n", encoding="utf-8")
    text = ManualAskingProvider(p).get_asking_prices("T", "M", "79030N").describe()
    assert "CHRONO24 ACTIVE ASKING DATA" in text
    assert "asking prices, not sales" in text
    assert "sold" not in text.lower().replace("not sales", "")


def test_chrono24_web_disabled_by_default():
    ev = Chrono24WebProvider().get_asking_prices("TUDOR", "BB58", "79030N")
    assert ev.coverage_status == COVERAGE_UNAVAILABLE
    assert "disabled" in ev.notes


def test_chrono24_web_fails_open_on_error():
    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("network gone")

    provider = Chrono24WebProvider(enabled=True, client=Boom())
    provider._robots_allow = lambda url: True
    ev = provider.get_asking_prices("TUDOR", "BB58", "79030N")
    assert ev.coverage_status == COVERAGE_UNAVAILABLE
    assert "unavailable" in ev.notes.lower()


def test_chrono24_respects_robots_disallow():
    provider = Chrono24WebProvider(enabled=True)
    provider._robots_allow = lambda url: False
    ev = provider.get_asking_prices("TUDOR", "BB58", "79030N")
    assert ev.coverage_status == COVERAGE_UNAVAILABLE
    assert "robots" in ev.notes.lower()


def test_chrono24_price_parsing_discards_outliers():
    provider = Chrono24WebProvider(enabled=True)
    prices = provider.parse_prices("<div>£2,750</div><span>£12</span><b>£2,850</b>")
    assert prices == [2750.0, 2850.0]


def test_scan_continues_when_chrono24_unavailable(conn, ref):
    l = listing()
    cand = analyse(conn, l, ref, FakeSold(sold()), {"v1|1|0": l},
                   chrono24_provider=Chrono24WebProvider())
    assert cand.verdict in {"BUY", "WATCH", "PASS"}
    assert cand.chrono24.has_evidence is False


def test_asking_data_shifts_market_estimate(conn, ref, tmp_path):
    p = tmp_path / "c.csv"
    p.write_text(
        "observed_date,reference,price_gbp,seller_type,country,year,condition,box_papers\n"
        "2026-08-20,79030N,3100,dealer,DE,,,\n"
        "2026-08-21,79030N,3200,dealer,DE,,,\n"
        "2026-08-22,79030N,3300,dealer,IT,,,\n", encoding="utf-8")
    l = listing()
    without = analyse(conn, l, ref, FakeSold(sold()), {"v1|1|0": l})
    with_asking = analyse(conn, l, ref, FakeSold(sold()), {"v1|1|0": l},
                          chrono24_provider=ManualAskingProvider(p))
    assert with_asking.market.market_mid > without.market.market_mid
    # ...but sold evidence still dominates at 70%.
    assert with_asking.market.weights["SOLD_EBAY_UK"] > 0.6


def test_dealer_provider_uses_its_own_label(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text(
        "observed_date,reference,price_gbp,seller_type,country,year,condition,box_papers\n"
        "2026-08-20,79030N,2900,dealer,GB,,,\n"
        "2026-08-21,79030N,2950,dealer,GB,,,\n", encoding="utf-8")
    ev = ManualDealerProvider(p).get_asking_prices("T", "M", "79030N")
    assert "DEALER" in ev.label
    assert ev.has_evidence


def test_asking_observations_persisted(conn, ref, tmp_path):
    p = tmp_path / "c.csv"
    p.write_text(
        "observed_date,reference,price_gbp,seller_type,country,year,condition,box_papers\n"
        "2026-08-20,79030N,2750,dealer,DE,,,\n"
        "2026-08-21,79030N,2850,dealer,DE,,,\n", encoding="utf-8")
    l = listing()
    analyse(conn, l, ref, FakeSold(sold()), {"v1|1|0": l},
            chrono24_provider=ManualAskingProvider(p))
    rows = conn.execute(
        "SELECT * FROM market_observations WHERE kind = 'ASKING'").fetchall()
    assert len(rows) == 1
    assert rows[0]["sample_size"] == 2


# --- AI layer ---------------------------------------------------------------

def test_ai_payload_contains_no_instruction_to_reprice(conn, ref):
    l = listing()
    cand = analyse(conn, l, ref, FakeSold(sold()), {"v1|1|0": l})
    payload = ai_module.build_payload(cand)
    assert payload["deterministic_analysis"]["max_buy_is_fixed"] is True
    assert payload["deterministic_analysis"]["max_buy_gbp"] == cand.maxbuy.value


def test_ai_validate_rejects_missing_fields():
    with pytest.raises(ValueError):
        ai_module.validate({"verdict": "BUY"})


def test_ai_validate_rejects_bad_verdict():
    bad = dict(AI_OK, verdict="STRONG BUY")
    with pytest.raises(ValueError):
        ai_module.validate(bad)


def test_ai_validate_treats_unreadable_risk_as_high():
    out = ai_module.validate(dict(AI_OK, authenticity_risk="probably fine"))
    assert out["authenticity_risk"] == "HIGH"


def test_ai_validate_strips_attempted_repricing():
    out = ai_module.validate(dict(AI_OK, max_buy_gbp=9999, suggested_max_buy=8888))
    assert "max_buy_gbp" not in out and "suggested_max_buy" not in out


def test_ai_extracts_json_from_fenced_response(conn, ref):
    l = listing()
    cand = analyse(conn, l, ref, FakeSold(sold()), {"v1|1|0": l})
    client = FakeAI(raw="```json\n" + json.dumps(AI_OK) + "\n```")
    result = ai_module.analyse_candidate(cand, client=client)
    assert result.ok and result.verdict == "BUY"


def test_ai_failure_is_non_fatal(conn, ref):
    l = listing()
    cand = analyse(conn, l, ref, FakeSold(sold()), {"v1|1|0": l})
    apply_ai(conn, cand, client=FakeAI(raises=True))
    assert cand.ai.ok is False
    assert cand.verdict == cand.deterministic_verdict
    assert "unavailable" in cand.ai_notes[0]


def test_ai_can_downgrade(conn, ref):
    l = listing()
    cand = analyse(conn, l, ref, FakeSold(sold()), {"v1|1|0": l})
    assert cand.deterministic_verdict == "BUY"
    apply_ai(conn, cand, client=FakeAI(dict(AI_OK, verdict="PASS",
                                            authenticity_risk="HIGH")))
    assert cand.verdict == "PASS"
    assert "downgraded" in cand.ai_notes[0]


def test_ai_cannot_upgrade(conn, ref):
    """An AI BUY must not override a deterministic WATCH."""
    l = listing(total_acquisition=1400.0)
    cand = analyse(conn, l, ref, NullSoldProvider(), {"v1|1|0": l})
    assert cand.deterministic_verdict == "WATCH"
    apply_ai(conn, cand, client=FakeAI(AI_OK))
    assert cand.verdict == "WATCH"
    assert "never applied" in cand.ai_notes[0]


def test_ai_never_alters_max_buy(conn, ref):
    l = listing()
    cand = analyse(conn, l, ref, FakeSold(sold()), {"v1|1|0": l})
    before = cand.maxbuy.value
    apply_ai(conn, cand, client=FakeAI(dict(AI_OK, max_buy_gbp=99999)))
    assert cand.maxbuy.value == before


def test_ai_result_persisted(conn, ref):
    l = listing()
    cand = analyse(conn, l, ref, FakeSold(sold()), {"v1|1|0": l})
    apply_ai(conn, cand, client=FakeAI(AI_OK), scan_run_id=None)
    row = conn.execute("SELECT * FROM ai_analysis").fetchone()
    assert row["verdict"] == "BUY"
    assert row["model"] == "fake-model"
    assert json.loads(row["payload_json"])["likely_reference"] == "79030N"


def test_reconcile_reports_agreement():
    ok = ai_module.AIResult(True, "m", AI_OK)
    verdict, notes = ai_module.reconcile("BUY", ok)
    assert verdict == "BUY" and "agrees" in notes[0]


# --- cost control and end to end --------------------------------------------

class FakeClient:
    def __init__(self, items):
        self.items = items

    def search(self, query, limit=50, exclusions=()):
        return self.items


def _items(n: int) -> list[dict]:
    return [
        {"itemId": f"v1|{i}|0",
         "title": "Tudor Black Bay 58 79030N full set box and papers serviced",
         "price": {"value": "1300.00", "currency": "GBP"},
         "buyingOptions": ["FIXED_PRICE"],
         "seller": {"username": f"s{i}", "feedbackScore": 900,
                    "feedbackPercentage": "99.9"},
         "itemLocation": {"city": "London", "country": "GB"},
         "condition": "Pre-owned"}
        for i in range(n)
    ]


def test_ai_calls_are_capped(conn, ref, monkeypatch):
    monkeypatch.setattr("wfs.pipeline.SETTINGS.max_ai_candidates_per_scan", 3)
    client = FakeAI(AI_OK)
    result = run_scan(conn, [ref], client=FakeClient(_items(8)),
                      sold_provider=FakeSold(sold()), ai_client=client)
    assert len(result["analysed"]) == 8
    assert client.calls == 3
    assert result["ai_calls"] == 3


def test_no_ai_calls_when_disabled(conn, ref):
    result = run_scan(conn, [ref], client=FakeClient(_items(3)),
                      sold_provider=FakeSold(sold()), use_ai=False)
    assert result["ai_calls"] == 0
    assert all(c.ai is None for c in result["analysed"])


def test_ai_budget_spent_on_best_candidates_first(conn, ref, monkeypatch):
    monkeypatch.setattr("wfs.pipeline.SETTINGS.max_ai_candidates_per_scan", 1)
    items = _items(1) + [{
        "itemId": "v1|99|0", "title": "Tudor Black Bay 58 79030N",
        "price": {"value": "2300.00", "currency": "GBP"},
        "buyingOptions": ["FIXED_PRICE"], "seller": {"username": "s9"}}]
    client = FakeAI(AI_OK)
    result = run_scan(conn, [ref], client=FakeClient(items),
                      sold_provider=FakeSold(sold()), ai_client=client)
    analysed_with_ai = [c for c in result["analysed"] if c.ai is not None]
    assert len(analysed_with_ai) == 1
    assert analysed_with_ai[0].deterministic_verdict == "BUY"


def test_scan_summary_reports_usage(conn, ref):
    result = run_scan(conn, [ref], client=FakeClient(_items(2)),
                      sold_provider=FakeSold(sold()), ai_client=FakeAI(AI_OK))
    run = conn.execute("SELECT * FROM scan_runs WHERE id = ?",
                       (result["run_id"],)).fetchone()
    assert run["listings_scanned"] == 2
    assert run["deep_analysed"] == 2
    assert run["ai_calls"] == 2


def test_report_includes_phase3_columns(conn, ref):
    result = run_scan(conn, [ref], client=FakeClient(_items(1)),
                      sold_provider=FakeSold(sold()), ai_client=FakeAI(AI_OK))
    row = report_rows(result)[0]
    assert row["AI verdict"] == "BUY"
    assert row["AI confidence"] == "HIGH"
    assert row["Chrono24 asking"] == "n/a"
