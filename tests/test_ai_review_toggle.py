"""A persistent "AI review" switch, and the order in which things decide.

Precedence: DOCANON_LLM (explicit) > the user's switch in the window > "on iff a
downloaded model is ready". The switch can only turn the review off or leave it
to the default - it can never claim a model exists.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.usefixtures("isolated_home")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("DOCANON_LLM", raising=False)
    monkeypatch.delenv("DOCANON_LLM_BUDGET_SECONDS", raising=False)


def _select_a_model():
    from app.llm import downloader, selection
    from app.llm.catalog import CATALOG

    choice = CATALOG[0]
    path = downloader.model_path(choice)
    path.touch()
    os.truncate(path, choice.size_bytes)  # right size; never loaded by these tests
    selection.set_selected(choice)


# ------------------------------------------------------------ settings.py


def test_settings_round_trip_and_survive_a_damaged_file(isolated_home):
    from app import settings

    assert settings.get("ai_review") is None
    settings.put("ai_review", False)
    assert settings.get("ai_review") is False

    path = isolated_home / ".document-anonymizer" / "settings.json"
    path.write_text("{this is not json", encoding="utf-8")
    assert settings.load() == {} and settings.get("ai_review", "unset") == "unset"
    settings.put("ai_review", True)  # a damaged file must not block saving
    assert json.loads(path.read_text(encoding="utf-8")) == {"ai_review": True}


def test_settings_writes_leave_no_temporary_file_behind(isolated_home):
    from app import settings

    settings.put("ai_review", False)
    folder = isolated_home / ".document-anonymizer"
    assert [p.name for p in folder.iterdir() if p.suffix == ".tmp"] == []


def test_the_switch_is_stored_on_disk_so_it_survives_a_restart(isolated_home):
    from app import settings

    settings.put("ai_review", False)
    on_disk = json.loads((isolated_home / ".document-anonymizer" / "settings.json").read_text())
    assert on_disk["ai_review"] is False


# ------------------------------------------------------------- precedence


def test_nothing_is_on_without_a_model_whatever_the_switch_says():
    from app import settings
    from app.detection.auditor import llm_audit_enabled

    assert llm_audit_enabled() is False
    settings.put("ai_review", True)
    assert llm_audit_enabled() is False, "ON means 'use it when ready', never 'pretend'"


def test_a_ready_model_turns_it_on_and_the_switch_turns_it_off():
    pytest.importorskip("llama_cpp")
    from app import settings
    from app.detection.auditor import llm_audit_enabled

    _select_a_model()
    assert llm_audit_enabled() is True
    settings.put("ai_review", False)
    assert llm_audit_enabled() is False
    settings.put("ai_review", True)
    assert llm_audit_enabled() is True


def test_the_environment_variable_beats_the_switch_in_both_directions(monkeypatch):
    pytest.importorskip("llama_cpp")
    from app import settings
    from app.detection.auditor import llm_audit_enabled

    _select_a_model()
    settings.put("ai_review", False)
    monkeypatch.setenv("DOCANON_LLM", "1")
    assert llm_audit_enabled() is True
    settings.put("ai_review", True)
    monkeypatch.setenv("DOCANON_LLM", "0")
    assert llm_audit_enabled() is False


# ------------------------------------------------------------- the window


def _window():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from app.ui.main_window import MainWindow

    return MainWindow()


def test_the_checkbox_shows_the_real_state_and_saves_each_change():
    pytest.importorskip("llama_cpp")
    from app import settings
    from app.detection.auditor import llm_audit_enabled

    _select_a_model()
    window = _window()
    try:
        box = window.ai_review_box
        assert box.isEnabled() and box.isChecked()

        box.click()
        assert not box.isChecked()
        assert settings.get("ai_review") is False
        assert llm_audit_enabled() is False

        box.click()
        assert box.isChecked()
        assert settings.get("ai_review") is True
        assert llm_audit_enabled() is True
    finally:
        window.close()


def test_a_saved_off_is_what_a_new_window_shows():
    pytest.importorskip("llama_cpp")
    from app import settings

    _select_a_model()
    settings.put("ai_review", False)
    window = _window()
    try:
        assert window.ai_review_box.isEnabled() and not window.ai_review_box.isChecked()
    finally:
        window.close()


def test_with_no_model_the_checkbox_is_off_and_says_why():
    window = _window()
    try:
        box = window.ai_review_box
        assert not box.isEnabled() and not box.isChecked()
        assert "No AI model" in box.toolTip()
    finally:
        window.close()


def test_when_the_environment_decides_the_checkbox_is_locked_and_says_so(monkeypatch):
    monkeypatch.setenv("DOCANON_LLM", "0")
    window = _window()
    try:
        box = window.ai_review_box
        assert not box.isEnabled() and not box.isChecked()
        assert "DOCANON_LLM" in box.toolTip()
    finally:
        window.close()
