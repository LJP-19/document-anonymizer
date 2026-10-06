"""Font-independent test documents: text in, real detection out.

Built on provider_shim.document_from_pages, so a test can use ANY script (Chinese,
Arabic, Vietnamese ...) without the PDF font having to draw it.
"""

from __future__ import annotations

from app.decisions.manager import DecisionManager
from app.detection.engine import analyse
from app.detection.provider_shim import document_from_pages


def analysed(*pages: str):
    doc = document_from_pages(list(pages))
    result = analyse(doc, use_llm=False)
    decisions = DecisionManager()
    decisions.register(result.candidates)
    return doc, result, decisions


def redacted(*pages: str) -> list[tuple[str, str]]:
    """(text, type) of everything that would actually be redacted."""
    _doc, result, decisions = analysed(*pages)
    return [(c.text, c.pii_type.name) for c in result.candidates if decisions.is_actionable(c)]
