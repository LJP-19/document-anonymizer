"""Overlap and conflict resolution (spec sections 23-24).

Detectors are additive; evidence is never overwritten by whichever detector ran
last. Where two candidates cover the same text the more complete/specific one
wins and inherits the other's evidence. Where the winner is genuinely ambiguous
the survivor is flagged Needs Review rather than silently resolved.
"""

from __future__ import annotations

from itertools import groupby

from .types import Candidate, LabelRegion, PiiType, Source

#: Types that describe the same underlying thing - not a real disagreement.
COMPATIBLE: list[set[PiiType]] = [
    {PiiType.ADDRESS, PiiType.STREET, PiiType.CITY_STATE, PiiType.PO_BOX, PiiType.POSTAL_CODE},
    {PiiType.TIN, PiiType.SSN, PiiType.EIN, PiiType.ITIN},
    {PiiType.PHONE, PiiType.FAX},
    {PiiType.ACCOUNT_ID, PiiType.CUSTOMER_ID, PiiType.MEMBER_ID},
]


def _compatible(a: PiiType, b: PiiType) -> bool:
    return a is b or any(a in family and b in family for family in COMPATIBLE)

#: Higher rank wins an overlap. Group-level spans outrank token detections
#: because they represent a complete logical value (spec section 23).
SOURCE_RANK = {Source.MANUAL: 4, Source.GROUP: 3, Source.REGEX: 2, Source.NER: 1, Source.COVERAGE: 1}


def strip_label_overlaps(candidates: list[Candidate], labels: list[LabelRegion]) -> list[Candidate]:
    """A label must never be redacted (sections 13 and 85)."""
    by_line: dict[tuple[int, int, int], list[LabelRegion]] = {}
    for lb in labels:
        by_line.setdefault(lb.line.key(), []).append(lb)

    out: list[Candidate] = []
    for c in candidates:
        trimmed = c
        for lb in by_line.get(c.line.key(), []):
            if c.start < lb.end and lb.start < c.end:
                # Clip the candidate so it starts after the label text.
                new_start = max(c.start, lb.end)
                new_end = c.end
                if new_end <= new_start:
                    trimmed = None
                    break
                text = c.line.text[new_start:new_end]
                lead = len(text) - len(text.lstrip())
                new_start += lead
                rect = c.line.rect_for(new_start, new_end)
                if rect is None or new_end <= new_start:
                    trimmed = None
                    break
                trimmed = Candidate(
                    pii_type=c.pii_type,
                    text=c.line.text[new_start:new_end],
                    page_no=c.page_no,
                    rect=rect,
                    line=c.line,
                    start=new_start,
                    end=new_end,
                    confidence=c.confidence,
                    source=c.source,
                    evidence=list(c.evidence),
                    group_id=c.group_id,
                    needs_review=c.needs_review,
                    review_reason=c.review_reason,
                )
        if trimmed is not None:
            out.append(trimmed)
    return out


def resolve_overlaps(candidates: list[Candidate]) -> list[Candidate]:
    ordered = sorted(candidates, key=lambda c: (c.line.key(), c.start, -c.end))
    resolved: list[Candidate] = []

    for _key, line_group in groupby(ordered, key=lambda c: c.line.key()):
        items = list(line_group)
        kept: list[Candidate] = []
        for cand in items:
            conflict = next((k for k in kept if k.overlaps(cand)), None)
            if conflict is None:
                kept.append(cand)
                continue
            winner, loser = _pick(conflict, cand)
            if winner is cand:
                kept[kept.index(conflict)] = cand
            _absorb(winner, loser)
        resolved.extend(kept)

    return sorted(resolved, key=lambda c: (c.page_no, c.rect[1], c.rect[0]))


def _pick(a: Candidate, b: Candidate) -> tuple[Candidate, Candidate]:
    ra, rb = SOURCE_RANK[a.source], SOURCE_RANK[b.source]
    if ra != rb:
        return (a, b) if ra > rb else (b, a)
    span_a, span_b = a.end - a.start, b.end - b.start
    if span_a != span_b:
        return (a, b) if span_a > span_b else (b, a)
    return (a, b) if a.confidence >= b.confidence else (b, a)


def _absorb(winner: Candidate, loser: Candidate) -> None:
    """Keep the losing detector's evidence; flag genuine disagreement."""
    winner.evidence.extend(loser.evidence)
    winner.group_id = winner.group_id or loser.group_id
    if not _compatible(winner.pii_type, loser.pii_type) and winner.source is not Source.GROUP:
        winner.needs_review = True
        winner.review_reason = (
            winner.review_reason
            or f"detectors disagree: {winner.pii_type.value} vs {loser.pii_type.value}"
        )
    winner.confidence = max(winner.confidence, loser.confidence)
