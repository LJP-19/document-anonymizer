"""Verify a packaged build is self-contained (spec sections 4, 67, 74).

Runs against the bundle PyInstaller produced, not against the source tree. The
question it answers is the only one that matters for distribution: if this were
copied to a machine with no Python, no models and no network, would it work?

A build that is missing a model fails HERE, loudly, rather than on a colleague's
laptop with an unhelpful error.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_NAME = "DocumentAnonymizer"

#: (description, glob) - each must match at least one file inside the bundle.
REQUIRED = [
    ("detection rules", "**/resources/rules/pii_rules.yaml"),
    ("application icon", "**/resources/icons/app.svg"),
    ("GLiNER weights", "**/resources/models/gliner-pii/onnx/model_quint8.onnx"),
    ("GLiNER tokenizer", "**/resources/models/gliner-pii/tokenizer.json"),
    ("spaCy model", "**/en_core_web_sm/**/config.cfg"),
    ("PyMuPDF", "**/pymupdf/**"),
    ("ONNX Runtime", "**/onnxruntime/**"),
    ("Qt libraries", "**/PySide6/**"),
    ("spreadsheet writer", "**/openpyxl/**"),
]

#: Files whose contents prove the bundle carries the current work rather than
#: an older tree that happened to package cleanly.
CONTENT_MARKERS = {
    "**/app/detection/engine.py": ["_complete_partial_lines", "_type_from_table_columns"],
    "**/app/detection/second_pass.py": ["second_pass"],
    "**/app/detection/entities_pass.py": ["spelling_variants"],
    "**/app/pseudonymization/generator.py": ["INLINE_LABELS"],
    "**/app/ui/main_window.py": ["ProcessingView", "search_and_blackout"],
    "**/app/document/model.py": ["LOOKALIKES"],
}

#: Required only when the build is meant to include the audit model.
LLM_REQUIRED = [
    ("Qwen weights", "**/resources/models/llm/*.gguf"),
    ("llama.cpp runtime", "**/llama_cpp/**"),
]

MIN_TOTAL_MB = 120
MIN_TOTAL_MB_WITH_LLM = 900


def bundle_root() -> Path:
    dist = ROOT / "dist"
    for candidate in (dist / f"{BUNDLE_NAME}.app", dist / BUNDLE_NAME, dist):
        if candidate.exists():
            return candidate
    raise SystemExit("no build found in dist/ - run buildtools/build.py first")


def total_megabytes(root: Path) -> float:
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) / 1e6


def check(require_llm: bool) -> list[str]:
    root = bundle_root()
    problems: list[str] = []

    checks = list(REQUIRED) + (list(LLM_REQUIRED) if require_llm else [])
    for description, pattern in checks:
        if not any(root.glob(pattern)):
            problems.append(f"missing from the bundle: {description}  ({pattern})")

    # PyInstaller may compile sources into a bytecode archive; check content
    # markers only where the .py files were kept.
    for pattern, markers in CONTENT_MARKERS.items():
        for path in root.glob(pattern):
            body = path.read_text("utf-8", "replace")
            for marker in markers:
                if marker not in body:
                    problems.append(f"{path.name} is an older version: no {marker!r}")
            break

    size = total_megabytes(root)
    floor = MIN_TOTAL_MB_WITH_LLM if require_llm else MIN_TOTAL_MB
    if size < floor:
        problems.append(
            f"bundle is {size:.0f} MB; expected at least {floor} MB. Something "
            "large did not make it in."
        )
    print(f"bundle: {root}  ({size:.0f} MB)")
    return problems


def main() -> int:
    require_llm = "--no-llm" not in sys.argv
    problems = check(require_llm)
    if problems:
        print(f"\nINCOMPLETE BUILD - {len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\nThis build would fail on a machine that does not already have "
            "these installed.\nIt has NOT been published."
        )
        return 1
    print("bundle is self-contained" + (" (including the audit model)" if require_llm else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
