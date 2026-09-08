"""Batch processing (spec section 45).

The workflow the UI drives:

    add files or a folder
        -> one tab per document, analysed independently
        -> the user approves, dismisses, or leaves each one
        -> approved documents are processed to the output folder
        -> a summary of what changed, per document

A single shared roster runs through the whole batch, so one client keeps one
pseudonym across every file in it. Processing is cancellable between documents
and reports progress as it goes; a cancelled run leaves the documents it already
wrote in place rather than deleting them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional

from .decisions.manager import DecisionState
from .entities.roster import ClientRoster
from .session import AnonymizationSession, Status

PDF_GLOB = "*.pdf"


class ItemState(str, Enum):
    PENDING = "Pending"
    ANALYZING = "Analyzing"
    READY = "Ready to review"
    APPROVED = "Approved"
    DISMISSED = "Dismissed"
    PROCESSING = "Processing"
    DONE = "Done"
    FAILED = "Failed"


@dataclass
class Change:
    """One original-to-pseudonym substitution, for the summary table."""

    pii_type: str
    original: str
    pseudonym: str
    occurrences: int
    pages: list[int] = field(default_factory=list)


@dataclass
class BatchItem:
    source_path: str
    session: Optional[AnonymizationSession] = None
    state: ItemState = ItemState.PENDING
    output_path: Optional[str] = None
    mapping_path: Optional[str] = None
    error: str = ""
    verification: Optional[object] = None
    changes: list[Change] = field(default_factory=list)

    @property
    def name(self) -> str:
        return Path(self.source_path).name

    @property
    def detections(self) -> int:
        return len(self.session.candidates) if self.session else 0

    @property
    def flagged(self) -> int:
        if not self.session:
            return 0
        return len(
            [
                c
                for c in self.session.candidates
                if c.needs_review and not self.session.decisions.is_reviewed(c)
            ]
        )

    @property
    def verified(self) -> bool:
        return bool(self.verification and getattr(self.verification, "passed", False))


@dataclass
class BatchProgress:
    stage: str
    current: int = 0
    total: int = 0
    document: str = ""

    @property
    def text(self) -> str:
        if self.total:
            return f"{self.stage} \u2014 {self.current} of {self.total}"
        return self.stage


class Cancelled(RuntimeError):
    pass


@dataclass
class Batch:
    """The whole working set."""

    items: list[BatchItem] = field(default_factory=list)
    output_folder: Optional[str] = None
    mapping_folder: Optional[str] = None
    roster_path: Optional[str] = None
    roster: ClientRoster = field(default_factory=ClientRoster)
    _cancel: threading.Event = field(default_factory=threading.Event)

    # -- input ------------------------------------------------------------

    def add_files(self, paths: Iterable[str]) -> list[BatchItem]:
        added = []
        existing = {item.source_path for item in self.items}
        for path in paths:
            resolved = str(Path(path).expanduser().resolve())
            if resolved in existing or not resolved.lower().endswith(".pdf"):
                continue
            item = BatchItem(source_path=resolved)
            self.items.append(item)
            added.append(item)
            existing.add(resolved)
        return added

    def add_folder(self, folder: str, recursive: bool = True) -> list[BatchItem]:
        root = Path(folder).expanduser()
        finder = root.rglob if recursive else root.glob
        return self.add_files(sorted(str(p) for p in finder(PDF_GLOB)))

    def remove(self, item: BatchItem) -> None:
        if item in self.items:
            self.items.remove(item)

    def clear(self) -> None:
        """Start over. Files already written to the output folder are untouched."""
        self.items = []
        self._cancel.clear()

    # -- state ------------------------------------------------------------

    @property
    def approved(self) -> list[BatchItem]:
        return [i for i in self.items if i.state is ItemState.APPROVED]

    @property
    def completed(self) -> list[BatchItem]:
        return [i for i in self.items if i.state is ItemState.DONE]

    def approve(self, item: BatchItem) -> None:
        if item.session is not None:
            item.state = ItemState.APPROVED

    def dismiss(self, item: BatchItem) -> None:
        item.state = ItemState.DISMISSED

    def unapprove(self, item: BatchItem) -> None:
        if item.state in (ItemState.APPROVED, ItemState.DISMISSED):
            item.state = ItemState.READY

    # -- analysis ---------------------------------------------------------

    def analyse(
        self, item: BatchItem, progress: Optional[Callable[[BatchProgress], None]] = None
    ) -> BatchItem:
        item.state = ItemState.ANALYZING
        if progress:
            progress(BatchProgress("Reading the document", document=item.name))
        try:
            if self.roster_path:
                self.roster = self.roster or ClientRoster.load(Path(self.roster_path))
            session = AnonymizationSession(
                source_path=item.source_path,
                roster=self.roster,
                roster_path=self.roster_path,
            )
            if progress:
                progress(BatchProgress("Detecting sensitive values", document=item.name))
            session.analyse()
            item.session = session
            item.state = ItemState.READY
        except Exception as exc:  # noqa: BLE001 - surfaced on the item
            item.state = ItemState.FAILED
            item.error = f"{type(exc).__name__}: {exc}"
        return item

    # -- processing -------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()

    def reset_cancel(self) -> None:
        self._cancel.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def output_path_for(self, item: BatchItem) -> str:
        session = item.session
        name = session.safe_output_name() if session else f"{Path(item.source_path).stem}.anonymized.pdf"
        folder = Path(self.output_folder) if self.output_folder else Path(item.source_path).parent
        folder.mkdir(parents=True, exist_ok=True)
        candidate = folder / name
        # Never silently overwrite a document already produced.
        counter = 2
        while candidate.exists():
            candidate = folder / f"{Path(name).stem}-{counter}{Path(name).suffix}"
            counter += 1
        return str(candidate)

    def process_approved(
        self, progress: Optional[Callable[[BatchProgress], None]] = None
    ) -> list[BatchItem]:
        """Export every approved document. Cancellable between documents.

        The cancel flag is NOT cleared here. Clearing it on entry would lose a
        cancel that arrived between the user pressing the button and this call
        starting - a real race in a threaded UI. Callers clear it deliberately
        with `reset_cancel()` when beginning a run.
        """
        queue = self.approved
        done: list[BatchItem] = []
        for index, item in enumerate(queue, start=1):
            if self.cancelled:
                break
            item.state = ItemState.PROCESSING
            if progress:
                progress(BatchProgress("Redacting", index, len(queue), item.name))
            try:
                output = self.output_path_for(item)
                if self.mapping_folder:
                    item.session.mapping_folder = self.mapping_folder
                _apply_report, report = item.session.process(output)
                item.output_path = output
                item.mapping_path = item.session.mapping_file
                item.verification = report
                item.changes = summarise_changes(item)
                item.state = ItemState.DONE
                done.append(item)
            except Exception as exc:  # noqa: BLE001
                item.state = ItemState.FAILED
                item.error = f"{type(exc).__name__}: {exc}"
            if progress:
                progress(BatchProgress("Verifying", index, len(queue), item.name))
        if progress:
            progress(BatchProgress("Finished", len(done), len(queue)))
        return done


def summarise_changes(item: BatchItem) -> list[Change]:
    """What actually changed, for the completion view."""
    if not item.session:
        return []
    plan = item.session.plan()
    grouped: dict[tuple[str, str], Change] = {}
    for target in plan.targets:
        key = (target.pii_type.value, target.original.strip())
        change = grouped.get(key)
        if change is None:
            change = Change(
                pii_type=target.pii_type.value,
                original=target.original.strip(),
                pseudonym=target.replacement,
                occurrences=0,
            )
            grouped[key] = change
        change.occurrences += 1
        if target.page_no + 1 not in change.pages:
            change.pages.append(target.page_no + 1)
    return sorted(grouped.values(), key=lambda c: (c.pii_type, c.original))
