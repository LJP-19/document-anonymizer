"""Output-level precision/recall on the synthetic organizational-form corpus.

Runs the REAL application (its own CLI: analyse, accept every detection,
redact, export, verify) on every fixture in `forms_corpus.py`, then scores the
WRITTEN file word by word against geometric ground truth: a PII word that is
still present unchanged is a miss; a non-PII word that changed is a false
positive (including collateral damage from an oversized redaction box).

The application under test is selected with --app-root, so the same scoring can
be applied to an older checkout to establish a baseline:

    python -m benchmark.run_forms_benchmark --out build/forms
    python -m benchmark.run_forms_benchmark --app-root ../baseline --out build/forms_base
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmark.forms_corpus import build_all  # noqa: E402
from benchmark.output_scoring import (EXCLUDED, NOT_PII, PII, extract_words,  # noqa: E402
                                      kept_mask, score)


def geometric_labeller(items):
    pii = [it for it in items if it.pii]
    other = [it for it in items if not it.pii]

    def inside(word, item, pad=1.5):
        x, y = word.center
        x0, y0, x1, y1 = item.rect
        return x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad

    def label(word):
        if not any(ch.isalnum() for ch in word.text):
            return NOT_PII, "separator"      # "&" between joint names is kept by design
        for it in pii:
            if it.page == word.page and inside(word, it):
                return PII, it.category
        for it in other:
            if it.page == word.page and inside(word, it):
                return NOT_PII, it.category
        return NOT_PII, "unlabelled_text"
    return label


def run_app(app_root: Path, src: str, out: str, env_extra: dict | None = None,
            accept_all: bool = True) -> tuple[int, float, str]:
    env = dict(os.environ)
    env.setdefault("DOCANON_LLM", "0")
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.update(env_extra or {})
    started = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "app.cli", "process", src, *(["--accept-all"] if accept_all else []), "-o", out],
        cwd=str(app_root), env=env, capture_output=True, text=True,
    )
    return proc.returncode, time.time() - started, proc.stdout[-4000:] + proc.stderr[-2000:]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-root", default=str(ROOT))
    ap.add_argument("--out", default=str(ROOT / "build" / "forms_benchmark"))
    ap.add_argument("--json")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--default-decisions", action="store_true",
                    help="use the app's own default decisions (held items stay unapplied) instead of --accept-all")
    args = ap.parse_args(argv)

    out_dir = Path(args.out).resolve()
    corpus = build_all(out_dir / "inputs")
    results = {}
    total = None
    for name, (path, items) in corpus.items():
        if args.only and name not in args.only:
            continue
        out_pdf = str(out_dir / f"{name}.out.pdf")
        code, secs, log = run_app(Path(args.app_root), path, out_pdf, accept_all=not args.default_decisions)
        if not Path(out_pdf).exists():
            results[name] = {"error": f"no output (exit {code})", "log": log[-1500:]}
            print(f"{name}: NO OUTPUT (exit {code})")
            continue
        s = score(extract_words(path), kept_mask(extract_words(path), extract_words(out_pdf)),
                  geometric_labeller(items))
        results[name] = {"exit": code, "seconds": round(secs, 1), **s.as_dict(),
                         "fn_pages": s.fn_pages, "fp_pages": s.fp_pages}
        print(f"{name:14} P={s.precision:.3f} R={s.recall:.3f} F1={s.f1:.3f} "
              f"tp={s.tp} fp={s.fp} fn={s.fn} ({secs:.0f}s, exit {code})")
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
        results["_total"] = total.as_dict()
        print(f"{'TOTAL':14} P={total.precision:.3f} R={total.recall:.3f} F1={total.f1:.3f} "
              f"tp={total.tp} fp={total.fp} fn={total.fn}")
        for cat, v in sorted(total.by_category.items()):
            print(f"   {cat:18} {v}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
