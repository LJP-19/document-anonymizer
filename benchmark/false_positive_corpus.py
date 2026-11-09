"""Synthetic FALSE-POSITIVE benchmark: documents that are almost entirely NOT PII.

Every value here is invented. The point is the opposite of the forms corpus: a
detector is scored on what it leaves alone. Each document mixes, on purpose,
the kinds of text that were wrongly redacted on real returns:

  * headings, sidebar titles, instructions, checkbox options, table headers
  * deadlines, due dates and "as of" dates that are not anybody's birthday
  * money amounts, percentages, line numbers, form/OMB/catalog numbers
  * public agency names and mailing addresses (IRS, a state tax board)
  * occupations, job titles, employer and organization names
  * ordinary words that are also first names ("will", "Grant", "Bill", "Mark")
  * a filing-status code ("MFJ") and bare reference numbers
  * text an EARLIER RUN of this application already replaced (pure red), sitting
    where form wording used to be - it must be left exactly as it is

A handful of labelled PII values (category person/ssn/street/...) stay in, so
the benchmark also proves real PII is still replaced.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from benchmark.forms_corpus import BLACK, FormDoc

RED = (1.0, 0.0, 0.0)   # the application's replacement colour


def _inherited(d: FormDoc, page: int, x: float, y: float, text: str, size: float = 8.0):
    """Text an earlier run already replaced. Not PII: a synthetic name."""
    return d.text(page, x, y, text, size=size, color=RED, category="inherited_pseudonym")


def build_filing_letter(path: Path) -> FormDoc:
    d = FormDoc("fp_filing_letter")
    p = d.page()
    d.text(p, 36, 40, "Electronic Filing Instructions for your 2024 Federal Tax Return", size=12, category="heading")
    d.text(p, 36, 54, "Important: Your taxes are not finished until all required steps are completed.",
           size=8, category="instruction")
    # The client block: real PII, labelled by position under a "Prepared for" caption.
    d.label(p, 36, 74, "Prepared for:")
    d.value(p, 36, 88, "Torvin Quell", "person", size=9)
    d.value(p, 36, 100, "2716 Larkspur Bend", "street", size=9)
    d.value(p, 36, 112, "Ashford Springs, OR 97401", "city_state_zip", size=9)
    # Sidebar titles - name-shaped, Title Case, alone on their lines.
    for i, title in enumerate(["Balance Due", "Need to Print", "Federal Tax Summary", "Payments to Make",
                               "for Next Year"]):
        d.text(p, 36, 150 + 40 * i, title, size=9, font="hebo", category="sidebar")
    body = [
        "Your federal tax return (Form 1040) shows a balance due of $3,412.00.",
        "You have elected to pay your balance due by Direct Debit on 04/15/2025.",
        "Mark the box on line 6 if you will file a joint return. Grant programs are not taxable",
        "income. Bill payments made after the due date may incur a penalty.",
        "Estimated payments for 2025 are shown below. Do not mail these vouchers with your return.",
    ]
    for i, line in enumerate(body):
        d.text(p, 160, 150 + 14 * i, line, size=8.5, font="cour", color=BLACK, category="instruction")
    for x, hdr in zip((180, 290, 420), ("Voucher Number", "Due Date", "Amount")):
        d.text(p, x, 240, hdr, size=8.5, font="cour", color=BLACK, category="table_header")
    for r, (n, due, amt) in enumerate([("1", "04/15/2025", "$1,250.00"), ("2", "06/16/2025", "$1,250.00"),
                                       ("3", "09/15/2025", "$1,250.00"), ("4", "01/15/2026", "$1,250.00")]):
        y = 254 + 12 * r
        d.text(p, 200, y, n, size=8.5, font="cour", color=BLACK, category="table_value")
        d.text(p, 290, y, due, size=8.5, font="cour", color=BLACK, category="deadline")
        d.text(p, 420, y, amt, size=8.5, font="cour", color=BLACK, category="money")
    d.text(p, 160, 320, "Mail payments to:", size=8.5, font="cour", color=BLACK, category="instruction")
    for i, line in enumerate(["Internal Revenue Service", "P.O. Box 931000", "Louisville, KY 40293-1000"]):
        d.text(p, 160, 334 + 12 * i, line, size=8.5, font="cour", color=BLACK, category="agency_address")
    d.text(p, 160, 380, "California residents also mail Form 540-ES to:", size=8.5, font="cour", color=BLACK,
           category="instruction")
    for i, line in enumerate(["Franchise Tax Board", "PO Box 942867", "Sacramento CA 94267-0001"]):
        d.text(p, 160, 394 + 12 * i, line, size=8.5, font="cour", color=BLACK, category="agency_address")
    d.text(p, 280, 760, "Page 1 of 2", size=8, category="page_number")
    return d


def build_form_page(path: Path) -> FormDoc:
    d = FormDoc("fp_form_page")
    p = d.page()
    d.text(p, 36, 40, "Form 1040  U.S. Individual Income Tax Return  2024", size=11, category="heading")
    d.text(p, 380, 40, "OMB No. 1545-0074", size=7, category="form_number")
    d.text(p, 470, 40, "IRS Use Only", size=7, category="heading")
    # An earlier run wrote a pseudonym where "Do not write or staple" used to be.
    _inherited(d, p, 470, 50, "Morgan Bates", size=7)
    d.text(p, 520, 50, "in this space.", size=7, category="instruction")
    d.label(p, 36, 70, "Filing Status")
    for i, opt in enumerate(["Single", "Married filing jointly", "Married filing separately (MFS)",
                             "Head of household (HOH)", "Qualifying surviving spouse (QSS)"]):
        d.text(p, 110 + 95 * i if i < 3 else 110 + 95 * (i - 3), 70 if i < 3 else 82, opt, size=7,
               category="checkbox_option")
    d.label(p, 36, 104, "Your first name and middle initial")
    d.label(p, 250, 104, "Last name")
    d.label(p, 430, 104, "Your social security number")
    d.value(p, 40, 116, "Torvin", "person")
    d.value(p, 254, 116, "Quell", "person")
    d.value(p, 434, 116, "612-40-7731", "ssn")
    d.label(p, 36, 140, "Your occupation")
    d.value(p, 40, 152, "Software Engineer", "occupation", pii=False)
    d.label(p, 250, 140, "Employer")
    d.value(p, 254, 152, "Lakeview Unified School District", "organization", pii=False)
    d.label(p, 36, 176, "Digital Assets")
    d.text(p, 110, 176, "At any time during 2024, did you receive, sell, or exchange a digital asset?",
           size=7, category="instruction")
    d.label(p, 36, 200, "Presidential Election Campaign")
    _inherited(d, p, 200, 200, "Kimberly Johnson", size=7)
    d.label(p, 36, 224, "Filing status code")
    d.value(p, 140, 224, "MFJ", "filing_status_code", pii=False)
    rows = [("1a", "Total amount from Form(s) W-2, box 1 (see instructions)", "84,210."),
            ("2b", "Taxable interest", "312."),
            ("12", "Standard deduction or itemized deductions (from Schedule A)", "29,200."),
            ("15", "Taxable income. Subtract line 14 from line 11", "55,322."),
            ("24", "Total tax. Add lines 22 and 23", "6,148."),
            ("34", "Refund. Amount you overpaid", "1,102.")]
    for r, (ln, text, amt) in enumerate(rows):
        y = 250 + 14 * r
        d.text(p, 36, y, ln, size=7, font="hebo", category="line_number")
        d.text(p, 60, y, text, size=7, category="form_text")
        d.value(p, 500, y, amt, "money", pii=False)
    # An earlier run replaced "Roth IRA" in this instruction with a name.
    d.text(p, 36, 350, "Complete this part if you converted part or all of your traditional IRA to a", size=7,
           category="instruction")
    _inherited(d, p, 300, 350, "Keith Lauren", size=7)
    d.text(p, 350, 350, ". See instructions.", size=7, category="instruction")
    d.text(p, 36, 380, "Control number", size=7, category="form_text")
    d.value(p, 36, 392, "573918204 SS EXT 01 0 240115 012", "reference_number", pii=False, size=8)
    d.text(p, 36, 760, "Cat. No. 11320B", size=7, category="form_number")
    d.text(p, 500, 760, "Form 1040 (2024)", size=7, category="form_number")
    return d


def build_worksheet(path: Path) -> FormDoc:
    """Tables, as-of dates, percentages and words that are also names."""
    d = FormDoc("fp_worksheet")
    p = d.page()
    d.text(p, 36, 40, "Qualified Dividends and Capital Gain Tax Worksheet", size=11, category="heading")
    d.text(p, 36, 56, "Keep for Your Records", size=8, category="heading")
    for x, hdr in zip((36, 260, 360, 470), ("Description", "Date acquired", "Rate", "Amount")):
        d.text(p, x, 80, hdr, size=8, category="table_header")
    rows = [("Vanguard Total Stock Market Index", "as of 12/31/2024", "15%", "$4,812.33"),
            ("Fidelity Contrafund", "as of 12/31/2024", "15%", "$1,208.10"),
            ("Will County bond interest", "as of 06/30/2024", "0%", "$96.40"),
            ("Grant Street Partners distribution", "as of 03/31/2024", "20%", "$2,500.00")]
    for r, (desc, when, rate, amt) in enumerate(rows):
        y = 96 + 14 * r
        d.text(p, 36, y, desc, size=8, font="cour", color=BLACK, category="organization")
        d.text(p, 260, y, when, size=8, font="cour", color=BLACK, category="date_not_personal")
        d.text(p, 360, y, rate, size=8, font="cour", color=BLACK, category="percent")
        d.text(p, 470, y, amt, size=8, font="cour", color=BLACK, category="money")
    lines = [
        "Enter the amount from Form 1040, line 15. If you will not file Schedule D, skip lines 3 through 5.",
        "Rose Bowl tickets, Hunter boots and Bill of Sale fees are not deductible business expenses.",
        "Payments are due by April 15. A taxpayer who files late may owe a penalty of 5% per month.",
    ]
    for i, line in enumerate(lines):
        d.text(p, 36, 170 + 12 * i, line, size=8, category="instruction")
    d.text(p, 36, 220, "Preparer use only", size=8, category="heading")
    d.label(p, 36, 236, "Firm's name")
    # Owner, 0.42.5: a firm/employer name under its caption IS identity (replaced).
    d.value(p, 120, 236, "Northgate Tax Partners LLC", "organization", pii=True, size=9)
    d.label(p, 300, 236, "Spouse's name")
    d.value(p, 380, 236, "Bela Quell", "person", size=9)
    d.text(p, 280, 760, "Page 2 of 2", size=8, category="page_number")
    return d


CORPUS: dict[str, Callable[[Path], FormDoc]] = {
    "fp_filing_letter": build_filing_letter,
    "fp_form_page": build_form_page,
    "fp_worksheet": build_worksheet,
}


def build_all(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, builder in CORPUS.items():
        doc = builder(directory / f"{name}.pdf")
        path = doc.save(directory / f"{name}.pdf")
        out[name] = (path, doc.items)
    return out
