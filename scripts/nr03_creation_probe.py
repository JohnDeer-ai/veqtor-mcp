# SPDX-License-Identifier: Apache-2.0
"""NR-03-only pre-creation outline profile for the frozen document validator.

This supplies a parser to an isolated function using the *unchanged* NR-01
validator code object. It neither changes NR-01's module globals nor rewrites
native events. Every substantive document check executes before a report can
credit the probe; the NR-03 adapter still checks every original journal page.
"""
from copy import deepcopy
from types import FunctionType

from check_codex_acceptance import _digest, _require, _sha256


def creation_probe_profile(events, baseline, creation_state, validator):
    # Late import avoids the main checker -> journal -> profile import cycle.
    # The same strict parser is used on the original document-only event view,
    # so pairing, ordering, payloads, producer and non-MCP refusals stay active.
    from check_next_round_acceptance import parse_native

    thread, parsed, _ = parse_native(events, baseline["producer"])
    _require(all(c["server"] == baseline["server_name"]
                 and c["tool"] not in {"read_deal_positions", "mutate_deal_positions"} for c in parsed),
             "creation profile requires the original document-tool event view")
    calls, failures, unresolved = [], [], []
    for index, call in enumerate(parsed):
        if not call["failed"]:
            calls.append(dict(call, call_index=index))
            continue
        _require(events[call["completed_at"]]["item"].get("status") == "failed",
                 "native MCP result reports failure without the supported failed status")
        _require(call["tool"] not in {"preflight_edits", "apply_edits"},
                 "preflight or apply attempt failed in the positive scenario")
        scope = {k: v for k, v in call["arguments"].items()
                 if k in {"path", "folder", "workspace", "source_path", "mode"}}
        failure = dict(call, call_index=index, scope=scope)
        failures.append(failure)
        # Preserve NR-01's exact later-call/scope/mode retry rule for every
        # ordinary failure. Only one otherwise unresolved outline may qualify.
        if not any(not c["failed"] and c["tool"] == call["tool"]
                   and c["started_at"] > call["completed_at"]
                   and all(c["arguments"].get(k) == v for k, v in scope.items()) for c in parsed):
            unresolved.append(call)
    if not unresolved:
        return validator, None
    _require(len(unresolved) == 1, "creation profile requires exactly one unresolved output probe")
    probe = unresolved[0]
    output = baseline["output_path"]
    original = events[probe["completed_at"]]["item"]
    _require(probe["tool"] == "inspect_document"
             and probe["arguments"] == dict(path=output, mode="outline")
             and original["status"] == "failed" and probe["error"] is None
             and probe["result"] == dict(structured_content=None, content=[dict(type="text", text=
                 "Error executing tool inspect_document: file_unreadable: operation refused")]),
             "unresolved failed read is not the supported pre-creation output probe")
    # The generic unreadable error is NOT evidence of absence. Require both the
    # independently frozen full inventory and receipt before-state supplied by
    # check_round, which binds them to the actual original baseline/receipt.
    _require(isinstance(creation_state, dict) and set(creation_state) == {"initial", "before"},
             "creation probe lacks independently bound initial and before-state")
    initial, before = creation_state["initial"], creation_state["before"]
    _require(isinstance(initial, dict) and set(initial) == {"docx", "store_sha256"}
             and isinstance(before, dict) and before == initial
             and initial["docx"] == baseline["source_sha256"] and output not in initial["docx"]
             and baseline["output_absent_before"] is True and _sha256(initial["store_sha256"]),
             "creation probe initial absence or before-state is inconsistent")
    pres = [c for c in parsed if c["tool"] == "preflight_edits"]
    apps = [c for c in parsed if c["tool"] == "apply_edits"]
    _require(len(pres) == len(apps) == 1 and not pres[0]["failed"] and not apps[0]["failed"],
             "creation probe requires one successful preflight and create apply")
    pre, app = pres[0], apps[0]
    _require(probe["completed_at"] < pre["started_at"] < pre["completed_at"] < app["started_at"]
             and app["arguments"].get("output_path") == output,
             "failed probe does not precede the selected destination creation")

    def profile_calls(original_events, server_name):
        _require(original_events is events and server_name == baseline["server_name"],
                 "creation profile parser received another event stream")
        return thread, calls, failures

    # Same frozen code, isolated globals with exactly one explicit profile
    # dependency. No monkeypatch, eval, code rewriting or exception-string waiver.
    scoped = FunctionType(validator.__code__, dict(validator.__globals__, native_calls=profile_calls),
                          validator.__name__, validator.__defaults__, validator.__closure__)
    proof = dict(id=probe["id"], original_failure=deepcopy(original),
                 started_at=probe["started_at"], completed_at=probe["completed_at"],
                 initial_state_sha256=_digest(initial), before_state_sha256=_digest(before),
                 preflight_id=pre["id"], apply_id=app["id"], output_path=output,
                 output_sha256=pre["payload"].get("candidate_sha256"))
    # The caller executes this validator and credits proof only after *all*
    # original substantive checks and the NR-03 journal gate have passed.
    return scoped, proof
