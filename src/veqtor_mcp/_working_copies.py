# SPDX-License-Identifier: Apache-2.0
"""Planned initial working copies, with no fabricated edits or manual history."""

import hashlib
import os
from copy import deepcopy

from veqtor_docx import round_snapshots as snapshots
from veqtor_docx._ooxml import MAX_DOCX_INPUT_BYTES, DocxError

from . import _deal_outgoing as outgoing
from . import _deal_workflow as w
from . import positions as p

COLLECTION = "working_copy_plans"
SELECTION_ACTIONS = {"publish_working_copy", "record_working_copy_verification"}


def upgrade(store):
    result = outgoing.upgrade(store)
    result["schema_version"] = store["schema_version"] if store["schema_version"] in {"deal_rounds_store.v6", "deal_rounds_store.v7"} else "deal_rounds_store.v5"
    result.setdefault(COLLECTION, [])
    return result


def _path(value):
    selected = w.relative(value)
    if not selected.lower().endswith(".docx"):
        p._fail("invalid_request")
    return selected


def _plan_hash(row):
    return hashlib.sha256(
        p._json_bytes(
            {
                "schema_version": "working_copy_plan.v1",
                "policy": "create_only_initial_incoming_copy",
                **{
                    k: row[k]
                    for k in ("request_id", "round_id", "source_sha256", "path")
                },
            }
        )
    ).hexdigest()


def _destination(folder, row):
    return os.path.join(os.path.expanduser(folder), _path(row["path"]))


def _require_new_path(folder, row):
    path = _destination(folder, row)
    with snapshots._directory(os.path.dirname(path)) as parent:
        snapshots._parent_controlled(parent.descriptor)
        try:
            os.stat(
                os.path.basename(path), dir_fd=parent.descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            pass
        else:
            p._fail("output_exists")
        parent.check()


def _verify_copy(destination, snapshot):
    with snapshots._directory(os.path.dirname(destination)) as parent:
        snapshots._parent_controlled(parent.descriptor)
        data = snapshots._read_file(
            parent.descriptor,
            os.path.basename(destination),
            limit=MAX_DOCX_INPUT_BYTES,
            private=True,
        )
        parent.check()
        snapshots._parent_controlled(parent.descriptor)
        if data != snapshot.payload:
            p._fail("working_copy_mismatch")


def validate(store):
    if store["schema_version"] not in {"deal_rounds_store.v5", "deal_rounds_store.v6", "deal_rounds_store.v7"}:
        return
    plans = {r["request_id"]: r for r in store[COLLECTION]}
    works = {r["request_id"]: r for r in store["working_documents"]}
    rounds = {r["round_id"]: r for r in store["rounds"]}
    seen, selected_rounds, paths, order = {}, set(), set(), []
    operated_rounds = set()
    try:
        for request in store["requests"]:
            key, rid, action = (
                request["request_id"],
                request["round_id"],
                request["action"],
            )
            if action == "prepare_working_copy":
                row = plans.pop(key)
                _path(row["path"])
                if (
                    rid in selected_rounds | operated_rounds
                    or row["round_id"] != rid
                    or row["source_sha256"] != rounds[rid]["incoming_sha256"]
                    or row["plan_sha256"] != _plan_hash(row)
                    or row["path"] in paths
                ):
                    p._fail("storage_corrupt")
                command = {
                    "action": action,
                    "round_id": rid,
                    "incoming_basis_sha256": row["source_sha256"],
                    "path": row["path"],
                }
                seen[key] = row
                paths.add(row["path"])
                order.append(row)
            elif action in SELECTION_ACTIONS:
                work = works[key]
                row = seen[work["copy_plan_request_id"]]
                expected = (
                    "published"
                    if action == "publish_working_copy"
                    else "observed_match"
                )
                if (
                    rid in selected_rounds | operated_rounds
                    or row["round_id"] != rid
                    or work["path"] != row["path"]
                    or work["file_sha256"] != row["source_sha256"]
                    or work["publication_status"] != expected
                ):
                    p._fail("storage_corrupt")
                command = {
                    "action": action,
                    "round_id": rid,
                    "copy_plan_request_id": row["request_id"],
                    "plan_sha256": row["plan_sha256"],
                }
            else:
                if key in works:
                    selected_rounds.add(rid)
                if action == "link_operation":
                    operated_rounds.add(rid)
                continue
            if (
                request["command_sha256"]
                != hashlib.sha256(p._json_bytes(command)).hexdigest()
            ):
                p._fail("storage_corrupt")
            if key in works:
                selected_rounds.add(rid)
        if plans or order != store[COLLECTION]:
            p._fail("storage_corrupt")
    except (KeyError, ValueError, TypeError, DocxError):
        p._fail("storage_corrupt")


def change(folder, store, request_id, command, snapshot_path):
    """Validate prospective state before returning any document publication."""
    rid, action = command["round_id"], command["action"]
    row = next((r for r in store["rounds"] if r["round_id"] == rid), None)
    if row is None:
        p._fail("round_missing")
    if w.working(store, rid):
        p._fail("working_document_exists")
    if any(op["round_id"] == rid for op in store["operations"]):
        p._fail("working_copy_basis_changed")
    if action == "prepare_working_copy":
        if command["incoming_basis_sha256"] != row["incoming_sha256"]:
            p._fail("working_basis_mismatch")
        plan = {
            "request_id": request_id,
            "round_id": rid,
            "source_sha256": row["incoming_sha256"],
            "path": _path(command["path"]),
        }
        if any(r["path"] == plan["path"] for r in store[COLLECTION]):
            p._fail("working_copy_path_reserved")
        _require_new_path(folder, plan)
        plan["plan_sha256"] = _plan_hash(plan)
        store[COLLECTION].append(plan)
        return None
    plan = next(
        (
            r
            for r in store[COLLECTION]
            if r["request_id"] == command["copy_plan_request_id"]
        ),
        None,
    )
    if (
        plan is None
        or plan["round_id"] != rid
        or plan["plan_sha256"] != command["plan_sha256"]
        or plan["source_sha256"] != row["incoming_sha256"]
    ):
        p._fail("working_copy_plan_mismatch")
    store["working_documents"].append(
        {
            "request_id": request_id,
            "round_id": rid,
            "operation_id": None,
            "path": plan["path"],
            "file_sha256": plan["source_sha256"],
            "previous_request_id": None,
            "origin": "verified_snapshot_copy",
            "statement": None,
            "copy_plan_request_id": plan["request_id"],
            "publication_status": "published"
            if action == "publish_working_copy"
            else "observed_match",
        }
    )

    def effect():
        origin = snapshot_path(folder, plan["source_sha256"])
        snapshot = snapshots.load_snapshot(origin)
        if snapshot.sha256 != plan["source_sha256"]:
            p._fail("snapshot_conflict")
        destination = _destination(folder, plan)
        if action == "publish_working_copy":
            try:
                snapshots.restore_snapshot_copy(
                    origin, destination, expected_sha256=plan["source_sha256"]
                )
            except snapshots.SnapshotError as exc:
                exc.metadata["copy_plan_request_id"] = plan["request_id"]
                raise
        # Matching bytes establish the selected copy, never who created it.
        # Verification does not write the document, repair it or infer a save.
        try:
            _verify_copy(destination, snapshot)
        except (OSError, DocxError) as exc:
            if action == "publish_working_copy":
                raise snapshots.SnapshotError(
                    "working_copy_verification_failed",
                    "copy was published but its current path cannot be verified",
                    publication_completed=True,
                    output_path_verified=False,
                    copy_plan_request_id=plan["request_id"],
                ) from exc
            raise

    return effect


def result(store):
    return {COLLECTION: deepcopy(store.get(COLLECTION, [])) if store else []}
