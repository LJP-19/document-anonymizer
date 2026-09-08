"""The build and the tests must not drift apart (spec sections 77 and 99)."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build-release.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def test_every_platform_that_ships_a_build_runs_the_tests():
    jobs = _workflow()["jobs"]
    shipping = [
        name
        for name, cfg in jobs.items()
        if any("upload-artifact" in str(step.get("uses", "")) for step in cfg["steps"])
    ]
    assert shipping, "no job uploads an installer"
    for name in shipping:
        steps = " ".join(str(step.get("run", "")) for step in jobs[name]["steps"])
        assert "pytest" in steps, f"job '{name}' ships a build without running the tests"


def test_builds_run_on_native_runners():
    jobs = _workflow()["jobs"]
    assert jobs["windows"]["runs-on"].startswith("windows")
    assert jobs["macos"]["runs-on"].startswith("macos")


def test_the_model_is_installed_before_packaging():
    """A packaged app that downloads its model at runtime is not offline."""
    for name in ("windows", "macos"):
        steps = " ".join(str(s.get("run", "")) for s in _workflow()["jobs"][name]["steps"])
        assert "en_core_web_sm" in steps


def test_no_secrets_are_written_into_the_workflow():
    text = WORKFLOW.read_text().lower()
    for marker in ("ghp_", "github_pat_", "-----begin"):
        assert marker not in text
