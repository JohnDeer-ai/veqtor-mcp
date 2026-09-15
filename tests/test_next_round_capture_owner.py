# SPDX-License-Identifier: Apache-2.0
"""Inert private capability and owned-direct-child controls; zero business work."""

import json
from copy import deepcopy
import hashlib
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import capture_nr03_app_server as capture
from check_codex_acceptance import EvidenceError, _digest, _file_sha256
from nr03_app_server import policy_hashes
from nr03_capture_owner import (
    VERSION,
    CaptureCancelled,
    OwnerChannel,
    private_runtime,
    receive_one,
    root_identity,
    send_one,
)
import test_next_round_source_profile as source

files, source_case = source.files, source.source_case


def context():
    return dict(
        H="1" * 40,
        T="2" * 40,
        stage="update",
        policy_sha256=policy_hashes(),
        inputs_sha256={
            k: "3" * 64
            for k in ("installation", "baseline", "plan", "parent_receipt", "prompt")
        },
        helpers_sha256={"revision_conflict_actor.py": "4" * 64},
    )


def watch_for(wait):
    c = wait["context"]
    return dict(
        schema_version=VERSION,
        H=c["H"],
        T=c["T"],
        started_ns=time.time_ns(),
        deadline_ns=time.time_ns() + 20 * 10**9,
        runtime_root=wait["root_identity"]["path"],
        root_identity=wait["root_identity"],
        parent_receipt_sha256=c["inputs_sha256"]["parent_receipt"],
        plan_sha256=c["inputs_sha256"]["plan"],
        installation_sha256=c["inputs_sha256"]["installation"],
        code_sha256=c["helpers_sha256"]["revision_conflict_actor.py"],
        wait_sha256=_digest(wait),
        run_id=wait["run_id"],
        connection_id=wait["connection_id"],
        nonce=wait["nonce"],
        capture_pid=wait["process"]["pid"],
        observer_pid=9876,
        observer_parent_pid=wait["process"]["parent_pid"],
    )


def responder(channel, folder, fault, saved):
    try:
        wait = receive_one(channel, time.monotonic() + 3)
        saved["wait"] = wait
        watch = watch_for(wait)
        if fault == "timeout":
            time.sleep(0.3)
            return
        if fault == "eof":
            return
        if fault == "watch_missing":
            del watch["nonce"]
        elif fault == "nonce":
            watch["nonce"] = "other"
        elif fault == "identity":
            watch["capture_pid"] += 1
        elif fault == "root":
            watch["runtime_root"] = "/other"
        elif fault == "root_mode":
            Path(watch["runtime_root"]).chmod(0o755)
        elif fault == "root_replaced":
            root = Path(watch["runtime_root"])
            root.rename(root.with_name(root.name + "-owned-original"))
            root.mkdir(mode=0o700)
            (root / "replacement.txt").write_text("Unowned replacement must survive")
        elif fault == "stale":
            watch["started_ns"] = wait["created_ns"] - 1
        elif fault == "expired":
            watch["deadline_ns"] = time.time_ns() - 1
        elif fault == "watch_type":
            watch["capture_pid"] = True
        elif fault == "helper":
            watch["code_sha256"] = "5" * 64
        elif fault == "plan":
            watch["plan_sha256"] = "5" * 64
        elif fault == "install":
            watch["installation_sha256"] = "5" * 64
        elif fault == "parent":
            watch["parent_receipt_sha256"] = "5" * 64
        elif fault == "run":
            watch["run_id"] = "different"
        elif fault == "connection":
            watch["connection_id"] = "different"
        path = folder / "watch-start.json"
        path.write_text(json.dumps(watch))
        path.chmod(0o600)
        if fault == "watch_mode":
            path.chmod(0o644)
        if fault == "watch_failed":
            (folder / "failure.json").write_text("{}").__class__
        decision = dict(
            schema_version=VERSION,
            action="RELEASE",
            wait_sha256=_digest(wait),
            watch_path=str(path),
            watch_sha256=_file_sha256(str(path)),
        )
        if fault == "decision_type":
            decision["action"] = True
        elif fault == "decision_wait":
            decision["wait_sha256"] = "0" * 64
        elif fault == "decision_extra":
            decision["extra"] = None
        elif fault == "watch_hash":
            decision["watch_sha256"] = "0" * 64
        elif fault == "abort":
            decision = dict(
                schema_version=VERSION, action="ABORT", wait_sha256=_digest(wait)
            )
        elif fault == "watch_partial":
            path.write_text('{"partial":')
        if fault in {"duplicate", "partial"}:
            data = json.dumps(decision).encode() + b"\n"
            channel.sendall(data * 2 if fault == "duplicate" else data[:-1])
            channel.shutdown(socket.SHUT_WR)
        else:
            send_one(channel, decision)
    except BaseException as error:
        saved["error"] = repr(error)
    finally:
        channel.close()


REFUSALS = (
    "abort",
    "eof",
    "timeout",
    "watch_missing",
    "nonce",
    "identity",
    "root",
    "root_mode",
    "root_replaced",
    "stale",
    "expired",
    "watch_type",
    "helper",
    "plan",
    "install",
    "parent",
    "run",
    "connection",
    "watch_mode",
    "watch_failed",
    "decision_type",
    "decision_wait",
    "decision_extra",
    "watch_hash",
    "watch_partial",
    "duplicate",
    "partial",
)


@pytest.mark.parametrize("fault", REFUSALS)
def test_pre_release_refusals_create_zero_native_including_version(
    tmp_path, monkeypatch, fault
):
    codex = tmp_path / "inert-not-executable"
    codex.write_text(capture.BUILD["commit"])
    monkeypatch.setattr(capture, "_file_sha256", lambda _: capture.BUILD["sha256"])
    launches = []

    def forbidden(*args, **kwargs):
        launches.append(args)
        raise AssertionError("Native creation before release")

    monkeypatch.setattr(capture.subprocess, "run", forbidden)
    monkeypatch.setattr(capture.subprocess, "Popen", forbidden)
    a, b = socket.socketpair()
    owner = OwnerChannel(a, context(), timeout=0.15 if fault == "timeout" else 2)
    saved = {}
    responder_thread = threading.Thread(
        target=responder, args=(b, tmp_path, fault, saved)
    )
    responder_thread.start()
    try:
        with pytest.raises((EvidenceError, OSError)):
            capture.capture(
                tmp_path, "update", [str(codex)], {}, "inert", tmp_path, {}, owner=owner
            )
    finally:
        responder_thread.join(3)
    assert not responder_thread.is_alive() and not launches and owner.state == "ABORTED"
    root = Path(saved["wait"]["root_identity"]["path"])
    if fault == "root_replaced":
        assert (
            root / "replacement.txt"
        ).read_text() == "Unowned replacement must survive"
    elif fault != "root_mode":
        assert not root.exists()


def test_one_release_and_explicit_cancel_linearization(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    a, b = socket.socketpair()
    owner = OwnerChannel(a, context())
    saved = {}
    worker = threading.Thread(target=responder, args=(b, tmp_path, None, saved))
    worker.start()
    metadata = owner.wait(runtime, "update", "run", "connection", policy_hashes())
    worker.join(3)
    assert owner.state == metadata["state"] == "RELEASED" and metadata["wait"][
        "root_identity"
    ] == root_identity(runtime)
    with pytest.raises(EvidenceError):
        owner.wait(runtime, "update", "run", "connection", policy_hashes())
    owner.cancel()
    with pytest.raises(CaptureCancelled, match="POST_RELEASE_CANCEL"):
        owner.check_cancelled()
    c, d = socket.socketpair()
    cancelled = OwnerChannel(c, context())
    cancelled.cancel()
    with pytest.raises(CaptureCancelled):
        cancelled.wait(runtime, "update", "run", "connection", policy_hashes())
    d.close()
    assert cancelled.state == "ABORTED"


def test_owned_child_is_reaped_if_recorder_setup_fails_after_release(
    tmp_path, monkeypatch
):
    binary = tmp_path / "inert-binary"
    binary.write_text(capture.BUILD["commit"])
    auth = tmp_path / "inert-auth"
    auth.mkdir()
    (auth / "auth.json").write_text('{"synthetic":true}')
    monkeypatch.setenv("CODEX_HOME", str(auth))
    monkeypatch.setattr(capture, "_file_sha256", lambda _: capture.BUILD["sha256"])
    calls = []

    def version(*args, **kwargs):
        calls.append("version")
        assert owner.state == "RELEASED"
        return SimpleNamespace(
            returncode=0, stdout=("codex-cli " + capture.BUILD["version"]).encode()
        )

    real_popen = subprocess.Popen
    children = []

    def popen(*args, **kwargs):
        calls.append("app-server-inert")
        assert owner.state == "RELEASED"
        child = real_popen(
            [sys.executable, "-I", "-B", "-c", "import sys;sys.stdin.read()"], **kwargs
        )
        children.append(child)
        return child

    def setup(*args, **kwargs):
        raise OSError("inert recorder construction failure")

    monkeypatch.setattr(capture.subprocess, "run", version)
    monkeypatch.setattr(capture.subprocess, "Popen", popen)
    monkeypatch.setattr(capture, "StdioCapture", setup)
    a, b = socket.socketpair()
    owner = OwnerChannel(a, context())
    saved = {}
    worker = threading.Thread(target=responder, args=(b, tmp_path, None, saved))
    worker.start()
    with pytest.raises(OSError, match="recorder construction"):
        capture.capture(
            tmp_path, "update", [str(binary)], {}, "inert", tmp_path, {}, owner=owner
        )
    worker.join(3)
    assert calls == ["version", "app-server-inert"] and children[0].poll() == 0
    runtime = Path(saved["wait"]["root_identity"]["path"])
    assert runtime.exists() and not (runtime / "auth.json").exists()
    status = json.loads((runtime / "QUARANTINE.json").read_text())
    assert status["cleanup_proven"] is True and status["success"] is False


def test_recorder_partial_construction_closes_open_files(tmp_path, monkeypatch):
    child = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import sys;sys.stdin.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    (tmp_path / "inert.jsonl").write_text("Existing original must survive")
    before = (tmp_path / "inert.jsonl").read_bytes()
    try:
        with pytest.raises(FileExistsError):
            capture.StdioCapture(child, tmp_path, "inert", "run", "connection")
    finally:
        capture.close_direct(child)
        child.stdout.close()
    assert child.poll() == 0 and (tmp_path / "inert.jsonl").read_bytes() == before
    assert (tmp_path / "inert.requests.jsonl").read_bytes() == b""


def test_direct_child_terminate_kill_and_cleanup_after_second_wait_failure(tmp_path):
    script = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('armed',flush=True);time.sleep(60)"
    child = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert child.stdout.readline() == b"armed\n"

    class BoundedOwned:
        stdin = child.stdin

        def wait(self, timeout):
            return child.wait(timeout=0.05)

        def terminate(self):
            child.terminate()

        def kill(self):
            child.kill()

    try:
        with pytest.raises(ExceptionGroup) as caught:
            capture.close_direct(BoundedOwned())
        assert len(caught.value.exceptions) == 2
        assert all(isinstance(error, subprocess.TimeoutExpired) for error in caught.value.exceptions)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        child.stdout.close()
    assert child.returncode == -signal.SIGKILL


def test_unknown_cleanup_retains_private_evidence_without_auth(tmp_path):
    with pytest.raises(RuntimeError):
        with private_runtime(tmp_path, Path(__file__).parents[1]) as (runtime, status):
            (runtime / "auth.json").write_text("synthetic-only")
            (runtime / "original.jsonl").write_bytes(b"{}\n")
            status.update(native_created=True, cleanup_proven=False)
            raise RuntimeError("inert cleanup uncertainty")
    assert (runtime / "original.jsonl").read_bytes() == b"{}\n" and not (
        runtime / "auth.json"
    ).exists()
    assert (
        json.loads((runtime / "QUARANTINE.json").read_text())["cleanup_proven"] is False
    )


@pytest.mark.parametrize(
    "fault",
    [
        "run",
        "root",
        "plan",
        "parent",
        "prompt",
        "watch_raw",
        "watch_value",
        "state",
        "clock",
    ],
)
def test_owner_metadata_cannot_replace_or_unbind_original_source(source_case, fault):
    f, producer = deepcopy(source_case)
    receipt, scope = f["receipt"], f["receipt"]["source"]
    now = time.time_ns()
    ctx = context()
    ctx["inputs_sha256"]["prompt"] = receipt["prompt_sha256"]
    receipt.update(
        step="update",
        plan_sha256="3" * 64,
        parent_receipt_sha256="3" * 64,
        installation_sha256="3" * 64,
        started_ns=now - 1,
        finished_ns=now + 10**9,
    )
    wait = dict(
        schema_version=VERSION,
        state="WAIT_OWNER",
        context=ctx,
        run_id=scope["run_id"],
        connection_id=scope["connection_id"],
        nonce="synthetic-owner-nonce",
        created_ns=now,
        process=dict(pid=100, parent_pid=101, uid=0),
        root_identity=dict(
            path=scope["runtime_root"], device=1, inode=2, uid=0, mode=0o700
        ),
    )
    watch = watch_for(wait)
    raw = json.dumps(watch)
    owner = dict(
        wait=wait,
        watch=watch,
        watch_raw=raw,
        state="RELEASED",
        released_ns=time.time_ns(),
        decision=dict(
            schema_version=VERSION,
            action="RELEASE",
            wait_sha256=_digest(wait),
            watch_path="/synthetic/watch-start.json",
            watch_sha256=hashlib.sha256(raw.encode()).hexdigest(),
        ),
    )
    scope["owner"] = owner
    source.rebind(f, rebuild_transport=False)
    assert len(source.assess((f, producer))) == 5
    original = deepcopy(f)
    if fault == "run":
        owner["wait"]["run_id"] = "other"
    elif fault == "root":
        owner["wait"]["root_identity"]["path"] = "/other"
    elif fault in {"plan", "parent", "prompt"}:
        field = dict(
            plan="plan_sha256", parent="parent_receipt_sha256", prompt="prompt_sha256"
        )[fault]
        receipt[field] = "0" * 64
    elif fault == "watch_raw":
        owner["watch_raw"] = "{}"
    elif fault == "watch_value":
        owner["watch"]["nonce"] = "other"
    elif fault == "state":
        owner["state"] = "ABORTED"
    elif fault == "clock":
        owner["released_ns"] = now - 2
    source.rebind(f, rebuild_transport=False)
    with pytest.raises(EvidenceError):
        source.assess((f, producer))
    assert len(source.assess((original, producer))) == 5
