"""User decisions, kept separate from detection and identity (spec section 26).

The detector finds candidates. The registry manages identities. This class owns
only what the user wants done, plus undo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from ..detection.types import Candidate, PiiType


class DecisionState(str, Enum):
    UNREVIEWED = "UNREVIEWED"
    ACCEPTED = "ACCEPTED"
    SKIPPED = "SKIPPED"
    EDITED = "EDITED"
    MANUALLY_ADDED = "MANUALLY_ADDED"


ACTIONABLE = {DecisionState.ACCEPTED, DecisionState.EDITED, DecisionState.MANUALLY_ADDED}


@dataclass
class Decision:
    candidate_id: str
    state: DecisionState = DecisionState.UNREVIEWED
    replacement_override: Optional[str] = None


@dataclass
class OccurrenceGroup:
    """Repeated occurrences of the same value, collapsed for review (section 27)."""

    pii_type: PiiType
    normalized: str
    display: str
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.candidates)

    @property
    def needs_review(self) -> bool:
        return any(c.needs_review for c in self.candidates)


@dataclass
class DecisionManager:
    decisions: dict[str, Decision] = field(default_factory=dict)
    _undo: list[list[tuple[str, Decision]]] = field(default_factory=list)
    on_change: Optional[Callable[[], None]] = None

    # -- registration ------------------------------------------------------

    def register(self, candidates: list[Candidate], default_accept_threshold: float = 0.9) -> None:
        for c in candidates:
            if c.id in self.decisions:
                continue
            state = (
                DecisionState.ACCEPTED
                if c.confidence >= default_accept_threshold and not c.needs_review
                else DecisionState.UNREVIEWED
            )
            self.decisions[c.id] = Decision(candidate_id=c.id, state=state)

    # -- queries -----------------------------------------------------------

    def state(self, candidate: Candidate) -> DecisionState:
        d = self.decisions.get(candidate.id)
        return d.state if d else DecisionState.UNREVIEWED

    def is_actionable(self, candidate: Candidate) -> bool:
        return self.state(candidate) in ACTIONABLE

    def override(self, candidate: Candidate) -> Optional[str]:
        d = self.decisions.get(candidate.id)
        return d.replacement_override if d else None

    def unreviewed(self, candidates: list[Candidate]) -> list[Candidate]:
        return [c for c in candidates if self.state(c) is DecisionState.UNREVIEWED]

    @staticmethod
    def occurrence_groups(candidates: list[Candidate]) -> list[OccurrenceGroup]:
        buckets: dict[tuple[PiiType, str], OccurrenceGroup] = {}
        for c in candidates:
            key = (c.pii_type, c.normalized.lower())
            g = buckets.get(key)
            if g is None:
                g = OccurrenceGroup(pii_type=c.pii_type, normalized=key[1], display=c.normalized)
                buckets[key] = g
            g.candidates.append(c)
        return sorted(buckets.values(), key=lambda g: (-g.count, g.pii_type.value))

    # -- mutations ---------------------------------------------------------

    def _snapshot(self, ids: list[str]) -> None:
        self._undo.append([(i, Decision(**vars(self.decisions[i]))) for i in ids if i in self.decisions])
        if len(self._undo) > 200:
            self._undo.pop(0)

    def set_state(
        self,
        candidates: list[Candidate],
        state: DecisionState,
        replacement: Optional[str] = None,
    ) -> None:
        ids = [c.id for c in candidates]
        self._snapshot(ids)
        for c in candidates:
            d = self.decisions.setdefault(c.id, Decision(candidate_id=c.id))
            d.state = state
            if replacement is not None:
                d.replacement_override = replacement
        self._notify()

    def accept(self, candidates: list[Candidate]) -> None:
        self.set_state(candidates, DecisionState.ACCEPTED)

    def skip(self, candidates: list[Candidate]) -> None:
        self.set_state(candidates, DecisionState.SKIPPED)

    def edit(self, candidates: list[Candidate], replacement: str) -> None:
        self.set_state(candidates, DecisionState.EDITED, replacement)

    def apply_to_all(
        self, all_candidates: list[Candidate], like: Candidate, state: DecisionState
    ) -> list[Candidate]:
        matches = [
            c
            for c in all_candidates
            if c.pii_type is like.pii_type and c.normalized.lower() == like.normalized.lower()
        ]
        self.set_state(matches, state)
        return matches

    def add_manual(self, candidate: Candidate) -> None:
        self._snapshot([candidate.id])
        self.decisions[candidate.id] = Decision(
            candidate_id=candidate.id, state=DecisionState.MANUALLY_ADDED
        )
        self._notify()

    def undo(self) -> bool:
        if not self._undo:
            return False
        for cid, decision in self._undo.pop():
            self.decisions[cid] = decision
        self._notify()
        return True

    def _notify(self) -> None:
        if self.on_change:
            self.on_change()
