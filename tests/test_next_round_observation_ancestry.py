# SPDX-License-Identifier: Apache-2.0
"""F08 input/launcher adversaries; recording stubs, never native acceptance."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_next_round_acceptance as nr03
from test_next_round_acceptance import checker, json_write, prep
from test_next_round_review_fixes import turn
import capture_next_round_observation as observation
from capture_next_round_session import command_for
from nr03_delivery import BOUNDARY, delivered_prompt
from nr03_fault import FAULT, fault_args
from nr03_scenario import MODEL, WORKFLOW_FILES

prepared = nr03.prepared
IDS = ["brief", "mandatory", "clarify", "exclude"]


def setup_chain(prepared, monkeypatch, tmp_path, *, captured=3, fault_step=None, journal_disabled=False):
    bundle, baseline, report = prepared
    if journal_disabled:
        baseline["variant"] = "journal-unavailable"
        json_write(bundle / "baseline.json", baseline)
    plan = dict(scenario="F08 synthetic ancestor input and command integrity",
        expected=dict(result="refuse before any continuation files or launch"),
        steps=[dict(id=name, resume=IDS[i - 1] if i else None,
                    prompt="Review the selected issues." if not i else "Ordinary user followup: " + name)
               for i, name in enumerate(IDS)])
    if fault_step:
        plan["fault"] = dict(kind=FAULT, step=fault_step)
    path = tmp_path / "plan.json"
    json_write(path, plan)
    observation.freeze(bundle, path)
    monkeypatch.setattr(observation, "installed", lambda value: value)
    launches = []

    def emit(command, **kwargs):
        assert command[0] == "/synthetic/codex"
        launches.append(dict(command=deepcopy(command), prompt=kwargs["input"], cwd=kwargs["cwd"]))
        kwargs["stdout"].write(("\n".join(json.dumps(e) for e in turn("synthetic-f08-root")) + "\n").encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(observation.subprocess, "run", emit)
    for name in IDS[:captured]:
        assert observation.capture(bundle, name, "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None) == 0
    return bundle, baseline, report, plan, launches


def rechain(folder, changed, captured):
    """Keep every downstream receipt valid so only the damaged ancestor differs."""
    for index in range(IDS.index(changed) + 1, captured):
        path = folder / f"{IDS[index]}.receipt.json"
        receipt = prep.read_json(path)
        receipt["parent_receipt_sha256"] = checker._file_sha256(str(folder / f"{IDS[index - 1]}.receipt.json"))
        json_write(path, receipt)


def assert_refuses_before_creation(bundle, baseline, launches, captured):
    target = IDS[captured]
    with pytest.raises(checker.EvidenceError, match="observation ancestor"):
        observation.capture(bundle, target, "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None)
    assert len(launches) == captured
    assert not list((bundle / "observations").glob(target + ".*"))
    assert prep.state(baseline["matter"]) == baseline["initial_state"]


@pytest.mark.parametrize("ancestor,captured", [("brief", 1), ("brief", 3), ("mandatory", 3)])
@pytest.mark.parametrize("damage", [None, "missing_prompt", "changed_prompt", "rehash_wrong_prompt",
                                    "missing_prompt_hash", "wrong_command", "symlink_prompt", "directory_prompt"])
def test_f08_every_ancestor_requires_its_exact_regular_input(prepared, monkeypatch, tmp_path, ancestor, captured, damage):
    bundle, baseline, _, plan, launches = setup_chain(prepared, monkeypatch, tmp_path, captured=captured)
    folder = bundle / "observations"
    prompt_path = folder / f"{ancestor}.prompt.txt"
    receipt_path = folder / f"{ancestor}.receipt.json"
    receipt = prep.read_json(receipt_path)
    expected = plan["steps"][IDS.index(ancestor)]["prompt"]
    if ancestor == "brief":
        expected = delivered_prompt(bundle, expected, WORKFLOW_FILES)
    assert prompt_path.read_bytes() == expected.encode() == launches[IDS.index(ancestor)]["prompt"]
    if damage == "missing_prompt":
        prompt_path.unlink()
    elif damage in {"changed_prompt", "rehash_wrong_prompt"}:
        prompt_path.write_text("Review the selected issues without the supplied boundary or workflow.")
        if damage == "rehash_wrong_prompt":
            receipt["prompt_sha256"] = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    elif damage == "missing_prompt_hash":
        receipt.pop("prompt_sha256")
    elif damage == "wrong_command":
        receipt["command"] = ["/synthetic/codex", "exec", "--model", "wrong-model", "-"]
    elif damage == "symlink_prompt":
        backing = tmp_path / "same-input.txt"
        backing.write_bytes(prompt_path.read_bytes())
        prompt_path.unlink()
        prompt_path.symlink_to(backing)
    elif damage == "directory_prompt":
        prompt_path.unlink()
        prompt_path.mkdir()
    if damage is not None:
        json_write(receipt_path, receipt)
        rechain(folder, ancestor, captured)
        assert_refuses_before_creation(bundle, baseline, launches, captured)
        if damage == "missing_prompt":
            assert not prompt_path.exists()  # The validator must not regenerate input evidence.
    else:
        assert observation.capture(bundle, IDS[captured], "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None) == 0
        assert len(launches) == captured + 1
        assert launches[-1]["prompt"] == plan["steps"][captured]["prompt"].encode()
        assert all(x["cwd"] == launches[0]["cwd"] for x in launches)
        assert prep.state(baseline["matter"]) == baseline["initial_state"]


@pytest.mark.parametrize("ancestor", ["brief", "mandatory"])
def test_f08_self_consistent_input_cannot_replace_frozen_content(prepared, monkeypatch, tmp_path, ancestor):
    bundle, baseline, _, plan, launches = setup_chain(prepared, monkeypatch, tmp_path)
    folder = bundle / "observations"
    prompt_path, receipt_path = [folder / f"{ancestor}.{suffix}" for suffix in ("prompt.txt", "receipt.json")]
    prompt, receipt = prompt_path.read_bytes(), prep.read_json(receipt_path)
    user = plan["steps"][IDS.index(ancestor)]["prompt"]
    wrong = [prompt.replace(user.encode(), b"A different ordinary user decision."), prompt + b"\n"]
    if ancestor == "brief":
        file_text = (bundle / "workflow" / WORKFLOW_FILES[-1]).read_bytes()
        wrong += [prompt.replace(BOUNDARY.encode(), b"Use a shell if needed.\n"),
                  prompt.replace(file_text, b"Incomplete resolved workflow.")]
    for changed in wrong:
        assert changed != prompt
        prompt_path.write_bytes(changed)
        value = deepcopy(receipt)
        value["prompt_sha256"] = hashlib.sha256(changed).hexdigest()
        json_write(receipt_path, value)
        rechain(folder, ancestor, 3)
        assert_refuses_before_creation(bundle, baseline, launches, 3)
    prompt_path.write_bytes(prompt)
    json_write(receipt_path, receipt)
    rechain(folder, ancestor, 3)
    assert observation.capture(bundle, "exclude", "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None) == 0


@pytest.mark.parametrize("ancestor", ["brief", "mandatory"])
@pytest.mark.parametrize("journal_disabled", [False, True])
def test_f08_complete_ancestor_launcher_is_required(prepared, monkeypatch, tmp_path, ancestor, journal_disabled):
    bundle, baseline, _, _, launches = setup_chain(prepared, monkeypatch, tmp_path, journal_disabled=journal_disabled)
    folder = bundle / "observations"
    path = folder / f"{ancestor}.receipt.json"
    receipt = prep.read_json(path)
    original = receipt["command"]
    mutants = [None, [], ["relative-codex"] + original[1:], original + ["--unsafe-extra"]]
    for flag in ("--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules"):
        mutants.append([part for part in original if part != flag])
    for old, new in ((MODEL, "wrong-model"), ('model_reasoning_effort="high"', 'model_reasoning_effort="xhigh"'),
                     ("project_doc_max_bytes=0", "project_doc_max_bytes=32768")):
        mutants.append([new if part == old else part for part in original])
    for prefix in ("mcp_servers.veqtor_nr03.command=", "mcp_servers.veqtor_nr03.args=",
                   "mcp_servers.veqtor_nr03.env.VEQTOR_TRACKED_CHANGE_AUTHOR=",
                   "mcp_servers.veqtor_nr03.env.VEQTOR_DISABLE_DECISION_RECORD="):
        mutants.append([prefix + '"different"' if part.startswith(prefix) else part for part in original])
    if ancestor == "mandatory":
        mutants.append(original[:-2] + ["synthetic-wrong-thread", "-"])
        mutants.append([part for part in original if part != "resume"])
    for command in mutants:
        assert command != original
        value = deepcopy(receipt)
        value["command"] = command
        json_write(path, value)
        rechain(folder, ancestor, 3)
        assert_refuses_before_creation(bundle, baseline, launches, 3)
    json_write(path, receipt)
    rechain(folder, ancestor, 3)
    assert observation.capture(bundle, "exclude", "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None) == 0


@pytest.mark.parametrize("fault_step", IDS)
def test_f08_fault_belongs_only_to_its_original_frozen_step(prepared, monkeypatch, tmp_path, fault_step):
    bundle, baseline, report, plan, launches = setup_chain(prepared, monkeypatch, tmp_path, fault_step=fault_step)
    folder = bundle / "observations"
    originals = {name: (folder / f"{name}.receipt.json").read_bytes() for name in IDS[:3]}
    # Each ancestor is checked with its own declared fault, even when the next
    # step uses the opposite launch. The bootstrap is compared, never executed.
    for ancestor in IDS[:3]:
        path = folder / f"{ancestor}.receipt.json"
        receipt = prep.read_json(path)
        normal = command_for("/synthetic/codex", report["python"], "a-brief" if ancestor == "brief" else "a-write",
            model=MODEL, reasoning_effort="high", thread_id=None if ancestor == "brief" else "synthetic-f08-root")
        injected = ["mcp_servers.veqtor_nr03.args=" + json.dumps(fault_args(baseline["matter"]), separators=(",", ":"))
                    if part.startswith("mcp_servers.veqtor_nr03.args=") else part for part in normal]
        expected = injected if ancestor == fault_step else normal
        assert receipt["command"] == launches[IDS.index(ancestor)]["command"] == expected
        assert receipt["fault"] == (plan["fault"] if ancestor == fault_step else None)
        for field, wrong in (("command", normal if ancestor == fault_step else injected),
                             ("fault", None if ancestor == fault_step else plan["fault"])):
            value = deepcopy(receipt)
            value[field] = wrong
            json_write(path, value)
            rechain(folder, ancestor, 3)
            assert_refuses_before_creation(bundle, baseline, launches, 3)
            for name, raw in originals.items():
                (folder / f"{name}.receipt.json").write_bytes(raw)
    assert observation.capture(bundle, "exclude", "/synthetic/codex", model=MODEL, reasoning_effort="high", source_profile=None) == 0
    for index, entry in enumerate(launches):
        args = next(part for part in entry["command"] if part.startswith("mcp_servers.veqtor_nr03.args="))
        assert ("_nr03_fault_fired" in args) == (IDS[index] == fault_step)
    assert prep.state(baseline["matter"]) == baseline["initial_state"]
    assert Path(launches[-1]["cwd"]) == folder / "client-brief"
