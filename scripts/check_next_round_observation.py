# SPDX-License-Identifier: Apache-2.0
"""NR-03 independent write assessment with predeclared positive/adverse dispatch.

No native launch, mutation, historical relabeling or aggregate acceptance. Human
business decisions, truthful limitation reporting and visual QA remain gates.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import check_next_round_acceptance as check
import capture_next_round_observation as observation
from check_codex_acceptance import _digest, _file_sha256, _require
from check_next_round_journal import validate_document_and_journal
from nr03_adverse_document import require_disabled, validate_adverse_document, validate_unavailable_exports
from nr03_creation_probe import classify_failures
from nr03_coverage import brief_coverage
from nr03_model_delivery import load_model_delivery
from nr03_scenario import DOCUMENT_POLICIES, INJECTION, SERVER, document, edit_specs, texts
from prepare_next_round_acceptance import read_json, state


def check_independent_write(bundle, step_id):
    bundle = Path(bundle).absolute()
    b, installation = check.load_baseline(bundle)
    _require(b["variant"] in DOCUMENT_POLICIES and b["variant"] != "main", "independent write scope differs")
    folder = bundle / "observations"
    frozen = read_json(folder / "plan.json")
    plan = observation.validate_plan(frozen["plan"])
    policy = plan["expected"].get("document_policy")
    _require(policy == DOCUMENT_POLICIES[b["variant"]], "missing or different predeclared document policy")
    _require(frozen["schema_version"] == "veqtor_next_round_observations.v1"
             and frozen["baseline_sha256"] == _file_sha256(str(bundle / "baseline.json"))
             and frozen["installation_sha256"] == b["installation_sha256"]
             and frozen["workflow_sha256"] == b["workflow_sha256"]
             and frozen["client_selection"] == b["client_selection"]
             and frozen["initial_state"] == b["initial_state"], "observation policy baseline/config binding differs")
    observation.resume_parent(folder, plan, frozen, step_id, b, installation)
    steps = {s["id"]: s for s in plan["steps"]}
    _require(step_id in steps and steps[step_id]["resume"] is not None, "write must follow its own decision/brief chain")
    chain, current = [], step_id
    while current is not None:
        chain.append(current)
        current = steps[current]["resume"]
    initial = b["initial_state"]
    source, previous, output = (b["inputs"]["a"][key] for key in ("source", "previous", "output"))
    expected_sources = {source: hashlib.sha256(document("incoming-a", variant=b["variant"])).hexdigest(),
                        previous: hashlib.sha256(document("previous")).hexdigest()}
    _require(initial["docx"] == expected_sources, "independent initial source oracle differs")
    parsed, coverage, deliveries, ancestor_failures = {}, None, {}, {}
    last = frozen["prepared_ns"]
    for ident in reversed(chain):
        receipt = read_json(folder / f"{ident}.receipt.json")
        _require(type(receipt["started_ns"]) is int and type(receipt["finished_ns"]) is int
                 and last < receipt["started_ns"] < receipt["finished_ns"], "observation phase order differs")
        last = receipt["finished_ns"]
        from nr03_app_server import parse_capture
        parsed_source = parse_capture(folder, ident, receipt, installation["producer"], require_tool_calls=ident in {step_id, chain[-1]})
        events, calls, messages = (parsed_source[key] for key in ("events", "calls", "messages"))
        check.scope(calls, b, "a")
        item = dict(receipt=receipt, **parsed_source)
        deliveries[ident] = load_model_delivery(folder, ident, item)
        parsed[ident] = item
        _require(receipt["before"] == initial, "original complete before-state differs")
        if ident != step_id:
            _require(receipt["after"] == initial and not any(c["tool"] in
                     {"preflight_edits", "apply_edits", "mutate_deal_positions"} for c in calls),
                     "decision ancestor mutated or preflighted before complete authorization")
            _require(not any(c["failed"] and c["tool"] == "read_deal_positions" for c in calls),
                     "decision ancestor contains a position failure")
            if any(c["tool"] != "read_deal_positions" for c in calls):
                _, successes, failures, ledger, _ = classify_failures(check.document_only(events),
                    dict(producer=installation["producer"], server_name=SERVER, output_path=output), None,
                    unavailable_workspace=b["matter"] if policy == "expected_unavailable_journal" else None)
                ancestor_failures[ident] = ledger
                if policy == "expected_unavailable_journal" and any(c["tool"] == "export_decision_record" for c in calls):
                    validate_unavailable_exports(successes, failures, b["matter"], require_final=False)
        if ident == chain[-1]:
            first = min(calls, key=lambda c: c["started_at"])
            check.full_position_read(first, b)
            _require(all(first["completed_at"] < c["started_at"] for c in calls
                         if c["tool"] != "read_deal_positions"), "first position recovery did not precede document work")
            before = texts("incoming-a") + ([INJECTION] if b["variant"] == "document-injection" else [])
            coverage = brief_coverage(calls, [(source, before), (previous, texts("previous"))],
                upper=messages[-1]["event_index"], delivered_ids=deliveries[ident]["calls"])
    write = parsed[step_id]
    receipt, calls = write["receipt"], write["calls"]
    final = dict(docx={**initial["docx"], output: _file_sha256(output)}, store_sha256=initial["store_sha256"])
    _require(receipt["after"] == state(b["matter"]) == final, "write changed source/store or unexpected output inventory")
    pres = [c for c in calls if c["tool"] == "preflight_edits"]
    _require(len(pres) == 1 and not pres[0]["failed"], "one full successful preflight required")
    reads = [c for c in calls if c["tool"] == "read_deal_positions"]
    _require(reads and any(c["completed_at"] < pres[0]["started_at"] for c in reads), "fresh positions before preflight missing")
    for call in reads:
        check.full_position_read(call, b)
    before = texts("incoming-a") + ([INJECTION] if b["variant"] == "document-injection" else [])
    after = texts("counter-a") + ([INJECTION] if b["variant"] == "document-injection" else [])
    _require(check.actual_texts(source) == before and check.actual_texts(previous) == texts("previous")
             and check.actual_texts(output) == after, "actual complete independent texts differ")
    edits = pres[0]["arguments"].get("edits")
    check.authorize_edits(source, "a", edits)
    baseline = dict(schema_version=check.paragraphs.BASELINE_SCHEMA, server_name=SERVER, producer=installation["producer"],
        source_sha256=initial["docx"], source_path=source, output_path=output, output_absent_before=True,
        expected_edits=edits, tracked_change_author=b["author"], expected_paragraphs=[
            dict(paragraph_index=i, before=before[i], after=after[i]) for i, *_ in edit_specs("a")])
    creation_state = dict(initial=initial, before=receipt["before"])
    if policy == "expected_unavailable_journal":
        for item in parsed.values():
            for call in item["calls"]:
                if not call["failed"]:
                    require_disabled(call["payload"])
        component = validate_adverse_document(check.document_only(write["events"]), baseline, creation_state=creation_state)
    else:
        component = validate_document_and_journal(check.document_only(write["events"]), baseline, calls,
                                                write["thread"], creation_state=creation_state)
    _require(state(b["matter"]) == final, "source/output/store drift during component validation")
    return dict(schema_version="nr03-independent-write.v3", status="PASSED_COMPONENT_ONLY", policy=policy,
        component=component, brief_coverage=coverage, model_delivery=deliveries, ancestor_failure_ledger=ancestor_failures,
        plan_sha256=_file_sha256(str(folder / "plan.json")), receipt_sha256=_file_sha256(str(folder / f"{step_id}.receipt.json")),
        message_sha256=_digest(write["messages"]), native_acceptance_claimed=False,
        open_gates=["business/parent decisions and truthful final journal limitation", "injection handling where applicable",
                    "journal client presentation", "all pages and visible markup", "new candidate independent acceptance"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--step", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(check_independent_write(args.bundle, args.step), indent=2))
    except (check.EvidenceError, OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        message = str(exc) if isinstance(exc, check.EvidenceError) else "malformed or unreadable evidence"
        print("NR-03 observation refused: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
