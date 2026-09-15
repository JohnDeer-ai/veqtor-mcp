# SPDX-License-Identifier: Apache-2.0
"""NR-03 v3 failure routes; preserve every original failure and frozen NR-01.

Ordinary recovery precedes classification of otherwise unresolved first-page
navigation. The generic unreadable error never proves absence or validity.
"""
from copy import deepcopy
from importlib import import_module
from types import FunctionType

from check_codex_acceptance import _digest, _file_sha256, _require, _sha256

PUBLIC_INSPECT_SHA256 = "c4e7b0c044af34fca9a2b8c3bed0a48f04b1b5a1285837c454f6e25e10cd4815"
POLICY_VERSION = "nr03-creation-discovery.v3"


def initial_navigation(args, output):
    inspect = import_module("veqtor_docx.inspect")
    _require(_file_sha256(inspect.__file__) == PUBLIC_INSPECT_SHA256,
             "creation discovery public semantics changed; review the policy")
    if (set(args) - {"path", "mode", "phrases", "match_basis", "selection", "cursor", "max_items"}
            or args.get("path") != output or not isinstance(args.get("mode"), str)
            or args.get("mode") not in {"outline", "browse", "literal_search"}
            or args.get("cursor") is not None):
        return False
    try:
        inspect._validate_common_inputs(args["mode"], args.get("phrases"), args.get("match_basis"),
            args.get("selection"), args.get("cursor"), args.get("max_items", inspect.DEFAULT_MAX_ITEMS))
    except inspect.InspectError:
        return False
    return True


def tool_error(call, code):
    return call["error"] is None and call["result"] == dict(structured_content=None,
        content=[dict(type="text", text=f"Error executing tool {call['tool']}: {code}: operation refused")])


def classify_failures(events, baseline, creation_state, *, unavailable_workspace=None):
    from check_next_round_acceptance import parse_native
    thread, parsed, _ = parse_native(events, baseline["producer"])
    _require(all(c["server"] == baseline["server_name"] and c["tool"] not in
                 {"read_deal_positions", "mutate_deal_positions"} for c in parsed),
             "creation profile requires the original document-tool event view")
    calls, failures, ledger, probes = [], [], [], []
    for index, call in enumerate(parsed):
        if not call["failed"]:
            calls.append(dict(call, call_index=index))
            continue
        original = events[call["completed_at"]]["item"]
        _require(original.get("status") == "failed",
                 "native MCP result reports failure without the supported failed status")
        _require(call["tool"] not in {"preflight_edits", "apply_edits"},
                 "preflight or apply attempt failed in the positive scenario")
        scope = {k: v for k, v in call["arguments"].items()
                 if k in {"path", "folder", "workspace", "source_path", "mode"}}
        failures.append(dict(call, call_index=index, scope=scope))
        recovered = next((c for c in parsed if not c["failed"] and c["tool"] == call["tool"]
            and c["started_at"] > call["completed_at"]
            and all(c["arguments"].get(k) == v for k, v in scope.items())), None)
        if recovered is not None:
            route = "ordinary_recovered"
        elif unavailable_workspace is not None and call["tool"] == "export_decision_record":
            _require(call["arguments"].get("workspace") == unavailable_workspace
                     and tool_error(call, "workspace_uninitialized"),
                     "unavailable journal error or workspace differs")
            route = "expected_unavailable_export"
        else:
            _require(call["tool"] == "inspect_document"
                     and initial_navigation(call["arguments"], baseline["output_path"])
                     and tool_error(call, "file_unreadable"),
                     "unresolved failed read is not eligible initial output navigation")
            probes.append(call)
            route = "initial_output_discovery"
        ledger.append(dict(id=call["id"], route=route, scope=scope,
            recovered_by=recovered["id"] if recovered else None, original_failure=deepcopy(original),
            started_at=call["started_at"], completed_at=call["completed_at"]))
    proof = None
    if probes:
        _require(isinstance(creation_state, dict) and set(creation_state) == {"initial", "before"},
                 "creation probe lacks independently bound initial and before-state")
        initial, before = creation_state["initial"], creation_state["before"]
        _require(isinstance(initial, dict) and set(initial) == {"docx", "store_sha256"}
                 and isinstance(before, dict) and before == initial
                 and initial["docx"] == baseline["source_sha256"]
                 and baseline["output_path"] not in initial["docx"]
                 and baseline["output_absent_before"] is True and _sha256(initial["store_sha256"]),
                 "creation probe initial absence or before-state is inconsistent")
        pres = [c for c in parsed if c["tool"] == "preflight_edits"]
        apps = [c for c in parsed if c["tool"] == "apply_edits"]
        _require(len(pres) == len(apps) == 1 and not pres[0]["failed"] and not apps[0]["failed"],
                 "creation probe requires one successful preflight and create apply")
        pre, app = pres[0], apps[0]
        _require(pre["completed_at"] < app["started_at"]
                 and all(c["completed_at"] < app["started_at"] for c in probes)
                 and app["arguments"].get("output_path") == baseline["output_path"],
                 "failed probe overlaps or follows selected destination creation")
        rows = [row for row in ledger if row["route"] == "initial_output_discovery"]
        proof = dict(rows[0], policy=POLICY_VERSION, probes=deepcopy(rows),
            initial_state_sha256=_digest(initial), before_state_sha256=_digest(before),
            preflight_id=pre["id"], apply_id=app["id"], output_path=baseline["output_path"],
            output_sha256=pre["payload"].get("candidate_sha256"))
    return thread, calls, failures, ledger, proof


def creation_probe_profile(events, baseline, creation_state, validator):
    thread, calls, failures, ledger, proof = classify_failures(events, baseline, creation_state)
    if proof is None:
        return validator, None, ledger

    def profile_calls(original_events, server_name):
        _require(original_events is events and server_name == baseline["server_name"],
                 "creation profile parser received another event stream")
        return thread, calls, failures

    # Same frozen code and one isolated dependency, never changed module globals.
    scoped = FunctionType(validator.__code__, dict(validator.__globals__, native_calls=profile_calls),
                          validator.__name__, validator.__defaults__, validator.__closure__)
    return scoped, proof, ledger
