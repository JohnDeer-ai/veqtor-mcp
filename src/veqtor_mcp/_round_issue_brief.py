# SPDX-License-Identifier: Apache-2.0
"""Checked selected issues from the original stores; never a second issue store."""
from copy import deepcopy
import hashlib

from veqtor_docx import round_snapshots as snapshots
from veqtor_docx._ooxml import DocxError
from veqtor_docx.inspect import _load_snapshot_from_payload, _resolve_paragraph

from . import deal_state as d, positions as p, _deal_workflow as w
from . import _round_issue_brief_contract as c

LIMITATIONS = [
    "internal_data_includes_private_assessments_positions_and_decision_statements",
    "external_document_requires_explicit_field_selection_and_review",
    "quotes_verified_in_supported_paragraph_reading_mode_only",
    "working_quotes_are_saved_snapshots_not_inferred_final_wording",
    "proposals_decisions_and_operation_results_are_distinct",
    "operation_links_do_not_prove_which_clause_or_comment_was_changed",
    "comment_relationships_are_not_recorded_and_are_not_inferred",
    "selection_completeness_is_not_completeness_of_contract_review",
    "live_file_and_journal_observations_are_not_frozen_by_state_revision",
    "no_sending_legal_agreement_or_finalization_inferred",
]


class _Quotes:
    def __init__(self, folder):
        self.folder = folder
        self.snapshots = {}
        self.parsed = {}
        self.total = 0

    def load(self, digest):
        if digest not in self.snapshots:
            if len(self.snapshots) >= p.MAX_SOURCE_FILES:
                p._fail("resource_limit_exceeded")
            snap = snapshots.load_snapshot(d._snapshot_path(self.folder, digest))
            if snap.sha256 != digest:
                p._fail("snapshot_conflict")
            self.total += snap.size_bytes
            if self.total > d.MAX_DOCUMENT_BYTES:
                p._fail("resource_limit_exceeded")
            self.snapshots[digest] = snap
        return self.snapshots[digest]

    def verify(self, quote):
        if quote is None:
            return
        digest = quote["file_sha256"]
        snap = self.load(digest)
        try:
            if digest not in self.parsed:
                self.parsed[digest] = _load_snapshot_from_payload(snap.payload, path="round-snapshot.docx")
            paragraph = _resolve_paragraph(self.parsed[digest], quote["paragraph_ref"])
            start = quote["start"]
            if paragraph.text[start:start + len(quote["text"])] != quote["text"]:
                p._fail("quote_mismatch")
        except DocxError:
            p._fail("quote_mismatch")


def _position_version(link, current, status):
    if status in {"not_recorded", "unavailable"}:
        return None
    # The history boundary, revision, matter and version were checked by the
    # original workflow observer. Never substitute today's concession terms.
    rows = {e["position"]["position_id"]: e["position"]
            for e in current["history"][:link["history_sequence"]]}
    return deepcopy(rows[link["position_id"]])


def read_round_issue_brief(folder, expected_revision, expected_positions_revision, round_id, issue_ids):
    d._validate(c.SHA, expected_revision)
    d._validate(c.nullable(c.SHA), expected_positions_revision)
    d._validate(d.c.ROUND_ID, round_id)
    d._validate(c.SELECTION, issue_ids)
    with p._storage(folder, write=False, load_store=d._load) as storage:
        if storage is None:
            p._fail("round_missing")
        _, side, recheck = storage
        store, _ = d._load(side)
        positions, _ = p._load(side)
        if store is None or store["revision"] != expected_revision:
            p._fail("revision_conflict")
        positions_revision = positions["revision"] if positions else None
        if positions_revision != expected_positions_revision:
            p._fail("positions_revision_conflict")
        round_row = next((r for r in store["rounds"] if r["round_id"] == round_id), None)
        if round_row is None:
            p._fail("round_missing")
        available = {i["issue_id"]: i for i in w.issues(store) if i["round_id"] == round_id}
        if not set(issue_ids) <= set(available):
            p._fail("issue_missing")
        selected = [available[key] for key in issue_ids]
        operation_ids = {oid for i in selected for oid in i["content"]["operation_ids"]}
        if len(operation_ids) > w.MAX_OPERATION_OBSERVATIONS:
            p._fail("resource_limit_exceeded")
        operations = [o for o in store.get("operations", []) if o["operation_id"] in operation_ids]
        works = w.working(store, round_id)[-1:]
        observed = w.observations(folder, {"rounds": [round_row], "issue_events": selected,
            "operations": operations, "working_documents": works}, positions, 0)
        basis = w.basis(store, round_row)
        quotes = _Quotes(folder)
        quotes.load(round_row["incoming_sha256"])
        quotes.load(basis)
        if round_row["sent_basis"]:
            quotes.load(round_row["sent_basis"]["file_sha256"])
        rows = []
        for issue, observation in zip(selected, observed["issue_observations"]):
            content = issue["content"]
            for key in ("sent_quote", "incoming_quote", "working_quote"):
                quotes.verify(content[key])
            working_quote = content["working_quote"]
            rows.append({"issue": deepcopy(issue), "observation": observation,
                "quote_status": {
                    "sent": "verified_snapshot" if content["sent_quote"] else "missing",
                    "incoming": "verified_snapshot" if content["incoming_quote"] else "missing",
                    "working": ("missing" if working_quote is None else "verified_current_snapshot"
                        if working_quote["file_sha256"] == basis else "verified_historical_snapshot")},
                "position_version": _position_version(content["position"], positions, observation["position_status"]),
                "related_comments": "not_recorded_in_issue_state"})
        data = {"revision": store["revision"], "positions_revision": positions_revision,
            "round_id": round_id, "selected_issue_ids": list(issue_ids),
            "omitted_issue_ids": [key for key in available if key not in issue_ids],
            "round_issue_count": len(available),
            "basis": {"incoming_sha256": round_row["incoming_sha256"], "sent_basis": deepcopy(round_row["sent_basis"]),
                "current_sha256": basis, "working_document": deepcopy(works[0]) if works else None,
                "working_status": observed["working_observations"][0]["status"] if works else "not_selected"},
            "issues": rows, "operations": [{"link": deepcopy(op), "observation": obs}
                for op, obs in zip(operations, observed["operation_observations"])],
            "limitations": list(LIMITATIONS)}
        # The dataset digest covers data only, including observations and order.
        payload = p._json_bytes(data)
        recheck()
        return {"schema_version": "round_issue_brief.v1", "status": "ok", "audience": "internal_review",
                "dataset_sha256": hashlib.sha256(payload).hexdigest(), "data": data}
