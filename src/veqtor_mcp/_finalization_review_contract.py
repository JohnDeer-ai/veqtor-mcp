# SPDX-License-Identifier: Apache-2.0
"""Closed read-only F10 review; never an approval or a final-file receipt."""
from typing import Any, ClassVar

from .contracts import MCP_CONTRACT_SCHEMA_EXTENSION, MCP_CONTRACT_SCHEMA_VERSION, _ContractResult
from . import _deal_state_contract as d
from ._positions_contract import HISTORY, POSITION, SHA, obj, text, nullable, array

ISSUE_BLOCKERS = ["undecided", "deferred", "remaining_question", "decision_basis_changed", "position_basis_changed"]
BLOCKERS = ["working_file_changed", "working_file_unavailable", "selected_round_has_successor",
            "unresolved_issues", "unresolved_operations", "pending_revisions",
            "package_coverage_blocked", "clean_probe_refused"]
MANUAL_CHECKS = ["agreement_on_exact_basis", "all_issues_and_concession_conditions",
                 "requisites", "annexes", "unfilled_places", "areas_outside_automatic_coverage",
                 "explicit_comments_and_properties_policy", "checked_final_export",
                 "word_review_and_final_saved_bytes_confirmation"]
LIMITATIONS = [
    "internal_data_includes_private_assessments_positions_and_decision_statements",
    "all_saved_issues_and_linked_operations_in_this_matter_are_included",
    "unrecorded_issues_or_operations_cannot_be_discovered_from_state",
    "lawyer_decisions_and_absence_of_markup_do_not_prove_mutual_agreement",
    "position_confirmations_are_not_approval_of_final_contract_wording",
    "observations_can_change_without_state_revision_and_are_not_approval_tokens",
    "revision_inventory_is_not_complete_content_or_hidden_data_review",
    "clean_probe_preserves_comments_properties_and_other_parts_without_publication",
    "basis_agreements_and_candidates_are_separate_state_final_byte_confirmation_unimplemented",
]

PAYLOAD = obj(
    revision=SHA, positions_revision=nullable(SHA), selected_round_id=d.ROUND_ID,
    included_round_ids=array(d.ROUND_ID, 50), scope={"const": "all_saved_matter_rounds"},
    basis=obj(file_sha256=SHA, snapshot_verified={"const": True},
        working_document=nullable(d.WORKING_RESULT_V5),
        working_status={"enum": ["not_selected", "same_bytes", "changed", "unavailable"]}),
    issues=array(obj(issue=d.ISSUE,
        position_status={"enum": ["not_recorded", "current", "historical_available", "unavailable"]},
        position_version=nullable(POSITION),
        blockers=array({"enum": ISSUE_BLOCKERS}, len(ISSUE_BLOCKERS))), 100),
    issue_history=array(obj(event=d.ISSUE,
        position_status={"enum": ["not_recorded", "current", "historical_available", "unavailable"]},
        position_version=nullable(POSITION)), 100),
    positions=array(POSITION, 50),
    position_history=array(HISTORY, 500),
    operations=array(obj(link=d.OPERATION,
        observation=d.RESULT["properties"]["operation_observations"]["items"],
        outcome={"enum": ["recorded_success", "recorded_no_publication", "unresolved"]}), 20),
    document=obj(inventory_complete={"type": "boolean"},
        revision_count={"type": "integer", "minimum": 0, "maximum": 20000},
        coverage_blockers=array(obj(code=text(100), part_name=text(4096),
            count={"type": "integer", "minimum": 1, "maximum": 20000}), 20000),
        clean_probe={"enum": ["not_attempted_pending_revisions", "not_attempted_coverage_blocked",
                              "passed_revision_free", "refused"]},
        clean_probe_error=nullable(text(100))),
    automatic_blockers=array({"enum": BLOCKERS}, len(BLOCKERS)),
    required_manual_checks={"const": MANUAL_CHECKS},
    limitations={"const": LIMITATIONS},
)
RESULT = obj(schema_version={"const": "finalization_review.v1"}, status={"const": "ok"},
    audience={"const": "internal_review"}, readiness={"const": "incomplete"},
    review_sha256=SHA, data=PAYLOAD,
    producer=d.RESULT["properties"]["producer"], record_id={"type": "null"},
    record_status={"const": "disabled"})
RESULT[MCP_CONTRACT_SCHEMA_EXTENSION] = MCP_CONTRACT_SCHEMA_VERSION


class FinalizationReviewResult(_ContractResult):
    contract_schema: ClassVar[dict[str, Any]] = RESULT
