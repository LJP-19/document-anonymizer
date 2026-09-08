"""Label-conditioned PII detection with GLiNER (spec section 10).

Runs `knowledgator/gliner-pii-edge-v1.0` (Apache-2.0) as a quantised ONNX graph
through onnxruntime. The official `gliner` package is not used because it pulls
in PyTorch, which would add hundreds of megabytes to an installer that must ship
offline; the pre- and post-processing is reimplemented here instead.

Unlike spaCy, GLiNER takes the label set as *input*. That is what fixes
mislabelling: asking for "taxpayer name" and "bank account number" returns those
labels, rather than a generic PERSON/ORG that then has to be guessed at.

Two of the labels are deliberately non-PII. Money and form references are asked
for so that a confident "this is a dollar amount" can suppress a competing
detection, rather than being left to regex alone.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from ..document.model import Document
from .types import Candidate, Evidence, PiiType, Source

log = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parents[2] / "resources" / "models" / "gliner-pii"
MODEL_FILE = "model_quint8.onnx"

ENT_TOKEN = "<<ENT>>"
SEP_TOKEN = "<<SEP>>"
MAX_WORDS_PER_CHUNK = 110  # the model's context is 256 subword tokens
CHUNK_OVERLAP = 12
DEFAULT_THRESHOLD = 0.45

#: Label text handed to the model -> the taxonomy type it maps to.
#: `None` marks a deliberately non-PII label used as negative evidence.
LABELS: dict[str, Optional[PiiType]] = {
    "person name": PiiType.PERSON,
    "taxpayer name": PiiType.PERSON,
    "spouse name": PiiType.PERSON,
    "preparer name": PiiType.PERSON,
    "street address": PiiType.STREET,
    "city and state": PiiType.CITY_STATE,
    "zip code": PiiType.POSTAL_CODE,
    "email address": PiiType.EMAIL,
    "phone number": PiiType.PHONE,
    "social security number": PiiType.SSN,
    "employer identification number": PiiType.EIN,
    "taxpayer identification number": PiiType.TIN,
    "bank account number": PiiType.BANK_ACCOUNT,
    "bank routing number": PiiType.ROUTING_NUMBER,
    "credit card number": PiiType.CARD_NUMBER,
    "date of birth": PiiType.DOB,
    "driver license number": PiiType.DRIVERS_LICENSE,
    "passport number": PiiType.PASSPORT,
    "insurance policy number": PiiType.POLICY_NUMBER,
    "medical record number": PiiType.MRN,
    "employee identification number": PiiType.EMPLOYEE_ID,
    "username": PiiType.USERNAME,
    # Negative classes - never redacted, used to veto competing detections.
    "money amount": None,
    "tax form or schedule number": None,
    "job title or occupation": None,
}

NEGATIVE_LABELS = {label for label, mapped in LABELS.items() if mapped is None}
LABEL_LIST = list(LABELS)


class GlinerUnavailable(RuntimeError):
    pass


@dataclass
class Span:
    start_word: int
    end_word: int
    label: str
    score: float


class GlinerDetector:
    def __init__(self, model_dir: Path = MODEL_DIR, threshold: float = DEFAULT_THRESHOLD):
        self.model_dir = Path(model_dir)
        self.threshold = threshold
        self._session = None
        self._tokenizer = None
        self._max_width = 12

    # -- loading -----------------------------------------------------------

    def load(self) -> None:
        if self._session is not None:
            return
        try:
            import numpy  # noqa: F401
            import onnxruntime as ort
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise GlinerUnavailable(f"onnxruntime/transformers not installed: {exc}") from exc

        onnx_path = self.model_dir / "onnx" / MODEL_FILE
        if not onnx_path.exists():
            raise GlinerUnavailable(
                f"model weights missing at {onnx_path}. The packaged build bundles "
                "them; for a source checkout run: python buildtools/fetch_models.py"
            )
        config_path = self.model_dir / "gliner_config.json"
        if config_path.exists():
            self._max_width = int(json.loads(config_path.read_text()).get("max_width", 12))

        options = ort.SessionOptions()
        options.log_severity_level = 3
        self._session = ort.InferenceSession(
            str(onnx_path), options, providers=["CPUExecutionProvider"]
        )
        self._tokenizer = AutoTokenizer.from_pretrained(str(self.model_dir))

    @property
    def available(self) -> bool:
        try:
            self.load()
            return True
        except GlinerUnavailable:
            return False

    # -- inference ---------------------------------------------------------

    def _encode(self, words: list[str], labels: list[str]):
        import numpy as np

        tokens: list[str] = []
        word_of_token: list[int] = []
        for label in labels:
            tokens.append(ENT_TOKEN)
            word_of_token.append(0)
            for piece in label.split():
                tokens.append(piece)
                word_of_token.append(0)
        tokens.append(SEP_TOKEN)
        word_of_token.append(0)
        for index, word in enumerate(words, start=1):
            tokens.append(word)
            word_of_token.append(index)

        encoded = self._tokenizer(
            tokens, is_split_into_words=True, return_tensors="np", add_special_tokens=True
        )
        mask, seen = [], set()
        for token_word in encoded.word_ids(0):
            if token_word is None or token_word in seen:
                mask.append(0)
            else:
                seen.add(token_word)
                mask.append(word_of_token[token_word])
        return encoded, np.array([mask], dtype=np.int64)

    def predict(self, words: list[str], labels: Optional[list[str]] = None) -> list[Span]:
        import numpy as np

        self.load()
        labels = labels or LABEL_LIST
        if not words:
            return []
        encoded, words_mask = self._encode(words, labels)
        logits = self._session.run(
            None,
            {
                "input_ids": encoded["input_ids"].astype(np.int64),
                "attention_mask": encoded["attention_mask"].astype(np.int64),
                "words_mask": words_mask,
                "text_lengths": np.array([[len(words)]], dtype=np.int64),
            },
        )[0]
        return self._decode(logits, len(words), labels)

    def _decode(self, logits, word_count: int, labels: list[str]) -> list[Span]:
        import numpy as np

        scores = 1.0 / (1.0 + np.exp(-logits[0]))  # [words, labels, (start, end, inside)]
        found: list[Span] = []
        for index, label in enumerate(labels):
            starts, ends, inside = scores[:, index, 0], scores[:, index, 1], scores[:, index, 2]
            for i in range(min(word_count, scores.shape[0])):
                if starts[i] < self.threshold:
                    continue
                for j in range(i, min(word_count, i + self._max_width)):
                    if ends[j] < self.threshold:
                        continue
                    middle = inside[i : j + 1]
                    if middle.min() < self.threshold * 0.5:
                        continue
                    score = float((starts[i] + ends[j] + middle.mean()) / 3)
                    found.append(Span(i, j, label, score))
                    break

        # Highest-scoring span wins any overlap.
        found.sort(key=lambda s: -s.score)
        kept: list[Span] = []
        for span in found:
            if any(not (span.end_word < k.start_word or k.end_word < span.start_word) for k in kept):
                continue
            kept.append(span)
        return sorted(kept, key=lambda s: s.start_word)


# -- document integration ---------------------------------------------------


def _words_with_offsets(text: str) -> list[tuple[str, int, int]]:
    words, start = [], None
    for index, char in enumerate(text):
        if char.isspace():
            if start is not None:
                words.append((text[start:index], start, index))
                start = None
        elif start is None:
            start = index
    if start is not None:
        words.append((text[start:], start, len(text)))
    return words


@lru_cache(maxsize=1)
def _detector() -> GlinerDetector:
    return GlinerDetector()


def detect_gliner(
    doc: Document, detector: Optional[GlinerDetector] = None
) -> tuple[list[Candidate], list[str], list[tuple[int, tuple, str]]]:
    """Returns (candidates, warnings, negative_regions).

    `negative_regions` are spans the model identified as money, form numbers or
    job titles - things the document should keep. They are handed to the
    resolver as veto evidence rather than turned into candidates.
    """
    detector = detector or _detector()
    warnings: list[str] = []
    try:
        detector.load()
    except GlinerUnavailable as exc:
        warnings.append(
            f"GLiNER layer disabled: {exc} Detection is running on rules, layout "
            "and spaCy only; labelling accuracy will be lower."
        )
        return [], warnings, []

    candidates: list[Candidate] = []
    negatives: list[tuple[int, tuple, str]] = []

    for page in doc.pages:
        for block in page.blocks:
            text = block.text
            words = _words_with_offsets(text)
            if not words:
                continue
            for chunk_start in range(0, len(words), MAX_WORDS_PER_CHUNK - CHUNK_OVERLAP):
                chunk = words[chunk_start : chunk_start + MAX_WORDS_PER_CHUNK]
                if not chunk:
                    break
                try:
                    spans = detector.predict([w for w, _s, _e in chunk])
                except Exception as exc:  # noqa: BLE001 - never break analysis
                    log.warning("GLiNER inference failed on a block: %s", type(exc).__name__)
                    warnings.append(f"GLiNER inference failed on page {page.number + 1}")
                    break

                for span in spans:
                    char_start = chunk[span.start_word][1]
                    char_end = chunk[span.end_word][2]
                    for line, line_start, line_end, rect in block.rect_for(char_start, char_end):
                        if span.label in NEGATIVE_LABELS:
                            negatives.append((page.number, rect, span.label))
                            continue
                        pii_type = LABELS[span.label]
                        candidates.append(
                            Candidate(
                                pii_type=pii_type,
                                text=line.text[line_start:line_end],
                                page_no=page.number,
                                rect=rect,
                                line=line,
                                start=line_start,
                                end=line_end,
                                confidence=min(0.95, span.score),
                                source=Source.NER,
                                evidence=[
                                    Evidence(Source.NER, f"GLiNER: {span.label}", span.score)
                                ],
                                needs_review=span.score < 0.62,
                                review_reason=(
                                    "" if span.score >= 0.62 else "low model confidence"
                                ),
                            )
                        )
                if chunk_start + MAX_WORDS_PER_CHUNK >= len(words):
                    break

    return candidates, warnings, negatives
