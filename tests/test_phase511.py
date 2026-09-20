"""Phase 5.1.1 — architecture guards and documentation regression tests.

These exist to stop the obsolete Phase 2 fee model creeping back into Phase 5+
code or documentation. They assert no methodology; they assert provenance.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from wfs import pricing as legacy_pricing
from wfs.condition import assess_condition
from wfs.config5 import Phase5Config
from wfs.economics import UK_BUSINESS, UK_PRIVATE, EconomicsEngine, get_profile
from wfs.flip import cost_breakdown
from wfs.flip_analysis import analyse_flip
from wfs.settings_store import DEFAULT_SETTINGS
from wfs.sold_market import COVERAGE_PARTIAL, SoldEvidence
from wfs.watchlist import WatchRef

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Modules that make up the Phase 5+ Flip Intelligence path. None of these may
# depend on the legacy Phase 2 pricing module.
PHASE5_MODULES = [
    "economics.py", "evidence.py", "condition.py", "risk.py", "flip.py",
    "decision.py", "flip_analysis.py", "pipeline5.py", "observation.py",
    "manual_evidence.py", "settings_store.py", "cache.py", "theme.py",
]

LEGACY_MODULE_NAMES = {"pricing"}
LEGACY_FEE_SYMBOLS = {"SELLING_FEE_PCT", "FIXED_SELLING_COSTS",
                      "REQUIRED_PROFIT_MARGIN"}


def _imported_names(path: pathlib.Path) -> set[str]:
    """Structural import extraction via AST — not source string matching."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[-1]
            found.add(module)
            for alias in node.names:
                found.add(f"{module}.{alias.name}")
                found.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[-1])
    return found


# --- architecture guards ----------------------------------------------------

@pytest.mark.parametrize("module_name", PHASE5_MODULES)
def test_phase5_modules_do_not_import_legacy_pricing(module_name):
    """No Phase 5+ module may import the obsolete Phase 2 pricing engine."""
    path = ROOT / "wfs" / module_name
    if not path.exists():
        pytest.skip(f"{module_name} not present")
    imported = _imported_names(path)
    assert not (imported & LEGACY_MODULE_NAMES), (
        f"{module_name} imports the legacy pricing module. Phase 5+ economics "
        "must come from wfs.economics.EconomicsEngine.")


@pytest.mark.parametrize("module_name", PHASE5_MODULES)
def test_phase5_modules_do_not_import_legacy_fee_constants(module_name):
    path = ROOT / "wfs" / module_name
    if not path.exists():
        pytest.skip(f"{module_name} not present")
    leaked = _imported_names(path) & LEGACY_FEE_SYMBOLS
    assert not leaked, (
        f"{module_name} imports legacy fee constant(s) {leaked}. Use the "
        "seller profile on EconomicsEngine instead.")


def test_streamlit_app_does_not_use_legacy_fee_constants():
    """The dashboard must report the active profile, not the Phase 2 flat fee."""
    leaked = _imported_names(ROOT / "app.py") & LEGACY_FEE_SYMBOLS
    assert not leaked, f"app.py imports legacy fee constant(s): {leaked}"


def test_legacy_pricing_module_is_marked_as_legacy():
    doc = (legacy_pricing.__doc__ or "").upper()
    assert "LEGACY" in doc and "PHASE 2" in doc
    assert "ECONOMICSENGINE" in doc.replace(" ", "")


def test_legacy_pricing_still_works_for_the_classic_scan():
    """Marking it legacy must not break Phase 1-4 functionality."""
    assert legacy_pricing.net_of_selling_costs(1000.0) < 1000.0
    from wfs import analysis  # the Phase 1-4 pipeline still depends on it
    assert analysis is not None


# --- EconomicsEngine remains authoritative ----------------------------------

def _sold(count=14):
    return SoldEvidence("79030N", 90, "manual_sold_evidence", COVERAGE_PARTIAL,
                        count, count, [2700 + i * 10 for i in range(count)])


REF = WatchRef("TUDOR", "Black Bay 58", "79030N", 2600, 2750, 2900)


def _listing(**kw):
    base = {
        "item_id": "guard-1",
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


def test_legacy_fee_constants_do_not_affect_flip_intelligence(monkeypatch):
    """Behavioural guard: corrupting the Phase 2 constants must change nothing.

    If any Phase 5 figure moves when the legacy flat fee is set to an absurd
    value, something is still reading the old model.
    """
    before = analyse_flip(_listing(), REF, _sold(), 2,
                          config=Phase5Config().with_profile(UK_PRIVATE))

    monkeypatch.setattr(legacy_pricing, "SELLING_FEE_PCT", 0.95)
    monkeypatch.setattr(legacy_pricing, "FIXED_SELLING_COSTS", 5000.0)
    monkeypatch.setattr(legacy_pricing, "REQUIRED_PROFIT_MARGIN", 0.90)

    after = analyse_flip(_listing(), REF, _sold(), 2,
                         config=Phase5Config().with_profile(UK_PRIVATE))

    assert after.net_profit == before.net_profit
    assert after.max_buy.standard == before.max_buy.standard
    assert after.max_buy.conservative == before.max_buy.conservative
    assert after.base.costs.total == before.base.costs.total
    assert after.velocity.score == before.velocity.score
    assert after.flip_score.score == before.flip_score.score
    assert after.verdict == before.verdict


def test_every_headline_metric_tracks_the_economics_engine():
    """Swapping the seller profile must move all dependent metrics together."""
    private = analyse_flip(_listing(price=2150.0, total_acquisition=2150.0), REF,
                           _sold(), 2, config=Phase5Config().with_profile(UK_PRIVATE))
    business = analyse_flip(_listing(price=2150.0, total_acquisition=2150.0), REF,
                            _sold(), 2, config=Phase5Config().with_profile(UK_BUSINESS))

    assert private.base.costs.total < business.base.costs.total   # exit costs
    assert private.net_profit > business.net_profit               # NET profit
    assert private.net_roi > business.net_roi                     # ROI
    assert private.max_buy.standard > business.max_buy.standard   # MAX BUY
    for name in ("QUICK", "BASE", "PATIENT"):                     # all strategies
        assert private.strategies[name].net_profit > business.strategies[name].net_profit
    assert private.velocity.profit_per_30d > business.velocity.profit_per_30d
    assert private.flip_score.score >= business.flip_score.score


def test_strategy_costs_come_from_the_active_profile():
    cond = assess_condition(_listing())
    private = cost_breakdown(2000.0, cond, Phase5Config().with_profile(UK_PRIVATE))
    business = cost_breakdown(2000.0, cond, Phase5Config().with_profile(UK_BUSINESS))
    assert private.transaction_fee == 0.0
    assert business.transaction_fee > 0.0


# --- UK private seller regression (spec 5.1.1 §7) ---------------------------

def test_uk_private_defaults_remain_zero_fee():
    p = get_profile(UK_PRIVATE)
    assert p.transaction_fee_pct == 0.0
    assert p.payment_processing_fee_pct == 0.0
    assert p.regulatory_operating_fee_pct == 0.0
    assert p.promoted_listing_enabled is False
    assert p.international_sale is False
    assert p.total_percentage_fees == 0.0


def test_uk_private_still_deducts_real_configured_costs():
    engine = EconomicsEngine(get_profile(UK_PRIVATE))
    costs = engine.exit_costs(2000.0, needs_service_reserve=True)
    assert costs.shipping > 0
    assert costs.insurance > 0
    assert costs.packaging > 0
    assert costs.service_reserve > 0
    assert costs.total > 0
    assert costs.net_proceeds < 2000.0


def test_uk_private_zero_fee_can_be_explicitly_overridden():
    p = get_profile(UK_PRIVATE, transaction_fee_pct=0.05)
    assert p.transaction_fee_pct == 0.05
    assert EconomicsEngine(p).exit_costs(1000.0).transaction_fee == pytest.approx(50.0)


def test_uk_private_remains_the_default_profile():
    assert DEFAULT_SETTINGS["seller_profile"] == UK_PRIVATE
    assert get_profile().name == UK_PRIVATE
    assert Phase5Config().engine.profile.name == UK_PRIVATE


# --- documentation regression (spec 5.1.1 §1, §2, §14) ----------------------

CURRENT_DOCS = ["README.md"]
LEGACY_MARKERS = ("LEGACY", "SUPERSEDED", "Phase 5.1", "historical", "no longer")


def _fee_mentions(text: str) -> list[tuple[int, str]]:
    needles = ("12.8", "0.128", "13%", "14.8")
    return [(i + 1, line) for i, line in enumerate(text.splitlines())
            if any(n in line for n in needles)]


@pytest.mark.parametrize("doc", CURRENT_DOCS)
def test_current_docs_do_not_present_legacy_fees_as_default(doc):
    """Any surviving fee mention must be explicitly framed as legacy."""
    path = ROOT / doc
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for lineno, line in _fee_mentions(text):
        window = " ".join(lines[max(0, lineno - 6):lineno + 3])
        assert any(m.lower() in window.lower() for m in LEGACY_MARKERS), (
            f"{doc}:{lineno} mentions a legacy fee without marking it as "
            f"superseded: {line.strip()!r}")


def test_readme_states_the_uk_private_zero_fee_default():
    text = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert "uk private" in text
    assert "transaction" in text and "0%" in text


def test_phase5_doc_marks_its_fee_section_as_superseded():
    text = (ROOT / "PHASE5.md").read_text(encoding="utf-8")
    for lineno, line in _fee_mentions(text):
        window = " ".join(text.splitlines()[max(0, lineno - 12):lineno + 3])
        assert "SUPERSEDED" in window.upper() or "LEGACY" in window.upper(), (
            f"PHASE5.md:{lineno} presents a legacy fee as current: {line.strip()!r}")


def test_marketplace_insights_still_documented_as_optional():
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert "marketplace insights" in readme
    assert "restricted" in readme
    assert "optional" in readme or "does not depend" in readme


def test_readme_documents_the_evidence_hierarchy():
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    for token in ("manual sold", "scanner", "model-family", "asking price",
                  "seed"):
        assert token in readme, f"README does not describe '{token}' evidence"
    assert "seed" in readme and "buy" in readme


def test_version_is_at_least_phase_511():
    """Phase 5.1.1 or later. Pinned by minimum, so a release bump is not a failure."""
    import tomllib
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    parts = tuple(int(x) for x in data["project"]["version"].split("."))
    assert parts >= (5, 1, 1), f"version {data['project']['version']} predates 5.1.1"
