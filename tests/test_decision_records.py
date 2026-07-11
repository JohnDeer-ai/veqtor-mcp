# SPDX-License-Identifier: Apache-2.0
"""M3 decision-record sidecar behavior at the MCP layer."""

from __future__ import annotations

import copy
import errno
import hashlib
import importlib
import inspect
import json
import os
import stat
import subprocess
import zipfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

import veqtor_mcp
import veqtor_docx
from veqtor_docx import contracts as docx_contracts
from veqtor_docx import generate_demo_rounds
from veqtor_mcp import records
from veqtor_mcp import server


def _matter(tmp_path: Path) -> Path:
    root = tmp_path / "matter"
    generate_demo_rounds(root)
    return root


def _cap_from_tool(path: Path) -> tuple[dict, dict]:
    extracted = server.extract_redlines(str(path))
    cap = next(
        unit
        for unit in extracted["change_units"]
        if unit["clause_anchor"] and unit["clause_anchor"]["label"] == "14.2"
    )
    return extracted, {
        "change_unit_id": cap["change_unit_id"],
        "file_sha256": extracted["file_sha256"],
    }


def _write_concurrent_record(workspace: str, index: int) -> dict:
    return records.write_record(
        workspace=Path(workspace),
        tool_name="list_rounds",
        input_payload={"index": index},
        result={"status": "ok", "index": index},
        provenance={"index": index},
    )


def test_with_record_preserves_explicit_empty_record_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def capture_write(**kwargs):
        captured.update(kwargs)
        return {"record_id": "dr_001", "record_status": "written"}

    monkeypatch.setattr(records, "write_record", capture_write)

    result = server._with_record(
        tool_name="list_rounds",
        workspace=tmp_path,
        input_payload={},
        result={"status": "ok", "value": 1},
        record_result={},
        provenance={},
    )

    assert captured["result"] == {}
    assert result["record_status"] == "written"


def test_successful_tool_calls_write_decision_records(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    source = matter / "round-2-counterparty-redline.docx"
    extracted, anchor = _cap_from_tool(source)

    verified = server.verify_quote(
        str(source),
        anchor,
        "the total fees paid by Client under this Agreement",
    )
    out = tmp_path / "counter.docx"
    applied = server.apply_edits(
        str(source),
        str(out),
        [
            {
                "anchor": anchor,
                "delete_text": " in respect of all claims in aggregate.",
                "insert_text": " per claim.",
            }
        ],
    )

    assert extracted["record_status"] == "written"
    assert verified["record_status"] == "written"
    assert applied["record_status"] == "written"
    assert applied["output_sha256"] == veqtor_docx.extract_redlines(str(out))["file_sha256"]

    exported = server.export_decision_record(str(matter), max_records=2, include_payload=True)
    assert exported["record_status"] == "written"
    assert exported["payloads"] == "full"
    assert exported["total_count"] == 3
    assert exported["access_count"] == 0
    assert exported["truncated"] is True
    assert exported["next_before_record_id"] == "dr_002"
    assert [record["record_id"] for record in exported["records"]] == ["dr_002", "dr_003"]

    verify_record, apply_record = exported["records"]
    assert verify_record["tool_name"] == "verify_quote"
    assert verify_record["input"]["quote"] == "the total fees paid by Client under this Agreement"
    assert verify_record["result"]["verdict"] == "exact"
    assert verify_record["provenance"]["anchors"][0]["side"] == "old"

    assert apply_record["tool_name"] == "apply_edits"
    assert apply_record["provenance"]["source_sha256"] == anchor["file_sha256"]
    assert apply_record["provenance"]["output_sha256"] == applied["output_sha256"]
    assert apply_record["provenance"]["round_trip_check"]["status"] == "passed"


def test_extract_record_is_compact_and_does_not_duplicate_change_text(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    source = matter / "round-2-counterparty-redline.docx"

    extracted, _ = _cap_from_tool(source)
    journal = records.read_records(str(matter), max_records=10)
    extract_record = next(
        record for record in journal["records"] if record["tool_name"] == "extract_redlines"
    )

    assert extract_record["record_type"] == "tool_observation.v1"
    assert extract_record["result"]["change_unit_count"] == len(extracted["change_units"])
    assert "change_units" not in extract_record["result"]
    assert extract_record["result_sha256"] == records._stable_digest(extract_record["result"])
    full_extract = veqtor_docx.extract_redlines(str(source))
    assert extract_record["tool_result_sha256"] == records._stable_digest(
        {"status": "ok", **full_extract}
    )
    assert extract_record["tool_result_sha256"] != extract_record["result_sha256"]
    assert extract_record["producer"]["build"]
    serialized = json.dumps(extract_record, ensure_ascii=False)
    text = next(
        candidate
        for unit in extracted["change_units"]
        for candidate in (unit["new_text"], unit["old_text"])
        if candidate and len(candidate) > 20
    )
    assert text not in serialized


def test_build_identity_ignores_asserted_environment_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VEQTOR_BUILD_COMMIT", "abc123")

    assert records.SOURCE_SNAPSHOT_IDENTITY.startswith(records.SOURCE_SNAPSHOT_PREFIX)
    assert "abc123" not in records.SOURCE_SNAPSHOT_IDENTITY


def test_source_snapshot_hashes_package_source_tree(
    tmp_path: Path,
) -> None:
    docx_root = tmp_path / "loaded" / "veqtor_docx"
    mcp_root = tmp_path / "loaded" / "veqtor_mcp"
    docx_root.mkdir(parents=True)
    mcp_root.mkdir(parents=True)
    (docx_root / "engine.py").write_text("ENGINE = 1\n", encoding="utf-8")
    (mcp_root / "server.py").write_text("SERVER = 1\n", encoding="utf-8")

    roots = [("veqtor_docx", docx_root), ("veqtor_mcp", mcp_root)]
    first = records._source_snapshot_identity(roots)
    reordered = records._source_snapshot_identity(list(reversed(roots)))

    (mcp_root / "server.py").write_text("SERVER = 2\n", encoding="utf-8")
    second = records._source_snapshot_identity(roots)

    assert first.startswith(records.SOURCE_SNAPSHOT_PREFIX)
    assert reordered == first
    assert second.startswith(records.SOURCE_SNAPSHOT_PREFIX)
    assert first != second


def test_source_snapshot_read_failure_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "loaded" / "veqtor_mcp"
    package_root.mkdir(parents=True)
    source = package_root / "server.py"
    source.write_text("SERVER = 1\n", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def fail_source_read(path: Path) -> bytes:
        if path == source:
            raise OSError("simulated unreadable source")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_source_read)

    assert (
        records._source_snapshot_identity([("veqtor_mcp", package_root)])
        == "source-snapshot-unavailable"
    )


def test_source_snapshot_missing_root_is_explicit(tmp_path: Path) -> None:
    package_root = tmp_path / "loaded" / "veqtor_mcp"
    package_root.mkdir(parents=True)
    (package_root / "server.py").write_text("SERVER = 1\n", encoding="utf-8")

    assert (
        records._source_snapshot_identity(
            [
                ("veqtor_docx", tmp_path / "missing" / "veqtor_docx"),
                ("veqtor_mcp", package_root),
            ]
        )
        == "source-snapshot-unavailable"
    )


def test_source_snapshot_non_directory_root_is_explicit(tmp_path: Path) -> None:
    package_root = tmp_path / "veqtor_mcp.py"
    package_root.write_text("SERVER = 1\n", encoding="utf-8")

    assert (
        records._source_snapshot_identity([("veqtor_mcp", package_root)])
        == "source-snapshot-unavailable"
    )


def test_source_snapshot_enumeration_failure_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "loaded" / "veqtor_mcp"
    package_root.mkdir(parents=True)

    def fail_walk(root, *, topdown, onerror, followlinks):
        onerror(OSError("simulated enumeration failure"))
        yield from ()

    monkeypatch.setattr(records.os, "walk", fail_walk)

    assert (
        records._source_snapshot_identity([("veqtor_mcp", package_root)])
        == "source-snapshot-unavailable"
    )


def test_source_snapshot_rejects_symlinked_source_directory(tmp_path: Path) -> None:
    package_root = tmp_path / "loaded" / "veqtor_mcp"
    external = tmp_path / "external"
    package_root.mkdir(parents=True)
    external.mkdir()
    (external / "hidden.py").write_text("HIDDEN = 1\n", encoding="utf-8")
    os.symlink(external, package_root / "linked")

    assert (
        records._source_snapshot_identity([("veqtor_mcp", package_root)])
        == "source-snapshot-unavailable"
    )


def test_source_snapshot_discovery_rejects_symlinked_package_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp_target = tmp_path / "mcp-target"
    docx_target = tmp_path / "docx-target"
    mcp_target.mkdir()
    docx_target.mkdir()
    (mcp_target / "__init__.py").write_text("MCP = 1\n", encoding="utf-8")
    (docx_target / "__init__.py").write_text("DOCX = 1\n", encoding="utf-8")
    mcp_link = tmp_path / "mcp-link"
    docx_link = tmp_path / "docx-link"
    os.symlink(mcp_target, mcp_link)
    os.symlink(docx_target, docx_link)
    monkeypatch.setattr(veqtor_mcp, "__path__", [str(mcp_link)])
    monkeypatch.setattr(veqtor_docx, "__path__", [str(docx_link)])

    discovered = records._loaded_package_roots()

    assert all(root.is_symlink() for _, root in discovered)
    assert records._source_snapshot_identity() == "source-snapshot-unavailable"


def test_source_snapshot_manifest_has_unambiguous_file_framing(tmp_path: Path) -> None:
    tree_a = tmp_path / "tree-a"
    tree_b = tmp_path / "tree-b"
    tree_a.mkdir()
    tree_b.mkdir()
    (tree_a / "a.py").write_bytes(b"X\0pkg/b.py\0Y")
    (tree_b / "a.py").write_bytes(b"X")
    (tree_b / "b.py").write_bytes(b"Y")

    digest_a = records._source_snapshot_identity([("pkg", tree_a)])
    digest_b = records._source_snapshot_identity([("pkg", tree_b)])

    assert digest_a.startswith(records.SOURCE_SNAPSHOT_PREFIX)
    assert digest_b.startswith(records.SOURCE_SNAPSHOT_PREFIX)
    assert digest_a != digest_b


def test_source_snapshot_serialization_failure_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "loaded" / "veqtor_mcp"
    package_root.mkdir(parents=True)
    surrogate_path = package_root / "bad_\udcff.py"
    monkeypatch.setattr(
        records,
        "_strict_python_sources",
        lambda root: iter([surrogate_path]),
    )
    monkeypatch.setattr(Path, "read_bytes", lambda path: b"SOURCE = 1\n")

    assert (
        records._source_snapshot_identity([("veqtor_mcp", package_root)])
        == records.SOURCE_SNAPSHOT_UNAVAILABLE
    )


def test_kill_switch_disables_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    matter = _matter(tmp_path)
    monkeypatch.setenv(records.DISABLE_ENV, "1")

    result = server.list_rounds(str(matter))

    assert result["record_id"] is None
    assert result["record_status"] == "disabled"
    assert not (matter / records.SIDECAR_DIR).exists()


def test_journal_write_failure_is_visible_without_failing_tool(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    (sidecar / records.JOURNAL_NAME).write_text("{not json\n", encoding="utf-8")

    result = server.list_rounds(str(matter))

    assert len(result["rounds"]) == 4
    assert result["record_id"] is None
    assert result["record_status"] == "write_failed"
    assert "journal_corrupt" in result["record_error"]


def test_sidecar_is_private_and_ignored_in_external_git(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=matter, check=True)

    server.list_rounds(str(matter))

    sidecar = matter / records.SIDECAR_DIR
    journal = sidecar / records.JOURNAL_NAME
    ignore = sidecar / records.GITIGNORE_NAME
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert stat.S_IMODE(ignore.stat().st_mode) == 0o600
    assert ignore.read_text(encoding="utf-8") == "*\n"
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".veqtor/decision-records.jsonl"],
        cwd=matter,
        check=False,
    )
    assert ignored.returncode == 0


def test_gitignore_is_not_rewritten_after_sidecar_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    assert server.list_rounds(str(matter))["record_status"] == "written"

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("gitignore rewrite reached the append hot path")

    real_fsync = records.os.fsync
    directory_fsyncs = 0

    def count_directory_fsyncs(fd: int) -> None:
        nonlocal directory_fsyncs
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_fsyncs += 1
        real_fsync(fd)

    monkeypatch.setattr(records, "_write_sidecar_gitignore", fail_if_called)
    monkeypatch.setattr(records.os, "fsync", count_directory_fsyncs)

    assert server.list_rounds(str(matter))["record_status"] == "written"
    assert directory_fsyncs == 0


def test_gitignore_rename_failure_cleans_unique_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    real_replace = records.os.replace

    def fail_replace(*_args, **_kwargs):
        raise OSError(errno.EIO, "simulated rename failure")

    monkeypatch.setattr(records.os, "replace", fail_replace)

    result = server.list_rounds(str(matter))

    assert result["record_status"] == "write_failed"
    sidecar = matter / records.SIDECAR_DIR
    assert not list(sidecar.glob(".*.tmp.*"))
    assert not (sidecar / records.GITIGNORE_NAME).exists()
    assert not (sidecar / records.JOURNAL_NAME).exists()

    monkeypatch.setattr(records.os, "replace", real_replace)
    recovered = server.list_rounds(str(matter))
    assert recovered["record_status"] == "written"
    assert (sidecar / records.GITIGNORE_NAME).read_text(encoding="utf-8") == "*\n"


def test_existing_sidecar_without_gitignore_is_repaired(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir(mode=0o700)

    result = server.list_rounds(str(matter))

    assert result["record_status"] == "written"
    ignore = sidecar / records.GITIGNORE_NAME
    assert ignore.read_text(encoding="utf-8") == "*\n"
    assert stat.S_IMODE(ignore.stat().st_mode) == 0o600
    assert (sidecar / records.JOURNAL_NAME).exists()


def test_zero_length_gitignore_write_prevents_journal_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    real_write = records.os.write

    monkeypatch.setattr(records.os, "write", lambda fd, payload: 0)
    failed = server.list_rounds(str(matter))

    sidecar = matter / records.SIDECAR_DIR
    assert failed["record_status"] == "write_failed"
    assert not (sidecar / records.GITIGNORE_NAME).exists()
    assert not (sidecar / records.JOURNAL_NAME).exists()

    monkeypatch.setattr(records.os, "write", real_write)
    recovered = server.list_rounds(str(matter))
    assert recovered["record_status"] == "written"
    assert (sidecar / records.GITIGNORE_NAME).read_bytes() == b"*\n"


def test_partial_gitignore_write_is_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    real_write = records.os.write
    partial = True

    def write_one_byte_first(fd: int, payload: bytes) -> int:
        nonlocal partial
        if partial:
            partial = False
            return real_write(fd, payload[:1])
        return real_write(fd, payload)

    monkeypatch.setattr(records.os, "write", write_one_byte_first)
    result = server.list_rounds(str(matter))

    sidecar = matter / records.SIDECAR_DIR
    assert result["record_status"] == "written"
    assert (sidecar / records.GITIGNORE_NAME).read_bytes() == b"*\n"
    assert (sidecar / records.JOURNAL_NAME).exists()


def test_interrupted_gitignore_write_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    real_write = records.os.write
    interrupted = True

    def interrupt_once(fd: int, payload: bytes) -> int:
        nonlocal interrupted
        if interrupted:
            interrupted = False
            raise InterruptedError
        return real_write(fd, payload)

    monkeypatch.setattr(records.os, "write", interrupt_once)
    result = server.list_rounds(str(matter))

    assert result["record_status"] == "written"
    assert (
        matter / records.SIDECAR_DIR / records.GITIGNORE_NAME
    ).read_bytes() == b"*\n"


def test_missing_workspace_failure_does_not_create_directories(tmp_path: Path) -> None:
    missing = tmp_path / "probe-nonexistent" / "deep" / "missing.docx"

    with pytest.raises(veqtor_docx.DocxError):
        server.extract_redlines(str(missing))

    assert not (tmp_path / "probe-nonexistent").exists()


def test_sidecar_symlink_is_refused_without_cross_matter_append(tmp_path: Path) -> None:
    matter_a = tmp_path / "matter-a"
    matter_b = tmp_path / "matter-b"
    generate_demo_rounds(matter_a)
    generate_demo_rounds(matter_b)
    server.list_rounds(str(matter_a))
    before = records.read_records(str(matter_a), max_records=10)["total_count"]
    os.symlink(matter_a / records.SIDECAR_DIR, matter_b / records.SIDECAR_DIR)

    result = server.list_rounds(str(matter_b))

    assert len(result["rounds"]) == 4
    assert result["record_status"] == "write_failed"
    assert "sidecar_symlink" in result["record_error"]
    after = records.read_records(str(matter_a), max_records=10)["total_count"]
    assert after == before
    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter_b), max_records=10)
    assert err.value.code == "sidecar_symlink"


def test_journal_symlink_is_refused(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir(mode=0o700)
    target = tmp_path / "outside.jsonl"
    target.write_text("", encoding="utf-8")
    os.symlink(target, sidecar / records.JOURNAL_NAME)

    result = server.list_rounds(str(matter))

    assert result["record_status"] == "write_failed"
    assert "journal_symlink" in result["record_error"]
    assert target.read_text(encoding="utf-8") == ""


def test_dangling_sidecar_symlink_is_refused_on_read(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    os.symlink(tmp_path / "missing-sidecar-target", matter / records.SIDECAR_DIR)

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10)

    assert err.value.code == "sidecar_symlink"


def test_journal_hardlink_is_refused_without_external_append(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir(mode=0o700)
    target = tmp_path / "outside.jsonl"
    target.write_text("", encoding="utf-8")
    os.link(target, sidecar / records.JOURNAL_NAME)

    result = server.list_rounds(str(matter))

    assert result["record_status"] == "write_failed"
    assert "journal_hardlink" in result["record_error"]
    assert target.read_text(encoding="utf-8") == ""


def test_gitignore_hardlink_is_refused_without_external_change(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir(mode=0o700)
    target = tmp_path / "outside-gitignore"
    target.write_text("sentinel\n", encoding="utf-8")
    os.link(target, sidecar / records.GITIGNORE_NAME)

    result = server.list_rounds(str(matter))

    assert result["record_status"] == "write_failed"
    assert "gitignore_hardlink" in result["record_error"]
    assert target.read_text(encoding="utf-8") == "sentinel\n"
    assert (sidecar / records.GITIGNORE_NAME).read_text(encoding="utf-8") == "sentinel\n"
    assert (sidecar / records.GITIGNORE_NAME).stat().st_ino == target.stat().st_ino
    assert not (sidecar / records.JOURNAL_NAME).exists()


def test_gitignore_symlink_is_refused_before_append(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir(mode=0o700)
    target = tmp_path / "outside-gitignore"
    target.write_text("*\n", encoding="utf-8")
    os.symlink(target, sidecar / records.GITIGNORE_NAME)

    result = server.list_rounds(str(matter))

    assert result["record_status"] == "write_failed"
    assert "gitignore_symlink" in result["record_error"]
    assert not (sidecar / records.JOURNAL_NAME).exists()


def test_gitignore_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir(mode=0o700)
    os.mkfifo(sidecar / records.GITIGNORE_NAME)

    result = server.list_rounds(str(matter))

    assert result["record_status"] == "write_failed"
    assert "gitignore_not_file" in result["record_error"]
    assert not (sidecar / records.JOURNAL_NAME).exists()


def test_gitignore_unexpected_content_is_refused(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir(mode=0o700)
    (sidecar / records.GITIGNORE_NAME).write_text("not-private\n", encoding="utf-8")

    result = server.list_rounds(str(matter))

    assert result["record_status"] == "write_failed"
    assert "gitignore_invalid" in result["record_error"]
    assert not (sidecar / records.JOURNAL_NAME).exists()


def test_valid_gitignore_permissions_are_tightened(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir(mode=0o700)
    ignore = sidecar / records.GITIGNORE_NAME
    ignore.write_text("*\n", encoding="utf-8")
    ignore.chmod(0o644)

    result = server.list_rounds(str(matter))

    assert result["record_status"] == "written"
    assert stat.S_IMODE(ignore.stat().st_mode) == 0o600


def test_gitignore_directory_fsync_failure_is_visible_and_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    real_fsync = records.os.fsync
    directory_calls = 0

    def fail_gitignore_directory_fsync(fd: int) -> None:
        nonlocal directory_calls
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_calls += 1
            if directory_calls == 2:
                raise OSError(errno.EIO, "simulated gitignore directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(records.os, "fsync", fail_gitignore_directory_fsync)
    result = server.list_rounds(str(matter))

    assert result["record_status"] == "write_failed"
    sidecar = matter / records.SIDECAR_DIR
    assert not (sidecar / records.GITIGNORE_NAME).exists()
    assert not (sidecar / records.JOURNAL_NAME).exists()

    monkeypatch.setattr(records.os, "fsync", real_fsync)
    assert server.list_rounds(str(matter))["record_status"] == "written"


def test_journal_directory_fsync_failure_is_visible_and_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir(mode=0o700)
    ignore = sidecar / records.GITIGNORE_NAME
    ignore.write_text("*\n", encoding="utf-8")
    ignore.chmod(0o600)
    real_fsync = records.os.fsync
    failed = False

    def fail_journal_directory_fsync(fd: int) -> None:
        nonlocal failed
        if not failed and stat.S_ISDIR(os.fstat(fd).st_mode):
            failed = True
            raise OSError(errno.EIO, "simulated journal directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(records.os, "fsync", fail_journal_directory_fsync)
    result = server.list_rounds(str(matter))

    assert result["record_status"] == "write_failed"
    assert not (sidecar / records.JOURNAL_NAME).exists()

    monkeypatch.setattr(records.os, "fsync", real_fsync)
    assert server.list_rounds(str(matter))["record_status"] == "written"


def test_dangling_journal_symlink_is_refused_on_read(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir(mode=0o700)
    os.symlink(tmp_path / "missing-journal-target", sidecar / records.JOURNAL_NAME)

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10)

    assert err.value.code == "journal_symlink"


def test_fifo_journal_is_rejected_without_blocking(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir(mode=0o700)
    os.mkfifo(sidecar / records.JOURNAL_NAME)

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10)

    assert err.value.code == "journal_not_file"


def test_workspace_identity_change_is_rejected(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    root, identity = records._canonical_workspace(matter)
    matter.rename(tmp_path / "original-matter")
    matter.mkdir()

    with pytest.raises(records.DecisionRecordError) as err:
        records._open_workspace_fd(root, identity)

    assert err.value.code == "workspace_changed"


def test_transient_journal_open_enoent_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    real_open = records.os.open
    attempts = 0

    def flaky_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal attempts
        if path == records.JOURNAL_NAME and flags & os.O_CREAT and attempts < 2:
            attempts += 1
            raise FileNotFoundError(errno.ENOENT, "transient journal open")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(records.os, "open", flaky_open)

    result = server.list_rounds(str(matter))

    assert result["record_status"] == "written"
    assert attempts == 2


def test_controlled_failures_are_recorded_then_reraised(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    source = matter / "round-2-counterparty-redline.docx"
    extracted, anchor = _cap_from_tool(source)
    bad_anchor = {**anchor, "change_unit_id": "cu_999"}

    with pytest.raises(veqtor_docx.ApplyError) as err:
        server.apply_edits(
            str(source),
            str(tmp_path / "never.docx"),
            [{"anchor": bad_anchor, "delete_text": "x", "insert_text": "y"}],
        )

    assert err.value.code == "anchor_not_found"
    exported = server.export_decision_record(
        str(matter), max_records=10, include_payload=True
    )
    error_records = [
        record
        for record in exported["records"]
        if record["tool_name"] == "apply_edits" and record["result"]["status"] == "error"
    ]
    assert len(error_records) == 1
    assert error_records[0]["result"]["error_code"] == "anchor_not_found"
    assert error_records[0]["input"]["edits"][0]["anchor"]["change_unit_id"] == "cu_999"
    assert error_records[0]["provenance"]["claimed_source_sha256"] == extracted["file_sha256"]
    assert error_records[0]["provenance"]["observed_source_sha256"] == extracted["file_sha256"]


def test_hash_mismatch_records_claimed_and_observed_sha(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    source = matter / "round-2-counterparty-redline.docx"
    extracted, anchor = _cap_from_tool(source)
    bad_anchor = {**anchor, "file_sha256": "0" * 64}

    with pytest.raises(veqtor_docx.ApplyError) as err:
        server.apply_edits(
            str(source),
            str(tmp_path / "never.docx"),
            [{"anchor": bad_anchor, "delete_text": "x", "insert_text": "y"}],
        )

    assert err.value.code == "file_sha256_mismatch"
    exported = records.read_records(str(matter), max_records=10)
    error_record = next(
        record
        for record in exported["records"]
        if record["tool_name"] == "apply_edits" and record["result"]["status"] == "error"
    )
    assert error_record["provenance"]["claimed_source_sha256"] == "0" * 64
    assert error_record["provenance"]["observed_source_sha256"] == extracted["file_sha256"]
    assert "source_sha256" not in error_record["provenance"]


def test_multi_edit_hash_mismatch_records_offending_claim(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    source = matter / "round-2-counterparty-redline.docx"
    extracted, anchor = _cap_from_tool(source)
    bad_anchor = {**anchor, "file_sha256": "0" * 64}

    with pytest.raises(veqtor_docx.ApplyError) as err:
        server.apply_edits(
            str(source),
            str(tmp_path / "never.docx"),
            [
                {
                    "anchor": anchor,
                    "delete_text": " in respect of all claims in aggregate.",
                    "insert_text": " per claim.",
                },
                {
                    "anchor": bad_anchor,
                    "delete_text": "x",
                    "insert_text": "y",
                },
            ],
        )

    assert err.value.code == "file_sha256_mismatch"
    error_record = next(
        record
        for record in records.read_records(str(matter), max_records=10)["records"]
        if record["tool_name"] == "apply_edits" and record["result"]["status"] == "error"
    )
    assert error_record["provenance"]["claimed_source_sha256"] == "0" * 64
    assert error_record["provenance"]["observed_source_sha256"] == extracted["file_sha256"]
    assert error_record["provenance"]["edit_index"] == 1


def test_multi_edit_delete_text_failure_records_offending_index(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    source = matter / "round-2-counterparty-redline.docx"
    extracted, anchor = _cap_from_tool(source)

    with pytest.raises(veqtor_docx.ApplyError) as err:
        server.apply_edits(
            str(source),
            str(tmp_path / "never.docx"),
            [
                {
                    "anchor": anchor,
                    "delete_text": " in respect of all claims in aggregate.",
                    "insert_text": " per claim.",
                },
                {
                    "anchor": anchor,
                    "delete_text": "definitely absent from the anchored clause",
                    "insert_text": "replacement",
                },
            ],
        )

    assert err.value.code == "delete_text_not_found"
    error_record = next(
        record
        for record in records.read_records(str(matter), max_records=10)["records"]
        if record["tool_name"] == "apply_edits" and record["result"]["status"] == "error"
    )
    assert error_record["provenance"]["claimed_source_sha256"] == extracted["file_sha256"]
    assert error_record["provenance"]["observed_source_sha256"] == extracted["file_sha256"]
    assert error_record["provenance"]["edit_index"] == 1


def test_malformed_second_edit_records_offending_index(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    source = matter / "round-2-counterparty-redline.docx"
    extracted, anchor = _cap_from_tool(source)

    with pytest.raises(veqtor_docx.ApplyError) as err:
        server.apply_edits(
            str(source),
            str(tmp_path / "never.docx"),
            [
                {
                    "anchor": anchor,
                    "delete_text": " in respect of all claims in aggregate.",
                    "insert_text": " per claim.",
                },
                {
                    "anchor": anchor,
                    "delete_text": "x",
                    "insert_text": 123,
                },
            ],
        )

    assert err.value.code == "invalid_edit"
    error_record = next(
        record
        for record in records.read_records(str(matter), max_records=10)["records"]
        if record["tool_name"] == "apply_edits" and record["result"]["status"] == "error"
    )
    assert error_record["provenance"]["claimed_source_sha256"] == extracted["file_sha256"]
    assert error_record["provenance"]["observed_source_sha256"] == extracted["file_sha256"]
    assert error_record["provenance"]["edit_index"] == 1


def test_late_apply_failure_records_offending_plan_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    source = matter / "round-2-counterparty-redline.docx"
    extracted, anchor = _cap_from_tool(source)
    apply_module = importlib.import_module("veqtor_docx.apply")
    original_apply_plan = apply_module._apply_plan

    def fail_second_plan(plan, author):
        if plan.edit_index == 1:
            raise veqtor_docx.ApplyError(
                "unsupported_run_shape", "simulated late apply failure"
            )
        return original_apply_plan(plan, author)

    monkeypatch.setattr(apply_module, "_apply_plan", fail_second_plan)
    output = tmp_path / "never.docx"

    with pytest.raises(veqtor_docx.ApplyError) as err:
        server.apply_edits(
            str(source),
            str(output),
            [
                {
                    "anchor": anchor,
                    "delete_text": "USD 50,000",
                    "insert_text": "EUR 60,000",
                },
                {
                    "anchor": anchor,
                    "delete_text": "Except as set out",
                    "insert_text": "Save as provided",
                },
            ],
        )

    assert err.value.code == "unsupported_run_shape"
    assert err.value.metadata["edit_index"] == 1
    assert err.value.metadata["claimed_source_sha256"] == extracted["file_sha256"]
    assert err.value.metadata["observed_source_sha256"] == extracted["file_sha256"]
    assert not output.exists()
    error_record = next(
        record
        for record in records.read_records(str(matter), max_records=10)["records"]
        if record["tool_name"] == "apply_edits" and record["result"]["status"] == "error"
    )
    assert error_record["provenance"]["edit_index"] == 1
    assert error_record["provenance"]["claimed_source_sha256"] == extracted["file_sha256"]
    assert error_record["provenance"]["observed_source_sha256"] == extracted["file_sha256"]


def test_export_missing_journal_returns_empty_and_records_export(tmp_path: Path) -> None:
    matter = tmp_path / "empty-matter"
    matter.mkdir()

    exported = server.export_decision_record(str(matter))

    assert exported["records"] == []
    assert exported["total_count"] == 0
    assert exported["access_count"] == 0
    assert exported["truncated"] is False
    assert exported["record_status"] == "written"
    assert exported["payloads"] == "compact"

    reread = records.read_records(str(matter), max_records=10, include_access_events=True)
    assert reread["total_count"] == 1
    assert reread["access_count"] == 1
    assert reread["records"][0]["tool_name"] == "export_decision_record"


def test_export_excludes_access_events_and_supports_cursor(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    for _ in range(3):
        server.list_rounds(str(matter))

    newest = server.export_decision_record(str(matter), max_records=1)
    assert [record["record_id"] for record in newest["records"]] == ["dr_003"]
    assert newest["next_before_record_id"] == "dr_003"

    second_export = server.export_decision_record(str(matter), max_records=1)
    assert [record["record_id"] for record in second_export["records"]] == ["dr_003"]
    assert second_export["access_count"] == 1

    previous = server.export_decision_record(
        str(matter), max_records=1, before_record_id=newest["next_before_record_id"]
    )
    assert [record["record_id"] for record in previous["records"]] == ["dr_002"]
    assert previous["next_before_record_id"] == "dr_002"


def test_default_export_is_compact_for_large_verbatim_input(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    source = matter / "round-2-counterparty-redline.docx"
    _, anchor = _cap_from_tool(source)
    quote = "X" * 200_000

    result = server.verify_quote(str(source), anchor, quote)
    assert result["verdict"] == "not_found"
    assert result["record_status"] == "written"

    exported = server.export_decision_record(str(matter), max_records=1)
    encoded = json.dumps(exported, ensure_ascii=False).encode("utf-8")
    assert len(encoded) < 20_000
    record = exported["records"][0]
    assert record["payloads"] == "compact"
    assert record["input"]["omitted"] is True
    assert "X" * 100 not in encoded.decode("utf-8")

    full = server.export_decision_record(
        str(matter),
        max_records=1,
        include_payload=True,
    )
    assert full["payloads"] == "full"
    assert full["records"][0]["input"]["quote"] == quote


def test_compact_export_omits_clause_and_change_text(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    source = matter / "round-2-counterparty-redline.docx"
    extracted, anchor = _cap_from_tool(source)
    cap = next(
        unit
        for unit in extracted["change_units"]
        if unit["change_unit_id"] == anchor["change_unit_id"]
    )

    server.verify_quote(
        str(source),
        anchor,
        "the total fees paid by Client under this Agreement",
    )

    exported = server.export_decision_record(str(matter), max_records=10)
    encoded = json.dumps(exported, ensure_ascii=False)
    assert exported["payloads"] == "compact"
    assert "Limitation of Liability" not in encoded
    assert cap["old_text"] not in encoded
    assert cap["new_text"] not in encoded
    verify_record = next(
        record for record in exported["records"] if record["tool_name"] == "verify_quote"
    )
    match = verify_record["result"]["matches"]["sample"][0]
    assert "clause" not in match
    assert "clause_sha256" in match


def test_compact_export_omits_client_asserted_anchor_fields(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    source = matter / "round-2-counterparty-redline.docx"
    _, anchor = _cap_from_tool(source)
    sentinel = "PRIVATE_VERBATIM_SENTINEL_42"
    asserted_anchor = {
        **anchor,
        "part_name": sentinel,
        "revision_ids": [sentinel],
    }

    result = server.verify_quote(
        str(source),
        asserted_anchor,
        "the total fees paid by Client under this Agreement",
    )
    assert result["verdict"] == "exact"

    exported = server.export_decision_record(str(matter), max_records=10)
    encoded = json.dumps(exported, ensure_ascii=False)
    assert sentinel not in encoded
    assert str(matter.resolve()) not in encoded
    verify_record = next(
        record for record in exported["records"] if record["tool_name"] == "verify_quote"
    )
    assert verify_record["provenance"]["input_anchor"]["omitted"] is True
    full_records = records.read_records(str(matter), max_records=10)["records"]
    assert sentinel not in json.dumps(full_records, ensure_ascii=False)


def test_compact_export_hashes_failed_client_claims(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    source = matter / "round-2-counterparty-redline.docx"
    _, anchor = _cap_from_tool(source)
    sentinel = "PRIVATE_FAILURE_SENTINEL_84"
    bad_anchor = {
        **anchor,
        "change_unit_id": sentinel,
        "file_sha256": sentinel,
    }

    with pytest.raises(veqtor_docx.ApplyError):
        server.apply_edits(
            str(source),
            str(tmp_path / "never.docx"),
            [{"anchor": bad_anchor, "delete_text": "x", "insert_text": "y"}],
        )

    exported = server.export_decision_record(str(matter), max_records=10)
    encoded = json.dumps(exported, ensure_ascii=False)
    assert sentinel not in encoded
    error_record = next(
        record
        for record in exported["records"]
        if record["tool_name"] == "apply_edits"
    )
    assert error_record["provenance"]["claimed_source_sha256"]["omitted"] is True
    assert error_record["provenance"]["anchors"]["sample"] == []
    assert error_record["provenance"]["anchors"]["truncated"] is True


@pytest.mark.parametrize(
    "sentinel",
    ["²" * 16, "٠١٢", "１２３"],
    ids=["superscript", "arabic-indic", "full-width"],
)
def test_compact_export_filters_non_ascii_observed_revision_ids(
    tmp_path: Path, sentinel: str
) -> None:
    matter = _matter(tmp_path)
    source = matter / "round-2-counterparty-redline.docx"
    with zipfile.ZipFile(source, "r") as archive:
        infos = archive.infolist()
        parts = {info.filename: archive.read(info.filename) for info in infos}
    document = parts["word/document.xml"]
    assert b'w:id="101"' in document
    parts["word/document.xml"] = document.replace(
        b'w:id="101"', f'w:id="{sentinel}"'.encode(), 1
    )
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        for info in infos:
            archive.writestr(info, parts[info.filename])

    extracted = server.extract_redlines(str(source))
    unit = next(
        item
        for item in extracted["change_units"]
        if sentinel in item["reference"]["revision_ids"]
    )
    quote = unit["new_text"] or unit["old_text"]
    verified = server.verify_quote(
        str(source),
        {
            "change_unit_id": unit["change_unit_id"],
            "file_sha256": extracted["file_sha256"],
        },
        quote,
    )
    assert verified["verdict"] == "exact"

    exported = server.export_decision_record(str(matter), max_records=10)
    encoded = json.dumps(exported, ensure_ascii=False)
    assert sentinel not in encoded
    verify_record = next(
        record for record in exported["records"] if record["tool_name"] == "verify_quote"
    )
    revision_ids = verify_record["result"]["matches"]["sample"][0]["revision_ids"]
    assert revision_ids["count"] >= 1
    assert sentinel not in revision_ids["sample"]
    assert revision_ids["truncated"] is True


@pytest.mark.parametrize("digits", ["²", "٠١٢", "１２３"])
def test_all_record_identifiers_require_ascii_decimal_digits(digits: str) -> None:
    assert records._revision_id(digits) is None
    assert records._change_unit_id(f"cu_{digits}") is None
    with pytest.raises(ValueError):
        records._record_number(f"dr_{digits}")

    assert records._revision_id("101") == "101"
    assert records._change_unit_id("cu_001") == "cu_001"
    assert records._record_number("dr_001") == 1


def test_all_compact_array_projections_filter_invalid_items() -> None:
    sentinel = "PRIVATE_ARRAY_SENTINEL_99"
    records_to_project = [
        {
            "tool_name": "list_rounds",
            "result": {
                "status": "ok",
                "folder": "/matter",
                "rounds": [{"sha256": sentinel, "revision_count": 1}],
                "skipped": [],
            },
        },
        {
            "tool_name": "apply_edits",
            "result": {
                "status": "ok",
                "output_sha256": "a" * 64,
                "applied": [
                    {
                        "change_unit_id": sentinel,
                        "operation": "replace",
                        "tracked_revision_ids": [sentinel],
                    }
                ],
                "round_trip_check": {"status": "passed", "collateral_changes": []},
            },
        },
        {
            "tool_name": "verify_quote",
            "result": {
                "status": "ok",
                "verdict": "exact",
                "exact": True,
                "checked_anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64},
                "matches": [
                    {
                        "part_name": "word/document.xml",
                        "revision_ids": [sentinel],
                        "side": "new",
                        "clause": "clause",
                    }
                ],
                "diff": [],
            },
        },
    ]

    projected = [records._summary_result(record) for record in records_to_project]
    encoded = json.dumps(projected, ensure_ascii=False)

    assert sentinel not in encoded
    assert projected[0]["rounds"]["sample"] == []
    assert projected[0]["rounds"]["truncated"] is True
    assert projected[1]["applied"]["sample"] == []
    revision_ids = projected[2]["matches"]["sample"][0]["revision_ids"]
    assert revision_ids["sample"] == []
    assert revision_ids["truncated"] is True


def test_docx_producer_domains_are_shared_with_v1_projection() -> None:
    assert records.EXTRACT_REVISION_CATEGORIES_V1 is (
        docx_contracts.EXTRACT_REVISION_CATEGORIES_V1
    )
    assert records.VERIFY_VERDICTS_V1 is docx_contracts.VERIFY_VERDICTS_V1
    assert records.APPLY_OPERATIONS_V1 is docx_contracts.APPLY_OPERATIONS_V1
    assert records.MATCH_SIDES_V1 is docx_contracts.MATCH_SIDES_V1
    assert records.RESULT_STATUS_OK == docx_contracts.RESULT_STATUS_OK
    assert records.RESULT_STATUS_ERROR == docx_contracts.RESULT_STATUS_ERROR
    assert records.ROUND_TRIP_STATUSES_V1 is (
        docx_contracts.ROUND_TRIP_STATUSES_V1
    )
    assert records.ROUND_TRIP_COMPARISONS_V1 is (
        docx_contracts.ROUND_TRIP_COMPARISONS_V1
    )
    assert docx_contracts.VERIFY_VERDICTS_V1 == {
        "exact",
        "normalized",
        "not_found",
    }
    assert docx_contracts.APPLY_OPERATIONS_V1 == {
        "replace",
        "delete",
        "counter",
        "reinstate",
    }
    assert docx_contracts.MATCH_SIDES_V1 == {"old", "new"}
    assert docx_contracts.DOCUMENT_PART_V1 == "word/document.xml"
    assert docx_contracts.ROUND_TRIP_STATUSES_V1 == {"passed"}
    assert docx_contracts.ROUND_TRIP_COMPARISONS_V1 == {
        "exact",
        "ooxml_semantic_diff_outside_touched_anchors",
    }


@pytest.mark.parametrize(
    "category", sorted(docx_contracts.EXTRACT_REVISION_CATEGORIES_V1)
)
def test_every_extract_revision_category_is_projected(category: str) -> None:
    projected = records._bounded_mapping({category: 1})

    assert projected["count"] == 1
    assert projected["sample"] == [{"key": category, "value": 1}]
    assert projected["truncated"] is False


def test_synthetic_extract_revision_categories_survive_compact_export(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    source = sorted(matter.glob("*.docx"))[1]

    extracted = server.extract_redlines(str(source))
    exported = server.export_decision_record(str(matter), max_records=10)
    extract_record = next(
        record
        for record in exported["records"]
        if record["tool_name"] == "extract_redlines"
    )
    snapshot = extract_record["result"]["unsupported_revisions"]
    projected_categories = {
        item["key"]: item["value"] for item in snapshot["sample"]
    }

    assert extracted["unsupported_revisions"] == {
        "rPrChange": 1,
        "trPrIns": 1,
        "paragraphMarkIns": 2,
    }
    assert projected_categories == extracted["unsupported_revisions"]
    assert snapshot["count"] == 3
    assert snapshot["truncated"] is False


def test_ten_thousand_anchors_have_bounded_journal_and_export(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    anchors = [
        {
            "change_unit_id": f"cu_{index:05d}",
            "file_sha256": "a" * 64,
            "revision_ids": [str(index)],
            "clause_anchor": None,
        }
        for index in range(10_000)
    ]
    provenance = {
        "path": str(matter / "large.docx"),
        "file_sha256": "a" * 64,
        "part_name": "word/document.xml",
        "anchors": records.bounded_observed_anchors(anchors),
    }
    meta = records.write_record(
        workspace=matter,
        tool_name="extract_redlines",
        input_payload={"path": str(matter / "large.docx")},
        result={
            "status": "ok",
            "path": str(matter / "large.docx"),
            "file_sha256": "a" * 64,
            "part_name": "word/document.xml",
            "revision_count": 10_000,
            "change_unit_count": 10_000,
            "unsupported_revisions": {},
        },
        provenance=provenance,
    )
    assert meta["record_status"] == "written"
    stored = records.read_records(str(matter), max_records=1)["records"][0]
    assert stored["provenance"]["anchors"]["count"] == 10_000
    assert len(stored["provenance"]["anchors"]["sample"]) == 20
    assert stored["provenance"]["anchors"]["truncated"] is True
    assert len(json.dumps(stored, ensure_ascii=False).encode()) < 20_000

    exported = server.export_decision_record(str(matter), max_records=1)
    assert len(json.dumps(exported, ensure_ascii=False).encode()) < 20_000
    assert exported["records"][0]["provenance"]["anchors"] == stored[
        "provenance"
    ]["anchors"]


def test_compact_export_reprojects_prebounded_anchor_snapshots(
    tmp_path: Path,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sentinel = "PRIVATE_PREBOUNDED_ANCHOR_SENTINEL_42"
    sample = [
        {
            "change_unit_id": f"cu_{index:03d}",
            "file_sha256": "a" * 64,
            "revision_ids": {
                "count": 1,
                "sha256": "b" * 64,
                "sample": [str(index)],
                "truncated": False,
                "private_nested": sentinel,
            },
            "clause_anchor_sha256": "c" * 64,
            "private_item": sentinel,
        }
        for index in range(25)
    ]
    meta = records.write_record(
        workspace=matter,
        tool_name="extract_redlines",
        input_payload={},
        result={
            "status": "ok",
            "file_sha256": "a" * 64,
            "part_name": "word/document.xml",
            "revision_count": 25,
            "change_unit_count": 25,
            "unsupported_revisions": {},
        },
        provenance={
            "anchors": {
                "count": 25,
                "sha256": "d" * 64,
                "sample": sample,
                "truncated": False,
                "private_snapshot": sentinel,
            }
        },
    )

    assert meta["record_status"] == "written"
    exported = server.export_decision_record(str(matter), max_records=10)
    encoded = json.dumps(exported, ensure_ascii=False)
    projected = exported["records"][0]["provenance"]["anchors"]

    assert sentinel not in encoded
    assert projected["count"] == 25
    assert projected["sha256"] == "d" * 64
    assert len(projected["sample"]) == records.COMPACT_SAMPLE_LIMIT
    assert projected["sample"][-1]["change_unit_id"] == "cu_019"
    assert projected["truncated"] is True
    assert set(projected) == {"count", "sha256", "sample", "truncated"}
    assert all(
        set(item)
        <= {
            "change_unit_id",
            "file_sha256",
            "part_name",
            "revision_ids",
            "side",
            "clause_anchor_sha256",
        }
        for item in projected["sample"]
    )


@pytest.mark.parametrize(
    "snapshot",
    [
        {
            "count": 1,
            "sha256": "not-a-digest",
            "sample": [],
            "truncated": False,
        },
        {
            "count": 0,
            "sha256": "a" * 64,
            "sample": [{"change_unit_id": "cu_001"}],
            "truncated": False,
        },
        {
            "count": 1,
            "sha256": "a" * 64,
            "sample": [],
            "truncated": "PRIVATE_INVALID_BOOLEAN",
        },
    ],
)
def test_compact_export_omits_invalid_prebounded_anchor_snapshots(
    snapshot: dict[str, object],
) -> None:
    projected = records._summary_provenance(
        {
            "tool_name": "extract_redlines",
            "result": {"status": "ok"},
            "provenance": {"anchors": snapshot},
        }
    )

    assert "anchors" not in projected


@pytest.mark.parametrize("extra_location", ["snapshot", "item", "nested"])
def test_prebounded_snapshot_marks_filtered_fields_truncated(
    extra_location: str,
) -> None:
    sentinel = "PRIVATE_FILTERED_SNAPSHOT_FIELD_91"
    item = {
        "change_unit_id": "cu_001",
        "revision_ids": {
            "count": 1,
            "sha256": "b" * 64,
            "sample": ["1"],
            "truncated": False,
        },
    }
    snapshot = {
        "count": 1,
        "sha256": "a" * 64,
        "sample": [item],
        "truncated": False,
    }
    if extra_location == "snapshot":
        snapshot["private"] = sentinel
    elif extra_location == "item":
        item["private"] = sentinel
    else:
        item["revision_ids"]["private"] = sentinel

    projected = records._validated_bounded_snapshot(
        snapshot, records._observed_anchor_summary
    )

    assert projected is not None
    assert projected["truncated"] is True
    assert sentinel not in json.dumps(projected, ensure_ascii=False)


def test_clean_prebounded_snapshot_projection_is_idempotent() -> None:
    snapshot = {
        "count": 1,
        "sha256": "a" * 64,
        "sample": [
            {
                "change_unit_id": "cu_001",
                "revision_ids": {
                    "count": 1,
                    "sha256": "b" * 64,
                    "sample": ["1"],
                    "truncated": False,
                },
            }
        ],
        "truncated": False,
    }

    assert records._validated_bounded_snapshot(
        snapshot, records._observed_anchor_summary
    ) == snapshot


@pytest.mark.parametrize(
    "created_at",
    [
        "2026-07-10\n00:00:00Z",
        "2026-07-10X00:00:00Z",
        "2026-07-10 00:00:00Z",
        "2026-07-10T00:00:00.1Z",
        "2026-07-10T00:00:00+00:00",
    ],
)
def test_compact_timestamp_rejects_values_outside_writer_grammar(
    created_at: str,
) -> None:
    assert records._compact_created_at(created_at) is None


def test_compact_timestamp_accepts_exact_writer_grammar() -> None:
    assert records._compact_created_at("2026-07-10T00:00:00Z") == (
        "2026-07-10T00:00:00Z"
    )
    assert records._compact_created_at("2026-07-10T00:00:00.123456Z") == (
        "2026-07-10T00:00:00.123456Z"
    )


@pytest.mark.parametrize(
    "skipped",
    [None, "PRIVATE_SKIPPED_SENTINEL"],
    ids=["null", "private_string"],
)
def test_malformed_provenance_skipped_is_safe_in_compact_export(
    tmp_path: Path,
    skipped: object,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    meta = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok", "rounds": [], "skipped": []},
        provenance={"rounds": [], "skipped": skipped},
    )

    assert meta["record_status"] == "written"
    compact = records.read_records(
        str(matter), max_records=1, include_payload=False
    )
    encoded = json.dumps(compact, ensure_ascii=False)

    assert compact["records"][0]["provenance"]["skipped_count"] == 0
    assert "PRIVATE_SKIPPED_SENTINEL" not in encoded


def test_unexpected_compact_projection_failure_is_controlled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    assert records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok", "rounds": [], "skipped": []},
        provenance={},
    )["record_status"] == "written"
    assert records.read_records(
        str(matter), max_records=1, include_payload=True
    )["records"]

    def fail_projection(_record: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("PRIVATE_PROJECTION_FAILURE")

    monkeypatch.setattr(records, "_compact_record", fail_projection)

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=1, include_payload=False)

    assert err.value.code == "journal_corrupt"
    assert str(err.value) == "journal_corrupt: compact projection failed"
    assert "PRIVATE_PROJECTION_FAILURE" not in str(err.value)


def test_compact_projection_is_total_for_schema_readable_nested_json() -> None:
    sentinel = "PRIVATE_TOTALITY_SENTINEL_57"
    shapes: list[object] = [
        None,
        False,
        7,
        0.5,
        sentinel,
        [sentinel],
        {"private": sentinel},
    ]
    base_results: dict[str, dict[str, object]] = {
        "list_rounds": {
            "status": "ok",
            "folder": "/matter",
            "rounds": [],
            "skipped": [],
        },
        "extract_redlines": {
            "status": "ok",
            "path": "/matter/round.docx",
            "file_sha256": "a" * 64,
            "part_name": "word/document.xml",
            "revision_count": 0,
            "change_unit_count": 0,
            "unsupported_revisions": {},
        },
        "apply_edits": {
            "status": "ok",
            "output_sha256": "a" * 64,
            "applied": [],
            "round_trip_check": {},
        },
        "verify_quote": {
            "status": "ok",
            "verdict": "not_found",
            "exact": False,
            "checked_anchor": {},
            "matches": [],
            "diff": [],
        },
        "export_decision_record": {
            "status": "ok",
            "total_count": 0,
            "access_count": 0,
            "returned_count": 0,
            "truncated": False,
            "next_before_record_id": None,
            "payloads": "compact",
        },
    }
    base_provenance: dict[str, dict[str, object]] = {
        "list_rounds": {"folder": "/matter", "rounds": [], "skipped": []},
        "extract_redlines": {
            "path": "/matter/round.docx",
            "file_sha256": "a" * 64,
            "part_name": "word/document.xml",
            "anchors": [],
        },
        "apply_edits": {
            "source_path": "/matter/source.docx",
            "output_path": "/matter/output.docx",
            "source_sha256": "a" * 64,
            "output_sha256": "b" * 64,
            "claimed_source_sha256": "a" * 64,
            "observed_source_sha256": "a" * 64,
            "edit_index": 0,
            "anchors": [],
            "applied": [],
            "round_trip_check": {},
        },
        "verify_quote": {
            "path": "/matter/round.docx",
            "file_sha256": "a" * 64,
            "verdict": "not_found",
            "checked_anchor": {},
            "input_anchor": {},
            "anchors": [],
        },
        "export_decision_record": {"workspace": "/matter"},
    }
    result_fields = {
        tool_name: tuple(result)
        for tool_name, result in base_results.items()
    }
    provenance_fields = {
        tool_name: tuple(provenance)
        for tool_name, provenance in base_provenance.items()
    }

    for section, fields_by_tool in (
        ("result", result_fields),
        ("provenance", provenance_fields),
    ):
        for tool_name, fields in fields_by_tool.items():
            for field in fields:
                for shape in shapes:
                    result = copy.deepcopy(base_results[tool_name])
                    provenance = copy.deepcopy(base_provenance[tool_name])
                    target = result if section == "result" else provenance
                    target[field] = copy.deepcopy(shape)
                    record = {
                        "schema_version": records.SCHEMA_VERSION,
                        "record_type": records.V1_HISTORICAL_TOOL_SPECS[
                            tool_name
                        ].record_type,
                        "record_id": "dr_001",
                        "created_at": "2026-07-10T00:00:00Z",
                        "tool_name": tool_name,
                        "workspace": "/matter",
                        "producer": {
                            "name": "veqtor-mcp",
                            "version": "0.0.0",
                            "build": records.SOURCE_SNAPSHOT_UNAVAILABLE,
                        },
                        "input": {},
                        "result": result,
                        "result_sha256": records._stable_digest(result),
                        "tool_result_sha256": records._stable_digest(result),
                        "provenance": provenance,
                    }
                    case = f"{tool_name}:{section}.{field}:{type(shape).__name__}"
                    try:
                        records._validated_record_bytes(record)
                        compact = records._compact_record(record)
                    except Exception as exc:
                        pytest.fail(
                            f"compact projection was not total for {case}: {exc}"
                        )
                    assert sentinel not in json.dumps(
                        compact, ensure_ascii=False
                    ), case


def test_compact_item_projectors_are_total_for_nested_json_types() -> None:
    sentinel = "PRIVATE_NESTED_TOTALITY_SENTINEL_63"
    shapes: list[object] = [
        None,
        False,
        7,
        0.5,
        sentinel,
        [sentinel],
        {"private": sentinel},
    ]
    cases = [
        (
            records._observed_anchor_summary,
            {"change_unit_id": "cu_001"},
            (
                "change_unit_id",
                "file_sha256",
                "part_name",
                "revision_ids",
                "side",
                "clause_anchor",
                "clause_anchor_sha256",
            ),
        ),
        (
            records._observed_round_summary,
            {"sha256": "a" * 64, "revision_count": 1},
            ("sha256", "revision_count"),
        ),
        (
            records._observed_applied_summary,
            {
                "change_unit_id": "cu_001",
                "operation": "replace",
                "tracked_revision_ids": [],
            },
            ("change_unit_id", "operation", "tracked_revision_ids"),
        ),
        (
            records._observed_match_summary,
            {
                "part_name": "word/document.xml",
                "revision_ids": [],
                "side": "new",
                "clause": None,
            },
            ("part_name", "revision_ids", "side", "clause"),
        ),
        (
            records._round_trip_summary,
            {
                "status": "passed",
                "comparison": "exact",
                "collateral_changes": [],
            },
            ("status", "comparison", "collateral_changes"),
        ),
    ]

    for projector, base, fields in cases:
        for field in fields:
            for shape in shapes:
                value = copy.deepcopy(base)
                value[field] = copy.deepcopy(shape)
                case = f"{projector.__name__}:{field}:{type(shape).__name__}"
                try:
                    projected = projector(value)
                except Exception as exc:
                    pytest.fail(f"item projection was not total for {case}: {exc}")
                assert sentinel not in json.dumps(
                    projected, ensure_ascii=False
                ), case


def test_compact_projection_never_copies_unvalidated_scalars() -> None:
    sentinel = "PRIVATE_COMPACT_SCALAR_SENTINEL_73"
    result_records = [
        {
            "tool_name": "list_rounds",
            "result": {"status": sentinel, "rounds": [], "skipped": []},
        },
        {
            "tool_name": "extract_redlines",
            "result": {
                "status": "ok",
                "revision_count": sentinel,
                "change_unit_count": sentinel,
                "unsupported_revisions": {sentinel: 1},
            },
        },
        {
            "tool_name": "apply_edits",
            "result": {
                "status": sentinel,
                "round_trip_check": {
                    "status": sentinel,
                    "comparison": sentinel,
                },
            },
        },
        {
            "tool_name": "verify_quote",
            "result": {
                "status": sentinel,
                "verdict": sentinel,
                "exact": sentinel,
            },
        },
        {
            "tool_name": "export_decision_record",
            "result": {
                "status": sentinel,
                "total_count": sentinel,
                "access_count": sentinel,
                "returned_count": sentinel,
                "truncated": sentinel,
                "next_before_record_id": sentinel,
                "payloads": sentinel,
            },
        },
        {
            "tool_name": "apply_edits",
            "result": {
                "status": "error",
                "error_code": sentinel,
                "error": sentinel,
            },
        },
    ]
    compact_record = {
        "schema_version": records.SCHEMA_VERSION,
        "record_type": "tool_observation.v1",
        "record_id": "dr_001",
        "created_at": sentinel,
        "tool_name": "list_rounds",
        "workspace": sentinel,
        "producer": {"name": sentinel, "version": sentinel, "build": sentinel},
        "input": {"private": sentinel},
        "result": {"status": sentinel, "rounds": [], "skipped": []},
        "result_sha256": "a" * 64,
        "tool_result_sha256": "b" * 64,
        "provenance": {},
    }

    projected = [records._summary_result(record) for record in result_records]
    projected.append(records._compact_record(compact_record))
    encoded = json.dumps(projected, ensure_ascii=False)

    assert sentinel not in encoded
    assert projected[-1]["created_at"] == "legacy-unvalidated"
    assert records._is_sha256(projected[-1]["created_at_sha256"])
    assert projected[-1]["producer"]["name"] == "legacy-unvalidated"
    assert projected[-1]["producer"]["version"] == "legacy-unvalidated"


def test_compact_export_summarizes_large_provenance(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    large_text_size = 200_000
    meta = records.write_record(
        workspace=matter,
        tool_name="verify_quote",
        input_payload={"quote": "Q" * large_text_size},
        result={
            "status": "ok",
            "verdict": "not_found",
            "diff": ["D" * large_text_size],
        },
        provenance={
            "path": "P" * large_text_size,
            "anchors": [
                {
                    "change_unit_id": "cu_001",
                    "file_sha256": "a" * 64,
                    "clause_anchor": {"heading": "H" * large_text_size},
                }
            ],
        },
    )

    assert meta["record_status"] == "written"
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    assert journal.stat().st_size <= records.MAX_JOURNAL_LINE_BYTES + 1
    exported = server.export_decision_record(str(matter), max_records=1)
    encoded = json.dumps(exported, ensure_ascii=False)
    assert exported["total_count"] == 1
    assert len(exported["records"]) == 1
    assert len(encoded.encode("utf-8")) < 20_000
    assert "Q" * 100 not in encoded
    assert "D" * 100 not in encoded
    assert "P" * 100 not in encoded
    assert "H" * 100 not in encoded


def test_compact_export_validates_producer_build(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    server.list_rounds(str(matter))
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    record = json.loads(journal.read_text(encoding="utf-8"))
    sentinel = "PRIVATE_LEGACY_BUILD_SENTINEL_42"
    record["producer"]["build"] = sentinel
    journal.write_text(json.dumps(record) + "\n", encoding="utf-8")

    compact = records.read_records(str(matter), max_records=1, include_payload=False)
    encoded = json.dumps(compact, ensure_ascii=False)
    producer = compact["records"][0]["producer"]

    assert sentinel not in encoded
    assert producer["build"] == "legacy-unvalidated"
    assert records._is_sha256(producer["build_sha256"])
    full = records.read_records(str(matter), max_records=1, include_payload=True)
    assert full["records"][0]["producer"]["build"] == sentinel


@pytest.mark.parametrize(
    "build",
    [
        records.SOURCE_SNAPSHOT_UNAVAILABLE,
        f"{records.SOURCE_SNAPSHOT_PREFIX}{'a' * 64}",
    ],
)
def test_compact_export_preserves_known_producer_builds(build: str) -> None:
    producer = records._producer_summary(
        {"name": "veqtor-mcp", "version": "0.0.0", "build": build}
    )

    assert producer == {"name": "veqtor-mcp", "version": "0.0.0", "build": build}


@pytest.mark.parametrize(
    "build",
    [
        f"source-snapshot-sha256:{'a' * 64}",
        f"code-sha256:{'b' * 64}",
        f"{records.SOURCE_SNAPSHOT_PREFIX}not-a-sha256",
    ],
)
def test_compact_export_marks_old_producer_builds_as_legacy(build: str) -> None:
    producer = records._producer_summary(
        {"name": "veqtor-mcp", "version": "0.0.0", "build": build}
    )

    assert producer["build"] == "legacy-unvalidated"
    assert records._is_sha256(producer["build_sha256"])


def test_corrupt_journal_schema_is_rejected(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    (sidecar / records.JOURNAL_NAME).write_text("{}\n", encoding="utf-8")

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10)

    assert err.value.code == "journal_corrupt"


def test_result_digest_mismatch_is_rejected(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={"folder": str(matter)},
        result={"status": "ok"},
        provenance={},
    )
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    record = json.loads(journal.read_text(encoding="utf-8"))
    record["result"]["status"] = "mutated"
    journal.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10)

    assert err.value.code == "journal_corrupt"


def test_duplicate_record_ids_are_rejected(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={"folder": str(matter)},
        result={"status": "ok"},
        provenance={},
    )
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    first = journal.read_text(encoding="utf-8").splitlines()[0]
    journal.write_text(first + "\n" + first + "\n", encoding="utf-8")

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10)

    assert err.value.code == "journal_corrupt"


def test_invalid_utf8_journal_is_rejected(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    (sidecar / records.JOURNAL_NAME).write_bytes(b"\xff\n")

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10)

    assert err.value.code == "journal_corrupt"
    assert "invalid UTF-8" in str(err.value)


def test_empty_journal_accepts_first_lf_terminated_record(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    journal = sidecar / records.JOURNAL_NAME
    journal.write_bytes(b"")

    assert records.read_records(str(matter), max_records=10)["total_count"] == 0
    meta = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )

    assert meta == {"record_id": "dr_001", "record_status": "written"}
    assert journal.read_bytes().endswith(b"\n")
    assert records.read_records(str(matter), max_records=10)["total_count"] == 1


def test_unterminated_record_is_corrupt_and_blocks_append(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    first = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )
    assert first["record_status"] == "written"
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    journal.write_bytes(journal.read_bytes().removesuffix(b"\n"))
    before = journal.read_bytes()

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10)

    assert err.value.code == "journal_corrupt"
    assert "unterminated journal record" in str(err.value)
    result = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )
    assert result["record_status"] == "write_failed"
    assert result["record_id"] is None
    assert "journal_corrupt" in result["record_error"]
    assert "unterminated journal record" in result["record_error"]
    assert journal.read_bytes() == before


@pytest.mark.parametrize("payload", [b"\n", b" \t\n"], ids=["blank", "whitespace"])
def test_empty_lf_terminated_frames_are_corrupt(
    tmp_path: Path,
    payload: bytes,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    (sidecar / records.JOURNAL_NAME).write_bytes(payload)

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10)

    assert err.value.code == "journal_corrupt"


def test_blank_frame_after_valid_record_is_corrupt(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    assert records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )["record_status"] == "written"
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    journal.write_bytes(journal.read_bytes() + b"\n")

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10)

    assert err.value.code == "journal_corrupt"


def test_unterminated_whitespace_fragment_is_corrupt(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    (sidecar / records.JOURNAL_NAME).write_bytes(b" \t")

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10)

    assert err.value.code == "journal_corrupt"
    assert "unterminated journal record" in str(err.value)


def test_oversized_unterminated_fragment_reports_size_first(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    (sidecar / records.JOURNAL_NAME).write_bytes(
        b"x" * (records.MAX_JOURNAL_LINE_BYTES + 1)
    )

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10)

    assert err.value.code == "journal_corrupt"
    detail = str(err.value).rsplit(": ", 1)[-1]
    assert "journal record exceeds" in detail
    assert "unterminated" not in detail


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("input", "input missing"),
        ("result", "result missing"),
        ("provenance", "provenance missing"),
        ("tool_name", "invalid tool_name"),
    ],
)
def test_invalid_record_schema_fails_before_sidecar_and_recovers(
    tmp_path: Path,
    case: str,
    reason: str,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    kwargs = {
        "workspace": matter,
        "tool_name": "list_rounds",
        "input_payload": {},
        "result": {"status": "ok"},
        "provenance": {},
    }
    if case == "input":
        kwargs["input_payload"] = []
    elif case == "result":
        kwargs["result"] = []
    elif case == "provenance":
        kwargs["provenance"] = []
    else:
        kwargs["tool_name"] = 7

    failed = records.write_record(**kwargs)

    assert failed["record_status"] == "write_failed"
    assert failed["record_id"] is None
    assert "record_invalid" in failed["record_error"]
    assert reason in failed["record_error"]
    assert not (matter / records.SIDECAR_DIR).exists()

    recovered = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )
    assert recovered["record_status"] == "written"
    assert records.read_records(str(matter), max_records=10)["total_count"] == 1
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    before = journal.read_bytes()

    failed_again = records.write_record(**kwargs)

    assert failed_again["record_status"] == "write_failed"
    assert journal.read_bytes() == before
    assert records.read_records(str(matter), max_records=10)["total_count"] == 1


@pytest.mark.parametrize(
    ("tool_name", "record_type"),
    sorted(
        (tool_name, records.V1_HISTORICAL_TOOL_SPECS[tool_name].record_type)
        for tool_name in records.WRITABLE_TOOL_NAMES
    ),
)
def test_record_type_is_derived_for_every_registered_tool(
    tmp_path: Path,
    tool_name: str,
    record_type: str,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()

    meta = records.write_record(
        workspace=matter,
        tool_name=tool_name,
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )

    assert meta["record_status"] == "written"
    loaded = records.read_records(
        str(matter),
        max_records=10,
        include_payload=True,
        include_access_events=True,
    )
    assert loaded["records"][0]["tool_name"] == tool_name
    assert loaded["records"][0]["record_type"] == record_type


def test_write_record_has_no_record_type_override() -> None:
    assert "record_type" not in inspect.signature(records.write_record).parameters


def test_v1_historical_tool_specs_are_frozen_and_cover_writable_tools() -> None:
    expected = {
        "list_rounds": ("tool_observation.v1", "list_rounds"),
        "extract_redlines": ("tool_observation.v1", "extract_redlines"),
        "verify_quote": ("verification.v1", "verify_quote"),
        "apply_edits": ("decision.v1", "apply_edits"),
        "export_decision_record": (
            records.ACCESS_RECORD_TYPE,
            "export_decision_record",
        ),
    }
    actual = {
        tool_name: (spec.record_type, spec.projection_kind)
        for tool_name, spec in records.V1_HISTORICAL_TOOL_SPECS.items()
    }

    assert actual == expected
    assert records.WRITABLE_TOOL_NAMES <= records.V1_HISTORICAL_TOOL_SPECS.keys()


def test_v1_read_limits_are_not_narrowed() -> None:
    assert records.MAX_JOURNAL_LINE_BYTES >= 1_048_576
    assert records.MAX_JOURNAL_DEPTH >= 64
    assert records.MAX_JOURNAL_NODES >= 100_000
    assert records.MAX_JSON_INTEGER_DIGITS >= 128
    assert records.MAX_COMPACT_ID_LENGTH >= 32
    assert records.MAX_CANONICAL_JSON_NODES >= records.MAX_JOURNAL_NODES
    assert records.COMPACT_SAMPLE_LIMIT == 20


@pytest.mark.parametrize(
    ("payload", "expected_json", "expected_digest"),
    [
        (
            {
                "юрист": "Юрист 🧑‍⚖️📄",
                "a": [None, True, 0.1, 1e-7, {"z": 2, "b": 1}],
            },
            '{"a":[null,true,0.1,1e-07,{"b":1,"z":2}],'
            '"юрист":"Юрист 🧑‍⚖️📄"}',
            "6f405e23f9b6a8d1e1e1536a68438b4351dfdf26edc2f4685d602781b56f9eb0",
        ),
        (
            {"text": "é"},
            '{"text":"é"}',
            "42d3cbf59fdccced04e5dff14433fb52d34d58e385e9770ffd896ff517d63b92",
        ),
        (
            {"text": "e\u0301"},
            '{"text":"e\u0301"}',
            "9b53287cd41955684903378d2b1b4a3ddea9d80d67dcd026319a7c5a9a8a8b42",
        ),
        (
            {
                "\U00010000": "astral",
                "\ue000": "bmp-private-use",
                "é": "nfc-key",
                "e\u0301": "nfd-key",
                "controls": "\x00\n\"\\\b\f\r\t",
                "negative_zero": -0.0,
            },
            '{"controls":"\\u0000\\n\\"\\\\\\b\\f\\r\\t",'
            '"e\u0301":"nfd-key","negative_zero":-0.0,"é":"nfc-key",'
            '"\ue000":"bmp-private-use","\U00010000":"astral"}',
            "d2d566113618f299e9638c9b6ecdc13b2a29e3bc7adb9cf8993a95bb7bed42cf",
        ),
        (
            {"non_short_controls": "\x0b\x0e\x0f\x1a\x1b\x1e\x1f"},
            '{"non_short_controls":"\\u000b\\u000e\\u000f'
            '\\u001a\\u001b\\u001e\\u001f"}',
            "e7809d1f4b2bb2e50b32a947d4fca6753d869cf164157806f200d11e2f4d18a7",
        ),
    ],
    ids=[
        "sorted_unicode_nested_float",
        "nfc",
        "nfd",
        "signed_zero_controls_normalization_and_key_order",
        "non_short_control_lowercase_hex",
    ],
)
def test_v1_canonical_digest_vectors_are_frozen(
    payload: dict[str, object],
    expected_json: str,
    expected_digest: str,
) -> None:
    expected_bytes = expected_json.encode("utf-8")

    assert records._canonical_json_bytes(payload) == expected_bytes
    assert records._stable_digest(payload) == expected_digest
    assert hashlib.sha256(expected_bytes).hexdigest() == expected_digest


def test_retired_tool_history_remains_readable_but_is_not_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    assert records.write_record(
        workspace=matter,
        tool_name="verify_quote",
        input_payload={},
        result={"status": "ok", "verdict": "exact"},
        provenance={},
    )["record_status"] == "written"
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    before = journal.read_bytes()
    monkeypatch.setattr(
        records,
        "WRITABLE_TOOL_NAMES",
        records.WRITABLE_TOOL_NAMES - {"verify_quote"},
    )

    failed = records.write_record(
        workspace=matter,
        tool_name="verify_quote",
        input_payload={},
        result={"status": "ok", "verdict": "exact"},
        provenance={},
    )
    compact = records.read_records(
        str(matter),
        max_records=10,
        include_payload=False,
    )
    full = records.read_records(
        str(matter),
        max_records=10,
        include_payload=True,
    )

    assert failed["record_status"] == "write_failed"
    assert "record_invalid" in failed["record_error"]
    assert "invalid tool_name" in failed["record_error"]
    assert journal.read_bytes() == before
    assert compact["payloads"] == "compact"
    assert compact["records"][0]["tool_name"] == "verify_quote"
    assert compact["records"][0]["result"] == {
        "status": "ok",
        "verdict": "exact",
        "exact": None,
        "checked_anchor": None,
        "matches": {
            "count": 0,
            "sha256": (
                "4f53cda18c2baa0c0354bb5f9a3ecbe5"
                "ed12ab4d8e11ba873c2f11161202b945"
            ),
            "sample": [],
            "truncated": False,
        },
        "diff_count": 0,
    }
    assert compact["records"][0]["provenance"] == {}
    assert full["payloads"] == "full"
    assert full["records"][0]["record_type"] == "verification.v1"


def test_golden_v1_journal_stays_readable_and_appendable(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    fixture = Path(__file__).parent / "data" / "decision-records-v1-golden.jsonl"
    expected_projection = (
        Path(__file__).parent
        / "data"
        / "decision-records-v1-compact-golden.json"
    )
    journal = sidecar / records.JOURNAL_NAME
    fixture_bytes = fixture.read_bytes()
    stored_records = [
        json.loads(line)
        for line in fixture_bytes.decode("utf-8").splitlines()
    ]
    expected_compact_records = json.loads(
        expected_projection.read_text(encoding="utf-8")
    )
    journal.write_bytes(fixture_bytes)

    full = records.read_records(
        str(matter),
        max_records=10,
        include_payload=True,
        include_access_events=True,
    )
    compact = records.read_records(
        str(matter),
        max_records=10,
        include_payload=False,
    )
    compact_with_access = records.read_records(
        str(matter),
        max_records=10,
        include_payload=False,
        include_access_events=True,
    )

    assert full["payloads"] == "full"
    assert full["records"] == stored_records
    assert compact["payloads"] == "compact"
    assert compact["records"] == expected_compact_records[:-1]
    assert compact["total_count"] == 5
    assert compact["access_count"] == 1
    assert compact["truncated"] is False
    assert compact_with_access["payloads"] == "compact"
    assert compact_with_access["records"] == expected_compact_records
    assert compact_with_access["total_count"] == 6
    assert compact_with_access["access_count"] == 1
    assert compact_with_access["workspace"]["omitted"] is True
    assert records._is_sha256(compact_with_access["workspace"]["sha256"])
    encoded_compact = json.dumps(compact_with_access, ensure_ascii=False)
    for sentinel in (
        "PRIVATE_LIST_INPUT",
        "PRIVATE_SKIP",
        "PRIVATE_EXTRACT_INPUT",
        "PRIVATE_QUOTE",
        "PRIVATE_CLAUSE",
        "PRIVATE_EDIT",
        "PRIVATE_DELETE",
        "PRIVATE_ERROR_DETAIL",
        "PRIVATE_CLAIM",
        "legacy-build-PROTECTED",
        "Юрист",
        "⚖️📄",
        "Ошибка проверки: Юрист 🧑‍⚖️",
    ):
        assert sentinel not in encoded_compact
    appended = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )
    assert appended == {"record_id": "dr_007", "record_status": "written"}


def test_unknown_tool_is_refused_before_sidecar_creation(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()

    failed = records.write_record(
        workspace=matter,
        tool_name="probe",
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )

    assert failed["record_status"] == "write_failed"
    assert failed["record_id"] is None
    assert "record_invalid" in failed["record_error"]
    assert "invalid tool_name" in failed["record_error"]
    assert not (matter / records.SIDECAR_DIR).exists()


@pytest.mark.parametrize(
    ("seed_tool", "mutated_tool", "mutated_type", "reason"),
    [
        (
            "apply_edits",
            "apply_edits",
            records.ACCESS_RECORD_TYPE,
            "record_type does not match tool_name",
        ),
        (
            "export_decision_record",
            "export_decision_record",
            "tool_observation.v1",
            "record_type does not match tool_name",
        ),
        (
            "list_rounds",
            "probe",
            "tool_observation.v1",
            "invalid tool_name",
        ),
    ],
    ids=["substantive_as_access", "access_as_substantive", "unknown_tool"],
)
def test_semantically_invalid_tool_type_pair_is_corrupt_and_blocks_append(
    tmp_path: Path,
    seed_tool: str,
    mutated_tool: str,
    mutated_type: str,
    reason: str,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    assert records.write_record(
        workspace=matter,
        tool_name=seed_tool,
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )["record_status"] == "written"
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    stored = json.loads(journal.read_text(encoding="utf-8"))
    stored["tool_name"] = mutated_tool
    stored["record_type"] = mutated_type
    encoded = json.dumps(
        stored,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    journal.write_text(
        encoded + "\n",
        encoding="utf-8",
    )
    before = journal.read_bytes()

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10)

    assert err.value.code == "journal_corrupt"
    assert reason in str(err.value)
    failed = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )
    assert failed["record_status"] == "write_failed"
    assert "journal_corrupt" in failed["record_error"]
    assert reason in failed["record_error"]
    assert journal.read_bytes() == before


def test_record_id_capacity_refuses_append_without_poisoning_journal(
    tmp_path: Path,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    assert records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )["record_status"] == "written"
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    record = json.loads(journal.read_text(encoding="utf-8"))
    # This literal is the accepted v1 floor; do not derive it from the limit.
    maximum_id = "dr_" + "9" * 32
    record["record_id"] = maximum_id
    journal.write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert records.read_records(str(matter), max_records=10)["records"][0][
        "record_id"
    ] == maximum_id
    before = journal.read_bytes()

    failed = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )

    assert failed["record_status"] == "write_failed"
    assert failed["record_id"] is None
    assert "record_invalid" in failed["record_error"]
    assert "invalid record_id" in failed["record_error"]
    assert journal.read_bytes() == before
    loaded = records.read_records(str(matter), max_records=10)
    assert loaded["records"][0]["record_id"] == maximum_id


def test_every_written_record_is_immediately_readable_and_lf_framed(
    tmp_path: Path,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME

    for index in range(1, 4):
        meta = records.write_record(
            workspace=matter,
            tool_name="list_rounds",
            input_payload={"index": index},
            result={"status": "ok", "index": index},
            provenance={"index": index},
        )

        assert meta["record_status"] == "written"
        assert journal.read_bytes().endswith(b"\n")
        loaded = records.read_records(str(matter), max_records=10)
        assert loaded["total_count"] == index
        assert loaded["records"][-1]["record_id"] == meta["record_id"]


def test_append_commits_the_exact_frame_validated_after_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    result = {"status": "ok", "value": "before"}
    validated_frames: list[bytes] = []
    real_decode = records._decode_record_payload

    def mutate_source_after_frame_capture(raw: bytes):
        validated_frames.append(raw)
        if len(validated_frames) == 2:
            result["value"] = "after"
        return real_decode(raw)

    monkeypatch.setattr(
        records,
        "_decode_record_payload",
        mutate_source_after_frame_capture,
    )

    meta = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result=result,
        provenance={},
    )

    assert meta == {"record_id": "dr_001", "record_status": "written"}
    assert result["value"] == "after"
    assert len(validated_frames) == 2
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    assert journal.read_bytes() == validated_frames[-1] + b"\n"
    loaded = records.read_records(str(matter), max_records=10)
    stored = loaded["records"][0]
    assert stored["result"]["value"] == "before"
    assert stored["result_sha256"] == records._stable_digest(stored["result"])


def test_inconsistent_final_snapshot_is_refused_without_journal_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    assert records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )["record_status"] == "written"
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    before = journal.read_bytes()
    result = {"status": "ok", "value": "before"}
    real_encode = records._journal_json_bytes
    encode_calls = 0

    def mutate_before_final_encode(record):
        nonlocal encode_calls
        encode_calls += 1
        if encode_calls == 2:
            result["value"] = "after"
        return real_encode(record)

    monkeypatch.setattr(records, "_journal_json_bytes", mutate_before_final_encode)

    failed = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result=result,
        provenance={},
    )

    assert failed["record_status"] == "write_failed"
    assert failed["record_id"] is None
    assert "record_invalid" in failed["record_error"]
    assert "result_sha256 mismatch" in failed["record_error"]
    assert journal.read_bytes() == before
    assert records.read_records(str(matter), max_records=10)["total_count"] == 1


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (
            b"[" * 10_000 + b"0" + b"]" * 10_000,
            "JSON decoder rejected input",
        ),
        (b"1" * 5_000, "JSON integer exceeds"),
        (
            b"[" * (records.MAX_JOURNAL_DEPTH + 1)
            + b"0"
            + b"]" * (records.MAX_JOURNAL_DEPTH + 1),
            "maximum depth",
        ),
        (
            b"["
            + b",".join([b"0"] * records.MAX_JOURNAL_NODES)
            + b"]",
            "maximum node count",
        ),
        (b'{"value":1,"value":2}', "duplicate JSON object key"),
        (b"NaN", "non-finite JSON number"),
    ],
    ids=[
        "decoder_recursion",
        "oversized_integer",
        "depth_limit",
        "node_limit",
        "duplicate_key",
        "non_finite_number",
    ],
)
def test_bounded_json_failures_are_classified_and_block_append(
    tmp_path: Path,
    payload: bytes,
    reason: str,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    journal = sidecar / records.JOURNAL_NAME
    journal.write_bytes(payload + b"\n")
    before = journal.read_bytes()

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10)

    assert err.value.code == "journal_corrupt"
    assert reason in str(err.value)
    result = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )
    assert result["record_status"] == "write_failed"
    assert result["record_id"] is None
    assert "journal_corrupt" in result["record_error"]
    assert reason in result["record_error"]
    assert journal.read_bytes() == before


def test_oversized_journal_line_is_classified_and_blocks_append(
    tmp_path: Path,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    journal = sidecar / records.JOURNAL_NAME
    journal.write_bytes(b'"' + b"x" * records.MAX_JOURNAL_LINE_BYTES + b'"\n')
    before = journal.read_bytes()

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10)

    assert err.value.code == "journal_corrupt"
    assert "journal record exceeds" in str(err.value)
    result = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )
    assert result["record_status"] == "write_failed"
    assert "journal_corrupt" in result["record_error"]
    assert "journal record exceeds" in result["record_error"]
    assert journal.read_bytes() == before


@pytest.mark.parametrize(
    "location",
    [
        "result",
        "tool_result",
        "input",
        "provenance",
        "input_key",
        "deep_input",
    ],
)
def test_invalid_unicode_new_record_is_a_symmetric_best_effort_failure(
    tmp_path: Path,
    location: str,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    surrogate = "\udcff"
    kwargs = {
        "workspace": matter,
        "tool_name": "list_rounds",
        "input_payload": {},
        "result": {"status": "ok"},
        "tool_result": None,
        "provenance": {},
    }
    if location == "result":
        kwargs["result"] = {"status": "ok", "value": surrogate}
    elif location == "tool_result":
        kwargs["tool_result"] = {"status": "ok", "value": surrogate}
    elif location == "input":
        kwargs["input_payload"] = {"value": surrogate}
    elif location == "provenance":
        kwargs["provenance"] = {"value": surrogate}
    elif location == "input_key":
        kwargs["input_payload"] = {surrogate: "value"}
    else:
        kwargs["input_payload"] = {"nested": {"items": [{"value": surrogate}]}}

    result = records.write_record(**kwargs)

    assert result["record_status"] == "write_failed"
    assert result["record_id"] is None
    assert "record_invalid" in result["record_error"]
    assert "invalid Unicode scalar value" in result["record_error"]
    assert surrogate not in result["record_error"]
    assert not (matter / records.SIDECAR_DIR).exists()


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("cycle", "cyclic JSON value"),
        ("non_finite", "non-finite JSON number"),
        ("unsupported", "unsupported JSON value type"),
    ],
)
def test_invalid_new_json_values_fail_before_sidecar_creation(
    tmp_path: Path,
    kind: str,
    reason: str,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    value: object
    if kind == "cycle":
        cycle: list[object] = []
        cycle.append(cycle)
        value = cycle
    elif kind == "non_finite":
        value = float("nan")
    else:
        value = object()

    result = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={"value": value},
        result={"status": "ok"},
        provenance={},
    )

    assert result["record_status"] == "write_failed"
    assert "record_invalid" in result["record_error"]
    assert reason in result["record_error"]
    assert not (matter / records.SIDECAR_DIR).exists()


def test_oversized_new_record_fails_before_sidecar_creation(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()

    result = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={"value": "x" * records.MAX_JOURNAL_LINE_BYTES},
        result={"status": "ok"},
        provenance={},
    )

    assert result["record_status"] == "write_failed"
    assert "record_invalid" in result["record_error"]
    assert "journal record exceeds" in result["record_error"]
    assert not (matter / records.SIDECAR_DIR).exists()


def test_large_tool_result_digest_is_not_limited_by_journal_line_cap(
    tmp_path: Path,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    tool_result = {"payload": "x" * (records.MAX_JOURNAL_LINE_BYTES + 1)}

    result = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok"},
        tool_result=tool_result,
        provenance={},
    )

    assert result["record_status"] == "written"
    stored = records.read_records(str(matter), max_records=1)["records"][0]
    assert stored["tool_result_sha256"] == records._stable_digest(tool_result)


def test_explicit_empty_tool_result_is_digested_as_provided(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()

    result = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok"},
        tool_result={},
        provenance={},
    )

    assert result["record_status"] == "written"
    stored = records.read_records(str(matter), max_records=1)["records"][0]
    assert stored["tool_result_sha256"] == records._stable_digest({})
    assert stored["tool_result_sha256"] != stored["result_sha256"]


def test_bounded_json_accepts_exact_depth_node_integer_and_line_limits() -> None:
    nested: object = None
    for _ in range(records.MAX_JOURNAL_DEPTH):
        nested = [nested]
    records._validate_json_value(nested, max_nodes=records.MAX_JOURNAL_NODES)
    with pytest.raises(records._JsonBoundaryError, match="maximum depth"):
        records._validate_json_value(
            [nested],
            max_nodes=records.MAX_JOURNAL_NODES,
        )

    nodes = [None] * (records.MAX_JOURNAL_NODES - 1)
    records._validate_json_value(nodes, max_nodes=records.MAX_JOURNAL_NODES)
    with pytest.raises(records._JsonBoundaryError, match="maximum node count"):
        records._validate_json_value(
            nodes + [None],
            max_nodes=records.MAX_JOURNAL_NODES,
        )

    assert records._parse_json_int("9" * records.MAX_JSON_INTEGER_DIGITS)
    with pytest.raises(records._JsonBoundaryError, match="JSON integer exceeds"):
        records._parse_json_int("9" * (records.MAX_JSON_INTEGER_DIGITS + 1))

    line = {"padding": ""}
    base_size = len(records._journal_json_bytes(line))
    line["padding"] = "x" * (records.MAX_JOURNAL_LINE_BYTES - base_size)
    assert len(records._journal_json_bytes(line)) == records.MAX_JOURNAL_LINE_BYTES
    line["padding"] += "x"
    with pytest.raises(records._JsonBoundaryError, match="journal record exceeds"):
        records._journal_json_bytes(line)


@pytest.mark.parametrize(
    "location",
    [
        "producer_build",
        "workspace",
        "input",
        "result",
        "input_key",
        "deep_input",
    ],
)
def test_json_escaped_surrogate_is_rejected_anywhere_in_record(
    tmp_path: Path, location: str
) -> None:
    matter = _matter(tmp_path)
    assert server.list_rounds(str(matter))["record_status"] == "written"
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    record = json.loads(journal.read_text(encoding="utf-8"))
    surrogate = "\udcff"
    if location == "producer_build":
        record["producer"]["build"] = surrogate
    elif location == "workspace":
        record["workspace"] = surrogate
    elif location == "input":
        record["input"]["value"] = surrogate
    elif location == "result":
        record["result"]["value"] = surrogate
    elif location == "input_key":
        record["input"][surrogate] = "value"
    else:
        record["input"]["nested"] = {"items": [{"value": surrogate}]}
    journal.write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert journal.read_bytes().isascii()

    with pytest.raises(records.DecisionRecordError) as err:
        records.read_records(str(matter), max_records=10, include_payload=True)

    assert err.value.code == "journal_corrupt"
    assert "invalid Unicode scalar value" in str(err.value)
    assert surrogate not in str(err.value)


def test_valid_non_ascii_journal_strings_are_accepted(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    assert server.list_rounds(str(matter))["record_status"] == "written"
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    record = json.loads(journal.read_text(encoding="utf-8"))
    record["input"]["unicode"] = {"ключ": ["Юрист", "⚖️"]}
    journal.write_text(
        json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    loaded = records.read_records(str(matter), max_records=10, include_payload=True)

    assert loaded["records"][0]["input"]["unicode"] == {
        "ключ": ["Юрист", "⚖️"]
    }


def test_valid_escaped_surrogate_pair_decodes_to_emoji(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    assert server.list_rounds(str(matter))["record_status"] == "written"
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    record = json.loads(journal.read_text(encoding="utf-8"))
    record["input"]["emoji"] = "😀"
    journal.write_text(json.dumps(record, ensure_ascii=True) + "\n", encoding="utf-8")
    assert b"\\ud83d\\ude00" in journal.read_bytes()

    loaded = records.read_records(str(matter), max_records=10, include_payload=True)

    assert loaded["records"][0]["input"]["emoji"] == "😀"


def test_surrogate_corrupt_journal_blocks_further_append(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    assert server.list_rounds(str(matter))["record_status"] == "written"
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    record = json.loads(journal.read_text(encoding="utf-8"))
    record["producer"]["build"] = "\udcff"
    journal.write_text(json.dumps(record) + "\n", encoding="utf-8")
    before = journal.read_bytes()

    result = server.list_rounds(str(matter))

    assert result["record_status"] == "write_failed"
    assert result["record_id"] is None
    assert "journal_corrupt" in result["record_error"]
    assert "invalid Unicode scalar value" in result["record_error"]
    assert journal.read_bytes() == before


def test_concurrent_appends_are_locked_and_ids_are_unique(tmp_path: Path) -> None:
    workspace = tmp_path / "matter"
    workspace.mkdir()

    with ProcessPoolExecutor(max_workers=4) as pool:
        metas = list(
            pool.map(
                _write_concurrent_record,
                [str(workspace)] * 12,
                range(12),
            )
        )

    assert all(meta["record_status"] == "written" for meta in metas)
    exported = records.read_records(str(workspace), max_records=20)
    assert exported["total_count"] == 12
    assert [record["record_id"] for record in exported["records"]] == [
        f"dr_{index:03d}" for index in range(1, 13)
    ]
    assert sorted(record["input"]["index"] for record in exported["records"]) == list(
        range(12)
    )


def test_threaded_cold_start_appends_do_not_drop_records(tmp_path: Path) -> None:
    workspace = tmp_path / "matter"
    workspace.mkdir()

    with ThreadPoolExecutor(max_workers=48) as pool:
        metas = list(
            pool.map(
                _write_concurrent_record,
                [str(workspace)] * 48,
                range(48),
            )
        )

    assert all(meta["record_status"] == "written" for meta in metas)
    exported = records.read_records(str(workspace), max_records=50)
    assert exported["total_count"] == 48
    assert len({record["record_id"] for record in exported["records"]}) == 48


def test_cold_start_concurrent_appends_do_not_drop_records(tmp_path: Path) -> None:
    for iteration in range(40):
        workspace = tmp_path / f"matter-{iteration}"
        workspace.mkdir()

        with ProcessPoolExecutor(max_workers=4) as pool:
            metas = list(
                pool.map(
                    _write_concurrent_record,
                    [str(workspace)] * 12,
                    range(12),
                )
            )

        assert all(meta["record_status"] == "written" for meta in metas), iteration
        exported = records.read_records(str(workspace), max_records=20)
        assert exported["total_count"] == 12, iteration
        assert len({record["record_id"] for record in exported["records"]}) == 12
