# SPDX-License-Identifier: Apache-2.0
"""Shared OOXML constants and helpers for WordprocessingML parsing."""

from __future__ import annotations

import os
import struct
import zipfile
import zlib
from pathlib import Path
from typing import BinaryIO

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

# Public-Alpha resource envelope.  DOCX packages are ZIP archives, so their
# compressed size is not enough to bound the work needed to inspect them.  All
# archive limits are checked from the central directory before any member is
# decompressed.
MIB = 1024 * 1024
MAX_DOCX_INPUT_BYTES = 50 * MIB
MAX_DOCX_ZIP_MEMBERS = 2_000
MAX_DOCX_CENTRAL_DIRECTORY_BYTES = 4 * MIB
MAX_DOCX_UNCOMPRESSED_BYTES = 100 * MIB
MAX_DOCX_XML_MEMBER_BYTES = 25 * MIB
MAX_DOCX_OTHER_MEMBER_BYTES = 50 * MIB
MAX_DOCX_XML_NODES = 100_000
MAX_DOCX_COMPRESSION_RATIO = 200
COMPRESSION_RATIO_MIN_UNCOMPRESSED_BYTES = 10 * MIB

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


class ResourceLimitError(DocxError):
    """A stable refusal for input that exceeds the safe processing envelope."""

    code = "resource_limit_exceeded"

    def __init__(self, limit: str, detail: str, **measurements: object) -> None:
        detail = f"{limit}: {detail}"
        super().__init__(f"{self.code}: {detail}")
        self.limit = limit
        self.detail = detail
        self.metadata = {"limit": limit, **measurements}


class DuplicateMemberError(DocxError):
    """A stable refusal for an ambiguous ZIP/OPC part identity."""

    code = "file_unextractable"

    def __init__(self) -> None:
        self.detail = "DOCX contains duplicate ZIP member names"
        super().__init__(f"{self.code}: {self.detail}")


class UserPathError(ValueError):
    """A stable refusal before any filesystem operation is attempted."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class _XmlResourceTarget:
    """Count structural XML items without building a second in-memory tree."""

    def __init__(self) -> None:
        self.node_count = 0

    def _add_nodes(self, count: int) -> None:
        self.node_count += count
        if self.node_count > MAX_DOCX_XML_NODES:
            raise ResourceLimitError(
                "xml_node_count",
                "XML part contains too many structural items",
                allowed_count=MAX_DOCX_XML_NODES,
                observed_count=self.node_count,
                observed_at_least=True,
            )

    def start(self, _tag: str, _attributes: dict[str, str]) -> None:
        self._add_nodes(1 + len(_attributes))

    def start_ns(self, _prefix: str | None, _uri: str) -> None:
        self._add_nodes(1)

    def end_ns(self, _prefix: str | None) -> None:
        return None

    def end(self, _tag: str) -> None:
        return None

    def data(self, _data: str) -> None:
        return None

    def comment(self, _text: str) -> None:
        self._add_nodes(1)

    def pi(self, _target: str, _data: str) -> None:
        self._add_nodes(1)

    def doctype(self, _name: str, _public_id: str, _system_id: str) -> None:
        raise DocxError("DOCTYPE declarations are unsupported")

    def close(self) -> None:
        return None


def _read_bounded(handle: BinaryIO) -> bytes:
    """Read at most one byte beyond the public DOCX input limit."""
    payload = handle.read(MAX_DOCX_INPUT_BYTES + 1)
    if len(payload) > MAX_DOCX_INPUT_BYTES:
        raise ResourceLimitError(
            "input_docx_bytes",
            "DOCX exceeds the "
            f"{MAX_DOCX_INPUT_BYTES // MIB} MiB compressed input limit",
            allowed_bytes=MAX_DOCX_INPUT_BYTES,
            observed_bytes=len(payload),
            observed_at_least=True,
        )
    return payload


def validate_docx_payload_size(
    payload: bytes,
    *,
    limit: str = "input_docx_bytes",
) -> None:
    """Reject an in-memory DOCX snapshot outside the public size envelope."""
    if len(payload) > MAX_DOCX_INPUT_BYTES:
        subject = "candidate DOCX" if limit == "candidate_docx_bytes" else "DOCX"
        raise ResourceLimitError(
            limit,
            f"{subject} exceeds the "
            f"{MAX_DOCX_INPUT_BYTES // MIB} MiB compressed size limit",
            allowed_bytes=MAX_DOCX_INPUT_BYTES,
            observed_bytes=len(payload),
        )


def read_docx_payload(path: str | os.PathLike[str]) -> bytes:
    """Read one bounded file snapshot without allocating beyond the limit."""
    with Path(path).open("rb") as handle:
        return _read_bounded(handle)


def validate_docx_central_directory(payload: bytes) -> None:
    """Bound the ZIP directory before ``ZipFile`` allocates ``ZipInfo`` objects."""
    eocd_signature = b"PK\x05\x06"
    eocd_size = 22
    search_start = max(0, len(payload) - (eocd_size + 65_535))
    search_end = len(payload)
    eocd_offset = -1
    while search_end > search_start:
        candidate = payload.rfind(eocd_signature, search_start, search_end)
        if candidate < 0:
            break
        if candidate + eocd_size <= len(payload):
            candidate_comment_size = struct.unpack_from(
                "<H", payload, candidate + 20
            )[0]
            if candidate + eocd_size + candidate_comment_size == len(payload):
                eocd_offset = candidate
                break
        search_end = candidate
    if eocd_offset < 0:
        raise DocxError("invalid ZIP central directory")

    (
        _signature,
        disk_number,
        directory_disk,
        entries_on_disk,
        entry_count,
        directory_size,
        directory_offset,
        comment_size,
    ) = struct.unpack_from("<4s4H2LH", payload, eocd_offset)
    if eocd_offset + eocd_size + comment_size != len(payload):
        raise DocxError("invalid ZIP end record")
    if disk_number != 0 or directory_disk != 0 or entries_on_disk != entry_count:
        raise DocxError("multi-disk ZIP packages are unsupported")
    if (
        entry_count == 0xFFFF
        or directory_size == 0xFFFFFFFF
        or directory_offset == 0xFFFFFFFF
    ):
        raise DocxError("ZIP64 central directories are unsupported")
    if entry_count > MAX_DOCX_ZIP_MEMBERS:
        raise ResourceLimitError(
            "zip_member_count",
            f"DOCX contains more than {MAX_DOCX_ZIP_MEMBERS} ZIP members",
            allowed_count=MAX_DOCX_ZIP_MEMBERS,
            observed_count=entry_count,
        )
    if directory_size > MAX_DOCX_CENTRAL_DIRECTORY_BYTES:
        raise ResourceLimitError(
            "central_directory_bytes",
            "DOCX ZIP central directory exceeds its safe size limit",
            allowed_bytes=MAX_DOCX_CENTRAL_DIRECTORY_BYTES,
            observed_bytes=directory_size,
        )
    if directory_offset + directory_size != eocd_offset:
        raise DocxError("invalid ZIP central-directory bounds")

    position = directory_offset
    observed_count = 0
    while position < eocd_offset:
        if (
            position + 46 > eocd_offset
            or payload[position : position + 4] != b"PK\x01\x02"
        ):
            raise DocxError("invalid ZIP central-directory entry")
        name_size, extra_size, member_comment_size = struct.unpack_from(
            "<HHH", payload, position + 28
        )
        position += 46 + name_size + extra_size + member_comment_size
        if position > eocd_offset:
            raise DocxError("invalid ZIP central-directory entry bounds")
        observed_count += 1
        if observed_count > MAX_DOCX_ZIP_MEMBERS:
            raise ResourceLimitError(
                "zip_member_count",
                f"DOCX contains more than {MAX_DOCX_ZIP_MEMBERS} ZIP members",
                allowed_count=MAX_DOCX_ZIP_MEMBERS,
                observed_count=observed_count,
                observed_at_least=True,
            )
    if observed_count != entry_count:
        raise DocxError("ZIP central-directory count mismatch")


def _is_xml_member(name: str) -> bool:
    lowered = name.casefold()
    return lowered.endswith(".xml") or lowered.endswith(".rels")


def validate_docx_archive(
    archive: zipfile.ZipFile,
) -> list[zipfile.ZipInfo]:
    """Validate declared ZIP resources before any member is decompressed.

    The ZIP central directory supplies compressed and uncompressed sizes.  A
    corrupt package may still fail later while being read, but no package that
    exceeds this declared envelope reaches ``ZipFile.read``.
    """
    infos = archive.infolist()
    if len(infos) > MAX_DOCX_ZIP_MEMBERS:
        raise ResourceLimitError(
            "zip_member_count",
            f"DOCX contains more than {MAX_DOCX_ZIP_MEMBERS} ZIP members",
            allowed_count=MAX_DOCX_ZIP_MEMBERS,
            observed_count=len(infos),
        )

    total_uncompressed = 0
    member_names: set[str] = set()
    for info in infos:
        if info.filename in member_names:
            raise DuplicateMemberError()
        member_names.add(info.filename)
        total_uncompressed += info.file_size
        is_xml = _is_xml_member(info.filename)
        member_limit = (
            MAX_DOCX_XML_MEMBER_BYTES
            if is_xml
            else MAX_DOCX_OTHER_MEMBER_BYTES
        )
        if info.file_size > member_limit:
            kind = "XML" if is_xml else "non-XML"
            raise ResourceLimitError(
                "xml_member_bytes" if is_xml else "other_member_bytes",
                f"{kind} ZIP member exceeds its safe uncompressed size limit",
                member_name=info.filename,
                allowed_bytes=member_limit,
                observed_bytes=info.file_size,
            )

        if info.file_size > COMPRESSION_RATIO_MIN_UNCOMPRESSED_BYTES:
            ratio_exceeded = (
                info.compress_size == 0
                or info.file_size
                > MAX_DOCX_COMPRESSION_RATIO * info.compress_size
            )
            if ratio_exceeded:
                observed_ratio = (
                    None
                    if info.compress_size == 0
                    else info.file_size / info.compress_size
                )
                raise ResourceLimitError(
                    "compression_ratio",
                    "ZIP member exceeds the safe "
                    f"{MAX_DOCX_COMPRESSION_RATIO}:1 compression ratio",
                    member_name=info.filename,
                    allowed_ratio=MAX_DOCX_COMPRESSION_RATIO,
                    observed_ratio=observed_ratio,
                    compressed_bytes=info.compress_size,
                    uncompressed_bytes=info.file_size,
                )

    if total_uncompressed > MAX_DOCX_UNCOMPRESSED_BYTES:
        raise ResourceLimitError(
            "total_uncompressed_bytes",
            "DOCX exceeds the "
            f"{MAX_DOCX_UNCOMPRESSED_BYTES // MIB} MiB total uncompressed "
            "size limit",
            allowed_bytes=MAX_DOCX_UNCOMPRESSED_BYTES,
            observed_bytes=total_uncompressed,
        )
    return infos


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
        preflight_parser = etree.XMLParser(
            collect_ids=False,
            huge_tree=False,
            load_dtd=False,
            no_network=True,
            recover=False,
            resolve_entities=False,
            target=_XmlResourceTarget(),
        )
        etree.fromstring(data, parser=preflight_parser)
        parser = etree.XMLParser(
            collect_ids=False,
            huge_tree=False,
            load_dtd=False,
            no_network=True,
            recover=False,
            resolve_entities=False,
        )
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise DocxError(f"malformed XML: {exc}") from exc
    if root.getroottree().docinfo.doctype:
        raise DocxError("DOCTYPE declarations are unsupported")
    return root
