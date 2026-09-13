# SPDX-License-Identifier: Apache-2.0
"""Capture one gated native NR-02 session; never modify permanent MCP settings."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time

from check_position_acceptance import SESSIONS, baseline, decode, restart_prompt, current_document_prompt, sha


def private_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def docx_hashes(expected):
    result = {}
    for folder in expected["folders"].values():
        for path in sorted(Path(folder).rglob("*")):
            if path.is_file() and path.suffix.lower() == ".docx":
                result[str(path)] = sha(path.read_bytes())
    return result


def journal_hash(expected):
    path = Path(expected["folders"]["moved"]) / ".veqtor" / "decision-records.jsonl"
    return sha(path.read_bytes()) if path.exists() else None


def capture(directory, name, codex, prompt):
    directory = Path(directory).absolute()
    raw = (directory / "baseline.json").read_bytes()
    expected = baseline(decode(raw))
    installed_raw = (directory / "installation.json").read_bytes()
    installation = decode(installed_raw)
    if name == "restart":
        prompt = restart_prompt(expected)
    elif name == "moved":
        prompt = current_document_prompt(expected)
    assert name in SESSIONS and isinstance(prompt, str) and prompt
    # A distinct server name and per-run command override leave user settings intact.
    command = [str(Path(codex).absolute()), "exec", "--json", "--skip-git-repo-check", "--ignore-user-config", "--ephemeral",
        "-c", "mcp_servers.veqtor_nr02.command=" + json.dumps(installation["python"]),
        "-c", 'mcp_servers.veqtor_nr02.args=["-I","-m","veqtor_mcp.server"]',
        "-c", 'mcp_servers.veqtor_nr02.env.VEQTOR_TRACKED_CHANGE_AUTHOR="Veqtor Acceptance"',
        "-c", 'mcp_servers.veqtor_nr02.env.VEQTOR_DISABLE_DECISION_RECORD=' + json.dumps("1" if name == "journal_disabled" else "0"),
        "-"]
    private_write(directory / f"{name}.prompt.txt", prompt.encode())
    before = docx_hashes(expected)
    journal_before = journal_hash(expected)
    start = time.time_ns()
    result = subprocess.run(command, input=prompt.encode(), cwd=directory, capture_output=True, check=False)
    finish = time.time_ns()
    private_write(directory / f"{name}.jsonl", result.stdout)
    private_write(directory / f"{name}.stderr.txt", result.stderr)
    receipt = {"baseline_sha256": sha(raw), "installation_sha256": sha(installed_raw),
        "events_sha256": sha(result.stdout), "prompt_sha256": sha(prompt.encode()), "exit_code": result.returncode,
        "started_ns": start, "finished_ns": finish, "command": command, "server_python": installation["python"],
        "docx_before": before, "docx_after": docx_hashes(expected),
        "journal_before": journal_before, "journal_after": journal_hash(expected)}
    private_write(directory / f"{name}.receipt.json", json.dumps(receipt, sort_keys=True).encode())
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--name", choices=sorted(SESSIONS), required=True)
    parser.add_argument("--codex", required=True, help="Working native Codex executable, not a shim")
    parser.add_argument("--prompt-file", type=Path)
    args = parser.parse_args()
    if args.name not in {"restart", "moved"} and args.prompt_file is None:
        parser.error("--prompt-file is required except for the fixed restart/moved prompts")
    return capture(args.bundle, args.name, args.codex, args.prompt_file.read_text() if args.prompt_file else None)


if __name__ == "__main__":
    raise SystemExit(main())
