"""Detection orchestration (spec sections 10, 18, 19, 23, 24)."""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..document.model import Document
from .deterministic import RuleSet, detect_deterministic, load_rules
from .auditor import adjudicate_document, audit_document
from .entities_pass import expand_compound_names, identify_subjects, propagate
from .gliner import detect_gliner
from .groups import analyse_coverage, build_groups
from .heuristics import detect_heuristics
from .ner import detect_ner
from .resolve import resolve_overlaps, strip_label_overlaps
from .types import DetectionResult, Evidence, Source

log = logging.getLogger(__name__)


def _veto(candidates: list, negatives: list) -> list:
    """Drop candidates the model confidently called money, a form number or a job title.

    This is the direct fix for redacting figures and business facts: a
    deterministic rule can fire on a number, but a model that says "that is a
    dollar amount" outranks it.
    """
    if not negatives:
        return candidates
    by_page: dict[int, list] = {}
    for page_no, rect, _label in negatives:
        by_page.setdefault(page_no, []).append(rect)

    kept = []
    for candidate in candidates:
        blocked = False
        for rect in by_page.get(candidate.page_no, []):
            cx0, cy0, cx1, cy1 = candidate.rect
            nx0, ny0, nx1, ny1 = rect
            # Candidate sits inside a region the model called non-PII.
            if cx0 >= nx0 - 1 and cx1 <= nx1 + 1 and cy0 >= ny0 - 1 and cy1 <= ny1 + 1:
                blocked = True
                break
        if not blocked:
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


def _retype_from_labels(candidates: list, groups: list) -> list:
    """The field label outranks a model's guess about what a value is.

    "Business name / Acme Holdings LLC" was being typed PERSON, because a
    name-shaped string looks like a person to a model trained on prose. The
    label already said what the field holds.
    """
    expected_by_line: dict[tuple, list] = {}
    for group in groups:
        if group.label.non_pii or len(group.expected_types) != 1:
            continue
        for line in group.value_lines:
            expected_by_line[line.key()] = group.expected_types[0]

    for candidate in candidates:
        expected = expected_by_line.get(candidate.line.key())
        if expected is None or candidate.pii_type is expected:
            continue
        if candidate.source is Source.MANUAL:
            continue  # the user said what this is
        candidate.evidence.append(
            Evidence(
                Source.GROUP,
                f"retyped {candidate.pii_type.value} -> {expected.value} from its field label",
                0.0,
            )
        )
        candidate.pii_type = expected
    return candidates


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
    from .deterministic import MONEY_RE, PERCENT_RE
    from .heuristics import FORM_VOCABULARY

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
        leftover = "".join(text[i] for i in remainder)
        # Separators are not part of the value. "JP & ML" is two people; the
        # "&" left uncovered between them must not drag one span over it.
        tokens = [
            tok.strip(".,;:()")
            for tok in leftover.split()
            if tok.strip(".,;:()") and tok.strip(".,;:()").lower() not in {"&", "and", "+", "or", "/"}
        ]
        if not tokens:
            continue
        if any(tok.lower() in FORM_VOCABULARY for tok in tokens):
            continue  # the rest of the line is the form talking, not the value
        if len(leftover.strip()) > 48:
            continue  # too much unexplained text to assume it is one value

        widest = max(group, key=lambda c: c.end - c.start)
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


def analyse(
    doc: Document,
    ruleset: Optional[RuleSet] = None,
    use_ner: bool = True,
    use_llm: bool = True,
) -> DetectionResult:
    rs = ruleset or load_rules()
    warnings: list[str] = []

    # Pass 1 - deterministic rules and local NER.
    candidates = detect_deterministic(doc, rs)
    if use_ner:
        ner_candidates, ner_warnings = detect_ner(doc)
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

    # Labels are context, not PII.
    candidates = strip_label_overlaps(candidates, labels)

    # Anything the model confidently called a business fact stays in the document.
    candidates = _veto(candidates, negatives)

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

    # Pass 7 - LLM audit. Advisory only: everything it names is located in the
    # real text by exact search, then goes through label stripping and overlap
    # resolution like any other candidate.
    if use_llm:
        additions, wrongly_flagged, audit_warnings = audit_document(doc, candidates)
        warnings.extend(audit_warnings)
        if wrongly_flagged:
            lowered = {w.lower() for w in wrongly_flagged}
            before = len(candidates)
            candidates = [c for c in candidates if c.normalized.lower() not in lowered]
            if before != len(candidates):
                log.debug("auditor removed %d business-fact detections", before - len(candidates))
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
        rejected, adjudication_warnings = adjudicate_document(doc, candidates)
        warnings.extend(adjudication_warnings)
        if rejected:
            candidates = [c for c in candidates if c.normalized.lower() not in rejected]

    candidates = _retype_from_labels(candidates, groups)
    candidates = _expand_to_field_values(candidates, groups)
    candidates = _complete_partial_lines(candidates, labels)
    candidates = _drop_financial_values(candidates)
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

    return DetectionResult(
        candidates=candidates, groups=groups, labels=labels, warnings=warnings
    )
