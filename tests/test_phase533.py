"""Phase 5.3.3 — evidence settings save regression, driven through the real UI.

The reported bug: ticking an optional collector showed "Unsaved changes", but
clicking "Save evidence settings" produced no confirmation and the page still
claimed unsaved, so the setting appeared not to persist.

Two independent faults, both reproduced and fixed:

1. `pending.sources` was rebuilt from the six checkbox rows, while
   `COLLECTION.sources` came from `DEFAULT_ENABLED`, which gained a seventh key
   (`ebay_active_api`) in Phase 5.3.2. The two dicts could never compare equal,
   so "Unsaved changes" was permanent — and the save dropped that key from the
   file.
2. The save ran in the script body followed by `st.rerun()`, which discards the
   current run's output. The success message never reached the browser.

These tests drive the actual Streamlit app with `AppTest`, so they exercise the
callback and session-state logic rather than the persistence layer in isolation.
An earlier suite tested the layer directly and passed while the UI was broken.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from wfs import db
from wfs import evidence_collection as ec

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app.py"
SETTINGS = ROOT / "settings.json"

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

OPTIONAL = [
    (ec.SRC_CHRONO24, "evsrc_chrono24"),
    (ec.SRC_WATCHCHARTS, "evsrc_watchcharts"),
    (ec.SRC_EBAY_SOLD_WEB, "evsrc_ebay_sold_web"),
]


@pytest.fixture
def clean_settings():
    """Run against a pristine settings file and restore whatever was there."""
    original = SETTINGS.read_text(encoding="utf-8") if SETTINGS.exists() else None
    SETTINGS.unlink(missing_ok=True)
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        yield SETTINGS
    finally:
        os.chdir(cwd)
        SETTINGS.unlink(missing_ok=True)
        if original is not None:
            SETTINGS.write_text(original, encoding="utf-8")


def _app():
    # Each AppTest run executes on a new thread; clear the cached resources so a
    # fresh connection is created rather than one bound to a dead thread.
    try:
        import streamlit as st
        st.cache_resource.clear()
    except Exception:
        pass
    at = AppTest.from_file(str(APP), default_timeout=180)
    at.run()
    return at


def _messages(at) -> list[str]:
    return ([i.value for i in at.info] + [s.value for s in at.success]
            + [e.value for e in at.error])


def _unsaved_shown(at) -> bool:
    return any("Unsaved changes" in m for m in _messages(at))


def _saved_shown(at) -> bool:
    return any(m.startswith("Saved. Active sources:") for m in _messages(at))


def _file_sources(path) -> dict:
    return json.loads(path.read_text())["evidence_collection"]["sources"]


# === THE REPORTED BUG =======================================================

@pytest.mark.parametrize("source,widget_key", OPTIONAL)
def test_enable_save_rerun_persists(source, widget_key, clean_settings):
    """Tick → Save → confirmation appears, unsaved clears, file holds it."""
    at = _app()
    assert at.checkbox(key=widget_key).value is False

    at.checkbox(key=widget_key).check().run()
    assert at.checkbox(key=widget_key).value is True
    assert _unsaved_shown(at), "ticking should flag unsaved changes"

    at.button(key="evidence_save_btn").click().run()
    assert not at.exception

    # The two symptoms the user reported.
    assert _saved_shown(at), "no 'Saved' confirmation after clicking Save"
    assert not _unsaved_shown(at), "'Unsaved changes' persisted after saving"

    assert _file_sources(clean_settings)[source] is True


@pytest.mark.parametrize("source,widget_key", OPTIONAL)
def test_setting_survives_a_full_app_restart(source, widget_key, clean_settings):
    at = _app()
    at.checkbox(key=widget_key).check().run()
    at.button(key="evidence_save_btn").click().run()

    restarted = _app()          # a brand new script run, fresh session state
    assert restarted.checkbox(key=widget_key).value is True
    assert not _unsaved_shown(restarted), "a freshly loaded app claims unsaved"


@pytest.mark.parametrize("source,widget_key", OPTIONAL)
def test_disable_save_rerun_also_persists(source, widget_key, clean_settings):
    """Turning a source back off must stick just as reliably."""
    at = _app()
    at.checkbox(key=widget_key).check().run()
    at.button(key="evidence_save_btn").click().run()
    assert _file_sources(clean_settings)[source] is True

    at.checkbox(key=widget_key).uncheck().run()
    at.button(key="evidence_save_btn").click().run()
    assert _saved_shown(at)
    assert _file_sources(clean_settings)[source] is False

    restarted = _app()
    assert restarted.checkbox(key=widget_key).value is False


def test_collector_registry_sees_the_persisted_value(clean_settings, tmp_path):
    """The whole point: the pipeline must act on what was saved."""
    at = _app()
    at.checkbox(key="evsrc_chrono24").check().run()
    at.button(key="evidence_save_btn").click().run()

    # Read back exactly as a scan would, from disk.
    from wfs.settings_store import load_settings
    collection = ec.CollectionSettings.from_dict(
        load_settings(clean_settings).get("evidence_collection"))
    assert collection.is_enabled(ec.SRC_CHRONO24) is True

    conn = db.connect(tmp_path / "reg.sqlite3")
    db.init_db(conn)
    built = {c.name: c.enabled for c in
             ec.CollectorRegistry(conn, collection).build()}
    assert built[ec.SRC_CHRONO24] is True


# === ROOT CAUSE 1: the source map must not lose keys ========================

def test_save_preserves_sources_without_a_checkbox(clean_settings):
    """eBay Active has no checkbox; rebuilding from widgets alone dropped it."""
    at = _app()
    at.checkbox(key="evsrc_chrono24").check().run()
    at.button(key="evidence_save_btn").click().run()

    saved = _file_sources(clean_settings)
    assert ec.SRC_EBAY_ACTIVE in saved, "a non-widget source was dropped on save"
    assert saved[ec.SRC_EBAY_ACTIVE] is True
    for key in ec.DEFAULT_ENABLED:
        assert key in saved, f"{key} missing from the saved source map"


def test_unsaved_indicator_is_false_on_a_clean_load(clean_settings):
    """It was permanently true because the two source maps differed in size."""
    assert not _unsaved_shown(_app())


def test_unsaved_indicator_clears_after_every_optional_source(clean_settings):
    at = _app()
    for _source, widget_key in OPTIONAL:
        at.checkbox(key=widget_key).check().run()
        assert _unsaved_shown(at)
        at.button(key="evidence_save_btn").click().run()
        assert not _unsaved_shown(at), f"{widget_key} left the page claiming unsaved"


# === ROOT CAUSE 2: the save happens in a callback ===========================

def test_save_uses_an_on_click_callback():
    """Saving in the body then calling st.rerun() discarded the confirmation."""
    source = APP.read_text(encoding="utf-8")
    block = source[source.index('key="evidence_save_btn"'):]
    block = block[:block.index("\n\n")]
    assert "on_click=_save_evidence_settings" in block


def test_no_rerun_immediately_after_the_evidence_save():
    """Structural check via AST, so a docstring mentioning st.rerun() is ignored."""
    import ast
    tree = ast.parse(APP.read_text(encoding="utf-8"))

    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_save_evidence_settings":
            target = node
            break
    assert target is not None, "_save_evidence_settings not found"

    for node in ast.walk(target):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", getattr(func, "id", ""))
            assert name != "rerun", (
                "the save callback must not call st.rerun() — it discards the "
                "run's output, which is what hid the confirmation")


def test_save_failure_is_surfaced_not_swallowed():
    source = APP.read_text(encoding="utf-8")
    start = source.index("def _save_evidence_settings")
    end = source.index("pending = _pending_collection_settings()")
    body = source[start:end]
    assert "evidence_save_error" in body
    assert "except Exception" in body


# === OTHER SETTINGS IN THE SAME BLOCK =======================================

def test_master_toggle_persists(clean_settings):
    at = _app()
    at.toggle(key="evidence_master").set_value(False).run()
    at.button(key="evidence_save_btn").click().run()
    data = json.loads(clean_settings.read_text())["evidence_collection"]
    assert data["master_enabled"] is False
    assert _app().toggle(key="evidence_master").value is False


def test_numeric_settings_persist(clean_settings):
    at = _app()
    at.number_input(key="evidence_cap").set_value(9).run()
    at.button(key="evidence_save_btn").click().run()
    data = json.loads(clean_settings.read_text())["evidence_collection"]
    assert data["shortlist_cap"] == 9
    assert _app().number_input(key="evidence_cap").value == 9


def test_multiple_sources_saved_together(clean_settings):
    at = _app()
    for _source, widget_key in OPTIONAL:
        at.checkbox(key=widget_key).check()
    at.run()
    at.button(key="evidence_save_btn").click().run()
    saved = _file_sources(clean_settings)
    for source, _widget_key in OPTIONAL:
        assert saved[source] is True


# === NOTHING ELSE DISTURBED =================================================

def test_seller_profile_untouched_by_an_evidence_save(clean_settings):
    at = _app()
    at.checkbox(key="evsrc_chrono24").check().run()
    at.button(key="evidence_save_btn").click().run()
    data = json.loads(clean_settings.read_text())
    assert data["seller_profile"] == "UK_PRIVATE"
    assert "overrides" in data


def test_no_credentials_written_to_settings(clean_settings, monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "SHOULD-NOT-APPEAR")
    at = _app()
    at.checkbox(key="evsrc_watchcharts").check().run()
    at.button(key="evidence_save_btn").click().run()
    raw = clean_settings.read_text()
    assert "SHOULD-NOT-APPEAR" not in raw
    for token in ("client_secret", "client_id", "access_token"):
        assert token not in raw.lower()


def test_app_runs_without_exception(clean_settings):
    assert not _app().exception
