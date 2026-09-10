"""Synthetic PDF fixtures.

All data here is invented. Real client PII must never enter this repository
(spec section 80).
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


def _write(path: Path, draw) -> str:
    c = canvas.Canvas(str(path), pagesize=letter)
    draw(c)
    c.save()
    return str(path)


def stacked_field_pdf(path: Path) -> str:
    """Spec section 84: label on one line, value stacked underneath."""

    def draw(c):
        c.setFont("Helvetica", 10)
        y = 700
        for text in [
            "Name, address, and zip code",
            "LJP",
            "Fremont, CA",
            "123",
        ]:
            c.drawString(72, y, text)
            y -= 14
        c.drawString(72, y - 24, "Taxable income: $123,456")

    return _write(path, draw)


def form_pdf(path: Path) -> str:
    """Spec section 82: labelled form fields with stacked values."""

    def draw(c):
        c.setFont("Helvetica", 10)
        rows = [
            ("Name:", ["John Smith"]),
            ("Address:", ["123 Main Street", "Apartment 4B", "Fremont, CA 94538"]),
            ("SSN:", ["123-45-6789"]),
            ("Email:", ["john@example.com"]),
            ("Annual Salary:", ["$85,000"]),
        ]
        y = 720
        for label, values in rows:
            c.drawString(72, y, label)
            y -= 14
            for v in values:
                c.drawString(72, y, v)
                y -= 14
            y -= 8

    return _write(path, draw)


def paragraph_pdf(path: Path) -> str:
    """Spec section 83: prose PII with a financial value that must survive."""

    def draw(c):
        c.setFont("Helvetica", 11)
        lines = [
            "John Smith submitted the report to ABC Company.",
            "Please contact john@example.com.",
            "His phone number is (555) 123-4567.",
            "His SSN is 123-45-6789.",
            "The total amount owed is $18,450.00.",
        ]
        y = 700
        for line in lines:
            c.drawString(72, y, line)
            y -= 16

    return _write(path, draw)


def table_pdf(path: Path) -> str:
    """Spec section 22: identities and SSNs go, salaries and headers stay."""

    def draw(c):
        c.setFont("Helvetica-Bold", 10)
        c.drawString(60, 700, "Employee")
        c.drawString(200, 700, "SSN")
        c.drawString(320, 700, "Salary")
        c.setFont("Helvetica", 10)
        rows = [
            ("John Smith", "123-45-6789", "$85,000"),
            ("Jane Doe", "987-65-4321", "$92,000"),
        ]
        y = 682
        for name, ssn, salary in rows:
            c.drawString(60, y, name)
            c.drawString(200, y, ssn)
            c.drawString(320, y, salary)
            y -= 16

    return _write(path, draw)


def scanned_like_pdf(path: Path) -> str:
    """A page with no extractable text: must be reported OCR REQUIRED."""

    def draw(c):
        c.setFillColorRGB(0.85, 0.85, 0.85)
        c.rect(50, 400, 500, 300, fill=1, stroke=0)

    return _write(path, draw)


ALL = {
    "stacked": stacked_field_pdf,
    "form": form_pdf,
    "paragraph": paragraph_pdf,
    "table": table_pdf,
    "scanned": scanned_like_pdf,
}


def build_all(directory: Path) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    return {name: fn(directory / f"{name}.pdf") for name, fn in ALL.items()}


if __name__ == "__main__":
    import sys

    out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/fixtures")
    for name, p in build_all(out).items():
        print(f"{name}: {p}")
