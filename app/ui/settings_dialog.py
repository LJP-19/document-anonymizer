"""Settings: one place for every preference.

Changes are saved as they are made (no Save button to forget). Anything an
environment variable decides is shown locked, with the reason - CI and IT
policies pin behaviour that way, and a control that silently does nothing is
worse than one that says why.

Opened from the toolbar "Settings" button, Ctrl+, (Windows) or the application
menu's Settings... entry, Cmd+, (macOS).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QLayout,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from .. import settings
from ..version import APP_NAME, __version__

log = logging.getLogger(__name__)


def _box(title: str) -> tuple:
    """A titled section. Spacing lives in the LAYOUT: stylesheet padding on a
    QGroupBox is not counted when Qt sizes its contents, which clipped the
    explanation lines under each control."""
    box = QGroupBox(title)
    column = QVBoxLayout(box)
    column.setContentsMargins(12, 18, 12, 10)
    column.setSpacing(4)
    return box, column


def _hint(text: str) -> QLabel:
    """Explanatory text under a control. Lines are broken by hand ("\n"): Qt sizes
    a box of several word-wrapped labels for one line each and clipped them."""
    label = QLabel(text)
    label.setWordWrap("\n" not in text)
    label.setObjectName("CardMeta")
    label.setContentsMargins(24, 0, 0, 4)
    label.setMinimumWidth(520)
    return label


class SettingsDialog(QDialog):
    #: Emitted after any change, so the main window can refresh its controls.
    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None, default_output: str = "", busy: bool = False):
        super().__init__(parent)
        #: A document is being analysed: the loaded model may be in use by that
        #: thread, so it must not be switched (freeing it mid-call would crash).
        self._busy = busy
        self.setWindowTitle("Settings")
        self.setMinimumWidth(600)
        self._default_output = default_output
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        # Wrapped explanation lines need their full height: without this the dialog
        # was sized for one line each and clipped them.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.addWidget(self._analysis_box())
        layout.addWidget(self._model_box())
        layout.addWidget(self._output_box())
        layout.addWidget(self._privacy_box())
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        about = QLabel(f"{APP_NAME} {__version__} · runs entirely on this computer")
        about.setObjectName("CardMeta")
        bottom = QHBoxLayout()
        bottom.addWidget(about, 1)
        bottom.addWidget(buttons)
        layout.addLayout(bottom)
        self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Sizes are first computed before the inherited theme (fonts, group-box
        # margins) is applied; re-measure once shown, or the explanation lines
        # under each control are clipped.
        super().showEvent(event)
        for label in self.findChildren(QLabel):
            label.ensurePolished()
            if not label.wordWrap():
                label.setMinimumHeight(label.sizeHint().height())
        for widget in self.findChildren(QWidget):
            widget.updateGeometry()
        self.layout().invalidate()
        self.layout().activate()
        self.adjustSize()

    # -- analysis ----------------------------------------------------------

    def _analysis_box(self) -> QGroupBox:
        box, column = _box("Analysis")
        self.thorough_radio = QRadioButton("Thorough (recommended)")
        self.fast_radio = QRadioButton("Fast")
        group = QButtonGroup(self)
        group.addButton(self.thorough_radio)
        group.addButton(self.fast_radio)
        self.thorough_radio.toggled.connect(lambda checked: checked and self._set_fast(False))
        self.fast_radio.toggled.connect(lambda checked: checked and self._set_fast(True))
        column.addWidget(self.thorough_radio)
        column.addWidget(_hint(
            "Every detection layer, then the optional AI review and a re-scan\n"
            "of the redacted result for anything still identifying."
        ))
        column.addWidget(self.fast_radio)
        column.addWidget(_hint(
            "Skips the AI review and the re-scan, which only add suggestions to review.\n"
            "The same values are redacted automatically; the saved-file check still runs."
        ))
        self.ai_review_check = QCheckBox("AI review (uses the local AI model)")
        self.ai_review_check.toggled.connect(self._set_ai_review)
        column.addWidget(self.ai_review_check)
        self.ai_review_hint = _hint("\n")
        column.addWidget(self.ai_review_hint)
        self.careful_check = QCheckBox("Careful mode")
        self.careful_check.toggled.connect(self._set_careful)
        column.addWidget(self.careful_check)
        column.addWidget(_hint(
            "Only change values that were positively identified. Finds less, never\n"
            "alters figures or form text; use Add missed item for anything it leaves."
        ))
        column.addWidget(_hint("Analysis settings apply to the next document analysed."))
        return box

    # -- AI model ------------------------------------------------------------

    def _model_box(self) -> QGroupBox:
        box, column = _box("AI model")
        self.model_label = QLabel("")
        self.model_label.setWordWrap(True)
        column.addWidget(self.model_label)
        row = QHBoxLayout()
        self.change_model_button = QPushButton("Change AI model…")
        self.change_model_button.clicked.connect(self._change_model)
        self.delete_unused_button = QPushButton("")
        self.delete_unused_button.clicked.connect(self._delete_unused)
        row.addWidget(self.change_model_button)
        row.addWidget(self.delete_unused_button)
        row.addStretch(1)
        column.addLayout(row)
        return box

    # -- output ----------------------------------------------------------------

    def _output_box(self) -> QGroupBox:
        box, column = _box("Output")
        column.addWidget(_hint("Default folder for anonymized files (used for new windows)."))
        row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.editingFinished.connect(self._save_output)
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose_output)
        row.addWidget(self.output_edit, 1)
        row.addWidget(choose)
        column.addLayout(row)
        return box

    # -- privacy -----------------------------------------------------------------

    def _privacy_box(self) -> QGroupBox:
        from ..entities.roster import PRIVATE_MAPPINGS_DIR

        box, column = _box("Privacy")
        column.addWidget(_hint(
            "Mapping workbooks (original → pseudonym) are re-identification material. "
            "They are kept in a private folder on this computer only, never beside the "
            "outputs. Delete the folder to remove them."
        ))
        row = QHBoxLayout()
        path = QLineEdit(str(PRIVATE_MAPPINGS_DIR))
        path.setReadOnly(True)
        open_button = QPushButton("Open folder")
        open_button.clicked.connect(lambda _checked=False: self._open(PRIVATE_MAPPINGS_DIR))
        row.addWidget(path, 1)
        row.addWidget(open_button)
        column.addLayout(row)
        return box

    # -- state -------------------------------------------------------------------

    def refresh(self) -> None:
        """Show the EFFECTIVE value of every setting, locked where the environment decides."""
        from ..detection import auditor
        from ..llm.catalog import CATALOG
        from ..llm.downloader import downloaded_bytes
        from ..llm.selection import get_selected

        for widget in (self.thorough_radio, self.fast_radio, self.ai_review_check, self.careful_check):
            widget.blockSignals(True)
        try:
            fast = settings.fast_mode()
            fast_locked = settings._env_flag("DOCANON_FAST") is not None
            self.fast_radio.setChecked(fast)
            self.thorough_radio.setChecked(not fast)
            for radio in (self.fast_radio, self.thorough_radio):
                radio.setEnabled(not fast_locked)
                radio.setToolTip("Set by DOCANON_FAST." if fast_locked else "")

            model_ready = False
            try:
                model_ready = auditor.LlmAuditor().configured
            except Exception:  # noqa: BLE001
                model_ready = False
            env = auditor.llm_env_override()
            self.ai_review_check.setChecked(auditor.llm_audit_enabled())
            if env is not None:
                self.ai_review_check.setEnabled(False)
                self.ai_review_hint.setText("Set by the DOCANON_LLM environment variable.")
            elif fast:
                self.ai_review_check.setEnabled(False)
                self.ai_review_hint.setText("Fast mode skips the AI review.")
            elif not model_ready:
                self.ai_review_check.setEnabled(False)
                self.ai_review_hint.setText("No AI model is ready - choose one below.")
            else:
                self.ai_review_check.setEnabled(True)
                self.ai_review_hint.setText(
                    "Reviews every page for misses and checks each redaction.\n"
                    "More thorough, slower; Skip AI review stops it at any time."
                )

            careful_locked = settings._env_flag("DOCANON_CONSERVATIVE") is not None
            self.careful_check.setChecked(settings.careful_mode())
            self.careful_check.setEnabled(not careful_locked)
            self.careful_check.setToolTip("Set by DOCANON_CONSERVATIVE." if careful_locked else "")
        finally:
            for widget in (self.thorough_radio, self.fast_radio, self.ai_review_check, self.careful_check):
                widget.blockSignals(False)

        self.change_model_button.setEnabled(not self._busy)
        self.change_model_button.setToolTip(
            "Available once the current analysis has finished." if self._busy else ""
        )
        current = get_selected()
        if current is None:
            self.model_label.setText("No AI model chosen. The AI review is off until one is.")
        else:
            self.model_label.setText(
                f"Current: {current.label} · {current.size_gb:.1f} GB · "
                f"stored in {Path.home() / '.document-anonymizer' / 'models' / 'llm'}"
            )
        unused = [c for c in CATALOG if (current is None or c.id != current.id) and downloaded_bytes(c)]
        unused_bytes = sum(downloaded_bytes(c) for c in unused)
        self.delete_unused_button.setVisible(bool(unused))
        self.delete_unused_button.setText(
            f"Delete other downloaded models ({unused_bytes / 1e9:.1f} GB)"
        )
        if not self.output_edit.hasFocus():
            self.output_edit.setText(settings.output_folder(self._default_output))

    def _save(self, key: str, value) -> None:
        try:
            settings.put(key, value)
        except OSError:
            log.exception("could not save setting %s", key)
            QMessageBox.warning(self, "Not saved", "The setting could not be saved to disk.")
        self.refresh()
        self.changed.emit()

    def _set_fast(self, fast: bool) -> None:
        self._save("fast_mode", bool(fast))

    def _set_ai_review(self, checked: bool) -> None:
        self._save("ai_review", bool(checked))

    def _set_careful(self, checked: bool) -> None:
        self._save("careful_mode", bool(checked))

    def _save_output(self) -> None:
        text = self.output_edit.text().strip()
        if text:
            self._save("output_folder", text)

    def _choose_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Default output folder", self.output_edit.text())
        if folder:
            self.output_edit.setText(folder)
            self._save("output_folder", folder)

    def _open(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _change_model(self) -> None:
        from ..detection import auditor
        from .model_picker import ModelPickerDialog

        dialog = ModelPickerDialog(self, changing=True)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            auditor.reset_model()      # the next analysis loads the new choice
            self.refresh()
            self.changed.emit()

    def _delete_unused(self) -> None:
        from ..llm.catalog import CATALOG
        from ..llm.downloader import delete_model, downloaded_bytes
        from ..llm.selection import get_selected

        current = get_selected()
        unused = [c for c in CATALOG if (current is None or c.id != current.id) and downloaded_bytes(c)]
        if not unused:
            return
        names = ", ".join(c.label for c in unused)
        answer = QMessageBox.question(
            self, "Delete downloaded models",
            f"Delete {names}? The current model is kept. A deleted model can be "
            "downloaded again from Change AI model.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # The current model is kept, so the loaded one is untouched (it may be in
        # use by an analysis right now).
        freed = sum(delete_model(c) for c in unused)
        self.refresh()
        QMessageBox.information(self, "Deleted", f"Freed {freed / 1e9:.1f} GB.")
