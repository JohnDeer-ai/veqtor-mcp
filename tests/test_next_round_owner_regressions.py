# SPDX-License-Identifier: Apache-2.0
"""F19 owned-resource cleanup and F20 independent final-authority controls."""

from copy import deepcopy
import hashlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from contextlib import contextmanager

import pytest

import capture_nr03_app_server as cap
from check_codex_acceptance import EvidenceError, _digest
from nr03_capture_owner import OWNER_HELPERS, validate_owner_authority
import test_next_round_capture_owner as own
import test_next_round_source_profile as source

files, source_case = source.files, source.source_case


class FailedClose:
    def __init__(self):
        self.attempts = 0

    def close(self):
        self.attempts += 1
        raise OSError("inert close failure")


def inert_child():
    return subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import sys;sys.stdin.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )


def test_close_failure_disposes_other_handles_and_retries_pending(tmp_path):
    child = inert_child()
    stream = cap.StdioCapture(child, tmp_path, "close", "run", "connection")
    originals = list(stream.files.values())
    failure = FailedClose()
    stream.files["sent"] = failure
    with pytest.raises(OSError, match="inert close failure"):
        stream.close()
    assert child.poll() == 0 and not stream.reader.is_alive()
    assert (
        all(f.closed for f in originals) and child.stdout.closed and child.stdin.closed
    )
    assert not stream.cleanup_proven and not stream.closed
    with pytest.raises(BaseExceptionGroup):
        stream.close()
    assert failure.attempts == 2 and not stream.cleanup_proven
    stream.files["sent"] = originals[0]
    with pytest.raises(BaseExceptionGroup):
        stream.close()
    assert (
        stream.cleanup_proven and stream.closed
    )  # Disposition fixed, errors retained.
    restored_folder = tmp_path / "restored"
    restored_folder.mkdir()
    restored = cap.StdioCapture(
        inert_child(), restored_folder, "good", "run", "connection"
    )
    assert restored.close() == 0 and restored.close() == 0 and restored.cleanup_proven


def test_fdopen_failure_closes_immediately_owned_raw_fd(tmp_path, monkeypatch):
    child = inert_child()
    allocated = []

    def broken(fd, *args, **kwargs):
        allocated.append(fd)
        raise OSError("inert fdopen failure")

    try:
        with monkeypatch.context() as m:
            m.setattr(cap.os, "fdopen", broken)
            with pytest.raises(OSError, match="inert fdopen failure") as caught:
                cap.StdioCapture(child, tmp_path, "fdopen", "run", "connection")
        assert caught.value.nr03_setup_cleanup_proven is True
        with pytest.raises(OSError):
            os.fstat(allocated[0])
    finally:
        cap.close_direct(child)
        child.stdout.close()
    restored = tmp_path / "restored"
    restored.mkdir()
    stream = cap.StdioCapture(inert_child(), restored, "good", "run", "connection")
    assert stream.close() == 0 and stream.cleanup_proven


@pytest.mark.parametrize("fault", ["recorder", "stderr"])
def test_operation_and_close_failures_keep_private_quarantine_without_auth(
    tmp_path, monkeypatch, fault
):
    binary = tmp_path / "inert-binary"
    binary.write_text(cap.BUILD["commit"])
    auth = tmp_path / "fixture-auth"
    auth.mkdir()
    (auth / "auth.json").write_text('{"synthetic":true}')
    monkeypatch.setenv("CODEX_HOME", str(auth))
    monkeypatch.setattr(cap, "_file_sha256", lambda _: cap.BUILD["sha256"])
    monkeypatch.setattr(
        cap.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout=("codex-cli " + cap.BUILD["version"]).encode()
        ),
    )
    real_popen, real_runtime, real_stream = (
        subprocess.Popen,
        cap.private_runtime,
        cap.StdioCapture,
    )
    roots, streams, stderr_streams = [], [], []
    real_fdopen, real_open = os.fdopen, os.open
    stderr_fds = set()

    def allocate(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if str(path).endswith("fixture.stderr.txt"):
            stderr_fds.add(fd)
        return fd

    class BrokenStderr(FailedClose):
        def __init__(self, underlying):
            super().__init__()
            self.underlying = underlying

        def __getattr__(self, name):
            return getattr(self.underlying, name)

    def fdopen(fd, *args, **kwargs):
        handle = real_fdopen(fd, *args, **kwargs)
        if fault == "stderr" and fd in stderr_fds:
            stderr_fds.remove(fd)
            handle = BrokenStderr(handle)
            stderr_streams.append(handle)
        return handle

    monkeypatch.setattr(cap.os, "open", allocate)
    monkeypatch.setattr(cap.os, "fdopen", fdopen)
    monkeypatch.setattr(
        cap.subprocess,
        "Popen",
        lambda *a, **k: real_popen(
            [sys.executable, "-I", "-B", "-c", "import sys;sys.stdin.read()"], **k
        ),
    )

    @contextmanager
    def runtime(*args):
        with real_runtime(*args) as value:
            roots.append(value[0])
            yield value

    class BrokenStream(real_stream):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if fault == "recorder":
                self.files["sent"] = FailedClose()
            streams.append(self)

        def send(self, *args, **kwargs):
            raise ValueError("inert primary operation failure")

    monkeypatch.setattr(cap, "private_runtime", runtime)
    monkeypatch.setattr(cap, "StdioCapture", BrokenStream)
    try:
        with pytest.raises(ExceptionGroup) as caught:
            cap.capture(tmp_path, "fixture", [str(binary)], {}, "inert", tmp_path, {})
    finally:
        for error_stream in stderr_streams:
            state = (error_stream.attempts, error_stream.closed)
            error_stream.underlying.close()  # Test owns the deliberately blocked handle.
            assert state == (1, False)
    assert {str(e) for e in caught.value.exceptions} == {
        "inert primary operation failure",
        "inert close failure",
    }
    assert streams[0].process.poll() == 0 and streams[0].process.stdout.closed
    assert all(f.closed for f in streams[0].owned_outputs)
    root = roots[0]
    assert root.exists() and not (root / "auth.json").exists()
    marker = json.loads((root / "QUARANTINE.json").read_text())
    assert marker["cleanup_proven"] is False and marker["success"] is False
    assert (
        len(marker["cleanup_failures"]) == 2
        and (root / "capture/fixture.transport.jsonl").exists()
    )


def owner_fixture(f):
    import time

    receipt, scope = f["receipt"], f["receipt"]["source"]
    now = time.time_ns()
    expected = own.context()
    expected["helpers_sha256"] = {
        n: hashlib.sha256(n.encode()).hexdigest() for n in OWNER_HELPERS
    }
    expected["inputs_sha256"]["prompt"] = receipt["prompt_sha256"]
    receipt.update(
        step="update",
        plan_sha256="3" * 64,
        parent_receipt_sha256="3" * 64,
        installation_sha256="3" * 64,
        started_ns=now - 1,
        finished_ns=now + 10**9,
    )
    wait = dict(
        schema_version=own.VERSION,
        state="WAIT_OWNER",
        context=deepcopy(expected),
        run_id=scope["run_id"],
        connection_id=scope["connection_id"],
        nonce="synthetic",
        created_ns=now,
        process=dict(pid=100, parent_pid=101, uid=0),
        root_identity=dict(
            path=scope["runtime_root"], device=1, inode=2, uid=0, mode=0o700
        ),
    )
    watch = own.watch_for(wait)
    scope["owner"] = dict(
        wait=wait,
        watch=watch,
        state="RELEASED",
        released_ns=time.time_ns(),
        decision=dict(
            schema_version=own.VERSION,
            action="RELEASE",
            watch_path="/synthetic/watch.json",
        ),
    )
    rebind_owner(f)
    return expected


def rebind_owner(f):
    owner = f["receipt"]["source"]["owner"]
    for k in ("H", "T"):
        owner["watch"][k] = owner["wait"]["context"][k]
    owner["watch"]["wait_sha256"] = owner["decision"]["wait_sha256"] = _digest(
        owner["wait"]
    )
    owner["watch_raw"] = json.dumps(owner["watch"])
    owner["decision"]["watch_sha256"] = hashlib.sha256(
        owner["watch_raw"].encode()
    ).hexdigest()
    source.rebind(f, rebuild_transport=False)


@pytest.mark.parametrize(
    "fault", ["H", "T", "baseline", "helper_missing", "helper_changed"]
)
def test_self_consistent_owner_must_match_independent_final_authority(
    source_case, fault
):
    f, producer = deepcopy(source_case)
    expected = owner_fixture(f)
    good = deepcopy(f)

    def assess(candidate):
        assert len(source.assess((candidate, producer))) == 5
        scope = candidate["receipt"]["source"]
        validate_owner_authority(scope["owner"], scope, candidate["receipt"], expected)

    assess(good)
    context = f["receipt"]["source"]["owner"]["wait"]["context"]
    if fault in ("H", "T"):
        context[fault] = "f" * 40
    elif fault == "baseline":
        context["inputs_sha256"]["baseline"] = "f" * 64
    elif fault == "helper_missing":
        del context["helpers_sha256"]["capture_owner.py"]
    else:
        context["helpers_sha256"]["capture_owner.py"] = "f" * 64
    rebind_owner(f)
    with pytest.raises(
        EvidenceError, match="final owner context differs from frozen authority"
    ):
        assess(f)
    assess(good)
