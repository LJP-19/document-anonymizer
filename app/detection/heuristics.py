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
    # Boilerplate/UI fragments seen in mail-in vouchers and cut lines.
    "here", "mail", "with", "detach", "page", "form", "cut", "fold",
    "along", "line", "dotted", "tear", "staple", "clip",
    # Reported: "Nondeductible IRAs" was typed PERSON. Every other word on
    # this list was already present; "ira"/"iras" was the one gap.
    "ira", "iras", "nondeductible", "rollover", "contribution",
    "distribution", "beneficiary", "traditional", "roth",
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
    "taxpayer", "spouse", "preparer", "filer", "ssn", "ein", "itin", "tin",
    "ptin", "social", "security", "identification",
    # Common English function words. FORM_VOCABULARY was tax-form specific, so
    # ordinary phrases like "Need to Keep" - title case, no digits - passed
    # every check and were typed PERSON.
    "need", "to", "keep", "have", "has", "had", "will", "would", "should",
    "could", "can", "may", "might", "must", "is", "are", "was", "were", "be",
    "been", "being", "do", "does", "did", "make", "made", "get", "got", "go",
    "went", "come", "came", "take", "took", "give", "gave", "want", "please",
    "note", "notes", "summary", "overview", "reminder", "action", "required",
    "pending", "next", "prior", "current", "previous", "review", "reviewed",
    "confirm", "confirmed", "pay", "paid", "payment", "estimated", "return",
    "corporation", "company", "inc", "llc", "llp", "ltd", "trust", "estate",
    "partnership", "bank", "national", "association", "federal", "united",
    "states", "america",
}

SUFFIX = r"(?:\s+(?:Jr|Sr|II|III|IV|MD|CPA|Esq|PhD|DDS)\.?)?"
TITLE = r"(?:(?:Mr|Mrs|Ms|Dr|Rev|Prof)\.?\s+)?"
ALL_CAPS_NAME = re.compile(
    r"^[A-Z][A-Z'\-]+(?:\s+[A-Z][A-Z'\-.]*){1,3}(?:\s+(?:JR|SR|II|III|IV|MD|CPA|ESQ)\.?)?$"
)
# A lone all-caps token: a surname in its own column. Weaker on its own, so it
# is only trusted inside a group whose label expects a person.
SINGLE_CAPS_TOKEN = re.compile(r"^[A-Z][A-Z'\-]{2,}$")
SURNAME_FIRST = re.compile(
    r"^[A-Z][a-zA-Z'\-]+,\s+[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-.]*)?" + SUFFIX + r"$"
)
TITLE_CASE_NAME = re.compile(
    "^" + TITLE + r"[A-Z][a-z'\-]+(?:\s+[A-Z][a-z'\-.]*){1,3}" + SUFFIX + r"$"
)
# A mixed all-caps surname with a title-case given name, plus optional suffix.
MIXED_CASE_NAME = re.compile(
    "^" + TITLE + r"[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-.]*){1,3}" + SUFFIX + r"$"
)


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
    if MIXED_CASE_NAME.match(stripped):
        # A mixed-case phrase needs more than shape: an English function word
        # anywhere in it means it is a sentence fragment, not a name -
        # "Need to Keep" has three of them.
        if any(word in FORM_VOCABULARY for word in stripped.lower().split()):
            return False, 0.0
        return True, 0.58
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
            existing_here = claimed.get(line.key(), [])
            out.extend(_scan_line(line, existing_here))
            # Joint names run independently of whatever _scan_line found, and
            # regardless of whether any field label bound this line at all.
            # resolve_overlaps() downstream handles any duplicate spans.
            out.extend(detect_joint_names(line))
    return out


SINGLE_NAME = re.compile(r"^[A-Z][a-zA-Z'\-]{2,}(?:\s+(?:Jr|Sr|II|III|IV)\.?)?$")


def looks_like_a_lone_surname(value: str) -> bool:
    """A single capitalised word standing alone in a field or cell.

    "Gonzalez-Reyes" on its own line is a surname on a form and a common noun
    in prose, so this only holds for a short standalone value - the caller must
    confirm the line contains nothing else.
    """
    stripped = value.strip().strip(".,;:")
    if len(stripped) < 3 or len(stripped) > 30:
        return False
    if _is_form_vocabulary(stripped):
        return False
    if any(ch.isdigit() or ch == "@" for ch in stripped):
        return False
    return bool(SINGLE_NAME.match(stripped) or SINGLE_CAPS_TOKEN.match(stripped))


#: "First & First Last" or "First and First Last" - a joint filer's names,
#: joined by an ampersand or "and", with a shared surname on the second half.
#: This shape is essentially unambiguous in ordinary prose, so it is matched
#: standalone, everywhere on the page - not only inside a field a label has
#: already bound. A joint name written somewhere with no name-type label
#: nearby (a signature block, a schedule continuation) was invisible without
#: this, because the household split otherwise only ran on group-bound lines.
JOINT_NAME = re.compile(
    r"\b([A-Z][a-zA-Z'\-]{1,20})\s*(?:&|\band\b)\s*"
    r"([A-Z][a-zA-Z'\-]{1,20})\s+([A-Z][a-zA-Z'\-]{1,20})\b"
)


def detect_joint_names(line: Line) -> list[Candidate]:
    """Standalone joint-name detection, independent of any field/group."""
    from .types import Evidence, PiiType, Source

    out: list[Candidate] = []
    for match in JOINT_NAME.finditer(line.text):
        first, second, surname = match.group(1), match.group(2), match.group(3)
        if _is_form_vocabulary(first) or _is_form_vocabulary(second) or _is_form_vocabulary(surname):
            continue
        household = f"{first} {second} {surname}"
        for name, start, end in (
            (first, match.start(1), match.end(1)),
            (f"{second} {surname}", match.start(2), match.end(3)),
        ):
            rect = line.rect_for(start, end)
            if rect is None:
                continue
            out.append(
                Candidate(
                    pii_type=PiiType.PERSON,
                    text=name,
                    page_no=line.page_no,
                    rect=rect,
                    line=line,
                    start=start,
                    end=end,
                    confidence=0.68,
                    source=Source.GROUP,
                    evidence=[Evidence(Source.GROUP, "standalone joint-name pattern", 0.68)],
                    household=household,
                )
            )
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
    # A single capitalised word standing alone is NOT treated as a name. That
    # rule found the occasional lone surname and misread a great deal of a form
    # besides - headings, captions, single-word answers. Token-level name
    # mapping covers the real case: a surname seen anywhere beside a given name
    # is known everywhere else it appears.
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
