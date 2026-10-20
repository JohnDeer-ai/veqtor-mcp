# SPDX-License-Identifier: Apache-2.0
"""Internal, versioned intent/result records for future round operations.

These pure functions perform no I/O and do not record user consent, authorize
an author, publish a DOCX, or persist a journal. Callers must validate the edit
plan against its operation schema and perform the actual guarded save. The
records describe supplied observations, not authenticated events.
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
import uuid
from typing import Any

from ._ooxml import is_xml_text_compatible, tracked_change_author_validation_error

INTENT_SCHEMA = "round_operation_intent.v1"
RESULT_SCHEMA = "round_operation_result.v1"
MAX_PLAN_BYTES = 1_048_576
MAX_PLAN_NODES = 20_000
MAX_PLAN_DEPTH = 32
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_INTENT_KEYS = frozenset({
    "schema_version", "operation_id", "producer", "source_sha256",
    "candidate_sha256", "author_profile", "destination", "save_mode",
    "plan", "plan_sha256",
})
_RESULT_KEYS = frozenset({
    "schema_version", "operation_id", "intent_sha256",
    "publication_status", "observed_output_sha256", "verification",
    "backup", "backup_verification", "error_code",
})


class OperationRecordError(ValueError):
    """A sanitized refusal of an invalid internal record."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str = "invalid_operation_record") -> None:
    if not condition:
        raise OperationRecordError(code)


def _string(value: object, maximum: int) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= maximum
        and value.strip() == value
        and not any(ord(char) < 32 for char in value)
    )


def _hash(value: object) -> bool:
    return type(value) is str and _HEX.fullmatch(value) is not None


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise OperationRecordError("invalid_operation_record") from exc


def canonical_plan_bytes(plan: object) -> bytes:
    """Bound and canonicalize a JSON-object plan, without validating edit kinds."""
    _require(type(plan) is dict and bool(plan), "invalid_operation_plan")
    nodes = 0
    string_bytes = 0

    def visit(value: object, depth: int) -> None:
        nonlocal nodes, string_bytes
        nodes += 1
        _require(
            depth <= MAX_PLAN_DEPTH and nodes <= MAX_PLAN_NODES,
            "operation_plan_limit_exceeded",
        )
        if type(value) is dict:
            for key, child in value.items():
                _require(type(key) is str, "invalid_operation_plan")
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif type(value) is list:
            for child in value:
                visit(child, depth + 1)
        elif type(value) is str:
            # Check cheap length before encoding a potentially huge string.
            _require(len(value) <= MAX_PLAN_BYTES, "operation_plan_limit_exceeded")
            try:
                string_bytes += len(value.encode("utf-8"))
            except UnicodeError as exc:
                raise OperationRecordError("invalid_operation_plan") from exc
            _require(string_bytes <= MAX_PLAN_BYTES, "operation_plan_limit_exceeded")
        elif type(value) is int:
            _require(value.bit_length() <= 425, "operation_plan_limit_exceeded")
        elif type(value) is float:
            _require(math.isfinite(value), "invalid_operation_plan")
        else:
            _require(value is None or type(value) is bool, "invalid_operation_plan")

    visit(plan, 0)
    payload = _json_bytes(plan)
    _require(len(payload) <= MAX_PLAN_BYTES, "operation_plan_limit_exceeded")
    return payload


def validate_operation_intent(record: object) -> dict[str, Any]:
    """Return an independent JSON copy of a closed and self-consistent intent."""
    _require(type(record) is dict and record.keys() == _INTENT_KEYS)
    _require(record["schema_version"] == INTENT_SCHEMA)
    _require(_string(record["operation_id"], 36))
    try:
        identifier = str(uuid.UUID(record["operation_id"]))
    except ValueError as exc:
        raise OperationRecordError("invalid_operation_record") from exc
    _require(identifier == record["operation_id"])
    _require(_string(record["producer"], 256))
    _require(_hash(record["source_sha256"]) and _hash(record["candidate_sha256"]))
    profile = record["author_profile"]
    _require(type(profile) is dict and profile.keys() == {"name", "initials"})
    _require(tracked_change_author_validation_error(profile["name"]) is None)
    _require(
        type(profile["initials"]) is str and bool(profile["initials"].strip())
        and len(profile["initials"]) <= 32 and is_xml_text_compatible(profile["initials"])
    )
    _require(_string(record["destination"], 4096))
    _require(record["destination"].startswith("/"))
    _require(record["save_mode"] in ("replace_source", "create_new"))
    plan_payload = canonical_plan_bytes(record["plan"])
    _require(
        _hash(record["plan_sha256"])
        and hashlib.sha256(plan_payload).hexdigest() == record["plan_sha256"],
        "operation_plan_digest_mismatch",
    )
    return json.loads(_json_bytes(record))


def make_operation_intent(
    *,
    plan: dict[str, Any],
    source_sha256: str,
    candidate_sha256: str,
    author_profile: dict[str, str],
    destination: str,
    save_mode: str,
    producer: str,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Describe an already validated plan; this is not an executable preflight."""
    plan_payload = canonical_plan_bytes(plan)
    return validate_operation_intent({
        "schema_version": INTENT_SCHEMA,
        "operation_id": operation_id if operation_id is not None else str(uuid.uuid4()),
        "producer": producer,
        "source_sha256": source_sha256,
        "candidate_sha256": candidate_sha256,
        "author_profile": author_profile,
        "destination": destination,
        "save_mode": save_mode,
        "plan": json.loads(plan_payload),
        "plan_sha256": hashlib.sha256(plan_payload).hexdigest(),
    })


def operation_intent_digest(intent: object) -> str:
    """Bind every intent field, not just the requested wording."""
    return hashlib.sha256(_json_bytes(validate_operation_intent(intent))).hexdigest()


def _backup_verification(intent: dict[str, Any], backup: object) -> str:
    if backup is None:
        return "not_required" if intent["save_mode"] == "create_new" else "not_checked"
    _require(type(backup) is dict and backup.keys() == {"path", "sha256"})
    _require(_string(backup["path"], 4096) and backup["path"].startswith("/"))
    _require(backup["sha256"] is None or _hash(backup["sha256"]))
    # Catch lexical aliases without inspecting the filesystem. Distinct strings
    # cannot prove distinct files; the save engine must check filesystem aliases.
    destination = posixpath.normpath("/" + intent["destination"].lstrip("/"))
    backup_path = posixpath.normpath("/" + backup["path"].lstrip("/"))
    if backup_path == destination:
        return "same_as_output"
    if backup["sha256"] is None:
        return "not_checked"
    return "matched" if backup["sha256"] == intent["source_sha256"] else "mismatch"


def validate_operation_result(
    intent: object, record: object,
) -> dict[str, Any]:
    """Check supplied outcome facts, preserving uncertainty and late errors."""
    checked = validate_operation_intent(intent)
    _require(type(record) is dict and record.keys() == _RESULT_KEYS)
    _require(record["schema_version"] == RESULT_SCHEMA)
    _require(
        record["operation_id"] == checked["operation_id"]
        and record["intent_sha256"] == operation_intent_digest(checked),
        "operation_intent_mismatch",
    )
    status = record["publication_status"]
    _require(status in ("not_published", "published", "uncertain"))
    output_hash = record["observed_output_sha256"]
    _require(output_hash is None or _hash(output_hash))
    verification = (
        "not_checked" if output_hash is None
        else "matched" if output_hash == checked["candidate_sha256"]
        else "mismatch"
    )
    _require(record["verification"] == verification)
    error = record["error_code"]
    _require(error is None or (type(error) is str and _CODE.fullmatch(error) is not None))
    if status == "not_published":
        _require(output_hash is None)
    elif status == "uncertain" or verification != "matched":
        _require(error is not None)
    backup = record["backup"]
    backup_verification = _backup_verification(checked, backup)
    _require(record["backup_verification"] == backup_verification)
    if backup_verification in ("mismatch", "same_as_output") or (
        status == "published" and checked["save_mode"] == "replace_source"
        and backup_verification != "matched"
    ):
        _require(error is not None)
    return json.loads(_json_bytes(record))


def make_operation_result(
    intent: object,
    *,
    publication_status: str,
    observed_output_sha256: str | None = None,
    backup: dict[str, str | None] | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Build a result from caller observations; never infer a safe retry."""
    checked = validate_operation_intent(intent)
    verification = (
        "not_checked" if observed_output_sha256 is None
        else "matched" if observed_output_sha256 == checked["candidate_sha256"]
        else "mismatch"
    )
    return validate_operation_result(checked, {
        "schema_version": RESULT_SCHEMA,
        "operation_id": checked["operation_id"],
        "intent_sha256": operation_intent_digest(checked),
        "publication_status": publication_status,
        "observed_output_sha256": observed_output_sha256,
        "verification": verification,
        "backup": backup,
        "backup_verification": _backup_verification(checked, backup),
        "error_code": error_code,
    })
