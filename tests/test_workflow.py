"""The build and the tests must not drift apart (spec sections 77 and 99)."""

from __future__ import annotations

from pathlib import Path

import pytest
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
        assert "en_core_web_trf" in steps


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


def test_no_job_installs_the_audit_model():
    """Removed: it proposed labels, headings and figures as often as real PII.

    Every acceptance was a chance to damage a document, and the deterministic
    re-scan of the output covers the useful part of what it did. It is opt-in
    now and no build ships it.
    """
    for name, config in _workflow()["jobs"].items():
        steps = " ".join(str(s.get("run", "")) for s in config["steps"])
        assert "llama-cpp-python" not in steps, f"{name} still installs the runtime"
        assert "--with-llm" not in steps, f"{name} still fetches the model"


def _superseded_test_the_llm_runtime_is_no_longer_optional_for_shipped_builds():
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


def _superseded_test_shipped_builds_require_the_audit_model():
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
    for required in ("gliner-pii", "llm/*.gguf", "en_core_web_trf", "PySide6", "onnxruntime"):
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


def _superseded_test_models_are_fetched_after_the_runtime_is_installed():
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


def _superseded_test_the_audit_model_runs_on_cpu_only():
    """Regression: a macOS build appeared to hang probing Metal kernels.

    The runner advertises a GPU it cannot use, so llama.cpp walked every kernel
    printing "not supported" and loaded at a crawl.
    """
    root = WORKFLOW.parents[2]
    source = (root / "app" / "detection" / "auditor.py").read_text()
    assert "n_gpu_layers=0" in source
    assert "GGML_METAL" in source


def _superseded_test_ci_smoke_runs_do_not_invoke_the_model_per_page():
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


def test_the_detection_model_is_still_fetched():
    """GLiNER stays. It types values; it does not propose them."""
    for name, config in _workflow()["jobs"].items():
        steps = " ".join(str(s.get("run", "")) for s in config["steps"])
        assert "fetch_models" in steps, f"{name} never fetches GLiNER"


def test_the_audit_model_is_off_by_default():
    root = WORKFLOW.parents[2]
    session = (root / "app" / "session.py").read_text()
    assert 'os.environ.get("DOCANON_LLM", "0")' in session
    engine = (root / "app" / "detection" / "engine.py").read_text()
    assert "use_llm: bool = False" in engine


def test_the_build_never_requires_the_llm_by_default():
    """Regression: verify_bundle.check() was still called with the OLD flag
    (--no-llm, never passed by CI), so it always required the model even
    though the build itself no longer bundled it. Both Windows and macOS
    completed PyInstaller successfully and then failed the self-check.
    """
    root = WORKFLOW.parents[2]
    source = (root / "buildtools" / "build.py").read_text()
    assert "args.no_llm" not in source, "stale flag still referenced"
    assert "require_llm=args.with_llm" in source
    assert '"--no-llm"' not in source, "the CLI flag was renamed to --with-llm"


def test_ci_never_passes_with_llm():
    for name, config in _workflow()["jobs"].items():
        for step in config["steps"]:
            run = str(step.get("run", ""))
            assert "--with-llm" not in run, f"{name} would require the 1.1GB model"


def test_verify_bundle_rejects_en_core_web_sm_if_present(tmp_path):
    """en_core_web_trf is the only spaCy model this project ships. A bundle
    containing sm or lg alongside it - not instead of it - must still fail,
    since neither should ever be present at all."""
    import importlib.util

    root = WORKFLOW.parents[2]
    spec = importlib.util.spec_from_file_location(
        "verify_bundle", root / "buildtools" / "verify_bundle.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    bundle = tmp_path / "dist" / "DocumentAnonymizer"
    (bundle / "en_core_web_sm").mkdir(parents=True)
    (bundle / "en_core_web_sm" / "junk").write_text("x")
    (bundle / "en_core_web_trf").mkdir(parents=True)
    (bundle / "en_core_web_trf" / "config.cfg").write_text("x")

    module.ROOT = tmp_path
    problems = module.check(require_llm=False)
    assert any("en_core_web_sm" in p for p in problems)


def test_ci_workflow_fetches_trf_not_sm():
    workflow = (WORKFLOW.parents[2] / ".github" / "workflows" / "build-release.yml").read_text()
    assert "en_core_web_trf" in workflow
    assert "en_core_web_sm" not in workflow


def test_self_test_mode_exists_and_reports_failure_clearly(capsys):
    """Real gap found while diagnosing a live crash: every CI smoke test ran
    `python -m app.cli` - the runner's own, normal, unfrozen Python, with
    each package's binaries properly isolated. That structurally cannot
    catch a PyInstaller-freezing-specific bundling bug (two packages
    vendoring conflicting copies of an overlapping shared library, merged
    into one flat directory only when frozen) - which is exactly what
    produced a real reported crash: `SystemError: <class 'ImportError'>
    returned a result with an exception set`, in the packaged .exe only,
    never in any CI smoke test. `--self-test` runs the same operation
    (load en_core_web_trf, load GLiNER, run one analysis) inside whatever
    process invokes it - unfrozen here, but the FROZEN executable when CI
    or a person runs it directly - and must fail loudly, not silently.

    Called in-process rather than via a fresh subprocess: a second, fully
    separate interpreter reloading the whole torch/spacy/trf stack on top
    of an already-heavy test run is real resource contention this sandbox
    (a ~3.9 GB-RAM container) cannot always absorb, which is a fact about
    THIS test's own footprint, not about the function it is testing. The
    real frozen-executable invocation always runs as its own dedicated CI
    step, never nested inside a long pytest run, so this does not weaken
    what actually gets verified in CI.
    """
    from app import main as main_module

    result_file = main_module._log_path().parent / "self-test-result.txt"
    result_file.unlink(missing_ok=True)

    exit_code = main_module._self_test()
    captured = capsys.readouterr()
    assert exit_code == 0, captured.out + captured.err
    # The real bug this whole feature exists to catch: a --windowed
    # PyInstaller build has NO console attached, so print() output alone
    # is not a reliable diagnostic - a real user ran this and got nothing
    # back. The result must ALSO always land in a plain file, independent
    # of whether console output worked at all.
    assert result_file.exists(), "self-test must always write its result to a file"
    assert "PASSED" in result_file.read_text()
    assert "PASSED" in captured.out


def test_ci_workflow_actually_executes_the_frozen_binary():
    """The build step alone only proves PyInstaller did not error while
    freezing - it says nothing about whether the frozen result actually
    runs. Both platform jobs must invoke the real artifact with
    --self-test, not just build it and upload it."""
    workflow = (WORKFLOW.parents[2] / ".github" / "workflows" / "build-release.yml").read_text()
    assert workflow.count("--self-test") >= 2, (
        "expected a --self-test invocation on both the Windows .exe and "
        "the macOS .app, after their respective build steps"
    )
    assert "DocumentAnonymizer.exe --self-test" in workflow
    assert "DocumentAnonymizer.app/Contents/MacOS/DocumentAnonymizer --self-test" in workflow


def test_self_test_writes_result_file_even_when_console_output_is_useless(capsys, monkeypatch):
    """Reproduces the actual reported symptom: a user ran
    `DocumentAnonymizer.exe --self-test` and got a blank response, because
    --windowed PyInstaller builds have no console attached and print()
    output goes nowhere. This must never leave the user with nothing to
    look at, regardless of whether stdout is usable at all."""
    from app import main as main_module

    result_file = main_module._log_path().parent / "self-test-result.txt"
    result_file.unlink(missing_ok=True)

    def boom(*args, **kwargs):
        raise SystemError("<class 'ImportError'> returned a result with an exception set")

    monkeypatch.setattr("spacy.load", boom)
    exit_code = main_module._self_test()
    assert exit_code == 1
    assert result_file.exists()
    content = result_file.read_text()
    assert "FAIL" in content
    assert "SystemError" in content
    assert "ImportError" in content


def test_transformers_package_is_collected_and_verified():
    """Real, reported crash on the FIRST actual CI run of --self-test on
    macOS: FileNotFoundError on
    Contents/Frameworks/transformers/models/__init__.pyc. spacy_transformers
    depends on transformers, but PyInstaller bundling a package's traced
    imports into the PYZ archive is not the same as preserving its loose
    files on disk - transformers.models scans its own package directory
    with os.listdir() at import time, a dynamic filesystem read no static
    import-analysis catches. The self-test step added specifically to catch
    frozen-build-only bugs caught exactly this, on its first real run.
    """
    import buildtools.build as build_module
    import buildtools.verify_bundle as verify_module

    assert "transformers" in build_module.COLLECT_ALL
    required_globs = [glob for _, glob in verify_module.REQUIRED]
    assert any("transformers/models/__init__" in g for g in required_globs), (
        "verify_bundle.py must check for this file specifically - it is "
        "the exact thing that was missing, and checking for it here catches "
        "the defect immediately after packaging, before the more expensive "
        "self-test step has to load the whole model to find out"
    )


def test_chardet_is_excluded_from_the_build():
    """Real, reported crash: spacy/__init__.py unconditionally imports its
    own cli/download subsystem (never used - the model ships bundled, never
    downloaded at runtime), which pulls in requests, which optionally tries
    chardet before falling back to charset_normalizer. That fallback only
    catches plain ImportError - but chardet broke with the same SystemError
    signature as the earlier numpy ABI issue when frozen, which escapes the
    fallback entirely and broke `import spacy` itself. Verified directly:
    with chardet genuinely unimportable, requests correctly falls through
    to charset_normalizer and spacy imports cleanly end to end - excluding
    chardet from the build reproduces exactly that working state.
    """
    import buildtools.build as build_module
    import buildtools.verify_bundle as verify_module

    assert "chardet" in build_module.EXCLUDE_MODULES
    verify_source = open(verify_module.__file__).read()
    assert "chardet" in verify_source and "**/chardet/**" in verify_source, (
        "verify_bundle.py must reject chardet if it is ever present, "
        "catching a regression if the build-time exclusion is removed"
    )


def test_transformers_hook_collects_py_files_as_loose_files():
    """Real, reported crash on TWO separate real CI runs (Windows, then
    macOS), even after transformers was added to COLLECT_ALL:
    FileNotFoundError on Contents/Frameworks/transformers/models/__init__.pyc.
    --collect-all does NOT, by itself, keep a package's .py/.pyc files as
    loose files on disk - by PyInstaller's own documented default, those get
    archived into the PYZ instead. transformers.models does its own
    os.listdir() scan of its package directory at import time, which needs
    real loose files. The fix is a dedicated hook using
    collect_data_files(..., include_py_files=True) - verified directly here
    against the real installed package, not just asserted.

    PyInstaller is a build-time-only tool: it is deliberately not part of
    requirements-dev.txt, since the general "run the test suite" CI job has
    no reason to install a packaging tool it never invokes - only the
    actual Windows/macOS build jobs need it, and they install it
    separately as part of building. Skip cleanly rather than fail when it
    is not present, exactly as it correctly is not in that job.
    """
    pytest.importorskip("PyInstaller")
    from PyInstaller.utils.hooks import collect_data_files

    datas = collect_data_files("transformers", include_py_files=True)
    assert any(
        dest == "transformers/models" and src.endswith("__init__.py")
        for src, dest in datas
    ), "the exact file that was missing in the real crash must be collected"


def test_build_wires_the_transformers_hook_and_ci_verifies_the_bundle():
    """The hook file existing is not enough - build.py must actually load
    it, and verify_bundle.py (which checks for exactly this file) must
    actually run in CI. It existed but was never wired into the workflow -
    it would have caught this regression days earlier than the self-test
    did, for a fraction of the cost."""
    import buildtools.build as build_module

    hook_file = WORKFLOW.parents[2] / "buildtools" / "hooks" / "hook-transformers.py"
    assert hook_file.exists()
    build_source = open(build_module.__file__).read()
    assert "--additional-hooks-dir" in build_source

    workflow = WORKFLOW.read_text()
    assert workflow.count("buildtools/verify_bundle.py") >= 2, (
        "verify_bundle.py must run on both the Windows and macOS build jobs"
    )
