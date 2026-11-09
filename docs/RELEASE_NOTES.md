### What changed in 0.42.2

**A labelled name the detection model mistook for a job title is pseudonymized
again.** "Dependent: Duong Thi Hong" was found and confirmed by its field label, but
the detection model read the unfamiliar name as an occupation, so it waited for review
instead of being replaced. A verified name label now settles that guess - only for a
value shaped like a name with no occupation word in it; "Dependent: Full-time
Student" is still held for review.

### What changed in 0.42.1

**Fixes a crash on macOS during the first-launch model download.** Choosing an AI
model larger than 2 GB (the 7B option, recommended on Macs with 16 GB of memory or
more) crashed the app ("DocumentAnonymizer quit unexpectedly", thread
`_DownloadWorker`). The download progress was passed between threads as a 32-bit
number, which a byte count above 2 GB overflows. Progress now carries the exact
byte count. The app's own version also shows in macOS dialogs (it read 0.0.0).

If the crash interrupted a download, choose the same model again: the part that
already arrived is kept and the download resumes.

### What changed in 0.42.0

**Only confirmed PII is changed without you.** One authorization policy decides, for
every detection, whether it may be pseudonymized automatically:
* a value of a recognised field label of the right type (e.g. "Your first name"),
* an identifier named by its own caption with a passing check (e.g. "SSN: ..."),
* another occurrence of a person or value a label already confirmed.

Everything else that may be personal (a name a model guessed, a bare number, a
surname in a sentence, an AI suggestion, anything flagged) is **listed for your
review and left readable** until you press Redact, Keep or Edit. Form labels,
headings, instructions, amounts, public agency addresses, organization and employer
names stay untouched. The check is repeated right before the PDF is written, and the
"redact everything" buttons and the `--accept-all` flag can no longer approve
uncertain items. The export status reads `EXPORT VERIFIED - REVIEW INCOMPLETE` while
items still wait for you.

Also: a same-row label and value are bound together; one street gets one pseudonym in
its original capitals; text an earlier version already replaced is never re-replaced.

### Known limitations
* Pages with no text layer are not analysed (no OCR); the app lists them.
* Names that no label identifies wait for review rather than being replaced
  automatically - expect more items in the review list than before.
* macOS: Apple Silicon only. Windows: x64 only; the installer is not code-signed.
