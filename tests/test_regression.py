"""Regression tests for the failure modes listed in spec section 81.

Every bug found during development becomes a case here. All data is synthetic.
"""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf
import pytest

from app.decisions.manager import DecisionState
from app.detection.deterministic import financial_tokens, load_rules
from app.detection.types import PiiType, Source
from app.entities.registry import EntityRegistry, _shares_token
from app.pseudonymization.generator import generate
from app.session import AnonymizationSession
from tests import fixtures


@pytest.fixture(scope="session")
def pdfs(tmp_path_factory) -> dict[str, str]:
    return fixtures.build_all(tmp_path_factory.mktemp("fixtures"))


def analysed(path: str) -> AnonymizationSession:
    s = AnonymizationSession(source_path=path)
    s.analyse()
    return s


def processed(path: str, out_dir: Path, name: str):
    s = analysed(path)
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    out = str(out_dir / f"{name}.anon.pdf")
    apply_report, report = s.process(out)
    return s, out, apply_report, report


def text_of(path: str) -> str:
    doc = pymupdf.open(path)
    try:
        return "\n".join(p.get_text() for p in doc)
    finally:
        doc.close()


# --- stacked PII (sections 14, 81, 84) -------------------------------------


def test_stacked_field_captures_every_value_line(pdfs):
    """The canonical failure: detecting only the last line of a stacked field."""
    s = analysed(pdfs["stacked"])
    groups = [g for g in s.detection.groups if "zip" in g.label.text.lower()]
    assert groups, "the 'Name, address, and zip code' label was not recognised"
    group = groups[0]
    values = [ln.text.strip() for ln in group.value_lines]
    assert values == ["LJP", "Fremont, CA", "123"], values


def test_stacked_field_does_not_swallow_the_financial_line(pdfs):
    s = analysed(pdfs["stacked"])
    group = next(g for g in s.detection.groups if "zip" in g.label.text.lower())
    assert not any("123,456" in ln.text for ln in group.value_lines)


def test_stacked_field_all_lines_transformed(pdfs, tmp_path):
    _s, out, _a, report = processed(pdfs["stacked"], tmp_path, "stacked")
    body = text_of(out)
    for original in ("LJP", "Fremont, CA"):
        assert original not in body, f"{original!r} survived redaction"
    assert report.passed, report.failures


def test_unclassified_value_line_is_flagged_and_redacted_by_default(pdfs):
    """Sections 86 and 98: no silent false negatives, and fail safe.

    An unclassified value line inside a sensitive field is surfaced for review
    AND redacted unless the user says otherwise. Leaving it in the document
    would be the unsafe default for a de-identification tool.
    """
    s = analysed(pdfs["stacked"])
    unclassified = [c for c in s.candidates if c.pii_type is PiiType.UNCLASSIFIED_GROUP_VALUE]
    assert unclassified
    assert all(c.needs_review for c in unclassified)
    assert all(s.decisions.state(c) is DecisionState.ACCEPTED for c in unclassified)


def test_low_confidence_detections_are_redacted_by_default(pdfs):
    """The 987-65-4321 case: flagged, but still removed unless kept explicitly."""
    s = analysed(pdfs["table"])
    weak = next(c for c in s.candidates if c.normalized == "987-65-4321")
    assert weak.needs_review and weak.confidence < 0.9
    assert s.decisions.state(weak) is DecisionState.ACCEPTED


# --- label protection (sections 13, 85) ------------------------------------


def test_labels_are_preserved(pdfs, tmp_path):
    _s, out, _a, _r = processed(pdfs["form"], tmp_path, "form")
    body = text_of(out)
    for label in ("Name:", "Address:", "SSN:", "Email:", "Annual Salary:"):
        assert label in body, f"label {label!r} was destroyed"


def test_financial_value_under_a_non_pii_label_is_preserved(pdfs, tmp_path):
    _s, out, _a, _r = processed(pdfs["form"], tmp_path, "form_money")
    assert "$85,000" in text_of(out)


def test_taxable_income_is_never_a_target(pdfs):
    s = analysed(pdfs["stacked"])
    assert not any("123,456" in c.text for c in s.candidates)


# --- multi-line address (sections 21, 81) ----------------------------------


def test_multiline_address_fully_replaced(pdfs, tmp_path):
    """The 'Apartment 4B' bug: partial line coverage leaving ' 4B' behind."""
    _s, out, _a, report = processed(pdfs["form"], tmp_path, "form_addr")
    body = text_of(out)
    for fragment in ("123 Main Street", "Apartment", "4B", "Fremont"):
        assert fragment not in body, f"address fragment {fragment!r} survived"
    assert report.passed, report.failures


# --- paragraph prose (sections 20, 83) -------------------------------------


def test_paragraph_pii_replaced_and_amount_preserved(pdfs, tmp_path):
    _s, out, _a, report = processed(pdfs["paragraph"], tmp_path, "para")
    body = text_of(out)
    for gone in ("John Smith", "john@example.com", "555) 123-4567", "123-45-6789"):
        assert gone not in body, f"{gone!r} survived"
    assert "$18,450.00" in body
    assert "ABC Company" in body, "a generic organisation name should be preserved"
    assert report.passed, report.failures


def test_phone_area_code_is_not_treated_as_currency():
    """Regression: '(555)' was matching the accounting-negative money pattern."""
    tokens = financial_tokens("call (555) 123-4567 owing $18,450.00 and (1,234.00)")
    assert "$18,450.00" in tokens
    assert "(1,234.00)" in tokens
    assert not any("555" in t for t in tokens)


# --- tables (sections 22, 81) ----------------------------------------------


def test_table_identities_and_ssns_detected_salaries_kept(pdfs, tmp_path):
    s = analysed(pdfs["table"])
    found = {c.normalized for c in s.candidates}
    assert "John Smith" in found and "Jane Doe" in found
    assert "123-45-6789" in found and "987-65-4321" in found

    _s2, out, _a, _r = processed(pdfs["table"], tmp_path, "table")
    body = text_of(out)
    assert "$85,000" in body and "$92,000" in body
    assert "Employee" in body and "SSN" in body and "Salary" in body


def test_ssn_failing_structural_validation_is_reviewed_not_dropped(pdfs):
    """Regression: 987-65-4321 fails the SSN area check and was being deleted."""
    s = analysed(pdfs["table"])
    c = next(c for c in s.candidates if c.normalized == "987-65-4321")
    assert c.needs_review
    assert "structural validation" in c.review_reason
    assert c.confidence < 0.9


def test_column_header_provides_context_to_later_rows(pdfs):
    from app.detection.deterministic import _context_window
    from app.document.provider import NativePdfTextProvider

    doc = NativePdfTextProvider().load(pdfs["table"])
    lines_by_page = {p.number: p.lines for p in doc.pages}
    line = next(ln for ln in doc.pages[0].lines if "987" in ln.text)
    assert "ssn" in _context_window(line, lines_by_page)


# --- entity registry (section 25) ------------------------------------------


def test_same_value_maps_to_same_pseudonym():
    r = EntityRegistry(scope="t")
    a = r.pseudonym_for(PiiType.PERSON, "John Smith")
    b = r.pseudonym_for(PiiType.PERSON, "john  smith")
    assert a == b


def test_pseudonyms_are_deterministic_across_runs():
    a = EntityRegistry(scope="t").pseudonym_for(PiiType.PERSON, "John Smith")
    b = EntityRegistry(scope="t").pseudonym_for(PiiType.PERSON, "John Smith")
    assert a == b


def test_different_values_do_not_collide():
    r = EntityRegistry(scope="t")
    names = {r.pseudonym_for(PiiType.PERSON, n) for n in ("John Smith", "Jane Doe", "Ann Lee")}
    assert len(names) == 3


def test_pseudonym_never_reuses_a_token_of_the_original():
    """Regression: 'John Smith' -> 'John Glass' leaked the first name."""
    r = EntityRegistry(scope="t")
    for original in ("John Smith", "Mary Johnson", "Robert Brown", "Fremont, CA"):
        assert not _shares_token(original, r.pseudonym_for(PiiType.PERSON, original))


def test_short_token_stays_short():
    """Regression: 'LJP' became 'Elizabeth Hernandez' and wrecked the layout."""
    out = generate(PiiType.UNCLASSIFIED_GROUP_VALUE, "LJP")
    assert len(out) <= 6


def test_user_edit_overrides_and_persists():
    r = EntityRegistry(scope="t")
    r.pseudonym_for(PiiType.PERSON, "John Smith")
    r.override(PiiType.PERSON, "John Smith", "Alan Turner")
    assert r.pseudonym_for(PiiType.PERSON, "John Smith") == "Alan Turner"


# --- decisions (sections 26-28) --------------------------------------------


def test_skip_preserves_the_value(pdfs, tmp_path):
    s = analysed(pdfs["form"])
    ssn = next(c for c in s.candidates if c.pii_type is PiiType.SSN)
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    s.decisions.skip([ssn])
    out = str(tmp_path / "skip.pdf")
    _a, report = s.process(out)
    assert "123-45-6789" in text_of(out)
    assert report.passed, report.failures


def test_apply_to_all_and_undo(pdfs):
    s = analysed(pdfs["paragraph"])
    target = next(c for c in s.candidates if c.pii_type is PiiType.PERSON)
    changed = s.decisions.apply_to_all(s.candidates, target, DecisionState.SKIPPED)
    assert changed and all(s.decisions.state(c) is DecisionState.SKIPPED for c in changed)
    assert s.decisions.undo()
    assert s.decisions.state(target) is not DecisionState.SKIPPED


def test_edit_replacement_is_used(pdfs, tmp_path):
    s = analysed(pdfs["form"])
    person = next(c for c in s.candidates if c.pii_type is PiiType.PERSON)
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    s.decisions.edit([person], "Casey Doyle")
    out = str(tmp_path / "edit.pdf")
    s.process(out)
    assert "Casey Doyle" in text_of(out)


# --- redaction and export (sections 34-36, 44) -----------------------------


def test_original_file_is_never_overwritten(pdfs):
    s = analysed(pdfs["form"])
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    with pytest.raises(ValueError):
        s.process(pdfs["form"])


def test_replacement_text_is_real_red_text(pdfs, tmp_path):
    s, out, _a, _r = processed(pdfs["form"], tmp_path, "red")
    doc = pymupdf.open(out)
    try:
        colors = {
            int(span["color"])
            for page in doc
            for b in page.get_text("dict")["blocks"]
            for line in b.get("lines", [])
            for span in line["spans"]
        }
    finally:
        doc.close()
    assert 0xFF0000 in colors, "no red text spans in the output"


def test_no_replacement_wraps_to_a_second_line(pdfs, tmp_path):
    _s, _out, apply_report, _r = processed(pdfs["form"], tmp_path, "wrap")
    assert not apply_report.overflowed


def test_preview_and_export_use_the_same_plan(pdfs, tmp_path):
    """Section 33: one transformation plan, no divergent preview logic."""
    s = analysed(pdfs["form"])
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    plan_a, plan_b = s.plan(), s.plan()
    assert [t.replacement for t in plan_a.targets] == [t.replacement for t in plan_b.targets]
    assert s.preview_transformed(0)[:8] == b"\x89PNG\r\n\x1a\n"


# --- verification (sections 37-41) -----------------------------------------


def test_verification_runs_against_the_saved_file(pdfs, tmp_path):
    _s, out, _a, report = processed(pdfs["form"], tmp_path, "verify")
    names = {c.name for c in report.checks}
    assert {"accepted originals removed", "replacement text is red", "no partial redaction"} <= names
    assert report.output_path == out


def test_verifier_detects_a_tampered_output(pdfs, tmp_path):
    """A verifier that cannot fail is worthless."""
    from app.verification.verifier import verify

    s = analysed(pdfs["form"])
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    plan = s.plan()
    untouched = str(tmp_path / "untouched.pdf")
    pymupdf.open(pdfs["form"]).save(untouched)
    report = verify(plan, untouched)
    assert not report.passed
    assert any(c.name == "accepted originals removed" for c in report.failures)


def test_short_numeric_value_inside_currency_is_not_a_false_survivor(pdfs, tmp_path):
    """Regression: value '123' was reported as surviving inside '$123,456'."""
    from app.verification.verifier import _contains_value

    assert not _contains_value("taxable income: $123,456", "123")
    assert _contains_value("zip 123 here", "123")


# --- unsupported content (sections 6, 42, 87) ------------------------------


def test_scanned_page_is_reported_not_silently_ignored(pdfs, tmp_path):
    s = analysed(pdfs["scanned"])
    assert s.document.ocr_required_pages == [0]
    assert any("OCR REQUIRED" in w for w in s.detection.warnings)
    out = str(tmp_path / "scan.pdf")
    _a, report = s.process(out)
    assert not report.passed
    assert any(c.name == "all pages analysed" for c in report.failures)


def test_status_never_claims_plain_verified(pdfs, tmp_path):
    """Section 87: no false certainty."""
    _s, _out, _a, report = processed(pdfs["form"], tmp_path, "status")
    assert report.status == "EXPORT VERIFIED"


# --- rules file -------------------------------------------------------------


def test_rules_file_loads_and_all_patterns_compile():
    rs = load_rules()
    assert len(rs.rules) > 20 and rs.labels and rs.non_pii_labels
    for rule in rs.rules:
        assert isinstance(rule.regex, re.Pattern)


def test_manual_pii_uses_the_same_pipeline(pdfs, tmp_path):
    s = analysed(pdfs["paragraph"])
    page = s.document.pages[0]
    line = page.lines[0]
    added = s.add_manual(0, line.bbox)
    assert added is not None and added.source is Source.MANUAL
    assert s.decisions.state(added) is DecisionState.MANUALLY_ADDED
    plan = s.plan()
    assert any(t.candidate_id == added.id for t in plan.targets)


# --- detection recall on realistic form layouts ----------------------------


def _hard_pdf(path: Path) -> str:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 10)
    rows = [
        (60, 710, "Your first name and middle initial"),
        (300, 710, "Last name"),
        (60, 696, "MARIA T"),
        (300, 696, "GONZALEZ-REYES"),
        (60, 674, "Home address (number and street). If you have a P.O. box, see instructions."),
        (60, 660, "4820 Camino Del Rio Apt 12C"),
        (60, 638, "City, town, or post office, state, and ZIP code"),
        (60, 624, "San Jose, CA 95129"),
        (60, 600, "Filing status: Married filing jointly"),
        (60, 578, "Occupation: Software Engineer"),
        (60, 556, "Daytime phone 408.555.0198   Email m.gonzalez@fastmail.co"),
        (60, 534, "Bank routing 121000248   Account 000123456789"),
        (60, 512, "1  Wages, salaries, tips  ................  $412,890"),
        (60, 498, "11 Adjusted gross income  ...............  $438,117"),
    ]
    for x, y, txt in rows:
        c.drawString(x, y, txt)
    c.save()
    return str(path)


@pytest.fixture(scope="session")
def hard_pdf(tmp_path_factory) -> str:
    return _hard_pdf(tmp_path_factory.mktemp("hard") / "hard.pdf")


def test_verbose_form_labels_are_recognised(hard_pdf):
    """Real labels are sentences: 'Your first name and middle initial'."""
    s = analysed(hard_pdf)
    labels = " | ".join(g.label.text.lower() for g in s.detection.groups)
    assert "first name" in labels
    assert "home address" in labels


def test_all_caps_and_lone_surname_names_detected(hard_pdf):
    s = analysed(hard_pdf)
    found = {c.normalized for c in s.candidates}
    assert "MARIA T" in found
    assert "GONZALEZ-REYES" in found


def test_full_street_line_captured_not_truncated(hard_pdf):
    """The street regex stops at 'Apt'; the field group must cover the rest."""
    s = analysed(hard_pdf)
    assert any("12C" in c.text for c in s.candidates)


def test_contact_and_bank_details_on_a_value_line_survive_label_matching(hard_pdf):
    """Regression: the word 'phone' turned the whole value line into a label."""
    s = analysed(hard_pdf)
    types = {c.pii_type for c in s.candidates}
    assert PiiType.EMAIL in types
    assert PiiType.PHONE in types
    assert PiiType.BANK_ACCOUNT in types or PiiType.ROUTING_NUMBER in types


def test_business_facts_under_unknown_labels_are_left_alone(hard_pdf):
    """Anonymise WHO, not WHAT. Occupation and filing status are not identity."""
    s = analysed(hard_pdf)
    text = " ".join(c.text for c in s.candidates)
    assert "Software Engineer" not in text
    assert "Married filing jointly" not in text
    assert "412,890" not in text and "438,117" not in text


def test_form_vocabulary_is_never_a_person(hard_pdf):
    """Regression: spaCy tagged 'Daytime' as a PERSON."""
    s = analysed(hard_pdf)
    people = {c.normalized for c in s.candidates if c.pii_type is PiiType.PERSON}
    assert not {"Daytime", "Preparer", "Occupation", "Wages"} & people


# --- GLiNER layer ----------------------------------------------------------


def _gliner():
    from app.detection.gliner import GlinerDetector

    detector = GlinerDetector()
    if not detector.available:
        pytest.skip("GLiNER weights not fetched; run buildtools/fetch_models.py")
    return detector


def test_gliner_returns_the_label_that_was_asked_for():
    """The point of a label-conditioned model: no more guessing the type."""
    detector = _gliner()
    words = "John Smith lives at 123 Main Street and his SSN is 123-45-6789 .".split()
    spans = {" ".join(words[s.start_word:s.end_word + 1]): s.label for s in detector.predict(words)}
    assert any("person" in label for value, label in spans.items() if "John" in value)
    assert any("street" in label for value, label in spans.items() if "Main" in value)


def test_a_currency_amount_is_never_pii_whatever_the_model_says(tmp_path):
    """Regression: GLiNER labelled '$85,000' a date of birth at 0.62."""
    s = analysed(fixtures.table_pdf(tmp_path / "t.pdf"))
    for candidate in s.candidates:
        assert "$" not in candidate.text, f"{candidate.text!r} flagged as {candidate.pii_type.value}"


def test_gliner_identifies_money_as_a_negative_class():
    """Used to veto competing detections so figures survive."""
    detector = _gliner()
    words = "Adjusted gross income was $ 438,117 for the year .".split()
    labels = [s.label for s in detector.predict(words)]
    assert "money amount" in labels


def test_field_value_is_never_partially_redacted(tmp_path):
    """The street regex stops at 'Apt'; the field must still go as a unit."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    path = tmp_path / "apt.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 10)
    c.drawString(60, 700, "Home address (number and street)")
    c.drawString(60, 686, "4820 Camino Del Rio Apt 12C")
    c.drawString(60, 660, "1  Wages  ......  $412,890")
    c.save()

    s = analysed(str(path))
    street = [c for c in s.candidates if "4820" in c.text]
    assert street, "the street line was not detected at all"
    assert "12C" in street[0].text, f"partial value detected: {street[0].text!r}"


def test_financial_figures_are_not_detected_as_pii(tmp_path):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    path = tmp_path / "money.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 10)
    for i, line in enumerate(
        ["1  Wages, salaries, tips  ......  $412,890",
         "11 Adjusted gross income  .....  $438,117",
         "Occupation: Software Engineer",
         "Filing status: Married filing jointly"]
    ):
        c.drawString(60, 700 - i * 16, line)
    c.save()

    s = analysed(str(path))
    text = " ".join(c.text for c in s.candidates)
    for preserved in ("412,890", "438,117", "Software Engineer", "Married filing jointly"):
        assert preserved not in text, f"{preserved!r} was flagged as PII"


# --- joint / compound names and propagation --------------------------------


def _joint_pdf(path: Path) -> str:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 10)
    rows = [
        (60, 720, "Taxpayer name"), (60, 706, "JP & ML"),
        (60, 684, "Spouse first name and last name"),
        (60, 670, "John and Mary Gonzalez-Reyes"),
        (60, 648, "Preparer notes"),
        (60, 634, "Reviewed the return for Gonzalez-Reyes this week."),
        (60, 600, "1  Wages, salaries, tips  ......  $412,890"),
    ]
    for x, y, text in rows:
        c.drawString(x, y, text)
    c.save()
    return str(path)


@pytest.fixture(scope="session")
def joint_pdf(tmp_path_factory) -> str:
    return _joint_pdf(tmp_path_factory.mktemp("joint") / "joint.pdf")


def test_ampersand_joint_name_becomes_two_people(joint_pdf):
    """'JP & ML' is two taxpayers, not one entity sharing one pseudonym."""
    s = analysed(joint_pdf)
    people = {c.normalized for c in s.candidates if c.pii_type is PiiType.PERSON}
    assert "JP" in people and "ML" in people
    assert "JP & ML" not in people


def test_written_and_joint_name_becomes_two_people(joint_pdf):
    s = analysed(joint_pdf)
    people = {c.normalized for c in s.candidates if c.pii_type is PiiType.PERSON}
    assert "John" in people
    assert any("Mary" in p for p in people)
    assert "John and Mary Gonzalez-Reyes" not in people


def test_joint_spouses_get_different_pseudonyms(joint_pdf, tmp_path):
    s = analysed(joint_pdf)
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    plan = s.plan()
    jp = next(t.replacement for t in plan.targets if t.original.strip() == "JP")
    ml = next(t.replacement for t in plan.targets if t.original.strip() == "ML")
    assert jp != ml, "both spouses received the same pseudonym"


def test_a_value_group_stops_at_the_next_field(joint_pdf):
    """Regression: the spouse field swallowed 'Preparer notes' and its value."""
    s = analysed(joint_pdf)
    spouse = next(g for g in s.detection.groups if "Spouse" in g.label.text)
    assert len(spouse.value_lines) == 1
    assert "Preparer" not in " ".join(l.text for l in spouse.value_lines)


def test_surname_is_caught_in_free_text_elsewhere(joint_pdf):
    """The name reappears in prose with no label; it must still be found."""
    s = analysed(joint_pdf)
    assert any("Gonzalez-Reyes" in c.text for c in s.candidates)


def test_propagation_finds_a_repeat_with_no_local_context(tmp_path):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    path = tmp_path / "repeat.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 10)
    c.drawString(60, 720, "Taxpayer name")
    c.drawString(60, 706, "Marisol Etxeberria")
    c.drawString(60, 600, "Payer            Amount")
    c.drawString(60, 586, "Marisol Etxeberria   $1,110")
    c.save()

    s = analysed(str(path))
    hits = [c for c in s.candidates if "Etxeberria" in c.text]
    assert len(hits) >= 2, "the repeat in the table was not found"


# --- roster, mapping workbook, LLM auditor ---------------------------------


def test_mapping_workbook_lists_original_and_pseudonym(tmp_path):
    from openpyxl import load_workbook

    s = analysed(fixtures.form_pdf(tmp_path / "f.pdf"))
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    s.process(str(tmp_path / "out.pdf"))
    assert s.mapping_file, "no mapping workbook was written"

    sheet = load_workbook(s.mapping_file)["Mapping"]
    assert "CONFIDENTIAL" in str(sheet["A1"].value)
    assert [c.value for c in sheet[2]][:3] == ["Type", "Original value", "Pseudonym"]
    rows = [(r[0], r[1], r[2]) for r in sheet.iter_rows(min_row=3, values_only=True) if r[0]]
    assert rows
    assert any(original == "John Smith" for _t, original, _p in rows)
    for _t, original, pseudonym in rows:
        assert original != pseudonym


def test_mapping_is_not_written_beside_the_anonymized_pdf(tmp_path):
    """It must not be picked up by "attach everything in this folder"."""
    s = analysed(fixtures.form_pdf(tmp_path / "f.pdf"))
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    out = tmp_path / "out.pdf"
    s.process(str(out))
    mapping = Path(s.mapping_file)
    assert mapping.parent != out.parent
    assert "DO-NOT-SEND" in mapping.name


def test_roster_keeps_one_client_consistent_across_documents(tmp_path):
    """A batch must read coherently: same person, same pseudonym, every file."""
    from app.entities.roster import ClientRoster
    from app.session import AnonymizationSession

    roster = ClientRoster()
    pseudonyms = []
    for index in (1, 2):
        source = fixtures.form_pdf(tmp_path / f"doc{index}.pdf")
        session = AnonymizationSession(source_path=source, roster=roster)
        session.analyse()
        session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)
        plan = session.plan()
        pseudonyms.append(
            next(t.replacement for t in plan.targets if t.original.strip() == "John Smith")
        )
    assert pseudonyms[0] == pseudonyms[1], "the same client got two pseudonyms"


def test_roster_survives_a_round_trip_through_the_workbook(tmp_path):
    from app.detection.types import PiiType
    from app.entities.roster import ClientRoster

    roster = ClientRoster()
    roster.record(PiiType.PERSON, "Marisol Etxeberria", "Julie Baker", "doc1.pdf")
    path = tmp_path / "roster.xlsx"
    roster.save(path)

    reloaded = ClientRoster.load(path)
    assert reloaded.pseudonym_for(PiiType.PERSON, "marisol  etxeberria") == "Julie Baker"


def test_name_suffix_is_inside_the_detected_span(tmp_path):
    """Regression: 'MARIA T GONZALEZ-REYES Jr.' left 'Jr.' behind."""
    from app.detection.heuristics import looks_like_person

    for name in ("MARIA T GONZALEZ-REYES JR.", "Smith, John Jr.", "Dr. John Smith III"):
        assert looks_like_person(name)[0], name


def test_demographic_categories_are_detected(tmp_path):
    """DOB, citizenship, birthplace and sex were missing entirely."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    path = tmp_path / "demog.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 10)
    rows = [
        (720, "Date of birth"), (706, "04/11/1979"),
        (684, "Country of citizenship"), (670, "Mexico"),
        (648, "Place of birth"), (634, "Guadalajara, Jalisco"),
        (612, "Sex"), (598, "F"),
        (576, "Occupation"), (562, "Software Engineer"),
    ]
    for y, text in rows:
        c.drawString(60, y, text)
    c.save()

    s = analysed(str(path))
    found = {c.text.strip() for c in s.candidates}
    for value in ("04/11/1979", "Mexico", "Guadalajara, Jalisco", "F"):
        assert value in found, f"{value!r} not detected"
    assert "Software Engineer" not in found


def test_gender_pseudonym_always_differs_from_the_original():
    from app.detection.types import PiiType
    from app.pseudonymization.generator import generate

    for original in ("M", "F", "male", "female"):
        assert generate(PiiType.GENDER, original).lower() != original.lower()


@pytest.mark.slow
def test_llm_auditor_finds_categories_no_rule_covers():
    from app.detection.auditor import LlmAuditor

    auditor = LlmAuditor()
    if not auditor.available:
        pytest.skip("LLM weights not fetched")
    text = (
        "Taxpayer name: MARIA T GONZALEZ-REYES\n"
        "Date of birth 04/11/1979   Country of citizenship: Mexico\n"
        "Place of birth: Guadalajara, Jalisco   Sex: F\n"
        "Occupation: Software Engineer\n"
        "1  Wages, salaries, tips ......... $412,890\n"
    )
    missed, wrong = auditor.audit(text, ["MARIA T GONZALEZ-REYES"])
    blob = " ".join(f.text for f in missed).lower()
    assert "1979" in blob and "mexico" in blob
    assert "412,890" not in blob, "the auditor flagged a financial figure"


def test_a_candidate_overlapping_a_currency_amount_is_dropped(tmp_path):
    """Regression: '123' matched inside '$123,456' and corrupted the figure.

    Word-boundary matching does not protect a number inside a currency amount,
    because '$' and ',' are not word characters. CI caught this on the stacked
    fixture: financial values preserved -> FAIL.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    path = tmp_path / "money_overlap.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 10)
    for y, text in (
        (720, "Name, address, and zip code"),
        (706, "LJP"),
        (692, "Fremont, CA"),
        (678, "123"),
        (640, "Taxable income: $123,456"),
    ):
        c.drawString(72, y, text)
    c.save()

    s = analysed(str(path))
    for candidate in s.candidates:
        assert "123,456" not in candidate.line.text or "Taxable" not in candidate.line.text, (
            f"{candidate.text!r} was flagged on the financial line"
        )

    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    out = str(tmp_path / "out.pdf")
    _apply, report = s.process(out)
    assert "$123,456" in text_of(out)
    financial = next(c for c in report.checks if c.name == "financial values preserved")
    assert financial.passed, financial.detail


def test_the_financial_guard_runs_after_every_pass_that_adds_candidates():
    """Regression: the guard ran before propagation and the audit, so both bypassed it.

    Ordering is the whole bug here - the check itself was correct.
    """
    source = (Path(__file__).resolve().parents[1] / "app" / "detection" / "engine.py").read_text()
    guard = "_drop_financial_values(candidates)"
    audit = "audit_document(doc, candidates)"
    propagation = "propagate(doc, candidates, subjects)"

    assert source.count(guard) >= 2, "the guard must run again after the later passes"
    last_guard = source.rfind(guard)
    assert last_guard > source.rfind(audit), "the guard must run after the audit pass"
    assert last_guard > source.rfind(propagation), "the guard must run after propagation"


# --- partial redaction on unlabelled lines ---------------------------------


def _lines_pdf(path: Path, lines: list[str]) -> str:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 10)
    y = 720
    for text in lines:
        c.drawString(72, y, text)
        # 14pt matches real form leading. Wider spacing exceeds the field-group
        # gap threshold, so a label would not bind its value and the fixture
        # would be testing something the layout never does.
        y -= 14
    c.save()
    return str(path)


def test_spaced_initials_are_redacted_whole(tmp_path):
    """Reported: 'LJ P' redacted only 'LJ'."""
    s = analysed(_lines_pdf(tmp_path / "a.pdf", ["LJ P", "1  Wages ...... $412,890"]))
    hit = next((c for c in s.candidates if "LJ" in c.text), None)
    assert hit is not None, "the name was not detected at all"
    assert hit.text.strip() == "LJ P", f"partial value: {hit.text!r}"


def test_city_state_and_number_are_redacted_whole(tmp_path):
    """Reported: 'Fremont, CA 1234' redacted only '1234'."""
    s = analysed(_lines_pdf(tmp_path / "b.pdf", ["Fremont, CA 1234", "1  Wages ...... $412,890"]))
    hit = next((c for c in s.candidates if "1234" in c.text), None)
    assert hit is not None
    assert "Fremont" in hit.text, f"partial value: {hit.text!r}"


def test_widening_never_swallows_a_figure(tmp_path):
    s = analysed(_lines_pdf(tmp_path / "c.pdf", ["LJ P", "Refund due 4,820.00", "$412,890"]))
    for candidate in s.candidates:
        assert "412,890" not in candidate.text
        assert "4,820" not in candidate.text


def test_widening_leaves_prose_alone(tmp_path):
    """On a sentence the value is a span, not the line."""
    s = analysed(
        _lines_pdf(tmp_path / "d.pdf", ["John Smith submitted the report to ABC Company."])
    )
    for candidate in s.candidates:
        assert "submitted" not in candidate.text
        assert "ABC Company" not in candidate.text


def test_widening_does_not_merge_joint_names(tmp_path):
    """'JP & ML' is two people; the separator must not pull them together."""
    s = analysed(_lines_pdf(tmp_path / "e.pdf", ["Taxpayer name", "JP & ML"]))
    people = {c.normalized for c in s.candidates if c.pii_type is PiiType.PERSON}
    assert "JP" in people and "ML" in people
    assert not any("&" in p for p in people)


# --- manual additions ------------------------------------------------------


def test_user_can_add_a_missed_value_with_a_custom_replacement(tmp_path):
    s = analysed(_lines_pdf(tmp_path / "m.pdf", ["Acme Holdings LLC", "1 Wages ...... $412,890"]))
    added = s.add_manual_text("Acme Holdings LLC", replacement="Northwind Trading LLC")
    assert added
    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    out = str(tmp_path / "out.pdf")
    s.process(out)
    body = text_of(out)
    assert "Acme Holdings" not in body
    assert "Northwind Trading LLC" in body
    assert "$412,890" in body


def test_cascade_finds_the_value_on_every_page(tmp_path):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    path = tmp_path / "multi.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    for _ in range(3):
        c.setFont("Helvetica", 10)
        c.drawString(72, 720, "Prepared for Vantage Partners")
        c.showPage()
    c.save()

    s = analysed(str(path))
    added = s.add_manual_text("Vantage Partners", cascade=True)
    assert {c.page_no for c in added} == {0, 1, 2}


def test_without_cascade_only_the_first_page_is_touched(tmp_path):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    path = tmp_path / "multi2.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    for _ in range(2):
        c.setFont("Helvetica", 10)
        c.drawString(72, 720, "Prepared for Vantage Partners")
        c.showPage()
    c.save()

    s = analysed(str(path))
    added = s.add_manual_text("Vantage Partners", cascade=False)
    assert {c.page_no for c in added} == {0}


def test_apply_to_same_off_adds_a_single_occurrence(tmp_path):
    s = analysed(
        _lines_pdf(tmp_path / "rep.pdf", ["Vantage Partners", "Vantage Partners again"])
    )
    added = s.add_manual_text("Vantage Partners", apply_to_same=False)
    assert len(added) == 1


def test_manual_additions_are_already_reviewed(tmp_path):
    """The user just told us; it does not belong back in their review queue."""
    s = analysed(_lines_pdf(tmp_path / "r.pdf", ["Acme Holdings LLC"]))
    added = s.add_manual_text("Acme Holdings LLC")
    assert added and all(s.decisions.is_reviewed(c) for c in added)


def test_business_names_are_detected_and_typed_from_their_label(tmp_path):
    """Reported: business names sometimes missed, and typed as people."""
    s = analysed(
        _lines_pdf(
            tmp_path / "biz.pdf",
            ["Business name", "Acme Holdings LLC",
             "Employer name", "Vantage Partners Inc.",
             "1  Gross receipts  ......  $1,204,880"],
        )
    )
    found = {c.normalized: c.pii_type for c in s.candidates}
    assert "Acme Holdings LLC" in found, "business name not detected"
    assert found["Acme Holdings LLC"] is PiiType.ORG_PRIVATE, "typed as the wrong thing"

    s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    out = str(tmp_path / "out.pdf")
    s.process(out)
    body = text_of(out)
    assert "Acme Holdings" not in body
    assert "$1,204,880" in body
    assert "Business name" in body


def test_a_company_pseudonym_keeps_its_entity_suffix(tmp_path):
    """LLC vs Inc carries tax meaning the receiving analysis needs."""
    from app.pseudonymization.generator import generate

    assert generate(PiiType.ORG_PRIVATE, "Acme Holdings LLC").endswith("LLC")
    assert generate(PiiType.ORG_PRIVATE, "Vantage Partners Inc.").rstrip(".").endswith("Inc")


def test_review_model_suggestions_are_not_applied_unasked():
    """Reported: random replacements on text that was not PII.

    The audit pass is the only layer that proposes values nothing else saw. Its
    suggestions are surfaced for a decision rather than redacted by default.
    """
    from app.decisions.manager import DecisionManager, DecisionState
    from app.detection.types import Candidate, PiiType, Source
    from app.document.model import Char, Line, Span

    char = Char(text="X", bbox=(0, 0, 5, 10))
    span = Span(text="X", bbox=(0, 0, 5, 10), font="helv", size=10, color=0, chars=[char])
    line = Line(page_no=0, block_no=0, line_no=0, spans=[span], text="X",
                offsets=[char], bbox=(0, 0, 5, 10))

    def make(source: Source) -> Candidate:
        return Candidate(
            pii_type=PiiType.PERSON, text="X", page_no=0, rect=(0, 0, 5, 10),
            line=line, start=0, end=1, confidence=0.7, source=source,
        )

    from_rule, from_audit = make(Source.REGEX), make(Source.AUDIT)
    from_audit.line = Line(page_no=0, block_no=0, line_no=1, spans=[span], text="X",
                           offsets=[char], bbox=(0, 0, 5, 10))

    manager = DecisionManager()
    manager.register([from_rule, from_audit])
    assert manager.state(from_rule) is DecisionState.ACCEPTED
    assert manager.state(from_audit) is DecisionState.SKIPPED


# --- pseudonym quality -----------------------------------------------------


def test_a_label_inside_a_value_survives():
    """Reported: 'Nguyen, Tuyet PTIN P01234567' replaced PTIN with a surname.

    PTIN names the field. Destroying it is the same failure as redacting a
    label, just hidden inside a value.
    """
    from app.pseudonymization.generator import generate

    out = generate(PiiType.UNCLASSIFIED_GROUP_VALUE, "Nguyen, Tuyet PTIN P01234567")
    assert "PTIN" in out
    assert "Nguyen" not in out and "Tuyet" not in out
    assert "P01234567" not in out
    assert re.search(r"[A-Z]\d{8}", out), f"the identifier lost its shape: {out!r}"


def test_every_component_of_a_place_survives():
    """Reported: 'Fremont, CA 1234' dropped the trailing number entirely."""
    from app.pseudonymization.generator import generate

    out = generate(PiiType.UNCLASSIFIED_GROUP_VALUE, "Fremont, CA 1234")
    assert "Fremont" not in out
    assert re.search(r",\s*[A-Z]{2}\s+\d{4}", out), f"a component was lost: {out!r}"


def test_initials_are_not_mistaken_for_a_state_code():
    from app.pseudonymization.generator import generate

    out = generate(PiiType.UNCLASSIFIED_GROUP_VALUE, "LJ P")
    assert len(out.split()) == 2
    assert "TN" not in out and "CA" not in out


def test_no_pseudonym_is_unreadable_noise():
    """Reported: 'asdiauguw adsasd asd'. Words must be replaced by words."""
    from app.pseudonymization.generator import generate

    for value in ("Nguyen, Tuyet", "Acme Holdings LLC", "Guadalajara, Jalisco", "LJ P"):
        out = generate(PiiType.UNCLASSIFIED_GROUP_VALUE, value)
        for token in out.split():
            letters = "".join(ch for ch in token if ch.isalpha())
            if len(letters) < 4:
                continue
            vowels = sum(1 for ch in letters.lower() if ch in "aeiou")
            assert vowels, f"unpronounceable output {out!r}"


def test_a_date_stays_a_date():
    from app.pseudonymization.generator import generate

    out = generate(PiiType.UNCLASSIFIED_GROUP_VALUE, "04/11/1979")
    assert re.fullmatch(r"\d{2}/\d{2}/\d{4}", out), out
    month, day, _year = out.split("/")
    assert 1 <= int(month) <= 12 and 1 <= int(day) <= 31, f"impossible date {out!r}"
