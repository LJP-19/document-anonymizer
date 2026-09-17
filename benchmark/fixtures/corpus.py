"""The golden benchmark/regression corpus (spec sections 23-24).

Every fixture here traces to something real: either a category the spec asks
for, or a specific bug this project actually shipped and fixed. Each is a
permanent regression test in benchmark form - if a category regresses, the
benchmark's precision/recall for it drops, which is a much more informative
signal than a single pass/fail test.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from ..run_benchmark import Fixture, GoldValue


def _lines(path: Path, lines: list[str], font: str = "Helvetica", size: int = 10,
           leading: float = 14.0) -> str:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont(font, size)
    y = 730.0
    for line in lines:
        c.drawString(72, y, line)
        y -= leading
    c.save()
    return str(path)


def _pages(path: Path, pages: list[list[str]], font: str = "Helvetica", size: int = 9,
            leading: float = 14.0) -> str:
    c = canvas.Canvas(str(path), pagesize=letter)
    for lines in pages:
        c.setFont(font, size)
        y = 730.0
        for line in lines:
            c.drawString(72, y, line)
            y -= leading
        c.showPage()
    c.save()
    return str(path)


ALL_FIXTURES: list[Fixture] = []


# --- A. true PII, plain shapes ---------------------------------------------

ALL_FIXTURES.append(Fixture(
    name="plain_form_fields",
    category="true_pii",
    build=lambda p: _lines(p, [
        "Taxpayer name", "John Smith",
        "Social security number", "123-45-6789",
        "Email address", "john.smith@example.com",
        "Phone number", "(555) 123-4567",
        "1  Wages, salaries, tips  ......  $412,890",
    ]),
    gold=[
        GoldValue("John Smith", "PERSON"),
        GoldValue("123-45-6789", "SSN"),
        GoldValue("john.smith@example.com", "EMAIL"),
        GoldValue("(555) 123-4567", "PHONE"),
        GoldValue("$412,890", "", should_detect=False),
    ],
))

ALL_FIXTURES.append(Fixture(
    name="ssn_no_dashes",
    category="difficult_formatting",
    build=lambda p: _lines(p, ["Social security number", "123456789"]),
    gold=[GoldValue("123456789", "SSN")],
))

ALL_FIXTURES.append(Fixture(
    name="all_caps_mailing_block",
    category="difficult_formatting",
    build=lambda p: _lines(p, [
        "MEGHAN SHAFFER", "TIFFANY SHAFFER",
        "4049 MICHAEL CMN", "CINCINNATI OH 45280-2502",
    ], size=9, leading=13),
    gold=[
        GoldValue("MEGHAN SHAFFER", "PERSON"),
        GoldValue("TIFFANY SHAFFER", "PERSON"),
        GoldValue("4049 MICHAEL CMN", "STREET"),
        GoldValue("CINCINNATI OH 45280-2502", "ADDRESS"),
    ],
))


# --- B. true non-PII / adversarial false positives -------------------------

ALL_FIXTURES.append(Fixture(
    name="tax_form_headings",
    category="adversarial_false_positive",
    build=lambda p: _lines(p, [
        "Nondeductible IRAs", "Roth IRA Contribution", "Traditional IRA Rollover",
        "Estimated Payments to Make for Next Year",
        "Internal Revenue Service", "Form 1040-ES", "Schedule C",
    ]),
    gold=[
        GoldValue("Nondeductible IRAs", "", should_detect=False),
        GoldValue("Roth IRA Contribution", "", should_detect=False),
        GoldValue("Traditional IRA Rollover", "", should_detect=False),
        GoldValue("Estimated Payments to Make for Next Year", "", should_detect=False),
        GoldValue("Internal Revenue Service", "", should_detect=False),
        GoldValue("Schedule C", "", should_detect=False),
    ],
))

ALL_FIXTURES.append(Fixture(
    name="english_phrases_that_look_like_names",
    category="adversarial_false_positive",
    build=lambda p: _lines(p, [
        "Need to Keep", "Please Review", "Estimated Payments",
        "Federal Tax Return", "Make for Next", "Action Required",
        "Check if you, or your spouse if filing jointly, want $3 to go to this fund",
    ]),
    gold=[
        GoldValue("Need to Keep", "", should_detect=False),
        GoldValue("Please Review", "", should_detect=False),
        GoldValue("Action Required", "", should_detect=False),
    ],
))

ALL_FIXTURES.append(Fixture(
    name="voucher_boilerplate",
    category="adversarial_false_positive",
    build=lambda p: _lines(p, [
        "Detach Here", "Mail With Your Check or Money Order",
        "Do not staple or attach your payment to this voucher.",
        "Cut along the dotted line",
    ]),
    gold=[
        GoldValue("Detach Here", "", should_detect=False),
        GoldValue("Mail With Your Check or Money Order", "", should_detect=False),
    ],
))

ALL_FIXTURES.append(Fixture(
    name="bare_role_label_is_not_a_value",
    category="adversarial_false_positive",
    build=lambda p: _lines(p, ["Policyholder", "Diana Whitfield"]),
    gold=[
        GoldValue("Policyholder", "", should_detect=False),
        GoldValue("Diana Whitfield", "PERSON"),
    ],
))

ALL_FIXTURES.append(Fixture(
    name="financial_facts_preserved",
    category="table",
    build=lambda p: _lines(p, [
        "1  Wages, salaries, tips  ......  $412,890",
        "Refund due 4,820.00", "Reduction percentage 25%",
        "Taxable income: $217,632.00",
    ]),
    gold=[
        GoldValue("$412,890", "", should_detect=False),
        GoldValue("4,820.00", "", should_detect=False),
        GoldValue("$217,632.00", "", should_detect=False),
    ],
))


# --- C. joint / conjugal names ----------------------------------------------

ALL_FIXTURES.append(Fixture(
    name="joint_name_ampersand",
    category="joint_names",
    build=lambda p: _lines(p, ["Taxpayer name", "Mark & Jane Lang", "1  Wages ...... $412,890"]),
    gold=[GoldValue("Mark", "PERSON"), GoldValue("Jane Lang", "PERSON")],
))

ALL_FIXTURES.append(Fixture(
    name="joint_name_no_label_nearby",
    category="joint_names",
    build=lambda p: _lines(p, [
        "Signature block: reviewed and signed by", "Mark & Jane Lang",
        "1  Wages ...... $412,890",
    ]),
    gold=[GoldValue("Mark", "PERSON"), GoldValue("Jane Lang", "PERSON")],
))

ALL_FIXTURES.append(Fixture(
    name="two_stacked_full_names_share_surname",
    category="joint_names",
    build=lambda p: _lines(p, ["Meghan Shaffer", "Tiffany Shaffer"], size=9, leading=13),
    gold=[GoldValue("Meghan Shaffer", "PERSON"), GoldValue("Tiffany Shaffer", "PERSON")],
))


# --- D. paragraph prose -----------------------------------------------------

ALL_FIXTURES.append(Fixture(
    name="paragraph_pii",
    category="paragraph_prose",
    build=lambda p: _lines(p, [
        "John Smith submitted the report to ABC Company.",
        "Please contact heat@example.com.",
        "His phone number is (445) 346-4854.",
        "His SSN is 131-76-2692.",
        "The total amount owed is $18,450.00.",
    ]),
    gold=[
        GoldValue("John Smith", "PERSON"),
        GoldValue("heat@example.com", "EMAIL"),
        GoldValue("(445) 346-4854", "PHONE"),
        GoldValue("131-76-2692", "SSN"),
        GoldValue("$18,450.00", "", should_detect=False),
        GoldValue("ABC Company", "", should_detect=False),
    ],
))

ALL_FIXTURES.append(Fixture(
    name="name_split_across_a_line_break",
    category="paragraph_prose",
    build=lambda p: _lines(p, [
        "We spoke at length with our client, Marisol",
        "Etxeberria, about the pending refund status.",
    ]),
    gold=[GoldValue("Marisol", "PERSON"), GoldValue("Etxeberria", "PERSON")],
))


# --- E. table / column semantics --------------------------------------------

ALL_FIXTURES.append(Fixture(
    name="table_column_typing",
    category="table",
    build=lambda p: _lines(p, [
        "Employee name          SSN               Salary",
        "John Smith             123-45-6789       $85,000",
        "Jane Doe                987-65-4321       $92,000",
    ]),
    gold=[
        GoldValue("John Smith", "PERSON"),
        GoldValue("Jane Doe", "PERSON"),
        GoldValue("123-45-6789", "SSN"),
        GoldValue("987-65-4321", "SSN"),
        GoldValue("$85,000", "", should_detect=False),
        GoldValue("$92,000", "", should_detect=False),
    ],
))


# --- F. multi-page propagation ----------------------------------------------

def _propagation_pages(path: Path, pages: int = 5) -> str:
    return _pages(path, [
        ["Taxpayer name", "Mark & Macey Lang",
         f"Preparer reviewed for Lang, page {i + 1} of {pages}"]
        for i in range(pages)
    ])


ALL_FIXTURES.append(Fixture(
    name="multi_page_propagation",
    category="propagation",
    build=lambda p: _propagation_pages(p, pages=5),
    gold=[GoldValue("Mark", "PERSON"), GoldValue("Macey Lang", "PERSON")],
))


# --- G. repeated identifiers -------------------------------------------------

ALL_FIXTURES.append(Fixture(
    name="repeated_address_no_label",
    category="propagation",
    build=lambda p: _pages(p, [
        ["123 Main Street", "San Jose, CA 95129"] for _ in range(4)
    ]),
    gold=[GoldValue("123 Main Street", "STREET"), GoldValue("San Jose, CA 95129", "ADDRESS")],
))


# --- H. structured identifiers, newly covered types -------------------------

ALL_FIXTURES.append(Fixture(
    name="new_identifier_types",
    category="deterministic",
    build=lambda p: _lines(p, [
        "Account number", "4429981123",
        "Fax: 555-987-6543",
        "Follow me @traveler_jane on Instagram",
    ]),
    gold=[
        GoldValue("555-987-6543", "FAX"),
        GoldValue("@traveler_jane", "SOCIAL_HANDLE"),
    ],
))


# --- I. business name consistency -------------------------------------------

ALL_FIXTURES.append(Fixture(
    name="business_name_label",
    category="table",
    build=lambda p: _lines(p, ["Business name", "Acme Holdings LLC"]),
    gold=[GoldValue("Acme Holdings LLC", "ORG_PRIVATE")],
))
