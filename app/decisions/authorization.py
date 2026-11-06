"""The ONE authorization policy: may a candidate be pseudonymized WITHOUT a person?

Owner's rule (0.42.0): fail closed. Only CONFIRMED PII is transformed
automatically; everything plausible-but-unconfirmed waits, readable, for an
explicit Redact / Keep / Edit; confirmed non-PII is never touched (that part is
the protection index and the gate - see detection/protection.py).

Used in three places, never re-implemented:
  * `DecisionManager.register()` - the default decision for a new candidate;
  * `transform.plan.build_plan()` - re-checks every AUTOMATIC approval at the
    transformation boundary, so a candidate wrongly marked ACCEPTED by any earlier
    component is refused there;
  * bulk operations (CLI --accept-all, batch, "Redact everything") - they may only
    approve what this policy auto-approves.

A detection SOURCE is never evidence on its own: a regex match, a group id, a
confidence score or a model label does not authorize anything. The evidence that
does (any one is enough):
  1. the value of a recognised, typed identity LABEL whose expected types include
     this candidate's type (verified binding, `annotate_label_binding`);
  2. a specific identifier (SSN, ITIN, EIN, phone, e-mail, account ...) whose rule
     matched WITH supporting context and whose validator passed;
  3. an occurrence of an entity a label already confirmed (propagation, a name
     control, or a name made only of a confirmed person's name words).
And nothing flagged for review, ambiguous, untyped, an organization, UNRESOLVED or
PROBABLE, or with a failed validator is ever auto-approved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from ..detection.types import Candidate, Evidence, PiiType, Source


class AuthOrigin(str, Enum):
    """WHO authorized a decision. ACCEPTED alone no longer says."""

    AUTO_APPROVED = "AUTO_APPROVED"        # this policy, at registration
    REVIEW_REQUIRED = "REVIEW_REQUIRED"    # this policy said no; waiting for a person
    USER_APPROVED = "USER_APPROVED"        # a person pressed Redact
    USER_EDITED = "USER_EDITED"            # a person typed the replacement
    USER_KEPT = "USER_KEPT"                # a person pressed Keep
    USER_ADDED = "USER_ADDED"              # a person added it by hand
    REJECTED = "REJECTED"                  # refused at the transformation boundary


USER_ORIGINS = {AuthOrigin.USER_APPROVED, AuthOrigin.USER_EDITED, AuthOrigin.USER_ADDED}

#: Specific identifiers: a validated rule match WITH context is confirmation.
SPECIFIC_IDS = {
    PiiType.SSN, PiiType.ITIN, PiiType.EIN, PiiType.TIN, PiiType.IP_PIN, PiiType.PHONE,
    PiiType.EMAIL, PiiType.CARD_NUMBER, PiiType.IBAN, PiiType.ROUTING_NUMBER,
}
for _name in ("BANK_ACCOUNT", "ACCOUNT_ID", "FAX", "PASSPORT", "DRIVERS_LICENSE", "MEDICARE_ID",
              "STATE_TAX_ID", "EMPLOYEE_ID", "PAYROLL_ID", "MEMBER_ID", "POLICY_NUMBER"):
    if hasattr(PiiType, _name):
        SPECIFIC_IDS.add(getattr(PiiType, _name))

#: Types that may stand in for each other under one label ("Home address" holds a
#: street, a city/state/ZIP line or a ZIP; "TIN" holds an SSN).
_FAMILIES = [
    {PiiType.ADDRESS, PiiType.STREET, PiiType.CITY_STATE, PiiType.POSTAL_CODE},
    {PiiType.SSN, PiiType.ITIN, PiiType.TIN, PiiType.EIN},
]
for _name in ("PO_BOX",):
    if hasattr(PiiType, _name):
        _FAMILIES[0].add(getattr(PiiType, _name))


def compatible(pii_type: PiiType, expected: Iterable[PiiType]) -> bool:
    expected = set(expected)
    if pii_type in expected:
        return True
    return any(pii_type in fam and expected & fam for fam in _FAMILIES)


#: Review flags that only express ONE DETECTOR'S doubt about its own type or
#: score. A verified, typed field label is the better witness and resolves them.
#: Every other flag (failed validation, an AI objection, a soft-protection
#: conflict, ambiguous context, an incomplete group, a type conflict, a
#: second-pass finding, low aggregated evidence) is never cleared by a label.
LABEL_RESOLVABLE_DOUBTS = (
    "matched by name shape rather than by the language model",
    "location entity outside a labelled address group",
    "a known person's name, typed as a place by the model",
    "low model confidence",
    "low presidio confidence",
    "the model called this an address, but it has no number",
    # Two detectors typed the same span differently, and the verified label of
    # the winner's type settles it ("Account Number: 123456789" - bank account,
    # not the SSN its shape also fits).
    "detectors disagree:",
    # A note about the field GROUP (another expected type was not found in it),
    # not doubt about this value: holding the confirmed value protects nothing.
    "partial / incomplete detection",
)

#: Soft protection (a line that repeats on every page, static boilerplate) is a
#: structural GUESS. An EXACT occurrence of an entity a label confirmed outranks it:
#: a filer's name printed in every page header is still the filer's name. Hard
#: protection (labels, figures, prior replacements) is never overridden - the gate
#: rejects those candidates outright.
SOFT_PROTECTION_DOUBTS = ("sits on running header text", "sits on running footer text",
                          "sits on static boilerplate text", "sits on form instruction text")


def _only_detector_doubt(reason: str) -> bool:
    parts = [p.strip().lower() for p in (reason or "").split(";") if p.strip()]
    return bool(parts) and all(any(p.startswith(d) for d in LABEL_RESOLVABLE_DOUBTS) for p in parts)


def annotate_label_binding(candidates: list, groups: list) -> None:
    """Record VERIFIED label binding on each candidate (`label_confirmed`).

    A group id alone is not proof: `groups._cover_span` attaches the id of any
    group to every detection that merely overlaps one of its value lines, and an
    UNKNOWN label-shaped caption creates a group too. Bound means: the group's
    label is recognised, typed, not a non-PII label, and expects this type.
    """
    by_id = {g.group_id: g for g in groups or []}
    for c in candidates:
        ok = False
        g = by_id.get(c.group_id) if c.group_id else None
        if g is not None and not g.label.unknown and not g.label.non_pii and g.expected_types:
            ok = compatible(c.pii_type, g.expected_types)
        if not ok:
            for e in c.evidence:
                m = re.match(r"typed (\w+) from the '.*' column", e.detail)
                if m and m.group(1) == c.pii_type.value:
                    ok = True
        c.label_confirmed = ok
        if ok and c.needs_review and _only_detector_doubt(c.review_reason):
            c.needs_review = False
            c.evidence.append(Evidence(Source.GROUP, f"label resolved detector doubt: {c.review_reason}", 0.0))
            c.review_reason = ""


def _evidence(c: Candidate) -> str:
    return " | ".join(e.detail for e in c.evidence).lower()


#: Words a caption uses to NAME each identifier type. A rule's context list is
#: wider ("taxpayer", "employee", "identification") - fine for finding a value,
#: not for authorizing one: "Taxpayer name:" one line above made a reference
#: number an "SSN". Without a verified label, the identifier's own line must
#: name its type before the value.
_TYPE_WORDS = {
    "SSN": ("ssn", "social security", "ss#", "soc. sec"),
    "ITIN": ("itin", "individual taxpayer identification"),
    "EIN": ("ein", "employer identification", "fein", "federal id", "federal tax id"),
    "TIN": ("tin", "taxpayer identification", "tax id"),
    "IP_PIN": ("ip pin", "identity protection pin"),
    "PHONE": ("phone", "tel", "telephone", "mobile", "cell"),
    "FAX": ("fax",),
    "EMAIL": ("email", "e-mail"),
    "ROUTING_NUMBER": ("routing", "aba", "rtn"),
    "BANK_ACCOUNT": ("account",),
    "ACCOUNT_ID": ("account",),
    "IBAN": ("iban",),
    "CARD_NUMBER": ("card",),
    "EMPLOYEE_ID": ("employee", "badge", "staff"),
    "PAYROLL_ID": ("payroll",),
    "MEMBER_ID": ("member",),
    "MEDICARE_ID": ("medicare",),
    "STATE_TAX_ID": ("state", "tax id"),
    "PASSPORT": ("passport",),
    "DRIVERS_LICENSE": ("license", "licence", "dl"),
    "POLICY_NUMBER": ("policy",),
}


def _names_its_own_type(c: Candidate) -> bool:
    words = _TYPE_WORDS.get(c.pii_type.value)
    if not words or c.line is None:
        return False
    before = (c.line.text[: c.start] or "").lower()
    return any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", before) for w in words)


def _own_validator_failed(c: Candidate) -> bool:
    """Did the validator of the rule that typed THIS candidate fail?

    Evidence from an overlapping detector of another type is merged into the
    winner (resolve._absorb): "021000021" under "Routing Number" carries the SSN
    rule's failed checksum, which says nothing about a routing number."""
    failed = [e.detail.split(" validation failed")[0].strip().lower()
              for e in c.evidence if "validation failed" in e.detail]
    if not failed:
        return False
    from ..detection.deterministic import load_rules

    rules = {r.name: r for r in load_rules().rules}
    own = {rules[e.detail[5:]].validator for e in c.evidence
           if e.detail.startswith("rule=") and e.detail[5:] in rules
           and rules[e.detail[5:]].pii_type is c.pii_type}
    if not own:
        return True  # cannot tell whose check failed: fail closed
    return any(v in own for v in failed)


@dataclass
class Assessment:
    eligible: bool
    reason: str


def _name_words(text: str) -> list[str]:
    from ..detection.protection import _name_tokens

    return _name_tokens(text)


def confirmed_name_words(candidates: Iterable[Candidate]) -> set:
    """Name words of people a LABEL confirmed (and who are not held)."""
    words: set = set()
    for c in candidates:
        if (c.pii_type is PiiType.PERSON and getattr(c, "label_confirmed", False)
                and not c.needs_review and not getattr(c, "ambiguous", False)):
            words.update(_name_words(c.text))
    return words


def assess(c: Candidate, confirmed_words: Optional[set] = None) -> Assessment:
    """May `c` be transformed with no person deciding? With the reason either way."""
    if c.source is Source.MANUAL:
        return Assessment(True, "added by the user")
    if c.needs_review:
        exact_entity = (c.source is Source.COVERAGE and not getattr(c, "ambiguous", False)
                        and any(e.detail.startswith("same value as an entity") for e in c.evidence))
        parts = [p.strip().lower() for p in (c.review_reason or "").split(";") if p.strip()]
        if not (exact_entity and parts and all(p.startswith(SOFT_PROTECTION_DOUBTS) for p in parts)):
            why = (c.review_reason or "flagged for review").strip()
            return Assessment(False, f"flagged for review: {why}")
    if getattr(c, "ambiguous", False):
        return Assessment(False, "the context leaves it unclear whose this is")
    if getattr(c, "blackout", False):
        return Assessment(False, "a blackout is the user's decision")
    if c.pii_type is PiiType.UNCLASSIFIED_GROUP_VALUE:
        return Assessment(False, "nothing could say what kind of value this is")
    if c.pii_type is PiiType.ORG_PRIVATE:
        return Assessment(False, "organization or employer name - not PII unless you decide it is")
    ev = _evidence(c)
    if _own_validator_failed(c):
        return Assessment(False, "fails its structural validation")
    if (getattr(c, "adjudication", "") or "") == "UNRESOLVED":
        return Assessment(False, "evidence is unresolved, not confirmed")
    # A reliable occurrence of an entity a LABEL confirmed is confirmed by that
    # label, not by the model's score for this occurrence - checked before the
    # model-adjudication veto. (needs_review and ambiguity above still veto.)
    if c.pii_type is PiiType.PERSON and confirmed_words and c.source is not Source.REGEX:
        words = _name_words(c.text)
        if words and all(w in confirmed_words for w in words):
            return Assessment(True, "made only of a label-confirmed person's name words")
    adjudication = getattr(c, "adjudication", "") or ""
    if adjudication in ("UNRESOLVED", "PROBABLE"):
        return Assessment(False, f"evidence is {adjudication.lower()}, not confirmed")

    if getattr(c, "label_confirmed", False):
        return Assessment(True, "value of a labelled field of this type")
    if (c.source is Source.REGEX and c.pii_type in SPECIFIC_IDS and "label context nearby" in ev
            and _names_its_own_type(c)):
        return Assessment(True, "specific identifier, validated, named by its own caption")
    if c.source is Source.COVERAGE and ("name control (" in ev or "same value as an entity" in ev):
        # Propagation only starts from subjects a label or a deterministic rule
        # confirmed (entities_pass.identify_subjects).
        return Assessment(True, "another occurrence of an entity a label confirmed")
    return Assessment(False, "no label, identifier context or confirmed entity supports it")
