"""Main window (spec sections 27-32, 88-90).

The right-hand pane renders the actual transformed PDF produced by the same
`apply_plan` the export uses, so what is previewed is what is written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QAction, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..decisions.manager import DecisionState, OccurrenceGroup
from ..detection.types import Candidate
from ..session import AnonymizationSession, Status
from ..verification.verifier import format_report
from ..version import APP_NAME, __version__

ROLE_CANDIDATES = Qt.UserRole + 1


class Worker(QObject):
    """Runs a callable off the GUI thread (spec section 89)."""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn, self._args, self._kwargs = fn, args, kwargs

    def run(self):
        try:
            self.finished.emit(self._fn(*self._args, **self._kwargs))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class PagePane(QScrollArea):
    def __init__(self, title: str):
        super().__init__()
        self.label = QLabel(f"({title})")
        self.label.setAlignment(Qt.AlignCenter)
        self.setWidget(self.label)
        self.setWidgetResizable(True)

    def show_png(self, data: bytes) -> None:
        image = QImage.fromData(data, "PNG")
        self.label.setPixmap(QPixmap.fromImage(image))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.session: Optional[AnonymizationSession] = None
        self.page_no = 0
        self.zoom = 1.5
        self._thread: Optional[QThread] = None
        self._worker: Optional[Worker] = None

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1500, 950)
        self._build_ui()
        self._set_status(Status.IDLE)

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        bar = QToolBar()
        bar.setMovable(False)
        self.addToolBar(bar)
        for text, slot in (
            ("Open PDF", self.open_file),
            ("Analyze", self.analyze),
            ("Process && Verify", self.process),
        ):
            action = QAction(text, self)
            action.triggered.connect(slot)
            bar.addAction(action)
        bar.addSeparator()
        bar.addWidget(QLabel(" Page "))
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.valueChanged.connect(self._on_page_change)
        bar.addWidget(self.page_spin)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._review_panel())

        panes = QWidget()
        layout = QHBoxLayout(panes)
        layout.setContentsMargins(0, 0, 0, 0)
        self.left_pane = PagePane("original")
        self.right_pane = PagePane("anonymized preview")
        layout.addWidget(self.left_pane)
        layout.addWidget(self.right_pane)
        # Synchronised scrolling (spec section 31).
        for a, b in ((self.left_pane, self.right_pane), (self.right_pane, self.left_pane)):
            a.verticalScrollBar().valueChanged.connect(b.verticalScrollBar().setValue)
            a.horizontalScrollBar().valueChanged.connect(b.horizontalScrollBar().setValue)
        splitter.addWidget(panes)
        splitter.setSizes([430, 1070])
        self.setCentralWidget(splitter)

        self.setStatusBar(QStatusBar())

    def _review_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Detected value", "Type", "Count"])
        self.tree.setColumnWidth(0, 190)
        layout.addWidget(self.tree, 3)

        for text, slot in (
            ("Accept", self.accept_selected),
            ("Skip", self.skip_selected),
            ("Edit replacement...", self.edit_selected),
            ("Accept all remaining", self.accept_all),
            ("Undo", self.undo),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            layout.addWidget(button)

        self.notes = QTextEdit()
        self.notes.setReadOnly(True)
        layout.addWidget(self.notes, 2)
        return panel

    # -- status ------------------------------------------------------------

    def _set_status(self, status: str, extra: str = "") -> None:
        self.statusBar().showMessage(f"  {status}{('   -   ' + extra) if extra else ''}")

    def _busy(self, label: str, fn, on_done) -> None:
        self._set_status(label)
        # Never abandon a running thread: replacing self._thread while the old
        # one is alive lets Qt destroy a running QThread.
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(5000)
        self._thread = QThread()
        self._worker = Worker(fn)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(on_done)
        self._worker.failed.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(5000)
        super().closeEvent(event)

    def _on_error(self, message: str) -> None:
        self._set_status(Status.EXPORT_FAILED, message)
        QMessageBox.critical(self, "Error", message)

    # -- actions -----------------------------------------------------------

    def open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", "", "PDF files (*.pdf)")
        if not path:
            return
        self.session = AnonymizationSession(source_path=path)
        self.page_no = 0
        self.tree.clear()
        self.notes.clear()
        self.setWindowTitle(f"{APP_NAME} {__version__} - {Path(path).name}")
        self._set_status(Status.IDLE, path)
        self.analyze()

    def analyze(self) -> None:
        if not self.session:
            return
        self._busy(Status.ANALYZING, self.session.analyse, self._on_analyzed)

    def _on_analyzed(self, _result) -> None:
        assert self.session
        self.page_spin.setMaximum(max(1, self.session.document.page_count))
        self._populate_tree()
        self._render()
        self._set_status(self.session.status, self._summary())

    def _summary(self) -> str:
        assert self.session
        n = len(self.session.candidates)
        pending = len(self.session.needs_review())
        return f"{n} detection(s), {pending} awaiting review"

    def _populate_tree(self) -> None:
        assert self.session
        self.tree.clear()
        pending = QTreeWidgetItem(["NEEDS REVIEW"])
        done = QTreeWidgetItem(["REVIEWED"])
        self.tree.addTopLevelItem(pending)
        self.tree.addTopLevelItem(done)

        for group in self.session.decisions.occurrence_groups(self.session.candidates):
            unresolved = any(
                self.session.decisions.state(c) is DecisionState.UNREVIEWED
                for c in group.candidates
            )
            item = QTreeWidgetItem(
                [group.display[:44], group.pii_type.value, str(group.count)]
            )
            item.setData(0, ROLE_CANDIDATES, group.candidates)
            (pending if (unresolved or group.needs_review) else done).addChild(item)

        pending.setExpanded(True)
        done.setExpanded(False)
        self._write_notes()

    def _write_notes(self) -> None:
        assert self.session
        lines = []
        for group in self.session.detection.groups:
            flag = "COMPLETE" if group.complete else "PARTIAL"
            lines.append(f"[{flag}] p{group.page_no + 1}  {group.describe()}")
        if self.session.detection.warnings:
            lines.append("")
            lines.extend(f"! {w}" for w in self.session.detection.warnings)
        self.notes.setPlainText("\n".join(lines))

    def _selected(self) -> list[Candidate]:
        out: list[Candidate] = []
        for item in self.tree.selectedItems():
            data = item.data(0, ROLE_CANDIDATES)
            if data:
                out.extend(data)
        return out

    def _after_decision(self) -> None:
        assert self.session
        self._populate_tree()
        self._render()
        self.session.status = self.session._derive_status()
        self._set_status(self.session.status, self._summary())

    def accept_selected(self) -> None:
        if self.session and (sel := self._selected()):
            self.session.decisions.accept(sel)
            self._after_decision()

    def skip_selected(self) -> None:
        if self.session and (sel := self._selected()):
            self.session.decisions.skip(sel)
            self._after_decision()

    def edit_selected(self) -> None:
        sel = self._selected()
        if not (self.session and sel):
            return
        text, ok = QInputDialog.getText(self, "Edit replacement", "Replacement text:")
        if ok and text.strip():
            self.session.decisions.edit(sel, text.strip())
            self._after_decision()

    def accept_all(self) -> None:
        if not self.session:
            return
        self.session.decisions.accept(self.session.needs_review())
        self._after_decision()

    def undo(self) -> None:
        if self.session and self.session.decisions.undo():
            self._after_decision()

    def _on_page_change(self, value: int) -> None:
        self.page_no = value - 1
        self._render()

    def _render(self) -> None:
        if not self.session or not self.session.detection:
            return
        self.left_pane.show_png(self.session.preview_original(self.page_no, self.zoom))
        self._busy(
            self.session.status,
            lambda: self.session.preview_transformed(self.page_no, self.zoom),
            self.right_pane.show_png,
        )

    def process(self) -> None:
        if not self.session or not self.session.detection:
            return
        if self.session.needs_review():
            answer = QMessageBox.question(
                self,
                "Items still need review",
                f"{len(self.session.needs_review())} detection(s) are unreviewed and "
                "will be LEFT IN the document. Continue?",
            )
            if answer is not QMessageBox.Yes:
                return
        suggested = str(Path(self.session.source_path).with_suffix("")) + ".anonymized.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "Save anonymized PDF", suggested, "PDF (*.pdf)")
        if not path:
            return
        self._busy(Status.PROCESSING, lambda: self.session.process(path), self._on_processed)

    def _on_processed(self, result) -> None:
        assert self.session
        _apply_report, report = result
        self._set_status(self.session.status, report.output_path)
        self.notes.setPlainText(format_report(report))
        box = QMessageBox(self)
        box.setWindowTitle(report.status)
        box.setText(report.status)
        box.setDetailedText(format_report(report))
        box.exec()


def run() -> int:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    return app.exec()
