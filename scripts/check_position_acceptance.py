# SPDX-License-Identifier: Apache-2.0
"""Fail-closed NR-02 native Codex evidence, separate from NR-00/NR-01 profiles.

All baseline wording and alternatives are predeclared. This checker independently
constructs complete expected positions/history; it never treats a successful
save result, compact journal, prompt or final prose as the expected readback.
Raw logs/receipts/wording are private evidence, not authentication of their author.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys

import jsonschema

from veqtor_mcp._positions_contract import CONTENT, POSITION, RESULT

TOOLS = {"read_deal_positions", "mutate_deal_positions"}
BASELINE_SCHEMA = "veqtor_position_baseline.v1"
SESSIONS = {"save", "confirm", "update", "restart", "withdraw", "moved", "changed", "missing",
            "journal_disabled", "journal_corrupt", "other", "conflict_a", "conflict_b", "conflict_final",
            "first_a", "first_b", "first_final", "retry", "copy_independent"}
_RESULT = jsonschema.Draft202012Validator(RESULT)


class EvidenceError(ValueError):
    pass


def require(value, message):
    if not value:
        raise EvidenceError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def decode(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, RecursionError):
        raise EvidenceError("invalid JSON evidence") from None


def baseline(value):
    require(isinstance(value, dict) and set(value) == {"schema_version", "producer", "folders",
        "initial_positions", "updated_content", "confirmation_statement", "conflict_contents", "source_files", "selected_current_file"},
        "baseline fields differ")
    require(value["schema_version"] == BASELINE_SCHEMA, "baseline schema differs")
    require(set(value["producer"]) == {"name", "version", "build"} and value["producer"]["name"] == "veqtor-mcp"
        and value["producer"]["version"] == "0.4.2.dev0"
        and re.fullmatch(r"source-snapshot-v1-sha256:[0-9a-f]{64}", value["producer"]["build"]), "producer differs")
    folders = value["folders"]
    require(set(folders) == {"original", "moved", "conflict", "first", "other"}
        and len(set(folders.values())) == 5 and all(isinstance(p, str) and Path(p).is_absolute()
            for p in folders.values()), "five distinct explicit matter folders required")
    rows = value["initial_positions"]
    require(isinstance(rows, list) and len(rows) == 5 and all(jsonschema.Draft202012Validator(POSITION).is_valid(p)
        and p["version"] == 1 and p["confirmation"] is None and p["lifecycle"] == "active" for p in rows),
        "five complete predeclared initial positions required")
    ids = [p["position_id"] for p in rows]
    require(ids == sorted(set(ids)), "initial IDs must be distinct and ordered")
    require(any(p["content"]["fallback"] and p["content"]["fallback_conditions"] for p in rows)
        and any(p["content"]["related_position_ids"] for p in rows)
        and rows[3]["content"]["business_decision"] == "pending"
        and rows[4]["content"]["content_origin"] == "model_proposal", "required five-position scenario absent")
    for row in rows:
        require(set(row["content"]["related_position_ids"]) <= set(ids), "baseline links escape matter")
    require(jsonschema.Draft202012Validator(CONTENT).is_valid(value["updated_content"])
        and value["updated_content"]["desired_outcome"] != rows[0]["content"]["desired_outcome"],
        "predeclared substantive content update absent")
    require(isinstance(value["confirmation_statement"], str) and 0 < len(value["confirmation_statement"]) <= 2000,
        "confirmation assertion absent")
    alternatives = value["conflict_contents"]
    require(isinstance(alternatives, list) and len(alternatives) == 2 and alternatives[0] != alternatives[1]
        and all(jsonschema.Draft202012Validator(CONTENT).is_valid(c) for c in alternatives),
        "independent conflict alternatives absent")
    sources = value["source_files"]
    require(isinstance(sources, dict) and sources and all(not Path(n).is_absolute() and ".." not in Path(n).parts
        and re.fullmatch(r"[0-9a-f]{64}", h) for n, h in sources.items()), "synthetic source baseline absent")
    bindings = [s for row in rows for s in row["content"]["sources"]]
    require(bindings and any(s["reference"] is not None for s in bindings)
        and all(sources.get(s["path"]) == s["file_sha256"] for s in bindings), "source bindings lack independent bytes")
    require(value["selected_current_file"] in sources and value["selected_current_file"] not in {s["path"] for s in bindings},
        "explicitly selected other current document absent")
    return value


def native_calls(events, producer):
    """Require paired native MCP envelopes and complete structured/text identity."""
    thread = None
    started = ended = False
    pending, seen, calls = {}, set(), []
    for index, event in enumerate(events):
        require(isinstance(event, dict) and not ended, "invalid or trailing native event")
        kind = event.get("type")
        if kind == "thread.started":
            require(thread is None and not started and isinstance(event.get("thread_id"), str), "thread identity absent")
            thread = event["thread_id"]
        elif kind == "turn.started":
            require(thread and not started, "invalid turn start")
            started = True
        elif kind == "turn.completed":
            require(started and not pending, "unfinished native calls")
            ended = True
        else:
            require(started and kind in {"item.started", "item.completed"}, "non-native event or failed turn")
            item = event.get("item", {})
            require(item.get("type") in {"agent_message", "reasoning", "mcp_tool_call"}, "non-MCP action in native run")
            if item["type"] != "mcp_tool_call":
                continue
            identity = {key: item.get(key) for key in ("server", "tool", "arguments")}
            require(identity["server"] == "veqtor_nr02" and identity["tool"] in TOOLS
                and isinstance(identity["arguments"], dict), "unexpected MCP target")
            item_id = item.get("id")
            require(isinstance(item_id, str) and item_id not in seen, "duplicate native call")
            if kind == "item.started":
                require(not pending and item_id not in pending and item.get("status") == "in_progress"
                    and item.get("result") is None and item.get("error") is None, "bad call start")
                pending[item_id] = identity, index
                continue
            prior = pending.pop(item_id, None)
            require(prior is not None and prior[0] == identity, "unpaired native completion")
            seen.add(item_id)
            result = item.get("result")
            call = {**identity, "started_at": prior[1], "completed_at": index}
            failed = item.get("status") == "failed" or (isinstance(result, dict) and
                (result.get("is_error") or result.get("isError")))
            if failed:
                require(identity["tool"] == "mutate_deal_positions", "unexpected failed read")
                require(not isinstance(result, dict) or result.get("structured_content") is None,
                    "successful structured payload substituted for refusal")
                # Require the real transport refusal text; never accept a client explanation.
                parts = result.get("content", []) if isinstance(result, dict) else []
                texts = [part.get("text", "") for part in parts if part.get("type") == "text"]
                error = item.get("error")
                if isinstance(error, dict):
                    texts.append(error.get("message", ""))
                elif isinstance(error, str):
                    texts.append(error)
                require(any(re.search(r"\brevision_conflict: (?:operation refused|deal-position operation refused)\b", t)
                    for t in texts), "native revision-conflict refusal absent")
                call["error_code"] = "revision_conflict"
            else:
                require(item.get("status") == "completed" and item.get("error") is None
                    and isinstance(result, dict), "native success absent")
                payload, content = result.get("structured_content"), result.get("content")
                require(isinstance(payload, dict) and isinstance(content, list) and len(content) == 1
                    and content[0].get("type") == "text" and decode(content[0].get("text")) == payload,
                    "full matching structured/text readback absent")
                require(_RESULT.is_valid(payload) and payload["producer"] == producer,
                    "native schema or installed producer differs")
                call["payload"] = payload
            calls.append(call)
    require(ended and calls and thread, "completed native session absent")
    session_ids = {c["payload"]["server_session_id"] for c in calls if "payload" in c}
    require(len(session_ids) == 1, "one identifiable server process per native session required")
    return thread, next(iter(session_ids)), calls


def _read(call, folder, positions, history, *, revision=None, matter=None, source_status="same_bytes"):
    require(call["tool"] == "read_deal_positions" and call["arguments"] == {
        "folder": folder, "include_history": True, "check_sources": True}, "full native history read absent or wrong matter")
    payload = call.get("payload", {})
    require(payload.get("positions") == positions and payload.get("history") == history
        and payload.get("history_included") is True, "complete predeclared values/history differ")
    require(payload.get("state") == ("initialized" if positions else "uninitialized"), "storage state differs")
    if positions:
        require(isinstance(payload.get("revision"), str) and isinstance(payload.get("matter_id"), str), "store identity absent")
    else:
        require(payload.get("revision") is None and payload.get("matter_id") is None, "uninitialized identity differs")
    if revision is not None:
        require(payload.get("revision") == revision, "read revision differs")
    if matter is not None:
        require(payload.get("matter_id") == matter, "matter identity differs")
    bindings = {canonical(s): s for row in positions + [h["position"] for h in history] for s in row["content"]["sources"]}
    expected_sources = [{"source": bindings[key], "status": source_status} for key in sorted(bindings)]
    require(payload.get("source_observations") == expected_sources, "full exact source observations differ")
    return payload


def _apply_expected(rows, history, operations):
    """Independent expected-value projection, no product mutation/storage helpers."""
    rows, history = deepcopy(rows), deepcopy(history)
    by_id = {row["position_id"]: row for row in rows}
    for operation in operations:
        pid, op = operation["position_id"], operation["op"]
        if op == "create":
            row = {"position_id": pid, "version": 1, "content": deepcopy(operation["content"]),
                   "confirmation": None, "lifecycle": "active"}
        else:
            row = deepcopy(by_id[pid])
            if op == "update":
                row.update(content=deepcopy(operation["content"]), confirmation=None, version=row["version"] + 1)
            elif op == "withdraw":
                row["lifecycle"] = "withdrawn"
            elif op == "confirm":
                row["confirmation"] = {"version": row["version"], "statement": operation["statement"],
                    "basis": "client_asserted_user_confirmation"}
        by_id[pid] = row
        history.append({"sequence": len(history) + 1, "operation": op, "position": deepcopy(row)})
    return [by_id[key] for key in sorted(by_id)], history


def _mutation(call, folder, revision, operations, rows):
    require(call["tool"] == "mutate_deal_positions" and call["arguments"] == {
        "folder": folder, "expected_revision": revision, "operations": operations}, "native mutation differs from predeclared operation")
    payload = call.get("payload", {})
    require(payload.get("positions") == rows and payload.get("revision") != revision
        and payload.get("state") == "initialized" and payload.get("history") == []
        and payload.get("history_included") is False, "save result is incomplete or unchanged")
    bindings = {canonical(s): s for row in rows for s in row["content"]["sources"]}
    require(payload.get("source_observations") == [{"source": bindings[k], "status": "not_checked"}
        for k in sorted(bindings)], "save source observations differ")
    return payload


def validate_evidence(events_by_name, expected):
    expected = baseline(expected)
    require(set(events_by_name) == SESSIONS, "required native sessions missing or substituted")
    decoded = {name: native_calls(events, expected["producer"]) for name, events in events_by_name.items()}
    require(len({row[0] for row in decoded.values()}) == len(SESSIONS), "fresh native client sessions absent")
    require(len({row[1] for row in decoded.values()}) == len(SESSIONS), "fresh native server processes absent")
    runs = {name: row[2] for name, row in decoded.items()}
    folders = expected["folders"]
    initial = expected["initial_positions"]
    creates = [{"op": "create", "position_id": row["position_id"], "content": row["content"]} for row in initial]
    rows, history = _apply_expected([], [], creates)
    save = runs["save"]
    require(len(save) == 3, "save needs absent read, exact batch and full fresh read")
    _read(save[0], folders["original"], [], [])
    payload = _mutation(save[1], folders["original"], None, creates, rows)
    _read(save[2], folders["original"], rows, history, revision=payload["revision"], matter=payload["matter_id"])
    revision, matter = payload["revision"], payload["matter_id"]

    def transition(name, operations):
        nonlocal rows, history, revision
        calls = runs[name]
        require(len(calls) == 2, "transition needs mutation followed by full native read")
        rows, history = _apply_expected(rows, history, operations)
        payload = _mutation(calls[0], folders["original"], revision, operations, rows)
        require(payload["matter_id"] == matter, "mutation changed matter identity")
        revision = payload["revision"]
        _read(calls[1], folders["original"], rows, history, revision=revision, matter=matter)

    transition("confirm", [{"op": "confirm", "position_id": initial[i]["position_id"], "expected_version": 1,
        "user_confirmed": True, "statement": expected["confirmation_statement"]} for i in (0, 3)])
    transition("update", [{"op": "update", "position_id": initial[0]["position_id"], "expected_version": 1,
        "content": expected["updated_content"]}])
    require(len(runs["restart"]) == 1, "restart must independently load context in one fresh read")
    _read(runs["restart"][0], folders["original"], rows, history, revision=revision, matter=matter)
    transition("withdraw", [{"op": "withdraw", "position_id": initial[4]["position_id"], "expected_version": 1}])
    moved_rows, moved_history = deepcopy(rows), deepcopy(history)
    for name, status in [("moved", "same_bytes"), ("changed", "changed"), ("missing", "unavailable")]:
        require(len(runs[name]) == 1, "relocation/source/provenance stage must reread full state")
        _read(runs[name][0], folders["moved"], rows, history, revision=revision, matter=matter, source_status=status)
    moved_revision = revision
    for version, name in [(1, "journal_disabled"), (2, "journal_corrupt")]:
        calls = runs[name]
        require(len(calls) == 2, "provenance independence requires a commit and full read")
        operations = [{"op": "update", "position_id": initial[2]["position_id"], "expected_version": version,
                       "content": initial[2]["content"]}]
        require(not initial[2]["content"]["sources"], "provenance test position must be source-free")
        moved_rows, moved_history = _apply_expected(moved_rows, moved_history, operations)
        out = _mutation(calls[0], folders["moved"], moved_revision, operations, moved_rows)
        moved_revision = out["revision"]
        _read(calls[1], folders["moved"], moved_rows, moved_history, revision=moved_revision,
              matter=matter, source_status="unavailable")
    require(len(runs["other"]) == 1, "independent empty matter read absent")
    _read(runs["other"][0], folders["other"], [], [])

    # Conflict folder is an idle byte-for-byte copy made before source alteration.
    winner = None
    for index, name in enumerate(("conflict_a", "conflict_b")):
        calls = runs[name]
        require(len(calls) == 2, "conflict client must read shared revision then attempt one mutation")
        _read(calls[0], folders["conflict"], rows, history, revision=revision, matter=matter)
        operations = [{"op": "update", "position_id": initial[1]["position_id"], "expected_version": 1,
                       "content": expected["conflict_contents"][index]}]
        new_rows, new_history = _apply_expected(rows, history, operations)
        require(calls[1]["arguments"] == {"folder": folders["conflict"], "expected_revision": revision,
            "operations": operations}, "conflict attempt is stale/wrong operation or wrong matter")
        if "payload" in calls[1]:
            require(winner is None, "both concurrent writes succeeded")
            out = _mutation(calls[1], folders["conflict"], revision, operations, new_rows)
            winner = new_rows, new_history, out["revision"]
        else:
            require(calls[1].get("error_code") == "revision_conflict", "loser conflict absent")
    require(winner is not None and len(runs["conflict_final"]) == 1, "conflict winner/readback absent")
    _read(runs["conflict_final"][0], folders["conflict"], winner[0], winner[1], revision=winner[2], matter=matter)
    require(len(runs["copy_independent"]) == 1, "post-conflict independent-copy reread absent")
    _read(runs["copy_independent"][0], folders["moved"], moved_rows, moved_history,
          revision=moved_revision, matter=matter, source_status="unavailable")
    # Repeating a possibly completed request must conflict and preserve the winner.
    retry = runs["retry"]
    require(len(retry) == 2 and retry[0].get("error_code") == "revision_conflict", "unknown-result retry conflict absent")
    winning_attempt = next(runs[n][1] for n in ("conflict_a", "conflict_b") if "payload" in runs[n][1])
    require(retry[0]["arguments"] == winning_attempt["arguments"], "retry was not the exact old request")
    _read(retry[1], folders["conflict"], winner[0], winner[1], revision=winner[2], matter=matter)

    first_winner = None
    for index, name in enumerate(("first_a", "first_b")):
        calls = runs[name]
        require(len(calls) == 2, "first-creation race lacks read and attempt")
        _read(calls[0], folders["first"], [], [])
        operations = deepcopy(creates)
        operations[1]["content"] = expected["conflict_contents"][index]
        first_rows, first_history = _apply_expected([], [], operations)
        require(calls[1]["arguments"] == {"folder": folders["first"], "expected_revision": None,
            "operations": operations}, "first-creation attempt differs")
        if "payload" in calls[1]:
            require(first_winner is None, "both initial creations succeeded")
            out = _mutation(calls[1], folders["first"], None, operations, first_rows)
            first_winner = first_rows, first_history, out["revision"], out["matter_id"]
        else:
            require(calls[1].get("error_code") == "revision_conflict", "first-creation conflict absent")
    require(first_winner is not None and first_winner[3] != matter and len(runs["first_final"]) == 1,
        "independent first-created matter/readback absent")
    _read(runs["first_final"][0], folders["first"], first_winner[0], first_winner[1],
          revision=first_winner[2], matter=first_winner[3])
    return {"status": "passed", "profile": "nr02_native_positions.v1", "native_sessions": len(SESSIONS),
            "full_predeclared_readback": True, "history_verified": True, "fresh_client_and_server": True,
            "conflicts_and_initialization": True, "move_and_sources": True,
            "log_authenticity": False}


def check_bundle(directory):
    """Validate capture receipts as well as semantic native evidence.

    The capture command is independently recorded by the launcher, not the model.
    File permissions, initial/source manipulations and failure injection are
    externally observed setup, never authorized product writes to DOCX.
    """
    directory = Path(directory)
    raw = (directory / "baseline.json").read_bytes()
    expected = baseline(decode(raw))
    installation = decode((directory / "installation.json").read_bytes())
    require(installation.get("schema_version") == "veqtor_position_install.v1"
        and installation.get("producer") == expected["producer"] and installation.get("source_files"),
        "exact installed-wheel/source report absent")
    from check_position_install import verify
    require(verify(installation["source_root"], installation["commit"], installation["tree"],
        installation["wheel"], installation["sdist"], installation["python"]) == installation,
        "installed candidate revalidation differs")
    require(re.fullmatch(r"[0-9a-f]{40}", installation.get("commit", ""))
        and re.fullmatch(r"[0-9a-f]{40}", installation.get("tree", "")), "candidate commit/tree absent")
    runs, receipts = {}, {}
    for name in sorted(SESSIONS):
        receipt = decode((directory / f"{name}.receipt.json").read_bytes())
        data = (directory / f"{name}.jsonl").read_bytes()
        prompt = (directory / f"{name}.prompt.txt").read_bytes()
        require(receipt.get("baseline_sha256") == sha(raw) and receipt.get("events_sha256") == sha(data)
            and receipt.get("prompt_sha256") == sha(prompt) and receipt.get("exit_code") == 0,
            "capture receipt does not bind baseline/events/prompt")
        require(receipt.get("installation_sha256") == sha((directory / "installation.json").read_bytes()),
            "capture used another installation")
        require(type(receipt.get("started_ns")) is int and type(receipt.get("finished_ns")) is int
            and receipt["finished_ns"] > receipt["started_ns"] > (directory / "baseline.json").stat().st_mtime_ns,
            "baseline not predeclared before capture")
        command = receipt.get("command")
        require(isinstance(command, list) and command and Path(command[0]).is_absolute()
            and receipt.get("server_python") == installation["python"], "native isolated launch receipt absent")
        expected_command = [command[0], "exec", "--json", "--skip-git-repo-check", "--ignore-user-config", "--ephemeral",
            "-c", "mcp_servers.veqtor_nr02.command=" + json.dumps(installation["python"]),
            "-c", 'mcp_servers.veqtor_nr02.args=["-I","-m","veqtor_mcp.server"]',
            "-c", 'mcp_servers.veqtor_nr02.env.VEQTOR_TRACKED_CHANGE_AUTHOR="Veqtor Acceptance"',
            "-c", 'mcp_servers.veqtor_nr02.env.VEQTOR_DISABLE_DECISION_RECORD=' + json.dumps("1" if name == "journal_disabled" else "0"), "-"]
        require(command == expected_command, "exact isolated producer command differs or has extra overrides")
        if name == "restart":
            require(prompt.decode() == restart_prompt(expected), "restart prompt carried prior dialogue/content")
        if name == "moved":
            require(prompt.decode() == current_document_prompt(expected), "explicit alternate current-document scenario absent")
        require(receipt.get("docx_before") == receipt.get("docx_after")
            and isinstance(receipt.get("docx_before"), dict), "native session modified DOCX")
        if name == "journal_disabled":
            require('mcp_servers.veqtor_nr02.env.VEQTOR_DISABLE_DECISION_RECORD="1"' in command,
                "provenance was not disabled in the native process")
        else:
            require('mcp_servers.veqtor_nr02.env.VEQTOR_DISABLE_DECISION_RECORD="0"' in command,
                "native provenance configuration differs")
        if name == "journal_corrupt":
            require(receipt.get("journal_before") == receipt.get("journal_after") == sha(b"NR-02 synthetic corrupt journal\n"),
                "corrupt journal setup/independence absent")
        receipts[name] = receipt
        runs[name] = [decode(line) for line in data.splitlines() if line.strip()]
    order = ["save", "confirm", "update", "restart", "withdraw", "moved", "changed", "missing", "journal_disabled", "journal_corrupt"]
    require(all(receipts[a]["finished_ns"] < receipts[b]["started_ns"] for a, b in zip(order, order[1:])),
        "native state transitions out of order")
    for a, b, final in [("conflict_a", "conflict_b", "conflict_final"), ("first_a", "first_b", "first_final")]:
        require(max(receipts[a]["started_ns"], receipts[b]["started_ns"]) <
            min(receipts[a]["finished_ns"], receipts[b]["finished_ns"]), "concurrent processes did not overlap")
        require(max(receipts[a]["finished_ns"], receipts[b]["finished_ns"]) < receipts[final]["started_ns"],
            "conflict final read preceded attempts")
    require(receipts["conflict_final"]["finished_ns"] < receipts["retry"]["started_ns"], "retry preceded final read")
    require(max(receipts["conflict_final"]["finished_ns"], receipts["journal_corrupt"]["finished_ns"]) <
        receipts["copy_independent"]["started_ns"], "independent-copy reread preceded changes")
    bound_names = {s["path"] for row in expected["initial_positions"] for s in row["content"]["sources"]}
    for name in SESSIONS:
        before = receipts[name]["docx_before"]
        if name in {"save", "confirm", "update", "restart", "withdraw"}:
            folder = expected["folders"]["original"]
            require(all(before.get(str(Path(folder) / n)) == h for n, h in expected["source_files"].items()),
                "original source evidence differs")
        elif name in {"moved", "changed", "missing", "journal_disabled", "journal_corrupt"}:
            folder = expected["folders"]["moved"]
            require(not any(Path(p).is_relative_to(expected["folders"]["original"]) for p in before),
                "old matter was not moved")
            for n, h in expected["source_files"].items():
                path = str(Path(folder) / n)
                if name == "moved" or n not in bound_names:
                    require(before.get(path) == h, "moved or alternate current document differs")
                elif name == "changed":
                    require(path in before and before[path] != h, "changed-source setup absent")
                else:
                    require(path not in before, "missing-source setup absent")
    # The installed files must still match the captured exact source manifest.
    actual = {}
    for package, root in installation["installed_roots"].items():
        for path in Path(root).rglob("*.py"):
            actual[f"{package}/{path.relative_to(root).as_posix()}"] = sha(path.read_bytes())
    require(actual == installation["source_files"], "installed sources drifted")
    report = validate_evidence(runs, expected)
    report.update(commit=installation["commit"], tree=installation["tree"], producer=installation["producer"])
    return report


def restart_prompt(expected):
    return ("Use only native veqtor_nr02 MCP. Call read_deal_positions once with " +
        json.dumps({"folder": expected["folders"]["original"], "include_history": True, "check_sources": True}, sort_keys=True)
        + ". Do not use shell, files or other tools. Return a brief acknowledgement after the call.\n")


def current_document_prompt(expected):
    return ("The user explicitly selects " + json.dumps(str(Path(expected["folders"]["moved"]) / expected["selected_current_file"]))
        + " as the current contract. Use only native veqtor_nr02 MCP. Call read_deal_positions once with "
        + json.dumps({"folder": expected["folders"]["moved"], "include_history": True, "check_sources": True}, sort_keys=True)
        + ". Do not rebind saved documentary references, change data, or use shell/files/other tools.\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(check_bundle(args.bundle), sort_keys=True))
    except (EvidenceError, OSError, KeyError, TypeError, ValueError):
        print("NR-02 native evidence rejected", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
