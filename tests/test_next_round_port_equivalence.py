# SPDX-License-Identifier: Apache-2.0
"""Run frozen substantive controls against both independently passing components.

Only the test module receives an isolated facade; frozen checker globals/functions
are never replaced. Journal variations are explicitly fabricated test envelopes.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_paragraph_acceptance as original_tests
import check_paragraph_acceptance as frozen
from nr03_adverse_document import validate_adverse_document
from nr03_scenario import SERVER
from test_next_round_v3 import disabled_events, normalized


def adverse_input(events, baseline):
    b = deepcopy(baseline)
    b["server_name"] = SERVER
    value = deepcopy(events)
    value.insert(-1, dict(type="item.completed", item=dict(id="final", type="agent_message", text="Synthetic component result.")))
    value = disabled_events(value, str(Path(b["source_path"]).parent))
    for event in value:
        if event.get("item", {}).get("type") == "mcp_tool_call":
            event["item"]["server"] = SERVER
    return normalized(value), b


def paired_check(events, baseline, observations):
    outcomes = []
    original_bytes = deepcopy(events)
    for name in ("positive", "adverse"):
        try:
            if name == "positive":
                result = frozen.validate_evidence(events, baseline)
            else:
                rows, b = adverse_input(events, baseline)
                result = validate_adverse_document(rows, b, creation_state=None)
            assert result["collateral_verified"] and result["exact_revisions_verified"]
            outcomes.append((name, "PASS", result))
        except frozen.EvidenceError as error:
            outcomes.append((name, str(error), error))
    assert events == original_bytes
    assert outcomes[0][1] == outcomes[1][1], [(name, cause) for name, cause, _ in outcomes]
    observations.append(dict(positive=outcomes[0][1], adverse=outcomes[1][1]))
    if outcomes[0][1] != "PASS":
        raise outcomes[0][2]
    return outcomes[0][2]


@pytest.mark.parametrize("scenario", ["clean", "mixed", "empty", "mixed_empty"])
def test_frozen_complete_positive_forms_survive_adverse_port(tmp_path, monkeypatch, scenario):
    monkeypatch.delenv("VEQTOR_DISABLE_DECISION_RECORD", raising=False)
    events, baseline = original_tests.build_evidence(tmp_path, mixed=scenario in {"mixed", "mixed_empty"},
                                                      empty=scenario in {"empty", "mixed_empty"})
    assert paired_check(events, baseline, [])["full_paragraphs_verified"]


STRUCTURE_FAULTS = ["duplicate_ppr", "drawing", "bookmarks", "combined", "empty_run", "wrapper_attribute",
    "body_tail", "table_tail", "combined_tails", "legacy_tail", "whitespace_tail", "tail_bookmarks",
    "space_missing", "space_default", "space_invalid", "space_deleted", "space_equivalent", "context_direct_p",
    "context_nested_r", "context_away"]


@pytest.mark.parametrize("fault", STRUCTURE_FAULTS)
def test_complete_structural_controls_have_same_causal_refusal(tmp_path, monkeypatch, fault, record_property):
    observations, saved = [], {}
    identity = frozen.validate_evidence
    parser = frozen.native_calls
    def compare(events, baseline):
        result = paired_check(events, baseline, observations)
        if not saved:
            saved.update(events=deepcopy(events), baseline=deepcopy(baseline),
                         files={path: Path(path).read_bytes() for path in [*baseline["source_sha256"], baseline["output_path"]]})
        return result
    # Reuse the existing causal mutation body, while exercising BOTH actual
    # validators. This facade is a test-local dependency, not a product patch.
    facade = SimpleNamespace(**{**vars(frozen), "validate_evidence": compare})
    with monkeypatch.context() as local:
        local.setattr(original_tests, "checker", facade)
        original_tests.test_independent_checker_rejects_actual_structural_collateral(tmp_path, local, fault)
    assert observations[0] == dict(positive="PASS", adverse="PASS")
    for path, data in saved["files"].items():
        Path(path).write_bytes(data)
    assert paired_check(saved["events"], saved["baseline"], observations)["status"] == "passed"
    assert frozen.validate_evidence is identity and frozen.native_calls is parser
    record_property("causal_structural_equivalence", __import__("json").dumps(dict(fault=fault, observations=observations)))


@pytest.mark.parametrize("fault", ["neighbour", "original_format", "insert_format"])
def test_rebound_actual_formatting_and_collateral_preserve_refusal(tmp_path, monkeypatch, fault, record_property):
    monkeypatch.delenv("VEQTOR_DISABLE_DECISION_RECORD", raising=False)
    evidence = original_tests.build_evidence(tmp_path)
    events, baseline = evidence
    original = deepcopy(events)
    data = Path(baseline["output_path"]).read_bytes()
    observations = []
    assert paired_check(events, baseline, observations)["status"] == "passed"
    facade = SimpleNamespace(**{**vars(frozen), "validate_evidence": lambda e, b: paired_check(e, b, observations)})
    with monkeypatch.context() as local:
        local.setattr(original_tests, "checker", facade)
        original_tests.test_rebound_logs_still_require_actual_collateral_preservation(evidence, fault)
    Path(baseline["output_path"]).write_bytes(data)
    assert paired_check(original, baseline, observations)["status"] == "passed"
    record_property("causal_formatting_equivalence", __import__("json").dumps(dict(fault=fault, observations=observations)))


def test_empty_output_read_and_exact_deletion_remain_required(tmp_path, monkeypatch, record_property):
    monkeypatch.delenv("VEQTOR_DISABLE_DECISION_RECORD", raising=False)
    original, baseline = original_tests.build_evidence(tmp_path, mixed=True, empty=True)
    observations = []
    assert paired_check(original, baseline, observations)["status"] == "passed"
    for fault in ("empty_read", "empty_deletion"):
        events = deepcopy(original)
        empty_index = next(r["paragraph_index"] for r in baseline["expected_paragraphs"] if not r["after"])
        units = frozen.extract_redlines(baseline["output_path"])["change_units"]
        empty_anchors = [u["anchor"] for u in units if u["reference"]["paragraph_index"] == empty_index]
        def omit(e):
            i = e.get("item", {})
            args = i.get("arguments", {})
            if args.get("path") != baseline["output_path"]:
                return False
            if fault == "empty_read":
                return args.get("mode") == "read" and args.get("selection", {}).get("paragraph_ref", {}).get("paragraph_index") == empty_index
            return i.get("tool") == "verify_quote" and args.get("anchor") in empty_anchors
        events = [e for e in events if not omit(e)]
        with pytest.raises(frozen.EvidenceError, match="exact deletion lacks ordered full read, extraction and native verification"):
            paired_check(events, baseline, observations)
        assert paired_check(original, baseline, observations)["status"] == "passed"
    record_property("causal_empty_output", __import__("json").dumps(observations))
