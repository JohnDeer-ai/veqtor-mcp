# SPDX-License-Identifier: Apache-2.0
"""Smoke the installed wheel through a real in-memory MCP client session."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session

from veqtor_docx import generate_demo_rounds
from veqtor_mcp import __version__
from veqtor_mcp.server import mcp


def _payload(result) -> dict:
    if isinstance(result.structuredContent, dict):
        data = result.structuredContent
        return data.get("result", data)
    return json.loads(result.content[0].text)


async def smoke() -> None:
    assert __version__ == "0.1.0"
    with tempfile.TemporaryDirectory(prefix="veqtor-wheel-smoke-") as tmp:
        matter = Path(tmp) / "matter"
        generate_demo_rounds(matter)
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as session:
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {
                "list_rounds",
                "extract_redlines",
                "verify_quote",
                "preflight_edits",
                "apply_edits",
                "export_decision_record",
            }
            listed = _payload(
                await session.call_tool("list_rounds", {"folder": str(matter)})
            )
            source = listed["rounds"][1]["path"]
            extracted = _payload(
                await session.call_tool("extract_redlines", {"path": source})
            )
            cap = next(
                unit
                for unit in extracted["change_units"]
                if (unit.get("clause_anchor") or {}).get("label") == "14.2"
            )
            edits = [
                {
                    "anchor": {
                        "change_unit_id": cap["change_unit_id"],
                        "file_sha256": extracted["file_sha256"],
                    },
                    "delete_text": "USD 50,000",
                    "insert_text": "USD 250,000",
                }
            ]
            preflight = _payload(
                await session.call_tool(
                    "preflight_edits",
                    {"source_path": source, "edits": edits},
                )
            )
            assert preflight["batch_applicable"] is True
            output = matter / "round-5-smoke.docx"
            applied = _payload(
                await session.call_tool(
                    "apply_edits",
                    {
                        "source_path": source,
                        "output_path": str(output),
                        "edits": edits,
                    },
                )
            )
            assert output.is_file()
            assert applied["output_sha256"] == preflight["candidate_sha256"]
            exported = _payload(
                await session.call_tool(
                    "export_decision_record",
                    {"workspace": str(matter)},
                )
            )
            assert exported["returned_count"] == len(exported["records"])
            assert exported["assurance"]["tamper_evident"] is False


if __name__ == "__main__":
    asyncio.run(smoke())
