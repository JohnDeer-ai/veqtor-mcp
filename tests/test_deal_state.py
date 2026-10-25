# SPDX-License-Identifier: Apache-2.0
"""Role selection, frozen bytes, lost replies and original position compatibility."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import jsonschema
import pytest

from veqtor_docx.incoming_comparison import compare_incoming_version
from veqtor_docx.synthetic import generate_demo_rounds
from veqtor_docx.round_snapshots import SnapshotError
from veqtor_mcp import deal_state as d, positions as p, server
from veqtor_mcp._deal_state_contract import RESULT


def rid(n=1):
    return f"round_{n:032x}"


def request(n=1):
    return f"{n:032x}"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def docs(tmp_path):
    demo = tmp_path / "demo"
    generate_demo_rounds(demo)
    return sorted(demo.glob("*.docx"))


def start(path, n=1, previous=None):
    return {"action": "start_round", "round_id": rid(n), "previous_round_id": previous,
            "path": str(path), "file_sha256": sha(path)}


def send(path, n=1):
    return {"action": "record_sent", "round_id": rid(n), "path": str(path),
            "file_sha256": sha(path), "user_confirmed": True,
            "statement": "The user identifies these exact bytes as the file actually sent."}


def apply(folder, state, n, command):
    result = server.mutate_deal_state(str(folder), state["revision"] if state else None,
                                      request(n), command)
    jsonschema.validate(result, RESULT)
    return result


def store_path(folder):
    return folder / ".veqtor" / d.STORE_NAME


def test_read_absent_never_creates_state_or_relabels_existing_journal(tmp_path):
    observed = server.read_deal_state(str(tmp_path))
    assert observed["revision"] is None and observed["rounds"] == observed["documents"] == []
    assert not list(tmp_path.iterdir())
    side = tmp_path / ".veqtor"
    side.mkdir(mode=0o700)
    (side / "operations").mkdir(mode=0o700)
    assert server.read_deal_state(str(tmp_path))["revision"] is None
    assert list(side.iterdir()) == [side / "operations"]


def test_prepare_A_send_modified_B_next_session_uses_B_not_A(tmp_path, docs):
    incoming, prepared, actually_sent, reply = docs[:4]
    originals = {path: path.read_bytes() for path in docs}
    state = apply(tmp_path, None, 1, start(incoming))
    assert state["rounds"][0]["sent_basis"] is None
    assert state["rounds"][0]["sent_documents"] == []
    # Preparing a different file has no side effect on sent status.
    prepared.read_bytes()
    state = apply(tmp_path, state, 2, send(actually_sent))
    state = apply(tmp_path, state, 3, start(reply, 2, rid()))
    script = "import json,sys;from veqtor_mcp.server import read_deal_state;print(json.dumps(read_deal_state(sys.argv[1])))"
    fresh = json.loads(subprocess.check_output([sys.executable, "-c", script, str(tmp_path)], text=True))
    basis = fresh["rounds"][1]["sent_basis"]
    assert basis["file_sha256"] == sha(actually_sent) != sha(prepared)
    paths = {row["file_sha256"]: row["path"] for row in fresh["documents"]}
    comparison = compare_incoming_version(paths[basis["file_sha256"]], basis["file_sha256"],
                                          paths[sha(reply)], sha(reply))
    assert comparison["sources"]["sent"]["sha256"] == sha(actually_sent)
    assert fresh["rounds"] == state["rounds"]
    assert {path: path.read_bytes() for path in docs} == originals
    assert all(Path(path).stat().st_mode & 0o777 == 0o600 for path in paths.values())


def test_existing_round_basis_does_not_follow_later_recorded_send(tmp_path, docs):
    state = apply(tmp_path, None, 1, start(docs[0]))
    state = apply(tmp_path, state, 2, send(docs[1]))
    state = apply(tmp_path, state, 3, start(docs[2], 2, rid()))
    state = apply(tmp_path, state, 4, send(docs[3]))
    assert state["rounds"][1]["sent_basis"]["file_sha256"] == sha(docs[1])


def test_lost_reply_retry_is_same_request_even_if_source_disappears(tmp_path, docs):
    command = start(docs[0])
    state = apply(tmp_path, None, 1, command)
    before = store_path(tmp_path).read_bytes()
    docs[0].unlink()
    replay = apply(tmp_path, None, 1, command)
    assert replay["revision"] == state["revision"]
    assert store_path(tmp_path).read_bytes() == before
    changed = {**command, "previous_round_id": rid(99)}
    with pytest.raises(p.PositionError, match="request_id_conflict"):
        apply(tmp_path, None, 1, changed)


def test_lost_send_reply_does_not_duplicate_send_or_change_positions(tmp_path, docs):
    state = apply(tmp_path, None, 1, start(docs[0]))
    old = deepcopy(state)
    command = send(docs[1])
    state = apply(tmp_path, state, 2, command)
    replay = apply(tmp_path, old, 2, command)
    assert replay["revision"] == state["revision"]
    assert len(replay["rounds"][0]["sent_documents"]) == 1


@pytest.mark.parametrize("which", ["revision", "predecessor", "round_missing", "round_exists", "sha", "confirmation", "unknown"])
def test_refusal_does_not_change_committed_state_or_documents(tmp_path, docs, which):
    state = apply(tmp_path, None, 1, start(docs[0]))
    before = store_path(tmp_path).read_bytes()
    command = send(docs[1])
    base = state
    if which == "revision": base = None
    if which == "predecessor": command = start(docs[1], 2, rid())
    if which == "round_missing": command["round_id"] = rid(2)
    if which == "round_exists": command = start(docs[1])
    if which == "sha": command["file_sha256"] = "f" * 64
    if which == "confirmation": command["user_confirmed"] = False
    if which == "unknown": command["unexpected"] = True
    originals = {path: path.read_bytes() for path in docs}
    codes = {"revision": "revision_conflict", "predecessor": "sent_basis_missing",
             "round_missing": "round_missing", "round_exists": "round_exists",
             "sha": "file_sha256_mismatch", "confirmation": "invalid_request", "unknown": "invalid_request"}
    with pytest.raises((p.PositionError, SnapshotError), match=codes[which]):
        apply(tmp_path, base, 2, command)
    assert store_path(tmp_path).read_bytes() == before
    assert {path: path.read_bytes() for path in docs} == originals


def test_original_positions_are_referenced_without_copying_or_migration(tmp_path, docs):
    content = {"title": "Payment", "desired_outcome": "30 days", "fallback": "60 days",
        "fallback_conditions": "Only if advance payment is 30%", "rationale": None,
        "related_position_ids": [], "content_origin": "user_instruction",
        "business_decision": "pending", "sources": []}
    pos_id = "pos_" + "1" * 32
    positions = server.mutate_deal_positions(str(tmp_path), None,
        [{"op": "create", "position_id": pos_id, "content": content}])
    before = (tmp_path / ".veqtor" / p.STORE_NAME).read_bytes()
    state = apply(tmp_path, None, 1, start(docs[0]))
    basis = state["rounds"][0]["positions_basis"]
    assert basis["revision"] == positions["revision"] and basis["history_sequence"] == 1
    assert (tmp_path / ".veqtor" / p.STORE_NAME).read_bytes() == before
    later = server.mutate_deal_positions(str(tmp_path), positions["revision"],
        [{"op": "update", "position_id": pos_id, "expected_version": 1,
          "content": {**content, "desired_outcome": "45 days"}}])
    loaded = server.read_deal_state(str(tmp_path))
    assert loaded["positions_current_revision"] == later["revision"] != basis["revision"]
    assert loaded["rounds"][0]["positions_basis"] == basis
    assert loaded["positions_basis_observations"] == [{"round_id": rid(), "status": "historical_available"}]
    history = server.read_deal_positions(str(tmp_path), include_history=True)["history"]
    assert history[basis["history_sequence"] - 1]["position"]["content"]["fallback_conditions"] == content["fallback_conditions"]
    (tmp_path / ".veqtor" / p.STORE_NAME).unlink()
    assert server.read_deal_state(str(tmp_path))["positions_basis_observations"][0]["status"] == "unavailable"


def test_commit_uncertain_recovers_from_state_without_recapturing(tmp_path, docs, monkeypatch):
    original = p._publish
    def committed_then_lost(*args, **kwargs):
        original(*args, **kwargs)
        p._fail("commit_uncertain")
    command = start(docs[0])
    monkeypatch.setattr(p, "_publish", committed_then_lost)
    with pytest.raises(p.PositionError, match="commit_uncertain"):
        apply(tmp_path, None, 1, command)
    assert len(server.read_deal_state(str(tmp_path))["rounds"]) == 1
    apply(tmp_path, None, 1, command)


def test_precommit_failure_leaves_verified_orphan_reusable_but_no_applied_role(tmp_path, docs, monkeypatch):
    command = start(docs[0])
    def fail(*args, **kwargs): p._fail("storage_failure")
    with monkeypatch.context() as m:
        m.setattr(p, "_publish", fail)
        with pytest.raises(p.PositionError): apply(tmp_path, None, 1, command)
    assert server.read_deal_state(str(tmp_path))["rounds"] == []
    state = apply(tmp_path, None, 1, command)
    assert len(state["rounds"]) == len(state["documents"]) == 1


@pytest.mark.parametrize("kind", ["state_symlink", "snapshot_symlink", "hardlink", "mode", "corrupt", "snapshot_tamper", "missing_lock"])
def test_unsafe_or_corrupt_storage_refuses_and_is_preserved(tmp_path, docs, kind):
    state = apply(tmp_path, None, 1, start(docs[0]))
    target = store_path(tmp_path)
    snapshot = Path(state["documents"][0]["path"])
    if kind == "state_symlink":
        saved = tmp_path / "moved.json"; target.rename(saved); target.symlink_to(saved)
    if kind == "snapshot_symlink":
        saved = tmp_path / "moved.docx"; snapshot.rename(saved); snapshot.symlink_to(saved)
    if kind == "hardlink": os.link(target, tmp_path / "alias.json")
    if kind == "mode": target.chmod(0o644)
    if kind == "corrupt": target.write_text('{"schema_version":"unknown"}')
    if kind == "snapshot_tamper": snapshot.write_bytes(docs[1].read_bytes())
    if kind == "missing_lock": (target.parent / p.LOCK_NAME).unlink()
    before = target.read_bytes()
    with pytest.raises((p.PositionError, SnapshotError)): server.read_deal_state(str(tmp_path))
    assert target.read_bytes() == before


def test_output_validation_failure_precedes_role_commit(tmp_path, docs):
    def reject(_): raise ValueError("transport contract failed")
    with pytest.raises(ValueError):
        d.mutate_deal_state(str(tmp_path), None, request(), start(docs[0]), validate_result=reject)
    assert server.read_deal_state(str(tmp_path))["rounds"] == []


def test_round_bound_is_enforced_before_publishing_another_snapshot(tmp_path, docs):
    state = apply(tmp_path, None, 1, start(docs[0]))
    value = json.loads(store_path(tmp_path).read_text())
    row = value["rounds"][0]
    event = value["requests"][0]
    value["rounds"] = [{**row, "round_id": rid(n)} for n in range(1, 51)]
    value["requests"] = [{**event, "request_id": request(n), "round_id": rid(n)} for n in range(1, 51)]
    value["revision"] = p._revision(value)
    store_path(tmp_path).write_bytes(p._json_bytes(value))
    state = server.read_deal_state(str(tmp_path))
    before = store_path(tmp_path).read_bytes()
    with pytest.raises(p.PositionError, match="invalid_request"):
        apply(tmp_path, state, 51, start(docs[1], 51))
    assert store_path(tmp_path).read_bytes() == before
    assert len(list((tmp_path / ".veqtor" / d.DOCUMENTS).iterdir())) == 1


def test_request_order_cannot_retroactively_use_a_later_send(tmp_path, docs):
    state = apply(tmp_path, None, 1, start(docs[0]))
    state = apply(tmp_path, state, 2, send(docs[1]))
    state = apply(tmp_path, state, 3, start(docs[2], 2, rid()))
    state = apply(tmp_path, state, 4, send(docs[3]))
    value = json.loads(store_path(tmp_path).read_text())
    value["rounds"][1]["sent_basis"] = {"round_id": rid(), "request_id": request(4), "file_sha256": sha(docs[3])}
    value["revision"] = p._revision(value)
    store_path(tmp_path).write_bytes(p._json_bytes(value))
    with pytest.raises(p.PositionError, match="storage_corrupt"):
        server.read_deal_state(str(tmp_path))


def test_competing_round_creations_use_one_current_revision(tmp_path, docs):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier = Barrier(2)
    def compete(n):
        barrier.wait(timeout=5)
        try:
            return apply(tmp_path, None, n, start(docs[n - 1], n))
        except p.PositionError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, [1, 2]))
    assert sum(isinstance(row, dict) for row in results) == 1
    assert "revision_conflict" in results
    state = server.read_deal_state(str(tmp_path))
    assert len(state["rounds"]) == len(state["requests"]) == len(state["documents"]) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_role_tools_over_MCP_and_safe_invalid_command(tmp_path, docs, mode):
    from mcp import Client
    def payload(result):
        return result.structured_content
    async with Client(server.mcp, mode=mode) as client:
        arguments = {"folder": str(tmp_path), "expected_revision": None,
                     "request_id": request(), "command": start(docs[0])}
        saved = payload(await client.call_tool("mutate_deal_state", arguments))
        assert saved["rounds"][0]["incoming_sha256"] == sha(docs[0])
        loaded = payload(await client.call_tool("read_deal_state", {"folder": str(tmp_path)}))
        assert loaded["revision"] == saved["revision"]
        malformed = {**arguments, "command": {**arguments["command"], "secret_private_field": "never disclose"}}
        response = await client.call_tool("mutate_deal_state", malformed)
        assert response.is_error
        wire = str(response)
        assert "never disclose" not in wire and "secret_private_field" not in wire
