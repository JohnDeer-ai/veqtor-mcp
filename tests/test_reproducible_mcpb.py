# SPDX-License-Identifier: Apache-2.0
"""Unit boundaries for the independent MCPB byte-equality gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_reproducible_mcpb.py"
SPEC = importlib.util.spec_from_file_location("check_reproducible_mcpb", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
rebuild = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rebuild)


def test_artifact_directory_requires_exact_mcpb_name(tmp_path: Path) -> None:
    with pytest.raises(rebuild.McpbReproducibilityError, match="canonical"):
        rebuild._artifact_bytes(tmp_path)

    expected = tmp_path / rebuild.MCPB_FILENAME
    expected.write_bytes(b"bundle")
    assert rebuild._artifact_bytes(tmp_path) == b"bundle"

    (tmp_path / "extra.mcpb").write_bytes(b"extra")
    with pytest.raises(rebuild.McpbReproducibilityError, match="canonical"):
        rebuild._artifact_bytes(tmp_path)


def test_rebuild_requires_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    (approved / rebuild.MCPB_FILENAME).write_bytes(b"approved")
    monkeypatch.setattr(rebuild, "_assert_toolchain", lambda: None)
    monkeypatch.setattr(rebuild, "_build", lambda *_: b"approved")

    assert rebuild.verify(tmp_path, approved)
    monkeypatch.setattr(rebuild, "_build", lambda *_: b"changed")
    with pytest.raises(rebuild.McpbReproducibilityError, match="bytes differ"):
        rebuild.verify(tmp_path, approved)


def test_cli_requires_approved_directory() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--source-root", str(ROOT)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "--approved-dir" in completed.stderr
