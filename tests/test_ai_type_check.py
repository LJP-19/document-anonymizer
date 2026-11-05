"""The AI type check is FLAG-ONLY: it may add a review note, never change a
type, a text or a decision. Designed from real measurements with the 0.5B model
(see the comment above TYPE_CHECK_PHASE_SECONDS in auditor.py)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.detection import auditor
from app.detection.auditor import AuditorUnavailable, check_types
from app.detection.types import Candidate, PiiType, Source

pytestmark = pytest.mark.usefixtures("isolated_home")


@pytest.fixture(autouse=True)
def _clean():
    auditor.reset_stop()
    auditor.set_progress_listener(None)
    yield
    auditor.reset_stop()
    auditor.set_progress_listener(None)


class _TypeFake:
    """Answers the type question from a dict; records every value it was asked."""

    def __init__(self, answers=None, fail_on=(), garbage=False):
        self.answers = answers or {}
        self.fail_on = set(fail_on)
        self.garbage = garbage
        self.asked = []
        self.messages = []
        self.loads = 0
        self._llm = MagicMock()
        self._llm.create_chat_completion.side_effect = self._chat

    def load(self):
        self.loads += 1

    def _chat(self, **kwargs):
        content = kwargs["messages"][-1]["content"]
        self.messages.append(content)
        if '"kind"' not in content:  # not the type question: act as an empty adjudication
            return {"choices": [{"message": {"content": '{"verdicts": []}'}}]}
        value = content.rsplit("VALUE: ", 1)[1]
        self.asked.append(value)
        if value in self.fail_on:
            raise RuntimeError("model failed")
        text = "not json at all" if self.garbage else json.dumps({"kind": self.answers.get(value, "person")})
        return {"choices": [{"message": {"content": text}}]}

    def audit(self, text, detected):
        return [], []


def _cand(text, pii_type, source=Source.NER, line_text=None, page=0, line_no=0):
    from app.document.model import Char, Line, Span

    full = line_text or text
    char = Char(text="x", bbox=(0, 0, 5, 10))
    span = Span(text=full, bbox=(0, 0, 50, 10), font="helv", size=10, color=0, chars=[char])
    line = Line(page_no=page, block_no=0, line_no=line_no, spans=[span], text=full,
                offsets=[char], bbox=(0, 0, 50, 10))
    start = max(full.find(text), 0)
    return Candidate(pii_type=pii_type, text=text, page_no=page, rect=(0, 0, 50, 10), line=line,
                     start=start, end=start + len(text), confidence=0.9, source=source)


def test_a_company_typed_as_a_person_is_flagged_and_nothing_else_changes():
    from app.decisions.manager import DecisionManager, DecisionState

    c = _cand("Acme Manufacturing Inc.", PiiType.PERSON, line_text="Employer: Acme Manufacturing Inc.")
    manager = DecisionManager()
    manager.register([c])
    assert manager.state(c) is DecisionState.ACCEPTED

    check_types([c], auditor=_TypeFake({"Acme Manufacturing Inc.": "organization"}))

    assert c.needs_review
    assert "a company or organization" in c.review_reason and "not a person's name" in c.review_reason
    assert c.pii_type is PiiType.PERSON, "flag-only: the type must never be changed"
    assert c.text == "Acme Manufacturing Inc."
    assert manager.state(c) is DecisionState.ACCEPTED, "flag-only: the decision must never be changed"


def test_agreement_flags_nothing():
    c = _cand("Jane A Public", PiiType.PERSON)
    check_types([c], auditor=_TypeFake({"Jane A Public": "person"}))
    assert not c.needs_review and c.review_reason == ""


@pytest.mark.parametrize("kind", ["email", "phone", "date", "government_id", "account", "other_id", "other"])
def test_an_answer_outside_person_company_address_never_flags(kind):
    """The model is unreliable outside those three (it called an SSN a phone
    number), so any other answer is treated as 'no opinion'."""
    c = _cand("Jane A Public", PiiType.PERSON)
    check_types([c], auditor=_TypeFake({"Jane A Public": kind}))
    assert not c.needs_review


@pytest.mark.parametrize("text,pii_type,source", [
    ("62704", PiiType.POSTAL_CODE, Source.NER),
    ("100 200 300 400", PiiType.ADDRESS, Source.NER),
    ("A1", PiiType.PERSON, Source.NER),
    ("123-45-6789", PiiType.SSN, Source.REGEX),
    ("Mark Jones", PiiType.PERSON, Source.MANUAL),
    ("x" * 130, PiiType.PERSON, Source.NER),
])
def test_values_the_model_is_unreliable_on_are_never_even_asked(text, pii_type, source):
    fake = _TypeFake()
    check_types([_cand(text, pii_type, source=source)], auditor=fake)
    assert fake.asked == [] and fake.loads == 0, "nothing checkable: do not even load the model"


def test_a_repeated_value_is_asked_once_and_every_occurrence_is_flagged():
    cands = [_cand("Acme Corp", PiiType.PERSON, page=n, line_no=n) for n in range(3)]
    fake = _TypeFake({"Acme Corp": "organization"})
    check_types(cands, auditor=fake)
    assert fake.asked == ["Acme Corp"]
    assert all(c.needs_review for c in cands)


def test_the_same_text_with_two_different_types_is_asked_for_each():
    cands = [_cand("Madison", PiiType.PERSON, line_no=0), _cand("Madison", PiiType.CITY_STATE, line_no=1)]
    fake = _TypeFake({"Madison": "person"})
    check_types(cands, auditor=fake)
    assert fake.asked == ["Madison", "Madison"]
    assert not cands[0].needs_review and cands[1].needs_review


def test_an_unavailable_model_warns_and_never_raises():
    fake = _TypeFake()
    fake.load = MagicMock(side_effect=AuditorUnavailable("no model"))
    warnings = check_types([_cand("Acme Corp", PiiType.PERSON)], auditor=fake)
    assert any("AI type check disabled" in w for w in warnings)


def test_one_failing_value_does_not_stop_the_others():
    a = _cand("Broken Value", PiiType.PERSON, line_no=0)
    b = _cand("Acme Corp", PiiType.PERSON, line_no=1)
    warnings = check_types([a, b], auditor=_TypeFake({"Acme Corp": "organization"}, fail_on={"Broken Value"}))
    assert b.needs_review and not a.needs_review
    assert any("could not read" in w for w in warnings)


def test_an_unreadable_answer_is_not_treated_as_a_disagreement():
    c = _cand("Acme Corp", PiiType.PERSON)
    check_types([c], auditor=_TypeFake(garbage=True))
    assert not c.needs_review


def test_progress_is_reported_and_a_stop_request_ends_the_check():
    cands = [_cand(f"Person Number{n}", PiiType.PERSON, line_no=n) for n in range(3)]
    seen = []
    auditor.set_progress_listener(seen.append)
    check_types(cands, auditor=_TypeFake())
    assert seen == ["AI type check: value 1 of 3", "AI type check: value 2 of 3", "AI type check: value 3 of 3"]

    auditor.request_stop()
    fake = _TypeFake()
    warnings = check_types(cands, auditor=fake)
    assert fake.asked == [] and any("skipped at your request" in w for w in warnings)


def test_the_instructions_come_first_and_the_value_last_so_the_prefix_is_reused():
    """Reordering this would quietly make every check several times slower."""
    fake = _TypeFake()
    check_types([_cand("Acme Corp", PiiType.PERSON, line_text="Employer: Acme Corp")], auditor=fake)
    message = fake.messages[0]
    assert message.startswith(auditor.TYPE_CHECK_HEAD)
    assert message.endswith("LINE: Employer: Acme Corp\nVALUE: Acme Corp")


def test_the_grammar_cache_holds_every_schema_so_none_is_rebuilt_on_each_call():
    assert auditor._grammar.cache_info().maxsize >= 3


def test_analyse_runs_the_type_check_only_when_the_model_phase_is_on(tmp_path):
    import pymupdf

    from app.session import AnonymizationSession

    path = str(tmp_path / "d.pdf")
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 100), "Taxpayer name: Mark Jones", fontsize=11)
    pdf.save(path)
    pdf.close()

    with patch("app.detection.engine.check_types", return_value=[]) as check, patch(
        "app.detection.engine.audit_document", return_value=([], [], [])
    ), patch("app.detection.engine.adjudicate_document", return_value=(set(), [])):
        AnonymizationSession(source_path=path).analyse(use_llm=True)
        assert check.call_count == 1
        AnonymizationSession(source_path=path).analyse(use_llm=False)
        assert check.call_count == 1, "must not run when the AI review is off"


def test_through_the_real_pipeline_a_disagreement_reaches_the_review_list(tmp_path):
    import pymupdf

    from app.session import AnonymizationSession

    path = str(tmp_path / "d.pdf")
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 100), "Taxpayer name: Mark Jones", fontsize=11)
    pdf.save(path)
    pdf.close()

    fake = _TypeFake({"Mark Jones": "organization"})
    with patch("app.detection.auditor._auditor", return_value=fake):
        session = AnonymizationSession(source_path=path)
        session.analyse(use_llm=True)

    flagged = [c for c in session.candidates if "Mark Jones" in c.text and "the AI thinks" in c.review_reason]
    assert flagged, [(c.text, c.pii_type.name, c.review_reason) for c in session.candidates]
    assert all(c.needs_review for c in flagged)
