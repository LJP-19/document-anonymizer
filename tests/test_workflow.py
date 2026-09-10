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


def test_the_llm_runtime_is_no_longer_optional_for_shipped_builds():
    """Superseded policy.

    It was made optional after a corrupt wheel killed two macOS builds. That
    traded away capability silently: the installer still built, but arrived on
    other machines without the audit model. Reliability now comes from retrying
    several versions, not from shipping less.
    """
    for name, config in _workflow()["jobs"].items():
        step = next(
            (s for s in config["steps"] if "LLM runtime" in str(s.get("name", ""))), None
        )
        assert step is not None, f"{name} has no LLM runtime step"
        assert step.get("continue-on-error") is not True, f"{name} tolerates a missing model"
        assert "0.3.35" in step["run"] and "0.3.16" in step["run"], (
            f"{name} does not try several wheel versions"
        )


def test_required_manifests_do_not_pin_the_llm_runtime():
    root = WORKFLOW.parents[2]
    for manifest in ("requirements.txt", "requirements-dev.txt"):
        assert "llama-cpp" not in (root / manifest).read_text(), manifest
    assert (root / "requirements-llm.txt").exists()


def test_shipped_builds_require_the_audit_model():
    """An installer goes to machines with nothing installed.

    Shipping one that quietly lacks the audit model makes it a different product
    from the one that was tested here.
    """
    for name, config in _workflow()["jobs"].items():
        step = next(
            (s for s in config["steps"] if "LLM runtime" in str(s.get("name", ""))), None
        )
        assert step is not None, f"{name} has no LLM runtime step"
        assert step.get("continue-on-error") is not True, (
            f"{name} still tolerates a missing audit model"
        )


def test_the_build_refuses_to_publish_an_incomplete_bundle():
    root = WORKFLOW.parents[2]
    build = (root / "buildtools" / "build.py").read_text()
    assert "Refusing to build without the audit model" in build
    assert "not self-contained; refusing to publish it" in build

    verify = (root / "buildtools" / "verify_bundle.py").read_text()
    for required in ("gliner-pii", "llm/*.gguf", "en_core_web_sm", "PySide6", "onnxruntime"):
        assert required in verify, f"the bundle check does not look for {required}"


def test_bundle_check_reports_what_is_missing(tmp_path, monkeypatch):
    import importlib.util

    root = WORKFLOW.parents[2]
    spec = importlib.util.spec_from_file_location(
        "verify_bundle", root / "buildtools" / "verify_bundle.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    empty = tmp_path / "dist" / "DocumentAnonymizer"
    empty.mkdir(parents=True)
    (empty / "placeholder.txt").write_text("x")
    monkeypatch.setattr(module, "ROOT", tmp_path)

    problems = module.check(require_llm=True)
    assert any("GLiNER" in p for p in problems)
    assert any("Qwen" in p for p in problems)
    assert any("MB" in p for p in problems), "an undersized bundle should be flagged"


def test_models_are_fetched_after_the_runtime_is_installed():
    """Regression: both builds refused to package, with no .gguf weights.

    fetch_models.py ran inside "Install dependencies", before the LLM runtime
    step, and skipped the Qwen download because llama_cpp was not importable
    yet. Correct guard, wrong order.
    """
    for name, config in _workflow()["jobs"].items():
        names = [str(s.get("name", "")) for s in config["steps"]]
        assert "Fetch the bundled models" in names, f"{name} never fetches the models"
        runtime = next(i for i, n in enumerate(names) if "LLM runtime" in n)
        fetch = names.index("Fetch the bundled models")
        assert fetch > runtime, f"{name} fetches models before installing the runtime"

        for step in config["steps"]:
            if str(step.get("name")) == "Install dependencies":
                assert "fetch_models" not in step["run"], (
                    f"{name} still fetches models during the dependency install"
                )


def test_the_fetcher_does_not_skip_on_a_missing_runtime():
    root = WORKFLOW.parents[2]
    source = (root / "buildtools" / "fetch_models.py").read_text()
    assert "find_spec(\"llama_cpp\")" not in source, (
        "the fetcher must not decide what to download from what is installed"
    )
    assert "fetch_llm(force=force)" in source


def test_the_audit_model_runs_on_cpu_only():
    """Regression: a macOS build appeared to hang probing Metal kernels.

    The runner advertises a GPU it cannot use, so llama.cpp walked every kernel
    printing "not supported" and loaded at a crawl.
    """
    root = WORKFLOW.parents[2]
    source = (root / "app" / "detection" / "auditor.py").read_text()
    assert "n_gpu_layers=0" in source
    assert "GGML_METAL" in source


def test_ci_smoke_runs_do_not_invoke_the_model_per_page():
    """It is exercised once, briefly, not across whole documents."""
    for name, config in _workflow()["jobs"].items():
        for step in config["steps"]:
            if "End-to-end CLI run" in str(step.get("name", "")):
                assert step.get("env", {}).get("DOCANON_LLM") == "0", (
                    f"{name} runs the audit model over every page in a smoke test"
                )
        smoke = next(
            (s for s in config["steps"] if "Smoke-test the audit model" in str(s.get("name", ""))),
            None,
        )
        assert smoke is not None, f"{name} never proves the model loads"
        assert smoke.get("timeout-minutes"), f"{name}'s model smoke test has no ceiling"


def test_every_job_has_a_timeout():
    """A stalled job must fail in minutes, not consume the six-hour limit."""
    for name, config in _workflow()["jobs"].items():
        assert config.get("timeout-minutes"), f"{name} has no timeout"
