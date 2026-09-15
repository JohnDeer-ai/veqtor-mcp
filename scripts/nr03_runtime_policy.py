# SPDX-License-Identifier: Apache-2.0
"""Pinned notification and selected MCP runtime boundary, shared live/offline."""
from check_codex_acceptance import _digest, _require
from nr03_scenario import SERVER

NOTIFICATIONS = {"remoteControl/status/changed", "mcpServer/startupStatus/updated"}
TOOLS = {"list_rounds", "extract_redlines", "inspect_document", "map_rounds",
         "trace_paragraph_history", "preflight_edits", "apply_edits", "verify_quote",
         "export_decision_record", "read_deal_positions", "mutate_deal_positions"}


def inventory_params(thread):
    return dict(threadId=thread, cursor=None, limit=100, detail="full")


def validate_inventory(result):
    _require(isinstance(result, dict) and set(result) == {"data", "nextCursor"}
             and result["nextCursor"] is None and isinstance(result["data"], list)
             and len(result["data"]) == 1, "source runtime inventory incomplete or extra server")
    row = result["data"][0]
    _require(isinstance(row, dict) and set(row) == {"name", "runtimeStatus", "pluginId", "serverInfo",
             "tools", "toolsError", "resources", "resourceTemplates", "authStatus"}
             and row["name"] == SERVER and row["runtimeStatus"] == "connected"
             and row["authStatus"] == "unsupported" and row["pluginId"] is None
             and row["toolsError"] is None and row["resources"] == [] and row["resourceTemplates"] == [],
             "source runtime server/capability boundary differs")
    info = row["serverInfo"]
    _require(isinstance(info, dict) and set(info) == {"name", "title", "version", "description", "icons", "websiteUrl"}
             and info["name"] == "veqtor" and info["version"] == "0.4.2.dev0"
             and all(info[k] is None for k in ("title", "description", "icons", "websiteUrl")),
             "source runtime producer metadata differs")
    tools = row["tools"]
    _require(isinstance(tools, dict) and set(tools) == TOOLS, "source runtime tool set differs")
    for name, tool in tools.items():
        _require(isinstance(tool, dict) and {"name", "inputSchema"} <= set(tool)
                 <= {"name", "inputSchema", "outputSchema", "description", "title", "icons", "annotations", "_meta"}
                 and tool["name"] == name and isinstance(tool["inputSchema"], dict),
                 "source runtime tool declaration differs")


class RuntimeBoundary:
    """Observe every original request/response; snapshots never replace startup."""

    def __init__(self, config, runtime, *, thread=None):
        self.config, self.runtime, self.thread = config, runtime, thread
        self.requests, self.methods, self.replies = {}, [], set()
        self.remote = None
        self.startup = None
        self.thread_notified = False
        self.inventory = False
        self.last_emitted = 0
        self.turn = None

    def request(self, row):
        _require(isinstance(row, dict), "source runtime malformed request")
        method = row.get("method")
        expected = ["initialize", "initialized", "config/read", "thread", "mcpServerStatus/list", "turn/start"]
        index = len(self.methods)
        _require(index < len(expected) and (method in {"thread/start", "thread/resume"}
                 if expected[index] == "thread" else method == expected[index]), "source runtime request lifecycle differs")
        if method == "mcpServerStatus/list":
            _require(self.thread_reply and self.startup == "ready" and self.remote is not None
                     and _digest(row.get("params")) == _digest(inventory_params(self.thread)),
                     "source runtime inventory requested before qualified readiness")
        if method == "turn/start":
            self.require_ready()
        if method in {"thread/start", "thread/resume"}:
            _require("config/read" in self.replies, "source thread before qualified config")
        if method != "initialized":
            ident = row.get("id")
            _require(type(ident) is int and ident not in self.requests, "source runtime request identity differs")
            self.requests[ident] = method
        else:
            _require("initialize" in self.replies and "id" not in row, "source runtime initialization differs")
        self.methods.append(method)

    @property
    def thread_reply(self):
        return bool(self.replies & {"thread/start", "thread/resume"})

    def bind_thread(self, value):
        _require(isinstance(value, str) and value and (self.thread is None or self.thread == value),
                 "source runtime thread differs")
        self.thread = value

    def receive(self, row):
        _require(isinstance(row, dict) and "error" not in row and not ("method" in row and "id" in row),
                 "source protocol error or server request")
        if "id" in row:
            _require(type(row["id"]) is int, "source runtime response identity differs")
            method = self.requests.get(row["id"])
            _require(method is not None and method not in self.replies, "source runtime unexpected/duplicate response")
            self.replies.add(method)
            result = row.get("result")
            _require(isinstance(result, dict), "source runtime malformed response")
            if method == "config/read":
                from capture_nr03_app_server import check_effective
                check_effective(result, self.config, self.runtime)
            elif method in {"thread/start", "thread/resume"}:
                self.bind_thread(result.get("thread", {}).get("id"))
            elif method == "mcpServerStatus/list":
                validate_inventory(result)
                self.inventory = True
            elif method == "turn/start":
                value = result.get("turn", {}).get("id")
                _require(isinstance(value, str) and value and (self.turn is None or value == self.turn),
                         "source runtime turn differs")
                self.turn = value
            return
        method, params = row.get("method"), row.get("params")
        if method == "thread/started":
            _require(any(m in self.methods for m in ("thread/start", "thread/resume"))
                     and not self.thread_notified and isinstance(params, dict), "source runtime thread notification differs")
            self.bind_thread(params.get("thread", {}).get("id"))
            self.thread_notified = True
        if method not in NOTIFICATIONS:
            from nr03_app_server import PASSIVE
            _require(isinstance(method, str) and method in PASSIVE | {"thread/started", "turn/started", "turn/completed",
                     "item/started", "item/completed"} and method != "model/rerouted"
                     and isinstance(params, dict), "source unsupported notification: " + str(method))
            if "threadId" in params:
                _require(self.thread is not None and params["threadId"] == self.thread,
                         "source notification thread differs")
            if method.startswith(("turn/", "item/")) or "turnId" in params:
                _require("turn/start" in self.methods, "source notification before turn request")
                value = params.get("turn", {}).get("id") if method.startswith("turn/") else params.get("turnId")
                _require(isinstance(value, str) and value and (self.turn is None or value == self.turn),
                         "source notification turn differs")
                self.turn = value
            return
        _require(set(row) == {"method", "params", "emittedAtMs"} and isinstance(params, dict)
                 and type(row["emittedAtMs"]) is int and 0 < row["emittedAtMs"] < 2**63
                 and row["emittedAtMs"] >= self.last_emitted, "source runtime notification envelope differs")
        self.last_emitted = row["emittedAtMs"]
        if method == "remoteControl/status/changed":
            _require("initialize" in self.replies and set(params) == {"status", "serverName", "installationId", "environmentId"}
                     and params["status"] == "disabled" and params["environmentId"] is None
                     and all(isinstance(params[k], str) and 0 < len(params[k]) <= 1024 for k in ("serverName", "installationId")),
                     "source remote state is not qualified disabled")
            _require(self.remote is None or _digest(self.remote) == _digest(params), "source remote identity changed")
            self.remote = dict(params)
        else:
            _require(set(params) == {"threadId", "name", "status", "error", "failureReason"}
                     and self.thread is not None and params["threadId"] == self.thread
                     and any(m in self.methods for m in ("thread/start", "thread/resume"))
                     and params["name"] == SERVER and params["error"] is None and params["failureReason"] is None,
                     "source unexpected runtime server or startup failure")
            state = params["status"]
            _require((state == "starting" and self.startup is None)
                     or (state == "ready" and self.startup in {"starting", "ready"}),
                     "source runtime startup lifecycle differs")
            self.startup = state

    def require_ready(self):
        _require(self.thread_reply and self.remote is not None and self.startup == "ready" and self.inventory,
                 "source runtime boundary not ready before turn")
