"""Only the client's values are transformed - never the form itself."""

from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.detection.form_text import detect_form_text, veto_form_text
from app.document.provider import NativePdfTextProvider
from app.session import AnonymizationSession


def _form(path: Path, pages: int = 3) -> str:
    c = canvas.Canvas(str(path), pagesize=letter)
    for _ in range(pages):
        c.setFont("Helvetica", 9)
        c.drawString(60, 760, "Form 1040 (2025)   Department of the Treasury")
        c.drawString(60, 40, "For Paperwork Reduction Act Notice, see instructions.  Cat. No. 11320B")
        c.setFont("Helvetica", 10)
        c.drawString(60, 700, "Your first name and middle initial")
        c.drawString(60, 686, "MARIA T")
        c.drawString(60, 664, "Check if you, or your spouse if filing jointly, want $3 to go to this fund")
        c.drawString(60, 640, "1  Wages, salaries, tips  ......  $412,890")
        c.showPage()
    c.save()
    return str(path)


@pytest.fixture
def form(tmp_path) -> str:
    return _form(tmp_path / "form.pdf")


def test_running_headers_and_footers_are_recognised(form):
    doc = NativePdfTextProvider().load(form)
    found = detect_form_text(doc)
    texts = {
        line.text
        for page in doc.pages
        for line in page.lines
        if found.covers(line)
    }
    assert any("Department of the Treasury" in t for t in texts), texts
    assert any("Paperwork Reduction" in t for t in texts), texts


def test_instructions_are_recognised(form):
    doc = NativePdfTextProvider().load(form)
    found = detect_form_text(doc)
    reasons = {
        found.reason_for(line)
        for page in doc.pages
        for line in page.lines
        if found.covers(line)
    }
    assert "form instruction" in reasons or "repeats on every page" in reasons


def test_only_the_filled_value_is_detected(form):
    session = AnonymizationSession(source_path=form)
    session.analyse()
    values = {c.text.strip() for c in session.candidates}
    assert values == {"MARIA T"}, values


def test_the_form_survives_processing(form, tmp_path):
    import pymupdf

    from app.decisions.manager import DecisionState

    session = AnonymizationSession(source_path=form)
    session.analyse()
    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)
    out = str(tmp_path / "out.pdf")
    session.process(out)

    doc = pymupdf.open(out)
    try:
        body = "\n".join(p.get_text() for p in doc)
    finally:
        doc.close()

    for kept in ("Department of the Treasury", "Paperwork Reduction",
                 "Your first name and middle initial", "$412,890", "Cat. No."):
        assert kept in body, f"the form lost {kept!r}"
    assert "MARIA T" not in body, "the value was not replaced"


def test_a_users_own_addition_is_never_vetoed(form):
    session = AnonymizationSession(source_path=form)
    session.analyse()
    added = session.add_manual_text("Department of the Treasury")
    assert added, "the user's explicit instruction was overruled"

    from app.detection.form_text import detect_form_text as detect

    kept, _dropped = veto_form_text(session.candidates, detect(session.document))
    assert any(c.source.value == "manual" for c in kept)


def test_a_single_page_document_keeps_its_values(tmp_path):
    """Repetition needs several pages; one page must not be over-suppressed."""
    path = _form(tmp_path / "one.pdf", pages=1)
    session = AnonymizationSession(source_path=path)
    session.analyse()
    assert any("MARIA" in c.text for c in session.candidates)
