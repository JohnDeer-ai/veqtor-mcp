# SPDX-License-Identifier: Apache-2.0
"""F12/F15 causal controls through actual v2 entrypoints; all inputs synthetic."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

import test_next_round_acceptance as base
import test_next_round_creation_probe as creation
import test_next_round_observation_v3 as observation
import test_next_round_source_profile as source
from capture_nr03_app_server import bind_delivery
from check_next_round_observation import check_independent_write
from nr03_app_server import core_to_app, lines
from nr03_model_delivery import load_model_delivery, validate_model_delivery
from nr03_source_fixtures import attach, config_for, encode, materialize_v2

files = source.files
source_case = source.source_case
prepared = base.prepared
probe = creation.probe


def snapshot(folder):
    return {p: p.read_bytes() for p in folder.iterdir() if p.is_file()}


def restore(saved):
    for path, data in saved.items():
        path.write_bytes(data)


def bind_prefix(folder, stage, raw):
    (folder / f"{stage}.session.jsonl").write_bytes(raw)
    receipt = json.loads((folder / f"{stage}.receipt.json").read_text())
    receipt["source"]["session_sha256"] = base.checker._file_sha256(str(folder / f"{stage}.session.jsonl"))
    (folder / f"{stage}.receipt.json").unlink()
    (folder / f"{stage}.delivery.json").unlink()
    bind_delivery(folder, stage, receipt)


def assert_failure_originals(ledger, folder, stage):
    session = lines((folder / f"{stage}.session.jsonl").read_bytes())
    raw = lines((folder / f"{stage}.jsonl").read_bytes())
    assert ledger
    for entry in ledger:
        original = entry["original_failure"]["source_failure"]
        assert original["profile"] == "nr03-app-server-original.v1"
        assert original["original_core_event"] in session
        assert original["original_app_server_notification"] in raw
        core = original["original_core_event"]["payload"]["item"]
        app = original["original_app_server_notification"]["params"]["item"]
        assert core["result"]["isError"] is True and core["status"] == app["status"] == "failed"
        assert core["result"]["content"] == app["result"]["content"]
        assert entry["original_failure"]["error"] is None


def change_v2_failure(folder, stage, fault):
    """Mutate a synthetic original, retaining every unaffected row and binding."""
    f = dict(receipt=json.loads((folder / f"{stage}.receipt.json").read_text()))
    for key, suffix in (("raw", "jsonl"), ("requests", "requests.jsonl"), ("transport", "transport.jsonl"), ("session", "session.jsonl")):
        f[key] = (folder / f"{stage}.{suffix}").read_text()
    session, raw = lines(f["session"].encode()), lines(f["raw"].encode())
    turn = f["receipt"]["source"]["turn_id"]
    core = next(r["payload"]["item"] for r in session if r.get("payload", {}).get("turn_id") == turn
                and r["payload"].get("item", {}).get("status") == "failed")
    if fault == "wrong_error":
        core["result"]["content"][0]["text"] = "Error executing tool inspect_document: unrelated_error: operation refused"
        app = next(r["params"]["item"] for r in raw if r.get("method") == "item/completed"
                   and r["params"]["item"]["id"] == core["id"])
        app["result"]["content"] = deepcopy(core["result"]["content"])
    elif fault == "missing_flag":
        del core["result"]["isError"]
    elif fault == "false_flag":
        core["result"]["isError"] = False
    else:
        assert fault == "completed_failure"
        core["status"] = "completed"
    f.update(raw=encode(raw), session=encode(session))
    source.rebind(f)
    f["receipt"]["events_sha256"] = f["receipt"]["source"]["events_sha256"]
    for key, suffix in (("raw", "jsonl"), ("transport", "transport.jsonl"), ("session", "session.jsonl")):
        (folder / f"{stage}.{suffix}").write_text(f[key])
    (folder / f"{stage}.receipt.json").unlink()
    (folder / f"{stage}.delivery.json").unlink()
    bind_delivery(folder, stage, f["receipt"])


@pytest.mark.parametrize("variant", ["journal-unavailable", "document-injection", "position-injection", "unsupported"])
def test_v2_all_independent_consumers_and_ancestry(tmp_path, monkeypatch, variant):
    bundle, _, _ = observation.make_observation(tmp_path, monkeypatch, variant)
    folder = bundle / "observations"
    materialize_v2(folder, ("brief", "decision"), observation=True)
    saved = snapshot(folder)
    positive = check_independent_write(bundle, "decision")
    assert positive["status"] == "PASSED_COMPONENT_ONLY"
    assert len(positive["brief_coverage"]["obligations"]) == 14
    if variant == "journal-unavailable":
        assert_failure_originals(positive["component"]["failure_ledger"], folder, "decision")
    rows = lines(saved[folder / "decision.session.jsonl"])
    del rows[next(n for n, row in enumerate(rows) if row.get("payload", {}).get("role") == "assistant")]
    bind_prefix(folder, "decision", encode(rows).encode())
    with pytest.raises(base.checker.EvidenceError, match="source observation parent prefix differs"):
        check_independent_write(bundle, "decision")
    restore(saved)
    assert check_independent_write(bundle, "decision")["status"] == positive["status"]
    assert snapshot(folder) == saved


@pytest.mark.parametrize("form", ["mcp", "array", "fulfilled", "labelled"])
@pytest.mark.parametrize("field", ["_meta", "annotations", "unknown"])
def test_terminal_block_field_fault_is_local(source_case, form, field):
    fixture, producer = source_case
    parsed = source.assess(source_case, deliver=False)
    call = parsed["calls"][3]
    good = deepcopy(call["core_result"])
    def assess(terminal):
        f = deepcopy(fixture)
        rows = lines(f["session"].encode())
        output = next(r["payload"] for r in rows if r.get("payload", {}).get("type") == "function_call_output"
                      and r["payload"]["call_id"] == call["id"])
        if form == "array":
            terminal = terminal["content"]
        elif form == "fulfilled":
            terminal = dict(status="fulfilled", value=terminal)
        elif form == "labelled":
            terminal = dict(file=call["arguments"]["path"], index=9, result=terminal)
        output["output"] = json.dumps(terminal)
        f["session"] = encode(rows)
        source.rebind(f)
        return source.assess((f, producer))
    positive = assess(good)
    assert len(positive) == 5
    bad = deepcopy(good)
    bad["content"][0][field] = {"source_document": "/contradictory/other.docx"}
    negative = assess(bad)
    assert set(negative) == set(positive) - {call["id"]}
    assert set(assess(good)) == set(positive)


@pytest.mark.parametrize("scope", ["block", "envelope"])
def test_original_metadata_is_preserved_with_typed_values(source_case, scope):
    fixture, producer = deepcopy(source_case)
    rows, raw = lines(fixture["session"].encode()), lines(fixture["raw"].encode())
    core = [r["payload"]["item"] for r in rows if r.get("payload", {}).get("item", {}).get("type") == "McpToolCall"][3]
    original = core["result"] if scope == "envelope" else core["result"]["content"][0]
    original["_meta"] = {"version": 1}
    app = next(r["params"]["item"] for r in raw if r.get("method") == "item/completed" and r["params"]["item"]["id"] == core["id"])
    app["result"] = core_to_app(core)["result"]
    output = next(r["payload"] for r in rows if r.get("payload", {}).get("type") == "function_call_output" and r["payload"]["call_id"] == core["id"])
    def assess(terminal):
        output["output"] = json.dumps(terminal, indent=2)
        fixture.update(raw=encode(raw), session=encode(rows))
        source.rebind(fixture)
        return source.assess((fixture, producer))
    good = deepcopy(core["result"])
    assert len(assess(good)) == 5
    for fault in ("typed_value", "missing"):
        bad = deepcopy(good)
        target = bad if scope == "envelope" else bad["content"][0]
        if fault == "missing":
            del target["_meta"]
        else:
            target["_meta"]["version"] = True
        negative = assess(bad)
        assert len(negative) == 4 and core["id"] not in negative
        assert len(assess(good)) == 5


def test_v2_full_component_block_metadata_refuses_and_restores(tmp_path, monkeypatch):
    bundle, b, installation = observation.make_observation(tmp_path, monkeypatch, "document-injection")
    folder = bundle / "observations"
    materialize_v2(folder, ("brief", "decision"), observation=True)
    saved = snapshot(folder)
    receipt = json.loads((folder / "decision.receipt.json").read_text())
    from nr03_app_server import parse_capture
    parsed = dict(receipt=receipt, **parse_capture(folder, "decision", receipt, installation["producer"]))
    call = next(c for c in parsed["calls"] if c["tool"] == "inspect_document" and c["arguments"].get("mode") == "read")
    parent = saved[folder / "brief.session.jsonl"]
    assert saved[folder / "decision.session.jsonl"].startswith(parent)
    rows = lines(saved[folder / "decision.session.jsonl"][len(parent):])
    output = next(r["payload"] for r in rows if r.get("payload", {}).get("type") == "function_call_output"
                  and r["payload"]["call_id"] == call["id"])
    terminal = deepcopy(call["core_result"])
    output["output"] = json.dumps(dict(file=call["arguments"]["path"], result=terminal))
    exact = parent + encode(rows).encode()
    bind_prefix(folder, "decision", exact)
    positive = check_independent_write(bundle, "decision")
    assert positive["status"] == "PASSED_COMPONENT_ONLY" and len(positive["brief_coverage"]["obligations"]) == 14
    receipt = json.loads((folder / "decision.receipt.json").read_text())
    parsed = dict(receipt=receipt, **parse_capture(folder, "decision", receipt, installation["producer"]))
    good_delivery = load_model_delivery(folder, "decision", parsed)["calls"]
    terminal["content"][0]["_meta"] = {"source_document": "/contradictory/other.docx"}
    output["output"] = json.dumps(dict(file=call["arguments"]["path"], result=terminal))
    bad = parent + encode(rows).encode()
    bind_prefix(folder, "decision", bad)
    parsed["source"]["session_sha256"] = base.checker._file_sha256(str(folder / "decision.session.jsonl"))
    local = validate_model_delivery(parsed["calls"], lines(bad), thread=parsed["thread"], cwd=parsed["receipt"]["cwd"], final_text=parsed["messages"][-1]["text"])
    assert set(local) == set(good_delivery) - {call["id"]}
    with pytest.raises(base.checker.EvidenceError, match="required model-facing evidence not established"):
        check_independent_write(bundle, "decision")
    assert (folder / "decision.jsonl").read_bytes() == saved[folder / "decision.jsonl"]
    bind_prefix(folder, "decision", exact)
    assert check_independent_write(bundle, "decision")["status"] == "PASSED_COMPONENT_ONLY"
    restore(saved)
    assert check_independent_write(bundle, "decision")["status"] == "PASSED_COMPONENT_ONLY"


def test_v2_initial_navigation_retains_original_failure(probe):
    bundle, b, installation, _ = probe
    for stage in ("a-brief", "a-write"):
        attach(bundle, stage, config=config_for(installation, b))
    materialize_v2(bundle, ("a-brief", "a-write"))
    saved = snapshot(bundle)
    result = base.checker.check_round(bundle, "a")["mechanical"]
    assert result["exact_revisions_verified"] and result["journal"]["complete_pages_verified"]
    assert_failure_originals(result["creation_probe"]["probes"], bundle, "a-write")
    assert snapshot(bundle) == saved
    for fault in ("wrong_error", "false_flag", "missing_flag", "completed_failure"):
        change_v2_failure(bundle, "a-write", fault)
        expected = "not eligible initial output navigation" if fault == "wrong_error" else "core failure/status contradiction"
        with pytest.raises(base.checker.EvidenceError, match=expected):
            base.checker.check_round(bundle, "a")
        restore(saved)
        assert base.checker.check_round(bundle, "a")["mechanical"]["creation_probe"]
    assert snapshot(bundle) == saved


def test_v2_unavailable_ancestors_and_final_failure_controls(tmp_path, monkeypatch, record_property):
    bundle, b, installation = observation.make_observation(tmp_path, monkeypatch, "journal-unavailable")
    observation.insert_journal_only(bundle, b, installation)
    folder = bundle / "observations"
    stages = ("brief", "journal-only", "decision")
    legacy = snapshot(folder)
    matter = {p: p.read_bytes() for p in Path(b["matter"]).rglob("*") if p.is_file()}
    materialize_v2(folder, stages, observation=True)
    saved = snapshot(folder)
    positive = check_independent_write(bundle, "decision")
    assert positive["status"] == "PASSED_COMPONENT_ONLY" and len(positive["brief_coverage"]["obligations"]) == 14
    assert positive["component"]["journal"]["status"] == "EXPECTED_UNAVAILABLE"
    assert_failure_originals(positive["ancestor_failure_ledger"]["journal-only"], folder, "journal-only")
    assert_failure_originals(positive["component"]["failure_ledger"], folder, "decision")
    causes = []
    for fault in ("missing_final", "early_final", "middle_workspace", "middle_error", "middle_limit"):
        restore(legacy)
        stage = "decision" if "final" in fault else "journal-only"
        path = folder / f"{stage}.jsonl"
        events = lines(path.read_bytes())
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
                    item["result"]["content"][0]["text"] = "Error executing tool export_decision_record: unrelated_error: operation refused"
        path.write_text(encode(events))
        receipt = json.loads((folder / f"{stage}.receipt.json").read_text())
        receipt["events_sha256"] = base.checker._file_sha256(str(path))
        base.json_write(folder / f"{stage}.receipt.json", receipt)
        base.synthetic_delivery(folder, stage)
        materialize_v2(folder, stages, observation=True)
        expected = ("actual expected export failure" if "final" in fault else
                    "native action targets another matter" if fault == "middle_workspace" else
                    "native export violates the explicit 20-record page bound" if fault == "middle_limit" else "unavailable journal error or workspace differs")
        with pytest.raises(base.checker.EvidenceError, match=expected) as caught:
            check_independent_write(bundle, "decision")
        causes.append(dict(fault=fault, cause=str(caught.value)))
        restore(saved)
        assert check_independent_write(bundle, "decision")["status"] == "PASSED_COMPONENT_ONLY"
    for fault in ("false_flag", "missing_flag", "completed_failure"):
        change_v2_failure(folder, "decision", fault)
        with pytest.raises(base.checker.EvidenceError, match="core failure/status contradiction") as caught:
            check_independent_write(bundle, "decision")
        causes.append(dict(fault=fault, cause=str(caught.value)))
        restore(saved)
        assert check_independent_write(bundle, "decision")["status"] == "PASSED_COMPONENT_ONLY"
    assert all(p.read_bytes() == data for p, data in matter.items())
    assert snapshot(folder) == saved
    record_property("F15_v2_causal", json.dumps(causes))
