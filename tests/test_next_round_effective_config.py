# SPDX-License-Identifier: Apache-2.0
"""Pinned effective defaults, strict input flags and shared offline validation."""
from copy import deepcopy
import json

import pytest

from capture_nr03_app_server import check_effective, launch_config
from check_codex_acceptance import EvidenceError
from nr03_app_server import lines
from nr03_source_fixtures import encode
import test_next_round_source_profile as source

files = source.files
source_case = source.source_case
DEFAULTS = dict(enabled=True, environment_id="local", tool_timeout_sec=None)


def effective(config):
    # Independent fixture representation observed from pinned installed config/read.
    value = deepcopy(config)
    value["mcp_servers"]["veqtor_nr03"].update(DEFAULTS)
    return value


def response(config):
    return dict(config=effective(config), origins={}, layers=[
        dict(name=dict(type="sessionFlags"), version="synthetic", config=deepcopy(config)),
        dict(name=dict(type="user", file="/synthetic/config.toml", profile=None), version="synthetic", config={}),
        dict(name=dict(type="system"), version="synthetic", config={})])


def mutations(original):
    result = []
    for key, values in {
        "enabled": (False, 1, "true", None),
        "environment_id": ("remote", None, False),
        "tool_timeout_sec": (0, 1, False, "null"),
        "command": ("/different/python",), "args": (["other-module"],), "env": ({"other": "value"},),
    }.items():
        for value in values:
            bad = deepcopy(original)
            bad["config"]["mcp_servers"]["veqtor_nr03"][key] = value
            result.append(("server_" + key + "_" + repr(value), bad))
    for key in (*DEFAULTS, "command", "args", "env"):
        bad = deepcopy(original)
        del bad["config"]["mcp_servers"]["veqtor_nr03"][key]
        result.append(("missing_" + key, bad))
    bad = deepcopy(original)
    bad["config"]["mcp_servers"]["veqtor_nr03"]["unknown_extension"] = None
    result.append(("unknown_server_field", bad))
    bad = deepcopy(original)
    bad["config"]["mcp_servers"]["another"] = dict(command="/other")
    result.append(("another_server", bad))
    for key, value in dict(model="other", model_reasoning_effort="low", project_doc_max_bytes=1,
        cli_auth_credentials_store="keyring", web_search="live", approval_policy="on-request",
        sandbox_mode="workspace-write").items():
        bad = deepcopy(original)
        bad["config"][key] = value
        result.append(("selected_" + key, bad))
    for key in ("plugins", "hooks", "instructions", "developer_instructions"):
        bad = deepcopy(original)
        bad["config"][key] = {"synthetic": True}
        result.append(("inherited_" + key, bad))
    for kind in ("project", "system", "packagedDefaults", "mdm", "enterpriseManaged", "user"):
        for disabled in (False, True):
            bad = deepcopy(original)
            name = dict(type=kind)
            if kind == "user":
                name["file"] = "/synthetic/config.toml"
            bad["layers"].append(dict(name=name, config=dict(model="unselected"),
                **(dict(disabledReason="synthetic disabled layer") if disabled else {})))
            result.append(("layer_" + kind + "_" + str(disabled), bad))
    for fault in ("changed_flags", "defaulted_flags", "disabled_flags", "missing_flags", "duplicate_flags", "wrong_user_path"):
        bad = deepcopy(original)
        flags = next(row for row in bad["layers"] if row["name"]["type"] == "sessionFlags")
        if fault == "changed_flags":
            flags["config"]["model_reasoning_effort"] = "low"
        elif fault == "defaulted_flags":
            flags["config"] = deepcopy(bad["config"])
        elif fault == "disabled_flags":
            flags["disabledReason"] = "synthetic disabled flags"
        elif fault == "missing_flags":
            bad["layers"].remove(flags)
        elif fault == "duplicate_flags":
            bad["layers"].append(deepcopy(flags))
        else:
            next(row for row in bad["layers"] if row["name"]["type"] == "user")["name"]["file"] = "/unselected/config.toml"
        result.append((fault, bad))
    return result


def test_effective_defaults_are_exact_and_originals_are_unchanged(record_property):
    config = launch_config("/selected/python", model="gpt-6-astra", reasoning_effort="high")
    original = response(config)
    before = deepcopy((original, config))
    check_effective(original, config, "/synthetic")
    refused = []
    for name, bad in mutations(original):
        with pytest.raises(EvidenceError) as caught:
            check_effective(bad, config, "/synthetic")
        refused.append(dict(name=name, cause=str(caught.value)))
        check_effective(original, config, "/synthetic")
    assert (original, config) == before
    record_property("F16_effective_causal", json.dumps(refused))


def test_selected_input_cannot_override_or_invent_effective_defaults():
    config = launch_config("/selected/python", model="gpt-6-astra", reasoning_effort="high")
    for field, value in dict(DEFAULTS, unknown_extension=None).items():
        bad = deepcopy(config)
        bad["mcp_servers"]["veqtor_nr03"][field] = value
        with pytest.raises(EvidenceError):
            check_effective(response(bad), bad, "/synthetic")


def test_original_effective_representation_reaches_shared_offline_consumer(source_case, record_property):
    f, producer = deepcopy(source_case)
    raw = lines(f["raw"].encode())
    returned = next(row for row in raw if row.get("id") == 2)
    # Explicitly install the pinned representation even on a pre-fix fixture.
    returned["result"]["config"] = effective(f["receipt"]["source"]["launch_config"])
    original = deepcopy(returned["result"])
    def assess(value):
        returned["result"] = value
        f["raw"] = encode(raw)
        source.rebind(f)
        return source.assess((f, producer))
    assert len(assess(original)) == 5
    refused = []
    # Use the same original response, not a reconstructed or filtered exchange.
    for name, bad in mutations(original):
        with pytest.raises(EvidenceError) as caught:
            assess(bad)
        refused.append(dict(name=name, cause=str(caught.value)))
        assert len(assess(original)) == 5
    record_property("F16_offline_exchange_causal", json.dumps(refused))
