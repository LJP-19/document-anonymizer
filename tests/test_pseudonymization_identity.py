"""Identity-level pseudonymization (0.40.0): one person, one synthetic identity.

Owner's acceptance tests A-J. Every name and value is invented. Assertions are
on behaviour that holds whichever NER model is installed: names are bound by
field labels so the deterministic layers establish the identity.
"""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf
import pytest

from app.decisions.manager import DecisionState
from app.session import AnonymizationSession

TEMPLATE = (0x23 / 255, 0x1F / 255, 0x20 / 255)
RED_INT = 0xFF0000


def _pdf(path: Path, pages: list[list[tuple]]) -> str:
    """pages: [(x, y, text, kind)] kind: label | value | prose."""
    doc = pymupdf.open()
    for items in pages:
        page = doc.new_page()
        for x, y, text, kind in items:
            if kind == "label":
                page.insert_text((x, y), text, fontsize=7, fontname="helv", color=TEMPLATE)
            elif kind == "prose":
                page.insert_text((x, y), text, fontsize=9, fontname="helv", color=(0, 0, 0))
            else:
                page.insert_text((x, y), text, fontsize=10, fontname="cour", color=(0, 0, 0))
    doc.save(str(path))
    doc.close()
    return str(path)


def _visual_lines(page) -> str:
    """Text as a reader SEES it: spans grouped by baseline, ordered by x.

    Plain get_text() lists newly inserted spans after the original content, so
    "Megan Cox & Sarah Cox" drawn in place extracts as "& Megan Cox Sarah Cox"
    (CLAUDE.md: plain get_text() is not proof of how a page reads)."""
    rows: dict[int, list] = {}
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            for sp in line["spans"]:
                if sp["text"].strip():
                    rows.setdefault(round(sp["origin"][1]), []).append(sp)
    out = []
    for y in sorted(rows):
        spans = sorted(rows[y], key=lambda sp: sp["bbox"][0])
        text = ""
        for sp in spans:
            piece = sp["text"]
            if text and not text.endswith(" ") and not piece.startswith((" ", ",", ".", "'", "\u2019")):
                text += " "
            text += piece
        out.append(" ".join(text.split()))
    return "\n".join(out)


def _run(path: str, accept_all: bool = False):
    s = AnonymizationSession(source_path=path)
    s.analyse()
    if accept_all:
        s.decisions.set_state(s.candidates, DecisionState.ACCEPTED)
    out = Path(path).with_name(Path(path).stem + ".out.pdf")
    s.process(str(out))
    with pymupdf.open(str(out)) as d:
        pages = [_visual_lines(p) for p in d]
    return s, str(out), pages


def _mapping(s) -> dict[str, str]:
    return {t.original: t.replacement for t in s.plan().targets}


HEADER = [
    (36, 60, "Your first name and middle initial", "label"),
    (250, 60, "Last name", "label"),
    (430, 60, "Your social security number", "label"),
    (36, 74, "Torvin", "value"),
    (250, 74, "Quell", "value"),
    (430, 74, "512-44-0193", "value"),
]


# --- A: full name, many pages ---------------------------------------------------

@pytest.fixture(scope="module")
def single(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("single")
    path = _pdf(tmp / "Torvin Quell 1040-Latest.pdf", [
        HEADER + [(36, 120, "Name(s) shown on return", "label"), (36, 134, "Torvin Quell", "value")],
        [(36, 60, "Name(s) shown on return", "label"), (36, 74, "Torvin Quell", "value"),
         (36, 120, "Torvin confirmed the transfer on the same day.", "prose")],
        [(36, 60, "Mr. Quell signed the statement. Torvin Quell's account was closed.", "prose"),
         (36, 80, "The Torvin River Trail is unrelated to this return.", "prose")],
    ])
    return _run(path)


def test_A_full_name_has_one_synthetic_identity_everywhere(single):
    s, _out, pages = single
    m = _mapping(s)
    full = {v for k, v in m.items() if k == "Torvin Quell"}
    assert len(full) == 1, "one person, one synthetic full name"
    joined = "\n".join(pages)
    assert "Torvin Quell" not in joined and "Quell" not in joined


def test_B_first_name_alone_gets_the_synthetic_first_name(single):
    s, _out, pages = single
    m = _mapping(s)
    synthetic_first, synthetic_last = m["Torvin Quell"].split()
    assert m.get("Torvin") == synthetic_first
    assert synthetic_first + " confirmed the transfer" in pages[1].replace("\n", " ")


def test_B_an_unrelated_use_of_the_first_name_is_left_alone(single):
    s, _out, pages = single
    assert "Torvin River Trail" in pages[2]
    on_line = [c for c in s.candidates if c.page_no == 2 and "River" in c.line.text and "Torvin" in c.text]
    assert all(not s.decisions.is_actionable(c) for c in on_line)


def test_B_an_ambiguous_component_is_reported_and_not_applied(tmp_path):
    """Context says this "Quell" belongs to another name: it is listed, never applied."""
    path = _pdf(tmp_path / "amb.pdf", [
        HEADER + [(36, 120, "Name(s) shown on return", "label"), (36, 134, "Torvin Quell", "value")],
        [(36, 60, "Quell Harrowgate Partners prepared the appraisal.", "prose")],
    ])
    s, _out, pages = _run(path)
    held = [c for c in s.candidates if c.ambiguous]
    assert "Quell Harrowgate" in pages[1]
    assert all(not s.decisions.is_actionable(c) for c in held)


def test_C_surname_alone_and_with_title(single):
    s, _out, pages = single
    m = _mapping(s)
    synthetic_last = m["Torvin Quell"].split()[1]
    text = pages[2].replace("\n", " ")
    assert f"Mr. {synthetic_last} signed" in text
    assert f"{m['Torvin Quell']}'s account" in text


# --- D: joint filers --------------------------------------------------------------

@pytest.fixture(scope="module")
def joint(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("joint")
    path = _pdf(tmp / "Corin Tash_Bela Tash 1040-Latest.pdf", [
        [(36, 60, "Electronic Filing Instructions for your 2021 Federal Tax Return", "prose"),
         (36, 90, "Corin Tash & Bela Tash", "value")],
        [(36, 60, "Your first name and middle initial", "label"), (230, 60, "Last name", "label"),
         (36, 74, "Corin", "value"), (230, 74, "Tash", "value"),
         (36, 90, "If joint return, spouse's first name and middle initial", "label"),
         (230, 90, "Last name", "label"),
         (36, 104, "Bela", "value"), (230, 104, "Tash", "value"),
         (36, 140, "Name(s) shown on return", "label"), (36, 154, "Corin & Bela Tash", "value")],
        [(36, 60, "Mr. Corin Tash and Mrs. Bela Tash signed. Corin Tash, Bela Tash.", "prose")],
    ])
    return _run(path)


def test_D_joint_filers_stay_two_people_in_order(joint):
    s, _out, pages = joint
    m = _mapping(s)
    a, b = m["Corin Tash"], m["Bela Tash"]
    assert a != b, "two people, two identities"
    assert a.split()[1] == b.split()[1], "a shared real surname keeps a shared synthetic surname"
    first = pages[0].replace("\n", " ")
    assert f"{a} & {b}" in first, first
    third = pages[2].replace("\n", " ")
    assert f"Mr. {a} and Mrs. {b} signed" in third
    assert f"{a}, {b}" in third


def test_D_given_names_keep_their_role(joint):
    """A first name met alone must not become a SURNAME ("Espinoza Harris")."""
    s, _out, _pages = joint
    m = _mapping(s)
    assert m.get("Corin") == m["Corin Tash"].split()[0]
    assert m.get("Bela") == m["Bela Tash"].split()[0]
    assert m.get("Tash") == m["Corin Tash"].split()[1]


def test_D_three_people_are_three_identities(tmp_path):
    path = _pdf(tmp_path / "three.pdf", [[
        (36, 60, "Name(s) shown on return", "label"),
        (36, 74, "Arlo Venn, Mira Venn & Oska Venn", "value"),
    ]])
    s, _out, pages = _run(path)
    text = pages[0]
    for original in ("Arlo", "Mira", "Oska", "Venn"):
        assert original not in text
    assert text.count("&") == 1 and text.count(",") >= 1


# --- E: shared and short names ----------------------------------------------------

def test_E_people_sharing_a_first_name_remain_distinct(tmp_path):
    path = _pdf(tmp_path / "shared.pdf", [[
        (36, 60, "Taxpayer name", "label"), (36, 74, "Rowe Pallas", "value"),
        (36, 100, "Preparer's name", "label"), (36, 114, "Rowe Kintner", "value"),
    ]])
    s, _out, _pages = _run(path)
    m = _mapping(s)
    assert m["Rowe Pallas"] != m["Rowe Kintner"]
    assert m["Rowe Pallas"].split()[1] != m["Rowe Kintner"].split()[1]


def test_E_a_short_surname_is_tracked(tmp_path):
    path = _pdf(tmp_path / "short.pdf", [
        [(36, 60, "Your first name", "label"), (230, 60, "Last name", "label"),
         (36, 74, "Kai", "value"), (230, 74, "Lim", "value")],
        [(36, 60, "Name(s) shown on return", "label"), (36, 74, "Kai Lim", "value"),
         (36, 100, "Mr. Lim agreed.", "prose")],
    ])
    s, _out, pages = _run(path)
    assert not re.search(r"\bLim\b", "\n".join(pages))


# --- F: every PII type is stable --------------------------------------------------

def test_F_repeated_values_get_one_replacement_each(tmp_path):
    page = [
        (36, 60, "Your social security number", "label"), (36, 74, "512-44-0193", "value"),
        (36, 100, "Home address (number and street)", "label"), (36, 114, "77 Larkfield Ter", "value"),
        (36, 140, "Phone no.", "label"), (100, 140, "(415)555-0172", "value"),
        (36, 170, "Email address", "label"), (36, 184, "t.quell@example.net", "value"),
        (36, 210, "Date of birth", "label"), (36, 224, "03/14/1979", "value"),
    ]
    path = _pdf(tmp_path / "repeat.pdf", [page, page, page])
    s, _out, pages = _run(path)
    by_original: dict[str, set] = {}
    for t in s.plan().targets:
        by_original.setdefault(" ".join(t.original.split()), set()).add(t.replacement)
    for original in ("512-44-0193", "77 Larkfield Ter", "(415)555-0172", "t.quell@example.net", "03/14/1979"):
        assert len(by_original.get(original, set())) == 1, original
    assert pages[0] == pages[1] == pages[2], "the same page pseudonymises identically"
    assert re.search(r"\d\d/\d\d/\d{4}", pages[0]), "a date stays a date"
    assert "@example" in pages[0] or "@" in pages[0]


# --- G: form content is not PII ---------------------------------------------------

def test_G_form_content_is_never_replaced(tmp_path):
    text = [
        (36, 60, "Form 1040 U.S. Individual Income Tax Return 2025", "label"),
        (36, 80, "Standard Deduction", "label"),
        (36, 94, "Attach Form 1040 Schedule B if required.", "label"),
        (36, 108, "Presidential Election Campaign", "label"),
        (36, 122, "Your occupation", "label"), (36, 136, "Financial Analyst", "value"),
        (36, 160, "Date of Withdrawal: 04/15/2026", "prose"),
        (36, 174, "Payment Due Date: 04/15/2026", "prose"),
        (36, 188, "Wages, salaries, tips $12,500.00", "prose"),
        (36, 202, "Employer: Northwind Logistics Inc.", "prose"),
        (36, 230, "Mail payments to:", "prose"), (36, 244, "Internal Revenue Service", "prose"),
        (36, 258, "PO BOX 802502", "prose"), (36, 272, "CINCINNATI OH 45280-2502", "prose"),
        (36, 300, "Tax Year 2025", "prose"),
    ]
    path = _pdf(tmp_path / "form_only.pdf", [text])
    s, _out, pages = _run(path)
    applied = [t.original for t in s.plan().targets]
    assert applied == [], f"non-PII replaced: {applied}"


# --- H / I: red, on the baseline, nothing covered ------------------------------------

def test_H_every_replacement_is_red_and_nothing_else_is(single):
    s, out, _pages = single
    replacements = {t.replacement for t in s.plan().targets}
    with pymupdf.open(out) as d:
        for page in d:
            for b in page.get_text("dict")["blocks"]:
                for line in b.get("lines", []):
                    for span in line["spans"]:
                        text = span["text"].strip()
                        if not text:
                            continue
                        if text in replacements:
                            assert span["color"] == RED_INT, text
                        if span["color"] == RED_INT:
                            assert any(text in r or r in text for r in replacements), text


def test_I_replacement_sits_on_the_original_baseline_without_boxes(single):
    s, out, _pages = single
    with pymupdf.open(out) as d:
        assert all(not page.get_drawings() for page in d), "no boxes or patches drawn"
        page = d[0]
        spans = [sp for b in page.get_text("dict")["blocks"] for l in b.get("lines", []) for sp in l["spans"]]
    ssn_target = next(t for t in s.plan().targets if t.original == "512-44-0193")
    red = next(sp for sp in spans if sp["text"].strip() == ssn_target.replacement)
    assert abs(red["origin"][1] - ssn_target.origin[1]) < 0.6
    assert abs(red["origin"][0] - ssn_target.rect[0]) < 0.6
    assert abs(red["size"] - 10) < 0.01, "same size as the original value"


# --- J: filenames -------------------------------------------------------------------

def test_J_single_filer_filename(single):
    s, _out, _pages = single
    m = _mapping(s)
    assert s.safe_output_name() == f"{m['Torvin Quell']} 1040-Latest.pdf"


def test_J_joint_filer_filename_keeps_order_and_separators(joint):
    s, _out, _pages = joint
    m = _mapping(s)
    assert s.safe_output_name() == f"{m['Corin Tash']}_{m['Bela Tash']} 1040-Latest.pdf"


def test_J_descriptive_words_and_numbers_survive(tmp_path):
    path = _pdf(tmp_path / "2025_Quell-Final 1040 v2.pdf", [HEADER])
    s, _out, _pages = _run(path)
    name = s.safe_output_name()
    assert name.endswith("-Final 1040 v2.pdf") and name.startswith("2025_")
    assert "Quell" not in name


def test_D_a_dependents_column_line_does_not_make_a_surname_a_given_name():
    """A "(1) First name" column can bind the whole "First M Last" line. Seeding
    every word of it as a given name made the surname a first-name pseudonym
    wherever it appeared alone (seen on a real return's filename). Surname
    labels are seeded first, and only the FIRST word under a given-name label."""
    from types import SimpleNamespace as NS

    from app.detection.types import PiiType
    from app.pseudonymization.names import NameRegistry
    from app.transform.plan import _seed_name_roles

    def cand(value, gid):
        return NS(pii_type=PiiType.PERSON, normalized=value, group_id=gid)

    groups = [NS(group_id="g1", label=NS(text="(1) First name")),
              NS(group_id="g2", label=NS(text="Last name"))]
    # The column line comes FIRST, as it did on the real return.
    det = NS(groups=groups, candidates=[cand("Oska Rue Venn", "g1"), cand("Venn", "g2")])
    reg = NameRegistry(scope="t")
    _seed_name_roles(reg, det)
    expected = NameRegistry(scope="t")
    expected.seed_role("Venn", surname=True)
    given = NameRegistry(scope="t")
    given.seed_role("Venn", surname=False)
    assert expected.known("Venn") != given.known("Venn")   # the test can tell them apart
    assert reg.known("Venn") == expected.known("Venn"), "the surname must get a surname pseudonym"


@pytest.mark.parametrize("value", ["4817 HARBORVIEW CMN", "12 Oak St", "98765 Long Road", "7 Elm Ct"])
def test_F_a_street_keeps_its_house_number_shape(value):
    """A 4-digit house number became a 5-digit one with a leading zero on a real
    return ("4817 ..." -> "08148 ..."). Same digit count, no leading zero."""
    from app.detection.types import PiiType
    from app.pseudonymization.generator import generate

    want = len(value.split()[0])
    for t in (PiiType.STREET, PiiType.ADDRESS):
        for scope in ("a", "b", "c", "d"):
            out = generate(t, value, scope=scope)
            number = out.split()[0]
            assert number.isdigit() and len(number) == want and number[0] != "0", (t, out)


def test_I_a_crowded_row_shrinks_on_the_baseline_instead_of_overlapping():
    """A replacement needing ~75% of its size to fit before the next target used
    to fall to the boxed fallback, which grew upward and drew it over the
    neighbouring replacement (page 73 of a real return). It now stays on the
    baseline, smaller, and ends before the next target."""
    from types import SimpleNamespace as NS

    import pymupdf

    from app.export.redactor import _insert_at_baseline

    doc = pymupdf.open()
    page = doc.new_page()
    width = pymupdf.get_text_length("Arlo Venn Pallas", fontname="helv", fontsize=9)
    first = NS(rect=(100, 100, 100 + width * 0.75, 112), origin=(100, 109), replacement="Arlo Venn Pallas",
               font_size=9, font_name="Helvetica", font_flags=0, color=(1, 0, 0))
    second = NS(rect=(100 + width * 0.75 + 4, 100, 260, 112), origin=(0, 109), replacement="x",
                font_size=9, font_name="Helvetica", font_flags=0, color=(1, 0, 0))
    assert _insert_at_baseline(page, first, [first, second], []) == "shrunk"
    spans = [s for b in page.get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]]
    placed = [s for s in spans if s["text"] == "Arlo Venn Pallas"][0]
    assert placed["bbox"][2] <= second.rect[0], "must end before the next target"
    assert abs(placed["origin"][1] - 109) < 0.5, "must stay on the original baseline"
    assert placed["size"] >= 9 * 0.6 - 0.01

    # With no neighbour limiting it, the 80% floor still applies and a value that
    # cannot fit hands over to the boxed fallback (None), as before.
    lonely = NS(rect=(100, 200, 110, 212), origin=(100, 209), replacement="A much longer synthetic value " * 30,
                font_size=9, font_name="Helvetica", font_flags=0, color=(1, 0, 0))
    assert _insert_at_baseline(page, lonely, [lonely], []) is None


def test_F_one_street_line_typed_two_ways_gets_one_pseudonym_in_its_own_case():
    """A real return typed the same street STREET on its cover letter and ADDRESS on
    Form 1040; keyed by type, it got two different pseudonyms - and the STREET one
    lost the original's capitals."""
    from app.detection.types import PiiType
    from app.entities.registry import EntityRegistry

    reg = EntityRegistry(scope="t")
    a = reg.pseudonym_for(PiiType.STREET, "4817 HARBORVIEW CMN")
    b = reg.pseudonym_for(PiiType.ADDRESS, "4817 HARBORVIEW CMN")
    assert a == b
    assert a == a.upper()
