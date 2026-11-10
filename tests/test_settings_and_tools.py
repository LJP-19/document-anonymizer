"""0.43.0: Settings window, Fast mode, Change AI model, Check leftovers, Skip."""

from __future__ import annotations

import os

import pymupdf
import pytest

pytestmark = pytest.mark.usefixtures("isolated_home")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("DOCANON_LLM", "DOCANON_FAST", "DOCANON_CONSERVATIVE", "DOCANON_SECOND_PASS"):
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------------ settings


def test_fast_mode_skips_only_the_optional_extras_and_the_environment_wins(monkeypatch):
    from app import settings
    from app.detection import auditor
    from app.session import second_pass_enabled

    assert not settings.fast_mode() and second_pass_enabled()
    settings.put("fast_mode", True)
    assert settings.fast_mode()
    assert not second_pass_enabled()
    assert auditor.llm_audit_enabled() is False
    monkeypatch.setenv("DOCANON_SECOND_PASS", "1")          # explicit beats the setting
    assert second_pass_enabled()
    monkeypatch.setenv("DOCANON_FAST", "0")
    assert not settings.fast_mode()


def test_careful_mode_is_a_saved_setting_with_the_environment_override(monkeypatch):
    from app import settings
    from app.detection.engine import conservative_mode

    assert not conservative_mode()
    settings.put("careful_mode", True)
    assert conservative_mode()
    monkeypatch.setenv("DOCANON_CONSERVATIVE", "0")
    assert not conservative_mode()


def test_switching_models_forgets_the_loaded_one():
    """_auditor() is cached per process: without reset_model a new choice in
    Settings kept the old model in use until a restart."""
    from app.detection import auditor

    first = auditor._auditor()
    assert auditor._auditor() is first
    auditor.reset_model()
    assert auditor._auditor() is not first


def test_deleting_unused_models_never_touches_the_current_one():
    from app.llm import downloader, selection
    from app.llm.catalog import CATALOG

    current, other = CATALOG[0], CATALOG[1]
    for choice in (current, other):
        for model_file in choice.files:
            path = downloader.file_path(model_file)
            path.touch()
            os.truncate(path, model_file.size_bytes)     # sparse, right size
    selection.set_selected(current)
    assert downloader.downloaded_bytes(other) == other.size_bytes
    freed = downloader.delete_model(other)
    assert freed == other.size_bytes and downloader.downloaded_bytes(other) == 0
    assert downloader.is_downloaded(current) and selection.get_selected() == current


# --------------------------------------------------------------- leftovers


def _pdf(tmp_path, lines) -> str:
    doc = pymupdf.open()
    page = doc.new_page()
    for i, text in enumerate(lines):
        page.insert_text((50, 80 + 30 * i), text, fontsize=11)
    path = tmp_path / "doc.pdf"
    doc.save(str(path))
    return str(path)


def _session(path):
    from app.session import AnonymizationSession

    s = AnonymizationSession(source_path=path)
    s.analyse()
    s.decisions.approve_auto_eligible(s.candidates)
    return s


def test_leftover_check_reports_a_held_name_part_and_nothing_covered(tmp_path):
    s = _session(_pdf(tmp_path, [
        "Taxpayer name: John Smith", "Smith & Wesson makes firearms.",
    ]))
    leftovers = s.find_leftovers()
    assert [(lo.value, lo.name_part) for lo in leftovers] == [("Smith", True)]
    hit = leftovers[0].hits[0]
    assert hit.status == "kept" and "Wesson" in hit.line_text
    # Covered occurrences are never reported.
    assert all(h.status in ("undetected", "kept") for lo in leftovers for h in lo.hits)


def test_leftover_check_finds_a_value_nothing_detected(tmp_path):
    """An occurrence no detector covers (forced here by removing its candidate)
    is reported as NOT detected, and redacting it makes the check come back clean."""
    s = _session(_pdf(tmp_path, [
        "Taxpayer name: Orla Quenneville", "Orla Quenneville signed the return.",
    ]))
    second = [c for c in s.candidates if c.page_no == 0 and "signed" in c.line.text]
    s.detection.candidates = [c for c in s.detection.candidates if c not in second]
    leftovers = s.find_leftovers()
    assert any(h.status == "undetected" and "signed" in h.line_text for lo in leftovers for h in lo.hits)
    for lo in leftovers:
        s.add_manual_text(lo.value)
    assert s.find_leftovers() == []


def test_the_same_identifier_written_without_dashes_is_replaced_automatically(tmp_path):
    """Found by the leftover check: '512487391' on a reference line is the SSN its
    own field confirmed as '512-48-7391'; GLiNER calling the bare digits a ZIP code
    held it for review."""
    s = _session(_pdf(tmp_path, [
        "Taxpayer name: John Smith", "SSN: 512-48-7391",
        "Reference copy for 512487391 on file.",
    ]))
    bare = [c for c in s.candidates if c.text == "512487391"]
    assert bare and all(s.decisions.is_actionable(c) for c in bare)
    assert not any(lo.value in ("512-48-7391", "512487391") for lo in s.find_leftovers())


def test_leftover_check_never_lists_amounts_or_form_text(tmp_path):
    s = _session(_pdf(tmp_path, [
        "Taxpayer name: John Smith", "Wages: $85,000.00", "Taxpayer name: see above",
    ]))
    assert all("85,000" not in lo.value and "Taxpayer" not in lo.value for lo in s.find_leftovers())


# --------------------------------------------------------------------- skip


def test_skip_also_skips_the_recheck_and_says_so(tmp_path, monkeypatch):
    from app.detection import auditor
    from app.session import AnonymizationSession

    monkeypatch.setenv("DOCANON_SECOND_PASS", "1")
    path = _pdf(tmp_path, ["Taxpayer name: John Smith"])
    calls = []
    monkeypatch.setattr(AnonymizationSession, "run_second_pass", lambda self: calls.append(1) or 0)

    AnonymizationSession(source_path=path).analyse()
    assert calls == [1]

    auditor.request_stop()
    s = AnonymizationSession(source_path=path)
    s.analyse()
    assert calls == [1], "Skip pressed: the re-check must not start"
    assert any("re-check" in w and "skipped" in w for w in s.detection.warnings)


# ---------------------------------------------------------------- the window


def _app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_settings_window_saves_fast_mode_and_the_toolbar_follows():
    _app()
    from app import settings
    from app.ui.main_window import MainWindow
    from app.ui.settings_dialog import SettingsDialog

    window = MainWindow()
    dialog = SettingsDialog(window, default_output="/tmp/out")
    try:
        dialog.changed.connect(window._refresh_mode_controls)
        assert dialog.thorough_radio.isChecked() and not window.fast_box.isChecked()
        dialog.fast_radio.click()
        assert settings.get("fast_mode") is True
        assert window.fast_box.isChecked()
        assert not window.ai_review_box.isEnabled()
        window.fast_box.click()                       # and back, from the toolbar
        assert settings.get("fast_mode") is False
    finally:
        dialog.close()
        window.close()


def test_change_model_dialog_starts_on_the_current_model_and_cancel_keeps_it():
    _app()
    from app.llm import downloader, selection
    from app.llm.catalog import CATALOG
    from app.ui.model_picker import ModelPickerDialog

    current = CATALOG[1]
    for model_file in current.files:
        path = downloader.file_path(model_file)
        path.touch()
        os.truncate(path, model_file.size_bytes)
    selection.set_selected(current)
    dialog = ModelPickerDialog(changing=True)
    try:
        assert dialog.windowTitle() == "Change AI model"
        assert dialog._buttons[current.id].isChecked()
        assert "(current)" in dialog._buttons[current.id].text()
        dialog.reject()
        assert selection.get_selected() == current
    finally:
        dialog.close()


def test_macos_gets_a_native_settings_menu_entry(monkeypatch):
    """On a Mac, Settings... belongs in the application menu (Cmd+,): an action with
    the Preferences role, which Qt moves there."""
    _app()
    import sys

    from PySide6.QtGui import QAction

    from app.ui.main_window import MainWindow

    monkeypatch.setattr(sys, "platform", "darwin")
    window = MainWindow()
    try:
        roles = [a.menuRole() for a in window.findChildren(QAction)]
        assert QAction.MenuRole.PreferencesRole in roles
    finally:
        window.close()


def test_final_review_warns_when_something_is_still_readable(tmp_path):
    _app()
    from app.batch import BatchItem, ItemState
    from app.ui.main_window import DocumentTab

    path = _pdf(tmp_path, ["Taxpayer name: John Smith", "Smith & Wesson makes firearms."])
    item = BatchItem(source_path=path)
    item.session = _session(path)
    item.state = ItemState.READY
    tab = DocumentTab(item)
    try:
        tab.populate()
        tab.set_final_review(True)
        assert not tab.final_leftovers.isHidden()
        assert "still readable" in tab.final_leftovers.text()
    finally:
        tab.stop()
        tab.close()
