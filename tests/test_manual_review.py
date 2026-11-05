"""Manual review: drawn blackout areas and search-and-act."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from app.decisions.manager import DecisionState
from app.session import AnonymizationSession
from tests import fixtures


def _analysed(path: str) -> AnonymizationSession:
    session = AnonymizationSession(source_path=path)
    session.analyse()
    return session


def _text(path: str) -> str:
    doc = pymupdf.open(path)
    try:
        return "\n".join(p.get_text() for p in doc)
    finally:
        doc.close()


def test_a_drawn_area_is_blacked_out(tmp_path):
    session = _analysed(fixtures.form_pdf(tmp_path / "draw.pdf"))
    added = session.add_manual_region(0, (70, 690, 300, 706))
    assert added is not None and added.blackout

    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)
    out = str(tmp_path / "out.pdf")
    _apply, report = session.process(out)
    assert report.passed, report.failures

    # Check the rendered page, not the drawing list: what matters is that the
    # area is actually black on screen and in print.
    doc = pymupdf.open(out)
    try:
        pixels = doc[0].get_pixmap()
        assert pixels.pixel(180, 698) == (0, 0, 0), "the drawn area is not black"
        assert pixels.pixel(400, 400) == (255, 255, 255), "the bar covered the whole page"
    finally:
        doc.close()


def test_a_degenerate_rectangle_is_refused(tmp_path):
    session = _analysed(fixtures.form_pdf(tmp_path / "tiny.pdf"))
    assert session.add_manual_region(0, (10, 10, 11, 11)) is None
    assert session.add_manual_region(99, (10, 10, 200, 40)) is None


def test_search_finds_every_occurrence(tmp_path):
    session = _analysed(fixtures.form_pdf(tmp_path / "find.pdf"))
    hits = session.find_text("John Smith")
    assert hits, "search found nothing"
    for page_no, rect, line in hits:
        assert page_no == 0
        assert rect[2] > rect[0] and rect[3] > rect[1]
        assert "John Smith" in line


def test_search_is_case_insensitive_and_whole_token(tmp_path):
    session = _analysed(fixtures.form_pdf(tmp_path / "case.pdf"))
    assert session.find_text("john smith")
    assert not session.find_text("ohn Smit"), "matched a fragment"


def test_search_then_black_out_removes_the_text(tmp_path):
    session = _analysed(fixtures.form_pdf(tmp_path / "sbo.pdf"))
    added = session.add_manual_text("Annual Salary", blackout=True)
    assert added and all(c.blackout for c in added)

    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)
    out = str(tmp_path / "out.pdf")
    session.process(out)
    assert "Annual Salary" not in _text(out)


def test_search_then_pseudonymize_substitutes_text(tmp_path):
    session = _analysed(fixtures.form_pdf(tmp_path / "sps.pdf"))
    added = session.add_manual_text("Annual Salary", replacement="Yearly Pay")
    assert added and not any(c.blackout for c in added)

    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)
    out = str(tmp_path / "out.pdf")
    session.process(out)
    body = _text(out)
    assert "Annual Salary" not in body and "Yearly Pay" in body


def test_blackouts_do_not_break_verification(tmp_path):
    """A blackout has no replacement text; the checks must not expect one."""
    session = _analysed(fixtures.form_pdf(tmp_path / "ver.pdf"))
    session.add_manual_region(0, (70, 690, 300, 706))
    session.add_manual_text("Annual Salary", blackout=True)
    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)

    _apply, report = session.process(str(tmp_path / "out.pdf"))
    assert report.passed, [f"{c.name}: {c.detail}" for c in report.failures]
