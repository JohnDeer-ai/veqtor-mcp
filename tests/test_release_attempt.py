# SPDX-License-Identifier: Apache-2.0
"""Fail-closed checks for the current GitHub Actions release attempt."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "verify_release_attempt.py"
SPEC = importlib.util.spec_from_file_location("verify_release_attempt", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
attempt_guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(attempt_guard)

COMMIT = "a" * 40
RUN_ID = 12345
ATTEMPT = 2


def _job(name: str) -> dict:
    prefix = "" if name == attempt_guard.REQUIRED_JOB_NAMES[0] else "Run complete reusable CI / "
    return {
        "name": f"{prefix}{name}",
        "run_id": RUN_ID,
        "run_attempt": ATTEMPT,
        "head_sha": COMMIT,
        "status": "completed",
        "conclusion": "success",
    }


def _payload() -> dict:
    return {
        "total_count": len(attempt_guard.REQUIRED_JOB_NAMES),
        "jobs": [_job(name) for name in attempt_guard.REQUIRED_JOB_NAMES],
    }


def _verify(payload) -> None:
    attempt_guard.verify_release_attempt(
        payload,
        commit_sha=COMMIT,
        run_id=RUN_ID,
        attempt=ATTEMPT,
    )


def test_complete_current_attempt_is_accepted() -> None:
    _verify(_payload())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("run_id", RUN_ID + 1, "another attempt"),
        ("run_attempt", ATTEMPT + 1, "another attempt"),
        ("head_sha", "b" * 40, "another commit"),
        ("status", "in_progress", "did not succeed"),
        ("conclusion", "failure", "did not succeed"),
    ],
)
def test_wrong_job_identity_or_result_fails_closed(field: str, value, message: str) -> None:
    payload = _payload()
    payload["jobs"][3][field] = value

    with pytest.raises(attempt_guard.AttemptError, match=message):
        _verify(payload)


def test_missing_or_duplicate_required_job_fails_closed() -> None:
    payload = _payload()
    missing = payload["jobs"].pop()
    with pytest.raises(attempt_guard.AttemptError, match="not unique"):
        _verify(payload)

    payload["jobs"].append(missing)
    payload["jobs"].append(dict(missing, name=missing["name"].split(" / ")[-1]))
    with pytest.raises(attempt_guard.AttemptError, match="not unique"):
        _verify(payload)


@pytest.mark.parametrize("payload", [None, [], {}, {"jobs": None}, {"jobs": {}}])
def test_malformed_jobs_response_fails_closed(payload) -> None:
    with pytest.raises(attempt_guard.AttemptError, match="not a supported object"):
        _verify(payload)


def test_cli_reports_a_safe_failure_for_malformed_json(tmp_path: Path, capsys) -> None:
    jobs = tmp_path / "jobs.json"
    jobs.write_text("not-json", encoding="utf-8")

    result = attempt_guard.main(
        [
            "--jobs",
            str(jobs),
            "--commit",
            COMMIT,
            "--run-id",
            str(RUN_ID),
            "--attempt",
            str(ATTEMPT),
        ]
    )

    assert result == 1
    assert "release attempt verification failed" in capsys.readouterr().err


def test_cli_accepts_the_complete_attempt(tmp_path: Path, capsys) -> None:
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps(_payload()), encoding="utf-8")

    result = attempt_guard.main(
        [
            "--jobs",
            str(jobs),
            "--commit",
            COMMIT,
            "--run-id",
            str(RUN_ID),
            "--attempt",
            str(ATTEMPT),
        ]
    )

    assert result == 0
    assert "release attempt verification passed" in capsys.readouterr().out
