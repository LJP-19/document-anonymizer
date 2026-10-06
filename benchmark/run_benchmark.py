"""Precision/recall benchmark harness (spec section 23).

Every fixture in `benchmark/fixtures/` is a small script building a PDF plus
a list of GOLD values: what SHOULD be detected, and what must NEVER be. This
runs full detection against each, compares against the gold list by exact
normalized text, and reports precision/recall/F1 - overall, per type, and
per document, plus the one number that matters most for a redaction tool:
whether ANY document had a missed true positive at all, since a single
missed SSN matters far more than a hundred correctly-handled words.

Run:
    python benchmark/run_benchmark.py
    python benchmark/run_benchmark.py --json report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.document.provider import NativePdfTextProvider
from app.detection.engine import analyse


@dataclass
class GoldValue:
    text: str
    pii_type: str  # PiiType.value, or "" to match any type
    should_detect: bool = True  # False = this text must NEVER be flagged


@dataclass
class Fixture:
    name: str
    build: Callable[[Path], str]
    gold: list[GoldValue]
    category: str = ""


@dataclass
class DocResult:
    fixture: str
    category: str
    true_positives: list[str] = field(default_factory=list)
    false_negatives: list[str] = field(default_factory=list)  # missed, should detect
    false_positives: list[str] = field(default_factory=list)  # flagged, should not
    true_negatives: list[str] = field(default_factory=list)  # correctly not flagged


def _normalize(text: str) -> str:
    return " ".join(text.split()).strip(".,;: ").lower()


def run_fixture(fixture: Fixture, tmp_dir: Path, use_ner: bool = True) -> DocResult:
    path = fixture.build(tmp_dir / f"{fixture.name}.pdf")
    doc = NativePdfTextProvider().load(path)
    result = analyse(doc, use_ner=use_ner, use_presidio=False)
    found = {_normalize(c.text): c.pii_type.value for c in result.candidates}

    out = DocResult(fixture=fixture.name, category=fixture.category)
    for gold in fixture.gold:
        key = _normalize(gold.text)
        detected = key in found
        if gold.should_detect:
            if detected:
                out.true_positives.append(gold.text)
            else:
                out.false_negatives.append(gold.text)
        else:
            if detected:
                out.false_positives.append(gold.text)
            else:
                out.true_negatives.append(gold.text)
    return out


def aggregate(results: list[DocResult]) -> dict:
    tp = sum(len(r.true_positives) for r in results)
    fp = sum(len(r.false_positives) for r in results)
    fn = sum(len(r.false_negatives) for r in results)
    tn = sum(len(r.true_negatives) for r in results)

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    docs_with_a_miss = [r.fixture for r in results if r.false_negatives]

    by_category: dict[str, dict] = {}
    for r in results:
        bucket = by_category.setdefault(r.category, {"tp": 0, "fp": 0, "fn": 0, "tn": 0})
        bucket["tp"] += len(r.true_positives)
        bucket["fp"] += len(r.false_positives)
        bucket["fn"] += len(r.false_negatives)
        bucket["tn"] += len(r.true_negatives)

    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "true_negatives": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "false_positive_rate": round(fp / (fp + tn), 4) if (fp + tn) else 0.0,
        "documents_total": len(results),
        "documents_with_any_missed_pii": len(docs_with_a_miss),
        "documents_with_any_missed_pii_names": docs_with_a_miss,
        "by_category": by_category,
        "per_document": [
            {
                "fixture": r.fixture,
                "category": r.category,
                "true_positives": r.true_positives,
                "false_negatives": r.false_negatives,
                "false_positives": r.false_positives,
            }
            for r in results
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=str, default=None, help="write the full report here")
    parser.add_argument("--no-ner", action="store_true", help="disable spaCy NER for this run")
    args = parser.parse_args()

    from benchmark.fixtures import ALL_FIXTURES

    tmp_dir = Path(__file__).resolve().parent / "_scratch"
    tmp_dir.mkdir(exist_ok=True)

    results = [run_fixture(f, tmp_dir, use_ner=not args.no_ner) for f in ALL_FIXTURES]
    report = aggregate(results)

    print(f"Documents:            {report['documents_total']}")
    print(f"Precision:            {report['precision']:.3f}")
    print(f"Recall:               {report['recall']:.3f}")
    print(f"F1:                   {report['f1']:.3f}")
    print(f"False positive rate:  {report['false_positive_rate']:.3f}")
    print(f"TP / FP / FN / TN:    {report['true_positives']} / {report['false_positives']} "
          f"/ {report['false_negatives']} / {report['true_negatives']}")
    print(f"Docs with ANY missed PII: {report['documents_with_any_missed_pii']} "
          f"of {report['documents_total']}")
    if report["documents_with_any_missed_pii_names"]:
        print("  ->", ", ".join(report["documents_with_any_missed_pii_names"]))

    print("\nBy category:")
    for category, counts in sorted(report["by_category"].items()):
        tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
        p = tp / (tp + fp) if (tp + fp) else 1.0
        r = tp / (tp + fn) if (tp + fn) else 1.0
        print(f"  {category:20} precision={p:.2f}  recall={r:.2f}  (tp={tp} fp={fp} fn={fn})")

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(f"\nfull report written to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
