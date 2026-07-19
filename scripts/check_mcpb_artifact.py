# SPDX-License-Identifier: Apache-2.0
"""Verify the closed identity and runtime contract of a Veqtor MCP bundle."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from check_release_artifacts import check as check_archive  # noqa: E402
from release_contract import (  # noqa: E402
    MCPB_DEMO_FILENAMES,
    MCPB_FILENAME,
    MCPB_MANIFEST_VERSION,
    MCPB_MEMBERS,
    MCPB_PLATFORM,
    MCPB_REQUIRED_TOOLS,
    MCPB_SOURCE_MAP,
    PROJECT_NAME,
    VERSION,
)
from veqtor_docx.synthetic import generate_demo_rounds  # noqa: E402


class McpbArtifactError(ValueError):
    """The bundle differs from the exact reviewed release contract."""


MCPB_ZIP_DATE = (2020, 2, 2, 0, 0, 0)
MCPB_MEMBER_MODE = 0o644


def _canonical_stored_docx(payload: bytes) -> bytes:
    """Remove zlib-version variance from a generated DOCX package."""

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as source:
            names = source.namelist()
            if len(names) != len(set(names)):
                raise McpbArtifactError("synthetic DOCX has duplicate members")
            members = {name: source.read(name) for name in names}
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise McpbArtifactError("synthetic DOCX is not readable") from exc

    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_STORED,
        strict_timestamps=True,
    ) as target:
        target.comment = b""
        for name, member_payload in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=MCPB_ZIP_DATE)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = MCPB_MEMBER_MODE << 16
            info.internal_attr = 0
            info.extra = b""
            info.comment = b""
            target.writestr(info, member_payload, compress_type=zipfile.ZIP_STORED)
    return output.getvalue()


def _approved_bytes(source_root: Path, commit: str, relative_path: str) -> bytes:
    if commit == "WORKTREE":
        path = source_root / relative_path
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise McpbArtifactError("approved source must be a regular file")
            return path.read_bytes()
        except OSError as exc:
            raise McpbArtifactError("cannot read approved worktree file") from exc
    result = subprocess.run(
        ["git", "-C", str(source_root), "show", f"{commit}:{relative_path}"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise McpbArtifactError("cannot read approved git blob")
    return result.stdout


def _generated_demo_payloads() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="veqtor-mcpb-demo-") as temporary:
        demo = Path(temporary) / "demo"
        generated = generate_demo_rounds(demo)
        observed = tuple(path.name for path in generated)
        if observed != MCPB_DEMO_FILENAMES:
            raise McpbArtifactError("synthetic demo inventory differs from contract")
        return {
            f"demo/{path.name}": _canonical_stored_docx(path.read_bytes())
            for path in generated
        }


def expected_member_payloads(
    source_root: Path,
    commit: str,
) -> dict[str, bytes]:
    payloads = {
        member: _approved_bytes(source_root, commit, relative_path)
        for member, relative_path in MCPB_SOURCE_MAP.items()
    }
    payloads.update(_generated_demo_payloads())
    if set(payloads) != set(MCPB_MEMBERS):
        raise McpbArtifactError("internal MCPB inventory is inconsistent")
    return payloads


def _closed_json(payload: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise McpbArtifactError("manifest contains duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise McpbArtifactError("manifest is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise McpbArtifactError("manifest must be an object")
    return value


def _validate_manifest(manifest_bytes: bytes, prompt_bytes: bytes) -> None:
    manifest = _closed_json(manifest_bytes)
    if manifest.get("manifest_version") != MCPB_MANIFEST_VERSION:
        raise McpbArtifactError("manifest version differs from contract")
    if manifest.get("name") != PROJECT_NAME or manifest.get("version") != VERSION:
        raise McpbArtifactError("manifest product identity differs from contract")
    if manifest.get("icon") != "icon.png":
        raise McpbArtifactError("manifest icon differs from contract")
    server = manifest.get("server")
    expected_server = {
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
    if server != expected_server:
        raise McpbArtifactError("manifest UV launch contract differs")
    expected_author_config = {
        "type": "string",
        "title": "Tracked-change author",
        "description": (
            "The name written into new Word tracked changes. Veqtor refuses a "
            "blank name or a value longer than 255 characters."
        ),
        "required": True,
        "sensitive": False,
    }
    if manifest.get("user_config") != {
        "tracked_change_author": expected_author_config
    }:
        raise McpbArtifactError("tracked-change author configuration differs")
    tools = manifest.get("tools")
    if (
        not isinstance(tools, list)
        or tuple(tool.get("name") for tool in tools if isinstance(tool, dict))
        != MCPB_REQUIRED_TOOLS
        or manifest.get("tools_generated") is not False
    ):
        raise McpbArtifactError("manifest tool inventory differs from contract")
    prompts = manifest.get("prompts")
    prompt = prompt_bytes.decode("utf-8").strip()
    if (
        not isinstance(prompts, list)
        or len(prompts) != 1
        or not isinstance(prompts[0], dict)
        or prompts[0].get("name") != "try_veqtor_demo"
        or prompts[0].get("text") != prompt
        or manifest.get("prompts_generated") is not False
    ):
        raise McpbArtifactError("manifest activation prompt differs from contract")
    if manifest.get("compatibility") != {
        "platforms": [MCPB_PLATFORM],
        "runtimes": {"python": ">=3.12,<3.15"},
    }:
        raise McpbArtifactError("manifest compatibility differs from contract")


def _validate_project(pyproject_bytes: bytes, lock_bytes: bytes) -> None:
    try:
        pyproject = tomllib.loads(pyproject_bytes.decode("utf-8"))
        lock = tomllib.loads(lock_bytes.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise McpbArtifactError("bundled project metadata is invalid") from exc
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise McpbArtifactError("bundled pyproject has no project table")
    if (
        project.get("name") != PROJECT_NAME
        or project.get("version") != VERSION
        or project.get("requires-python") != ">=3.12,<3.15"
    ):
        raise McpbArtifactError("bundled project identity differs from manifest")
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise McpbArtifactError("bundled UV lock has no package inventory")
    roots = [
        package
        for package in packages
        if isinstance(package, dict) and package.get("name") == PROJECT_NAME
    ]
    if len(roots) != 1 or roots[0].get("version") != VERSION:
        raise McpbArtifactError("bundled UV lock root differs from manifest")


def _archive_payloads(path: Path) -> dict[str, bytes]:
    try:
        check_archive(path, canonical=True)
    except SystemExit as exc:
        raise McpbArtifactError(str(exc)) from exc
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != set(MCPB_MEMBERS):
                raise McpbArtifactError("MCPB member set differs from contract")
            return {name: archive.read(name) for name in names}
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise McpbArtifactError("cannot read MCPB archive") from exc


def verify(path: Path, source_root: Path, commit: str) -> dict[str, Any]:
    if path.name != MCPB_FILENAME:
        raise McpbArtifactError("MCPB filename differs from contract")
    payloads = _archive_payloads(path)
    expected = expected_member_payloads(source_root, commit)
    if payloads != expected:
        changed = sorted(
            name
            for name in set(payloads) | set(expected)
            if payloads.get(name) != expected.get(name)
        )
        detail = changed[0] if changed else "unknown"
        raise McpbArtifactError(f"MCPB member bytes differ: {detail}")
    _validate_manifest(
        payloads["manifest.json"], payloads["demo/FIRST_PROMPT.txt"]
    )
    _validate_project(payloads["pyproject.toml"], payloads["uv.lock"])
    if not payloads["icon.png"].startswith(b"\x89PNG\r\n\x1a\n"):
        raise McpbArtifactError("MCPB icon is not a PNG")
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "manifest_version": MCPB_MANIFEST_VERSION,
        "version": VERSION,
        "platform": MCPB_PLATFORM,
        "member_count": len(payloads),
        "tools": list(MCPB_REQUIRED_TOOLS),
        "demo_sha256": {
            filename: hashlib.sha256(payloads[f"demo/{filename}"]).hexdigest()
            for filename in MCPB_DEMO_FILENAMES
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--commit", default="WORKTREE")
    options = parser.parse_args(argv)
    try:
        result = verify(
            options.artifact.resolve(),
            options.source_root.resolve(),
            options.commit,
        )
    except (OSError, McpbArtifactError) as exc:
        print(f"MCPB verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
