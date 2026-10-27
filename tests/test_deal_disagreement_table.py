# SPDX-License-Identifier: Apache-2.0
"""Explicit public selection, durable preview and cold continuation."""
from copy import deepcopy
import hashlib
import json
import subprocess
import sys
import zipfile

import pytest
from test_deal_outgoing import case as case
from test_deal_outgoing import change, RID, raw, publish, verify, prepare as old_prepare
from test_deal_workflow import content
from veqtor_docx.round_snapshots import SnapshotError
from veqtor_mcp import server, positions as p, deal_state as d

IID = "issue_" + "1" * 32


def issue(case):
    folder, source, work, state = case
    data = content(source)
    data.update(model_assessment="PRIVATE_ASSESSMENT", remaining_question="PRIVATE_QUESTION")
    state = change(folder, state, 3, "save_issue", issue_id=IID, expected_version=None, content=data)
    return folder, source, work, state


def command(folder, state, **overrides):
    brief = server.read_round_issue_brief(str(folder), state["revision"], None, RID, [IID])
    work = state["working_documents"][-1]
    selection = dict(expected_dataset_sha256=brief["dataset_sha256"], issue_ids=[IID],
        columns=["incoming_quote", "proposed_wording", "sent_quote"],
        name="Disagreements.docx", title="Selected disagreements", language="en")
    selection.update(overrides)
    return dict(action="prepare_outgoing_package_with_table", round_id=RID,
        expected_working_request_id=work["request_id"], file_sha256=work["file_sha256"],
        path="outgoing", redline_name="Redline.docx", clean_draft_name="Clean.docx",
        comments_policy="preserve_all", properties_policy="preserve_all", table_selection=selection)


def prepare(folder, state, **overrides):
    return server.mutate_deal_state(str(folder), state["revision"], f"{4:032x}", command(folder, state, **overrides))


def test_exact_view_private_exclusion_publish_and_cold_resume(case):
    folder, source, work, state = issue(case)
    original = source.read_bytes()
    before = raw(folder)
    assert server.read_deal_state(str(folder))["revision"] == state["revision"] and raw(folder) == before
    state = prepare(folder, state)
    assert json.loads(raw(folder))["schema_version"] == "deal_rounds_store.v7"
    row = state["outgoing_packages"][0]
    report = json.loads(row["report_utf8"])
    assert report["schema_version"] == "outgoing_preparation.v3"
    assert report["disagreement_table"]["view"]["rows"] == [dict(
        incoming_quote=state["issues"][0]["content"]["incoming_quote"]["text"],
        proposed_wording="Services agreement", sent_quote=None)]
    assert "PRIVATE_" not in row["report_utf8"] and not (folder / "outgoing").exists()
    state = publish(folder, state, n=5)
    state = verify(folder, state, n=6)
    with zipfile.ZipFile(folder / "outgoing/Disagreements.docx") as z:
        payload = b"".join(z.read(n) for n in z.namelist())
    for secret in [b"PRIVATE_", IID.encode(), str(folder).encode(), b"client_asserted"]:
        assert secret not in payload
    assert source.read_bytes() == work.read_bytes() == original
    assert not state["rounds"][0]["sent_documents"]
    before = raw(folder)
    cold = json.loads(subprocess.check_output([sys.executable, "-c",
        "import json,sys;from veqtor_mcp.server import read_deal_state;print(json.dumps(read_deal_state(sys.argv[1])))", str(folder)]))
    assert cold["outgoing_packages"] == state["outgoing_packages"] and raw(folder) == before


@pytest.mark.parametrize("overrides", [{"expected_dataset_sha256": "f" * 64},
    {"columns": ["model_assessment"]}, {"columns": []}, {"issue_ids": [IID, IID]},
    {"issue_ids": ["issue_" + "2" * 32]}, {"name": "redline.docx"}, {"title": "Bad\rtitle"}])
def test_invalid_or_stale_selection_has_no_state_or_output_effect(case, overrides):
    folder, _, _, state = issue(case)
    before = raw(folder)
    with pytest.raises((p.PositionError, SnapshotError)):
        prepare(folder, state, **overrides)
    assert raw(folder) == before and not (folder / "outgoing").exists()


def test_changed_issue_blocks_old_publication_and_keeps_plan(case):
    folder, source, _, state = issue(case)
    state = prepare(folder, state)
    plan = deepcopy(state["outgoing_packages"])
    state = change(folder, state, 5, "save_issue", issue_id=IID, expected_version=1,
        content={**content(source), "proposed_wording": "Different"})
    assert json.loads(raw(folder))["schema_version"] == "deal_rounds_store.v7"
    before = raw(folder)
    with pytest.raises(p.PositionError, match="outgoing_basis_changed"):
        publish(folder, state, n=6)
    assert raw(folder) == before and not (folder / "outgoing").exists()
    assert server.read_deal_state(str(folder))["outgoing_packages"] == plan


def test_old_preview_survives_upgrade_and_remains_verifiable(case):
    folder, source, work, state = issue(case)
    state = old_prepare((folder, source, work, state), n=10, path="old-kit")
    state = publish(folder, state, n=11)
    old = deepcopy(state["outgoing_packages"][0])
    state = prepare(folder, state)
    assert state["outgoing_packages"][0] == old
    state = verify(folder, state, n=12)
    assert state["outgoing_packages"][0] == old


def test_rehashed_report_cannot_inject_unselected_text(case):
    folder, _, _, state = issue(case)
    prepare(folder, state)
    stored = json.loads(raw(folder))
    row = stored["outgoing_packages"][0]
    report = json.loads(row["report_utf8"])
    report["disagreement_table"]["view"]["rows"][0]["incoming_quote"] = "Invented quote"
    row["report_utf8"] = p._json_bytes(report).decode()
    row["plan_sha256"] = hashlib.sha256(row["report_utf8"].encode()).hexdigest()
    stored["revision"] = p._revision(stored)
    (folder / ".veqtor" / d.STORE_NAME).write_bytes(p._json_bytes(stored))
    with pytest.raises(p.PositionError, match="storage_corrupt"):
        server.read_deal_state(str(folder))


def test_table_only_and_later_working_copy_commands_preserve_v7(case):
    folder, source, _, state = issue(case)
    cmd = command(folder, state, columns=["proposed_wording"])
    cmd.update(redline_name=None, clean_draft_name=None)
    state = server.mutate_deal_state(str(folder), state["revision"], f"{4:032x}", cmd)
    state = publish(folder, state, n=5)
    assert [path.name for path in (folder / "outgoing").iterdir()] == ["Disagreements.docx"]
    rid = "round_" + "2" * 32
    digest = state["rounds"][0]["incoming_sha256"]
    state = server.mutate_deal_state(str(folder), state["revision"], f"{6:032x}",
        dict(action="start_round", round_id=rid, previous_round_id=None, path=str(source), file_sha256=digest))
    state = server.mutate_deal_state(str(folder), state["revision"], f"{7:032x}",
        dict(action="prepare_working_copy", round_id=rid, incoming_basis_sha256=digest, path="another-copy.docx"))
    assert json.loads(raw(folder))["schema_version"] == "deal_rounds_store.v7"
    assert len(server.read_deal_state(str(folder))["outgoing_packages"]) == 1


def test_live_working_change_after_dataset_read_refuses(case):
    folder, source, work, state = issue(case)
    cmd = command(folder, state)
    work.write_bytes(b"Changed outside Veqtor")
    before = raw(folder)
    with pytest.raises(p.PositionError, match="outgoing_basis_changed"):
        server.mutate_deal_state(str(folder), state["revision"], f"{4:032x}", cmd)
    assert raw(folder) == before and not (folder / "outgoing").exists()


def test_later_issue_edit_does_not_rewrite_published_historical_table(case):
    folder, source, _, state = issue(case)
    state = prepare(folder, state)
    state = publish(folder, state, n=5)
    original = (folder / "outgoing/Disagreements.docx").read_bytes()
    state = change(folder, state, 6, "save_issue", issue_id=IID, expected_version=1,
        content={**content(source), "proposed_wording": "Later proposal"})
    state = verify(folder, state, n=7)
    assert state["outgoing_events"][-1]["status"] == "observed_match"
    assert (folder / "outgoing/Disagreements.docx").read_bytes() == original
