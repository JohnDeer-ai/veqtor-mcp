# SPDX-License-Identifier: Apache-2.0
"""Executable NR02-R4 inventory controls for the shared installed-wheel smoke."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]
# Independent specification oracle, not copied from the observed server result
# or obtained from the smoke's constants.
DEVELOPMENT_TOOLS = (
    "list_rounds", "extract_redlines", "inspect_document", "map_rounds",
    "trace_paragraph_history", "preflight_edits", "apply_edits", "verify_quote",
    "export_decision_record", "read_deal_positions", "mutate_deal_positions",
)
FROZEN_TOOLS = (
    "list_rounds", "extract_redlines", "inspect_document", "map_rounds",
    "trace_paragraph_history", "preflight_edits", "apply_edits", "verify_quote",
    "export_decision_record",
)


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location(
        "installed_wheel_smoke_under_test", ROOT / "scripts" / "installed_wheel_smoke.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inventory(names):
    return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in names])


def test_explicit_eleven_tool_development_inventory_passes(smoke):
    assert smoke.__version__ == "0.4.2.dev0"
    assert smoke._assert_tool_inventory(inventory(DEVELOPMENT_TOOLS)) == DEVELOPMENT_TOOLS


@pytest.mark.parametrize("names", [
    pytest.param(FROZEN_TOOLS, id="old-nine-only"),
    pytest.param(tuple(n for n in DEVELOPMENT_TOOLS if n != "read_deal_positions"), id="missing-read"),
    pytest.param(tuple(n for n in DEVELOPMENT_TOOLS if n != "mutate_deal_positions"), id="missing-mutate"),
    pytest.param((*DEVELOPMENT_TOOLS, "unexpected_tool"), id="unexpected"),
    pytest.param((*DEVELOPMENT_TOOLS, "read_deal_positions"), id="duplicate"),
    pytest.param((*FROZEN_TOOLS, "mutate_deal_positions", "read_deal_positions"), id="reordered"),
    pytest.param((*FROZEN_TOOLS, "read_deal_positions", "read_deal_positions"), id="duplicate-replaces-mutate"),
])
def test_development_inventory_rejects_weaker_or_different_surface(smoke, names):
    assert smoke.__version__ == "0.4.2.dev0"
    with pytest.raises(AssertionError, match="tool inventory differs"):
        smoke._assert_tool_inventory(inventory(names))
    assert smoke._assert_tool_inventory(inventory(DEVELOPMENT_TOOLS)) == DEVELOPMENT_TOOLS


def test_frozen_v04_contract_remains_exactly_nine(smoke, monkeypatch):
    monkeypatch.setattr(smoke, "__version__", "0.4.0")
    assert smoke._assert_tool_inventory(inventory(FROZEN_TOOLS)) == FROZEN_TOOLS
    for names in (DEVELOPMENT_TOOLS, FROZEN_TOOLS[:-1],
                  (*FROZEN_TOOLS, "export_decision_record"), tuple(reversed(FROZEN_TOOLS))):
        with pytest.raises(AssertionError, match="tool inventory differs"):
            smoke._assert_tool_inventory(inventory(names))
    assert smoke._assert_tool_inventory(inventory(FROZEN_TOOLS)) == FROZEN_TOOLS


def test_unknown_version_cannot_infer_a_contract_from_observed_tools(smoke, monkeypatch):
    monkeypatch.setattr(smoke, "__version__", "0.4.3.dev0")
    with pytest.raises(AssertionError, match="No explicit installed-wheel tool contract"):
        smoke._assert_tool_inventory(inventory(DEVELOPMENT_TOOLS))


def test_complete_smoke_validates_inventory_in_both_stdio_eras_and_inmemory(smoke, monkeypatch):
    # Real synthetic MCP transports and all remaining behavior/producer/export
    # assertions. The observer delegates to the actual inventory validator.
    observed = []
    validate = smoke._assert_tool_inventory

    def observe(result):
        names = validate(result)
        observed.append(names)
        return names

    monkeypatch.delenv("VEQTOR_SMOKE_MATTER", raising=False)
    monkeypatch.setattr(smoke, "_assert_tool_inventory", observe)
    result = asyncio.run(smoke.smoke())
    assert observed == [DEVELOPMENT_TOOLS, DEVELOPMENT_TOOLS, DEVELOPMENT_TOOLS]
    assert result["tool_count"] == 11
    assert result["stdio_protocol_versions"] == {"auto": "2026-07-28", "legacy": "2025-11-25"}
