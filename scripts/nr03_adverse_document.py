# SPDX-License-Identifier: Apache-2.0
"""Static substantive port of frozen NR-01, only for declared unavailable journal.

Source lineage: check_paragraph_acceptance.py at H3e557e6, SHA-256
ca9df9ca03b88c0c10c7b99e2c531275c01917097964b7f0bcf1b6337bfe5965.
The body from source lines 185–406 and final 425–427 is unchanged; helper
identities remain frozen. Tests compare the AST and exercise causal negatives.
There is no runtime slicing, positive-report exception waiver or fake record.
See docs/NR03_ADVERSE_OBLIGATIONS.md for every obligation and policy seam.
"""
from collections import Counter
from copy import deepcopy
import hashlib

import jsonschema
from veqtor_docx import extract_redlines, inspect_document
from veqtor_docx.inspect import InspectError
from veqtor_docx._paragraph_edits import (
    paragraph_format_signature, validate_paragraph_candidate, validate_paragraph_context,
)
from veqtor_mcp.contracts import PARAGRAPH_REF_SCHEMA
from check_codex_acceptance import EvidenceError, _digest, _file_sha256, _require, _sha256
from check_paragraph_acceptance import (
    PROFILE_TOOLS, _baseline, _RESULT_VALIDATORS, _paragraphs, _text, _unit, _collateral,
)
from nr03_creation_probe import classify_failures, tool_error
from nr03_delivery import EXPORT_PAGE_LIMIT


def require_disabled(payload):
    _require(payload.get("status", "ok") == "ok" and payload.get("record_status") == "disabled"
             and "record_id" in payload and payload["record_id"] is None and "record_error" not in payload,
             "adverse result lacks disabled/null/no-error provenance")


def validate_unavailable_exports(calls, failures, workspace, *, require_final=True):
    exports = [c for c in calls + failures if c["tool"] == "export_decision_record"]
    last = max(c["completed_at"] for c in calls
               if c["tool"] in {"inspect_document", "verify_quote", "extract_redlines"})
    _require(exports and (not require_final or any(c["started_at"] > last for c in exports)),
             "actual expected export failure after document verification is missing")
    for call in exports:
        args = call["arguments"]
        _require(set(args) <= {"workspace", "max_records", "before_record_id"}
                 and args.get("workspace") == workspace and args.get("before_record_id") is None
                 and type(args.get("max_records")) is int and 1 <= args["max_records"] <= EXPORT_PAGE_LIMIT,
                 "adverse export workspace or bounded first-page arguments differ")
        _require(call["failed"] and tool_error(call, "workspace_uninitialized"),
                 "adverse export must preserve the actual expected failure")
    return dict(status="EXPECTED_UNAVAILABLE", positive_journal_pass=False,
                original_export_ids=[c["id"] for c in exports], log_authenticity_verified=False)


def validate_adverse_document(events, baseline, *, creation_state):
    """Document component only; the public observation wrapper binds case/launch."""
    from check_next_round_journal import require_frozen_dependencies
    from pathlib import Path
    require_frozen_dependencies()
    baseline = _baseline(baseline)
    thread_id, calls, failures, ledger, creation_probe = classify_failures(
        events, baseline, creation_state, unavailable_workspace=str(Path(baseline["source_path"]).parent))
    _require(PROFILE_TOOLS - {"export_decision_record"} <= {c["tool"] for c in calls},
             "native scenario lacks required document tools")
    for call in calls:
        payload = call["payload"]
        validator = _RESULT_VALIDATORS.get(call["tool"])
        _require(validator is None or validator.is_valid(payload), "native result violates its public schema")
        _require(payload.get("producer") == baseline["producer"], "native producer identity differs")
        require_disabled(payload)
    source, output = baseline["source_path"], baseline["output_path"]
    source_hash = baseline["source_sha256"][source]
    for path, sha in baseline["source_sha256"].items():
        _require(_file_sha256(path) == sha, "original source bytes changed")

    def matches(tool, **args):
        return [call for call in calls if call["tool"] == tool and all(
            call["arguments"].get(key) == value for key, value in args.items())]

    pres, apps = matches("preflight_edits"), matches("apply_edits")
    _require(len(pres) == len(apps) == 1, "scenario needs exactly one preflight and apply")
    pre, app = pres[0], apps[0]
    _require(pre["completed_at"] < app["started_at"], "apply preceded completed preflight")
    edits = baseline["expected_edits"]
    _require(pre["arguments"].get("source_path") == source
             and app["arguments"].get("source_path") == source
             and app["arguments"].get("output_path") == output
             and pre["arguments"].get("edits") == app["arguments"].get("edits") == edits,
             "source, output or ordered edit payload differs")
    pre_result, result = pre["payload"], app["payload"]
    candidate = pre_result.get("candidate_sha256")
    _require(_sha256(candidate) and _file_sha256(output) == candidate,
             "output bytes differ from preflight candidate")
    proof_content = {
        "schema_version": "preflight_proof.v1", "source_sha256": source_hash,
        "edits_sha256": _digest(edits), "tracked_change_author": baseline["tracked_change_author"],
        "producer_build": baseline["producer"]["build"], "candidate_sha256": candidate,
    }
    proof = {**proof_content, "proof_sha256": _digest(proof_content)}
    _require(pre_result.get("batch_applicable") is True
             and pre_result.get("preflight_proof") == app["arguments"].get("preflight_proof") == proof,
             "preflight proof is not exact and applicable")
    for payload in (pre_result, result):
        _require(payload.get("source_sha256") == source_hash
                 and payload.get("tracked_change_author") == baseline["tracked_change_author"],
                 "source or author result binding differs")
        check = payload.get("round_trip_check", {})
        _require(check.get("status") == "passed" and check.get("collateral_changes") == []
                 and check.get("comparison") == "ooxml_semantic_diff_outside_touched_anchors",
                 "round-trip collateral evidence missing")
    _require(result.get("output_path") == output and result.get("output_sha256") == candidate
             and result.get("preflight_candidate_sha256") == candidate
             and result.get("candidate_output_sha256_match") is True
             and result.get("preflight_binding_status") == "verified", "publication binding missing")

    # Independently reread exact local bytes. This substantiates the logged native
    # extraction/read evidence; it cannot replace any required native call below.
    source_units = extract_redlines(source)["change_units"]
    output_extraction = extract_redlines(output)
    output_units = output_extraction["change_units"]
    native_extracts = [call for call in matches("extract_redlines", path=output)
                       if app["completed_at"] < call["started_at"]
                       and call["payload"].get("file_sha256") == candidate
                       and call["payload"].get("change_units") == output_units
                       and call["payload"].get("revision_count") == output_extraction["revision_count"]]
    _require(native_extracts, "complete exact output revisions lack native extraction")
    _, source_document, source_paras = _paragraphs(source)
    _, _, output_paras = _paragraphs(output)
    expected = {row["paragraph_index"]: row for row in baseline["expected_paragraphs"]}
    applied = result.get("applied")
    diagnostics = pre_result.get("edits")
    _require(isinstance(applied, list) and len(applied) == len(edits)
             and isinstance(diagnostics, list) and len(diagnostics) == len(edits),
             "edit result or preflight diagnostics incomplete")
    new_units, touched = [], set()

    def read_before(path, ref, text, lower, upper):
        for read in matches("inspect_document", path=path, mode="read"):
            payload = read["payload"]
            if (lower < read["started_at"] and read["completed_at"] < upper
                    and payload.get("mode") == "read" and payload.get("reading_mode") == "accepted_current_v1"
                    and payload.get("path") == path and payload.get("part_name") == ref["part_name"]
                    and payload.get("container_policy") == ref["container_policy"]
                    and payload.get("file_sha256") == ref["file_sha256"]
                    and read["arguments"].get("selection") == {"paragraph_ref": ref}
                    and payload.get("selection_kind") == "paragraph"
                    and len(payload.get("paragraphs", [])) == 1
                    and payload["paragraphs"][0].get("paragraph_ref") == ref
                    and payload["paragraphs"][0].get("text") == text
                    and hashlib.sha256(text.encode()).hexdigest() == ref["paragraph_text_sha256"]):
                return True
        return False

    def verified(path, ref, quote, lower, upper, *, current=False, unit=None, side=None):
        def bound_matches(payload):
            matches = payload.get("matches", [])
            return bool(matches) and all(
                match.get("path") == path
                and match.get("part_name") == (ref["part_name"] if current else unit["reference"]["part_name"])
                and match.get("revision_ids") == ([] if current else unit["reference"]["revision_ids"])
                and match.get("side") == ("paragraph_current" if current else side)
                for match in matches)

        return [call for call in matches("verify_quote", path=path, anchor=ref, quote=quote)
                if lower < call["started_at"] and call["completed_at"] < upper
                and call["payload"].get("checked_anchor") == ref
                and call["payload"].get("verdict") == "exact"
                and call["payload"].get("exact") is True
                and bound_matches(call["payload"])
                and (not current or (
                    call["arguments"].get("paragraph_projection") == "accepted_current_v1"
                    and call["payload"].get("checked_projection", {}).get("mode") == "accepted_current_v1"
                    and call["payload"].get("checked_projection") == {
                        "schema_version": "verified_paragraph_projection.v1", "mode": "accepted_current_v1",
                        "projection_status": "complete", "anchor_reading_mode": "accepted_current_v1",
                        "anchor_paragraph_text_sha256": ref["paragraph_text_sha256"],
                        "projection_text_sha256": ref["paragraph_text_sha256"],
                        "text_length": len(expected[ref["paragraph_index"]]["before" if path == source else "after"]),
                    }
                    and all(match.get("side") == "paragraph_current" and match.get("path") == path
                            and match.get("paragraph_index") == ref["paragraph_index"]
                            and match.get("paragraph_text_sha256") == ref["paragraph_text_sha256"]
                            and match.get("projection_text_sha256") == ref["paragraph_text_sha256"]
                            and match.get("projection_mode") == "accepted_current_v1"
                            for match in call["payload"].get("matches", []))))]

    for number, (edit, item, diagnostic) in enumerate(zip(edits, applied, diagnostics)):
        if "target" in edit:
            target = edit["target"]
            ref = target["paragraph_ref"]
            index = ref["paragraph_index"]
            _require(item.get("target") == target and "change_unit_id" not in item
                     and diagnostic.get("target") == target and diagnostic.get("change_unit_id") is None,
                     "paragraph identity is missing or disguised as a source unit")
            operation = "replace" if edit.get("insert_text") else "delete"
            input_anchor = ref
        else:
            input_anchor = edit["anchor"]
            source_unit = next((unit for unit in source_units if unit["anchor"] == input_anchor), None)
            _require(source_unit is not None, "legacy anchor differs from exact source")
            _require(any(call["completed_at"] < pre["started_at"]
                         and call["payload"].get("change_units") == source_units
                         and call["payload"].get("file_sha256") == source_hash
                         for call in matches("extract_redlines", path=source)), "legacy native source extraction missing")
            index = source_unit["reference"]["paragraph_index"]
            source_read = inspect_document(source, mode="browse", max_items=100)
            ref = next(row["paragraph_ref"] for row in source_read["paragraphs"]
                       if row["paragraph_ref"]["paragraph_index"] == index)
            operation = "reinstate" if "reinstate_text" in edit else item.get("operation")
            _require(item.get("change_unit_id") == input_anchor["change_unit_id"]
                     and operation in {"replace", "delete", "counter", "reinstate"}, "legacy identity mismatch")
        _require(ref["file_sha256"] == source_hash and index in expected, "target expected paragraph missing")
        touched.add(index)
        row = expected[index]
        _require(index < len(source_paras) and index < len(output_paras)
                 and _text(source_paras[index]) == row["before"] and _text(output_paras[index]) == row["after"],
                 "local full expected paragraph differs")
        input_quote = edit.get("delete_text", edit.get("reinstate_text"))
        _require(any(read_before(source, ref, row["before"], -1, call["started_at"])
                     for call in verified(source, input_anchor, input_quote, -1, pre["started_at"],
                                          current="target" in edit,
                                          unit=None if "target" in edit else source_unit,
                                          side=None if "target" in edit else (
                                              "new" if source_unit["new_text"] and input_quote in source_unit["new_text"]
                                              else "old"))), "input lacks ordered full read and exact verification")
        _require(diagnostic.get("edit_index") == number and diagnostic.get("status") == "applicable"
                 and diagnostic.get("operation") == operation and diagnostic.get("match_count") == 1
                 and diagnostic.get("position_status") == "supported" and diagnostic.get("refusal_code") is None,
                 "preflight edit facts differ")
        _require(item.get("operation") == operation and item.get("deleted_text") == edit.get("delete_text")
                 and item.get("inserted_text") == (edit.get("insert_text", edit.get("reinstate_text")) or None),
                 "reported edit wording differs")
        units = [unit for unit in output_units if unit["reference"]["revision_ids"] == item.get("tracked_revision_ids")]
        _require(len(units) == 1, "exact output revision identity missing")
        unit = units[0]
        _require(_unit(unit)[:6] == (index, "insert" if operation == "reinstate" else operation,
                 baseline["tracked_change_author"], None, edit.get("delete_text"),
                 edit.get("insert_text", edit.get("reinstate_text")) or None), "exact output revision facts differ")
        new_units.append(_unit(unit))
        output_ref = {**ref, "file_sha256": candidate,
                      "paragraph_text_sha256": hashlib.sha256(row["after"].encode()).hexdigest()}
        jsonschema.validate(output_ref, PARAGRAPH_REF_SCHEMA)
        if row["after"]:
            _require(any(read_before(output, output_ref, row["after"], app["completed_at"], call["started_at"])
                         for call in verified(output, output_ref, row["after"], app["completed_at"], float("inf"), current=True)),
                     "full expected output lacks ordered native read and verification")
        # For delete-only, exact deletion is independently checked even for an
        # empty resulting paragraph (which cannot be a nonempty quote).
        if edit.get("delete_text"):
            _require(any(read_before(output, output_ref, row["after"], app["completed_at"], call["started_at"])
                         and any(extract["completed_at"] < call["started_at"] for extract in native_extracts)
                         and any(match.get("side") == "old" and match.get("path") == output
                                 and match.get("revision_ids") == unit["reference"]["revision_ids"]
                                 for match in call["payload"].get("matches", []))
                         for call in verified(output, unit["anchor"], edit["delete_text"], app["completed_at"], float("inf"),
                                              unit=unit, side="old")),
                     "exact deletion lacks ordered full read, extraction and native verification")
    _require(touched == set(expected), "expected paragraphs differ from affected targets")
    _require(Counter(map(_unit, output_units)) == Counter(map(_unit, source_units)) + Counter(new_units),
             "output revisions differ from exactly baseline plus intended changes")
    for index in touched:
        paragraph_edits = [edit for edit in edits if "target" in edit
                           and edit["target"]["paragraph_ref"]["paragraph_index"] == index]
        if not paragraph_edits:
            continue
        try:
            validate_paragraph_context(source_document, source_paras[index])
            validate_paragraph_candidate(source_paras[index], output_paras[index])
        except InspectError as exc:
            raise EvidenceError("unaccounted paragraph structure in actual output") from exc
        original_signature = paragraph_format_signature(source_paras[index])
        _require(original_signature == paragraph_format_signature(output_paras[index], reject_new=True),
                 "original paragraph text or formatting changed")
        attrs, props, original_tokens = original_signature
        tokens = list(original_tokens)
        original_text = expected[index]["before"]
        spans = []
        for edit in paragraph_edits:
            fragment = edit["delete_text"]
            start = original_text.find(fragment)
            _require(start >= 0 and original_text.find(fragment, start + 1) < 0,
                     "paragraph edit is not uniquely matched in original text")
            spans.append((start, start + len(fragment), edit.get("insert_text", "")))
        ordered = sorted(spans)
        _require(all(left[1] <= right[0] and left[0] != right[0]
                     for left, right in zip(ordered, ordered[1:])), "paragraph edit spans overlap")
        for start, end, replacement in reversed(ordered):
            style = tokens[start][1:]
            tokens[start:end] = [(char, *style) for char in replacement]
        _require((attrs, props, tuple(tokens)) == paragraph_format_signature(output_paras[index], accept_new=True),
                 "current paragraph text or replacement formatting differs")
    _collateral(source, output, touched)
    journal = validate_unavailable_exports(calls, failures, str(Path(source).parent))
    for path, sha in baseline["source_sha256"].items():
        _require(_file_sha256(path) == sha, "source drifted during evidence verification")
    _require(_file_sha256(output) == candidate, "output drifted during evidence verification")
    return {"schema_version": "nr03-adverse-document.v3", "status": "passed", "journal": journal,
            "failure_ledger": ledger, "creation_probe": creation_probe, "thread_id": thread_id,
            "producer": deepcopy(baseline["producer"]), "source_hashes_unchanged": True,
            "output_sha256": candidate, "preflight_proof_sha256": proof["proof_sha256"],
            "affected_paragraph_count": len(touched), "native_mcp_call_count": len(calls) + len(failures),
            "native_actions_only": True, "exact_revisions_verified": True,
            "full_paragraphs_verified": True, "collateral_verified": True,
            "visual_word_qa_verified": False, "log_authenticity_verified": False}
