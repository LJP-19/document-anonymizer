"""The audit pass runs by default once a model is chosen, and what it finds is
redacted AND flagged for review.

This reverses an earlier, deliberate policy (off by default; model findings
wait for a decision) on the owner's explicit instruction. The reversal is only
safe because every filter between the model and the document is unchanged, so
several tests below exist specifically to prove those filters still hold now
that nothing waits for a human before a finding is applied.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.usefixtures("isolated_home")


# ---------------------------------------------------------------- helpers


def _select_a_model():
    from app.llm import downloader, selection
    from app.llm.catalog import CATALOG

    choice = CATALOG[0]
    path = downloader.model_path(choice)
    path.touch()
    os.truncate(path, choice.size_bytes)  # right size, not a real model
    selection.set_selected(choice)
    return path


class _FakeAuditor:
    """Stands in for LlmAuditor: records use, returns canned findings."""

    def __init__(self, missed=()):
        from app.detection.auditor import AuditFinding

        self.loads = 0
        self.audits = 0
        self._missed = [AuditFinding(text, category) for text, category in missed]
        # adjudicate_document talks to the raw llama object: accept everything.
        self._llm = MagicMock()
        self._llm.create_chat_completion.return_value = {
            "choices": [{"message": {"content": '{"verdicts": []}'}}]
        }

    def load(self):
        self.loads += 1

    def audit(self, text, detected):
        self.audits += 1
        return list(self._missed), []


def _pdf(path, lines):
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    y = 90
    for line in lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 24
    document.save(str(path))
    document.close()
    return str(path)


def _candidate(source, pii_type, line_no=0):
    from app.detection.types import Candidate
    from app.document.model import Char, Line, Span

    char = Char(text="X", bbox=(0, 0, 5, 10))
    span = Span(text="X", bbox=(0, 0, 5, 10), font="helv", size=10, color=0, chars=[char])
    line = Line(page_no=0, block_no=0, line_no=line_no, spans=[span], text="X",
                offsets=[char], bbox=(0, 0, 5, 10))
    return Candidate(pii_type=pii_type, text="X", page_no=0, rect=(0, 0, 5, 10), line=line,
                     start=0, end=1, confidence=0.7, source=source)


# ------------------------------------------------- the default itself


def test_audit_is_off_when_no_model_has_been_chosen(monkeypatch):
    from app.detection.auditor import llm_audit_enabled

    monkeypatch.delenv("DOCANON_LLM", raising=False)
    assert llm_audit_enabled() is False


def test_audit_turns_on_by_itself_once_a_model_is_chosen(monkeypatch):
    pytest.importorskip("llama_cpp")
    from app.detection.auditor import llm_audit_enabled

    monkeypatch.delenv("DOCANON_LLM", raising=False)
    _select_a_model()
    assert llm_audit_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_an_explicit_off_always_wins(monkeypatch, value):
    pytest.importorskip("llama_cpp")
    from app.detection.auditor import llm_audit_enabled

    _select_a_model()
    monkeypatch.setenv("DOCANON_LLM", value)
    assert llm_audit_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_an_explicit_on_is_honoured_even_without_a_model(monkeypatch, value):
    from app.detection.auditor import llm_audit_enabled

    monkeypatch.setenv("DOCANON_LLM", value)
    assert llm_audit_enabled() is True


def test_checking_whether_the_audit_can_run_never_loads_the_model(monkeypatch):
    """The status label and the default both ask this at startup. Loading a
    1.9-4.7 GB model just to decide whether to show a word is wrong."""
    llama_cpp = pytest.importorskip("llama_cpp")
    from app.detection.auditor import LlmAuditor, llm_audit_enabled

    _select_a_model()
    monkeypatch.delenv("DOCANON_LLM", raising=False)
    with patch.object(llama_cpp, "Llama", side_effect=AssertionError("model was loaded")):
        assert LlmAuditor().configured is True
        assert llm_audit_enabled() is True


def test_the_session_uses_the_audit_by_default_and_not_when_forced_off(tmp_path, monkeypatch):
    """The wiring, through the real session: no environment variable set, a
    model chosen -> the audit runs; DOCANON_LLM=0 -> it does not."""
    pytest.importorskip("llama_cpp")
    from app.session import AnonymizationSession

    _select_a_model()
    path = _pdf(tmp_path / "doc.pdf", ["Prepared for Mark Jones", "Wages ....... $412,890"])

    fake = _FakeAuditor()
    monkeypatch.delenv("DOCANON_LLM", raising=False)
    with patch("app.detection.auditor._auditor", return_value=fake):
        AnonymizationSession(source_path=path).analyse()
    assert fake.loads >= 1, "a chosen model must switch the audit on with no setting"

    off = _FakeAuditor()
    monkeypatch.setenv("DOCANON_LLM", "0")
    with patch("app.detection.auditor._auditor", return_value=off):
        AnonymizationSession(source_path=path).analyse()
    assert off.loads == 0


# ------------------------------------- findings are applied AND flagged


def test_a_typed_model_finding_is_applied_by_default_and_stays_flagged():
    from app.decisions.manager import DecisionManager, DecisionState
    from app.detection.types import PiiType, Source

    typed = _candidate(Source.AUDIT, PiiType.PERSON)
    typed.needs_review = True  # audit_document always sets this
    manager = DecisionManager()
    manager.register([typed])
    assert manager.state(typed) is DecisionState.ACCEPTED
    assert manager.is_actionable(typed), "ACCEPTED means it is redacted in the output"
    assert typed.needs_review, "...and it must still be flagged for the reviewer"


def test_what_waited_before_still_waits():
    """Auto-applying model findings must not drag along the two cases that
    must never be applied unasked: a value nothing could TYPE (there is no
    valid replacement to generate), and one with too little evidence."""
    from app.decisions.manager import DecisionManager, DecisionState
    from app.detection.types import PiiType, Source

    untyped = _candidate(Source.AUDIT, PiiType.UNCLASSIFIED_GROUP_VALUE, line_no=1)
    unresolved = _candidate(Source.AUDIT, PiiType.PERSON, line_no=2)
    unresolved.adjudication = "UNRESOLVED"
    manager = DecisionManager()
    manager.register([untyped, unresolved])
    assert manager.state(untyped) is DecisionState.SKIPPED
    assert manager.state(unresolved) is DecisionState.SKIPPED


def test_the_reviewer_is_told_the_truth_about_what_will_happen(tmp_path):
    """The old reason said the value was "KEPT unless you press Redact"."""
    from app.detection.auditor import audit_document
    from app.session import AnonymizationSession

    path = _pdf(tmp_path / "doc.pdf", ["Prepared for Marisol Etxeberria today"])
    session = AnonymizationSession(source_path=path)
    document = session.provider.load(path)
    fake = _FakeAuditor(missed=[("Marisol Etxeberria", "name")])

    additions, _wrong, _warnings = audit_document(document, [], auditor=fake)

    assert additions, "the stand-in model's finding should have been located and typed"
    finding = additions[0]
    assert finding.needs_review
    assert "redacted unless you press Keep" in finding.review_reason
    assert "KEPT" not in finding.review_reason


# ------------------ the safety filters did not loosen: nothing waits now


def test_a_figure_proposed_by_the_model_is_never_redacted_even_though_findings_now_apply(
    tmp_path, monkeypatch
):
    """The most important test here. With findings applied by default, the
    only thing standing between a hallucinated 'identifier' and a damaged
    return is the filtering. A currency amount, a percentage and a field
    label must never reach the plan, however confidently the model proposes
    them.

    The model reviews every page now, so it is consulted whatever the other
    detectors found; the test still asserts that it really was consulted,
    because its first version passed vacuously (the page was never selected
    for audit, so the filters were never exercised).
    """
    pytest.importorskip("llama_cpp")
    from app.session import AnonymizationSession

    _select_a_model()
    monkeypatch.delenv("DOCANON_LLM", raising=False)
    path = _pdf(tmp_path / "doc.pdf", ["Wages, salaries, tips ....... $412,890", "Total tax 18%"])
    fake = _FakeAuditor(missed=[
        ("$412,890", "identifier"),
        ("412,890", "identifier"),
        ("18%", "identifier"),
        ("Wages, salaries, tips", "name"),
    ])
    with patch("app.detection.auditor._auditor", return_value=fake):
        session = AnonymizationSession(source_path=path)
        session.analyse()

    assert fake.audits >= 1, "the model must actually have been consulted"
    leaked = [c.text for c in session.candidates
              if "412" in c.text or "18%" in c.text or "Wages" in c.text]
    assert not leaked, f"a figure or label became a redaction: {leaked}"


def test_conservative_mode_still_excludes_model_findings():
    from app.detection.engine import _conservative_filter
    from app.detection.types import PiiType, Source

    audit = _candidate(Source.AUDIT, PiiType.PERSON)
    kept, dropped = _conservative_filter([audit])
    assert dropped == 1 and not kept
