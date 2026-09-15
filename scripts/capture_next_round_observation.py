# SPDX-License-Identifier: Apache-2.0
"""Freeze/capture operator-controlled NR-03 variants, without an acceptance verdict.

Allows real conversational follow-ups and externally timed synthetic perturbations.
It never selects MCP calls, mutates DOCX or interprets an expected outcome as proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from check_codex_acceptance import _file_sha256, _json, _read, _require
from capture_next_round_session import command_for
from nr03_scenario import EFFORTS, MODEL, WORKFLOW_FILES
from nr03_delivery import delivered_prompt, validate_export_limits
from nr03_fault import FAULT, fault_args
from prepare_next_round_acceptance import installed, read_json, state, write_json
from nr03_app_server import PROFILE


def validate_plan(value):
    _require(isinstance(value, dict) and set(value) in ({"scenario", "steps", "expected"}, {"scenario", "steps", "expected", "fault"})
             and isinstance(value["scenario"], str) and value["scenario"].strip()
             and isinstance(value["expected"], dict) and value["expected"]
             and isinstance(value["steps"], list) and 1 <= len(value["steps"]) <= 12, "invalid predeclared observation plan")
    seen = set()
    for step in value["steps"]:
        _require(isinstance(step, dict) and set(step) == {"id", "resume", "prompt"}
                 and isinstance(step["id"], str) and re.fullmatch(r"[a-z][a-z0-9-]{0,39}", step["id"])
                 and step["id"] not in seen and (step["resume"] is None or step["resume"] in seen)
                 and isinstance(step["prompt"], str) and step["prompt"].strip(), "invalid observation step/resume")
        seen.add(step["id"])
    fault = value.get("fault")
    _require(fault is None or (isinstance(fault, dict) and set(fault) == {"kind", "step"}
             and fault["kind"] == FAULT and fault["step"] in seen), "invalid predeclared fault step")
    return value


def freeze(bundle, plan_file):
    from check_next_round_acceptance import load_baseline
    bundle = Path(bundle).absolute()
    baseline, _ = load_baseline(bundle)
    plan = validate_plan(read_json(plan_file))
    folder = bundle / "observations"
    folder.mkdir(mode=0o700)
    write_json(folder / "plan.json", dict(schema_version="veqtor_next_round_observations.v1",
        prepared_ns=time.time_ns(), baseline_sha256=_file_sha256(str(bundle / "baseline.json")),
        installation_sha256=baseline["installation_sha256"], workflow_sha256=baseline["workflow_sha256"],
        client_selection=baseline["client_selection"], initial_state=state(baseline["matter"]), plan=plan))
    for step in plan["steps"]:
        (folder / ("client-" + step["id"])).mkdir(mode=0o700)


def step_fault(plan, step_id):
    fault = plan.get("fault")
    return fault if fault is not None and fault["step"] == step_id else None


def step_command(codex, installation, baseline, plan, step_id, resumed, source_profile=None):
    """Use this step's frozen selection and fault, never a later step's settings."""
    if source_profile is not None:
        from nr03_app_server import PROFILE
        from capture_nr03_app_server import command_for as app_command, launch_config
        _require(source_profile == PROFILE, "unsupported observation source profile")
        config = launch_config(installation["python"], **baseline["client_selection"],
            journal_disabled=baseline["variant"] == "journal-unavailable",
            server_args=fault_args(baseline["matter"]) if step_fault(plan, step_id) is not None else None)
        return app_command(codex, config)
    command = command_for(codex, installation["python"], "a-write" if resumed is not None else "a-brief",
        **baseline["client_selection"], thread_id=resumed,
        journal_disabled=baseline["variant"] == "journal-unavailable")
    if step_fault(plan, step_id) is not None:
        for i, argument in enumerate(command):
            if argument.startswith("mcp_servers.veqtor_nr03.args="):
                command[i] = "mcp_servers.veqtor_nr03.args=" + json.dumps(fault_args(baseline["matter"]), separators=(",", ":"))
    return command


def resume_parent(folder, plan, frozen, parent, baseline, installation):
    """Bind every ancestor's delivered input and launch to its original turn."""
    from nr03_app_server import parse_capture
    by_id = {step["id"]: step for step in plan["steps"]}
    chain = []
    while parent is not None:
        chain.append(parent)
        parent = by_id[parent]["resume"]
    cwd = folder / ("client-" + chain[-1])
    plan_hash = _file_sha256(str(folder / "plan.json"))
    original_thread = None
    parent_hash = None
    parent_source_prefix = None
    parent_source_turn = None
    for ancestor in reversed(chain):
        receipt_path = folder / f"{ancestor}.receipt.json"
        events_path = folder / f"{ancestor}.jsonl"
        prompt_path = folder / f"{ancestor}.prompt.txt"
        _require(all(path.is_file() and not path.is_symlink() for path in (receipt_path, events_path, prompt_path)),
                 "observation ancestor evidence is missing or is a symlink")
        receipt_raw = _read(receipt_path)
        receipt = _json(receipt_raw.decode())
        raw = _read(events_path)
        prompt = _read(prompt_path)
        expected = by_id[ancestor]["prompt"]
        if by_id[ancestor]["resume"] is None:
            expected = delivered_prompt(folder.parent, expected, WORKFLOW_FILES)
        _require(receipt.get("prompt_sha256") == hashlib.sha256(prompt).hexdigest()
                 and prompt == expected.encode(), "observation ancestor delivered input differs from its frozen step")
        _require(receipt["events_sha256"] == hashlib.sha256(raw).hexdigest()
                 and receipt["exit_code"] == 0 and receipt["step"] == ancestor
                 and receipt["plan_sha256"] == plan_hash
                 and receipt["installation_sha256"] == frozen["installation_sha256"],
                 "observation parent did not complete this frozen plan")
        parsed = parse_capture(folder, ancestor, receipt, installation["producer"], require_tool_calls=False)
        thread, calls = parsed["thread"], parsed["calls"]
        if "source" in receipt:
            current_prefix = (folder / f"{ancestor}.session.jsonl").read_bytes()
            _require(original_thread is None or (parent_source_prefix is not None and current_prefix.startswith(parent_source_prefix)
                     and receipt["source"]["turn_id"] != parent_source_turn), "source observation parent prefix differs")
            parent_source_prefix, parent_source_turn = current_prefix, receipt["source"]["turn_id"]
        else:
            parent_source_prefix = parent_source_turn = None
        validate_export_limits(calls)
        _require(receipt.get("resumed_thread_id") == original_thread,
                 "observation requested thread differs from the original conversation")
        _require(original_thread is None or thread == original_thread,
                 "observation returned thread differs from the requested resume target")
        _require(receipt.get("parent_receipt_sha256") == parent_hash,
                 "observation parent receipt chain differs")
        _require(receipt["cwd"] == str(cwd), "observation parent working directory differs")
        command = receipt.get("command")
        _require(isinstance(command, list) and command and all(isinstance(part, str) for part in command)
                 and Path(command[0]).is_absolute(), "observation ancestor native command absent or invalid")
        _require("fault" in receipt and receipt["fault"] == step_fault(plan, ancestor)
                 and command == step_command(command[0], installation, baseline, plan, ancestor, original_thread,
                                              source_profile=receipt.get("source", {}).get("profile")),
                 "observation ancestor launch/fault differs from its frozen step")
        original_thread = thread
        parent_hash = hashlib.sha256(receipt_raw).hexdigest()
    return original_thread, parent_hash, cwd


def capture(bundle, step_id, codex, *, model, reasoning_effort, source_profile=PROFILE):
    from capture_nr03_app_server import bind_delivery, capture as app_capture, launch_config
    bundle = Path(bundle).absolute()
    folder = bundle / "observations"
    frozen = read_json(folder / "plan.json")
    plan = validate_plan(frozen["plan"])
    b = read_json(bundle / "baseline.json")
    report = installed(read_json(bundle / "installation.json"))
    _require(frozen["baseline_sha256"] == _file_sha256(str(bundle / "baseline.json"))
             and frozen["installation_sha256"] == _file_sha256(str(bundle / "installation.json"))
             and frozen["client_selection"] == b["client_selection"] == dict(model=model, reasoning_effort=reasoning_effort),
             "observation identity/selection drift")
    for name in WORKFLOW_FILES:
        _require(_file_sha256(str(bundle / "workflow" / name)) == frozen["workflow_sha256"][name]
                 == _file_sha256(str(Path(report["source_root"]) / name)), "observation workflow drift")
    step = next(s for s in plan["steps"] if s["id"] == step_id)
    prompt = step["prompt"]
    resume = None
    parent_hash = None
    cwd = folder / ("client-" + step_id)
    if step["resume"] is not None:
        resume, parent_hash, cwd = resume_parent(folder, plan, frozen, step["resume"], b, report)
        if source_profile is not None:
            _require(read_json(folder / f"{step['resume']}.receipt.json").get("source", {}).get("profile") == PROFILE,
                     "new source capture cannot resume legacy evidence")
    else:
        prompt = delivered_prompt(bundle, prompt, WORKFLOW_FILES)
    command = step_command(codex, report, b, plan, step_id, resume, source_profile=source_profile)
    fault = step_fault(plan, step_id)
    from capture_position_session import private_write
    private_write(folder / f"{step_id}.prompt.txt", prompt.encode())
    before = state(b["matter"])
    started = time.time_ns()
    config = launch_config(report["python"], **b["client_selection"], journal_disabled=b["variant"] == "journal-unavailable",
        server_args=fault_args(b["matter"]) if fault is not None else None)
    if source_profile is None:
        # Explicit legacy controls retain their original transport, without K credit.
        fd = os.open(folder / f"{step_id}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        err_fd = os.open(folder / f"{step_id}.stderr.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output, os.fdopen(err_fd, "wb") as errors:
            result = subprocess.run(command, input=prompt.encode(), cwd=cwd, stdout=output, stderr=errors, check=False)
        code, source = result.returncode, None
    else:
        code, source = app_capture(folder, step_id, command, config, prompt, cwd, b["client_selection"], resumed=resume,
            parent_prefix=folder / f"{step['resume']}.session.jsonl" if resume else None)
    receipt = dict(schema_version="veqtor_next_round_observation_capture.v2" if source is not None else "veqtor_next_round_observation_capture.v1",
        plan_sha256=_file_sha256(str(folder / "plan.json")), step=step_id, command=command, cwd=str(cwd),
        resumed_thread_id=resume, parent_receipt_sha256=parent_hash,
        prompt_sha256=_file_sha256(str(folder / f"{step_id}.prompt.txt")),
        events_sha256=_file_sha256(str(folder / f"{step_id}.jsonl")),
        installation_sha256=_file_sha256(str(bundle / "installation.json")),
        started_ns=started, finished_ns=time.time_ns(), before=before, after=state(b["matter"]),
        exit_code=code, fault=fault, acceptance_assessed=False)
    if source is not None:
        receipt["source"] = source
        bind_delivery(folder, step_id, receipt)
    else:
        write_json(folder / f"{step_id}.receipt.json", receipt)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("freeze")
    create.add_argument("--plan", required=True)
    run = sub.add_parser("capture")
    run.add_argument("--step", required=True)
    run.add_argument("--codex", required=True)
    run.add_argument("--model", choices=(MODEL,), required=True)
    run.add_argument("--reasoning-effort", choices=EFFORTS, required=True)
    args = parser.parse_args()
    if args.action == "freeze":
        freeze(args.bundle, args.plan)
        print("Synthetic observation plan frozen; no acceptance claimed.")
        return 0
    return capture(args.bundle, args.step, args.codex, model=args.model, reasoning_effort=args.reasoning_effort)


if __name__ == "__main__":
    raise SystemExit(main())
