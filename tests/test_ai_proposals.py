"""The AI as a real proposal layer: typed, located in real text, gated, proportionate."""

from __future__ import annotations

from unittest.mock import patch

from app.decisions.manager import DecisionManager, DecisionState
from app.detection import auditor as A
from app.detection.auditor import AuditFinding
from app.detection.engine import analyse
from app.detection.provider_shim import document_from_pages
from app.detection.types import PiiType, Source


class FakeAuditor:
    def __init__(self, findings=(), wrong=()):
        self.findings, self.wrong, self.calls = list(findings), list(wrong), []

    def load(self):
        pass

    def audit(self, text, detected):
        self.calls.append((text, list(detected)))
        return list(self.findings), list(self.wrong)


def run(page_text, findings=(), wrong=(), checked=None):
    """Full analyse() with the model phase on, driven by a fake model."""
    fake = FakeAuditor(findings, wrong)
    real = A.audit_document
    doc = document_from_pages(page_text if isinstance(page_text, list) else [page_text])
    with patch("app.detection.engine.audit_document",
               lambda d, c, labels=None: real(d, c, auditor=fake, labels=labels)), \
         patch("app.detection.engine.adjudicate_document", return_value=(set(), [])), \
         patch("app.detection.engine.check_types", return_value=[]):
        result = analyse(doc, use_llm=True)
    decisions = DecisionManager(); decisions.register(result.candidates)
    return doc, result, decisions, fake


def by_text(result, text):
    return [c for c in result.candidates if c.text == text]


def test_a_typed_finding_becomes_a_real_candidate_that_no_detector_recognised():
    doc = document_from_pages(["Notes\nShe is a national of Espana since 2009"])
    fake = FakeAuditor([AuditFinding("Espana", "CITIZENSHIP", "nationality")])
    added, _wrong, _warnings = A.audit_document(doc, [], auditor=fake)
    (c,) = added
    assert c.pii_type is PiiType.CITIZENSHIP and c.source is Source.AUDIT and c.needs_review
    assert any("AI:" in e.detail for e in c.evidence)
    dm = DecisionManager(); dm.register(added)
    assert dm.state(c) is DecisionState.ACCEPTED, "a confident typed AI finding is applied and flagged"


def test_a_type_disagreement_with_a_detector_is_surfaced_not_silently_resolved():
    _d, result, _dm, _f = run("Notes\nShe is a national of Espana since 2009",
                              [AuditFinding("Espana", "CITIZENSHIP", "nationality")])
    spans = by_text(result, "Espana")
    assert len(spans) == 1, "no duplicate candidate on the same span"
    if spans[0].pii_type is not PiiType.CITIZENSHIP:
        assert spans[0].needs_review and "AI reads this as CITIZENSHIP" in spans[0].review_reason


def test_an_ai_finding_types_an_untyped_candidate_instead_of_being_skipped_for_overlap():
    from app.detection.types import Candidate, Evidence

    doc = document_from_pages(["Home country: Espana"])
    line = doc.pages[0].lines[0]
    start = line.text.index("Espana")
    untyped = Candidate(PiiType.UNCLASSIFIED_GROUP_VALUE, "Espana", 0, line.rect_for(start, start + 6), line,
                        start, start + 6, 0.7, Source.GROUP, [Evidence(Source.GROUP, "value under an unknown label", 0.7)])
    fake = FakeAuditor([AuditFinding("Espana", "CITIZENSHIP", "nationality")])
    added, _wrong, _warn = A.audit_document(doc, [untyped], auditor=fake)
    assert added == [], "no duplicate candidate on the same span"
    assert untyped.pii_type is PiiType.CITIZENSHIP and untyped.needs_review
    assert any("AI:" in e.detail for e in untyped.evidence)


def test_identifier_rules_no_longer_accept_ordinary_words():
    """"passport" itself and "Espana" were both redacted as passport numbers."""
    from tests.helpers_text import redacted

    assert redacted("She held a passport from Espana") == []
    assert ("X1234567", "PASSPORT") in redacted("Passport number: X1234567")


def test_an_untyped_finding_waits_for_a_human_instead_of_being_guessed():
    _d, result, dm, _f = run("Reference: zx9 qq\nNotes", [AuditFinding("zx9 qq", "other", "")])
    (c,) = by_text(result, "zx9 qq")
    assert c.pii_type is PiiType.UNCLASSIFIED_GROUP_VALUE and dm.state(c) is DecisionState.SKIPPED


def test_an_uncertain_finding_is_kept_for_review_but_not_applied():
    _d, result, dm, _f = run("known as tito bong", [AuditFinding("tito bong", "PERSON", "maybe", uncertain=True)])
    (c,) = by_text(result, "tito bong")
    assert c.needs_review and dm.state(c) is DecisionState.SKIPPED


def test_a_finding_in_a_different_spelling_is_located_in_the_original_characters():
    _d, result, _dm, _f = run("Beneficiary: José Núñez", [AuditFinding("JOSE NUNEZ", "PERSON", "name")])
    assert any(c.text == "José Núñez" for c in result.candidates)


def test_a_finding_that_includes_its_label_is_clipped_back_to_the_value():
    _d, result, _dm, _f = run("Citizenship: Espana", [AuditFinding("Citizenship: Espana", "CITIZENSHIP", "x")])
    for c in result.candidates:
        assert "Citizenship" not in c.text, c.text
    assert any(c.text == "Espana" for c in result.candidates)


def test_the_model_cannot_make_a_figure_or_a_label_redactable():
    _d, result, _dm, _f = run("Total wages: 85,000\nTax year 2025\nCitizenship: Espana", [
        AuditFinding("85,000", "DOB", "date"), AuditFinding("2025", "DOB", "year"),
        AuditFinding("Total wages", "PERSON", "name"), AuditFinding("Citizenship", "PERSON", "name"),
    ])
    texts = [c.text for c in result.candidates]
    assert not any(t in texts for t in ("85,000", "2025", "Total wages", "Citizenship"))


def test_a_strong_candidate_survives_an_ai_objection_but_is_flagged():
    _d, result, dm, _f = run("SSN: 123-45-6789", wrong=["123-45-6789"])
    (c,) = by_text(result, "123-45-6789")
    assert dm.state(c) is DecisionState.ACCEPTED and c.needs_review and "AI thinks" in c.review_reason


def test_every_line_of_a_long_page_reaches_the_model():
    lines = [f"Row {i} note {'x' * 60}" for i in range(150)]
    lines[149] = "Place of Birth: Zamboanga"
    fake = FakeAuditor()
    doc = document_from_pages(["\n".join(lines)])
    A.audit_document(doc, [], auditor=fake)
    seen = "\n".join(text for text, _ in fake.calls)
    assert all(line in seen for line in lines), "a line never reached the model"
    assert all(len(text) <= A.MAX_CHARS_PER_CALL + 200 for text, _ in fake.calls)
    assert len(fake.calls) > 1


def test_the_real_audit_method_never_truncates_long_text():
    prompts = []

    def fake_chat(self, **kwargs):
        prompts.append(kwargs["messages"][1]["content"])
        return {"choices": [{"message": {"content": '{"missed": [], "uncertain": [], "wrong": []}'}}]}

    text = "\n".join(f"line {i} " + "w" * 55 for i in range(200))
    with patch.object(A.LlmAuditor, "load", lambda self: None), patch.object(A, "_chat", fake_chat), \
         patch.object(A, "_grammar", lambda schema: None):
        A.LlmAuditor().audit(text, [])
    joined = "\n".join(prompts)
    assert len(prompts) > 1 and all(f"line {i} " in joined for i in range(200))


def test_split_text_overlaps_and_loses_nothing():
    text = "\n".join(f"line {i}" for i in range(100))
    chunks = A.split_text(text, 120, overlap=2)
    assert all(f"line {i}" in "\n".join(chunks) for i in range(100))
    assert chunks[0].split("\n")[-2:] == chunks[1].split("\n")[:2]


def test_each_adjudicated_value_is_judged_with_its_own_lines_not_the_page_head():
    lines = [f"Filler {i} " + "z" * 70 for i in range(120)]
    lines[110] = "Beneficiary: Zoltan Varga"
    doc = document_from_pages(["\n".join(lines)])
    result = analyse(doc, use_llm=False)
    target = [c for c in result.candidates if "Zoltan" in c.text]
    assert target, "fixture should detect the name"
    batches = A._adjudication_batches(doc.pages[0], target)
    assert batches and all("Beneficiary: Zoltan Varga" in context for _v, context in batches)
    assert all(len(context) <= A.MAX_CHARS_PER_CALL for _v, context in batches)


def test_the_parser_reads_typed_uncertain_and_wrong():
    raw = ('{"missed":[{"text":"Espana","type":"CITIZENSHIP","reason":"field"}],'
           '"uncertain":[{"text":"Tito","type":"PERSON","reason":"maybe"}],'
           '"wrong":[{"text":"Taxpayer","reason":"label"}]}')
    found, wrong = A._parse(raw)
    assert [(f.text, f.category, f.uncertain) for f in found] == [
        ("Espana", "CITIZENSHIP", False), ("Tito", "PERSON", True)]
    assert wrong == ["Taxpayer"]


def test_model_type_names_resolve_to_the_taxonomy():
    assert A.resolve_ai_type("DOB") is PiiType.DOB
    assert A.resolve_ai_type("birthplace") is PiiType.BIRTHPLACE
    assert A.resolve_ai_type("something invented") is PiiType.UNCLASSIFIED_GROUP_VALUE


def test_the_schema_forces_a_taxonomy_type():
    import json

    schema = json.loads(A.AUDIT_SCHEMA)
    enum = schema["properties"]["missed"]["items"]["properties"]["type"]["enum"]
    assert "CITIZENSHIP" in enum and "UNCLASSIFIED_GROUP_VALUE" not in enum


def test_output_cut_off_at_the_token_cap_still_yields_its_complete_items():
    """Discarding the whole response over one unfinished item made a useful answer
    look like "the model found nothing"."""
    cut = ('{"missed": [{"text": "11/02/1979", "type": "DOB"}, '
           '{"text": "Brazil", "type": "CITIZENSHIP"}, {"text": "Rec')
    found, wrong = A._parse(cut)
    assert [(f.text, f.category) for f in found] == [("11/02/1979", "DOB"), ("Brazil", "CITIZENSHIP")]


def test_the_schema_asks_for_no_justification_text():
    import json

    item = json.loads(A.AUDIT_SCHEMA)["properties"]["missed"]["items"]
    assert "reason" not in item["properties"] and item["required"] == ["text", "type"]
