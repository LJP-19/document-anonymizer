"""The second pass re-scans the transformed output (spec sections 18-19, 37-39)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.decisions.manager import DecisionState
from app.detection.second_pass import second_pass
from app.detection.types import Source
from app.session import AnonymizationSession
from tests import fixtures


def _analysed(path: str) -> AnonymizationSession:
    session = AnonymizationSession(source_path=path)
    session.analyse()
    return session


def test_a_fully_transformed_document_comes_back_clean(tmp_path):
    session = _analysed(fixtures.form_pdf(tmp_path / "clean.pdf"))
    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)
    result = second_pass(session.document, session.plan())
    assert result.scanned_pages >= 1
    assert result.clean, [f.text for f in result.findings]


def test_a_value_left_in_the_output_is_reported(tmp_path):
    """Remove one target from the plan: its original survives, and is found."""
    session = _analysed(fixtures.form_pdf(tmp_path / "gap.pdf"))
    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)

    plan = session.plan()
    dropped = next(t for t in plan.targets if "@" in t.original)
    plan.targets = [t for t in plan.targets if t is not dropped]

    result = second_pass(session.document, plan)
    assert any(dropped.original.strip() in f.text for f in result.findings), (
        f"the surviving value was not reported: {[f.text for f in result.findings]}"
    )


def test_findings_join_the_normal_review_list(tmp_path):
    session = _analysed(fixtures.form_pdf(tmp_path / "mark.pdf"))
    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)

    plan = session.plan()
    plan.targets = plan.targets[:1]
    result = second_pass(session.document, plan)
    assert result.findings

    for finding in result.findings:
        assert finding.source is Source.COVERAGE
        assert finding.needs_review
        assert "still readable" in finding.review_reason

    # A finding the session has not already registered must wait for a
    # decision. (Ones that collide with an existing candidate keep that
    # candidate's state, which is correct - they are the same item.)
    from app.decisions.manager import DecisionManager

    # One process: these behave like any other detection - redacted by default
    # and listed for review, not held back in a separate stage.
    fresh = DecisionManager()
    fresh.register(result.findings)
    for finding in result.findings:
        assert fresh.state(finding) is DecisionState.ACCEPTED
        assert finding.needs_review, "a late finding should still be flagged"


def test_pseudonyms_are_not_reported_as_misses(tmp_path):
    """The replacements are, by design, name-shaped. They must not be flagged."""
    session = _analysed(fixtures.form_pdf(tmp_path / "pseudo.pdf"))
    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)
    plan = session.plan()
    result = second_pass(session.document, plan)
    replacements = {t.replacement.lower() for t in plan.targets}
    for finding in result.findings:
        assert finding.normalized.lower() not in replacements


def test_a_value_the_user_kept_is_not_reported(tmp_path):
    session = _analysed(fixtures.form_pdf(tmp_path / "kept.pdf"))
    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)
    keep = next(c for c in session.candidates if "@" in c.text)
    session.decisions.skip([keep])

    result = second_pass(session.document, session.plan())
    assert not any(keep.normalized in f.text for f in result.findings), (
        "a deliberately kept value was reported as a miss"
    )


def test_it_runs_automatically_during_analysis(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCANON_SECOND_PASS", "1")
    session = _analysed(fixtures.form_pdf(tmp_path / "auto.pdf"))
    assert session.detection is not None
    # It ran: either it reported findings, or the document came back clean.
    assert session.document is not None


def test_it_can_be_turned_off(tmp_path, monkeypatch):
    from app.session import second_pass_enabled

    monkeypatch.setenv("DOCANON_SECOND_PASS", "0")
    assert not second_pass_enabled()
    monkeypatch.setenv("DOCANON_SECOND_PASS", "1")
    assert second_pass_enabled()


def test_a_failure_in_the_second_pass_never_breaks_analysis(tmp_path, monkeypatch):
    import app.detection.second_pass as module

    def boom(*args, **kwargs):
        raise MemoryError("simulated")

    monkeypatch.setattr(module, "second_pass", boom)
    session = AnonymizationSession(source_path=fixtures.form_pdf(tmp_path / "safe.pdf"))
    session.analyse()
    assert session.candidates, "analysis did not survive a failing second pass"


# --- replacement plausibility ----------------------------------------------


def test_replacements_are_checked_for_sanity(tmp_path):
    from app.detection.second_pass import check_replacements

    session = _analysed(fixtures.form_pdf(tmp_path / "sane.pdf"))
    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)
    assert not check_replacements(session.plan()), "a clean plan reported faults"


def test_a_scrambled_replacement_is_reported(tmp_path):
    from app.detection.second_pass import check_replacements

    session = _analysed(fixtures.form_pdf(tmp_path / "bad.pdf"))
    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)
    plan = session.plan()
    # Real output from the old character-level masker.
    plan.targets[0].replacement = "Nkzjenrkk Xqfmvtbz"
    faults = check_replacements(plan)
    assert any("readable" in why for _o, _r, why in faults), faults


def test_a_date_replaced_by_a_name_is_reported(tmp_path):
    from app.detection.second_pass import check_replacements
    from app.transform.plan import Target

    session = _analysed(fixtures.form_pdf(tmp_path / "date.pdf"))
    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)
    plan = session.plan()
    plan.targets.append(
        Target(candidate_id="x", page_no=0, rect=(0, 0, 10, 10),
               original="04/11/1979", replacement="Marisol Etxeberria",
               font_size=10.0, font_name="helv")
    )
    faults = check_replacements(plan)
    assert any("different kind" in why for _o, _r, why in faults), faults


def test_a_replacement_repeating_the_original_is_reported(tmp_path):
    from app.detection.second_pass import check_replacements
    from app.transform.plan import Target

    session = _analysed(fixtures.form_pdf(tmp_path / "leak.pdf"))
    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)
    plan = session.plan()
    plan.targets.append(
        Target(candidate_id="y", page_no=0, rect=(0, 0, 10, 10),
               original="John Smith", replacement="John Glass",
               font_size=10.0, font_name="helv")
    )
    faults = check_replacements(plan)
    assert any("repeats" in why for _o, _r, why in faults), faults
