"""Shape-based heuristics for identity text the statistical model misses.

spaCy's NER is trained on prose. It fails badly on the two shapes that dominate
tax and financial forms:

    JOHN A SMITH            (all caps, no sentence context)
    Smith, John A           (surname-first, comma separated)

Both are unambiguous to a human reader looking at a field labelled "Name". These
detectors use letter case, token shape and position inside a labelled value
group rather than a language model.
"""

from __future__ import annotations

import re

from ..document.model import Document, Line
from .types import Candidate, Evidence, PiiType, Source

# Words that look like names by shape but are structural text on forms.
FORM_VOCABULARY = {
    "form", "schedule", "department", "treasury", "internal", "revenue",
    "service", "attachment", "sequence", "omb", "copy", "page", "part",
    "section", "line", "total", "subtotal", "amount", "balance", "due",
    "paid", "tax", "taxes", "income", "wages", "salary", "gross", "net",
    "adjusted", "taxable", "deduction", "deductions", "credit", "credits",
    "refund", "withholding", "employer", "employee", "name", "address",
    "city", "state", "zip", "code", "number", "date", "signature", "title",
    "single", "married", "filing", "jointly", "separately", "household",
    "widow", "widower", "yes", "no", "none", "see", "instructions", "check",
    "box", "if", "and", "or", "the", "of", "for", "from", "this", "that",
    "continued", "important", "notice", "statement", "summary", "detail",
    "account", "type", "description", "quantity", "rate", "percent",
    "daytime", "evening", "home", "work", "mobile", "cell", "phone", "email",
    "fax", "routing", "bank", "preparer", "occupation", "engineer", "manager",
    "analyst", "director", "officer", "consultant", "attorney", "accountant",
    "corporation", "company", "inc", "llc", "llp", "ltd", "trust", "estate",
    "partnership", "bank", "national", "association", "federal", "united",
    "states", "america",
}

ALL_CAPS_NAME = re.compile(r"^[A-Z][A-Z'\-]+(?:\s+[A-Z][A-Z'\-.]*){1,3}$")
# A lone all-caps token: a surname in its own column. Weaker on its own, so it
# is only trusted inside a group whose label expects a person.
SINGLE_CAPS_TOKEN = re.compile(r"^[A-Z][A-Z'\-]{2,}$")
SURNAME_FIRST = re.compile(r"^[A-Z][a-zA-Z'\-]+,\s+[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-.]*)?$")
TITLE_CASE_NAME = re.compile(r"^[A-Z][a-z'\-]+(?:\s+[A-Z][a-z'\-.]*){1,3}$")


def _is_form_vocabulary(text: str) -> bool:
    tokens = [t.strip(".,:;()").lower() for t in text.split()]
    if not tokens:
        return True
    # Any structural word disqualifies the whole span - "TOTAL WAGES PAID" is
    # not a person no matter how name-shaped it looks.
    return any(t in FORM_VOCABULARY for t in tokens)


def looks_like_person(text: str) -> tuple[bool, float]:
    """Returns (is_name_shaped, confidence)."""
    stripped = text.strip().strip(".,;:")
    if len(stripped) < 4 or len(stripped) > 60 or _is_form_vocabulary(stripped):
        return False, 0.0
    if any(ch.isdigit() or ch == "@" for ch in stripped):
        return False, 0.0
    if ALL_CAPS_NAME.match(stripped):
        return True, 0.72
    if SINGLE_CAPS_TOKEN.match(stripped):
        return True, 0.55
    if SURNAME_FIRST.match(stripped):
        return True, 0.75
    if TITLE_CASE_NAME.match(stripped):
        return True, 0.62
    return False, 0.0


def looks_sensitive(text: str) -> bool:
    """Is this value line identity-bearing enough to redact under an unknown label?

    Deliberately conservative. Under a label the taxonomy does not recognise, we
    only act on shapes that carry identity: names, addresses, contact details,
    long identifiers. Plain words like "Married filing jointly" are left alone,
    because destroying WHAT the document says is as much a failure as leaking
    WHO it belongs to (spec section 3).
    """
    stripped = text.strip()
    if not stripped or _is_form_vocabulary(stripped):
        return False
    if "@" in stripped:
        return True
    if looks_like_person(stripped)[0]:
        return True
    if re.search(r"\b\d{1,6}\s+[A-Z][A-Za-z.\-]", stripped):  # street address
        return True
    if re.search(r",\s*[A-Z]{2}\b", stripped):  # city, ST
        return True
    digits = sum(ch.isdigit() for ch in stripped)
    if digits >= 5:  # identifier-length numeric run
        return True
    return False


def detect_heuristics(doc: Document, existing: list[Candidate]) -> list[Candidate]:
    """Name-shaped lines that no other detector claimed."""
    claimed: dict[tuple[int, int, int], list[Candidate]] = {}
    for c in existing:
        claimed.setdefault(c.line.key(), []).append(c)

    out: list[Candidate] = []
    for page in doc.pages:
        for line in page.lines:
            out.extend(_scan_line(line, claimed.get(line.key(), [])))
    return out


def _scan_line(line: Line, on_line: list[Candidate]) -> list[Candidate]:
    text = line.text
    results: list[Candidate] = []

    # Whole trimmed line first: the common case on forms.
    start = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    if end <= start:
        return results

    segment = text[start:end]
    is_name, confidence = looks_like_person(segment)
    if not is_name:
        return results

    covered = any(c.start < end and start < c.end for c in on_line)
    if covered:
        return results

    rect = line.rect_for(start, end)
    if rect is None:
        return results

    return [
        Candidate(
            pii_type=PiiType.PERSON,
            text=segment,
            page_no=line.page_no,
            rect=rect,
            line=line,
            start=start,
            end=end,
            confidence=confidence,
            source=Source.COVERAGE,
            evidence=[Evidence(Source.COVERAGE, "name-shaped line, no model hit", confidence)],
            needs_review=True,
            review_reason="matched by name shape rather than by the language model",
        )
    ]
