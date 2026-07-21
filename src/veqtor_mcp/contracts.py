# SPDX-License-Identifier: Apache-2.0
"""Versioned JSON contracts advertised by the Veqtor MCP tools.

The public Python helpers intentionally continue to accept and return ordinary
``dict`` objects.  ``WithJsonSchema`` lets the MCP boundary advertise a useful,
closed schema without converting those dictionaries into Pydantic model
instances for direct Python callers.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Annotated, Any, ClassVar, Literal

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, WithJsonSchema, model_validator
from veqtor_docx.contracts import (
    INSPECT_CONTAINER_POLICY_V1,
    INSPECT_FIXED_LIMITS_V1,
    INSPECT_INCLUDED_CONTAINERS_V1,
    INSPECT_INCLUDED_PARTS_V1,
    INSPECT_MATCH_BASES_V1,
    INSPECT_MODES_V1,
    INSPECT_READING_MODE_V1,
    INSPECT_SEARCH_SCOPE_V1,
    REVISION_COUNT_BASIS_V1,
)

MCP_CONTRACT_SCHEMA_VERSION = "veqtor.mcp.v0.3"
MCP_CONTRACT_META_KEY = "veqtor.pro/contractSchemaVersion"
MCP_CONTRACT_SCHEMA_EXTENSION = "x-veqtor-contract-schema-version"
RECORD_ID_PATTERN = r"^dr_[0-9]+(?![\s\S])"
RECORD_ERROR_PATTERN = r"^[a-z][a-z0-9_]{0,63}(?![\s\S])"
RECORD_ERROR_MAX_LENGTH = 64
_RECORD_ID_RE = re.compile(RECORD_ID_PATTERN, flags=re.ASCII)
_RECORD_ERROR_RE = re.compile(RECORD_ERROR_PATTERN, flags=re.ASCII)


def is_record_id(value: object) -> bool:
    """Return whether value belongs to the exact public record-id domain."""
    return isinstance(value, str) and _RECORD_ID_RE.fullmatch(value) is not None


def is_record_error(value: object) -> bool:
    """Return whether value is one bounded path-free record error code."""
    return (
        isinstance(value, str)
        and len(value) <= RECORD_ERROR_MAX_LENGTH
        and _RECORD_ERROR_RE.fullmatch(value) is not None
    )


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
_SHA256 = {
    "type": "string",
    "minLength": 64,
    "maxLength": 64,
    "pattern": r"^[0-9a-f]{64}(?![\s\S])",
}
_NULLABLE_SHA256 = {"anyOf": [_SHA256, {"type": "null"}]}
_NONNEGATIVE_INTEGER = {"type": "integer", "minimum": 0}
_NULLABLE_NONNEGATIVE_INTEGER = {"anyOf": [_NONNEGATIVE_INTEGER, {"type": "null"}]}
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


_LEGACY_CHANGE_UNIT_ANCHOR_SCHEMA: dict[str, Any] = {
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

_CHANGE_UNIT_ANCHOR_V2_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"const": "change_unit_anchor.v2"},
        "change_unit_id": {
            "type": "string",
            "pattern": "^cu_[0-9]+$",
        },
        "file_sha256": _SHA256,
        "container_policy": {"const": INSPECT_CONTAINER_POLICY_V1},
        "unit_fingerprint_sha256": _SHA256,
    },
    "required": [
        "schema_version",
        "change_unit_id",
        "file_sha256",
        "container_policy",
        "unit_fingerprint_sha256",
    ],
    "additionalProperties": False,
}

ANCHOR_INPUT_SCHEMA: dict[str, Any] = {
    "title": "Veqtor change-unit anchor",
    MCP_CONTRACT_SCHEMA_EXTENSION: MCP_CONTRACT_SCHEMA_VERSION,
    "oneOf": [
        _LEGACY_CHANGE_UNIT_ANCHOR_SCHEMA,
        _CHANGE_UNIT_ANCHOR_V2_SCHEMA,
    ],
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
PreflightProofInput = Annotated[dict[str, Any], WithJsonSchema(PREFLIGHT_PROOF_SCHEMA)]


_INSPECT_POLICY_PROPERTIES: dict[str, Any] = {
    "file_sha256": _SHA256,
    "part_name": {"const": "word/document.xml"},
    "reading_mode": {"const": INSPECT_READING_MODE_V1},
    "container_policy": {"const": INSPECT_CONTAINER_POLICY_V1},
}

PARAGRAPH_REF_SCHEMA: dict[str, Any] = {
    "title": "Veqtor paragraph reference",
    MCP_CONTRACT_SCHEMA_EXTENSION: MCP_CONTRACT_SCHEMA_VERSION,
    "type": "object",
    "properties": {
        **_INSPECT_POLICY_PROPERTIES,
        "schema_version": {"const": "paragraph_ref.v1"},
        "ref_type": {"const": "paragraph"},
        "paragraph_index": _NONNEGATIVE_INTEGER,
        "paragraph_text_sha256": _SHA256,
    },
    "required": [
        "file_sha256",
        "schema_version",
        "ref_type",
        "part_name",
        "paragraph_index",
        "paragraph_text_sha256",
        "reading_mode",
        "container_policy",
    ],
    "additionalProperties": False,
}

VERIFY_ANCHOR_INPUT_SCHEMA: dict[str, Any] = {
    "title": "Veqtor quote-verification anchor",
    MCP_CONTRACT_SCHEMA_EXTENSION: MCP_CONTRACT_SCHEMA_VERSION,
    "oneOf": [
        _LEGACY_CHANGE_UNIT_ANCHOR_SCHEMA,
        _CHANGE_UNIT_ANCHOR_V2_SCHEMA,
        PARAGRAPH_REF_SCHEMA,
    ],
}

VerifyAnchorInput = Annotated[
    dict[str, Any], WithJsonSchema(VERIFY_ANCHOR_INPUT_SCHEMA)
]

SECTION_REF_SCHEMA: dict[str, Any] = {
    "title": "Veqtor structural section reference",
    MCP_CONTRACT_SCHEMA_EXTENSION: MCP_CONTRACT_SCHEMA_VERSION,
    "type": "object",
    "properties": {
        **_INSPECT_POLICY_PROPERTIES,
        "schema_version": {"const": "section_ref.v1"},
        "ref_type": {"const": "section"},
        "heading_paragraph_index": _NONNEGATIVE_INTEGER,
        "heading_text_sha256": {
            **_SHA256,
            "description": (
                "Hash of the heading text, not a unique section identifier. "
                "Section identity is the complete position- and file-bound "
                "section_ref.v1 object."
            ),
        },
    },
    "required": [
        "file_sha256",
        "schema_version",
        "ref_type",
        "part_name",
        "heading_paragraph_index",
        "heading_text_sha256",
        "reading_mode",
        "container_policy",
    ],
    "additionalProperties": False,
}

INSPECT_SELECTION_SCHEMA: dict[str, Any] = {
    "title": "Veqtor document-inspection selection",
    MCP_CONTRACT_SCHEMA_EXTENSION: MCP_CONTRACT_SCHEMA_VERSION,
    "type": "object",
    "properties": {
        "paragraph_ref": PARAGRAPH_REF_SCHEMA,
        "section_ref": SECTION_REF_SCHEMA,
    },
    "oneOf": [
        {
            "required": ["paragraph_ref"],
            "not": {"required": ["section_ref"]},
        },
        {
            "required": ["section_ref"],
            "not": {"required": ["paragraph_ref"]},
        },
    ],
    "additionalProperties": False,
}

InspectSelectionInput = Annotated[
    dict[str, Any], WithJsonSchema(INSPECT_SELECTION_SCHEMA)
]


_RECORD_METADATA_PROPERTIES: dict[str, Any] = {
    "record_id": {
        "anyOf": [
            {"type": "string", "pattern": RECORD_ID_PATTERN},
            {"type": "null"},
        ]
    },
    "record_status": {"enum": ["written", "disabled", "write_failed"]},
    "record_error": {
        "type": "string",
        "minLength": 1,
        "maxLength": RECORD_ERROR_MAX_LENGTH,
        "pattern": RECORD_ERROR_PATTERN,
    },
}

_RECORD_METADATA_TUPLE_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "properties": {
                "record_id": {
                    "type": "string",
                    "pattern": RECORD_ID_PATTERN,
                },
                "record_status": {"const": "written"},
            },
            "required": ["record_id", "record_status"],
            "not": {"required": ["record_error"]},
        },
        {
            "properties": {
                "record_id": {"type": "null"},
                "record_status": {"const": "disabled"},
            },
            "required": ["record_id", "record_status"],
            "not": {"required": ["record_error"]},
        },
        {
            "properties": {
                "record_id": {"type": "null"},
                "record_status": {"const": "write_failed"},
            },
            "required": ["record_id", "record_status", "record_error"],
        },
    ]
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
        "status": {"enum": ["applicable", "planned", "blocked", "not_evaluated"]},
        "operation": {
            "anyOf": [
                {"enum": ["replace", "delete", "counter", "reinstate"]},
                {"type": "null"},
            ]
        },
        "match_count": _NULLABLE_NONNEGATIVE_INTEGER,
        "target_author": _NULLABLE_STRING,
        "target_revision_ids": {"type": "array", "items": _STRING},
        "position_status": {"enum": ["supported", "unsupported", "not_evaluated"]},
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
        "properties": {
            **properties,
            "producer": _PRODUCER_SCHEMA,
            **_RECORD_METADATA_PROPERTIES,
        },
        "required": [*required, "producer", "record_id", "record_status"],
        "allOf": [_RECORD_METADATA_TUPLE_SCHEMA],
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
                "kind": {"enum": ["filename", "caller_supplied_filename_sequence"]},
                "rule": _NONEMPTY_STRING,
                "lineage_verified": {"const": False},
                "round_id_semantics": {"const": "position_only"},
            },
            "required": ["kind", "lineage_verified", "round_id_semantics"],
            "additionalProperties": True,
        },
        "revision_count_basis": {"const": REVISION_COUNT_BASIS_V1},
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
    [
        "folder",
        "ordering_source",
        "order_basis",
        "revision_count_basis",
        "rounds",
        "skipped",
    ],
)

_CHANGE_UNIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "change_unit_id": {"type": "string", "pattern": "^cu_[0-9]+$"},
        "file_sha256": _SHA256,
        "change_type": {"enum": ["insert", "delete", "replace", "counter"]},
        "author": _STRING,
        "date": _NULLABLE_STRING,
        "anchor": _CHANGE_UNIT_ANCHOR_V2_SCHEMA,
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
                "container_kind": {"enum": ["body", "table_cell"]},
                "group_index": _NONNEGATIVE_INTEGER,
                "revision_ids": {"type": "array", "items": _STRING},
            },
            "required": [
                "path",
                "part_name",
                "paragraph_index",
                "container_kind",
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
        "anchor",
        "clause_anchor",
        "paragraph_context",
        "old_text",
        "new_text",
        "reference",
    ],
    "additionalProperties": True,
}

_CONTAINER_COVERAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"const": INSPECT_CONTAINER_POLICY_V1},
        "indexed_paragraph_count": _NONNEGATIVE_INTEGER,
        "body_paragraph_count": _NONNEGATIVE_INTEGER,
        "table_cell_paragraph_count": _NONNEGATIVE_INTEGER,
        "excluded_subtree_count": _NONNEGATIVE_INTEGER,
        "excluded_paragraph_count": _NONNEGATIVE_INTEGER,
        "excluded_by_kind": {
            "type": "object",
            "additionalProperties": _NONNEGATIVE_INTEGER,
        },
        "excluded_paragraphs_by_kind": {
            "type": "object",
            "additionalProperties": _NONNEGATIVE_INTEGER,
        },
        "coverage_complete": {"type": "boolean"},
        "legacy_two_field_anchor_safe": {"type": "boolean"},
    },
    "required": [
        "schema_version",
        "indexed_paragraph_count",
        "body_paragraph_count",
        "table_cell_paragraph_count",
        "excluded_subtree_count",
        "excluded_paragraph_count",
        "excluded_by_kind",
        "excluded_paragraphs_by_kind",
        "coverage_complete",
        "legacy_two_field_anchor_safe",
    ],
    "additionalProperties": False,
}

_REVISION_INVENTORY_V2_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"const": "revision_inventory.v2"},
        "scope": {"const": "word/document.xml"},
        "container_policy": _CONTAINER_COVERAGE_SCHEMA,
        "tracked_text_revision_elements": _NONNEGATIVE_INTEGER,
        "total_revision_elements": _NONNEGATIVE_INTEGER,
        "in_scope_revision_elements": _NONNEGATIVE_INTEGER,
        "decoded_revision_elements": _NONNEGATIVE_INTEGER,
        "unsupported_revision_occurrences": _NONNEGATIVE_INTEGER,
        "unsupported_revision_kind_count": _NONNEGATIVE_INTEGER,
        "excluded_container_occurrences": _NONNEGATIVE_INTEGER,
        "excluded_container_kind_count": _NONNEGATIVE_INTEGER,
        "unsupported_by_kind": {
            "type": "object",
            "additionalProperties": _NONNEGATIVE_INTEGER,
        },
        "excluded_by_container": {
            "type": "object",
            "additionalProperties": _NONNEGATIVE_INTEGER,
        },
        "partition_valid": {"type": "boolean"},
        "all_in_scope_revision_elements_decoded": {"type": "boolean"},
        "all_revision_elements_decoded": {"type": "boolean"},
        "emitted_change_unit_count": _NONNEGATIVE_INTEGER,
    },
    "required": [
        "schema_version",
        "scope",
        "container_policy",
        "tracked_text_revision_elements",
        "total_revision_elements",
        "in_scope_revision_elements",
        "decoded_revision_elements",
        "unsupported_revision_occurrences",
        "unsupported_revision_kind_count",
        "excluded_container_occurrences",
        "excluded_container_kind_count",
        "unsupported_by_kind",
        "excluded_by_container",
        "partition_valid",
        "all_in_scope_revision_elements_decoded",
        "all_revision_elements_decoded",
    ],
    "additionalProperties": False,
}

EXTRACT_REDLINES_RESULT_SCHEMA = _output_schema(
    "extract_redlines result",
    {
        "path": _NONEMPTY_STRING,
        "file_sha256": _SHA256,
        "part_name": _NONEMPTY_STRING,
        "revision_count": _NONNEGATIVE_INTEGER,
        "revision_count_basis": {"const": REVISION_COUNT_BASIS_V1},
        "change_units": {"type": "array", "items": _CHANGE_UNIT_SCHEMA},
        "unsupported_revisions": {
            "type": "object",
            "additionalProperties": _NONNEGATIVE_INTEGER,
        },
        "revision_inventory": _REVISION_INVENTORY_V2_SCHEMA,
    },
    [
        "path",
        "file_sha256",
        "part_name",
        "revision_count",
        "revision_count_basis",
        "change_units",
        "unsupported_revisions",
        "revision_inventory",
    ],
)

_INSPECT_NAVIGATION_SCHEMA: dict[str, Any] = {
    "anyOf": [
        {
            "type": "object",
            "properties": {
                "label": _NULLABLE_STRING,
                "heading": _NULLABLE_STRING,
                "level": _NONNEGATIVE_INTEGER,
                "basis": {"const": "word_outline_level_v1"},
                "label_basis": {
                    "anyOf": [
                        {
                            "enum": [
                                "word_numbering_v1",
                                "explicit_heading_text_v1",
                            ]
                        },
                        {"type": "null"},
                    ]
                },
            },
            "required": ["label", "heading", "level", "basis", "label_basis"],
            "additionalProperties": False,
        },
        {"type": "null"},
    ]
}

_INSPECT_PARAGRAPH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "paragraph_ref": PARAGRAPH_REF_SCHEMA,
        "container_kind": {"enum": ["body", "table_cell"]},
        "has_tracked_text_revisions": {"type": "boolean"},
        "section_navigation": _INSPECT_NAVIGATION_SCHEMA,
        "text": _STRING,
    },
    "required": [
        "paragraph_ref",
        "container_kind",
        "has_tracked_text_revisions",
        "section_navigation",
        "text",
    ],
    "additionalProperties": False,
}

_INSPECT_SECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "section_ref": SECTION_REF_SCHEMA,
        "label": _NULLABLE_STRING,
        "heading": _NULLABLE_STRING,
        "level": _NONNEGATIVE_INTEGER,
        "basis": {"const": "word_outline_level_v1"},
        "label_basis": {
            "anyOf": [
                {
                    "enum": [
                        "word_numbering_v1",
                        "explicit_heading_text_v1",
                    ]
                },
                {"type": "null"},
            ]
        },
        "start_paragraph_index": _NONNEGATIVE_INTEGER,
        "end_paragraph_index_exclusive": _NONNEGATIVE_INTEGER,
    },
    "required": [
        "section_ref",
        "label",
        "heading",
        "level",
        "basis",
        "label_basis",
        "start_paragraph_index",
        "end_paragraph_index_exclusive",
    ],
    "additionalProperties": False,
}

_INSPECT_MATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phrase_index": _NONNEGATIVE_INTEGER,
        "match_basis": {"enum": sorted(INSPECT_MATCH_BASES_V1)},
        "occurrence_count": {"type": "integer", "minimum": 1},
        "paragraph_ref": PARAGRAPH_REF_SCHEMA,
        "container_kind": {"enum": ["body", "table_cell"]},
        "has_tracked_text_revisions": {"type": "boolean"},
        "section_navigation": _INSPECT_NAVIGATION_SCHEMA,
        "snippet": {
            "type": "object",
            "description": (
                "Navigation-only context. Even an untruncated snippet is not "
                "quotation evidence; use read and then verify_quote."
            ),
            "properties": {
                "text": _STRING,
                "match_start": _NONNEGATIVE_INTEGER,
                "match_end": _NONNEGATIVE_INTEGER,
                "truncated_before": {"type": "boolean"},
                "truncated_after": {"type": "boolean"},
            },
            "required": [
                "text",
                "match_start",
                "match_end",
                "truncated_before",
                "truncated_after",
            ],
            "additionalProperties": False,
        },
    },
    "required": [
        "phrase_index",
        "match_basis",
        "occurrence_count",
        "paragraph_ref",
        "container_kind",
        "has_tracked_text_revisions",
        "section_navigation",
        "snippet",
    ],
    "additionalProperties": False,
}

INSPECT_DOCUMENT_RESULT_SCHEMA = _output_schema(
    "inspect_document result",
    {
        "mode": {"enum": sorted(INSPECT_MODES_V1)},
        "path": _NONEMPTY_STRING,
        "file_sha256": _SHA256,
        "part_name": {"const": "word/document.xml"},
        "search_scope": {"const": INSPECT_SEARCH_SCOPE_V1},
        "reading_mode": {"const": INSPECT_READING_MODE_V1},
        "container_policy": {"const": INSPECT_CONTAINER_POLICY_V1},
        "has_tracked_text_revisions": {"type": "boolean"},
        "revision_inventory": _REVISION_INVENTORY_V2_SCHEMA,
        "coverage": {
            "type": "object",
            "properties": {
                "scan_complete": {"const": True},
                "indexed_paragraph_count": _NONNEGATIVE_INTEGER,
                "nonempty_indexed_paragraph_count": _NONNEGATIVE_INTEGER,
                "eligible_item_count": _NONNEGATIVE_INTEGER,
                "returned_item_count": _NONNEGATIVE_INTEGER,
                "cursor_offset": _NONNEGATIVE_INTEGER,
                "output_truncated": {"type": "boolean"},
                "complete_literal_match_count": {
                    "anyOf": [_NONNEGATIVE_INTEGER, {"type": "null"}]
                },
                "included_parts": {"const": list(INSPECT_INCLUDED_PARTS_V1)},
                "excluded_parts": {
                    "type": "array",
                    "items": _NONEMPTY_STRING,
                },
                "included_containers": {"const": list(INSPECT_INCLUDED_CONTAINERS_V1)},
                "container_coverage": _CONTAINER_COVERAGE_SCHEMA,
            },
            "required": [
                "scan_complete",
                "indexed_paragraph_count",
                "nonempty_indexed_paragraph_count",
                "eligible_item_count",
                "returned_item_count",
                "cursor_offset",
                "output_truncated",
                "complete_literal_match_count",
                "included_parts",
                "excluded_parts",
                "included_containers",
                "container_coverage",
            ],
            "additionalProperties": False,
        },
        "limits": {
            "type": "object",
            "properties": {
                "requested_max_items": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": INSPECT_FIXED_LIMITS_V1["max_items"],
                },
                **{
                    key: {"const": value}
                    for key, value in INSPECT_FIXED_LIMITS_V1.items()
                },
            },
            "required": [
                "requested_max_items",
                "max_items",
                "max_phrases",
                "max_phrase_chars",
                "max_total_phrase_chars",
                "max_paragraph_text_chars",
                "max_returned_text_chars",
                "max_indexed_paragraphs",
                "max_aggregate_text_chars",
                "max_literal_match_candidates",
                "max_literal_occurrences_per_candidate",
                "wall_clock_partial_results",
            ],
            "additionalProperties": False,
        },
        "next_cursor": {
            "anyOf": [
                {"type": "string", "pattern": "^c1:[0-9]+:[0-9a-f]{64}$"},
                {"type": "null"},
            ]
        },
        "sections": {"type": "array", "items": _INSPECT_SECTION_SCHEMA},
        "matches": {"type": "array", "items": _INSPECT_MATCH_SCHEMA},
        "paragraphs": {"type": "array", "items": _INSPECT_PARAGRAPH_SCHEMA},
        "match_basis": {"enum": sorted(INSPECT_MATCH_BASES_V1)},
        "phrase_count": _NONNEGATIVE_INTEGER,
        "selection_kind": {"enum": ["paragraph", "section"]},
        "section_navigation": _INSPECT_NAVIGATION_SCHEMA,
    },
    [
        "mode",
        "path",
        "file_sha256",
        "part_name",
        "search_scope",
        "reading_mode",
        "container_policy",
        "has_tracked_text_revisions",
        "revision_inventory",
        "coverage",
        "limits",
        "next_cursor",
    ],
)
INSPECT_DOCUMENT_RESULT_SCHEMA["oneOf"] = [
    {
        "properties": {"mode": {"const": "outline"}},
        "required": ["sections"],
        "not": {
            "anyOf": [
                {"required": [field]}
                for field in (
                    "matches",
                    "paragraphs",
                    "match_basis",
                    "phrase_count",
                    "selection_kind",
                    "section_navigation",
                )
            ]
        },
    },
    {
        "properties": {"mode": {"const": "literal_search"}},
        "required": ["matches", "match_basis", "phrase_count"],
        "not": {
            "anyOf": [
                {"required": [field]}
                for field in (
                    "sections",
                    "paragraphs",
                    "selection_kind",
                    "section_navigation",
                )
            ]
        },
    },
    {
        "properties": {"mode": {"const": "browse"}},
        "required": ["paragraphs"],
        "not": {
            "anyOf": [
                {"required": [field]}
                for field in (
                    "sections",
                    "matches",
                    "match_basis",
                    "phrase_count",
                    "selection_kind",
                    "section_navigation",
                )
            ]
        },
    },
    {
        "properties": {
            "mode": {"const": "read"},
            "selection_kind": {"const": "paragraph"},
        },
        "required": ["paragraphs", "selection_kind"],
        "not": {
            "anyOf": [
                {"required": [field]}
                for field in (
                    "sections",
                    "matches",
                    "match_basis",
                    "phrase_count",
                    "section_navigation",
                )
            ]
        },
    },
    {
        "properties": {
            "mode": {"const": "read"},
            "selection_kind": {"const": "section"},
        },
        "required": ["paragraphs", "selection_kind", "section_navigation"],
        "not": {
            "anyOf": [
                {"required": [field]}
                for field in (
                    "sections",
                    "matches",
                    "match_basis",
                    "phrase_count",
                )
            ]
        },
    },
]

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
        "round_trip_check": {"anyOf": [_ROUND_TRIP_SCHEMA, {"type": "null"}]},
        "preflight_proof": {"anyOf": [PREFLIGHT_PROOF_SCHEMA, {"type": "null"}]},
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
    ],
)

_VERIFY_CHANGE_UNIT_MATCH_SCHEMA: dict[str, Any] = {
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

_VERIFY_PARAGRAPH_MATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": _NONEMPTY_STRING,
        "part_name": {"const": "word/document.xml"},
        "revision_ids": {"type": "array", "maxItems": 0},
        "clause": _NULLABLE_STRING,
        "side": {"const": "paragraph_current"},
        "paragraph_index": _NONNEGATIVE_INTEGER,
        "paragraph_text_sha256": _SHA256,
        "reading_mode": {"const": INSPECT_READING_MODE_V1},
    },
    "required": [
        "path",
        "part_name",
        "revision_ids",
        "clause",
        "side",
        "paragraph_index",
        "paragraph_text_sha256",
        "reading_mode",
    ],
    "additionalProperties": False,
}

_VERIFY_MATCH_SCHEMA: dict[str, Any] = {
    "oneOf": [
        _VERIFY_CHANGE_UNIT_MATCH_SCHEMA,
        _VERIFY_PARAGRAPH_MATCH_SCHEMA,
    ]
}

VERIFY_QUOTE_RESULT_SCHEMA = _output_schema(
    "verify_quote result",
    {
        "verdict": {"enum": ["exact", "normalized", "not_found"]},
        "exact": {"type": "boolean"},
        "checked_anchor": VERIFY_ANCHOR_INPUT_SCHEMA,
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
        "record_id": {"type": "string", "pattern": RECORD_ID_PATTERN},
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
                {"type": "string", "pattern": RECORD_ID_PATTERN},
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
                        {"type": "string", "pattern": RECORD_ID_PATTERN},
                        {"type": "null"},
                    ]
                },
                "record_type": {"const": "access_event.v1"},
                "record_status": {"enum": ["written", "disabled", "write_failed"]},
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
    producer: dict[str, str]
    record_id: str | None
    record_status: Literal["written", "disabled", "write_failed"]

    @model_validator(mode="after")
    def _validate_record_metadata_tuple(self) -> _ContractResult:
        extras = self.model_extra or {}
        has_error = "record_error" in extras
        if self.record_status == "written":
            valid = is_record_id(self.record_id) and not has_error
        elif self.record_status == "disabled":
            valid = self.record_id is None and not has_error
        else:
            valid = (
                self.record_id is None
                and has_error
                and is_record_error(extras.get("record_error"))
            )
        if not valid:
            raise ValueError("record metadata tuple is inconsistent")
        return self

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
    revision_count_basis: Literal[REVISION_COUNT_BASIS_V1]
    rounds: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


class ExtractRedlinesResult(_ContractResult):
    contract_schema = EXTRACT_REDLINES_RESULT_SCHEMA
    path: str
    file_sha256: str
    part_name: str
    revision_count: int
    revision_count_basis: Literal[REVISION_COUNT_BASIS_V1]
    change_units: list[dict[str, Any]]
    unsupported_revisions: dict[str, int]
    revision_inventory: dict[str, Any]


class InspectDocumentResult(_ContractResult):
    contract_schema = INSPECT_DOCUMENT_RESULT_SCHEMA
    mode: Literal["outline", "literal_search", "browse", "read"]
    path: str
    file_sha256: str
    part_name: Literal["word/document.xml"]
    search_scope: Literal["word_document_xml_body_v1"]
    reading_mode: Literal["accepted_current_v1"]
    container_policy: Literal["canonical_body_flow_v1"]
    has_tracked_text_revisions: bool
    revision_inventory: dict[str, Any]
    coverage: dict[str, Any]
    limits: dict[str, Any]
    next_cursor: str | None


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
