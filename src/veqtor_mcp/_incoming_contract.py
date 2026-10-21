# SPDX-License-Identifier: Apache-2.0
"""Closed additive contract for read-only incoming-version comparison."""
from typing import Annotated, Any, ClassVar

from pydantic import StringConstraints, WithJsonSchema

from veqtor_docx.incoming_comparison import LIMITS, POLICY, SCHEMA, _REVISION_NAMES
from .contracts import (
    MCP_CONTRACT_SCHEMA_EXTENSION, MCP_CONTRACT_SCHEMA_VERSION,
    PARAGRAPH_REF_SCHEMA, _ContractResult,
)


def obj(**fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def array(schema, maximum):
    return {"type": "array", "items": schema, "maxItems": maximum}


SHA = {"type": "string", "pattern": r"^[0-9a-f]{64}(?![\s\S])"}
TEXT = {"type": "string"}
COUNT = {"type": "integer", "minimum": 0}
BOOL = {"type": "boolean"}
SIDE = {"enum": ["sent", "incoming"]}
GROUP_ID = {"type": "string", "pattern": r"^group_[0-9]+(?![\s\S])"}
MODE = {"enum": ["accepted_current_v1", "pending_text_revisions_rejected_v1"]}
ISSUES = array(TEXT, 100)
REFS = {}
for kind, schema in (("table", "table_ref.v1"), ("row", "table_row_ref.v1"), ("cell", "table_cell_ref.v1")):
    fields = dict(schema_version={"const": schema}, ref_type={"const": kind}, file_sha256=SHA,
                  part_name={"const": "word/document.xml"}, container_policy={"const": "canonical_body_flow_v1"},
                  fingerprint_policy={"const": "element_c14n_without_comments_v1"},
                  element_sha256=SHA, table_index=COUNT)
    if kind in {"row", "cell"}:
        fields["row_index"] = COUNT
    if kind == "cell":
        fields["cell_index"] = COUNT
    REFS[kind] = obj(**fields)
PARAGRAPH = obj(
    kind={"const": "paragraph"}, side=SIDE, group_id=GROUP_ID,
    paragraph_ref=PARAGRAPH_REF_SCHEMA, container_kind={"enum": ["body", "table_cell"]},
    cell_ref=nullable(REFS["cell"]), text={**TEXT, "maxLength": 50_000},
    rejected_text=nullable({**TEXT, "maxLength": 50_000}),
    projection_status={"enum": ["complete", "unavailable"]}, projection_reasons=ISSUES,
    revision_occurrences=array(obj(kind={"enum": sorted(_REVISION_NAMES)}, revision_id=nullable(TEXT),
                                   occurrence_index=COUNT, markup_sha256=SHA),
                               LIMITS["revision_occurrences_per_paragraph"]),
    revision_markup_sha256=SHA, revision_ids_sha256=SHA, property_markup_sha256=SHA,
    paragraph_xml_sha256=SHA,
)
PARAGRAPH["allOf"] = [{
    "if": {"properties": {"projection_status": {"const": "complete"}}},
    "then": {"properties": {"rejected_text": {"type": "string"}, "projection_reasons": {"maxItems": 0}}},
    "else": {"properties": {"rejected_text": {"type": "null"}, "projection_reasons": {"minItems": 1}}},
}]
GROUP = obj(
    kind={"const": "comparison_group"}, group_id=GROUP_ID,
    basis={"enum": ["unique_projection_equality", "bounded_interval", "unresolved_interval", "unresolved_correspondence"]},
    sent_count=COUNT, incoming_count=COUNT,
    observations=array({"enum": ["text_changed", "revision_markup_changed", "revision_identifiers_changed",
        "property_markup_changed", "paragraph_xml_changed", "added_in_interval", "removed_from_interval",
        "ambiguous_or_unmatched_blocks"]}, 10),
    explanation={"enum": ["visible_in_current_revisions", "prior_revision_decision_compatible",
                          "not_explained_by_available_markup", "indeterminate", "not_applicable"]},
    equalities=array(obj(sent_mode=MODE, incoming_mode=MODE, sent_text_sha256=nullable(SHA),
                         incoming_text_sha256=nullable(SHA), result={"enum": ["equal", "different", "unavailable"]}), 4),
)
GROUP["allOf"] = [{
    "if": {"properties": {"basis": {"enum": ["unresolved_interval", "unresolved_correspondence"]}}},
    "then": {"properties": {"explanation": {"const": "indeterminate"}}},
}]
TABLE = obj(kind={"const": "table"}, side=SIDE, reference=REFS["table"],
            parent_cell_ref=nullable(REFS["cell"]), grid_column_count=nullable(COUNT), nesting_depth=COUNT, issues=ISSUES, property_markup_sha256=SHA)
ROW = obj(kind={"const": "row"}, side=SIDE, reference=REFS["row"],
          grid_before=COUNT, grid_after=COUNT, grid_extent=COUNT, issues=ISSUES, property_markup_sha256=SHA)
CELL = obj(kind={"const": "cell"}, side=SIDE, reference=REFS["cell"], grid_start=COUNT,
           grid_span={"type": "integer", "minimum": 1},
           h_merge={"enum": [None, "restart", "continue"]}, v_merge={"enum": [None, "restart", "continue"]},
           content_kinds=array({"enum": ["paragraph", "table"]}, 10_500),
           paragraph_indices=array(COUNT, 10_000), issues=ISSUES, property_markup_sha256=SHA)
PART = obj(kind={"const": "package_part"}, part_name=TEXT, sent_sha256=nullable(SHA),
           incoming_sha256=nullable(SHA), status={"enum": ["same_bytes", "changed_bytes", "only_sent", "only_incoming"]},
           content_interpretation={"const": "not_claimed"})
COVERAGE = obj(indexed_paragraphs={**COUNT, "maximum": 10_000}, accounted_paragraphs=COUNT,
               unaccounted_paragraphs={"const": 0}, empty_paragraphs=COUNT,
               container_coverage_complete=BOOL, excluded_subtrees=COUNT, excluded_paragraphs=COUNT,
               excluded_by_kind={"type": "object", "additionalProperties": COUNT},
               excluded_parts=array(TEXT, 2100), unavailable_projections=COUNT,
               table_coverage_complete=BOOL, table_coverage_reason=nullable(TEXT))
SOURCE = obj(sha256=SHA, size_bytes={**COUNT, "maximum": 50 * 1024 * 1024})
RESULT = obj(
    schema_version={"const": SCHEMA}, policy={"const": POLICY}, status={"const": "ok"},
    sources=obj(sent=SOURCE, incoming=SOURCE), comparison_id=SHA,
    coverage=obj(sent=COVERAGE, incoming=COVERAGE, partition_complete={"const": True}, supported_scope_complete=BOOL),
    observations=obj(package_bytes_equal=BOOL, all_part_bytes_equal=BOOL, current_text_sequence_equal=BOOL,
                     unique_anchor_order_changed=BOOL, current_table_topology={"enum": ["same", "different", "unavailable"]},
                     sent_topology_sha256=nullable(SHA), incoming_topology_sha256=nullable(SHA),
                     table_property_markup_sequence_equal=nullable(BOOL), document_equivalence={"const": "not_claimed"}, authorship_verified={"const": False},
                     tracking_disabled_verified={"const": False}),
    limitations=array(TEXT, 20), limits=obj(**{key: {"const": value} for key, value in LIMITS.items()}),
    items=array({"oneOf": [GROUP, PARAGRAPH, TABLE, ROW, CELL, PART]}, LIMITS["max_items"]),
    pagination=obj(total_items={**COUNT, "maximum": LIMITS["report_items"]}, offset=COUNT,
                   returned_items={**COUNT, "maximum": LIMITS["max_items"]}, has_more=BOOL),
    next_cursor=nullable({"type": "string", "pattern": r"^ic1:[1-9][0-9]{0,7}:[0-9a-f]{64}(?![\s\S])"}),
    producer=obj(name={"const": "veqtor-mcp"}, version={**TEXT, "minLength": 1, "maxLength": 64},
                 build={**TEXT, "minLength": 1, "maxLength": 128}),
    record_id={"type": "null"}, record_status={"const": "disabled"},
)
RESULT[MCP_CONTRACT_SCHEMA_EXTENSION] = MCP_CONTRACT_SCHEMA_VERSION


class IncomingComparisonResult(_ContractResult):
    contract_schema: ClassVar[dict[str, Any]] = RESULT


ComparisonShaInput = Annotated[
    str, StringConstraints(strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
    WithJsonSchema(SHA),
]
