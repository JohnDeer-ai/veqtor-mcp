# SPDX-License-Identifier: Apache-2.0
"""Causal original-source controls. All records here are explicitly synthetic."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

import test_next_round_acceptance as base
import test_next_round_coverage_v3 as coverage
from nr03_app_server import lines, parse_protocol
from nr03_model_delivery import validate_model_delivery
from nr03_source_fixtures import encode, launch_config, supplement, attach, config_for
from capture_nr03_app_server import StdioCapture

files = coverage.files
prepared = base.prepared
evidence = base.evidence


@pytest.fixture
def source_case(files):
    events = [dict(type="thread.started", thread_id="synthetic-v3-coverage"), dict(type="turn.started")]
    path = files[0][0]
    browse = coverage.add(events, "inspect_document", path=path, mode="browse", max_items=100)
    row = browse["paragraphs"][9]
    for _ in range(2):
        coverage.add(events, "inspect_document", path=path, mode="read", selection=dict(paragraph_ref=row["paragraph_ref"]))
        coverage.add(events, "verify_quote", path=path, anchor=row["paragraph_ref"], quote=row["text"], paragraph_projection="accepted_current_v1")
    events += [dict(type="item.completed", item=dict(id="final", type="agent_message", text="Synthetic brief.")), dict(type="turn.completed")]
    old = coverage.model_session(events)
    prompt = "Synthetic source profile control."
    receipt = dict(command=["/synthetic/codex"], cwd="/synthetic", resumed_thread_id=None,
                   prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(), fixture_prompt=prompt)
    fixture = supplement(events, old, receipt, launch_config("/synthetic/python", model=base.MODEL, reasoning_effort="high", journal_disabled=True))
    return fixture, browse["producer"]


def assess(case, *, deliver=True):
    f, producer = case
    parsed = parse_protocol(*(f[k].encode() for k in ("raw", "requests", "transport", "session")),
        f["receipt"]["source"], receipt=f["receipt"], producer=producer)
    if not deliver:
        return parsed
    return validate_model_delivery(parsed["calls"], lines(f["session"].encode()), thread=parsed["thread"],
        cwd=f["receipt"]["cwd"], final_text=parsed["messages"][-1]["text"])


def rebind(f, *, rebuild_transport=True):
    source = f["receipt"]["source"]
    if rebuild_transport:
        groups = {"sent": lines(f["requests"].encode()), "received": lines(f["raw"].encode())}
        old = lines(f["transport"].encode())
        # Tests change received notification inventory only after initialization;
        # requests and their response ordering remain fixed before turn events.
        first_turn = next(n for n, r in enumerate(groups["received"]) if r.get("method") == "turn/started")
        prefix = []
        counts = dict(sent=0, received=0)
        for entry in old:
            d = entry["direction"]
            if d == "received" and counts[d] >= first_turn:
                break
            counts[d] += 1
            prefix.append(entry)
        for n in range(first_turn, len(groups["received"])):
            prefix.append(dict(direction="received", line=n+1))
        for n, entry in enumerate(prefix):
            entry.update(sha256=hashlib.sha256(encode([groups[entry["direction"]][entry["line"]-1]]).encode()).hexdigest(),
                monotonic_ns=n+1, run_id=source["run_id"], connection_id=source["connection_id"])
        f["transport"] = encode(prefix)
    for k, target in (("raw", "events"), ("requests", "requests"), ("transport", "transport"), ("session", "session")):
        source[target+"_sha256"] = hashlib.sha256(f[k].encode()).hexdigest()
    source["context_sha256"] = base.checker._digest({k: v for k, v in f["receipt"].items() if k != "source"})


def test_equal_sequential_original_occurrences_are_individually_credited(source_case):
    f, _ = source_case
    parsed = assess(source_case, deliver=False)
    calls = parsed["calls"]
    assert calls[1]["arguments"] == calls[3]["arguments"] and calls[1]["payload"] == calls[3]["payload"]
    assert calls[2]["arguments"] == calls[4]["arguments"] and calls[2]["payload"] == calls[4]["payload"]
    assert len(assess(source_case)) == 5
    bad = deepcopy(f)
    session = lines(bad["session"].encode())
    output = next(r["payload"] for r in session if r.get("payload", {}).get("type") == "function_call_output"
                  and r["payload"]["call_id"] == calls[3]["id"])
    output["output"] = '{"producer":'
    bad["session"] = encode(session)
    rebind(bad)
    delivered = assess((bad, source_case[1]))
    assert calls[1]["id"] in delivered and calls[3]["id"] not in delivered and len(delivered) == 4
    assert len(assess(source_case)) == 5


@pytest.mark.parametrize("fault", ["profile", "build", "policy", "connection", "thread", "turn", "model", "effort",
    "missing_start", "duplicate_start", "missing_end", "duplicate_end", "missing_core", "duplicate_core", "backfill",
    "typed_args", "server", "operation", "metadata", "content_array", "producer", "timing", "core_error",
    "unknown_core", "unknown_notification", "non_mcp", "prefix", "transport", "launch_layers", "request_prompt"])
def test_source_lifecycle_and_complete_conversion_refuse_causal_faults(source_case, fault, record_property):
    assert len(assess(source_case)) == 5
    f = deepcopy(source_case[0])
    s = f["receipt"]["source"]
    raw, session = lines(f["raw"].encode()), lines(f["session"].encode())
    a = next(n for n, r in enumerate(raw) if r.get("method") == "item/started")
    b = next(n for n, r in enumerate(raw) if r.get("method") == "item/completed")
    c = next(n for n, r in enumerate(session) if r.get("payload", {}).get("type") == "item_completed")
    if fault in {"profile", "build", "policy"}:
        key = {"profile": "profile", "build": "build", "policy": "policy_sha256"}[fault]
        s[key] = "unqualified"
    elif fault in {"connection", "thread", "turn"}:
        raw[b]["params"]["threadId" if fault == "thread" else "turnId"] = "other"
        if fault == "connection":
            s["connection_id"] = "not-a-run"
    elif fault in {"model", "effort"}:
        context = next(r["payload"] for r in session if r.get("type") == "turn_context")
        context[fault] = "other"
    elif fault in {"missing_start", "backfill"}:
        missing = raw.pop(a)
        if fault == "backfill":
            raw[-1]["params"]["turn"]["items"] = [raw[b-1]["params"]["item"]]
            assert missing["method"] == "item/started"
    elif fault == "duplicate_start":
        raw.insert(a, deepcopy(raw[a]))
    elif fault == "missing_end":
        del raw[b]
    elif fault == "duplicate_end":
        raw.insert(b, deepcopy(raw[b]))
    elif fault == "missing_core":
        del session[c]
    elif fault == "duplicate_core":
        session.insert(c, deepcopy(session[c]))
    elif fault in {"typed_args", "server", "operation"}:
        item = raw[a]["params"]["item"]
        if fault == "typed_args":
            item["arguments"]["max_items"] = True
        else:
            item["server" if fault == "server" else "tool"] = "unrelated"
    elif fault in {"metadata", "content_array", "producer"}:
        result = raw[b]["params"]["item"]["result"]
        if fault == "metadata":
            result["_meta"] = {"contradictory": True}
        elif fault == "content_array":
            result["content"].append(deepcopy(result["content"][0]))
        else:
            result["structuredContent"]["producer"] = {"name": "different"}
    elif fault == "timing":
        raw[b]["params"]["completedAtMs"] += 1
    elif fault == "core_error":
        session[c]["payload"]["item"]["result"]["isError"] = True
    elif fault == "unknown_core":
        session[c]["payload"]["item"]["unknown"] = "contradiction"
    elif fault == "unknown_notification":
        raw.insert(b, dict(method="transport/dropped", params={}))
    elif fault == "non_mcp":
        raw[a]["params"]["item"]["type"] = "commandExecution"
    elif fault == "prefix":
        session.pop()
    elif fault == "launch_layers":
        response = next(r["result"] for r in raw if r.get("id") == 2)
        response["layers"].append(dict(name=dict(type="project"), version="synthetic", config=dict(mcp_servers={"other": {}})))
    elif fault == "request_prompt":
        requests = lines(f["requests"].encode())
        requests[-1]["params"]["input"][0]["text"] = "changed business stimulus"
        f["requests"] = encode(requests)
    f["raw"], f["session"] = encode(raw), encode(session)
    rebind(f)
    if fault == "transport":
        transport = lines(f["transport"].encode())
        transport[-1]["run_id"] = str(uuid.uuid4())
        f["transport"] = encode(transport)
        rebind(f, rebuild_transport=False)
    with pytest.raises(base.checker.EvidenceError) as caught:
        assess((f, source_case[1]))
    assert len(assess(source_case)) == 5
    record_property("source_causal", json.dumps(dict(fault=fault, refused=str(caught.value), positive=5, restored=5)))


def test_original_protocol_transport_harness_preserves_unsolicited_bytes(tmp_path):
    code = "import sys,json\nfor line in sys.stdin:\n r=json.loads(line)\n print(json.dumps({'method':'synthetic/unsolicited','params':{'original':True}}),flush=True)\n if 'id' in r: print(json.dumps({'id':r['id'],'result':{'echo':r}}),flush=True)\n"
    child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    private = tmp_path / "private"
    public = tmp_path / "evidence"
    private.mkdir()
    public.mkdir()
    stream = StdioCapture(child, private, "harness", str(uuid.uuid4()), str(uuid.uuid4()))
    stream.send("initialize", {"synthetic": True}, 1)
    result = stream.response(1)
    assert result["echo"]["params"] == {"synthetic": True}
    assert not list(public.iterdir())
    first_bytes = {p.name: p.read_bytes() for p in private.iterdir()}
    stream.publish(public)
    assert all((public / name).read_bytes() == raw for name, raw in first_bytes.items())
    stream.send("config/read", {"synthetic": "after publication"}, 2)
    assert stream.response(2)["echo"]["params"]["synthetic"] == "after publication"
    assert stream.close() == 0
    assert lines((public / "harness.jsonl").read_bytes())[0]["method"] == "synthetic/unsolicited"
    assert len(lines((public / "harness.transport.jsonl").read_bytes())) == 6
    assert len(stream.notifications) == 2


@pytest.mark.parametrize("disabled", [False, True])
def test_effective_inherited_layer_refuses_before_publication(source_case, disabled):
    from capture_nr03_app_server import check_effective
    f = source_case[0]
    source = f["receipt"]["source"]
    result = next(r["result"] for r in lines(f["raw"].encode()) if r.get("id") == 2)
    check_effective(result, source["launch_config"], source["runtime_root"])
    result["layers"].append(dict(name=dict(type="project"), config={"synthetic_secret": "DO-NOT-PUBLISH"},
                                 **({"disabledReason": "untrusted"} if disabled else {})))
    with pytest.raises(base.checker.EvidenceError, match="source inherited configuration layer"):
        check_effective(result, source["launch_config"], source["runtime_root"])


def test_main_two_rounds_source_profile_real_consumers_and_journal_clipping(evidence):
    bundle, b, installation = evidence
    # First exercise the explicit additional source fixture route with original
    # bytes and response-item facts preserved at their original paths.
    originals = {p: p.read_bytes() for p in bundle.iterdir() if p.suffix in {".jsonl", ".json"}}
    for stage in ("a-brief", "a-write", "b-brief", "b-write"):
        attach(bundle, stage, config=config_for(installation, b))
    report = base.checker.check_bundle(bundle)
    assert report["status"] == "mechanical_evidence_passed"
    assert all(len(r["brief_coverage"]["obligations"]) == 14 for r in report["rounds"])
    assert all(r["mechanical"]["exact_revisions_verified"] and r["mechanical"]["journal"]["complete_pages_verified"] for r in report["rounds"])
    assert all(p.read_bytes() == raw for p, raw in originals.items() if not p.name.endswith(".delivery.json"))
    # Then materialize separately declared synthetic original protocol inputs
    # into this temporary fixture's v2 entrypoint (not a native claim).
    from capture_nr03_app_server import bind_delivery
    prefixes = {}
    for stage in ("a-brief", "a-write", "b-brief", "b-write"):
        f = json.loads((bundle / f"{stage}.source-fixture.json").read_text())
        receipt = f["receipt"]
        receipt["schema_version"] = "veqtor_next_round_capture.v2"
        if stage.endswith("write"):
            parent = stage.replace("write", "brief")
            receipt["parent_receipt_sha256"] = base.checker._file_sha256(str(bundle / f"{parent}.receipt.json"))
            f["session"] = prefixes[parent] + encode(lines(f["session"].encode())[1:])
        prefixes[stage] = f["session"]
        for key, suffix in (("raw", "jsonl"), ("requests", "requests.jsonl"), ("transport", "transport.jsonl"), ("session", "session.jsonl")):
            (bundle / f"{stage}.{suffix}").write_text(f[key])
        (bundle / f"{stage}.stderr.txt").write_bytes(b"")
        receipt["events_sha256"] = hashlib.sha256(f["raw"].encode()).hexdigest()
        receipt["source"]["session_sha256"] = hashlib.sha256(f["session"].encode()).hexdigest()
        receipt["source"]["stderr_sha256"] = hashlib.sha256(b"").hexdigest()
        (bundle / f"{stage}.receipt.json").unlink()
        (bundle / f"{stage}.delivery.json").unlink()
        bind_delivery(bundle, stage, receipt)
    assert base.checker.check_bundle(bundle)["status"] == "mechanical_evidence_passed"
    stage = base.checker.load_stage(bundle, "a-write", b, installation)
    session = lines((bundle / "a-write.session.jsonl").read_bytes())
    turn_start = max(n for n, r in enumerate(session) if r.get("payload", {}).get("type") == "task_started")
    from nr03_model_delivery import DOCUMENT_OPERANDS
    roles = set()
    # Exercise every supported document operand on the same qualified complete
    # write, including both apply operands and legacy verify anchors.
    for call in stage["calls"]:
        for role in DOCUMENT_OPERANDS.get(call["tool"], ()):
            if (call["tool"], role) in roles:
                continue
            labelled = deepcopy(session)
            observed = next(r["payload"] for r in labelled[turn_start:] if r.get("payload", {}).get("type") == "function_call_output"
                            and r["payload"]["call_id"] == call["id"])
            observed["output"] = json.dumps(dict(file=call["arguments"][role], result=call["payload"]))
            result = validate_model_delivery(stage["calls"], labelled, thread=stage["thread"], cwd=stage["receipt"]["cwd"],
                                             final_text=stage["messages"][-1]["text"])
            assert result[call["id"]]["label_binding"]["file_argument"] == role
            roles.add((call["tool"], role))
    assert roles == {(tool, role) for tool, operands in DOCUMENT_OPERANDS.items() for role in operands}
    exports = [c for c in stage["calls"] if c["tool"] == "export_decision_record"]
    assert len(exports) > 1
    row = next(r["payload"] for r in session[turn_start:] if r.get("payload", {}).get("type") == "function_call_output" and r["payload"]["call_id"] == exports[0]["id"])
    row["output"] = '{"producer":'
    delivered = validate_model_delivery(stage["calls"], session, thread=stage["thread"], cwd=stage["receipt"]["cwd"], final_text=stage["messages"][-1]["text"])
    assert exports[0]["id"] not in delivered and exports[-1]["id"] in delivered
    assert stage["calls"][-1]["payload"]["truncated"] is False


@pytest.mark.parametrize("form", ["label", "index", "fulfilled_label", "label_fulfilled", "flat", "separate"])
def test_labelled_result_finite_grammar_qualified_source(source_case, form):
    f = deepcopy(source_case[0])
    parsed = assess(source_case, deliver=False)
    call = parsed["calls"][1]
    session = lines(f["session"].encode())
    row = next(r["payload"] for r in session if r.get("payload", {}).get("type") == "function_call_output" and r["payload"]["call_id"] == call["id"])
    item = dict(file=Path(call["arguments"]["path"]).name, result=call["payload"])
    if form == "index":
        item["index"] = 9
    elif form == "fulfilled_label":
        item = dict(status="fulfilled", value=item)
    elif form == "label_fulfilled":
        item["result"] = dict(status="fulfilled", value=item["result"])
    elif form == "flat":
        item = [item]
    row["output"] = [dict(type="input_text", text=json.dumps(item))] if form == "separate" else json.dumps(item)
    f["session"] = encode(session)
    rebind(f)
    result = assess((f, source_case[1]))
    assert len(result) == 5 and result[call["id"]]["label_binding"]["file_path"] == call["arguments"]["path"]


@pytest.mark.parametrize("fault", ["wrong_file", "wrong_index", "bool_index", "float_index", "relative_file", "unknown_field",
    "repeat_label", "repeat_fulfilled", "rejected", "nested", "malformed_member", "duplicate_json", "nonfinite", "duplicate_output", "replay",
    "zero_label_values", "multiple_label_values", "nested_collection", "envelope_metadata"])
def test_label_and_output_fault_is_local_to_qualified_occurrence(source_case, fault):
    f = deepcopy(source_case[0])
    parsed = assess(source_case, deliver=False)
    prior, call = parsed["calls"][1], parsed["calls"][3]
    session = lines(f["session"].encode())
    index = next(n for n, r in enumerate(session) if r.get("payload", {}).get("type") == "function_call_output" and r["payload"]["call_id"] == call["id"])
    row = session[index]["payload"]
    item = dict(file=call["arguments"]["path"], index=9, result=call["payload"])
    if fault == "wrong_file":
        item["file"] = "/wrong/source.docx"
    elif fault in {"wrong_index", "bool_index", "float_index"}:
        item["index"] = {"wrong_index": 8, "bool_index": True, "float_index": 9.0}[fault]
    elif fault == "relative_file":
        item["file"] = "../" + Path(item["file"]).name
    elif fault == "unknown_field":
        item["extra"] = True
    elif fault == "repeat_label":
        item["result"] = deepcopy(item)
    elif fault == "repeat_fulfilled":
        item = dict(status="fulfilled", value=dict(status="fulfilled", value=item))
    elif fault == "rejected":
        item = dict(status="rejected", value=item)
    elif fault == "nested":
        item = dict(unrelated=item)
    elif fault == "malformed_member":
        item = [item, {"wrong": "member"}]
    elif fault in {"zero_label_values", "multiple_label_values"}:
        item["result"] = [] if fault == "zero_label_values" else [dict(type="text", text=json.dumps(call["payload"])) for _ in range(2)]
    elif fault == "nested_collection":
        item = [[item]]
    elif fault == "envelope_metadata":
        item["result"] = dict(content=[dict(type="text", text=json.dumps(call["payload"]))], structuredContent=call["payload"], _meta={"unrelated": True})
    row["output"] = json.dumps(item)
    if fault == "duplicate_json":
        row["output"] = row["output"][:-1] + ',"index":9}'
    elif fault == "nonfinite":
        row["output"] = row["output"].replace('"index": 9', '"index": NaN')
    elif fault == "duplicate_output":
        session.insert(index, deepcopy(session[index]))
    elif fault == "replay":
        row["call_id"] = prior["id"]
    f["session"] = encode(session)
    rebind(f)
    delivered = assess((f, source_case[1]))
    assert call["id"] not in delivered
    if fault != "replay":
        assert prior["id"] in delivered
    assert len(assess(source_case)) == 5


def exec_form(fixture, *, batch=False):
    f = deepcopy(fixture)
    session = lines(f["session"].encode())
    result, blocks = [], []
    outer = None
    for row in session:
        p = row.get("payload", {})
        if row.get("type") == "response_item" and p.get("type") == "function_call":
            outer = "synthetic-exec-batch" if batch else "synthetic-exec-" + p["call_id"]
            if not batch or not any(r.get("payload", {}).get("call_id") == outer for r in result):
                result.append(dict(type="response_item", payload=dict(type="custom_tool_call", name="exec", call_id=outer,
                              input="/* synthetic original code; never evaluated by checker */ text(results);")))
        elif row.get("type") == "response_item" and p.get("type") == "function_call_output":
            blocks.append(dict(type="input_text", text=p["output"]))
            if not batch:
                result.append(dict(type="response_item", payload=dict(type="custom_tool_call_output", call_id=outer, output=blocks)))
                blocks = []
        elif batch and row.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "assistant":
            result.append(dict(type="response_item", payload=dict(type="custom_tool_call_output", call_id=outer, output=blocks)))
            result.append(row)
        else:
            result.append(row)
    f["session"] = encode(result)
    rebind(f)
    return f


def test_exec_qualified_id_sequential_equals_and_unresolved_batch(source_case):
    f = exec_form(source_case[0])
    assert len(assess((f, source_case[1]))) == 5
    batch = exec_form(source_case[0], batch=True)
    # The complete eligible set has two equal reads and two equal quotes. No
    # output label, order or consumed set is allowed to select an occurrence.
    assert set(assess((batch, source_case[1]))) == {assess(source_case, deliver=False)["calls"][0]["id"]}
    assert len(assess((f, source_case[1]))) == 5


@pytest.mark.parametrize("fault", ["projected_id", "direct_before_action", "direct_after_output", "exec_after_output", "exec_overlap", "exec_wrong_inner"])
def test_qualified_original_identity_and_producer_timing(source_case, fault):
    f = exec_form(source_case[0]) if fault.startswith("exec") else deepcopy(source_case[0])
    assert len(assess((f, source_case[1]))) == 5
    session = lines(f["session"].encode())
    cores = [n for n, r in enumerate(session) if r.get("payload", {}).get("type") == "item_completed"]
    n = cores[-1]
    if fault == "projected_id":
        action = session[n-1]["payload"]
        session[n+1]["payload"]["call_id"] = action["call_id"] = "projected-cli-item-123"
    elif fault == "direct_before_action":
        session.insert(n-1, session.pop(n))
    elif fault in {"direct_after_output", "exec_after_output"}:
        session.insert(n+1, session.pop(n))
    elif fault == "exec_overlap":
        session.insert(n, dict(type="response_item", payload=dict(type="custom_tool_call", call_id="overlap", name="exec", input="text('overlap');")))
    elif fault == "exec_wrong_inner":
        session[n]["payload"]["item"]["id"] = "unbound-inner"
    f["session"] = encode(session)
    rebind(f)
    if fault in {"projected_id", "exec_wrong_inner"}:
        with pytest.raises(base.checker.EvidenceError):
            assess((f, source_case[1]))
    else:
        result = assess((f, source_case[1]))
        assert len(result) == 4
        assert assess(source_case, deliver=False)["calls"][-1]["id"] not in result


@pytest.mark.parametrize("fault", ["same_basename", "same_operand_roles", "unsupported_index", "nonparagraph_ref", "missing_ref_type"])
def test_label_operand_and_reference_contract(fault):
    from nr03_model_delivery import checked_labels
    call = dict(tool="inspect_document", arguments=dict(path="/one/source.docx", selection=dict(paragraph_ref=dict(
        schema_version="paragraph_ref.v1", ref_type="paragraph", paragraph_index=9))))
    label = dict(file="source.docx", index=9)
    paths = {"/one/source.docx"}
    assert checked_labels(label, call, paths) is not None
    if fault == "same_basename":
        paths.add("/two/source.docx")
    elif fault == "same_operand_roles":
        call = dict(tool="apply_edits", arguments=dict(source_path="/one/source.docx", output_path="/one/source.docx"))
        label.pop("index")
    elif fault == "unsupported_index":
        call["tool"] = "extract_redlines"
    elif fault == "nonparagraph_ref":
        call["arguments"]["selection"]["paragraph_ref"]["ref_type"] = "section"
    else:
        del call["arguments"]["selection"]["paragraph_ref"]["ref_type"]
    assert checked_labels(label, call, paths) is None
