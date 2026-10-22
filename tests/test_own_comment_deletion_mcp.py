# SPDX-License-Identifier: Apache-2.0
"""A selected thread must survive closed-contract preflight, save and reread."""
import hashlib
from pathlib import Path

import jsonschema
import pytest
from mcp.client import Client

from test_drafting_v060_workflow import fixture
from veqtor_mcp import server


def payload(response):
    assert not response.is_error, response
    result = response.structured_content
    return result.get("result", result)


@pytest.mark.anyio
@pytest.mark.parametrize("delete_thread", [False, True])
async def test_delete_owned_messages_through_preflight_and_apply(tmp_path, demo_dir, monkeypatch, delete_thread):
    monkeypatch.setenv("VEQTOR_TRACKED_CHANGE_AUTHOR", "Synthetic Own Reviewer")
    monkeypatch.setenv("VEQTOR_COMMENT_INITIALS", "SR")
    monkeypatch.delenv("VEQTOR_OWN_AUTHOR_PROFILES", raising=False)
    path = fixture(tmp_path, demo_dir)
    async with Client(server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

        async def call(name, args):
            result = payload(await client.call_tool(name, args))
            jsonschema.validate(result, tools[name].output_schema)
            return result

        async def save(operations):
            original = path.read_bytes()
            args = {"source_path": str(path), "file_sha256": hashlib.sha256(original).hexdigest(),
                    "operations": operations}
            dry = await call("preflight_document_edits", args)
            assert dry["batch_applicable"], dry
            assert path.read_bytes() == original
            result = await call("apply_document_edits", {**args, "preflight_proof": dry["preflight_proof"]})
            assert Path(result["backup_path"]).read_bytes() == original
            return result

        inspected = await call("inspect_document", {"path": str(path), "mode": "browse", "max_items": 1})
        paragraph = inspected["paragraphs"][0]
        created = await save([{"kind": "add_comment", "paragraph_ref": paragraph["paragraph_ref"],
                               "quote": paragraph["text"], "text": "Our test thread."}])
        root = created["operations"][0]["id"]
        reply = (await save([{"kind": "reply_comment", "comment_id": root,
                              "text": "Our test reply."}]))["operations"][0]["id"]
        original = path.read_bytes()
        if delete_thread:
            incomplete = await call("preflight_document_edits", {
                "source_path": str(path), "file_sha256": hashlib.sha256(original).hexdigest(),
                "operations": [{"kind": "delete_comment_thread", "comment_id": root,
                                "member_comment_ids": [root]}],
            })
            assert not incomplete["batch_applicable"]
            assert incomplete["refusal_code"] == "comment_thread_selection_mismatch"
            assert path.read_bytes() == original
            result = await save([{"kind": "delete_comment_thread", "comment_id": root,
                                  "member_comment_ids": [root, reply]}])
            assert {r["id"] for r in result["operations"][0]["deleted_comments"]} == {root, reply}
        else:
            result = await save([{"kind": "delete_comment_reply", "comment_id": reply}])
            assert result["operations"][0]["id"] == reply
        comments = await call("read_document_comments", {"path": str(path)})
        assert reply not in {r["id"] for r in comments["comments"]}
        assert (root in {r["id"] for r in comments["comments"]}) is (not delete_thread)


def test_later_foreign_thread_refusal_discards_earlier_valid_reply_deletion(tmp_path, demo_dir):
    from test_drafting_v060_workflow import save, ref, PROFILE
    from veqtor_docx import drafting

    path = fixture(tmp_path, demo_dir)
    root = save(path, [{"kind": "add_comment", "paragraph_ref": ref(path),
                       "quote": "Supply", "text": "Our root."}])["operations"][0]["id"]
    reply = save(path, [{"kind": "reply_comment", "comment_id": root,
                        "text": "Our reply."}])["operations"][0]["id"]
    foreign = save(path, [{"kind": "add_comment", "paragraph_ref": ref(path),
                          "quote": "Supply", "text": "Foreign root."}],
                   {**PROFILE, "author": "Synthetic Other Reviewer"})["operations"][0]["id"]
    original = path.read_bytes()
    before_files = set(tmp_path.rglob("*"))
    result = drafting.preflight_document_edits(str(path), hashlib.sha256(original).hexdigest(), [
        {"kind": "delete_comment_reply", "comment_id": reply},
        {"kind": "delete_comment_thread", "comment_id": foreign, "member_comment_ids": [foreign]},
    ], **PROFILE)
    assert not result["batch_applicable"]
    assert result["refusal_code"] == "comment_author_mismatch"
    assert result["blocking_operation_index"] == 1
    assert path.read_bytes() == original and set(tmp_path.rglob("*")) == before_files
    assert reply in {c["id"] for c in drafting.read_document_comments(str(path))["comments"]}


@pytest.mark.parametrize("at_limit", [True, False])
def test_deletion_preview_budget_counts_bodies_and_metadata(tmp_path, demo_dir, monkeypatch, at_limit):
    from test_drafting_v060_workflow import save, ref, PROFILE
    from veqtor_docx import drafting

    path = fixture(tmp_path, demo_dir)
    root = save(path, [{"kind": "add_comment", "paragraph_ref": ref(path),
                       "quote": "Supply", "text": "Our comment."}])["operations"][0]["id"]
    reply = save(path, [{"kind": "reply_comment", "comment_id": root,
                        "text": "Our response."}])["operations"][0]["id"]
    original = path.read_bytes()
    args = (str(path), hashlib.sha256(original).hexdigest(), [{
        "kind": "delete_comment_thread", "comment_id": root, "member_comment_ids": [root, reply]}])
    baseline = drafting.preflight_document_edits(*args, **PROFILE)
    assert baseline["batch_applicable"]
    total = sum(len(row[field] or "") for row in baseline["operations"][0]["deleted_comments"]
                for field in ("text", "author", "date"))
    monkeypatch.setattr(drafting, "MAX_BATCH_CHARS", total if at_limit else total - 1)
    result = drafting.preflight_document_edits(*args, **PROFILE)
    assert result["batch_applicable"] is at_limit
    if not at_limit:
        assert result["refusal_code"] == "resource_limit_exceeded"
    assert path.read_bytes() == original
