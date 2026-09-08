"""Application session: one document under review.

Owned by the UI and by the CLI alike so that both drive exactly the same
pipeline (spec sections 33 and 47).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .decisions.manager import DecisionManager, DecisionState
from .detection.engine import analyse
from .detection.types import Candidate, DetectionResult, PiiType
from .document.model import Document
from .document.hidden import HiddenContent, extract_hidden
from .document.provider import DocumentTextProvider, default_provider
from .entities.registry import EntityRegistry
from .entities.roster import ClientRoster, mapping_path_for, write_mapping_workbook
from .export.redactor import (
    ApplyReport,
    export,
    render_original,
    render_originals,
    render_page,
    render_pages,
)
from .transform.plan import TransformationPlan, build_plan
from .verification.verifier import VerificationReport, verify


class Status:
    IDLE = "IDLE"
    ANALYZING = "ANALYZING"
    NEEDS_REVIEW = "NEEDS REVIEW"
    READY = "READY"
    PROCESSING = "PROCESSING"
    VERIFYING = "VERIFYING"
    VERIFIED = "EXPORT VERIFIED"
    PARTIALLY_ANALYZED = "PARTIALLY ANALYZED"
    OCR_REQUIRED = "OCR REQUIRED"
    VERIFICATION_FAILED = "VERIFICATION FAILED"
    EXPORT_FAILED = "EXPORT FAILED"


@dataclass
class AnonymizationSession:
    source_path: str
    provider: DocumentTextProvider = field(default_factory=default_provider)
    document: Optional[Document] = None
    detection: Optional[DetectionResult] = None
    decisions: DecisionManager = field(default_factory=DecisionManager)
    registry: Optional[EntityRegistry] = None
    status: str = Status.IDLE
    batch_scope: Optional[str] = None
    roster: Optional[ClientRoster] = None
    roster_path: Optional[str] = None
    mapping_file: Optional[str] = None
    hidden: Optional[HiddenContent] = None
    mapping_folder: Optional[str] = None

    def analyse(self, use_ner: bool = True, use_llm: Optional[bool] = None) -> DetectionResult:
        import os

        if use_llm is None:
            # The audit pass costs ~30-60s per uncertain page. Off by default in
            # tests and any environment that opts out.
            use_llm = os.environ.get("DOCANON_LLM", "1") not in ("0", "false", "no")
        self.status = Status.ANALYZING
        self.document = self.provider.load(self.source_path)
        # Metadata, annotations, attachments and bookmarks carry identity that
        # page redaction never touches (spec section 42).
        self.hidden = extract_hidden(self.source_path)
        self.detection = analyse(self.document, use_ner=use_ner, use_llm=use_llm)
        if self.hidden:
            self.detection.warnings.append(
                f"{len(self.hidden.items)} item(s) of hidden content found "
                "(document properties, annotations, attachments or bookmarks); "
                "these are stripped or rewritten on export"
            )
        self.decisions.register(self.detection.candidates)
        if self.roster is None and self.roster_path:
            self.roster = ClientRoster.load(Path(self.roster_path))
        self.registry = EntityRegistry(
            scope=self.batch_scope or Path(self.source_path).name,
            roster=self.roster,
            document_name=Path(self.source_path).name,
        )
        self.status = self._derive_status()
        return self.detection

    def _derive_status(self) -> str:
        if self.document and self.document.ocr_required_pages:
            return Status.OCR_REQUIRED
        if self.detection and any(
            c.needs_review and self.decisions.state(c) is not DecisionState.SKIPPED
            for c in self.detection.candidates
        ):
            return Status.NEEDS_REVIEW
        return Status.READY

    # -- plan --------------------------------------------------------------

    def plan(self) -> TransformationPlan:
        if self.detection is None or self.registry is None:
            raise RuntimeError("analyse() must run before a plan can be built")
        plan = build_plan(
            self.source_path,
            self.detection,
            self.decisions,
            self.registry,
            ocr_required_pages=self.document.ocr_required_pages if self.document else [],
        )
        if self.hidden:
            plan.hidden_items = list(self.hidden.items)
        return plan

    # -- preview -----------------------------------------------------------

    def preview_original(self, page_no: int, zoom: float = 1.5) -> bytes:
        return render_original(self.source_path, page_no, zoom)

    def preview_transformed(self, page_no: int, zoom: float = 1.5) -> bytes:
        return render_page(self.plan(), page_no, zoom)

    def preview_originals(self, pages: list[int], zoom: float = 1.0) -> dict[int, bytes]:
        return render_originals(self.source_path, pages, zoom)

    def preview_transformed_pages(self, pages: list[int], zoom: float = 1.0) -> dict[int, bytes]:
        return render_pages(self.plan(), pages, zoom)

    def safe_output_name(self) -> str:
        """A filename that does not itself leak the client (spec section 3).

        "Smith John 2025 1040.pdf" would undo the whole exercise the moment the
        file is emailed, so any detected value appearing in the name is replaced
        with the same pseudonym used inside the document.
        """
        from pathlib import Path as _Path

        stem = _Path(self.source_path).stem
        if not self.detection or not self.registry:
            return f"{stem}.anonymized.pdf"

        replacements: list[tuple[str, str]] = []
        for candidate in self.detection.candidates:
            if not self.decisions.is_actionable(candidate):
                continue
            original = candidate.normalized
            if len(original) < 3:
                continue
            replacements.append(
                (original, self.registry.pseudonym_for(candidate.pii_type, original))
            )
            # Individual words of a name appear in filenames far more often than
            # the full string does.
            for token in original.split():
                if len(token) >= 3 and token.isalpha():
                    replacements.append((token, ""))

        safe = stem
        for original, pseudonym in sorted(replacements, key=lambda r: -len(r[0])):
            pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(original)}(?![A-Za-z0-9])", re.I)
            safe = pattern.sub(pseudonym or "REDACTED", safe)
        safe = re.sub(r"[_\s-]{2,}", "-", safe).strip("-_ ") or "document"
        return f"{safe}.anonymized.pdf"

    # -- export + verify ---------------------------------------------------

    def process(self, output_path: str) -> tuple[ApplyReport, VerificationReport]:
        plan = self.plan()
        self.status = Status.PROCESSING
        try:
            apply_report = export(plan, output_path)
        except Exception:
            self.status = Status.EXPORT_FAILED
            raise
        self.status = Status.VERIFYING
        report = verify(plan, output_path)
        self.mapping_file = self.write_mapping(output_path)
        self.status = Status.VERIFIED if report.passed else Status.VERIFICATION_FAILED
        if self.document and self.document.ocr_required_pages:
            self.status = Status.VERIFICATION_FAILED if not report.passed else Status.PARTIALLY_ANALYZED
        return apply_report, report

    def write_mapping(self, output_path: str) -> Optional[str]:
        """Write the pseudonym-to-original workbook, and persist the roster."""
        if self.registry is None:
            return None
        from .entities.roster import ROSTER_TYPES, RosterEntry

        entries = [
            RosterEntry(
                pii_type=record.key.pii_type,
                original=record.original,
                pseudonym=record.pseudonym,
                first_seen_in=Path(self.source_path).name,
            )
            for record in self.registry.records
            if record.key.pii_type in ROSTER_TYPES
        ]
        if not entries:
            return None
        path = mapping_path_for(
            Path(output_path), Path(self.mapping_folder) if self.mapping_folder else None
        )
        try:
            write_mapping_workbook(sorted(entries, key=lambda e: (e.pii_type.value, e.original)), path)
        except Exception:
            return None
        if self.roster is not None and self.roster_path:
            try:
                self.roster.save(Path(self.roster_path))
            except Exception:
                pass
        return str(path)

    # -- review helpers ----------------------------------------------------

    @property
    def candidates(self) -> list[Candidate]:
        return self.detection.candidates if self.detection else []

    def needs_review(self) -> list[Candidate]:
        return [c for c in self.candidates if self.decisions.state(c) is DecisionState.UNREVIEWED]

    def reviewed(self) -> list[Candidate]:
        return [c for c in self.candidates if self.decisions.state(c) is not DecisionState.UNREVIEWED]

    def add_manual_text(
        self,
        text: str,
        replacement: Optional[str] = None,
        pii_type: Optional[object] = None,
        cascade: bool = True,
        apply_to_same: bool = True,
    ) -> list:
        """Add a value the detectors missed, by text rather than by region.

        cascade        - find it on every page, not only where it was first seen
        apply_to_same  - treat every occurrence as one entity with one pseudonym
        """
        import re as _re

        from .detection.types import Candidate, Evidence, PiiType, Source

        needle = " ".join(text.split()).strip()
        if not needle or self.document is None or self.detection is None:
            return []
        kind = pii_type or PiiType.UNCLASSIFIED_GROUP_VALUE

        existing = {(c.line.key(), c.start, c.end) for c in self.detection.candidates}
        pattern = _re.compile(
            rf"(?<![A-Za-z0-9]){_re.escape(needle)}(?![A-Za-z0-9])", _re.IGNORECASE
        )
        pages = self.document.pages if cascade else self.document.pages[:1]

        added: list = []
        for page in pages:
            for line in page.lines:
                for match in pattern.finditer(line.text):
                    key = (line.key(), match.start(), match.end())
                    if key in existing:
                        continue
                    rect = line.rect_for(match.start(), match.end())
                    if rect is None:
                        continue
                    candidate = Candidate(
                        pii_type=kind,
                        text=line.text[match.start():match.end()],
                        page_no=page.number,
                        rect=rect,
                        line=line,
                        start=match.start(),
                        end=match.end(),
                        confidence=1.0,
                        source=Source.MANUAL,
                        evidence=[Evidence(Source.MANUAL, "added by the user", 1.0)],
                    )
                    self.detection.candidates.append(candidate)
                    self.decisions.add_manual(candidate)
                    self.decisions.mark_reviewed([candidate], True)
                    added.append(candidate)
                    existing.add(key)
                    if not apply_to_same:
                        break
                if added and not apply_to_same:
                    break
            if added and not apply_to_same:
                break

        if replacement and added and self.registry is not None:
            self.registry.override(kind, added[0].normalized, replacement)
        return added

    def add_manual(
        self, page_no: int, rect, pii_type: PiiType = PiiType.UNCLASSIFIED_GROUP_VALUE
    ) -> Optional[Candidate]:
        """Manual PII goes through the identical pipeline (spec section 29)."""
        from .detection.types import Evidence, Source

        if self.document is None:
            return None
        page = self.document.pages[page_no]
        for line in page.lines:
            if not (line.bbox[2] < rect[0] or rect[2] < line.bbox[0] or line.bbox[3] < rect[1] or rect[3] < line.bbox[1]):
                start, end = 0, len(line.text.rstrip())
                candidate = Candidate(
                    pii_type=pii_type,
                    text=line.text[start:end],
                    page_no=page_no,
                    rect=line.rect_for(start, end) or tuple(rect),
                    line=line,
                    start=start,
                    end=end,
                    confidence=1.0,
                    source=Source.MANUAL,
                    evidence=[Evidence(Source.MANUAL, "user-selected region", 1.0)],
                )
                self.detection.candidates.append(candidate)
                self.decisions.add_manual(candidate)
                return candidate
        return None
