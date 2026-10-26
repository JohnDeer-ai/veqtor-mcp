# SPDX-License-Identifier: Apache-2.0
"""Journal-enabled proof migration and actual MCP boundaries on fictional DOCX."""
import copy
import errno
import hashlib
import json

import pytest
from mcp.client import Client

from veqtor_docx import drafting, inspect_document, operation_journal
from veqtor_docx.apply import ApplyError
from veqtor_mcp import server

PROFILE = {"author": "Fictional Counsel", "initials": "FC", "producer_build": "test-build"}


def arguments(tmp_path, demo_dir):
    path = tmp_path / "draft.docx"
    path.write_bytes((demo_dir / "round-1-outgoing-draft.docx").read_bytes())
    para = inspect_document(str(path), "browse", max_items=1)["paragraphs"][0]
    return path, {"source_path": str(path), "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "operations": [{"kind": "replace_text", "paragraph_ref": para["paragraph_ref"],
                        "old_text": "Supply", "new_text": "Services"}]}


@pytest.mark.parametrize('private', ['.veqtor', '.veqtor-backups', '.VEQTOR', '.Veqtor-Backups'])
@pytest.mark.parametrize('target', ['source', 'output', 'aliased_source', 'aliased_output'])
def test_private_storage_refused_before_any_publication_or_journal(tmp_path, demo_dir, monkeypatch, private, target):
    from veqtor_docx import _drafting_io as io
    path, args = arguments(tmp_path, demo_dir)
    directory = tmp_path / private
    directory.mkdir()
    frozen = directory / 'frozen.docx'
    frozen.write_bytes(path.read_bytes())
    selected = directory
    if target.startswith('aliased'):
        selected = tmp_path / 'alias'
        selected.symlink_to(directory, target_is_directory=True)
    if target.endswith('source'):
        args['source_path'] = str(selected / frozen.name)
        args['output_path'] = str(tmp_path / 'output.docx')
    else:
        args['output_path'] = str(selected / 'output.docx')
    original = frozen.read_bytes()
    # The guard applies to both preflight and the lower-level publisher, even
    # without journals. No lock acquisition/recovery may modify private storage.
    dry = drafting.preflight_document_edits(**args, **PROFILE, journal=True)
    assert not dry['batch_applicable'] and dry['refusal_code'] == 'protected_storage_path'
    monkeypatch.setattr(io, '_writer_lock', lambda *a: pytest.fail('private storage lock acquired'))
    with pytest.raises(ApplyError, match='protected_storage_path'):
        io.publish_document(args['source_path'], args['output_path'], original, original)
    with pytest.raises(ApplyError, match='protected_storage_path'):
        with operation_journal._storage(str(frozen), '0' * 36, create=True):
            pytest.fail('private journal created')
    assert sorted(p.name for p in directory.iterdir()) == [frozen.name]
    assert frozen.read_bytes() == original
    assert not (tmp_path / 'output.docx').exists()


def test_v2_proof_id_bound_journal_and_candidate_bytes_unchanged(tmp_path, demo_dir):
    path, args = arguments(tmp_path, demo_dir)
    dry = drafting.preflight_document_edits(**args, **PROFILE, journal=True)
    proof = dry["preflight_proof"]
    assert dry["batch_applicable"] and proof["schema_version"] == "drafting_preflight.v2"
    assert not (tmp_path / ".veqtor").exists()
    with pytest.raises(ApplyError, match="journal_mode_mismatch"):
        drafting.apply_document_edits(**args, preflight_proof=proof, **PROFILE)
    changed = copy.deepcopy(proof)
    changed["operation_id"] = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(ApplyError, match="preflight_binding_mismatch"):
        drafting.apply_document_edits(**args, preflight_proof=changed, **PROFILE, journal=True)
    result = drafting.apply_document_edits(**args, preflight_proof=proof, **PROFILE, journal=True)
    assert result["output_sha256"] == proof["candidate_sha256"]
    assert result["operation_journal"]["operation_id"] == proof["operation_id"]
    folder = tmp_path / ".veqtor" / "operations" / proof["operation_id"]
    plan = json.loads((folder / "prepared.json").read_text())["intent"]["plan"]
    assert plan["operations"] == args["operations"]
    assert plan["preflight_proof"] == proof
    assert plan["observed_previews"] == dry["operations"]


def test_required_journal_refuses_legacy_v1_proof(tmp_path, demo_dir):
    path, args = arguments(tmp_path, demo_dir)
    proof = drafting.preflight_document_edits(**args, **PROFILE)["preflight_proof"]
    before = path.read_bytes()
    with pytest.raises(ApplyError, match="journal_preflight_required"):
        drafting.apply_document_edits(**args, preflight_proof=proof, **PROFILE, journal=True)
    assert path.read_bytes() == before


@pytest.mark.anyio
async def test_live_mcp_required_journal_success_and_recovery_contract(tmp_path, demo_dir, monkeypatch):
    path, args = arguments(tmp_path, demo_dir)
    monkeypatch.setenv("VEQTOR_OPERATION_JOURNAL", "required")
    async with Client(server.mcp) as client:
        schemas = {t.name: t.output_schema for t in (await client.list_tools()).tools}
        dry = await client.call_tool("preflight_document_edits", args)
        assert not dry.is_error, dry
        pre = dry.structured_content
        request = {**args, "preflight_proof": pre["preflight_proof"]}
        saved = await client.call_tool("apply_document_edits", request)
        assert not saved.is_error, saved
        result = saved.structured_content
        assert result["record_status"] == "written"
        assert result["record_id"] == request["preflight_proof"]["operation_id"]
        assert result["operation_journal"]["journal_status"] == "written"
        recovered = await client.call_tool("recover_document_operation", {
            "source_path": str(path), "operation_id": result["record_id"]})
        assert not recovered.is_error, recovered
        recovery = recovered.structured_content
        assert recovery["recovery_status"] == "existing_result"
        import jsonschema
        jsonschema.validate(result, schemas["apply_document_edits"])
        jsonschema.validate(recovery, schemas["recover_document_operation"])


@pytest.mark.anyio
async def test_mcp_postpublication_journal_failure_is_not_document_failure(tmp_path, demo_dir, monkeypatch):
    path, args = arguments(tmp_path, demo_dir)
    monkeypatch.setenv("VEQTOR_OPERATION_JOURNAL", "required")
    original = operation_journal._write

    def broken(fd, name, payload):
        if name == "result.json":
            raise OSError("PRIVATE_JOURNAL_ERROR_DETAIL")
        return original(fd, name, payload)

    async with Client(server.mcp) as client:
        dry = await client.call_tool("preflight_document_edits", args)
        proof = dry.structured_content["preflight_proof"]
        with monkeypatch.context() as patch:
            patch.setattr(operation_journal, "_write", broken)
            saved = await client.call_tool("apply_document_edits", {**args, "preflight_proof": proof})
        assert not saved.is_error, saved
        result = saved.structured_content
        assert result["record_status"] == "write_failed"
        assert result["operation_journal"]["publication_status"] == "published"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == proof["candidate_sha256"]
        assert "PRIVATE_JOURNAL_ERROR_DETAIL" not in str(result)
        recovered = await client.call_tool("recover_document_operation", {
            "source_path": str(path), "operation_id": proof["operation_id"]})
        assert not recovered.is_error and recovered.structured_content["recovered"]


@pytest.mark.anyio
@pytest.mark.parametrize("effect", [False, True])
async def test_mcp_exposes_unknown_syscall_outcome_and_recovers_without_apply(tmp_path, demo_dir, monkeypatch, effect):
    import jsonschema
    from veqtor_docx import _drafting_io as io
    path, args = arguments(tmp_path, demo_dir)
    monkeypatch.setenv("VEQTOR_OPERATION_JOURNAL", "required")
    original = io.os.replace

    def ambiguous(*a, **kw):
        if a[1] != path.name:
            return original(*a, **kw)
        if effect:
            original(*a, **kw)
        raise OSError(errno.EIO, "PRIVATE_AMBIGUOUS_PUBLICATION")

    async with Client(server.mcp) as client:
        schemas = {t.name: t.output_schema for t in (await client.list_tools()).tools}
        dry = await client.call_tool("preflight_document_edits", args)
        proof = dry.structured_content["preflight_proof"]
        with monkeypatch.context() as patch:
            patch.setattr(io.os, "replace", ambiguous)
            failed = await client.call_tool("apply_document_edits", {**args, "preflight_proof": proof})
        assert failed.is_error and failed.structured_content["error_code"] == "publication_uncertain"
        recovery = failed.structured_content["recovery"]
        assert recovery["publication_completed"] is False
        assert recovery["publication_status"] == recovery["operation_journal"]["publication_status"] == "uncertain"
        assert recovery["operation_journal"]["observed_output_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert "PRIVATE_AMBIGUOUS" not in str(failed)
        jsonschema.validate(failed.structured_content, schemas["apply_document_edits"])
        before = path.read_bytes()
        recovered = await client.call_tool("recover_document_operation", {
            "source_path": str(path), "operation_id": proof["operation_id"]})
        assert not recovered.is_error
        assert recovered.structured_content["recovered"] is effect
        assert recovered.structured_content["recovery_status"] == ("recovered" if effect else "unresolved")
        assert path.read_bytes() == before


def test_journal_does_not_change_legacy_candidate_bytes(tmp_path, demo_dir, monkeypatch):
    _, args = arguments(tmp_path, demo_dir)
    monkeypatch.setattr(drafting, "_new_timestamp", lambda: "2026-10-20T10:00:00+00:00")
    legacy = drafting.preflight_document_edits(**args, **PROFILE)
    current = drafting.preflight_document_edits(**args, **PROFILE, journal=True)
    assert legacy["preflight_proof"]["candidate_sha256"] == current["preflight_proof"]["candidate_sha256"]
    assert legacy["operations"] == current["operations"]


@pytest.mark.anyio
async def test_explicit_disabled_mode_retains_legacy_protocol(tmp_path, demo_dir, monkeypatch):
    _, args = arguments(tmp_path, demo_dir)
    monkeypatch.setenv("VEQTOR_OPERATION_JOURNAL", "disabled")
    async with Client(server.mcp) as client:
        pre = await client.call_tool("preflight_document_edits", args)
        proof = pre.structured_content["preflight_proof"]
        assert proof["schema_version"] == "drafting_preflight.v1"
        saved = await client.call_tool("apply_document_edits", {**args, "preflight_proof": proof})
        assert not saved.is_error and saved.structured_content["record_status"] == "disabled"
        assert "operation_journal" not in saved.structured_content
    assert not (tmp_path / ".veqtor").exists()


def test_doctor_refuses_unknown_journal_mode(monkeypatch, capsys):
    monkeypatch.setenv("VEQTOR_OPERATION_JOURNAL", "unrecognized-mode")
    monkeypatch.setattr(server.sys, "argv", ["veqtor-mcp", "doctor"])
    with pytest.raises(SystemExit) as result:
        server.main()
    assert result.value.code == 2
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["status"] == "error"
    assert doctor["configuration_error"]["code"] == "operation_journal_invalid"
