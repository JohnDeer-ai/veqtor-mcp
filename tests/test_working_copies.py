# SPDX-License-Identifier: Apache-2.0
"""Initial unchanged working copies, separate publication and loss recovery."""

import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
from test_deal_workflow import edit
from test_outgoing_package import fixture_source

from veqtor_docx import round_snapshots as snapshots
from veqtor_mcp import _deal_outgoing as outgoing
from veqtor_mcp import _deal_state_contract as c
from veqtor_mcp import _deal_workflow as w
from veqtor_mcp import deal_state as d
from veqtor_mcp import positions as p
from veqtor_mcp import server

RID = "round_" + "6" * 32


def change(folder, state, n, action, **values):
    result = server.mutate_deal_state(
        str(folder),
        state["revision"] if state else None,
        f"{n:032x}",
        dict(action=action, round_id=RID, **values),
    )
    jsonschema.validate(result, c.RESULT)
    return result


@pytest.fixture
def case(tmp_path, demo_dir):
    source, snap = fixture_source(tmp_path, demo_dir)
    state = change(
        tmp_path,
        None,
        1,
        "start_round",
        previous_round_id=None,
        path=str(source),
        file_sha256=snap.sha256,
    )
    return tmp_path, source, snap, state


def prepare(case, n=2, **values):
    folder, _, snap, state = case
    return change(
        folder,
        state,
        n,
        "prepare_working_copy",
        **{"incoming_basis_sha256": snap.sha256, "path": "working.docx", **values},
    )


def select(folder, state, n=3, *, verify=False, **values):
    plan = state["working_copy_plans"][-1]
    return change(
        folder,
        state,
        n,
        "record_working_copy_verification" if verify else "publish_working_copy",
        **{
            "copy_plan_request_id": plan["request_id"],
            "plan_sha256": plan["plan_sha256"],
            **values,
        },
    )


def raw(folder):
    return (folder / ".veqtor" / d.STORE_NAME).read_bytes()


def test_first_unchanged_copy_survives_new_process_without_invented_edit(case):
    folder, source, snap, old = case
    state = prepare(case)
    assert not (folder / "working.docx").exists() and not state["working_documents"]
    saved_plan = state["working_copy_plans"][0]
    assert json.loads(raw(folder))["working_copy_plans"] == [saved_plan]
    state = select(folder, state)
    work = state["working_documents"][0]
    assert work["file_sha256"] == snap.sha256
    assert (
        work["origin"] == "verified_snapshot_copy"
        and work["publication_status"] == "published"
    )
    assert (
        work["operation_id"] is work["previous_request_id"] is work["statement"] is None
    )
    assert state["working_observations"][0]["status"] == "same_bytes"
    assert state["operations"] == state["issues"] == state["outgoing_packages"] == []
    assert state["rounds"] == old["rounds"]
    assert source.read_bytes() == (folder / "working.docx").read_bytes() == snap.payload
    assert os.stat(source).st_ino != os.stat(folder / "working.docx").st_ino
    snapshot_dir = Path(state["documents"][0]["path"]).parent
    assert {x.name for x in snapshot_dir.iterdir()} == {"source.docx", "manifest.json"}
    assert not (folder / ".veqtor" / "operations").exists()
    script = "import json,sys;from veqtor_mcp.server import read_deal_state;print(json.dumps(read_deal_state(sys.argv[1])))"
    cold = json.loads(
        subprocess.check_output([sys.executable, "-c", script, str(folder)], text=True)
    )
    assert cold["working_documents"] == state["working_documents"]
    assert cold["working_copy_plans"] == [saved_plan]


def test_copy_uses_snapshot_after_original_disappears(case):
    folder, source, snap, _ = case
    state = prepare(case)
    source.unlink()  # Fictional source loss; the saved role remains intact.
    state = select(folder, state)
    assert (folder / "working.docx").read_bytes() == snap.payload
    assert state["rounds"][0]["incoming_sha256"] == snap.sha256


def test_next_real_edit_and_manual_version_preserve_copy_origin(case):
    folder, source, snap, _ = case
    state = select(folder, prepare(case))
    first = state["working_documents"][0]
    work = folder / "working.docx"
    saved = edit(work, old="Delivery", new="Shipment")
    operation = saved["operation_journal"]["operation_id"]
    state = change(
        folder,
        state,
        4,
        "link_operation",
        source_path=str(work),
        operation_id=operation,
    )
    state = change(folder, state, 5, "link_working_document", operation_id=operation)
    assert state["working_documents"][0] == first
    assert state["working_documents"][-1]["previous_request_id"] == first["request_id"]
    # A controlled fictional manual save restores the original bytes at this
    # ordinary selected path; it is recorded as a new version, never as sending.
    work.write_bytes(snap.payload)
    state = change(
        folder,
        state,
        6,
        "record_working_version",
        expected_working_request_id=state["working_documents"][-1]["request_id"],
        file_sha256=snap.sha256,
        user_confirmed=True,
        statement="Fictional manual save.",
    )
    assert (
        len(state["working_documents"]) == 3 and state["working_documents"][0] == first
    )
    assert json.loads(raw(folder))["schema_version"] == "deal_rounds_store.v5"
    assert (
        source.read_bytes() == snap.payload and not state["rounds"][0]["sent_documents"]
    )


def test_unchanged_copy_can_prepare_verified_outgoing_without_edit(case):
    folder, source, snap, _ = case
    state = select(folder, prepare(case))
    state = change(
        folder,
        state,
        4,
        "prepare_outgoing_package",
        expected_working_request_id=state["working_documents"][0]["request_id"],
        file_sha256=snap.sha256,
        path="outgoing",
        redline_name="Response.docx",
        clean_draft_name=None,
        comments_policy="preserve_all",
        properties_policy="preserve_all",
    )
    plan = state["outgoing_packages"][0]
    state = change(
        folder,
        state,
        5,
        "publish_outgoing_package",
        package_request_id=plan["request_id"],
        plan_sha256=plan["plan_sha256"],
        user_confirmed=True,
        statement="Publish this exact fictional unchanged response.",
    )
    state = change(
        folder,
        state,
        6,
        "record_outgoing_verification",
        package_request_id=plan["request_id"],
        plan_sha256=plan["plan_sha256"],
    )
    assert (
        (folder / "outgoing/Response.docx").read_bytes()
        == source.read_bytes()
        == snap.payload
    )
    assert not state["operations"] and not state["rounds"][0]["sent_documents"]
    assert json.loads(raw(folder))["schema_version"] == "deal_rounds_store.v5"


@pytest.mark.parametrize("committed", [False, True])
def test_lost_state_commit_never_republishes_and_retains_original_plan(
    case, monkeypatch, committed
):
    folder, _, snap, _ = case
    state = prepare(case)
    before = raw(folder)
    original = p._publish

    def fail(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise OSError("fictional lost state reply")

    with monkeypatch.context() as patch:
        patch.setattr(p, "_publish", fail)
        with pytest.raises(
            snapshots.SnapshotError, match="working_copy_state_commit_uncertain"
        ) as caught:
            select(folder, state)
        assert caught.value.metadata["publication_completed"] is True
        assert (
            caught.value.metadata["copy_plan_request_id"]
            == state["working_copy_plans"][0]["request_id"]
        )
    assert (folder / "working.docx").read_bytes() == snap.payload
    monkeypatch.setattr(
        snapshots,
        "restore_snapshot_copy",
        lambda *a, **k: pytest.fail("must not publish again"),
    )
    if committed:
        result = select(folder, state)  # Exact original request replay.
        assert result["working_documents"][0]["publication_status"] == "published"
    else:
        assert raw(folder) == before
        result = select(folder, state, 4, verify=True)
        assert result["working_documents"][0]["publication_status"] == "observed_match"
    assert result["working_copy_plans"] == state["working_copy_plans"]


def test_existing_matching_file_is_only_observed_and_replay_is_historical(case):
    folder, _, snap, _ = case
    state = prepare(case)
    work = folder / "working.docx"
    work.write_bytes(snap.payload)
    work.chmod(0o600)
    with pytest.raises(snapshots.SnapshotError, match="output_exists"):
        select(folder, state)
    result = select(folder, state, verify=True)
    assert result["working_documents"][0]["publication_status"] == "observed_match"
    before = raw(folder)
    work.write_bytes(b"fictional later drift")
    replay = select(folder, state, verify=True)
    assert (
        replay["working_documents"] == result["working_documents"]
        and raw(folder) == before
    )
    assert replay["working_observations"][0]["status"] != "same_bytes"
    assert work.read_bytes() == b"fictional later drift"


@pytest.mark.parametrize(
    "fault", ["different", "symlink", "hardlink", "mode", "missing"]
)
def test_verification_refuses_unsafe_or_different_output_without_changing_it(
    case, fault
):
    folder, source, snap, _ = case
    state = prepare(case)
    before = raw(folder)
    path = folder / "working.docx"
    if fault == "symlink":
        path.symlink_to(source)
    elif fault == "hardlink":
        os.link(source, path)
    elif fault != "missing":
        path.write_bytes(snap.payload if fault == "mode" else b"unknown data")
        path.chmod(0o644 if fault == "mode" else 0o600)
    data = path.read_bytes() if path.exists() else None
    with pytest.raises((ValueError, OSError)):
        select(folder, state, verify=True)
    assert raw(folder) == before
    assert (path.read_bytes() if path.exists() else None) == data


@pytest.mark.parametrize(
    "path",
    ["source.docx", "../x.docx", ".VEQTOR/x.docx", ".veqtor-backups/x.docx", "x.txt"],
)
def test_prepare_never_selects_an_existing_or_reserved_destination(case, path):
    folder, source, snap, _ = case
    before = raw(folder)
    with pytest.raises((ValueError, OSError)):
        prepare(case, path=path)
    assert raw(folder) == before and source.read_bytes() == snap.payload


@pytest.mark.parametrize(
    "override", [{"plan_sha256": "f" * 64}, {"copy_plan_request_id": "e" * 32}]
)
def test_plan_mismatch_has_no_file_or_state_effect(case, override):
    folder, _, _, _ = case
    state = prepare(case)
    before = raw(folder)
    with pytest.raises(p.PositionError, match="working_copy_plan_mismatch"):
        select(folder, state, **override)
    assert raw(folder) == before and not (folder / "working.docx").exists()


@pytest.mark.parametrize("competing", ["import", "operation"])
def test_prepared_copy_cannot_override_later_work(case, competing):
    folder, source, snap, _ = case
    state = prepare(case)
    if competing == "import":
        (folder / "human.docx").write_bytes(snap.payload)
        state = change(
            folder,
            state,
            4,
            "import_working_document",
            path="human.docx",
            file_sha256=snap.sha256,
            incoming_basis_sha256=snap.sha256,
            user_confirmed=True,
            statement="Fictional separate working selection.",
        )
    else:
        saved = edit(source, folder / "edited.docx", old="Delivery", new="Shipment")
        state = change(
            folder,
            state,
            4,
            "link_operation",
            source_path=str(source),
            operation_id=saved["operation_journal"]["operation_id"],
        )
    before = raw(folder)
    with pytest.raises(p.PositionError):
        select(folder, state, 5)
    assert raw(folder) == before and not (folder / "working.docx").exists()


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_legacy_read_is_unchanged_and_first_copy_upgrade_is_lossless(case, version):
    folder, source, snap, state = case
    store = json.loads(raw(folder))
    if version >= 2:
        store = w.upgrade(store, imported=version >= 3)
    if version == 4:
        store = outgoing.upgrade(store)
    store["revision"] = p._revision(store)
    path = folder / ".veqtor" / d.STORE_NAME
    path.write_bytes(p._json_bytes(store))
    before = path.read_bytes()
    state = server.read_deal_state(str(folder))
    assert path.read_bytes() == before
    result = prepare((folder, source, snap, state))
    assert (
        result["rounds"] == state["rounds"]
        and result["requests"][:-1] == state["requests"]
    )
    assert json.loads(path.read_bytes())["schema_version"] == "deal_rounds_store.v5"


@pytest.mark.parametrize(
    "field,value",
    [
        ("plan_sha256", "f" * 64),
        ("path", "different.docx"),
        ("source_sha256", "e" * 64),
    ],
)
def test_corrupt_plan_is_refused_even_with_recomputed_store_revision(
    case, field, value
):
    folder, _, _, _ = case
    prepare(case)
    store = json.loads(raw(folder))
    store["working_copy_plans"][0][field] = value
    store["revision"] = p._revision(store)
    path = folder / ".veqtor" / d.STORE_NAME
    path.write_bytes(p._json_bytes(store))
    before = path.read_bytes()
    with pytest.raises(p.PositionError, match="storage_corrupt"):
        server.read_deal_state(str(folder))
    assert path.read_bytes() == before


def test_result_validation_refuses_before_copy_creation(case):
    folder, _, _, _ = case
    state = prepare(case)
    plan = state["working_copy_plans"][0]
    before = raw(folder)

    def refuse(_result):
        raise ValueError("fictional result schema failure")

    with pytest.raises(ValueError, match="result schema"):
        d.mutate_deal_state(
            str(folder),
            state["revision"],
            "3" * 32,
            {
                "action": "publish_working_copy",
                "round_id": RID,
                "copy_plan_request_id": plan["request_id"],
                "plan_sha256": plan["plan_sha256"],
            },
            validate_result=refuse,
        )
    assert raw(folder) == before and not (folder / "working.docx").exists()


def test_two_competing_initial_selections_publish_only_one(case):
    from concurrent.futures import ThreadPoolExecutor

    folder, source, snap, state = case
    state = prepare(case)
    state = prepare((folder, source, snap, state), 3, path="second.docx")

    def attempt(n, plan):
        try:
            return select(
                folder,
                state,
                n,
                copy_plan_request_id=plan["request_id"],
                plan_sha256=plan["plan_sha256"],
            )
        except p.PositionError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda pair: attempt(*pair), enumerate(state["working_copy_plans"], 4)
            )
        )
    assert sum(isinstance(result, dict) for result in outcomes) == 1
    assert "revision_conflict" in outcomes
    assert (
        sum((folder / name).exists() for name in ["working.docx", "second.docx"]) == 1
    )
    loaded = server.read_deal_state(str(folder))
    assert len(loaded["working_documents"]) == 1


def test_plan_count_limit_refuses_before_any_copy(case):
    folder, source, snap, state = case
    for i in range(20):
        state = prepare((folder, source, snap, state), i + 2, path=f"copy-{i}.docx")
    before = raw(folder)
    with pytest.raises(p.PositionError):
        prepare((folder, source, snap, state), 22, path="excess.docx")
    assert raw(folder) == before and not list(folder.glob("copy-*.docx"))


def test_parent_alias_after_preparation_is_refused(case):
    folder, _, _, _ = case
    parent = folder / "copies"
    parent.mkdir()
    state = prepare(case, path="copies/working.docx")
    parent.rename(folder / "retained")
    parent.symlink_to(folder / "retained", target_is_directory=True)
    before = raw(folder)
    with pytest.raises((ValueError, OSError)):
        select(folder, state)
    assert raw(folder) == before and not (folder / "retained/working.docx").exists()


def test_late_copy_drift_retains_publication_fact_and_unknown_bytes(case, monkeypatch):
    folder, _, _, _ = case
    state = prepare(case)
    before = raw(folder)
    original = snapshots.restore_snapshot_copy

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        (folder / "working.docx").write_bytes(b"fictional concurrent data")
        return result

    monkeypatch.setattr(snapshots, "restore_snapshot_copy", changed)
    with pytest.raises(
        snapshots.SnapshotError, match="working_copy_verification_failed"
    ) as caught:
        select(folder, state)
    assert caught.value.metadata["publication_completed"] is True
    assert caught.value.metadata["output_path_verified"] is False
    assert raw(folder) == before
    assert (folder / "working.docx").read_bytes() == b"fictional concurrent data"


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_initial_copy_commands_through_public_mcp(case, mode):
    from mcp import Client

    folder, _, snap, state = case
    async with Client(server.mcp, mode=mode) as client:
        tools = await client.list_tools()
        mutate = next(t for t in tools.tools if t.name == "mutate_deal_state")
        command = {
            "action": "prepare_working_copy",
            "round_id": RID,
            "incoming_basis_sha256": snap.sha256,
            "path": "working.docx",
        }
        for n in [2, 3]:
            if n == 3:
                plan = state["working_copy_plans"][0]
                command = {
                    "action": "publish_working_copy",
                    "round_id": RID,
                    "copy_plan_request_id": plan["request_id"],
                    "plan_sha256": plan["plan_sha256"],
                }
            args = {
                "folder": str(folder),
                "expected_revision": state["revision"],
                "request_id": f"{n:032x}",
                "command": command,
            }
            jsonschema.validate(args, mutate.input_schema)
            result = await client.call_tool("mutate_deal_state", args)
            assert not result.is_error, result
            state = result.structured_content
            jsonschema.validate(state, c.RESULT)
        assert state["working_documents"][0]["publication_status"] == "published"
        assert state["working_observations"][0]["status"] == "same_bytes"
