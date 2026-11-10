"""Upright reading frame for pages whose text runs sideways or upside down.

Tax software often prints a landscape form (Form 4562, Schedule E continuation
sheets) on a PORTRAIT page with the text rotated 90 degrees. PyMuPDF reports such
text in page coordinates, where every line runs vertically. Everything that reads
layout - labels to the left of or above their values, columns, rows, form text -
assumes horizontal lines, so on those pages labels bound nothing and identities
under them were missed; and replacement text was written horizontally into the
narrow vertical box of the original, shrinking to an unreadable size.

The fix: on such a page every glyph box is converted into an UPRIGHT frame at
extraction (the page as a person reading it sees it), so all detection works
unchanged. Geometry is converted back only where the real PDF is touched - the
redactor (which also writes the replacement at the original angle), the export
check, and the on-screen overlays.

Mapping for text direction (dx, dy) in page coordinates (y grows downward):
  (0, -1)  rotation 90  - text runs UP the page:   x' = H - y, y' = x
  (0,  1)  rotation 270 - text runs DOWN the page: x' = y,     y' = W - x
  (-1, 0)  rotation 180 - upside down:             x' = W - x, y' = H - y
PyMuPDF's insert_text(point, text, rotate=rotation) writes text in exactly that
direction from `point` (verified: rotate=90 gives dir (0, -1)).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

Rect = tuple[float, float, float, float]

#: Share of a page's characters that must run in one non-horizontal direction for
#: the page to be read in that direction's frame.
DOMINANT_SHARE = 0.6
#: Fewer characters than this never changes a page's frame.
MIN_CHARS = 20

_ROTATION_FOR_DIR = {(0, -1): 90, (0, 1): 270, (-1, 0): 180}


@dataclass(frozen=True)
class PageFrame:
    rotation: int        # 90, 180 or 270
    width: float         # the PDF page's own (unrotated) width
    height: float

    @property
    def frame_size(self) -> tuple[float, float]:
        if self.rotation in (90, 270):
            return self.height, self.width
        return self.width, self.height

    # -- points ----------------------------------------------------------------

    def to_frame_point(self, x: float, y: float) -> tuple[float, float]:
        if self.rotation == 90:
            return self.height - y, x
        if self.rotation == 270:
            return y, self.width - x
        return self.width - x, self.height - y

    def to_pdf_point(self, x: float, y: float) -> tuple[float, float]:
        if self.rotation == 90:
            return y, self.height - x
        if self.rotation == 270:
            return self.width - y, x
        return self.width - x, self.height - y

    # -- rectangles --------------------------------------------------------------

    def to_frame_rect(self, rect: Rect) -> Rect:
        return _normal(self.to_frame_point(rect[0], rect[1]) + self.to_frame_point(rect[2], rect[3]))

    def to_pdf_rect(self, rect: Rect) -> Rect:
        return _normal(self.to_pdf_point(rect[0], rect[1]) + self.to_pdf_point(rect[2], rect[3]))


def _normal(r) -> Rect:
    x0, y0, x1, y1 = r
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def dominant_frame(raw: dict, width: float, height: float) -> Optional[PageFrame]:
    """The frame a page should be read in, from PyMuPDF rawdict output, or None
    when most of its text already runs left to right."""
    counts: dict = {}
    total = 0
    for block in raw.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1, 0))
            key = (round(dx), round(dy))
            n = sum(len(s.get("chars", [])) for s in line.get("spans", []))
            counts[key] = counts.get(key, 0) + n
            total += n
    if total < MIN_CHARS:
        return None
    key, n = max(counts.items(), key=lambda kv: kv[1])
    rotation = _ROTATION_FOR_DIR.get(key)
    if rotation is None or n < DOMINANT_SHARE * total:
        return None
    return PageFrame(rotation=rotation, width=width, height=height)


def to_frame_raw(raw: dict, frame: PageFrame) -> None:
    """Rewrite every bbox and origin of a rawdict IN PLACE into the upright frame."""
    def rect(r):
        return frame.to_frame_rect(tuple(r)) if r else r

    for block in raw.get("blocks", []):
        if "bbox" in block:
            block["bbox"] = rect(block["bbox"])
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            line["bbox"] = rect(line.get("bbox"))
            dx, dy = line.get("dir", (1, 0))
            # The line's own direction, seen in the frame: the dominant direction
            # becomes (1, 0); any other keeps its angle relative to it.
            fx = frame.to_frame_point(dx, dy)
            ox = frame.to_frame_point(0.0, 0.0)
            line["dir"] = (round(fx[0] - ox[0], 6), round(fx[1] - ox[1], 6))
            for span in line.get("spans", []):
                span["bbox"] = rect(span.get("bbox"))
                if span.get("origin"):
                    span["origin"] = frame.to_frame_point(*span["origin"])
                for ch in span.get("chars", []):
                    ch["bbox"] = rect(ch["bbox"])
                    if ch.get("origin"):
                        ch["origin"] = frame.to_frame_point(*ch["origin"])
