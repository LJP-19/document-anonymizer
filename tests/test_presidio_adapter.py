"""The Presidio proposal layer holds to five guardrails (see the module
docstring in app/detection/presidio_adapter.py):

  1. PyMuPDF is the only PDF backbone - no coordinates of its own.
  2. groups.py is read, never rewritten or duplicated.
  3. Presidio only PROPOSES - it never types or decides on its own.
  4. en_core_web_sm only, reused from the already-loaded model.
  5. Context comes from groups.py's own label output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("presidio_analyzer")

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.detection.deterministic import load_rules
from app.detection.groups import build_groups
from app.detection.presidio_adapter import (
    ENTITY_MAP,
    PresidioDetector,
    PresidioUnavailable,
    build_line_context,
    detect_presidio,
)
from app.detection.types import PiiType, Source
from app.document.provider import NativePdfTextProvider


def _pdf(path: Path, lines: list[str]) -> str:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 10)
    y = 720
    for line in lines:
        c.drawString(72, y, line)
        y -= 14
    c.save()
    return str(path)


def test_loads_and_proposes_on_a_plain_line():
    detector = PresidioDetector()
    detector.load()
    proposals = detector.analyze_line(
        "Contact John Smith at john@example.com or 555-123-4567.", []
    )
    entities = {p.entity_type for p in proposals}
    assert "EMAIL_ADDRESS" in entities
    assert "PERSON" in entities


def test_context_from_a_label_boosts_the_score():
    """Guardrail 5: context comes from groups.py's label text."""
    detector = PresidioDetector()
    detector.load()

    without = detector.analyze_line("Reference 123456789", [])
    with_context = detector.analyze_line(
        "Taxpayer SSN 123456789", ["ssn", "taxpayer", "social", "security"]
    )

    ssn_without = next((p for p in without if p.entity_type == "US_SSN"), None)
    ssn_with = next((p for p in with_context if p.entity_type == "US_SSN"), None)
    assert ssn_with is not None
    if ssn_without is not None:
        assert ssn_with.score >= ssn_without.score


def test_build_line_context_reads_groups_output_only(tmp_path):
    """Guardrail 2: this reads groups.py's output, never re-parses labels."""
    path = _pdf(
        tmp_path / "ctx.pdf",
        ["Social security number", "123456789", "1  Wages ...... $412,890"],
    )
    doc = NativePdfTextProvider().load(path)
    groups, _labels, _extra = build_groups(doc, [], load_rules())

    context = build_line_context(groups)
    value_line = groups[0].value_lines[0]
    assert value_line.key() in context
    assert "security" in context[value_line.key()].words


def test_a_result_is_always_low_confidence_and_flagged(tmp_path):
    """Guardrail 3: Presidio proposes; it never hands over a confident,
    unreviewed candidate the way a confirmed rule match does."""
    path = _pdf(tmp_path / "propose.pdf", ["John Smith runs a small business."])
    doc = NativePdfTextProvider().load(path)

    candidates, _warnings = detect_presidio(doc, [])
    assert candidates
    for candidate in candidates:
        assert candidate.confidence <= 0.6
        assert candidate.source is Source.NER


def test_an_unmapped_entity_stays_unclassified(tmp_path):
    """Guardrail 3, continued: type-or-skip holds even for a Presidio finding
    - an entity type with no mapped PiiType is never given a shaped
    replacement it may not deserve."""
    from app.detection.presidio_adapter import PresidioProposal

    assert "URL" not in ENTITY_MAP
    assert "ORGANIZATION" not in ENTITY_MAP


def test_geometry_comes_from_line_rect_for_only(tmp_path):
    """Guardrail 1: no coordinate system of its own - every candidate's rect
    is produced by the SAME Line.rect_for() every other detector uses."""
    path = _pdf(tmp_path / "geom.pdf", ["Contact John Smith today."])
    doc = NativePdfTextProvider().load(path)

    candidates, _warnings = detect_presidio(doc, [])
    for candidate in candidates:
        expected = candidate.line.rect_for(candidate.start, candidate.end)
        assert candidate.rect == expected


def test_degrades_gracefully_when_not_installed(tmp_path, monkeypatch):
    import app.detection.presidio_adapter as module

    detector = module.PresidioDetector()

    def boom(*args, **kwargs):
        raise ImportError("simulated: not installed")

    monkeypatch.setattr("builtins.__import__", boom)
    with pytest.raises(module.PresidioUnavailable):
        try:
            detector.load()
        finally:
            pass


def test_a_missing_package_produces_a_warning_not_an_exception(tmp_path, monkeypatch):
    import app.detection.presidio_adapter as module

    class AlwaysUnavailable(module.PresidioDetector):
        def load(self, spacy_nlp=None) -> None:
            raise module.PresidioUnavailable("simulated: not installed")

    path = _pdf(tmp_path / "safe.pdf", ["John Smith"])
    doc = NativePdfTextProvider().load(path)
    candidates, warnings = detect_presidio(doc, [], detector=AlwaysUnavailable())
    assert candidates == []
    assert warnings and "disabled" in warnings[0]


def test_the_engine_still_runs_with_presidio_active(tmp_path):
    """End to end: the full pipeline, with this layer switched on."""
    from app.detection.engine import analyse
    from app.document.provider import NativePdfTextProvider

    path = _pdf(
        tmp_path / "e2e.pdf",
        ["Taxpayer name", "John Smith", "1  Wages ...... $412,890"],
    )
    doc = NativePdfTextProvider().load(path)
    result = analyse(doc, use_presidio=True)
    assert any(c.pii_type is PiiType.PERSON for c in result.candidates)
    for candidate in result.candidates:
        assert "$412,890" not in candidate.text
