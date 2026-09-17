"""A person is found everywhere they appear, on every page (spec sections 18-19, 21).

The reported failure: a surname detected and redacted on pages 1-5 was left
untouched elsewhere on those same pages, and on pages 7-10 entirely.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.detection.types import PiiType
from app.session import AnonymizationSession


def _multi_page_form(path: Path, pages: int = 5, note: bool = True) -> str:
    c = canvas.Canvas(str(path), pagesize=letter)
    for page in range(pages):
        c.setFont("Helvetica", 10)
        c.drawString(72, 720, "Taxpayer name")
        c.drawString(72, 706, "Mark & Macey Lang")
        if note:
            c.drawString(72, 680, f"Preparer reviewed for Lang, page {page + 1} of {pages}")
        c.showPage()
    c.save()
    return str(path)


def test_a_name_field_repeated_on_every_page_is_still_detected(tmp_path):
    """Regression: a field bound to a PII label was vetoed as a repeated
    running header, because it legitimately sits in the same position on
    every page - exactly like chrome does. The client's name disappeared
    from every page, not just the repeated ones."""
    session = AnonymizationSession(source_path=_multi_page_form(tmp_path / "f.pdf", note=False))
    session.analyse()
    by_page = {}
    for candidate in session.candidates:
        by_page.setdefault(candidate.page_no, []).append(candidate.text.strip())
    for page in range(5):
        assert "Mark" in by_page.get(page, []), f"page {page + 1}: {by_page.get(page)}"
        assert any("Lang" in v for v in by_page.get(page, [])), f"page {page + 1}"


def test_a_bare_surname_is_found_on_every_page_it_appears(tmp_path):
    """Once a surname is known, it must be caught wherever it recurs -
    including inside otherwise boilerplate-looking text with distinct page
    numbers, which real repetition detection must not blanket-veto."""
    session = AnonymizationSession(source_path=_multi_page_form(tmp_path / "g.pdf", pages=10))
    session.analyse()
    hits_per_page = {
        page: [c for c in session.candidates if c.page_no == page and "Lang" in c.text]
        for page in range(10)
    }
    missing = [p + 1 for p, hits in hits_per_page.items() if not hits]
    assert not missing, f"surname missed entirely on page(s): {missing}"


def test_propagated_hits_are_not_re_vetoed_as_form_chrome(tmp_path):
    """A confirmed subject's name inside a repeated boilerplate sentence must
    still be caught - propagation already proved it is real identity, and a
    later repetition guess must not override that."""
    from app.detection.types import Source

    session = AnonymizationSession(
        source_path=_multi_page_form(tmp_path / "h.pdf", pages=6, note=True)
    )
    session.analyse()
    propagated = [c for c in session.candidates if c.source is Source.COVERAGE]
    assert propagated, "nothing was propagated at all"
    assert any("Lang" in c.text for c in propagated)


@pytest.mark.xfail(
    reason="Known, ACCEPTED trade-off: the redactor was reverted to the "
    "literal v0.8.3 rendering logic on direct instruction, which has no "
    "neighbour-awareness at all. This exact tight-spacing case can lose "
    "text as a result. The alternative (headroom-capped padding) was "
    "reported as rendering worse on real documents, repeatedly, and that "
    "report is being trusted over this specific synthetic risk. Do not "
    "silently re-add headroom capping to fix this without being asked.",
    strict=False,
)
def test_every_occurrence_survives_to_the_final_output(tmp_path):
    from app.decisions.manager import DecisionState

    path = _multi_page_form(tmp_path / "out.pdf", pages=4, note=False)
    session = AnonymizationSession(source_path=path)
    session.analyse()
    typed = [c for c in session.candidates if c.pii_type is PiiType.PERSON]
    session.decisions.set_state(typed, DecisionState.ACCEPTED)

    out = str(tmp_path / "result.pdf")
    _apply, report = session.process(out)
    assert report.passed, [f"{c.name}: {c.detail}" for c in report.failures]

    import pymupdf

    doc = pymupdf.open(out)
    try:
        body = "\n".join(p.get_text() for p in doc)
    finally:
        doc.close()
    assert "Mark & Macey Lang" not in body
    assert "Lang" not in body


def test_an_unlabelled_repeated_block_is_not_treated_as_chrome(tmp_path):
    """General fix: ANY line already carrying a candidate is exempt from the
    repeated-position veto, not only group-bound values. A real address or ID
    block printed identically on every page of a multi-page return usually has
    no binding label at all, and is legitimate repeated PII, not a header."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    path = tmp_path / "unlabelled.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    for _ in range(4):
        c.setFont("Helvetica", 10)
        c.drawString(72, 720, "123 Main Street")
        c.drawString(72, 706, "San Jose, CA 95129")
        c.showPage()
    c.save()

    session = AnonymizationSession(source_path=str(path))
    session.analyse()
    by_page = {
        page: [c.text for c in session.candidates if c.page_no == page]
        for page in range(4)
    }
    for page in range(4):
        assert by_page[page], f"page {page + 1}: everything was vetoed as chrome"
        assert any("San Jose" in v for v in by_page[page]), by_page[page]


def test_propagation_does_not_depend_on_a_hand_curated_label_word_list(tmp_path):
    """Reported: cascading fails on real documents during normal processing.

    A value's own label was required to match a fixed word list (taxpayer,
    spouse, filer...) OR reach 0.85 confidence to ever become a "subject" -
    which meant a document whose actual label wording ("Policyholder") was
    not on that list never qualified anything, so propagation silently never
    ran at all, regardless of how many pages repeated the value.
    """
    path = _multi_page_form(tmp_path / "unknown_label.pdf", pages=4, note=False)
    # Overwrite with an unrecognised-wording label instead of "Taxpayer name".
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=letter)
    for page in range(4):
        c.setFont("Helvetica", 9)
        c.drawString(72, 720, "Policyholder")
        c.drawString(72, 706, "Diana Whitfield")
        c.drawString(72, 680, "Notice reference for Whitfield on file")
        c.showPage()
    c.save()

    from app.session import AnonymizationSession

    session = AnonymizationSession(source_path=str(path))
    session.analyse()
    by_page = {
        page: [c.text for c in session.candidates if c.page_no == page]
        for page in range(4)
    }
    for page in range(4):
        assert any("Whitfield" in v for v in by_page[page]), f"page {page + 1}: {by_page[page]}"
    # And the label itself was never misdetected as PII.
    assert not any("Policyholder" in c.text for c in session.candidates)
