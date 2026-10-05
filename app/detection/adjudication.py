"""Evidence-based adjudication (spec sections 6-7).

Confidence has always been a single float, set once by whichever pass touched
a candidate last. Evidence has genuinely accumulated in `Candidate.evidence`
across passes - retyping, widening, propagation all append to it - but nothing
ever read that list back to decide anything. This module adds that read: an
aggregate score computed FROM the evidence, and a per-type threshold that
turns the score into one of three states.

This does not replace how any existing pass sets `.confidence` - it adds a
SEPARATE, later judgement on top, in `.adjudication`. A candidate no other
code has touched still gets a sensible default (CONFIRMED, matching prior
behaviour) so nothing that already worked changes unless the evidence and the
threshold for its type say otherwise.
"""

from __future__ import annotations

import math
from enum import Enum

from .types import Candidate, PiiType


class Adjudication(str, Enum):
    CONFIRMED = "CONFIRMED"
    PROBABLE = "PROBABLE"
    UNRESOLVED = "UNRESOLVED"


#: (confirm_at, probable_at) per type. Below probable_at -> UNRESOLVED.
#: Structured identifiers with a real checksum (SSN, EIN, IBAN, card numbers)
#: get a low bar once validated, because the validator itself is strong
#: evidence a regex match alone is not. Free-text categories (PERSON,
#: ORG_PRIVATE, ADDRESS) need more corroboration, because shape alone is
#: weak evidence for those - which is the entire history of this project's
#: false-positive bugs (form headings, tax terms, English phrases typed as
#: names).
DEFAULT_THRESHOLDS: dict[PiiType, tuple[float, float]] = {
    PiiType.SSN: (0.55, 0.4),
    PiiType.EIN: (0.55, 0.4),
    PiiType.ITIN: (0.55, 0.4),
    PiiType.TIN: (0.55, 0.4),
    PiiType.IBAN: (0.55, 0.4),
    PiiType.CARD_NUMBER: (0.55, 0.4),
    PiiType.ROUTING_NUMBER: (0.6, 0.45),
    PiiType.SWIFT_BIC: (0.6, 0.45),
    PiiType.UUID: (0.6, 0.45),
    PiiType.IP_ADDRESS: (0.6, 0.45),
    PiiType.MAC_ADDRESS: (0.6, 0.45),
    PiiType.EMAIL: (0.6, 0.45),
    PiiType.PHONE: (0.6, 0.45),
    PiiType.DOB: (0.65, 0.5),
    PiiType.PERSONAL_DATE: (0.65, 0.5),
    PiiType.PERSON: (0.68, 0.5),
    PiiType.ORG_PRIVATE: (0.68, 0.5),
    PiiType.ADDRESS: (0.65, 0.5),
    PiiType.STREET: (0.65, 0.5),
    PiiType.CITY_STATE: (0.65, 0.5),
    PiiType.POSTAL_CODE: (0.6, 0.45),
}
#: Anything not listed above - free-form IDs, categorical fields, and the
#: like - uses this. Deliberately permissive at the "probable" edge, since
#: these types are usually label-bound (a label match is already strong
#: evidence) rather than shape-detected.
FALLBACK_THRESHOLD = (0.65, 0.45)


def aggregate_confidence(candidate: Candidate) -> float:
    """Combine every evidence entry's weight into one score.

    Noisy-OR: `1 - product(1 - w_i)` for positive weights, so several weak
    signals agreeing can add up to something strong without any single one
    exceeding 1.0, and a negative-weight entry pulls the score down instead
    of being ignored. Falls back to the candidate's own `.confidence` when
    there is no evidence to aggregate - most candidates from before this
    module existed have exactly one implicit signal: whichever detector
    made them, at whatever confidence it assigned directly.
    """
    if not candidate.evidence:
        return candidate.confidence

    positive_product = 1.0
    negative_sum = 0.0
    for item in candidate.evidence:
        if item.weight > 0:
            positive_product *= (1.0 - min(item.weight, 0.99))
        elif item.weight < 0:
            negative_sum += item.weight

    aggregated = 1.0 - positive_product
    if aggregated <= 0.0:
        aggregated = candidate.confidence  # no positive evidence recorded
    return max(0.0, min(1.0, aggregated + negative_sum))


def adjudicate(candidate: Candidate, thresholds: dict = None) -> Adjudication:
    """Confirmed / probable / unresolved, from aggregated evidence and a
    per-type threshold rather than one universal cutoff."""
    thresholds = thresholds or DEFAULT_THRESHOLDS
    confirm_at, probable_at = thresholds.get(candidate.pii_type, FALLBACK_THRESHOLD)
    score = aggregate_confidence(candidate)
    if score >= confirm_at:
        return Adjudication.CONFIRMED
    if score >= probable_at:
        return Adjudication.PROBABLE
    return Adjudication.UNRESOLVED
