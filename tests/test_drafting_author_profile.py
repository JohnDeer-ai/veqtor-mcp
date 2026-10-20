# SPDX-License-Identifier: Apache-2.0
"""Request-local author selection through the real MCP transport."""

import asyncio
import copy
import hashlib
import json
import zipfile
from pathlib import Path

import jsonschema
import pytest
from lxml import etree
from mcp.client import Client

from veqtor_docx import inspect_document
from veqtor_docx._ooxml import w
from veqtor_mcp import server


DEFAULT_NAME = "Configured Reviewer"
DEFAULT_INITIALS = "CR"
SELECTED = {"name": "Shilov, Ilya", "initials": "IS"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def configured_author(monkeypatch):
    monkeypatch.setenv("VEQTOR_TRACKED_CHANGE_AUTHOR", DEFAULT_NAME)
    monkeypatch.setenv("VEQTOR_COMMENT_INITIALS", DEFAULT_INITIALS)
    server._tracked_change_author.cache_clear()
    server._comment_initials.cache_clear()
    yield
    server._tracked_change_author.cache_clear()
    server._comment_initials.cache_clear()


def _fixture(tmp_path, demo_dir, name="working.docx"):
    path = tmp_path / name
    path.write_bytes((demo_dir / "round-1-outgoing-draft.docx").read_bytes())
    inspected = inspect_document(str(path), "browse", max_items=1)
    reference = inspected["paragraphs"][0]["paragraph_ref"]
    return path, {
        "source_path": str(path),
        "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "operations": [
            {"kind": "replace_text", "paragraph_ref": reference,
             "old_text": "Supply", "new_text": "Services"},
            {"kind": "add_comment", "paragraph_ref": reference,
             "quote": "Master", "text": "Please confirm the scope."},
        ],
    }


def _text(result):
    return "\n".join(item.text for item in result.content if hasattr(item, "text"))


def _payload(result):
    assert not result.is_error, _text(result)
    value = result.structured_content or json.loads(_text(result))
    return value.get("result", value)


async def _save(client, arguments):
    dry = _payload(await client.call_tool("preflight_document_edits", arguments))
    assert dry["batch_applicable"], dry
    saved = _payload(await client.call_tool(
        "apply_document_edits", {**arguments, "preflight_proof": dry["preflight_proof"]}
    ))
    return dry, saved


def _assert_saved_author(path, name, initials):
    with zipfile.ZipFile(path) as archive:
        document = etree.fromstring(archive.read("word/document.xml"))
        comments = etree.fromstring(archive.read("word/comments.xml"))
    revisions = [node for node in document.iter()
                 if node.tag in {w("ins"), w("del")}]
    assert revisions
    assert {node.get(w("author")) for node in revisions} == {name}
    added = [node for node in comments if "".join(node.itertext()) == "Please confirm the scope."]
    assert len(added) == 1
    assert added[0].get(w("author")) == name
    assert added[0].get(w("initials")) == initials


@pytest.mark.anyio
async def test_explicit_profile_controls_saved_revisions_comments_and_proof(tmp_path, demo_dir):
    path, arguments = _fixture(tmp_path, demo_dir)
    original = path.read_bytes()
    async with Client(server.mcp) as client:
        dry, saved = await _save(client, {**arguments, "author_profile": SELECTED})
    for result in (dry, saved):
        assert result["tracked_change_author"] == SELECTED["name"]
        assert result["comment_initials"] == SELECTED["initials"]
    assert dry["preflight_proof"]["author"] == SELECTED["name"]
    assert dry["preflight_proof"]["initials"] == SELECTED["initials"]
    assert saved["save_mode"] == "in_place"
    assert saved["output_path"] == str(path)
    assert Path(saved["backup_path"]).read_bytes() == original
    _assert_saved_author(path, SELECTED["name"], SELECTED["initials"])
    assert server._tracked_change_author() == DEFAULT_NAME
    assert server._comment_initials() == DEFAULT_INITIALS


@pytest.mark.anyio
@pytest.mark.parametrize("profile_argument", [{}, {"author_profile": None}])
async def test_omitted_or_null_profile_keeps_configured_defaults(tmp_path, demo_dir, profile_argument):
    path, arguments = _fixture(tmp_path, demo_dir)
    async with Client(server.mcp) as client:
        dry, saved = await _save(client, {**arguments, **profile_argument})
    assert dry["tracked_change_author"] == saved["tracked_change_author"] == DEFAULT_NAME
    assert dry["comment_initials"] == saved["comment_initials"] == DEFAULT_INITIALS
    _assert_saved_author(path, DEFAULT_NAME, DEFAULT_INITIALS)


@pytest.mark.anyio
async def test_explicit_profile_works_with_invalid_environment_configuration(tmp_path, demo_dir, monkeypatch):
    monkeypatch.setenv("VEQTOR_TRACKED_CHANGE_AUTHOR", "")
    monkeypatch.setenv("VEQTOR_COMMENT_INITIALS", "\x01")
    server._tracked_change_author.cache_clear()
    server._comment_initials.cache_clear()
    path, arguments = _fixture(tmp_path, demo_dir)
    async with Client(server.mcp) as client:
        _, saved = await _save(client, {**arguments, "author_profile": SELECTED})
    assert saved["tracked_change_author"] == SELECTED["name"]
    _assert_saved_author(path, SELECTED["name"], SELECTED["initials"])
    assert server._tracked_change_author.cache_info().currsize == 0
    assert server._comment_initials.cache_info().currsize == 0


@pytest.mark.anyio
@pytest.mark.parametrize("tool_name", ["preflight_document_edits", "apply_document_edits"])
@pytest.mark.parametrize("profile", [
    {},
    {"name": "PRIVATE_PROFILE_SENTINEL"},
    {"initials": "IS"},
    {**SELECTED, "PRIVATE_UNKNOWN_FIELD": "PRIVATE_PROFILE_SENTINEL"},
    {"name": "", "initials": "IS"},
    {"name": " ", "initials": "IS"},
    {"name": 17, "initials": "IS"},
    {"name": True, "initials": "IS"},
    {"name": "X" * 256, "initials": "IS"},
    {"name": "PRIVATE_PROFILE_SENTINEL\x01", "initials": "IS"},
    {"name": SELECTED["name"], "initials": ""},
    {"name": SELECTED["name"], "initials": " "},
    {"name": SELECTED["name"], "initials": 17},
    {"name": SELECTED["name"], "initials": True},
    {"name": SELECTED["name"], "initials": "X" * 33},
    {"name": SELECTED["name"], "initials": "\x01"},
    "PRIVATE_PROFILE_SENTINEL",
    ["PRIVATE_PROFILE_SENTINEL"],
])
async def test_malformed_profiles_refuse_without_writes_or_private_echo(
    tmp_path, demo_dir, tool_name, profile
):
    path, arguments = _fixture(tmp_path, demo_dir)
    original = path.read_bytes()
    async with Client(server.mcp) as client:
        if tool_name == "apply_document_edits":
            dry = _payload(await client.call_tool("preflight_document_edits", arguments))
            assert dry["batch_applicable"], dry
            arguments["preflight_proof"] = dry["preflight_proof"]
        result = await client.call_tool(tool_name, {**arguments, "author_profile": profile})
    assert result.is_error
    message = _text(result)
    assert any(code in message for code in ("invalid_author", "invalid_initials", "invalid_arguments"))
    assert "PRIVATE_PROFILE_SENTINEL" not in message
    assert "PRIVATE_UNKNOWN_FIELD" not in message
    assert str(path) not in message
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.anyio
@pytest.mark.parametrize("drift", ["name", "initials", "omit", "proof_author", "proof_initials"])
async def test_profile_drift_refuses_before_backup(tmp_path, demo_dir, drift):
    path, arguments = _fixture(tmp_path, demo_dir)
    original = path.read_bytes()
    arguments["author_profile"] = dict(SELECTED)
    async with Client(server.mcp) as client:
        dry = _payload(await client.call_tool("preflight_document_edits", arguments))
        assert dry["batch_applicable"], dry
        changed = copy.deepcopy(arguments)
        changed["preflight_proof"] = copy.deepcopy(dry["preflight_proof"])
        if drift in {"name", "initials"}:
            changed["author_profile"][drift] += "X"
        elif drift == "omit":
            del changed["author_profile"]
        elif drift == "proof_author":
            changed["preflight_proof"]["author"] += "X"
        else:
            changed["preflight_proof"]["initials"] += "X"
        result = await client.call_tool("apply_document_edits", changed)
    assert result.is_error
    assert "preflight_binding_mismatch" in _text(result)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.anyio
async def test_parallel_clients_keep_profiles_request_local_and_default_unchanged(tmp_path, demo_dir):
    # Saves intentionally serialize per parent directory. Independent working
    # folders exercise request identity without competing for that save lock.
    folders = [tmp_path / name for name in ("selected", "another", "default")]
    for folder in folders:
        folder.mkdir()
    selected_path, selected_args = _fixture(folders[0], demo_dir)
    another_path, another_args = _fixture(folders[1], demo_dir)
    default_path, default_args = _fixture(folders[2], demo_dir)
    another = {"name": "Other Reviewer", "initials": "OR"}

    async def run(arguments):
        async with Client(server.mcp) as client:
            return await _save(client, arguments)

    results = await asyncio.gather(
        run({**selected_args, "author_profile": SELECTED}),
        run({**another_args, "author_profile": another}),
        run(default_args),
    )
    expected = [(SELECTED["name"], SELECTED["initials"]),
                (another["name"], another["initials"]),
                (DEFAULT_NAME, DEFAULT_INITIALS)]
    for (dry, saved), (name, initials) in zip(results, expected):
        assert dry["tracked_change_author"] == saved["tracked_change_author"] == name
        assert dry["comment_initials"] == saved["comment_initials"] == initials
        assert dry["preflight_proof"]["author"] == name
        assert dry["preflight_proof"]["initials"] == initials
    for path, (name, initials) in zip((selected_path, another_path, default_path), expected):
        _assert_saved_author(path, name, initials)
    assert server._tracked_change_author() == DEFAULT_NAME
    assert server._comment_initials() == DEFAULT_INITIALS


@pytest.mark.anyio
async def test_advertised_profile_schema_is_closed_and_optional():
    async with Client(server.mcp) as client:
        tools = (await client.list_tools()).tools
    for tool in tools:
        if tool.name not in {"preflight_document_edits", "apply_document_edits"}:
            continue
        schema = tool.input_schema
        assert "author_profile" not in schema.get("required", [])
        profile_schema = schema["properties"]["author_profile"]
        validator = jsonschema.Draft202012Validator(profile_schema)
        assert validator.is_valid(None)
        assert validator.is_valid(SELECTED)
        for invalid in ({}, {"name": "Reviewer"},
                        {**SELECTED, "extra": True},
                        {"name": 17, "initials": "IS"},
                        {"name": "X" * 256, "initials": "IS"},
                        {"name": SELECTED["name"], "initials": "X" * 33}):
            assert not validator.is_valid(invalid)
