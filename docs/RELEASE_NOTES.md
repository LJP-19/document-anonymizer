### What changed in 0.42.7

**macOS: the app no longer shows "damaged and can't be opened."** Since 0.42.1 the
build wrote the version number into the app after its signature was applied, which
broke the signature, and macOS calls a downloaded app with a broken signature
"damaged" with no way to open it. The signature is now re-applied after that step,
and the build fails if it is not valid. The DMG is still not signed by an Apple
developer account or notarized, so macOS asks once before the first launch: open the
app, then System Settings > Privacy & Security > Open Anyway (macOS 14 and earlier:
right-click the app > Open). After that it opens normally.

### What changed in 0.42.6

**A person's first name or surname alone is replaced everywhere.** Once a field label
confirms the person ("Your first name", "Last name"), the name on its own is replaced
on every page, in sentences too ("Dear Ines,", "Quell agreed", "the Okonkwo family"),
with the same pseudonym as everywhere else. The one exception: a surname joined to
another name or a company word ("Smith & Wesson", "Smith Holdings LLC") names
something else and waits for your review.

**A first name a model mistook for a company is the person again.** If a model called
the spouse's first name an "employer name" on one page, that wrong type was carried
into later sentences and the name waited for review; a label-confirmed name now wins.

**The W-2 control number (box d) is replaced.** Its value is centred in a wide box
far from its caption, so it was never tied to the caption; it is now, and it keeps its
format ("A7731-0042" becomes another letter-digit code of the same shape).

### What changed in 0.42.5

**Amounts are never changed automatically - enforced at the last step.** Whatever any
detector, label or model concludes, a value shaped like an amount ($1,200.00,
612450.00, 2,905., (1,500), 12.5%) is left for a person. The export check now also
compares bare amounts as tax forms print them (W-2 "176100.00"), not only "$" amounts.

**One pseudonym per value across the whole document, for every kind of value.**
Addresses are now replaced part by part, like names: the same street, city and ZIP get
the same replacement wherever they appear ("Cupertino" in the 1040 City box and
"Cupertino, CA 95014" on the W-2). The state is never changed. State tax IDs keep
their own format.

**Firm and employer names are replaced** when their caption identifies them (Firm's
name, W-2 box c Employer's name). A company only a model guessed at still waits for
your review.

**Short surnames** (Lin, Lim, Wu...) in their own name fields and in the IRS name
control are replaced; a lone surname in ordinary sentences still waits for review.

**Fixes:** the macOS test failure (a model's guess could hide a confirmed person's
name in a sentence); form numbers 8814/4972 and the W-2 "22222" are form text;
the export check no longer fails on words a form shares with a firm's name.

### What changed in 0.42.4

Tested on the official IRS Form 1040 and Form W-2, filled with an invented return.

**Form W-2 wage and tax amounts are never changed.** "3 Social security wages" and
"5 Medicare wages and tips" were read as SSN and Medicare-number fields, so their
amounts were replaced. A box caption is now typed by its last word - "wages",
"withheld", "tips", "benefits" describe amounts.

**More of every return is replaced automatically** (all of it on the test 1040 and
W-2, except the firm/employer name, which is always your decision):
* dependents 2-4 on the 1040 (only the first dependent column was recognised);
* the ZIP and State boxes when the value is centred in its box;
* the W-2 employee address (its caption is printed under the box);
* the preparer's PTIN (it was guessed to be a medical record number);
* the same address repeated on a cover letter, the IRS name control on the payment
  voucher, and a confirmed person's surname in a later statement;
* names and IDs in a table under "Employee | Employee ID" style headers;
* "Ordering provider: Dr. ..." and similar medical roles.

**Fewer false alarms in the review list:** form line numbers ("1a", "24"), "Are
blind", "Software Architect" and "Deceased MM DD YYYY" are no longer proposed.

**Replacement phone numbers are valid US numbers** (no area code starting with 0/1).

Measured on the fictional benchmark returns: recall 84.5% -> 99.1%, with zero form
or amount words changed (precision 100%).

### What changed in 0.42.3

**Fixes SSNs, routing and account numbers leaking on the IRS fillable Form 1040.**
When a return is filled in the IRS's own fillable PDF (or printed by software that
puts one digit in each box), every digit of an SSN, routing number and account
number sits in its own box. Earlier versions read each box as a separate one-digit
value: on a test 2025 Form 1040, 7 of the 9 digits of each SSN and the full bank
routing and account numbers stayed readable in the output - and the export check
still said EXPORT VERIFIED, because it read the saved file the same broken way.
Boxed digits are now read as one value, so they are detected and replaced; and
the export check reads boxed fields the same way, so a boxed SSN that survives
now fails verification.

Still held for your review on a Form 1040 (left readable until you decide):
dependents in the 2nd-4th columns, the preparer's PTIN, the ZIP code, and the
firm name.

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
