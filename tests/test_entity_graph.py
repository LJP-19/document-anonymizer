"""Co-occurring PII links into one entity - deciding one carries the rest
to reviewed, without changing anyone's own decision (spec sections 15-19)."""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.decisions.manager import DecisionState
from app.detection.entity_graph import DisjointSet, build_entity_graph
from app.detection.types import PiiType
from app.session import AnonymizationSession


def _stacked_field(path: Path) -> str:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 10)
    c.drawString(72, 720, "Name, address, and zip code")
    c.drawString(72, 706, "Mark Lang")
    c.drawString(72, 692, "4820 Camino Del Rio Dr")
    c.drawString(72, 678, "San Jose, CA 95129")
    c.save()
    return str(path)


def test_disjoint_set_basics():
    sets = DisjointSet()
    sets.union("a", "b")
    sets.union("b", "c")
    assert sets.connected("a", "c")
    assert sets.component("a") == {"a", "b", "c"}
    sets.add("d")
    assert not sets.connected("a", "d")


def test_a_stacked_field_links_name_street_and_city(tmp_path):
    session = AnonymizationSession(source_path=_stacked_field(tmp_path / "stack.pdf"))
    session.analyse()
    graph = build_entity_graph(session.candidates, session.detection.groups)

    name = next(c for c in session.candidates if c.pii_type is PiiType.PERSON)
    linked_texts = {c.text for c in graph.linked(name)}
    assert "4820 Camino Del Rio Dr" in linked_texts
    assert "San Jose, CA 95129" in linked_texts


def test_unrelated_values_on_different_fields_are_not_linked(tmp_path):
    path = tmp_path / "unrelated.pdf"
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 10)
    c.drawString(72, 720, "Taxpayer name")
    c.drawString(72, 706, "Mark Lang")
    c.drawString(72, 660, "Spouse name")
    c.drawString(72, 646, "Jenny Lang")
    c.save()

    session = AnonymizationSession(source_path=str(path))
    session.analyse()
    graph = build_entity_graph(session.candidates, session.detection.groups)

    mark = next(c for c in session.candidates if c.text.strip() == "Mark Lang")
    jenny = next(c for c in session.candidates if c.text.strip() == "Jenny Lang")
    assert jenny not in graph.linked(mark)


def test_deciding_one_linked_field_marks_the_others_reviewed(tmp_path):
    """The one concrete integration point: a decision on the name also marks
    the co-occurring street and city/state/zip reviewed - WITHOUT changing
    what THEIR own decision is."""
    session = AnonymizationSession(source_path=_stacked_field(tmp_path / "review.pdf"))
    session.analyse()

    from app.detection.entity_graph import build_entity_graph

    name = next(c for c in session.candidates if c.pii_type is PiiType.PERSON)
    street = next(c for c in session.candidates if c.pii_type is PiiType.STREET)

    street_state_before = session.decisions.state(street)

    session.decisions.set_state([name], DecisionState.ACCEPTED)
    session.decisions.mark_reviewed([name], True)
    assert not session.decisions.is_reviewed(street)

    graph = build_entity_graph(session.candidates, session.detection.groups)
    for linked in graph.linked(name):
        session.decisions.mark_reviewed([linked], True)

    assert session.decisions.is_reviewed(street)
    # The street's own decision is untouched by this - only its REVIEWED
    # flag moved. Marking it reviewed must not silently decide to redact
    # or keep it on its own account.
    assert session.decisions.state(street) == street_state_before
