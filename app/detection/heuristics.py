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
from .textnorm import fold, is_letter
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

# ---------------------------------------------------------------------------
# Name SHAPE, in any script. These used to be five ASCII regexes
# ([A-Z][A-Za-z'\-]+ ...), so "Müller", "Łukasz", "Nguyễn Văn Minh" and every
# name in a caseless script (Chinese, Arabic) could never look like a name:
# measured, "Hans Müller" was missed in every layout where "Hans Mueller" was
# caught. Shape is judged per TOKEN by character class, never by a dictionary:
# an unfamiliar name must be as acceptable as a common one.
# ---------------------------------------------------------------------------
_LEGAL_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "md", "cpa", "esq", "phd", "dds"}
_TITLE_WORDS = {"mr", "mrs", "ms", "dr", "rev", "prof"}
_COMMAS = {ord("\u060c"): ",", ord("\uff0c"): ",", ord("\u3001"): ","}
_JOINERS = "'\u2019-"


def _letter_count(token: str) -> int:
    return sum(1 for ch in token if is_letter(ch))


def _token_ok(token: str) -> bool:
    """Letters, with ' ’ - allowed INSIDE, and an optional trailing period."""
    core = token[:-1] if token.endswith(".") else token
    if not core:
        return False
    for index, ch in enumerate(core):
        if is_letter(ch):
            continue
        if ch in _JOINERS and 0 < index < len(core) - 1:
            continue
        return False
    return True


def _case_class(token: str) -> str:
    """upper | title | mixed | lower | caseless."""
    letters = [ch for ch in token if is_letter(ch)]
    cased = [ch for ch in letters if ch.isupper() or ch.islower()]
    if not cased:
        return "caseless"
    if all(ch.isupper() for ch in cased):
        return "upper"
    if all(ch.islower() for ch in cased):
        return "lower"
    parts = [p for p in re.split(r"[-'\u2019]", token.rstrip(".")) if p]
    def title_part(part: str) -> bool:
        first = next((ch for ch in part if is_letter(ch)), "")
        rest = [ch for ch in part.replace(first, "", 1) if is_letter(ch) and (ch.isupper() or ch.islower())]
        return first.isupper() and all(ch.islower() for ch in rest)
    if all(title_part(p) for p in parts):
        return "title"
    first_cased = cased[0]
    return "mixed" if first_cased.isupper() else "lower"


def _is_initial(token: str) -> bool:
    letters = [ch for ch in token if is_letter(ch)]
    return len(letters) == 1 and letters[0].isupper()


def _drop_suffix(tokens: list[str]) -> list[str]:
    if tokens and fold(tokens[-1].rstrip(".")) in _LEGAL_SUFFIXES:
        return tokens[:-1]
    return tokens


def _all_caps_name(text: str) -> bool:
    tokens = _drop_suffix(text.split())
    if not 2 <= len(tokens) <= 4 or not all(_token_ok(t) for t in tokens):
        return False
    return all(_case_class(t) == "upper" for t in tokens) and _letter_count(tokens[0]) >= 2


def _single_caps_token(text: str) -> bool:
    tokens = text.split()
    return (len(tokens) == 1 and _token_ok(tokens[0]) and _case_class(tokens[0]) == "upper"
            and _letter_count(tokens[0]) >= 3)


_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM",
    "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY", "PR", "GU", "VI",
}


def _surname_first(text: str) -> bool:
    text = text.translate(_COMMAS)
    if "," not in text:
        return False
    left, _, right = text.partition(",")
    left_tokens, right_tokens = left.split(), _drop_suffix(right.split())
    if len(left_tokens) != 1 or not 1 <= len(right_tokens) <= 2:
        return False
    if not all(_token_ok(t) for t in left_tokens + right_tokens):
        return False
    # "Springfield, CA" is a place, not "Surname, Given": a US state code is never a given name.
    if right_tokens[0].rstrip(".") in _US_STATES and len(right_tokens) == 1:
        return False
    return all(_case_class(t) in ("title", "mixed", "upper", "caseless") for t in left_tokens + right_tokens)


def _cased_name(text: str, loose: bool) -> bool:
    tokens = text.split()
    if tokens and fold(tokens[0].rstrip(".")) in _TITLE_WORDS:
        tokens = tokens[1:]
    tokens = _drop_suffix(tokens)
    if not 2 <= len(tokens) <= 4 or not all(_token_ok(t) for t in tokens):
        return False
    allowed = ("title", "mixed", "upper") if loose else ("title",)
    if _case_class(tokens[0]) not in allowed or _letter_count(tokens[0]) < 2:
        return False
    return all(_case_class(t) in allowed or _is_initial(t) for t in tokens[1:])


def _caseless_name(text: str) -> bool:
    """A run of letters in a script with no capitals (Chinese, Arabic, Thai ...).
    Short, because a whole caseless sentence is not a name."""
    tokens = text.split()
    if not 1 <= len(tokens) <= 4:
        return False
    if not all(_token_ok(t) and _case_class(t) == "caseless" for t in tokens):
        return False
    return 2 <= sum(_letter_count(t) for t in tokens) <= 12


def _lone_name(text: str) -> bool:
    tokens = _drop_suffix(text.split())
    if len(tokens) != 1 or not _token_ok(tokens[0]):
        return False
    return (_case_class(tokens[0]) in ("title", "mixed") and _letter_count(tokens[0]) >= 3) \
        or _single_caps_token(tokens[0])


def _is_form_vocabulary(text: str) -> bool:
    tokens = [t.strip(".,:;()").lower() for t in text.split()]
    if not tokens:
        return True
    # Any structural word disqualifies the whole span - "TOTAL WAGES PAID" is
    # not a person no matter how name-shaped it looks.
    return any(t in FORM_VOCABULARY for t in tokens)


def looks_like_person(text: str) -> tuple[bool, float]:
    """Returns (is_name_shaped, confidence). Script-aware; never dictionary-based."""
    stripped = text.strip().strip(".,;:")
    caseless = _caseless_name(stripped)
    if len(stripped) > 60 or _is_form_vocabulary(stripped):
        return False, 0.0
    if len(stripped) < 4 and not caseless:
        return False, 0.0
    if any(ch.isdigit() or ch == "@" for ch in stripped):
        return False, 0.0
    if _all_caps_name(stripped):
        return True, 0.72
    if _single_caps_token(stripped):
        return True, 0.55
    if _surname_first(stripped):
        return True, 0.75
    if _cased_name(stripped, loose=False):
        return True, 0.62
    if _cased_name(stripped, loose=True):
        # A mixed-case phrase needs more than shape: an English function word
        # anywhere in it means it is a sentence fragment, not a name -
        # "Need to Keep" has three of them.
        if any(word in FORM_VOCABULARY for word in stripped.lower().split()):
            return False, 0.0
        return True, 0.58
    if caseless:
        return True, 0.52
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
    if any(m.group(1).isupper() for m in re.finditer(r"\b\d{1,6}\s+([^\W\d_])", stripped)):  # street address
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
    return _lone_name(stripped)


#: "First & First Last" or "First and First Last" - a joint filer's names,
#: joined by an ampersand or "and", with a shared surname on the second half.
#: This shape is essentially unambiguous in ordinary prose, so it is matched
#: standalone, everywhere on the page - not only inside a field a label has
#: already bound. A joint name written somewhere with no name-type label
#: nearby (a signature block, a schedule continuation) was invisible without
#: this, because the household split otherwise only ran on group-bound lines.
_NAME_RUN = r"[^\W\d_][\w'\u2019\-]{1,20}"
JOINT_NAME = re.compile(rf"\b({_NAME_RUN})\s*(?:&|\band\b)\s*({_NAME_RUN})\s+({_NAME_RUN})\b")


def detect_joint_names(line: Line) -> list[Candidate]:
    """Standalone joint-name detection, independent of any field/group."""
    from .types import Evidence, PiiType, Source

    out: list[Candidate] = []
    for match in JOINT_NAME.finditer(line.text):
        first, second, surname = match.group(1), match.group(2), match.group(3)
        if not all(_token_ok(w) and _case_class(w) in ("title", "mixed", "upper") for w in (first, second, surname)):
            continue
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

    overlapping = [c for c in on_line if c.start < end and start < c.end]
    if overlapping:
        # Normally another detector already owns the line. The exception: a
        # statistical model tagged only a FRAGMENT of a name-shaped line as a
        # place ("Núñez" in "Núñez, José"). The whole-line shape is stronger
        # evidence than a partial geographic tag, so offer it and let overlap
        # resolution arbitrate (the longer span wins at equal rank, and the
        # disagreement is flagged for review).
        geographic = {PiiType.CITY_STATE, PiiType.ADDRESS, PiiType.STREET, PiiType.POSTAL_CODE}
        partial_model_fragments = all(
            (c.pii_type in geographic or c.pii_type is PiiType.PERSON)
            and c.source in (Source.NER, Source.COVERAGE)
            and not (c.start <= start and end <= c.end)
            for c in overlapping
        )
        # At least one fragment must be the WRONG kind (a place tag inside a name);
        # fragments that are all PERSON already describe the name correctly.
        wrong_kind_fragment = any(c.pii_type in geographic for c in overlapping)
        if not (partial_model_fragments and wrong_kind_fragment):
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
