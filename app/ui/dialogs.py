"""Dialogs for editing a detection and adding one the detectors missed."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ..detection.types import PiiType
from ..pseudonymization.generator import generate

#: Offered in the type pickers, in the order a user would look for them.
COMMON_TYPES = [
    PiiType.PERSON, PiiType.ORG_PRIVATE, PiiType.ADDRESS, PiiType.STREET,
    PiiType.CITY_STATE, PiiType.POSTAL_CODE, PiiType.EMAIL, PiiType.PHONE,
    PiiType.SSN, PiiType.ITIN, PiiType.EIN, PiiType.TIN, PiiType.DOB,
    PiiType.BIRTHPLACE, PiiType.CITIZENSHIP, PiiType.GENDER,
    PiiType.BANK_ACCOUNT, PiiType.ROUTING_NUMBER, PiiType.CARD_NUMBER,
    PiiType.EMPLOYEE_ID, PiiType.POLICY_NUMBER, PiiType.MEMBER_ID,
    PiiType.DRIVERS_LICENSE, PiiType.PASSPORT, PiiType.CASE_NUMBER,
    PiiType.USERNAME, PiiType.UNCLASSIFIED_GROUP_VALUE,
]


def coerce_type(value) -> PiiType:
    """Always return a PiiType, whatever Qt handed back."""
    if isinstance(value, PiiType):
        return value
    if isinstance(value, str):
        if value in PiiType.__members__:
            return PiiType[value]
        try:
            return PiiType(value)
        except ValueError:
            pass
    return PiiType.UNCLASSIFIED_GROUP_VALUE


def _label_for(pii_type: PiiType) -> str:
    if pii_type is PiiType.UNCLASSIFIED_GROUP_VALUE:
        return "Other / unlabelled"
    if pii_type is PiiType.ORG_PRIVATE:
        return "Business name"
    return pii_type.value.replace("_", " ").title()


class _TypePicker(QComboBox):
    def __init__(self, selected: Optional[PiiType] = None):
        super().__init__()
        # Store the enum NAME, not the member. PiiType subclasses str, so Qt
        # keeps it as a plain string and hands back a str - which then has no
        # .value, and crashes anything that groups by type.
        for pii_type in COMMON_TYPES:
            self.addItem(_label_for(pii_type), pii_type.name)
        if selected is not None:
            index = self.findData(getattr(selected, "name", selected))
            if index >= 0:
                self.setCurrentIndex(index)

    def selected_type(self) -> PiiType:
        return coerce_type(self.currentData())


class EditDetectionDialog(QDialog):
    """Relabel a detection and adjust its replacement."""

    def __init__(self, parent, original: str, pii_type: PiiType, replacement: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Edit this detection")
        self.setMinimumWidth(460)
        self.original = original
        self._user_edited = False

        layout = QVBoxLayout(self)
        note = QLabel("Correct the detected text if the span is wrong, then set its type.")
        note.setWordWrap(True)
        note.setObjectName("CardValue")
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        self.type_picker = _TypePicker(pii_type)
        self.type_picker.currentIndexChanged.connect(self._on_type_changed)
        self.value_edit = QLineEdit(original)
        form.addRow("Detected value", self.value_edit)
        form.addRow("This is a", self.type_picker)

        self.replacement = QLineEdit(replacement or generate(pii_type, original))
        self.replacement.textEdited.connect(lambda _t: setattr(self, "_user_edited", True))
        form.addRow("Replace with", self.replacement)
        layout.addLayout(form)

        self.apply_all = QCheckBox("Apply to every occurrence of this value")
        self.apply_all.setChecked(True)
        layout.addWidget(self.apply_all)

        hint = QLabel("Changing the type regenerates the replacement to match.")
        hint.setObjectName("CardMeta")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_type_changed(self) -> None:
        # Only overwrite a replacement the user has not typed themselves.
        if not self._user_edited:
            self.replacement.setText(generate(self.type_picker.selected_type(), self.original))

    def result_values(self) -> tuple[PiiType, str, bool]:
        return (
            self.type_picker.selected_type(),
            self.replacement.text().strip(),
            self.apply_all.isChecked(),
        )

    def edited_value(self) -> str:
        """The detected text, which the user may have corrected."""
        return self.value_edit.text().strip() if hasattr(self, "value_edit") else ""


class AddPiiDialog(QDialog):
    """Add a value the detectors missed."""

    def __init__(self, parent, preset: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Add missed PII")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.text = QLineEdit(preset)
        self.text.setPlaceholderText("Exactly as it appears in the document")
        form.addRow("Text to redact", self.text)

        self.type_picker = _TypePicker(PiiType.PERSON)
        self.type_picker.currentIndexChanged.connect(self._refresh)
        form.addRow("This is a", self.type_picker)

        self.replacement = QLineEdit()
        self.replacement.setPlaceholderText("Leave blank to generate one")
        form.addRow("Replace with", self.replacement)
        layout.addLayout(form)

        self.cascade = QCheckBox("Find it on every page")
        self.cascade.setChecked(True)
        self.apply_same = QCheckBox("Redact every occurrence of it")
        self.apply_same.setChecked(True)
        layout.addWidget(self.cascade)
        layout.addWidget(self.apply_same)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.text.textChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        ok_button = self.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        ok_button.setEnabled(bool(self.text.text().strip()))

    def result_values(self) -> tuple[str, PiiType, str, bool, bool]:
        return (
            self.text.text().strip(),
            self.type_picker.selected_type(),
            self.replacement.text().strip(),
            self.cascade.isChecked(),
            self.apply_same.isChecked(),
        )


class UnreviewedPrompt(QDialog):
    """Offered when approved documents still contain unreviewed items.

    Processing only the reviewed items is a real option, not a courtesy: it lets
    a long document be cleared in passes without an all-or-nothing decision.
    """

    PROCESS_REVIEWED = "reviewed"
    PROCESS_ALL = "all"
    KEEP_REVIEWING = "review"
    CANCEL = "cancel"

    def __init__(self, parent, reviewed: int = 0, unreviewed: int = 0):
        super().__init__(parent)
        self.setWindowTitle("Some items are still unreviewed")
        self.setMinimumWidth(520)
        self.choice = self.CANCEL

        layout = QVBoxLayout(self)
        summary = QLabel(
            f"{reviewed} item(s) reviewed, {unreviewed} not yet reviewed."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        options = [
            ("Redact everything", self.PROCESS_ALL,
             "Apply every detection, reviewed or not."),
            ("Process reviewed items only", self.PROCESS_REVIEWED,
             "Unreviewed items are left in the document untouched."),
            ("Keep reviewing", self.KEEP_REVIEWING,
             "Return to the review list without writing anything."),
        ]
        for label, value, detail in options:
            button = QPushButton(label)
            if value == self.PROCESS_ALL:
                button.setObjectName("Primary")
            button.clicked.connect(lambda _checked=False, v=value: self._choose(v))
            layout.addWidget(button)
            hint = QLabel(detail)
            hint.setObjectName("CardMeta")
            hint.setWordWrap(True)
            layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _choose(self, value: str) -> None:
        self.choice = value
        self.accept()



class FindResultsDialog(QDialog):
    """Every occurrence of what the user searched for, and what covers each one.

    Read-only: it looks, it does not redact. The point is to spot what the detectors
    MISSED - a hit marked "Not detected" is text that will stay in the output unless
    the user acts on it.
    """

    hit_chosen = Signal(int)
    redact_requested = Signal(bool)   # True = black out, False = pseudonymize
    closed = Signal()

    _LABELS = {"redacted": "Redacted", "kept": "Found, kept",
               "protected": "Form text, kept by design", "undetected": "NOT DETECTED"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find")
        self.setModal(False)
        self.resize(560, 420)
        layout = QVBoxLayout(self)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._row_changed)
        layout.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.prev_button = QPushButton("\u2039 Previous")
        self.next_button = QPushButton("Next \u203a")
        self.prev_button.clicked.connect(lambda: self.step(-1))
        self.next_button.clicked.connect(lambda: self.step(1))
        row.addWidget(self.prev_button)
        row.addWidget(self.next_button)
        row.addStretch(1)
        layout.addLayout(row)

        act = QHBoxLayout()
        self.pseudo_button = QPushButton("Pseudonymize the undetected")
        self.black_button = QPushButton("Black out the undetected")
        self.pseudo_button.clicked.connect(lambda: self.redact_requested.emit(False))
        self.black_button.clicked.connect(lambda: self.redact_requested.emit(True))
        act.addWidget(self.pseudo_button)
        act.addWidget(self.black_button)
        layout.addLayout(act)

    def show_results(self, needle: str, hits: list) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        counts = {"redacted": 0, "kept": 0, "protected": 0, "undetected": 0}
        for hit in hits:
            counts[hit.status] += 1
            label = self._LABELS[hit.status] + (f" ({hit.pii_type})" if hit.pii_type else "")
            item = QListWidgetItem(f"Page {hit.page_no + 1}   \u00b7   {label}   \u00b7   {hit.context}")
            if hit.status == "undetected":
                item.setForeground(QBrush(QColor(200, 90, 0)))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.list.addItem(item)
        self.list.blockSignals(False)
        if not hits:
            self.summary.setText(f"\u201c{needle}\u201d does not appear in this document\u2019s text. "
                                 "If a page is a scan, its text cannot be searched.")
        else:
            self.summary.setText(
                f"\u201c{needle}\u201d: {len(hits)} found \u2014 {counts['redacted']} redacted, "
                f"{counts['kept']} found but kept, {counts['protected']} form text/figures kept by design, "
                f"{counts['undetected']} NOT detected."
            )
        undetected = counts["undetected"] > 0
        self.pseudo_button.setEnabled(undetected)
        self.black_button.setEnabled(undetected)
        self.prev_button.setEnabled(len(hits) > 1)
        self.next_button.setEnabled(len(hits) > 1)
        self.setWindowTitle(f"Find \u2014 {len(hits)} found")
        if hits:
            self.list.setCurrentRow(0)

    def step(self, delta: int) -> None:
        count = self.list.count()
        if count:
            self.list.setCurrentRow((self.list.currentRow() + delta) % count)

    def _row_changed(self, row: int) -> None:
        if row >= 0:
            self.hit_chosen.emit(row)

    def done(self, result: int) -> None:
        self.closed.emit()
        super().done(result)

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)
