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
import threading
import time
import uuid

from capture_position_session import private_write
from check_codex_acceptance import _digest, _file_sha256, _require
from nr03_app_server import BUILD, PROFILE, lines, policy_hashes
from nr03_scenario import AUTHOR, EFFORTS, MODEL, SERVER
from nr03_runtime_policy import RuntimeBoundary, inventory_params, validate_inventory
from nr03_capture_owner import private_runtime
from position_source_launch import SERVER_ARGS

ISOLATED_FEATURES = dict(apps=False, remote_control=False, remote_plugin=False)
EFFECTIVE_FEATURES = dict(network_proxy=None, **ISOLATED_FEATURES, auth_elicitation=True,
    background_paginated_rollout_migration=False, mcp_2026_07_28=False, memories=False,
    mentions_v2=True, tool_suggest=True, windows_sandbox_service=False)


def launch_config(python, *, model, reasoning_effort, journal_disabled=False, server_args=None):
    _require(model == MODEL and reasoning_effort in EFFORTS, "NR-03 model/effort outside authorized policy")
    return dict(model=model, model_reasoning_effort=reasoning_effort, project_doc_max_bytes=0,
        cli_auth_credentials_store="file", web_search="disabled", approval_policy="never",
        sandbox_mode="danger-full-access", features=dict(ISOLATED_FEATURES), mcp_servers={SERVER: dict(command=python,
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
    servers = config.get("mcp_servers")
    _require(isinstance(servers, dict) and set(servers) == {SERVER}
             and isinstance(servers[SERVER], dict) and set(servers[SERVER]) == {"command", "args", "env"},
             "source selected MCP launch configuration differs")
    # Pinned config/read expands this one selected stdio server. Compare the
    # complete effective map, while sessionFlags below retain the original input.
    # Never remove fields from the original response or accept arbitrary extras.
    _require(_digest(config.get("features")) == _digest(ISOLATED_FEATURES), "source runtime isolation flags differ")
    expected = dict(config, features=EFFECTIVE_FEATURES, mcp_servers={SERVER: dict(servers[SERVER],
                    enabled=True, environment_id="local", tool_timeout_sec=None)})
    actual = result.get("config")
    _require(isinstance(actual, dict) and all(_digest(actual.get(k)) == _digest(v) for k, v in expected.items()),
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
    _require(methods == ["initialize", "initialized", "config/read", thread_method, "mcpServerStatus/list", "turn/start"],
             "source original request lifecycle differs")
    _require(responses["initialize"][1]["params"] == initialize_request(), "source notifications suppressed or initialization differs")
    _require(responses["config/read"][1]["params"] == dict(includeLayers=True, cwd=receipt["cwd"]),
             "source effective configuration cwd differs")
    config, selection = source["launch_config"], source["selection"]
    _require(config["model"] == selection["model"] and config["model_reasoning_effort"] == selection["reasoning_effort"]
             and receipt["command"] == command_for(receipt["command"][0], config), "source launch selection differs")
    check_effective(responses["config/read"][2], config, source["runtime_root"])
    _require(responses["mcpServerStatus/list"][1]["params"] == inventory_params(source["thread_id"]),
             "source runtime inventory request differs")
    validate_inventory(responses["mcpServerStatus/list"][2])
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
    def __init__(self, process, folder, stage, run, connection, *, boundary=None, cancel_check=lambda: None):
        self.process, self.run, self.connection = process, run, connection
        self.boundary, self.failure = boundary, None
        self.cancel_check, self.closed, self.cleanup_proven = cancel_check, False, False
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.counts = dict(sent=0, received=0)
        self.notifications = []
        self.files = {}
        self.paths = {}
        try:
            for key, suffix in (("sent", "requests.jsonl"), ("received", "jsonl"), ("timeline", "transport.jsonl")):
                self.paths[key] = Path(folder) / f"{stage}.{suffix}"
                fd = os.open(self.paths[key], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                self.files[key] = os.fdopen(fd, "wb")
            self.reader = threading.Thread(target=self.read, daemon=True)
            self.reader.start()
        except BaseException:
            for output in self.files.values():
                output.close()
            raise

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
                    if self.boundary is not None:
                        try:
                            self.boundary.receive(lines(raw)[0])
                        except Exception as error:
                            self.failure = self.failure or error
                self.queue.put(raw)
        except Exception as error:
            self.failure = self.failure or error
        finally:
            self.queue.put(None)

    def send(self, method, params=None, ident=None):
        self.cancel_check()
        row = dict(method=method)
        if params is not None:
            row["params"] = params
        if ident is not None:
            row["id"] = ident
        raw = json.dumps(row, separators=(",", ":")).encode() + b"\n"
        with self.lock:
            if self.failure is not None:
                raise self.failure
            if self.boundary is not None:
                self.boundary.request(row)
            self.record("sent", raw)
            self.process.stdin.write(raw)
            self.process.stdin.flush()

    def receive(self):
        self.cancel_check()
        if self.failure is not None:
            raise self.failure
        deadline = time.monotonic() + 60
        while True:
            try:
                raw = self.queue.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                break
            except queue.Empty:
                self.cancel_check()
                _require(time.monotonic() < deadline, "source transport timed out")
        _require(raw is not None, "source transport ended before completion")
        if self.failure is not None:
            raise self.failure
        row = lines(raw)[0]
        _require("error" not in row and not ("id" in row and "method" in row), "source protocol error/server request")
        if "method" in row:
            self.notifications.append(row)
        return row

    def runtime_inventory(self):
        _require(self.boundary is not None, "source live runtime boundary missing")
        while self.boundary.startup != "ready" or self.boundary.remote is None:
            self.receive()
        self.send("mcpServerStatus/list", inventory_params(self.boundary.thread), 5)
        self.response(5)
        self.boundary.require_ready()

    def response(self, ident):
        while True:
            row = self.receive()
            if row.get("id") == ident:
                return row["result"]

    def close(self):
        if self.closed:
            _require(self.cleanup_proven, "source cleanup remained unproven")
            return self.process.returncode
        self.closed = True
        error = None
        try:
            code = close_direct(self.process)
        except BaseException as caught:
            error = caught
            code = -1
        finally:
            self.reader.join(timeout=10)
            self.cleanup_proven = self.process.poll() is not None and not self.reader.is_alive()
            # A live reader retains open private records for quarantine. Closing
            # under a blocked reader could destroy its last original bytes.
            if not self.reader.is_alive():
                for output in self.files.values():
                    output.close()
                self.process.stdout.close()
        _require(self.cleanup_proven, "source direct child/transport cleanup unproven")
        if error is not None:
            raise error
        if self.failure is not None:
            raise self.failure
        return code


def close_direct(process):
    """Reap exactly our Popen; terminate then kill only that owned direct child."""
    error = None
    try:
        if process.stdin is not None:
            process.stdin.close()
    except OSError as caught:
        error = caught
    try:
        code = process.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError) as caught:
        error = error or caught
        try:
            process.terminate()
            code = process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            process.kill()
            code = process.wait(timeout=5)
    if error is not None:
        raise error
    return code


def capture(folder, stage, command, config, prompt, cwd, selection, *, resumed=None, parent_prefix=None, owner=None):
    """Keep a complete prefix before deleting the private per-process runtime."""
    codex = Path(command[0])
    _require(_file_sha256(str(codex)) == BUILD["sha256"], "source executable hash differs from qualified build")
    _require(BUILD["commit"].encode() in codex.read_bytes(), "source embedded commit absent")
    folder = Path(folder)
    run, connection = str(uuid.uuid4()), str(uuid.uuid4())
    cancel = owner.check_cancelled if owner is not None else lambda: None
    with private_runtime(folder, Path(__file__).resolve().parents[1]) as (runtime, runtime_status):
        owner_metadata = owner.wait(runtime, stage, run, connection, policy_hashes()) if owner is not None else None
        cancel()
        runtime_status["native_created"] = True
        version = subprocess.run([str(codex), "--version"], capture_output=True, check=False, timeout=15)
        cancel()
        _require(version.returncode == 0 and version.stdout.decode().strip() == "codex-cli " + BUILD["version"],
                 "source executable version differs")
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
            process = stream = None
            configuration_safe = False
            try:
                cancel()
                process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
                runtime_status["cleanup_proven"] = False
                stream = StdioCapture(process, startup, stage, run, connection, boundary=RuntimeBoundary(config, runtime), cancel_check=cancel)
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
                _require(prefix_path.is_absolute() and ".." not in prefix_path.parts and prefix_path != runtime
                         and prefix_path.is_relative_to(runtime) and prefix_path.resolve().is_relative_to(runtime)
                         and not prefix_path.is_symlink(), "source session escaped private runtime")
                stream.runtime_inventory()
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
                try:
                    if stream is not None:
                        code = stream.close()
                    elif process is not None:
                        code = close_direct(process)
                finally:
                    runtime_status["cleanup_proven"] = ((process is None or process.poll() is not None)
                        and (stream is None or stream.cleanup_proven))
                    if stream is None and process is not None and process.poll() is not None:
                        process.stdout.close()
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
            if owner_metadata is not None:
                source["owner"] = owner_metadata
            for name, suffix in (("events", "jsonl"), ("requests", "requests.jsonl"), ("transport", "transport.jsonl"),
                                 ("session", "session.jsonl"), ("stderr", "stderr.txt")):
                source[name + "_sha256"] = _file_sha256(str(folder / f"{stage}.{suffix}"))
            cancel()
            runtime_status["success"] = True
    return code, source


def bind_delivery(folder, stage, receipt):
    from prepare_next_round_acceptance import write_json
    receipt["source"]["context_sha256"] = _digest({k: v for k, v in receipt.items() if k != "source"})
    write_json(Path(folder) / f"{stage}.receipt.json", receipt)
    write_json(Path(folder) / f"{stage}.delivery.json", dict(schema_version="nr03-model-delivery.v3",
        session_path=str(Path(folder) / f"{stage}.session.jsonl"), session_sha256=receipt["source"]["session_sha256"],
        receipt_sha256=_file_sha256(str(Path(folder) / f"{stage}.receipt.json"))))
