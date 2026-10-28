# SPDX-License-Identifier: Apache-2.0
"""Closed contracts for explicitly selected negotiation document roles."""
from copy import deepcopy
from typing import Annotated, Any, ClassVar

from pydantic import WithJsonSchema

from .contracts import MCP_CONTRACT_SCHEMA_EXTENSION, MCP_CONTRACT_SCHEMA_VERSION, PARAGRAPH_REF_SCHEMA, _ContractResult
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
STORE_V1 = obj(schema_version={"const": "deal_rounds_store.v1"}, revision=SHA,
            rounds=array(ROUND, 50), requests=array(REQUEST, 150))
JOURNAL_ID = {"type": "string", "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\s\S])"}
ISSUE_ID = {"type": "string", "pattern": r"^issue_[0-9a-f]{32}(?![\s\S])"}
RELATIVE_PATH = {**text(500), "pattern": r"^(?!/)(?!.*\\)[^\u0000-\u001f]+(?![\s\S])"}
OPERATION = obj(request_id=UUID, round_id=ROUND_ID, operation_id=JOURNAL_ID,
                source_path=text(4096), intent_sha256=SHA, source_sha256=SHA,
                candidate_sha256=SHA, output_path=RELATIVE_PATH,
                save_mode={"enum": ["create_new", "replace_source"]})
WORKING = obj(request_id=UUID, round_id=ROUND_ID, operation_id=nullable(JOURNAL_ID),
              path=RELATIVE_PATH, file_sha256=SHA, previous_request_id=nullable(UUID),
              origin={"enum": ["journaled_operation", "client_asserted_manual_save"]},
              statement=nullable(text(2000)))
QUOTE = obj(file_sha256=SHA, paragraph_ref=PARAGRAPH_REF_SCHEMA,
            start={"type": "integer", "minimum": 0, "maximum": 1000000}, text=text(8000))
POSITION_LINK = obj(matter_id=UUID, revision=SHA,
                    history_sequence={"type": "integer", "minimum": 1, "maximum": 500},
                    position_id=ID, version=VERSION)
ISSUE_CONTENT = obj(title=text(200), sent_quote=nullable(QUOTE), incoming_quote=nullable(QUOTE),
                    working_quote=nullable(QUOTE), position=nullable(POSITION_LINK),
                    model_assessment=nullable(text(4000)), proposed_wording=nullable(text(8000)),
                    remaining_question=nullable(text(4000)),
                    operation_ids={**array(JOURNAL_ID, 20), "uniqueItems": True})
DECISION = obj(choice={"enum": ["approve", "reject", "defer"]}, statement=text(2000),
               basis_sha256=SHA, basis={"const": "client_asserted_lawyer_decision"})
ISSUE = obj(request_id=UUID, round_id=ROUND_ID, issue_id=ISSUE_ID, version=VERSION,
            content=ISSUE_CONTENT, decision=nullable(DECISION))
WORKFLOW_ACTIONS = ["link_operation", "link_working_document", "save_issue", "decide_issue", "record_working_version", "link_issue_operation"]
REQUEST = obj(request_id=UUID, command_sha256=SHA, round_id=ROUND_ID,
              action={"enum": ["start_round", "record_sent", *WORKFLOW_ACTIONS]})
STORE_V2 = obj(schema_version={"const": "deal_rounds_store.v2"}, revision=SHA,
               rounds=array(ROUND, 50), requests=array(REQUEST, 150),
               operations=array(OPERATION, 100), working_documents=array(WORKING, 100),
               issue_events=array(ISSUE, 100))
IMPORTED_WORKING = obj(request_id=UUID, round_id=ROUND_ID, operation_id={"type": "null"},
    path=RELATIVE_PATH, file_sha256=SHA, previous_request_id={"type": "null"},
    origin={"const": "client_asserted_working_import"}, statement=text(2000), incoming_basis_sha256=SHA)
WORKING_RESULT = {"oneOf": [WORKING, IMPORTED_WORKING]}
WORKFLOW_ACTIONS = [*WORKFLOW_ACTIONS, "import_working_document"]
REQUEST_V3 = obj(request_id=UUID, command_sha256=SHA, round_id=ROUND_ID,
                 action={"enum": ["start_round", "record_sent", *WORKFLOW_ACTIONS]})
STORE_V3 = obj(schema_version={"const": "deal_rounds_store.v3"}, revision=SHA,
    rounds=array(ROUND, 50), requests=array(REQUEST_V3, 150), operations=array(OPERATION, 100),
    working_documents=array(WORKING_RESULT, 100), issue_events=array(ISSUE, 100))
OUTGOING_ACTIONS = ["prepare_outgoing_package", "publish_outgoing_package", "record_outgoing_verification"]
OUTGOING_FILE = obj(kind={"enum": ["cumulative_redline", "clean_draft"]}, name=text(120), sha256=SHA)
OUTGOING = obj(request_id=UUID, round_id=ROUND_ID, working_request_id=UUID, source_sha256=SHA,
    path=RELATIVE_PATH, positions_revision=nullable(SHA), basis_sha256=SHA, plan_sha256=SHA,
    redline_name=nullable(text(120)), clean_draft_name=nullable(text(120)),
    files=array(OUTGOING_FILE, 2), report_utf8=text(2 * 1024 * 1024),
    readiness={"const": "prepared_unreviewed"}, comments_policy={"const": "preserve_all"},
    properties_policy={"const": "preserve_all"})
OUTGOING_EVENT = obj(request_id=UUID, round_id=ROUND_ID, package_request_id=UUID, plan_sha256=SHA,
    status={"enum": ["published", "observed_match"]}, statement=nullable(text(2000)))
REQUEST_V4 = obj(request_id=UUID, command_sha256=SHA, round_id=ROUND_ID,
    action={"enum": ["start_round", "record_sent", *WORKFLOW_ACTIONS, *OUTGOING_ACTIONS]})
STORE_V4 = obj(schema_version={"const": "deal_rounds_store.v4"}, revision=SHA,
    rounds=array(ROUND, 50), requests=array(REQUEST_V4, 150), operations=array(OPERATION, 100),
    working_documents=array(WORKING_RESULT, 100), issue_events=array(ISSUE, 100),
    outgoing_packages=array(OUTGOING, 20), outgoing_events=array(OUTGOING_EVENT, 20))
WORKING_COPY_ACTIONS = ["prepare_working_copy", "publish_working_copy", "record_working_copy_verification"]
WORKING_COPY_PLAN = obj(request_id=UUID, round_id=ROUND_ID, source_sha256=SHA,
    path=RELATIVE_PATH, plan_sha256=SHA)
COPIED_WORKING = obj(request_id=UUID, round_id=ROUND_ID, operation_id={"type": "null"},
    path=RELATIVE_PATH, file_sha256=SHA, previous_request_id={"type": "null"},
    origin={"const": "verified_snapshot_copy"}, statement={"type": "null"},
    copy_plan_request_id=UUID, publication_status={"enum": ["published", "observed_match"]})
WORKING_RESULT_V5 = {"oneOf": [WORKING, IMPORTED_WORKING, COPIED_WORKING]}
REQUEST_V5 = obj(request_id=UUID, command_sha256=SHA, round_id=ROUND_ID,
    action={"enum": ["start_round", "record_sent", *WORKFLOW_ACTIONS, *OUTGOING_ACTIONS, *WORKING_COPY_ACTIONS]})
STORE_V5 = obj(schema_version={"const": "deal_rounds_store.v5"}, revision=SHA,
    rounds=array(ROUND, 50), requests=array(REQUEST_V5, 150), operations=array(OPERATION, 100),
    working_documents=array(WORKING_RESULT_V5, 100), issue_events=array(ISSUE, 100),
    outgoing_packages=array(OUTGOING, 20), outgoing_events=array(OUTGOING_EVENT, 20),
    working_copy_plans=array(WORKING_COPY_PLAN, 20))
COMMENT_ID = {"type": "string", "pattern": r"^(?:0|[1-9][0-9]{0,9})(?![\s\S])"}
COMMENTS_POLICY = {"oneOf": [{"const": "preserve_all"}, obj(mode={"const": "remove_all"}),
    obj(mode={"const": "remove_threads"}, threads={**array(obj(root_id=COMMENT_ID,
        member_ids={**array(COMMENT_ID, 100), "minItems": 1, "uniqueItems": True}), 100), "minItems": 1})]}
PROPERTIES_POLICY = {"oneOf": [{"const": "preserve_all"}, obj(mode={"const": "remove_selected"},
    fields={**array(obj(part={"enum": ["core", "app", "custom"]}, name=text(255)), 50),
            "minItems": 1, "uniqueItems": True})]}
OUTGOING_V6 = deepcopy(OUTGOING)
OUTGOING_V6["properties"].update(comments_policy=COMMENTS_POLICY, properties_policy=PROPERTIES_POLICY)
STORE_V6 = deepcopy(STORE_V5)
STORE_V6["properties"].update(schema_version={"const": "deal_rounds_store.v6"}, outgoing_packages=array(OUTGOING_V6, 20))
TABLE_SELECTION = obj(expected_dataset_sha256=SHA,
    issue_ids={**array(ISSUE_ID, 20), "minItems": 1, "uniqueItems": True},
    columns={**array({"enum": ["sent_quote", "incoming_quote", "proposed_wording"]}, 3), "minItems": 1, "uniqueItems": True},
    name=text(120), title=text(200), language={"enum": ["ru", "en"]})
OUTGOING_TABLE = deepcopy(OUTGOING_V6)
OUTGOING_TABLE["properties"].update(table_selection=TABLE_SELECTION,
    files=array(obj(kind={"enum": ["cumulative_redline", "clean_draft", "disagreement_table"]}, name=text(120), sha256=SHA), 3))
OUTGOING_TABLE["required"].append("table_selection")
OUTGOING_V7 = {"oneOf": [OUTGOING_V6, OUTGOING_TABLE]}
OUTGOING_ACTIONS = [*OUTGOING_ACTIONS, "prepare_outgoing_package_with_table"]
REQUEST_V7 = deepcopy(REQUEST_V5)
REQUEST_V7["properties"]["action"]["enum"].append("prepare_outgoing_package_with_table")
STORE_V7 = deepcopy(STORE_V6)
STORE_V7["properties"].update(schema_version={"const": "deal_rounds_store.v7"},
    requests=array(REQUEST_V7, 150), outgoing_packages=array(OUTGOING_V7, 20))
FINALIZATION_ACTIONS = ["record_final_basis_agreement", "prepare_final_document",
                        "publish_final_document", "record_final_document_verification"]
FINAL_CHECKS = obj(known_questions=text(2000), historical_conditions=text(2000),
    requisites=text(2000), annexes=text(2000), unfilled_places=text(2000), uncovered_areas=text(2000))
ISSUE_RESOLUTIONS = array(obj(issue_id=ISSUE_ID, version=VERSION, statement=text(2000)), 100)
POSITION_RESOLUTIONS = array(obj(position_id=ID, version=VERSION, statement=text(2000)), 50)
FINAL_AGREEMENT = obj(request_id=UUID, round_id=ROUND_ID, file_sha256=SHA,
    positions_revision=nullable(SHA), scope_sha256=SHA, review_sha256=SHA, review_basis_sha256=SHA,
    review_utf8=text(2 * 1024 * 1024), statement=text(2000),
    issue_resolutions=ISSUE_RESOLUTIONS, position_resolutions=POSITION_RESOLUTIONS,
    manual_checks=FINAL_CHECKS, assurance={"const": "client_asserted_agreement_on_exact_basis"})
FINAL_DOCUMENT = obj(request_id=UUID, round_id=ROUND_ID, agreement_request_id=UUID,
    source_sha256=SHA, path=RELATIVE_PATH, name=text(120), file_sha256=SHA,
    properties_policy=PROPERTIES_POLICY, comments_policy=obj(mode={"const": "remove_all"}),
    plan_sha256=SHA, report_utf8=text(2 * 1024 * 1024),
    stage={"const": "candidate_for_word_review"})
FINAL_EVENT = obj(request_id=UUID, round_id=ROUND_ID, document_request_id=UUID, plan_sha256=SHA,
    status={"enum": ["published", "observed_match"]}, statement=nullable(text(2000)))
REQUEST_V8 = deepcopy(REQUEST_V7)
REQUEST_V8["properties"]["action"]["enum"].extend(FINALIZATION_ACTIONS)
STORE_V8 = deepcopy(STORE_V7)
STORE_V8["properties"].update(schema_version={"const": "deal_rounds_store.v8"},
    requests=array(REQUEST_V8, 150), final_basis_agreements=array(FINAL_AGREEMENT, 10),
    final_documents=array(FINAL_DOCUMENT, 10), final_document_events=array(FINAL_EVENT, 20))
STORE_V8["required"].extend(["final_basis_agreements", "final_documents", "final_document_events"])
FINAL_SAVED_ACTIONS = ["prepare_final_saved_review", "confirm_final_saved_document"]
FINAL_SAVED_REVIEW = obj(request_id=UUID, round_id=ROUND_ID, document_request_id=UUID,
    plan_sha256=SHA, path=RELATIVE_PATH, file_sha256=SHA, candidate_sha256=SHA,
    positions_revision=nullable(SHA), scope_sha256=SHA, report_sha256=SHA,
    report_utf8=text(1024 * 1024), stage={"const": "saved_version_requires_confirmation"})
FINAL_SAVED_CHECKS = obj(word_reopened=text(2000), all_pages_and_layout=text(2000),
    date_and_field_behavior=text(2000), properties_and_confidentiality=text(2000),
    requisites=text(2000), annexes=text(2000), unfilled_places=text(2000),
    final_agreed_text=text(2000))
PART_RESOLUTIONS = array(obj(part_name=text(500), statement=text(2000)), 200)
FINAL_CONFIRMATION = obj(request_id=UUID, round_id=ROUND_ID, saved_review_request_id=UUID,
    report_sha256=SHA, file_sha256=SHA, statement=text(2000), manual_checks=FINAL_SAVED_CHECKS,
    changed_part_resolutions=PART_RESOLUTIONS, issue_resolutions=ISSUE_RESOLUTIONS,
    position_resolutions=POSITION_RESOLUTIONS,
    assurance={"const": "client_asserted_final_saved_version"})
REQUEST_V9 = deepcopy(REQUEST_V8)
REQUEST_V9["properties"]["action"]["enum"].extend(FINAL_SAVED_ACTIONS)
STORE_V9 = deepcopy(STORE_V8)
STORE_V9["properties"].update(schema_version={"const": "deal_rounds_store.v9"},
    requests=array(REQUEST_V9, 150), final_saved_reviews=array(FINAL_SAVED_REVIEW, 10),
    final_confirmations=array(FINAL_CONFIRMATION, 10))
STORE_V9["required"].extend(["final_saved_reviews", "final_confirmations"])
STORE = {"oneOf": [STORE_V1, STORE_V2, STORE_V3, STORE_V4, STORE_V5, STORE_V6, STORE_V7, STORE_V8, STORE_V9]}
COMMAND = {"oneOf": [
    obj(action={"const": "prepare_working_copy"}, round_id=ROUND_ID,
        incoming_basis_sha256=SHA, path=RELATIVE_PATH),
    obj(action={"const": "publish_working_copy"}, round_id=ROUND_ID,
        copy_plan_request_id=UUID, plan_sha256=SHA),
    obj(action={"const": "record_working_copy_verification"}, round_id=ROUND_ID,
        copy_plan_request_id=UUID, plan_sha256=SHA),
    obj(action={"const": "prepare_outgoing_package"}, round_id=ROUND_ID,
        expected_working_request_id=UUID, file_sha256=SHA, path=RELATIVE_PATH,
        redline_name=nullable(text(120)), clean_draft_name=nullable(text(120)),
        comments_policy=COMMENTS_POLICY, properties_policy=PROPERTIES_POLICY),
    obj(action={"const": "publish_outgoing_package"}, round_id=ROUND_ID,
        package_request_id=UUID, plan_sha256=SHA, user_confirmed={"type": "boolean", "const": True},
        statement=text(2000)),
    obj(action={"const": "record_outgoing_verification"}, round_id=ROUND_ID,
        package_request_id=UUID, plan_sha256=SHA),
    obj(action={"const": "import_working_document"}, round_id=ROUND_ID,
        path=RELATIVE_PATH, file_sha256=SHA, incoming_basis_sha256=SHA,
        user_confirmed={"type": "boolean", "const": True}, statement=text(2000)),
    obj(action={"const": "start_round"}, round_id=ROUND_ID,
        previous_round_id=nullable(ROUND_ID), path=text(4096), file_sha256=SHA),
    obj(action={"const": "record_sent"}, round_id=ROUND_ID, path=text(4096),
        file_sha256=SHA, user_confirmed={"const": True, "type": "boolean"}, statement=text(2000)),
    obj(action={"const": "link_operation"}, round_id=ROUND_ID,
        source_path=text(4096), operation_id=JOURNAL_ID),
    obj(action={"const": "link_working_document"}, round_id=ROUND_ID, operation_id=JOURNAL_ID),
    obj(action={"const": "record_working_version"}, round_id=ROUND_ID,
        expected_working_request_id=UUID, file_sha256=SHA,
        user_confirmed={"type": "boolean", "const": True}, statement=text(2000)),
    obj(action={"const": "save_issue"}, round_id=ROUND_ID, issue_id=ISSUE_ID,
        expected_version=nullable(VERSION), content=ISSUE_CONTENT),
    obj(action={"const": "link_issue_operation"}, round_id=ROUND_ID, issue_id=ISSUE_ID,
        expected_version=VERSION, operation_id=JOURNAL_ID),
    obj(action={"const": "decide_issue"}, round_id=ROUND_ID, issue_id=ISSUE_ID,
        expected_version=VERSION, choice={"enum": ["approve", "reject", "defer"]},
        basis_sha256=SHA, user_confirmed={"type": "boolean", "const": True}, statement=text(2000)),
]}
TABLE_COMMAND = deepcopy(next(item for item in COMMAND["oneOf"] if item["properties"]["action"].get("const") == "prepare_outgoing_package"))
TABLE_COMMAND["properties"].update(action={"const": "prepare_outgoing_package_with_table"}, table_selection=TABLE_SELECTION)
TABLE_COMMAND["required"].append("table_selection")
COMMAND["oneOf"].append(TABLE_COMMAND)
COMMAND["oneOf"].extend([
    obj(action={"const": "record_final_basis_agreement"}, round_id=ROUND_ID, file_sha256=SHA,
        expected_positions_revision=nullable(SHA), expected_review_sha256=SHA,
        user_confirmed={"const": True, "type": "boolean"}, statement=text(2000),
        issue_resolutions=ISSUE_RESOLUTIONS, position_resolutions=POSITION_RESOLUTIONS, manual_checks=FINAL_CHECKS),
    obj(action={"const": "prepare_final_document"}, round_id=ROUND_ID, agreement_request_id=UUID,
        path=RELATIVE_PATH, name=text(120), comments_policy=obj(mode={"const": "remove_all"}),
        properties_policy=PROPERTIES_POLICY),
    obj(action={"const": "publish_final_document"}, round_id=ROUND_ID, document_request_id=UUID,
        plan_sha256=SHA, user_confirmed={"const": True, "type": "boolean"}, statement=text(2000)),
    obj(action={"const": "record_final_document_verification"}, round_id=ROUND_ID,
        document_request_id=UUID, plan_sha256=SHA),
])
COMMAND["oneOf"].extend([
    obj(action={"const": "prepare_final_saved_review"}, round_id=ROUND_ID,
        document_request_id=UUID, plan_sha256=SHA, path=RELATIVE_PATH, file_sha256=SHA),
    obj(action={"const": "confirm_final_saved_document"}, round_id=ROUND_ID,
        saved_review_request_id=UUID, report_sha256=SHA, file_sha256=SHA,
        user_confirmed={"const": True, "type": "boolean"}, statement=text(2000),
        manual_checks=FINAL_SAVED_CHECKS, changed_part_resolutions=PART_RESOLUTIONS,
        issue_resolutions=ISSUE_RESOLUTIONS, position_resolutions=POSITION_RESOLUTIONS),
])
COMMAND[MCP_CONTRACT_SCHEMA_EXTENSION] = MCP_CONTRACT_SCHEMA_VERSION
CommandInput = Annotated[dict[str, Any], WithJsonSchema(COMMAND)]
RequestIdInput = Annotated[str, WithJsonSchema(UUID)]
RESULT = obj(
    schema_version={"const": "deal_state_result.v9"}, status={"const": "ok"},
    revision=nullable(SHA), rounds=array(ROUND, 50), requests=array(REQUEST_V9, 150),
    documents=array(obj(file_sha256=SHA, path=text(4096)), 150),
    positions_current_revision=nullable(SHA), applied_request_id=nullable(UUID),
    positions_basis_observations=array(obj(round_id=ROUND_ID,
        status={"enum": ["not_recorded", "current", "historical_available", "unavailable"]}), 50),
    operations=array(OPERATION, 100), working_documents=array(WORKING_RESULT_V5, 100),
    issues=array(ISSUE, 100), issue_events=array(ISSUE, 100),
    outgoing_packages=array(OUTGOING_V7, 20), outgoing_events=array(OUTGOING_EVENT, 20),
    working_copy_plans=array(WORKING_COPY_PLAN, 20),
    final_basis_agreements=array(FINAL_AGREEMENT, 10), final_documents=array(FINAL_DOCUMENT, 10),
    final_document_events=array(FINAL_EVENT, 20),
    final_saved_reviews=array(FINAL_SAVED_REVIEW, 10), final_confirmations=array(FINAL_CONFIRMATION, 10),
    final_confirmation_observations=array(obj(confirmation_request_id=UUID,
        snapshot_sha256=SHA, context_status={"enum": ["current_checked", "changed_or_unavailable"]},
        selected_file_status={"enum": ["same_bytes", "changed", "unavailable"]}), 10),
    final_basis_observations=array(obj(agreement_request_id=UUID,
        status={"enum": ["saved_context_matches_live_checks_required", "saved_context_changed"]}), 10),
    operation_observations=array(obj(operation_id=JOURNAL_ID,
        status={"enum": ["completed", "prepared_only", "unavailable", "intent_changed", "not_checked"]},
        publication_status=nullable({"enum": ["published", "not_published", "uncertain"]}),
        verification=nullable({"enum": ["matched", "mismatch", "not_checked"]}),
        operation_error=nullable(text(64)), current_output_sha256=nullable(SHA),
        backup_current_verified=nullable({"type": "boolean"})), 100),
    working_observations=array(obj(request_id=UUID,
        status={"enum": ["same_bytes", "changed", "unavailable", "not_checked"]}), 100),
    issue_observations=array(obj(issue_id=ISSUE_ID,
        position_status={"enum": ["not_recorded", "current", "historical_available", "unavailable"]},
        decision_status={"enum": ["not_recorded", "current_basis", "basis_changed", "unavailable", "not_checked"]}), 100),
    observation_page=obj(offset={"type": "integer", "minimum": 0, "maximum": 100},
        page_size={"type": "integer", "minimum": 1, "maximum": 20},
        total={"type": "integer", "minimum": 0, "maximum": 100},
        next_offset=nullable({"type": "integer", "minimum": 1, "maximum": 100})),
    limitations=array(text(200), 10),
    producer=obj(name={"const": "veqtor-mcp"}, version=text(64), build=text(128)),
    record_id={"type": "null"}, record_status={"const": "disabled"},
)
RESULT[MCP_CONTRACT_SCHEMA_EXTENSION] = MCP_CONTRACT_SCHEMA_VERSION


class DealStateResult(_ContractResult):
    contract_schema: ClassVar[dict[str, Any]] = RESULT
