# SPDX-License-Identifier: Apache-2.0
"""Validate a path-free exact-SHA I8 acceptance evidence packet."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from release_contract import (
    FIVE_EDIT_OUTPUT_SHA256,
    MCPB_REQUIRED_TOOLS,
    PREVIOUS_PUBLIC_MCPB_SHA256,
    PREVIOUS_PUBLIC_MCPB_TOOLS,
    PREVIOUS_PUBLIC_VERSION,
    VERSION,
)


SCHEMA_VERSION = "veqtor_release_acceptance.v6"
MAX_EVIDENCE_BYTES = 64 * 1024
MAX_PACKET_INTEGER_DIGITS = 128
HEX = frozenset("0123456789abcdef")
_VERSION_COMPONENT = r"(?:0|[1-9][0-9]{0,5})"
_CLIENT_VERSION_PATTERN = re.compile(
    rf"{_VERSION_COMPONENT}(?:\.{_VERSION_COMPONENT}){{2,3}}"
)
_PLATFORM_VERSION_PATTERN = re.compile(
    rf"{_VERSION_COMPONENT}(?:\.{_VERSION_COMPONENT}){{1,2}}"
)


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


def _boolean(value: Any, expected: bool, location: str) -> None:
    if value is not expected:
        raise EvidenceError(f"{location} does not equal {expected}")


def _version(
    value: Any,
    *,
    pattern: re.Pattern[str],
    grammar: str,
    location: str,
) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise EvidenceError(f"{location} does not match {grammar}")
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
            "fresh_user_profile",
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
        or desktop["client"] != "claude_desktop_fresh_user_profile"
        or desktop["fresh_user_profile"] is not True
        or desktop["event_omitted_from_records"] is not True
        or desktop["current_event_not_in_access_count"] is not True
        or desktop["raw_vs_compact_explained"] is not True
    ):
        raise EvidenceError("Claude Desktop rehearsal did not pass")
    if desktop["runtime_producer_build"] != producer_build:
        raise EvidenceError("Desktop runtime build does not equal the source tree")
    if desktop["runtime_version"] != VERSION:
        raise EvidenceError("Desktop runtime version does not equal the candidate")
    _hex_digest(desktop["transcript_sha256"], 64, "desktop_rehearsal.transcript_sha256")
    _hex_digest(
        desktop["raw_journal_sha256"],
        64,
        "desktop_rehearsal.raw_journal_sha256",
    )

    extension = _exact_keys(
        packet["desktop_extension"],
        {
            "artifact_sha256",
            "artifact_origin",
            "installation_channel",
            "platform",
            "client",
            "client_version",
            "platform_version",
            "environment",
            "host_managed_uv_runtime_confirmed",
            "tracked_change_author_confirmed",
            "extension_enabled_confirmed",
            "server_connected_confirmed",
            "english_scenario_completed",
            "visible_tools",
            "called_tools",
            "runtime_producer_build",
            "runtime_version",
            "demo_round_count",
            "bundled_demo_prompt_completed",
            "inspection_map",
            "history_trace",
            "verify_quote_v2",
            "compact_privacy",
            "stdio_lifecycle",
            "post_apply_list_rounds_status",
            "post_apply_round_count",
            "source_sha256_unchanged",
            "output_sha256_matches_list_rounds",
            "output_sha256_matches_reextract",
            "session_transcript_sha256",
            "demo_journal_sha256",
            "lifecycle",
        },
        "desktop_extension",
    )
    candidate_artifact_digest = _hex_digest(
        extension["artifact_sha256"],
        64,
        "desktop_extension.artifact_sha256",
    )
    if (
        extension["artifact_origin"] != "successful_main_ci_artifact"
        or extension["installation_channel"] != "direct_download_mcpb"
        or extension["platform"] != "darwin"
        or extension["client"] != "claude_desktop_fresh_user_profile"
    ):
        raise EvidenceError("Claude Desktop extension identity differs")
    environment = _exact_keys(
        extension["environment"],
        {
            "kind",
            "physical_host",
            "clean_physical_mac_claimed",
            "fresh_user_profile",
            "preexisting_veqtor_user_state_absent",
            "repository_checkout_absent",
            "manual_server_configuration_absent",
            "developer_runtime_used",
        },
        "desktop_extension.environment",
    )
    if (
        environment["kind"] != "fresh_isolated_standard_macos_user_v1"
        or environment["physical_host"] != "maintainer_mac"
    ):
        raise EvidenceError("Desktop acceptance environment identity differs")
    for field in (
        "fresh_user_profile",
        "preexisting_veqtor_user_state_absent",
        "repository_checkout_absent",
        "manual_server_configuration_absent",
    ):
        _boolean(environment[field], True, f"desktop_extension.environment.{field}")
    for field in ("clean_physical_mac_claimed", "developer_runtime_used"):
        _boolean(environment[field], False, f"desktop_extension.environment.{field}")
    for field in (
        "host_managed_uv_runtime_confirmed",
        "tracked_change_author_confirmed",
        "extension_enabled_confirmed",
        "server_connected_confirmed",
        "english_scenario_completed",
        "bundled_demo_prompt_completed",
    ):
        _boolean(extension[field], True, f"desktop_extension.{field}")
    _version(
        extension["client_version"],
        pattern=_CLIENT_VERSION_PATTERN,
        grammar="MAJOR.MINOR.PATCH[.BUILD]",
        location="desktop_extension.client_version",
    )
    _version(
        extension["platform_version"],
        pattern=_PLATFORM_VERSION_PATTERN,
        grammar="MAJOR.MINOR[.PATCH]",
        location="desktop_extension.platform_version",
    )
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
    _exact_count(extension["demo_round_count"], 4, "desktop_extension.demo_round_count")
    inspection_map = _exact_keys(
        extension["inspection_map"],
        {
            "inspect_browse_status",
            "inspect_record_status",
            "round_map_schema_version",
            "round_map_status",
            "round_map_record_status",
            "scan_complete",
            "candidate_document_count",
            "exact_content_equality_count",
            "navigation_candidate_count",
            "recorded_derivation_count",
            "ambiguous_count",
            "exact_unique_count",
            "unresolved_count",
            "derivation_recorded",
            "lineage_verified",
            "chronology_verified",
            "support_profile",
            "supporting_record_count",
            "supporting_current_count",
        },
        "desktop_extension.inspection_map",
    )
    if inspection_map != {
        "inspect_browse_status": "passed",
        "inspect_record_status": "written",
        "round_map_schema_version": "round_map.v1",
        "round_map_status": "ok",
        "round_map_record_status": "written",
        "scan_complete": True,
        "candidate_document_count": 5,
        "exact_content_equality_count": 4,
        "navigation_candidate_count": 0,
        "recorded_derivation_count": 1,
        "ambiguous_count": 0,
        "exact_unique_count": 4,
        "unresolved_count": 1,
        "derivation_recorded": True,
        "lineage_verified": False,
        "chronology_verified": False,
        "support_profile": "current_only",
        "supporting_record_count": 1,
        "supporting_current_count": 1,
    }:
        raise EvidenceError("Desktop inspection and Round Map acceptance differs")
    for field, expected in {
        "candidate_document_count": 5,
        "exact_content_equality_count": 4,
        "navigation_candidate_count": 0,
        "recorded_derivation_count": 1,
        "ambiguous_count": 0,
        "exact_unique_count": 4,
        "unresolved_count": 1,
        "supporting_record_count": 1,
        "supporting_current_count": 1,
    }.items():
        _exact_count(
            inspection_map[field], expected, f"desktop_extension.inspection_map.{field}"
        )
    for field, expected in {
        "scan_complete": True,
        "derivation_recorded": True,
        "lineage_verified": False,
        "chronology_verified": False,
    }.items():
        _boolean(
            inspection_map[field], expected, f"desktop_extension.inspection_map.{field}"
        )

    history = _exact_keys(
        extension["history_trace"],
        {
            "schema_version",
            "status",
            "record_status",
            "ordering_source",
            "result_order",
            "candidate_document_count",
            "returned_observation_count",
            "selected_paragraph_count",
            "exact_unique_count",
            "ambiguous_count",
            "unresolved_count",
            "rejected_projection_equality_count",
            "next_cursor_absent",
            "seed_deletion_change_unit_present",
            "seed_deletion_author_literal_is_53",
            "change_units_restricted_to_selected_paragraph",
            "authorship_verified",
            "time_verified",
            "selected_relationships_lineage_verified",
            "chronology_verified",
            "semantic_identity_verified",
        },
        "desktop_extension.history_trace",
    )
    if (
        history["schema_version"] != "paragraph_history.v1"
        or history["status"] != "ok"
        or history["record_status"] != "written"
        or history["ordering_source"] != "filename_lexicographic_v1"
        or history["result_order"] != "seed_then_descending_position_v1"
    ):
        raise EvidenceError("Desktop history trace identity differs")
    for field, expected in {
        "candidate_document_count": 4,
        "returned_observation_count": 4,
        "selected_paragraph_count": 4,
        "exact_unique_count": 3,
        "ambiguous_count": 0,
        "unresolved_count": 0,
        "rejected_projection_equality_count": 3,
    }.items():
        _exact_count(
            history[field], expected, f"desktop_extension.history_trace.{field}"
        )
    for field in (
        "next_cursor_absent",
        "seed_deletion_change_unit_present",
        "seed_deletion_author_literal_is_53",
        "change_units_restricted_to_selected_paragraph",
    ):
        _boolean(history[field], True, f"desktop_extension.history_trace.{field}")
    for field in (
        "authorship_verified",
        "time_verified",
        "selected_relationships_lineage_verified",
        "chronology_verified",
        "semantic_identity_verified",
    ):
        _boolean(history[field], False, f"desktop_extension.history_trace.{field}")

    verification = _exact_keys(
        extension["verify_quote_v2"],
        {
            "schema_version",
            "verdict",
            "exact",
            "record_status",
            "checked_projection_schema_version",
            "checked_projection_mode",
            "checked_projection_status",
            "match_count",
            "match_side",
            "diff_count",
            "checked_anchor_matches_history_seed",
            "projection_sha256_matches_history",
        },
        "desktop_extension.verify_quote_v2",
    )
    if verification != {
        "schema_version": "verification_result.v2",
        "verdict": "exact",
        "exact": True,
        "record_status": "written",
        "checked_projection_schema_version": "verified_paragraph_projection.v1",
        "checked_projection_mode": "pending_text_revisions_rejected_v1",
        "checked_projection_status": "complete",
        "match_count": 1,
        "match_side": "paragraph_rejected_pending",
        "diff_count": 0,
        "checked_anchor_matches_history_seed": True,
        "projection_sha256_matches_history": True,
    }:
        raise EvidenceError("Desktop verify_quote v2 acceptance differs")
    _exact_count(
        verification["match_count"], 1, "desktop_extension.verify_quote_v2.match_count"
    )
    _exact_count(
        verification["diff_count"], 0, "desktop_extension.verify_quote_v2.diff_count"
    )
    for field in (
        "exact",
        "checked_anchor_matches_history_seed",
        "projection_sha256_matches_history",
    ):
        _boolean(
            verification[field],
            True,
            f"desktop_extension.verify_quote_v2.{field}",
        )

    privacy = _exact_keys(
        extension["compact_privacy"],
        {
            "export_record_status",
            "export_payloads",
            "history_record_type",
            "verification_record_type",
            "history_record_present",
            "verification_record_present",
            "history_raw_path_text_author_absent",
            "history_compact_path_text_author_absent",
            "verification_compact_path_text_clause_absent",
            "history_snapshot_digests_match_live",
            "verification_projection_hashes_match_live",
        },
        "desktop_extension.compact_privacy",
    )
    if (
        privacy["export_record_status"] != "written"
        or privacy["export_payloads"] != "compact"
        or privacy["history_record_type"] != "paragraph_history.v1"
        or privacy["verification_record_type"] != "verification.v2"
    ):
        raise EvidenceError("Desktop compact privacy identity differs")
    for field in (
        "history_record_present",
        "verification_record_present",
        "history_raw_path_text_author_absent",
        "history_compact_path_text_author_absent",
        "verification_compact_path_text_clause_absent",
        "history_snapshot_digests_match_live",
        "verification_projection_hashes_match_live",
    ):
        _boolean(privacy[field], True, f"desktop_extension.compact_privacy.{field}")

    stdio_lifecycle = _exact_keys(
        extension["stdio_lifecycle"],
        {
            "client_request_abandonment_status",
            "cancellation_notification_status",
            "post_cancellation_session_recovery_status",
            "server_work_cancellation_verified",
            "cancelled_request_side_effect_absence_verified",
            "process_teardown_status",
        },
        "desktop_extension.stdio_lifecycle",
    )
    for field in (
        "client_request_abandonment_status",
        "cancellation_notification_status",
        "post_cancellation_session_recovery_status",
        "process_teardown_status",
    ):
        _passed(stdio_lifecycle[field], f"desktop_extension.stdio_lifecycle.{field}")
    for field in (
        "server_work_cancellation_verified",
        "cancelled_request_side_effect_absence_verified",
    ):
        _boolean(
            stdio_lifecycle[field],
            False,
            f"desktop_extension.stdio_lifecycle.{field}",
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
    lifecycle = _exact_keys(
        extension["lifecycle"],
        {
            "scenario",
            "previous_artifact_source",
            "previous_artifact_version",
            "initial_artifact_sha256",
            "initial_checksum_status",
            "previous_install_status",
            "previous_visible_tools",
            "upgrade_status",
            "post_upgrade_artifact_sha256",
            "post_upgrade_checksum_status",
            "post_upgrade_runtime_version",
            "post_upgrade_visible_tools",
            "rollback_status",
            "post_rollback_artifact_sha256",
            "post_rollback_checksum_status",
            "post_rollback_runtime_version",
            "post_rollback_visible_tools",
            "post_rollback_smoke_status",
            "post_rollback_workspace_kind",
            "rollback_scope",
            "v04_workspace_presented_to_v03",
            "v04_journal_downgrade_claimed",
            "candidate_reinstall_status",
            "post_reinstall_artifact_sha256",
            "post_reinstall_checksum_status",
            "post_reinstall_runtime_version",
            "post_reinstall_visible_tools",
            "uninstall_status",
            "post_uninstall_tools_absent",
        },
        "desktop_extension.lifecycle",
    )
    if (
        lifecycle["scenario"] != "v0.3.0_to_v0.4.0_upgrade_rollback_v1"
        or lifecycle["previous_artifact_source"] != "immutable_github_release_v0.3.0"
        or lifecycle["previous_artifact_version"] != PREVIOUS_PUBLIC_VERSION
        or lifecycle["post_upgrade_runtime_version"] != VERSION
        or lifecycle["post_rollback_runtime_version"] != PREVIOUS_PUBLIC_VERSION
        or lifecycle["post_reinstall_runtime_version"] != VERSION
        or lifecycle["post_rollback_workspace_kind"]
        != "fresh_v03_compatible_workspace_v1"
        or lifecycle["rollback_scope"] != "extension_runtime_and_tool_surface_only"
    ):
        raise EvidenceError("Desktop extension lifecycle identity differs")
    expected_artifact_digests = {
        "initial_artifact_sha256": PREVIOUS_PUBLIC_MCPB_SHA256,
        "post_upgrade_artifact_sha256": candidate_artifact_digest,
        "post_rollback_artifact_sha256": PREVIOUS_PUBLIC_MCPB_SHA256,
        "post_reinstall_artifact_sha256": candidate_artifact_digest,
    }
    for field, expected_digest in expected_artifact_digests.items():
        observed_digest = _hex_digest(
            lifecycle[field],
            64,
            f"desktop_extension.lifecycle.{field}",
        )
        if observed_digest != expected_digest:
            raise EvidenceError(
                f"desktop_extension.lifecycle.{field} does not equal the accepted artifact"
            )
    previous_tools = list(PREVIOUS_PUBLIC_MCPB_TOOLS)
    candidate_tools = list(MCPB_REQUIRED_TOOLS)
    if (
        lifecycle["previous_visible_tools"] != previous_tools
        or lifecycle["post_upgrade_visible_tools"] != candidate_tools
        or lifecycle["post_rollback_visible_tools"] != previous_tools
        or lifecycle["post_reinstall_visible_tools"] != candidate_tools
    ):
        raise EvidenceError("Desktop extension lifecycle tool inventory differs")
    for field in (
        "initial_checksum_status",
        "previous_install_status",
        "upgrade_status",
        "post_upgrade_checksum_status",
        "rollback_status",
        "post_rollback_checksum_status",
        "post_rollback_smoke_status",
        "candidate_reinstall_status",
        "post_reinstall_checksum_status",
        "uninstall_status",
    ):
        _passed(lifecycle[field], f"desktop_extension.lifecycle.{field}")
    _boolean(
        lifecycle["v04_workspace_presented_to_v03"],
        False,
        "desktop_extension.lifecycle.v04_workspace_presented_to_v03",
    )
    _boolean(
        lifecycle["v04_journal_downgrade_claimed"],
        False,
        "desktop_extension.lifecycle.v04_journal_downgrade_claimed",
    )
    _boolean(
        lifecycle["post_uninstall_tools_absent"],
        True,
        "desktop_extension.lifecycle.post_uninstall_tools_absent",
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
