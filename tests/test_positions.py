# SPDX-License-Identifier: Apache-2.0
"""Synthetic NR-02 storage/transport checks; not native Codex acceptance."""
from copy import deepcopy
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys

import jsonschema
import pytest

from veqtor_docx.synthetic import generate_demo_rounds
from veqtor_docx import inspect_document, extract_redlines
from veqtor_mcp import positions as p, server, records
from veqtor_mcp._positions_contract import RESULT


def pid(index=1):
    return f"pos_{index:032x}"


def content(**changes):
    return {"title": "Payment", "desired_outcome": "Pay within 30 days.", "fallback": None,
            "fallback_conditions": None, "rationale": None, "related_position_ids": [],
            "content_origin": "model_proposal", "business_decision": "not_required", "sources": [], **changes}


def create(index=1, **changes):
    return {"op": "create", "position_id": pid(index), "content": content(**changes)}


def mutate(folder, revision, *ops):
    return server.mutate_deal_positions(str(folder), revision, list(ops))


def read(folder, **kwargs):
    return server.read_deal_positions(str(folder), **kwargs)


def snapshot(folder):
    return folder / ".veqtor" / p.STORE_NAME


def test_uninitialized_read_has_no_side_effects_even_with_existing_journal(tmp_path):
    for history in (False, True):
        result = read(tmp_path, include_history=history, check_sources=True)
        jsonschema.validate(result, RESULT)
        assert result["state"] == "uninitialized" and result["revision"] is None
        assert result["positions"] == result["history"] == []
        assert list(tmp_path.iterdir()) == []
    side = tmp_path / ".veqtor"
    side.mkdir(mode=0o700)
    journal = side / "decision-records.jsonl"
    journal.write_text("corrupt journal")
    before = {str(path): path.read_bytes() for path in side.iterdir()}
    read(tmp_path)
    assert {str(path): path.read_bytes() for path in side.iterdir()} == before


def test_five_positions_confirmation_reset_history_withdraw_and_matter_isolation(tmp_path):
    saved = mutate(tmp_path, None, create(),
                   create(2, fallback="60 days", fallback_conditions="Only if secured.", related_position_ids=[pid()]),
                   create(3, related_position_ids=[pid(2)]),
                   create(4, business_decision="pending", content_origin="user_instruction"), create(5))
    assert all(row["confirmation"] is None for row in saved["positions"])
    confirmed = mutate(tmp_path, saved["revision"],
        {"op": "confirm", "position_id": pid(4), "expected_version": 1, "user_confirmed": True,
         "statement": "The user explicitly confirmed the displayed fourth position, version 1."})
    row = confirmed["positions"][3]
    assert row["confirmation"]["version"] == 1 and row["content"]["business_decision"] == "pending"
    updated = mutate(tmp_path, confirmed["revision"],
        {"op": "update", "position_id": pid(4), "expected_version": 1, "content": content(desired_outcome="45 days")})
    assert updated["positions"][3]["confirmation"] is None and updated["positions"][3]["version"] == 2
    withdrawn = mutate(tmp_path, updated["revision"], {"op": "withdraw", "position_id": pid(4), "expected_version": 2})
    recovered = read(tmp_path, include_history=True)
    assert recovered["positions"] == withdrawn["positions"] and len(recovered["history"]) == 8
    assert recovered["history"][5]["position"] == row
    assert recovered["history"][6]["position"]["confirmation"] is None
    other = tmp_path / "other"
    other.mkdir()
    assert read(other)["state"] == "uninitialized"
    assert mutate(other, None, create())["matter_id"] != saved["matter_id"]


@pytest.mark.parametrize("change", ["desired_outcome", "fallback", "fallback_conditions", "rationale", "title", "related_position_ids", "business_decision", "content_origin", "sources"])
def test_every_content_update_resets_confirmation(tmp_path, change):
    saved = mutate(tmp_path, None, create(fallback="60 days"), create(2))
    confirmed = mutate(tmp_path, saved["revision"], {"op": "confirm", "position_id": pid(), "expected_version": 1,
                       "user_confirmed": True, "statement": "Confirmed exact version 1."})
    updated_content = deepcopy(confirmed["positions"][0]["content"])
    updated_content[change] = {"related_position_ids": [pid(2)], "business_decision": "pending",
                               "content_origin": "user_instruction", "sources": []}.get(change, "New value")
    result = mutate(tmp_path, confirmed["revision"], {"op": "update", "position_id": pid(),
                      "expected_version": 1, "content": updated_content})
    assert result["positions"][0]["confirmation"] is None
    assert result["positions"][0]["version"] == 2


@pytest.mark.parametrize("bad", [
    {"op": "confirm", "position_id": pid(), "expected_version": 2, "user_confirmed": True, "statement": "yes"},
    {"op": "confirm", "position_id": pid(), "expected_version": 1, "user_confirmed": False, "statement": "yes"},
    {"op": "confirm", "position_id": pid(), "expected_version": 1, "user_confirmed": True, "statement": " "},
    {"op": "update", "position_id": pid(), "expected_version": 1, "content": content(), "confirmation": True},
    create(2, related_position_ids=[pid(99)]), create(2, related_position_ids=[pid(2)]),
    create(2, fallback_conditions="without fallback"), create(2, desired_outcome="x" * 4001),
    create(2, sources=[{"path": "../outside.docx", "file_sha256": "a" * 64, "reference": None}]),
])
def test_invalid_batch_has_no_partial_effects(tmp_path, bad):
    saved = mutate(tmp_path, None, create())
    before = snapshot(tmp_path).read_bytes()
    with pytest.raises(p.PositionError):
        mutate(tmp_path, saved["revision"], create(3), bad)
    assert snapshot(tmp_path).read_bytes() == before


def test_repeat_stale_and_withdrawn_refusals(tmp_path):
    first = mutate(tmp_path, None, create())
    with pytest.raises(p.PositionError, match="revision_conflict"):
        mutate(tmp_path, None, create())
    confirmed = mutate(tmp_path, first["revision"], {"op": "confirm", "position_id": pid(), "expected_version": 1,
                       "user_confirmed": True, "statement": "yes"})
    with pytest.raises(p.PositionError, match="position_conflict"):
        mutate(tmp_path, confirmed["revision"], {"op": "confirm", "position_id": pid(), "expected_version": 1,
                       "user_confirmed": True, "statement": "yes"})
    withdrawn = mutate(tmp_path, confirmed["revision"], {"op": "withdraw", "position_id": pid(), "expected_version": 1})
    for op in ({"op": "withdraw", "position_id": pid(), "expected_version": 1},
               {"op": "update", "position_id": pid(), "expected_version": 1, "content": content()}):
        with pytest.raises(p.PositionError, match="position_conflict"):
            mutate(tmp_path, withdrawn["revision"], op)


@pytest.mark.parametrize("disabled", [False, True])
def test_provenance_is_independent(tmp_path, monkeypatch, disabled):
    monkeypatch.setenv("VEQTOR_DISABLE_DECISION_RECORD", "1" if disabled else "0")
    side = tmp_path / ".veqtor"
    side.mkdir(mode=0o700)
    journal = side / "decision-records.jsonl"
    journal.write_bytes(b"corrupt private journal")
    monkeypatch.setattr(records, "write_record", lambda **_: pytest.fail("NR-02 must not journal"))
    saved = mutate(tmp_path, None, create())
    assert read(tmp_path)["positions"] == saved["positions"]
    assert journal.read_bytes() == b"corrupt private journal"
    assert saved["record_status"] == "disabled"
    assert (side / ".gitignore").read_bytes() == b"*\n"
    for path in side.iterdir():
        if path != journal:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_sources_exact_references_status_and_portability(tmp_path):
    matter = tmp_path / "synthetic-matter"
    files = generate_demo_rounds(matter, profile="paragraph-edits")
    source = files[0]
    ref = inspect_document(str(source), "browse")["paragraphs"][0]["paragraph_ref"]
    binding = {"path": source.name, "file_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "reference": ref}
    saved = mutate(matter, None, create(sources=[binding]))
    assert saved["source_observations"][0]["status"] == "not_checked"
    assert read(matter, check_sources=True)["source_observations"][0]["status"] == "same_bytes"
    # Explicit synthetic scenario setup: move the whole matter and make a separate copy.
    moved = tmp_path / "moved"
    matter.rename(moved)
    clone = tmp_path / "clone"
    shutil.copytree(moved, clone)
    assert read(moved, include_history=True)["positions"] == saved["positions"]
    assert read(moved, check_sources=True)["source_observations"][0]["status"] == "same_bytes"
    changed = mutate(clone, saved["revision"], create(2))
    assert read(moved)["revision"] == saved["revision"] != changed["revision"]
    # Explicit synthetic source setup, never a product write to DOCX.
    (moved / source.name).write_bytes(b"changed synthetic source")
    assert read(moved, check_sources=True)["source_observations"][0]["status"] == "changed"
    (moved / source.name).unlink()
    assert read(moved, check_sources=True)["source_observations"][0]["status"] == "unavailable"
    assert read(moved)["positions"] == saved["positions"]


def test_sources_accept_exact_change_units_and_reject_weak_binding(tmp_path):
    files = generate_demo_rounds(tmp_path / "synthetic", profile="paragraph-edits")
    source = files[1]
    anchor = extract_redlines(str(source))["change_units"][0]["anchor"]
    binding = {"path": source.name, "file_sha256": anchor["file_sha256"], "reference": anchor}
    first = mutate(source.parent, None, create(sources=[binding]))
    before = snapshot(source.parent).read_bytes()
    for broken in [{**binding, "file_sha256": "f" * 64},
                   {**binding, "reference": {**anchor, "unit_fingerprint_sha256": "f" * 64}},
                   {**binding, "reference": {"file_sha256": anchor["file_sha256"], "change_unit_id": anchor["change_unit_id"]}}]:
        with pytest.raises(p.PositionError):
            mutate(source.parent, first["revision"], create(2, sources=[broken]))
        assert snapshot(source.parent).read_bytes() == before


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "public_permissions"])
@pytest.mark.parametrize("name", [p.STORE_NAME, p.LOCK_NAME])
def test_unsafe_store_and_lock_refuse(tmp_path, kind, name):
    saved = mutate(tmp_path, None, create())
    target = tmp_path / ".veqtor" / name
    original = target.read_bytes()
    target.unlink()
    alternate = tmp_path / "alternate"
    alternate.write_bytes(original)
    alternate.chmod(0o600)
    if kind == "symlink":
        target.symlink_to(alternate)
    elif kind == "hardlink":
        os.link(alternate, target)
    elif kind == "fifo":
        os.mkfifo(target, 0o600)
    else:
        target.write_bytes(original)
        target.chmod(0o644)
    with pytest.raises(p.PositionError):
        mutate(tmp_path, saved["revision"], create(2))
    assert alternate.read_bytes() == original


@pytest.mark.parametrize("corruption", ["json", "duplicate", "version", "digest", "history", "current", "extra"])
def test_store_corruption_never_reinitialized(tmp_path, corruption):
    saved = mutate(tmp_path, None, create())
    store = json.loads(snapshot(tmp_path).read_bytes())
    if corruption == "json":
        raw = b"broken"
    elif corruption == "duplicate":
        raw = b'{"schema_version":"deal_positions_store.v1","schema_version":"deal_positions_store.v1"}'
    else:
        if corruption == "version":
            store["schema_version"] = "deal_positions_store.v999"
        if corruption == "digest":
            store["revision"] = "0" * 64
        if corruption == "history":
            store["history"][0]["position"]["version"] = 2
        if corruption == "current":
            store["positions"][0]["content"]["title"] = "unrecorded change"
        if corruption == "extra":
            store["extra"] = True
        if corruption != "digest":
            store["revision"] = p._revision(store)
        raw = json.dumps(store).encode()
    snapshot(tmp_path).write_bytes(raw)
    for call in (lambda: read(tmp_path), lambda: mutate(tmp_path, saved["revision"], create(2))):
        with pytest.raises(p.PositionError):
            call()
    assert snapshot(tmp_path).read_bytes() == raw


@pytest.mark.parametrize("phase", ["write", "file_fsync", "replace", "after_replace", "directory_fsync", "output_validation"])
def test_commit_point_and_unknown_outcome(tmp_path, monkeypatch, phase):
    saved = mutate(tmp_path, None, create())
    before = snapshot(tmp_path).read_bytes()
    real_replace, real_sync = os.replace, os.fsync
    replaced = False

    def replacement(*args, **kwargs):
        nonlocal replaced
        if phase == "replace":
            raise OSError("private sentinel")
        result = real_replace(*args, **kwargs)
        replaced = True
        if phase == "after_replace":
            raise OSError("private sentinel")
        return result

    def sync(fd):
        if phase == "file_fsync" and stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("private sentinel")
        if phase == "directory_fsync" and replaced:
            raise OSError("private sentinel")
        return real_sync(fd)

    def fail(*args, **kwargs):
        raise OSError("private sentinel")

    with monkeypatch.context() as m:
        m.setattr(os, "replace", replacement)
        m.setattr(os, "fsync", sync)
        if phase == "write":
            m.setattr(p, "_write_all", fail)
        if phase == "output_validation":
            m.setattr(server, "_position_result", fail)
        expected = "commit_uncertain" if phase in {"replace", "after_replace", "directory_fsync"} else "storage_failure"
        with pytest.raises(p.PositionError, match=expected):
            mutate(tmp_path, saved["revision"], create(2))
    if phase in {"after_replace", "directory_fsync"}:
        assert len(read(tmp_path)["positions"]) == 2
        with pytest.raises(p.PositionError, match="revision_conflict"):
            mutate(tmp_path, saved["revision"], create(2))
    else:
        assert snapshot(tmp_path).read_bytes() == before


WORKER = '''
import json, os, sys, time
from pathlib import Path
from veqtor_mcp import server
from veqtor_mcp.positions import PositionError
folder, revision, index, ready, go = sys.argv[1:]
observed = server.read_deal_positions(folder)
Path(ready + '.tmp').write_text(json.dumps(observed))
os.replace(ready + '.tmp', ready)
while not Path(go).exists(): time.sleep(.01)
try:
    content = dict(title='Worker', desired_outcome='Independent concurrent write', fallback=None,
                   fallback_conditions=None, rationale=None, related_position_ids=[],
                   content_origin='model_proposal', business_decision='not_required', sources=[])
    result = server.mutate_deal_positions(folder, json.loads(revision),
        [dict(op='create', position_id=f'pos_{int(index):032x}', content=content)])
    print(json.dumps(dict(status='ok', result=result)))
except PositionError as exc: print(json.dumps(dict(status=exc.code)))
'''


@pytest.mark.parametrize("initialized", [False, True])
@pytest.mark.parametrize("trial", range(3))
def test_actual_process_conflicts_including_first_creation_and_restart(tmp_path, initialized, trial):
    matter = tmp_path / "matter"
    matter.mkdir()
    revision = mutate(matter, None, create())["revision"] if initialized else None
    go = tmp_path / "go"
    processes = []
    for index in (2, 3):
        ready = tmp_path / str(index)
        process = subprocess.Popen([sys.executable, "-c", WORKER, str(matter), json.dumps(revision),
                                    str(index), str(ready), str(go)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append((process, ready))
    import time
    deadline = time.monotonic() + 20
    while not all(path.exists() for _, path in processes):
        assert time.monotonic() < deadline
        time.sleep(.01)
    for _, path in processes:
        assert json.loads(path.read_text())["revision"] == revision
    go.touch()
    outcomes = []
    for process, _ in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        outcomes.append(json.loads(stdout))
    assert sorted(row["status"] for row in outcomes) == ["ok", "revision_conflict"]
    winner = next(row["result"] for row in outcomes if row["status"] == "ok")
    assert read(matter)["positions"] == winner["positions"]
    code = "import json,sys; from veqtor_mcp.server import read_deal_positions; print(json.dumps(read_deal_positions(sys.argv[1], True)))"
    recovered = json.loads(subprocess.check_output([sys.executable, "-c", code, str(matter)], text=True))
    assert recovered["positions"] == winner["positions"] and recovered["revision"] == winner["revision"]
    assert recovered["server_session_id"] != winner["server_session_id"] != read(matter)["server_session_id"]


def test_lock_timeout_and_workspace_switch(tmp_path, monkeypatch):
    import fcntl
    first = mutate(tmp_path, None, create())
    with (tmp_path / ".veqtor" / p.LOCK_NAME).open() as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        monkeypatch.setattr(p, "LOCK_SECONDS", .02)
        with pytest.raises(p.PositionError, match="lock_timeout"):
            mutate(tmp_path, first["revision"], create(2))
    real_write = p._write_all
    switched = False
    moved = tmp_path.with_name(tmp_path.name + "-moved")

    def switch(fd, payload):
        nonlocal switched
        real_write(fd, payload)
        if not switched:
            tmp_path.rename(moved)
            tmp_path.mkdir()
            switched = True

    monkeypatch.setattr(p, "_write_all", switch)
    with pytest.raises(p.PositionError, match="workspace_changed"):
        mutate(tmp_path, first["revision"], create(2))
    assert read(moved)["revision"] == first["revision"]
    assert read(tmp_path)["state"] == "uninitialized"


def test_json_and_count_limits_refuse_without_loss(tmp_path, monkeypatch):
    first = mutate(tmp_path, None, create())
    before = snapshot(tmp_path).read_bytes()
    with pytest.raises(p.PositionError):
        mutate(tmp_path, first["revision"], *[create(i) for i in range(2, 23)])
    nested = []
    for _ in range(22):
        nested = [nested]
    with pytest.raises(p.PositionError, match="resource_limit_exceeded"):
        mutate(tmp_path, first["revision"], nested)
    monkeypatch.setattr(p, "MAX_NODES", 10)
    with pytest.raises(p.PositionError, match="resource_limit_exceeded"):
        mutate(tmp_path, first["revision"], create(2))
    assert snapshot(tmp_path).read_bytes() == before


def test_position_and_history_capacity_and_full_boundary_read(tmp_path):
    current = mutate(tmp_path, None, *[create(i) for i in range(1, 21)])
    current = mutate(tmp_path, current["revision"], *[create(i) for i in range(21, 41)])
    current = mutate(tmp_path, current["revision"], *[create(i) for i in range(41, 51)])
    before = snapshot(tmp_path).read_bytes()
    with pytest.raises(p.PositionError, match="resource_limit_exceeded"):
        mutate(tmp_path, current["revision"], create(51))
    assert snapshot(tmp_path).read_bytes() == before
    # Explicit synthetic near-capacity snapshot setup; no product pruning.
    store = json.loads(before)
    row = deepcopy(store["positions"][0])
    while len(store["history"]) < 499:
        row["version"] += 1
        store["history"].append(dict(sequence=len(store["history"]) + 1, operation="update", position=deepcopy(row)))
    store["positions"][0] = row
    store["revision"] = p._revision(store)
    snapshot(tmp_path).write_bytes(p._json_bytes(store))
    confirmed = mutate(tmp_path, store["revision"], dict(op="confirm", position_id=row["position_id"],
        expected_version=row["version"], user_confirmed=True, statement="Exact version confirmed"))
    full = read(tmp_path, include_history=True)
    assert len(full["positions"]) == 50 and len(full["history"]) == 500
    before = snapshot(tmp_path).read_bytes()
    with pytest.raises(p.PositionError, match="resource_limit_exceeded"):
        mutate(tmp_path, confirmed["revision"], dict(op="withdraw", position_id=row["position_id"], expected_version=row["version"]))
    assert snapshot(tmp_path).read_bytes() == before


def test_source_budgets_return_not_checked_without_hiding_intentions(tmp_path, monkeypatch):
    files = []
    for index in range(2):
        path = tmp_path / f"synthetic-{index}.txt"
        path.write_bytes(b"Synthetic independent documentary basis")
        files.append(dict(path=path.name, file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), reference=None))
    saved = mutate(tmp_path, None, create(sources=files))
    monkeypatch.setattr(p, "MAX_SOURCE_FILES", 1)
    result = read(tmp_path, include_history=True, check_sources=True)
    assert result["positions"] == saved["positions"]
    assert [s["status"] for s in result["source_observations"]] == ["same_bytes", "not_checked"]
    with pytest.raises(p.PositionError, match="resource_limit_exceeded"):
        mutate(tmp_path, saved["revision"], create(2, sources=files))
    assert read(tmp_path)["revision"] == saved["revision"]


def test_symlink_ancestors_source_hardlinks_and_private_metadata(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(p.PositionError, match="invalid_workspace"):
        read(link)
    with pytest.raises(p.PositionError, match="invalid_workspace"):
        read(str(link) + "/../real")
    side = real / ".veqtor"
    side.mkdir(mode=0o755)
    with pytest.raises(p.PositionError, match="unsafe_storage"):
        mutate(real, None, create())
    side.chmod(0o700)
    source = real / "synthetic.txt"
    source.write_bytes(b"Synthetic source")
    os.link(source, real / "linked.txt")
    binding = dict(path=source.name, file_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), reference=None)
    with pytest.raises(p.PositionError, match="source_unverified"):
        mutate(real, None, create(sources=[binding]))


@pytest.mark.parametrize("target", [".veqtor", p.LOCK_NAME])
def test_missing_write_lock_never_becomes_uninitialized_success(tmp_path, monkeypatch, target):
    real_open = os.open

    def fail_lock(path, flags, *args, **kwargs):
        if path == target:
            raise FileNotFoundError("synthetic lock bootstrap failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_lock)
    with pytest.raises(p.PositionError, match="storage_failure"):
        mutate(tmp_path, None, create())
    assert not snapshot(tmp_path).exists()
