# SPDX-License-Identifier: Apache-2.0
"""Validate a path-free exact-SHA I8 acceptance evidence packet."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from release_contract import FIVE_EDIT_OUTPUT_SHA256, MCPB_REQUIRED_TOOLS, VERSION


SCHEMA_VERSION = "veqtor_release_acceptance.v4"
MAX_EVIDENCE_BYTES = 64 * 1024
MAX_PACKET_INTEGER_DIGITS = 128
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
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceError(f"{location} is not an integer count")
    if value < minimum:
        raise EvidenceError(f"{location} is below the acceptance minimum")
    return value


def _exact_count(value: Any, expected: int, location: str) -> int:
    count = _count(value, 0, location)
    if count != expected:
        raise EvidenceError(f"{location} does not equal {expected}")
    return count


def _passed(value: Any, location: str) -> None:
    if value != "passed":
        raise EvidenceError(f"{location} did not pass")


def _label(value: Any, location: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise EvidenceError(f"{location} is not a bounded label")
    return value


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
    if not isinstance(value, dict):
        raise EvidenceError("packet fields differ from the acceptance schema")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError("packet schema version is unsupported")
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
            "installed_two_export",
            "desktop_rehearsal",
            "desktop_extension",
        },
        "packet",
    )
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
    if payment["batch_applicable"] is not False or (
        payment["refusal_code"] != "counter_position_unsupported"
    ):
        raise EvidenceError("payment preflight does not prove the expected refusal")
    _exact_count(payment["match_count"], 1, "payment_preflight.match_count")

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

    installed = _exact_keys(
        packet["installed_two_export"],
        {
            "first_access_count",
            "second_access_count",
            "first_event_absent_from_windows",
            "current_event_outside_own_snapshot",
            "runtime_producer_build",
            "runtime_version",
        },
        "installed_two_export",
    )
    if (
        installed["first_event_absent_from_windows"] is not True
        or installed["current_event_outside_own_snapshot"] is not True
    ):
        raise EvidenceError("installed two-export acceptance did not pass")
    _exact_count(
        installed["first_access_count"],
        0,
        "installed_two_export.first_access_count",
    )
    _exact_count(
        installed["second_access_count"],
        1,
        "installed_two_export.second_access_count",
    )
    if installed["runtime_producer_build"] != producer_build:
        raise EvidenceError("installed runtime build does not equal the source tree")
    if installed["runtime_version"] != VERSION:
        raise EvidenceError("installed runtime version does not equal the candidate")

    desktop = _exact_keys(
        packet["desktop_rehearsal"],
        {
            "verdict",
            "client",
            "fresh_copy",
            "event_omitted_from_records",
            "current_event_not_in_access_count",
            "raw_vs_compact_explained",
            "runtime_producer_build",
            "runtime_version",
            "transcript_sha256",
            "raw_journal_sha256",
        },
        "desktop_rehearsal",
    )
    if (
        desktop["verdict"] != "passed"
        or desktop["client"] != "claude_desktop_fresh_copy"
        or desktop["fresh_copy"] is not True
        or desktop["event_omitted_from_records"] is not True
        or desktop["current_event_not_in_access_count"] is not True
        or desktop["raw_vs_compact_explained"] is not True
    ):
        raise EvidenceError("Claude Desktop rehearsal did not pass")
    if desktop["runtime_producer_build"] != producer_build:
        raise EvidenceError("Desktop runtime build does not equal the source tree")
    if desktop["runtime_version"] != VERSION:
        raise EvidenceError("Desktop runtime version does not equal the candidate")
    _hex_digest(
        desktop["transcript_sha256"], 64, "desktop_rehearsal.transcript_sha256"
    )
    _hex_digest(
        desktop["raw_journal_sha256"],
        64,
        "desktop_rehearsal.raw_journal_sha256",
    )

    extension = _exact_keys(
        packet["desktop_extension"],
        {
            "artifact_sha256",
            "installation_channel",
            "platform",
            "client",
            "client_version",
            "platform_version",
            "manual_uv_install_absent",
            "manual_python_install_absent",
            "host_managed_uv_runtime_confirmed",
            "tracked_change_author_confirmed",
            "extension_enabled_confirmed",
            "server_connected_confirmed",
            "visible_tools",
            "called_tools",
            "runtime_producer_build",
            "runtime_version",
            "demo_round_count",
            "bundled_demo_prompt_completed",
            "post_apply_list_rounds_status",
            "post_apply_round_count",
            "source_sha256_unchanged",
            "output_sha256_matches_list_rounds",
            "output_sha256_matches_reextract",
            "session_transcript_sha256",
            "demo_journal_sha256",
            "lifecycle_scenario",
            "fresh_install_status",
            "upgrade_status",
            "rollback_status",
            "reinstall_same_artifact_status",
            "uninstall_status",
            "post_uninstall_tools_absent",
        },
        "desktop_extension",
    )
    _hex_digest(
        extension["artifact_sha256"],
        64,
        "desktop_extension.artifact_sha256",
    )
    if (
        extension["installation_channel"] != "direct_download_mcpb"
        or extension["platform"] != "darwin"
        or extension["client"] != "claude_desktop_fresh_copy"
        or extension["manual_uv_install_absent"] is not True
        or extension["manual_python_install_absent"] is not True
        or extension["host_managed_uv_runtime_confirmed"] is not True
        or extension["tracked_change_author_confirmed"] is not True
        or extension["extension_enabled_confirmed"] is not True
        or extension["server_connected_confirmed"] is not True
        or extension["bundled_demo_prompt_completed"] is not True
        or extension["lifecycle_scenario"] != "first_public_mcpb"
        or extension["upgrade_status"]
        != "not_applicable_first_public_mcpb"
        or extension["rollback_status"]
        != "not_applicable_no_prior_public_mcpb"
        or extension["post_uninstall_tools_absent"] is not True
    ):
        raise EvidenceError("Claude Desktop extension activation did not pass")
    _label(extension["client_version"], "desktop_extension.client_version")
    _label(extension["platform_version"], "desktop_extension.platform_version")
    if extension["visible_tools"] != list(MCPB_REQUIRED_TOOLS):
        raise EvidenceError("Desktop extension tool inventory differs")
    if extension["called_tools"] != list(MCPB_REQUIRED_TOOLS):
        raise EvidenceError("Desktop extension tool call coverage differs")
    if extension["runtime_producer_build"] != producer_build:
        raise EvidenceError("Desktop extension build does not equal the source tree")
    if extension["runtime_version"] != VERSION:
        raise EvidenceError(
            "Desktop extension runtime version does not equal the candidate"
        )
    _exact_count(
        extension["demo_round_count"], 4, "desktop_extension.demo_round_count"
    )
    _passed(
        extension["post_apply_list_rounds_status"],
        "desktop_extension.post_apply_list_rounds_status",
    )
    _exact_count(
        extension["post_apply_round_count"],
        5,
        "desktop_extension.post_apply_round_count",
    )
    if (
        extension["source_sha256_unchanged"] is not True
        or extension["output_sha256_matches_list_rounds"] is not True
        or extension["output_sha256_matches_reextract"] is not True
    ):
        raise EvidenceError("Claude Desktop extension post-apply readback failed")
    _hex_digest(
        extension["session_transcript_sha256"],
        64,
        "desktop_extension.session_transcript_sha256",
    )
    _hex_digest(
        extension["demo_journal_sha256"],
        64,
        "desktop_extension.demo_journal_sha256",
    )
    for field in (
        "fresh_install_status",
        "reinstall_same_artifact_status",
        "uninstall_status",
    ):
        _passed(extension[field], f"desktop_extension.{field}")


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


def _read_packet(path: Path) -> bytes:
    with path.open("rb") as handle:
        payload = handle.read(MAX_EVIDENCE_BYTES + 1)
    if len(payload) > MAX_EVIDENCE_BYTES:
        raise EvidenceError("evidence packet exceeds the size limit")
    return payload


def _parse_bounded_int(raw: str) -> int:
    digits = raw[1:] if raw.startswith("-") else raw
    if len(digits) > MAX_PACKET_INTEGER_DIGITS:
        raise EvidenceError(
            f"evidence packet integer exceeds {MAX_PACKET_INTEGER_DIGITS} digits"
        )
    return int(raw)


def _parse_packet(payload: bytes) -> Any:
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceError("evidence packet contains duplicate keys")
            result[key] = value
        return result

    def reject_non_finite(value):
        raise EvidenceError(f"evidence packet contains non-finite number {value}")

    try:
        return json.loads(
            payload,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_finite,
            parse_int=_parse_bounded_int,
        )
    except EvidenceError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise EvidenceError("evidence packet is not valid UTF-8 JSON") from exc


def _canonical_packet_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceError("evidence packet cannot be canonicalized") from exc


def _load_packet_and_bytes(path: Path) -> tuple[Any, bytes]:
    payload = _read_packet(path)
    packet = _parse_packet(payload)
    if payload != _canonical_packet_bytes(packet):
        raise EvidenceError("evidence packet is not canonical compact JSON")
    return packet, payload


def _load_packet(path: Path) -> Any:
    return _load_packet_and_bytes(path)[0]


def _packet_digest(payload: bytes, expected_sha256: str | None = None) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None:
        expected = _hex_digest(expected_sha256, 64, "expected evidence SHA-256")
        if digest != expected:
            raise EvidenceError("evidence packet SHA-256 differs from expected")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate an external sanitized Veqtor I8 evidence packet"
    )
    parser.add_argument("packet", type=Path)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--expected-sha256")
    args = parser.parse_args(argv)
    source_root = args.source_root.resolve()
    packet_digest: str | None = None
    try:
        if _git(source_root, "status", "--porcelain", "--untracked-files=all"):
            raise EvidenceError("source worktree is not clean")
        candidate_sha = _git(source_root, "rev-parse", "HEAD")
        candidate_tree = _git(source_root, "rev-parse", "HEAD^{tree}")
        sys.path.insert(0, str(source_root / "src"))
        from veqtor_mcp.records import SOURCE_SNAPSHOT_IDENTITY

        packet, payload = _load_packet_and_bytes(args.packet)
        validate_evidence(
            packet,
            candidate_sha=candidate_sha,
            candidate_tree=candidate_tree,
            producer_build=SOURCE_SNAPSHOT_IDENTITY,
        )
        packet_digest = _packet_digest(payload, args.expected_sha256)
    except (EvidenceError, OSError) as exc:
        print(f"acceptance evidence failed: {exc}", file=sys.stderr)
        return 1
    assert packet_digest is not None
    print(f"acceptance evidence passed: sha256:{packet_digest}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
