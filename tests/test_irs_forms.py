"""The official IRS 2025 Form 1040 and 2026 Form W-2, filled with an INVENTED return
(demo/f1040, demo/w2; rebuild with the make_*.py scripts there) and run through the
real pipeline. Every value below is fictional.

Each assertion is a bug found by running these forms (0.42.3 / 0.42.4):
boxed SSN digits read one per line, dependents 2-4 never bound, a centred ZIP,
the PTIN typed as a medical record number, W-2 wage boxes typed SSN / Medicare ID
(their amounts were REPLACED), a W-2 caption printed under its box, a vertical
watermark and the template's own text bound as values.
"""
from __future__ import annotations

import re
from pathlib import Path

import pymupdf
import pytest

from app.document.provider import comb_values_text
from app.session import AnonymizationSession

ROOT = Path(__file__).resolve().parents[1]
F1040 = ROOT / "demo" / "f1040" / "Feldman_Marcus_2025_Form1040.pdf"
W2 = ROOT / "demo" / "w2" / "Feldman_Marcus_W2.pdf"


def _process(src: Path, tmp_path: Path):
    s = AnonymizationSession(source_path=str(src))
    s.analyse()
    s.decisions.approve_auto_eligible(s.candidates)
    out = tmp_path / "out.pdf"
    _apply, report = s.process(str(out))
    with pymupdf.open(str(out)) as d:
        text = "\n".join(p.get_text() for p in d) + "\n" + comb_values_text(d)
    return s, report, " ".join(text.split())


@pytest.fixture(scope="module")
def f1040(tmp_path_factory, monkeypatch_module):
    return _process(F1040, tmp_path_factory.mktemp("f1040"))


@pytest.fixture(scope="module")
def w2(tmp_path_factory, monkeypatch_module):
    return _process(W2, tmp_path_factory.mktemp("w2"))


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    mp.setenv("DOCANON_LLM", "0")
    yield mp
    mp.undo()


IDENTITY_1040 = [
    "512487391", "528610943", "604183372", "611250498",          # all four SSNs (boxed)
    "121000358", "4471982036",                                   # routing / account (boxed)
    "Marcus T", "Elena R", "Feldman", "Sofia", "Lucas",          # filers + dependents 1 AND 2
    "4817 Larkspur Ridge Dr", "Fremont", "94539",                # address incl. centred ZIP
    "(510) 555-0142", "mfeldman.home@example.com",
    "Dana K. Whitfield", "P01234567", "84-3127755",              # preparer, PTIN, firm EIN
    "2200 Mission College Blvd", "Santa Clara, CA 95054",
]


def test_form_1040_every_identity_value_is_replaced(f1040):
    _s, report, text = f1040
    assert report.passed, [c for c in report.checks if not c.passed]
    left = [v for v in IDENTITY_1040 if v in text]
    assert not left, f"identity left readable on the 1040: {left}"


def test_form_1040_keeps_amounts_labels_and_state(f1040):
    _s, _report, text = f1040
    for keep in ["612,450", "811,482", "148,920", "Your social security number",
                 "Dependent 2", "Routing number", "Software Architect", "Physician"]:
        assert keep in text, keep


def test_form_1040_firm_name_is_replaced_and_held_items_stay_readable(f1040):
    """Owner, 0.42.5: firm and employer names are identity and are replaced. Anything
    still held (a model's guess, which can differ between model builds) is left
    readable - never applied - and never an amount."""
    s, _report, text = f1040
    assert "Whitfield Tax Advisors LLC" not in text
    from app.decisions.authorization import is_amount_shaped

    for c in s.decisions.awaiting_review(s.candidates):
        assert not s.decisions.is_actionable(c)
        assert not is_amount_shaped(c.text)


IDENTITY_W2 = ["512-48-7391", "84-3127755", "Marcus T", "Feldman", "1450 Charleston Rd", "Northwind Analytics Inc",
               "Mountain View, CA 94043", "4817 Larkspur Ridge Dr", "Fremont, CA 94539", "113-4482-9",
               "A7731-0042"]   # box d control number, centred in its box (0.42.6)


def test_w2_every_identity_value_is_replaced(w2):
    _s, report, text = w2
    assert report.passed, [c for c in report.checks if not c.passed]
    left = [v for v in IDENTITY_W2 if v in text]
    assert not left, f"identity left readable on the W-2: {left}"


def test_w2_wage_and_tax_amounts_are_never_touched(w2):
    """Boxes 1-6 and 16-17: 'Social security wages' / 'Medicare wages and tips' were
    typed SSN / Medicare ID and their amounts replaced."""
    _s, _report, text = w2
    for amount in ["612450.00", "148920.00", "176100.00", "10918.20", "12592.53", "58210.44"]:
        assert amount in text, amount


def test_w2_form_text_is_never_a_value(w2):
    _s, _report, text = w2
    for keep in ["For Informational Purposes", "For Official Use Only", "15 State",
                 "Employee's social security number"]:
        assert keep.replace("'", "’") in text or keep in text, keep


# --- a jointly filed 1040 + the taxpayer's W-2 in ONE document (demo/joint_tw) -----
JOINT = ROOT / "demo" / "joint_tw" / "Chen_Lin_2025_1040_W2.pdf"


@pytest.fixture(scope="module")
def joint(tmp_path_factory, monkeypatch_module):
    return _process(JOINT, tmp_path_factory.mktemp("joint"))


def test_joint_return_every_identity_value_is_replaced(joint):
    s, report, text = joint
    assert report.passed, [c for c in report.checks if not c.passed]
    left = [v for v in [
        "Wei-Ting", "Yu-Hsuan", "Pin-Yu", "Chia-Hao", "Chen", "Lin", "Shu-Fen Hsu",   # incl. 3-letter surnames
        "547283916", "547-28-3916", "562108437", "618305742", "627419058",
        "2318 Rosewood Ter", "Cupertino", "95014", "San Jose", "88 Innovation Way", "Bascom",
        "Formosa Tax Partners LLC", "Pacific Rim Semiconductor Inc",                  # firm + employer
        "P02468135", "77-4051836", "94-2716635", "348-1176-2",
        "121042882", "7730158294", "(408) 555-0173", "weiting.chen@example.com",
        "PRS-20419",                                                                  # W-2 box d
    ] if re.search(rf"(?<![\w-]){re.escape(v)}(?![\w-])", text)]   # whole words: "Lin" is in "Line"
    assert not left, left
    assert not s.decisions.awaiting_review(s.candidates)


def test_joint_return_every_amount_and_label_is_untouched(joint):
    import collections

    from app.detection.deterministic import financial_tokens

    with pymupdf.open(str(JOINT)) as d:
        src = "\n".join(p.get_text() for p in d)
    _s, _report, text = joint
    assert collections.Counter(financial_tokens(src)) == collections.Counter(financial_tokens(text))
    for keep in ["Your first name and middle initial", "Spouse's social security number".replace("'", "’"),
                 "Dependent 2", "Social security wages", "Medicare wages and tips", "Firm’s name",
                 "Semiconductor Engineer", "Pharmacist", "CA"]:
        assert keep in text, keep


def test_joint_return_one_pseudonym_per_value_across_all_pages(joint):
    """Owner: the same person, SSN, address part, city, ZIP... gets the SAME pseudonym
    on every page (the SSN is boxed on the 1040 and hyphenated on the W-2; the city is
    alone on the 1040 and in a city/state/ZIP line on the W-2)."""
    import re

    s, _report, _text = joint
    plan = s.plan()
    seen: dict = {}
    for t in plan.targets:
        if t.pii_type.name in ("SSN", "EIN", "BANK_ACCOUNT", "ROUTING_NUMBER"):
            key, rep = re.sub(r"\D", "", t.original), re.sub(r"\D", "", t.replacement)
        else:
            key, rep = t.original.lower(), t.replacement.lower()
        assert seen.setdefault((t.pii_type.name, key), rep) == rep, (t.original, t.replacement, seen[(t.pii_type.name, key)])
    by = {t.original: t.replacement for t in plan.targets}
    city = by["Cupertino"]
    assert by["Cupertino, CA 95014"].startswith(city + ", CA ")          # state kept, city consistent
    assert by["Cupertino, CA 95014"].endswith(by["95014"])               # ZIP consistent
    assert re.sub(r"\D", "", by["547-28-3916"]) == re.sub(r"\D", "", by["547283916"])
