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


def test_unclassified_value_line_is_flagged_not_silently_accepted(pdfs):
    """Section 86: no silent false negatives."""
    s = analysed(pdfs["stacked"])
    unclassified = [c for c in s.candidates if c.pii_type is PiiType.UNCLASSIFIED_GROUP_VALUE]
    assert unclassified
    assert all(c.needs_review for c in unclassified)
    assert all(s.decisions.state(c) is DecisionState.UNREVIEWED for c in unclassified)


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
