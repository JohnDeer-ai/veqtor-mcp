# SPDX-License-Identifier: Apache-2.0
"""The external I8 evidence packet is exact-SHA bound and privacy-shaped."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_acceptance_evidence as evidence_module  # noqa: E402
from check_acceptance_evidence import (  # noqa: E402
    EvidenceError,
    MAX_PACKET_INTEGER_DIGITS,
    SCHEMA_VERSION,
    _canonical_packet_bytes,
    _load_packet,
    _packet_digest,
    _parse_packet,
    validate_evidence,
)
from release_contract import (  # noqa: E402
    FIVE_EDIT_OUTPUT_SHA256,
    MCPB_REQUIRED_TOOLS,
    VERSION,
)


CANDIDATE_SHA = "a" * 40
CANDIDATE_TREE = "b" * 40
PRODUCER_BUILD = "source-snapshot-v1-sha256:" + "c" * 64
CORPUS_SHA = "d" * 64
OUTPUT_SHA = FIVE_EDIT_OUTPUT_SHA256
RUNTIME_VERSION = VERSION


def _packet() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree": CANDIDATE_TREE,
        "producer_build": PRODUCER_BUILD,
        "public_matrix": {
            "python_3_12": "passed",
            "python_3_13": "passed",
            "python_3_14": "passed",
            "minimum_direct": "passed",
        },
        "private_dogfood": {
            "used": {
                "passed": 4,
                "skipped": 1,
                "corpus_before_sha256": CORPUS_SHA,
                "corpus_after_sha256": CORPUS_SHA,
            },
            "clean": {
                "passed": 4,
                "skipped": 1,
                "corpus_before_sha256": CORPUS_SHA,
                "corpus_after_sha256": CORPUS_SHA,
            },
        },
        "payment_preflight": {
            "batch_applicable": False,
            "refusal_code": "counter_position_unsupported",
            "match_count": 1,
        },
        "five_edit_batch": {
            "preflight_applicable": True,
            "apply_status": "ok",
            "applied_count": 5,
            "round_trip_status": "passed",
            "collateral_change_count": 0,
            "output_sha256": OUTPUT_SHA,
        },
        "installed_two_export": {
            "first_access_count": 0,
            "second_access_count": 1,
            "first_event_absent_from_windows": True,
            "current_event_outside_own_snapshot": True,
            "runtime_producer_build": PRODUCER_BUILD,
            "runtime_version": RUNTIME_VERSION,
        },
        "desktop_rehearsal": {
            "verdict": "passed",
            "client": "claude_desktop_fresh_copy",
            "fresh_copy": True,
            "event_omitted_from_records": True,
            "current_event_not_in_access_count": True,
            "raw_vs_compact_explained": True,
            "runtime_producer_build": PRODUCER_BUILD,
            "runtime_version": RUNTIME_VERSION,
            "transcript_sha256": "e" * 64,
            "raw_journal_sha256": "f" * 64,
        },
        "desktop_extension": {
            "artifact_sha256": "1" * 64,
            "installation_channel": "direct_download_mcpb",
            "platform": "darwin",
            "client": "claude_desktop_fresh_copy",
            "client_version": "1.0.0",
            "platform_version": "15.5",
            "manual_uv_install_absent": True,
            "manual_python_install_absent": True,
            "host_managed_uv_runtime_confirmed": True,
            "tracked_change_author_confirmed": True,
            "extension_enabled_confirmed": True,
            "server_connected_confirmed": True,
            "visible_tools": list(MCPB_REQUIRED_TOOLS),
            "called_tools": list(MCPB_REQUIRED_TOOLS),
            "runtime_producer_build": PRODUCER_BUILD,
            "runtime_version": RUNTIME_VERSION,
            "demo_round_count": 4,
            "bundled_demo_prompt_completed": True,
            "inspection_map": {
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
            },
            "post_apply_list_rounds_status": "passed",
            "post_apply_round_count": 5,
            "source_sha256_unchanged": True,
            "output_sha256_matches_list_rounds": True,
            "output_sha256_matches_reextract": True,
            "session_transcript_sha256": "2" * 64,
            "demo_journal_sha256": "3" * 64,
            "lifecycle_scenario": "first_public_mcpb",
            "fresh_install_status": "passed",
            "upgrade_status": "not_applicable_first_public_mcpb",
            "rollback_status": "not_applicable_no_prior_public_mcpb",
            "reinstall_same_artifact_status": "passed",
            "uninstall_status": "passed",
            "post_uninstall_tools_absent": True,
        },
    }


def _validate(packet: dict) -> None:
    validate_evidence(
        packet,
        candidate_sha=CANDIDATE_SHA,
        candidate_tree=CANDIDATE_TREE,
        producer_build=PRODUCER_BUILD,
    )


def test_complete_exact_candidate_evidence_passes() -> None:
    _validate(_packet())


def test_documented_working_template_matches_executable_v5_schema() -> None:
    releasing = (ROOT / "RELEASING.md").read_text()
    template = releasing.split("<!-- acceptance-v5-template-begin -->", 1)[1]
    template = template.split("<!-- acceptance-v5-template-end -->", 1)[0]
    packet = json.loads(template.split("```json\n", 1)[1].split("\n```", 1)[0])

    validate_evidence(
        packet,
        candidate_sha=packet["candidate_sha"],
        candidate_tree=packet["candidate_tree"],
        producer_build=packet["producer_build"],
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda packet: packet.update({"private_path": "/Users/example/matter"}),
            "fields differ",
        ),
        (
            lambda packet: packet.update({"candidate_sha": "f" * 40}),
            "candidate does not equal",
        ),
        (
            lambda packet: packet["private_dogfood"]["used"].update(
                {"corpus_after_sha256": "f" * 64}
            ),
            "modified the source corpus",
        ),
        (
            lambda packet: packet["payment_preflight"].update(
                {"match_count": 0}
            ),
            "payment_preflight.match_count does not equal",
        ),
        (
            lambda packet: packet["five_edit_batch"].update(
                {"collateral_change_count": 1}
            ),
            "collateral changes",
        ),
        (
            lambda packet: packet["five_edit_batch"].update(
                {"output_sha256": "0" * 64}
            ),
            "output fingerprint differs",
        ),
        (
            lambda packet: packet.pop("desktop_rehearsal"),
            "fields differ",
        ),
        (
            lambda packet: packet["desktop_rehearsal"].update(
                {"verdict": "failed"}
            ),
            "Desktop rehearsal did not pass",
        ),
        (
            lambda packet: packet["installed_two_export"].update(
                {"second_access_count": 0}
            ),
            "installed_two_export.second_access_count does not equal",
        ),
        (
            lambda packet: packet["desktop_rehearsal"].update(
                {
                    "runtime_producer_build": (
                        "source-snapshot-v1-sha256:" + "0" * 64
                    )
                }
            ),
            "Desktop runtime build does not equal",
        ),
        (
            lambda packet: packet["desktop_extension"].update(
                {"artifact_sha256": "not-a-digest"}
            ),
            "desktop_extension.artifact_sha256 is not",
        ),
        (
            lambda packet: packet["desktop_extension"].update(
                {"manual_uv_install_absent": False}
            ),
            "extension activation did not pass",
        ),
        (
            lambda packet: packet["desktop_extension"].update(
                {"extension_enabled_confirmed": False}
            ),
            "extension activation did not pass",
        ),
        (
            lambda packet: packet["desktop_extension"].update(
                {"server_connected_confirmed": False}
            ),
            "extension activation did not pass",
        ),
        (
            lambda packet: packet["desktop_extension"].update(
                {"visible_tools": ["list_rounds"]}
            ),
            "tool inventory differs",
        ),
        (
            lambda packet: packet["desktop_extension"].update(
                {"called_tools": ["list_rounds"]}
            ),
            "tool call coverage differs",
        ),
        (
            lambda packet: packet["desktop_extension"].update(
                {"post_apply_list_rounds_status": "failed"}
            ),
            "post_apply_list_rounds_status did not pass",
        ),
        (
            lambda packet: packet["desktop_extension"]["inspection_map"].update(
                {"lineage_verified": True}
            ),
            "inspection and Round Map acceptance differs",
        ),
        (
            lambda packet: packet["desktop_extension"].update(
                {"post_apply_round_count": 4}
            ),
            "post_apply_round_count does not equal",
        ),
        (
            lambda packet: packet["desktop_extension"].update(
                {"source_sha256_unchanged": False}
            ),
            "post-apply readback failed",
        ),
        (
            lambda packet: packet["desktop_extension"].update(
                {"output_sha256_matches_list_rounds": False}
            ),
            "post-apply readback failed",
        ),
        (
            lambda packet: packet["desktop_extension"].update(
                {"output_sha256_matches_reextract": False}
            ),
            "post-apply readback failed",
        ),
        (
            lambda packet: packet["desktop_extension"].update(
                {"uninstall_status": "failed"}
            ),
            "uninstall_status did not pass",
        ),
        (
            lambda packet: packet["desktop_extension"].update(
                {"upgrade_status": "passed"}
            ),
            "extension activation did not pass",
        ),
    ],
    ids=[
        "extra_private_field",
        "wrong_sha",
        "corpus_changed",
        "payment",
        "collateral",
        "wrong_output_sha",
        "missing_desktop",
        "desktop_failed",
        "two_export_failed",
        "desktop_wrong_build",
        "mcpb_digest",
        "mcpb_clean_host",
        "mcpb_enabled",
        "mcpb_connected",
        "mcpb_tools",
        "mcpb_tool_calls",
        "mcpb_post_apply_list",
        "mcpb_round_map_lineage",
        "mcpb_post_apply_count",
        "mcpb_source_unchanged",
        "mcpb_output_list_hash",
        "mcpb_output_reextract_hash",
        "mcpb_uninstall",
        "mcpb_first_release_lifecycle",
    ],
)
def test_incomplete_or_non_private_shape_fails(mutate, message: str) -> None:
    packet = copy.deepcopy(_packet())
    mutate(packet)
    with pytest.raises(EvidenceError, match=message):
        _validate(packet)


@pytest.mark.parametrize(
    "schema_version",
    [
        "veqtor_release_acceptance.v2",
        "veqtor_release_acceptance.v3",
        "veqtor_release_acceptance.v4",
    ],
)
def test_older_packet_is_rejected_before_shape_validation(
    schema_version: str,
) -> None:
    packet = _packet()
    packet["schema_version"] = schema_version
    packet.pop("desktop_extension")

    with pytest.raises(EvidenceError, match="schema version is unsupported"):
        _validate(packet)


def test_runtime_versions_and_builds_are_candidate_bound() -> None:
    for section in (
        "installed_two_export",
        "desktop_rehearsal",
        "desktop_extension",
    ):
        packet = _packet()
        packet[section]["runtime_version"] = "0.1.0"
        with pytest.raises(EvidenceError, match="runtime version does not equal"):
            _validate(packet)


@pytest.mark.parametrize(
    ("field", "value", "grammar"),
    [
        ("client_version", "1.0", r"MAJOR\.MINOR\.PATCH"),
        ("client_version", "01.0.0", r"MAJOR\.MINOR\.PATCH"),
        ("client_version", "1.0.0-beta", r"MAJOR\.MINOR\.PATCH"),
        ("client_version", "1.0.0/Users/example", r"MAJOR\.MINOR\.PATCH"),
        ("platform_version", "15", r"MAJOR\.MINOR"),
        ("platform_version", "15.05", r"MAJOR\.MINOR"),
        ("platform_version", "macOS 15.5", r"MAJOR\.MINOR"),
        ("platform_version", "../../15.5", r"MAJOR\.MINOR"),
        ("platform_version", "15.5.1.2", r"MAJOR\.MINOR"),
    ],
)
def test_extension_versions_use_path_free_numeric_grammars(
    field: str,
    value: str,
    grammar: str,
) -> None:
    packet = _packet()
    packet["desktop_extension"][field] = value

    with pytest.raises(EvidenceError, match=grammar):
        _validate(packet)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_version", "1.22209.0"),
        ("client_version", "1.22209.0.42"),
        ("platform_version", "26.5"),
        ("platform_version", "26.5.1"),
    ],
)
def test_extension_versions_accept_public_numeric_formats(
    field: str,
    value: str,
) -> None:
    packet = _packet()
    packet["desktop_extension"][field] = value

    _validate(packet)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("payment_preflight", "match_count", 1.0),
        ("installed_two_export", "first_access_count", 0.0),
        ("installed_two_export", "second_access_count", 1.0),
        ("desktop_extension", "post_apply_round_count", 5.0),
    ],
)
def test_exact_count_fields_reject_json_floats(
    section: str,
    field: str,
    value: float,
) -> None:
    packet = _packet()
    packet[section][field] = value

    with pytest.raises(EvidenceError, match="not an integer count"):
        _validate(packet)


def test_canonical_packet_loader_has_one_transport_representation(
    tmp_path: Path,
) -> None:
    packet = _packet()
    canonical = _canonical_packet_bytes(packet)
    evidence = tmp_path / "evidence.json"
    evidence.write_bytes(canonical)

    assert _load_packet(evidence) == packet
    for suffix_or_payload in (
        canonical + b"\n",
        b" " + canonical,
        json.dumps(packet).encode("utf-8"),
    ):
        evidence.write_bytes(suffix_or_payload)
        with pytest.raises(EvidenceError, match="canonical compact JSON"):
            _load_packet(evidence)


def test_expected_digest_binds_the_canonical_packet_bytes() -> None:
    canonical = _canonical_packet_bytes(_packet())
    expected = hashlib.sha256(canonical).hexdigest()

    assert _packet_digest(canonical, expected) == expected
    with pytest.raises(EvidenceError, match="differs from expected"):
        _packet_digest(canonical, "0" * 64)
    with pytest.raises(EvidenceError, match="lowercase hex digest"):
        _packet_digest(canonical, "not-a-digest")


def test_evidence_loader_is_bounded_and_rejects_duplicate_keys(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"one","schema_version":"two"}')
    with pytest.raises(EvidenceError, match="duplicate keys"):
        _load_packet(duplicate)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(json.dumps(_packet()).encode() + b" " * (64 * 1024))
    with pytest.raises(EvidenceError, match="size limit"):
        _load_packet(oversized)

    non_finite = tmp_path / "non-finite.json"
    non_finite.write_bytes(b'{"value":NaN}')
    with pytest.raises(EvidenceError, match="non-finite"):
        _load_packet(non_finite)


def test_evidence_json_integer_digit_limit_is_sign_independent() -> None:
    assert MAX_PACKET_INTEGER_DIGITS == 128
    positive = _parse_packet(
        b'{"value":' + b"9" * MAX_PACKET_INTEGER_DIGITS + b"}"
    )
    negative = _parse_packet(
        b'{"value":-' + b"9" * MAX_PACKET_INTEGER_DIGITS + b"}"
    )

    assert positive["value"] > 0
    assert negative["value"] < 0


@pytest.mark.parametrize("digit_count", [129, 5001])
def test_evidence_json_integer_digit_limit_is_controlled(digit_count: int) -> None:
    payload = b'{"value":' + b"9" * digit_count + b"}"

    with pytest.raises(EvidenceError, match="integer exceeds 128 digits"):
        _parse_packet(payload)


def test_evidence_cli_rejects_huge_integer_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = tmp_path / "huge-integer.json"
    evidence.write_bytes(b'{"value":' + b"9" * 5001 + b"}")

    def clean_candidate(_source_root: Path, *arguments: str) -> str:
        if arguments == ("status", "--porcelain", "--untracked-files=all"):
            return ""
        if arguments == ("rev-parse", "HEAD"):
            return CANDIDATE_SHA
        if arguments == ("rev-parse", "HEAD^{tree}"):
            return CANDIDATE_TREE
        raise AssertionError(f"unexpected git arguments: {arguments}")

    monkeypatch.setattr(evidence_module, "_git", clean_candidate)

    assert evidence_module.main([str(evidence), "--source-root", str(ROOT)]) == 1
    stderr = capsys.readouterr().err
    assert "acceptance evidence failed: evidence packet integer exceeds 128 digits" in stderr
    assert "Traceback" not in stderr
