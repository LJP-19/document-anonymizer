"""Apply-to-All through the real session: linked forms, found-but-never-detected
occurrences, composed edits, honest messages."""

from __future__ import annotations

import pymupdf

from app.decisions.manager import DecisionState
from app.session import AnonymizationSession


def _session(tmp_path, lines):
    doc = pymupdf.open()
    page = doc.new_page(); y = 70
    for line in lines:
        page.insert_text((60, y), line, fontsize=10); y += 16
    path = str(tmp_path / "t.pdf"); doc.save(path); doc.close()
    s = AnonymizationSession(source_path=path); s.analyse()
    return s


def _find(s, text):
    return next(c for c in s.candidates if c.text == text)


def test_skip_everywhere_reaches_every_linked_form(tmp_path):
    s = _session(tmp_path, ["Taxpayer Name: John Smith", "Prepared for JOHN SMITH", "Index: Smith, John"])
    result = s.apply_to_entity(_find(s, "John Smith"), DecisionState.SKIPPED)
    assert len(result.applied) >= 3
    assert all(s.decisions.state(c) is DecisionState.SKIPPED for c in result.applied)
    assert "Applied to" in result.message and "page" in result.message


def test_accepting_an_entity_finds_occurrences_that_were_never_candidates(tmp_path):
    s = _session(tmp_path, ["Taxpayer Name: José Núñez", "Cover note for Núñez, José and family"])
    seed = _find(s, "José Núñez")
    before = {c.id for c in s.candidates}
    s.detection.candidates = [c for c in s.detection.candidates if c.text == "José Núñez"]
    result = s.apply_to_entity(seed, DecisionState.ACCEPTED)
    assert any(c.text == "Núñez, José" for c in result.created), [c.text for c in result.created]
    assert all(s.decisions.is_actionable(c) for c in result.created)
    assert before  # the fixture produced candidates to begin with


def test_an_edit_is_composed_for_every_variant_and_registered(tmp_path):
    s = _session(tmp_path, ["Taxpayer Name: John Smith", "Prepared for JOHN SMITH", "Index: Smith, John"])
    result = s.apply_to_entity(_find(s, "John Smith"), DecisionState.EDITED, replacement="Mark Santos")
    overrides = {c.text: s.decisions.override(c) for c in result.applied}
    assert overrides["John Smith"] == "Mark Santos"
    assert overrides["JOHN SMITH"] == "MARK SANTOS"
    assert overrides["Smith, John"] == "Santos, Mark"
    plan = {t.original: t.replacement for t in s.plan().targets}
    assert plan.get("JOHN SMITH") == "MARK SANTOS" and plan.get("Smith, John") == "Santos, Mark"


def test_a_second_person_with_the_same_surname_is_untouched(tmp_path):
    s = _session(tmp_path, ["Taxpayer Name: John Smith", "Spouse: Mary Smith"])
    s.apply_to_entity(_find(s, "John Smith"), DecisionState.SKIPPED)
    assert s.decisions.state(_find(s, "Mary Smith")) is not DecisionState.SKIPPED


def test_ambiguous_short_forms_are_left_for_review_and_counted(tmp_path):
    s = _session(tmp_path, ["Taxpayer Name: John Smith", "Smith Barney held the account."])
    s.detection.candidates = [c for c in s.detection.candidates if c.text == "John Smith"]
    result = s.apply_to_entity(_find(s, "John Smith"), DecisionState.ACCEPTED)
    smith = [c for c in s.candidates if c.text == "Smith"]
    assert smith and all(c.needs_review and s.decisions.state(c) is DecisionState.SKIPPED for c in smith)
    assert "ambiguous" in result.message


def test_manual_add_of_a_full_name_also_finds_its_other_written_forms(tmp_path):
    s = _session(tmp_path, ["Reference: Zoltan Varga", "Signed by VARGA, ZOLTAN"])
    s.add_manual_text("Zoltan Varga", pii_type="PERSON", cascade=True, apply_to_same=True)
    texts = {c.text for c in s.candidates if s.decisions.is_actionable(c)}
    assert "Zoltan Varga" in texts and "VARGA, ZOLTAN" in texts


def test_manual_add_search_ignores_accents_and_case(tmp_path):
    s = _session(tmp_path, ["Client: José Núñez", "Sent to JOSE NUNEZ today"])
    added = s.add_manual_text("josé núñez", pii_type="PERSON", cascade=True, apply_to_same=True)
    assert {c.text for c in added} >= {"José Núñez", "JOSE NUNEZ"}


def test_apply_to_same_off_still_adds_a_single_occurrence(tmp_path):
    s = _session(tmp_path, ["Reference: Zoltan Varga", "Signed by VARGA, ZOLTAN"])
    added = s.add_manual_text("Zoltan Varga", pii_type="PERSON", cascade=True, apply_to_same=False)
    assert len(added) == 1


# -- read-only search: find what the detectors missed ------------------------------------

def test_search_reports_what_is_redacted_kept_and_undetected(tmp_path):
    s = _session(tmp_path, ["Taxpayer Name: John Smith", "Cover note for the Smith family", "Wire to Smith Barney"])
    smith = s.search_occurrences("Smith")
    assert len(smith) == 3 and smith[0].status == "redacted" and smith[0].pii_type == "PERSON"
    assert all(h.status in ("redacted", "kept") for h in smith), "every Smith was found by something"
    family = s.search_occurrences("family")
    assert [h.status for h in family] == ["undetected"] and family[0].candidate is None
    assert not [c for c in s.detection.candidates if "family" in c.text], "search must not add candidates"


def test_search_changes_nothing(tmp_path):
    s = _session(tmp_path, ["Taxpayer Name: John Smith", "Reference to Smith"])
    before = ([c.id for c in s.detection.candidates], dict((k, d.state) for k, d in s.decisions.decisions.items()))
    s.search_occurrences("Smith")
    after = ([c.id for c in s.detection.candidates], dict((k, d.state) for k, d in s.decisions.decisions.items()))
    assert before == after


def test_search_ignores_case_and_accents(tmp_path):
    s = _session(tmp_path, ["Client: José Núñez", "Sent to JOSE NUNEZ today"])
    assert len(s.search_occurrences("jose nunez")) == 2


def test_a_number_is_found_however_it_is_punctuated(tmp_path):
    s = _session(tmp_path, ["SSN: 123-45-6789", "Reference 123456789 on file", "Phone 408 555 0198"])
    hits = s.search_occurrences("123456789")
    assert len(hits) == 2
    assert s.search_occurrences("123-45-6789") and len(s.search_occurrences("123-45-6789")) == 2


def test_search_never_matches_inside_a_longer_word(tmp_path):
    s = _session(tmp_path, ["Smithson and Smith"])
    assert len(s.search_occurrences("Smith")) == 1


def test_search_hits_are_ordered_and_have_geometry_and_context(tmp_path):
    s = _session(tmp_path, ["First line Smith here", "Second line also Smith"])
    hits = s.search_occurrences("smith")
    assert [h.rect[1] for h in hits] == sorted(h.rect[1] for h in hits)
    assert all(h.rect[2] > h.rect[0] for h in hits)
    assert "[Smith]" in hits[0].context


def test_empty_or_unknown_search_is_empty(tmp_path):
    s = _session(tmp_path, ["Nothing here"])
    assert s.search_occurrences("") == [] and s.search_occurrences("zzzqqq") == []


def test_search_tells_protected_form_text_apart_from_a_real_miss(tmp_path):
    s = _session(tmp_path, ["Taxpayer Name: John Smith", "Total tax: $7,420", "Notes about the family"])
    label = s.search_occurrences("Total tax")
    figure = s.search_occurrences("7,420")
    plain = s.search_occurrences("family")
    assert [h.status for h in label] == ["protected"] and label[0].pii_type == "FORM_LABEL"
    assert [h.status for h in figure] == ["protected"]
    assert figure[0].pii_type in ("MONEY_AMOUNT", "NON_PII_FIELD")   # either protection explains it
    assert [h.status for h in plain] == ["undetected"], "unflagged ordinary text is what a reviewer must see"
