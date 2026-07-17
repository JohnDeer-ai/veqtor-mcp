# SPDX-License-Identifier: Apache-2.0
"""Verify the public GitHub release surface without a release-write token."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Callable
from urllib.request import Request, urlopen

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from release_contract import (  # noqa: E402
    CHECKSUMS_FILENAME,
    GITHUB_PAYLOAD_FILENAMES,
    GITHUB_RELEASE_FILENAMES,
    MCPB_FILENAME,
    RELEASE_NOTES_PATH,
    RELEASE_TITLE,
    VERSION as CONTRACT_VERSION,
)


class ConsumerVerificationError(RuntimeError):
    """The public release differs from the approved consumer contract."""


def _public_bytes(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "veqtor-release-verifier",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.read()
    except OSError as exc:
        raise ConsumerVerificationError("public release download failed") from exc


def _public_json(url: str) -> dict:
    try:
        payload = json.loads(_public_bytes(url))
    except json.JSONDecodeError as exc:
        raise ConsumerVerificationError("public API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ConsumerVerificationError("public API returned a non-object")
    return payload


def _local_assets(dist_dir: Path) -> dict[str, bytes]:
    paths = [dist_dir / name for name in GITHUB_PAYLOAD_FILENAMES]
    paths.append(dist_dir / CHECKSUMS_FILENAME)
    try:
        observed_names = {path.name for path in dist_dir.iterdir()}
    except OSError as exc:
        raise ConsumerVerificationError("cannot inspect local release set") from exc
    if (
        observed_names != set(GITHUB_RELEASE_FILENAMES)
        or any(not stat.S_ISREG(path.lstat().st_mode) for path in paths)
    ):
        raise ConsumerVerificationError("local release set is incomplete")
    return {path.name: path.read_bytes() for path in paths}


def _verify_manifest(downloaded: dict[str, bytes]) -> None:
    try:
        lines = downloaded["SHA256SUMS.txt"].decode("utf-8").splitlines()
    except (KeyError, UnicodeError) as exc:
        raise ConsumerVerificationError("public checksum manifest is invalid") from exc
    observed: dict[str, str] = {}
    for line in lines:
        parts = line.split()
        if (
            len(parts) != 2
            or len(parts[0]) != 64
            or any(character not in "0123456789abcdef" for character in parts[0])
            or Path(parts[1]).name != parts[1]
        ):
            raise ConsumerVerificationError("public checksum paths are not flat")
        digest, name = parts
        if name in observed:
            raise ConsumerVerificationError("public checksum names are duplicated")
        observed[name] = digest
    expected = {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in downloaded.items()
        if name != "SHA256SUMS.txt"
    }
    if observed != expected:
        raise ConsumerVerificationError("public checksum manifest does not validate")


def _artifact_verifier(directory: Path, source_root: Path, commit_sha: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(source_root / "scripts" / "check_release_artifacts.py"),
            "--source-root",
            str(source_root),
            "--commit",
            commit_sha,
            *map(str, sorted(directory.glob("*.whl"))),
            *map(str, sorted(directory.glob("*.tar.gz"))),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ConsumerVerificationError("downloaded artifacts failed identity checks")
    mcpb = subprocess.run(
        [
            sys.executable,
            str(source_root / "scripts" / "check_mcpb_artifact.py"),
            "--source-root",
            str(source_root),
            "--commit",
            commit_sha,
            str(directory / MCPB_FILENAME),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if mcpb.returncode != 0:
        raise ConsumerVerificationError("downloaded MCPB failed identity checks")


def verify(
    *,
    repository: str,
    version: str,
    commit_sha: str,
    dist_dir: Path,
    source_root: Path,
    notes_path: Path,
    get_json: Callable[[str], dict] = _public_json,
    download: Callable[[str], bytes] = _public_bytes,
    artifact_verifier: Callable[[Path, Path, str], None] = _artifact_verifier,
) -> None:
    if version != CONTRACT_VERSION:
        raise ConsumerVerificationError("version differs from release contract")
    tag = f"v{version}"
    api = f"https://api.github.com/repos/{repository}"
    release = get_json(f"{api}/releases/tags/{tag}")
    tag_ref = get_json(f"{api}/git/ref/tags/{tag}")
    target = tag_ref.get("object")
    if (
        not isinstance(target, dict)
        or target.get("type") != "commit"
        or target.get("sha") != commit_sha
    ):
        raise ConsumerVerificationError("public tag does not target the approved commit")
    try:
        notes = notes_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConsumerVerificationError("cannot read approved release notes") from exc
    if (
        release.get("tag_name") != tag
        or release.get("name") != RELEASE_TITLE
        or release.get("draft") is not False
        or release.get("prerelease") is not True
        or release.get("immutable") is not True
        or (release.get("body") or "").rstrip("\n") != notes.rstrip("\n")
    ):
        raise ConsumerVerificationError("public release metadata differs from contract")

    local = _local_assets(dist_dir)
    assets = release.get("assets")
    if not isinstance(assets, list) or len(assets) != len(local):
        raise ConsumerVerificationError("public release asset set differs from contract")
    asset_names = [
        asset.get("name") for asset in assets if isinstance(asset, dict)
    ]
    if len(asset_names) != len(assets) or set(asset_names) != set(local):
        raise ConsumerVerificationError("public release asset set differs from contract")
    downloaded: dict[str, bytes] = {}
    for asset in assets:
        name = asset["name"]
        url = asset.get("browser_download_url")
        if not isinstance(url, str):
            raise ConsumerVerificationError("public release asset has no download URL")
        payload = download(url)
        digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        if payload != local[name] or asset.get("digest") != digest:
            raise ConsumerVerificationError("public release asset bytes differ from CI")
        downloaded[name] = payload
    _verify_manifest(downloaded)

    with tempfile.TemporaryDirectory(prefix="veqtor-public-release-") as tmp:
        directory = Path(tmp)
        for name, payload in downloaded.items():
            (directory / name).write_bytes(payload)
        artifact_verifier(directory, source_root, commit_sha)


def main() -> int:
    try:
        verify(
            repository=os.environ.get("GITHUB_REPOSITORY", ""),
            version=os.environ.get("VERSION", ""),
            commit_sha=os.environ.get("COMMIT_SHA", ""),
            dist_dir=Path(os.environ.get("DIST_DIR", "dist")),
            source_root=ROOT,
            notes_path=ROOT / RELEASE_NOTES_PATH,
        )
    except ConsumerVerificationError as exc:
        print(f"public release verification failed: {exc}", file=sys.stderr)
        return 1
    print("public release verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
