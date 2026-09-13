# SPDX-License-Identifier: Apache-2.0
"""Synthetic artifact/receipt controls, never installed/native acceptance evidence."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import check_position_install as install  # noqa: E402
import check_position_acceptance as checker  # noqa: E402
import capture_position_session as capture  # noqa: E402
from test_position_acceptance import build  # noqa: E402


def test_install_checker_positive_and_weaker_artifact_substitutes(tmp_path, monkeypatch):
    root = tmp_path / "source"
    installed = tmp_path / "installed"
    root.mkdir()
    installed.mkdir()
    files = {"veqtor_mcp/__init__.py": b"# synthetic MCP source\n",
             "veqtor_docx/__init__.py": b"# synthetic DOCX source\n"}
    for name, data in files.items():
        for parent in (root / "src", installed):
            (parent / name).parent.mkdir(exist_ok=True, parents=True)
            (parent / name).write_bytes(data)
    (root / "pyproject.toml").write_text('''[project]
version = "0.4.2.dev0"
[tool.hatch.build.targets.wheel]
include = ["/src/veqtor_mcp/__init__.py", "/src/veqtor_docx/__init__.py"]
[tool.hatch.build.targets.sdist]
include = ["/pyproject.toml", "/src/veqtor_mcp/__init__.py", "/src/veqtor_docx/__init__.py"]
''')
    sd = tmp_path / "synthetic.tar.gz"
    with tarfile.open(sd, "w:gz") as archive:
        for name, data in {**{"src/" + n: d for n, d in files.items()},
                           "pyproject.toml": (root / "pyproject.toml").read_bytes(), "PKG-INFO": b"synthetic"}.items():
            member = tarfile.TarInfo("veqtor_mcp-0.4.2.dev0/" + name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    dist = "veqtor_mcp-0.4.2.dev0.dist-info"
    wheel_files = files | {f"{dist}/{n}": b"synthetic metadata" for n in
        ("METADATA", "WHEEL", "RECORD", "entry_points.txt", "licenses/LICENSE", "licenses/NOTICE")}
    wheel = tmp_path / "synthetic.whl"

    def write_wheel(values):
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, data in values.items():
                archive.writestr(name, data)

    write_wheel(wheel_files)
    manifest = {"schema": "source_snapshot.v1", "files": [{"path": n, "sha256": hashlib.sha256(d).hexdigest()}
                                                        for n, d in sorted(files.items())]}
    build_id = "source-snapshot-v1-sha256:" + hashlib.sha256(json.dumps(manifest, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    probe = dict(version="0.4.2.dev0", distribution_version="0.4.2.dev0", build=build_id,
        roots={n: str(installed / n) for n in ("veqtor_mcp", "veqtor_docx")},
        tools=["list_rounds", "extract_redlines", "inspect_document", "map_rounds", "trace_paragraph_history",
               "preflight_edits", "apply_edits", "verify_quote", "export_decision_record",
               "read_deal_positions", "mutate_deal_positions"])
    monkeypatch.setattr(install, "git", lambda _, *args: "" if args[0] == "status" else "a" * 40 if args[-1] == "HEAD" else "b" * 40)
    monkeypatch.setattr(install.subprocess, "check_output", lambda *a, **kw: json.dumps(probe).encode())
    args = (root, "a" * 40, "b" * 40, wheel, sd, tmp_path / "env/bin/python")
    assert install.verify(*args)["producer"]["build"] == build_id
    for replacement in ({k: v for k, v in wheel_files.items() if k != "veqtor_mcp/__init__.py"},
                        wheel_files | {"private-client.docx": b"private"},
                        wheel_files | {"veqtor_mcp/.veqtor/deal-positions.json": b"private"},
                        wheel_files | {"veqtor_mcp/__init__.py": b"wrong producer"}):
        write_wheel(replacement)
        with pytest.raises(install.InstallError):
            install.verify(*args)
    write_wheel(wheel_files)
    target = installed / "veqtor_mcp/__init__.py"
    target.write_bytes(b"another installed source")
    with pytest.raises(install.InstallError):
        install.verify(*args)
    target.write_bytes(files["veqtor_mcp/__init__.py"])
    for key, value in [("version", "0.4.0"), ("distribution_version", "0.4.0"),
                       ("build", "source-snapshot-unavailable"), ("tools", probe["tools"][:-1])]:
        saved = probe[key]
        probe[key] = value
        with pytest.raises(install.InstallError):
            install.verify(*args)
        probe[key] = saved
    assert install.verify(*args)["producer"]["build"] == build_id


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    # Synthetic envelopes and observer receipts test the gate's structure.
    observations = {}

    def observe(name, phase, expected):
        facts = observations.setdefault(name, {})
        # Observe the real synthetic filesystem at each session boundary, rather
        # than manufacturing inventories only for a subset of the scenarios.
        facts[f"docx_{phase}"] = capture.docx_hashes(expected)
        facts[f"journal_{phase}"] = capture.journal_hash(expected)
        if name == "moved":
            facts[f"relocation_{phase}"] = capture.relocation_state(expected)

    events, expected = build(tmp_path, monkeypatch, observer=observe)
    directory = tmp_path / "bundle"
    directory.mkdir()
    raw = json.dumps(expected).encode()
    (directory / "baseline.json").write_bytes(raw)
    location = tmp_path / "synthetic-installed" / "veqtor_mcp"
    location.mkdir(parents=True)
    (location / "__init__.py").write_bytes(b"# synthetic installation fixture\n")
    installation = dict(schema_version="veqtor_position_install.v1", commit="a" * 40, tree="b" * 40,
        producer=expected["producer"], source_files={"veqtor_mcp/__init__.py": checker.sha((location / "__init__.py").read_bytes())},
        installed_roots={"veqtor_mcp": str(location)}, python="/synthetic/installed/python",
        source_root="/synthetic/source", wheel="/synthetic/wheel", sdist="/synthetic/sdist")
    monkeypatch.setattr(install, "verify", lambda *a: deepcopy(installation))
    installed_raw = json.dumps(installation).encode()
    (directory / "installation.json").write_bytes(installed_raw)
    start = (directory / "baseline.json").stat().st_mtime_ns + 1000000
    ordering = ["save", "confirm", "update", "restart", "withdraw", "moved", "changed", "missing",
                "journal_disabled", "journal_corrupt", "other", "conflict_a", "conflict_b", "conflict_final",
                "retry", "first_a", "first_b", "first_final", "copy_independent"]
    for index, name in enumerate(ordering):
        data = b"\n".join(json.dumps(e).encode() for e in events[name])
        prompt = checker.restart_prompt(expected).encode() if name == "restart" else b"Synthetic capture fixture"
        if name == "moved":
            prompt = checker.current_document_prompt(expected).encode()
        command = ["/synthetic/codex", "exec", "--json", "--skip-git-repo-check", "--ignore-user-config", "--ephemeral",
            "-c", 'mcp_servers.veqtor_nr02.command="/synthetic/installed/python"',
            "-c", 'mcp_servers.veqtor_nr02.args=["-I","-m","veqtor_mcp.server"]',
            "-c", 'mcp_servers.veqtor_nr02.env.VEQTOR_TRACKED_CHANGE_AUTHOR="Veqtor Acceptance"',
            "-c", 'mcp_servers.veqtor_nr02.env.VEQTOR_DISABLE_DECISION_RECORD=' + json.dumps("1" if name == "journal_disabled" else "0"),
            "--model", "gpt-6-astra", "-c", 'model_reasoning_effort="ultra"', "-"]
        first_ns = start + index * 10000
        if name in {"conflict_b", "first_b"}:
            first_ns -= 9000
        receipt = dict(baseline_sha256=checker.sha(raw), installation_sha256=checker.sha(installed_raw),
            events_sha256=checker.sha(data), prompt_sha256=checker.sha(prompt), exit_code=0,
            started_ns=first_ns, finished_ns=first_ns + 5000, command=command,
            server_python=installation["python"], client_selection=deepcopy(expected["client_selection"]),
            **observations[name])
        for suffix, payload in [("jsonl", data), ("prompt.txt", prompt), ("receipt.json", json.dumps(receipt).encode())]:
            (directory / f"{name}.{suffix}").write_bytes(payload)
    return directory


def test_complete_bundle_positive_control(bundle):
    assert checker.check_bundle(bundle, model="gpt-6-astra", reasoning_effort="ultra")["status"] == "passed"


@pytest.mark.parametrize("mutation", ["baseline_time", "stale_install", "extra_override", "no_overlap", "wrong_prompt",
    "prose_only", "docx_changed", "journal_not_corrupt", "changed_setup_missing", "missing_receipt", "missing_installed_file", "alternate_document_missing"])
def test_missing_or_weaker_observer_evidence_fails(bundle, mutation):
    name = {"no_overlap": "conflict_b", "wrong_prompt": "restart", "alternate_document_missing": "moved", "journal_not_corrupt": "journal_corrupt",
            "changed_setup_missing": "changed"}.get(mutation, "save")
    path = bundle / f"{name}.receipt.json"
    receipt = json.loads(path.read_bytes())
    if mutation == "baseline_time":
        receipt["started_ns"] = 0
    elif mutation == "stale_install":
        receipt["installation_sha256"] = "0" * 64
    elif mutation == "extra_override":
        receipt["command"].extend(["-c", 'mcp_servers.veqtor_nr02.command="/other/server"'])
    elif mutation == "no_overlap":
        receipt["started_ns"] += 9000
        receipt["finished_ns"] += 9000
    elif mutation == "wrong_prompt":
        (bundle / "restart.prompt.txt").write_bytes(b"Readback the 30 day payment we discussed")
        receipt["prompt_sha256"] = checker.sha((bundle / "restart.prompt.txt").read_bytes())
    elif mutation == "alternate_document_missing":
        (bundle / "moved.prompt.txt").write_bytes(b"Read the moved matter")
        receipt["prompt_sha256"] = checker.sha((bundle / "moved.prompt.txt").read_bytes())
    elif mutation == "prose_only":
        (bundle / "save.jsonl").write_bytes(b'{"type":"agent_message","text":"all passed"}')
        receipt["events_sha256"] = checker.sha((bundle / "save.jsonl").read_bytes())
    elif mutation == "docx_changed":
        receipt["docx_after"] = {}
    elif mutation == "journal_not_corrupt":
        receipt["journal_before"] = receipt["journal_after"] = None
    elif mutation == "changed_setup_missing":
        expected = json.loads((bundle / "baseline.json").read_bytes())
        receipt["docx_before"] = {str(Path(expected["folders"]["moved"]) / n): h for n, h in expected["source_files"].items()}
        receipt["docx_after"] = deepcopy(receipt["docx_before"])
    elif mutation == "missing_receipt":
        path.unlink()
    elif mutation == "missing_installed_file":
        installed = json.loads((bundle / "installation.json").read_bytes())
        (Path(installed["installed_roots"]["veqtor_mcp"]) / "__init__.py").unlink()
    if mutation != "missing_receipt":
        path.write_text(json.dumps(receipt))
    with pytest.raises((checker.EvidenceError, OSError)):
        checker.check_bundle(bundle, model="gpt-6-astra", reasoning_effort="ultra")
