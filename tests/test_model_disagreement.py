"""When a real model disagrees with a label or with the shape of the text.

These were found by running the suite against the shipped models (en_core_web_trf and
GLiNER) instead of the small stand-in used for quick local runs: a spouse's Arabic name
came back as a city, a Vietnamese name as a job title, a surname above an address as a
street. The tests here are hermetic - the model's wrong answer is built by hand - so
they hold whichever models happen to be installed.
"""

from __future__ import annotations

from app.detection import engine as E
from app.detection.adjudication import Adjudication, adjudicate
from app.detection.groups import build_groups
from app.detection.provider_shim import document_from_pages
from app.detection.types import Candidate, Evidence, PiiType, Source


def _doc(text):
    doc = document_from_pages([text])
    return doc, doc.pages[0].lines[0]


def _cand(line, text, pii_type, source=Source.NER, detail="GLiNER: x", weight=0.55, nth=0):
    start = -1
    for _ in range(nth + 1):
        start = line.text.index(text, start + 1)
    end = start + len(text)
    return Candidate(pii_type, text, line.page_no, line.rect_for(start, end), line, start, end,
                     weight, source, [Evidence(source, detail, weight)])


# -- the label outranks a model's guess, for a value on the label's own line ----------------

def test_a_value_on_the_labels_own_line_is_retyped_by_the_label():
    doc, line = _doc("Spouse: محمد أحمد")
    wrong = _cand(line, "محمد أحمد", PiiType.CITY_STATE, detail="GLiNER: city and state")
    groups, _labels, _extra = build_groups(doc, [wrong])
    E._retype_from_labels([wrong], groups)
    assert wrong.pii_type is PiiType.PERSON
    assert any("retyped CITY_STATE -> PERSON" in e.detail for e in wrong.evidence)


def test_only_the_value_after_each_label_is_retyped_on_a_multi_field_line():
    doc, line = _doc("Date of birth 11/02/1979   Nationality: Brazil")
    dob = _cand(line, "11/02/1979", PiiType.DOB, Source.REGEX, "rule=dob", 0.88)
    brazil = _cand(line, "Brazil", PiiType.CITY_STATE, detail="GLiNER: city and state")
    groups, _l, _e = build_groups(doc, [dob, brazil])
    E._retype_from_labels([dob, brazil], groups)
    assert (dob.pii_type, brazil.pii_type) == (PiiType.DOB, PiiType.CITIZENSHIP)


def test_a_pattern_match_on_a_shared_line_is_not_overruled_by_a_neighbours_label():
    doc, line = _doc("Date of birth 11/02/1979   Nationality: 123-45-6789")
    ssn = _cand(line, "123-45-6789", PiiType.SSN, Source.REGEX, "rule=ssn", 0.95)
    groups, _l, _e = build_groups(doc, [ssn])
    E._retype_from_labels([ssn], groups)
    assert ssn.pii_type is PiiType.SSN


def test_a_label_bound_value_the_model_typed_correctly_gains_the_label_as_evidence():
    """"Dương Thị Hồng" under "Taxpayer Name:" had only a 0.49 model guess - UNRESOLVED."""
    doc, line = _doc("Taxpayer Name: Dương Thị Hồng")
    weak = _cand(line, "Dương Thị Hồng", PiiType.PERSON, detail="GLiNER: spouse name", weight=0.49)
    assert adjudicate(weak) is Adjudication.UNRESOLVED
    groups, _l, _e = build_groups(doc, [weak])
    E._retype_from_labels([weak], groups)
    assert adjudicate(weak) is not Adjudication.UNRESOLVED
    assert any(e.source is Source.GROUP for e in weak.evidence)


def test_an_unrecognised_label_adds_no_evidence():
    doc, line = _doc("Colour of the car: Dương Thị Hồng")
    weak = _cand(line, "Dương Thị Hồng", PiiType.PERSON, weight=0.49)
    groups, _l, _e = build_groups(doc, [weak])
    E._retype_from_labels([weak], groups)
    assert not any(e.source is Source.GROUP for e in weak.evidence)


# -- a name next to an address is not an address --------------------------------------------

def test_a_name_the_model_called_a_street_is_a_person():
    doc, line = _doc("Sławomir Nowak")
    c = _cand(line, "Sławomir Nowak", PiiType.STREET, detail="GLiNER: street address", weight=0.65)
    E._retype_name_shaped_places([c])
    assert c.pii_type is PiiType.PERSON and c.needs_review


def test_a_caseless_script_name_is_recognised_too():
    doc, line = _doc("李伟")
    c = _cand(line, "李伟", PiiType.STREET, detail="GLiNER: street address", weight=0.75)
    E._retype_name_shaped_places([c])
    assert c.pii_type is PiiType.PERSON


def test_a_real_street_a_confirmed_place_and_one_word_are_left_alone():
    doc, line = _doc("Main Street 4")
    street = _cand(line, "Main Street 4", PiiType.STREET, detail="spaCy FAC", weight=0.6)
    doc2, line2 = _doc("Santa Cruz")
    city = _cand(line2, "Santa Cruz", PiiType.CITY_STATE, detail="GLiNER: city and state", weight=0.68)
    city.evidence.append(Evidence(Source.NER, "spaCy GPE", 0.6))
    doc3, line3 = _doc("Springfield")
    town = _cand(line3, "Springfield", PiiType.CITY_STATE, detail="GLiNER: city and state", weight=0.5)
    doc4, line4 = _doc("Maple Grove Road")
    road = _cand(line4, "Maple Grove Road", PiiType.STREET, detail="GLiNER: street address", weight=0.6)
    E._retype_name_shaped_places([street, city, town, road])
    assert [c.pii_type for c in (street, city, town, road)] == [
        PiiType.STREET, PiiType.CITY_STATE, PiiType.CITY_STATE, PiiType.STREET]


def test_a_place_type_vouched_for_by_a_rule_or_label_is_never_retyped():
    doc, line = _doc("Ana Maria")
    c = _cand(line, "Ana Maria", PiiType.STREET, Source.REGEX, "rule=street", 0.9)
    g = _cand(line, "Ana Maria", PiiType.CITY_STATE)
    g.evidence.append(Evidence(Source.GROUP, "value of the labelled field 'City'", 0.7))
    E._retype_name_shaped_places([c, g])
    assert (c.pii_type, g.pii_type) == (PiiType.STREET, PiiType.CITY_STATE)


# -- an identifier with no digit or symbol --------------------------------------------------

def test_a_plain_word_the_model_called_a_username_waits_for_a_person():
    doc, line = _doc("She held a passport from Espana")
    c = _cand(line, "Espana", PiiType.USERNAME, detail="GLiNER: username", weight=0.49)
    E._doubt_shapeless_identifiers([c])
    assert adjudicate(c) is Adjudication.UNRESOLVED and c.needs_review


def test_a_real_looking_username_or_a_labelled_one_is_untouched():
    doc, line = _doc("login jsmith_42 and Username: sam")
    shaped = _cand(line, "jsmith_42", PiiType.USERNAME, weight=0.49)
    labelled = _cand(line, "sam", PiiType.USERNAME, weight=0.49)
    labelled.evidence.append(Evidence(Source.GROUP, "value of the labelled field 'Username'", 0.7))
    E._doubt_shapeless_identifiers([shaped, labelled])
    assert adjudicate(shaped) is not Adjudication.UNRESOLVED
    assert adjudicate(labelled) is not Adjudication.UNRESOLVED


# -- a model's "job title" does not delete a label-bound value -------------------------------

def _negative_over(c, label="job title or occupation"):
    x0, y0, x1, y1 = c.rect
    return [(c.page_no, (x0 - 2, y0 - 2, x1 + 2, y1 + 2), label)]


def test_a_job_title_guess_does_not_delete_a_value_under_an_identity_label():
    doc, line = _doc("Dependent: Dương Thị Hồng")
    c = _cand(line, "Dương Thị Hồng", PiiType.PERSON, Source.GROUP, "value line", 0.7)
    groups, _l, extra = build_groups(doc, [])
    bound = extra[0]
    kept = E._veto([bound], _negative_over(bound), groups)
    assert kept == [bound] and bound.needs_review and "job title" in bound.review_reason


def test_the_veto_still_removes_an_unlabelled_figure_or_job_title():
    doc, line = _doc("Software Engineer")
    c = _cand(line, "Software Engineer", PiiType.PERSON, weight=0.5)
    assert E._veto([c], _negative_over(c), []) == []


def test_the_veto_still_removes_a_value_under_a_label_that_is_not_an_identity():
    doc, line = _doc("Occupation: Software Engineer")
    groups, _labels, extra = build_groups(doc, [])
    c = _cand(line, "Software Engineer", PiiType.PERSON, weight=0.5)
    assert E._veto([c], _negative_over(c), groups) == []
