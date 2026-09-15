# SPDX-License-Identifier: Apache-2.0
"""Explicit additional synthetic source evidence, never native log repair.

Legacy bytes and original response items are inputs, not rewritten outputs.
New synthetic lifecycle/context definitions are supplied separately. Production
parsers verify every source binding and equality of the preserved call facts.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid

from check_codex_acceptance import _digest, _file_sha256
from nr03_app_server import BUILD, PROFILE, core_to_app, lines, policy_hashes
from capture_nr03_app_server import command_for, initialize_request, launch_config


def encode(rows):
    return "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)


def supplement(events, old_session, receipt, config):
    thread = events[0]["thread_id"]
    run, connection = str(uuid.uuid4()), str(uuid.uuid4())
    turn = "synthetic-source-turn-" + run
    runtime = "/synthetic-private-runtime/" + run
    core, starts = {}, {}
    for n, event in enumerate(events):
        item = event.get("item", {})
        if item.get("type") != "mcp_tool_call":
            continue
        if event["type"] == "item.started":
            starts[item["id"]] = 1000 + n
            continue
        result = deepcopy(item.get("result"))
        if isinstance(result, dict):
            result["structuredContent"] = result.pop("structured_content", None)
            result["isError"] = item["status"] == "failed"
        body = dict(type="McpToolCall", **{k: deepcopy(item[k]) for k in ("id", "server", "tool", "arguments", "status")},
                    result=result, duration=dict(secs=0, nanos=(1000+n-starts[item["id"]])*10**6))
        if item.get("error") is not None:
            body["error"] = deepcopy(item["error"])
        core[item["id"]] = dict(type="event_msg", payload=dict(type="item_completed", thread_id=thread, turn_id=turn,
            started_at_ms=starts[item["id"]], completed_at_ms=1000+n, item=body))
    session = deepcopy(old_session)
    session[0]["payload"]["cli_version"] = BUILD["version"]
    for row in session:
        if row.get("type") == "event_msg" and row["payload"].get("type") in {"task_started", "task_complete"}:
            row["payload"]["turn_id"] = turn
    session.insert(2, dict(type="turn_context", payload=dict(turn_id=turn, cwd=receipt["cwd"],
        model=config["model"], effort=config["model_reasoning_effort"])))
    augmented, supplied = [], set()
    for row in session:
        p = row.get("payload", {})
        if row.get("type") == "event_msg" and p.get("type") == "item_completed" and p.get("item", {}).get("type") == "McpToolCall":
            ident = p["item"]["id"]
            completion = deepcopy(core[ident])
            supplied_item = deepcopy(p["item"])
            if isinstance(supplied_item.get("result"), dict):
                supplied_item["result"].setdefault("isError", core[ident]["payload"]["item"]["status"] == "failed")
            completion["payload"]["item"].update(supplied_item)
            augmented.append(completion)
            supplied.add(ident)
            continue
        if row.get("type") == "response_item" and p.get("type") in {"function_call_output", "custom_tool_call_output"}:
            ident = p.get("call_id")
            if ident in core and ident not in supplied:
                augmented.append(deepcopy(core[ident]))
                supplied.add(ident)
        augmented.append(row)
    assert supplied == set(core), (supplied, set(core))
    receipt = deepcopy(receipt)
    receipt["command"] = command_for(receipt["command"][0], config)
    selection = dict(model=config["model"], reasoning_effort=config["model_reasoning_effort"])
    source = dict(profile=PROFILE, build=deepcopy(BUILD), evidence_kind="synthetic", run_id=run, connection_id=connection,
        executable_path=receipt["command"][0], runtime_root=runtime, launch_config=config, selection=selection,
        thread_id=thread, turn_id=turn, policy_sha256=policy_hashes())
    incoming, outgoing, timeline = [], [], []
    def wire(direction, row):
        group = incoming if direction == "received" else outgoing
        group.append(row)
        timeline.append(dict(direction=direction, line=len(group), sha256=hashlib.sha256(encode([row]).encode()).hexdigest(),
                             monotonic_ns=len(timeline)+1, run_id=run, connection_id=connection))
    def request(ident, method, params, result):
        wire("sent", dict(id=ident, method=method, params=params))
        wire("received", dict(id=ident, result=result))
    request(1, "initialize", initialize_request(), dict(userAgent="synthetic app server profile fixture"))
    wire("sent", dict(method="initialized"))
    request(2, "config/read", dict(includeLayers=True, cwd=receipt["cwd"]), dict(config=config, origins={}, layers=[
        dict(name=dict(type="sessionFlags"), version="synthetic", config=config),
        dict(name=dict(type="user", file=runtime+"/config.toml", profile=None), version="synthetic", config={})]))
    params = dict(model=selection["model"], cwd=receipt["cwd"], approvalPolicy="never", sandbox="danger-full-access")
    if receipt.get("resumed_thread_id"):
        params.update(threadId=thread, path=runtime+"/resume.jsonl", excludeTurns=True)
    else:
        params.update(ephemeral=False)
    request(3, "thread/resume" if receipt.get("resumed_thread_id") else "thread/start", params,
        dict(thread=dict(id=thread, path=runtime+"/session.jsonl"), model=selection["model"], reasoningEffort=selection["reasoning_effort"],
             cwd=receipt["cwd"], approvalPolicy="never", instructionSources=[]))
    request(4, "turn/start", dict(threadId=thread, model=selection["model"], effort=selection["reasoning_effort"],
        input=[dict(type="text", text=receipt.pop("fixture_prompt"), text_elements=[])]), dict(turn=dict(id=turn, status="inProgress")))
    for event in events[1:]:
        kind, item = event["type"], event.get("item", {})
        if kind in {"turn.started", "turn.completed"}:
            wire("received", dict(method=kind.replace(".", "/"), params=dict(threadId=thread,
                turn=dict(id=turn, status="inProgress" if kind == "turn.started" else "completed", error=None, items=[]))))
        elif item.get("type") == "mcp_tool_call":
            original = core[item["id"]]["payload"]
            starting = kind == "item.started"
            c = dict(original["item"], status="inProgress", result=None, error=None, duration=None) if starting else original["item"]
            wire("received", dict(method=kind.replace(".", "/"), params=dict(threadId=thread, turnId=turn,
                item=core_to_app(c), **({"startedAtMs": original["started_at_ms"]} if starting else {"completedAtMs": original["completed_at_ms"]}))))
        elif item.get("type") in {"agent_message", "reasoning"}:
            wire("received", dict(method=kind.replace(".", "/"), params=dict(threadId=thread, turnId=turn,
                item=dict(item, type="agentMessage" if item["type"] == "agent_message" else "reasoning"))))
        else:
            raise AssertionError(event)
    fixture = dict(raw=encode(incoming), requests=encode(outgoing), transport=encode(timeline), session=encode(augmented))
    for key, target in (("raw", "events"), ("requests", "requests"), ("transport", "transport"), ("session", "session")):
        source[target+"_sha256"] = hashlib.sha256(fixture[key].encode()).hexdigest()
    source["context_sha256"] = _digest(receipt)
    receipt["source"] = source
    fixture["receipt"] = receipt
    return fixture


def attach(folder, stage, *, config=None):
    folder = Path(folder)
    marker = folder / f"{stage}.source-fixture-config.json"
    if config is not None:
        marker.write_text(json.dumps(config))
    else:
        config = json.loads(marker.read_text())
    receipt = json.loads((folder / f"{stage}.receipt.json").read_text())
    binding_path = folder / f"{stage}.delivery.json"
    binding = json.loads(binding_path.read_text())
    raw = (folder / f"{stage}.jsonl").read_bytes()
    old = Path(binding["session_path"]).read_bytes()
    fixture = supplement(lines(raw), lines(old), dict(receipt, fixture_prompt=(folder / f"{stage}.prompt.txt").read_text()), config)
    fixture.update(schema_version="nr03-source-fixture.v1", legacy_receipt_sha256=_file_sha256(str(folder / f"{stage}.receipt.json")),
                   legacy_events_sha256=hashlib.sha256(raw).hexdigest(), legacy_session_sha256=hashlib.sha256(old).hexdigest())
    path = folder / f"{stage}.source-fixture.json"
    path.write_text(json.dumps(fixture))
    binding["source_fixture_sha256"] = _file_sha256(str(path))
    binding_path.write_text(json.dumps(binding))
    assert (folder / f"{stage}.jsonl").read_bytes() == raw and Path(binding["session_path"]).read_bytes() == old


def config_for(installation, baseline):
    return launch_config(installation["python"], **baseline["client_selection"], journal_disabled=baseline["variant"] == "journal-unavailable")
