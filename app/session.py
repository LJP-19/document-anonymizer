"""Application session: one document under review.

Owned by the UI and by the CLI alike so that both drive exactly the same
pipeline (spec sections 33 and 47).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .decisions.manager import DecisionManager, DecisionState
from .detection.engine import analyse
from .detection.types import Candidate, DetectionResult, PiiType
from .document.model import Document
from .document.provider import DocumentTextProvider, default_provider
from .entities.registry import EntityRegistry
from .export.redactor import ApplyReport, export, render_original, render_page
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

    def analyse(self, use_ner: bool = True) -> DetectionResult:
        self.status = Status.ANALYZING
        self.document = self.provider.load(self.source_path)
        self.detection = analyse(self.document, use_ner=use_ner)
        self.decisions.register(self.detection.candidates)
        self.registry = EntityRegistry(scope=self.batch_scope or Path(self.source_path).name)
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
        return build_plan(
            self.source_path,
            self.detection,
            self.decisions,
            self.registry,
            ocr_required_pages=self.document.ocr_required_pages if self.document else [],
        )

    # -- preview -----------------------------------------------------------

    def preview_original(self, page_no: int, zoom: float = 1.5) -> bytes:
        return render_original(self.source_path, page_no, zoom)

    def preview_transformed(self, page_no: int, zoom: float = 1.5) -> bytes:
        return render_page(self.plan(), page_no, zoom)

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
        self.status = Status.VERIFIED if report.passed else Status.VERIFICATION_FAILED
        if self.document and self.document.ocr_required_pages:
            self.status = Status.VERIFICATION_FAILED if not report.passed else Status.PARTIALLY_ANALYZED
        return apply_report, report

    # -- review helpers ----------------------------------------------------

    @property
    def candidates(self) -> list[Candidate]:
        return self.detection.candidates if self.detection else []

    def needs_review(self) -> list[Candidate]:
        return [c for c in self.candidates if self.decisions.state(c) is DecisionState.UNREVIEWED]

    def reviewed(self) -> list[Candidate]:
        return [c for c in self.candidates if self.decisions.state(c) is not DecisionState.UNREVIEWED]

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
