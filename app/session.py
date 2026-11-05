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


def _as_pii_type(value):
    """Coerce anything the UI passes into a real PiiType.

    PiiType subclasses str, so a value round-tripping through Qt comes back as
    a plain string. Storing that produced candidates whose `pii_type` had no
    `.value`, crashing the review list the moment it grouped them.
    """
    from .detection.types import PiiType

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
class SearchHit:
    """One occurrence of searched text: where it is, and whether anything covers it."""

    page_no: int
    rect: tuple
    start: int
    end: int
    line_text: str
    candidate: Optional[object] = None
    status: str = "undetected"      # redacted | kept | protected | undetected
    pii_type: str = ""

    @property
    def context(self) -> str:
        left = self.line_text[max(0, self.start - 40):self.start].lstrip()
        right = self.line_text[self.end:self.end + 40].rstrip()
        prefix = "\u2026" if self.start > 40 else ""
        suffix = "\u2026" if len(self.line_text) > self.end + 40 else ""
        return f"{prefix}{left}[{self.line_text[self.start:self.end]}]{right}{suffix}"


class _Span:
    def __init__(self, start: int, end: int):
        self._s, self._e = start, end

    def start(self) -> int:
        return self._s

    def end(self) -> int:
        return self._e


def _matches(text: str, needle: str) -> list:
    """Occurrences of `needle` in `text`, ignoring case, accents, hyphens and
    apostrophes, as offsets into the ORIGINAL text (see detection/textnorm.py)."""
    from .detection.textnorm import find_occurrences

    return [_Span(s, e) for s, e in find_occurrences(text, needle)]


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
        if use_llm is None:
            # On whenever a downloaded model is ready (DOCANON_LLM=0 forces it
            # off, which the test suite and the CI end-to-end step rely on).
            # It costs time on every page it reviews. See llm_audit_enabled()
            # for why this is on now and what still guards its findings.
            from .detection.auditor import llm_audit_enabled

            use_llm = llm_audit_enabled()
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
            # The financial guard runs inside analyse()'s own pipeline
            # multiple times, but a second-pass finding is added AFTER
            # analyse() has already returned - it never passed through that
            # guard. This is the exact same class of leak the guard exists
            # to prevent (the auditor has been directly observed labelling
            # "$85,000" as a date of birth), just reached from a different
            # entry point: an unreviewed automated finding, potentially
            # from the LLM auditor itself, must never be exempt from a
            # hard rule that applies to every other detector's output.
            from .detection.engine import _drop_financial_values

            before = len(added)
            added = _drop_financial_values(added)
            dropped = before - len(added)
            if dropped:
                self.detection.warnings.append(
                    f"{dropped} second-pass finding(s) were a financial "
                    "amount or percentage and were discarded rather than "
                    "flagged for review - these are never redacted"
                )
        if added and self.detection.protection is not None:
            # The second pass re-scans the OUTPUT on a scratch document that has no
            # label context, so it sees untouched labels ("Member ID") as values.
            # Observed: 'Member ID' came back as an ADDRESS and 'Employee ID' as an
            # ADDRESS. Every path that adds candidates answers to the same gate.
            from .detection.protection import CandidateGate

            gated, gate_log = CandidateGate(self.detection.protection).run(added)
            self.detection.gate_log.extend(gate_log)
            refused = len(added) - len(gated)
            added = gated
            if refused:
                self.detection.warnings.append(
                    f"{refused} second-pass finding(s) were form text, labels or figures "
                    "and were discarded - these are never redacted"
                )
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
            document=self.document,
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
        """A filename that does not itself leak the client (spec sections 42-50).

        "Smith John 2025 1040.pdf" would undo the whole exercise the moment the
        file is emailed, so any detected value appearing in the name is
        replaced with the SAME pseudonym used inside the document - never a
        placeholder. A bare first or last name token now maps through the
        same per-token NameRegistry the document body uses, so "Lance" in a
        filename becomes whichever given name "Lance" became in the PDF
        (e.g. "Mark"), not the word "REDACTED": routing a name token to an
        empty replacement was the actual bug behind that placeholder - it
        discarded the per-token mapping this project already has for exactly
        this case and fell back to a generic word instead.

        A name-shaped filename token that never appears anywhere in the PDF
        body at all is also handled (spec section 44: filename PII need not
        occur inside the document) - it is run through the same name-shape
        heuristics used on document text, and if it looks like a name, it
        gets a pseudonym from the same registry, creating a new entity if one
        does not already exist, so it is at least consistently pseudonymized.
        """
        from pathlib import Path as _Path

        from .detection.heuristics import looks_like_a_lone_surname, looks_like_person
        from .detection.types import PiiType
        from .pseudonymization.names import NameRegistry

        stem = _Path(self.source_path).stem
        if not self.detection or not self.registry:
            return f"{stem}.anonymized.pdf"

        if self.registry.names is None:
            self.registry.names = NameRegistry(scope=self.registry.scope)
        names = self.registry.names

        replacements: list[tuple[str, str]] = []
        name_tokens: set[str] = set()
        for candidate in self.detection.candidates:
            if not self.decisions.is_actionable(candidate):
                continue
            original = candidate.normalized
            if len(original) < 3:
                continue
            if candidate.pii_type is PiiType.PERSON:
                replacements.append((original, names.pseudonym(original)))
                for token in original.split():
                    if len(token) >= 2 and token.isalpha():
                        name_tokens.add(token)
                        replacements.append((token, names.pseudonym(token)))
            else:
                replacements.append(
                    (original, self.registry.pseudonym_for(candidate.pii_type, original))
                )

        # Filename-only names: a token that looks like a name but was never a
        # document candidate at all still gets a stable pseudonym, rather
        # than being left in the clear or blanked.
        for raw_token in re.findall(r"[A-Za-z]{2,}", stem):
            if raw_token in name_tokens or raw_token.lower() in {
                "return", "form", "tax", "anonymized", "final", "draft", "copy",
                "pdf", "report", "statement", "document", "file", "files",
                "backup", "documents", "summary", "scan", "export", "print",
                "letter", "notes", "note", "email", "invoice", "receipt",
                "records", "record", "data", "info", "information", "revised",
                "updated", "old", "new", "archive", "signed", "unsigned",
                "review", "reviewed", "estimate", "worksheet", "attachment",
                "attachments", "exhibit", "packet", "package", "original",
            }:
                continue
            shaped, _confidence = looks_like_person(raw_token)
            if not shaped:
                shaped = looks_like_a_lone_surname(raw_token)
            if shaped:
                replacements.append((raw_token, names.pseudonym(raw_token)))

        safe = stem
        for original, pseudonym in sorted(replacements, key=lambda r: -len(r[0])):
            pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(original)}(?![A-Za-z0-9])", re.I)
            safe = pattern.sub(pseudonym or "Anonymized", safe)
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

    def search_occurrences(self, needle: str) -> list["SearchHit"]:
        """Every place `needle` appears in the document, and what is being done about it.

        Read-only. This is how a reviewer looks for what the detectors MISSED: each hit
        says whether something is already redacting it, whether it was found and kept,
        or whether nothing detected it at all.

        Matching ignores case, accents, hyphens and apostrophes (so "jose nunez" finds
        "José Núñez"), and a number matches however it is punctuated (123456789 finds
        123-45-6789).
        """
        from .detection.textnorm import find_occurrences

        needle = " ".join(needle.split()).strip()
        if not needle or self.document is None:
            return []
        digits = re.sub(r"\D", "", needle)
        numeric = len(digits) >= 5 and len(digits) >= 0.6 * len(re.sub(r"\s", "", needle))
        by_line: dict = {}
        for c in (self.detection.candidates if self.detection else []):
            by_line.setdefault(c.line.key(), []).append(c)

        protection = getattr(self.detection, "protection", None) if self.detection else None
        hits: list[SearchHit] = []
        for page in self.document.pages:
            for line in page.lines:
                spans = set(find_occurrences(line.text, needle))
                if numeric:
                    for m in re.finditer(r"\d[\d\-\s.]{3,}\d", line.text):
                        if re.sub(r"\D", "", m.group()) == digits:
                            spans.add((m.start(), m.end()))
                for start, end in sorted(spans):
                    rect = line.rect_for(start, end)
                    if rect is None:
                        continue
                    over = [c for c in by_line.get(line.key(), []) if c.start < end and start < c.end]
                    live = [c for c in over if self.decisions.is_actionable(c)]
                    chosen = (live or over or [None])[0]
                    status = "redacted" if live else ("kept" if over else "undetected")
                    label = chosen.pii_type.value if chosen is not None else ""
                    if status == "undetected" and protection is not None:
                        # Not a miss: the form's own label, a figure or a form number,
                        # which are kept on purpose. Say so, so it is not mistaken for one.
                        covering = [r for r in protection.regions_for(line) if r.overlaps(start, end)]
                        if covering:
                            status, label = "protected", covering[0].kind.value
                    hits.append(SearchHit(
                        page_no=page.number, rect=rect, start=start, end=end, line_text=line.text,
                        candidate=chosen, status=status, pii_type=label,
                    ))
        return sorted(hits, key=lambda h: (h.page_no, h.rect[1], h.rect[0]))

    def find_text(self, needle: str) -> list[tuple[int, tuple, str]]:
        """Every occurrence of `needle`, as (page, rect, line text)."""
        import re as _re

        results: list[tuple[int, tuple, str]] = []
        needle = " ".join(needle.split()).strip()
        if not needle or self.document is None:
            return results
        for page in self.document.pages:
            for line in page.lines:
                for match in _matches(line.text, needle):
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
        kind = _as_pii_type(pii_type)
        if pii_type is None and kind is PiiType.UNCLASSIFIED_GROUP_VALUE and self.registry is not None:
            # The caller didn't say what this is - most often "Search and
            # pseudonymize"/"Search and black out", used exactly for "the
            # detector missed this on another page". Defaulting straight to
            # UNCLASSIFIED_GROUP_VALUE means the registry key
            # (pii_type, normalized text) never matches the ORIGINAL
            # candidate's key, so a value that already has a pseudonym gets
            # an unrelated, inconsistent one from the generic generator
            # instead of reusing it - confirmed directly: "Wynn Shaffer" ->
            # "Patricia Lewis" on initial processing, then a same-session
            # search-and-add for the missed "Wynn" on another page produced
            # "Wynn" -> "Charles", not "Patricia". Not specific to names -
            # the same registry-key mismatch applies to any type. Check for
            # an exact match against a known value first (addresses, SSNs,
            # anything already registered under some other type), then fall
            # back to the name-token registry for a partial match like
            # "Wynn" alone against an already-known "Wynn Shaffer".
            normalized_needle = self.registry.normalize(needle)
            for record in self.registry.records:
                if record.key.normalized == normalized_needle:
                    kind = record.key.pii_type
                    break
            else:
                names = self.registry.names
                if names is not None and any(names.known(word) for word in needle.split()):
                    kind = PiiType.PERSON

        # Map, not a set: a value the detectors already found must be RETURNED
        # and updated, not silently skipped. Pressing Add on something already
        # in the list appeared to do nothing at all.
        existing = {
            (c.line.key(), c.start, c.end): c for c in self.detection.candidates
        }
        pages = self.document.pages if cascade else self.document.pages[:1]

        added: list = []
        for page in pages:
            for line in page.lines:
                for match in _matches(line.text, needle):
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
        if cascade and apply_to_same and added and kind is PiiType.PERSON:
            from .detection.textnorm import parse_person

            if parse_person(needle).is_full:
                added = added + self._entity_variants(added[0], needle, replacement)
        return added

    last_apply_message: str = ""

    def _adopt(self, created: list, ambiguous: list) -> None:
        """Add newly found occurrences, removing the fragments they supersede."""
        from .entities.identity import fragments_inside

        superseded = {c.id for c in fragments_inside(created, self.detection.candidates)}
        if superseded:
            self.detection.candidates = [c for c in self.detection.candidates if c.id not in superseded]
        for group in (created, ambiguous):
            if group:
                self.detection.candidates.extend(group)
                self.decisions.register(group)

    def _entity_variants(self, seed, needle: str, replacement: Optional[str]) -> list:
        """After a manual add: the same person written another way ("Smith, John",
        "John R. Smith") is found too, with evidence, and gated like everything else."""
        from .entities.identity import EntityIndex, compose_replacement, rescan_entity

        index = EntityIndex.build(self.detection.candidates)
        profile = index.entity_of(seed)
        if profile is None:
            return []
        created, ambiguous = rescan_entity(
            self.document, profile, self.detection.candidates, self.detection.protection, [needle]
        )
        self._adopt(created, ambiguous)
        for c in created:
            if replacement:
                self.decisions.edit([c], compose_replacement(replacement, c.text))
            self.decisions.mark_reviewed([c], True)
        pages = {c.page_no for c in created}
        self.last_apply_message = (
            f"Also found {len(created)} other form(s) of this person across {len(pages)} page(s)."
            + (f" {len(ambiguous)} ambiguous short-form occurrence(s) were left for review." if ambiguous else "")
        ) if (created or ambiguous) else ""
        return created

    def apply_to_entity(
        self, candidate, state, replacement: Optional[str] = None,
        explicit: tuple = (), also: Optional[list] = None,
    ):
        """Apply a decision to everything that is the same identity, and find
        occurrences of it that never became candidates. Returns an ApplyResult."""
        from .entities.identity import ApplyResult, EntityIndex, compose_replacement, rescan_entity

        index = EntityIndex.build(self.detection.candidates)
        profile = index.entity_of(candidate)
        created: list = []
        ambiguous: list = []
        if state is not DecisionState.SKIPPED and profile is not None and self.document is not None:
            created, ambiguous = rescan_entity(
                self.document, profile, self.detection.candidates, self.detection.protection, explicit
            )
            self._adopt(created, ambiguous)
        applied = self.decisions.apply_to_entity(
            self.detection.candidates, candidate, state, index=index, replacement=replacement,
            extra=list(also or []) + created,
        )
        if state is DecisionState.EDITED and replacement and self.registry is not None:
            for text in {c.text for c in applied}:
                self.registry.override(candidate.pii_type, text, compose_replacement(replacement, text, profile.observed() if profile else ()))
        self.decisions.mark_reviewed(applied, True)
        result = ApplyResult(
            applied=applied, created=created, ambiguous=ambiguous,
            pages={c.page_no for c in applied}, entity=profile,
        )
        self.last_apply_message = result.message
        return result

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
