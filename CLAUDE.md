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

## Deterministic rules are case-insensitive by default

`load_rules()` compiles every rule with re.I UNLESS the rule sets
`case_sensitive: true` in the YAML. Before this, every value-matching rule
(street suffixes, PO Box, unit markers) was case-SENSITIVE while labels were
already case-insensitive - so "4049 MICHAEL CMN" matched nothing on an
all-caps government notice and was typed PERSON by a heuristic fallback
instead, producing a garbage pseudonym.

The ONE rule that must opt OUT: city_state_zip. Half the two-letter state
codes are common English words (OR, IN, HI, ME, OK...), and matching them
case-insensitively turned "you, or your spouse" into a detected address.
Never remove `case_sensitive: true` from that rule without re-checking this.

Also fixed in the same rule: the comma between city and state is optional
now - many real mailing blocks (IRS notices among them) omit it entirely
("CINCINNATI OH 45280-2502"), and the original pattern required one.

## Cascade search on retype (main_window.py decide_group, apply_all)

When apply_all is true, route through `session.add_manual_text(..., cascade=True,
apply_to_same=True)`, never a plain list-comprehension match against
`self.session.candidates`. Matching only existing candidates is how a value
confirmed correctly on one page stayed missed on another that was never
detected at all - `add_manual_text` already does a document-wide search and
adopts what it finds; the gap was simply not calling it from this path.

## Entity graph (app/detection/entity_graph.py)

Union-find over co-occurrence (same line, or sibling value lines under one
compound header from groups.py). Deciding one candidate marks every candidate
in its connected component reviewed too - it NEVER changes what another
node's own decision is, only that it stops needing a separate look. Reads
groups.py's output; never re-derives grouping.

## PIILeakError (app/verification/verifier.py raise_if_leaked)

A thin exception wrapper around the EXISTING verify() report, for a caller
that wants exception-based flow control. Adds no new checking logic - `verify()`
already re-opens the saved file and confirms every accepted original is gone.
Only CRITICAL checks raise; a non-critical failure (financial preservation is
cosmetic-severity in some contexts) passes through.

## A cross-line match needs one Candidate per physical line

Paragraph-context Presidio can find a match spanning a "\n" join between two
Line objects ("Marisol\nEtxeberria"). There is no single Line spanning two
rows, so `_split_across_lines` breaks it into one candidate per line it
actually touches - never try to build one Candidate covering two Line
objects.

## The insert_text fallback must NEVER be dropped from _insert_replacement

insert_textbox has an internal HEIGHT requirement - roughly 1.6-1.7x the font
size for one line - that a tightly-capped, neighbour-aware pad can
legitimately fail to satisfy (real, dense documents have rows that close
together). When all four fitting attempts fail on height, the ONLY remaining
path is insert_text, which places at a baseline point with no box-height
check at all. This block was ACCIDENTALLY DROPPED during an edit in this
session and caused two replacements in an ordinary paragraph fixture to
vanish entirely - no error, no warning, just missing text. Every edit to
this function must end by confirming `"insert_text("` still appears in it.

## Plain get_text() is not proof of a visual merge - sample pixels or spans

PyMuPDF's plain get_text() joins two adjacent spans into ONE STRING with no
space whenever there is no whitespace CHARACTER between them, even when
there is real, visible pixel separation on the page. A "Mark & Macey Lang"
->"Fischer Robert Smith" APPEARING merged in plain get_text() output is not,
on its own, proof the page renders badly - sample actual pixels (a blank
horizontal run) or check get_text("dict") span x-coordinates before
concluding two replacements visually collide. Chasing this distinction
without checking it first can waste a lot of time solving a problem that
does not visually exist.

## The redactor was reverted to the LITERAL v0.8.3 code, on direct instruction

The user provided an actual archived zip of v0.8.3 rather than asking for a
memory-based reconstruction. `_insert_replacement`/`_fit_font_size`/`_safe_font`
now match that file exactly (font-flags-aware weight selection kept, since it
is unrelated to spacing). Every headroom/neighbour-capping function
(`_headroom_above/_below/_right`, `_row_headroom`) is REMOVED, not dormant -
they do not exist in this file anymore.

Known, ACCEPTED trade-off, not an oversight: this has zero protection against
two adjacent targets' boxes overlapping on a VERY tightly-spaced document
(rows a fraction of a point apart). One test
(`test_every_occurrence_survives_to_the_final_output` in test_propagation.py)
is marked xfail for exactly this synthetic case. Do NOT silently re-add
headroom capping to "fix" that xfail - it was tried, and the neighbour-aware
version was reported as rendering WORSE on real documents, repeatedly and
consistently, which is why this revert happened. Only change this again on
explicit instruction, not on grounds of "this looks more robust."

Everything else built later this session that is NOT about padding/growth
stays: `_merge_overlapping_targets` in plan.py (fixes duplicate-detection
overlaps, confirmed absent from v0.8.3's plan.py, unrelated to this),
blackout mode, font flags/origin capture, the entity graph, Presidio,
case-insensitive rules, all detection-side work untouched per the user's
explicit "only redaction, not detection" instruction.

## Simplifying the redactor's box-growth logic has been tried twice and failed both times

Removing growth/neighbour-capping "back to something simpler" broke a real,
previously-working tight-spacing case within minutes, twice, in different
ways (a missing replacement, then an empty page). The neighbour-aware
padding and growth caps are not complexity for its own sake - they are the
fix for specific, reproduced collision bugs on real dense documents. Do not
attempt a broad simplification of this function again without a specific,
reproduced problem to fix; if asked to, point back to this entry.

## Horizontal box growth must be capped against the NEXT same-row target too

`_headroom_right` in redactor.py mirrors `_headroom_above`/`_headroom_below`:
rightward growth (for a replacement wider than the original) is capped at
the nearest OTHER target on the same row, never just at the page edge. Left
uncapped, one short replacement's box grew straight into the next one on the
same line - "Mark & Macey Lang" -> "Fischer" and "Robert Smith" rendered
merged into one run, "Fischer Robert Smith", with the "&" separator
untouched between them but visually swallowed by the overlap. This was open
as a known xfail for several rounds; fixed properly, not just accepted.

## Font flags/origin are captured for weight and precise baseline ONLY

`Span.flags`/`Span.origin` preserve bold/italic and exact baseline position.
`_safe_font` reads the FLAGS BITMASK first (bit 4 = bold, bit 1 = italic),
falling back to the font-name substring check only when flags are 0 - flags
catch an obfuscated embedded font name a substring check cannot see at all.
Color is NEVER touched by any of this: replacement text stays red, always -
it is a deliberate, verified security signal (`replacement text is red` is
one of the 14 automated checks). Do not add color fidelity without the
person explicitly asking to trade that signal away.

## Geometric label exclusion is a SEPARATE safety net from strip_label_overlaps

`veto_by_label_geometry` in resolve.py checks true bounding-box overlap
(>=50% of the candidate's own area) against every label, regardless of which
Line either side belongs to. `strip_label_overlaps` only clips overlap
WITHIN one Line by character offset - it misses extraction splitting one
visual line into two Line objects, and a proposal landing on a different but
geometrically overlapping line. Keep both; they catch different failures.

## Presidio is a proposal layer only (app/detection/presidio_adapter.py)

Optional dependency, matching the LLM's pattern (requirements-presidio.txt).
It runs AFTER groups.py, since its context comes from groups.py's own label
output - never re-parses labels itself. Reuses the already-loaded
en_core_web_sm spaCy model (never en_core_web_lg, never a second copy). Every
result is a Source.NER candidate capped at confidence 0.6 with needs_review -
it proposes; the deterministic layers, label strip and form-text veto still
decide. An entity type with no ENTITY_MAP mapping stays UNCLASSIFIED, so
type-or-skip holds for it too.

If touching this file: the real presidio-analyzer NlpEngine ABC requires
get_supported_entities, is_loaded, and process_batch in addition to
process_text/is_stopword/is_punct - verified against the installed package,
not assumed. Check the real interface again if presidio-analyzer's major
version changes.

## Final review widget removal must detach before deleting

`rebuild_final_list` uses `layout.takeAt(0)` to remove a widget IMMEDIATELY,
then `deleteLater()` on the detached widget. The previous version relied on
`layout.count()` staying accurate after a `setParent(None)` + `deleteLater()`
call - but deleteLater only frees the object on the NEXT event loop turn, so a
second rebuild in the same call stack (deciding an item right after entering
final review) touched a widget mid-deletion and segfaulted the process. This
is very likely what "(Not Responding)" in the app title bar was.

## Careful mode must not exclude Source.GROUP

GROUP is label-confirmed evidence, not a guess, and it is the ONLY mechanism
that creates a joint name's household split. Excluding it (as an earlier
version did) makes Careful mode miss every joint name and stacked-field value.

## Joint names need a standalone detector, not only a grouped one

`detect_joint_names` in heuristics.py matches "X & Y Z" / "X and Y Z" on ANY
line, independent of any label or field group. The group-based household split
only fires when a name-type label is nearby; a joint name in a signature block
or elsewhere with no such label was invisible without this.

## Label words need a wide vocabulary, not just the field's own abbreviation

`INLINE_LABELS` must include every word of a multi-word label ("social",
"security", not just "ssn"), or widening can absorb part of the label into a
value on the same line. There is now also a FINAL strip_label_overlaps pass
after every widening/propagation/joint-name step, as a safety net for
whatever the per-word list still misses.

## Repetition suppression must exempt ANY already-detected line, not just labelled ones

`detect_form_text` takes the full `candidates` list, not only `labels`/`groups`.
Every line a detector has already put a candidate on is exempt from the
repeated-position veto - the previous, narrower fix only exempted group-bound
values, which missed a very common real shape: an address, ID or other value
block printed identically on every page of a multi-page return with NO
preceding label at all. Never narrow this exemption back to labels/groups only.

## Button labels must match what a button actually does

The footer button still read "Process N approved" after the mandatory
final-review gate changed what it does (redirect, not write). Renamed to
"Continue to final review (N)". Any behaviour change to a control's action
requires checking its label in the same edit.

`detect_form_text`'s repeated-position rule fires on any line at the same spot
across 3+ pages - which is exactly what a name field filled into the same box
on every page of a form looks like. It must exclude every line already bound to
a PII-typed field's value (via `groups`, not just `labels`), and propagated
hits (Source.COVERAGE) must never be re-vetoed by it on a later pass, since
propagation already confirmed the value belongs to the document's subject.

## Text-box padding: symmetric by default, capped by REAL neighbours only

Replacement text padding is 0.35x font size, up AND down, matching every
version through v0.25.0. Do not make it asymmetric or scale it to the target's
own height "for safety" - that under-pads ordinary documents to guard against a
collision that only happens on unusually tight synthetic fixtures. If a
collision case needs guarding, cap the pad using the REAL nearest neighbouring
target on the page (`_headroom_above`/`_headroom_below` in redactor.py), never
a guess based on the target's own geometry.

`_insert_replacement`'s box growth is downward-only, scaled to the target's OWN
row height (never the font size against absolute page coordinates). The old
symmetric 0.35x-font-size pad could exceed the gap to the row above on tightly
leaded documents and render replacement text overlapping the label two rows up.

## Qt lambdas connected to a signal must accept the signal's argument

A zero-arg lambda connected to `.clicked` (which emits `checked: bool`) can
silently fail to fire depending on binding path. Always `lambda _checked=False: ...`.
A GUI test helper that builds tabs by hand instead of through the real
add-tab path will silently skip any button wiring done there - mirror the real
path exactly, do not shortcut it.

## The LLM flag is --with-llm, everywhere

Renamed from --no-llm when the model went opt-in. When touching build.py or
verify_bundle.py, grep for BOTH names before editing - a half-renamed flag
passed PyInstaller successfully on both platforms and only failed at the very
last self-check step, which is expensive to catch.

## The audit model is off

It proposed form labels, headings and figures as often as real misses, and each
acceptance was a chance to damage a document. `DOCANON_LLM=1` re-enables it;
nothing ships it. GLiNER stays - it TYPES values rather than proposing them,
which is the safe direction.

## The review model is advisory (when enabled)

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

## Deterministic rules are case-insensitive by default

`load_rules()` compiles every rule with re.I UNLESS the rule sets
`case_sensitive: true` in the YAML. Before this, every value-matching rule
(street suffixes, PO Box, unit markers) was case-SENSITIVE while labels were
already case-insensitive - so "4049 MICHAEL CMN" matched nothing on an
all-caps government notice and was typed PERSON by a heuristic fallback
instead, producing a garbage pseudonym.

The ONE rule that must opt OUT: city_state_zip. Half the two-letter state
codes are common English words (OR, IN, HI, ME, OK...), and matching them
case-insensitively turned "you, or your spouse" into a detected address.
Never remove `case_sensitive: true` from that rule without re-checking this.

Also fixed in the same rule: the comma between city and state is optional
now - many real mailing blocks (IRS notices among them) omit it entirely
("CINCINNATI OH 45280-2502"), and the original pattern required one.

## Cascade search on retype (main_window.py decide_group, apply_all)

When apply_all is true, route through `session.add_manual_text(..., cascade=True,
apply_to_same=True)`, never a plain list-comprehension match against
`self.session.candidates`. Matching only existing candidates is how a value
confirmed correctly on one page stayed missed on another that was never
detected at all - `add_manual_text` already does a document-wide search and
adopts what it finds; the gap was simply not calling it from this path.

## Entity graph (app/detection/entity_graph.py)

Union-find over co-occurrence (same line, or sibling value lines under one
compound header from groups.py). Deciding one candidate marks every candidate
in its connected component reviewed too - it NEVER changes what another
node's own decision is, only that it stops needing a separate look. Reads
groups.py's output; never re-derives grouping.

## PIILeakError (app/verification/verifier.py raise_if_leaked)

A thin exception wrapper around the EXISTING verify() report, for a caller
that wants exception-based flow control. Adds no new checking logic - `verify()`
already re-opens the saved file and confirms every accepted original is gone.
Only CRITICAL checks raise; a non-critical failure (financial preservation is
cosmetic-severity in some contexts) passes through.

## A cross-line match needs one Candidate per physical line

Paragraph-context Presidio can find a match spanning a "\n" join between two
Line objects ("Marisol\nEtxeberria"). There is no single Line spanning two
rows, so `_split_across_lines` breaks it into one candidate per line it
actually touches - never try to build one Candidate covering two Line
objects.

## The insert_text fallback must NEVER be dropped from _insert_replacement

insert_textbox has an internal HEIGHT requirement - roughly 1.6-1.7x the font
size for one line - that a tightly-capped, neighbour-aware pad can
legitimately fail to satisfy (real, dense documents have rows that close
together). When all four fitting attempts fail on height, the ONLY remaining
path is insert_text, which places at a baseline point with no box-height
check at all. This block was ACCIDENTALLY DROPPED during an edit in this
session and caused two replacements in an ordinary paragraph fixture to
vanish entirely - no error, no warning, just missing text. Every edit to
this function must end by confirming `"insert_text("` still appears in it.

## Plain get_text() is not proof of a visual merge - sample pixels or spans

PyMuPDF's plain get_text() joins two adjacent spans into ONE STRING with no
space whenever there is no whitespace CHARACTER between them, even when
there is real, visible pixel separation on the page. A "Mark & Macey Lang"
->"Fischer Robert Smith" APPEARING merged in plain get_text() output is not,
on its own, proof the page renders badly - sample actual pixels (a blank
horizontal run) or check get_text("dict") span x-coordinates before
concluding two replacements visually collide. Chasing this distinction
without checking it first can waste a lot of time solving a problem that
does not visually exist.

## The redactor was reverted to the LITERAL v0.8.3 code, on direct instruction

The user provided an actual archived zip of v0.8.3 rather than asking for a
memory-based reconstruction. `_insert_replacement`/`_fit_font_size`/`_safe_font`
now match that file exactly (font-flags-aware weight selection kept, since it
is unrelated to spacing). Every headroom/neighbour-capping function
(`_headroom_above/_below/_right`, `_row_headroom`) is REMOVED, not dormant -
they do not exist in this file anymore.

Known, ACCEPTED trade-off, not an oversight: this has zero protection against
two adjacent targets' boxes overlapping on a VERY tightly-spaced document
(rows a fraction of a point apart). One test
(`test_every_occurrence_survives_to_the_final_output` in test_propagation.py)
is marked xfail for exactly this synthetic case. Do NOT silently re-add
headroom capping to "fix" that xfail - it was tried, and the neighbour-aware
version was reported as rendering WORSE on real documents, repeatedly and
consistently, which is why this revert happened. Only change this again on
explicit instruction, not on grounds of "this looks more robust."

Everything else built later this session that is NOT about padding/growth
stays: `_merge_overlapping_targets` in plan.py (fixes duplicate-detection
overlaps, confirmed absent from v0.8.3's plan.py, unrelated to this),
blackout mode, font flags/origin capture, the entity graph, Presidio,
case-insensitive rules, all detection-side work untouched per the user's
explicit "only redaction, not detection" instruction.

## Simplifying the redactor's box-growth logic has been tried twice and failed both times

Removing growth/neighbour-capping "back to something simpler" broke a real,
previously-working tight-spacing case within minutes, twice, in different
ways (a missing replacement, then an empty page). The neighbour-aware
padding and growth caps are not complexity for its own sake - they are the
fix for specific, reproduced collision bugs on real dense documents. Do not
attempt a broad simplification of this function again without a specific,
reproduced problem to fix; if asked to, point back to this entry.

## Horizontal box growth must be capped against the NEXT same-row target too

`_headroom_right` in redactor.py mirrors `_headroom_above`/`_headroom_below`:
rightward growth (for a replacement wider than the original) is capped at
the nearest OTHER target on the same row, never just at the page edge. Left
uncapped, one short replacement's box grew straight into the next one on the
same line - "Mark & Macey Lang" -> "Fischer" and "Robert Smith" rendered
merged into one run, "Fischer Robert Smith", with the "&" separator
untouched between them but visually swallowed by the overlap. This was open
as a known xfail for several rounds; fixed properly, not just accepted.

## Font flags/origin are captured for weight and precise baseline ONLY

`Span.flags`/`Span.origin` preserve bold/italic and exact baseline position.
`_safe_font` reads the FLAGS BITMASK first (bit 4 = bold, bit 1 = italic),
falling back to the font-name substring check only when flags are 0 - flags
catch an obfuscated embedded font name a substring check cannot see at all.
Color is NEVER touched by any of this: replacement text stays red, always -
it is a deliberate, verified security signal (`replacement text is red` is
one of the 14 automated checks). Do not add color fidelity without the
person explicitly asking to trade that signal away.

## Geometric label exclusion is a SEPARATE safety net from strip_label_overlaps

`veto_by_label_geometry` in resolve.py checks true bounding-box overlap
(>=50% of the candidate's own area) against every label, regardless of which
Line either side belongs to. `strip_label_overlaps` only clips overlap
WITHIN one Line by character offset - it misses extraction splitting one
visual line into two Line objects, and a proposal landing on a different but
geometrically overlapping line. Keep both; they catch different failures.

## Presidio is a proposal layer only (app/detection/presidio_adapter.py)

Optional dependency, matching the LLM's pattern (requirements-presidio.txt).
It runs AFTER groups.py, since its context comes from groups.py's own label
output - never re-parses labels itself. Reuses the already-loaded
en_core_web_sm spaCy model (never en_core_web_lg, never a second copy). Every
result is a Source.NER candidate capped at confidence 0.6 with needs_review -
it proposes; the deterministic layers, label strip and form-text veto still
decide. An entity type with no ENTITY_MAP mapping stays UNCLASSIFIED, so
type-or-skip holds for it too.

If touching this file: the real presidio-analyzer NlpEngine ABC requires
get_supported_entities, is_loaded, and process_batch in addition to
process_text/is_stopword/is_punct - verified against the installed package,
not assumed. Check the real interface again if presidio-analyzer's major
version changes.

## Final review widget removal must detach before deleting

`rebuild_final_list` uses `layout.takeAt(0)` to remove a widget IMMEDIATELY,
then `deleteLater()` on the detached widget. The previous version relied on
`layout.count()` staying accurate after a `setParent(None)` + `deleteLater()`
call - but deleteLater only frees the object on the NEXT event loop turn, so a
second rebuild in the same call stack (deciding an item right after entering
final review) touched a widget mid-deletion and segfaulted the process. This
is very likely what "(Not Responding)" in the app title bar was.

## Careful mode must not exclude Source.GROUP

GROUP is label-confirmed evidence, not a guess, and it is the ONLY mechanism
that creates a joint name's household split. Excluding it (as an earlier
version did) makes Careful mode miss every joint name and stacked-field value.

## Joint names need a standalone detector, not only a grouped one

`detect_joint_names` in heuristics.py matches "X & Y Z" / "X and Y Z" on ANY
line, independent of any label or field group. The group-based household split
only fires when a name-type label is nearby; a joint name in a signature block
or elsewhere with no such label was invisible without this.

## Label words need a wide vocabulary, not just the field's own abbreviation

`INLINE_LABELS` must include every word of a multi-word label ("social",
"security", not just "ssn"), or widening can absorb part of the label into a
value on the same line. There is now also a FINAL strip_label_overlaps pass
after every widening/propagation/joint-name step, as a safety net for
whatever the per-word list still misses.

## Repetition suppression must exempt ANY already-detected line, not just labelled ones

`detect_form_text` takes the full `candidates` list, not only `labels`/`groups`.
Every line a detector has already put a candidate on is exempt from the
repeated-position veto - the previous, narrower fix only exempted group-bound
values, which missed a very common real shape: an address, ID or other value
block printed identically on every page of a multi-page return with NO
preceding label at all. Never narrow this exemption back to labels/groups only.

## Button labels must match what a button actually does

The footer button still read "Process N approved" after the mandatory
final-review gate changed what it does (redirect, not write). Renamed to
"Continue to final review (N)". Any behaviour change to a control's action
requires checking its label in the same edit.

`detect_form_text`'s repeated-position rule fires on any line at the same spot
across 3+ pages - which is exactly what a name field filled into the same box
on every page of a form looks like. It must exclude every line already bound to
a PII-typed field's value (via `groups`, not just `labels`), and propagated
hits (Source.COVERAGE) must never be re-vetoed by it on a later pass, since
propagation already confirmed the value belongs to the document's subject.

## Text-box padding: symmetric by default, capped by REAL neighbours only

Replacement text padding is 0.35x font size, up AND down, matching every
version through v0.25.0. Do not make it asymmetric or scale it to the target's
own height "for safety" - that under-pads ordinary documents to guard against a
collision that only happens on unusually tight synthetic fixtures. If a
collision case needs guarding, cap the pad using the REAL nearest neighbouring
target on the page (`_headroom_above`/`_headroom_below` in redactor.py), never
a guess based on the target's own geometry.

`_insert_replacement`'s box growth is downward-only, scaled to the target's OWN
row height (never the font size against absolute page coordinates). The old
symmetric 0.35x-font-size pad could exceed the gap to the row above on tightly
leaded documents and render replacement text overlapping the label two rows up.

## Qt lambdas connected to a signal must accept the signal's argument

A zero-arg lambda connected to `.clicked` (which emits `checked: bool`) can
silently fail to fire depending on binding path. Always `lambda _checked=False: ...`.
A GUI test helper that builds tabs by hand instead of through the real
add-tab path will silently skip any button wiring done there - mirror the real
path exactly, do not shortcut it.

## The LLM flag is --with-llm, everywhere

Renamed from --no-llm when the model went opt-in. When touching build.py or
verify_bundle.py, grep for BOTH names before editing - a half-renamed flag
passed PyInstaller successfully on both platforms and only failed at the very
last self-check step, which is expensive to catch.

## The audit model is off

It proposed form labels, headings and figures as often as real misses, and each
acceptance was a chance to damage a document. `DOCANON_LLM=1` re-enables it;
nothing ships it. GLiNER stays - it TYPES values rather than proposing them,
which is the safe direction.

## The review model is advisory (when enabled)

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

## Names are mapped per TOKEN

`app/pseudonymization/names.py`. "John" and "Smith" each get one substitution,
and every form composes from them - full name, surname alone, given name alone,
surname-first, and both halves of a joint name. Mapping whole strings gave the
same person a different identity on every page.

A pseudonym never reuses a real name from the document: replacing "Jenny" with
"John" while a real John is present is worse than not replacing at all. Seed the
registry with `note_original` before generating anything.

Identifiers are keyed by their DIGITS, so one SSN written three ways gets one
pseudonym.

## Superseded: the lone-surname heuristic

A single capitalised word alone on a line used to become a PERSON. It found the
occasional surname and misread a great deal of a form besides. Token mapping
covers the real case. Do not reintroduce it.

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

Do not assert a SPECIFIC value is or isn't typed/unlabelled - whether GLiNER
resolves something can go either way depending on what's installed, and CI has
it while a quick local run may not. Assert the INVARIANT instead: every value
under a label is either transformed or surfaced under Unlabelled and left
readable, never silently dropped.

A fixture value that GLiNER finds and the rules do not makes a test pass locally
and fail in CI. Run the suite BOTH ways before shipping:

    DOCANON_LLM=0 pytest tests -q                    # rules only
    python buildtools/fetch_models.py --no-llm && pytest tests -q

Assert on behaviour that holds either way.

## Expensive objects are reused, not rebuilt

`Faker()` loads every provider on construction. Building one per call ran
thousands of times per document and aborted the process inside the garbage
collector under Qt teardown. One instance per thread, seeded per call - seeding
is what gives determinism, not construction. The same caution applies to spaCy,
GLiNER and llama.cpp, all of which are already cached.

## Waiting in tests

Wait on a clock with a deadline, never on a fixed number of `processEvents()`
turns. A busy CI runner may not have started a worker thread within fifty
iterations, which fails as a real bug and is not one.

## Never ship without running the suite AFTER the last edit

A v0.18.0 archive was packaged with a missing import because the tests ran
before the final change. Package only from a tree whose last action was a green
run, both with and without the models.

## Type or skip

Nothing is replaced unless a detector said what it is. A span with no type has
no matching replacement, and generating one wrote corrupt text over real
documents. Untyped values are surfaced under "Unlabelled", left readable, and
wait for the user to name them. Two exceptions: a manual addition (the user
already said), and a blackout (needs no type).

This supersedes the earlier fail-safe rule that redacted everything found. A
miss the user can see beats damage they cannot.

## Final review

`set_final_review` hides the detection list and shows the document as it will be
written, with a dismiss badge on every applied change. The badges are drawn from
the PLAN, never from the candidate list - a candidate the user kept must not
show a badge.

## Careful mode

`DOCANON_CONSERVATIVE=1`, or the checkbox, restricts transformation to values
that were positively identified: matched rules, model hits above threshold, and
labelled fields. It exists because the widening passes are what damage a
document when they misfire. Manual additions always survive it.

## Overlapping targets must never both be applied

`_merge_overlapping_targets` in plan.py runs after every retyping and widening
pass, right before anything is drawn. Two candidates on the same or adjacent
geometry both redacting-and-inserting is how one replacement got drawn over
another without clearing it - visible as doubled, jumbled text in real output.

The threshold is AREA OVERLAP FRACTION (>=0.35 of the smaller rect), never
distance/padding. Padding-based adjacency merged legitimate neighbouring
fields and silently dropped one of them, which is worse than the bug it was
meant to fix.

## Name heuristics need an English-word guard

Title-case shape alone matches any short phrase. `looks_like_person` rejects a
mixed-case candidate containing any FORM_VOCABULARY word - "Need to Keep" has
three. FORM_VOCABULARY carries general English stopwords for this reason, not
only tax-form terms.

## A single-type field ends when the shape changes

A "Taxpayer name" label absorbed the street and city/state/zip lines below it
and typed all three PERSON - hiding an undetected address as a mistyped one.
`_shape_mismatch` in groups.py stops stacking when a line under a PERSON-only
label looks like a street or city/state/zip. `NAME_LIKE_MAX_LINES` also caps a
name field at 2 lines even before that check fires. Only applies to single-type
labels; a multi-type label ("name, address, and zip code") is supposed to
absorb several kinds of line.

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


## Propagation must not depend on guessing every real document's label wording

`identify_subjects` in entities_pass.py now qualifies a subject if the LABEL'S
OWN RECOGNISED TYPE matches the candidate's type (group.expected_types), not
only if the label text matches a hand-curated word list (PRIMARY_LABEL). The
word list alone meant any document whose actual label wording was not on it
never qualified ANYTHING as a subject, so propagation silently never ran at
all regardless of page count. Keep the type-based check as the PRIMARY
signal; the word list is now a fallback, not the only path.

## Bare role labels need their own list, separate from the "...name" pattern

The person-label pattern requires the literal word "name" ("taxpayer name",
"full name"). A form saying just "Policyholder" or "Beneficiary" with no
trailing "name" does not match it at all - there is a SEPARATE bare-word
list in pii_rules.yaml for exactly this shape (responsible party, trustee,
beneficiary...). Add missing real-world role words there, never to the
name-suffix pattern.

## Business names need core-name keying, not just whole-string normalization

`EntityRegistry._org_core` strips the entity suffix (LLC/Inc/Corp/etc) for
KEYING only - "Acme Holdings LLC", "Acme Holdings, L.L.C.", and a bare "Acme
Holdings" (suffix dropped by one occurrence) all key to the same pseudonym.
The FIRST occurrence's cached pseudonym (with its own suffix) is what every
later occurrence reuses, same as every other type already works - this is
not new behaviour, just extending it to ORG_PRIVATE, which previously had no
token- or core-name fallback the way PERSON already did.

## Adjudication is additive - never replace how confidence is set elsewhere

`app/detection/adjudication.py` reads a candidate's existing `.evidence` list
and computes a SEPARATE judgement (`Candidate.adjudication`:
CONFIRMED/PROBABLE/UNRESOLVED) using per-type thresholds. It must never become
the thing that sets `.confidence` itself - every existing pass that sets
confidence directly stays as the single source of truth for that field.
Adjudication is a second read of the same evidence, layered on top.

## UNRESOLVED broadens type-or-skip; it does not replace it

`decisions/manager.py`'s `register()` defaults a candidate to SKIPPED when
either: (a) its type is UNCLASSIFIED_GROUP_VALUE (original rule - no type
means no valid replacement), or (b) its `.adjudication` is UNRESOLVED (this
addition - a known type with too little evidence is the same failure shape).
Both exemptions for Source.MANUAL stay separate and must both be preserved;
do not collapse them into one condition.

## The coverage matrix is generated, not remembered

Before claiming a PiiType is "supported," run
`python buildtools/generate_coverage_matrix.py`. It cross-checks every type
against every real detector mapping (regex types, label expects, GLiNER
LABELS, Presidio ENTITY_MAP) and fails non-zero if any type has none. Do not
add a new PiiType member without also giving it at least one real path in
that check - a taxonomy entry with no detector is exactly the trap this
script exists to catch (it already caught ACCOUNT_ID, FAX, MARITAL_STATUS,
MATTER_ID, MEDICARE_ID, PAYROLL_ID, SOCIAL_HANDLE, STATE_TAX_ID,
URL_PERSONAL once).

## The widening guard in _complete_partial_lines needs an ENGLISH-WORD check, not just FORM_VOCABULARY

`ENGLISH_FUNCTION_WORDS` in engine.py (pronouns, prepositions, everyday verbs)
is checked separately from FORM_VOCABULARY (tax/form terms) in the leftover-
token check. A short identifier inside an ordinary sentence ("Follow me
@handle on Instagram") slid under the length/token-count guard and got
widened to the whole sentence, because FORM_VOCABULARY has no reason to
contain words like "follow" or "on" - it is about tax/form terminology, not
general English. If a future widening leak involves an ordinary English
word, extend ENGLISH_FUNCTION_WORDS, not FORM_VOCABULARY.

## Run the benchmark before claiming an accuracy fix works

`python -m benchmark.run_benchmark` runs the real pipeline against 18 golden
fixtures and reports precision/recall/F1 and, most importantly, which
documents (by name) had ANY missed true positive. It is wired into the
ordinary pytest run too. A "the detector should catch X now" claim is not
verified until this reports 0 documents with any missed PII - checking a
single synthetic case in isolation is not the same thing, as the benchmark's
own discovery (the ENGLISH_FUNCTION_WORDS bug) demonstrated: an isolated
check of the new rule passed while the FULL pipeline still ate the sentence
around it.
