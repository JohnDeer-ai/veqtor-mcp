# SPDX-License-Identifier: Apache-2.0
"""Independently rebuild and byte-compare the v0.1 release artifacts."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_contract import (
    BUILD_PYTHON_VERSION,
    BUILD_UV_VERSION,
    SOURCE_DATE_EPOCH,
)


class ReproducibilityError(RuntimeError):
    """The build environment or rebuilt bytes differ from the contract."""


def _tool_version(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ReproducibilityError(f"cannot execute {command[0]}")
    return completed.stdout.strip()


def _assert_toolchain() -> None:
    if platform.python_version() != BUILD_PYTHON_VERSION:
        raise ReproducibilityError(
            f"expected Python {BUILD_PYTHON_VERSION}, got {platform.python_version()}"
        )
    uv_version = _tool_version(["uv", "--version"]).split()
    if len(uv_version) < 2 or uv_version[:2] != ["uv", BUILD_UV_VERSION]:
        raise ReproducibilityError("uv version differs from release contract")


def _artifact_bytes(directory: Path) -> dict[str, bytes]:
    paths = [*directory.glob("*.whl"), *directory.glob("*.tar.gz")]
    if len(paths) != 2 or len({path.name for path in paths}) != 2:
        raise ReproducibilityError("expected exactly one wheel and one sdist")
    return {path.name: path.read_bytes() for path in paths}


def _build(source_root: Path, output: Path) -> dict[str, bytes]:
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
    completed = subprocess.run(
        ["uv", "build", "--clear", "--out-dir", str(output)],
        cwd=source_root,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        raise ReproducibilityError("independent build failed")
    return _artifact_bytes(output)


def _compare(expected: dict[str, bytes], observed: dict[str, bytes]) -> None:
    if set(expected) != set(observed):
        raise ReproducibilityError("release artifact filenames differ")
    for name in sorted(expected):
        if expected[name] != observed[name]:
            raise ReproducibilityError(f"rebuilt bytes differ for {name}")


def verify(source_root: Path, approved_dir: Path) -> dict[str, str]:
    _assert_toolchain()
    with tempfile.TemporaryDirectory(prefix="veqtor-rebuild-") as temporary:
        root = Path(temporary)
        expected = _artifact_bytes(approved_dir)
        observed = _build(source_root, root / "independent")
        _compare(expected, observed)
    return {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in sorted(expected.items())
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--approved-dir", type=Path, required=True)
    options = parser.parse_args(argv)
    try:
        digests = verify(options.source_root.resolve(), options.approved_dir)
    except (OSError, ReproducibilityError) as exc:
        print(f"reproducible build check failed: {exc}", file=sys.stderr)
        return 1
    for name, digest in digests.items():
        print(f"{digest}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
