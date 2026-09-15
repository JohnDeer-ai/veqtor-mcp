# SPDX-License-Identifier: Apache-2.0
"""Actual local synthetic tools + fabricated receipts; not a native campaign."""
import json
from copy import deepcopy

import pytest

import test_next_round_acceptance as base
from check_next_round_observation import check_independent_write
import capture_next_round_observation as observation


def make_observation(tmp_path, monkeypatch, variant):
    bundle, b, installation = base.prepare_fixture(tmp_path, monkeypatch, variant)
    plan = dict(scenario=variant, expected=dict(result="supported document component", document_policy=("expected_unavailable_journal"
        if variant == "journal-unavailable" else "positive_complete_journal")), steps=[
        dict(id="brief", resume=None, prompt=base.capture.stimulus(bundle, "a-brief", b)),
        dict(id="decision", resume="brief", prompt=base.capture.stimulus(bundle, "a-write", b))])
    # Unsupported case receives ordinary explicit exclusion, never a prescribed call.
    if variant == "unsupported":
        plan["steps"][1]["prompt"] = 'Exclude the unsupported header request; proceed with the approved supported changes.'
    path = tmp_path / "plan.json"
    base.json_write(path, plan)
    observation.freeze(bundle, path)
    folder = bundle / "observations"
    if variant == "journal-unavailable":
        monkeypatch.setenv("VEQTOR_DISABLE_DECISION_RECORD", "1")
    parent = None
    for stage, step in (("a-brief", "brief"), ("a-write", "decision")):
        base.native_stage(bundle, b, installation, stage, monkeypatch, allow_unavailable_journal=variant == "journal-unavailable")
        old = base.prep.read_json(bundle / f"{stage}.receipt.json")
        thread = "synthetic-client-a"
        resumed = None if step == "brief" else thread
        (folder / f"{step}.jsonl").write_bytes((bundle / f"{stage}.jsonl").read_bytes())
        prompt = plan["steps"][int(step == "decision")]["prompt"]
        if step == "brief":
            prompt = observation.delivered_prompt(bundle, prompt, base.WORKFLOW_FILES)
        (folder / f"{step}.prompt.txt").write_text(prompt)
        receipt = dict(schema_version="veqtor_next_round_observation_capture.v1", step=step,
            command=observation.step_command("/synthetic/codex", installation, b, plan, step, resumed),
            cwd=str(folder / "client-brief"), resumed_thread_id=resumed, parent_receipt_sha256=parent,
            plan_sha256=base.checker._file_sha256(str(folder / "plan.json")),
            prompt_sha256=base.checker._file_sha256(str(folder / f"{step}.prompt.txt")),
            events_sha256=base.checker._file_sha256(str(folder / f"{step}.jsonl")),
            installation_sha256=b["installation_sha256"], started_ns=old["started_ns"], finished_ns=old["finished_ns"],
            before=old["before"], after=old["after"], exit_code=0, fault=None, acceptance_assessed=False)
        base.json_write(folder / f"{step}.receipt.json", receipt)
        parent = base.checker._file_sha256(str(folder / f"{step}.receipt.json"))
        base.synthetic_delivery(folder, step)
    return bundle, b, installation


@pytest.mark.parametrize("variant", ["journal-unavailable", "document-injection", "position-injection", "unsupported"])
def test_predeclared_independent_case_dispatch_is_component_only(tmp_path, monkeypatch, variant):
    bundle, _, _ = make_observation(tmp_path, monkeypatch, variant)
    result = check_independent_write(bundle, "decision")
    assert result["status"] == "PASSED_COMPONENT_ONLY"
    assert len(result["brief_coverage"]["obligations"]) == 14
    assert result["native_acceptance_claimed"] is False and result["open_gates"]
    if variant == "journal-unavailable":
        assert result["component"]["journal"]["status"] == "EXPECTED_UNAVAILABLE"
        assert result["component"]["journal"]["positive_journal_pass"] is False
    else:
        assert result["component"]["journal"]["complete_pages_verified"]


def test_bound_adverse_dispatch_and_ancestry_causal_negatives(tmp_path, monkeypatch, record_property):
    bundle, _, _ = make_observation(tmp_path, monkeypatch, "journal-unavailable")
    folder = bundle / "observations"
    assert check_independent_write(bundle, "decision")["status"] == "PASSED_COMPONENT_ONLY"
    paths = [p for p in folder.iterdir() if p.is_file()]
    saved = {p: p.read_bytes() for p in paths}
    causes = []
    cases = {
        "undeclared_policy": "missing or different predeclared document policy",
        "wrong_policy": "missing or different predeclared document policy",
        "wrong_launch": "ancestor launch/fault differs",
        "wrong_parent": "parent receipt chain differs",
        "wrong_workspace": "parent working directory differs",
        "wrong_before": "original complete before-state differs",
        "wrong_after": "write changed source/store or unexpected output inventory",
        "wrong_phase": "observation phase order differs",
        "clipped_delivery": "required model-facing evidence is clipped",
    }
    for fault, cause in cases.items():
        try:
            receipt_path = folder / "decision.receipt.json"
            receipt = base.prep.read_json(receipt_path)
            if "policy" in fault:
                plan = base.prep.read_json(folder / "plan.json")
                if fault == "undeclared_policy":
                    del plan["plan"]["expected"]["document_policy"]
                else:
                    plan["plan"]["expected"]["document_policy"] = "positive_complete_journal"
                base.json_write(folder / "plan.json", plan)
            elif fault == "wrong_launch":
                receipt["command"] = [s.replace('VEQTOR_DISABLE_DECISION_RECORD="1"', 'VEQTOR_DISABLE_DECISION_RECORD="0"') for s in receipt["command"]]
            elif fault == "wrong_parent":
                receipt["parent_receipt_sha256"] = "0"*64
            elif fault == "wrong_workspace":
                receipt["cwd"] = "/other"
            elif fault == "wrong_before":
                receipt["before"]["store_sha256"] = "0"*64
            elif fault == "wrong_after":
                receipt["after"]["store_sha256"] = "0"*64
            elif fault == "wrong_phase":
                receipt["started_ns"] = 1
            base.json_write(receipt_path, receipt)
            if "policy" in fault:
                # Preserve all enclosing hashes; policy itself must refuse.
                first = base.prep.read_json(folder / "brief.receipt.json")
                first["plan_sha256"] = base.checker._file_sha256(str(folder / "plan.json"))
                base.json_write(folder / "brief.receipt.json", first)
                receipt["plan_sha256"] = first["plan_sha256"]
                receipt["parent_receipt_sha256"] = base.checker._file_sha256(str(folder / "brief.receipt.json"))
                base.json_write(receipt_path, receipt)
                base.synthetic_delivery(folder, "brief")
            base.synthetic_delivery(folder, "decision")
            if fault == "clipped_delivery":
                binding_path = folder / "decision.delivery.json"
                binding = base.prep.read_json(binding_path)
                session_path = folder / "decision.synthetic-client.jsonl"
                session = [json.loads(line) for line in session_path.read_text().splitlines()]
                row = next(r["payload"] for r in session if r.get("payload", {}).get("type") == "function_call_output")
                row["output"] = '{"complete":true,"metadata_only":true}'
                session_path.write_text("\n".join(json.dumps(r) for r in session)+"\n")
                binding["session_sha256"] = base.checker._file_sha256(str(session_path))
                base.json_write(binding_path, binding)
            with pytest.raises(base.checker.EvidenceError, match=cause) as caught:
                check_independent_write(bundle, "decision")
            causes.append(dict(fault=fault, cause=str(caught.value)))
        finally:
            for path, data in saved.items():
                path.write_bytes(data)
        assert check_independent_write(bundle, "decision")["status"] == "PASSED_COMPONENT_ONLY"
    record_property("causal_adverse_dispatch", json.dumps(causes))


def insert_journal_only(bundle, b, installation):
    """Predeclared synthetic middle turn; preserve complete original brief/write."""
    folder = bundle / "observations"
    frozen = base.prep.read_json(folder / "plan.json")
    plan = frozen["plan"]
    plan["steps"].insert(1, dict(id="journal-only", resume="brief", prompt="Show the available action record before proceeding."))
    plan["steps"][-1]["resume"] = "journal-only"
    base.json_write(folder / "plan.json", frozen)
    first = base.prep.read_json(folder / "brief.receipt.json")
    last = base.prep.read_json(folder / "decision.receipt.json")
    first["plan_sha256"] = base.checker._file_sha256(str(folder / "plan.json"))
    base.json_write(folder / "brief.receipt.json", first)
    base.synthetic_delivery(folder, "brief")
    thread, ident = "synthetic-client-a", "journal-only"
    original = [json.loads(line) for line in (folder / "decision.jsonl").read_text().splitlines()]
    pair = deepcopy([e for e in original if e.get("item", {}).get("tool") == "export_decision_record"])
    assert len(pair) == 2
    for e in pair:
        e["item"]["id"] = "journal-only-attempt"
    events = [dict(type="thread.started", thread_id=thread), dict(type="turn.started"), *pair,
              dict(type="item.completed", item=dict(id="message", type="agent_message", text="The journal is unavailable.")),
              dict(type="turn.completed")]
    (folder / f"{ident}.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (folder / f"{ident}.prompt.txt").write_text(plan["steps"][1]["prompt"])
    middle = dict(first, step=ident, command=observation.step_command("/synthetic/codex", installation, b, plan, ident, thread),
        resumed_thread_id=thread, parent_receipt_sha256=base.checker._file_sha256(str(folder / "brief.receipt.json")),
        prompt_sha256=base.checker._file_sha256(str(folder / f"{ident}.prompt.txt")),
        events_sha256=base.checker._file_sha256(str(folder / f"{ident}.jsonl")),
        started_ns=first["finished_ns"] + 1, finished_ns=first["finished_ns"] + 2,
        before=b["initial_state"], after=b["initial_state"])
    base.json_write(folder / f"{ident}.receipt.json", middle)
    base.synthetic_delivery(folder, ident)
    last["plan_sha256"] = first["plan_sha256"]
    last["parent_receipt_sha256"] = base.checker._file_sha256(str(folder / f"{ident}.receipt.json"))
    assert middle["finished_ns"] < last["started_ns"]
    base.json_write(folder / "decision.receipt.json", last)
    base.synthetic_delivery(folder, "decision")
    observation.resume_parent(folder, plan, frozen, "decision", b, installation)


def test_full_chain_journal_only_intermediate_preserves_final_verification(tmp_path, monkeypatch, record_property):
    bundle, b, installation = make_observation(tmp_path, monkeypatch, "journal-unavailable")
    assert check_independent_write(bundle, "decision")["status"] == "PASSED_COMPONENT_ONLY"
    insert_journal_only(bundle, b, installation)
    result = check_independent_write(bundle, "decision")
    assert result["status"] == "PASSED_COMPONENT_ONLY"
    assert len(result["brief_coverage"]["obligations"]) == 14
    assert result["component"]["journal"]["status"] == "EXPECTED_UNAVAILABLE"
    record_property("F11_full_chain", json.dumps(dict(status=result["status"],
        ancestor_failure_ledger=result["ancestor_failure_ledger"], journal=result["component"]["journal"])))
    folder = bundle / "observations"
    saved = {p: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    causes = []
    for fault in ["missing_final", "early_final", "middle_workspace", "middle_error", "middle_limit"]:
        try:
            stage = "decision" if "final" in fault else "journal-only"
            path = folder / f"{stage}.jsonl"
            events = [json.loads(line) for line in path.read_text().splitlines()]
            pair = [e for e in events if e.get("item", {}).get("tool") == "export_decision_record"]
            if "final" in fault:
                events = [e for e in events if e not in pair]
                if fault == "early_final":
                    events[2:2] = pair
            else:
                for e in pair:
                    item = e["item"]
                    if fault == "middle_workspace":
                        item["arguments"]["workspace"] = "/unselected"
                    elif fault == "middle_limit":
                        item["arguments"]["max_records"] = 21
                    elif item.get("result"):
                        item["result"]["content"][0]["text"] = "Error executing tool export_decision_record: unrelated_failure: operation refused"
            path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
            receipt_path = folder / f"{stage}.receipt.json"
            receipt = base.prep.read_json(receipt_path)
            receipt["events_sha256"] = base.checker._file_sha256(str(path))
            base.json_write(receipt_path, receipt)
            base.synthetic_delivery(folder, stage)
            if stage == "journal-only":
                last = base.prep.read_json(folder / "decision.receipt.json")
                last["parent_receipt_sha256"] = base.checker._file_sha256(str(receipt_path))
                base.json_write(folder / "decision.receipt.json", last)
                base.synthetic_delivery(folder, "decision")
            expected = ("actual expected export failure" if "final" in fault else
                        "native action targets another matter" if fault == "middle_workspace" else
                        "native export violates the explicit 20-record page bound" if fault == "middle_limit" else "unavailable journal error or workspace differs")
            with pytest.raises(base.checker.EvidenceError, match=expected) as caught:
                check_independent_write(bundle, "decision")
            causes.append(dict(fault=fault, cause=str(caught.value)))
        finally:
            for path, data in saved.items():
                path.write_bytes(data)
        assert check_independent_write(bundle, "decision")["status"] == "PASSED_COMPONENT_ONLY"
    record_property("F11_causal", json.dumps(causes))


def exec_delivery(folder, stage):
    """Fabricated exec/runtime/output representation of this synthetic fixture."""
    path = folder / f"{stage}.synthetic-client.jsonl"
    old = [json.loads(line) for line in path.read_text().splitlines()]
    session = old[:2]
    session[1]["payload"]["turn_id"] = "synthetic-turn"
    thread = session[0]["payload"]["id"]
    for i in range(2, len(old)-2, 2):
        action, output = old[i]["payload"], old[i+1]["payload"]
        args, payload = json.loads(action["arguments"]), json.loads(output["output"])
        content = [dict(type="text", text=json.dumps(payload))]
        outer_id = "outer-" + action["call_id"]
        session += [dict(type="response_item", payload=dict(type="custom_tool_call", name="exec", call_id=outer_id,
                        input=f"const r = await tools.mcp__{base.SERVER}__{action['name']}({json.dumps(args)}); text(r);")),
                    dict(type="event_msg", payload=dict(type="item_completed", thread_id=thread, turn_id="synthetic-turn",
                        item=dict(type="McpToolCall", id=action["call_id"], server=base.SERVER, tool=action["name"], arguments=args,
                            status="completed" if payload else "failed", result=dict(content=content, structuredContent=payload)))),
                    dict(type="response_item", payload=dict(type="custom_tool_call_output", call_id=outer_id,
                        output=[dict(type="input_text", text=json.dumps(dict(status="fulfilled", value=content)))]))]
    session += old[-2:]
    path.write_text("\n".join(json.dumps(e) for e in session) + "\n")
    binding_path = folder / f"{stage}.delivery.json"
    binding = base.prep.read_json(binding_path)
    binding["session_sha256"] = base.checker._file_sha256(str(path))
    base.json_write(binding_path, binding)
    return session


@pytest.mark.parametrize("variant", ["document-injection", "journal-unavailable"])
def test_full_wrappers_refuse_false_attribution_and_accept_exec_shape(tmp_path, monkeypatch, variant, record_property):
    bundle, _, _ = make_observation(tmp_path, monkeypatch, variant)
    folder = bundle / "observations"
    original = check_independent_write(bundle, "decision")
    assert original["status"] == "PASSED_COMPONENT_ONLY"
    saved = {p: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    path = folder / "decision.synthetic-client.jsonl"
    binding_path = folder / "decision.delivery.json"
    causes = []
    for surface in ["direct", "exec"]:
        for fault in [None, "operation", "arguments"]:
            for p, data in saved.items():
                p.write_bytes(data)
            session = exec_delivery(folder, "decision") if surface == "exec" else [json.loads(line) for line in path.read_text().splitlines()]
            if fault:
                action = next(e["payload"]["item"] for e in session if e.get("payload", {}).get("item", {}).get("tool") == "preflight_edits") if surface == "exec" else next(
                    e["payload"] for e in session if e.get("payload", {}).get("type") == "function_call" and e["payload"].get("name") == "preflight_edits")
                if fault == "operation":
                    action["tool" if surface == "exec" else "name"] = "inspect_document"
                else:
                    args = action["arguments"] if surface == "exec" else json.loads(action["arguments"])
                    args["source_path"] = "/unselected/another.docx"
                    action["arguments"] = args if surface == "exec" else json.dumps(args)
            path.write_text("\n".join(json.dumps(e) for e in session) + "\n")
            binding = base.prep.read_json(binding_path)
            binding["session_sha256"] = base.checker._file_sha256(str(path))
            base.json_write(binding_path, binding)
            if fault:
                with pytest.raises(base.checker.EvidenceError, match="required model-facing evidence") as caught:
                    check_independent_write(bundle, "decision")
                causes.append(dict(surface=surface, fault=fault, cause=str(caught.value)))
            else:
                assert check_independent_write(bundle, "decision")["status"] == "PASSED_COMPONENT_ONLY"
        for p, data in saved.items():
            p.write_bytes(data)
        assert check_independent_write(bundle, "decision")["status"] == "PASSED_COMPONENT_ONLY"
    record_property("F12_full_wrapper", json.dumps(dict(variant=variant, causes=causes, restored="PASSED_COMPONENT_ONLY")))
