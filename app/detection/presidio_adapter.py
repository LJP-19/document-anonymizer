"""Presidio as an additional proposal layer (spec sections 18-19, 37-39).

Architectural guardrails this module holds to:

  1. PyMuPDF is the only PDF backbone. This module never touches a PDF file or
     a coordinate system of its own - it runs on `Line.text` and converts a
     result back to page geometry with the SAME `Line.rect_for()` every other
     detector uses. No pdfplumber, no second coordinate space to reconcile.

  2. `groups.py` is untouched. This module reads its OUTPUT (LabelRegion,
     LogicalFieldGroup) to build context for the enhancer; it does not
     duplicate or replace the vertical-stack / compound-header parser there.

  3. Presidio only PROPOSES. A raw AnalyzerEngine result becomes a Candidate
     with `needs_review=True` and a low starting confidence - the same
     contract GLiNER's spans already have. It is the deterministic layers,
     the label-overlap strip, and the form-text veto that decide what
     actually gets typed and transformed, exactly as they do for every other
     detector. This module adds a proposer; it does not add an approver.

  4. `en_core_web_sm` only. Presidio's NlpEngine is pointed at the SAME small
     model the rest of the app already loads for `detect_ner` - never
     `en_core_web_lg`. The engine also REUSES that already-loaded spaCy
     pipeline object rather than loading a second copy, so this layer costs
     no additional model weight or startup time beyond the packages
     themselves.

  5. Context from `groups.py`. Presidio's `LemmaContextAwareEnhancer` boosts a
     recognizer's confidence when a context word it declares appears near the
     matched text. The context passed per line is built directly from the
     LabelRegion bound to that line (via LogicalFieldGroup.value_lines) - the
     same label text `groups.py` already extracted, not a second label parse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from ..document.model import Document, Line
from .types import Candidate, Evidence, LabelRegion, LogicalFieldGroup, PiiType, Source

log = logging.getLogger(__name__)

#: Presidio's built-in entity names -> our taxonomy. Anything not listed here
#: is proposed as UNCLASSIFIED_GROUP_VALUE, so type-or-skip holds: an entity
#: Presidio names that we do not have a mapped type for is never silently
#: given a PERSON- or ADDRESS-shaped replacement it may not deserve.
ENTITY_MAP: dict[str, PiiType] = {
    "PERSON": PiiType.PERSON,
    "EMAIL_ADDRESS": PiiType.EMAIL,
    "PHONE_NUMBER": PiiType.PHONE,
    "LOCATION": PiiType.ADDRESS,
    "US_SSN": PiiType.SSN,
    "US_ITIN": PiiType.ITIN,
    "US_BANK_NUMBER": PiiType.BANK_ACCOUNT,
    "US_DRIVER_LICENSE": PiiType.DRIVERS_LICENSE,
    "US_PASSPORT": PiiType.PASSPORT,
    "IBAN_CODE": PiiType.IBAN,
    "CREDIT_CARD": PiiType.CARD_NUMBER,
    "DATE_TIME": PiiType.PERSONAL_DATE,
    "NRP": PiiType.CITIZENSHIP,
}

#: Below this score a proposal is discarded outright rather than surfaced -
#: matches GLiNER's threshold contract so both proposers behave consistently.
MIN_SCORE = 0.35

#: Context words attached to our own custom recognizers, and to the enhancer.
#: These are the LEMMAS LemmaContextAwareEnhancer compares against nearby
#: label text, not a display list.
SSN_CONTEXT = ["ssn", "social", "security", "taxpayer", "number"]
EIN_CONTEXT = ["ein", "employer", "identification", "federal", "number"]
ITIN_CONTEXT = ["itin", "individual", "taxpayer", "identification", "number"]


class PresidioUnavailable(RuntimeError):
    """presidio-analyzer, or its spaCy backend, is not installed."""


@dataclass
class PresidioProposal:
    """One raw Presidio result, still in TEXT-STRING terms, not yet a Candidate."""

    entity_type: str
    start: int
    end: int
    score: float


@dataclass
class LineContext:
    """The label context for one line, built from groups.py's own output."""

    label_text: str = ""
    words: list[str] = field(default_factory=list)


def build_line_context(groups: list[LogicalFieldGroup]) -> dict[tuple, LineContext]:
    """One context entry per value line, keyed exactly like `Line.key()`.

    Built entirely from what groups.py already computed - the label bound to
    each stacked value line - so this is reading its output, not re-deriving
    labels from scratch.
    """
    context: dict[tuple, LineContext] = {}
    for group in groups:
        if group.label.non_pii:
            continue
        label_words = [w.strip(".,:;()").lower() for w in group.label.text.split()]
        label_words = [w for w in label_words if w]
        entry = LineContext(label_text=group.label.text, words=label_words)
        # The label's own line gets its own words as context too, which
        # matters for an inline label+value line ("Taxpayer SSN 123456789").
        context.setdefault(group.label.line.key(), entry)
        for line in group.value_lines:
            context.setdefault(line.key(), entry)
    return context


class PresidioDetector:
    """Lazy-loaded, cached. One instance is reused for the whole document."""

    def __init__(self) -> None:
        self._engine = None
        self._error: Optional[str] = None

    def load(self, spacy_nlp=None) -> None:
        """Build the AnalyzerEngine once.

        `spacy_nlp` is the ALREADY-LOADED spaCy pipeline `detect_ner` uses -
        passing it in means Presidio never loads a second copy of the model.
        """
        if self._engine is not None:
            return
        if self._error is not None:
            raise PresidioUnavailable(self._error)

        try:
            from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
            from presidio_analyzer.context_aware_enhancers import (
                LemmaContextAwareEnhancer,
            )
            from presidio_analyzer.nlp_engine import NlpArtifacts, NlpEngine
        except ImportError as exc:
            self._error = (
                f"presidio-analyzer is not installed ({exc}). Install it with: "
                "pip install -r requirements-presidio.txt"
            )
            raise PresidioUnavailable(self._error) from None

        try:
            nlp_engine = _build_reused_nlp_engine(spacy_nlp) if spacy_nlp is not None else None
            if nlp_engine is None:
                from presidio_analyzer.nlp_engine import NlpEngineProvider

                # en_core_web_sm ONLY - never lg. If detect_ner's model was not
                # passed in, load the small model directly rather than letting
                # Presidio's provider default to something larger.
                nlp_engine = NlpEngineProvider(
                    nlp_configuration={
                        "nlp_engine_name": "spacy",
                        "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
                    }
                ).create_engine()

            engine = AnalyzerEngine(
                nlp_engine=nlp_engine,
                context_aware_enhancer=LemmaContextAwareEnhancer(
                    context_similarity_factor=0.45,
                    min_score_with_context_similarity=0.4,
                ),
                supported_languages=["en"],
            )
            for pattern, name, context in (
                (r"(?<!\d)\d{9}(?!\d)", "SSN_FLEXIBLE_NO_DASH", SSN_CONTEXT),
                (r"(?<!\d)\d{2}-\d{7}(?!\d)", "EIN_FLEXIBLE", EIN_CONTEXT),
                (r"(?<!\d)9\d{2}-\d{2}-\d{4}(?!\d)", "ITIN_FLEXIBLE", ITIN_CONTEXT),
            ):
                recognizer = PatternRecognizer(
                    supported_entity=(
                        "US_SSN" if "SSN" in name else "US_ITIN" if "ITIN" in name else "US_EIN"
                    ),
                    name=name,
                    patterns=[Pattern(name=name, regex=pattern, score=0.55)],
                    context=context,
                )
                engine.registry.add_recognizer(recognizer)
                ENTITY_MAP.setdefault(recognizer.supported_entities[0], PiiType.SSN)
        except Exception as exc:  # noqa: BLE001 - never let setup crash analysis
            self._error = f"{type(exc).__name__}: {exc}"
            raise PresidioUnavailable(self._error) from None

        ENTITY_MAP["US_EIN"] = PiiType.EIN
        self._engine = engine

    def analyze_line(self, text: str, context_words: list[str]) -> list[PresidioProposal]:
        if self._engine is None:
            return []
        try:
            results = self._engine.analyze(
                text=text, language="en", context=context_words or None
            )
        except Exception as exc:  # noqa: BLE001 - one bad line must not stop analysis
            log.debug("Presidio failed on a line: %s", type(exc).__name__)
            return []
        return [
            PresidioProposal(r.entity_type, r.start, r.end, r.score)
            for r in results
            if r.score >= MIN_SCORE
        ]


def _build_reused_nlp_engine(nlp):
    """Wrap an already-loaded spaCy Language object as a full Presidio
    NlpEngine, so the small model is loaded exactly once for the application.

    NlpEngine is an ABC requiring get_supported_entities, is_loaded, and
    process_batch as well as process_text/is_stopword/is_punct - verified
    against the installed presidio-analyzer's actual abstract interface
    rather than assumed, since a subclass missing any of these raises
    TypeError the moment it is instantiated.
    """
    from presidio_analyzer.nlp_engine import NlpArtifacts, NlpEngine

    class ReusedSpacyNlpEngine(NlpEngine):
        def __init__(self, spacy_nlp) -> None:
            self._nlp = spacy_nlp

        def load(self) -> None:  # already loaded
            return

        def is_loaded(self) -> bool:
            return True

        def process_text(self, text: str, language: str) -> NlpArtifacts:
            doc = self._nlp(text)
            return NlpArtifacts(
                entities=list(doc.ents),
                tokens=doc,
                tokens_indices=[t.idx for t in doc],
                lemmas=[t.lemma_ for t in doc],
                nlp_engine=self,
                language=language,
            )

        def process_batch(self, texts, language: str, batch_size: int = 1, n_process: int = 1, **kwargs):
            for text in texts:
                yield text, self.process_text(text, language)

        def is_stopword(self, word: str, language: str) -> bool:
            return self._nlp.vocab[word].is_stop

        def is_punct(self, word: str, language: str) -> bool:
            return self._nlp.vocab[word].is_punct

        def get_supported_entities(self) -> list[str]:
            return list(ENTITY_MAP.keys())

        def get_supported_languages(self) -> list[str]:
            return ["en"]

    return ReusedSpacyNlpEngine(nlp)


_detector: Optional[PresidioDetector] = None


def _get_detector() -> PresidioDetector:
    global _detector
    if _detector is None:
        _detector = PresidioDetector()
    return _detector


def _labelled_line_keys(groups: list[LogicalFieldGroup]) -> set:
    keys = set()
    for group in groups:
        keys.add(group.label.line.key())
        keys.update(line.key() for line in group.value_lines)
    return keys


def _analyze_unstructured_blocks(
    doc: Document, detector: "PresidioDetector", labelled: set
) -> list[Candidate]:
    """Presidio on FULL PARAGRAPH text, for prose with no field binding.

    A single line gives the NLP model almost no sentence context. A narrative
    paragraph - a client letter, a preparer's notes, a cover memo - benefits
    from the whole block at once, the way a human reading it would use the
    surrounding sentence to tell a name from a common word. This runs ONLY
    for blocks with no label-bound line in them at all; a form field still
    goes through the per-line pass above, unchanged.
    """
    out: list[Candidate] = []
    for page in doc.pages:
        for block in page.blocks:
            if not block.lines or any(line.key() in labelled for line in block.lines):
                continue
            text = block.text
            if len(text.strip()) < 20:
                continue
            proposals = detector.analyze_line(text, [])
            for proposal in proposals:
                # A match spanning a line break ("Marisol\nEtxeberria") is
                # split into one candidate per line it touches - a Candidate
                # is anchored to exactly one Line, so a name Presidio only
                # found by reading across the break still needs one rect per
                # physical line it actually occupies on the page.
                for line, local_start, local_end in _split_across_lines(
                    block, proposal.start, proposal.end
                ):
                    rect = line.rect_for(local_start, local_end)
                    if rect is None:
                        continue
                    pii_type = ENTITY_MAP.get(proposal.entity_type, PiiType.UNCLASSIFIED_GROUP_VALUE)
                    out.append(
                        Candidate(
                            pii_type=pii_type,
                            text=line.text[local_start:local_end],
                            page_no=page.number,
                            rect=rect,
                            line=line,
                            start=local_start,
                            end=local_end,
                            confidence=min(0.6, proposal.score),
                            source=Source.NER,
                            evidence=[
                                Evidence(
                                    Source.NER,
                                    f"Presidio (paragraph context): {proposal.entity_type}",
                                    proposal.score,
                                )
                            ],
                            needs_review=proposal.score < 0.6,
                            review_reason="" if proposal.score >= 0.6 else "low Presidio confidence",
                        )
                    )
    return out


def _split_across_lines(block, block_start: int, block_end: int):
    """Map an offset span in `block.text` (lines joined by "\n") back to one
    or more (Line, local_start, local_end) pieces - one per physical line the
    span actually touches. A match Presidio only found by reading across a
    line break still has to be redacted on each line it occupies; there is no
    single Line object spanning two rows.
    """
    pieces = []
    cursor = 0
    for line in block.lines:
        line_len = len(line.text)
        line_end = cursor + line_len
        overlap_start = max(block_start, cursor)
        overlap_end = min(block_end, line_end)
        if overlap_end > overlap_start:
            pieces.append((line, overlap_start - cursor, overlap_end - cursor))
        cursor = line_end + 1  # +1 for the joining "\n"
        if cursor > block_end:
            break
    return pieces


def detect_presidio(
    doc: Document,
    groups: list[LogicalFieldGroup],
    spacy_nlp=None,
    detector: Optional[PresidioDetector] = None,
    unstructured: bool = True,
) -> tuple[list[Candidate], list[str]]:
    """Propose candidates from Presidio. Never types or decides on its own.

    Every result becomes a low-confidence, needs_review candidate - the same
    contract GLiNER's output has. Confirmation and typing happen downstream,
    exactly as for every other proposer in this pipeline.
    """
    detector = detector or _get_detector()
    warnings: list[str] = []
    try:
        detector.load(spacy_nlp)
    except PresidioUnavailable as exc:
        warnings.append(
            f"Presidio layer disabled: {exc} Detection continues on rules, "
            "layout, spaCy and GLiNER without it."
        )
        return [], warnings

    context_by_line = build_line_context(groups)
    candidates: list[Candidate] = []

    for page in doc.pages:
        for line in page.lines:
            if not line.text.strip():
                continue
            context = context_by_line.get(line.key())
            proposals = detector.analyze_line(line.text, context.words if context else [])
            for proposal in proposals:
                rect = line.rect_for(proposal.start, proposal.end)
                if rect is None:
                    continue
                pii_type = ENTITY_MAP.get(proposal.entity_type, PiiType.UNCLASSIFIED_GROUP_VALUE)
                candidates.append(
                    Candidate(
                        pii_type=pii_type,
                        text=line.text[proposal.start:proposal.end],
                        page_no=page.number,
                        rect=rect,
                        line=line,
                        start=proposal.start,
                        end=proposal.end,
                        confidence=min(0.6, proposal.score),
                        source=Source.NER,
                        evidence=[
                            Evidence(
                                Source.NER,
                                f"Presidio: {proposal.entity_type} (context-boosted)"
                                if context and context.words
                                else f"Presidio: {proposal.entity_type}",
                                proposal.score,
                            )
                        ],
                        needs_review=proposal.score < 0.6,
                        review_reason="" if proposal.score >= 0.6 else "low Presidio confidence",
                    )
                )
    if unstructured:
        candidates.extend(
            _analyze_unstructured_blocks(doc, detector, _labelled_line_keys(groups))
        )
    return candidates, warnings
