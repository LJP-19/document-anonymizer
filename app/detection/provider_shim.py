"""Build a throwaway Document from plain page text.

The second pass needs to run the detectors over text it holds in memory, with
no PDF behind it. Geometry is synthetic and never used for redaction - findings
are located back in the real source document before anything can act on them.
"""

from __future__ import annotations

from typing import Optional

from ..document.model import Block, Char, Document, Line, Page, Span

CHAR_WIDTH = 5.0
LINE_HEIGHT = 12.0


def document_from_pages(pages: list[str]) -> Optional[Document]:
    if not pages:
        return None
    built: list[Page] = []
    for page_no, text in enumerate(pages):
        lines: list[Line] = []
        for line_no, raw in enumerate(text.splitlines()):
            if not raw.strip():
                continue
            top = line_no * LINE_HEIGHT
            chars = [
                Char(text=ch, bbox=(index * CHAR_WIDTH, top,
                                    (index + 1) * CHAR_WIDTH, top + LINE_HEIGHT))
                for index, ch in enumerate(raw)
            ]
            span = Span(
                text=raw,
                bbox=(0.0, top, len(raw) * CHAR_WIDTH, top + LINE_HEIGHT),
                font="helv",
                size=10.0,
                color=0,
                chars=chars,
            )
            lines.append(
                Line(
                    page_no=page_no,
                    block_no=0,
                    line_no=line_no,
                    spans=[span],
                    text=raw,
                    offsets=list(chars),
                    bbox=span.bbox,
                )
            )
        blocks = [Block(page_no=page_no, block_no=0, lines=lines,
                        bbox=(0.0, 0.0, 600.0, max(len(lines), 1) * LINE_HEIGHT))] if lines else []
        built.append(Page(number=page_no, width=612.0, height=792.0, blocks=blocks))
    return Document(path="<second-pass>", pages=built)
