# SPDX-License-Identifier: Apache-2.0
"""End-to-end smoke test of the MCP tool surface over an in-memory session."""

import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import ValidationError

import veqtor_docx
from veqtor_docx import rounds as rounds_module
from veqtor_docx import generate_demo_rounds
from veqtor_docx.contracts import REVISION_COUNT_BASIS_V1
from veqtor_mcp import __version__
from veqtor_mcp import records
from veqtor_mcp import server
from veqtor_mcp.contracts import (
    ExtractRedlinesResult,
    InspectDocumentResult,
    ListRoundsResult,
    MCP_CONTRACT_META_KEY,
    MCP_CONTRACT_SCHEMA_EXTENSION,
    MCP_CONTRACT_SCHEMA_VERSION,
)
from veqtor_mcp.server import mcp


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _payload(result) -> dict:
    if isinstance(result.structuredContent, dict):
        data = result.structuredContent
        return data.get("result", data)
    return json.loads(result.content[0].text)


def _error_text(result) -> str:
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def _rewrite_docx_part(path: Path, part_name: str, transform) -> None:
    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()
        parts = {info.filename: archive.read(info.filename) for info in infos}
    parts[part_name] = transform(parts[part_name])
    with zipfile.ZipFile(path, "w") as archive:
        for info in infos:
            archive.writestr(info, parts[info.filename])


def _dummy_preflight_proof() -> dict[str, str]:
    """Schema-valid placeholder for tests that fail before proof evaluation."""
    return {
        "schema_version": "preflight_proof.v1",
        "source_sha256": "0" * 64,
        "edits_sha256": "0" * 64,
        "tracked_change_author": "Veqtor MCP",
        "producer_build": "test-build",
        "candidate_sha256": "0" * 64,
        "proof_sha256": "0" * 64,
    }


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            ListRoundsResult,
            {
                "folder": "demo",
                "ordering_source": "filename_lexicographic_v1",
                "order_basis": {},
                "rounds": [],
                "skipped": [],
            },
        ),
        (
            ExtractRedlinesResult,
            {
                "path": "demo/round.docx",
                "file_sha256": "0" * 64,
                "part_name": "word/document.xml",
                "revision_count": 0,
                "change_units": [],
                "unsupported_revisions": {},
                "revision_inventory": {},
            },
        ),
    ],
)
def test_revision_count_basis_is_enforced_by_runtime_result_models(
    model,
    payload: dict,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(
            {
                **payload,
                "revision_count_basis": "wrong",
                "record_id": None,
                "record_status": "disabled",
            }
        )


def test_inspect_runtime_result_model_enforces_advertised_mode_shape() -> None:
    base = {
        "path": "demo/round.docx",
        "file_sha256": "0" * 64,
        "part_name": "word/document.xml",
        "search_scope": "word_document_xml_body_v1",
        "reading_mode": "accepted_current_v1",
        "container_policy": "canonical_body_flow_v1",
        "has_tracked_text_revisions": False,
        "revision_inventory": {},
        "coverage": {},
        "limits": {},
        "next_cursor": None,
        "producer": {"name": "veqtor-mcp"},
        "record_id": None,
        "record_status": "disabled",
    }

    InspectDocumentResult.model_validate({**base, "mode": "outline", "sections": []})
    InspectDocumentResult.model_validate({**base, "mode": "browse", "paragraphs": []})
    InspectDocumentResult.model_validate(
        {
            **base,
            "mode": "read",
            "paragraphs": [],
            "selection_kind": "section",
            "section_navigation": {},
        }
    )

    invalid = [
        {**base, "mode": "outline"},
        {**base, "mode": "outline", "sections": [], "paragraphs": []},
        {**base, "mode": "literal_search", "matches": []},
        {**base, "mode": "read", "paragraphs": []},
        {
            **base,
            "mode": "read",
            "paragraphs": [],
            "selection_kind": "section",
        },
    ]
    for payload in invalid:
        with pytest.raises(ValidationError):
            InspectDocumentResult.model_validate(payload)


@pytest.mark.anyio
async def test_protocol_initialization_reports_veqtor_version(monkeypatch) -> None:
    initialize = ClientSession.initialize
    observed_results = []

    async def capture_initialize(session):
        result = await initialize(session)
        observed_results.append(result)
        return result

    monkeypatch.setattr(ClientSession, "initialize", capture_initialize)

    async with create_connected_server_and_client_session(mcp._mcp_server):
        pass

    assert len(observed_results) == 1
    assert observed_results[0].serverInfo.name == "veqtor"
    assert observed_results[0].serverInfo.version == __version__


@pytest.mark.anyio
async def test_tool_contracts_are_versioned_typed_and_honestly_annotated() -> None:
    expected_output_core = {
        "list_rounds": {
            "folder",
            "ordering_source",
            "order_basis",
            "revision_count_basis",
            "rounds",
        },
        "extract_redlines": {
            "path",
            "file_sha256",
            "revision_count",
            "revision_count_basis",
            "change_units",
            "unsupported_revisions",
        },
        "inspect_document": {
            "mode",
            "file_sha256",
            "search_scope",
            "reading_mode",
            "container_policy",
            "coverage",
            "limits",
            "next_cursor",
        },
        "preflight_edits": {
            "source_sha256",
            "batch_applicable",
            "candidate_sha256",
            "edits",
            "preflight_proof",
        },
        "apply_edits": {
            "source_sha256",
            "output_sha256",
            "applied",
            "round_trip_check",
            "preflight_binding_status",
            "preflight_candidate_sha256",
            "candidate_output_sha256_match",
        },
        "verify_quote": {
            "verdict",
            "checked_anchor",
            "matches",
            "diff",
        },
        "export_decision_record": {
            "total_count",
            "records",
            "assurance",
            "current_export_event",
        },
    }

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}

    assert set(tools) == records.WRITABLE_TOOL_NAMES
    assert set(server._RESULT_MODELS) == set(tools)
    assert all(callable(getattr(server, name)) for name in tools)
    for name, tool in tools.items():
        registered_tool = mcp._tool_manager.get_tool(name)
        assert registered_tool is not None
        assert registered_tool.fn_metadata.output_model is server._RESULT_MODELS[name]
        assert server._RESULT_MODELS[name].contract_schema == tool.outputSchema
        assert tool.meta == {MCP_CONTRACT_META_KEY: MCP_CONTRACT_SCHEMA_VERSION}
        assert tool.outputSchema is not None
        assert tool.outputSchema[MCP_CONTRACT_SCHEMA_EXTENSION] == (
            MCP_CONTRACT_SCHEMA_VERSION
        )
        assert tool.outputSchema["type"] == "object"
        assert expected_output_core[name] <= set(tool.outputSchema["properties"])
        assert tool.annotations is not None
        # Every tool can append local provenance (export appends an access
        # event), so complete calls are neither read-only nor idempotent.
        assert tool.annotations.readOnlyHint is False
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is False
        assert tool.annotations.openWorldHint is False

    for name in ("preflight_edits", "apply_edits"):
        edit_schema = tools[name].inputSchema["properties"]["edits"]["items"]
        assert edit_schema["additionalProperties"] is False
        anchor_variants = edit_schema["properties"]["anchor"]["oneOf"]
        assert len(anchor_variants) == 2
        assert all(item["additionalProperties"] is False for item in anchor_variants)
        assert anchor_variants[0]["required"] == [
            "change_unit_id",
            "file_sha256",
        ]
        assert set(anchor_variants[1]["required"]) == {
            "schema_version",
            "change_unit_id",
            "file_sha256",
            "container_policy",
            "unit_fingerprint_sha256",
        }

    verify_anchor = tools["verify_quote"].inputSchema["properties"]["anchor"]
    assert len(verify_anchor["oneOf"]) == 3
    assert all(item["additionalProperties"] is False for item in verify_anchor["oneOf"])
    assert verify_anchor["oneOf"][2]["properties"]["schema_version"] == {
        "const": "paragraph_ref.v1"
    }
    inspect_schema = tools["inspect_document"].inputSchema
    assert inspect_schema["properties"]["mode"]["enum"] == [
        "outline",
        "literal_search",
        "browse",
        "read",
    ]
    selection_schema = inspect_schema["properties"]["selection"]["anyOf"][0]
    assert selection_schema["additionalProperties"] is False
    assert set(selection_schema["properties"]) == {
        "paragraph_ref",
        "section_ref",
    }
    inspect_output_variants = tools["inspect_document"].outputSchema["oneOf"]
    assert [
        item["properties"]["mode"]["const"] for item in inspect_output_variants
    ] == [
        "outline",
        "literal_search",
        "browse",
        "read",
        "read",
    ]
    apply_schema = tools["apply_edits"].inputSchema
    assert "preflight_proof" in apply_schema["required"]
    assert "preflight_proof" in tools["preflight_edits"].outputSchema["required"]
    proof_schema = apply_schema["properties"]["preflight_proof"]
    assert proof_schema["additionalProperties"] is False
    assert set(proof_schema["required"]) == {
        "schema_version",
        "source_sha256",
        "edits_sha256",
        "tracked_change_author",
        "producer_build",
        "candidate_sha256",
        "proof_sha256",
    }
    diagnostic_schema = tools["preflight_edits"].outputSchema["properties"]["edits"][
        "items"
    ]
    assert diagnostic_schema["properties"]["position_status"]["enum"] == [
        "supported",
        "unsupported",
        "not_evaluated",
    ]
    assert "position_supported" not in diagnostic_schema["properties"]
    assert tools["preflight_edits"].outputSchema["properties"]["failure_phase"][
        "anyOf"
    ][0]["enum"] == [
        "validation",
        "source",
        "matching",
        "planning",
        "surgery",
        "serialization",
        "round_trip",
        "preflight_binding",
        "publication",
    ]
    ordered_filenames = tools["list_rounds"].inputSchema["properties"][
        "ordered_filenames"
    ]
    ordered_sequence_schema = next(
        branch for branch in ordered_filenames["anyOf"] if branch.get("type") == "array"
    )
    assert ordered_sequence_schema["items"] == {"type": "string"}
    assert "ordered_filenames" not in tools["list_rounds"].inputSchema["required"]
    assert tools["export_decision_record"].outputSchema["properties"][
        "current_export_event"
    ]["properties"]["record_status"]["enum"] == [
        "written",
        "disabled",
        "write_failed",
    ]
    for tool_name in ("list_rounds", "extract_redlines"):
        output_schema = tools[tool_name].outputSchema
        assert output_schema["properties"]["revision_count_basis"] == {
            "const": REVISION_COUNT_BASIS_V1
        }
        assert "revision_count_basis" in output_schema["required"]


@pytest.mark.anyio
async def test_inspect_transport_rejects_schema_invalid_nested_output(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = veqtor_docx.inspect_document
    sentinel = "PRIVATE_INVALID_OUTPUT_SENTINEL"
    nested_sentinel = "PRIVATE_INVALID_NESTED_SENTINEL"

    def invalid_nested_result(*args, **kwargs):
        result = original(*args, **kwargs)
        result["search_scope"] = sentinel
        result["sections"] = [{"garbage": nested_sentinel}]
        return result

    monkeypatch.setattr(veqtor_docx, "inspect_document", invalid_nested_result)
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        response = await session.call_tool(
            "inspect_document",
            {
                "path": str(demo_dir / "round-1-outgoing-draft.docx"),
                "mode": "outline",
            },
        )
        exported_response = await session.call_tool(
            "export_decision_record",
            {"workspace": str(demo_dir), "max_records": 10},
        )

    assert response.isError is True
    error_text = _error_text(response)
    assert "output_contract_error" in error_text
    assert "tool output failed contract validation" in error_text
    assert sentinel not in error_text
    assert nested_sentinel not in error_text
    assert "garbage" not in error_text

    raw = records.read_records(
        str(demo_dir),
        max_records=10,
        include_payload=True,
    )
    inspection_records = [
        item for item in raw["records"] if item["tool_name"] == "inspect_document"
    ]
    assert len(inspection_records) == 1
    assert inspection_records[0]["result"]["status"] == "error"
    assert inspection_records[0]["result"]["error_code"] == ("output_contract_error")
    assert inspection_records[0]["provenance"] == {"failure_phase": "output_validation"}
    assert sentinel not in json.dumps(inspection_records[0], ensure_ascii=False)
    assert nested_sentinel not in json.dumps(inspection_records[0], ensure_ascii=False)

    assert exported_response.isError is False
    exported = _payload(exported_response)
    compact_inspection = next(
        item for item in exported["records"] if item["tool_name"] == "inspect_document"
    )
    assert compact_inspection["result"]["status"] == "error"
    assert compact_inspection["result"]["error_code"] == "output_contract_error"
    assert sentinel not in json.dumps(exported, ensure_ascii=False)
    assert nested_sentinel not in json.dumps(exported, ensure_ascii=False)


@pytest.mark.anyio
async def test_invalid_style_outline_is_error_recorded_before_mcp_validation(
    tmp_path: Path,
) -> None:
    matter = tmp_path / "matter"
    generate_demo_rounds(matter)
    source = matter / "round-1-outgoing-draft.docx"

    def make_outline_negative(styles: bytes) -> bytes:
        marker = b'w:outlineLvl w:val="0"'
        assert marker in styles
        return styles.replace(marker, b'w:outlineLvl w:val="-1"', 1)

    _rewrite_docx_part(source, "word/styles.xml", make_outline_negative)
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        response = await session.call_tool(
            "inspect_document",
            {"path": str(source), "mode": "outline"},
        )

    assert response.isError is True
    assert "file_unextractable" in _error_text(response)
    journal = records.read_records(str(matter), max_records=10, include_payload=True)
    inspection_records = [
        item for item in journal["records"] if item["tool_name"] == "inspect_document"
    ]
    assert len(inspection_records) == 1
    assert inspection_records[0]["result"]["status"] == "error"
    assert inspection_records[0]["result"]["error_code"] == "file_unextractable"


@pytest.mark.anyio
async def test_surrogate_literal_phrase_is_a_stable_mcp_error(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    generate_demo_rounds(matter)
    source = matter / "round-1-outgoing-draft.docx"

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        response = await session.call_tool(
            "inspect_document",
            {
                "path": str(source),
                "mode": "literal_search",
                "phrases": ["bad\ud800phrase"],
                "match_basis": "exact_literal",
            },
        )

    assert response.isError is True
    assert "invalid_phrase" in _error_text(response)
    journal = records.read_records(str(matter), max_records=10, include_payload=True)
    inspection_records = [
        item for item in journal["records"] if item["tool_name"] == "inspect_document"
    ]
    # The transport may reject a non-scalar input before the journaling layer;
    # it must never create a contradictory successful inspection record.
    assert all(item["result"]["status"] == "error" for item in inspection_records)


@pytest.mark.anyio
async def test_list_rounds_accepts_an_explicit_positional_manifest(
    demo_dir: Path,
) -> None:
    ordered_filenames = sorted(
        (path.name for path in demo_dir.glob("*.docx")),
        reverse=True,
    )
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        listed = await session.call_tool(
            "list_rounds",
            {
                "folder": str(demo_dir),
                "ordered_filenames": ordered_filenames,
            },
        )

    assert not listed.isError
    payload = _payload(listed)
    assert [item["filename"] for item in payload["rounds"]] == ordered_filenames
    assert payload["ordering_source"] == "explicit_filename_sequence_v1"
    assert payload["order_basis"] == {
        "kind": "caller_supplied_filename_sequence",
        "lineage_verified": False,
        "round_id_semantics": "position_only",
    }


@pytest.mark.anyio
async def test_verify_quote_rejects_anchor_fields_outside_closed_contract(
    demo_dir: Path,
) -> None:
    source = str(demo_dir / "round-2-counterparty-redline.docx")
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        extracted = _payload(
            await session.call_tool("extract_redlines", {"path": source})
        )
        anchor = {
            "change_unit_id": extracted["change_units"][0]["change_unit_id"],
            "file_sha256": extracted["file_sha256"],
            "unexpected": 1,
        }
        verified = await session.call_tool(
            "verify_quote",
            {"path": source, "anchor": anchor, "quote": "anything"},
        )

    assert verified.isError
    assert "invalid_anchor" in verified.content[0].text


@pytest.mark.anyio
async def test_inspect_document_and_paragraph_verify_are_hash_bound(
    demo_dir: Path,
) -> None:
    source = str(demo_dir / "round-1-outgoing-draft.docx")
    phrase = "Except as set out in Clause 14.3"
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        inspected = await session.call_tool(
            "inspect_document",
            {
                "path": source,
                "mode": "literal_search",
                "phrases": [phrase],
                "match_basis": "exact_literal",
                "max_items": 1,
            },
        )
        assert not inspected.isError
        inspection = _payload(inspected)
        paragraph_ref = inspection["matches"][0]["paragraph_ref"]
        verified = await session.call_tool(
            "verify_quote",
            {
                "path": source,
                "anchor": paragraph_ref,
                "quote": phrase,
            },
        )

    assert not verified.isError
    assert inspection["record_status"] == "written"
    assert inspection["search_scope"] == "word_document_xml_body_v1"
    assert inspection["coverage"]["complete_literal_match_count"] == 1
    verification = _payload(verified)
    assert verification["verdict"] == "exact"
    assert verification["checked_anchor"] == paragraph_ref
    assert verification["matches"] == [
        {
            "path": source,
            "part_name": "word/document.xml",
            "revision_ids": [],
            "clause": "14.2 Limitation of Liability",
            "side": "paragraph_current",
            "paragraph_index": paragraph_ref["paragraph_index"],
            "paragraph_text_sha256": paragraph_ref["paragraph_text_sha256"],
            "reading_mode": "accepted_current_v1",
        }
    ]


@pytest.mark.anyio
async def test_verify_quote_accepts_policy_bound_change_unit_anchor(
    demo_dir: Path,
) -> None:
    source = str(demo_dir / "round-2-counterparty-redline.docx")
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        extracted = _payload(
            await session.call_tool("extract_redlines", {"path": source})
        )
        unit = next(
            item
            for item in extracted["change_units"]
            if item["new_text"] == "USD 50,000"
        )
        verified = await session.call_tool(
            "verify_quote",
            {
                "path": source,
                "anchor": unit["anchor"],
                "quote": "USD 50,000",
            },
        )

    assert not verified.isError
    payload = _payload(verified)
    assert payload["verdict"] == "exact"
    assert payload["checked_anchor"] == unit["anchor"]
    assert payload["matches"][0]["side"] == "new"


@pytest.mark.anyio
async def test_tools_are_exposed_and_callable(demo_dir: Path) -> None:
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        tools = await session.list_tools()
        runtime_tools = {tool.name for tool in tools.tools}
        documented_tools = set(
            re.findall(
                r"^## `([^`]+)`$",
                (Path(__file__).parents[1] / "API.md").read_text(encoding="utf-8"),
                flags=re.MULTILINE,
            )
        )
        assert runtime_tools == records.WRITABLE_TOOL_NAMES
        assert documented_tools == runtime_tools
        export_tool = next(
            tool for tool in tools.tools if tool.name == "export_decision_record"
        )
        list_tool = next(tool for tool in tools.tools if tool.name == "list_rounds")
        extract_tool = next(
            tool for tool in tools.tools if tool.name == "extract_redlines"
        )
        apply_tool = next(tool for tool in tools.tools if tool.name == "apply_edits")
        for tool_name in ("preflight_edits", "apply_edits"):
            tool = next(tool for tool in tools.tools if tool.name == tool_name)
            assert "author" not in tool.inputSchema["properties"]
        assert "include_payload" not in export_tool.inputSchema["properties"]
        assert "actual expanded-output limits" in list_tool.description
        assert "split the folder and retry" in list_tool.description
        assert "without returning a partial round list" in list_tool.description
        assert "revision_count_basis" in list_tool.description
        assert "w:ins" in list_tool.description
        assert "revision_count_basis" in extract_tool.description
        assert "revision_inventory.total_revision_elements" in (
            extract_tool.description
        )
        assert "does not accept, reject or remove" in apply_tool.description
        assert "not a tamper-evident audit log" in export_tool.description
        assert "not authentication or a hash chain" in export_tool.description
        assert "access_event.v1" in export_tool.description
        assert "first" in export_tool.description
        assert "access_count" in export_tool.description
        assert re.search(
            r"privacy-minimized\s+compact projection", export_tool.description
        )

        listed = await session.call_tool("list_rounds", {"folder": str(demo_dir)})
        assert not listed.isError
        listed_payload = _payload(listed)
        assert listed_payload["record_status"] == "written"
        assert listed_payload["record_id"].startswith("dr_")
        assert listed_payload["revision_count_basis"] == REVISION_COUNT_BASIS_V1
        rounds = listed_payload["rounds"]
        assert len(rounds) == 4

        extracted = await session.call_tool(
            "extract_redlines", {"path": rounds[1]["path"]}
        )
        assert not extracted.isError
        assert _payload(extracted)["revision_count_basis"] == (REVISION_COUNT_BASIS_V1)
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
        assert export_payload["returned_count"] == len(export_payload["records"])
        assert export_payload["payloads"] == "compact"
        assert export_payload["assurance"] == {
            "journal_model": "best_effort_local_provenance",
            "model_payload": "compact_only",
            "raw_journal_visibility": "private_local_only",
            "raw_journal_result": ("tool_specific_summary_not_verbatim_live_response"),
            "compact_projection": "privacy_minimized_view_not_raw_journal",
            "access_event_policy": (
                "raw_journal_only_excluded_from_default_compact_records"
            ),
            "tamper_evident": False,
            "hash_chain": False,
            "record_id_guarantee": "strictly_increasing_only",
            "producer_identity": "python_source_files_snapshot_only",
            "content_hashes": "recheckable_fingerprints_not_authentication",
            "round_trip_scope": (
                "ooxml_semantic_diff_outside_touched_anchors_not_docx_byte_identity"
            ),
        }
        assert export_payload["records_scope"] == "substantive_records_only"
        assert export_payload["total_count_scope"] == (
            "substantive_records_before_cursor"
        )
        assert export_payload["access_events_recorded_locally"] is True
        assert export_payload["access_events_in_records"] is False
        assert export_payload["access_count_scope"] == (
            "all_prior_access_events_before_current_export"
        )
        assert export_payload["access_count_includes_current_export"] is False
        assert export_payload["current_export_event"] == {
            "record_id": export_payload["record_id"],
            "record_type": "access_event.v1",
            "record_status": "written",
            "recorded_locally": True,
            "included_in_records": False,
            "included_in_total_count": False,
            "included_in_access_count": False,
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
    assert (
        records.write_record(
            workspace=matter,
            tool_name="verify_quote",
            input_payload={"quote": sentinel},
            result={"status": "ok", "verdict": "not_found"},
            provenance={},
        )["record_status"]
        == "written"
    )

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
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

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
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
async def test_export_workspace_discovery_states_are_path_safe_at_mcp_boundary(
    tmp_path: Path,
) -> None:
    async def export_error(workspace: Path) -> str:
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as session:
            result = await session.call_tool(
                "export_decision_record",
                {"workspace": str(workspace), "max_records": 10},
            )
        assert result.isError
        return _error_text(result)

    uninitialized = tmp_path / "PRIVATE_EMPTY_MATTER"
    uninitialized.mkdir()
    text = await export_error(uninitialized)
    assert "workspace_uninitialized" in text
    assert '"candidate_count":0' in text
    assert str(uninitialized) not in text
    assert not (uninitialized / records.SIDECAR_DIR).exists()

    wrong_parent = tmp_path / "PRIVATE_WRONG_PARENT"
    child = wrong_parent / "rounds"
    child.mkdir(parents=True)
    assert (
        records.write_record(
            workspace=child,
            tool_name="list_rounds",
            input_payload={},
            result={"status": "ok"},
            provenance={},
        )["record_status"]
        == "written"
    )
    text = await export_error(wrong_parent)
    assert "workspace_mismatch" in text
    assert '"relative_path":"rounds"' in text
    assert str(wrong_parent) not in text
    assert not (wrong_parent / records.SIDECAR_DIR).exists()

    ambiguous = tmp_path / "PRIVATE_AMBIGUOUS_PARENT"
    for name in ("PRIVATE_ALPHA", "PRIVATE_BETA"):
        candidate = ambiguous / name
        candidate.mkdir(parents=True)
        assert (
            records.write_record(
                workspace=candidate,
                tool_name="list_rounds",
                input_payload={},
                result={"status": "ok"},
                provenance={},
            )["record_status"]
            == "written"
        )
    text = await export_error(ambiguous)
    assert "workspace_ambiguous" in text
    assert '"candidate_count_at_least":2' in text
    assert "PRIVATE_ALPHA" not in text
    assert "PRIVATE_BETA" not in text
    assert str(ambiguous) not in text
    assert not (ambiguous / records.SIDECAR_DIR).exists()


@pytest.mark.anyio
async def test_apply_edits_tool_end_to_end(demo_dir: Path, tmp_path: Path) -> None:
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        source = str(demo_dir / "round-2-counterparty-redline.docx")
        extracted = _payload(
            await session.call_tool("extract_redlines", {"path": source})
        )
        cap = next(
            u
            for u in extracted["change_units"]
            if u["clause_anchor"] and u["clause_anchor"]["label"] == "14.2"
        )
        out = str(tmp_path / "counter.docx")
        edits = [
            {
                "anchor": {
                    "change_unit_id": cap["change_unit_id"],
                    "file_sha256": extracted["file_sha256"],
                },
                "delete_text": " in respect of all claims in aggregate.",
                "insert_text": " per claim.",
            }
        ]
        preflighted = await session.call_tool(
            "preflight_edits",
            {"source_path": source, "edits": edits},
        )
        assert not preflighted.isError
        preflight_payload = _payload(preflighted)
        assert preflight_payload["record_status"] == "written"
        assert preflight_payload["batch_applicable"] is True
        assert (
            preflight_payload["tracked_change_author"]
            == server._tracked_change_author()
        )
        assert (
            preflight_payload["producer"]["build"] == records.SOURCE_SNAPSHOT_IDENTITY
        )
        assert not Path(out).exists()

        applied = await session.call_tool(
            "apply_edits",
            {
                "source_path": source,
                "output_path": out,
                "edits": edits,
                "preflight_proof": preflight_payload["preflight_proof"],
            },
        )
        assert not applied.isError
        payload = _payload(applied)
        assert payload["record_status"] == "written"
        assert payload["source_sha256"] == extracted["file_sha256"]
        assert payload["tracked_change_author"] == server._tracked_change_author()
        assert payload["producer"]["build"] == records.SOURCE_SNAPSHOT_IDENTITY
        assert payload["output_sha256"]
        assert payload["round_trip_check"]["status"] == "passed"
        assert payload["output_sha256"] == preflight_payload["candidate_sha256"]
        assert payload["preflight_binding_status"] == "verified"
        assert (
            payload["preflight_candidate_sha256"]
            == preflight_payload["candidate_sha256"]
        )
        assert payload["candidate_output_sha256_match"] is True
        assert Path(out).exists()

        # Fail-closed surfaces as a tool error and writes nothing.
        broken = await session.call_tool(
            "apply_edits",
            {
                "source_path": source,
                "output_path": str(tmp_path / "never.docx"),
                "edits": [
                    {
                        "anchor": {
                            "change_unit_id": "cu_999",
                            "file_sha256": extracted["file_sha256"],
                        },
                        "delete_text": "x",
                        "insert_text": "y",
                    }
                ],
                "preflight_proof": preflight_payload["preflight_proof"],
            },
        )
        assert broken.isError
        assert not (tmp_path / "never.docx").exists()


@pytest.mark.anyio
async def test_invalid_xml_edit_text_is_recorded_as_preflight_refusal(
    demo_dir: Path,
) -> None:
    source = str(demo_dir / "round-2-counterparty-redline.docx")
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        extracted = _payload(
            await session.call_tool("extract_redlines", {"path": source})
        )
        cap = next(
            unit
            for unit in extracted["change_units"]
            if unit["clause_anchor"] and unit["clause_anchor"]["label"] == "14.2"
        )
        refused = await session.call_tool(
            "preflight_edits",
            {
                "source_path": source,
                "edits": [
                    {
                        "anchor": {
                            "change_unit_id": cap["change_unit_id"],
                            "file_sha256": extracted["file_sha256"],
                        },
                        "delete_text": " in respect of all claims in aggregate.",
                        "insert_text": "USD\u0001 250,000",
                    }
                ],
            },
        )

    assert not refused.isError
    payload = _payload(refused)
    assert payload["batch_applicable"] is False
    assert payload["failure_phase"] == "validation"
    assert payload["refusal_code"] == "invalid_edit"
    assert payload["record_status"] == "written"
    history = records.read_records(
        str(demo_dir),
        max_records=20,
        include_access_events=True,
        include_payload=True,
    )
    recorded = next(
        item
        for item in reversed(history["records"])
        if item["tool_name"] == "preflight_edits"
    )
    assert recorded["result"]["refusal_code"] == "invalid_edit"
    assert recorded["provenance"]["tracked_change_author"] == (
        server._tracked_change_author()
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "insert_text",
    [None, "", False, 0, 3.14, [], {}],
    ids=["null", "empty", "false", "zero", "float", "list", "object"],
)
async def test_mcp_reinstate_preflight_rejects_present_insert_text_without_output(
    demo_dir: Path,
    tmp_path: Path,
    insert_text,
) -> None:
    from veqtor_docx.synthetic import CARVEOUT_DROPPED

    source = str(demo_dir / "round-4-counterparty-reply.docx")
    output = tmp_path / "never.docx"
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        extracted = _payload(
            await session.call_tool("extract_redlines", {"path": source})
        )
        cap = next(
            unit
            for unit in extracted["change_units"]
            if unit["change_type"] == "replace"
        )
        edits = [
            {
                "anchor": {
                    "change_unit_id": cap["change_unit_id"],
                    "file_sha256": extracted["file_sha256"],
                },
                "reinstate_text": CARVEOUT_DROPPED,
                "insert_text": insert_text,
            }
        ]
        preflight = await session.call_tool(
            "preflight_edits", {"source_path": source, "edits": edits}
        )

    assert not preflight.isError
    payload = _payload(preflight)
    assert payload["batch_applicable"] is False
    assert payload["failure_phase"] == "validation"
    assert payload["refusal_code"] == "invalid_edit"
    assert set(payload["edits"][0]) == {
        "edit_index",
        "change_unit_id",
        "status",
        "operation",
        "match_count",
        "target_author",
        "target_revision_ids",
        "position_status",
        "refusal_code",
    }
    assert not output.exists()

    history = records.read_records(str(demo_dir), max_records=50, include_payload=True)[
        "records"
    ]
    preflight_record = next(
        record
        for record in reversed(history)
        if record["tool_name"] == "preflight_edits"
    )
    assert preflight_record["result"]["refusal_code"] == "invalid_edit"


def test_tracked_change_author_environment_is_validated(monkeypatch) -> None:
    server._tracked_change_author.cache_clear()
    monkeypatch.delenv(server.TRACKED_CHANGE_AUTHOR_ENV, raising=False)
    assert server._tracked_change_author_from_environment() == "Veqtor MCP"

    monkeypatch.setenv(server.TRACKED_CHANGE_AUTHOR_ENV, "  John Deer  ")
    assert server._tracked_change_author_from_environment() == "John Deer"

    for invalid in ("   ", "bad\nname", "x" * 256, "\udcff"):
        monkeypatch.setenv(server.TRACKED_CHANGE_AUTHOR_ENV, invalid)
        with pytest.raises(RuntimeError):
            server._tracked_change_author_from_environment()
    server._tracked_change_author.cache_clear()


def test_blank_author_keeps_version_and_makes_doctor_and_startup_diagnostic() -> None:
    env = os.environ.copy()
    env[server.TRACKED_CHANGE_AUTHOR_ENV] = "   "

    version = subprocess.run(
        [sys.executable, "-m", "veqtor_mcp.server", "--version"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    doctor = subprocess.run(
        [sys.executable, "-m", "veqtor_mcp.server", "doctor"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    startup = subprocess.run(
        [sys.executable, "-m", "veqtor_mcp.server"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert version.returncode == 0
    assert version.stdout.strip() == f"veqtor-mcp {__version__}"
    assert "Traceback" not in version.stderr
    assert doctor.returncode == 2
    diagnosis = json.loads(doctor.stdout)
    assert diagnosis["status"] == "error"
    assert diagnosis["tracked_change_author"] is None
    assert diagnosis["configuration_error"]["code"] == ("tracked_change_author_invalid")
    assert "Traceback" not in doctor.stderr
    assert startup.returncode == 2
    assert "configuration error:" in startup.stderr
    assert "Traceback" not in startup.stderr


def test_cli_version_and_doctor(monkeypatch, capsys) -> None:
    monkeypatch.setattr(server.sys, "argv", ["veqtor-mcp", "--version"])
    server.main()
    assert capsys.readouterr().out.strip() == f"veqtor-mcp {__version__}"

    monkeypatch.setattr(server.sys, "argv", ["veqtor-mcp", "doctor"])
    server.main()
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["name"] == "veqtor-mcp"
    assert doctor["version"] == __version__
    assert doctor["build"] == records.SOURCE_SNAPSHOT_IDENTITY
    assert doctor["tracked_change_author"] == server._tracked_change_author()
    assert doctor["configuration_error"] is None
    assert doctor["status"] == "ok"


@pytest.mark.anyio
async def test_tool_errors_are_reported_not_raised(demo_dir: Path) -> None:
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        broken = await session.call_tool(
            "list_rounds", {"folder": str(demo_dir / "nope")}
        )
        assert broken.isError


@pytest.mark.anyio
async def test_round_scan_budget_overrun_is_a_stable_protocol_error(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rounds_module, "MAX_ROUND_TOTAL_EXPANDED_BYTES", 0)

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        result = await session.call_tool(
            "list_rounds",
            {"folder": str(demo_dir)},
        )

    text = _error_text(result)
    assert result.isError
    assert "resource_limit_exceeded" in text
    assert "aggregate expanded-output limit" in text
    assert "split the folder and retry" in text
    assert '"rounds"' not in text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool_name", "core_owner", "arguments"),
    [
        ("list_rounds", veqtor_docx, {"folder": None}),
        ("extract_redlines", veqtor_docx, {"path": None}),
        (
            "verify_quote",
            veqtor_docx,
            {
                "path": None,
                "anchor": {"change_unit_id": "cu_001", "file_sha256": "0" * 64},
                "quote": "quote",
            },
        ),
        ("preflight_edits", veqtor_docx, {"source_path": None, "edits": []}),
        (
            "apply_edits",
            veqtor_docx,
            {"source_path": None, "output_path": None, "edits": []},
        ),
        (
            "export_decision_record",
            records,
            {"workspace": None, "max_records": 10},
        ),
    ],
)
async def test_every_unexpected_tool_failure_is_sanitized_and_journaled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    core_owner,
    arguments: dict,
) -> None:
    matter = tmp_path / tool_name
    veqtor_docx.generate_demo_rounds(matter)
    source = str(matter / "round-2-counterparty-redline.docx")
    resolved_arguments = {
        key: (
            str(matter)
            if key in {"folder", "workspace"} and value is None
            else source
            if key in {"path", "source_path"} and value is None
            else str(tmp_path / "never.docx")
            if key == "output_path" and value is None
            else value
        )
        for key, value in arguments.items()
    }
    if tool_name == "apply_edits":
        resolved_arguments["preflight_proof"] = _dummy_preflight_proof()
    sentinel = f"PRIVATE_IMPLEMENTATION_SENTINEL_{tool_name}"
    core_name = (
        tool_name if core_owner is veqtor_docx else "export_records_with_access_event"
    )
    original = getattr(core_owner, core_name)

    def explode(*_args, **_kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(
        core_owner,
        core_name,
        explode,
    )
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        result = await session.call_tool(tool_name, resolved_arguments)
    monkeypatch.setattr(
        core_owner,
        core_name,
        original,
    )

    text = _error_text(result)
    assert result.isError
    assert "internal_error" in text
    assert sentinel not in text
    raw = records.read_records(
        str(matter),
        max_records=20,
        include_access_events=True,
        include_payload=True,
    )
    failure = next(
        record
        for record in reversed(raw["records"])
        if record["tool_name"] == tool_name
        and record["result"].get("error_code") == "internal_error"
    )
    assert failure["result"]["error"] == "unexpected internal failure"
    assert sentinel not in json.dumps(failure, ensure_ascii=False)


@pytest.mark.anyio
async def test_post_core_failure_uses_the_same_sanitized_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = tmp_path / "matter"
    veqtor_docx.generate_demo_rounds(matter)
    sentinel = "PRIVATE_POST_CORE_SENTINEL"

    # Provenance construction is intentionally inside the same boundary as the
    # core call and journal publication.
    def explode(_result):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(server, "_list_rounds_provenance", explode)
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        result = await session.call_tool("list_rounds", {"folder": str(matter)})

    text = _error_text(result)
    assert result.isError
    assert "internal_error" in text
    assert sentinel not in text
    raw = records.read_records(str(matter), max_records=10, include_payload=True)
    assert raw["records"][-1]["result"] == {
        "status": "error",
        "error_code": "internal_error",
        "error": "unexpected internal failure",
    }


@pytest.mark.anyio
async def test_export_permission_failure_never_exposes_the_workspace_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = tmp_path / "private-client-matter"
    matter.mkdir()
    sentinel = f"permission denied at {matter}"

    def deny_open(*_args, **_kwargs):
        raise PermissionError(sentinel)

    monkeypatch.setattr(records, "_open_workspace_fd", deny_open)
    with pytest.raises(records.DecisionRecordError) as error:
        records.read_records(str(matter), max_records=10)
    assert error.value.code == "workspace_unreadable"
    assert str(error.value) == "workspace_unreadable: workspace cannot be read"
    assert sentinel not in str(error.value)

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        result = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "max_records": 10},
        )
    text = _error_text(result)
    assert result.isError
    assert "workspace_unreadable" in text
    assert sentinel not in text
    assert str(matter) not in text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("sidecar_kind", "expected_code"),
    [
        ("symlink", "sidecar_symlink"),
        ("file", "sidecar_not_directory"),
    ],
)
async def test_decision_record_refusals_are_path_free_at_mcp_boundary(
    tmp_path: Path,
    sidecar_kind: str,
    expected_code: str,
) -> None:
    matter = tmp_path / "PRIVATE_CLIENT_MATTER"
    matter.mkdir()
    sidecar = matter / ".veqtor"
    if sidecar_kind == "symlink":
        target = tmp_path / "PRIVATE_SIDECAR_TARGET"
        target.mkdir()
        sidecar.symlink_to(target, target_is_directory=True)
    else:
        sidecar.write_text("private", encoding="utf-8")

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        result = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "max_records": 10},
        )

    text = _error_text(result)
    assert result.isError
    assert expected_code in text
    assert "decision-record operation refused" in text
    assert str(matter) not in text
    assert "PRIVATE" not in text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("list_rounds", {"folder": "bad\x00path"}),
        ("extract_redlines", {"path": "bad\x00path"}),
        (
            "verify_quote",
            {
                "path": "bad\x00path",
                "anchor": {"change_unit_id": "cu_001", "file_sha256": "0" * 64},
                "quote": "quote",
            },
        ),
        ("preflight_edits", {"source_path": "bad\x00path", "edits": []}),
        (
            "apply_edits",
            {
                "source_path": "bad\x00path",
                "output_path": "never.docx",
                "edits": [],
                "preflight_proof": _dummy_preflight_proof(),
            },
        ),
        ("export_decision_record", {"workspace": "bad\x00path"}),
    ],
)
async def test_unresolvable_mcp_paths_are_stable_tool_errors(
    tool_name: str, arguments: dict
) -> None:
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        result = await session.call_tool(tool_name, arguments)

    text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
    assert result.isError
    assert "invalid_path" in text
    assert "embedded null byte" not in text
    assert "lstat" not in text


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

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        exported = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "max_records": 10},
        )

    error_text = "\n".join(
        block.text for block in exported.content if hasattr(block, "text")
    )
    assert exported.isError
    assert "journal_corrupt" in error_text
    assert "decision-record operation refused" in error_text
    assert str(journal) not in error_text
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

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        exported = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "max_records": 10},
        )

    error_text = "\n".join(
        block.text for block in exported.content if hasattr(block, "text")
    )
    assert exported.isError
    assert "journal_corrupt" in error_text
    assert "decision-record operation refused" in error_text
    assert str(journal) not in error_text
    assert "Extra data" not in error_text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("payload", "reasons", "raw_detail"),
    [
        (
            b"[" * 10_000 + b"0" + b"]" * 10_000,
            ("JSON decoder rejected input", "maximum depth"),
            "maximum recursion depth",
        ),
        (
            b"1" * 5_000,
            ("JSON integer exceeds",),
            "int_max_str_digits",
        ),
    ],
    ids=["decoder_recursion", "oversized_integer"],
)
async def test_decoder_limit_failures_are_controlled_tool_errors(
    tmp_path: Path,
    payload: bytes,
    reasons: tuple[str, ...],
    raw_detail: str,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    journal = sidecar / records.JOURNAL_NAME
    journal.write_bytes(payload + b"\n")

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        exported = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "max_records": 10},
        )

    error_text = "\n".join(
        block.text for block in exported.content if hasattr(block, "text")
    )
    assert exported.isError
    assert "journal_corrupt" in error_text
    assert "decision-record operation refused" in error_text
    assert not any(reason in error_text for reason in reasons)
    assert raw_detail not in error_text
