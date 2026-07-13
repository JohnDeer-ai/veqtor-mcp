# SPDX-License-Identifier: Apache-2.0
"""Verify that every release gate succeeded in the current workflow attempt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REQUIRED_JOB_NAMES = (
    "Verify promotion identity and ancestry",
    "Test ubuntu-latest / Python 3.12",
    "Test ubuntu-latest / Python 3.13",
    "Test ubuntu-latest / Python 3.14",
    "Test macos-latest / Python 3.12",
    "Test macos-latest / Python 3.13",
    "Test macos-latest / Python 3.14",
    "Test declared minimum direct dependencies",
    "Build and smoke release artifacts",
    "Independently reproduce release artifacts",
    "Scan repository history for secrets",
)


class AttemptError(ValueError):
    """The current workflow attempt does not prove every required gate."""


def _job_matches(job_name: str, required_name: str) -> bool:
    return job_name == required_name or job_name.endswith(f" / {required_name}")


def verify_release_attempt(
    payload: Any,
    *,
    commit_sha: str,
    run_id: int,
    attempt: int,
) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise AttemptError("jobs response is not a supported object")
    if len(commit_sha) != 40 or any(char not in "0123456789abcdef" for char in commit_sha):
        raise AttemptError("candidate commit is not a lowercase full SHA")
    if run_id < 1 or attempt < 1:
        raise AttemptError("run identity is invalid")

    jobs = payload["jobs"]
    for required_name in REQUIRED_JOB_NAMES:
        matches = [
            job
            for job in jobs
            if isinstance(job, dict)
            and isinstance(job.get("name"), str)
            and _job_matches(job["name"], required_name)
        ]
        if len(matches) != 1:
            raise AttemptError(f"required job identity is not unique: {required_name}")

        job = matches[0]
        if job.get("run_id") != run_id or job.get("run_attempt") != attempt:
            raise AttemptError(f"required job belongs to another attempt: {required_name}")
        if job.get("head_sha") != commit_sha:
            raise AttemptError(f"required job verified another commit: {required_name}")
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            raise AttemptError(f"required job did not succeed: {required_name}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--attempt", type=int, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        payload = json.loads(args.jobs.read_text(encoding="utf-8"))
        verify_release_attempt(
            payload,
            commit_sha=args.commit,
            run_id=args.run_id,
            attempt=args.attempt,
        )
    except (AttemptError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"release attempt verification failed: {exc}", file=sys.stderr)
        return 1
    print("release attempt verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
