"""Local NER (spec section 10). No network, no API - a bundled spaCy model only.

Runs on block text rather than line text so that sentences spanning several
visual lines are handled (spec section 20). Entity offsets are mapped back to
per-line rectangles through the block offset map.
"""

from __future__ import annotations

import logging
from functools import lru_cache
import re
from typing import Optional

from ..document.model import Document
from .types import Candidate, Evidence, PiiType, Source

log = logging.getLogger(__name__)

#: en_core_web_trf - the ONLY spaCy English model this project ships in the
#: production build, on direct and repeated instruction. Loaded once at
#: process start (~5s on CPU, measured) and reused for the whole document;
#: never reloaded per page. Real, measured installed cost of the runtime
#: this requires (torch CPU wheel + spacy-transformers + the model itself):
#: roughly 1.8 GB, on top of everything else this application bundles. That
#: cost is accepted deliberately, not overlooked - en_core_web_sm and
#: en_core_web_lg are NEVER bundled in the shipped application.
#:
#: DOCANON_SPACY_MODEL overrides this for development/testing on a machine
#: where the full torch runtime is impractical to install (e.g. this
#: pipeline's own CI test job may run against en_core_web_sm to keep test
#: latency reasonable) - the PRODUCTION BUILD must never set this override.
import os

MODEL_NAME = os.environ.get("DOCANON_SPACY_MODEL", "en_core_web_trf")

# Only entity labels that are actually person/place identifiers are taken.
# ORG is deliberately excluded: generic organisation names are business facts
# the spec requires us to preserve (section 12).
LABEL_MAP = {
    "PERSON": PiiType.PERSON,
    "GPE": PiiType.CITY_STATE,
    "LOC": PiiType.CITY_STATE,
    "FAC": PiiType.STREET,
}

BASE_CONFIDENCE = {
    PiiType.PERSON: 0.82,
    PiiType.CITY_STATE: 0.55,
    PiiType.STREET: 0.6,
}

#: Words that make a spaCy PERSON hit implausible on a form.
PERSON_STOPWORDS = {
    "form", "schedule", "irs", "department", "treasury", "internal", "revenue",
    "service", "attachment", "sequence", "copy", "page", "part", "total",
}


class NerUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=1)
def load_nlp(model: str = MODEL_NAME):
    """Load once per process; the caller is expected to reuse the returned
    pipeline, never reload it per page or per document.

    CPU is required explicitly, not merely left as whatever the default
    happens to be. en_core_web_trf runs on a torch backend that WILL try to
    use a CUDA device if thinc detects one - on a machine that happens to
    have a GPU but no properly configured CUDA runtime, that probe can raise
    rather than silently falling back. require_cpu() rules this out entirely
    before the model is loaded, matching the explicit product requirement
    that GPU availability must never be a prerequisite or even attempted.
    """
    try:
        import spacy
    except ImportError as exc:  # pragma: no cover
        raise NerUnavailable("spaCy is not installed") from exc

    spacy.require_cpu()

    try:
        return spacy.load(model, exclude=["lemmatizer", "tagger", "attribute_ruler"])
    except OSError as exc:
        raise NerUnavailable(
            f"local model '{model}' is not installed; the packaged build must bundle it"
        ) from exc


def ner_available() -> bool:
    try:
        load_nlp()
        return True
    except NerUnavailable:
        return False


def detect_ner(
    doc: Document, nlp=None, existing: Optional[list[Candidate]] = None
) -> tuple[list[Candidate], list[str]]:
    """Returns (candidates, warnings). Never raises if the model is missing.

    `existing` is whatever the cheap deterministic pass already found -
    en_core_web_trf is the single most expensive step in this pipeline (a
    full transformer forward pass per block), so a block already run
    unconditionally through it for a page full of dollar-amount rows or a
    pure-digit financial table is exactly the wasted cost the tiered
    architecture this project uses is meant to avoid. A block with no
    ALPHABETIC content left uncovered by an existing deterministic hit is
    skipped outright - there is nothing left in it a name/org/location model
    could usefully add, and skipping it costs nothing in recall.
    """
    warnings: list[str] = []
    try:
        nlp = nlp or load_nlp()
    except NerUnavailable as exc:
        warnings.append(
            f"NER layer disabled: {exc}. Detection is running on deterministic "
            "rules and layout only; person-name recall will be materially lower."
        )
        return [], warnings

    covered_by_page: dict[int, set[tuple]] = {}
    for candidate in existing or []:
        covered_by_page.setdefault(candidate.page_no, set()).add(candidate.line.key())

    out: list[Candidate] = []
    skipped_blocks = 0
    for page in doc.pages:
        page_covered = covered_by_page.get(page.number, set())
        for block in page.blocks:
            text = block.text
            if not text.strip():
                continue
            if _fully_covered_or_no_letters(block, text, page_covered):
                skipped_blocks += 1
                continue
            for ent in nlp(text).ents:
                pii_type = LABEL_MAP.get(ent.label_)
                if pii_type is None:
                    continue
                if pii_type is PiiType.PERSON and _implausible_person(ent.text):
                    continue
                # Trim incidental whitespace from the entity's own boundary
                # before mapping to geometry. en_core_web_trf's tokenizer can
                # include a trailing newline INSIDE an entity span
                # ("Marisol\n") where en_core_web_sm did not - a span ending
                # in whitespace straddles into the next line's start by one
                # character, which silently broke the cross-line rect
                # mapping and cost a real detection. The model's own
                # confidence in what text is the entity is unaffected; only
                # the whitespace at its edges is not part of it.
                ent_start_char = ent.start_char
                ent_end_char = ent.end_char
                while ent_start_char < ent_end_char and text[ent_start_char].isspace():
                    ent_start_char += 1
                while ent_end_char > ent_start_char and text[ent_end_char - 1].isspace():
                    ent_end_char -= 1
                if ent_end_char <= ent_start_char:
                    continue
                for line, start, end, rect in block.rect_for(ent_start_char, ent_end_char):
                    out.append(
                        Candidate(
                            pii_type=pii_type,
                            text=line.text[start:end],
                            page_no=page.number,
                            rect=rect,
                            line=line,
                            start=start,
                            end=end,
                            confidence=BASE_CONFIDENCE.get(pii_type, 0.6),
                            source=Source.NER,
                            evidence=[Evidence(Source.NER, f"spaCy {ent.label_}", 0.6)],
                            needs_review=pii_type is not PiiType.PERSON,
                            review_reason="" if pii_type is PiiType.PERSON else "location entity outside a labelled address group",
                        )
                    )
    if skipped_blocks:
        warnings.append(
            f"skipped the transformer on {skipped_blocks} block(s) with no "
            "uncovered alphabetic content - a financial table or pure-digit "
            "region a name/org model could not usefully improve"
        )
    return out, warnings


def _fully_covered_or_no_letters(block, text: str, page_covered: set) -> bool:
    """True if there is nothing left in this block a name/org/location model
    could usefully add.

    A financial table row is rarely PURE digits - "Line 4  Wages  $85,000"
    has real letters in it, but they are the form's own vocabulary
    (line/wages/total/etc.), never a name. Money and known form/label words
    are stripped first; only what remains is checked for name-shaped
    alphabetic content. This is deliberately conservative: it strips ONLY
    words already known elsewhere in this project to be form vocabulary,
    never guesses, so it cannot silently discard a real name sitting next
    to a number.
    """
    from .deterministic import MONEY_RE, PERCENT_RE
    from .heuristics import FORM_VOCABULARY

    if all(line.key() in page_covered for line in block.lines):
        return True

    stripped = MONEY_RE.sub(" ", text)
    stripped = PERCENT_RE.sub(" ", stripped)
    remaining_words = [
        w for w in re.findall(r"[A-Za-z]{3,}", stripped)
        if w.lower() not in FORM_VOCABULARY
    ]
    return not remaining_words


def _implausible_person(text: str) -> bool:
    """Reject model hits that no reader would call a name.

    The small model tags single capitalised form words - "Daytime", "Preparer" -
    as PERSON. Requiring a plausible name shape removes that class of noise
    without weakening recall on genuine multi-token names.
    """
    from .heuristics import FORM_VOCABULARY, looks_like_person

    stripped = text.strip()
    tokens = [t.lower().strip(".,") for t in stripped.split()]
    if not tokens or len(stripped) < 2:
        return True
    if any(t in PERSON_STOPWORDS or t in FORM_VOCABULARY for t in tokens):
        return True
    if all(t.isdigit() for t in tokens):
        return True
    # A single token from the model needed a second, independent signal
    # before this stopped rejecting it outright: en_core_web_trf segments a
    # name split across a line break ("Marisol\nEtxeberria") into TWO
    # separate single-token PERSON entities, where en_core_web_sm returned
    # one combined two-token span - so this filter, unchanged, silently lost
    # a real detection purely because of which model produced it. The shape
    # heuristics (the same ones used elsewhere for lone surnames) are that
    # second signal: "Daytime" and "Preparer" fail this check and are still
    # rejected; "Marisol" and "Etxeberria" pass it and are kept.
    if len(tokens) == 1:
        from .heuristics import looks_like_a_lone_surname

        return not looks_like_a_lone_surname(stripped)
    return not looks_like_person(stripped)[0]
