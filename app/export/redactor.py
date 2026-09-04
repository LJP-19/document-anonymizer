"""Permanent redaction and red replacement text (spec sections 34-36).

Original content is removed with PyMuPDF's redaction machinery - not covered
with a rectangle, not annotated, not painted over. The replacement is real text
drawn into the page content stream in red.

`apply_plan` is the only transformation code path in the application. The live
preview renders the document this function produces, so preview and export can
never diverge (spec sections 32-33).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pymupdf

from ..transform.plan import TransformationPlan, Target

log = logging.getLogger(__name__)

MIN_FONT_SIZE = 4.0
RECT_PAD = 0.6


@dataclass
class ApplyReport:
    redacted: int = 0
    inserted: int = 0
    shrunk: list[str] = None  # candidate ids whose font had to be reduced
    overflowed: list[str] = None  # candidate ids that could not be fitted

    def __post_init__(self):
        self.shrunk = self.shrunk or []
        self.overflowed = self.overflowed or []


def apply_plan(plan: TransformationPlan, doc: Optional[pymupdf.Document] = None) -> tuple[pymupdf.Document, ApplyReport]:
    """Apply the plan to a copy of the source document. Never mutates the original file."""
    own = doc is None
    pdf = doc if doc is not None else pymupdf.open(plan.source_path)
    report = ApplyReport()

    try:
        for page_no in sorted({t.page_no for t in plan.targets}):
            page = pdf[page_no]
            targets = plan.targets_for_page(page_no)

            # 1. Mark and permanently remove the original content.
            for t in targets:
                rect = pymupdf.Rect(t.rect)
                page.add_redact_annot(rect, fill=None)
                report.redacted += 1

            # Keep images and line art intact so tables and rules survive.
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )

            # 2. Insert the red replacement text into the cleared area.
            for t in targets:
                status = _insert_replacement(page, t)
                if status == "shrunk":
                    report.shrunk.append(t.candidate_id)
                elif status == "overflow":
                    report.overflowed.append(t.candidate_id)
                if status != "failed":
                    report.inserted += 1
    except Exception:
        if own:
            pdf.close()
        raise

    return pdf, report


def _insert_replacement(page: pymupdf.Page, target: Target) -> str:
    """Draw red replacement text, shrinking to fit rather than clipping."""
    x0, y0, x1, y1 = target.rect
    size = max(target.font_size, MIN_FONT_SIZE)
    fontname = _safe_font(target.font_name)

    # Allow modest horizontal growth into whitespace to the right, but never
    # vertical growth - that is what corrupts neighbouring rows.
    box = pymupdf.Rect(x0 - RECT_PAD, y0 - RECT_PAD, x1 + RECT_PAD, y1 + RECT_PAD)
    grown = pymupdf.Rect(box.x0, box.y0, min(page.rect.x1 - 2, box.x1 + 0.65 * box.width), box.y1)

    # Fit on ONE line if at all possible: wrapping a replacement into a second
    # line is what corrupts tables and pushes text over neighbouring rows.
    # Order matters. Growing into empty space to the right keeps the original
    # font size; shrinking is the last resort, because mismatched sizes down a
    # column look like a rendering fault even when the content is correct.
    attempts: list[tuple[pymupdf.Rect, float]] = [
        (box, size),      # original size, original box
        (grown, size),    # original size, grown into adjacent whitespace
        (grown, None),    # shrink, grown box
        (box, None),      # shrink, original box
    ]
    for attempt_box, forced in attempts:
        if forced is not None:
            width = pymupdf.get_text_length(target.replacement, fontname=fontname, fontsize=forced)
            current = forced if width <= attempt_box.width - 1.0 else None
        else:
            current = _fit_font_size(target.replacement, fontname, size, attempt_box.width - 1.0)
        if current is None:
            continue
        draw_box = pymupdf.Rect(
            attempt_box.x0,
            attempt_box.y0 - 0.35 * current,
            attempt_box.x1,
            attempt_box.y1 + 0.35 * current,
        )
        rc = page.insert_textbox(
            draw_box,
            target.replacement,
            fontsize=current,
            fontname=fontname,
            color=target.color,
            align=pymupdf.TEXT_ALIGN_LEFT,
        )
        if rc >= 0:
            return "shrunk" if current < size - 0.01 else "ok"

    # Last resort: place at the original baseline so nothing is silently lost.
    try:
        page.insert_text(
            (x0, y1 - 1),
            target.replacement,
            fontsize=MIN_FONT_SIZE,
            fontname=fontname,
            color=target.color,
        )
        return "overflow"
    except Exception as exc:  # pragma: no cover
        log.warning("replacement insertion failed for %s: %s", target.candidate_id, type(exc).__name__)
        return "failed"


def _fit_font_size(text: str, fontname: str, start_size: float, width: float) -> Optional[float]:
    """Largest size <= start_size at which `text` fits on one line, else None."""
    size = start_size
    while size >= MIN_FONT_SIZE:
        if pymupdf.get_text_length(text, fontname=fontname, fontsize=size) <= width:
            return size
        size -= 0.25
    return None


def _safe_font(font_name: str) -> str:
    """Map an embedded font name onto a base-14 font we can always draw."""
    name = (font_name or "").lower()
    bold = "bold" in name or "black" in name or "heavy" in name
    italic = "italic" in name or "oblique" in name
    if "courier" in name or "mono" in name:
        family = {(0, 0): "cour", (1, 0): "cobo", (0, 1): "coit", (1, 1): "cobi"}
    elif "times" in name or ("serif" in name and "sans" not in name):
        family = {(0, 0): "tiro", (1, 0): "tibo", (0, 1): "tiit", (1, 1): "tibi"}
    else:
        family = {(0, 0): "helv", (1, 0): "hebo", (0, 1): "heit", (1, 1): "hebi"}
    return family[(int(bold), int(italic))]


def export(plan: TransformationPlan, output_path: str) -> ApplyReport:
    """Write the transformed document to a NEW file (spec section 44)."""
    if str(output_path) == str(plan.source_path):
        raise ValueError("refusing to overwrite the original document (spec section 44)")
    pdf, report = apply_plan(plan)
    try:
        pdf.save(output_path, garbage=4, deflate=True, clean=True)
    finally:
        pdf.close()
    return report


def render_page(plan: TransformationPlan, page_no: int, zoom: float = 1.5) -> bytes:
    """Render the transformed page for the live preview - same plan, same code."""
    pdf, _ = apply_plan(plan)
    try:
        pix = pdf[page_no].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        return pix.tobytes("png")
    finally:
        pdf.close()


def render_original(path: str, page_no: int, zoom: float = 1.5) -> bytes:
    pdf = pymupdf.open(path)
    try:
        pix = pdf[page_no].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        return pix.tobytes("png")
    finally:
        pdf.close()
