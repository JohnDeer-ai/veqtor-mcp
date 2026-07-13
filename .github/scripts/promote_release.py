# SPDX-License-Identifier: Apache-2.0
"""Fail-closed, retry-safe promotion of verified GitHub release artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable, Sequence

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from release_contract import (  # noqa: E402
    RELEASE_NOTES_PATH,
    RELEASE_TITLE,
    VERSION as CONTRACT_VERSION,
)


class PromotionError(RuntimeError):
    """Raised when remote release state is not exactly the approved state."""


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _default_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=False,
        capture_output=True,
        text=True,
    )


def _admin_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    token = os.environ.get("RELEASE_ADMIN_TOKEN")
    if not token:
        return subprocess.CompletedProcess(list(args), 1, "", "missing admin token")
    environment = os.environ.copy()
    environment["GH_TOKEN"] = token
    return subprocess.run(
        list(args),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _checked_json(result: subprocess.CompletedProcess[str], operation: str) -> dict:
    if result.returncode != 0:
        raise PromotionError(f"{operation} failed")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PromotionError(f"{operation} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PromotionError(f"{operation} returned a non-object")
    return payload


def _gh_json(runner: Runner, args: Sequence[str], operation: str) -> dict:
    return _checked_json(runner(["gh", *args]), operation)


def _validate_ref(payload: dict, commit_sha: str) -> None:
    target = payload.get("object")
    if not isinstance(target, dict):
        raise PromotionError("tag reference has no object")
    if target.get("type") != "commit" or target.get("sha") != commit_sha:
        raise PromotionError("tag reference is not the approved lightweight tag")


def _ensure_tag(
    runner: Runner,
    repository: str,
    tag: str,
    commit_sha: str,
) -> None:
    endpoint = f"repos/{repository}/git/refs"
    created = runner(
        [
            "gh",
            "api",
            "--method",
            "POST",
            endpoint,
            "-f",
            f"ref=refs/tags/{tag}",
            "-f",
            f"sha={commit_sha}",
        ]
    )
    if created.returncode == 0:
        payload = _checked_json(created, "tag creation")
    else:
        payload = _gh_json(
            runner,
            ["api", f"repos/{repository}/git/ref/tags/{tag}"],
            "existing tag lookup",
        )
    _validate_ref(payload, commit_sha)


def _expected_assets(dist_dir: Path, checksums: Path) -> dict[str, dict[str, object]]:
    candidates = [*dist_dir.glob("*.whl"), *dist_dir.glob("*.tar.gz"), checksums]
    if len(candidates) != 3 or any(not path.is_file() for path in candidates):
        raise PromotionError("expected one wheel, one sdist and SHA256SUMS.txt")
    if len({path.name for path in candidates}) != len(candidates):
        raise PromotionError("release artifact names are not unique")
    artifact_paths = [path for path in candidates if path != checksums]
    expected_manifest = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in artifact_paths
    }
    observed_manifest: dict[str, str] = {}
    try:
        lines = checksums.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PromotionError("cannot read SHA256SUMS.txt") from exc
    for line in lines:
        parts = line.split()
        if len(parts) != 2 or len(parts[0]) != 64:
            raise PromotionError("SHA256SUMS.txt has an invalid line")
        digest, name = parts
        if Path(name).name != name or name in observed_manifest:
            raise PromotionError("SHA256SUMS.txt must contain unique flat names")
        if any(char not in "0123456789abcdef" for char in digest):
            raise PromotionError("SHA256SUMS.txt has an invalid digest")
        observed_manifest[name] = digest
    if observed_manifest != expected_manifest:
        raise PromotionError("SHA256SUMS.txt does not match the verified artifacts")
    return {
        path.name: {
            "size": path.stat().st_size,
            "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
        }
        for path in candidates
    }


def _release(
    runner: Runner,
    repository: str,
    tag: str,
) -> dict:
    return _gh_json(
        runner,
        ["api", f"repos/{repository}/releases/tags/{tag}"],
        "release lookup",
    )


def _validate_release(
    payload: dict,
    tag: str,
    expected_assets: dict[str, dict[str, object]],
    expected_body: str,
    *,
    published: bool,
) -> None:
    if payload.get("tag_name") != tag:
        raise PromotionError("release tag does not match the approved tag")
    if payload.get("name") != RELEASE_TITLE:
        raise PromotionError("release title does not match the Alpha contract")
    if payload.get("prerelease") is not True:
        raise PromotionError("release is not marked as an Alpha prerelease")
    if (payload.get("body") or "").rstrip("\n") != expected_body.rstrip("\n"):
        raise PromotionError("release body does not match the approved notes")
    if published and payload.get("immutable") is not True:
        raise PromotionError("published release is not immutable")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise PromotionError("release assets are not an array")
    observed: dict[str, dict[str, object]] = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise PromotionError("release contains malformed asset metadata")
        name = asset["name"]
        if name in observed:
            raise PromotionError("release contains duplicate asset names")
        observed[name] = {
            "size": asset.get("size"),
            "digest": asset.get("digest"),
        }
        if asset.get("state") != "uploaded":
            raise PromotionError("release contains an incomplete asset")
    if observed != expected_assets:
        raise PromotionError("release assets do not match the verified artifacts")


def promote(
    *,
    runner: Runner = _default_runner,
    admin_runner: Runner | None = None,
    repository: str,
    version: str,
    commit_sha: str,
    dist_dir: Path,
    checksums: Path,
    notes_path: Path,
    run_attempt: int = 1,
) -> None:
    if not repository or not version:
        raise PromotionError("repository and version are required")
    if version != CONTRACT_VERSION:
        raise PromotionError("version differs from the release contract")
    if len(commit_sha) != 40 or any(
        char not in "0123456789abcdef" for char in commit_sha
    ):
        raise PromotionError("commit SHA must be 40 lowercase hexadecimal characters")

    try:
        expected_body = notes_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PromotionError("cannot read approved release notes") from exc
    settings = _gh_json(
        admin_runner or runner,
        ["api", f"repos/{repository}/immutable-releases"],
        "immutable releases setting lookup",
    )
    if settings.get("enabled") is not True:
        raise PromotionError("immutable releases are not enabled")

    tag = f"v{version}"
    main_ref = _gh_json(
        runner,
        ["api", f"repos/{repository}/git/ref/heads/main"],
        "main reference lookup",
    )
    target = main_ref.get("object")
    if not isinstance(target, dict) or target.get("type") != "commit":
        raise PromotionError("main reference is not a commit")
    main_sha = target.get("sha")

    existing_tag = runner(
        ["gh", "api", f"repos/{repository}/git/ref/tags/{tag}"]
    )
    if existing_tag.returncode == 0:
        _validate_ref(_checked_json(existing_tag, "existing tag lookup"), commit_sha)
        if main_sha != commit_sha:
            if run_attempt <= 1:
                raise PromotionError("tag recovery requires a later run attempt")
            comparison = _gh_json(
                runner,
                ["api", f"repos/{repository}/compare/{commit_sha}...main"],
                "candidate ancestry lookup",
            )
            merge_base = comparison.get("merge_base_commit")
            if (
                comparison.get("status") not in {"ahead", "identical"}
                or not isinstance(merge_base, dict)
                or merge_base.get("sha") != commit_sha
            ):
                raise PromotionError("approved commit is no longer an ancestor of main")
    else:
        if main_sha != commit_sha:
            raise PromotionError("first promotion requires main at the approved commit")
        _ensure_tag(runner, repository, tag, commit_sha)
    expected = _expected_assets(dist_dir, checksums)

    created = runner(
        [
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            repository,
            "--draft",
            "--verify-tag",
            "--prerelease",
            "--title",
            RELEASE_TITLE,
            "--notes-file",
            str(notes_path),
        ]
    )
    if created.returncode != 0:
        # A prior attempt may have created the draft or even published it.
        # Continue only if an authenticated exact-tag lookup proves it exists.
        payload = _release(runner, repository, tag)
    else:
        payload = _release(runner, repository, tag)

    if payload.get("draft") is True:
        uploaded = runner(
            [
                "gh",
                "release",
                "upload",
                tag,
                *[str(dist_dir / name) for name in sorted(expected) if name != checksums.name],
                str(checksums),
                "--repo",
                repository,
                "--clobber",
            ]
        )
        if uploaded.returncode != 0:
            raise PromotionError("draft asset upload failed")
        payload = _release(runner, repository, tag)
        _validate_release(
            payload,
            tag,
            expected,
            expected_body,
            published=False,
        )
        published = runner(
            [
                "gh",
                "release",
                "edit",
                tag,
                "--repo",
                repository,
                "--draft=false",
            ]
        )
        if published.returncode != 0:
            raise PromotionError("release publication failed")
        payload = _release(runner, repository, tag)
        if payload.get("draft") is not False:
            raise PromotionError("release remained a draft after publication")
        _validate_release(
            payload,
            tag,
            expected,
            expected_body,
            published=True,
        )
        return

    if payload.get("draft") is not False:
        raise PromotionError("release has an invalid draft state")
    # Never clobber or edit an already-published release. A retry is successful
    # only when its immutable public surface already matches the approved bits.
    _validate_release(
        payload,
        tag,
        expected,
        expected_body,
        published=True,
    )


def main() -> int:
    try:
        promote(
            admin_runner=_admin_runner,
            repository=os.environ.get("GITHUB_REPOSITORY", ""),
            version=os.environ.get("VERSION", ""),
            commit_sha=os.environ.get("COMMIT_SHA", ""),
            dist_dir=Path(os.environ.get("DIST_DIR", "dist")),
            checksums=Path(
                os.environ.get("CHECKSUMS_PATH", "dist/SHA256SUMS.txt")
            ),
            notes_path=Path(
                os.environ.get("RELEASE_NOTES_PATH", RELEASE_NOTES_PATH)
            ),
            run_attempt=int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
        )
    except PromotionError as exc:
        print(f"release promotion refused: {exc}", file=sys.stderr)
        return 1
    print("release promotion completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
