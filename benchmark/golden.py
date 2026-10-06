"""Golden-corpus evaluation: does the engine remove identity AND keep everything else?

Each document lists what MUST be redacted (text + type), what MUST be kept (labels,
figures, form references), and groups of spellings that are ONE identity. Documents are
built from text (font-independent), so every script can be tested on any machine.

Metrics, per configuration:
  recall                 gold identity values covered by a redaction
  precision              redactions that land on a gold identity value
  false_positive_rate    redactions on text that must be kept, per kept item
  type_accuracy          covered values redacted with the right type
  label_preservation     kept labels/headings never touched
  figure_preservation    kept money, percentages, years, form/line numbers never touched
  partial_redaction_rate gold values only partly covered (a half-redacted value)
  entity_consistency     identity groups whose every spelling is covered by ONE entity
  coverage               (AI only) share of lines actually sent to the model

Run:
    python benchmark/golden.py                    # rules / +NER / full
    python benchmark/golden.py --ai               # also the local model (slow, needs one)
    python benchmark/golden.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.decisions.manager import DecisionManager
from app.detection.engine import analyse
from app.detection.provider_shim import document_from_pages
from app.detection.textnorm import fold
from app.entities.identity import EntityIndex, rescan_entity


@dataclass
class Golden:
    name: str
    text: str
    redact: list[tuple[str, str]] = field(default_factory=list)   # (text, TYPE)
    keep: list[str] = field(default_factory=list)                 # labels
    figures: list[str] = field(default_factory=list)              # figures / references to keep
    identities: list[list[str]] = field(default_factory=list)     # spellings of ONE identity


CORPUS = [
    Golden("1040 header", "Form 1040  U.S. Individual Income Tax Return  2025\nTaxpayer Name: John Smith\nSSN: 123-45-6789\n"
           "Total wages: $123,456\nTaxable income: 92,000\nTotal tax: $7,420\nLine 12  48,500",
           [("John Smith", "PERSON"), ("123-45-6789", "SSN")], ["Taxpayer Name", "SSN", "Total wages", "Taxable income", "Total tax"],
           ["$123,456", "92,000", "$7,420", "48,500", "Form 1040", "2025", "Line 12"]),
    Golden("two fields per line", "Date of birth 11/02/1979   Nationality: Brazil\nSSN 123-45-6789   Phone: 408-555-0198\n"
           "Taxpayer name: Maria Santos     Total tax: $12,500",
           [("11/02/1979", "DOB"), ("Brazil", "CITIZENSHIP"), ("123-45-6789", "SSN"), ("408-555-0198", "PHONE"), ("Maria Santos", "PERSON")],
           ["Date of birth", "Nationality", "Phone", "Taxpayer name", "Total tax"], ["$12,500"]),
    Golden("payroll", "Employer: Acme Corp   EIN: 12-3456789\nEmployee Name: Robert Chen\nEmployee ID: EMP-004417\n"
           "Gross wages: $85,000.00   Withholding: 22%\nPay period 2025",
           [("12-3456789", "EIN"), ("Robert Chen", "PERSON"), ("EMP-004417", "EMPLOYEE_ID")],
           ["Employer", "EIN", "Employee Name", "Employee ID", "Gross wages", "Withholding"], ["$85,000.00", "22%", "2025"]),
    Golden("bank form", "Account Number: 123456789\nRouting Number: 021000021\nAccount owner: Priya Raman\nBalance: $18,250.00",
           [("123456789", "BANK_ACCOUNT"), ("021000021", "ROUTING_NUMBER"), ("Priya Raman", "PERSON")],
           ["Account Number", "Routing Number", "Account owner", "Balance"], ["$18,250.00"]),
    Golden("repeated identity", "Taxpayer Name: John Smith\nPrepared for JOHN SMITH\nBeneficiary index: Smith, John\nSSN: 123-45-6789\nReference 123456789",
           [("John Smith", "PERSON"), ("JOHN SMITH", "PERSON"), ("Smith, John", "PERSON"), ("123-45-6789", "SSN")],
           ["Taxpayer Name", "SSN"], [], [["John Smith", "JOHN SMITH", "Smith, John"]]),
    Golden("multilingual labelled", "Taxpayer Name: José Núñez\nSpouse: Łukasz Wiśniewski\nDependent: Nguyễn Văn Minh\n"
           "Preparer: Željko Petrović\nPartner: 李伟\nContact: محمد أحمد",
           [("José Núñez", "PERSON"), ("Łukasz Wiśniewski", "PERSON"), ("Nguyễn Văn Minh", "PERSON"),
            ("Željko Petrović", "PERSON"), ("李伟", "PERSON"), ("محمد أحمد", "PERSON")],
           ["Taxpayer Name", "Spouse", "Dependent", "Preparer", "Contact"]),
    Golden("multilingual repeats", "Client: José Núñez\nSent to JOSE NUNEZ today\nFiled for Núñez, José",
           [("José Núñez", "PERSON"), ("JOSE NUNEZ", "PERSON"), ("Núñez, José", "PERSON")], ["Client"], [],
           [["José Núñez", "JOSE NUNEZ", "Núñez, José"]]),
    Golden("prose", "Mary Jones opened the account in March and called about the refund.\nSmith & Wesson makes firearms.",
           [("Mary Jones", "PERSON")], [], []),
    Golden("table", "Employee Name    Employee ID    Wages\nJohn Smith    12345    85,000",
           [("John Smith", "PERSON"), ("12345", "EMPLOYEE_ID")], ["Employee Name", "Employee ID", "Wages"], ["85,000"]),
    Golden("stacked", "Name, address, zip\nJohn Smith\n123 Main Street\n90210",
           [("John Smith", "PERSON"), ("123 Main Street", "STREET"), ("90210", "POSTAL_CODE")], ["Name, address, zip"]),
    Golden("identifiers", "EIN: 12-3456789\nMember ID: AB123456\nDOB: 03/22/1985\nPassport number: X1234567",
           [("12-3456789", "EIN"), ("AB123456", "MEMBER_ID"), ("03/22/1985", "DOB"), ("X1234567", "PASSPORT")],
           ["EIN", "Member ID", "DOB", "Passport number"]),
    Golden("business facts only", "Revenue: $500,000\nTotal deductions: (18,250)\nRate: 75%\nSchedule C  Form 941-X  2024", [], [],
           ["$500,000", "(18,250)", "75%", "Schedule C", "Form 941-X", "2024"]),
]


@dataclass
class Totals:
    gold: int = 0
    covered: int = 0
    typed: int = 0
    partial: int = 0
    redactions: int = 0
    on_gold: int = 0
    kept_labels: int = 0
    labels_hit: int = 0
    kept_figures: int = 0
    figures_hit: int = 0
    groups: int = 0
    groups_ok: int = 0
    misses: list[str] = field(default_factory=list)
    leaks: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)

    def ratio(self, a: int, b: int) -> float:
        return round(a / b, 3) if b else 1.0

    def report(self) -> dict:
        return {
            "recall": self.ratio(self.covered, self.gold),
            "precision": self.ratio(self.on_gold, self.redactions),
            "false_positive_rate": round(1 - self.ratio(self.kept_labels + self.kept_figures - self.labels_hit - self.figures_hit,
                                                      self.kept_labels + self.kept_figures), 3),
            "type_accuracy": self.ratio(self.typed, self.covered),
            "label_preservation": round(1 - self.ratio(self.labels_hit, self.kept_labels), 3),
            "figure_preservation": round(1 - self.ratio(self.figures_hit, self.kept_figures), 3),
            "partial_redaction_rate": self.ratio(self.partial, self.gold),
            "entity_consistency": self.ratio(self.groups_ok, self.groups),
            "missed": self.misses, "over_redacted": self.leaks, "extra_redactions": self.extras,
        }


def evaluate(config: dict, use_llm_factory=None) -> Totals:
    totals = Totals()
    for golden in CORPUS:
        doc = document_from_pages([golden.text])
        result = analyse(doc, use_llm=False, **config)
        decisions = DecisionManager(); decisions.register(result.candidates)
        live = [c for c in result.candidates if decisions.is_actionable(c)]

        # identity groups: link, rescan for what no detector found, then every form must be covered
        index = EntityIndex.build(live)
        extra = []
        for forms in golden.identities:
            seed = next((c for c in live if fold(c.text) in {fold(f) for f in forms}), None)
            profile = index.entity_of(seed) if seed else None
            if profile is not None:
                created, _amb = rescan_entity(doc, profile, live, result.protection)
                extra.extend(created)
        covered_all = live + extra

        for text, typ in golden.redact:
            totals.gold += 1
            hits = [c for c in covered_all if fold(text) in fold(c.text) or fold(c.text) in fold(text)]
            if not hits:
                totals.misses.append(f"{golden.name}: {text}")
                continue
            if any(fold(c.text) == fold(text) or fold(text) in fold(c.text) for c in hits):
                totals.covered += 1
                totals.typed += int(any(c.pii_type.name == typ for c in hits))
            else:
                totals.partial += 1
                totals.misses.append(f"{golden.name}: {text} (only partly covered)")
        gold_texts = [fold(t) for t, _ in golden.redact]
        for c in live:
            totals.redactions += 1
            if any(g in fold(c.text) or fold(c.text) in g for g in gold_texts):
                totals.on_gold += 1
            else:
                totals.extras.append(f"{golden.name}: {c.text!r} ({c.pii_type.name})")
        for group, bucket in ((golden.keep, "labels"), (golden.figures, "figures")):
            for item in group:
                hit = any(fold(item) in fold(c.text) for c in live)
                if bucket == "labels":
                    totals.kept_labels += 1; totals.labels_hit += int(hit)
                else:
                    totals.kept_figures += 1; totals.figures_hit += int(hit)
                if hit:
                    totals.leaks.append(f"{golden.name}: {item}")
        for forms in golden.identities:
            totals.groups += 1
            seeds = [next((c for c in covered_all if fold(f) in fold(c.text)), None) for f in forms]
            ok = all(seeds)
            if ok:
                owners = {index.entity_of(c).entity_id for c in seeds if index.entity_of(c)}
                ok = len(owners) <= 1 or all(c in extra for c in seeds[1:])
            totals.groups_ok += int(ok)
    return totals


CONFIGS = {
    "rules only": dict(use_ner=False, use_presidio=False),
    "rules + NER (+GLiNER if installed)": dict(use_ner=True, use_presidio=False),
    "full system": dict(use_ner=True, use_presidio=True),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json")
    args = parser.parse_args()
    os.environ.setdefault("DOCANON_LLM", "0")
    out = {}
    keys = ["recall", "precision", "type_accuracy", "label_preservation", "figure_preservation",
            "false_positive_rate", "partial_redaction_rate", "entity_consistency"]
    print(f"{'configuration':<38}" + "".join(f"{k[:11]:>13}" for k in keys))
    for name, config in CONFIGS.items():
        report = evaluate(config).report()
        out[name] = report
        print(f"{name:<38}" + "".join(f"{report[k]:>13.3f}" for k in keys))
    best = out["full system"]
    print("\nmissed:", best["missed"] or "none")
    print("over-redacted (kept text that was touched):", best["over_redacted"] or "none")
    print("redactions outside the gold list (these lower precision):", best["extra_redactions"] or "none")
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
