# SPDX-License-Identifier: Apache-2.0
"""Synthetic state lifecycle controls; no Codex or model process is launched."""
from copy import deepcopy
import json

import pytest

from check_codex_acceptance import EvidenceError
from nr03_app_server import BUILD, PROFILE, policy_hashes
from nr03_resume_state import companion, parent_state, restore_state, state_binding, validate_ancestry, validate_parent_prefix
from nr03_runtime_policy import RuntimeBoundary


@pytest.fixture
def parent(tmp_path):
    thread = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    rows = [dict(type="session_meta", payload=dict(id=thread, cwd=str(tmp_path), cli_version=BUILD["version"], history_mode="legacy")),
            dict(type="event_msg", payload=dict(type="task_started", turn_id="one")),
            dict(type="event_msg", payload=dict(type="task_complete", turn_id="one"))]
    prefix = b"".join(json.dumps(r).encode() + b"\n" for r in rows)
    raw = prefix + b'{"type":"metadata","payload":{"synthetic":true}}\n'
    path = tmp_path / "brief.session.jsonl"
    path.write_bytes(prefix)
    companion(path).write_bytes(raw)
    binding = state_binding(raw, prefix, "sessions/2026/09/15/rollout-synthetic-" + thread + ".jsonl", thread=thread, cwd=tmp_path)
    source = dict(profile=PROFILE, build=BUILD, evidence_kind="native", policy_sha256=policy_hashes(), thread_id=thread,
        selection=dict(model="gpt-6-astra", reasoning_effort="high"), session_sha256=binding["prefix_sha256"], persisted_state=binding)
    # Synthetic fields explicitly exercise native validation; never capture evidence.
    return path, source, raw


def load(parent):
    path, source, _ = parent
    return parent_state(path, source, thread=source["thread_id"], cwd=path.parent, selection=source["selection"])


@pytest.mark.parametrize("fault", ["missing_full", "missing_prefix", "symlink", "foreign_id", "wrong_cwd", "paginated",
    "missing_mode", "stale_prefix", "stale_full", "boolean_length", "wrong_policy", "wrong_build", "synthetic_source",
    "missing_binding", "foreign_path", "traversal", "unsettled_turn", "extra_binding"])
def test_parent_state_refuses_before_process_and_restores(parent, monkeypatch, tmp_path, fault):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("State refusal must precede a native process"))
    path, source, raw = parent
    prefix = path.read_bytes()
    original = deepcopy(source)
    assert load(parent)[0] == raw
    if fault == "missing_full":
        companion(path).unlink()
    elif fault == "missing_prefix":
        path.unlink()
    elif fault == "symlink":
        companion(path).unlink()
        target = tmp_path / "elsewhere"
        target.write_bytes(raw)
        companion(path).symlink_to(target)
    elif fault in {"foreign_id", "wrong_cwd", "paginated", "missing_mode"}:
        rows = [json.loads(r) for r in raw.splitlines()]
        meta = rows[0]["payload"]
        if fault == "missing_mode":
            del meta["history_mode"]
        else:
            meta[{"foreign_id": "id", "wrong_cwd": "cwd", "paginated": "history_mode"}[fault]] = "paginated" if fault == "paginated" else "other"
        companion(path).write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in rows))
    elif fault == "stale_prefix":
        path.write_bytes(prefix[:-1])
    elif fault == "stale_full":
        companion(path).write_bytes(prefix)
    elif fault == "unsettled_turn":
        companion(path).write_bytes(raw + b'{"type":"event_msg","payload":{"type":"task_started","turn_id":"two"}}\n')
    elif fault == "wrong_policy":
        source["policy_sha256"] = {}
    elif fault == "wrong_build":
        source["build"] = {}
    elif fault == "synthetic_source":
        source["evidence_kind"] = "synthetic"
    elif fault == "missing_binding":
        del source["persisted_state"]
    elif fault == "boolean_length":
        source["persisted_state"]["rollout_bytes"] = True
    elif fault == "extra_binding":
        source["persisted_state"]["extra"] = True
    else:
        source["persisted_state"]["relative_rollout_path"] = "sessions/../other.jsonl" if fault == "traversal" else "resume.jsonl"
    with pytest.raises(EvidenceError):
        load(parent)
    path.write_bytes(prefix)
    if companion(path).is_symlink():
        companion(path).unlink()
    companion(path).write_bytes(raw)
    source.clear()
    source.update(original)
    assert load(parent)[0] == raw


def test_complete_original_state_survives_cold_copy_and_ancestry(parent, tmp_path):
    raw, binding = load(parent)
    runtime = tmp_path / "fresh-private"
    runtime.mkdir(mode=0o700)
    target = restore_state(runtime, raw, binding)
    assert target.read_bytes() == raw and parent[0].read_bytes() != raw
    assert {str(p.relative_to(runtime)) for p in runtime.rglob("*") if p.is_file()} == {binding["relative_rollout_path"]}
    validate_parent_prefix(raw + b"child bytes", binding, thread=binding["thread_id"], cwd=binding["cwd"])
    source = dict(evidence_kind="native", parent_state=binding)
    validate_ancestry(source, parent[1])
    bad = deepcopy(parent[1])
    bad["persisted_state"]["rollout_sha256"] = "f" * 64
    with pytest.raises(EvidenceError):
        validate_ancestry(source, bad)
    with pytest.raises(EvidenceError):
        validate_parent_prefix(parent[0].read_bytes(), binding, thread=binding["thread_id"], cwd=binding["cwd"])
    assert target.read_bytes() == raw


def resume_boundary():
    boundary = RuntimeBoundary({}, "/synthetic")
    boundary.methods = ["initialize", "initialized", "config/read"]
    boundary.replies = {"initialize", "config/read"}
    boundary.request(dict(id=3, method="thread/resume", params=dict(threadId="parent")))
    return boundary


@pytest.mark.parametrize("fault", ["wrong_thread", "extra_field", "duplicate", "before_reply", "after_turn", "initial_start", "goal_update", "boolean_time"])
def test_exact_empty_resume_snapshot_and_early_startup(fault):
    def ready():
        boundary = resume_boundary()
        boundary.receive(dict(method="mcpServer/startupStatus/updated", params=dict(threadId="parent", name="veqtor_nr03",
            status="starting", error=None, failureReason=None), emittedAtMs=1))
        boundary.receive(dict(id=3, result=dict(thread=dict(id="parent"))))
        return boundary
    event = dict(method="thread/goal/cleared", params=dict(threadId="parent"), emittedAtMs=2)
    positive = ready()
    positive.receive(event)
    assert positive.empty_goal_snapshot
    boundary, bad = ready(), deepcopy(event)
    if fault == "wrong_thread":
        bad["params"]["threadId"] = "foreign"
    elif fault == "extra_field":
        bad["params"]["goal"] = None
    elif fault == "duplicate":
        boundary.receive(event)
    elif fault == "before_reply":
        boundary = resume_boundary()
    elif fault == "after_turn":
        boundary.methods.append("turn/start")
    elif fault == "initial_start":
        boundary.replies.remove("thread/resume")
        boundary.replies.add("thread/start")
    elif fault == "goal_update":
        bad["method"] = "thread/goal/updated"
    else:
        bad["emittedAtMs"] = True
    with pytest.raises(EvidenceError):
        boundary.receive(bad)
    ready().receive(event)
    wrong = resume_boundary()
    with pytest.raises(EvidenceError):
        wrong.receive(dict(method="mcpServer/startupStatus/updated", params=dict(threadId="foreign", name="veqtor_nr03",
            status="starting", error=None, failureReason=None), emittedAtMs=1))
