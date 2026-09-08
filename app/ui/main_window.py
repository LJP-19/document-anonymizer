"""Main window: a tabbed batch workspace.

    add files or a folder
        -> one tab per document
        -> review each, then Approve or Dismiss it
        -> approved documents appear in the queue
        -> Process writes them to the output folder
        -> a summary shows what changed, beside the redacted preview

Nothing is written until Process is pressed, and Start over never touches files
already written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import weakref

import logging

import shiboken6

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QDesktopServices, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..batch import Batch, BatchItem, BatchProgress, ItemState
from ..decisions.manager import DecisionState, OccurrenceGroup
from ..session import Status
from ..version import APP_NAME, __version__
from . import theme
from .dialogs import AddPiiDialog, EditDetectionDialog, UnreviewedPrompt
from .widgets import KEEP, REDACT, DetectionCard, DocumentView, TaskRunner, stop_all_runners

log = logging.getLogger(__name__)


def _build_summary() -> str:
    """Version and active layers, on screen at all times.

    Several rounds of bug reports turned out to be an older binary. The running
    version must never be something the user has to go looking for.
    """
    from ..detection.auditor import LlmAuditor
    from ..detection.gliner import GlinerDetector

    layers = ["rules", "layout"]
    try:
        if GlinerDetector().available:
            layers.append("GLiNER")
    except Exception:  # noqa: BLE001
        pass
    try:
        if LlmAuditor().available:
            layers.append("LLM review")
    except Exception:  # noqa: BLE001
        pass
    return f"v{__version__} \u00b7 {' + '.join(layers)} \u00b7 runs entirely on this machine"

ICON_PATH = Path(__file__).resolve().parents[2] / "resources" / "icons" / "app.svg"


_ANIMATIONS: "weakref.WeakSet[QPropertyAnimation]" = weakref.WeakSet()


def stop_all_animations() -> None:
    """Stop every fade. An animation driving a destroyed widget's effect is a
    use-after-free, which shows up as an intermittent bus error."""
    for animation in list(_ANIMATIONS):
        try:
            if shiboken6.isValid(animation):
                animation.stop()
        except (RuntimeError, ReferenceError):
            pass


def fade_in(widget: QWidget, duration: int = 180) -> None:
    """A short fade so panels appear rather than snap into place.

    A fade already running is stopped and cleared first. Restarting one on top
    of another can leave the opacity effect attached at a partial value, which
    shows up as a permanently dimmed panel.
    """
    running = getattr(widget, "_fade", None)
    if running is not None:
        running.stop()
        widget.setGraphicsEffect(None)
        widget._fade = None

    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(duration)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setEasingCurve(QEasingCurve.OutCubic)
    def _clear() -> None:
        widget.setGraphicsEffect(None)
        widget._fade = None

    animation.finished.connect(_clear)
    _ANIMATIONS.add(animation)
    animation.start(QPropertyAnimation.DeleteWhenStopped)
    widget._fade = animation  # hold a reference so it is not collected mid-run


class DocumentTab(QWidget):
    """Review surface for a single document."""

    changed = Signal()

    def __init__(self, item: BatchItem):
        super().__init__()
        self.item = item
        self.zoom = 1.0
        self.view_mode = "split"
        self.filter_mode = "flagged"
        self.search_text = ""
        self.selected_key: Optional[tuple] = None
        self.cards: list[DetectionCard] = []
        self._placeholder: Optional[QLabel] = None

        self.original_task = TaskRunner(self)
        self.preview_task = TaskRunner(self)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(140)
        self._preview_timer.timeout.connect(self._refresh_preview)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._sidebar())
        layout.addWidget(self._viewer(), 1)

    @property
    def session(self):
        return self.item.session

    # -- construction ------------------------------------------------------

    def _sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(392)
        column = QVBoxLayout(side)
        column.setContentsMargins(14, 14, 14, 12)
        column.setSpacing(10)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search detected values\u2026")
        self.search_box.textChanged.connect(self._on_search)
        column.addWidget(self.search_box)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.chip_group = QButtonGroup(self)
        self.chips: dict[str, QPushButton] = {}
        for key, label in (
            ("flagged", "To review"), ("reviewed", "Done"), ("all", "All"), ("kept", "Kept")
        ):
            chip = QPushButton(label)
            chip.setObjectName("Chip")
            chip.setCheckable(True)
            chip.clicked.connect(lambda _c=False, k=key: self.set_filter(k))
            self.chip_group.addButton(chip)
            self.chips[key] = chip
            chips.addWidget(chip)
        chips.addStretch(1)
        self.chips["flagged"].setChecked(True)
        column.addLayout(chips)

        self.list_area = QScrollArea()
        self.list_area.setWidgetResizable(True)
        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        self.list_area.setWidget(self.list_host)
        column.addWidget(self.list_area, 1)

        bulk = QHBoxLayout()
        bulk.setSpacing(6)
        redact_all = QPushButton("Redact everything")
        redact_all.clicked.connect(self.redact_all)
        undo = QPushButton("Undo")
        undo.clicked.connect(self.undo)
        add_missed = QPushButton("Add missed item\u2026")
        add_missed.clicked.connect(self.add_missed)
        bulk.addWidget(redact_all, 1)
        bulk.addWidget(add_missed)
        bulk.addWidget(undo)
        column.addLayout(bulk)

        decide = QHBoxLayout()
        decide.setSpacing(6)
        self.approve_button = QPushButton("Approve for processing")
        self.approve_button.setObjectName("Primary")
        self.approve_button.clicked.connect(self.approve)
        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.clicked.connect(self.dismiss)
        decide.addWidget(self.approve_button, 1)
        decide.addWidget(self.dismiss_button)
        column.addLayout(decide)
        return side

    def _viewer(self) -> QWidget:
        wrapper = QWidget()
        column = QVBoxLayout(wrapper)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        toolbar = QWidget()
        toolbar.setObjectName("Toolbar")
        toolbar.setFixedHeight(48)
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(14, 8, 14, 8)
        bar.setSpacing(6)

        self.view_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        for key, label in (
            ("original", "Original"), ("redacted", "Redacted"), ("split", "Side by side")
        ):
            button = QPushButton(label)
            button.setObjectName("Chip")
            button.setCheckable(True)
            button.clicked.connect(lambda _c=False, k=key: self.set_view(k))
            group.addButton(button)
            self.view_buttons[key] = button
            bar.addWidget(button)
        self.view_buttons["split"].setChecked(True)
        bar.addSpacing(16)

        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.setFixedWidth(62)
        self.page_spin.valueChanged.connect(lambda v: self.go_page(v - 1))
        self.page_total = QLabel("of 0")
        self.page_total.setObjectName("CardMeta")
        bar.addWidget(self.page_spin)
        bar.addWidget(self.page_total)
        bar.addStretch(1)

        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("CardMeta")
        self.zoom_label.setFixedWidth(38)
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setFixedWidth(130)
        self.zoom_slider.setRange(50, 300)
        self.zoom_slider.setValue(100)
        self.zoom_slider.valueChanged.connect(self._on_zoom)
        bar.addWidget(self.zoom_label)
        bar.addWidget(self.zoom_slider)
        column.addWidget(toolbar)

        canvases = QWidget()
        row = QHBoxLayout(canvases)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(1)
        self.left_canvas = DocumentView("original")
        self.right_canvas = DocumentView("anonymized preview")
        row.addWidget(self.left_canvas)
        row.addWidget(self.right_canvas)
        for a, b in ((self.left_canvas, self.right_canvas), (self.right_canvas, self.left_canvas)):
            a.verticalScrollBar().valueChanged.connect(b.verticalScrollBar().setValue)
        column.addWidget(canvases, 1)
        return wrapper

    # -- population --------------------------------------------------------

    def populate(self) -> None:
        if not self.session or not self.session.document:
            return
        total = self.session.document.page_count
        self.page_spin.setMaximum(max(1, total))
        self.page_total.setText(f"of {total}")
        self.rebuild_cards()
        self.rebuild_views()
        fade_in(self)

    def groups(self) -> list[OccurrenceGroup]:
        if not self.session or not self.session.detection:
            return []
        return self.session.decisions.occurrence_groups(self.session.candidates)

    def _state_of(self, group: OccurrenceGroup) -> DecisionState:
        return self.session.decisions.state(group.candidates[0])

    def _is_reviewed(self, group: OccurrenceGroup) -> bool:
        return all(self.session.decisions.is_reviewed(c) for c in group.candidates)

    def visible_groups(self) -> list[OccurrenceGroup]:
        groups = self.groups()
        if self.filter_mode == "flagged":
            groups = [g for g in groups if not self._is_reviewed(g)]
        elif self.filter_mode == "reviewed":
            groups = [g for g in groups if self._is_reviewed(g)]
        elif self.filter_mode == "kept":
            groups = [g for g in groups if self._state_of(g) is KEEP]
        if self.search_text:
            needle = self.search_text.lower()
            groups = [
                g for g in groups
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

    def rebuild_cards(self) -> None:
        self._clear_cards()
        groups = self.visible_groups()
        if not groups:
            message = {
                "flagged": "Everything here has been reviewed.",
                "reviewed": "Nothing reviewed yet. Tick an item to move it here.",
                "kept": "Nothing is being kept \u2014 every detection will be pseudonymized.",
                "all": "No detections. If that looks wrong, the page may need OCR.",
            }[self.filter_mode]
            placeholder = QLabel(message)
            placeholder.setObjectName("EmptyState")
            placeholder.setWordWrap(True)
            placeholder.setAlignment(Qt.AlignCenter)
            self.list_layout.insertWidget(0, placeholder)
            self._placeholder = placeholder
        else:
            for group in groups:
                card = DetectionCard(
                    group, self._state_of(group), group.needs_review, self._is_reviewed(group)
                )
                card.selected.connect(self.select_group)
                card.decided.connect(self.decide_group)
                card.reviewed_changed.connect(self.mark_reviewed)  # kept for the API
                card.page_clicked.connect(self.jump_to_page)
                card.set_selected((group.pii_type, group.normalized) == self.selected_key)
                self.list_layout.insertWidget(self.list_layout.count() - 1, card)
                self.cards.append(card)
            fade_in(self.list_host, 140)
        self._update_chips()
        self.changed.emit()

    def _update_chips(self) -> None:
        groups = self.groups()
        flagged = len([g for g in groups if not self._is_reviewed(g)])
        reviewed = len([g for g in groups if self._is_reviewed(g)])
        kept = len([g for g in groups if self._state_of(g) is KEEP])
        self.chips["flagged"].setText(f"To review \u00b7 {flagged}")
        self.chips["reviewed"].setText(f"Done \u00b7 {reviewed}")
        self.chips["all"].setText(f"All \u00b7 {len(groups)}")
        self.chips["kept"].setText(f"Kept \u00b7 {kept}")

    # -- interaction -------------------------------------------------------

    def set_filter(self, mode: str) -> None:
        self.filter_mode = mode
        if not self.chips[mode].isChecked():
            self.chips[mode].setChecked(True)
        self.rebuild_cards()

    def _on_search(self, text: str) -> None:
        self.search_text = text.strip()
        self.rebuild_cards()

    def select_group(self, group: OccurrenceGroup) -> None:
        self.selected_key = (group.pii_type, group.normalized)
        for card in self.cards:
            card.set_selected((card.group.pii_type, card.group.normalized) == self.selected_key)
        first = group.candidates[0]
        self.rebuild_views()
        self.left_canvas.scroll_to_page(first.page_no, first.rect, self.zoom)
        self.right_canvas.scroll_to_page(first.page_no, first.rect, self.zoom)

    def jump_to_page(self, group: OccurrenceGroup, page_no: int) -> None:
        self.select_group(group)
        self.left_canvas.scroll_to_page(page_no)
        self.right_canvas.scroll_to_page(page_no)

    def clear_focus(self) -> None:
        self.selected_key = None
        for card in self.cards:
            card.set_selected(False)
        self.rebuild_views()

    def decide_group(self, group: OccurrenceGroup, state: DecisionState) -> None:
        if state is DecisionState.EDITED:
            current = self.session.registry.lookup(group.pii_type, group.normalized)
            dialog = EditDetectionDialog(
                self,
                original=group.display,
                pii_type=group.pii_type,
                replacement=current.pseudonym if current else "",
            )
            if dialog.exec() != dialog.Accepted:
                return
            values = dialog.values if hasattr(dialog, "values") else None
            if values is None:
                new_type, replacement, apply_all = dialog.result_values()
            else:
                new_type = values["pii_type"]
                replacement = values["replacement"]
                apply_all = values["apply_to_all"]

            targets = group.candidates
            if apply_all:
                targets = [
                    c for c in self.session.candidates
                    if c.normalized.lower() == group.normalized.lower()
                ]
            for candidate in targets:
                candidate.pii_type = new_type
            if replacement:
                self.session.decisions.edit(targets, replacement)
            else:
                # Retyping regenerates the pseudonym so it matches the new kind.
                self.session.registry.forget(group.pii_type, group.normalized)
                self.session.decisions.set_state(targets, REDACT)
        else:
            self.session.decisions.set_state(group.candidates, state)
        # Deciding is reviewing. The item moves to Done with no extra click.
        self.session.decisions.mark_reviewed(group.candidates, True)
        self._after_decision()

    def mark_reviewed(self, group: OccurrenceGroup, value: bool) -> None:
        self.session.decisions.mark_reviewed(group.candidates, value)
        self.rebuild_cards()

    def add_missed(self) -> None:
        """Let the user add a value the detectors never found."""
        if not self.session or not self.session.detection:
            return
        dialog = AddPiiDialog(self)
        if dialog.exec() != dialog.Accepted:
            return
        text, pii_type, replacement, cascade, apply_same = dialog.result_values()
        added = self.session.add_manual_text(
            text,
            replacement=replacement or None,
            pii_type=pii_type,
            cascade=cascade,
            apply_to_same=apply_same,
        )
        if not added:
            QMessageBox.information(
                self, "Not found",
                f"\u201c{text}\u201d does not appear in this document's text.\n\n"
                "Check the spelling, or the page may need OCR.",
            )
            return
        self._after_decision()
        QMessageBox.information(
            self, "Added",
            f"Found {len(added)} occurrence(s) of \u201c{text}\u201d on "
            f"{len({c.page_no for c in added})} page(s).",
        )

    def redact_all(self) -> None:
        self.session.decisions.set_state(self.session.candidates, REDACT)
        self._after_decision()

    def undo(self) -> None:
        if self.session.decisions.undo():
            self._after_decision()

    def _after_decision(self) -> None:
        self.rebuild_cards()
        self.draw_overlays()
        self._preview_timer.start()

    def approve(self) -> None:
        self.item.state = ItemState.APPROVED
        self.changed.emit()

    def dismiss(self) -> None:
        self.item.state = ItemState.DISMISSED
        self.changed.emit()

    # -- rendering ---------------------------------------------------------

    def set_view(self, mode: str) -> None:
        self.view_mode = mode
        self.rebuild_views()

    def _on_zoom(self, value: int) -> None:
        self.zoom = value / 100
        self.zoom_label.setText(f"{value}%")
        self.rebuild_views()

    def go_page(self, page_no: int) -> None:
        self.left_canvas.scroll_to_page(page_no)
        self.right_canvas.scroll_to_page(page_no)

    def visible_pages(self) -> list[int]:
        if not self.session or not self.session.document:
            return []
        total = self.session.document.page_count
        if self.selected_key is None:
            return list(range(total))
        pages = sorted({
            c.page_no for c in self.session.candidates
            if (c.pii_type, c.normalized) == self.selected_key
        })
        return pages or list(range(total))

    def rebuild_views(self) -> None:
        if not self.session or not self.session.document:
            return
        pages = self.visible_pages()
        show_original = self.view_mode in ("original", "split")
        show_redacted = self.view_mode in ("redacted", "split")
        self.left_canvas.setVisible(show_original)
        self.right_canvas.setVisible(show_redacted)
        self.left_canvas.build(pages if show_original else [])
        self.right_canvas.build(pages if show_redacted else [])

        if show_original:
            zoom = self.zoom
            self.original_task.run(
                lambda: self.session.preview_originals(pages, zoom),
                lambda images: self._apply(self.left_canvas, images, zoom, True),
                lambda message: self._on_render_error("original", message),
            )
        if show_redacted:
            self._preview_timer.start()

    def _on_render_error(self, which: str, message: str) -> None:
        """Surface a failed render instead of leaving a blank page.

        These were discarded silently, which is why a preview could show nothing
        but page headers with no indication anything had gone wrong.
        """
        log.warning("%s render failed: %s", which, message)
        canvas = self.left_canvas if which == "original" else self.right_canvas
        if shiboken6.isValid(canvas):
            canvas.clear(f"Could not render the {which}:\n{message}")

    def _apply(self, canvas, images: dict, zoom: float, overlays: bool) -> None:
        if not shiboken6.isValid(self) or not shiboken6.isValid(canvas):
            return
        for page_no, png in images.items():
            canvas.set_png(page_no, png, zoom)
        if overlays:
            self.draw_overlays()

    def _refresh_preview(self) -> None:
        if not shiboken6.isValid(self) or not shiboken6.isValid(self.right_canvas):
            return
        if not self.session or self.view_mode == "original":
            return
        pages, zoom = self.visible_pages(), self.zoom
        self.preview_task.run(
            lambda: self.session.preview_transformed_pages(pages, zoom),
            lambda images: self._apply(self.right_canvas, images, zoom, False),
            lambda message: self._on_render_error("redacted preview", message),
        )

    def draw_overlays(self) -> None:
        if not shiboken6.isValid(self) or not self.session or not self.session.detection:
            return
        by_page: dict[int, list] = {p: [] for p in self.visible_pages()}
        for candidate in self.session.candidates:
            if candidate.page_no not in by_page:
                continue
            state = self.session.decisions.state(candidate)
            kind = "keep" if state is KEEP else ("review" if candidate.needs_review else "redact")
            focused = (
                self.selected_key is not None
                and (candidate.pii_type, candidate.normalized) == self.selected_key
            )
            by_page[candidate.page_no].append((candidate.rect, kind, focused))
        for page_no, overlays in by_page.items():
            self.left_canvas.set_overlays(page_no, overlays)

    def stop(self) -> None:
        # The debounce timer must die first. Left running, it fires ~140ms after
        # the tab is gone and renders into a destroyed widget.
        self._preview_timer.stop()
        stop_all_animations()
        self.original_task.stop()
        self.preview_task.stop()


class ResultsView(QWidget):
    """What changed, beside the finished document."""

    def __init__(self):
        super().__init__()
        self.items: list[BatchItem] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        left = QWidget()
        left.setObjectName("Sidebar")
        left.setFixedWidth(580)
        column = QVBoxLayout(left)
        column.setContentsMargins(16, 16, 16, 16)
        column.setSpacing(10)

        heading = QLabel("What changed")
        heading.setObjectName("DocTitle")
        column.addWidget(heading)

        self.doc_picker = QHBoxLayout()
        self.doc_picker.setSpacing(6)
        column.addLayout(self.doc_picker)
        self.doc_buttons: list[QPushButton] = []

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Type", "Original", "Pseudonym", "Pages"])
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        column.addWidget(self.table, 1)

        self.summary = QLabel("")
        self.summary.setObjectName("CardMeta")
        self.summary.setWordWrap(True)
        column.addWidget(self.summary)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.open_folder_button = QPushButton("Open output folder")
        self.open_folder_button.setObjectName("Primary")
        self.start_over_button = QPushButton("Start over")
        buttons.addWidget(self.open_folder_button, 1)
        buttons.addWidget(self.start_over_button)
        column.addLayout(buttons)

        layout.addWidget(left)
        self.preview = DocumentView("redacted preview")
        layout.addWidget(self.preview, 1)

    def show_results(self, items: list[BatchItem], output_folder: Optional[str]) -> None:
        self.items = [i for i in items if i.state is ItemState.DONE]
        for button in self.doc_buttons:
            button.setParent(None)
            button.deleteLater()
        self.doc_buttons = []
        for index, item in enumerate(self.items):
            button = QPushButton(item.name[:24])
            button.setObjectName("Chip")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.clicked.connect(lambda _c=False, i=index: self.select(i))
            self.doc_picker.addWidget(button)
            self.doc_buttons.append(button)

        total = sum(len(i.changes) for i in self.items)
        verified = len([i for i in self.items if i.verified])
        self.summary.setText(
            f"{len(self.items)} document(s) written to "
            f"{output_folder or 'their source folders'} \u00b7 {total} distinct value(s) "
            f"replaced \u00b7 {verified} passed export verification.\n"
            "The mapping workbook is in the mappings subfolder \u2014 never send it with the PDF."
        )
        if self.items:
            self.select(0)
        fade_in(self)

    def select(self, index: int) -> None:
        if not (0 <= index < len(self.items)):
            return
        for position, button in enumerate(self.doc_buttons):
            button.setChecked(position == index)
        item = self.items[index]

        self.table.setRowCount(len(item.changes))
        for row, change in enumerate(item.changes):
            values = [
                change.pii_type.replace("_", " ").title(),
                change.original,
                change.pseudonym,
                ", ".join(str(p) for p in change.pages[:6]),
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(row, column, cell)

        if item.output_path and Path(item.output_path).exists() and item.session:
            from ..export.redactor import render_originals

            pages = list(range(min(6, item.session.document.page_count)))
            self.preview.build(pages)
            for page_no, png in render_originals(item.output_path, pages, 1.0).items():
                self.preview.set_png(page_no, png, 1.0)


class ProcessingView(QWidget):
    """Shown while documents are being written."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(60, 60, 60, 60)
        layout.setSpacing(14)
        layout.addStretch(1)

        self.heading = QLabel("Processing")
        self.heading.setObjectName("DocTitle")
        self.heading.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.heading)

        self.status_line = QLabel("Starting\u2026")
        self.status_line.setObjectName("CardMeta")
        self.status_line.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_line)

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        layout.addWidget(self.bar)

        self.detail = QLabel("")
        self.detail.setObjectName("CardMeta")
        self.detail.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.detail)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setFixedWidth(140)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.cancel_button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(2)


class MainWindow(QMainWindow):
    #: Emitted from the worker thread; Qt queues it onto the GUI thread. Widgets
    #: must never be touched directly from a worker - that is what made the app
    #: vanish when processing started.
    progress_reported = Signal(str, int, int)

    def __init__(self):
        super().__init__()
        self.batch = Batch()
        self.tabs_by_item: dict[str, DocumentTab] = {}
        self.analysis_task = TaskRunner(self)
        self.process_task = TaskRunner(self)

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1620, 1000)
        self.setStyleSheet(theme.STYLESHEET)
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self._build()
        self.progress_reported.connect(self._on_progress, Qt.QueuedConnection)
        self._shortcuts()
        self._set_status(Status.IDLE, "Add documents to begin")

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        root = QWidget()
        column = QVBoxLayout(root)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self._header())
        column.addWidget(self._folders())

        self.stack = QStackedWidget()
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(lambda _i: self._refresh_queue())

        self.empty = QLabel(
            "Add files or a folder to begin.\n\nNothing is written until you press Process."
        )
        self.empty.setObjectName("EmptyState")
        self.empty.setAlignment(Qt.AlignCenter)

        self.results = ResultsView()
        self.results.open_folder_button.clicked.connect(self.open_output_folder)
        self.results.start_over_button.clicked.connect(self.start_over)

        self.processing = ProcessingView()
        self.processing.cancel_button.clicked.connect(self.cancel)
        self.status_line = self.processing.status_line
        self.progress_detail = self.processing.detail

        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.tabs)
        self.stack.addWidget(self.processing)
        self.stack.addWidget(self.results)
        column.addWidget(self.stack, 1)
        column.addWidget(self._queue())
        self.setCentralWidget(root)

    def _header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("Header")
        header.setFixedHeight(62)
        row = QHBoxLayout(header)
        row.setContentsMargins(18, 10, 18, 10)
        row.setSpacing(10)

        add_files = QPushButton("Add files")
        add_files.clicked.connect(self.add_files)
        add_folder = QPushButton("Add folder")
        add_folder.clicked.connect(self.add_folder)
        row.addWidget(add_files)
        row.addWidget(add_folder)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.title_label = QLabel(APP_NAME)
        self.title_label.setObjectName("DocTitle")
        self.subtitle_label = QLabel(_build_summary())
        self.subtitle_label.setObjectName("DocSubtitle")
        titles.addWidget(self.title_label)
        titles.addWidget(self.subtitle_label)
        row.addLayout(titles, 1)

        self.status_pill = QLabel(Status.IDLE)
        self.status_pill.setObjectName("StatusPill")
        row.addWidget(self.status_pill)
        return header

    def _folders(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("Toolbar")
        bar.setFixedHeight(46)
        row = QHBoxLayout(bar)
        row.setContentsMargins(18, 8, 18, 8)
        row.setSpacing(8)

        label = QLabel("Output folder")
        label.setObjectName("CardMeta")
        row.addWidget(label)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Same folder as each source file")
        self.output_edit.textChanged.connect(
            lambda text: setattr(self.batch, "output_folder", text.strip() or None)
        )
        row.addWidget(self.output_edit, 1)
        browse = QPushButton("Choose\u2026")
        browse.clicked.connect(self.choose_output_folder)
        row.addWidget(browse)
        open_button = QPushButton("Open")
        open_button.setObjectName("Ghost")
        open_button.clicked.connect(self.open_output_folder)
        row.addWidget(open_button)
        return bar

    def _queue(self) -> QWidget:
        footer = QWidget()
        footer.setObjectName("Footer")
        footer.setFixedHeight(62)
        row = QHBoxLayout(footer)
        row.setContentsMargins(18, 10, 18, 10)
        row.setSpacing(10)

        self.queue_label = QLabel("")
        self.queue_label.setObjectName("CardMeta")
        row.addWidget(self.queue_label, 1)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(220)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        row.addWidget(self.progress)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.cancel)
        row.addWidget(self.cancel_button)

        self.start_over_button = QPushButton("Start over")
        self.start_over_button.clicked.connect(self.start_over)
        row.addWidget(self.start_over_button)

        self.process_button = QPushButton("Process approved")
        self.process_button.setObjectName("Primary")
        self.process_button.setEnabled(False)
        self.process_button.clicked.connect(self.process)
        row.addWidget(self.process_button)
        return footer

    def _shortcuts(self) -> None:
        for keys, slot in (
            ("Ctrl+O", self.add_files),
            ("Ctrl+Shift+O", self.add_folder),
            ("Ctrl+E", self.process),
            ("Escape", self._clear_focus),
        ):
            QShortcut(QKeySequence(keys), self, activated=slot)

    # -- status ------------------------------------------------------------

    def _set_status(self, status: str, detail: str = "") -> None:
        self.status_pill.setText(status)
        colour = {
            Status.NEEDS_REVIEW: theme.WARN,
            Status.READY: theme.OK,
            Status.VERIFIED: theme.OK,
            Status.VERIFICATION_FAILED: theme.DANGER,
            Status.EXPORT_FAILED: theme.DANGER,
            Status.OCR_REQUIRED: theme.WARN,
        }.get(status, theme.TEXT_DIM)
        self.status_pill.setStyleSheet(
            f"color: {colour}; background: {theme.rgba(colour, 0.12)};"
            f"border: 1px solid {theme.rgba(colour, 0.35)};"
        )
        if detail:
            self.subtitle_label.setText(detail)

    # -- input -------------------------------------------------------------

    def add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add PDFs", "", "PDF files (*.pdf)")
        if paths:
            self.add(self.batch.add_files(paths))

    def add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add a folder of PDFs")
        if not folder:
            return
        added = self.batch.add_folder(folder)
        if not added:
            QMessageBox.information(self, "Nothing added", "No PDFs were found in that folder.")
        self.add(added)

    def add(self, items: list[BatchItem]) -> None:
        for item in items:
            tab = DocumentTab(item)
            tab.changed.connect(self._refresh_queue)
            index = self.tabs.addTab(tab, item.name[:26])
            self.tabs.setTabToolTip(index, item.source_path)
            self.tabs_by_item[item.source_path] = tab
        if self.tabs.count():
            self.stack.setCurrentWidget(self.tabs)
        self._refresh_queue()
        self.analyse_next()

    def analyse_next(self) -> None:
        pending = [i for i in self.batch.items if i.state is ItemState.PENDING]
        if not pending:
            self.progress.setVisible(False)
            self._set_status(Status.NEEDS_REVIEW, "Review each tab, then approve it")
            return
        item = pending[0]
        self._set_status(Status.ANALYZING, f"Analyzing {item.name}\u2026")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.analysis_task.run(
            lambda: self.batch.analyse(item),
            self._on_analysed,
            lambda message: self._on_analysis_error(item, message),
        )

    def _on_analysed(self, item: BatchItem) -> None:
        tab = self.tabs_by_item.get(item.source_path)
        if tab is not None:
            tab.populate()
        self._refresh_queue()
        self.analyse_next()

    def _on_analysis_error(self, item: BatchItem, message: str) -> None:
        item.state = ItemState.FAILED
        item.error = message
        self._refresh_queue()
        self.analyse_next()

    def _close_tab(self, index: int) -> None:
        tab = self.tabs.widget(index)
        if isinstance(tab, DocumentTab):
            tab.stop()
            self.batch.remove(tab.item)
            self.tabs_by_item.pop(tab.item.source_path, None)
        self.tabs.removeTab(index)
        if self.tabs.count() == 0:
            self.stack.setCurrentWidget(self.empty)
        self._refresh_queue()

    def _clear_focus(self) -> None:
        tab = self.tabs.currentWidget()
        if isinstance(tab, DocumentTab):
            tab.clear_focus()

    # -- queue -------------------------------------------------------------

    def _refresh_queue(self) -> None:
        approved = self.batch.approved
        ready = [i for i in self.batch.items if i.state is ItemState.READY]
        dismissed = [i for i in self.batch.items if i.state is ItemState.DISMISSED]
        failed = [i for i in self.batch.items if i.state is ItemState.FAILED]
        parts = [f"{len(approved)} approved"]
        if ready:
            parts.append(f"{len(ready)} awaiting review")
        if dismissed:
            parts.append(f"{len(dismissed)} dismissed")
        if failed:
            parts.append(f"{len(failed)} failed")
        self.queue_label.setText(" \u00b7 ".join(parts))
        self.process_button.setEnabled(bool(approved))
        self.process_button.setText(
            f"Process {len(approved)} approved" if approved else "Process approved"
        )
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            if isinstance(tab, DocumentTab):
                marker = {
                    ItemState.APPROVED: "\u2713 ",
                    ItemState.DISMISSED: "\u2014 ",
                    ItemState.FAILED: "! ",
                }.get(tab.item.state, "")
                self.tabs.setTabText(index, marker + tab.item.name[:24])

    # -- output ------------------------------------------------------------

    def choose_output_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose an output folder")
        if folder:
            self.output_edit.setText(folder)

    def open_output_folder(self) -> None:
        folder = self.batch.output_folder
        if not folder:
            done = self.batch.completed
            folder = str(Path(done[0].output_path).parent) if done and done[0].output_path else None
        if not folder or not Path(folder).exists():
            QMessageBox.information(
                self, "No output yet", "Choose an output folder, or process a document first."
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def process(self) -> None:
        approved = self.batch.approved
        if not approved:
            return
        reviewed, unreviewed = self.batch.reviewed_counts()
        if unreviewed:
            prompt = UnreviewedPrompt(self, reviewed=reviewed, unreviewed=unreviewed)
            if prompt.exec() != prompt.Accepted:
                return
            if prompt.choice == prompt.KEEP_REVIEWING:
                self.stack.setCurrentWidget(self.tabs)
                return
            if prompt.choice == prompt.PROCESS_REVIEWED:
                if not reviewed:
                    QMessageBox.information(
                        self, "Nothing reviewed yet",
                        "No items have been reviewed, so there is nothing to apply.",
                    )
                    return
                self.batch.keep_unreviewed()
            elif prompt.choice == prompt.PROCESS_ALL:
                self.batch.redact_everything()
            else:
                return

        self.batch.reset_cancel()
        self.progress.setVisible(True)
        self.progress.setRange(0, len(approved))
        self.progress.setValue(0)
        self.processing.bar.setRange(0, len(approved))
        self.processing.bar.setValue(0)
        self.processing.heading.setText(
            f"Processing {len(approved)} document" + ("s" if len(approved) != 1 else "")
        )
        self.processing.status_line.setText("Starting\u2026")
        self.processing.detail.setText("")
        self.stack.setCurrentWidget(self.processing)
        fade_in(self.processing)
        self.cancel_button.setVisible(True)
        self.process_button.setEnabled(False)
        self._set_status(Status.PROCESSING, "Writing anonymized documents\u2026")
        self.process_task.run(
            lambda: self.batch.process_approved(progress=self._report),
            self._on_processed,
            self._on_process_error,
        )

    def _report(self, progress: BatchProgress) -> None:
        """Called ON THE WORKER THREAD. Only emit; never touch a widget here."""
        suffix = f" \u2014 {progress.document}" if progress.document else ""
        self.progress_reported.emit(progress.text + suffix, progress.current, progress.total)

    def _on_progress(self, text: str, current: int, total: int) -> None:
        """Runs on the GUI thread via a queued connection."""
        if not shiboken6.isValid(self):
            return
        self.status_line.setText(text)
        self.queue_label.setText(text)
        if total:
            self.progress.setValue(current)
            self.processing.bar.setRange(0, total)
            self.processing.bar.setValue(current)
            self.progress_detail.setText(f"{current} of {total}")

    def _on_processed(self, done: list[BatchItem]) -> None:
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)
        self.process_button.setEnabled(True)
        self._refresh_queue()
        if self.batch.cancelled:
            self._set_status(
                Status.READY, f"Cancelled \u2014 {len(done)} document(s) completed and kept"
            )
            self.stack.setCurrentWidget(self.results if done else self.tabs)
            if done:
                self.results.show_results(self.batch.items, self.batch.output_folder)
            return
        unverified = [i for i in done if not i.verified]
        if unverified:
            self._set_status(
                Status.VERIFICATION_FAILED,
                f"{len(done)} written, {len(unverified)} did not pass verification",
            )
        else:
            self._set_status(Status.VERIFIED, f"{len(done)} document(s) written and verified")
        self.results.show_results(self.batch.items, self.batch.output_folder)
        self.stack.setCurrentWidget(self.results)

    def _on_process_error(self, message: str) -> None:
        self.stack.setCurrentWidget(self.tabs if self.tabs.count() else self.empty)
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)
        self.process_button.setEnabled(True)
        self._set_status(Status.EXPORT_FAILED, message)
        QMessageBox.critical(self, "Processing failed", message)

    def cancel(self) -> None:
        self.batch.cancel()
        self.queue_label.setText("Cancelling after the current document\u2026")
        self.cancel_button.setEnabled(False)
        QTimer.singleShot(1500, lambda: self.cancel_button.setEnabled(True))

    def start_over(self) -> None:
        """Clear the workspace. Files already written are left alone."""
        if self.batch.items:
            answer = QMessageBox.question(
                self,
                "Start over?",
                "This clears the documents from the workspace.\n\n"
                "Anything already written to the output folder is kept.",
            )
            if answer is not QMessageBox.Yes:
                return
        for index in reversed(range(self.tabs.count())):
            tab = self.tabs.widget(index)
            if isinstance(tab, DocumentTab):
                tab.stop()
            self.tabs.removeTab(index)
        self.tabs_by_item = {}
        self.batch.clear()
        self.stack.setCurrentWidget(self.empty)
        self._refresh_queue()
        self._set_status(Status.IDLE, "Add documents to begin")

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        self.analysis_task.stop()
        self.process_task.stop()
        # Every tab, not just the ones still in the index: a tab removed from
        # the widget can still own a running render thread.
        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if isinstance(widget, DocumentTab) and shiboken6.isValid(widget):
                widget.stop()
        for tab in list(self.tabs_by_item.values()):
            tab.stop()
        stop_all_animations()
        stop_all_runners()
        super().closeEvent(event)


def run() -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(APP_NAME)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    window = MainWindow()
    window.show()
    try:
        return app.exec()
    finally:
        stop_all_runners()
