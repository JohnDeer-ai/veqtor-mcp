# SPDX-License-Identifier: Apache-2.0
"""Read-only attribution of original native payloads to a bound client turn.

The observer supplies an immutable client session prefix and its byte binding;
this module never manufactures client output from a raw MCP transcript.
"""
import json
from pathlib import Path

from check_codex_acceptance import _digest, _file_sha256, _require


def payloads(output):
    if isinstance(output, str):
        segments = [output]
    elif isinstance(output, list):
        segments = [row.get("text", "") for row in output if row.get("type") in {"input_text", "text"}]
    else:
        return []
    found = []
    for text in segments:
        try:
            value = json.loads(text)
            if isinstance(value, dict) and "content" in value:
                content = value["content"]
                if len(content) != 1 or content[0].get("type") != "text":
                    continue
                inner = json.loads(content[0]["text"])
                if any(value[k] != inner for k in ("structured_content", "structuredContent") if k in value):
                    continue
                value = inner
            if isinstance(value, dict) and "producer" in value:
                found.append(value)
        except (ValueError, TypeError, KeyError):
            continue
    return found


def validate_model_delivery(calls, session, *, thread, cwd, final_text):
    _require(session and session[0].get("type") == "session_meta"
             and session[0].get("payload", {}).get("id") == thread
             and session[0]["payload"].get("cwd") == cwd, "model delivery session identity differs")
    starts = [i for i, e in enumerate(session) if e.get("type") == "event_msg"
              and e.get("payload", {}).get("type") == "task_started"]
    ends = [i for i, e in enumerate(session) if e.get("type") == "event_msg"
            and e.get("payload", {}).get("type") == "task_complete"]
    _require(starts and ends and starts[-1] < ends[-1] == len(session) - 1,
             "model delivery lacks an exact complete current-turn prefix")
    active = session[starts[-1] + 1:ends[-1]]
    pending, visible, finals = {}, [], []
    for i, row in enumerate(active):
        if row.get("type") != "response_item":
            continue
        item = row.get("payload", {})
        kind = item.get("type")
        if kind in {"function_call", "custom_tool_call"}:
            ident = item.get("call_id")
            _require(isinstance(ident, str) and ident not in pending, "model tool attribution is ambiguous")
            pending[ident] = i
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            _require(item.get("call_id") in pending, "model result lacks its corresponding call")
            for payload in payloads(item.get("output")):
                visible.append(dict(sha256=_digest(payload), line=starts[-1] + i + 2,
                                    model_call_id=item["call_id"]))
        elif kind == "message" and item.get("role") == "assistant":
            text = "".join(c.get("text", "") for c in item.get("content", [])
                           if c.get("type") in {"output_text", "text"})
            finals.append(dict(text=text, line=starts[-1] + i + 2))
    _require(finals and finals[-1]["text"] == final_text, "model final message differs from raw native turn")
    visible = [v for v in visible if v["line"] < finals[-1]["line"]]
    delivered = {}
    # Consume distinct deliveries: an earlier equal output cannot be credited
    # repeatedly to later calls. Written record IDs also bind individual results.
    for call in calls:
        if call["failed"]:
            continue
        match = next((v for v in visible if v["sha256"] == _digest(call["payload"])), None)
        if match is not None:
            visible.remove(match)
            delivered[call["id"]] = match
    return delivered


def load_model_delivery(directory, stage, parsed):
    path = Path(directory) / f"{stage}.delivery.json"
    binding = json.loads(path.read_text())
    _require(set(binding) == {"schema_version", "session_path", "session_sha256", "receipt_sha256"}
             and binding["schema_version"] == "nr03-model-delivery.v3"
             and Path(binding["session_path"]).is_absolute(), "model delivery binding is invalid")
    session_path = Path(binding["session_path"])
    _require(not session_path.is_symlink() and _file_sha256(str(session_path)) == binding["session_sha256"]
             and _file_sha256(str(Path(directory) / f"{stage}.receipt.json")) == binding["receipt_sha256"],
             "model delivery byte/receipt binding differs")
    session = [json.loads(line) for line in session_path.read_text().splitlines()]
    delivered = validate_model_delivery(parsed["calls"], session, thread=parsed["thread"],
        cwd=parsed["receipt"]["cwd"], final_text=parsed["messages"][-1]["text"])
    required = [c["id"] for c in parsed["calls"] if not c["failed"] and (
        c["tool"] in {"read_deal_positions", "verify_quote", "preflight_edits", "apply_edits", "extract_redlines"}
        or c["tool"] == "inspect_document" and c["arguments"].get("mode") == "read")]
    _require(set(required) <= set(delivered), "required model-facing evidence is clipped, missing or metadata-only")
    return dict(status="PASS", calls=delivered, session_sha256=binding["session_sha256"],
                log_authenticity_verified=False, journal_presentation="SEPARATE")
