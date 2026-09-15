# SPDX-License-Identifier: Apache-2.0
"""Read-only attribution of original native payloads to a bound client turn.

The observer supplies an immutable client session prefix and its byte binding;
this module never manufactures client output from a raw MCP transcript.
"""
import json
import math
from pathlib import Path

from check_codex_acceptance import _digest, _file_sha256, _require


def decoded(text):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def non_json(value):
        raise ValueError("non-JSON number")

    def finite(value):
        result = float(value)
        return result if math.isfinite(result) else non_json(value)

    try:
        return json.loads(text, object_pairs_hook=object_pairs, parse_constant=non_json,
                          parse_float=finite) if isinstance(text, str) else None
    except ValueError:
        return None


def text_payloads(content):
    """Complete text-block arrays only; never search arbitrary nested objects."""
    if not isinstance(content, list) or not content or not all(
        isinstance(row, dict) and row.get("type") == "text" for row in content
    ):
        return []
    values = [decoded(row.get("text")) for row in content]
    return values if all(isinstance(v, dict) and "producer" in v for v in values) else []


def result_payloads(value):
    if isinstance(value, dict) and any(key in value and value[key] is not False for key in ("isError", "is_error")):
        return []
    if isinstance(value, dict) and "producer" in value:
        return [value]
    if isinstance(value, list):
        return text_payloads(value)
    if not isinstance(value, dict) or "content" not in value or value.get("isError") or value.get("is_error"):
        return []
    values = text_payloads(value["content"])
    for key in ("structured_content", "structuredContent"):
        if key in value and (len(values) != 1 or _digest(value[key]) != _digest(values[0])):
            return []
    return values


def result_item(value, *, labelled=False, settled=False):
    """Finite item grammar: one label and one fulfilled wrapper, in either order."""
    if isinstance(value, dict) and set(value) in ({"file", "result"}, {"file", "index", "result"}):
        if labelled:
            return []
        items = result_item(value["result"], labelled=True, settled=settled)
        if len(items) != 1:
            return []
        return [dict(items[0], labels={k: value[k] for k in value if k != "result"},
                     wrappers=["label", *items[0]["wrappers"]])]
    if isinstance(value, dict) and set(value) == {"status", "value"}:
        if settled or value["status"] != "fulfilled":
            return []
        return [dict(item, wrappers=["fulfilled", *item["wrappers"]])
                for item in result_item(value["value"], labelled=labelled, settled=True)]
    return [dict(payload=payload, labels={}, wrappers=[], terminal=value, terminal_index=index)
            for index, payload in enumerate(result_payloads(value))]


def result_items(output):
    """Complete blocks/flat collections only; retain labels instead of discarding them."""
    segments = [output] if isinstance(output, str) else [
        row.get("text") for row in output if isinstance(row, dict) and row.get("type") in {"input_text", "text"}
    ] if isinstance(output, list) else []
    found = []
    for block, text in enumerate(segments):
        value = decoded(text)
        # A complete text-block array is a terminal, not a nested collection.
        items = result_item(value)
        groups = [items] if items else [result_item(v) for v in value] if isinstance(value, list) else []
        if groups and all(groups):
            for index, group in enumerate(groups):
                found.extend(dict(item, output_block=block, output_item=index, payload_index=n)
                             for n, item in enumerate(group))
    return found


def payloads(output):
    """Payload projection for shape diagnostics only; this does not award delivery."""
    return [item["payload"] for item in result_items(output)]


def terminal_matches(result, call):
    """Check offered fields only after unique complete payload correspondence.

    Text serialization may differ; block attributes must match the original,
    including field presence. A block in an aggregate still belongs to its own
    producer. Metadata never selects a producer or repairs an ambiguous batch.
    """
    terminal = result["terminal"]
    if isinstance(terminal, dict) and "producer" in terminal:
        return True
    original = call.get("core_result", call.get("result"))
    expected = original["content"][0] if isinstance(original, dict) else dict(type="text")
    blocks = terminal if isinstance(terminal, list) else terminal["content"]
    offered = blocks[result["terminal_index"]]
    def attributes(block):
        return {k: v for k, v in block.items() if k != "text"}
    if _digest(attributes(offered)) != _digest(attributes(expected)):
        return False
    if isinstance(terminal, dict):
        if not set(terminal) <= {"content", "structured_content", "structuredContent", "is_error", "isError", "_meta"}:
            return False
        # The pinned conversion maps absent optional envelope metadata to null.
        # A non-null original cannot silently disappear from a full MCP result.
        if _digest(terminal.get("_meta")) != _digest(original.get("_meta") if original else None):
            return False
    return True


DOCUMENT_OPERANDS = {
    "inspect_document": ("path",), "verify_quote": ("path",), "extract_redlines": ("path",),
    "preflight_edits": ("source_path",), "apply_edits": ("source_path", "output_path"),
}


def document_operands(call):
    return {key: call["arguments"][key] for key in DOCUMENT_OPERANDS.get(call["tool"], ())
            if isinstance(call["arguments"].get(key), str) and call["arguments"][key].startswith("/")}


def checked_labels(labels, call, paths):
    """Labels constrain an already unique producer; never use them to select one."""
    if not labels:
        return {}
    file = labels["file"]
    if not isinstance(file, str) or not file:
        return None
    operands = document_operands(call)
    if file.startswith("/"):
        roles = [key for key, path in operands.items() if path == file]
    elif "/" not in file and "\\" not in file and file not in {".", ".."}:
        same_names = {path for path in paths if path.rsplit("/", 1)[-1] == file}
        roles = [key for key, path in operands.items() if path in same_names] if len(same_names) == 1 else []
    else:
        return None
    if len(roles) != 1:
        return None
    result = dict(labels=labels, file_argument=roles[0], file_path=operands[roles[0]])
    if "index" in labels:
        args = call["arguments"]
        if call["tool"] == "inspect_document" and isinstance(args.get("selection"), dict) and set(args["selection"]) == {"paragraph_ref"}:
            ref, location = args["selection"]["paragraph_ref"], "selection.paragraph_ref.paragraph_index"
        elif call["tool"] == "verify_quote":
            ref, location = args.get("anchor"), "anchor.paragraph_index"
        else:
            return None
        if (not isinstance(ref, dict) or ref.get("schema_version") != "paragraph_ref.v1" or ref.get("ref_type") != "paragraph"
                or type(labels["index"]) is not int or labels["index"] < 0
                or type(ref.get("paragraph_index")) is not int or labels["index"] != ref["paragraph_index"]):
            return None
        result["index_argument"] = location
    return result


def direct_call(action, call):
    args = decoded(action.get("arguments") if action.get("type") == "function_call" else action.get("input"))
    return (("source_occurrence" not in call or action.get("call_id") == call["source_occurrence"]["item_id"])
            and action.get("name") in {call["tool"], f"mcp__{call['server']}__{call['tool']}"}
            and isinstance(args, dict) and _digest(args) == _digest(call["arguments"]))


def native_call(item, call):
    values = result_payloads(item.get("result"))
    return (("source_occurrence" not in call or (item.get("id") == call["source_occurrence"]["item_id"]
             and _digest(item) == _digest(call["source_occurrence"]["core_item"])))
            and item.get("status") == "completed" and item.get("error") is None
            and all(item.get(k) == call[k] for k in ("server", "tool"))
            and _digest(item.get("arguments")) == _digest(call["arguments"])
            and len(values) == 1 and _digest(values[0]) == _digest(call["payload"]))


def validate_model_delivery(calls, session, *, thread, cwd, final_text, diagnostics=None):
    _require(session and all(isinstance(row, dict) for row in session) and session[0].get("type") == "session_meta"
             and session[0].get("payload", {}).get("id") == thread
             and session[0]["payload"].get("cwd") == cwd, "model delivery session identity differs")
    starts = [i for i, e in enumerate(session) if e.get("type") == "event_msg"
              and e.get("payload", {}).get("type") == "task_started"]
    ends = [i for i, e in enumerate(session) if e.get("type") == "event_msg"
            and e.get("payload", {}).get("type") == "task_complete"]
    _require(starts and ends and starts[-1] < ends[-1] == len(session) - 1,
             "model delivery lacks an exact complete current-turn prefix")
    active = session[starts[-1] + 1:ends[-1]]
    turn = session[starts[-1]]["payload"].get("turn_id")
    _require(session[ends[-1]]["payload"].get("turn_id") == turn, "model delivery completion turn differs")
    qualified = any("source_occurrence" in c for c in calls)
    if qualified:
        from nr03_app_server import PROFILE
        _require(all(c.get("source_occurrence", {}).get("profile") == PROFILE
                 and c["source_occurrence"].get("thread_id") == thread
                 and c["source_occurrence"].get("turn_id") == turn for c in calls),
                 "model source occurrence scope differs")
    pending, finals, inner_seen, bindings = {}, [], set(), []
    diagnostics = diagnostics if diagnostics is not None else []
    successful = {c["id"]: c for c in calls if not c["failed"]}
    paths = {path for c in calls for path in document_operands(c).values()}
    for i, row in enumerate(active):
        line = starts[-1] + i + 2
        item = row.get("payload", {})
        if row.get("type") == "event_msg" and item.get("type") == "item_completed" and item.get("item", {}).get("type") == "McpToolCall":
            inner = item["item"]
            ident = inner.get("id")
            _require(isinstance(ident, str) and ident not in inner_seen, "model inner native attribution is ambiguous")
            inner_seen.add(ident)
            # Serialized runtime invocation records provide evaluated arguments;
            # do not execute or infer them from arbitrary JavaScript source.
            opened = [p for p in pending.values() if not p["closed"]]
            if (len(opened) == 1 and opened[0]["exec"] and turn is not None
                    and item.get("thread_id") == thread and item.get("turn_id") == turn):
                matches = [c["id"] for c in successful.values() if native_call(inner, c)]
                # Only the qualified original source profile enables K identity.
                # Legacy CLI diagnostics retain unique complete value matching.
                if len(matches) == 1:
                    raw_id = matches[0]
                    opened[0]["inner"][raw_id] = dict(line=line, id=ident)
                    bindings.append(raw_id)
                else:
                    diagnostics.append(dict(reason="missing_or_ambiguous_original_invocation", line=line, inner_call_id=ident))
            else:
                diagnostics.append(dict(reason="invocation_outside_unique_current_producer", line=line, inner_call_id=ident))
            continue
        if row.get("type") != "response_item":
            continue
        kind = item.get("type")
        if kind in {"function_call", "custom_tool_call"}:
            ident = item.get("call_id")
            _require(isinstance(ident, str) and ident not in pending, "model tool attribution is ambiguous")
            if qualified:
                _require((kind == "custom_tool_call" and item.get("name") == "exec")
                         or any(direct_call(item, c) for c in calls), "model forbidden or unbound original action")
            pending[ident] = dict(action=item, line=line, inner={}, outputs=[], closed=False,
                exec=kind == "custom_tool_call" and item.get("name") == "exec"
                     and isinstance(item.get("input"), str) and bool(item["input"].strip()))
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            _require(item.get("call_id") in pending, "model result lacks its corresponding call")
            parent = pending[item["call_id"]]
            parent["outputs"].append(dict(item=item, line=line))
            parent["closed"] = True
        elif kind == "message" and item.get("role") == "assistant":
            text = "".join(c.get("text", "") for c in item.get("content", [])
                           if c.get("type") in {"output_text", "text"})
            finals.append(dict(text=text, line=starts[-1] + i + 2))
    _require(finals and finals[-1]["text"] == final_text, "model final message differs from raw native turn")
    _require(len(bindings) == len(set(bindings)), "raw result has multiple inner producers")
    proposals = {}
    for outer, parent in pending.items():
        if len(parent["outputs"]) != 1:
            diagnostics.append(dict(reason="missing_or_duplicate_output", model_call_id=outer))
            continue
        item, line = parent["outputs"][0]["item"], parent["outputs"][0]["line"]
        if item["type"] != parent["action"]["type"] + "_output" or line >= finals[-1]["line"]:
            diagnostics.append(dict(reason="wrong_or_late_output", model_call_id=outer, line=line))
            continue
        candidates = list(parent["inner"]) if parent["exec"] else [
            call["id"] for call in successful.values() if direct_call(parent["action"], call)
            and (not qualified or parent["line"] < call["source_occurrence"]["core_line"] < line)]
        items = result_items(item.get("output"))
        if not items:
            diagnostics.append(dict(reason="unsupported_malformed_or_incomplete_output", model_call_id=outer, line=line))
        for result in items:
            digest = _digest(result["payload"])
            matches = [ident for ident in candidates if _digest(successful[ident]["payload"]) == digest]
            if len(matches) != 1:
                diagnostics.append(dict(reason="missing_or_ambiguous_complete_correspondence", model_call_id=outer, line=line))
                continue
            ident = matches[0]
            # Never consume a candidate to break a later tie. Multiplicity is
            # checked globally, including differently labelled equal outputs.
            label = checked_labels(result["labels"], successful[ident], paths)
            terminal_valid = terminal_matches(result, successful[ident])
            proposals.setdefault(ident, []).append(dict(sha256=digest, line=line, model_call_id=outer, native_call_id=ident,
                action_line=parent["line"], native_line=parent["inner"].get(ident, {}).get("line"),
                inner_call_id=parent["inner"].get(ident, {}).get("id"), labels=result["labels"], label_binding=label,
                output_block=result["output_block"], output_item=result["output_item"], payload_index=result["payload_index"],
                wrappers=result["wrappers"], terminal_valid=terminal_valid,
                source_occurrence=successful[ident].get("source_occurrence"),
                attribution=("qualified_original_inner_mcp" if parent["exec"] else "qualified_original_direct") if qualified
                    else "legacy_unique_inner_value" if parent["exec"] else "legacy_unique_direct_value"))
    delivered = {}
    for ident, matches in proposals.items():
        if len(matches) == 1 and matches[0]["label_binding"] is not None and matches[0]["terminal_valid"]:
            delivered[ident] = matches[0]
        else:
            diagnostics.append(dict(reason="duplicate_result_observation" if len(matches) != 1 else
                                    "contradictory_or_unsupported_terminal" if not matches[0]["terminal_valid"] else "contradictory_or_ambiguous_label",
                                    native_call_id=ident, observations=matches))
    return delivered


def load_model_delivery(directory, stage, parsed):
    path = Path(directory) / f"{stage}.delivery.json"
    binding = decoded(path.read_text())
    fields = {"schema_version", "session_path", "session_sha256", "receipt_sha256"}
    _require(isinstance(binding, dict) and set(binding) in (fields, fields | {"source_fixture_sha256"})
             and binding["schema_version"] == "nr03-model-delivery.v3"
             and Path(binding["session_path"]).is_absolute(), "model delivery binding is invalid")
    session_path = Path(binding["session_path"])
    _require(not session_path.is_symlink() and _file_sha256(str(session_path)) == binding["session_sha256"]
             and _file_sha256(str(Path(directory) / f"{stage}.receipt.json")) == binding["receipt_sha256"],
             "model delivery byte/receipt binding differs")
    session = [decoded(line) for line in session_path.read_text().splitlines()]
    if "source_fixture_sha256" in binding:
        _require(parsed.get("source_fixture_session") is not None, "model source fixture was not qualified")
        session = parsed["source_fixture_session"]
    elif parsed.get("source") is not None:
        _require(binding["session_sha256"] == parsed["source"]["session_sha256"], "model source prefix binding differs")
    diagnostics = []
    delivered = validate_model_delivery(parsed["calls"], session, thread=parsed["thread"],
        cwd=parsed["receipt"]["cwd"], final_text=parsed["messages"][-1]["text"], diagnostics=diagnostics)
    required = [c["id"] for c in parsed["calls"] if not c["failed"] and (
        c["tool"] in {"read_deal_positions", "verify_quote", "preflight_edits", "apply_edits", "extract_redlines"}
        or c["tool"] == "inspect_document" and c["arguments"].get("mode") == "read")]
    _require(set(required) <= set(delivered), "required model-facing evidence not established; missing results: "
             + ", ".join(sorted(set(required) - set(delivered))) + "; observed reasons: "
             + ", ".join(sorted({d["reason"] for d in diagnostics})) + "; no physical-clipping inference")
    return dict(status="PASS", calls=delivered, session_sha256=binding["session_sha256"],
                diagnostics=diagnostics, log_authenticity_verified=False, journal_presentation="SEPARATE")
