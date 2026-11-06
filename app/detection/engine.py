"""Detection orchestration (spec sections 10, 18, 19, 23, 24)."""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..document.model import Document
from .deterministic import RuleSet, detect_deterministic, load_rules
from .auditor import adjudicate_document, audit_document, check_types
from .entities_pass import expand_compound_names, identify_subjects, propagate
from .form_text import detect_form_text, veto_form_text
from .gliner import detect_gliner
from .groups import analyse_coverage, build_groups
from .heuristics import detect_heuristics
from .ner import detect_ner
from .resolve import resolve_overlaps, strip_label_overlaps, veto_by_label_geometry
from .protection import CandidateGate, ProtectionIndex, apply_ai_objections, drop_prior_replacements, hold_unconfirmed_proposals
from .types import Candidate, DetectionResult, Evidence, PiiType, Source

log = logging.getLogger(__name__)


def _veto(candidates: list, negatives: list, groups: Optional[list] = None) -> list:
    """Drop candidates the model confidently called money, a form number or a job title.

    This is the direct fix for redacting figures and business facts: a
    deterministic rule can fire on a number, but a model that says "that is a
    dollar amount" outranks it.

    One exception: a value that sits in a field whose LABEL names a kind of identity
    ("Dependent:", "Spouse:") is kept and flagged. A model reading an unfamiliar name
    may call it a job title; the label is the better witness, and silently deleting a
    label-bound value is the failure the project rules forbid.
    """
    if not negatives:
        return candidates
    bound = {
        g.group_id for g in (groups or ())
        if not g.label.non_pii and not g.label.unknown and g.expected_types
    }
    by_page: dict[int, list] = {}
    for page_no, rect, label in negatives:
        by_page.setdefault(page_no, []).append((rect, label))

    kept = []
    for candidate in candidates:
        blocked_by = None
        for rect, label in by_page.get(candidate.page_no, []):
            cx0, cy0, cx1, cy1 = candidate.rect
            nx0, ny0, nx1, ny1 = rect
            # Candidate sits inside a region the model called non-PII.
            if cx0 >= nx0 - 1 and cx1 <= nx1 + 1 and cy0 >= ny0 - 1 and cy1 <= ny1 + 1:
                blocked_by = label
                break
        if blocked_by is None:
            kept.append(candidate)
        elif candidate.group_id in bound or candidate.source is Source.MANUAL:
            candidate.needs_review = True
            candidate.review_reason = candidate.review_reason or (
                f"the model read this as a {blocked_by}, but its field label says it is "
                f"{candidate.pii_type.value.lower().replace('_', ' ')} - check it"
            )
            kept.append(candidate)
    return kept


def _type_unclassified_group_values(candidates: list) -> list:
    """A value under an unrecognised label that is shaped like a person's name is
    a PERSON, on its own evidence.

    The field-group layer already judged the value identity-bearing, but could not
    say WHAT, so it emitted UNCLASSIFIED_GROUP_VALUE - which waits, unapplied,
    until a user names it. A name that no model happened to tag (an unfamiliar or
    non-English one) therefore went unredacted: measured, "Spouse: Željko
    Petrović" was left alone while "Spouse: John Smith" was caught only because
    the small spaCy model tagged it. Name shape is judged by character class in
    any script, never by a dictionary (heuristics.looks_like_person); the result
    is flagged for review, with the reason on the candidate.
    """
    from dataclasses import replace

    from .heuristics import looks_like_person

    out = []
    for c in candidates:
        if c.pii_type is PiiType.UNCLASSIFIED_GROUP_VALUE and c.source is Source.GROUP:
            is_name, shape_confidence = looks_like_person(c.text)
            if is_name:
                c = replace(
                    c,
                    pii_type=PiiType.PERSON,
                    confidence=min(max(c.confidence, shape_confidence), 0.7),
                    needs_review=True,
                    review_reason=c.review_reason or "typed as a name by its shape, under an unrecognised label",
                    evidence=list(c.evidence) + [
                        Evidence(Source.GROUP, "name-shaped value under an unrecognised label", 0.6)
                    ],
                )
        out.append(c)
    return out


def _drop_label_text(candidates: list, labels: list) -> list:
    """Never redact the form's own words.

    Deterministic, and deliberately not left to the model: a label was being
    pseudonymized in builds where the audit model was absent, and a check that
    only works when a 1.1 GB optional component is installed is not a check.

    Three things are dropped: a value identical to a detected label, a value
    made entirely of form vocabulary, and a value that is only punctuation.
    """
    from .heuristics import FORM_VOCABULARY

    label_texts = {
        " ".join(label.text.split()).strip(" :.").lower()
        for label in labels
        if label.text.strip()
    }

    kept = []
    for candidate in candidates:
        if candidate.source is Source.MANUAL:
            kept.append(candidate)          # the user said so
            continue

        value = " ".join(candidate.text.split()).strip(" :.")
        lowered = value.lower()
        if not value or not any(ch.isalnum() for ch in value):
            continue
        if lowered in label_texts:
            continue
        tokens = [tok.strip(".,:;()").lower() for tok in value.split()]
        tokens = [tok for tok in tokens if tok]
        if tokens and all(tok in FORM_VOCABULARY for tok in tokens):
            continue
        kept.append(candidate)
    return kept


def _drop_financial_values(candidates: list) -> list:
    """A currency amount is never PII, whatever any detector says.

    The model has been observed labelling "$85,000" as a date of birth at
    moderate confidence. Preserving financial facts is a hard requirement (spec
    sections 12 and 41), so this is a deterministic guard rather than a
    confidence threshold: a span that is entirely a money or percentage token is
    removed no matter which layer produced it.
    """
    from .deterministic import MONEY_RE, PERCENT_RE

    kept = []
    for candidate in candidates:
        stripped = candidate.text.strip()
        if stripped and (MONEY_RE.fullmatch(stripped) or PERCENT_RE.fullmatch(stripped)):
            continue
        # A span that merely OVERLAPS a currency amount is just as destructive.
        # "123" from a stacked address field matched inside "$123,456", because
        # "$" and "," are not word characters, and pseudonymising it corrupted
        # the figure. Any candidate touching a money token is dropped.
        line_text = candidate.line.text
        overlaps_money = any(
            candidate.start < match.end() and match.start() < candidate.end
            for match in list(MONEY_RE.finditer(line_text)) + list(PERCENT_RE.finditer(line_text))
        )
        if overlaps_money:
            continue
        kept.append(candidate)
    return kept


_US_STATES_FULL = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
}


def _drop_bare_states(candidates: list) -> list:
    """A state on its own identifies nobody (HIPAA Safe Harbor keeps it too).

    A model tagged the "CA" in a State box as a place and it was pseudonymised -
    on a return that changes which state's law applies. Only a candidate that is
    NOTHING but a state code or name is dropped; a city/state/ZIP line keeps its
    state inside the redacted value, and a manual addition always survives.
    """
    from .entities_pass import _STATE_CODES

    kept = []
    for c in candidates:
        bare = " ".join(c.text.split()).strip(" .,").lower()
        if c.source is not Source.MANUAL and (bare in _STATE_CODES or bare in _US_STATES_FULL):
            continue
        kept.append(c)
    return kept


_DATE_TOKEN = re.compile(r"^(0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])[-/]((?:19|20)\d{2})$")
_IDENTITY_TYPES = {PiiType.PERSON, PiiType.ADDRESS, PiiType.STREET, PiiType.SSN, PiiType.ITIN}


def _dates_beside_identity(doc, candidates: list) -> list:
    """A date of birth printed with no label, in the filer's identity block.

    Machine-readable state forms print the values only - name, street, ZIP and
    each filer's date of birth - with no caption at all, so the DOB rule's
    required "birth" cue can never be there. Measured on a real return: both
    spouses' dates of birth survived. A line made ONLY of dates whose years are
    birth-plausible, with no money on it, within a few lines of an identity
    value already found on the same page, is typed DOB and flagged for review.
    Transaction and asset dates are recent or sit in tables beside amounts.
    """
    import datetime

    from .deterministic import MONEY_RE

    latest = datetime.date.today().year - 14
    identity = {}
    for c in candidates:
        if c.pii_type in _IDENTITY_TYPES:
            identity.setdefault(c.page_no, []).append(c.rect)
    if not identity:
        return []
    covered = {(c.line.key(), c.start) for c in candidates}
    out = []
    for page in doc.pages:
        rects = identity.get(page.number)
        if not rects:
            continue
        for line in page.lines:
            tokens = line.text.split()
            if not tokens or len(tokens) > 3 or MONEY_RE.search(line.text):
                continue
            matches = [_DATE_TOKEN.match(t) for t in tokens]
            if not all(matches) or not all(1900 <= int(m.group(3)) <= latest for m in matches):
                continue
            y0, y1 = line.bbox[1], line.bbox[3]
            if not any(r[1] - 60 <= y0 and y1 <= r[3] + 60 for r in rects):
                continue
            pos = 0
            for tok in tokens:
                start = line.text.index(tok, pos)
                pos = start + len(tok)
                if (line.key(), start) in covered:
                    continue
                rect = line.rect_for(start, pos)
                if rect is None:
                    continue
                out.append(Candidate(
                    pii_type=PiiType.DOB, text=tok, page_no=page.number, rect=rect, line=line,
                    start=start, end=pos, confidence=0.7, source=Source.REGEX,
                    evidence=[Evidence(Source.REGEX, "birth-year date beside the filer's identity block", 0.7)],
                    needs_review=True,
                    review_reason="a date with a birth-year, printed with the filer's name and address",
                ))
    return out


_LANDMARK_WORDS = {
    "river", "trail", "park", "lake", "creek", "mountain", "mount", "bridge", "beach", "bay",
    "island", "airport", "station", "hall", "museum", "forest", "canyon", "falls", "harbor",
    "harbour", "reservoir", "monument", "memorial", "stadium", "arena", "plaza", "mall",
}
_COUNTRIES = {"u.s.", "us", "usa", "u.s.a.", "united states", "united states of america", "america",
              "canada", "mexico", "uk", "u.k.", "united kingdom"}


def _drop_implausible_model_hits(candidates: list, form_words: set = frozenset()) -> list:
    """Model-only proposals that cannot be what they are typed as.

    - a "place" of one or two letters ("MM" in "MM/DD/YYYY") or a country
      ("U.S.") identifies nobody;
    - a "person" on a machine-read code line (a voucher scanline) - only the
      name-control rule, which knows the surname, may act there.
    """
    from .entities_pass import _code_line

    kept = []
    for c in candidates:
        if c.source is Source.COVERAGE and c.group_id is None and c.pii_type in (PiiType.CITY_STATE, PiiType.STREET):
            bare = " ".join(c.text.split()).strip(" ,;:").lower()
            if not any(ch.isdigit() for ch in bare) and any(w.strip(".,") in _LANDMARK_WORDS for w in bare.split()):
                continue  # a second-pass "place" that is a public landmark
        if c.source is Source.NER and c.group_id is None:
            bare = " ".join(c.text.split()).strip(" ,;:").lower()
            if c.pii_type is PiiType.CITY_STATE and (len(bare.replace(".", "")) <= 2 or bare in _COUNTRIES):
                continue
            if (c.pii_type in (PiiType.CITY_STATE, PiiType.STREET) and not any(ch.isdigit() for ch in bare)
                    and any(w.strip(".,") in _LANDMARK_WORDS for w in bare.split())):
                continue  # "the Corin River Trail" is a public landmark, not an address
            if c.pii_type is PiiType.PERSON and c.line is not None and _code_line(c.line.text):
                continue
            token = c.text.strip(" .,;:()")
            if (c.pii_type in (PiiType.PERSON, PiiType.ORG_PRIVATE) and token.isalpha() and token.isupper()
                    and len(token) <= 3):
                # A lone all-caps code ("MFJ", "HOH", "IRA", "SEP") is a form
                # abbreviation, not a name. A short surname in a name FIELD
                # arrives through its label (group), which this does not touch.
                continue
            words = [w.strip(".,;:()[]'\"\u2019").lower() for w in c.text.split()]
            if (form_words and c.pii_type in (PiiType.PERSON, PiiType.ORG_PRIVATE)
                    and words and all(w in form_words for w in words if w)):
                continue  # the document's own printed vocabulary
        kept.append(c)
    return kept


def drop_form_words(candidates: list, form_words: set) -> list:
    """Person/organisation findings made only of this document's own printed
    words. Used for second-pass findings, which re-scan the output without
    layout and so re-propose the same chrome the main pass already rejected."""
    if not form_words:
        return candidates
    kept = []
    for c in candidates:
        words = [w.strip(".,;:()[]'\"\u2019").lower() for w in c.text.split()]
        if (c.source is not Source.MANUAL and c.pii_type in (PiiType.PERSON, PiiType.ORG_PRIVATE)
                and words and all(w in form_words for w in words if w)):
            continue
        kept.append(c)
    return kept


def _drop_numberless_model_streets(candidates: list) -> list:
    """A street address has a number. spaCy's FAC (building, bridge, trail,
    airport) is mapped to STREET, so "Jordan River Trail" in a sentence was
    redacted as somebody's address. A model-only STREET with no digit, not bound
    by a field label, is a public place and is dropped."""
    from .heuristics import looks_like_person

    kept = []
    for c in candidates:
        if (c.pii_type is PiiType.STREET and c.source in (Source.NER, Source.COVERAGE) and c.group_id is None
                and not any(ch.isdigit() for ch in c.text)
                and any(w.strip(".,").lower() in _STREET_WORDS for w in c.text.split())):
            continue
        if (c.pii_type is PiiType.STREET and c.source in (Source.NER, Source.COVERAGE) and c.group_id is None
                and not re.match(r"^\d{1,6}\s", c.text.strip())
                and not any(w.strip(".,()").lower() in _STREET_WORDS for w in c.text.split())
                and not looks_like_person(c.text.strip())[0]):
            continue  # a model "street" with no street word ("(540NR),") is not an address
        # A name-shaped "place" with no street word ("İpek Yılmaz" tagged FAC
        # beside "Main Street 4") is left for _retype_name_shaped_places, which
        # makes it a PERSON - dropping it here lost the name (caught by
        # test_unicode_names on the production model).
        kept.append(c)
    return kept


_STREET_WORDS = {
    "street", "st", "avenue", "ave", "road", "rd", "boulevard", "blvd", "drive", "dr", "lane", "ln",
    "court", "ct", "way", "circle", "cir", "place", "pl", "terrace", "ter", "parkway", "pkwy",
    "highway", "hwy", "trail", "trl", "path", "loop", "common", "cmn", "bridge", "tunnel", "park",
    "square", "sq", "plaza", "row", "walk", "crossing", "pass", "pike", "turnpike", "expressway",
    "freeway", "building", "tower", "center", "centre", "airport", "station", "hall", "mall",
}


_HONORIFIC = re.compile(r"^(?:dr|mr|mrs|ms|mx|prof|rev|hon|sr|sra|srta)\.?\s+", re.I)


_POSSESSIVE = re.compile(r"['\u2019][sS]?$")


def _trim_possessives(candidates: list) -> list:
    """"Quell's account": the name is "Quell"; the "'s" is grammar and stays.
    Left in the span it became a separate token in the name registry and the
    person got a SECOND synthetic surname."""
    from dataclasses import replace

    out = []
    for c in candidates:
        if c.pii_type is PiiType.PERSON and c.source is not Source.MANUAL:
            m = _POSSESSIVE.search(c.text)
            if m and m.start() > 1:
                end = c.start + m.start()
                rect = c.line.rect_for(c.start, end) if c.line is not None else None
                if rect is not None:
                    c = replace(c, text=c.text[: m.start()], end=end, rect=rect)
        out.append(c)
    return out


def _retype_known_name_components(candidates: list) -> list:
    """A registered person's name word that a MODEL typed as a place or company
    ("Torvin confirmed ..." tagged GPE) is that person: the field label that
    established the identity outranks a model's guess about one word."""
    from dataclasses import replace

    from .textnorm import fold

    known = set()
    for c in candidates:
        if c.pii_type is PiiType.PERSON and (c.group_id is not None or c.source in (Source.GROUP, Source.COVERAGE)):
            known.update(fold(t.strip(".,")) for t in c.text.split() if len(t.strip(".,")) >= 2)
    out = []
    for c in candidates:
        if (c.source is Source.NER and c.group_id is None
                and c.pii_type in (PiiType.CITY_STATE, PiiType.ORG_PRIVATE, PiiType.STREET)
                and " " not in c.text.strip() and fold(c.text.strip(".,")) in known):
            from .entities_pass import _component_context_is_ambiguous

            # The SAME context check propagation applies to a lone component:
            # "Torvin confirmed ..." is the person, "Torvin River Trail" may not be.
            unclear = c.line is not None and _component_context_is_ambiguous(c.line.text, c.start, c.end, known)
            c = replace(c, pii_type=PiiType.PERSON, ambiguous=unclear, needs_review=unclear,
                        review_reason=("a known person's name word in a context that may mean something else"
                                       if unclear else ""),
                        evidence=list(c.evidence) + [Evidence(Source.COVERAGE,
                                                              "a label-confirmed person's name word", 0.0)])
        out.append(c)
    return out


def _trim_honorifics(candidates: list) -> list:
    """"Dr." / "Mr." in front of a name is not identity; redacting it removes
    meaning ("Dr. X said") and protects nobody. The span starts at the name."""
    from dataclasses import replace

    out = []
    for c in candidates:
        if c.pii_type is PiiType.PERSON and c.source is not Source.MANUAL:
            m = _HONORIFIC.match(c.text)
            if m and m.end() < len(c.text):
                start = c.start + m.end()
                rect = c.line.rect_for(start, c.end) if c.line is not None else None
                if rect is not None:
                    c = replace(c, text=c.text[m.end():], start=start, rect=rect)
        out.append(c)
    return out


def _type_from_table_columns(doc, candidates: list, ruleset) -> list:
    """Type a value from the column it sits under.

    A table row has no label beside it - the label is the column header, several
    rows up and horizontally aligned. Without this, the second row of an SSN
    column is just a number.
    """
    from .labels import detect_labels

    labels = detect_labels(doc, ruleset)

    # Only a HEADER ROW counts: two or more labels side by side on one line.
    # A lone stacked label ("Email:" with its value beneath) belongs to the
    # field-group logic, and treating it as a column header made it claim
    # unrelated values further down the page.
    per_line: dict[tuple, list] = {}
    for label in labels:
        if label.non_pii or len(label.expected_types) != 1:
            continue
        per_line.setdefault(label.line.key(), []).append(label)

    def is_header_row(line_labels: list) -> bool:
        """A header row is labels with NOTHING between them. "Date of birth 11/02/1979
        Nationality: Brazil" also has two labels on one line, but it is a row of
        key-value pairs; treating it as a header typed every value below it as a date
        of birth ("Badge no. 77231" became a DOB)."""
        ordered = sorted(line_labels, key=lambda lb: lb.start)
        text = ordered[0].line.text
        for position, lb in enumerate(ordered):
            gap_end = ordered[position + 1].start if position + 1 < len(ordered) else len(text)
            if text[lb.end:gap_end].strip():
                return False
        return True

    headers: dict[int, list] = {}
    for line_labels in per_line.values():
        if len(line_labels) < 2 or not is_header_row(line_labels):
            continue
        for label in line_labels:
            headers.setdefault(label.page_no, []).append(label)
    if not headers:
        return candidates

    for candidate in candidates:
        if candidate.source is Source.MANUAL:
            continue
        for header in headers.get(candidate.page_no, []):
            if header.line.key() == candidate.line.key():
                continue
            # Below the header, within a plausible number of rows, and
            # overlapping it horizontally.
            if candidate.rect[1] <= header.rect[3]:
                continue
            row_height = max(candidate.rect[3] - candidate.rect[1], 1.0)
            if candidate.rect[1] - header.rect[3] > 40 * row_height:
                continue
            overlap = min(candidate.rect[2], header.rect[2]) - max(
                candidate.rect[0], header.rect[0]
            )
            width = max(candidate.rect[2] - candidate.rect[0], 1.0)
            if overlap / width < 0.5:
                continue
            expected = header.expected_types[0]
            if candidate.pii_type is expected:
                break
            candidate.evidence.append(
                Evidence(
                    Source.GROUP,
                    f"typed {expected.value} from the '{header.text}' column",
                    0.0,
                )
            )
            candidate.pii_type = expected
            break
    return candidates


def _label_hints(candidates: list, groups: list) -> tuple[dict[str, str], set[str]]:
    """(value -> the label it sits under, values whose type the label agrees with).

    Fed to the local model so it reads "Employer <- Acme Corp" rather than a bare
    string. The second set is the safety side: a value the label itself says is that
    kind of identity is never dropped on the model's say-so.
    """
    spans: dict[tuple, list] = {}
    for group in groups:
        if group.label.non_pii or group.label.unknown or not group.expected_types:
            continue
        for line in group.value_lines:
            spans.setdefault(line.key(), []).append((0, len(line.text), group))
        if group.inline_span is not None:
            start, end = group.inline_span
            spans.setdefault(group.label.line.key(), []).append((start, end, group))
    hints: dict[str, str] = {}
    agreed: set[str] = set()
    for candidate in candidates:
        value = candidate.normalized
        if not value:
            continue
        for start, end, group in spans.get(candidate.line.key(), ()):
            if min(candidate.end, end) - max(candidate.start, start) < (candidate.end - candidate.start) / 2:
                continue
            hints.setdefault(value.lower(), group.label.text.strip().rstrip(":").strip())
            if candidate.pii_type in group.expected_types:
                agreed.add(value.lower())
            break
    return hints, agreed


def _retype_from_labels(candidates: list, groups: list) -> list:
    """The field label outranks a model's guess about what a value is.

    "Business name / Acme Holdings LLC" was being typed PERSON, because a
    name-shaped string looks like a person to a model trained on prose. The
    label already said what the field holds.

    This covers a value written on the label's own line ("Spouse: Jane Doe") as well
    as stacked value lines. Real models disagree with each other about unfamiliar
    names (a Vietnamese or Arabic name under "Spouse:" came back as a city), and the
    field label is the better witness. A value whose type the label agrees with also
    gets that as evidence: a label-bound value is not a lone model guess.
    """
    by_line: dict[tuple, list] = {}
    for group in groups:
        if group.label.non_pii or group.label.unknown or not group.expected_types:
            continue
        for line in group.value_lines:
            by_line.setdefault(line.key(), []).append((0, len(line.text), group))
        if group.inline_span is not None:
            start, end = group.inline_span
            by_line.setdefault(group.label.line.key(), []).append((start, end, group))

    for candidate in candidates:
        if candidate.source is Source.MANUAL:
            continue  # the user said what this is
        for start, end, group in by_line.get(candidate.line.key(), ()):
            inside = min(candidate.end, end) - max(candidate.start, start)
            if inside * 2 < candidate.end - candidate.start:
                continue  # mostly outside this field's value
            expected = group.expected_types
            if candidate.pii_type in expected:
                if not any(e.source is Source.GROUP for e in candidate.evidence):
                    candidate.evidence.append(
                        Evidence(Source.GROUP, f"value of the labelled field '{group.label.text.strip()}'", 0.7)
                    )
                break
            if len(expected) != 1:
                continue
            if candidate.source is Source.REGEX and candidate.line.key() == group.label.line.key():
                continue  # a pattern match on a shared line is not overruled by a neighbour's label
            candidate.evidence.append(
                Evidence(
                    Source.GROUP,
                    f"retyped {candidate.pii_type.value} -> {expected[0].value} from its field label",
                    0.7,
                )
            )
            candidate.pii_type = expected[0]
            _clear_type_doubt(candidate)
            break
    return candidates


#: Review flags that only express a MODEL's doubt about its own type guess. Once a
#: field label (or a label-confirmed identity) settles the type, they no longer
#: apply - a flag that outlives its reason holds a confirmed value for no reason.
_TYPE_DOUBT_REASONS = ("location entity outside a labelled address group",
                       "a known person's name, typed as a place by the model")


def _clear_type_doubt(candidate) -> None:
    if candidate.needs_review and (candidate.review_reason or "").strip() in _TYPE_DOUBT_REASONS:
        candidate.needs_review = False
        candidate.review_reason = ""


#: Words that make text read as a street. A "street address" with no digit and none of
#: these is not backed by its own shape.
_STREET_WORDS = frozenset({
    "street", "st", "avenue", "ave", "road", "rd", "boulevard", "blvd", "drive", "dr", "lane",
    "ln", "court", "ct", "circle", "cir", "place", "pl", "terrace", "ter", "parkway", "pkwy",
    "highway", "hwy", "way", "common", "cmn", "trail", "crossing", "square", "alley", "plaza",
    "box", "suite", "ste", "apartment", "apt", "unit", "floor", "rue", "calle", "via", "viale",
    "strasse", "straße", "str", "platz", "weg", "allee", "avenida", "rua", "carrer", "ulica",
})


def _retype_name_shaped_places(candidates: list) -> list:
    """A model that sees a name next to an address may call the NAME an address.

    GLiNER reads a window of the page, so "Sławomir Nowak" printed above "Main Street 4"
    came back as a street address (and a Vietnamese name as a city). A street address has
    a number or a street word; a city is confirmed by spaCy as a place. Where a place type
    rests on the model's guess alone, the text has neither, and the text is shaped like a
    person's name in its own script, it is a name. It is flagged so the type gets a look.
    """
    from .heuristics import looks_like_person  # one capitalised word is never name-shaped here

    for c in candidates:
        if c.pii_type not in (PiiType.STREET, PiiType.ADDRESS, PiiType.CITY_STATE):
            continue
        if c.source is not Source.NER or any(e.source is not Source.NER for e in c.evidence):
            continue  # a rule, a label or the user vouched for the type
        text = c.text.strip()
        if any(ch.isdigit() for ch in text):
            continue
        words = {w.strip(".,;:").casefold() for w in text.split()}
        if words & _STREET_WORDS:
            continue
        if c.pii_type is PiiType.CITY_STATE and any(
            "gpe" in e.detail.lower() or "loc" in e.detail.lower() for e in c.evidence
        ):
            continue  # spaCy independently says this is a place
        if not looks_like_person(text)[0]:
            continue
        c.evidence.append(
            Evidence(Source.NER, f"retyped {c.pii_type.value} -> PERSON: name-shaped, no address component", 0.35)
        )
        c.pii_type = PiiType.PERSON
        c.needs_review = True
        c.review_reason = (
            "the model called this an address, but it has no number or street word and is "
            "shaped like a person's name - check the type"
        )
    return candidates


#: Identifier types whose real values carry a digit or a symbol. A model that tags a plain
#: word with one of these (a country in a sentence as a "username") is guessing.
_SHAPED_ID_TYPES = frozenset({
    PiiType.USERNAME, PiiType.EMPLOYEE_ID, PiiType.MRN, PiiType.DRIVERS_LICENSE, PiiType.PASSPORT,
})


def _doubt_shapeless_identifiers(candidates: list) -> list:
    """An identifier with no digit or symbol, on a model's say-so alone, is in doubt.

    Not removed: it loses confidence, so it waits for a person instead of being applied.
    A label-bound value ("Username: sam") keeps its label evidence and is untouched.
    """
    for c in candidates:
        if c.pii_type not in _SHAPED_ID_TYPES or c.source is not Source.NER:
            continue
        if any(e.source in (Source.REGEX, Source.GROUP, Source.COVERAGE, Source.MANUAL) for e in c.evidence):
            continue  # a rule, a label or the user vouched for it (the AI's disagreement does not)
        if any(ch.isdigit() or ch in "@_./-" for ch in c.text):
            continue
        c.evidence.append(Evidence(Source.NER, "identifier-typed text with no digit or symbol", -0.2))
        c.needs_review = True
        c.review_reason = c.review_reason or (
            f"the model called this a {c.pii_type.value.lower().replace('_', ' ')}, but it has no "
            "digit or symbol - it may just be a word"
        )
    return candidates


_VALUE_PARTICLES = {
    "van", "von", "der", "den", "del", "della", "dela", "de", "di", "da", "du", "la", "le", "el", "al",
    "bin", "ibn", "bint", "ter", "ten", "dos", "das", "los", "las", "and", "the",
}


def _is_prose_word(token: str) -> bool:
    """A plain lowercase word of four or more letters: running text, not a value."""
    from ..entities.registry import EntityRegistry

    word = token.strip(".,;:()'\"")
    if len(word) < 4 or not word.isalpha() or not word.islower():
        return False
    return word not in _VALUE_PARTICLES and word not in EntityRegistry._STREET_SYNONYMS


def _complete_partial_lines(candidates: list, labels: list) -> list:
    """Never redact half a value.

    Two reported failures, both the same shape:

        "LJ P"             -> only "LJ" was replaced
        "Fremont, CA 1234" -> only "1234" was replaced

    Field-value widening only covers lines inside a recognised label group. On a
    line with no label, a detector that matched part of the value left the rest
    in the document. This closes that: if what remains uncovered on the line is
    identity-shaped - not a label, not a figure, not the form's own vocabulary -
    the candidate is widened to the whole line.
    """
    from ..pseudonymization.generator import INLINE_LABELS
    from .deterministic import MONEY_RE, PERCENT_RE
    from .heuristics import FORM_VOCABULARY

    # Common English function words: pronouns, prepositions, everyday verbs.
    # A genuine form-value remainder is ANOTHER PART OF THE SAME VALUE - a
    # second name token, a suffix, a number - never one of these. Their
    # presence means the line is a SENTENCE, not a field value, however short
    # it is. "Follow me @traveler_jane on Instagram" (38 chars, 5 tokens) slid
    # under the length/token-count guard below and swallowed a whole sentence
    # around a social handle, because none of "follow"/"me"/"on"/"instagram"
    # happened to be on the FORM_VOCABULARY list, which is about TAX/FORM
    # terms, not general English. This is a small, separate, general check -
    # extend THIS set for future English-word leaks, not FORM_VOCABULARY.
    ENGLISH_FUNCTION_WORDS = {
        "me", "you", "he", "she", "it", "we", "they", "him", "her", "them",
        "on", "in", "at", "to", "from", "with", "by", "for", "of", "about",
        "follow", "following", "visit", "see", "call", "email", "text",
        "our", "your", "his", "her", "their", "my", "its",
    }

    label_spans: dict[tuple, list[tuple[int, int]]] = {}
    for label in labels:
        label_spans.setdefault(label.line.key(), []).append((label.start, label.end))

    by_line: dict[tuple, list] = {}
    for candidate in candidates:
        by_line.setdefault(candidate.line.key(), []).append(candidate)

    for key, group in by_line.items():
        line = group[0].line
        text = line.text
        start = len(text) - len(text.lstrip())
        end = len(text.rstrip())
        if end <= start:
            continue

        protected: set[int] = set()
        for lo, hi in label_spans.get(key, []):
            protected.update(range(lo, hi))
        for match in list(MONEY_RE.finditer(text)) + list(PERCENT_RE.finditer(text)):
            protected.update(range(match.start(), match.end()))

        covered: set[int] = set()
        for candidate in group:
            covered.update(range(candidate.start, candidate.end))

        # Only value-shaped lines. On prose this pass would swallow the
        # sentence around a name; on a form the line IS the value.
        whole = text[start:end]
        if len(whole) > 44 or len(whole.split()) > 5:
            continue
        if re.search(r"[.!?]\s", whole):
            continue

        remainder = [
            index
            for index in range(start, end)
            if index not in covered and index not in protected and text[index].strip()
        ]
        if not remainder:
            continue

        # What is left over: is it part of the same value, or unrelated text?
        # Whitespace has to be kept, or adjacent words merge into one token:
        # "Taxpayer SSN" became "TaxpayerSSN", matched no vocabulary entry, and
        # the label was swallowed into the value.
        remainder_set = set(remainder)
        leftover = "".join(
            text[i] if i in remainder_set else " " for i in range(start, end)
        )
        # Separators are not part of the value. "JP & ML" is two people; the
        # "&" left uncovered between them must not drag one span over it.
        tokens = [
            tok.strip(".,;:()")
            for tok in leftover.split()
            if tok.strip(".,;:()") and tok.strip(".,;:()").lower() not in {"&", "and", "+", "or", "/"}
        ]
        if any(tok.lower() in ENGLISH_FUNCTION_WORDS for tok in tokens):
            continue  # a sentence, not a value line
        # Another part of the SAME VALUE is a name token, a number, a suffix or an
        # initial - not an ordinary lowercase word. "Smith & Wesson makes firearms."
        # had its whole sentence typed PERSON because "makes" and "firearms" are not
        # on any vocabulary list. Particles (van, de, bin ...) and street words are
        # lowercase parts of real values, so they are exempt.
        if any(_is_prose_word(tok) for tok in tokens):
            continue
        if not tokens:
            continue
        # The rest of the line is the form talking, not the value.
        if any(tok.lower() in FORM_VOCABULARY for tok in tokens):
            continue
        if any(tok.lower().strip(".,:;()") in INLINE_LABELS for tok in tokens):
            continue  # "Taxpayer SSN 987654321" - do not swallow the label
        if len(leftover.strip()) > 48:
            continue  # too much unexplained text to assume it is one value

        widest = max(group, key=lambda c: c.end - c.start)
        # A number-defined identifier (SSN, account, EIN ...) is its digits. A word beside
        # it ("Reference 123456789") is not part of the value, so words alone never widen it;
        # a leftover with digits ("(408)" before "555-0198") still does.
        from ..entities.registry import EntityRegistry as _Registry

        if widest.pii_type in _Registry.DIGIT_KEYED and not any(ch.isdigit() for ch in leftover):
            continue
        # The converse: a NAME has no digits. A model's "REV" (PERSON) on the
        # software footer "REV 04/01/22 TTMac" was widened over the date and
        # product code and the footer was pseudonymised on every page.
        if widest.pii_type is PiiType.PERSON and any(ch.isdigit() for ch in leftover):
            continue
        new_start = min(widest.start, min(remainder))
        new_end = max(widest.end, max(remainder) + 1)
        while new_start in protected and new_start < new_end:
            new_start += 1
        while (new_end - 1) in protected and new_end > new_start:
            new_end -= 1
        if new_start >= new_end or (new_start == widest.start and new_end == widest.end):
            continue
        rect = line.rect_for(new_start, new_end)
        if rect is None:
            continue
        widest.start, widest.end = new_start, new_end
        widest.text = text[new_start:new_end]
        widest.rect = rect
        widest.evidence.append(
            Evidence(Source.COVERAGE, "widened to cover the rest of the value", 0.0)
        )
    return candidates


def _expand_to_field_values(candidates: list, groups: list) -> list:
    """A field value is redacted as a unit, never in fragments.

    Whichever detector fires, if its span sits on a value line of a PII-bearing
    field it is widened to the whole line. This is what stops "4820 Camino Del
    Rio Apt 12C" going out as "<redacted> 12C" because the street regex stopped
    at "Apt" - the partial-redaction failure of spec section 39.
    """
    from ..document.model import union
    from .deterministic import MONEY_RE, PERCENT_RE

    value_lines: dict[tuple[int, int, int], object] = {}
    for group in groups:
        if group.label.non_pii:
            continue
        for line in group.value_lines:
            value_lines[line.key()] = line

    by_line: dict[tuple, list] = {}
    for candidate in candidates:
        by_line.setdefault(candidate.line.key(), []).append(candidate)

    for candidate in candidates:
        line = value_lines.get(candidate.line.key())
        if line is None:
            continue
        # If everything meaningful on this line is already covered - as it is
        # once "JP & ML" has been split into two people - widening would merge
        # them back into one entity sharing one pseudonym.
        siblings = by_line.get(candidate.line.key(), [])
        from .entities_pass import CONJUNCTION

        separators: set[int] = set()
        for match in CONJUNCTION.finditer(line.text):
            separators.update(range(match.start(), match.end()))
        meaningful = {
            i for i, ch in enumerate(line.text) if ch.isalnum() and i not in separators
        }
        claimed = {
            i for sib in siblings for i in range(sib.start, sib.end) if line.text[i].isalnum()
        }
        if meaningful and len(claimed) / len(meaningful) >= 0.95:
            continue
        start = len(line.text) - len(line.text.lstrip())
        end = len(line.text.rstrip())
        # Never absorb a financial figure into a field value.
        for match in list(MONEY_RE.finditer(line.text)) + list(PERCENT_RE.finditer(line.text)):
            if match.start() <= start < match.end():
                start = match.end()
            if match.start() < end <= match.end():
                end = match.start()
        if end <= start or (candidate.start <= start and candidate.end >= end):
            continue
        rect = line.rect_for(start, end)
        if rect is None:
            continue
        candidate.start, candidate.end = start, end
        candidate.text = line.text[start:end]
        candidate.rect = rect
        candidate.evidence.append(
            Evidence(Source.GROUP, "widened to the whole field value", 0.0)
        )
    return candidates


def conservative_mode() -> bool:
    """High-precision only. DOCANON_CONSERVATIVE=1 turns it on.

    The layers that widen coverage - line widening, whole-line group values,
    shape heuristics, propagation, the model's own additions - are what find
    values nothing else sees. They are also what damages a document when they
    misfire, and misfires are hard to see until a real form goes through.

    In this mode only high-confidence, positively-identified values are
    transformed: matched rules, model hits above threshold, and values in
    fields whose label names them. Recall drops. Nothing gets mangled.
    """
    import os

    return os.environ.get("DOCANON_CONSERVATIVE", "0") in ("1", "true", "yes")


def _conservative_filter(candidates: list) -> tuple[list, int]:
    """Keep only what was positively identified."""
    kept, dropped = [], 0
    for candidate in candidates:
        if candidate.source is Source.MANUAL:
            kept.append(candidate)
            continue
        unnamed = candidate.pii_type is PiiType.UNCLASSIFIED_GROUP_VALUE
        # GROUP is not weak evidence: it is a value confirmed by its own field
        # label, and it is the ONLY mechanism that creates the household split
        # for a joint name ("Mark & Jane Lang" -> Mark, Jane Lang) and types a
        # stacked field's value at all. Excluding it here is why Careful mode
        # was missing joint names and labelled values specifically.
        weak = candidate.source in (Source.COVERAGE, Source.AUDIT)
        if unnamed or weak or candidate.confidence < 0.65:
            dropped += 1
            continue
        kept.append(candidate)
    return kept, dropped


def analyse(
    doc: Document,
    ruleset: Optional[RuleSet] = None,
    use_ner: bool = True,
    use_llm: bool = False,
    use_presidio: bool = True,
) -> DetectionResult:
    rs = ruleset or load_rules()
    warnings: list[str] = []

    # Pass 1 - deterministic rules and local NER.
    candidates = detect_deterministic(doc, rs)
    spacy_nlp = None
    if use_ner:
        from .ner import load_nlp, NerUnavailable

        try:
            spacy_nlp = load_nlp()
        except NerUnavailable:
            spacy_nlp = None
        # `candidates` already holds every deterministic hit at this point in
        # the pipeline (Pass 1 runs rules first) - passed through so the
        # transformer is not run on blocks those cheap rules already fully
        # explain. See the tiered-execution requirement: en_core_web_trf is
        # the most expensive single step here and must not run
        # unconditionally over every block on every page.
        ner_candidates, ner_warnings = detect_ner(doc, spacy_nlp, existing=candidates)
        candidates.extend(ner_candidates)
        warnings.extend(ner_warnings)

    # Pass 1b - GLiNER. Label-conditioned, so it returns the label asked for
    # rather than a generic entity type that has to be guessed at afterwards.
    negatives: list = []
    if use_ner:
        gliner_candidates, gliner_warnings, negatives = detect_gliner(doc)
        candidates.extend(gliner_candidates)
        warnings.extend(gliner_warnings)

    # Pass 2 - shape heuristics for identity text the model misses (ALL CAPS
    # names, surname-first names) before grouping, so groups can see them.
    candidates.extend(detect_heuristics(doc, candidates))

    # Pass 3 - labels, logical field groups, stacked/compound inference.
    groups, labels, group_candidates = build_groups(doc, candidates, rs)
    candidates.extend(group_candidates)
    candidates = _type_unclassified_group_values(candidates)

    # Pass 1c - Presidio. Runs AFTER groups exist, since its context
    # enhancement is built from the label groups.py already extracted, not a
    # second label parse. It only PROPOSES: every result carries low starting
    # confidence and needs_review, exactly like a GLiNER span, so the same
    # downstream label-strip, form-text veto and overlap resolution decide
    # what survives.
    if use_presidio:
        from .presidio_adapter import detect_presidio

        presidio_candidates, presidio_warnings = detect_presidio(doc, groups, spacy_nlp)
        candidates.extend(presidio_candidates)
        warnings.extend(presidio_warnings)

    # Labels are context, not PII.
    candidates = strip_label_overlaps(candidates, labels)

    # Neither is the rest of the form. Headings, instructions and running
    # headers belong to the document, not to the client.
    form_text = detect_form_text(doc, labels, groups, candidates)
    candidates, dropped = veto_form_text(candidates, form_text)
    if dropped:
        log.debug("dropped %d candidate(s) sitting on the form's own text", dropped)

    # A name-shaped phrase set in the typeface of the form's own labels is the
    # form talking ("Presidential Election Campaign"), when - and only when -
    # this document sets its filled values in a different style.
    from .layout_style import form_styles, veto_form_styled_proposals

    from .heuristics import looks_like_person
    from .layout_style import form_vocabulary

    document_form_styles = form_styles(labels, groups)
    document_form_vocabulary = form_vocabulary(doc, document_form_styles)
    # Running chrome (a line repeated at the same spot on 3+ pages) is the
    # document's own text too - a software footer mark tagged PERSON once, at an
    # unusual spot, was otherwise redacted there and left on 60 other pages.
    for page in doc.pages:
        for line in page.lines:
            if form_text.reason_for(line) == "repeats on every page" and not looks_like_person(line.text)[0]:
                document_form_vocabulary.update(
                    w.strip(".,;:()[]'\"\u2019").lower() for w in line.text.split() if w.strip())
    # Never a word some rule or field label has identified as a VALUE anywhere in
    # the document: a filer's name printed in a fixed header spot on several
    # software pages is "repeated", but the same name in the 1040's name box is
    # label-bound - measured, without this the name was suppressed document-wide.
    for c in candidates:
        if c.source in (Source.GROUP, Source.REGEX, Source.MANUAL) or c.group_id is not None:
            for w in c.text.split():
                document_form_vocabulary.discard(w.strip(".,;:()[]'\"\u2019").lower())
    candidates, styled = veto_form_styled_proposals(candidates, document_form_styles)
    if styled:
        log.debug("dropped %d name-shaped proposal(s) set in the form's own typeface", styled)

    # The protected regions of THIS document - labels, form text, figures - built
    # once. The unified gate at the end of the pipeline (and on every later path
    # that adds candidates) answers to this, not to seven separate passes.
    protection = ProtectionIndex.build(doc, labels, form_text, groups)
    candidates, prior = drop_prior_replacements(candidates, protection)
    if prior:
        log.debug("left %d candidate(s) on text an earlier run already replaced", prior)

    # Anything the model confidently called a business fact stays in the document.
    candidates = _veto(candidates, negatives, groups)

    # Pass 4 - completeness analysis before resolution so partial groups are
    # flagged against the full candidate set.
    analyse_coverage(groups, candidates)

    candidates = resolve_overlaps(candidates)
    candidates = _drop_financial_values(candidates)

    # Pass 5 - joint and compound name fields become one target per person, so
    # spouses do not share a pseudonym.
    candidates = resolve_overlaps(expand_compound_names(candidates, groups))

    # Pass 6 - establish who this document is about, then find every remaining
    # occurrence of them. This is where isolated repeats in tables, headers and
    # footers get caught, having triggered no detector of their own.
    subjects = identify_subjects(candidates, groups)
    propagated = propagate(doc, candidates, subjects)
    if propagated:
        candidates = resolve_overlaps(candidates + propagated)
    beside = _dates_beside_identity(doc, candidates)
    if beside:
        candidates = resolve_overlaps(candidates + beside)

    # Pass 7 - LLM audit. Advisory only: everything it names is located in the
    # real text by exact search, then goes through label stripping and overlap
    # resolution like any other candidate.
    if use_llm:
        additions, wrongly_flagged, audit_warnings = audit_document(
            doc, candidates, labels=labels
        )
        warnings.extend(audit_warnings)
        if wrongly_flagged:
            candidates, removed, flagged = apply_ai_objections(
                candidates, set(wrongly_flagged), "a label or business fact"
            )
            if removed or flagged:
                log.debug("AI objections: removed %d weak, flagged %d strong", removed, flagged)
        if additions:
            additions = strip_label_overlaps(additions, labels)
            candidates = resolve_overlaps(candidates + additions)

    # Run the financial guard again, last. Propagation and the audit pass both
    # add candidates after the first pass, and either can land inside a currency
    # amount - which is how "123" ended up pseudonymising "$123,456".
    candidates = _drop_financial_values(candidates)

    # Pass 8 - the model reviews the whole plan, not just the doubtful pages.
    # The layers above favour recall, so this is where the form's own labels and
    # stray figures get dropped before anything is written.
    if use_llm:
        hints, label_agreed = _label_hints(candidates, groups)
        rejected, adjudication_warnings = adjudicate_document(doc, candidates, fields=hints)
        warnings.extend(adjudication_warnings)
        rejected = {r for r in rejected if r.lower() not in label_agreed}
        if rejected:
            candidates, removed, flagged = apply_ai_objections(
                candidates, set(rejected), "form text or a business fact"
            )
            if flagged:
                warnings.append(
                    f"the AI questioned {flagged} detection(s) backed by strong evidence; "
                    "they were kept and flagged for review"
                )

    candidates = _drop_label_text(candidates, labels)
    candidates = _drop_bare_states(candidates)
    candidates = _drop_numberless_model_streets(candidates)
    candidates = _drop_implausible_model_hits(candidates, document_form_vocabulary)
    candidates = _trim_honorifics(candidates)
    candidates = _trim_possessives(candidates)
    candidates = _retype_known_name_components(candidates)
    if conservative_mode():
        candidates, held_back = _conservative_filter(candidates)
        if held_back:
            warnings.append(
                f"conservative mode: {held_back} lower-confidence detection(s) were "
                "not applied. Values found only by shape, widening or the model are "
                "excluded; use Add missed item for anything it leaves behind."
            )

    # Propagated hits are never re-vetoed here. propagate() only fires for a
    # value already CONFIRMED as belonging to this document's subject, so a
    # boilerplate sentence that happens to repeat with the client's name
    # substituted in must still lose the name - the repetition guess is not
    # allowed to override a confirmed identity.
    from .types import Source as _Source

    protected = {c.id for c in candidates if c.source is _Source.COVERAGE}
    survivors, veto_pool = [], []
    for c in candidates:
        (survivors if c.id in protected else veto_pool).append(c)
    veto_pool, late_dropped = veto_form_text(veto_pool, form_text)
    candidates = survivors + veto_pool
    if late_dropped:
        warnings.append(
            f"{late_dropped} later detection(s) fell on the form's own text and "
            "were discarded"
        )
    candidates = _retype_from_labels(candidates, groups)
    candidates = _retype_name_shaped_places(candidates)
    candidates = _doubt_shapeless_identifiers(candidates)
    candidates = _type_from_table_columns(doc, candidates, rs)
    candidates = _expand_to_field_values(candidates, groups)
    candidates = _complete_partial_lines(candidates, labels)
    candidates = _drop_financial_values(candidates)

    # A final safety pass. Widening, propagation, and the standalone
    # joint-name detector all add or extend candidates AFTER the first label
    # strip, and any of them can land on a label's own text - a field's
    # caption getting a redaction badge is the same failure as redacting it.
    candidates = strip_label_overlaps(candidates, labels)

    # Geometric safety net: a true bounding-box check, independent of which
    # Line object either side belongs to. Catches what the character-offset
    # version above cannot - extraction splitting one visual line into two
    # Line objects, or a proposal layer's span landing on a distinct but
    # geometrically overlapping line.
    candidates, geo_dropped = veto_by_label_geometry(candidates, labels)
    if geo_dropped:
        warnings.append(
            f"{geo_dropped} detection(s) geometrically overlapped a label by "
            "more than half their area and were discarded"
        )

    # The authority: whatever any layer proposed, nothing protected is redacted,
    # nothing is clipped short of its real value, and every outcome has a reason.
    candidates, gate_log = CandidateGate(protection).run(candidates)
    # Verified label binding first: the corroboration hold and the authorization
    # policy both read it, and a bare group id is not proof of a label.
    from ..decisions.authorization import annotate_label_binding

    annotate_label_binding(candidates, groups)
    # Strict PII-only: a proposal nothing corroborates is held for review.
    candidates, held = hold_unconfirmed_proposals(candidates)
    if held:
        log.debug("held %d unconfirmed proposal(s) for review", held)
    rejected_by_gate = sum(1 for d in gate_log if d.action == "reject")
    if rejected_by_gate:
        log.debug("gate rejected %d candidate(s) on protected text", rejected_by_gate)

    analyse_coverage(groups, candidates)

    for subject in subjects:
        log.debug("subject: %s (%s)", subject.pii_type.value, subject.reason)

    for page in doc.pages:
        if page.needs_ocr:
            warnings.append(
                f"OCR REQUIRED - page {page.number + 1}: {page.ocr_reason}. "
                "This page was NOT analysed and cannot be considered anonymised."
            )

    for group in groups:
        if not group.complete:
            warnings.append(f"page {group.page_no + 1}: {group.reason} ({group.describe()})")

    # Evidence-based adjudication (spec sections 6-7, 18-19). A separate,
    # later read of the same evidence every candidate already carries -
    # never changes what a detector set .confidence to, only adds a
    # confirmed/probable/unresolved judgement on top.
    from .adjudication import Adjudication, adjudicate

    confirmed = probable = unresolved = 0
    for candidate in candidates:
        verdict = adjudicate(candidate)
        candidate.adjudication = verdict.value
        if verdict is Adjudication.CONFIRMED:
            confirmed += 1
        elif verdict is Adjudication.PROBABLE:
            probable += 1
        else:
            unresolved += 1
            if not candidate.needs_review:
                candidate.needs_review = True
                candidate.review_reason = candidate.review_reason or (
                    "low aggregated confidence for this type - review before accepting"
                )

    # Flag-only: the model's second opinion on whether each name/company/place
    # was given the right type. Never changes a type or a decision.
    if use_llm:
        warnings.extend(check_types(candidates))

    pages_needing_ocr = sum(1 for page in doc.pages if page.needs_ocr)

    return DetectionResult(
        candidates=candidates,
        groups=groups,
        labels=labels,
        warnings=warnings,
        gate_log=gate_log,
        protection=protection,
        form_styles=document_form_styles,
        form_vocabulary=document_form_vocabulary,
        pages_analysed=len(doc.pages) - pages_needing_ocr,
        pages_needing_ocr=pages_needing_ocr,
        confirmed_count=confirmed,
        probable_count=probable,
        unresolved_count=unresolved,
    )
