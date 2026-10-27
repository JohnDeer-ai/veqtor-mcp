# SPDX-License-Identifier: Apache-2.0
"""Exact-basis assertions, checked final candidates and historical recovery."""

from copy import deepcopy
import json
import subprocess
import sys

import jsonschema
import pytest
from lxml import etree
from mcp import Client

from test_deal_workflow import content, sha
from test_outgoing_cleanup import fictional_parts, pack, unpack, REMOVE_PROPS
from test_round_issue_brief import inventory
from veqtor_docx._ooxml import w
from veqtor_docx import round_snapshots as snapshots
from veqtor_mcp import positions as p, server
from veqtor_mcp._deal_state_contract import RESULT

RID = "round_" + "1" * 32
IID = "issue_" + "1" * 32


def change(folder, state, action, *, rid=RID, request_id=None, **values):
    key = request_id or f"{len(state['requests']) + 1 if state else 1:032x}"
    result = server.mutate_deal_state(
        str(folder),
        state["revision"] if state else None,
        key,
        dict(action=action, round_id=rid, **values),
    )
    jsonschema.validate(result, RESULT)
    return result


def make_case(folder, *, revisions=False, extra=None):
    parts = fictional_parts()
    root = etree.fromstring(parts["word/document.xml"])
    if not revisions:
        revision = next(root.iter(w("ins")))
        revision.getparent().remove(revision)
    parts["word/document.xml"] = etree.tostring(root)
    parts.update(extra or {})
    source = folder / "source.docx"
    source.write_bytes(pack(parts))
    state = change(
        folder,
        None,
        "start_round",
        previous_round_id=None,
        path=str(source),
        file_sha256=sha(source),
    )
    return folder, source, state


@pytest.fixture
def case(tmp_path):
    return make_case(tmp_path)


def command(folder, state, *, rid=RID, positions=None):
    row = next(r for r in state["rounds"] if r["round_id"] == rid)
    works = [r for r in state["working_documents"] if r["round_id"] == rid]
    digest = works[-1]["file_sha256"] if works else row["incoming_sha256"]
    review = server.read_finalization_review(
        str(folder),
        state["revision"],
        positions["revision"] if positions else None,
        rid,
        digest,
    )
    return dict(
        file_sha256=digest,
        expected_positions_revision=positions["revision"] if positions else None,
        expected_review_sha256=review["review_sha256"],
        user_confirmed=True,
        statement="Fictional lawyer confirms this exact basis as agreed; no real agreement.",
        issue_resolutions=[
            dict(
                issue_id=i["issue"]["issue_id"],
                version=i["issue"]["version"],
                statement="Fictional issue resolved on the selected exact basis.",
            )
            for i in review["data"]["issues"]
        ],
        position_resolutions=[
            dict(
                position_id=r["position_id"],
                version=r["version"],
                statement="Fictional business outcome and conditions reconciled on this basis.",
            )
            for r in review["data"]["positions"]
            if r["lifecycle"] == "active"
        ],
        manual_checks={
            k: "Fictional completed lawyer check: " + k
            for k in [
                "known_questions",
                "historical_conditions",
                "requisites",
                "annexes",
                "unfilled_places",
                "uncovered_areas",
            ]
        },
    )


def agree(folder, state, **kwargs):
    return change(
        folder,
        state,
        "record_final_basis_agreement",
        **command(folder, state, **kwargs),
        **({"rid": kwargs["rid"]} if "rid" in kwargs else {}),
    )


def prepare(folder, state, **kwargs):
    return change(
        folder,
        state,
        "prepare_final_document",
        **{
            "agreement_request_id": state["final_basis_agreements"][-1]["request_id"],
            "path": "final-candidate",
            "name": "Final-candidate.docx",
            "comments_policy": {"mode": "remove_all"},
            "properties_policy": REMOVE_PROPS,
            **kwargs,
        },
    )


def publish(folder, state, **kwargs):
    row = state["final_documents"][-1]
    return change(
        folder,
        state,
        "publish_final_document",
        **{
            "document_request_id": row["request_id"],
            "plan_sha256": row["plan_sha256"],
            "user_confirmed": True,
            "statement": "Publish the exact reviewed fictional final candidate only.",
            **kwargs,
        },
    )


def test_exact_agreement_preview_publish_verify_and_cold_resume(case):
    folder, source, state = case
    original = source.read_bytes()
    state = change(
        folder,
        state,
        "save_issue",
        issue_id=IID,
        expected_version=None,
        content=content(source),
    )
    original_issues = deepcopy(state["issue_events"])
    state = agree(folder, state)
    assert (
        json.loads((folder / ".veqtor/deal-rounds.json").read_bytes())["schema_version"]
        == "deal_rounds_store.v8"
    )
    assert (
        state["issue_events"] == original_issues
        and state["issues"][0]["decision"] is None
    )
    assert state["final_basis_agreements"][0]["file_sha256"] == sha(source)
    state = prepare(folder, state)
    row = state["final_documents"][0]
    report = json.loads(row["report_utf8"])
    assert row["stage"] == "candidate_for_word_review"
    assert report["clean_draft"]["accepted_revision_count"] == 0
    assert not (folder / "final-candidate").exists()
    assert (
        "Fictional internal author" in row["report_utf8"]
    )  # complete internal cleanup preview
    state = publish(folder, state)
    output = folder / row["path"] / row["name"]
    assert sha(output) == row["file_sha256"] and output.read_bytes() != original
    parts = unpack(output.read_bytes())
    assert not any(
        n.tag
        in {
            w("comment"),
            w("commentReference"),
            w("commentRangeStart"),
            w("commentRangeEnd"),
        }
        for name, raw in parts.items()
        if name.endswith(".xml")
        for n in etree.fromstring(raw).iter()
    )
    assert b"Fictional internal author" not in parts["docProps/core.xml"]
    assert b"Keep this title" in parts["docProps/core.xml"]
    assert (
        "".join(etree.fromstring(parts["word/document.xml"]).itertext())
        == "Delivery within thirty days."
    )
    assert source.read_bytes() == original
    state = change(
        folder,
        state,
        "record_final_document_verification",
        document_request_id=row["request_id"],
        plan_sha256=row["plan_sha256"],
    )
    assert [e["status"] for e in state["final_document_events"]] == [
        "published",
        "observed_match",
    ]
    assert (
        state["final_basis_observations"][0]["status"]
        == "saved_context_matches_live_checks_required"
    )
    before = inventory(folder)
    script = "import json,sys;from veqtor_mcp.server import read_deal_state;print(json.dumps(read_deal_state(sys.argv[1])))"
    cold = json.loads(
        subprocess.check_output([sys.executable, "-c", script, str(folder)], text=True)
    )
    assert (
        cold["final_documents"] == state["final_documents"]
        and cold["final_basis_agreements"] == state["final_basis_agreements"]
    )
    assert inventory(folder) == before and not cold["rounds"][0]["sent_documents"]
    assert (
        server.read_finalization_review(
            str(folder), state["revision"], None, RID, sha(source)
        )["readiness"]
        == "incomplete"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing_issue",
        "duplicate_issue",
        "wrong_version",
        "foreign_issue",
        "missing_check",
        "false_confirmation",
        "stale_review",
        "stale_positions",
        "pending_revisions",
        "unknown_part",
    ],
)
def test_incomplete_agreement_never_saves_state_or_output(tmp_path, fault):
    folder, source, state = make_case(
        tmp_path,
        revisions=fault == "pending_revisions",
        extra={"customXml/unknown.xml": b'<unknown xmlns="urn:private-uncovered"/>'}
        if fault == "unknown_part"
        else None,
    )
    state = change(
        folder,
        state,
        "save_issue",
        issue_id=IID,
        expected_version=None,
        content=content(source),
    )
    values = command(folder, state)
    if fault == "missing_issue":
        values["issue_resolutions"] = []
    if fault == "duplicate_issue":
        values["issue_resolutions"] *= 2
    if fault == "wrong_version":
        values["issue_resolutions"][0]["version"] = 2
    if fault == "foreign_issue":
        values["issue_resolutions"][0]["issue_id"] = "issue_" + "9" * 32
    if fault == "missing_check":
        del values["manual_checks"]["annexes"]
    if fault == "false_confirmation":
        values["user_confirmed"] = False
    if fault == "stale_review":
        values["expected_review_sha256"] = "f" * 64
    if fault == "stale_positions":
        values["expected_positions_revision"] = "f" * 64
    before = inventory(folder)
    with pytest.raises(p.PositionError):
        change(folder, state, "record_final_basis_agreement", **values)
    assert inventory(folder) == before


def test_historical_issue_and_pending_business_are_resolved_only_for_new_basis(case):
    folder, source, state = case
    pid = "pos_" + "1" * 32
    terms = dict(
        title="Payment",
        desired_outcome="30 days",
        fallback="60 days",
        fallback_conditions="Only with advance",
        rationale="Fictional",
        related_position_ids=[],
        content_origin="user_instruction",
        business_decision="pending",
        sources=[],
    )
    positions = server.mutate_deal_positions(
        str(folder), None, [dict(op="create", position_id=pid, content=terms)]
    )
    link = dict(
        matter_id=positions["matter_id"],
        revision=positions["revision"],
        history_sequence=1,
        position_id=pid,
        version=1,
    )
    state = change(
        folder,
        state,
        "save_issue",
        issue_id=IID,
        expected_version=None,
        content={**content(source), "position": link},
    )
    state = change(
        folder,
        state,
        "decide_issue",
        issue_id=IID,
        expected_version=1,
        choice="defer",
        user_confirmed=True,
        statement="Fictional deferred question",
        basis_sha256=sha(source),
    )
    state = change(
        folder,
        state,
        "record_sent",
        path=str(source),
        file_sha256=sha(source),
        user_confirmed=True,
        statement="Fictional test send",
    )
    incoming = folder / "returned.docx"
    returned_parts = unpack(source.read_bytes())
    returned_parts["word/document.xml"] = returned_parts["word/document.xml"].replace(
        b"thirty", b"sixty"
    )
    incoming.write_bytes(pack(returned_parts))
    assert sha(incoming) != sha(source)
    rid = "round_" + "2" * 32
    state = change(
        folder,
        state,
        "start_round",
        rid=rid,
        previous_round_id=RID,
        path=str(incoming),
        file_sha256=sha(incoming),
    )
    values = command(folder, state, rid=rid, positions=positions)
    omitted = {**values, "position_resolutions": []}
    before = inventory(folder)
    with pytest.raises(p.PositionError, match="final_resolution_mismatch"):
        change(folder, state, "record_final_basis_agreement", rid=rid, **omitted)
    assert inventory(folder) == before
    state = change(folder, state, "record_final_basis_agreement", rid=rid, **values)
    assert state["issues"][0]["decision"]["choice"] == "defer"
    assert state["final_basis_agreements"][0]["issue_resolutions"][0]["issue_id"] == IID
    assert (
        server.read_deal_positions(str(folder))["positions"][0]["content"][
            "business_decision"
        ]
        == "pending"
    )
    assert not state["final_documents"]


@pytest.mark.parametrize(
    "change_kind", ["issue", "sent", "positions", "round", "working"]
)
def test_changed_basis_invalidates_agreement_and_replay_does_not_renew_it(
    case, change_kind
):
    folder, source, state = case
    values = command(folder, state)
    key = f"{len(state['requests']) + 1:032x}"
    state = change(folder, state, "record_final_basis_agreement", **values)
    if change_kind == "issue":
        state = change(
            folder,
            state,
            "save_issue",
            issue_id=IID,
            expected_version=None,
            content=content(source),
        )
    if change_kind == "sent":
        state = change(
            folder,
            state,
            "record_sent",
            path=str(source),
            file_sha256=sha(source),
            user_confirmed=True,
            statement="Fictional send",
        )
    if change_kind == "positions":
        server.mutate_deal_positions(
            str(folder),
            None,
            [
                dict(
                    op="create",
                    position_id="pos_" + "1" * 32,
                    content=dict(
                        title="New position",
                        desired_outcome="30 days",
                        fallback=None,
                        fallback_conditions=None,
                        rationale=None,
                        related_position_ids=[],
                        content_origin="user_instruction",
                        business_decision="pending",
                        sources=[],
                    ),
                )
            ],
        )
        state = server.read_deal_state(str(folder))
    if change_kind == "round":
        state = change(
            folder,
            state,
            "start_round",
            rid="round_" + "2" * 32,
            previous_round_id=None,
            path=str(source),
            file_sha256=sha(source),
        )
    if change_kind == "working":
        work = folder / "working.docx"
        work.write_bytes(source.read_bytes())
        state = change(
            folder,
            state,
            "import_working_document",
            path="working.docx",
            incoming_basis_sha256=sha(source),
            file_sha256=sha(work),
            user_confirmed=True,
            statement="Fictional work selection",
        )
    assert state["final_basis_observations"][0]["status"] == "saved_context_changed"
    before = inventory(folder)
    replay = change(
        folder, state, "record_final_basis_agreement", request_id=key, **values
    )
    assert replay["final_basis_observations"][0]["status"] == "saved_context_changed"
    with pytest.raises(p.PositionError, match="final_basis_changed"):
        prepare(folder, replay)
    assert inventory(folder) == before


def test_lost_state_commit_after_publication_recovers_without_republication(
    case, monkeypatch
):
    folder, source, state = case
    state = prepare(folder, agree(folder, state))
    row = state["final_documents"][0]
    original = p._publish

    def fail(*args, **kwargs):
        raise OSError("injected state commit failure")

    monkeypatch.setattr(p, "_publish", fail)
    with pytest.raises(
        snapshots.SnapshotError, match="final_document_state_commit_uncertain"
    ) as error:
        publish(folder, state)
    assert error.value.metadata["document_request_id"] == row["request_id"]
    output = folder / row["path"] / row["name"]
    assert sha(output) == row["file_sha256"]
    monkeypatch.setattr(p, "_publish", original)
    state = server.read_deal_state(str(folder))
    assert state["final_document_events"] == []
    before = output.read_bytes()
    stamp = output.stat().st_mtime_ns
    state = change(
        folder,
        state,
        "record_final_document_verification",
        document_request_id=row["request_id"],
        plan_sha256=row["plan_sha256"],
    )
    assert state["final_document_events"][0]["status"] == "observed_match"
    assert output.read_bytes() == before and output.stat().st_mtime_ns == stamp
    with pytest.raises(p.PositionError, match="final_document_already_recorded"):
        publish(folder, state)


def test_historical_verification_does_not_restore_agreement_after_change(case):
    folder, source, state = case
    state = publish(folder, prepare(folder, agree(folder, state)))
    row = state["final_documents"][0]
    state = change(
        folder,
        state,
        "save_issue",
        issue_id=IID,
        expected_version=None,
        content=content(source),
    )
    state = change(
        folder,
        state,
        "record_final_document_verification",
        document_request_id=row["request_id"],
        plan_sha256=row["plan_sha256"],
    )
    assert state["final_basis_observations"][0]["status"] == "saved_context_changed"
    assert state["final_document_events"][-1]["status"] == "observed_match"
    with pytest.raises(p.PositionError, match="final_basis_changed"):
        prepare(folder, state, path="different-output")


@pytest.mark.parametrize(
    "fault",
    [
        "foreign_destination",
        "reserved_destination",
        "wrong_agreement",
        "preserve_comments",
        "false_publication",
        "wrong_plan",
        "changed_output",
        "extra_output",
    ],
)
def test_output_guards_preserve_source_and_foreign_data(case, fault):
    folder, source, state = case
    state = agree(folder, state)
    if fault == "foreign_destination":
        (folder / "final-candidate").mkdir()
        (folder / "final-candidate/foreign.txt").write_text("Preserve me")
    if fault in {
        "foreign_destination",
        "reserved_destination",
        "wrong_agreement",
        "preserve_comments",
    }:
        kwargs = {}
        if fault == "reserved_destination":
            kwargs["path"] = ".veqtor/final"
        if fault == "wrong_agreement":
            kwargs["agreement_request_id"] = "f" * 32
        if fault == "preserve_comments":
            kwargs["comments_policy"] = "preserve_all"
        before = inventory(folder)
        with pytest.raises(p.PositionError):
            prepare(folder, state, **kwargs)
        assert inventory(folder) == before
        return
    state = prepare(folder, state)
    row = state["final_documents"][0]
    if fault in {"false_publication", "wrong_plan"}:
        before = inventory(folder)
        with pytest.raises(p.PositionError):
            publish(
                folder,
                state,
                **(
                    {"user_confirmed": False}
                    if fault == "false_publication"
                    else {"plan_sha256": "f" * 64}
                ),
            )
        assert inventory(folder) == before
        return
    state = publish(folder, state)
    if fault == "changed_output":
        (folder / row["path"] / row["name"]).write_bytes(source.read_bytes())
    else:
        (folder / row["path"] / "foreign.txt").write_text("Do not delete")
    before = inventory(folder)
    with pytest.raises(snapshots.SnapshotError):
        change(
            folder,
            state,
            "record_final_document_verification",
            document_request_id=row["request_id"],
            plan_sha256=row["plan_sha256"],
        )
    assert inventory(folder) == before


@pytest.mark.parametrize("action", ["workflow", "outgoing", "copy"])
def test_old_mutations_preserve_v8_agreements(case, action):
    folder, source, state = case
    if action == "outgoing":
        work = folder / "working.docx"
        work.write_bytes(source.read_bytes())
        state = change(
            folder,
            state,
            "import_working_document",
            path="working.docx",
            incoming_basis_sha256=sha(source),
            file_sha256=sha(work),
            user_confirmed=True,
            statement="Fictional work",
        )
    state = agree(folder, state)
    agreements = deepcopy(state["final_basis_agreements"])
    if action == "workflow":
        state = change(
            folder,
            state,
            "save_issue",
            issue_id=IID,
            expected_version=None,
            content=content(source),
        )
    if action == "outgoing":
        state = change(
            folder,
            state,
            "prepare_outgoing_package",
            expected_working_request_id=state["working_documents"][-1]["request_id"],
            file_sha256=sha(source),
            path="ordinary-outgoing",
            redline_name="Review.docx",
            clean_draft_name=None,
            comments_policy="preserve_all",
            properties_policy="preserve_all",
        )
    if action == "copy":
        state = change(
            folder,
            state,
            "prepare_working_copy",
            incoming_basis_sha256=sha(source),
            path="new-working.docx",
        )
    assert state["final_basis_agreements"] == agreements
    assert (
        json.loads((folder / ".veqtor/deal-rounds.json").read_bytes())["schema_version"]
        == "deal_rounds_store.v8"
    )


@pytest.mark.parametrize(
    "field",
    [
        "statement",
        "issue_resolutions",
        "scope_sha256",
        "review_basis_sha256",
        "file_sha256",
    ],
)
def test_corrupt_agreement_refused_without_repair(case, field):
    folder, source, state = case
    state = agree(folder, state)
    path = folder / ".veqtor/deal-rounds.json"
    store = json.loads(path.read_bytes())
    row = store["final_basis_agreements"][0]
    row[field] = (
        [dict(issue_id=IID, version=1, statement="Invented")]
        if field == "issue_resolutions"
        else "f" * 64
    )
    store["revision"] = p._revision(store)
    path.write_bytes(p._json_bytes(store))
    before = inventory(folder)
    with pytest.raises(p.PositionError, match="storage_corrupt"):
        server.read_deal_state(str(folder))
    assert inventory(folder) == before


@pytest.mark.anyio
async def test_public_finalization_actions_keep_optional_provenance_disabled(
    case, monkeypatch
):
    folder, source, state = case
    values = command(folder, state)
    monkeypatch.delenv("VEQTOR_DISABLE_DECISION_RECORD", raising=False)
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "mutate_deal_state",
            dict(
                folder=str(folder),
                expected_revision=state["revision"],
                request_id="a" * 32,
                command=dict(
                    action="record_final_basis_agreement", round_id=RID, **values
                ),
            ),
        )
    assert not result.is_error
    jsonschema.validate(result.structured_content, RESULT)
    assert result.structured_content["record_status"] == "disabled"
    assert not (folder / ".veqtor/decision-records.jsonl").exists()


@pytest.mark.parametrize("phase", ["prepare", "publish"])
@pytest.mark.parametrize("fault", ["changed", "missing", "symlink"])
def test_live_work_drift_blocks_even_without_state_revision_change(case, phase, fault):
    folder, source, state = case
    work = folder / "working.docx"
    work.write_bytes(source.read_bytes())
    state = change(
        folder,
        state,
        "import_working_document",
        path="working.docx",
        incoming_basis_sha256=sha(source),
        file_sha256=sha(work),
        user_confirmed=True,
        statement="Fictional separate work copy",
    )
    state = agree(folder, state)
    if phase == "publish":
        state = prepare(folder, state)
    if fault == "changed":
        parts = unpack(work.read_bytes())
        parts["word/document.xml"] = parts["word/document.xml"].replace(
            b"thirty", b"sixty"
        )
        work.write_bytes(pack(parts))
    else:
        work.rename(folder / "preserved-working.docx")
        if fault == "symlink":
            work.symlink_to(source)
    current = server.read_deal_state(str(folder))
    assert current["revision"] == state["revision"]
    before = inventory(folder)
    with pytest.raises(p.PositionError, match="final_basis_blocked"):
        (prepare if phase == "prepare" else publish)(folder, current)
    assert inventory(folder) == before and not (folder / "final-candidate").exists()


def test_changed_external_original_cannot_replace_agreed_snapshot(case):
    folder, source, state = case
    state = agree(folder, state)
    digest = sha(source)
    parts = unpack(source.read_bytes())
    parts["word/document.xml"] = parts["word/document.xml"].replace(b"thirty", b"sixty")
    source.write_bytes(pack(parts))
    state = publish(folder, prepare(folder, state))
    row = state["final_documents"][0]
    assert row["source_sha256"] == digest != sha(source)
    output = unpack((folder / row["path"] / row["name"]).read_bytes())
    assert "thirty" in "".join(etree.fromstring(output["word/document.xml"]).itertext())


def test_missing_historical_position_cannot_be_overridden(case):
    folder, source, state = case
    positions = server.mutate_deal_positions(
        str(folder),
        None,
        [
            dict(
                op="create",
                position_id="pos_" + "1" * 32,
                content=dict(
                    title="Terms",
                    desired_outcome="30 days",
                    fallback="60 days",
                    fallback_conditions="With advance only",
                    rationale=None,
                    related_position_ids=[],
                    content_origin="user_instruction",
                    business_decision="pending",
                    sources=[],
                ),
            )
        ],
    )
    state = change(
        folder,
        state,
        "save_issue",
        issue_id=IID,
        expected_version=None,
        content={
            **content(source),
            "position": dict(
                matter_id=positions["matter_id"],
                revision=positions["revision"],
                history_sequence=1,
                position_id="pos_" + "1" * 32,
                version=1,
            ),
        },
    )
    # Losing the current link must not hide the earlier conditional concession.
    state = change(
        folder,
        state,
        "save_issue",
        issue_id=IID,
        expected_version=1,
        content=content(source),
    )
    (folder / ".veqtor/deal-positions.json").rename(folder / "preserved-positions.json")
    values = command(folder, state)
    before = inventory(folder)
    with pytest.raises(p.PositionError, match="final_basis_blocked"):
        change(folder, state, "record_final_basis_agreement", **values)
    assert inventory(folder) == before


@pytest.mark.parametrize(
    "collection,field",
    [
        ("final_documents", "name"),
        ("final_documents", "source_sha256"),
        ("final_documents", "report_utf8"),
        ("final_document_events", "statement"),
        ("final_document_events", "plan_sha256"),
        ("final_document_events", "status"),
    ],
)
def test_corrupt_plan_or_receipt_refused_without_repair(case, collection, field):
    folder, source, state = case
    state = publish(folder, prepare(folder, agree(folder, state)))
    path = folder / ".veqtor/deal-rounds.json"
    store = json.loads(path.read_bytes())
    store[collection][0][field] = "observed_match" if field == "status" else "f" * 64
    store["revision"] = p._revision(store)
    path.write_bytes(p._json_bytes(store))
    before = inventory(folder)
    with pytest.raises(p.PositionError, match="storage_corrupt"):
        server.read_deal_state(str(folder))
    assert inventory(folder) == before
