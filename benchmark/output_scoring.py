"""Output-level scoring: what actually changed in the WRITTEN file.

The detection benchmark (`run_benchmark.py`) compares candidate TEXT against a
gold list. That cannot see three failure classes that matter most for a
redaction tool:

  - a detection that never became a redaction (plan, geometry or writer bug)
  - collateral damage: a redaction box wider than its value, erasing a label
  - a value redacted on one page and left on another

This module compares each word of the INPUT file with the OUTPUT file at the
same position. A word is "kept" if the output still has the same text there,
in a non-red colour; otherwise it was transformed (removed, replaced, or
blacked out). Scoring needs only (input, output, per-word truth), so it works
identically for any build of the application and for any document.

Nothing here prints or stores word text unless the caller asks for it, so it is
safe to run against real documents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import pymupdf

RED = 0xFF0000


@dataclass
class Word:
    page: int
    text: str
    bbox: tuple[float, float, float, float]
    color: int
    font: str

    @property
    def center(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bbox
        return (x0 + x1) / 2, (y0 + y1) / 2


def extract_words(path: str) -> list[list[Word]]:
    """Every whitespace-delimited word on every page, with colour and font.

    Built from rawdict characters so each word keeps the colour of the span it
    came from - get_text("words") drops colour, and colour is how replacement
    text is recognised.
    """
    pages: list[list[Word]] = []
    with pymupdf.open(path) as doc:
        for page_no, page in enumerate(doc):
            words: list[Word] = []
            raw = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_TEXT)
            for block in raw.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        buf, box = [], None
                        for ch in span.get("chars", []) + [None]:
                            if ch is None or ch["c"].isspace():
                                if buf and box:
                                    words.append(Word(page_no, "".join(buf), tuple(box),
                                                      span["color"], span["font"]))
                                buf, box = [], None
                                continue
                            buf.append(ch["c"])
                            x0, y0, x1, y1 = ch["bbox"]
                            if box is None:
                                box = [x0, y0, x1, y1]
                            else:
                                box = [min(box[0], x0), min(box[1], y0),
                                       max(box[2], x1), max(box[3], y1)]
            pages.append(words)
    return pages


def _same_place(a: Word, b: Word, tol: float = 2.5) -> bool:
    ax, ay = a.center
    bx, by = b.center
    return abs(ax - bx) <= tol + 0.15 * (a.bbox[2] - a.bbox[0]) and abs(ay - by) <= tol


def kept_mask(inp: list[list[Word]], out: list[list[Word]]) -> list[list[bool]]:
    """For each input word: True if the output still shows it, unchanged, in place."""
    mask: list[list[bool]] = []
    for page_no, words in enumerate(inp):
        out_words = out[page_no] if page_no < len(out) else []
        by_text: dict[str, list[Word]] = {}
        for w in out_words:
            by_text.setdefault(w.text, []).append(w)
        page_mask = []
        for w in words:
            same = [o for o in by_text.get(w.text, ()) if _same_place(w, o)]
            # A word that was ALREADY red in the input is kept if still there;
            # a word that was not red is only kept if it is still not red.
            if w.color == RED:
                page_mask.append(bool(same))
            else:
                page_mask.append(any(o.color != RED for o in same))
        mask.append(page_mask)
    return mask


# Truth labels for a word.
PII, NOT_PII, EXCLUDED = "pii", "not_pii", "excluded"


@dataclass
class Score:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    excluded: int = 0
    by_category: dict = field(default_factory=dict)
    fn_pages: dict = field(default_factory=dict)
    fp_pages: dict = field(default_factory=dict)

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def as_dict(self) -> dict:
        return {
            "tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
            "excluded": self.excluded,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "false_positive_rate": round(self.fp / (self.fp + self.tn), 4) if self.fp + self.tn else 0.0,
            "false_negative_rate": round(self.fn / (self.fn + self.tp), 4) if self.fn + self.tp else 0.0,
            "by_category": self.by_category,
        }


def score(
    inp: list[list[Word]],
    mask: list[list[bool]],
    labeller: Callable[[Word], tuple[str, str]],
) -> Score:
    """labeller(word) -> (PII | NOT_PII | EXCLUDED, category)."""
    s = Score()
    for page_words, page_mask in zip(inp, mask):
        for word, kept in zip(page_words, page_mask):
            label, category = labeller(word)
            if label == EXCLUDED:
                s.excluded += 1
                continue
            cat = s.by_category.setdefault(category, {"tp": 0, "fp": 0, "fn": 0, "tn": 0})
            transformed = not kept
            if label == PII:
                key = "tp" if transformed else "fn"
            else:
                key = "fp" if transformed else "tn"
            cat[key] += 1
            setattr(s, key, getattr(s, key) + 1)
            if key == "fn":
                s.fn_pages[word.page + 1] = s.fn_pages.get(word.page + 1, 0) + 1
            elif key == "fp":
                s.fp_pages[word.page + 1] = s.fp_pages.get(word.page + 1, 0) + 1
    return s


def mask_shape(text: str) -> str:
    """A PII-free rendering of a value's shape: 'John 555-0101' -> 'Xxxx 999-9999'."""
    out = []
    for ch in text:
        if ch.isupper():
            out.append("X")
        elif ch.islower():
            out.append("x")
        elif ch.isdigit():
            out.append("9")
        else:
            out.append(ch)
    return "".join(out)


def iter_pairs(inp: Iterable[list[Word]], mask: Iterable[list[bool]]):
    for words, m in zip(inp, mask):
        yield from zip(words, m)


def page_count(path: str) -> int:
    with pymupdf.open(path) as doc:
        return doc.page_count


def surviving_text(path: str) -> str:
    with pymupdf.open(path) as doc:
        return "\n".join(page.get_text() for page in doc)


def optional(value) -> Optional[str]:
    return None if value is None else str(value)
