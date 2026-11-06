"""Fail-closed authorization (0.42.0): the owner's 18 cases and every bypass route.

ONLY CONFIRMED PII IS TRANSFORMED WITHOUT A PERSON. A detection source, a group id,
a regex match or a confidence score authorizes nothing; `needs_review` vetoes
automatic redaction absolutely; the transformation plan re-checks every automatic
approval; the redactor refuses a target that did not pass that boundary. All
values are fictional.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

from app.decisions.authorization import (AuthOrigin, annotate_label_binding, assess,
                                         confirmed_name_words)
from app.decisions.manager import Decision, DecisionManager, DecisionState
from app.detection.types import (Candidate, Evidence, LabelRegion, LogicalFieldGroup, PiiType,
                                 Source)
from app.document.model import Char, Line, Span
from app.session import AnonymizationSession
from tests.helpers_text import analysed

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (0x23 / 255, 0x1F / 255, 0x20 / 255)


def _line(text="X", n=0):
    chars = [Char(text=ch, bbox=(i * 5, 0, i * 5 + 5, 10)) for i, ch in enumerate(text)]
    span = Span(text=text, bbox=(0, 0, 5 * len(text), 10), font="helv", size=10, color=0, chars=chars)
    return Line(page_no=0, block_no=0, line_no=n, spans=[span], text=text, offsets=chars,
                bbox=(0, 0, 5 * len(text), 10))


def cand(text="Torvin Quell", pii_type=PiiType.PERSON, source=Source.NER, evidence=(), n=0, **kw):
    line = _line(text, n)
    return Candidate(pii_type=pii_type, text=text, page_no=0, rect=line.bbox, line=line, start=0,
                     end=len(text), confidence=kw.pop("confidence", 0.8), source=source,
                     evidence=[Evidence(source, e, 0.5) for e in evidence], **kw)


def _group(label_text, expected, unknown=False, non_pii=False, gid="g0"):
    line = _line(label_text, 99)
    label = LabelRegion(text=label_text, line=line, start=0, end=len(label_text), rect=line.bbox,
                        expected_types=list(expected), non_pii=non_pii, unknown=unknown)
    return LogicalFieldGroup(group_id=gid, label=label, value_lines=[], expected_types=list(expected))


def _pdf(path: Path, items) -> str:
    doc = pymupdf.open()
    page = doc.new_page()
    for x, y, text, kind in items:
        if kind == "label":
            page.insert_text((x, y), text, fontsize=7, fontname="helv", color=TEMPLATE)
        else:
            page.insert_text((x, y), text, fontsize=10, fontname="cour", color=(0, 0, 0))
    doc.save(str(path))
    doc.close()
    return str(path)


def _actionable(text: str):
    _doc, result, dm = analysed(text)
    return {c.text: dm.is_actionable(c) for c in result.candidates}, result, dm


# -- the owner's 18 authorization cases -------------------------------------------------

def test_01_an_unlabelled_five_digit_number_is_not_applied():
    got, _r, _dm = _actionable("Reference 94621 was logged.")
    assert got.get("94621") is not True


def test_02_an_unlabelled_nine_digit_number_is_not_applied():
    got, _r, _dm = _actionable("Control 573918204 SS EXT 01")
    assert got.get("573918204") is not True


def test_03_a_generic_regex_match_without_context_is_not_authorization():
    c = cand("415-555-0172", PiiType.PHONE, Source.REGEX, ["rule=phone_us"])
    assert not assess(c).eligible


def test_04_an_arbitrary_group_id_is_not_label_binding():
    c = cand("Torvin Quell", PiiType.PERSON, Source.GROUP, ["value line of labelled group 'Notes'"],
             group_id="g0")
    annotate_label_binding([c], [_group("Notes", [PiiType.ADDRESS])])   # wrong type
    assert not c.label_confirmed and not assess(c).eligible
    annotate_label_binding([c], [_group("Some caption", [PiiType.PERSON], unknown=True)])
    assert not c.label_confirmed and not assess(c).eligible
    annotate_label_binding([c], [])                                      # no such group at all
    assert not c.label_confirmed and not assess(c).eligible


def test_05_a_model_only_person_guess_is_not_applied():
    assert not assess(cand(source=Source.NER, evidence=["spaCy PERSON"])).eligible


def test_06_needs_review_is_an_absolute_veto():
    c = cand(source=Source.GROUP, group_id="g0", needs_review=True, review_reason="AI thinks this is a company")
    c.label_confirmed = True
    assert not assess(c, {"torvin", "quell"}).eligible


def test_07_a_probable_candidate_is_not_applied():
    assert not assess(cand(adjudication="PROBABLE")).eligible


def test_08_an_unresolved_candidate_is_not_applied_even_with_confirmed_words():
    assert not assess(cand(adjudication="UNRESOLVED"), {"torvin", "quell"}).eligible


def test_09_a_failed_validator_blocks_automatic_redaction():
    c = cand("123-45-6789", PiiType.SSN, Source.REGEX,
             ["rule=ssn_dashed", "label context nearby", "ssn validation failed"])
    c.label_confirmed = True
    assert not assess(c).eligible


def test_10_an_unknown_label_does_not_bind():
    c = cand(source=Source.GROUP, group_id="g0")
    annotate_label_binding([c], [_group("Misc. item (see attached)", [PiiType.PERSON], unknown=True)])
    assert not c.label_confirmed


def test_11_a_correctly_bound_name_is_applied():
    c = cand(source=Source.GROUP, group_id="g0")
    annotate_label_binding([c], [_group("Taxpayer name", [PiiType.PERSON])])
    assert c.label_confirmed and assess(c).eligible


def _captioned(caption, value, pii_type, evidence):
    line = _line(caption + value)
    return Candidate(pii_type=pii_type, text=value, page_no=0, rect=line.bbox, line=line,
                     start=len(caption), end=len(caption) + len(value), confidence=0.97,
                     source=Source.REGEX, evidence=[Evidence(Source.REGEX, e, 0.5) for e in evidence])


def test_12_a_validated_identifier_named_by_its_own_caption_is_applied():
    c = _captioned("SSN: ", "612-40-7731", PiiType.SSN, ["rule=ssn_dashed", "label context nearby"])
    assert assess(c).eligible


def test_12b_a_generic_context_word_is_not_an_identifier_caption():
    """'Taxpayer name:' one line above made "Reference 573918204" an SSN."""
    c = _captioned("Reference ", "573918204", PiiType.SSN, ["rule=ssn_plain", "label context nearby"])
    assert not assess(c).eligible


def test_13_a_confirmed_entity_repeated_elsewhere_is_applied():
    c = cand(source=Source.COVERAGE, evidence=["same value as an entity named in a subject field"])
    assert assess(c).eligible
    # and the same person's name words found by a model elsewhere
    assert assess(cand(source=Source.NER, adjudication="PROBABLE"), {"torvin", "quell"}).eligible


def test_14_an_unrelated_common_surname_is_not_applied():
    got, _r, _dm = _actionable("Taxpayer name: Torvin Baker\nThe Baker Street bakery opens at nine.")
    assert got.get("Torvin Baker") is True
    assert not any(v for t, v in got.items() if "bakery" in t or t == "Baker Street")


def _session_with_held(tmp_path):
    path = _pdf(tmp_path / "t.pdf", [(36, 60, "Taxpayer name:", "label"), (110, 60, "Torvin Quell", "value"),
                                     (36, 90, "Reference 573918204 was logged.", "value")])
    s = AnonymizationSession(source_path=path)
    s.analyse()
    return s


def test_15_explicit_redact_authorizes(tmp_path):
    s = _session_with_held(tmp_path)
    held = [c for c in s.candidates if c.text == "573918204"]
    assert held and not s.decisions.is_actionable(held[0])
    s.decisions.set_state(held, DecisionState.ACCEPTED)
    assert s.decisions.origin(held[0]) == AuthOrigin.USER_APPROVED.value
    assert held[0].id in {t.candidate_id for t in s.plan().targets}


def test_16_explicit_keep_preserves(tmp_path):
    s = _session_with_held(tmp_path)
    name = next(c for c in s.candidates if c.text == "Torvin Quell")
    assert s.decisions.is_actionable(name)
    s.decisions.set_state([name], DecisionState.SKIPPED)
    assert s.decisions.origin(name) == AuthOrigin.USER_KEPT.value
    assert name.id not in {t.candidate_id for t in s.plan().targets}


def test_17_explicit_edit_authorizes_with_the_users_replacement(tmp_path):
    s = _session_with_held(tmp_path)
    held = next(c for c in s.candidates if c.text == "573918204")
    s.decisions.edit([held], "000000000")
    targets = {t.candidate_id: t for t in s.plan().targets}
    assert targets[held.id].replacement == "000000000" and targets[held.id].authorized_by == "USER_EDITED"


def test_18_a_candidate_wrongly_marked_accepted_is_refused_by_the_plan(tmp_path):
    s = _session_with_held(tmp_path)
    held = next(c for c in s.candidates if c.text == "573918204")
    # Some component sets ACCEPTED directly, with no authorization: refused.
    s.decisions.decisions[held.id] = Decision(candidate_id=held.id, state=DecisionState.ACCEPTED)
    plan = s.plan()
    assert held.id not in {t.candidate_id for t in plan.targets} and held.id in plan.refused
    # ...and an AUTO approval the policy no longer supports is refused too.
    s.decisions.decisions[held.id] = Decision(candidate_id=held.id, state=DecisionState.ACCEPTED,
                                              origin="AUTO_APPROVED")
    plan = s.plan()
    assert held.id not in {t.candidate_id for t in plan.targets}
    entry = next(e for e in plan.authorization_log if e["candidate_id"] == held.id)
    assert entry["authorization"] == "REJECTED" and "refused" in entry["outcome"]


# -- the boundary is enforced twice -------------------------------------------------------

def test_the_redactor_refuses_a_target_that_never_passed_the_boundary(tmp_path):
    from app.export.redactor import UnauthorizedTargetError, apply_plan

    s = _session_with_held(tmp_path)
    plan = s.plan()
    assert plan.targets and all(t.authorized_by for t in plan.targets)
    plan.targets[0].authorized_by = ""
    with pytest.raises(UnauthorizedTargetError):
        apply_plan(plan)


def test_registration_is_fail_closed_even_when_asked_to_fail_safe():
    c = cand(source=Source.NER, evidence=["spaCy PERSON"])
    dm = DecisionManager()
    dm.register([c], fail_safe=True)
    assert not dm.is_actionable(c) and dm.origin(c) == "REVIEW_REQUIRED"


def test_every_plan_entry_has_an_auditable_authorization_record(tmp_path):
    s = _session_with_held(tmp_path)
    plan = s.plan()
    ids = {e["candidate_id"] for e in plan.authorization_log}
    assert ids == {c.id for c in s.candidates}
    for e in plan.authorization_log:
        assert {"page", "rect", "pii_type", "source", "decision", "authorization", "reason", "outcome"} <= set(e)
        assert "text" not in e  # no values in the audit record


# -- workflow bypasses ------------------------------------------------------------------

def test_cli_accept_all_cannot_approve_review_required_items(tmp_path):
    path = _pdf(tmp_path / "cli.pdf", [(36, 60, "Taxpayer name:", "label"), (110, 60, "Torvin Quell", "value"),
                                       (36, 90, "Reference 573918204 was logged.", "value")])
    out = tmp_path / "cli.out.pdf"
    proc = subprocess.run([sys.executable, "-m", "app.cli", "process", path, "--accept-all", "-o", str(out)],
                          cwd=str(ROOT), capture_output=True, text=True,
                          env={**__import__("os").environ, "DOCANON_LLM": "0"})
    assert out.exists(), proc.stdout + proc.stderr
    text = "".join(p.get_text() for p in pymupdf.open(str(out)))
    assert "573918204" in text, "a review-required number was redacted by --accept-all"
    assert "Torvin Quell" not in text, "the confirmed name was not redacted"
    assert "LEFT READABLE" in proc.stdout


def test_batch_redact_everything_cannot_approve_review_required_items(tmp_path):
    s = _session_with_held(tmp_path)

    class Item:
        session = s
    from app.batch import Batch

    class OneItemBatch(Batch):   # a subclass: never touch the real class
        @property
        def approved(self):
            return [Item()]

    OneItemBatch.__new__(OneItemBatch).redact_everything()
    held = next(c for c in s.candidates if c.text == "573918204")
    assert not s.decisions.is_actionable(held)


def test_apply_to_all_does_not_approve_an_ambiguous_occurrence():
    a = cand("Quell", n=1, source=Source.COVERAGE)
    b = cand("Quell", n=2, source=Source.COVERAGE, ambiguous=True, needs_review=True)
    dm = DecisionManager()
    dm.register([a, b])
    dm.apply_to_all([a, b], a, DecisionState.ACCEPTED)
    assert dm.is_actionable(a) and not dm.is_actionable(b)


def test_apply_to_entity_does_not_approve_an_ambiguous_occurrence():
    from app.entities.identity import EntityIndex

    a = cand("Torvin Quell", n=1, source=Source.COVERAGE)
    b = cand("Quell", n=2, source=Source.COVERAGE, ambiguous=True, needs_review=True)
    dm = DecisionManager()
    dm.register([a, b])
    found = dm.apply_to_entity([a, b], a, DecisionState.ACCEPTED, index=EntityIndex.build([a, b]),
                               extra=[b])
    assert b not in found and not dm.is_actionable(b)


def test_second_pass_findings_obey_the_same_policy(tmp_path):
    s = _session_with_held(tmp_path)
    s.run_second_pass()
    for c in s.candidates:
        if s.decisions.is_actionable(c):
            assert s.decisions.origin(c) in ("AUTO_APPROVED", "USER_APPROVED", "USER_EDITED", "USER_ADDED")
            if s.decisions.origin(c) == "AUTO_APPROVED":
                assert assess(c, confirmed_name_words(s.candidates)).eligible


def test_status_says_review_is_incomplete_while_items_wait(tmp_path):
    s = _session_with_held(tmp_path)
    s.process(str(tmp_path / "out.pdf"))
    assert s.status == "EXPORT VERIFIED - REVIEW INCOMPLETE"
    for c in s.decisions.awaiting_review(s.candidates):
        s.decisions.set_state([c], DecisionState.SKIPPED)
    s.process(str(tmp_path / "out2.pdf"))
    assert s.status == "EXPORT VERIFIED"


def test_reopening_a_processed_pdf_does_not_redetect_the_pseudonyms(tmp_path):
    s = _session_with_held(tmp_path)
    out = tmp_path / "once.pdf"
    s.process(str(out))
    again = AnonymizationSession(source_path=str(out))
    again.analyse()
    replaced = {t.replacement for t in s.plan().targets}
    assert not any(again.decisions.is_actionable(c) and c.text in replaced for c in again.candidates)


def test_an_exact_confirmed_entity_outranks_soft_repetition_but_nothing_weaker_does():
    """A filer's name printed in every page header is soft-protected as 'repeats on
    every page'. An EXACT occurrence of the label-confirmed entity is still the filer;
    an alias or a model guess sitting there is not authorized by that rule."""
    reason = "sits on running header text - check it is really identity"
    exact = cand(source=Source.COVERAGE, evidence=["same value as an entity named in a subject field"],
                 needs_review=True, review_reason=reason)
    assert assess(exact).eligible
    guess = cand(source=Source.NER, evidence=["spaCy PERSON"], needs_review=True, review_reason=reason)
    assert not assess(guess, {"torvin", "quell"}).eligible
    alias = cand("Quell", source=Source.COVERAGE, evidence=["same value as an entity named in a subject field"],
                 needs_review=True, review_reason=reason + "; may be an unrelated person", ambiguous=True)
    assert not assess(alias).eligible
