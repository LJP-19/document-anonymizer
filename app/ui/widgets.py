"""Reusable review widgets: detection cards and the page canvas.

The review list is the primary surface. Selecting an item scrolls the document
to it and outlines it. Every detection is redacted by default; the user's job is
to spot the ones that should be kept, which is the safe direction for a tool
whose purpose is de-identifying documents before they leave the firm.
"""

from __future__ import annotations

from pathlib import Path
import logging
import weakref
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

log = logging.getLogger(__name__)

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
        except BaseException as exc:  # noqa: BLE001 - must never escape the thread
            # BaseException, not Exception: anything escaping a worker thread
            # takes the whole process down with no message, which is what made
            # the app appear to exit by itself during processing.
            import traceback

            detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            log.error("background task failed:\n%s", detail)
            try:
                self.failed.emit(f"{type(exc).__name__}: {exc}")
            except RuntimeError:
                pass


#: Every live runner, weakly held. Qt aborts the process if a QThread is
#: destroyed while running, so at shutdown they all have to be stopped - not
#: only the ones a window still knows about.
_RUNNERS: "weakref.WeakSet[TaskRunner]" = weakref.WeakSet()


def stop_all_runners() -> None:
    """Stop every background thread. Safe to call more than once."""
    for runner in list(_RUNNERS):
        try:
            if shiboken6.isValid(runner):
                # Shutdown is the one place a join is safe: there is no event
                # loop left for the worker to deadlock against.
                runner.stop()
        except (RuntimeError, ReferenceError):
            pass  # the underlying C++ object is already gone


class TaskRunner(QObject):
    """Owns one background thread at a time and never abandons a running one."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[Worker] = None
        _RUNNERS.add(self)

    def run(self, fn: Callable, on_done: Callable, on_error: Callable) -> None:
        self.stop()
        self._thread = QThread()
        # Let Qt own the thread object's lifetime. Deleting it ourselves after
        # it has finished is what turned a shutdown warning into a segfault.
        self._thread.finished.connect(self._thread.deleteLater)
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
        """Detach from the thread and join it if it is still running.

        This is the least-bad of three behaviours measured. Never joining leaves
        a live QThread at exit and Qt aborts every time. Joining unconditionally
        dereferences a half-destroyed object and segfaults every time. Parking
        threads to join at shutdown segfaults every time. Guarding on validity
        and isRunning fails roughly 4 runs in 10, always during teardown after
        the assertions have passed - see README "Known defects".
        """
        thread, worker = self._thread, self._worker
        self._thread = None
        self._worker = None

        # Do NOT disconnect the worker's signals here. `finished` is also wired
        # to the thread's quit(); dropping every slot meant the thread was never
        # told to stop, so it was alive at exit and Qt aborted every single run.

        if thread is None or not shiboken6.isValid(thread):
            return
        try:
            if thread.isRunning():
                thread.quit()
                thread.wait(5000)
        except RuntimeError:
            pass  # the C++ object went away underneath us


class ElidingLabel(QLabel):
    """Shortens its text to the width it is actually given.

    Truncating at a fixed character count clipped everything on a narrow panel
    and wasted space on a wide one.
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        self.setMinimumWidth(40)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._full = text
        super().setText(self._shortened())

    def _shortened(self) -> str:
        metrics = self.fontMetrics()
        return metrics.elidedText(self._full, Qt.ElideRight, max(self.width() - 4, 40))

    def resizeEvent(self, event):  # noqa: N802 - Qt naming
        super().setText(self._shortened())
        super().resizeEvent(event)


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
        # A tick shown once the item has been decided. Pressing Redact or Keep
        # IS the review - making the user also tick a box was busywork.
        self.reviewed_mark = QLabel("\u2713" if reviewed else "")
        self.reviewed_mark.setFixedWidth(14)
        self.reviewed_mark.setStyleSheet(f"color: {theme.OK}; font-weight: 700;")
        top.addWidget(self.reviewed_mark)
        value = ElidingLabel(group.display)
        value.setObjectName("CardValue")
        value.setToolTip(group.display)
        top.addWidget(value, 1)
        top.addWidget(TypeBadge(group.pii_type.value))
        outer.addLayout(top)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        meta = ElidingLabel(self._meta_text(group, state))
        meta.setObjectName("CardMeta")
        meta.setToolTip(self._meta_text(group, state))
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
        self.page_list.setMaximumHeight(150)
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
        """Show which action is current. Never disable either button.

        Disabling Redact whenever an item was already set to redact - which is
        the default for everything - made the button permanently unclickable.
        """
        keeping = state is KEEP
        active = f"color: {theme.ACCENT}; font-weight: 700;"
        self.redact_button.setStyleSheet("" if keeping else active)
        self.keep_button.setStyleSheet(active if keeping else "")
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
    """One page, with detection outlines painted over it.

    In box mode a drag draws a blackout area. Coordinates are converted from
    the widget back to PDF space using the same zoom the page was rendered at,
    so the rectangle the user sees is the rectangle that gets covered.
    """

    region_drawn = Signal(int, tuple)

    def __init__(self, page_no: int):
        super().__init__()
        self.page_no = page_no
        self.box_mode = False
        self._drag_origin = None
        self._drag_current = None
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

    def set_box_mode(self, enabled: bool) -> None:
        self.box_mode = enabled
        self.setCursor(Qt.CrossCursor if enabled else Qt.ArrowCursor)

    def _widget_to_pdf(self, point) -> tuple[float, float]:
        pixmap = self.pixmap()
        offset_x = max(0, (self.width() - pixmap.width()) // 2) if pixmap else 0
        offset_y = max(0, (self.height() - pixmap.height()) // 2) if pixmap else 0
        return ((point.x() - offset_x) / self._zoom, (point.y() - offset_y) / self._zoom)

    def mousePressEvent(self, event):  # noqa: N802 - Qt naming
        if self.box_mode and event.button() == Qt.LeftButton:
            self._drag_origin = event.position().toPoint()
            self._drag_current = self._drag_origin
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt naming
        if self.box_mode and self._drag_origin is not None:
            self._drag_current = event.position().toPoint()
            self.repaint_overlays()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt naming
        if self.box_mode and self._drag_origin is not None:
            start = self._widget_to_pdf(self._drag_origin)
            end = self._widget_to_pdf(event.position().toPoint())
            self._drag_origin = self._drag_current = None
            rect = (
                min(start[0], end[0]), min(start[1], end[1]),
                max(start[0], end[0]), max(start[1], end[1]),
            )
            self.repaint_overlays()
            if rect[2] - rect[0] >= 2 and rect[3] - rect[1] >= 2:
                self.region_drawn.emit(self.page_no, rect)
            return
        super().mouseReleaseEvent(event)

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
        if self._drag_origin is not None and self._drag_current is not None:
            painter = QPainter(canvas)
            pen = QPen(QColor(0, 0, 0, 220))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(QColor(0, 0, 0, 90))
            x0 = min(self._drag_origin.x(), self._drag_current.x())
            y0 = min(self._drag_origin.y(), self._drag_current.y())
            painter.drawRect(
                x0, y0,
                abs(self._drag_current.x() - self._drag_origin.x()),
                abs(self._drag_current.y() - self._drag_origin.y()),
            )
            painter.end()
        self.setText("")
        self.setPixmap(canvas)


class DocumentView(QScrollArea):
    """All pages in one continuous scroller, not one page at a time."""

    region_drawn = Signal(int, tuple)

    def __init__(self, title: str):
        super().__init__()
        self.box_mode = False
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
            header = getattr(view, "header", None)
            if header is not None:
                header.setParent(None)
                header.deleteLater()
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
            view.set_box_mode(self.box_mode)
            view.region_drawn.connect(self.region_drawn)
            self.views[page_no] = view
            view.header = header

    def set_box_mode(self, enabled: bool) -> None:
        self.box_mode = enabled
        for view in self.views.values():
            view.set_box_mode(enabled)

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
        # Force layout: a page whose image has not arrived yet reports 0, which
        # is why clicking a page in the list appeared to do nothing.
        self.host.updateGeometry()
        self.host.adjustSize()
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
