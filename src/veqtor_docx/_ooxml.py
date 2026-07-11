# SPDX-License-Identifier: Apache-2.0
"""Shared OOXML constants and helpers for WordprocessingML parsing."""

from __future__ import annotations

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


class DocxError(ValueError):
    """Raised when a file cannot be read as a DOCX package."""


def w(tag: str) -> str:
    """Return the fully qualified name for a ``w:`` tag."""
    return f"{{{W_NS}}}{tag}"


# Tracked-change wrapper elements that carry run content.
TEXT_REVISION_TAGS = frozenset(w(name) for name in TEXT_REVISION_NAMES_V1)
MOVE_REVISION_TAGS = frozenset(w(name) for name in MOVE_REVISION_NAMES_V1)

# Revision markup M1 does not extract as change units. These are counted and
# reported so the caller knows facts were present but not decoded.
UNSUPPORTED_REVISION_TAGS = frozenset(
    w(name) for name in UNSUPPORTED_REVISION_NAMES_V1
)


def run_text(element: etree._Element) -> str:
    """Concatenate visible text of runs under ``element``.

    Maps tabs and breaks to whitespace so extracted quotes stay searchable.
    Both ``w:t`` and ``w:delText`` are read; the caller decides which side of
    a tracked change the element belongs to.
    """
    parts: list[str] = []
    for node in element.iter():
        tag = node.tag
        if tag == w("t") or tag == w("delText"):
            parts.append(node.text or "")
        elif tag == w("tab"):
            parts.append("\t")
        elif tag in (w("br"), w("cr")):
            parts.append("\n")
        elif tag == w("noBreakHyphen"):
            parts.append("-")
    return "".join(parts)


def parse_xml(data: bytes) -> etree._Element:
    """Parse an OOXML part; malformed XML is a DocxError, not a raw lxml
    exception — the whole read path shares one fail-closed boundary."""
    try:
        return etree.fromstring(data)
    except etree.XMLSyntaxError as exc:
        raise DocxError(f"malformed XML: {exc}") from exc
