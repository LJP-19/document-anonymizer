"""Main window.

The review list is the primary surface. Selecting an item scrolls the document
to it and outlines it. Every detection is redacted by default; the user's job is
to spot the ones that should be kept, which is the safe direction for a tool
whose purpose is de-identifying documents before they leave the firm.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import shiboken6

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..decisions.manager import DecisionState, OccurrenceGroup
from ..session import AnonymizationSession, Status
from ..verification.verifier import format_report
from ..version import APP_NAME, __version__
from . import theme

REDACT = DecisionState.ACCEPTED
KEEP = DecisionState.SKIPPED


# --------------------------------------------------------------------------- #
# background work
# --------------------------------------------------------------------------- #


class Worker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.finished.emit(self._fn())
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class TaskRunner(QObject):
    """Owns one background thread at a time and never abandons a running one."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[Worker] = None

    def run(self, fn: Callable, on_done: Callable, on_error: Callable) -> None:
        self.stop()
        self._thread = QThread()
        self._worker = Worker(fn)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(on_done)
        self._worker.failed.connect(on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(5000)


# --------------------------------------------------------------------------- #
# small widgets
# --------------------------------------------------------------------------- #


class TypeBadge(QLabel):
    def __init__(self, pii_type: str):
        label = pii_type.replace("UNCLASSIFIED_GROUP_VALUE", "UNLABELLED").replace("_", " ")
        super().__init__(label)
        self.setObjectName("TypeBadge")
        color = theme.type_color(pii_type)
        self.setStyleSheet(f"color: {color}; background: {theme.rgba(color, 0.14)};")


class DetectionCard(QFrame):
    """One distinct value, with every occurrence of it folded together."""

    selected = Signal(object)
    decided = Signal(object, object)

    def __init__(self, group: OccurrenceGroup, state: DecisionState, flagged: bool):
        super().__init__()
        self.group = group
        self.setObjectName("Card")
        self.setProperty("selected", "false")
        self.setCursor(Qt.PointingHandCursor)
        self.setFrameShape(QFrame.NoFrame)
        self.setMinimumHeight(84)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(11, 9, 11, 9)
        outer.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        value = QLabel(self._elide(group.display))
        value.setObjectName("CardValue")
        value.setToolTip(group.display)
        top.addWidget(value, 1)
        top.addWidget(TypeBadge(group.pii_type.value))
        outer.addLayout(top)

        meta = QLabel(self._meta_text(group, state))
        meta.setObjectName("CardMeta")
        outer.addWidget(meta)

        if flagged:
            reason = next((c.review_reason for c in group.candidates if c.review_reason), "")
            if reason:
                note = QLabel(reason)
                note.setObjectName("CardReason")
                note.setWordWrap(True)
                outer.addWidget(note)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.redact_button = QPushButton("Redact")
        self.keep_button = QPushButton("Keep")
        self.edit_button = QPushButton("Edit\u2026")
        for button in (self.redact_button, self.keep_button, self.edit_button):
            button.setObjectName("Ghost")
        self.redact_button.clicked.connect(lambda: self.decided.emit(self.group, REDACT))
        self.keep_button.clicked.connect(lambda: self.decided.emit(self.group, KEEP))
        self.edit_button.clicked.connect(
            lambda: self.decided.emit(self.group, DecisionState.EDITED)
        )
        actions.addWidget(self.redact_button)
        actions.addWidget(self.keep_button)
        actions.addWidget(self.edit_button)
        actions.addStretch(1)
        outer.addLayout(actions)

        self._apply_state(state)

    @staticmethod
    def _elide(text: str, limit: int = 38) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 1] + "\u2026"

    @staticmethod
    def _meta_text(group: OccurrenceGroup, state: DecisionState) -> str:
        where = f"{group.count} occurrence" + ("s" if group.count != 1 else "")
        pages = sorted({c.page_no + 1 for c in group.candidates})
        page_text = "page " + ", ".join(str(p) for p in pages[:3])
        if len(pages) > 3:
            page_text += f" +{len(pages) - 3}"
        if state is DecisionState.EDITED:
            verdict = "custom replacement"
        elif state is KEEP:
            verdict = "will be kept as-is"
        else:
            verdict = "will be redacted"
        return f"{where} \u00b7 {page_text} \u00b7 {verdict}"

    def _apply_state(self, state: DecisionState) -> None:
        keeping = state is KEEP
        self.redact_button.setEnabled(keeping)
        self.keep_button.setEnabled(not keeping)
        self.setStyleSheet(
            "" if not keeping else f"#Card {{ border-left: 3px solid {theme.TEXT_FAINT}; }}"
        )

    def set_selected(self, value: bool) -> None:
        self.setProperty("selected", "true" if value else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event):  # noqa: N802 - Qt naming
        self.selected.emit(self.group)
        super().mousePressEvent(event)


class PageCanvas(QScrollArea):
    """Renders a page and draws detection outlines on top of it."""

    def __init__(self):
        super().__init__()
        self.setObjectName("Canvas")
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.setWidget(self.label)
        self.setWidgetResizable(True)
        self._base: Optional[QPixmap] = None
        self._overlays: list = []
        self._zoom = 1.6

    def set_page(self, png: bytes, zoom: float) -> None:
        # A render can land after the window has started closing; the worker
        # thread has no way to know its target widget is gone.
        if not shiboken6.isValid(self) or not shiboken6.isValid(self.label):
            return
        self._zoom = zoom
        self._base = QPixmap.fromImage(QImage.fromData(png, "PNG"))
        self._repaint()

    def set_overlays(self, overlays) -> None:
        self._overlays = list(overlays)
        self._repaint()

    def clear(self, message: str) -> None:
        self._base = None
        self._overlays = []
        self.label.setPixmap(QPixmap())
        self.label.setObjectName("EmptyState")
        self.label.setText(message)

    def _repaint(self) -> None:
        if self._base is None:
            return
        canvas = QPixmap(self._base)
        if self._overlays:
            painter = QPainter(canvas)
            painter.setRenderHint(QPainter.Antialiasing)
            for rect, kind, focused in self._overlays:
                fill, edge = {
                    "redact": (theme.HL_REDACT, theme.HL_REDACT_EDGE),
                    "review": (theme.HL_REVIEW, theme.HL_REVIEW_EDGE),
                    "keep": (theme.HL_KEEP, theme.HL_KEEP_EDGE),
                }[kind]
                x0, y0, x1, y1 = (v * self._zoom for v in rect)
                painter.setBrush(QColor(*fill))
                pen = QPen(QColor(*(theme.HL_FOCUS_EDGE if focused else edge)))
                pen.setWidth(2 if focused else 1)
                painter.setPen(pen)
                painter.drawRoundedRect(
                    int(x0) - 2, int(y0) - 2, int(x1 - x0) + 4, int(y1 - y0) + 4, 3, 3
                )
            painter.end()
        self.label.setText("")
        self.label.setPixmap(canvas)

    def scroll_to(self, rect, zoom: float) -> None:
        _x0, y0, _x1, y1 = rect
        centre = (y0 + y1) / 2 * zoom
        bar = self.verticalScrollBar()
        bar.setValue(max(0, int(centre - self.viewport().height() / 2)))


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #


class MainWindow(QMainWindow):
    VIEW_ORIGINAL, VIEW_REDACTED, VIEW_SPLIT = "original", "redacted", "split"

    def __init__(self):
        super().__init__()
        self.session: Optional[AnonymizationSession] = None
        self.page_no = 0
        self.zoom = 1.6
        self.view_mode = self.VIEW_SPLIT
        self.filter_mode = "flagged"
        self.search_text = ""
        self.selected_key: Optional[tuple] = None
        self.cards: list[DetectionCard] = []
        self._placeholder: Optional[QLabel] = None

        self.analysis_task = TaskRunner(self)
        self.preview_task = TaskRunner(self)
        self.export_task = TaskRunner(self)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(140)  # debounce so clicking stays responsive
        self._preview_timer.timeout.connect(self._refresh_preview)

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1580, 980)
        self.setStyleSheet(theme.STYLESHEET)
        self._build()
        self._install_shortcuts()
        self._set_status(Status.IDLE, "Open a PDF to begin")
        self.left_canvas.clear("Open a PDF to see it here")
        self.right_canvas.clear("")

    # -- layout ------------------------------------------------------------

    def _build(self) -> None:
        root = QWidget()
        column = QVBoxLayout(root)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self._header())

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_layout.addWidget(self._sidebar())
        body_layout.addWidget(self._viewer(), 1)
        column.addWidget(body, 1)
        column.addWidget(self._footer())
        self.setCentralWidget(root)

    def _header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("Header")
        header.setFixedHeight(62)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.setSpacing(12)

        open_button = QPushButton("Open PDF")
        open_button.clicked.connect(self.open_file)
        layout.addWidget(open_button)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.title_label = QLabel("No document")
        self.title_label.setObjectName("DocTitle")
        self.subtitle_label = QLabel("Everything runs locally on this machine")
        self.subtitle_label.setObjectName("DocSubtitle")
        titles.addWidget(self.title_label)
        titles.addWidget(self.subtitle_label)
        layout.addLayout(titles, 1)

        self.status_pill = QLabel(Status.IDLE)
        self.status_pill.setObjectName("StatusPill")
        layout.addWidget(self.status_pill)

        self.export_button = QPushButton("Export && Verify")
        self.export_button.setObjectName("Primary")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export)
        layout.addWidget(self.export_button)
        return header

    def _sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(392)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(10)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search detected values\u2026")
        self.search_box.textChanged.connect(self._on_search)
        layout.addWidget(self.search_box)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.chip_group = QButtonGroup(self)
        self.chip_group.setExclusive(True)
        self.chips: dict[str, QPushButton] = {}
        for key, label in (("flagged", "Review"), ("all", "All"), ("kept", "Kept")):
            chip = QPushButton(label)
            chip.setObjectName("Chip")
            chip.setCheckable(True)
            chip.clicked.connect(lambda _checked=False, k=key: self._set_filter(k))
            self.chip_group.addButton(chip)
            self.chips[key] = chip
            chips.addWidget(chip)
        chips.addStretch(1)
        self.chips["flagged"].setChecked(True)
        layout.addLayout(chips)

        self.list_area = QScrollArea()
        self.list_area.setWidgetResizable(True)
        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        self.list_area.setWidget(self.list_host)
        layout.addWidget(self.list_area, 1)

        bulk = QHBoxLayout()
        bulk.setSpacing(6)
        redact_all = QPushButton("Redact everything")
        redact_all.clicked.connect(self.redact_all)
        undo = QPushButton("Undo")
        undo.clicked.connect(self.undo)
        bulk.addWidget(redact_all, 1)
        bulk.addWidget(undo)
        layout.addLayout(bulk)
        return side

    def _viewer(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QWidget()
        toolbar.setObjectName("Toolbar")
        toolbar.setFixedHeight(48)
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(14, 8, 14, 8)
        bar.setSpacing(6)

        self.view_buttons: dict[str, QPushButton] = {}
        view_group = QButtonGroup(self)
        view_group.setExclusive(True)
        for key, label in (
            (self.VIEW_ORIGINAL, "Original"),
            (self.VIEW_REDACTED, "Redacted"),
            (self.VIEW_SPLIT, "Side by side"),
        ):
            button = QPushButton(label)
            button.setObjectName("Chip")
            button.setCheckable(True)
            button.clicked.connect(lambda _c=False, k=key: self._set_view(k))
            view_group.addButton(button)
            self.view_buttons[key] = button
            bar.addWidget(button)
        self.view_buttons[self.VIEW_SPLIT].setChecked(True)
        bar.addSpacing(16)

        prev_button = QPushButton("\u2039")
        next_button = QPushButton("\u203a")
        prev_button.setObjectName("Ghost")
        next_button.setObjectName("Ghost")
        prev_button.clicked.connect(lambda: self._go_page(self.page_no - 1))
        next_button.clicked.connect(lambda: self._go_page(self.page_no + 1))
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.setFixedWidth(62)
        self.page_spin.valueChanged.connect(lambda v: self._go_page(v - 1))
        self.page_total = QLabel("of 0")
        self.page_total.setObjectName("CardMeta")
        bar.addWidget(prev_button)
        bar.addWidget(self.page_spin)
        bar.addWidget(self.page_total)
        bar.addWidget(next_button)

        bar.addStretch(1)
        zoom_label = QLabel("Zoom")
        zoom_label.setObjectName("CardMeta")
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setFixedWidth(130)
        self.zoom_slider.setRange(80, 320)
        self.zoom_slider.setValue(int(self.zoom * 100))
        self.zoom_slider.valueChanged.connect(self._on_zoom)
        bar.addWidget(zoom_label)
        bar.addWidget(self.zoom_slider)
        layout.addWidget(toolbar)

        canvases = QWidget()
        canvas_layout = QHBoxLayout(canvases)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.setSpacing(1)
        self.left_canvas = PageCanvas()
        self.right_canvas = PageCanvas()
        canvas_layout.addWidget(self.left_canvas)
        canvas_layout.addWidget(self.right_canvas)
        for a, b in ((self.left_canvas, self.right_canvas), (self.right_canvas, self.left_canvas)):
            a.verticalScrollBar().valueChanged.connect(b.verticalScrollBar().setValue)
        layout.addWidget(canvases, 1)
        return wrapper

    def _footer(self) -> QWidget:
        footer = QWidget()
        footer.setObjectName("Footer")
        footer.setFixedHeight(34)
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(16, 6, 16, 6)
        self.footer_label = QLabel("")
        self.footer_label.setObjectName("CardMeta")
        layout.addWidget(self.footer_label, 1)
        hint = QLabel("R redact \u00b7 K keep \u00b7 J next \u00b7 \u2190 \u2192 page")
        hint.setObjectName("CardMeta")
        layout.addWidget(hint)
        return footer

    def _install_shortcuts(self) -> None:
        for keys, slot in (
            ("Ctrl+O", self.open_file),
            ("Ctrl+E", self.export),
            ("Ctrl+Z", self.undo),
            ("R", lambda: self._decide_selected(REDACT)),
            ("K", lambda: self._decide_selected(KEEP)),
            ("J", lambda: self._step_selection(1)),
            ("Shift+J", lambda: self._step_selection(-1)),
            ("Left", lambda: self._go_page(self.page_no - 1)),
            ("Right", lambda: self._go_page(self.page_no + 1)),
        ):
            QShortcut(QKeySequence(keys), self, activated=slot)

    # -- status ------------------------------------------------------------

    def _set_status(self, status: str, detail: str = "") -> None:
        self.status_pill.setText(status)
        colour = {
            Status.NEEDS_REVIEW: theme.WARN,
            Status.VERIFIED: theme.OK,
            Status.READY: theme.OK,
            Status.VERIFICATION_FAILED: theme.DANGER,
            Status.EXPORT_FAILED: theme.DANGER,
            Status.OCR_REQUIRED: theme.WARN,
        }.get(status, theme.TEXT_DIM)
        self.status_pill.setStyleSheet(
            f"color: {colour};"
            f"background: {theme.rgba(colour, 0.12)};"
            f"border: 1px solid {theme.rgba(colour, 0.35)};"
        )
        if detail:
            self.footer_label.setText(detail)

    def _on_error(self, message: str) -> None:
        self._set_status(Status.EXPORT_FAILED, message)
        QMessageBox.critical(self, "Something went wrong", message)

    # -- document ----------------------------------------------------------

    def open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", "", "PDF files (*.pdf)")
        if path:
            self.load(path)

    def load(self, path: str) -> None:
        self.session = AnonymizationSession(source_path=path)
        self.page_no = 0
        self.selected_key = None
        self.title_label.setText(Path(path).name)
        self.subtitle_label.setText(str(Path(path).parent))
        self.export_button.setEnabled(False)
        self._clear_cards()
        self._set_status(Status.ANALYZING, "Reading the document\u2026")
        self.analysis_task.run(self.session.analyse, self._on_analyzed, self._on_error)

    def _on_analyzed(self, _result) -> None:
        assert self.session
        total = self.session.document.page_count
        self.page_spin.setMaximum(max(1, total))
        self.page_total.setText(f"of {total}")
        self.export_button.setEnabled(True)
        self._rebuild_cards()
        self._go_page(0)
        self._update_status_from_session()

    def _update_status_from_session(self) -> None:
        assert self.session
        flagged = len(self._flagged_groups())
        total = len(self.session.candidates)
        ocr = self.session.document.ocr_required_pages if self.session.document else []
        if ocr:
            status = Status.OCR_REQUIRED
            detail = (
                f"{total} detections \u00b7 page(s) "
                + ", ".join(str(p + 1) for p in ocr)
                + " have no readable text and were NOT analysed"
            )
        elif flagged:
            status = Status.NEEDS_REVIEW
            detail = f"{total} detections \u00b7 {flagged} worth a second look before exporting"
        else:
            status = Status.READY
            detail = f"{total} detections \u00b7 all will be redacted"
        self.session.status = status
        self._set_status(status, detail)

    # -- review list -------------------------------------------------------

    def _groups(self) -> list[OccurrenceGroup]:
        if not self.session or not self.session.detection:
            return []
        return self.session.decisions.occurrence_groups(self.session.candidates)

    def _state_of(self, group: OccurrenceGroup) -> DecisionState:
        return self.session.decisions.state(group.candidates[0])

    def _flagged_groups(self) -> list[OccurrenceGroup]:
        return [g for g in self._groups() if g.needs_review and self._state_of(g) is not KEEP]

    def _visible_groups(self) -> list[OccurrenceGroup]:
        groups = self._groups()
        if self.filter_mode == "flagged":
            groups = [g for g in groups if g.needs_review and self._state_of(g) is not KEEP]
        elif self.filter_mode == "kept":
            groups = [g for g in groups if self._state_of(g) is KEEP]
        if self.search_text:
            needle = self.search_text.lower()
            groups = [
                g
                for g in groups
                if needle in g.display.lower() or needle in g.pii_type.value.lower()
            ]
        return groups

    def _clear_cards(self) -> None:
        for card in self.cards:
            card.setParent(None)
            card.deleteLater()
        self.cards = []
        if self._placeholder is not None:
            self._placeholder.setParent(None)
            self._placeholder.deleteLater()
            self._placeholder = None

    def _rebuild_cards(self) -> None:
        self._clear_cards()
        groups = self._visible_groups()

        if not groups:
            message = {
                "flagged": "Nothing flagged. Everything detected will be redacted.",
                "kept": "Nothing is being kept \u2014 every detection will be redacted.",
                "all": "No detections. If that looks wrong, the page may need OCR.",
            }[self.filter_mode]
            placeholder = QLabel(message)
            placeholder.setObjectName("EmptyState")
            placeholder.setWordWrap(True)
            placeholder.setAlignment(Qt.AlignCenter)
            self.list_layout.insertWidget(0, placeholder)
            self._placeholder = placeholder
            self._update_chip_counts()
            return

        for group in groups:
            card = DetectionCard(group, self._state_of(group), group.needs_review)
            card.selected.connect(self._on_card_selected)
            card.decided.connect(self._on_card_decided)
            card.set_selected((group.pii_type, group.normalized) == self.selected_key)
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)
            self.cards.append(card)
        self._update_chip_counts()

    def _update_chip_counts(self) -> None:
        groups = self._groups()
        flagged = len(self._flagged_groups())
        kept = len([g for g in groups if self._state_of(g) is KEEP])
        self.chips["flagged"].setText(f"Review \u00b7 {flagged}")
        self.chips["all"].setText(f"All \u00b7 {len(groups)}")
        self.chips["kept"].setText(f"Kept \u00b7 {kept}")

    def _set_filter(self, mode: str) -> None:
        self.filter_mode = mode
        if not self.chips[mode].isChecked():
            self.chips[mode].setChecked(True)
        self._rebuild_cards()

    def _on_search(self, text: str) -> None:
        self.search_text = text.strip()
        self._rebuild_cards()

    # -- decisions ---------------------------------------------------------

    def _on_card_selected(self, group: OccurrenceGroup) -> None:
        self.selected_key = (group.pii_type, group.normalized)
        first = group.candidates[0]
        for card in self.cards:
            card.set_selected((card.group.pii_type, card.group.normalized) == self.selected_key)
        if first.page_no != self.page_no:
            self._go_page(first.page_no)
        else:
            self._draw_overlays()
        self.left_canvas.scroll_to(first.rect, self.zoom)

    def _on_card_decided(self, group: OccurrenceGroup, state: DecisionState) -> None:
        if state is DecisionState.EDITED:
            text, ok = QInputDialog.getText(
                self, "Custom replacement", f"Replace \u201c{group.display}\u201d with:"
            )
            if not (ok and text.strip()):
                return
            self.session.decisions.edit(group.candidates, text.strip())
        else:
            self.session.decisions.set_state(group.candidates, state)
        self._after_decision()

    def _decide_selected(self, state: DecisionState) -> None:
        if not self.selected_key:
            return
        group = next(
            (g for g in self._groups() if (g.pii_type, g.normalized) == self.selected_key), None
        )
        if group:
            self._on_card_decided(group, state)

    def _step_selection(self, delta: int) -> None:
        if not self.cards:
            return
        keys = [(c.group.pii_type, c.group.normalized) for c in self.cards]
        index = keys.index(self.selected_key) if self.selected_key in keys else -1
        index = max(0, min(len(keys) - 1, index + delta))
        self._on_card_selected(self.cards[index].group)
        self.list_area.ensureWidgetVisible(self.cards[index], 0, 40)

    def redact_all(self) -> None:
        if self.session:
            self.session.decisions.set_state(self.session.candidates, REDACT)
            self._after_decision()

    def undo(self) -> None:
        if self.session and self.session.decisions.undo():
            self._after_decision()

    def _after_decision(self) -> None:
        self._rebuild_cards()
        self._update_status_from_session()
        self._draw_overlays()
        self._preview_timer.start()

    # -- viewer ------------------------------------------------------------

    def _set_view(self, mode: str) -> None:
        self.view_mode = mode
        self._go_page(self.page_no)

    def _on_zoom(self, value: int) -> None:
        self.zoom = value / 100
        self._go_page(self.page_no)

    def _go_page(self, page_no: int) -> None:
        if not self.session or not self.session.document:
            return
        page_no = max(0, min(self.session.document.page_count - 1, page_no))
        self.page_no = page_no
        if self.page_spin.value() != page_no + 1:
            self.page_spin.blockSignals(True)
            self.page_spin.setValue(page_no + 1)
            self.page_spin.blockSignals(False)

        show_original = self.view_mode in (self.VIEW_ORIGINAL, self.VIEW_SPLIT)
        show_redacted = self.view_mode in (self.VIEW_REDACTED, self.VIEW_SPLIT)
        self.left_canvas.setVisible(show_original)
        self.right_canvas.setVisible(show_redacted)

        if show_original:
            self.left_canvas.set_page(self.session.preview_original(page_no, self.zoom), self.zoom)
            self._draw_overlays()
        if show_redacted:
            self._preview_timer.start()

    def _refresh_preview(self) -> None:
        if not self.session or self.view_mode == self.VIEW_ORIGINAL:
            return
        page_no, zoom = self.page_no, self.zoom
        self.preview_task.run(
            lambda: self.session.preview_transformed(page_no, zoom),
            lambda png: self.right_canvas.set_page(png, zoom),
            self._on_error,
        )

    def _draw_overlays(self) -> None:
        if not self.session or not self.session.detection:
            return
        overlays = []
        for candidate in self.session.candidates:
            if candidate.page_no != self.page_no:
                continue
            state = self.session.decisions.state(candidate)
            if state is KEEP:
                kind = "keep"
            elif candidate.needs_review:
                kind = "review"
            else:
                kind = "redact"
            focused = (
                self.selected_key is not None
                and (candidate.pii_type, candidate.normalized) == self.selected_key
            )
            overlays.append((candidate.rect, kind, focused))
        self.left_canvas.set_overlays(overlays)

    # -- export ------------------------------------------------------------

    def export(self) -> None:
        if not self.session or not self.session.detection:
            return
        flagged = len(self._flagged_groups())
        if flagged:
            answer = QMessageBox.question(
                self,
                "Export without reviewing?",
                f"{flagged} detection(s) are flagged for a second look. They will be "
                "redacted as-is.\n\nExport anyway?",
            )
            if answer is not QMessageBox.Yes:
                self.chips["flagged"].setChecked(True)
                self._set_filter("flagged")
                return

        suggested = str(Path(self.session.source_path).with_suffix("")) + ".anonymized.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save anonymized PDF", suggested, "PDF (*.pdf)"
        )
        if not path:
            return
        self._set_status(Status.PROCESSING, "Redacting and writing the file\u2026")
        self.export_button.setEnabled(False)
        self.export_task.run(
            lambda: self.session.process(path), self._on_exported, self._on_export_error
        )

    def _on_export_error(self, message: str) -> None:
        self.export_button.setEnabled(True)
        self._on_error(message)

    def _on_exported(self, result) -> None:
        _apply_report, report = result
        self.export_button.setEnabled(True)
        self._set_status(self.session.status, report.output_path)
        box = QMessageBox(self)
        box.setWindowTitle(report.status)
        box.setText(report.status)
        box.setInformativeText(
            "This confirms the redactions were applied to the saved file. It does not "
            "prove every piece of PII was found \u2014 read the output before sharing it."
        )
        box.setDetailedText(format_report(report))
        box.exec()

    # -- teardown ----------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        for runner in (self.analysis_task, self.preview_task, self.export_task):
            runner.stop()
        super().closeEvent(event)


def run() -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    window.show()
    return app.exec()
