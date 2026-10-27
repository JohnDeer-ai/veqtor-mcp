# SPDX-License-Identifier: Apache-2.0
"""A saved Word file is reviewed as exact bytes, never silently approved."""

from pathlib import Path
from copy import deepcopy
from test_deal_finalization import change, IID
from test_deal_workflow import content, sha
from veqtor_mcp import server

import hashlib
import json

import pytest
from lxml import etree

from test_deal_finalization import make_case, agree, prepare, publish
from test_outgoing_cleanup import unpack, pack
from veqtor_docx._ooxml import w
from veqtor_docx import round_snapshots as snapshots
from veqtor_mcp import _final_saved_document as f, positions as p


def snapshot(raw):
    return snapshots.DocumentSnapshot(raw, hashlib.sha256(raw).hexdigest(), len(raw))


@pytest.fixture
def candidate(tmp_path):
    folder, _, state = make_case(tmp_path)
    state = publish(folder, prepare(folder, agree(folder, state)))
    row = state["final_documents"][-1]
    return snapshot((folder / row["path"] / row["name"]).read_bytes())


def review(candidate, parts):
    saved = snapshot(pack(parts))
    raw, digest = f.build(candidate, saved)
    return json.loads(raw), raw, digest


def test_same_bytes_remain_unconfirmed(candidate):
    raw, digest = f.build(candidate, candidate)
    report = f.validate_report(raw.decode(), digest)
    assert report["readiness"] == "lawyer_confirmation_required"
    assert report["changed_parts"] == report["automatic_blockers"] == []
    assert (
        report["comparison"]["metadata"]["observations"]["document_equivalence"]
        == "not_claimed"
    )
    assert f.build(candidate, candidate) == (raw, digest)


def test_changed_word_metadata_and_text_are_fully_visible(candidate):
    parts = unpack(candidate.payload)
    root = etree.fromstring(parts["word/document.xml"])
    next(
        root.iter(w("t"))
    ).text = "Fictional replacement requiring a new explicit legal decision."
    parts["word/document.xml"] = etree.tostring(root)
    core = etree.fromstring(parts["docProps/core.xml"])
    node = etree.SubElement(
        core,
        "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}lastModifiedBy",
    )
    node.text = "Fictional Word user"
    parts["docProps/core.xml"] = etree.tostring(core)
    report, _, _ = review(candidate, parts)
    assert {r["part_name"] for r in report["changed_parts"]} == {
        "word/document.xml",
        "docProps/core.xml",
    }
    assert "Fictional Word user" in report["properties_after"]["docProps/core.xml"]
    assert not report["comparison"]["metadata"]["observations"][
        "current_text_sequence_equal"
    ]
    assert report["readiness"] == "lawyer_confirmation_required"


@pytest.mark.parametrize(
    "node", ["commentReference", "commentRangeStart", "commentRangeEnd"]
)
def test_comments_block_confirmation(candidate, node):
    parts = unpack(candidate.payload)
    root = etree.fromstring(parts["word/document.xml"])
    etree.SubElement(next(root.iter(w("p"))), w(node), {w("id"): "0"})
    parts["word/document.xml"] = etree.tostring(root)
    report, _, _ = review(candidate, parts)
    assert "comments_remaining" in report["automatic_blockers"]


def test_revision_blocks_confirmation(candidate):
    parts = unpack(candidate.payload)
    root = etree.fromstring(parts["word/document.xml"])
    revision = etree.SubElement(
        next(root.iter(w("p"))), w("ins"), {w("id"): "2", w("author"): "Fictional"}
    )
    etree.SubElement(etree.SubElement(revision, w("r")), w("t")).text = "pending"
    parts["word/document.xml"] = etree.tostring(root)
    report, _, _ = review(candidate, parts)
    assert "pending_revisions" in report["automatic_blockers"]


def test_unknown_content_is_not_waived(candidate):
    parts = unpack(candidate.payload)
    parts["customXml/item1.xml"] = b"<private>This must not disappear.</private>"
    report, _, _ = review(candidate, parts)
    assert "package_coverage_blocked" in report["automatic_blockers"]


def test_report_hash_and_budget_are_enforced(candidate, monkeypatch):
    raw, digest = f.build(candidate, candidate)
    with pytest.raises(p.PositionError, match="storage_corrupt"):
        f.validate_report(raw.decode() + " ", digest)
    monkeypatch.setattr(f, "MAX_REPORT_BYTES", 100)
    with pytest.raises(p.PositionError):
        f.build(candidate, candidate)


@pytest.fixture
def saved_case(tmp_path):
    folder, source, state = make_case(tmp_path)
    state = publish(folder, prepare(folder, agree(folder, state)))
    row = state["final_documents"][-1]
    saved = folder / "Word-saved.docx"
    saved.write_bytes((folder / row["path"] / row["name"]).read_bytes())
    return folder, source, saved, state


def prepare_saved(folder, saved, state):
    row = state["final_documents"][-1]
    return change(
        folder,
        state,
        "prepare_final_saved_review",
        document_request_id=row["request_id"],
        plan_sha256=row["plan_sha256"],
        path=saved.name,
        file_sha256=sha(saved),
    )


def confirmation(state):
    row = state["final_saved_reviews"][-1]
    report = json.loads(row["report_utf8"])
    agreement = state["final_basis_agreements"][-1]
    return dict(
        saved_review_request_id=row["request_id"],
        report_sha256=row["report_sha256"],
        file_sha256=row["file_sha256"],
        user_confirmed=True,
        statement="Fictional lawyer explicitly confirms these final saved bytes.",
        manual_checks={
            k: "Fictional completed check: " + k
            for k in (
                "word_reopened",
                "all_pages_and_layout",
                "date_and_field_behavior",
                "properties_and_confidentiality",
                "requisites",
                "annexes",
                "unfilled_places",
                "final_agreed_text",
            )
        },
        issue_resolutions=deepcopy(agreement["issue_resolutions"]),
        position_resolutions=deepcopy(agreement["position_resolutions"]),
        changed_part_resolutions=[
            dict(
                part_name=p["part_name"],
                statement="Fictional reviewed and accepted exact part change.",
            )
            for p in report["changed_parts"]
        ],
    )


def test_confirm_exact_snapshot_and_cold_resume_detects_later_drift(saved_case):
    folder, source, saved, state = saved_case
    original = source.read_bytes()
    state = prepare_saved(folder, saved, state)
    assert not state["final_confirmations"]
    state = change(folder, state, "confirm_final_saved_document", **confirmation(state))
    row = state["final_confirmations"][-1]
    snap = next(d for d in state["documents"] if d["file_sha256"] == row["file_sha256"])
    assert Path(snap["path"]).read_bytes() == saved.read_bytes()
    assert state["final_confirmation_observations"] == [
        dict(
            confirmation_request_id=row["request_id"],
            snapshot_sha256=sha(saved),
            context_status="current_checked",
            selected_file_status="same_bytes",
        )
    ]
    saved.write_bytes(source.read_bytes())
    resumed = server.read_deal_state(str(folder))
    assert resumed["final_confirmations"] == state["final_confirmations"]
    assert (
        resumed["final_confirmation_observations"][0]["selected_file_status"]
        == "changed"
    )
    assert source.read_bytes() == original
    assert Path(snap["path"]).read_bytes() != saved.read_bytes()


@pytest.mark.parametrize(
    "missing",
    [
        "user_confirmed",
        "statement",
        "manual_checks",
        "issue_resolutions",
        "position_resolutions",
    ],
)
def test_no_implicit_final_confirmation(saved_case, missing):
    folder, _, saved, state = saved_case
    state = prepare_saved(folder, saved, state)
    values = confirmation(state)
    del values[missing]
    before = (folder / ".veqtor/deal-rounds.json").read_bytes()
    with pytest.raises(p.PositionError, match="invalid_request"):
        change(folder, state, "confirm_final_saved_document", **values)
    assert (folder / ".veqtor/deal-rounds.json").read_bytes() == before


def test_changed_text_requires_every_part_resolution(saved_case):
    folder, _, saved, state = saved_case
    parts = unpack(saved.read_bytes())
    root = etree.fromstring(parts["word/document.xml"])
    next(root.iter(w("t"))).text = "Different fictional negotiated provision."
    parts["word/document.xml"] = etree.tostring(root)
    saved.write_bytes(pack(parts))
    state = prepare_saved(folder, saved, state)
    values = confirmation(state)
    assert len(values["changed_part_resolutions"]) == 1
    before = (folder / ".veqtor/deal-rounds.json").read_bytes()
    with pytest.raises(p.PositionError, match="final_changed_parts_unresolved"):
        change(
            folder,
            state,
            "confirm_final_saved_document",
            **{**values, "changed_part_resolutions": []},
        )
    assert (folder / ".veqtor/deal-rounds.json").read_bytes() == before
    state = change(folder, state, "confirm_final_saved_document", **values)
    assert state["final_confirmations"][-1]["file_sha256"] == sha(saved)


@pytest.mark.parametrize("what", ["file", "issue", "sent", "candidate"])
def test_changed_context_or_file_refuses_confirmation(saved_case, what):
    folder, source, saved, state = saved_case
    state = prepare_saved(folder, saved, state)
    values = confirmation(state)
    if what == "file":
        saved.write_bytes(source.read_bytes())
    elif what == "issue":
        state = change(
            folder,
            state,
            "save_issue",
            issue_id=IID,
            expected_version=None,
            content=content(source),
        )
    elif what == "sent":
        state = change(
            folder,
            state,
            "record_sent",
            path=str(source),
            file_sha256=sha(source),
            user_confirmed=True,
            statement="Fictional changed context.",
        )
    else:
        row = state["final_documents"][-1]
        (folder / row["path"] / row["name"]).write_bytes(source.read_bytes())
    before = (folder / ".veqtor/deal-rounds.json").read_bytes()
    with pytest.raises((p.PositionError, snapshots.SnapshotError)):
        change(folder, state, "confirm_final_saved_document", **values)
    assert (folder / ".veqtor/deal-rounds.json").read_bytes() == before


def test_unknown_package_cannot_be_waived_by_manual_checks(saved_case):
    folder, _, saved, state = saved_case
    parts = unpack(saved.read_bytes())
    parts["customXml/item1.xml"] = b"<private>Retain me.</private>"
    saved.write_bytes(pack(parts))
    state = prepare_saved(folder, saved, state)
    assert json.loads(state["final_saved_reviews"][-1]["report_utf8"])[
        "automatic_blockers"
    ]
    with pytest.raises(p.PositionError, match="final_saved_document_blocked"):
        change(folder, state, "confirm_final_saved_document", **confirmation(state))


@pytest.mark.parametrize("after_commit", [False, True])
def test_uncertain_confirmation_exact_request_recovery(
    saved_case, monkeypatch, after_commit
):
    folder, _, saved, state = saved_case
    state = prepare_saved(folder, saved, state)
    values = confirmation(state)
    request_id = "a" * 32
    real = p._publish

    def fail(*args, **kwargs):
        if after_commit:
            real(*args, **kwargs)
        raise OSError("fictional state failure")

    monkeypatch.setattr(p, "_publish", fail)
    with pytest.raises(p.PositionError, match="storage_failure"):
        change(
            folder,
            state,
            "confirm_final_saved_document",
            request_id=request_id,
            **values,
        )
    monkeypatch.setattr(p, "_publish", real)
    observed = server.read_deal_state(str(folder))
    assert len(observed["final_confirmations"]) == int(after_commit)
    retried = change(
        folder,
        observed,
        "confirm_final_saved_document",
        request_id=request_id,
        **values,
    )
    assert len(retried["final_confirmations"]) == 1
    assert retried["final_confirmations"][0]["request_id"] == request_id


def test_old_requests_preserve_v9_state_and_late_changes_are_historical(saved_case):
    folder, source, saved, state = saved_case
    state = prepare_saved(folder, saved, state)
    values = confirmation(state)
    state = change(folder, state, "confirm_final_saved_document", **values)
    state = change(
        folder,
        state,
        "save_issue",
        issue_id=IID,
        expected_version=None,
        content=content(source),
    )
    assert state["final_confirmations"]
    assert (
        state["final_confirmation_observations"][0]["context_status"]
        == "changed_or_unavailable"
    )
    assert (
        json.loads((folder / ".veqtor/deal-rounds.json").read_text())["schema_version"]
        == "deal_rounds_store.v9"
    )


def test_recreated_selected_property_is_exposed_for_explicit_resolution(candidate):
    parts = unpack(candidate.payload)
    root = etree.fromstring(parts["docProps/app.xml"])
    etree.SubElement(
        root,
        "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}Company",
    ).text = ""
    parts["docProps/app.xml"] = etree.tostring(root)
    saved = snapshot(pack(parts))
    raw, _ = f.build(
        candidate,
        saved,
        candidate_properties_policy={
            "mode": "remove_selected",
            "fields": [{"part": "app", "name": "Company"}],
        },
    )
    report = json.loads(raw)
    assert report["reappeared_selected_properties"][0]["name"] == "Company"
    assert report["readiness"] == "lawyer_confirmation_required"
    assert saved.payload == pack(parts)


@pytest.mark.parametrize("after_commit", [False, True])
def test_prepare_snapshot_state_failure_and_orphan_recovery(
    saved_case, monkeypatch, after_commit
):
    folder, _, saved, state = saved_case
    real = p._publish

    def fail(*args, **kwargs):
        if after_commit:
            real(*args, **kwargs)
        raise OSError("fictional prepare state failure")

    monkeypatch.setattr(p, "_publish", fail)
    with pytest.raises(p.PositionError, match="storage_failure"):
        prepare_saved(folder, saved, state)
    monkeypatch.setattr(p, "_publish", real)
    observed = server.read_deal_state(str(folder))
    assert len(observed["final_saved_reviews"]) == int(after_commit)
    # Same original request index and exact parameters recover either outcome.
    recovered = prepare_saved(folder, saved, state)
    assert len(recovered["final_saved_reviews"]) == 1
    assert not recovered["final_confirmations"]


def test_unknown_manual_check_does_not_waive_missing_required_check(saved_case):
    folder, _, saved, state = saved_case
    state = prepare_saved(folder, saved, state)
    values = confirmation(state)
    values["manual_checks"]["skip_all_checks"] = "yes"
    del values["manual_checks"]["word_reopened"]
    with pytest.raises(p.PositionError, match="invalid_request"):
        change(folder, state, "confirm_final_saved_document", **values)


def test_historical_confirmation_replay_does_not_approve_changed_bytes(saved_case):
    folder, source, saved, state = saved_case
    state = prepare_saved(folder, saved, state)
    values = confirmation(state)
    key = "b" * 32
    state = change(
        folder, state, "confirm_final_saved_document", request_id=key, **values
    )
    saved.write_bytes(source.read_bytes())
    resumed = change(
        folder, state, "confirm_final_saved_document", request_id=key, **values
    )
    assert resumed["revision"] == state["revision"]
    assert (
        resumed["final_confirmation_observations"][0]["selected_file_status"]
        == "changed"
    )
