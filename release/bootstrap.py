"""Make sure the release machine has what the tests need.

Runs before the test suite so a fresh machine does not fail on a missing
dependency. Everything installed here is pinned; nothing is fetched at
application runtime.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = "en_core_web_sm"
MODEL_WHEEL = (
    "https://github.com/explosion/spacy-models/releases/download/"
    "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
)
REQUIRED = ["pytest", "pymupdf", "spacy", "faker", "yaml", "reportlab", "PySide6"]

#: Versions the pinned dependencies actually ship wheels for. Outside this range
#: pip tries to compile spaCy and PySide6 from source and fails.
MIN_PYTHON = (3, 11)
MAX_PYTHON = (3, 12)


def python_is_supported() -> bool:
    return MIN_PYTHON <= sys.version_info[:2] <= MAX_PYTHON


def python_problem() -> str:
    """Empty string when the interpreter is fine, otherwise an explanation."""
    if python_is_supported():
        return ""
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    wanted = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]} to {MAX_PYTHON[0]}.{MAX_PYTHON[1]}"
    return (
        f"this is Python {running}; the pinned dependencies need {wanted}. "
        "spaCy and PySide6 publish no wheels for newer versions, so pip would "
        "try to build them from source and fail."
    )


def _missing() -> list[str]:
    return [m for m in REQUIRED if importlib.util.find_spec(m) is None]


def _pip(*args: str) -> int:
    return subprocess.run([sys.executable, "-m", "pip", "install", *args]).returncode


def ensure(quiet: bool = False) -> bool:
    """Returns True if the environment is ready. Installs only what is absent."""
    problem = python_problem()
    if problem:
        if not quiet:
            print(f"  {problem}")
        return False

    missing = _missing()
    if missing:
        if not quiet:
            print(f"  installing missing packages: {', '.join(missing)}")
        if _pip("-r", str(ROOT / "requirements-dev.txt")) != 0:
            print("  could not install dependencies from requirements-dev.txt")
            return False

    if importlib.util.find_spec(MODEL) is None:
        if not quiet:
            print(f"  installing the local language model ({MODEL})")
        if _pip(MODEL_WHEEL) != 0:
            print(
                f"  could not install {MODEL}. The tests need it; install it manually:\n"
                f"    {sys.executable} -m pip install {MODEL_WHEEL}"
            )
            return False

    return not _missing() and importlib.util.find_spec(MODEL) is not None


if __name__ == "__main__":
    raise SystemExit(0 if ensure() else 1)
