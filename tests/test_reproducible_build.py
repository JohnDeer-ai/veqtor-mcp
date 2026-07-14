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


def test_pypi_mirror_must_match_the_approved_distribution_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = tmp_path / "approved"
    mirror = tmp_path / "mirror"
    approved.mkdir()
    mirror.mkdir()
    for directory in (approved, mirror):
        (directory / "package.whl").write_bytes(b"wheel")
        (directory / "package.tar.gz").write_bytes(b"sdist")

    monkeypatch.setattr(rebuild, "_assert_toolchain", lambda: None)
    monkeypatch.setattr(
        rebuild,
        "_build",
        lambda source_root, output: rebuild._artifact_bytes(approved),
    )

    assert set(rebuild.verify(tmp_path, approved, mirror)) == {
        "package.whl",
        "package.tar.gz",
    }
    (mirror / "package.whl").write_bytes(b"changed")
    with pytest.raises(rebuild.ReproducibilityError, match="bytes differ"):
        rebuild.verify(tmp_path, approved, mirror)


def test_cli_requires_an_approved_candidate_directory() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--source-root", str(ROOT)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "--approved-dir" in completed.stderr
