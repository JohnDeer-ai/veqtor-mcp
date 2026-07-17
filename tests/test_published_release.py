# SPDX-License-Identifier: Apache-2.0
"""Consumer-view verification of the immutable public release."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "verify_published_release.py"
SPEC = importlib.util.spec_from_file_location("verify_published_release", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
consumer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(consumer)

COMMIT = "a" * 40
VERSION = consumer.CONTRACT_VERSION
NOTES = ROOT / ".github" / "release-notes" / f"v{VERSION}.md"


def _release_fixture(tmp_path: Path, *, immutable: bool = True):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / f"veqtor_mcp-{VERSION}-py3-none-any.whl"
    sdist = dist / f"veqtor_mcp-{VERSION}.tar.gz"
    mcpb = dist / consumer.MCPB_FILENAME
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    mcpb.write_bytes(b"mcpb")
    manifest = dist / "SHA256SUMS.txt"
    manifest.write_text(
        f"{hashlib.sha256(wheel.read_bytes()).hexdigest()}  {wheel.name}\n"
        f"{hashlib.sha256(sdist.read_bytes()).hexdigest()}  {sdist.name}\n"
        f"{hashlib.sha256(mcpb.read_bytes()).hexdigest()}  {mcpb.name}\n"
    )
    payloads = {
        path.name: path.read_bytes() for path in (wheel, sdist, mcpb, manifest)
    }
    assets = [
        {
            "name": name,
            "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
            "browser_download_url": f"https://downloads.example/{name}",
        }
        for name, payload in payloads.items()
    ]
    release = {
        "tag_name": f"v{VERSION}",
        "name": f"Veqtor v{VERSION} Alpha",
        "body": NOTES.read_text(),
        "draft": False,
        "prerelease": True,
        "immutable": immutable,
        "assets": assets,
    }
    return dist, payloads, release


def test_public_consumer_verifies_flat_bytes_metadata_and_artifacts(tmp_path: Path) -> None:
    dist, payloads, release = _release_fixture(tmp_path)
    verified = []

    def get_json(url: str):
        return (
            {"object": {"type": "commit", "sha": COMMIT}}
            if "/git/ref/" in url
            else release
        )

    consumer.verify(
        repository="example/veqtor",
        version=VERSION,
        commit_sha=COMMIT,
        dist_dir=dist,
        source_root=ROOT,
        notes_path=NOTES,
        get_json=get_json,
        download=lambda url: payloads[url.rsplit("/", 1)[1]],
        artifact_verifier=lambda directory, root, sha: verified.append(
            (set(path.name for path in directory.iterdir()), root, sha)
        ),
    )

    assert verified == [(set(payloads), ROOT, COMMIT)]


def test_local_release_set_rejects_directory_and_symlink(tmp_path: Path) -> None:
    dist, _, _ = _release_fixture(tmp_path)
    (dist / "private").mkdir()
    with pytest.raises(consumer.ConsumerVerificationError, match="incomplete"):
        consumer._local_assets(dist)

    (dist / "private").rmdir()
    wheel = dist / f"veqtor_mcp-{VERSION}-py3-none-any.whl"
    real_wheel = tmp_path / "real-wheel"
    wheel.replace(real_wheel)
    wheel.symlink_to(real_wheel)
    with pytest.raises(consumer.ConsumerVerificationError, match="incomplete"):
        consumer._local_assets(dist)


def test_public_consumer_rejects_mutable_release(tmp_path: Path) -> None:
    dist, payloads, release = _release_fixture(tmp_path, immutable=False)

    with pytest.raises(consumer.ConsumerVerificationError, match="metadata"):
        consumer.verify(
            repository="example/veqtor",
            version=VERSION,
            commit_sha=COMMIT,
            dist_dir=dist,
            source_root=ROOT,
            notes_path=NOTES,
            get_json=lambda url: (
                {"object": {"type": "commit", "sha": COMMIT}}
                if "/git/ref/" in url
                else release
            ),
            download=lambda url: payloads[url.rsplit("/", 1)[1]],
            artifact_verifier=lambda *_: None,
        )


def test_public_consumer_rejects_changed_asset_bytes(tmp_path: Path) -> None:
    dist, payloads, release = _release_fixture(tmp_path)

    with pytest.raises(consumer.ConsumerVerificationError, match="bytes differ"):
        consumer.verify(
            repository="example/veqtor",
            version=VERSION,
            commit_sha=COMMIT,
            dist_dir=dist,
            source_root=ROOT,
            notes_path=NOTES,
            get_json=lambda url: (
                {"object": {"type": "commit", "sha": COMMIT}}
                if "/git/ref/" in url
                else release
            ),
            download=lambda url: b"changed"
            if url.endswith(".whl")
            else payloads[url.rsplit("/", 1)[1]],
            artifact_verifier=lambda *_: None,
        )


def test_public_consumer_rejects_duplicate_assets(tmp_path: Path) -> None:
    dist, payloads, release = _release_fixture(tmp_path)
    release["assets"].append(dict(release["assets"][0]))

    with pytest.raises(consumer.ConsumerVerificationError, match="asset set"):
        consumer.verify(
            repository="example/veqtor",
            version=VERSION,
            commit_sha=COMMIT,
            dist_dir=dist,
            source_root=ROOT,
            notes_path=NOTES,
            get_json=lambda url: (
                {"object": {"type": "commit", "sha": COMMIT}}
                if "/git/ref/" in url
                else release
            ),
            download=lambda url: payloads[url.rsplit("/", 1)[1]],
            artifact_verifier=lambda *_: None,
        )
