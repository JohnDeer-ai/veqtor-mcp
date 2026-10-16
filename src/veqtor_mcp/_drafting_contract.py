# SPDX-License-Identifier: Apache-2.0
"""Additive, closed contracts for the author drafting workflow."""
from typing import Annotated, Any, ClassVar

from pydantic import StringConstraints, WithJsonSchema

from .contracts import (
    MCP_CONTRACT_SCHEMA_EXTENSION, MCP_CONTRACT_SCHEMA_VERSION,
    PARAGRAPH_REF_SCHEMA, _ContractResult,
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
OPERATIONS = {"type": "array", "minItems": 1, "maxItems": 100, "items": {"oneOf": [
    obj(kind={"const": "replace_text"}, paragraph_ref=PARAGRAPH_REF_SCHEMA,
        old_text=NONEMPTY, new_text=NEW_TEXT),
    obj(kind={"const": "add_comment"}, paragraph_ref=PARAGRAPH_REF_SCHEMA,
        quote=NONEMPTY, text=NONEMPTY),
    obj(kind={"const": "update_comment"}, comment_id=NONEMPTY, text=NONEMPTY),
    obj(kind={"const": "delete_comment"}, comment_id=NONEMPTY),
]}}
PROOF = obj(schema_version={"const": "drafting_preflight.v1"}, source_path=TEXT,
            source_sha256=SHA, output_path=TEXT, operations_sha256=SHA, author=NONEMPTY,
            initials=NONEMPTY, producer_build=NONEMPTY, candidate_sha256=SHA, proof_sha256=SHA)
DraftingOperationsInput = Annotated[list[dict[str, Any]], WithJsonSchema(OPERATIONS)]
DraftingProofInput = Annotated[dict[str, Any], WithJsonSchema(PROOF)]
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
               paragraph_indexes={"type": "array", "items": {"type": "integer", "minimum": 0}})
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
    index=_INDEX, operation={"enum": ["add_comment", "update_comment", "delete_comment"]},
    **_COMMENT["properties"],
)
_APPLIED = {"type": "array", "maxItems": 100,
            "items": {"oneOf": [_TEXT_OPERATION, _COMMENT_OPERATION]}}
COMMENTS_RESULT = obj(**_COMMON, source_path=TEXT, file_sha256=SHA,
                      comments={"type": "array", "maxItems": 100, "items": _COMMENT},
                      total_count={"type": "integer", "minimum": 0},
                      next_offset=nullable({"type": "integer", "minimum": 0}))
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
APPLY_RESULT = obj(**_COMMON, status={"const": "ok"}, output_path=TEXT,
                   backup_path=nullable(TEXT), backup_sha256=nullable(SHA),
                   save_mode={"enum": ["copy", "in_place"]}, source_sha256=SHA, output_sha256=SHA,
                   tracked_change_author=TEXT, comment_initials=TEXT, operations=_APPLIED,
                   preflight_binding_status={"const": "verified"})
APPLY_RESULT["properties"]["operations"] = {**_APPLIED, "minItems": 1}
APPLY_RESULT["allOf"] = [{
    "if": {"properties": {"save_mode": {"const": "in_place"}}},
    "then": {"properties": {"backup_path": NONEMPTY, "backup_sha256": SHA}},
    "else": {"properties": {"backup_path": {"type": "null"}, "backup_sha256": {"type": "null"}}},
}]
for schema in (COMMENTS_RESULT, PREFLIGHT_RESULT, APPLY_RESULT):
    schema[MCP_CONTRACT_SCHEMA_EXTENSION] = MCP_CONTRACT_SCHEMA_VERSION


class ReadDocumentCommentsResult(_ContractResult):
    contract_schema: ClassVar[dict[str, Any]] = COMMENTS_RESULT


class PreflightDocumentEditsResult(_ContractResult):
    contract_schema: ClassVar[dict[str, Any]] = PREFLIGHT_RESULT


class ApplyDocumentEditsResult(_ContractResult):
    contract_schema: ClassVar[dict[str, Any]] = APPLY_RESULT
