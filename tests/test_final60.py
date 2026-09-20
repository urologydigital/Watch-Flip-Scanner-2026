"""Final 6.0 — evidence hierarchy, matching, comparables, control cases.

The three control cases (spec §24 G/H/I) are the ones that matter most: they run
the REAL pipeline end to end and assert that a good watch reaches BUY, a bad one
does not, and asking prices alone cannot manufacture confidence.
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from wfs import db, evidence_bridge, evidence_store
from wfs import evidence_collection as ec
from wfs.collectors import product_research as pr
from wfs.collectors.base import (BEST_OFFER_UNCERTAIN, CONFIRMED_PRICE,
                                 EV_ACTIVE_ASKING, EV_MARKET_CONTEXT, EV_SOLD,
                                 HEALTH_BLOCKED, HEALTH_NOT_CONFIGURED,
                                 HEALTH_USER_ACTION, MATCH_EXACT, MATCH_FAMILY,
                                 MATCH_MATERIAL, MATCH_REJECTED, MATCH_VARIANT,
                                 MATCH_WEAK, PRICE_UNKNOWN, CollectedEvidence,
                                 classify_match)
from wfs.collectors.watchcharts_api import WatchChartsAPICollector
from wfs.comparables import filter_comparables
from wfs.config5 import EVIDENCE_REFERENCE_EXACT, EVIDENCE_SEED
from wfs.decision import BUY, PASS, WATCH
from wfs.evidence import score_confidence
from wfs.pipeline5 import run_flip_scan
from wfs.watchlist import WatchRef

ROOT = pathlib.Path(__file__).resolve().parent.parent
REF = WatchRef("TUDOR", "Black Bay 58", "79030N", 2600, 2750, 2900)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "final60.sqlite3")
    db.init_db(c)
    evidence_store.migrate(c)
    return c


def sold_record(price=2700.0, date="2026-09-01", item_id="s1",
                title="Tudor Black Bay 58 79030N full set",
                source="ebay_product_research", certainty=CONFIRMED_PRICE,
                match=MATCH_EXACT):
    return CollectedEvidence(
        source=source, evidence_type=EV_SOLD, reference="79030N",
        brand="TUDOR", model="Black Bay 58", title=title,
        price_gbp=price, original_price=price, sale_date=date,
        item_id=item_id, price_certainty=certainty, match_type=match)


class FakeEbay:
    """Browse API stand-in. Returns active listings only — never sold data."""

    def __init__(self, prices, title="Tudor Black Bay 58 79030N full set "
                                     "box and papers serviced"):
        self.prices = prices
        self.title = title
        self.calls = 0

    def search(self, query, limit=50, exclusions=()):
        self.calls += 1
        return [{"itemId": f"v1|{i}|0", "title": self.title,
                 "price": {"value": str(p), "currency": "GBP"},
                 "buyingOptions": ["FIXED_PRICE"],
                 "seller": {"username": "trusted_seller", "feedbackScore": 1400,
                            "feedbackPercentage": "99.9"},
                 "itemLocation": {"city": "London", "country": "GB"},
                 "condition": "Pre-owned", "image": {"imageUrl": "http://img"},
                 "itemWebUrl": f"https://www.ebay.co.uk/itm/{i}"}
                for i, p in enumerate(self.prices)]


# === §7 REFERENCE MATCHING ==================================================

def test_exact_reference():
    assert classify_match("Tudor Black Bay 58 79030N full set", "79030N",
                          "Black Bay 58", "TUDOR") == MATCH_EXACT


def test_high_confidence_variant_extends_our_reference():
    assert classify_match("Omega Aqua Terra 220.10.38.20.03.001",
                          "220.10.38.20", "Aqua Terra 38", "OMEGA") == MATCH_VARIANT


def test_different_variant_is_rejected_not_family():
    """79030B is a different watch, even though the model name matches."""
    assert classify_match("Tudor Black Bay 58 79030B blue dial", "79030N",
                          "Black Bay 58", "TUDOR") == MATCH_REJECTED


def test_model_family_when_no_reference():
    assert classify_match("Tudor Black Bay 58 automatic diver", "79030N",
                          "Black Bay 58", "TUDOR") == MATCH_FAMILY


def test_weak_match_brand_only():
    assert classify_match("Tudor mens automatic watch", "79030N",
                          "Black Bay 58", "TUDOR") == MATCH_WEAK


def test_unrelated_rejected():
    assert classify_match("Casio F91W digital", "79030N",
                          "Black Bay 58", "TUDOR") == MATCH_REJECTED


def test_only_strong_matches_are_material():
    assert MATCH_EXACT in MATCH_MATERIAL and MATCH_VARIANT in MATCH_MATERIAL
    assert MATCH_FAMILY not in MATCH_MATERIAL
    assert MATCH_WEAK not in MATCH_MATERIAL


# === §8 COMPARABLE FILTERING WITH AUDIT =====================================

@pytest.mark.parametrize("title,code", [
    ("Tudor 79030N strap only", "STRAP_ONLY"),
    ("Tudor 79030N empty box only", "BOX_ONLY"),
    ("Tudor 79030N for parts not working", "PARTS_ONLY"),
    ("Tudor 79030N replica homage", "REPLICA"),
    ("Tudor 79030N aftermarket dial modded", "AFTERMARKET"),
    ("Tudor 79030N watch head only", "HEAD_ONLY"),
    ("Tudor 79030N job lot of 3 watches", "BULK_LOT"),
])
def test_dirty_comparables_excluded_with_reason(title, code):
    audit = filter_comparables([sold_record(title=title)], "79030N",
                               "TUDOR", "Black Bay 58")
    assert audit.included == []
    assert audit.excluded[0].code == code
    assert audit.excluded[0].reason


def test_wrong_reference_excluded():
    audit = filter_comparables(
        [sold_record(title="Tudor Black Bay 58 79030B blue")], "79030N",
        "TUDOR", "Black Bay 58")
    assert audit.excluded[0].code == "WRONG_REFERENCE"


def test_clean_comparables_included():
    records = [sold_record(2680, item_id="a"), sold_record(2700, item_id="b"),
               sold_record(2720, item_id="c")]
    audit = filter_comparables(records, "79030N", "TUDOR", "Black Bay 58")
    assert len(audit.included) == 3
    assert audit.exclusion_counts() == {}


def test_price_outlier_excluded_with_explanation():
    records = [sold_record(2680, item_id="a"), sold_record(2700, item_id="b"),
               sold_record(2720, item_id="c"), sold_record(2690, item_id="d"),
               sold_record(15000, item_id="e")]
    audit = filter_comparables(records, "79030N", "TUDOR", "Black Bay 58")
    excluded = [d for d in audit.excluded if d.code == "PRICE_OUTLIER"]
    assert len(excluded) == 1
    assert "median" in excluded[0].reason


def test_audit_is_fully_inspectable():
    audit = filter_comparables(
        [sold_record(2700, item_id="a"),
         sold_record(title="Tudor 79030N strap only", item_id="b")],
        "79030N", "TUDOR", "Black Bay 58")
    rows = audit.as_rows()
    assert len(rows) == 2
    for row in rows:
        assert "included" in row and "reason" in row and "match_label" in row
    assert "excluded" in audit.summary()


def test_filter_never_excludes_everything_on_outliers():
    records = [sold_record(1000 * (i + 1), item_id=f"x{i}") for i in range(5)]
    audit = filter_comparables(records, "79030N", "TUDOR", "Black Bay 58")
    assert audit.included, "filtering must not discard every comparable"


# === §3B PRODUCT RESEARCH ===================================================

CAPTURE = """Title\tSold Price\tAsking Price\tDate Sold\tCondition\tBest Offer\tItem ID
Tudor Black Bay 58 79030N full set box and papers\t£2,680.00\t£2,750.00\t2026-09-01\tPre-owned\tNo\t101
Tudor Black Bay 58 79030N 2023\t£2,720.00\t£2,800.00\t2026-09-05\tPre-owned\tNo\t102
Tudor Black Bay 58 79030N mint\t£2,650.00\t£2,700.00\t2026-08-20\tPre-owned\tYes\t103
Tudor Black Bay 58 79030B blue\t£2,900.00\t£2,950.00\t2026-09-02\tPre-owned\tNo\t104
Tudor Black Bay 79030N bracelet only\t£320.00\t£350.00\t2026-09-03\tUsed\tNo\t105
"""


def test_product_research_url_uses_brand_and_reference():
    url = pr.research_url("Tudor", "79030N", "Black Bay 58")
    assert "Tudor+79030N" in url
    assert "sh/research" in url
    assert "EBAY-GB" in url


def test_product_research_parses_and_rejects_correctly():
    report = pr.parse_capture(CAPTURE, "79030N", "TUDOR", "Black Bay 58")
    assert len(report.accepted) == 3          # two priced + one Best Offer
    assert len(report.rejected) == 2          # wrong reference + bracelet only
    reasons = " ".join(r.reason for r in report.rejected)
    assert "reference" in reasons and "Excluded comparable" in reasons


def test_best_offer_price_is_never_invented():
    report = pr.parse_capture(CAPTURE, "79030N", "TUDOR", "Black Bay 58")
    offers = [r for r in report.accepted
              if r.price_certainty == BEST_OFFER_UNCERTAIN]
    assert offers
    for row in offers:
        assert row.best_offer_price_gbp is None
        assert "asking price" in row.reason


def test_product_research_is_level_a(conn):
    pr.import_capture(conn, CAPTURE, "79030N", "TUDOR", "Black Bay 58")
    evidence, _agg = evidence_bridge.build_market_evidence(
        conn, "79030N", 4, brand="TUDOR", model="Black Bay 58")
    assert evidence.evidence_level == EVIDENCE_REFERENCE_EXACT
    assert evidence.has_sold_evidence is True


def test_product_research_captures_asking_and_dates(conn):
    report = pr.parse_capture(CAPTURE, "79030N", "TUDOR", "Black Bay 58")
    first = report.accepted[0]
    assert first.original_asking_price_gbp == 2750.0
    assert first.sale_date == "2026-09-01"


def test_product_research_module_has_no_network_client():
    """It must never fetch, log in or touch a session."""
    import ast
    tree = ast.parse((ROOT / "wfs" / "collectors" / "product_research.py")
                     .read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [(getattr(node, "module", None) or "")] + \
                    [a.name for a in node.names]
            for name in names:
                assert "httpx" not in name and "requests" not in name
                assert "playwright" not in name


def test_product_research_never_mentions_cookies_or_passwords():
    text = (ROOT / "wfs" / "collectors" / "product_research.py").read_text().lower()
    for token in ("cookie_jar", "set_cookie", "password=", "session_id="):
        assert token not in text


def test_product_research_collector_requests_user_action(conn):
    from wfs.collectors.product_research import ProductResearchCollector
    result = ProductResearchCollector(conn).collect("79030N", "TUDOR")
    assert result.status.status == HEALTH_USER_ACTION
    assert "research page" in result.status.error


def test_future_dated_sale_rejected():
    bad = ("Title\tSold Price\tDate Sold\n"
           "Tudor Black Bay 58 79030N\t£2,700.00\t2099-01-01\n")
    report = pr.parse_capture(bad, "79030N", "TUDOR", "Black Bay 58")
    assert report.accepted[0].sale_date is None


# === PRICE PARSING — regression for a factor-of-ten truncation bug ==========
# The original pattern [0-9]{1,3}(?:,[0-9]{3})* matched only the first three
# digits when no thousands separator was present, silently turning £2650.00
# into £265.00. Every valuation built on such a record was wrong by 10x.

@pytest.mark.parametrize("text,expected", [
    ("£2,650.00", 2650.0),
    ("£2650.00", 2650.0),      # the bug: previously 265.0
    ("2650", 2650.0),          # the bug: previously 265.0
    ("£12,450.00", 12450.0),
    ("£12450", 12450.0),
    ("£950", 950.0),
    ("£195.50", 195.5),
])
def test_prices_parse_without_truncation(text, expected):
    assert pr.parse_price(text) == expected


def test_ebay_web_parser_handles_unseparated_prices():
    from wfs.collectors.ebay_sold import EbaySoldWebCollector
    html = ('<li class="s-item"><div class="s-item__title">Tudor Black Bay 58 '
            '79030N</div><span>£2650.00</span></li>')
    records = EbaySoldWebCollector(enabled=True).parse(html, "79030N", "TUDOR")
    assert records[0].price_gbp == 2650.0


def test_watchcharts_parser_handles_unseparated_prices():
    from wfs.collectors.watchcharts import WatchChartsCollector
    records = WatchChartsCollector(enabled=True).parse(
        "<div>Market value £2650</div>", "79030N")
    assert records[0].price_gbp == 2650.0


def test_captured_prices_survive_the_round_trip(conn):
    """End to end: a £2,650 sale must still be £2,650 after storage."""
    capture = ("Title\tSold Price\tDate Sold\tItem ID\n"
               "Tudor Black Bay 58 79030N full set\t£2650.00\t2026-09-01\tr1\n")
    pr.import_capture(conn, capture, "79030N", "TUDOR", "Black Bay 58")
    stored = evidence_store.load_evidence(conn, "79030N")
    assert stored[0].price_gbp == 2650.0


# === §4 WATCHCHARTS API =====================================================

def test_watchcharts_not_configured_without_key(monkeypatch):
    monkeypatch.delenv("WFS_WATCHCHARTS_API_KEY", raising=False)
    result = WatchChartsAPICollector(enabled=True).collect("79030N", "Tudor")
    assert result.status.status == HEALTH_NOT_CONFIGURED
    assert "key not configured" in result.status.error


def test_watchcharts_rejected_key_is_blocked(monkeypatch):
    monkeypatch.setenv("WFS_WATCHCHARTS_API_KEY", "planted-test-key")

    class Rejects:
        def get(self, *a, **kw):
            class R:
                status_code = 403
            return R()

    result = WatchChartsAPICollector(
        enabled=True, http_client=Rejects()).collect("79030N", "Tudor")
    assert result.status.status == HEALTH_BLOCKED


def test_watchcharts_is_level_b_context(monkeypatch):
    monkeypatch.setenv("WFS_WATCHCHARTS_API_KEY", "planted-test-key")

    class Responds:
        def get(self, *a, **kw):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"data": {"market_value": 2750, "currency": "GBP",
                                     "reference": "79030N"}}
            return R()

    result = WatchChartsAPICollector(
        enabled=True, http_client=Responds()).collect("79030N", "Tudor")
    assert len(result.records) == 1
    record = result.records[0]
    assert record.evidence_type == EV_MARKET_CONTEXT
    assert record.evidence_type != EV_SOLD
    assert record.usable_for_valuation is False     # never a confirmed sale


def test_watchcharts_key_never_appears_in_a_url(monkeypatch):
    monkeypatch.setenv("WFS_WATCHCHARTS_API_KEY", "planted-test-key")
    captured = {}

    class Capturing:
        def get(self, url, params=None, headers=None, **kw):
            captured["url"] = url
            captured["params"] = params or {}
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {}
            return R()

    WatchChartsAPICollector(enabled=True,
                            http_client=Capturing()).collect("79030N", "Tudor")
    assert "planted-test-key" not in captured["url"]
    assert "planted-test-key" not in repr(captured["params"])


def test_watchcharts_unknown_currency_skipped(monkeypatch):
    monkeypatch.setenv("WFS_WATCHCHARTS_API_KEY", "planted-test-key")
    collector = WatchChartsAPICollector(enabled=True, http_client=object())
    assert collector.parse({"data": {"market_value": 400000,
                                     "currency": "JPY"}}, "79030N") == []


# === §9 ADAPTIVE WEIGHTING ==================================================

def test_asking_prices_cannot_inflate_a_sold_estimate():
    from wfs.evidence_bridge import blended_valuation

    class Agg:
        effective_sample = 6.0

    blended = blended_valuation(Agg(), (2600, 2700, 2800),
                                asking_prices=[3400, 3500, 3600, 3700],
                                independent_value=3300)
    assert blended.market_mid <= 2700
    assert blended.capped_by_sold is True


def test_thin_sold_sample_loses_weight():
    from wfs.evidence_bridge import blended_valuation

    strong = blended_valuation(type("A", (), {"effective_sample": 6.0})(),
                               (2600, 2700, 2800), asking_prices=[2400, 2450])
    thin = blended_valuation(type("A", (), {"effective_sample": 1.0})(),
                             (2600, 2700, 2800), asking_prices=[2400, 2450])
    assert (thin.weights["confirmed_sold"]
            < strong.weights["confirmed_sold"])


def test_no_evidence_yields_no_valuation():
    from wfs.evidence_bridge import blended_valuation
    blended = blended_valuation(type("A", (), {"effective_sample": 0.0})(),
                                (None, None, None))
    assert blended.market_mid is None


# === §24 G — KNOWN-GOOD CONTROL CASE ========================================

def test_control_good_candidate_reaches_buy(conn):
    """Real pipeline, genuine Level A fixture evidence, clearly profitable."""
    capture = "Title\tSold Price\tDate Sold\tBest Offer\tItem ID\n" + "".join(
        f"Tudor Black Bay 58 79030N full set\t£{2650 + i * 20}.00\t"
        f"2026-09-{(i % 28) + 1:02d}\tNo\tg{i}\n" for i in range(10))
    pr.import_capture(conn, capture, "79030N", "TUDOR", "Black Bay 58")

    result = run_flip_scan(conn, [REF], client=FakeEbay([1850, 2600, 2650]),
                           use_ai=False)
    analyses = {a.listing["price"]: a for a in result["analyses"]}
    candidate = analyses[1850.0]

    assert candidate.verdict == BUY, candidate.decision.why
    assert candidate.evidence.has_sold_evidence is True
    assert candidate.confidence.score >= 60
    assert candidate.max_buy.standard is not None
    assert candidate.acquisition <= candidate.max_buy.standard
    # All three scenarios present, ordered, and profitable.
    quick, base, patient = (candidate.strategies["QUICK"],
                            candidate.strategies["BASE"],
                            candidate.strategies["PATIENT"])
    assert quick.sale_price <= base.sale_price <= patient.sale_price
    assert base.net_profit > 0 and base.net_roi_pct > 0
    assert base.days is not None


# === §24 H — KNOWN-BAD CONTROL CASE =========================================

def test_control_bad_candidate_never_buys(conn):
    """Overpriced against the same solid evidence, plus condition risk."""
    capture = "Title\tSold Price\tDate Sold\tBest Offer\tItem ID\n" + "".join(
        f"Tudor Black Bay 58 79030N full set\t£{2650 + i * 20}.00\t"
        f"2026-09-{(i % 28) + 1:02d}\tNo\tb{i}\n" for i in range(10))
    pr.import_capture(conn, capture, "79030N", "TUDOR", "Black Bay 58")

    result = run_flip_scan(
        conn, [REF],
        client=FakeEbay([3200], title="Tudor Black Bay 58 79030N polished "
                                      "aftermarket bezel no papers"),
        use_ai=False)
    candidate = result["analyses"][0]
    assert candidate.verdict != BUY
    assert candidate.decision.gates


# === §24 I — ASKING-ONLY CONTROL CASE =======================================

def test_control_asking_only_cannot_produce_confident_buy(conn):
    """Attractive active asking prices, zero Level A evidence."""
    evidence_store.store_evidence(conn, [
        CollectedEvidence(source="chrono24", evidence_type=EV_ACTIVE_ASKING,
                          reference="79030N", price_gbp=3400 + i * 50,
                          original_price=3400 + i * 50,
                          price_certainty=PRICE_UNKNOWN, match_type=MATCH_EXACT,
                          item_id=f"c24-{i}")
        for i in range(6)])

    result = run_flip_scan(conn, [REF], client=FakeEbay([1900, 3300, 3400]),
                           use_ai=False)
    for candidate in result["analyses"]:
        assert candidate.verdict != BUY, (
            "asking prices alone must never produce a BUY")
        assert candidate.confidence.score < 60
        assert candidate.evidence.has_sold_evidence is False


def test_chrono24_evidence_can_never_be_sold(conn):
    evidence_store.store_evidence(conn, [
        CollectedEvidence(source="chrono24", evidence_type=EV_ACTIVE_ASKING,
                          reference="79030N", price_gbp=3000,
                          price_certainty=PRICE_UNKNOWN, match_type=MATCH_EXACT,
                          item_id="c1")])
    records = evidence_store.load_evidence(conn, "79030N")
    for record in records:
        if record.source == "chrono24":
            assert record.evidence_type == EV_ACTIVE_ASKING
            assert record.is_sold is False
            assert record.usable_for_valuation is False


def test_seed_values_alone_cannot_buy(conn):
    result = run_flip_scan(conn, [REF], client=FakeEbay([900]), use_ai=False)
    candidate = result["analyses"][0]
    assert candidate.verdict != BUY
    assert candidate.evidence.evidence_level in (EVIDENCE_SEED, "NONE",
                                                 "ASKING_ONLY",
                                                 "OBSERVED_ACTIVITY")


# === §24 F — END TO END =====================================================

def test_end_to_end_pipeline_produces_a_complete_candidate(conn):
    capture = "Title\tSold Price\tDate Sold\tBest Offer\tItem ID\n" + "".join(
        f"Tudor Black Bay 58 79030N full set\t£{2650 + i * 20}.00\t"
        f"2026-09-{(i % 28) + 1:02d}\tNo\te{i}\n" for i in range(9))
    pr.import_capture(conn, capture, "79030N", "TUDOR", "Black Bay 58")

    result = run_flip_scan(conn, [REF], client=FakeEbay([1900, 2600]),
                           use_ai=False)
    assert result["analyses"]
    candidate = result["analyses"][0]

    # Every stage of the documented pipeline produced something.
    assert candidate.ref.reference == "79030N"
    assert candidate.evidence.valuation_band()[1]
    assert candidate.liquidity is not None
    assert candidate.max_buy.standard is not None
    assert set(candidate.strategies) == {"QUICK", "BASE", "PATIENT"}
    assert candidate.decision.why
    assert candidate.seller_mode == "UK Private Seller"
    assert candidate.listing_url


def test_scan_survives_every_external_source_failing(conn):
    class Boom:
        def collect_for_references(self, *a, **kw):
            raise RuntimeError("all sources down")

    result = run_flip_scan(conn, [REF], client=FakeEbay([2400]),
                           use_ai=False, registry=Boom())
    assert result["analyses"], "a total collector failure must not lose the scan"


# === §20 DATABASE SAFETY ====================================================

def test_backup_created_before_migration(tmp_path):
    path = tmp_path / "live.sqlite3"
    conn = db.connect(path)
    db.init_db(conn)
    db.upsert_listing(conn, {"item_id": "keep", "title": "Tudor 79030N",
                             "price": 2400.0, "reference": "79030N"})
    before = db.table_row_counts(conn)

    backup = db.backup_database(path, backup_dir=tmp_path / "db_backups")
    assert backup is not None and backup.exists()

    evidence_store.migrate(conn)
    after = db.table_row_counts(conn)
    for table, count in before.items():
        assert after[table] >= count, f"{table} lost rows during migration"


def test_backup_returns_none_without_a_database(tmp_path):
    assert db.backup_database(tmp_path / "nothing.sqlite3") is None


def test_row_counts_cover_every_table(conn):
    from wfs.manual_evidence import init_manual_schema
    init_manual_schema(conn)
    counts = db.table_row_counts(conn)
    for table in ("listings", "collected_evidence", "source_health",
                  "manual_sold_evidence", "scan_runs"):
        assert table in counts


# === §19 STATUSES ===========================================================

def test_all_final_statuses_exist():
    from wfs.collectors import base
    for name in ("HEALTH_OK", "HEALTH_REUSED", "HEALTH_NOT_CONFIGURED",
                 "HEALTH_NOT_RUN", "HEALTH_UNAVAILABLE", "HEALTH_BLOCKED",
                 "HEALTH_USER_ACTION", "HEALTH_ERROR", "HEALTH_STALE"):
        assert hasattr(base, name), f"{name} missing"


def test_every_status_has_a_plain_english_explanation():
    from wfs.collectors.base import HEALTH_EXPLANATION
    for status, text in HEALTH_EXPLANATION.items():
        assert len(text) > 10, f"{status} has no useful explanation"


def test_enabled_is_not_the_same_as_operational(conn):
    """A ticked checkbox must not make a source look healthy."""
    settings = ec.CollectionSettings(
        master_enabled=True,
        sources={**ec.DEFAULT_ENABLED, ec.SRC_WATCHCHARTS_API: True})
    registry = ec.CollectorRegistry(conn, settings)
    states = {c.name: c for c in registry.build()}
    watchcharts = states[ec.SRC_WATCHCHARTS_API]
    assert watchcharts.enabled is True
    assert watchcharts.is_available() is False      # enabled, not operational


def test_new_sources_registered(conn):
    names = {c.name for c in ec.CollectorRegistry(conn).build()}
    assert ec.SRC_PRODUCT_RESEARCH in names
    assert ec.SRC_WATCHCHARTS_API in names


# === §23 SECURITY ===========================================================

def test_no_secrets_in_evidence_or_health(conn, monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "PLANTED-SECRET")
    monkeypatch.setenv("WFS_WATCHCHARTS_API_KEY", "PLANTED-KEY")
    pr.import_capture(conn, CAPTURE, "79030N", "TUDOR", "Black Bay 58")
    run_flip_scan(conn, [REF], client=FakeEbay([2400]), use_ai=False)

    blob = repr(evidence_store.load_evidence(conn, "79030N"))
    blob += repr(ec.source_health_report(conn))
    blob += repr(ec.CollectorRegistry(conn).preflight())
    assert "PLANTED-SECRET" not in blob
    assert "PLANTED-KEY" not in blob


def test_version_is_six_zero():
    import tomllib
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"].startswith("6.0")
