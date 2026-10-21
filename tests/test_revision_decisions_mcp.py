# SPDX-License-Identifier: Apache-2.0
"""Closed transport and actor authority for selective revision decisions."""

import copy
import json
from pathlib import Path

import jsonschema
import pytest
from mcp.client import Client
from test_revision_decisions import make

from veqtor_mcp import server


@pytest.fixture
def anyio_backend():
    return "asyncio"


def payload(result):
    assert not result.is_error, result
    content = result.structured_content
    return (
        content.get("result", content)
        if isinstance(content, dict)
        else json.loads(result.content[0].text)
    )


@pytest.mark.anyio
async def test_mcp_select_foreign_property_with_own_actor_exact_backup(
    tmp_path, demo_dir, monkeypatch
):
    monkeypatch.setenv("VEQTOR_TRACKED_CHANGE_AUTHOR", "Selected Reviewer")
    monkeypatch.setenv("VEQTOR_COMMENT_INITIALS", "SR")
    for fn in (
        server._tracked_change_author,
        server._comment_initials,
        server._configured_author_profiles,
    ):
        fn.cache_clear()
    path = make(tmp_path, demo_dir, "pPrChange")
    before = path.read_bytes()
    try:
        async with Client(server.mcp) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            listing = payload(
                await client.call_tool("read_document_revisions", {"path": str(path)})
            )
            jsonschema.validate(listing, tools["read_document_revisions"].output_schema)
            assert listing["record_status"] == "disabled"
            member = listing["revisions"][0]
            assert member["author"] == "Fictional Counterparty"
            args = {
                "source_path": str(path),
                "file_sha256": listing["file_sha256"],
                "operations": [
                    {
                        "kind": "decide_revision",
                        "decision": "reject",
                        "target": {
                            "kind": "single",
                            "revision_ref": member["revision_ref"],
                        },
                    }
                ],
                "author_profile": {"name": "Selected Reviewer", "initials": "SR"},
            }
            preview = payload(await client.call_tool("preflight_document_edits", args))
            jsonschema.validate(
                preview, tools["preflight_document_edits"].output_schema
            )
            assert preview["batch_applicable"], preview
            assert path.read_bytes() == before
            denied = await client.call_tool(
                "apply_document_edits",
                {
                    **args,
                    "preflight_proof": preview["preflight_proof"],
                    "author_profile": {
                        "name": "Fictional Counterparty",
                        "initials": "FC",
                    },
                },
            )
            assert denied.is_error
            assert path.read_bytes() == before
            result = payload(
                await client.call_tool(
                    "apply_document_edits",
                    {**args, "preflight_proof": preview["preflight_proof"]},
                )
            )
            jsonschema.validate(result, tools["apply_document_edits"].output_schema)
            assert Path(result["backup_path"]).read_bytes() == before
            assert result["operations"][0]["actor_profile"] == args["author_profile"]
            assert (
                result["operations"][0]["members"][0]["author"]
                == "Fictional Counterparty"
            )
            reread = payload(
                await client.call_tool(
                    "read_document_revisions",
                    {"path": str(path), "file_sha256": result["output_sha256"]},
                )
            )
            assert reread["total_count"] == 0
            assert result["record_status"] == "disabled"
    finally:
        for fn in (
            server._tracked_change_author,
            server._comment_initials,
            server._configured_author_profiles,
        ):
            fn.cache_clear()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad",
    ["extra", "id_only", "wrong_part", "wrong_action", "noninteger_path", "bad_hash"],
)
async def test_closed_decision_targets_refuse_before_core(
    tmp_path, demo_dir, monkeypatch, bad
):
    path = make(tmp_path, demo_dir, "ins")
    before = path.read_bytes()
    async with Client(server.mcp) as client:
        listing = payload(
            await client.call_tool("read_document_revisions", {"path": str(path)})
        )
        operation = {
            "kind": "decide_revision",
            "decision": "accept",
            "target": {
                "kind": "single",
                "revision_ref": copy.deepcopy(listing["revisions"][0]["revision_ref"]),
            },
        }
        ref = operation["target"]["revision_ref"]
        if bad == "extra":
            operation["target"]["author"] = "Forged"
        if bad == "id_only":
            operation["target"] = {"kind": "single", "id": "21"}
        if bad == "wrong_part":
            ref["part_name"] = "word/header1.xml"
        if bad == "wrong_action":
            operation["decision"] = "accept_all"
        if bad == "noninteger_path":
            ref["element_path"] = [True]
        if bad == "bad_hash":
            ref["owner_sha256"] = "f" * 64 + "\n"

        def called(*a, **kw):
            raise AssertionError("invalid input reached mutation core")

        monkeypatch.setattr(server.drafting, "preflight_document_edits", called)
        result = await client.call_tool(
            "preflight_document_edits",
            {
                "source_path": str(path),
                "file_sha256": listing["file_sha256"],
                "operations": [operation],
            },
        )
        assert result.is_error
        assert path.read_bytes() == before
        assert not (path.parent / ".veqtor-backups").exists()


@pytest.mark.anyio
async def test_inventory_does_not_claim_uninspected_stories_are_complete(
    tmp_path, demo_dir
):
    path = make(tmp_path, demo_dir, "ins")
    async with Client(server.mcp) as client:
        listing = payload(
            await client.call_tool(
                "read_document_revisions", {"path": str(path), "max_items": 1}
            )
        )
        assert listing["coverage"] == "main_document_only"
        assert "word/styles.xml" in listing["uninspected_parts"]
        assert listing["next_offset"] is None
        stale = await client.call_tool(
            "read_document_revisions", {"path": str(path), "file_sha256": "f" * 64}
        )
        assert stale.is_error
