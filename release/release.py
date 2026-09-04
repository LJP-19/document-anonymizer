#!/usr/bin/env python3
"""One-click release (spec sections 57-72).

Edit release_config.json, double-click the launcher for your OS, and this script
does the rest: preflight, tests, commit, push, tag. GitHub Actions then builds
the Windows .exe and macOS .dmg on native runners.

Safety rules this script will not break:
  - it never runs `git reset --hard`, `git checkout --force`, or `git clean`
  - it never discards uncommitted work
  - it pushes the CURRENT repository state, not a copy or a cached snapshot
  - it reports failure honestly and never claims a release succeeded when the
    push or the build did not
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(__file__).resolve().parent / "release_config.json"

REQUIRED_FILES = [
    "main.py",
    "app/version.py",
    "app/session.py",
    "requirements.txt",
    "resources/rules/pii_rules.yaml",
    "buildtools/build.py",
    ".github/workflows/build-release.yml",
]

BOLD, DIM, RED, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    say(f"  {GREEN}OK{RESET}    {msg}")


def warn(msg: str) -> None:
    say(f"  {YELLOW}WARN{RESET}  {msg}")


def fail(msg: str) -> None:
    say(f"  {RED}FAIL{RESET}  {msg}")


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=check, capture_output=True, text=True
    )


def stop(message: str) -> None:
    say()
    say(f"{RED}{BOLD}Release aborted.{RESET}")
    say(f"  {message}")
    say()
    _hold()
    sys.exit(1)


def _hold() -> None:
    """Keep the window open when launched by double-click."""
    if sys.stdin and sys.stdin.isatty():
        try:
            input("Press Enter to close...")
        except EOFError:
            pass


# --- configuration ---------------------------------------------------------


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        stop(f"{CONFIG_PATH} is missing.")
    try:
        config = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as exc:
        stop(f"release_config.json is not valid JSON: {exc}")

    for key in ("repository", "branch", "version"):
        if not config.get(key):
            stop(f"release_config.json is missing a value for '{key}'.")
    if "YOUR-ACCOUNT" in config["repository"]:
        stop("Set 'repository' in release_config.json to your real GitHub repository URL.")
    for forbidden in ("token", "password", "secret", "key", "passphrase"):
        if any(forbidden in k.lower() for k in config):
            stop(
                f"release_config.json contains a '{forbidden}' field. Credentials must "
                "never live in the repository (spec section 59). Remove it and use "
                "GitHub's own login prompt or GitHub Secrets."
            )
    return config


def version_from_source() -> str:
    text = (ROOT / "app" / "version.py").read_text()
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        stop("Could not read __version__ from app/version.py.")
    return match.group(1)


def sync_version(config: dict) -> str:
    """app/version.py is the single source of truth (spec section 64)."""
    source_version = version_from_source()
    if config["version"] != source_version:
        warn(
            f"release_config.json says {config['version']}, app/version.py says "
            f"{source_version}. Using {source_version} and updating the config."
        )
        config["version"] = source_version
        CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    ok(f"version {source_version}")
    return source_version


# --- preflight -------------------------------------------------------------


def preflight(config: dict) -> tuple[str, list[str]]:
    say(f"{BOLD}Preflight{RESET}")

    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        stop("Git is not installed. https://git-scm.com/downloads")
    ok("git available")

    missing = [f for f in REQUIRED_FILES if not (ROOT / f).exists()]
    if missing:
        stop("Required files are missing: " + ", ".join(missing))
    ok(f"{len(REQUIRED_FILES)} required file(s) present")

    version = sync_version(config)

    if not (ROOT / ".git").exists():
        say("  initialising a new git repository")
        git("init")
        git("branch", "-M", config["branch"])
    if config.get("git_name"):
        git("config", "user.name", config["git_name"])
    if config.get("git_email"):
        git("config", "user.email", config["git_email"])

    branch = git("rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
    if branch and branch != "HEAD" and branch != config["branch"]:
        stop(
            f"You are on branch '{branch}' but release_config.json targets "
            f"'{config['branch']}'. Switch branch or change the config; this script "
            "will not move you between branches."
        )
    ok(f"branch {config['branch']}")

    status = git("status", "--porcelain").stdout.splitlines()
    if status:
        ok(f"{len(status)} local change(s) will be committed")
    else:
        warn("working tree is clean - nothing new to commit")

    return version, status


def run_tests(config: dict) -> None:
    if not config.get("run_tests_before_push", True):
        warn("tests skipped by configuration")
        return
    say()
    say(f"{BOLD}Tests{RESET}")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q"],
        cwd=ROOT,
        env={**__import__("os").environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    if result.returncode != 0:
        stop("Tests failed. Nothing was pushed. Fix the failures and run this again.")
    ok("test suite passed")


# --- publish ---------------------------------------------------------------


def commit_and_push(config: dict, version: str, dirty: list[str]) -> str:
    say()
    say(f"{BOLD}Publish{RESET}")

    remote_url = config["repository"]
    existing = git("remote", "get-url", "origin", check=False)
    if existing.returncode != 0:
        git("remote", "add", "origin", remote_url)
    elif existing.stdout.strip() != remote_url:
        warn(f"origin was {existing.stdout.strip()}; pointing it at {remote_url}")
        git("remote", "set-url", "origin", remote_url)
    ok(f"origin {remote_url}")

    if dirty:
        git("add", "-A")
        message = config.get("commit_message") or f"Release v{version}"
        commit = git("commit", "-m", message, check=False)
        if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
            stop(f"git commit failed:\n{commit.stdout}\n{commit.stderr}")
        ok(f"committed: {message}")

    push = git("push", "-u", "origin", config["branch"], check=False)
    if push.returncode != 0:
        stop(
            "git push failed:\n"
            f"{push.stderr.strip()}\n\n"
            "Common causes:\n"
            f"  - the repository does not exist yet: create it at https://github.com/new\n"
            f"    (name it exactly as in your repository URL, do NOT add a README)\n"
            "  - you are not signed in: run 'git push' once in a terminal and complete the login\n"
            "  - the remote has commits you do not have: run 'git pull --rebase' first"
        )
    ok(f"pushed to {config['branch']}")

    commit_sha = git("rev-parse", "--short", "HEAD").stdout.strip()

    if config.get("create_github_release", True):
        tag = f"v{version}"
        if git("rev-parse", tag, check=False).returncode == 0:
            stop(
                f"Tag {tag} already exists. Bump __version__ in app/version.py before "
                "publishing another release."
            )
        git("tag", "-a", tag, "-m", f"{config.get('application_name', 'Release')} {tag}")
        tag_push = git("push", "origin", tag, check=False)
        if tag_push.returncode != 0:
            stop(f"pushing tag {tag} failed:\n{tag_push.stderr.strip()}")
        ok(f"tagged and pushed {tag}")

    return commit_sha


def report(config: dict, version: str, commit_sha: str) -> None:
    repo_web = config["repository"].removesuffix(".git")
    say()
    say(f"{GREEN}{BOLD}Release submitted.{RESET}")
    say()
    say(f"  Repository : {repo_web}")
    say(f"  Branch     : {config['branch']}")
    say(f"  Version    : {version}")
    say(f"  Commit     : {commit_sha}")
    say(f"  Targets    : Windows .exe {'ON' if config.get('build_windows', True) else 'off'}"
        f"  |  macOS .dmg {'ON' if config.get('build_macos', True) else 'off'}")
    say()
    say(f"  {BOLD}The build runs on GitHub, not on this machine.{RESET}")
    say(f"  Watch it here : {repo_web}/actions")
    say(f"  Installers    : {repo_web}/releases"
        if config.get("create_github_release", True)
        else f"  Installers    : {repo_web}/actions  ->  latest run  ->  Artifacts")
    say()
    say(f"  {DIM}This script cannot see the build result. If the run shows a red X,")
    say(f"  the release did NOT produce installers - open the run to see why.{RESET}")
    say()


def main() -> int:
    say()
    say(f"{BOLD}{'=' * 58}{RESET}")
    say(f"{BOLD}  Document Anonymizer - one-click release{RESET}")
    say(f"{BOLD}{'=' * 58}{RESET}")
    say()

    config = load_config()
    version, dirty = preflight(config)
    run_tests(config)
    commit_sha = commit_and_push(config, version, dirty)
    report(config, version, commit_sha)
    _hold()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
