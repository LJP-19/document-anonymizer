"""Detection orchestration (spec sections 10, 18, 19, 23, 24)."""

from __future__ import annotations

import logging
from typing import Optional

from ..document.model import Document
from .deterministic import RuleSet, detect_deterministic, load_rules
from .auditor import audit_document
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
        kept.append(candidate)
    return kept


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

    candidates = _expand_to_field_values(candidates, groups)
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
