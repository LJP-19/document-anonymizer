"""Internal document representation (spec sections 8-9).

The model deliberately keeps character-level geometry so that any text offset
inside a line or block can be mapped back to an exact rectangle on the page.
Reducing a PDF to a flat string is explicitly forbidden by the spec.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1


def union(rects: Iterable[Rect]) -> Optional[Rect]:
    rects = [r for r in rects if r is not None]
    if not rects:
        return None
    return (
        min(r[0] for r in rects),
        min(r[1] for r in rects),
        max(r[2] for r in rects),
        max(r[3] for r in rects),
    )


def intersects(a: Rect, b: Rect, pad: float = 0.0) -> bool:
    return not (
        a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1]
    )


def vertical_overlap(a: Rect, b: Rect) -> float:
    """Fraction of the shorter box's height that overlaps the other box."""
    top, bottom = max(a[1], b[1]), min(a[3], b[3])
    if bottom <= top:
        return 0.0
    shorter = min(a[3] - a[1], b[3] - b[1]) or 1.0
    return (bottom - top) / shorter


@dataclass
class Char:
    text: str
    bbox: Rect


@dataclass
class Span:
    text: str
    bbox: Rect
    font: str
    size: float
    color: int
    chars: list[Char] = field(default_factory=list)


@dataclass
class Line:
    """A visual line of text plus an offset map from text index to Char."""

    page_no: int
    block_no: int
    line_no: int
    spans: list[Span]
    text: str
    offsets: list[Optional[Char]]  # same length as text; None = synthetic char
    bbox: Rect

    @property
    def size(self) -> float:
        return max((s.size for s in self.spans), default=10.0)

    @property
    def font(self) -> str:
        return self.spans[0].font if self.spans else "helv"

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    def rect_for(self, start: int, end: int) -> Optional[Rect]:
        """Exact rectangle covering text[start:end]."""
        return union(c.bbox for c in self.offsets[start:end] if c is not None)

    def key(self) -> tuple[int, int, int]:
        return (self.page_no, self.block_no, self.line_no)


@dataclass
class Block:
    """A layout block; also the unit fed to the NLP layer for prose context."""

    page_no: int
    block_no: int
    lines: list[Line]
    bbox: Rect

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    def locate(self, offset: int) -> Optional[tuple[Line, int]]:
        """Map a block-level text offset back to (line, line_offset)."""
        cursor = 0
        for line in self.lines:
            end = cursor + len(line.text)
            if cursor <= offset < end:
                return line, offset - cursor
            cursor = end + 1  # the joining newline
        return None

    def rect_for(self, start: int, end: int) -> list[tuple[Line, int, int, Rect]]:
        """Map a block-level span to per-line rectangles (handles wrapping)."""
        out: list[tuple[Line, int, int, Rect]] = []
        cursor = 0
        for line in self.lines:
            line_end = cursor + len(line.text)
            lo, hi = max(start, cursor), min(end, line_end)
            if lo < hi:
                rect = line.rect_for(lo - cursor, hi - cursor)
                if rect:
                    out.append((line, lo - cursor, hi - cursor, rect))
            cursor = line_end + 1
        return out


@dataclass
class Page:
    number: int  # zero-based
    width: float
    height: float
    blocks: list[Block]
    needs_ocr: bool = False
    ocr_reason: str = ""

    @property
    def lines(self) -> list[Line]:
        return [line for block in self.blocks for line in block.lines]

    def lines_in_reading_order(self) -> list[Line]:
        """Column-aware ordering (spec section 9)."""
        return sorted(self.lines, key=lambda ln: (_column_of(ln, self.width), ln.bbox[1], ln.bbox[0]))


def _column_of(line: Line, page_width: float, columns: int = 2) -> int:
    """Cheap column bucket; enough to keep multi-column reading order sane."""
    mid = (line.bbox[0] + line.bbox[2]) / 2
    return int(mid // (page_width / columns)) if page_width else 0


@dataclass
class Document:
    path: str
    pages: list[Page]
    producer: str = ""

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def ocr_required_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.needs_ocr]

    def all_lines(self) -> Sequence[Line]:
        return [line for page in self.pages for line in page.lines]
