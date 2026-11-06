# Root-cause report: how an uncertain detection could reach the PDF (0.41.0)

Written BEFORE the 0.42.0 changes, from the code as it stood at 0.41.0
(commit 59b1bd1). Every finding names the file and function and explains the
route through which an unconfirmed candidate could be redacted.

## The pipeline as it actually runs

`AnonymizationSession.analyse()` -> `engine.analyse()` (extraction, labels,
regex, spaCy/GLiNER/Presidio, heuristics, groups, protection index, resolution,
subjects, propagation, AI audit when enabled, widening, final gate,
`hold_unconfirmed_proposals`) -> `DecisionManager.register()` ->
(GUI / batch / CLI decisions) -> `session.plan()` -> `transform.plan.build_plan()`
-> `export.redactor.apply_plan()` -> `verification.verifier.verify()`.

There was exactly ONE place that decided whether a candidate is transformed:
`build_plan` reads `decisions.state(candidate)` and transforms everything in
ACCEPTED / EDITED / MANUALLY_ADDED. So the whole question is: who can put a
candidate into one of those states, and with what evidence?

## Root causes

### RC1 - Fail-OPEN default in `DecisionManager.register(fail_safe=True)`
`app/decisions/manager.py`. Every new candidate became ACCEPTED unless it fell
into one of four exceptions (ambiguous, ORG_PRIVATE, UNCLASSIFIED, adjudication
UNRESOLVED). Everything else was applied without a person - including:
* candidates with `needs_review=True` (a failed SSN checksum under a label, an AI
  objection to a strong candidate, a soft-protection conflict, a bare surname of a
  known person, a date beside an identity block, a short propagated name);
* adjudication PROBABLE candidates;
* regex matches with no label (ZIP-shaped five-digit numbers, phones, dates).
The docstring states the intent explicitly ("redaction is the default for
everything found, including low-confidence and flagged items"). That is the
opposite of the owner's rule.

### RC2 - ACCEPTED did not say WHO accepted it
The same `DecisionState.ACCEPTED` meant "the user pressed Redact" and "nothing
objected at registration". There was no provenance, so no later stage could tell
an automatic approval from a human one, and nothing could re-check an automatic
approval.

### RC3 - The transformation boundary trusted the state alone
`app/transform/plan.py::build_plan` never re-evaluated a candidate's evidence. Any
code path that set ACCEPTED (correctly or by mistake) produced a target. The
redactor (`export.redactor.apply_plan`) then drew every target it was given; it had
no way to know whether a target was authorized.

### RC4 - Bulk operations approved review items
* `app/cli.py --accept-all`: `set_state(all candidates, ACCEPTED)` - every held and
  flagged item.
* `app/batch.py::redact_everything()` (GUI "Process all" from the final prompt):
  the same, for every document in the batch.
* `app/ui/main_window.py::_redact_all` ("Redact everything"): the same, behind a
  confirmation dialog that did not say uncertain items were included.
None of these is a per-item human judgement.

### RC5 - Evidence shortcuts in `app/detection/protection.py`
* `candidate_strength()`: every `Source.GROUP` candidate is "strong"; a REGEX
  candidate of an identifier type is "strong" with no label or context.
* `_label_bound()`: any `group_id` counted as label binding, without checking the
  group's label is a recognised, typed, identity label that expects this type
  (an UNKNOWN label-shaped caption also creates a group).
* `apply_ai_objections()`: a strong candidate the model objected to stayed
  applied - only flagged - and flagged items were applied by RC1.
* `is_proposal()` exempts REGEX, so `hold_unconfirmed_proposals` never held an
  unlabelled regex match (e.g. a five-digit number typed POSTAL_CODE).

### RC6 - Rules that type by shape alone (`resources/rules/pii_rules.yaml`)
`us_zip` (any five digits), `ssn_plain_unlabelled` (any nine digits; held since
0.41.0), `phone_*` and date rules carry no context requirement. By themselves
they are only proposals; combined with RC1 they were applied.

### RC7 - Entity operations could approve context-ambiguous occurrences
`DecisionManager.apply_to_entity` and `apply_to_all` set the chosen state on every
linked candidate, including occurrences flagged as context-ambiguous. One
approval therefore approved occurrences the user never looked at.

### RC8 - Status overstated completeness
`session.process()` reported `EXPORT VERIFIED` when the written file passed
verification, even if review-required items were still undecided (and left
readable). It never claimed "anonymized", but did not say review was incomplete.

### Not a root cause (checked)
* The redactor cannot create targets; it only draws what the plan gives it.
* The second pass (`session.run_second_pass`) registers its findings through the
  same `register()` - so it inherited RC1 but is not a separate bypass.
* Manual additions (`Source.MANUAL`) are explicit user decisions by definition.

## Fix (0.42.0) - one authority, enforced twice

1. `app/decisions/authorization.py::assess()` is the ONE policy: it decides from a
   candidate's evidence whether it may be applied automatically, and why not.
2. `register()` is fail-CLOSED: auto-approved only when `assess()` says so; every
   other candidate is REVIEW_REQUIRED (pending, readable, listed with the reason).
3. Every decision records its ORIGIN (AUTO / USER / REVIEW_REQUIRED) and a reason.
   `set_state` from the UI records USER; registration records AUTO.
4. `build_plan` re-runs `assess()` on every AUTO approval and refuses any candidate
   that is not authorized, recording the refusal; each target carries its
   authorization. `apply_plan` refuses a target without authorization.
5. `--accept-all`, `redact_everything()` and "Redact everything" approve only what
   the policy auto-approves; review-required items stay pending.
6. Entity/apply-to-all operations skip context-ambiguous occurrences.
7. The status says when review is incomplete.
