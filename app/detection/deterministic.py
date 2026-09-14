"""Deterministic rule-based detection (spec section 10).

All patterns live in resources/rules/pii_rules.yaml so the taxonomy can be
extended without editing Python (spec section 46).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from ..document.model import Document, Line
from .types import Candidate, Evidence, PiiType, Source

RULES_PATH = Path(__file__).resolve().parents[2] / "resources" / "rules" / "pii_rules.yaml"

# Currency / financial tokens that must survive untouched (spec section 12).
# A parenthesised number only counts as an accounting negative when it carries
# a currency symbol or a thousands separator. Without that guard "(555) 123-4567"
# is misread as money, which both corrupts the financial-integrity check and
# terminates logical field groups early.
MONEY_RE = re.compile(
    r"\$\s?-?[\d,]+(?:\.\d{1,2})?"
    r"|\(\s?\$\s?[\d,]+(?:\.\d{1,2})?\s?\)"
    r"|\(\s?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?\s?\)"
)
PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s?%")


@dataclass(frozen=True)
class Rule:
    name: str
    pii_type: PiiType
    regex: re.Pattern
    confidence: float
    validator: str = "none"
    context_any: tuple[str, ...] = ()
    context_required: bool = False
    money_guard: bool = False


@dataclass(frozen=True)
class LabelRule:
    regex: re.Pattern
    expects: tuple[PiiType, ...]


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[Rule, ...]
    labels: tuple[LabelRule, ...]
    non_pii_labels: tuple[re.Pattern, ...]


@lru_cache(maxsize=4)
def load_rules(path: str = str(RULES_PATH)) -> RuleSet:
    data = yaml.safe_load(Path(path).read_text())
    rules = tuple(
        Rule(
            name=r["name"],
            pii_type=PiiType[r["type"]],
            regex=re.compile(r["pattern"]),
            confidence=float(r.get("confidence", 0.7)),
            validator=r.get("validator", "none"),
            context_any=tuple(w.lower() for w in r.get("context_any", [])),
            context_required=bool(r.get("context_required", False)),
            money_guard=bool(r.get("money_guard", False)),
        )
        for r in data.get("rules", [])
    )
    labels = tuple(
        LabelRule(
            regex=re.compile(r"\b(?:" + lb["pattern"] + r")\b", re.I),
            expects=tuple(PiiType[t] for t in lb.get("expects", [])),
        )
        for lb in data.get("labels", [])
    )
    non_pii = tuple(
        re.compile(r"\b(?:" + p + r")\b", re.I) for p in data.get("non_pii_labels", [])
    )
    return RuleSet(rules=rules, labels=labels, non_pii_labels=non_pii)


# -- validators -------------------------------------------------------------


def valid_ssn(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if len(digits) != 9:
        return False
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    if area in ("000", "666") or area.startswith("9"):
        return False
    return group != "00" and serial != "0000"


def valid_ein(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return len(digits) == 9 and digits[:2] not in ("00", "07", "08", "09", "17", "18", "19", "28", "29", "49", "69", "70", "78", "79", "89")


def valid_luhn(value: str) -> bool:
    digits = [int(c) for c in re.sub(r"\D", "", value)]
    if not 13 <= len(digits) <= 19:
        return False
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def valid_aba(value: str) -> bool:
    d = re.sub(r"\D", "", value)
    if len(d) != 9:
        return False
    checksum = (
        3 * (int(d[0]) + int(d[3]) + int(d[6]))
        + 7 * (int(d[1]) + int(d[4]) + int(d[7]))
        + (int(d[2]) + int(d[5]) + int(d[8]))
    )
    return checksum % 10 == 0


VALIDATORS = {
    "ssn": valid_ssn,
    "ein": valid_ein,
    "luhn": valid_luhn,
    "aba": valid_aba,
    "none": lambda _v: True,
}


# -- detection --------------------------------------------------------------


COLUMN_HEADER_LOOKBACK = 8


def _context_window(line: Line, lines_by_page: dict[int, list[Line]]) -> str:
    """Same line, the line directly above, and any column header above it.

    Table rows carry no label of their own; the label lives in the column
    header several rows up (spec section 22). Looking only one line up meant the
    second row of a table lost the context the first row had.
    """
    parts = [line.text]
    siblings = lines_by_page.get(line.page_no, [])

    above = [other for other in siblings if other.bbox[3] <= line.bbox[1] + 1]
    adjacent = [
        o for o in above if line.bbox[1] - o.bbox[3] < 1.8 * max(line.height, 1.0)
    ]
    if adjacent:
        parts.append(max(adjacent, key=lambda ln: ln.bbox[3]).text)

    # Column-aligned lines above: horizontal overlap with this line's extent.
    column = [
        o
        for o in above
        if not (o.bbox[2] < line.bbox[0] - 2 or line.bbox[2] < o.bbox[0] - 2)
    ]
    column.sort(key=lambda ln: -ln.bbox[3])
    parts.extend(ln.text for ln in column[:COLUMN_HEADER_LOOKBACK])

    return " ".join(parts).lower()


def _inside_money(line_text: str, start: int, end: int) -> bool:
    for m in list(MONEY_RE.finditer(line_text)) + list(PERCENT_RE.finditer(line_text)):
        if m.start() <= start and end <= m.end():
            return True
    # A number immediately preceded by a currency symbol is a financial value.
    prefix = line_text[max(0, start - 2):start]
    return "$" in prefix


def detect_deterministic(doc: Document, ruleset: Optional[RuleSet] = None) -> list[Candidate]:
    rs = ruleset or load_rules()
    lines_by_page: dict[int, list[Line]] = {}
    for page in doc.pages:
        lines_by_page[page.number] = page.lines

    out: list[Candidate] = []
    for page in doc.pages:
        for line in page.lines:
            context = _context_window(line, lines_by_page)
            for rule in rs.rules:
                for m in rule.regex.finditer(line.text):
                    has_ctx = any(w in context for w in rule.context_any) if rule.context_any else False
                    if rule.context_required and not has_ctx:
                        continue
                    if rule.money_guard and _inside_money(line.text, m.start(), m.end()):
                        continue
                    structurally_valid = VALIDATORS[rule.validator](m.group())
                    if not structurally_valid and not has_ctx:
                        # No context and it fails its own checksum: drop it.
                        continue
                    rect = line.rect_for(m.start(), m.end())
                    if rect is None:
                        continue
                    conf = min(0.99, rule.confidence + (0.08 if has_ctx else 0.0))
                    evidence = [Evidence(Source.REGEX, f"rule={rule.name}", rule.confidence)]
                    if has_ctx:
                        evidence.append(Evidence(Source.REGEX, "label context nearby", 0.08))
                    review, reason = False, ""
                    if not structurally_valid:
                        # Shape matches and the label says this is the field, but
                        # the checksum/structure fails. Never discard it - that is
                        # a silent false negative (spec section 86).
                        conf = min(conf, 0.6)
                        review = True
                        reason = (
                            f"matches {rule.pii_type.value} in a labelled field but fails "
                            "structural validation - confirm before accepting"
                        )
                        evidence.append(
                            Evidence(Source.REGEX, f"{rule.validator} validation failed", -0.3)
                        )
                    out.append(
                        Candidate(
                            pii_type=rule.pii_type,
                            text=m.group(),
                            page_no=page.number,
                            rect=rect,
                            line=line,
                            start=m.start(),
                            end=m.end(),
                            confidence=conf,
                            source=Source.REGEX,
                            evidence=evidence,
                            needs_review=review,
                            review_reason=reason,
                        )
                    )
    return out


def financial_tokens(text: str) -> list[str]:
    """Currency and percentage tokens used by the integrity check (section 41)."""
    return sorted(
        [m.group().replace(" ", "") for m in MONEY_RE.finditer(text)]
        + [m.group().replace(" ", "") for m in PERCENT_RE.finditer(text)]
    )
