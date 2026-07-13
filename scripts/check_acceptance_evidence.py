# SPDX-License-Identifier: Apache-2.0
"""Validate a path-free exact-SHA I8 acceptance evidence packet."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from release_contract import FIVE_EDIT_OUTPUT_SHA256


SCHEMA_VERSION = "veqtor_release_acceptance.v1"
MAX_EVIDENCE_BYTES = 64 * 1024
HEX = frozenset("0123456789abcdef")


class EvidenceError(ValueError):
    """The evidence packet is incomplete, unsafe, or belongs to another tree."""


def _exact_keys(value: Any, expected: set[str], location: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise EvidenceError(f"{location} fields differ from the acceptance schema")
    return value


def _hex_digest(value: Any, length: int, location: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(char not in HEX for char in value)
    ):
        raise EvidenceError(f"{location} is not a lowercase hex digest")
    return value


def _count(value: Any, minimum: int, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvidenceError(f"{location} is below the acceptance minimum")
    return value


def _passed(value: Any, location: str) -> None:
    if value != "passed":
        raise EvidenceError(f"{location} did not pass")


def _validate_private_run(value: Any, location: str) -> None:
    run = _exact_keys(
        value,
        {
            "passed",
            "skipped",
            "corpus_before_sha256",
            "corpus_after_sha256",
        },
        location,
    )
    _count(run["passed"], 4, f"{location}.passed")
    _count(run["skipped"], 0, f"{location}.skipped")
    before = _hex_digest(
        run["corpus_before_sha256"], 64, f"{location}.corpus_before_sha256"
    )
    after = _hex_digest(
        run["corpus_after_sha256"], 64, f"{location}.corpus_after_sha256"
    )
    if before != after:
        raise EvidenceError(f"{location} modified the source corpus")


def validate_evidence(
    value: Any,
    *,
    candidate_sha: str,
    candidate_tree: str,
    producer_build: str,
) -> None:
    packet = _exact_keys(
        value,
        {
            "schema_version",
            "candidate_sha",
            "candidate_tree",
            "producer_build",
            "public_matrix",
            "private_dogfood",
            "payment_preflight",
            "five_edit_batch",
        },
        "packet",
    )
    if packet["schema_version"] != SCHEMA_VERSION:
        raise EvidenceError("packet schema version is unsupported")
    if packet["candidate_sha"] != candidate_sha:
        raise EvidenceError("packet candidate does not equal checked-out HEAD")
    if packet["candidate_tree"] != candidate_tree:
        raise EvidenceError("packet tree does not equal checked-out HEAD tree")
    if packet["producer_build"] != producer_build:
        raise EvidenceError("packet producer build does not equal the source tree")

    matrix = _exact_keys(
        packet["public_matrix"],
        {"python_3_12", "python_3_13", "python_3_14", "minimum_direct"},
        "public_matrix",
    )
    for lane, status in matrix.items():
        _passed(status, f"public_matrix.{lane}")

    private = _exact_keys(
        packet["private_dogfood"], {"used", "clean"}, "private_dogfood"
    )
    _validate_private_run(private["used"], "private_dogfood.used")
    _validate_private_run(private["clean"], "private_dogfood.clean")

    payment = _exact_keys(
        packet["payment_preflight"],
        {"batch_applicable", "refusal_code", "match_count"},
        "payment_preflight",
    )
    if (
        payment["batch_applicable"] is not False
        or payment["refusal_code"] != "counter_position_unsupported"
        or isinstance(payment["match_count"], bool)
        or payment["match_count"] != 1
    ):
        raise EvidenceError("payment preflight does not prove the expected refusal")

    batch = _exact_keys(
        packet["five_edit_batch"],
        {
            "preflight_applicable",
            "apply_status",
            "applied_count",
            "round_trip_status",
            "collateral_change_count",
            "output_sha256",
        },
        "five_edit_batch",
    )
    if batch["preflight_applicable"] is not True:
        raise EvidenceError("five-edit preflight was not applicable")
    if batch["apply_status"] != "ok":
        raise EvidenceError("five-edit apply did not succeed")
    if batch["round_trip_status"] != "passed":
        raise EvidenceError("five-edit round trip did not pass")
    collateral_count = _count(
        batch["collateral_change_count"], 0, "five_edit_batch.collateral_change_count"
    )
    if collateral_count != 0:
        raise EvidenceError("five-edit apply reported collateral changes")
    applied_count = _count(batch["applied_count"], 0, "five_edit_batch.applied_count")
    if applied_count != 5:
        raise EvidenceError("five-edit apply count differs from five")
    output_sha256 = _hex_digest(
        batch["output_sha256"], 64, "five_edit_batch.output_sha256"
    )
    if output_sha256 != FIVE_EDIT_OUTPUT_SHA256:
        raise EvidenceError(
            "five-edit output fingerprint differs from the release contract"
        )


def _git(source_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source_root), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise EvidenceError("cannot resolve the checked-out git candidate")
    return completed.stdout.strip()


def _load_packet(path: Path) -> Any:
    with path.open("rb") as handle:
        payload = handle.read(MAX_EVIDENCE_BYTES + 1)
    if len(payload) > MAX_EVIDENCE_BYTES:
        raise EvidenceError("evidence packet exceeds the size limit")

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceError("evidence packet contains duplicate keys")
            result[key] = value
        return result

    try:
        return json.loads(payload, object_pairs_hook=reject_duplicate_keys)
    except EvidenceError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("evidence packet is not valid UTF-8 JSON") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate an external sanitized Veqtor I8 evidence packet"
    )
    parser.add_argument("packet", type=Path)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    source_root = args.source_root.resolve()
    try:
        if _git(source_root, "status", "--porcelain", "--untracked-files=all"):
            raise EvidenceError("source worktree is not clean")
        candidate_sha = _git(source_root, "rev-parse", "HEAD")
        candidate_tree = _git(source_root, "rev-parse", "HEAD^{tree}")
        sys.path.insert(0, str(source_root / "src"))
        from veqtor_mcp.records import SOURCE_SNAPSHOT_IDENTITY

        validate_evidence(
            _load_packet(args.packet),
            candidate_sha=candidate_sha,
            candidate_tree=candidate_tree,
            producer_build=SOURCE_SNAPSHOT_IDENTITY,
        )
    except (EvidenceError, OSError) as exc:
        print(f"acceptance evidence failed: {exc}", file=sys.stderr)
        return 1
    print("acceptance evidence passed")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
