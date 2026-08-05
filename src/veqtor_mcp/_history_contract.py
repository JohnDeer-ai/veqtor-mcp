# SPDX-License-Identifier: Apache-2.0
"""Closed JSON Schemas for the internal Stage 3C paragraph-history contract.

The schemas in this module deliberately describe the path-bearing live
operation result returned by :mod:`veqtor_mcp._history_io`, before the MCP
boundary adds producer and decision-record metadata.  They are self-contained
so that the public contract module can import them without creating an import
cycle through the history implementation.
"""

from __future__ import annotations

from typing import Any


def _anchored(body: str) -> str:
    """Anchor one ECMAScript-compatible JSON Schema pattern absolutely."""
    return rf"^(?:{body})(?![\s\S])"


def _closed(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


_SHA256 = {
    "type": "string",
    "minLength": 64,
    "maxLength": 64,
    "pattern": _anchored(r"[0-9a-f]{64}"),
}
_NONNEGATIVE_INTEGER = {"type": "integer", "minimum": 0}
_NONEMPTY_STRING = {"type": "string", "minLength": 1}
_NULLABLE_STRING = {"type": ["string", "null"]}
_POSITION = {"type": "integer", "minimum": 0, "maximum": 499}
_PARAGRAPH_INDEX = {"type": "integer", "minimum": 0, "maximum": 9_999}
_FILENAME = {
    "type": "string",
    "minLength": 5,
    "pattern": _anchored(r"[^/]*[.][dD][oO][cC][xX]"),
}
_CURSOR = {
    "type": "string",
    "pattern": _anchored(
        r"ph1:(?:[1-9]|[1-9][0-9]|[1-4][0-9]{2}):[0-9a-f]{64}"
    ),
}
PARAGRAPH_HISTORY_CURSOR_SCHEMA = dict(_CURSOR)
PARAGRAPH_HISTORY_NULLABLE_CURSOR_SCHEMA = _nullable(_CURSOR)


def _identity(prefix: str) -> dict[str, Any]:
    return {
        "type": "string",
        "pattern": _anchored(rf"{prefix}:[0-9a-f]{{64}}"),
    }


_DOCUMENT_ID = _identity("rm_doc_v1")
_OBSERVATION_ID = _identity("rm_obs_v1")
_PARAGRAPH_ID = _identity("rm_par_v1")
_PARAGRAPH_OBSERVATION_ID = _identity("ph_par_obs_v1")
_RELATIONSHIP_ID = _identity("ph_rel_v1")
_SECTION_ID = _identity("rm_sec_v1")
_NAVIGATION_CANDIDATE_ID = _identity("ph_nav_v1")


_PARAGRAPH_REF_SCHEMA = _closed(
    {
        "schema_version": {"const": "paragraph_ref.v1"},
        "ref_type": {"const": "paragraph"},
        "file_sha256": _SHA256,
        "part_name": {"const": "word/document.xml"},
        "paragraph_index": _PARAGRAPH_INDEX,
        "paragraph_text_sha256": _SHA256,
        "reading_mode": {"const": "accepted_current_v1"},
        "container_policy": {"const": "canonical_body_flow_v1"},
    }
)

_SECTION_REF_SCHEMA = _closed(
    {
        "schema_version": {"const": "section_ref.v1"},
        "ref_type": {"const": "section"},
        "file_sha256": _SHA256,
        "part_name": {"const": "word/document.xml"},
        "heading_paragraph_index": _PARAGRAPH_INDEX,
        "heading_text_sha256": _SHA256,
        "reading_mode": {"const": "accepted_current_v1"},
        "container_policy": {"const": "canonical_body_flow_v1"},
    }
)


PARAGRAPH_HISTORY_SEED_SCHEMA: dict[str, Any] = {
    "title": "Veqtor paragraph-history seed",
    **_closed(
        {
            "schema_version": {"const": "paragraph_history_seed.v1"},
            "path": _NONEMPTY_STRING,
            "paragraph_ref": _PARAGRAPH_REF_SCHEMA,
        }
    ),
}

_LEXICOGRAPHIC_ORDER_INPUT = _closed(
    {
        "schema_version": {"const": "paragraph_history_order.v1"},
        "kind": {"const": "filename_lexicographic_v1"},
    }
)
_EXPLICIT_ORDER_INPUT = _closed(
    {
        "schema_version": {"const": "paragraph_history_order.v1"},
        "kind": {"const": "explicit_filename_sequence_v1"},
        "ordered_filenames": {
            "type": "array",
            "items": _FILENAME,
            "minItems": 1,
            "maxItems": 500,
            "uniqueItems": True,
        },
    }
)
PARAGRAPH_HISTORY_ORDER_SCHEMA: dict[str, Any] = {
    "title": "Veqtor paragraph-history order declaration",
    "oneOf": [_LEXICOGRAPHIC_ORDER_INPUT, _EXPLICIT_ORDER_INPUT],
}


_EXCLUSION_KIND_COUNTS = {
    "type": "object",
    "properties": {
        kind: _NONNEGATIVE_INTEGER
        for kind in (
            "alt_chunk",
            "alternate_content",
            "text_box",
            "nested_paragraph",
            "unknown_container",
        )
    },
    "additionalProperties": False,
}
_CONTAINER_COVERAGE = _closed(
    {
        "schema_version": {"const": "canonical_body_flow_v1"},
        "indexed_paragraph_count": _NONNEGATIVE_INTEGER,
        "body_paragraph_count": _NONNEGATIVE_INTEGER,
        "table_cell_paragraph_count": _NONNEGATIVE_INTEGER,
        "excluded_subtree_count": _NONNEGATIVE_INTEGER,
        "excluded_paragraph_count": _NONNEGATIVE_INTEGER,
        "excluded_by_kind": _EXCLUSION_KIND_COUNTS,
        "excluded_paragraphs_by_kind": _EXCLUSION_KIND_COUNTS,
        "coverage_complete": {"type": "boolean"},
        "legacy_two_field_anchor_safe": {"type": "boolean"},
    }
)
_INSPECTION_COVERAGE = _closed(
    {
        "schema_version": {"const": "round_map_inspection_coverage.v1"},
        "scan_complete": {"const": True},
        "indexed_paragraph_count": {
            "type": "integer",
            "minimum": 0,
            "maximum": 10_000,
        },
        "nonempty_indexed_paragraph_count": {
            "type": "integer",
            "minimum": 0,
            "maximum": 10_000,
        },
        "included_parts": {
            "type": "array",
            "prefixItems": [{"const": "word/document.xml"}],
            "minItems": 1,
            "maxItems": 1,
        },
        "excluded_parts": {"type": "array", "items": _NONEMPTY_STRING},
        "included_containers": {
            "type": "array",
            "prefixItems": [{"const": "body"}, {"const": "table_cell"}],
            "minItems": 2,
            "maxItems": 2,
        },
        "container_coverage": _CONTAINER_COVERAGE,
    }
)


_EXACT_CONTENT_EQUALITY_BASIS = _closed(
    {
        "schema_version": {"const": "exact_content_equality_basis.v1"},
        "reading_mode": {"const": "accepted_current_v1"},
        "container_policy": {"const": "canonical_body_flow_v1"},
        "part_name": {"const": "word/document.xml"},
        "comparison": {"const": "complete_unicode_scalar_sequence_v1"},
        "full_text_compared": {"const": True},
        "paragraph_text_sha256": _SHA256,
    }
)
_REJECTED_PROJECTION_EQUALITY_BASIS = _closed(
    {
        "schema_version": {"const": "rejected_projection_equality_basis.v1"},
        "evidence_class": {"const": "projection_text_equality_only"},
        "part_name": {"const": "word/document.xml"},
        "container_policy": {"const": "canonical_body_flow_v1"},
        "lower_observation_id": _OBSERVATION_ID,
        "higher_observation_id": _OBSERVATION_ID,
        "lower_paragraph_observation_id": _PARAGRAPH_OBSERVATION_ID,
        "higher_paragraph_observation_id": _PARAGRAPH_OBSERVATION_ID,
        "higher_position_projection": {
            "const": "pending_text_revisions_rejected_v1"
        },
        "lower_position_projection": {"const": "accepted_current_v1"},
        "lower_current_text_sha256": _SHA256,
        "higher_rejected_projection_text_sha256": _SHA256,
        "higher_current_text_sha256": _SHA256,
        "comparison": {"const": "sha256_then_full_unicode_equality_v1"},
        "full_text_compared": {"const": True},
        "semantic_identity": {"const": "not_claimed"},
        "direction_semantics": {"const": "projection_role_only"},
        "derivation_recorded": {"const": False},
        "lineage_verified": {"const": False},
        "chronology_verified": {"const": False},
        "authorship_verified": {"const": False},
        "time_verified": {"const": False},
    }
)


def _relationship_variant(
    relationship_type: str,
    basis: dict[str, Any],
) -> dict[str, Any]:
    return _closed(
        {
            "schema_version": {"const": "paragraph_history_relationship.v1"},
            "relationship_id": _RELATIONSHIP_ID,
            "relationship_type": {"const": relationship_type},
            "lower_position": _POSITION,
            "higher_position": _POSITION,
            "lower_observation_id": _OBSERVATION_ID,
            "higher_observation_id": _OBSERVATION_ID,
            "lower_paragraph_observation_id": _PARAGRAPH_OBSERVATION_ID,
            "higher_paragraph_observation_id": _PARAGRAPH_OBSERVATION_ID,
            "lower_paragraph_id": _PARAGRAPH_ID,
            "higher_paragraph_id": _PARAGRAPH_ID,
            "comparison_text_sha256": _SHA256,
            "basis": basis,
            "derivation_recorded": {"const": False},
            "lineage_verified": {"const": False},
            "chronology_verified": {"const": False},
            "authorship_verified": {"const": False},
            "time_verified": {"const": False},
            "semantic_identity": {"const": "not_claimed"},
        }
    )


_CURRENT_RELATIONSHIP = _relationship_variant(
    "exact_content_equality", _EXACT_CONTENT_EQUALITY_BASIS
)
_REJECTED_RELATIONSHIP = _relationship_variant(
    "rejected_projection_equality", _REJECTED_PROJECTION_EQUALITY_BASIS
)
_RELATIONSHIP = {"oneOf": [_CURRENT_RELATIONSHIP, _REJECTED_RELATIONSHIP]}


def _candidate_variant(
    evidence_type: str,
    relationship: dict[str, Any],
) -> dict[str, Any]:
    return _closed(
        {
            "paragraph_observation_id": _PARAGRAPH_OBSERVATION_ID,
            "paragraph_id": _PARAGRAPH_ID,
            "paragraph_ref": _PARAGRAPH_REF_SCHEMA,
            "evidence_type": {"const": evidence_type},
            "relationship": relationship,
        }
    )


_CANDIDATE = {
    "oneOf": [
        _candidate_variant("exact_content_equality", _CURRENT_RELATIONSHIP),
        _candidate_variant(
            "rejected_projection_equality", _REJECTED_RELATIONSHIP
        ),
    ]
}
_CANDIDATE_SUMMARY = _closed(
    {
        "schema_version": {"const": "paragraph_history_candidate_summary.v1"},
        "count": {"type": "integer", "minimum": 0, "maximum": 10_000},
        "sha256": _SHA256,
        "sample": {
            "type": "array",
            "items": _CANDIDATE,
            "maxItems": 20,
        },
        "truncated": {"type": "boolean"},
    }
)


_NAVIGATION_SIGNAL = _closed(
    {
        "kind": {"enum": ["label_exact_v1", "heading_exact_v1"]},
        "value_sha256": _SHA256,
    }
)
_NAVIGATION_BASIS = _closed(
    {
        "schema_version": {"const": "navigation_candidate_basis.v1"},
        "signals": {
            "type": "array",
            "items": _NAVIGATION_SIGNAL,
            "minItems": 1,
            "maxItems": 2,
            "uniqueItems": True,
        },
        "evidence_class": {"const": "navigation_only"},
    }
)
_NAVIGATION_CANDIDATE = _closed(
    {
        "schema_version": {
            "const": "paragraph_history_navigation_candidate.v1"
        },
        "navigation_candidate_id": _NAVIGATION_CANDIDATE_ID,
        "observation_id": _OBSERVATION_ID,
        "seed_section_id": _SECTION_ID,
        "candidate_section_id": _SECTION_ID,
        "section_ref": _SECTION_REF_SCHEMA,
        "label": _NULLABLE_STRING,
        "heading": _NULLABLE_STRING,
        "level": {"type": "integer", "minimum": 0, "maximum": 8},
        "outline_basis": {"const": "word_outline_level_v1"},
        "label_basis": _nullable(
            {"enum": ["word_numbering_v1", "explicit_heading_text_v1"]}
        ),
        "evidence_basis": _NAVIGATION_BASIS,
    }
)
_NAVIGATION_SUMMARY = _closed(
    {
        "schema_version": {
            "const": "paragraph_history_navigation_summary.v1"
        },
        "count": {"type": "integer", "minimum": 0, "maximum": 10_000},
        "sha256": _SHA256,
        "sample": {
            "type": "array",
            "items": _NAVIGATION_CANDIDATE,
            "maxItems": 20,
        },
        "truncated": {"type": "boolean"},
    }
)


_PROJECTION_COMMON = {
    "schema_version": {"const": "paragraph_projection.v1"},
    "mode": {"const": "pending_text_revisions_rejected_v1"},
}
_COMPLETE_REJECTED_PROJECTION = _closed(
    {
        **_PROJECTION_COMMON,
        "projection_status": {"const": "complete"},
        "unavailable_reasons": {"type": "array", "maxItems": 0},
        "text_state": {"enum": ["empty", "nonempty"]},
        "equals_current": {"type": "boolean"},
        "has_non_whitespace": {"type": "boolean"},
        "match_eligible": {"type": "boolean"},
        "projection_text_sha256": _SHA256,
        "text_length": {
            "type": "integer",
            "minimum": 0,
            "maximum": 50_000,
        },
        "text": {"type": "string", "maxLength": 50_000},
        "move_wrapper_visibility_applied": {"type": "boolean"},
        "move_pairing": {"const": "not_attempted"},
    }
)
_UNAVAILABLE_REJECTED_PROJECTION = _closed(
    {
        **_PROJECTION_COMMON,
        "projection_status": {"const": "unavailable"},
        "unavailable_reasons": {
            "type": "array",
            "items": {
                "enum": [
                    "stray_deleted_text",
                    "existence_affecting_revision",
                    "declared_scope_incomplete",
                ]
            },
            "minItems": 1,
            "maxItems": 3,
            "uniqueItems": True,
        },
        "text_state": {"type": "null"},
        "equals_current": {"type": "null"},
        "has_non_whitespace": {"const": False},
        "match_eligible": {"const": False},
        "projection_text_sha256": {"type": "null"},
        "text_length": {"type": "null"},
        "text": {"type": "null"},
        "move_wrapper_visibility_applied": {"const": False},
        "move_pairing": {"const": "not_attempted"},
    }
)
_REJECTED_PROJECTION = {
    "oneOf": [_COMPLETE_REJECTED_PROJECTION, _UNAVAILABLE_REJECTED_PROJECTION]
}

_CURRENT_PROJECTION = _closed(
    {
        "schema_version": {"const": "paragraph_current_projection.v1"},
        "mode": {"const": "accepted_current_v1"},
        "text_sha256": _SHA256,
        "text_length": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50_000,
        },
        "has_non_whitespace": {"const": True},
        "text": {"type": "string", "minLength": 1, "maxLength": 50_000},
    }
)

_CHANGE_UNIT_ANCHOR = _closed(
    {
        "schema_version": {"const": "change_unit_anchor.v2"},
        "change_unit_id": {
            "type": "string",
            "pattern": _anchored(r"cu_[0-9]+"),
        },
        "file_sha256": _SHA256,
        "container_policy": {"const": "canonical_body_flow_v1"},
        "unit_fingerprint_sha256": _SHA256,
    }
)
_CLAUSE_ANCHOR = _nullable(
    _closed(
        {
            "label": _NULLABLE_STRING,
            "heading": _NULLABLE_STRING,
        }
    )
)
_PARAGRAPH_CONTEXT = _closed(
    {
        "before": {"type": "string", "maxLength": 240},
        "after": {"type": "string", "maxLength": 240},
        "manual_label": _NULLABLE_STRING,
        "truncated_before": {"type": "boolean"},
        "truncated_after": {"type": "boolean"},
    }
)
_CHANGE_UNIT_REFERENCE = _closed(
    {
        "part_name": {"const": "word/document.xml"},
        "paragraph_index": _PARAGRAPH_INDEX,
        "container_kind": {"enum": ["body", "table_cell"]},
        "group_index": _NONNEGATIVE_INTEGER,
        "revision_ids": {
            "type": "array",
            "items": _NONEMPTY_STRING,
        },
    }
)
_CHANGE_UNIT = _closed(
    {
        "schema_version": {"const": "paragraph_history_change_unit.v1"},
        "change_unit_id": {
            "type": "string",
            "pattern": _anchored(r"cu_[0-9]+"),
        },
        "file_sha256": _SHA256,
        "change_type": {"enum": ["insert", "delete", "replace", "counter"]},
        "author": {"type": "string", "maxLength": 200_000},
        "date": _nullable({"type": "string", "maxLength": 200_000}),
        "anchor": _CHANGE_UNIT_ANCHOR,
        "clause_anchor": _CLAUSE_ANCHOR,
        "paragraph_context": _PARAGRAPH_CONTEXT,
        "old_text": _nullable({"type": "string", "maxLength": 100_000}),
        "new_text": _nullable({"type": "string", "maxLength": 100_000}),
        "reference": _CHANGE_UNIT_REFERENCE,
        "countered_by": {
            "type": "array",
            "items": _NONEMPTY_STRING,
        },
    }
)
_PROJECTION_COVERAGE = _closed(
    {
        "schema_version": {"const": "paragraph_projection_coverage.v1"},
        "text_revision_wrapper_count": _NONNEGATIVE_INTEGER,
        "move_wrapper_count": _NONNEGATIVE_INTEGER,
        "text_neutral_property_revision_count": _NONNEGATIVE_INTEGER,
        "existence_affecting_revision_count": _NONNEGATIVE_INTEGER,
        "excluded_text_bearing_subtree_count": _NONNEGATIVE_INTEGER,
        "projection_complete": {"type": "boolean"},
    }
)
_METADATA_ASSURANCE = _closed(
    {
        "author_metadata_interpretation": {
            "const": "unverified_document_string"
        },
        "date_metadata_interpretation": {"const": "unverified_document_string"},
        "authorship_verified": {"const": False},
        "time_verified": {"const": False},
    }
)
_SELECTED_PARAGRAPH = _closed(
    {
        "schema_version": {
            "const": "paragraph_history_selected_paragraph.v1"
        },
        "paragraph_observation_id": _PARAGRAPH_OBSERVATION_ID,
        "paragraph_id": _PARAGRAPH_ID,
        "paragraph_ref": _PARAGRAPH_REF_SCHEMA,
        "current": _CURRENT_PROJECTION,
        "rejected_pending": _REJECTED_PROJECTION,
        "support_to_higher": {
            "type": "array",
            "items": _RELATIONSHIP,
            "maxItems": 1,
        },
        "change_units": {
            "type": "array",
            "items": _CHANGE_UNIT,
            "maxItems": 1_000,
        },
        "change_units_sha256": _SHA256,
        "projection_coverage": _PROJECTION_COVERAGE,
        "metadata_assurance": _METADATA_ASSURANCE,
    }
)


_RESOLUTION_COMMON = {
    "schema_version": {"const": "paragraph_history_resolution.v1"},
    "candidate_count": {"type": "integer", "minimum": 0, "maximum": 10_000},
    "candidate_set_sha256": _SHA256,
    "current_candidate_count": {
        "type": "integer",
        "minimum": 0,
        "maximum": 10_000,
    },
    "rejected_candidate_count": {
        "type": "integer",
        "minimum": 0,
        "maximum": 10_000,
    },
}
_EXACT_UNIQUE_RESOLUTION = _closed(
    {
        **_RESOLUTION_COMMON,
        "state": {"const": "exact_unique"},
        "candidate_count": {"const": 1},
        "reason": {
            "enum": ["exact_current_unique", "rejected_projection_unique"]
        },
        "higher_rejected_projection_complete": {"const": True},
        "propagation_permitted": {"const": True},
    }
)
_AMBIGUOUS_RESOLUTION = _closed(
    {
        **_RESOLUTION_COMMON,
        "state": {"const": "ambiguous"},
        "candidate_count": {
            "type": "integer",
            "minimum": 2,
            "maximum": 10_000,
        },
        "reason": {"const": "multiple_exact_candidates"},
        "higher_rejected_projection_complete": {"type": "boolean"},
        "propagation_permitted": {"const": False},
    }
)
_UNRESOLVED_RESOLUTION = _closed(
    {
        **_RESOLUTION_COMMON,
        "state": {"const": "unresolved"},
        "candidate_count": {"type": "integer", "minimum": 0, "maximum": 1},
        "reason": {
            "enum": [
                "rejected_projection_unavailable",
                "navigation_only",
                "no_match_in_declared_scope",
                "blocked_by_higher_ambiguity",
                "blocked_by_higher_unresolved",
            ]
        },
        "higher_rejected_projection_complete": _nullable({"type": "boolean"}),
        "propagation_permitted": {"const": False},
    }
)
_RESOLUTION = {
    "oneOf": [
        _EXACT_UNIQUE_RESOLUTION,
        _AMBIGUOUS_RESOLUTION,
        _UNRESOLVED_RESOLUTION,
    ]
}

_OBSERVATION = _closed(
    {
        "schema_version": {"const": "paragraph_history_observation.v1"},
        "observation_id": _OBSERVATION_ID,
        "document_id": _DOCUMENT_ID,
        "path": _NONEMPTY_STRING,
        "filename": _FILENAME,
        "position": _POSITION,
        "file_sha256": _SHA256,
        "byte_length": {
            "type": "integer",
            "minimum": 0,
            "maximum": 52_428_800,
        },
        "entry_role": {"enum": ["seed", "trace_step"]},
        "selected_paragraph": _nullable(_SELECTED_PARAGRAPH),
        "resolution": _nullable(_RESOLUTION),
        "candidates": _CANDIDATE_SUMMARY,
        "navigation_candidates": _NAVIGATION_SUMMARY,
        "inspection_coverage": _INSPECTION_COVERAGE,
    }
)


_SEED_RESULT = _closed(
    {
        "schema_version": {"const": "paragraph_history_seed_result.v1"},
        "document_id": _DOCUMENT_ID,
        "observation_id": _OBSERVATION_ID,
        "paragraph_id": _PARAGRAPH_ID,
        "paragraph_observation_id": _PARAGRAPH_OBSERVATION_ID,
        "position": _POSITION,
        "paragraph_ref": _PARAGRAPH_REF_SCHEMA,
    }
)


def _order_result_variant(kind: str, rule: str) -> dict[str, Any]:
    return _closed(
        {
            "schema_version": {"const": "paragraph_history_order_result.v1"},
            "kind": {"const": kind},
            "rule": {"const": rule},
            "position_semantics": {"const": "declared_position_only"},
            "chronology_verified": {"const": False},
            "filename_manifest_sha256": _SHA256,
        }
    )


_ORDER_RESULT = {
    "oneOf": [
        _order_result_variant("filename_lexicographic_v1", "casefold_then_exact"),
        _order_result_variant("explicit_filename_sequence_v1", "exact_sequence"),
    ]
}
_SNAPSHOT = _closed(
    {
        "schema_version": {"const": "paragraph_history_snapshot.v1"},
        "filesystem_snapshot_sha256": _SHA256,
        "candidate_manifest_sha256": _SHA256,
        "seed_binding_sha256": _SHA256,
        "order_binding_sha256": _SHA256,
        "projection_policy_sha256": _SHA256,
        "limits_sha256": _SHA256,
        "full_result_set_sha256": _SHA256,
        "result_order": {"const": "seed_then_descending_position_v1"},
        "filesystem_cross_file_atomic": {"const": False},
    }
)

_RESOLUTION_COUNTS = _closed(
    {
        name: {"type": "integer", "minimum": 0, "maximum": 499}
        for name in ("exact_unique", "ambiguous", "unresolved")
    }
)
_RELATIONSHIP_COUNTS = _closed(
    {
        name: {"type": "integer", "minimum": 0, "maximum": 50_000}
        for name in (
            "exact_content_equality",
            "rejected_projection_equality",
        )
    }
)
_PROJECTION_STATUS_COUNTS = _closed(
    {
        name: {"type": "integer", "minimum": 0, "maximum": 500}
        for name in ("complete", "unavailable")
    }
)
_PROJECTION_TEXT_STATE_COUNTS = _closed(
    {
        name: {"type": "integer", "minimum": 0, "maximum": 500}
        for name in ("empty", "nonempty")
    }
)
_COVERAGE = _closed(
    {
        "scan_complete": {"const": True},
        "candidate_document_count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
        },
        "inspected_document_count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
        },
        "eligible_observation_count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
        },
        "returned_observation_count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
        },
        "cursor_offset": _POSITION,
        "output_truncated": {"type": "boolean"},
        "seed_entry_count": {"const": 1},
        "selected_paragraph_count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
        },
        "resolution_counts": _RESOLUTION_COUNTS,
        "blocked_observation_count": {
            "type": "integer",
            "minimum": 0,
            "maximum": 499,
        },
        "relationship_counts": _RELATIONSHIP_COUNTS,
        "selected_relationship_count": {
            "type": "integer",
            "minimum": 0,
            "maximum": 499,
        },
        "evaluated_rejected_projection_count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
        },
        "projection_status_counts": _PROJECTION_STATUS_COUNTS,
        "projection_text_state_counts": _PROJECTION_TEXT_STATE_COUNTS,
        "projection_equals_current_count": {
            "type": "integer",
            "minimum": 0,
            "maximum": 500,
        },
        "navigation_candidate_count": {
            "type": "integer",
            "minimum": 0,
            "maximum": 10_000,
        },
        "navigation_candidate_set_sha256": _SHA256,
        "returned_verbatim_char_count": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1_000_000,
        },
        "search_scope": {"const": "word_document_xml_body_v1"},
        "current_reading_mode": {"const": "accepted_current_v1"},
        "rejected_reading_mode": {
            "const": "pending_text_revisions_rejected_v1"
        },
        "container_policy": {"const": "canonical_body_flow_v1"},
        "whole_docx_coverage": {"const": False},
        "negative_whole_doc_claims": {"const": False},
        "chronology_verified": {"const": False},
        "semantic_identity_verified": {"const": False},
        "filesystem_cross_file_atomic": {"const": False},
    }
)


PARAGRAPH_HISTORY_LIMITS: dict[str, Any] = {
    "candidate_docx_files": 500,
    "candidate_compressed_input_bytes": 524_288_000,
    "candidate_expanded_bytes": 524_288_000,
    "compressed_bytes_per_docx": 52_428_800,
    "indexed_paragraphs_per_docx": 10_000,
    "indexed_paragraphs_per_folder": 100_000,
    "accepted_current_chars_per_paragraph": 50_000,
    "accepted_current_chars_per_docx": 2_000_000,
    "accepted_current_chars_per_folder": 20_000_000,
    "rejected_projection_chars_per_paragraph": 50_000,
    "rejected_projection_chars_per_docx": 50_000,
    "rejected_projection_chars_per_folder": 20_000_000,
    "decoded_text_chars_per_folder": 50_000_000,
    "exact_candidate_relationships": 50_000,
    "navigation_candidates": 10_000,
    "selected_change_units_per_result": 10_000,
    "change_units_per_selected_paragraph": 1_000,
    "change_unit_text_chars_per_selected_observation": 100_000,
    "change_unit_text_chars_per_result": 10_000_000,
    "sample_items": 20,
    "returned_verbatim_chars_per_observation": 200_000,
    "returned_verbatim_chars_per_page": 1_000_000,
    "default_page_items": 50,
    "maximum_page_items": 100,
    "revision_nesting_depth": 2,
    "journal_bytes": 67_108_864,
    "wall_clock_partial_results": False,
    "semantic_or_vector_search": False,
}
_LIMITS = _closed(
    {key: {"const": value} for key, value in PARAGRAPH_HISTORY_LIMITS.items()}
)


PARAGRAPH_HISTORY_RESULT_PROPERTIES: dict[str, Any] = {
    "schema_version": {"const": "paragraph_history.v1"},
    "status": {"const": "ok"},
    "seed": _SEED_RESULT,
    "ordering_source": {
        "enum": [
            "filename_lexicographic_v1",
            "explicit_filename_sequence_v1",
        ]
    },
    "order_basis": _ORDER_RESULT,
    "result_order": {"const": "seed_then_descending_position_v1"},
    "snapshot": _SNAPSHOT,
    "observations": {
        "type": "array",
        "items": _OBSERVATION,
        "minItems": 1,
        "maxItems": 100,
    },
    "coverage": _COVERAGE,
    "limits": _LIMITS,
    "next_cursor": _nullable(_CURSOR),
}
PARAGRAPH_HISTORY_RESULT_REQUIRED = list(PARAGRAPH_HISTORY_RESULT_PROPERTIES)

PARAGRAPH_HISTORY_OPERATION_RESULT_SCHEMA: dict[str, Any] = {
    "title": "Veqtor internal paragraph-history result",
    **_closed(
        PARAGRAPH_HISTORY_RESULT_PROPERTIES,
        required=PARAGRAPH_HISTORY_RESULT_REQUIRED,
    ),
}


__all__ = [
    "PARAGRAPH_HISTORY_CURSOR_SCHEMA",
    "PARAGRAPH_HISTORY_NULLABLE_CURSOR_SCHEMA",
    "PARAGRAPH_HISTORY_LIMITS",
    "PARAGRAPH_HISTORY_OPERATION_RESULT_SCHEMA",
    "PARAGRAPH_HISTORY_ORDER_SCHEMA",
    "PARAGRAPH_HISTORY_RESULT_PROPERTIES",
    "PARAGRAPH_HISTORY_RESULT_REQUIRED",
    "PARAGRAPH_HISTORY_SEED_SCHEMA",
]
