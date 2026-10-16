# SPDX-License-Identifier: Apache-2.0
"""Drafting transport rejects malformed input without leaking it or writing."""

import copy
import hashlib
import json
import zipfile

from lxml import etree

import jsonschema
import pytest
from mcp.client import Client
from pydantic import TypeAdapter, ValidationError

from veqtor_docx import ApplyError, inspect_document
from veqtor_docx._ooxml import w
from veqtor_mcp import server
from veqtor_mcp._drafting_contract import DraftingShaInput, SHA, _APPLIED


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _error_text(result):
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def _payload(result):
    if isinstance(result.structured_content, dict):
        return result.structured_content.get("result", result.structured_content)
    return json.loads(result.content[0].text)


def _fixture(tmp_path, demo_dir):
    path = tmp_path / "private-working-document.docx"
    path.write_bytes((demo_dir / "round-1-outgoing-draft.docx").read_bytes())
    result = inspect_document(str(path), "browse", max_items=1)
    operation = {"kind": "replace_text", "paragraph_ref": result["paragraphs"][0]["paragraph_ref"],
                 "old_text": "Supply", "new_text": "Services"}
    return path, {"source_path": str(path), "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "operations": [operation]}


@pytest.mark.parametrize("invalid", ["0" * 63, "0" * 65, "A" * 64,
    "0" * 64 + "\n", "0" * 64 + "\r\n", True, 12, None])
def test_sha_runtime_and_advertised_schema_agree(invalid):
    validator = TypeAdapter(DraftingShaInput)
    assert validator.validate_python("a" * 64) == "a" * 64
    assert jsonschema.Draft202012Validator(SHA).is_valid("a" * 64)
    assert not jsonschema.Draft202012Validator(SHA).is_valid(invalid)
    with pytest.raises(ValidationError):
        validator.validate_python(invalid)


@pytest.mark.anyio
@pytest.mark.parametrize("tool_name", ["preflight_document_edits", "apply_document_edits"])
@pytest.mark.parametrize("invalid", ["0" * 63, "A" * 64, "0" * 64 + "\n", True,
                                       "PRIVATE_BAD_HASH_SENTINEL"])
async def test_invalid_sha_stops_before_execution_and_writes_nothing(tmp_path, demo_dir, monkeypatch, tool_name, invalid):
    path, arguments = _fixture(tmp_path, demo_dir)
    original = path.read_bytes()
    arguments["file_sha256"] = invalid
    if tool_name == "apply_document_edits":
        arguments["preflight_proof"] = {}
    called = []

    def forbidden_core(*args, **kwargs):
        called.append(True)
        raise RuntimeError("PRIVATE_CORE_WAS_CALLED")

    monkeypatch.setattr(server.drafting, tool_name, forbidden_core)
    async with Client(server.mcp) as session:
        result = await session.call_tool(tool_name, arguments)
    assert result.is_error
    assert _error_text(result) == "invalid_arguments: tool input failed validation"
    assert called == []
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.anyio
@pytest.mark.parametrize("tool_name", ["preflight_document_edits", "apply_document_edits"])
async def test_extra_operation_keys_are_closed_and_private_values_not_echoed(tmp_path, demo_dir, tool_name):
    path, arguments = _fixture(tmp_path, demo_dir)
    original = path.read_bytes()
    arguments["operations"][0]["PRIVATE_UNKNOWN_KEY"] = "PRIVATE_PAYLOAD_SENTINEL"
    if tool_name == "apply_document_edits":
        arguments["preflight_proof"] = {}
    async with Client(server.mcp) as session:
        result = await session.call_tool(tool_name, arguments)
    if tool_name == "preflight_document_edits":
        payload = _payload(result)
        assert not result.is_error
        assert payload["batch_applicable"] is False
        assert payload["refusal_code"] == "invalid_edit"
        assert payload["operations"] == []
    else:
        assert result.is_error
        assert "invalid_edit" in _error_text(result)
    rendered = _error_text(result)
    assert "PRIVATE_PAYLOAD_SENTINEL" not in rendered
    assert "PRIVATE_UNKNOWN_KEY" not in rendered
    assert str(path) not in rendered
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.anyio
@pytest.mark.parametrize("drift", ["operations", "proof", "destination"])
async def test_transport_proof_drift_is_sanitized_and_never_publishes(tmp_path, demo_dir, drift):
    path, arguments = _fixture(tmp_path, demo_dir)
    original = path.read_bytes()
    async with Client(server.mcp) as session:
        preflight = _payload(await session.call_tool("preflight_document_edits", arguments))
        assert preflight["batch_applicable"]
        modified = copy.deepcopy(arguments)
        modified["preflight_proof"] = preflight["preflight_proof"]
        if drift == "operations":
            modified["operations"][0]["new_text"] = "PRIVATE_DRIFT_SENTINEL"
        elif drift == "proof":
            modified["preflight_proof"]["PRIVATE_UNKNOWN_KEY"] = "PRIVATE_DRIFT_SENTINEL"
        else:
            modified["output_path"] = str(tmp_path / "PRIVATE_DRIFT_SENTINEL.docx")
        result = await session.call_tool("apply_document_edits", modified)
    assert result.is_error
    assert "preflight_binding_mismatch" in _error_text(result)
    assert "PRIVATE_DRIFT_SENTINEL" not in _error_text(result)
    assert str(path) not in _error_text(result)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.anyio
async def test_malformed_output_is_sanitized_at_transport(tmp_path, monkeypatch):
    def invalid_output(*args, **kwargs):
        return {"schema_version": "document_drafting.v1", "PRIVATE_OUTPUT_KEY": "PRIVATE_OUTPUT_SENTINEL"}

    monkeypatch.setattr(server.drafting, "read_document_comments", invalid_output)
    async with Client(server.mcp) as session:
        result = await session.call_tool("read_document_comments", {"path": str(tmp_path / "private.docx")})
    assert result.is_error
    assert "output_contract_error" in _error_text(result)
    assert "PRIVATE_OUTPUT_SENTINEL" not in _error_text(result)
    assert "PRIVATE_OUTPUT_KEY" not in _error_text(result)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("operation", ["replace", "delete", "edit_own_insertion", "counter"])
def test_closed_text_operation_output_contract(operation):
    pending = operation in {"edit_own_insertion", "counter"}
    count = 1 if operation in {"delete", "edit_own_insertion"} else 2
    item = {"index": 0, "operation": operation, "old_text": "old", "new_text": "" if operation == "delete" else "new",
            "start": 0, "end": 3, "revision_ids": [str(index) for index in range(count)],
            "target_revision_id": "7" if pending else None, "target_author": "Author" if pending else None,
            "paragraph_index": 0}
    validator = jsonschema.Draft202012Validator(_APPLIED)
    assert validator.is_valid([item])
    assert not validator.is_valid([{**item, "PRIVATE_UNKNOWN_KEY": "unvalidated"}])
    for invalid_index in (True, -1, 100):
        assert not validator.is_valid([{**item, "index": invalid_index}])
    if pending:
        assert not validator.is_valid([{**item, "target_revision_id": None}])
        assert not validator.is_valid([{**item, "target_author": None}])
    else:
        assert not validator.is_valid([{**item, "target_revision_id": "unexpected"}])
    if operation == "edit_own_insertion":
        assert not validator.is_valid([{**item, "revision_ids": ["7", "8"]}])
    if operation == "delete":
        assert not validator.is_valid([{**item, "new_text": "unexpected insertion"}])


@pytest.mark.parametrize("operation", ["add_comment", "update_comment", "delete_comment"])
def test_closed_comment_operation_output_contract(operation):
    item = {"index": 0, "operation": operation, "id": "0", "author": "Author", "initials": "A",
            "text": "Comment", "paragraph_indexes": [0]}
    validator = jsonschema.Draft202012Validator(_APPLIED)
    assert validator.is_valid([item])
    assert not validator.is_valid([{**item, "PRIVATE_UNKNOWN_KEY": "unvalidated"}])
    assert not validator.is_valid([{**item, "paragraph_indexes": [-1]}])
    assert not validator.is_valid([{**item, "paragraph_indexes": [True]}])
    assert not validator.is_valid([{key: value for key, value in item.items() if key != "id"}])


@pytest.mark.parametrize("invalid", [
    [],
    [{"name": "ordinary", "id": "3", "position": "replacement_start"}],
    [{"name": "_GoBack", "id": "", "position": "replacement_start"}],
    [{"name": "_GoBack", "id": "3", "position": "replacement_end"}],
    [{"name": "_GoBack", "id": "3", "position": "replacement_start", "extra": True}],
    [{"name": "_GoBack", "id": "3", "position": "replacement_start"}] * 2,
])
def test_relocated_bookmark_output_is_narrow_and_closed(invalid):
    item = {"index": 0, "operation": "edit_own_insertion", "old_text": "old", "new_text": "new",
            "start": 0, "end": 3, "revision_ids": ["7"],
            "target_revision_id": "7", "target_author": "Shilov, Ilya", "paragraph_index": 0}
    validator = jsonschema.Draft202012Validator(_APPLIED)
    correct = [{"name": "_GoBack", "id": "3", "position": "replacement_start"}]
    assert validator.is_valid([item])
    assert validator.is_valid([{**item, "relocated_bookmarks": correct}])
    assert not validator.is_valid([{**item, "relocated_bookmarks": invalid}])
    for operation in ("replace", "delete", "counter"):
        candidate = {**item, "operation": operation, "relocated_bookmarks": correct}
        if operation == "counter":
            candidate["revision_ids"] = ["7", "8"]
        else:
            candidate["target_revision_id"] = candidate["target_author"] = None
            if operation == "delete":
                candidate["new_text"] = ""
            else:
                candidate["revision_ids"] = ["7", "8"]
        assert not validator.is_valid([candidate])


@pytest.mark.anyio
async def test_mcp_goback_roundtrip_preserves_author_revision_and_bookmark_identity(tmp_path, demo_dir, monkeypatch):
    path, _ = _fixture(tmp_path, demo_dir)
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        parts = {info.filename: archive.read(info.filename) for info in infos}
    document = etree.fromstring(parts["word/document.xml"])
    paragraph = document.find(f'{w("body")}/{w("p")}')
    for child in list(paragraph):
        if child.tag != w("pPr"):
            paragraph.remove(child)
    before = etree.SubElement(paragraph, w("r"))
    etree.SubElement(before, w("t")).text = "Before "
    insertion = etree.SubElement(paragraph, w("ins"))
    insertion.set(w("id"), "9001")
    insertion.set(w("author"), "Shilov, Ilya")
    insertion.set(w("date"), "2026-10-16T10:00:00Z")
    first = etree.SubElement(insertion, w("r"))
    etree.SubElement(first, w("t")).text = "R-Pharm "
    marker = etree.SubElement(insertion, w("bookmarkStart"))
    marker.set(w("id"), "3")
    marker.set(w("name"), "_GoBack")
    second = etree.SubElement(insertion, w("r"))
    etree.SubElement(second, w("t")).text = "Company"
    end = etree.SubElement(paragraph, w("bookmarkEnd"))
    end.set(w("id"), "3")
    after = etree.SubElement(paragraph, w("r"))
    etree.SubElement(after, w("t")).text = " after"
    revision_attrs, marker_attrs, end_attrs = dict(insertion.attrib), dict(marker.attrib), dict(end.attrib)
    parts["word/document.xml"] = etree.tostring(document, xml_declaration=True, encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(path, "w") as archive:
        for info in infos:
            archive.writestr(info, parts[info.filename])
    original = path.read_bytes()
    snapshot = inspect_document(str(path), "browse", max_items=1)
    arguments = {"source_path": str(path), "file_sha256": snapshot["file_sha256"],
                 "operations": [{"kind": "replace_text", "paragraph_ref": snapshot["paragraphs"][0]["paragraph_ref"],
                                 "old_text": "R-Pharm Company", "new_text": "R-Pharm Legal"}]}
    monkeypatch.setenv("VEQTOR_TRACKED_CHANGE_AUTHOR", "Shilov, Ilya")
    monkeypatch.setenv("VEQTOR_COMMENT_INITIALS", "IS")
    server._tracked_change_author.cache_clear()
    server._comment_initials.cache_clear()
    try:
        async with Client(server.mcp) as client:
            response = await client.call_tool("preflight_document_edits", arguments)
            assert not response.is_error, _error_text(response)
            preflight = _payload(response)
            assert preflight["batch_applicable"], preflight
            expected_relocation = [{"name": "_GoBack", "id": "3", "position": "replacement_start"}]
            assert preflight["operations"][0]["relocated_bookmarks"] == expected_relocation
            assert path.read_bytes() == original
            response = await client.call_tool("apply_document_edits", {**arguments, "preflight_proof": preflight["preflight_proof"]})
            assert not response.is_error, _error_text(response)
            saved = _payload(response)
            assert saved["operations"][0]["relocated_bookmarks"] == expected_relocation
            assert saved["tracked_change_author"] == "Shilov, Ilya"
            with open(saved["backup_path"], "rb") as backup:
                assert backup.read() == original
            response = await client.call_tool("inspect_document", {"path": str(path), "mode": "browse", "max_items": 1})
            assert not response.is_error, _error_text(response)
            inspected = _payload(response)
            assert inspected["paragraphs"][0]["text"] == "Before R-Pharm Legal after"
            assert inspected["file_sha256"] == saved["output_sha256"]
        with zipfile.ZipFile(path) as archive:
            result_doc = etree.fromstring(archive.read("word/document.xml"))
        result_paragraph = result_doc.find(f'{w("body")}/{w("p")}')
        result_insertion = result_paragraph.find(w("ins"))
        starts, ends = list(result_paragraph.iter(w("bookmarkStart"))), list(result_paragraph.iter(w("bookmarkEnd")))
        assert dict(result_insertion.attrib) == revision_attrs
        assert len(starts) == len(ends) == 1
        assert dict(starts[0].attrib) == marker_attrs
        assert dict(ends[0].attrib) == end_attrs
        assert result_insertion[0] is starts[0]
        assert result_paragraph.index(ends[0]) > result_paragraph.index(result_insertion)
    finally:
        server._tracked_change_author.cache_clear()
        server._comment_initials.cache_clear()


@pytest.mark.anyio
async def test_unsupported_permissions_transport_guidance_omits_private_details(tmp_path, monkeypatch):
    def refused(*args, **kwargs):
        raise ApplyError("unsupported_permissions", "PRIVATE_ACL_DETAIL " + str(tmp_path))

    monkeypatch.setattr(server.drafting, "apply_document_edits", refused)
    arguments = {"source_path": str(tmp_path / "PRIVATE_DOCUMENT.docx"), "file_sha256": "a" * 64,
                 "operations": [{"kind": "delete_comment", "comment_id": "0"}], "preflight_proof": {}}
    async with Client(server.mcp) as session:
        result = await session.call_tool("apply_document_edits", arguments)
    assert result.is_error
    rendered = _error_text(result)
    assert "unsupported_permissions" in rendered
    assert "keep the original permissions and use a supported working location" in rendered
    assert "PRIVATE_ACL_DETAIL" not in rendered
    assert "PRIVATE_DOCUMENT" not in rendered
    assert str(tmp_path) not in rendered
    assert list(tmp_path.iterdir()) == []
