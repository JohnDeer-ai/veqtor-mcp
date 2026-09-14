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
from nr03_fault import FAULT, fault_args
from prepare_next_round_acceptance import installed, read_json, state, write_json


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


def capture(bundle, step_id, codex, *, model, reasoning_effort):
    from check_next_round_acceptance import parse_native
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
        parent = step["resume"]
        parent_receipt = read_json(folder / f"{parent}.receipt.json")
        raw = _read(folder / f"{parent}.jsonl")
        _require(parent_receipt["events_sha256"] == hashlib.sha256(raw).hexdigest()
                 and parent_receipt["exit_code"] == 0 and parent_receipt["step"] == parent
                 and parent_receipt["plan_sha256"] == _file_sha256(str(folder / "plan.json"))
                 and parent_receipt["installation_sha256"] == frozen["installation_sha256"],
                 "observation parent did not complete this frozen plan")
        resume, _, _ = parse_native([_json(line) for line in raw.decode().splitlines()], report["producer"],
                                    require_tool_calls=False)
        parent_hash = _file_sha256(str(folder / f"{parent}.receipt.json"))
        by_id = {s["id"]: s for s in plan["steps"]}
        root_step = by_id[parent]
        while root_step["resume"] is not None:
            root_step = by_id[root_step["resume"]]
        cwd = folder / ("client-" + root_step["id"])
        _require(parent_receipt["cwd"] == str(cwd), "observation parent working directory differs")
    else:
        prompt = "Use this delivered Veqtor skill and resolved workflow for the user request below.\n\n" + "\n\n".join(
            (bundle / "workflow" / name).read_text() for name in WORKFLOW_FILES) + "\n\n" + prompt
    command = command_for(codex, report["python"], "a-write" if resume else "a-brief",
        model=model, reasoning_effort=reasoning_effort, thread_id=resume,
        journal_disabled=b["variant"] == "journal-unavailable")
    fault = plan.get("fault")
    injected = fault is not None and fault["step"] == step_id
    if injected:
        for i, argument in enumerate(command):
            if argument.startswith("mcp_servers.veqtor_nr03.args="):
                command[i] = "mcp_servers.veqtor_nr03.args=" + json.dumps(fault_args(b["matter"]), separators=(",", ":"))
    from capture_position_session import private_write
    private_write(folder / f"{step_id}.prompt.txt", prompt.encode())
    before = state(b["matter"])
    started = time.time_ns()
    # Stream events to an exclusive private file so an external observer can act
    # after an actual native read. No race is inferred solely from process overlap.
    fd = os.open(folder / f"{step_id}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    err_fd = os.open(folder / f"{step_id}.stderr.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output, os.fdopen(err_fd, "wb") as errors:
        result = subprocess.run(command, input=prompt.encode(), cwd=cwd, stdout=output, stderr=errors, check=False)
    write_json(folder / f"{step_id}.receipt.json", dict(schema_version="veqtor_next_round_observation_capture.v1",
        plan_sha256=_file_sha256(str(folder / "plan.json")), step=step_id, command=command, cwd=str(cwd),
        resumed_thread_id=resume, parent_receipt_sha256=parent_hash,
        prompt_sha256=_file_sha256(str(folder / f"{step_id}.prompt.txt")),
        events_sha256=_file_sha256(str(folder / f"{step_id}.jsonl")),
        installation_sha256=_file_sha256(str(bundle / "installation.json")),
        started_ns=started, finished_ns=time.time_ns(), before=before, after=state(b["matter"]),
        exit_code=result.returncode, fault=fault if injected else None, acceptance_assessed=False))
    return result.returncode


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
