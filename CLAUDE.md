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

## Accuracy rules learned the hard way

GLiNER sees a sliding window of the PAGE in reading order, never a single
layout block. A block on a form is often one line, which left the model
classifying an isolated fragment.

Character normalization at extraction is strictly 1:1. A ligature expanding to
two characters shifts every offset after it and misplaces rectangles.

Column typing applies only to a real HEADER ROW - two or more labels on one
line. A lone stacked label belongs to the field-group logic; treating it as a
column header made it claim unrelated values down the page.

## Widening guards

`_complete_partial_lines` must keep whitespace when inspecting the uncovered
remainder. Joining only the non-space characters merged "Taxpayer SSN" into
"TaxpayerSSN", which matched no vocabulary entry, so the label was swallowed
into the value.

## One process

Analysis is a single process. The re-scan of the transformed output is a pass
inside it, not a stage with its own list or filter: its findings join the review
list like any other detection. Do not reintroduce a separate section for them.

## The review model is advisory

Its output is filtered by `is_acceptable_finding` against what the
deterministic layers already know: label regions on the page, form vocabulary,
currency patterns. Never widen the model's authority by loosening that filter -
a prompt cannot be relied on to keep a model away from captions and figures.
Company words are the one exception: a client's business IS identity.

`check_replacements` runs before anything is written: scrambled output, a date
that stopped being a date, a pseudonym repeating the original.

## Only values, never the form

`app/detection/form_text.py` identifies the document's own text - running
headers and footers, instructions, headings, recognised labels - and vetoes any
candidate sitting on it. The signals are structural, not semantic, so they hold
on forms nobody wrote a rule for. The veto runs twice: after label clipping, and
again after the later passes that add candidates.

A manual addition is never vetoed. The user has said what they want.

## Detection layers

Rules -> GLiNER -> shape heuristics -> field groups -> resolution -> compound
split -> subject identification -> propagation -> value widening -> coverage.

`LINE_GAP_FACTOR` in groups.py is measured, not guessed: within a field, lines
sit ~0.02 of line height apart; the gap before the next field is ~0.6. Do not
raise it above 0.45 without re-measuring.

Edits to constants must be verified by reading the file back. A failed
string-replace is silent and looks exactly like a fix that did not work.

## Accuracy rules learned the hard way

GLiNER sees a sliding window of the PAGE in reading order, never a single
layout block. A block on a form is often one line, which left the model
classifying an isolated fragment.

Character normalization at extraction is strictly 1:1. A ligature expanding to
two characters shifts every offset after it and misplaces rectangles.

Column typing applies only to a real HEADER ROW - two or more labels on one
line. A lone stacked label belongs to the field-group logic; treating it as a
column header made it claim unrelated values down the page.

## Widening guards

`_complete_partial_lines` must keep whitespace when inspecting the uncovered
remainder. Joining only the non-space characters merged "Taxpayer SSN" into
"TaxpayerSSN", which matched no vocabulary entry, so the label was swallowed
into the value.

## One process

Analysis is a single process. The re-scan of the transformed output is a pass
inside it, not a stage with its own list or filter: its findings join the review
list like any other detection. Do not reintroduce a separate section for them.

## The review model is advisory

Its output is filtered by `is_acceptable_finding` against what the
deterministic layers already know: label regions on the page, form vocabulary,
currency patterns. Never widen the model's authority by loosening that filter -
a prompt cannot be relied on to keep a model away from captions and figures.
Company words are the one exception: a client's business IS identity.

`check_replacements` runs before anything is written: scrambled output, a date
that stopped being a date, a pseudonym repeating the original.

## Only values, never the form

`app/detection/form_text.py` identifies the document's own text - running
headers and footers, instructions, headings, recognised labels - and vetoes any
candidate sitting on it. The signals are structural, not semantic, so they hold
on forms nobody wrote a rule for. The veto runs twice: after label clipping, and
again after the later passes that add candidates.

A manual addition is never vetoed. The user has said what they want.

## Detection layers (detail)

Rules -> GLiNER -> shape heuristics -> field groups -> coverage -> resolution.
Never delete a layer to fix a bug in another. GLiNER is label-conditioned: add a
label to `LABELS` in `app/detection/gliner.py` rather than writing a regex for an
entity a model can name. Its non-PII labels are veto evidence, not detections.

A currency or percentage token is never a candidate, whatever the model says -
and neither is any span that OVERLAPS one. `_drop_financial_values` must run
again after propagation and after the audit pass, because both add candidates.

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

A worker still running when its widgets are destroyed crashes the process -
SIGABRT or SIGBUS, with no message. Every test that starts a TaskRunner must
stop it. Never disconnect a worker's `finished` signal: it also drives the
thread's quit(), and severing it leaves the thread alive at exit.

## Model fetching order

`fetch_models.py` downloads everything it is asked for, unconditionally. It must
never skip a model because the matching runtime is not importable yet - that
makes the contents of a build depend on CI step order, which is how two builds
shipped with no audit model. In CI it runs AFTER the runtime install.

## Shipped builds are self-contained

An installer goes to machines with nothing installed. The build fails rather
than omitting a model, and `verify_bundle.py` inspects the produced bundle -
not the source tree - before anything is published. Never restore
`continue-on-error` on the LLM install step in a job that uploads an artifact.

## Optional dependencies

`llama-cpp-python` is optional and lives in `requirements-llm.txt`. It must
never appear in `requirements.txt`: its wheel has been served corrupt from the
CPU index and twice failed an entire macOS build. Absent runtime means the audit
pass reports itself disabled; every other layer is unaffected, and the packager
drops the 1.1 GB weights rather than shipping something nothing can load.

## CI installs

Every pip install retries three times with `--no-cache-dir`. A wheel can and
does arrive corrupted; without this a bad copy is cached and replays forever.

## Partial redaction

`_complete_partial_lines` widens a detection to the whole value on unlabelled,
value-shaped lines. It must stay narrow: skip lines over 44 chars or 5 tokens,
skip anything with sentence punctuation, never cross a money token or a label,
and never treat a separator ("&", "and") as uncovered value. Prose is spans;
form lines are values.

## Pseudonym quality

A replacement must be the same KIND of thing as the original, readable, and
complete. Three rules, each from a shipped bug:

- never emit scrambled characters ("asdiauguw adsasd asd")
- a label inside a value stays verbatim ("... PTIN P01234567" keeps PTIN)
- every component survives ("Fremont, CA 1234" keeps a trailing number)

Values are replaced component by component in `_pseudonym_for_unclassified`,
not as one blob.

## Qt enums

Compare dialog results against the CLASS: `QDialog.DialogCode.Accepted`. Instance
access (`dialog.Accepted`) raises AttributeError in PySide6, and an exception
inside a slot is swallowed - the button simply appears to do nothing. That one
line disabled Process, Add missed item and Edit simultaneously.

## Counting

User-facing counts are DISTINCT VALUES, not occurrences. The review list groups
repeats, so counting candidates produced "42 reviewed" beside a chip showing 0.

## Blackout vs pseudonymize

Two transformation modes. Pseudonymize substitutes red text; blackout draws a
solid bar and inserts nothing. Verification must not expect replacement text for
a blackout target - check `Target.blackout` before asserting presence or colour.

## Tests must not depend on which models are installed

A fixture value that GLiNER finds and the rules do not makes a test pass locally
and fail in CI. Run the suite BOTH ways before shipping:

    DOCANON_LLM=0 pytest tests -q                    # rules only
    python buildtools/fetch_models.py --no-llm && pytest tests -q

Assert on behaviour that holds either way.

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
