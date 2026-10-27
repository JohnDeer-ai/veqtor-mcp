# SPDX-License-Identifier: Apache-2.0
"""Bounded private comparison of an exact candidate and its saved Word copy.

This module prepares evidence, never a legal approval or a DOCX transformation.
Every package change remains visible and must be reconciled by the caller.
"""

from copy import deepcopy
from lxml import etree
import hashlib
import json

from veqtor_docx import incoming_comparison, outgoing_cleanup, round_snapshots
from veqtor_docx._ooxml import load_validated_docx, parse_xml, w
from veqtor_docx.revision_inventory import inventory_package_revisions

from . import positions as p
from ._finalization_review import _document

MAX_REPORT_BYTES = 1024 * 1024
MAX_CHANGED_PARTS = 200


def _xml_roots(package, payload):
    # Use the inventory's content-type-aware discovery, not filename suffixes.
    return {
        row["part_name"]: parse_xml(package.parts[row["part_name"]])
        for row in inventory_package_revisions(payload)["parts"]
        if row["inspection"] in {"xml", "opaque_user_storage"}
    }


def _properties(roots):
    tags = {tag for _, tag, _ in outgoing_cleanup.PROPS.values()}
    return {
        name: etree.tostring(root, encoding="unicode")
        for name, root in roots.items()
        if root.tag in tags
    }


def _canonical_properties(roots):
    relationships = roots.get("_rels/.rels")
    if relationships is None:
        return False
    for path, tag, relation in outgoing_cleanup.PROPS.values():
        names = [name for name, root in roots.items() if root.tag == tag]
        links = [node for node in relationships if node.get("Type") == relation]
        if not names and not links:
            continue
        if (
            names != [path]
            or len(links) != 1
            or links[0].get("Target") not in {path, "/" + path}
            or links[0].get("TargetMode", "Internal") != "Internal"
        ):
            return False
    return True


def build(candidate, saved, *, candidate_properties_policy="preserve_all"):
    """Return a complete immutable-byte review or refuse the whole bounded read."""
    candidate = round_snapshots.DocumentSnapshot(
        candidate.payload, candidate.sha256, candidate.size_bytes
    )
    saved = round_snapshots.DocumentSnapshot(
        saved.payload, saved.sha256, saved.size_bytes
    )
    before = load_validated_docx(candidate.payload, capture=None)
    after = load_validated_docx(saved.payload, capture=None)
    before_roots = _xml_roots(before, candidate.payload)
    after_roots = _xml_roots(after, saved.payload)
    document = _document(saved.payload, saved.sha256)
    forbidden = {
        w(n)
        for n in ("comment", "commentReference", "commentRangeStart", "commentRangeEnd")
    }
    comments = sum(
        n.tag in forbidden for root in after_roots.values() for n in root.iter()
    )
    comparison = incoming_comparison.build_incoming_comparison(candidate, saved)
    metadata = json.loads(comparison.metadata)
    changed = [
        {
            "part_name": name,
            "candidate_sha256": hashlib.sha256(before.parts[name]).hexdigest()
            if name in before.parts
            else None,
            "saved_sha256": hashlib.sha256(after.parts[name]).hexdigest()
            if name in after.parts
            else None,
        }
        for name in sorted(before.parts.keys() | after.parts.keys())
        if before.parts.get(name) != after.parts.get(name)
    ]
    if len(changed) > MAX_CHANGED_PARTS:
        p._fail("resource_limit_exceeded")
    blockers = []
    if document["revision_count"]:
        blockers.append("pending_revisions")
    if not document["inventory_complete"] or document["coverage_blockers"]:
        blockers.append("package_coverage_blocked")
    if document["clean_probe"] != "passed_revision_free":
        blockers.append("clean_probe_not_passed")
    if comments:
        blockers.append("comments_remaining")
    if not metadata["coverage"]["supported_scope_complete"]:
        blockers.append("comparison_scope_incomplete")
    _, properties_policy = outgoing_cleanup.policies(
        properties=candidate_properties_policy
    )
    if not _canonical_properties(before_roots) or not _canonical_properties(
        after_roots
    ):
        blockers.append("metadata_layout_unsupported")
    reappeared = []
    if properties_policy != "preserve_all":
        for field in properties_policy["fields"]:
            _, tag, _ = outgoing_cleanup.PROPS[field["part"]]
            for path, root in after_roots.items():
                if root.tag != tag:
                    continue
                matches = [
                    n
                    for n in root
                    if isinstance(n.tag, str)
                    and (
                        n.get("name") == field["name"]
                        if field["part"] == "custom"
                        else etree.QName(n).localname == field["name"]
                    )
                ]
                if matches:
                    reappeared.append(
                        {
                            **field,
                            "part_name": path,
                            "xml_values": [
                                etree.tostring(n, encoding="unicode") for n in matches
                            ],
                        }
                    )
    data = {
        "schema_version": "final_saved_review.v1",
        "audience": "internal_review",
        "readiness": "lawyer_confirmation_required",
        "candidate_sha256": candidate.sha256,
        "saved_sha256": saved.sha256,
        "saved_size_bytes": saved.size_bytes,
        "document": document,
        "ordinary_comment_nodes": comments,
        "candidate_properties_policy": deepcopy(properties_policy),
        "reappeared_selected_properties": reappeared,
        "automatic_blockers": blockers,
        "changed_parts": changed,
        "properties_before": _properties(before_roots),
        "properties_after": _properties(after_roots),
        "comparison": {
            "metadata": metadata,
            "items": [json.loads(i) for i in comparison.items],
            "sha256": comparison.digest,
        },
        "limitations": [
            "no_document_equivalence_claim",
            "no_field_or_property_mutation",
            "word_and_legal_checks_require_explicit_client_assertions",
            "no_actual_send_or_signature_readiness",
        ],
    }
    payload = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(payload) > MAX_REPORT_BYTES:
        p._fail("resource_limit_exceeded")
    return payload, hashlib.sha256(payload).hexdigest()


def validate_report(raw, expected_sha256):
    payload = raw.encode("utf-8")
    if (
        len(payload) > MAX_REPORT_BYTES
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        p._fail("storage_corrupt")
    data = json.loads(
        raw,
        object_pairs_hook=p._pairs,
        parse_constant=lambda _: p._fail("storage_corrupt"),
    )
    if data.get("schema_version") != "final_saved_review.v1":
        p._fail("storage_corrupt")
    return deepcopy(data)


COLLECTIONS = ("final_saved_reviews", "final_confirmations")
ASSERTIONS = (
    "statement",
    "manual_checks",
    "changed_part_resolutions",
    "issue_resolutions",
    "position_resolutions",
)


def upgrade(store):
    from . import _deal_finalization as final

    result = final.upgrade(store)
    result["schema_version"] = "deal_rounds_store.v9"
    for name in COLLECTIONS:
        result.setdefault(name, [])
    return result


def _prepare_command(row):
    return {
        "action": "prepare_final_saved_review",
        **{
            k: row[k]
            for k in (
                "round_id",
                "document_request_id",
                "plan_sha256",
                "path",
                "file_sha256",
            )
        },
    }


def _confirm_command(row):
    return {
        "action": "confirm_final_saved_document",
        "user_confirmed": True,
        **{
            k: deepcopy(row[k])
            for k in (
                "round_id",
                "saved_review_request_id",
                "report_sha256",
                "file_sha256",
                *ASSERTIONS,
            )
        },
    }


def _report(row):
    data = validate_report(row["report_utf8"], row["report_sha256"])
    if (
        data["candidate_sha256"] != row["candidate_sha256"]
        or data["saved_sha256"] != row["file_sha256"]
        or data["readiness"] != "lawyer_confirmation_required"
    ):
        p._fail("storage_corrupt")
    return data


def _assertions(report, agreement, command):
    from . import _deal_finalization as final

    if report["automatic_blockers"]:
        p._fail("final_saved_document_blocked")
    rows = command["changed_part_resolutions"]
    if len(rows) != len({r["part_name"] for r in rows}) or sorted(
        r["part_name"] for r in rows
    ) != [r["part_name"] for r in report["changed_parts"]]:
        p._fail("final_changed_parts_unresolved")
    final._resolutions(final._review(agreement), command)


def validate(store):
    from . import (
        _deal_finalization as final,
        _deal_workflow as workflow,
        _deal_state_contract as c,
    )

    if store["schema_version"] != "deal_rounds_store.v9":
        return
    groups = {name: {r["request_id"]: r for r in store[name]} for name in COLLECTIONS}
    orders = {name: [] for name in COLLECTIONS}
    prefix = {
        name: []
        for name in ("rounds", "operations", "working_documents", "issue_events")
    }
    rounds = {r["round_id"]: r for r in store["rounds"]}
    current_rounds, documents, agreements, reviews = {}, {}, {}, {}
    links = {
        name: {r["request_id"]: r for r in store[name]}
        for name in ("operations", "working_documents", "issue_events")
    }
    all_documents = {r["request_id"]: r for r in store["final_documents"]}
    all_agreements = {r["request_id"]: r for r in store["final_basis_agreements"]}
    events = {r["request_id"]: r for r in store["final_document_events"]}
    observed_documents = set()
    try:
        for request in store["requests"]:
            key, rid, action = (
                request["request_id"],
                request["round_id"],
                request["action"],
            )
            if action == "start_round":
                row = {**deepcopy(rounds[rid]), "sent_documents": []}
                prefix["rounds"].append(row)
                current_rounds[rid] = row
            elif action == "record_sent":
                current_rounds[rid]["sent_documents"].append(
                    next(
                        r
                        for r in rounds[rid]["sent_documents"]
                        if r["request_id"] == key
                    )
                )
            for name in links:
                if key in links[name]:
                    prefix[name].append(links[name][key])
            if key in all_agreements:
                agreements[key] = all_agreements[key]
            if key in all_documents:
                documents[key] = all_documents[key]
            if key in events:
                observed_documents.add(events[key]["document_request_id"])
            if action not in c.FINAL_SAVED_ACTIONS:
                continue
            collection = (
                COLLECTIONS[0]
                if action == "prepare_final_saved_review"
                else COLLECTIONS[1]
            )
            row = groups[collection].pop(key)
            if row["round_id"] != rid:
                p._fail("storage_corrupt")
            if action == "prepare_final_saved_review":
                document = documents[row["document_request_id"]]
                agreement = agreements[document["agreement_request_id"]]
                workflow.relative(row["path"])
                _report(row)
                if (
                    document["round_id"] != rid
                    or document["request_id"] not in observed_documents
                    or document["plan_sha256"] != row["plan_sha256"]
                    or document["file_sha256"] != row["candidate_sha256"]
                    or agreement["positions_revision"] != row["positions_revision"]
                    or row["scope_sha256"] != agreement["scope_sha256"]
                    or row["scope_sha256"]
                    != final.scope(prefix, rid, row["positions_revision"])
                ):
                    p._fail("storage_corrupt")
                reviews[key] = row
                command = _prepare_command(row)
            else:
                review = reviews[row["saved_review_request_id"]]
                document = documents[review["document_request_id"]]
                agreement = agreements[document["agreement_request_id"]]
                if (
                    review["round_id"] != rid
                    or review["file_sha256"] != row["file_sha256"]
                    or review["report_sha256"] != row["report_sha256"]
                    or review["scope_sha256"]
                    != final.scope(prefix, rid, review["positions_revision"])
                ):
                    p._fail("storage_corrupt")
                _assertions(_report(review), agreement, row)
                command = _confirm_command(row)
            if final._hash(command) != request["command_sha256"]:
                p._fail("storage_corrupt")
            orders[collection].append(row)
        if any(groups[name] or orders[name] != store[name] for name in COLLECTIONS):
            p._fail("storage_corrupt")
    except (KeyError, TypeError, ValueError, StopIteration):
        p._fail("storage_corrupt")


def _selected_snapshot(folder, path, digest):
    from . import _deal_workflow as workflow

    with workflow._sources(folder) as sources:
        payload = sources.payload(workflow.relative(path))
    if hashlib.sha256(payload).hexdigest() != digest:
        p._fail("final_saved_file_changed")
    return round_snapshots.DocumentSnapshot(payload, digest, len(payload))


def _document_and_agreement(store, rid, document_id, plan_sha256):
    document = next(
        (r for r in store["final_documents"] if r["request_id"] == document_id), None
    )
    if (
        document is None
        or document["round_id"] != rid
        or document["plan_sha256"] != plan_sha256
        or not any(
            e["document_request_id"] == document_id
            for e in store["final_document_events"]
        )
    ):
        p._fail("final_plan_mismatch")
    agreement = next(
        r
        for r in store["final_basis_agreements"]
        if r["request_id"] == document["agreement_request_id"]
    )
    return document, agreement


def _checked(folder, store, positions, row, snapshot_path):
    from . import _deal_finalization as final

    document, agreement = _document_and_agreement(
        store, row["round_id"], row["document_request_id"], row["plan_sha256"]
    )
    final._current(folder, store, positions, agreement)
    plan = final._plan(folder, document, snapshot_path)
    # Preserve and check the published candidate; do not silently bless an
    # overwritten outgoing kit when reviewing a differently named saved copy.
    final.engine.verify_outgoing_package(
        plan,
        final._destination(folder, document),
        expected_plan_sha256=document["plan_sha256"],
    )
    payload = plan.files[0].payload
    candidate = round_snapshots.DocumentSnapshot(
        payload, document["file_sha256"], len(payload)
    )
    saved = _selected_snapshot(folder, row["path"], row["file_sha256"])
    raw, digest = build(
        candidate, saved, candidate_properties_policy=document["properties_policy"]
    )
    return saved, raw.decode("utf-8"), digest, agreement


def change(folder, store, positions, request_id, command, snapshot_path):
    from . import _deal_finalization as final

    rid = command["round_id"]
    if command["action"] == "prepare_final_saved_review":
        saved, raw, digest, agreement = _checked(
            folder, store, positions, command, snapshot_path
        )
        document, _ = _document_and_agreement(
            store, rid, command["document_request_id"], command["plan_sha256"]
        )
        row = {
            "request_id": request_id,
            **{
                k: deepcopy(command[k])
                for k in (
                    "round_id",
                    "document_request_id",
                    "plan_sha256",
                    "path",
                    "file_sha256",
                )
            },
            "candidate_sha256": document["file_sha256"],
            "report_utf8": raw,
            "report_sha256": digest,
            "positions_revision": agreement["positions_revision"],
            "scope_sha256": agreement["scope_sha256"],
            "stage": "saved_version_requires_confirmation",
        }
        store[COLLECTIONS[0]].append(row)
        return saved, None
    row = next(
        (
            r
            for r in store[COLLECTIONS[0]]
            if r["request_id"] == command["saved_review_request_id"]
        ),
        None,
    )
    if (
        row is None
        or row["round_id"] != rid
        or row["report_sha256"] != command["report_sha256"]
        or row["file_sha256"] != command["file_sha256"]
    ):
        p._fail("final_saved_review_mismatch")
    saved, raw, digest, agreement = _checked(
        folder, store, positions, row, snapshot_path
    )
    if raw != row["report_utf8"] or digest != row["report_sha256"]:
        p._fail("final_saved_review_mismatch")
    if (
        round_snapshots.load_snapshot(snapshot_path(folder, row["file_sha256"]))
        != saved
    ):
        p._fail("snapshot_conflict")
    _assertions(_report(row), agreement, command)
    store[COLLECTIONS[1]].append(
        {
            "request_id": request_id,
            **{
                k: deepcopy(command[k])
                for k in (
                    "round_id",
                    "saved_review_request_id",
                    "report_sha256",
                    "file_sha256",
                    *ASSERTIONS,
                )
            },
            "assurance": "client_asserted_final_saved_version",
        }
    )

    def effect():
        final._current(folder, store, positions, agreement)
        if _selected_snapshot(folder, row["path"], row["file_sha256"]) != saved:
            p._fail("final_saved_file_changed")
        if (
            round_snapshots.load_snapshot(snapshot_path(folder, row["file_sha256"]))
            != saved
        ):
            p._fail("snapshot_conflict")

    return None, effect


def result(folder, store, positions):
    from . import _deal_finalization as final, _deal_workflow as workflow
    from veqtor_docx._ooxml import DocxError

    values = {
        name: deepcopy(store.get(name, [])) if store else [] for name in COLLECTIONS
    }
    observations = []
    for confirmation in values[COLLECTIONS[1]]:
        row = next(
            r
            for r in values[COLLECTIONS[0]]
            if r["request_id"] == confirmation["saved_review_request_id"]
        )
        _, agreement = _document_and_agreement(
            store, row["round_id"], row["document_request_id"], row["plan_sha256"]
        )
        context = "current_checked"
        try:
            final._current(folder, store, positions, agreement)
        except (p.PositionError, DocxError, OSError):
            context = "changed_or_unavailable"
        try:
            with workflow._sources(folder) as sources:
                payload = sources.payload(row["path"])
            status = (
                "same_bytes"
                if hashlib.sha256(payload).hexdigest() == row["file_sha256"]
                else "changed"
            )
        except (p.PositionError, DocxError, OSError):
            status = "unavailable"
        observations.append(
            {
                "confirmation_request_id": confirmation["request_id"],
                "snapshot_sha256": row["file_sha256"],
                "context_status": context,
                "selected_file_status": status,
            }
        )
    values["final_confirmation_observations"] = observations
    return values
