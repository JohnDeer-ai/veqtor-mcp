# SPDX-License-Identifier: Apache-2.0
"""Typed F17/F18 refusals on original-order synthetic capture and full parser."""

from copy import deepcopy
import hashlib
import json
import subprocess
import sys
import uuid

import pytest

from capture_nr03_app_server import StdioCapture
from check_codex_acceptance import EvidenceError
from nr03_app_server import lines
from nr03_runtime_policy import RuntimeBoundary
from nr03_source_fixtures import encode
import test_next_round_source_profile as source

files, source_case = source.files, source.source_case


def ordered(f):
    groups = {
        "sent": lines(f["requests"].encode()),
        "received": lines(f["raw"].encode()),
    }
    return [
        (r["direction"], groups[r["direction"]][r["line"] - 1])
        for r in lines(f["transport"].encode())
    ]


def replace_order(f, order):
    groups = dict(sent=[], received=[])
    timeline = []
    scope = f["receipt"]["source"]
    for d, row in order:
        groups[d].append(row)
        timeline.append(
            dict(
                direction=d,
                line=len(groups[d]),
                sha256=hashlib.sha256(encode([row]).encode()).hexdigest(),
                monotonic_ns=len(timeline) + 1,
                run_id=scope["run_id"],
                connection_id=scope["connection_id"],
            )
        )
    f.update(
        raw=encode(groups["received"]),
        requests=encode(groups["sent"]),
        transport=encode(timeline),
    )
    source.rebind(f, rebuild_transport=False)


def feed(f):
    scope = f["receipt"]["source"]
    boundary = RuntimeBoundary(scope["launch_config"], scope["runtime_root"])
    for d, row in ordered(f):
        (boundary.request if d == "sent" else boundary.receive)(row)
    boundary.require_ready()


FAULTS = (
    "remote_connected",
    "remote_unknown",
    "remote_environment",
    "remote_identity",
    "remote_missing",
    "remote_extra",
    "timestamp_bool",
    "timestamp_missing",
    "timestamp_backwards",
    "startup_wrong_thread",
    "startup_extra_server",
    "startup_failed",
    "startup_cancelled",
    "startup_error",
    "startup_failure_reason",
    "startup_missing",
    "startup_ready_first",
    "startup_restart",
    "unknown_notification",
    "missing_ready",
    "missing_remote",
    "extra_server_inventory",
    "missing_inventory",
    "inventory_cursor",
    "inventory_disconnected",
    "extra_tool",
    "missing_tool",
    "inventory_resources",
    "inventory_plugin",
    "features_apps",
    "features_remote_plugin",
    "features_remote_control",
    "features_unknown",
    "features_type",
    "inventory_before_ready",
)


def mutate(order, fault):
    remote = next(
        row for _, row in order if row.get("method") == "remoteControl/status/changed"
    )
    startup = [
        row
        for _, row in order
        if row.get("method") == "mcpServer/startupStatus/updated"
    ]
    inventory = next(
        row["result"] for _, row in order if row.get("id") == 5 and "result" in row
    )
    if fault in {"missing_ready", "missing_remote", "missing_inventory"}:
        target = (
            startup[-1]
            if fault == "missing_ready"
            else remote
            if fault == "missing_remote"
            else next(row for _, row in order if row.get("id") == 5 and "result" in row)
        )
        order[:] = [(d, row) for d, row in order if row is not target]
    elif fault.startswith("remote_"):
        p = remote["params"]
        if fault == "remote_connected":
            p["status"] = "connected"
        elif fault == "remote_unknown":
            p["status"] = "future"
        elif fault == "remote_environment":
            p["environmentId"] = "remote"
        elif fault == "remote_identity":
            p["installationId"] = 7
        elif fault == "remote_missing":
            del p["status"]
        elif fault == "remote_extra":
            p["extra"] = None
    elif fault.startswith("timestamp"):
        if fault == "timestamp_bool":
            remote["emittedAtMs"] = True
        elif fault == "timestamp_missing":
            del remote["emittedAtMs"]
        else:
            startup[-1]["emittedAtMs"] = 1
    elif fault.startswith("startup_"):
        p = startup[0]["params"]
        if fault == "startup_wrong_thread":
            p["threadId"] = "other"
        elif fault == "startup_extra_server":
            p["name"] = "codex_apps"
        elif fault == "startup_failed":
            p["status"] = "failed"
        elif fault == "startup_cancelled":
            p["status"] = "cancelled"
        elif fault == "startup_error":
            p["error"] = "failed"
        elif fault == "startup_failure_reason":
            p["failureReason"] = "reauthenticationRequired"
        elif fault == "startup_missing":
            del p["error"]
        elif fault == "startup_ready_first":
            p["status"] = "ready"
        else:
            startup[-1]["params"]["status"] = "starting"
    elif fault == "unknown_notification":
        remote["method"] = "unknown/new"
    elif fault == "extra_server_inventory":
        inventory["data"].append(dict(inventory["data"][0], name="codex_apps"))
    elif fault == "inventory_cursor":
        inventory["nextCursor"] = "more"
    elif fault == "inventory_disconnected":
        inventory["data"][0]["runtimeStatus"] = "disconnected"
    elif fault == "extra_tool":
        inventory["data"][0]["tools"]["other"] = dict(name="other", inputSchema={})
    elif fault == "missing_tool":
        del inventory["data"][0]["tools"]["inspect_document"]
    elif fault == "inventory_resources":
        inventory["data"][0]["resources"] = [{}]
    elif fault == "inventory_plugin":
        inventory["data"][0]["pluginId"] = "extra"
    elif fault.startswith("features_"):
        features = next(
            row["result"]["config"]["features"]
            for _, row in order
            if row.get("id") == 2 and "result" in row
        )
        key = fault.removeprefix("features_")
        if key == "type":
            features["apps"] = 0
        elif key == "unknown":
            features["future"] = False
        else:
            features[key] = True
    elif fault == "inventory_before_ready":
        at = next(
            i for i, (d, row) in enumerate(order) if d == "sent" and row.get("id") == 5
        )
        row = order.pop(at)
        at = next(i for i, (_, row) in enumerate(order) if row is startup[-1])
        order.insert(at, row)
    else:
        raise AssertionError(fault)


@pytest.mark.parametrize("fault", FAULTS)
def test_live_boundary_and_full_original_parser_refuse_and_restore(source_case, fault):
    f, producer = deepcopy(source_case)
    original = deepcopy(f)
    before = source.assess((f, producer))
    feed(f)
    order = ordered(f)
    mutate(order, fault)
    replace_order(f, order)
    with pytest.raises(EvidenceError):
        feed(f)
    with pytest.raises(EvidenceError):
        source.assess((f, producer))
    feed(original)
    assert set(source.assess((original, producer))) == set(before)


def test_actual_recorder_refuses_original_bad_startup_before_turn(
    source_case, tmp_path
):
    f, _ = source_case
    order = ordered(deepcopy(f))
    mutate(order, "startup_extra_server")
    # An inert subprocess only emits labelled fixture bytes in response to
    # requests. It is never Codex and cannot execute MCP or business work.
    batches, current = [], []
    for d, row in order:
        if d == "sent":
            if current:
                batches[-1].extend(current)
                current = []
            batches.append([])
        else:
            current.append(row)
    batches[-1].extend(current)
    script = "import sys,json\nfor batch in json.loads(sys.argv[1]):\n if not sys.stdin.readline(): break\n for row in batch: print(json.dumps(row),flush=True)\n"
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", script, json.dumps(batches)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    scope = f["receipt"]["source"]
    stream = StdioCapture(
        process,
        tmp_path,
        "inert",
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        boundary=RuntimeBoundary(scope["launch_config"], scope["runtime_root"]),
    )
    try:
        with pytest.raises(EvidenceError):
            for d, row in order:
                if d == "sent":
                    stream.send(row["method"], row.get("params"), row.get("id"))
                    if row.get("id") is not None:
                        stream.response(row["id"])
    finally:
        with pytest.raises(EvidenceError):
            stream.close()
    assert process.poll() is not None and stream.cleanup_proven
    assert not any(
        r.get("method") == "turn/start"
        for r in lines((tmp_path / "inert.requests.jsonl").read_bytes())
    )
    assert b"codex_apps" in (tmp_path / "inert.jsonl").read_bytes()


def test_duplicate_json_key_is_not_a_notification_projection():
    with pytest.raises(EvidenceError):
        lines(
            b'{"method":"remoteControl/status/changed","params":{"status":"disabled","status":"connected"}}\n'
        )


def test_repeated_ready_retains_all_occurrences_and_late_failure_refuses(source_case):
    f, producer = deepcopy(source_case)
    order = ordered(f)
    ready = next(
        row
        for _, row in order
        if row.get("method") == "mcpServer/startupStatus/updated"
        and row["params"]["status"] == "ready"
    )
    at = (
        next(
            i for i, (_, row) in enumerate(order) if row.get("method") == "turn/started"
        )
        + 1
    )
    order.insert(at, ("received", deepcopy(ready)))
    replace_order(f, order)
    feed(f)
    assert len(source.assess((f, producer))) == 5
    order[at][1]["params"]["status"] = "failed"
    replace_order(f, order)
    with pytest.raises(EvidenceError):
        feed(f)
    with pytest.raises(EvidenceError):
        source.assess((f, producer))
