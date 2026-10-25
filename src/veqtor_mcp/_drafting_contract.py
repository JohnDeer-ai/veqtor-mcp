# SPDX-License-Identifier: Apache-2.0
"""Additive, closed contracts for the author drafting workflow."""
from typing import Annotated, Any, ClassVar
import uuid

from pydantic import model_validator

from pydantic import StringConstraints, WithJsonSchema

from veqtor_docx._drafting_paragraphs import MAX_PARAGRAPH_EDIT_STEPS
from veqtor_docx._drafting_text import DRAFTING_TIMESTAMP_PATTERN

from ._incoming_contract import REFS as TABLE_REFS
from .contracts import (
    MCP_CONTRACT_SCHEMA_EXTENSION,
    MCP_CONTRACT_SCHEMA_VERSION,
    PARAGRAPH_REF_SCHEMA,
    RECORD_ERROR_PATTERN,
    _ContractResult,
)


def obj(**properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


TEXT = {"type": "string"}
SHA = {"type": "string", "minLength": 64, "maxLength": 64,
       "pattern": r"^[0-9a-f]{64}(?![\s\S])"}
NONEMPTY = {"type": "string", "minLength": 1, "maxLength": 20_000}
NEW_TEXT = {"type": "string", "maxLength": 20_000}
LOCK_REASON = {"const": "owner_not_proven_dead"}
TIMESTAMP = {"type": "string", "minLength": 20, "maxLength": 25,
             "pattern": "^" + DRAFTING_TIMESTAMP_PATTERN + r"(?![\s\S])"}
REVISION_REF = obj(
    schema_version={"const": "revision_ref.v1"}, file_sha256=SHA,
    part_name={"const": "word/document.xml"},
    element_path={"type": "array", "minItems": 1, "maxItems": 64,
                  "items": {"type": "integer", "minimum": 0, "maximum": 1_000_000}},
    element_sha256=SHA, owner_sha256=SHA,
    fingerprint_policy={"const": "element_c14n_without_comments_v1"},
)
DECISION_TARGET = {"oneOf": [
    obj(kind={"const": "single"}, revision_ref=REVISION_REF),
    obj(kind={"const": "replacement"}, deletion_ref=REVISION_REF, insertion_ref=REVISION_REF),
    obj(kind={"const": "paragraph_structure"}, paragraph_mark_ref=REVISION_REF,
        text_revision_refs={"type": "array", "minItems": 1, "maxItems": 64, "items": REVISION_REF}),
    obj(kind={"const": "row_structure"}, row_mark_ref=REVISION_REF,
        content_revision_refs={"type": "array", "maxItems": 128, "items": REVISION_REF}),
    obj(kind={"const": "counter_insertion"}, original_insertion_ref=REVISION_REF,
        counter_deletion_ref=REVISION_REF, counter_insertion_ref=nullable(REVISION_REF)),
]}
DECISION_INPUT = obj(kind={"const": "decide_revision"},
                     decision={"enum": ["accept", "reject"]}, target=DECISION_TARGET)
DECISION_INPUT["properties"]["counter_decision"] = {"enum": ["accept", "reject"]}
DECISION_INPUT["allOf"] = [{
    "if": {"properties": {"target": {"properties": {"kind": {"const": "counter_insertion"}}}}},
    "then": {"required": ["counter_decision"]},
    "else": {"properties": {"counter_decision": False}},
}]
DECISION_PREVIEW = obj(
    operation={"const": "decide_revision"}, decision={"enum": ["accept", "reject"]},
    revision_kind={"enum": ["ins", "del", "replacement", "counter_insertion", "rPrChange", "pPrChange", "tcPrChange", "trPrChange", "tblPrChange", "tblGridChange", "tblPrExChange", "paragraph_insertion", "paragraph_deletion", "row_insertion", "row_deletion"]},
    members={"type": "array", "minItems": 1, "maxItems": 129,
             "items": obj(kind=TEXT, id=nullable(TEXT), author=nullable(TEXT), date=nullable(TEXT))},
    current_text=NEW_TEXT, accepted_text=nullable(NEW_TEXT), rejected_text=nullable(NEW_TEXT),
    current_properties=nullable(NEW_TEXT), previous_properties=nullable(NEW_TEXT),
    accept_refusal_code=nullable(NONEMPTY), reject_refusal_code=nullable(NONEMPTY),
    fixed_width_validation_scope={"enum": ["not_applicable", "single_outcome", "explicit_batch_final_state"]},
    table_validation_scope={"enum": ["not_applicable", "single_outcome", "explicit_batch_final_state"]},
    table_group_members={"type": "array", "maxItems": 100,
                         "items": obj(revision_ref=REVISION_REF, decision={"enum": ["accept", "reject"]})},
)
_ROW_TEXT = {"type": "string", "maxLength": 40_001}
for field in ("current_text", "accepted_text", "rejected_text"):
    DECISION_PREVIEW["properties"][field] = (
        _ROW_TEXT if field == "current_text" else nullable(_ROW_TEXT)
    )
DECISION_PREVIEW["allOf"] = [{
    "if": {"properties": {"revision_kind": {"enum": ["row_insertion", "row_deletion"]}}},
    "else": {"properties": {"current_text": NEW_TEXT,
                             "accepted_text": nullable(NEW_TEXT), "rejected_text": nullable(NEW_TEXT)}},
}]
NUMBERING_FACT = obj(
    paragraph_index={"type": "integer", "minimum": 0}, p_style=nullable(TEXT),
    style_chain={"type": "array", "maxItems": 64, "items": TEXT},
    num_id=nullable(TEXT), abstract_num_id=nullable(TEXT), ilvl=nullable({"type": "integer", "minimum": 0, "maximum": 8}),
    label=nullable(TEXT), value=nullable({"type": "integer"}), start=nullable({"type": "integer"}),
    start_source=nullable({"enum": ["abstract_start", "level_override", "start_override"]}),
    restart=nullable({"const": "continuous_level_zero"}),
)
PARAGRAPH_NUMBERING = obj(**{action: {"type": "array", "maxItems": 1000, "items": NUMBERING_FACT}
                            for action in ("accepted", "rejected")})
# Optional additive fields, present only for the respective structural units.
DECISION_PREVIEW["properties"]["paragraph_numbering"] = PARAGRAPH_NUMBERING
DECISION_PREVIEW["properties"]["row_numbering"] = PARAGRAPH_NUMBERING
DECISION_PREVIEW["properties"]["counter_decision"] = {"enum": ["accept", "reject"]}
DECISION_PREVIEW["properties"]["counter_outcomes"] = {
    "type": "array", "minItems": 4, "maxItems": 4,
    "items": obj(original_decision={"enum": ["accept", "reject"]},
                 counter_decision={"enum": ["accept", "reject"]},
                 text=NEW_TEXT, refusal_code=nullable(NONEMPTY)),
}
DECISION_PREVIEW["allOf"].append({
    "if": {"properties": {"revision_kind": {"const": "counter_insertion"}}},
    "then": {"required": ["counter_decision", "counter_outcomes"]},
    "else": {"properties": {"counter_decision": False, "counter_outcomes": False}},
})
_REPLACE_PARAGRAPH_INPUT = obj(
    kind={"const": "replace_paragraph"}, paragraph_ref=PARAGRAPH_REF_SCHEMA,
    old_text=NEW_TEXT, new_text=NEW_TEXT,
)
_REPLACE_PARAGRAPH_INPUT["properties"]["mode"] = {
    "enum": ["minimal", "whole"], "default": "minimal",
}
CELL_TEXTS = {"type": "array", "minItems": 2, "maxItems": 2, "items": NEW_TEXT}
_INSERT_TABLE_ROW_INPUT = obj(
    kind={"const": "insert_table_row"}, row_ref=TABLE_REFS["row"],
    position={"enum": ["before", "after"]}, template_row_ref=TABLE_REFS["row"],
    cell_texts=CELL_TEXTS,
)
_INSERT_TABLE_ROW_INPUT["properties"]["separator"] = obj(
    template_row_ref=TABLE_REFS["row"], position={"enum": ["before", "after"]},
)
_DELETE_TABLE_ROW_INPUT = obj(
    kind={"const": "delete_table_row"}, row_ref=TABLE_REFS["row"], cell_texts=CELL_TEXTS,
)
_DELETE_TABLE_ROW_INPUT["properties"]["separator_row_ref"] = TABLE_REFS["row"]
OPERATIONS = {"type": "array", "minItems": 1, "maxItems": 100, "items": {"oneOf": [
    DECISION_INPUT,
    obj(kind={"const": "replace_text"}, paragraph_ref=PARAGRAPH_REF_SCHEMA,
        old_text=NONEMPTY, new_text=NEW_TEXT),
    _REPLACE_PARAGRAPH_INPUT,
    obj(kind={"const": "insert_paragraph"}, paragraph_ref=PARAGRAPH_REF_SCHEMA,
        position={"enum": ["before", "after"]}, text=NONEMPTY),
    obj(kind={"const": "delete_paragraph"}, paragraph_ref=PARAGRAPH_REF_SCHEMA, old_text=NONEMPTY),
    _INSERT_TABLE_ROW_INPUT,
    _DELETE_TABLE_ROW_INPUT,
    obj(kind={"const": "add_comment"}, paragraph_ref=PARAGRAPH_REF_SCHEMA,
        quote=NONEMPTY, text=NONEMPTY),
    obj(kind={"const": "update_comment"}, comment_id=NONEMPTY, text=NONEMPTY),
    obj(kind={"const": "delete_comment"}, comment_id=NONEMPTY),
    obj(kind={"const": "delete_comment_reply"}, comment_id=NONEMPTY),
    obj(kind={"const": "delete_comment_thread"}, comment_id=NONEMPTY,
        member_comment_ids={"type": "array", "minItems": 1, "maxItems": 100,
                            "uniqueItems": True, "items": {"type": "string", "pattern": "^[0-9]{1,10}$"}}),
    obj(kind={"const": "reply_comment"}, comment_id=NONEMPTY, text=NONEMPTY),
    obj(kind={"const": "set_comment_resolved"}, comment_id=NONEMPTY,
        resolved={"type": "boolean"}),
]}}
PROOF = obj(schema_version={"const": "drafting_preflight.v1"}, source_path=TEXT,
            source_sha256=SHA, output_path=TEXT, operations_sha256=SHA, author=NONEMPTY,
            initials=NONEMPTY, producer_build=NONEMPTY, timestamp=TIMESTAMP,
            candidate_sha256=SHA, proof_sha256=SHA)
OPERATION_ID = {"type": "string", "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\s\S])"}
_LEGACY_PROOF = PROOF
_JOURNAL_PROOF = obj(**{**_LEGACY_PROOF["properties"],
    "schema_version": {"const": "drafting_preflight.v2"}, "operation_id": OPERATION_ID})
PROOF = {"oneOf": [_LEGACY_PROOF, _JOURNAL_PROOF]}
DraftingOperationsInput = Annotated[list[dict[str, Any]], WithJsonSchema(OPERATIONS)]
DraftingProofInput = Annotated[dict[str, Any], WithJsonSchema(PROOF)]
AUTHOR_PROFILE = obj(
    name={"type": "string", "minLength": 1, "maxLength": 255},
    initials={"type": "string", "minLength": 1, "maxLength": 32},
)
DraftingAuthorProfileInput = Annotated[
    dict[str, Any], WithJsonSchema(AUTHOR_PROFILE)
]
DraftingShaInput = Annotated[
    str,
    StringConstraints(strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
    WithJsonSchema(SHA),
]
_COMMON = {
    "schema_version": {"const": "document_drafting.v1"},
    "producer": obj(name={"const": "veqtor-mcp"}, version=NONEMPTY, build=NONEMPTY),
    # The immutable counterproposal journal does not describe mutable drafts.
    "record_id": {"type": "null"}, "record_status": {"const": "disabled"},
}
_COMMENT = obj(id=TEXT, author=TEXT, initials=TEXT, text=TEXT,
               paragraph_indexes={"type": "array", "items": {"type": "integer", "minimum": 0}},
               parent_comment_id=nullable(TEXT), thread_root_id=TEXT,
               resolved=nullable({"type": "boolean"}))
_INDEX = {"type": "integer", "minimum": 0, "maximum": 99}
_REVISION_IDS = {"type": "array", "minItems": 1, "maxItems": 2, "items": NONEMPTY}
_TEXT_OPERATION = obj(
    index=_INDEX, operation={"enum": ["replace", "delete", "edit_own_insertion", "counter"]},
    old_text=NONEMPTY, new_text=NEW_TEXT, start={"type": "integer", "minimum": 0},
    end={"type": "integer", "minimum": 1}, revision_ids=_REVISION_IDS,
    target_revision_id=nullable(NONEMPTY), target_author=nullable(NONEMPTY),
    paragraph_index={"type": "integer", "minimum": 0},
)
# Optional only when the own-insertion edit moved Word's cursor bookmark.
# Add after obj() so the field remains optional in the otherwise closed object.
_TEXT_OPERATION["properties"]["relocated_bookmarks"] = {
    "type": "array", "minItems": 1, "maxItems": 1,
    "items": obj(name={"const": "_GoBack"}, id=NONEMPTY,
                 position={"const": "replacement_start"}),
}
_TEXT_OPERATION["allOf"] = [
    {"if": {"required": ["relocated_bookmarks"]},
     "then": {"properties": {"operation": {"const": "edit_own_insertion"}}}},
    {"if": {"properties": {"operation": {"enum": ["edit_own_insertion", "counter"]}}},
     "then": {"properties": {"target_revision_id": NONEMPTY, "target_author": NONEMPTY}},
     "else": {"properties": {"target_revision_id": {"type": "null"}, "target_author": {"type": "null"}}}},
    {"if": {"properties": {"operation": {"const": "edit_own_insertion"}}},
     "then": {"properties": {"revision_ids": {"maxItems": 1}}}},
    {"if": {"properties": {"operation": {"const": "delete"}}},
     "then": {"properties": {"new_text": {"const": ""}, "revision_ids": {"maxItems": 1}}}},
    {"if": {"properties": {"operation": {"const": "replace"}}},
     "then": {"properties": {"new_text": NONEMPTY, "revision_ids": {"minItems": 2}}}},
    {"if": {"properties": {"operation": {"const": "counter"}, "new_text": {"const": ""}}},
     "then": {"properties": {"revision_ids": {"maxItems": 1}}}},
    {"if": {"properties": {"operation": {"const": "counter"}, "new_text": NONEMPTY}},
     "then": {"properties": {"revision_ids": {"minItems": 2}}}},
]
_COMMENT_OPERATION = obj(
    index=_INDEX, operation={"enum": ["add_comment", "update_comment", "delete_comment",
                                    "reply_comment", "set_comment_resolved"]},
    **_COMMENT["properties"],
)
_DELETE_REPLY_OPERATION = obj(
    index=_INDEX, operation={"const": "delete_comment_reply"},
    **_COMMENT["properties"], date=nullable(TEXT),
)
_DELETE_THREAD_OPERATION = obj(
    index=_INDEX, operation={"const": "delete_comment_thread"}, thread_root_id=TEXT,
    deleted_comments={"type": "array", "minItems": 1, "maxItems": 100,
                      "items": obj(**_COMMENT["properties"], date=nullable(TEXT))},
)
_INSERT_PARAGRAPH = obj(
    index=_INDEX, operation={"const": "insert_paragraph"},
    position={"enum": ["before", "after"]}, text=NONEMPTY,
    revision_ids={**_REVISION_IDS, "minItems": 2},
    paragraph_mark_location={"enum": ["anchor", "inserted"]},
    paragraph_mark_revision_id=NONEMPTY, text_revision_id=NONEMPTY,
    paragraph_index={"type": "integer", "minimum": 0},
)
_DELETE_PARAGRAPH = obj(
    index=_INDEX, operation={"const": "delete_paragraph"}, old_text=NONEMPTY,
    revision_ids={**_REVISION_IDS, "minItems": 2}, paragraph_mark_revision_id=NONEMPTY,
    text_revision_id=NONEMPTY, paragraph_index={"type": "integer", "minimum": 0},
    paragraph_numbering=PARAGRAPH_NUMBERING,
)
_TABLE_ROW_FIELDS = dict(
    cell_texts=CELL_TEXTS,
    row_count={"type": "integer", "minimum": 1, "maximum": 2},
    revision_ids={"type": "array", "minItems": 1, "maxItems": 258,
                  "uniqueItems": True, "items": NONEMPTY},
    row_numbering=PARAGRAPH_NUMBERING,
    separator_included={"type": "boolean"},
    row_revision_groups={"type": "array", "minItems": 1, "maxItems": 2,
                         "items": obj(
                             row_mark_revision_id=NONEMPTY,
                             content_revision_ids={"type": "array", "maxItems": 128,
                                                   "uniqueItems": True, "items": NONEMPTY},
                             cell_texts=CELL_TEXTS, role={"enum": ["clause", "separator"]},
                         )},
)
_INSERT_TABLE_ROW = obj(index=_INDEX, operation={"const": "insert_table_row"},
                        position={"enum": ["before", "after"]}, **_TABLE_ROW_FIELDS)
_DELETE_TABLE_ROW = obj(index=_INDEX, operation={"const": "delete_table_row"}, **_TABLE_ROW_FIELDS)
for schema in (_INSERT_TABLE_ROW, _DELETE_TABLE_ROW):
    schema["allOf"] = [{
        "if": {"properties": {"separator_included": {"const": True}}},
        "then": {"properties": {
            "row_count": {"const": 2},
            "row_revision_groups": {"minItems": 2, "allOf": [
                {"contains": {"properties": {"role": {"const": role}}},
                 "minContains": 1, "maxContains": 1} for role in ("clause", "separator")
            ]},
        }},
        "else": {"properties": {
            "row_count": {"const": 1},
            "row_revision_groups": {"maxItems": 1,
                                    "items": {"properties": {"role": {"const": "clause"}}}},
        }},
    }]
_PARAGRAPH_STEP = {
    "type": "object",
    "properties": {k: v for k, v in _TEXT_OPERATION["properties"].items()
                   if k not in {"index", "paragraph_index"}},
    "required": [k for k in _TEXT_OPERATION["required"]
                 if k not in {"index", "paragraph_index", "start", "end"}],
    "additionalProperties": False,
}
_PARAGRAPH_STEP["properties"] = {
    **_PARAGRAPH_STEP["properties"],
    "operation": {"enum": ["replace", "delete", "edit_own_insertion", "insert"]},
    "old_text": NEW_TEXT,
    "end": {"type": "integer", "minimum": 0},
    "target_start": {"type": "integer", "minimum": 0},
    "target_end": {"type": "integer", "minimum": 0},
}
_PARAGRAPH_STEP["dependentRequired"] = {
    "start": ["end"], "end": ["start"],
    "target_start": ["target_end"], "target_end": ["target_start"],
}
_PARAGRAPH_STEP["allOf"] = [
    {"if": {"properties": {"operation": {"const": "insert"}}},
     "then": {"properties": {"old_text": {"const": ""}, "new_text": NONEMPTY}},
     "else": {"properties": {"old_text": NONEMPTY}}},
    {"if": {"properties": {"old_text": {"const": ""}}},
     "then": {"properties": {"new_text": NONEMPTY}},
     "else": {"properties": {"end": {"minimum": 1}}}},
    {"if": {"properties": {"operation": {"const": "delete"}}},
     "then": {"properties": {"new_text": {"const": ""}}}},
    {"if": {"properties": {"operation": {"const": "replace"}}},
     "then": {"properties": {"new_text": NONEMPTY, "revision_ids": {"minItems": 2}}},
     "else": {"properties": {"revision_ids": {"maxItems": 1}}}},
    {"if": {"properties": {"operation": {"const": "edit_own_insertion"}}},
     "then": {"properties": {"target_revision_id": NONEMPTY, "target_author": NONEMPTY}},
     "else": {"properties": {"target_revision_id": {"type": "null"},
                             "target_author": {"type": "null"}}}},
    {"if": {"anyOf": [{"required": ["target_start"]}, {"required": ["relocated_bookmarks"]}]},
     "then": {"properties": {"operation": {"const": "edit_own_insertion"}}}},
]
_REPLACE_PARAGRAPH = obj(
    index=_INDEX, operation={"const": "replace_paragraph"},
    old_text=NEW_TEXT, new_text=NEW_TEXT, mode={"enum": ["minimal", "whole"]},
    paragraph_index={"type": "integer", "minimum": 0},
    revision_ids={"type": "array", "minItems": 1, "maxItems": 2 * MAX_PARAGRAPH_EDIT_STEPS,
                  "uniqueItems": True, "items": NONEMPTY},
    steps={"type": "array", "minItems": 1, "maxItems": MAX_PARAGRAPH_EDIT_STEPS,
           "items": _PARAGRAPH_STEP},
)
_DECISION_APPLIED = obj(**{k: v for k, v in DECISION_PREVIEW["properties"].items()
                         if k not in {"paragraph_numbering", "row_numbering", "counter_decision", "counter_outcomes"}}, index=_INDEX,
                        target=DECISION_TARGET, actor_profile=AUTHOR_PROFILE)
_DECISION_APPLIED["properties"]["paragraph_numbering"] = PARAGRAPH_NUMBERING
_DECISION_APPLIED["properties"]["row_numbering"] = PARAGRAPH_NUMBERING
for field in ("counter_decision", "counter_outcomes"):
    _DECISION_APPLIED["properties"][field] = DECISION_PREVIEW["properties"][field]
_DECISION_APPLIED["allOf"] = DECISION_PREVIEW["allOf"]
_APPLIED = {"type": "array", "maxItems": 100,
            "items": {"oneOf": [_TEXT_OPERATION, _COMMENT_OPERATION, _DELETE_REPLY_OPERATION, _DELETE_THREAD_OPERATION,
                              _INSERT_PARAGRAPH, _DELETE_PARAGRAPH, _INSERT_TABLE_ROW,
                              _DELETE_TABLE_ROW, _REPLACE_PARAGRAPH, _DECISION_APPLIED]}}
COMMENTS_RESULT = obj(**_COMMON, source_path=TEXT, file_sha256=SHA,
                      comments={"type": "array", "maxItems": 100, "items": _COMMENT},
                      total_count={"type": "integer", "minimum": 0},
                      next_offset=nullable({"type": "integer", "minimum": 0}))
REVISIONS_RESULT = obj(
    **{**_COMMON, "schema_version": {"const": "document_revisions.v1"}},
    source_path=TEXT, file_sha256=SHA,
    revisions={"type": "array", "maxItems": 100, "items": obj(
        revision_ref=REVISION_REF, kind=TEXT, id=nullable(TEXT), author=nullable(TEXT), date=nullable(TEXT),
        supported={"type": "boolean"}, refusal_code=nullable(TEXT), preview=nullable(DECISION_PREVIEW))},
    total_count={"type": "integer", "minimum": 0, "maximum": 20_000},
    next_offset=nullable({"type": "integer", "minimum": 0}),
    coverage={"const": "main_document_only"}, uninspected_parts={"type": "array", "items": TEXT},
)
REVISIONS_RESULT["properties"]["revisions"]["items"]["properties"]["structural_target"] = {
    "oneOf": DECISION_TARGET["oneOf"][2:4],
}
REVISIONS_RESULT["properties"]["revisions"]["items"]["properties"]["counter_target"] = DECISION_TARGET["oneOf"][4]
PREFLIGHT_RESULT = obj(**_COMMON, batch_applicable={"type": "boolean"}, source_sha256=SHA,
                       tracked_change_author=TEXT, comment_initials=TEXT, operations=_APPLIED,
                       preflight_proof=nullable(PROOF), refusal_code=nullable(TEXT),
                       blocking_operation_index=nullable({"type": "integer", "minimum": 0}))
PREFLIGHT_RESULT["allOf"] = [{
    "if": {"properties": {"batch_applicable": {"const": True}}},
    "then": {"properties": {"preflight_proof": PROOF, "refusal_code": {"type": "null"},
                             "operations": {"minItems": 1}, "blocking_operation_index": {"type": "null"}}},
    "else": {"properties": {"preflight_proof": {"type": "null"}, "refusal_code": NONEMPTY,
                             "operations": {"maxItems": 0}}},
}]
OPERATION_JOURNAL = obj(
    schema_version={"const": "operation_journal_status.v1"}, operation_id=OPERATION_ID,
    publication_status={"enum": ["published", "not_published", "uncertain"]},
    verification={"enum": ["matched", "mismatch", "not_checked"]},
    observed_output_sha256=nullable(SHA),
    backup_verification={"enum": ["not_required", "not_checked", "matched", "mismatch", "same_as_output"]},
    journal_status={"enum": ["written", "incomplete"]},
    journal_error=nullable({"enum": ["journal_finalize_failed", "operation_outcome_unresolved"]}),
    operation_error=nullable({"type": "string", "pattern": RECORD_ERROR_PATTERN}),
)
_JOURNAL_METADATA = {"record_id": nullable(OPERATION_ID),
    "record_status": {"enum": ["written", "disabled", "write_failed"]}}
APPLY_RESULT = obj(**_COMMON, status={"const": "ok"}, output_path=TEXT,
                   backup_path=nullable(TEXT), backup_sha256=nullable(SHA),
                   save_mode={"enum": ["copy", "in_place"]}, source_sha256=SHA, output_sha256=SHA,
                   tracked_change_author=TEXT, comment_initials=TEXT, operations=_APPLIED,
                   preflight_binding_status={"const": "verified"})
APPLY_RESULT["properties"].update(_JOURNAL_METADATA)
APPLY_RESULT["properties"]["record_error"] = {"type": "string", "pattern": RECORD_ERROR_PATTERN}
APPLY_RESULT["properties"]["operation_journal"] = OPERATION_JOURNAL
APPLY_RESULT["properties"]["operations"] = {**_APPLIED, "minItems": 1}
APPLY_RESULT["allOf"] = [{
    "if": {"properties": {"save_mode": {"const": "in_place"}}},
    "then": {"properties": {"backup_path": NONEMPTY, "backup_sha256": SHA}},
    "else": {"properties": {"backup_path": {"type": "null"}, "backup_sha256": {"type": "null"}}},
}]
for schema in (COMMENTS_RESULT, REVISIONS_RESULT, PREFLIGHT_RESULT, APPLY_RESULT):
    schema[MCP_CONTRACT_SCHEMA_EXTENSION] = MCP_CONTRACT_SCHEMA_VERSION

# Structured failures are part of the advertised wire contract too. In
# particular, TypeScript clients validate structuredContent even with isError.
DRAFTING_ERROR_RESULT = obj(
    schema_version={"const": "drafting_error.v1"}, status={"const": "error"},
    error_code={"type": "string", "maxLength": 64, "pattern": RECORD_ERROR_PATTERN},
    message=NONEMPTY,
    recovery=obj(output_path=NONEMPTY, backup_path=nullable(NONEMPTY),
                 backup_sha256=nullable(SHA), backup_path_verified={"type": "boolean"},
                 publication_completed={"type": "boolean"}, lock_path=nullable(NONEMPTY),
                 lock_reason=nullable(LOCK_REASON)),
)
DRAFTING_ERROR_RESULT["properties"]["recovery"]["properties"]["operation_journal"] = OPERATION_JOURNAL
DRAFTING_ERROR_RESULT["properties"]["recovery"]["properties"]["publication_status"] = {
    "enum": ["not_published", "published", "uncertain"],
}
DRAFTING_ERROR_RESULT["properties"]["recovery"]["allOf"] = [
    {"if": {"required": ["publication_status"],
            "properties": {"publication_status": {"const": "published"}}},
     "then": {"properties": {"publication_completed": {"const": True}}}},
    {"if": {"required": ["publication_status"],
            "properties": {"publication_status": {"enum": ["not_published", "uncertain"]}}},
     "then": {"properties": {"publication_completed": {"const": False}}}},
    {"if": {"properties": {"backup_path": {"type": "null"}}},
     "then": {"properties": {"backup_sha256": {"type": "null"},
                              "backup_path_verified": {"const": False}}},
     "else": {"properties": {"backup_sha256": SHA}}},
    {"if": {"properties": {"lock_path": {"type": "null"}}},
     "then": {"properties": {"lock_reason": {"type": "null"}}},
     "else": {"properties": {"lock_reason": LOCK_REASON}}},
]
APPLY_OUTPUT = {
    "type": "object", "oneOf": [APPLY_RESULT, DRAFTING_ERROR_RESULT],
    MCP_CONTRACT_SCHEMA_EXTENSION: MCP_CONTRACT_SCHEMA_VERSION,
}


class ReadDocumentRevisionsResult(_ContractResult):
    contract_schema: ClassVar[dict[str, Any]] = REVISIONS_RESULT


class ReadDocumentCommentsResult(_ContractResult):
    contract_schema: ClassVar[dict[str, Any]] = COMMENTS_RESULT


class PreflightDocumentEditsResult(_ContractResult):
    contract_schema: ClassVar[dict[str, Any]] = PREFLIGHT_RESULT


class ApplyDocumentEditsSuccessResult(_ContractResult):
    contract_schema: ClassVar[dict[str, Any]] = APPLY_RESULT

    @model_validator(mode="after")
    def _validate_record_metadata_tuple(self):
        error = (self.model_extra or {}).get("record_error")
        if self.record_status == "written":
            try:
                valid = str(uuid.UUID(self.record_id)) == self.record_id and error is None
            except (ValueError, TypeError, AttributeError):
                valid = False
        elif self.record_status == "disabled":
            valid = self.record_id is None and error is None
        else:
            valid = self.record_id is None and isinstance(error, str) and bool(error)
        if not valid:
            raise ValueError("operation journal metadata is inconsistent")
        return self


RECOVERY_RESULT = obj(
    **{**_COMMON, **_JOURNAL_METADATA},
    source_path=NONEMPTY, operation_journal=OPERATION_JOURNAL,
    recovered={"type": "boolean"}, current_output_sha256=nullable(SHA),
    recovery_status={"enum": ["existing_result", "recovered", "unresolved"]},
)
RECOVERY_RESULT["properties"]["schema_version"] = {"const": "document_operation_recovery.v1"}
RECOVERY_RESULT["properties"]["record_error"] = {"type": "string", "pattern": RECORD_ERROR_PATTERN}
RECOVERY_RESULT[MCP_CONTRACT_SCHEMA_EXTENSION] = MCP_CONTRACT_SCHEMA_VERSION


class RecoverDocumentOperationResult(ApplyDocumentEditsSuccessResult):
    contract_schema: ClassVar[dict[str, Any]] = RECOVERY_RESULT


class ApplyDocumentEditsResult(ApplyDocumentEditsSuccessResult):
    """Successful values use the model; explicit tool errors use the other branch."""

    contract_schema: ClassVar[dict[str, Any]] = APPLY_OUTPUT
