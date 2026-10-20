# SPDX-License-Identifier: Apache-2.0
"""Synthetic contract checks for observations, not journal/save integration."""

from __future__ import annotations

from copy import deepcopy

import pytest

from veqtor_docx import round_operations as operations

SOURCE = "a" * 64
CANDIDATE = "b" * 64


def _intent(**overrides):
    arguments = {
        "plan": {"operations": [{"kind": "synthetic", "text": "Invented wording"}]},
        "source_sha256": SOURCE,
        "candidate_sha256": CANDIDATE,
        "author_profile": {"name": "Synthetic Reviewer", "initials": "SR"},
        "destination": "/private/synthetic/working.docx",
        "save_mode": "replace_source",
        "producer": "synthetic-producer-v1",
        "operation_id": "5f1a90d0-7aa4-45e2-ab05-e64ce7c72084",
    }
    arguments.update(overrides)
    return operations.make_operation_intent(**arguments)


def test_intent_detaches_plan_and_binds_order_but_not_mapping_order():
    plan = {"z": [{"kind": "synthetic"}], "a": "Синтетический пример"}
    intent = _intent(plan=plan)
    plan["z"].append({"kind": "another"})
    assert intent["plan"]["z"] == [{"kind": "synthetic"}]
    reordered = _intent(plan={"a": "Синтетический пример", "z": [{"kind": "synthetic"}]})
    assert operations.operation_intent_digest(intent) == operations.operation_intent_digest(reordered)
    changed = _intent(plan=plan)
    assert changed["plan_sha256"] != intent["plan_sha256"]


def test_intent_accepts_existing_author_profile_limits_without_normalization():
    profile = {"name": "Synthetic Reviewer", "initials": "S" * 32}
    assert _intent(author_profile=profile)["author_profile"] == profile
    with_tab = {**profile, "initials": "S\tR"}
    assert _intent(author_profile=with_tab)["author_profile"] == with_tab
    for invalid in (
        {**profile, "initials": "S" * 33},
        {**profile, "initials": "\x00"},
        {**profile, "name": "\ud800"},
    ):
        with pytest.raises(operations.OperationRecordError):
            _intent(author_profile=invalid)


@pytest.mark.parametrize("change", [
    {"destination": "/private/synthetic/another.docx"},
    {"author_profile": {"name": "Another Synthetic Reviewer", "initials": "AR"}},
    {"producer": "different-producer"},
    {"candidate_sha256": "c" * 64},
    {"source_sha256": "c" * 64},
])
def test_result_cannot_be_reused_with_changed_intent(change):
    result = operations.make_operation_result(_intent(), publication_status="not_published")
    with pytest.raises(operations.OperationRecordError, match="operation_intent_mismatch"):
        operations.validate_operation_result(_intent(**change), result)


def test_plan_tampering_and_extra_record_fields_are_rejected():
    intent = _intent()
    intent["plan"]["operations"][0]["text"] = "Changed wording"
    with pytest.raises(operations.OperationRecordError, match="operation_plan_digest_mismatch"):
        operations.validate_operation_intent(intent)
    intent = _intent()
    intent["sent"] = True
    with pytest.raises(operations.OperationRecordError):
        operations.validate_operation_intent(intent)


def test_successful_published_replacement_requires_exact_backup():
    intent = _intent()
    for backup in (None, {"path": "/private/synthetic/backup.docx", "sha256": "c" * 64}):
        with pytest.raises(operations.OperationRecordError):
            operations.make_operation_result(
                intent, publication_status="published",
                observed_output_sha256=CANDIDATE, backup=backup,
            )
    result = operations.make_operation_result(
        intent, publication_status="published", observed_output_sha256=CANDIDATE,
        backup={"path": "/private/synthetic/backup.docx", "sha256": SOURCE},
    )
    assert result["verification"] == "matched"
    assert result["backup_verification"] == "matched"


@pytest.mark.parametrize(("backup", "expected", "error"), [
    (None, "not_checked", "backup_missing"),
    ({"path": "/private/synthetic/backup.docx", "sha256": None},
     "not_checked", "backup_unreadable"),
    ({"path": "/private/synthetic/backup.docx", "sha256": "c" * 64},
     "mismatch", "backup_digest_mismatch"),
])
def test_published_replacement_preserves_failed_backup_observation(backup, expected, error):
    intent = _intent()
    result = operations.make_operation_result(
        intent, publication_status="published", observed_output_sha256=CANDIDATE,
        backup=backup, error_code=error,
    )
    assert result["publication_status"] == "published"
    assert result["verification"] == "matched"
    assert result["backup"] == backup
    assert result["backup_verification"] == expected
    assert result["error_code"] == error
    for field, value in (("backup_verification", "matched"), ("error_code", None)):
        tampered = {**result, field: value}
        with pytest.raises(operations.OperationRecordError):
            operations.validate_operation_result(intent, tampered)


def test_known_publication_with_both_files_unreadable_is_recordable():
    result = operations.make_operation_result(
        _intent(), publication_status="published", error_code="verification_unavailable",
    )
    assert result["publication_status"] == "published"
    assert result["verification"] == result["backup_verification"] == "not_checked"


def test_create_new_has_no_fabricated_backup():
    result = operations.make_operation_result(
        _intent(save_mode="create_new"), publication_status="published",
        observed_output_sha256=CANDIDATE,
    )
    assert result["backup"] is None
    assert result["backup_verification"] == "not_required"
    assert result["publication_status"] == "published"


def test_late_error_does_not_hide_publication():
    result = operations.make_operation_result(
        _intent(), publication_status="published",
        observed_output_sha256=CANDIDATE,
        backup={"path": "/private/synthetic/backup.docx", "sha256": SOURCE},
        error_code="journal_finish_failed",
    )
    assert result["publication_status"] == "published"
    assert result["verification"] == "matched"
    assert result["error_code"] == "journal_finish_failed"


def test_uncertain_publication_with_matching_bytes_is_still_uncertain():
    result = operations.make_operation_result(
        _intent(), publication_status="uncertain",
        observed_output_sha256=CANDIDATE, error_code="publication_uncertain",
    )
    assert result["publication_status"] == "uncertain"
    with pytest.raises(operations.OperationRecordError):
        operations.make_operation_result(_intent(), publication_status="uncertain")


def test_wrong_published_bytes_are_recordable_only_as_explicit_error():
    arguments = {
        "publication_status": "published",
        "observed_output_sha256": "c" * 64,
        "backup": {"path": "/private/synthetic/backup.docx", "sha256": SOURCE},
    }
    with pytest.raises(operations.OperationRecordError):
        operations.make_operation_result(_intent(), **arguments)
    result = operations.make_operation_result(
        _intent(), **arguments, error_code="output_digest_mismatch",
    )
    assert result["verification"] == "mismatch"
    tampered = deepcopy(result)
    tampered["verification"] = "matched"
    with pytest.raises(operations.OperationRecordError):
        operations.validate_operation_result(_intent(), tampered)


def test_known_publication_with_unreadable_output_remains_explicit():
    result = operations.make_operation_result(
        _intent(), publication_status="published",
        backup={"path": "/private/synthetic/backup.docx", "sha256": SOURCE},
        error_code="output_unreadable",
    )
    assert result["verification"] == "not_checked"
    assert result["publication_status"] == "published"


@pytest.mark.parametrize("plan", [
    {}, {"value": float("nan")}, {"value": float("inf")},
    {"value": "\ud800"}, {1: "bad key"}, {"value": object()},
    {"value": (1, 2)},
])
def test_non_json_or_empty_plans_are_rejected(plan):
    with pytest.raises(operations.OperationRecordError):
        _intent(plan=plan)


def test_cyclic_plan_is_bounded():
    plan = {}
    plan["self"] = plan
    with pytest.raises(operations.OperationRecordError, match="operation_plan_limit_exceeded"):
        _intent(plan=plan)


def test_plan_byte_limit_counts_serialization_overhead(monkeypatch):
    payload = operations.canonical_plan_bytes({"text": "abc"})
    monkeypatch.setattr(operations, "MAX_PLAN_BYTES", len(payload))
    assert operations.canonical_plan_bytes({"text": "abc"}) == payload
    with pytest.raises(operations.OperationRecordError, match="operation_plan_limit_exceeded"):
        operations.canonical_plan_bytes({"text": "abcd"})


def test_plan_node_and_depth_limits(monkeypatch):
    monkeypatch.setattr(operations, "MAX_PLAN_NODES", 3)
    assert operations.canonical_plan_bytes({"value": None})
    with pytest.raises(operations.OperationRecordError, match="operation_plan_limit_exceeded"):
        operations.canonical_plan_bytes({"value": [None]})
    monkeypatch.setattr(operations, "MAX_PLAN_NODES", 100)
    monkeypatch.setattr(operations, "MAX_PLAN_DEPTH", 2)
    with pytest.raises(operations.OperationRecordError, match="operation_plan_limit_exceeded"):
        operations.canonical_plan_bytes({"value": {"child": {"too_deep": None}}})


@pytest.mark.parametrize("change", [
    {"source_sha256": "A" * 64}, {"source_sha256": "a" * 63},
    {"destination": "relative.docx"}, {"operation_id": "not-an-operation-id"},
    {"save_mode": "sent"}, {"author_profile": {"name": "Synthetic", "initials": "S", "role": "own"}},
])
def test_invalid_identifiers_and_unknown_fields_are_rejected(change):
    with pytest.raises(operations.OperationRecordError):
        _intent(**change)
