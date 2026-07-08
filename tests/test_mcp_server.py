# SPDX-License-Identifier: Apache-2.0
"""End-to-end smoke test of the MCP tool surface over an in-memory session."""

import json
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from veqtor_mcp.server import mcp


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _payload(result) -> dict:
    if isinstance(result.structuredContent, dict):
        data = result.structuredContent
        return data.get("result", data)
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_tools_are_exposed_and_callable(demo_dir: Path) -> None:
    async with create_connected_server_and_client_session(
        mcp._mcp_server
    ) as session:
        tools = await session.list_tools()
        assert {t.name for t in tools.tools} == {"list_rounds", "extract_redlines"}

        listed = await session.call_tool("list_rounds", {"folder": str(demo_dir)})
        assert not listed.isError
        rounds = _payload(listed)["rounds"]
        assert len(rounds) == 4

        extracted = await session.call_tool(
            "extract_redlines", {"path": rounds[1]["path"]}
        )
        assert not extracted.isError
        payload = _payload(extracted)
        assert payload["file_sha256"] == rounds[1]["sha256"]
        anchors = {
            u["clause_anchor"]["label"]
            for u in payload["change_units"]
            if u["clause_anchor"]
        }
        assert "14.2" in anchors


@pytest.mark.anyio
async def test_tool_errors_are_reported_not_raised(demo_dir: Path) -> None:
    async with create_connected_server_and_client_session(
        mcp._mcp_server
    ) as session:
        broken = await session.call_tool("list_rounds", {"folder": str(demo_dir / "nope")})
        assert broken.isError
