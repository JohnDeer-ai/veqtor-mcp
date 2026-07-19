# SPDX-License-Identifier: Apache-2.0
"""Smoke the installed wheel through a real in-memory MCP client session."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from importlib.metadata import distribution
import json
import os
import tempfile
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session

from veqtor_docx import generate_demo_rounds
from veqtor_mcp import __version__
from veqtor_mcp.records import SOURCE_SNAPSHOT_IDENTITY
from veqtor_mcp.server import mcp


def _payload(result) -> dict:
    if isinstance(result.structuredContent, dict):
        data = result.structuredContent
        return data.get("result", data)
    return json.loads(result.content[0].text)


async def smoke() -> dict:
    installed = distribution("veqtor-mcp")
    assert installed.metadata["Name"] == "veqtor-mcp"
    assert installed.version == __version__
    configured_matter = os.environ.get("VEQTOR_SMOKE_MATTER")
    workspace = (
        nullcontext(configured_matter)
        if configured_matter is not None
        else tempfile.TemporaryDirectory(prefix="veqtor-wheel-smoke-")
    )
    with workspace as root:
        if configured_matter is None:
            matter = Path(root) / "matter"
            generate_demo_rounds(matter)
        else:
            matter = Path(root)
            assert matter.is_dir()
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as session:
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {
                "list_rounds",
                "extract_redlines",
                "inspect_document",
                "verify_quote",
                "preflight_edits",
                "apply_edits",
                "export_decision_record",
            }
            listed = _payload(
                await session.call_tool("list_rounds", {"folder": str(matter)})
            )
            source = listed["rounds"][1]["path"]
            inspected = _payload(
                await session.call_tool(
                    "inspect_document",
                    {"path": source, "mode": "outline", "max_items": 1},
                )
            )
            assert inspected["mode"] == "outline"
            assert inspected["file_sha256"] == listed["rounds"][1]["sha256"]
            assert inspected["search_scope"] == "word_document_xml_body_v1"
            assert inspected["revision_inventory"]["schema_version"] == (
                "revision_inventory.v2"
            )
            extracted = _payload(
                await session.call_tool("extract_redlines", {"path": source})
            )
            cap = next(
                unit
                for unit in extracted["change_units"]
                if (unit.get("clause_anchor") or {}).get("label") == "14.2"
            )
            anchor = {
                "change_unit_id": cap["change_unit_id"],
                "file_sha256": extracted["file_sha256"],
            }
            verified = _payload(
                await session.call_tool(
                    "verify_quote",
                    {
                        "path": source,
                        "anchor": anchor,
                        "quote": cap["new_text"],
                    },
                )
            )
            assert verified["verdict"] == "exact"
            edits = [
                {
                    "anchor": anchor,
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
                        "preflight_proof": preflight["preflight_proof"],
                    },
                )
            )
            assert output.is_file()
            assert applied["preflight_binding_status"] == "verified"
            assert (
                applied["preflight_candidate_sha256"] == preflight["candidate_sha256"]
            )
            assert applied["candidate_output_sha256_match"] is True
            assert applied["output_sha256"] == preflight["candidate_sha256"]
            exported = _payload(
                await session.call_tool(
                    "export_decision_record",
                    {"workspace": str(matter)},
                )
            )
            assert exported["returned_count"] == len(exported["records"])
            assert exported["assurance"]["tamper_evident"] is False
            assert exported["access_count"] == 0
            assert exported["access_events_recorded_locally"] is True
            assert exported["access_events_in_records"] is False
            first_access_id = exported["current_export_event"]["record_id"]
            assert first_access_id == exported["record_id"]
            assert all(
                record["record_id"] != first_access_id for record in exported["records"]
            )

            exported_again = _payload(
                await session.call_tool(
                    "export_decision_record",
                    {"workspace": str(matter), "max_records": 3},
                )
            )
            assert exported_again["total_count"] == exported["total_count"]
            assert exported_again["access_count"] == 1
            assert exported_again["access_count_includes_current_export"] is False
            assert exported_again["returned_count"] == 3
            assert all(
                record["record_type"] != "access_event.v1"
                and record["record_id"] != first_access_id
                for record in exported_again["records"]
            )
            assert exported_again["current_export_event"]["record_id"] == (
                f"dr_{int(first_access_id.removeprefix('dr_')) + 1:03d}"
            )
            return {
                "first_access_count": exported["access_count"],
                "second_access_count": exported_again["access_count"],
                "first_event_absent_from_windows": True,
                "current_event_outside_own_snapshot": True,
                "runtime_producer_build": SOURCE_SNAPSHOT_IDENTITY,
                "runtime_version": __version__,
                "installed_metadata_version": installed.version,
                "tool_count": len(names),
                "used_bundled_demo": configured_matter is not None,
            }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(smoke()), sort_keys=True))
