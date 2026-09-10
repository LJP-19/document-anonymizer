"""Deterministic, offline pseudonym generation (spec sections 25 and 36).

Same input + same scope seed => same pseudonym, every run, no state file needed.
Replacements aim to match the original's visual length so that layout survives.
"""

from __future__ import annotations

import hashlib
import re
import threading
import string
from typing import Optional

from faker import Faker

from ..detection.types import PiiType

_STATE_CODES = [
    "AL", "AZ", "CO", "CT", "FL", "GA", "IA", "KS", "MA", "MD", "MI", "MN",
    "MO", "NC", "NE", "NV", "OH", "OR", "SC", "TN", "UT", "VA", "WA", "WI",
]


def _seed(scope: str, pii_type: PiiType, value: str) -> int:
    key = f"{scope}|{pii_type.value}|{' '.join(value.split()).lower()}"
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")


def _mask_preserving_shape(value: str, rng_seed: int) -> str:
    """Format-preserving substitution: digits->digits, letters->letters."""
    digits = "0123456789"
    letters = string.ascii_uppercase
    out = []
    h = hashlib.sha256(str(rng_seed).encode()).digest()
    i = 0
    for ch in value:
        if ch.isdigit():
            out.append(digits[h[i % len(h)] % 10])
            i += 1
        elif ch.isalpha():
            c = letters[h[i % len(h)] % 26]
            out.append(c if ch.isupper() else c.lower())
            i += 1
        else:
            out.append(ch)
    return "".join(out)


def _fit_length(candidates: list[str], target: int) -> str:
    return min(candidates, key=lambda s: abs(len(s) - target))


#: One Faker per thread, reused. Constructing one loads every provider, and
#: this function is called thousands of times per document - building a fresh
#: instance each call was slow and, under Qt teardown, aborted the process
#: inside the garbage collector. Seeding gives the determinism, not the
#: construction.
_local = threading.local()


def _faker() -> Faker:
    instance = getattr(_local, "faker", None)
    if instance is None:
        instance = Faker("en_US")
        _local.faker = instance
    return instance


def generate_for_household(value: str, household: str, scope: str = "document") -> str:
    """One side of a joint name, sharing the household's new surname.

    "Mark & Mich Smith" is one couple. Giving each half an unrelated surname
    loses that, and whoever reads the anonymised return can no longer tell they
    are married.
    """
    shared = _faker()
    shared.seed_instance(_seed(scope, PiiType.PERSON, household))
    surname = shared.last_name()

    side = _faker()
    side.seed_instance(_seed(scope, PiiType.PERSON, value))
    given = side.first_name()

    tokens = [tok for tok in NAME_SUFFIX.sub("", value).split() if tok]
    return f"{given} {surname}" if len(tokens) > 1 else given


NAME_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|md|cpa|esq)\b\.?$", re.I)


def generate(pii_type: PiiType, value: str, scope: str = "document") -> str:
    """Return a stable pseudonym for `value` of type `pii_type`."""
    seed = _seed(scope, pii_type, value)
    fake = _faker()
    fake.seed_instance(seed)
    target = len(value.strip())

    if pii_type is PiiType.PERSON:
        options = [fake.name() for _ in range(6)]
        # Preserve a single-token original as a single token.
        if len(value.split()) == 1:
            options = [fake.last_name() for _ in range(6)]
        return _fit_length(options, target)

    if pii_type in (PiiType.STREET, PiiType.PO_BOX):
        return _fit_length([fake.street_address() for _ in range(5)], target)

    if pii_type is PiiType.CITY_STATE:
        if "," in value:
            return _fit_length(
                [f"{fake.city()}, {fake.random_element(_STATE_CODES)}" for _ in range(6)], target
            )
        return _fit_length([fake.city() for _ in range(6)], target)

    if pii_type is PiiType.ADDRESS:
        if re.search(r",\s*[A-Z]{2}", value):
            zip_part = re.search(r"\d{5}(?:-\d{4})?", value)
            base = f"{fake.city()}, {fake.random_element(_STATE_CODES)}"
            return f"{base} {fake.postcode()}" if zip_part else base
        return _fit_length([fake.street_address() for _ in range(5)], target)

    if pii_type is PiiType.POSTAL_CODE:
        return fake.postcode() if len(value) <= 5 else f"{fake.postcode()}-{fake.numerify('####')}"

    if pii_type is PiiType.SSN:
        return f"{fake.numerify('1##')}-{fake.numerify('##')}-{fake.numerify('####')}"

    if pii_type is PiiType.ITIN:
        return f"9{fake.numerify('##')}-7{fake.numerify('#')}-{fake.numerify('####')}"

    if pii_type in (PiiType.EIN, PiiType.TIN, PiiType.STATE_TAX_ID):
        return f"{fake.numerify('##')}-{fake.numerify('#######')}"

    if pii_type is PiiType.EMAIL:
        local = fake.user_name()[: max(3, min(len(value.split('@')[0]), 14))]
        return f"{local}@example.com"

    if pii_type in (PiiType.PHONE, PiiType.FAX):
        return _mask_preserving_shape(re.sub(r"\d", "5", value), seed)

    if pii_type is PiiType.DOB:
        d = fake.date_of_birth(minimum_age=22, maximum_age=80)
        return d.strftime("%m/%d/%Y") if "/" in value else d.strftime("%m-%d-%Y")

    if pii_type is PiiType.ORG_PRIVATE:
        # Keep the entity suffix: "LLC" and "Inc" carry tax meaning that the
        # receiving analysis needs, while the name itself does not.
        import re as _re

        suffix = _re.search(r"\b(LLC|L\.L\.C\.|Inc\.?|Corp\.?|Co\.?|LP|LLP|PLLC|PC|Ltd\.?|Trust)\b",
                            value, _re.I)
        base = _fit_length([fake.company() for _ in range(5)], len(value))
        base = _re.sub(r"[,]?\s+(LLC|Inc\.?|Ltd\.?|PLC|Group|and Sons|Group)$", "", base).strip()
        return f"{base} {suffix.group()}" if suffix else base

    if pii_type is PiiType.GENDER:
        # Must differ from the original, or the value survives the redaction and
        # verification correctly reports the original as still present.
        current = value.strip().lower()
        if len(current) <= 2:
            return "F" if current.startswith("m") else "M"
        return "female" if current.startswith("m") else "male"

    if pii_type is PiiType.CITIZENSHIP:
        return fake.country()

    if pii_type is PiiType.BIRTHPLACE:
        return f"{fake.city()}, {fake.country()}" if "," in value else fake.city()

    if pii_type is PiiType.MARITAL_STATUS:
        return value  # a filing-relevant fact; never altered

    if pii_type is PiiType.UNCLASSIFIED_GROUP_VALUE:
        return _pseudonym_for_unclassified(value, fake, seed)

    # Everything else: keep the shape, change every character.
    return _mask_preserving_shape(value, seed)


COMPANY_WORDS = {
    "llc", "inc", "corp", "corporation", "company", "co", "ltd", "lp", "llp",
    "plc", "pllc", "pc", "trust", "partners", "holdings", "group", "associates",
    "enterprises", "services", "solutions", "industries", "bank", "&",
}

#: Label words that appear INSIDE a value. They name the field, so they are the
#: form talking, not the client - and must survive verbatim. Turning "PTIN" into
#: "Lynch" destroys the document's meaning as surely as leaking the number.
INLINE_LABELS = {
    "ptin", "ssn", "ein", "itin", "tin", "efin", "caf", "id", "no", "no.",
    "num", "number", "acct", "account", "routing", "aba", "dob", "apt", "apt.",
    "unit", "ste", "suite", "box", "po", "p.o.", "phone", "tel", "fax", "cell",
    "mobile", "email", "e-mail", "attn", "c/o", "dba", "of", "and", "the",
    "mr", "mrs", "ms", "dr", "jr", "sr", "ii", "iii", "iv", "cpa", "esq", "md",
}

#: Real state codes only. Matching any two capitals turned the initials "LJ"
#: into a state abbreviation.
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR", "VI", "GU", "AS", "MP",
}
DATE_RE = re.compile(r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}")


def looks_like_a_company(value: str) -> bool:
    return any(token.strip(".,").lower() in COMPANY_WORDS for token in value.split())


def _is_inline_label(token: str) -> bool:
    return token.strip(".,:;()").lower() in INLINE_LABELS


def _initials_like(value: str, seed: int) -> str:
    letters = "BCDFGHJKLMNPRSTVWZ"
    digest = hashlib.sha256(str(seed).encode()).digest()
    out = "".join(letters[digest[i % len(digest)] % len(letters)] for i in range(len(value)))
    return out if value.isupper() else out.capitalize()


def _digits_like(value: str, fake: Faker) -> str:
    """Same punctuation and letters' positions, different digits."""
    return "".join(str(fake.random_digit()) if ch.isdigit() else ch for ch in value)


def _reference_like(token: str, fake: Faker) -> str:
    """An identifier such as P01234567: same shape, different characters."""
    out = []
    for ch in token:
        if ch.isdigit():
            out.append(str(fake.random_digit()))
        elif ch.isalpha():
            letter = fake.random_element("ABCDEFGHJKLMNPQRSTUVWXYZ")
            out.append(letter if ch.isupper() else letter.lower())
        else:
            out.append(ch)
    return "".join(out)


def _date_like(value: str, fake: Faker) -> str:
    separator = "/" if "/" in value else "-"
    parts = re.split(r"[/\-]", value)
    date = fake.date_of_birth(minimum_age=20, maximum_age=80)
    year = date.strftime("%Y") if len(parts[-1]) == 4 else date.strftime("%y")
    return separator.join([date.strftime("%m"), date.strftime("%d"), year])


def _pseudonym_for_unclassified(value: str, fake: Faker, seed: int) -> str:
    """A value whose type no detector settled on.

    Replaced component by component rather than as one blob. Three rules the
    blob approach broke:

      - a label inside the value stays ("... PTIN P01234567" keeps "PTIN")
      - every component survives ("Fremont, CA 1234" keeps a number on the end)
      - each component is replaced with the same KIND of thing
    """
    stripped = value.strip()
    if not stripped:
        return stripped

    if DATE_RE.fullmatch(stripped):
        return _date_like(stripped, fake)
    if "@" in stripped:
        return f"{fake.user_name()}@example.com"
    if re.fullmatch(r"[\d\-\s/().+]+", stripped):
        return _digits_like(stripped, fake)
    if looks_like_a_company(stripped):
        return generate(PiiType.ORG_PRIVATE, stripped)

    # "Fremont, CA 1234" is a place, and any trailing number is its postal part.
    # Handled before the token loop, because a comma means something different
    # here than in "Nguyen, Tuyet".
    place = re.fullmatch(
        r"(?P<city>[A-Za-z][A-Za-z .'\-]*),\s*(?P<state>[A-Z]{2})\.?(?:\s+(?P<code>[\d\-]+))?",
        stripped,
    )
    if place:
        out = f"{fake.city()}, {fake.random_element(_STATE_CODES)}"
        code = place.group("code")
        return f"{out} {_digits_like(code, fake)}" if code else out

    # "Nguyen, Tuyet ..." - surname first, given name after the comma.
    if re.match(r"^[A-Za-z'\-]+,\s+[A-Z][a-z]", stripped):
        head, _, tail = stripped.partition(",")
        rest = tail.strip().split()
        rebuilt = [f"{fake.last_name()},", fake.first_name()]
        for index, token in enumerate(rest[1:], start=1):
            fake.seed_instance(seed + index)
            if _is_inline_label(token):
                rebuilt.append(token)
            elif any(ch.isdigit() for ch in token):
                rebuilt.append(_reference_like(token, fake))
            else:
                rebuilt.append(fake.last_name())
        return " ".join(rebuilt)

    tokens = stripped.split()
    out: list[str] = []
    name_words_used = 0

    for index, token in enumerate(tokens):
        fake.seed_instance(seed + index)
        core = token.strip(".,:;()")
        trailing = token[len(token.rstrip(".,:;()")):]

        if _is_inline_label(token):
            out.append(token)                       # the form's own word
        elif core in US_STATES:
            out.append(fake.random_element(_STATE_CODES) + trailing)
        elif any(ch.isdigit() for ch in core) and any(ch.isalpha() for ch in core):
            out.append(_reference_like(core, fake) + trailing)   # P01234567
        elif core.isdigit():
            out.append(_digits_like(core, fake) + trailing)      # a ZIP or box
        elif core.isalpha():
            if len(core) <= 5 and core.isupper():
                out.append(_initials_like(core, seed + index) + trailing)
            else:
                replacement = fake.last_name() if name_words_used else fake.first_name()
                out.append(replacement + trailing)
                name_words_used += 1
        else:
            out.append(_reference_like(core, fake) + trailing)

    return " ".join(out)
