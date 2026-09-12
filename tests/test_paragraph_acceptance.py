# SPDX-License-Identifier: Apache-2.0
"""Synthetic envelopes test the NR-01 checker; these are not native acceptance."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import pytest

from veqtor_docx.synthetic import generate_demo_rounds
from veqtor_mcp import server

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import check_paragraph_acceptance as checker  # noqa: E402


def build_evidence(tmp_path, *, mixed=False, empty=False):
    paths = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")
    source, output = paths[int(mixed)], tmp_path / "output.docx"
    events = [{"type": "thread.started", "thread_id": "synthetic-checker-test"}, {"type": "turn.started"}]

    def add(tool, **args):
        payload = getattr(server, tool)(**args)
        identity = {"id": str(len(events)), "type": "mcp_tool_call", "server": "veqtor",
                    "tool": tool, "arguments": args}
        events.append({"type": "item.started", "item": {**deepcopy(identity), "status": "in_progress",
                       "result": None, "error": None}})
        events.append({"type": "item.completed", "item": {**deepcopy(identity), "status": "completed",
                       "result": {"structured_content": deepcopy(payload),
                                  "content": [{"type": "text", "text": json.dumps(payload)}]}, "error": None}})
        return payload

    read = add("inspect_document", path=str(source), mode="browse", max_items=100)
    original = read["paragraphs"]
    edits = []
    expected = []
    for index, deleted, inserted in [(0, "30 days", "45 days"),
                                     (1, original[1]["text"] if empty else "optional ", ""),
                                     (3, "30 days", "60 days")]:
        ref = original[index]["paragraph_ref"]
        add("inspect_document", path=str(source), mode="read", selection={"paragraph_ref": ref})
        add("verify_quote", path=str(source), anchor=ref, quote=deleted, paragraph_projection="accepted_current_v1")
        edits.append({"target": {"kind": "paragraph", "paragraph_ref": ref},
                      "delete_text": deleted, "insert_text": inserted})
        expected.append({"paragraph_index": index, "before": original[index]["text"],
                         "after": original[index]["text"].replace(deleted, inserted)})
    if mixed:
        units = add("extract_redlines", path=str(source))["change_units"]
        for unit in units:
            index = unit["reference"]["paragraph_index"]
            ref = original[index]["paragraph_ref"]
            add("inspect_document", path=str(source), mode="read", selection={"paragraph_ref": ref})
            quote = unit["new_text"] or unit["old_text"]
            add("verify_quote", path=str(source), anchor=unit["anchor"], quote=quote)
            if unit["new_text"]:
                edits.append({"anchor": unit["anchor"], "delete_text": quote, "insert_text": "75 units"})
                after = original[index]["text"].replace(quote, "75 units")
            else:
                edits.append({"anchor": unit["anchor"], "reinstate_text": quote})
                after = "Audit: inspection right remains."
            expected.append({"paragraph_index": index, "before": original[index]["text"], "after": after})
    baseline = {"schema_version": checker.BASELINE_SCHEMA, "server_name": "veqtor", "producer": read["producer"],
                "source_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
                "source_path": str(source), "output_path": str(output), "output_absent_before": True,
                "expected_edits": deepcopy(edits), "expected_paragraphs": expected,
                "tracked_change_author": server._tracked_change_author()}
    pre = add("preflight_edits", source_path=str(source), edits=edits)
    assert pre["batch_applicable"], pre
    app = add("apply_edits", source_path=str(source), output_path=str(output), edits=edits,
              preflight_proof=pre["preflight_proof"])
    units = add("extract_redlines", path=str(output))["change_units"]
    add("inspect_document", path=str(output), mode="browse", max_items=100)
    for intended, item in zip(edits, app["applied"]):
        unit = next(unit for unit in units if unit["reference"]["revision_ids"] == item["tracked_revision_ids"])
        index = unit["reference"]["paragraph_index"]
        expected_text = next(row["after"] for row in expected if row["paragraph_index"] == index)
        ref = {**original[index]["paragraph_ref"], "file_sha256": app["output_sha256"],
               "paragraph_text_sha256": hashlib.sha256(expected_text.encode()).hexdigest()}
        add("inspect_document", path=str(output), mode="read", selection={"paragraph_ref": ref})
        if expected_text:
            add("verify_quote", path=str(output), anchor=ref, quote=expected_text,
                paragraph_projection="accepted_current_v1")
        if "delete_text" in intended:
            add("verify_quote", path=str(output), anchor=unit["anchor"], quote=intended["delete_text"])
    add("export_decision_record", workspace=str(source.parent), max_records=100)
    events.append({"type": "turn.completed"})
    return events, baseline


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.delenv("VEQTOR_DISABLE_DECISION_RECORD", raising=False)
    return build_evidence(tmp_path)


def completed(events, tool, **arguments):
    return [event["item"] for event in events if event["type"] == "item.completed"
            and event["item"].get("tool") == tool and all(
                event["item"]["arguments"].get(key) == value for key, value in arguments.items())]


def sync(events, item):
    item["result"]["content"][0]["text"] = json.dumps(item["result"]["structured_content"])
    start = next(event["item"] for event in events if event["type"] == "item.started"
                 and event["item"]["id"] == item["id"])
    start["arguments"] = deepcopy(item["arguments"])


@pytest.mark.parametrize("scenario", ["clean", "mixed", "empty", "mixed_empty"])
def test_positive_real_payloads_but_synthetic_native_envelopes(tmp_path, monkeypatch, scenario):
    monkeypatch.delenv("VEQTOR_DISABLE_DECISION_RECORD", raising=False)
    events, baseline = build_evidence(tmp_path, mixed=scenario in {"mixed", "mixed_empty"}, empty=scenario in {"empty", "mixed_empty"})
    report = checker.validate_evidence(events, baseline)
    assert report["status"] == "passed"
    assert report["full_paragraphs_verified"] and report["exact_revisions_verified"]
    assert report["visual_word_qa_verified"] is False
    assert not {"map_rounds", "trace_paragraph_history"} & {event.get("item", {}).get("tool") for event in events}


@pytest.mark.parametrize("tool", sorted(checker.PROFILE_TOOLS))
def test_missing_required_native_tool_fails(evidence, tool):
    events, baseline = evidence
    events[:] = [event for event in events if event.get("item", {}).get("tool") != tool]
    with pytest.raises(checker.EvidenceError):
        checker.validate_evidence(events, baseline)


@pytest.mark.parametrize("mutation", ["missing", "snippet", "wrong_text", "wrong_ref", "wrong_hash",
                                       "before_apply", "after_verify", "wrong_selection", "browse"])
def test_full_output_read_cannot_be_substituted(evidence, mutation):
    events, baseline = evidence
    item = completed(events, "inspect_document", path=baseline["output_path"], mode="read")[0]
    payload = item["result"]["structured_content"]
    if mutation in {"missing", "before_apply", "after_verify"}:
        pair = [event for event in events if event.get("item", {}).get("id") == item["id"]]
        events[:] = [event for event in events if event not in pair]
        if mutation != "missing":
            target = completed(events, "apply_edits")[0] if mutation == "before_apply" else completed(
                events, "verify_quote", path=baseline["output_path"])[0]
            position = next(i for i, event in enumerate(events) if event.get("item", {}).get("id") == target["id"])
            if mutation == "after_verify":
                position += 2
            events[position:position] = pair
    elif mutation in {"snippet", "browse"}:
        mode = "literal_search" if mutation == "snippet" else "browse"
        item["arguments"]["mode"] = payload["mode"] = mode
    elif mutation == "wrong_text":
        payload["paragraphs"][0]["text"] = "45 days"
    elif mutation == "wrong_ref":
        payload["paragraphs"][0]["paragraph_ref"]["paragraph_index"] = 2
    elif mutation == "wrong_hash":
        payload["file_sha256"] = "f" * 64
    else:
        item["arguments"]["selection"] = {"paragraph_ref": baseline["expected_edits"][1]["target"]["paragraph_ref"]}
    sync(events, item) if mutation != "missing" else None
    with pytest.raises(checker.EvidenceError):
        checker.validate_evidence(events, baseline)


@pytest.mark.parametrize("mutation", ["source_read", "source_verify", "full_verify", "deletion_verify",
                                       "revision_missing", "revision_wrong", "diagnostic", "identity",
                                       "export_identity", "proof", "build", "expected_after", "existing_output"])
def test_other_missing_or_weaker_evidence_fails(evidence, mutation):
    events, baseline = evidence
    output = baseline["output_path"]
    if mutation in {"source_read", "source_verify", "full_verify", "deletion_verify"}:
        if mutation == "source_read":
            item = completed(events, "inspect_document", path=baseline["source_path"], mode="read")[0]
        elif mutation == "source_verify":
            item = completed(events, "verify_quote", path=baseline["source_path"])[0]
        else:
            items = completed(events, "verify_quote", path=output)
            item = next(item for item in items if ("change_unit_id" in item["arguments"]["anchor"]) == (mutation == "deletion_verify"))
        events[:] = [event for event in events if event.get("item", {}).get("id") != item["id"]]
    elif mutation in {"expected_after", "existing_output"}:
        if mutation == "expected_after":
            baseline["expected_paragraphs"][1]["after"] = "wrong but removed quote absent"
        else:
            baseline["output_absent_before"] = False
    else:
        tool = "extract_redlines" if mutation.startswith("revision_") else (
            "export_decision_record" if mutation == "export_identity" else (
                "preflight_edits" if mutation in {"diagnostic", "proof"} else "apply_edits"))
        item = completed(events, tool, **({"path": output} if tool == "extract_redlines" else {}))[0]
        payload = item["result"]["structured_content"]
        if mutation == "revision_missing":
            payload["change_units"].pop()
        elif mutation == "revision_wrong":
            payload["change_units"][0]["old_text"] = "other"
        elif mutation == "diagnostic":
            payload["edits"][0]["match_count"] = 2
        elif mutation == "identity":
            payload["applied"][0]["change_unit_id"] = "cu_001"
        elif mutation == "export_identity":
            record = next(row for row in payload["records"] if row["tool_name"] == "apply_edits")
            record["result"]["applied"]["sample"][0].pop("target")
        elif mutation == "proof":
            payload["preflight_proof"]["edits_sha256"] = "f" * 64
        else:
            payload["producer"]["build"] = "source-snapshot-v1-sha256:" + "f" * 64
        sync(events, item)
    with pytest.raises(checker.EvidenceError):
        checker.validate_evidence(events, baseline)


def test_consistent_historical_producer_cannot_establish_new_acceptance(evidence):
    events, baseline = evidence
    baseline["producer"]["version"] = "0.4.0"
    for event in events:
        if event["type"] == "item.completed":
            item = event["item"]
            item["result"]["structured_content"]["producer"]["version"] = "0.4.0"
            sync(events, item)
    with pytest.raises(checker.EvidenceError, match="exact checker runtime"):
        checker.validate_evidence(events, baseline)


@pytest.mark.parametrize("fault", ["neighbour", "original_format", "insert_format"])
def test_rebound_logs_still_require_actual_collateral_preservation(evidence, fault):
    # Keep all hash/proof fields consistent while changing actual output OOXML.
    # The independent byte/paragraph check must reject weaker logged assurances.
    from lxml import etree
    from veqtor_docx._ooxml import w
    from test_paragraph_edits import rewrite

    events, baseline = evidence
    output = Path(baseline["output_path"])
    old_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    def mutate(root):
        if fault == "neighbour":
            list(root.iter(w("p")))[2].set("changed", "yes")
        elif fault == "original_format":
            etree.SubElement(root.find(".//" + w("pPr")), w("jc")).set(w("val"), "right")
        else:
            run = root.find(".//" + w("ins") + "/" + w("r"))
            etree.SubElement(etree.SubElement(run, w("rPr")), w("b"))
    rewrite(output, mutate)
    new_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    def rebind(value, old, new):
        if isinstance(value, dict):
            return {key: rebind(item, old, new) for key, item in value.items()}
        if isinstance(value, list):
            return [rebind(item, old, new) for item in value]
        return new if value == old else value
    events[:] = rebind(events, old_sha, new_sha)
    actual_extraction = checker.extract_redlines(str(output))
    old_units = completed(events, "extract_redlines")[0]["result"]["structured_content"]["change_units"]
    for old, new in zip(old_units, actual_extraction["change_units"]):
        events[:] = rebind(events, old["anchor"]["unit_fingerprint_sha256"], new["anchor"]["unit_fingerprint_sha256"])
    completed(events, "extract_redlines")[0]["result"]["structured_content"].update(actual_extraction)
    pre = completed(events, "preflight_edits")[0]["result"]["structured_content"]
    proof = pre["preflight_proof"]
    new_proof_sha = checker._digest({key: value for key, value in proof.items() if key != "proof_sha256"})
    events[:] = rebind(events, proof["proof_sha256"], new_proof_sha)
    for event in events:
        if event["type"] == "item.completed":
            sync(events, event["item"])
    with pytest.raises(checker.EvidenceError, match="(untouched paragraph|formatting|paragraph structure)"):
        checker.validate_evidence(events, baseline)


@pytest.mark.parametrize("which,fault", [
    (which, fault)
    for which in ("preflight_edits", "apply_edits")
    for fault in (
        "result_fields", "source_hash", "output_or_candidate_hash", "author", "round_trip",
        "provenance", "producer", "workspace", "input_digest", "result_digest", "tool_result_digest",
        "revision_count", "revision_digest", "revision_truncation", "collection_digest", "combined",
        "provenance_source", "provenance_author", "provenance_round_trip", "proof_binding",
    )
    if not (which == "preflight_edits" and (fault.startswith("revision_") or fault == "proof_binding"))
])
def test_export_binds_all_native_record_facts(evidence, which, fault):
    events, baseline = evidence
    item = completed(events, "export_decision_record")[0]
    row = next(row for row in item["result"]["structured_content"]["records"] if row["tool_name"] == which)
    collection = "edits" if which == "preflight_edits" else "applied"
    result = row["result"]
    if fault == "result_fields":
        row["result"] = {collection: result[collection]}
    elif fault == "source_hash":
        result["source_sha256"] = "f" * 64
    elif fault == "output_or_candidate_hash":
        result["candidate_sha256" if which == "preflight_edits" else "output_sha256"] = "f" * 64
    elif fault == "author":
        result["tracked_change_author"] = "Unrelated Author"
    elif fault == "round_trip":
        result["round_trip_check"].update(status="failed", collateral_change_count=1)
    elif fault == "provenance":
        row["provenance"] = {}
    elif fault == "provenance_source":
        row["provenance"]["source_sha256"] = "f" * 64
    elif fault == "provenance_author":
        row["provenance"]["tracked_change_author"] = "Unrelated Author"
    elif fault == "provenance_round_trip":
        row["provenance"]["round_trip_check"].update(status="failed", collateral_change_count=1)
    elif fault == "proof_binding":
        row["provenance"]["preflight_binding_status"] = "mismatch"
    elif fault == "producer":
        row["producer"] = {"name": "veqtor-mcp", "version": "0.4.0",
                           "build": "source-snapshot-v1-sha256:" + "f" * 64}
    elif fault == "workspace":
        row["workspace"]["sha256"] = "f" * 64
    elif fault == "input_digest":
        row["input"]["sha256"] = "f" * 64
    elif fault in {"result_digest", "tool_result_digest"}:
        row[fault.replace("digest", "sha256")] = "f" * 64
    elif fault.startswith("revision_"):
        key = "target_revision_ids" if which == "preflight_edits" else "tracked_revision_ids"
        revision = result[collection]["sample"][0][key]
        revision[{"revision_count": "count", "revision_digest": "sha256",
                  "revision_truncation": "truncated"}[fault]] = {
                      "revision_count": 999, "revision_digest": "f" * 64, "revision_truncation": True}[fault]
    elif fault == "collection_digest":
        result[collection]["sha256"] = "f" * 64
    else:
        row["result"] = {collection: result[collection]}
        row["provenance"] = {}
        row["producer"] = {"name": "veqtor-mcp", "version": "0.4.0",
                           "build": "source-snapshot-v1-sha256:" + "f" * 64}
        row["workspace"]["sha256"] = row["input"]["sha256"] = "f" * 64
        row["result_sha256"] = row["tool_result_sha256"] = "f" * 64
        if which == "apply_edits":
            result[collection]["sample"][0]["tracked_revision_ids"].update(
                count=999, sha256="f" * 64, truncated=True)
    sync(events, item)
    with pytest.raises(checker.EvidenceError, match="final export differs"):
        checker.validate_evidence(events, baseline)


@pytest.mark.parametrize("direction", ["source", "output"])
@pytest.mark.parametrize("field,value", [("path", "/tmp/unrelated.docx"),
                                         ("part_name", "word/header1.xml")])
def test_returned_read_identity_is_bound(evidence, direction, field, value):
    events, baseline = evidence
    item = completed(events, "inspect_document", path=baseline[direction + "_path"], mode="read")[0]
    item["result"]["structured_content"][field] = value
    sync(events, item)
    with pytest.raises(checker.EvidenceError):
        checker.validate_evidence(events, baseline)


@pytest.mark.parametrize("direction", ["source", "output"])
@pytest.mark.parametrize("field,value", [("path", "/tmp/unrelated.docx"), ("part_name", "word/header1.xml"),
                                         ("revision_ids", ["9999"]), ("side", "paragraph_current"),
                                         ("combined", None)])
def test_returned_unit_verification_identity_is_bound(tmp_path, monkeypatch, direction, field, value):
    monkeypatch.delenv("VEQTOR_DISABLE_DECISION_RECORD", raising=False)
    events, baseline = build_evidence(tmp_path, mixed=True, empty=True)
    item = next(item for item in completed(events, "verify_quote", path=baseline[direction + "_path"])
                if "change_unit_id" in item["arguments"]["anchor"])
    match = item["result"]["structured_content"]["matches"][0]
    if field == "combined":
        match.update(path="/tmp/unrelated.docx", part_name="word/header1.xml", revision_ids=["9999"])
    else:
        match[field] = value
    sync(events, item)
    with pytest.raises(checker.EvidenceError):
        checker.validate_evidence(events, baseline)


def test_combined_wrong_reads_matches_and_export_cannot_attest_mixed_scenario(tmp_path, monkeypatch):
    monkeypatch.delenv("VEQTOR_DISABLE_DECISION_RECORD", raising=False)
    events, baseline = build_evidence(tmp_path, mixed=True, empty=True)
    assert checker.validate_evidence(events, baseline)["status"] == "passed"
    for direction in ("source", "output"):
        for item in completed(events, "inspect_document", path=baseline[direction + "_path"], mode="read"):
            item["result"]["structured_content"]["path"] = "/tmp/unrelated.docx"
        for item in completed(events, "verify_quote", path=baseline[direction + "_path"]):
            if "change_unit_id" in item["arguments"]["anchor"]:
                item["result"]["structured_content"]["matches"][0].update(
                    path="/tmp/unrelated.docx", part_name="word/header1.xml", revision_ids=["9999"])
    for row in completed(events, "export_decision_record")[0]["result"]["structured_content"]["records"]:
        if row["tool_name"] in {"preflight_edits", "apply_edits"}:
            row["result"] = {key: value for key, value in row["result"].items() if key in {"edits", "applied"}}
            row["provenance"] = {}
            row["producer"] = {"name": "veqtor-mcp", "version": "0.4.0",
                               "build": "source-snapshot-v1-sha256:" + "f" * 64}
            row["workspace"]["sha256"] = row["input"]["sha256"] = "f" * 64
            row["result_sha256"] = row["tool_result_sha256"] = "f" * 64
    for event in events:
        if event["type"] == "item.completed":
            sync(events, event["item"])
    with pytest.raises(checker.EvidenceError):
        checker.validate_evidence(events, baseline)


@pytest.mark.parametrize("fault", ["duplicate_ppr", "drawing", "bookmarks", "combined", "empty_run", "wrapper_attribute",
                                   "body_tail", "table_tail", "combined_tails", "legacy_tail", "whitespace_tail",
                                   "tail_bookmarks"])
def test_independent_checker_rejects_actual_structural_collateral(tmp_path, monkeypatch, fault):
    from test_paragraph_edits import inject_paragraph_structure, rewrite

    monkeypatch.delenv("VEQTOR_DISABLE_DECISION_RECORD", raising=False)
    events, baseline = build_evidence(tmp_path, mixed=True, empty=True)
    assert checker.validate_evidence(events, baseline)["status"] == "passed"
    output = Path(baseline["output_path"])
    old_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    rewrite(output, lambda root: inject_paragraph_structure(root, fault))
    new_sha = hashlib.sha256(output.read_bytes()).hexdigest()

    def rebind(value, old, new):
        if isinstance(value, dict):
            return {key: rebind(item, old, new) for key, item in value.items()}
        if isinstance(value, list):
            return [rebind(item, old, new) for item in value]
        return new if value == old else value

    events[:] = rebind(events, old_sha, new_sha)
    actual = checker.extract_redlines(str(output))
    extraction = completed(events, "extract_redlines", path=str(output))[0]["result"]["structured_content"]
    for old, new in zip(extraction["change_units"], actual["change_units"]):
        events[:] = rebind(events, old["anchor"]["unit_fingerprint_sha256"], new["anchor"]["unit_fingerprint_sha256"])
    completed(events, "extract_redlines", path=str(output))[0]["result"]["structured_content"].update(actual)
    proof = completed(events, "preflight_edits")[0]["result"]["structured_content"]["preflight_proof"]
    events[:] = rebind(events, proof["proof_sha256"], checker._digest({
        key: value for key, value in proof.items() if key != "proof_sha256"}))
    for event in events:
        if event["type"] == "item.completed":
            sync(events, event["item"])
    # Rebuild every export binding too. Only actual paragraph structure should
    # distinguish these rebound envelopes from the passing control above.
    exports = completed(events, "export_decision_record")[0]
    for index, row in enumerate(exports["result"]["structured_content"]["records"]):
        if row["tool_name"] in {"preflight_edits", "apply_edits"}:
            native = completed(events, row["tool_name"])[0]
            call = {"tool": row["tool_name"], "arguments": native["arguments"],
                    "payload": native["result"]["structured_content"]}
            exports["result"]["structured_content"]["records"][index] = checker._expected_export_record(
                call, row, str(Path(baseline["source_path"]).parent))
    sync(events, exports)
    message = "table or document skeleton changed" if fault == "legacy_tail" else "unaccounted paragraph structure"
    with pytest.raises(checker.EvidenceError, match=message):
        checker.validate_evidence(events, baseline)
