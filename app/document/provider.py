"""Text providers (spec section 6).

The rest of the application talks to `DocumentTextProvider`, never to PyMuPDF
directly, so that an OCR provider can be added in V2 without touching detection,
transformation, or verification.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import pymupdf

from .model import Block, Char, Document, Line, Page, Rect, Span, normalize_char, union
from .pdflock import PDF_LOCK

# A page with less than this many extracted characters that also carries a large
# image is almost certainly a scan.
MIN_CHARS_FOR_NATIVE_TEXT = 24
IMAGE_COVERAGE_THRESHOLD = 0.45


class DocumentTextProvider(ABC):
    @abstractmethod
    def load(self, path: str) -> Document: ...

    @abstractmethod
    def supports(self, path: str) -> bool: ...


class NativePdfTextProvider(DocumentTextProvider):
    """Extracts selectable text with full geometry from a native PDF."""

    def supports(self, path: str) -> bool:
        return str(path).lower().endswith(".pdf")

    def load(self, path: str) -> Document:
        with PDF_LOCK:
            doc = pymupdf.open(path)
        try:
            pages = [self._build_page(doc, i) for i in range(doc.page_count)]
            producer = (doc.metadata or {}).get("producer", "") or ""
            return Document(path=str(path), pages=pages, producer=producer)
        finally:
            doc.close()

    # -- internals ---------------------------------------------------------

    def _build_page(self, doc: pymupdf.Document, index: int) -> Page:
        page = doc[index]
        raw = page.get_text("rawdict")
        blocks: list[Block] = []
        char_count = 0

        for b_no, raw_block in enumerate(raw.get("blocks", [])):
            if raw_block.get("type", 0) != 0:  # image block
                continue
            lines: list[Line] = []
            for l_no, raw_line in enumerate(raw_block.get("lines", [])):
                line = self._build_line(index, b_no, l_no, raw_line)
                if line and line.text.strip():
                    lines.append(line)
                    char_count += len(line.text.strip())
            if lines:
                blocks.append(
                    Block(
                        page_no=index,
                        block_no=b_no,
                        lines=lines,
                        bbox=union(ln.bbox for ln in lines) or (0, 0, 0, 0),
                    )
                )

        blocks = _merge_comb_runs(blocks)
        needs_ocr, reason = self._assess_ocr(page, char_count)
        built = Page(
            number=index,
            width=page.rect.width,
            height=page.rect.height,
            blocks=blocks,
            needs_ocr=needs_ocr,
            ocr_reason=reason,
        )
        if not needs_ocr:
            unreadable, why = self._assess_extraction(built)
            if unreadable:
                built.needs_ocr = True
                built.ocr_reason = f"unreliable text extraction: {why}"
        return built

    def _build_line(self, page_no: int, b_no: int, l_no: int, raw_line: dict) -> Optional[Line]:
        spans: list[Span] = []
        text_parts: list[str] = []
        offsets: list[Optional[Char]] = []
        prev_x1: Optional[float] = None

        for raw_span in raw_line.get("spans", []):
            chars = [
                Char(text=normalize_char(c["c"]), bbox=tuple(c["bbox"]))
                for c in raw_span.get("chars", [])
            ]
            if not chars:
                continue
            size = float(raw_span.get("size", 10.0))
            # Insert a synthetic separator when spans are visually apart but the
            # extractor did not emit whitespace.
            if prev_x1 is not None:
                gap = chars[0].bbox[0] - prev_x1
                if gap > 0.22 * size and not (text_parts and text_parts[-1].endswith(" ")):
                    text_parts.append(" ")
                    offsets.append(None)
            span_text = "".join(c.text for c in chars)
            spans.append(
                Span(
                    text=span_text,
                    bbox=tuple(raw_span.get("bbox", chars[0].bbox)),
                    font=raw_span.get("font", "helv"),
                    size=size,
                    color=int(raw_span.get("color", 0)),
                    chars=chars,
                    flags=int(raw_span.get("flags", 0)),
                    origin=tuple(raw_span["origin"]) if "origin" in raw_span else None,
                )
            )
            text_parts.append(span_text)
            offsets.extend(chars)
            prev_x1 = chars[-1].bbox[2]

        if not spans:
            return None
        text = "".join(text_parts)
        assert len(text) == len(offsets), "offset map desynchronised from line text"
        spans, text, offsets = _close_comb_spacing(spans, text, offsets)
        return Line(
            page_no=page_no,
            block_no=b_no,
            line_no=l_no,
            spans=spans,
            text=text,
            offsets=offsets,
            bbox=tuple(raw_line.get("bbox")) if raw_line.get("bbox") else union(s.bbox for s in spans),
        )

    def _assess_extraction(self, page_obj) -> tuple[bool, str]:
        """Is the extracted text actually readable, or is the encoding broken?

        A subset or CID font with a missing ToUnicode map extracts as control
        characters or replacement marks. Nothing errors - the detectors simply
        find nothing, which is indistinguishable from a clean page with no PII.
        """
        text = " ".join(line.text for line in page_obj.lines)
        letters = [ch for ch in text if ch.isprintable() and not ch.isspace()]
        if len(letters) < 40:
            return False, ""

        suspect = sum(1 for ch in letters if ch in "\ufffd\u0000" or ord(ch) < 32)
        alphabetic = sum(1 for ch in letters if ch.isalpha())
        if suspect / len(letters) > 0.05:
            return True, "the text layer contains replacement or control characters"
        if alphabetic / len(letters) < 0.35:
            return True, "almost no alphabetic characters were extracted"
        return False, ""

    def _assess_ocr(self, page: pymupdf.Page, char_count: int) -> tuple[bool, str]:
        if char_count >= MIN_CHARS_FOR_NATIVE_TEXT:
            return False, ""
        page_area = max(page.rect.get_area(), 1.0)
        image_area = 0.0
        for img in page.get_images(full=True):
            for rect in page.get_image_rects(img[0]):
                image_area += rect.get_area()
        if image_area / page_area >= IMAGE_COVERAGE_THRESHOLD:
            return True, "image-only page: no selectable text under a full-page image"
        if char_count == 0:
            return True, "no extractable text on page"
        return True, f"only {char_count} extractable characters on page"


class FutureOcrTextProvider(DocumentTextProvider):
    """Placeholder for V2. Declared so the seam exists; never silently used."""

    def supports(self, path: str) -> bool:  # pragma: no cover - V2
        return False

    def load(self, path: str) -> Document:  # pragma: no cover - V2
        raise NotImplementedError("OCR is out of scope for V1 (spec section 6)")


def default_provider() -> DocumentTextProvider:
    return NativePdfTextProvider()


# --- comb fields ------------------------------------------------------------
#: Shortest run of boxed single characters treated as one value (an IP PIN has 6,
#: an SSN 9, a routing number 9, an account number up to 17).
COMB_MIN_RUN = 4


def _comb_char(line: Line) -> bool:
    # Digits only: boxed letters ("S I N G L E" in a heading) must never be glued.
    return len(line.spans) == 1 and len(line.text) == 1 and line.text.isdigit()


def _even_pitch_runs(xs: list[float], size: float) -> list[tuple[int, int]]:
    """(start, end) index ranges of >= COMB_MIN_RUN positions at an even pitch."""
    runs, start = [], 0
    for i in range(1, len(xs) + 1):
        ok = i < len(xs)
        if ok:
            pitch = xs[i] - xs[i - 1]
            ok = 0.6 * size <= pitch <= 2.2 * size
            if ok and i - start >= 2:
                first = xs[start + 1] - xs[start]
                ok = abs(pitch - first) <= 0.08 * first
        if not ok:
            if i - start >= COMB_MIN_RUN:
                runs.append((start, i))
            start = i
    return runs


def _close_comb_spacing(spans, text, offsets):
    """Within ONE line, drop the spaces between boxed digits ("5 1 2 4 8 7 3 9 1").

    Some PDFs put a comb field on one line with a space glyph (or our synthetic
    gap separator) between the boxes. The digits keep their own characters and
    rectangles; only whitespace between digits of an evenly spaced run is
    removed, together with its offset entry, so text and offsets stay 1:1.
    """
    digit_spans = [i for i, sp in enumerate(spans) if len(sp.text) == 1 and sp.text.isdigit()]
    if len(digit_spans) < COMB_MIN_RUN:
        return spans, text, offsets
    # consecutive digit spans separated only by whitespace spans
    groups, cur = [], [digit_spans[0]]
    for a, b in zip(digit_spans, digit_spans[1:]):
        if all(not spans[k].text.strip() for k in range(a + 1, b)):
            cur.append(b)
        else:
            groups.append(cur)
            cur = [b]
    groups.append(cur)
    drop_spans: set[int] = set()
    drop_chars: set[int] = set()
    for g in groups:
        size = spans[g[0]].size
        xs = [spans[i].chars[0].bbox[0] for i in g]
        for s0, s1 in _even_pitch_runs(xs, size):
            run = g[s0:s1]
            first_char = spans[run[0]].chars[0]
            last_char = spans[run[-1]].chars[0]
            lo = next(i for i, c in enumerate(offsets) if c is first_char)
            hi = next(i for i, c in enumerate(offsets) if c is last_char)
            drop_chars.update(i for i in range(lo, hi) if text[i].isspace())
            drop_spans.update(k for k in range(run[0] + 1, run[-1]) if not spans[k].text.strip())
    if not drop_chars:
        return spans, text, offsets
    keep = [i for i in range(len(text)) if i not in drop_chars]
    return ([sp for k, sp in enumerate(spans) if k not in drop_spans],
            "".join(text[i] for i in keep), [offsets[i] for i in keep])


def _merge_comb_runs(blocks: list[Block]) -> list[Block]:
    """Rejoin a comb field - one character per printed box - into ONE line.

    A filled-and-flattened IRS fillable PDF (Form 1040 SSN, routing and account
    boxes) puts every digit in its own box; PyMuPDF then reports each digit as a
    separate one-character LINE in its own block. Nothing downstream could see
    "512487391": the SSN rule matched nothing, a model typed two stray digits, and
    the other seven digits of every SSN survived into the output (found on the real
    2025 Form 1040). A run is: >= COMB_MIN_RUN single alphanumeric characters on
    one baseline, same font and size, at an even pitch (within 8%) of 0.6-2.2x the font size: two SSN fields side
    by side (Form 1040 dependents) sit 15% further apart at the column break.
    The merged text has no separators, so it stays strictly 1:1 with the glyphs and
    every character keeps its own rectangle.
    """
    singles = [ln for b in blocks for ln in b.lines if _comb_char(ln)]
    if len(singles) < COMB_MIN_RUN:
        return blocks
    rows: dict[tuple, list[Line]] = {}
    for ln in singles:
        key = (round(ln.bbox[3]), ln.spans[0].font, round(ln.spans[0].size, 1))
        rows.setdefault(key, []).append(ln)

    merged: dict[int, Line] = {}   # id(first line) -> merged line
    absorbed: set[int] = set()
    for (_y, _font, size), row in rows.items():
        row.sort(key=lambda ln: ln.bbox[0])
        run = [row[0]]
        runs: list[list[Line]] = []
        for prev, cur in zip(row, row[1:]):
            pitch = cur.bbox[0] - prev.bbox[0]
            ok = 0.6 * size <= pitch <= 2.2 * size
            if ok and len(run) >= 2:
                first = run[1].bbox[0] - run[0].bbox[0]
                ok = abs(pitch - first) <= 0.08 * first  # boxes are evenly spaced; the next field is not
            if ok:
                run.append(cur)
            else:
                runs.append(run)
                run = [cur]
        runs.append(run)
        for r in runs:
            if len(r) < COMB_MIN_RUN:
                continue
            head = r[0]
            line = Line(
                page_no=head.page_no, block_no=head.block_no, line_no=head.line_no,
                spans=[s for ln in r for s in ln.spans],
                text="".join(ln.text for ln in r),
                offsets=[c for ln in r for c in ln.offsets],
                bbox=union(ln.bbox for ln in r),
            )
            merged[id(head)] = line
            absorbed.update(id(ln) for ln in r[1:])

    if not merged:
        return blocks
    out: list[Block] = []
    for b in blocks:
        lines = [merged.get(id(ln), ln) for ln in b.lines if id(ln) not in absorbed]
        if lines:
            out.append(Block(page_no=b.page_no, block_no=b.block_no, lines=lines,
                             bbox=union(ln.bbox for ln in lines) or b.bbox))
    return out


def comb_values_text(doc: pymupdf.Document) -> str:
    """The rejoined comb-field values of an OPEN document, one per line.

    The verifier reads saved files with plain get_text(), which splits a boxed
    SSN into nine one-digit lines - so a surviving SSN was invisible to the
    "originals removed" check and the file was reported EXPORT VERIFIED. It
    appends this text to see what the analysis sees.
    """
    provider = NativePdfTextProvider()
    out: list[str] = []
    for i in range(doc.page_count):
        for line in provider._build_page(doc, i).lines:
            if len(line.spans) >= COMB_MIN_RUN and all(len(s.text) == 1 for s in line.spans):
                out.append(line.text)
    return "\n".join(out)
