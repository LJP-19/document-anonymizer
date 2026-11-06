"""Strict PII-only (0.41.0): every false positive found on real returns, pinned.

Owner's rule: ONLY CONFIRMED PII IS PSEUDONYMIZED AUTOMATICALLY. Everything else
stays as printed. Each case below rebuilds the SHAPE of a measured failure with
invented values and fails if the layer that was wrong regresses.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pymupdf
import pytest

from app.decisions.manager import DecisionState
from app.detection.types import Candidate, PiiType, Source
from app.session import AnonymizationSession
from tests.helpers_text import analysed

TEMPLATE = (0x23 / 255, 0x1F / 255, 0x20 / 255)
RED = (1.0, 0.0, 0.0)


def _pdf(path: Path, items: list[tuple[float, float, str, str]]) -> str:
    """items: (x, y, text, kind) - kind 'label' (template), 'value' (black Courier), 'red' (prior pseudonym)."""
    doc = pymupdf.open()
    page = doc.new_page()
    for x, y, text, kind in items:
        if kind == "label":
            page.insert_text((x, y), text, fontsize=7, fontname="helv", color=TEMPLATE)
        elif kind == "red":
            page.insert_text((x, y), text, fontsize=7, fontname="helv", color=RED)
        else:
            page.insert_text((x, y), text, fontsize=10, fontname="cour", color=(0, 0, 0))
    doc.save(str(path))
    doc.close()
    return str(path)


def _applied(s: AnonymizationSession) -> list[str]:
    return [c.text for c in s.candidates if s.decisions.is_actionable(c)]


# -- text an earlier run already replaced -------------------------------------------------

def test_text_an_earlier_run_replaced_is_left_exactly_as_it_is(tmp_path):
    """Real returns processed by an earlier version carry red pseudonyms where FORM
    WORDING used to be ("Do not write or staple", "Roth IRA"). A model read those
    synthetic names as people; re-replacing them painted headings and sidebars red
    again and swallowed the neighbouring form words ("Do", "See")."""
    path = _pdf(tmp_path / "prior.pdf", [
        (400, 40, "IRS Use Only", "label"),
        (400, 52, "Morgan Bates", "red"),
        (448, 52, "Do not write or staple", "label"),
        (36, 90, "Your first name and middle initial", "label"),
        (36, 102, "TORVIN", "value"),
        (36, 140, "Complete this part if you converted all of your IRA to a", "label"),
        (240, 140, "Keith Lauren", "red"),
        (285, 140, ". See instructions.", "label"),
    ])
    s = AnonymizationSession(source_path=path)
    s.analyse()
    applied = _applied(s)
    assert "TORVIN" in applied
    for word in ("Morgan", "Bates", "Keith", "Lauren", "Do", "See"):
        assert not any(word in t for t in applied), (word, applied)


def test_the_gate_rejects_a_model_proposal_over_prior_replacement_text(tmp_path):
    """Model-independent: a proposal covering prior red text plus the neighbouring
    form word ("Morgan Bates Do") is rejected outright - not clipped to "Do"."""
    from app.detection.protection import CandidateGate, ProtectionIndex, drop_prior_replacements
    from app.document.provider import NativePdfTextProvider

    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((400, 52), "Morgan Bates", fontsize=7, fontname="helv", color=RED)
    page.insert_text((445, 52), "Do not write", fontsize=7, fontname="helv", color=TEMPLATE)
    pdf.save(str(tmp_path / "gate2.pdf"))
    doc = NativePdfTextProvider().load(str(tmp_path / "gate2.pdf"))
    line = next(l for l in doc.pages[0].lines if "Morgan" in l.text)
    end = line.text.index("Do") + 2
    c = Candidate(pii_type=PiiType.PERSON, text=line.text[:end], page_no=0, rect=line.rect_for(0, end),
                  line=line, start=0, end=end, confidence=0.82, source=Source.NER)
    index = ProtectionIndex.build(doc, [], None, [])
    kept, dropped = drop_prior_replacements([c], index)
    assert kept == [] and dropped == 1
    gated, log_ = CandidateGate(index).run([c])
    assert gated == [] and log_[0].kind == "PRIOR_REPLACEMENT"


def test_a_synthetic_name_never_becomes_an_entity_that_spreads(tmp_path):
    """A prior pseudonym must not enter the registry: its words elsewhere, in black,
    are not the client and are not replaced."""
    path = _pdf(tmp_path / "spread.pdf", [
        (36, 40, "Rowe Pallas", "red"),
        (36, 80, "Your first name and middle initial", "label"),
        (36, 92, "TORVIN", "value"),
        (36, 140, "Pallas", "value"),
    ])
    s = AnonymizationSession(source_path=path)
    s.analyse()
    assert not any("Pallas" in t for t in _applied(s))


# -- a shape alone is not confirmation ----------------------------------------------------

def test_nine_bare_digits_with_no_ssn_label_are_held_not_applied():
    """A software control number on a code line ("573918204 SS EXT 01 ...") was typed
    SSN by shape alone - printed identically on two different people's returns."""
    _doc, result, decisions = analysed("Control number\n573918204 SS EXT 01 0 240115 012")
    hits = [c for c in result.candidates if c.text == "573918204"]
    assert hits and not any(decisions.is_actionable(c) for c in hits)


def test_nine_digits_under_an_ssn_label_are_still_applied():
    _doc, result, decisions = analysed("Your social security number: 612407731")
    assert any(c.text == "612407731" and c.pii_type is PiiType.SSN and decisions.is_actionable(c)
               for c in result.candidates)


def test_a_lone_all_caps_code_is_not_a_name():
    """"MFJ" next to "Filing status" was proposed as a PERSON by the model."""
    from app.detection.engine import _drop_implausible_model_hits

    line = NS(text="MFJ", spans=[], key=lambda: (0, 0, 0))
    c = Candidate(pii_type=PiiType.PERSON, text="MFJ", page_no=0, rect=(0, 0, 10, 10), line=line,
                  start=0, end=3, confidence=0.82, source=Source.NER)
    assert _drop_implausible_model_hits([c]) == []


def test_a_heading_shaped_like_a_joint_name_is_not_applied():
    """"Qualified Dividends and Capital Gain Tax Worksheet" matched "X and Y Z"."""
    _doc, result, decisions = analysed("Qualified Dividends and Capital Gain Tax Worksheet\n4,812.33")
    assert not any(decisions.is_actionable(c) for c in result.candidates)


def test_an_unlabelled_name_shaped_phrase_is_held_but_a_confirmed_persons_name_is_applied():
    """Corroboration, not deletion: "Fidelity Contrafund" alone on a line is a guess;
    the client's own name repeated in a sentence is confirmed by the label."""
    _doc, result, decisions = analysed(
        "Taxpayer name: Torvin Quell\nFidelity Contrafund\nPrepared for Torvin Quell on request.")
    applied = [c.text for c in result.candidates if decisions.is_actionable(c)]
    assert not any("Fidelity" in t or "Contrafund" in t for t in applied), applied
    assert sum("Torvin Quell" in t for t in applied) >= 2, applied


# -- organizations and labels ---------------------------------------------------------------

@pytest.mark.parametrize("label", ["Firm's name", "Employer's name"])
def test_a_possessive_organization_label_is_not_a_person_label(label):
    from app.detection.deterministic import load_rules
    from app.detection.labels import _match_label

    assert _match_label(label, load_rules())[0] == [PiiType.ORG_PRIVATE]


def test_organization_names_are_held_not_replaced():
    """Owner: organization and employer names are not PII."""
    _doc, result, decisions = analysed("Firm's name: Northgate Tax Partners LLC")
    orgs = [c for c in result.candidates if "Northgate" in c.text]
    assert orgs and not any(decisions.is_actionable(c) for c in orgs)


def test_a_value_beside_its_label_on_the_same_row_is_bound(tmp_path):
    """Extraction split "Spouse's name    Bela Quell" into two lines; the value was
    left to the unlabelled-line fallback."""
    path = _pdf(tmp_path / "row.pdf", [(36, 100, "Spouse's name", "label"), (110, 100, "Bela Quell", "value")])
    s = AnonymizationSession(source_path=path)
    s.analyse()
    assert any("Bela Quell" in c.text and c.group_id for c in s.candidates if s.decisions.is_actionable(c))


# -- the false-positive corpus: zero wrongly changed words ---------------------------------

def test_the_false_positive_corpus_has_no_false_positives(tmp_path):
    from benchmark.false_positive_corpus import build_all
    from benchmark.output_scoring import NOT_PII, extract_words, kept_mask, score
    from benchmark.run_forms_benchmark import geometric_labeller

    corpus = build_all(tmp_path / "in")
    for name, (path, items) in corpus.items():
        s = AnonymizationSession(source_path=path)
        s.analyse()
        out = str(tmp_path / f"{name}.out.pdf")
        s.process(out)
        src = extract_words(path)
        result = score(src, kept_mask(src, extract_words(out)), geometric_labeller(items))
        assert result.fp == 0, (name, result.as_dict()["by_category"])
        assert result.fn == 0, (name, result.as_dict()["by_category"])
