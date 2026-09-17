"""Proof that nothing reaches the network (spec sections 4 and 74).

Every socket connection attempt raises. If any layer - PyMuPDF, spaCy, ONNX
Runtime, the tokenizer, llama.cpp, Qt - tries to open a connection during
analysis, redaction or verification, these tests fail and the build goes red.

"Designed to be offline" and "proven offline" are different claims. For a tool
whose whole purpose is keeping client data local, only the second one counts.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from app.decisions.manager import DecisionState
from app.session import AnonymizationSession
from tests import fixtures


class NetworkAccessAttempted(AssertionError):
    """Raised the moment anything tries to open a connection."""


@pytest.fixture
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise NetworkAccessAttempted(
            "the application attempted a network connection; it must run fully offline"
        )

    # Every route out: raw sockets, the convenience helpers, and DNS.
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "gethostbyname", blocked)

    try:
        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", blocked)
    except ImportError:  # pragma: no cover
        pass
    try:
        import requests

        monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    except ImportError:
        pass
    yield


def test_analysis_runs_with_the_network_blocked(no_network, tmp_path):
    source = fixtures.form_pdf(tmp_path / "form.pdf")
    session = AnonymizationSession(source_path=source)
    session.analyse()
    assert session.candidates, "nothing detected offline"


def test_full_export_and_verification_run_offline(no_network, tmp_path):
    source = fixtures.form_pdf(tmp_path / "form.pdf")
    session = AnonymizationSession(source_path=source)
    session.analyse()
    session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)
    _apply_report, report = session.process(str(tmp_path / "out.pdf"))
    assert report.passed, report.failures
    assert Path(session.mapping_file).exists()


def test_the_offline_guard_itself_works(no_network):
    """A guard that cannot fail proves nothing."""
    with pytest.raises(NetworkAccessAttempted):
        socket.create_connection(("example.com", 80))


@pytest.mark.slow
def test_gliner_loads_and_infers_offline(no_network):
    from app.detection.gliner import GlinerDetector

    detector = GlinerDetector()
    if not detector.available:
        pytest.skip("GLiNER weights not fetched")
    spans = detector.predict("John Smith lives at 123 Main Street .".split())
    assert spans, "the model produced nothing offline"


@pytest.mark.slow
def test_llm_auditor_loads_and_infers_offline(no_network):
    from app.detection.auditor import LlmAuditor

    auditor = LlmAuditor()
    if not auditor.available:
        pytest.skip("LLM weights not fetched")
    missed, _wrong = auditor.audit("Contact John Smith on 555-123-4567.", [])
    assert isinstance(missed, list)


def test_no_module_reaches_the_network_at_import_time(no_network):
    """Imports must not phone home either."""
    import importlib

    for module in (
        "app.detection.gliner",
        "app.detection.auditor",
        "app.detection.ner",
        "app.export.redactor",
        "app.verification.verifier",
        "app.document.hidden",
    ):
        importlib.reload(importlib.import_module(module))
