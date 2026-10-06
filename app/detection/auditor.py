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
llama.cpp entirely offline. REVERTED from Q8_0 (~1.89 GB) after a real,
hard platform constraint this project did not previously account for:
GitHub Releases rejects any single asset over 2 GB (2147483648 bytes =
exactly 2^31) - confirmed directly from a real failed release upload:
"size must be less than 2147483648". The Windows with-LLM zip at Q8_0
came in around 2.2 GB, over that limit; the BUILD succeeded while the
RELEASE PUBLISH step failed, wasting a full build cycle on a problem
invisible until the very last step. This is a tighter, non-negotiable
ceiling than the "below 4 GB" total-app-size figure used when Q8_0 was
chosen - that number was about disk/download size in general, not about
what GitHub's own Release mechanism will accept as a single file, and
the latter wins when the two conflict. Estimated (comparing the real
no-LLM Windows zip against the real Q8_0 with-LLM zip suggests the model
file itself compresses by roughly 24% - it is quantized binary data, not
highly compressible like the rest of the app) that Q4_K_M's smaller raw
size gives a real ~400+ MB margin under 2 GB, not just barely fitting -
not yet re-confirmed by an actual build. verify_bundle.py now checks the
packaged artifact's real size against this same limit before the
expensive release-upload step runs. If a future size change is
considered: check this constraint FIRST (GitHub's 2 GB-per-asset ceiling
applies regardless of any total-app-size preference), not the general
disk-size budget - and verify any precision/model-size change with a real
build's actual zip size before trusting an estimate again, the way this
one should have been checked before shipping instead of after.
"""

from __future__ import annotations

import json
import logging
import time
import threading
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from ..document.model import Document, Line
from .protection import is_protected_figure_text
from .textnorm import find_occurrences, fold
from .types import Candidate, Evidence, PiiType, Source

log = logging.getLogger(__name__)

# No module-level MODEL_DIR/MODEL_FILE constants anymore - the model is
# chosen by the user in the mandatory first-launch picker
# (app/ui/model_picker.py), not fixed at build time. See
# LlmAuditor.__init__ and app/llm/selection.py for where the real path
# comes from.

# Qwen2.5-7B supports up to 32K context; this stays modest rather than
# matching that ceiling. A larger context window does not improve judgment
# on the page-sized chunks this auditor actually sees (MAX_CHARS_PER_CALL
# below caps each call well under the old 4096-token ceiling already), and
# a bigger n_ctx allocation costs real memory before a single token is
# generated. Revisit if a real need for longer context appears - do not
# raise this "to be safe" without one.
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
    "business": PiiType.ORG_PRIVATE,
    "company": PiiType.ORG_PRIVATE,
    "employer": PiiType.ORG_PRIVATE,
    "organization": PiiType.ORG_PRIVATE,
}

SYSTEM = "You return only valid JSON. No explanation, no markdown fences."

#: Grammar-constrained decoding, not just a prompt instruction. A prompt
#: asking for "ONLY JSON" is still a suggestion the model can drift from
#: over a long or unusual page; a grammar built from this schema makes
#: malformed or off-shape output structurally impossible to generate,
#: rather than merely discouraged. Verified directly against the real
#: llama-cpp-python API (LlamaGrammar.from_json_schema) before using it -
#: not assumed from documentation alone. The free-text parsing below
#: (_parse/_parse_verdicts) stays as a safety net: grammar construction
#: itself is wrapped in try/except and falls back to the unconstrained
#: prompt-only path if it ever fails, matching this module's existing
#: rule that the audit is advisory and must never be fatal.
#: Every type the model may name. The grammar enforces it, so a proposal's type is
#: always one of OUR taxonomy - never free text that has to be guessed at.
AI_TYPES = [t.value for t in PiiType if t is not PiiType.UNCLASSIFIED_GROUP_VALUE]

_FINDING = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "type": {"type": "string", "enum": AI_TYPES},
    },
    # No "reason" field: asking a 0.5B model to justify every finding made its output
    # ~10x longer (9 s -> 90 s measured) and pushed it past the output cap.
    "required": ["text", "type"],
}
AUDIT_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "missed": {"type": "array", "items": _FINDING},
        "uncertain": {"type": "array", "items": _FINDING},
        "wrong": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    "required": ["missed", "uncertain", "wrong"],
})

ADJUDICATE_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["identity", "form", "business"],
                    },
                },
                "required": ["text", "kind"],
            },
        },
    },
    "required": ["verdicts"],
})


@lru_cache(maxsize=4)
def _grammar(schema: str):
    """Built once per schema, not per call - grammar construction has real
    overhead and the two schemas above are static.

    The import is deliberately INSIDE the try/except, not above it. A real,
    reported CI failure proved why: llama_cpp is optional and genuinely not
    installed in the standard test environment (requirements-llm.txt is
    kept out of the default install on purpose). With the import outside
    the try/except, that produces an uncaught ModuleNotFoundError here,
    which the CALLER's own try/except then catches - but that caller
    catches it around the ENTIRE model call, so the effect is silently
    skipping the whole chunk/page rather than falling back to a
    grammar-less call the way this function's docstring already promised.
    Confirmed directly: reproduced the exact CI failure by uninstalling
    llama_cpp locally, which logged "adjudication failed on page 1:
    ModuleNotFoundError" and left the whole page unprocessed - not a
    graceful fallback, a silent skip. Moving the import inside the
    try/except means a missing llama_cpp is handled the same way as any
    other grammar-build failure: this function returns None, and the
    caller proceeds with grammar=None (a valid value the real API
    accepts), still making the actual model call instead of abandoning it.
    """
    try:
        from llama_cpp import LlamaGrammar

        return LlamaGrammar.from_json_schema(schema, verbose=False)
    except Exception:  # noqa: BLE001 - fall back to prompt-only, never fatal
        log.warning("failed to build grammar from schema; falling back to prompt-only JSON")
        return None


ADJUDICATE_PROMPT = """You are checking a redaction plan before it runs on a document \
that will be sent to an outside service for analysis.

PAGE TEXT:
{text}

PROPOSED FOR REDACTION:
{proposed}

For each proposed item decide what it actually is:
  "identity"  - it identifies a specific person, household or their accounts
  "form"      - it is the document's own text: a field label, heading, caption, \
instruction, form or line number, or boilerplate
  "business"  - it is a business or tax fact: an amount, a rate, a date of a \
transaction, an occupation, a filing status, a generic company or agency name

Return ONLY JSON:
{{"verdicts": [{{"text": "<copied exactly from PROPOSED>", "kind": "identity|form|business"}}]}}

Judge only what is listed. Include every item exactly once. When genuinely unsure, \
answer "identity"."""

#: The AI is a real PROPOSAL layer: what it names becomes a typed candidate even
#: when no rule, spaCy or GLiNER recognised it. Fixed instructions come FIRST and
#: the page text LAST so llama.cpp reuses the shared prefix between calls.
_PROMPT_HEAD = """You audit PII detection on a document that will be sent to an outside \
service for analysis. Identity must be removed; business facts must be kept.

Find identity-bearing values that are MISSING from ALREADY DETECTED. Person names, places \
of birth, citizenship, addresses and identifiers may be in ANY language or writing system, \
with accents or an unusual spelling. Do not reject a value because it is uncommon or non-English.

A field label is evidence, never a target:
  DOB: 03/22/1985       -> the value is 03/22/1985
  Citizenship: Espana   -> the value is Espana
Return the value only, copied EXACTLY from TEXT, character for character. Never return a label \
together with its value, never a whole sentence when a short value is enough, never invented text.

Types (use exactly these names): @@TYPES@@

NEVER treat these as PII: money, wages, income, deductions, totals, percentages, tax years, \
form numbers, line numbers, generic tax vocabulary, generic company or agency names, headings, \
instructions, job titles.

Return ONLY JSON:
{{"missed": [{{"text": "<exact value>", "type": "<TYPE>"}}], \
"uncertain": [{{"text": "<exact value>", "type": "<TYPE>"}}], \
"wrong": [{{"text": "<exact entry from ALREADY DETECTED>"}}]}}
"missed" = identity values you are confident about that are not already detected. \
"uncertain" = may be identity but you are not sure. "wrong" = entries in ALREADY DETECTED that \
are labels or business facts, not identity.

Example 1:
TEXT:
Applicant: JOHN R MILLER
Born 03/22/1985   Citizenship: Espana
Place of Birth: Manila
Line 4  Total deductions .......... $18,250

ALREADY DETECTED: ["JOHN R MILLER"]

Correct output:
{{"missed": [{{"text": "03/22/1985", "type": "DOB"}}, \
{{"text": "Espana", "type": "CITIZENSHIP"}}, \
{{"text": "Manila", "type": "BIRTHPLACE"}}], \
"uncertain": [], "wrong": []}}

Note what this example does NOT do: it does not repeat "JOHN R MILLER" in "missed" (already \
detected), and it does not mention "$18,250" at all (a money amount, never identity).

Example 2:
TEXT:
Taxpayer Name: Lukasz Wisniewski
Employee ID: EMP-004417   Total wages 85,000

ALREADY DETECTED: []

Correct output:
{{"missed": [{{"text": "Lukasz Wisniewski", "type": "PERSON"}}, \
{{"text": "EMP-004417", "type": "EMPLOYEE_ID"}}], \
"uncertain": [], "wrong": []}}

Now do the same for this document.

""".replace("@@TYPES@@", ", ".join(AI_TYPES))

PROMPT = _PROMPT_HEAD + """TEXT:
{text}

ALREADY DETECTED: {found}"""


class AuditorUnavailable(RuntimeError):
    pass


@dataclass
class AuditFinding:
    text: str
    category: str
    #: The model's own few-word justification: evidence, not truth.
    reason: str = ""
    #: From the model's "uncertain" bucket: kept as a candidate, but only for review.
    uncertain: bool = False


class LlmAuditor:
    def __init__(self, model_dir: Optional[Path] = None, threads: int = 4):
        # No model ships bundled anymore (see app/llm/__init__.py) - the
        # real path comes from whichever catalog entry the user actually
        # chose and downloaded in the mandatory first-launch picker, not
        # a fixed constant.
        #
        # model_dir stays an explicit override for tests and anything
        # that wants to point at a specific directory directly, but even
        # then the FILENAME comes from the user's actual selection, not
        # the old fixed MODEL_FILE constant - a test overriding model_dir
        # must still name the file that is genuinely selected, or there
        # is nothing real to find there.
        from ..llm.selection import get_selected

        self._choice = get_selected()
        if self._choice is None:
            self.model_path: Optional[Path] = None
        else:
            directory = Path(model_dir) if model_dir is not None else self._default_dir()
            self.model_path = directory / self._choice.filename
        self.threads = threads
        self._llm = None
        self._abort_callback = None  # must stay referenced or ctypes frees it

    @staticmethod
    def _default_dir() -> Path:
        from ..llm.downloader import model_dir as llm_model_dir

        return llm_model_dir()

    def load(self) -> None:
        if self._llm is not None:
            return
        import os

        # Belt and braces: some builds read these before the Python arguments.
        os.environ.setdefault("GGML_METAL", "0")
        os.environ.setdefault("LLAMA_METAL", "0")
        try:
            from llama_cpp import Llama
        except ImportError as exc:
            raise AuditorUnavailable(f"llama-cpp-python not installed: {exc}") from exc
        if self.model_path is None:
            raise AuditorUnavailable(
                "no model selected yet - the first-launch picker has not been completed"
            )
        if not self.model_path.exists():
            raise AuditorUnavailable(
                f"model missing at {self.model_path}. Run the app to trigger the picker again."
            )
        # A downloaded file can have exactly the right size and still not
        # be a loadable model (a corrupted download passes a size check;
        # found when leaked, zero-filled test files made llama.cpp raise a
        # bare ValueError on a real CI runner). Every caller in the
        # pipeline handles AuditorUnavailable and carries on without the
        # audit pass - a ValueError would instead abort the whole analysis.
        try:
            self._llm = Llama(
                model_path=str(self.model_path),
                n_ctx=CONTEXT_TOKENS,
                n_threads=self.threads,
                # CPU only, deliberately. Offloading probes every Metal kernel on
                # machines that advertise a GPU they cannot use, which produces
                # pages of "not supported" and a very slow load for no benefit.
                n_gpu_layers=0,
                verbose=False,
            )
        except Exception as exc:  # noqa: BLE001 - any load failure means unavailable
            self._llm = None
            raise AuditorUnavailable(
                f"model could not be loaded ({self.model_path.name}): {exc}. "
                "The file may be corrupt - delete it to choose and download again."
            ) from exc
        self._install_abort_callback()

    def _install_abort_callback(self) -> None:
        """Let Skip stop a model call that is ALREADY RUNNING.

        Without this, request_stop() only took effect between calls, and one
        call can take half a minute here and minutes on a larger model - so
        the button looked broken. llama.cpp polls this callback while it works
        (reading the page AND writing the answer); returning True aborts the
        call and llama_decode returns 2, which surfaces as a RuntimeError that
        _chat() turns into AnalysisStopped. Measured with the real model:
        stops 0.01-0.03 s after the request, no measurable slowdown, and the
        next answer is byte-identical to one never interrupted.
        """
        try:
            import llama_cpp

            self._abort_callback = llama_cpp.ggml_abort_callback(lambda _data: _stop.is_set())
            llama_cpp.llama_set_abort_callback(self._llm._ctx.ctx, self._abort_callback, None)
        except Exception:  # noqa: BLE001 - degrade to "stop between calls", never fail the load
            self._abort_callback = None
            log.warning(
                "could not install the abort callback; Skip will wait for the "
                "current model call to finish",
                exc_info=True,
            )

    @property
    def configured(self) -> bool:
        """A model was chosen, its file is on disk, and the engine is
        installed - answered WITHOUT loading the model. available loads it
        (seconds, and gigabytes of RAM), which is right when about to use it
        and wrong for a status label or a default."""
        import importlib.util

        return (
            self.model_path is not None
            and self.model_path.exists()
            and importlib.util.find_spec("llama_cpp") is not None
        )

    @property
    def available(self) -> bool:
        try:
            self.load()
            return True
        except AuditorUnavailable:
            return False

    def audit(self, text: str, detected: list[str]) -> tuple[list[AuditFinding], list[str]]:
        """Returns (findings, wrongly_flagged). `findings` holds the model's confident
        AND uncertain proposals (AuditFinding.uncertain tells them apart).

        Text longer than one call can hold is split into overlapping chunks and
        EVERY chunk is sent - this used to be text[:MAX_CHARS_PER_CALL], so the tail
        of a long page was never looked at, and nothing said so.
        """
        self.load()
        findings: list[AuditFinding] = []
        wrong: list[str] = []
        for piece in split_text(text, MAX_CHARS_PER_CALL):
            prompt = PROMPT.format(text=piece, found=json.dumps(detected[:60]))
            response = _chat(
                self,
                messages=[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=MAX_OUTPUT_TOKENS,
                grammar=_grammar(AUDIT_SCHEMA),
            )
            found, objected = _parse(response["choices"][0]["message"]["content"])
            findings.extend(found)
            wrong.extend(objected)
        findings = _dedupe_findings(findings)
        findings = _drop_redundant_missed(findings, detected)
        return findings, list(dict.fromkeys(wrong))


def split_text(text: str, limit: int = 0, overlap: int = 2) -> list[str]:
    """Split text into chunks of at most `limit` characters, on LINE boundaries,
    each starting `overlap` lines before the previous one ended, so a value on a
    boundary is seen whole by at least one chunk. Every character of the input is
    in at least one chunk (a single over-long line is cut at spaces, losing nothing)."""
    limit = limit or MAX_CHARS_PER_CALL
    lines: list[str] = []
    for line in text.split("\n"):
        while len(line) > limit:
            cut_at = line.rfind(" ", 0, limit)
            cut_at = cut_at if cut_at > limit // 2 else limit
            lines.append(line[:cut_at])
            line = line[cut_at:].lstrip()
        lines.append(line)
    chunks: list[str] = []
    start = 0
    while start < len(lines):
        size, end = 0, start
        while end < len(lines) and (end == start or size + len(lines[end]) + 1 <= limit):
            size += len(lines[end]) + 1
            end += 1
        chunks.append("\n".join(lines[start:end]))
        if end >= len(lines):
            break
        start = max(end - overlap, start + 1)
    return chunks or [text]


def chunk_lines(lines: list[Line], limit: int = 0, overlap: int = 2) -> list[list[Line]]:
    """The same idea over real Line objects, so a finding in a chunk is located in
    exactly the lines that were sent."""
    limit = limit or MAX_CHARS_PER_CALL
    chunks: list[list[Line]] = []
    start = 0
    while start < len(lines):
        size, end = 0, start
        while end < len(lines) and (end == start or size + len(lines[end].text) + 1 <= limit):
            size += len(lines[end].text) + 1
            end += 1
        chunks.append(lines[start:end])
        if end >= len(lines):
            break
        start = max(end - overlap, start + 1)
    return chunks


def region_chunks(page, limit: int = 0) -> list[list[Line]]:
    """A page as LOGICAL regions (its layout blocks), packed up to the call limit;
    a block too big for one call is split by lines with overlap. Every line with
    text lands in at least one chunk."""
    limit = limit or MAX_CHARS_PER_CALL
    groups: list[list[Line]] = []
    current: list[Line] = []
    size = 0
    for block in page.blocks:
        block_lines = [line for line in block.lines if line.text.strip()]
        if not block_lines:
            continue
        block_size = sum(len(line.text) + 1 for line in block_lines)
        if block_size > limit:
            if current:
                groups.append(current)
                current, size = [], 0
            groups.extend(chunk_lines(block_lines, limit))
            continue
        if current and size + block_size > limit:
            groups.append(current)
            current, size = [], 0
        current.extend(block_lines)
        size += block_size
    if current:
        groups.append(current)
    return groups


def _dedupe_findings(findings: list[AuditFinding]) -> list[AuditFinding]:
    """Overlapping chunks report the same value twice; keep one, preferring the
    confident proposal over the uncertain one."""
    best: dict[tuple[str, str], AuditFinding] = {}
    for finding in findings:
        key = (fold(finding.text), finding.category.strip().upper())
        held = best.get(key)
        if held is None or (held.uncertain and not finding.uncertain):
            best[key] = finding
    return list(best.values())


def _parse_verdicts(raw: str) -> dict[str, str]:
    cleaned = re.sub(r"^\s*```(?:json)?|```\s*$", "", raw.strip(), flags=re.M).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    for item in data.get("verdicts", []) or []:
        if isinstance(item, dict) and item.get("text"):
            kind = str(item.get("kind", "identity")).strip().lower()
            out[" ".join(str(item["text"]).split()).lower()] = kind
    return out


# --- the type check (FLAG-ONLY) ---------------------------------------------
#
# Asks the model, one value at a time and WITHOUT telling it what the pipeline
# decided, what kind of thing the value is; if it names a different one of
# person / company / address, the detection is flagged for review. Never changes
# the type, the text or the decision - only adds a note and needs_review.
#
# Designed from measurements with the real 0.5B model (not assumed):
# - Batching many values into one answer FAILED: 30% correct, and 13 of 30
#   values got no answer at all (it stopped early, or ran on until the token cap).
# - One narrow question per value worked: 94% correct on a hard 70-value set
#   with no labels to help (people 28/30, companies 18/20, addresses 20/20),
#   and cheap (~1.2 s a value) because the instructions come first and
#   llama.cpp reuses that prefix between calls.
# - It was reliable for names, companies and places but NOT for numeric
#   identifiers (it called an SSN a phone number), so only those three families
#   are checked and numeric-looking values are skipped. A value it labels
#   anything else is never flagged.
# - Flag rate on correctly labelled values: 6% (4/70; e.g. a person named
#   "Sydney Park", a bank with a town in its name). Wrong labels caught: 97%.
TYPE_CHECK_PHASE_SECONDS = float("inf")

_FAMILY = {
    PiiType.PERSON: "person",
    PiiType.ORG_PRIVATE: "organization",
    PiiType.ADDRESS: "address",
    PiiType.STREET: "address",
    PiiType.CITY_STATE: "address",
    PiiType.PO_BOX: "address",
    PiiType.BIRTHPLACE: "address",
}
_FAMILY_NOUN = {
    "person": "a person's name",
    "organization": "a company or organization",
    "address": "an address or place",
}
TYPE_CHECK_KINDS = [
    "person", "organization", "address", "email", "phone",
    "government_id", "account", "date", "other_id", "other",
]
TYPE_CHECK_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"kind": {"type": "string", "enum": TYPE_CHECK_KINDS}},
    "required": ["kind"],
})
#: Fixed text FIRST, the value LAST: llama.cpp reuses the shared prefix between
#: calls, which is most of why this is cheap. Do not reorder.
TYPE_CHECK_HEAD = """You label one value found in a document. Say what kind of thing it is:
  "person"        - a human being's name
  "organization"  - a company, firm, lender or other organization name
  "address"       - a street address, PO box, city, state, zip code or place
  "email"         - an email address
  "phone"         - a phone or fax number
  "government_id" - a social security, tax, license or passport number
  "account"       - a bank account, routing or card number
  "date"          - a date
  "other_id"      - an employee, policy, member, medical or case number
  "other"         - anything else

Return ONLY JSON: {"kind": "<kind>"}

"""


def _checkable_family(candidate: Candidate) -> Optional[str]:
    family = _FAMILY.get(candidate.pii_type)
    if family is None or candidate.source is Source.MANUAL:
        return None
    text = candidate.normalized
    letters = sum(ch.isalpha() for ch in text)
    digits = sum(ch.isdigit() for ch in text)
    # Numeric-looking values are exactly where this model is unreliable.
    if letters < 2 or digits > letters or len(text) > 120:
        return None
    return family


def _parse_kind(raw: str) -> str:
    match = re.search(r'"kind"\s*:\s*"([a-z_]+)"', raw)
    return match.group(1) if match else ""


def check_types(candidates: list[Candidate], auditor: Optional[LlmAuditor] = None) -> list[str]:
    """Flag detections whose type the model disagrees with. Returns warnings.

    Each distinct (value, type) is asked once however often it repeats, and every
    occurrence is flagged together.
    """
    warnings: list[str] = []
    groups: dict[tuple[str, str], list[Candidate]] = {}
    for candidate in candidates:
        family = _checkable_family(candidate)
        if family:
            groups.setdefault((candidate.normalized.lower(), family), []).append(candidate)
    if not groups:
        return warnings

    auditor = auditor or _auditor()
    try:
        auditor.load()
    except AuditorUnavailable as exc:
        warnings.append(f"AI type check disabled: {exc}")
        return warnings

    gate = _PhaseGate("AI type check", TYPE_CHECK_PHASE_SECONDS, len(groups), warnings, unit="value")
    grammar = _grammar(TYPE_CHECK_SCHEMA)
    flagged = failures = 0
    for (_value, family), members in groups.items():
        if not gate.allow():
            break
        first = members[0]
        # The value as written (punctuation kept - "Inc." not "Inc"), exactly as
        # it was when the accuracy was measured; only the grouping uses the
        # normalised form.
        value_text = " ".join(first.text.split())
        line = " ".join(first.line.text.split())[:200]
        try:
            response = _chat(
                auditor,
                messages=[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": TYPE_CHECK_HEAD + f"LINE: {line}\nVALUE: {value_text}"},
                ],
                temperature=0.0,
                max_tokens=24,
                grammar=grammar,
            )
            kind = _parse_kind(response["choices"][0]["message"]["content"])
        except AnalysisStopped:
            gate.aborted()  # flags already raised are kept
            break
        except Exception as exc:  # noqa: BLE001 - advisory only
            failures += 1
            log.warning("type check failed: %s", type(exc).__name__)
            continue
        if kind in _FAMILY_NOUN and kind != family:
            note = (
                f"the AI thinks this may be {_FAMILY_NOUN[kind]}, not {_FAMILY_NOUN[family]} - "
                "check the type before relying on the replacement"
            )
            for member in members:
                member.needs_review = True
                if note not in member.review_reason:
                    member.review_reason = f"{member.review_reason}; {note}" if member.review_reason else note
            flagged += len(members)
    gate.finish()
    if failures:
        warnings.append(f"AI type check could not read the model's answer for {failures} value(s); they were not checked.")
    if flagged:
        warnings.append(f"AI type check flagged {flagged} detection(s) whose type may be wrong - see the review list.")
    return warnings


def _drop_redundant_missed(
    missed: list[AuditFinding], detected: list[str]
) -> list[AuditFinding]:
    """Filter out any "missed" finding that duplicates something already in
    the "detected" list the model was told about, enforced in code rather
    than left to the model following its own prompt instructions.

    Real, reported CI failure - the first time this auditor's own test ran
    against the real model, rather than skipping because the weights were
    never fetched in CI before: given `detected=["MARIA T GONZALEZ-REYES"]`,
    the model's own "missed" list came back containing exactly that same
    name, despite the prompt explicitly saying "missed" must be things
    that are NOT already detected. Grammar-constrained decoding (see
    AUDIT_SCHEMA / _grammar above) enforces that the response is
    syntactically valid JSON in the right shape - it says nothing about
    whether the model reasoned correctly about WHICH values belong in
    which array, which is exactly what failed here. A "missed" finding
    that only repeats an already-detected value adds no information
    either way - true or not, the value is already marked for redaction -
    so this can be filtered with certainty, without knowing why the model
    produced it.
    """
    if not missed or not detected:
        return missed
    known = {" ".join(d.split()).strip(" .,;:").lower() for d in detected}
    kept = []
    for finding in missed:
        normalized = " ".join(finding.text.split()).strip(" .,;:").lower()
        if not normalized:
            continue
        if any(normalized == k or normalized in k or k in normalized for k in known):
            continue
        kept.append(finding)
    return kept


def _salvage(text: str) -> dict:
    """Recover the COMPLETE items from JSON that was cut off at the output cap.

    Discarding the whole response because the last item was unfinished turned a
    useful answer into "the model found nothing" - silently."""
    data: dict = {"missed": [], "uncertain": [], "wrong": []}
    for key in data:
        section = re.search(rf'"{key}"\s*:\s*\[(.*?)(?:\]\s*[,}}]|$)', text, re.S)
        if not section:
            continue
        for item in re.finditer(r'\{[^{}]*\}', section.group(1)):
            try:
                data[key].append(json.loads(item.group()))
            except json.JSONDecodeError:
                continue
    return data if any(data.values()) else {}


def _parse(raw: str) -> tuple[list[AuditFinding], list[str]]:
    cleaned = re.sub(r"^\s*```(?:json)?|```\s*$", "", raw.strip(), flags=re.M).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        return [], []
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        data = _salvage(cleaned)
        if not data:
            log.debug("auditor returned unparseable JSON")
            return [], []

    findings: list[AuditFinding] = []
    for key, uncertain in (("missed", False), ("uncertain", True)):
        for item in data.get(key, []) or []:
            if isinstance(item, dict) and item.get("text"):
                findings.append(AuditFinding(
                    str(item["text"]).strip(),
                    str(item.get("type", "")).strip(),
                    str(item.get("reason", "")).strip()[:160],
                    uncertain,
                ))
    wrong = []
    for item in data.get("wrong", []) or []:
        value = item.get("text") if isinstance(item, dict) else item
        if value:
            wrong.append(str(value).strip())
    return findings, wrong


def _adjudication_batches(page, proposed: list[Candidate]) -> list[tuple[list[str], str]]:
    """Batches of at most 25 values, each with the lines that HOLD them (plus a
    neighbouring line either side) as context.

    This used to be the first MAX_CHARS_PER_CALL characters of the PAGE for every
    batch, so a value further down a long page was judged without ever seeing the
    text it sits in - and the model was asked to rule on it anyway."""
    lines = page.lines
    index = {line.key(): i for i, line in enumerate(lines)}
    hosts: dict[str, set[int]] = {}
    for candidate in proposed:
        value = candidate.normalized
        if value:
            hosts.setdefault(value, set()).add(index.get(candidate.line.key(), 0))

    def with_neighbours(indexes: set[int]) -> list[int]:
        keep: set[int] = set()
        for i in indexes:
            keep.update({max(i - 1, 0), i, min(i + 1, len(lines) - 1)})
        return sorted(keep)

    batches: list[tuple[list[str], set[int]]] = []
    values: list[str] = []
    held: set[int] = set()
    for value in sorted(hosts):
        trial = held | hosts[value]
        size = sum(len(lines[i].text) + 1 for i in with_neighbours(trial))
        if values and (len(values) >= 25 or size > MAX_CHARS_PER_CALL):
            batches.append((values, held))
            values, trial = [], set(hosts[value])
        values.append(value)
        held = trial
    if values:
        batches.append((values, held))

    out: list[tuple[list[str], str]] = []
    for vals, idx in batches:
        context = "\n".join(lines[i].text for i in with_neighbours(idx))
        if len(context) > MAX_CHARS_PER_CALL:  # the values' own lines alone are too long
            context = "\n".join(lines[i].text for i in sorted(idx))
        out.append((vals, context))
    return out


def adjudicate_document(
    doc: Document, candidates: list[Candidate], auditor: Optional[LlmAuditor] = None
) -> tuple[set[str], list[str]]:
    """Review every proposed redaction. Returns (rejected_values, warnings).

    This runs over the whole page, not only pages the earlier layers doubted.
    Those layers are tuned for recall, so the plan reaching this point contains
    the form's own labels and headings and the occasional business figure. The
    model is asked a narrow, checkable question about each item - identity, form
    text, or business fact - and only "form" and "business" are dropped.

    Uncertainty resolves to identity: an item wrongly kept costs a pseudonymised
    word, an item wrongly dropped leaks a client.
    """
    auditor = auditor or _auditor()
    warnings: list[str] = []
    try:
        auditor.load()
    except AuditorUnavailable as exc:
        warnings.append(f"LLM adjudication disabled: {exc}")
        return set(), warnings

    by_page: dict[int, list[Candidate]] = {}
    for candidate in candidates:
        by_page.setdefault(candidate.page_no, []).append(candidate)

    rejected: set[str] = set()
    gate = _PhaseGate(
        "AI check", ADJUDICATE_PHASE_SECONDS, sum(1 for p in doc.pages if by_page.get(p.number)), warnings
    )
    for page in doc.pages:
        proposed = by_page.get(page.number, [])
        if not proposed:
            continue
        batches = _adjudication_batches(page, proposed)
        if not batches:
            continue
        if not gate.allow():
            break

        for chunk, context in batches:
            if gate.expired():
                break
            prompt = ADJUDICATE_PROMPT.format(text=context, proposed=json.dumps(chunk))
            try:
                response = _chat(
                    auditor,
                    messages=[
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.0,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    grammar=_grammar(ADJUDICATE_SCHEMA),
                )
                verdicts = _parse_verdicts(response["choices"][0]["message"]["content"])
            except AnalysisStopped:
                gate.aborted()  # rejections already decided are kept
                break
            except Exception as exc:  # noqa: BLE001 - advisory only
                log.warning("adjudication failed on page %s: %s", page.number + 1, type(exc).__name__)
                warnings.append(f"LLM adjudication failed on page {page.number + 1}")
                continue

            for value in chunk:
                kind = verdicts.get(value.lower())
                if kind in ("form", "business"):
                    rejected.add(value.lower())

    gate.finish()
    if rejected:
        warnings.append(
            f"review pass dropped {len(rejected)} proposed value(s) as form text or "
            "business facts rather than identity"
        )
    return rejected, warnings


def is_acceptable_finding(text: str, label_words: set[str]) -> tuple[bool, str]:
    """Would acting on this finding damage the document?

    The model is advisory and noisy. Rather than trusting a prompt to keep it
    away from labels and figures, its output is filtered against what the
    deterministic layers already know: the label regions found on the page, the
    form vocabulary, and the currency patterns. A model cannot talk the system
    into redacting a caption or a number.
    """
    from ..pseudonymization.generator import INLINE_LABELS
    from .deterministic import MONEY_RE, PERCENT_RE
    from .heuristics import FORM_VOCABULARY

    stripped = text.strip().strip(".,;:")
    if len(stripped) < 2:
        return False, "too short to locate reliably"
    if MONEY_RE.search(stripped) or PERCENT_RE.search(stripped):
        return False, "contains a figure"
    if is_protected_figure_text(stripped) is not None:
        return False, "a business figure or form identifier"
    if re.fullmatch(r"[\d\s.,%$()-]+", stripped):
        # A long digit run is an identifier - an SSN, an account, a policy. A
        # short one is a line number, a quantity or a year.
        digits = sum(1 for ch in stripped if ch.isdigit())
        if digits < 7 or "," in stripped or "$" in stripped:
            return False, "a number, not an identifier"
        return True, ""

    tokens = [tok.strip(".,;:()").lower() for tok in stripped.split() if tok.strip(".,;:()")]
    if not tokens:
        return False, "no usable text"
    if stripped.lower() in label_words:
        return False, "matches a field label on this page"
    # Any form word disqualifies it - a name does not contain "wages" or
    # "schedule"; a mis-scoped span does. Company suffixes are the exception:
    # they sit in that vocabulary to keep the person heuristics honest, but a
    # client's business IS identity here.
    from ..pseudonymization.generator import COMPANY_WORDS

    disqualifying = FORM_VOCABULARY - COMPANY_WORDS
    if any(tok in disqualifying for tok in tokens):
        return False, "contains the document's own wording"
    if all(tok in INLINE_LABELS for tok in tokens):
        return False, "field label wording only"
    if stripped.endswith(":"):
        return False, "looks like a field label"
    return True, ""


def map_category(category: str) -> PiiType:
    lowered = category.lower()
    for key, pii_type in CATEGORY_MAP.items():
        if key in lowered:
            return pii_type
    return PiiType.UNCLASSIFIED_GROUP_VALUE


@lru_cache(maxsize=1)
def _auditor() -> LlmAuditor:
    return LlmAuditor()


# --- keeping the model phase bounded, visible and stoppable ------------------
#
# Reported by a user: analysis "stuck for more than an hour" on the smallest
# model. Measured with the real 0.5B model: one model call costs roughly 10 s
# (mostly reading the page, not writing the answer) on a single slow core, and
# the audit plus the adjudication call it about once per page each - so model
# time grows in step with page count and nothing bounded it, reported on it, or
# let the user stop it. The model is ADVISORY (the other layers already covered
# the document), so cutting it short loses a second opinion, never coverage.

#: Wall-clock seconds each model phase may run, per document. NO default cap:
#: the owner wants the model to review every page, and a fixed cap silently
#: turns "every page" into "the first few" on a long document. The defences
#: against an apparent hang are visibility (page n of N, with an estimate),
#: the Skip button, and the persistent on/off switch - not a hidden cut-off.
#: DOCANON_LLM_BUDGET_SECONDS sets a cap anyway (0 = none). A cap cannot
#: interrupt a call already in flight; that finishes first.
AUDIT_PHASE_SECONDS = float("inf")
ADJUDICATE_PHASE_SECONDS = float("inf")

_stop = threading.Event()
_progress_listener = None


def request_stop() -> None:
    """Ask the running analysis to skip the rest of the model phase."""
    _stop.set()


def reset_stop() -> None:
    _stop.clear()


def stop_requested() -> bool:
    return _stop.is_set()


def set_progress_listener(listener) -> None:
    """listener(text) is called from the analysis thread; pass None to clear."""
    global _progress_listener
    _progress_listener = listener


def _report(text: str) -> None:
    listener = _progress_listener
    if listener is None:
        return
    try:
        listener(text)
    except Exception:  # noqa: BLE001 - progress display must never break analysis
        pass


def _phase_seconds(default: float) -> float:
    """DOCANON_LLM_BUDGET_SECONDS overrides both phases; 0 means no limit."""
    raw = os.environ.get("DOCANON_LLM_BUDGET_SECONDS", "").strip()
    if raw:
        try:
            value = float(raw)
            return float("inf") if value <= 0 else value
        except ValueError:
            pass
    return default


class AnalysisStopped(Exception):
    """The user pressed Skip while a model call was running and it was aborted."""


def _chat(auditor, **kwargs):
    """Every model call goes through here. An abort caused by Skip becomes
    AnalysisStopped; any OTHER failure is left alone (a real error must not be
    mistaken for the user's request)."""
    try:
        return auditor._llm.create_chat_completion(**kwargs)
    except RuntimeError as exc:
        if not _stop.is_set():
            raise
        try:
            # The aborted call may have left the model mid-evaluation; start the
            # next one from a clean slate rather than trust a half-built state.
            auditor._llm.reset()
        except Exception:  # noqa: BLE001
            pass
        raise AnalysisStopped() from exc


def _wait_text(seconds: float) -> str:
    if seconds < 60:
        return "under a minute left"
    return f"about {round(seconds / 60)} min left"


class _PhaseGate:
    """Decides, before each model call, whether the phase may continue."""

    def __init__(
        self, label: str, default_seconds: float, total: int, warnings: list[str], unit: str = "page"
    ):
        self.label = label
        self.unit = unit
        self.total = total
        self.warnings = warnings
        self.limit = _phase_seconds(default_seconds)
        self.started = time.monotonic()
        self.deadline = self.started + self.limit
        self.done = 0
        self.cut = False
        self.reason = ""
        self._warned = False

    def expired(self) -> bool:
        if _stop.is_set():
            self.reason = "was skipped at your request"
        elif time.monotonic() >= self.deadline:
            self.reason = f"stopped after {self.limit:.0f} seconds"
        else:
            return False
        self.cut = True
        return True

    def allow(self) -> bool:
        """Call once per page. False means stop the phase."""
        if self.expired():
            return False
        self.done += 1
        text = f"{self.label}: {self.unit} {self.done} of {self.total}"
        remaining = self.total - self.done
        if self.done >= 3 and remaining > 0:
            # Average of the pages already finished, applied to those left
            # (including the one starting now).
            per_page = (time.monotonic() - self.started) / (self.done - 1)
            text += f" - {_wait_text(per_page * (remaining + 1))}"
        _report(text)
        return True

    def aborted(self) -> None:
        """A call was cut off mid-way. It did not complete, so it is not counted;
        everything the phase had already finished is kept."""
        self.done = max(self.done - 1, 0)
        self.reason = "was skipped at your request"
        self.cut = True

    def finish(self) -> None:
        if self.cut and not self._warned:
            self._warned = True
            self.warnings.append(
                f"{self.label} {self.reason}. The model reviewed {self.done} of "
                f"{self.total} {self.unit}(s); the other detection layers covered the whole document."
            )


def llm_env_override() -> Optional[bool]:
    """DOCANON_LLM when it holds a recognised value (True/False), else None."""
    import os

    setting = os.environ.get("DOCANON_LLM", "").strip().lower()
    if setting in ("0", "false", "no", "off"):
        return False
    if setting in ("1", "true", "yes", "on"):
        return True
    return None


def llm_audit_enabled() -> bool:
    """Should the AI review run? Precedence: the DOCANON_LLM environment
    variable, then the user's persistent switch in the window (settings.json,
    key "ai_review"), then "on iff a downloaded model is ready".

    The switch can only turn the review OFF or leave it to the default: ON
    means "use it when a model is ready", never "pretend there is one".

    History: this used to default OFF everywhere, because the model proposed
    labels, headings and figures as often as real PII. The owner reversed that:
    the first-launch picker makes a model mandatory, its findings are redacted
    by default AND flagged for review, and the model now reviews every page.
    That is only acceptable because of the filters every finding still passes -
    is_acceptable_finding (figures, labels, form wording), confirm_with_detectors
    (only a value the rules/spaCy/GLiNER can TYPE is applied), the financial
    guard, and the adjudication pass. None of those were loosened.

    DOCANON_LLM=0/false/no/off forces it off (the CI end-to-end step relies on
    that); 1/true/yes/on forces it on (it then degrades with a warning if no
    model can load).
    """
    override = llm_env_override()
    if override is not None:
        return override
    try:
        from .. import settings

        if settings.get("ai_review") is False:
            return False
        return LlmAuditor().configured
    except Exception:  # noqa: BLE001 - a default must never break analysis
        return False


def _locate(needle: str, lines: list[Line]) -> list[tuple[Line, int, int]]:
    """Find the model's text in the real document, ignoring case, accents, hyphens
    and apostrophes (Unicode-aware), and return offsets into the ORIGINAL line.
    Not found means discarded: the model never supplies geometry."""
    needle = needle.strip().strip(".,;:")
    if not needle:
        return []
    return [(line, start, end) for line in lines for start, end in find_occurrences(line.text, needle)]


def resolve_ai_type(category: str) -> PiiType:
    """The model's type name -> our PiiType, exact first, then the legacy synonyms."""
    cleaned = category.strip().upper().replace(" ", "_")
    try:
        return PiiType(cleaned)
    except ValueError:
        return map_category(category)


def confirm_with_detectors(doc, findings: list, line_of) -> list:
    """Let the deterministic detectors STRENGTHEN or CORRECT an AI proposal.

    This used to be a gate: only a span the rules, spaCy or GLiNER could
    independently type was accepted, and anything else became
    UNCLASSIFIED_GROUP_VALUE and waited unapplied. That made the model useless for
    exactly what it is for - citizenship, a birthplace, an unfamiliar name, an
    identifier in an odd format - because those are the values the other detectors
    do not recognise. Now:

      * a type the deterministic detectors agree on raises confidence;
      * a deterministic identifier type OVERRIDES a conflicting AI type (strong
        evidence outranks a small model's label) and the disagreement is flagged;
      * otherwise the AI's own valid taxonomy type stands, as a PROPOSAL: flagged
        for review, and still subject to every protected region and the unified gate.
    """
    from .deterministic import detect_deterministic, load_rules
    from .heuristics import looks_like_a_lone_surname, looks_like_person
    from .provider_shim import document_from_pages

    ruleset = load_rules()
    confirmed = []
    for candidate in findings:
        text = candidate.text.strip()
        if not text:
            continue

        detector_type = None
        scratch = document_from_pages([text])
        if scratch is not None:
            exact = [h for h in detect_deterministic(scratch, ruleset)
                     if fold(h.normalized) == fold(text)]
            if exact:
                detector_type = max(exact, key=lambda h: h.confidence).pii_type

        ai_type = candidate.pii_type if candidate.pii_type is not PiiType.UNCLASSIFIED_GROUP_VALUE else None
        shaped_like_a_name = looks_like_person(text)[0] or looks_like_a_lone_surname(text)

        if detector_type is not None:
            if ai_type is not None and ai_type is not detector_type:
                candidate.needs_review = True
                candidate.review_reason = (
                    f"the AI said {ai_type.value}, the rules say {detector_type.value}; "
                    "the rules' type was used"
                )
            candidate.pii_type = detector_type
            candidate.confidence = max(candidate.confidence, 0.72)
            candidate.evidence.append(Evidence(Source.AUDIT, "deterministic detectors confirm the type", 0.5))
        elif ai_type is not None:
            candidate.evidence.append(Evidence(Source.AUDIT, "type from the AI alone - no detector disagreed", 0.0))
        elif shaped_like_a_name:
            candidate.pii_type = PiiType.PERSON
            candidate.confidence = max(candidate.confidence, 0.6)
            candidate.evidence.append(Evidence(Source.AUDIT, "name-shaped value", 0.5))
        else:
            candidate.pii_type = PiiType.UNCLASSIFIED_GROUP_VALUE
            candidate.needs_review = True
            candidate.review_reason = "the review model flagged this, but nothing could say what it is"
        confirmed.append(candidate)
    return confirmed


def audit_document(
    doc: Document,
    candidates: list[Candidate],
    auditor: Optional[LlmAuditor] = None,
    labels: Optional[list] = None,
) -> tuple[list[Candidate], list[str], list[str]]:
    """Returns (additional_candidates, objected_values, warnings).

    Every page is covered, in logical regions that overlap at their edges; each
    finding is located in the REAL text of the region it came from, so the model
    never controls geometry. A finding becomes a typed candidate whether or not
    any other detector recognised it; protected regions and the unified gate (run
    by the caller) are what keep it from damaging the document.
    """
    auditor = auditor or _auditor()
    warnings: list[str] = []
    try:
        auditor.load()
    except AuditorUnavailable as exc:
        warnings.append(f"LLM auditor disabled: {exc}")
        return [], [], warnings

    by_page: dict[int, list[Candidate]] = {}
    for candidate in candidates:
        by_page.setdefault(candidate.page_no, []).append(candidate)

    label_words: dict[int, set[str]] = {}
    for label in labels or []:
        label_words.setdefault(label.page_no, set()).add(label.text.strip().lower())

    additions: list[Candidate] = []
    objected: list[str] = []
    rejected = 0

    pages_to_audit = {p.number for p in doc.pages if any(l.text.strip() for l in p.lines)}
    gate = _PhaseGate("AI review", AUDIT_PHASE_SECONDS, len(pages_to_audit), warnings)
    for page in doc.pages:
        if page.number not in pages_to_audit:
            continue
        if not gate.allow():
            break
        page_candidates = by_page.get(page.number, [])
        existing = [(c.line.key(), c.start, c.end) for c in page_candidates]
        page_labels = label_words.get(page.number, set())
        stopped = False
        for lines in region_chunks(page):
            if gate.expired():
                stopped = True
                break
            keys = {line.key() for line in lines}
            detected = sorted({c.normalized for c in page_candidates if c.line.key() in keys})
            try:
                found, wrong = auditor.audit("\n".join(line.text for line in lines), detected)
            except AnalysisStopped:
                gate.aborted()  # findings from the regions already reviewed are kept
                stopped = True
                break
            except Exception as exc:  # noqa: BLE001 - the audit is advisory, never fatal
                log.warning("auditor failed on page %s: %s", page.number + 1, type(exc).__name__)
                warnings.append(f"LLM auditor failed on page {page.number + 1}")
                continue

            objected.extend(wrong)
            for finding in found:
                acceptable, why = is_acceptable_finding(finding.text, page_labels)
                if not acceptable:
                    rejected += 1
                    log.debug("rejected model finding (%s)", why)
                    continue
                hits = [
                    (line, start, end)
                    for line, start, end in _locate(finding.text, lines)
                    if not any(key == line.key() and start < e and s < end for key, s, e in existing)
                ]
                ai_type = resolve_ai_type(finding.category)
                if ai_type is not PiiType.UNCLASSIFIED_GROUP_VALUE:
                    for line, start, end in _locate(finding.text, lines):
                        for c in page_candidates:
                            if (c.pii_type is PiiType.UNCLASSIFIED_GROUP_VALUE and c.line.key() == line.key()
                                    and c.start == start and c.end == end):
                                # Something already found this value but could not say
                                # WHAT it is; the model can. Type it in place instead of
                                # skipping it for overlapping.
                                c.pii_type = ai_type
                                c.confidence = max(c.confidence, 0.62)
                                c.needs_review = True
                                c.review_reason = "typed by the AI review; check the type"
                                c.adjudication = ""
                                c.evidence.append(Evidence(
                                    Source.AUDIT, f"AI: {ai_type.value} - {finding.reason or 'typed an unlabelled value'}", 0.62))
                            elif (c.line.key() == line.key() and c.start == start and c.end == end
                                    and c.pii_type is not ai_type and c.source is not Source.MANUAL):
                                # Same span, two different readings. Neither is silently
                                # overridden - a model's label does not outrank a detector, and
                                # a detector's does not outrank a second opinion - but the
                                # disagreement is put in front of the reviewer.
                                note = (f"the AI reads this as {ai_type.value}; it was detected as "
                                        f"{c.pii_type.value}")
                                c.needs_review = True
                                if note not in c.review_reason:
                                    c.review_reason = f"{c.review_reason}; {note}" if c.review_reason else note
                                c.evidence.append(Evidence(Source.AUDIT, f"AI disagrees on type: {ai_type.value}", 0.0))
                for line, start, end in hits:
                    rect = line.rect_for(start, end)
                    if rect is None:
                        continue
                    weight = 0.4 if finding.uncertain else 0.62
                    why_text = finding.reason or "identity-bearing value"
                    reason = (
                        "suggested by the AI review as uncertain - nothing else detected it"
                        if finding.uncertain
                        else "suggested by the AI review - nothing else detected it. "
                        "It is redacted unless you press Keep"
                    )
                    if len(hits) > 1:
                        reason += f" (the same text occurs {len(hits)} times in this region)"
                    additions.append(Candidate(
                        pii_type=ai_type,
                        text=line.text[start:end],
                        page_no=page.number,
                        rect=rect,
                        line=line,
                        start=start,
                        end=end,
                        confidence=weight,
                        source=Source.AUDIT,
                        evidence=[Evidence(Source.AUDIT, f"AI: {ai_type.value} - {why_text}", weight)],
                        needs_review=True,
                        review_reason=reason,
                    ))
                    existing.append((line.key(), start, end))
        if stopped:
            break

    gate.finish()
    if additions:
        additions = confirm_with_detectors(doc, additions, None)
    if rejected:
        warnings.append(
            f"the review model proposed {rejected} item(s) that were labels, "
            "figures or form wording; those were discarded"
        )
    return additions, list(dict.fromkeys(objected)), warnings
