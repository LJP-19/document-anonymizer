"""Check that a release archive contains everything the build needs.

A packaged app that is missing a rule file, a workflow, or a launcher fails
late and confusingly. This fails immediately, before anything is shipped.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Present in every release, or the archive is not usable.
REQUIRED = [
    "app/version.py",
    "app/main.py",
    "app/session.py",
    "app/batch.py",
    "app/detection/engine.py",
    "app/detection/types.py",
    "app/detection/auditor.py",
    "app/detection/gliner.py",
    "app/detection/entities_pass.py",
    "app/document/hidden.py",
    "app/document/pdflock.py",
    "app/entities/roster.py",
    "app/export/redactor.py",
    "app/verification/verifier.py",
    "app/ui/main_window.py",
    "app/ui/widgets.py",
    "app/ui/theme.py",
    "resources/rules/pii_rules.yaml",
    "resources/icons/app.svg",
    "buildtools/build.py",
    "buildtools/fetch_models.py",
    "release/release.py",
    "release/update.py",
    "release/bootstrap.py",
    "release/release_config.json",
    ".github/workflows/build-release.yml",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-llm.txt",
    "main.py",
    "PUBLISH.bat",
    "PUBLISH.command",
    "pytest.ini",
]

#: Content that proves the archive carries the current fixes, not an older tree.
MARKERS = {
    "app/detection/types.py": ["CITIZENSHIP", "ORG_PRIVATE"],
    "app/detection/engine.py": ["_complete_partial_lines", "_retype_from_labels",
                                "_drop_financial_values", "adjudicate_document"],
    "app/session.py": ["add_manual_text", "roster", "safe_output_name"],
    "app/ui/main_window.py": ["ProcessingView", "progress_reported"],
    "app/ui/widgets.py": ["stop_all_runners"],
    "app/document/pdflock.py": ["PDF_LOCK"],
}


def entries(archive: Path) -> dict[str, str]:
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        prefix = ""
        if not any(n == "app/version.py" for n in names):
            for name in names:
                if name.endswith("app/version.py"):
                    prefix = name[: -len("app/version.py")]
                    break
        return {
            name[len(prefix):]: name for name in names if name.startswith(prefix)
        }


def check(archive: Path) -> list[str]:
    problems: list[str] = []
    mapping = entries(archive)
    for relative in REQUIRED:
        if relative not in mapping:
            problems.append(f"missing from the archive: {relative}")

    with zipfile.ZipFile(archive) as zf:
        for relative, markers in MARKERS.items():
            if relative not in mapping:
                continue
            body = zf.read(mapping[relative]).decode("utf-8", "replace")
            for marker in markers:
                if marker not in body:
                    problems.append(f"{relative} is an older version: no {marker!r}")

    # Every tracked source file on disk should be in the archive.
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if any(part in {"build", "dist", "__pycache__", ".git"} for part in path.parts):
            continue
        if relative not in mapping:
            problems.append(f"on disk but not in the archive: {relative}")
    return problems


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python buildtools/verify_release.py <archive.zip>")
        return 2
    archive = Path(sys.argv[1])
    problems = check(archive)
    if problems:
        print(f"{archive.name}: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"{archive.name}: complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
