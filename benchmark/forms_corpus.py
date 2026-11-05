"""Synthetic organizational-form corpus with GEOMETRIC ground truth.

Every value is invented. The shapes are not: each fixture reproduces a failure
observed on a real, multi-page, software-prepared individual tax return (the
diagnostic samples are never committed - see CLAUDE.md rule 9). What carried
over is only the SHAPE of each failure:

  - joint name lines ("A & B Surname") on software summary pages
  - an ALL-CAPS street line with an uncommon USPS suffix ("... CMN")
  - first name and last name in SEPARATE boxes under separate labels
  - a dependents table: first name | last name | SSN | relationship
  - "Name(s) shown on return" on every schedule, value on the next line
  - occupation, phone and preparer text in a signature block
  - dates of birth on a state page, under column labels
  - a payment-voucher scanline carrying the surname's 4-letter name control
  - sidebar headings, checkbox captions and "IRS Use Only" banners that a
    name model mistook for people
  - prose mentioning the client as "Mr. <Surname>" and by first name alone,
    beside an unrelated word that shares the first name

Form text is drawn in the template colour of a real government form; filled
values in black Courier, as tax software prints them. Each PII item records the
exact rectangle it was drawn in, so scoring is by geometry, not by guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pymupdf

TEMPLATE = (0x23 / 255, 0x1F / 255, 0x20 / 255)
BLACK = (0, 0, 0)


@dataclass
class Item:
    page: int
    text: str
    rect: tuple[float, float, float, float]
    pii: bool
    category: str


@dataclass
class FormDoc:
    name: str
    items: list[Item] = field(default_factory=list)
    _doc: pymupdf.Document = field(default_factory=pymupdf.open)

    def page(self, width: float = 612, height: float = 792) -> int:
        self._doc.new_page(width=width, height=height)
        return self._doc.page_count - 1

    def text(self, page: int, x: float, y: float, text: str, *, size: float = 8.0,
             font: str = "helv", color=TEMPLATE, pii: bool = False, category: str = "form_text") -> Item:
        p = self._doc[page]
        p.insert_text((x, y), text, fontsize=size, fontname=font, color=color)
        width = pymupdf.get_text_length(text, fontname=font, fontsize=size)
        item = Item(page, text, (x, y - size * 0.95, x + width, y + size * 0.3), pii, category)
        self.items.append(item)
        return item

    def label(self, page: int, x: float, y: float, text: str, size: float = 7.0) -> Item:
        return self.text(page, x, y, text, size=size)

    def value(self, page: int, x: float, y: float, text: str, category: str, *,
              pii: bool = True, size: float = 10.0) -> Item:
        return self.text(page, x, y, text, size=size, font="cour", color=BLACK, pii=pii, category=category)

    def prose(self, page: int, x: float, y: float, text: str, size: float = 9.0) -> Item:
        """Software-generated text (not part of the government template)."""
        return self.text(page, x, y, text, size=size, color=BLACK, category="software_text")

    def rule(self, page: int, y: float, x0: float = 36, x1: float = 576) -> None:
        self._doc[page].draw_line((x0, y), (x1, y), color=TEMPLATE, width=0.5)

    def save(self, path: Path) -> str:
        self._doc.save(str(path))
        self._doc.close()
        return str(path)


@dataclass
class Household:
    first: str
    middle: str
    last: str
    spouse_first: str
    kids: tuple[tuple[str, str], ...]
    ssn: str
    spouse_ssn: str
    kid_ssns: tuple[str, ...]
    street: str
    street_mixed: str
    city: str
    state: str
    zip: str
    phone: str
    email: str
    dob: str
    spouse_dob: str
    occupation: str
    spouse_occupation: str


HOUSEHOLD_A = Household(
    first="Avery", middle="M", last="Quillfeather", spouse_first="Jordan-Lee",
    kids=(("Rowan", "Daughter"), ("Ellis", "Son")),
    ssn="521-38-4417", spouse_ssn="533-62-1904", kid_ssns=("548-21-7730", "549-90-2268"),
    street="4821 MAPLEWOOD CMN", street_mixed="4821 Maplewood Cmn",
    city="BRIARWOOD", state="CA", zip="94512", phone="(510)555-0142",
    email="avery.q@example.net", dob="06-21-1975", spouse_dob="09-02-1978",
    occupation="Pharmacist", spouse_occupation="Architect",
)

HOUSEHOLD_B = Household(
    first="Tomasz", middle="R", last="Okonkwo-Brandt", spouse_first="Ines",
    kids=(("Mila", "Daughter"),),
    ssn="612-44-0981", spouse_ssn="618-20-3357", kid_ssns=("627-11-5402",),
    street="77 HARBOR VIEW TER", street_mixed="77 Harbor View Ter",
    city="PORTLAND", state="OR", zip="97209", phone="503-555-0177",
    email="t.okonkwo@example.org", dob="11-23-1969", spouse_dob="06-30-1972",
    occupation="Engineer", spouse_occupation="Teacher",
)

#: Short (3-letter) surname, hyphenated given name, 3-letter given names. Every
#: length guard written for "John Smith"-shaped names rejects some part of this
#: household - the shape behind the largest miss on the real diagnostic returns.
HOUSEHOLD_C = Household(
    first="Kai", middle="", last="Lim", spouse_first="Hua-Wen",
    kids=(("Ana", "Daughter"), ("Theo", "Son")),
    ssn="604-27-1185", spouse_ssn="607-35-9921", kid_ssns=("611-48-2076", "613-02-6654"),
    street="2290 SEACLIFF CMN", street_mixed="2290 Seacliff Cmn",
    city="LAKESIDE CITY", state="CA", zip="94621", phone="(510)555-0186",
    email="kai.lim@example.com", dob="08-19-1981", spouse_dob="02-11-1983",
    occupation="Nurse", spouse_occupation="Accountant",
)


def _joint(h: Household) -> str:
    return f"{h.first} & {h.spouse_first} {h.last}"


def build_return(path: Path, h: Household, name: str = "return") -> FormDoc:
    """A 9-page, software-prepared individual return for household `h`."""
    d = FormDoc(name)

    # ---- page 1: software summary / e-file instructions (software text) ----
    p = d.page()
    d.text(p, 36, 50, "Electronic Filing Instructions for your 2021 Federal Tax Return", size=12,
           color=BLACK, category="software_text")
    d.prose(p, 36, 80, "Important: Your taxes are not finished until all required steps are completed.")
    d.value(p, 36, 110, _joint(h), "person", size=10)
    d.value(p, 36, 124, h.street, "address", size=10)
    d.value(p, 36, 138, f"{h.city} {h.state} {h.zip}", "address", size=10)
    d.prose(p, 36, 170, "Balance Due/Refund")
    d.prose(p, 36, 184, "Your federal tax return (Form 1040) shows a balance due of $8,477.00.")
    d.prose(p, 36, 198, "Your return shows you have elected to pay your balance due by Direct Debit.")
    d.prose(p, 36, 212, "- Amount Withdrawn:       $8,477.00")
    d.prose(p, 36, 226, "- Date of Withdrawal:     04/14/2022")
    d.prose(p, 36, 254, "Need to Keep")
    d.prose(p, 36, 268, "A copy of your federal return")
    d.prose(p, 36, 296, "Adjusted Gross Income                   $    218,032.00")
    d.prose(p, 36, 310, "Taxable Income                          $    192,932.00")
    d.prose(p, 36, 324, "Effective Tax Rate                               14.92%")
    d.prose(p, 36, 352, "Mail payments to:")
    d.prose(p, 36, 366, "Internal Revenue Service")
    d.prose(p, 36, 380, "PO BOX 802502")
    d.prose(p, 36, 394, "CINCINNATI OH 45280-2502")
    d.prose(p, 36, 422, "Voucher Number        Due Date                Amount")
    d.prose(p, 36, 436, "1                04/18/2022           $    2,905.00")
    d.prose(p, 36, 450, "2                06/15/2022           $    2,905.00")

    # ---- page 2: estimated tax voucher with scanline ----
    p = d.page()
    d.label(p, 36, 60, "Form 1040-ES Department of the Treasury Internal Revenue Service", 8)
    d.label(p, 36, 74, "Payment Voucher 1", 9)
    d.label(p, 36, 100, "Your first name and middle initial")
    d.label(p, 230, 100, "Your last name")
    d.label(p, 420, 100, "Your social security number")
    d.value(p, 36, 114, f"{h.first.upper()} {h.middle}".strip(), "person")
    d.value(p, 230, 114, h.last.upper(), "person")
    d.value(p, 420, 114, h.ssn, "ssn")
    d.label(p, 36, 134, "If joint payment, complete for spouse")
    d.label(p, 36, 148, "Spouse's first name and middle initial")
    d.label(p, 230, 148, "Spouse's last name")
    d.label(p, 420, 148, "Spouse's social security number")
    d.value(p, 36, 162, h.spouse_first.upper(), "person")
    d.value(p, 230, 162, h.last.upper(), "person")
    d.value(p, 420, 162, h.spouse_ssn, "ssn")
    d.label(p, 36, 182, "Address (number, street, and apt. no.)")
    d.value(p, 36, 196, h.street, "address")
    d.label(p, 36, 216, "City, town, or post office. If you have a foreign address, also complete spaces below.")
    d.value(p, 36, 230, h.city, "address")
    d.label(p, 300, 216, "State")
    d.value(p, 300, 230, h.state, "address", pii=False)
    d.label(p, 360, 216, "ZIP code")
    d.value(p, 360, 230, h.zip, "address")
    d.label(p, 36, 260, "Amount of estimated tax you are paying by check or money order.")
    d.value(p, 480, 260, "2,905.", "money", pii=False)
    name_control = "".join(ch for ch in h.last.upper() if ch.isalpha())[:4]
    d.value(p, 36, 300, f"{h.state} {name_control}  30 0 202212 430", "scanline", pii=False)
    # Only the name-control token inside the scanline is identity.
    d.items[-1].pii = False
    x = 36 + pymupdf.get_text_length(f"{h.state} ", fontname="cour", fontsize=10)
    d.items.append(Item(p, name_control, (x, 300 - 9.5, x + pymupdf.get_text_length(name_control, fontname="cour", fontsize=10), 303), True, "name_control"))

    # ---- page 3: Form 1040 page 1 ----
    p = d.page()
    d.text(p, 36, 40, "Form 1040", size=14)
    d.label(p, 110, 34, "Department of the Treasury—Internal Revenue Service")
    d.text(p, 110, 46, "U.S. Individual Income Tax Return", size=11)
    d.text(p, 330, 46, "2021", size=14)
    d.label(p, 420, 46, "IRS Use Only—Do not write or staple in this space.")
    d.text(p, 36, 66, "Filing Status", size=9)
    d.label(p, 120, 66, "Single")
    d.label(p, 170, 66, "Married filing jointly")
    d.label(p, 270, 66, "Married filing separately (MFS)")
    d.label(p, 400, 66, "Head of household (HOH)")
    d.label(p, 36, 90, "Your first name and middle initial")
    d.label(p, 250, 90, "Last name")
    d.label(p, 450, 90, "Your social security number")
    d.value(p, 40, 104, f"{h.first} {h.middle}".strip(), "person")
    d.value(p, 250, 104, h.last, "person")
    d.value(p, 450, 104, h.ssn, "ssn")
    d.label(p, 36, 120, "If joint return, spouse's first name and middle initial")
    d.label(p, 250, 120, "Last name")
    d.label(p, 450, 120, "Spouse's social security number")
    d.value(p, 40, 134, h.spouse_first, "person")
    d.value(p, 250, 134, h.last, "person")
    d.value(p, 450, 134, h.spouse_ssn, "ssn")
    d.label(p, 36, 150, "Home address (number and street). If you have a P.O. box, see instructions.")
    d.label(p, 400, 150, "Apt. no.")
    d.value(p, 40, 164, h.street, "address")
    d.label(p, 450, 150, "Presidential Election Campaign")
    d.label(p, 450, 160, "Check here if you, or your spouse")
    d.label(p, 450, 170, "if filing jointly, want $3 to go to this fund.")
    d.label(p, 470, 186, "You")
    d.label(p, 520, 186, "Spouse")
    d.label(p, 36, 180, "City, town, or post office. If you have a foreign address, also complete spaces below.")
    d.label(p, 300, 180, "State")
    d.label(p, 360, 180, "ZIP code")
    d.value(p, 40, 194, h.city.title(), "address")
    d.value(p, 300, 194, h.state, "address", pii=False)
    d.value(p, 360, 194, h.zip, "address")
    d.label(p, 36, 214, "At any time during 2021, did you receive, sell, exchange, or otherwise dispose of any financial interest in any virtual currency?")
    d.label(p, 36, 232, "Standard Deduction", 8)
    d.label(p, 120, 232, "Someone can claim:")
    d.label(p, 200, 232, "You as a dependent")
    d.label(p, 300, 232, "Your spouse as a dependent")
    d.text(p, 36, 256, "Dependents", size=9)
    d.label(p, 100, 256, "(see instructions):")
    d.label(p, 120, 268, "(1) First name")
    d.label(p, 200, 268, "Last name")
    d.label(p, 300, 268, "(2) Social security number")
    d.label(p, 410, 268, "(3) Relationship to you")
    y = 282
    for (kid, rel), kssn in zip(h.kids, h.kid_ssns):
        d.value(p, 120, y, kid, "person")
        d.value(p, 200, y, h.last, "person")
        d.value(p, 300, y, kssn, "ssn")
        d.value(p, 410, y, rel, "relationship", pii=False)
        y += 14
    d.label(p, 40, 330, "Attach")
    d.label(p, 40, 340, "Sch. B if")
    d.label(p, 40, 350, "required.")
    rows = [
        ("1", "Wages, salaries, tips, etc. Attach Form(s) W-2", "171,240."),
        ("2a", "Tax-exempt interest", ""),
        ("5a", "Pensions and annuities", "312."),
        ("8", "Other income from Schedule 1, line 10", "47,115."),
        ("9", "Add lines 1, 2b, 3b, 4b, 5b, 6b, 7, and 8. This is your total income", "218,667."),
        ("11", "Subtract line 10 from line 9. This is your adjusted gross income", "218,032."),
        ("12a", "Standard deduction or itemized deductions (from Schedule A)", "25,100."),
        ("15", "Taxable income. Subtract line 14 from line 11. If zero or less, enter -0-", "192,932."),
    ]
    y = 330
    for num, text, amount in rows:
        d.label(p, 100, y, num, 8)
        d.label(p, 120, y, text, 8)
        if amount:
            d.value(p, 520, y, amount, "money", pii=False)
        y += 16
    d.label(p, 36, 400, "Standard")
    d.label(p, 36, 410, "Deduction for—")
    d.label(p, 36, 420, "• Single or")
    d.label(p, 36, 430, "Married filing")
    d.label(p, 36, 440, "separately, $12,550")
    d.label(p, 36, 760, "For Disclosure, Privacy Act, and Paperwork Reduction Act Notice, see separate instructions.")

    # ---- page 4: Form 1040 page 2, signature block ----
    p = d.page()
    d.label(p, 36, 40, "Form 1040 (2021)", 8)
    d.label(p, 36, 80, "16 Tax (see instructions). Check if any from Form(s):")
    d.value(p, 520, 80, "36,210.", "money", pii=False)
    d.label(p, 36, 96, "37 Amount you owe. Subtract line 33 from line 24.")
    d.value(p, 520, 96, "8,477.", "money", pii=False)
    d.text(p, 36, 130, "Sign Here", size=10)
    d.label(p, 110, 120, "Under penalties of perjury, I declare that I have examined this return and accompanying schedules.")
    d.label(p, 110, 140, "Your signature")
    d.label(p, 280, 140, "Date")
    d.label(p, 340, 140, "Your occupation")
    d.value(p, 340, 152, h.occupation, "occupation", pii=False)
    d.label(p, 110, 168, "Spouse's signature. If a joint return, both must sign.")
    d.label(p, 340, 168, "Spouse's occupation")
    d.value(p, 340, 180, h.spouse_occupation, "occupation", pii=False)
    d.label(p, 110, 196, "Phone no.")
    d.value(p, 160, 196, h.phone, "phone")
    d.label(p, 300, 196, "Email address")
    d.value(p, 370, 196, h.email, "email")
    d.text(p, 36, 230, "Paid Preparer Use Only", size=9)
    d.label(p, 150, 222, "Preparer's name")
    d.label(p, 300, 222, "Preparer's signature")
    d.value(p, 150, 234, "Self-Prepared", "software_value", pii=False)

    # ---- pages 5-7: schedules with "Name(s) shown on return" ----
    for title, amount_rows in (
        ("SCHEDULE 1 (Form 1040) Additional Income and Adjustments to Income",
         [("8z", "Other income. List type and amount:", "7,350.")]),
        ("SCHEDULE C (Form 1040) Profit or Loss From Business",
         [("1", "Gross receipts or sales.", "41,200."), ("31", "Net profit or (loss).", "27,880.")]),
        ("SCHEDULE SE (Form 1040) Self-Employment Tax",
         [("2", "Net profit or (loss) from Schedule C, line 31", "27,880.")]),
    ):
        p = d.page()
        d.text(p, 36, 40, title, size=10)
        d.label(p, 36, 64, "Attach to Form 1040, 1040-SR, or 1040-NR.")
        d.label(p, 36, 84, "Name(s) shown on Form 1040, 1040-SR, or 1040-NR")
        d.label(p, 430, 84, "Your social security number")
        d.value(p, 36, 98, " ".join(x for x in (h.first, h.middle, "&", h.spouse_first, h.last) if x), "person")
        d.value(p, 430, 98, h.ssn, "ssn")
        y = 130
        for num, text, amount in amount_rows:
            d.label(p, 36, y, num, 8)
            d.label(p, 60, y, text, 8)
            d.value(p, 500, y, amount, "money", pii=False)
            y += 18
        if "SCHEDULE C" in title:
            d.label(p, 36, 190, "A Principal business or profession, including product or service")
            d.value(p, 36, 204, "bookkeeping services", "business_activity", pii=False)
            d.label(p, 36, 222, "Name of proprietor")
            d.value(p, 36, 236, f"{h.spouse_first} {h.last}", "person")
            d.label(p, 36, 254, "E Business address (including suite or room no.)")
            d.value(p, 36, 268, h.street_mixed, "address")
        d.label(p, 36, 760, "For Paperwork Reduction Act Notice, see your tax return instructions.")

    # ---- page 8: state return with dates of birth ----
    p = d.page()
    d.text(p, 36, 40, "California Resident Income Tax Return 2021 Form 540", size=10)
    d.label(p, 36, 70, "Your first name")
    d.label(p, 160, 70, "Initial")
    d.label(p, 200, 70, "Last name")
    d.label(p, 420, 70, "Your SSN or ITIN")
    d.value(p, 36, 84, h.first.upper(), "person")
    if h.middle:
        d.value(p, 160, 84, h.middle, "person", pii=False)
    d.value(p, 200, 84, h.last.upper(), "person")
    d.value(p, 420, 84, h.ssn, "ssn")
    d.label(p, 36, 104, "Street address (number and street) or PO box")
    d.value(p, 36, 118, h.street, "address")
    d.label(p, 36, 140, "Date of Birth")
    d.label(p, 36, 152, "Your DOB (mm/dd/yyyy)")
    d.label(p, 200, 152, "Spouse's/RDP's DOB (mm/dd/yyyy)")
    d.value(p, 36, 166, h.dob, "dob")
    d.value(p, 200, 166, h.spouse_dob, "dob")
    d.label(p, 36, 190, "Prior Name: If you filed your 2020 tax return under a different last name, write the last name only.")
    d.label(p, 36, 210, "Filing Status. Check the box for your filing status. See instructions.")
    d.label(p, 36, 224, "Married/RDP filing jointly. See inst.")
    d.label(p, 36, 250, "13 Federal adjusted gross income from federal Form 1040, line 11")
    d.value(p, 500, 250, "218032", "money", pii=False)
    d.label(p, 36, 266, "19 Taxable income. Subtract line 18 from line 17.")
    d.value(p, 500, 266, "209411", "money", pii=False)

    # ---- page 9: software explanation statement (prose) ----
    p = d.page()
    d.text(p, 36, 50, "Additional information from your 2021 Federal Tax Return", size=11,
           color=BLACK, category="software_text")
    d.prose(p, 36, 80, "Form 1040: Individual Tax Return")
    d.prose(p, 36, 94, "Line 4a  Explanation Statement")
    d.prose(p, 36, 108, "Date of the Original Contribution: 3/09/2021")
    d.prose(p, 36, 122, "Date of the Recharacterization: 03/28/2022")
    _sentence(d, p, 36, 150, [
        ("Mr.", False), (h.last, True),
        ("recharacterized the contribution because MAGI was too high to qualify.", False)])
    _sentence(d, p, 36, 164, [
        ("The account owner,", False), (h.spouse_first, True),
        (", confirmed the transfer amount of $4210.35.", False)])
    d.prose(p, 36, 178, "Reason for the Recharacterization: MAGI too high to contribute to a Roth IRA.")
    d.prose(p, 36, 206, "Payer: Northwind Literacy Foundation")
    return d


def _sentence(d: FormDoc, page: int, x: float, y: float, parts: list[tuple[str, bool]], size: float = 9.0) -> None:
    """Write a sentence whose identity parts are recorded as PII items."""
    for text, is_pii in parts:
        if not text.startswith((",", ".")) and x > 36:
            x += pymupdf.get_text_length(" ", fontname="helv", fontsize=size)
        d.text(page, x, y, text, size=size, color=BLACK, pii=is_pii,
               category="person" if is_pii else "software_text")
        x += pymupdf.get_text_length(text, fontname="helv", fontsize=size)


def build_blank(path: Path) -> FormDoc:
    """The same template with no filled values: a pure false-positive benchmark."""
    full = build_return(path, HOUSEHOLD_A, "blank")
    full._doc.close()
    # Re-draw only the government template text, at the same size and place.
    d2 = FormDoc("blank")
    pages = {}
    for item in full.items:
        if item.category == "form_text":
            pages.setdefault(item.page, []).append(item)
    for page_no in range(max(pages) + 1):
        d2.page()
        for item in pages.get(page_no, []):
            size = (item.rect[3] - item.rect[1]) / 1.25
            d2.text(page_no, item.rect[0], item.rect[1] + size * 0.95, item.text, size=size)
    return d2


def build_mixed_layout(path: Path) -> FormDoc:
    """Unrelated layouts: a two-column letter on A4 and a landscape table page."""
    d = FormDoc("mixed_layout")
    p = d.page(595, 842)
    d.text(p, 50, 60, "Northwind Community Clinic", size=14, color=BLACK, category="software_text")
    d.prose(p, 50, 80, "Patient Visit Summary")
    d.label(p, 50, 110, "Patient name:", 9)
    d.value(p, 130, 110, "Harriet Vandermolen", "person", size=9)
    d.label(p, 320, 110, "Date of birth:", 9)
    d.value(p, 400, 110, "09/14/1958", "dob", size=9)
    d.label(p, 50, 126, "Member ID:", 9)
    d.value(p, 130, 126, "XQ48812093", "id", size=9)
    d.label(p, 320, 126, "Phone:", 9)
    d.value(p, 400, 126, "415-555-0199", "phone", size=9)
    left = [
        "Visit reason: annual review.", "Blood pressure stable.",
        "Continue current plan.", "Follow up in six months.",
    ]
    right = [
        "Ordering provider: Dr. Kwame Asante", "Clinic: Northwind Community Clinic",
        "Billing: Standard plan", "Copay collected: $25.00",
    ]
    for i, line in enumerate(left):
        d.prose(p, 50, 160 + 14 * i, line)
    _sentence(d, p, 320, 160, [("Ordering provider: Dr.", False), ("Kwame Asante", True)])
    for i, line in enumerate(right[1:], start=1):
        d.prose(p, 320, 160 + 14 * i, line)
    _sentence(d, p, 50, 240, [
        ("Ms.", False), ("Vandermolen", True),
        ("was advised to walk daily. Jordan River Trail is near the clinic.", False)])
    p = d.page(792, 612)
    d.text(p, 40, 40, "Quarterly Contributions Report", size=12, color=BLACK, category="software_text")
    headers = ["Employee", "Employee ID", "Department", "Contribution", "Match"]
    xs = [40, 220, 340, 470, 600]
    for x, hdr in zip(xs, headers):
        d.label(p, x, 70, hdr, 9)
    rows = [("Priya Raghunathan", "E-48213", "Finance", "$1,200.00", "$600.00"),
            ("Mateo Ferreira-Lind", "E-51877", "Operations", "$950.00", "$475.00"),
            ("Grace Whitlock", "E-60022", "Legal", "$1,500.00", "$750.00")]
    for r, row in enumerate(rows):
        y = 90 + 16 * r
        d.value(p, xs[0], y, row[0], "person", size=9)
        d.value(p, xs[1], y, row[1], "id", size=9)
        d.value(p, xs[2], y, row[2], "department", pii=False, size=9)
        d.value(p, xs[3], y, row[3], "money", pii=False, size=9)
        d.value(p, xs[4], y, row[4], "money", pii=False, size=9)
    d.prose(p, 40, 170, "Totals reflect payroll through the end of the quarter. Questions: contact Payroll.")
    return d


CORPUS: dict[str, Callable[[Path], FormDoc]] = {
    "return_a": lambda path: build_return(path, HOUSEHOLD_A, "return_a"),
    "return_b": lambda path: build_return(path, HOUSEHOLD_B, "return_b"),
    "return_c": lambda path: build_return(path, HOUSEHOLD_C, "return_c"),
    "blank_form": build_blank,
    "mixed_layout": build_mixed_layout,
}


def build_all(directory: Path) -> dict[str, tuple[str, list[Item]]]:
    directory.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, builder in CORPUS.items():
        doc = builder(directory / f"{name}.pdf")
        path = doc.save(directory / f"{name}.pdf")
        out[name] = (path, doc.items)
    return out
