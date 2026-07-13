# SPDX-License-Identifier: Apache-2.0
"""State-machine tests for retry-safe GitHub release promotion."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "promote_release.py"
SPEC = importlib.util.spec_from_file_location("promote_release", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
promotion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(promotion)

COMMIT = "a" * 40
REPOSITORY = "example/veqtor"
TAG = "v0.1.0"
NOTES_PATH = ROOT / ".github" / "release-notes" / "v0.1.0.md"
NOTES = NOTES_PATH.read_text(encoding="utf-8")
TITLE = "Veqtor v0.1.0 Alpha"


def _completed(args: list[str], returncode: int = 0, payload=None):
    stdout = "" if payload is None else json.dumps(payload)
    return subprocess.CompletedProcess(args, returncode, stdout, "refused")


def _ref(sha: str = COMMIT, kind: str = "commit") -> dict:
    return {"object": {"type": kind, "sha": sha}}


def _assets(paths: list[Path]) -> list[dict]:
    return [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
            "state": "uploaded",
        }
        for path in paths
    ]


def _artifacts(tmp_path: Path) -> tuple[Path, Path, list[Path]]:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    wheel = dist / "veqtor_mcp-0.1.0-py3-none-any.whl"
    sdist = dist / "veqtor_mcp-0.1.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    sums = dist / "SHA256SUMS.txt"
    sums.write_text(
        f"{hashlib.sha256(wheel.read_bytes()).hexdigest()}  {wheel.name}\n"
        f"{hashlib.sha256(sdist.read_bytes()).hexdigest()}  {sdist.name}\n",
        encoding="utf-8",
    )
    return dist, sums, [wheel, sdist, sums]


class FakeGitHub:
    def __init__(
        self,
        assets: list[dict],
        *,
        tag_exists: bool = False,
        tag_sha: str = COMMIT,
        tag_kind: str = "commit",
        release_state: str = "absent",
        upload_ok: bool = True,
        main_sha: str = COMMIT,
        comparison_status: str = "ahead",
        immutable_enabled: bool = True,
        release_title: str = TITLE,
        prerelease: bool = True,
    ) -> None:
        self.assets = assets
        self.tag_exists = tag_exists
        self.tag_sha = tag_sha
        self.tag_kind = tag_kind
        self.release_state = release_state
        self.upload_ok = upload_ok
        self.main_sha = main_sha
        self.comparison_status = comparison_status
        self.immutable_enabled = immutable_enabled
        self.release_title = release_title
        self.prerelease = prerelease
        self.calls: list[list[str]] = []

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        joined = " ".join(args)
        if "/immutable-releases" in joined:
            return _completed(args, payload={"enabled": self.immutable_enabled})
        if "git/ref/heads/main" in joined:
            return _completed(args, payload=_ref(self.main_sha))
        if "/compare/" in joined:
            return _completed(
                args,
                payload={
                    "status": self.comparison_status,
                    "merge_base_commit": {"sha": COMMIT},
                },
            )
        if "--method POST" in joined and "git/refs" in joined:
            if self.tag_exists:
                return _completed(args, 1)
            self.tag_exists = True
            return _completed(args, payload=_ref(self.tag_sha, self.tag_kind))
        if "git/ref/tags/" in joined:
            if not self.tag_exists:
                return _completed(args, 1)
            return _completed(args, payload=_ref(self.tag_sha, self.tag_kind))
        if "release create" in joined:
            if self.release_state == "absent":
                self.release_state = "draft"
                return _completed(args)
            return _completed(args, 1)
        if "release upload" in joined:
            if not self.upload_ok:
                return _completed(args, 1)
            return _completed(args)
        if "release edit" in joined:
            self.release_state = "published"
            return _completed(args)
        if "/releases/tags/" in joined:
            if self.release_state == "absent":
                return _completed(args, 1)
            return _completed(
                args,
                payload={
                    "tag_name": TAG,
                    "name": self.release_title,
                    "body": NOTES,
                    "prerelease": self.prerelease,
                    "draft": self.release_state == "draft",
                    "immutable": self.release_state == "published",
                    "assets": self.assets,
                },
            )
        raise AssertionError(f"unexpected command: {args}")


def _promote(tmp_path: Path, fake: FakeGitHub) -> None:
    dist, sums, _ = _artifacts(tmp_path)
    promotion.promote(
        runner=fake,
        repository=REPOSITORY,
        version="0.1.0",
        commit_sha=COMMIT,
        dist_dir=dist,
        checksums=sums,
        notes_path=NOTES_PATH,
    )


def test_new_release_is_uploaded_as_draft_verified_and_published(tmp_path: Path) -> None:
    dist, sums, paths = _artifacts(tmp_path)
    fake = FakeGitHub(_assets(paths))
    promotion.promote(
        runner=fake,
        repository=REPOSITORY,
        version="0.1.0",
        commit_sha=COMMIT,
        dist_dir=dist,
        checksums=sums,
        notes_path=NOTES_PATH,
    )

    assert fake.release_state == "published"
    assert any("release upload" in " ".join(call) for call in fake.calls)
    assert any("--clobber" in call for call in fake.calls)
    create = next(call for call in fake.calls if "release create" in " ".join(call))
    assert "--prerelease" in create
    assert create[create.index("--title") + 1] == TITLE
    assert create[create.index("--notes-file") + 1] == str(NOTES_PATH)


def test_separately_approved_dispatch_accepts_exact_existing_tag_and_draft(
    tmp_path: Path,
) -> None:
    dist, sums, paths = _artifacts(tmp_path)
    fake = FakeGitHub(
        _assets(paths),
        tag_exists=True,
        release_state="draft",
    )
    promotion.promote(
        runner=fake,
        repository=REPOSITORY,
        version="0.1.0",
        commit_sha=COMMIT,
        dist_dir=dist,
        checksums=sums,
        notes_path=NOTES_PATH,
        run_attempt=1,
    )

    assert fake.release_state == "published"
    assert any("git/ref/tags/" in " ".join(call) for call in fake.calls)


@pytest.mark.parametrize(
    ("sha", "kind"),
    [("b" * 40, "commit"), (COMMIT, "tag")],
)
def test_retry_rejects_mismatched_or_annotated_tag(
    tmp_path: Path,
    sha: str,
    kind: str,
) -> None:
    _, _, paths = _artifacts(tmp_path)
    fake = FakeGitHub(
        _assets(paths),
        tag_exists=True,
        tag_sha=sha,
        tag_kind=kind,
    )

    with pytest.raises(promotion.PromotionError):
        _promote(tmp_path / "run", fake)


def test_interrupted_draft_upload_never_publishes(tmp_path: Path) -> None:
    _, _, paths = _artifacts(tmp_path)
    fake = FakeGitHub(_assets(paths), upload_ok=False)

    with pytest.raises(promotion.PromotionError, match="upload failed"):
        _promote(tmp_path / "run", fake)

    assert fake.release_state == "draft"
    assert not any("release edit" in " ".join(call) for call in fake.calls)


def test_existing_published_release_is_verified_without_mutation(tmp_path: Path) -> None:
    dist, sums, paths = _artifacts(tmp_path)
    fake = FakeGitHub(_assets(paths), release_state="published")
    promotion.promote(
        runner=fake,
        repository=REPOSITORY,
        version="0.1.0",
        commit_sha=COMMIT,
        dist_dir=dist,
        checksums=sums,
        notes_path=NOTES_PATH,
    )

    assert not any("release upload" in " ".join(call) for call in fake.calls)
    assert not any("release edit" in " ".join(call) for call in fake.calls)


def test_existing_published_release_with_wrong_asset_fails_closed(tmp_path: Path) -> None:
    _, _, paths = _artifacts(tmp_path)
    assets = _assets(paths)
    assets[0]["digest"] = "sha256:" + "0" * 64
    fake = FakeGitHub(assets, release_state="published")

    with pytest.raises(promotion.PromotionError, match="do not match"):
        _promote(tmp_path / "run", fake)

    assert not any("release upload" in " ".join(call) for call in fake.calls)


def test_recovery_after_main_advances_requires_later_attempt_and_ancestry(
    tmp_path: Path,
) -> None:
    dist, sums, paths = _artifacts(tmp_path)
    fake = FakeGitHub(
        _assets(paths),
        tag_exists=True,
        release_state="draft",
        main_sha="b" * 40,
    )

    promotion.promote(
        runner=fake,
        repository=REPOSITORY,
        version="0.1.0",
        commit_sha=COMMIT,
        dist_dir=dist,
        checksums=sums,
        notes_path=NOTES_PATH,
        run_attempt=2,
    )

    assert fake.release_state == "published"
    assert any(f"{COMMIT}...main" in " ".join(call) for call in fake.calls)


def test_new_dispatch_cannot_recover_after_main_advances(tmp_path: Path) -> None:
    _, _, paths = _artifacts(tmp_path)
    fake = FakeGitHub(
        _assets(paths),
        tag_exists=True,
        release_state="draft",
        main_sha="b" * 40,
    )

    with pytest.raises(promotion.PromotionError, match="later run attempt"):
        _promote(tmp_path / "run", fake)


@pytest.mark.parametrize("attempt", [1, 2])
def test_unsafe_recovery_state_fails_closed(tmp_path: Path, attempt: int) -> None:
    _, _, paths = _artifacts(tmp_path)
    fake = FakeGitHub(
        _assets(paths),
        tag_exists=True,
        release_state="draft",
        main_sha="b" * 40,
        comparison_status="diverged",
    )

    with pytest.raises(promotion.PromotionError):
        dist, sums, _ = _artifacts(tmp_path / "run")
        promotion.promote(
            runner=fake,
            repository=REPOSITORY,
            version="0.1.0",
            commit_sha=COMMIT,
            dist_dir=dist,
            checksums=sums,
            notes_path=NOTES_PATH,
            run_attempt=attempt,
        )


def test_first_promotion_requires_main_tip(tmp_path: Path) -> None:
    _, _, paths = _artifacts(tmp_path)
    fake = FakeGitHub(_assets(paths), main_sha="b" * 40)

    with pytest.raises(promotion.PromotionError, match="first promotion"):
        _promote(tmp_path / "run", fake)


def test_immutable_setting_is_required_before_tag_creation(tmp_path: Path) -> None:
    _, _, paths = _artifacts(tmp_path)
    fake = FakeGitHub(_assets(paths), immutable_enabled=False)

    with pytest.raises(promotion.PromotionError, match="not enabled"):
        _promote(tmp_path / "run", fake)

    assert not any("git/refs" in " ".join(call) for call in fake.calls)


@pytest.mark.parametrize(
    ("title", "prerelease"),
    [("Veqtor v0.1.0", True), (TITLE, False)],
)
def test_published_release_requires_exact_alpha_metadata(
    tmp_path: Path,
    title: str,
    prerelease: bool,
) -> None:
    _, _, paths = _artifacts(tmp_path)
    fake = FakeGitHub(
        _assets(paths),
        tag_exists=True,
        release_state="published",
        release_title=title,
        prerelease=prerelease,
    )

    with pytest.raises(promotion.PromotionError):
        _promote(tmp_path / "run", fake)


def test_checksum_manifest_requires_flat_exact_names(tmp_path: Path) -> None:
    dist, sums, paths = _artifacts(tmp_path)
    wheel, sdist, _ = paths
    sums.write_text(
        f"{hashlib.sha256(wheel.read_bytes()).hexdigest()}  dist/{wheel.name}\n"
        f"{hashlib.sha256(sdist.read_bytes()).hexdigest()}  {sdist.name}\n"
    )
    fake = FakeGitHub(_assets(paths))

    with pytest.raises(promotion.PromotionError, match="flat names"):
        promotion.promote(
            runner=fake,
            repository=REPOSITORY,
            version="0.1.0",
            commit_sha=COMMIT,
            dist_dir=dist,
            checksums=sums,
            notes_path=NOTES_PATH,
        )
