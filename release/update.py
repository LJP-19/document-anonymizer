#!/usr/bin/env python3
"""Apply a new build and publish it, in one step.

Finds the most recent `document-anonymizer-v*.zip` you have downloaded, copies
its contents over this project, and then runs the normal release. The version
and the commit message come from the new files, so nothing has to be edited by
hand.

Local settings are preserved: `release/release_config.json` is never overwritten,
and `.git` is never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZIP_GLOB = "document-anonymizer-v*.zip"

#: Files whose local copy always wins over the one in the zip.
PRESERVE = {"release/release_config.json"}

#: Never copied out of the archive, never deleted from the project.
NEVER_TOUCH = {".git", ".venv", "venv", "build", "dist", "__pycache__"}

BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"
)


def say(msg: str = "") -> None:
    print(msg, flush=True)


def hold() -> None:
    if sys.stdin and sys.stdin.isatty():
        try:
            input("Press Enter to close...")
        except EOFError:
            pass


def stop(message: str) -> None:
    say()
    say(f"{RED}{BOLD}Update aborted.{RESET}")
    say(f"  {message}")
    say()
    hold()
    sys.exit(1)


def search_locations() -> list[Path]:
    home = Path.home()
    names = ["Downloads", "downloads", "Desktop", "desktop", "Documents", "documents"]
    candidates = [ROOT, ROOT.parent, ROOT.parent.parent]
    candidates += [home / n for n in names]
    # OneDrive redirects these folders on many Windows machines.
    onedrive = os.environ.get("OneDrive") or os.environ.get("ONEDRIVE")
    if onedrive:
        candidates += [Path(onedrive) / n for n in names]
    seen, out = set(), []
    for path in candidates:
        resolved = path.resolve() if path.exists() else None
        if resolved and resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


def find_zip(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            stop(f"{path} does not exist.")
        return path

    found: list[Path] = []
    for directory in search_locations():
        try:
            found.extend(directory.glob(ZIP_GLOB))
        except OSError:
            continue
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


def version_in(path: Path) -> str:
    text = (path / "app" / "version.py").read_text()
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split('"')[1]
    return "unknown"


def current_version() -> str:
    return version_in(ROOT)


def extract(archive: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="docanon-update-"))
    with zipfile.ZipFile(archive) as zf:
        # Refuse absolute paths and parent traversal in archive members.
        for member in zf.namelist():
            target = (tmp / member).resolve()
            if not str(target).startswith(str(tmp.resolve())):
                stop(f"the archive contains an unsafe path: {member}")
        zf.extractall(tmp)

    roots = [p for p in tmp.iterdir() if p.is_dir()]
    for root in roots:
        if (root / "app" / "version.py").exists():
            return root
    if (tmp / "app" / "version.py").exists():
        return tmp
    stop("that zip does not look like a Document Anonymizer build (no app/version.py).")
    return tmp  # unreachable


def apply_update(source: Path) -> list[str]:
    changed: list[str] = []
    for src in source.rglob("*"):
        relative = src.relative_to(source)
        if any(part in NEVER_TOUCH for part in relative.parts):
            continue
        destination = ROOT / relative

        if src.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue

        as_posix = relative.as_posix()
        if as_posix in PRESERVE and destination.exists():
            say(f"  {DIM}kept your local {as_posix}{RESET}")
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists() or destination.read_bytes() != src.read_bytes():
            shutil.copy2(src, destination)
            changed.append(as_posix)
    return changed


def sync_commit_message(version: str) -> None:
    """Keep release_config.json in step without asking the user to edit it."""
    import json

    config_path = ROOT / "release" / "release_config.json"
    if not config_path.exists():
        return
    try:
        config = json.loads(config_path.read_text())
    except json.JSONDecodeError:
        return
    config["version"] = version
    config["commit_message"] = f"Release v{version}"
    config_path.write_text(json.dumps(config, indent=2) + "\n")


def main() -> int:
    say()
    say(f"{BOLD}{'=' * 58}{RESET}")
    say(f"{BOLD}  Document Anonymizer - update and publish{RESET}")
    say(f"{BOLD}{'=' * 58}{RESET}")
    say()

    explicit = sys.argv[1] if len(sys.argv) > 1 else None
    archive = find_zip(explicit)
    before = current_version()

    if archive is None:
        say(f"  No {ZIP_GLOB} found in Downloads, Desktop, Documents or nearby.")
        say(f"  Publishing the project as it stands (v{before}).")
        say()
    else:
        say(f"{BOLD}Update{RESET}")
        say(f"  found {archive}")
        source = extract(archive)
        incoming = version_in(source)
        if incoming == before:
            say(f"  {YELLOW}note{RESET}  that archive is v{incoming}, same as this project")
        changed = apply_update(source)
        shutil.rmtree(source.parent, ignore_errors=True)
        say(f"  v{before} -> v{incoming}, {len(changed)} file(s) updated")
        for name in changed[:12]:
            say(f"    {DIM}{name}{RESET}")
        if len(changed) > 12:
            say(f"    {DIM}... and {len(changed) - 12} more{RESET}")
        sync_commit_message(incoming)
        say()

    say(f"{BOLD}Dependencies{RESET}")
    sys.path.insert(0, str(ROOT / "release"))
    import bootstrap

    can_test = bootstrap.ensure()
    if can_test:
        say(f"  {GREEN}OK{RESET}    environment ready")
    else:
        # Not fatal. The local test run is a convenience; the workflow runs the
        # same suite on Linux, Windows and macOS and no installer is built
        # unless it passes. Blocking the push here would stop a release for a
        # problem that only affects this machine.
        say(f"  {YELLOW}SKIP{RESET}  tests will not run on this machine")
        say(f"        {DIM}GitHub still runs the full suite on all three")
        say(f"        platforms before building anything.{RESET}")
        if bootstrap.python_problem():
            say(f"        {DIM}To run them here, install Python 3.12 and re-run.{RESET}")

    args = [sys.executable, str(ROOT / "release" / "release.py"), "--no-hold"]
    if not can_test:
        args.append("--skip-tests")
    result = subprocess.run(args, cwd=ROOT)
    hold()
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
