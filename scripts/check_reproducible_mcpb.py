# SPDX-License-Identifier: Apache-2.0
"""Independently rebuild and byte-compare the macOS MCP bundle."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_contract import (  # noqa: E402
    BUILD_PYTHON_VERSION,
    BUILD_UV_VERSION,
    MCPB_FILENAME,
)


class McpbReproducibilityError(RuntimeError):
    """The rebuild environment or MCPB bytes differ from the approved artifact."""


def _tool_version(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise McpbReproducibilityError(f"cannot execute {command[0]}")
    return completed.stdout.strip()


def _assert_toolchain() -> None:
    if platform.python_version() != BUILD_PYTHON_VERSION:
        raise McpbReproducibilityError(
            f"expected Python {BUILD_PYTHON_VERSION}, got {platform.python_version()}"
        )
    uv_version = _tool_version(["uv", "--version"]).split()
    if len(uv_version) < 2 or uv_version[:2] != ["uv", BUILD_UV_VERSION]:
        raise McpbReproducibilityError("uv version differs from release contract")


def _artifact_bytes(directory: Path) -> bytes:
    path = directory / MCPB_FILENAME
    candidates = list(directory.glob("*.mcpb"))
    if candidates != [path] or not path.is_file():
        raise McpbReproducibilityError("expected exactly the canonical macOS MCPB")
    return path.read_bytes()


def _build(source_root: Path, output: Path) -> bytes:
    completed = subprocess.run(
        [
            sys.executable,
            str(source_root / "scripts" / "build_mcpb.py"),
            "--source-root",
            str(source_root),
            "--out-dir",
            str(output),
        ],
        cwd=source_root,
        check=False,
    )
    if completed.returncode != 0:
        raise McpbReproducibilityError("independent MCPB build failed")
    return _artifact_bytes(output)


def verify(source_root: Path, approved_dir: Path) -> str:
    _assert_toolchain()
    expected = _artifact_bytes(approved_dir)
    with tempfile.TemporaryDirectory(prefix="veqtor-mcpb-rebuild-") as temporary:
        observed = _build(source_root, Path(temporary))
    if expected != observed:
        raise McpbReproducibilityError("rebuilt MCPB bytes differ")
    return hashlib.sha256(expected).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--approved-dir", type=Path, required=True)
    options = parser.parse_args(argv)
    try:
        digest = verify(
            options.source_root.resolve(), options.approved_dir.resolve()
        )
    except (OSError, McpbReproducibilityError) as exc:
        print(f"reproducible MCPB check failed: {exc}", file=sys.stderr)
        return 1
    print(f"{digest}  {MCPB_FILENAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
