# SPDX-License-Identifier: Apache-2.0
"""Bounded links to original journals, separate work copies and legal issues."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import os

from veqtor_docx import operation_journal as journal, round_snapshots as snapshots
from veqtor_docx._ooxml import DocxError
from veqtor_docx.inspect import _load_snapshot_from_payload, _resolve_paragraph

from . import positions as p

COLLECTIONS = ("operations", "working_documents", "issue_events")
MAX_OPERATION_OBSERVATIONS = 20
TABLE = {"link_operation": "operations", "link_working_document": "working_documents",
         "record_working_version": "working_documents", "save_issue": "issue_events", "decide_issue": "issue_events", "link_issue_operation": "issue_events"}


@contextmanager
def _sources(folder):
    path, root = p._open_root(folder)
    try:
        yield p._Sources(root)
        p._recheck_root(path, root)
    finally:
        os.close(root)


def upgrade(store):
    candidate = deepcopy(store)
    candidate["schema_version"] = "deal_rounds_store.v2"
    for name in COLLECTIONS:
        candidate.setdefault(name, [])
    return candidate


def relative(path):
    parts = p._source_path(path)
    if any(part.casefold() in {".veqtor", ".veqtor-backups"} for part in parts):
        p._fail("invalid_request")
    return path


def absolute(path):
    if (not os.path.isabs(path) or os.path.normpath(path) != path
            or any(part.casefold() in {".veqtor", ".veqtor-backups"} for part in path.split("/"))):
        p._fail("invalid_request")
    return path


def working(store, rid):
    return [r for r in store.get("working_documents", []) if r["round_id"] == rid]


def basis(store, row):
    works = working(store, row["round_id"])
    return works[-1]["file_sha256"] if works else row["incoming_sha256"]


def issues(store):
    latest = {}
    for row in store.get("issue_events", []):
        latest[row["issue_id"]] = row
    return list(latest.values())


def _roles(row):
    return {row["incoming_sha256"]} | ({row["sent_basis"]["file_sha256"]} if row["sent_basis"] else set())


def _content(content, row, works, ops):
    allowed = {"sent_quote": row["sent_basis"]["file_sha256"] if row["sent_basis"] else None,
               "incoming_quote": row["incoming_sha256"]}
    if content["incoming_quote"] is None and content["sent_quote"] is None:
        p._fail("issue_source_missing")
    for name in ("sent_quote", "incoming_quote", "working_quote"):
        quote = content[name]
        if quote is None:
            continue
        permitted = ({w["file_sha256"] for w in works} if name == "working_quote" else {allowed[name]})
        if (quote["file_sha256"] not in permitted
                or quote["paragraph_ref"]["file_sha256"] != quote["file_sha256"]):
            p._fail("issue_source_mismatch")
    if not set(content["operation_ids"]) <= {o["operation_id"] for o in ops}:
        p._fail("operation_missing")


def validate(store):
    """Replay only links, never edits, to validate order and referential integrity."""
    if store["schema_version"] == "deal_rounds_store.v1":
        return
    rows = {r["round_id"]: r for r in store["rounds"]}
    events = {}
    for name in COLLECTIONS:
        for row in store[name]:
            if row["request_id"] in events:
                p._fail("storage_corrupt")
            events[row["request_id"]] = (name, row)
    seen_ops, seen_works, seen_issues = [], [], {}
    orders = {name: [] for name in COLLECTIONS}
    try:
        for request in store["requests"]:
            action = request["action"]
            if action not in TABLE:
                continue
            name = TABLE[action]
            kind, event = events.pop(request["request_id"])
            if kind != name or event["round_id"] != request["round_id"]:
                p._fail("storage_corrupt")
            orders[name].append(event)
            rid = event["round_id"]
            row = rows[rid]
            ops = [o for o in seen_ops if o["round_id"] == rid]
            works = [w for w in seen_works if w["round_id"] == rid]
            if action == "link_operation":
                absolute(event["source_path"])
                relative(event["output_path"])
                if any(o["operation_id"] == event["operation_id"] for o in seen_ops):
                    p._fail("storage_corrupt")
                if event["save_mode"] == "create_new":
                    if event["source_sha256"] not in _roles(row) | {w["file_sha256"] for w in works}:
                        p._fail("storage_corrupt")
                elif (not works or event["source_sha256"] != works[-1]["file_sha256"]
                      or event["output_path"] != works[-1]["path"]):
                    p._fail("storage_corrupt")
                seen_ops.append(event)
            elif action in {"link_working_document", "record_working_version"}:
                relative(event["path"])
                if event["previous_request_id"] != (works[-1]["request_id"] if works else None):
                    p._fail("storage_corrupt")
                if action == "record_working_version":
                    if (not works or event["operation_id"] is not None or event["statement"] is None
                            or event["origin"] != "client_asserted_manual_save"
                            or event["path"] != works[-1]["path"]
                            or event["file_sha256"] == works[-1]["file_sha256"]):
                        p._fail("storage_corrupt")
                    seen_works.append(event)
                    continue
                if event["origin"] != "journaled_operation" or event["statement"] is not None:
                    p._fail("storage_corrupt")
                op = next(o for o in ops if o["operation_id"] == event["operation_id"])
                if (event["file_sha256"] != op["candidate_sha256"] or event["path"] != op["output_path"]
                        or any(w["operation_id"] == op["operation_id"] for w in seen_works)):
                    p._fail("storage_corrupt")
                # An in-place result must still extend the latest selected copy.
                if op["save_mode"] == "replace_source" and (not works
                        or works[-1]["file_sha256"] != op["source_sha256"]
                        or works[-1]["path"] != op["output_path"]):
                    p._fail("storage_corrupt")
                seen_works.append(event)
            else:
                _content(event["content"], row, works, ops)
                old = seen_issues.get(event["issue_id"])
                if old is not None and old["round_id"] != rid:
                    p._fail("storage_corrupt")
                if action == "save_issue":
                    if event["version"] != (old["version"] + 1 if old else 1) or event["decision"] is not None:
                        p._fail("storage_corrupt")
                elif action == "link_issue_operation":
                    old_ids = old["content"]["operation_ids"] if old else []
                    new_ids = event["content"]["operation_ids"]
                    if (old is None or event["version"] != old["version"] or event["decision"] != old["decision"]
                            or {k: v for k, v in old["content"].items() if k != "operation_ids"}
                            != {k: v for k, v in event["content"].items() if k != "operation_ids"}
                            or len(new_ids) != len(old_ids) + 1 or new_ids[:-1] != old_ids):
                        p._fail("storage_corrupt")
                elif (old is None or old["version"] != event["version"] or old["decision"] is not None
                      or event["decision"] is None or old["content"] != event["content"]
                      or event["decision"]["basis_sha256"] != (works[-1]["file_sha256"] if works else row["incoming_sha256"])):
                    p._fail("storage_corrupt")
                seen_issues[event["issue_id"]] = event
        if events or any(orders[name] != store[name] for name in COLLECTIONS):
            p._fail("storage_corrupt")
    except (KeyError, StopIteration, p.PositionError):
        p._fail("storage_corrupt")


def operation_observation(op, folder):
    result = {"operation_id": op["operation_id"], "status": "unavailable",
              "publication_status": None, "verification": None,
              "operation_error": None, "current_output_sha256": None, "backup_current_verified": None}
    try:
        observed = journal.read_operation(op["source_path"], op["operation_id"])
    except (DocxError, OSError):
        return result
    intent = observed["intent"]
    if (observed["intent_sha256"] != op["intent_sha256"]
            or any(op[key] != intent[key] for key in ("source_sha256", "candidate_sha256", "save_mode"))
            or intent["destination"] != os.path.join(os.path.expanduser(folder), op["output_path"])):
        return {**result, "status": "intent_changed"}
    record = observed["result"]
    return {**result, "status": "prepared_only" if record is None else "completed",
            "publication_status": record["publication_status"] if record else None,
            "verification": record["verification"] if record else None,
            "operation_error": record["error_code"] if record else None,
            "current_output_sha256": observed["current_output_sha256"],
            "backup_current_verified": observed["backup_current_verified"]}


def _position_status(link, current):
    if link is None:
        return "not_recorded"
    if current is None or current["matter_id"] != link["matter_id"]:
        return "unavailable"
    count = link["history_sequence"]
    if count > len(current["history"]):
        return "unavailable"
    history = current["history"][:count]
    rows = {e["position"]["position_id"]: e["position"] for e in history}
    historical = {**current, "positions": [rows[k] for k in sorted(rows)], "history": history}
    pos = rows.get(link["position_id"])
    if (p._revision(historical) != link["revision"] or pos is None or pos["version"] != link["version"]):
        return "unavailable"
    return "current" if current["revision"] == link["revision"] else "historical_available"


def _current_position(link, current):
    return (_position_status(link, current) == "current" and any(
        pos["position_id"] == link["position_id"] and pos["lifecycle"] == "active"
        for pos in current["positions"]))


def observations(folder, store, positions_store, offset):
    result = {name: deepcopy(store.get(name, [])) if store else [] for name in COLLECTIONS}
    result["issues"] = deepcopy(issues(store)) if store else []
    result["operation_observations"] = [operation_observation(op, folder) if offset <= n < offset + MAX_OPERATION_OBSERVATIONS else
        {"operation_id": op["operation_id"], "status": "not_checked", "publication_status": None,
         "verification": None, "operation_error": None, "current_output_sha256": None, "backup_current_verified": None}
        for n, op in enumerate(result["operations"])]
    result["working_observations"] = []
    retry_from = None
    with _sources(folder) as sources:
        for n, work in enumerate(result["working_documents"]):
            if retry_from is not None or not offset <= n < offset + MAX_OPERATION_OBSERVATIONS:
                result["working_observations"].append({"request_id": work["request_id"], "status": "not_checked"})
                continue
            try:
                payload = sources.payload(work["path"])
                status = "same_bytes" if hashlib.sha256(payload).hexdigest() == work["file_sha256"] else "changed"
            except p.PositionError as exc:
                # Retry a shortened page with a fresh budget. A file that fails
                # even as the first item is unavailable; do not loop forever.
                if exc.code == "resource_limit_exceeded" and n > offset:
                    retry_from = n
                    status = "not_checked"
                else:
                    status = "unavailable"
            result["working_observations"].append({"request_id": work["request_id"], "status": status})
    total = max(len(result["operations"]), len(result["working_documents"]))
    next_offset = retry_from if retry_from is not None else offset + MAX_OPERATION_OBSERVATIONS
    result["observation_page"] = {"offset": offset, "page_size": MAX_OPERATION_OBSERVATIONS,
        "total": total, "next_offset": next_offset if next_offset < total else None}
    by_work = {w["request_id"]: obs["status"] for w, obs in zip(result["working_documents"], result["working_observations"])}
    rounds = {r["round_id"]: r for r in store["rounds"]} if store else {}
    result["issue_observations"] = []
    for issue in result["issues"]:
        decision = issue["decision"]
        works = working(store, issue["round_id"])
        status = "not_recorded"
        if decision is not None:
            status = "current_basis" if decision["basis_sha256"] == basis(store, rounds[issue["round_id"]]) else "basis_changed"
            if works and by_work[works[-1]["request_id"]] != "same_bytes":
                observed = by_work[works[-1]["request_id"]]
                if status != "basis_changed":
                    status = {"changed": "basis_changed", "not_checked": "not_checked", "unavailable": "unavailable"}[observed]
            if _position_status(issue["content"]["position"], positions_store) in {"unavailable", "historical_available"}:
                status = "unavailable" if _position_status(issue["content"]["position"], positions_store) == "unavailable" else "basis_changed"
        result["issue_observations"].append({"issue_id": issue["issue_id"],
            "position_status": _position_status(issue["content"]["position"], positions_store), "decision_status": status})
    return result


def _verify_quotes(folder, content, snapshot_path):
    # Exact offsets in the existing supported paragraph reading mode. Deleted
    # text and uncovered stories are not reconstructed as quotes.
    parsed = {}
    for name in ("sent_quote", "incoming_quote", "working_quote"):
        quote = content[name]
        if quote is None:
            continue
        digest = quote["file_sha256"]
        if digest not in parsed:
            snap = snapshots.load_snapshot(snapshot_path(folder, digest))
            parsed[digest] = _load_snapshot_from_payload(snap.payload, path="round-snapshot.docx")
        try:
            paragraph = _resolve_paragraph(parsed[digest], quote["paragraph_ref"])
            start = quote["start"]
            if paragraph.text[start:start + len(quote["text"])] != quote["text"]:
                p._fail("quote_mismatch")
        except DocxError:
            p._fail("quote_mismatch")


def change(folder, store, positions_store, request_id, command, snapshot_path):
    """Mutate links only; document publication/recovery remain separate calls."""
    rid, action = command["round_id"], command["action"]
    row = next((r for r in store["rounds"] if r["round_id"] == rid), None)
    if row is None:
        p._fail("round_missing")
    meta = {"request_id": request_id, "round_id": rid}
    ops = [o for o in store["operations"] if o["round_id"] == rid]
    works = working(store, rid)
    snapshot = None
    if action == "link_operation":
        absolute(command["source_path"])
        if any(o["operation_id"] == command["operation_id"] for o in store["operations"]):
            p._fail("operation_exists")
        observed = journal.read_operation(command["source_path"], command["operation_id"])
        intent = observed["intent"]
        path = relative(os.path.relpath(intent["destination"], os.path.expanduser(folder)))
        if intent["save_mode"] == "create_new":
            if intent["source_sha256"] not in _roles(row) | {w["file_sha256"] for w in works}:
                p._fail("operation_source_mismatch")
        elif (not works or works[-1]["file_sha256"] != intent["source_sha256"]
              or works[-1]["path"] != path or command["source_path"] != intent["destination"]):
            p._fail("working_basis_mismatch")
        store["operations"].append({**meta, "operation_id": command["operation_id"],
            "source_path": command["source_path"], "intent_sha256": observed["intent_sha256"],
            "source_sha256": intent["source_sha256"], "candidate_sha256": intent["candidate_sha256"],
            "output_path": path, "save_mode": intent["save_mode"]})
    elif action == "link_working_document":
        op = next((o for o in ops if o["operation_id"] == command["operation_id"]), None)
        if op is None:
            p._fail("operation_missing")
        obs = operation_observation(op, folder)
        if (obs["status"] != "completed" or obs["publication_status"] != "published"
                or obs["verification"] != "matched" or obs["operation_error"] is not None
                or obs["current_output_sha256"] != op["candidate_sha256"] or not obs["backup_current_verified"]):
            p._fail("operation_unverified")
        if any(w["operation_id"] == op["operation_id"] for w in store["working_documents"]):
            p._fail("working_document_exists")
        if op["save_mode"] == "replace_source" and (not works or works[-1]["path"] != op["output_path"]
                or works[-1]["file_sha256"] != op["source_sha256"]):
            p._fail("working_basis_mismatch")
        path = os.path.join(os.path.expanduser(folder), op["output_path"])
        snapshot = snapshots.capture_document_snapshot(path, op["candidate_sha256"])
        store["working_documents"].append({**meta, "operation_id": op["operation_id"],
            "path": op["output_path"], "file_sha256": snapshot.sha256,
            "previous_request_id": works[-1]["request_id"] if works else None,
            "origin": "journaled_operation", "statement": None})
    elif action == "record_working_version":
        if not works or works[-1]["request_id"] != command["expected_working_request_id"]:
            p._fail("working_basis_mismatch")
        if command["file_sha256"] == works[-1]["file_sha256"]:
            p._fail("working_version_unchanged")
        path = os.path.join(os.path.expanduser(folder), works[-1]["path"])
        if journal.observed_hash(path) != command["file_sha256"]:
            p._fail("working_basis_mismatch")
        snapshot = snapshots.capture_document_snapshot(path, command["file_sha256"])
        store["working_documents"].append({**meta, "operation_id": None,
            "path": works[-1]["path"], "file_sha256": snapshot.sha256,
            "previous_request_id": works[-1]["request_id"], "origin": "client_asserted_manual_save",
            "statement": command["statement"]})
    else:
        old = next((i for i in issues(store) if i["issue_id"] == command["issue_id"]), None)
        if ((old is not None and old["round_id"] != rid)
                or command["expected_version"] != (old["version"] if old else None)):
            p._fail("issue_version_conflict")
        if action == "save_issue":
            content = deepcopy(command["content"])
            _content(content, row, works, ops)
            _verify_quotes(folder, content, snapshot_path)
            if content["position"] is not None and not _current_position(content["position"], positions_store):
                p._fail("position_basis_mismatch")
            event = {**meta, "issue_id": command["issue_id"], "version": old["version"] + 1 if old else 1,
                     "content": content, "decision": None}
        elif action == "link_issue_operation":
            if (old is None or command["operation_id"] not in {o["operation_id"] for o in ops}
                    or command["operation_id"] in old["content"]["operation_ids"]):
                p._fail("issue_operation_conflict")
            event = {**deepcopy(old), **meta}
            event["content"]["operation_ids"].append(command["operation_id"])
        else:
            if old is None or old["decision"] is not None:
                p._fail("issue_version_conflict")
            if command["basis_sha256"] != basis(store, row):
                p._fail("working_basis_mismatch")
            if old["content"]["position"] is not None and not _current_position(old["content"]["position"], positions_store):
                p._fail("position_basis_mismatch")
            if works:
                with _sources(folder) as sources:
                    if hashlib.sha256(sources.payload(works[-1]["path"])).hexdigest() != basis(store, row):
                        p._fail("working_basis_mismatch")
            event = {**deepcopy(old), **meta, "decision": {"choice": command["choice"],
                     "statement": command["statement"], "basis_sha256": command["basis_sha256"],
                     "basis": "client_asserted_lawyer_decision"}}
        store["issue_events"].append(event)
    return snapshot
