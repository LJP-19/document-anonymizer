"""Local NER (spec section 10). No network, no API - a bundled spaCy model only.

Runs on block text rather than line text so that sentences spanning several
visual lines are handled (spec section 20). Entity offsets are mapped back to
per-line rectangles through the block offset map.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

from ..document.model import Document
from .types import Candidate, Evidence, PiiType, Source

log = logging.getLogger(__name__)

MODEL_NAME = "en_core_web_sm"

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
    try:
        import spacy
    except ImportError as exc:  # pragma: no cover
        raise NerUnavailable("spaCy is not installed") from exc
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


def detect_ner(doc: Document, nlp=None) -> tuple[list[Candidate], list[str]]:
    """Returns (candidates, warnings). Never raises if the model is missing."""
    warnings: list[str] = []
    try:
        nlp = nlp or load_nlp()
    except NerUnavailable as exc:
        warnings.append(
            f"NER layer disabled: {exc}. Detection is running on deterministic "
            "rules and layout only; person-name recall will be materially lower."
        )
        return [], warnings

    out: list[Candidate] = []
    for page in doc.pages:
        for block in page.blocks:
            text = block.text
            if not text.strip():
                continue
            for ent in nlp(text).ents:
                pii_type = LABEL_MAP.get(ent.label_)
                if pii_type is None:
                    continue
                if pii_type is PiiType.PERSON and _implausible_person(ent.text):
                    continue
                for line, start, end, rect in block.rect_for(ent.start_char, ent.end_char):
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
    return out, warnings


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
    # A single token from the model is too weak on its own; the shape
    # heuristics pick up genuine lone surnames inside name fields.
    if len(tokens) == 1:
        return True
    return not looks_like_person(stripped)[0]
