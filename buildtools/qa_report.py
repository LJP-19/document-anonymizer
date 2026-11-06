"""Build the sanitized QA report (PDF) and machine-readable results (JSON).

Inputs are files the QA tools already wrote, none of which contain values:
  --results   qa_run.py output (per-document checks, detections as rectangles)
  --metrics   a JSON with before/after scores and environment notes
Output: PII_Redaction_QA_Report.pdf and PII_Redaction_QA_Results.json.

The report never contains an original value or a text snippet. Placeholders
([PERSON_001] ...) are used wherever an individual item is referred to.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

STYLES = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=STYLES["BodyText"], fontSize=9, leading=12)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=7.5, leading=9.5)
H1 = ParagraphStyle("h1", parent=STYLES["Heading1"], fontSize=16)
H2 = ParagraphStyle("h2", parent=STYLES["Heading2"], fontSize=12, spaceBefore=10)


def _table(rows, widths=None, header=True):
    data = [[Paragraph(str(c), SMALL) for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8ecf2")))
    t.setStyle(TableStyle(style))
    return t


def _pct(v):
    return "not measured" if v is None else f"{v:.3f}"


def build_pdf(results: list, metrics: dict, out_pdf: Path) -> None:
    doc = SimpleDocTemplate(str(out_pdf), pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch, title="PII Redaction QA Report")
    s = []
    s.append(Paragraph("PII Redaction QA Report", H1))
    s.append(Paragraph(metrics.get("summary", ""), BODY))
    s.append(Spacer(1, 6))

    s.append(Paragraph("Repository and environment", H2))
    env = metrics.get("environment", {})
    s.append(_table([["Item", "Value"]] + [[k, v] for k, v in env.items()], [2.0 * inch, 5.2 * inch]))

    s.append(Paragraph("Per-document status", H2))
    rows = [["Document", "Pages", "Status", "Redactions", "Distinct values", "Flagged for review",
             "No-text pages", "Seconds"]]
    for r in results:
        if r.get("status") == "NOT RUN":
            rows.append([r["document_id"], "-", "NOT RUN", "-", "-", "-", "-", r.get("failure_reason", "")])
            continue
        nt = r["independent_checks"].get("pages_without_native_text", {}).get("pages", [])
        rows.append([r["document_id"], r["pages"], r["status"], r["targets_applied"], r["distinct_values"],
                     r["flagged_for_review"], ", ".join(map(str, nt)) or "none", r["seconds_total"]])
    s.append(_table(rows))
    s.append(Paragraph(
        "Status meaning: PASS = every applicable check passed; PARTIAL PASS = the redaction checks passed but "
        "a known limitation remains (e.g. a page with no extractable text was not analysed, or the app's own "
        "verifier flagged an item); FAIL = an applied value is still extractable or a hidden structure carries "
        "it; NOT RUN = processing did not complete. These are QA outputs for inspection, not approved "
        "production documents.", SMALL))

    s.append(Paragraph("Before / after accuracy", H2))
    for block in metrics.get("comparisons", []):
        s.append(Paragraph(block["title"], BODY))
        cols = block.get("columns") or ["Metric", "Original (v0.38.2)", "Improved", "Change"]
        rows = [cols] + [list(r) for r in block["rows"]]
        first = 2.0 * inch
        rest = (7.2 * inch - first) / (len(cols) - 1)
        s.append(_table(rows, [first] + [rest] * (len(cols) - 1)))
        if block.get("note"):
            s.append(Paragraph(block["note"], SMALL))
        s.append(Spacer(1, 6))

    s.append(Paragraph("Per-category results (real samples, improved build)", H2))
    cat = metrics.get("real_categories_improved", {})
    rows = [["Ground-truth category", "Redacted (TP)", "Missed (FN)", "Wrongly redacted (FP)", "Kept (TN)"]]
    for k, v in sorted(cat.items()):
        rows.append([k, v.get("tp", 0), v.get("fn", 0), v.get("fp", 0), v.get("tn", 0)])
    s.append(_table(rows))

    s.append(PageBreak())
    s.append(Paragraph("Independent verification by document", H2))
    for r in results:
        if r.get("status") == "NOT RUN":
            continue
        s.append(Paragraph(f"{r['document_id']} - {r['output_file']} - {r['status']}", BODY))
        rows = [["Independent check", "Result", "Detail"]]
        for k, v in r["independent_checks"].items():
            rows.append([k.replace("_", " "), "PASS" if v["passed"] else "FAIL", v["detail"]])
        s.append(_table(rows, [2.2 * inch, 0.6 * inch, 4.4 * inch]))
        rows = [["Application verifier check", "Result"]]
        for c in r["app_verifier"]:
            rows.append([c["check"], "PASS" if c["passed"] else ("FAIL" if c.get("critical", True) else "WARN")])
        s.append(_table(rows, [5.0 * inch, 2.2 * inch]))
        cats = Counter(d["category"] for d in r["detections"])
        s.append(Paragraph("Redactions by category: " + ", ".join(f"{k} {v}" for k, v in sorted(cats.items())), SMALL))
        s.append(Spacer(1, 8))

    s.append(Paragraph("Identity, colour and filename checks", H2))
    rows = [["Document", "Output filename", "Red replacements", "One identity per person",
             "Joint filers apart", "No boxes/patches", "Filename"]]
    for r in results:
        if r.get("status") == "NOT RUN":
            continue
        ic = r["independent_checks"]
        def st(k):
            v = ic.get(k)
            return "not run" if v is None else ("PASS" if v["passed"] else "FAIL")
        rows.append([r["document_id"], r["output_file"], st("replacements_red"), st("one_identity_per_person"),
                     st("joint_filers_kept_apart"), st("no_boxes_or_patches"), st("filename_pseudonymised")])
    s.append(_table(rows))

    s.append(Paragraph("Held for review (detected, NOT applied)", H2))
    s.append(Paragraph(
        "Only confirmed PII is applied by default. The items below were detected but not confirmed - no type, too "
        "little evidence, or an ambiguous name component - so they were left readable and listed for a reviewer. "
        "Only page, category and reason are given; no value is shown.", SMALL))
    rows = [["Document", "Page", "Category", "Reason"]]
    for r in results:
        for h in r.get("held_for_review", []):
            rows.append([r["document_id"], h["page"], h["category"], h["reason"]])
    if len(rows) == 1:
        rows.append(["-", "-", "-", "nothing held"])
    s.append(_table(rows, [1.0 * inch, 0.6 * inch, 2.2 * inch, 3.4 * inch]))

    if metrics.get("mapping_storage"):
        s.append(Paragraph("Mapping registry: where it is and how to remove it", H2))
        for item in metrics["mapping_storage"]:
            s.append(Paragraph("&bull; " + item, BODY))

    s.append(Paragraph("Findings and remaining problems", H2))
    for item in metrics.get("findings", []):
        s.append(Paragraph("&bull; " + item, BODY))
    s.append(Paragraph("Limitations", H2))
    for item in metrics.get("limitations", []):
        s.append(Paragraph("&bull; " + item, BODY))
    doc.build(s)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--metrics", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args(argv)
    results = json.loads(Path(args.results).read_text())
    metrics = json.loads(Path(args.metrics).read_text())
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    build_pdf(results, metrics, out / "PII_Redaction_QA_Report.pdf")
    machine = {
        "report_version": 2,
        "summary": metrics.get("summary", ""),
        "mapping_storage": metrics.get("mapping_storage", []),
        "findings": metrics.get("findings", []),
        "limitations": metrics.get("limitations", []),
        "environment": metrics.get("environment", {}),
        "comparisons": metrics.get("comparisons", []),
        "documents": [
            {k: v for k, v in r.items() if k not in ("previews",)} for r in results
        ],
    }
    (out / "PII_Redaction_QA_Results.json").write_text(json.dumps(machine, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
