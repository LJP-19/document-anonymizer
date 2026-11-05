"""Protected regions and the unified gate: labels, figures and form text are never redacted."""

from __future__ import annotations

import pymupdf
import pytest

from app.detection.protection import (
    CandidateGate, ProtectionKind, apply_ai_objections, candidate_strength, is_protected_figure_text,
)
from app.detection.types import Candidate, Evidence, PiiType, Source
from tests.helpers_text import analysed, redacted


@pytest.mark.parametrize("text", [
    "$123,456", "123,456", "1,234.56", "(123,456)", "-123,456", "85,000", "75%", "15.5%", "$18,250.00",
    "2025", "Line 12", "Form 1040", "1040", "941", "941-X", "Schedule C", "€1,234.50", "USD 5,000",
    "1,234,567", "Page 3 of 12", "Form W-2",
])
def test_business_figures_and_form_references_are_protected(text):
    assert is_protected_figure_text(text) is not None


@pytest.mark.parametrize("text", [
    "123-45-6789", "12-3456789", "123456789", "03/22/1985", "AB123456", "(555) 123-4567",
    "408.555.0198", "90210", "12345", "192.168.1.1", "021000021", "John Smith", "March 3, 2010",
    "EMP-004417", "4111 1111 1111 1111",
])
def test_real_identifiers_are_not_mistaken_for_figures(text):
    assert is_protected_figure_text(text) is None


def test_a_label_survives_and_its_value_goes():
    found = redacted("Taxpayer Name: John Smith\nSSN: 123-45-6789")
    texts = " | ".join(t for t, _ in found)
    assert "John Smith" in texts and "123-45-6789" in texts
    assert "Taxpayer" not in texts and "SSN:" not in texts and "Name" not in texts


def test_identifier_values_still_go_when_they_look_numeric():
    got = dict(redacted("EIN: 12-3456789\nAccount Number: 123456789\nRouting Number: 021000021\n"
                        "DOB: 03/22/1985\nMember ID: AB123456"))
    for value in ("12-3456789", "123456789", "021000021", "03/22/1985", "AB123456"):
        assert value in got, f"{value} was not redacted: {got}"


def test_totals_and_rates_are_kept():
    found = redacted("Total tax: $7,420\nTotal wages: $123,456\nTaxable income: 92,000\n"
                     "Total deductions: (18,250)\nRate: 75%\nTax year 2025\nLine 12 48,500\nForm 1040 Schedule C")
    assert found == [], found


def test_the_gate_clips_a_candidate_that_swallowed_its_label():
    doc, result, _ = analysed("Taxpayer Name: John Smith")
    line = doc.pages[0].lines[0]
    wide = Candidate(PiiType.PERSON, line.text, 0, line.rect_for(0, len(line.text)), line, 0,
                     len(line.text), 0.9, Source.NER)
    kept, log = CandidateGate(result.protection).run([wide])
    assert [c.text for c in kept] == ["John Smith"] and log[0].action == "clip"
    assert "label" in log[0].reason


def test_the_gate_rejects_a_candidate_that_is_only_a_label():
    doc, result, _ = analysed("Member ID: AB123456")
    line = doc.pages[0].lines[0]
    on_label = Candidate(PiiType.ADDRESS, "Member ID", 0, line.rect_for(0, 9), line, 0, 9, 0.75, Source.COVERAGE)
    kept, log = CandidateGate(result.protection).run([on_label])
    assert kept == [] and log[0].action == "reject" and log[0].kind == ProtectionKind.FORM_LABEL.value


def test_no_detector_can_redact_a_money_amount_even_the_ai():
    doc, result, _ = analysed("Total wages: 85,000")
    line = doc.pages[0].lines[0]
    start = line.text.index("85,000")
    bad = Candidate(PiiType.DOB, "85,000", 0, line.rect_for(start, start + 6), line, start, start + 6,
                    0.9, Source.AUDIT)
    kept, log = CandidateGate(result.protection).run([bad])
    assert kept == [] and log[0].action == "reject"


def test_the_user_can_always_override_protection():
    doc, result, _ = analysed("Member ID: AB123456")
    line = doc.pages[0].lines[0]
    manual = Candidate(PiiType.UNCLASSIFIED_GROUP_VALUE, "Member ID", 0, line.rect_for(0, 9), line, 0, 9,
                       1.0, Source.MANUAL)
    kept, _ = CandidateGate(result.protection).run([manual])
    assert kept == [manual]


def test_every_gate_decision_has_a_reason():
    _doc, result, _ = analysed("Taxpayer Name: John Smith\nSSN: 123-45-6789\nTotal tax: $7,420")
    assert result.gate_log and all(d.reason for d in result.gate_log)


# -- the second-pass path that used to bypass every label check -------------------------

def _pdf(tmp_path, rows):
    doc = pymupdf.open(); page = doc.new_page(); y = 70
    for row in rows:
        for x, text in (row if isinstance(row, tuple) else ((60, row),)):
            page.insert_text((x, y), text, fontsize=10)
        y += 18
    path = str(tmp_path / "t.pdf"); doc.save(path); doc.close()
    return path


def test_labels_are_not_redacted_by_the_second_pass(tmp_path):
    from app.session import AnonymizationSession

    s = AnonymizationSession(source_path=_pdf(tmp_path, ["DOB: 03/22/1985", "Member ID: AB123456"]))
    s.analyse()
    redacted_texts = [c.text for c in s.candidates if s.decisions.is_actionable(c)]
    assert "AB123456" in redacted_texts
    assert not [t for t in redacted_texts if t in ("Member ID", "Member", "ID")], redacted_texts


def test_table_headers_are_not_redacted(tmp_path):
    from app.session import AnonymizationSession

    rows = [((60, "Employee Name"), (220, "Employee ID"), (360, "Wages")),
            ((60, "John Smith"), (220, "12345"), (360, "85,000"))]
    s = AnonymizationSession(source_path=_pdf(tmp_path, rows)); s.analyse()
    got = [c.text for c in s.candidates if s.decisions.is_actionable(c)]
    assert "John Smith" in got and not any(h in got for h in ("Employee ID", "Employee Name", "Wages", "85,000"))


# -- evidence strength and proportionate AI objections ---------------------------------------

def test_a_weak_model_cannot_remove_strong_evidence_only_flag_it():
    _doc, result, _ = analysed("SSN: 123-45-6789")
    ssn = next(c for c in result.candidates if c.pii_type is PiiType.SSN)
    assert candidate_strength(ssn) == "strong"
    kept, removed, flagged = apply_ai_objections([ssn], {"123-45-6789"}, "a business fact")
    assert kept == [ssn] and removed == 0 and flagged == 1
    assert ssn.needs_review and "AI thinks" in ssn.review_reason


def test_a_weak_candidate_is_removed_when_the_ai_objects():
    doc, result, _ = analysed("Taxpayer Name: John Smith")
    line = doc.pages[0].lines[0]
    weak = Candidate(PiiType.PERSON, "Taxpayer", 0, line.rect_for(0, 8), line, 0, 8, 0.5, Source.COVERAGE,
                     [Evidence(Source.COVERAGE, "name-shaped line, no model hit", 0.5)])
    kept, removed, flagged = apply_ai_objections([weak], {"taxpayer"}, "form text")
    assert kept == [] and removed == 1 and flagged == 0


def test_a_user_decision_is_always_strong():
    doc, result, _ = analysed("Taxpayer Name: John Smith")
    line = doc.pages[0].lines[0]
    manual = Candidate(PiiType.PERSON, "John Smith", 0, line.rect_for(15, 25), line, 15, 25, 1.0, Source.MANUAL)
    assert candidate_strength(manual) == "strong"


def test_a_sentence_containing_a_label_word_is_not_a_label():
    """"account" matched the bank-account label pattern, so the whole sentence became a
    protected label and the client's name in it was never redacted."""
    for sentence in ("Mary Jones opened the account.",
                     "Mary Jones opened the account in March and called me about the refund."):
        doc, result, dm = analysed(sentence)
        assert [l.text for l in result.labels] == [], sentence
        assert ("Mary Jones", "PERSON") in [(c.text, c.pii_type.name) for c in result.candidates
                                            if dm.is_actionable(c)], sentence


def test_real_labels_are_still_labels():
    _doc, result, _dm = analysed("Account Number: 123456789\nName, address, and zip code\nJohn Smith")
    assert {"Account Number", "Name, address, and zip code"} <= {l.text for l in result.labels}


def test_a_name_in_prose_is_not_swallowed_into_a_sentence():
    got = redacted("Taxpayer Name: John Smith\nSmith & Wesson makes firearms.")
    assert not [t for t, _k in got if "firearms" in t or "makes" in t]


# -- several fields on one line (common on forms; used to hide values inside "labels") ----------

import pytest as _pytest


@_pytest.mark.parametrize("line,expected", [
    ("Date of birth 11/02/1979   Nationality: Brazil", {("11/02/1979", "DOB"), ("Brazil", "CITIZENSHIP")}),
    ("SSN 123-45-6789   Phone: 408-555-0198", {("123-45-6789", "SSN"), ("408-555-0198", "PHONE")}),
    ("Name: John Smith   SSN: 123-45-6789   Phone: 408-555-0198",
     {("John Smith", "PERSON"), ("123-45-6789", "SSN"), ("408-555-0198", "PHONE")}),
    ("Taxpayer name: John Smith     Total tax: $12,500", {("John Smith", "PERSON")}),
])
def test_each_value_on_a_multi_field_line_gets_its_own_label_and_type(line, expected):
    assert set(redacted(line)) == expected


def test_a_value_inside_the_first_label_span_is_not_swallowed_as_label_text():
    _doc, result, _dm = analysed("Date of birth 11/02/1979   Nationality: Brazil")
    assert {l.text for l in result.labels} == {"Date of birth", "Nationality"}
    assert not [w for w in result.warnings if "INCOMPLETE" in w]


def test_a_total_on_the_same_line_is_never_pulled_into_the_name():
    got = redacted("Taxpayer name: John Smith     Total tax: $12,500")
    assert [t for t, _k in got] == ["John Smith"], got


def test_an_ai_finding_for_a_value_beside_another_field_is_not_rejected_as_label_text():
    """The real-model trace: the AI found the date of birth, and the gate threw it away
    because the whole line had been registered as one label."""
    from app.detection.auditor import AuditFinding
    from tests.test_ai_proposals import run

    _d, result, _dm, _f = run("Date of birth 11/02/1979   Nationality: Brazil",
                              [AuditFinding("11/02/1979", "DOB", "")])
    assert any(c.text == "11/02/1979" and c.pii_type.name == "DOB" for c in result.candidates)


def test_a_line_of_key_value_pairs_is_not_a_table_header():
    """"Date of birth 11/02/1979   Nationality: Brazil" has two labels on one line, but
    it is not a header row; treating it as one typed "Badge no. 77231" as a DOB."""
    got = dict(redacted("Date of birth 11/02/1979   Nationality: Brazil\nBorn in: Recife\nBadge no. 77231"))
    assert got.get("77231") == "EMPLOYEE_ID", got
    assert got.get("11/02/1979") == "DOB"


def test_a_real_header_row_still_types_its_columns(tmp_path):
    from app.session import AnonymizationSession

    rows = [((60, "Employee Name"), (220, "Employee ID"), (360, "Wages")),
            ((60, "John Smith"), (220, "12345"), (360, "85,000"))]
    s = AnonymizationSession(source_path=_pdf(tmp_path, rows)); s.analyse()
    types = {c.text: c.pii_type.name for c in s.candidates if s.decisions.is_actionable(c)}
    assert types.get("12345") == "EMPLOYEE_ID" and types.get("John Smith") == "PERSON"
