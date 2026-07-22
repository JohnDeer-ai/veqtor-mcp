# SPDX-License-Identifier: Apache-2.0
"""Closed nested JSON Schemas for the Stage 3B Round Map v0.3 contract."""

from __future__ import annotations

from typing import Any

from .round_map import ROUND_MAP_LIMITS

_SHA = {
    "type": "string",
    "minLength": 64,
    "maxLength": 64,
    "pattern": r"^[0-9a-f]{64}(?![\s\S])",
}
_NONNEG = {"type": "integer", "minimum": 0}
_STRING = {"type": "string"}
_NONEMPTY = {"type": "string", "minLength": 1}
_NULL_STRING = {"type": ["string", "null"]}


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


PARAGRAPH_REF_SCHEMA = _closed(
    {
        "schema_version": {"const": "paragraph_ref.v1"},
        "ref_type": {"const": "paragraph"},
        "file_sha256": _SHA,
        "part_name": {"const": "word/document.xml"},
        "paragraph_index": _NONNEG,
        "paragraph_text_sha256": _SHA,
        "reading_mode": {"const": "accepted_current_v1"},
        "container_policy": {"const": "canonical_body_flow_v1"},
    }
)

SECTION_REF_SCHEMA = _closed(
    {
        "schema_version": {"const": "section_ref.v1"},
        "ref_type": {"const": "section"},
        "file_sha256": _SHA,
        "part_name": {"const": "word/document.xml"},
        "heading_paragraph_index": _NONNEG,
        "heading_text_sha256": _SHA,
        "reading_mode": {"const": "accepted_current_v1"},
        "container_policy": {"const": "canonical_body_flow_v1"},
    }
)

ROUND_MAP_SEED_SCHEMA: dict[str, Any] = {
    "title": "Veqtor Round Map seed",
    "type": "object",
    "properties": {
        "schema_version": {"const": "round_map_seed.v1"},
        "path": _NONEMPTY,
        "paragraph_ref": PARAGRAPH_REF_SCHEMA,
    },
    "required": ["schema_version", "path", "paragraph_ref"],
    "additionalProperties": False,
}

_CONTAINER_COVERAGE = _closed(
    {
        "schema_version": {"const": "canonical_body_flow_v1"},
        "indexed_paragraph_count": _NONNEG,
        "body_paragraph_count": _NONNEG,
        "table_cell_paragraph_count": _NONNEG,
        "excluded_subtree_count": _NONNEG,
        "excluded_paragraph_count": _NONNEG,
        "excluded_by_kind": {
            "type": "object",
            "additionalProperties": _NONNEG,
        },
        "excluded_paragraphs_by_kind": {
            "type": "object",
            "additionalProperties": _NONNEG,
        },
        "coverage_complete": {"type": "boolean"},
        "legacy_two_field_anchor_safe": {"type": "boolean"},
    }
)

_INSPECTION_COVERAGE = {
    "anyOf": [
        _closed(
            {
                "schema_version": {"const": "round_map_inspection_coverage.v1"},
                "scan_complete": {"const": True},
                "indexed_paragraph_count": _NONNEG,
                "nonempty_indexed_paragraph_count": _NONNEG,
                "included_parts": {
                    "type": "array",
                    "prefixItems": [{"const": "word/document.xml"}],
                    "minItems": 1,
                    "maxItems": 1,
                },
                "excluded_parts": {"type": "array", "items": _NONEMPTY},
                "included_containers": {
                    "type": "array",
                    "prefixItems": [{"const": "body"}, {"const": "table_cell"}],
                    "minItems": 2,
                    "maxItems": 2,
                },
                "container_coverage": _CONTAINER_COVERAGE,
            }
        ),
        {"type": "null"},
    ]
}

_TOPOLOGY_FLAGS = _closed(
    {
        "multiple_parents": {"type": "boolean"},
        "cycle_member": {"type": "boolean"},
        "self_loop": {"type": "boolean"},
    }
)

_DOCUMENT_NODE = _closed(
    {
        "schema_version": {"const": "round_map_item.v1"},
        "item_type": {"const": "document_node"},
        "id": {"type": "string", "pattern": r"^rm_doc_v1:[0-9a-f]{64}$"},
        "file_sha256": _SHA,
        "observation_state": {
            "enum": ["current", "record_only", "current_and_recorded"]
        },
        "observation_count": _NONNEG,
        "inspection_coverage": _INSPECTION_COVERAGE,
        "incoming_recorded_derivation_count": _NONNEG,
        "outgoing_recorded_derivation_count": _NONNEG,
        "topology_flags": _TOPOLOGY_FLAGS,
    }
)

_OBSERVATION = _closed(
    {
        "schema_version": {"const": "round_map_item.v1"},
        "item_type": {"const": "document_observation"},
        "id": {"type": "string", "pattern": r"^rm_obs_v1:[0-9a-f]{64}$"},
        "document_id": {
            "type": "string",
            "pattern": r"^rm_doc_v1:[0-9a-f]{64}$",
        },
        "path": _NONEMPTY,
        "filename": _NONEMPTY,
        "position": _NONNEG,
        "round_id": {"type": "string", "pattern": r"^round-[0-9]{3,}$"},
        "position_basis": {
            "enum": [
                "filename_lexicographic_v1",
                "explicit_filename_sequence_v1",
            ]
        },
    }
)

_PARAGRAPH_NODE = _closed(
    {
        "schema_version": {"const": "round_map_item.v1"},
        "item_type": {"const": "paragraph_node"},
        "id": {"type": "string", "pattern": r"^rm_par_v1:[0-9a-f]{64}$"},
        "document_id": {
            "type": "string",
            "pattern": r"^rm_doc_v1:[0-9a-f]{64}$",
        },
        "paragraph_ref": PARAGRAPH_REF_SCHEMA,
        "container_kind": {"enum": ["body", "table_cell"]},
        "roles": {
            "oneOf": [
                {
                    "type": "array",
                    "prefixItems": [{"const": "seed"}],
                    "minItems": 1,
                    "maxItems": 1,
                },
                {
                    "type": "array",
                    "prefixItems": [{"const": "exact_candidate"}],
                    "minItems": 1,
                    "maxItems": 1,
                },
            ]
        },
    }
)

_LABEL_BASIS = {
    "anyOf": [
        {"enum": ["word_numbering_v1", "explicit_heading_text_v1"]},
        {"type": "null"},
    ]
}

_SECTION_NODE = _closed(
    {
        "schema_version": {"const": "round_map_item.v1"},
        "item_type": {"const": "section_node"},
        "id": {"type": "string", "pattern": r"^rm_sec_v1:[0-9a-f]{64}$"},
        "document_id": {
            "type": "string",
            "pattern": r"^rm_doc_v1:[0-9a-f]{64}$",
        },
        "section_ref": SECTION_REF_SCHEMA,
        "label": _NULL_STRING,
        "heading": _NULL_STRING,
        "level": {"type": "integer", "minimum": 0, "maximum": 8},
        "basis": {"const": "word_outline_level_v1"},
        "label_basis": _LABEL_BASIS,
        "roles": {
            "oneOf": [
                {
                    "type": "array",
                    "prefixItems": [{"const": "seed_navigation"}],
                    "minItems": 1,
                    "maxItems": 1,
                },
                {
                    "type": "array",
                    "prefixItems": [{"const": "candidate_navigation"}],
                    "minItems": 1,
                    "maxItems": 1,
                },
            ]
        },
    }
)

_SUPPORT_SAMPLE = _closed(
    {
        "record_id": {"type": "string", "pattern": r"^dr_[0-9]+$"},
        "record_sha256": _SHA,
        "profile": {
            "enum": [
                "current_v0.3",
                "published_v0.1.2_preflightless",
                "frozen_legacy_v1",
            ]
        },
    }
)

_RECORDED_BASIS = _closed(
    {
        "schema_version": {"const": "recorded_derivation_basis.v1"},
        "record_schema_version": {"const": "decision_record.v1"},
        "tool_name": {"const": "apply_edits"},
        "record_type": {"const": "decision.v1"},
        "assurance": {"const": "best_effort_local_non_tamper_evident"},
        "derivation_scope": {"const": "document_bytes_only"},
        "support_profile": {
            "enum": [
                "current_only",
                "published_v0_1_2_only",
                "frozen_legacy_only",
                "mixed",
            ]
        },
        "supporting_records": _closed(
            {
                "count": _NONNEG,
                "current_count": _NONNEG,
                "published_v0_1_2_count": _NONNEG,
                "frozen_legacy_count": _NONNEG,
                "sha256": _SHA,
                "sample": {
                    "type": "array",
                    "items": _SUPPORT_SAMPLE,
                    "maxItems": 20,
                },
                "truncated": {"type": "boolean"},
            }
        ),
    }
)

_EQUALITY_BASIS = _closed(
    {
        "schema_version": {"const": "exact_content_equality_basis.v1"},
        "reading_mode": {"const": "accepted_current_v1"},
        "container_policy": {"const": "canonical_body_flow_v1"},
        "part_name": {"const": "word/document.xml"},
        "comparison": {"const": "complete_unicode_scalar_sequence_v1"},
        "full_text_compared": {"const": True},
        "paragraph_text_sha256": _SHA,
    }
)

_NAVIGATION_SIGNAL = _closed(
    {
        "kind": {"enum": ["label_exact_v1", "heading_exact_v1"]},
        "value_sha256": _SHA,
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
        },
        "evidence_class": {"const": "navigation_only"},
    }
)


def _relationship_variant(
    relationship_type: str,
    direction: str,
    basis: dict[str, Any],
    derivation_recorded: bool,
) -> dict[str, Any]:
    return _closed(
        {
            "schema_version": {"const": "round_map_item.v1"},
            "item_type": {"const": "relationship"},
            "id": {"type": "string", "pattern": r"^rm_rel_v1:[0-9a-f]{64}$"},
            "relationship_type": {"const": relationship_type},
            "from_id": {
                "type": "string",
                "pattern": (
                    r"^rm_doc_v1:[0-9a-f]{64}$"
                    if relationship_type == "recorded_derivation"
                    else r"^rm_par_v1:[0-9a-f]{64}$"
                    if relationship_type == "exact_content_equality"
                    else r"^rm_sec_v1:[0-9a-f]{64}$"
                ),
            },
            "to_id": {
                "type": "string",
                "pattern": (
                    r"^rm_doc_v1:[0-9a-f]{64}$"
                    if relationship_type == "recorded_derivation"
                    else r"^rm_par_v1:[0-9a-f]{64}$"
                    if relationship_type == "exact_content_equality"
                    else r"^rm_sec_v1:[0-9a-f]{64}$"
                ),
            },
            "direction": {"const": direction},
            "basis": basis,
            "derivation_recorded": {"const": derivation_recorded},
            "lineage_verified": {"const": False},
            "chronology_verified": {"const": False},
        }
    )


_RELATIONSHIP = {
    "oneOf": [
        _relationship_variant("recorded_derivation", "directed", _RECORDED_BASIS, True),
        _relationship_variant(
            "exact_content_equality", "symmetric", _EQUALITY_BASIS, False
        ),
        _relationship_variant(
            "navigation_candidate", "directed", _NAVIGATION_BASIS, False
        ),
    ]
}

_CANDIDATE_IDS = _closed(
    {
        "count": _NONNEG,
        "sha256": _SHA,
        "sample": {
            "type": "array",
            "items": {
                "type": "string",
                "pattern": r"^rm_(?:par|sec)_v1:[0-9a-f]{64}$",
            },
            "maxItems": 20,
        },
        "truncated": {"type": "boolean"},
    }
)

_RESOLUTION = _closed(
    {
        "schema_version": {"const": "round_map_item.v1"},
        "item_type": {"const": "resolution"},
        "id": {
            "type": "string",
            "pattern": r"^rm_resolution_v1:[0-9a-f]{64}$",
        },
        "seed_paragraph_id": {
            "type": "string",
            "pattern": r"^rm_par_v1:[0-9a-f]{64}$",
        },
        "document_id": {
            "type": "string",
            "pattern": r"^rm_doc_v1:[0-9a-f]{64}$",
        },
        "state": {"enum": ["exact_unique", "ambiguous", "unresolved"]},
        "reason": {
            "enum": [
                "one_exact_candidate",
                "multiple_exact_candidates",
                "recorded_fact_conflict",
                "navigation_only",
                "no_match_in_declared_scope",
                "declared_scope_incomplete",
                "record_only_document",
            ]
        },
        "exact_candidate_count": _NONNEG,
        "navigation_candidate_count": _NONNEG,
        "conflict_count": _NONNEG,
        "candidate_ids": _CANDIDATE_IDS,
    }
)

_CONFLICT = _closed(
    {
        "schema_version": {"const": "round_map_item.v1"},
        "item_type": {"const": "conflict"},
        "id": {
            "type": "string",
            "pattern": r"^rm_conflict_v1:[0-9a-f]{64}$",
        },
        "conflict_type": {"const": "inconsistent_apply_record"},
        "reason": {
            "enum": [
                "result_status_invalid",
                "missing_source_sha256",
                "invalid_source_sha256",
                "missing_output_sha256",
                "invalid_output_sha256",
                "result_output_sha256_mismatch",
                "round_trip_missing",
                "round_trip_failed",
                "round_trip_comparison_unsupported",
                "round_trip_fact_mismatch",
                "result_source_sha256_mismatch",
                "preflight_binding_status_invalid",
                "preflight_candidate_sha256_mismatch",
                "candidate_output_sha256_match_invalid",
                "strengthened_fact_mismatch",
                "unsupported_legacy_profile",
            ]
        },
        "affected_document_ids": {
            "type": "array",
            "items": {
                "type": "string",
                "pattern": r"^rm_doc_v1:[0-9a-f]{64}$",
            },
            "minItems": 1,
            "uniqueItems": True,
        },
        "record_sha256": _SHA,
        "edge_emitted": {"const": False},
    }
)

ROUND_MAP_ITEM_SCHEMA: dict[str, Any] = {
    "oneOf": [
        _DOCUMENT_NODE,
        _OBSERVATION,
        _PARAGRAPH_NODE,
        _SECTION_NODE,
        _RELATIONSHIP,
        _RESOLUTION,
        _CONFLICT,
    ]
}

_SEED_RESULT = _closed(
    {
        "document_id": {
            "type": "string",
            "pattern": r"^rm_doc_v1:[0-9a-f]{64}$",
        },
        "paragraph_id": {
            "type": "string",
            "pattern": r"^rm_par_v1:[0-9a-f]{64}$",
        },
        "paragraph_ref": PARAGRAPH_REF_SCHEMA,
    }
)

_ORDER_BASIS = _closed(
    {
        "kind": {"enum": ["filename", "caller_supplied_filename_sequence"]},
        "rule": {"enum": ["casefold_then_exact", "exact_sequence"]},
        "lineage_verified": {"const": False},
        "round_id_semantics": {"const": "position_only"},
        "filename_manifest_sha256": _SHA,
    }
)

_SNAPSHOT = _closed(
    {
        "schema_version": {"const": "round_map_snapshot.v1"},
        "filesystem_snapshot_sha256": _SHA,
        "journal_snapshot_sha256": _SHA,
        "journal_state": {
            "enum": [
                "relevant_apply_records_present",
                "no_relevant_apply_records",
            ]
        },
        "full_result_set_sha256": _SHA,
        "filesystem_cross_file_atomic": {"const": False},
        "cross_source_atomic": {"const": False},
    }
)

_RELATIONSHIP_COUNTS = _closed(
    {
        "recorded_derivation": _NONNEG,
        "exact_content_equality": _NONNEG,
        "navigation_candidate": _NONNEG,
    }
)
_RESOLUTION_COUNTS = _closed(
    {"exact_unique": _NONNEG, "ambiguous": _NONNEG, "unresolved": _NONNEG}
)
_ITEM_TYPE_COUNTS = _closed(
    {
        name: _NONNEG
        for name in (
            "document_node",
            "document_observation",
            "paragraph_node",
            "section_node",
            "relationship",
            "resolution",
            "conflict",
        )
    }
)

_COVERAGE = _closed(
    {
        "scan_complete": {"const": True},
        "candidate_document_count": _NONNEG,
        "inspected_document_count": _NONNEG,
        "record_only_document_count": _NONNEG,
        "relevant_apply_record_count": _NONNEG,
        "eligible_derivation_record_count": _NONNEG,
        "rejected_semantic_record_count": _NONNEG,
        "eligible_item_count": _NONNEG,
        "returned_item_count": _NONNEG,
        "cursor_offset": _NONNEG,
        "output_truncated": {"type": "boolean"},
        "relationship_counts": _RELATIONSHIP_COUNTS,
        "resolution_counts": _RESOLUTION_COUNTS,
        "item_type_counts": _ITEM_TYPE_COUNTS,
        "search_scope": {"const": "word_document_xml_body_v1"},
        "reading_mode": {"const": "accepted_current_v1"},
        "container_policy": {"const": "canonical_body_flow_v1"},
        "whole_docx_coverage": {"const": False},
        "negative_whole_doc_claims": {"const": False},
    }
)

_LIMITS = _closed({key: {"const": value} for key, value in ROUND_MAP_LIMITS.items()})

ROUND_MAP_RESULT_PROPERTIES: dict[str, Any] = {
    "schema_version": {"const": "round_map.v1"},
    "status": {"const": "ok"},
    "seed": _SEED_RESULT,
    "ordering_source": {
        "enum": [
            "filename_lexicographic_v1",
            "explicit_filename_sequence_v1",
        ]
    },
    "order_basis": _ORDER_BASIS,
    "snapshot": _SNAPSHOT,
    "items": {"type": "array", "items": ROUND_MAP_ITEM_SCHEMA, "maxItems": 100},
    "coverage": _COVERAGE,
    "limits": _LIMITS,
    "next_cursor": {
        "anyOf": [
            {
                "type": "string",
                "pattern": r"^rm1:[1-9][0-9]*:[0-9a-f]{64}$",
            },
            {"type": "null"},
        ]
    },
}

ROUND_MAP_RESULT_REQUIRED = list(ROUND_MAP_RESULT_PROPERTIES)

_SUMMARY_ID_PREFIXES = {
    "document_node": "rm_doc_v1",
    "document_observation": "rm_obs_v1",
    "paragraph_node": "rm_par_v1",
    "section_node": "rm_sec_v1",
    "relationship": "rm_rel_v1",
    "resolution": "rm_resolution_v1",
    "conflict": "rm_conflict_v1",
}

_ITEM_SUMMARY_ENTRY = {
    "oneOf": [
        _closed(
            {
                "item_type": {"const": item_type},
                "id": {
                    "type": "string",
                    "pattern": rf"^{prefix}:[0-9a-f]{{64}}$",
                },
                "item_sha256": _SHA,
            }
        )
        for item_type, prefix in _SUMMARY_ID_PREFIXES.items()
    ]
}

ROUND_MAP_RECORD_RESULT_SCHEMA: dict[str, Any] = _closed(
    {
        "status": {"const": "ok"},
        "seed": _SEED_RESULT,
        "ordering_source": ROUND_MAP_RESULT_PROPERTIES["ordering_source"],
        "filename_manifest_sha256": _SHA,
        "snapshot": _SNAPSHOT,
        "coverage": _COVERAGE,
        "limits": _LIMITS,
        "next_cursor_sha256": {
            "anyOf": [_SHA, {"type": "null"}],
        },
        "items_summary": _closed(
            {
                "count": _NONNEG,
                "sha256": _SHA,
                "sample": {
                    "type": "array",
                    "items": _ITEM_SUMMARY_ENTRY,
                    "maxItems": 20,
                },
                "truncated": {"type": "boolean"},
            }
        ),
    }
)

ROUND_MAP_RECORD_PROVENANCE_SCHEMA: dict[str, Any] = _closed(
    {
        "filesystem_snapshot_sha256": _SHA,
        "journal_snapshot_sha256": _SHA,
        "full_result_set_sha256": _SHA,
        "reading_mode": {"const": "accepted_current_v1"},
        "container_policy": {"const": "canonical_body_flow_v1"},
        "search_scope": {"const": "word_document_xml_body_v1"},
    }
)
