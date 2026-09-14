# SPDX-License-Identifier: Apache-2.0
"""Capture real Codex workflow turns with isolated MCP settings and exact stimuli."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time

from capture_position_session import private_write
from check_codex_acceptance import _file_sha256, _require
from nr03_scenario import AUTHOR, EFFORTS, MODEL, SERVER, STAGES, WORKFLOW_FILES
from position_source_launch import SERVER_ARGS
from prepare_next_round_acceptance import state, write_json


def quoted_section(text, title):
    section = text.split("## " + title + "\n", 1)[1].split("\n## ", 1)[0]
    lines = [line[2:] if line.startswith("> ") else "" for line in section.splitlines() if line.startswith(">")]
    _require(bool(lines), "frozen user message missing")
    return " ".join(lines)


def stimulus(directory, stage, baseline):
    text = (Path(directory) / "user-replies.md").read_text()
    round_name, phase = stage.split("-")
    titles = {"a-brief": "Round one starter", "a-write": "Round one decision, after the brief and proposed exact wording",
              "b-brief": "Round two starter, new client and server session", "b-write": "Round two decision, after its independent brief"}
    message = quoted_section(text, titles[stage])
    inputs = baseline["inputs"][round_name]
    replacements = {"[matter folder]": baseline["matter"], "[same matter folder]": baseline["matter"],
        "[incoming-a.docx]": baseline["inputs"]["a"]["source"],
        "[incoming-b.docx]": baseline["inputs"]["b"]["source"],
        "[previous-sent.docx]": str(Path(baseline["matter"]) / "previous-sent.docx"),
        "[counter-a.docx]": baseline["inputs"]["a"]["output"],
        "[actual counter-a.docx]": baseline["inputs"]["a"]["output"],
        "[counter-b.docx]": baseline["inputs"]["b"]["output"]}
    for key, value in replacements.items():
        message = message.replace(key, value)
    if baseline["variant"] == "no-previous" and phase == "brief":
        # Only this explicit previous-file clause is removed. Stored source status
        # still does not grant authority to use that file as a selected prior round.
        message = message.replace(
            ", and the previous document we sent is " + replacements["[previous-sent.docx]"] + ".",
            ". No previous sent document is available for this run.")
    if baseline["variant"] == "unsupported" and phase == "write":
        message += ' Changing the header to "NR-03 approved contract" is also mandatory.'
    _require(inputs["output"] not in (inputs["source"], inputs["previous"]), "output collides with input")
    return message


def prompt_for(directory, stage, baseline):
    user = stimulus(directory, stage, baseline)
    if stage.endswith("write"):
        return user
    # Loading the exact shipped skill and resolving its sole workflow reference is
    # deterministic delivery, not a replacement task or predetermined MCP script.
    parts = ["Use this delivered Veqtor skill and its complete resolved canonical workflow. "
             "The user request follows. Use MCP for matter operations; the observer handles rendering separately."]
    for name in WORKFLOW_FILES:
        raw = (Path(directory) / "workflow" / name).read_text()
        parts.append(f"<delivered-file path={json.dumps(name)}>\n{raw}\n</delivered-file>")
    parts.append("<user-request>\n" + user + "\n</user-request>")
    return "\n\n".join(parts)


def command_for(codex, python, stage, *, model, reasoning_effort, thread_id=None, journal_disabled=False):
    _require(model == MODEL and reasoning_effort in EFFORTS, "NR-03 model/effort outside authorized policy")
    is_write = stage.endswith("write")
    _require(is_write == (thread_id is not None), "resume must identify exactly its own brief session")
    command = [str(Path(codex).absolute()), "exec"] + (["resume"] if is_write else [])
    command += ["--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
        "--model", model, "-c", "model_reasoning_effort=" + json.dumps(reasoning_effort),
        "-c", "project_doc_max_bytes=0",
        "-c", f"mcp_servers.{SERVER}.command=" + json.dumps(python),
        "-c", f"mcp_servers.{SERVER}.args=" + json.dumps(SERVER_ARGS, separators=(",", ":")),
        "-c", f"mcp_servers.{SERVER}.env.VEQTOR_TRACKED_CHANGE_AUTHOR=" + json.dumps(AUTHOR),
        "-c", f"mcp_servers.{SERVER}.env.VEQTOR_DISABLE_DECISION_RECORD=" + json.dumps("1" if journal_disabled else "0")]
    command += [thread_id, "-"] if is_write else ["-"]
    return command


def capture(directory, stage, codex, *, model, reasoning_effort):
    from check_next_round_acceptance import load_baseline, load_stage
    directory = Path(directory).absolute()
    baseline, installation = load_baseline(directory)
    _require(stage in STAGES, "unknown capture stage")
    selection = dict(model=model, reasoning_effort=reasoning_effort)
    _require(baseline["client_selection"] == selection, "selection differs from pre-run baseline")
    round_name, phase = stage.split("-")
    if round_name == "b":
        _require(baseline["variant"] == "main", "second round supported for main scenario only")
        _require((directory / "round-b-baseline.json").is_file(), "freeze second input before capture")
    thread_id = None
    parent_receipt = None
    if phase == "write":
        _require(baseline["variant"] in {"main", "unsupported", "existing-output", "journal-unavailable",
                 "document-injection", "position-injection"}, "variant requires observed missing decision; do not inject main approval")
        parent = load_stage(directory, f"{round_name}-brief", baseline, installation)
        thread_id = parent["thread"]
        parent_receipt = _file_sha256(str(directory / f"{round_name}-brief.receipt.json"))
    prompt = prompt_for(directory, stage, baseline)
    command = command_for(codex, installation["python"], stage, model=model, reasoning_effort=reasoning_effort,
                          thread_id=thread_id, journal_disabled=baseline["variant"] == "journal-unavailable")
    # Exclusive creation prevents overwriting earlier evidence or accidental reruns.
    private_write(directory / f"{stage}.prompt.txt", prompt.encode())
    before = state(baseline["matter"])
    started = time.time_ns()
    result = subprocess.run(command, input=prompt.encode(), cwd=directory / f"client-{round_name}",
                            capture_output=True, check=False)
    finished = time.time_ns()
    private_write(directory / f"{stage}.jsonl", result.stdout)
    private_write(directory / f"{stage}.stderr.txt", result.stderr)
    write_json(directory / f"{stage}.receipt.json", dict(
        schema_version="veqtor_next_round_capture.v1", stage=stage, command=command,
        cwd=str(directory / f"client-{round_name}"), client_selection=selection,
        baseline_sha256=_file_sha256(str(directory / "baseline.json")),
        second_baseline_sha256=_file_sha256(str(directory / "round-b-baseline.json")) if round_name == "b" else None,
        installation_sha256=_file_sha256(str(directory / "installation.json")),
        prompt_sha256=_file_sha256(str(directory / f"{stage}.prompt.txt")),
        events_sha256=_file_sha256(str(directory / f"{stage}.jsonl")),
        workflow_sha256=baseline["workflow_sha256"],
        parent_receipt_sha256=parent_receipt, resumed_thread_id=thread_id,
        started_ns=started, finished_ns=finished, exit_code=result.returncode, before=before, after=state(baseline["matter"])))
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument("--codex", required=True)
    parser.add_argument("--model", choices=(MODEL,), required=True)
    parser.add_argument("--reasoning-effort", choices=EFFORTS, required=True)
    args = parser.parse_args()
    return capture(args.bundle, args.stage, args.codex, model=args.model, reasoning_effort=args.reasoning_effort)


if __name__ == "__main__":
    raise SystemExit(main())
