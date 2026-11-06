"""False-positive benchmark: run the REAL CLI with the app's DEFAULT decisions on
documents that are almost entirely non-PII, and score the written files.

Reports, per document and in total: words wrongly changed (false positives), the
false-positive rate, precision, recall on the few PII values present, the
redaction AREA spent on non-PII words, and false positives by category.

    python -m benchmark.run_fp_benchmark --out build/fp_benchmark [--json result.json]

Release gate (owner, 0.41.0): zero false positives on this corpus, precision >= 0.99.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmark.false_positive_corpus import build_all  # noqa: E402
from benchmark.output_scoring import NOT_PII, PII, extract_words, kept_mask, score  # noqa: E402
from benchmark.run_forms_benchmark import geometric_labeller, run_app  # noqa: E402


def run(out_dir: Path, app_root: Path = ROOT) -> dict:
    import os
    import subprocess
    import time

    corpus = build_all(out_dir / "inputs")
    results, total = {}, None
    area_total = 0.0
    for name, (path, items) in corpus.items():
        out_pdf = str(out_dir / f"{name}.out.pdf")
        env = dict(os.environ, DOCANON_LLM=os.environ.get("DOCANON_LLM", "0"), QT_QPA_PLATFORM="offscreen")
        started = time.time()
        proc = subprocess.run([sys.executable, "-m", "app.cli", "process", path, "-o", out_pdf],
                              cwd=str(app_root), env=env, capture_output=True, text=True)
        if not Path(out_pdf).exists():
            results[name] = {"error": f"no output (exit {proc.returncode})", "log": (proc.stdout + proc.stderr)[-1500:]}
            continue
        src_words = extract_words(path)
        kept = kept_mask(src_words, extract_words(out_pdf))
        label = geometric_labeller(items)
        s = score(src_words, kept, label)
        area = 0.0
        for p, page in enumerate(src_words):
            for i, w in enumerate(page):
                lab, _cat = label(w)
                if lab == NOT_PII and not kept[p][i]:
                    x0, y0, x1, y1 = w.bbox
                    area += (x1 - x0) * (y1 - y0)
        area_total += area
        results[name] = {"exit": proc.returncode, "seconds": round(time.time() - started, 1), **s.as_dict(),
                         "fp_area_pt2": round(area, 1), "fp_pages": s.fp_pages}
        if total is None:
            total = s
        else:
            for k in ("tp", "fp", "fn", "tn", "excluded"):
                setattr(total, k, getattr(total, k) + getattr(s, k))
            for cat, v in s.by_category.items():
                agg = total.by_category.setdefault(cat, {"tp": 0, "fp": 0, "fn": 0, "tn": 0})
                for k in v:
                    agg[k] += v[k]
    if total is not None:
        results["_total"] = {**total.as_dict(), "fp_area_pt2": round(area_total, 1)}
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "build" / "fp_benchmark"))
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    results = run(Path(args.out).resolve())
    for name, r in results.items():
        if "error" in r:
            print(f"{name}: {r['error']}")
            continue
        print(f"{name:18} FP={r['fp']} (rate {r['false_positive_rate']:.4f}, area {r['fp_area_pt2']} pt2) "
              f"P={r['precision']:.3f} R={r['recall']:.3f} tp={r['tp']} fn={r['fn']} tn={r['tn']}")
    tot = results.get("_total", {})
    for cat, v in sorted(tot.get("by_category", {}).items()):
        print(f"   {cat:22} {v}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
