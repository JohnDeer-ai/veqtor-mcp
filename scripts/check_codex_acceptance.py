# SPDX-License-Identifier: Apache-2.0
"""Check one native Codex MCP run against a pre-run local baseline.

This verifies local evidence and current file hashes, not log authenticity,
visual Word quality, clean-user acceptance, or release readiness. The baseline
and raw Codex JSONL can contain private paths and document text; keep them local.
The checker never launches the server or writes a document.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


BASELINE_SCHEMA = "veqtor_codex_acceptance_baseline.v1"
REPORT_SCHEMA = "veqtor_codex_acceptance.v1"
REQUIRED_TOOLS = frozenset({
    "list_rounds", "inspect_document", "extract_redlines", "map_rounds",
    "trace_paragraph_history", "verify_quote", "preflight_edits", "apply_edits",
    "export_decision_record",
})
MAX_EVIDENCE_BYTES = 32 * 1024 * 1024


class EvidenceError(ValueError):
    """The supplied run does not prove the bounded acceptance scenario."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _file_sha256(path: str) -> str:
    candidate = Path(path)
    _require(candidate.is_file() and not candidate.is_symlink(),
             "an evidence document is missing or is a symlink")
    with candidate.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _json(text: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate JSON field")
            result[key] = value
        return result

    def non_finite(_value):
        raise EvidenceError("non-finite JSON number")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=non_finite)
    except (ValueError, RecursionError) as exc:
        raise EvidenceError("invalid evidence JSON") from exc


def _read(path: Path) -> bytes:
    with path.open("rb") as handle:
        payload = handle.read(MAX_EVIDENCE_BYTES + 1)
    _require(len(payload) <= MAX_EVIDENCE_BYTES, "evidence file is too large")
    return payload


def _baseline(value: Any) -> dict:
    fields = {
        "schema_version", "server_name", "producer", "source_sha256",
        "source_path", "output_path", "output_absent_before", "expected_edits",
        "tracked_change_author",
    }
    _require(isinstance(value, dict) and set(value) == fields,
             "baseline fields differ from the supported schema")
    _require(value["schema_version"] == BASELINE_SCHEMA, "unsupported baseline schema")
    for field in ("server_name", "tracked_change_author"):
        _require(isinstance(value[field], str) and bool(value[field].strip()),
                 "baseline identity is empty")
    producer = value["producer"]
    _require(isinstance(producer, dict) and set(producer) == {"name", "version", "build"}
             and producer["name"] == "veqtor-mcp"
             and all(isinstance(item, str) and item for item in producer.values()),
             "baseline producer identity is invalid")
    sources = value["source_sha256"]
    _require(isinstance(sources, dict) and bool(sources), "baseline sources are empty")
    for path, sha in sources.items():
        _require(isinstance(path, str) and Path(path).is_absolute() and _sha256(sha),
                 "baseline source path or hash is invalid")
    _require(value["source_path"] in sources, "selected source is absent from baseline")
    output = value["output_path"]
    _require(isinstance(output, str) and Path(output).is_absolute() and output not in sources,
             "output path is invalid or names an original source")
    _require(value["output_absent_before"] is True,
             "baseline does not record an absent output before the run")
    edits = value["expected_edits"]
    _require(isinstance(edits, list) and bool(edits), "baseline expected edits are empty")
    for edit in edits:
        _require(isinstance(edit, dict) and set(edit) in (
            {"delete_text", "insert_text"}, {"reinstate_text"},
        ) and all(isinstance(text, str) and bool(text) for text in edit.values()),
            "this acceptance scenario supports nonempty replacement and reinstatement only")
    return value


def native_calls(events: list[dict], server_name: str) -> tuple[str, list[dict], list[dict]]:
    """Decode the observed Codex 0.153.4 JSONL envelope, refusing other actions."""
    thread_id = None
    started = completed = False
    pending: dict[str, dict] = {}
    start_events: dict[str, int] = {}
    completed_ids: set[str] = set()
    calls = []
    failures = []
    for event_index, event in enumerate(events):
        _require(isinstance(event, dict), "event is not an object")
        kind = event.get("type")
        _require(not completed, "events continue after the completed turn")
        if kind == "thread.started":
            _require(thread_id is None and not started, "duplicate or late thread start")
            thread_id = event.get("thread_id")
            _require(isinstance(thread_id, str) and bool(thread_id), "thread id is absent")
            continue
        if kind == "turn.started":
            _require(thread_id is not None and not started, "invalid turn start")
            started = True
            continue
        if kind == "turn.completed":
            _require(started and not pending, "turn ends with unfinished MCP calls")
            completed = True
            continue
        _require(kind in {"item.started", "item.completed"},
                 "unsupported event or unsuccessful turn")
        item = event.get("item")
        _require(isinstance(item, dict), "event item is absent")
        item_type = item.get("type")
        if not started and item_type == "error":
            # This exact pre-turn warning is emitted by the observed local CLI.
            # Other error items, especially errors during the turn, are failures.
            _require(kind == "item.completed" and isinstance(item.get("message"), str)
                     and item["message"].startswith("Under-development features enabled:"),
                     "unrecognized startup error")
            continue
        _require(started, "action occurred before the turn")
        _require(item_type in {"agent_message", "reasoning", "mcp_tool_call"},
                 "non-MCP action appeared in the native-only run")
        if item_type != "mcp_tool_call":
            continue
        item_id = item.get("id")
        _require(isinstance(item_id, str) and bool(item_id)
                 and item_id not in completed_ids, "missing or duplicate MCP item id")
        _require(item.get("server") == server_name
                 and item.get("tool") in REQUIRED_TOOLS,
                 "MCP call targets an unexpected server or tool")
        _require(isinstance(item.get("arguments"), dict), "MCP arguments are not an object")
        identity = {key: item[key] for key in ("server", "tool", "arguments")}
        if kind == "item.started":
            _require(item_id not in pending and item.get("status") == "in_progress"
                     and item.get("result") is None and item.get("error") is None,
                     "invalid or duplicate MCP call start")
            pending[item_id] = identity
            start_events[item_id] = event_index
            continue
        _require(pending.pop(item_id, None) == identity,
                 "MCP completion does not match its start")
        if item.get("status") == "failed":
            _require(item["tool"] not in {"preflight_edits", "apply_edits"},
                     "preflight or apply attempt failed in the positive scenario")
            scope = {key: value for key, value in item["arguments"].items()
                     if key in {"path", "folder", "workspace", "source_path", "mode"}}
            failures.append({"tool": item["tool"], "call_index": len(completed_ids), "scope": scope})
            completed_ids.add(item_id)
            continue
        _require(item.get("status") == "completed" and item.get("error") is None,
                 "native MCP call failed")
        result = item.get("result")
        _require(isinstance(result, dict) and not result.get("isError")
                 and not result.get("is_error"), "MCP result is missing or reports failure")
        payload = result.get("structured_content")
        content = result.get("content")
        _require(isinstance(payload, dict) and isinstance(content, list)
                 and len(content) == 1 and isinstance(content[0], dict)
                 and content[0].get("type") == "text"
                 and isinstance(content[0].get("text"), str),
                 "MCP result lacks the observed structured and text payloads")
        _require(_json(content[0]["text"]) == payload,
                 "MCP structured and text payloads disagree")
        calls.append({**identity, "payload": payload, "call_index": len(completed_ids),
                      "started_at": start_events[item_id], "completed_at": event_index})
        completed_ids.add(item_id)
    _require(completed and bool(calls), "native run did not complete with MCP calls")
    _require(all(any(call["tool"] == failure["tool"]
                     and call["call_index"] > failure["call_index"]
                     and all(call["arguments"].get(key) == value
                             for key, value in failure["scope"].items()) for call in calls)
                 for failure in failures), "a failed read call was not followed by a successful retry")
    return thread_id, calls, failures


def validate_evidence(events: list[dict], baseline: dict) -> dict:
    baseline = _baseline(baseline)
    thread_id, calls, failures = native_calls(events, baseline["server_name"])
    _require({call["tool"] for call in calls} == REQUIRED_TOOLS,
             "native run did not successfully exercise all nine tools")
    for call in calls:
        payload = call["payload"]
        _require(payload.get("producer") == baseline["producer"],
                 "a tool returned another producer identity")
        _require(payload.get("status", "ok") == "ok"
                 and payload.get("record_status") == "written"
                 and isinstance(payload.get("record_id"), str),
                 "a tool failed or did not write provenance")

    def matches(name, **arguments):
        return [(index, call) for index, call in enumerate(calls)
                if call["tool"] == name and all(
                    call["arguments"].get(key) == value for key, value in arguments.items())]

    preflights = matches("preflight_edits")
    applies = matches("apply_edits")
    _require(len(preflights) == len(applies) == 1,
             "acceptance requires exactly one preflight and one apply")
    pre_index, pre = preflights[0]
    apply_index, apply = applies[0]
    _require(pre["completed_at"] < apply["started_at"],
             "apply started before its successful preflight completed")
    source = baseline["source_path"]
    output = baseline["output_path"]
    source_hash = baseline["source_sha256"][source]
    _require(pre["arguments"].get("source_path") == source
             and apply["arguments"].get("source_path") == source
             and apply["arguments"].get("output_path") == output,
             "preflight or apply targets another source or output")
    edits = pre["arguments"].get("edits")
    _require(isinstance(edits, list) and edits == apply["arguments"].get("edits")
             and all(isinstance(edit, dict) and isinstance(edit.get("anchor"), dict) for edit in edits),
             "preflight and apply do not share the exact anchored edit batch")
    _require([{key: value for key, value in edit.items() if key != "anchor"}
              for edit in edits] == baseline["expected_edits"],
             "actual edits differ from the pre-run intended changes")
    extracts = [(index, call) for index, call in matches("extract_redlines", path=source)
                if call["completed_at"] < pre["started_at"]
                and call["payload"].get("file_sha256") == source_hash]
    _require(bool(extracts), "source was not natively extracted before preflight")
    anchors = [unit.get("anchor") for _, call in extracts
               for unit in call["payload"].get("change_units", []) if isinstance(unit, dict)]
    _require(all(edit["anchor"] in anchors for edit in edits),
             "edit anchors were not returned by native source extraction")
    for edit in edits:
        fragment = edit.get("delete_text", edit.get("reinstate_text"))
        _require(any(call["completed_at"] < pre["started_at"]
                     and call["arguments"].get("quote") == fragment
                     and call["arguments"].get("anchor") == edit["anchor"]
                     and call["payload"].get("checked_anchor") == edit["anchor"]
                     and edit["anchor"].get("file_sha256") == source_hash
                     and call["payload"].get("verdict") == "exact"
                     for index, call in matches("verify_quote", path=source)),
                 "an intended input fragment lacks exact source-bound native verification")
    initial_lists = [(index, call) for index, call in matches("list_rounds")
                     if index < pre_index]
    _require(any(call["payload"].get("skipped") == [] and
                 {row.get("path"): row.get("sha256") for row in call["payload"].get("rounds", [])}
                 == baseline["source_sha256"] for _, call in initial_lists),
             "initial native round inventory differs from the baseline")

    pre_result, applied = pre["payload"], apply["payload"]
    proof = pre_result.get("preflight_proof")
    candidate = pre_result.get("candidate_sha256")
    _require(pre_result.get("batch_applicable") is True and _sha256(candidate)
             and pre_result.get("source_sha256") == source_hash
             and pre_result.get("tracked_change_author") == baseline["tracked_change_author"],
             "preflight did not produce an applicable source-bound candidate")
    proof_content = {
        "schema_version": "preflight_proof.v1", "source_sha256": source_hash,
        "edits_sha256": _digest(edits),
        "tracked_change_author": baseline["tracked_change_author"],
        "producer_build": baseline["producer"]["build"], "candidate_sha256": candidate,
    }
    _require(proof == {**proof_content, "proof_sha256": _digest(proof_content)}
             and apply["arguments"].get("preflight_proof") == proof,
             "apply proof differs from the exact successful preflight binding")
    _require(applied.get("source_sha256") == source_hash
             and applied.get("output_sha256") == candidate
             and applied.get("output_path") == output
             and applied.get("tracked_change_author") == baseline["tracked_change_author"]
             and applied.get("preflight_binding_status") == "verified"
             and applied.get("preflight_candidate_sha256") == candidate
             and applied.get("candidate_output_sha256_match") is True,
             "apply does not confirm the source, author, output and candidate binding")
    for result in (pre_result, applied):
        check = result.get("round_trip_check")
        _require(isinstance(check, dict) and check.get("status") == "passed"
                 and check.get("collateral_changes") == []
                 and check.get("comparison") == "ooxml_semantic_diff_outside_touched_anchors",
                 "round-trip or collateral-change validation did not pass")
    applied_items = applied.get("applied")
    _require(isinstance(applied_items, list) and len(applied_items) == len(edits),
             "apply did not report every intended edit")
    for edit, result in zip(edits, applied_items):
        _require(isinstance(result, dict)
                 and result.get("change_unit_id") == edit["anchor"].get("change_unit_id")
                 and result.get("operation") in (
                     {"reinstate"} if "reinstate_text" in edit else {"counter", "replace"})
                 and result.get("deleted_text") == edit.get("delete_text")
                 and result.get("inserted_text") == edit.get("insert_text", edit.get("reinstate_text")),
                 "apply operation or wording differs from an intended edit")
    _require(any(call["started_at"] > apply["completed_at"]
                 and call["payload"].get("file_sha256") == candidate
                 for index, call in matches("extract_redlines", path=output)),
             "output was not natively re-extracted after apply")
    for edit in edits:
        fragment = edit.get("insert_text", edit.get("reinstate_text"))
        verified = False
        for index, call in matches("verify_quote", path=output, quote=fragment):
            anchor = call["arguments"].get("anchor")
            result = call["payload"]
            if not (call["started_at"] > apply["completed_at"] and isinstance(anchor, dict)
                    and anchor.get("file_sha256") == candidate
                    and anchor.get("reading_mode") == "accepted_current_v1"
                    and call["arguments"].get("paragraph_projection") == "accepted_current_v1"
                    and result.get("checked_anchor") == anchor and result.get("verdict") == "exact"
                    and isinstance(result.get("checked_projection"), dict)
                    and result["checked_projection"].get("mode") == "accepted_current_v1"
                    and result["checked_projection"].get("projection_status") == "complete"
                    and any(isinstance(match, dict) and match.get("side") == "paragraph_current"
                            for match in result.get("matches", []))):
                continue
            verified = any(apply["completed_at"] < read["started_at"]
                           and read["completed_at"] < call["started_at"]
                           and read["payload"].get("file_sha256") == candidate
                           and anchor in [row.get("paragraph_ref") for row in
                               read["payload"].get("matches", []) + read["payload"].get("paragraphs", [])
                               if isinstance(row, dict)]
                           for read_index, read in matches("inspect_document", path=output))
            if verified:
                break
        _require(verified, "an intended output fragment lacks exact current-projection native verification")
    wanted = {(pre_result["record_id"], "preflight_edits"),
              (applied["record_id"], "apply_edits")}
    _require(any(call["started_at"] > apply["completed_at"] and wanted <= {
                 (record.get("record_id"), record.get("tool_name"))
                 for record in call["payload"].get("records", []) if isinstance(record, dict)}
                 for index, call in matches("export_decision_record", workspace=str(Path(source).parent))),
             "post-apply native export lacks the preflight and apply records")
    for path, expected in baseline["source_sha256"].items():
        _require(_file_sha256(path) == expected, "an original source changed after the baseline")
    _require(_file_sha256(output) == candidate, "current output bytes differ from the candidate")
    return {
        "schema_version": REPORT_SCHEMA, "status": "passed", "thread_id": thread_id,
        "producer": baseline["producer"], "tool_count": len(REQUIRED_TOOLS),
        "native_mcp_call_count": len(calls) + len(failures),
        "successful_mcp_call_count": len(calls),
        "failed_read_call_count": len(failures),
        "failed_read_tools": [failure["tool"] for failure in failures],
        "tool_sequence": [call["tool"] for call in calls],
        "source_count": len(baseline["source_sha256"]), "source_hashes_unchanged": True,
        "output_sha256": candidate, "preflight_proof_sha256": proof["proof_sha256"],
        "native_actions_only": True, "visual_word_qa_verified": False,
        "log_authenticity_verified": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        events_bytes = _read(arguments.events)
        baseline_bytes = _read(arguments.baseline)
        events = [_json(line) for line in events_bytes.decode("utf-8").splitlines() if line.strip()]
        report = validate_evidence(events, _json(baseline_bytes.decode("utf-8")))
        report["events_sha256"] = hashlib.sha256(events_bytes).hexdigest()
        report["baseline_sha256"] = hashlib.sha256(baseline_bytes).hexdigest()
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except (EvidenceError, OSError, UnicodeError, KeyError, TypeError, ValueError, AttributeError) as exc:
        # Paths and document text are deliberately omitted from diagnostics.
        message = str(exc) if isinstance(exc, EvidenceError) else "malformed or unreadable local evidence"
        print(f"Codex acceptance failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
