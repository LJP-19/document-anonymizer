"""Detection primitives and the PII taxonomy (spec sections 11 and 13)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..document.model import Line, Rect


class PiiType(str, Enum):
    # Identity
    PERSON = "PERSON"
    ORG_PRIVATE = "ORG_PRIVATE"
    # Tax identifiers
    SSN = "SSN"
    ITIN = "ITIN"
    EIN = "EIN"
    TIN = "TIN"
    IP_PIN = "IP_PIN"
    STATE_TAX_ID = "STATE_TAX_ID"
    # Contact
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    FAX = "FAX"
    URL_PERSONAL = "URL_PERSONAL"
    SOCIAL_HANDLE = "SOCIAL_HANDLE"
    # Address
    ADDRESS = "ADDRESS"
    STREET = "STREET"
    CITY_STATE = "CITY_STATE"
    POSTAL_CODE = "POSTAL_CODE"
    PO_BOX = "PO_BOX"
    # Dates
    DOB = "DOB"
    PERSONAL_DATE = "PERSONAL_DATE"
    # Financial
    BANK_ACCOUNT = "BANK_ACCOUNT"
    ROUTING_NUMBER = "ROUTING_NUMBER"
    IBAN = "IBAN"
    SWIFT_BIC = "SWIFT_BIC"
    CARD_NUMBER = "CARD_NUMBER"
    # Employment
    EMPLOYEE_ID = "EMPLOYEE_ID"
    PAYROLL_ID = "PAYROLL_ID"
    # Government / licences
    DRIVERS_LICENSE = "DRIVERS_LICENSE"
    PASSPORT = "PASSPORT"
    MEDICARE_ID = "MEDICARE_ID"
    # Healthcare / insurance
    POLICY_NUMBER = "POLICY_NUMBER"
    MEMBER_ID = "MEMBER_ID"
    MRN = "MRN"
    CLAIM_NUMBER = "CLAIM_NUMBER"
    # Legal
    CASE_NUMBER = "CASE_NUMBER"
    MATTER_ID = "MATTER_ID"
    # Digital
    USERNAME = "USERNAME"
    CUSTOMER_ID = "CUSTOMER_ID"
    ACCOUNT_ID = "ACCOUNT_ID"
    UUID = "UUID"
    IP_ADDRESS = "IP_ADDRESS"
    MAC_ADDRESS = "MAC_ADDRESS"
    # Structural
    UNCLASSIFIED_GROUP_VALUE = "UNCLASSIFIED_GROUP_VALUE"


#: Types whose replacement should preserve rough visual length.
NUMERIC_LIKE = {
    PiiType.SSN, PiiType.ITIN, PiiType.EIN, PiiType.TIN, PiiType.IP_PIN,
    PiiType.STATE_TAX_ID, PiiType.POSTAL_CODE, PiiType.BANK_ACCOUNT,
    PiiType.ROUTING_NUMBER, PiiType.CARD_NUMBER, PiiType.EMPLOYEE_ID,
    PiiType.PAYROLL_ID, PiiType.MEDICARE_ID, PiiType.POLICY_NUMBER,
    PiiType.MEMBER_ID, PiiType.MRN, PiiType.CLAIM_NUMBER, PiiType.CASE_NUMBER,
    PiiType.MATTER_ID, PiiType.CUSTOMER_ID, PiiType.ACCOUNT_ID,
    PiiType.DRIVERS_LICENSE, PiiType.PASSPORT,
}


class Source(str, Enum):
    REGEX = "regex"
    NER = "ner"
    GROUP = "group"
    COVERAGE = "coverage"
    MANUAL = "manual"


@dataclass
class Evidence:
    source: Source
    detail: str
    weight: float = 0.0


@dataclass
class Candidate:
    """A detected span of possible PII, anchored to exact page geometry."""

    pii_type: PiiType
    text: str
    page_no: int
    rect: Rect
    line: Line
    start: int  # offset within line.text
    end: int
    confidence: float
    source: Source
    evidence: list[Evidence] = field(default_factory=list)
    group_id: Optional[str] = None
    needs_review: bool = False
    review_reason: str = ""

    @property
    def id(self) -> str:
        return f"{self.page_no}:{self.line.block_no}:{self.line.line_no}:{self.start}:{self.end}"

    @property
    def normalized(self) -> str:
        return " ".join(self.text.split()).strip(" .,;:")

    def overlaps(self, other: "Candidate") -> bool:
        return (
            self.line.key() == other.line.key()
            and self.start < other.end
            and other.start < self.end
        )


@dataclass
class LabelRegion:
    """A field label (spec section 13). Labels are never redacted."""

    text: str
    line: Line
    start: int
    end: int
    rect: Rect
    expected_types: list[PiiType]
    non_pii: bool = False  # e.g. "Annual Salary" - its values must be preserved
    unknown: bool = False  # looks like a label but is not in the taxonomy

    @property
    def page_no(self) -> int:
        return self.line.page_no


@dataclass
class LogicalFieldGroup:
    """A label plus the value lines that belong to it (spec sections 15-16)."""

    group_id: str
    label: LabelRegion
    value_lines: list[Line]
    expected_types: list[PiiType]
    detected_types: set[PiiType] = field(default_factory=set)
    complete: bool = True
    reason: str = ""

    @property
    def page_no(self) -> int:
        return self.label.page_no

    @property
    def missing_types(self) -> list[PiiType]:
        return [t for t in self.expected_types if t not in self.detected_types]

    def describe(self) -> str:
        return f"{self.label.text.strip()} -> {len(self.value_lines)} value line(s)"


@dataclass
class DetectionResult:
    candidates: list[Candidate]
    groups: list[LogicalFieldGroup]
    labels: list[LabelRegion]
    warnings: list[str] = field(default_factory=list)
