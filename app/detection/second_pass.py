"""Second pass: check the work (spec sections 18-19, 37-39).

The first pass decides what to replace. This one asks the harder question -
after those replacements, is anything identifying still there?

It builds the transformed document in memory, extracts its text, and runs the
detectors over it again. Anything found is by definition something the first
pass missed or only partly covered: the replacements are already in place, so a
hit is residual original text.

Findings are located back in the SOURCE document by exact search, because that
is where redaction geometry lives. Anything that cannot be located is discarded
rather than guessed at.

Nothing is applied. Findings enter the review list flagged as second pass and
wait for a decision, because a check that silently acts on its own conclusions
is not a check.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from ..document.model import Document
from ..document.pdflock import PDF_LOCK
from ..transform.plan import TransformationPlan
from .types import Candidate, Evidence, PiiType, Source

log = logging.getLogger(__name__)

#: Detector sources trusted to report a genuine miss on the second pass. Layout
#: grouping is excluded: the transformed document has different text, so its
#: field groups are not comparable to the original's.
TRUSTED_SOURCES = {Source.REGEX, Source.NER, Source.COVERAGE}

MIN_LENGTH = 3


@dataclass
class SecondPassResult:
    findings: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scanned_pages: int = 0

    @property
    def clean(self) -> bool:
        return not self.findings


def _transformed_text(plan: TransformationPlan) -> tuple[list[str], Optional[str]]:
    """Page text of the document the plan would produce, without writing a file."""
    from ..export.redactor import apply_plan

    try:
        with PDF_LOCK:
            pdf, _report = apply_plan(plan)
            try:
                return [page.get_text() for page in pdf], None
            finally:
                pdf.close()
    except Exception as exc:  # noqa: BLE001 - advisory pass, never fatal
        return [], f"{type(exc).__name__}: {exc}"


def second_pass(
    doc: Document, plan: TransformationPlan, ruleset=None, use_ner: bool = True
) -> SecondPassResult:
    """Re-scan the transformed document and report what is still identifying."""
    from .deterministic import detect_deterministic
    from .heuristics import detect_heuristics
    from .ner import detect_ner
    from .provider_shim import document_from_pages

    result = SecondPassResult()
    pages, error = _transformed_text(plan)
    if error:
        result.warnings.append(f"second pass could not build the preview: {error}")
        return result
    result.scanned_pages = len(pages)

    scratch = document_from_pages(pages)
    if scratch is None:
        return result

    candidates = detect_deterministic(scratch, ruleset)
    if use_ner:
        ner_candidates, ner_warnings = detect_ner(scratch)
        candidates.extend(ner_candidates)
        result.warnings.extend(ner_warnings)
    candidates.extend(detect_heuristics(scratch, candidates))

    replaced = {t.replacement.lower() for t in plan.targets}
    kept = {value.lower() for value in plan.skipped_values}
    seen: set[str] = set()
    residual: list[tuple[str, PiiType]] = []

    for candidate in candidates:
        if candidate.source not in TRUSTED_SOURCES:
            continue
        value = candidate.normalized
        lowered = value.lower()
        if len(value) < MIN_LENGTH or lowered in seen:
            continue
        if lowered in replaced:
            continue    # this is a pseudonym we put there
        if lowered in kept:
            continue    # deliberately left alone
        seen.add(lowered)
        residual.append((value, candidate.pii_type))

    result.findings = _locate_in_source(doc, residual, plan)
    return result


def _locate_in_source(
    doc: Document, residual: list[tuple[str, PiiType]], plan: TransformationPlan
) -> list[Candidate]:
    """Find each residual value in the original, where redaction geometry lives."""
    already = {(t.page_no, t.rect) for t in plan.targets}
    found: list[Candidate] = []

    for value, pii_type in residual:
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])")
        for page in doc.pages:
            for line in page.lines:
                for match in pattern.finditer(line.text):
                    rect = line.rect_for(match.start(), match.end())
                    if rect is None or (page.number, rect) in already:
                        continue
                    found.append(
                        Candidate(
                            pii_type=pii_type,
                            text=line.text[match.start():match.end()],
                            page_no=page.number,
                            rect=rect,
                            line=line,
                            start=match.start(),
                            end=match.end(),
                            confidence=0.75,
                            source=Source.COVERAGE,
                            evidence=[
                                Evidence(
                                    Source.COVERAGE,
                                    "still readable in the anonymized output",
                                    0.75,
                                )
                            ],
                            needs_review=True,
                            review_reason="still readable in the anonymized output",
                        )
                    )
    return found


# --------------------------------------------------------------------------- #
# replacement plausibility
# --------------------------------------------------------------------------- #

REPLACEMENT_PROBLEMS = {
    "identical": "the replacement is the same as the original",
    "empty": "no replacement was produced",
    "unreadable": "the replacement is not readable text",
    "wrong_shape": "the replacement is a different kind of thing",
    "leaks": "the replacement repeats part of the original",
}


def check_replacements(plan: TransformationPlan) -> list[tuple[str, str, str]]:
    """Sanity-check every replacement before anything is written.

    Returns (original, replacement, problem). Deterministic on purpose: these
    are the failures that reached documents - a scrambled string, a date that
    stopped being a date, a pseudonym repeating the original's surname.
    """
    import re as _re

    problems: list[tuple[str, str, str]] = []
    date = _re.compile(r"^\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}$")
    digits_only = _re.compile(r"^[\d\s\-().+]+$")

    for target in plan.targets:
        if target.blackout:
            continue
        original, replacement = target.original.strip(), target.replacement.strip()
        if not replacement:
            problems.append((original, replacement, REPLACEMENT_PROBLEMS["empty"]))
            continue
        if original.lower() == replacement.lower():
            problems.append((original, replacement, REPLACEMENT_PROBLEMS["identical"]))
            continue

        # A word replaced by an unpronounceable string is the scrambling bug.
        # Four consecutive consonants is the signature of character-level
        # masking ("Nkzjenrkk"); a low vowel ratio catches the rest.
        for token in replacement.split():
            letters = [ch.lower() for ch in token if ch.isalpha()]
            if len(letters) < 4:
                continue
            vowels = sum(1 for ch in letters if ch in "aeiouy")
            run = 0
            longest = 0
            for ch in letters:
                run = 0 if ch in "aeiouy" else run + 1
                longest = max(longest, run)
            if longest >= 4 or vowels / len(letters) < 0.2:
                problems.append((original, replacement, REPLACEMENT_PROBLEMS["unreadable"]))
                break
        else:
            if date.match(original) and not date.match(replacement):
                problems.append((original, replacement, REPLACEMENT_PROBLEMS["wrong_shape"]))
            elif digits_only.match(original) and not digits_only.match(replacement):
                problems.append((original, replacement, REPLACEMENT_PROBLEMS["wrong_shape"]))
            else:
                # An email keeps its domain on purpose, so compare only the
                # part before the "@"; otherwise every address looks like a leak.
                left = original.split("@")[0] if "@" in original else original
                right = replacement.split("@")[0] if "@" in replacement else replacement
                shared = {
                    tok.lower() for tok in _re.split(r"[^A-Za-z0-9]+", left) if len(tok) >= 3
                } & {
                    tok.lower() for tok in _re.split(r"[^A-Za-z0-9]+", right) if len(tok) >= 3
                }
                if shared:
                    problems.append((original, replacement, REPLACEMENT_PROBLEMS["leaks"]))
    return problems
