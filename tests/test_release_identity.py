# SPDX-License-Identifier: Apache-2.0
"""Positive-identity tests for the closed v0.1 release inventory."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import shutil
import subprocess
import struct
import sys
import tarfile
import tomllib
import zipfile
import zlib
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
from release_contract import (  # noqa: E402
    RUNTIME_SOURCE_FILES,
    SDIST_GIT_FILES,
    SDIST_MEMBERS,
    WHEEL_MEMBERS,
)
from check_release_artifacts import (  # noqa: E402
    _expected_metadata_headers,
    _metadata_contract,
)


def _scanner(project: Path, artifacts: list[Path]):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "check_release_artifacts.py"),
            "--source-root",
            str(project),
            "--commit",
            "WORKTREE",
            *map(str, artifacts),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _project_copy(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for relative in SDIST_GIT_FILES:
        source = ROOT / relative
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return project


def _rewrite_wheel_member(wheel: Path, suffix: str, mutate) -> None:
    with zipfile.ZipFile(wheel) as archive:
        infos = archive.infolist()
        members = {info.filename: archive.read(info) for info in infos}
    target = next(name for name in members if name.endswith(suffix))
    members[target] = mutate(members[target])
    record = next(name for name in members if name.endswith(".dist-info/RECORD"))
    rows = []
    for info in infos:
        name = info.filename
        if name == record:
            rows.append((name, "", ""))
            continue
        payload = members[name]
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        rows.append((name, f"sha256={digest.decode('ascii')}", str(len(payload))))
    encoded = io.StringIO()
    csv.writer(encoded, lineterminator="\n").writerows(rows)
    members[record] = encoded.getvalue().encode("utf-8")

    replacement = wheel.with_suffix(".replacement")
    with zipfile.ZipFile(replacement, "w") as archive:
        for info in infos:
            archive.writestr(info, members[info.filename])
    replacement.replace(wheel)


def _canonical_gzip(payload: bytes) -> bytes:
    compressor = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
    compressed = compressor.compress(payload) + compressor.flush()
    header = b"\x1f\x8b\x08\x00" + struct.pack("<I", 1580601600) + b"\x02\xff"
    trailer = struct.pack("<II", zlib.crc32(payload), len(payload) & 0xFFFFFFFF)
    return header + compressed + trailer


def _rewrite_first_tar_header(sdist: Path, mutate) -> None:
    raw = bytearray(zlib.decompress(sdist.read_bytes(), wbits=31))
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        member = archive.next()
        assert member is not None
        offset = member.offset
    header = bytearray(raw[offset : offset + 512])
    mutate(header)
    header[148:156] = b"        "
    checksum = sum(header)
    header[148:156] = f"{checksum:06o}".encode() + b"\0 "
    raw[offset : offset + 512] = header
    sdist.write_bytes(_canonical_gzip(bytes(raw)))


def _rewrite_tar_owner(sdist: Path) -> None:
    def mutate(header: bytearray) -> None:
        header[108:116] = b"0000765\0"  # uid 501
        header[116:124] = b"0000024\0"  # gid 20
        header[265:297] = b"ilyashilov".ljust(32, b"\0")
        header[297:329] = b"staff".ljust(32, b"\0")

    _rewrite_first_tar_header(sdist, mutate)


def _rewrite_tar_contract_field(sdist: Path, field: str) -> None:
    def mutate(header: bytearray) -> None:
        if field == "linkname":
            hidden = base64.b64encode(b"/Users/private-user/Clients/Secret")
            header[157:257] = hidden.ljust(100, b"\0")
        elif field == "type":
            header[156:157] = tarfile.AREGTYPE
        elif field == "devmajor":
            header[329:337] = b"0000001\0"
        elif field == "devminor":
            header[337:345] = b"0000001\0"
        else:  # pragma: no cover - test-helper guard
            raise AssertionError(field)

    _rewrite_first_tar_header(sdist, mutate)


def _rewrite_local_zip_version(wheel: Path, version: int) -> None:
    payload = bytearray(wheel.read_bytes())
    local = payload.find(b"PK\x03\x04")
    assert local >= 0
    struct.pack_into("<H", payload, local + 4, version)
    wheel.write_bytes(payload)


def _rewrite_zip_metadata(wheel: Path, *, comment: bytes = b"", extra: bytes = b"") -> None:
    with zipfile.ZipFile(wheel) as archive:
        infos = archive.infolist()
        members = {info.filename: archive.read(info) for info in infos}
    replacement = wheel.with_suffix(".replacement")
    with zipfile.ZipFile(replacement, "w") as archive:
        archive.comment = comment
        for index, info in enumerate(infos):
            if index == 0:
                info.extra = extra
            archive.writestr(info, members[info.filename])
    replacement.replace(wheel)


def test_hatch_source_selection_equals_release_contract() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    sdist = config["tool"]["hatch"]["build"]["targets"]["sdist"]

    assert set(wheel["include"]) == {f"/{path}" for path in RUNTIME_SOURCE_FILES}
    assert wheel["sources"] == ["src"]
    assert {f"/{path}" for path in RUNTIME_SOURCE_FILES} <= set(sdist["include"])
    assert "/src" not in sdist["include"]


def test_extra_package_files_cannot_enter_release_artifacts(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    (project / "src/veqtor_mcp/client-matter.docx").write_bytes(b"private")
    (project / "src/veqtor_mcp/secret.py").write_text("PRIVATE = True\n")
    output = tmp_path / "dist"
    built = subprocess.run(
        ["uv", "build", "--out-dir", str(output), str(project)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    artifacts = sorted(
        path
        for path in output.iterdir()
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    )
    checked = _scanner(project, artifacts)

    assert checked.returncode == 0, checked.stderr
    wheel = next(path for path in artifacts if path.suffix == ".whl")
    sdist = next(path for path in artifacts if path.name.endswith(".tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        assert set(archive.namelist()) == set(WHEEL_MEMBERS)
        assert not any("client-matter" in name or "secret.py" in name for name in archive.namelist())
    with tarfile.open(sdist) as archive:
        assert set(archive.getnames()) == set(SDIST_MEMBERS)
        assert not any("client-matter" in name or "secret.py" in name for name in archive.getnames())


def test_identity_verifier_rejects_extra_wheel_member(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    output = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--out-dir", str(output), str(project)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(output.glob("*.whl"))
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("veqtor_mcp/client-matter.docx", b"private")

    artifacts = sorted(
        path
        for path in output.iterdir()
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    )
    checked = _scanner(project, artifacts)

    assert checked.returncode != 0
    assert (
        "wheel member inventory mismatch" in checked.stderr
        or "noncanonical ZIP member metadata" in checked.stderr
    )


def _built_artifacts(project: Path, output: Path) -> list[Path]:
    subprocess.run(
        ["uv", "build", "--out-dir", str(output), str(project)],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(
        path
        for path in output.iterdir()
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    )


def _insert_metadata_header(payload: bytes, header: bytes) -> bytes:
    head, body = payload.split(b"\n\n", 1)
    return head + b"\n" + header + b"\n\n" + body


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: (
            b"From L1VzZXJzL3ByaXZhdGUtdXNlci9DbGllbnRzL1NlY3JldA==\n"
            + payload
        ),
        lambda payload: b" " + payload,
        lambda payload: b"Malformed header\n" + payload,
        lambda payload: payload + b"\nX-Unparsed-Trailer: private\n",
    ],
    ids=["mbox-from", "leading-continuation", "malformed-header", "suffix"],
)
def test_raw_core_metadata_identity_rejects_parser_blind_spots(mutation) -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    readme = (ROOT / "README.md").read_bytes()
    project = pyproject["project"]
    # Start with bytes emitted by the pinned backend; this also ratchets the
    # serializer used by the release identity check.
    headers = _expected_metadata_headers(pyproject)
    canonical = b"".join(
        f"{key}: {value}\n".encode("utf-8") for key, value in headers
    ) + b"\n" + readme
    assert project["name"] == "veqtor-mcp"

    with pytest.raises(SystemExit, match="metadata raw bytes differ"):
        _metadata_contract(mutation(canonical), pyproject, readme, "test metadata")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: _insert_metadata_header(
            payload, b"Requires-Dist: unapproved-runtime>=1"
        ),
        lambda payload: _insert_metadata_header(
            payload, b"Project-URL: Private, https://example.invalid"
        ),
        lambda payload: _insert_metadata_header(payload, b"X-Unapproved: value"),
        lambda payload: payload.replace(b"Author: Ilya Shilov", b"Author: Someone Else"),
        lambda payload: payload.replace(
            b"Maintainer: Ilya Shilov", b"Maintainer: Someone Else"
        ),
        lambda payload: payload.replace(
            b"Keywords: docx,legal-tech,mcp,redlining,tracked-changes",
            b"Keywords: private-client",
        ),
    ],
    ids=["dependency", "url", "unknown-header", "author", "maintainer", "keywords"],
)
def test_full_metadata_contract_rejects_unapproved_headers_even_with_valid_record(
    tmp_path: Path, mutation
) -> None:
    project = _project_copy(tmp_path)
    artifacts = _built_artifacts(project, tmp_path / "dist")
    wheel = next(path for path in artifacts if path.suffix == ".whl")
    _rewrite_wheel_member(wheel, "/METADATA", mutation)

    checked = _scanner(project, artifacts)

    assert checked.returncode != 0
    assert "metadata raw bytes differ from source" in checked.stderr


def test_full_metadata_contract_rejects_description_drift(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    artifacts = _built_artifacts(project, tmp_path / "dist")
    wheel = next(path for path in artifacts if path.suffix == ".whl")
    _rewrite_wheel_member(wheel, "/METADATA", lambda payload: payload + b"drift\n")

    checked = _scanner(project, artifacts)

    assert checked.returncode != 0
    assert "metadata raw bytes differ from source" in checked.stderr


def test_wheel_generator_is_bound_to_pinned_backend(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    artifacts = _built_artifacts(project, tmp_path / "dist")
    wheel = next(path for path in artifacts if path.suffix == ".whl")
    _rewrite_wheel_member(
        wheel,
        "/WHEEL",
        lambda payload: payload.replace(b"hatchling 1.31.0", b"hatchling 9.9.9"),
    )

    checked = _scanner(project, artifacts)

    assert checked.returncode != 0
    assert "wheel metadata differs from release contract" in checked.stderr


@pytest.mark.parametrize("surface", ["comment", "extra"])
def test_full_identity_rejects_noncanonical_zip_metadata(
    tmp_path: Path, surface: str
) -> None:
    project = _project_copy(tmp_path)
    artifacts = _built_artifacts(project, tmp_path / "dist")
    wheel = next(path for path in artifacts if path.suffix == ".whl")
    hidden = zlib.compress(b"/Users/private-user/Clients/Secret")
    _rewrite_zip_metadata(
        wheel,
        comment=hidden if surface == "comment" else b"",
        extra=(struct.pack("<HH", 0xCAFE, len(hidden)) + hidden)
        if surface == "extra"
        else b"",
    )

    checked = _scanner(project, artifacts)

    assert checked.returncode != 0
    assert "noncanonical ZIP" in checked.stderr


def test_full_identity_rejects_noncanonical_tar_ownership(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    artifacts = _built_artifacts(project, tmp_path / "dist")
    sdist = next(path for path in artifacts if path.name.endswith(".tar.gz"))
    _rewrite_tar_owner(sdist)

    checked = _scanner(project, artifacts)

    assert checked.returncode != 0
    assert "noncanonical TAR member metadata" in checked.stderr


@pytest.mark.parametrize("field", ["linkname", "type", "devmajor", "devminor"])
def test_full_identity_rejects_every_noncanonical_tar_header_field(
    tmp_path: Path, field: str
) -> None:
    project = _project_copy(tmp_path)
    artifacts = _built_artifacts(project, tmp_path / "dist")
    sdist = next(path for path in artifacts if path.name.endswith(".tar.gz"))
    _rewrite_tar_contract_field(sdist, field)

    checked = _scanner(project, artifacts)

    assert checked.returncode != 0
    assert "noncanonical TAR member metadata" in checked.stderr


def test_full_identity_rejects_noncanonical_local_zip_version(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    artifacts = _built_artifacts(project, tmp_path / "dist")
    wheel = next(path for path in artifacts if path.suffix == ".whl")
    _rewrite_local_zip_version(wheel, 10)

    checked = _scanner(project, artifacts)

    assert checked.returncode != 0
    assert "ZIP local and central headers differ" in checked.stderr


def test_full_identity_rejects_noncanonical_gzip_header(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    artifacts = _built_artifacts(project, tmp_path / "dist")
    sdist = next(path for path in artifacts if path.name.endswith(".tar.gz"))
    payload = bytearray(sdist.read_bytes())
    payload[9] = 3
    sdist.write_bytes(payload)

    checked = _scanner(project, artifacts)

    assert checked.returncode != 0
    assert "noncanonical gzip header" in checked.stderr
