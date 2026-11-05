"""Skip against a REAL downloaded model. Skipped wherever no model is available
(CI downloads none); runs on a machine that has one. Not isolated from the home
folder on purpose: it needs the model the user chose."""

from __future__ import annotations

import threading
import time

import pytest

from app.detection import auditor
from app.detection.auditor import LlmAuditor
from app.detection.types import PiiType

pytestmark = pytest.mark.slow

PAGE = ("Taxpayer name: MARIA T GONZALEZ-REYES\nDate of birth 04/11/1979   Country of citizenship: Mexico\n"
        "Place of birth: Guadalajara, Jalisco   Sex: F\nOccupation: Software Engineer\n"
        "1  Wages, salaries, tips ......... $412,890\n") * 3


def test_a_running_model_call_stops_within_seconds_and_the_model_still_works_afterwards():
    from tests.test_ai_type_check import _cand

    model = LlmAuditor()
    if not model.available:
        pytest.skip("no downloaded model on this machine")

    auditor.reset_stop()
    timer = threading.Timer(1.0, auditor.request_stop)  # lands while it is still READING the page
    started = time.monotonic()
    timer.start()
    try:
        with pytest.raises(auditor.AnalysisStopped):
            model.audit(PAGE, [])
    finally:
        timer.cancel()
    # A full call takes ~25 s on a slow core; the stop must not wait for it.
    assert time.monotonic() - started < 6, "Skip waited for the running call to finish"

    auditor.reset_stop()
    wrong = _cand("Acme Manufacturing Inc.", PiiType.PERSON, line_text="Employer: Acme Manufacturing Inc.")
    auditor.check_types([wrong], auditor=model)
    assert wrong.needs_review, "the model must work normally after an aborted call"
