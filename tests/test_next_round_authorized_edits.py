# SPDX-License-Identifier: Apache-2.0
"""F05 controls use actual synthetic DOCX/proofs in fabricated client envelopes.

These are checker tests, not native acceptance or a replay of the failed campaign.
"""
from copy import deepcopy
import json

import pytest

import test_next_round_acceptance as nr03
from test_next_round_acceptance import checker, json_write, mutate_events, native_stage, prep, texts
from nr03_scenario import edit_specs

prepared = nr03.prepared


def payment_edit(edits):
    return next(edit for edit in edits if edit.get("target", {}).get("paragraph_ref", {}).get("paragraph_index") == 4)


def narrow_payment(edits, rows):
    payment_edit(edits).update(delete_text="60", insert_text="45")


def full_payment(edits, rows):
    payment_edit(edits).update(delete_text=texts("incoming-a")[4], insert_text=texts("counter-a")[4])


@pytest.mark.parametrize("transform", [narrow_payment, full_payment], ids=["observed-60-to-45", "full-paragraph"])
def test_f05_exact_authorized_result_allows_alternative_boundaries(prepared, monkeypatch, transform):
    bundle, baseline, installation = prepared
    native_stage(bundle, baseline, installation, "a-brief", monkeypatch)
    native_stage(bundle, baseline, installation, "a-write", monkeypatch, edit_transform=transform)
    report = checker.check_round(bundle, "a")
    assert report["mechanical"]["exact_revisions_verified"]
    assert checker.actual_texts(baseline["inputs"]["a"]["output"]) == texts("counter-a")
    # The second source must retain this actual first output's revision payloads.
    prep.prepare_second(bundle)
    native_stage(bundle, baseline, installation, "b-brief", monkeypatch)
    native_stage(bundle, baseline, installation, "b-write", monkeypatch)
    assert checker.check_bundle(bundle)["status"] == "mechanical_evidence_passed"
    events = [json.loads(line) for line in (bundle / "b-write.jsonl").read_text().splitlines()]
    ids = sorted(int(row["record_id"][3:]) for event in events
        if event["type"] == "item.completed" and event.get("item", {}).get("tool") == "export_decision_record"
        for row in event["item"]["result"]["structured_content"]["records"])
    # Round one's export access events create real gaps in the next round's
    # substantive record IDs. Complete pagination must still pass.
    assert any(right - left > 1 for left, right in zip(ids, ids[1:]))


@pytest.fixture
def narrow_evidence(prepared, monkeypatch):
    bundle, baseline, installation = prepared
    native_stage(bundle, baseline, installation, "a-brief", monkeypatch)
    native_stage(bundle, baseline, installation, "a-write", monkeypatch, edit_transform=narrow_payment)
    # Retain the positive control with the same actual output and all proof/read
    # evidence, then remove exactly the obligation under test.
    assert checker.check_round(bundle, "a")["mechanical"]["exact_revisions_verified"]
    return bundle, baseline


@pytest.mark.parametrize("fault", ["wrong_target", "omitted_guarantee", "extra_edit", "wrong_result"])
def test_f05_matching_full_output_cannot_authorize_another_batch(narrow_evidence, fault):
    bundle, baseline = narrow_evidence
    source = baseline["inputs"]["a"]["source"]
    rows = checker.inspect_document(source, mode="browse", max_items=100)["paragraphs"]

    def change(events):
        pre = next(e["item"] for e in events if e["type"] == "item.completed"
                   and e.get("item", {}).get("tool") == "preflight_edits")
        edits = deepcopy(pre["arguments"]["edits"])
        if fault == "wrong_target":
            # A fresh, valid reference to the unselected audit-related paragraph.
            payment_edit(edits)["target"]["paragraph_ref"] = rows[9]["paragraph_ref"]
        elif fault == "omitted_guarantee":
            edits[:] = [e for e in edits if e.get("target", {}).get("paragraph_ref", {}).get("paragraph_index") != 6]
        elif fault == "extra_edit":
            edits.append(dict(target=dict(kind="paragraph", paragraph_ref=rows[7]["paragraph_ref"]),
                              delete_text="twelve months", insert_text="twenty-four months"))
        else:
            payment_edit(edits)["insert_text"] = "46"
        # Keep a matching ordered batch and correctly digested entire proof on
        # both sides. The output still has every correct paragraph. Authorization
        # must refuse before those otherwise plausible substitutes are considered.
        proof = deepcopy(pre["result"]["structured_content"]["preflight_proof"])
        proof["edits_sha256"] = checker._digest(edits)
        proof["proof_sha256"] = checker._digest({k: v for k, v in proof.items() if k != "proof_sha256"})
        for event in events:
            item = event.get("item", {})
            if item.get("tool") in {"preflight_edits", "apply_edits"}:
                item["arguments"]["edits"] = deepcopy(edits)
                if item["tool"] == "apply_edits":
                    item["arguments"]["preflight_proof"] = deepcopy(proof)
                elif event["type"] == "item.completed":
                    item["result"]["structured_content"]["preflight_proof"] = deepcopy(proof)

    mutate_events(bundle, "a-write", change)
    assert checker.actual_texts(baseline["inputs"]["a"]["output"]) == texts("counter-a")
    with pytest.raises(checker.EvidenceError, match="authorized"):
        checker.check_round(bundle, "a")


@pytest.mark.parametrize("fault, message", [
    ("changed_apply", "ordered edit payload differs"),
    ("changed_proof", "preflight proof is not exact"),
    ("missing_actual_delete_quote", "input lacks ordered full read and exact verification"),
    ("changed_revision_identity", "exact output revision identity missing"),
])
def test_f05_alternative_boundary_keeps_actual_execution_guarantees(narrow_evidence, fault, message):
    bundle, baseline = narrow_evidence

    def change(events):
        if fault == "missing_actual_delete_quote":
            # Retain all full paragraph reads/quotes and exact output deletion
            # evidence; remove just the input verification of the actual "60".
            events[:] = [event for event in events if not (
                event.get("item", {}).get("tool") == "verify_quote"
                and event["item"]["arguments"].get("path") == baseline["inputs"]["a"]["source"]
                and event["item"]["arguments"].get("quote") == "60")]
            return
        for event in events:
            item = event.get("item", {})
            if item.get("tool") != "apply_edits":
                continue
            if fault == "changed_apply":
                # Both variants have the correct full result, but apply may not
                # silently substitute another boundary after preflight.
                payment_edit(item["arguments"]["edits"]).update(delete_text="60 days", insert_text="45 days")
            elif fault == "changed_proof":
                item["arguments"]["preflight_proof"]["tracked_change_author"] = "Another author"
            elif event["type"] == "item.completed":
                item["result"]["structured_content"]["applied"][0]["tracked_revision_ids"] = ["999"]

    mutate_events(bundle, "a-write", change)
    assert checker.actual_texts(baseline["inputs"]["a"]["output"]) == texts("counter-a")
    with pytest.raises(checker.EvidenceError, match=message):
        checker.check_round(bundle, "a")


def test_f05_old_frozen_oracle_is_not_reinterpreted(prepared):
    bundle, baseline, _ = prepared
    old = deepcopy(baseline)
    old["oracle"]["version"] = "nr03-next-round.v1"
    old["oracle"].pop("edit_authorization")
    old["oracle"].pop("edit_targets")
    for key in ("acceptance_delivery", "export_page_limit", "journal_validation"):
        old["oracle"].pop(key)
    old["oracle"]["edits"] = {r: [list(row) for row in edit_specs(r)] for r in ("a", "b")}
    json_write(bundle / "baseline.json", old)
    frozen_bytes = (bundle / "baseline.json").read_bytes()
    with pytest.raises(checker.EvidenceError, match="frozen pre-MCP semantic oracle differs"):
        checker.load_baseline(bundle)
    assert (bundle / "baseline.json").read_bytes() == frozen_bytes
    assert json.loads(frozen_bytes)["oracle"]["version"] == "nr03-next-round.v1"
