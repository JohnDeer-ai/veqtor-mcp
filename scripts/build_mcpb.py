# SPDX-License-Identifier: Apache-2.0
"""Build the deterministic macOS Claude Desktop extension artifact."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_mcpb_artifact import (  # noqa: E402
    expected_member_payloads,
    verify,
)
from release_contract import MCPB_FILENAME  # noqa: E402

MCPB_ZIP_DATE = (2020, 2, 2, 0, 0, 0)
MCPB_MEMBER_MODE = 0o644


def _write_bundle(path: Path, payloads: dict[str, bytes]) -> None:
    with zipfile.ZipFile(
        path,
        mode="w",
        compression=zipfile.ZIP_STORED,
        strict_timestamps=True,
    ) as archive:
        archive.comment = b""
        for name, payload in sorted(payloads.items()):
            info = zipfile.ZipInfo(name, date_time=MCPB_ZIP_DATE)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = MCPB_MEMBER_MODE << 16
            info.internal_attr = 0
            info.extra = b""
            info.comment = b""
            archive.writestr(
                info,
                payload,
                compress_type=zipfile.ZIP_STORED,
            )


def _write_stage(directory: Path, payloads: dict[str, bytes]) -> None:
    if directory.exists():
        if not directory.is_dir() or any(directory.iterdir()):
            raise ValueError("MCPB stage directory must be absent or empty")
    else:
        directory.mkdir(parents=True)
    for name, payload in sorted(payloads.items()):
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        target.chmod(MCPB_MEMBER_MODE)


def build(
    source_root: Path,
    out_dir: Path,
    stage_dir: Path | None = None,
) -> Path:
    source_root = source_root.resolve()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / MCPB_FILENAME
    payloads = expected_member_payloads(source_root, "WORKTREE")
    if stage_dir is not None:
        _write_stage(stage_dir.resolve(), payloads)
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            dir=out_dir,
            prefix=f".{MCPB_FILENAME}.",
            suffix=".tmp",
        )
        os.close(descriptor)
        temporary = Path(raw_path)
        _write_bundle(temporary, payloads)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
        temporary = None
        verify(target, source_root, "WORKTREE")
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--stage-dir", type=Path)
    options = parser.parse_args(argv)
    try:
        path = build(options.source_root, options.out_dir, options.stage_dir)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError) as exc:
        print(f"MCPB build failed: {exc}", file=sys.stderr)
        return 1
    print(f"{digest}  {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
