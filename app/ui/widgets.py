"""Reusable review widgets: detection cards and the page canvas.

The review list is the primary surface. Selecting an item scrolls the document
to it and outlines it. Every detection is redacted by default; the user's job is
to spot the ones that should be kept, which is the safe direction for a tool
whose purpose is de-identifying documents before they leave the firm.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import shiboken6

from PySide6.QtCore import QObject, QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QCheckBox,
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
    reviewed_changed = Signal(object, bool)
    page_clicked = Signal(object, int)

    def __init__(self, group: OccurrenceGroup, state: DecisionState, flagged: bool,
                 reviewed: bool = False):
        super().__init__()
        self.group = group
        self.expanded = False
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
        self.reviewed_box = QCheckBox()
        self.reviewed_box.setChecked(reviewed)
        self.reviewed_box.setToolTip("Mark as reviewed - moves this to the Reviewed tab")
        self.reviewed_box.toggled.connect(
            lambda value: self.reviewed_changed.emit(self.group, value)
        )
        top.addWidget(self.reviewed_box)
        value = QLabel(self._elide(group.display))
        value.setObjectName("CardValue")
        value.setToolTip(group.display)
        top.addWidget(value, 1)
        top.addWidget(TypeBadge(group.pii_type.value))
        outer.addLayout(top)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        meta = QLabel(self._meta_text(group, state))
        meta.setObjectName("CardMeta")
        meta_row.addWidget(meta, 1)
        self.expander = QPushButton(self._expander_text())
        self.expander.setObjectName("Ghost")
        self.expander.clicked.connect(self._toggle_pages)
        if len(self._pages()) > 1:
            meta_row.addWidget(self.expander)
        outer.addLayout(meta_row)

        self.page_list = QWidget()
        page_column = QVBoxLayout(self.page_list)
        page_column.setContentsMargins(24, 2, 0, 2)
        page_column.setSpacing(2)
        for page_no in self._pages():
            hits = sum(1 for c in group.candidates if c.page_no == page_no)
            link = QPushButton(f"Page {page_no + 1}  ({hits})")
            link.setObjectName("Ghost")
            link.setCursor(Qt.PointingHandCursor)
            link.clicked.connect(lambda _c=False, p=page_no: self.page_clicked.emit(self.group, p))
            page_column.addWidget(link, alignment=Qt.AlignLeft)
        self.page_list.setVisible(False)
        outer.addWidget(self.page_list)

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

    def _pages(self) -> list[int]:
        return sorted({c.page_no for c in self.group.candidates})

    def _expander_text(self) -> str:
        return ("\u25be  " if self.expanded else "\u25b8  ") + f"{len(self._pages())} pages"

    def _toggle_pages(self) -> None:
        self.expanded = not self.expanded
        self.page_list.setVisible(self.expanded)
        self.expander.setText(self._expander_text())

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


class PageView(QLabel):
    """One page, with detection outlines painted over it."""

    def __init__(self, page_no: int):
        super().__init__()
        self.page_no = page_no
        self.setAlignment(Qt.AlignCenter)
        self._base: Optional[QPixmap] = None
        self._overlays: list = []
        self._zoom = 1.0
        self.setText(f"page {page_no + 1}")

    def set_png(self, png: bytes, zoom: float) -> None:
        if not shiboken6.isValid(self):
            return
        self._zoom = zoom
        self._base = QPixmap.fromImage(QImage.fromData(png, "PNG"))
        self.repaint_overlays()

    def set_overlays(self, overlays) -> None:
        self._overlays = list(overlays)
        self.repaint_overlays()

    def repaint_overlays(self) -> None:
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
        self.setText("")
        self.setPixmap(canvas)


class DocumentView(QScrollArea):
    """All pages in one continuous scroller, not one page at a time."""

    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("Canvas")
        self.title = title
        self.host = QWidget()
        self.column = QVBoxLayout(self.host)
        self.column.setContentsMargins(16, 16, 16, 16)
        self.column.setSpacing(18)
        self.column.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.setWidget(self.host)
        self.setWidgetResizable(True)
        self.views: dict[int, PageView] = {}
        self._empty = QLabel(f"({title})")
        self._empty.setObjectName("EmptyState")
        self._empty.setAlignment(Qt.AlignCenter)
        self.column.addWidget(self._empty)

    def build(self, pages: list[int]) -> None:
        """Show exactly these page numbers, in order."""
        for view in self.views.values():
            view.setParent(None)
            view.deleteLater()
        self.views = {}
        self._empty.setVisible(not pages)
        for page_no in pages:
            header = QLabel(f"Page {page_no + 1}")
            header.setObjectName("SectionLabel")
            header.setAlignment(Qt.AlignLeft)
            view = PageView(page_no)
            self.column.addWidget(header)
            self.column.addWidget(view)
            self.views[page_no] = view
            view.header = header

    def clear(self, message: str) -> None:
        self.build([])
        self._empty.setText(message)
        self._empty.setVisible(True)

    def set_png(self, page_no: int, png: bytes, zoom: float) -> None:
        view = self.views.get(page_no)
        if view is not None:
            view.set_png(png, zoom)

    def set_overlays(self, page_no: int, overlays) -> None:
        view = self.views.get(page_no)
        if view is not None:
            view.set_overlays(overlays)

    def scroll_to_page(self, page_no: int, rect=None, zoom: float = 1.0) -> None:
        view = self.views.get(page_no)
        if view is None:
            return
        offset = view.mapTo(self.host, QPoint(0, 0)).y()
        if rect is not None:
            offset += int((rect[1] + rect[3]) / 2 * zoom) - self.viewport().height() // 3
        else:
            offset -= 12
        self.verticalScrollBar().setValue(max(0, offset))


class PageCanvas(QScrollArea):
    """Single-page canvas, retained for the headless smoke tests."""

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
