"""The form's own text (spec sections 12, 13, 85).

Label protection only covered labels the taxonomy recognises. A blank form is
full of other text that belongs to the document rather than to the client:
headings, captions, instructions, footnotes, the running header on every page.
None of it is PII, and pseudonymising any of it destroys what the document says
while protecting nobody.

This module identifies that text so it can be vetoed before anything acts on it.
The signals are structural rather than semantic, so they hold on forms nobody
has written a rule for:

  - the line repeats in the same place across several pages (header, footer)
  - it reads as an instruction ("see instructions", "check if", "attach")
  - it is a recognised field label
  - it is entirely the document's own vocabulary
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..document.model import Document, Line, Rect

#: Phrasing that only appears in a form's own text, never in a filled value.
INSTRUCTION = re.compile(
    r"\b(see\s+(the\s+)?instructions?|check\s+(if|the\s+box)|enter\s+(the|your|amount)|"
    r"attach\s+(to|form|schedule)|if\s+you|do\s+not\s+(write|file|send)|for\s+(privacy|paperwork)|"
    r"go\s+to\s+www|complete\s+(and|this)|use\s+(this|the)|leave\s+blank|"
    r"for\s+official\s+use|page\s+\d+\s+of\s+\d+|form\s+\d{3,4}|schedule\s+[a-z]\b|"
    r"cat\.?\s*no\.?|omb\s+no)",
    re.I,
)

#: A line appearing on at least this many pages at the same height is chrome.
REPEAT_PAGES = 3
POSITION_TOLERANCE = 6.0


@dataclass
class FormText:
    """Regions belonging to the document rather than to the client."""

    lines: set[tuple[int, int, int]] = field(default_factory=set)
    reasons: dict[tuple[int, int, int], str] = field(default_factory=dict)

    def covers(self, line: Line) -> bool:
        return line.key() in self.lines

    def reason_for(self, line: Line) -> str:
        return self.reasons.get(line.key(), "")

    def add(self, line: Line, reason: str) -> None:
        self.lines.add(line.key())
        self.reasons.setdefault(line.key(), reason)


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def detect_form_text(doc: Document, labels: Optional[list] = None) -> FormText:
    found = FormText()

    # Repeated running text: the same words in the same place on several pages.
    seen: dict[tuple[str, int], list[Line]] = {}
    for page in doc.pages:
        for line in page.lines:
            normal = _normalized(line.text)
            if len(normal) < 4:
                continue
            band = int(line.bbox[1] / POSITION_TOLERANCE)
            seen.setdefault((normal, band), []).append(line)

    for (_normal, _band), lines in seen.items():
        pages = {line.page_no for line in lines}
        if len(pages) >= REPEAT_PAGES:
            for line in lines:
                found.add(line, "repeats on every page")

    for page in doc.pages:
        for line in page.lines:
            if found.covers(line):
                continue
            if INSTRUCTION.search(line.text):
                found.add(line, "form instruction")

    for label in labels or []:
        # A label occupying its whole line is form text; one sharing a line with
        # its value is handled by label clipping instead.
        if label.start == 0 and label.end >= len(label.line.text.rstrip()):
            found.add(label.line, "field label")

    return found


def veto_form_text(candidates: list, form_text: FormText) -> tuple[list, int]:
    """Drop candidates that sit on the document's own text.

    Returns (kept, dropped_count). Manual additions are never dropped: the user
    has said what they want, and a heuristic does not overrule that.
    """
    from .types import Source

    kept = []
    dropped = 0
    for candidate in candidates:
        if candidate.source is Source.MANUAL:
            kept.append(candidate)
            continue
        if form_text.covers(candidate.line):
            dropped += 1
            continue
        kept.append(candidate)
    return kept, dropped
