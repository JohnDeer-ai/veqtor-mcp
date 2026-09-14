# SPDX-License-Identifier: Apache-2.0
"""NR-03 cursor-page provenance and adapter for the unchanged NR-01 document gate.

Receipts and the complete native turn must already pass NR-03 strict parsing.
No page union is inserted into a transcript or presented to NR-01 as a native call.
"""
from copy import deepcopy
from pathlib import Path
import re

from veqtor_mcp import records

import check_paragraph_acceptance as paragraphs
from check_codex_acceptance import EvidenceError, _digest, _file_sha256, _require
from nr03_delivery import EXPORT_PAGE_LIMIT

FROZEN_DEPENDENCIES = {
    "check_paragraph_acceptance.py": "ca9df9ca03b88c0c10c7b99e2c531275c01917097964b7f0bcf1b6337bfe5965",
    "check_codex_acceptance.py": "aba5f014fdc11f56057c3b7f7a53ff5617853bcac8ef8cc79509354bfe00e3dc",
}
SAME_PAGE_REFUSAL = "final export differs from native inputs, results or provenance"
DOCUMENT_TOOLS = {"verify_quote", "extract_redlines", "inspect_document"}


def require_frozen_dependencies():
    # This adapter depends on the precise location of NR-01's single-page gate:
    # after every document check, before only final source/output hash rechecks
    # and report construction. Unknown dependency revisions must fail closed.
    root = Path(paragraphs.__file__).parent
    _require(all(_file_sha256(str(root / name)) == sha for name, sha in FROZEN_DEPENDENCIES.items()),
             "NR-03 frozen document-gate dependency changed; review the adapter")


def record_number(value):
    _require(isinstance(value, str) and re.fullmatch(r"dr_[0-9]+", value) is not None
             and int(value[3:]) > 0, "export record ID is invalid")
    return int(value[3:])


def validate_export_pages(calls, workspace, producer):
    """Check original parsed calls from one receipt-bound turn, never loose pages.

    Pages run newest to oldest; records within each page run oldest to newest.
    total_count counts substantive records BEFORE that page's cursor, so the
    next total is the preceding total minus returned_count. IDs may have gaps.
    """
    selected_workspace = workspace
    workspace = str(Path(workspace).resolve())
    workspace_digest = records._path_digest(workspace)
    last_verification = max((c["completed_at"] for c in calls if c["tool"] in DOCUMENT_TOOLS), default=-1)
    all_exports = [c for c in calls if c["tool"] == "export_decision_record"]
    for call in all_exports:
        args = call["arguments"]
        _require(set(args) <= {"workspace", "max_records", "before_record_id"}
                 and args.get("workspace") == selected_workspace
                 and type(args.get("max_records")) is int and 1 <= args["max_records"] <= EXPORT_PAGE_LIMIT,
                 "export arguments violate the exact workspace/bounded-page contract")
    pages = [c for c in all_exports if c["started_at"] > last_verification]
    _require(pages, "complete final export pages are missing")
    cursor = remaining = previous_access = previous_export = None
    completed_at = last_verification
    collected, seen_numbers, page_of, page_hashes = {}, set(), {}, []
    terminal = False
    initial_count = None
    for page_index, call in enumerate(pages):
        _require(not terminal and call["started_at"] > completed_at,
                 "export pages overlap, are reordered or follow the terminal page")
        args, payload = call["arguments"], call.get("payload", {})
        _require(not call.get("failed") and paragraphs._RESULT_VALIDATORS["export_decision_record"].is_valid(payload)
                 and payload.get("producer") == producer and payload.get("workspace") == workspace_digest,
                 "export page result/producer/workspace differs")
        _require(args.get("before_record_id") == cursor, "export requested cursor breaks the page chain")
        _require(payload["records_scope"] == records.EXPORT_RECORDS_SCOPE
                 and payload["total_count_scope"] == records.EXPORT_TOTAL_COUNT_SCOPE
                 and payload["access_count_scope"] == records.EXPORT_ACCESS_COUNT_SCOPE,
                 "export count/record scope differs")
        rows = payload["records"]
        if remaining is None:
            initial_count = remaining = payload["total_count"]
        count = len(rows)
        _require(payload["total_count"] == remaining and payload["returned_count"] == count
                 and count == min(args["max_records"], remaining), "export page counts or completeness differ")
        truncated = remaining > count
        next_cursor = rows[0]["record_id"] if truncated else None
        _require(payload["truncated"] is truncated and payload["next_before_record_id"] == next_cursor,
                 "export truncated/terminal cursor differs")
        numbers = [record_number(row["record_id"]) for row in rows]
        _require(numbers == sorted(set(numbers)) and not seen_numbers.intersection(numbers)
                 and (cursor is None or all(number < record_number(cursor) for number in numbers)),
                 "export records are duplicate, reordered or outside the cursor")
        for row in rows:
            _require(row["workspace"] == workspace_digest and row["producer"] == producer
                     and row["record_type"] != records.ACCESS_RECORD_TYPE,
                     "export record belongs to another workspace/producer or is an access event")
            collected[row["record_id"]] = row
            page_of[row["record_id"]] = page_index
        export_id = record_number(payload["record_id"])
        access = payload["current_export_event"]
        _require(payload["record_status"] == "written" and payload["access_events_recorded_locally"] is True
                 and access["record_id"] == payload["record_id"] and access["record_status"] == "written"
                 and access["recorded_locally"] is True and (not numbers or export_id > max(numbers))
                 and (previous_export is None or export_id > previous_export)
                 and (previous_access is None or payload["access_count"] == previous_access + 1),
                 "export access-event identity/count sequence differs")
        seen_numbers.update(numbers)
        page_hashes.append(_digest(dict(arguments=args, payload=payload)))
        cursor, remaining = next_cursor, remaining - count
        previous_access, previous_export = payload["access_count"], export_id
        completed_at = call["completed_at"]
        terminal = not truncated
    _require(terminal and cursor is None and remaining == 0 and len(collected) == initial_count,
             "export page sequence is incomplete")
    wanted = [c for c in calls if c["tool"] in {"preflight_edits", "apply_edits"}]
    _require(len(wanted) == 2 and {c["tool"] for c in wanted} == {"preflight_edits", "apply_edits"}
             and len({c.get("payload", {}).get("record_id") for c in wanted}) == 2,
             "export requires distinct actual preflight/apply records")
    wanted_pages = {}
    for call in wanted:
        rid = call.get("payload", {}).get("record_id")
        _require(not call.get("failed") and rid in collected and call["completed_at"] < pages[0]["started_at"],
                 "export is missing an actual completed preflight/apply record")
        _require(collected[rid] == paragraphs._expected_export_record(call, collected[rid], workspace),
                 "export record differs from complete native inputs, results or provenance")
        wanted_pages[call["tool"]] = page_of[rid]
    return dict(schema_version="nr03-export-pages.v1", complete_pages_verified=True,
                page_count=len(pages), record_count=len(collected), page_sha256=page_hashes,
                wanted_pages=wanted_pages, log_authenticity_verified=False)


def validate_document_and_journal(events, baseline, calls, thread_id):
    """Keep NR-01's document checks intact, replacing only its same-page policy.

    NR-01 receives the original document-tool events unchanged. A frozen, specific
    final refusal can reach this adapter; every earlier refusal propagates. The
    new page gate must independently pass, then NR-01's final hash checks run again.
    This is an NR-03 report, not a claim that NR-01's single-page profile passed.
    """
    require_frozen_dependencies()
    legacy_report = None
    try:
        legacy_report = paragraphs.validate_evidence(events, baseline)
    except EvidenceError as exc:
        if str(exc) != SAME_PAGE_REFUSAL:
            raise
    journal = validate_export_pages(calls, str(Path(baseline["source_path"]).parent), baseline["producer"])
    if legacy_report is None:
        _require(journal["wanted_pages"]["preflight_edits"] != journal["wanted_pages"]["apply_edits"],
                 "NR-01 refused a same-page export; cannot substitute pagination")
        pre = next(c for c in calls if c["tool"] == "preflight_edits")["payload"]
        legacy_report = dict(thread_id=thread_id, producer=deepcopy(baseline["producer"]),
            source_hashes_unchanged=True, output_sha256=pre["candidate_sha256"],
            preflight_proof_sha256=pre["preflight_proof"]["proof_sha256"],
            affected_paragraph_count=len(baseline["expected_paragraphs"]),
            native_mcp_call_count=sum(c["tool"] not in {"read_deal_positions", "mutate_deal_positions"} for c in calls),
            native_actions_only=True, exact_revisions_verified=True, full_paragraphs_verified=True,
            collateral_verified=True, visual_word_qa_verified=False, log_authenticity_verified=False)
    for path, sha in baseline["source_sha256"].items():
        _require(_file_sha256(path) == sha, "source drifted during evidence verification")
    _require(_file_sha256(baseline["output_path"]) == legacy_report["output_sha256"],
             "output drifted during evidence verification")
    return dict(legacy_report, schema_version="nr03-document-evidence.v2", status="passed", journal=journal)
