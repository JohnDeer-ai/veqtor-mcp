# SPDX-License-Identifier: Apache-2.0
"""Permit website-only main drift only with unchanged release artifact bytes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from release_contract import (
    MCPB_SOURCE_MAP,
    SDIST_GIT_FILES,
    WHEEL_LICENSE_MAP,
    WHEEL_SOURCE_MAP,
)

RELEASE_INPUTS = frozenset(MCPB_SOURCE_MAP.values()) | SDIST_GIT_FILES | frozenset(
    (*WHEEL_SOURCE_MAP.values(), *WHEEL_LICENSE_MAP.values())
)


class SiteDriftError(RuntimeError):
    """Main is not a website-only, artifact-identical descendant."""


def _run(root: Path, args: list[str]) -> str:
    environment = os.environ.copy()
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "RELEASE_ADMIN_TOKEN"):
        environment.pop(name, None)
    completed = subprocess.run(
        args, cwd=root, env=environment, capture_output=True, text=True, check=False
    )
    if completed.returncode:
        raise SiteDriftError(f"site drift proof failed: {args[0]} {args[1]}")
    return completed.stdout


def check_scope(root: Path, candidate: str, main: str) -> list[str]:
    for sha in (candidate, main):
        if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            raise SiteDriftError("candidate and main must be full lowercase commit SHAs")
        _run(root, ["git", "cat-file", "-e", f"{sha}^{{commit}}"])
    _run(root, ["git", "merge-base", "--is-ancestor", candidate, main])
    # No REST file-count cap; disabling rename detection also exposes an input
    # moved out of the package into website/. NUL delimiters preserve filenames.
    changed = _run(root, [
        "git", "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
        "--name-only", "-z", candidate, main, "--",
    ]).split("\0")[:-1]
    rejected = [
        path for path in changed
        if not path.startswith("website/") or path in RELEASE_INPUTS
    ]
    if rejected:
        raise SiteDriftError(f"main changed release inputs or non-website paths: {rejected!r}")
    return changed


def verify(root: Path, candidate: str, main: str, approved_dir: Path) -> dict:
    # Fetch only the frozen observed SHA; all subsequent reads use that SHA.
    if re.fullmatch(r"[0-9a-f]{40}", main) is None:
        raise SiteDriftError("main must be a full lowercase commit SHA")
    _run(root, ["git", "fetch", "--no-tags", "origin", main])
    changed = check_scope(root, candidate, main)
    if _run(root, ["git", "rev-parse", "HEAD"]).strip() != candidate:
        raise SiteDriftError("proof must run from the exact candidate checkout")
    with tempfile.TemporaryDirectory(prefix="veqtor-site-drift-") as temporary:
        source = Path(temporary) / "main"
        _run(root, ["git", "worktree", "add", "--detach", str(source), main])
        try:
            proofs = {}
            for script in ("check_reproducible_build.py", "check_reproducible_mcpb.py"):
                proofs[script] = _run(root, [
                    sys.executable, str(root / "scripts" / script),
                    "--source-root", str(source),
                    "--approved-dir", str(approved_dir.resolve()),
                ]).strip()
        finally:
            _run(root, ["git", "worktree", "remove", "--force", str(source)])
    report = {"candidate": candidate, "main": main, "changed_paths": changed, "proofs": proofs}
    print(json.dumps(report, sort_keys=True))
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--main", required=True)
    options = parser.parse_args()
    try:
        changed = check_scope(options.source_root.resolve(), options.candidate, options.main)
    except (OSError, SiteDriftError) as exc:
        print(f"release scope refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"changed_paths": changed}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
