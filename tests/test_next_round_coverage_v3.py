# SPDX-License-Identifier: Apache-2.0
"""Synthetic causal brief coverage and independent model-delivery controls."""
from copy import deepcopy
import json
import zipfile

import pytest
from lxml import etree

import test_next_round_acceptance as base
from nr03_coverage import brief_coverage
from nr03_model_delivery import validate_model_delivery
from nr03_scenario import SELECTED, document, texts, package, xml
from veqtor_docx._ooxml import w
from veqtor_mcp import server, records


def add(events, tool, **args):
    payload = getattr(server, tool)(**args)
    ident = dict(id=str(len(events)), type="mcp_tool_call", server=base.SERVER, tool=tool, arguments=args)
    events += [dict(type="item.started", item=dict(ident, status="in_progress", result=None, error=None)),
               dict(type="item.completed", item=dict(ident, status="completed", error=None,
                    result=dict(structured_content=payload, content=[dict(type="text", text=json.dumps(payload))])))]
    return payload


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setenv("VEQTOR_DISABLE_DECISION_RECORD", "1")
    paths = []
    for name in ("incoming-a", "previous"):
        path = tmp_path / f"{name}.docx"
        path.write_bytes(document(name))
        with zipfile.ZipFile(path) as archive:
            parts = {n: archive.read(n) for n in archive.namelist()}
        root = etree.fromstring(parts["word/document.xml"])
        p = root.find(w("body"))[0]
        props = etree.Element(w("pPr"))
        etree.SubElement(props, w("outlineLvl"), {w("val"): "0"})
        p.insert(0, props)
        parts["word/document.xml"] = xml(root)
        path.write_bytes(package(parts))
        paths.append((str(path), texts(name)))
    return paths


def capture(files, form):
    events = [dict(type="thread.started", thread_id="synthetic-v3-coverage"), dict(type="turn.started")]
    for path, _ in files:
        outline = add(events, "inspect_document", path=path, mode="outline")
        browse = add(events, "inspect_document", path=path, mode="browse", max_items=100)
        assert len(outline["sections"]) == 1
        if form != "direct":
            cursor = None
            while True:
                page = add(events, "inspect_document", path=path, mode="read",
                    selection={"section_ref": outline["sections"][0]["section_ref"]}, cursor=cursor,
                    max_items=100 if form == "single" else 2)
                for row in page["paragraphs"]:
                    if row["paragraph_ref"]["paragraph_index"] in SELECTED:
                        add(events, "verify_quote", path=path, anchor=row["paragraph_ref"], quote=row["text"],
                            paragraph_projection="accepted_current_v1")
                cursor = page["next_cursor"]
                if cursor is None:
                    break
        if form in {"direct", "mixed"}:
            for row in browse["paragraphs"]:
                index = row["paragraph_ref"]["paragraph_index"]
                if index in (SELECTED if form == "direct" else (9,)):
                    add(events, "inspect_document", path=path, mode="read", selection={"paragraph_ref": row["paragraph_ref"]})
                    add(events, "verify_quote", path=path, anchor=row["paragraph_ref"], quote=row["text"],
                        paragraph_projection="accepted_current_v1")
    events += [dict(type="item.completed", item=dict(id="final", type="agent_message", text="Synthetic brief.")),
               dict(type="turn.completed")]
    return events


def parsed(events):
    return base.checker.parse_native(events, dict(name="veqtor-mcp", version=base.__version__, build=records.SOURCE_SNAPSHOT_IDENTITY))


def model_session(events, *, custom=False):
    _, calls, _ = parsed(events)
    session = [dict(type="session_meta", payload=dict(id="synthetic-v3-coverage", cwd="/synthetic", synthetic=True)),
               dict(type="event_msg", payload=dict(type="task_started"))]
    for c in calls:
        kind = "custom_tool_call" if custom else "function_call"
        value = json.dumps(c["payload"])
        session += [dict(type="response_item", payload=dict(type=kind, call_id=c["id"], name=c["tool"],
                        **{"input" if custom else "arguments": json.dumps(c["arguments"])})),
                    dict(type="response_item", payload=dict(type=kind+"_output", call_id=c["id"],
                        output=[dict(type="input_text", text=value)] if custom else value))]
    session += [dict(type="response_item", payload=dict(type="message", role="assistant",
                    content=[dict(type="output_text", text="Synthetic brief.")])),
                dict(type="event_msg", payload=dict(type="task_complete"))]
    return session


def assess(events, files, session=None):
    _, calls, messages = parsed(events)
    session = model_session(events) if session is None else session
    from nr03_source_fixtures import supplement, launch_config
    from nr03_app_server import parse_protocol, lines
    receipt = dict(command=["/synthetic/codex"], cwd="/synthetic", resumed_thread_id=None,
                   prompt_sha256=base.checker._digest("unused"), fixture_prompt="synthetic coverage stimulus")
    import hashlib
    receipt["prompt_sha256"] = hashlib.sha256(receipt["fixture_prompt"].encode()).hexdigest()
    fixture = supplement(events, session, receipt, launch_config("/synthetic/python", model=base.MODEL, reasoning_effort="high", journal_disabled=True))
    qualified = parse_protocol(*(fixture[k].encode() for k in ("raw", "requests", "transport", "session")),
        fixture["receipt"]["source"], receipt=fixture["receipt"], producer=calls[0]["payload"]["producer"])
    for c, q in zip(calls, qualified["calls"]):
        c.update(source_occurrence=q["source_occurrence"], core_result=q["core_result"])
    delivered = validate_model_delivery(calls, lines(fixture["session"].encode()),
        thread="synthetic-v3-coverage", cwd="/synthetic", final_text=messages[-1]["text"])
    return brief_coverage(calls, files, upper=messages[-1]["event_index"], delivered_ids=delivered)


def resync(events):
    for e in events:
        result = e.get("item", {}).get("result")
        if result:
            result["content"][0]["text"] = json.dumps(result["structured_content"])


@pytest.mark.parametrize("form", ["direct", "single", "chain", "mixed"])
def test_complete_fourteen_obligations_in_all_forms(files, form):
    events = capture(files, form)
    report = assess(events, files, model_session(events, custom=True))
    assert len(report["obligations"]) == 14 and report["model_delivery"] == "PASS"
    forms = {r["form"] for r in report["obligations"]}
    assert forms == ({"paragraph"} if form == "direct" else {"paragraph", "section"} if form == "mixed" else {"section"})


def test_p9_both_sides_and_every_read_quote_causally_required(files, record_property):
    original = capture(files, "direct")
    assert assess(original, files)["raw_coverage_passed"]
    cases = []
    controls = [(side, 9, None) for side in (0, 1, "both")]
    controls += [(side, index, tool) for side in (0, 1) for index in SELECTED
                 for tool in ("inspect_document", "verify_quote")]
    for side, index, tool in controls:
        paths = {p for p, _ in files} if side == "both" else {files[side][0]}
        def omit(e):
            i = e.get("item", {})
            args = i.get("arguments", {})
            ref = args.get("anchor", args.get("selection", {}).get("paragraph_ref", {}))
            return args.get("path") in paths and ref.get("paragraph_index") == index and (tool is None or i.get("tool") == tool)
        events = [e for e in original if not omit(e)]
        with pytest.raises(base.checker.EvidenceError, match="selected-issue brief lacks full verified evidence") as caught:
            assess(events, files)
        assert assess(original, files)["raw_coverage_passed"]
        cases.append(dict(side=side, index=index, omitted=tool, cause=str(caught.value)))
    record_property("causal_fourteen_obligations", json.dumps(cases))


@pytest.mark.parametrize("fault", ["wrong_hash", "wrong_side", "wrong_file_ref", "quote_before_read", "after_final"])
def test_quote_identity_and_timing(files, fault):
    original = capture(files, "direct")
    assert assess(original, files)["raw_coverage_passed"]
    events = deepcopy(original)
    quote = next(e["item"]["id"] for e in events if e.get("item", {}).get("tool") == "verify_quote")
    pair = [e for e in events if e.get("item", {}).get("id") == quote]
    if fault in {"quote_before_read", "after_final"}:
        events = [e for e in events if e.get("item", {}).get("id") != quote]
        at = 2 if fault == "quote_before_read" else len(events)-1
        events[at:at] = pair
    else:
        for e in pair:
            i = e["item"]
            if fault == "wrong_file_ref":
                i["arguments"]["path"] = files[1][0]
            if i.get("result"):
                p = i["result"]["structured_content"]
                if fault == "wrong_hash":
                    p["checked_anchor"]["file_sha256"] = "0"*64
                elif fault == "wrong_side":
                    p["matches"][0]["side"] = "old"
        resync(events)
    cause = {"after_final": "no result message after native work",
             "wrong_side": "native result violates public contract"}.get(fault, "selected-issue brief lacks full verified evidence")
    with pytest.raises(base.checker.EvidenceError, match=cause):
        assess(events, files)
    assert assess(original, files)["raw_coverage_passed"]


@pytest.mark.parametrize("fault", ["missing_page", "offset", "count", "terminal", "cross_file", "selection", "reordered"])
def test_section_chain_requires_every_exact_page(files, fault):
    original = capture(files, "chain")
    assert assess(original, files)["raw_coverage_passed"]
    events = deepcopy(original)
    pages = [e for e in events if e["type"] == "item.completed" and e.get("item", {}).get("arguments", {}).get("mode") == "read"]
    page = pages[1]["item"]
    if fault in {"missing_page", "reordered"}:
        pair = [e for e in events if e.get("item", {}).get("id") == page["id"]]
        events = [e for e in events if e.get("item", {}).get("id") != page["id"]]
        if fault == "reordered":
            events[2:2] = pair
    elif fault == "selection":
        for e in events:
            if e.get("item", {}).get("id") == page["id"]:
                e["item"]["arguments"]["selection"]["section_ref"]["file_sha256"] = "0"*64
    else:
        p = page["result"]["structured_content"]
        if fault == "offset":
            p["coverage"]["cursor_offset"] += 1
        elif fault == "count":
            p["coverage"]["eligible_item_count"] += 1
        elif fault == "terminal":
            p["coverage"]["output_truncated"] = False
            p["next_cursor"] = None
        elif fault == "cross_file":
            p["file_sha256"] = base.checker._file_sha256(files[1][0])
    resync(events)
    with pytest.raises(base.checker.EvidenceError, match="selected-issue brief lacks full verified evidence"):
        assess(events, files)
    assert assess(original, files)["raw_coverage_passed"]


@pytest.mark.parametrize("form", ["direct", "chain"])
@pytest.mark.parametrize("fault", ["clipped", "metadata_only", "wrong_session", "missing_call"])
def test_complete_raw_does_not_prove_model_delivery(files, form, fault):
    events = capture(files, form)
    session = model_session(events)
    assert assess(events, files, session)["model_delivery"] == "PASS"
    bad = deepcopy(session)
    _, calls, _ = parsed(events)
    required = next(c for c in calls if c["tool"] == "inspect_document" and c["arguments"].get("mode") == "read")
    row = next(r["payload"] for r in bad if r.get("payload", {}).get("type") == "function_call_output"
               and r["payload"]["call_id"] == required["id"])
    if fault == "clipped":
        row["output"] = 'Warning: 1234 tokens truncated'
    elif fault == "metadata_only":
        row["output"] = json.dumps(dict(sha256=base.checker._digest(required["payload"]), complete=True))
    elif fault == "wrong_session":
        bad[0]["payload"]["id"] = "other"
    else:
        bad = [r for r in bad if not (r.get("payload", {}).get("type") == "function_call"
                                     and r["payload"]["call_id"] == required["id"])]
    cause = {"wrong_session": "source prefix session/build", "missing_call": "corresponding call"}.get(fault, "brief model delivery incomplete")
    with pytest.raises(base.checker.EvidenceError, match=cause):
        assess(events, files, bad)
    assert assess(events, files, session)["model_delivery"] == "PASS"
