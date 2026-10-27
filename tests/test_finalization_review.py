# SPDX-License-Identifier: Apache-2.0
"""A clean-looking file never hides unresolved negotiation state."""
import hashlib
import json
import os
import subprocess
import sys

import jsonschema
import pytest
from mcp import Client

from test_clean_copy import _package, _root, _revision
from test_deal_workflow import RID, IID, content, edit, link_copy, sha
from test_round_issue_brief import inventory
from veqtor_mcp import server, positions as p, deal_state as d, _deal_workflow as w
from veqtor_mcp import _finalization_review as f
from veqtor_mcp._finalization_review_contract import RESULT, MANUAL_CHECKS


def mutate(folder, state, n, command):
    return server.mutate_deal_state(str(folder), state["revision"] if state else None,
        f"{n:032x}", {"round_id": RID, **command})


@pytest.fixture
def case(tmp_path, demo_dir):
    source = tmp_path / "incoming.docx"
    source.write_bytes(_package(demo_dir, _root('<w:p><w:r><w:t>Supply agreement</w:t></w:r></w:p>')))
    state = mutate(tmp_path, None, 1, dict(action="start_round", previous_round_id=None,
        path=str(source), file_sha256=sha(source)))
    return tmp_path, source, state


def args(folder, state, digest=None):
    works = [r for r in state["working_documents"] if r["round_id"] == RID]
    return dict(folder=str(folder), expected_revision=state["revision"], expected_positions_revision=None,
        round_id=RID, basis_sha256=digest or (works[-1]["file_sha256"] if works else state["rounds"][0]["incoming_sha256"]))


def review(folder, state, **kwargs):
    result = server.read_finalization_review(**(args(folder, state) | kwargs))
    jsonschema.validate(result, RESULT)
    assert result["review_sha256"] == hashlib.sha256(p._json_bytes(result["data"])).hexdigest()
    assert result["readiness"] == "incomplete"
    assert result["data"]["required_manual_checks"] == MANUAL_CHECKS
    return result


def add_issue(folder, source, state, n=2, iid=IID, **overrides):
    return mutate(folder, state, n, dict(action="save_issue", issue_id=iid, expected_version=None,
        content={**content(source), "remaining_question": None, **overrides}))


def decide(folder, state, choice="approve", n=3):
    return mutate(folder, state, n, dict(action="decide_issue", issue_id=IID, expected_version=1,
        choice=choice, user_confirmed=True, statement="Fictional lawyer issue decision only",
        basis_sha256=args(folder, state)["basis_sha256"]))


def test_clean_file_has_no_automatic_obstacles_but_is_not_final(case):
    folder, source, state = case
    before = inventory(folder)
    result = review(folder, state)
    assert result["data"]["document"] == dict(inventory_complete=True, revision_count=0,
        coverage_blockers=[], clean_probe="passed_revision_free", clean_probe_error=None)
    assert result["data"]["automatic_blockers"] == []
    assert result["data"]["basis"]["working_status"] == "not_selected"
    assert result["data"]["included_round_ids"] == [RID]
    assert result["data"]["issues"] == [] and result["data"]["operations"] == []
    assert result["record_id"] is None and result["record_status"] == "disabled"
    assert inventory(folder) == before


@pytest.mark.parametrize("choice", [None, "defer", "approve", "reject"])
@pytest.mark.parametrize("remaining", [None, "Await bank details"])
def test_comment_free_document_does_not_close_a_saved_question(case, choice, remaining):
    folder, source, state = case
    state = add_issue(folder, source, state, remaining_question=remaining)
    if choice:
        state = decide(folder, state, choice)
    before = inventory(folder)
    data = review(folder, state)["data"]
    expected = ([] if choice in {"approve", "reject"} else ["deferred" if choice else "undecided"])
    if remaining:
        expected.append("remaining_question")
    assert data["issues"][0]["blockers"] == expected
    assert ("unresolved_issues" in data["automatic_blockers"]) == bool(expected)
    assert data["issues"][0]["issue"] == state["issues"][0]
    assert inventory(folder) == before


def test_old_and_other_round_questions_are_not_filtered_out(case):
    folder, source, state = case
    state = add_issue(folder, source, state)
    # Two independent first-round entries in the same saved matter are both
    # included; neither a selected round nor a clean document hides a question.
    other = "round_" + "2" * 32
    state = mutate(folder, state, 3, dict(action="start_round", round_id=other,
        previous_round_id=None, path=str(source), file_sha256=sha(source)))
    state = mutate(folder, state, 4, dict(action="save_issue", round_id=other,
        issue_id="issue_" + "2" * 32, expected_version=None,
        content={**content(source), "title": "Other saved round", "remaining_question": None}))
    data = review(folder, state, round_id=other)["data"]
    assert data["included_round_ids"] == [RID, other]
    assert [i["issue"]["round_id"] for i in data["issues"]] == [RID, other]
    assert all(i["blockers"] == ["undecided"] for i in data["issues"])


def test_later_round_and_old_decisions_are_explicit(case, demo_dir):
    folder, source, state = case
    state = decide(folder, add_issue(folder, source, state))
    state = mutate(folder, state, 4, dict(action="record_sent", path=str(source), file_sha256=sha(source),
        user_confirmed=True, statement="Fictional exact send only"))
    incoming = folder / "returned.docx"
    incoming.write_bytes(_package(demo_dir, _root('<w:p><w:r><w:t>Returned wording</w:t></w:r></w:p>')))
    other = "round_" + "2" * 32
    state = mutate(folder, state, 5, dict(action="start_round", round_id=other,
        previous_round_id=RID, path=str(incoming), file_sha256=sha(incoming)))
    earlier = review(folder, state)["data"]
    assert "selected_round_has_successor" in earlier["automatic_blockers"]
    later = review(folder, state, round_id=other, basis_sha256=sha(incoming))["data"]
    assert later["issues"][0]["blockers"] == ["decision_basis_changed"]
    assert "unresolved_issues" in later["automatic_blockers"]


@pytest.mark.parametrize("fault", ["changed", "missing", "symlink", "hardlink"])
def test_working_copy_must_still_match_saved_basis(case, fault):
    folder, source, state = case
    state, work, _ = link_copy(folder, source, state)
    good = review(folder, state)
    assert good["data"]["basis"]["working_status"] == "same_bytes"
    if fault == "changed":
        work.write_bytes(source.read_bytes())
    else:
        work.unlink()
        if fault == "symlink":
            work.symlink_to(source)
        elif fault == "hardlink":
            os.link(source, work)
    before = inventory(folder)
    changed = review(folder, state)
    status = "changed" if fault == "changed" else "unavailable"
    assert changed["data"]["basis"]["working_status"] == status
    assert "working_file_" + status in changed["data"]["automatic_blockers"]
    assert changed["review_sha256"] != good["review_sha256"]
    assert inventory(folder) == before


def test_unlinked_operation_is_still_reviewed_and_missing_journal_blocks(case):
    folder, source, state = case
    state, _, oid = link_copy(folder, source, state)
    # There are no issues and thus no issue-operation links.
    data = review(folder, state)["data"]
    assert data["operations"][0]["link"]["operation_id"] == oid
    assert data["operations"][0]["outcome"] == "recorded_success"
    assert "unresolved_operations" not in data["automatic_blockers"]
    # Move only this test operation's journal aside, preserving its contents.
    journal_dir = source.parent / ".veqtor" / "operations"
    assert journal_dir.exists()
    journal_dir.rename(journal_dir.with_name("operations-test-unavailable"))
    before = inventory(folder)
    data = review(folder, state)["data"]
    assert data["operations"][0]["outcome"] == "unresolved"
    assert "unresolved_operations" in data["automatic_blockers"]
    assert inventory(folder) == before


@pytest.mark.parametrize("status,publication,verification,error,expected", [
    ("prepared_only", None, None, None, "unresolved"),
    ("intent_changed", None, None, None, "unresolved"),
    ("completed", "uncertain", "not_checked", "save_failed", "unresolved"),
    ("completed", "published", "mismatch", None, "unresolved"),
    ("completed", "published", "matched", "backup_failed", "unresolved"),
    ("completed", "not_published", "not_checked", "save_failed", "recorded_no_publication"),
])
def test_operation_outcome_classification_is_not_inferred_from_file(case, monkeypatch,
        status, publication, verification, error, expected):
    folder, source, state = case
    state, _, _ = link_copy(folder, source, state)
    original = w.operation_observation
    def observe(op, matter):
        result = original(op, matter)
        return {**result, "status": status, "publication_status": publication,
            "verification": verification, "operation_error": error}
    monkeypatch.setattr(w, "operation_observation", observe)
    data = review(folder, state)["data"]
    assert data["operations"][0]["outcome"] == expected
    assert ("unresolved_operations" in data["automatic_blockers"]) == (expected == "unresolved")


def test_concession_conditions_and_historical_link_survive_position_update(case):
    folder, source, state = case
    pid = "pos_" + "a" * 32
    terms = dict(title="Payment", desired_outcome="30 days", fallback="60 days",
        fallback_conditions="Only with 30% advance", rationale="Fictional private reason",
        related_position_ids=[], content_origin="user_instruction", business_decision="pending", sources=[])
    positions = server.mutate_deal_positions(str(folder), None, [dict(op="create", position_id=pid, content=terms)])
    link = dict(matter_id=positions["matter_id"], revision=positions["revision"], history_sequence=1,
        position_id=pid, version=1)
    state = add_issue(folder, source, state, position=link)
    state = decide(folder, state)
    old = positions["revision"]
    positions = server.mutate_deal_positions(str(folder), old, [dict(op="update", position_id=pid,
        expected_version=1, content={**terms, "fallback_conditions": "Only with 50% advance"})])
    with pytest.raises(p.PositionError, match="positions_revision_conflict"):
        review(folder, state, expected_positions_revision=old)
    data = review(folder, state, expected_positions_revision=positions["revision"])["data"]
    assert data["issues"][0]["position_version"]["content"]["fallback_conditions"] == "Only with 30% advance"
    assert data["positions"][0]["content"]["fallback_conditions"] == "Only with 50% advance"
    assert data["issues"][0]["blockers"] == ["position_basis_changed"]
    # A later issue version removes the link; both histories must still expose
    # the original conditional concession for the lawyer's reconciliation.
    state = mutate(folder, state, 4, dict(action="save_issue", issue_id=IID, expected_version=1,
        content={**content(source), "position": None, "remaining_question": None}))
    data = review(folder, state, expected_positions_revision=positions["revision"])["data"]
    assert data["issues"][0]["issue"]["content"]["position"] is None
    assert data["issue_history"][0]["event"]["content"]["position"] == link
    assert data["issue_history"][0]["position_version"]["content"]["fallback_conditions"] == "Only with 30% advance"
    assert data["position_history"][0]["position"]["content"]["fallback_conditions"] == "Only with 30% advance"
    assert len(data["position_history"]) == 2


@pytest.mark.parametrize("kind", ["revisions", "unknown", "protected"])
def test_document_gate_never_accepts_revisions_or_drops_unknown_parts(tmp_path, demo_dir, monkeypatch, kind):
    xml = '<w:p><w:r><w:t>Supply agreement</w:t></w:r>'
    if kind == "revisions":
        xml += _revision("ins", "Fictional pending proposal")
    xml += '</w:p>'
    extra = {"customXml/unknown.xml": b'<unknown xmlns="urn:uncovered-test"/>'} if kind == "unknown" else None
    if kind == "protected":
        extra = {"word/settings.xml": b'<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:documentProtection w:edit="readOnly" w:enforcement="1"/></w:settings>'}
    source = tmp_path / "incoming.docx"
    source.write_bytes(_package(demo_dir, _root(xml), extra))
    state = mutate(tmp_path, None, 1, dict(action="start_round", previous_round_id=None,
        path=str(source), file_sha256=sha(source)))
    if kind != "protected":
        def forbidden(*a, **kw):
            pytest.fail("No clean acceptance when revisions or uncovered parts remain")
        monkeypatch.setattr(f.clean_copy, "prepare_clean_copy", forbidden)
    before = inventory(tmp_path)
    data = review(tmp_path, state)["data"]
    assert {"revisions": "pending_revisions", "unknown": "package_coverage_blocked",
        "protected": "clean_probe_refused"}[kind] in data["automatic_blockers"]
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("fault", ["revision", "positions", "round", "basis", "malformed"])
def test_stale_or_invalid_inputs_refuse_whole_request(case, fault):
    folder, _, state = case
    kwargs = args(folder, state)
    key = {"revision": "expected_revision", "positions": "expected_positions_revision",
        "round": "round_id", "basis": "basis_sha256", "malformed": "basis_sha256"}[fault]
    kwargs[key] = "round_" + "9" * 32 if fault == "round" else "f" * 64
    if fault == "malformed":
        kwargs[key] += "\n"
    before = inventory(folder)
    with pytest.raises(p.PositionError):
        server.read_finalization_review(**kwargs)
    assert inventory(folder) == before


def test_absent_store_is_not_created(tmp_path):
    with pytest.raises(p.PositionError, match="round_missing"):
        server.read_finalization_review(str(tmp_path), "0" * 64, None, RID, "0" * 64)
    assert list(tmp_path.iterdir()) == []


def test_twenty_first_actual_linked_operation_refuses_complete_review(case):
    folder, source, state = case
    for n in range(21):
        saved = edit(source, folder / f"draft-{n}.docx")
        state = mutate(folder, state, n + 2, dict(action="link_operation", source_path=str(source),
            operation_id=saved["operation_journal"]["operation_id"]))
    assert len(state["operations"]) == 21
    before = inventory(folder)
    with pytest.raises(p.PositionError, match="resource_limit_exceeded"):
        review(folder, state)
    assert inventory(folder) == before


@pytest.mark.parametrize("fault", ["operations", "snapshot", "quote"])
def test_limits_or_invalid_evidence_never_return_partial_review(case, monkeypatch, fault):
    folder, source, state = case
    state = add_issue(folder, source, state)
    if fault == "operations":
        state, _, _ = link_copy(folder, source, state, n=3)
        monkeypatch.setattr(f, "MAX_REVIEW_OPERATIONS", 0)
    elif fault == "snapshot":
        monkeypatch.setattr(d, "MAX_DOCUMENT_BYTES", 1)
    else:
        path = folder / ".veqtor" / d.STORE_NAME
        value = json.loads(path.read_bytes())
        value["issue_events"][0]["content"]["incoming_quote"]["text"] = "Invented quote"
        value["revision"] = p._revision(value)
        path.write_bytes(p._json_bytes(value))
        state = {**state, "revision": value["revision"]}
    before = inventory(folder)
    with pytest.raises(p.PositionError, match="quote_mismatch" if fault == "quote" else "resource_limit_exceeded"):
        review(folder, state)
    assert inventory(folder) == before


def test_cold_read_uses_immutable_snapshot_not_deleted_original(case):
    folder, source, state = case
    expected = review(folder, state)
    source.unlink()
    before = inventory(folder)
    script = 'import json,sys;from veqtor_mcp.server import read_finalization_review;print(json.dumps(read_finalization_review(**json.loads(sys.argv[1]))))'
    actual = json.loads(subprocess.check_output([sys.executable, "-c", script, json.dumps(args(folder, state))], text=True))
    assert actual == expected
    assert inventory(folder) == before


@pytest.mark.anyio
async def test_public_contract_has_no_selection_or_approval_input(case, monkeypatch):
    folder, _, state = case
    before = inventory(folder)
    monkeypatch.delenv("VEQTOR_DISABLE_DECISION_RECORD", raising=False)
    async with Client(server.mcp) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "read_finalization_review")
        assert tool.annotations.read_only_hint and tool.annotations.idempotent_hint
        assert set(tool.input_schema["properties"]) == set(args(folder, state))
        result = await client.call_tool("read_finalization_review", args(folder, state))
    assert not result.is_error
    jsonschema.validate(result.structured_content, RESULT)
    assert result.structured_content["readiness"] == "incomplete"
    assert inventory(folder) == before
