# SPDX-License-Identifier: Apache-2.0
"""Shared OOXML constants and helpers for WordprocessingML parsing."""

from __future__ import annotations

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NSMAP = {"w": W_NS}

DOCUMENT_PART = "word/document.xml"


class DocxError(ValueError):
    """Raised when a file cannot be read as a DOCX package."""


def w(tag: str) -> str:
    """Return the fully qualified name for a ``w:`` tag."""
    return f"{{{W_NS}}}{tag}"


# Tracked-change wrapper elements that carry run content.
TEXT_REVISION_TAGS = frozenset({w("ins"), w("del")})
MOVE_REVISION_TAGS = frozenset({w("moveFrom"), w("moveTo")})

# Revision markup M1 does not extract as change units. These are counted and
# reported so the caller knows facts were present but not decoded.
UNSUPPORTED_REVISION_TAGS = frozenset(
    {
        w("rPrChange"),
        w("pPrChange"),
        w("tblPrChange"),
        w("trPrChange"),
        w("tcPrChange"),
        w("sectPrChange"),
        w("numberingChange"),
        w("cellIns"),
        w("cellDel"),
    }
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
