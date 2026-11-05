"""Regressions found on real, software-prepared tax returns (v0.39.0 audit).

The diagnostic returns are never committed. Every case here rebuilds the SHAPE
of a measured failure with invented values. Each test names the layer that was
wrong and fails if that layer regresses.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from app.decisions.manager import DecisionState
from app.detection.deterministic import load_rules
from app.detection.groups import build_groups, is_placeholder_value
from app.detection.labels import _match_label
from app.detection.resolve import veto_by_label_geometry
from app.detection.types import Candidate, PiiType, Source
from app.document.provider import NativePdfTextProvider
from app.session import AnonymizationSession

TEMPLATE = (0x23 / 255, 0x1F / 255, 0x20 / 255)


def _form(path: Path, pages: list[list[tuple[float, float, str, str]]]) -> str:
    """pages: [(x, y, text, kind)], kind 'label' (template Helvetica) or 'value' (black Courier)."""
    doc = pymupdf.open()
    for items in pages:
        page = doc.new_page()
        for x, y, text, kind in items:
            if kind == "label":
                page.insert_text((x, y), text, fontsize=7, fontname="helv", color=TEMPLATE)
            elif kind == "prose":
                page.insert_text((x, y), text, fontsize=9, fontname="helv", color=(0, 0, 0))
            else:
                page.insert_text((x, y), text, fontsize=10, fontname="cour", color=(0, 0, 0))
    doc.save(str(path))
    doc.close()
    return str(path)


def _processed(path: str, out: Path) -> str:
    s = AnonymizationSession(source_path=path)
    s.analyse()
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    s.process(str(out))
    with pymupdf.open(str(out)) as d:
        return "\n".join(p.get_text() for p in d)


HEADER_ROW = [
    (36, 100, "Your first name and middle initial", "label"),
    (230, 100, "Your last name", "label"),
    (420, 100, "Your social security number", "label"),
    (36, 114, "KAI", "value"),
    (230, 114, "LIM", "value"),
    (420, 114, "604-27-1185", "value"),
]


# --- groups.py: side-by-side labels -----------------------------------------

def test_every_column_of_a_label_row_binds_its_value(tmp_path):
    """Value discovery walked page reading order and stopped at the first line
    out of alignment - the LEFT column's value - so only the leftmost label on a
    row ever bound anything. Measured on real returns: every last-name, SSN,
    state and ZIP box."""
    doc = NativePdfTextProvider().load(_form(tmp_path / "row.pdf", [HEADER_ROW]))
    groups, _labels, _extra = build_groups(doc, [], load_rules())
    bound = {g.label.text: [ln.text for ln in g.value_lines] for g in groups}
    assert bound["Your first name and middle initial"] == ["KAI"]
    assert bound["Your last name"] == ["LIM"]
    assert bound["Your social security number"] == ["604-27-1185"]


# --- labels.py ----------------------------------------------------------------

@pytest.mark.parametrize("value", ["Lakeside City", "LAKESIDE CITY", "Salt Lake City", "Jersey City"])
def test_a_city_name_ending_in_city_is_not_a_label(value):
    """The address-label pattern matched "CITY" inside "LAKESIDE CITY"; the value
    became a protected label and was never redacted."""
    assert _match_label(value, load_rules()) is None


@pytest.mark.parametrize("label", ["City", "Your city", "City, town, or post office"])
def test_real_city_labels_are_still_labels(label):
    assert _match_label(label, load_rules()) is not None


@pytest.mark.parametrize("label, expected", [
    ("Spouse's/RDP's DOB (mm/dd/yyyy)", PiiType.DOB),
    ("Taxpayer SSN", PiiType.SSN),
    ("Spouse's social security number", PiiType.SSN),
    ("Your first name and middle initial", PiiType.PERSON),
])
def test_a_label_is_typed_by_its_head_noun(label, expected):
    """Longest-match typing let the role word win: a spouse's DOB column was PERSON."""
    expects, non_pii, _unknown = _match_label(label, load_rules())
    assert not non_pii and expects == [expected]


# --- resolve.py ---------------------------------------------------------------

def test_label_geometry_veto_only_compares_the_same_page(tmp_path):
    """A first name on one page was deleted because a label sat at the same x/y
    on a DIFFERENT page. Rectangles are page coordinates."""
    path = _form(tmp_path / "pages.pdf", [
        [(36, 80, "Important: read this", "label")],
        [(36, 80, "KAI", "value")],
    ])
    doc = NativePdfTextProvider().load(path)
    _g, labels, _e = build_groups(doc, [], load_rules())
    line = doc.pages[1].lines[0]
    cand = Candidate(pii_type=PiiType.PERSON, text="KAI", page_no=1, rect=line.rect_for(0, 3),
                     line=line, start=0, end=3, confidence=0.9, source=Source.GROUP)
    kept, dropped = veto_by_label_geometry([cand], labels)
    assert kept == [cand] and dropped == 0


# --- groups.py: placeholders ----------------------------------------------------

@pytest.mark.parametrize("value", ["Self-Prepared", "N/A", "See attached", "NONE"])
def test_placeholder_values_identify_nobody(value):
    assert is_placeholder_value(value)


# --- end to end: the shapes that leaked on real returns -------------------------

def _return(tmp_path) -> str:
    page1 = HEADER_ROW + [
        (36, 140, "Home address (number and street). If you have a P.O. box, see instructions.", "label"),
        (36, 154, "2290 SEACLIFF CMN", "value"),
        (36, 174, "City, town, or post office. If you have a foreign address, also complete spaces below.", "label"),
        (300, 174, "State", "label"),
        (360, 174, "ZIP code", "label"),
        (36, 188, "LAKESIDE CITY", "value"),
        (300, 188, "CA", "value"),
        (360, 188, "94621", "value"),
        (450, 140, "Presidential Election Campaign", "label"),
        (450, 220, "Preparer's name", "label"),
        (450, 234, "Self-Prepared", "value"),
    ]
    page2 = [
        (36, 60, "Name(s) shown on return", "label"),
        (430, 60, "Your social security number", "label"),
        (36, 74, "Kai Lim", "value"),
        (430, 74, "604-27-1185", "value"),
        (36, 120, "Mail payments to:", "prose"),
        (36, 134, "Internal Revenue Service", "prose"),
        (36, 148, "PO BOX 802502", "prose"),
        (36, 162, "CINCINNATI OH 45280-2502", "prose"),
    ]
    page3 = [
        (36, 60, "Your first name", "label"),
        (200, 60, "Last name", "label"),
        (36, 74, "KAI", "value"),
        (200, 74, "LIM", "value"),
        (36, 120, "CA LIM  30 0 202212 430", "value"),
        (36, 160, "Mr. Lim confirmed the transfer amount of $4210.35.", "prose"),
    ]
    return _form(tmp_path / "return.pdf", [page1, page2, page3])


@pytest.fixture(scope="module")
def processed_return(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ret")
    src = _return(tmp)
    return _processed(src, tmp / "out.pdf")


def test_short_surname_is_redacted_on_every_page(processed_return):
    """A 3-letter surname, label-confirmed on page 1, survived on every later
    page: propagation and the name-shape checks both required 4+ letters."""
    import re

    assert not re.search(r"\bLIM\b|\bLim\b", processed_return)


def test_city_value_ending_in_city_is_redacted(processed_return):
    assert "LAKESIDE CITY" not in processed_return


def test_scanline_name_control_is_redacted(processed_return):
    assert "30 0 202212 430" in processed_return   # the machine codes stay
    assert "CA LIM" not in processed_return


def test_form_heading_and_placeholder_are_not_redacted(processed_return):
    assert "Presidential Election Campaign" in processed_return
    assert "Self-Prepared" in processed_return


def test_public_body_mailing_address_is_not_redacted(processed_return):
    assert "PO BOX 802502" in processed_return
    assert "CINCINNATI OH 45280-2502" in processed_return


def test_state_alone_is_not_redacted(processed_return):
    import re

    assert re.search(r"(?m)^CA$", processed_return)


def test_identity_values_are_gone(processed_return):
    for value in ("KAI", "604-27-1185", "2290 SEACLIFF CMN", "94621"):
        assert value not in processed_return


# --- offline: Presidio's e-mail recognizer must not fetch the suffix list --------

def test_tldextract_is_forced_offline(monkeypatch):
    import socket

    pytest.importorskip("tldextract")
    from app.detection.presidio_adapter import _force_offline_tldextract

    def refuse(*_a, **_k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    _force_offline_tldextract()
    import tldextract

    assert tldextract.extract("someone@mail.example.co.uk").suffix == "co.uk"


def test_presidio_dates_need_a_personal_cue():
    from app.detection.presidio_adapter import _personal_dates_only
    from app.detection.provider_shim import document_from_pages

    doc = document_from_pages(["Date of Withdrawal: 04/14/2022\nDate of birth: 03/14/1979"])
    lines = doc.pages[0].lines
    pay, dob = lines[0], lines[1]

    def c(line, text):
        start = line.text.index(text)
        return Candidate(pii_type=PiiType.PERSONAL_DATE, text=text, page_no=0,
                         rect=line.rect_for(start, start + len(text)), line=line,
                         start=start, end=start + len(text), confidence=0.6, source=Source.NER)

    kept = _personal_dates_only([c(pay, "04/14/2022"), c(dob, "03/14/1979")], {})
    assert [k.text for k in kept] == ["03/14/1979"]


# --- verifier: ordinary words are not "fragments", real leaks still are -------

def test_verifier_ignores_a_value_word_that_is_also_form_text(tmp_path):
    """"CITY" in a redacted "LAKESIDE CITY" also occurs in the label "City, town...".
    The surviving label word failed verification on a correctly redacted file."""
    src = _form(tmp_path / "city.pdf", [[
        (36, 60, "City, town, or post office", "label"),
        (36, 74, "LAKESIDE CITY", "value"),
        (36, 100, "Your first name", "label"),
        (36, 114, "KAI", "value"),
    ]])
    s = AnonymizationSession(source_path=src)
    s.analyse()
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    _apply, report = s.process(str(tmp_path / "city.out.pdf"))
    failed = [c.name for c in report.checks if not c.passed]
    assert "no partial redaction" not in failed
    assert "no residual text in accepted field groups" not in failed


def test_verifier_still_fails_a_real_leak(tmp_path):
    """Verifying the UNREDACTED source against the plan must fail."""
    from app.verification.verifier import verify

    src = _form(tmp_path / "leak.pdf", [[
        (36, 100, "Your first name", "label"),
        (36, 114, "KAIMANA", "value"),
        (36, 140, "Your social security number", "label"),
        (36, 154, "604-27-1185", "value"),
    ]])
    s = AnonymizationSession(source_path=src)
    s.analyse()
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    report = verify(s.plan(), src)
    assert not report.passed


# --- rules / generator / model hygiene ---------------------------------------

@pytest.mark.parametrize("text, hit", [
    ("(510)555-0142", True), ("(510) 555-0142", True), ("510-555-0142", True),
    ("5105550142", False), ("$510,555.01", False),
])
def test_phone_without_separator_after_area_code(text, hit):
    """Tax software prints "(510)555-0142"; the old pattern required a space."""
    rule = next(r for r in load_rules().rules if r.name == "phone_us")
    assert bool(rule.regex.search(text)) is hit


@pytest.mark.parametrize("value, shape", [
    ("LAKESIDE CITY", r"^[A-Z][A-Z .'-]+$"),
    ("LAKESIDE CITY CA 94621", r"^[A-Z .'-]+ [A-Z]{2} \d{5}$"),
    ("CINCINNATI OH 45280-2502", r"^[A-Z .'-]+ [A-Z]{2} \d{5}-\d{4}$"),
    ("2290 SEACLIFF CMN", r"^\d+ [A-Z]"),
])
def test_address_replacement_keeps_the_kind_and_case(value, shape):
    """A city alone, or a comma-less city/state/ZIP line, got a STREET address."""
    import re

    from app.pseudonymization.generator import generate

    assert re.match(shape, generate(PiiType.ADDRESS, value))


def test_honorific_is_not_part_of_the_name_and_numberless_places_are_not_streets():
    from app.detection.engine import _drop_numberless_model_streets, _trim_honorifics
    from app.detection.provider_shim import document_from_pages

    doc = document_from_pages(["Ordering provider: Dr. Kwame Asante near Jordan River Trail"])
    line = doc.pages[0].lines[0]

    def c(text, t, source=Source.NER):
        start = line.text.index(text)
        return Candidate(pii_type=t, text=text, page_no=0, rect=line.rect_for(start, start + len(text)),
                         line=line, start=start, end=start + len(text), confidence=0.8, source=source)

    trimmed = _trim_honorifics([c("Dr. Kwame Asante", PiiType.PERSON)])
    assert trimmed[0].text == "Kwame Asante"
    assert _drop_numberless_model_streets([c("Jordan River Trail", PiiType.STREET)]) == []


# --- redactor: removal must not reach the neighbouring line --------------------

def test_redaction_box_does_not_erase_the_label_above(tmp_path):
    """A 10pt value 9pt below a 7pt label: the value's character box overlapped
    the label's, and PyMuPDF removes every intersecting character - the whole
    label vanished. The value must be gone, the label intact."""
    src = _form(tmp_path / "tight.pdf", [[
        (36, 100, "Your social security number", "label"),
        (36, 109, "604-27-1185", "value"),
    ]])
    with pymupdf.open(src) as d:
        assert "Your social security number" in d[0].get_text()
    s = AnonymizationSession(source_path=src)
    s.analyse()
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    out = tmp_path / "tight.out.pdf"
    s.process(str(out))
    with pymupdf.open(str(out)) as d:
        text = d[0].get_text()
    assert "604-27-1185" not in text
    assert "Your social security number" in text


# --- unlabelled identity block on a machine-readable page ----------------------

def test_birth_dates_in_an_unlabelled_identity_block_are_redacted(tmp_path):
    """A machine-readable state page prints only values - no "birth" caption -
    so the DOB rule could never fire and both filers' dates of birth survived."""
    src = _form(tmp_path / "scan.pdf", [[
        (36, 120, "KAI LIM", "value"),
        (36, 134, "2290 SEACLIFF CMN", "value"),
        (36, 148, "LAKESIDE CITY CA 94621", "value"),
        (36, 162, "08-19-1981 02-11-1983", "value"),
        (36, 300, "Placed in service 07-31-2021 $14,950", "value"),
    ]])
    s = AnonymizationSession(source_path=src)
    s.analyse()
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    out = tmp_path / "scan.out.pdf"
    s.process(str(out))
    with pymupdf.open(str(out)) as d:
        text = d[0].get_text()
    assert "08-19-1981" not in text and "02-11-1983" not in text
    assert "07-31-2021" in text and "$14,950" in text


# --- typeset style: learned per document, only when the document shows it -------

def _styles_of(path):
    from app.detection.groups import _preliminary_groups
    from app.detection.labels import detect_labels, label_line_keys
    from app.detection.layout_style import form_styles

    doc = NativePdfTextProvider().load(path)
    labels = detect_labels(doc, load_rules())
    lines = {p.number: p.lines_in_reading_order() for p in doc.pages}
    return doc, form_styles(labels, _preliminary_groups(labels, lines, label_line_keys(labels)))


def test_form_style_is_the_label_font_never_the_value_font(tmp_path):
    from benchmark.forms_corpus import CORPUS

    doc = CORPUS["return_c"](tmp_path / "rc.pdf")
    path = doc.save(tmp_path / "rc.pdf")
    _doc, styles = _styles_of(path)
    fonts = {font for font, _colour in styles}
    assert styles, "a filled form must show its form style"
    assert not any("Courier" in f for f in fonts)


def test_a_plain_document_has_no_form_style(tmp_path):
    """Labels and values in one font: nothing can be concluded, nothing is vetoed."""
    doc = pymupdf.open()
    page = doc.new_page()
    y = 80
    for label, value in [("Name:", "Harriet Vandermolen"), ("Address:", "12 Elm Street"),
                         ("Phone:", "415-555-0199"), ("Employer:", "Northwind Clinic")] * 2:
        page.insert_text((50, y), label, fontsize=10, fontname="helv")
        page.insert_text((50, y + 13), value, fontsize=10, fontname="helv")
        y += 34
    path = str(tmp_path / "plain.pdf")
    doc.save(path)
    _doc, styles = _styles_of(path)
    assert styles == set()


def test_pseudonym_never_reuses_a_word_of_another_detected_value():
    from app.entities.registry import EntityRegistry

    reg = EntityRegistry(scope="t")
    first = reg.pseudonym_for(PiiType.ADDRESS, "LAKESIDE CITY")
    city_word = next(w for w in first.split() if len(w) >= 4).lower()
    reg2 = EntityRegistry(scope="t")
    reg2.document_words = {city_word}
    second = reg2.pseudonym_for(PiiType.ADDRESS, "LAKESIDE CITY")
    assert city_word not in second.lower()


def test_a_tightly_set_value_does_not_erase_the_amount_above(tmp_path):
    """The value line sits so close under an amount that even the inset box
    intersected the amount's characters - the amount must survive."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((300, 200), "The Amount Transferred: $4210.35", fontsize=9, fontname="helv")
    page.insert_text((300, 207), "Your social security number 604-27-1185", fontsize=9, fontname="helv")
    src = str(tmp_path / "amt.pdf")
    doc.save(src)
    s = AnonymizationSession(source_path=src)
    s.analyse()
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    out = tmp_path / "amt.out.pdf"
    s.process(str(out))
    with pymupdf.open(str(out)) as d:
        text = d[0].get_text()
    assert "604-27-1185" not in text
    assert "$4210.35" in text


def test_a_captioned_line_is_the_next_field_not_the_value_above(tmp_path):
    """"- Date of Withdrawal: <date>" under an empty "- Routing Transit Number:"
    was bound as the routing number's value and shape-masked into scrambled text."""
    doc = pymupdf.open()
    page = doc.new_page()
    for k, line in enumerate(["- Account Number:", "- Routing Transit Number:", "- Date of Withdrawal:     04/14/2022"]):
        page.insert_text((36, 100 + 12 * k), line, fontsize=10, fontname="cour")
    src = str(tmp_path / "debit.pdf")
    doc.save(src)
    s = AnonymizationSession(source_path=src)
    s.analyse()
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    out = tmp_path / "debit.out.pdf"
    s.process(str(out))
    with pymupdf.open(str(out)) as d:
        assert "Date of Withdrawal" in d[0].get_text()
