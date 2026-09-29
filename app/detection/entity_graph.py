"""A co-occurrence entity graph (spec sections 15-19, 21).

Every existing consistency mechanism is TYPE-SPECIFIC: names share a
pseudonym through token mapping, SSNs and EINs share one through digit
keying. None of that links a person to what sits BESIDE them - their SSN,
their address, in the same field group - as one connected identity.

This module builds that link. Nodes that co-occur on the same line, or as
sibling value lines under one compound header, join one connected component
via union-find. What it is used for: when the user decides one node in a
component, every other node already in it is marked reviewed alongside it -
one decision covers the household's whole cluster of fields, rather than a
separate click for the name, the SSN, and the address that were always sitting
together on the page.

This never changes what a candidate IS or what it gets replaced with - it
only cuts review clicks for values already known to belong together. Each
node still redacts, or not, by its own existing decision; linking just
proposes "these went together" so confirming one can carry the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .types import Candidate, LogicalFieldGroup


class DisjointSet:
    """Textbook union-find, path compression and union by size."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._size: dict[str, int] = {}

    def add(self, key: str) -> None:
        if key not in self._parent:
            self._parent[key] = key
            self._size[key] = 1

    def find(self, key: str) -> str:
        self.add(key)
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:
            self._parent[key], key = root, self._parent[key]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._size[ra] < self._size[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        self._size[ra] += self._size[rb]

    def connected(self, a: str, b: str) -> bool:
        return self.find(a) == self.find(b)

    def component(self, key: str) -> set[str]:
        root = self.find(key)
        return {k for k in self._parent if self.find(k) == root}


@dataclass
class EntityGraph:
    """Links Candidate ids into connected components by co-occurrence."""

    sets: DisjointSet = field(default_factory=DisjointSet)
    #: candidate id -> the Candidate itself, so a caller can get objects back
    #: from a component rather than just ids.
    by_id: dict[str, Candidate] = field(default_factory=dict)

    def linked(self, candidate: Candidate) -> list[Candidate]:
        """Every OTHER candidate co-occurring with this one. Never includes
        the candidate itself."""
        if candidate.id not in self.by_id:
            return []
        component = self.sets.component(candidate.id)
        return [self.by_id[cid] for cid in component if cid != candidate.id and cid in self.by_id]


def build_entity_graph(
    candidates: list[Candidate], groups: list[LogicalFieldGroup]
) -> EntityGraph:
    """Link candidates that co-occur: same line, or sibling value lines under
    one compound header (spec sections 15-16 - the same grouping groups.py
    already computed; this reads it, exactly as the Presidio context builder
    reads groups.py's labels rather than re-parsing anything).
    """
    graph = EntityGraph()
    for candidate in candidates:
        graph.by_id[candidate.id] = candidate
        graph.sets.add(candidate.id)

    # Same line -> linked. "Mark Lang    123-45-6789" on one row is one entity.
    by_line: dict[tuple, list[Candidate]] = {}
    for candidate in candidates:
        by_line.setdefault(candidate.line.key(), []).append(candidate)
    for same_line in by_line.values():
        for other in same_line[1:]:
            graph.sets.union(same_line[0].id, other.id)

    # Sibling value lines under one compound header -> linked. This is
    # exactly the stacked-field shape groups.py already resolves: a "Name,
    # address, and zip code" header binding three lines together means those
    # three lines' candidates are one entity, not three unrelated ones.
    line_to_group: dict[tuple, str] = {}
    for group in groups:
        for line in group.value_lines:
            line_to_group[line.key()] = group.group_id

    by_group: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        group_id = line_to_group.get(candidate.line.key())
        if group_id is not None:
            by_group.setdefault(group_id, []).append(candidate)
    for members in by_group.values():
        for other in members[1:]:
            graph.sets.union(members[0].id, other.id)

    return graph


def linked_reviewed_ids(
    graph: EntityGraph, decided: Candidate, all_candidates: list[Candidate]
) -> list[Candidate]:
    """Every candidate that should be marked reviewed alongside `decided`,
    because it is already known to belong to the same entity.

    This never touches what a linked candidate's OWN decision is - only that
    it no longer needs a separate look, the same way deciding one occurrence
    of a repeated value already moves the whole group to Done.
    """
    return graph.linked(decided)
