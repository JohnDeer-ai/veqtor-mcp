# SPDX-License-Identifier: Apache-2.0
"""Frozen outgoing previews, durable internal reports and explicit publication.

Receipts describe observations at the operation, not current filesystem state,
confidentiality clearance, legal agreement, or transmission to another party.
"""
from copy import deepcopy
import hashlib
import json
import os

from veqtor_docx import operation_journal as journal, outgoing_package as engine
from veqtor_docx import round_snapshots as snapshots
from veqtor_docx._ooxml import DocxError

from . import positions as p, _deal_workflow as w

COLLECTIONS = ("outgoing_packages", "outgoing_events")


def upgrade(store):
    result = w.upgrade(store)
    result["schema_version"] = ("deal_rounds_store.v5" if store["schema_version"] == "deal_rounds_store.v5"
                                else "deal_rounds_store.v4")
    for name in COLLECTIONS:
        result.setdefault(name, [])
    return result


def basis(store, rid, positions_revision):
    row = next(r for r in store["rounds"] if r["round_id"] == rid)
    works = w.working(store, rid)
    if not works:
        p._fail("working_basis_mismatch")
    value = dict(round_id=rid, incoming_sha256=row["incoming_sha256"], sent_basis=row["sent_basis"],
                 working=works[-1], positions_revision=positions_revision,
                 issues=[i for i in w.issues(store) if i["round_id"] == rid])
    return hashlib.sha256(p._json_bytes(value)).hexdigest()


def _destination(folder, row):
    return os.path.join(os.path.expanduser(folder), w.relative(row["path"]))


def _report(row):
    raw = row["report_utf8"].encode("utf-8")
    if len(raw) > engine.MAX_REPORT_BYTES or hashlib.sha256(raw).hexdigest() != row["plan_sha256"]:
        p._fail("storage_corrupt")
    report = json.loads(raw, object_pairs_hook=p._pairs,
                        parse_constant=lambda _: p._fail("storage_corrupt"))
    if (report["schema_version"] != "outgoing_preparation.v1"
            or report["source_sha256"] != row["source_sha256"]
            or [{k: f[k] for k in ("kind", "name", "sha256")} for f in report["files"]] != row["files"]
            or any(report[key] != row[key] for key in ("readiness", "comments_policy", "properties_policy"))):
        p._fail("storage_corrupt")
    selected = [(kind, row[key]) for kind, key in
                [("cumulative_redline", "redline_name"), ("clean_draft", "clean_draft_name")]
                if row[key] is not None]
    if not selected or [(f["kind"], f["name"]) for f in row["files"]] != selected:
        p._fail("storage_corrupt")
    for _, name in selected:
        engine._name(name)
    return report


def validate(store):
    if store["schema_version"] not in {"deal_rounds_store.v4", "deal_rounds_store.v5"}:
        return
    packages = {r["request_id"]: r for r in store["outgoing_packages"]}
    events = {r["request_id"]: r for r in store["outgoing_events"]}
    seen, package_order, event_order = {}, [], []
    prefix = {**store, "working_documents": [], "issue_events": []}
    works = {r["request_id"]: r for r in store["working_documents"]}
    issues = {r["request_id"]: r for r in store["issue_events"]}
    try:
        for request in store["requests"]:
            key, rid, action = request["request_id"], request["round_id"], request["action"]
            if key in works:
                prefix["working_documents"].append(works[key])
            if key in issues:
                prefix["issue_events"].append(issues[key])
            if action == "prepare_outgoing_package":
                row = packages.pop(key)
                work = w.working(prefix, rid)[-1]
                w.relative(row["path"])
                _report(row)
                if (row["round_id"] != rid or row["working_request_id"] != work["request_id"]
                        or row["source_sha256"] != work["file_sha256"]
                        or row["basis_sha256"] != basis(prefix, rid, row["positions_revision"])):
                    p._fail("storage_corrupt")
                seen[key] = row
                package_order.append(row)
            elif action in {"publish_outgoing_package", "record_outgoing_verification"}:
                row = events.pop(key)
                package = seen[row["package_request_id"]]
                expected = "published" if action == "publish_outgoing_package" else "observed_match"
                if (row["round_id"] != rid or package["round_id"] != rid
                        or row["plan_sha256"] != package["plan_sha256"] or row["status"] != expected
                        or (row["statement"] is None) != (action == "record_outgoing_verification")):
                    p._fail("storage_corrupt")
                if action == "publish_outgoing_package" and package["basis_sha256"] != basis(
                        prefix, rid, package["positions_revision"]):
                    p._fail("storage_corrupt")
                if action == "publish_outgoing_package" and any(
                        e["package_request_id"] == package["request_id"] for e in event_order):
                    p._fail("storage_corrupt")
                event_order.append(row)
        if packages or events or package_order != store["outgoing_packages"] or event_order != store["outgoing_events"]:
            p._fail("storage_corrupt")
    except (KeyError, IndexError, StopIteration, ValueError, TypeError, DocxError):
        p._fail("storage_corrupt")


def _plan(folder, row, snapshot_path):
    snap = snapshots.load_snapshot(snapshot_path(folder, row["source_sha256"]))
    plan = engine.prepare_outgoing_package(snap, _destination(folder, row),
        redline_name=row["redline_name"], clean_draft_name=row["clean_draft_name"])
    if plan.report.decode("utf-8") != row["report_utf8"] or plan.plan_sha256 != row["plan_sha256"]:
        p._fail("outgoing_plan_mismatch")
    return plan


def _current(folder, store, positions_store, row):
    revision = positions_store["revision"] if positions_store else None
    if basis(store, row["round_id"], revision) != row["basis_sha256"]:
        p._fail("outgoing_basis_changed")
    work = w.working(store, row["round_id"])[-1]
    if journal.observed_hash(os.path.join(os.path.expanduser(folder), work["path"])) != row["source_sha256"]:
        p._fail("working_basis_mismatch")


def change(folder, store, positions_store, request_id, command, snapshot_path):
    """Build a prospective state and deferred effect; no output writes here."""
    rid, action = command["round_id"], command["action"]
    if not any(r["round_id"] == rid for r in store["rounds"]):
        p._fail("round_missing")
    if action == "prepare_outgoing_package":
        works = w.working(store, rid)
        if (not works or works[-1]["request_id"] != command["expected_working_request_id"]
                or works[-1]["file_sha256"] != command["file_sha256"]):
            p._fail("working_basis_mismatch")
        row = dict(request_id=request_id, round_id=rid, working_request_id=works[-1]["request_id"],
            source_sha256=command["file_sha256"], path=w.relative(command["path"]),
            positions_revision=positions_store["revision"] if positions_store else None,
            redline_name=command["redline_name"], clean_draft_name=command["clean_draft_name"],
            readiness="prepared_unreviewed", comments_policy="preserve_all", properties_policy="preserve_all")
        row["basis_sha256"] = basis(store, rid, row["positions_revision"])
        _current(folder, store, positions_store, row)
        snap = snapshots.load_snapshot(snapshot_path(folder, row["source_sha256"]))
        plan = engine.prepare_outgoing_package(snap, _destination(folder, row),
            redline_name=row["redline_name"], clean_draft_name=row["clean_draft_name"])
        report = json.loads(plan.report)
        row.update(plan_sha256=plan.plan_sha256, report_utf8=plan.report.decode("utf-8"),
                   files=[{k: f[k] for k in ("kind", "name", "sha256")} for f in report["files"]])
        store["outgoing_packages"].append(row)
        return None
    row = next((r for r in store["outgoing_packages"] if r["request_id"] == command["package_request_id"]), None)
    if row is None or row["round_id"] != rid or row["plan_sha256"] != command["plan_sha256"]:
        p._fail("outgoing_plan_mismatch")
    plan = _plan(folder, row, snapshot_path)
    if action == "publish_outgoing_package":
        _current(folder, store, positions_store, row)
        if any(e["package_request_id"] == row["request_id"] for e in store["outgoing_events"]):
            p._fail("outgoing_already_recorded")
    store["outgoing_events"].append(dict(request_id=request_id, round_id=rid,
        package_request_id=row["request_id"], plan_sha256=row["plan_sha256"],
        status="published" if action == "publish_outgoing_package" else "observed_match",
        statement=command.get("statement")))

    def effect():
        # A prepare request was already durably committed before this call.
        # A failed state commit never authorizes publishing another folder.
        if action == "publish_outgoing_package":
            _current(folder, store, positions_store, row)
            engine.publish_outgoing_package(plan, _destination(folder, row), expected_plan_sha256=row["plan_sha256"])
        else:
            engine.verify_outgoing_package(plan, _destination(folder, row), expected_plan_sha256=row["plan_sha256"])
    return effect


def result(store):
    return {name: deepcopy(store.get(name, [])) if store else [] for name in COLLECTIONS}
