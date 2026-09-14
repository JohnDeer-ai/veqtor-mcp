# SPDX-License-Identifier: Apache-2.0
"""Check NR-03 native workflow evidence; human meaning/visual gates stay separate.

No generated transcript is native evidence. This checker checks consistency of
actual receipts, immutable inputs, native structured results and actual DOCX; it
cannot authenticate an operator or log, or decide legal equivalence.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import jsonschema
from lxml import etree

from veqtor_docx import extract_redlines, inspect_document
from veqtor_mcp import _positions_contract

import check_paragraph_acceptance as paragraphs
from check_codex_acceptance import (
    EvidenceError, REQUIRED_TOOLS, _digest, _file_sha256, _json, _read, _require,
)
from capture_next_round_session import command_for, prompt_for
from check_next_round_journal import validate_document_and_journal
from nr03_delivery import validate_export_limits
from nr03_scenario import (
    AUTHOR, C2, C3, CONFIRM, EFFORTS, IDS, MODEL, ORACLE_FILES, SELECTED, SERVER, VERSION, WORKFLOW_FILES,
    assert_oracle, document, edit_specs, intents, source_manifest, texts,
)
from prepare_next_round_acceptance import SCHEMA, installed, read_json

POSITION_RESULT = jsonschema.Draft202012Validator(_positions_contract.RESULT)
ALLOWED = REQUIRED_TOOLS | {"read_deal_positions", "mutate_deal_positions"}


def parse_native(events, producer, *, require_tool_calls=True):
    """Validate a complete turn; calls retain completion order and dispatch times.

    Observation-only continuations may contain a user-facing refusal/question
    without tools. Main success evidence still requires tools by default.
    """
    thread = None
    started = done = False
    pending, seen, calls, messages = {}, set(), [], []
    for index, event in enumerate(events):
        _require(isinstance(event, dict) and not done, "events after turn completion or malformed event")
        kind = event.get("type")
        if kind == "thread.started":
            _require(thread is None and not started and isinstance(event.get("thread_id"), str)
                     and event["thread_id"], "invalid client session start")
            thread = event["thread_id"]
            continue
        if kind == "turn.started":
            _require(thread and not started, "invalid native turn start")
            started = True
            continue
        if kind == "turn.completed":
            _require(started and not pending, "incomplete native calls")
            done = True
            continue
        item = event.get("item")
        _require(kind in {"item.started", "item.completed"} and isinstance(item, dict), "unsupported native event")
        if not started and item.get("type") == "error":
            _require(kind == "item.completed" and item.get("message", "").startswith("Under-development features enabled:"),
                     "native startup error")
            continue
        _require(started and item.get("type") in {"mcp_tool_call", "agent_message", "reasoning"},
                 "non-MCP action in native workflow")
        if item["type"] != "mcp_tool_call":
            if kind == "item.completed" and item["type"] == "agent_message":
                _require(isinstance(item.get("text"), str) and item["text"].strip(), "empty native message")
                messages.append(dict(event_index=index, text=item["text"]))
            continue
        ident = item.get("id")
        _require(isinstance(ident, str) and ident and ident not in seen
                 and item.get("server") == SERVER and item.get("tool") in ALLOWED
                 and isinstance(item.get("arguments"), dict), "invalid native call identity")
        identity = {k: item[k] for k in ("server", "tool", "arguments")}
        if kind == "item.started":
            _require(ident not in pending and item.get("status") == "in_progress"
                     and item.get("result") is None and item.get("error") is None, "bad native call start")
            pending[ident] = (identity, index)
            continue
        prior = pending.pop(ident, None)
        _require(prior is not None and prior[0] == identity, "native completion differs from start")
        seen.add(ident)
        call = dict(identity, started_at=prior[1], completed_at=index, id=ident)
        result = item.get("result")
        if item.get("status") == "failed" or (isinstance(result, dict) and (result.get("isError") or result.get("is_error"))):
            call.update(failed=True, error=deepcopy(item.get("error")), result=deepcopy(result))
            calls.append(call)
            continue
        _require(item.get("status") == "completed" and item.get("error") is None and isinstance(result, dict),
                 "unknown native result status")
        payload, content = result.get("structured_content"), result.get("content")
        _require(isinstance(payload, dict) and isinstance(content, list) and len(content) == 1
                 and content[0].get("type") == "text" and isinstance(content[0].get("text"), str)
                 and _json(content[0]["text"]) == payload, "native full structured/text payload missing or differs")
        _require(payload.get("producer") == producer, "native producer differs from installed candidate")
        validator = POSITION_RESULT if item["tool"] in {"read_deal_positions", "mutate_deal_positions"} else paragraphs._RESULT_VALIDATORS.get(item["tool"])
        _require(validator is None or validator.is_valid(payload), "native result violates public contract")
        call.update(failed=False, payload=payload)
        calls.append(call)
    _require(done and messages, "native turn lacks completion or final user-facing content")
    _require(calls or not require_tool_calls, "native turn lacks required tool calls")
    _require(messages[-1]["event_index"] > max((c["completed_at"] for c in calls), default=-1),
             "no result message after native work")
    return thread, calls, messages


def load_baseline(directory):
    directory = Path(directory).absolute()
    b = read_json(directory / "baseline.json")
    _require(set(b) == {"schema_version", "workflow_version", "prepared_ns", "variant", "matter", "client_selection",
        "installation_sha256", "workflow_sha256", "oracle_source_sha256", "oracle", "initial_state", "store", "author", "inputs"}
        and b["schema_version"] == SCHEMA and b["workflow_version"] == VERSION and b["author"] == AUTHOR,
        "unsupported workflow baseline")
    _require(type(b["prepared_ns"]) is int and b["prepared_ns"] > 0, "preparation timing absent")
    _require(b["client_selection"].get("model") == MODEL and b["client_selection"].get("reasoning_effort") in EFFORTS
             and set(b["client_selection"]) == {"model", "reasoning_effort"}, "model/effort selection differs")
    assert_oracle(b["oracle"])
    report = read_json(directory / "installation.json")
    _require(_file_sha256(str(directory / "installation.json")) == b["installation_sha256"], "installation receipt drift")
    installed(report)
    _require(b["workflow_sha256"] == source_manifest(report["source_root"], WORKFLOW_FILES)
             == source_manifest(directory / "workflow", WORKFLOW_FILES), "different delivered workflow/skill")
    _require(b["oracle_source_sha256"] == source_manifest(report["source_root"], ORACLE_FILES), "oracle source drift")
    _require(_file_sha256(str(directory / "user-replies.md")) == b["oracle_source_sha256"]["docs/NR03_USER_REPLIES.md"],
             "scripted user stimuli differ")
    matter = directory / "matter"
    _require(b["matter"] == str(matter), "wrong matter root")
    for r in ("a", "b"):
        expected = dict(source=str(matter / f"incoming-{r}.docx"), output=str(matter / f"counter-{r}.docx"),
                        previous=str(matter / ("previous-sent.docx" if r == "a" else "counter-a.docx")))
        if b["variant"] == "no-previous" and r == "a":
            expected["previous"] = None
        _require(b["inputs"][r] == expected, "selected file identities differ")
    for path, sha in b["initial_state"]["docx"].items():
        _require(_file_sha256(path) == sha, "original or preexisting output changed")
    stored = matter / ".veqtor" / "deal-positions.json"
    observed = _file_sha256(str(stored)) if stored.exists() else None
    _require(observed == b["initial_state"]["store_sha256"], "saved store changed during main workflow")
    if b["variant"] == "main":
        originals = {str(matter / filename): hashlib.sha256(document(name)).hexdigest()
                     for filename, name in (("previous-sent.docx", "previous"), ("incoming-a.docx", "incoming-a"))}
        _require(b["initial_state"]["docx"] == originals, "initial complete synthetic DOCX bytes differ from oracle")
        snapshot = read_json(stored)
        _require(snapshot["positions"] == b["store"]["positions"] and snapshot["history"] == b["store"]["history"]
                 and snapshot["revision"] == b["store"]["revision"] and snapshot["matter_id"] == b["store"]["matter_id"],
                 "baseline complete store differs")
        contents = intents(b["initial_state"]["docx"][str(matter / "previous-sent.docx")])
        created = [dict(position_id=pid, version=1, content=c, confirmation=None, lifecycle="active")
                   for pid, c in zip(IDS, contents)]
        confirmed = [dict(p, confirmation=dict(version=1, statement=CONFIRM, basis="client_asserted_user_confirmation"))
                     for p in created]
        history = [dict(sequence=i + 1, operation="create" if i < 5 else "confirm", position=p)
                   for i, p in enumerate(created + confirmed)]
        _require(snapshot["positions"] == confirmed and snapshot["history"] == history
                 and snapshot["revision"] == _digest({k: v for k, v in snapshot.items() if k != "revision"})
                 and jsonschema.Draft202012Validator(_positions_contract.STORE).is_valid(snapshot),
                 "saved full content/confirmation/history differs from independent oracle")
    return b, report


def load_stage(directory, stage, b, installation):
    r, phase = stage.split("-")
    receipt = read_json(directory / f"{stage}.receipt.json")
    expected_fields = {"schema_version", "stage", "command", "cwd", "client_selection", "baseline_sha256",
        "second_baseline_sha256", "installation_sha256", "prompt_sha256", "events_sha256", "workflow_sha256",
        "parent_receipt_sha256", "resumed_thread_id", "started_ns", "finished_ns", "exit_code", "before", "after"}
    _require(set(receipt) == expected_fields and receipt["schema_version"] == "veqtor_next_round_capture.v1"
             and receipt["stage"] == stage and receipt["exit_code"] == 0, "invalid stage receipt")
    for field in ("started_ns", "finished_ns"):
        _require(type(receipt[field]) is int, "stage timing is not an integer")
    _require(b["prepared_ns"] < receipt["started_ns"] < receipt["finished_ns"], "stage precedes frozen inputs")
    prompt = _read(directory / f"{stage}.prompt.txt")
    events = _read(directory / f"{stage}.jsonl")
    _require(prompt == prompt_for(directory, stage, b).encode(), "starter/decision differs or copied prior dialogue/positions")
    _require(receipt["prompt_sha256"] == hashlib.sha256(prompt).hexdigest()
             and receipt["events_sha256"] == hashlib.sha256(events).hexdigest()
             and receipt["baseline_sha256"] == _file_sha256(str(directory / "baseline.json"))
             and receipt["installation_sha256"] == b["installation_sha256"]
             and receipt["workflow_sha256"] == b["workflow_sha256"]
             and receipt["client_selection"] == b["client_selection"], "receipt byte/identity binding differs")
    resumed = receipt["resumed_thread_id"]
    if phase == "brief":
        _require(resumed is None and receipt["parent_receipt_sha256"] is None, "brief reused another conversation")
    else:
        _require(receipt["parent_receipt_sha256"] == _file_sha256(str(directory / f"{r}-brief.receipt.json")),
                 "decision not bound to its own brief")
    command = receipt["command"]
    _require(isinstance(command, list) and command and Path(command[0]).is_absolute(), "native command absent")
    _require(command == command_for(command[0], installation["python"], stage,
             **b["client_selection"], thread_id=resumed, journal_disabled=b["variant"] == "journal-unavailable")
             and receipt["cwd"] == str(directory / f"client-{r}"), "different native launch/resume/configuration")
    if r == "a":
        _require(receipt["second_baseline_sha256"] is None, "unexpected second-round binding")
    else:
        second = read_json(directory / "round-b-baseline.json")
        _require(receipt["second_baseline_sha256"] == _file_sha256(str(directory / "round-b-baseline.json"))
                 and second["prepared_ns"] < receipt["started_ns"], "second input was not frozen before native session")
    decoded = [_json(line) for line in events.decode().splitlines()]
    thread, calls, messages = parse_native(decoded, installation["producer"])
    validate_export_limits(calls)
    if resumed:
        _require(thread == resumed, "write resumed another brief")
    return dict(receipt=receipt, events=decoded, thread=thread, calls=calls, messages=messages)


def full_position_read(call, b):
    _require(not call["failed"] and call["tool"] == "read_deal_positions", "native NR-02 read absent")
    args, result, expected = call["arguments"], call["payload"], b["store"]
    _require(args.get("folder") == b["matter"] and args.get("check_sources") is True,
             "position read lacks selected matter/source checks")
    for key in ("state", "matter_id", "revision", "positions", "source_observations"):
        _require(result[key] == expected[key], "complete saved positions or source observations differ")
    _require(result["history"] == (expected["history"] if args.get("include_history", False) else [])
             and result["history_included"] == args.get("include_history", False), "history read is incomplete")
    _require(result["record_status"] == "disabled" and result["record_id"] is None, "position journal semantics differ")
    return result["server_session_id"]


def exact_read_quote(calls, path, index, text, *, upper=float("inf")):
    sha = _file_sha256(path)
    ref = dict(schema_version="paragraph_ref.v1", ref_type="paragraph", file_sha256=sha,
               part_name="word/document.xml", paragraph_index=index,
               paragraph_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
               reading_mode="accepted_current_v1", container_policy="canonical_body_flow_v1")
    reads = [c for c in calls if not c["failed"] and c["tool"] == "inspect_document"
             and c["arguments"].get("path") == path and c["arguments"].get("mode") == "read"
             and c["arguments"].get("selection") == {"paragraph_ref": ref}
             and c["payload"].get("path") == path and c["payload"].get("file_sha256") == sha
             and c["payload"].get("mode") == "read" and c["payload"].get("selection_kind") == "paragraph"
             and len(c["payload"].get("paragraphs", [])) == 1
             and c["payload"]["paragraphs"][0].get("paragraph_ref") == ref
             and c["payload"]["paragraphs"][0].get("text") == text]
    for c in calls:
        if c["failed"] or c["tool"] != "verify_quote":
            continue
        args, p = c["arguments"], c["payload"]
        if not (args.get("path") == path and args.get("anchor") == ref and args.get("quote") == text
                and args.get("paragraph_projection", "accepted_current_v1") == "accepted_current_v1"
                and p.get("checked_anchor") == ref and p.get("exact") is True and p.get("verdict") == "exact"
                and c["completed_at"] < upper and any(read["completed_at"] < c["started_at"] for read in reads)):
            continue
        projection = dict(schema_version="verified_paragraph_projection.v1", mode="accepted_current_v1",
            projection_status="complete", anchor_reading_mode="accepted_current_v1",
            anchor_paragraph_text_sha256=ref["paragraph_text_sha256"],
            projection_text_sha256=ref["paragraph_text_sha256"], text_length=len(text))
        matches = p.get("matches", [])
        if p.get("checked_projection") != projection or len(matches) != 1:
            continue
        m = matches[0]
        if all(m.get(k) == v for k, v in dict(path=path, part_name="word/document.xml", revision_ids=[],
                side="paragraph_current", paragraph_index=index, paragraph_text_sha256=ref["paragraph_text_sha256"],
                projection_text_sha256=ref["paragraph_text_sha256"], projection_mode="accepted_current_v1").items()):
            return True
    return False


def document_only(events):
    # A derived view for the unchanged NR-01 checker, not a fabricated native log.
    # The raw complete turn has already been checked above, including NR-02 calls.
    return [event for event in events if event.get("item", {}).get("tool") not in
            {"read_deal_positions", "mutate_deal_positions"}]


def actual_texts(path):
    return [paragraphs._text(p) for p in paragraphs._paragraphs(path)[2]]


def expected_edits(source, round_name):
    """One fixture edit choice; its exact addresses also bind authorized targets."""
    rows = inspect_document(source, mode="browse", max_items=100)["paragraphs"]
    refs = {row["paragraph_ref"]["paragraph_index"]: row["paragraph_ref"] for row in rows}
    units = extract_redlines(source)["change_units"]
    edits = []
    for index, kind, old, new in edit_specs(round_name):
        address = {"target": dict(kind="paragraph", paragraph_ref=refs[index])} if kind == "paragraph" else {
            "anchor": next(u["anchor"] for u in units if u["reference"]["paragraph_index"] == index and u["new_text"] == old)}
        edits.append(dict(address, delete_text=old, insert_text=new))
    return edits


def authorize_edits(source, round_name, edits):
    """Bind each input edit to a frozen target and exact full before/after text.

    Substring boundaries are chosen by the client. They are not inferred from
    the output or a claim of legal equivalence. NR-01 subsequently requires the
    actual delete quote, ordered preflight/apply batch and entire proof, exact
    new/prior revisions, full output reads and independent formatting/collateral.
    """
    before, after = (texts(name) for name in (
        "incoming-a" if round_name == "a" else "incoming-b", f"counter-{round_name}"))

    def address(edit):
        return _digest({key: value for key, value in edit.items() if key not in {"delete_text", "insert_text"}})

    targets = {address(edit): index for (index, *_), edit in zip(
        edit_specs(round_name), expected_edits(source, round_name))}
    _require(isinstance(edits, list) and len(edits) == len(targets),
             "authorized complete edit set differs from frozen decisions/targets")
    for edit in edits:
        _require(isinstance(edit, dict), "authorized edit is malformed")
        index = targets.pop(address(edit), None)
        _require(index is not None, "authorized target missing, repeated or different")
        old, new = edit.get("delete_text"), edit.get("insert_text")
        _require(isinstance(old, str) and old and isinstance(new, str), "authorized exact replacement text missing")
        start = before[index].find(old)
        _require(start >= 0 and before[index].find(old, start + 1) < 0,
                 "authorized deletion is not unique in full source paragraph")
        _require(before[index][:start] + new + before[index][start + len(old):] == after[index],
                 "authorized full before/after result differs from frozen decision")


def scope(calls, b, r):
    paths = {b["inputs"][r][k] for k in ("source", "previous", "output")}
    for call in calls:
        _require(call["tool"] != "mutate_deal_positions", "main scenario never authorizes position mutation")
        args = call["arguments"]
        for key in ("path", "source_path", "output_path"):
            _require(key not in args or args[key] in paths, "native action targets an unselected document")
        for key in ("folder", "workspace"):
            _require(key not in args or args[key] == b["matter"], "native action targets another matter")
        if "seed" in args:
            _require(args["seed"].get("path") in paths, "history seed targets another document")


def check_round(directory, r, *, loaded=None):
    directory = Path(directory).absolute()
    b, installation = loaded or load_baseline(directory)
    _require(b["variant"] == "main" and r in {"a", "b"}, "positive checker only accepts main two-round scenario")
    brief, write = [load_stage(directory, f"{r}-{phase}", b, installation) for phase in ("brief", "write")]
    _require(brief["thread"] == write["thread"]
             and brief["receipt"]["finished_ns"] < write["receipt"]["started_ns"], "write does not follow its completed brief")
    _require(not any(c["tool"] in {"preflight_edits", "apply_edits", "mutate_deal_positions"} for c in brief["calls"]),
             "mutation/preflight before the scripted decision")
    scope(brief["calls"] + write["calls"], b, r)
    first_dispatched = min(brief["calls"], key=lambda call: call["started_at"])
    first_read = full_position_read(first_dispatched, b)
    _require(all(first_dispatched["completed_at"] < call["started_at"]
                 for call in brief["calls"] if call["tool"] != "read_deal_positions"),
             "position recovery must finish before document work starts")
    position_reads = [c for c in write["calls"] if c["tool"] == "read_deal_positions"]
    _require(position_reads, "no fresh positions check after user decision")
    write_sessions = {full_position_read(c, b) for c in position_reads}
    pres = [c for c in write["calls"] if c["tool"] == "preflight_edits"]
    _require(len(pres) == 1 and not pres[0]["failed"], "one completed full preflight required")
    _require(any(c["completed_at"] < pres[0]["started_at"] for c in position_reads), "position freshness check follows preflight")
    source, previous, output = (b["inputs"][r][k] for k in ("source", "previous", "output"))
    before_name, previous_name = ("incoming-a", "previous") if r == "a" else ("incoming-b", "counter-a")
    after_name = f"counter-{r}"
    for path, name in ((source, before_name), (previous, previous_name), (output, after_name)):
        _require(actual_texts(path) == texts(name), "complete actual document differs from independent text oracle")
    for path, name in ((source, before_name), (previous, previous_name)):
        for index in SELECTED:
            _require(exact_read_quote(brief["calls"], path, index, texts(name)[index], upper=brief["messages"][-1]["event_index"]),
                     "selected-issue brief lacks full verified current/previous evidence")
    ordered = pres[0]["arguments"].get("edits")
    authorize_edits(source, r, ordered)
    source_hashes = b["initial_state"]["docx"].copy()
    if r == "b":
        second = read_json(directory / "round-b-baseline.json")
        _require(set(second) == {"schema_version", "prepared_ns", "baseline_sha256", "first_output_sha256",
                 "source_sha256", "state", "semantic_oracle_sha256"}
                 and second["schema_version"] == "veqtor_next_round_second.v1"
                 and second["baseline_sha256"] == _file_sha256(str(directory / "baseline.json"))
                 and second["semantic_oracle_sha256"] == _digest(b["oracle"])
                 and second["first_output_sha256"] == _file_sha256(previous)
                 and second["source_sha256"] == _file_sha256(source), "second round does not bind actual first output")
        source_hashes[previous] = second["first_output_sha256"]
        source_hashes[source] = second["source_sha256"]
        # Only the clean confidentiality text changed in synthetic preparation.
        paragraphs._collateral(previous, source, {2})
        old = deepcopy(paragraphs._paragraphs(previous)[2][2])
        new = deepcopy(paragraphs._paragraphs(source)[2][2])
        _require(paragraphs._text(old) == C3 and paragraphs._text(new) == C2, "second input setup drift")
        old[0][0].text = C2
        _require(etree.tostring(old) == etree.tostring(new), "second input alters confidentiality structure")
        _require(second["state"] == dict(docx=source_hashes, store_sha256=b["initial_state"]["store_sha256"]),
                 "second baseline inventory incomplete")
    initial = dict(docx=source_hashes, store_sha256=b["initial_state"]["store_sha256"])
    # Capture observations bind the original complete inventory, not just selected files.
    _require(brief["receipt"]["before"] == brief["receipt"]["after"] == write["receipt"]["before"] == initial,
             "brief wrote data or pre-write sources/store/output existence differs")
    final = dict(docx={**source_hashes, output: _file_sha256(output)}, store_sha256=initial["store_sha256"])
    _require(write["receipt"]["after"] == final, "post-write inventory contains partial/extra/mutated data")
    rows = [dict(paragraph_index=i, before=texts(before_name)[i], after=texts(after_name)[i])
            for i, *_ in edit_specs(r)]
    report = validate_document_and_journal(document_only(write["events"]), dict(
        schema_version=paragraphs.BASELINE_SCHEMA, server_name=SERVER, producer=installation["producer"],
        source_sha256=source_hashes, source_path=source, output_path=output, output_absent_before=True,
        expected_edits=ordered, expected_paragraphs=rows, tracked_change_author=AUTHOR), write["calls"], write["thread"])
    _require(_file_sha256(str(Path(b["matter"]) / ".veqtor" / "deal-positions.json")) == initial["store_sha256"],
             "saved store drifted during evidence verification")
    # NR-01 independently checks structure/text/revisions, including markup from
    # earlier rounds on untouched paragraphs. NR-03 checks complete cursor-bound
    # action-record pages without altering NR-01's independent same-page profile.
    return dict(round=r, client_thread=brief["thread"], server_sessions=sorted({first_read} | write_sessions),
                output=output, output_sha256=report["output_sha256"], mechanical=report,
                brief_message_sha256=_digest(brief["messages"]), result_message_sha256=_digest(write["messages"]))


def check_bundle(directory):
    directory = Path(directory).absolute()
    loaded = load_baseline(directory)
    a, b = [check_round(directory, r, loaded=loaded) for r in ("a", "b")]
    _require(a["client_thread"] != b["client_thread"] and not set(a["server_sessions"]) & set(b["server_sessions"]),
             "second round reused client conversation or MCP server session")
    a_end = read_json(directory / "a-write.receipt.json")["finished_ns"]
    second_start = read_json(directory / "round-b-baseline.json")["prepared_ns"]
    _require(a_end < second_start, "second source prepared before real first output")
    from prepare_next_round_acceptance import inventory
    expected_inventory = {**loaded[0]["initial_state"]["docx"],
        a["output"]: a["output_sha256"], b["output"]: b["output_sha256"],
        loaded[0]["inputs"]["b"]["source"]: _file_sha256(loaded[0]["inputs"]["b"]["source"])}
    _require(inventory(loaded[0]["matter"]) == expected_inventory, "unexpected final DOCX inventory")
    return dict(schema_version="veqtor_next_round_evidence.v1", status="mechanical_evidence_passed",
        commit=loaded[1]["commit"], tree=loaded[1]["tree"], producer=loaded[1]["producer"],
        workflow_sha256=loaded[0]["workflow_sha256"], rounds=[a, b],
        log_authenticity_verified=False, legal_equivalence_verified=False,
        open_gates=["independent brief/decision content assessment", "native adversarial scenario observations",
                    "all pages and visible markup of both outputs", "required local and hosted final gates"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--round", choices=("a", "b"))
    args = parser.parse_args()
    try:
        report = check_round(args.bundle, args.round) if args.round else check_bundle(args.bundle)
        print(json.dumps(report, indent=2, sort_keys=True))
    except (EvidenceError, OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        print("NR-03 evidence refused: " + (str(exc) if isinstance(exc, EvidenceError) else "malformed or unreadable evidence"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
