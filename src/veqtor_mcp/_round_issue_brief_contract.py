# SPDX-License-Identifier: Apache-2.0
"""One bounded internal dataset, independent of the eventual document format."""
from typing import Annotated, Any, ClassVar

from pydantic import WithJsonSchema

from .contracts import MCP_CONTRACT_SCHEMA_EXTENSION, MCP_CONTRACT_SCHEMA_VERSION, _ContractResult
from . import _deal_state_contract as d
from ._positions_contract import POSITION, SHA, obj, text, nullable, array

SELECTION = {**array(d.ISSUE_ID, 20), "minItems": 1, "uniqueItems": True}
IssueSelectionInput = Annotated[list[str], WithJsonSchema(SELECTION)]
BriefRevisionInput = Annotated[str, WithJsonSchema(SHA)]
BriefRoundInput = Annotated[str, WithJsonSchema(d.ROUND_ID)]

PAYLOAD = obj(
    revision=SHA, positions_revision=nullable(SHA), round_id=d.ROUND_ID,
    selected_issue_ids=SELECTION, omitted_issue_ids=array(d.ISSUE_ID, 100),
    round_issue_count={"type": "integer", "minimum": 1, "maximum": 100},
    basis=obj(incoming_sha256=SHA, sent_basis=nullable(d.SENT_REF),
        current_sha256=SHA, working_document=nullable(d.WORKING_RESULT_V5),
        working_status={"enum": ["not_selected", "same_bytes", "changed", "unavailable"]}),
    issues=array(obj(
        issue=d.ISSUE,
        observation=d.RESULT["properties"]["issue_observations"]["items"],
        quote_status=obj(sent={"enum": ["missing", "verified_snapshot"]},
            incoming={"enum": ["missing", "verified_snapshot"]},
            working={"enum": ["missing", "verified_current_snapshot", "verified_historical_snapshot"]}),
        position_version=nullable(POSITION),
        related_comments={"const": "not_recorded_in_issue_state"}), 20),
    operations=array(obj(link=d.OPERATION,
        observation=d.RESULT["properties"]["operation_observations"]["items"]), 20),
    limitations=array(text(200), 10),
)
RESULT = obj(schema_version={"const": "round_issue_brief.v1"}, status={"const": "ok"},
    audience={"const": "internal_review"}, dataset_sha256=SHA, data=PAYLOAD,
    producer=d.RESULT["properties"]["producer"], record_id={"type": "null"},
    record_status={"const": "disabled"})
RESULT[MCP_CONTRACT_SCHEMA_EXTENSION] = MCP_CONTRACT_SCHEMA_VERSION


class RoundIssueBriefResult(_ContractResult):
    contract_schema: ClassVar[dict[str, Any]] = RESULT
