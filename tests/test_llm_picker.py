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


def _fake_choice():
    from app.llm.catalog import LlmChoice

    return LlmChoice(
        id="test", label="Test", repo="fake/repo", filename="test.gguf",
        size_bytes=10_000_000, min_ram_gb=4, description="test",
    )


def _chunked(total_bytes: int, chunk_size: int):
    content = b"x" * total_bytes
    return [content[i : i + chunk_size] for i in range(0, len(content), chunk_size)]


def test_download_writes_the_right_file_and_reports_full_progress(tmp_path, monkeypatch):
    from app.llm import downloader

    choice = _fake_choice()
    chunks = _chunked(choice.size_bytes, downloader.CHUNK_SIZE)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.iter_content.return_value = chunks
    mock_response.__enter__ = lambda self: mock_response
    mock_response.__exit__ = lambda self, *a: None

    progress = []
    with patch("requests.get", return_value=mock_response):
        result = downloader.download(choice, on_progress=lambda w, t: progress.append((w, t)))

    assert result.exists()
    assert result.stat().st_size == choice.size_bytes
    assert progress[-1] == (choice.size_bytes, choice.size_bytes)
    assert downloader.is_downloaded(choice)


def test_cancel_leaves_a_resumable_partial_and_resume_sends_the_right_range(tmp_path, monkeypatch):
    from app.llm import downloader

    choice = _fake_choice()
    chunks = _chunked(choice.size_bytes, downloader.CHUNK_SIZE)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.iter_content.return_value = chunks
    mock_response.__enter__ = lambda self: mock_response
    mock_response.__exit__ = lambda self, *a: None

    calls = {"n": 0}

    def cancel_after_3():
        calls["n"] += 1
        return calls["n"] > 3

    with patch("requests.get", return_value=mock_response):
        downloader.download(choice, cancel=cancel_after_3)

    partial = downloader.model_path(choice).with_suffix(".gguf.part")
    assert partial.exists()
    assert 0 < partial.stat().st_size < choice.size_bytes
    assert not downloader.is_downloaded(choice)

    resumed_from = partial.stat().st_size
    mock_resume = MagicMock()
    mock_resume.status_code = 206
    mock_resume.iter_content.return_value = chunks[3:]
    mock_resume.__enter__ = lambda self: mock_resume
    mock_resume.__exit__ = lambda self, *a: None

    seen_headers = {}

    def capture_get(url, headers=None, **kwargs):
        seen_headers.update(headers or {})
        return mock_resume

    with patch("requests.get", side_effect=capture_get):
        result = downloader.download(choice)

    assert seen_headers.get("Range") == f"bytes={resumed_from}-"
    assert result.stat().st_size == choice.size_bytes
    assert downloader.is_downloaded(choice)


def test_a_size_mismatched_download_is_rejected_not_silently_accepted(tmp_path, monkeypatch):
    from app.llm import downloader

    choice = _fake_choice()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.iter_content.return_value = [b"x" * (choice.size_bytes // 2)]
    mock_response.__enter__ = lambda self: mock_response
    mock_response.__exit__ = lambda self, *a: None

    with patch("requests.get", return_value=mock_response):
        with pytest.raises(downloader.DownloadError):
            downloader.download(choice)
    assert not downloader.is_downloaded(choice)


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
