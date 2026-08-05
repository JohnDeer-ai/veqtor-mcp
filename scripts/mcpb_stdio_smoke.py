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
import mcp.client.stdio as stdio_transport
from mcp.client.stdio import stdio_client
from veqtor_docx.synthetic import CAP_R3, CAP_R4


EXPECTED_TOOLS = {
    "list_rounds",
    "extract_redlines",
    "inspect_document",
    "map_rounds",
    "trace_paragraph_history",
    "preflight_edits",
    "apply_edits",
    "verify_quote",
    "export_decision_record",
}


def _payload(result) -> dict:
    if isinstance(result.structured_content, dict):
        data = result.structured_content
        return data.get("result", data)
    return json.loads(result.content[0].text)


async def _prove_forced_transport_teardown(
    parameters: StdioServerParameters,
) -> None:
    """Cancel the transport-owning task and prove its subprocess is reaped."""
    process = None
    initialized = asyncio.Event()
    original_spawn = stdio_transport._create_platform_compatible_process

    async def capture_spawn(*args, **kwargs):
        nonlocal process
        process = await original_spawn(*args, **kwargs)
        return process

    async def own_transport() -> None:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                initialized.set()
                await asyncio.Future()

    stdio_transport._create_platform_compatible_process = capture_spawn
    owner = asyncio.create_task(own_transport())
    try:
        await asyncio.wait_for(initialized.wait(), timeout=30)
        owner.cancel()
        try:
            await asyncio.wait_for(owner, timeout=10)
        except asyncio.CancelledError:
            pass
        else:
            raise ValueError("transport owner cancellation returned normally")
        if process is None:
            raise ValueError("forced-teardown stdio server process was not created")
        await asyncio.wait_for(process.wait(), timeout=10)
        if process.returncode is None:
            raise ValueError("stdio server process survived forced transport teardown")
    finally:
        stdio_transport._create_platform_compatible_process = original_spawn
        if not owner.done():
            owner.cancel()
            try:
                await owner
            except asyncio.CancelledError:
                pass


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
    process = None
    # Capture the exact process created by the SDK transport so this smoke can
    # prove context teardown reaped it. The factory is restored on every path.
    original_spawn = stdio_transport._create_platform_compatible_process

    async def capture_spawn(*args, **kwargs):
        nonlocal process
        process = await original_spawn(*args, **kwargs)
        return process

    stdio_transport._create_platform_compatible_process = capture_spawn
    try:
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
                source = listed["rounds"][-1]["path"]
                inspected = _payload(
                    await session.call_tool(
                        "inspect_document",
                        {
                            "path": source,
                            "mode": "literal_search",
                            "phrases": [CAP_R4],
                            "match_basis": "exact_literal",
                            "max_items": 1,
                        },
                    )
                )
                if (
                    inspected["mode"] != "literal_search"
                    or len(inspected["matches"]) != 1
                ):
                    raise ValueError("bundled demo inspection differs")
                paragraph_ref = inspected["matches"][0]["paragraph_ref"]
                mapped = _payload(
                    await session.call_tool(
                        "map_rounds",
                        {
                            "folder": "demo",
                            "seed": {
                                "schema_version": "round_map_seed.v1",
                                "path": source,
                                "paragraph_ref": paragraph_ref,
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
                history_arguments = {
                    "folder": "demo",
                    "seed": {
                        "schema_version": "paragraph_history_seed.v1",
                        "path": source,
                        "paragraph_ref": paragraph_ref,
                    },
                    "order_basis": {
                        "schema_version": "paragraph_history_order.v1",
                        "kind": "filename_lexicographic_v1",
                    },
                    "max_items": 100,
                }
                history = _payload(
                    await session.call_tool(
                        "trace_paragraph_history",
                        history_arguments,
                    )
                )
                if (
                    history["schema_version"] != "paragraph_history.v1"
                    or history["next_cursor"] is not None
                    or len(history["observations"]) != 4
                    or [
                        observation["resolution"]["reason"]
                        for observation in history["observations"][1:]
                    ]
                    != ["rejected_projection_unique"] * 3
                ):
                    raise ValueError("bundled demo paragraph history differs")
                selected = history["observations"][0]["selected_paragraph"]
                deletion = next(
                    (
                        unit
                        for unit in selected["change_units"]
                        if unit["change_type"] == "delete"
                    ),
                    None,
                )
                if (
                    deletion is None
                    or deletion["author"] != "53"
                    or selected["metadata_assurance"]["authorship_verified"] is not False
                    or selected["metadata_assurance"]["time_verified"] is not False
                    or {
                        unit["reference"]["paragraph_index"]
                        for unit in selected["change_units"]
                    }
                    != {selected["paragraph_ref"]["paragraph_index"]}
                ):
                    raise ValueError("bundled demo history change-unit assurance differs")
                verified = _payload(
                    await session.call_tool(
                        "verify_quote",
                        {
                            "path": source,
                            "anchor": paragraph_ref,
                            "quote": CAP_R3,
                            "paragraph_projection": (
                                "pending_text_revisions_rejected_v1"
                            ),
                        },
                    )
                )
                if (
                    verified["schema_version"] != "verification_result.v2"
                    or verified["verdict"] != "exact"
                    or verified["checked_projection"]["mode"]
                    != "pending_text_revisions_rejected_v1"
                    or verified["matches"][0]["side"]
                    != "paragraph_rejected_pending"
                ):
                    raise ValueError("bundled demo verify_quote v2 differs")
                exported = _payload(
                    await session.call_tool(
                        "export_decision_record",
                        {"workspace": "demo"},
                    )
                )
                record_types = {record["record_type"] for record in exported["records"]}
                if not {"paragraph_history.v1", "verification.v2"} <= record_types:
                    raise ValueError("bundled demo compact provenance differs")
                compact = json.dumps(exported, ensure_ascii=False, sort_keys=True)
                if any(
                    private in compact
                    for private in (str(stage_dir), CAP_R3, CAP_R4, '"53"')
                ):
                    raise ValueError("bundled demo compact provenance leaked private data")

                request_issued = asyncio.Event()
                cancellation_sent = asyncio.Event()
                dispatcher = session._dispatcher
                original_write = dispatcher._write

                async def observe_write(message, metadata):
                    # Observe the real dispatcher writes: cancellation counts
                    # only after tools/call was issued and the courtesy MCP
                    # notification was sent on the same live transport. This
                    # does not claim that synchronous server work stopped.
                    await original_write(message, metadata)
                    method = getattr(message, "method", None)
                    params = getattr(message, "params", None) or {}
                    if (
                        method == "tools/call"
                        and params.get("name") == "trace_paragraph_history"
                    ):
                        request_issued.set()
                    elif method == "notifications/cancelled":
                        cancellation_sent.set()

                dispatcher._write = observe_write
                cancelled = asyncio.create_task(
                    session.call_tool("trace_paragraph_history", history_arguments)
                )
                await asyncio.wait_for(request_issued.wait(), timeout=2)
                cancelled.cancel()
                try:
                    await cancelled
                except asyncio.CancelledError:
                    pass
                else:
                    raise ValueError("client request abandonment returned a result")
                await asyncio.wait_for(cancellation_sent.wait(), timeout=2)
                dispatcher._write = original_write
                after_cancel = await session.list_tools()
                if {tool.name for tool in after_cancel.tools} != EXPECTED_TOOLS:
                    raise ValueError("stdio session did not recover after cancellation")

                result = {
                    "bundled_demo_filenames": filenames,
                    "cancelled_request_side_effect_absence_verified": False,
                    "cancellation_notification_status": "passed",
                    "client_request_abandonment_status": "passed",
                    "history_exact_unique_count": 3,
                    "history_observation_count": len(history["observations"]),
                    "post_cancellation_session_recovery_status": "passed",
                    "round_map_candidate_document_count": mapped["coverage"][
                        "candidate_document_count"
                    ],
                    "stdio_tool_count": len(names),
                    "server_work_cancellation_verified": False,
                    "verification_schema_version": verified["schema_version"],
                }
    finally:
        stdio_transport._create_platform_compatible_process = original_spawn
    if process is None or process.returncode is None:
        raise ValueError("stdio server process survived transport teardown")
    await _prove_forced_transport_teardown(parameters)
    result["process_teardown_status"] = "passed"
    return result


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
