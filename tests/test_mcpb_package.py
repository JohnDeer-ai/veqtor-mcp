# SPDX-License-Identifier: Apache-2.0
"""Closed identity and deterministic-build tests for the Desktop extension."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

import pytest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_mcpb  # noqa: E402
import check_mcpb_artifact as checker  # noqa: E402
from release_contract import (  # noqa: E402
    MCPB_DEMO_FILENAMES,
    MCPB_FILENAME,
    MCPB_MANIFEST_VERSION,
    MCPB_MEMBERS,
    MCPB_REQUIRED_TOOLS,
    VERSION,
)


def test_manifest_declares_uv_author_tools_and_macos_only() -> None:
    manifest = json.loads(
        (ROOT / "packaging/mcpb/manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["manifest_version"] == MCPB_MANIFEST_VERSION
    assert manifest["version"] == VERSION
    assert manifest["server"] == {
        "type": "uv",
        "entry_point": "src/veqtor_mcp/server.py",
        "mcp_config": {
            "command": "uv",
            "args": [
                "run",
                "--frozen",
                "--no-dev",
                "--directory",
                "${__dirname}",
                "veqtor-mcp",
            ],
            "env": {
                "VEQTOR_TRACKED_CHANGE_AUTHOR": (
                    "${user_config.tracked_change_author}"
                ),
                "UV_NO_PROGRESS": "1",
            },
        },
    }
    assert manifest["user_config"]["tracked_change_author"]["required"] is True
    assert manifest["user_config"]["tracked_change_author"]["sensitive"] is False
    assert tuple(tool["name"] for tool in manifest["tools"]) == MCPB_REQUIRED_TOOLS
    assert manifest["tools_generated"] is False
    assert manifest["compatibility"] == {
        "platforms": ["darwin"],
        "runtimes": {"python": ">=3.12,<3.15"},
    }


def test_build_is_byte_deterministic_and_verifies(tmp_path: Path) -> None:
    first = build_mcpb.build(ROOT, tmp_path / "first")
    second = build_mcpb.build(ROOT, tmp_path / "second")

    assert first.name == MCPB_FILENAME
    assert first.read_bytes() == second.read_bytes()
    result = checker.verify(first, ROOT, "WORKTREE")
    assert result["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert result["member_count"] == len(MCPB_MEMBERS)
    assert tuple(result["tools"]) == MCPB_REQUIRED_TOOLS
    assert set(result["demo_sha256"]) == set(MCPB_DEMO_FILENAMES)


def test_bundle_has_only_reviewed_sources_and_generated_demo(tmp_path: Path) -> None:
    artifact = build_mcpb.build(ROOT, tmp_path / "dist")

    with zipfile.ZipFile(artifact) as archive:
        assert set(archive.namelist()) == set(MCPB_MEMBERS)
        assert ".mcpbignore" not in archive.namelist()
        assert not any(
            name == ".veqtor" or name.startswith(".veqtor/")
            for name in archive.namelist()
        )
        assert archive.read("demo/FIRST_PROMPT.txt").decode("utf-8").strip()
        assert all(
            archive.read(f"demo/{name}").startswith(b"PK")
            for name in MCPB_DEMO_FILENAMES
        )
        assert all(
            info.compress_type == zipfile.ZIP_STORED
            for info in archive.infolist()
        )
        for name in MCPB_DEMO_FILENAMES:
            with zipfile.ZipFile(io.BytesIO(archive.read(f"demo/{name}"))) as demo:
                assert all(
                    info.compress_type == zipfile.ZIP_STORED
                    for info in demo.infolist()
                )


def test_stage_is_complete_and_refuses_nonempty_destination(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    artifact = build_mcpb.build(ROOT, tmp_path / "dist", stage)

    assert artifact.is_file()
    staged = {
        path.relative_to(stage).as_posix()
        for path in stage.rglob("*")
        if path.is_file()
    }
    assert staged == set(MCPB_MEMBERS)
    with pytest.raises(ValueError, match="absent or empty"):
        build_mcpb.build(ROOT, tmp_path / "again", stage)


def test_changed_or_extra_member_fails_closed(tmp_path: Path) -> None:
    artifact = build_mcpb.build(ROOT, tmp_path / "dist")
    expected = checker.expected_member_payloads(ROOT, "WORKTREE")
    changed = dict(expected)
    changed["README.md"] += b"changed"
    build_mcpb._write_bundle(artifact, changed)

    with pytest.raises(checker.McpbArtifactError, match="member bytes differ"):
        checker.verify(artifact, ROOT, "WORKTREE")

    extra = dict(expected)
    extra["private.txt"] = b"not approved"
    build_mcpb._write_bundle(artifact, extra)
    with pytest.raises(checker.McpbArtifactError, match="member set"):
        checker.verify(artifact, ROOT, "WORKTREE")


def test_noncanonical_member_order_fails_closed(tmp_path: Path) -> None:
    artifact = tmp_path / MCPB_FILENAME
    payloads = checker.expected_member_payloads(ROOT, "WORKTREE")
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(payloads, reverse=True):
            info = zipfile.ZipInfo(name, date_time=build_mcpb.MCPB_ZIP_DATE)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = build_mcpb.MCPB_MEMBER_MODE << 16
            archive.writestr(info, payloads[name])

    with pytest.raises(checker.McpbArtifactError, match="member order"):
        checker.verify(artifact, ROOT, "WORKTREE")


def test_wrong_filename_and_trailing_bytes_fail_closed(tmp_path: Path) -> None:
    artifact = build_mcpb.build(ROOT, tmp_path / "dist")
    wrong_name = tmp_path / "dist" / "renamed.mcpb"
    wrong_name.write_bytes(artifact.read_bytes())
    with pytest.raises(checker.McpbArtifactError, match="filename"):
        checker.verify(wrong_name, ROOT, "WORKTREE")

    artifact.write_bytes(artifact.read_bytes() + b"trailing")
    with pytest.raises(checker.McpbArtifactError, match="trailing"):
        checker.verify(artifact, ROOT, "WORKTREE")
