# SPDX-License-Identifier: Apache-2.0
"""Durable cleanup plan, exact approval, old-store migration and public MCP."""

import hashlib
import json
import subprocess
import sys

import jsonschema
import pytest
from test_deal_outgoing import RID, change, publish, raw, verify
from test_outgoing_cleanup import (
    REMOVE_PROPS,
    REMOVE_THREAD,
    fictional_parts,
    pack,
    unpack,
)

from veqtor_docx import round_snapshots as snapshots
from veqtor_mcp import _deal_state_contract as contract
from veqtor_mcp import positions, server


@pytest.fixture
def case(tmp_path):
    payload = pack(fictional_parts())
    source = tmp_path / "source.docx"
    source.write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    state = change(
        tmp_path,
        None,
        1,
        "start_round",
        previous_round_id=None,
        path=str(source),
        file_sha256=sha,
    )
    state = change(
        tmp_path,
        state,
        2,
        "prepare_working_copy",
        incoming_basis_sha256=sha,
        path="working.docx",
    )
    plan = state["working_copy_plans"][0]
    state = change(
        tmp_path,
        state,
        3,
        "publish_working_copy",
        copy_plan_request_id=plan["request_id"],
        plan_sha256=plan["plan_sha256"],
    )
    return tmp_path, source, state


def command(state, **overrides):
    work = state["working_documents"][-1]
    return dict(
        action="prepare_outgoing_package",
        round_id=RID,
        expected_working_request_id=work["request_id"],
        file_sha256=work["file_sha256"],
        path="outgoing",
        redline_name="Review.docx",
        clean_draft_name="Clean.docx",
        comments_policy=REMOVE_THREAD,
        properties_policy=REMOVE_PROPS,
        **overrides,
    )


def prepare(folder, state, n=4):
    return server.mutate_deal_state(
        str(folder), state["revision"], f"{n:032x}", command(state)
    )


def test_durable_exact_cleanup_before_publication_and_fresh_process(case):
    folder, source, old = case
    source_bytes = source.read_bytes()
    before = raw(folder)
    assert json.loads(before)["schema_version"] == "deal_rounds_store.v5"
    assert (
        server.read_deal_state(str(folder))["revision"] == old["revision"]
        and raw(folder) == before
    )
    state = prepare(folder, old)
    row = state["outgoing_packages"][0]
    assert json.loads(raw(folder))["schema_version"] == "deal_rounds_store.v6"
    assert not (folder / "outgoing").exists()
    assert len(json.loads(row["report_utf8"])["cleanup"]["removed_comments"]) == 2
    assert state["working_documents"] == old["working_documents"]
    state = publish(folder, state, n=5)
    state = verify(folder, state, n=6)
    assert source.read_bytes() == (folder / "working.docx").read_bytes() == source_bytes
    assert (folder / "outgoing/Review.docx").read_bytes() != source_bytes
    assert not state["rounds"][0]["sent_documents"]
    before_cold = raw(folder)
    cold = json.loads(
        subprocess.check_output(
            [
                sys.executable,
                "-c",
                "import json,sys;from veqtor_mcp.server import read_deal_state;print(json.dumps(read_deal_state(sys.argv[1])))",
                str(folder),
            ],
            text=True,
        )
    )
    assert cold["outgoing_packages"] == state["outgoing_packages"]
    assert cold["outgoing_events"] == state["outgoing_events"]
    assert raw(folder) == before_cold


@pytest.mark.parametrize("after_commit", [False, True])
def test_lost_state_reply_keeps_cleaned_files_and_recovers_same_plan(
    case, monkeypatch, after_commit
):
    folder, source, old = case
    prepared = prepare(folder, old)
    original = positions._publish
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            if after_commit:
                original(*args, **kwargs)
            raise OSError("fictional lost state reply")

        patch.setattr(positions, "_publish", fail)
        with pytest.raises(
            snapshots.SnapshotError, match="outgoing_state_commit_uncertain"
        ):
            publish(folder, prepared, n=5)
    files = {
        p.name: (p.stat().st_ino, p.read_bytes())
        for p in (folder / "outgoing").iterdir()
    }
    if after_commit:
        state = publish(folder, prepared, n=5)
        assert state["outgoing_events"][0]["status"] == "published"
    else:
        state = verify(folder, prepared, n=6)
        assert state["outgoing_events"][0]["status"] == "observed_match"
    assert {
        p.name: (p.stat().st_ino, p.read_bytes())
        for p in (folder / "outgoing").iterdir()
    } == files
    assert source.read_bytes() == (folder / "working.docx").read_bytes()


def test_prior_v5_report_unchanged_and_publishable_after_v6_upgrade(case):
    folder, _, state = case
    work = state["working_documents"][-1]
    old_command = command(state)
    old_command.update(
        comments_policy="preserve_all",
        properties_policy="preserve_all",
        path="preserved",
    )
    state = server.mutate_deal_state(
        str(folder), state["revision"], f"{4:032x}", old_command
    )
    old_report = state["outgoing_packages"][0]
    state = prepare(folder, state, n=5)
    assert state["outgoing_packages"][0] == old_report
    state = publish(folder, state, n=6)
    assert (folder / "preserved/Review.docx").read_bytes() == (
        folder / "working.docx"
    ).read_bytes()
    manual = unpack((folder / "working.docx").read_bytes())
    manual["docProps/core.xml"] = manual["docProps/core.xml"].replace(
        b"Keep this title", b"Fictional manual title"
    )
    payload = pack(manual)
    (folder / "working.docx").write_bytes(payload)
    state = change(
        folder,
        state,
        7,
        "record_working_version",
        expected_working_request_id=work["request_id"],
        file_sha256=hashlib.sha256(payload).hexdigest(),
        user_confirmed=True,
        statement="Fictional manual property edit.",
    )
    assert json.loads(raw(folder))["schema_version"] == "deal_rounds_store.v6"


def test_mutated_policy_and_recomputed_plan_cannot_rewrite_saved_request_history(case):
    folder, _, state = case
    state = prepare(folder, state)
    stored = json.loads(raw(folder))
    row = stored["outgoing_packages"][0]
    row["comments_policy"] = {"mode": "remove_all"}
    report = json.loads(row["report_utf8"])
    report["comments_policy"] = row["comments_policy"]
    row["report_utf8"] = positions._json_bytes(report).decode()
    row["plan_sha256"] = hashlib.sha256(row["report_utf8"].encode()).hexdigest()
    stored["revision"] = positions._revision(stored)
    path = folder / ".veqtor/deal-rounds.json"
    path.write_bytes(positions._json_bytes(stored))
    before = path.read_bytes()
    with pytest.raises(positions.PositionError, match="storage_corrupt"):
        server.read_deal_state(str(folder))
    assert path.read_bytes() == before


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_cleanup_policy_and_report_public_mcp(case, mode):
    from mcp import Client

    folder, _, state = case
    async with Client(server.mcp, mode=mode) as client:
        tools = await client.list_tools()
        mutate = next(t for t in tools.tools if t.name == "mutate_deal_state")
        args = {
            "folder": str(folder),
            "expected_revision": state["revision"],
            "request_id": f"{4:032x}",
            "command": command(state),
        }
        jsonschema.validate(args, mutate.input_schema)
        response = await client.call_tool("mutate_deal_state", args)
        assert not response.is_error, response
        result = response.structured_content
        jsonschema.validate(result, contract.RESULT)
        assert result["outgoing_packages"][0]["comments_policy"] == REMOVE_THREAD
        assert not result["outgoing_events"] and not (folder / "outgoing").exists()
