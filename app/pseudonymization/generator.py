"""Deterministic, offline pseudonym generation (spec sections 25 and 36).

Same input + same scope seed => same pseudonym, every run, no state file needed.
Replacements aim to match the original's visual length so that layout survives.
"""

from __future__ import annotations

import hashlib
import re
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


def generate(pii_type: PiiType, value: str, scope: str = "document") -> str:
    """Return a stable pseudonym for `value` of type `pii_type`."""
    seed = _seed(scope, pii_type, value)
    fake = Faker("en_US")
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

    if pii_type is PiiType.UNCLASSIFIED_GROUP_VALUE:
        return _pseudonym_for_unclassified(value, fake, seed)

    # Everything else: keep the shape, change every character.
    return _mask_preserving_shape(value, seed)


def _pseudonym_for_unclassified(value: str, fake: Faker, seed: int) -> str:
    """A value line inside a sensitive group that no detector classified."""
    stripped = value.strip()
    if re.fullmatch(r"[\d\-\s]+", stripped):
        return _mask_preserving_shape(stripped, seed)
    if re.search(r",\s*[A-Z]{2}\b", stripped):
        return f"{fake.city()}, {fake.random_element(_STATE_CODES)}"
    if re.match(r"^\d+\s", stripped):
        return fake.street_address()
    tokens = stripped.split()
    if stripped.replace(" ", "").isalpha():
        # A short single token (initials, a surname fragment) must stay short -
        # swapping "LJP" for "Elizabeth Hernandez" wrecks the layout.
        if len(tokens) == 1 and len(stripped) <= 6:
            return _mask_preserving_shape(stripped, seed)
        if len(tokens) <= 4:
            return _fit_length([fake.name() for _ in range(6)], len(stripped))
    return _mask_preserving_shape(stripped, seed)
