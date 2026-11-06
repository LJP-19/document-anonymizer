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
    PiiType.PERSON, PiiType.ORG_PRIVATE, PiiType.SSN, PiiType.ITIN, PiiType.EIN, PiiType.TIN,
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
        household = " ".join(part[0] for part in usable).strip() if surname else ""
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
                    household=household,
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
        if getattr(candidate, "ambiguous", False) and not candidate.group_id:
            # Held for review is not confirmed: a guess must never become a
            # subject and spread to every page as an applied replacement.
            continue
        value = candidate.normalized
        group = by_group.get(candidate.group_id) if candidate.group_id else None
        if len(value) < MIN_PROPAGATION_LENGTH and not _short_name_ok(candidate, group):
            continue
        if not _subject_shape_ok(candidate, group):
            continue

        # A label naming the value's OWN TYPE at all - groups.py already
        # recognised it as a PERSON/SSN/etc. field - is a stronger, more
        # general signal than matching a hand-curated word list. Depending
        # only on PRIMARY_LABEL meant a document whose actual label wording
        # ("Policyholder", "Name", "Insured Party"...) wasn't on that list
        # never qualified ANYTHING as a subject, so propagation silently
        # never ran at all - regardless of how many pages repeated the value.
        typed_field = bool(
            group and candidate.pii_type in group.expected_types
            and not group.label.non_pii
        )
        primary = typed_field or bool(group and PRIMARY_LABEL.search(group.label.text))
        # Strict PII-only (0.41.0): only a LABEL or a deterministic identifier
        # rule makes a subject. A confident model guess used to qualify too, and a
        # wrong one ("Hunter", a fund name) then spread to every page as a
        # confirmed entity.
        strong = candidate.confidence >= 0.85 and candidate.source is Source.REGEX

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

    # A person's surname alone identifies them later in the document, and the
    # same name is often written slightly differently elsewhere on a form.
    for entity in list(known.values()):
        if entity.pii_type is not PiiType.PERSON:
            continue
        tokens = [t for t in NAME_SUFFIX.sub("", entity.value).split() if len(t.strip(".")) >= 2]
        if len(tokens) >= 2:
            entity.aliases.add(tokens[-1])
            entity.aliases.add(" ".join(tokens))
            # The given name alone identifies the person too ("Ann" for "Ann
            # Tash"), once the full name is established. Surname-first values
            # ("Tash, Ann") put it last. Each occurrence is still validated in
            # propagate() and an uncertain one is only reported.
            surname_first = "," in entity.value
            given = tokens[1] if surname_first else tokens[0]
            if given.strip(".,").isalpha() and len(given.strip(".,")) >= 2:
                entity.aliases.add(given.strip(".,"))
        entity.aliases.update(spelling_variants(entity.value))
    return list(known.values())


# --------------------------------------------------------------------------- #
# short names (2-3 letters)
# --------------------------------------------------------------------------- #

#: A 2-3 letter value is only a name when the document says so. MIN_PROPAGATION_LENGTH
#: kept "Smith"-shaped names safe from noise, but silently excluded every short
#: surname - Li, Wu, Ng, Lim, Kim, Ho - from identity tracking altogether.
#: Measured on real returns: a 3-letter surname, label-confirmed on page 1, stayed
#: readable on dozens of later schedules. These are the guards that replace the
#: blanket length cut-off for PERSON tokens.
_STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in", "ia", "ks",
    "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny",
    "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc", "pr", "us", "usa",
}


def _subject_shape_ok(candidate: Candidate, group) -> bool:
    """A value is trusted document-wide only if it LOOKS like what it is typed.

    Propagation multiplies a mistake by every page: a sentence or a label
    fragment bound under a name label ("relationship", "to you", "award from
    the IRS for information you provided ...") became a known "person" and was
    redacted wherever its words recurred. A PERSON subject must be name-shaped
    (or a label-confirmed short name); nothing made only of form words is a
    subject; a digit-defined type must have digits.
    """
    from .groups import _DIGIT_TYPES, is_placeholder_value
    from .heuristics import FORM_VOCABULARY, looks_like_a_lone_surname, looks_like_person

    value = candidate.normalized
    words = [w.strip(".,;:()'\u2019").lower() for w in value.split()]
    if not words or is_placeholder_value(value):
        return False
    if all(w in FORM_VOCABULARY or w in _SHORT_ENGLISH for w in words if w):
        return False
    if candidate.pii_type.value in _DIGIT_TYPES and not any(ch.isdigit() for ch in value):
        return False
    if candidate.pii_type is PiiType.PERSON:
        if len(value) < MIN_PROPAGATION_LENGTH:
            return _short_name_ok(candidate, group)
        tokens = [t for t in value.split() if t not in ("&", "+", "and", "AND", "And", ",")]
        return looks_like_person(value)[0] or (bool(tokens) and all(
            _short_token_shape_ok(t) or looks_like_person(t)[0] or looks_like_a_lone_surname(t)
            or (len(t.strip(".")) == 1 and t[:1].isalpha())          # middle initial
            for t in tokens
        ))
    return True


def _short_token_shape_ok(token: str) -> bool:
    from .heuristics import FORM_VOCABULARY

    bare = token.strip(".,;:")
    return (
        2 <= len(bare) <= 3
        and bare.isalpha()
        and bare.lower() not in _STATE_CODES
        and bare.lower() not in FORM_VOCABULARY
        and bare.lower() not in _SHORT_ENGLISH
    )


#: Two- and three-letter English words that are also capitalised on forms
#: ("You", "Not", "Who", "Tax"). A short name must never be one of these:
#: measured, a checkbox caption "You" bound as a value became a known "person"
#: and was propagated onto every sentence-initial "You" in a return.
_SHORT_ENGLISH = {
    "you", "your", "yes", "not", "who", "how", "why", "all", "any", "are", "was", "has", "had",
    "her", "him", "his", "its", "our", "out", "own", "per", "the", "and", "but", "for", "nor",
    "yet", "too", "use", "see", "may", "can", "did", "get", "got", "let", "new", "old", "one",
    "two", "six", "ten", "tax", "net", "sum", "add", "box", "end", "day", "due", "pay", "fee",
    "job", "age", "law", "row", "set", "top", "way", "act", "aid", "air", "art", "car", "cut",
    "him", "it", "is", "in", "on", "of", "or", "to", "by", "an", "as", "at", "be", "do", "go",
    "he", "if", "me", "my", "no", "so", "up", "us", "we", "am", "id", "mo", "yr", "pt", "ln",
    "jr", "sr", "ii", "iv", "md", "dds", "cpa", "esq", "phd", "inc", "llc", "ltd", "co", "corp",
}


def _short_name_ok(candidate: Candidate, group) -> bool:
    """A short PERSON value is a subject only when its own field label says so."""
    if candidate.pii_type is not PiiType.PERSON or group is None:
        return False
    if group.label.non_pii or PiiType.PERSON not in group.expected_types:
        return False
    tokens = candidate.normalized.split()
    return bool(tokens) and all(_short_token_shape_ok(t) or len(t) >= MIN_PROPAGATION_LENGTH for t in tokens)


def _short_form_ok(entity: "KnownEntity", form: str) -> bool:
    return entity.pii_type is PiiType.PERSON and " " not in form.strip() and _short_token_shape_ok(form)


def _short_match_ok(text: str) -> bool:
    """The occurrence itself must be written as a name: Capitalised or ALL CAPS.
    "li" inside a sentence, or a lowercase word, is not the person."""
    return text[:1].isupper() and (text.isupper() or text[1:].islower())


#: A machine-read scanline: tokens are mostly digits ("CA LIM  30 0 202212 430").
def _code_line(text: str) -> bool:
    tokens = text.split()
    if len(tokens) < 4:
        return False
    numeric = sum(1 for t in tokens if t.isdigit())
    return numeric / len(tokens) >= 0.5


def name_control_hits(doc: Document, entities: Iterable["KnownEntity"], covered: dict) -> list[Candidate]:
    """The IRS "name control" - the first four letters of a filer's surname -
    printed on payment-voucher scanlines. It is a fragment of the surname, so
    it is the person; but it is not the surname, so propagation never matched it.
    Only looked for on code-like lines (mostly numeric tokens), where a 2-4
    letter uppercase token has no other plausible reading."""
    controls: dict[str, KnownEntity] = {}
    for entity in entities:
        if entity.pii_type is not PiiType.PERSON:
            continue
        tokens = [t for t in NAME_SUFFIX.sub("", entity.value).split() if t.strip(".").isalpha() or "-" in t]
        if len(tokens) < 2:
            continue
        letters = "".join(ch for ch in tokens[-1] if ch.isalpha()).upper()
        if len(letters) >= 2:
            controls.setdefault(letters[:4], entity)
    if not controls:
        return []
    found: list[Candidate] = []
    for page in doc.pages:
        for line in page.lines:
            if not _code_line(line.text):
                continue
            existing = covered.get(line.key(), [])
            for match in re.finditer(r"(?<![A-Z0-9])[A-Z]{2,4}(?![A-Z0-9])", line.text):
                entity = controls.get(match.group())
                if entity is None or any(match.start() < e and s < match.end() for s, e in existing):
                    continue
                rect = line.rect_for(match.start(), match.end())
                if rect is None:
                    continue
                found.append(Candidate(
                    pii_type=PiiType.PERSON, text=match.group(), page_no=page.number, rect=rect,
                    line=line, start=match.start(), end=match.end(), confidence=0.75,
                    source=Source.COVERAGE,
                    evidence=[Evidence(Source.COVERAGE, "name control (first letters of a known surname) on a scanline", 0.75)],
                    needs_review=True,
                    review_reason="the first letters of a known surname, printed on a machine-read line",
                ))
    return found


def _after_known_components(text: str, end: int, components: set) -> str:
    """The text after a name match, skipping the person's OTHER name words.

    "Marcus" in "Marcus Feldman Manufacturing Inc." is followed by the known
    surname and then the business suffix: the occurrence belongs to the
    business name exactly as the full name does, and the given-name alias must
    not take it over once the full name was (correctly) refused there.
    """
    from .textnorm import fold

    rest = text[end:]
    while True:
        m = re.match(r"\s+([^\W\d_][\w'\u2019\-]*)", rest)
        if not m or fold(m.group(1)) not in components:
            return rest
        rest = rest[m.end():]


_TITLES = {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "rev", "hon", "sir", "madam"}
_JOINERS = {"and", "&", "or", "y", "et"}


def _component_context_is_ambiguous(text: str, start: int, end: int, components: set) -> bool:
    """Is a lone name component in a context that may mean someone or something else?

    "Tash agreed" / "Mr. Tash" / "Tash's account" / "Tash and Tash" are the person.
    "Corin River Trail", "TASH FAMILY TRUST", "Smith Barney" are not safely the
    person: the FOLLOWING capitalised word makes it part of another name. A word written in lower case on a mixed-case line ("a tash of")
    is an ordinary word. Only the immediately adjacent word, separated by ONE
    space, counts - a wider gap is a table column, not part of the name.
    """
    from .textnorm import fold

    matched = text[start:end]
    if matched.islower() and any(ch.isupper() for ch in text):
        return True
    after = re.match(r" ([^\W\d_][\w'\u2019\-]*)", text[end:])
    if after:
        word = after.group(1)
        bare = re.sub(r"['\u2019]s$", "", word)
        if word[:1].isupper() and fold(bare) not in components and fold(bare) not in _JOINERS:
            return True
    # The word BEFORE is not used: sentences start with capitals ("Call John
    # tomorrow"), and a real other person's full name ("Bob Tash") is already a
    # model/rule candidate covering this spot, which keeps propagation off it.
    return False


def spelling_variants(name: str) -> set[str]:
    """Common ways the same name is written on different parts of a form.

    Hyphenation and middle initials vary between fields far more often than
    they stay identical, and an exact-match propagation misses every variant.
    """
    cleaned = NAME_SUFFIX.sub("", name).strip()
    variants: set[str] = set()
    if not cleaned:
        return variants

    if "-" in cleaned:
        variants.add(cleaned.replace("-", " "))
        variants.add(cleaned.replace("-", ""))
    if " " in cleaned:
        tokens = cleaned.split()
        # Drop a middle initial: "Maria T Gonzalez" -> "Maria Gonzalez".
        without_initials = [t for t in tokens if not (len(t.strip(".")) == 1)]
        if 2 <= len(without_initials) < len(tokens):
            variants.add(" ".join(without_initials))
        # Surname-first, as tables and indexes often write it.
        if len(tokens) >= 2:
            variants.add(f"{tokens[-1]}, {' '.join(tokens[:-1])}")

    return {v for v in variants if len(v) >= MIN_PROPAGATION_LENGTH and v != cleaned}


# --------------------------------------------------------------------------- #
# propagation
# --------------------------------------------------------------------------- #


#: Immediately following one of these is strong evidence the matched token
#: is the START of a DIFFERENT entity's name - a business, not the known
#: person - not another occurrence of them. "LJP" propagated against "LJP
#: Manufacturing Inc." must not blindly retype "LJP" as the person and leave
#: "Manufacturing Inc." dangling; the spec's own canonical example for this
#: feature is exactly this case. Checked only for PERSON entities - an SSN
#: or address has no equivalent "this might be a different thing" signal
#: from what follows it.
ORG_SUFFIX_AFTER = re.compile(
    r"^\s*[,.]?\s*"
    r"(inc\.?|llc\.?|l\.l\.c\.?|corp\.?|corporation|company|co\.?|ltd\.?|"
    r"limited|lp\.?|llp\.?|manufacturing|industries|group|partners|"
    r"associates|enterprises|holdings|ventures|solutions|systems)\b",
    re.I,
)


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

    from .textnorm import fold

    components = {
        fold(t.strip(".,"))
        for e in entities if e.pii_type is PiiType.PERSON
        for form in e.all_forms for t in form.replace(",", " ").split()
    }
    patterns: list[tuple[KnownEntity, str, re.Pattern]] = []
    for entity in entities:
        for form in entity.all_forms:
            if len(form) < MIN_PROPAGATION_LENGTH and not _short_form_ok(entity, form):
                continue
            patterns.append(
                (
                    entity,
                    form,
                    re.compile(rf"(?<!\w){re.escape(form)}(?!\w)", re.I),
                )
            )

    # Longest form first. The forms came from a SET, so the order - and therefore
    # whether "JOHN SMITH" matched whole or was claimed piecemeal by the aliases
    # "JOHN" and "SMITH" - varied from one run to the next on identical input.
    patterns.sort(key=lambda item: (-len(item[1]), item[1].lower()))

    found: list[Candidate] = []
    for page in doc.pages:
        for line in page.lines:
            existing = covered.get(line.key(), [])
            for entity, form, pattern in patterns:
                for match in pattern.finditer(line.text):
                    if any(match.start() < e and s < match.end() for s, e in existing):
                        continue
                    if entity.pii_type is PiiType.PERSON and ORG_SUFFIX_AFTER.match(
                        _after_known_components(line.text, match.end(), components)[:20]
                    ):
                        # "LJP Manufacturing Inc." - do not transform the
                        # known person's name into the start of a different
                        # entity's name, per the spec's own example. Never
                        # propagated here at all, not merely demoted -
                        # treating this token as the same person would be
                        # wrong regardless of confidence.
                        continue
                    if len(form) < MIN_PROPAGATION_LENGTH and not _short_match_ok(match.group()):
                        continue
                    rect = line.rect_for(match.start(), match.end())
                    if rect is None:
                        continue
                    is_alias = form != entity.value or len(form) < MIN_PROPAGATION_LENGTH
                    # A bare surname or first name of a known person is redacted too, and
                    # maps through the same per-name pseudonym ("John Smith" -> "Tony Stark",
                    # "Smith" -> "Stark"). Owner's rule: once the full name is known, every
                    # occurrence of its parts goes. The org-suffix guard above ("Smith Inc.")
                    # and the protected-text gate still apply; it is flagged so a reviewer
                    # can keep the odd one ("Smith & Wesson").
                    ambiguous = bool(
                        entity.pii_type is PiiType.PERSON and " " not in form.strip()
                        and _component_context_is_ambiguous(line.text, match.start(), match.end(), components)
                    )
                    found.append(
                        Candidate(
                            pii_type=entity.pii_type,
                            text=match.group(),
                            page_no=page.number,
                            rect=rect,
                            line=line,
                            start=match.start(),
                            end=match.end(),
                            confidence=(0.4 if ambiguous else 0.9 if not is_alias else 0.75),
                            source=Source.COVERAGE,
                            evidence=[
                                Evidence(
                                    Source.COVERAGE,
                                    "bare surname matches a known name - ambiguous without more context"
                                    if ambiguous
                                    else f"same value as an entity {entity.reason}",
                                    0.4 if ambiguous else 0.9,
                                )
                            ],
                            needs_review=is_alias,
                            review_reason=(
                                "" if not is_alias else
                                "may be an unrelated person, company or word that shares this name - "
                                "accept it only if it is the same person" if ambiguous
                                else "matched on part of a known name"
                            ),
                            ambiguous=ambiguous,
                        )
                    )
                    existing.append((match.start(), match.end()))
    found.extend(name_control_hits(doc, entities, covered))
    return found
