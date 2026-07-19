# SPDX-License-Identifier: Apache-2.0
"""Shared OOXML constants and helpers for WordprocessingML parsing."""

from __future__ import annotations

import io
import os
import struct
import zipfile
import zlib
from collections.abc import Collection, Iterator
from dataclasses import dataclass
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
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
VML_NS = "urn:schemas-microsoft-com:vml"
NSMAP = {"w": W_NS}

DOCUMENT_PART = DOCUMENT_PART_V1
CANONICAL_BODY_FLOW_POLICY_V1 = "canonical_body_flow_v1"
MAX_TRACKED_CHANGE_AUTHOR_LENGTH = 255

# Public-Alpha resource envelope.  DOCX packages are ZIP archives, so their
# compressed size is not enough to bound the work needed to inspect them.
# Declared metadata is checked before decoder creation; actual output, CRC and
# end-of-stream are checked while every member is decoded within hard bounds.
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
ZIP_DECODE_CHUNK_BYTES = 64 * 1024
SUPPORTED_DOCX_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})

_ZIP_DESCRIPTOR_FLAG = 0x0008
_ZIP_UTF8_FLAG = 0x0800
_ZIP_ENCRYPTION_FLAGS = 0x0001 | 0x0040 | 0x2000
_ZIP_DEFLATE_OPTION_FLAGS = 0x0002 | 0x0004
_ZIP_ALLOWED_FLAGS = _ZIP_DESCRIPTOR_FLAG | _ZIP_UTF8_FLAG | _ZIP_DEFLATE_OPTION_FLAGS
_ZIP64_EXTRA_ID = 0x0001

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


class ExpandedOutputBudgetExceeded(ResourceLimitError):
    """The shared output budget for one larger operation is exhausted."""


@dataclass
class ExpandedOutputBudget:
    """Monotonically account for actual ZIP-member output across packages."""

    allowed_bytes: int
    limit: str
    consumed_bytes: int = 0

    def __post_init__(self) -> None:
        if self.allowed_bytes < 0:
            raise ValueError("expanded output budget must not be negative")
        if self.consumed_bytes < 0 or self.consumed_bytes > self.allowed_bytes:
            raise ValueError("expanded output consumption is outside its budget")

    @property
    def remaining_bytes(self) -> int:
        """Return the output still permitted before the first refused byte."""
        return max(0, self.allowed_bytes - self.consumed_bytes)

    def consume(self, byte_count: int) -> None:
        """Charge actual output, refusing at exactly one byte over the limit."""
        if byte_count < 0:
            raise ValueError("expanded output charge must not be negative")
        observed = self.consumed_bytes + byte_count
        if observed > self.allowed_bytes:
            observed = self.allowed_bytes + 1
            self.consumed_bytes = observed
            raise ExpandedOutputBudgetExceeded(
                self.limit,
                "decoded ZIP members exceed the aggregate expanded-output limit",
                allowed_bytes=self.allowed_bytes,
                observed_bytes=observed,
                observed_at_least=True,
            )
        self.consumed_bytes = observed


class ArchiveValidationError(DocxError):
    """A stable refusal for a structurally ambiguous ZIP package."""

    code = "file_unextractable"

    def __init__(self, detail: str, **metadata: object) -> None:
        self.detail = detail
        self.metadata = metadata
        super().__init__(f"{self.code}: {detail}")


class UnsupportedCompressionError(ArchiveValidationError):
    """A stable refusal before an unsupported decoder can be created."""

    code = "unsupported_compression"


class EncryptedDocxError(ArchiveValidationError):
    """A stable refusal for encrypted or masked ZIP members."""

    code = "encrypted_docx"


class DuplicateMemberError(ArchiveValidationError):
    """A stable refusal for an ambiguous ZIP/OPC part identity."""

    def __init__(self) -> None:
        super().__init__("DOCX contains duplicate ZIP member names")


@dataclass(frozen=True)
class _CentralEntry:
    filename: str
    raw_filename: bytes
    flags: int
    compress_type: int
    crc: int
    compress_size: int
    file_size: int
    local_header_offset: int
    extra: bytes


@dataclass(frozen=True)
class _CentralDirectory:
    offset: int
    size: int
    eocd_offset: int
    entries: tuple[_CentralEntry, ...]


@dataclass(frozen=True)
class ValidatedDocx:
    """One eagerly validated package with only requested parts retained."""

    infos: tuple[zipfile.ZipInfo, ...]
    parts: dict[str, bytes]
    member_names: frozenset[str]
    expanded_bytes: int


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


def _parse_extra_fields(
    extra: bytes,
    *,
    member_name: str,
    location: str,
) -> frozenset[int]:
    """Parse one ZIP extra-field sequence without trusting its inner lengths."""
    field_ids: set[int] = set()
    position = 0
    while position < len(extra):
        if position + 4 > len(extra):
            raise ArchiveValidationError(
                f"malformed {location} ZIP extra fields",
                member_name=member_name,
            )
        field_id, field_size = struct.unpack_from("<HH", extra, position)
        position += 4
        field_end = position + field_size
        if field_end > len(extra):
            raise ArchiveValidationError(
                f"malformed {location} ZIP extra fields",
                member_name=member_name,
            )
        field_ids.add(field_id)
        position = field_end
    return frozenset(field_ids)


def _decode_zip_filename(raw_name: bytes, flags: int) -> str:
    encoding = "utf-8" if flags & _ZIP_UTF8_FLAG else "cp437"
    try:
        filename = raw_name.decode(encoding)
    except UnicodeError as exc:
        raise ArchiveValidationError("invalid ZIP member filename encoding") from exc
    if not filename or "\x00" in filename:
        raise ArchiveValidationError("invalid ZIP member filename")
    return filename


def _is_xml_member(name: str) -> bool:
    lowered = name.casefold()
    return lowered.endswith(".xml") or lowered.endswith(".rels")


def _member_size_limit(name: str) -> tuple[str, int, str]:
    if _is_xml_member(name):
        return "xml_member_bytes", MAX_DOCX_XML_MEMBER_BYTES, "XML"
    return "other_member_bytes", MAX_DOCX_OTHER_MEMBER_BYTES, "non-XML"


def _validate_member_policy(name: str, flags: int, compress_type: int) -> None:
    if flags & _ZIP_ENCRYPTION_FLAGS:
        raise EncryptedDocxError(
            "encrypted ZIP members are unsupported",
            member_name=name,
        )
    if compress_type not in SUPPORTED_DOCX_COMPRESSION:
        raise UnsupportedCompressionError(
            "ZIP member uses unsupported compression",
            member_name=name,
            compression_method=compress_type,
        )
    if flags & ~_ZIP_ALLOWED_FLAGS:
        raise ArchiveValidationError(
            "ZIP member uses unsupported general-purpose flags",
            member_name=name,
        )
    if compress_type == zipfile.ZIP_STORED and flags & _ZIP_DEFLATE_OPTION_FLAGS:
        raise ArchiveValidationError(
            "stored ZIP member declares DEFLATE-only flags",
            member_name=name,
        )


def _validate_declared_entries(entries: Collection[_CentralEntry]) -> None:
    if len(entries) > MAX_DOCX_ZIP_MEMBERS:
        raise ResourceLimitError(
            "zip_member_count",
            f"DOCX contains more than {MAX_DOCX_ZIP_MEMBERS} ZIP members",
            allowed_count=MAX_DOCX_ZIP_MEMBERS,
            observed_count=len(entries),
        )

    total_uncompressed = 0
    member_names: set[str] = set()
    for entry in entries:
        if entry.filename in member_names:
            raise DuplicateMemberError()
        member_names.add(entry.filename)
        _validate_member_policy(entry.filename, entry.flags, entry.compress_type)
        if _ZIP64_EXTRA_ID in _parse_extra_fields(
            entry.extra,
            member_name=entry.filename,
            location="central",
        ):
            raise ArchiveValidationError(
                "ZIP64 members are unsupported",
                member_name=entry.filename,
            )

        total_uncompressed += entry.file_size
        limit_name, member_limit, kind = _member_size_limit(entry.filename)
        if entry.file_size > member_limit:
            raise ResourceLimitError(
                limit_name,
                f"{kind} ZIP member exceeds its safe uncompressed size limit",
                member_name=entry.filename,
                allowed_bytes=member_limit,
                observed_bytes=entry.file_size,
            )

        if entry.file_size > COMPRESSION_RATIO_MIN_UNCOMPRESSED_BYTES:
            ratio_exceeded = (
                entry.compress_size == 0
                or entry.file_size > MAX_DOCX_COMPRESSION_RATIO * entry.compress_size
            )
            if ratio_exceeded:
                observed_ratio = (
                    None
                    if entry.compress_size == 0
                    else entry.file_size / entry.compress_size
                )
                raise ResourceLimitError(
                    "compression_ratio",
                    "ZIP member exceeds the safe "
                    f"{MAX_DOCX_COMPRESSION_RATIO}:1 compression ratio",
                    member_name=entry.filename,
                    allowed_ratio=MAX_DOCX_COMPRESSION_RATIO,
                    observed_ratio=observed_ratio,
                    compressed_bytes=entry.compress_size,
                    uncompressed_bytes=entry.file_size,
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


def validate_docx_central_directory(payload: bytes) -> _CentralDirectory:
    """Parse and bound the ZIP directory before ``ZipFile`` is constructed."""
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
            candidate_comment_size = struct.unpack_from("<H", payload, candidate + 20)[
                0
            ]
            if candidate + eocd_size + candidate_comment_size == len(payload):
                eocd_offset = candidate
                break
        search_end = candidate
    if eocd_offset < 0:
        raise ArchiveValidationError("invalid ZIP central directory")

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
        raise ArchiveValidationError("invalid ZIP end record")
    if disk_number != 0 or directory_disk != 0 or entries_on_disk != entry_count:
        raise ArchiveValidationError("multi-disk ZIP packages are unsupported")
    if (
        entry_count == 0xFFFF
        or directory_size == 0xFFFFFFFF
        or directory_offset == 0xFFFFFFFF
    ):
        raise ArchiveValidationError("ZIP64 central directories are unsupported")
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
        raise ArchiveValidationError("invalid ZIP central-directory bounds")

    position = directory_offset
    entries: list[_CentralEntry] = []
    while position < eocd_offset:
        if (
            position + 46 > eocd_offset
            or payload[position : position + 4] != b"PK\x01\x02"
        ):
            raise ArchiveValidationError("invalid ZIP central-directory entry")
        (
            _entry_signature,
            _version_made_by,
            _version_needed,
            flags,
            compress_type,
            _modified_time,
            _modified_date,
            crc,
            compress_size,
            file_size,
            name_size,
            extra_size,
            member_comment_size,
            disk_start,
            _internal_attributes,
            _external_attributes,
            local_header_offset,
        ) = struct.unpack_from("<4s6H3L5H2L", payload, position)
        entry_end = position + 46 + name_size + extra_size + member_comment_size
        if entry_end > eocd_offset:
            raise ArchiveValidationError("invalid ZIP central-directory entry bounds")
        if (
            disk_start == 0xFFFF
            or compress_size == 0xFFFFFFFF
            or file_size == 0xFFFFFFFF
            or local_header_offset == 0xFFFFFFFF
        ):
            raise ArchiveValidationError("ZIP64 members are unsupported")
        if disk_start != 0:
            raise ArchiveValidationError("multi-disk ZIP members are unsupported")
        raw_name_start = position + 46
        raw_name = payload[raw_name_start : raw_name_start + name_size]
        filename = _decode_zip_filename(raw_name, flags)
        extra_start = raw_name_start + name_size
        extra = payload[extra_start : extra_start + extra_size]
        entries.append(
            _CentralEntry(
                filename=filename,
                raw_filename=raw_name,
                flags=flags,
                compress_type=compress_type,
                crc=crc,
                compress_size=compress_size,
                file_size=file_size,
                local_header_offset=local_header_offset,
                extra=extra,
            )
        )
        if len(entries) > MAX_DOCX_ZIP_MEMBERS:
            raise ResourceLimitError(
                "zip_member_count",
                f"DOCX contains more than {MAX_DOCX_ZIP_MEMBERS} ZIP members",
                allowed_count=MAX_DOCX_ZIP_MEMBERS,
                observed_count=len(entries),
                observed_at_least=True,
            )
        position = entry_end
    if len(entries) != entry_count:
        raise ArchiveValidationError("ZIP central-directory count mismatch")
    _validate_declared_entries(entries)
    return _CentralDirectory(
        offset=directory_offset,
        size=directory_size,
        eocd_offset=eocd_offset,
        entries=tuple(entries),
    )


def _compare_zip_infos(
    entries: tuple[_CentralEntry, ...],
    infos: tuple[zipfile.ZipInfo, ...],
) -> None:
    if len(entries) != len(infos):
        raise ArchiveValidationError("ZIP central-directory count mismatch")
    for entry, info in zip(entries, infos, strict=True):
        if (
            info.filename != entry.filename
            or info.flag_bits != entry.flags
            or info.compress_type != entry.compress_type
            or info.CRC != entry.crc
            or info.compress_size != entry.compress_size
            or info.file_size != entry.file_size
            or info.header_offset != entry.local_header_offset
        ):
            raise ArchiveValidationError(
                "ZIP parser metadata mismatch",
                member_name=entry.filename,
            )


def _member_data_span(
    payload: bytes,
    entry: _CentralEntry,
    boundary: int,
) -> tuple[int, int]:
    offset = entry.local_header_offset
    if offset < 0 or offset + 30 > boundary:
        raise ArchiveValidationError(
            "invalid ZIP local-header bounds",
            member_name=entry.filename,
        )
    (
        signature,
        _version_needed,
        flags,
        compress_type,
        _modified_time,
        _modified_date,
        local_crc,
        local_compress_size,
        local_file_size,
        name_size,
        extra_size,
    ) = struct.unpack_from("<4s5H3L2H", payload, offset)
    if signature != b"PK\x03\x04":
        raise ArchiveValidationError(
            "invalid ZIP local-header signature",
            member_name=entry.filename,
        )
    header_end = offset + 30 + name_size + extra_size
    if header_end > boundary:
        raise ArchiveValidationError(
            "invalid ZIP local-header bounds",
            member_name=entry.filename,
        )
    raw_name = payload[offset + 30 : offset + 30 + name_size]
    local_extra = payload[offset + 30 + name_size : header_end]
    if raw_name != entry.raw_filename:
        raise ArchiveValidationError(
            "ZIP local and central filenames differ",
            member_name=entry.filename,
        )
    if _ZIP64_EXTRA_ID in _parse_extra_fields(
        local_extra,
        member_name=entry.filename,
        location="local",
    ):
        raise ArchiveValidationError(
            "ZIP64 members are unsupported",
            member_name=entry.filename,
        )
    if flags != entry.flags or compress_type != entry.compress_type:
        raise ArchiveValidationError(
            "ZIP local and central metadata differ",
            member_name=entry.filename,
        )
    if local_compress_size == 0xFFFFFFFF or local_file_size == 0xFFFFFFFF:
        raise ArchiveValidationError(
            "ZIP64 members are unsupported",
            member_name=entry.filename,
        )

    has_descriptor = bool(entry.flags & _ZIP_DESCRIPTOR_FLAG)
    if has_descriptor:
        if local_crc not in (0, entry.crc):
            raise ArchiveValidationError(
                "ZIP local CRC disagrees with central directory",
                member_name=entry.filename,
            )
        if local_compress_size not in (0, entry.compress_size):
            raise ArchiveValidationError(
                "ZIP local compressed size disagrees with central directory",
                member_name=entry.filename,
            )
        if local_file_size not in (0, entry.file_size):
            raise ArchiveValidationError(
                "ZIP local file size disagrees with central directory",
                member_name=entry.filename,
            )
    elif (
        local_crc != entry.crc
        or local_compress_size != entry.compress_size
        or local_file_size != entry.file_size
    ):
        raise ArchiveValidationError(
            "ZIP local and central sizes or CRC differ",
            member_name=entry.filename,
        )

    data_end = header_end + entry.compress_size
    if data_end > boundary:
        raise ArchiveValidationError(
            "ZIP member exceeds its local layout boundary",
            member_name=entry.filename,
        )
    if not has_descriptor:
        if data_end != boundary:
            raise ArchiveValidationError(
                "unexpected bytes after ZIP member data",
                member_name=entry.filename,
            )
        return header_end, data_end

    descriptor_size = boundary - data_end
    if descriptor_size == 16 and payload[data_end : data_end + 4] == b"PK\x07\x08":
        descriptor_crc, descriptor_compress_size, descriptor_file_size = (
            struct.unpack_from("<3L", payload, data_end + 4)
        )
    elif descriptor_size == 12:
        descriptor_crc, descriptor_compress_size, descriptor_file_size = (
            struct.unpack_from("<3L", payload, data_end)
        )
    else:
        raise ArchiveValidationError(
            "invalid ZIP data-descriptor boundary",
            member_name=entry.filename,
        )
    if (
        descriptor_crc != entry.crc
        or descriptor_compress_size != entry.compress_size
        or descriptor_file_size != entry.file_size
    ):
        raise ArchiveValidationError(
            "ZIP data descriptor disagrees with central directory",
            member_name=entry.filename,
        )
    return header_end, data_end


def _observe_member_output(
    entry: _CentralEntry,
    *,
    member_bytes: int,
    total_before: int,
) -> None:
    if member_bytes > entry.file_size:
        raise ArchiveValidationError(
            "ZIP member expands beyond its declared size",
            member_name=entry.filename,
            declared_bytes=entry.file_size,
            observed_bytes=member_bytes,
            observed_at_least=True,
        )
    limit_name, member_limit, kind = _member_size_limit(entry.filename)
    if member_bytes > member_limit:
        raise ResourceLimitError(
            limit_name,
            f"{kind} ZIP member exceeds its safe uncompressed size limit",
            member_name=entry.filename,
            allowed_bytes=member_limit,
            observed_bytes=member_bytes,
            observed_at_least=True,
        )
    if total_before + member_bytes > MAX_DOCX_UNCOMPRESSED_BYTES:
        raise ResourceLimitError(
            "total_uncompressed_bytes",
            "DOCX exceeds its safe total uncompressed size limit",
            allowed_bytes=MAX_DOCX_UNCOMPRESSED_BYTES,
            observed_bytes=total_before + member_bytes,
            observed_at_least=True,
        )
    if member_bytes > COMPRESSION_RATIO_MIN_UNCOMPRESSED_BYTES and (
        entry.compress_size == 0
        or member_bytes > MAX_DOCX_COMPRESSION_RATIO * entry.compress_size
    ):
        observed_ratio = (
            None if entry.compress_size == 0 else member_bytes / entry.compress_size
        )
        raise ResourceLimitError(
            "compression_ratio",
            "ZIP member exceeds the safe "
            f"{MAX_DOCX_COMPRESSION_RATIO}:1 compression ratio",
            member_name=entry.filename,
            allowed_ratio=MAX_DOCX_COMPRESSION_RATIO,
            observed_ratio=observed_ratio,
            compressed_bytes=entry.compress_size,
            uncompressed_bytes=member_bytes,
            observed_at_least=True,
        )


def _decode_deflated_member(
    payload: bytes,
    entry: _CentralEntry,
    start: int,
    end: int,
    *,
    total_before: int,
    capture: bool,
    expanded_budget: ExpandedOutputBudget | None,
) -> tuple[bytes | None, int]:
    try:
        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
    except zlib.error as exc:
        raise ArchiveValidationError(
            "cannot initialize DEFLATE decoder",
            member_name=entry.filename,
        ) from exc
    captured: list[bytes] | None = [] if capture else None
    member_bytes = 0
    crc = 0
    compressed_position = start
    pending = b""

    while compressed_position < end or pending:
        if not pending:
            chunk_end = min(end, compressed_position + ZIP_DECODE_CHUNK_BYTES)
            pending = payload[compressed_position:chunk_end]
            compressed_position = chunk_end
        pending_size = len(pending)
        max_output = min(
            ZIP_DECODE_CHUNK_BYTES,
            entry.file_size - member_bytes + 1,
            _member_size_limit(entry.filename)[1] - member_bytes + 1,
            MAX_DOCX_UNCOMPRESSED_BYTES - total_before - member_bytes + 1,
        )
        if expanded_budget is not None:
            max_output = min(max_output, expanded_budget.remaining_bytes + 1)
        if max_output <= 0:
            _observe_member_output(
                entry,
                member_bytes=member_bytes + 1,
                total_before=total_before,
            )
            raise ArchiveValidationError(
                "ZIP member exceeds its bounded output envelope",
                member_name=entry.filename,
            )
        try:
            output = decoder.decompress(pending, max_output)
        except zlib.error as exc:
            raise ArchiveValidationError(
                "invalid DEFLATE stream",
                member_name=entry.filename,
            ) from exc
        pending = decoder.unconsumed_tail
        if output:
            member_bytes += len(output)
            if expanded_budget is not None:
                expanded_budget.consume(len(output))
            _observe_member_output(
                entry,
                member_bytes=member_bytes,
                total_before=total_before,
            )
            crc = zlib.crc32(output, crc)
            if captured is not None:
                captured.append(output)
        if decoder.unused_data:
            raise ArchiveValidationError(
                "trailing bytes follow the DEFLATE stream",
                member_name=entry.filename,
            )
        if decoder.eof:
            if pending or compressed_position != end:
                raise ArchiveValidationError(
                    "trailing bytes follow the DEFLATE stream",
                    member_name=entry.filename,
                )
            break
        if not output and len(pending) == pending_size:
            raise ArchiveValidationError(
                "DEFLATE decoder made no progress",
                member_name=entry.filename,
            )

    if not decoder.eof:
        raise ArchiveValidationError(
            "truncated DEFLATE stream",
            member_name=entry.filename,
        )
    if member_bytes != entry.file_size or crc != entry.crc:
        raise ArchiveValidationError(
            "ZIP member size or CRC mismatch",
            member_name=entry.filename,
            declared_bytes=entry.file_size,
            observed_bytes=member_bytes,
        )
    return (b"".join(captured) if captured is not None else None), member_bytes


def _decode_stored_member(
    payload: bytes,
    entry: _CentralEntry,
    start: int,
    end: int,
    *,
    total_before: int,
    capture: bool,
    expanded_budget: ExpandedOutputBudget | None,
) -> tuple[bytes | None, int]:
    member_bytes = end - start
    if expanded_budget is not None:
        expanded_budget.consume(member_bytes)
    _observe_member_output(
        entry,
        member_bytes=member_bytes,
        total_before=total_before,
    )
    view = memoryview(payload)[start:end]
    crc = zlib.crc32(view)
    if member_bytes != entry.file_size or crc != entry.crc:
        raise ArchiveValidationError(
            "ZIP member size or CRC mismatch",
            member_name=entry.filename,
            declared_bytes=entry.file_size,
            observed_bytes=member_bytes,
        )
    return (bytes(view) if capture else None), member_bytes


def load_validated_docx(
    payload: bytes,
    *,
    capture: Collection[str] | None,
    expanded_budget: ExpandedOutputBudget | None = None,
) -> ValidatedDocx:
    """Validate every member and retain only the requested uncompressed bytes."""
    validate_docx_payload_size(payload)
    directory = validate_docx_central_directory(payload)
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = tuple(archive.infolist())
    except ZIP_READ_ERRORS as exc:
        raise ArchiveValidationError("invalid ZIP central directory") from exc
    _compare_zip_infos(directory.entries, infos)

    layout_entries = sorted(
        directory.entries,
        key=lambda entry: entry.local_header_offset,
    )
    if layout_entries and layout_entries[0].local_header_offset != 0:
        raise ArchiveValidationError("unexpected bytes before first ZIP member")
    if len({entry.local_header_offset for entry in layout_entries}) != len(
        layout_entries
    ):
        raise ArchiveValidationError("overlapping ZIP local headers")

    spans: dict[str, tuple[int, int]] = {}
    for index, entry in enumerate(layout_entries):
        boundary = (
            layout_entries[index + 1].local_header_offset
            if index + 1 < len(layout_entries)
            else directory.offset
        )
        spans[entry.filename] = _member_data_span(payload, entry, boundary)

    capture_names = None if capture is None else frozenset(capture)
    parts: dict[str, bytes] = {}
    total_expanded = 0
    for entry in directory.entries:
        start, end = spans[entry.filename]
        should_capture = capture_names is None or entry.filename in capture_names
        if entry.compress_type == zipfile.ZIP_STORED:
            part, member_bytes = _decode_stored_member(
                payload,
                entry,
                start,
                end,
                total_before=total_expanded,
                capture=should_capture,
                expanded_budget=expanded_budget,
            )
        else:
            part, member_bytes = _decode_deflated_member(
                payload,
                entry,
                start,
                end,
                total_before=total_expanded,
                capture=should_capture,
                expanded_budget=expanded_budget,
            )
        total_expanded += member_bytes
        if part is not None:
            parts[entry.filename] = part

    return ValidatedDocx(
        infos=infos,
        parts=parts,
        member_names=frozenset(entry.filename for entry in directory.entries),
        expanded_bytes=total_expanded,
    )


def w(tag: str) -> str:
    """Return the fully qualified name for a ``w:`` tag."""
    return f"{{{W_NS}}}{tag}"


def require_single_direct_document_body(
    document: etree._Element,
) -> etree._Element:
    """Return the sole direct ``w:body`` of a ``w:document`` root.

    Extraction and bounded inspection must share this structural gate: reading
    only the first body while another consumer inventories the entire XML tree
    can otherwise make their coverage claims contradict each other.
    """
    direct_bodies = (
        [child for child in document if child.tag == w("body")]
        if document.tag == w("document")
        else []
    )
    if len(direct_bodies) != 1:
        raise DocxError("word/document.xml must contain exactly one direct w:body")
    return direct_bodies[0]


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
        raise UserPathError("invalid_path", "path must be a string or path-like object")
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
UNSUPPORTED_REVISION_TAGS = frozenset(w(name) for name in UNSUPPORTED_REVISION_NAMES_V1)


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


@dataclass(frozen=True)
class CanonicalParagraph:
    """One supported paragraph in the versioned body-flow index."""

    element: etree._Element
    paragraph_index: int
    container_kind: str  # "body" | "table_cell"


@dataclass(frozen=True)
class CanonicalExcludedSubtree:
    """One non-overlapping subtree pruned from canonical paragraph reading."""

    element: etree._Element
    kind: str


@dataclass(frozen=True)
class CanonicalBodyFlow:
    """Deterministic paragraph membership and exclusion facts for one body."""

    paragraphs: tuple[CanonicalParagraph, ...]
    excluded_subtrees: tuple[CanonicalExcludedSubtree, ...]
    excluded_node_kinds: dict[etree._Element, str]
    excluded_paragraph_count: int
    excluded_paragraphs_by_kind: dict[str, int]

    def exclusion_kind_for(self, element: etree._Element) -> str | None:
        """Return the pruned-container reason that owns ``element``, if any."""
        return self.excluded_node_kinds.get(element)

    @property
    def container_policy(self) -> dict[str, object]:
        excluded_by_kind: dict[str, int] = {}
        for excluded in self.excluded_subtrees:
            excluded_by_kind[excluded.kind] = excluded_by_kind.get(excluded.kind, 0) + 1
        body_count = sum(
            paragraph.container_kind == "body" for paragraph in self.paragraphs
        )
        table_count = len(self.paragraphs) - body_count
        return {
            "schema_version": CANONICAL_BODY_FLOW_POLICY_V1,
            "indexed_paragraph_count": len(self.paragraphs),
            "body_paragraph_count": body_count,
            "table_cell_paragraph_count": table_count,
            "excluded_subtree_count": len(self.excluded_subtrees),
            "excluded_paragraph_count": self.excluded_paragraph_count,
            "excluded_by_kind": excluded_by_kind,
            "excluded_paragraphs_by_kind": dict(self.excluded_paragraphs_by_kind),
            "coverage_complete": not self.excluded_subtrees,
            # v0.2 two-field anchors carry no traversal-policy identity. They
            # are safe to resolve under v0.3 only when filtering changed no
            # paragraph membership or reading surface for these exact bytes.
            "legacy_two_field_anchor_safe": not self.excluded_subtrees,
        }


_MC_ALTERNATE_CONTENT_TAGS = frozenset(
    f"{{{MC_NS}}}{name}" for name in ("AlternateContent", "Choice", "Fallback")
)
_TEXT_BOX_TAGS = frozenset(
    {
        w("drawing"),
        w("object"),
        w("pict"),
        w("txbxContent"),
        f"{{{VML_NS}}}textbox",
    }
)
_CANONICAL_BLOCK_PASS_THROUGH = frozenset(
    {w("tbl"), w("tr"), w("tc"), w("sdt"), w("sdtContent")}
)
_CANONICAL_INLINE_PASS_THROUGH = frozenset(
    {
        w("bdo"),
        w("customXml"),
        w("del"),
        w("dir"),
        w("fldSimple"),
        w("hyperlink"),
        w("ins"),
        w("moveFrom"),
        w("moveTo"),
        w("r"),
        w("sdt"),
        w("sdtContent"),
        w("smartTag"),
    }
)
_CANONICAL_PROPERTY_SUBTREES = frozenset(
    {
        w("customXmlPr"),
        w("pPr"),
        w("rPr"),
        w("sdtEndPr"),
        w("sdtPr"),
        w("sectPr"),
        w("tblGrid"),
        w("tblPr"),
        w("tblPrEx"),
        w("tcPr"),
        w("trPr"),
    }
)
_CANONICAL_STRUCTURAL_REVISION_PROPERTY_TAGS = frozenset(
    {w("trPr"), w("tcPr"), w("tblPr"), w("sectPr")}
)
_CANONICAL_TEXT_ATOM_TAGS = frozenset(
    {w("t"), w("delText"), w("tab"), w("br"), w("cr"), w("noBreakHyphen")}
)


def _subtree_has_canonical_payload(element: etree._Element) -> bool:
    return any(
        node.tag in {w("p"), w("altChunk")}
        or node.tag in _CANONICAL_TEXT_ATOM_TAGS
        or node.tag in TEXT_REVISION_TAGS
        or node.tag in MOVE_REVISION_TAGS
        or node.tag in UNSUPPORTED_REVISION_TAGS
        for node in element.iter()
    )


def _property_subtree_has_illegal_payload(element: etree._Element) -> bool:
    """Detect rendered or text-revision payload hidden in Word properties.

    Property subtrees normally carry only formatting metadata.  The two text
    revision shapes below are legitimate property/structural revision markup
    and remain in-scope unsupported inventory.  Any paragraph, rendered text,
    move revision, or differently nested insertion/deletion is not part of the
    canonical body reading and must become a fail-visible exclusion.
    """
    for node in element.iter():
        if node is element:
            continue
        if (
            node.tag in {w("p"), w("altChunk")}
            or node.tag in _CANONICAL_TEXT_ATOM_TAGS
            or node.tag in MOVE_REVISION_TAGS
        ):
            return True
        if node.tag not in TEXT_REVISION_TAGS:
            continue
        parent = node.getparent()
        if parent is None:
            return True
        if parent.tag in _CANONICAL_STRUCTURAL_REVISION_PROPERTY_TAGS:
            continue
        grandparent = parent.getparent()
        if (
            parent.tag == w("rPr")
            and grandparent is not None
            and grandparent.tag == w("pPr")
        ):
            continue
        return True
    return False


def _explicit_exclusion_kind(element: etree._Element) -> str | None:
    # altChunk imports relationship-backed content (commonly HTML) that is not
    # present below the w:altChunk element itself. It must therefore be an
    # unconditional, fail-visible exclusion rather than relying on descendant
    # payload detection.
    if element.tag == w("altChunk"):
        return "alt_chunk"
    if element.tag in _MC_ALTERNATE_CONTENT_TAGS:
        return "alternate_content"
    if element.tag in _TEXT_BOX_TAGS:
        return "text_box"
    if element.tag == w("p"):
        return "nested_paragraph"
    return None


def _canonical_inline_child_kind(element: etree._Element) -> tuple[bool, str | None]:
    """Return (traverse, exclusion_kind) for one paragraph descendant."""
    explicit = _explicit_exclusion_kind(element)
    if explicit is not None:
        return False, explicit
    if element.tag in _CANONICAL_PROPERTY_SUBTREES:
        return (
            False,
            "unknown_container"
            if _property_subtree_has_illegal_payload(element)
            else None,
        )
    if (
        element.tag in _CANONICAL_INLINE_PASS_THROUGH
        or element.tag in _CANONICAL_TEXT_ATOM_TAGS
    ):
        return True, None
    if _subtree_has_canonical_payload(element):
        return False, "unknown_container"
    return False, None


def canonical_paragraph_children(
    element: etree._Element,
) -> Iterator[etree._Element]:
    """Yield supported children, pruning the v1 excluded-container boundary."""
    for child in element:
        traverse, _ = _canonical_inline_child_kind(child)
        if traverse:
            yield child


def iter_canonical_paragraph_nodes(
    element: etree._Element,
) -> Iterator[etree._Element]:
    """Pre-order paragraph traversal with excluded subtrees removed."""
    yield element
    for child in canonical_paragraph_children(element):
        yield from iter_canonical_paragraph_nodes(child)


def canonical_run_text(
    element: etree._Element,
    *,
    include_deleted_text: bool = True,
) -> str:
    """Read a run/wrapper without leaking text from excluded subtrees."""
    parts: list[str] = []
    for node in iter_canonical_paragraph_nodes(element):
        value = text_atom(node, include_deleted_text=include_deleted_text)
        if value is not None:
            parts.append(value)
    return "".join(parts)


def _inline_excluded_roots(paragraph: etree._Element) -> list[CanonicalExcludedSubtree]:
    excluded: list[CanonicalExcludedSubtree] = []

    def walk(element: etree._Element) -> None:
        for child in element:
            traverse, kind = _canonical_inline_child_kind(child)
            if kind is not None:
                excluded.append(CanonicalExcludedSubtree(child, kind))
            elif traverse:
                walk(child)

    walk(paragraph)
    return excluded


def canonical_body_flow_v1(body: etree._Element) -> CanonicalBodyFlow:
    """Classify body paragraphs and build one non-overlapping pruning policy.

    Paragraph identity is positional within this filtered, deterministic index.
    Content controls are transparent; text boxes, AlternateContent, nested
    paragraphs and unknown text-bearing wrappers are fail-visible exclusions.
    """
    if body.tag != w("body"):
        raise ValueError("canonical_body_flow_v1 requires a w:body element")

    supported: list[tuple[etree._Element, str]] = []
    roots: list[CanonicalExcludedSubtree] = []

    def exclude(element: etree._Element, kind: str) -> None:
        # Traversal stops at the first unsupported boundary, so these roots are
        # non-overlapping by construction and every hidden node has one owner.
        roots.append(CanonicalExcludedSubtree(element, kind))

    def walk_block(element: etree._Element, *, in_table_cell: bool) -> None:
        for child in element:
            if child.tag == w("p"):
                supported.append((child, "table_cell" if in_table_cell else "body"))
                roots.extend(_inline_excluded_roots(child))
                continue

            explicit = _explicit_exclusion_kind(child)
            if explicit is not None:
                exclude(child, explicit)
                continue

            if child.tag in _CANONICAL_BLOCK_PASS_THROUGH:
                walk_block(
                    child,
                    in_table_cell=in_table_cell or child.tag == w("tc"),
                )
                continue

            if child.tag in _CANONICAL_PROPERTY_SUBTREES:
                # These are known non-text metadata surfaces. Do not include
                # them in paragraph reading, but leave any revision descendants
                # unowned so revision_inventory classifies them as in-scope
                # unsupported structural/property markup.
                if _property_subtree_has_illegal_payload(child):
                    exclude(child, "unknown_container")
                continue

            # Benign unlisted markers (for example bookmarks) are ignored only
            # while they carry no paragraph, rendered text atom or recognized
            # revision markup. The first unknown payload-bearing container is
            # the exclusion root, rather than each paragraph below it, so
            # coverage and revision partitioning remain non-overlapping.
            if _subtree_has_canonical_payload(child):
                exclude(child, "unknown_container")

    walk_block(body, in_table_cell=False)

    excluded_paragraph_count = 0
    excluded_paragraphs_by_kind: dict[str, int] = {}
    for root in roots:
        paragraph_count = sum(1 for _ in root.element.iter(w("p")))
        excluded_paragraph_count += paragraph_count
        if paragraph_count:
            excluded_paragraphs_by_kind[root.kind] = (
                excluded_paragraphs_by_kind.get(root.kind, 0) + paragraph_count
            )

    excluded_node_kinds: dict[etree._Element, str] = {}
    for root in roots:
        for node in root.element.iter():
            excluded_node_kinds[node] = root.kind

    paragraphs = tuple(
        CanonicalParagraph(paragraph, index, container_kind)
        for index, (paragraph, container_kind) in enumerate(supported)
    )
    return CanonicalBodyFlow(
        paragraphs=paragraphs,
        excluded_subtrees=tuple(roots),
        excluded_node_kinds=excluded_node_kinds,
        excluded_paragraph_count=excluded_paragraph_count,
        excluded_paragraphs_by_kind=excluded_paragraphs_by_kind,
    )


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
