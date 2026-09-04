"""Logical field groups: stacked and compound PII (spec sections 14-17).

This is the module that stops the system from mistaking one detected token for
a whole sensitive region. A label such as

    Name, address, and zip code
    LJP
    Fremont, CA
    123

is bound to all three value lines, and every value line in a PII-bearing group
becomes a redaction target even if no individual detector fired on it.
"""

from __future__ import annotations

import re
from typing import Optional

from ..document.model import Document, Line
from .deterministic import MONEY_RE, PERCENT_RE, RuleSet, load_rules
from .labels import detect_labels, label_line_keys
from .types import (
    Candidate,
    Evidence,
    LabelRegion,
    LogicalFieldGroup,
    PiiType,
    Source,
)

MAX_STACKED_LINES = 6
FULL_COVERAGE_RATIO = 0.95
LINE_GAP_FACTOR = 1.9          # multiples of line height before the group ends
LEFT_EDGE_TOLERANCE = 14.0     # points of allowed left-edge drift
INLINE_MIN_GAP = -2.0

#: Which detected types satisfy which expected type during coverage analysis.
SATISFIED_BY: dict[PiiType, set[PiiType]] = {
    PiiType.ADDRESS: {PiiType.ADDRESS, PiiType.STREET, PiiType.CITY_STATE, PiiType.PO_BOX, PiiType.POSTAL_CODE},
    PiiType.POSTAL_CODE: {PiiType.POSTAL_CODE, PiiType.ADDRESS},
    PiiType.PERSON: {PiiType.PERSON},
    PiiType.TIN: {PiiType.TIN, PiiType.SSN, PiiType.EIN, PiiType.ITIN},
}

ONLY_MONEY_RE = re.compile(r"^[\s$()\-]*[\d,.]+[\s$()%]*$")


def build_groups(
    doc: Document,
    candidates: list[Candidate],
    ruleset: Optional[RuleSet] = None,
) -> tuple[list[LogicalFieldGroup], list[LabelRegion], list[Candidate]]:
    """Returns (groups, labels, extra_candidates_for_uncovered_value_lines)."""
    rs = ruleset or load_rules()
    labels = detect_labels(doc, rs)
    label_keys = label_line_keys(labels)
    lines_by_page = {p.number: p.lines_in_reading_order() for p in doc.pages}

    groups: list[LogicalFieldGroup] = []
    extra: list[Candidate] = []
    claimed: set[tuple[int, int, int]] = set()

    for idx, label in enumerate(labels):
        if label.non_pii or not label.expected_types:
            continue
        value_lines, inline_span = _value_lines_for(label, lines_by_page, label_keys, claimed)
        if not value_lines and inline_span is None:
            continue

        group = LogicalFieldGroup(
            group_id=f"g{idx}",
            label=label,
            value_lines=value_lines,
            expected_types=list(label.expected_types),
        )

        if inline_span is not None:
            start, end = inline_span
            extra.extend(_cover_span(label.line, start, end, group, candidates))
        for line in value_lines:
            claimed.add(line.key())
            start, end = _trimmed_span(line)
            if start is None:
                continue
            extra.extend(_cover_span(line, start, end, group, candidates))

        groups.append(group)

    return groups, labels, extra


# -- value line discovery ---------------------------------------------------


def _value_lines_for(
    label: LabelRegion,
    lines_by_page: dict[int, list[Line]],
    label_keys: set[tuple[int, int, int]],
    claimed: set[tuple[int, int, int]],
) -> tuple[list[Line], Optional[tuple[int, int]]]:
    line = label.line
    inline: Optional[tuple[int, int]] = None

    # Case A: value sits after the label on the same line ("Name: John Smith").
    remainder = line.text[label.end:]
    if remainder.strip():
        start = label.end + (len(remainder) - len(remainder.lstrip()))
        end = len(line.text.rstrip())
        if end > start:
            inline = (start, end)

    # Case B: value(s) stacked underneath the label.
    stacked: list[Line] = []
    if inline is None:
        siblings = lines_by_page.get(line.page_no, [])
        below = [
            other
            for other in siblings
            if other.bbox[1] > line.bbox[1] + 0.4 * max(line.height, 1.0)
        ]
        below.sort(key=lambda ln: (ln.bbox[1], ln.bbox[0]))
        anchor_left = line.bbox[0]
        prev_bottom = line.bbox[3]
        for candidate_line in below:
            if len(stacked) >= MAX_STACKED_LINES:
                break
            if candidate_line.key() in label_keys or candidate_line.key() in claimed:
                break
            gap = candidate_line.bbox[1] - prev_bottom
            if gap > LINE_GAP_FACTOR * max(candidate_line.height, line.height, 1.0):
                break
            if abs(candidate_line.bbox[0] - anchor_left) > LEFT_EDGE_TOLERANCE:
                break
            if _is_pure_financial(candidate_line.text):
                break
            if MONEY_RE.search(candidate_line.text) or PERCENT_RE.search(candidate_line.text):
                # A financial fact starts a new region; never absorb it into a
                # PII value group (spec sections 12 and 85).
                break
            stacked.append(candidate_line)
            prev_bottom = candidate_line.bbox[3]
            anchor_left = candidate_line.bbox[0] if len(stacked) == 1 else anchor_left

    return stacked, inline


def _is_pure_financial(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if MONEY_RE.fullmatch(stripped) or PERCENT_RE.fullmatch(stripped):
        return True
    return bool(ONLY_MONEY_RE.match(stripped)) and ("$" in stripped or "%" in stripped)


def _trimmed_span(line: Line) -> tuple[Optional[int], int]:
    text = line.text
    start = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    if end <= start:
        return None, 0
    # Never pull a currency amount into a group-level redaction (section 85).
    for m in list(MONEY_RE.finditer(text)) + list(PERCENT_RE.finditer(text)):
        if m.start() <= start < m.end():
            start = m.end()
        if m.start() < end <= m.end():
            end = m.start()
    return (start, end) if end > start else (None, 0)


def _cover_span(
    line: Line,
    start: int,
    end: int,
    group: LogicalFieldGroup,
    candidates: list[Candidate],
) -> list[Candidate]:
    """Attach existing detections to the group; synthesise one if uncovered."""
    existing = [
        c
        for c in candidates
        if c.line.key() == line.key() and c.start < end and start < c.end
    ]
    for c in existing:
        c.group_id = c.group_id or group.group_id
        group.detected_types.add(c.pii_type)

    covered = sum(
        len(line.text[max(c.start, start):min(c.end, end)].strip()) for c in existing
    )
    total = len(line.text[start:end].strip())
    # Near-total coverage only. A threshold of "most of the line" lets cases like
    # "Apartment 4B" through with " 4B" surviving in the output - exactly the
    # partial-redaction failure spec section 39 calls critical.
    if total and covered / total >= FULL_COVERAGE_RATIO:
        return []

    rect = line.rect_for(start, end)
    if rect is None:
        return []
    return [
        Candidate(
            pii_type=PiiType.UNCLASSIFIED_GROUP_VALUE,
            text=line.text[start:end],
            page_no=line.page_no,
            rect=rect,
            line=line,
            start=start,
            end=end,
            confidence=0.7,
            source=Source.GROUP,
            evidence=[
                Evidence(
                    Source.GROUP,
                    f"value line of labelled group '{group.label.text}'",
                    0.7,
                )
            ],
            group_id=group.group_id,
            needs_review=True,
            review_reason=(
                "value line belongs to a sensitive labelled field but no detector "
                "classified it - review before accepting"
            ),
        )
    ]


# -- coverage analysis (sections 18-19) -------------------------------------


def analyse_coverage(groups: list[LogicalFieldGroup], candidates: list[Candidate]) -> None:
    by_group: dict[str, list[Candidate]] = {}
    for c in candidates:
        if c.group_id:
            by_group.setdefault(c.group_id, []).append(c)

    for group in groups:
        members = by_group.get(group.group_id, [])
        group.detected_types = {c.pii_type for c in members}
        missing = []
        for expected in group.expected_types:
            accepted = SATISFIED_BY.get(expected, {expected})
            if not (group.detected_types & accepted):
                missing.append(expected)

        unclassified = [c for c in members if c.pii_type is PiiType.UNCLASSIFIED_GROUP_VALUE]
        if missing and not unclassified:
            group.complete = False
            group.reason = (
                "PARTIAL / INCOMPLETE DETECTION: expected "
                + ", ".join(t.value for t in missing)
                + " in this field but did not find it"
            )
            for c in members:
                c.needs_review = True
                c.review_reason = c.review_reason or group.reason
        elif missing and unclassified:
            group.complete = False
            group.reason = (
                "PARTIAL: unclassified value lines cover the missing "
                + ", ".join(t.value for t in missing)
            )
        else:
            group.complete = True
