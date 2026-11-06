"""QA runner: process documents with the REAL application and verify the output
independently. Writes only sanitized results - no original value, no snippet.

    python buildtools/qa_run.py INPUT.pdf [INPUT.pdf ...] --out qa_outputs/run \
        --names "PII_Sample_1_Redacted_QA.pdf" ...

What it runs is the application's own pipeline, nothing re-implemented:
`AnonymizationSession.analyse()` (every detection layer) ->
every detection accepted (the unattended equivalent of the CLI's
`--accept-all`; in the GUI a reviewer decides each flagged item) ->
`session.process()` (the single transformation path, export.redactor) ->
the application's own verifier.

Then a SECOND, independent verification, written separately from the app's
verifier so one cannot vouch for the other:
  1. the file exists, opens, keeps page count and page sizes
  2. every applied original value is searched for in the extracted text of the
     output (whitespace/case-normalised), compared with the occurrences the plan
     knowingly left (e.g. a skipped or never-detected occurrence)
  3. each redaction rectangle is clipped and its surviving glyphs inspected:
     no original token may remain inside it
  4. annotations, form widgets, metadata, embedded files and bookmarks are
     inspected for applied originals
  5. text OUTSIDE every redaction rectangle is compared word-for-word with the
     input: the share preserved measures collateral damage
Only counts, categories, page numbers and rectangles are written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DOCANON_LLM", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf  # noqa: E402


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def _inside(word_rect, rect, pad=0.5) -> bool:
    x0, y0, x1, y1 = word_rect
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return rect[0] - pad <= cx <= rect[2] + pad and rect[1] - pad <= cy <= rect[3] + pad


def independent_verify(src: str, out: str, plan) -> dict:
    checks: dict[str, dict] = {}
    if not Path(out).exists():
        return {"file_exists": {"passed": False, "detail": "output missing"}}
    try:
        dst_doc = pymupdf.open(out)
    except Exception as exc:  # noqa: BLE001
        return {"file_opens": {"passed": False, "detail": type(exc).__name__}}
    src_doc = pymupdf.open(src)
    checks["file_opens"] = {"passed": True, "detail": "opened"}
    same_pages = src_doc.page_count == dst_doc.page_count
    same_sizes = same_pages and all(
        tuple(round(v, 1) for v in src_doc[i].rect) == tuple(round(v, 1) for v in dst_doc[i].rect)
        for i in range(src_doc.page_count)
    )
    checks["page_count_and_sizes"] = {
        "passed": bool(same_pages and same_sizes),
        "detail": f"{src_doc.page_count} -> {dst_doc.page_count} pages; sizes {'identical' if same_sizes else 'CHANGED'}",
    }

    applied = [t for t in plan.targets]
    out_text = _norm("\n".join(p.get_text() for p in dst_doc))
    src_text = _norm("\n".join(p.get_text() for p in src_doc))
    leaks = 0
    leak_types: Counter = Counter()
    leak_pages: dict = {}
    seen: set[str] = set()
    for t in applied:
        value = _norm(t.original)
        if len(value) < 3 or value in seen:
            continue
        seen.add(value)
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(value) + r"(?![a-z0-9])")
        remaining = len(pattern.findall(out_text))
        allowed = int(plan.expected_residual_counts.get(value, 0)) if plan.expected_residual_counts else 0
        # A replacement can legitimately equal a value elsewhere only by chance;
        # count it against the leak but never print it.
        if remaining > allowed:
            leaks += 1
            leak_types[t.pii_type.value] += 1
            leak_pages.setdefault(t.pii_type.value, set()).update(
                i + 1 for i, page in enumerate(dst_doc) if pattern.search(_norm(page.get_text())))
    checks["applied_values_not_extractable"] = {
        "passed": leaks == 0,
        "detail": f"{len(seen)} distinct applied value(s) searched; {leaks} still extractable beyond the plan's known residual",
        "by_type": dict(leak_types),
        "pages_by_type": {k: sorted(v) for k, v in leak_pages.items()},
    }

    # 3. glyphs inside each redaction rectangle
    residue = 0
    for t in applied:
        page = dst_doc[t.page_no]
        orig_tokens = {tok.lower() for tok in re.findall(r"\w+", t.original) if len(tok) >= 2}
        # A token the replacement legitimately repeats (a kept "LLC", a label
        # word inside a value) is not residue of the original.
        orig_tokens -= {tok.lower() for tok in re.findall(r"\w+", t.replacement or "")}
        words = page.get_text("words", clip=pymupdf.Rect(t.rect))
        for w in words:
            if w[4].strip(".,;:()").lower() in orig_tokens and _inside(w[:4], t.rect):
                # red replacement text is allowed; an original token is not
                residue += 1
                break
    checks["no_original_glyphs_inside_redaction_boxes"] = {
        "passed": residue == 0,
        "detail": f"{len(applied)} box(es) inspected; {residue} still show an original token",
    }

    # 4. hidden structures
    hidden_hits = 0
    annots = widgets = 0
    for page in dst_doc:
        for a in page.annots() or []:
            annots += 1
            content = _norm((a.info or {}).get("content", "") + " " + (a.info or {}).get("title", ""))
            hidden_hits += any(_norm(v) in content for v in seen if v)
        for w in page.widgets() or []:
            widgets += 1
            hidden_hits += any(_norm(v) in _norm(str(w.field_value or "")) for v in seen if v)
    meta = _norm(" ".join(str(v) for v in (dst_doc.metadata or {}).values() if v))
    hidden_hits += sum(1 for v in seen if v and v in meta)
    toc = _norm(" ".join(item[1] for item in dst_doc.get_toc()))
    hidden_hits += sum(1 for v in seen if v and v in toc)
    embedded = dst_doc.embfile_count()
    checks["hidden_structures_clean"] = {
        "passed": hidden_hits == 0 and embedded == 0,
        "detail": f"{annots} annotation(s), {widgets} form field(s), {embedded} embedded file(s); "
                  f"{hidden_hits} applied value(s) found in annotations/fields/metadata/bookmarks",
    }

    # 5. preservation of everything outside the redaction boxes
    kept = total = 0
    for i in range(min(src_doc.page_count, dst_doc.page_count)):
        rects = [t.rect for t in applied if t.page_no == i]
        src_words = [w for w in src_doc[i].get_text("words") if not any(_inside(w[:4], r) for r in rects)]
        dst_words = Counter((w[4], round(w[0]), round(w[1])) for w in dst_doc[i].get_text("words"))
        for w in src_words:
            total += 1
            key = (w[4], round(w[0]), round(w[1]))
            if dst_words.get(key, 0) > 0:
                dst_words[key] -= 1
                kept += 1
    share = kept / total if total else 1.0
    checks["unrelated_text_preserved"] = {
        "passed": share >= 0.995,
        "detail": f"{kept}/{total} words outside redaction boxes unchanged ({share:.2%})",
    }
    no_text_pages = [i + 1 for i in range(src_doc.page_count) if not src_doc[i].get_text().strip()]
    checks["pages_without_native_text"] = {
        "passed": True,
        "detail": f"{len(no_text_pages)} page(s) carry no extractable text and were NOT analysed (no OCR, by scope)",
        "pages": no_text_pages,
    }
    src_doc.close()
    dst_doc.close()
    _ = src_text
    return checks


def status_from(app_report, checks: dict) -> str:
    critical = ["file_opens", "page_count_and_sizes", "applied_values_not_extractable",
                "no_original_glyphs_inside_redaction_boxes", "hidden_structures_clean",
                "replacements_red", "one_identity_per_person", "joint_filers_kept_apart",
                "no_boxes_or_patches", "filename_pseudonymised"]
    if any(not checks.get(k, {}).get("passed", False) for k in critical):
        return "FAIL"
    soft_ok = checks.get("unrelated_text_preserved", {}).get("passed", False) and app_report.passed
    no_text = checks.get("pages_without_native_text", {}).get("pages")
    return "PASS" if soft_ok and not no_text else "PARTIAL PASS"


def identity_checks(src: str, out: str, plan, session) -> dict:
    """Phase-2 checks: red replacements, one identity per person, joint filers
    kept apart, no boxes drawn, filename free of original names. Counts only."""
    from app.detection.types import PiiType

    checks: dict[str, dict] = {}
    red, not_red = 0, 0
    with pymupdf.open(out) as dst:
        spans = [(i, sp) for i, page in enumerate(dst) for b in page.get_text("dict")["blocks"]
                 for l in b.get("lines", []) for sp in l["spans"] if sp["text"].strip()]
        drawings_out = sum(len(p.get_drawings()) for p in dst)
    with pymupdf.open(src) as s_doc:
        drawings_in = sum(len(p.get_drawings()) for p in s_doc)
    by_page: dict[int, list] = {}
    for i, sp in spans:
        by_page.setdefault(i, []).append(sp)
    for t in plan.targets:
        if t.blackout or not t.replacement:
            continue
        hits = [sp for sp in by_page.get(t.page_no, []) if sp["text"].strip() == t.replacement.strip()]
        if hits:
            red += 1
            not_red += sum(1 for sp in hits if sp["color"] != 0xFF0000)
    checks["replacements_red"] = {
        "passed": not_red == 0,
        "detail": f"{red} placed replacement(s) inspected; {not_red} not in the configured red",
    }
    # one identity per person: every lone name token maps as it does inside full names
    names = session.registry.names
    conflicts = 0
    person = [t for t in plan.targets if t.pii_type is PiiType.PERSON and t.replacement]
    for t in person:
        o_tokens = [w for w in re.split(r"[\s,&]+", t.original) if w]
        r_tokens = [w for w in re.split(r"[\s,&]+", t.replacement) if w]
        if len(o_tokens) != len(r_tokens):
            continue
        for o, r in zip(o_tokens, r_tokens):
            known = names.known(re.sub(r"['\u2019]s$", "", o)) if names else None
            if known and known.lower() != re.sub(r"['\u2019]s$", "", r).lower():
                conflicts += 1
    checks["one_identity_per_person"] = {
        "passed": conflicts == 0,
        "detail": f"{len(person)} person replacement(s); {conflicts} token(s) disagreeing with the person's registered synthetic name",
    }
    joint_bad = 0
    joint = [t for t in person if re.search(r"\s(&|and)\s", t.original)]
    for t in joint:
        if len(re.findall(r"\s(?:&|and)\s", t.original)) != len(re.findall(r"\s(?:&|and)\s", t.replacement)):
            joint_bad += 1
    checks["joint_filers_kept_apart"] = {
        "passed": joint_bad == 0,
        "detail": f"{len(joint)} joint-name replacement(s); {joint_bad} lost a person or separator",
    }
    checks["no_boxes_or_patches"] = {
        "passed": drawings_out <= drawings_in,
        "detail": f"vector drawings {drawings_in} in input, {drawings_out} in output",
    }
    originals = set()
    for t in person:
        originals.update(w.lower() for w in re.findall(r"[^\W\d_]{2,}", t.original))
    name_words = {w.lower() for w in re.findall(r"[^\W\d_]{2,}", Path(out).stem)}
    leaked = len(name_words & originals)
    checks["filename_pseudonymised"] = {
        "passed": leaked == 0,
        "detail": f"{leaked} original name word(s) in the output filename; "
                  f"{len(getattr(session, 'filename_review', []))} filename word(s) listed for review",
    }
    return checks


def run_one(src: str, out: str, label: str, preview_dir: Path | None, accept_all: bool = False) -> dict:
    """`out` may be a directory: the output is then named by the application's
    own pseudonymised filename (session.safe_output_name)."""
    from app.decisions.manager import DecisionState
    from app.session import AnonymizationSession

    started = time.time()
    session = AnonymizationSession(source_path=src)
    out_path = Path(out)
    private = (out_path if out_path.is_dir() else out_path.parent) / "_private_mappings"
    session.mapping_folder = str(private)
    session.analyse()
    analysed_at = time.time()
    if accept_all:
        session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)
    if out_path.is_dir():
        out = str(out_path / session.safe_output_name())
    apply_report, app_report = session.process(out)
    finished = time.time()
    plan = session.plan()
    checks = independent_verify(src, out, plan)
    checks.update(identity_checks(src, out, plan, session))
    status = status_from(app_report, checks)

    detections = []
    for idx, t in enumerate(plan.targets, start=1):
        cand = next((c for c in session.candidates if c.id == t.candidate_id), None)
        detections.append({
            "detection_id": f"{label}-D{idx:04d}",
            "page": t.page_no + 1,
            "category": t.pii_type.value,
            "bbox": [round(v, 2) for v in t.rect],
            "confidence": round(cand.confidence, 3) if cand else None,
            "source": cand.source.value if cand else None,
            "needs_review": bool(cand.needs_review) if cand else None,
            "redaction_status": "blackout" if t.blackout else "redacted_and_replaced",
            "verification_status": "verified_absent" if checks["applied_values_not_extractable"]["passed"] else "see_document_checks",
        })
    unresolved = [{"page": c.page_no + 1, "category": c.pii_type.value, "bbox": [round(v, 2) for v in c.rect],
                   "redaction_status": "not_applied_untyped", "failure_reason": "no detector could type this value; left readable for a reviewer"}
                  for c in session.candidates if c.pii_type.value == "UNCLASSIFIED_GROUP_VALUE"]
    previews = []
    if preview_dir is not None:
        preview_dir.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(out) as d:
            for i, page in enumerate(d):
                path = preview_dir / f"{Path(out).stem}_p{i + 1:03d}.png"
                page.get_pixmap(dpi=60).save(str(path))
                previews.append(str(path))
    return {
        "document_id": label,
        "output_file": Path(out).name,
        "pages": session.document.page_count,
        "status": status,
        "app_status": session.status,
        "seconds_analyse": round(analysed_at - started, 1),
        "seconds_total": round(finished - started, 1),
        "detections_total": len(session.candidates),
        "targets_applied": len(plan.targets),
        "distinct_values": len({_norm(t.original) for t in plan.targets}),
        "targets_by_category": dict(Counter(t.pii_type.value for t in plan.targets)),
        "flagged_for_review": sum(1 for c in session.candidates if c.needs_review),
        "redactions_written": apply_report.redacted,
        "replacements_written": apply_report.inserted,
        "app_verifier": [{"check": c.name, "passed": c.passed, "critical": getattr(c, "critical", None)}
                         for c in app_report.checks],
        "independent_checks": checks,
        "detections": detections,
        "unresolved": unresolved,
        "held_for_review": [
            {"page": c.page_no + 1, "category": c.pii_type.value, "bbox": [round(v, 2) for v in c.rect],
             "reason": "ambiguous name component" if getattr(c, "ambiguous", False) else "not confirmed (default: not applied)"}
            for c in session.candidates if not session.decisions.is_actionable(c)
        ],
        "previews": previews,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--names", nargs="*")
    ap.add_argument("--labels", nargs="*")
    ap.add_argument("--previews", action="store_true")
    ap.add_argument("--accept-all", action="store_true",
                    help="apply every detection (the CLI --accept-all); default: the app's own decisions")
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for i, src in enumerate(args.inputs):
        label = args.labels[i] if args.labels else f"Document {i + 1}"
        if args.names:
            out = str(out_dir / args.names[i])
        else:   # the application's own pseudonymised filename, one folder per input
            sub = out_dir / f"doc{i + 1}"
            sub.mkdir(parents=True, exist_ok=True)
            out = str(sub)
        try:
            r = run_one(src, out, label, out_dir / "previews" if args.previews else None,
                        accept_all=args.accept_all)
        except Exception as exc:  # noqa: BLE001 - report, never fake an output
            r = {"document_id": label, "status": "NOT RUN", "failure_reason": f"{type(exc).__name__}"}
        results.append(r)
        print(f"{label}: {r['status']} ({r.get('targets_applied', 0)} redactions, {r.get('seconds_total', '?')}s)")
    Path(args.json or out_dir / "qa_results.json").write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
