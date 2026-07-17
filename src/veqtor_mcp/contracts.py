# SPDX-License-Identifier: Apache-2.0
"""Versioned JSON contracts advertised by the Veqtor MCP tools.

The public Python helpers intentionally continue to accept and return ordinary
``dict`` objects.  ``WithJsonSchema`` lets the MCP boundary advertise a useful,
closed schema without converting those dictionaries into Pydantic model
instances for direct Python callers.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Any, ClassVar, Literal

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, WithJsonSchema

MCP_CONTRACT_SCHEMA_VERSION = "veqtor.mcp.v0.2"
MCP_CONTRACT_META_KEY = "veqtor.pro/contractSchemaVersion"
MCP_CONTRACT_SCHEMA_EXTENSION = "x-veqtor-contract-schema-version"


def contract_meta() -> dict[str, str]:
    """Return per-tool MCP metadata with the advertised contract version."""
    return {MCP_CONTRACT_META_KEY: MCP_CONTRACT_SCHEMA_VERSION}


def local_journaling_annotations(title: str) -> ToolAnnotations:
    """Describe a local tool whose otherwise factual call appends provenance.

    Every current tool can append a local decision-record or access-event
    entry.  It would therefore be misleading to mark even extraction and
    verification calls as read-only or idempotent at the complete tool level.
    None of the tools overwrites source data, and none reaches an open-world
    network service.
    """
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )


_STRING = {"type": "string"}
_NONEMPTY_STRING = {"type": "string", "minLength": 1}
_NULLABLE_STRING = {"type": ["string", "null"]}
_SHA256 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_NULLABLE_SHA256 = {"anyOf": [_SHA256, {"type": "null"}]}
_NONNEGATIVE_INTEGER = {"type": "integer", "minimum": 0}
_NULLABLE_NONNEGATIVE_INTEGER = {
    "anyOf": [_NONNEGATIVE_INTEGER, {"type": "null"}]
}
_NULLABLE_FAILURE_PHASE = {
    "anyOf": [
        {
            "enum": [
                "validation",
                "source",
                "matching",
                "planning",
                "surgery",
                "serialization",
                "round_trip",
                "preflight_binding",
                "publication",
            ]
        },
        {"type": "null"},
    ]
}


ANCHOR_INPUT_SCHEMA: dict[str, Any] = {
    "title": "Veqtor change-unit anchor",
    MCP_CONTRACT_SCHEMA_EXTENSION: MCP_CONTRACT_SCHEMA_VERSION,
    "type": "object",
    "properties": {
        "change_unit_id": {
            "type": "string",
            "pattern": "^cu_[0-9]+$",
            "description": "Identifier returned by extract_redlines.",
        },
        "file_sha256": {
            **_SHA256,
            "description": "SHA-256 of the exact DOCX snapshot extracted.",
        },
    },
    "required": ["change_unit_id", "file_sha256"],
    "additionalProperties": False,
}

EDIT_INPUT_SCHEMA: dict[str, Any] = {
    "title": "Veqtor tracked edit",
    MCP_CONTRACT_SCHEMA_EXTENSION: MCP_CONTRACT_SCHEMA_VERSION,
    "type": "object",
    "properties": {
        "anchor": ANCHOR_INPUT_SCHEMA,
        "delete_text": {
            **_NONEMPTY_STRING,
            "description": "Exact text to delete or replace in the anchor.",
        },
        "insert_text": {
            **_STRING,
            "description": "Replacement text inserted after delete_text.",
        },
        "reinstate_text": {
            **_NONEMPTY_STRING,
            "description": (
                "Exact text preserved in a counterparty deletion and reinserted "
                "as a new tracked insertion."
            ),
        },
    },
    "required": ["anchor"],
    "oneOf": [
        {
            "required": ["delete_text"],
            "not": {"required": ["reinstate_text"]},
        },
        {
            "required": ["reinstate_text"],
            "not": {
                "anyOf": [
                    {"required": ["delete_text"]},
                    {"required": ["insert_text"]},
                ]
            },
        },
    ],
    "additionalProperties": False,
}

PREFLIGHT_PROOF_SCHEMA: dict[str, Any] = {
    "title": "Veqtor preflight proof",
    MCP_CONTRACT_SCHEMA_EXTENSION: MCP_CONTRACT_SCHEMA_VERSION,
    "type": "object",
    "description": (
        "Hash-bound proof returned by preflight_edits for the exact source, "
        "edit payload, author, producer build and candidate bytes."
    ),
    "properties": {
        "schema_version": {"const": "preflight_proof.v1"},
        "source_sha256": _SHA256,
        "edits_sha256": _SHA256,
        "tracked_change_author": _NONEMPTY_STRING,
        "producer_build": _NONEMPTY_STRING,
        "candidate_sha256": _SHA256,
        "proof_sha256": _SHA256,
    },
    "required": [
        "schema_version",
        "source_sha256",
        "edits_sha256",
        "tracked_change_author",
        "producer_build",
        "candidate_sha256",
        "proof_sha256",
    ],
    "additionalProperties": False,
}

AnchorInput = Annotated[dict[str, Any], WithJsonSchema(ANCHOR_INPUT_SCHEMA)]
EditInput = Annotated[dict[str, Any], WithJsonSchema(EDIT_INPUT_SCHEMA)]
PreflightProofInput = Annotated[
    dict[str, Any], WithJsonSchema(PREFLIGHT_PROOF_SCHEMA)
]


_RECORD_METADATA_PROPERTIES: dict[str, Any] = {
    "record_id": {
        "anyOf": [
            {"type": "string", "pattern": "^dr_[0-9]+$"},
            {"type": "null"},
        ]
    },
    "record_status": {"enum": ["written", "disabled", "write_failed"]},
}

_PRODUCER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"const": "veqtor-mcp"},
        "version": _NONEMPTY_STRING,
        "build": _NONEMPTY_STRING,
    },
    "required": ["name", "version", "build"],
    "additionalProperties": False,
}

_CLAUSE_ANCHOR_SCHEMA: dict[str, Any] = {
    "anyOf": [
        {
            "type": "object",
            "properties": {
                "label": _NULLABLE_STRING,
                "heading": _NULLABLE_STRING,
            },
            "required": ["label", "heading"],
            "additionalProperties": False,
        },
        {"type": "null"},
    ]
}

_ROUND_TRIP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"enum": ["passed", "failed"]},
        "comparison": _NONEMPTY_STRING,
        "collateral_changes": {"type": "array", "items": {}},
    },
    "required": ["status", "comparison", "collateral_changes"],
    "additionalProperties": True,
}

_PREFLIGHT_DIAGNOSTIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "edit_index": _NONNEGATIVE_INTEGER,
        "change_unit_id": {
            "anyOf": [
                {"type": "string", "pattern": "^cu_[0-9]+$"},
                {"type": "null"},
            ]
        },
        "status": {
            "enum": ["applicable", "planned", "blocked", "not_evaluated"]
        },
        "operation": {
            "anyOf": [
                {"enum": ["replace", "delete", "counter", "reinstate"]},
                {"type": "null"},
            ]
        },
        "match_count": _NULLABLE_NONNEGATIVE_INTEGER,
        "target_author": _NULLABLE_STRING,
        "target_revision_ids": {"type": "array", "items": _STRING},
        "position_status": {
            "enum": ["supported", "unsupported", "not_evaluated"]
        },
        "refusal_code": _NULLABLE_STRING,
    },
    "required": [
        "edit_index",
        "change_unit_id",
        "status",
        "operation",
        "match_count",
        "target_author",
        "target_revision_ids",
        "position_status",
        "refusal_code",
    ],
    "additionalProperties": True,
}


def _output_schema(
    title: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    """Build an additive top-level result contract with stable core fields."""
    return {
        "title": title,
        MCP_CONTRACT_SCHEMA_EXTENSION: MCP_CONTRACT_SCHEMA_VERSION,
        "type": "object",
        "properties": {**properties, **_RECORD_METADATA_PROPERTIES},
        "required": [*required, "record_id", "record_status"],
        # Result payloads are intentionally additive across compatible releases.
        "additionalProperties": True,
    }


LIST_ROUNDS_RESULT_SCHEMA = _output_schema(
    "list_rounds result",
    {
        "folder": _NONEMPTY_STRING,
        "ordering_source": {
            "enum": [
                "filename_lexicographic_v1",
                "explicit_filename_sequence_v1",
            ]
        },
        "order_basis": {
            "type": "object",
            "properties": {
                "kind": {
                    "enum": ["filename", "caller_supplied_filename_sequence"]
                },
                "rule": _NONEMPTY_STRING,
                "lineage_verified": {"const": False},
                "round_id_semantics": {"const": "position_only"},
            },
            "required": ["kind", "lineage_verified", "round_id_semantics"],
            "additionalProperties": True,
        },
        "rounds": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "round_id": {
                        "type": "string",
                        "pattern": "^round-[0-9]+$",
                    },
                    "path": _NONEMPTY_STRING,
                    "filename": _NONEMPTY_STRING,
                    "sha256": _SHA256,
                    "revision_count": _NONNEGATIVE_INTEGER,
                },
                "required": [
                    "round_id",
                    "path",
                    "filename",
                    "sha256",
                    "revision_count",
                ],
                "additionalProperties": True,
            },
        },
        "skipped": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "filename": _NONEMPTY_STRING,
                    "reason": _NONEMPTY_STRING,
                },
                "required": ["filename", "reason"],
                "additionalProperties": False,
            },
        },
    },
    ["folder", "ordering_source", "order_basis", "rounds", "skipped"],
)

_CHANGE_UNIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "change_unit_id": {"type": "string", "pattern": "^cu_[0-9]+$"},
        "file_sha256": _SHA256,
        "change_type": {
            "enum": ["insert", "delete", "replace", "counter"]
        },
        "author": _STRING,
        "date": _NULLABLE_STRING,
        "clause_anchor": _CLAUSE_ANCHOR_SCHEMA,
        "paragraph_context": {
            "type": "object",
            "properties": {
                "before": _STRING,
                "after": _STRING,
                "manual_label": _NULLABLE_STRING,
                "truncated_before": {"type": "boolean"},
                "truncated_after": {"type": "boolean"},
            },
            "required": [
                "before",
                "after",
                "manual_label",
                "truncated_before",
                "truncated_after",
            ],
            "additionalProperties": False,
        },
        "old_text": _NULLABLE_STRING,
        "new_text": _NULLABLE_STRING,
        "reference": {
            "type": "object",
            "properties": {
                "path": _NONEMPTY_STRING,
                "part_name": _NONEMPTY_STRING,
                "paragraph_index": _NONNEGATIVE_INTEGER,
                "group_index": _NONNEGATIVE_INTEGER,
                "revision_ids": {"type": "array", "items": _STRING},
            },
            "required": [
                "path",
                "part_name",
                "paragraph_index",
                "group_index",
                "revision_ids",
            ],
            "additionalProperties": False,
        },
        "countered_by": {"type": "array", "items": _STRING},
    },
    "required": [
        "change_unit_id",
        "file_sha256",
        "change_type",
        "author",
        "date",
        "clause_anchor",
        "paragraph_context",
        "old_text",
        "new_text",
        "reference",
    ],
    "additionalProperties": True,
}

EXTRACT_REDLINES_RESULT_SCHEMA = _output_schema(
    "extract_redlines result",
    {
        "path": _NONEMPTY_STRING,
        "file_sha256": _SHA256,
        "part_name": _NONEMPTY_STRING,
        "revision_count": _NONNEGATIVE_INTEGER,
        "change_units": {"type": "array", "items": _CHANGE_UNIT_SCHEMA},
        "unsupported_revisions": {
            "type": "object",
            "additionalProperties": _NONNEGATIVE_INTEGER,
        },
        "revision_inventory": {
            "type": "object",
            "properties": {
                "schema_version": {"const": "revision_inventory.v1"},
                "scope": _NONEMPTY_STRING,
                "total_revision_elements": _NONNEGATIVE_INTEGER,
                "decoded_revision_elements": _NONNEGATIVE_INTEGER,
                "unsupported_revision_occurrences": _NONNEGATIVE_INTEGER,
                "unsupported_revision_kind_count": _NONNEGATIVE_INTEGER,
                "emitted_change_unit_count": _NONNEGATIVE_INTEGER,
                "unsupported_by_kind": {
                    "type": "object",
                    "additionalProperties": _NONNEGATIVE_INTEGER,
                },
                "partition_valid": {"type": "boolean"},
                "all_revision_elements_decoded": {"type": "boolean"},
            },
            "required": [
                "schema_version",
                "scope",
                "total_revision_elements",
                "decoded_revision_elements",
                "unsupported_revision_occurrences",
                "unsupported_revision_kind_count",
                "emitted_change_unit_count",
                "unsupported_by_kind",
                "partition_valid",
                "all_revision_elements_decoded",
            ],
            "additionalProperties": True,
        },
    },
    [
        "path",
        "file_sha256",
        "part_name",
        "revision_count",
        "change_units",
        "unsupported_revisions",
        "revision_inventory",
    ],
)

PREFLIGHT_EDITS_RESULT_SCHEMA = _output_schema(
    "preflight_edits result",
    {
        "status": {"const": "ok"},
        "source_path": _NULLABLE_STRING,
        "source_sha256": _NULLABLE_SHA256,
        "tracked_change_author": _NULLABLE_STRING,
        "batch_applicable": {"type": "boolean"},
        "candidate_sha256": _NULLABLE_SHA256,
        "observed_candidate_sha256": _NULLABLE_SHA256,
        "blocking_edit_index": _NULLABLE_NONNEGATIVE_INTEGER,
        "refusal_code": _NULLABLE_STRING,
        "failure_phase": _NULLABLE_FAILURE_PHASE,
        "reason": _NULLABLE_STRING,
        "edits": {
            "type": "array",
            "items": _PREFLIGHT_DIAGNOSTIC_SCHEMA,
        },
        "round_trip_check": {
            "anyOf": [_ROUND_TRIP_SCHEMA, {"type": "null"}]
        },
        "preflight_proof": {
            "anyOf": [PREFLIGHT_PROOF_SCHEMA, {"type": "null"}]
        },
        "producer": _PRODUCER_SCHEMA,
    },
    [
        "status",
        "source_path",
        "source_sha256",
        "tracked_change_author",
        "batch_applicable",
        "candidate_sha256",
        "observed_candidate_sha256",
        "blocking_edit_index",
        "refusal_code",
        "failure_phase",
        "reason",
        "edits",
        "round_trip_check",
        "preflight_proof",
        "producer",
    ],
)

APPLY_EDITS_RESULT_SCHEMA = _output_schema(
    "apply_edits result",
    {
        "status": {"const": "ok"},
        "source_sha256": _SHA256,
        "output_path": _NONEMPTY_STRING,
        "output_sha256": _SHA256,
        "tracked_change_author": _NONEMPTY_STRING,
        "applied": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "change_unit_id": {
                        "type": "string",
                        "pattern": "^cu_[0-9]+$",
                    },
                    "operation": {
                        "enum": ["replace", "delete", "counter", "reinstate"]
                    },
                    "deleted_text": _NULLABLE_STRING,
                    "inserted_text": _NULLABLE_STRING,
                    "tracked_revision_ids": {
                        "type": "array",
                        "items": _STRING,
                    },
                },
                "required": [
                    "change_unit_id",
                    "operation",
                    "deleted_text",
                    "inserted_text",
                    "tracked_revision_ids",
                ],
                "additionalProperties": True,
            },
        },
        "round_trip_check": _ROUND_TRIP_SCHEMA,
        "preflight_binding_status": {"const": "verified"},
        "preflight_candidate_sha256": _SHA256,
        "candidate_output_sha256_match": {"const": True},
        "producer": _PRODUCER_SCHEMA,
    },
    [
        "status",
        "source_sha256",
        "output_path",
        "output_sha256",
        "tracked_change_author",
        "applied",
        "round_trip_check",
        "preflight_binding_status",
        "preflight_candidate_sha256",
        "candidate_output_sha256_match",
        "producer",
    ],
)

_VERIFY_MATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": _NONEMPTY_STRING,
        "part_name": _NONEMPTY_STRING,
        "revision_ids": {"type": "array", "items": _STRING},
        "clause": _NULLABLE_STRING,
        "side": {"enum": ["old", "new"]},
    },
    "required": ["path", "part_name", "revision_ids", "clause", "side"],
    "additionalProperties": False,
}

VERIFY_QUOTE_RESULT_SCHEMA = _output_schema(
    "verify_quote result",
    {
        "verdict": {"enum": ["exact", "normalized", "not_found"]},
        "exact": {"type": "boolean"},
        "checked_anchor": ANCHOR_INPUT_SCHEMA,
        "matches": {"type": "array", "items": _VERIFY_MATCH_SCHEMA},
        "diff": {"type": "array", "items": _STRING},
    },
    ["verdict", "exact", "checked_anchor", "matches", "diff"],
)

_PATH_DIGEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sha256": _SHA256,
        "omitted": {"const": True},
    },
    "required": ["sha256", "omitted"],
    "additionalProperties": False,
}

_COMPACT_RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": _NONEMPTY_STRING,
        "record_type": _NONEMPTY_STRING,
        "record_id": {"type": "string", "pattern": "^dr_[0-9]+$"},
        "tool_name": _NONEMPTY_STRING,
        "workspace": _PATH_DIGEST_SCHEMA,
        "producer": {"type": "object", "additionalProperties": True},
        "input": _PATH_DIGEST_SCHEMA,
        "result": {"type": "object", "additionalProperties": True},
        "result_sha256": _SHA256,
        "tool_result_sha256": _SHA256,
        "provenance": {"type": "object", "additionalProperties": True},
        "payloads": {"const": "compact"},
        "created_at": _NONEMPTY_STRING,
    },
    "required": [
        "schema_version",
        "record_type",
        "record_id",
        "tool_name",
        "workspace",
        "producer",
        "input",
        "result",
        "result_sha256",
        "tool_result_sha256",
        "provenance",
        "payloads",
        "created_at",
    ],
    "additionalProperties": True,
}

EXPORT_DECISION_RECORD_RESULT_SCHEMA = _output_schema(
    "export_decision_record result",
    {
        "workspace": _PATH_DIGEST_SCHEMA,
        "total_count": _NONNEGATIVE_INTEGER,
        "access_count": _NONNEGATIVE_INTEGER,
        "returned_count": _NONNEGATIVE_INTEGER,
        "truncated": {"type": "boolean"},
        "next_before_record_id": {
            "anyOf": [
                {"type": "string", "pattern": "^dr_[0-9]+$"},
                {"type": "null"},
            ]
        },
        "records": {"type": "array", "items": _COMPACT_RECORD_SCHEMA},
        "payloads": {"const": "compact"},
        "assurance": {"type": "object", "additionalProperties": True},
        "records_scope": _NONEMPTY_STRING,
        "total_count_scope": _NONEMPTY_STRING,
        "access_events_recorded_locally": {"type": "boolean"},
        "access_events_in_records": {"const": False},
        "access_count_scope": _NONEMPTY_STRING,
        "access_count_includes_current_export": {"const": False},
        "current_export_event": {
            "type": "object",
            "properties": {
                "record_id": {
                    "anyOf": [
                        {"type": "string", "pattern": "^dr_[0-9]+$"},
                        {"type": "null"},
                    ]
                },
                "record_type": {"const": "access_event.v1"},
                "record_status": {
                    "enum": ["written", "disabled", "write_failed"]
                },
                "recorded_locally": {"type": "boolean"},
                "included_in_records": {"const": False},
                "included_in_total_count": {"const": False},
                "included_in_access_count": {"const": False},
            },
            "required": [
                "record_id",
                "record_type",
                "record_status",
                "recorded_locally",
                "included_in_records",
                "included_in_total_count",
                "included_in_access_count",
            ],
            "additionalProperties": False,
        },
    },
    [
        "workspace",
        "total_count",
        "access_count",
        "returned_count",
        "truncated",
        "next_before_record_id",
        "records",
        "payloads",
        "assurance",
        "records_scope",
        "total_count_scope",
        "access_events_recorded_locally",
        "access_events_in_records",
        "access_count_scope",
        "access_count_includes_current_export",
        "current_export_event",
    ],
)

class _ContractResult(BaseModel):
    """Additive output model that preserves every key returned by tool code."""

    model_config = ConfigDict(extra="allow")
    contract_schema: ClassVar[dict[str, Any]]
    record_id: str | None
    record_status: Literal["written", "disabled", "write_failed"]

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        # Run the handler so Pydantic completes its normal schema bookkeeping,
        # then advertise the exact versioned transport contract. Validation at
        # the boundary still requires an object and preserves all additive keys.
        handler(core_schema)
        return deepcopy(cls.contract_schema)


class ListRoundsResult(_ContractResult):
    contract_schema = LIST_ROUNDS_RESULT_SCHEMA
    folder: str
    ordering_source: str
    order_basis: dict[str, Any]
    rounds: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


class ExtractRedlinesResult(_ContractResult):
    contract_schema = EXTRACT_REDLINES_RESULT_SCHEMA
    path: str
    file_sha256: str
    part_name: str
    revision_count: int
    change_units: list[dict[str, Any]]
    unsupported_revisions: dict[str, int]
    revision_inventory: dict[str, Any]


class PreflightEditsResult(_ContractResult):
    contract_schema = PREFLIGHT_EDITS_RESULT_SCHEMA
    status: Literal["ok"]
    source_path: str | None
    source_sha256: str | None
    tracked_change_author: str | None
    batch_applicable: bool
    candidate_sha256: str | None
    observed_candidate_sha256: str | None
    blocking_edit_index: int | None
    refusal_code: str | None
    failure_phase: str | None
    reason: str | None
    edits: list[dict[str, Any]]
    round_trip_check: dict[str, Any] | None
    preflight_proof: dict[str, Any] | None
    producer: dict[str, str]


class ApplyEditsResult(_ContractResult):
    contract_schema = APPLY_EDITS_RESULT_SCHEMA
    status: Literal["ok"]
    source_sha256: str
    output_path: str
    output_sha256: str
    tracked_change_author: str
    applied: list[dict[str, Any]]
    round_trip_check: dict[str, Any]
    preflight_binding_status: Literal["verified"]
    preflight_candidate_sha256: str
    candidate_output_sha256_match: Literal[True]
    producer: dict[str, str]


class VerifyQuoteResult(_ContractResult):
    contract_schema = VERIFY_QUOTE_RESULT_SCHEMA
    verdict: Literal["exact", "normalized", "not_found"]
    exact: bool
    checked_anchor: dict[str, Any]
    matches: list[dict[str, Any]]
    diff: list[str]


class ExportDecisionRecordResult(_ContractResult):
    contract_schema = EXPORT_DECISION_RECORD_RESULT_SCHEMA
    workspace: dict[str, Any]
    total_count: int
    access_count: int
    returned_count: int
    truncated: bool
    next_before_record_id: str | None
    records: list[dict[str, Any]]
    payloads: Literal["compact"]
    assurance: dict[str, Any]
    records_scope: str
    total_count_scope: str
    access_events_recorded_locally: bool
    access_events_in_records: Literal[False]
    access_count_scope: str
    access_count_includes_current_export: Literal[False]
    current_export_event: dict[str, Any]
