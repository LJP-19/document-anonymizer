"""Addresses are pseudonymized COMPONENT BY COMPONENT, like names are per token.

One registry per document maps each street, city and ZIP once, and every way the
document writes an address is composed from those parts:

    "Cupertino"                       (City box, page 1)
    "Cupertino, CA 95014"             (W-2, page 3)
    "2318 Rosewood Ter"               (both)
    "1880 S Bascom Ave Ste 210, Campbell, CA 95008"   (one-line firm address)

Before this, each string was its own entity: the same city got a different
pseudonym on page 1 and page 3, the ZIP two different ones, and the STATE was
changed inside a city line ("CA" -> "CT") while the State box kept "CA".

The state is never changed: it identifies no one (HIPAA Safe Harbor keeps it) and
changing it changes which state's law the return is read under.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from ..detection.types import PiiType
from .generator import US_STATES, generate

_UNIT = r"(?:apt|apartment|ste|suite|unit|rm|room|fl|floor|bldg|building|#)"
_TAIL = re.compile(
    r"(?P<lead>^|,\s*|\s+)(?P<city>[^\d,]+?)(?P<sep1>,?\s+)(?P<st>[A-Za-z]{2})\.?"
    r"(?:(?P<sep2>,?\s+)(?P<zip>\d{5}(?:-\d{4})?))?\s*$"
)
_ZIP_ONLY = re.compile(r"^\s*(\d{5})(?:-(\d{4}))?\s*$")
_STREET_UNIT = re.compile(rf"^(?P<street>.*?)(?P<usep>,?\s+)(?P<unit>{_UNIT}\.?\s*#?\s*[\w-]+)$", re.I)


def _case_like(src: str, out: str) -> str:
    letters = [ch for ch in src if ch.isalpha()]
    return out.upper() if letters and all(ch.isupper() for ch in letters) else out


class AddressComposer:
    """Component pseudonyms for one document. `fresh(kind, original, attempt)`
    generates a candidate; `ok(candidate)` rejects one that reuses a real word."""

    def __init__(self, scope: str, ok: Optional[Callable[[str], bool]] = None,
                 norm: Optional[Callable[[str], str]] = None):
        self.scope = scope
        self.ok = ok or (lambda _s: True)
        # "2318 Rosewood Terrace" and "2318 ROSEWOOD TER" are one street.
        self.norm = norm or (lambda t: " ".join(t.lower().split()))
        self.parts: dict[tuple[str, str], str] = {}

    # -- components ------------------------------------------------------
    def _part(self, kind: str, key: str, make: Callable[[int], str]) -> str:
        k = (kind, key)
        if k not in self.parts:
            out = make(0)
            n = 0
            while (not self.ok(out) or out.lower() == key.lower()
                   or out.lower() in {v.lower() for (kk, _), v in self.parts.items() if kk == kind}) and n < 25:
                n += 1
                out = make(n)
            self.parts[k] = out
        return self.parts[k]

    def city(self, city: str) -> str:
        key = " ".join(city.lower().split())
        out = self._part("city", key, lambda n: generate(PiiType.CITY_STATE, f"{key}#{n}" if n else key,
                                                         scope=self.scope))
        return _case_like(city, out)

    def zip(self, zip5: str, plus4: Optional[str]) -> str:
        z = self._part("zip", zip5, lambda n: re.sub(r"\D", "", generate(
            PiiType.POSTAL_CODE, f"{zip5}#{n}" if n else zip5, scope=self.scope))[:5].ljust(5, "1"))
        if plus4 is None:
            return z
        p4 = self._part("zip4", zip5 + plus4, lambda n: re.sub(r"\D", "", generate(
            PiiType.POSTAL_CODE, f"{zip5}-{plus4}#{n}", scope=self.scope))[-4:].rjust(4, "1"))
        return f"{z}-{p4}"

    def street(self, street: str) -> str:
        key = self.norm(street)
        out = self._part("street", key, lambda n: generate(PiiType.STREET, f"{street}#{n}" if n else street,
                                                           scope=self.scope))
        return _case_like(street, out)

    def unit(self, unit: str) -> str:
        """'Ste 210' keeps 'Ste'; the number is replaced consistently."""
        m = re.match(rf"^({_UNIT}\.?\s*#?\s*)([\w-]+)$", unit, re.I)
        if not m:
            return unit
        num = m.group(2)
        out = self._part("unit", num.lower(), lambda n: "".join(
            str((int(ch) + 3 + n) % 10) if ch.isdigit() else ch for ch in num))
        return m.group(1) + out

    # -- whole values ----------------------------------------------------
    def compose(self, text: str) -> Optional[str]:
        """The pseudonym for an address-family value, or None if it is not a shape
        this understands (the caller then falls back to the old whole-string path)."""
        s = text.strip()
        if not s:
            return None
        z = _ZIP_ONLY.match(s)
        if z:
            return self.zip(z.group(1), z.group(2))
        head, tail = s, ""
        m = _TAIL.search(s)
        if m and m.group("st").upper() in US_STATES and not re.search(r"\d", m.group("city")):
            zip_part = ""
            if m.group("zip"):
                z5, _, p4 = m.group("zip").partition("-")
                zip_part = m.group("sep2") + self.zip(z5, p4 or None)
            city = m.group("city").strip()
            tail = (m.group("lead") + self.city(city) + m.group("sep1") + m.group("st") + zip_part)
            head = s[:m.start()]
            if not head:
                return tail.lstrip(", ")
        if not re.search(r"\d", head):
            # a city (or place) on its own, no state
            return self.city(head) + tail if not tail else None
        u = _STREET_UNIT.match(head)
        if u:
            return self.street(u.group("street")) + u.group("usep") + self.unit(u.group("unit")) + tail
        return self.street(head) + tail
