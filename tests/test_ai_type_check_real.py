"""Runs the type check against a REAL downloaded model. Skipped wherever no model
is available (CI downloads none); runs on a machine that has one.

Not isolated from the home folder on purpose: it needs the model the user chose.
"""

from __future__ import annotations

import pytest

from app.detection.auditor import LlmAuditor, check_types
from app.detection.types import PiiType

pytestmark = pytest.mark.slow


def test_a_real_model_flags_wrong_types_and_leaves_right_ones_alone():
    from tests.test_ai_type_check import _cand

    auditor = LlmAuditor()
    if not auditor.available:
        pytest.skip("no downloaded model on this machine")

    wrong = [
        _cand("Acme Manufacturing Inc.", PiiType.PERSON, line_text="Employer: Acme Manufacturing Inc.", line_no=0),
        _cand("John Q Public", PiiType.ORG_PRIVATE, line_text="Taxpayer name: John Q Public", line_no=1),
        _cand("Springfield, IL", PiiType.PERSON, line_text="City: Springfield, IL", line_no=2),
    ]
    right = [
        _cand("Jane A Public", PiiType.PERSON, line_text="Spouse: Jane A Public", line_no=3),
        _cand("Bright Path Consulting LLC", PiiType.ORG_PRIVATE, line_text="Firm: Bright Path Consulting LLC", line_no=4),
        _cand("100 Maple Street, Springfield, IL 62704", PiiType.ADDRESS,
              line_text="Address: 100 Maple Street, Springfield, IL 62704", line_no=5),
    ]
    check_types(wrong + right, auditor=auditor)

    assert [c.text for c in wrong if not c.needs_review] == [], "a wrong type went unflagged"
    assert [c.text for c in right if c.needs_review] == [], "a right type was flagged"
