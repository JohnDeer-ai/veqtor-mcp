# SPDX-License-Identifier: Apache-2.0
"""Request-local author selection through the real MCP transport."""

import asyncio
import copy
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

import jsonschema
import pytest
from lxml import etree
from mcp.client import Client
from mcp import StdioServerParameters

from veqtor_docx import inspect_document
from veqtor_docx._drafting_comments import add_comment
from veqtor_docx._ooxml import w
from veqtor_mcp import server


DEFAULT_NAME = "Configured Reviewer"
DEFAULT_INITIALS = "CR"
SELECTED = {"name": "Shilov, Ilya", "initials": "IS"}
ANOTHER = {"name": "Other Reviewer", "initials": "OR"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def configured_author(monkeypatch):
    monkeypatch.setenv("VEQTOR_TRACKED_CHANGE_AUTHOR", DEFAULT_NAME)
    monkeypatch.setenv("VEQTOR_COMMENT_INITIALS", DEFAULT_INITIALS)
    monkeypatch.setenv(server.OWN_AUTHOR_PROFILES_ENV, json.dumps([SELECTED, ANOTHER]))
    server._tracked_change_author.cache_clear()
    server._comment_initials.cache_clear()
    server._configured_author_profiles.cache_clear()
    server._initialize_drafting_configuration()
    yield
    server._tracked_change_author.cache_clear()
    server._comment_initials.cache_clear()
    server._configured_author_profiles.cache_clear()


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
async def test_explicit_profile_cannot_bypass_invalid_default_configuration(tmp_path, demo_dir, monkeypatch):
    monkeypatch.setenv("VEQTOR_TRACKED_CHANGE_AUTHOR", "")
    monkeypatch.setenv("VEQTOR_COMMENT_INITIALS", "\x01")
    server._tracked_change_author.cache_clear()
    server._comment_initials.cache_clear()
    server._configured_author_profiles.cache_clear()
    path, arguments = _fixture(tmp_path, demo_dir)
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "preflight_document_edits", {**arguments, "author_profile": SELECTED}
        )
    assert result.is_error and "invalid_author" in _text(result)
    assert list(tmp_path.iterdir()) == [path]
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
            assert dry["batch_applicable"], dry["refusal_code"]
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
@pytest.mark.parametrize("drift", [
    "name", "initials", "allowed_profile", "omit", "proof_author", "proof_initials",
])
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
        elif drift == "allowed_profile":
            changed["author_profile"] = dict(ANOTHER)
        elif drift == "omit":
            del changed["author_profile"]
        elif drift == "proof_author":
            changed["preflight_proof"]["author"] += "X"
        else:
            changed["preflight_proof"]["initials"] += "X"
        result = await client.call_tool("apply_document_edits", changed)
    assert result.is_error
    expected = "author_not_allowed" if drift in {"name", "initials"} else "preflight_binding_mismatch"
    assert expected in _text(result)
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
    another = dict(ANOTHER)

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
        paragraph = next(
            item for item in schema["properties"]["operations"]["items"]["oneOf"]
            if item["properties"]["kind"] == {"const": "replace_paragraph"}
        )
        assert "mode" not in paragraph["required"]
        mode = paragraph["properties"]["mode"]
        assert mode["default"] == "minimal"
        assert jsonschema.Draft202012Validator(mode).is_valid("minimal")
        assert jsonschema.Draft202012Validator(mode).is_valid("whole")
        for invalid in (None, True, "automatic"):
            assert not jsonschema.Draft202012Validator(mode).is_valid(invalid)


def test_unset_allowlist_means_exact_configured_default_only(monkeypatch):
    monkeypatch.delenv(server.OWN_AUTHOR_PROFILES_ENV)
    server._configured_author_profiles.cache_clear()
    assert server._configured_author_profiles() == ((DEFAULT_NAME, DEFAULT_INITIALS),)
    assert server._drafting_author_profile(None) == (DEFAULT_NAME, DEFAULT_INITIALS)
    assert server._drafting_author_profile({"name": DEFAULT_NAME, "initials": DEFAULT_INITIALS}) == (
        DEFAULT_NAME, DEFAULT_INITIALS,
    )
    for profile in (SELECTED, {"name": DEFAULT_NAME, "initials": "OTHER"}):
        with pytest.raises(server.veqtor_docx.ApplyError, match="author_not_allowed"):
            server._drafting_author_profile(profile)


@pytest.mark.parametrize("raw", [
    "", " ", "[]", "null", "{}", "false", "17", '"name"',
    '{"profiles":[]}', '[{}]', '[{"name":"Allowed"}]',
    '[{"name":"Allowed","initials":"AL","extra":true}]',
    '[{"name":"Allowed","name":"Counterparty","initials":"AL"}]',
    json.dumps([{"name": DEFAULT_NAME, "initials": DEFAULT_INITIALS}]),
    json.dumps([{"name": DEFAULT_NAME, "initials": "OTHER"}]),
    json.dumps([SELECTED, SELECTED]),
    json.dumps([SELECTED, {**SELECTED, "initials": "OTHER"}]),
    json.dumps([{**SELECTED, "name": " "}]),
    json.dumps([{**SELECTED, "name": " Allowed "}]),
    json.dumps([{**SELECTED, "initials": " AL "}]),
    json.dumps([{**SELECTED, "name": 17}]),
    json.dumps([{**SELECTED, "initials": True}]),
    json.dumps([{**SELECTED, "name": "X" * 256}]),
    json.dumps([{**SELECTED, "initials": "X" * 33}]),
    json.dumps([{**SELECTED, "name": "PRIVATE_BAD\x01"}]),
    json.dumps([{**SELECTED, "initials": "\udcff"}]),
    json.dumps([{"name": f"Own {i}", "initials": "O"} for i in range(16)]),
    "[" * 1200 + "]" * 1200,
    " " * 16_385,
])
def test_empty_malformed_or_conflicting_allowlist_fails_closed(monkeypatch, raw):
    monkeypatch.setenv(server.OWN_AUTHOR_PROFILES_ENV, raw)
    server._configured_author_profiles.cache_clear()
    with pytest.raises(server._AuthorProfilesConfigurationError) as error:
        server._configured_author_profiles()
    assert "PRIVATE_BAD" not in str(error.value)
    with pytest.raises(server.veqtor_docx.ApplyError, match="invalid_author"):
        server._drafting_author_profile(SELECTED)


def test_additional_profile_limit_is_inclusive_and_names_are_exact(monkeypatch):
    extras = [{"name": f"Own {i}", "initials": "O"} for i in range(15)]
    monkeypatch.setenv(server.OWN_AUTHOR_PROFILES_ENV, json.dumps(extras))
    server._configured_author_profiles.cache_clear()
    assert len(server._configured_author_profiles()) == 16
    assert server._drafting_author_profile(extras[-1]) == ("Own 14", "O")
    for name in ("own 14", "Own 14 ", "Own  14"):
        with pytest.raises(server.veqtor_docx.ApplyError, match="author_not_allowed"):
            server._drafting_author_profile({"name": name, "initials": "O"})


def test_process_profiles_are_frozen_after_configuration(monkeypatch):
    expected = server._initialize_drafting_configuration()
    instructions = server.mcp.instructions
    monkeypatch.setenv(server.TRACKED_CHANGE_AUTHOR_ENV, "Counterparty")
    monkeypatch.setenv(server.COMMENT_INITIALS_ENV, "CP")
    monkeypatch.setenv(server.OWN_AUTHOR_PROFILES_ENV, '[{"name":"Counterparty","initials":"CP"}]')
    assert server._initialize_drafting_configuration() == expected
    assert server.mcp.instructions == instructions
    with pytest.raises(server.veqtor_docx.ApplyError, match="author_not_allowed"):
        server._drafting_author_profile({"name": "Counterparty", "initials": "CP"})


@pytest.mark.anyio
async def test_initialization_advertises_exact_profiles_and_concrete_approval_protocol(monkeypatch):
    monkeypatch.setenv("UNRELATED_PRIVATE_CONFIG", "PRIVATE_CONFIG_SENTINEL")
    async with Client(server.mcp) as client:
        instructions = client.instructions
    assert instructions is not None
    advertised = json.loads(instructions.split("first is the default): ", 1)[1])
    assert advertised == [{"name": DEFAULT_NAME, "initials": DEFAULT_INITIALS}, SELECTED, ANOTHER]
    assert "PRIVATE_CONFIG_SENTINEL" not in instructions
    assert "already explicitly authorized" in instructions
    assert "do not ask for the same" in instructions
    assert 'mode="whole" only' in instructions
    assert "specific" in instructions and "do not infer" in instructions
    assert "Never modify the allowlist" in instructions


@pytest.mark.anyio
async def test_real_stdio_initialization_freezes_and_exposes_only_startup_profiles(
    tmp_path, demo_dir, monkeypatch,
):
    path, arguments = _fixture(tmp_path, demo_dir)
    original = path.read_bytes()
    env = dict(os.environ)
    env[server.OWN_AUTHOR_PROFILES_ENV] = json.dumps([SELECTED])
    transport = StdioServerParameters(
        command=sys.executable, args=["-m", "veqtor_mcp.server"], env=env,
    )
    async with Client(transport) as client:
        instructions = client.instructions
        assert instructions is not None
        choices = json.loads(instructions.split("first is the default): ", 1)[1])
        assert choices == [{"name": DEFAULT_NAME, "initials": DEFAULT_INITIALS}, SELECTED]
        assert "exact proposed edits" in instructions
        assert "already explicitly authorized" in instructions
        assert "specific" in instructions and "do not infer" in instructions
        monkeypatch.setenv(server.OWN_AUTHOR_PROFILES_ENV, json.dumps([SELECTED, ANOTHER]))
        refused = await client.call_tool("preflight_document_edits", {
            **arguments, "author_profile": ANOTHER,
        })
        assert refused.is_error and "author_not_allowed" in _text(refused)
        accepted = _payload(await client.call_tool("preflight_document_edits", {
            **arguments, "author_profile": SELECTED,
        }))
        assert accepted["batch_applicable"]
    assert path.read_bytes() == original and list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("arguments", [["doctor"], []])
def test_invalid_allowlist_refuses_doctor_and_server_startup(monkeypatch, capsys, arguments):
    monkeypatch.setenv(server.OWN_AUTHOR_PROFILES_ENV, "[]")
    server._configured_author_profiles.cache_clear()
    monkeypatch.setattr(server.sys, "argv", ["veqtor-mcp", *arguments])
    monkeypatch.setattr(server.mcp, "run", lambda: pytest.fail("invalid config reached transport"))
    with pytest.raises(SystemExit) as error:
        server.main()
    assert error.value.code == 2
    captured = capsys.readouterr()
    if arguments:
        payload = json.loads(captured.out)
        assert payload["configuration_error"]["code"] == "author_profiles_invalid"
        assert payload["drafting_author_profiles"] is None
    else:
        assert "configuration error:" in captured.err


def test_startup_freezes_profiles_and_doctor_lists_only_author_configuration(monkeypatch, capsys):
    monkeypatch.setattr(server.sys, "argv", ["veqtor-mcp", "doctor"])
    server.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["drafting_author_profiles"] == [
        {"name": DEFAULT_NAME, "initials": DEFAULT_INITIALS}, SELECTED, ANOTHER,
    ]
    assert payload["configuration_error"] is None
    called = []
    monkeypatch.setattr(server.sys, "argv", ["veqtor-mcp"])
    monkeypatch.setattr(server.mcp, "run", lambda: called.append(server.mcp.instructions))
    server.main()
    assert called == [server.mcp.instructions]
    assert json.dumps(SELECTED) in called[0]


@pytest.mark.anyio
@pytest.mark.parametrize("tool_name", ["preflight_document_edits", "apply_document_edits"])
@pytest.mark.parametrize("kind", ["replace_text", "update_comment", "delete_comment"])
async def test_foreign_author_spoof_refused_before_drafting_and_any_backup(
    tmp_path, demo_dir, monkeypatch, tool_name, kind,
):
    path, arguments = _fixture(tmp_path, demo_dir)
    # Seed foreign-authored content directly; never authorize that profile.
    with zipfile.ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    document = etree.fromstring(parts["word/document.xml"])
    paragraph = document.find(f'{w("body")}/{w("p")}')
    insertion = etree.SubElement(paragraph, w("ins"), {
        w("id"): "900", w("author"): "Counterparty Reviewer",
    })
    run = etree.SubElement(insertion, w("r"))
    etree.SubElement(run, w("t")).text = "Foreign proposal"
    foreign_comment = add_comment(
        parts, document, paragraph, "Master", "Foreign comment",
        "Counterparty Reviewer", "CR",
    )
    parts["word/document.xml"] = etree.tostring(document)
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    original = path.read_bytes()
    reference = inspect_document(str(path), "browse", max_items=1)["paragraphs"][0]["paragraph_ref"]
    arguments["file_sha256"] = hashlib.sha256(original).hexdigest()
    safe = {"kind": "replace_text", "paragraph_ref": reference, "old_text": "Supply", "new_text": "Services"}
    async with Client(server.mcp) as client:
        if tool_name == "apply_document_edits":
            dry = _payload(await client.call_tool("preflight_document_edits", {**arguments, "operations": [safe]}))
            assert dry["batch_applicable"], dry["refusal_code"]
            arguments["preflight_proof"] = dry["preflight_proof"]
        operation = ({"kind": kind, "paragraph_ref": reference, "old_text": "Foreign proposal", "new_text": "Overwritten"}
                     if kind == "replace_text" else {"kind": kind, "comment_id": foreign_comment["id"]})
        if kind == "update_comment":
            operation["text"] = "Overwritten"
        monkeypatch.setattr(server.drafting, tool_name, lambda *a, **k: pytest.fail("unconfigured author reached drafting"))
        result = await client.call_tool(tool_name, {**arguments, "operations": [operation],
            "author_profile": {"name": "Counterparty Reviewer", "initials": "CR"}})
    assert result.is_error and "author_not_allowed" in _text(result)
    assert "Counterparty Reviewer" not in _text(result)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.anyio
async def test_allowed_current_profile_counteredit_preserves_foreign_author_and_marks_change(
    tmp_path, demo_dir,
):
    path, arguments = _fixture(tmp_path, demo_dir)
    with zipfile.ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    document = etree.fromstring(parts["word/document.xml"])
    paragraph = document.find(f'{w("body")}/{w("p")}')
    foreign = etree.SubElement(paragraph, w("ins"), {
        w("id"): "900", w("author"): "Counterparty Reviewer",
    })
    run = etree.SubElement(foreign, w("r"))
    etree.SubElement(run, w("t")).text = "Foreign proposal"
    parts["word/document.xml"] = etree.tostring(document)
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    original = path.read_bytes()
    reference = inspect_document(str(path), "browse", max_items=1)["paragraphs"][0]["paragraph_ref"]
    arguments.update(file_sha256=hashlib.sha256(original).hexdigest(), operations=[{
        "kind": "replace_text", "paragraph_ref": reference,
        "old_text": "Foreign proposal", "new_text": "Alternative proposal",
    }])
    async with Client(server.mcp) as client:
        dry, saved = await _save(client, arguments)
    assert dry["operations"][0]["operation"] == saved["operations"][0]["operation"] == "counter"
    assert saved["tracked_change_author"] == DEFAULT_NAME
    assert Path(saved["backup_path"]).read_bytes() == original
    with zipfile.ZipFile(path) as archive:
        document = etree.fromstring(archive.read("word/document.xml"))
    preserved = next(node for node in document.iter(w("ins")) if node.get(w("id")) == "900")
    assert preserved.get(w("author")) == "Counterparty Reviewer"
    deletions = [node for node in preserved.iter(w("del"))]
    assert len(deletions) == 1 and deletions[0].get(w("author")) == DEFAULT_NAME
    assert "".join(deletions[0].itertext()) == "Foreign proposal"
    added = [node for node in document.iter(w("ins")) if node.get(w("author")) == DEFAULT_NAME]
    assert len(added) == 1 and "".join(added[0].itertext()) == "Alternative proposal"
