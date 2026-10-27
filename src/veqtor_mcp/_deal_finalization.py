# SPDX-License-Identifier: Apache-2.0
"""Explicit agreement on a saved basis and checked candidates for Word review.

Agreement is a client assertion. Publication and verification never confirm the
final saved Word bytes, authorize sending, or create a signature-ready status.
"""

from copy import deepcopy
import hashlib
import json
import os

from veqtor_docx import outgoing_package as engine, round_snapshots as snapshots
from veqtor_docx._ooxml import DocxError, load_validated_docx, parse_xml, w as tag

from . import positions as p, _deal_workflow as w, _deal_outgoing as outgoing
from . import _deal_state_contract as c

COLLECTIONS = ("final_basis_agreements", "final_documents", "final_document_events")
_DECLARATIONS = (
    "statement",
    "issue_resolutions",
    "position_resolutions",
    "manual_checks",
)


def _hash(value):
    return hashlib.sha256(p._json_bytes(value)).hexdigest()


def upgrade(store):
    candidate = outgoing.upgrade(store, table=True)
    candidate["schema_version"] = "deal_rounds_store.v9" if store["schema_version"] == "deal_rounds_store.v9" else "deal_rounds_store.v8"
    for name in COLLECTIONS:
        candidate.setdefault(name, [])
    return candidate


def scope(store, rid, positions_revision):
    return _hash(
        {
            "round_id": rid,
            "positions_revision": positions_revision,
            **{
                name: store.get(name, [])
                for name in (
                    "rounds",
                    "operations",
                    "working_documents",
                    "issue_events",
                )
            },
        }
    )


def _review_basis(data):
    # Finalization's own records advance the store revision, but cannot renew
    # agreement on changed negotiation data or live file/journal observations.
    return _hash({k: v for k, v in data.items() if k != "revision"})


def _load_json(raw):
    return json.loads(
        raw,
        object_pairs_hook=p._pairs,
        parse_constant=lambda _: p._fail("storage_corrupt"),
    )


def _review(row):
    from . import deal_state as d, _finalization_review_contract as review_contract

    value = _load_json(row["review_utf8"])
    core = deepcopy(review_contract.RESULT)
    for key in ("producer", "record_id", "record_status"):
        del core["properties"][key]
        core["required"].remove(key)
    d._validate(core, value, "storage_corrupt")
    data = value["data"]
    if (
        value["review_sha256"] != row["review_sha256"]
        or _hash(data) != row["review_sha256"]
        or _review_basis(data) != row["review_basis_sha256"]
        or data["basis"]["file_sha256"] != row["file_sha256"]
        or data["selected_round_id"] != row["round_id"]
        or data["positions_revision"] != row["positions_revision"]
    ):
        p._fail("storage_corrupt")
    return data


def _require_review(data):
    doc = data["document"]
    if (
        set(data["automatic_blockers"]) - {"unresolved_issues"}
        or not doc["inventory_complete"]
        or doc["revision_count"]
        or doc["coverage_blockers"]
        or doc["clean_probe"] != "passed_revision_free"
        or data["basis"]["working_status"] not in {"not_selected", "same_bytes"}
        or any(
            i["position_status"] == "unavailable"
            for i in [*data["issues"], *data["issue_history"]]
        )
        or any(o["outcome"] == "unresolved" for o in data["operations"])
    ):
        p._fail("final_basis_blocked")


def _resolutions(data, declaration):
    for key, id_key, expected in (
        ("issue_resolutions", "issue_id", [i["issue"] for i in data["issues"]]),
        (
            "position_resolutions",
            "position_id",
            [x for x in data["positions"] if x["lifecycle"] == "active"],
        ),
    ):
        rows = declaration[key]
        if (
            len(rows) != len(expected)
            or len({r[id_key] for r in rows}) != len(rows)
            or {r[id_key]: r["version"] for r in rows}
            != {r[id_key]: r["version"] for r in expected}
        ):
            p._fail("final_resolution_mismatch")


def _agreement_command(row):
    return {
        "action": "record_final_basis_agreement",
        "round_id": row["round_id"],
        "file_sha256": row["file_sha256"],
        "expected_positions_revision": row["positions_revision"],
        "expected_review_sha256": row["review_sha256"],
        "user_confirmed": True,
        **{k: deepcopy(row[k]) for k in _DECLARATIONS},
    }


def _document_command(row):
    return {
        "action": "prepare_final_document",
        **{
            k: deepcopy(row[k])
            for k in (
                "round_id",
                "agreement_request_id",
                "path",
                "name",
                "comments_policy",
                "properties_policy",
            )
        },
    }


def _report(row):
    raw = row["report_utf8"].encode("utf-8")
    if (
        len(raw) > engine.MAX_REPORT_BYTES
        or hashlib.sha256(raw).hexdigest() != row["plan_sha256"]
    ):
        p._fail("storage_corrupt")
    report = _load_json(raw)
    if (
        report["schema_version"] != "outgoing_preparation.v2"
        or report["source_sha256"] != row["source_sha256"]
        or report["readiness"] != "prepared_unreviewed"
        or report["comments_policy"] != row["comments_policy"]
        or report["properties_policy"] != row["properties_policy"]
        or report["inventory_before"]["revisions"]
        or report["clean_draft"]["accepted_revision_count"] != 0
        or len(report["files"]) != 1
        or {k: report["files"][0][k] for k in ("kind", "name", "sha256")}
        != {"kind": "clean_draft", "name": row["name"], "sha256": row["file_sha256"]}
    ):
        p._fail("storage_corrupt")
    engine.policies(row["comments_policy"], row["properties_policy"])
    engine._name(row["name"])
    return report


def validate(store):
    if store["schema_version"] not in {"deal_rounds_store.v8", "deal_rounds_store.v9"}:
        return
    groups = {name: {r["request_id"]: r for r in store[name]} for name in COLLECTIONS}
    order = {name: [] for name in COLLECTIONS}
    prefix = {
        "rounds": [],
        "operations": [],
        "working_documents": [],
        "issue_events": [],
    }
    by_round = {r["round_id"]: r for r in store["rounds"]}
    current_rounds, agreements, documents = {}, {}, {}
    links = {
        name: {r["request_id"]: r for r in store[name]}
        for name in ("operations", "working_documents", "issue_events")
    }
    try:
        for request in store["requests"]:
            key, rid, action = (
                request["request_id"],
                request["round_id"],
                request["action"],
            )
            if action == "start_round":
                row = {**deepcopy(by_round[rid]), "sent_documents": []}
                prefix["rounds"].append(row)
                current_rounds[rid] = row
            elif action == "record_sent":
                current_rounds[rid]["sent_documents"].append(
                    next(
                        r
                        for r in by_round[rid]["sent_documents"]
                        if r["request_id"] == key
                    )
                )
            for name in links:
                if key in links[name]:
                    prefix[name].append(links[name][key])
            if action not in c.FINALIZATION_ACTIONS:
                continue
            collection = (
                COLLECTIONS[0]
                if action == "record_final_basis_agreement"
                else COLLECTIONS[1]
                if action == "prepare_final_document"
                else COLLECTIONS[2]
            )
            row = groups[collection].pop(key)
            if row["round_id"] != rid:
                p._fail("storage_corrupt")
            if action == "record_final_basis_agreement":
                data = _review(row)
                _require_review(data)
                _resolutions(data, row)
                if (
                    row["scope_sha256"] != scope(prefix, rid, row["positions_revision"])
                    or row["file_sha256"] != w.basis(prefix, current_rounds[rid])
                    or [i["issue"] for i in data["issues"]] != w.issues(prefix)
                    or [i["event"] for i in data["issue_history"]]
                    != prefix["issue_events"]
                    or [o["link"] for o in data["operations"]] != prefix["operations"]
                    or data["included_round_ids"] != list(current_rounds)
                    or data["basis"]["working_document"]
                    != next(iter(w.working(prefix, rid)[-1:]), None)
                ):
                    p._fail("storage_corrupt")
                command = _agreement_command(row)
                agreements[key] = row
            elif action == "prepare_final_document":
                agreement = agreements[row["agreement_request_id"]]
                w.relative(row["path"])
                _report(row)
                if (
                    agreement["round_id"] != rid
                    or row["source_sha256"] != agreement["file_sha256"]
                    or agreement["scope_sha256"]
                    != scope(prefix, rid, agreement["positions_revision"])
                ):
                    p._fail("storage_corrupt")
                command = _document_command(row)
                documents[key] = row
            else:
                document = documents[row["document_request_id"]]
                agreement = agreements[document["agreement_request_id"]]
                publish = action == "publish_final_document"
                if (
                    document["round_id"] != rid
                    or document["plan_sha256"] != row["plan_sha256"]
                    or row["status"] != ("published" if publish else "observed_match")
                    or (row["statement"] is not None) != publish
                ):
                    p._fail("storage_corrupt")
                if publish and (
                    agreement["scope_sha256"]
                    != scope(prefix, rid, agreement["positions_revision"])
                    or any(
                        e["document_request_id"] == document["request_id"]
                        for e in order[COLLECTIONS[2]]
                    )
                ):
                    p._fail("storage_corrupt")
                command = {
                    "action": action,
                    "round_id": rid,
                    "document_request_id": document["request_id"],
                    "plan_sha256": document["plan_sha256"],
                }
                if publish:
                    command.update(user_confirmed=True, statement=row["statement"])
            if _hash(command) != request["command_sha256"]:
                p._fail("storage_corrupt")
            order[collection].append(row)
        if any(groups[name] or order[name] != store[name] for name in COLLECTIONS):
            p._fail("storage_corrupt")
    except (KeyError, IndexError, StopIteration, ValueError, TypeError, DocxError):
        p._fail("storage_corrupt")


def _current(folder, store, positions, agreement):
    from ._finalization_review import read_locked

    revision = positions["revision"] if positions else None
    if agreement["scope_sha256"] != scope(store, agreement["round_id"], revision):
        p._fail("final_basis_changed")
    checked = read_locked(
        folder, store, positions, agreement["round_id"], agreement["file_sha256"]
    )
    _require_review(checked["data"])
    if _review_basis(checked["data"]) != agreement["review_basis_sha256"]:
        p._fail("final_basis_changed")


def _destination(folder, row):
    return os.path.join(os.path.expanduser(folder), w.relative(row["path"]))


def _new_destination(folder, row):
    destination = _destination(folder, row)
    with snapshots._directory(os.path.dirname(destination)) as parent:
        snapshots._parent_controlled(parent.descriptor)
        try:
            os.stat(
                os.path.basename(destination),
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            p._fail("output_exists")
        parent.check()


def _build(folder, row, snapshot_path):
    snap = snapshots.load_snapshot(snapshot_path(folder, row["source_sha256"]))
    if snap.sha256 != row["source_sha256"]:
        p._fail("snapshot_conflict")
    plan = engine.prepare_outgoing_package(
        snap,
        _destination(folder, row),
        clean_draft_name=row["name"],
        comments_policy=row["comments_policy"],
        properties_policy=row["properties_policy"],
    )
    report = _load_json(plan.report)
    if (
        report["inventory_before"]["revisions"]
        or report["clean_draft"]["accepted_revision_count"]
    ):
        p._fail("final_basis_blocked")
    # Cleanup owns the dependency proof. Independently require no surviving
    # ordinary comment messages or anchors anywhere in the inspected package.
    package = load_validated_docx(plan.files[0].payload, capture=None)
    forbidden = {
        tag(x)
        for x in ("comment", "commentRangeStart", "commentRangeEnd", "commentReference")
    }
    if any(
        n.tag in forbidden
        for name, raw in package.parts.items()
        if name.endswith(".xml")
        for n in parse_xml(raw).iter()
    ):
        p._fail("final_comments_remaining")
    return plan


def _plan(folder, row, snapshot_path):
    plan = _build(folder, row, snapshot_path)
    if (
        plan.report.decode("utf-8") != row["report_utf8"]
        or plan.plan_sha256 != row["plan_sha256"]
    ):
        p._fail("final_plan_mismatch")
    return plan


def change(folder, store, positions, request_id, command, snapshot_path):
    from ._finalization_review import read_locked

    action, rid = command["action"], command["round_id"]
    if not any(r["round_id"] == rid for r in store["rounds"]):
        p._fail("round_missing")
    if action == "record_final_basis_agreement":
        revision = positions["revision"] if positions else None
        if command["expected_positions_revision"] != revision:
            p._fail("positions_revision_conflict")
        checked = read_locked(folder, store, positions, rid, command["file_sha256"])
        if checked["review_sha256"] != command["expected_review_sha256"]:
            p._fail("final_review_changed")
        _require_review(checked["data"])
        _resolutions(checked["data"], command)
        store[COLLECTIONS[0]].append(
            {
                "request_id": request_id,
                "round_id": rid,
                "file_sha256": command["file_sha256"],
                "positions_revision": revision,
                "scope_sha256": scope(store, rid, revision),
                "review_sha256": checked["review_sha256"],
                "review_basis_sha256": _review_basis(checked["data"]),
                "review_utf8": p._json_bytes(checked).decode("utf-8"),
                "assurance": "client_asserted_agreement_on_exact_basis",
                **{k: deepcopy(command[k]) for k in _DECLARATIONS},
            }
        )
        return None
    if action == "prepare_final_document":
        agreement = next(
            (
                r
                for r in store[COLLECTIONS[0]]
                if r["request_id"] == command["agreement_request_id"]
            ),
            None,
        )
        if agreement is None or agreement["round_id"] != rid:
            p._fail("final_agreement_missing")
        _current(folder, store, positions, agreement)
        row = {
            "request_id": request_id,
            "source_sha256": agreement["file_sha256"],
            **{
                k: deepcopy(command[k])
                for k in (
                    "round_id",
                    "agreement_request_id",
                    "path",
                    "name",
                    "comments_policy",
                    "properties_policy",
                )
            },
            "stage": "candidate_for_word_review",
        }
        _new_destination(folder, row)
        plan = _build(folder, row, snapshot_path)
        row.update(
            plan_sha256=plan.plan_sha256,
            report_utf8=plan.report.decode("utf-8"),
            file_sha256=hashlib.sha256(plan.files[0].payload).hexdigest(),
        )
        store[COLLECTIONS[1]].append(row)
        return None
    row = next(
        (
            r
            for r in store[COLLECTIONS[1]]
            if r["request_id"] == command["document_request_id"]
        ),
        None,
    )
    if (
        row is None
        or row["round_id"] != rid
        or row["plan_sha256"] != command["plan_sha256"]
    ):
        p._fail("final_plan_mismatch")
    agreement = next(
        r
        for r in store[COLLECTIONS[0]]
        if r["request_id"] == row["agreement_request_id"]
    )
    plan = _plan(folder, row, snapshot_path)
    publish = action == "publish_final_document"
    if publish:
        _current(folder, store, positions, agreement)
        if any(
            e["document_request_id"] == row["request_id"] for e in store[COLLECTIONS[2]]
        ):
            p._fail("final_document_already_recorded")
    store[COLLECTIONS[2]].append(
        {
            "request_id": request_id,
            "round_id": rid,
            "document_request_id": row["request_id"],
            "plan_sha256": row["plan_sha256"],
            "status": "published" if publish else "observed_match",
            "statement": command.get("statement"),
        }
    )

    def effect():
        if publish:
            _current(folder, store, positions, agreement)
            engine.publish_outgoing_package(
                plan, _destination(folder, row), expected_plan_sha256=row["plan_sha256"]
            )
        else:
            engine.verify_outgoing_package(
                plan, _destination(folder, row), expected_plan_sha256=row["plan_sha256"]
            )

    return effect


def result(store, positions):
    revision = positions["revision"] if positions else None
    result = {
        name: deepcopy(store.get(name, [])) if store else [] for name in COLLECTIONS
    }
    result["final_basis_observations"] = [
        {
            "agreement_request_id": row["request_id"],
            "status": "saved_context_matches_live_checks_required"
            if row["scope_sha256"] == scope(store, row["round_id"], revision)
            else "saved_context_changed",
        }
        for row in result[COLLECTIONS[0]]
    ]
    return result
