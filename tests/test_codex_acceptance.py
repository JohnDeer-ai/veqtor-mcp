# SPDX-License-Identifier: Apache-2.0
"""Reject misleading native-client acceptance evidence and changed artifacts."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import pytest
from veqtor_mcp import __version__


sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import check_codex_acceptance as checker  # noqa: E402


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(value):
    return _hash(json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode())


def _completed(events, tool):
    return next(event["item"] for event in events if event.get("type") == "item.completed"
                and event.get("item", {}).get("tool") == tool)


def _sync_text(item):
    item["result"]["content"][0]["text"] = json.dumps(item["result"]["structured_content"])


@pytest.fixture
def evidence(tmp_path):
    # The checker treats documents as opaque bytes: it verifies logged native
    # operations and current hashes, and does not claim to replace the DOCX engine.
    source, output = tmp_path / "source.docx", tmp_path / "counter.docx"
    source.write_bytes(b"original source bytes")
    output.write_bytes(b"counterproposal bytes")
    source_hash, output_hash = _hash(source.read_bytes()), _hash(output.read_bytes())
    producer = {"name": "veqtor-mcp", "version": __version__,
                "build": "source-snapshot-v1-sha256:" + "a" * 64}
    anchor = {"schema_version": "change_unit_anchor.v2", "file_sha256": source_hash,
              "change_unit_id": "cu_001"}
    expected = [{"delete_text": "50", "insert_text": "250"}, {"reinstate_text": "misconduct"}]
    second_anchor = {**anchor, "change_unit_id": "cu_002"}
    edits = [{"anchor": anchor, **expected[0]}, {"anchor": second_anchor, **expected[1]}]
    proof_content = {
        "schema_version": "preflight_proof.v1", "source_sha256": source_hash,
        "edits_sha256": _canonical(edits), "tracked_change_author": "Veqtor MCP",
        "producer_build": producer["build"], "candidate_sha256": output_hash,
    }
    proof = {**proof_content, "proof_sha256": _canonical(proof_content)}
    check = {"status": "passed", "collateral_changes": [],
             "comparison": "ooxml_semantic_diff_outside_touched_anchors"}
    baseline = {
        "schema_version": checker.BASELINE_SCHEMA, "server_name": "veqtor",
        "producer": producer, "source_sha256": {str(source): source_hash},
        "source_path": str(source), "output_path": str(output),
        "output_absent_before": True, "expected_edits": expected,
        "tracked_change_author": "Veqtor MCP",
    }
    events = [
        {"type": "thread.started", "thread_id": "native-test-thread"},
        {"type": "item.completed", "item": {"id": "warning", "type": "error",
          "message": "Under-development features enabled: chronicle."}},
        {"type": "turn.started"},
    ]

    def add(tool, arguments, payload):
        item_id = f"item_{len(events)}"
        identity = {"id": item_id, "type": "mcp_tool_call", "server": "veqtor",
                    "tool": tool, "arguments": arguments}
        payload = {"producer": producer, "record_status": "written",
                   "record_id": "record-" + tool, **payload}
        events.append({"type": "item.started", "item": {**deepcopy(identity),
                       "status": "in_progress", "result": None, "error": None}})
        events.append({"type": "item.completed", "item": {**deepcopy(identity),
                       "status": "completed", "error": None, "result": {
                           "content": [{"type": "text", "text": json.dumps(payload)}],
                           "structured_content": deepcopy(payload)}}})

    add("list_rounds", {"folder": str(tmp_path)}, {"skipped": [], "rounds": [
        {"path": str(source), "sha256": source_hash}]})
    for tool in ("inspect_document", "map_rounds", "trace_paragraph_history"):
        add(tool, {"path": str(source), "mode": "read"}, {})
    add("extract_redlines", {"path": str(source)}, {
        "file_sha256": source_hash, "change_units": [{"anchor": anchor}, {"anchor": second_anchor}]})
    add("verify_quote", {"path": str(source), "quote": "50", "anchor": anchor},
        {"verdict": "exact", "checked_anchor": anchor})
    add("verify_quote", {"path": str(source), "quote": "misconduct", "anchor": second_anchor},
        {"verdict": "exact", "checked_anchor": second_anchor})
    add("preflight_edits", {"source_path": str(source), "edits": edits}, {
        "batch_applicable": True, "source_sha256": source_hash,
        "candidate_sha256": output_hash, "preflight_proof": proof, "round_trip_check": check,
        "tracked_change_author": "Veqtor MCP"})
    add("apply_edits", {"source_path": str(source), "output_path": str(output),
                        "edits": edits, "preflight_proof": proof}, {
        "source_sha256": source_hash, "output_sha256": output_hash,
        "output_path": str(output), "tracked_change_author": "Veqtor MCP",
        "preflight_binding_status": "verified", "preflight_candidate_sha256": output_hash,
        "candidate_output_sha256_match": True, "round_trip_check": check, "applied": [
            {"change_unit_id": "cu_001", "operation": "counter", "deleted_text": "50", "inserted_text": "250"},
            {"change_unit_id": "cu_002", "operation": "reinstate", "deleted_text": None, "inserted_text": "misconduct"}]})
    add("extract_redlines", {"path": str(output)}, {"file_sha256": output_hash})
    output_ref = {"schema_version": "paragraph_ref.v1", "file_sha256": output_hash,
                  "paragraph_index": 0, "reading_mode": "accepted_current_v1"}
    add("inspect_document", {"path": str(output), "mode": "read"}, {
        "file_sha256": output_hash, "paragraphs": [{"paragraph_ref": output_ref}]})
    for phrase in ("250", "misconduct"):
        add("verify_quote", {"path": str(output), "quote": phrase, "anchor": output_ref,
                             "paragraph_projection": "accepted_current_v1"}, {
            "verdict": "exact", "checked_anchor": output_ref,
            "checked_projection": {"mode": "accepted_current_v1", "projection_status": "complete"},
            "matches": [{"side": "paragraph_current"}]})
    add("export_decision_record", {"workspace": str(tmp_path)}, {"records": [
        {"record_id": "record-preflight_edits", "tool_name": "preflight_edits"},
        {"record_id": "record-apply_edits", "tool_name": "apply_edits"}]})
    events.append({"type": "turn.completed", "usage": {}})
    return events, baseline


def test_valid_native_sequence_and_current_files_produce_bounded_report(evidence):
    events, baseline = evidence
    report = checker.validate_evidence(events, baseline)
    assert report["status"] == "passed"
    assert report["tool_count"] == 9
    assert report["failed_read_call_count"] == 0
    assert report["visual_word_qa_verified"] is False
    assert report["log_authenticity_verified"] is False
    assert baseline["source_path"] not in json.dumps(report)
    assert baseline["expected_edits"][0]["delete_text"] not in report


@pytest.mark.parametrize("action", ["command_execution", "file_change", "unknown_action"])
def test_any_non_mcp_action_rejects_native_only_claim(evidence, action):
    events, baseline = evidence
    events.insert(3, {"type": "item.completed", "item": {
        "id": "fallback", "type": action, "command": "python fallback.py"}})
    with pytest.raises(checker.EvidenceError, match="non-MCP action"):
        checker.validate_evidence(events, baseline)


@pytest.mark.parametrize("field", ["source_path", "output_path"])
def test_current_artifact_changes_reject_otherwise_valid_log(evidence, field):
    events, baseline = evidence
    Path(baseline[field]).write_bytes(b"changed after the logged operation")
    with pytest.raises(checker.EvidenceError, match="changed|current output bytes"):
        checker.validate_evidence(events, baseline)


@pytest.mark.parametrize("mutation", ["edits", "proof", "producer", "readback", "export", "anchor"])
def test_semantically_incomplete_or_drifting_evidence_is_refused(evidence, mutation):
    events, baseline = evidence
    item = _completed(events, "apply_edits")
    if mutation == "edits":
        item["arguments"]["edits"][0]["insert_text"] = "500"
    elif mutation == "proof":
        item["arguments"]["preflight_proof"]["proof_sha256"] = "0" * 64
    elif mutation == "producer":
        item["result"]["structured_content"]["producer"]["build"] = "another-build"
        _sync_text(item)
    elif mutation == "readback":
        events[:] = [event for event in events if not (
            event.get("item", {}).get("tool") == "extract_redlines"
            and event["item"]["arguments"].get("path") == baseline["output_path"])]
    elif mutation == "export":
        item = _completed(events, "export_decision_record")
        item["result"]["structured_content"]["records"] = []
        _sync_text(item)
    else:
        item = _completed(events, "extract_redlines")
        item["result"]["structured_content"]["change_units"] = []
        _sync_text(item)
    # Keep paired start/completion arguments in agreement, so these exercise
    # operation binding rather than merely malformed event pairs.
    if mutation in {"edits", "proof"}:
        next(event["item"] for event in events if event.get("type") == "item.started"
             and event["item"].get("id") == item["id"])["arguments"] = deepcopy(item["arguments"])
    with pytest.raises(checker.EvidenceError):
        checker.validate_evidence(events, baseline)


def test_report_counts_recovered_read_failure_but_refuses_failed_write(evidence):
    events, baseline = evidence
    successful = _completed(events, "inspect_document")
    failed = deepcopy(successful)
    failed.update(id="failed_read", status="failed", result={"content": [
        {"type": "text", "text": "Error executing tool inspect_document"}],
        "structured_content": None})
    start = deepcopy(failed)
    start.update(status="in_progress", result=None)
    events[3:3] = [{"type": "item.started", "item": start},
                   {"type": "item.completed", "item": failed}]
    report = checker.validate_evidence(events, baseline)
    assert report["failed_read_call_count"] == 1
    assert report["failed_read_tools"] == ["inspect_document"]
    for item in (start, failed):
        item["tool"] = "apply_edits"
    with pytest.raises(checker.EvidenceError, match="attempt failed"):
        checker.validate_evidence(events, baseline)


def test_later_read_of_another_document_does_not_recover_a_failure(evidence):
    events, baseline = evidence
    item = _completed(events, "inspect_document")
    item.update(status="failed", result={"content": [{"type": "text", "text": "read failed"}],
                                         "structured_content": None})
    # A later successful inspect reads the output, so the tool name alone must
    # not make the failed source inspection count as a recovered attempt.
    with pytest.raises(checker.EvidenceError, match="successful retry"):
        checker.validate_evidence(events, baseline)


@pytest.mark.parametrize("target", ["source", "output"])
def test_each_intended_fragment_needs_its_own_native_quote(evidence, target):
    events, baseline = evidence
    path = baseline["source_path" if target == "source" else "output_path"]
    events[:] = [event for event in events if not (
        event.get("item", {}).get("tool") == "verify_quote"
        and event["item"]["arguments"].get("path") == path
        and event["item"]["arguments"].get("quote") == "misconduct")]
    with pytest.raises(checker.EvidenceError, match="fragment lacks"):
        checker.validate_evidence(events, baseline)


@pytest.mark.parametrize("mutation", ["candidate_hash", "unobserved_reference", "old_side"])
def test_output_quotes_must_use_observed_current_candidate_references(evidence, mutation):
    events, baseline = evidence
    item = next(event["item"] for event in events if event.get("type") == "item.completed"
                and event.get("item", {}).get("tool") == "verify_quote"
                and event["item"]["arguments"]["path"] == baseline["output_path"])
    result = item["result"]["structured_content"]
    if mutation == "old_side":
        result["matches"] = [{"side": "old"}]
    else:
        key, value = (("file_sha256", "0" * 64) if mutation == "candidate_hash"
                      else ("paragraph_index", 123))
        item["arguments"]["anchor"][key] = value
        result["checked_anchor"][key] = value
        next(event["item"] for event in events if event.get("type") == "item.started"
             and event["item"].get("id") == item["id"])["arguments"] = deepcopy(item["arguments"])
    _sync_text(item)
    with pytest.raises(checker.EvidenceError, match="output fragment lacks"):
        checker.validate_evidence(events, baseline)


def test_apply_must_start_after_preflight_completes(evidence):
    events, baseline = evidence
    index = next(index for index, event in enumerate(events) if event.get("type") == "item.started"
                 and event["item"].get("tool") == "apply_edits")
    apply_start = events.pop(index)
    index = next(index for index, event in enumerate(events) if event.get("type") == "item.completed"
                 and event.get("item", {}).get("tool") == "preflight_edits")
    events.insert(index, apply_start)
    with pytest.raises(checker.EvidenceError, match="started before"):
        checker.validate_evidence(events, baseline)


def test_text_only_success_and_unfinished_turn_do_not_prove_acceptance(evidence):
    events, baseline = evidence
    with pytest.raises(checker.EvidenceError, match="did not complete"):
        checker.validate_evidence(events[:-1], baseline)
    _completed(events, "apply_edits")["result"]["structured_content"] = None
    with pytest.raises(checker.EvidenceError, match="structured and text"):
        checker.validate_evidence(events, baseline)


def test_conflicting_payloads_and_duplicate_completions_are_refused(evidence):
    events, baseline = evidence
    item = _completed(events, "apply_edits")
    item["result"]["content"][0]["text"] = "{}"
    with pytest.raises(checker.EvidenceError, match="disagree"):
        checker.validate_evidence(events, baseline)
    _sync_text(item)
    events.insert(-1, {"type": "item.completed", "item": deepcopy(item)})
    with pytest.raises(checker.EvidenceError, match="duplicate MCP item id"):
        checker.validate_evidence(events, baseline)


def test_cli_checks_raw_files_and_hashes_them(evidence, tmp_path, capsys):
    events, baseline = evidence
    log, before = tmp_path / "native.jsonl", tmp_path / "baseline.json"
    log.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    before.write_text(json.dumps(baseline))
    assert checker.main(["--events", str(log), "--baseline", str(before)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["events_sha256"] == _hash(log.read_bytes())
    assert report["baseline_sha256"] == _hash(before.read_bytes())
    before.write_text('{"schema_version":1,"schema_version":2}')
    assert checker.main(["--events", str(log), "--baseline", str(before)]) == 1
    assert str(tmp_path) not in capsys.readouterr().err


@pytest.mark.parametrize("tool", ["preflight_edits", "apply_edits"])
@pytest.mark.parametrize("envelope", ["failed", "isError", "is_error", "payload_status"])
def test_write_failure_cannot_be_a_successful_native_call(evidence, tool, envelope):
    events, baseline = evidence
    item = _completed(events, tool)
    if envelope == "failed":
        item["status"] = "failed"
    elif envelope == "payload_status":
        item["result"]["structured_content"]["status"] = "error"
        _sync_text(item)
    else:
        item["result"][envelope] = True
    with pytest.raises(checker.EvidenceError):
        checker.validate_evidence(events, baseline)


@pytest.mark.parametrize("field", ["version", "build"])
def test_invalid_baseline_identity_is_not_candidate_proof(evidence, field):
    events, baseline = evidence
    baseline["producer"][field] = "PRIVATE_TEXT /private/path"
    with pytest.raises(checker.EvidenceError, match="source snapshot") as caught:
        checker.validate_evidence(events, baseline)
    assert "PRIVATE" not in str(caught.value)


def test_dropping_an_intended_edit_even_with_rebound_proof_is_refused(evidence):
    events, baseline = evidence
    preflight = _completed(events, "preflight_edits")
    apply = _completed(events, "apply_edits")
    edits = deepcopy(preflight["arguments"]["edits"][:1])
    proof = deepcopy(preflight["result"]["structured_content"]["preflight_proof"])
    proof["edits_sha256"] = _canonical(edits)
    proof["proof_sha256"] = _canonical({k: v for k, v in proof.items() if k != "proof_sha256"})
    for event in events:
        item = event.get("item", {})
        if item.get("tool") in {"preflight_edits", "apply_edits"}:
            item["arguments"]["edits"] = deepcopy(edits)
            if item["tool"] == "apply_edits":
                item["arguments"]["preflight_proof"] = deepcopy(proof)
    preflight["result"]["structured_content"]["preflight_proof"] = proof
    apply["result"]["structured_content"]["applied"] = apply["result"]["structured_content"]["applied"][:1]
    _sync_text(preflight)
    _sync_text(apply)
    with pytest.raises(checker.EvidenceError, match="pre-run intended"):
        checker.validate_evidence(events, baseline)


def test_overlapping_read_cannot_count_as_recovery(evidence):
    events, baseline = evidence
    success = _completed(events, "inspect_document")
    start_index = next(i for i, event in enumerate(events) if event["type"] == "item.started"
                       and event["item"]["id"] == success["id"])
    failed = deepcopy(success)
    failed.update(id="failed_read", status="failed", result=None, error={"message": "PRIVATE_TEXT"})
    start = deepcopy(failed)
    start.update(status="in_progress", error=None)
    events.insert(start_index, {"type": "item.started", "item": start})
    events.insert(start_index + 2, {"type": "item.completed", "item": failed})
    with pytest.raises(checker.EvidenceError, match="successful retry"):
        checker.validate_evidence(events, baseline)


def test_inventory_must_complete_before_preflight_starts(evidence):
    events, baseline = evidence
    index = next(i for i, event in enumerate(events) if event["type"] == "item.started"
                 and event["item"].get("tool") == "preflight_edits")
    pre_start = events.pop(index)
    index = next(i for i, event in enumerate(events) if event["type"] == "item.completed"
                 and event.get("item", {}).get("tool") == "list_rounds")
    events.insert(index, pre_start)
    with pytest.raises(checker.EvidenceError):
        checker.validate_evidence(events, baseline)
