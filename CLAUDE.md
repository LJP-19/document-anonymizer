# Working rules for this repository

The repository is the single source of truth. A change that exists only in a
chat reply has not been made.

## Every substantive turn

Before: read the files you are about to change. The current code wins over any
earlier description of it, including this file.

After: leave the tree consistent — imports resolve, tests reflect the change,
`requirements*.txt`, `buildtools/build.py` and `.github/workflows/` still match
what the app now needs. Say which files you actually changed.

## Non-negotiable behaviours

1. **Labels are never redacted.** `Name:` stays; the value goes.
2. **A logical field group is transformed as a unit.** If a label implies PII,
   every value line bound to it is a target, including lines no detector
   classified. Partial coverage of a value line is a critical bug.
3. **Redaction is permanent.** `add_redact_annot` + `apply_redactions`. Never a
   white box, never an annotation, never an overlay.
4. **Replacement text is real red text in the content stream**, verified by
   reading span colour back from the saved file.
5. **One transformation path.** `export.redactor.apply_plan` only. If you write
   a second one, preview and export will diverge and the tests will not catch it.
6. **Financial values are preserved** unless explicitly targeted.
7. **A failed validator never deletes a detection** when label context supports
   it. Lower the confidence and flag Needs Review instead.
8. **Never claim more certainty than the checks support.** The status string is
   `EXPORT VERIFIED`, not `VERIFIED`, and never `ANONYMIZED`.
9. **No real PII in the repository**, including in tests and sample files.
10. **No network at runtime.** Models and rules are bundled. GitHub is for
    source control and CI only.

## Detection layers

Rules -> GLiNER -> shape heuristics -> field groups -> resolution -> compound
split -> subject identification -> propagation -> value widening -> coverage.

`LINE_GAP_FACTOR` in groups.py is measured, not guessed: within a field, lines
sit ~0.02 of line height apart; the gap before the next field is ~0.6. Do not
raise it above 0.45 without re-measuring.

Edits to constants must be verified by reading the file back. A failed
string-replace is silent and looks exactly like a fix that did not work.

## Detection layers (detail)

Rules -> GLiNER -> shape heuristics -> field groups -> coverage -> resolution.
Never delete a layer to fix a bug in another. GLiNER is label-conditioned: add a
label to `LABELS` in `app/detection/gliner.py` rather than writing a regex for an
entity a model can name. Its non-PII labels are veto evidence, not detections.

A currency or percentage token is never a candidate, whatever the model says.

## Hidden content

Page text is not the whole document. Metadata, annotations, attachments and
bookmarks carry identity and are stripped or rewritten in `app/document/hidden.py`.
Verification checks the saved file for all four. Never add a code path that
writes a PDF without going through `sanitize_hidden`.

## Offline

`tests/test_offline.py` blocks every socket and runs the full pipeline. If a
change makes any layer reach the network, that suite fails. Do not skip it, and
do not remove the environment pins in `app/__init__.py`.

## Threading

MuPDF is not safe to call from several threads. Every PyMuPDF call goes through
`PDF_LOCK` in `app/document/pdflock.py`. Never open, render or save a document
outside it.

GUI teardown crashes intermittently in PySide6 and is a known open defect; CI
gates on `-m "not gui"`. Do not "fix" it by weakening the engine suite.

## Bug protocol

Reproduce, find the layer that is actually wrong, fix it there, add a regression
test to `tests/test_regression.py`, run the suite. Do not patch the symptom.

Bugs already fixed and locked behind tests — do not reintroduce them:

- coverage threshold below ~0.95 leaves fragments like ` 4B` in the output
- Faker reusing a token of the original (`John Smith` -> `John Glass`)
- `(555)` matching the accounting-negative money pattern
- SSN checksum failure silently deleting a real detection
- substring containment reporting `123` as surviving inside `$123,456`
- a group value line the user skipped being reported as residue
- replacing a `QThread` that is still running
- unanchored label matching turning a value line ("Daytime phone 408.555.0198")
  into a label, which deleted every detection on it
- spaCy tagging single capitalised form words ("Daytime") as PERSON
- 8-digit hex alpha in a Qt stylesheet (Qt needs `rgba()`)
- a render landing on a canvas the window has already destroyed
- clearing the cancel flag on entry to process_approved, losing a cancel that
  arrived first
- restarting a fade animation over a running one, leaving a panel dimmed
- moving an existing git tag instead of creating a new one, so every release
  overwrote v0.2.0
- a merge keeping old versions of edited files beside brand-new ones, so the
  tree imported file-by-file but not together
- picking the release archive by file timestamp, so a stale v0.2.0 download was
  applied over a v0.7.2 project and committed as a release
- a preflight gate that could not tell a missing third-party package from a
  broken tree, blocking every release on a machine without the dependencies
- a QThread destroyed while running at interpreter exit: every test passed but
  the process aborted with code 134, failing CI. TaskRunner.stop() must quit
  and join unconditionally, not only when isRunning() is true
- GLiNER labelling "$85,000" a date of birth
- a field value redacted in fragments because one detector's span stopped short

## UI rules

Redact is the default for every detection; Keep is the explicit exception.
`app/ui/theme.py` owns all colour, spacing and type. Widgets do not carry
ad-hoc stylesheets except for per-item accent colour.

## Layer map

A detection change propagates: taxonomy -> detector -> confidence -> grouping ->
coverage -> resolution -> plan -> transformation -> verification -> tests.

A dependency change propagates: `requirements.txt` -> `requirements-dev.txt` ->
`buildtools/build.py` (`COLLECT_ALL`) -> `.github/workflows/build-release.yml`.
