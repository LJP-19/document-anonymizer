"""One person, one pseudonym, however their name is written.

A name is not one string. The same person appears as "John Smith" on page 1,
as "Smith" and "John" in separate columns on page 2, as "Smith, John" in an
index, and as one half of "John & Jenny Smith" on a joint return. Mapping whole
strings gives each of those a different pseudonym, and the anonymised document
stops being about one person.

So the mapping is per TOKEN. "John" becomes "Alan" and "Smith" becomes "Doyle"
once, and every form composes from those:

    John Smith         -> Alan Doyle
    Smith              -> Doyle
    John               -> Alan
    Smith, John        -> Doyle, Alan
    John & Jenny Smith -> Alan & Rita Doyle

Joint names need no special handling: both halves share the surname token, so
they share the surname in the output.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

from ..detection.textnorm import fold, is_letter

SEPARATOR = re.compile(r"(\s*(?:&|\band\b|\+|,)\s*|\s+)", re.I)
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "md", "cpa", "esq", "phd", "dds"}
TITLE = {"mr", "mrs", "ms", "miss", "dr", "rev", "prof"}


def _is_structural(token: str) -> bool:
    """Titles, suffixes and initials are carried through unchanged."""
    bare = token.strip(".,")
    if fold(bare) in SUFFIX or fold(bare) in TITLE:
        return True
    # An initial is a single CASED letter. A single caseless character (a
    # Chinese given name such as "伟") is a whole name, not an initial.
    letters = [ch for ch in bare if is_letter(ch)]
    return len(bare) <= 1 and (not letters or letters[0].isupper() or letters[0].islower())


@dataclass
class NameRegistry:
    """Stable given-name and surname substitutions for one scope."""

    scope: str = "document"
    tokens: dict[str, str] = field(default_factory=dict)
    _taken: set[str] = field(default_factory=set)
    #: Every original name token seen. A pseudonym must never be one of these:
    #: replacing "Jenny" with "John" while a real John is in the same document
    #: is worse than no replacement at all.
    _originals: set[str] = field(default_factory=set)

    def _seed(self, token: str) -> int:
        key = f"{self.scope}|name|{fold(token)}"
        return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")

    def _substitute(self, token: str, surname: bool) -> str:
        from faker import Faker

        from .generator import _faker

        bare = token.strip(".,")
        self._originals.add(fold(bare))
        cached = self.tokens.get(fold(bare))
        if cached is not None:
            return self._match_case(cached, bare)

        fake: Faker = _faker()
        seed = self._seed(bare)
        chosen = ""
        for salt in range(30):
            fake.seed_instance(seed + salt)
            chosen = fake.last_name() if surname else fake.first_name()
            lowered = fold(chosen)
            if (
                lowered != fold(bare)
                and lowered not in self._taken
                and lowered not in self._originals
            ):
                break
        self.tokens[fold(bare)] = chosen
        self._taken.add(fold(chosen))
        return self._match_case(chosen, bare)

    @staticmethod
    def _match_case(replacement: str, original: str) -> str:
        if original.isupper():
            return replacement.upper()
        if original.islower():
            return replacement.lower()
        return replacement

    def note_original(self, name: str) -> None:
        """Record a real name so no pseudonym can collide with it."""
        for piece in SEPARATOR.split(name or ""):
            if piece and not SEPARATOR.fullmatch(piece):
                self._originals.add(fold(piece.strip(".,")))

    def pseudonym(self, name: str) -> str:
        """Rewrite a name, keeping its punctuation, order and structure."""
        text = name.strip()
        if not text:
            return text

        pieces = [p for p in SEPARATOR.split(text) if p is not None and p != ""]
        words = [p for p in pieces if not SEPARATOR.fullmatch(p)]
        if not words:
            return text

        # The last non-structural word is the surname; everything before it is
        # a given name. "Smith, John" is handled by the same rule because the
        # comma is a separator, not a word.
        meaningful = [w for w in words if not _is_structural(w)]
        surname_word = fold(meaningful[-1]) if meaningful else ""
        # Unicode letters, not [A-Za-z]: "Núñez, José" is surname-first too.
        surname_first = bool(re.match(r"^[^\W\d_]+(?:['\u2019\-][^\W\d_]+)*[,\u060c]", text)) and len(meaningful) > 1
        if surname_first:
            surname_word = fold(meaningful[0])

        out = []
        for piece in pieces:
            if SEPARATOR.fullmatch(piece):
                out.append(piece)
                continue
            if _is_structural(piece):
                out.append(piece)
                continue
            is_surname = fold(piece.strip(".,")) == surname_word
            out.append(self._substitute(piece, surname=is_surname))
        return "".join(out)

    def known(self, token: str) -> Optional[str]:
        return self.tokens.get(fold(token.strip(".,")))
