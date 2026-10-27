# SPDX-License-Identifier: Apache-2.0
"""Exact document roles, working-copy links and explicitly asserted legal decisions."""
from copy import deepcopy
import hashlib
import json
import os

import jsonschema

from veqtor_docx import round_snapshots as snapshots
from . import positions as p
from . import _deal_workflow as w
from . import _deal_state_contract as c
from . import _deal_outgoing as outgoing
from . import _working_copies as copies
from . import _deal_finalization as finalization

STORE_NAME = "deal-rounds.json"
DOCUMENTS = "round-documents"
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
LIMITATIONS = [
    "roles_and_actual_send_are_client_assertions_not_transmission_evidence",
    "final_saved_word_bytes_confirmation_remains_unfinished",
    "journal_and_working_file_observations_are_current_not_legal_authorization",
    "positions_remain_in_the_original_store_and_history",
    "snapshot_publication_and_state_commit_are_separate_recoverable_steps",
    "no_legal_agreement_or_finalization_inferred",
    "manual_working_import_is_a_client_assertion_not_a_reconstructed_edit_history",
    "outgoing_receipts_are_historical_observations_not_current_file_checks",
    "outgoing_publication_and_state_commit_are_separate_recoverable_steps",
]


def _validate(schema, value, code="invalid_request"):
    p._json_bytes(value)
    if not jsonschema.Draft202012Validator(schema).is_valid(value):
        p._fail(code)


def _load(side):
    try:
        fd = os.open(STORE_NAME, p._FILE_FLAGS, dir_fd=side)
    except FileNotFoundError:
        return None, None
    try:
        raw, stamp = p._read_file(fd, p.MAX_BYTES, private=True)
        p._named_same(side, STORE_NAME, fd)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw, object_pairs_hook=p._pairs,
                           parse_constant=lambda _: p._fail("storage_corrupt"))
    except (ValueError, UnicodeError, RecursionError):
        p._fail("storage_corrupt")
    _validate(c.STORE, value, "storage_corrupt")
    if value["revision"] != p._revision(value):
        p._fail("storage_corrupt")
    rounds = {}
    sent = {}
    for row in value["rounds"]:
        rid = row["round_id"]
        if rid in rounds:
            p._fail("storage_corrupt")
        previous = row["previous_round_id"]
        basis = row["sent_basis"]
        if previous is None:
            if basis is not None:
                p._fail("storage_corrupt")
        elif (previous not in rounds or basis is None or basis["round_id"] != previous
              or sent.get(basis["request_id"]) != (previous, basis["file_sha256"])):
            p._fail("storage_corrupt")
        rounds[rid] = row
        for entry in row["sent_documents"]:
            if entry["request_id"] in sent:
                p._fail("storage_corrupt")
            sent[entry["request_id"]] = (rid, entry["file_sha256"])
    requests = {}
    starts = set()
    send_history = {}
    start_order = []
    for request in value["requests"]:
        key = request["request_id"]
        rid = request["round_id"]
        if key in requests or rid not in rounds:
            p._fail("storage_corrupt")
        if request["action"] == "start_round":
            if rid in starts:
                p._fail("storage_corrupt")
            row = rounds[rid]
            previous = row["previous_round_id"]
            if previous is not None:
                basis = row["sent_basis"]
                if previous not in starts or send_history.get(previous) != basis["request_id"]:
                    p._fail("storage_corrupt")
            starts.add(rid)
            start_order.append(rid)
        elif rid not in starts:
            p._fail("storage_corrupt")
        elif request["action"] == "record_sent" and (key not in sent or sent[key][0] != rid):
            p._fail("storage_corrupt")
        elif request["action"] == "record_sent":
            send_history[rid] = key
        requests[key] = request
    if starts != set(rounds) or set(sent) != {r["request_id"] for r in value["requests"]
                                          if r["action"] == "record_sent"}:
        p._fail("storage_corrupt")
    if start_order != list(rounds):
        p._fail("storage_corrupt")
    for rid, row in rounds.items():
        expected = [r["request_id"] for r in value["requests"]
                    if r["round_id"] == rid and r["action"] == "record_sent"]
        if expected != [entry["request_id"] for entry in row["sent_documents"]]:
            p._fail("storage_corrupt")
    w.validate(value)
    outgoing.validate(value)
    copies.validate(value)
    finalization.validate(value)
    return value, stamp


def _snapshot_path(folder, digest):
    return os.path.join(os.path.expanduser(folder), ".veqtor", DOCUMENTS, digest)


def _store_snapshot(folder, side, snapshot, recheck):
    try:
        os.mkdir(DOCUMENTS, 0o700, dir_fd=side)
        os.fsync(side)
    except FileExistsError:
        pass
    fd = os.open(DOCUMENTS, p._DIR_FLAGS, dir_fd=side)
    try:
        snapshots._private(fd, directory=True)
        snapshots._supported_filesystem(fd, side)
        recheck()
        p._named_same(side, DOCUMENTS, fd)
        destination = _snapshot_path(folder, snapshot.sha256)
        try:
            os.stat(snapshot.sha256, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            snapshots.persist_snapshot(snapshot, destination)
        else:
            if snapshots.load_snapshot(destination) != snapshot:
                p._fail("snapshot_conflict")
        recheck()
        p._named_same(side, DOCUMENTS, fd)
    finally:
        os.close(fd)


def _hashes(store):
    return ({digest for row in store["rounds"] for digest in
             [row["incoming_sha256"], *[entry["file_sha256"] for entry in row["sent_documents"]]]}
            | {r["file_sha256"] for r in store.get("working_documents", [])})


def _positions_observation(basis, current):
    if basis is None:
        return "not_recorded"
    if current is None or basis["matter_id"] != current["matter_id"]:
        return "unavailable"
    count = basis["history_sequence"]
    if count > len(current["history"]):
        return "unavailable"
    history = current["history"][:count]
    rows = {entry["position"]["position_id"]: entry["position"] for entry in history}
    positions = [rows[key] for key in sorted(rows)]
    versions = [{"position_id": row["position_id"], "version": row["version"]} for row in positions]
    historical = {**current, "positions": positions, "history": history}
    if p._revision(historical) != basis["revision"] or versions != basis["versions"]:
        return "unavailable"
    return "current" if current["revision"] == basis["revision"] else "historical_available"


def _result(folder, store, positions_store, request_id=None, *, pending_snapshot=None, observation_offset=0):
    documents = []
    total = 0
    for digest in sorted(_hashes(store)) if store else []:
        destination = _snapshot_path(folder, digest)
        snapshot = (pending_snapshot if pending_snapshot is not None and pending_snapshot.sha256 == digest
                    else snapshots.load_snapshot(destination))
        if snapshot.sha256 != digest:
            p._fail("snapshot_conflict")
        total += snapshot.size_bytes
        if total > MAX_DOCUMENT_BYTES:
            p._fail("resource_limit_exceeded")
        documents.append({"file_sha256": digest, "path": os.path.join(destination, "source.docx")})
    return {"schema_version": "deal_state_result.v8", "status": "ok",
            "revision": store["revision"] if store else None,
            "rounds": deepcopy(store["rounds"]) if store else [],
            "requests": deepcopy(store["requests"]) if store else [],
            "documents": documents, "applied_request_id": request_id,
            "positions_current_revision": positions_store["revision"] if positions_store else None,
            "positions_basis_observations": [{"round_id": row["round_id"],
                "status": _positions_observation(row["positions_basis"], positions_store)}
                for row in store["rounds"]] if store else [],
            **w.observations(folder, store, positions_store, observation_offset),
            **outgoing.result(store),
            **copies.result(store),
            **finalization.result(store, positions_store),
            "limitations": list(LIMITATIONS)}


def read_deal_state(folder, observation_offset=0):
    if type(observation_offset) is not int or not 0 <= observation_offset <= 100:
        p._fail("invalid_request")
    with p._storage(folder, write=False, load_store=_load) as storage:
        if storage is None:
            return _result(folder, None, None, observation_offset=observation_offset)
        _, side, recheck = storage
        store, _ = _load(side)
        positions_store, _ = p._load(side)
        result = _result(folder, store, positions_store, observation_offset=observation_offset)
        recheck()
        return result


def mutate_deal_state(folder, expected_revision, request_id, command, *, validate_result=None):
    _validate(c.nullable(c.SHA), expected_revision)
    _validate(c.UUID, request_id)
    _validate(c.COMMAND, command)
    # Original incoming/sent paths stay out of the store. Workflow source paths
    # are explicit durable journal locators, distinct from document-role evidence.
    command_sha = hashlib.sha256(p._json_bytes(command)).hexdigest()
    with p._storage(folder, write=True, load_store=_load) as storage:
        _, side, recheck = storage
        store, stamp = _load(side)
        positions_store, _ = p._load(side)
        for old in store["requests"] if store else []:
            if old["request_id"] == request_id:
                if old["command_sha256"] != command_sha:
                    p._fail("request_id_conflict")
                result = _result(folder, store, positions_store, request_id)
                recheck()
                return validate_result(result) if validate_result else result
        if expected_revision != (store["revision"] if store else None):
            p._fail("revision_conflict")
        candidate = deepcopy(store) if store else {
            "schema_version": "deal_rounds_store.v1", "revision": "0" * 64,
            "rounds": [], "requests": []}
        if command["action"] in c.WORKFLOW_ACTIONS:
            candidate = w.upgrade(candidate, imported=command["action"] == "import_working_document")
        if command["action"] in c.OUTGOING_ACTIONS:
            candidate = outgoing.upgrade(candidate, table=command["action"] == "prepare_outgoing_package_with_table",
                cleanup=command["action"] in {"prepare_outgoing_package", "prepare_outgoing_package_with_table"}
                and (command["comments_policy"] != "preserve_all" or command["properties_policy"] != "preserve_all"))
        if command["action"] in c.WORKING_COPY_ACTIONS:
            candidate = copies.upgrade(candidate)
        if command["action"] in c.FINALIZATION_ACTIONS:
            candidate = finalization.upgrade(candidate)
        snapshot = None
        effect = None
        rid = command["round_id"]
        by_id = {row["round_id"]: row for row in candidate["rounds"]}
        if command["action"] == "start_round":
            if rid in by_id:
                p._fail("round_exists")
            previous = command["previous_round_id"]
            basis = None
            if previous is not None:
                if previous not in by_id or not by_id[previous]["sent_documents"]:
                    p._fail("sent_basis_missing")
                sent = by_id[previous]["sent_documents"][-1]
                basis = {"round_id": previous, "request_id": sent["request_id"],
                         "file_sha256": sent["file_sha256"]}
            positions_basis = None if positions_store is None else {
                "matter_id": positions_store["matter_id"], "revision": positions_store["revision"],
                "history_sequence": len(positions_store["history"]),
                "versions": [{"position_id": item["position_id"], "version": item["version"]}
                             for item in positions_store["positions"]]}
            candidate["rounds"].append({"round_id": rid,
                "incoming_sha256": command["file_sha256"], "previous_round_id": previous,
                "sent_basis": basis, "positions_basis": positions_basis, "sent_documents": []})
        elif command["action"] == "record_sent":
            if rid not in by_id:
                p._fail("round_missing")
            by_id[rid]["sent_documents"].append({"request_id": request_id,
                "file_sha256": command["file_sha256"], "statement": command["statement"],
                "basis": "client_asserted_actual_sent_file"})
        elif command["action"] in c.OUTGOING_ACTIONS:
            effect = outgoing.change(folder, candidate, positions_store, request_id, command, _snapshot_path)
        elif command["action"] in c.WORKING_COPY_ACTIONS:
            effect = copies.change(folder, candidate, request_id, command, _snapshot_path)
        elif command["action"] in c.FINALIZATION_ACTIONS:
            effect = finalization.change(folder, candidate, positions_store, request_id, command, _snapshot_path)
        else:
            snapshot = w.change(folder, candidate, positions_store, request_id, command, _snapshot_path)
        candidate["requests"].append({"request_id": request_id, "command_sha256": command_sha,
                                      "round_id": rid, "action": command["action"]})
        candidate["revision"] = p._revision(candidate)
        _validate(c.STORE, candidate)
        w.validate(candidate)
        outgoing.validate(candidate)
        copies.validate(candidate)
        finalization.validate(candidate)
        if command["action"] in {"start_round", "record_sent"}:
            snapshot = snapshots.capture_document_snapshot(command["path"], command["file_sha256"])
        # Validate the aggregate budget and transport result with captured bytes
        # before publishing another snapshot. The publisher still verifies an
        # existing same-hash snapshot, including orphan reuse, before role commit.
        result = _result(folder, candidate, positions_store, request_id, pending_snapshot=snapshot)
        if validate_result:
            result = validate_result(result)
        if snapshot is not None:
            _store_snapshot(folder, side, snapshot, recheck)
        if effect is not None:
            recheck()
            _, observed_stamp = _load(side)
            if observed_stamp != stamp:
                p._fail("workspace_changed")
            current_positions, _ = p._load(side)
            if (current_positions["revision"] if current_positions else None) != (
                    positions_store["revision"] if positions_store else None):
                p._fail("outgoing_basis_changed")
            effect()
        try:
            if effect is not None and command["action"] in copies.SELECTION_ACTIONS:
                result = _result(folder, candidate, positions_store, request_id)
                if validate_result:
                    result = validate_result(result)
            p._publish(side, candidate, stamp, recheck, store_name=STORE_NAME, load_store=_load)
        except Exception as exc:
            if effect is not None and command["action"] == "publish_working_copy":
                raise snapshots.SnapshotError("working_copy_state_commit_uncertain",
                    "copy published; reread state and verify this saved copy plan before retrying",
                    publication_completed=True, output_path_verified=False,
                    copy_plan_request_id=command["copy_plan_request_id"]) from exc
            if effect is not None and command["action"] == "publish_outgoing_package":
                raise snapshots.SnapshotError("outgoing_state_commit_uncertain",
                    "kit published; reread state and verify the frozen plan before any retry",
                    publication_completed=True, output_path_verified=False,
                    package_request_id=command["package_request_id"]) from exc
            if effect is not None and command["action"] == "publish_final_document":
                raise snapshots.SnapshotError("final_document_state_commit_uncertain",
                    "candidate published; reread state and verify this frozen plan before retrying",
                    publication_completed=True, output_path_verified=False,
                    document_request_id=command["document_request_id"]) from exc
            raise
        return result
