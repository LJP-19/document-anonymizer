"""The model phase of analysis must be bounded, visible and stoppable.

Reported: analysis "stuck for more than an hour" on the smallest model. Measured
with the real 0.5B model, one call costs ~10 s and the model is called about once
per page per phase, so time grew with page count and nothing bounded it, showed
progress, or let the user stop it. These tests use a stand-in model that is slow
on purpose; the real-model numbers are recorded in CLAUDE.md.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from app.detection import auditor

pytestmark = pytest.mark.usefixtures("isolated_home")


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv("DOCANON_LLM_BUDGET_SECONDS", raising=False)
    auditor.reset_stop()
    auditor.set_progress_listener(None)
    yield
    auditor.reset_stop()
    auditor.set_progress_listener(None)


class _SlowAuditor:
    def __init__(self, delay=0.12):
        self.delay = delay
        self.calls = 0
        self._llm = MagicMock()
        self._llm.create_chat_completion.side_effect = self._chat

    def _chat(self, **kwargs):
        self.calls += 1
        time.sleep(self.delay)
        return {"choices": [{"message": {"content": '{"verdicts": []}'}}]}

    def load(self):
        pass

    def audit(self, text, detected):
        self.calls += 1
        time.sleep(self.delay)
        return [], []


def _document(tmp_path, pages):
    import pymupdf

    from app.session import AnonymizationSession

    path = str(tmp_path / "doc.pdf")
    pdf = pymupdf.open()
    for n in range(pages):
        pdf.new_page().insert_text((72, 100), f"Page {n + 1} Mark Jones", fontsize=11)
    pdf.save(path)
    pdf.close()
    return AnonymizationSession(source_path=path).provider.load(path)


def _candidates(document):
    from app.detection.types import Candidate, PiiType, Source

    out = []
    for page in document.pages:
        line = page.lines[0]
        out.append(Candidate(
            pii_type=PiiType.PERSON, text="Mark Jones", page_no=page.number,
            rect=(0, 0, 50, 10), line=line, start=0, end=10, confidence=0.9, source=Source.NER,
        ))
    return out


def test_the_check_phase_stops_at_its_budget_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCANON_LLM_BUDGET_SECONDS", "0.25")
    document = _document(tmp_path, 10)
    fake = _SlowAuditor()

    rejected, warnings = auditor.adjudicate_document(document, _candidates(document), auditor=fake)

    assert 1 <= fake.calls < 10, f"the budget must cut the phase short (made {fake.calls} calls)"
    message = next(w for w in warnings if "AI check" in w)
    assert "stopped after" in message and "of 10 page(s)" in message
    assert "other detection layers covered the whole document" in message


def test_the_review_phase_stops_at_its_budget_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCANON_LLM_BUDGET_SECONDS", "0.25")
    document = _document(tmp_path, 10)
    fake = _SlowAuditor()

    _additions, _wrong, warnings = auditor.audit_document(document, [], auditor=fake)

    assert 1 <= fake.calls < 10
    assert any("AI review stopped after" in w for w in warnings)


def test_a_stop_request_skips_the_model_phase_immediately(tmp_path):
    document = _document(tmp_path, 5)
    fake = _SlowAuditor()
    auditor.request_stop()

    _rejected, warnings = auditor.adjudicate_document(document, _candidates(document), auditor=fake)

    assert fake.calls == 0
    assert any("skipped at your request" in w for w in warnings)


def test_a_stop_request_ends_a_phase_already_running(tmp_path):
    import threading

    document = _document(tmp_path, 12)
    fake = _SlowAuditor(delay=0.1)
    threading.Timer(0.25, auditor.request_stop).start()

    _rejected, warnings = auditor.adjudicate_document(document, _candidates(document), auditor=fake)

    assert 1 <= fake.calls < 12
    assert any("skipped at your request" in w for w in warnings)


def test_zero_means_no_limit_and_nothing_is_cut(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCANON_LLM_BUDGET_SECONDS", "0")
    document = _document(tmp_path, 4)
    fake = _SlowAuditor(delay=0.02)

    _rejected, warnings = auditor.adjudicate_document(document, _candidates(document), auditor=fake)

    assert fake.calls == 4
    assert not any("AI check" in w for w in warnings)


def test_progress_is_reported_page_by_page_and_a_broken_listener_cannot_break_analysis(tmp_path):
    document = _document(tmp_path, 3)
    seen = []
    auditor.set_progress_listener(seen.append)
    auditor.adjudicate_document(document, _candidates(document), auditor=_SlowAuditor(delay=0.01))
    assert seen == ["AI check: page 1 of 3", "AI check: page 2 of 3", "AI check: page 3 of 3"]

    def explode(_text):
        raise RuntimeError("the window was already closed")

    auditor.set_progress_listener(explode)
    rejected, _warnings = auditor.adjudicate_document(
        document, _candidates(document), auditor=_SlowAuditor(delay=0.01)
    )
    assert rejected == set()


def test_nothing_cuts_the_review_short_by_default_but_a_cap_can_still_be_set(tmp_path, monkeypatch):
    """Reversed on the owner's instruction (the model must review every page,
    and a hidden cap quietly turns that into 'the first few'). The defence
    against an apparent hang is now visibility, the Skip button and the
    persistent switch. A cap stays available for anyone who wants one."""
    assert auditor.AUDIT_PHASE_SECONDS == float("inf")
    assert auditor.ADJUDICATE_PHASE_SECONDS == float("inf")

    document = _document(tmp_path, 6)
    fake = _SlowAuditor(delay=0.01)
    _rejected, warnings = auditor.adjudicate_document(document, _candidates(document), auditor=fake)
    assert fake.calls == 6 and not any("AI check" in w for w in warnings)


def test_the_review_covers_every_page_even_when_every_detection_is_confident(tmp_path):
    """It used to skip pages whose detections were all confident and typed.
    A confident, typed detection can still be wrong (a form's own wording, a
    mislabelled value), so every page with text is reviewed now."""
    document = _document(tmp_path, 6)
    fake = _SlowAuditor(delay=0)
    auditor.audit_document(document, _candidates(document), auditor=fake)
    assert fake.calls == 6


def test_a_page_with_no_text_is_not_sent_to_the_model(tmp_path):
    import pymupdf

    from app.session import AnonymizationSession

    path = str(tmp_path / "blank.pdf")
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 100), "Page one Mark Jones", fontsize=11)
    pdf.new_page()  # blank
    pdf.new_page().insert_text((72, 100), "Page three", fontsize=11)
    pdf.save(path)
    pdf.close()
    document = AnonymizationSession(source_path=path).provider.load(path)
    fake = _SlowAuditor(delay=0)
    auditor.audit_document(document, [], auditor=fake)
    assert fake.calls == 2


def test_progress_gains_a_time_estimate_after_a_few_pages(tmp_path):
    document = _document(tmp_path, 6)
    seen = []
    auditor.set_progress_listener(seen.append)
    auditor.adjudicate_document(document, _candidates(document), auditor=_SlowAuditor(delay=0.01))
    assert seen[0] == "AI check: page 1 of 6" and seen[1] == "AI check: page 2 of 6"
    assert seen[2].startswith("AI check: page 3 of 6 - ") and seen[2].endswith("left")
    assert "left" not in seen[5], "nothing remains after the last page"


# ----------------------------------------------------------- the window


@pytest.fixture
def window():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from app.ui.main_window import MainWindow

    w = MainWindow()
    w._set_status = MagicMock()
    yield w
    w.close()


def test_progress_text_reaches_the_status_line_only_while_analysing(window):
    from app.ui.main_window import Status

    window._on_audit_progress("AI check: page 2 of 9")
    window._set_status.assert_not_called()

    window._analysing_name = "return.pdf"
    window._on_audit_progress("AI check: page 2 of 9")
    status, detail = window._set_status.call_args.args
    assert status == Status.ANALYZING
    assert "return.pdf" in detail and "AI check: page 2 of 9" in detail


def test_the_skip_button_exists_hidden_and_only_shows_when_the_audit_is_on(window):
    from unittest.mock import patch

    assert window.skip_ai_button.isHidden()
    with patch.object(auditor, "llm_audit_enabled", return_value=False):
        window._begin_ai_review_ui()
    assert window.skip_ai_button.isHidden()
    with patch.object(auditor, "llm_audit_enabled", return_value=True):
        window._begin_ai_review_ui()
    assert not window.skip_ai_button.isHidden()
    window._end_ai_review_ui()
    assert window.skip_ai_button.isHidden()


def test_pressing_skip_asks_the_analysis_to_stop(window):
    from unittest.mock import patch

    with patch.object(auditor, "llm_audit_enabled", return_value=True):
        window._begin_ai_review_ui()
    assert not auditor.stop_requested()
    window.skip_ai_button.click()
    assert auditor.stop_requested()
    assert not window.skip_ai_button.isEnabled(), "a second press must not be possible"
    window._begin_ai_review_ui()
    assert not auditor.stop_requested(), "each new document starts with a clean slate"
