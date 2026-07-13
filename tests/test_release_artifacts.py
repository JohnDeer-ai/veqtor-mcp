# SPDX-License-Identifier: Apache-2.0
"""Adversarial checks for the public release archive privacy boundary."""

from __future__ import annotations

import gzip
import io
import os
import stat
import struct
import subprocess
import sys
import tarfile
import zipfile
import zlib
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_release_artifacts.py"


def _check(
    path: Path,
    *,
    markers_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("VEQTOR_RELEASE_FORBIDDEN_MARKERS_FILE", None)
    if markers_file is not None:
        env["VEQTOR_RELEASE_FORBIDDEN_MARKERS_FILE"] = str(markers_file)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _regular_tar_member(
    archive: tarfile.TarFile,
    payload: bytes,
    *,
    name: str = "package/README.md",
    pax_headers: dict[str, str] | None = None,
) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.pax_headers = pax_headers or {}
    archive.addfile(info, io.BytesIO(payload))


def _tar_with_payload(tmp_path: Path, name: str, payload: bytes) -> Path:
    artifact = tmp_path / name
    with tarfile.open(artifact, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        _regular_tar_member(archive, payload)
    return artifact


def _gzip_envelope(
    payload: bytes,
    *,
    filename: bytes | None = None,
    comment: bytes | None = None,
) -> bytes:
    flags = (0x08 if filename is not None else 0) | (0x10 if comment is not None else 0)
    header = bytearray(b"\x1f\x8b\x08" + bytes([flags]) + b"\0\0\0\0\0\xff")
    if filename is not None:
        header.extend(filename + b"\0")
    if comment is not None:
        header.extend(comment + b"\0")
    compressor = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
    compressed = compressor.compress(payload) + compressor.flush()
    trailer = struct.pack("<II", zlib.crc32(payload), len(payload) & 0xFFFFFFFF)
    return bytes(header) + compressed + trailer


def _zip_with_local_only_extra(path: Path, extra: bytes) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("package/module.py", b"clean")
    payload = bytearray(path.read_bytes())
    eocd = payload.rfind(b"PK\x05\x06")
    directory_offset = struct.unpack_from("<I", payload, eocd + 16)[0]
    name_length = struct.unpack_from("<H", payload, 26)[0]
    old_extra_length = struct.unpack_from("<H", payload, 28)[0]
    insert_at = 30 + name_length + old_extra_length
    payload[insert_at:insert_at] = extra
    struct.pack_into("<H", payload, 28, old_extra_length + len(extra))
    new_eocd = eocd + len(extra)
    struct.pack_into("<I", payload, new_eocd + 16, directory_offset + len(extra))
    path.write_bytes(payload)


def _prefix_zip(path: Path, prefix: bytes) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("package/module.py", b"clean")
    original = bytearray(path.read_bytes())
    old_eocd = original.rfind(b"PK\x05\x06")
    old_directory = struct.unpack_from("<I", original, old_eocd + 16)[0]
    payload = bytearray(prefix) + original
    new_directory = old_directory + len(prefix)
    new_eocd = old_eocd + len(prefix)
    struct.pack_into("<I", payload, new_directory + 42, len(prefix))
    struct.pack_into("<I", payload, new_eocd + 16, new_directory)
    path.write_bytes(payload)


def test_clean_archives_and_documentation_placeholders_pass(tmp_path: Path) -> None:
    source = b"examples: /Users/you/Deal and /Users/example"
    sdist = _tar_with_payload(tmp_path, "clean.tar.gz", source)
    wheel = tmp_path / "clean.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("package/README.md", source)

    assert _check(sdist).returncode == 0
    assert _check(wheel).returncode == 0


@pytest.mark.parametrize(
    "member_type",
    [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE],
    ids=["symlink", "hardlink", "fifo"],
)
def test_tar_links_and_special_members_are_rejected(
    tmp_path: Path,
    member_type: bytes,
) -> None:
    artifact = tmp_path / f"unsafe-{member_type.hex()}.tar.gz"
    with tarfile.open(artifact, "w:gz") as archive:
        info = tarfile.TarInfo("package/src/private-link")
        info.type = member_type
        info.linkname = "relative-target"
        archive.addfile(info)

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "unsafe archive member type" in checked.stderr


def test_zip_symlink_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "unsafe.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        info = zipfile.ZipInfo("package/private-link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, b"relative-target")

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "unsafe archive member type" in checked.stderr


@pytest.mark.parametrize(
    "payload",
    [
        b"/Users/private-user",
        b'{"path":"\\/Users\\/private-user\\/Clients"}',
        br"C:\users\private-user\Clients",
        b"%2FUsers%2Fprivate-user%2FClients",
        b"%252FUsers%252Fprivate-user%252FClients",
    ],
    ids=[
        "eof",
        "json-escaped",
        "windows-lowercase",
        "percent-encoded",
        "double-percent-encoded",
    ],
)
def test_normalized_private_paths_are_rejected(
    tmp_path: Path,
    payload: bytes,
) -> None:
    artifact = _tar_with_payload(tmp_path, "private-content.tar.gz", payload)

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "private home path" in checked.stderr
    assert "private-user" not in checked.stderr


def test_tar_pax_metadata_is_scanned(tmp_path: Path) -> None:
    artifact = tmp_path / "private-pax.tar.gz"
    with tarfile.open(artifact, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        _regular_tar_member(
            archive,
            b"clean",
            pax_headers={"comment": "/Users/private-user/Clients"},
        )

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "private home path" in checked.stderr


@pytest.mark.parametrize("field", ["uname", "gname"])
@pytest.mark.parametrize(
    "private_path", ["/Users/private-user", "/Users/private-user/Clients"]
)
def test_tar_ownership_metadata_is_scanned(
    tmp_path: Path,
    field: str,
    private_path: str,
) -> None:
    artifact = tmp_path / f"private-{field}.tar.gz"
    with tarfile.open(artifact, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo("package/README.md")
        info.size = len(b"clean")
        setattr(info, field, private_path)
        archive.addfile(info, io.BytesIO(b"clean"))

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "private home path" in checked.stderr
    assert "private-user" not in checked.stderr


@pytest.mark.parametrize("field", ["filename", "comment"])
def test_gzip_envelope_metadata_is_scanned(tmp_path: Path, field: str) -> None:
    clean = _tar_with_payload(tmp_path, "base.tar.gz", b"clean")
    raw = gzip.decompress(clean.read_bytes())
    artifact = tmp_path / f"private-gzip-{field}.tar.gz"
    options = {field: b"/Users/private-user"}
    artifact.write_bytes(_gzip_envelope(raw, **options))

    checked = _check(artifact)

    assert checked.returncode != 0
    assert f"gzip-{field}" in checked.stderr


def test_concatenated_gzip_stream_is_rejected(tmp_path: Path) -> None:
    artifact = _tar_with_payload(tmp_path, "concatenated.tar.gz", b"clean")
    artifact.write_bytes(artifact.read_bytes() + gzip.compress(b"trailing"))

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "trailing or concatenated" in checked.stderr


def test_raw_tar_surface_catches_unparsed_trailing_material(tmp_path: Path) -> None:
    clean = _tar_with_payload(tmp_path, "base.tar.gz", b"clean")
    raw = gzip.decompress(clean.read_bytes())
    artifact = tmp_path / "private-trailing.tar.gz"
    artifact.write_bytes(gzip.compress(raw + b"/Users/private-user/Clients"))

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "end padding" in checked.stderr


def test_nonzero_padding_between_tar_members_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "padding.tar.gz"
    with tarfile.open(artifact, "w:gz") as archive:
        _regular_tar_member(archive, b"first", name="package/src/first.py")
        _regular_tar_member(archive, b"second", name="package/src/second.py")
    raw = bytearray(gzip.decompress(artifact.read_bytes()))
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        first = archive.next()
        assert first is not None
        content_end = first.offset_data + first.size
        padding_end = first.offset_data + ((first.size + 511) // 512) * 512
    hidden = gzip.compress(b"/Users/private-user/Clients/Secret")
    assert len(hidden) <= padding_end - content_end
    raw[content_end : content_end + len(hidden)] = hidden
    artifact.write_bytes(gzip.compress(bytes(raw)))

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "nonzero TAR member padding" in checked.stderr


def test_zip_archive_comment_is_scanned(tmp_path: Path) -> None:
    artifact = tmp_path / "private-comment.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.comment = b"/Users/private-user/Clients"
        archive.writestr("package/module.py", b"clean")

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "archive-comment" in checked.stderr


def test_raw_zip_surface_catches_unparsed_trailing_material(tmp_path: Path) -> None:
    artifact = tmp_path / "private-trailing.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("package/module.py", b"clean")
    with artifact.open("ab") as handle:
        handle.write(gzip.compress(b"/Users/private-user/Clients"))

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "trailing or malformed" in checked.stderr


def test_oversized_logical_tar_member_is_rejected_before_read(tmp_path: Path) -> None:
    artifact = tmp_path / "oversized-member.tar.gz"
    payload = b"\0" * (4 * 1_048_576 + 1)
    with tarfile.open(artifact, "w:gz") as archive:
        _regular_tar_member(archive, payload)

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "oversized archive member" in checked.stderr


def test_sparse_tar_member_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "sparse.tar.gz"
    with tarfile.open(artifact, "w:gz", format=tarfile.GNU_FORMAT) as archive:
        info = tarfile.TarInfo("package/README.md")
        info.type = tarfile.GNUTYPE_SPARSE
        info.size = 0
        archive.addfile(info)

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "sparse archive member" in checked.stderr


def test_archive_member_count_is_bounded(tmp_path: Path) -> None:
    artifact = tmp_path / "too-many.tar.gz"
    with tarfile.open(artifact, "w:gz") as archive:
        for index in range(129):
            _regular_tar_member(
                archive,
                b"",
                name=f"package/src/member-{index}.py",
            )

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "too many archive members" in checked.stderr


def test_zip_member_comment_and_extra_metadata_are_scanned(tmp_path: Path) -> None:
    private_path = b"/Users/private-user/Clients"
    for kind in ("comment", "extra"):
        artifact = tmp_path / f"private-{kind}.whl"
        with zipfile.ZipFile(artifact, "w") as archive:
            info = zipfile.ZipInfo("package/module.py")
            if kind == "comment":
                info.comment = private_path
            else:
                info.extra = struct.pack("<HH", 0xCAFE, len(private_path)) + private_path
            archive.writestr(info, b"clean")

        checked = _check(artifact)

        assert checked.returncode != 0
        assert "private home path" in checked.stderr


def test_private_local_header_extra_is_scanned_when_central_extra_is_clean(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "private-local-extra.whl"
    private_path = b"/Users/private-user/Clients"
    extra = struct.pack("<HH", 0xCAFE, len(private_path)) + private_path
    _zip_with_local_only_extra(artifact, extra)
    with zipfile.ZipFile(artifact) as archive:
        assert archive.infolist()[0].extra == b""
        assert archive.read("package/module.py") == b"clean"

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "local-extra" in checked.stderr
    assert "private-user" not in checked.stderr


def test_valid_self_extracting_zip_prefix_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "prefixed.whl"
    _prefix_zip(artifact, b"unapproved-prefix")
    with zipfile.ZipFile(artifact) as archive:
        assert archive.read("package/module.py") == b"clean"

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "prefix or member gap" in checked.stderr


def test_zip_member_count_is_rejected_from_eocd_before_member_loading(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "too-many.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        for index in range(129):
            archive.writestr(f"package/member-{index}.py", b"")

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "too many archive members" in checked.stderr


def test_private_path_in_zip_member_name_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "private-name.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("package/Users/private-user/Clients/deal.txt", b"clean")

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "private home path" in checked.stderr


def test_external_private_marker_file_is_optional_and_not_echoed(
    tmp_path: Path,
) -> None:
    artifact = _tar_with_payload(
        tmp_path,
        "private-marker.tar.gz",
        b"synthetic-confidential-token",
    )
    markers = tmp_path / "markers.txt"
    markers.write_bytes(b"synthetic-confidential-token\n")

    checked = _check(artifact, markers_file=markers)

    assert checked.returncode != 0
    assert "external private marker" in checked.stderr
    assert "synthetic-confidential-token" not in checked.stderr


def test_external_marker_file_is_bounded_before_full_read(tmp_path: Path) -> None:
    artifact = _tar_with_payload(tmp_path, "clean-markers.tar.gz", b"clean")
    markers = tmp_path / "oversized-markers.txt"
    markers.write_bytes(b"x" * (1_048_576 + 1))

    checked = _check(artifact, markers_file=markers)

    assert checked.returncode != 0
    assert "oversized archive surface" in checked.stderr


@pytest.mark.parametrize("top_level", ["tests", "scripts", ".github", "uv.lock"])
def test_sdist_manifest_rejects_non_runtime_top_level_members(
    tmp_path: Path,
    top_level: str,
) -> None:
    artifact = tmp_path / f"unexpected-{top_level.replace('.', '-')}.tar.gz"
    with tarfile.open(artifact, "w:gz") as archive:
        _regular_tar_member(
            archive,
            b"clean",
            name=f"package/{top_level}/unexpected.txt"
            if "." not in top_level
            else f"package/{top_level}",
        )

    checked = _check(artifact)

    assert checked.returncode != 0
    assert "unexpected sdist member" in checked.stderr


def test_release_scanner_uses_no_embedded_private_denylist() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "FORBIDDEN_CONTENT" not in source
    assert "FORBIDDEN_MARKERS_ENV" in source
