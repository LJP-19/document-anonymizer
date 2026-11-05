"""Protected regions and the unified candidate gate.

The anonymizer's job is to remove identity and keep everything else. "Everything
else" is not a list of exceptions bolted onto each detector; it is a property of
the DOCUMENT, so it lives here, in one place:

  ProtectedRegion   a span of real page text that is not the client's identity
                    - a field label, a heading, a money amount, a line or form
                    number, a tax year - with the reason it is protected.
  ProtectionIndex   all regions for a document, queried by line and character
                    span (not by whole line: "Taxpayer Name: John Smith" has a
                    protected label AND a value that must survive).
  CandidateGate     the last word on every candidate, whichever layer proposed it
                    (rules, spaCy, GLiNER, Presidio, heuristics, the local LLM,
                    propagation, the second pass, an entity rescan). It clips a
                    candidate back to its real value, rejects one that is wholly
                    protected, and records WHY on the candidate and in a log.

Earlier the same protection was re-implemented in seven separate passes
(strip_label_overlaps, veto_form_text, _drop_label_text, _drop_financial_values,
veto_by_label_geometry, the GLiNER negatives veto, ...) and still missed a path:
the second-pass leak check appended findings with no label gate at all, which is
how a label such as "Member ID" came back as an ADDRESS. Those passes remain as
defence in depth; this gate is the authority.

Hard rule: no detector - spaCy, GLiNER, Presidio, the LLM, a heuristic,
propagation or widening - may redact a HARD protected region. Only an explicit
user decision (Source.MANUAL) overrides protection.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional

from ..document.model import Document, Line
from .types import Candidate, Evidence, LabelRegion, LogicalFieldGroup, NUMERIC_LIKE, PiiType, Source

log = logging.getLogger(__name__)

HARD = 1.0


class ProtectionKind(str, Enum):
    FORM_LABEL = "FORM_LABEL"
    FORM_HEADING = "FORM_HEADING"
    FORM_TITLE = "FORM_TITLE"
    FORM_INSTRUCTION = "FORM_INSTRUCTION"
    FORM_NUMBER = "FORM_NUMBER"
    LINE_NUMBER = "LINE_NUMBER"
    TAX_YEAR = "TAX_YEAR"
    TABLE_HEADER = "TABLE_HEADER"
    FINANCIAL_VALUE = "FINANCIAL_VALUE"
    MONEY_AMOUNT = "MONEY_AMOUNT"
    PERCENT_VALUE = "PERCENT_VALUE"
    STATIC_BOILERPLATE = "STATIC_BOILERPLATE"
    RUNNING_HEADER = "RUNNING_HEADER"
    RUNNING_FOOTER = "RUNNING_FOOTER"
    NON_PII_FIELD = "NON_PII_FIELD"


#: Business figures and form identifiers: protected as VALUES, never clipped out
#: of the middle of a date or an identifier.
FIGURE_KINDS = {
    ProtectionKind.MONEY_AMOUNT, ProtectionKind.FINANCIAL_VALUE, ProtectionKind.PERCENT_VALUE,
    ProtectionKind.TAX_YEAR, ProtectionKind.LINE_NUMBER, ProtectionKind.FORM_NUMBER,
}

#: A figure inside a field labelled as one of these is identity, not a business
#: figure ("Account Number: 123456789", "DOB: 1985").
FIGURE_WAIVER_TYPES = set(NUMERIC_LIKE) | {PiiType.DOB, PiiType.PERSONAL_DATE}

#: A candidate of one of these types may legitimately CONTAIN a figure-looking
#: part ("March 3, 2010"), so a figure inside it is never clipped out.
_FIGURE_CLIP_EXEMPT = FIGURE_WAIVER_TYPES | {PiiType.ADDRESS, PiiType.STREET, PiiType.CITY_STATE}


# --------------------------------------------------------------------- figures

_CUR = r"(?:[$€£¥₹₩₽₪₫฿]|(?:USD|EUR|GBP|CAD|AUD|JPY|CNY|INR|CHF|MXN|NZD|SEK|NOK|DKK|HKD|SGD)(?![A-Za-z]))"
_NOT_WORD = r"(?<![\w.,/])"

_FIGURE_PATTERNS: list[tuple[ProtectionKind, str, re.Pattern]] = [
    (ProtectionKind.MONEY_AMOUNT, "currency amount",
     re.compile(rf"\(?-?\s?{_CUR}\s?\d[\d,]*(?:\.\d+)?\s?\)?|\d[\d,]*(?:\.\d+)?\s?{_CUR}(?![\w])")),
    (ProtectionKind.MONEY_AMOUNT, "amount with thousands separators",
     re.compile(rf"{_NOT_WORD}\(?-?\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?\)?(?![\w,])")),
    (ProtectionKind.MONEY_AMOUNT, "decimal amount",
     re.compile(rf"{_NOT_WORD}\(?-?\d+\.\d{{2}}\)?(?![\w.])")),
    (ProtectionKind.PERCENT_VALUE, "percentage",
     re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?\s?%")),
    (ProtectionKind.TAX_YEAR, "tax year",
     re.compile(r"(?<![\w./,\-])(?:19|20)\d{2}(?![\w/\-])")),
    (ProtectionKind.LINE_NUMBER, "line number",
     re.compile(r"\b(?:lines?|ln\.?)\s+\d{1,3}[a-z]?(?:\([a-z0-9]\))?(?![\w])", re.I)),
    (ProtectionKind.FORM_NUMBER, "form or schedule reference",
     re.compile(
         r"\b(?:forms?|schedules?|sch\.?|pub(?:lication)?\.?|notice)\s+"
         r"(?:(?-i:[A-Z]{1,3})-?\d{1,4}(?-i:[A-Z])?(?:-(?-i:[A-Z]{1,3}))?|\d{3,4}(?:-?(?-i:[A-Z]{1,3}))?|(?-i:[A-Z]{1,2})\b)",
         re.I)),
    (ProtectionKind.FORM_NUMBER, "tax form number",
     re.compile(
         r"(?<![\w./,\-])(?:1040(?:-?(?:SR|NR|X|ES))?|1065|1120(?:-?S)?|1099(?:-?[A-Z]{2,5})?|"
         r"941(?:-?X)?|940|W-?[2-9]|SS-?4|8832|2553|4868|7004|8949|8962)(?![\w/\-])", re.I)),
    (ProtectionKind.FORM_NUMBER, "page number",
     re.compile(r"\bpage\s+\d+(?:\s+of\s+\d+)?(?![\w])", re.I)),
]


def find_figures(text: str) -> list[tuple[int, int, ProtectionKind, str]]:
    """Every business figure or form identifier in `text`, as (start, end, kind,
    reason). This is the ONE definition of 'a figure'; it does not decide
    whether a figure sits inside an identity field - ProtectionIndex does."""
    found: list[tuple[int, int, ProtectionKind, str]] = []
    for kind, reason, pattern in _FIGURE_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if end > start and any(ch.isalnum() for ch in text[start:end]):
                found.append((start, end, kind, reason))
    return found


def is_protected_figure_text(text: str) -> Optional[ProtectionKind]:
    """The kind of figure `text` is ENTIRELY made of, else None."""
    stripped = text.strip()
    if not stripped:
        return None
    for start, end, kind, _reason in find_figures(stripped):
        if start == 0 and end == len(stripped):
            return kind
    return None


# --------------------------------------------------------------------- regions


@dataclass(frozen=True)
class ProtectedRegion:
    page_no: int
    line_key: tuple[int, int, int]
    start: int
    end: int
    rect: Optional[tuple]
    text: str
    kind: ProtectionKind
    reason: str
    source: str
    strength: float = HARD
    field: str = ""

    @property
    def hard(self) -> bool:
        return self.strength >= HARD

    def overlaps(self, start: int, end: int) -> bool:
        return self.start < end and start < self.end


@dataclass
class ProtectionIndex:
    by_line: dict[tuple[int, int, int], list[ProtectedRegion]] = field(default_factory=dict)

    def regions_for(self, line: Line) -> list[ProtectedRegion]:
        return self.by_line.get(line.key(), [])

    def all_regions(self) -> list[ProtectedRegion]:
        return [region for regions in self.by_line.values() for region in regions]

    def _add(self, region: ProtectedRegion) -> None:
        self.by_line.setdefault(region.line_key, []).append(region)

    def explain(self, candidate: Candidate) -> list[ProtectedRegion]:
        return [r for r in self.regions_for(candidate.line) if r.overlaps(candidate.start, candidate.end)]

    @classmethod
    def build(
        cls,
        doc: Document,
        labels: list[LabelRegion],
        form_text=None,
        groups: Optional[list[LogicalFieldGroup]] = None,
    ) -> "ProtectionIndex":
        index = cls()
        lines = {line.key(): line for line in doc.all_lines()}

        # Where a figure is IDENTITY rather than a business fact: to the right of
        # a label that expects an identifier, or in the value lines of such a group.
        identity_from: dict[tuple[int, int, int], int] = {}
        identity_lines: set[tuple[int, int, int]] = set()
        for label in labels:
            if not label.non_pii and set(label.expected_types) & FIGURE_WAIVER_TYPES:
                key = label.line.key()
                identity_from[key] = min(identity_from.get(key, label.end), label.end)
        for group in groups or []:
            if not group.label.non_pii and set(group.expected_types) & FIGURE_WAIVER_TYPES:
                identity_lines.update(line.key() for line in group.value_lines)

        # Labels: protected, including the unknown label-shaped ones - a label is
        # context whether or not the taxonomy knows it.
        for label in labels:
            text = label.line.text
            index._add(ProtectedRegion(
                label.line.page_no, label.line.key(), label.start, label.end, label.rect,
                text[label.start:label.end], ProtectionKind.FORM_LABEL,
                f"field label '{label.text.strip()}'", "labels"))
            if label.non_pii and label.end < len(text.rstrip()):
                value_end = len(text.rstrip())
                index._add(ProtectedRegion(
                    label.line.page_no, label.line.key(), label.end, value_end,
                    label.line.rect_for(label.end, value_end), text[label.end:value_end],
                    ProtectionKind.NON_PII_FIELD,
                    f"value of non-identity field '{label.text.strip()}'", "labels"))
        for group in groups or []:
            if group.label.non_pii:
                for line in group.value_lines:
                    text = line.text.rstrip()
                    if text:
                        index._add(ProtectedRegion(
                            line.page_no, line.key(), 0, len(text), line.rect_for(0, len(text)),
                            text, ProtectionKind.NON_PII_FIELD,
                            f"value of non-identity field '{group.label.text.strip()}'", "groups"))

        # Form text: whole lines that belong to the document, not the client.
        if form_text is not None:
            for key, reason in form_text.reasons.items():
                line = lines.get(key)
                if line is None or not line.text.strip():
                    continue
                text = line.text.rstrip()
                if reason == "field label":
                    kind, strength = ProtectionKind.FORM_LABEL, HARD
                elif reason == "form instruction":
                    kind, strength = ProtectionKind.FORM_INSTRUCTION, 0.9
                elif reason == "repeats on every page":
                    footer = line.bbox[1] > 0.88 * _page_height(doc, line.page_no)
                    kind = ProtectionKind.RUNNING_FOOTER if footer else (
                        ProtectionKind.RUNNING_HEADER if line.bbox[1] < 0.12 * _page_height(doc, line.page_no)
                        else ProtectionKind.STATIC_BOILERPLATE)
                    strength = 0.9
                else:
                    kind, strength = ProtectionKind.STATIC_BOILERPLATE, 0.9
                index._add(ProtectedRegion(
                    line.page_no, key, 0, len(text), line.rect_for(0, len(text)), text,
                    kind, f"form text: {reason}", "form_text", strength))

        # Figures and form identifiers, unless they are identity values.
        for key, line in lines.items():
            text = line.text
            for start, end, kind, reason in find_figures(text):
                if key in identity_lines or (key in identity_from and start >= identity_from[key]):
                    continue
                index._add(ProtectedRegion(
                    line.page_no, key, start, end, line.rect_for(start, end), text[start:end],
                    kind, reason, "figures"))
        return index


def _page_height(doc: Document, page_no: int) -> float:
    for page in doc.pages:
        if page.number == page_no:
            return page.height or 792.0
    return 792.0


# -------------------------------------------------------------------- strength

_STRONG_IDS = {
    PiiType.SSN, PiiType.ITIN, PiiType.EIN, PiiType.TIN, PiiType.IP_PIN, PiiType.EMAIL,
    PiiType.PHONE, PiiType.CARD_NUMBER, PiiType.IBAN, PiiType.ROUTING_NUMBER,
}


def candidate_strength(candidate: Candidate) -> str:
    """strong / medium / weak, from the evidence hierarchy.

    strong  user decision; deterministic identifier rule; explicit label + value;
            a confirmed entity's occurrence.
    medium  a confident model hit; an unlabelled coverage hit.
    weak    a heuristic, a low-confidence model hit, the local LLM on its own.

    A weak model's objection never overturns a strong candidate - it flags it.
    """
    if candidate.source is Source.MANUAL:
        return "strong"
    evidence = " ".join(item.detail for item in candidate.evidence).lower()
    if candidate.source is Source.REGEX and (
        "label context" in evidence or candidate.pii_type in _STRONG_IDS
    ):
        return "strong"
    if candidate.source is Source.GROUP:
        return "strong"
    if candidate.source is Source.COVERAGE:
        return "strong" if any(w in evidence for w in ("entity", "propagat", "subject", "alias")) else "medium"
    if candidate.source is Source.NER and candidate.confidence >= 0.8:
        return "medium"
    if candidate.source is Source.REGEX:
        return "medium"
    return "weak"


def _note(candidate: Candidate, text: str) -> None:
    candidate.evidence.append(Evidence(candidate.source, f"gate: {text}", 0.0))


def apply_ai_objections(
    candidates: list[Candidate], objected: set[str], what: str
) -> tuple[list[Candidate], int, int]:
    """What the local model says is NOT identity, applied with proportion.

    A small model is better at noticing what was MISSED than at overturning what
    was reliably found. So its objection:
      * removes a WEAK candidate (a heuristic, a low-confidence model hit);
      * only FLAGS a strong one (a deterministic identifier, a label-bound value, a
        confirmed entity occurrence, a user's own addition) - the value stays, with
        the disagreement visible in the review list.
    Hard structural protection (labels, figures) is enforced by the gate regardless
    of what any model thinks, so it never depends on this.

    Returns (kept, dropped, flagged).
    """
    from .textnorm import fold

    wanted = {fold(value) for value in objected if value}
    if not wanted:
        return candidates, 0, 0
    kept: list[Candidate] = []
    dropped = flagged = 0
    for candidate in candidates:
        if fold(candidate.normalized) not in wanted:
            kept.append(candidate)
            continue
        if candidate_strength(candidate) == "strong":
            candidate.needs_review = True
            note = f"the AI thinks this may be {what}, not identity - keep it unless you agree"
            if note not in candidate.review_reason:
                candidate.review_reason = f"{candidate.review_reason}; {note}" if candidate.review_reason else note
            _note(candidate, f"AI objection recorded, evidence too strong to remove ({what})")
            kept.append(candidate)
            flagged += 1
        else:
            dropped += 1
    return kept, dropped, flagged


# ------------------------------------------------------------------------ gate


@dataclass
class GateDecision:
    candidate_id: str
    text: str
    pii_type: str
    action: str          # accept | clip | reject | flag
    reason: str
    kind: str = ""
    strength: str = ""


class CandidateGate:
    """Evaluate every candidate against the protected regions, once, with reasons."""

    def __init__(self, protection: ProtectionIndex):
        self.protection = protection

    def run(self, candidates: list[Candidate]) -> tuple[list[Candidate], list[GateDecision]]:
        kept: list[Candidate] = []
        log_: list[GateDecision] = []
        for candidate in candidates:
            result, decision = self.evaluate(candidate)
            log_.append(decision)
            if result is not None:
                kept.append(result)
        return kept, log_

    def evaluate(self, c: Candidate) -> tuple[Optional[Candidate], GateDecision]:
        strength = candidate_strength(c)

        def decide(action: str, reason: str, kind: str = "") -> GateDecision:
            return GateDecision(c.id, c.text, c.pii_type.value, action, reason, kind, strength)

        # Invariant 5: a candidate must be text that really exists, with geometry.
        slice_ = c.line.text[c.start:c.end] if 0 <= c.start < c.end <= len(c.line.text) else ""
        if not slice_.strip() or not any(ch.isalnum() for ch in slice_) or c.rect is None:
            return None, decide("reject", "no real source text or geometry")

        if c.source is Source.MANUAL:
            return c, decide("accept", "explicit user decision")

        start, end = c.start, c.end
        notes: list[str] = []
        soft_conflict: Optional[ProtectedRegion] = None
        for region in sorted(self.protection.regions_for(c.line), key=lambda r: (r.start, r.end)):
            if not region.overlaps(start, end):
                continue
            covers = region.start <= start and region.end >= end
            if not region.hard:
                # Soft protection (running text, instructions) yields to strong
                # evidence: flag it for review instead of silently deleting it.
                if strength == "strong":
                    soft_conflict = soft_conflict or region
                    continue
                if covers:
                    return None, decide("reject", region.reason, region.kind.value)
                continue
            if covers:
                return None, decide("reject", region.reason, region.kind.value)
            if region.kind in FIGURE_KINDS and c.pii_type in _FIGURE_CLIP_EXEMPT:
                continue
            if region.start <= start < region.end:
                start = region.end
                notes.append(f"clipped {region.kind.value.lower()} '{region.text.strip()}'")
            elif region.start < end <= region.end:
                end = region.start
                notes.append(f"clipped {region.kind.value.lower()} '{region.text.strip()}'")
            elif region.kind not in FIGURE_KINDS:
                # A label inside the candidate: keep the larger side.
                if region.start - start >= end - region.end:
                    end = region.start
                else:
                    start = region.end
                notes.append(f"split at {region.kind.value.lower()} '{region.text.strip()}'")

        text = c.line.text
        while start < end and (text[start].isspace() or text[start] in ":,;"):
            start += 1
        while end > start and (text[end - 1].isspace() or text[end - 1] in ":,;"):
            end -= 1
        if end <= start or not any(ch.isalnum() for ch in text[start:end]):
            return None, decide("reject", "nothing left after protected text was removed")

        result = c
        if (start, end) != (c.start, c.end):
            rect = c.line.rect_for(start, end)
            if rect is None:
                return None, decide("reject", "clipped span has no geometry")
            result = replace(
                c, text=text[start:end], rect=rect, start=start, end=end,
                evidence=list(c.evidence),
            )
            _note(result, "; ".join(notes))
            decision = decide("clip", "; ".join(notes))
        else:
            decision = decide("accept", f"{strength} evidence ({c.source.value})")

        if soft_conflict is not None:
            result = result if result is not c else replace(c, evidence=list(c.evidence))
            result.needs_review = True
            result.review_reason = (
                f"{result.review_reason}; " if result.review_reason else ""
            ) + f"sits on {soft_conflict.kind.value.lower().replace('_', ' ')} text - check it is really identity"
            _note(result, f"flagged: strong evidence over soft protection ({soft_conflict.reason})")
            decision = decide("flag", soft_conflict.reason, soft_conflict.kind.value)
        return result, decision
