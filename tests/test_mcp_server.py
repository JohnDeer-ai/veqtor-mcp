# SPDX-License-Identifier: Apache-2.0
"""End-to-end smoke test of the MCP tool surface over an in-memory session."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import veqtor_docx
from veqtor_mcp import records
from veqtor_mcp import server
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
    return "\n".join(
        block.text for block in result.content if hasattr(block, "text")
    )


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
        for tool_name in ("preflight_edits", "apply_edits"):
            tool = next(tool for tool in tools.tools if tool.name == tool_name)
            assert "author" not in tool.inputSchema["properties"]
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
        assert export_payload["returned_count"] == len(export_payload["records"])
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
        assert preflight_payload["tracked_change_author"] == server._tracked_change_author()
        assert preflight_payload["producer"]["build"] == records.SOURCE_SNAPSHOT_IDENTITY
        assert not Path(out).exists()

        applied = await session.call_tool(
            "apply_edits",
            {
                "source_path": source,
                "output_path": out,
                "edits": edits,
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
async def test_invalid_xml_edit_text_is_recorded_as_preflight_refusal(
    demo_dir: Path,
) -> None:
    source = str(demo_dir / "round-2-counterparty-redline.docx")
    async with create_connected_server_and_client_session(
        mcp._mcp_server
    ) as session:
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
async def test_mcp_reinstate_rejects_present_insert_text_without_output(
    demo_dir: Path,
    tmp_path: Path,
    insert_text,
) -> None:
    from veqtor_docx.synthetic import CARVEOUT_DROPPED

    source = str(demo_dir / "round-4-counterparty-reply.docx")
    output = tmp_path / "never.docx"
    async with create_connected_server_and_client_session(
        mcp._mcp_server
    ) as session:
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
        applied = await session.call_tool(
            "apply_edits",
            {
                "source_path": source,
                "output_path": str(output),
                "edits": edits,
            },
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
        "position_supported",
        "refusal_code",
    }
    assert applied.isError
    assert "invalid_edit" in _error_text(applied)
    assert not output.exists()

    history = records.read_records(
        str(demo_dir), max_records=50, include_payload=True
    )["records"]
    preflight_record = next(
        record
        for record in reversed(history)
        if record["tool_name"] == "preflight_edits"
    )
    apply_record = next(
        record
        for record in reversed(history)
        if record["tool_name"] == "apply_edits"
    )
    assert preflight_record["result"]["refusal_code"] == "invalid_edit"
    assert apply_record["result"]["error_code"] == "invalid_edit"


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
    assert version.stdout.strip() == "veqtor-mcp 0.1.0"
    assert "Traceback" not in version.stderr
    assert doctor.returncode == 2
    diagnosis = json.loads(doctor.stdout)
    assert diagnosis["status"] == "error"
    assert diagnosis["tracked_change_author"] is None
    assert diagnosis["configuration_error"]["code"] == (
        "tracked_change_author_invalid"
    )
    assert "Traceback" not in doctor.stderr
    assert startup.returncode == 2
    assert "configuration error:" in startup.stderr
    assert "Traceback" not in startup.stderr


def test_cli_version_and_doctor(monkeypatch, capsys) -> None:
    monkeypatch.setattr(server.sys, "argv", ["veqtor-mcp", "--version"])
    server.main()
    assert capsys.readouterr().out.strip() == "veqtor-mcp 0.1.0"

    monkeypatch.setattr(server.sys, "argv", ["veqtor-mcp", "doctor"])
    server.main()
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["name"] == "veqtor-mcp"
    assert doctor["version"] == "0.1.0"
    assert doctor["build"] == records.SOURCE_SNAPSHOT_IDENTITY
    assert doctor["tracked_change_author"] == server._tracked_change_author()
    assert doctor["configuration_error"] is None
    assert doctor["status"] == "ok"


@pytest.mark.anyio
async def test_tool_errors_are_reported_not_raised(demo_dir: Path) -> None:
    async with create_connected_server_and_client_session(
        mcp._mcp_server
    ) as session:
        broken = await session.call_tool("list_rounds", {"folder": str(demo_dir / "nope")})
        assert broken.isError


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
    sentinel = f"PRIVATE_IMPLEMENTATION_SENTINEL_{tool_name}"
    original = getattr(core_owner, tool_name if core_owner is veqtor_docx else "read_records")

    def explode(*_args, **_kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(
        core_owner,
        tool_name if core_owner is veqtor_docx else "read_records",
        explode,
    )
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        result = await session.call_tool(tool_name, resolved_arguments)
    monkeypatch.setattr(
        core_owner,
        tool_name if core_owner is veqtor_docx else "read_records",
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
            {"source_path": "bad\x00path", "output_path": "never.docx", "edits": []},
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
    assert "decision-record operation refused" in error_text
    assert not any(reason in error_text for reason in reasons)
    assert raw_detail not in error_text
