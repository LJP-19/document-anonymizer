"""Headless GUI smoke test (spec section 56). Runs in CI with the offscreen platform."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.session import AnonymizationSession  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tests import fixtures  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_window_opens_analyzes_and_previews(qapp, tmp_path):
    pdf = fixtures.form_pdf(tmp_path / "form.pdf")
    window = MainWindow()
    window.session = AnonymizationSession(source_path=pdf)
    window.session.analyse()
    window._on_analyzed(None)

    assert window.tree.topLevelItemCount() == 2
    assert window.tree.topLevelItem(0).childCount() > 0, "nothing queued for review"
    assert "NEEDS REVIEW" in window.statusBar().currentMessage()

    # The preview must be a real rendering of the real transformed document.
    png = window.session.preview_transformed(0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"

    window.accept_all()
    assert not window.session.needs_review()
    window.close()
