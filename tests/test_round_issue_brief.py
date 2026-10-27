# SPDX-License-Identifier: Apache-2.0
"""Fictional selected-issue projections: exact facts, bounded complete selection."""

import hashlib
import json
import os
import subprocess
import sys

import jsonschema
import pytest
from mcp import Client

from test_deal_workflow import RID, IID, content, link_copy, mutate, quote, sha
from veqtor_docx import inspect_document
from veqtor_mcp import _deal_workflow as workflow
from veqtor_mcp import server, positions as p, deal_state as d
from veqtor_mcp._round_issue_brief_contract import RESULT


@pytest.fixture
def case(tmp_path, demo_dir):
    source = tmp_path / "incoming.docx"
    source.write_bytes((demo_dir / "round-1-outgoing-draft.docx").read_bytes())
    state = mutate(
        tmp_path,
        None,
        1,
        dict(
            action="start_round",
            previous_round_id=None,
            path=str(source),
            file_sha256=sha(source),
        ),
    )
    state = mutate(
        tmp_path,
        state,
        2,
        dict(
            action="save_issue",
            issue_id=IID,
            expected_version=None,
            content=content(source),
        ),
    )
    return tmp_path, source, state


def read(folder, state, ids=None, **kwargs):
    result = server.read_round_issue_brief(
        str(folder),
        state["revision"],
        kwargs.pop("positions_revision", None),
        kwargs.pop("round_id", RID),
        ids or [IID],
        **kwargs,
    )
    jsonschema.validate(result, RESULT)
    assert (
        result["dataset_sha256"]
        == hashlib.sha256(p._json_bytes(result["data"])).hexdigest()
    )
    return result


def inventory(folder):
    return {
        str(f.relative_to(folder)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in folder.rglob("*")
        if f.is_file()
    }


def test_first_round_exact_quote_missing_fields_and_private_labels(case):
    folder, source, state = case
    before = inventory(folder)
    result = read(folder, state)
    data = result["data"]
    row = data["issues"][0]
    assert data["selected_issue_ids"] == [IID] and data["omitted_issue_ids"] == []
    assert data["round_issue_count"] == 1 and data["operations"] == []
    assert data["basis"]["sent_basis"] is None
    assert data["basis"]["working_status"] == "not_selected"
    assert row["issue"] == state["issues"][0]
    assert row["quote_status"] == dict(
        sent="missing", incoming="verified_snapshot", working="missing"
    )
    assert row["observation"]["decision_status"] == "not_recorded"
    assert row["related_comments"] == "not_recorded_in_issue_state"
    assert result["audience"] == "internal_review" and row["position_version"] is None
    assert inventory(folder) == before
    assert read(folder, state) == result


def test_order_omissions_and_latest_version_are_not_silently_changed(case):
    folder, source, state = case
    ids = [IID, "issue_" + "2" * 32, "issue_" + "3" * 32]
    for n, iid in enumerate(ids[1:], 3):
        state = mutate(
            folder,
            state,
            n,
            dict(
                action="save_issue",
                issue_id=iid,
                expected_version=None,
                content={**content(source), "title": f"Fictional question {n}"},
            ),
        )
    state = mutate(
        folder,
        state,
        5,
        dict(
            action="save_issue",
            issue_id=IID,
            expected_version=1,
            content={**content(source), "proposed_wording": "New unapproved proposal"},
        ),
    )
    out = read(folder, state, [ids[2], ids[0]])["data"]
    assert [r["issue"]["issue_id"] for r in out["issues"]] == [ids[2], ids[0]]
    assert out["omitted_issue_ids"] == [ids[1]] and out["round_issue_count"] == 3
    assert out["issues"][1]["issue"]["version"] == 2
    assert out["issues"][1]["issue"]["decision"] is None


@pytest.mark.parametrize(
    "fault",
    [
        "empty",
        "duplicate",
        "missing",
        "wrong_round",
        "stale",
        "too_many",
        "bad_id",
        "wrong_type",
        "bad_positions",
    ],
)
def test_selection_errors_refuse_without_partial_results_or_writes(case, fault):
    folder, source, state = case
    args = dict(
        folder=str(folder),
        expected_revision=state["revision"],
        expected_positions_revision=None,
        round_id=RID,
        issue_ids=[IID],
    )
    if fault == "empty":
        args["issue_ids"] = []
    if fault == "duplicate":
        args["issue_ids"] *= 2
    if fault == "missing":
        args["issue_ids"].append("issue_" + "9" * 32)
    if fault == "wrong_round":
        args["round_id"] = "round_" + "9" * 32
    if fault == "stale":
        args["expected_revision"] = "0" * 64
    if fault == "too_many":
        args["issue_ids"] = [f"issue_{n:032x}" for n in range(21)]
    if fault == "bad_id":
        args["issue_ids"] = [IID + "\n"]
    if fault == "wrong_type":
        args["issue_ids"] = IID
    if fault == "bad_positions":
        args["expected_positions_revision"] = "f" * 64
    before = inventory(folder)
    with pytest.raises(p.PositionError):
        server.read_round_issue_brief(**args)
    assert inventory(folder) == before


def test_absent_store_is_not_created(tmp_path):
    with pytest.raises(p.PositionError, match="round_missing"):
        server.read_round_issue_brief(str(tmp_path), "0" * 64, None, RID, [IID])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("fault", ["text", "offset", "paragraph"])
def test_rechecks_quote_even_after_rehashed_store_tampering(case, fault):
    folder, _, state = case
    path = folder / ".veqtor" / d.STORE_NAME
    value = json.loads(path.read_bytes())
    q = value["issue_events"][0]["content"]["incoming_quote"]
    if fault == "text":
        q["text"] = "Fabricated quote"
    if fault == "offset":
        q["start"] = 1
    if fault == "paragraph":
        q["paragraph_ref"]["paragraph_text_sha256"] = "f" * 64
    value["revision"] = p._revision(value)
    path.write_bytes(p._json_bytes(value))
    before = inventory(folder)
    with pytest.raises(p.PositionError, match="quote_mismatch"):
        read(folder, {"revision": value["revision"]})
    assert inventory(folder) == before


@pytest.mark.parametrize("fault", ["changed", "missing", "symlink", "hardlink"])
def test_live_work_drift_never_becomes_current_approval(case, fault):
    folder, source, state = case
    state, work, oid = link_copy(folder, source, state, n=3)
    state = mutate(
        folder,
        state,
        5,
        dict(
            action="save_issue",
            issue_id=IID,
            expected_version=1,
            content=content(source, work, oid),
        ),
    )
    state = mutate(
        folder,
        state,
        6,
        dict(
            action="decide_issue",
            issue_id=IID,
            expected_version=2,
            choice="approve",
            user_confirmed=True,
            statement="Fictional approval",
            basis_sha256=sha(work),
        ),
    )
    before_result = read(folder, state)
    if fault == "changed":
        work.write_bytes(source.read_bytes())
    if fault == "missing":
        work.unlink()
    if fault in {"symlink", "hardlink"}:
        work.unlink()
        if fault == "symlink":
            work.symlink_to(source)
        else:
            os.link(source, work)
    result = read(folder, state)
    data = result["data"]
    row = data["issues"][0]
    assert data["basis"]["working_status"] == (
        "changed" if fault == "changed" else "unavailable"
    )
    assert row["observation"]["decision_status"] == (
        "basis_changed" if fault == "changed" else "unavailable"
    )
    assert row["issue"]["decision"]["choice"] == "approve"
    assert row["quote_status"]["working"] == "verified_current_snapshot"
    assert result["dataset_sha256"] != before_result["dataset_sha256"]


def test_historical_position_terms_and_confirmation_are_preserved(case):
    folder, source, state = case
    pid = "pos_" + "2" * 32
    data = dict(
        title="Price",
        desired_outcome="100",
        fallback="90",
        fallback_conditions="Only if prepaid",
        rationale="Private rationale",
        related_position_ids=[],
        content_origin="user_instruction",
        business_decision="pending",
        sources=[],
    )
    positions = server.mutate_deal_positions(
        str(folder), None, [dict(op="create", position_id=pid, content=data)]
    )
    link = dict(
        matter_id=positions["matter_id"],
        revision=positions["revision"],
        history_sequence=1,
        position_id=pid,
        version=1,
    )
    state = mutate(
        folder,
        state,
        3,
        dict(
            action="save_issue",
            issue_id=IID,
            expected_version=1,
            content={**content(source), "position": link},
        ),
    )
    current = read(folder, state, positions_revision=positions["revision"])
    positions = server.mutate_deal_positions(
        str(folder),
        positions["revision"],
        [
            dict(
                op="update",
                position_id=pid,
                expected_version=1,
                content={**data, "fallback_conditions": "Only if 2 years"},
            )
        ],
    )
    with pytest.raises(p.PositionError, match="positions_revision_conflict"):
        read(folder, state, positions_revision=current["data"]["positions_revision"])
    result = read(folder, state, positions_revision=positions["revision"])["data"][
        "issues"
    ][0]
    assert result["observation"]["position_status"] == "historical_available"
    assert (
        result["position_version"]["content"]["fallback_conditions"]
        == "Only if prepaid"
    )
    assert result["position_version"]["confirmation"] is None


def test_cold_process_reproduces_dataset_without_history_or_original(case):
    folder, source, state = case
    expected = read(folder, state)
    source.unlink()
    before = inventory(folder)
    script = (
        "import json,sys;from veqtor_mcp.server import read_round_issue_brief;"
        "print(json.dumps(read_round_issue_brief(*json.loads(sys.argv[1]))))"
    )
    # Source paths are unnecessary: quotes use immutable snapshots.
    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            script,
            json.dumps([str(folder), state["revision"], None, RID, [IID]]),
        ],
        text=True,
    )
    assert json.loads(output) == expected and inventory(folder) == before


def test_snapshot_budget_refuses_without_truncating(case, monkeypatch):
    folder, _, state = case
    before = inventory(folder)
    monkeypatch.setattr(d, "MAX_DOCUMENT_BYTES", 1)
    with pytest.raises(p.PositionError, match="resource_limit_exceeded"):
        read(folder, state)
    assert inventory(folder) == before


@pytest.mark.anyio
async def test_public_tool_closed_schema_and_no_optional_provenance(case, monkeypatch):
    folder, _, state = case
    before = inventory(folder)
    monkeypatch.delenv("VEQTOR_DISABLE_DECISION_RECORD", raising=False)
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "read_round_issue_brief",
            dict(
                folder=str(folder),
                expected_revision=state["revision"],
                expected_positions_revision=None,
                round_id=RID,
                issue_ids=[IID],
            ),
        )
    assert not result.is_error
    data = result.structured_content
    jsonschema.validate(data, RESULT)
    assert data["record_status"] == "disabled" and data["record_id"] is None
    assert inventory(folder) == before


def test_later_working_version_marks_old_quote_historical(case):
    folder, source, state = case
    state, work, oid = link_copy(folder, source, state, n=3)
    state = mutate(
        folder,
        state,
        5,
        dict(
            action="save_issue",
            issue_id=IID,
            expected_version=1,
            content=content(source, work, oid),
        ),
    )
    old_quote = quote(work)
    state = mutate(
        folder,
        state,
        6,
        dict(
            action="decide_issue",
            issue_id=IID,
            expected_version=2,
            choice="approve",
            user_confirmed=True,
            statement="Fictional choice",
            basis_sha256=sha(work),
        ),
    )
    work.write_bytes(source.read_bytes())
    state = mutate(
        folder,
        state,
        7,
        dict(
            action="record_working_version",
            expected_working_request_id=state["working_documents"][-1]["request_id"],
            file_sha256=sha(work),
            user_confirmed=True,
            statement="Fictional manual save",
        ),
    )
    data = read(folder, state)["data"]
    assert data["basis"]["working_status"] == "same_bytes"
    assert (
        data["issues"][0]["quote_status"]["working"] == "verified_historical_snapshot"
    )
    assert data["issues"][0]["issue"]["content"]["working_quote"] == old_quote
    assert data["issues"][0]["observation"]["decision_status"] == "basis_changed"


def test_frozen_sent_basis_and_cross_round_selection(case, demo_dir):
    folder, source, state = case
    sent = folder / "fictional-sent.docx"
    sent.write_bytes((demo_dir / "round-2-counterparty-redline.docx").read_bytes())
    state = mutate(
        folder,
        state,
        3,
        dict(
            action="record_sent",
            path=str(sent),
            file_sha256=sha(sent),
            user_confirmed=True,
            statement="Fictional test-only sent assertion",
        ),
    )
    next_rid = "round_" + "2" * 32
    next_iid = "issue_" + "2" * 32
    for n, cmd in [
        (
            4,
            dict(
                action="start_round",
                previous_round_id=RID,
                path=str(source),
                file_sha256=sha(source),
            ),
        ),
        (
            5,
            dict(
                action="save_issue",
                issue_id=next_iid,
                expected_version=None,
                content={**content(source), "sent_quote": quote(sent)},
            ),
        ),
    ]:
        state = server.mutate_deal_state(
            str(folder), state["revision"], f"{n:032x}", dict(round_id=next_rid, **cmd)
        )
    # A later send in the preceding round must not change the next round's basis.
    state = mutate(
        folder,
        state,
        6,
        dict(
            action="record_sent",
            path=str(source),
            file_sha256=sha(source),
            user_confirmed=True,
            statement="A different fictional send",
        ),
    )
    data = read(folder, state, [next_iid], round_id=next_rid)["data"]
    assert data["basis"]["sent_basis"]["file_sha256"] == sha(sent) != sha(source)
    assert data["round_issue_count"] == 1 and data["omitted_issue_ids"] == []
    assert data["issues"][0]["issue"]["content"]["sent_quote"] == quote(sent)
    with pytest.raises(p.PositionError, match="issue_missing"):
        read(folder, state, [next_iid, IID], round_id=next_rid)


@pytest.mark.parametrize("version", [2, 3, 4, 5, 6])
def test_old_and_current_store_versions_read_without_rewrite(case, version):
    folder, _, _ = case
    path = folder / ".veqtor" / d.STORE_NAME
    store = json.loads(path.read_bytes())
    store["schema_version"] = f"deal_rounds_store.v{version}"
    if version >= 4:
        store.update(outgoing_packages=[], outgoing_events=[])
    if version >= 5:
        store["working_copy_plans"] = []
    store["revision"] = p._revision(store)
    path.write_bytes(p._json_bytes(store))
    before = inventory(folder)
    read(folder, store)
    assert inventory(folder) == before


def test_v1_without_issues_refuses_without_upgrade(case):
    folder, _, _ = case
    path = folder / ".veqtor" / d.STORE_NAME
    store = json.loads(path.read_bytes())
    store["schema_version"] = "deal_rounds_store.v1"
    store["requests"] = store["requests"][:1]
    for key in ("operations", "working_documents", "issue_events"):
        del store[key]
    store["revision"] = p._revision(store)
    path.write_bytes(p._json_bytes(store))
    before = inventory(folder)
    with pytest.raises(p.PositionError, match="issue_missing"):
        read(folder, store)
    assert inventory(folder) == before


def test_selected_operation_is_observed_and_not_truncated_at_budget(case, monkeypatch):
    folder, source, state = case
    state, work, oid = link_copy(folder, source, state, n=3)
    state = mutate(
        folder,
        state,
        5,
        dict(
            action="save_issue",
            issue_id=IID,
            expected_version=1,
            content=content(source, work, oid),
        ),
    )
    data = read(folder, state)["data"]
    assert len(data["operations"]) == 1
    assert data["operations"][0]["observation"]["status"] == "completed"
    assert data["operations"][0]["observation"]["verification"] == "matched"
    monkeypatch.setattr(workflow, "MAX_OPERATION_OBSERVATIONS", 0)
    with pytest.raises(p.PositionError, match="resource_limit_exceeded"):
        read(folder, state)


def test_bilingual_cells_and_line_breaks_are_exact_separate_sources(case):
    import io
    import zipfile
    from lxml import etree
    from veqtor_docx._ooxml import w as tag

    folder, source, _ = case
    matter = folder / "bilingual"
    matter.mkdir()
    target = matter / "incoming.docx"
    with zipfile.ZipFile(source) as package:
        parts = {i.filename: package.read(i) for i in package.infolist()}
    document = etree.fromstring(parts["word/document.xml"])
    body = document.find(tag("body"))
    table = etree.Element(tag("tbl"))
    etree.SubElement(table, tag("tblPr"))
    grid = etree.SubElement(table, tag("tblGrid"))
    for _ in range(2):
        etree.SubElement(grid, tag("gridCol")).set(tag("w"), "4000")
    row = etree.SubElement(table, tag("tr"))
    texts = ["Supply agreement", "Договор поставки"]
    for text in texts:
        para = etree.SubElement(etree.SubElement(row, tag("tc")), tag("p"))
        run = etree.SubElement(para, tag("r"))
        etree.SubElement(run, tag("t")).text = text
        etree.SubElement(run, tag("br"))
        etree.SubElement(run, tag("t")).text = "2026"
    body.insert(0, table)
    parts["word/document.xml"] = etree.tostring(
        document, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as package:
        for name, payload in parts.items():
            package.writestr(name, payload)
    target.write_bytes(out.getvalue())
    state = mutate(
        matter,
        None,
        1,
        dict(
            action="start_round",
            previous_round_id=None,
            path=str(target),
            file_sha256=sha(target),
        ),
    )
    paragraphs = inspect_document(str(target), "browse", max_items=2)["paragraphs"]
    ids = []
    for n, paragraph in enumerate(paragraphs, 2):
        ids.append(f"issue_{n:032x}")
        data = content(target)
        data["incoming_quote"] = dict(
            file_sha256=sha(target),
            paragraph_ref=paragraph["paragraph_ref"],
            start=0,
            text=paragraph["text"],
        )
        state = mutate(
            matter,
            state,
            n,
            dict(
                action="save_issue",
                issue_id=ids[-1],
                expected_version=None,
                content=data,
            ),
        )
    before = inventory(matter)
    rows = read(matter, state, ids)["data"]["issues"]
    assert [r["issue"]["content"]["incoming_quote"]["text"] for r in rows] == [
        t + "\n2026" for t in texts
    ]
    assert (
        rows[0]["issue"]["content"]["incoming_quote"]["paragraph_ref"]
        != rows[1]["issue"]["content"]["incoming_quote"]["paragraph_ref"]
    )
    assert inventory(matter) == before
