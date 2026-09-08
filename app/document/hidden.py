"""Text that is not on the page (spec sections 42 and 100).

A PDF carries identity in four places the page-text extractor never sees:

    metadata      File > Properties. Title, Author, Subject, Keywords.
                  Tax software and scanners stamp client names here routinely.
    annotations   Sticky notes, comments, highlights, stamps. A preparer's note
                  saying "confirm SSN 123-45-6789" survives page redaction whole.
    attachments   Entire files embedded inside the PDF.
    bookmarks     The navigation outline, very often "Smith, John - 2024 1040".

Before this module, a document could pass every verification check and still
carry the client's name in its properties. These items have no page geometry, so
they are not redacted in place - they are rewritten or removed outright.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import pymupdf

#: Metadata keys worth scanning. `format` and `encryption` are structural.
SCANNED_METADATA_KEYS = (
    "title", "author", "subject", "keywords", "creator", "producer",
)

#: Keys cleared outright rather than pseudonymised - they never carry meaning
#: worth preserving and frequently carry a name.
ALWAYS_CLEAR = ("author", "keywords")


class HiddenKind(str, Enum):
    METADATA = "metadata"
    ANNOTATION = "annotation"
    ATTACHMENT = "attachment"
    BOOKMARK = "bookmark"


@dataclass
class HiddenItem:
    """One piece of text living outside the page content."""

    kind: HiddenKind
    key: str            # metadata key, annotation id, filename, or outline path
    text: str
    page_no: Optional[int] = None
    detail: str = ""

    @property
    def label(self) -> str:
        where = f"page {self.page_no + 1}" if self.page_no is not None else "document"
        return f"{self.kind.value}:{self.key} ({where})"


@dataclass
class HiddenContent:
    items: list[HiddenItem] = field(default_factory=list)

    @property
    def texts(self) -> list[str]:
        return [item.text for item in self.items if item.text.strip()]

    def of_kind(self, kind: HiddenKind) -> list[HiddenItem]:
        return [item for item in self.items if item.kind is kind]

    def __bool__(self) -> bool:
        return bool(self.items)


def extract_hidden(path: str) -> HiddenContent:
    """Read everything identity-bearing that is not page text."""
    content = HiddenContent()
    doc = pymupdf.open(path)
    try:
        metadata = doc.metadata or {}
        for key in SCANNED_METADATA_KEYS:
            value = (metadata.get(key) or "").strip()
            if value:
                content.items.append(
                    HiddenItem(HiddenKind.METADATA, key, value, detail="document properties")
                )

        for page in doc:
            for annot in page.annots() or []:
                info = annot.info or {}
                for field_name in ("content", "title", "subject"):
                    value = (info.get(field_name) or "").strip()
                    if value:
                        content.items.append(
                            HiddenItem(
                                HiddenKind.ANNOTATION,
                                f"{annot.xref}:{field_name}",
                                value,
                                page_no=page.number,
                                detail=annot.type[1] if annot.type else "annotation",
                            )
                        )

        for index in range(doc.embfile_count()):
            try:
                info = doc.embfile_info(index)
            except Exception:  # noqa: BLE001 - a malformed entry is still reportable
                continue
            name = info.get("filename") or info.get("name") or f"attachment-{index}"
            content.items.append(
                HiddenItem(
                    HiddenKind.ATTACHMENT,
                    str(name),
                    str(name),
                    detail=f"{info.get('length', 0)} bytes embedded in the file",
                )
            )

        for level, title, page_no, *_rest in doc.get_toc(simple=True) or []:
            if title and title.strip():
                content.items.append(
                    HiddenItem(
                        HiddenKind.BOOKMARK,
                        f"level{level}",
                        title.strip(),
                        page_no=max(0, page_no - 1) if page_no else None,
                        detail="navigation outline",
                    )
                )
    finally:
        doc.close()
    return content


def sanitize_hidden(
    doc: pymupdf.Document, replacements: dict[str, str], drop_attachments: bool = True
) -> list[str]:
    """Rewrite or strip hidden content in an open document.

    `replacements` maps original text to its pseudonym. Anything not covered is
    cleared rather than left, because hidden text cannot be reviewed visually and
    the safe default for an unreviewable field is empty.
    """
    actions: list[str] = []

    metadata = dict(doc.metadata or {})
    updated = dict(metadata)
    for key in SCANNED_METADATA_KEYS:
        value = (metadata.get(key) or "").strip()
        if not value:
            continue
        if key in ALWAYS_CLEAR:
            updated[key] = ""
            actions.append(f"cleared metadata:{key}")
            continue
        rewritten = _apply(value, replacements)
        if rewritten != value:
            updated[key] = rewritten
            actions.append(f"rewrote metadata:{key}")
        elif key in ("title", "subject"):
            # Untouched means nothing matched a known entity; it may still name
            # the client, and no one will ever look at it.
            updated[key] = ""
            actions.append(f"cleared metadata:{key}")
    doc.set_metadata(updated)

    for page in doc:
        for annot in list(page.annots() or []):
            page.delete_annot(annot)
            actions.append(f"removed annotation on page {page.number + 1}")

    if drop_attachments:
        for index in reversed(range(doc.embfile_count())):
            try:
                name = doc.embfile_info(index).get("filename", str(index))
                doc.embfile_del(index)
                actions.append(f"removed attachment {name}")
            except Exception:  # noqa: BLE001
                continue

    toc = doc.get_toc(simple=True) or []
    if toc:
        rewritten_toc = []
        changed = False
        for entry in toc:
            level, title, page_no = entry[0], entry[1], entry[2]
            new_title = _apply(title, replacements)
            if new_title == title:
                new_title = f"Section {len(rewritten_toc) + 1}"
            if new_title != title:
                changed = True
            rewritten_toc.append([level, new_title, page_no])
        if changed:
            doc.set_toc(rewritten_toc)
            actions.append(f"rewrote {len(rewritten_toc)} bookmark(s)")

    return actions


def _apply(text: str, replacements: dict[str, str]) -> str:
    import re

    out = text
    for original, pseudonym in sorted(replacements.items(), key=lambda kv: -len(kv[0])):
        if len(original) < 3:
            continue
        out = re.sub(
            rf"(?<![A-Za-z0-9]){re.escape(original)}(?![A-Za-z0-9])",
            pseudonym,
            out,
            flags=re.I,
        )
    return out


def residual_hidden_pii(path: str, originals: list[str]) -> list[str]:
    """Any accepted original still present in hidden content of a saved file."""
    import re

    content = extract_hidden(path)
    haystack = " \n ".join(content.texts).lower()
    survivors = []
    for original in originals:
        needle = " ".join(original.split()).strip().lower()
        if len(needle) < 3:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack):
            survivors.append(original)
    return survivors
