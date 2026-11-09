"""Stable identity mapping (spec section 25).

The same original value maps to the same pseudonym everywhere within a scope,
two different originals never collide onto one pseudonym, and pseudonyms are not
regenerated on re-render. Scope is per-document by default; batch-consistent
scope is available for multi-document engagements (spec section 45).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from ..detection.textnorm import fold
from ..detection.types import PiiType
from ..pseudonymization.generator import generate


def _shares_token(original: str, pseudonym: str) -> bool:
    """True if the pseudonym reuses any meaningful token from the original."""
    import re

    def tokens(s: str) -> set[str]:
        return {tok for tok in re.split(r"[\W_]+", fold(s)) if len(tok) >= 3}

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
    roster: object = None  # ClientRoster, injected; keeps names stable across files
    names: object = None   # NameRegistry, built on first use
    document_name: str = ""
    _records: dict[EntityKey, EntityRecord] = field(default_factory=dict)
    _taken: set[str] = field(default_factory=set)
    #: Every word of every value detected in the document. A pseudonym must not
    #: contain one: a generated city that equals a real
    #: value redacted elsewhere puts that value back into the file - measured,
    #: the independent verifier found two such on a real return.
    document_words: set = field(default_factory=set)

    #: Types whose value is the digits, however they are punctuated. "123456789"
    #: and "123-45-6789" are one SSN and must get one pseudonym.
    DIGIT_KEYED = {
        PiiType.SSN, PiiType.ITIN, PiiType.EIN, PiiType.TIN,
        PiiType.BANK_ACCOUNT, PiiType.ROUTING_NUMBER, PiiType.CARD_NUMBER,
        PiiType.STATE_TAX_ID,
    }

    #: Common street-suffix spellings that all mean the same thing. The same
    #: address written as "Street" on one page and "St" on another must key to
    #: one pseudonym, the same way one SSN in different formats already does.
    _STREET_SYNONYMS = {
        "street": "st", "avenue": "ave", "boulevard": "blvd", "drive": "dr",
        "lane": "ln", "court": "ct", "circle": "cir", "place": "pl",
        "terrace": "ter", "parkway": "pkwy", "highway": "hwy", "suite": "ste",
        "apartment": "apt", "common": "cmn", "trail": "trl", "crossing": "xing",
    }

    #: Business entity suffixes, all meaning the same LEGAL FORM at the level
    #: that matters for consistent keying. "Acme Holdings LLC" and "Acme
    #: Holdings, L.L.C." must key the same; a bare "Acme Holdings" (one
    #: occurrence dropped the suffix entirely - real and common when a
    #: name wraps or a form abbreviates) is treated as the SAME core entity
    #: too, since dropping the key entirely for missing-suffix cases fixes
    #: more real inconsistency than it risks - two different companies that
    #: happen to share a bare core name are rare next to one company written
    #: two ways.
    _ORG_SUFFIXES = {
        "llc", "l.l.c", "inc", "incorporated", "corp", "corporation", "co",
        "company", "ltd", "limited", "lp", "llp", "pllc", "pc", "plc",
    }

    @classmethod
    def normalize(cls, text: str) -> str:
        collapsed = unicodedata.normalize("NFKC", " ".join(text.split())).strip(" .,;:").casefold()
        tokens = collapsed.split()
        return " ".join(cls._STREET_SYNONYMS.get(tok, tok) for tok in tokens)

    @classmethod
    def _org_core(cls, text: str) -> str:
        """The business name with any trailing entity suffix stripped, for
        KEYING only - the pseudonym itself still adds back whatever suffix
        THIS occurrence actually had, via generate()'s own suffix detection."""
        collapsed = " ".join(text.split()).strip(" .,;:").lower()
        tokens = [tok.strip(".,;:") for tok in collapsed.split()]
        while tokens and tokens[-1] in cls._ORG_SUFFIXES:
            tokens.pop()
        return " ".join(tokens) or collapsed

    def key_for(self, pii_type: PiiType, text: str, discriminator: str = "") -> EntityKey:
        import re as _re

        if pii_type in self.DIGIT_KEYED:
            digits = _re.sub(r"\D", "", text)
            if digits:
                return EntityKey(pii_type, digits, discriminator)
        if pii_type in (PiiType.STREET, PiiType.ADDRESS) and _re.match(r"^\s*\d", text or ""):
            # One street line, one entity: detectors type the same "4817 HARBORVIEW
            # CMN" STREET on one page and ADDRESS on another, and keying by type
            # gave the same address two different pseudonyms (0.41.0).
            return EntityKey(PiiType.ADDRESS, self.normalize(text), discriminator)
        if pii_type is PiiType.ORG_PRIVATE:
            return EntityKey(pii_type, self._org_core(text), discriminator)
        if pii_type is PiiType.PERSON:
            # "JOSE NUNEZ", "José Núñez" and "José-Núñez" are one person.
            return EntityKey(pii_type, fold(text), discriminator)
        return EntityKey(pii_type, self.normalize(text), discriminator)

    def pseudonym_for(
        self, pii_type: PiiType, text: str, discriminator: str = ""
    ) -> str:
        key = self.key_for(pii_type, text, discriminator)
        record = self._records.get(key)
        if record is not None and record.user_edited:
            record.occurrences += 1
            return record.pseudonym
        # Addresses are composed from per-component pseudonyms EVERY time, so each
        # occurrence keeps its own case and punctuation while the street, city and
        # ZIP stay the same across the whole document. A user's edit still wins.
        if pii_type in (PiiType.ADDRESS, PiiType.CITY_STATE, PiiType.POSTAL_CODE, PiiType.STREET):
            composed = self._address_composer().compose(text)
            if composed is not None:
                if record is None:
                    self._records[key] = EntityRecord(key=key, original=text, pseudonym=composed, occurrences=1)
                    if self.roster is not None:
                        self.roster.record(pii_type, text, composed, self.document_name)
                else:
                    record.occurrences += 1
                self._taken.add(composed.lower())
                return composed
        if record is not None:
            record.occurrences += 1
            return record.pseudonym

        # A client already seen in another document keeps the same pseudonym,
        # so a batch reads coherently to whoever analyses it.
        if self.roster is not None:
            carried = self.roster.pseudonym_for(pii_type, text)
            if carried:
                self._records[key] = EntityRecord(
                    key=key, original=text, pseudonym=carried, occurrences=1
                )
                self._taken.add(carried.lower())
                return carried

        # Names go through the token registry: "John Smith", "Smith", "John"
        # and "John & Jenny Smith" must all agree with each other.
        if pii_type is PiiType.PERSON:
            if self.names is None:
                from ..pseudonymization.names import NameRegistry

                self.names = NameRegistry(scope=self.scope)
            pseudonym = self.names.pseudonym(text)
            self._records[key] = EntityRecord(
                key=key, original=text, pseudonym=pseudonym, occurrences=1
            )
            self._taken.add(pseudonym.lower())
            if self.roster is not None:
                self.roster.record(pii_type, text, pseudonym, self.document_name)
            return pseudonym

        pseudonym = generate(pii_type, text, scope=f"{self.scope}|{discriminator}")
        salt = 0
        while (pseudonym.lower() in self._taken or _shares_token(text, pseudonym)
               or self._reuses_document_word(pseudonym)):
            salt += 1
            pseudonym = generate(pii_type, f"{text}#{salt}", scope=f"{self.scope}|{discriminator}")
            if salt > 25:  # pathological; accept rather than loop forever
                break
        record = EntityRecord(key=key, original=text, pseudonym=pseudonym, occurrences=1)
        self._records[key] = record
        self._taken.add(pseudonym.lower())
        if self.roster is not None:
            self.roster.record(pii_type, text, pseudonym, self.document_name)
        return pseudonym

    def _address_composer(self):
        """One component registry per document: every street, city and ZIP is
        mapped once and every way the document writes an address is composed from
        those parts (app/pseudonymization/address.py)."""
        comp = getattr(self, "_addresses", None)
        if comp is None:
            from ..pseudonymization.address import AddressComposer

            comp = AddressComposer(self.scope, ok=lambda p: not self._reuses_document_word(p),
                                   norm=self.normalize)
            object.__setattr__(self, "_addresses", comp)
        return comp

    def _reuses_document_word(self, pseudonym: str) -> bool:
        if not self.document_words:
            return False
        import re as _re

        return any(w.lower() in self.document_words for w in _re.findall(r"[^\W\d_]{4,}", pseudonym))

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
        if self.roster is not None:
            self.roster.record(pii_type, text, pseudonym, self.document_name)

    def forget(self, pii_type: PiiType, text: str, discriminator: str = "") -> None:
        """Drop a mapping so the next request regenerates it.

        Used when the user retypes a value: a date that was pseudonymised as a
        name must not keep that name once it is known to be a date.
        """
        key = self.key_for(pii_type, text, discriminator)
        record = self._records.pop(key, None)
        if record is not None:
            self._taken.discard(record.pseudonym.lower())

    def lookup(self, pii_type: PiiType, text: str, discriminator: str = "") -> Optional[EntityRecord]:
        return self._records.get(self.key_for(pii_type, text, discriminator))

    @property
    def records(self) -> list[EntityRecord]:
        return list(self._records.values())
