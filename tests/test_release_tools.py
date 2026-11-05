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


def test_launchers_exist():
    assert (ROOT / "PUBLISH.bat").exists()
    assert (ROOT / "PUBLISH.command").exists()


def test_shell_launchers_are_executable_in_the_repository():
    """The exec bit must live in the git index, not on the local filesystem.

    Windows cannot represent it, so checking `stat()` fails for anyone who
    publishes from Windows even though the committed file is correct. What
    matters is the mode a macOS user gets when they clone.
    """
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "-s", "--", "PUBLISH.command", "release/release.command"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("not a git checkout")
    for line in result.stdout.strip().splitlines():
        mode, _, path = line.partition(" ")[0], None, line.split("\t")[-1]
        assert mode == "100755", f"{path} is committed as {mode}, expected 100755"


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


# --- tag uniqueness --------------------------------------------------------


def test_patch_version_advances(release):
    assert release._bump_patch("0.2.0") == "0.2.1"
    assert release._bump_patch("1.9") == "1.9.1"
    assert release._bump_patch("2.0.9") == "2.0.10"


def test_a_published_version_is_never_reused(release, monkeypatch, tmp_path):
    """Regression: an existing tag was moved, so every release reused v0.2.0.

    Reusing a tag silently replaces the installers already attached to that
    release, and nobody who downloaded the old ones can tell.
    """
    published = {"v0.2.0", "v0.2.1"}
    monkeypatch.setattr(release, "_tag_exists", lambda tag: tag in published)
    monkeypatch.setattr(release, "git", lambda *a, **k: type("R", (), {"stdout": "", "returncode": 0})())

    version_file = tmp_path / "app" / "version.py"
    version_file.parent.mkdir(parents=True)
    version_file.write_text('__version__ = "0.2.0"\n')
    config_file = tmp_path / "release_config.json"
    config_file.write_text(json.dumps({"version": "0.2.0"}))
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "CONFIG_PATH", config_file)

    config = {"version": "0.2.0"}
    assert release.ensure_unique_version(config, "0.2.0") == "0.2.2"
    assert '"0.2.2"' in version_file.read_text()
    assert json.loads(config_file.read_text())["version"] == "0.2.2"


def test_an_unpublished_version_is_left_alone(release, monkeypatch):
    monkeypatch.setattr(release, "_tag_exists", lambda tag: False)
    monkeypatch.setattr(release, "git", lambda *a, **k: type("R", (), {"stdout": "", "returncode": 0})())
    assert release.ensure_unique_version({"version": "9.9.9"}, "9.9.9") == "9.9.9"


def test_the_launcher_never_deletes_a_tag(release):
    """Moving a tag is how the reuse bug happened; it must not come back."""
    calls = _git_calls(ROOT / "release" / "release.py")
    for call in calls:
        assert not (call and call[0] == "tag" and "-d" in call), f"git {' '.join(call)}"
        assert not any(arg.startswith(":refs/tags/") for arg in call), f"git {' '.join(call)}"


def test_consistency_gate_catches_a_half_merged_tree(release, monkeypatch, tmp_path):
    """Regression: a merge kept old files beside new ones and CI caught it, not us.

    New files arrived cleanly while the files they depend on stayed at an older
    version. Each module imported alone; together they did not.
    """
    package = tmp_path / "app"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "alpha.py").write_text("VALUE = 1\n")
    (package / "beta.py").write_text("from .alpha import MISSING_NAME\n")

    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "CONSISTENCY_MODULES", ["app.alpha", "app.beta"])
    stopped = []
    monkeypatch.setattr(release, "stop", lambda message: stopped.append(message))

    release.check_tree_is_consistent()
    assert stopped, "an inconsistent tree was allowed through"
    assert "inconsistent" in stopped[0]


def test_consistency_gate_passes_on_a_sound_tree(release, monkeypatch, tmp_path):
    package = tmp_path / "app"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "alpha.py").write_text("VALUE = 1\n")
    (package / "beta.py").write_text("from .alpha import VALUE\n")

    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "CONSISTENCY_MODULES", ["app.alpha", "app.beta"])
    monkeypatch.setattr(release, "stop", lambda message: pytest.fail(message))
    release.check_tree_is_consistent()


def test_the_real_project_imports_consistently(release):
    """The gate runs against this repository, not only a synthetic one."""
    assert release.CONSISTENCY_MODULES
    for name in ("app.session", "app.batch", "app.detection.types"):
        assert name in release.CONSISTENCY_MODULES


# --- archive selection -----------------------------------------------------


def test_highest_version_wins_not_newest_file(update, tmp_path, monkeypatch):
    """Regression: an old zip with a fresh timestamp overwrote a newer project.

    v0.2.0 was applied over a v0.7.2 tree and committed, silently reverting
    every file the newer build had changed.
    """
    import os
    import time

    old = tmp_path / "document-anonymizer-v0.2.0.zip"
    new = tmp_path / "document-anonymizer-v0.7.2.zip"
    new.write_bytes(b"new")
    time.sleep(0.01)
    old.write_bytes(b"old")  # the OLDER version has the NEWER timestamp
    assert old.stat().st_mtime > new.stat().st_mtime

    monkeypatch.setattr(update, "search_locations", lambda: [tmp_path])
    assert update.find_zip(None) == new


def test_version_is_parsed_from_the_archive_name(update, tmp_path):
    assert update.version_of_archive(Path("document-anonymizer-v0.7.2.zip")) == (0, 7, 2)
    assert update.version_of_archive(Path("document-anonymizer-v1.10.0.zip")) == (1, 10, 0)
    assert update.version_of_archive(Path("unrelated.zip")) == ()


def test_version_ordering_is_numeric_not_alphabetical(update):
    assert update.version_tuple("0.10.0") > update.version_tuple("0.9.0")
    assert update.version_tuple("1.0.0") > update.version_tuple("0.99.99")


def test_applying_an_older_build_is_refused(update):
    """A downgrade must stop, not silently revert the project."""
    text = (ROOT / "release" / "update.py").read_text()
    assert "older than this project" in text
    assert "version_tuple(incoming) < version_tuple(before)" in text


def test_a_missing_dependency_does_not_block_the_push(release, monkeypatch, tmp_path):
    """Regression: no PyMuPDF on Python 3.14 meant no release at all.

    A package this machine cannot install is an environment problem. CI checks
    the same imports on all three platforms, so skipping here loses nothing.
    """
    package = tmp_path / "app"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "alpha.py").write_text("VALUE = 1\n")
    (package / "needs_dep.py").write_text("import definitely_not_installed_xyz\n")

    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "CONSISTENCY_MODULES", ["app.alpha", "app.needs_dep"])
    monkeypatch.setattr(release, "stop", lambda message: pytest.fail(message))
    release.check_tree_is_consistent()


def test_a_broken_tree_still_blocks_the_push(release, monkeypatch, tmp_path):
    """The dependency carve-out must not swallow a real inconsistency."""
    package = tmp_path / "app"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "alpha.py").write_text("VALUE = 1\n")
    (package / "beta.py").write_text("from .alpha import MISSING_NAME\n")

    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "CONSISTENCY_MODULES", ["app.alpha", "app.beta"])
    stopped = []
    monkeypatch.setattr(release, "stop", stopped.append)
    release.check_tree_is_consistent()
    assert stopped and "inconsistent" in stopped[0]


def test_a_flat_archive_is_understood(update, tmp_path, monkeypatch):
    """The release archive packs the project at the root, not inside a folder.

    Windows already creates a folder named after the zip, so an extra wrapper
    meant opening two folders to reach the files.
    """
    import zipfile

    archive = tmp_path / "document-anonymizer-v9.9.9.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("app/version.py", '__version__ = "9.9.9"\n')
        zf.writestr("PUBLISH.bat", "@echo off\n")

    root = update.extract(archive)
    assert (root / "app" / "version.py").exists()
    assert update.version_in(root) == "9.9.9"


def test_a_wrapped_archive_still_works(update, tmp_path):
    import zipfile

    archive = tmp_path / "document-anonymizer-v9.9.8.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("docanon/app/version.py", '__version__ = "9.9.8"\n')

    root = update.extract(archive)
    assert update.version_in(root) == "9.9.8"
