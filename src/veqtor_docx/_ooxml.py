# SPDX-License-Identifier: Apache-2.0
"""Shared OOXML constants and helpers for WordprocessingML parsing."""

from __future__ import annotations

import os
import zipfile
import zlib
from pathlib import Path

from lxml import etree

from .contracts import (
    DOCUMENT_PART_V1,
    MOVE_REVISION_NAMES_V1,
    TEXT_REVISION_NAMES_V1,
    UNSUPPORTED_REVISION_NAMES_V1,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NSMAP = {"w": W_NS}

DOCUMENT_PART = DOCUMENT_PART_V1
MAX_TRACKED_CHANGE_AUTHOR_LENGTH = 255

try:
    import lzma as _lzma
except ImportError:
    _LZMA_READ_ERRORS: tuple[type[BaseException], ...] = ()
else:
    _LZMA_READ_ERRORS = (_lzma.LZMAError,)

# Expected failures while reading a ZIP-backed DOCX package. Keep this tuple
# shared so discovery, extraction, and editing expose the same controlled
# boundary for the same input bytes.
ZIP_READ_ERRORS = (
    EOFError,
    OSError,
    RuntimeError,
    NotImplementedError,
    UnicodeError,
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    zlib.error,
) + _LZMA_READ_ERRORS


class DocxError(ValueError):
    """Raised when a file cannot be read as a DOCX package."""


class UserPathError(ValueError):
    """A stable refusal before any filesystem operation is attempted."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def w(tag: str) -> str:
    """Return the fully qualified name for a ``w:`` tag."""
    return f"{{{W_NS}}}{tag}"


def is_xml_text_compatible(value: str) -> bool:
    """Whether every character is allowed by the XML 1.0 ``Char`` rule."""
    return all(
        code in (0x09, 0x0A, 0x0D)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
        for code in map(ord, value)
    )


def tracked_change_author_validation_error(value: object) -> str | None:
    """Return a stable validation detail for a tracked-change author."""
    if not isinstance(value, str):
        return "tracked-change author must be a string"
    if not value.strip():
        return "tracked-change author must not be blank"
    if len(value) > MAX_TRACKED_CHANGE_AUTHOR_LENGTH:
        return (
            "tracked-change author must be at most "
            f"{MAX_TRACKED_CHANGE_AUTHOR_LENGTH} characters"
        )
    if not is_xml_text_compatible(value) or any(ord(char) < 0x20 for char in value):
        return "tracked-change author contains characters invalid in XML"
    return None


def resolve_user_path(value: object) -> str:
    """Resolve one text path without leaking ``pathlib`` exceptions."""
    if not isinstance(value, (str, os.PathLike)):
        raise UserPathError(
            "invalid_path", "path must be a string or path-like object"
        )
    try:
        raw = os.fspath(value)
    except Exception:
        raise UserPathError(
            "invalid_path", "path must be a string or path-like object"
        ) from None
    if not isinstance(raw, str):
        raise UserPathError("invalid_path", "path must resolve to text")
    if "\x00" in raw:
        raise UserPathError("invalid_path", "path contains a NUL character")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in raw):
        raise UserPathError(
            "invalid_path", "path contains an invalid Unicode scalar value"
        )
    try:
        return str(Path(raw).expanduser())
    except Exception as exc:
        if isinstance(exc, (KeyError, RuntimeError)):
            raise UserPathError(
                "path_unresolvable", "user home directory cannot be resolved"
            ) from exc
        raise UserPathError("invalid_path", "path cannot be resolved") from exc


# Tracked-change wrapper elements that carry run content.
TEXT_REVISION_TAGS = frozenset(w(name) for name in TEXT_REVISION_NAMES_V1)
MOVE_REVISION_TAGS = frozenset(w(name) for name in MOVE_REVISION_NAMES_V1)

# Revision markup M1 does not extract as change units. These are counted and
# reported so the caller knows facts were present but not decoded.
UNSUPPORTED_REVISION_TAGS = frozenset(
    w(name) for name in UNSUPPORTED_REVISION_NAMES_V1
)


def text_atom(
    node: etree._Element,
    *,
    include_deleted_text: bool = False,
) -> str | None:
    """Map one supported OOXML text atom to its current string value."""
    tag = node.tag
    if tag == w("t") or (include_deleted_text and tag == w("delText")):
        return node.text or ""
    if tag == w("tab"):
        return "\t"
    if tag in (w("br"), w("cr")):
        return "\n"
    if tag == w("noBreakHyphen"):
        return "-"
    return None


def current_text_atom(
    node: etree._Element,
    *,
    boundary: etree._Element | None = None,
) -> str | None:
    """Return one atom in the accepted/current reading, or ``None`` if hidden.

    ``boundary`` limits ancestor inspection to the paragraph or wrapper whose
    offsets are being built. Extraction and edit matching share this exact
    visibility rule so a quote emitted by one cannot become a false zero-match
    in the other.
    """
    contribution = text_atom(node)
    if contribution is None:
        return None
    for ancestor in node.iterancestors():
        if ancestor is boundary:
            break
        if ancestor.tag in (w("del"), w("moveFrom")):
            return None
    return contribution


def run_text(element: etree._Element) -> str:
    """Concatenate visible text of runs under ``element``.

    Maps tabs and breaks to whitespace so extracted quotes stay searchable.
    Both ``w:t`` and ``w:delText`` are read; the caller decides which side of
    a tracked change the element belongs to.
    """
    parts: list[str] = []
    for node in element.iter():
        value = text_atom(node, include_deleted_text=True)
        if value is not None:
            parts.append(value)
    return "".join(parts)


def parse_xml(data: bytes) -> etree._Element:
    """Parse an OOXML part; malformed XML is a DocxError, not a raw lxml
    exception — the whole read path shares one fail-closed boundary."""
    try:
        return etree.fromstring(data)
    except etree.XMLSyntaxError as exc:
        raise DocxError(f"malformed XML: {exc}") from exc
