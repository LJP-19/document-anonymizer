"""0.44.0: rotated pages and "apply to all occurrences". Every value is fictional.

Real report: a landscape Form 4562 printed sideways on a portrait page had its
labels ignored (every line ran vertically, so nothing was "left of" or "above"
anything) and its replacements written horizontally into the narrow vertical box,
shrunk to an unreadable size. And values confirmed by a label on one page were
missed or held on pages where they appear without a label.
"""
from __future__ import annotations

import pymupdf
import pytest

from app.document.frame import PageFrame, dominant_frame
from app.session import AnonymizationSession


@pytest.fixture(autouse=True)
def _rules_only(monkeypatch):
    monkeypatch.setenv("DOCANON_LLM", "0")
    monkeypatch.setenv("DOCANON_SECOND_PASS", "0")


def _run(path, tmp_path):
    s = AnonymizationSession(source_path=str(path))
    s.analyse()
    s.decisions.approve_auto_eligible(s.candidates)
    out = tmp_path / "out.pdf"
    _apply, report = s.process(str(out))
    return s, report, out


# -- the frame itself -----------------------------------------------------------


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_frame_points_round_trip(rotation):
    f = PageFrame(rotation=rotation, width=612, height=792)
    for x, y in [(0, 0), (100, 50), (611, 791), (300.5, 12.25)]:
        fx, fy = f.to_frame_point(x, y)
        assert 0 <= fx <= f.frame_size[0] and 0 <= fy <= f.frame_size[1]
        bx, by = f.to_pdf_point(fx, fy)
        assert (round(bx, 6), round(by, 6)) == (x, y)


def test_a_mostly_horizontal_page_keeps_its_own_frame():
    raw = {"blocks": [{"type": 0, "lines": [
        {"dir": (1, 0), "spans": [{"chars": [{}] * 80}]},
        {"dir": (0, -1), "spans": [{"chars": [{}] * 10}]},
    ]}]}
    assert dominant_frame(raw, 612, 792) is None


# -- a sideways form --------------------------------------------------------------


def _sideways_form(path):
    """A portrait page with its text rotated 90 degrees: read upright, a caption
    with its value on the next row, and a caption with its value to the right."""
    d = pymupdf.open()
    p = d.new_page(width=612, height=792)
    # rotate=90 writes text running UP the page; in the upright frame x' = 792 - y.
    def put(x_up, y_up, text, font="helv", size=9):
        p.insert_text((y_up, 792 - x_up), text, fontname=font, fontsize=size, rotate=90)
    put(60, 60, "Form 4562 Depreciation and Amortization", "hebo", 11)
    put(60, 100, "Name(s) shown on return", "hebo", 8)
    put(60, 114, "Torvin Quell", "cour", 10)
    put(330, 100, "Identifying number", "hebo", 8)
    put(330, 114, "512-48-7391", "cour", 10)
    put(60, 150, "Business or activity to which this form relates", "hebo", 8)
    for i in range(6):
        put(60, 200 + i * 16, f"Line {i + 1} Maximum amount   1,050,000", "helv", 8)
    d.save(str(path))
    d.close()


def test_a_sideways_page_is_read_upright_and_its_values_replaced_in_place(tmp_path):
    src = tmp_path / "sideways.pdf"
    _sideways_form(src)
    s, report, out = _run(src, tmp_path)
    page = s.document.pages[0]
    assert page.frame is not None and page.frame.rotation == 90

    applied = {c.text for c in s.candidates if s.decisions.get(c.id).state.name == "ACCEPTED"}
    assert "Torvin Quell" in applied and "512-48-7391" in applied, applied
    assert report.passed, [c.name for c in report.checks if not c.passed]

    with pymupdf.open(str(out)) as d:
        text = d[0].get_text()
        assert "Torvin" not in text and "512-48-7391" not in text
        assert "Name(s) shown on return" in text and "1,050,000" in text
        red = [sp for b in d[0].get_text("dict")["blocks"] for ln in b.get("lines", [])
               for sp in ln["spans"] if sp["color"] == 0xFF0000 and sp["text"].strip()
               for _ in [ln]]
        lines = [ln for b in d[0].get_text("dict")["blocks"] for ln in b.get("lines", [])
                 if any(sp["color"] == 0xFF0000 and sp["text"].strip() for sp in ln["spans"])]
    assert red and lines
    # Written in the page's own direction, at a readable size - not shrunk sideways.
    assert all(tuple(round(v) for v in ln["dir"]) == (0, -1) for ln in lines)
    assert min(sp["size"] for sp in red) >= 6


# -- apply to all occurrences ----------------------------------------------------


def _two_page_return(path):
    d = pymupdf.open()
    p1 = d.new_page(width=612, height=792)
    p1.insert_text((72, 80), "Your first name and middle initial", fontname="hebo", fontsize=7)
    p1.insert_text((72, 92), "Torvin", fontname="cour", fontsize=10)
    p1.insert_text((260, 80), "Last name", fontname="hebo", fontsize=7)
    p1.insert_text((260, 92), "Quell", fontname="cour", fontsize=10)
    p1.insert_text((420, 80), "Your social security number", fontname="hebo", fontsize=7)
    p1.insert_text((420, 92), "512-48-7391", fontname="cour", fontsize=10)
    p1.insert_text((72, 130), "Employer's name", fontname="hebo", fontsize=7)
    p1.insert_text((72, 142), "Brindlewood Fabrication", fontname="cour", fontsize=10)
    p2 = d.new_page(width=612, height=792)
    # A continuation sheet: the same values printed with NO label at all.
    p2.insert_text((72, 60), "Torvin Quell 512-48-7391", fontname="cour", fontsize=10)
    p2.insert_text((72, 100), "Statement 1 - Brindlewood Fabrication", fontname="cour", fontsize=10)
    p2.insert_text((72, 140), "Total wages   85,000", fontname="cour", fontsize=10)
    d.save(str(path))
    d.close()


def test_a_value_confirmed_on_one_page_is_replaced_on_every_page_with_one_pseudonym(tmp_path):
    src = tmp_path / "return.pdf"
    _two_page_return(src)
    s, report, out = _run(src, tmp_path)
    assert report.passed, [c.name for c in report.checks if not c.passed]
    with pymupdf.open(str(out)) as d:
        p1, p2 = d[0].get_text(), d[1].get_text()
    for value in ("Torvin", "Quell", "512-48-7391", "Brindlewood"):
        assert value not in p1 and value not in p2, value
    assert "85,000" in p2 and "Statement 1" in p2
    # One pseudonym per value: the SSN's replacement on page 1 is the one on page 2.
    plan = s.plan()
    ssn = {t.replacement for t in plan.targets if "512" in (t.original or "") and "48" in t.original}
    assert len(ssn) == 1, ssn
