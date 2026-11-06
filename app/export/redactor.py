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

from ..document.pdflock import PDF_LOCK

from ..document.hidden import sanitize_hidden
from ..transform.plan import TransformationPlan, Target

log = logging.getLogger(__name__)

MIN_FONT_SIZE = 4.0
RECT_PAD = 0.6


@dataclass
class ApplyReport:
    redacted: int = 0
    inserted: int = 0
    hidden_actions: list = None
    shrunk: list[str] = None  # candidate ids whose font had to be reduced
    overflowed: list[str] = None  # candidate ids that could not be fitted

    def __post_init__(self):
        self.shrunk = self.shrunk or []
        self.overflowed = self.overflowed or []
        self.hidden_actions = self.hidden_actions or []


#: Share of a target's character-box height trimmed from top and bottom before
#: REMOVAL. Only the removal rectangle - replacement placement uses the full
#: target rect and is unchanged (see CLAUDE.md on _insert_replacement).
REMOVAL_INSET = 0.2


def clear_of_neighbours(rect: pymupdf.Rect, target, words) -> pymupdf.Rect:
    """Shrink a removal rectangle vertically so it stops short of every word
    that is NOT part of the target (its centre lies outside the target's box).

    Measured on a real return: a value set tightly under an amount line
    ("$4210.35") still intersected the amount's character boxes after the
    inset, and the amount was lost - a financial value must survive. The
    rectangle keeps at least 30% of the target's height, so it always still
    intersects every target glyph (which PyMuPDF then removes whole).
    """
    t = pymupdf.Rect(target)
    r = pymupdf.Rect(rect)
    min_h = 0.3 * t.height
    for w in words:
        wx0, wy0, wx1, wy1 = w[:4]
        cx, cy = (wx0 + wx1) / 2, (wy0 + wy1) / 2
        if t.x0 <= cx <= t.x1 and t.y0 <= cy <= t.y1:
            continue                      # part of the target itself
        if wx1 <= r.x0 or wx0 >= r.x1 or wy1 <= r.y0 or wy0 >= r.y1:
            continue                      # does not touch the removal box
        same_row = min(wy1, t.y1) - max(wy0, t.y0) > 0.5 * min(wy1 - wy0, t.height)
        if same_row:
            # A neighbour on the SAME row whose box touches ours ("Firm's" 2pt
            # into a name's box): stop short of it horizontally. The rectangle
            # still reaches into the target's last/first glyph, which PyMuPDF
            # then removes whole.
            min_w = 0.3 * t.width
            if cx > (t.x0 + t.x1) / 2:
                r.x1 = max(min(r.x1, wx0 - 0.05), r.x0 + min_w)
            else:
                r.x0 = min(max(r.x0, wx1 + 0.05), r.x1 - min_w)
        elif cy < (t.y0 + t.y1) / 2:      # neighbour above
            r.y0 = min(max(r.y0, wy1 + 0.05), r.y1 - min_h)
        else:                             # neighbour below
            r.y1 = max(min(r.y1, wy0 - 0.05), r.y0 + min_h)
    return r


#: Share of each glyph box kept, centred, as its removal rectangle.
GLYPH_CORE = 0.4


def removal_rects(target, words) -> list:
    """The rectangles handed to add_redact_annot for one target.

    Preferred: one small rectangle at the centre of each of the value's own
    glyphs. PyMuPDF removes every character a rectangle intersects, so each
    value glyph is certainly removed, and a neighbouring word is touched only if
    one of its glyphs covers the very centre of a value glyph. Measured on real
    returns: a single rectangle per value - even inset and shrunk away from
    neighbours - either took a touching label word ("Firm's") with it or, when
    shrunk further, left part of the value behind. Falls back to the single
    inset rectangle when glyph boxes are unknown.
    """
    chars = getattr(target, "char_rects", None) or []
    if not chars:
        return [clear_of_neighbours(removal_rect(target.rect), target.rect, words)]
    out = []
    t = pymupdf.Rect(target.rect)
    for x0, y0, x1, y1 in chars:
        w, h = (x1 - x0), (y1 - y0)
        dx, dy = w * (1 - GLYPH_CORE) / 2, h * (1 - GLYPH_CORE) / 2
        r = pymupdf.Rect(x0 + dx, y0 + dy, x1 - dx, y1 - dy)
        # Lines set tighter than their type size overlap vertically: keep the
        # core clear of words above and below (never below 30% of the glyph).
        min_h = 0.3 * h
        for wx0, wy0, wx1, wy1, *_ in words:
            cx, cy = (wx0 + wx1) / 2, (wy0 + wy1) / 2
            if t.x0 <= cx <= t.x1 and t.y0 <= cy <= t.y1:
                continue
            if wx1 <= r.x0 or wx0 >= r.x1 or wy1 <= r.y0 or wy0 >= r.y1:
                continue
            if cy < (y0 + y1) / 2:
                r.y0 = min(max(r.y0, wy1 + 0.05), r.y1 - min_h)
            elif cy > (y0 + y1) / 2:
                r.y1 = max(min(r.y1, wy0 - 0.05), r.y0 + min_h)
        out.append(r)
    return out


def removal_rect(rect) -> pymupdf.Rect:
    """The rectangle handed to add_redact_annot.

    PyMuPDF removes EVERY character whose box intersects the redaction
    rectangle, and a PDF character box runs from the font's ascent to its
    descent - well above and below the visible glyph. On a dense form the value
    line's box overlaps the label line just above it, so redacting the value
    erased the label too. Reproduced: a 10pt value 9pt under a 7pt label - the
    full box removed the whole label "Your social security number"; this inset
    removed every glyph of the value and none of the label. Characters are
    removed whole, so the inset can never leave part of a target glyph behind;
    `test_redaction_box_does_not_erase_the_label_above` and the verifier's
    glyph-residue check pin both sides.
    """
    r = pymupdf.Rect(rect)
    dy = r.height * REMOVAL_INSET
    return pymupdf.Rect(r.x0 + 0.2, r.y0 + dy, r.x1 - 0.2, r.y1 - dy)


def apply_plan(plan: TransformationPlan, doc: Optional[pymupdf.Document] = None) -> tuple[pymupdf.Document, ApplyReport]:
    """Apply the plan to a copy of the source document. Never mutates the original file."""
    with PDF_LOCK:
        own = doc is None
        pdf = doc if doc is not None else pymupdf.open(plan.source_path)
        report = ApplyReport()

        try:
            for page_no in sorted({t.page_no for t in plan.targets}):
                page = pdf[page_no]
                targets = plan.targets_for_page(page_no)

                # 1. Mark and permanently remove the original content.
                words = page.get_text("words")
                for t in targets:
                    for rect in removal_rects(t, words):
                        page.add_redact_annot(rect, fill=None)
                    report.redacted += 1

                # Keep images and line art intact so tables and rules survive.
                page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_NONE,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                    text=pymupdf.PDF_REDACT_TEXT_REMOVE,
                )

                # 2a. Blackout targets get a solid bar. Used where a pseudonym
                # cannot stand in - a signature, a logo, a photograph.
                for t in targets:
                    if t.blackout:
                        page.draw_rect(
                            pymupdf.Rect(t.rect), color=(0, 0, 0), fill=(0, 0, 0),
                            width=0.5, overlay=True,
                        )
                        report.inserted += 1

                # 2b. Insert the red replacement text into the cleared area,
                # on the original baseline where it fits beside what remains.
                remaining = page.get_text("words")
                for t in targets:
                    if t.blackout:
                        continue
                    status = _insert_at_baseline(page, t, targets, remaining)
                    if status is None:
                        status = _insert_replacement(page, t, targets)
                    if status == "shrunk":
                        report.shrunk.append(t.candidate_id)
                    elif status == "overflow":
                        report.overflowed.append(t.candidate_id)
                    if status != "failed":
                        report.inserted += 1
            # Hidden content has no page geometry, so it is rewritten or removed
            # rather than redacted in place.
            report.hidden_actions = sanitize_hidden(pdf, plan.replacement_map)
        except Exception:
            if own:
                pdf.close()
            raise

        return pdf, report


#: A replacement may shrink to this share of the original size on its own
#: baseline before the boxed fallback below is used.
BASELINE_MIN_SCALE = 0.8
#: When a real neighbour (a word or another target) on the same row is what
#: limits the width, shrinking further on the baseline beats handing over to the
#: boxed fallback: that path grows the box and drew one replacement on top of the
#: next on a densely packed row of a real return.
CROWDED_MIN_SCALE = 0.6


def _insert_at_baseline(page: pymupdf.Page, target: Target, page_targets: list, words: list) -> Optional[str]:
    """Write the replacement where the original sat: same x, same baseline,
    same size (down to 80%, or 60% when a neighbour on the row is the limit), in
    red - when it fits before the next word or target on the same row. Returns None to hand over to _insert_replacement.

    Owner's request (0.40.0): replacements should look like a value entered in
    the field - preserved baseline and alignment, no overlap with neighbouring
    labels or values. The boxed v0.8.3 logic below stays as the fallback, with
    its insert_text last resort, exactly as CLAUDE.md requires.
    """
    if not target.origin or not target.replacement:
        return None
    x0, y0, x1, y1 = target.rect
    baseline = target.origin[1]
    if not (y0 - 2 <= baseline <= y1 + 2):
        return None
    edge = page.rect.x1 - 2
    limit = edge
    for w in words:
        wx0, wy0, wx1, wy1 = w[:4]
        if wx0 >= x1 - 0.5 and wy0 < y1 - 1 and wy1 > y0 + 1:
            limit = min(limit, wx0 - 2.0)
    for other in page_targets:
        if other is target:
            continue
        ox0, oy0, ox1, oy1 = other.rect
        if ox0 >= x1 - 0.5 and oy0 < y1 - 1 and oy1 > y0 + 1:
            limit = min(limit, ox0 - 2.0)
    width_available = limit - x0
    fontname = _safe_font(target.font_name, getattr(target, "font_flags", 0))
    size = max(target.font_size, MIN_FONT_SIZE)
    current = size
    floor_scale = CROWDED_MIN_SCALE if limit < edge else BASELINE_MIN_SCALE
    while current >= max(MIN_FONT_SIZE, size * floor_scale) - 1e-6:
        if pymupdf.get_text_length(target.replacement, fontname=fontname, fontsize=current) <= width_available:
            page.insert_text((x0, baseline), target.replacement, fontsize=current,
                             fontname=fontname, color=target.color)
            return "ok" if current >= size - 0.01 else "shrunk"
        current -= 0.25
    return None


def _insert_replacement(page: pymupdf.Page, target: Target, page_targets: list) -> str:
    """Draw red replacement text, shrinking to fit rather than clipping.

    LITERALLY the v0.8.3 logic, restored from an actual archived copy of that
    version rather than reconstructed from memory - symmetric padding on both
    sides, no neighbour/headroom awareness at all. `page_targets` is accepted
    but UNUSED; kept only so the call site elsewhere in this file needs no
    change.

    Known, accepted trade-off: this has no protection against two adjacent
    targets' boxes overlapping on a very tightly-spaced document (rows a
    fraction of a point apart) - that protection was built later in this
    project and is what this revert removes. It is being kept out anyway,
    on direct instruction, because the neighbour-aware version was reported
    as rendering worse on real documents than this simpler one repeatedly
    and consistently. Do not silently re-add headroom capping here.
    """
    x0, y0, x1, y1 = target.rect
    size = max(target.font_size, MIN_FONT_SIZE)
    fontname = _safe_font(target.font_name, getattr(target, "font_flags", 0))

    box = pymupdf.Rect(x0 - RECT_PAD, y0 - RECT_PAD, x1 + RECT_PAD, y1 + RECT_PAD)
    grown = pymupdf.Rect(box.x0, box.y0, min(page.rect.x1 - 2, box.x1 + 0.65 * box.width), box.y1)

    attempts: list[tuple[pymupdf.Rect, float]] = [
        (box, size),
        (grown, size),
        (grown, None),
        (box, None),
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
    except Exception as exc:  # noqa: BLE001
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


def _safe_font(font_name: str, flags: int = 0) -> str:
    """Map an embedded font name onto a base-14 font we can always draw.

    Bold/italic is read from PyMuPDF's FONT FLAGS bitmask first (bit 4 = bold,
    bit 1 = italic) - reliable regardless of what the font is actually named,
    which a substring check on an obfuscated embedded name (e.g.
    "Arial-CustomID7") cannot see at all. The name-substring check still runs
    as a fallback for when flags were not captured (0).
    """
    name = (font_name or "").lower()
    bold = bool(flags & (1 << 4)) or "bold" in name or "black" in name or "heavy" in name
    italic = bool(flags & (1 << 1)) or "italic" in name or "oblique" in name
    if "courier" in name or "mono" in name:
        family = {(0, 0): "cour", (1, 0): "cobo", (0, 1): "coit", (1, 1): "cobi"}
    elif "times" in name or ("serif" in name and "sans" not in name):
        family = {(0, 0): "tiro", (1, 0): "tibo", (0, 1): "tiit", (1, 1): "tibi"}
    else:
        family = {(0, 0): "helv", (1, 0): "hebo", (0, 1): "heit", (1, 1): "hebi"}
    return family[(int(bold), int(italic))]


def export(plan: TransformationPlan, output_path: str) -> ApplyReport:
    """Write the transformed document to a NEW file (spec section 44)."""
    with PDF_LOCK:
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
    with PDF_LOCK:
        pdf, _ = apply_plan(plan)
        try:
            pix = pdf[page_no].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            return pix.tobytes("png")
        finally:
            pdf.close()


def render_pages(plan: TransformationPlan, pages: list[int], zoom: float = 1.0) -> dict[int, bytes]:
    """Render several transformed pages from a single application of the plan."""
    with PDF_LOCK:
        pdf, _ = apply_plan(plan)
        try:
            out: dict[int, bytes] = {}
            for page_no in pages:
                if 0 <= page_no < pdf.page_count:
                    pix = pdf[page_no].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
                    out[page_no] = pix.tobytes("png")
            return out
        finally:
            pdf.close()


def render_originals(path: str, pages: list[int], zoom: float = 1.0) -> dict[int, bytes]:
    with PDF_LOCK:
        pdf = pymupdf.open(path)
        try:
            return {
                page_no: pdf[page_no].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
                for page_no in pages
                if 0 <= page_no < pdf.page_count
            }
        finally:
            pdf.close()


def render_original(path: str, page_no: int, zoom: float = 1.5) -> bytes:
    with PDF_LOCK:
        pdf = pymupdf.open(path)
        try:
            pix = pdf[page_no].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            return pix.tobytes("png")
        finally:
            pdf.close()

