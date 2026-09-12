# SPDX-License-Identifier: Apache-2.0
"""Validate the separate NR-01 native profile against exact local source/output.

Raw baselines and logs are private. This checks evidence consistency, not log
identity/authenticity, visual layout, a release or final legal approval. The old
nine-tool checker and its baseline schema are unchanged.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import zipfile

from lxml import etree
import jsonschema

from veqtor_docx import extract_redlines, inspect_document
from veqtor_docx.inspect import InspectError
from veqtor_docx._ooxml import canonical_body_flow_v1, parse_xml, w
from veqtor_docx._paragraph_edits import paragraph_format_signature, validate_paragraph_candidate
from veqtor_mcp import __version__, records, server
from veqtor_mcp.records import SOURCE_SNAPSHOT_IDENTITY
from veqtor_mcp.contracts import (
    EDIT_INPUT_SCHEMA, PARAGRAPH_REF_SCHEMA, APPLY_EDITS_RESULT_SCHEMA,
    PREFLIGHT_EDITS_RESULT_SCHEMA, INSPECT_DOCUMENT_RESULT_SCHEMA,
    VERIFY_QUOTE_RESULT_SCHEMA, EXTRACT_REDLINES_RESULT_SCHEMA,
    EXPORT_DECISION_RECORD_RESULT_SCHEMA,
)

from check_codex_acceptance import (
    EvidenceError, _digest, _file_sha256, _json, _read, _require, _sha256, native_calls,
)

BASELINE_SCHEMA = "veqtor_codex_paragraph_baseline.v1"
REPORT_SCHEMA = "veqtor_codex_paragraph_acceptance.v1"
PROFILE_TOOLS = frozenset({"inspect_document", "verify_quote", "preflight_edits",
                           "apply_edits", "extract_redlines", "export_decision_record"})


_RESULT_VALIDATORS = {
    tool: jsonschema.Draft202012Validator(schema) for tool, schema in {
        "apply_edits": APPLY_EDITS_RESULT_SCHEMA,
        "preflight_edits": PREFLIGHT_EDITS_RESULT_SCHEMA,
        "inspect_document": INSPECT_DOCUMENT_RESULT_SCHEMA,
        "verify_quote": VERIFY_QUOTE_RESULT_SCHEMA,
        "extract_redlines": EXTRACT_REDLINES_RESULT_SCHEMA,
        "export_decision_record": EXPORT_DECISION_RECORD_RESULT_SCHEMA,
    }.items()
}


def _baseline(value):
    _require(isinstance(value, dict) and set(value) == {
        "schema_version", "server_name", "producer", "source_sha256", "source_path",
        "output_path", "output_absent_before", "expected_edits", "expected_paragraphs",
        "tracked_change_author",
    }, "baseline fields differ from NR-01 schema")
    _require(value["schema_version"] == BASELINE_SCHEMA, "unsupported paragraph profile")
    producer = value["producer"]
    _require(isinstance(producer, dict) and set(producer) == {"name", "version", "build"}
             and producer["name"] == "veqtor-mcp" and isinstance(producer["version"], str)
             and bool(producer["version"]) and isinstance(producer["build"], str)
             and producer["build"].startswith("source-snapshot-v1-sha256:")
             and _sha256(producer["build"].removeprefix("source-snapshot-v1-sha256:")),
             "invalid producer identity")
    _require(producer == {"name": "veqtor-mcp", "version": __version__, "build": SOURCE_SNAPSHOT_IDENTITY},
             "baseline does not name the exact checker runtime source build")
    _require(all(isinstance(value[key], str) and value[key].strip()
                 for key in ("server_name", "tracked_change_author")), "empty baseline identity")
    sources = value["source_sha256"]
    _require(isinstance(sources, dict) and sources and all(
        isinstance(path, str) and Path(path).is_absolute() and _sha256(sha)
        for path, sha in sources.items()), "invalid source inventory")
    _require(value["source_path"] in sources, "selected source missing")
    _require(isinstance(value["output_path"], str) and Path(value["output_path"]).is_absolute()
             and value["output_path"] not in sources and value["output_absent_before"] is True,
             "output was not separately absent before run")
    edits = value["expected_edits"]
    _require(isinstance(edits, list) and 1 <= len(edits) <= 100, "invalid expected edits")
    for edit in edits:
        jsonschema.validate(edit, EDIT_INPUT_SCHEMA)
    _require(any("target" in edit for edit in edits), "profile requires a paragraph target")
    paragraphs = value["expected_paragraphs"]
    _require(isinstance(paragraphs, list) and paragraphs and all(
        isinstance(row, dict) and set(row) == {"paragraph_index", "before", "after"}
        and type(row["paragraph_index"]) is int and row["paragraph_index"] >= 0
        and isinstance(row["before"], str) and isinstance(row["after"], str)
        for row in paragraphs), "expected full paragraph texts are missing")
    _require(len({row["paragraph_index"] for row in paragraphs}) == len(paragraphs),
             "duplicate expected paragraph index")
    return value


def _paragraphs(path):
    with zipfile.ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    root = parse_xml(parts["word/document.xml"])
    paragraphs = canonical_body_flow_v1(root.find(w("body"))).paragraphs
    return parts, root, [para.element for para in paragraphs]


def _text(paragraph):
    # This independent narrow current-text reading is sufficient for the supported
    # direct-run and legacy text-revision scenario; unknown content cannot attest it.
    return "".join(node.text or "" for node in paragraph.iter(w("t"))
                   if not any(parent.tag in {w("del"), w("moveFrom")}
                              for parent in node.iterancestors()))


def _unit(unit):
    return (unit["reference"]["paragraph_index"], unit["change_type"], unit["author"],
            unit["date"], unit["old_text"], unit["new_text"],
            tuple(unit["reference"]["revision_ids"]))


def _collateral(source, output, touched):
    before_parts, before, before_paras = _paragraphs(source)
    after_parts, after, after_paras = _paragraphs(output)
    _require(before_parts.keys() == after_parts.keys() and all(
        before_parts[name] == after_parts[name] for name in before_parts if name != "word/document.xml"),
        "non-document package contents changed")
    _require(len(before_paras) == len(after_paras), "paragraph structure changed")
    before_all, after_all = list(before.iter(w("p"))), list(after.iter(w("p")))
    _require(len(before_all) == len(after_all), "noncanonical paragraph structure changed")
    exempt = {id(before_paras[index]) for index in touched}
    for old, new in zip(before_all, after_all):
        if id(old) not in exempt:
            _require(etree.tostring(old) == etree.tostring(new), "untouched paragraph changed")
    for root in (before, after):
        for paragraph in root.iter(w("p")):
            paragraph.clear(keep_tail=True)
    _require(etree.tostring(before) == etree.tostring(after), "table or document skeleton changed")


def _expected_export_record(call, exported, workspace):
    """Rebuild the compact view from native evidence, never from exported facts.

    Only the journal timestamp is unavailable in the native response. Validate
    its representation without claiming to authenticate its value or the log.
    Projection uses the versioned record contract, including every nested
    collection's digest, count, sample and truncation flag.
    """
    created_at = exported.get("created_at")
    _require(records._compact_created_at(created_at) == created_at and created_at is not None,
             "exported record timestamp is invalid")
    payload = {key: value for key, value in call["payload"].items()
               if key not in {"record_id", "record_status", "record_error"}}
    payload.setdefault("status", "ok")
    args = call["arguments"]
    tool = call["tool"]
    input_payload = {"source_path": args["source_path"], "edits": server._record_edits(args["edits"])}
    provenance = server._preflight_provenance
    if tool == "apply_edits":
        input_payload.update(output_path=args["output_path"], preflight_proof=args["preflight_proof"])
        provenance = server._apply_provenance
    return records._compact_record({
        "schema_version": records.SCHEMA_VERSION,
        "record_type": records._writable_tool_spec(tool).record_type,
        "record_id": call["payload"]["record_id"], "created_at": created_at,
        "tool_name": tool, "workspace": workspace, "producer": call["payload"]["producer"],
        "input": input_payload, "result": payload,
        "result_sha256": _digest(payload), "tool_result_sha256": _digest(payload),
        "provenance": provenance(payload, args["source_path"], args["edits"]),
    })


def validate_evidence(events, baseline):
    baseline = _baseline(baseline)
    thread_id, calls, failures = native_calls(events, baseline["server_name"])
    _require(PROFILE_TOOLS <= {call["tool"] for call in calls}, "native scenario lacks required tools")
    for call in calls:
        payload = call["payload"]
        validator = _RESULT_VALIDATORS.get(call["tool"])
        _require(validator is None or validator.is_valid(payload), "native result violates its public schema")
        _require(payload.get("producer") == baseline["producer"], "native producer identity differs")
        _require(payload.get("status", "ok") == "ok" and payload.get("record_status") == "written"
                 and isinstance(payload.get("record_id"), str), "native call lacks successful provenance")
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
    _, _, source_paras = _paragraphs(source)
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
    exports = matches("export_decision_record", workspace=str(Path(source).parent))
    wanted = {pre_result["record_id"]: pre, result["record_id"]: app}
    _require(len(wanted) == 2, "preflight and apply record identities collide")
    workspace = str(Path(source).resolve().parent)
    valid_export = False
    for call in exports:
        if call["started_at"] <= max(item["completed_at"] for item in calls
                                     if item["tool"] in {"verify_quote", "extract_redlines", "inspect_document"}):
            continue
        rows = call["payload"].get("records", [])
        exported = {row.get("record_id"): row for row in rows}
        if (len(exported) != len(rows) or not wanted.keys() <= exported.keys()
                or call["payload"].get("workspace") != records._path_digest(workspace)):
            continue
        if all(exported[rid] == _expected_export_record(native, exported[rid], workspace)
               for rid, native in wanted.items()):
            valid_export = True
    _require(valid_export, "final export differs from native inputs, results or provenance")
    for path, sha in baseline["source_sha256"].items():
        _require(_file_sha256(path) == sha, "source drifted during evidence verification")
    _require(_file_sha256(output) == candidate, "output drifted during evidence verification")
    return {"schema_version": REPORT_SCHEMA, "status": "passed", "thread_id": thread_id,
            "producer": deepcopy(baseline["producer"]), "source_hashes_unchanged": True,
            "output_sha256": candidate, "preflight_proof_sha256": proof["proof_sha256"],
            "affected_paragraph_count": len(touched), "native_mcp_call_count": len(calls) + len(failures),
            "native_actions_only": True, "exact_revisions_verified": True,
            "full_paragraphs_verified": True, "collateral_verified": True,
            "visual_word_qa_verified": False, "log_authenticity_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        events_bytes, baseline_bytes = _read(args.events), _read(args.baseline)
        report = validate_evidence([_json(line) for line in events_bytes.decode().splitlines() if line.strip()],
                                   _json(baseline_bytes.decode()))
        report["events_sha256"] = hashlib.sha256(events_bytes).hexdigest()
        report["baseline_sha256"] = hashlib.sha256(baseline_bytes).hexdigest()
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, EvidenceError) else "malformed or unreadable local evidence"
        print(f"NR-01 acceptance failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
