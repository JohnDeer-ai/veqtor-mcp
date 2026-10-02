# SPDX-License-Identifier: Apache-2.0
"""Public error boundary controls, without a workspace scan or model call."""
from copy import deepcopy
import json

import pytest

from veqtor_mcp import records, server
from check_codex_acceptance import EvidenceError
from nr03_adverse_document import validate_unavailable_exports
from nr03_creation_probe import tool_error


PREFIX = "Error executing tool export_decision_record: workspace_uninitialized: "
PAYLOAD = dict(error_code="workspace_uninitialized", workspace_discovery=dict(candidate_count=0,
    classification_complete=True, entry_limit=500, max_depth=1, scope="direct_children", time_limit_seconds=1.0))


def example():
    return dict(id="synthetic-export", tool="export_decision_record", failed=True, error=None, started_at=3, completed_at=4,
        arguments=dict(workspace="/selected", max_records=1),
        result=dict(structured_content=None, content=[dict(type="text", text=PREFIX + json.dumps(PAYLOAD))]))


def test_real_public_exception_formatter_matches_closed_contract():
    discovery = records.WorkspaceDiscovery(state="workspace_uninitialized", classification_complete=True, candidate_count=0)
    assert discovery.metadata() == PAYLOAD["workspace_discovery"]
    def fail(_workspace, _input):
        raise records.WorkspaceDiscoveryError(discovery)
    with pytest.raises(server._McpBoundaryError) as caught:
        server._run_tool_boundary(tool_name="export_decision_record", workspace_resolver=lambda: None,
            input_payload_factory=lambda: {}, internal_provenance_factory=lambda: {}, operation=fail)
    call = example()
    call["result"]["content"][0]["text"] = "Error executing tool export_decision_record: " + str(caught.value)
    assert tool_error(call, "workspace_uninitialized")
    call["result"]["content"][0]["text"] = PREFIX + json.dumps(PAYLOAD, indent=2)
    assert tool_error(call, "workspace_uninitialized")


FAULTS = ["generic", "substring", "transport", "success", "wrong_tool", "wrong_workspace", "unbounded", "boolean_limit",
    "cursor", "extra_arg", "structured", "extra_result", "extra_block", "block_metadata", "outer_code", "inner_code",
    "missing_discovery", "extra_payload", "extra_discovery", "duplicate_key", "trailing", "malformed", "nonfinite"]
FAULTS += ["missing_" + k for k in PAYLOAD["workspace_discovery"]]
FAULTS += ["typed_" + k for k in PAYLOAD["workspace_discovery"]]


@pytest.mark.parametrize("fault", FAULTS)
def test_initial_and_final_expected_error_refuse_and_restore(fault):
    call = example()
    def check(value):
        assert validate_unavailable_exports([], [value], "/selected", require_final=False)["status"] == "EXPECTED_UNAVAILABLE"
        verification = dict(tool="verify_quote", failed=False, completed_at=2)
        assert validate_unavailable_exports([verification], [value], "/selected")["status"] == "EXPECTED_UNAVAILABLE"
    check(call)
    bad, payload = deepcopy(call), deepcopy(PAYLOAD)
    text = None
    if fault in {"generic", "substring", "outer_code", "duplicate_key", "trailing", "malformed", "nonfinite"}:
        text = {"generic": PREFIX + "operation refused", "substring": "workspace_uninitialized",
            "outer_code": PREFIX.replace("workspace_uninitialized", "workspace_mismatch") + json.dumps(payload),
            "duplicate_key": PREFIX + json.dumps(payload)[:-1] + ',"error_code":"workspace_uninitialized"}',
            "trailing": PREFIX + json.dumps(payload) + " ignored", "malformed": PREFIX + "{",
            "nonfinite": PREFIX + json.dumps(payload).replace("1.0", "NaN")}[fault]
    elif fault == "transport":
        bad["error"] = {"message": "failed"}
    elif fault == "success":
        bad["failed"] = False
    elif fault == "wrong_tool":
        bad["tool"] = "inspect_document"
    elif fault in {"wrong_workspace", "unbounded", "boolean_limit", "cursor", "extra_arg"}:
        key, value = {"wrong_workspace": ("workspace", "/foreign"), "unbounded": ("max_records", 21),
            "boolean_limit": ("max_records", True), "cursor": ("before_record_id", "invented"), "extra_arg": ("unknown", 1)}[fault]
        bad["arguments"][key] = value
    elif fault in {"structured", "extra_result", "extra_block", "block_metadata"}:
        if fault == "structured":
            bad["result"]["structured_content"] = {}
        elif fault == "extra_result":
            bad["result"]["unknown"] = True
        elif fault == "extra_block":
            bad["result"]["content"].append(dict(type="text", text="contradiction"))
        else:
            bad["result"]["content"][0]["_meta"] = {}
    else:
        if fault == "inner_code":
            payload["error_code"] = "workspace_mismatch"
        elif fault == "missing_discovery":
            del payload["workspace_discovery"]
        elif fault == "extra_payload":
            payload["unknown"] = True
        elif fault == "extra_discovery":
            payload["workspace_discovery"]["stop_reason"] = "time_limit"
        elif fault.startswith("missing_"):
            del payload["workspace_discovery"][fault.removeprefix("missing_")]
        else:
            key = fault.removeprefix("typed_")
            payload["workspace_discovery"][key] = {"candidate_count": False, "classification_complete": 1,
                "entry_limit": 500.0, "max_depth": True, "scope": "recursive", "time_limit_seconds": 1}[key]
        text = PREFIX + json.dumps(payload)
    if text is not None:
        bad["result"]["content"][0]["text"] = text
    with pytest.raises(EvidenceError):
        check(bad)
    check(call)
