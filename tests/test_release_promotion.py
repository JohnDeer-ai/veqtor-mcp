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
VERSION = promotion.CONTRACT_VERSION
TAG = f"v{VERSION}"
NOTES_PATH = ROOT / ".github" / "release-notes" / f"v{VERSION}.md"
NOTES = NOTES_PATH.read_text(encoding="utf-8")
TITLE = f"Veqtor v{VERSION} Alpha"


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
    wheel = dist / f"veqtor_mcp-{VERSION}-py3-none-any.whl"
    sdist = dist / f"veqtor_mcp-{VERSION}.tar.gz"
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
        duplicate_releases: int = 0,
        release_on_second_page: bool = False,
        create_ok: bool = True,
    ) -> None:
        self.expected_assets = {asset["name"]: dict(asset) for asset in assets}
        self.tag_exists = tag_exists
        self.tag_sha = tag_sha
        self.tag_kind = tag_kind
        self.upload_ok = upload_ok
        self.main_sha = main_sha
        self.comparison_status = comparison_status
        self.immutable_enabled = immutable_enabled
        self.release_title = release_title
        self.prerelease = prerelease
        self.release_on_second_page = release_on_second_page
        self.create_ok = create_ok
        self.calls: list[list[str]] = []
        self.upload_calls: list[tuple[str, Path]] = []
        self.releases: list[dict] = []
        self.next_release_id = 100
        self.next_asset_id = 1000
        if release_state != "absent":
            self.releases.append(
                self._new_release(release_state, assets=[*assets])
            )
        for _ in range(duplicate_releases):
            self.releases.append(
                self._new_release(release_state, assets=[*assets])
            )

    @property
    def release_state(self) -> str:
        if not self.releases:
            return "absent"
        return "draft" if self.releases[0]["draft"] else "published"

    def _stored_assets(self, assets: list[dict]) -> list[dict]:
        stored = []
        for asset in assets:
            item = dict(asset)
            item.setdefault("id", self.next_asset_id)
            self.next_asset_id += 1
            stored.append(item)
        return stored

    def _new_release(self, state: str, *, assets: list[dict]) -> dict:
        release = {
            "id": self.next_release_id,
            "upload_url": (
                f"https://uploads.github.com/repos/{REPOSITORY}/releases/"
                f"{self.next_release_id}/assets{{?name,label}}"
            ),
            "tag_name": TAG,
            "name": self.release_title,
            "body": NOTES,
            "prerelease": self.prerelease,
            "draft": state == "draft",
            "immutable": state == "published",
            "assets": self._stored_assets(assets),
        }
        self.next_release_id += 1
        return release

    def _release_by_id(self, release_id: int) -> dict:
        return next(
            release for release in self.releases if release["id"] == release_id
        )

    def upload_asset(self, upload_url: str, path: Path) -> dict:
        self.upload_calls.append((upload_url, path))
        if not self.upload_ok:
            raise promotion.PromotionError("draft asset upload failed")
        release_id = int(upload_url.split("/releases/", 1)[1].split("/", 1)[0])
        asset = dict(self.expected_assets[path.name])
        asset["id"] = self.next_asset_id
        self.next_asset_id += 1
        self._release_by_id(release_id)["assets"].append(asset)
        return asset

    @staticmethod
    def _endpoint(args: list[str]) -> str | None:
        return next((item for item in args if item.startswith("repos/")), None)

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
        endpoint = self._endpoint(args)
        if endpoint is not None and "releases?per_page=100" in endpoint:
            pages = (
                [[], self.releases]
                if self.release_on_second_page
                else [self.releases]
            )
            return _completed(args, payload=pages)
        if (
            endpoint == f"repos/{REPOSITORY}/releases"
            and "--method POST" in joined
        ):
            if not self.create_ok:
                return _completed(args, 1)
            release = self._new_release("draft", assets=[])
            self.releases.append(release)
            return _completed(args, payload=release)
        if endpoint is not None and "/releases/assets/" in endpoint:
            asset_id = int(endpoint.rsplit("/", 1)[1])
            for release in self.releases:
                release["assets"] = [
                    asset for asset in release["assets"] if asset.get("id") != asset_id
                ]
            return _completed(args)
        if endpoint is not None and "/releases/" in endpoint:
            release_id = int(endpoint.rsplit("/", 1)[1])
            release = self._release_by_id(release_id)
            if "--method PATCH" in joined:
                release["draft"] = False
                release["immutable"] = True
            return _completed(args, payload=release)
        raise AssertionError(f"unexpected command: {args}")


def _promote(tmp_path: Path, fake: FakeGitHub) -> None:
    dist, sums, _ = _artifacts(tmp_path)
    promotion.promote(
        runner=fake,
        asset_uploader=fake.upload_asset,
        repository=REPOSITORY,
        version=VERSION,
        commit_sha=COMMIT,
        dist_dir=dist,
        checksums=sums,
        notes_path=NOTES_PATH,
    )


def _reserve(fake: FakeGitHub, *, run_attempt: int = 1) -> None:
    promotion.reserve_tag(
        runner=fake,
        repository=REPOSITORY,
        version=VERSION,
        commit_sha=COMMIT,
        run_attempt=run_attempt,
    )


def test_reserve_tag_creates_only_the_exact_lightweight_tag() -> None:
    fake = FakeGitHub([])

    _reserve(fake)

    assert fake.tag_exists is True
    assert fake.tag_sha == COMMIT
    assert fake.tag_kind == "commit"
    assert fake.release_state == "absent"
    assert not fake.upload_calls
    assert any(
        "--method POST" in " ".join(call)
        and f"repos/{REPOSITORY}/git/refs" in call
        for call in fake.calls
    )
    assert not any(
        (endpoint := fake._endpoint(call)) is not None
        and (
            endpoint == f"repos/{REPOSITORY}/releases"
            or endpoint.startswith(f"repos/{REPOSITORY}/releases/")
        )
        for call in fake.calls
    )


def test_reserved_tag_allows_same_attempt_publish_after_main_advances(
    tmp_path: Path,
) -> None:
    dist, sums, paths = _artifacts(tmp_path)
    fake = FakeGitHub(_assets(paths))
    _reserve(fake, run_attempt=1)
    assert fake.release_state == "absent"
    fake.main_sha = "b" * 40

    promotion.promote(
        runner=fake,
        asset_uploader=fake.upload_asset,
        repository=REPOSITORY,
        version=VERSION,
        commit_sha=COMMIT,
        dist_dir=dist,
        checksums=sums,
        notes_path=NOTES_PATH,
        run_attempt=1,
        allow_current_attempt_ancestor=True,
    )

    assert fake.release_state == "published"
    assert any(f"{COMMIT}...main" in " ".join(call) for call in fake.calls)


def test_reserve_phase_entrypoint_does_not_enter_release_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def capture_reservation(**kwargs) -> None:
        captured.update(kwargs)

    def reject_publication(**_kwargs) -> None:
        pytest.fail("reserve_tag phase entered release publication")

    monkeypatch.setattr(promotion, "reserve_tag", capture_reservation)
    monkeypatch.setattr(promotion, "promote", reject_publication)
    monkeypatch.setenv("RELEASE_PHASE", "reserve_tag")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "3")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("VERSION", VERSION)
    monkeypatch.setenv("COMMIT_SHA", COMMIT)

    assert promotion.main() == 0
    assert captured == {
        "admin_runner": promotion._admin_runner,
        "repository": REPOSITORY,
        "version": VERSION,
        "commit_sha": COMMIT,
        "run_attempt": 3,
    }


@pytest.mark.parametrize(
    ("reserved_attempt", "expected_allowance"),
    [(None, False), ("2", False), ("3", True)],
)
def test_publish_phase_allows_ancestor_recovery_only_for_its_reservation(
    monkeypatch: pytest.MonkeyPatch,
    reserved_attempt: str | None,
    expected_allowance: bool,
) -> None:
    captured = {}

    def capture_publication(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(promotion, "promote", capture_publication)
    monkeypatch.setenv("RELEASE_PHASE", "publish")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "3")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("VERSION", VERSION)
    monkeypatch.setenv("COMMIT_SHA", COMMIT)
    if reserved_attempt is None:
        monkeypatch.delenv("TAG_RESERVED_ATTEMPT", raising=False)
    else:
        monkeypatch.setenv("TAG_RESERVED_ATTEMPT", reserved_attempt)

    assert promotion.main() == 0
    assert captured["run_attempt"] == 3
    assert captured["allow_current_attempt_ancestor"] is expected_allowance


def test_new_release_is_uploaded_as_draft_verified_and_published(tmp_path: Path) -> None:
    dist, sums, paths = _artifacts(tmp_path)
    fake = FakeGitHub(_assets(paths))
    promotion.promote(
        runner=fake,
        asset_uploader=fake.upload_asset,
        repository=REPOSITORY,
        version=VERSION,
        commit_sha=COMMIT,
        dist_dir=dist,
        checksums=sums,
        notes_path=NOTES_PATH,
    )

    assert fake.release_state == "published"
    create = next(
        call
        for call in fake.calls
        if "--method POST" in " ".join(call)
        and f"repos/{REPOSITORY}/releases" in call
    )
    assert "prerelease=true" in create
    assert f"name={TITLE}" in create
    assert f"body=@{NOTES_PATH}" in create
    uploads = fake.upload_calls
    assert len(uploads) == 3
    assert all("/releases/100/assets?name=" in url for url, _ in uploads)
    assert not any("/releases/tags/" in " ".join(call) for call in fake.calls)


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
        asset_uploader=fake.upload_asset,
        repository=REPOSITORY,
        version=VERSION,
        commit_sha=COMMIT,
        dist_dir=dist,
        checksums=sums,
        notes_path=NOTES_PATH,
        run_attempt=1,
    )

    assert fake.release_state == "published"
    assert any("git/ref/tags/" in " ".join(call) for call in fake.calls)
    assert any("--paginate --slurp" in " ".join(call) for call in fake.calls)


def test_draft_recovery_reads_every_release_page(tmp_path: Path) -> None:
    dist, sums, paths = _artifacts(tmp_path)
    fake = FakeGitHub(
        _assets(paths),
        tag_exists=True,
        release_state="draft",
        release_on_second_page=True,
    )

    promotion.promote(
        runner=fake,
        asset_uploader=fake.upload_asset,
        repository=REPOSITORY,
        version=VERSION,
        commit_sha=COMMIT,
        dist_dir=dist,
        checksums=sums,
        notes_path=NOTES_PATH,
    )

    assert fake.release_state == "published"
    assert not any(
        "--method POST" in " ".join(call)
        and f"repos/{REPOSITORY}/releases" in call
        for call in fake.calls
    )


def test_duplicate_exact_tag_releases_fail_closed_before_mutation(
    tmp_path: Path,
) -> None:
    _, _, paths = _artifacts(tmp_path)
    fake = FakeGitHub(
        _assets(paths),
        tag_exists=True,
        release_state="draft",
        duplicate_releases=1,
    )

    with pytest.raises(promotion.PromotionError, match="multiple releases"):
        _promote(tmp_path / "run", fake)

    assert not fake.upload_calls
    assert not any("--method PATCH" in " ".join(call) for call in fake.calls)


def test_partial_draft_asset_is_replaced_by_asset_and_release_id(
    tmp_path: Path,
) -> None:
    dist, sums, paths = _artifacts(tmp_path)
    fake = FakeGitHub(
        _assets(paths),
        tag_exists=True,
        release_state="draft",
    )
    fake.releases[0]["assets"][0]["digest"] = "sha256:" + "0" * 64

    promotion.promote(
        runner=fake,
        asset_uploader=fake.upload_asset,
        repository=REPOSITORY,
        version=VERSION,
        commit_sha=COMMIT,
        dist_dir=dist,
        checksums=sums,
        notes_path=NOTES_PATH,
    )

    assert fake.release_state == "published"
    assert any("/releases/assets/" in " ".join(call) for call in fake.calls)
    assert fake.upload_calls


def test_every_authenticated_api_call_pins_the_api_version(tmp_path: Path) -> None:
    _, _, paths = _artifacts(tmp_path)
    fake = FakeGitHub(_assets(paths))

    _promote(tmp_path / "run", fake)

    api_calls = [call for call in fake.calls if call[:2] == ["gh", "api"]]
    assert api_calls
    assert all(promotion.GITHUB_API_HEADER in call for call in api_calls)


def test_asset_upload_uses_validated_upload_url_and_header_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "asset.whl"
    path.write_bytes(b"wheel")
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read() -> bytes:
            return json.dumps({"name": path.name}).encode()

    def open_url(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setenv("GH_TOKEN", "secret-token")
    monkeypatch.setattr(promotion, "urlopen", open_url)

    result = promotion._default_asset_uploader(
        "https://uploads.github.com/repos/example/veqtor/releases/100/"
        "assets?name=asset.whl",
        path,
    )

    request = captured["request"]
    assert result == {"name": path.name}
    assert captured["timeout"] == 60
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert request.get_header("X-github-api-version") == "2026-03-10"
    assert request.data == b"wheel"


def test_asset_upload_rejects_untrusted_release_upload_url(tmp_path: Path) -> None:
    path = tmp_path / "asset.whl"
    path.write_bytes(b"wheel")
    payload = {
        "id": 100,
        "upload_url": "https://example.invalid/assets{?name,label}",
    }

    with pytest.raises(promotion.PromotionError, match="unexpected upload_url"):
        promotion._draft_asset_upload_url(
            payload,
            REPOSITORY,
            100,
            path,
        )


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
    assert not any("--method PATCH" in " ".join(call) for call in fake.calls)


def test_existing_published_release_is_verified_without_mutation(tmp_path: Path) -> None:
    dist, sums, paths = _artifacts(tmp_path)
    fake = FakeGitHub(_assets(paths), release_state="published")
    promotion.promote(
        runner=fake,
        asset_uploader=fake.upload_asset,
        repository=REPOSITORY,
        version=VERSION,
        commit_sha=COMMIT,
        dist_dir=dist,
        checksums=sums,
        notes_path=NOTES_PATH,
    )

    assert not fake.upload_calls
    assert not any("--method PATCH" in " ".join(call) for call in fake.calls)


def test_existing_published_release_with_wrong_asset_fails_closed(tmp_path: Path) -> None:
    _, _, paths = _artifacts(tmp_path)
    assets = _assets(paths)
    assets[0]["digest"] = "sha256:" + "0" * 64
    fake = FakeGitHub(assets, release_state="published")

    with pytest.raises(promotion.PromotionError, match="do not match"):
        _promote(tmp_path / "run", fake)

    assert not fake.upload_calls


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
        asset_uploader=fake.upload_asset,
        repository=REPOSITORY,
        version=VERSION,
        commit_sha=COMMIT,
        dist_dir=dist,
        checksums=sums,
        notes_path=NOTES_PATH,
        run_attempt=2,
    )

    assert fake.release_state == "published"
    assert any(f"{COMMIT}...main" in " ".join(call) for call in fake.calls)


def test_same_attempt_ancestor_recovery_is_refused_by_default(tmp_path: Path) -> None:
    _, _, paths = _artifacts(tmp_path)
    fake = FakeGitHub(_assets(paths))
    _reserve(fake, run_attempt=1)
    fake.main_sha = "b" * 40

    with pytest.raises(promotion.PromotionError, match="later run attempt"):
        _promote(tmp_path / "run", fake)

    assert fake.release_state == "absent"
    assert not fake.upload_calls


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
            asset_uploader=fake.upload_asset,
            repository=REPOSITORY,
            version=VERSION,
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
    [(f"Veqtor v{VERSION}", True), (TITLE, False)],
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
            asset_uploader=fake.upload_asset,
            repository=REPOSITORY,
            version=VERSION,
            commit_sha=COMMIT,
            dist_dir=dist,
            checksums=sums,
            notes_path=NOTES_PATH,
        )
