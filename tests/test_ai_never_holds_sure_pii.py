"""Owner's rule (0.44.1): the AI review never holds back what the app is sure of.

Reported on a Mac (AI review on, a model downloaded): fewer values were redacted
automatically than on Windows, values were not applied to all their occurrences,
and pressing Skip AI review left the document worse, not just un-reviewed. Cause:
a model objection held a label-confirmed value for review - which also removed it
from the apply-to-all set - or deleted a confident detection outright.

The invariant tested here, on the fictional joint return in demo/joint_tw: with a
model that objects to EVERY value and calls every name a company, and with Skip
pressed part-way, the automatic redactions are exactly those of the AI-off run.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.detection import auditor
from app.session import AnonymizationSession

pytestmark = pytest.mark.usefixtures("isolated_home")

ROOT = Path(__file__).resolve().parents[1]
JOINT = ROOT / "demo" / "joint_tw" / "Chen_Lin_2025_1040_W2.pdf"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DOCANON_SECOND_PASS", "0")
    monkeypatch.delenv("DOCANON_LLM", raising=False)
    auditor.reset_stop()
    yield
    auditor.reset_stop()


class _HostileAuditor:
    """Objects to everything: every detected value is 'a label', every value in the
    plan is 'business', every name or place is 'a company'. Optionally presses Skip
    after `stop_after` model calls."""

    def __init__(self, everything: set, stop_after: int | None = None):
        self.everything = sorted(everything)
        self.stop_after = stop_after
        self.calls = 0
        self._llm = MagicMock()
        self._llm.create_chat_completion.side_effect = self._chat

    def _tick(self):
        self.calls += 1
        if self.stop_after is not None and self.calls >= self.stop_after:
            auditor.request_stop()

    def _chat(self, **kwargs):
        self._tick()
        content = kwargs["messages"][-1]["content"]
        if "VALUE:" in content:
            return {"choices": [{"message": {"content": '{"kind": "company"}'}}]}
        verdicts = ",".join(f'{{"text": "{v}", "kind": "business"}}' for v in self.everything)
        return {"choices": [{"message": {"content": f'{{"verdicts": [{verdicts}]}}'}}]}

    def load(self):
        pass

    def audit(self, text, detected):
        self._tick()
        return [], list(detected)


def _auto_redacted(use_llm: bool, fake=None):
    s = AnonymizationSession(source_path=str(JOINT))
    if use_llm:
        with patch("app.detection.auditor._auditor", return_value=fake):
            s.analyse(use_llm=True)
    else:
        s.analyse(use_llm=False)
    s.decisions.approve_auto_eligible(s.candidates)
    plan = s.plan()
    return s, {(t.page_no, t.original) for t in plan.targets}


@pytest.fixture(scope="module")
def baseline():
    mp = pytest.MonkeyPatch()
    mp.setenv("DOCANON_SECOND_PASS", "0")
    s, targets = _auto_redacted(False)
    mp.undo()
    assert len(targets) > 20, "the fixture should redact plenty without the AI"
    return {c.text for c in s.candidates}, targets


def test_a_model_that_objects_to_everything_changes_no_automatic_redaction(baseline):
    texts, expected = baseline
    fake = _HostileAuditor(texts)
    _s, got = _auto_redacted(True, fake)
    assert fake.calls > 0, "the model was never consulted - the test proves nothing"
    assert expected - got == set(), f"sure PII held back by the AI: {sorted(expected - got)[:8]}"


@pytest.mark.parametrize("stop_after", [1, 3, 6])
def test_skip_ai_review_part_way_keeps_every_automatic_redaction(baseline, stop_after):
    texts, expected = baseline
    fake = _HostileAuditor(texts, stop_after=stop_after)
    _s, got = _auto_redacted(True, fake)
    assert auditor.stop_requested()
    assert expected - got == set(), f"Skip lost sure PII: {sorted(expected - got)[:8]}"


class _FormTextAuditor(_HostileAuditor):
    """Reproduces what the real 1.5B model did on this return (0.44.2): it
    'found' the form's own line numbers and checkbox options."""

    def audit(self, text, detected):
        from app.detection.auditor import AuditFinding

        self._tick()
        junk = [("11b", "birthplace"), ("25c", "birthplace"), ("Full-time", "marital status"),
                ("Permanently", "marital status"), ("Combat zone", "fax")]
        return [AuditFinding(t, k) for t, k in junk if t in text], []

    def _chat(self, **kwargs):
        self._tick()
        return {"choices": [{"message": {"content": '{"verdicts": []}'}}]}


def test_form_text_suggested_by_the_ai_never_reaches_the_review_list(baseline):
    _texts, expected = baseline
    fake = _FormTextAuditor(set())
    s, got = _auto_redacted(True, fake)
    assert fake.calls > 0
    junk = {"11b", "25c", "Full-time", "Permanently", "Combat zone"}
    listed = [c.text for c in s.decisions.awaiting_review(s.candidates) if c.text in junk]
    assert listed == [], f"form text from the AI is in the review list: {listed}"
    assert got == expected
