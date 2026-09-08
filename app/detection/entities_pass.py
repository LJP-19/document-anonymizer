"""Known entities: who this document is about, and everywhere they appear.

Two problems solved here.

**Compound names.** A joint return names both spouses in one field:

    Taxpayer name
    JP & ML
    John and Mary Gonzalez-Reyes

Treating that as one opaque string gives both people the same pseudonym and
loses the fact that there are two of them. Each side is split out, and a shared
surname on the right is carried back to the left.

**Propagation.** A name confidently found once in a labelled taxpayer field is
the same name in a table cell forty pages later, where no detector fires because
there is no context. Once an entity is established, every occurrence of it in the
document becomes a target - which is the difference between "detects PII" and
"detects all of it".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..document.model import Document, Line
from .types import Candidate, Evidence, LogicalFieldGroup, PiiType, Source

CONJUNCTION = re.compile(r"\s*(?:&|\band\b|\+)\s*", re.I)
NAME_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|md|cpa|esq)\b\.?$", re.I)

#: Types worth chasing across the whole document once established.
PROPAGATED_TYPES = {
    PiiType.PERSON, PiiType.SSN, PiiType.ITIN, PiiType.EIN, PiiType.TIN,
    PiiType.EMAIL, PiiType.PHONE, PiiType.BANK_ACCOUNT, PiiType.ROUTING_NUMBER,
    PiiType.STREET, PiiType.CITY_STATE, PiiType.POSTAL_CODE, PiiType.ADDRESS,
    PiiType.EMPLOYEE_ID, PiiType.POLICY_NUMBER, PiiType.MEMBER_ID,
}

#: A propagated fragment must be at least this long to be trusted on its own.
MIN_PROPAGATION_LENGTH = 4

#: Words that are never a person even when they sit in a name field.
NOT_A_NAME = {
    "the", "and", "or", "of", "for", "same", "none", "n/a", "na", "self",
    "spouse", "taxpayer", "joint", "both", "see", "attached", "various",
}


@dataclass
class KnownEntity:
    """A value this document is established to be about."""

    pii_type: PiiType
    value: str
    confidence: float
    reason: str
    aliases: set[str] = field(default_factory=set)

    @property
    def all_forms(self) -> set[str]:
        return {self.value} | self.aliases


# --------------------------------------------------------------------------- #
# compound names
# --------------------------------------------------------------------------- #


def split_compound_name(text: str) -> list[tuple[str, int, int]]:
    """Split "John & Mary Smith" into its parts with offsets into `text`.

    Returns [(part, start, end), ...]. A single name returns one entry. The
    separator itself is never included, so "&" survives into the output and the
    line still reads as a joint field.
    """
    stripped = text.strip()
    if not stripped:
        return []
    parts: list[tuple[str, int, int]] = []
    cursor = 0
    for chunk in CONJUNCTION.split(text):
        if not chunk.strip():
            continue
        start = text.index(chunk, cursor)
        end = start + len(chunk)
        lead = len(chunk) - len(chunk.lstrip())
        trail = len(chunk) - len(chunk.rstrip())
        parts.append((chunk.strip(), start + lead, end - trail))
        cursor = end
    if len(parts) < 2:
        return parts

    # "John & Mary Smith": the surname belongs to both. Record it as an alias
    # rather than rewriting the span, so redaction geometry stays exact.
    return parts


def shared_surname(parts: list[tuple[str, int, int]]) -> Optional[str]:
    if len(parts) < 2:
        return None
    last = NAME_SUFFIX.sub("", parts[-1][0]).strip()
    tokens = last.split()
    if len(tokens) < 2:
        return None
    first_side = parts[0][0].split()
    if len(first_side) >= 2:
        return None  # both sides already carry a surname
    return tokens[-1]


def looks_like_a_name_part(text: str) -> bool:
    stripped = text.strip(" .,;:")
    if not stripped or len(stripped) > 40:
        return False
    if stripped.lower() in NOT_A_NAME:
        return False
    if any(ch.isdigit() or ch == "@" for ch in stripped):
        return False
    return any(ch.isalpha() for ch in stripped)


def expand_compound_names(
    candidates: list[Candidate], groups: list[LogicalFieldGroup]
) -> list[Candidate]:
    """Replace a single span over "JP & ML" with one span per person."""
    person_lines: set[tuple[int, int, int]] = set()
    for group in groups:
        if PiiType.PERSON not in group.expected_types:
            continue
        for line in group.value_lines:
            person_lines.add(line.key())

    out: list[Candidate] = []
    for candidate in candidates:
        is_person_field = (
            candidate.pii_type is PiiType.PERSON or candidate.line.key() in person_lines
        )
        if not is_person_field or not CONJUNCTION.search(candidate.text):
            out.append(candidate)
            continue

        parts = split_compound_name(candidate.text)
        usable = [p for p in parts if looks_like_a_name_part(p[0])]
        if len(usable) < 2:
            out.append(candidate)
            continue

        surname = shared_surname(usable)
        for value, start, end in usable:
            absolute_start = candidate.start + start
            absolute_end = candidate.start + end
            rect = candidate.line.rect_for(absolute_start, absolute_end)
            if rect is None:
                continue
            evidence = list(candidate.evidence)
            evidence.append(
                Evidence(Source.COVERAGE, "one side of a joint/compound name field", 0.0)
            )
            if surname and len(value.split()) == 1:
                evidence.append(
                    Evidence(Source.COVERAGE, f"shares the surname {surname!r}", 0.0)
                )
            out.append(
                Candidate(
                    pii_type=PiiType.PERSON,
                    text=value,
                    page_no=candidate.page_no,
                    rect=rect,
                    line=candidate.line,
                    start=absolute_start,
                    end=absolute_end,
                    confidence=max(candidate.confidence, 0.7),
                    source=candidate.source,
                    evidence=evidence,
                    group_id=candidate.group_id,
                    needs_review=candidate.needs_review,
                    review_reason=candidate.review_reason,
                )
            )
    return out


# --------------------------------------------------------------------------- #
# who the document is about
# --------------------------------------------------------------------------- #

#: Label wording that marks a field as belonging to the document's subject.
PRIMARY_LABEL = re.compile(
    r"(taxpayer|your |spouse|filer|client|employee|patient|insured|member|"
    r"applicant|borrower|owner|responsible party|account holder)",
    re.I,
)


def identify_subjects(
    candidates: list[Candidate], groups: list[LogicalFieldGroup]
) -> list[KnownEntity]:
    """Work out who the document is about, without asking the user.

    A value that sits in a field whose label names the subject - "Taxpayer
    name", "Your first name", "Spouse SSN" - is who this document belongs to.
    Those values are then trusted anywhere else they appear.
    """
    by_group = {g.group_id: g for g in groups}
    known: dict[tuple[PiiType, str], KnownEntity] = {}

    for candidate in candidates:
        if candidate.pii_type not in PROPAGATED_TYPES:
            continue
        value = candidate.normalized
        if len(value) < MIN_PROPAGATION_LENGTH:
            continue

        group = by_group.get(candidate.group_id) if candidate.group_id else None
        primary = bool(group and PRIMARY_LABEL.search(group.label.text))
        strong = candidate.confidence >= 0.85

        if not (primary or strong):
            continue

        key = (candidate.pii_type, value.lower())
        reason = "named in a subject field" if primary else "high-confidence detection"
        existing = known.get(key)
        if existing is None or candidate.confidence > existing.confidence:
            known[key] = KnownEntity(
                pii_type=candidate.pii_type,
                value=value,
                confidence=candidate.confidence,
                reason=reason,
            )

    # A person's surname alone identifies them later in the document.
    for entity in list(known.values()):
        if entity.pii_type is not PiiType.PERSON:
            continue
        tokens = [t for t in NAME_SUFFIX.sub("", entity.value).split() if len(t) >= 3]
        if len(tokens) >= 2:
            entity.aliases.add(tokens[-1])
            entity.aliases.add(" ".join(tokens))
    return list(known.values())


# --------------------------------------------------------------------------- #
# propagation
# --------------------------------------------------------------------------- #


def propagate(
    doc: Document, candidates: list[Candidate], entities: Iterable[KnownEntity]
) -> list[Candidate]:
    """Find every remaining occurrence of each known entity in the document."""
    entities = [e for e in entities if e.value]
    if not entities:
        return []

    covered: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
    for candidate in candidates:
        covered.setdefault(candidate.line.key(), []).append((candidate.start, candidate.end))

    patterns: list[tuple[KnownEntity, str, re.Pattern]] = []
    for entity in entities:
        for form in entity.all_forms:
            if len(form) < MIN_PROPAGATION_LENGTH:
                continue
            patterns.append(
                (
                    entity,
                    form,
                    re.compile(
                        rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])", re.I
                    ),
                )
            )

    found: list[Candidate] = []
    for page in doc.pages:
        for line in page.lines:
            existing = covered.get(line.key(), [])
            for entity, form, pattern in patterns:
                for match in pattern.finditer(line.text):
                    if any(match.start() < e and s < match.end() for s, e in existing):
                        continue
                    rect = line.rect_for(match.start(), match.end())
                    if rect is None:
                        continue
                    is_alias = form != entity.value
                    found.append(
                        Candidate(
                            pii_type=entity.pii_type,
                            text=match.group(),
                            page_no=page.number,
                            rect=rect,
                            line=line,
                            start=match.start(),
                            end=match.end(),
                            confidence=0.9 if not is_alias else 0.75,
                            source=Source.COVERAGE,
                            evidence=[
                                Evidence(
                                    Source.COVERAGE,
                                    f"same value as an entity {entity.reason}",
                                    0.9,
                                )
                            ],
                            needs_review=is_alias,
                            review_reason=(
                                "" if not is_alias else "matched on part of a known name"
                            ),
                        )
                    )
                    existing.append((match.start(), match.end()))
    return found
