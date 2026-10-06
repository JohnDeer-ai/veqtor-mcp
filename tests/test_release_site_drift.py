# SPDX-License-Identifier: Apache-2.0
"""Exercise release drift proof against real Git histories and refused builds."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import check_release_site_drift as drift


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(root: Path, path: str, content: str = "changed") -> str:
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content)
    git(root, "add", "--all")
    git(root, "commit", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "Release test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    candidate = commit(tmp_path, "src/runtime.py", "original")
    git(tmp_path, "remote", "add", "origin", str(tmp_path))
    return tmp_path, candidate


def test_site_edit_and_empty_commit_preserve_candidate(repository) -> None:
    root, candidate = repository
    assert drift.check_scope(root, candidate, candidate) == []
    main = commit(root, "website/src/data/guides-source.json")
    assert drift.check_scope(root, candidate, main) == ["website/src/data/guides-source.json"]
    git(root, "commit", "--allow-empty", "-m", "empty")
    assert drift.check_scope(root, main, git(root, "rev-parse", "HEAD")) == []


@pytest.mark.parametrize("path", [
    "src/runtime.py", "pyproject.toml", "uv.lock", "RELEASING.md",
    ".github/workflows/release.yml", "scripts/build_mcpb.py",
    "website/public/logo-512.png", "website-other/page.html",
])
def test_release_relevant_change_is_refused(repository, path: str) -> None:
    root, candidate = repository
    main = commit(root, path)
    with pytest.raises(drift.SiteDriftError, match="release inputs or non-website"):
        drift.check_scope(root, candidate, main)


def test_rename_into_website_cannot_hide_deleted_runtime(repository) -> None:
    root, candidate = repository
    (root / "website").mkdir()
    git(root, "mv", "src/runtime.py", "website/article.py")
    git(root, "commit", "-m", "move")
    with pytest.raises(drift.SiteDriftError, match="src/runtime.py"):
        drift.check_scope(root, candidate, git(root, "rev-parse", "HEAD"))


def test_large_site_diff_does_not_hide_non_site_change(repository) -> None:
    root, candidate = repository
    (root / "website").mkdir()
    for number in range(305):
        (root / "website" / f"{number}.txt").write_text("site")
    main = commit(root, "zzz-runtime.py")
    with pytest.raises(drift.SiteDriftError, match="zzz-runtime.py"):
        drift.check_scope(root, candidate, main)


def test_divergent_main_is_refused(repository) -> None:
    root, base = repository
    candidate = commit(root, "website/candidate.txt")
    git(root, "checkout", "--detach", base)
    main = commit(root, "website/divergent.txt")
    with pytest.raises(drift.SiteDriftError, match="git merge-base"):
        drift.check_scope(root, candidate, main)


@pytest.mark.parametrize("sha", ["main", "--help", "a" * 39, "A" * 40])
def test_symbolic_or_invalid_revision_is_refused(repository, sha: str) -> None:
    root, candidate = repository
    with pytest.raises(drift.SiteDriftError, match="full lowercase"):
        drift.check_scope(root, candidate, sha)


def test_child_process_receives_no_release_tokens(tmp_path, monkeypatch) -> None:
    names = ("GH_TOKEN", "GITHUB_TOKEN", "RELEASE_ADMIN_TOKEN")
    for name in names:
        monkeypatch.setenv(name, "sentinel-secret")
    output = drift._run(tmp_path, [sys.executable, "-c", (
        "import os; print(any(k in os.environ for k in " + repr(names) + "))"
    )])
    assert output.strip() == "False"
    assert all(os.environ[name] == "sentinel-secret" for name in names)


@pytest.mark.parametrize("fail_script", [None, "check_reproducible_build.py", "check_reproducible_mcpb.py"])
def test_frozen_main_rebuilt_and_worktree_cleaned(repository, tmp_path, monkeypatch, fail_script) -> None:
    root, candidate = repository
    main = commit(root, "website/page.html")
    git(root, "checkout", "--detach", candidate)
    original_run = drift._run
    builds = []

    def run(root, args):
        if args[0] == sys.executable:
            source = Path(args[args.index("--source-root") + 1])
            assert git(source, "rev-parse", "HEAD") == main
            assert Path(args[args.index("--approved-dir") + 1]) == tmp_path
            builds.append(Path(args[1]).name)
            if builds[-1] == fail_script:
                raise drift.SiteDriftError("rebuilt bytes differ")
            return "verified digest"
        return original_run(root, args)

    monkeypatch.setattr(drift, "_run", run)
    if fail_script:
        with pytest.raises(drift.SiteDriftError, match="rebuilt bytes differ"):
            drift.verify(root, candidate, main, tmp_path)
    else:
        report = drift.verify(root, candidate, main, tmp_path)
        assert report["main"] == main
        assert builds == ["check_reproducible_build.py", "check_reproducible_mcpb.py"]
    assert git(root, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert git(root, "rev-parse", "HEAD") == candidate
