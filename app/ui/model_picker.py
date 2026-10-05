"""Mandatory first-launch dialog: choose and download a local LLM.

No model ships bundled anymore (see app/llm/__init__.py for why). The
main window must not open until this dialog has completed successfully -
there is deliberately no "skip" or "use later" option, matching the
explicit instruction that the picker is mandatory, not a suggestion.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..llm.catalog import CATALOG, LlmChoice
from ..llm.downloader import DownloadError, download, is_downloaded
from ..llm.hardware import detect_hardware, recommend
from ..llm.selection import get_selected, set_selected

log = logging.getLogger(__name__)


class _DownloadWorker(QThread):
    """One dedicated thread per download attempt, not a reused pool
    thread - a download can run for minutes, and the teardown rules this
    follows (let Qt own the thread's lifetime via deleteLater, never
    disconnect signals mid-run) are the same hard-won ones already
    documented on TaskRunner in widgets.py, which exist for a reason;
    re-deriving a different, less careful pattern here would risk the
    exact segfault classes that comment describes avoiding.
    """

    progress = Signal(int, int)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, choice: LlmChoice, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._choice = choice
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            path = download(
                self._choice,
                on_progress=lambda w, t: self.progress.emit(w, t),
                cancel=lambda: self._cancelled,
            )
        except DownloadError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - report it, do not vanish
            log.exception("model download failed unexpectedly")
            self.failed.emit(str(exc))
            return
        if self._cancelled:
            self.failed.emit("cancelled")
            return
        self.succeeded.emit(path)


class ModelPickerDialog(QDialog):
    """Blocks until a model is chosen and fully downloaded. Closing the
    dialog any other way (the window's own X button, Escape) is treated
    the same as Cancel - there is no silent way past this screen."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Choose a local AI model")
        self.setMinimumWidth(560)
        self._worker: Optional[_DownloadWorker] = None
        self._selected_path = None

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Document Anonymizer uses a local AI model to double-check detection "
            "on ambiguous cases - never sent anywhere, runs entirely on this "
            "machine. Pick one below and it will be downloaded now."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        hardware = detect_hardware()
        recommended = recommend(hardware)
        if hardware.detected:
            hw_label = QLabel(
                f"Detected: {hardware.total_ram_gb:.0f} GB RAM, "
                f"{hardware.logical_cpu_count} CPU threads."
            )
        else:
            hw_label = QLabel(
                "Could not detect this machine's hardware - recommending the "
                "smallest, safest option."
            )
        hw_label.setStyleSheet("color: gray;")
        layout.addWidget(hw_label)

        self._group = QButtonGroup(self)
        self._buttons: dict[str, QRadioButton] = {}
        for choice in CATALOG:
            text = f"{choice.label} - {choice.size_gb:.1f} GB"
            if choice.id == recommended.id:
                text += "  (recommended for this machine)"
            button = QRadioButton(text)
            button.setToolTip(choice.description)
            desc = QLabel(choice.description)
            desc.setWordWrap(True)
            desc.setStyleSheet("color: gray; margin-left: 22px; margin-bottom: 8px;")
            layout.addWidget(button)
            layout.addWidget(desc)
            self._group.addButton(button)
            self._buttons[choice.id] = button
            if choice.id == recommended.id:
                button.setChecked(True)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        self._status = QLabel("")
        self._status.setStyleSheet("color: gray;")
        layout.addWidget(self._status)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.clicked.connect(self._on_cancel_clicked)
        button_row.addWidget(self._cancel_button)
        self._download_button = QPushButton("Download and continue")
        self._download_button.setDefault(True)
        self._download_button.clicked.connect(self._on_download_clicked)
        button_row.addWidget(self._download_button)
        layout.addLayout(button_row)

    def _selected_choice(self) -> Optional[LlmChoice]:
        for choice in CATALOG:
            if self._buttons[choice.id].isChecked():
                return choice
        return None

    def _set_busy(self, busy: bool) -> None:
        for button in self._buttons.values():
            button.setEnabled(not busy)
        self._download_button.setEnabled(not busy)
        self._progress.setVisible(busy)

    def _on_download_clicked(self) -> None:
        choice = self._selected_choice()
        if choice is None:
            return
        if is_downloaded(choice):
            set_selected(choice)
            self._selected_path = choice
            self.accept()
            return
        self._set_busy(True)
        self._status.setText(f"Downloading {choice.label}...")
        self._worker = _DownloadWorker(choice, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.succeeded.connect(lambda path, c=choice: self._on_succeeded(c))
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_progress(self, written: int, total: int) -> None:
        if total:
            self._progress.setMaximum(100)
            self._progress.setValue(int(written * 100 / total))
        written_gb = written / 1e9
        total_gb = total / 1e9
        self._status.setText(f"Downloading... {written_gb:.2f} / {total_gb:.2f} GB")

    def _on_succeeded(self, choice: LlmChoice) -> None:
        set_selected(choice)
        self._selected_path = choice
        self._status.setText("Done.")
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        if message == "cancelled":
            self._status.setText("Cancelled. The partial download was kept - choose "
                                  "the same option again to resume it.")
            return
        self._status.setText("")
        QMessageBox.critical(
            self,
            "Download failed",
            f"Could not download the model:\n\n{message}\n\n"
            "Check the internet connection and try again.",
        )

    def _on_cancel_clicked(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            return
        self.reject()

    def reject(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            # Mandatory: closing the window mid-download is a cancel, not
            # a silent way past the picker - stop the worker cleanly
            # first rather than tearing down while it is still writing.
            self._worker.cancel()
            self._worker.wait(5000)
        super().reject()


def ensure_model_selected(parent: Optional[QWidget] = None) -> bool:
    """Returns True if a model is selected and ready (already chosen on a
    prior launch, or just chosen now) - False if the user declined, in
    which case the caller must not open the main window."""
    if get_selected() is not None:
        return True
    dialog = ModelPickerDialog(parent)
    result = dialog.exec()
    return result == QDialog.DialogCode.Accepted and get_selected() is not None
