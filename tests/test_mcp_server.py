# SPDX-License-Identifier: Apache-2.0
"""End-to-end smoke test of the MCP tool surface over an in-memory session."""

import json
import re
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from veqtor_mcp import records
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
        runtime_tools = {tool.name for tool in tools.tools}
        documented_tools = set(
            re.findall(
                r"^## `([^`]+)`$",
                (Path(__file__).parents[1] / "API.md").read_text(
                    encoding="utf-8"
                ),
                flags=re.MULTILINE,
            )
        )
        assert runtime_tools == records.WRITABLE_TOOL_NAMES
        assert documented_tools == runtime_tools
        export_tool = next(
            tool for tool in tools.tools if tool.name == "export_decision_record"
        )
        assert "include_payload" not in export_tool.inputSchema["properties"]
        assert "not a tamper-evident audit log" in export_tool.description
        assert "not authentication or a hash chain" in export_tool.description

        listed = await session.call_tool("list_rounds", {"folder": str(demo_dir)})
        assert not listed.isError
        listed_payload = _payload(listed)
        assert listed_payload["record_status"] == "written"
        assert listed_payload["record_id"].startswith("dr_")
        rounds = listed_payload["rounds"]
        assert len(rounds) == 4

        extracted = await session.call_tool(
            "extract_redlines", {"path": rounds[1]["path"]}
        )
        assert not extracted.isError
        payload = _payload(extracted)
        assert payload["record_status"] == "written"
        assert payload["file_sha256"] == rounds[1]["sha256"]
        anchors = {
            u["clause_anchor"]["label"]
            for u in payload["change_units"]
            if u["clause_anchor"]
        }
        assert "14.2" in anchors

        exported = await session.call_tool(
            "export_decision_record", {"workspace": str(demo_dir), "max_records": 2}
        )
        assert not exported.isError
        export_payload = _payload(exported)
        assert export_payload["record_status"] == "written"
        assert export_payload["total_count"] >= 2
        assert len(export_payload["records"]) <= 2
        assert export_payload["payloads"] == "compact"
        assert export_payload["assurance"] == {
            "journal_model": "best_effort_local_provenance",
            "model_payload": "compact_only",
            "tamper_evident": False,
            "hash_chain": False,
            "record_id_guarantee": "strictly_increasing_only",
            "producer_identity": "python_source_files_snapshot_only",
            "content_hashes": "recheckable_fingerprints_not_authentication",
            "round_trip_scope": (
                "ooxml_semantic_diff_outside_touched_anchors_not_docx_byte_identity"
            ),
        }


@pytest.mark.anyio
@pytest.mark.parametrize("stale_value", [True, 1, "true", "yes"])
async def test_stale_full_export_argument_never_returns_private_payload(
    tmp_path: Path,
    stale_value: object,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sentinel = "PRIVATE_STALE_FULL_EXPORT_SENTINEL_53"
    assert records.write_record(
        workspace=matter,
        tool_name="verify_quote",
        input_payload={"quote": sentinel},
        result={"status": "ok", "verdict": "not_found"},
        provenance={},
    )["record_status"] == "written"

    async with create_connected_server_and_client_session(
        mcp._mcp_server
    ) as session:
        exported = await session.call_tool(
            "export_decision_record",
            {
                "workspace": str(matter),
                "max_records": 1,
                "include_payload": stale_value,
            },
        )

    assert not exported.isError
    payload = _payload(exported)
    assert payload["payloads"] == "compact"
    assert sentinel not in json.dumps(payload, ensure_ascii=False)
    full = records.read_records(
        str(matter),
        max_records=10,
        include_access_events=True,
        include_payload=True,
    )
    access = next(
        record
        for record in full["records"]
        if record["tool_name"] == "export_decision_record"
    )
    assert "include_payload" not in access["input"]
    assert access["result"]["payloads"] == "compact"


@pytest.mark.anyio
async def test_invalid_export_cursor_does_not_create_history(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()

    async with create_connected_server_and_client_session(
        mcp._mcp_server
    ) as session:
        exported = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "before_record_id": "bad"},
        )

    assert exported.isError
    error_text = "\n".join(
        block.text for block in exported.content if hasattr(block, "text")
    )
    assert "invalid_before_record_id" in error_text
    assert not (matter / records.SIDECAR_DIR).exists()


@pytest.mark.anyio
async def test_apply_edits_tool_end_to_end(demo_dir: Path, tmp_path: Path) -> None:
    async with create_connected_server_and_client_session(
        mcp._mcp_server
    ) as session:
        source = str(demo_dir / "round-2-counterparty-redline.docx")
        extracted = _payload(await session.call_tool("extract_redlines", {"path": source}))
        cap = next(
            u
            for u in extracted["change_units"]
            if u["clause_anchor"] and u["clause_anchor"]["label"] == "14.2"
        )
        out = str(tmp_path / "counter.docx")
        applied = await session.call_tool(
            "apply_edits",
            {
                "source_path": source,
                "output_path": out,
                "edits": [
                    {
                        "anchor": {
                            "change_unit_id": cap["change_unit_id"],
                            "file_sha256": extracted["file_sha256"],
                        },
                        "delete_text": " in respect of all claims in aggregate.",
                        "insert_text": " per claim.",
                    }
                ],
            },
        )
        assert not applied.isError
        payload = _payload(applied)
        assert payload["record_status"] == "written"
        assert payload["output_sha256"]
        assert payload["round_trip_check"]["status"] == "passed"
        assert Path(out).exists()

        # Fail-closed surfaces as a tool error and writes nothing.
        broken = await session.call_tool(
            "apply_edits",
            {
                "source_path": source,
                "output_path": str(tmp_path / "never.docx"),
                "edits": [
                    {
                        "anchor": {"change_unit_id": "cu_999", "file_sha256": extracted["file_sha256"]},
                        "delete_text": "x",
                        "insert_text": "y",
                    }
                ],
            },
        )
        assert broken.isError
        assert not (tmp_path / "never.docx").exists()


@pytest.mark.anyio
async def test_tool_errors_are_reported_not_raised(demo_dir: Path) -> None:
    async with create_connected_server_and_client_session(
        mcp._mcp_server
    ) as session:
        broken = await session.call_tool("list_rounds", {"folder": str(demo_dir / "nope")})
        assert broken.isError


@pytest.mark.anyio
async def test_surrogate_corrupt_journal_is_a_controlled_tool_error(
    tmp_path: Path,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    meta = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={"folder": str(matter)},
        result={"status": "ok"},
        provenance={},
    )
    assert meta["record_status"] == "written"
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    record = json.loads(journal.read_text(encoding="utf-8"))
    record["producer"]["build"] = "\udcff"
    journal.write_text(json.dumps(record) + "\n", encoding="utf-8")

    async with create_connected_server_and_client_session(
        mcp._mcp_server
    ) as session:
        exported = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "max_records": 10},
        )

    error_text = "\n".join(
        block.text for block in exported.content if hasattr(block, "text")
    )
    assert exported.isError
    assert "journal_corrupt" in error_text
    assert "invalid Unicode scalar value" in error_text
    assert "surrogates not allowed" not in error_text


@pytest.mark.anyio
async def test_unterminated_journal_is_a_controlled_tool_error(
    tmp_path: Path,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    meta = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={"folder": str(matter)},
        result={"status": "ok"},
        provenance={},
    )
    assert meta["record_status"] == "written"
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    journal.write_bytes(journal.read_bytes().removesuffix(b"\n"))

    async with create_connected_server_and_client_session(
        mcp._mcp_server
    ) as session:
        exported = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "max_records": 10},
        )

    error_text = "\n".join(
        block.text for block in exported.content if hasattr(block, "text")
    )
    assert exported.isError
    assert "journal_corrupt" in error_text
    assert "unterminated journal record" in error_text
    assert "Extra data" not in error_text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("payload", "reason", "raw_detail"),
    [
        (
            b"[" * 10_000 + b"0" + b"]" * 10_000,
            "JSON decoder rejected input",
            "maximum recursion depth",
        ),
        (
            b"1" * 5_000,
            "JSON integer exceeds",
            "int_max_str_digits",
        ),
    ],
    ids=["decoder_recursion", "oversized_integer"],
)
async def test_decoder_limit_failures_are_controlled_tool_errors(
    tmp_path: Path,
    payload: bytes,
    reason: str,
    raw_detail: str,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    journal = sidecar / records.JOURNAL_NAME
    journal.write_bytes(payload + b"\n")

    async with create_connected_server_and_client_session(
        mcp._mcp_server
    ) as session:
        exported = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "max_records": 10},
        )

    error_text = "\n".join(
        block.text for block in exported.content if hasattr(block, "text")
    )
    assert exported.isError
    assert "journal_corrupt" in error_text
    assert reason in error_text
    assert raw_detail not in error_text
