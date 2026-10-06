"""Identity resolution: one person, company or identifier, however it is written.

A document names the same client many ways - "John Smith", "JOHN SMITH",
"Smith, John", "John R. Smith", "J. Smith", "Smith" - and the same SSN as
"123-45-6789" and "123456789". Matching exact strings (what Apply to All used to
do) finds some of them and quietly leaves the rest.

  EntityIndex     groups the candidates already found into identities, and records
                  WHY each form was linked (the evidence is part of the result).
  rescan_entity   searches the whole document for the identity's other
                  occurrences - including pages and lines that never produced a
                  candidate - and returns them as candidates for the unified gate.
  compose_replacement
                  turns one edited replacement ("Mark Santos") into the matching
                  form for each variant ("Santos, Mark", "MARK SANTOS", "Santos").

Short forms are the dangerous part. "Smith", "Lee" or "May" can be a surname, a
town, a company or an ordinary word, so a bare surname/first name is NEVER linked
or searched for on its own initiative: it needs evidence (it is already a
candidate and unambiguous in this document) or an explicit user choice. What the
rescan cannot decide it REPORTS as ambiguous instead of redacting.

Normalisation is for comparison only (see detection/textnorm.py); the original
characters and their exact geometry are what get redacted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..detection.protection import CandidateGate, ProtectionIndex, candidate_strength
from ..detection.textnorm import (
    PersonName, find_occurrences, fold, parse_person, same_person,
)
from ..detection.types import Candidate, Evidence, PiiType, Source
from .registry import EntityRegistry

_KEYER = EntityRegistry()
_DIGIT_RUN = re.compile(r"\d[\d\-\s.]{4,}\d")


@dataclass
class EntityProfile:
    entity_id: str
    pii_type: PiiType
    canonical: str
    #: folded form -> the spellings actually observed in the document
    forms: dict[str, set[str]] = field(default_factory=dict)
    #: one line per link: why this form belongs to this identity
    evidence: list[str] = field(default_factory=list)
    pages: set[int] = field(default_factory=set)
    candidate_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0

    def observed(self) -> list[str]:
        return sorted({spelling for spellings in self.forms.values() for spelling in spellings})


@dataclass
class ApplyResult:
    applied: list[Candidate] = field(default_factory=list)
    created: list[Candidate] = field(default_factory=list)
    ambiguous: list[Candidate] = field(default_factory=list)
    pages: set[int] = field(default_factory=set)
    entity: Optional[EntityProfile] = None

    @property
    def message(self) -> str:
        count, pages = len(self.applied), len(self.pages)
        base = (
            f"Applied to {count} linked occurrence{'s' if count != 1 else ''} "
            f"across {pages} page{'s' if pages != 1 else ''}."
        )
        if self.ambiguous:
            n = len(self.ambiguous)
            base += f" {n} additional ambiguous occurrence{'s were' if n != 1 else ' was'} left for review."
        return base


class _Union:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


class EntityIndex:
    def __init__(self, candidates: list[Candidate], profiles: list[EntityProfile], owner: dict[str, int]):
        self.candidates = candidates
        self.profiles = profiles
        self._owner = owner  # candidate id -> index into profiles

    # ------------------------------------------------------------------ build

    @classmethod
    def build(cls, candidates: list[Candidate]) -> "EntityIndex":
        n = len(candidates)
        uf = _Union(n)
        evidence: dict[int, list[str]] = {}

        def link(a: int, b: int, why: str) -> None:
            uf.union(a, b)
            evidence.setdefault(a, []).append(why)

        # -- identical identity: same type, same comparison key (digits-only for
        #    numeric identifiers, street synonyms, company suffix stripped, and
        #    case/accent/hyphen-insensitive for people).
        first_with_key: dict[tuple, int] = {}
        for i, c in enumerate(candidates):
            if c.pii_type is PiiType.UNCLASSIFIED_GROUP_VALUE:
                continue
            key = _KEYER.key_for(c.pii_type, c.text)
            if key in first_with_key:
                link(i, first_with_key[key], f"same {c.pii_type.value} value, written differently")
            else:
                first_with_key[key] = i

        # -- people: full names that are compatible are one person.
        parsed = {i: parse_person(c.text) for i, c in enumerate(candidates) if c.pii_type is PiiType.PERSON}
        full = [i for i, p in parsed.items() if p.is_full]
        for a_pos, a in enumerate(full):
            for b in full[a_pos + 1:]:
                why = same_person(parsed[a], parsed[b])
                if why:
                    link(a, b, f"'{candidates[a].text}' and '{candidates[b].text}': {why}")

        # -- short forms need evidence. A bare surname or first name joins an
        #    identity only when exactly ONE full-name identity in this document
        #    owns it AND it is not just a weak guess. Otherwise it stays on its own.
        roots_of_full = {uf.find(i) for i in full}
        for i, p in parsed.items():
            if p.is_full or not (p.surname or p.given):
                continue
            token = fold(p.surname or " ".join(p.given))
            owners = {
                uf.find(j) for j in full
                if fold(parsed[j].surname) == token or fold(parsed[j].first) == token
            }
            # A guess the pipeline itself marked ambiguous (low confidence, UNRESOLVED) is
            # exactly what must NOT be pulled into an identity by a later "apply to all".
            ambiguous = candidates[i].confidence < 0.5 or candidates[i].adjudication == "UNRESOLVED"
            if len(owners) == 1 and candidate_strength(candidates[i]) != "weak" and not ambiguous:
                j = next(j for j in full if uf.find(j) in owners)
                role = "surname" if fold(parsed[j].surname) == token else "given name"
                link(i, j, f"'{candidates[i].text}' is the only {role} '{token}' in this document, "
                           "and it was detected on its own")
        del roots_of_full

        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(uf.find(i), []).append(i)

        profiles: list[EntityProfile] = []
        owner: dict[str, int] = {}
        for number, members in enumerate(groups.values(), start=1):
            members_c = [candidates[i] for i in members]
            pii_type = members_c[0].pii_type
            canonical = max((c.text.strip() for c in members_c), key=lambda s: (len(s.split()), len(s)))
            forms: dict[str, set[str]] = {}
            for c in members_c:
                forms.setdefault(fold(c.text), set()).add(" ".join(c.text.split()))
            profile = EntityProfile(
                entity_id=f"E{number}",
                pii_type=pii_type,
                canonical=canonical,
                forms=forms,
                evidence=[w for i in members for w in evidence.get(i, [])],
                pages={c.page_no for c in members_c},
                candidate_ids=[c.id for c in members_c],
                confidence=max(c.confidence for c in members_c),
            )
            profiles.append(profile)
            for c in members_c:
                owner[c.id] = len(profiles) - 1
        return cls(candidates, profiles, owner)

    # ------------------------------------------------------------------ query

    def entity_of(self, candidate: Candidate) -> Optional[EntityProfile]:
        position = self._owner.get(candidate.id)
        return self.profiles[position] if position is not None else None

    def linked(self, candidate: Candidate) -> list[Candidate]:
        profile = self.entity_of(candidate)
        if profile is None:
            return [candidate]
        ids = set(profile.candidate_ids)
        return [c for c in self.candidates if c.id in ids]


# ----------------------------------------------------------------------- rescan


def _person_forms(profile: EntityProfile, explicit: Iterable[str]) -> list[str]:
    """Spellings worth searching for: every FULL form observed, its reorderings,
    plus anything the user explicitly named. Never a bare surname on its own."""
    forms: set[str] = set(explicit)
    for observed in profile.observed():
        p = parse_person(observed)
        forms.add(observed)
        if p.is_full:
            given = " ".join(p.given)
            forms.update({f"{given} {p.surname}", f"{p.surname}, {given}",
                          f"{p.first} {p.surname}", f"{p.surname}, {p.first}"})
    return sorted(forms, key=lambda s: (-len(s), s))


def _overlaps(taken: list[tuple[tuple, int, int]], key: tuple, start: int, end: int) -> bool:
    return any(k == key and start < e and s < end for k, s, e in taken)


def fragments_inside(created: list[Candidate], existing: list[Candidate]) -> list[Candidate]:
    """Existing candidates that are only a PART of a newly found full form.

    A model or a rule often tags one word of a name ("ZOLTAN" in "VARGA, ZOLTAN").
    When the identity's whole form is found, the fragment is superseded by it, or the
    two would overlap and only half the name would be replaced."""
    out = []
    for new in created:
        for c in existing:
            if (c.line.key() == new.line.key() and new.start <= c.start and c.end <= new.end
                    and (c.start, c.end) != (new.start, new.end) and c.source is not Source.MANUAL
                    and c.source in (Source.NER, Source.COVERAGE, Source.AUDIT, Source.GROUP)
                    and c.pii_type in (new.pii_type, PiiType.PERSON, PiiType.CITY_STATE, PiiType.ADDRESS)):
                out.append(c)
    return out


def rescan_entity(
    document,
    profile: EntityProfile,
    candidates: list[Candidate],
    protection: Optional[ProtectionIndex] = None,
    explicit: Iterable[str] = (),
) -> tuple[list[Candidate], list[Candidate]]:
    """Find the identity's occurrences that are not yet candidates.

    Returns (created, ambiguous). `created` are confirmed occurrences, already
    passed through the unified gate. `ambiguous` are short-form occurrences left
    for the reviewer - possible, but not safe to redact on a guess.
    """
    # Spans that BLOCK a new match: anything overlapping it, except a fragment that
    # lies wholly inside it (that is superseded - see fragments_inside).
    spans = [(c.line.key(), c.start, c.end, c) for c in candidates]
    taken = [(k, s, e) for k, s, e, _c in spans]
    created: list[Candidate] = []

    def blocked(key, start, end) -> bool:
        for k, s, e, c in spans:
            if k == key and start < e and s < end:
                if start <= s and e <= end and fragments_inside(
                        [Candidate(profile.pii_type, "", 0, (0, 0, 0, 0), c.line, start, end, 0, Source.COVERAGE)], [c]):
                    continue
                return True
        return False

    def make(line, start, end, form: str, why: str, confidence: float, source=Source.COVERAGE) -> Optional[Candidate]:
        rect = line.rect_for(start, end)
        if rect is None:
            return None
        return Candidate(
            pii_type=profile.pii_type, text=line.text[start:end], page_no=line.page_no, rect=rect,
            line=line, start=start, end=end, confidence=confidence, source=source,
            evidence=[Evidence(source, why, confidence)],
        )

    if profile.pii_type is PiiType.PERSON:
        forms = _person_forms(profile, explicit)
    else:
        forms = sorted({f for f in profile.observed()} | set(explicit), key=lambda s: (-len(s), s))
    numeric = profile.pii_type in EntityRegistry.DIGIT_KEYED
    wanted_digits = re.sub(r"\D", "", profile.canonical) if numeric else ""

    for page in document.pages:
        for line in page.lines:
            hits: list[tuple[int, int, str]] = []
            if numeric and wanted_digits:
                for match in _DIGIT_RUN.finditer(line.text):
                    if re.sub(r"\D", "", match.group()) == wanted_digits:
                        hits.append((match.start(), match.end(), match.group()))
            else:
                for form in forms:
                    hits.extend((s, e, form) for s, e in find_occurrences(line.text, form))
            for start, end, form in sorted(hits, key=lambda h: (h[0], -(h[1] - h[0]))):
                if blocked(line.key(), start, end) or _overlaps(
                        [t for t in taken if t not in [(k, s, e) for k, s, e, _c in spans]], line.key(), start, end):
                    continue
                candidate = make(
                    line, start, end, form,
                    f"entity rescan: '{form}' is a known form of {profile.entity_id} ({profile.canonical})", 0.8,
                )
                if candidate is not None:
                    created.append(candidate)
                    taken.append((line.key(), start, end))
                    spans.append((line.key(), start, end, candidate))

    if protection is not None and created:
        created, _log = CandidateGate(protection).run(created)

    ambiguous: list[Candidate] = []
    if profile.pii_type is PiiType.PERSON:
        shorts: set[str] = set()
        for observed in profile.observed():
            p = parse_person(observed)
            if p.is_full:
                shorts.update(t for t in (p.surname, p.first) if len(fold(t)) >= 3)
        from ..detection.heuristics import FORM_VOCABULARY

        for page in document.pages:
            for line in page.lines:
                for token in sorted(shorts):
                    if fold(token) in FORM_VOCABULARY:
                        continue
                    for start, end in find_occurrences(line.text, token):
                        if _overlaps(taken, line.key(), start, end) or len(ambiguous) >= 20:
                            continue
                        rect = line.rect_for(start, end)
                        if rect is None:
                            continue
                        ambiguous.append(Candidate(
                            pii_type=PiiType.PERSON, text=line.text[start:end], page_no=line.page_no,
                            rect=rect, line=line, start=start, end=end, confidence=0.35,
                            source=Source.COVERAGE,
                            evidence=[Evidence(Source.COVERAGE, f"possible short form of {profile.canonical}", 0.35)],
                            needs_review=True,
                            review_reason=(
                                f"may be a short form of {profile.canonical}, or an unrelated word - "
                                "accept it only if it is the same person"
                            ),
                            adjudication="UNRESOLVED",
                        ))
                        taken.append((line.key(), start, end))
        if protection is not None and ambiguous:
            ambiguous, _log = CandidateGate(protection).run(ambiguous)
    return created, ambiguous


# ------------------------------------------------------------------ replacements


def compose_replacement(edited: str, observed: str, context: Iterable[str] = ()) -> str:
    """One edited replacement, rendered the way `observed` was written.

    edited "Mark Santos":  "John Smith" -> "Mark Santos"   "Smith, John" -> "Santos, Mark"
                           "JOHN SMITH" -> "MARK SANTOS"    "Smith" -> "Santos"
                           "J. Smith" -> "M. Santos"        "John" -> "Mark"
    """
    new, old = parse_person(edited), parse_person(observed)
    if not new.is_full:
        return edited
    if old.is_full:
        first_old = old.first
        first = f"{new.first[0]}." if len(fold(first_old)) == 1 else new.first
        rest = [" ".join(new.given[1:])] if new.given[1:] and len(old.given) > 1 else []
        given = " ".join([first] + rest)
        text = f"{new.surname}, {given}" if old.surname_first else f"{given} {new.surname}"
    elif old.surname and not old.given:
        # A lone token is a surname or a first name; only the identity's OTHER known
        # forms can say which ("John" beside "John Smith" is a first name).
        token = fold(old.surname)
        known = [parse_person(c) for c in context]
        if any(p.is_full and fold(p.first) == token for p in known) and not any(
            p.is_full and fold(p.surname) == token for p in known
        ):
            text = new.first
        else:
            text = new.surname
    else:
        text = new.first
    if observed.isupper():
        return text.upper()
    return text
