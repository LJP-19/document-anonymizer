"""Headless GUI smoke tests for the tabbed batch workspace."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DOCANON_LLM", "0")

pytest.importorskip("PySide6")

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
