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


def test_dependency_installs_retry_and_never_cache():
    """A corrupted wheel must not fail a release, or poison later runs.

    A macOS build died on `BadZipFile: Bad CRC-32` from an 18 MB llama.cpp
    wheel that arrived damaged. Without --no-cache-dir pip would keep replaying
    the broken copy from cache on every subsequent run.
    """
    jobs = _workflow()["jobs"]
    for name, config in jobs.items():
        step = next(
            (s for s in config["steps"] if s.get("name") == "Install dependencies"), None
        )
        assert step is not None, f"{name} has no dependency install step"
        run = step["run"]
        assert "--no-cache-dir" in run, f"{name} caches wheels"
        assert "--retries" in run, f"{name} does not retry"
        assert ("Install-Retry" in run) or ("for attempt in" in run), (
            f"{name} has no retry loop around pip"
        )


def test_the_llm_runtime_is_optional_in_every_job():
    """Regression: a corrupt llama-cpp wheel failed the whole macOS build twice.

    The audit pass is one layer of seven. Losing it must cost a feature, not
    the installer.
    """
    for name, config in _workflow()["jobs"].items():
        step = next(
            (s for s in config["steps"] if s.get("name") == "Install the optional LLM runtime"),
            None,
        )
        assert step is not None, f"{name} has no optional LLM step"
        assert step.get("continue-on-error") is True, f"{name} can still fail on the LLM"


def test_required_manifests_do_not_pin_the_llm_runtime():
    root = WORKFLOW.parents[2]
    for manifest in ("requirements.txt", "requirements-dev.txt"):
        assert "llama-cpp" not in (root / manifest).read_text(), manifest
    assert (root / "requirements-llm.txt").exists()
