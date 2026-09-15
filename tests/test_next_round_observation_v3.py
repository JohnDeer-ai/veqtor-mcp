# SPDX-License-Identifier: Apache-2.0
"""Actual local synthetic tools + fabricated receipts; not a native campaign."""
import json

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
