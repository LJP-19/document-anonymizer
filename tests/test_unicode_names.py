"""Names in any script: detected, matched across spellings, and pseudonymised consistently."""

from __future__ import annotations

import pytest

from app.detection.heuristics import looks_like_person
from app.entities.registry import EntityRegistry
from app.detection.types import PiiType
from app.pseudonymization.names import NameRegistry
from tests.helpers_text import redacted

NAMES = [
    "José Núñez", "Élodie Martin", "Łukasz Kowalski", "Nguyễn Văn Minh", "Sławomir Nowak",
    "İpek Yılmaz", "Željko Petrović", "李伟", "محمد أحمد", "Hans Müller", "Núñez, José",
    "Øystein Haugen", "Dương Thị Hồng", "Müller-Schmidt, Hans",
]


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("template", ["Taxpayer Name: {n}", "Spouse: {n}", "Dependent: {n}"])
def test_a_labelled_name_in_any_script_is_redacted_as_a_person(template, name):
    got = redacted(template.format(n=name))
    assert [(t, k) for t, k in got if name in t or t in name] == [(name, "PERSON")], got


@pytest.mark.parametrize("name", NAMES)
def test_an_unlabelled_name_line_is_redacted_as_a_person(name):
    got = redacted(f"{name}\nMain Street 4")
    hits = [(t, k) for t, k in got if "Main" not in t]
    assert hits and all(k == "PERSON" for _t, k in hits), got


@pytest.mark.parametrize("text,expected", [
    ("Springfield, CA", False), ("Total Wages Paid", False), ("Need to Keep", False),
    ("Form 1040", False), ("Taxpayer Name", False), ("john smith", False), ("Smith, John", True),
    ("李", False),
])
def test_name_shape_still_rejects_form_text_and_places(text, expected):
    assert looks_like_person(text)[0] is expected


def test_accent_case_and_order_variants_share_one_pseudonym():
    names = NameRegistry(scope="t")
    a, b, c = names.pseudonym("José Núñez"), names.pseudonym("JOSE NUNEZ"), names.pseudonym("Núñez, José")
    assert b == a.upper()
    assert c == f"{a.split()[1]}, {a.split()[0]}"
    assert names.pseudonym("Núñez") == a.split()[1] and names.pseudonym("José") == a.split()[0]


def test_person_entity_keys_ignore_case_accents_and_hyphens():
    registry = EntityRegistry()
    keys = {registry.key_for(PiiType.PERSON, t) for t in ("José Núñez", "JOSE NUNEZ", "José-Núñez")}
    assert len(keys) == 1


def test_a_surname_first_name_with_diacritics_keeps_its_structure():
    out = NameRegistry(scope="t").pseudonym("Núñez, José")
    assert "," in out and out.split(",")[0].strip() != "Núñez"


def test_a_single_caseless_character_is_a_name_not_an_initial():
    from app.pseudonymization.names import _is_structural

    assert not _is_structural("伟") and _is_structural("J")


def test_a_block_of_only_non_latin_text_is_not_skipped_as_numbers_only():
    got = redacted("李伟\n85,000")
    assert ("李伟", "PERSON") in got
