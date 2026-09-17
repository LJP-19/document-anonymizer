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
    ("spaCy TRF model", "**/en_core_web_trf/**/config.cfg"),
    ("spacy-transformers runtime", "**/spacy_transformers/**"),
    ("torch CPU runtime", "**/torch/**"),
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

# en_core_web_trf + torch + spacy-transformers is now the single largest
# cost in every build, LLM or not - roughly 1.8 GB measured. Both floors
# raised accordingly; a build smaller than this did not actually bundle the
# transformer runtime, whatever the LLM flag says.
# en_core_web_trf + torch + spacy-transformers is the largest single cost in
# every build now, LLM or not - but how much it actually costs in a
# PACKAGED bundle is NOT the same number as a raw `pip install` disk delta.
# PyInstaller's --collect-all does its own file selection (it does not
# straight-copy site-packages: no .dist-info/egg-info, no pip cache, no
# per-platform redundancy that a naive disk-delta measurement includes), so
# an estimate taken from `pip install` on one platform is not a safe floor
# for what a REAL build on a DIFFERENT platform produces.
#
# This floor was originally set to 1700 MB from a Linux sandbox pip-install
# disk-delta measurement, and it broke two real, successful CI builds: a
# genuine Windows build (all required packages collected with zero errors,
# per PyInstaller's own log) came in at 1533 MB, and a genuine macOS build
# at 819 MB - macOS torch wheels are known to be meaningfully smaller than
# Linux/Windows ones (Apple's Accelerate framework instead of bundled MKL,
# fewer x86-specific optimized kernels). Both real numbers are now the
# basis for this floor, not a single-platform guess: set safely below the
# smaller confirmed-complete build (819 MB), but still well above the
# ~220 MB a build MISSING the whole transformer runtime would produce - so
# a genuinely incomplete build (the failure this check exists to catch)
# still fails, while a real, complete build on either target platform does
# not. The precise per-file REQUIRED pattern checks above remain the
# authoritative "is it actually complete" signal; this total-size number is
# a secondary sanity check, not the primary one.
MIN_TOTAL_MB = 500
MIN_TOTAL_MB_WITH_LLM = 1600


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

    # en_core_web_trf is the ONLY spaCy English model this project ships, on
    # direct and repeated instruction - sm and lg must never be present in a
    # shipped bundle, not even alongside trf "just in case."
    for forbidden_name, forbidden_pattern in (
        ("en_core_web_sm", "**/en_core_web_sm/**"),
        ("en_core_web_lg", "**/en_core_web_lg/**"),
    ):
        if any(root.glob(forbidden_pattern)):
            problems.append(
                f"{forbidden_name} is present in the bundle - only "
                "en_core_web_trf may ship"
            )

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
    require_llm = "--with-llm" in sys.argv
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
