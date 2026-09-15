# SPDX-License-Identifier: Apache-2.0
"""Synthetic client-shape/attribution controls, never native acceptance evidence."""
from copy import deepcopy
import json
from pathlib import Path
import tomllib

import pytest

import test_next_round_acceptance as base
from nr03_model_delivery import payloads, validate_model_delivery


def example_calls():
    return [dict(id=f"inner-{i}", server=base.SERVER, tool=tool, arguments=args, failed=False,
                 payload=dict(producer={"name": "synthetic"}, value=i))
            for i, (tool, args) in enumerate([
                ("inspect_document", dict(path="/synthetic/source.docx", mode="read")),
                ("verify_quote", dict(path="/synthetic/source.docx", quote="Complete text")),
                ("preflight_edits", dict(source_path="/synthetic/source.docx", edits=[])),
            ])]


def exec_session(calls):
    session = [dict(type="session_meta", payload=dict(id="synthetic", cwd="/synthetic")),
               dict(type="event_msg", payload=dict(type="task_started", turn_id="turn")),
               dict(type="response_item", payload=dict(type="custom_tool_call", name="exec", call_id="outer",
                    input="const results = await Promise.allSettled([" + ",".join(
                        f"tools.mcp__{c['server']}__{c['tool']}({json.dumps(c['arguments'])})" for c in calls)
                        + "]); results.forEach(text);"))]
    blocks = []
    for c in calls:
        content = [dict(type="text", text=json.dumps(c["payload"]))]
        session.append(dict(type="event_msg", payload=dict(type="item_completed", thread_id="synthetic", turn_id="turn",
            item=dict(type="McpToolCall", id=c["id"], server=c["server"], tool=c["tool"], arguments=c["arguments"],
                      status="completed", result=dict(content=content, structuredContent=c["payload"], isError=False)))))
        blocks.append(dict(type="input_text", text=json.dumps(dict(status="fulfilled", value=content))))
    session += [dict(type="response_item", payload=dict(type="custom_tool_call_output", call_id="outer", output=blocks)),
                dict(type="response_item", payload=dict(type="message", role="assistant",
                    content=[dict(type="output_text", text="Complete.")])),
                dict(type="event_msg", payload=dict(type="task_complete", turn_id="turn"))]
    return session


def delivery(calls, session):
    return validate_model_delivery(calls, session, thread="synthetic", cwd="/synthetic", final_text="Complete.")


def test_exec_batch_with_original_inner_operations_and_different_ids():
    calls = example_calls()
    delivered = delivery(calls, exec_session(calls))
    assert set(delivered) == {c["id"] for c in calls}
    assert {row["model_call_id"] for row in delivered.values()} == {"outer"}


@pytest.mark.parametrize("fault", ["operation", "arguments"])
def test_direct_matching_text_from_contradictory_action_is_not_attributed(fault):
    call = example_calls()[-1]
    session = exec_session([])
    action = session[2]["payload"]
    action.update(type="function_call", name=call["tool"], call_id=call["id"], arguments=json.dumps(call["arguments"]))
    session[3]["payload"].update(type="function_call_output", call_id=call["id"], output=json.dumps(call["payload"]))
    assert set(delivery([call], session)) == {call["id"]}
    bad = deepcopy(session)
    if fault == "operation":
        bad[2]["payload"]["name"] = "inspect_document"
    else:
        bad[2]["payload"]["arguments"] = json.dumps(dict(source_path="/unselected/another.docx", edits=[]))
    assert not delivery([call], bad)
    assert set(delivery([call], session)) == {call["id"]}


def test_sdist_selects_required_local_protocol_reference():
    root = Path(__file__).parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    include = config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    target = "docs/NR03_ADVERSE_OBLIGATIONS.md"
    assert "NR03_ADVERSE_OBLIGATIONS.md" in (root / "docs/NR03_ACCEPTANCE.md").read_text()
    assert "/" + target in include and (root / target).is_file()


def test_three_distinct_id_namespaces_require_unique_complete_correspondence():
    calls = example_calls()
    session = exec_session(calls)
    for row in session:
        item = row.get("payload", {}).get("item")
        if item:
            item["id"] = "client-" + item["id"]
    delivered = delivery(calls, session)
    assert set(delivered) == {c["id"] for c in calls}
    assert all(row["inner_call_id"] == "client-" + ident for ident, row in delivered.items())
    duplicate = deepcopy(calls[-1])
    duplicate["id"] = "indistinguishable-raw-call"
    assert calls[-1]["id"] not in delivery(calls + [duplicate], session)


@pytest.mark.parametrize("form", ["content_array", "mcp", "fulfilled_mcp", "settled_array", "multiple_text_blocks"])
def test_complete_client_envelope_forms_with_original_inner_records(form):
    calls = example_calls()
    session = exec_session(calls)
    output = session[-3]["payload"]
    contents = [[dict(type="text", text=json.dumps(c["payload"]))] for c in calls]
    values = [dict(content=v, structuredContent=c["payload"]) for c, v in zip(calls, contents)]
    if form == "content_array":
        values = contents
    elif form == "fulfilled_mcp":
        values = [dict(status="fulfilled", value=v) for v in values]
    elif form == "settled_array":
        values = [[dict(status="fulfilled", value=v) for v in contents]]
    elif form == "multiple_text_blocks":
        values = [dict(status="fulfilled", value=[block for content in contents for block in content])]
    output["output"] = [dict(type="input_text", text=json.dumps(v)) for v in values]
    assert set(delivery(calls, session)) == {c["id"] for c in calls}


@pytest.mark.parametrize("fault", ["operation", "arguments", "argument_type", "server", "inner_payload", "inner_structured",
    "missing_inner", "wrong_thread", "wrong_turn", "wrong_outer", "missing_input", "missing_action", "after_output",
    "clipped", "metadata", "nested", "rejected", "mismatched_output_type", "overlapping_actions", "replayed_turn"])
def test_exec_correspondence_and_complete_delivery_causal_negatives(fault, record_property):
    calls = example_calls()
    session = exec_session(calls)
    assert len(delivery(calls, session)) == 3
    bad = deepcopy(session)
    inner = bad[5]["payload"]["item"]
    output = bad[-3]["payload"]
    if fault == "operation":
        inner["tool"] = "inspect_document"
    elif fault in {"arguments", "argument_type"}:
        inner["arguments"]["source_path"] = "/unselected/another.docx" if fault == "arguments" else True
    elif fault == "server":
        inner["server"] = "unrelated_server"
    elif fault in {"inner_payload", "inner_structured"}:
        inner["result"]["structuredContent"]["value"] = 999
        if fault == "inner_payload":
            inner["result"]["content"][0]["text"] = json.dumps(inner["result"]["structuredContent"])
    elif fault == "missing_inner":
        del bad[5]
    elif fault in {"wrong_thread", "wrong_turn"}:
        bad[5]["payload"]["thread_id" if fault == "wrong_thread" else "turn_id"] = "other"
    elif fault == "wrong_outer":
        bad[2]["payload"].update(type="function_call", name="inspect_document", arguments='{"path":"/unselected/another.docx"}')
        output["type"] = "function_call_output"
    elif fault == "missing_input":
        del bad[2]["payload"]["input"]
    elif fault == "missing_action":
        del bad[2]
    elif fault == "after_output":
        bad.insert(len(bad)-2, bad.pop(5))
    elif fault in {"clipped", "metadata", "nested", "rejected"}:
        value = {"clipped": '{"producer":', "metadata": json.dumps(dict(complete=True, sha256=base.checker._digest(calls[-1]["payload"]))),
                 "nested": json.dumps(dict(unrelated=dict(status="fulfilled", value=[dict(type="text", text=json.dumps(calls[-1]["payload"]))]))),
                 "rejected": json.dumps(dict(status="rejected", value=[dict(type="text", text=json.dumps(calls[-1]["payload"]))]))}[fault]
        output["output"][-1]["text"] = value
    elif fault == "mismatched_output_type":
        output["type"] = "function_call_output"
    elif fault == "overlapping_actions":
        bad.insert(3, dict(type="response_item", payload=dict(type="custom_tool_call", name="exec", call_id="overlap", input="text('unrelated');")))
    elif fault == "replayed_turn":
        bad.insert(len(bad)-2, dict(type="event_msg", payload=dict(type="task_started", turn_id="new-turn")))
    if fault in {"missing_action", "replayed_turn"}:
        with pytest.raises(base.checker.EvidenceError, match="corresponding call" if fault == "missing_action" else "completion turn differs") as caught:
            delivery(calls, bad)
        result = str(caught.value)
    else:
        result = delivery(calls, bad)
        assert calls[-1]["id"] not in result
    assert len(delivery(calls, session)) == 3
    record_property("F12_causal", json.dumps(dict(fault=fault, positive=3, negative=result, restored=3)))


def test_duplicate_inner_and_replayed_output_cannot_supply_new_deliveries():
    calls = example_calls()
    session = exec_session(calls)
    bad = deepcopy(session)
    bad.insert(6, deepcopy(bad[5]))
    with pytest.raises(base.checker.EvidenceError, match="inner native attribution is ambiguous"):
        delivery(calls, bad)
    bad[6]["payload"]["item"]["id"] = "different-id-same-invocation"
    with pytest.raises(base.checker.EvidenceError, match="multiple inner producers"):
        delivery(calls, bad)
    output = deepcopy(session[-3])
    session[-3]["payload"]["output"][-1]["text"] = "clipped"
    session.insert(len(session)-2, output)
    assert calls[-1]["id"] not in delivery(calls, session)


def test_ambiguous_identical_batch_payloads_and_arbitrary_nested_text_refuse():
    calls = example_calls()
    calls[-1]["payload"] = deepcopy(calls[0]["payload"])
    assert set(delivery(calls, exec_session(calls))) == {calls[1]["id"]}
    value = dict(status="fulfilled", value=[dict(type="text", text=json.dumps(calls[0]["payload"]))])
    assert not payloads(json.dumps(dict(unrelated=value)))
    assert not payloads(json.dumps(dict(status="fulfilled", value=value)))


def notified_session():
    """Explicit synthetic qualified occurrences, never original runtime evidence."""
    from nr03_app_server import PROFILE
    calls = example_calls()
    session = exec_session(calls)
    for n, call in enumerate(calls):
        core = session[n + 3]["payload"]["item"]
        call["source_occurrence"] = dict(profile=PROFILE, thread_id="synthetic", turn_id="turn",
            item_id=call["id"], core_item=deepcopy(core), core_line=n + 4)
    session[-3]["payload"].update(id="empty-completion", output=[])
    for n, call in enumerate(calls):
        session.insert(len(session) - 2, dict(type="response_item", payload=dict(type="custom_tool_call_output",
            id=f"notification-{n}", call_id="outer", name="exec", output=json.dumps(call["payload"]),
            internal_chat_message_metadata_passthrough=dict(turn_id="turn", create_time=1000.0 + n))))
    return calls, session


@pytest.mark.parametrize("fault", ["missing", "duplicate", "duplicate_value_new_id", "duplicate_id_different_value",
    "missing_id", "wrong_name", "wrong_turn", "extra_metadata", "typed_time", "clipped", "summary", "block_metadata",
    "before_core", "missing_core", "typed_args", "late", "equal_producers"])
def test_distinct_notification_occurrences_are_independent_and_fail_closed(fault):
    calls, session = notified_session()
    expected = {c["id"] for c in calls}
    positive = delivery(calls, session)
    assert set(positive) == expected
    assert {r["output_id"] for r in positive.values()} == {f"notification-{n}" for n in range(3)}
    bad, changed = deepcopy(session), deepcopy(calls)
    at = next(n for n, row in enumerate(bad) if row.get("payload", {}).get("id") == "notification-0")
    out = bad[at]["payload"]
    missing = {calls[0]["id"]}
    if fault == "missing":
        bad.pop(at)
    elif fault in {"duplicate", "duplicate_value_new_id"}:
        duplicate = deepcopy(bad[at])
        if fault == "duplicate_value_new_id":
            duplicate["payload"]["id"] = "different-original-output-id"
        bad.insert(at, duplicate)
    elif fault == "duplicate_id_different_value":
        bad[at + 1]["payload"]["id"] = out["id"]
        missing.add(calls[1]["id"])
    elif fault == "missing_id":
        del out["id"]
    elif fault == "wrong_name":
        out["name"] = "other"
    elif fault in {"wrong_turn", "extra_metadata", "typed_time"}:
        key, value = {"wrong_turn": ("turn_id", "foreign"), "extra_metadata": ("unknown", None), "typed_time": ("create_time", True)}[fault]
        out["internal_chat_message_metadata_passthrough"][key] = value
    elif fault == "clipped":
        out["output"] = out["output"][:-1]
    elif fault == "summary":
        out["output"] = '{"record_id":"summary"}'
    elif fault == "block_metadata":
        out["output"] = json.dumps(dict(content=[dict(type="text", text=out["output"], _meta={"unknown": True})]))
    elif fault == "before_core":
        bad.insert(3, bad.pop(at))
        missing = expected
    elif fault == "missing_core":
        bad.pop(3)
    elif fault == "typed_args":
        bad[3]["payload"]["item"]["arguments"]["mode"] = True
    elif fault == "late":
        moved = bad.pop(at)
        bad.insert(len(bad) - 1, moved)
    else:
        changed[1]["payload"] = deepcopy(changed[0]["payload"])
        core = bad[4]["payload"]["item"]
        core["result"] = deepcopy(bad[3]["payload"]["item"]["result"])
        changed[1]["source_occurrence"]["core_item"] = deepcopy(core)
        missing.add(calls[1]["id"])
    assert set(delivery(changed, bad)) == expected - missing
    assert set(delivery(calls, session)) == expected
