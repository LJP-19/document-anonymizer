"""Headless GUI smoke tests for the tabbed batch workspace."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DOCANON_LLM", "0")

pytest.importorskip("PySide6")

# PySide6 under the offscreen platform crashes during widget teardown on roughly
# 40% of runs (SIGBUS/SIGABRT), after the assertions have passed. Unresolved -
# see README "Known defects". Marked so CI can gate on the engine suite while
# these still run and report.
pytestmark = pytest.mark.gui

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.batch import ItemState  # noqa: E402
from app.decisions.manager import DecisionState  # noqa: E402
from app.ui.main_window import DocumentTab, MainWindow  # noqa: E402
from tests import fixtures  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    # Close anything still open so no render thread outlives the interpreter.
    for widget in app.topLevelWidgets():
        widget.close()
    app.processEvents()


def _window(tmp_path, count: int = 2) -> MainWindow:
    window = MainWindow()
    window.output_edit.setText(str(tmp_path / "out"))
    sources = []
    for index in range(count):
        sources.append(fixtures.form_pdf(tmp_path / f"doc{index}.pdf"))
    items = window.batch.add_files(sources)
    for item in items:
        tab = DocumentTab(item)
        tab.changed.connect(window._refresh_queue)
        window.tabs.addTab(tab, item.name)
        window.tabs_by_item[item.source_path] = tab
        window.batch.analyse(item)
        tab.populate()
    window._refresh_queue()
    return window


def test_one_tab_per_document(qapp, tmp_path):
    window = _window(tmp_path, 3)
    assert window.tabs.count() == 3
    assert all(isinstance(window.tabs.widget(i), DocumentTab) for i in range(3))
    window.close()


def test_approving_a_tab_puts_it_in_the_queue(qapp, tmp_path):
    window = _window(tmp_path)
    assert not window.process_button.isEnabled()
    window.tabs.widget(0).approve()
    assert window.batch.approved
    assert window.process_button.isEnabled()
    assert "1 approved" in window.queue_label.text()
    assert window.tabs.tabText(0).startswith("\u2713")
    window.close()


def test_dismissing_a_tab_keeps_it_out_of_the_queue(qapp, tmp_path):
    window = _window(tmp_path)
    window.tabs.widget(0).dismiss()
    assert not window.batch.approved
    assert "dismissed" in window.queue_label.text()
    window.close()


def test_only_approved_documents_are_written(qapp, tmp_path):
    window = _window(tmp_path)
    tab = window.tabs.widget(0)
    tab.session.decisions.set_state(tab.session.candidates, DecisionState.ACCEPTED)
    tab.approve()
    done = window.batch.process_approved()
    assert len(done) == 1
    assert len(list((tmp_path / "out").glob("*.pdf"))) == 1
    window.close()


def test_results_view_lists_changes_and_previews_the_output(qapp, tmp_path):
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab.session.decisions.set_state(tab.session.candidates, DecisionState.ACCEPTED)
    tab.approve()
    done = window.batch.process_approved()
    window._on_processed(done)

    assert window.stack.currentWidget() is window.results
    assert window.results.table.rowCount() > 0
    headers = [window.results.table.horizontalHeaderItem(i).text() for i in range(4)]
    assert headers == ["Type", "Original", "Pseudonym", "Pages"]
    originals = {window.results.table.item(r, 1).text() for r in range(window.results.table.rowCount())}
    assert "John Smith" in originals
    assert window.results.preview.views, "no redacted preview rendered"
    window.close()


def test_start_over_clears_tabs_but_not_written_files(qapp, tmp_path):
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab.session.decisions.set_state(tab.session.candidates, DecisionState.ACCEPTED)
    tab.approve()
    window.batch.process_approved()
    written = list((tmp_path / "out").glob("*.pdf"))
    assert written

    window.batch.clear()
    for index in reversed(range(window.tabs.count())):
        window.tabs.removeTab(index)
    window.tabs_by_item = {}
    window._refresh_queue()

    assert window.tabs.count() == 0
    assert list((tmp_path / "out").glob("*.pdf")) == written
    window.close()


def test_reviewed_items_move_to_the_done_tab(qapp, tmp_path):
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab.set_filter("all")
    group = tab.cards[0].group
    tab.set_filter("reviewed")
    assert not tab.cards
    tab.mark_reviewed(group, True)
    tab.set_filter("reviewed")
    keys = [(c.group.pii_type, c.group.normalized) for c in tab.cards]
    assert (group.pii_type, group.normalized) in keys
    window.close()


def test_output_folder_is_customizable(qapp, tmp_path):
    window = MainWindow()
    window.output_edit.setText(str(tmp_path / "custom"))
    assert window.batch.output_folder == str(tmp_path / "custom")
    window.output_edit.setText("")
    assert window.batch.output_folder is None
    window.close()


def test_zoom_defaults_to_one_hundred_percent(qapp, tmp_path):
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    assert tab.zoom == 1.0 and tab.zoom_slider.value() == 100
    window.close()


def test_stopping_a_runner_twice_is_safe(qapp):
    """Shutdown calls stop() from several places; it must be idempotent."""
    from app.ui.widgets import TaskRunner, stop_all_runners
    from PySide6.QtWidgets import QWidget

    parent = QWidget()
    runner = TaskRunner(parent)
    runner.run(lambda: 1, lambda _r: None, lambda _e: None)
    runner.stop()
    runner.stop()
    stop_all_runners()
    parent.close()


def test_every_runner_is_registered_for_shutdown(qapp, tmp_path):
    """A thread Qt destroys while running aborts the process, not just the test."""
    from app.ui.widgets import _RUNNERS

    window = _window(tmp_path, 1)
    assert any(r is window.analysis_task for r in _RUNNERS)
    tab = window.tabs.widget(0)
    assert any(r is tab.preview_task for r in _RUNNERS)
    window.close()
    assert tab.preview_task._thread is None


def test_stop_is_safe_on_a_thread_qt_already_destroyed(qapp):
    """Regression: unconditional wait() on a half-destroyed QThread segfaulted."""
    import shiboken6
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QWidget

    from app.ui.widgets import TaskRunner

    parent = QWidget()
    runner = TaskRunner(parent)
    runner.run(lambda: 1, lambda _r: None, lambda _e: None)
    thread = runner._thread
    assert thread is not None
    runner.stop()
    assert runner._thread is None

    # Simulate Qt having reclaimed the C++ object, then stop again.
    runner._thread = thread
    if shiboken6.isValid(thread):
        thread.deleteLater()
        qapp.processEvents()
    runner.stop()
    parent.close()


def test_repeated_shutdown_never_raises(qapp, tmp_path):
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    for _ in range(3):
        tab.stop()
        window.close()
    assert tab.preview_task._thread is None


def test_concurrent_rendering_is_serialised(qapp, tmp_path):
    """Regression: MuPDF called from several threads segfaulted intermittently.

    Roughly one run in three crashed with SIGSEGV or SIGBUS. The same race
    would hit a user rendering a large document while a preview was still in
    flight, so this is a correctness fix, not a test workaround.
    """
    import threading

    from app.session import AnonymizationSession

    source = fixtures.form_pdf(tmp_path / "race.pdf")
    session = AnonymizationSession(source_path=source)
    session.analyse()

    errors: list = []

    def render() -> None:
        try:
            for _ in range(6):
                session.preview_originals([0], 1.0)
                session.preview_transformed_pages([0], 1.0)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=render) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert not errors, errors


def test_both_decision_buttons_stay_clickable(qapp, tmp_path):
    """Regression: Redact was disabled whenever the item was already redacting,
    which is the default for everything - so it could never be pressed."""
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab.set_filter("all")
    card = tab.cards[0]
    assert card.redact_button.isEnabled()
    assert card.keep_button.isEnabled()
    card.decided.emit(card.group, DecisionState.SKIPPED)
    tab.set_filter("all")
    for c in tab.cards:
        assert c.redact_button.isEnabled() and c.keep_button.isEnabled()
    window.close()


def test_deciding_moves_the_item_to_done_without_a_checkbox(qapp, tmp_path):
    """The checkbox was busywork: pressing Redact or Keep IS reviewing it."""
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab.set_filter("all")
    group = tab.cards[0].group
    assert not hasattr(tab.cards[0], "reviewed_box")

    tab.decide_group(group, DecisionState.ACCEPTED)
    tab.set_filter("reviewed")
    keys = [(c.group.pii_type, c.group.normalized) for c in tab.cards]
    assert (group.pii_type, group.normalized) in keys
    window.close()


def test_a_failing_render_reports_instead_of_leaving_a_blank_page(qapp, tmp_path):
    """Regression: render errors were discarded, so the preview showed only
    page headers with no explanation."""
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab._on_render_error("original", "Boom: something failed")
    assert "Could not render" in tab.left_canvas._empty.text()
    window.close()


def test_a_worker_failure_does_not_kill_the_process(qapp):
    """Regression: the app exited by itself during processing."""
    from PySide6.QtWidgets import QWidget

    from app.ui.widgets import TaskRunner

    parent = QWidget()
    runner = TaskRunner(parent)
    seen: list = []

    def boom():
        raise MemoryError("simulated")

    runner.run(boom, lambda _r: None, seen.append)
    for _ in range(50):
        qapp.processEvents()
        if seen:
            break
    assert seen and "MemoryError" in seen[0]
    runner.stop()
    parent.close()


def test_add_missed_item_finds_and_adds_it(qapp, tmp_path):
    """Requested: text / replace with / cascade / apply to same."""
    from app.detection.types import PiiType

    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    added = tab.session.add_manual_text(
        "Annual Salary", replacement="Yearly Pay", pii_type=PiiType.ORG_PRIVATE,
        cascade=True, apply_to_same=True,
    )
    assert added
    assert all(c.pii_type is PiiType.ORG_PRIVATE for c in added)
    window.close()


def test_editing_retypes_and_regenerates_the_replacement(qapp, tmp_path):
    """A date pseudonymised as a name must stop being a name once retyped."""
    from app.detection.types import PiiType

    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab.set_filter("all")
    group = tab.cards[0].group
    before = tab.session.registry.pseudonym_for(group.pii_type, group.normalized)

    tab.session.registry.forget(group.pii_type, group.normalized)
    for candidate in group.candidates:
        candidate.pii_type = PiiType.DOB
    after = tab.session.registry.pseudonym_for(PiiType.DOB, group.normalized)
    assert after != before
    window.close()


def test_unreviewed_prompt_offers_every_route(qapp):
    from app.ui.dialogs import UnreviewedPrompt

    prompt = UnreviewedPrompt(None, reviewed=3, unreviewed=5)
    assert {prompt.PROCESS_ALL, prompt.PROCESS_REVIEWED, prompt.KEEP_REVIEWING} == {
        "all", "reviewed", "review"
    }
    assert prompt.choice == prompt.CANCEL
    prompt._choose(prompt.PROCESS_REVIEWED)
    assert prompt.choice == "reviewed"
    prompt.close()


def test_output_folder_defaults_to_a_real_location(qapp):
    """Blank meant 'beside each source file', which looked like nothing ran."""
    from pathlib import Path

    window = MainWindow()
    assert window.output_edit.text().strip()
    assert window.batch.output_folder == window.output_edit.text().strip()
    assert Path(window.batch.output_folder).name == "Anonymized"
    window.close()


def test_an_added_item_appears_in_the_list_immediately(qapp, tmp_path):
    """Regression: additions were marked reviewed and vanished into Done."""
    from app.detection.types import PiiType

    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab.set_filter("all")
    before = len(tab.cards)

    added = tab.session.add_manual_text("Annual Salary", pii_type=PiiType.ORG_PRIVATE)
    assert added
    tab.session.decisions.mark_reviewed(added, False)
    tab.set_filter("all")
    assert len(tab.cards) > before

    tab.set_filter("flagged")
    shown = {c.group.normalized.lower() for c in tab.cards}
    assert "annual salary" in shown, "an added item is not visible for review"
    window.close()


def test_a_decision_refreshes_the_preview_immediately(qapp, tmp_path):
    """Regression: the live preview did not update after edits or additions."""
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab.set_filter("all")
    group = tab.cards[0].group

    calls: list = []
    tab._refresh_preview = lambda: calls.append(True)
    tab.decide_group(group, DecisionState.SKIPPED)
    assert calls, "the preview was never refreshed after a decision"
    window.close()


def test_results_name_every_file_written(qapp, tmp_path):
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab.session.decisions.set_state(tab.session.candidates, DecisionState.ACCEPTED)
    tab.approve()
    done = window.batch.process_approved()
    window.results.show_results(window.batch.items, window.batch.output_folder)
    summary = window.results.summary.text()
    assert done and done[0].output_path
    from pathlib import Path

    assert Path(done[0].output_path).name in summary, "the output filename is not shown"
    window.close()


def test_process_actually_writes_a_file(qapp, tmp_path, monkeypatch):
    """Regression: Process did nothing at all.

    `prompt.Accepted` raises AttributeError on a PySide6 dialog INSTANCE - the
    enum lives on the class. The exception died inside the slot, so the button
    appeared inert. The same line broke Add missed item and Edit.
    """
    import time

    from PySide6.QtWidgets import QDialog

    import app.ui.main_window as mw

    class AutoPrompt(mw.UnreviewedPrompt):
        def exec(self):
            self.choice = self.PROCESS_ALL
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw, "UnreviewedPrompt", AutoPrompt)
    monkeypatch.setattr(mw.QMessageBox, "information", staticmethod(lambda *a, **k: None))

    out = tmp_path / "out"
    window = _window(tmp_path, 1)
    window.output_edit.setText(str(out))
    window.tabs.widget(0).approve()
    window._refresh_queue()
    window.process()

    assert window.stack.currentWidget() is window.processing, "no processing view shown"
    deadline = time.time() + 90
    while time.time() < deadline and not window.batch.completed:
        qapp.processEvents()
        time.sleep(0.05)

    assert window.batch.completed, "nothing was processed"
    assert list(out.glob("*.pdf")), "no output file was written"
    window.close()


def test_dialogs_compare_against_the_class_enum():
    """Guard the exact mistake: instance access to a Qt enum."""
    from pathlib import Path as _Path

    source = (_Path(__file__).resolve().parents[1] / "app" / "ui" / "main_window.py").read_text()
    assert "dialog.Accepted" not in source
    assert "prompt.Accepted" not in source
    assert source.count("QDialog.DialogCode.Accepted") >= 3


def test_adding_a_missed_value_updates_the_list_and_the_plan(qapp, tmp_path, monkeypatch):
    """Regression: the added value never appeared and the preview never changed."""
    from PySide6.QtWidgets import QDialog

    import app.ui.main_window as mw
    from app.detection.types import PiiType

    class AutoAdd(mw.AddPiiDialog):
        def exec(self):
            return QDialog.DialogCode.Accepted

        def result_values(self):
            return ("Annual Salary", PiiType.ORG_PRIVATE, "Yearly Pay", True, True)

    monkeypatch.setattr(mw, "AddPiiDialog", AutoAdd)
    monkeypatch.setattr(mw.QMessageBox, "information", staticmethod(lambda *a, **k: None))

    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab.set_filter("all")
    before_rows, before_targets = len(tab.cards), len(tab.session.plan().targets)

    tab.add_missed()
    tab.set_filter("all")

    assert len(tab.cards) == before_rows + 1, "the added value is not in the list"
    assert len(tab.session.plan().targets) == before_targets + 1, "the plan did not change"
    assert any("Annual Salary" in c.group.display for c in tab.cards)
    window.close()


def test_editing_retypes_through_the_dialog(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog

    import app.ui.main_window as mw
    from app.decisions.manager import DecisionState
    from app.detection.types import PiiType

    class AutoEdit(mw.EditDetectionDialog):
        def exec(self):
            return QDialog.DialogCode.Accepted

        def result_values(self):
            return (PiiType.DOB, "", True)

    monkeypatch.setattr(mw, "EditDetectionDialog", AutoEdit)

    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab.set_filter("all")
    group = tab.cards[0].group
    tab.decide_group(group, DecisionState.EDITED)
    tab.set_filter("all")
    assert PiiType.DOB in {c.group.pii_type for c in tab.cards}
    window.close()


def test_a_decision_changes_the_transformed_preview(qapp, tmp_path):
    """Regression: the live preview did not follow edits, adds or deletes."""
    import hashlib

    from app.decisions.manager import DecisionState

    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab.set_filter("all")

    def render_hash() -> str:
        images = tab.session.preview_transformed_pages(tab.visible_pages(), 1.0)
        return hashlib.sha256(images[0]).hexdigest()

    before = render_hash()
    tab.decide_group(tab.cards[0].group, DecisionState.SKIPPED)
    assert render_hash() != before, "the preview did not follow the decision"
    window.close()


def test_redact_everything_asks_first_and_moves_all_to_done(qapp, tmp_path, monkeypatch):
    import app.ui.main_window as mw

    asked = []

    def fake_question(*args, **kwargs):
        asked.append(args[1] if len(args) > 1 else "")
        return mw.QMessageBox.Yes

    monkeypatch.setattr(mw.QMessageBox, "question", staticmethod(fake_question))

    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab.redact_all()
    assert asked, "Redact everything did not ask for confirmation"
    tab.set_filter("flagged")
    assert not tab.cards, "items remained in To review"
    window.close()


def test_search_and_act_from_the_manual_strip(qapp, tmp_path, monkeypatch):
    import app.ui.main_window as mw

    monkeypatch.setattr(mw.QMessageBox, "information", staticmethod(lambda *a, **k: None))
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)

    tab.search_doc.setText("Annual Salary")
    tab.search_and_blackout()
    tab.set_filter("all")
    added = [c for c in tab.session.candidates if c.blackout]
    assert added, "search + black out added nothing"

    tab.search_doc.setText("no such text at all")
    tab.search_and_pseudonymize()  # must not raise
    window.close()


def test_drawing_a_box_adds_a_blackout(qapp, tmp_path):
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    before = len(tab.session.candidates)
    tab.on_region_drawn(0, (70.0, 690.0, 300.0, 706.0))
    assert len(tab.session.candidates) == before + 1
    assert tab.session.candidates[-1].blackout
    window.close()


def test_box_mode_toggles_on_the_canvas(qapp, tmp_path):
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab.set_box_mode(True)
    assert tab.left_canvas.box_mode
    assert all(v.box_mode for v in tab.left_canvas.views.values())
    tab.set_box_mode(False)
    assert not tab.left_canvas.box_mode
    window.close()


def test_there_is_no_separate_second_pass_section(qapp, tmp_path):
    """Analysis is one process; its later findings are not a separate stage."""
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    assert set(tab.chips) == {"flagged", "reviewed", "all", "kept"}
    assert not hasattr(tab, "_is_second_pass")
    window.close()


def test_keep_everything_moves_all_to_kept(qapp, tmp_path, monkeypatch):
    """Requested: the counterpart to Redact everything."""
    import app.ui.main_window as mw

    monkeypatch.setattr(
        mw.QMessageBox, "question",
        staticmethod(lambda *a, **k: mw.QMessageBox.StandardButton.Yes),
    )
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    tab.keep_all()

    tab.set_filter("kept")
    assert tab.cards, "nothing moved to Kept"
    tab.set_filter("flagged")
    assert not tab.cards, "items remained in To review"
    assert not tab.session.plan().targets, "something is still being changed"
    window.close()


def test_the_review_panel_resizes_with_the_window(qapp, tmp_path):
    """Reported: text cut off. The panel was pinned regardless of window size."""
    window = _window(tmp_path, 1)
    tab = window.tabs.widget(0)
    sidebar = tab.splitter.widget(0)
    assert sidebar.maximumWidth() > sidebar.minimumWidth(), "the panel is fixed width"
    assert sidebar.minimumWidth() <= 320
    tab.splitter.setSizes([300, 900])
    assert tab.splitter.sizes()[0] != 392
    window.close()


def test_card_text_elides_to_the_available_width(qapp):
    from app.ui.widgets import ElidingLabel

    full = "Financial Professional at a very long organisation indeed"
    label = ElidingLabel(full)

    label.resize(120, 20)
    label.setText(full)          # re-elide at the new width
    narrow = label.text()

    label.resize(900, 20)
    label.setText(full)
    wide = label.text()

    assert narrow.endswith("\u2026"), narrow
    assert len(wide) > len(narrow), (narrow, wide)
    assert label.toolTip() == "" or full in label.toolTip()


def test_clicking_a_page_scrolls_to_it(qapp, tmp_path):
    """Reported: page jumping did nothing."""
    import time

    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    path = tmp_path / "many.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    for _ in range(4):
        c.setFont("Helvetica", 10)
        c.drawString(72, 720, "Taxpayer name")
        c.drawString(72, 706, "Marisol Etxeberria")
        c.showPage()
    c.save()

    from app.batch import BatchItem
    from app.ui.main_window import DocumentTab

    window = MainWindow()
    item = window.batch.add_files([str(path)])[0]
    tab = DocumentTab(item)
    window.tabs.addTab(tab, item.name)
    window.tabs_by_item[item.source_path] = tab
    window.batch.analyse(item)
    tab.populate()
    tab.set_filter("all")

    group = next(g for g in tab.groups() if "Etxeberria" in g.display)
    tab.jump_to_page(group, 3)
    deadline = time.time() + 5
    while time.time() < deadline and tab.page_spin.value() != 4:
        qapp.processEvents()
        time.sleep(0.02)
    assert tab.page_spin.value() == 4, "the page indicator did not follow the jump"
    window.close()
