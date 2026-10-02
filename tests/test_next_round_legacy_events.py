# SPDX-License-Identifier: Apache-2.0
"""Synthetic legacy persistence controls; never reconstructed native evidence."""
from copy import deepcopy

import pytest

import test_next_round_source_profile as source
from nr03_app_server import core_to_app, legacy_core_item, lines
from nr03_source_fixtures import encode

files = source.files
source_case = source.source_case


@pytest.fixture
def legacy_case(source_case):
    fixture, producer = deepcopy(source_case)
    rows = lines(fixture["session"].encode())
    rows[0]["payload"]["history_mode"] = "legacy"
    for row in rows:
        event = row.get("payload", {})
        if event.get("type") != "item_completed" or event.get("item", {}).get("type") != "McpToolCall":
            continue
        item = event["item"]
        row["payload"] = dict(type="mcp_tool_call_end", call_id=item["id"],
            invocation={k: deepcopy(item[k]) for k in ("server", "tool", "arguments")},
            read_only_hint=item.get("readOnlyHint"), duration=deepcopy(item["duration"]),
            result={"Ok": deepcopy(item["result"])})
    fixture["session"] = encode(rows)
    source.rebind(fixture)
    return fixture, producer


def test_legacy_original_occurrences_and_model_outputs(legacy_case):
    before = deepcopy(legacy_case)
    parsed = source.assess(legacy_case, deliver=False)
    assert len(parsed["calls"]) == len(source.assess(legacy_case)) == 5
    assert legacy_case == before
    rows = lines(legacy_case[0]["session"].encode())
    for call in parsed["calls"]:
        original = rows[call["source_occurrence"]["core_line"] - 1]["payload"]
        assert original["type"] == "mcp_tool_call_end"
        assert original["call_id"] == call["id"]
        assert original["result"]["Ok"] == call["core_result"]


@pytest.mark.parametrize("fault", ["missing", "duplicate", "foreign_id", "foreign_turn_field", "history",
    "server", "tool", "arguments", "result", "error_flag", "result_union", "duration", "hint",
    "unknown_field", "missing_start", "wrong_start", "end_before_start", "boolean_clock"])
def test_legacy_contradictions_refuse_and_original_restores(legacy_case, fault):
    fixture, producer = deepcopy(legacy_case)
    rows = lines(fixture["session"].encode())
    index = next(i for i, r in enumerate(rows) if r.get("payload", {}).get("type") == "mcp_tool_call_end")
    event = rows[index]["payload"]
    if fault == "missing":
        del rows[index]
    elif fault == "duplicate":
        rows.insert(index, deepcopy(rows[index]))
    elif fault == "foreign_id":
        event["call_id"] = "another-invocation"
    elif fault == "foreign_turn_field":
        event["turn_id"] = "another-turn"
    elif fault == "history":
        rows[0]["payload"]["history_mode"] = "paginated"
    elif fault in {"server", "tool"}:
        event["invocation"][fault] = "foreign"
    elif fault == "arguments":
        event["invocation"]["arguments"] = {"path": "/foreign.docx"}
    elif fault == "result":
        event["result"]["Ok"]["structuredContent"] = {"different": True}
    elif fault == "error_flag":
        event["result"]["Ok"]["isError"] = True
    elif fault == "result_union":
        event["result"]["Err"] = "contradictory transport error"
    elif fault == "duration":
        event["duration"]["nanos"] += 1000000
    elif fault == "hint":
        event["read_only_hint"] = True
    elif fault == "unknown_field":
        event["extra"] = True
    else:
        raw = lines(fixture["raw"].encode())
        start = next(r for r in raw if r.get("method") == "item/started"
                     and r["params"]["item"]["id"] == event["call_id"])
        end = next(r for r in raw if r.get("method") == "item/completed"
                   and r["params"]["item"]["id"] == event["call_id"])
        if fault == "missing_start":
            raw.remove(start)
        elif fault == "wrong_start":
            start["params"]["item"]["arguments"] = {"path": "/foreign.docx"}
        elif fault == "boolean_clock":
            start["params"]["startedAtMs"] = True
        else:
            end["params"]["completedAtMs"] = start["params"]["startedAtMs"] - 1
        fixture["raw"] = encode(raw)
    fixture["session"] = encode(rows)
    source.rebind(fixture)
    with pytest.raises(source.base.checker.EvidenceError):
        source.assess((fixture, producer))
    assert len(source.assess(legacy_case)) == 5


def test_legacy_error_retains_exact_failure_and_rejects_malformed_union(legacy_case):
    rows = lines(legacy_case[0]["session"].encode())
    event = deepcopy(next(r["payload"] for r in rows if r.get("payload", {}).get("type") == "mcp_tool_call_end"))
    event["result"] = {"Err": "original transport failure"}
    projected = core_to_app(legacy_core_item(event))
    assert projected["status"] == "failed" and projected["result"] is None
    assert projected["error"] == {"message": "original transport failure"}
    event["result"] = {"Ok": None}
    with pytest.raises(source.base.checker.EvidenceError):
        legacy_core_item(event)
