"""Detection orchestration (spec sections 10, 18, 19, 23, 24)."""

from __future__ import annotations

from typing import Optional

from ..document.model import Document
from .deterministic import RuleSet, detect_deterministic, load_rules
from .groups import analyse_coverage, build_groups
from .ner import detect_ner
from .resolve import resolve_overlaps, strip_label_overlaps
from .types import DetectionResult


def analyse(doc: Document, ruleset: Optional[RuleSet] = None, use_ner: bool = True) -> DetectionResult:
    rs = ruleset or load_rules()
    warnings: list[str] = []

    # Pass 1 - deterministic rules and local NER.
    candidates = detect_deterministic(doc, rs)
    if use_ner:
        ner_candidates, ner_warnings = detect_ner(doc)
        candidates.extend(ner_candidates)
        warnings.extend(ner_warnings)

    # Pass 2 - labels, logical field groups, stacked/compound inference.
    groups, labels, group_candidates = build_groups(doc, candidates, rs)
    candidates.extend(group_candidates)

    # Labels are context, not PII.
    candidates = strip_label_overlaps(candidates, labels)

    # Pass 3 - completeness analysis before resolution so partial groups are
    # flagged against the full candidate set.
    analyse_coverage(groups, candidates)

    candidates = resolve_overlaps(candidates)
    analyse_coverage(groups, candidates)

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
