# SPDX-License-Identifier: Apache-2.0
"""Closed NR-02 input, snapshot and full-read contracts."""
from typing import Annotated, Any, ClassVar

from pydantic import WithJsonSchema

from .contracts import (
    MCP_CONTRACT_SCHEMA_EXTENSION, MCP_CONTRACT_SCHEMA_VERSION,
    PARAGRAPH_REF_SCHEMA, _CHANGE_UNIT_ANCHOR_V2_SCHEMA, _ContractResult,
)


def obj(**properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def text(limit):
    return {"type": "string", "minLength": 1, "maxLength": limit, "pattern": r"\S"}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def array(schema, limit):
    return {"type": "array", "items": schema, "maxItems": limit}


SHA = {"type": "string", "pattern": r"^[0-9a-f]{64}(?![\s\S])"}
ID = {"type": "string", "pattern": r"^pos_[0-9a-f]{32}(?![\s\S])"}
UUID = {"type": "string", "pattern": r"^[0-9a-f]{32}(?![\s\S])"}
VERSION = {"type": "integer", "minimum": 1, "maximum": 500}
SOURCE = obj(path={**text(500), "pattern": r"^(?!/)(?!.*\\)[^\u0000-\u001f]+(?![\s\S])"},
             file_sha256=SHA,
             reference=nullable({"oneOf": [PARAGRAPH_REF_SCHEMA, _CHANGE_UNIT_ANCHOR_V2_SCHEMA]}))
CONTENT = obj(
    title=text(200), desired_outcome=text(4000), fallback=nullable(text(4000)),
    fallback_conditions=nullable(text(4000)), rationale=nullable(text(4000)),
    related_position_ids={**array(ID, 10), "uniqueItems": True},
    content_origin={"enum": ["model_proposal", "user_instruction"]},
    business_decision={"enum": ["pending", "not_required"]}, sources=array(SOURCE, 5),
)
CONFIRMATION = obj(version=VERSION, statement=text(2000), basis={"const": "client_asserted_user_confirmation"})
POSITION = obj(position_id=ID, version=VERSION, content=CONTENT,
               confirmation=nullable(CONFIRMATION), lifecycle={"enum": ["active", "withdrawn"]})
HISTORY = obj(sequence=VERSION, operation={"enum": ["create", "update", "confirm", "withdraw"]},
              position=POSITION)
STORE = obj(schema_version={"const": "deal_positions_store.v1"}, matter_id=UUID,
            revision=SHA, positions=array(POSITION, 50), history=array(HISTORY, 500))
OPERATION = {"oneOf": [
    obj(op={"const": "create"}, position_id=ID, content=CONTENT),
    obj(op={"const": "update"}, position_id=ID, expected_version=VERSION, content=CONTENT),
    obj(op={"const": "confirm"}, position_id=ID, expected_version=VERSION,
        user_confirmed={"type": "boolean", "const": True}, statement=text(2000)),
    obj(op={"const": "withdraw"}, position_id=ID, expected_version=VERSION),
]}
OPERATIONS = {**array(OPERATION, 20), "minItems": 1,
              MCP_CONTRACT_SCHEMA_EXTENSION: MCP_CONTRACT_SCHEMA_VERSION}
OperationsInput = Annotated[list[dict[str, Any]], WithJsonSchema(OPERATIONS)]
RevisionInput = Annotated[str | None, WithJsonSchema(nullable(SHA))]
OBSERVATION = obj(source=SOURCE, status={"enum": ["same_bytes", "changed", "unavailable", "not_checked"]})
RESULT = obj(
    schema_version={"const": "deal_positions_result.v1"}, status={"const": "ok"},
    state={"enum": ["uninitialized", "initialized"]}, matter_id=nullable(UUID), revision=nullable(SHA),
    positions=array(POSITION, 50), history=array(HISTORY, 500), history_included={"type": "boolean"},
    source_observations=array(OBSERVATION, 2750), server_session_id=UUID,
    producer=obj(name={"const": "veqtor-mcp"}, version=text(64), build=text(128)),
    record_id={"type": "null"}, record_status={"const": "disabled"},
)
RESULT[MCP_CONTRACT_SCHEMA_EXTENSION] = MCP_CONTRACT_SCHEMA_VERSION
RESULT["allOf"] = [
    {"if": {"properties": {"state": {"const": "uninitialized"}}},
     "then": {"properties": {"matter_id": {"type": "null"}, "revision": {"type": "null"},
         "positions": {"maxItems": 0}, "history": {"maxItems": 0}, "source_observations": {"maxItems": 0}}},
     "else": {"properties": {"matter_id": UUID, "revision": SHA, "positions": {"minItems": 1}}}},
    {"if": {"properties": {"history_included": {"const": False}}},
     "then": {"properties": {"history": {"maxItems": 0}}}},
]


class DealPositionsResult(_ContractResult):
    contract_schema: ClassVar[dict[str, Any]] = RESULT
