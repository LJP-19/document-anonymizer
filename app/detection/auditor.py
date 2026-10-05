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
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from ..document.model import Document, Line
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
AUDIT_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "missed": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "type": {"type": "string"},
                },
                "required": ["text", "type"],
            },
        },
        "wrong": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    "required": ["missed", "wrong"],
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


@lru_cache(maxsize=2)
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

#: Real, reported CI failure, twice in a row with the real model: given
#: a name already marked "detected", the model's "missed" list never
#: contained the date of birth or citizenship in the test text at all -
#: not found, not even attempted, in either real run. The duplicate-echo
#: version of this bug (the model literally repeating the detected name)
#: is fixed separately in _drop_redundant_missed(); this is a different
#: question - whether the model finds NEW things at all.
#:
#: Added a concrete worked example below, rather than instructions alone.
#: This is an informed attempt, not a verified fix - checked real,
#: documented evidence first (constrained decoding guarantees valid JSON
#: SHAPE, never correct content, and can push a small model toward
#: "non-canonical token paths it rarely saw in training" under grammar
#: constraints specifically), and few-shot examples are a standard,
#: low-risk technique for improving small-model structured-output
#: reliability without touching the schema/grammar that provides the
#: JSON-validity guarantee. Whether this actually helps can only be
#: confirmed by the next real CI run of
#: test_llm_auditor_finds_categories_no_rule_covers - if it still fails
#: on the same assertion with the example present, the honest conclusion
#: becomes a genuine 1.5B-scale capability limit for this task, not a
#: prompting problem, and the next step is a harder look at whether
#: grammar constraints themselves are suppressing content (worth testing
#: prompt-only, no grammar, as an isolated comparison) or at the model
#: size itself.
PROMPT = """You audit PII detection on a document that will be sent to an outside \
service for analysis. Identity must be removed; business facts must be kept.

Example:
TEXT:
Applicant: JOHN R MILLER
Born 03/22/1985   Citizenship: Canada
Line 4  Total deductions .......... $18,250

ALREADY DETECTED: ["JOHN R MILLER"]

Correct output:
{{"missed": [{{"text": "03/22/1985", "type": "dob"}}, {{"text": "Canada", "type": "citizenship"}}], \
"wrong": []}}

Note what this example does NOT do: it does not repeat "JOHN R MILLER" in "missed" (already \
detected), and it does not mention "$18,250" at all (a money amount, never identity).

Now do the same for this document.

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
            grammar=_grammar(AUDIT_SCHEMA),
        )
        raw = response["choices"][0]["message"]["content"]
        missed, wrong = _parse(raw)
        missed = _drop_redundant_missed(missed, detected)
        return missed, wrong


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
    for page in doc.pages:
        proposed = by_page.get(page.number, [])
        if not proposed:
            continue
        values = sorted({c.normalized for c in proposed if c.normalized})
        if not values:
            continue
        text = "\n".join(line.text for line in page.lines)

        for chunk_start in range(0, len(values), 25):
            chunk = values[chunk_start : chunk_start + 25]
            prompt = ADJUDICATE_PROMPT.format(
                text=text[:MAX_CHARS_PER_CALL], proposed=json.dumps(chunk)
            )
            try:
                response = auditor._llm.create_chat_completion(
                    messages=[
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.0,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    grammar=_grammar(ADJUDICATE_SCHEMA),
                )
                verdicts = _parse_verdicts(response["choices"][0]["message"]["content"])
            except Exception as exc:  # noqa: BLE001 - advisory only
                log.warning("adjudication failed on page %s: %s", page.number + 1, type(exc).__name__)
                warnings.append(f"LLM adjudication failed on page {page.number + 1}")
                continue

            for value in chunk:
                kind = verdicts.get(value.lower())
                if kind in ("form", "business"):
                    rejected.add(value.lower())

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


def llm_audit_enabled() -> bool:
    """Should the audit pass run? DOCANON_LLM overrides; otherwise it runs
    exactly when a downloaded model is ready.

    History: this used to default OFF everywhere, because the model proposed
    labels, headings and figures as often as real PII. The owner reversed
    that: the first-launch picker makes a model mandatory, and its findings
    are redacted by default AND flagged for review. That is only acceptable
    because of the filters every finding still passes - is_acceptable_finding
    (figures, labels, form wording), confirm_with_detectors (only a value the
    rules/spaCy/GLiNER can TYPE is applied), the financial guard, and the
    adjudication pass. None of those were loosened.

    0/false/no/off forces it off (the CI end-to-end step relies on that);
    1/true/yes/on forces it on (it then degrades with a warning if no model
    can load). Anything else, including unset, means "on iff a model is ready".
    """
    import os

    setting = os.environ.get("DOCANON_LLM", "").strip().lower()
    if setting in ("0", "false", "no", "off"):
        return False
    if setting in ("1", "true", "yes", "on"):
        return True
    try:
        return LlmAuditor().configured
    except Exception:  # noqa: BLE001 - a default must never break analysis
        return False


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


def confirm_with_detectors(doc, findings: list, line_of) -> list:
    """Re-run the deterministic detectors over each proposed span.

    The model is good at noticing that something was skipped and poor at saying
    what it is. So its proposals are handed back to the rules, spaCy and GLiNER,
    and only a span one of them can TYPE is accepted. A span nothing can type is
    reported, never replaced - which is what stopped corrupt text reaching real
    documents.
    """
    from .deterministic import detect_deterministic, load_rules
    from .heuristics import looks_like_a_lone_surname, looks_like_person
    from .provider_shim import document_from_pages
    from .types import PiiType

    ruleset = load_rules()
    confirmed = []
    for candidate in findings:
        text = candidate.text.strip()
        if not text:
            continue

        typed = None
        scratch = document_from_pages([text])
        if scratch is not None:
            hits = detect_deterministic(scratch, ruleset)
            exact = [h for h in hits if h.normalized.lower() == text.lower()]
            if exact:
                typed = max(exact, key=lambda h: h.confidence).pii_type

        if typed is None and (looks_like_person(text)[0] or looks_like_a_lone_surname(text)):
            typed = PiiType.PERSON

        if typed is None:
            candidate.pii_type = PiiType.UNCLASSIFIED_GROUP_VALUE
            candidate.needs_review = True
            candidate.review_reason = (
                "the review model flagged this, but nothing could say what it is"
            )
        else:
            candidate.pii_type = typed
            candidate.confidence = max(candidate.confidence, 0.7)
        confirmed.append(candidate)
    return confirmed


def audit_document(
    doc: Document,
    candidates: list[Candidate],
    auditor: Optional[LlmAuditor] = None,
    labels: Optional[list] = None,
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

    # What the deterministic layers already identified as labels on each page.
    label_words: dict[int, set[str]] = {}
    for label in labels or []:
        label_words.setdefault(label.page_no, set()).add(label.text.strip().lower())

    additions: list[Candidate] = []
    wrong_total: list[str] = []
    rejected = 0

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

        page_labels = label_words.get(page.number, set())
        for finding in missed:
            acceptable, why = is_acceptable_finding(finding.text, page_labels)
            if not acceptable:
                rejected += 1
                log.debug("rejected model finding (%s)", why)
                continue
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
                        review_reason=(
                            "suggested by the review model \u2014 nothing else detected it. "
                            "It is redacted unless you press Keep"
                        ),
                    )
                )
                existing.append((line.key(), start, end))

    if additions:
        additions = confirm_with_detectors(doc, additions, None)

    if rejected:
        warnings.append(
            f"the review model proposed {rejected} item(s) that were labels, "
            "figures or form wording; those were discarded"
        )
    return additions, wrong_total, warnings
