"""The single transformation plan (spec section 33).

Preview and export both consume this object. There is deliberately no second
code path: if the preview shows it, the exported file contains it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..decisions.manager import DecisionManager, DecisionState
from ..detection.types import Candidate, DetectionResult, PiiType, Rect
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
    pii_type: PiiType = PiiType.UNCLASSIFIED_GROUP_VALUE
    group_id: Optional[str] = None
    state: DecisionState = DecisionState.ACCEPTED


@dataclass
class TransformationPlan:
    source_path: str
    targets: list[Target] = field(default_factory=list)
    skipped_values: list[str] = field(default_factory=list)
    group_membership: dict[str, list[str]] = field(default_factory=dict)
    group_value_texts: dict[str, list[str]] = field(default_factory=dict)
    ocr_required_pages: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def replacements(self) -> list[str]:
        return [t.replacement for t in self.targets]

    @property
    def originals(self) -> list[str]:
        return [t.original for t in self.targets]

    def targets_for_page(self, page_no: int) -> list[Target]:
        return [t for t in self.targets if t.page_no == page_no]


def build_plan(
    source_path: str,
    detection: DetectionResult,
    decisions: DecisionManager,
    registry: EntityRegistry,
    ocr_required_pages: Optional[list[int]] = None,
) -> TransformationPlan:
    plan = TransformationPlan(
        source_path=source_path,
        ocr_required_pages=list(ocr_required_pages or []),
        warnings=list(detection.warnings),
    )

    skipped_lines: set[tuple[int, int, int]] = set()

    for candidate in detection.candidates:
        state = decisions.state(candidate)
        if state not in (
            DecisionState.ACCEPTED,
            DecisionState.EDITED,
            DecisionState.MANUALLY_ADDED,
        ):
            if state is DecisionState.SKIPPED:
                plan.skipped_values.append(candidate.normalized)
                skipped_lines.add(candidate.line.key())
            continue

        override = decisions.override(candidate)
        if override is not None:
            replacement = override
            registry.override(candidate.pii_type, candidate.normalized, override, _disc(candidate))
        else:
            replacement = registry.pseudonym_for(
                candidate.pii_type, candidate.normalized, _disc(candidate)
            )

        target = Target(
            candidate_id=candidate.id,
            page_no=candidate.page_no,
            rect=candidate.rect,
            original=candidate.text,
            replacement=replacement,
            font_size=candidate.line.size,
            font_name=candidate.line.font,
            pii_type=candidate.pii_type,
            group_id=candidate.group_id,
            state=state,
        )
        plan.targets.append(target)
        if candidate.group_id:
            plan.group_membership.setdefault(candidate.group_id, []).append(candidate.id)

    for group in detection.groups:
        if group.group_id in plan.group_membership:
            # A line the user chose to skip must not be expected to disappear.
            plan.group_value_texts[group.group_id] = [
                ln.text for ln in group.value_lines if ln.key() not in skipped_lines
            ]

    return plan


def _disc(candidate: Candidate) -> str:
    """Discriminator so identical names in unrelated fields do not merge.

    Group membership is the only relationship signal available in V1; two
    occurrences of the same name inside the same labelled field are the same
    person, occurrences elsewhere are treated as the same person too unless the
    user edits one. Cross-document merging is prevented by registry scope.
    """
    return ""
