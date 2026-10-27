# SPDX-License-Identifier: Apache-2.0
"""Complete bounded matter review before the separate finalization workflow."""
from collections import Counter
from copy import deepcopy
import hashlib

from veqtor_docx import clean_copy
from veqtor_docx.apply import ApplyError
from veqtor_docx.revision_inventory import inventory_package_revisions

from . import deal_state as d, positions as p, _deal_workflow as w
from ._round_issue_brief import _Quotes, _position_version
from . import _finalization_review_contract as c

MAX_REVIEW_OPERATIONS = 20


def _document(payload, digest):
    inventory = inventory_package_revisions(payload)
    groups = Counter((b["code"], b["part_name"]) for b in inventory["blockers"])
    result = {"inventory_complete": inventory["complete"],
        "revision_count": len(inventory["revisions"]),
        "coverage_blockers": [{"code": code, "part_name": part, "count": count}
            for (code, part), count in sorted(groups.items())],
        "clean_probe": "not_attempted_coverage_blocked", "clean_probe_error": None}
    if result["revision_count"]:
        result["clean_probe"] = "not_attempted_pending_revisions"
    elif inventory["complete"] and not inventory["blockers"]:
        # No implicit accept-all: only revision-free bytes enter the existing
        # engine. Keep its package/protection/verification guards unchanged.
        try:
            candidate = clean_copy.prepare_clean_copy(payload, expected_source_sha256=digest)
            if candidate.accepted_revision_count != 0 or candidate.payload != payload:
                p._fail("round_trip_failed")
        except ApplyError as exc:
            result.update(clean_probe="refused", clean_probe_error=exc.code)
        else:
            result["clean_probe"] = "passed_revision_free"
    return result


def read_finalization_review(folder, expected_revision, expected_positions_revision,
                             round_id, basis_sha256):
    d._validate(c.SHA, expected_revision)
    d._validate(c.nullable(c.SHA), expected_positions_revision)
    d._validate(d.c.ROUND_ID, round_id)
    d._validate(c.SHA, basis_sha256)
    with p._storage(folder, write=False, load_store=d._load) as storage:
        if storage is None:
            p._fail("round_missing")
        _, side, recheck = storage
        store, _ = d._load(side)
        positions, _ = p._load(side)
        if store is None or store["revision"] != expected_revision:
            p._fail("revision_conflict")
        if (positions["revision"] if positions else None) != expected_positions_revision:
            p._fail("positions_revision_conflict")
        row = next((r for r in store["rounds"] if r["round_id"] == round_id), None)
        if row is None:
            p._fail("round_missing")
        if w.basis(store, row) != basis_sha256:
            p._fail("working_basis_mismatch")
        # No selection or pagination can hide an open issue or operation,
        # including older rounds or another saved continuation of this matter.
        operations = store.get("operations", [])
        if len(operations) > MAX_REVIEW_OPERATIONS:
            p._fail("resource_limit_exceeded")
        quotes = _Quotes(folder)
        snapshot = quotes.load(basis_sha256)
        works = w.working(store, round_id)
        work = works[-1] if works else None
        working_status = "not_selected"
        if work:
            with w._sources(folder) as sources:
                try:
                    payload = sources.payload(work["path"])
                except p.PositionError as exc:
                    if exc.code == "resource_limit_exceeded":
                        raise
                    working_status = "unavailable"
                else:
                    working_status = "same_bytes" if hashlib.sha256(payload).hexdigest() == basis_sha256 else "changed"
        blockers = []
        if working_status in {"changed", "unavailable"}:
            blockers.append("working_file_" + working_status)
        if any(r["previous_round_id"] == round_id for r in store["rounds"]):
            blockers.append("selected_round_has_successor")
        # Replacing an issue can remove its old quote or position link. Keep
        # and verify the whole history rather than erasing conditional terms
        # from this review merely because the latest issue no longer links them.
        issue_history = []
        for event in store.get("issue_events", []):
            for name in ("incoming_quote", "sent_quote", "working_quote"):
                quotes.verify(event["content"][name])
            link = event["content"]["position"]
            status = w._position_status(link, positions)
            issue_history.append({"event": deepcopy(event), "position_status": status,
                "position_version": _position_version(link, positions, status)})
        issues = []
        for issue in w.issues(store):
            content, decision = issue["content"], issue["decision"]
            status = w._position_status(content["position"], positions)
            reasons = []
            if decision is None:
                reasons.append("undecided")
            else:
                if decision["choice"] == "defer":
                    reasons.append("deferred")
                if decision["basis_sha256"] != basis_sha256:
                    reasons.append("decision_basis_changed")
            if content["remaining_question"] is not None:
                reasons.append("remaining_question")
            if status in {"historical_available", "unavailable"}:
                reasons.append("position_basis_changed")
            issues.append({"issue": deepcopy(issue), "position_status": status,
                "position_version": _position_version(content["position"], positions, status),
                "blockers": reasons})
        if any(i["blockers"] for i in issues):
            blockers.append("unresolved_issues")
        observed = []
        for op in operations:
            obs = w.operation_observation(op, folder)
            outcome = "unresolved"
            if obs["status"] == "completed":
                if obs["publication_status"] == "not_published":
                    outcome = "recorded_no_publication"
                elif (obs["publication_status"] == "published" and obs["verification"] == "matched"
                      and obs["operation_error"] is None):
                    outcome = "recorded_success"
            observed.append({"link": deepcopy(op), "observation": obs, "outcome": outcome})
        if any(o["outcome"] == "unresolved" for o in observed):
            blockers.append("unresolved_operations")
        document = _document(snapshot.payload, basis_sha256)
        if document["revision_count"]:
            blockers.append("pending_revisions")
        if not document["inventory_complete"] or document["coverage_blockers"]:
            blockers.append("package_coverage_blocked")
        if document["clean_probe"] == "refused":
            blockers.append("clean_probe_refused")
        data = {"revision": store["revision"], "positions_revision": expected_positions_revision,
            "selected_round_id": round_id, "included_round_ids": [r["round_id"] for r in store["rounds"]],
            "scope": "all_saved_matter_rounds", "basis": {"file_sha256": basis_sha256,
                "snapshot_verified": True, "working_document": deepcopy(work), "working_status": working_status},
            "issues": issues, "issue_history": issue_history,
            "positions": deepcopy(positions["positions"]) if positions else [],
            "position_history": deepcopy(positions["history"]) if positions else [],
            "operations": observed, "document": document, "automatic_blockers": blockers,
            "required_manual_checks": list(c.MANUAL_CHECKS), "limitations": list(c.LIMITATIONS)}
        payload = p._json_bytes(data)
        recheck()
        return {"schema_version": "finalization_review.v1", "status": "ok", "audience": "internal_review",
            "readiness": "incomplete", "review_sha256": hashlib.sha256(payload).hexdigest(), "data": data}
