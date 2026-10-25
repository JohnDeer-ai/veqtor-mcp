# SPDX-License-Identifier: Apache-2.0
"""Closed contracts for explicitly selected negotiation document roles."""
from typing import Annotated, Any, ClassVar

from pydantic import WithJsonSchema

from .contracts import MCP_CONTRACT_SCHEMA_EXTENSION, MCP_CONTRACT_SCHEMA_VERSION, _ContractResult
from ._positions_contract import SHA, UUID, VERSION, ID, obj, text, nullable, array

ROUND_ID = {"type": "string", "pattern": r"^round_[0-9a-f]{32}(?![\s\S])"}
POSITION_BASIS = obj(matter_id=UUID, revision=SHA,
                     history_sequence={"type": "integer", "minimum": 1, "maximum": 500},
                     versions=array(obj(position_id=ID, version=VERSION), 50))
SENT = obj(request_id=UUID, file_sha256=SHA, statement=text(2000),
           basis={"const": "client_asserted_actual_sent_file"})
SENT_REF = obj(round_id=ROUND_ID, request_id=UUID, file_sha256=SHA)
ROUND = obj(round_id=ROUND_ID, incoming_sha256=SHA,
            previous_round_id=nullable(ROUND_ID), sent_basis=nullable(SENT_REF),
            positions_basis=nullable(POSITION_BASIS), sent_documents=array(SENT, 100))
REQUEST = obj(request_id=UUID, command_sha256=SHA, round_id=ROUND_ID,
              action={"enum": ["start_round", "record_sent"]})
STORE = obj(schema_version={"const": "deal_rounds_store.v1"}, revision=SHA,
            rounds=array(ROUND, 50), requests=array(REQUEST, 150))
COMMAND = {"oneOf": [
    obj(action={"const": "start_round"}, round_id=ROUND_ID,
        previous_round_id=nullable(ROUND_ID), path=text(4096), file_sha256=SHA),
    obj(action={"const": "record_sent"}, round_id=ROUND_ID, path=text(4096),
        file_sha256=SHA, user_confirmed={"const": True, "type": "boolean"}, statement=text(2000)),
]}
COMMAND[MCP_CONTRACT_SCHEMA_EXTENSION] = MCP_CONTRACT_SCHEMA_VERSION
CommandInput = Annotated[dict[str, Any], WithJsonSchema(COMMAND)]
RequestIdInput = Annotated[str, WithJsonSchema(UUID)]
RESULT = obj(
    schema_version={"const": "deal_state_result.v1"}, status={"const": "ok"},
    revision=nullable(SHA), rounds=array(ROUND, 50), requests=array(REQUEST, 150),
    documents=array(obj(file_sha256=SHA, path=text(4096)), 150),
    positions_current_revision=nullable(SHA), applied_request_id=nullable(UUID),
    positions_basis_observations=array(obj(round_id=ROUND_ID,
        status={"enum": ["not_recorded", "current", "historical_available", "unavailable"]}), 50),
    limitations=array(text(200), 10),
    producer=obj(name={"const": "veqtor-mcp"}, version=text(64), build=text(128)),
    record_id={"type": "null"}, record_status={"const": "disabled"},
)
RESULT[MCP_CONTRACT_SCHEMA_EXTENSION] = MCP_CONTRACT_SCHEMA_VERSION


class DealStateResult(_ContractResult):
    contract_schema: ClassVar[dict[str, Any]] = RESULT
