"""Tests for the release tooling (spec sections 61-63).

The launcher is the part a non-developer touches, so its safety properties need
to be enforced by tests rather than by care.
"""

from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "release" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def update():
    return _load("update")


def _fake_build(directory: Path, version: str) -> Path:
    (directory / "app").mkdir(parents=True, exist_ok=True)
    (directory / "app" / "version.py").write_text(f'__version__ = "{version}"\n')
    (directory / "release").mkdir(parents=True, exist_ok=True)
    (directory / "release" / "release_config.json").write_text(
        json.dumps({"repository": "https://example.invalid/x.git", "version": version})
    )
    (directory / "newfile.txt").write_text("new\n")
    return directory


def test_version_is_read_from_the_build_not_the_config(update, tmp_path):
    build = _fake_build(tmp_path / "build", "9.9.9")
    assert update.version_in(build) == "9.9.9"


def test_local_config_is_preserved_across_an_update(update, tmp_path, monkeypatch):
    project = tmp_path / "project"
    _fake_build(project, "1.0.0")
    mine = {"repository": "https://github.com/me/mine.git", "version": "1.0.0"}
    (project / "release" / "release_config.json").write_text(json.dumps(mine))

    incoming = _fake_build(tmp_path / "incoming", "2.0.0")
    monkeypatch.setattr(update, "ROOT", project)
    update.apply_update(incoming)

    kept = json.loads((project / "release" / "release_config.json").read_text())
    assert kept["repository"] == "https://github.com/me/mine.git"
    assert update.version_in(project) == "2.0.0"


def test_git_directory_is_never_overwritten(update, tmp_path, monkeypatch):
    project = tmp_path / "project"
    _fake_build(project, "1.0.0")
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    incoming = _fake_build(tmp_path / "incoming", "2.0.0")
    (incoming / ".git").mkdir()
    (incoming / ".git" / "HEAD").write_text("CORRUPT\n")

    monkeypatch.setattr(update, "ROOT", project)
    update.apply_update(incoming)
    assert (project / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"


def test_commit_message_follows_the_version(update, tmp_path, monkeypatch):
    project = tmp_path / "project"
    _fake_build(project, "1.0.0")
    monkeypatch.setattr(update, "ROOT", project)
    update.sync_commit_message("3.1.4")
    config = json.loads((project / "release" / "release_config.json").read_text())
    assert config["version"] == "3.1.4"
    assert config["commit_message"] == "Release v3.1.4"


def test_archive_with_a_traversal_path_is_rejected(update, tmp_path, monkeypatch):
    archive = tmp_path / "document-anonymizer-v9.9.9.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escaped.txt", "no")

    calls = []
    monkeypatch.setattr(update, "stop", lambda msg: (_ for _ in ()).throw(RuntimeError(msg)))
    with pytest.raises(RuntimeError, match="unsafe path"):
        update.extract(archive)
    assert not calls


def test_newest_archive_wins(update, tmp_path, monkeypatch):
    import os
    import time

    older = tmp_path / "document-anonymizer-v1.0.0.zip"
    newer = tmp_path / "document-anonymizer-v2.0.0.zip"
    older.write_bytes(b"a")
    time.sleep(0.01)
    newer.write_bytes(b"b")
    os.utime(older, (1, 1))

    monkeypatch.setattr(update, "search_locations", lambda: [tmp_path])
    assert update.find_zip(None) == newer


def test_release_config_holds_no_credentials():
    """Spec section 59: no secrets in the repository, ever."""
    config = json.loads((ROOT / "release" / "release_config.json").read_text())
    forbidden = ("token", "password", "secret", "key", "passphrase", "credential")
    assert not [k for k in config if any(f in k.lower() for f in forbidden)]


def test_launchers_exist_and_are_executable():
    assert (ROOT / "PUBLISH.bat").exists()
    command = ROOT / "PUBLISH.command"
    assert command.exists()
    assert command.stat().st_mode & 0o111, "PUBLISH.command is not executable"


# --- interpreter compatibility ---------------------------------------------


@pytest.fixture(scope="module")
def bootstrap():
    return _load("bootstrap")


def _fake_sys(major: int, minor: int):
    """A stand-in for `sys` that supports both slicing and .major/.minor."""
    from collections import namedtuple
    from types import SimpleNamespace

    Version = namedtuple("Version", "major minor micro")
    return SimpleNamespace(version_info=Version(major, minor, 0))


def test_python_314_is_reported_as_unsupported(bootstrap, monkeypatch):
    """The pinned spaCy and PySide6 have no wheels above 3.12."""
    monkeypatch.setattr(bootstrap, "sys", _fake_sys(3, 14))
    assert not bootstrap.python_is_supported()
    problem = bootstrap.python_problem()
    assert "3.14" in problem and "3.11 to 3.12" in problem


def test_supported_pythons_pass(bootstrap, monkeypatch):
    for major, minor in ((3, 11), (3, 12)):
        monkeypatch.setattr(bootstrap, "sys", _fake_sys(major, minor))
        assert bootstrap.python_is_supported(), (major, minor)


def test_an_unusable_interpreter_does_not_block_publishing():
    """A local environment problem must not stop a release.

    CI runs the same suite on Linux, Windows and macOS and refuses to build an
    installer unless it passes, so skipping the local run loses no safety.
    """
    text = (ROOT / "release" / "update.py").read_text()
    assert "stop(\"dependencies could not be installed" not in text
    assert "--skip-tests" in text


def test_release_skips_rather_than_aborts_without_dependencies():
    text = (ROOT / "release" / "release.py").read_text()
    assert "--skip-tests" in text
    assert "stop(\"dependencies for the test suite" not in text


def test_launchers_prefer_a_supported_python():
    bat = (ROOT / "PUBLISH.bat").read_text()
    command = (ROOT / "PUBLISH.command").read_text()
    assert "3.12" in bat and "3.11" in bat
    assert "python3.12" in command and "python3.11" in command


# --- diverged remote recovery ----------------------------------------------


@pytest.fixture(scope="module")
def release():
    return _load("release")


def test_non_fast_forward_rejection_is_recognised(release):
    """The exact wording GitHub returns when the remote is ahead."""
    stderr = (
        " ! [rejected]        main -> main (fetch first)\n"
        "error: failed to push some refs to 'https://github.com/x/y.git'\n"
    )
    assert release._is_behind_remote(stderr)


def test_ordinary_failures_are_not_mistaken_for_divergence(release):
    assert not release._is_behind_remote("fatal: repository not found")
    assert not release._is_behind_remote("fatal: Authentication failed")
    assert not release._is_behind_remote("")


def _git_calls(path: Path) -> list[tuple[str, ...]]:
    """Every git(...) invocation in the file, as literal argument tuples.

    Reading the source text alone is not enough: the module docstring names the
    destructive commands in order to promise it never runs them.
    """
    import ast

    calls = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "git":
            args = [a.value for a in node.args if isinstance(a, ast.Constant)]
            calls.append(tuple(str(a) for a in args))
    return calls


def test_recovery_never_force_pushes_or_discards_work():
    """A release tool that rewrites shared history is worse than one that stops."""
    calls = _git_calls(ROOT / "release" / "release.py")
    assert calls, "no git calls found - the parser is wrong, not the code"
    for call in calls:
        joined = " ".join(call)
        for forbidden in ("--force", "-f", "reset", "clean"):
            assert forbidden not in call, f"release.py runs: git {joined}"
    # A failed rebase must restore the previous state rather than leave a mess.
    assert ("rebase", "--abort") in calls
