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
    #: WHO authorized this (decisions.authorization.AuthOrigin value) and why.
    #: ACCEPTED alone used to mean both "a person pressed Redact" and "nothing
    #: objected at registration"; the transformation boundary now needs to know.
    origin: str = "REVIEW_REQUIRED"
    reason: str = ""


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
    reviewed: set[str] = field(default_factory=set)
    _undo: list[list[tuple[str, Decision]]] = field(default_factory=list)
    on_change: Optional[Callable[[], None]] = None

    # -- registration ------------------------------------------------------

    def register(self, candidates: list[Candidate], fail_safe: bool = False) -> None:
        """Seed decisions for newly detected candidates - FAIL CLOSED (0.42.0).

        The one authorization policy (decisions/authorization.py) decides: a
        candidate it auto-approves is ACCEPTED with origin AUTO_APPROVED; every
        other one waits (SKIPPED, origin REVIEW_REQUIRED) - readable, listed with
        the reason, until a person presses Redact, Keep or Edit.

        `fail_safe=True` used to make redaction the default for everything found,
        including flagged and low-confidence items. It is accepted for backward
        compatibility but no longer widens anything: no flag may approve what the
        policy does not.
        """
        from .authorization import AuthOrigin, assess, confirmed_name_words

        words = confirmed_name_words(candidates)
        for c in candidates:
            if c.id in self.decisions:
                continue
            verdict = assess(c, words)
            if verdict.eligible:
                state, origin = DecisionState.ACCEPTED, AuthOrigin.AUTO_APPROVED
            else:
                state, origin = DecisionState.SKIPPED, AuthOrigin.REVIEW_REQUIRED
                c.needs_review = True
                if not c.review_reason:
                    c.review_reason = verdict.reason
            self.decisions[c.id] = Decision(candidate_id=c.id, state=state,
                                            origin=origin.value, reason=verdict.reason)

    def origin(self, candidate: Candidate) -> str:
        d = self.decisions.get(candidate.id)
        return d.origin if d else "REVIEW_REQUIRED"

    def awaiting_review(self, candidates: list[Candidate]) -> list[Candidate]:
        """Review-required items no person has decided yet (left readable)."""
        return [c for c in candidates if self.origin(c) == "REVIEW_REQUIRED"]

    def approve_auto_eligible(self, candidates: list[Candidate]) -> list[Candidate]:
        """The ONLY bulk approval: what the policy itself auto-approves.

        CLI --accept-all, the batch "process all" and the GUI "Redact everything"
        used to mark EVERY candidate ACCEPTED - flagged, ambiguous and held items
        included. A bulk action is not a per-item human judgement; review-required
        items stay pending."""
        from .authorization import AuthOrigin, assess, confirmed_name_words

        words = confirmed_name_words(candidates)
        approved = []
        for c in candidates:
            if assess(c, words).eligible and self.state(c) is not DecisionState.ACCEPTED:
                approved.append(c)
        if approved:
            self._snapshot([c.id for c in approved])
            for c in approved:
                d = self.decisions.setdefault(c.id, Decision(candidate_id=c.id))
                d.state, d.origin, d.reason = DecisionState.ACCEPTED, AuthOrigin.AUTO_APPROVED.value, \
                    "approved in bulk: the policy auto-approves it"
            self._notify()
        return approved

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
        """An explicit decision by a PERSON (the UI, a test standing in for one).
        Records the user as the authorizing origin."""
        ids = [c.id for c in candidates]
        self._snapshot(ids)
        origin = {DecisionState.ACCEPTED: "USER_APPROVED", DecisionState.EDITED: "USER_EDITED",
                  DecisionState.SKIPPED: "USER_KEPT", DecisionState.MANUALLY_ADDED: "USER_ADDED",
                  DecisionState.UNREVIEWED: "REVIEW_REQUIRED"}[state]
        for c in candidates:
            d = self.decisions.setdefault(c.id, Decision(candidate_id=c.id))
            d.state = state
            d.origin = origin
            d.reason = "decided by the user"
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
            # Approving one occurrence never approves one whose context is unclear.
            and (c is like or state is DecisionState.SKIPPED or not getattr(c, "ambiguous", False))
        ]
        self.set_state(matches, state)
        return matches

    def apply_to_entity(
        self,
        all_candidates: list[Candidate],
        like: Candidate,
        state: DecisionState,
        index=None,
        replacement: Optional[str] = None,
        extra: Optional[list[Candidate]] = None,
    ) -> list[Candidate]:
        """Apply one decision to every candidate of the same IDENTITY, not just the
        same string: "JOHN SMITH", "Smith, John" and "John R. Smith" are one
        person (see entities/identity.py for the evidence for each link).

        An edited replacement is re-rendered per variant, so editing "John Smith"
        to "Mark Santos" turns "Smith, John" into "Santos, Mark".
        """
        from ..entities.identity import EntityIndex, compose_replacement

        index = index or EntityIndex.build(all_candidates)
        targets = {c.id: c for c in index.linked(like)}
        for c in extra or []:
            targets[c.id] = c
        found = [c for c in targets.values()
                 if c is like or c.id == like.id or state is DecisionState.SKIPPED
                 or not getattr(c, "ambiguous", False)]
        if state is DecisionState.EDITED and replacement:
            self._snapshot([c.id for c in found])
            profile = index.entity_of(like)
            context = profile.observed() if profile is not None else []
            for c in found:
                d = self.decisions.setdefault(c.id, Decision(candidate_id=c.id))
                d.state = state
                d.origin, d.reason = "USER_EDITED", "edited by the user"
                d.replacement_override = compose_replacement(replacement, c.text, context)
            self._notify()
        else:
            self.set_state(found, state)
        return found

    def add_manual(self, candidate: Candidate) -> None:
        self._snapshot([candidate.id])
        self.decisions[candidate.id] = Decision(
            candidate_id=candidate.id, state=DecisionState.MANUALLY_ADDED,
            origin="USER_ADDED", reason="added by the user",
        )
        self._notify()

    def mark_reviewed(self, candidates: list[Candidate], value: bool = True) -> None:
        """Confirm an item has been looked at, without changing its fate."""
        for c in candidates:
            if value:
                self.reviewed.add(c.id)
            else:
                self.reviewed.discard(c.id)
        self._notify()

    def is_reviewed(self, candidate: Candidate) -> bool:
        return candidate.id in self.reviewed

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
