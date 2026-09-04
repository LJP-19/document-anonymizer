"""Local LLM auditor (spec sections 18-19).

Runs AFTER the deterministic, model and layout layers, and never drives
redaction geometry on its own. Asking a generative model for character offsets
invites hallucinated positions; instead it returns text, and that text is located
in the real document by exact search. Anything it names that cannot be found is
discarded.

Its job is the long tail nobody wrote a rule for - citizenship, place of birth,
sex, an identifier in an unusual format - and the reverse: flagging business
facts that the earlier layers wrongly claimed.

Model: Qwen2.5-1.5B-Instruct (Apache-2.0), ~1.1 GB at Q4_K_M, run through
llama.cpp entirely offline.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from ..document.model import Document, Line
from .types import Candidate, Evidence, PiiType, Source

log = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parents[2] / "resources" / "models" / "llm"
MODEL_FILE = "qwen2.5-1.5b-instruct-q4_k_m.gguf"

CONTEXT_TOKENS = 4096
MAX_OUTPUT_TOKENS = 512
MAX_CHARS_PER_CALL = 2400

#: What the auditor's free-text categories map onto in the taxonomy.
CATEGORY_MAP = {
    "name": PiiType.PERSON,
    "person": PiiType.PERSON,
    "date of birth": PiiType.DOB,
    "dob": PiiType.DOB,
    "birth": PiiType.DOB,
    "date": PiiType.PERSONAL_DATE,
    "citizenship": PiiType.CITIZENSHIP,
    "nationality": PiiType.CITIZENSHIP,
    "birthplace": PiiType.BIRTHPLACE,
    "place of birth": PiiType.BIRTHPLACE,
    "gender": PiiType.GENDER,
    "sex": PiiType.GENDER,
    "marital": PiiType.MARITAL_STATUS,
    "address": PiiType.ADDRESS,
    "email": PiiType.EMAIL,
    "phone": PiiType.PHONE,
    "ssn": PiiType.SSN,
    "social security": PiiType.SSN,
    "ein": PiiType.EIN,
    "tax": PiiType.TIN,
    "account": PiiType.BANK_ACCOUNT,
    "routing": PiiType.ROUTING_NUMBER,
    "license": PiiType.DRIVERS_LICENSE,
    "passport": PiiType.PASSPORT,
    "policy": PiiType.POLICY_NUMBER,
    "medical": PiiType.MRN,
    "employee": PiiType.EMPLOYEE_ID,
    "username": PiiType.USERNAME,
}

SYSTEM = "You return only valid JSON. No explanation, no markdown fences."

PROMPT = """You audit PII detection on a document that will be sent to an outside \
service for analysis. Identity must be removed; business facts must be kept.

TEXT:
{text}

ALREADY DETECTED: {found}

Return ONLY JSON in this shape:
{{"missed": [{{"text": "<exact substring copied from TEXT>", "type": "<category>"}}], \
"wrong": [{{"text": "<exact entry from ALREADY DETECTED>"}}]}}

"missed" = details identifying a specific person or their accounts that are NOT already \
detected: names, dates of birth, citizenship, place of birth, sex or gender, marital status, \
addresses, phone numbers, emails, and any identification or account numbers.

"wrong" = entries in ALREADY DETECTED that are business facts rather than identity.

NEVER list: money amounts, wages, totals, percentages, tax form or line numbers, tax years, \
job titles, or generic company names. Copy "text" exactly as it appears in TEXT, and copy the \
value only - never include its field label."""


class AuditorUnavailable(RuntimeError):
    pass


@dataclass
class AuditFinding:
    text: str
    category: str


class LlmAuditor:
    def __init__(self, model_dir: Path = MODEL_DIR, threads: int = 4):
        self.model_path = Path(model_dir) / MODEL_FILE
        self.threads = threads
        self._llm = None

    def load(self) -> None:
        if self._llm is not None:
            return
        try:
            from llama_cpp import Llama
        except ImportError as exc:
            raise AuditorUnavailable(f"llama-cpp-python not installed: {exc}") from exc
        if not self.model_path.exists():
            raise AuditorUnavailable(
                f"model missing at {self.model_path}. Run: python buildtools/fetch_models.py"
            )
        self._llm = Llama(
            model_path=str(self.model_path),
            n_ctx=CONTEXT_TOKENS,
            n_threads=self.threads,
            verbose=False,
        )

    @property
    def available(self) -> bool:
        try:
            self.load()
            return True
        except AuditorUnavailable:
            return False

    def audit(self, text: str, detected: list[str]) -> tuple[list[AuditFinding], list[str]]:
        """Returns (missed, wrongly_flagged)."""
        self.load()
        prompt = PROMPT.format(text=text[:MAX_CHARS_PER_CALL], found=json.dumps(detected[:60]))
        response = self._llm.create_chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=MAX_OUTPUT_TOKENS,
        )
        raw = response["choices"][0]["message"]["content"]
        return _parse(raw)


def _parse(raw: str) -> tuple[list[AuditFinding], list[str]]:
    cleaned = re.sub(r"^\s*```(?:json)?|```\s*$", "", raw.strip(), flags=re.M).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        return [], []
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        log.debug("auditor returned unparseable JSON")
        return [], []

    missed = []
    for item in data.get("missed", []) or []:
        if isinstance(item, dict) and item.get("text"):
            missed.append(AuditFinding(str(item["text"]).strip(), str(item.get("type", "")).lower()))
    wrong = []
    for item in data.get("wrong", []) or []:
        value = item.get("text") if isinstance(item, dict) else item
        if value:
            wrong.append(str(value).strip())
    return missed, wrong


def map_category(category: str) -> PiiType:
    lowered = category.lower()
    for key, pii_type in CATEGORY_MAP.items():
        if key in lowered:
            return pii_type
    return PiiType.UNCLASSIFIED_GROUP_VALUE


@lru_cache(maxsize=1)
def _auditor() -> LlmAuditor:
    return LlmAuditor()


def _locate(needle: str, lines: list[Line]) -> list[tuple[Line, int, int]]:
    """Find the model's text in the real document. Not found means discarded."""
    needle = needle.strip().strip(".,;:")
    if not needle:
        return []
    hits = []
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(needle)}(?![A-Za-z0-9])")
    for line in lines:
        for match in pattern.finditer(line.text):
            hits.append((line, match.start(), match.end()))
    return hits


def _pages_worth_auditing(doc: Document, candidates: list[Candidate]) -> set[int]:
    """Audit where the earlier layers were unsure, not everywhere.

    A full-document audit costs ~30-60s per page. Pages whose detections are all
    confident and classified rarely gain from a second opinion; pages with an
    unclassified value, a low-confidence hit, or no detections at all are where
    the misses live.
    """
    by_page: dict[int, list[Candidate]] = {}
    for candidate in candidates:
        by_page.setdefault(candidate.page_no, []).append(candidate)

    interesting: set[int] = set()
    for page in doc.pages:
        found = by_page.get(page.number, [])
        has_text = any(line.text.strip() for line in page.lines)
        if not has_text:
            continue
        if not found:
            interesting.add(page.number)
            continue
        if any(
            c.needs_review
            or c.confidence < 0.8
            or c.pii_type is PiiType.UNCLASSIFIED_GROUP_VALUE
            for c in found
        ):
            interesting.add(page.number)
    return interesting


def audit_document(
    doc: Document, candidates: list[Candidate], auditor: Optional[LlmAuditor] = None
) -> tuple[list[Candidate], list[str], list[str]]:
    """Returns (additional_candidates, wrongly_flagged_values, warnings)."""
    auditor = auditor or _auditor()
    warnings: list[str] = []
    try:
        auditor.load()
    except AuditorUnavailable as exc:
        warnings.append(f"LLM auditor disabled: {exc}")
        return [], [], warnings

    detected_by_page: dict[int, list[str]] = {}
    for candidate in candidates:
        detected_by_page.setdefault(candidate.page_no, []).append(candidate.normalized)

    additions: list[Candidate] = []
    wrong_total: list[str] = []

    pages_of_interest = _pages_worth_auditing(doc, candidates)
    for page in doc.pages:
        if page.number not in pages_of_interest:
            continue
        lines = page.lines
        if not lines:
            continue
        text = "\n".join(line.text for line in lines)
        if not text.strip():
            continue
        try:
            missed, wrong = auditor.audit(text, sorted(set(detected_by_page.get(page.number, []))))
        except Exception as exc:  # noqa: BLE001 - the audit is advisory, never fatal
            log.warning("auditor failed on page %s: %s", page.number + 1, type(exc).__name__)
            warnings.append(f"LLM auditor failed on page {page.number + 1}")
            continue

        wrong_total.extend(wrong)
        existing = [(c.line.key(), c.start, c.end) for c in candidates if c.page_no == page.number]

        for finding in missed:
            for line, start, end in _locate(finding.text, lines):
                if any(
                    key == line.key() and start < e and s < end for key, s, e in existing
                ):
                    continue
                rect = line.rect_for(start, end)
                if rect is None:
                    continue
                additions.append(
                    Candidate(
                        pii_type=map_category(finding.category),
                        text=line.text[start:end],
                        page_no=page.number,
                        rect=rect,
                        line=line,
                        start=start,
                        end=end,
                        confidence=0.7,
                        source=Source.AUDIT,
                        evidence=[
                            Evidence(Source.AUDIT, f"auditor: {finding.category or 'identity'}", 0.7)
                        ],
                        needs_review=True,
                        review_reason="found by the review pass, not by a rule or model",
                    )
                )
                existing.append((line.key(), start, end))

    return additions, wrong_total, warnings
