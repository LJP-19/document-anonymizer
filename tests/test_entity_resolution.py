"""Identity resolution: one person is one person however the document writes them."""

from __future__ import annotations

import pytest

from app.decisions.manager import DecisionManager, DecisionState
from app.detection.textnorm import fold, find_occurrences, parse_person, same_person
from app.detection.types import PiiType, Source
from app.entities.identity import EntityIndex, compose_replacement, rescan_entity
from tests.helpers_text import analysed


def _person(result, text):
    return next(c for c in result.candidates if c.text == text)


# -- text normalisation: the foundation ------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("José Núñez", "JOSE NUNEZ"), ("Garcia-Lopez", "Garcia Lopez"),
    ("O’Reilly", "O'Reilly"), ("Łukasz", "Lukasz"), ("İpek Yılmaz", "IPEK YILMAZ"),
])
def test_fold_ignores_case_accents_hyphens_and_apostrophes(a, b):
    assert fold(a) == fold(b)


def test_search_maps_back_to_the_original_characters():
    text = "Taxpayer: JOSE NUNEZ and José Núñez."
    found = [text[s:e] for s, e in find_occurrences(text, "josé núñez")]
    assert found == ["JOSE NUNEZ", "José Núñez"]


def test_search_never_matches_inside_a_longer_word():
    assert find_occurrences("李伟明 and 李伟", "李伟") == [(8, 10)]
    assert find_occurrences("Smithson and Smith", "Smith") == [(13, 18)]


@pytest.mark.parametrize("a,b,same", [
    ("John Smith", "Smith, John", True), ("John R. Smith", "John Smith", True),
    ("J. Smith", "John Smith", True), ("John Smith", "Mary Smith", False),
    ("John R Smith", "John T Smith", False), ("Smith", "John Smith", False),
    ("José Núñez", "NUNEZ, JOSE", True), ("محمد أحمد", "احمد، محمد", True),
])
def test_two_names_are_one_person_only_with_evidence(a, b, same):
    assert bool(same_person(parse_person(a), parse_person(b))) is same


# -- the index: what is linked, and why --------------------------------------------------

def test_name_variants_form_one_identity_with_recorded_evidence():
    _doc, result, _dm = analysed("Taxpayer Name: John Smith\nPrepared for JOHN SMITH\nBeneficiary index: Smith, John")
    index = EntityIndex.build(result.candidates)
    john = index.entity_of(_person(result, "John Smith"))
    assert john is not None and len(john.candidate_ids) >= 3
    assert any("same surname" in line for line in john.evidence), john.evidence


def test_a_shared_surname_does_not_merge_two_people():
    _doc, result, _dm = analysed("Taxpayer Name: John Smith\nSpouse: Mary Smith")
    index = EntityIndex.build(result.candidates)
    assert index.entity_of(_person(result, "Mary Smith")) is not index.entity_of(_person(result, "John Smith"))


def test_a_bare_surname_with_two_possible_owners_is_not_linked():
    _doc, result, _dm = analysed("Taxpayer Name: John Smith\nSpouse: Mary Smith\nContact: Smith")
    index = EntityIndex.build(result.candidates)
    for c in result.candidates:
        if c.text == "Smith":
            owner = index.entity_of(c)
            assert owner is None or not any(
                fold(t) in ("john smith", "mary smith") for t in owner.forms
            ), "an ambiguous surname was attached to one of two people"


def test_the_same_ssn_in_two_formats_is_one_identity():
    _doc, result, _dm = analysed("SSN: 123-45-6789\nSocial Security Number: 123456789")
    ssns = [c for c in result.candidates if c.pii_type is PiiType.SSN]
    index = EntityIndex.build(result.candidates)
    assert len(ssns) >= 2 and len({index.entity_of(c).entity_id for c in ssns}) == 1


# -- rescan: occurrences that never became candidates --------------------------------------

def test_rescan_recreates_a_variant_that_never_became_a_candidate():
    doc, result, _dm = analysed("Taxpayer Name: John Smith\nPrepared for JOHN SMITH\nBeneficiary index: Smith, John")
    profile = EntityIndex.build(result.candidates).entity_of(_person(result, "John Smith"))
    missing = [c for c in result.candidates if c.text in ("JOHN SMITH", "Smith, John")]
    assert len(missing) == 2
    without = [c for c in result.candidates if c not in missing]
    created, _amb = rescan_entity(doc, profile, without, result.protection)
    assert {c.text for c in created} == {"JOHN SMITH", "Smith, John"}
    for c in created:
        assert c.source is Source.COVERAGE and "known form" in c.evidence[0].detail and not c.needs_review


def test_a_bare_surname_in_an_unrelated_sentence_is_not_redacted_and_not_linked():
    _doc, result, dm = analysed("Taxpayer Name: John Smith\nSmith & Wesson makes firearms.")
    sentence = [c for c in result.candidates if "firearms" in c.text or "makes" in c.text]
    assert not sentence, "a whole sentence was swallowed by widening"
    index = EntityIndex.build(result.candidates)
    john = index.entity_of(_person(result, "John Smith"))
    for c in result.candidates:
        if c.text == "Smith":
            assert c.needs_review and dm.state(c) is DecisionState.SKIPPED, "an ambiguous guess must wait"
            assert c.id not in john.candidate_ids, "an ambiguous guess must not join the identity"


def test_rescan_reports_a_bare_surname_as_ambiguous_instead_of_redacting_it():
    doc, result, _dm = analysed("Taxpayer Name: John Smith\nSmith & Wesson makes firearms.")
    profile = EntityIndex.build(result.candidates).entity_of(_person(result, "John Smith"))
    without = [c for c in result.candidates if c.text != "Smith"]
    created, ambiguous = rescan_entity(doc, profile, without, result.protection)
    assert not any(c.text == "Smith" for c in created)
    assert any(c.text == "Smith" and c.needs_review for c in ambiguous)
    dm = DecisionManager(); dm.register(ambiguous)
    assert all(dm.state(c) is DecisionState.SKIPPED for c in ambiguous)


def test_rescan_does_not_touch_an_unrelated_person_with_the_same_surname():
    doc, result, _dm = analysed("Taxpayer Name: John Smith\nSpouse: Mary Smith")
    profile = EntityIndex.build(result.candidates).entity_of(_person(result, "John Smith"))
    created, _amb = rescan_entity(doc, profile, result.candidates, result.protection)
    assert not any("Mary" in c.text for c in created)


def test_rescan_never_creates_a_candidate_on_a_label_or_a_figure():
    doc, result, _dm = analysed("Taxpayer Name: John Smith\nJohn Smith Total tax: $12,500")
    profile = EntityIndex.build(result.candidates).entity_of(_person(result, "John Smith"))
    created, _amb = rescan_entity(doc, profile, result.candidates, result.protection)
    for c in created:
        assert "12,500" not in c.text and "Total" not in c.text and "Name:" not in c.text


def test_rescan_matches_a_unicode_identity_across_spellings():
    doc, result, _dm = analysed("Taxpayer Name: José Núñez\nPrepared for JOSE NUNEZ\nNúñez, José")
    profile = EntityIndex.build(result.candidates).entity_of(_person(result, "José Núñez"))
    created, _ = rescan_entity(doc, profile, result.candidates, result.protection)
    names = {c.text for c in result.candidates if c.pii_type is PiiType.PERSON} | {c.text for c in created}
    assert {"JOSE NUNEZ", "Núñez, José"} <= names


def test_rescan_finds_a_numeric_identifier_in_any_format():
    doc, result, _dm = analysed("SSN: 123-45-6789\nReference 123 45 6789 on file")
    ssn = next(c for c in result.candidates if c.text == "123-45-6789")
    profile = EntityIndex.build(result.candidates).entity_of(ssn)
    without = [c for c in result.candidates if c.text != "123 45 6789"]
    created, _ = rescan_entity(doc, profile, without, result.protection)
    assert [c.text for c in created] == ["123 45 6789"] and created[0].pii_type is PiiType.SSN


# -- decisions and edited replacements -------------------------------------------------------

def test_apply_to_entity_reaches_every_linked_form():
    _doc, result, dm = analysed("Taxpayer Name: John Smith\nSmith, John\nPrepared for JOHN SMITH")
    like = _person(result, "John Smith")
    index = EntityIndex.build(result.candidates)
    linked = dm.apply_to_entity(result.candidates, like, DecisionState.SKIPPED, index=index)
    assert len(linked) >= 2 and all(dm.state(c) is DecisionState.SKIPPED for c in linked)


@pytest.mark.parametrize("observed,expected", [
    ("John Smith", "Mark Santos"), ("Smith, John", "Santos, Mark"), ("JOHN SMITH", "MARK SANTOS"),
    ("Smith", "Santos"), ("John", "Mark"), ("J. Smith", "M. Santos"),
])
def test_an_edited_replacement_is_rendered_per_variant(observed, expected):
    assert compose_replacement("Mark Santos", observed, context=["John Smith"]) == expected


def test_editing_one_form_edits_every_linked_form_consistently():
    _doc, result, dm = analysed("Taxpayer Name: John Smith\nSmith, John")
    like = _person(result, "John Smith")
    found = dm.apply_to_entity(result.candidates, like, DecisionState.EDITED, replacement="Mark Santos")
    overrides = {c.text: dm.override(c) for c in found}
    assert overrides.get("John Smith") == "Mark Santos"
    if "Smith, John" in overrides:
        assert overrides["Smith, John"] == "Santos, Mark"


def test_old_exact_apply_to_all_still_works():
    _doc, result, dm = analysed("Taxpayer Name: John Smith\nPrepared for John Smith")
    like = _person(result, "John Smith")
    same = dm.apply_to_all(result.candidates, like, DecisionState.SKIPPED)
    assert same and all(dm.state(c) is DecisionState.SKIPPED for c in same)
