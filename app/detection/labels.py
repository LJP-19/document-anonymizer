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


GENERIC_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9 '&/().,\-]{1,44}$")

#: A label word must account for this share of the text before the text counts
#: as that kind of label. Without it, the stray "box" in "If you have a P.O.
#: box, see instructions" would classify a whole address label as non-PII.
DOMINANCE = 0.3
VALUE_MARKERS = re.compile(r"@|\d{4,}|\$\s?[\d,]+|\d{3}[.\-]\d{3}[.\-]\d{4}")


def _carries_a_value(text: str) -> bool:
    """True if the line contains data, so it cannot be a label line on its own.

    "Daytime phone 408.555.0198  Email m.gonzalez@fastmail.co" contains the word
    "phone", but it is a value line. Treating it as a label would protect it from
    redaction and delete every detection on it.
    """
    return bool(VALUE_MARKERS.search(text))


def _match_label(text: str, rs: RuleSet) -> Optional[tuple[list[PiiType], bool, bool]]:
    """Returns (expected_types, is_non_pii, is_unknown), or None if not a label."""
    cleaned = _clean(text)
    if not cleaned or len(cleaned) > 110:
        return None
    for pattern in rs.non_pii_labels:
        m = pattern.search(cleaned)
        if m and len(m.group().strip()) / max(len(cleaned), 1) >= DOMINANCE:
            return [], True, False
    best: Optional[tuple[list[PiiType], int]] = None
    for rule in rs.labels:
        m = rule.regex.search(cleaned)
        # Require a substantial match so that a stray "name" inside unrelated
        # prose does not turn the whole line into a field label.
        if m and len(m.group().strip()) >= 3:
            span = len(m.group().strip())
            if best is None or span > best[1]:
                best = (list(rule.expects), span)
    if best is not None:
        return best[0], False, False
    # Unrecognised but label-shaped. Real forms are full of these; refusing to
    # bind their values is the main source of missed PII. The values themselves
    # still have to look identity-bearing before anything is redacted.
    if GENERIC_LABEL.match(cleaned) and any(ch.isalpha() for ch in cleaned):
        return [], False, True
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
            expects, non_pii, unknown = matched
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
                        unknown=unknown,
                    )
                )
        return out

    # Standalone label line: "Name, address, and zip code" with the value stacked
    # underneath (spec sections 14 and 84).
    if _carries_a_value(text):
        return out

    matched = _match_label(text, rs)
    if matched is not None:
        expects, non_pii, unknown = matched
        if unknown:
            # A standalone line with no colon is too weak a signal on its own.
            return out
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
                    unknown=unknown,
                )
            )
    return out


def label_line_keys(labels: list[LabelRegion]) -> set[tuple[int, int, int]]:
    """Lines that START with a label - never eligible as stacked value lines.

    This includes "Taxable income: $123,456", which begins a new (non-PII)
    field and must therefore terminate the preceding field's value group.
    """
    return {lb.line.key() for lb in labels if lb.start == 0}
