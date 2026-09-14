# SPDX-License-Identifier: Apache-2.0
"""NR02-R1/R2/R3 closure controls. Synthetic fixtures, never native acceptance."""
from copy import deepcopy
from itertools import combinations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from veqtor_mcp import server
from test_position_gate_artifacts import bundle as bundle, capture, checker
import prepare_position_acceptance as prepare


SELECTION = {"model": "gpt-6-astra", "reasoning_effort": "ultra"}


def check(directory):
    return checker.check_bundle(directory, **SELECTION)


def edit_receipt(directory, name, change):
    path = directory / f"{name}.receipt.json"
    original = path.read_bytes()
    value = json.loads(original)
    change(value)
    path.write_text(json.dumps(value))
    return path, original


@pytest.mark.parametrize("name", sorted(checker.SESSIONS - {"other"}))
@pytest.mark.parametrize("mutation", ["empty", "omit_unbound", "unrelated_substitute"])
def test_r1_complete_stage_inventory_cannot_be_weakened(bundle, name, mutation):
    expected = json.loads((bundle / "baseline.json").read_bytes())
    folder = Path(expected["folders"][checker.SESSION_FOLDERS[name]])
    missing = str(folder / expected["selected_current_file"])

    def weaken(receipt):
        for key in ("docx_before", "docx_after"):
            inventory = receipt[key]
            assert missing in inventory  # The actual complete positive capture includes it.
            if mutation == "empty":
                receipt[key] = {}
            else:
                digest = inventory.pop(missing)
                assert inventory  # Nonempty remaining documents cannot substitute.
                if mutation == "unrelated_substitute":
                    inventory[str(folder.parent / "unrelated" / Path(missing).name)] = digest

    path, original = edit_receipt(bundle, name, weaken)
    with pytest.raises(checker.EvidenceError, match="complete stage DOCX inventory"):
        check(bundle)
    path.write_bytes(original)
    assert check(bundle)["status"] == "passed"


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("mutation", ["missing_observation", "missing_original", "missing_root_state",
    "old_root_only", "old_store_remains", "missing_moved_store", "wrong_store_bytes", "positions_only_hash"])
def test_r2_relocation_requires_complete_root_and_snapshot_evidence(bundle, phase, mutation):
    key = "relocation_" + phase

    def weaken(receipt):
        observed = receipt[key]
        if mutation == "missing_observation":
            del receipt[key]
        elif mutation == "missing_original":
            del observed["original"]
        elif mutation == "missing_root_state":
            del observed["original"]["root_state"]
        elif mutation == "old_root_only":
            observed["original"]["root_state"] = "directory"
        elif mutation == "old_store_remains":
            observed["original"] = deepcopy(observed["moved"])
        elif mutation == "missing_moved_store":
            observed["moved"].update(store_state="absent", store_sha256=None)
        elif mutation == "wrong_store_bytes":
            observed["moved"]["store_sha256"] = "f" * 64
        else:
            events = [json.loads(line) for line in (bundle / "moved.jsonl").read_text().splitlines()]
            payload = next(e["item"]["result"]["structured_content"] for e in events
                if e.get("item", {}).get("result"))
            observed["moved"]["store_sha256"] = checker.sha(checker.canonical(payload["positions"]))

    path, original = edit_receipt(bundle, "moved", weaken)
    with pytest.raises(checker.EvidenceError, match="root/store relocation evidence"):
        check(bundle)
    path.write_bytes(original)
    assert check(bundle)["status"] == "passed"


def test_r2_actual_copy_plus_docx_removal_is_not_a_move(tmp_path, monkeypatch):
    # Explicit synthetic setup reproduces the reviewer counterexample outside checkout.
    real_rename = Path.rename

    def copy_keep_store(path, target):
        if path.name == "matter" and Path(target).name == "moved":
            shutil.copytree(path, target)
            for doc in path.glob("*.docx"):
                doc.unlink()
            assert (path / ".veqtor" / "deal-positions.json").is_file()
            return Path(target)
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", copy_keep_store)
    weak_bundle = bundle.__wrapped__(tmp_path, monkeypatch)
    expected = json.loads((weak_bundle / "baseline.json").read_bytes())
    original = Path(expected["folders"]["original"])
    assert original.is_dir() and (original / ".veqtor" / "deal-positions.json").is_file()
    assert not list(original.glob("*.docx"))
    receipt = json.loads((weak_bundle / "moved.receipt.json").read_bytes())
    assert receipt["relocation_before"]["original"]["store_state"] == "regular"
    assert receipt["relocation_after"]["original"]["store_state"] == "regular"
    with pytest.raises(checker.EvidenceError, match="root/store relocation evidence"):
        check(weak_bundle)


def test_r2_later_independent_folder_recreation_does_not_rewrite_move_history(bundle):
    expected = json.loads((bundle / "baseline.json").read_bytes())
    original = Path(expected["folders"]["original"])
    assert not original.exists()
    original.mkdir()
    # A new independent synthetic matter after the relocation observations.
    saved = server.mutate_deal_positions(str(original), None, [dict(op="create",
        position_id="pos_" + "f" * 32, content=deepcopy(expected["initial_positions"][2]["content"]) |
        {"related_position_ids": []})])
    moved = json.loads((Path(expected["folders"]["moved"]) / ".veqtor" / "deal-positions.json").read_bytes())
    assert saved["matter_id"] != moved["matter_id"]
    source = Path(expected["folders"]["first"]) / expected["selected_current_file"]
    later_doc = original / "independent-later.docx"
    shutil.copy2(source, later_doc)
    # Later captures may honestly contain that independent folder's DOCX.
    def later_inventory(receipt):
        for key in ("docx_before", "docx_after"):
            receipt[key][str(later_doc)] = checker.sha(later_doc.read_bytes())
    edit_receipt(bundle, "copy_independent", later_inventory)
    assert check(bundle)["status"] == "passed"


@pytest.mark.parametrize("mutation", ["receipt_missing", "receipt_model", "receipt_effort", "flags_missing",
    "model_changed", "effort_changed", "flags_and_receipt_changed", "mcp_target", "isolation_removed"])
def test_r3_selection_is_required_without_relaxing_mcp_or_isolation(bundle, mutation):
    def weaken(receipt):
        command = receipt["command"]
        if mutation == "receipt_missing":
            del receipt["client_selection"]
        elif mutation in {"receipt_model", "receipt_effort"}:
            key = "model" if mutation == "receipt_model" else "reasoning_effort"
            receipt["client_selection"][key] = "gpt-5.5" if key == "model" else "high"
        elif mutation == "flags_missing":
            command[-5:-1] = []
        elif mutation == "model_changed":
            command[command.index("--model") + 1] = "gpt-5.5"
        elif mutation == "effort_changed":
            command[-2] = 'model_reasoning_effort="high"'
        elif mutation == "flags_and_receipt_changed":
            receipt["client_selection"] = {"model": "gpt-5.5", "reasoning_effort": "high"}
            command[command.index("--model") + 1] = "gpt-5.5"
            command[-2] = 'model_reasoning_effort="high"'
        elif mutation == "mcp_target":
            command[command.index("-c") + 1] = 'mcp_servers.veqtor_nr02.command="/other/python"'
        else:
            command.remove("--ignore-user-config")
    path, original = edit_receipt(bundle, "save", weaken)
    with pytest.raises(checker.EvidenceError, match="selection|exact isolated producer"):
        check(bundle)
    path.write_bytes(original)
    report = check(bundle)
    assert report["status"] == "passed" and report["client_selection"] == SELECTION


@pytest.mark.parametrize("mutation", ["omitted_baseline_selection", "rewritten_all_selections"])
def test_r3_required_caller_selection_cannot_be_replaced_by_bundle_claims(bundle, mutation):
    path = bundle / "baseline.json"
    stamp = path.stat()
    baseline = json.loads(path.read_bytes())
    if mutation == "omitted_baseline_selection":
        del baseline["client_selection"]
    else:
        baseline["client_selection"] = {"model": "gpt-5.5", "reasoning_effort": "high"}
    raw = json.dumps(baseline).encode()
    path.write_bytes(raw)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    for name in checker.SESSIONS:
        def rebind(receipt):
            receipt["baseline_sha256"] = checker.sha(raw)
            if mutation == "rewritten_all_selections":
                receipt["client_selection"] = deepcopy(baseline["client_selection"])
                receipt["command"][receipt["command"].index("--model") + 1] = "gpt-5.5"
                receipt["command"][-2] = 'model_reasoning_effort="high"'
        edit_receipt(bundle, name, rebind)
    with pytest.raises(checker.EvidenceError, match="baseline fields|baseline differs"):
        check(bundle)


def test_r1_r2_r3_combined_weak_receipt_and_partial_restorations(bundle):
    path = bundle / "moved.receipt.json"
    original = path.read_bytes()
    assert check(bundle)["status"] == "passed"
    for count in (1, 2, 3):
        for missing in combinations(("documents", "relocation", "selection"), count):
            receipt = json.loads(original)
            if "documents" in missing:
                receipt["docx_before"] = receipt["docx_after"] = {}
            if "relocation" in missing:
                del receipt["relocation_before"]
                del receipt["relocation_after"]
            if "selection" in missing:
                receipt["command"][-5:-1] = []
            path.write_text(json.dumps(receipt))
            with pytest.raises(checker.EvidenceError):
                check(bundle)
    path.write_bytes(original)
    assert check(bundle)["status"] == "passed"


@pytest.fixture
def prepared(tmp_path):
    installation = tmp_path / "installation.json"
    installation.write_text(json.dumps({"source_root": str(tmp_path / "synthetic-source"),
        "producer": server._producer(), "python": "/synthetic/installed/python"}))
    return prepare.prepare(tmp_path / "prepared", installation, **SELECTION)


def test_r3_real_capture_interface_emits_selected_command_and_receipt(prepared, monkeypatch):
    commands = []

    def synthetic_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=b"Synthetic capture-interface control only\n", stderr=b"")

    monkeypatch.setattr(capture.subprocess, "run", synthetic_run)
    assert capture.capture(prepared, "save", "/synthetic/codex", "Synthetic prompt", **SELECTION) == 0
    receipt = json.loads((prepared / "save.receipt.json").read_bytes())
    expected = json.loads((prepared / "baseline.json").read_bytes())
    assert receipt["client_selection"] == expected["client_selection"] == SELECTION
    assert receipt["command"] == commands[0]
    assert commands[0][-5:] == ["--model", "gpt-6-astra", "-c", 'model_reasoning_effort="ultra"', "-"]
    assert 'mcp_servers.veqtor_nr02.command="/synthetic/installed/python"' in commands[0]
    assert capture.SERVER_OVERRIDE in commands[0]
    assert "--ignore-user-config" in commands[0] and "--ephemeral" in commands[0]
    checker._check_documents("save", receipt, expected)


def test_r2_real_capture_records_the_stage_snapshot(prepared, monkeypatch):
    expected = json.loads((prepared / "baseline.json").read_bytes())
    original, moved = (Path(expected["folders"][n]) for n in ("original", "moved"))
    server.mutate_deal_positions(str(original), None, [dict(op="create",
        position_id=p["position_id"], content=p["content"]) for p in expected["initial_positions"]])
    original.rename(moved)  # Labelled synthetic whole-folder setup.
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **kw:
        SimpleNamespace(returncode=0, stdout=b"Synthetic capture-interface control only\n", stderr=b""))
    assert capture.capture(prepared, "moved", "/synthetic/codex", None, **SELECTION) == 0
    receipt = json.loads((prepared / "moved.receipt.json").read_bytes())
    assert receipt["relocation_before"] == receipt["relocation_after"]
    assert receipt["relocation_before"]["original"] == dict(root_state="absent", store_state="absent", store_sha256=None)
    assert receipt["relocation_before"]["moved"]["store_sha256"] == checker.sha(
        (moved / ".veqtor" / "deal-positions.json").read_bytes())
    checker._check_relocation(receipt, server.read_deal_positions(str(moved), include_history=True))


@pytest.mark.parametrize("selection", [{"model": "gpt-5.5", "reasoning_effort": "ultra"},
                                     {"model": "gpt-6-astra", "reasoning_effort": "high"}])
def test_r3_capture_refuses_unapproved_selection_before_launch(prepared, monkeypatch, selection):
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **kw: pytest.fail("must not launch"))
    with pytest.raises(checker.EvidenceError, match="predeclared model/effort"):
        capture.capture(prepared, "save", "/synthetic/codex", "Synthetic prompt", **selection)
    assert not (prepared / "save.prompt.txt").exists()


@pytest.mark.parametrize("script", ["prepare_position_acceptance.py", "capture_position_session.py", "check_position_acceptance.py"])
@pytest.mark.parametrize("omitted", ["model", "reasoning-effort"])
def test_r3_canonical_cli_requires_explicit_selection(tmp_path, script, omitted):
    args = [sys.executable, str(Path(__file__).parents[1] / "scripts" / script), "--bundle", str(tmp_path / "absent")]
    if script.startswith("prepare"):
        args += ["--installation", str(tmp_path / "absent.json")]
    elif script.startswith("capture"):
        args += ["--name", "restart", "--codex", "/synthetic/codex"]
    args += ["--reasoning-effort", "ultra"] if omitted == "model" else ["--model", "gpt-6-astra"]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 2 and "--" + omitted in result.stderr
    assert not (tmp_path / "absent").exists()
