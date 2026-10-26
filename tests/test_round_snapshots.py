# SPDX-License-Identifier: Apache-2.0
"""Exact snapshots use synthetic packages only, including fault/race probes."""

import dataclasses
import errno
import hashlib
import io
import json
import os
import stat
import zipfile

import pytest

from veqtor_docx import round_snapshots as snapshots
from veqtor_docx._ooxml import DocxError


def _package(text="Original", extra=None):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                         '<w:body><w:p><w:r><w:t>' + text + '</w:t></w:r></w:p></w:body></w:document>')
        for name, payload in (extra or {}).items():
            archive.writestr(name, payload)
    return data.getvalue()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "private-source-name.docx"
    path.write_bytes(_package())
    return path


def _capture(source):
    return snapshots.capture_document_snapshot(source)


def _persist(source, path):
    return snapshots.persist_snapshot(_capture(source), path)


def test_restore_exact_bytes_outside_snapshot_and_refuse_existing_or_wrong_hash(source, tmp_path):
    directory = tmp_path / 'snapshot'
    snap = _capture(source)
    snapshots.persist_snapshot(snap, directory)
    source.unlink()
    original = {p.name: p.read_bytes() for p in directory.iterdir()}
    output = tmp_path / 'restored.docx'
    with pytest.raises(snapshots.SnapshotError, match='file_sha256_mismatch'):
        snapshots.restore_snapshot_copy(directory, output, expected_sha256='0' * 64)
    assert not output.exists()
    result = snapshots.restore_snapshot_copy(directory, output, expected_sha256=snap.sha256)
    assert result.sha256 == snap.sha256 and output.read_bytes() == snap.payload
    assert stat.S_IMODE(output.stat().st_mode) == 0o600 and output.stat().st_nlink == 1
    with pytest.raises(snapshots.SnapshotError, match='output_exists'):
        snapshots.restore_snapshot_copy(directory, output, expected_sha256=snap.sha256)
    with pytest.raises(snapshots.SnapshotError, match='protected_storage_path'):
        snapshots.restore_snapshot_copy(directory, directory / 'new.docx', expected_sha256=snap.sha256)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == original


@pytest.mark.parametrize('fault', ['before_rename', 'after_rename', 'foreign_stage'])
def test_restore_failure_reports_publication_and_keeps_foreign_data(source, tmp_path, monkeypatch, fault):
    directory = tmp_path / 'snapshot'
    snap = _capture(source)
    snapshots.persist_snapshot(snap, directory)
    output = tmp_path / 'restored.docx'
    rename = snapshots._rename_noreplace

    def fail(fd, stage, name):
        if fault == 'after_rename':
            rename(fd, stage, name)
        elif fault == 'foreign_stage':
            os.rename(stage, 'owned-moved', src_dir_fd=fd, dst_dir_fd=fd)
            (tmp_path / stage).write_bytes(b'foreign bytes')
        raise OSError('injected failure')

    monkeypatch.setattr(snapshots, '_rename_noreplace', fail)
    with pytest.raises(snapshots.SnapshotError) as caught:
        snapshots.restore_snapshot_copy(directory, output, expected_sha256=snap.sha256)
    assert caught.value.metadata['publication_completed'] == {
        'before_rename': False, 'after_rename': True, 'foreign_stage': None}[fault]
    assert output.exists() == (fault == 'after_rename')
    if output.exists():
        assert output.read_bytes() == snap.payload
        assert caught.value.metadata['output_path_verified'] is True
    if fault == 'foreign_stage':
        assert next(tmp_path.glob('.veqtor-restore-*')).read_bytes() == b'foreign bytes'
        assert (tmp_path / 'owned-moved').read_bytes() == snap.payload
    assert snapshots.load_snapshot(directory) == snap


def test_capture_is_frozen_exact_bytes_not_live_source(source):
    original = source.read_bytes()
    result = snapshots.capture_document_snapshot(source, hashlib.sha256(original).hexdigest())
    source.write_bytes(_package("Changed later"))
    assert result.payload == original
    assert result.sha256 == hashlib.sha256(original).hexdigest()
    assert result.size_bytes == len(original)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.payload = b"changed"
    assert set(dataclasses.asdict(result)) == {"payload", "sha256", "size_bytes"}


@pytest.mark.parametrize("expected", ["", "A" * 64, "0" * 63, 123, False])
def test_invalid_expected_hash_refused_before_read(source, expected, monkeypatch):
    monkeypatch.setattr(snapshots, "_read_file", lambda *a, **k: pytest.fail("source was read"))
    with pytest.raises(snapshots.SnapshotError, match="invalid_sha256"):
        snapshots.capture_document_snapshot(source, expected)


def test_wrong_expected_hash_refuses(source):
    with pytest.raises(snapshots.SnapshotError, match="file_sha256_mismatch"):
        snapshots.capture_document_snapshot(source, "0" * 64)


@pytest.mark.parametrize("kind", ["leaf_symlink", "parent_symlink", "directory", "fifo"])
def test_capture_refuses_symlinks_and_non_regular(source, tmp_path, kind):
    original = source.read_bytes()
    candidate = tmp_path / "candidate.docx"
    if kind == "leaf_symlink":
        candidate.symlink_to(source)
    elif kind == "parent_symlink":
        candidate.symlink_to(tmp_path, target_is_directory=True)
        candidate = candidate / source.name
    elif kind == "directory":
        candidate.mkdir()
    else:
        os.mkfifo(candidate)
    with pytest.raises(snapshots.SnapshotError):
        snapshots.capture_document_snapshot(candidate)
    assert source.read_bytes() == original


def test_existing_hardlinked_source_is_readable(source, tmp_path):
    alias = tmp_path / "alias.docx"
    os.link(source, alias)
    assert _capture(alias) == _capture(source)


def test_capture_refuses_observed_in_place_drift(source, monkeypatch):
    real_read = os.read
    changed = False

    def read(fd, count):
        nonlocal changed
        data = real_read(fd, count)
        if data and not changed:
            changed = True
            source.write_bytes(_package("External change"))
        return data

    monkeypatch.setattr(snapshots.os, "read", read)
    with pytest.raises(snapshots.SnapshotError, match="source_changed"):
        _capture(source)


def test_capture_refuses_replaced_leaf(source, tmp_path, monkeypatch):
    real_read = os.read
    replacement = tmp_path / "replacement.docx"
    replacement.write_bytes(source.read_bytes())
    changed = False

    def read(fd, count):
        nonlocal changed
        data = real_read(fd, count)
        if data and not changed:
            changed = True
            os.replace(replacement, source)
        return data

    monkeypatch.setattr(snapshots.os, "read", read)
    with pytest.raises(snapshots.SnapshotError, match="source_changed"):
        _capture(source)


@pytest.mark.parametrize("payload", [b"not ZIP", _package(extra={"word/unread.xml": "<broken>"}),
                                     _package(extra={"_rels/.rels": "<broken>"})])
def test_all_zip_and_xml_members_are_validated(source, payload):
    source.write_bytes(payload)
    with pytest.raises(DocxError):
        _capture(source)


def test_source_bound_is_checked_before_materializing(source, monkeypatch):
    monkeypatch.setattr(snapshots, "MAX_DOCX_INPUT_BYTES", 10)
    monkeypatch.setattr(snapshots.os, "read", lambda *a: pytest.fail("oversize file read"))
    with pytest.raises(DocxError, match="snapshot_input_bytes"):
        _capture(source)


def test_persistence_exact_private_closed_manifest_and_load(source, tmp_path):
    captured = _capture(source)
    source.write_bytes(_package("Source changed after capture"))
    destination = tmp_path / "chosen-snapshot"
    result = snapshots.persist_snapshot(captured, destination)
    assert result == snapshots.SnapshotPublication(str(destination), captured.sha256, captured.size_bytes)
    assert (destination / "source.docx").read_bytes() == captured.payload
    manifest = json.loads((destination / "manifest.json").read_bytes())
    assert manifest == {"schema_version": "round_snapshot.v1", "sha256": captured.sha256,
                        "size_bytes": captured.size_bytes}
    assert source.name not in (destination / "manifest.json").read_text()
    assert snapshots.load_snapshot(destination) == captured
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    for child in destination.iterdir():
        assert stat.S_IMODE(child.stat().st_mode) == 0o600
        assert child.stat().st_nlink == 1
    assert not list(tmp_path.glob(".veqtor-snapshot-*"))


@pytest.mark.parametrize("kind", ["same_snapshot", "other_directory", "file", "symlink", "dangling"])
def test_create_only_never_overwrites_existing_destination(source, tmp_path, kind):
    destination = tmp_path / "snapshot"
    if kind == "same_snapshot":
        _persist(source, destination)
    elif kind == "other_directory":
        destination.mkdir()
        (destination / "foreign").write_text("keep")
    elif kind == "file":
        destination.write_text("keep")
    elif kind == "symlink":
        destination.symlink_to(source)
    else:
        destination.symlink_to(tmp_path / "absent")
    before = os.lstat(destination)
    with pytest.raises(snapshots.SnapshotError, match="snapshot_exists"):
        _persist(source, destination)
    after = os.lstat(destination)
    assert (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    assert not list(tmp_path.glob(".veqtor-snapshot-*"))


def test_racing_foreign_final_is_preserved(source, tmp_path, monkeypatch):
    destination = tmp_path / "snapshot"
    real_rename = snapshots._rename_noreplace

    def race(parent, stage, name):
        destination.mkdir()
        (destination / "foreign").write_text("keep")
        real_rename(parent, stage, name)

    monkeypatch.setattr(snapshots, "_rename_noreplace", race)
    with pytest.raises(snapshots.SnapshotError) as raised:
        _persist(source, destination)
    assert raised.value.metadata["publication_completed"] is False
    assert (destination / "foreign").read_text() == "keep"
    assert not list(tmp_path.glob(".veqtor-snapshot-*"))


@pytest.mark.parametrize("fault", ["write", "stage_fsync", "rename", "parent_fsync", "after_rename"])
def test_publication_faults_report_actual_boundary(source, tmp_path, monkeypatch, fault):
    destination = tmp_path / "snapshot"
    if fault == "write":
        monkeypatch.setattr(snapshots, "_write_all", lambda *a: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full")))
    elif fault in {"stage_fsync", "parent_fsync"}:
        real_fsync = os.fsync

        def fsync(fd):
            info = os.fstat(fd)
            target = (os.stat(tmp_path).st_ino == info.st_ino) if fault == "parent_fsync" else (
                stat.S_ISDIR(info.st_mode) and os.stat(tmp_path).st_ino != info.st_ino)
            if target:
                raise OSError(errno.EIO, "injected fsync failure")
            return real_fsync(fd)

        monkeypatch.setattr(snapshots.os, "fsync", fsync)
    else:
        real_rename = snapshots._rename_noreplace

        def rename(*a):
            if fault == "after_rename":
                real_rename(*a)
            raise OSError(errno.EIO, "injected rename failure")

        monkeypatch.setattr(snapshots, "_rename_noreplace", rename)
    with pytest.raises(snapshots.SnapshotError) as raised:
        _persist(source, destination)
    expected_published = fault in {"parent_fsync", "after_rename"}
    assert raised.value.metadata["publication_completed"] is expected_published
    assert destination.exists() is expected_published
    if expected_published:
        assert snapshots.load_snapshot(destination) == _capture(source)
    assert not list(tmp_path.glob(".veqtor-snapshot-*"))


@pytest.mark.parametrize("damage", ["extra", "missing", "hash", "size_bool", "unknown_field", "duplicate_field", "bad_schema",
                                    "source_symlink", "manifest_symlink", "source_hardlink", "public_file", "public_directory"])
def test_load_refuses_corrupt_or_unsafe_store(source, tmp_path, damage):
    destination = tmp_path / "snapshot"
    _persist(source, destination)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    if damage == "extra":
        (destination / "extra").write_text("foreign")
    elif damage == "missing":
        (destination / "source.docx").unlink()
    elif damage in {"source_symlink", "manifest_symlink"}:
        child = destination / ("source.docx" if damage == "source_symlink" else "manifest.json")
        child.unlink()
        child.symlink_to(source)
    elif damage == "source_hardlink":
        os.link(destination / "source.docx", tmp_path / "alias")
    elif damage == "public_file":
        (destination / "source.docx").chmod(0o644)
    elif damage == "public_directory":
        destination.chmod(0o755)
    elif damage == "duplicate_field":
        manifest_path.write_text('{"schema_version":"round_snapshot.v1","schema_version":"round_snapshot.v1"}')
    else:
        if damage == "hash":
            manifest["sha256"] = "0" * 64
        elif damage == "size_bool":
            manifest["size_bytes"] = True
        elif damage == "unknown_field":
            manifest["original_path"] = str(source)
        else:
            manifest["schema_version"] = "round_snapshot.v2"
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(DocxError):
        snapshots.load_snapshot(destination)


def test_dataclass_constructor_cannot_bypass_zip_validation(tmp_path):
    payload = b"not a DOCX"
    captured = snapshots.DocumentSnapshot(payload, hashlib.sha256(payload).hexdigest(), len(payload))
    with pytest.raises(DocxError):
        snapshots.persist_snapshot(captured, tmp_path / "snapshot")
    assert list(tmp_path.iterdir()) == []


def test_valid_replacement_of_both_staged_files_cannot_change_snapshot(source, tmp_path, monkeypatch):
    destination = tmp_path / "snapshot"
    real_load = snapshots._load_directory
    other_payload = _package("Different valid bytes")
    other = snapshots.DocumentSnapshot(other_payload, hashlib.sha256(other_payload).hexdigest(), len(other_payload))
    changed = False

    def load(fd):
        nonlocal changed
        if not changed:
            changed = True
            stage = next(tmp_path.glob(".veqtor-snapshot-*"))
            (stage / "source.docx").write_bytes(other.payload)
            (stage / "manifest.json").write_bytes(snapshots._manifest(other))
        return real_load(fd)

    monkeypatch.setattr(snapshots, "_load_directory", load)
    with pytest.raises(snapshots.SnapshotError, match="source_changed"):
        _persist(source, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".veqtor-snapshot-*"))


def test_cleanup_keeps_replaced_foreign_file_in_own_stage(source, tmp_path, monkeypatch):
    real_write = snapshots._write_all
    replacement = tmp_path / "foreign"
    replacement.write_bytes(b"foreign data must survive")
    called = False

    def write(fd, data):
        nonlocal called
        real_write(fd, data)
        if not called:
            called = True
            stage = next(tmp_path.glob(".veqtor-snapshot-*"))
            os.replace(replacement, stage / "source.docx")

    monkeypatch.setattr(snapshots, "_write_all", write)
    with pytest.raises(snapshots.SnapshotError):
        _persist(source, tmp_path / "snapshot")
    stages = list(tmp_path.glob(".veqtor-snapshot-*"))
    assert len(stages) == 1
    assert (stages[0] / "source.docx").read_bytes() == b"foreign data must survive"
    assert list(stages[0].iterdir()) == [stages[0] / "source.docx"]
    assert not (tmp_path / "snapshot").exists()


def test_parent_swap_cannot_publish_into_replacement_directory(source, tmp_path, monkeypatch):
    parent = tmp_path / "evidence"
    parent.mkdir()
    moved = tmp_path / "moved-evidence"
    real_write = snapshots._write_all
    changed = False

    def write(fd, data):
        nonlocal changed
        real_write(fd, data)
        if not changed:
            changed = True
            parent.rename(moved)
            parent.mkdir()
            (parent / "foreign").write_text("keep")

    monkeypatch.setattr(snapshots, "_write_all", write)
    with pytest.raises(snapshots.SnapshotError, match="source_changed") as raised:
        _persist(source, parent / "snapshot")
    assert raised.value.metadata["publication_completed"] is False
    assert list(moved.iterdir()) == []
    assert list(parent.iterdir()) == [parent / "foreign"]
    assert (parent / "foreign").read_text() == "keep"


def test_unsupported_filesystem_refuses_without_staging(source, tmp_path, monkeypatch):
    captured = _capture(source)

    def unsupported(*args):
        raise snapshots.SnapshotError("unsupported_filesystem", "test refusal")

    monkeypatch.setattr(snapshots, "_supported_filesystem", unsupported)
    with pytest.raises(snapshots.SnapshotError, match="unsupported_filesystem"):
        snapshots.persist_snapshot(captured, tmp_path / "snapshot")
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize("payload", [b"[" * 1100 + b"]" * 1100,
                                     '{"schema_version":"round_snapshot.v1"}'.encode("utf-16")])
def test_manifest_parser_refuses_unsupported_encoding_and_depth(source, tmp_path, payload):
    destination = tmp_path / "snapshot"
    _persist(source, destination)
    (destination / "manifest.json").write_bytes(payload)
    with pytest.raises(snapshots.SnapshotError, match="invalid_manifest"):
        snapshots.load_snapshot(destination)


def test_uppercase_xml_and_doctype_are_validated(source):
    for extra in ({"custom/UNREAD.XML": b"<broken>"},
                  {"word/extra.xml": b'<!DOCTYPE a [<!ENTITY x "payload">]><a>&x;</a>'}):
        source.write_bytes(_package(extra=extra))
        with pytest.raises(DocxError):
            _capture(source)


def test_private_store_cannot_be_loaded_through_directory_symlink(source, tmp_path):
    destination = tmp_path / "snapshot"
    _persist(source, destination)
    alias = tmp_path / "alias"
    alias.symlink_to(destination, target_is_directory=True)
    with pytest.raises(snapshots.SnapshotError):
        snapshots.load_snapshot(alias)


def test_source_tree_mutation_after_first_read_is_detected(source, tmp_path, monkeypatch):
    real_read = snapshots._read_file
    parent = tmp_path / "parent"
    parent.mkdir()
    nested = parent / "source.docx"
    nested.write_bytes(source.read_bytes())
    moved = tmp_path / "moved"

    def read(*args, **kwargs):
        data = real_read(*args, **kwargs)
        parent.rename(moved)
        parent.mkdir()
        (parent / "source.docx").write_bytes(source.read_bytes())
        return data

    monkeypatch.setattr(snapshots, "_read_file", read)
    with pytest.raises(snapshots.SnapshotError, match="source_changed"):
        _capture(nested)


def test_parent_traversal_is_not_normalized_across_a_symlink(source, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(snapshots.SnapshotError, match="invalid_path"):
        snapshots.capture_document_snapshot(str(alias / ".." / source.name))


def test_parent_permission_drift_refuses_before_publish(source, tmp_path, monkeypatch):
    parent = tmp_path / "evidence"
    parent.mkdir()
    real_write = snapshots._write_all

    def write(fd, data):
        real_write(fd, data)
        parent.chmod(0o777)

    monkeypatch.setattr(snapshots, "_write_all", write)
    with pytest.raises(snapshots.SnapshotError, match="unsafe_path") as raised:
        _persist(source, parent / "snapshot")
    assert raised.value.metadata["publication_completed"] is False
    assert not list(parent.iterdir())


def test_late_parent_move_reports_published_but_unverified_path(source, tmp_path, monkeypatch):
    parent = tmp_path / "evidence"
    parent.mkdir()
    moved = tmp_path / "moved"
    real_rename = snapshots._rename_noreplace

    def rename(*args):
        real_rename(*args)
        parent.rename(moved)
        parent.mkdir()
        (parent / "foreign").write_text("keep")

    monkeypatch.setattr(snapshots, "_rename_noreplace", rename)
    with pytest.raises(snapshots.SnapshotError, match="publication_uncertain") as raised:
        _persist(source, parent / "snapshot")
    assert raised.value.metadata["publication_completed"] is True
    assert raised.value.metadata["output_path_verified"] is False
    assert snapshots.load_snapshot(moved / "snapshot") == _capture(source)
    assert (parent / "foreign").read_text() == "keep"
