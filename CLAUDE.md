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

## Layer map

A detection change propagates: taxonomy -> detector -> confidence -> grouping ->
coverage -> resolution -> plan -> transformation -> verification -> tests.

A dependency change propagates: `requirements.txt` -> `requirements-dev.txt` ->
`buildtools/build.py` (`COLLECT_ALL`) -> `.github/workflows/build-release.yml`.
