# SPDX-License-Identifier: Apache-2.0
"""Real synthetic payloads/files in fabricated envelopes test the checker, not Codex."""
from copy import deepcopy
from functools import cache
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest

from veqtor_mcp import records, server, positions
from veqtor_mcp import __version__

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import check_next_round_acceptance as checker  # noqa: E402
import prepare_next_round_acceptance as prep  # noqa: E402
import capture_next_round_session as capture  # noqa: E402
from nr03_scenario import AUTHOR, MODEL, SERVER, SELECTED, document, texts, source_manifest, WORKFLOW_FILES  # noqa: E402

ROOT = Path(__file__).parents[1]


def json_write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def synthetic_delivery(bundle, stage):
    """Fabricated client envelopes for checker controls; NEVER native evidence."""
    events = [json.loads(line) for line in (bundle / f"{stage}.jsonl").read_text().splitlines()]
    receipt = prep.read_json(bundle / f"{stage}.receipt.json")
    session = [dict(type="session_meta", payload=dict(id=events[0]["thread_id"], cwd=receipt["cwd"], synthetic=True)),
               dict(type="event_msg", payload=dict(type="task_started"))]
    for event in events:
        item = event.get("item", {})
        if event["type"] == "item.completed" and item.get("type") == "mcp_tool_call":
            session += [dict(type="response_item", payload=dict(type="function_call", call_id=item["id"], name=item["tool"],
                        arguments=json.dumps(item["arguments"]))),
                        dict(type="response_item", payload=dict(type="function_call_output", call_id=item["id"],
                        output=json.dumps((item.get("result") or {}).get("structured_content"))))]
        elif item.get("type") == "agent_message":
            session.append(dict(type="response_item", payload=dict(type="message", role="assistant",
                            content=[dict(type="output_text", text=item["text"])])))
    session.append(dict(type="event_msg", payload=dict(type="task_complete")))
    path = bundle / f"{stage}.synthetic-client.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in session)+"\n")
    json_write(bundle / f"{stage}.delivery.json", dict(schema_version="nr03-model-delivery.v3", session_path=str(path),
        session_sha256=checker._file_sha256(str(path)), receipt_sha256=checker._file_sha256(str(bundle / f"{stage}.receipt.json"))))
    if (bundle / f"{stage}.source-fixture-config.json").exists():
        from nr03_source_fixtures import attach
        attach(bundle, stage)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    return prepare_fixture(tmp_path, monkeypatch)


def prepare_fixture(tmp_path, monkeypatch, variant="main"):
    monkeypatch.setenv("VEQTOR_TRACKED_CHANGE_AUTHOR", AUTHOR)
    # Isolate lazy process configuration without clearing or warming the prior
    # cache; monkeypatch restores that exact cache object during teardown.
    monkeypatch.setattr(server, "_tracked_change_author", cache(server._tracked_change_author.__wrapped__))
    monkeypatch.delenv("VEQTOR_DISABLE_DECISION_RECORD", raising=False)
    # Package/source installation has its own real gate. No test ever labels this
    # substituted receipt native or installed-build evidence.
    monkeypatch.setattr(prep, "installed", lambda value: value)
    monkeypatch.setattr(checker, "installed", lambda value: value)
    report = dict(source_root=str(ROOT), commit="a" * 40, tree="b" * 40,
        python=str(tmp_path / "env/bin/python"), producer=dict(name="veqtor-mcp", version=__version__, build=records.SOURCE_SNAPSHOT_IDENTITY))
    installation = tmp_path / "install.json"
    json_write(installation, report)
    bundle = tmp_path / "bundle"
    b = prep.prepare(bundle, installation, model=MODEL, reasoning_effort="high", variant=variant)
    return bundle, b, report


@pytest.mark.parametrize("warmed", [False, True], ids=["cold-cache", "warm-cache"])
def test_f04_prepared_author_isolation_restores_prior_cache(tmp_path, monkeypatch, warmed):
    # Preserve the real process cache while simulating an earlier test's cache
    # and a later environment change that must not replace a cached author.
    prior_cache = cache(server._tracked_change_author.__wrapped__)
    monkeypatch.setattr(server, "_tracked_change_author", prior_cache)
    monkeypatch.setenv(server.TRACKED_CHANGE_AUTHOR_ENV, "Earlier cached author")
    if warmed:
        assert server._tracked_change_author() == "Earlier cached author"
    prior_info = prior_cache.cache_info()
    monkeypatch.setenv(server.TRACKED_CHANGE_AUTHOR_ENV, "Prior environment author")

    # Exercise the fixture itself and its monkeypatch teardown in one test.
    with monkeypatch.context() as fixture_patch:
        _, baseline, _ = prepared.__wrapped__(tmp_path, fixture_patch)
        source = baseline["inputs"]["a"]["source"]
        proof = server.preflight_edits(source_path=source, edits=checker.expected_edits(source, "a"))
        assert proof["batch_applicable"] is True
        assert proof["tracked_change_author"] == AUTHOR
        assert proof["preflight_proof"]["tracked_change_author"] == AUTHOR
        assert server._tracked_change_author is not prior_cache
        fixture_patch.setenv(server.TRACKED_CHANGE_AUTHOR_ENV, "Later fixture environment")
        assert server._tracked_change_author() == AUTHOR

    assert server._tracked_change_author is prior_cache
    assert prior_cache.cache_info() == prior_info
    assert os.environ[server.TRACKED_CHANGE_AUTHOR_ENV] == "Prior environment author"
    assert server._tracked_change_author() == ("Earlier cached author" if warmed else "Prior environment author")


def native_stage(bundle, b, installation, stage, monkeypatch, *, edit_transform=None, export_page_size=20,
                 allow_unavailable_journal=False):
    r, phase = stage.split("-")
    thread = f"synthetic-client-{r}"
    monkeypatch.setattr(positions, "SERVER_SESSION_ID", ("1" if r == "a" else "2") * 32)
    events = [dict(type="thread.started", thread_id=thread), dict(type="turn.started")]
    before = prep.state(b["matter"])
    started = time.time_ns()

    def add(tool, **args):
        identity = dict(id=str(len(events)), type="mcp_tool_call", server=SERVER, tool=tool, arguments=args)
        events.append(dict(type="item.started", item=dict(deepcopy(identity), status="in_progress", result=None, error=None)))
        try:
            payload = getattr(server, tool)(**args)
        except Exception as exc:
            if not allow_unavailable_journal or tool != "export_decision_record":
                raise
            assert getattr(exc, "code", None) == "workspace_uninitialized", str(exc)
            events.append(dict(type="item.completed", item=dict(deepcopy(identity), status="failed", error=None,
                result=dict(structured_content=None, content=[dict(type="text", text=
                    "Error executing tool export_decision_record: workspace_uninitialized: operation refused")]))))
            return None
        events.append(dict(type="item.completed", item=dict(deepcopy(identity), status="completed", error=None,
            result=dict(structured_content=deepcopy(payload), content=[dict(type="text", text=json.dumps(payload))]))))
        return payload

    add("read_deal_positions", folder=b["matter"], check_sources=True, include_history=False)
    source, previous, output = [b["inputs"][r][k] for k in ("source", "previous", "output")]

    def read_all(path):
        result = add("inspect_document", path=path, mode="browse", max_items=100)
        rows = result["paragraphs"]
        for index in SELECTED:
            ref = rows[index]["paragraph_ref"]
            add("inspect_document", path=path, mode="read", selection={"paragraph_ref": ref})
            add("verify_quote", path=path, anchor=ref, quote=rows[index]["text"], paragraph_projection="accepted_current_v1")
        return rows

    rows = read_all(source)
    if phase == "brief":
        read_all(previous)
    else:
        units = add("extract_redlines", path=source)["change_units"]
        edits = checker.expected_edits(source, r)
        if edit_transform:
            edit_transform(edits, rows)
        # Deliberately reverse the order: intended target/wording set is fixed,
        # but a workflow is free to choose an order before preflight.
        edits.reverse()
        for edit in edits:
            ref = edit.get("target", {}).get("paragraph_ref")
            anchor = edit.get("anchor", ref)
            index = ref["paragraph_index"] if ref else next(u["reference"]["paragraph_index"] for u in units if u["anchor"] == anchor)
            add("inspect_document", path=source, mode="read", selection={"paragraph_ref": rows[index]["paragraph_ref"]})
            opts = {} if "anchor" in edit else dict(paragraph_projection="accepted_current_v1")
            add("verify_quote", path=source, anchor=anchor, quote=edit["delete_text"], **opts)
        pre = add("preflight_edits", source_path=source, edits=edits)
        assert pre["batch_applicable"], pre
        app = add("apply_edits", source_path=source, output_path=output, edits=edits, preflight_proof=pre["preflight_proof"])
        extracted = add("extract_redlines", path=output)["change_units"]
        result = add("inspect_document", path=output, mode="browse", max_items=100)
        for item, edit in zip(app["applied"], edits):
            unit = next(u for u in extracted if u["reference"]["revision_ids"] == item["tracked_revision_ids"])
            row = result["paragraphs"][unit["reference"]["paragraph_index"]]
            add("inspect_document", path=output, mode="read", selection={"paragraph_ref": row["paragraph_ref"]})
            add("verify_quote", path=output, anchor=row["paragraph_ref"], quote=row["text"], paragraph_projection="accepted_current_v1")
            add("verify_quote", path=output, anchor=unit["anchor"], quote=edit["delete_text"])
        cursor = None
        while True:
            page = add("export_decision_record", workspace=b["matter"], max_records=export_page_size,
                       **({"before_record_id": cursor} if cursor else {}))
            if page is None and allow_unavailable_journal:
                break
            if not page["truncated"]:
                break
            cursor = page["next_before_record_id"]
    events.append(dict(type="item.completed", item=dict(id="message", type="agent_message", text="Synthetic memo/result. Human assessment remains open.")))
    events.append(dict(type="turn.completed"))
    event_path = bundle / f"{stage}.jsonl"
    event_path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    prompt = capture.prompt_for(bundle, stage, b)
    (bundle / f"{stage}.prompt.txt").write_text(prompt)
    resume = thread if phase == "write" else None
    receipt = dict(schema_version="veqtor_next_round_capture.v1", stage=stage,
        command=capture.command_for("/synthetic/codex", installation["python"], stage,
            model=MODEL, reasoning_effort="high", thread_id=resume,
            journal_disabled=b["variant"] == "journal-unavailable"), cwd=str(bundle / f"client-{r}"),
        client_selection=b["client_selection"], baseline_sha256=checker._file_sha256(str(bundle / "baseline.json")),
        second_baseline_sha256=checker._file_sha256(str(bundle / "round-b-baseline.json")) if r == "b" else None,
        installation_sha256=b["installation_sha256"], prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        events_sha256=checker._file_sha256(str(event_path)), workflow_sha256=b["workflow_sha256"],
        parent_receipt_sha256=checker._file_sha256(str(bundle / f"{r}-brief.receipt.json")) if resume else None,
        resumed_thread_id=resume, started_ns=started, finished_ns=time.time_ns(), exit_code=0,
        before=before, after=prep.state(b["matter"]))
    json_write(bundle / f"{stage}.receipt.json", receipt)
    synthetic_delivery(bundle, stage)
    return events


@pytest.fixture
def evidence(prepared, monkeypatch):
    bundle, b, installation = prepared
    for stage in ("a-brief", "a-write"):
        native_stage(bundle, b, installation, stage, monkeypatch)
    prep.prepare_second(bundle)
    for stage in ("b-brief", "b-write"):
        native_stage(bundle, b, installation, stage, monkeypatch)
    return bundle, b, installation


def test_synthetic_fixture_is_deterministic_complete_and_supported():
    assert document("incoming-a") == document("incoming-a")
    assert document("incoming-a") != document("previous")


def test_positive_two_round_control_is_not_native_or_human_acceptance(evidence):
    bundle, b, _ = evidence
    report = checker.check_bundle(bundle)
    assert report["status"] == "mechanical_evidence_passed"
    assert len(report["rounds"]) == 2
    assert len(report["open_gates"]) == 4
    assert report["legal_equivalence_verified"] is report["log_authenticity_verified"] is False
    assert checker.actual_texts(b["inputs"]["b"]["output"]) == texts("counter-b")
    assert report["rounds"][1]["mechanical"]["exact_revisions_verified"]


def mutate_events(bundle, stage, mutate):
    path = bundle / f"{stage}.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()]
    mutate(events)
    # Resynchronize envelopes and outer hashes: a plausible weaker substitute
    # must fail semantic evidence checks, not merely stale checksum validation.
    for event in events:
        item = event.get("item", {})
        if event["type"] == "item.completed" and item.get("type") == "mcp_tool_call" and item.get("result"):
            item["result"]["content"][0]["text"] = json.dumps(item["result"]["structured_content"])
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    receipt_path = bundle / f"{stage}.receipt.json"
    receipt = prep.read_json(receipt_path)
    receipt["events_sha256"] = checker._file_sha256(str(path))
    json_write(receipt_path, receipt)
    if stage.endswith("brief"):
        follow = bundle / (stage.replace("brief", "write") + ".receipt.json")
        rec = prep.read_json(follow)
        rec["parent_receipt_sha256"] = checker._file_sha256(str(receipt_path))
        json_write(follow, rec)
        synthetic_delivery(bundle, stage.replace("brief", "write"))
    synthetic_delivery(bundle, stage)


@pytest.mark.parametrize("fault", ["no_store_read", "partial_store", "missing_condition", "source_not_checked",
    "old_confirmed_pending", "wrong_matter", "wrong_producer", "snippet_instead", "no_quote",
    "wrong_quote_source", "wrong_read_source", "wrong_prior_ref", "same_server", "shell_injection"])
def test_absent_or_weaker_second_round_brief_evidence_refuses(evidence, fault):
    bundle, b, _ = evidence

    def change(events):
        if fault == "no_store_read":
            events[:] = [e for e in events if e.get("item", {}).get("tool") != "read_deal_positions"]
            return
        if fault == "shell_injection":
            events.insert(-1, dict(type="item.completed", item=dict(type="command_execution", command="read another matter")))
            return
        for event in events:
            item = event.get("item", {})
            args = item.get("arguments", {})
            p = item.get("result", {}).get("structured_content", {}) if item.get("result") else {}
            if item.get("tool") == "read_deal_positions":
                if fault == "partial_store" and p:
                    p["positions"].pop()
                elif fault == "missing_condition" and p:
                    p["positions"][2]["content"]["fallback_conditions"] = None
                elif fault == "source_not_checked":
                    args["check_sources"] = False
                    if p:
                        p["source_observations"][0]["status"] = "not_checked"
                elif fault == "old_confirmed_pending" and p:
                    p["positions"][3]["content"]["business_decision"] = "not_required"
                elif fault == "wrong_matter":
                    args["folder"] = str(bundle / "other")
                elif fault == "same_server" and p:
                    p["server_session_id"] = "1" * 32
            if fault == "wrong_producer" and p:
                p["producer"]["build"] = "source-snapshot-v1-sha256:" + "f" * 64
            if item.get("tool") == "inspect_document" and args.get("mode") == "read":
                if fault == "snippet_instead":
                    args["mode"] = "browse"
                elif fault == "wrong_read_source" and p:
                    p["path"] = b["inputs"]["a"]["source"]
                elif fault == "wrong_prior_ref":
                    args["selection"]["paragraph_ref"]["file_sha256"] = b["initial_state"]["docx"][b["inputs"]["a"]["source"]]
            if fault == "wrong_quote_source" and item.get("tool") == "verify_quote" and p:
                p["matches"][0]["path"] = b["inputs"]["a"]["source"]
        if fault == "no_quote":
            events[:] = [e for e in events if e.get("item", {}).get("tool") != "verify_quote"]
    mutate_events(bundle, "b-brief", change)
    with pytest.raises(checker.EvidenceError):
        checker.check_bundle(bundle)


@pytest.mark.parametrize("fault", ["no_fresh_store", "no_preflight", "no_apply", "no_output_read", "only_snippet",
    "no_deletion_quote", "no_export", "no_output_extract", "changed_target", "different_edits", "old_proof"])
def test_write_evidence_requires_exact_complete_native_chain(evidence, fault):
    bundle, _, _ = evidence

    def change(events):
        omitted = dict(no_fresh_store="read_deal_positions", no_preflight="preflight_edits", no_apply="apply_edits",
                       no_export="export_decision_record", no_output_extract="extract_redlines").get(fault)
        if omitted:
            events[:] = [e for e in events if e.get("item", {}).get("tool") != omitted]
        for e in events:
            item = e.get("item", {})
            args = item.get("arguments", {})
            if item.get("tool") == "inspect_document" and args.get("path", "").endswith("counter-b.docx") and args.get("mode") == "read":
                if fault == "only_snippet":
                    args["mode"] = "browse"
            if item.get("tool") == "apply_edits":
                if fault == "different_edits":
                    args["edits"][0]["insert_text"] = "four years"
                if fault == "old_proof":
                    args["preflight_proof"]["source_sha256"] = "0" * 64
            if fault == "changed_target" and item.get("tool") in {"preflight_edits", "apply_edits"}:
                args["edits"][0]["target"]["paragraph_ref"]["paragraph_index"] = 3
        if fault == "no_output_read":
            events[:] = [e for e in events if not (e.get("item", {}).get("tool") == "inspect_document"
                and e["item"]["arguments"].get("path", "").endswith("counter-b.docx") and e["item"]["arguments"].get("mode") == "read")]
        if fault == "no_deletion_quote":
            events[:] = [e for e in events if not (e.get("item", {}).get("tool") == "verify_quote"
                and e["item"]["arguments"].get("path", "").endswith("counter-b.docx")
                and "change_unit_id" in e["item"]["arguments"].get("anchor", {}))]
    mutate_events(bundle, "b-write", change)
    with pytest.raises(checker.EvidenceError):
        checker.check_bundle(bundle)


@pytest.mark.parametrize("fault", ["missing_decision", "copied_positions", "different_workflow", "resume_previous_round",
    "old_output", "source_mutated", "extra_output", "no_actual_output", "missing_prior", "wrong_effort", "wrong_install"])
def test_delivery_receipts_and_actual_files_are_required(evidence, fault):
    bundle, b, _ = evidence
    receipt_path = bundle / "b-brief.receipt.json"
    receipt = prep.read_json(receipt_path)
    if fault in {"missing_decision", "copied_positions"}:
        stage = "b-write" if fault == "missing_decision" else "b-brief"
        path = bundle / f"{stage}.prompt.txt"
        path.write_text("Proceed." if fault == "missing_decision" else path.read_text() + "\n" + json.dumps(b["store"]["positions"]))
        rp = bundle / f"{stage}.receipt.json"
        rec = prep.read_json(rp)
        rec["prompt_sha256"] = checker._file_sha256(str(path))
        json_write(rp, rec)
    elif fault == "different_workflow":
        (bundle / "workflow" / WORKFLOW_FILES[1]).write_text("A different workflow with the same output claim.")
    elif fault == "resume_previous_round":
        receipt["resumed_thread_id"] = "synthetic-client-a"
        json_write(receipt_path, receipt)
    elif fault == "old_output":
        receipt["before"]["docx"][b["inputs"]["b"]["output"]] = "0" * 64
        json_write(receipt_path, receipt)
    elif fault == "source_mutated":
        Path(b["inputs"]["a"]["source"]).write_bytes(document("previous"))
    elif fault == "extra_output":
        (Path(b["matter"]) / "hidden-partial.docx").write_bytes(document("previous"))
    elif fault == "no_actual_output":
        Path(b["inputs"]["b"]["output"]).unlink()
    elif fault == "missing_prior":
        Path(b["inputs"]["a"]["output"]).unlink()
    elif fault == "wrong_effort":
        receipt["command"] = [v.replace('"high"', '"ultra"') for v in receipt["command"]]
        json_write(receipt_path, receipt)
    elif fault == "wrong_install":
        # A different executable alone is not a producer substitution; change the server command.
        receipt["command"] = [v.replace("env/bin/python", "other/bin/python") for v in receipt["command"]]
        json_write(receipt_path, receipt)
    with pytest.raises((checker.EvidenceError, FileNotFoundError)):
        checker.check_bundle(bundle)


@pytest.mark.parametrize("variant", ["no-store", "proposal", "withdrawn", "unconfirmed", "ambiguous",
    "existing-output", "document-injection", "position-injection", "no-previous"])
def test_separate_synthetic_variants_are_prepared_not_claimed_accepted(prepared, tmp_path, variant):
    _, _, _ = prepared
    bundle = tmp_path / variant
    b = prep.prepare(bundle, tmp_path / "install.json", model=MODEL, reasoning_effort="high", variant=variant)
    if variant == "no-store":
        assert b["store"] is None and b["initial_state"]["store_sha256"] is None
    if variant == "no-previous":
        assert "No previous sent document is available" in capture.stimulus(bundle, "a-brief", b)
    with pytest.raises(checker.EvidenceError, match="positive checker only"):
        checker.check_round(bundle, "a")


def test_workflow_copy_hashes_bind_both_files(prepared):
    bundle, b, _ = prepared
    assert source_manifest(bundle / "workflow", WORKFLOW_FILES) == b["workflow_sha256"]
    assert capture.prompt_for(bundle, "a-brief", b) != capture.prompt_for(bundle, "b-brief", b)


@pytest.mark.parametrize("fault", ["other_source_bytes", "confirmation", "history"])
def test_predeclared_oracle_not_reconstructed_from_matching_baseline(prepared, fault):
    bundle, b, _ = prepared
    if fault == "other_source_bytes":
        source = Path(b["inputs"]["a"]["source"])
        source.write_bytes(document("previous"))
        b["initial_state"]["docx"][str(source)] = checker._file_sha256(str(source))
    else:
        store_path = Path(b["matter"]) / ".veqtor/deal-positions.json"
        snapshot = prep.read_json(store_path)
        if fault == "confirmation":
            snapshot["positions"][0]["confirmation"]["statement"] = "A weaker unspecified confirmation."
            snapshot["history"][5]["position"] = deepcopy(snapshot["positions"][0])
        else:
            snapshot["history"][0]["position"]["content"]["desired_outcome"] = "Different prior intention."
        snapshot["revision"] = checker._digest({k: v for k, v in snapshot.items() if k != "revision"})
        json_write(store_path, snapshot)
        b["initial_state"]["store_sha256"] = checker._file_sha256(str(store_path))
        for key in ("positions", "history", "revision"):
            b["store"][key] = deepcopy(snapshot[key])
    json_write(bundle / "baseline.json", b)
    with pytest.raises(checker.EvidenceError):
        checker.load_baseline(bundle)


@pytest.mark.parametrize("with_fault", [False, True])
def test_observation_capture_freezes_inputs_and_records_actual_resume(prepared, monkeypatch, tmp_path, with_fault):
    import capture_next_round_observation as observation
    from types import SimpleNamespace
    bundle, b, report = prepared
    monkeypatch.setattr(observation, "installed", lambda value: value)
    plan = dict(scenario="synthetic capture unit test", expected=dict(outcome="ask before unsupported work"),
        steps=[dict(id="brief", resume=None, prompt="Use the workflow for this synthetic matter and ask for decisions."),
               dict(id="decision", resume="brief", prompt="Defer the unsupported mandatory work; do not write yet.")])
    if with_fault:
        from nr03_fault import FAULT
        plan["fault"] = dict(kind=FAULT, step="decision")
    plan_path = tmp_path / "plan.json"
    json_write(plan_path, plan)
    observation.freeze(bundle, plan_path)
    launches = []

    def run(command, *, input, cwd, stdout, stderr, check):
        launches.append((command, input, cwd))
        payload = server.read_deal_positions(folder=b["matter"], check_sources=True)
        identity = dict(id="read", type="mcp_tool_call", server=SERVER, tool="read_deal_positions",
                        arguments=dict(folder=b["matter"], check_sources=True))
        events = [dict(type="thread.started", thread_id="synthetic-observation-thread"), dict(type="turn.started"),
            dict(type="item.started", item=dict(identity, status="in_progress", result=None, error=None)),
            dict(type="item.completed", item=dict(identity, status="completed", error=None,
                 result=dict(structured_content=payload, content=[dict(type="text", text=json.dumps(payload))]))),
            dict(type="item.completed", item=dict(id="result", type="agent_message", text="Synthetic question.")),
            dict(type="turn.completed")]
        stdout.write(("\n".join(json.dumps(e) for e in events) + "\n").encode())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(observation.subprocess, "run", run)
    assert observation.capture(bundle, "brief", "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None) == 0
    assert observation.capture(bundle, "decision", "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None) == 0
    assert "resume" not in launches[0][0] and "resume" in launches[1][0]
    assert launches[1][1].decode() == plan["steps"][1]["prompt"]
    assert launches[0][2] == launches[1][2]
    receipt = prep.read_json(bundle / "observations/decision.receipt.json")
    assert receipt["acceptance_assessed"] is False
    assert receipt["before"] == receipt["after"] == b["initial_state"]
    assert receipt["installation_sha256"] == checker._file_sha256(str(bundle / "installation.json"))
    assert report["python"] in " ".join(launches[1][0])
    assert "_nr03_fsync" not in " ".join(launches[0][0])
    assert ("_nr03_fsync" in " ".join(launches[1][0])) is with_fault
    assert receipt["fault"] == plan.get("fault")


@pytest.mark.parametrize("fault", ["future_resume", "duplicate_id", "path_id", "extra_fields", "missing_expected",
                                  "unknown_fault", "unknown_fault_step"])
def test_observation_plan_refuses_ambiguous_or_unfrozen_steps(fault):
    from capture_next_round_observation import validate_plan
    plan = dict(scenario="synthetic", expected=dict(result="no write"), steps=[dict(id="brief", resume=None, prompt="Read selected issue.")])
    if fault == "future_resume":
        plan["steps"][0]["resume"] = "future"
    elif fault == "duplicate_id":
        plan["steps"].append(deepcopy(plan["steps"][0]))
    elif fault == "path_id":
        plan["steps"][0]["id"] = "../outside"
    elif fault == "extra_fields":
        plan["steps"][0]["tool_calls"] = []
    elif fault == "missing_expected":
        plan["expected"] = {}
    elif fault == "unknown_fault":
        plan["fault"] = dict(kind="arbitrary-call-replacement", step="brief")
    else:
        from nr03_fault import FAULT
        plan["fault"] = dict(kind=FAULT, step="absent")
    with pytest.raises(checker.EvidenceError):
        validate_plan(plan)


def test_native_fault_source_fails_real_postcommit_once_and_preserves_version(prepared, monkeypatch):
    import os
    from nr03_fault import fault_args
    from nr03_scenario import IDS
    from position_source_launch import SOURCE_ONLY_BOOTSTRAP
    bundle, b, _ = prepared
    matter = b["matter"]
    original_fsync = os.fsync
    # Execute only the same pre-run fault prefix, never start a fake MCP server.
    prefix = fault_args(matter)[-1].split("\nimport runpy\n", 1)[0].removeprefix(SOURCE_ONLY_BOOTSTRAP)
    namespace = {}
    exec(prefix, namespace)
    patched = os.fsync
    os.fsync = original_fsync
    monkeypatch.setattr(os, "fsync", patched)
    current = positions.read_deal_positions(matter, include_history=True, check_sources=True)
    revised = deepcopy(current["positions"][0]["content"])
    revised["desired_outcome"] = "Protect confidential information for four years after termination."
    with pytest.raises(positions.PositionError, match="commit_uncertain"):
        positions.mutate_deal_positions(matter, current["revision"], [dict(
            op="update", position_id=IDS[0], expected_version=1, content=revised)])
    observed = positions.read_deal_positions(matter, include_history=True, check_sources=True)
    assert observed["positions"][0]["version"] == 2
    assert observed["positions"][0]["confirmation"] is None
    assert len(observed["history"]) == 11
    confirmed = positions.mutate_deal_positions(matter, observed["revision"], [dict(
        op="confirm", position_id=IDS[0], expected_version=2, user_confirmed=True, statement="Exact v2 synthetic confirmation.")])
    assert confirmed["positions"][0]["version"] == 2
    assert namespace["_nr03_fault_fired"] is True
    assert (bundle / "baseline.json").is_file()
