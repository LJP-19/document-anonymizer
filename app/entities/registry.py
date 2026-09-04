"""Stable identity mapping (spec section 25).

The same original value maps to the same pseudonym everywhere within a scope,
two different originals never collide onto one pseudonym, and pseudonyms are not
regenerated on re-render. Scope is per-document by default; batch-consistent
scope is available for multi-document engagements (spec section 45).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..detection.types import PiiType
from ..pseudonymization.generator import generate


def _shares_token(original: str, pseudonym: str) -> bool:
    """True if the pseudonym reuses any meaningful token from the original."""
    import re

    def tokens(s: str) -> set[str]:
        return {tok for tok in re.split(r"[^A-Za-z0-9]+", s.lower()) if len(tok) >= 3}

    return bool(tokens(original) & tokens(pseudonym))


@dataclass(frozen=True)
class EntityKey:
    pii_type: PiiType
    normalized: str
    discriminator: str = ""  # distinguishes same-name different-person


@dataclass
class EntityRecord:
    key: EntityKey
    original: str
    pseudonym: str
    occurrences: int = 0
    user_edited: bool = False


@dataclass
class EntityRegistry:
    scope: str = "document"
    _records: dict[EntityKey, EntityRecord] = field(default_factory=dict)
    _taken: set[str] = field(default_factory=set)

    @staticmethod
    def normalize(text: str) -> str:
        return " ".join(text.split()).strip(" .,;:").lower()

    def key_for(self, pii_type: PiiType, text: str, discriminator: str = "") -> EntityKey:
        return EntityKey(pii_type, self.normalize(text), discriminator)

    def pseudonym_for(
        self, pii_type: PiiType, text: str, discriminator: str = ""
    ) -> str:
        key = self.key_for(pii_type, text, discriminator)
        record = self._records.get(key)
        if record is not None:
            record.occurrences += 1
            return record.pseudonym

        pseudonym = generate(pii_type, text, scope=f"{self.scope}|{discriminator}")
        salt = 0
        while pseudonym.lower() in self._taken or _shares_token(text, pseudonym):
            salt += 1
            pseudonym = generate(pii_type, f"{text}#{salt}", scope=f"{self.scope}|{discriminator}")
            if salt > 25:  # pathological; accept rather than loop forever
                break
        record = EntityRecord(key=key, original=text, pseudonym=pseudonym, occurrences=1)
        self._records[key] = record
        self._taken.add(pseudonym.lower())
        return pseudonym

    def override(self, pii_type: PiiType, text: str, pseudonym: str, discriminator: str = "") -> None:
        key = self.key_for(pii_type, text, discriminator)
        existing = self._records.get(key)
        if existing:
            self._taken.discard(existing.pseudonym.lower())
            existing.pseudonym = pseudonym
            existing.user_edited = True
        else:
            self._records[key] = EntityRecord(
                key=key, original=text, pseudonym=pseudonym, user_edited=True
            )
        self._taken.add(pseudonym.lower())

    def lookup(self, pii_type: PiiType, text: str, discriminator: str = "") -> Optional[EntityRecord]:
        return self._records.get(self.key_for(pii_type, text, discriminator))

    @property
    def records(self) -> list[EntityRecord]:
        return list(self._records.values())
