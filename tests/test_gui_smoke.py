"""Headless GUI smoke test (spec section 56). Runs in CI with the offscreen platform."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.decisions.manager import DecisionState  # noqa: E402
from app.session import AnonymizationSession  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tests import fixtures  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _opened(tmp_path) -> MainWindow:
    pdf = fixtures.form_pdf(tmp_path / "form.pdf")
    window = MainWindow()
    window.session = AnonymizationSession(source_path=pdf)
    window.session.analyse()
    window._on_analyzed(None)
    return window


def test_window_analyzes_and_lists_detections(qapp, tmp_path):
    window = _opened(tmp_path)
    window._set_filter("all")
    assert window.cards, "no detection cards were built"
    assert window.export_button.isEnabled()
    assert window.page_total.text().startswith("of ")
    window.close()


def test_preview_is_a_real_render_of_the_transformed_document(qapp, tmp_path):
    window = _opened(tmp_path)
    png = window.session.preview_transformed(0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    window.close()


def test_selecting_a_card_focuses_it_and_draws_overlays(qapp, tmp_path):
    window = _opened(tmp_path)
    window._set_filter("all")
    group = window.cards[0].group
    window._on_card_selected(group)
    assert window.selected_key == (group.pii_type, group.normalized)
    assert window.left_canvas._overlays, "no highlight overlays drawn"
    window.close()


def test_keep_moves_an_item_out_of_the_redaction_plan(qapp, tmp_path):
    window = _opened(tmp_path)
    window._set_filter("all")
    group = window.cards[0].group
    window._on_card_decided(group, DecisionState.SKIPPED)
    plan = window.session.plan()
    assert group.display.lower() in [v.lower() for v in plan.skipped_values]
    assert group.candidates[0].id not in {t.candidate_id for t in plan.targets}
    window.close()


def test_filters_partition_the_detections(qapp, tmp_path):
    window = _opened(tmp_path)
    window._set_filter("all")
    total = len(window.cards)
    window._set_filter("kept")
    assert not window.cards
    window._set_filter("flagged")
    assert len(window.cards) <= total
    window.close()


def test_search_narrows_the_list(qapp, tmp_path):
    window = _opened(tmp_path)
    window._set_filter("all")
    window._on_search("zzzz-no-such-value")
    assert not window.cards
    window._on_search("")
    assert window.cards
    window.close()
