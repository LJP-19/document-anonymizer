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


def second_pass_enabled() -> bool:
    """On by default. DOCANON_SECOND_PASS=0 turns it off for speed."""
    import os

    return os.environ.get("DOCANON_SECOND_PASS", "1") not in ("0", "false", "no")


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
        # Second pass: build the transformed document and look for anything
        # still identifying. Findings join the review list; nothing is applied.
        if second_pass_enabled():
            self.run_second_pass()

        self.status = self._derive_status()
        return self.detection

    def run_second_pass(self) -> int:
        """Re-scan the planned output. Returns the number of new findings."""
        from .detection.second_pass import second_pass

        if self.detection is None or self.document is None or self.registry is None:
            return 0
        try:
            result = second_pass(self.document, self.plan())
        except Exception as exc:  # noqa: BLE001 - advisory, never fatal
            self.detection.warnings.append(
                f"second pass failed: {type(exc).__name__}: {exc}"
            )
            return 0

        self.detection.warnings.extend(result.warnings)

        # Check the replacements themselves, not only what survived.
        from .detection.second_pass import check_replacements

        faults = check_replacements(self.plan())
        if faults:
            self.detection.warnings.append(
                f"{len(faults)} replacement(s) look wrong: "
                + "; ".join(f"{o!r} -> {r!r} ({why})" for o, r, why in faults[:3])
                + ("; ..." if len(faults) > 3 else "")
            )
        existing = {(c.page_no, c.rect) for c in self.detection.candidates}
        added = [f for f in result.findings if (f.page_no, f.rect) not in existing]
        if added:
            self.detection.candidates.extend(added)
            self.decisions.register(added)
            self.detection.warnings.append(
                f"the review pass found {len(added)} further value(s) still "
                "readable in the output; they are in the review list"
            )
        return len(added)

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

    def retype(self, candidates: list, pii_type, replacement: Optional[str] = None) -> str:
        """Change what an item IS, and give it a fitting replacement.

        Editing only the replacement text left the wrong category behind, so the
        next occurrence of the same value was pseudonymised as the wrong kind of
        thing. Changing the type regenerates the pseudonym unless the user
        supplies their own.
        """
        from .pseudonymization.generator import generate

        if not candidates:
            return ""
        for candidate in candidates:
            candidate.pii_type = pii_type
        original = candidates[0].normalized
        if not replacement:
            replacement = generate(pii_type, original, scope=self.registry.scope if self.registry else "document")
        if self.registry is not None:
            self.registry.override(pii_type, original, replacement)
        self.decisions.edit(candidates, replacement)
        self.decisions.mark_reviewed(candidates, True)
        return replacement

    def add_manual_region(self, page_no: int, rect, label: str = "") -> Optional[object]:
        """Black out a drawn rectangle.

        For content a pseudonym cannot represent - a signature, a logo, a
        photograph, a handwritten note. The area is permanently covered, not
        substituted, so no text is inserted and none is expected back.
        """
        from .detection.types import Candidate, Evidence, PiiType, Source

        if self.document is None or self.detection is None:
            return None
        if not (0 <= page_no < self.document.page_count):
            return None
        x0, y0, x1, y1 = rect
        if x1 - x0 < 2 or y1 - y0 < 2:
            return None

        page = self.document.pages[page_no]
        anchor = page.lines[0] if page.lines else None
        if anchor is None:
            return None

        candidate = Candidate(
            pii_type=PiiType.UNCLASSIFIED_GROUP_VALUE,
            text=label or "drawn area",
            page_no=page_no,
            rect=(float(x0), float(y0), float(x1), float(y1)),
            line=anchor,
            start=0,
            end=0,
            confidence=1.0,
            source=Source.MANUAL,
            evidence=[Evidence(Source.MANUAL, "area drawn by the user", 1.0)],
            blackout=True,
        )
        self.detection.candidates.append(candidate)
        self.decisions.add_manual(candidate)
        self.decisions.mark_reviewed([candidate], True)
        return candidate

    def find_text(self, needle: str) -> list[tuple[int, tuple, str]]:
        """Every occurrence of `needle`, as (page, rect, line text)."""
        import re as _re

        results: list[tuple[int, tuple, str]] = []
        needle = " ".join(needle.split()).strip()
        if not needle or self.document is None:
            return results
        pattern = _re.compile(
            rf"(?<![A-Za-z0-9]){_re.escape(needle)}(?![A-Za-z0-9])", _re.IGNORECASE
        )
        for page in self.document.pages:
            for line in page.lines:
                for match in pattern.finditer(line.text):
                    rect = line.rect_for(match.start(), match.end())
                    if rect:
                        results.append((page.number, rect, line.text))
        return results

    def add_manual_text(
        self,
        text: str,
        replacement: Optional[str] = None,
        pii_type: Optional[object] = None,
        cascade: bool = True,
        apply_to_same: bool = True,
        blackout: bool = False,
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

        # Map, not a set: a value the detectors already found must be RETURNED
        # and updated, not silently skipped. Pressing Add on something already
        # in the list appeared to do nothing at all.
        existing = {
            (c.line.key(), c.start, c.end): c for c in self.detection.candidates
        }
        pattern = _re.compile(
            rf"(?<![A-Za-z0-9]){_re.escape(needle)}(?![A-Za-z0-9])", _re.IGNORECASE
        )
        pages = self.document.pages if cascade else self.document.pages[:1]

        added: list = []
        for page in pages:
            for line in page.lines:
                for match in pattern.finditer(line.text):
                    key = (line.key(), match.start(), match.end())
                    known = existing.get(key)
                    if known is not None:
                        # Adopt it: apply the chosen type and treat it as decided.
                        if pii_type is not None:
                            known.pii_type = kind
                        known.blackout = blackout or known.blackout
                        self.decisions.set_state([known], DecisionState.ACCEPTED)
                        self.decisions.mark_reviewed([known], True)
                        added.append(known)
                        if not apply_to_same:
                            break
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
                        blackout=blackout,
                    )
                    self.detection.candidates.append(candidate)
                    self.decisions.add_manual(candidate)
                    self.decisions.mark_reviewed([candidate], True)
                    added.append(candidate)
                    existing[key] = candidate
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
