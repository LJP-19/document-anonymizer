"""Independent verification (spec sections 37-41).

Everything here runs against the SAVED FILE, reopened from disk. Nothing is
verified against in-memory transformation state, because that would only prove
the plan was consistent with itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import pymupdf

from ..detection.deterministic import financial_tokens
from ..transform.plan import TransformationPlan

RED_INT = 0xFF0000
MIN_RESIDUAL_TOKEN = 4


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    critical: bool = True


@dataclass
class VerificationReport:
    output_path: str
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.critical)

    @property
    def status(self) -> str:
        if not self.checks:
            return "NOT VERIFIED"
        # Deliberately NOT the word "verified" on its own. This report proves the
        # transformation plan was executed against the saved file. It cannot prove
        # detection was complete - an entity no detector found is an entity no
        # verifier can check for (spec section 87).
        return "EXPORT VERIFIED" if self.passed else "VERIFICATION FAILED"

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def add(self, name: str, passed: bool, detail: str = "", critical: bool = True) -> None:
        self.checks.append(Check(name, passed, detail, critical))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _contains_value(haystack: str, needle: str) -> bool:
    """Whole-token containment.

    A bare `in` test produces false alarms: the value "123" is a substring of
    the financial figure "$123,456", which would report a survivor that is not
    there. Matches that sit inside a currency amount are excluded.
    """
    needle = _normalize(needle)
    if not needle:
        return False
    # The guards reject matches that are fragments of a longer identifier:
    # "123" inside "123-45-6789" is part of that SSN, not a separate occurrence
    # of the value 123. Without this, skipping one field raises a false residue
    # alarm on an unrelated one.
    pattern = re.compile(
        rf"(?<![\w$])(?<!\d[-/.]){re.escape(needle)}(?![\w])(?![-/.]\d)"
    )
    for m in pattern.finditer(haystack):
        window = haystack[max(0, m.start() - 12):m.end() + 12]
        inside_money = any(
            mm.start() <= m.start() - max(0, m.start() - 12) and m.end() - max(0, m.start() - 12) <= mm.end()
            for mm in re.finditer(r"\$\s?[\d,]+(?:\.\d{1,2})?", window)
        )
        if not inside_money:
            return True
    return False


def verify(plan: TransformationPlan, output_path: str) -> VerificationReport:
    report = VerificationReport(output_path=output_path)

    try:
        out = pymupdf.open(output_path)
    except Exception as exc:
        report.add("pdf integrity", False, f"output could not be reopened: {exc}")
        return report

    try:
        src = pymupdf.open(plan.source_path)
        try:
            report.add(
                "pdf integrity",
                not out.is_repaired and out.page_count > 0,
                "reopened cleanly" if not out.is_repaired else "PDF required repair on reopen",
            )
            report.add(
                "page count preserved",
                out.page_count == src.page_count,
                f"{src.page_count} -> {out.page_count}",
            )

            out_text = "\n".join(page.get_text() for page in out)
            src_text = "\n".join(page.get_text() for page in src)
            out_norm = _normalize(out_text)

            _check_originals_absent(report, plan, out_norm)
            _check_replacements_present(report, plan, out_norm)
            _check_replacement_colour(report, plan, out)
            _check_skipped_preserved(report, plan, out_norm)
            _check_group_completeness(report, plan, out_norm)
            _check_partial_redaction(report, plan, out_norm)
            _check_group_residue(report, plan, out_norm)
            _check_financials(report, src_text, out_text)
            _check_unsupported_content(report, plan)
            _check_layout_sanity(report, plan, out)
        finally:
            src.close()
    finally:
        out.close()

    return report


# -- individual checks ------------------------------------------------------


def _check_originals_absent(report: VerificationReport, plan: TransformationPlan, out_norm: str) -> None:
    survivors = sorted(
        {
            t.candidate_id
            for t in plan.targets
            if _contains_value(out_norm, t.original)
        }
    )
    report.add(
        "accepted originals removed",
        not survivors,
        "all accepted originals absent from the saved file"
        if not survivors
        else f"{len(survivors)} accepted value(s) still extractable from the output",
    )


def _check_replacements_present(report: VerificationReport, plan: TransformationPlan, out_norm: str) -> None:
    missing = [t.candidate_id for t in plan.targets if not _contains_value(out_norm, t.replacement)]
    report.add(
        "replacements present",
        not missing,
        "all replacements found" if not missing else f"{len(missing)} replacement(s) missing from output",
    )


def _check_replacement_colour(report: VerificationReport, plan: TransformationPlan, out: pymupdf.Document) -> None:
    if not plan.targets:
        report.add("replacement text is red", True, "no targets", critical=False)
        return
    red_spans: list[str] = []
    for page in out:
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if int(span.get("color", 0)) == RED_INT:
                        red_spans.append(_normalize(span.get("text", "")))
    joined = " ".join(s for s in red_spans if s)
    not_red = [t.candidate_id for t in plan.targets if _normalize(t.replacement) not in joined]
    report.add(
        "replacement text is red",
        not not_red,
        f"{len(red_spans)} red span(s) in output"
        if not not_red
        else f"{len(not_red)} replacement(s) are not rendered as red text",
    )


def _check_skipped_preserved(report: VerificationReport, plan: TransformationPlan, out_norm: str) -> None:
    lost = [v for v in plan.skipped_values if _normalize(v) and not _contains_value(out_norm, v)]
    report.add(
        "skipped values preserved",
        not lost,
        "skipped values intact" if not lost else f"{len(lost)} skipped value(s) were removed anyway",
    )


def _check_group_completeness(report: VerificationReport, plan: TransformationPlan, out_norm: str) -> None:
    incomplete = []
    for group_id, member_ids in plan.group_membership.items():
        members = [t for t in plan.targets if t.candidate_id in member_ids]
        if any(_contains_value(out_norm, t.original) for t in members):
            incomplete.append(group_id)
    report.add(
        "logical field groups fully transformed",
        not incomplete,
        "all group members transformed"
        if not incomplete
        else f"group(s) {', '.join(incomplete)} only partially transformed",
    )


def _check_partial_redaction(report: VerificationReport, plan: TransformationPlan, out_norm: str) -> None:
    """Catch residual fragments of an accepted value (spec section 39)."""
    residual: list[str] = []
    for t in plan.targets:
        for token in re.split(r"[\s,]+", t.original.strip()):
            token = token.strip(".,;:()")
            if len(token) < MIN_RESIDUAL_TOKEN:
                continue
            if token.lower() in _normalize(t.replacement):
                continue
            if re.search(rf"(?<![\w-]){re.escape(token.lower())}(?![\w-])", out_norm):
                residual.append(f"{t.candidate_id}:{len(token)}chars")
    report.add(
        "no partial redaction",
        not residual,
        "no residual fragments of accepted values"
        if not residual
        else f"{len(residual)} fragment(s) of accepted values survive in the output",
    )


def _check_group_residue(report: VerificationReport, plan: TransformationPlan, out_norm: str) -> None:
    """Every value line of an accepted field group must be gone (section 39).

    Token-level checks miss the case where a detector covered part of a value
    line and the rest survived, so this checks the ORIGINAL LINE text.
    """
    residue: list[str] = []
    for group_id, texts in plan.group_value_texts.items():
        member_ids = set(plan.group_membership.get(group_id, []))
        replacement_text = _normalize(
            " ".join(t.replacement for t in plan.targets if t.candidate_id in member_ids)
        )
        for original_line in texts:
            for token in re.split(r"[\s,]+", original_line.strip()):
                token = token.strip(".,;:()")
                if len(token) < 3:
                    continue
                if token.lower() in replacement_text:
                    continue
                if _contains_value(out_norm, token):
                    residue.append(f"{group_id}:{token[:2]}...")
    report.add(
        "no residual text in accepted field groups",
        not residue,
        "all group value lines fully transformed"
        if not residue
        else f"{len(residue)} fragment(s) of accepted field group values survive",
    )


def _check_financials(report: VerificationReport, src_text: str, out_text: str) -> None:
    before, after = financial_tokens(src_text), financial_tokens(out_text)
    lost = sorted(set(before) - set(after))
    report.add(
        "financial values preserved",
        not lost,
        f"{len(before)} financial token(s) preserved"
        if not lost
        else f"{len(lost)} financial token(s) changed or lost",
    )


def _check_unsupported_content(report: VerificationReport, plan: TransformationPlan) -> None:
    pages = plan.ocr_required_pages
    report.add(
        "all pages analysed",
        not pages,
        "every page had extractable text"
        if not pages
        else "OCR REQUIRED on page(s) "
        + ", ".join(str(p + 1) for p in pages)
        + " - these were NOT analysed and this document is NOT fully anonymised",
    )


def _check_layout_sanity(report: VerificationReport, plan: TransformationPlan, out: pymupdf.Document) -> None:
    """Cheap visual sanity: replacement text must land inside the page (section 40)."""
    off_page = 0
    for t in plan.targets:
        page = out[t.page_no]
        if not pymupdf.Rect(t.rect).intersects(page.rect):
            off_page += 1
    report.add(
        "replacement geometry sane",
        off_page == 0,
        "all replacements inside page bounds"
        if off_page == 0
        else f"{off_page} replacement(s) fall outside the page",
        critical=False,
    )


def format_report(report: VerificationReport) -> str:
    lines = [
        f"{report.status}  ({report.output_path})",
        "  scope: confirms the plan was executed on the saved file; it does not",
        "         prove detection was complete.",
    ]
    for check in report.checks:
        mark = "PASS" if check.passed else ("FAIL" if check.critical else "WARN")
        lines.append(f"  [{mark}] {check.name}: {check.detail}")
    return "\n".join(lines)
