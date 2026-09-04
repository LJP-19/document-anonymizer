"""Text providers (spec section 6).

The rest of the application talks to `DocumentTextProvider`, never to PyMuPDF
directly, so that an OCR provider can be added in V2 without touching detection,
transformation, or verification.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import pymupdf

from .model import Block, Char, Document, Line, Page, Rect, Span, union

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

        needs_ocr, reason = self._assess_ocr(page, char_count)
        return Page(
            number=index,
            width=page.rect.width,
            height=page.rect.height,
            blocks=blocks,
            needs_ocr=needs_ocr,
            ocr_reason=reason,
        )

    def _build_line(self, page_no: int, b_no: int, l_no: int, raw_line: dict) -> Optional[Line]:
        spans: list[Span] = []
        text_parts: list[str] = []
        offsets: list[Optional[Char]] = []
        prev_x1: Optional[float] = None

        for raw_span in raw_line.get("spans", []):
            chars = [
                Char(text=c["c"], bbox=tuple(c["bbox"]))
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
                )
            )
            text_parts.append(span_text)
            offsets.extend(chars)
            prev_x1 = chars[-1].bbox[2]

        if not spans:
            return None
        text = "".join(text_parts)
        assert len(text) == len(offsets), "offset map desynchronised from line text"
        return Line(
            page_no=page_no,
            block_no=b_no,
            line_no=l_no,
            spans=spans,
            text=text,
            offsets=offsets,
            bbox=tuple(raw_line.get("bbox")) if raw_line.get("bbox") else union(s.bbox for s in spans),
        )

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
