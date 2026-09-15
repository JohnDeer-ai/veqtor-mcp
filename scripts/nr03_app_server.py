# SPDX-License-Identifier: Apache-2.0
"""Bounded original App Server source profile, never a CLI-ID reconstruction.

The JSONL inputs are original bytes. Projections below exist only in memory for
the frozen document consumers. See docs/NR03_SOURCE_PROFILE.md for the pinned
producer relationship, supported conversion and separate authenticity gate.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import uuid

from check_codex_acceptance import _digest, _file_sha256, _require
from nr03_model_delivery import decoded

PROFILE = "nr03-app-server-original.v1"
BUILD = dict(version="0.154.0-alpha.6.2", commit="b5bffd3ec4db487e7e3dec59663875b0ef7b72ca",
             sha256="ecad78dbf98adb89ec475edac86630406cbe59d9f3070b17d88065f136b94bcb")
POLICY_FILES = ("scripts/nr03_app_server.py", "scripts/capture_nr03_app_server.py",
                "scripts/nr03_runtime_policy.py", "scripts/nr03_capture_owner.py", "scripts/nr03_model_delivery.py", "docs/NR03_SOURCE_PROFILE.md")
PASSIVE = {"thread/status/changed", "thread/tokenUsage/updated", "account/rateLimits/updated",
           "model/rerouted", "item/agentMessage/delta", "item/reasoning/summaryTextDelta",
           "item/reasoning/summaryPartAdded", "item/reasoning/textDelta", "item/mcpToolCall/progress"}


def policy_hashes(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    return {name: _file_sha256(str(root / name)) for name in POLICY_FILES}


def lines(raw):
    _require(isinstance(raw, bytes) and raw.endswith(b"\n"), "source transport is truncated")
    try:
        rows = [decoded(line) for line in raw.decode("utf-8").splitlines()]
    except UnicodeError:
        rows = []
    _require(rows and all(isinstance(row, dict) for row in rows), "source JSONL malformed or unsupported")
    return rows


def uuid_value(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def core_to_app(core):
    """Pinned b5bffd conversion; reject unknown fields, retain original separately."""
    required = {"type", "id", "server", "tool", "arguments", "status"}
    optional = {"connectorId", "mcpAppResourceUri", "linkId", "appName", "actionName", "pluginId",
                "readOnlyHint", "result", "error", "duration"}
    _require(required <= set(core) <= required | optional and core["type"] == "McpToolCall",
             "unsupported core MCP envelope")
    result = core.get("result")
    if result is not None:
        _require(isinstance(result, dict) and {"content"} <= set(result) <=
                 {"content", "structuredContent", "isError", "_meta"}
                 and isinstance(result["content"], list)
                 and ("isError" not in result or type(result["isError"]) is bool),
                 "unsupported core result envelope")
        result = {"content": deepcopy(result["content"]), "structuredContent": deepcopy(result.get("structuredContent")),
                  "_meta": deepcopy(result.get("_meta"))}
    error = core.get("error")
    _require(error is None or (isinstance(error, dict) and set(error) == {"message"}
             and isinstance(error["message"], str)), "unsupported core error")
    status = core["status"]
    failure = core.get("result", {}).get("isError") if isinstance(core.get("result"), dict) else None
    _require(status in {"completed", "failed", "inProgress"}, "unsupported core status")
    _require((status == "inProgress" and result is None and error is None)
             or (status == "completed" and result is not None and error is None and failure is not True)
             or (status == "failed" and (failure is True or error is not None)), "core failure/status contradiction")
    duration = core.get("duration")
    millis = None
    if duration is not None:
        _require(isinstance(duration, dict) and set(duration) == {"secs", "nanos"}
                 and all(type(v) is int and v >= 0 for v in duration.values()) and duration["nanos"] < 10**9,
                 "unsupported core duration")
        millis = duration["secs"] * 1000 + duration["nanos"] // 10**6
    app = {key: deepcopy(core[key]) for key in ("id", "server", "tool", "arguments", "status")}
    app.update(type="mcpToolCall", appContext=None, pluginId=core.get("pluginId"),
               readOnlyHint=core.get("readOnlyHint"), result=result, error=deepcopy(error), durationMs=millis)
    if core.get("mcpAppResourceUri") is not None:
        app["mcpAppResourceUri"] = core["mcpAppResourceUri"]
    if core.get("connectorId") is not None:
        app["appContext"] = dict(connectorId=core["connectorId"], linkId=core.get("linkId"),
            resourceUri=core.get("mcpAppResourceUri"), appName=core.get("appName"), actionName=core.get("actionName"))
    return app


def core_inventory(session, thread, turn, cwd, selection):
    _require(session[0].get("type") == "session_meta" and session[0].get("payload", {}).get("id") == thread
             and session[0]["payload"].get("cwd") == cwd
             and session[0]["payload"].get("cli_version") == BUILD["version"], "source prefix session/build differs")
    starts = [i for i, row in enumerate(session) if row.get("type") == "event_msg"
              and row.get("payload", {}).get("type") == "task_started"]
    _require(starts and session[starts[-1]]["payload"].get("turn_id") == turn
             and session[-1].get("type") == "event_msg"
             and session[-1].get("payload", {}).get("type") == "task_complete"
             and session[-1]["payload"].get("turn_id") == turn, "source prefix lacks exact complete turn")
    active = session[starts[-1] + 1:-1]
    _require(not any(r.get("type") == "event_msg" and r.get("payload", {}).get("type") == "task_complete" for r in active),
             "source prefix has duplicate or premature completion")
    contexts = [r["payload"] for r in active if r.get("type") == "turn_context"]
    _require(contexts and all(c.get("turn_id") == turn and c.get("cwd") == cwd
             and c.get("model") == selection["model"] and c.get("effort") == selection["reasoning_effort"]
             for c in contexts), "source actual model/effort/context differs")
    inventory = {}
    for n, row in enumerate(active, starts[-1] + 2):
        event = row.get("payload", {})
        if row.get("type") == "event_msg" and event.get("type") == "item_completed" and event.get("item", {}).get("type") == "McpToolCall":
            ident = event["item"].get("id")
            _require(isinstance(ident, str) and ident and ident not in inventory
                     and event.get("thread_id") == thread and event.get("turn_id") == turn,
                     "source core occurrence duplicate or cross-thread/turn")
            _require(type(event.get("started_at_ms")) is int and type(event.get("completed_at_ms")) is int
                     and 0 < event["started_at_ms"] <= event["completed_at_ms"], "source core timing absent or inconsistent")
            core_to_app(event["item"])
            inventory[ident] = dict(event=event, line=n)
    return inventory


def parse_protocol(raw, requests, transport, session_raw, source, *, receipt, producer, require_tool_calls=True):
    """Same validator for native and explicitly synthetic protocol fixtures."""
    from check_next_round_acceptance import parse_native
    fields = {"profile", "build", "policy_sha256", "evidence_kind", "run_id", "connection_id", "executable_path",
              "runtime_root", "launch_config", "selection", "thread_id", "turn_id", "context_sha256",
              "events_sha256", "requests_sha256", "transport_sha256", "session_sha256"}
    _require(isinstance(source, dict) and fields <= set(source) <= fields | {"stderr_sha256", "owner"}
             and isinstance(source["selection"], dict) and set(source["selection"]) == {"model", "reasoning_effort"}
             and isinstance(source["launch_config"], dict) and isinstance(source["runtime_root"], str)
             and Path(source["runtime_root"]).is_absolute(), "source binding envelope unsupported")
    _require(source.get("profile") == PROFILE and source.get("build") == BUILD
             and source.get("policy_sha256") == policy_hashes(), "source profile/build/policy differs")
    _require(source.get("evidence_kind") in {"native", "synthetic"}
             and uuid_value(source.get("run_id")) and uuid_value(source.get("connection_id")),
             "source run/connection identity invalid")
    _require(source.get("executable_path") == receipt["command"][0] and Path(source["executable_path"]).is_absolute(),
             "source executable identity differs")
    _require(source.get("context_sha256") == _digest({k: v for k, v in receipt.items() if k != "source"}),
             "source run context binding differs")
    if "owner" in source:
        from nr03_capture_owner import validate_source_owner
        validate_source_owner(source["owner"], source, receipt)
    for name, data in (("events", raw), ("requests", requests), ("transport", transport), ("session", session_raw)):
        _require(source.get(name + "_sha256") == hashlib.sha256(data).hexdigest(), "source " + name + " byte binding differs")
    received, sent, timeline, session = lines(raw), lines(requests), lines(transport), lines(session_raw)
    offsets = {"received": 0, "sent": 0}
    raws = {"received": raw.splitlines(keepends=True), "sent": requests.splitlines(keepends=True)}
    ordered = []
    last = 0
    for entry in timeline:
        direction = entry.get("direction")
        _require(set(entry) == {"direction", "line", "sha256", "monotonic_ns", "run_id", "connection_id"}
                 and direction in offsets and type(entry["monotonic_ns"]) is int and entry["monotonic_ns"] > last
                 and entry["run_id"] == source["run_id"] and entry["connection_id"] == source["connection_id"],
                 "source transport ordering/run differs")
        index = offsets[direction]
        _require(index < len(raws[direction]) and entry["line"] == index + 1
                 and entry["sha256"] == hashlib.sha256(raws[direction][index]).hexdigest(),
                 "source transport lost/duplicated bytes")
        ordered.append((direction, (received if direction == "received" else sent)[index], index + 1))
        offsets[direction] += 1
        last = entry["monotonic_ns"]
    _require(all(offsets[k] == len(v) for k, v in raws.items()), "source transport incomplete")
    thread, turn = source.get("thread_id"), source.get("turn_id")
    _require(all(isinstance(v, str) and v for v in (thread, turn)), "source thread/turn absent")
    selection = source["selection"]
    core = core_inventory(session, thread, turn, receipt["cwd"], selection)
    pending_requests, responses, sent_methods = {}, {}, []
    pending, completed, events, locations = {}, {}, [], {}
    started = done = initialized = False
    from nr03_runtime_policy import NOTIFICATIONS, RuntimeBoundary
    boundary = RuntimeBoundary(source["launch_config"], source["runtime_root"], thread=thread)
    for direction, row, line in ordered:
        if direction == "sent":
            boundary.request(row)
            method, ident = row.get("method"), row.get("id")
            _require(method in {"initialize", "initialized", "config/read", "thread/start", "thread/resume", "mcpServerStatus/list", "turn/start"},
                     "source unsupported client request")
            if method == "initialized":
                _require("id" not in row and "initialize" in responses and not initialized, "source initialization differs")
                initialized = True
            else:
                _require(type(ident) is int and ident not in pending_requests and ident not in [r[0] for r in responses.values()],
                         "source duplicate request identity")
                if method != "initialize":
                    _require(initialized, "source request before initialization")
                pending_requests[ident] = row
            sent_methods.append(method)
            continue
        boundary.receive(row)
        if "id" in row:
            req = pending_requests.pop(row["id"], None)
            _require(req is not None and isinstance(row.get("result"), dict) and req["method"] not in responses,
                     "source unexpected/duplicate response")
            responses[req["method"]] = (row["id"], req, row["result"])
            continue
        method, params = row.get("method"), row.get("params")
        _require(isinstance(params, dict), "source malformed notification")
        if "threadId" in params:
            _require(params["threadId"] == thread, "source notification thread differs")
        if "turnId" in params:
            _require(params["turnId"] == turn, "source notification turn differs")
        if method == "thread/started":
            _require(not started and params.get("thread", {}).get("id") == thread, "source thread notification differs")
        elif method in {"turn/started", "turn/completed"}:
            t = params.get("turn", {})
            _require(params.get("threadId") == thread and t.get("id") == turn and t.get("error") is None,
                     "source turn identity/error differs")
            if method == "turn/started":
                _require(not started and not done and t.get("status") == "inProgress", "source turn start missing/duplicate")
                started = True
                events.extend([dict(type="thread.started", thread_id=thread), dict(type="turn.started")])
            else:
                _require(started and not done and not pending and t.get("status") == "completed", "source turn incomplete")
                # A snapshot can verify, never create, completed lifecycles.
                snapshots = [i for i in t.get("items", []) if i.get("type") == "mcpToolCall"]
                _require(not snapshots or _digest(snapshots) == _digest(list(completed.values())), "source final snapshot contradicts lifecycles")
                events.append(dict(type="turn.completed"))
                done = True
        elif method in {"item/started", "item/completed"}:
            _require(started and not done and params.get("threadId") == thread and params.get("turnId") == turn,
                     "source item outside original current turn")
            item = params.get("item", {})
            kind, ident = item.get("type"), item.get("id")
            _require(isinstance(ident, str) and ident and kind in
                     {"mcpToolCall", "agentMessage", "reasoning", "userMessage", "functionCallOutput"},
                     "source forbidden or unsupported non-MCP action")
            if kind == "mcpToolCall":
                _require(ident in core and ident not in completed, "source duplicate/unpersisted MCP occurrence")
                original = core[ident]["event"]
                if method == "item/started":
                    _require(ident not in pending and item.get("status") == "inProgress"
                             and item.get("result") is None and item.get("error") is None
                             and params.get("startedAtMs") == original["started_at_ms"], "source missing/duplicate/contradictory start")
                    expected = core_to_app(dict(original["item"], status="inProgress", result=None, error=None, duration=None))
                    _require(_digest(item) == _digest(expected), "source start operation/arguments/metadata differs")
                    pending[ident] = line
                    converted = dict(type="mcp_tool_call", id=ident, **{k: deepcopy(item[k]) for k in ("server", "tool", "arguments")},
                                     status="in_progress", result=None, error=None)
                else:
                    start = pending.pop(ident, None)
                    _require(start is not None and params.get("completedAtMs") == original["completed_at_ms"]
                             and _digest(item) == _digest(core_to_app(original["item"])), "source terminal differs or original start absent")
                    completed[ident] = item
                    result = deepcopy(original["item"].get("result"))
                    if result is not None:
                        result["structured_content"] = result.pop("structuredContent", None)
                    converted = dict(type="mcp_tool_call", id=ident, **{k: deepcopy(item[k]) for k in ("server", "tool", "arguments")},
                                     status=item["status"], result=result, error=deepcopy(item["error"]))
                    if item["status"] == "failed":
                        # Only this validated source projection represents the
                        # core failure flag via the legacy consumer's status.
                        # Complete originals travel with the failure into its
                        # ledger; neither source input is edited or discarded.
                        if result is not None:
                            result.pop("isError", None)
                        converted["source_failure"] = dict(profile=PROFILE, run_id=source["run_id"],
                            connection_id=source["connection_id"], thread_id=thread, turn_id=turn, item_id=ident,
                            core_line=core[ident]["line"], end_line=line,
                            original_core_event=deepcopy(session[core[ident]["line"] - 1]),
                            original_app_server_notification=deepcopy(row))
                    locations[ident] = dict(profile=PROFILE, run_id=source["run_id"], connection_id=source["connection_id"],
                        thread_id=thread, turn_id=turn, item_id=ident, start_line=start, end_line=line,
                        core_line=core[ident]["line"], core_item=deepcopy(original["item"]))
                events.append(dict(type=method.replace("/", "."), item=converted))
            elif kind == "agentMessage":
                events.append(dict(type=method.replace("/", "."), item=dict(type="agent_message", id=ident, text=item.get("text"))))
            elif kind == "functionCallOutput":
                _require(item.get("name") in {"exec", "functions.exec"}, "source forbidden function output")
        elif method in NOTIFICATIONS:
            pass  # Complete original envelope was validated by the shared boundary.
        elif method in PASSIVE:
            _require(method != "model/rerouted", "source model rerouted")
        else:
            _require(False, "source unsupported notification: " + str(method))
    _require(done and set(core) == set(completed) and not pending_requests, "source inventory/transport incomplete")
    boundary.require_ready()
    from capture_nr03_app_server import validate_exchange
    validate_exchange(sent_methods, responses, source, receipt)
    parsed_thread, calls, messages = parse_native(events, producer, require_tool_calls=require_tool_calls)
    for call in calls:
        call["source_occurrence"] = locations[call["id"]]
        call["core_result"] = deepcopy(core[call["id"]]["event"]["item"].get("result"))
    return dict(thread=parsed_thread, calls=calls, messages=messages, events=events, source=source)


def parse_capture(directory, stage, receipt, producer, *, require_tool_calls=True):
    """Explicit profile dispatch: old CLI bytes are never auto-detected as original."""
    from check_next_round_acceptance import parse_native
    directory = Path(directory)
    raw = (directory / f"{stage}.jsonl").read_bytes()
    if "source" not in receipt:
        events = lines(raw)
        thread, calls, messages = parse_native(events, producer, require_tool_calls=require_tool_calls)
        binding_path = directory / f"{stage}.delivery.json"
        binding = decoded(binding_path.read_text()) if binding_path.exists() else {}
        if isinstance(binding, dict) and "source_fixture_sha256" in binding:
            path = directory / f"{stage}.source-fixture.json"
            _require(not path.is_symlink() and _file_sha256(str(path)) == binding["source_fixture_sha256"],
                     "source synthetic supplement binding differs")
            fixture = decoded(path.read_text())
            _require(isinstance(fixture, dict) and fixture.get("schema_version") == "nr03-source-fixture.v1"
                     and fixture.get("legacy_receipt_sha256") == _file_sha256(str(directory / f"{stage}.receipt.json"))
                     and fixture.get("legacy_events_sha256") == hashlib.sha256(raw).hexdigest()
                     and fixture.get("legacy_session_sha256") == binding["session_sha256"], "source fixture legacy bytes differ")
            original_session = Path(binding["session_path"]).read_bytes()
            _require(hashlib.sha256(original_session).hexdigest() == binding["session_sha256"], "source fixture original prefix differs")
            supplied = fixture["receipt"]
            _require(supplied.get("source", {}).get("evidence_kind") == "synthetic"
                     and {k: v for k, v in supplied.items() if k not in {"command", "source"}} ==
                         {k: v for k, v in receipt.items() if k != "command"}, "source fixture context/provenance differs")
            from capture_nr03_app_server import launch_config
            from nr03_scenario import SERVER
            config = supplied["source"]["launch_config"]
            server = config["mcp_servers"][SERVER]
            _require(config == launch_config(server["command"], **supplied["source"]["selection"],
                     journal_disabled=server["env"]["VEQTOR_DISABLE_DECISION_RECORD"] == "1", server_args=server["args"]),
                     "source fixture launch policy differs")
            for key, value in (("command", server["command"]), ("args", server["args"]),
                               ("env.VEQTOR_TRACKED_CHANGE_AUTHOR", server["env"]["VEQTOR_TRACKED_CHANGE_AUTHOR"]),
                               ("env.VEQTOR_DISABLE_DECISION_RECORD", server["env"]["VEQTOR_DISABLE_DECISION_RECORD"])):
                prefix = f"mcp_servers.{SERVER}.{key}="
                actual = [decoded(part[len(prefix):]) for part in receipt["command"] if part.startswith(prefix)]
                _require(len(actual) == 1 and _digest(actual[0]) == _digest(value), "source fixture server launch differs from original")
            parsed = parse_protocol(*(fixture[key].encode() for key in ("raw", "requests", "transport", "session")),
                supplied["source"], receipt=supplied, producer=producer, require_tool_calls=require_tool_calls)
            def facts(cs):
                return [{k: c.get(k) for k in ("id", "server", "tool", "arguments", "failed", "payload", "error")} for c in cs]
            def actions(ss):
                return [r for r in ss if r.get("type") == "response_item"]
            _require(_digest(facts(parsed["calls"])) == _digest(facts(calls)) and parsed["thread"] == thread
                     and [m["text"] for m in parsed["messages"]] == [m["text"] for m in messages]
                     and _digest(actions(lines(fixture["session"].encode()))) == _digest(actions(lines(original_session))),
                     "source fixture changed original call/action/output facts")
            # Consumers use original synthetic event positions and frozen state;
            # qualification adds occurrence evidence, never deletes a call.
            for call, supplied_call in zip(calls, parsed["calls"]):
                call.update(source_occurrence=supplied_call["source_occurrence"], core_result=supplied_call["core_result"])
            return dict(thread=thread, calls=calls, messages=messages, events=events, source=supplied["source"],
                        source_fixture_session=lines(fixture["session"].encode()))
        return dict(thread=thread, calls=calls, messages=messages, events=events, source=None)
    source = receipt["source"]
    data = {}
    for kind, suffix in (("requests", "requests.jsonl"), ("transport", "transport.jsonl"), ("session_raw", "session.jsonl")):
        path = directory / f"{stage}.{suffix}"
        _require(path.is_file() and not path.is_symlink(), "source evidence file missing or symlink")
        data[kind] = path.read_bytes()
    _require(source.get("stderr_sha256") == _file_sha256(str(directory / f"{stage}.stderr.txt")), "source diagnostics byte binding differs")
    return parse_protocol(raw, source=source, receipt=receipt, producer=producer,
                          require_tool_calls=require_tool_calls, **data)
