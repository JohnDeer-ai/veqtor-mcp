# SPDX-License-Identifier: Apache-2.0
"""Smoke the installed wheel through modern, legacy, and in-memory MCP clients."""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import nullcontext
import hashlib
import io
from importlib.metadata import distribution
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client

from position_smoke import exercise_positions

from veqtor_docx import generate_demo_rounds
from veqtor_docx.synthetic import CAP_R3, CAP_R4
from veqtor_mcp import __version__
from veqtor_mcp._inspection_live import CheckedInspectionResult
from veqtor_mcp.records import SOURCE_SNAPSHOT_IDENTITY
from veqtor_mcp.server import mcp


# Keep the historical release surface separate from the development contract.
# Neither expectation is derived from the installed server's observed tools.
FROZEN_V04_TOOL_NAMES = (
    "list_rounds",
    "extract_redlines",
    "inspect_document",
    "map_rounds",
    "trace_paragraph_history",
    "preflight_edits",
    "apply_edits",
    "verify_quote",
    "export_decision_record",
)
NR02_TOOL_NAMES = (
    "list_rounds",
    "extract_redlines",
    "inspect_document",
    "map_rounds",
    "trace_paragraph_history",
    "preflight_edits",
    "apply_edits",
    "verify_quote",
    "export_decision_record",
    "read_deal_positions",
    "mutate_deal_positions",
)
EXPECTED_TOOL_NAMES_BY_VERSION = {
    "0.4.0": FROZEN_V04_TOOL_NAMES,
    "0.4.2.dev0": NR02_TOOL_NAMES,
    "0.4.2": NR02_TOOL_NAMES,
    "0.5.0.dev1": (*NR02_TOOL_NAMES, "read_document_comments",
                    "preflight_document_edits", "apply_document_edits"),
    "0.5.0.dev2": (*NR02_TOOL_NAMES, "read_document_comments",
                    "preflight_document_edits", "apply_document_edits"),
    "0.5.0.dev3": (*NR02_TOOL_NAMES, "read_document_comments",
                    "preflight_document_edits", "apply_document_edits"),
    "0.6.0.dev1": (*NR02_TOOL_NAMES, "read_deal_state", "mutate_deal_state", "read_round_issue_brief", "read_finalization_review", "compare_incoming_version", "read_document_tables", "check_bilingual_conditions", "read_document_comments", "read_document_revisions",
                   "preflight_document_edits", "apply_document_edits", "recover_document_operation"),
    "0.5.0": (*NR02_TOOL_NAMES, "read_document_comments",
                    "preflight_document_edits", "apply_document_edits"),
}


def _assert_tool_inventory(result) -> tuple[str, ...]:
    assert __version__ in EXPECTED_TOOL_NAMES_BY_VERSION, (
        f"No explicit installed-wheel tool contract for {__version__}"
    )
    names = tuple(tool.name for tool in result.tools)
    expected = EXPECTED_TOOL_NAMES_BY_VERSION[__version__]
    assert names == expected, (
        f"Installed {__version__} tool inventory differs: "
        f"expected {expected!r}, observed {names!r}"
    )
    return names


def _payload(result) -> dict:
    if isinstance(result.structured_content, dict):
        data = result.structured_content
        return data.get("result", data)
    return json.loads(result.content[0].text)


def _assert_producer(payload: dict) -> None:
    assert payload["record_status"] == "written"
    assert payload["record_id"].startswith("dr_")
    assert payload["producer"] == {
        "name": "veqtor-mcp",
        "version": __version__,
        "build": SOURCE_SNAPSHOT_IDENTITY,
    }


async def _exercise_public_v04(client: Client, matter: Path, listed: dict) -> None:
    latest = listed["rounds"][-1]["path"]
    located = _payload(
        await client.call_tool(
            "inspect_document",
            {
                "path": latest,
                "mode": "literal_search",
                "phrases": [CAP_R4],
                "match_basis": "exact_literal",
                "max_items": 1,
            },
        )
    )
    _assert_producer(located)
    paragraph_ref = located["matches"][0]["paragraph_ref"]
    verified = _payload(
        await client.call_tool(
            "verify_quote",
            {
                "path": latest,
                "anchor": paragraph_ref,
                "quote": CAP_R3,
                "paragraph_projection": "pending_text_revisions_rejected_v1",
            },
        )
    )
    _assert_producer(verified)
    assert verified["schema_version"] == "verification_result.v2"
    assert verified["verdict"] == "exact"
    assert verified["checked_projection"]["mode"] == (
        "pending_text_revisions_rejected_v1"
    )
    history = _payload(
        await client.call_tool(
            "trace_paragraph_history",
            {
                "folder": str(matter),
                "seed": {
                    "schema_version": "paragraph_history_seed.v1",
                    "path": latest,
                    "paragraph_ref": paragraph_ref,
                },
                "order_basis": {
                    "schema_version": "paragraph_history_order.v1",
                    "kind": "filename_lexicographic_v1",
                },
                "max_items": 100,
            },
        )
    )
    _assert_producer(history)
    assert history["schema_version"] == "paragraph_history.v1"
    assert [
        observation["resolution"]["reason"]
        for observation in history["observations"][1:]
    ] == ["rejected_projection_unique"] * 3


async def _dual_era_stdio_smoke() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="veqtor-wheel-stdio-") as root:
        matter = Path(root) / "matter"
        generate_demo_rounds(matter)
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "veqtor_mcp.server"],
            env={
                "VEQTOR_TRACKED_CHANGE_AUTHOR": "Veqtor installed-wheel stdio",
            },
        )
        negotiated: dict[str, str] = {}
        for mode, expected_protocol_version in (
            ("auto", "2026-07-28"),
            ("legacy", "2025-11-25"),
        ):
            async with Client(stdio_client(parameters), mode=mode) as client:
                negotiated[mode] = client.protocol_version
                tools = await client.list_tools()
                _assert_tool_inventory(tools)
                listed = _payload(
                    await client.call_tool(
                        "list_rounds",
                        {"folder": str(matter)},
                    )
                )
                _assert_producer(listed)
                assert listed["ordering_source"] == "filename_lexicographic_v1"
                await _exercise_public_v04(client, matter, listed)
                if __version__ == "0.6.0.dev1":
                    await _exercise_incoming_comparison(client, listed)
                    await _exercise_bilingual_columns(client)
            assert negotiated[mode] == expected_protocol_version
        return negotiated


async def _exercise_bilingual_columns(client: Client) -> dict:
    """Independent synthetic input through both new installed MCP tools."""
    with tempfile.TemporaryDirectory(prefix="veqtor-bilingual-smoke-") as root:
        source = Path(root).resolve() / "synthetic.docx"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as package:
            package.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            package.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>1,000.50 USD</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>1 000,50 USD</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:sectPr/></w:body></w:document>')
        payload = buffer.getvalue()
        source.write_bytes(payload)
        result = await client.call_tool("read_document_tables", {"path": str(source)})
        assert not result.is_error
        inventory = _payload(result)
        assert inventory["producer"] == {"name": "veqtor-mcp", "version": __version__, "build": SOURCE_SNAPSHOT_IDENTITY}
        assert inventory["record_status"] == "disabled"
        table = next(i for i in inventory["items"] if i["kind"] == "table")
        row = next(i for i in inventory["items"] if i["kind"] == "row")
        cells = [i for i in inventory["items"] if i["kind"] == "cell"]
        paragraphs = [i for i in inventory["items"] if i["kind"] == "paragraph"]
        pair = {"pair_id": "synthetic-payment", "table_ref": table["table_ref"], "row_ref": row["row_ref"], "conditions": []}
        condition = {"condition_id": "amount"}
        for side, language, index, decimal, grouping in (("left", "en", 0, ".", ","), ("right", "ru", 1, ",", " ")):
            pair[side] = {"cell_ref": cells[index]["cell_ref"], "language": language, "grid_column": index,
                          "format": {"decimal_separator": decimal, "grouping_separator": grouping, "date_order": "reject_ambiguous"}}
            p = paragraphs[index]
            condition[side] = {"paragraph_ref": p["paragraph_ref"], "start": 0, "end": len(p["text"]), "quote": p["text"]}
        pair["conditions"].append(condition)
        result = await client.call_tool("check_bilingual_conditions", {"path": str(source), "file_sha256": inventory["file_sha256"], "cell_pairs": [pair]})
        assert not result.is_error
        checked = _payload(result)
        assert checked["producer"] == inventory["producer"]
        assert checked["record_status"] == "disabled"
        assert checked["pairs"][0]["conditions"][0]["status"] == "numeric_values_and_units_match"
        assert checked["pairs"][0]["coverage"]["paired_values"] == 2
        assert checked["obligation_equivalence"] == "not_claimed"
        assert source.read_bytes() == payload and sorted(p.name for p in Path(root).iterdir()) == ["synthetic.docx"]
        return {"selected_conditions": 1, "paired_values": 2, "sources_unchanged": True,
                "obligation_equivalence": "not_claimed"}


async def _exercise_incoming_comparison(client: Client, listed: dict) -> dict:
    """Use explicit synthetic snapshots, drain pages, and balance all members."""
    selected = {side: {**source, "path": str(Path(source["path"]).resolve())}
                for side, source in zip(("sent", "incoming"), listed["rounds"][:2], strict=True)}
    arguments = {key: selected[side][field] for side in selected
                 for field, key in (("path", side + "_path"), ("sha256", side + "_sha256"))}
    members, groups, page_count, comparison_id = [], {}, 0, None
    cursor = None
    while True:
        result = await client.call_tool("compare_incoming_version", {
            **arguments, "max_items": 3, "cursor": cursor,
        })
        assert not result.is_error
        page = _payload(result)
        assert page["record_status"] == "disabled" and page["record_id"] is None
        assert page["producer"] == {"name": "veqtor-mcp", "version": __version__,
                                    "build": SOURCE_SNAPSHOT_IDENTITY}
        assert page["coverage"]["partition_complete"]
        if comparison_id is None:
            comparison_id = page["comparison_id"]
        assert comparison_id == page["comparison_id"]
        for item in page["items"]:
            if item["kind"] == "comparison_group":
                assert item["group_id"] not in groups
                groups[item["group_id"]] = item
            elif item["kind"] == "paragraph":
                members.append(item)
        page_count += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    for side, source in selected.items():
        expected = page["coverage"][side]["indexed_paragraphs"]
        own = [item for item in members if item["side"] == side]
        assert sorted(item["paragraph_ref"]["paragraph_index"] for item in own) == list(range(expected))
        counts = Counter(item["group_id"] for item in own)
        assert all(counts[key] == group[side + "_count"] for key, group in groups.items())
        assert all(item["paragraph_ref"]["file_sha256"] == source["sha256"] for item in own)
        assert hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest() == source["sha256"]
    assert page_count > 1
    return {"pages": page_count, "paragraph_memberships": len(members),
            "partition_complete": True, "sources_unchanged": True}


async def smoke() -> dict:
    installed = distribution("veqtor-mcp")
    assert installed.metadata["Name"] == "veqtor-mcp"
    assert installed.version == __version__
    assert CheckedInspectionResult.__module__ == "veqtor_mcp._inspection_live"
    stdio_protocol_versions = await _dual_era_stdio_smoke()
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
        async with Client(mcp) as session:
            tools = await session.list_tools()
            names = _assert_tool_inventory(tools)
            listed = _payload(
                await session.call_tool("list_rounds", {"folder": str(matter)})
            )
            _assert_producer(listed)
            await _exercise_public_v04(session, matter, listed)
            incoming_result = (await _exercise_incoming_comparison(session, listed)
                               if __version__ == "0.6.0.dev1" else None)
            bilingual_result = (await _exercise_bilingual_columns(session)
                                if __version__ == "0.6.0.dev1" else None)
            positions_result = (
                await exercise_positions(session)
                if __version__ in {"0.4.2.dev0", "0.4.2", "0.5.0.dev1", "0.5.0.dev2", "0.5.0.dev3", "0.5.0", "0.6.0.dev1"} else None
            )
            source = listed["rounds"][1]["path"]
            inspected = _payload(
                await session.call_tool(
                    "inspect_document",
                    {"path": source, "mode": "outline", "max_items": 1},
                )
            )
            _assert_producer(inspected)
            assert inspected["mode"] == "outline"
            assert inspected["file_sha256"] == listed["rounds"][1]["sha256"]
            assert inspected["search_scope"] == "word_document_xml_body_v1"
            assert inspected["revision_inventory"]["schema_version"] == (
                "revision_inventory.v2"
            )
            extracted = _payload(
                await session.call_tool("extract_redlines", {"path": source})
            )
            _assert_producer(extracted)
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
            _assert_producer(verified)
            assert verified["schema_version"] == "verification_result.v2"
            assert verified["checked_projection"] is None
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
            _assert_producer(preflight)
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
            _assert_producer(applied)
            assert output.is_file()
            assert applied["preflight_binding_status"] == "verified"
            assert (
                applied["preflight_candidate_sha256"] == preflight["candidate_sha256"]
            )
            assert applied["candidate_output_sha256_match"] is True
            assert applied["output_sha256"] == preflight["candidate_sha256"]
            browsed = _payload(
                await session.call_tool(
                    "inspect_document",
                    {"path": source, "mode": "browse", "max_items": 1},
                )
            )
            _assert_producer(browsed)
            mapped = _payload(
                await session.call_tool(
                    "map_rounds",
                    {
                        "folder": str(matter),
                        "seed": {
                            "schema_version": "round_map_seed.v1",
                            "path": source,
                            "paragraph_ref": browsed["paragraphs"][0]["paragraph_ref"],
                        },
                        "max_items": 100,
                    },
                )
            )
            _assert_producer(mapped)
            derivations = [
                item
                for item in mapped["items"]
                if item["item_type"] == "relationship"
                and item["relationship_type"] == "recorded_derivation"
            ]
            assert any(
                item["from_id"] == f"rm_doc_v1:{applied['source_sha256']}"
                and item["to_id"] == f"rm_doc_v1:{applied['output_sha256']}"
                and item["derivation_recorded"] is True
                and item["lineage_verified"] is False
                and item["chronology_verified"] is False
                for item in derivations
            )
            exported = _payload(
                await session.call_tool(
                    "export_decision_record",
                    {"workspace": str(matter)},
                )
            )
            _assert_producer(exported)
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
            _assert_producer(exported_again)
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
                "stdio_protocol_versions": stdio_protocol_versions,
                "tool_count": len(names),
                "used_bundled_demo": configured_matter is not None,
                "positions": positions_result,
                "incoming_comparison": incoming_result,
                "bilingual_columns": bilingual_result,
            }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(smoke()), sort_keys=True))
