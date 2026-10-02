# SPDX-License-Identifier: Apache-2.0
"""F23 causal controls through the complete parser/delivery path; synthetic only."""
from copy import deepcopy

import pytest

import test_next_round_source_profile as source
from nr03_app_server import lines
from nr03_source_fixtures import encode

files, source_case = source.files, source.source_case


def output_form(fixture, form):
    f = deepcopy(fixture) if form == "direct" else source.exec_form(fixture)
    if form == "notifications":
        rows = []
        for row in lines(f["session"].encode()):
            p = row.get("payload", {})
            if p.get("type") == "custom_tool_call_output":
                empty = deepcopy(row)
                empty["payload"].update(id=p["id"] + "-empty", output=[])
                rows.append(empty)
            rows.append(row)
        f["session"] = encode(rows)
        source.rebind(f)
    return f


@pytest.mark.parametrize("form", ["direct", "ordinary_exec", "notifications"])
@pytest.mark.parametrize("fault", ["duplicate_id", "foreign_turn", "duplicate_id_foreign_turn", "contradictory_duplicate",
    "contradictory_duplicate_id", "missing_id", "typed_id", "wrong_name", "typed_time"])
def test_original_output_identity_is_uniform_and_duplicates_remain_visible(source_case, form, fault):
    f = output_form(source_case[0], form)
    positive = source.assess((f, source_case[1]))
    calls = source.assess((f, source_case[1]), deliver=False)["calls"]
    assert len(positive) == 5
    target, second = calls[:2]
    bad = deepcopy(f)
    rows = lines(bad["session"].encode())
    at = positive[target["id"]]["line"] - 1
    other_at = positive[second["id"]]["line"] - 1
    item = rows[at]["payload"]
    missing = {target["id"]}
    if fault in {"duplicate_id", "duplicate_id_foreign_turn"}:
        rows[other_at]["payload"]["id"] = item["id"]
        missing.add(second["id"])
        if fault == "duplicate_id_foreign_turn":
            rows[other_at]["payload"]["internal_chat_message_metadata_passthrough"] = dict(turn_id="foreign", create_time=1.0)
    elif fault in {"contradictory_duplicate", "contradictory_duplicate_id"}:
        dup = deepcopy(rows[at])
        if fault == "contradictory_duplicate":
            dup["payload"]["id"] += "-different-id"
        dup["payload"]["internal_chat_message_metadata_passthrough"] = dict(turn_id="foreign", create_time=1.0)
        rows.insert(at, dup)
    elif fault == "missing_id":
        del item["id"]
    elif fault == "typed_id":
        item["id"] = True
    elif fault == "wrong_name":
        item["name"] = "unrelated"
    else:
        item["internal_chat_message_metadata_passthrough"] = dict(
            turn_id="foreign" if fault == "foreign_turn" else f["receipt"]["source"]["turn_id"],
            create_time=True if fault == "typed_time" else 1.0)
    bad["session"] = encode(rows)
    source.rebind(bad)
    # The full original-source parser still qualifies all five invocations.
    assert len(source.assess((bad, source_case[1]), deliver=False)["calls"]) == 5
    assert set(source.assess((bad, source_case[1]))) == set(positive) - missing
    assert source.assess((f, source_case[1])) == positive
