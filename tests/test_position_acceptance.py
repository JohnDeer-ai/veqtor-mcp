# SPDX-License-Identifier: Apache-2.0
"""Real synthetic store results in synthetic envelopes: checker tests, not native proof."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys

import pytest

from veqtor_docx import inspect_document
from veqtor_docx.synthetic import generate_demo_rounds
from veqtor_mcp import positions, server

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import check_position_acceptance as checker  # noqa: E402


def content(**changes):
    return dict(title="Synthetic payment", desired_outcome="30 days", fallback=None,
        fallback_conditions=None, rationale=None, related_position_ids=[], content_origin="user_instruction",
        business_decision="not_required", sources=[]) | changes


def build(tmp_path, monkeypatch):
    original = tmp_path / "matter"
    files = generate_demo_rounds(original, profile="paragraph-edits")
    first = files[0]
    ref = inspect_document(str(first), "browse")["paragraphs"][0]["paragraph_ref"]
    binding = dict(path=first.name, file_sha256=hashlib.sha256(first.read_bytes()).hexdigest(), reference=ref)
    ids = [f"pos_{i:032x}" for i in range(1, 6)]
    contents = [content(sources=[binding]), content(fallback="60 days", fallback_conditions="If secured"),
                content(related_position_ids=[ids[1]]), content(business_decision="pending"),
                content(content_origin="model_proposal")]
    initial = [dict(position_id=pid, version=1, content=con, confirmation=None, lifecycle="active")
               for pid, con in zip(ids, contents)]
    folders = {name: str(tmp_path / name) for name in ("original", "moved", "conflict", "first", "other")}
    folders["original"] = str(original)
    for name in ("first", "other"):
        Path(folders[name]).mkdir()
    for source in files:
        shutil.copy2(source, Path(folders["first"]) / source.name)
    expected = {"schema_version": checker.BASELINE_SCHEMA, "producer": server._producer(), "folders": folders,
        "initial_positions": deepcopy(initial), "updated_content": content(desired_outcome="45 days"),
        "confirmation_statement": "User explicitly confirmed these exact version 1 positions.",
        "conflict_contents": [content(desired_outcome="A: 60 days"), content(desired_outcome="B: 75 days")],
        "source_files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}, "selected_current_file": files[1].name}
    # The baseline exists independently before any position mutation.
    checker.baseline(expected)
    events, sessions = {}, {}

    def session(name):
        events[name] = [{"type": "thread.started", "thread_id": "synthetic-" + name}, {"type": "turn.started"}]
        sessions[name] = f"{len(sessions) + 1:032x}"

    def call(name, tool, **args):
        monkeypatch.setattr(positions, "SERVER_SESSION_ID", sessions[name])
        seq = events[name]
        identity = dict(id=str(len(seq)), type="mcp_tool_call", server="veqtor_nr02", tool=tool, arguments=deepcopy(args))
        seq.append({"type": "item.started", "item": dict(**deepcopy(identity), status="in_progress", result=None, error=None)})
        try:
            out = getattr(server, tool)(**args)
            result = dict(structured_content=deepcopy(out), content=[dict(type="text", text=json.dumps(out))])
        except positions.PositionError as exc:
            out = None
            result = dict(is_error=True, content=[dict(type="text", text=str(exc))])
        seq.append({"type": "item.completed", "item": dict(**identity, status="completed", result=result, error=None)})
        return out

    def read(name, folder):
        return call(name, "read_deal_positions", folder=folder, include_history=True, check_sources=True)

    def write(name, folder, rev, ops):
        return call(name, "mutate_deal_positions", folder=folder, expected_revision=rev, operations=ops)

    creates = [dict(op="create", position_id=pid, content=con) for pid, con in zip(ids, contents)]
    session("save")
    read("save", folders["original"])
    rev = write("save", folders["original"], None, creates)["revision"]
    read("save", folders["original"])
    for name, ops in [("confirm", [dict(op="confirm", position_id=ids[i], expected_version=1, user_confirmed=True,
          statement=expected["confirmation_statement"]) for i in (0, 3)]),
        ("update", [dict(op="update", position_id=ids[0], expected_version=1, content=expected["updated_content"])])]:
        session(name)
        rev = write(name, folders["original"], rev, ops)["revision"]
        read(name, folders["original"])
    session("restart")
    read("restart", folders["original"])
    session("withdraw")
    rev = write("withdraw", folders["original"], rev, [dict(op="withdraw", position_id=ids[4], expected_version=1)])["revision"]
    read("withdraw", folders["original"])
    shutil.copytree(original, folders["conflict"])
    original.rename(folders["moved"])
    session("moved")
    read("moved", folders["moved"])
    # Explicit synthetic source setup outside native tool actions.
    moved_source = Path(folders["moved"]) / first.name
    moved_source.write_bytes(b"NR-02 synthetic modified source")
    session("changed")
    read("changed", folders["moved"])
    moved_source.unlink()
    session("missing")
    read("missing", folders["moved"])
    moved_rev = rev
    for version, name in [(1, "journal_disabled"), (2, "journal_corrupt")]:
        session(name)
        if name == "journal_corrupt":
            (Path(folders["moved"]) / ".veqtor" / "decision-records.jsonl").write_bytes(b"NR-02 synthetic corrupt journal\n")
        monkeypatch.setenv("VEQTOR_DISABLE_DECISION_RECORD", "1" if name == "journal_disabled" else "0")
        moved_rev = write(name, folders["moved"], moved_rev,
            [dict(op="update", position_id=ids[2], expected_version=version, content=contents[2])])["revision"]
        read(name, folders["moved"])
    session("other")
    read("other", folders["other"])
    for name in ("conflict_a", "conflict_b"):
        session(name)
        read(name, folders["conflict"])
    conflict_ops = [[dict(op="update", position_id=ids[1], expected_version=1, content=c)] for c in expected["conflict_contents"]]
    for name, ops in zip(("conflict_a", "conflict_b"), conflict_ops):
        write(name, folders["conflict"], rev, ops)
    session("conflict_final")
    read("conflict_final", folders["conflict"])
    session("copy_independent")
    read("copy_independent", folders["moved"])
    session("retry")
    write("retry", folders["conflict"], rev, conflict_ops[0])
    read("retry", folders["conflict"])
    for name in ("first_a", "first_b"):
        session(name)
        read(name, folders["first"])
    for i, name in enumerate(("first_a", "first_b")):
        ops = deepcopy(creates)
        ops[1]["content"] = expected["conflict_contents"][i]
        write(name, folders["first"], None, ops)
    session("first_final")
    read("first_final", folders["first"])
    for seq in events.values():
        seq.append({"type": "turn.completed"})
    return events, expected


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    return build(tmp_path, monkeypatch)


def payloads(events, name, tool="read_deal_positions"):
    return [e["item"] for e in events[name] if e["type"] == "item.completed" and e["item"].get("tool") == tool]


def resync(item):
    item["result"]["content"][0]["text"] = json.dumps(item["result"]["structured_content"])


def test_positive_actual_payloads_with_predeclared_values(evidence):
    events, expected = evidence
    assert checker.validate_evidence(events, expected)["status"] == "passed"


@pytest.mark.parametrize("name", sorted(checker.SESSIONS))
def test_missing_required_native_session_fails(evidence, name):
    events, expected = evidence
    del events[name]
    with pytest.raises(checker.EvidenceError):
        checker.validate_evidence(events, expected)


@pytest.mark.parametrize("name", ["save", "confirm", "update", "restart", "withdraw", "moved", "conflict_final", "first_final", "journal_disabled", "journal_corrupt"])
@pytest.mark.parametrize("mutation", ["missing_structured", "missing_history", "partial_text", "wrong_source", "wrong_revision", "wrong_producer"])
def test_plausibly_weaker_readback_cannot_pass(evidence, name, mutation):
    events, expected = evidence
    item = payloads(events, name)[-1]
    payload = item["result"]["structured_content"]
    if mutation == "missing_structured":
        del item["result"]["structured_content"]
    else:
        if mutation == "missing_history":
            payload["history"] = []
        elif mutation == "partial_text":
            payload["positions"][0]["content"]["desired_outcome"] = "summary"
        elif mutation == "wrong_source":
            payload["source_observations"] = []
        elif mutation == "wrong_revision":
            payload["revision"] = "f" * 64
        else:
            payload["producer"]["build"] = "source-snapshot-v1-sha256:" + "f" * 64
        resync(item)
    with pytest.raises(checker.EvidenceError):
        checker.validate_evidence(events, expected)


@pytest.mark.parametrize("kind", ["client", "server", "prompt_only", "wrong_matter", "read_before_save", "prose_conflict", "first_already_initialized", "lost_confirmation", "false_current", "partial_batch"])
def test_cross_session_and_causal_weak_substitutes_fail(evidence, kind):
    events, expected = evidence
    if kind == "client":
        events["restart"][0]["thread_id"] = events["save"][0]["thread_id"]
    elif kind == "server":
        item = payloads(events, "restart")[0]
        item["result"]["structured_content"]["server_session_id"] = payloads(events, "save")[0]["result"]["structured_content"]["server_session_id"]
        resync(item)
    elif kind == "prompt_only":
        events["restart"] = [events["restart"][0], {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "All five recovered exactly"}}, {"type": "turn.completed"}]
    elif kind == "wrong_matter":
        for event in events["restart"]:
            if event.get("item", {}).get("tool"):
                event["item"]["arguments"]["folder"] = expected["folders"]["other"]
    elif kind == "read_before_save":
        seq = events["save"]
        seq[4:8] = seq[6:8] + seq[4:6]
    elif kind == "prose_conflict":
        item = payloads(events, "conflict_b", "mutate_deal_positions")[0]
        item["result"] = {"content": [{"type": "text", "text": "The revision conflict occurred"}]}
    elif kind == "first_already_initialized":
        item = payloads(events, "first_b")[0]
        item["result"]["structured_content"]["state"] = "initialized"
        resync(item)
    elif kind == "lost_confirmation":
        item = payloads(events, "restart")[0]
        item["result"]["structured_content"]["history"][5]["position"]["confirmation"] = None
        resync(item)
    elif kind == "false_current":
        item = payloads(events, "changed")[0]
        item["result"]["structured_content"]["source_observations"][0]["status"] = "same_bytes"
        resync(item)
    else:
        for event in events["save"]:
            if event.get("item", {}).get("tool") == "mutate_deal_positions":
                event["item"]["arguments"]["operations"].pop()
    with pytest.raises(checker.EvidenceError):
        checker.validate_evidence(events, expected)
