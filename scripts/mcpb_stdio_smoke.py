# SPDX-License-Identifier: Apache-2.0
"""Launch the staged MCPB through its exact UV stdio command."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


EXPECTED_TOOLS = {
    "list_rounds",
    "extract_redlines",
    "inspect_document",
    "map_rounds",
    "verify_quote",
    "preflight_edits",
    "apply_edits",
    "export_decision_record",
}


def _payload(result) -> dict:
    if isinstance(result.structuredContent, dict):
        data = result.structuredContent
        return data.get("result", data)
    return json.loads(result.content[0].text)


async def smoke(stage_dir: Path) -> dict:
    stage_dir = stage_dir.resolve()
    if not (stage_dir / "manifest.json").is_file():
        raise ValueError("staged MCPB manifest is missing")
    parameters = StdioServerParameters(
        command="uv",
        args=[
            "run",
            "--frozen",
            "--no-dev",
            "--directory",
            str(stage_dir),
            "veqtor-mcp",
        ],
        env={
            **os.environ,
            "VEQTOR_TRACKED_CHANGE_AUTHOR": "Veqtor MCPB stdio CI",
            "UV_NO_PROGRESS": "1",
        },
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            if names != EXPECTED_TOOLS:
                raise ValueError("stdio tool inventory differs")
            listed = _payload(
                await session.call_tool("list_rounds", {"folder": "demo"})
            )
            filenames = [round_["filename"] for round_ in listed["rounds"]]
            if len(filenames) != 4 or listed["skipped"]:
                raise ValueError("bundled demo is not available from MCPB cwd")
            source = listed["rounds"][1]["path"]
            inspected = _payload(
                await session.call_tool(
                    "inspect_document",
                    {"path": source, "mode": "browse", "max_items": 1},
                )
            )
            if (
                inspected["mode"] != "browse"
                or inspected["coverage"]["returned_item_count"] != 1
            ):
                raise ValueError("bundled demo inspection differs")
            mapped = _payload(
                await session.call_tool(
                    "map_rounds",
                    {
                        "folder": "demo",
                        "seed": {
                            "schema_version": "round_map_seed.v1",
                            "path": source,
                            "paragraph_ref": inspected["paragraphs"][0][
                                "paragraph_ref"
                            ],
                        },
                        "max_items": 100,
                    },
                )
            )
            if (
                mapped["status"] != "ok"
                or mapped["coverage"]["scan_complete"] is not True
                or mapped["coverage"]["candidate_document_count"] != 4
            ):
                raise ValueError("bundled demo Round Map differs")
            return {
                "bundled_demo_filenames": filenames,
                "round_map_candidate_document_count": mapped["coverage"][
                    "candidate_document_count"
                ],
                "stdio_tool_count": len(names),
            }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage-dir", type=Path, required=True)
    options = parser.parse_args(argv)
    try:
        result = asyncio.run(smoke(options.stage_dir))
    except (OSError, ValueError, ExceptionGroup) as exc:
        print(f"MCPB stdio smoke failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
