"""Phase 5.3.1 — evidence settings persistence regression tests.

The bug: in Settings → Automatic Market Evidence, ticking an optional source
showed the public-web warning but "Save evidence settings" went inert, so the
choice was never persisted and the collector pipeline still saw the source as
DISABLED.

Root cause: no widget in that block carried an explicit `key=`, and a
conditionally rendered `st.warning` sat between the checkboxes and the Save
button. Streamlit identifies unkeyed widgets partly by position in the element
tree, so inserting the warning changed the button's identity mid-run.

These tests cover both halves: the persistence layer behaves correctly for every
optional source, and the UI block is structured so the fault cannot recur.
"""
from __future__ import annotations

import ast
import pathlib
import re
import tempfile

import pytest

from wfs import db
from wfs import evidence_collection as ec
from wfs import settings_store as ss

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app.py"

OPTIONAL_SOURCES = [ec.SRC_CHRONO24, ec.SRC_WATCHCHARTS, ec.SRC_EBAY_SOLD_WEB]


@pytest.fixture
def settings_path(tmp_path):
    return tmp_path / "settings.json"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "s531.sqlite3")
    db.init_db(c)
    return c


def _registry_enabled(conn, collection: ec.CollectionSettings) -> dict[str, bool]:
    """What the collector pipeline actually sees."""
    registry = ec.CollectorRegistry(conn, collection)
    return {getattr(c, "name", "?"): bool(getattr(c, "enabled", False))
            for c in registry.build()}


def _save(settings_path, collection: ec.CollectionSettings) -> None:
    """Mimic the app's Save handler against an explicit path."""
    data = dict(ss.load_settings(settings_path))
    data["evidence_collection"] = collection.as_dict()
    ss.save_settings(data, settings_path)


def _load(settings_path) -> ec.CollectionSettings:
    return ec.CollectionSettings.from_dict(
        ss.load_settings(settings_path).get("evidence_collection"))


# === THE HEADLINE REGRESSION ================================================

def test_chrono24_enable_persists_and_reaches_the_collector(settings_path, conn):
    """The exact reported journey, start to finish."""
    # 1. Chrono24 starts disabled.
    initial = _load(settings_path)
    assert initial.is_enabled(ec.SRC_CHRONO24) is False
    assert _registry_enabled(conn, initial)[ec.SRC_CHRONO24] is False

    # 2. The user ticks it and saves.
    pending = ec.CollectionSettings(
        master_enabled=True,
        sources={**initial.sources, ec.SRC_CHRONO24: True},
        shortlist_cap=initial.shortlist_cap,
        lookback_days=initial.lookback_days)
    _save(settings_path, pending)

    # 3. The choice survives a reload (a Streamlit rerun or an app restart).
    reloaded = _load(settings_path)
    assert reloaded.is_enabled(ec.SRC_CHRONO24) is True

    # 4. The collector registry now sees Chrono24 as enabled.
    assert _registry_enabled(conn, reloaded)[ec.SRC_CHRONO24] is True

    # 5. Disabling and saving again also persists.
    off = ec.CollectionSettings(
        master_enabled=True,
        sources={**reloaded.sources, ec.SRC_CHRONO24: False},
        shortlist_cap=reloaded.shortlist_cap,
        lookback_days=reloaded.lookback_days)
    _save(settings_path, off)
    final = _load(settings_path)
    assert final.is_enabled(ec.SRC_CHRONO24) is False
    assert _registry_enabled(conn, final)[ec.SRC_CHRONO24] is False


# === COMMON CAUSE, NOT A CHRONO24 SPECIAL CASE ==============================

@pytest.mark.parametrize("source", OPTIONAL_SOURCES)
def test_every_optional_source_persists_both_ways(source, settings_path, conn):
    """WatchCharts and eBay Sold (web) must behave identically to Chrono24."""
    initial = _load(settings_path)
    assert initial.is_enabled(source) is False

    _save(settings_path, ec.CollectionSettings(
        master_enabled=True, sources={**initial.sources, source: True}))
    on = _load(settings_path)
    assert on.is_enabled(source) is True
    assert _registry_enabled(conn, on)[source] is True

    _save(settings_path, ec.CollectionSettings(
        master_enabled=True, sources={**on.sources, source: False}))
    off = _load(settings_path)
    assert off.is_enabled(source) is False
    assert _registry_enabled(conn, off)[source] is False


def test_enabling_one_source_does_not_disturb_the_others(settings_path):
    initial = _load(settings_path)
    _save(settings_path, ec.CollectionSettings(
        master_enabled=True, sources={**initial.sources, ec.SRC_CHRONO24: True}))
    reloaded = _load(settings_path)
    assert reloaded.is_enabled(ec.SRC_CHRONO24) is True
    assert reloaded.is_enabled(ec.SRC_WATCHCHARTS) is False
    assert reloaded.is_enabled(ec.SRC_EBAY_SOLD_WEB) is False
    # The always-safe sources stay on.
    assert reloaded.is_enabled(ec.SRC_LOCAL_HISTORY) is True


def test_all_optional_sources_can_be_enabled_together(settings_path, conn):
    initial = _load(settings_path)
    _save(settings_path, ec.CollectionSettings(
        master_enabled=True,
        sources={**initial.sources, **{s: True for s in OPTIONAL_SOURCES}}))
    reloaded = _load(settings_path)
    states = _registry_enabled(conn, reloaded)
    assert all(states[s] for s in OPTIONAL_SOURCES)


def test_numeric_settings_persist(settings_path):
    _save(settings_path, ec.CollectionSettings(
        master_enabled=True, sources=dict(ec.DEFAULT_ENABLED),
        shortlist_cap=7, lookback_days=180))
    reloaded = _load(settings_path)
    assert reloaded.shortlist_cap == 7
    assert reloaded.lookback_days == 180


def test_master_switch_persists_and_overrides(settings_path, conn):
    _save(settings_path, ec.CollectionSettings(
        master_enabled=False,
        sources={**ec.DEFAULT_ENABLED, ec.SRC_CHRONO24: True}))
    reloaded = _load(settings_path)
    assert reloaded.master_enabled is False
    # Master off means nothing runs, even a ticked source.
    assert reloaded.is_enabled(ec.SRC_CHRONO24) is False
    assert _registry_enabled(conn, reloaded)[ec.SRC_CHRONO24] is False


def test_seller_profile_survives_an_evidence_save(settings_path):
    """Saving evidence settings must not clobber unrelated settings."""
    ss.save_settings({"seller_profile": "UK_BUSINESS",
                      "overrides": {"shipping_cost_gbp": 20.0}}, settings_path)
    _save(settings_path, ec.CollectionSettings(
        master_enabled=True, sources={**ec.DEFAULT_ENABLED, ec.SRC_CHRONO24: True}))
    data = ss.load_settings(settings_path)
    assert data["seller_profile"] == "UK_BUSINESS"
    assert data["overrides"]["shipping_cost_gbp"] == 20.0
    assert _load(settings_path).is_enabled(ec.SRC_CHRONO24) is True


def test_no_credentials_written_by_an_evidence_save(settings_path):
    ss.save_settings({"seller_profile": "UK_PRIVATE", "overrides": {},
                      "ebay_client_secret": "LEAK-ME"}, settings_path)
    _save(settings_path, ec.CollectionSettings(master_enabled=True))
    raw = settings_path.read_text()
    assert "LEAK-ME" not in raw and "client_secret" not in raw


# === UI STRUCTURE: the fault cannot recur ===================================

def _evidence_settings_block() -> str:
    src = APP.read_text(encoding="utf-8")
    start = src.index('st.subheader("Automatic Market Evidence")\n    st.caption('
                      '"Sources that need no network')
    end = src.index('st.subheader("Worked example with current settings")')
    return src[start:end]


def test_every_widget_in_the_block_is_keyed():
    """The root cause: unkeyed widgets take their identity from position."""
    block = _evidence_settings_block()
    calls = re.findall(r"\.?(checkbox|toggle|number_input|button)\(([^;]*?)\)\n",
                       block, re.S)
    assert calls, "no widgets found — the block moved or was renamed"
    for widget, args in calls:
        assert "key=" in args, f"st.{widget} in the evidence block has no key="


def test_warning_uses_a_fixed_placeholder_not_inline_rendering():
    """A conditional widget between siblings is what broke the Save button."""
    block = _evidence_settings_block()
    assert "public_web_warning = st.empty()" in block
    # The warning is written into the reserved slot, never rendered inline.
    assert "public_web_warning.warning(" in block
    assert not re.search(r"^\s+st\.warning\(", block, re.M)


def test_save_button_is_never_disabled():
    block = _evidence_settings_block()
    save = block[block.index('button("Save evidence settings"'):]
    save = save[:save.index(")\n")]
    assert "disabled" not in save, "the Save button must never be disabled"


def test_save_button_has_a_stable_key():
    assert 'key="evidence_save_btn"' in _evidence_settings_block()


def test_session_state_seeded_once_not_every_rerun():
    """Re-seeding each run would overwrite the user's unsaved ticks."""
    block = _evidence_settings_block()
    assert 'if "evidence_state_loaded" not in st.session_state:' in block


def test_unsaved_changes_are_signalled():
    block = _evidence_settings_block()
    assert "has_changes" in block and "Unsaved changes" in block


def test_all_six_sources_are_offered_in_the_ui():
    block = _evidence_settings_block()
    for source in (ec.SRC_LOCAL_HISTORY, ec.SRC_EBAY_SOLD_STORED,
                   ec.SRC_EBAY_SOLD_INSIGHTS, *OPTIONAL_SOURCES):
        constant = source.upper()
        assert f"SRC_{constant}" in block, f"{source} missing from the settings UI"


def test_app_still_parses():
    ast.parse(APP.read_text(encoding="utf-8"))
