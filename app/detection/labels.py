"""Field label detection (spec section 13).

A label is evidence about the value near it. It is never PII itself and must
never be redacted - "Name:" stays, "John Smith" goes.
"""

from __future__ import annotations

import re
from typing import Optional

from ..document.model import Document, Line
from .deterministic import RuleSet, load_rules
from .types import LabelRegion, PiiType

COLON_RE = re.compile(r"^(?P<label>[^:]{2,60}):\s*(?P<value>.*)$")
# A trailing run of dots/underscores is a form leader, not part of the label.
LEADER_RE = re.compile(r"[.\u2026_\-]{2,}\s*$")


def _clean(text: str) -> str:
    return LEADER_RE.sub("", text).strip()


def _match_label(text: str, rs: RuleSet) -> Optional[tuple[list[PiiType], bool]]:
    """Returns (expected_types, is_non_pii) or None if not a recognised label."""
    cleaned = _clean(text)
    if not cleaned or len(cleaned) > 70:
        return None
    for pattern in rs.non_pii_labels:
        if pattern.match(cleaned):
            return [], True
    for rule in rs.labels:
        if rule.regex.match(cleaned):
            return list(rule.expects), False
    return None


def detect_labels(doc: Document, ruleset: Optional[RuleSet] = None) -> list[LabelRegion]:
    rs = ruleset or load_rules()
    labels: list[LabelRegion] = []
    for page in doc.pages:
        for line in page.lines:
            labels.extend(_labels_in_line(line, rs))
    return labels


def _labels_in_line(line: Line, rs: RuleSet) -> list[LabelRegion]:
    text = line.text
    out: list[LabelRegion] = []

    colon = COLON_RE.match(text)
    if colon:
        raw = colon.group("label")
        matched = _match_label(raw, rs)
        if matched is not None:
            expects, non_pii = matched
            start, end = 0, len(raw) + 1  # include the colon in the label region
            rect = line.rect_for(start, end)
            if rect:
                out.append(
                    LabelRegion(
                        text=raw.strip(),
                        line=line,
                        start=start,
                        end=end,
                        rect=rect,
                        expected_types=expects,
                        non_pii=non_pii,
                    )
                )
        return out

    # Standalone label line: "Name, address, and zip code" with the value stacked
    # underneath (spec sections 14 and 84).
    matched = _match_label(text, rs)
    if matched is not None:
        expects, non_pii = matched
        rect = line.rect_for(0, len(text))
        if rect:
            out.append(
                LabelRegion(
                    text=text.strip(),
                    line=line,
                    start=0,
                    end=len(text),
                    rect=rect,
                    expected_types=expects,
                    non_pii=non_pii,
                )
            )
    return out


def label_line_keys(labels: list[LabelRegion]) -> set[tuple[int, int, int]]:
    """Lines that START with a label - never eligible as stacked value lines.

    This includes "Taxable income: $123,456", which begins a new (non-PII)
    field and must therefore terminate the preceding field's value group.
    """
    return {lb.line.key() for lb in labels if lb.start == 0}
