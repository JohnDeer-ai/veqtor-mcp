# SPDX-License-Identifier: Apache-2.0
"""F07 page/adapter adversaries in fabricated envelopes, never native acceptance."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

import test_next_round_acceptance as nr03
from test_next_round_acceptance import checker, json_write, mutate_events, native_stage, texts
import check_next_round_journal as journal
from nr03_scenario import edit_specs

prepared = nr03.prepared


@pytest.fixture
def paged(prepared, monkeypatch):
    bundle, baseline, installation = prepared
    native_stage(bundle, baseline, installation, "a-brief", monkeypatch)
    native_stage(bundle, baseline, installation, "a-write", monkeypatch, export_page_size=1)
    result = checker.check_round(bundle, "a")
    pages = result["mechanical"]["journal"]
    assert pages["complete_pages_verified"] and pages["page_count"] > 2
    assert pages["wanted_pages"]["preflight_edits"] != pages["wanted_pages"]["apply_edits"]
    assert result["mechanical"]["exact_revisions_verified"] and result["mechanical"]["collateral_verified"]
    return bundle, baseline, installation


def test_f07_complete_bound_pages_and_adversarial_controls(paged, record_property):
    bundle, baseline, installation = paged
    raw_path, receipt_path = bundle / "a-write.jsonl", bundle / "a-write.receipt.json"
    original_raw, original_receipt = raw_path.read_bytes(), receipt_path.read_bytes()
    faults = ["missing_page", "missing_record", "duplicate_page", "duplicate_record", "swapped_pages",
              "requested_cursor", "returned_cursor", "total_count", "returned_count", "truncated", "end_cursor",
              "page_limit", "argument_workspace", "result_workspace", "record_workspace", "producer",
              "other_session", "other_receipt", "receipt_hash", "alter_preflight", "alter_apply",
              "missing_text", "missing_structure"]
    rejected = []
    for fault in faults:
        raw_path.write_bytes(original_raw)
        receipt_path.write_bytes(original_receipt)
        events = [json.loads(line) for line in original_raw.splitlines()]
        exports = [e["item"] for e in events if e["type"] == "item.completed"
                   and e.get("item", {}).get("tool") == "export_decision_record"]
        selected, first, last = exports[1], exports[0], exports[-1]
        payload = selected["result"]["structured_content"]
        ident = selected["id"]
        pair_indices = [i for i, e in enumerate(events) if e.get("item", {}).get("id") == ident]
        if fault == "missing_page":
            events[:] = [e for e in events if e.get("item", {}).get("id") != ident]
        elif fault == "missing_record":
            payload["records"] = []
            payload["returned_count"] = 0  # Still plausible schema/count types.
        elif fault == "duplicate_page":
            pair = deepcopy([events[i] for i in pair_indices])
            for event in pair:
                event["item"]["id"] += "-duplicate"
            events[pair_indices[-1] + 1:pair_indices[-1] + 1] = pair
        elif fault == "duplicate_record":
            payload["records"] = deepcopy(first["result"]["structured_content"]["records"])
        elif fault == "swapped_pages":
            first_indices = [i for i, e in enumerate(events) if e.get("item", {}).get("id") == first["id"]]
            for left, right in zip(first_indices, pair_indices):
                events[left], events[right] = events[right], events[left]
        elif fault in {"requested_cursor", "page_limit", "argument_workspace"}:
            for event in events:
                item = event.get("item", {})
                if item.get("id") == ident:
                    key, value = {"requested_cursor": ("before_record_id", "dr_999999"),
                                  "page_limit": ("max_records", 21),
                                  "argument_workspace": ("workspace", str(bundle / "another-matter"))}[fault]
                    item["arguments"][key] = value
        elif fault == "returned_cursor":
            payload["next_before_record_id"] = "dr_999999"
        elif fault in {"total_count", "returned_count"}:
            payload[fault] += 1
        elif fault == "truncated":
            payload["truncated"] = False
        elif fault == "end_cursor":
            last["result"]["structured_content"]["next_before_record_id"] = "dr_001"
        elif fault == "result_workspace":
            payload["workspace"]["sha256"] = "0" * 64
        elif fault == "record_workspace":
            payload["records"][0]["workspace"]["sha256"] = "0" * 64
        elif fault == "producer":
            payload["producer"]["build"] = "source-snapshot-v1-sha256:" + "0" * 64
        elif fault == "other_session":
            events.insert(pair_indices[0], dict(type="thread.started", thread_id="another-native-conversation"))
        elif fault in {"alter_preflight", "alter_apply"}:
            tool = "preflight_edits" if fault == "alter_preflight" else "apply_edits"
            row = next(row for export in exports for row in export["result"]["structured_content"]["records"]
                       if row["tool_name"] == tool)
            if fault == "alter_preflight":
                row["input"]["sha256"] = "0" * 64
            else:
                row["provenance"]["source_sha256"] = "0" * 64
        elif fault == "missing_text":
            selected["result"]["content"] = []
        elif fault == "missing_structure":
            selected["result"]["structured_content"] = None  # Retain its original complete text.
        for event in events:
            item = event.get("item", {})
            result = item.get("result")
            if event["type"] == "item.completed" and result and isinstance(result.get("structured_content"), dict):
                if not (fault == "missing_text" and item["id"] == ident):
                    result["content"] = [dict(type="text", text=json.dumps(result["structured_content"]))]
        raw_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        receipt = json.loads(original_receipt)
        receipt["events_sha256"] = checker._file_sha256(str(raw_path))
        if fault == "other_receipt":
            receipt["resumed_thread_id"] = "another-native-conversation"
        if fault == "receipt_hash":
            receipt["events_sha256"] = "0" * 64
        json_write(receipt_path, receipt)
        try:
            # The actual integration binds the entire raw turn, receipt, producer,
            # conversation, prompt and workflow before collecting any pages.
            stage = checker.load_stage(bundle, "a-write", baseline, installation)
            journal.validate_export_pages(stage["calls"], baseline["matter"], installation["producer"])
        except checker.EvidenceError:
            rejected.append(fault)
        else:
            pytest.fail(f"F07 accepted mutant {fault}")
    raw_path.write_bytes(original_raw)
    receipt_path.write_bytes(original_receipt)
    assert rejected == faults
    assert checker.check_round(bundle, "a")["mechanical"]["journal"]["complete_pages_verified"]
    record_property("refused_mutants", json.dumps(rejected))


@pytest.mark.parametrize("name", list(journal.FROZEN_DEPENDENCIES))
def test_f07_unknown_document_gate_dependency_refuses(monkeypatch, name):
    journal.require_frozen_dependencies()
    original = journal._file_sha256
    monkeypatch.setattr(journal, "_file_sha256", lambda path: "0" * 64 if Path(path).name == name else original(path))
    with pytest.raises(checker.EvidenceError, match="frozen document-gate dependency changed"):
        journal.require_frozen_dependencies()


def test_f07_selected_workspace_argument_keeps_canonical_journal_identity(paged, tmp_path):
    bundle, baseline, installation = paged
    stage = checker.load_stage(bundle, "a-write", baseline, installation)
    alias = tmp_path / "selected-matter-alias"
    alias.symlink_to(baseline["matter"], target_is_directory=True)
    calls = deepcopy(stage["calls"])
    for call in calls:
        if call["tool"] == "export_decision_record":
            call["arguments"]["workspace"] = str(alias)
    # Preserve the exact selected path in native arguments; the API legitimately
    # hashes the canonical journal workspace, as the unchanged NR-01 gate does.
    assert journal.validate_export_pages(calls, str(alias), installation["producer"])["complete_pages_verified"]


def test_f07_original_nr01_refusal_and_earlier_document_failures_stay_strict(paged):
    bundle, b, installation = paged
    stage = checker.load_stage(bundle, "a-write", b, installation)
    pre = next(c for c in stage["calls"] if c["tool"] == "preflight_edits")
    legacy = dict(schema_version=journal.paragraphs.BASELINE_SCHEMA, server_name=nr03.SERVER,
        producer=installation["producer"], source_sha256=b["initial_state"]["docx"],
        source_path=b["inputs"]["a"]["source"], output_path=b["inputs"]["a"]["output"], output_absent_before=True,
        expected_edits=pre["arguments"]["edits"], tracked_change_author=b["author"],
        expected_paragraphs=[dict(paragraph_index=i, before=texts("incoming-a")[i], after=texts("counter-a")[i])
                             for i, *_ in edit_specs("a")])
    # The unchanged NR-01 profile still refuses split pages; only NR-03 owns the
    # new policy. Its original raw input is neither merged nor rewritten here.
    with pytest.raises(checker.EvidenceError, match=journal.SAME_PAGE_REFUSAL):
        journal.paragraphs.validate_evidence(checker.document_only(stage["events"]), legacy)
    raw_path, receipt_path = bundle / "a-write.jsonl", bundle / "a-write.receipt.json"
    original_raw, original_receipt = raw_path.read_bytes(), receipt_path.read_bytes()
    for fault, message in [("no_output_read", "full expected output lacks"),
                           ("changed_apply", "ordered edit payload differs"),
                           ("changed_proof", "preflight proof is not exact")]:
        raw_path.write_bytes(original_raw)
        receipt_path.write_bytes(original_receipt)

        def change(events):
            if fault == "no_output_read":
                events[:] = [e for e in events if not (e.get("item", {}).get("tool") == "inspect_document"
                    and e["item"]["arguments"].get("path") == b["inputs"]["a"]["output"]
                    and e["item"]["arguments"].get("mode") == "read")]
                return
            for event in events:
                item = event.get("item", {})
                if item.get("tool") == "apply_edits":
                    if fault == "changed_apply":
                        item["arguments"]["edits"].reverse()
                    else:
                        item["arguments"]["preflight_proof"]["source_sha256"] = "0" * 64

        mutate_events(bundle, "a-write", change)
        with pytest.raises(checker.EvidenceError, match=message):
            checker.check_round(bundle, "a")
    raw_path.write_bytes(original_raw)
    receipt_path.write_bytes(original_receipt)
    nr03.synthetic_delivery(bundle, "a-write")
    assert checker.check_round(bundle, "a")["mechanical"]["journal"]["complete_pages_verified"]


@pytest.mark.parametrize("target", ["source", "output", "store"])
def test_f07_final_source_output_and_store_binding_after_page_validation(paged, monkeypatch, target):
    bundle, baseline, _ = paged
    path = (Path(baseline["matter"]) / ".veqtor/deal-positions.json" if target == "store"
            else Path(baseline["inputs"]["a"][target]))
    original_bytes = path.read_bytes()
    original_validator = journal.validate_export_pages

    def drift(*args):
        report = original_validator(*args)
        path.write_bytes(original_bytes + b"\n")
        return report

    monkeypatch.setattr(journal, "validate_export_pages", drift)
    try:
        with pytest.raises(checker.EvidenceError, match="drifted during evidence verification"):
            checker.check_round(bundle, "a")
    finally:
        path.write_bytes(original_bytes)
