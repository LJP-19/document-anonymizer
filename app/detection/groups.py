"""Logical field groups: stacked and compound PII (spec sections 14-17).

This is the module that stops the system from mistaking one detected token for
a whole sensitive region. A label such as

    Name, address, and zip code
    LJP
    Fremont, CA
    123

is bound to all three value lines, and every value line in a PII-bearing group
becomes a redaction target even if no individual detector fired on it.
"""

from __future__ import annotations

import re
from typing import Optional

from ..document.model import Document, Line
from .deterministic import MONEY_RE, PERCENT_RE, RuleSet, load_rules
from .heuristics import looks_sensitive
from .labels import detect_labels, label_line_keys
from .types import (
    Candidate,
    Evidence,
    LabelRegion,
    LogicalFieldGroup,
    PiiType,
    Source,
)

MAX_STACKED_LINES = 6
#: A single-type label whose type is one of these stacks at most this many
#: lines before shape mismatch is checked - a name is rarely more than two
#: lines (a joint name split across a wrap), while an address legitimately
#: runs three or four.
NAME_LIKE_MAX_LINES = {PiiType.PERSON: 2}
FULL_COVERAGE_RATIO = 0.95
# Measured on real form layouts: lines within one field sit ~0.02 of line height
# apart; the gap before the NEXT field is ~0.6. Anything above 0.45 swallows the
# following label and its value into this group.
LINE_GAP_FACTOR = 0.45
LEFT_EDGE_TOLERANCE = 14.0     # points of allowed left-edge drift
INLINE_MIN_GAP = -2.0

#: Which detected types satisfy which expected type during coverage analysis.
SATISFIED_BY: dict[PiiType, set[PiiType]] = {
    PiiType.ADDRESS: {PiiType.ADDRESS, PiiType.STREET, PiiType.CITY_STATE, PiiType.PO_BOX, PiiType.POSTAL_CODE},
    PiiType.POSTAL_CODE: {PiiType.POSTAL_CODE, PiiType.ADDRESS},
    PiiType.PERSON: {PiiType.PERSON},
    PiiType.TIN: {PiiType.TIN, PiiType.SSN, PiiType.EIN, PiiType.ITIN},
}

ONLY_MONEY_RE = re.compile(r"^[\s$()\-]*[\d,.]+[\s$()%]*$")


def build_groups(
    doc: Document,
    candidates: list[Candidate],
    ruleset: Optional[RuleSet] = None,
) -> tuple[list[LogicalFieldGroup], list[LabelRegion], list[Candidate]]:
    """Returns (groups, labels, extra_candidates_for_uncovered_value_lines)."""
    rs = ruleset or load_rules()
    labels = detect_labels(doc, rs)
    label_keys = label_line_keys(labels)
    lines_by_page = {p.number: p.lines_in_reading_order() for p in doc.pages}

    # The form's own typeface, learned from a strict, conservative pre-pass
    # (column-skipping off). Values set in that typeface are form text: on a
    # dense government form, letting a label bind the lines below it in its
    # own column otherwise swallowed instruction lines ("Subtract line 10 from
    # line 9") as values - measured, typed SSN and PERSON on real returns.
    from .layout_style import form_styles

    styles = form_styles(labels, _preliminary_groups(labels, lines_by_page, label_keys))

    groups: list[LogicalFieldGroup] = []
    extra: list[Candidate] = []
    claimed: set[tuple[int, int, int]] = set()

    for idx, label in enumerate(labels):
        if label.non_pii:
            continue
        if not label.expected_types and not label.unknown:
            continue
        # Another label later on the SAME line ends this field's value: in
        # "Date of birth 11/02/1979   Nationality: Brazil" the date is the first
        # field's value, and "Nationality: Brazil" is a second field, not part of it.
        later = [lb.start for lb in labels if lb.line.key() == label.line.key() and lb.start >= label.end]
        value_lines, inline_span = _value_lines_for(
            label, lines_by_page, label_keys, claimed, limit=min(later) if later else None,
            form_styles=styles,
        )
        if not value_lines and inline_span is None:
            value_lines = _values_above_caption(label, lines_by_page, label_keys, claimed, styles)
        if not value_lines and inline_span is None:
            continue

        group = LogicalFieldGroup(
            group_id=f"g{idx}",
            label=label,
            value_lines=value_lines,
            expected_types=list(label.expected_types),
            inline_span=inline_span,
        )

        if inline_span is not None:
            start, end = inline_span
            if _eligible(label, label.line.text[start:end]):
                extra.extend(_cover_span(label.line, start, end, group, candidates))
        for line in value_lines:
            claimed.add(line.key())
            start, end = _trimmed_span(line)
            if start is None:
                continue
            if not _eligible(label, line.text[start:end]):
                continue
            extra.extend(_cover_span(line, start, end, group, candidates))

        groups.append(group)

        # A ROW caption beside a table's first cell names every cell on its row:
        # Form 1040 "(1) First name | Dependent 1 | Dependent 2 | ...". Only the
        # nearest cell was bound, so dependents 2-4 waited for review with their
        # SSNs readable.
        for k, cell in enumerate(_row_continuation(label, value_lines, inline_span, lines_by_page,
                                                   label_keys, claimed, styles)):
            claimed.add(cell.key())
            cell_group = LogicalFieldGroup(
                group_id=f"g{idx}c{k + 1}", label=label, value_lines=[cell],
                expected_types=list(label.expected_types),
            )
            start, end = _trimmed_span(cell)
            if start is not None and _eligible(label, cell.text[start:end]):
                extra.extend(_cover_span(cell, start, end, cell_group, candidates))
            groups.append(cell_group)

    return groups, labels, extra


#: W-2 box f: caption 50 pt under the address it names; boxes are under ~80 pt tall.
ABOVE_CAPTION_MAX_DISTANCE = 80.0


def _values_above_caption(label, lines_by_page, label_keys, claimed, styles):
    """A caption printed at the BOTTOM of its box, under the value: W-2 box f
    "Employee's address and ZIP code" sits below the address it names, so the
    address was never bound and waited for review. Fallback only (nothing was
    found beneath the caption): the unclaimed, non-label lines directly above
    it in its own column, nearest first, up to the field's line limit. A label,
    another field's value or the form's own text ends the search.
    """
    from .layout_style import in_form_style

    if label.unknown or not label.expected_types:
        return []
    # Only a NAME/ADDRESS/COMPANY box prints its caption under a multi-line value
    # (W-2 box f). An identifier caption that found nothing beneath it is an EMPTY
    # box (the W-2's blank second copy), and reaching up bound the copy above's
    # control number as an SSN and the "22222" form identifier as an EIN.
    if not set(label.expected_types) & {PiiType.ADDRESS, PiiType.PERSON, PiiType.ORG_PRIVATE,
                                         PiiType.STREET, PiiType.CITY_STATE}:
        return []
    line = label.line
    if label.start > 2:
        return []  # a caption that does not start its line is a row caption, not a box caption
    x0 = label.rect[0] if label.rect else line.bbox[0]
    cap = NAME_LIKE_MAX_LINES.get(label.expected_types[0] if len(label.expected_types) == 1 else None,
                                  MAX_STACKED_LINES)
    above = sorted(
        (o for o in lines_by_page.get(line.page_no, [])
         if o is not line and o.bbox[3] <= line.bbox[1] + 0.5 and abs(o.bbox[0] - x0) <= LEFT_EDGE_TOLERANCE
         and o.text.strip()),
        key=lambda o: -o.bbox[3],
    )
    label_rows = sorted(lb_line.bbox[3] for lb_line in lines_by_page.get(line.page_no, [])
                        if lb_line.key() in label_keys and lb_line is not line)
    found: list[Line] = []
    for o in above:
        if len(found) >= cap:
            break
        if line.bbox[1] - o.bbox[3] > ABOVE_CAPTION_MAX_DISTANCE:
            break  # beyond one box: another field's value
        if any(o.bbox[3] < y <= line.bbox[1] for y in label_rows):
            break  # another caption sits between them: a different box
        if (o.key() in label_keys or o.key() in claimed or _symbol_font(o)
                or (styles and in_form_style(o, styles)) or _OWN_LABEL.match(o.text)
                or _BOX_CAPTION.match(o.text) or _same_template(o, line)
                or _is_pure_financial(o.text) or MONEY_RE.search(o.text)):
            break
        if found and found[-1].bbox[1] - o.bbox[3] > LINE_GAP_FACTOR * max(o.height, 1.0):
            break  # values of one field sit together
        found.append(o)
    return list(reversed(found))


#: Furthest gap between two cells of one table row (Form 1040 dependents: 9 pt
#: between SSN boxes, 92 pt between short names in 108 pt columns).
ROW_CELL_MAX_GAP = 120.0


def _row_continuation(label, value_lines, inline_span, lines_by_page, label_keys, claimed, styles):
    """The further cells of a row whose caption bound its FIRST cell (Case C).

    A cell continues the row when it is on the same row, to the right, set in the
    SAME style as the first cell (a filled value, not the form), is not itself a
    label or another field's value, and follows the previous cell closely. A label
    or form-styled text between cells ends the row.
    """
    from .layout_style import in_form_style, line_style

    if inline_span is not None or len(value_lines) != 1:
        return []
    first = value_lines[0]
    line = label.line
    mid = (line.bbox[1] + line.bbox[3]) / 2
    if not (first.bbox[1] <= mid <= first.bbox[3]) or first.bbox[0] < (label.rect or line.bbox)[2]:
        return []  # stacked underneath, not a row
    style = line_style(first)
    row = sorted(
        (o for o in lines_by_page.get(line.page_no, [])
         if o is not first and o.bbox[0] > first.bbox[0] and o.bbox[1] <= mid <= o.bbox[3]
         and o.text.strip()),
        key=lambda o: o.bbox[0],
    )
    cells, prev = [], first
    for o in row:
        if (o.bbox[0] - prev.bbox[2] > ROW_CELL_MAX_GAP or o.key() in label_keys
                or o.key() in claimed or line_style(o) != style
                or (styles and in_form_style(o, styles))):
            break
        cells.append(o)
        prev = o
    return cells


def _eligible(label, value_text: str) -> bool:
    """Should this value line be redacted because of its label?

    A label in the taxonomy vouches for its values outright. An unrecognised
    label only vouches for values that look identity-bearing, so that
    "Filing status: Married filing jointly" survives intact.
    """
    if is_placeholder_value(value_text):
        return False
    if not label.unknown:
        return True
    return looks_sensitive(value_text)


#: Values a form puts in an identity field that identify nobody. "Self-Prepared"
#: under "Preparer's name" was typed PERSON and pseudonymised into a fake name,
#: which changes what the return says (it was NOT paid-prepared).
PLACEHOLDER_VALUES = {
    "self-prepared", "self prepared", "self", "n/a", "na", "n.a.", "none", "same", "same as above",
    "see attached", "see statement", "see stmt", "various", "not applicable", "unknown",
    "applied for", "-", "--", "—", "x", "xx", "xxx", "on file", "declined", "withheld",
}


def is_placeholder_value(text: str) -> bool:
    return " ".join(text.lower().split()).strip(" .:;") in PLACEHOLDER_VALUES


# -- value line discovery ---------------------------------------------------


#: A box caption on an IRS form: a box number, then a wide gap ("15  State",
#: "1   Wages, tips"). Never a value - "f Employee's address and ZIP code" bound
#: the next box's "15  State" as its value. Two spaces, so "15 Main St" is not one.
_BOX_CAPTION = re.compile(r"^\s*\d{1,2}[a-z]?\s{2,}\S")


def _same_template(a: Line, b: Line) -> bool:
    """Same font FAMILY and colour: the form talking, not a filled value. W-2:
    "For Official Use Only" (template bold) beside the SSN caption (template
    roman, same red) was bound as the SSN's value."""
    sa, sb = (a.spans[0] if a.spans else None), (b.spans[0] if b.spans else None)
    if sa is None or sb is None:
        return False
    return sa.font.split("-")[0] == sb.font.split("-")[0] and sa.color == sb.color


#: A line that opens with its own caption and a colon starts a new field.
_OWN_LABEL = re.compile(r"^[\s\-\u2010-\u2015\u2022*]*[A-Za-z][A-Za-z '&/().#\-]{1,40}:(\s|$)")


def _preliminary_groups(labels, lines_by_page, label_keys) -> list[LogicalFieldGroup]:
    """Conservative label->value pairs, used only to learn the form's typeface."""
    out = []
    claimed: set = set()
    for idx, label in enumerate(labels):
        if label.non_pii or (not label.expected_types and not label.unknown):
            continue
        value_lines, _inline = _value_lines_for(label, lines_by_page, label_keys, claimed, strict=True)
        if value_lines:
            out.append(LogicalFieldGroup(group_id=f"p{idx}", label=label, value_lines=value_lines,
                                         expected_types=list(label.expected_types)))
    return out


def _span_style_at(line: Line, offset: int):
    pos = 0
    for span in getattr(line, "spans", []) or []:
        end = pos + len(span.text)
        if pos <= offset < end:
            return (span.font, span.color)
        pos = end
    return None


def _symbol_font(line) -> bool:
    """Braces, arrows and checkboxes drawn from a symbol/dingbat font ("AdobePiStd",
    "ZapfDingbats", "Symbol") are page furniture: a 40pt brace beside "Fill in
    Your Address" was bound as that heading's value."""
    fonts = " ".join(getattr(span, "font", "") or "" for span in getattr(line, "spans", []) or []).lower()
    return any(k in fonts for k in ("pistd", "dingbat", "symbol", "wingding", "zapf"))


def _is_known_label(text: str, rs, match) -> bool:
    """A RECOGNISED label (typed or non-PII). A merely label-SHAPED short phrase
    ("Bela Quell") is not one - that is what a value looks like too."""
    m = match(text, rs)
    return bool(m) and (bool(m[0]) or bool(m[1]))


def _value_lines_for(
    label: LabelRegion,
    lines_by_page: dict[int, list[Line]],
    label_keys: set[tuple[int, int, int]],
    claimed: set[tuple[int, int, int]],
    limit: Optional[int] = None,
    form_styles: Optional[set] = None,
    strict: bool = False,
) -> tuple[list[Line], Optional[tuple[int, int]]]:
    from .layout_style import in_form_style, line_style

    form_styles = form_styles or set()
    line = label.line
    inline: Optional[tuple[int, int]] = None

    # Case A: value sits after the label on the same line ("Name: John Smith").
    remainder = line.text[label.end:]
    if remainder.strip():
        start = label.end + (len(remainder) - len(remainder.lstrip()))
        end = len(line.text.rstrip())
        if limit is not None:
            end = min(end, limit)
            while end > start and line.text[end - 1].isspace():
                end -= 1
        if end > start:
            inline = (start, end)
            # A remainder set in the form's own typeface is more form text
            # ("Social security benefits . . 6a  b Taxable amount"), not a value.
            style = _span_style_at(line, start)
            if form_styles and style is not None and (
                style in form_styles
                or (style[0].split("-")[0], style[1]) in {(f.split("-")[0], c) for f, c in form_styles}
            ):
                inline = None

    # Case C: the value is a SEPARATE line object to the right on the same row -
    # "Spouse's name    Bela Quell", "Filing status    MFJ". Extraction often
    # splits a row into two Line objects; neither Case A nor the stacking below
    # saw the value, so it was left to model guesses (a wrong type, or none).
    stacked: list[Line] = []
    if inline is None and not remainder.strip():
        label_x1 = label.rect[2] if label.rect else line.bbox[2]
        mid = (line.bbox[1] + line.bbox[3]) / 2
        row = [
            other for other in lines_by_page.get(line.page_no, [])
            if other is not line and other.bbox[0] >= label_x1 - 0.5
            and other.bbox[1] <= mid <= other.bbox[3]
            and other.text.strip()
        ]
        row.sort(key=lambda ln: ln.bbox[0])
        if row:
            nearest = row[0]
            from .deterministic import load_rules
            from .labels import _match_label

            # A filled value is set in a different style from its label; two
            # template-text cells side by side are a header row ("(2) Social
            # security number | (3) Relationship to you"), not a field and its value.
            # Horizontal text of about the caption's height only: a watermark set
            # vertically down the page edge ("For Informational Purposes" on the
            # W-2, 208 pt tall) sits "on" every row it crosses.
            tall = nearest.height > 2.5 * max(line.height, 1.0)
            if (not tall and not _same_template(nearest, line) and not _BOX_CAPTION.match(nearest.text)
                    and nearest.bbox[0] - label_x1 <= 80.0
                    and nearest.key() not in label_keys
                    and nearest.key() not in claimed
                    and line_style(nearest) != line_style(line)
                    and sum(ch.isalnum() for ch in nearest.text) >= 2
                    and not _symbol_font(nearest)
                    and not _is_known_label(nearest.text, load_rules(), _match_label)
                    and not (form_styles and in_form_style(nearest, form_styles))):
                stacked = [nearest]
                return stacked, inline

    # Case B: value(s) stacked underneath the label.
    if inline is None:
        siblings = lines_by_page.get(line.page_no, [])
        below = [
            other
            for other in siblings
            if other.bbox[1] > line.bbox[1] + 0.4 * max(line.height, 1.0)
        ]
        below.sort(key=lambda ln: (ln.bbox[1], ln.bbox[0]))
        # The label's own horizontal extent, not its line's: a label that is
        # the second field on its row starts mid-line.
        label_x0 = label.rect[0] if label.rect else line.bbox[0]
        label_x1 = label.rect[2] if label.rect else line.bbox[2]
        anchor_left = label_x0
        prev_bottom = line.bbox[3]
        line_cap = NAME_LIKE_MAX_LINES.get(
            label.expected_types[0] if len(label.expected_types) == 1 else None,
            MAX_STACKED_LINES,
        )
        for candidate_line in below:
            if len(stacked) >= line_cap:
                break
            # Only lines in THIS label's column are candidates. Lines were walked
            # in page reading order, so on a row of side-by-side labels ("First
            # name | Last name | SSN") the first line below the SECOND label was
            # the FIRST label's value, at the far left; it failed the left-edge
            # test and ended the search. Every field but the leftmost on such a
            # row was never bound - measured on real returns, every last-name,
            # SSN, state and ZIP box. Another column's line is skipped, not fatal.
            cx0, cx1 = candidate_line.bbox[0], candidate_line.bbox[2]
            overlaps_label = min(cx1, label_x1) - max(cx0, label_x0) > 1.0
            boxed = not stacked and _centred_in_box(candidate_line, line, label_x0, label_x1, siblings)
            if not overlaps_label and abs(cx0 - anchor_left) > LEFT_EDGE_TOLERANCE and not boxed:
                if strict:
                    break
                continue
            if form_styles and in_form_style(candidate_line, form_styles):
                break  # the form's own text: this field has ended
            if _BOX_CAPTION.match(candidate_line.text):
                break  # the next box's caption, not this field's value
            if _OWN_LABEL.match(candidate_line.text):
                # "- Date of Withdrawal:  04/11/2022" under "- Routing Transit
                # Number:" is the NEXT field, not this one's value. Bound as a
                # value it was typed ROUTING_NUMBER and shape-masked into
                # scrambled text on a real return.
                break
            if candidate_line.key() in label_keys or candidate_line.key() in claimed:
                break
            gap = candidate_line.bbox[1] - prev_bottom
            if gap > LINE_GAP_FACTOR * max(candidate_line.height, line.height, 1.0):
                break
            if abs(candidate_line.bbox[0] - anchor_left) > LEFT_EDGE_TOLERANCE and not (
                # A value CENTERED or right-aligned in its box under the caption
                # (Form 1040 "ZIP code" -> "94539" starts 21 pt to the right): it
                # sits under the caption and starts inside its column - not a line
                # reaching in from the left. Only the first value line qualifies.
                not stacked and overlaps_label and cx0 >= anchor_left - 0.5
                and cx0 <= label_x1 + LEFT_EDGE_TOLERANCE
            ) and not boxed:
                break
            if _is_pure_financial(candidate_line.text):
                break
            if MONEY_RE.search(candidate_line.text) or PERCENT_RE.search(candidate_line.text):
                # A financial fact starts a new region; never absorb it into a
                # PII value group (spec sections 12 and 85).
                break
            if _shape_mismatch(candidate_line.text, label.expected_types):
                # A "Taxpayer name" label absorbing the street address and
                # city/state/zip lines below it, and typing all three PERSON,
                # is what hid an undetected home address as a MISTYPED one.
                # When the label names exactly one type and the line clearly
                # looks like a different kind of value, the field ends here.
                break
            stacked.append(candidate_line)
            prev_bottom = candidate_line.bbox[3]
            anchor_left = candidate_line.bbox[0] if len(stacked) == 1 else anchor_left

    return stacked, inline


#: How far right of a short caption a box's value may be centred (W-2 box d:
#: "Control number" ends at x=99, its value starts at x=163 in a 270 pt wide box).
BOX_CENTRE_MAX_OFFSET = 80.0


def _centred_in_box(value: Line, caption_line: Line, label_x0: float, label_x1: float,
                    siblings: list[Line]) -> bool:
    """A filled value centred in a WIDE box whose caption is short and sits at the
    box's left edge: right of the caption, alone on its row inside the column, set
    in another style than the caption, and with no other caption above it on the
    caption's row (that would be the value's own caption)."""
    from .layout_style import line_style

    vx0, vy0, vx1, vy1 = value.bbox
    if not (label_x1 - 0.5 <= vx0 <= label_x1 + BOX_CENTRE_MAX_OFFSET):
        return False
    if line_style(value) == line_style(caption_line) or _same_template(value, caption_line):
        return False
    if sum(ch.isalnum() for ch in value.text) < 2 or _symbol_font(value):
        return False
    cy = (caption_line.bbox[1] + caption_line.bbox[3]) / 2
    vy = (vy0 + vy1) / 2
    for other in siblings:
        if other is value or other is caption_line or not other.text.strip():
            continue
        ox0, oy0, ox1, oy1 = other.bbox
        # another caption on the caption's row, above the value: the value is its
        if oy0 <= cy <= oy1 and label_x1 < ox0 <= vx1:
            return False
        # something else on the value's row between the caption's left edge and it
        if oy0 <= vy <= oy1 and label_x0 - 0.5 <= ox1 <= vx0 + 0.5:
            return False
    return True


_ADDRESS_SHAPE = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|"
    r"MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|"
    r"WA|WV|WI|WY|DC)\.?\s*\d{5}?\b",
    re.I,
)
_STREET_SHAPE = re.compile(
    r"^\d{1,6}\s+\S+.*\b(?:st|street|ave|avenue|rd|road|blvd|dr|drive|ln|lane|"
    r"ct|court|way|cir|circle|pl|place|ter|terrace|pkwy|hwy|cmn|common|apt|ste|unit)\b",
    re.I,
)


def _shape_mismatch(text: str, expected: list) -> bool:
    """Does this line look like a different KIND of value than the label expects?

    Only fires for single-type labels naming PERSON - the case that actually
    produced a mistyped address. A multi-type label ("name, address, and zip
    code") is supposed to absorb several kinds of line.
    """
    if len(expected) != 1 or expected[0] is not PiiType.PERSON:
        return False
    stripped = text.strip()
    return bool(_ADDRESS_SHAPE.search(stripped) or _STREET_SHAPE.match(stripped))


#: Types defined by their digits. A line with no digit cannot be one: "Enter code
#: from instructions" under an SSN label, "Checking  Savings" under "Type of
#: account" - measured on real returns, both were typed and redacted.
_DIGIT_TYPES = {
    "SSN", "EIN", "ITIN", "TIN", "STATE_TAX_ID", "BANK_ACCOUNT", "ROUTING_NUMBER", "PHONE", "FAX",
    "POSTAL_CODE", "DOB", "PERSONAL_DATE", "CARD_NUMBER", "MEDICARE_ID", "IP_PIN", "PTIN",
}


def _shape_fits_label(text: str, expected: list) -> bool:
    if expected and all(t.value in _DIGIT_TYPES for t in expected):
        return any(ch.isdigit() for ch in text)
    return True


def _is_pure_financial(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if MONEY_RE.fullmatch(stripped) or PERCENT_RE.fullmatch(stripped):
        return True
    return bool(ONLY_MONEY_RE.match(stripped)) and ("$" in stripped or "%" in stripped)


def _trimmed_span(line: Line) -> tuple[Optional[int], int]:
    text = line.text
    start = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    if end <= start:
        return None, 0
    # Never pull a currency amount into a group-level redaction (section 85).
    for m in list(MONEY_RE.finditer(text)) + list(PERCENT_RE.finditer(text)):
        if m.start() <= start < m.end():
            start = m.end()
        if m.start() < end <= m.end():
            end = m.start()
    return (start, end) if end > start else (None, 0)


def _cover_span(
    line: Line,
    start: int,
    end: int,
    group: LogicalFieldGroup,
    candidates: list[Candidate],
) -> list[Candidate]:
    """Attach existing detections to the group; synthesise one if uncovered."""
    existing = [
        c
        for c in candidates
        if c.line.key() == line.key() and c.start < end and start < c.end
    ]
    for c in existing:
        c.group_id = c.group_id or group.group_id
        group.detected_types.add(c.pii_type)

    covered = sum(
        len(line.text[max(c.start, start):min(c.end, end)].strip()) for c in existing
    )
    total = len(line.text[start:end].strip())
    # Near-total coverage only. A threshold of "most of the line" lets cases like
    # "Apartment 4B" through with " 4B" surviving in the output - exactly the
    # partial-redaction failure spec section 39 calls critical.
    if total and covered / total >= FULL_COVERAGE_RATIO:
        return []

    rect = line.rect_for(start, end)
    if rect is None:
        return []
    if not _shape_fits_label(line.text[start:end], group.expected_types):
        return []
    # The label already says what this field holds. Using it beats reporting
    # UNLABELLED, which is what made the review list unreadable.
    inferred = (
        group.expected_types[0]
        if len(group.expected_types) == 1
        else PiiType.UNCLASSIFIED_GROUP_VALUE
    )
    return [
        Candidate(
            pii_type=inferred,
            text=line.text[start:end],
            page_no=line.page_no,
            rect=rect,
            line=line,
            start=start,
            end=end,
            confidence=0.7,
            source=Source.GROUP,
            evidence=[
                Evidence(
                    Source.GROUP,
                    f"value line of labelled group '{group.label.text}'",
                    0.7,
                )
            ],
            group_id=group.group_id,
            needs_review=inferred is PiiType.UNCLASSIFIED_GROUP_VALUE,
            review_reason=(
                ""
                if inferred is not PiiType.UNCLASSIFIED_GROUP_VALUE
                else "value line belongs to a sensitive labelled field but no "
                "detector classified it - review before accepting"
            ),
        )
    ]


# -- coverage analysis (sections 18-19) -------------------------------------


_TAX_ID_ALTERNATIVES = {PiiType.SSN, PiiType.EIN, PiiType.TIN, PiiType.ITIN}


def analyse_coverage(groups: list[LogicalFieldGroup], candidates: list[Candidate]) -> None:
    by_group: dict[str, list[Candidate]] = {}
    for c in candidates:
        if c.group_id:
            by_group.setdefault(c.group_id, []).append(c)

    for group in groups:
        members = by_group.get(group.group_id, [])
        group.detected_types = {c.pii_type for c in members}
        missing = []
        for expected in group.expected_types:
            accepted = SATISFIED_BY.get(expected, {expected})
            if not (group.detected_types & accepted):
                missing.append(expected)

        # "Identifying number" holds an SSN OR an EIN (0.44.0): alternatives, not
        # a list of values that must all be present.
        alternatives = set(group.expected_types)
        if (missing and len(alternatives) > 1 and alternatives <= _TAX_ID_ALTERNATIVES
                and group.detected_types & alternatives):
            missing = []

        unclassified = [c for c in members if c.pii_type is PiiType.UNCLASSIFIED_GROUP_VALUE]
        if missing and not unclassified:
            group.complete = False
            group.reason = (
                "PARTIAL / INCOMPLETE DETECTION: expected "
                + ", ".join(t.value for t in missing)
                + " in this field but did not find it"
            )
            for c in members:
                c.needs_review = True
                c.review_reason = c.review_reason or group.reason
        elif missing and unclassified:
            group.complete = False
            group.reason = (
                "PARTIAL: unclassified value lines cover the missing "
                + ", ".join(t.value for t in missing)
            )
        else:
            group.complete = True
