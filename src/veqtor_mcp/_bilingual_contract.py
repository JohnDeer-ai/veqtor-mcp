# SPDX-License-Identifier: Apache-2.0
"""Closed additive MCP contracts for read-only bilingual evidence."""
from typing import Annotated, Any, ClassVar

from pydantic import WithJsonSchema

from veqtor_docx.bilingual import (
    CONDITION, COUNT, LIMITS, PAIR, PARAGRAPH_REF, POLICY, REFS, SELECTIONS, SHA,
    SIDE, TEXT, array, obj,
)
from .contracts import (
    MCP_CONTRACT_SCHEMA_EXTENSION, MCP_CONTRACT_SCHEMA_VERSION, _ContractResult,
    _CONTAINER_COVERAGE_SCHEMA,
)


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


BOOL = {"type": "boolean"}
ISSUES = array(TEXT, 100)
REVISION_FLAGS = dict(
    has_tracked_property_revisions=BOOL,
    tracked_property_revision_counts={"type": "object", "additionalProperties": COUNT},
    has_structural_revision_markers=BOOL,
    structural_revision_marker_counts={"type": "object", "additionalProperties": COUNT},
)
TABLE = obj(
    kind={"const": "table"}, table_ref=REFS["table"], table_index=COUNT,
    nesting_depth=COUNT, parent_cell_ref=nullable(REFS["cell"]), grid_column_count=nullable(COUNT),
    has_grid_gaps=BOOL, has_merges=BOOL, has_nested_tables=BOOL, simple_rectangular=BOOL,
    mutation_safety={"const": "not_assessed"}, issues=ISSUES, **REVISION_FLAGS,
)
ROW = obj(kind={"const": "row"}, row_ref=REFS["row"], row_index=COUNT,
          grid_before=COUNT, grid_after=COUNT, grid_extent=COUNT, issues=ISSUES, **REVISION_FLAGS)
CELL = obj(
    kind={"const": "cell"}, cell_ref=REFS["cell"], cell_index=COUNT,
    grid_start=COUNT, grid_span={"type": "integer", "minimum": 1},
    h_merge={"enum": [None, "restart", "continue"]}, v_merge={"enum": [None, "restart", "continue"]},
    paragraph_refs=array(PARAGRAPH_REF, LIMITS["paragraphs"]),
    content=array({"oneOf": [obj(kind={"const": "paragraph"}, reference=PARAGRAPH_REF),
                                 obj(kind={"const": "table"}, reference=REFS["table"])]}, 10_500),
    nested_table_refs=array(REFS["table"], 500), issues=ISSUES, **REVISION_FLAGS,
)
PARAGRAPH = obj(kind={"const": "paragraph"}, paragraph_ref=PARAGRAPH_REF,
                cell_ref=nullable(REFS["cell"]), text={**TEXT, "maxLength": 50_000}, numeric_scan_supported=BOOL)
TEXT_REFERENCE = obj(kind={"const": "text_reference"}, paragraph_ref=PARAGRAPH_REF,
                     start=COUNT, end=COUNT, quote=TEXT, within_affected_scope=BOOL)
FIELD = obj(kind={"const": "field"}, paragraph_ref=PARAGRAPH_REF, occurrence_index=COUNT,
            node_kind={"enum": ["fldSimple", "fldChar", "instrText"]},
            instruction=nullable(TEXT), field_char_type=nullable(TEXT), within_affected_scope=BOOL)
LIMIT_SCHEMA = obj(**{key: {"const": value} for key, value in LIMITS.items()})
PRODUCER = obj(name={"const": "veqtor-mcp"}, version={**TEXT, "minLength": 1, "maxLength": 64},
               build={**TEXT, "minLength": 1, "maxLength": 128})
COMMON = dict(policy={"const": POLICY}, status={"const": "ok"}, file_sha256=SHA,
              limits=LIMIT_SCHEMA, limitations=array(TEXT, 20), producer=PRODUCER,
              record_id={"type": "null"}, record_status={"const": "disabled"})
# The canonical-flow coverage object is versioned by the existing inspection
# contract; use its closed schema, shared by table_map and incoming comparison.
INVENTORY_RESULT = obj(
    schema_version={"const": "bilingual_table_inventory.v1"}, **COMMON,
    items=array({"oneOf": [TABLE, ROW, CELL, PARAGRAPH, TEXT_REFERENCE, FIELD]}, 100_000),
    counts=obj(tables=COUNT, rows=COUNT, cells=COUNT, table_paragraphs=COUNT,
               empty_table_paragraphs=COUNT, nested_tables=COUNT, paragraphs=COUNT,
               text_references=COUNT, field_nodes=COUNT),
    coverage=obj(scope={"const": "canonical_main_body_tables_v1"}, complete=BOOL,
                 canonical_body_flow=_CONTAINER_COVERAGE_SCHEMA,
                 excluded_parts=array(TEXT, 2100), excluded_table_count=COUNT,
                 unmapped_table_cell_paragraph_count=COUNT, structural_issue_count=COUNT),
    reference_report=obj(scope={"const": "canonical_main_body_current_text_and_raw_field_nodes_v1"},
                         scanned_paragraphs=COUNT, affected_paragraph_refs=array(PARAGRAPH_REF, LIMITS["paragraphs"]),
                         target_validity={"const": "not_assessed"}, semantic_reference_completeness={"const": "not_claimed"},
                         fields_include_historical_markup={"const": True}),
)
VALUE = obj(
    paragraph_ref=PARAGRAPH_REF, start=COUNT, end=COUNT, quote=TEXT,
    kind={"enum": ["number", "amount", "percentage", "duration", "quantity", "date", "number_words", "formula"]},
    normalized=nullable(TEXT), unit=nullable(TEXT), day_basis={"enum": [None, "calendar", "business", "unspecified"]},
    scale=TEXT, status={"enum": ["supported", "unsupported"]}, reason=nullable(TEXT),
    value_id=TEXT, paired_condition_id=nullable(TEXT),
)
CONDITION_RESULT = obj(
    **CONDITION["properties"],
    status={"enum": ["numeric_values_and_units_match", "mismatch", "not_established", "unsupported_format"]},
    reason=TEXT, left_value_ids=array(TEXT, LIMITS["values"]), right_value_ids=array(TEXT, LIMITS["values"]),
)
PAIR_RESULT = obj(
    pair_id=PAIR["properties"]["pair_id"], table_ref=REFS["table"], row_ref=REFS["row"], left=SIDE, right=SIDE,
    conditions=array(CONDITION_RESULT, LIMITS["conditions_per_pair"]),
    values=obj(left=array(VALUE, LIMITS["values"]), right=array(VALUE, LIMITS["values"])),
    coverage=obj(values=COUNT, unsupported_values=COUNT, unpaired_values=COUNT,
                 paired_values=COUNT, selected_conditions=COUNT, conditions_with_match=COUNT),
)
CHECK_RESULT = obj(
    schema_version={"const": "bilingual_condition_check.v1"}, **COMMON,
    pairs=array(PAIR_RESULT, LIMITS["cell_pairs"]),
    coverage=obj(selected_cell_pairs=COUNT, values=COUNT, document_wide_numeric_coverage={"const": "not_claimed"}),
    obligation_equivalence={"const": "not_claimed"},
)
for _result in (INVENTORY_RESULT, CHECK_RESULT):
    _result[MCP_CONTRACT_SCHEMA_EXTENSION] = MCP_CONTRACT_SCHEMA_VERSION


class ReadDocumentTablesResult(_ContractResult):
    contract_schema: ClassVar[dict[str, Any]] = INVENTORY_RESULT


class BilingualConditionsResult(_ContractResult):
    contract_schema: ClassVar[dict[str, Any]] = CHECK_RESULT


BilingualPairsInput = Annotated[list[dict[str, Any]], WithJsonSchema(SELECTIONS)]
AffectedParagraphsInput = Annotated[list[dict[str, Any]], WithJsonSchema(array(PARAGRAPH_REF, LIMITS["paragraphs"]))]
