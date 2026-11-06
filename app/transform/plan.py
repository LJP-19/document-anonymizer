"""The single transformation plan (spec section 33).

Preview and export both consume this object. There is deliberately no second
code path: if the preview shows it, the exported file contains it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..decisions.manager import DecisionManager, DecisionState
from ..detection.types import Candidate, DetectionResult, PiiType, Rect, Source
from ..entities.registry import EntityRegistry

RED = (1.0, 0.0, 0.0)


@dataclass
class Target:
    candidate_id: str
    page_no: int
    rect: Rect
    original: str
    replacement: str
    font_size: float
    font_name: str
    color: tuple[float, float, float] = RED
    #: Original weight/slant, preserved for visual fidelity. Never used to
    #: change color - red stays a deliberate, verified security signal.
    font_flags: int = 0
    origin: Optional[tuple[float, float]] = None
    pii_type: PiiType = PiiType.UNCLASSIFIED_GROUP_VALUE
    group_id: Optional[str] = None
    state: DecisionState = DecisionState.ACCEPTED
    blackout: bool = False
    #: The value's own glyph boxes. The redactor removes one small rectangle at
    #: the centre of each, so a neighbour whose box overlaps the value's is not
    #: taken with it (PyMuPDF removes every character a rectangle touches).
    char_rects: list = field(default_factory=list)
    #: Who authorized this target (decisions.authorization.AuthOrigin value).
    #: Set ONLY by build_plan after the authorization check; the redactor refuses
    #: a target without it.
    authorized_by: str = ""


@dataclass
class TransformationPlan:
    source_path: str
    targets: list[Target] = field(default_factory=list)
    skipped_values: list[str] = field(default_factory=list)
    group_membership: dict[str, list[str]] = field(default_factory=dict)
    group_value_texts: dict[str, list[str]] = field(default_factory=dict)
    ocr_required_pages: list[int] = field(default_factory=list)
    replacement_map: dict[str, str] = field(default_factory=dict)
    hidden_items: list = field(default_factory=list)
    #: Values found but never typed. Left in the document, listed for a decision.
    unresolved: list = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: normalized value -> how many occurrences of it are EXPECTED to remain
    #: in the output. Not always zero: a value can legitimately occur more
    #: times in the source than were ever accepted as candidates - "LJPS
    #: Manufacturing Inc." is correctly never a candidate at all when "LJPS"
    #: is a known person, so that occurrence was never going to be redacted
    #: and its survival is not a leak. Computed once, from the ORIGINAL
    #: document, at plan-build time - the verifier compares against this
    #: instead of assuming every accepted value must vanish completely.
    expected_residual_counts: dict = field(default_factory=dict)
    #: One sanitized record per candidate: page, rectangle, type, source, decision,
    #: authorization origin and reason, and what happened to it. No text.
    authorization_log: list = field(default_factory=list)
    #: Candidates marked ACCEPTED that the authorization policy refused here.
    refused: list = field(default_factory=list)

    @property
    def replacements(self) -> list[str]:
        return [t.replacement for t in self.targets]

    @property
    def originals(self) -> list[str]:
        return [t.original for t in self.targets]

    def targets_for_page(self, page_no: int) -> list[Target]:
        return [t for t in self.targets if t.page_no == page_no]


def _char_rects(candidate) -> list:
    line = candidate.line
    offsets = getattr(line, "offsets", None) or []
    out = []
    for i in range(candidate.start, min(candidate.end, len(offsets))):
        ch = offsets[i]
        if ch is not None and not line.text[i].isspace():
            out.append(tuple(ch.bbox))
    return out


def _style(candidate):
    """(size, font, flags, baseline origin) of the span the value sits in."""
    line = candidate.line
    span = line.span_at(candidate.start, candidate.end) if hasattr(line, "span_at") else None
    if span is None:
        return line.size, line.font, line.flags, line.origin
    origin = span.origin
    if origin is not None:
        origin = (candidate.rect[0], origin[1])   # the value's own x, the span's baseline
    return span.size, span.font, span.flags, origin


def _seed_name_roles(names, detection) -> None:
    """Teach the name registry which tokens are given names and which are
    surnames BEFORE any replacement is generated.

    Order: (1) field labels ("first name" / "last name"), the strongest
    evidence; (2) full names, longest first, which fix both roles at once. A
    token's role, once fixed, is used wherever it appears alone - so "Ann" alone
    becomes the same synthetic first name as in "Ann Tash", never a surname.
    """
    import re

    given_re = re.compile(r"\b(first|given|forename)\b", re.I)
    surname_re = re.compile(r"\b(last|sur|family)\s*name\b|\bsurname\b", re.I)
    by_group = {g.group_id: g for g in getattr(detection, "groups", []) or []}
    persons = [c for c in detection.candidates if c.pii_type is PiiType.PERSON]
    labelled = []
    for c in persons:
        group = by_group.get(c.group_id) if c.group_id else None
        if group is None:
            continue
        tokens = [t for t in c.normalized.split() if t.strip(".,").isalpha() and len(t.strip(".,")) > 1]
        if tokens:
            labelled.append((group.label.text, tokens))
    # Surname labels first: a "first name" column on a dependents table often
    # binds the whole "First M Last" line, and its trailing word is a surname.
    # Seeding that line's words as given names first turned a surname into a
    # first-name pseudonym everywhere it appeared alone. Under a given-name
    # label only the FIRST word is taken as a given name.
    for label, tokens in labelled:
        if surname_re.search(label) and not given_re.search(label) and len(tokens) == 1:
            names.seed_role(tokens[0], surname=True)
    for label, tokens in labelled:
        if given_re.search(label) and not surname_re.search(label):
            names.seed_role(tokens[0], surname=False)
    multi = sorted({c.normalized for c in persons if len(c.normalized.split()) >= 2},
                   key=lambda v: (-len(v.split()), v))
    for value in multi:
        names.seed_full_name(value)


def build_plan(
    source_path: str,
    detection: DetectionResult,
    decisions: DecisionManager,
    registry: EntityRegistry,
    ocr_required_pages: Optional[list[int]] = None,
    document=None,
) -> TransformationPlan:
    plan = TransformationPlan(
        source_path=source_path,
        ocr_required_pages=list(ocr_required_pages or []),
        warnings=list(detection.warnings),
    )

    skipped_lines: set[tuple[int, int, int]] = set()

    # Seed the name registry with every real name first. Generating "John" as a
    # replacement while a real John appears on page 9 is worse than not
    # replacing at all.
    people = [
        c.normalized for c in detection.candidates if c.pii_type is PiiType.PERSON
    ]
    import re as _re

    registry.document_words = {
        w.lower() for c in detection.candidates for w in _re.findall(r"[^\W\d_]{4,}", c.normalized)
    }
    if people or registry.document_words:
        from ..pseudonymization.names import NameRegistry

        if registry.names is None:
            registry.names = NameRegistry(scope=registry.scope)
        for name in people:
            registry.names.note_original(name)
        # Any detected value's words, not only people's: a generated surname
        # must not equal a real city or employer redacted elsewhere either.
        for word in registry.document_words:
            registry.names.note_original(word)
        _seed_name_roles(registry.names, detection)

    unresolved: list = []

    from ..decisions.authorization import assess, confirmed_name_words

    confirmed_words = confirmed_name_words(detection.candidates)

    def _log(candidate, state, origin, reason, outcome):
        plan.authorization_log.append({
            "candidate_id": candidate.id, "page": candidate.page_no + 1,
            "rect": [round(v, 1) for v in candidate.rect], "pii_type": candidate.pii_type.value,
            "source": candidate.source.value, "label_bound": bool(getattr(candidate, "label_confirmed", False)),
            "needs_review": bool(candidate.needs_review), "adjudication": getattr(candidate, "adjudication", ""),
            "decision": state.value, "authorization": origin, "reason": reason, "outcome": outcome,
        })

    for candidate in detection.candidates:
        state = decisions.state(candidate)
        decision = decisions.decisions.get(candidate.id)
        origin = decision.origin if decision is not None else "REVIEW_REQUIRED"
        if state not in (
            DecisionState.ACCEPTED,
            DecisionState.EDITED,
            DecisionState.MANUALLY_ADDED,
        ):
            if state is DecisionState.SKIPPED:
                plan.skipped_values.append(candidate.normalized)
                skipped_lines.add(candidate.line.key())
            _log(candidate, state, origin, decision.reason if decision else "", "left readable")
            continue

        # THE AUTHORIZATION BOUNDARY. A person's decision stands. An automatic
        # approval is re-checked against the one policy here, so a candidate any
        # earlier component wrongly marked ACCEPTED is refused, not transformed.
        if origin in ("USER_APPROVED", "USER_EDITED", "USER_ADDED"):
            authorized_by, why = origin, "decided by the user"
        elif origin == "AUTO_APPROVED" and state is DecisionState.ACCEPTED:
            verdict = assess(candidate, confirmed_words)
            if not verdict.eligible:
                plan.refused.append(candidate.id)
                _log(candidate, state, "REJECTED", verdict.reason, "refused at the transformation boundary")
                skipped_lines.add(candidate.line.key())
                plan.skipped_values.append(candidate.normalized)
                continue
            authorized_by, why = origin, verdict.reason
        else:
            plan.refused.append(candidate.id)
            _log(candidate, state, "REJECTED", f"no authorization ({origin})", "refused at the transformation boundary")
            skipped_lines.add(candidate.line.key())
            plan.skipped_values.append(candidate.normalized)
            continue

        # No type means no replacement. Generating one for a span nothing could
        # identify is what wrote scrambled words over form instructions. Such a
        # span is reported instead, unless the user has said what to do with it.
        if (
            candidate.pii_type is PiiType.UNCLASSIFIED_GROUP_VALUE
            and candidate.source is not Source.MANUAL   # the user named it
            and not getattr(candidate, "blackout", False)
            and decisions.override(candidate) is None
        ):
            unresolved.append(candidate.normalized)
            # Verification must not expect this to disappear: it was found but
            # deliberately left readable because nothing could type it.
            skipped_lines.add(candidate.line.key())
            plan.skipped_values.append(candidate.normalized)
            _log(candidate, state, origin, "nothing could say what kind of value this is", "left readable")
            continue

        override = decisions.override(candidate)
        if getattr(candidate, "blackout", False):
            replacement = ""
        elif override is not None:
            replacement = override
            registry.override(candidate.pii_type, candidate.normalized, override, _disc(candidate))
        else:
            # Joint names need no special case: both halves share the surname
            # TOKEN, so the token registry gives them the same new surname.
            replacement = registry.pseudonym_for(
                candidate.pii_type, candidate.normalized, _disc(candidate)
            )

        target = Target(
            blackout=getattr(candidate, "blackout", False),
            candidate_id=candidate.id,
            page_no=candidate.page_no,
            rect=candidate.rect,
            original=candidate.text,
            replacement=replacement,
            font_size=_style(candidate)[0],
            font_name=_style(candidate)[1],
            font_flags=_style(candidate)[2],
            origin=_style(candidate)[3],
            char_rects=_char_rects(candidate),
            pii_type=candidate.pii_type,
            group_id=candidate.group_id,
            state=state,
            authorized_by=authorized_by,
        )
        plan.targets.append(target)
        _log(candidate, state, authorized_by, why, "transformed")
        if replacement:
            plan.replacement_map[candidate.normalized] = replacement
        if candidate.group_id:
            plan.group_membership.setdefault(candidate.group_id, []).append(candidate.id)

    plan.unresolved = unresolved

    plan.targets = _merge_overlapping_targets(plan.targets)

    for group in detection.groups:
        if group.group_id in plan.group_membership:
            # A line the user chose to skip must not be expected to disappear.
            plan.group_value_texts[group.group_id] = [
                ln.text for ln in group.value_lines if ln.key() not in skipped_lines
            ]

    if document is not None:
        plan.expected_residual_counts = _compute_expected_residuals(plan, document)

    return plan


def _compute_expected_residuals(plan: TransformationPlan, document) -> dict:
    """How many occurrences of each accepted value are expected to remain.

    Not always zero. "LJPS Manufacturing Inc." is correctly never a
    candidate at all once "LJPS" is a known person - the organizational-
    suffix guard in propagate() skips it deliberately - so that occurrence
    was never going to be redacted, and the verifier's leak check must not
    flag its survival as a leak. Computed from the ORIGINAL document text,
    once, here - a value's total occurrence count minus however many of
    those occurrences actually became targets is exactly how many should
    still be findable in the output.
    """
    import re as _re

    accepted_counts: dict[str, int] = {}
    for target in plan.targets:
        key = " ".join(target.original.split()).strip(".,;: ").lower()
        accepted_counts[key] = accepted_counts.get(key, 0) + 1

    if not accepted_counts:
        return {}

    total_counts: dict[str, int] = {key: 0 for key in accepted_counts}
    patterns = {
        key: _re.compile(rf"(?<![A-Za-z0-9]){_re.escape(key)}(?![A-Za-z0-9])", _re.I)
        for key in accepted_counts
    }
    for page in document.pages:
        for line in page.lines:
            normalized_line = " ".join(line.text.split()).lower()
            for key, pattern in patterns.items():
                total_counts[key] += len(pattern.findall(normalized_line))

    return {
        key: max(0, total_counts[key] - accepted_counts[key])
        for key in accepted_counts
    }


def _overlap_fraction(a, b) -> float:
    """What share of the SMALLER rect's area the two rects actually share.

    Adjacency is not overlap. Two fields on consecutive lines, or side by side
    in a table, can sit a point or two apart - merging those drops one of them
    entirely, which is worse than the doubled text this exists to prevent. Only
    genuine area overlap - the same glyphs claimed by two candidates - counts.
    """
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    intersection = (ix1 - ix0) * (iy1 - iy0)
    smaller = min((ax1 - ax0) * (ay1 - ay0), (bx1 - bx0) * (by1 - by0))
    return intersection / smaller if smaller > 0 else 0.0


OVERLAP_THRESHOLD = 0.35


def _rects_touch(a, b) -> bool:
    return _overlap_fraction(a, b) >= OVERLAP_THRESHOLD


def _merge_overlapping_targets(targets: list) -> list:
    """Never let two targets redact-and-insert onto the same spot.

    A line can produce several candidates after retyping, widening and the
    household split - e.g. the person span and a group-value span that both end
    up covering the same or an adjacent region. Applying both draws the second
    replacement over the first without clearing it, which is exactly the
    doubled, overlapping text reported from real output: the original glyphs
    were never fully cleared before something else was written on top.

    One target per page/line survives per touching cluster: the one with the
    longest original span, since that one is most likely to be the whole value.
    """
    from collections import defaultdict

    # Group by page; comparing every pair on a page is cheap at this scale.
    by_page: dict[int, list] = defaultdict(list)
    for target in targets:
        by_page[target.page_no].append(target)

    kept: list = []
    for page_targets in by_page.values():
        clusters: list[list] = []
        for target in page_targets:
            placed = False
            for cluster in clusters:
                if any(_rects_touch(target.rect, member.rect) for member in cluster):
                    cluster.append(target)
                    placed = True
                    break
            if not placed:
                clusters.append([target])
        # A touching group may need to merge further (transitive overlap).
        merged_clusters: list[list] = []
        for cluster in clusters:
            absorbed = False
            for existing in merged_clusters:
                if any(
                    _rects_touch(a.rect, b.rect) for a in cluster for b in existing
                ):
                    existing.extend(cluster)
                    absorbed = True
                    break
            if not absorbed:
                merged_clusters.append(cluster)

        for cluster in merged_clusters:
            if len(cluster) == 1:
                kept.append(cluster[0])
                continue
            winner = max(cluster, key=lambda t: (len(t.original), t.rect[2] - t.rect[0]))
            kept.append(winner)
    return kept


def _disc(candidate: Candidate) -> str:
    """Discriminator so identical names in unrelated fields do not merge.

    Group membership is the only relationship signal available in V1; two
    occurrences of the same name inside the same labelled field are the same
    person, occurrences elsewhere are treated as the same person too unless the
    user edits one. Cross-document merging is prevented by registry scope.
    """
    return ""
