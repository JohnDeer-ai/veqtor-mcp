# SPDX-License-Identifier: Apache-2.0
"""Real bytecode loading controls plus synthetic receipt refusal controls."""
import json
import subprocess
import sys

import pytest

from test_position_gate_artifacts import artifact_case as artifact_case, bundle as bundle, checker, install
from position_source_launch import SERVER_ARGS, SOURCE_ONLY_BOOTSTRAP


@pytest.mark.parametrize("mode", ["TIMESTAMP", "UNCHECKED_HASH"])
def test_source_only_launch_ignores_valid_stale_caches(tmp_path, mode):
    # Both ordinary imports (installation probe) and runpy (server entry point)
    # must compile source, even when Python accepts a wrong same-sized cache.
    module = tmp_path / "synthetic_producer.py"
    module.write_text("print('source')\n")
    writer = """import importlib.util, os, pathlib, py_compile, sys
source = pathlib.Path(sys.argv[1])
previous = source.with_name('previous.py')
previous.write_text("print('cached')\\n")
os.utime(previous, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns))
py_compile.compile(str(previous), cfile=importlib.util.cache_from_source(str(source)),
    dfile=str(source), doraise=True, invalidation_mode=getattr(py_compile.PycInvalidationMode, sys.argv[2]))
"""
    subprocess.run([sys.executable, "-I", "-B", "-c", writer, str(module), mode], check=True)
    cache = next((tmp_path / "__pycache__").glob("synthetic_producer.*.pyc"))
    saved = cache.read_bytes()
    for action in ("import synthetic_producer", "import runpy; runpy.run_module('synthetic_producer', run_name='__main__')"):
        probe = f"import sys; sys.path.insert(0, {str(tmp_path)!r})\n" + action
        def run(prefix):
            return subprocess.check_output([sys.executable, "-I", "-B", "-c", prefix + probe]).decode().strip()
        assert run("") == "cached"
        assert run(SOURCE_ONLY_BOOTSTRAP) == "source"
    assert cache.read_bytes() == saved and module.read_text() == "print('source')\n"
    cache.unlink()
    assert run("") == run(SOURCE_ONLY_BOOTSTRAP) == "source"


@pytest.mark.parametrize("args", [["-I", "-m", "veqtor_mcp.server"],
    ["-I", "-B", "-m", "veqtor_mcp.server"], ["-I", "-B", "-c", SERVER_ARGS[-1].replace(SOURCE_ONLY_BOOTSTRAP, "")]])
def test_capture_evidence_requires_source_only_server_launch(bundle, args):
    assert checker.check_bundle(bundle, model="gpt-6-astra", reasoning_effort="ultra")["status"] == "passed"
    path = bundle / "save.receipt.json"
    saved = path.read_bytes()
    receipt = json.loads(saved)
    receipt["command"] = ["mcp_servers.veqtor_nr02.args=" + json.dumps(args, separators=(",", ":"))
        if arg.startswith("mcp_servers.veqtor_nr02.args=") else arg for arg in receipt["command"]]
    path.write_text(json.dumps(receipt))
    with pytest.raises(checker.EvidenceError, match="exact isolated producer command"):
        checker.check_bundle(bundle, model="gpt-6-astra", reasoning_effort="ultra")
    path.write_bytes(saved)
    assert checker.check_bundle(bundle, model="gpt-6-astra", reasoning_effort="ultra")["status"] == "passed"


def test_install_probe_uses_source_only_launch(artifact_case, monkeypatch):
    commands = []
    def probe(command, **kwargs):
        commands.append(command)
        return json.dumps(artifact_case.probe).encode()
    monkeypatch.setattr(install.subprocess, "check_output", probe)
    install.verify(*artifact_case.args)
    assert commands[0][1:4] == ["-I", "-B", "-c"]
    assert commands[0][4].startswith(SOURCE_ONLY_BOOTSTRAP)
