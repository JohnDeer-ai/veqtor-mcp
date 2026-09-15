# SPDX-License-Identifier: Apache-2.0
"""Original stdio capture. Invoked only by an explicitly authorized native run.

Child-only CODEX_HOME uses its documented configuration/auth/session semantics.
Private authentication never enters evidence. No daemon, permanent config, model
tool script, synthesized session prefix or backfill is used.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import uuid

from capture_position_session import private_write
from check_codex_acceptance import _digest, _file_sha256, _require
from nr03_app_server import BUILD, PROFILE, lines, policy_hashes
from nr03_scenario import AUTHOR, EFFORTS, MODEL, SERVER
from position_source_launch import SERVER_ARGS


def launch_config(python, *, model, reasoning_effort, journal_disabled=False, server_args=None):
    _require(model == MODEL and reasoning_effort in EFFORTS, "NR-03 model/effort outside authorized policy")
    return dict(model=model, model_reasoning_effort=reasoning_effort, project_doc_max_bytes=0,
        cli_auth_credentials_store="file", web_search="disabled", approval_policy="never",
        sandbox_mode="danger-full-access", mcp_servers={SERVER: dict(command=python,
        args=SERVER_ARGS if server_args is None else server_args,
        env=dict(VEQTOR_TRACKED_CHANGE_AUTHOR=AUTHOR, VEQTOR_DISABLE_DECISION_RECORD="1" if journal_disabled else "0"))})


def toml_value(value):
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + "=" + toml_value(v) for k, v in sorted(value.items())) + "}"
    return json.dumps(value, separators=(",", ":"))


def command_for(codex, config):
    return [str(Path(codex).absolute()), "app-server", "--stdio", "--strict-config"] + [
        part for key, value in sorted(config.items()) for part in ("-c", key + "=" + toml_value(value))]


def initialize_request():
    return dict(clientInfo=dict(name="veqtor_nr03_capture", version="1"),
                capabilities=dict(experimentalApi=True, optOutNotificationMethods=[]))


def check_effective(result, config, runtime):
    actual = result.get("config")
    _require(isinstance(actual, dict) and all(_digest(actual.get(k)) == _digest(v) for k, v in config.items()),
             "source effective launch configuration differs")
    _require(not actual.get("plugins") and not actual.get("hooks") and not actual.get("instructions")
             and not actual.get("developer_instructions"), "source inherited plugins/hooks/instructions")
    layers = result.get("layers")
    _require(isinstance(layers, list) and layers, "source effective configuration layers missing")
    flags = []
    for layer in layers:
        name = layer.get("name", {})
        kind = name.get("type")
        _require(kind in {"sessionFlags", "user", "project", "system", "packagedDefaults", "mdm", "enterpriseManaged"},
                 "source unsupported configuration layer")
        if kind == "sessionFlags":
            flags.append(layer)
            _require(not layer.get("disabledReason") and _digest(layer.get("config")) == _digest(config),
                     "source session flags differ")
        elif kind == "user":
            _require(name.get("file") == str(Path(runtime) / "config.toml") and not layer.get("config"),
                     "source inherited user configuration")
        else:
            # Disabled inherited values can also contain credentials.
            _require(not layer.get("config"), "source inherited configuration layer")
    _require(len(flags) == 1, "source session config layer missing/duplicate")


def validate_exchange(methods, responses, source, receipt):
    resumed = receipt.get("resumed_thread_id")
    thread_method = "thread/resume" if resumed else "thread/start"
    _require(methods == ["initialize", "initialized", "config/read", thread_method, "turn/start"],
             "source original request lifecycle differs")
    _require(responses["initialize"][1]["params"] == initialize_request(), "source notifications suppressed or initialization differs")
    _require(responses["config/read"][1]["params"] == dict(includeLayers=True, cwd=receipt["cwd"]),
             "source effective configuration cwd differs")
    config, selection = source["launch_config"], source["selection"]
    _require(config["model"] == selection["model"] and config["model_reasoning_effort"] == selection["reasoning_effort"]
             and receipt["command"] == command_for(receipt["command"][0], config), "source launch selection differs")
    check_effective(responses["config/read"][2], config, source["runtime_root"])
    request, result = responses[thread_method][1]["params"], responses[thread_method][2]
    expected = dict(model=selection["model"], cwd=receipt["cwd"], approvalPolicy="never", sandbox="danger-full-access")
    if resumed:
        expected.update(threadId=resumed, path=str(Path(source["runtime_root"]) / "resume.jsonl"), excludeTurns=True)
    else:
        expected.update(ephemeral=False)
    _require(request == expected and result.get("thread", {}).get("id") == source["thread_id"]
             and (not resumed or resumed == source["thread_id"])
             and result.get("model") == selection["model"] and result.get("reasoningEffort") == selection["reasoning_effort"]
             and result.get("cwd") == receipt["cwd"] and result.get("approvalPolicy") == "never"
             and result.get("instructionSources") == [], "source thread launch/actual configuration differs")
    turn_request, turn_response = responses["turn/start"][1]["params"], responses["turn/start"][2]
    _require(set(turn_request) == {"threadId", "input", "model", "effort"}
             and turn_request["threadId"] == source["thread_id"] and turn_request["model"] == selection["model"]
             and turn_request["effort"] == selection["reasoning_effort"]
             and turn_response.get("turn", {}).get("id") == source["turn_id"], "source turn request/identity differs")
    content = turn_request["input"]
    _require(isinstance(content, list) and len(content) == 1 and set(content[0]) == {"type", "text", "text_elements"}
             and content[0]["type"] == "text" and content[0]["text_elements"] == []
             and hashlib.sha256(content[0]["text"].encode()).hexdigest() == receipt["prompt_sha256"],
             "source business stimulus differs")


class StdioCapture:
    """Unfiltered original lines plus a separately bound ordering journal."""
    def __init__(self, process, folder, stage, run, connection):
        self.process, self.run, self.connection = process, run, connection
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.counts = dict(sent=0, received=0)
        self.notifications = []
        self.files = {}
        self.paths = {}
        for key, suffix in (("sent", "requests.jsonl"), ("received", "jsonl"), ("timeline", "transport.jsonl")):
            self.paths[key] = Path(folder) / f"{stage}.{suffix}"
            fd = os.open(self.paths[key], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.files[key] = os.fdopen(fd, "wb")
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def publish(self, folder):
        """Publish every startup byte only after the effective config is safe.

        Inherited layers may contain credentials. Holding the recorder lock
        preserves the complete original prefix and prevents a transport gap.
        """
        with self.lock:
            for key, path in self.paths.items():
                self.files[key].flush()
                destination = Path(folder) / path.name
                private_write(destination, path.read_bytes())
                self.files[key].close()
                self.files[key] = destination.open("ab")
            self.paths = {key: Path(folder) / path.name for key, path in self.paths.items()}

    def record(self, direction, raw):
        self.files[direction].write(raw)
        self.files[direction].flush()
        self.counts[direction] += 1
        entry = dict(direction=direction, line=self.counts[direction], sha256=hashlib.sha256(raw).hexdigest(),
                     monotonic_ns=time.monotonic_ns(), run_id=self.run, connection_id=self.connection)
        self.files["timeline"].write(json.dumps(entry).encode() + b"\n")
        self.files["timeline"].flush()

    def read(self):
        try:
            while raw := self.process.stdout.readline():
                with self.lock:
                    self.record("received", raw)
                self.queue.put(raw)
        finally:
            self.queue.put(None)

    def send(self, method, params=None, ident=None):
        row = dict(method=method)
        if params is not None:
            row["params"] = params
        if ident is not None:
            row["id"] = ident
        raw = json.dumps(row, separators=(",", ":")).encode() + b"\n"
        with self.lock:
            self.record("sent", raw)
            self.process.stdin.write(raw)
            self.process.stdin.flush()

    def receive(self):
        try:
            raw = self.queue.get(timeout=60)
        except queue.Empty:
            _require(False, "source transport timed out")
        _require(raw is not None, "source transport ended before completion")
        row = lines(raw)[0]
        _require("error" not in row and not ("id" in row and "method" in row), "source protocol error/server request")
        if "method" in row:
            self.notifications.append(row)
        return row

    def response(self, ident):
        while True:
            row = self.receive()
            if row.get("id") == ident:
                return row["result"]

    def close(self):
        self.process.stdin.close()
        try:
            code = self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)
            code = -1
        self.reader.join(timeout=10)
        _require(not self.reader.is_alive(), "source transport reader incomplete")
        for output in self.files.values():
            output.close()
        return code


def capture(folder, stage, command, config, prompt, cwd, selection, *, resumed=None, parent_prefix=None):
    """Keep a complete prefix before deleting the private per-process runtime."""
    codex = Path(command[0])
    _require(_file_sha256(str(codex)) == BUILD["sha256"], "source executable hash differs from qualified build")
    version = subprocess.run([str(codex), "--version"], capture_output=True, check=False)
    _require(version.returncode == 0 and version.stdout.decode().strip() == "codex-cli " + BUILD["version"],
             "source executable version differs")
    _require(BUILD["commit"].encode() in codex.read_bytes(), "source embedded commit absent")
    folder = Path(folder)
    run, connection = str(uuid.uuid4()), str(uuid.uuid4())
    with tempfile.TemporaryDirectory(prefix="veqtor-nr03-runtime-") as temporary:
        runtime = Path(temporary).resolve()
        os.chmod(runtime, 0o700)
        _require(not runtime.is_relative_to(folder) and not runtime.is_relative_to(Path(__file__).resolve().parents[1]),
                 "source private runtime must be outside evidence and checkout")
        # File backend is selected explicitly. No keyring writes, symlinks or
        # authentication values in commands/receipts/evidence; discard refreshes.
        auth_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        auth = auth_root / "auth.json"
        _require(auth.is_file(), "source isolated file authentication unavailable")
        private_write(runtime / "auth.json", auth.read_bytes())
        if resumed:
            _require(parent_prefix is not None, "source resume prefix missing")
            private_write(runtime / "resume.jsonl", Path(parent_prefix).read_bytes())
        env = {k: os.environ[k] for k in ("PATH", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR") if k in os.environ}
        # This is the supported child configuration-root parameter, not a shell
        # variable reassignment or permanent setting. No parent CODEX_* inherited.
        env["CODEX_HOME"] = str(runtime)
        startup = runtime / "capture"
        startup.mkdir(mode=0o700)
        private_errors = startup / f"{stage}.stderr.txt"
        err_fd = os.open(private_errors, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(err_fd, "wb") as errors:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
            stream = StdioCapture(process, startup, stage, run, connection)
            configuration_safe = False
            try:
                stream.send("initialize", initialize_request(), 1)
                stream.response(1)
                stream.send("initialized")
                stream.send("config/read", dict(includeLayers=True, cwd=str(cwd)), 2)
                check_effective(stream.response(2), config, runtime)
                configuration_safe = True
                stream.publish(folder)
                params = dict(model=selection["model"], cwd=str(cwd), approvalPolicy="never", sandbox="danger-full-access")
                if resumed:
                    params.update(threadId=resumed, path=str(runtime / "resume.jsonl"), excludeTurns=True)
                else:
                    params.update(ephemeral=False)
                stream.send("thread/resume" if resumed else "thread/start", params, 3)
                reply = stream.response(3)
                thread = reply["thread"]["id"]
                _require((not resumed or thread == resumed) and reply.get("instructionSources") == []
                         and reply.get("model") == selection["model"] and reply.get("reasoningEffort") == selection["reasoning_effort"],
                         "source thread isolation/model differs before model turn")
                prefix_path = Path(reply["thread"]["path"])
                _require(prefix_path.is_relative_to(runtime) and not prefix_path.is_symlink(), "source session escaped private runtime")
                stream.send("turn/start", dict(threadId=thread, input=[dict(type="text", text=prompt, text_elements=[])],
                    model=selection["model"], effort=selection["reasoning_effort"]), 4)
                turn_reply = stream.response(4)
                turn = turn_reply["turn"]["id"]
                while not any(row.get("method") == "turn/completed" for row in stream.notifications):
                    stream.receive()
                terminal = [row for row in stream.notifications if row.get("method") == "turn/completed"]
                _require(len(terminal) == 1 and terminal[0]["params"]["threadId"] == thread
                         and terminal[0]["params"]["turn"]["id"] == turn, "source captured turn completion differs")
            finally:
                code = stream.close()
                errors.flush()
                if configuration_safe:
                    private_write(folder / f"{stage}.stderr.txt", private_errors.read_bytes())
            # Persisted task_complete is required. Never synthesize it or use
            # thread/read to replace a missing prefix or original start.
            session = prefix_path.read_bytes()
            entries = session.splitlines(keepends=True)
            end = [i for i, raw in enumerate(entries) if (r := lines(raw)[0]).get("type") == "event_msg"
                   and r.get("payload", {}).get("type") == "task_complete" and r["payload"].get("turn_id") == turn]
            _require(end, "source original complete model prefix unavailable")
            private_write(folder / f"{stage}.session.jsonl", b"".join(entries[:end[-1] + 1]))
            source = dict(profile=PROFILE, build=deepcopy(BUILD), evidence_kind="native", run_id=run, connection_id=connection,
                executable_path=str(codex), runtime_root=str(runtime), launch_config=config, selection=selection,
                thread_id=thread, turn_id=turn, policy_sha256=policy_hashes())
            for name, suffix in (("events", "jsonl"), ("requests", "requests.jsonl"), ("transport", "transport.jsonl"),
                                 ("session", "session.jsonl"), ("stderr", "stderr.txt")):
                source[name + "_sha256"] = _file_sha256(str(folder / f"{stage}.{suffix}"))
    return code, source


def bind_delivery(folder, stage, receipt):
    from prepare_next_round_acceptance import write_json
    receipt["source"]["context_sha256"] = _digest({k: v for k, v in receipt.items() if k != "source"})
    write_json(Path(folder) / f"{stage}.receipt.json", receipt)
    write_json(Path(folder) / f"{stage}.delivery.json", dict(schema_version="nr03-model-delivery.v3",
        session_path=str(Path(folder) / f"{stage}.session.jsonl"), session_sha256=receipt["source"]["session_sha256"],
        receipt_sha256=_file_sha256(str(Path(folder) / f"{stage}.receipt.json"))))
