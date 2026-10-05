"""One person keeps one identity, however their name is written."""

from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.decisions.manager import DecisionState
from app.detection.types import PiiType
from app.pseudonymization.names import NameRegistry
from app.session import AnonymizationSession


def _pdf(path: Path, lines: list[str]) -> str:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 10)
    y = 720
    for line in lines:
        c.drawString(72, y, line)
        y -= 14
    c.save()
    return str(path)


def test_every_form_of_a_name_maps_consistently():
    registry = NameRegistry(scope="t")
    full = registry.pseudonym("John Smith")
    given, surname = full.split()

    assert registry.pseudonym("John") == given
    assert registry.pseudonym("Smith") == surname
    assert registry.pseudonym("Smith, John") == f"{surname}, {given}"
    assert registry.pseudonym("JOHN SMITH") == full.upper()


def test_a_joint_name_shares_the_surname():
    registry = NameRegistry(scope="t")
    joint = registry.pseudonym("John & Jenny Smith")
    assert " & " in joint

    left, right = joint.split(" & ")
    assert right.split()[-1] == registry.pseudonym("Smith")
    assert left != right.split()[0], "both halves got the same given name"
    assert registry.pseudonym("Jenny Smith").split()[-1] == right.split()[-1]


def test_and_is_handled_like_ampersand():
    registry = NameRegistry(scope="t")
    assert registry.pseudonym("Mark and Mich Smith").count(" and ") == 1


def test_titles_and_suffixes_pass_through():
    registry = NameRegistry(scope="t")
    out = registry.pseudonym("Mr. John A Smith Jr.")
    assert out.startswith("Mr. ") and out.endswith(" Jr.")
    assert " A " in out, "the middle initial was altered"


def test_a_pseudonym_never_reuses_a_real_name():
    registry = NameRegistry(scope="t")
    for real in ("John Smith", "Jenny Smith", "Mark Doyle"):
        registry.note_original(real)
    generated = {registry.pseudonym(n).lower() for n in ("John Smith", "Jenny", "Mark")}
    for token in ("john", "jenny", "smith", "mark", "doyle"):
        assert not any(token in g.split() for g in generated), token


def test_consistency_holds_across_pages(tmp_path):
    """Page 1 writes the full name; page 2 splits it into columns."""
    path = _pdf(
        tmp_path / "pages.pdf",
        ["First and last name", "John Smith", "Last name    First name",
         "Smith            John", "1  Wages ...... $412,890"],
    )
    session = AnonymizationSession(source_path=path)
    session.analyse()
    session.decisions.set_state(
        [c for c in session.candidates if c.pii_type is not PiiType.UNCLASSIFIED_GROUP_VALUE],
        DecisionState.ACCEPTED,
    )
    plan = session.plan()
    by_original = {t.original.strip(): t.replacement for t in plan.targets}

    full = by_original.get("John Smith")
    assert full, by_original
    given, surname = full.split()
    for original, replacement in by_original.items():
        if original == "John":
            assert replacement == given
        if original == "Smith":
            assert replacement == surname


def test_one_identifier_in_several_formats_gets_one_pseudonym(tmp_path):
    path = _pdf(
        tmp_path / "ids.pdf",
        ["Social security number", "123456789", "123-45-6789", "123 45 6789"],
    )
    session = AnonymizationSession(source_path=path)
    session.analyse()
    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)

    ssn = {t.replacement for t in session.plan().targets if t.pii_type is PiiType.SSN}
    assert len(ssn) == 1, ssn


def test_an_undashed_ssn_is_detected_without_a_label(tmp_path):
    path = _pdf(tmp_path / "bare.pdf", ["Prepared 2025", "123456789"])
    session = AnonymizationSession(source_path=path)
    session.analyse()
    assert any(c.pii_type is PiiType.SSN for c in session.candidates)


def test_an_invalid_nine_digit_number_is_not_an_ssn(tmp_path):
    """Structural validation is what makes an unlabelled match safe."""
    path = _pdf(tmp_path / "notssn.pdf", ["Reference 000123456"])
    session = AnonymizationSession(source_path=path)
    session.analyse()
    assert not any(c.normalized == "000123456" for c in session.candidates)


# --- the reported case, exactly -------------------------------------------


def test_joint_name_and_address_are_not_swallowed_into_one_group(tmp_path):
    """Reported: 'Mark & Macey Lang' with an address not detected.

    'Taxpayer name' was absorbing the street and city/state/zip lines below it
    and typing all three PERSON, which hid the address as a mistyped value
    rather than a missed one.
    """
    path = _pdf(
        tmp_path / "reported.pdf",
        ["Taxpayer name", "Mark & Macey Lang", "4049 Caribbean Cmn",
         "Fremont, CA 94538", "1  Wages ...... $412,890"],
    )
    session = AnonymizationSession(source_path=path)
    session.analyse()

    by_type = {}
    for candidate in session.candidates:
        by_type.setdefault(candidate.pii_type, []).append(candidate.text.strip())

    assert "Mark" in by_type.get(PiiType.PERSON, []), by_type
    assert any("Macey" in v for v in by_type.get(PiiType.PERSON, [])), by_type
    assert any("Cmn" in v for v in by_type.get(PiiType.STREET, [])), by_type
    assert any("Fremont" in v for v in by_type.get(PiiType.ADDRESS, [])), by_type

    # And the two halves of the joint name still share a surname.
    session.decisions.set_state(
        [c for c in session.candidates if c.pii_type is not PiiType.UNCLASSIFIED_GROUP_VALUE],
        DecisionState.ACCEPTED,
    )
    replacements = {t.original.strip(): t.replacement for t in session.plan().targets}
    mark_out = replacements.get("Mark")
    macey_out = replacements.get("Macey Lang")
    assert mark_out and macey_out
    assert macey_out.split()[-1] not in (mark_out,), "surnames did not stay linked"


def test_uncommon_street_suffixes_are_recognised():
    """Reported miss. USPS Pub 28 has far more suffixes than the common few."""
    from app.detection.deterministic import load_rules

    ruleset = load_rules()
    street_rule = next(r for r in ruleset.rules if r.name == "street_address")
    for address in (
        "4049 Caribbean Cmn", "789 Sunset Ridge", "12 Meadow Grove",
        "456 Oak Trail", "99 Riverside Common", "22 Harbor Landing",
    ):
        assert street_rule.regex.search(address), address
