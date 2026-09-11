# SPDX-License-Identifier: Apache-2.0
"""Expected refusals stay actionable and private across MCP SDK versions."""

from copy import deepcopy
import json
from pathlib import Path

from mcp.client import Client
from mcp.server import MCPServer
import pytest

import veqtor_docx
from veqtor_docx.apply import ApplyError
from veqtor_mcp import server
from veqtor_mcp.contracts import contract_meta, local_journaling_annotations


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _payload(result) -> dict:
    assert not result.is_error
    if isinstance(result.structured_content, dict):
        return result.structured_content
    return json.loads(result.content[0].text)


def _error_text(result) -> str:
    assert result.is_error
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


async def _apply_arguments(session, matter: Path) -> dict:
    source = str(matter / "round-2-counterparty-redline.docx")
    extracted = _payload(
        await session.call_tool("extract_redlines", {"path": source})
    )
    cap = next(
        unit
        for unit in extracted["change_units"]
        if unit["clause_anchor"] and unit["clause_anchor"]["label"] == "14.2"
    )
    edits = [
        {
            "anchor": cap["anchor"],
            "delete_text": " in respect of all claims in aggregate.",
            "insert_text": " per claim.",
        }
    ]
    preflight = _payload(
        await session.call_tool(
            "preflight_edits", {"source_path": source, "edits": edits}
        )
    )
    assert preflight["batch_applicable"] is True
    return {
        "source_path": source,
        "output_path": str(matter / "PRIVATE_OUTPUT.docx"),
        "edits": edits,
        "preflight_proof": preflight["preflight_proof"],
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "refusal",
    ["output_exists", "preflight_proof_invalid", "preflight_binding_mismatch"],
)
async def test_apply_refusal_code_crosses_transport_without_private_details(
    tmp_path: Path, refusal: str
) -> None:
    matter = tmp_path / "PRIVATE_MATTER"
    veqtor_docx.generate_demo_rounds(matter)
    async with Client(server.mcp) as session:
        arguments = await _apply_arguments(session, matter)
        source_before = Path(arguments["source_path"]).read_bytes()
        output = Path(arguments["output_path"])
        if refusal == "output_exists":
            output.write_bytes(b"PRIVATE_EXISTING_OUTPUT")
        elif refusal == "preflight_proof_invalid":
            proof = arguments["preflight_proof"]
            proof["proof_sha256"] = "0" * 64
        else:
            # A valid proof for the original payload cannot authorize this edit.
            arguments["edits"][0]["insert_text"] = " per individual claim."
        text = _error_text(await session.call_tool("apply_edits", arguments))

    assert text == f"Error executing tool apply_edits: {refusal}: operation refused"
    assert str(matter) not in text
    assert "PRIVATE" not in text
    assert "Traceback" not in text
    assert Path(arguments["source_path"]).read_bytes() == source_before
    if refusal == "output_exists":
        assert output.read_bytes() == b"PRIVATE_EXISTING_OUTPUT"
    else:
        assert not output.exists()


@pytest.mark.anyio
async def test_invalid_selection_is_actionable_over_transport(tmp_path: Path) -> None:
    matter = tmp_path / "PRIVATE_INSPECTION"
    veqtor_docx.generate_demo_rounds(matter)
    arguments = {
        "path": str(matter / "round-1-outgoing-draft.docx"),
        "mode": "read",
        "selection": {},
    }
    async with Client(server.mcp) as session:
        text = _error_text(await session.call_tool("inspect_document", arguments))
    assert text == (
        "Error executing tool inspect_document: invalid_selection: operation refused"
    )
    assert "PRIVATE" not in text

    # The decorator must not alter library-side diagnostics or exception types.
    with pytest.raises(veqtor_docx.InspectError) as caught:
        server.inspect_document(**arguments)
    assert caught.value.code == "invalid_selection"
    assert str(caught.value) != "invalid_selection: operation refused"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (ApplyError("output_exists", "PRIVATE_TEXT /private/path"), "output_exists"),
        (ApplyError("/private/path", "PRIVATE_TEXT"), "docx_error"),
        (ApplyError("bad\nPRIVATE_TEXT", "PRIVATE_TEXT"), "docx_error"),
        (RuntimeError("PRIVATE_TEXT /private/path"), "internal_error"),
    ],
)
async def test_transport_adapter_does_not_leak_exception_details(
    monkeypatch: pytest.MonkeyPatch, exception: Exception, expected: str
) -> None:
    # Register through the public SDK API; direct and transport calls use the
    # same callable, with only the registered version receiving the adapter.
    probe = MCPServer("transport-error-probe")
    monkeypatch.setattr(server, "mcp", probe)

    @server._mcp_tool()
    def fail() -> str:
        raise exception

    with pytest.raises(type(exception)) as caught:
        fail()
    assert caught.value is exception
    async with Client(probe) as session:
        text = _error_text(await session.call_tool("fail", {}))
    assert expected in text
    assert "PRIVATE_TEXT" not in text
    assert "/private/path" not in text
    assert "Traceback" not in text


@pytest.mark.anyio
async def test_sanitized_workspace_suggestion_is_preserved(tmp_path: Path) -> None:
    parent = tmp_path / "PRIVATE_PARENT"
    child = parent / "rounds"
    veqtor_docx.generate_demo_rounds(child)
    async with Client(server.mcp) as session:
        _payload(await session.call_tool("list_rounds", {"folder": str(child)}))
        text = _error_text(
            await session.call_tool("export_decision_record", {"workspace": str(parent)})
        )
    assert "workspace_mismatch" in text
    assert '"relative_path":"rounds"' in text
    assert "PRIVATE_PARENT" not in text
    assert str(parent) not in text


@pytest.mark.anyio
async def test_all_nine_registered_schemas_match_unwrapped_functions() -> None:
    original = MCPServer("unwrapped-contract-probe")
    actual_tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert set(actual_tools) == set(server._RESULT_MODELS)
    for name, actual in actual_tools.items():
        function = getattr(server, name)
        assert not hasattr(function, "__wrapped__")
        original.tool(
            annotations=local_journaling_annotations(actual.annotations.title),
            meta=contract_meta(),
            structured_output=True,
        )(function)
    reference_tools = {tool.name: tool for tool in await original.list_tools()}
    for name, actual in actual_tools.items():
        reference = reference_tools[name]
        assert actual.input_schema == reference.input_schema
        assert actual.output_schema == reference.output_schema
        assert actual.description == reference.description
        assert actual.meta == reference.meta


def test_direct_apply_error_retains_core_exception_and_metadata(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    veqtor_docx.generate_demo_rounds(matter)
    source = str(matter / "round-2-counterparty-redline.docx")
    extracted = server.extract_redlines(source)
    cap = next(
        unit
        for unit in extracted["change_units"]
        if unit["clause_anchor"] and unit["clause_anchor"]["label"] == "14.2"
    )
    edits = [{
        "anchor": cap["anchor"],
        "delete_text": " in respect of all claims in aggregate.",
        "insert_text": " per claim.",
    }]
    preflight = server.preflight_edits(source, edits)
    proof = deepcopy(preflight["preflight_proof"])
    proof["proof_sha256"] = "0" * 64
    with pytest.raises(ApplyError) as caught:
        server.apply_edits(source, str(matter / "counter.docx"), edits, proof)
    assert caught.value.code == "preflight_proof_invalid"
    assert caught.value.metadata["failure_phase"] == "preflight_binding"
