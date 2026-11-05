"""Regression tests for the first-launch LLM picker system (app/llm/,
app/ui/model_picker.py). Each test here was first verified as a standalone
script while building the feature - this file converts that verification
into permanent coverage rather than leaving it as one-off manual checks.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

# Every test here touches ~/.document-anonymizer, so every test is isolated -
# see isolated_home in conftest.py for why HOME alone is not enough.
pytestmark = pytest.mark.usefixtures("isolated_home")


# ---------------------------------------------------------------------------
# hardware.py
# ---------------------------------------------------------------------------


def test_recommend_picks_the_largest_tier_the_real_ram_covers():
    from app.llm.catalog import CATALOG
    from app.llm.hardware import HardwareInfo, recommend

    expected = {
        2: "small", 4: "small",
        8: "balanced",
        16: "larger",
        32: "largest", 64: "largest",
    }
    for ram_gb, expected_id in expected.items():
        hw = HardwareInfo(total_ram_gb=ram_gb, logical_cpu_count=4, detected=True)
        assert recommend(hw).id == expected_id, f"{ram_gb} GB RAM"
    assert {c.min_ram_gb for c in CATALOG} == {4, 8, 16, 32}, (
        "this test's thresholds must track the real catalog, not duplicate "
        "stale numbers if the catalog changes"
    )


def test_recommend_falls_back_to_smallest_if_detection_failed():
    from app.llm.hardware import HardwareInfo, recommend

    failed = HardwareInfo(total_ram_gb=0, logical_cpu_count=1, detected=False)
    assert recommend(failed).id == "small"


def test_detect_hardware_never_raises_even_if_psutil_is_broken():
    from app.llm.hardware import detect_hardware

    with patch("psutil.virtual_memory", side_effect=RuntimeError("boom")):
        hw = detect_hardware()
    assert hw.detected is False


# ---------------------------------------------------------------------------
# downloader.py
# ---------------------------------------------------------------------------


def _fake_choice(files=(("test.gguf", 10_000_000),)):
    from app.llm.catalog import LlmChoice, ModelFile

    return LlmChoice(
        id="test", label="Test", repo="fake/repo", revision="a" * 40,
        files=tuple(ModelFile(name, size) for name, size in files),
        min_ram_gb=4, description="test",
    )


def _chunked(total_bytes: int, chunk_size: int):
    content = b"x" * total_bytes
    return [content[i : i + chunk_size] for i in range(0, len(content), chunk_size)]


def _response(status, chunks):
    response = MagicMock()
    response.status_code = status
    response.iter_content.return_value = chunks
    response.__enter__ = lambda self: response
    response.__exit__ = lambda self, *a: None
    return response


def _partial(choice, index=0):
    from app.llm import downloader

    path = downloader.file_path(choice.files[index])
    return path.with_suffix(path.suffix + ".part")


def test_download_writes_the_right_file_and_reports_full_progress():
    from app.llm import downloader

    choice = _fake_choice()
    progress = []
    with patch("requests.get", return_value=_response(200, _chunked(choice.size_bytes, downloader.CHUNK_SIZE))):
        result = downloader.download(choice, on_progress=lambda w, t: progress.append((w, t)))

    assert result.exists() and result.stat().st_size == choice.size_bytes
    assert progress[-1] == (choice.size_bytes, choice.size_bytes)
    assert downloader.is_downloaded(choice)


def test_cancel_leaves_a_resumable_partial_and_resume_sends_the_right_range():
    from app.llm import downloader

    choice = _fake_choice()
    chunks = _chunked(choice.size_bytes, downloader.CHUNK_SIZE)
    calls = {"n": 0}

    def cancel_after_3():
        calls["n"] += 1
        return calls["n"] > 3

    with patch("requests.get", return_value=_response(200, chunks)):
        downloader.download(choice, cancel=cancel_after_3)

    partial = _partial(choice)
    assert partial.exists() and 0 < partial.stat().st_size < choice.size_bytes
    assert not downloader.is_downloaded(choice)

    resumed_from = partial.stat().st_size
    seen = {}

    def capture_get(url, headers=None, **kwargs):
        seen.update(headers or {})
        return _response(206, chunks[3:])

    with patch("requests.get", side_effect=capture_get):
        result = downloader.download(choice)

    assert seen.get("Range") == f"bytes={resumed_from}-"
    assert result.stat().st_size == choice.size_bytes
    assert downloader.is_downloaded(choice)


def test_a_download_that_comes_up_short_is_kept_for_resume_not_accepted():
    from app.llm import downloader

    choice = _fake_choice()
    with patch("requests.get", return_value=_response(200, [b"x" * (choice.size_bytes // 2)])):
        with pytest.raises(downloader.DownloadError, match="resume"):
            downloader.download(choice)
    assert not downloader.is_downloaded(choice)
    assert _partial(choice).stat().st_size == choice.size_bytes // 2


def test_a_download_larger_than_expected_is_discarded_not_kept():
    from app.llm import downloader

    choice = _fake_choice()
    with patch("requests.get", return_value=_response(200, [b"x" * (choice.size_bytes + 1000)])):
        with pytest.raises(downloader.DownloadError, match="discarded"):
            downloader.download(choice)
    assert not downloader.is_downloaded(choice)
    assert not _partial(choice).exists(), "a file that cannot be a prefix of the real one must not be resumed"


def test_a_partial_that_already_has_every_byte_is_finalised_with_no_network_call():
    """The real Windows report: a complete 1,894,532,128-byte download was
    rejected only because the catalog size was rounded, and left in place as
    a .part file. Once the catalog is right, retrying must reuse it instead
    of downloading 1.9 GB again."""
    import os

    from app.llm import downloader

    choice = _fake_choice()
    partial = _partial(choice)
    partial.touch()
    os.truncate(partial, choice.size_bytes)

    with patch("requests.get", side_effect=AssertionError("the network must not be used")):
        result = downloader.download(choice)

    assert result.exists() and not partial.exists()
    assert downloader.is_downloaded(choice)


def test_a_416_from_the_server_restarts_the_file_instead_of_failing():
    from app.llm import downloader

    choice = _fake_choice()
    partial = _partial(choice)
    partial.write_bytes(b"x" * 3_000_000)
    responses = [_response(416, []), _response(200, _chunked(choice.size_bytes, downloader.CHUNK_SIZE))]

    with patch("requests.get", side_effect=responses):
        downloader.download(choice)

    assert downloader.is_downloaded(choice)


def test_downloads_come_from_the_pinned_revision_never_main():
    from app.llm import downloader

    choice = _fake_choice()
    urls = []

    def capture_get(url, headers=None, **kwargs):
        urls.append(url)
        return _response(200, _chunked(choice.size_bytes, downloader.CHUNK_SIZE))

    with patch("requests.get", side_effect=capture_get):
        downloader.download(choice)

    assert urls == [f"https://huggingface.co/fake/repo/resolve/{'a' * 40}/test.gguf"]


def test_a_split_model_downloads_every_shard_with_one_continuous_progress():
    from app.llm import downloader

    choice = _fake_choice(files=(("m-00001-of-00002.gguf", 6_000_000), ("m-00002-of-00002.gguf", 4_000_000)))
    sizes = {f.filename: f.size_bytes for f in choice.files}

    def by_url(url, headers=None, **kwargs):
        name = url.rsplit("/", 1)[1]
        return _response(200, _chunked(sizes[name], downloader.CHUNK_SIZE))

    progress = []
    with patch("requests.get", side_effect=by_url):
        result = downloader.download(choice, on_progress=lambda w, t: progress.append((w, t)))

    assert result.name == "m-00001-of-00002.gguf", "llama.cpp must be pointed at the FIRST shard"
    assert all(total == 10_000_000 for _, total in progress)
    assert [w for w, _ in progress] == sorted(w for w, _ in progress), "progress must never go backwards"
    assert progress[-1][0] == 10_000_000
    assert downloader.is_downloaded(choice)


def test_a_split_model_is_not_downloaded_until_every_shard_is_present():
    import os

    from app.llm import downloader

    choice = _fake_choice(files=(("m-00001-of-00002.gguf", 6_000_000), ("m-00002-of-00002.gguf", 4_000_000)))
    first = downloader.file_path(choice.files[0])
    first.touch()
    os.truncate(first, 6_000_000)
    assert not downloader.is_downloaded(choice)

    second = downloader.file_path(choice.files[1])
    second.touch()
    os.truncate(second, 4_000_000)
    assert downloader.is_downloaded(choice)


def test_catalog_is_exact_pinned_and_not_rounded():
    """The first catalog used rounded sizes and one filename that does not
    exist; both shipped and broke real downloads. The exact numbers below
    were read from the Hugging Face API; tests/test_llm_catalog_live.py
    re-checks them against the live API whenever the network is reachable."""
    import re

    from app.llm.catalog import CATALOG

    expected = {
        "small": ("Qwen/Qwen2.5-0.5B-Instruct-GGUF", {"qwen2.5-0.5b-instruct-q4_k_m.gguf": 491_400_032}),
        "balanced": ("Qwen/Qwen2.5-1.5B-Instruct-GGUF", {"qwen2.5-1.5b-instruct-q8_0.gguf": 1_894_532_128}),
        "larger": ("Qwen/Qwen2.5-3B-Instruct-GGUF", {"qwen2.5-3b-instruct-q4_k_m.gguf": 2_104_932_768}),
        "largest": (
            "Qwen/Qwen2.5-7B-Instruct-GGUF",
            {
                "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf": 3_993_201_344,
                "qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf": 689_872_288,
            },
        ),
    }
    assert {c.id for c in CATALOG} == set(expected)
    for choice in CATALOG:
        repo, files = expected[choice.id]
        assert choice.repo == repo
        assert {f.filename: f.size_bytes for f in choice.files} == files
        assert re.fullmatch(r"[0-9a-f]{40}", choice.revision), "must be pinned to a full commit hash"
        for f in choice.files:
            assert f.size_bytes % 1_000_000 != 0, f"{f.filename}: a rounded size is an estimate, not a measurement"
    split = next(c for c in CATALOG if c.id == "largest")
    assert split.filename.endswith("-00001-of-00002.gguf"), "llama.cpp loads a split model from its first shard"


# ---------------------------------------------------------------------------
# selection.py
# ---------------------------------------------------------------------------


def test_a_selection_marker_is_never_trusted_unless_the_file_still_matches(tmp_path, monkeypatch):
    from app.llm import downloader, selection
    from app.llm.catalog import CATALOG

    choice = CATALOG[0]
    assert selection.get_selected() is None

    selection.set_selected(choice)
    assert selection.get_selected() is None, "marked but never downloaded must not be trusted"

    # A sparse file reports the real size via stat() - all is_downloaded()
    # actually checks - without writing real bytes to disk, which matters
    # for a multi-hundred-MB catalog entry in a shared, size-limited
    # sandbox.
    path = downloader.model_path(choice)
    path.touch()
    import os

    os.truncate(path, choice.size_bytes)
    assert selection.get_selected() is not None and selection.get_selected().id == choice.id

    os.truncate(path, 100)
    assert selection.get_selected() is None, "truncated after the fact must not be trusted"


# ---------------------------------------------------------------------------
# app/ui/model_picker.py - headless Qt, matching this project's established
# QT_QPA_PLATFORM=offscreen pattern for testing real widgets without a display.
# ---------------------------------------------------------------------------


@pytest.fixture
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_picker_dialog_preselects_the_hardware_recommendation(tmp_path, monkeypatch, qapp):
    from app.llm.hardware import HardwareInfo
    from app.ui.model_picker import ModelPickerDialog

    with patch("app.ui.model_picker.detect_hardware", return_value=HardwareInfo(32, 8, True)):
        dialog = ModelPickerDialog()
    assert dialog._selected_choice().id == "largest"


def test_already_selected_model_skips_the_dialog_entirely(tmp_path, monkeypatch, qapp):
    from app.llm import downloader, selection
    from app.llm.catalog import CATALOG
    from app.ui.model_picker import ensure_model_selected

    choice = CATALOG[1]
    path = downloader.model_path(choice)
    path.touch()
    import os

    os.truncate(path, choice.size_bytes)  # sparse - see the prior test's comment
    selection.set_selected(choice)

    assert ensure_model_selected() is True


def test_full_download_flow_through_the_dialog_accepts_and_persists_selection(
    tmp_path, monkeypatch, qapp
):
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QDialog

    from app.llm.catalog import CATALOG
    from app.ui import model_picker

    choice = CATALOG[0]

    def fake_download(c, on_progress=None, cancel=None):
        for pct in (0, 50, 100):
            if on_progress:
                on_progress(int(c.size_bytes * pct / 100), c.size_bytes)
            time.sleep(0.005)
        from app.llm.downloader import model_path

        path = model_path(c)
        path.touch()
        import os

        os.truncate(path, c.size_bytes)  # sparse - avoids real disk usage
        return path

    dialog = model_picker.ModelPickerDialog()
    dialog._buttons["small"].setChecked(True)

    with patch("app.ui.model_picker.download", side_effect=fake_download):
        dialog._on_download_clicked()
        deadline = time.time() + 5
        while dialog._worker is not None and dialog._worker.isRunning() and time.time() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.01)
        QCoreApplication.processEvents()

    assert model_picker.get_selected() is not None
    assert model_picker.get_selected().id == "small"


def test_cancelling_mid_download_does_not_set_a_selection(tmp_path, monkeypatch, qapp):
    from PySide6.QtCore import QCoreApplication

    from app.llm.catalog import CATALOG
    from app.ui import model_picker

    choice = CATALOG[0]

    def slow_fake_download(c, on_progress=None, cancel=None):
        for pct in range(0, 101, 10):
            if cancel and cancel():
                return None
            if on_progress:
                on_progress(int(c.size_bytes * pct / 100), c.size_bytes)
            time.sleep(0.01)
        return None

    dialog = model_picker.ModelPickerDialog()
    dialog._buttons["small"].setChecked(True)

    with patch("app.ui.model_picker.download", side_effect=slow_fake_download):
        dialog._on_download_clicked()
        time.sleep(0.03)
        QCoreApplication.processEvents()
        dialog._on_cancel_clicked()
        deadline = time.time() + 5
        while dialog._worker is not None and dialog._worker.isRunning() and time.time() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.01)
        QCoreApplication.processEvents()

    assert model_picker.get_selected() is None
    assert dialog._download_button.isEnabled()


# ---------------------------------------------------------------------------
# Guards for the two real problems a Windows CI run exposed
# ---------------------------------------------------------------------------


def test_isolated_home_really_redirects_the_path_the_app_uses(isolated_home):
    """Path.home() is what the app uses for the model folder. On Windows it
    reads USERPROFILE, not HOME - this fails on a Windows runner if the
    fixture ever stops setting it, which is exactly the bug it prevents."""
    from pathlib import Path

    from app.llm import downloader

    assert Path.home().resolve() == isolated_home.resolve()
    assert isolated_home.resolve() in downloader.model_dir().resolve().parents


def test_a_right_sized_but_corrupt_model_file_makes_the_auditor_unavailable_not_crash():
    """A size check cannot tell a real model from garbage of the same size.
    llama.cpp raised a bare ValueError on such a file, which is not the
    AuditorUnavailable every caller in the pipeline handles - so one bad
    download could abort a whole analysis instead of just skipping the
    optional audit pass."""
    pytest.importorskip("llama_cpp")
    import os

    from app.detection.auditor import AuditorUnavailable, LlmAuditor
    from app.llm import downloader, selection
    from app.llm.catalog import CATALOG

    choice = CATALOG[0]
    path = downloader.model_path(choice)
    path.touch()
    os.truncate(path, choice.size_bytes)  # right size, all zeros: not a model
    selection.set_selected(choice)

    auditor = LlmAuditor()
    assert auditor.model_path == path
    assert auditor.available is False
    with pytest.raises(AuditorUnavailable):
        auditor.load()
