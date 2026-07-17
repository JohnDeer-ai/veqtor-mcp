# SPDX-License-Identifier: Apache-2.0
"""Fail closed when release archives contain local/private material."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import os
import re
import stat
import struct
import sys
import tarfile
import tomllib
import subprocess
import zipfile
import zlib
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from urllib.parse import unquote_to_bytes

from release_contract import (
    CANONICAL_GZIP_OS,
    CANONICAL_GZIP_XFL,
    CANONICAL_TAR_MODE,
    CANONICAL_ZIP_DATE,
    CANONICAL_ZIP_FLAGS,
    CANONICAL_ZIP_METHOD,
    CANONICAL_ZIP_TIME,
    CANONICAL_ZIP_VERSION_MADE,
    CANONICAL_ZIP_VERSION_NEEDED,
    EXPECTED_ENTRY_POINTS,
    EXPECTED_WHEEL_METADATA,
    MAX_ARCHIVE_EXPANDED_BYTES,
    MAX_ARCHIVE_FILE_BYTES,
    MAX_ARCHIVE_MEMBER_BYTES,
    MAX_ARCHIVE_MEMBER_NAME_BYTES,
    MAX_ARCHIVE_MEMBERS,
    MAX_ARCHIVE_METADATA_BYTES,
    MAX_NORMALIZATION_PASSES,
    SDIST_MEMBERS,
    SDIST_SOURCE_MAP,
    SOURCE_DATE_EPOCH,
    VERSION,
    WHEEL_GENERATED_MEMBERS,
    WHEEL_LICENSE_MAP,
    WHEEL_MEMBERS,
    WHEEL_SOURCE_MAP,
)


FORBIDDEN_MARKERS_ENV = "VEQTOR_RELEASE_FORBIDDEN_MARKERS_FILE"
FORBIDDEN_MEMBER_PARTS = frozenset({".claude", ".veqtor", ".DS_Store"})
ALLOWED_HOME_NAMES = frozenset({b"you", b"example"})
HOME_PATH_RE = re.compile(
    rb"(?:[A-Z]:)?[\\/]+(?:users|home)[\\/]+"
    rb"([A-Za-z0-9._-]+)(?=[^A-Za-z0-9._-]|$)",
    re.IGNORECASE,
)
SDIST_ALLOWED_TOP_LEVEL = frozenset(
    {
        ".gitignore",
        "API.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "KNOWN_LIMITATIONS.md",
        "LICENSE",
        "NOTICE",
        "PKG-INFO",
        "README.md",
        "RELEASING.md",
        "ROADMAP.md",
        "SECURITY.md",
        "TRADEMARKS.md",
        "pyproject.toml",
        "src",
    }
)
MAX_EXTERNAL_MARKERS_BYTES = 1_048_576


def _normalized_variants(payload: bytes) -> Iterator[bytes]:
    """Yield a bounded fixed point of path/escaping normalizations."""
    current = payload
    for _ in range(MAX_NORMALIZATION_PASSES):
        yield current
        normalized = current.replace(b"\\/", b"/")
        while b"\\\\" in normalized:
            normalized = normalized.replace(b"\\\\", b"\\")
        try:
            normalized = unquote_to_bytes(normalized)
        except (UnicodeError, ValueError):
            return
        if normalized == current:
            return
        current = normalized
    raise SystemExit("privacy normalization exceeds the bounded depth")


def _external_markers() -> tuple[bytes, ...]:
    configured = os.environ.get(FORBIDDEN_MARKERS_ENV)
    if configured is None:
        return ()
    try:
        path = Path(configured).expanduser()
        with path.open("rb") as handle:
            payload = _bounded_read(
                handle, path, "external-markers", MAX_EXTERNAL_MARKERS_BYTES
            )
    except OSError as exc:
        raise SystemExit("cannot read external privacy markers") from exc
    markers = tuple(
        line.strip()
        for line in payload.splitlines()
        if line.strip() and not line.lstrip().startswith(b"#")
    )
    if not markers:
        raise SystemExit("external privacy markers contain no values")
    return markers


def _check_surface(
    artifact: Path,
    surface: str,
    payload: bytes,
    markers: tuple[bytes, ...],
) -> None:
    for variant in _normalized_variants(payload):
        for match in HOME_PATH_RE.finditer(variant):
            if match.group(1).lower() not in ALLOWED_HOME_NAMES:
                raise SystemExit(
                    f"private home path in {artifact.name}:{surface}"
                )
        if any(marker in variant for marker in markers):
            raise SystemExit(
                f"external private marker in {artifact.name}:{surface}"
            )


def _check_member_name(
    artifact: Path,
    name: str,
    *,
    sdist: bool,
) -> None:
    member = PurePosixPath(name)
    if member.is_absolute() or ".." in member.parts:
        raise SystemExit(f"unsafe archive member in {artifact.name}")
    if FORBIDDEN_MEMBER_PARTS.intersection(member.parts):
        raise SystemExit(f"build-only archive member in {artifact.name}")
    if sdist:
        if len(member.parts) < 2 or member.parts[1] not in SDIST_ALLOWED_TOP_LEVEL:
            raise SystemExit(f"unexpected sdist member in {artifact.name}")


def _bounded_read(handle, artifact: Path, surface: str, limit: int) -> bytes:
    payload = handle.read(limit + 1)
    if len(payload) > limit:
        raise SystemExit(f"oversized archive surface in {artifact.name}:{surface}")
    return payload


def _read_file_bounded(path: Path) -> bytes:
    if path.stat().st_size > MAX_ARCHIVE_FILE_BYTES:
        raise SystemExit(f"oversized release artifact: {path.name}")
    with path.open("rb") as handle:
        return _bounded_read(handle, path, "container", MAX_ARCHIVE_FILE_BYTES)


def _zip_eocd(payload: bytes, path: Path) -> tuple[int, int, int, int, bytes]:
    signature = b"PK\x05\x06"
    start = max(0, len(payload) - (65_535 + 22))
    position = payload.rfind(signature, start)
    while position >= start:
        if position + 22 <= len(payload):
            fields = struct.unpack_from("<4s4H2IH", payload, position)
            _, disk, directory_disk, disk_entries, entries, size, offset, comment_length = fields
            if position + 22 + comment_length == len(payload):
                if disk or directory_disk or disk_entries != entries:
                    raise SystemExit(f"multi-disk ZIP is unsupported: {path.name}")
                if entries == 0xFFFF or size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
                    raise SystemExit(f"ZIP64 is unsupported: {path.name}")
                if entries > MAX_ARCHIVE_MEMBERS:
                    raise SystemExit(f"too many archive members: {path.name}")
                if size > MAX_ARCHIVE_METADATA_BYTES:
                    raise SystemExit(f"oversized ZIP metadata: {path.name}")
                if offset + size != position:
                    raise SystemExit(f"ZIP central directory has gaps: {path.name}")
                return position, entries, offset, size, payload[position + 22 :]
        position = payload.rfind(signature, start, position)
    raise SystemExit(f"ZIP has trailing or malformed data: {path.name}")


def _decode_zip_name(raw: bytes, flags: int, path: Path) -> str:
    if len(raw) > MAX_ARCHIVE_MEMBER_NAME_BYTES:
        raise SystemExit(f"oversized archive member name: {path.name}")
    try:
        return raw.decode("utf-8" if flags & 0x800 else "cp437")
    except UnicodeError as exc:
        raise SystemExit(f"invalid ZIP member name: {path.name}") from exc


def _zip_layout(
    payload: bytes, path: Path
) -> tuple[list[dict[str, object]], bytes]:
    _, expected_count, directory_offset, directory_size, archive_comment = (
        _zip_eocd(payload, path)
    )
    position = directory_offset
    entries: list[dict[str, object]] = []
    seen_names: set[str] = set()
    for _ in range(expected_count):
        if position + 46 > len(payload) or payload[position : position + 4] != b"PK\x01\x02":
            raise SystemExit(f"malformed ZIP central directory: {path.name}")
        fields = struct.unpack_from("<4s6H3I5H2I", payload, position)
        version_made, version_needed = fields[1:3]
        flags, method, modified_time, modified_date = fields[3:7]
        crc, compressed_size, file_size = fields[7:10]
        name_length, extra_length, comment_length = fields[10:13]
        disk_start, internal_attr, external_attr, local_offset = fields[13:17]
        end = position + 46 + name_length + extra_length + comment_length
        if end > len(payload) or disk_start:
            raise SystemExit(f"malformed ZIP central member: {path.name}")
        raw_name = payload[position + 46 : position + 46 + name_length]
        name = _decode_zip_name(raw_name, flags, path)
        if name in seen_names:
            raise SystemExit(f"duplicate archive member: {path.name}")
        seen_names.add(name)
        extra_start = position + 46 + name_length
        comment_start = extra_start + extra_length
        entries.append(
            {
                "name": name,
                "raw_name": raw_name,
                "version_made": version_made,
                "version_needed": version_needed,
                "flags": flags,
                "method": method,
                "time": modified_time,
                "date": modified_date,
                "crc": crc,
                "compressed_size": compressed_size,
                "file_size": file_size,
                "external_attr": external_attr,
                "internal_attr": internal_attr,
                "local_offset": local_offset,
                "central_extra": payload[extra_start:comment_start],
                "comment": payload[comment_start:end],
            }
        )
        position = end
    if position != directory_offset + directory_size:
        raise SystemExit(f"ZIP member count does not match directory: {path.name}")

    cursor = 0
    for entry in sorted(entries, key=lambda item: int(item["local_offset"])):
        local_offset = int(entry["local_offset"])
        if local_offset != cursor or local_offset + 30 > directory_offset:
            raise SystemExit(f"ZIP has a prefix or member gap: {path.name}")
        fields = struct.unpack_from("<4s5H3I2H", payload, local_offset)
        if fields[0] != b"PK\x03\x04":
            raise SystemExit(f"malformed ZIP local header: {path.name}")
        local_version_needed = fields[1]
        flags, method, modified_time, modified_date = fields[2:6]
        crc, compressed_size, file_size = fields[6:9]
        name_length, extra_length = fields[9:11]
        if (
            name_length > MAX_ARCHIVE_MEMBER_NAME_BYTES
            or extra_length > MAX_ARCHIVE_METADATA_BYTES
        ):
            raise SystemExit(f"oversized ZIP local metadata: {path.name}")
        name_start = local_offset + 30
        extra_start = name_start + name_length
        data_start = extra_start + extra_length
        data_end = data_start + compressed_size
        if data_end > directory_offset:
            raise SystemExit(f"truncated ZIP member: {path.name}")
        raw_name = payload[name_start:extra_start]
        if (
            raw_name != entry["raw_name"]
            or local_version_needed != entry["version_needed"]
            or flags != entry["flags"]
            or method != entry["method"]
            or modified_time != entry["time"]
            or modified_date != entry["date"]
            or crc != entry["crc"]
            or compressed_size != entry["compressed_size"]
            or file_size != entry["file_size"]
        ):
            raise SystemExit(f"ZIP local and central headers differ: {path.name}")
        if flags & ~0x800 or method not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise SystemExit(f"unsupported ZIP flags or compression: {path.name}")
        entry["local_extra"] = payload[extra_start:data_start]
        entry["local_header"] = payload[local_offset:data_start]
        entry["compressed"] = payload[data_start:data_end]
        cursor = data_end
    if cursor != directory_offset:
        raise SystemExit(f"ZIP has data before its central directory: {path.name}")
    return entries, archive_comment


def _zip_surfaces(path: Path, *, canonical: bool) -> Iterator[tuple[str, bytes]]:
    payload = _read_file_bounded(path)
    entries, archive_comment = _zip_layout(payload, path)
    if canonical and path.suffix == ".mcpb":
        expected_names = sorted(str(entry["name"]) for entry in entries)
        central_names = [str(entry["name"]) for entry in entries]
        local_names = [
            str(entry["name"])
            for entry in sorted(entries, key=lambda item: int(item["local_offset"]))
        ]
        if central_names != expected_names or local_names != expected_names:
            raise SystemExit(f"noncanonical ZIP member order: {path.name}")
    if sum(int(entry["file_size"]) for entry in entries) > MAX_ARCHIVE_EXPANDED_BYTES:
        raise SystemExit(f"oversized expanded archive: {path.name}")
    yield "archive-comment", archive_comment
    if canonical and archive_comment:
        raise SystemExit(f"noncanonical ZIP archive comment: {path.name}")
    for index, entry in enumerate(entries):
        surface = f"member-{index}"
        name = str(entry["name"])
        _check_member_name(path, name, sdist=False)
        yield f"{surface}-name", name.encode(
            "utf-8", errors="backslashreplace"
        )
        yield f"{surface}-comment", bytes(entry["comment"])
        yield f"{surface}-central-extra", bytes(entry["central_extra"])
        yield f"{surface}-local-extra", bytes(entry["local_extra"])
        if canonical:
            expected_method = (
                zipfile.ZIP_STORED
                if path.suffix == ".mcpb"
                else CANONICAL_ZIP_METHOD
            )
            expected_mode = (
                0o100644 if name in WHEEL_SOURCE_MAP else CANONICAL_TAR_MODE
            )
            expected_local_header = struct.pack(
                "<4s5H3I2H",
                b"PK\x03\x04",
                CANONICAL_ZIP_VERSION_NEEDED,
                CANONICAL_ZIP_FLAGS,
                expected_method,
                CANONICAL_ZIP_TIME,
                CANONICAL_ZIP_DATE,
                int(entry["crc"]),
                int(entry["compressed_size"]),
                int(entry["file_size"]),
                len(bytes(entry["raw_name"])),
                0,
            ) + bytes(entry["raw_name"])
            if (
                entry["version_made"] != CANONICAL_ZIP_VERSION_MADE
                or entry["version_needed"] != CANONICAL_ZIP_VERSION_NEEDED
                or entry["flags"] != CANONICAL_ZIP_FLAGS
                or entry["method"] != expected_method
                or entry["time"] != CANONICAL_ZIP_TIME
                or entry["date"] != CANONICAL_ZIP_DATE
                or entry["internal_attr"] != 0
                or entry["external_attr"] != expected_mode << 16
                or entry["comment"]
                or entry["central_extra"]
                or entry["local_extra"]
                or entry["local_header"] != expected_local_header
            ):
                raise SystemExit(f"noncanonical ZIP member metadata: {path.name}")
        file_size = int(entry["file_size"])
        if file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise SystemExit(f"oversized archive member in {path.name}")
        mode = (int(entry["external_attr"]) >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if file_type == stat.S_IFLNK:
            raise SystemExit(f"unsafe archive member type in {path.name}")
        if name.endswith("/"):
            continue
        if file_type not in {0, stat.S_IFREG}:
            raise SystemExit(f"unsafe archive member type in {path.name}")
        compressed = bytes(entry["compressed"])
        if int(entry["method"]) == zipfile.ZIP_STORED:
            content = compressed
        else:
            decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
            content = decompressor.decompress(
                compressed, MAX_ARCHIVE_MEMBER_BYTES + 1
            )
            if (
                not decompressor.eof
                or decompressor.unused_data
                or decompressor.unconsumed_tail
            ):
                raise SystemExit(f"invalid ZIP deflate stream: {path.name}")
        if (
            len(content) != file_size
            or zlib.crc32(content) & 0xFFFFFFFF != int(entry["crc"])
        ):
            raise SystemExit(f"archive member size mismatch in {path.name}")
        yield f"{surface}-content", content


def _gzip_c_string(payload: bytes, position: int, label: str) -> tuple[bytes, int]:
    end = payload.find(b"\0", position)
    if end == -1:
        raise SystemExit(f"unterminated gzip {label}")
    return payload[position:end], end + 1


def _decode_single_gzip(
    path: Path, *, canonical: bool
) -> tuple[bytes, list[tuple[str, bytes]]]:
    payload = _read_file_bounded(path)
    if len(payload) < 18 or payload[:3] != b"\x1f\x8b\x08":
        raise SystemExit(f"invalid gzip header: {path.name}")
    flags = payload[3]
    if flags & 0xE0:
        raise SystemExit(f"reserved gzip flags in {path.name}")
    if canonical:
        expected_header = (
            b"\x1f\x8b\x08\x00"
            + struct.pack("<I", int(SOURCE_DATE_EPOCH))
            + bytes((CANONICAL_GZIP_XFL, CANONICAL_GZIP_OS))
        )
        if payload[:10] != expected_header:
            raise SystemExit(f"noncanonical gzip header: {path.name}")
    position = 10
    surfaces: list[tuple[str, bytes]] = []
    if flags & 0x04:
        if position + 2 > len(payload):
            raise SystemExit("truncated gzip extra length")
        extra_length = struct.unpack_from("<H", payload, position)[0]
        position += 2
        end = position + extra_length
        if end > len(payload):
            raise SystemExit("truncated gzip extra field")
        surfaces.append(("gzip-extra", payload[position:end]))
        position = end
    if flags & 0x08:
        value, position = _gzip_c_string(payload, position, "filename")
        surfaces.append(("gzip-filename", value))
    if flags & 0x10:
        value, position = _gzip_c_string(payload, position, "comment")
        surfaces.append(("gzip-comment", value))
    if flags & 0x02:
        if position + 2 > len(payload):
            raise SystemExit("truncated gzip header checksum")
        expected_header_crc = struct.unpack_from("<H", payload, position)[0]
        if zlib.crc32(payload[:position]) & 0xFFFF != expected_header_crc:
            raise SystemExit("invalid gzip header checksum")
        position += 2

    decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
    expanded = decompressor.decompress(
        payload[position:], MAX_ARCHIVE_EXPANDED_BYTES + 1
    )
    if len(expanded) > MAX_ARCHIVE_EXPANDED_BYTES or decompressor.unconsumed_tail:
        raise SystemExit(f"oversized expanded archive: {path.name}")
    if not decompressor.eof:
        raise SystemExit(f"truncated gzip stream: {path.name}")
    trailer = decompressor.unused_data
    if len(trailer) != 8:
        raise SystemExit(f"gzip has trailing or concatenated data: {path.name}")
    expected_crc, expected_size = struct.unpack("<II", trailer)
    if zlib.crc32(expanded) & 0xFFFFFFFF != expected_crc:
        raise SystemExit(f"gzip CRC mismatch: {path.name}")
    if len(expanded) & 0xFFFFFFFF != expected_size:
        raise SystemExit(f"gzip size mismatch: {path.name}")
    return expanded, surfaces


def _mapping_surfaces(prefix: str, value: Mapping[str, object]):
    for index, (key, item) in enumerate(sorted(value.items())):
        yield f"{prefix}-{index}-key", str(key).encode(
            "utf-8", errors="backslashreplace"
        )
        yield f"{prefix}-{index}-value", str(item).encode(
            "utf-8", errors="backslashreplace"
        )


def _tar_surfaces(path: Path, *, canonical: bool) -> Iterator[tuple[str, bytes]]:
    raw_payload, gzip_surfaces = _decode_single_gzip(path, canonical=canonical)
    yield from gzip_surfaces
    with tarfile.open(fileobj=io.BytesIO(raw_payload), mode="r:") as archive:
        yield from _mapping_surfaces("archive-pax", archive.pax_headers)
        if archive.pax_headers:
            raise SystemExit(f"global PAX metadata is unsupported: {path.name}")
        seen_names: set[str] = set()
        member_count = 0
        total_size = 0
        data_end = 0
        expected_offset = 0
        for index, info in enumerate(archive):
            member_count += 1
            if member_count > MAX_ARCHIVE_MEMBERS:
                raise SystemExit(f"too many archive members: {path.name}")
            if info.name in seen_names:
                raise SystemExit(f"duplicate archive member: {path.name}")
            seen_names.add(info.name)
            surface = f"member-{index}"
            _check_member_name(path, info.name, sdist=True)
            yield f"{surface}-name", info.name.encode(
                "utf-8", errors="backslashreplace"
            )
            yield f"{surface}-linkname", info.linkname.encode(
                "utf-8", errors="backslashreplace"
            )
            yield f"{surface}-uname", info.uname.encode(
                "utf-8", errors="backslashreplace"
            )
            yield f"{surface}-gname", info.gname.encode(
                "utf-8", errors="backslashreplace"
            )
            yield from _mapping_surfaces(f"{surface}-pax", info.pax_headers)
            raw_header = raw_payload[info.offset : info.offset + 512]
            yield f"{surface}-raw-header", raw_header
            if info.pax_headers:
                raise SystemExit(f"PAX member metadata is unsupported: {path.name}")
            if info.offset != expected_offset or info.offset_data != info.offset + 512:
                raise SystemExit(f"TAR member offsets are noncanonical: {path.name}")
            if info.type == tarfile.GNUTYPE_SPARSE or getattr(info, "sparse", None) or any(
                str(key).startswith("GNU.sparse") for key in info.pax_headers
            ):
                raise SystemExit(f"sparse archive member in {path.name}")
            if canonical:
                try:
                    expected_info = tarfile.TarInfo(info.name)
                    expected_info.size = info.size
                    expected_info.mode = CANONICAL_TAR_MODE
                    expected_info.uid = 0
                    expected_info.gid = 0
                    expected_info.mtime = int(SOURCE_DATE_EPOCH)
                    expected_info.type = tarfile.REGTYPE
                    expected_info.linkname = ""
                    expected_info.uname = ""
                    expected_info.gname = ""
                    expected_info.devmajor = 0
                    expected_info.devminor = 0
                    canonical_header = expected_info.tobuf(
                        format=tarfile.USTAR_FORMAT,
                        encoding="utf-8",
                        errors="strict",
                    )
                except (UnicodeError, ValueError) as exc:
                    raise SystemExit(f"cannot canonicalize TAR header: {path.name}") from exc
                if (
                    raw_header != canonical_header
                    or info.uid != 0
                    or info.gid != 0
                    or info.uname != ""
                    or info.gname != ""
                    or info.mode != CANONICAL_TAR_MODE
                    or info.mtime != int(SOURCE_DATE_EPOCH)
                    or info.type != tarfile.REGTYPE
                    or info.linkname != ""
                    or info.devmajor != 0
                    or info.devminor != 0
                ):
                    raise SystemExit(f"noncanonical TAR member metadata: {path.name}")
            if info.size > MAX_ARCHIVE_MEMBER_BYTES:
                raise SystemExit(f"oversized archive member in {path.name}")
            total_size += info.size
            if total_size > MAX_ARCHIVE_EXPANDED_BYTES:
                raise SystemExit(f"oversized expanded archive: {path.name}")
            padded_size = ((info.size + 511) // 512) * 512
            data_end = max(data_end, info.offset_data + padded_size)
            content_end = info.offset_data + info.size
            padding_end = info.offset_data + padded_size
            if any(raw_payload[content_end:padding_end]):
                raise SystemExit(f"nonzero TAR member padding: {path.name}")
            expected_offset = padding_end
            if info.isdir():
                continue
            if not info.isfile():
                raise SystemExit(f"unsafe archive member type in {path.name}")
            handle = archive.extractfile(info)
            if handle is None:
                raise SystemExit(f"unreadable archive member in {path.name}")
            content = _bounded_read(
                handle, path, f"{surface}-content", MAX_ARCHIVE_MEMBER_BYTES
            )
            if len(content) != info.size:
                raise SystemExit(f"archive member size mismatch in {path.name}")
            yield f"{surface}-content", content
    ending = raw_payload[data_end:]
    if len(ending) < 1024 or any(ending):
        raise SystemExit(f"TAR has nonzero or missing end padding: {path.name}")


def _surfaces(path: Path, *, canonical: bool) -> Iterator[tuple[str, bytes]]:
    if path.suffix in {".whl", ".mcpb"}:
        yield from _zip_surfaces(path, canonical=canonical)
        return
    if path.name.endswith(".tar.gz"):
        yield from _tar_surfaces(path, canonical=canonical)
        return
    raise SystemExit(f"unsupported release artifact: {path.name}")


def check(path: Path, *, canonical: bool = False) -> None:
    markers = _external_markers()
    for surface, payload in _surfaces(path, canonical=canonical):
        _check_surface(path, surface, payload, markers)


def _git_blob(source_root: Path, commit: str, relative_path: str) -> bytes:
    if commit == "WORKTREE":
        try:
            return (source_root / relative_path).read_bytes()
        except OSError as exc:
            raise SystemExit("cannot read approved worktree file") from exc
    result = subprocess.run(
        ["git", "-C", str(source_root), "show", f"{commit}:{relative_path}"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SystemExit("cannot read approved git blob")
    return result.stdout


def _canonical_specifiers(value: str) -> str:
    return ",".join(sorted(part.strip() for part in value.split(",") if part.strip()))


def _canonical_requirement(value: str) -> str:
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]*)(.*)", value.strip())
    if match is None:
        raise SystemExit("release dependency has unsupported syntax")
    name, specifiers = match.groups()
    if not specifiers:
        return name
    return f"{name}{_canonical_specifiers(specifiers)}"


def _expected_metadata_headers(pyproject: dict) -> list[tuple[str, str]]:
    project = pyproject["project"]
    headers: list[tuple[str, str]] = [
        ("Metadata-Version", "2.4"),
        ("Name", project["name"]),
        ("Version", project["version"]),
        ("Summary", project["description"]),
    ]
    headers.extend(
        ("Project-URL", f"{label}, {url}")
        for label, url in project.get("urls", {}).items()
    )
    authors = [item.get("name") for item in project.get("authors", [])]
    if any(not isinstance(author, str) or not author for author in authors):
        raise SystemExit("release author metadata is unsupported")
    if authors:
        headers.append(("Author", ", ".join(authors)))
    maintainers = [item.get("name") for item in project.get("maintainers", [])]
    if any(
        not isinstance(maintainer, str) or not maintainer
        for maintainer in maintainers
    ):
        raise SystemExit("release maintainer metadata is unsupported")
    if maintainers:
        headers.append(("Maintainer", ", ".join(maintainers)))
    license_expression = project.get("license")
    if not isinstance(license_expression, str):
        raise SystemExit("release license metadata is unsupported")
    headers.append(("License-Expression", license_expression))
    headers.extend(("License-File", name) for name in ("LICENSE", "NOTICE"))
    keywords = project.get("keywords", [])
    if not isinstance(keywords, list) or any(
        not isinstance(keyword, str) or not keyword for keyword in keywords
    ):
        raise SystemExit("release keyword metadata is unsupported")
    if keywords:
        headers.append(("Keywords", ",".join(sorted(keywords))))
    headers.extend(
        ("Classifier", classifier)
        for classifier in project.get("classifiers", [])
    )
    headers.append(
        (
            "Requires-Python",
            _canonical_specifiers(project.get("requires-python", "")),
        )
    )
    headers.extend(
        ("Requires-Dist", _canonical_requirement(requirement))
        for requirement in project.get("dependencies", [])
    )
    for extra, requirements in project.get("optional-dependencies", {}).items():
        headers.append(("Provides-Extra", extra))
        headers.extend(
            (
                "Requires-Dist",
                f"{_canonical_requirement(requirement)}; extra == '{extra}'",
            )
            for requirement in requirements
        )
    headers.append(("Description-Content-Type", "text/markdown"))
    return headers


def _metadata_contract(
    payload: bytes,
    pyproject: dict,
    approved_readme: bytes,
    surface: str,
) -> None:
    expected = b"".join(
        f"{key}: {value}\n".encode("utf-8")
        for key, value in _expected_metadata_headers(pyproject)
    ) + b"\n" + approved_readme
    if payload != expected:
        # Raw equality is intentional.  Parsing email-style metadata first
        # can hide mbox envelopes, malformed continuations and parser defects.
        raise SystemExit(
            f"package metadata raw bytes differ from source in {surface}"
        )


def _verify_wheel_record(members: dict[str, bytes]) -> None:
    record_name = next(name for name in members if name.endswith(".dist-info/RECORD"))
    rows = list(csv.reader(io.StringIO(members[record_name].decode("utf-8"))))
    if len(rows) != len(members) or any(len(row) != 3 for row in rows):
        raise SystemExit("wheel RECORD does not enumerate every member once")
    observed: dict[str, tuple[str, str]] = {}
    for name, digest, size in rows:
        if name in observed:
            raise SystemExit("wheel RECORD contains duplicate member names")
        observed[name] = (digest, size)
    if set(observed) != set(members):
        raise SystemExit("wheel RECORD member set mismatch")
    for name, payload in members.items():
        digest, size = observed[name]
        if name == record_name:
            if digest or size:
                raise SystemExit("wheel RECORD self-entry must omit hash and size")
            continue
        encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        if digest != f"sha256={encoded.decode('ascii')}" or size != str(len(payload)):
            raise SystemExit("wheel RECORD hash or size mismatch")


def _verify_release_identity(
    artifacts: list[Path],
    source_root: Path,
    commit: str,
) -> None:
    wheels = [path for path in artifacts if path.suffix == ".whl"]
    sdists = [path for path in artifacts if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1 or len(artifacts) != 2:
        raise SystemExit("identity verification needs exactly one wheel and one sdist")

    pyproject_bytes = _git_blob(source_root, commit, "pyproject.toml")
    pyproject = tomllib.loads(pyproject_bytes.decode("utf-8"))
    if pyproject["project"]["version"] != VERSION:
        raise SystemExit("release contract version differs from approved source")
    readme_path = pyproject["project"].get("readme")
    if not isinstance(readme_path, str):
        raise SystemExit("release README configuration is unsupported")
    approved_readme = _git_blob(source_root, commit, readme_path)

    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(WHEEL_MEMBERS):
            raise SystemExit("wheel member inventory mismatch")
        wheel_members = {name: archive.read(name) for name in names}
    for member, source in {**WHEEL_SOURCE_MAP, **WHEEL_LICENSE_MAP}.items():
        if wheel_members[member] != _git_blob(source_root, commit, source):
            raise SystemExit("wheel source member differs from approved git blob")
    metadata_name = next(
        name for name in WHEEL_GENERATED_MEMBERS if name.endswith("/METADATA")
    )
    _metadata_contract(
        wheel_members[metadata_name],
        pyproject,
        approved_readme,
        "wheel METADATA",
    )
    entry_name = next(
        name for name in WHEEL_GENERATED_MEMBERS if name.endswith("/entry_points.txt")
    )
    if wheel_members[entry_name].decode("utf-8") != EXPECTED_ENTRY_POINTS:
        raise SystemExit("wheel entry points differ from release contract")
    wheel_name = next(
        name for name in WHEEL_GENERATED_MEMBERS if name.endswith("/WHEEL")
    )
    if wheel_members[wheel_name].decode("utf-8") != EXPECTED_WHEEL_METADATA:
        raise SystemExit("wheel metadata differs from release contract")
    _verify_wheel_record(wheel_members)

    with tarfile.open(sdists[0], "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)) or set(names) != set(SDIST_MEMBERS):
            raise SystemExit("sdist member inventory mismatch")
        sdist_members: dict[str, bytes] = {}
        for member in members:
            handle = archive.extractfile(member)
            if handle is None:
                raise SystemExit("sdist inventory contains a non-file member")
            sdist_members[member.name] = handle.read()
    for member, source in SDIST_SOURCE_MAP.items():
        if sdist_members[member] != _git_blob(source_root, commit, source):
            raise SystemExit("sdist source member differs from approved git blob")
    pkg_info_name = next(name for name in SDIST_MEMBERS if name.endswith("/PKG-INFO"))
    _metadata_contract(
        sdist_members[pkg_info_name],
        pyproject,
        approved_readme,
        "sdist PKG-INFO",
    )
    if wheel_members[metadata_name] != sdist_members[pkg_info_name]:
        raise SystemExit("wheel and sdist core metadata bytes differ")


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("artifacts", nargs="+")
    options = parser.parse_args(argv)
    artifacts = [Path(value) for value in options.artifacts]
    if (options.source_root is None) != (options.commit is None):
        raise SystemExit("--source-root and --commit must be used together")
    for path in artifacts:
        check(path, canonical=options.source_root is not None)
    if options.source_root is not None:
        _verify_release_identity(artifacts, options.source_root, options.commit)
    print(f"release artifact privacy check passed: {len(artifacts)} artifact(s)")


if __name__ == "__main__":
    main(sys.argv[1:])
