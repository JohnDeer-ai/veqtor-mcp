# SPDX-License-Identifier: Apache-2.0
"""One cooperative pre-native owner capability; no descendant supervision.

The private inherited socket carries one WAIT and one final decision, each
terminated by write EOF. Observer records are metadata, never source protocol.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager

from check_codex_acceptance import _digest, _file_sha256, _require
from nr03_model_delivery import decoded

VERSION = "nr03-capture-owner.v1"
MAX_MESSAGE = 131072


class CaptureCancelled(Exception):
    pass


def root_identity(path):
    path = Path(path)
    _require(path.is_absolute() and str(path) == str(path.resolve()) and not path.is_symlink(),
             "owner root not canonical")
    info = path.lstat()
    _require(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.getuid(),
             "owner root identity/mode differs")
    return dict(path=str(path), device=info.st_dev, inode=info.st_ino, uid=info.st_uid, mode=0o700)


def validate_context(context):
    _require(isinstance(context, dict) and set(context) == {"H", "T", "stage", "policy_sha256", "inputs_sha256", "helpers_sha256"}
             and all(isinstance(context[k], str) and re.fullmatch("[0-9a-f]{40}", context[k]) for k in ("H", "T"))
             and context["stage"] == "update", "owner context envelope differs")
    for key in ("policy_sha256", "inputs_sha256", "helpers_sha256"):
        values = context[key]
        _require(isinstance(values, dict) and values and all(isinstance(k, str) and k and isinstance(v, str)
                 and re.fullmatch("[0-9a-f]{64}", v) for k, v in values.items()), "owner input pins malformed")
    _require(set(context["inputs_sha256"]) == {"installation", "baseline", "plan", "parent_receipt", "prompt"}
             and "revision_conflict_actor.py" in context["helpers_sha256"], "owner required input/helper pins missing")


def send_one(channel, value):
    raw = json.dumps(value, separators=(",", ":")).encode() + b"\n"
    _require(len(raw) <= MAX_MESSAGE, "owner message exceeds bound")
    channel.sendall(raw)
    channel.shutdown(socket.SHUT_WR)


def receive_one(channel, deadline, cancelled=lambda: False):
    data = bytearray()
    while True:
        if cancelled():
            raise CaptureCancelled("owner cancellation before decision")
        remaining = deadline - time.monotonic()
        _require(remaining > 0, "owner channel timeout")
        channel.settimeout(min(0.1, remaining))
        try:
            part = channel.recv(min(4096, MAX_MESSAGE + 1 - len(data)))
        except socket.timeout:
            continue
        if not part:
            break
        data.extend(part)
        _require(len(data) <= MAX_MESSAGE, "owner message exceeds bound")
    _require(data.endswith(b"\n") and data.count(b"\n") == 1, "owner missing/partial/duplicate message")
    value = decoded(bytes(data).decode())
    _require(isinstance(value, dict), "owner message malformed")
    return value


def validate_watch(wait, watch, *, now_ns=None):
    now_ns = time.time_ns() if now_ns is None else now_ns
    fields = {"schema_version", "H", "T", "started_ns", "deadline_ns", "runtime_root", "root_identity",
              "parent_receipt_sha256", "plan_sha256", "installation_sha256", "code_sha256", "wait_sha256",
              "run_id", "connection_id", "nonce", "capture_pid", "observer_pid", "observer_parent_pid"}
    _require(isinstance(watch, dict) and set(watch) == fields and watch["schema_version"] == VERSION,
             "owner watch envelope differs")
    context = wait["context"]
    expected = dict(H=context["H"], T=context["T"], runtime_root=wait["root_identity"]["path"],
        root_identity=wait["root_identity"], parent_receipt_sha256=context["inputs_sha256"]["parent_receipt"],
        plan_sha256=context["inputs_sha256"]["plan"], installation_sha256=context["inputs_sha256"]["installation"],
        code_sha256=context["helpers_sha256"]["revision_conflict_actor.py"], wait_sha256=_digest(wait),
        run_id=wait["run_id"], connection_id=wait["connection_id"], nonce=wait["nonce"],
        capture_pid=wait["process"]["pid"], observer_parent_pid=wait["process"]["parent_pid"])
    _require(all(_digest(watch[k]) == _digest(v) for k, v in expected.items()), "owner watch context differs")
    _require(type(watch["observer_pid"]) is int and watch["observer_pid"] > 0
             and all(type(watch[k]) is int for k in ("started_ns", "deadline_ns"))
             and wait["created_ns"] <= watch["started_ns"] <= now_ns < watch["deadline_ns"]
             and watch["deadline_ns"] <= watch["started_ns"] + 300 * 10**9, "owner watch stale or expired")


class OwnerChannel:
    def __init__(self, channel, context, *, timeout=60):
        validate_context(context)
        _require(channel.family == socket.AF_UNIX and channel.type == socket.SOCK_STREAM
                 and 0 < timeout <= 300, "owner private channel/timeout differs")
        os.set_inheritable(channel.fileno(), False)
        self.channel, self.context, self.timeout = channel, context, timeout
        self.state, self.metadata = "NEW", None
        self.cancelled = threading.Event()
        self.lock = threading.Lock()

    def cancel(self):
        # One lock establishes which decision won. Signals only set this event;
        # they never raise between Popen returning and ownership assignment.
        self.cancelled.set()

    def check_cancelled(self):
        with self.lock:
            if self.cancelled.is_set():
                self.state = "POST_RELEASE_CANCEL" if self.state in {"RELEASED", "POST_RELEASE_CANCEL"} else "ABORTED"
                if self.metadata is not None:
                    self.metadata["state"] = self.state
                raise CaptureCancelled(self.state)

    def wait(self, runtime, stage, run, connection, policies):
        _require(self.state == "NEW" and self.context["stage"] == stage
                 and self.context["policy_sha256"] == policies, "owner attempt reused or policy differs")
        identity = root_identity(runtime)
        wait = dict(schema_version=VERSION, state="WAIT_OWNER", context=self.context, run_id=run, connection_id=connection,
            nonce=str(uuid.uuid4()), process=dict(pid=os.getpid(), parent_pid=os.getppid(), uid=os.getuid()),
            root_identity=identity, created_ns=time.time_ns())
        self.state = "WAIT_OWNER"
        self.metadata = dict(wait=wait, decision=None, state=self.state)
        try:
            self.check_cancelled()
            send_one(self.channel, wait)
            decision = receive_one(self.channel, time.monotonic() + self.timeout, self.cancelled.is_set)
            self.metadata["decision"] = decision
            _require(set(decision) in ({"schema_version", "action", "wait_sha256"},
                     {"schema_version", "action", "wait_sha256", "watch_path", "watch_sha256"})
                     and decision["schema_version"] == VERSION and decision["wait_sha256"] == _digest(wait),
                     "owner decision context differs")
            _require(decision["action"] == "RELEASE" and "watch_path" in decision, "owner aborted attempt")
            path = Path(decision["watch_path"])
            _require(path.is_absolute() and path.resolve() == path and not path.is_symlink()
                     and stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_uid == os.getuid()
                     and not (path.parent / "failure.json").exists(), "owner watch file absent/unsafe/failed")
            raw = path.read_bytes()
            _require(hashlib.sha256(raw).hexdigest() == decision["watch_sha256"], "owner watch byte binding differs")
            watch = decoded(raw.decode())
            validate_watch(wait, watch)
            self.metadata["watch"] = watch
            self.metadata["watch_raw"] = raw.decode()
            _require(root_identity(runtime) == identity, "owner root replaced before release")
            with self.lock:
                if self.cancelled.is_set():
                    raise CaptureCancelled("ABORTED")
                self.state = "RELEASED"
            self.metadata["state"] = self.state
            self.metadata["released_ns"] = time.time_ns()
            return self.metadata
        except BaseException:
            self.state = "POST_RELEASE_CANCEL" if self.state == "RELEASED" else "ABORTED"
            self.metadata["state"] = self.state
            raise
        finally:
            self.channel.close()


def observation_context(bundle, stage, report, parent_hash, prompt, helpers):
    """Capture computes current input pins, independent of wrapper assertions."""
    from nr03_app_server import policy_hashes
    bundle = Path(bundle)
    value = dict(H=report["commit"], T=report["tree"], stage=stage, policy_sha256=policy_hashes(),
        inputs_sha256=dict(installation=_file_sha256(str(bundle / "installation.json")),
            baseline=_file_sha256(str(bundle / "baseline.json")), plan=_file_sha256(str(bundle / "observations/plan.json")),
            parent_receipt=parent_hash, prompt=hashlib.sha256(prompt.encode()).hexdigest()), helpers_sha256=helpers)
    validate_context(value)
    return value


@contextmanager
def private_runtime(folder, checkout):
    """Keep failed private originals; never delete a replaced root or live recorder."""
    runtime = Path(tempfile.mkdtemp(prefix="veqtor-nr03-runtime-")).resolve()
    os.chmod(runtime, 0o700)
    identity = root_identity(runtime)
    _require(not runtime.is_relative_to(folder) and not runtime.is_relative_to(checkout),
             "source private runtime must be outside evidence and checkout")
    status = dict(native_created=False, cleanup_proven=True, success=False)
    try:
        yield runtime, status
    finally:
        try:
            same = root_identity(runtime) == identity
        except (OSError, ValueError):
            same = False
        if same:
            # Only our canonical root; unlinking a name never follows its target.
            (runtime / "auth.json").unlink(missing_ok=True)
            if status["cleanup_proven"] and (status["success"] or not status["native_created"]):
                shutil.rmtree(runtime)
            else:
                marker = runtime / "QUARANTINE.json"
                marker.write_text(json.dumps(dict(runtime_root=str(runtime), **status)))
                os.chmod(marker, 0o600)


def validate_source_owner(owner, source, receipt):
    _require(isinstance(owner, dict) and set(owner) == {"wait", "decision", "watch", "watch_raw", "state", "released_ns"}
             and owner["state"] == "RELEASED", "source owner not released")
    wait, decision = owner["wait"], owner["decision"]
    _require(isinstance(decision, dict) and set(decision) == {"schema_version", "action", "wait_sha256", "watch_path", "watch_sha256"}
             and isinstance(owner["watch_raw"], str)
             and hashlib.sha256(owner["watch_raw"].encode()).hexdigest() == decision["watch_sha256"]
             and _digest(decoded(owner["watch_raw"])) == _digest(owner["watch"]), "source original watch bytes differ")
    validate_context(wait["context"])
    _require(wait["schema_version"] == VERSION and wait["state"] == "WAIT_OWNER"
             and wait["run_id"] == source["run_id"] and wait["connection_id"] == source["connection_id"]
             and wait["root_identity"]["path"] == source["runtime_root"]
             and wait["context"]["policy_sha256"] == source["policy_sha256"]
             and wait["context"]["stage"] == receipt.get("step")
             and decision["schema_version"] == VERSION and decision["action"] == "RELEASE"
             and decision["wait_sha256"] == _digest(wait), "source owner/run/context differs")
    for key, name in (("plan", "plan_sha256"), ("parent_receipt", "parent_receipt_sha256"),
                      ("installation", "installation_sha256"), ("prompt", "prompt_sha256")):
        _require(wait["context"]["inputs_sha256"][key] == receipt[name], "source owner input differs")
    _require(type(owner["released_ns"]) is int and receipt["started_ns"] <= wait["created_ns"] <= owner["released_ns"]
             <= receipt["finished_ns"], "source owner release clock differs")
    validate_watch(wait, owner["watch"], now_ns=owner["released_ns"])
