# SPDX-License-Identifier: Apache-2.0
"""Fail-closed, retry-safe promotion of verified GitHub release artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Sequence
from urllib.parse import quote
from urllib.request import Request, urlopen

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
AssetUploader = Callable[[str, Path], dict]
GITHUB_API_VERSION = "2026-03-10"
GITHUB_API_HEADER = f"X-GitHub-Api-Version: {GITHUB_API_VERSION}"


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


def _checked_json_value(
    result: subprocess.CompletedProcess[str], operation: str
) -> Any:
    if result.returncode != 0:
        raise PromotionError(f"{operation} failed")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PromotionError(f"{operation} returned invalid JSON") from exc
    return payload


def _checked_json(result: subprocess.CompletedProcess[str], operation: str) -> dict:
    payload = _checked_json_value(result, operation)
    if not isinstance(payload, dict):
        raise PromotionError(f"{operation} returned a non-object")
    return payload


def _api_command(
    args: Sequence[str], *, hostname: str | None = None
) -> list[str]:
    command = ["gh", "api", "-H", GITHUB_API_HEADER]
    if hostname is not None:
        command.extend(["--hostname", hostname])
    command.extend(args)
    return command


def _api_result(
    runner: Runner,
    args: Sequence[str],
    *,
    hostname: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return runner(_api_command(args, hostname=hostname))


def _api_json(
    runner: Runner,
    args: Sequence[str],
    operation: str,
    *,
    hostname: str | None = None,
) -> dict:
    return _checked_json(
        _api_result(runner, args, hostname=hostname),
        operation,
    )


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
    created = _api_result(
        runner,
        [
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
        payload = _api_json(
            runner,
            [f"repos/{repository}/git/ref/tags/{tag}"],
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


def _release_id(payload: dict) -> int:
    release_id = payload.get("id")
    if (
        not isinstance(release_id, int)
        or isinstance(release_id, bool)
        or release_id < 1
    ):
        raise PromotionError("release has no valid id")
    return release_id


def _release_by_id(
    runner: Runner,
    repository: str,
    release_id: int,
) -> dict:
    return _api_json(
        runner,
        [f"repos/{repository}/releases/{release_id}"],
        "release lookup",
    )


def _release_for_tag(
    runner: Runner,
    repository: str,
    tag: str,
) -> dict | None:
    result = _api_result(
        runner,
        [
            "--paginate",
            "--slurp",
            f"repos/{repository}/releases?per_page=100",
        ],
    )
    pages = _checked_json_value(result, "release list lookup")
    if not isinstance(pages, list) or any(
        not isinstance(page, list) for page in pages
    ):
        raise PromotionError("release list lookup returned invalid pages")
    releases: list[dict] = []
    for page in pages:
        for release in page:
            if not isinstance(release, dict):
                raise PromotionError("release list contains a non-object")
            if release.get("tag_name") == tag:
                releases.append(release)
    if len(releases) > 1:
        raise PromotionError("multiple releases exist for the approved tag")
    return releases[0] if releases else None


def _validate_release_metadata(
    payload: dict,
    tag: str,
    expected_body: str,
    *,
    published: bool,
) -> None:
    _release_id(payload)
    if payload.get("tag_name") != tag:
        raise PromotionError("release tag does not match the approved tag")
    if payload.get("name") != RELEASE_TITLE:
        raise PromotionError("release title does not match the Alpha contract")
    if payload.get("prerelease") is not True:
        raise PromotionError("release is not marked as an Alpha prerelease")
    if (payload.get("body") or "").rstrip("\n") != expected_body.rstrip("\n"):
        raise PromotionError("release body does not match the approved notes")
    if payload.get("draft") is not (not published):
        raise PromotionError("release has an invalid draft state")
    if published and payload.get("immutable") is not True:
        raise PromotionError("published release is not immutable")


def _create_or_recover_release(
    runner: Runner,
    repository: str,
    tag: str,
    notes_path: Path,
    expected_body: str,
) -> dict:
    existing = _release_for_tag(runner, repository, tag)
    if existing is not None:
        return existing
    created = _api_result(
        runner,
        [
            "--method",
            "POST",
            f"repos/{repository}/releases",
            "-f",
            f"tag_name={tag}",
            "-f",
            f"name={RELEASE_TITLE}",
            "-F",
            "draft=true",
            "-F",
            "prerelease=true",
            "-F",
            f"body=@{notes_path}",
        ],
    )
    if created.returncode == 0:
        payload = _checked_json(created, "draft release creation")
    else:
        # A retry or race may have created the draft after the initial list.
        payload = _release_for_tag(runner, repository, tag)
        if payload is None:
            raise PromotionError("draft release creation failed")
    _validate_release_metadata(
        payload,
        tag,
        expected_body,
        published=False,
    )
    return payload


def _release_asset_map(payload: dict) -> dict[str, dict]:
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise PromotionError("release assets are not an array")
    observed: dict[str, dict] = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise PromotionError("release contains malformed asset metadata")
        name = asset["name"]
        if name in observed:
            raise PromotionError("release contains duplicate asset names")
        observed[name] = asset
    return observed


def _asset_matches(
    asset: dict,
    expected: dict[str, object],
) -> bool:
    return (
        asset.get("state") == "uploaded"
        and asset.get("size") == expected["size"]
        and asset.get("digest") == expected["digest"]
    )


def _delete_draft_asset(
    runner: Runner,
    repository: str,
    asset: dict,
) -> None:
    asset_id = asset.get("id")
    if not isinstance(asset_id, int) or isinstance(asset_id, bool) or asset_id < 1:
        raise PromotionError("draft asset has no valid id")
    deleted = _api_result(
        runner,
        [
            "--method",
            "DELETE",
            f"repos/{repository}/releases/assets/{asset_id}",
        ],
    )
    if deleted.returncode != 0:
        raise PromotionError("draft asset replacement failed")


def _draft_asset_upload_url(
    payload: dict,
    repository: str,
    release_id: int,
    path: Path,
) -> str:
    upload_url = payload.get("upload_url")
    if not isinstance(upload_url, str):
        raise PromotionError("draft release has no upload_url")
    base_url = upload_url.split("{", 1)[0]
    expected_url = (
        f"https://uploads.github.com/repos/{repository}/releases/"
        f"{release_id}/assets"
    )
    if base_url != expected_url:
        raise PromotionError("draft release has an unexpected upload_url")
    return f"{base_url}?name={quote(path.name)}"


def _default_asset_uploader(upload_url: str, path: Path) -> dict:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise PromotionError("draft asset upload requires GH_TOKEN")
    try:
        request = Request(
            upload_url,
            data=path.read_bytes(),
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
                "User-Agent": "veqtor-release-promoter",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )
        with urlopen(request, timeout=60) as response:  # noqa: S310
            payload = json.loads(response.read())
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise PromotionError("draft asset upload failed") from exc
    if not isinstance(payload, dict):
        raise PromotionError("draft asset upload returned a non-object")
    return payload


def _upload_draft_asset(
    asset_uploader: AssetUploader,
    payload: dict,
    repository: str,
    release_id: int,
    path: Path,
) -> None:
    upload_url = _draft_asset_upload_url(
        payload,
        repository,
        release_id,
        path,
    )
    uploaded = asset_uploader(upload_url, path)
    if uploaded.get("name") != path.name:
        raise PromotionError("draft asset upload failed")


def _upload_draft_assets(
    runner: Runner,
    asset_uploader: AssetUploader,
    repository: str,
    payload: dict,
    expected: dict[str, dict[str, object]],
    paths: dict[str, Path],
) -> None:
    release_id = _release_id(payload)
    observed = _release_asset_map(payload)
    unexpected = set(observed) - set(expected)
    if unexpected:
        raise PromotionError("draft release contains unexpected assets")
    for name in sorted(expected):
        asset = observed.get(name)
        if asset is not None and _asset_matches(asset, expected[name]):
            continue
        if asset is not None:
            _delete_draft_asset(runner, repository, asset)
        _upload_draft_asset(
            asset_uploader,
            payload,
            repository,
            release_id,
            paths[name],
        )


def _validate_release(
    payload: dict,
    tag: str,
    expected_assets: dict[str, dict[str, object]],
    expected_body: str,
    *,
    published: bool,
) -> None:
    _validate_release_metadata(
        payload,
        tag,
        expected_body,
        published=published,
    )
    observed: dict[str, dict[str, object]] = {}
    for name, asset in _release_asset_map(payload).items():
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
    asset_uploader: AssetUploader = _default_asset_uploader,
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
    settings = _api_json(
        admin_runner or runner,
        [f"repos/{repository}/immutable-releases"],
        "immutable releases setting lookup",
    )
    if settings.get("enabled") is not True:
        raise PromotionError("immutable releases are not enabled")

    tag = f"v{version}"
    main_ref = _api_json(
        runner,
        [f"repos/{repository}/git/ref/heads/main"],
        "main reference lookup",
    )
    target = main_ref.get("object")
    if not isinstance(target, dict) or target.get("type") != "commit":
        raise PromotionError("main reference is not a commit")
    main_sha = target.get("sha")

    existing_tag = _api_result(
        runner,
        [f"repos/{repository}/git/ref/tags/{tag}"],
    )
    if existing_tag.returncode == 0:
        _validate_ref(_checked_json(existing_tag, "existing tag lookup"), commit_sha)
        if main_sha != commit_sha:
            if run_attempt <= 1:
                raise PromotionError("tag recovery requires a later run attempt")
            comparison = _api_json(
                runner,
                [f"repos/{repository}/compare/{commit_sha}...main"],
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
    asset_paths = {
        path.name: path
        for path in (
            *sorted(dist_dir.glob("*.whl")),
            *sorted(dist_dir.glob("*.tar.gz")),
            checksums,
        )
    }
    payload = _create_or_recover_release(
        runner,
        repository,
        tag,
        notes_path,
        expected_body,
    )

    if payload.get("draft") is True:
        _validate_release_metadata(
            payload,
            tag,
            expected_body,
            published=False,
        )
        _upload_draft_assets(
            runner,
            asset_uploader,
            repository,
            payload,
            expected,
            asset_paths,
        )
        release_id = _release_id(payload)
        payload = _release_by_id(runner, repository, release_id)
        _validate_release(
            payload,
            tag,
            expected,
            expected_body,
            published=False,
        )
        published = _api_result(
            runner,
            [
                "--method",
                "PATCH",
                f"repos/{repository}/releases/{release_id}",
                "-F",
                "draft=false",
            ]
        )
        if published.returncode != 0:
            raise PromotionError("release publication failed")
        _checked_json(published, "release publication")
        payload = _release_by_id(runner, repository, release_id)
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
