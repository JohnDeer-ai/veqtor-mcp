# SPDX-License-Identifier: Apache-2.0
"""F09 fabricated-envelope controls; never new-candidate native acceptance."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

import test_next_round_acceptance as nr03
from test_next_round_acceptance import checker, json_write, native_stage
import check_next_round_journal as journal
from nr03_scenario import edit_specs, texts

prepared = nr03.prepared


def failed_pair(path, *, ident="creation-probe", tool="inspect_document", mode="outline"):
    args = dict(path=path, mode=mode)
    identity = dict(id=ident, type="mcp_tool_call", server=nr03.SERVER, tool=tool, arguments=args)
    return [dict(type="item.started", item=dict(deepcopy(identity), status="in_progress", result=None, error=None)),
            dict(type="item.completed", item=dict(deepcopy(identity), status="failed", error=None,
                 result=dict(structured_content=None, content=[dict(type="text", text=
                     f"Error executing tool {tool}: file_unreadable: operation refused")])))]


def save_events(bundle, events):
    # Keep the original failed result, including null structured payload. Only
    # successful fabricated payload pairs and their enclosing checksum are bound.
    for event in events:
        result = event.get("item", {}).get("result")
        if result and isinstance(result.get("structured_content"), dict):
            result["content"] = [dict(type="text", text=json.dumps(result["structured_content"]))]
    path = bundle / "a-write.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    receipt = nr03.prep.read_json(bundle / "a-write.receipt.json")
    receipt["events_sha256"] = checker._file_sha256(str(path))
    json_write(bundle / "a-write.receipt.json", receipt)
    nr03.synthetic_delivery(bundle, "a-write")


@pytest.fixture
def probe(prepared, monkeypatch):
    bundle, b, installation = prepared
    native_stage(bundle, b, installation, "a-brief", monkeypatch)
    events = native_stage(bundle, b, installation, "a-write", monkeypatch, export_page_size=1)
    assert checker.check_round(bundle, "a")["mechanical"]["exact_revisions_verified"]
    # Exact actual failure pattern: outline of selected absent output before
    # preflight/create, followed only by successful browse/full read, no outline.
    events[4:4] = failed_pair(b["inputs"]["a"]["output"])
    save_events(bundle, events)
    return bundle, b, installation, events


def legacy_input(bundle, b, installation):
    stage = checker.load_stage(bundle, "a-write", b, installation)
    pre = next(c for c in stage["calls"] if c["tool"] == "preflight_edits")
    baseline = dict(schema_version=journal.paragraphs.BASELINE_SCHEMA, server_name=nr03.SERVER,
        producer=installation["producer"], source_sha256=b["initial_state"]["docx"],
        source_path=b["inputs"]["a"]["source"], output_path=b["inputs"]["a"]["output"],
        output_absent_before=True, expected_edits=pre["arguments"]["edits"], tracked_change_author=b["author"],
        expected_paragraphs=[dict(paragraph_index=i, before=texts("incoming-a")[i], after=texts("counter-a")[i])
                             for i, *_ in edit_specs("a")])
    return stage, baseline


def test_f09_original_probe_pattern_runs_all_document_and_page_checks(probe):
    bundle, b, installation, events = probe
    before = (bundle / "a-write.jsonl").read_bytes()
    original_function = journal.paragraphs.validate_evidence
    original_parser = journal.paragraphs.native_calls
    report = checker.check_round(bundle, "a")["mechanical"]
    assert report["exact_revisions_verified"] and report["full_paragraphs_verified"]
    assert report["collateral_verified"] and report["journal"]["complete_pages_verified"]
    assert report["journal"]["wanted_pages"]["preflight_edits"] != report["journal"]["wanted_pages"]["apply_edits"]
    assert report["creation_probe"]["id"] == "creation-probe"
    assert report["creation_probe"]["output_sha256"] == report["output_sha256"]
    assert report["creation_probe"]["original_failure"]["result"]["structured_content"] is None
    assert (bundle / "a-write.jsonl").read_bytes() == before
    assert journal.paragraphs.validate_evidence is original_function
    assert journal.paragraphs.native_calls is original_parser
    journal.require_frozen_dependencies()
    _, baseline = legacy_input(bundle, b, installation)
    with pytest.raises(checker.EvidenceError, match="failed read call.*successful retry"):
        original_function(checker.document_only(events), baseline)


def test_f09_profile_itself_requires_both_independent_inventories(probe):
    bundle, b, installation, _ = probe
    stage, baseline = legacy_input(bundle, b, installation)
    state = dict(initial=deepcopy(b["initial_state"]), before=deepcopy(stage["receipt"]["before"]))
    wrong = [None, {}, dict(initial=state["initial"]), dict(before=state["before"])]
    for fault in ("existing_in_both", "before_only", "source_hash", "missing_docx", "missing_store"):
        bad = deepcopy(state)
        if fault == "existing_in_both":
            for value in bad.values():
                value["docx"][baseline["output_path"]] = checker._file_sha256(baseline["output_path"])
        elif fault == "before_only":
            bad["before"]["docx"][baseline["output_path"]] = checker._file_sha256(baseline["output_path"])
        elif fault == "source_hash":
            bad["before"]["docx"][baseline["source_path"]] = "0" * 64
        else:
            del bad["initial"]["docx" if fault == "missing_docx" else "store_sha256"]
        wrong.append(bad)
    for bad in wrong:
        with pytest.raises(checker.EvidenceError, match="creation probe.*(initial|before-state)"):
            journal.validate_document_and_journal(checker.document_only(stage["events"]), baseline,
                stage["calls"], stage["thread"], creation_state=bad)
    assert journal.validate_document_and_journal(checker.document_only(stage["events"]), baseline,
        stage["calls"], stage["thread"], creation_state=state)["creation_probe"]


def test_f09_ordinary_matching_mode_retry_still_uses_frozen_nr01(probe):
    bundle, b, _, events = probe
    events = deepcopy(events)
    for event in events:
        if event.get("item", {}).get("id") == "creation-probe":
            event["item"]["arguments"]["mode"] = "read"
    save_events(bundle, events)
    assert checker.check_round(bundle, "a")["mechanical"]["creation_probe"] is None
    # An ordinary source read failure recovered with the same mode can coexist
    # with the distinct permitted output outline; scope/mode policy is unchanged.
    events[4:4] = failed_pair(b["inputs"]["a"]["source"], ident="source-read-retry", mode="read")
    for event in events:
        if event.get("item", {}).get("id") == "creation-probe":
            event["item"]["arguments"]["mode"] = "outline"
    save_events(bundle, events)
    assert checker.check_round(bundle, "a")["mechanical"]["creation_probe"]["id"] == "creation-probe"


FAULTS = ["source_probe", "previous_probe", "other_output_probe", "other_probe_mode", "invalid_probe_argument",
          "failure_after_apply", "unrelated_failed_read",
          "failed_preflight", "failed_apply", "no_preflight", "no_apply", "repeated_apply", "repeated_preflight",
          "wrong_candidate", "wrong_output_hash", "wrong_apply_output", "wrong_proof", "wrong_edits",
          "no_post_read", "browse_not_full_read", "missing_one_full_read", "no_output_quote",
          "missing_one_quote", "no_deletion_quote", "no_revision_extract", "partial_revision_extract",
          "no_source_read", "no_source_quote", "no_export_page", "wrong_journal_provenance",
          "preexisting_destination", "missing_before", "inconsistent_before", "missing_initial",
          "wrong_error", "transport_error", "malformed_pair", "non_mcp", "completed_error_read",
          "actual_output_bytes", "missing_output_file"]


def test_f09_required_facts_cannot_be_replaced_by_weaker_evidence(probe, record_property):
    bundle, b, _, _ = probe
    assert checker.check_round(bundle, "a")["mechanical"]["creation_probe"]
    paths = [bundle / "a-write.jsonl", bundle / "a-write.receipt.json", bundle / "baseline.json",
             Path(b["inputs"]["a"]["output"])]
    originals = {path: path.read_bytes() for path in paths}
    refused = []
    for fault in FAULTS:
        try:
            refuse_mutant(probe, fault)
            refused.append(fault)
        finally:
            for path, data in originals.items():
                path.write_bytes(data)
    assert checker.check_round(bundle, "a")["mechanical"]["creation_probe"]
    record_property("refused_f09_mutants", json.dumps(refused))


def refuse_mutant(probe, fault):
    bundle, b, _, original = probe
    events = deepcopy(original)
    source, previous, output = (b["inputs"]["a"][k] for k in ("source", "previous", "output"))

    def items(tool, path=None):
        return [e["item"] for e in events if e.get("item", {}).get("tool") == tool
                and (path is None or e["item"]["arguments"].get("path") == path)]

    def remove_pair(item):
        events[:] = [e for e in events if e.get("item", {}).get("id") != item["id"]]

    probe_items = [e["item"] for e in events if e.get("item", {}).get("id") == "creation-probe"]
    if fault in {"source_probe", "previous_probe", "other_output_probe", "other_probe_mode", "invalid_probe_argument"}:
        for item in probe_items:
            if fault == "other_probe_mode":
                item["arguments"]["mode"] = "comments"
            elif fault == "invalid_probe_argument":
                item["arguments"]["max_items"] = True
            else:
                item["arguments"]["path"] = dict(source_probe=source, previous_probe=previous,
                                                  other_output_probe=output + ".other.docx")[fault]
    elif fault in {"failure_after_apply", "failure_after_preflight"}:
        pair = [e for e in events if e.get("item", {}).get("id") == "creation-probe"]
        remove_pair(pair[0]["item"])
        tool = "apply_edits" if fault == "failure_after_apply" else "preflight_edits"
        index = next(i for i, e in enumerate(events) if e["type"] == "item.completed" and e.get("item", {}).get("tool") == tool)
        events[index + 1:index + 1] = pair
    elif fault in {"second_probe", "unrelated_failed_read", "failed_preflight", "failed_apply"}:
        tool = {"failed_preflight": "preflight_edits", "failed_apply": "apply_edits"}.get(fault, "inspect_document")
        events[6:6] = failed_pair(source if fault == "unrelated_failed_read" else output, ident="extra-failure", tool=tool)
    elif fault in {"no_preflight", "no_apply"}:
        tool = "preflight_edits" if fault == "no_preflight" else "apply_edits"
        remove_pair(items(tool)[0])
    elif fault in {"repeated_apply", "repeated_preflight"}:
        tool = "apply_edits" if fault == "repeated_apply" else "preflight_edits"
        pair = deepcopy([e for e in events if e.get("item", {}).get("tool") == tool])
        for event in pair:
            event["item"]["id"] += "-duplicate"
        events[-2:-2] = pair
    elif fault in {"wrong_candidate", "wrong_output_hash", "wrong_apply_output", "wrong_proof", "wrong_edits"}:
        for item in items("preflight_edits") + items("apply_edits"):
            result = item.get("result")
            if result and fault == "wrong_candidate" and item["tool"] == "preflight_edits":
                result["structured_content"]["candidate_sha256"] = "0" * 64
            if result and fault == "wrong_output_hash" and item["tool"] == "apply_edits":
                result["structured_content"]["output_sha256"] = "0" * 64
            if item["tool"] == "apply_edits":
                if fault == "wrong_apply_output":
                    item["arguments"]["output_path"] = output + ".other.docx"
                elif fault == "wrong_proof":
                    item["arguments"]["preflight_proof"]["source_sha256"] = "0" * 64
                elif fault == "wrong_edits":
                    item["arguments"]["edits"].reverse()
    elif fault in {"no_post_read", "browse_not_full_read", "missing_one_full_read", "no_source_read"}:
        reads = [i for i in items("inspect_document", source if fault == "no_source_read" else output)
                 if i["arguments"].get("mode") == "read"]
        if fault == "browse_not_full_read":
            for item in reads:
                item["arguments"]["mode"] = "browse"  # Keep plausible full-looking result.
        else:
            for item in reads[:1] if fault == "missing_one_full_read" else reads:
                remove_pair(item)
    elif fault in {"no_output_quote", "missing_one_quote", "no_deletion_quote", "no_source_quote"}:
        quotes = items("verify_quote", source if fault == "no_source_quote" else output)
        if fault == "no_deletion_quote":
            quotes = [i for i in quotes if "change_unit_id" in i["arguments"].get("anchor", {})]
        if fault == "missing_one_quote":
            quotes = [i for i in quotes if "paragraph_index" in i["arguments"].get("anchor", {})][:1]
        for item in quotes:
            remove_pair(item)
    elif fault in {"no_revision_extract", "partial_revision_extract"}:
        for item in items("extract_redlines", output):
            if fault == "no_revision_extract":
                remove_pair(item)
            elif item.get("result"):
                item["result"]["structured_content"]["change_units"].pop()
    elif fault in {"no_export_page", "wrong_journal_provenance"}:
        exports = items("export_decision_record")
        if fault == "no_export_page":
            remove_pair(exports[len(exports) // 2])
        else:
            row = next(row for i in exports if i.get("result")
                       for row in i["result"]["structured_content"]["records"] if row["tool_name"] == "apply_edits")
            row["input"]["sha256"] = "0" * 64
    elif fault in {"wrong_error", "transport_error", "malformed_pair"}:
        item = probe_items[-1]
        if fault == "wrong_error":
            item["result"]["content"][0]["text"] = "arbitrary failure"
        elif fault == "transport_error":
            item["error"] = "transport disconnected"
        else:
            item["arguments"]["mode"] = "read"
    elif fault == "non_mcp":
        events.insert(-2, dict(type="item.completed", item=dict(type="command_execution", command="read file")))
    elif fault == "completed_error_read":
        pair = failed_pair(source, ident="completed-error", mode="read")
        pair[1]["item"]["status"] = "completed"
        pair[1]["item"]["result"]["isError"] = True
        events[6:6] = pair
    save_events(bundle, events)
    if fault in {"preexisting_destination", "missing_before", "inconsistent_before"}:
        path = bundle / "a-write.receipt.json"
        receipt = nr03.prep.read_json(path)
        if fault == "missing_before":
            del receipt["before"]
        elif fault == "preexisting_destination":
            receipt["before"]["docx"][output] = checker._file_sha256(output)
        else:
            receipt["before"]["docx"][source] = "0" * 64
        json_write(path, receipt)
    if fault == "missing_initial":
        path = bundle / "baseline.json"
        baseline = nr03.prep.read_json(path)
        del baseline["initial_state"]
        json_write(path, baseline)
    if fault == "actual_output_bytes":
        path = Path(output)
        path.write_bytes(path.read_bytes() + b"\n")  # Valid same-text ZIP, different bytes.
        receipt = nr03.prep.read_json(bundle / "a-write.receipt.json")
        receipt["after"]["docx"][output] = checker._file_sha256(output)
        json_write(bundle / "a-write.receipt.json", receipt)
    if fault == "missing_output_file":
        Path(output).unlink()
    # The CLI also refuses unreadable local evidence; its early actual-text read
    # raises FileNotFoundError before document validation for this one control.
    refusal = FileNotFoundError if fault == "missing_output_file" else checker.EvidenceError
    with pytest.raises(refusal):
        checker.check_round(bundle, "a")
