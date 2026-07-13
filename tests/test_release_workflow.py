# SPDX-License-Identifier: Apache-2.0
"""Ratchets for the trusted release workflow ordering and write boundary."""

import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _job(workflow: str, name: str, next_name: str | None) -> str:
    start = workflow.index(f"  {name}:\n")
    end = workflow.index(f"  {next_name}:\n", start) if next_name else len(workflow)
    return workflow[start:end]


def _jobs(workflow: str) -> dict[str, str]:
    jobs_block = workflow.split("\njobs:\n", 1)[1]
    starts = list(re.finditer(r"^  ([a-zA-Z0-9_-]+):\n", jobs_block, re.MULTILINE))
    jobs: dict[str, str] = {}
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(jobs_block)
        jobs[match.group(1)] = jobs_block[match.start() : end]
    return jobs


def test_release_guard_precedes_execution_of_requested_commit() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    guard = _job(workflow, "guard", "verify")
    verify = _job(workflow, "verify", "attempt_guard")

    assert "ref: main" in guard
    assert 'test "$GITHUB_REF" = "refs/heads/main"' in guard
    assert 'test "$GITHUB_SHA" = "$COMMIT_SHA"' in guard
    assert 'git rev-parse origin/main' in guard
    assert 'git ls-remote --exit-code --refs origin "$TAG_REF"' in guard
    assert 'test "$TAG_SHA" = "$COMMIT_SHA"' in guard
    assert 'git merge-base --is-ancestor "$COMMIT_SHA" "$MAIN_SHA"' in guard
    assert 'test "$MAIN_SHA" = "$COMMIT_SHA"' in guard
    assert "remote tag lookup failed" in guard
    assert "ref: ${{ inputs.commit_sha }}" not in guard
    assert "needs: guard" in verify
    assert "ref: ${{ inputs.commit_sha }}" in verify
    assert "secrets: inherit" not in workflow
    assert "private_dogfood_passed" not in workflow
    assert "acceptance_evidence:" in workflow
    assert "acceptance_evidence_sha256:" in workflow
    assert "veqtor_release_acceptance.v2" in workflow
    assert "scripts/check_acceptance_evidence.py" in guard
    assert 'printf \'%s\' "$ACCEPTANCE_EVIDENCE"' in guard
    assert '[[ "$ACCEPTANCE_EVIDENCE_SHA256" =~ ^[0-9a-f]{64}$ ]]' in guard
    assert 'sha256sum "$EVIDENCE_FILE"' in guard
    assert (
        'test "$ACTUAL_EVIDENCE_SHA256" = "$ACCEPTANCE_EVIDENCE_SHA256"'
        in guard
    )
    assert '--expected-sha256 "$ACCEPTANCE_EVIDENCE_SHA256"' in guard
    assert "uv sync --frozen --no-dev --python 3.12.13" in guard
    detach = 'git checkout --detach "$COMMIT_SHA"'
    head_assertion = 'test "$(git rev-parse HEAD)" = "$COMMIT_SHA"'
    assert detach in guard
    assert guard.count(head_assertion) == 2
    assert (
        guard.index('test "$VERSION" = "$PACKAGE_VERSION"')
        < guard.index(detach)
        < guard.index('printf \'%s\' "$ACCEPTANCE_EVIDENCE"')
        < guard.index("uv sync --frozen")
        < guard.rindex(head_assertion)
        < guard.index("scripts/check_acceptance_evidence.py")
    )


def test_detached_checkout_models_tagged_ancestor_recovery(tmp_path: Path) -> None:
    repository = tmp_path / "recovery"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        check=True,
        capture_output=True,
    )
    marker = repository / "marker.txt"
    marker.write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "marker.txt"], cwd=repository, check=True)
    commit_env = {
        "GIT_AUTHOR_NAME": "Veqtor test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Veqtor test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(
        ["git", "commit", "-m", "candidate"],
        cwd=repository,
        check=True,
        capture_output=True,
        env={**os.environ, **commit_env},
    )
    candidate = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    subprocess.run(["git", "tag", "v0.1.1", candidate], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "successor"],
        cwd=repository,
        check=True,
        capture_output=True,
        env={**os.environ, **commit_env},
    )
    successor = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()

    subprocess.run(
        ["git", "merge-base", "--is-ancestor", candidate, successor],
        cwd=repository,
        check=True,
    )
    assert subprocess.check_output(
        ["git", "rev-parse", f"{candidate}^{{tree}}"],
        cwd=repository,
        text=True,
    ) == subprocess.check_output(
        ["git", "rev-parse", f"{successor}^{{tree}}"],
        cwd=repository,
        text=True,
    )
    subprocess.run(
        ["git", "checkout", "--detach", candidate],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip() == candidate


def test_release_promotion_is_exact_sha_retry_safe_and_write_scoped() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    attempt_guard = _job(workflow, "attempt_guard", "publish")
    publish = _job(workflow, "publish", None)

    assert workflow.count("contents: write") == 1
    assert "environment: release" in publish
    assert "group: release-${{ inputs.version }}" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "ref: ${{ inputs.commit_sha }}" in publish
    assert "persist-credentials: false" in publish
    assert "python3 .github/scripts/promote_release.py" in publish
    assert "RELEASE_ADMIN_READ_TOKEN" in publish
    assert "python3 .github/scripts/verify_published_release.py" in publish
    assert "needs: [guard, verify, attempt_guard]" in publish
    assert "needs.attempt_guard.outputs.verified_attempt" in publish
    assert 'test "$VERIFIED_ATTEMPT" = "$GITHUB_RUN_ATTEMPT"' in publish
    assert "sha256sum dist/* >" not in publish
    assert "--target" not in publish
    promotion = (
        ROOT / ".github" / "scripts" / "promote_release.py"
    ).read_text()
    assert 'GITHUB_API_VERSION = "2026-03-10"' in promotion
    assert 'GITHUB_API_HEADER = f"X-GitHub-Api-Version:' in promotion
    assert "--paginate" in promotion
    assert "--slurp" in promotion
    assert "multiple releases exist for the approved tag" in promotion
    assert "releases/tags/" not in promotion
    assert "release upload" not in promotion
    assert "release edit" not in promotion

    assert "needs: [guard, verify]" in attempt_guard
    assert "actions: read" in attempt_guard
    assert "contents: read" in attempt_guard
    assert "verify_release_attempt.py" in attempt_guard
    assert "attempts/$GITHUB_RUN_ATTEMPT/jobs?per_page=100" in attempt_guard
    assert "X-GitHub-Api-Version: 2026-03-10" in attempt_guard
    assert "--run-id \"$GITHUB_RUN_ID\"" in attempt_guard
    assert "--attempt \"$GITHUB_RUN_ATTEMPT\"" in attempt_guard


def test_read_only_artifact_job_owns_and_smokes_the_flat_manifest() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    artifact = _job(workflow, "artifact", "reproduce")

    assert "sha256sum *.whl *.tar.gz > SHA256SUMS.txt" in artifact
    assert "sha256sum -c SHA256SUMS.txt" in artifact
    assert "--source-root . --commit \"$VERIFY_REF\"" in artifact
    assert "name: ${{ env.DIST_ARTIFACT_NAME }}" in artifact
    assert "path: dist/*" in artifact
    assert "overwrite: true" not in artifact


def test_independent_rebuild_is_a_required_secretless_ci_job() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    reproduce = _job(workflow, "reproduce", "gitleaks")

    assert "needs: artifact" in reproduce
    assert "runs-on: ubuntu-24.04" in reproduce
    assert 'python-version: "3.12.13"' in reproduce
    assert 'version: "0.11.28"' in reproduce
    assert "enable-cache: false" in reproduce
    assert "persist-credentials: false" in reproduce
    assert "check_reproducible_build.py" in reproduce
    assert "--approved-dir approved-dist" in reproduce
    assert "name: ${{ env.DIST_ARTIFACT_NAME }}" in reproduce
    assert "secrets:" not in reproduce


def test_release_artifacts_are_scoped_to_the_current_run_attempt() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    release = (ROOT / ".github/workflows/release.yml").read_text()
    publish = _job(release, "publish", None)
    identity = "veqtor-mcp-dist-${{ github.run_id }}-${{ github.run_attempt }}"

    assert f"DIST_ARTIFACT_NAME: {identity}" in ci
    assert f"DIST_ARTIFACT_NAME: {identity}" in release
    assert "name: ${{ env.DIST_ARTIFACT_NAME }}" in publish
    assert "name: veqtor-mcp-dist\n" not in ci
    assert "name: veqtor-mcp-dist\n" not in release
    assert "overwrite: true" not in ci
    assert "overwrite: true" not in release


def test_release_build_inputs_are_pinned() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()

    assert 'SOURCE_DATE_EPOCH: "1580601600"' in workflow
    assert 'requires = ["hatchling==1.31.0"]' in pyproject
    for setup in workflow.split("astral-sh/setup-uv@")[1:]:
        assert 'version: "0.11.28"' in setup.split("- name:", 1)[0]


def test_documented_rebuild_is_bound_to_dist() -> None:
    releasing = (ROOT / "RELEASING.md").read_text()

    assert "--source-root . --approved-dir dist" in releasing


def test_i3_and_i7_contracts_are_finite_and_transport_safe() -> None:
    releasing = (ROOT / "RELEASING.md").read_text()

    assert "ZIP local headers, central headers and EOCD" in releasing
    assert "regular-file type, empty link/owner/group names" in releasing
    assert "the original exception never crosses the MCP transport" in releasing
    assert "rolls back the complete batch" in releasing
    assert "check_acceptance_evidence.py" in releasing


def test_recovery_contract_distinguishes_new_dispatch_from_advanced_main() -> None:
    releasing = (ROOT / "RELEASING.md").read_text()

    assert re.search(r"separately\s+approved dispatch", releasing)
    assert "caller SHA, candidate SHA and `main`" in releasing
    assert "After `main` advances, only a later attempt" in releasing
    assert "full required pre-publication gate set" in releasing
    assert re.search(r"selective rerun\s+of the root `guard`", releasing)
    assert "An incomplete rerun" in releasing
    assert "Re-run all jobs" in releasing
    assert "run ID and attempt number" in releasing
    assert re.search(r"continue\s+by that id", releasing)
    assert "every authenticated release-list page" in releasing
    assert re.search(r"Duplicate exact-tag\s+drafts fail closed", releasing)
    assert "selective job reruns fail closed" not in releasing


def test_product_acceptance_requires_two_export_live_rehearsal() -> None:
    releasing = (ROOT / "RELEASING.md").read_text()
    smoke = (ROOT / "scripts" / "installed_wheel_smoke.py").read_text()

    assert "installed-wheel smoke performs two live MCP exports" in releasing
    assert (
        "fresh-copy Claude Desktop rehearsal is release-blocking" in releasing
    )
    assert "first event exists locally but is omitted" in releasing
    assert "first_access_id" in smoke
    assert 'exported_again["access_count"] == 1' in smoke
    assert 'record["record_type"] != "access_event.v1"' in smoke
    assert "`installed_two_export`" in releasing
    assert "`desktop_rehearsal`" in releasing


def test_guard_is_the_only_root_of_the_release_job_graph() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    jobs = _jobs(workflow)
    roots = [
        name
        for name, body in jobs.items()
        if re.search(r"^    needs:", body, re.MULTILINE) is None
    ]

    assert set(jobs) == {"guard", "verify", "attempt_guard", "publish"}
    assert roots == ["guard"]


def test_all_release_actions_are_pinned_to_full_commit_shas() -> None:
    for relative in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
        workflow = (ROOT / relative).read_text()
        for line in workflow.splitlines():
            if "uses:" not in line or "./.github/" in line:
                continue
            target = line.split("uses:", 1)[1].strip().split()[0]
            revision = target.rsplit("@", 1)[1]
            assert len(revision) == 40
            assert all(char in "0123456789abcdef" for char in revision)


def test_release_documents_use_only_the_canonical_project_slug() -> None:
    documents = [
        ROOT / "README.md",
        ROOT / ".github" / "release-notes" / "v0.1.1.md",
    ]
    combined = "\n".join(path.read_text() for path in documents)

    assert "github.com/ilyashilov/veqtor-mcp" not in combined.casefold()
    assert "github.com/JohnDeer-ai/veqtor-mcp" in combined
