# SPDX-License-Identifier: Apache-2.0
"""Unit boundaries for the independent byte-equality build gate."""

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_reproducible_build.py"
SPEC = importlib.util.spec_from_file_location("check_reproducible_build", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
rebuild = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rebuild)


def test_rebuild_comparison_requires_exact_names_and_bytes() -> None:
    artifacts = {"package.whl": b"wheel", "package.tar.gz": b"sdist"}
    rebuild._compare(artifacts, dict(artifacts))

    with pytest.raises(rebuild.ReproducibilityError, match="filenames"):
        rebuild._compare(artifacts, {"package.whl": b"wheel"})
    with pytest.raises(rebuild.ReproducibilityError, match="bytes differ"):
        rebuild._compare(
            artifacts,
            {"package.whl": b"changed", "package.tar.gz": b"sdist"},
        )


def test_artifact_directory_requires_one_wheel_and_one_sdist(tmp_path: Path) -> None:
    (tmp_path / "package.whl").write_bytes(b"wheel")
    with pytest.raises(rebuild.ReproducibilityError, match="one wheel"):
        rebuild._artifact_bytes(tmp_path)

    (tmp_path / "package.tar.gz").write_bytes(b"sdist")
    assert rebuild._artifact_bytes(tmp_path) == {
        "package.whl": b"wheel",
        "package.tar.gz": b"sdist",
    }


def test_cli_requires_an_approved_candidate_directory() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--source-root", str(ROOT)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "--approved-dir" in completed.stderr
