# SPDX-License-Identifier: Apache-2.0
"""NR03-F01/F02/F03 regressions: synthetic capture/evidence, never native acceptance."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

import test_next_round_acceptance as nr03
from test_next_round_acceptance import MODEL, SERVER, checker, json_write, mutate_events, prep, server
import capture_next_round_observation as observation

prepared = nr03.prepared
evidence = nr03.evidence


def turn(thread, *, payload=None, folder=None, message="Header editing is unsupported; no file was created."):
    events = [dict(type="thread.started", thread_id=thread), dict(type="turn.started")]
    if payload is not None:
        identity = dict(id="read", type="mcp_tool_call", server=SERVER, tool="read_deal_positions",
                        arguments=dict(folder=folder, check_sources=True))
        events.extend([
            dict(type="item.started", item=dict(identity, status="in_progress", result=None, error=None)),
            dict(type="item.completed", item=dict(identity, status="completed", error=None,
                 result=dict(structured_content=payload, content=[dict(type="text", text=json.dumps(payload))]))),
        ])
    events.extend([dict(type="item.completed", item=dict(id="message", type="agent_message", text=message)),
                   dict(type="turn.completed")])
    return events


def test_f01_no_tool_refusal_can_resume_explicit_exclusion(prepared, monkeypatch, tmp_path):
    bundle, b, report = prepared
    monkeypatch.setattr(observation, "installed", lambda value: value)
    plan = dict(scenario="mandatory header then explicit exclusion",
        expected=dict(refusal="no tool call or output required", exclusion="same actual conversation"),
        steps=[dict(id="brief", resume=None, prompt="Read the selected saved positions."),
               dict(id="mandatory", resume="brief", prompt="The header change is mandatory. Do not write a partial file."),
               dict(id="exclude", resume="mandatory", prompt="Exclude the header change and list it as manual work. Proceed with the agreed batch.")])
    path = tmp_path / "plan.json"
    json_write(path, plan)
    observation.freeze(bundle, path)
    launches = []

    def emit(command, *, input, cwd, stdout, stderr, check):
        index = len(launches)
        launches.append((command, input, cwd))
        payload = None if index == 1 else server.read_deal_positions(folder=b["matter"], check_sources=True)
        events = turn("synthetic-same-conversation", payload=payload, folder=b["matter"])
        stdout.write(("\n".join(json.dumps(event) for event in events) + "\n").encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(observation.subprocess, "run", emit)
    for step in ("brief", "mandatory", "exclude"):
        assert observation.capture(bundle, step, "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None) == 0
    assert len(launches) == 3
    assert launches[1][0][-2] == launches[2][0][-2] == "synthetic-same-conversation"
    assert launches[2][1].decode() == plan["steps"][2]["prompt"]
    assert launches[0][2] == launches[1][2] == launches[2][2]
    assert prep.state(b["matter"]) == b["initial_state"]
    parent_path = bundle / "observations/mandatory.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text().splitlines()]
    assert not any(e.get("item", {}).get("type") == "mcp_tool_call" for e in parent)
    receipt = prep.read_json(bundle / "observations/exclude.receipt.json")
    assert receipt["parent_receipt_sha256"] == checker._file_sha256(str(bundle / "observations/mandatory.receipt.json"))
    assert receipt["resumed_thread_id"] == "synthetic-same-conversation"
    assert receipt["acceptance_assessed"] is False
    with pytest.raises(checker.EvidenceError, match="required tool calls"):
        checker.parse_native(parent, report["producer"])


@pytest.mark.parametrize("with_tools", [False, True])
def test_f03_foreign_parent_cannot_launch_exclusion(prepared, monkeypatch, tmp_path, with_tools):
    bundle, b, _ = prepared
    monkeypatch.setattr(observation, "installed", lambda value: value)
    plan = dict(scenario="foreign native conversation after requested resume",
        expected=dict(result="refuse before launching exclusion"),
        steps=[dict(id="brief", resume=None, prompt="Read saved positions."),
               dict(id="mandatory", resume="brief", prompt="The header change is mandatory; no partial output."),
               dict(id="exclude", resume="mandatory", prompt="Exclude the header and proceed with the agreed batch.")])
    path = tmp_path / "plan.json"
    json_write(path, plan)
    observation.freeze(bundle, path)
    launches = []

    def emit(command, *, input, cwd, stdout, stderr, check):
        index = len(launches)
        launches.append(command)
        payload = server.read_deal_positions(folder=b["matter"], check_sources=True) if with_tools or index == 0 else None
        events = turn("synthetic-root-A" if index == 0 else "synthetic-foreign-B", payload=payload, folder=b["matter"])
        stdout.write(("\n".join(json.dumps(e) for e in events) + "\n").encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(observation.subprocess, "run", emit)
    for step in ("brief", "mandatory"):
        assert observation.capture(bundle, step, "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None) == 0
    assert launches[1][-2] == "synthetic-root-A"
    with pytest.raises(checker.EvidenceError, match="observation.*thread"):
        observation.capture(bundle, "exclude", "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None)
    assert len(launches) == 2
    assert not (bundle / "observations/exclude.prompt.txt").exists()
    assert prep.state(b["matter"]) == b["initial_state"]


@pytest.mark.parametrize("fault", [None, "foreign_ancestor", "forged_resume", "replaced_root",
                                  "broken_ancestor_link", "resumed_root", "missing_ancestor"])
def test_f03_original_conversation_survives_multiple_no_tool_followups(prepared, monkeypatch, tmp_path, fault):
    bundle, b, _ = prepared
    monkeypatch.setattr(observation, "installed", lambda value: value)
    ids = ["brief", "mandatory", "clarify", "exclude"]
    plan = dict(scenario="original conversation through multiple no-tool followups",
        expected=dict(result="preserve original conversation and reject incomplete or changed ancestry"),
        steps=[dict(id=name, resume=ids[i - 1] if i else None, prompt="Synthetic user followup: " + name)
               for i, name in enumerate(ids)])
    path = tmp_path / "plan.json"
    json_write(path, plan)
    observation.freeze(bundle, path)
    folder = bundle / "observations"
    launches = []

    def emit(command, *, input, cwd, stdout, stderr, check):
        launches.append(command)
        stdout.write(("\n".join(json.dumps(e) for e in turn("synthetic-root-A")) + "\n").encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(observation.subprocess, "run", emit)
    for name in ids[:-1]:
        assert observation.capture(bundle, name, "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None) == 0
    if fault is not None:
        ancestor = "brief" if fault in {"replaced_root", "resumed_root"} else "mandatory"
        receipt_path = folder / f"{ancestor}.receipt.json"
        receipt = prep.read_json(receipt_path)
        if fault in {"foreign_ancestor", "forged_resume", "replaced_root"}:
            events_path = folder / f"{ancestor}.jsonl"
            events = [json.loads(line) for line in events_path.read_text().splitlines()]
            events[0]["thread_id"] = "synthetic-foreign-B"
            events_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
            receipt["events_sha256"] = checker._file_sha256(str(events_path))
            if fault == "forged_resume":
                # Self-consistent intermediate request/result cannot replace the original identity.
                receipt["resumed_thread_id"] = receipt["command"][-2] = "synthetic-foreign-B"
        elif fault == "broken_ancestor_link":
            receipt["parent_receipt_sha256"] = "0" * 64
        elif fault == "resumed_root":
            receipt["resumed_thread_id"] = "synthetic-root-A"
        else:
            (folder / "mandatory.jsonl").unlink()
        json_write(receipt_path, receipt)
        # Keep downstream hashes valid so a shallow check of the direct parent
        # still sees a complete A turn, an A resume target and matching evidence.
        for index in range(ids.index(ancestor) + 1, 3):
            child = folder / f"{ids[index]}.receipt.json"
            value = prep.read_json(child)
            value["parent_receipt_sha256"] = checker._file_sha256(str(folder / f"{ids[index - 1]}.receipt.json"))
            json_write(child, value)
        with pytest.raises(checker.EvidenceError):
            observation.capture(bundle, "exclude", "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None)
        assert len(launches) == 3
        assert not (folder / "exclude.prompt.txt").exists()
    else:
        assert observation.capture(bundle, "exclude", "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None) == 0
        assert len(launches) == 4
    assert all(command[-2] == "synthetic-root-A" for command in launches[1:])
    assert prep.state(b["matter"]) == b["initial_state"]


def test_f03_independent_roots_and_declared_branches_remain_valid(prepared, monkeypatch, tmp_path):
    bundle, b, _ = prepared
    monkeypatch.setattr(observation, "installed", lambda value: value)
    parents = [("root-a", None), ("first-a", "root-a"), ("root-b", None), ("first-b", "root-b"),
               ("branch-a", "root-a"), ("next-a", "first-a")]
    plan = dict(scenario="independent roots and branches", expected=dict(result="resume each declared original conversation"),
                steps=[dict(id=name, resume=parent, prompt="Synthetic followup: " + name) for name, parent in parents])
    path = tmp_path / "plan.json"
    json_write(path, plan)
    observation.freeze(bundle, path)
    launches = []

    def emit(command, *, input, cwd, stdout, stderr, check):
        index = len(launches)
        thread = "synthetic-root-B" if index in (2, 3) else "synthetic-root-A"
        launches.append(command)
        if parents[index][1] is not None:
            assert command[-2] == thread
        assert cwd.name == ("client-root-b" if index in (2, 3) else "client-root-a")
        stdout.write(("\n".join(json.dumps(e) for e in turn(thread)) + "\n").encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(observation.subprocess, "run", emit)
    for name, _ in parents:
        assert observation.capture(bundle, name, "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None) == 0
    assert len(launches) == len(parents)
    assert prep.state(b["matter"]) == b["initial_state"]


def move_document_start(events, boundary):
    store = next(i for i, e in enumerate(events) if e["type"] == boundary
                 and e.get("item", {}).get("tool") == "read_deal_positions")
    doc = next(i for i, e in enumerate(events) if e["type"] == "item.started"
               and e.get("item", {}).get("tool") == "inspect_document")
    events.insert(store, events.pop(doc))


@pytest.mark.parametrize("boundary", ["item.started", "item.completed"])
def test_f02_initial_recovery_must_complete_before_document_dispatch(evidence, boundary):
    bundle, _, _ = evidence
    mutate_events(bundle, "b-brief", lambda events: move_document_start(events, boundary))
    with pytest.raises(checker.EvidenceError):
        checker.check_bundle(bundle)


@pytest.mark.parametrize("fault", ["missing_thread", "missing_start", "missing_finish", "missing_message",
    "unfinished_message", "empty_message", "failed_turn", "pending_tool", "unpaired_tool", "mismatched_pair",
    "message_before_tool_completion", "late_event", "shell_action"])
def test_f01_observation_relaxation_keeps_parent_protocol_strict(prepared, fault):
    _, b, report = prepared
    events = turn("synthetic-parent")
    if fault == "missing_thread":
        events.pop(0)
    elif fault == "missing_start":
        events.pop(1)
    elif fault == "missing_finish":
        events.pop()
    elif fault == "missing_message":
        events.pop(2)
    elif fault == "unfinished_message":
        events[2]["type"] = "item.started"
    elif fault == "empty_message":
        events[2]["item"]["text"] = " "
    elif fault == "failed_turn":
        events[-1]["type"] = "turn.failed"
    elif fault == "late_event":
        events.append(deepcopy(events[2]))
    elif fault == "shell_action":
        events[2]["item"]["type"] = "command_execution"
    else:
        payload = server.read_deal_positions(folder=b["matter"], check_sources=True)
        events = turn("synthetic-parent", payload=payload, folder=b["matter"])
        if fault == "pending_tool":
            events.pop(3)
        elif fault == "unpaired_tool":
            events.pop(2)
        elif fault == "mismatched_pair":
            events[3]["item"]["arguments"] = dict(folder=b["matter"], check_sources=False)
        else:
            events.insert(3, events.pop(4))
    with pytest.raises(checker.EvidenceError):
        checker.parse_native(events, report["producer"], require_tool_calls=False)


def test_f01_malformed_refusal_parent_cannot_launch_exclusion(prepared, monkeypatch, tmp_path):
    bundle, b, report = prepared
    monkeypatch.setattr(observation, "installed", lambda value: value)
    plan = dict(scenario="unfinished refusal parent", expected=dict(result="no continuation launch"),
        steps=[dict(id="mandatory", resume=None, prompt="Mandatory header change; no partial output."),
               dict(id="exclude", resume="mandatory", prompt="Exclude header and proceed with the agreed batch.")])
    path = tmp_path / "plan.json"
    json_write(path, plan)
    observation.freeze(bundle, path)
    launches = []

    def emit(command, *, input, cwd, stdout, stderr, check):
        launches.append(command)
        # A zero exit code is not evidence that the parent turn completed.
        events = turn("synthetic-unfinished")[:-1]
        stdout.write(("\n".join(json.dumps(e) for e in events) + "\n").encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(observation.subprocess, "run", emit)
    assert observation.capture(bundle, "mandatory", "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None) == 0
    with pytest.raises(checker.EvidenceError):
        observation.capture(bundle, "exclude", "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None)
    assert len(launches) == 1
    assert not (bundle / "observations/exclude.prompt.txt").exists()
    assert prep.state(b["matter"]) == b["initial_state"]


def test_f01_f02_f03_combined_chain_parser_and_order_guarantees_with_controls(evidence, monkeypatch, tmp_path):
    bundle, b, report = evidence
    assert checker.check_bundle(bundle)["status"] == "mechanical_evidence_passed"
    originals = {path: path.read_bytes() for path in bundle.iterdir() if path.is_file()}

    def restore():
        for path, raw in originals.items():
            path.write_bytes(raw)

    def no_tools(events):
        events[:] = [e for e in events if e.get("item", {}).get("type") != "mcp_tool_call"]

    def failed_early_document(events):
        existing = next(e for e in events if e["type"] == "item.started"
                        and e.get("item", {}).get("tool") == "inspect_document")
        start = deepcopy(existing)
        start["item"]["id"] = "early-failed-document"
        end = dict(type="item.completed", item=dict(deepcopy(start["item"]), status="failed",
                   error="synthetic recoverable document read error", result=None))
        # The failed attempt starts before recovery. A later complete read/quote
        # chain stays intact, so failure recovery cannot hide the early dispatch.
        events.insert(2, start)
        completed_store = next(i for i, e in enumerate(events) if e["type"] == "item.completed"
                               and e.get("item", {}).get("tool") == "read_deal_positions")
        events.insert(completed_store + 1, end)

    cases = [no_tools, lambda e: move_document_start(e, "item.started"),
             lambda e: move_document_start(e, "item.completed"), failed_early_document]
    monkeypatch.setattr(observation, "installed", lambda value: value)
    plan = dict(scenario="combined foreign conversation, no-tool and early-document variants",
        expected=dict(result="refuse foreign continuation and main evidence with missing or late recovery"),
        steps=[dict(id=f"{stage}-{i}", resume=f"{parent}-{i}" if parent else None,
                    prompt="Synthetic combined check: " + stage)
               for i in range(len(cases)) for stage, parent in (("brief", None), ("parent", "brief"), ("exclude", "parent"))])
    plan_path = tmp_path / "combined-plan.json"
    json_write(plan_path, plan)
    observation.freeze(bundle, plan_path)
    valid_events = [json.loads(line) for line in (bundle / "b-brief.jsonl").read_text().splitlines()]
    matter_before = prep.state(b["matter"])
    for index, mutation in enumerate(cases):
        restore()
        mutate_events(bundle, "b-brief", mutation)
        raw = [json.loads(line) for line in (bundle / "b-brief.jsonl").read_text().splitlines()]
        _, calls, _ = checker.parse_native(raw, report["producer"], require_tool_calls=False)
        if mutation is no_tools:
            assert calls == []  # Valid conversation; not main success evidence.
        else:
            assert calls[0]["tool"] == "read_deal_positions"  # Completion order still looks plausible.
        with pytest.raises(checker.EvidenceError):
            checker.check_bundle(bundle)
        foreign_events = deepcopy(raw)
        foreign_events[0]["thread_id"] = "synthetic-foreign-B"
        launches = []

        def emit(command, *, input, cwd, stdout, stderr, check):
            events = foreign_events if launches else valid_events
            launches.append(command)
            stdout.write(("\n".join(json.dumps(e) for e in events) + "\n").encode())
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(observation.subprocess, "run", emit)
        for stage in ("brief", "parent"):
            assert observation.capture(bundle, f"{stage}-{index}", "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None) == 0
        assert launches[1][-2] == valid_events[0]["thread_id"]
        with pytest.raises(checker.EvidenceError, match="observation.*thread"):
            observation.capture(bundle, f"exclude-{index}", "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None)
        assert len(launches) == 2
        assert not (bundle / f"observations/exclude-{index}.prompt.txt").exists()
        assert prep.state(b["matter"]) == matter_before

    restore()
    # Independent document reads may overlap once position recovery is complete.
    # Do not turn the causal prerequisite into a blanket serialization rule.
    def parallel_documents_after_recovery(events):
        current_start = next(i for i, e in enumerate(events) if e["type"] == "item.started"
                             and e.get("item", {}).get("tool") == "inspect_document")
        previous_start = next(i for i, e in enumerate(events) if e["type"] == "item.started"
                              and e.get("item", {}).get("tool") == "inspect_document"
                              and e["item"]["arguments"].get("path") == b["inputs"]["b"]["previous"])
        events.insert(current_start + 1, events.pop(previous_start))

    mutate_events(bundle, "b-brief", parallel_documents_after_recovery)
    assert checker.check_bundle(bundle)["status"] == "mechanical_evidence_passed"
    restore()
    assert checker.check_bundle(bundle)["status"] == "mechanical_evidence_passed"
