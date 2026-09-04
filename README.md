# Document Anonymizer

Offline PII detection, pseudonymization, permanent redaction and independent
verification for native-text PDFs. Everything runs locally: no cloud, no LLM
API, no telemetry, no runtime downloads.

Built for de-identifying client documents before they are shared with an AI
tool or an outside vendor.

---

## Status

**v0.1.0 — working engine, minimal GUI, release pipeline in place.**

What is implemented and covered by tests:

| Area | State |
|---|---|
| Native-text PDF extraction with character-level geometry | done |
| Scanned / image-only page detection (`OCR REQUIRED`) | done |
| Deterministic rule detection (30 rules, external YAML) | done |
| Local spaCy NER | done |
| Field-label detection and label protection | done |
| Stacked and compound logical field groups | done |
| Coverage / completeness analysis | done |
| Overlap and conflict resolution | done |
| Stable pseudonyms, entity registry | done |
| Decision manager: accept / skip / edit / apply-to-all / undo | done |
| Single transformation plan shared by preview and export | done |
| True permanent redaction + real red replacement text | done |
| Independent verification of the saved file (12 checks) | done |
| Dual-pane GUI with live transformed preview | minimal |
| Batch / multi-document processing | **not started** |
| OCR | **out of scope for V1, by design** |
| Code signing and notarization | **not configured** — see Limitations |

---

## Install for development

```bash
python -m pip install -r requirements-dev.txt
python -m pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
```

The model is installed from a pinned wheel URL. The packaged application bundles
it; it is never downloaded at runtime.

## Run

```bash
python main.py                                    # GUI
python -m app.cli analyze  path/to/file.pdf       # detect and report
python -m app.cli process  path/to/file.pdf -o out.pdf --accept-all
python -m pytest tests -q                         # regression suite
```

## Release

1. Bump `__version__` in `app/version.py` (single source of truth).
2. Edit `release/release_config.json`.
3. Double-click `release/release.bat` (Windows) or `release/release.command` (macOS).

Preflight runs, tests run, the current repository state is committed and pushed,
a `v*` tag is created, and GitHub Actions builds the Windows `.exe` and macOS
`.dmg` on native runners. See `release/README.md`.

---

## Architecture

```
app/
├── document/     text providers + document model (chars, lines, blocks, pages)
├── detection/    rules, NER, labels, field groups, coverage, conflict resolution
├── entities/     stable original -> pseudonym mapping
├── pseudonymization/  deterministic local generators
├── decisions/    what the user chose to do
├── transform/    the single TransformationPlan
├── export/       redaction, red text insertion, rendering  (preview + export)
├── verification/ independent checks against the saved file
├── ui/           PySide6 main window
└── session.py    binds the above; used by both the GUI and the CLI
```

Two structural rules hold the design together:

**One transformation path.** `export.redactor.apply_plan` is the only code that
modifies a document. The live preview renders the document that function
produces, so preview and export cannot diverge.

**Labels are evidence, not targets.** A label such as `Name:` is detected,
protected from redaction, and used to decide what its value group should
contain. That is what makes stacked fields work:

```
Name, address, and zip code        Name, address, and zip code   (black, kept)
LJP                          →     GHR                            (red)
Fremont, CA                        South Kevinside, OR            (red)
123                                180                            (red)
Taxable income: $123,456           Taxable income: $123,456       (untouched)
```

`LJP` and `123` are replaced because they are value lines of a PII-bearing
label, not because any detector recognised them.

---

## Extending detection

Edit `resources/rules/pii_rules.yaml`. No Python changes needed to add a rule,
a label, or a non-PII label. Rules support `context_any`, `context_required`,
`money_guard`, and a `validator` (`ssn`, `ein`, `luhn`, `aba`).

A validator failure never deletes a detection. If the shape matches and label
context is present, the candidate survives at lower confidence and is flagged
Needs Review. Silently dropping it is how `987-65-4321` disappeared from a
column headed `SSN` during development.

---

## Limitations — read before relying on this

**`EXPORT VERIFIED` is not `ANONYMIZED`.** Verification proves the transformation
plan was executed against the saved file: originals gone, replacements present,
replacement text genuinely red, financial values unchanged, no partial
redaction. It cannot prove detection was complete. An entity no detector found
is an entity no verifier can look for. A human still has to read the output.

**Person-name recall depends on `en_core_web_sm`.** The small spaCy model
underperforms on form-like documents. `en_core_web_lg` is a drop-in improvement
at the cost of installer size.

**Same-name people are merged.** Two unrelated people named John Smith in one
document receive the same pseudonym. Fixing this needs relationship inference
that V1 does not attempt. Edit one of them manually if it matters.

**Unsigned builds.** The `.exe` triggers SmartScreen and the `.dmg` triggers
Gatekeeper ("damaged and can't be opened"). For internal use, macOS users can
clear the quarantine flag once:
`xattr -dr com.apple.quarantine /Applications/DocumentAnonymizer.app`.
Distributing outside the firm needs an Apple Developer ID ($99/yr) plus
`codesign` and `notarytool` steps in the workflow, and ideally a Windows code
signing certificate.

**PyMuPDF is AGPL-3.0.** Internal use within one organisation is fine.
Distributing the binary outside the firm triggers source-disclosure obligations
unless you buy a commercial licence from Artifex.

**The packaged build has not been run.** `buildtools/build.py` and the workflow
are written and statically checked; no `.exe` or `.dmg` has been produced yet.
The first CI run is the real test.

---

## Test data

All fixtures in `tests/fixtures.py` are synthetic. Real client PII must never be
committed to this repository.
