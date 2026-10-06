"""Unicode-aware text comparison and person-name parsing.

Everything here is for COMPARISON and SEARCH only. The document's own
characters, and the exact bounding boxes behind them, are never altered: a
match is found in a folded copy and mapped back to offsets in the ORIGINAL line,
so a redaction always covers the characters that are really on the page.

Why this exists: matching used `str.lower()` and ASCII-only patterns, so
"JOSE NUNEZ" did not match "José Núñez", "O'Reilly" did not match "O’Reilly",
"Garcia-Lopez" did not match "Garcia Lopez", and a name containing a letter
outside [A-Za-z] was not even tokenised as a name.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

_HYPHENS = "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"
_APOSTROPHES = "'\u2019\u2018\u02bc`\u00b4\u2032"
#: Letters with no Unicode decomposition that a person would still call "the
#: same letter without the mark" (Polish ł, Turkish dotless ı, Danish ø, ...).
_TRANSLIT = {
    "ł": "l", "đ": "d", "ø": "o", "æ": "ae", "œ": "oe", "ð": "d",
    "þ": "th", "ħ": "h", "ı": "i", "ŧ": "t",
}


def _fold_char(ch: str) -> str:
    if ch in _HYPHENS:
        return " "
    if ch in _APOSTROPHES or ch == ".":
        return ""
    out: list[str] = []
    for piece in ch.casefold():
        for part in unicodedata.normalize("NFKD", piece):
            if unicodedata.combining(part):
                continue
            out.append(_TRANSLIT.get(part, part))
    return "".join(out)


def fold_with_map(text: str) -> tuple[str, list[int]]:
    """A comparison copy of `text` plus, for every character of it, the index of
    the ORIGINAL character it came from."""
    chars: list[str] = []
    mapping: list[int] = []
    for index, ch in enumerate(text):
        folded = " " if ch.isspace() else _fold_char(ch)
        for piece in folded:
            if piece == " " and chars and chars[-1] == " ":
                continue
            chars.append(piece)
            mapping.append(index)
    return "".join(chars), mapping


def fold(text: str) -> str:
    """Case-, accent-, hyphen-, apostrophe- and whitespace-insensitive key."""
    return fold_with_map(text)[0].strip()


def find_occurrences(text: str, needle: str) -> list[tuple[int, int]]:
    """Every (start, end) in the ORIGINAL `text` where `needle` occurs as a whole
    word or phrase, ignoring case, accents, hyphens and apostrophes."""
    wanted = fold(needle)
    if not wanted:
        return []
    folded, mapping = fold_with_map(text)
    found: list[tuple[int, int]] = []
    position = 0
    while True:
        index = folded.find(wanted, position)
        if index == -1:
            return found
        position = index + 1
        start = mapping[index]
        end = mapping[index + len(wanted) - 1] + 1
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        if (before and before.isalnum()) or (after and after.isalnum()):
            continue
        found.append((start, end))


def is_letter(ch: str) -> bool:
    return unicodedata.category(ch).startswith("L")


def looks_like_name_token(token: str) -> bool:
    """Name-shaped, in any script: capitalised in a cased script, or a letter
    run in a caseless one (Chinese, Arabic, Thai, ...). Not a dictionary test -
    an unfamiliar name must be as acceptable as a common one."""
    letters = [ch for ch in token if is_letter(ch)]
    if not letters:
        return False
    first = letters[0]
    if first.isupper():
        return True
    return not first.islower()  # caseless script


# --------------------------------------------------------------- person names

_TITLES = {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "rev", "sr", "sra", "srta"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "md", "cpa", "esq", "phd", "dds"}
_COMMAS = {ord("\u060c"): ",", ord("\uff0c"): ",", ord("\u3001"): ",", ord("\u061b"): ","}
_WORD = re.compile(r"[^\W\d_]+(?:[’'\-][^\W\d_]+)*\.?", re.UNICODE)


@dataclass
class PersonName:
    given: list[str] = field(default_factory=list)   # first name, middle names/initials
    surname: str = ""
    surname_first: bool = False
    raw: str = ""

    @property
    def is_full(self) -> bool:
        return bool(self.given) and bool(self.surname)

    @property
    def first(self) -> str:
        return self.given[0] if self.given else ""


def parse_person(text: str) -> PersonName:
    """Split a name into given names and surname, handling "Last, First",
    titles, suffixes, initials and non-Latin scripts."""
    # Arabic, fullwidth and ideographic commas separate "Last, First" too.
    raw = " ".join(text.translate(_COMMAS).split()).strip(" .,;:")
    comma_parts = [part.strip() for part in raw.split(",") if part.strip()]

    def words(piece: str) -> list[str]:
        out = []
        for match in _WORD.finditer(piece):
            token = match.group()
            bare = fold(token)
            if bare in _TITLES or bare in _SUFFIXES:
                continue
            out.append(token.rstrip("."))
        return out

    if len(comma_parts) == 2:
        surname_words, given_words = words(comma_parts[0]), words(comma_parts[1])
        if surname_words and given_words:
            return PersonName(given=given_words, surname=" ".join(surname_words),
                              surname_first=True, raw=raw)
    tokens = words(raw)
    if len(tokens) == 1:
        return PersonName(given=[], surname=tokens[0], raw=raw)
    if len(tokens) >= 2:
        return PersonName(given=tokens[:-1], surname=tokens[-1], raw=raw)
    return PersonName(raw=raw)


def _initial_compatible(a: str, b: str) -> bool:
    fa, fb = fold(a), fold(b)
    if not fa or not fb:
        return False
    if fa == fb:
        return True
    return (len(fa) == 1 and fb.startswith(fa)) or (len(fb) == 1 and fa.startswith(fb))


def same_person(a: PersonName, b: PersonName) -> Optional[str]:
    """Why two FULL names are the same person, or None.

    Surnames must match exactly (folded); first names must match or be an
    initial of each other; middle names must not contradict. A surname or first
    name on its own is NEVER matched here - that needs outside evidence.
    """
    if not (a.is_full and b.is_full):
        return None
    if fold(a.surname) != fold(b.surname):
        return None
    if not _initial_compatible(a.first, b.first):
        return None
    for left, right in zip(a.given[1:], b.given[1:]):
        if not _initial_compatible(left, right):
            return None
    return f"same surname '{fold(a.surname)}' and compatible given name"
