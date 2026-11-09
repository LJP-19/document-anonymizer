"""Form text versus filled-in values, told apart by how they are TYPESET.

A filled form is two documents printed on top of each other: the blank form
(labels, headings, captions, instructions) and the values someone typed into
it. Software that fills a form almost never sets the values in the form's own
typeface and colour - tax software prints them in Courier, a scanner-era
template is a different font, a PDF form field has its own appearance.

That difference is the most reliable "this is the form talking" signal there
is, and it needs no vocabulary: "Presidential Election Campaign", a sidebar
heading, a checkbox caption - each one is name-SHAPED, and the shape heuristic
and the language models both proposed them as people. Measured on real,
software-prepared returns, form headings and captions replaced by fake names
were the single largest class of false positive.

The rule is deliberately narrow:

  * it only acts when the document SHOWS the split - the style its recognised
    labels are set in is (almost) never the style its label-bound values are
    set in. A plain letter, a scanned-then-OCRed page or any document where
    labels and values share a style gets no veto at all;
  * it only removes PROPOSALS (shape heuristic, spaCy/GLiNER/Presidio) of
    name/organisation/place types. A rule match (SSN, phone, e-mail ...), a
    label-bound value, a joint-name split, a propagated known entity and a
    manual addition are never touched by it.
"""

from __future__ import annotations

from collections import Counter
from typing import Optional

from ..document.model import Document, Line
from .types import Candidate, PiiType, Source

#: Types a model or the shape heuristic proposes from wording alone.
WORDING_TYPES = {
    PiiType.PERSON, PiiType.ORG_PRIVATE, PiiType.UNCLASSIFIED_GROUP_VALUE,
}
for _name in ("LOCATION", "CITY_STATE", "BIRTHPLACE", "OCCUPATION", "NATIONALITY", "CITIZENSHIP"):
    if hasattr(PiiType, _name):
        WORDING_TYPES.add(getattr(PiiType, _name))

#: Minimum evidence before a style is called the form's own.
MIN_LABEL_LINES = 3
#: A form style may carry at most this share of the label-bound value lines.
MAX_VALUE_SHARE = 0.10


def line_style(line: Line) -> Optional[tuple[str, int]]:
    """(font, colour) of the span carrying most of the line's characters."""
    if not getattr(line, "spans", None):
        return None
    weights: Counter = Counter()
    for span in line.spans:
        text = getattr(span, "text", "") or ""
        weights[(span.font, span.color)] += len(text.strip()) or 1
    return weights.most_common(1)[0][0] if weights else None


def form_styles(labels: list, groups: list) -> set[tuple[str, int]]:
    """Styles that belong to the blank form rather than to the filled values.

    Evidence, per (font, colour):
      * whole-line labels set in it (a label that is its entire line), and
      * how often a line in it sits as a VALUE under a label of ANOTHER style.

    A style is the form's when it carries at least 5 whole-line labels and is
    almost never (<= 3%) a value under another style. The rule only applies
    when the document has a real value style - one that appears as a value
    under other styles at least 3 times and is rarely a label itself.

    Measured on a real 108-page software-prepared return: the government
    template fonts carry hundreds of whole-line labels and are a value under
    another style 0-2% of the time; the software's Courier values about 30%;
    its own Helvetica prose about 3.4%. Two earlier criteria failed there -
    "any label style whose labels never bind a same-style line" (dense forms
    stack form lines under labels constantly) and "label count vs total value
    count" (made the VALUE font a form style, because the software also prints
    colon-labels in it). This one is pinned by tests on both shapes.
    """
    pure: Counter = Counter()
    for label in labels:
        line = label.line
        if label.start == 0 and label.end >= len(line.text.rstrip()) - 1:
            style = line_style(line)
            if style is not None:
                pure[style] += 1
    as_value: Counter = Counter()
    for group in groups:
        label_style = line_style(group.label.line)
        for line in group.value_lines:
            style = line_style(line)
            if style is not None and style != label_style:
                as_value[style] += 1

    # The value FONT is judged across colours: a previous tool (or a reviewer's
    # markup) may have recoloured some values, and a font that is plainly the
    # value font in one colour is never the form's font in another.
    font_value: Counter = Counter()
    font_pure: Counter = Counter()
    for (font, _colour), n in as_value.items():
        font_value[font] += n
    for (font, _colour), n in pure.items():
        font_pure[font] += n
    value_fonts = {f for f, n in font_value.items() if n >= MIN_LABEL_LINES and font_pure.get(f, 0) <= n}
    if not value_fonts:
        return set()
    return {
        st for st, n in pure.items()
        if n >= 5 and st[0] not in value_fonts and as_value.get(st, 0) <= max(1, 0.03 * n)
    }


def _family(font: str) -> str:
    """"HelveticaLTStd-Obl" -> "HelveticaLTStd": weight/slant variants of one face."""
    return (font or "").split("-")[0].split(",")[0]


def in_form_style(line, styles: set) -> bool:
    """The line is set in a form style, or in another weight/slant of the same
    typeface in the same colour (the template's oblique or condensed cut,
    which carries too few whole-line labels to be learned on its own)."""
    style = line_style(line)
    if style is None or not styles:
        return False
    if style in styles:
        return True
    families = {(_family(f), c) for f, c in styles}
    return (_family(style[0]), style[1]) in families


def veto_form_styled_proposals(
    candidates: list[Candidate], styles: set
) -> tuple[list[Candidate], int]:
    """Drop name/org/place PROPOSALS set in the form's own typeface.

    `styles` comes from form_styles(), computed once per document and kept on
    the DetectionResult so the second pass applies exactly the same rule.
    """
    if not styles:
        return candidates, 0
    kept: list[Candidate] = []
    dropped = 0
    for c in candidates:
        proposal = c.group_id is None and (
            c.source in (Source.NER, Source.COVERAGE)
            # the standalone joint-name pattern ("X and Y Z") is a proposal too
            or (c.source is Source.GROUP and any("joint-name" in e.detail for e in c.evidence))
        )
        # A MODEL's number set in the form's own typeface is the form's number
        # (line "16", "Form(s) 8814 / 4972"): GLiNER called 4972 a zip code. A
        # filled value is printed in the value font, and rules are never vetoed here.
        model_number = c.source is Source.NER and any(ch.isdigit() for ch in c.text)
        if proposal and (c.pii_type in WORDING_TYPES or model_number) and in_form_style(c.line, styles):
            dropped += 1
            continue
        kept.append(c)
    return kept, dropped


def form_vocabulary(doc, styles: set, min_count: int = 3) -> set[str]:
    """Words THIS document prints in its form typeface at least `min_count` times.

    A document-adaptive stop list: "SEP" in "traditional, SEP, or SIMPLE IRA",
    "BAA", a form's own abbreviations. A model tagged such a token PERSON on one
    page; it was redacted there and - correctly - flagged by verification as
    still readable on 60 others. A filer's name is never part of the printed
    form, so it can never be in this set.
    """
    if not styles:
        return set()
    counts: Counter = Counter()
    for page in doc.pages:
        for line in page.lines:
            for span in getattr(line, "spans", []) or []:
                if (span.font, span.color) in styles:
                    for word in span.text.split():
                        word = word.strip(".,;:()[]'\"\u2019").lower()
                        if word:
                            counts[word] += 1
    return {w for w, n in counts.items() if n >= min_count}
