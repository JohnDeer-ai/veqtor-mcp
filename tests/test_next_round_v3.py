# SPDX-License-Identifier: Apache-2.0
"""Causal v3 controls on explicitly SYNTHETIC envelopes; never native acceptance."""
import ast
from copy import deepcopy
from itertools import product
import json
from pathlib import Path

import pytest

import test_next_round_acceptance as base
from test_next_round_acceptance import checker, native_stage
from test_next_round_creation_probe import failed_pair, legacy_input
from nr03_creation_probe import initial_navigation
from nr03_adverse_document import validate_adverse_document
import nr03_adverse_document as adverse
from check_next_round_journal import validate_document_and_journal, require_frozen_dependencies

prepared = base.prepared


@pytest.fixture
def component(prepared, monkeypatch):
    bundle, b, installation = prepared
    native_stage(bundle, b, installation, "a-brief", monkeypatch)
    events = native_stage(bundle, b, installation, "a-write", monkeypatch)
    stage, baseline = legacy_input(bundle, b, installation)
    state = dict(initial=deepcopy(b["initial_state"]), before=deepcopy(stage["receipt"]["before"]))
    return events, baseline, state


def normalized(events):
    value = deepcopy(events)
    for e in value:
        item = e.get("item", {})
        result = item.get("result")
        if isinstance(result, dict) and isinstance(result.get("structured_content"), dict):
            result["content"] = [dict(type="text", text=json.dumps(result["structured_content"]))]
    return value


def run(component, events=None, *, disabled=False, baseline=None):
    original, source, state = component
    events = normalized(original if events is None else events)
    baseline = source if baseline is None else baseline
    thread, calls, _ = checker.parse_native(events, baseline["producer"])
    if disabled:
        return validate_adverse_document(checker.document_only(events), baseline, creation_state=state)
    return validate_document_and_journal(checker.document_only(events), baseline, calls, thread, creation_state=state)


def disabled_events(events, workspace):
    """Journal-only synthetic policy variation; leave document facts identical."""
    value = deepcopy(events)
    value = [e for e in value if e.get("item", {}).get("tool") != "export_decision_record"]
    for e in value:
        result = e.get("item", {}).get("result")
        if result and isinstance(result.get("structured_content"), dict):
            p = result["structured_content"]
            p.update(record_status="disabled", record_id=None)
            p.pop("record_error", None)
    pair = failed_pair(workspace, tool="export_decision_record", ident="expected-export")
    for e in pair:
        e["item"]["arguments"] = dict(workspace=workspace, max_records=20)
    pair[1]["item"]["result"]["content"][0]["text"] = "Error executing tool export_decision_record: workspace_uninitialized: operation refused"
    value[-2:-2] = pair
    return value


def insert_probe(events, output, args=None, *, after_preflight=False, ident="probe"):
    value = deepcopy(events)
    pair = failed_pair(output, ident=ident)
    for e in pair:
        e["item"]["arguments"].update(args or {})
    offset = 4
    if after_preflight:
        offset = next(i + 1 for i, e in enumerate(value) if e["type"] == "item.completed"
                      and e.get("item", {}).get("tool") == "preflight_edits")
    value[offset:offset] = pair
    return value


def test_all_public_navigation_classes_with_full_creation(component, record_property):
    events, baseline, _ = component
    assert run(component)["status"] == "passed"
    output = baseline["output_path"]
    # Remove a redundant navigation result so browse is otherwise unresolved,
    # rather than being accidentally tested only as ordinary later recovery.
    original = [e for e in events if not (e.get("item", {}).get("tool") == "inspect_document"
        and e["item"]["arguments"].get("path") == output and e["item"]["arguments"].get("mode") == "browse")]
    limits = [{}, {"max_items": 1}, {"max_items": 37}, {"max_items": 50}, {"max_items": 100}]
    admitted = []
    for mode in ("outline", "browse"):
        for flags in product((False, True), repeat=4):
            optional = {key: None for key, include in zip(("phrases", "match_basis", "selection", "cursor"), flags) if include}
            for limit in limits:
                args = dict(mode=mode, **optional, **limit)
                result = run(component, insert_probe(original, output, args))
                assert result["creation_probe"]["route"] == "initial_output_discovery"
                admitted.append(args)
    for basis in ("exact_literal", "normalized_literal", "normalized_casefold_literal"):
        for selection, cursor in product((False, True), repeat=2):
            for limit in limits:
                args = dict(mode="literal_search", phrases=["inventory audit"], match_basis=basis, **limit)
                if selection:
                    args["selection"] = None
                if cursor:
                    args["cursor"] = None
                assert run(component, insert_probe(original, output, args))["creation_probe"]
                admitted.append(args)
    assert run(component)["status"] == "passed"
    record_property("admitted_full_creation_classes", json.dumps(admitted))


def test_invalid_navigation_classes_cause_eligibility_refusal(component, record_property):
    events, baseline, _ = component
    good = insert_probe(events, baseline["output_path"])
    assert run(component, good)["creation_probe"]
    invalid = [{"max_items": v} for v in (True, False, 1.0, "1", None, 0, 101)]
    invalid += [{"extra": None}, {"mode": "comments"}, {"cursor": "invented"}, {"phrases": ["x"]},
        {"match_basis": "exact_literal"}, {"selection": {}},
        {"mode": "literal_search"}, {"mode": "literal_search", "phrases": ["x"], "match_basis": None},
        {"mode": "literal_search", "phrases": [" "] , "match_basis": "exact_literal"},
        {"mode": "literal_search", "phrases": ["x"]*21, "match_basis": "exact_literal"},
        {"mode": "literal_search", "phrases": ["x"*2001], "match_basis": "exact_literal"},
        {"mode": "literal_search", "phrases": ["x"*2000]*6, "match_basis": "exact_literal"},
        {"mode": "literal_search", "phrases": ["\ud800"], "match_basis": "exact_literal"}]
    causes = []
    for args in invalid:
        assert not initial_navigation(dict(path=baseline["output_path"], mode="outline") | args, baseline["output_path"])
        with pytest.raises(checker.EvidenceError, match="not eligible initial output navigation") as caught:
            run(component, insert_probe(events, baseline["output_path"], args))
        causes.append(dict(args=args, cause=str(caught.value)))
        assert run(component, good)["creation_probe"]
    record_property("causal_invalid_navigation", json.dumps(causes))


def test_combined_routes_and_isolation(component, record_property):
    events, baseline, _ = component
    frozen_parser = __import__("check_paragraph_acceptance").native_calls
    require_frozen_dependencies()
    assert run(component)["status"] == "passed"
    cases = []
    for disabled in (False, True):
        origin = disabled_events(events, str(Path(baseline["source_path"]).parent)) if disabled else events
        assert run(component, origin, disabled=disabled)["status"] == "passed"
        for count in (1, 2, 3):
            value = deepcopy(origin)
            for i in range(count):
                value = insert_probe(value, baseline["output_path"], {"max_items": 1}, after_preflight=i > 0, ident=f"probe-{i}")
            value[4:4] = failed_pair(baseline["source_path"], mode="read", ident="recovered-source")
            result = run(component, value, disabled=disabled)
            routes = [r["route"] for r in result["failure_ledger"]]
            assert routes.count("initial_output_discovery") == count
            assert routes.count("ordinary_recovered") == 1
            assert routes.count("expected_unavailable_export") == int(disabled)
            bad = deepcopy(value)
            bad[4:4] = failed_pair(baseline["source_path"], mode="outline", ident="extra-unresolved")
            with pytest.raises(checker.EvidenceError, match="not eligible initial output navigation") as caught:
                run(component, bad, disabled=disabled)
            assert run(component, value, disabled=disabled)["exact_revisions_verified"]
            cases.append(dict(disabled=disabled, probes=count, routes=routes, refusal=str(caught.value)))
    assert __import__("check_paragraph_acceptance").native_calls is frozen_parser
    require_frozen_dependencies()
    assert run(component)["status"] == "passed"
    record_property("causal_combined_routes", json.dumps(cases))


def body_block(function, start, end):
    tree = ast.parse(Path(function).read_text())
    f = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("validate_")
             and n.name in {"validate_evidence", "validate_adverse_document"})
    begin = next(i for i, n in enumerate(f.body) if ast.unparse(n).startswith(start))
    stop = next(i for i, n in enumerate(f.body) if i > begin and ast.unparse(n).startswith(end))
    return [ast.dump(n, include_attributes=False) for n in f.body[begin:stop]]


def test_complete_static_port_and_frozen_helper_lineage():
    source = Path(__import__("check_paragraph_acceptance").__file__)
    port = Path(adverse.__file__)
    assert body_block(source, "source, output =", "exports =") == body_block(port, "source, output =", "journal =")
    def final_hash_checks(path, name):
        function = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == name)
        return [ast.dump(n, include_attributes=False) for n in function.body[-3:-1]]
    assert final_hash_checks(source, "validate_evidence") == final_hash_checks(port, "validate_adverse_document")
    # All final source/output rechecks are identical too, beyond the journal seam.
    for name in ("_baseline", "_paragraphs", "_text", "_unit", "_collateral", "_RESULT_VALIDATORS"):
        assert getattr(adverse, name) is getattr(__import__("check_paragraph_acceptance"), name)
    require_frozen_dependencies()


def test_substantive_negative_equivalence_from_passing_components(component, record_property):
    original, baseline, _ = component
    output, source = baseline["output_path"], baseline["source_path"]
    workspace = str(Path(source).parent)
    causes = []
    cases = {
        "source_read": "input lacks ordered full read and exact verification",
        "source_quote": "input lacks ordered full read and exact verification",
        "output_read": "full expected output lacks ordered native read and verification",
        "output_quote": "full expected output lacks ordered native read and verification",
        "deletion_quote": "exact deletion lacks ordered full read, extraction and native verification",
        "source_extract": "legacy native source extraction missing",
        "output_extract": "complete exact output revisions lack native extraction",
        "proof": "preflight proof is not exact and applicable",
        "candidate": "output bytes differ from preflight candidate",
        "publication": "publication binding missing",
        "author": "source or author result binding differs",
        "diagnostic": "preflight edit facts differ",
        "wording": "reported edit wording differs",
        "revision_id": "exact output revision identity missing",
        "target_identity": "paragraph identity is missing or disguised as a source unit",
    }
    for disabled in (False, True):
        good = disabled_events(original, workspace) if disabled else deepcopy(original)
        good = insert_probe(good, output, {"max_items": 1}, after_preflight=True)
        assert run(component, good, disabled=disabled)["collateral_verified"]
        for fault, expected in cases.items():
            value = deepcopy(good)
            if fault.endswith(("read", "quote", "extract")):
                side, kind = fault.split("_")
                path = source if side == "source" else output
                tool = {"read": "inspect_document", "quote": "verify_quote", "extract": "extract_redlines"}[kind]
                def remove(e):
                    i = e.get("item", {})
                    a = i.get("arguments", {})
                    return i.get("tool") == tool and a.get("path") == path and (
                        kind != "read" or a.get("mode") == "read") and (
                        side != "deletion" or "change_unit_id" in a.get("anchor", {})) and (
                        fault != "output_quote" or "paragraph_index" in a.get("anchor", {}))
                value = [e for e in value if not remove(e)]
            else:
                completed = [e["item"] for e in value if e["type"] == "item.completed"]
                pre = next(i for i in completed if i.get("tool") == "preflight_edits")
                app = next(i for i in completed if i.get("tool") == "apply_edits")
                pp, ap = pre["result"]["structured_content"], app["result"]["structured_content"]
                if fault == "proof":
                    for e in value:
                        if e.get("item", {}).get("tool") == "apply_edits":
                            e["item"]["arguments"]["preflight_proof"]["proof_sha256"] = "0"*64
                elif fault == "candidate":
                    pp["candidate_sha256"] = "0"*64
                elif fault == "publication":
                    ap["output_sha256"] = "0"*64
                elif fault == "author":
                    ap["tracked_change_author"] = "Different author"
                elif fault == "diagnostic":
                    pp["edits"][0]["match_count"] = 2
                elif fault == "wording":
                    ap["applied"][0]["inserted_text"] = "Different wording"
                elif fault == "revision_id":
                    ap["applied"][0]["tracked_revision_ids"] = ["1234567"]
                elif fault == "target_identity":
                    item = next(i for i in ap["applied"] if "target" in i)
                    item["target"]["paragraph_ref"]["paragraph_index"] = 99
            with pytest.raises(checker.EvidenceError, match=expected) as caught:
                run(component, value, disabled=disabled)
            assert run(component, good, disabled=disabled)["collateral_verified"]
            causes.append(dict(component="adverse" if disabled else "positive", fault=fault, cause=str(caught.value), restored=True))
    record_property("causal_full_document_equivalence", json.dumps(causes))


def test_adverse_journal_and_provenance_negatives(component, record_property):
    original, baseline, _ = component
    good = disabled_events(original, str(Path(baseline["source_path"]).parent))
    good = insert_probe(good, baseline["output_path"])
    assert run(component, good, disabled=True)["journal"]["positive_journal_pass"] is False
    causes = []
    for fault, cause in {
        "missing_export": "actual expected export failure",
        "early_export": "actual expected export failure",
        "wrong_error": "unavailable journal error or workspace differs",
        "wrong_workspace": "unavailable journal error or workspace differs",
        "unbounded_export": "adverse export workspace or bounded first-page arguments differ",
        "disabled_record_error": "native result violates public contract",
        "written": "adverse result lacks disabled/null/no-error provenance",
    }.items():
        events = deepcopy(good)
        if fault in {"missing_export", "early_export"}:
            pair = [e for e in events if e.get("item", {}).get("tool") == "export_decision_record"]
            events = [e for e in events if e.get("item", {}).get("tool") != "export_decision_record"]
            if fault == "early_export":
                events[4:4] = pair
        for e in events:
            i = e.get("item", {})
            if i.get("tool") == "export_decision_record":
                if fault == "wrong_workspace":
                    i["arguments"]["workspace"] = "/different"
                elif fault == "unbounded_export":
                    i["arguments"]["max_records"] = 21
                elif fault == "wrong_error" and i.get("result"):
                    i["result"]["content"][0]["text"] = "Other error"
            if i.get("tool") == "preflight_edits" and i.get("result"):
                p = i["result"]["structured_content"]
                if fault == "written":
                    p.update(record_status="written", record_id="dr_1")
                elif fault == "disabled_record_error":
                    p["record_error"] = None
        with pytest.raises(checker.EvidenceError, match=cause) as caught:
            run(component, events, disabled=True)
        assert run(component, good, disabled=True)["status"] == "passed"
        causes.append(dict(fault=fault, cause=str(caught.value), restored=True))
    # Positive scope never adopts the adverse export exemption or disabled provenance.
    with pytest.raises(checker.EvidenceError, match="not eligible initial output navigation"):
        run(component, good)
    assert run(component)["journal"]["complete_pages_verified"]
    record_property("causal_adverse_policy", json.dumps(causes))


def test_public_probe_publication_boundary_is_causal(component, record_property):
    original, baseline, _ = component
    good = insert_probe(original, baseline["output_path"], after_preflight=True)
    assert run(component, good)["creation_probe"]
    causes = []
    for overlap in (False, True):
        value = deepcopy(good)
        pair = [e for e in value if e.get("item", {}).get("id") == "probe"]
        value = [e for e in value if e.get("item", {}).get("id") != "probe"]
        app_start = next(i for i, e in enumerate(value) if e["type"] == "item.started"
                         and e.get("item", {}).get("tool") == "apply_edits")
        if overlap:
            value[app_start:app_start] = pair[:1]
            value[app_start+2:app_start+2] = pair[1:]
        else:
            app_end = next(i for i, e in enumerate(value) if e["type"] == "item.completed"
                           and e.get("item", {}).get("tool") == "apply_edits")
            value[app_end+1:app_end+1] = pair
        with pytest.raises(checker.EvidenceError, match="failed probe overlaps or follows selected destination creation") as caught:
            run(component, value)
        assert run(component, good)["creation_probe"]
        causes.append(dict(overlap=overlap, cause=str(caught.value), restored=True))
    record_property("causal_publication_cutoff", json.dumps(causes))
