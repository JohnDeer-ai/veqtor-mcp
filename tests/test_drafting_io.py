# SPDX-License-Identifier: Apache-2.0
"""Publication safety for user-owned, repeatedly edited documents."""

import errno
import hashlib
import os
import plistlib
import stat
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from veqtor_docx import ApplyError
from veqtor_docx import _drafting_io as publishing


SOURCE = b"the exact, validated source package\x00\xff"
CANDIDATE = b"the fully validated edited package\x00\xfe"


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "Agreement.docx"
    path.write_bytes(SOURCE)
    path.chmod(0o640)
    return path


def _save(source: Path, output: Path | None = None) -> dict:
    return publishing.publish_document(str(source), str(output) if output is not None else None,
                                       SOURCE, CANDIDATE)


def _no_temporary_files(folder: Path) -> None:
    assert not list(folder.rglob(".veqtor-*.tmp"))
    assert not list(folder.glob(".veqtor-*.lock"))


@pytest.mark.parametrize("explicit", [False, True])
def test_in_place_saves_exact_backup_and_preserves_mode(source: Path, explicit: bool):
    result = _save(source, source if explicit else None)
    assert result == {
        "output_path": str(source), "backup_path": result["backup_path"],
        "backup_sha256": hashlib.sha256(SOURCE).hexdigest(), "save_mode": "in_place",
    }
    backup = Path(result["backup_path"])
    assert backup.parent == source.parent / ".veqtor-backups"
    assert backup.read_bytes() == SOURCE
    assert source.read_bytes() == CANDIDATE
    assert stat.S_IMODE(source.stat().st_mode) == 0o640
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
    assert source.stat().st_nlink == backup.stat().st_nlink == 1
    _no_temporary_files(source.parent)


def test_repeated_saves_keep_distinct_backups(source: Path):
    first = _save(source)
    second = publishing.publish_document(str(source), None, CANDIDATE, b"third version")
    assert first["backup_path"] != second["backup_path"]
    assert Path(first["backup_path"]).read_bytes() == SOURCE
    assert Path(second["backup_path"]).read_bytes() == CANDIDATE
    assert source.read_bytes() == b"third version"


def test_copy_leaves_source_untouched(source: Path, tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    output = other / "copy.docx"
    result = _save(source, output)
    assert result == {"output_path": str(output), "backup_path": None,
                      "backup_sha256": None, "save_mode": "copy"}
    assert output.read_bytes() == CANDIDATE
    assert source.read_bytes() == SOURCE
    assert stat.S_IMODE(output.stat().st_mode) == 0o640
    assert not (tmp_path / ".veqtor-backups").exists()
    assert output.stat().st_nlink == 1
    _no_temporary_files(tmp_path)


@pytest.mark.parametrize("kind", ["regular", "symlink", "hardlink", "dangling_symlink"])
def test_copy_never_overwrites_existing_entry(source: Path, tmp_path: Path, kind: str):
    output = tmp_path / "copy.docx"
    target = tmp_path / "target.docx"
    target.write_bytes(b"must survive")
    if kind == "regular":
        output.write_bytes(b"must survive")
    elif kind == "symlink":
        output.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, output)
    else:
        output.symlink_to(tmp_path / "missing.docx")
    with pytest.raises(ApplyError) as error:
        _save(source, output)
    assert error.value.code == "output_exists"
    assert target.read_bytes() == b"must survive"
    assert source.read_bytes() == SOURCE
    assert os.path.lexists(output)
    _no_temporary_files(tmp_path)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo"])
def test_unsafe_source_refused(source: Path, tmp_path: Path, kind: str):
    unsafe = tmp_path / "unsafe.docx"
    if kind == "symlink":
        unsafe.symlink_to(source)
    elif kind == "hardlink":
        os.link(source, unsafe)
    elif kind == "directory":
        unsafe.mkdir()
    else:
        os.mkfifo(unsafe)
    with pytest.raises(ApplyError) as error:
        _save(unsafe)
    assert error.value.code == "unsafe_path"
    assert source.read_bytes() == SOURCE
    _no_temporary_files(tmp_path)


@pytest.mark.parametrize(("basename", "lock_name"), [
    ("Agreement.docx", "~$Agreement.docx"),
    ("Agreement.docx", "~$greement.docx"),
    ("Agreement.docx", "~$reement.docx"),
    ("A.docx", "~$A.docx"),
    ("A.docx", "~$.docx"),
    ("ABC.docx", "~$BC.docx"),
    ("ABC.docx", "~$C.docx"),
])
def test_word_owner_file_refuses_save(source: Path, basename: str, lock_name: str):
    if source.name != basename:
        source = source.rename(source.with_name(basename))
    lock = source.parent / lock_name
    lock.write_bytes(b"Word owns this document")
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "file_locked"
    assert source.read_bytes() == SOURCE
    assert lock.read_bytes() == b"Word owns this document"
    _no_temporary_files(source.parent)


def test_stale_snapshot_refused_before_backup(source: Path):
    source.write_bytes(b"a newer user save")
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "file_sha256_mismatch"
    assert source.read_bytes() == b"a newer user save"
    assert not (source.parent / ".veqtor-backups").exists()
    _no_temporary_files(source.parent)


@pytest.mark.parametrize("mutation", ["write", "replace", "word_lock", "hardlink"])
def test_observed_race_before_publication_preserves_external_change(source: Path, monkeypatch, mutation: str):
    original = publishing._backup

    def race(*args, **kwargs):
        name = original(*args, **kwargs)
        if mutation == "write":
            source.write_bytes(b"newer Word content")
        elif mutation == "replace":
            replacement = source.parent / "replacement.docx"
            replacement.write_bytes(SOURCE)
            os.replace(replacement, source)
        elif mutation == "word_lock":
            (source.parent / "~$Agreement.docx").write_bytes(b"open")
        else:
            os.link(source, source.parent / "alias.docx")
        return name

    monkeypatch.setattr(publishing, "_backup", race)
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == {"write": "source_changed", "replace": "source_changed",
                                "word_lock": "file_locked", "hardlink": "unsafe_path"}[mutation]
    assert source.read_bytes() == (b"newer Word content" if mutation == "write" else SOURCE)
    assert error.value.metadata["publication_completed"] is False
    assert Path(error.value.metadata["backup_path"]).read_bytes() == SOURCE
    _no_temporary_files(source.parent)


def test_change_during_source_read_refused(source: Path, monkeypatch):
    original = publishing.os.read
    mutated = False

    def racing_read(fd, count):
        nonlocal mutated
        result = original(fd, count)
        if not mutated:
            mutated = True
            source.write_bytes(b"Word raced the read")
        return result

    monkeypatch.setattr(publishing.os, "read", racing_read)
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "source_changed"
    assert source.read_bytes() == b"Word raced the read"
    _no_temporary_files(source.parent)


def test_backup_directory_creation_failure_keeps_source(source: Path, monkeypatch):
    original = publishing.os.mkdir

    def fail(path, *args, **kwargs):
        if path == ".veqtor-backups":
            raise OSError(errno.ENOSPC, "disk full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(publishing.os, "mkdir", fail)
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "backup_failed"
    assert source.read_bytes() == SOURCE
    _no_temporary_files(source.parent)


def test_backup_data_sync_failure_keeps_source(source: Path, monkeypatch):
    original = publishing.os.fsync

    def fail(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode) and os.fstat(fd).st_size == len(SOURCE):
            raise OSError(errno.EIO, "backup write failed")
        return original(fd)

    monkeypatch.setattr(publishing.os, "fsync", fail)
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "backup_failed"
    assert source.read_bytes() == SOURCE
    assert not list((source.parent / ".veqtor-backups").iterdir())
    _no_temporary_files(source.parent)


def test_candidate_write_failure_keeps_source(source: Path, monkeypatch):
    original = publishing._write_all

    def fail(fd, payload):
        if payload == CANDIDATE:
            os.write(fd, payload[:3])
            raise OSError(errno.ENOSPC, "disk full during candidate write")
        return original(fd, payload)

    monkeypatch.setattr(publishing, "_write_all", fail)
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "output_unwritable"
    assert source.read_bytes() == SOURCE
    assert not (source.parent / ".veqtor-backups").exists()
    _no_temporary_files(source.parent)


def test_atomic_replace_failure_keeps_source_and_complete_backup(source: Path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError(errno.EACCES, "replacement denied")

    monkeypatch.setattr(publishing.os, "replace", fail)
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "output_unwritable"
    assert error.value.metadata["publication_completed"] is False
    assert source.read_bytes() == SOURCE
    assert Path(error.value.metadata["backup_path"]).read_bytes() == SOURCE
    _no_temporary_files(source.parent)


@pytest.mark.parametrize("kind", ["symlink", "writable_directory", "file"])
def test_unsafe_backup_directory_refused(source: Path, tmp_path: Path, kind: str):
    backups = tmp_path / ".veqtor-backups"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if kind == "symlink":
        backups.symlink_to(elsewhere, target_is_directory=True)
    elif kind == "file":
        backups.write_bytes(b"keep this")
    else:
        backups.mkdir(mode=0o700)
        backups.chmod(0o777)
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "unsafe_path"
    assert source.read_bytes() == SOURCE
    assert not list(elsewhere.iterdir())
    _no_temporary_files(tmp_path)


def test_backup_directory_swap_is_detected(source: Path, monkeypatch):
    original = publishing._backup
    displaced = source.parent / "displaced-backups"

    def race(*args, **kwargs):
        name = original(*args, **kwargs)
        backups = source.parent / ".veqtor-backups"
        backups.rename(displaced)
        backups.mkdir(mode=0o700)
        return name

    monkeypatch.setattr(publishing, "_backup", race)
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "source_changed"
    assert source.read_bytes() == SOURCE
    assert len(list(displaced.glob("*.docx"))) == 1
    assert not list((source.parent / ".veqtor-backups").iterdir())
    _no_temporary_files(source.parent)


def test_copy_creation_race_does_not_clobber(source: Path, tmp_path: Path, monkeypatch):
    output = tmp_path / "copy.docx"
    original = publishing.os.link

    def race(src, dst, *args, **kwargs):
        if dst == output.name:
            output.write_bytes(b"newly created by someone else")
        return original(src, dst, *args, **kwargs)

    monkeypatch.setattr(publishing.os, "link", race)
    with pytest.raises(ApplyError) as error:
        _save(source, output)
    assert error.value.code == "output_exists"
    assert source.read_bytes() == SOURCE
    assert output.read_bytes() == b"newly created by someone else"
    _no_temporary_files(tmp_path)


def test_concurrent_writer_is_refused(source: Path, monkeypatch):
    ready = threading.Event()
    release = threading.Event()
    original = publishing._backup
    results = []

    def pause(*args, **kwargs):
        result = original(*args, **kwargs)
        ready.set()
        assert release.wait(10)
        return result

    def first_save():
        try:
            results.append(_save(source))
        except BaseException as error:
            results.append(error)

    monkeypatch.setattr(publishing, "_backup", pause)
    worker = threading.Thread(target=first_save)
    worker.start()
    try:
        assert ready.wait(10)
        with pytest.raises(ApplyError) as error:
            _save(source)
        assert error.value.code == "file_locked"
    finally:
        release.set()
        worker.join(10)
    assert len(results) == 1 and isinstance(results[0], dict), results
    assert source.read_bytes() == CANDIDATE
    _no_temporary_files(source.parent)


def test_dead_legacy_sidecar_is_reclaimed(source: Path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    lock = source.parent / publishing._legacy_lock_name(source.name)
    lock.write_bytes(f"pid={dead.pid} token={'a' * 32}\n".encode())
    lock.chmod(0o600)
    result = _save(source)
    assert not lock.exists()
    assert source.read_bytes() == CANDIDATE
    assert Path(result["backup_path"]).read_bytes() == SOURCE


@pytest.mark.parametrize("kind", ["invalid", "empty", "live", "shared_write", "symlink", "hardlink"])
def test_unverifiable_legacy_sidecar_is_never_removed(source: Path, kind: str):
    lock = source.parent / publishing._legacy_lock_name(source.name)
    payload = f"pid={os.getpid()} token={'a' * 32}\n".encode()
    if kind == "invalid":
        payload = b"pid=999999999 token=abandoned"
    elif kind == "empty":
        payload = b""
    target = source.parent / "other-lock"
    if kind in {"symlink", "hardlink"}:
        target.write_bytes(payload)
        target.chmod(0o600)
        if kind == "symlink":
            lock.symlink_to(target)
        else:
            os.link(target, lock)
    else:
        lock.write_bytes(payload)
        lock.chmod(0o666 if kind == "shared_write" else 0o600)
    identity = lock.lstat().st_ino
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "file_locked"
    assert lock.lstat().st_ino == identity
    assert lock.read_bytes() == payload
    assert source.read_bytes() == SOURCE


def test_changed_legacy_lock_inode_is_not_reclaimed(source: Path, monkeypatch):
    lock = source.parent / publishing._legacy_lock_name(source.name)
    lock.write_bytes(f"pid=999999999 token={'a' * 32}\n".encode())
    lock.chmod(0o600)
    replacement_payload = f"pid={os.getpid()} token={'b' * 32}\n".encode()

    def process_lookup(pid, signal):
        replacement = source.parent / "replacement-lock"
        replacement.write_bytes(replacement_payload)
        replacement.chmod(0o600)
        os.replace(replacement, lock)
        raise ProcessLookupError(errno.ESRCH, "synthetic dead original process")

    monkeypatch.setattr(publishing.os, "kill", process_lookup)
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "file_locked"
    assert lock.read_bytes() == replacement_payload
    assert source.read_bytes() == SOURCE


@pytest.mark.parametrize("crash", [False, True])
@pytest.mark.parametrize("legacy_writer", [False, True])
def test_process_lock_excludes_live_writer_and_recovers_after_exit(source: Path, crash: bool,
                                                                  legacy_writer: bool):
    # A separate process owns the actual OS lock. Exercise both normal exit
    # and SIGKILL, where neither Python finally nor sidecar cleanup can run.
    script = """
import sys, os, hashlib, unicodedata, uuid
from veqtor_docx import _drafting_io as publishing
if sys.argv[3] == 'legacy':
    token = hashlib.sha256(unicodedata.normalize('NFC', sys.argv[2]).casefold().encode()).hexdigest()[:24]
    path = os.path.join(sys.argv[1], '.veqtor-' + token + '.lock')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, ('pid=' + str(os.getpid()) + ' token=' + uuid.uuid4().hex + '\\n').encode())
        os.fsync(descriptor)
        print('locked', flush=True)
        sys.stdin.read()
    finally:
        os.close(descriptor)
        os.unlink(path)
else:
    with publishing._directory(sys.argv[1]) as directory:
        with publishing._writer_lock(directory, sys.argv[1], sys.argv[2]):
            print('locked', flush=True)
            sys.stdin.read()
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(source.parent), source.name,
                                "legacy" if legacy_writer else "current"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        # The old O_EXCL writer must also see a current writer's sidecar.
        lock = source.parent / publishing._legacy_lock_name(source.name)
        with pytest.raises(FileExistsError):
            os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        # Removing an unrelated file cannot bypass the directory lock.
        obsolete = source.parent / ".veqtor-obsolete.lock"
        obsolete.write_bytes(b"not a lock")
        obsolete.unlink()
        with pytest.raises(ApplyError) as error:
            _save(source)
        assert error.value.code == "file_locked"
        assert source.read_bytes() == SOURCE
        assert not (source.parent / ".veqtor-backups").exists()
        if crash:
            process.kill()
        else:
            process.stdin.close()
        process.wait(timeout=10)
        result = _save(source)
        assert source.read_bytes() == CANDIDATE
        assert Path(result["backup_path"]).read_bytes() == SOURCE
        _no_temporary_files(source.parent)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


@pytest.mark.parametrize("phase", ["before_first_write", "before_publication", "after_publication"])
def test_sigkill_during_sidecar_initialization_never_leaves_unrecoverable_lock(source: Path, phase: str):
    script = """
import sys
from pathlib import Path
from veqtor_docx import _drafting_io as publishing
phase = sys.argv[2]
def pause():
    print('paused', flush=True)
    sys.stdin.read()
if phase == 'before_first_write':
    original = publishing._write_all
    def first_write(descriptor, payload):
        pause()
        return original(descriptor, payload)
    publishing._write_all = first_write
else:
    original = publishing._rename_noreplace
    def publish(directory, source, destination):
        if phase == 'before_publication':
            pause()
        result = original(directory, source, destination)
        if phase == 'after_publication':
            pause()
        return result
    publishing._rename_noreplace = publish
source = Path(sys.argv[1])
publishing.publish_document(str(source), None, source.read_bytes(), b'unreachable candidate')
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(source), phase],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "paused"
        lock = source.parent / publishing._legacy_lock_name(source.name)
        pending = list(source.parent.glob(".veqtor-lock-*.tmp"))
        if phase == "after_publication":
            assert not pending
            assert lock.stat().st_nlink == 1
            assert lock.read_bytes().startswith(f"pid={process.pid} token=".encode())
            assert lock.read_bytes().endswith(b"\n")
        else:
            assert not lock.exists()
            assert len(pending) == 1
            assert pending[0].stat().st_nlink == 1
            if phase == "before_first_write":
                assert pending[0].read_bytes() == b""
        pending_before = {path: (path.stat().st_ino, path.read_bytes()) for path in pending}
        process.kill()
        process.wait(timeout=10)
        assert source.read_bytes() == SOURCE
        result = _save(source)
        assert source.read_bytes() == CANDIDATE
        assert Path(result["backup_path"]).read_bytes() == SOURCE
        assert not lock.exists()
        # Unpublished unique files are inert. Do not delete an unknown empty
        # file just to make a cleanup assertion pass after the killed writer.
        assert {path: (path.stat().st_ino, path.read_bytes()) for path in pending} == pending_before
        assert set(source.parent.glob(".veqtor-lock-*.tmp")) == set(pending)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def test_exception_immediately_after_lock_publication_cleans_owned_sidecar(source: Path, monkeypatch):
    original = publishing._rename_noreplace

    def interrupted(*args):
        original(*args)
        raise OSError(errno.EIO, "interrupted just after native publication")

    monkeypatch.setattr(publishing, "_rename_noreplace", interrupted)
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "output_unwritable"
    assert source.read_bytes() == SOURCE
    _no_temporary_files(source.parent)


def test_atomic_sidecar_publication_never_replaces_existing_file(tmp_path: Path):
    temporary = tmp_path / "initialized"
    temporary.write_bytes(b"new lock")
    existing = tmp_path / "existing"
    existing.write_bytes(b"unrelated owner")
    with publishing._directory(str(tmp_path)) as directory:
        with pytest.raises(FileExistsError):
            publishing._rename_noreplace(directory, temporary.name, existing.name)
    assert temporary.read_bytes() == b"new lock"
    assert existing.read_bytes() == b"unrelated owner"


def test_directory_replacement_during_lock_acquisition_is_refused(tmp_path: Path, monkeypatch):
    parent = tmp_path / "working"
    parent.mkdir()
    source = parent / "Agreement.docx"
    source.write_bytes(SOURCE)
    displaced = tmp_path / "displaced"
    original = publishing.fcntl.flock

    def race(descriptor, operation):
        result = original(descriptor, operation)
        if operation & publishing.fcntl.LOCK_EX:
            parent.rename(displaced)
            parent.mkdir()
            source.write_bytes(b"replacement folder document")
        return result

    monkeypatch.setattr(publishing.fcntl, "flock", race)
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "source_changed"
    assert error.value.metadata["backup_path_verified"] is False
    assert source.read_bytes() == b"replacement folder document"
    assert (displaced / source.name).read_bytes() == SOURCE
    assert not (parent / ".veqtor-backups").exists()
    assert not (displaced / ".veqtor-backups").exists()
    _no_temporary_files(tmp_path)


def test_directory_replacement_after_backup_is_refused(tmp_path: Path, monkeypatch):
    parent = tmp_path / "working"
    parent.mkdir()
    source = parent / "Agreement.docx"
    source.write_bytes(SOURCE)
    displaced = tmp_path / "displaced"
    original = publishing._backup

    def race(*args, **kwargs):
        name = original(*args, **kwargs)
        parent.rename(displaced)
        parent.mkdir()
        source.write_bytes(b"replacement folder document")
        return name

    monkeypatch.setattr(publishing, "_backup", race)
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "source_changed"
    assert source.read_bytes() == b"replacement folder document"
    assert (displaced / source.name).read_bytes() == SOURCE
    backups = list((displaced / ".veqtor-backups").glob("*.docx"))
    assert len(backups) == 1 and backups[0].read_bytes() == SOURCE
    assert error.value.metadata["backup_path_verified"] is False
    assert not Path(error.value.metadata["backup_path"]).exists()
    _no_temporary_files(tmp_path)


def test_final_directory_sync_failure_reports_committed_candidate(source: Path, monkeypatch):
    original = publishing.os.fsync

    def fail(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and source.read_bytes() == CANDIDATE:
            raise OSError(errno.EIO, "directory sync failed after replacement")
        return original(fd)

    monkeypatch.setattr(publishing.os, "fsync", fail)
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "publication_uncertain"
    assert error.value.metadata["publication_completed"] is True
    assert error.value.metadata["backup_path_verified"] is True
    assert source.read_bytes() == CANDIDATE
    assert Path(error.value.metadata["backup_path"]).read_bytes() == SOURCE
    _no_temporary_files(source.parent)


def test_extended_attributes_preserved_when_supported(source: Path):
    if not hasattr(os, "setxattr"):
        pytest.skip("platform has no extended attributes")
    name = "com.veqtor.test" if sys.platform == "darwin" else "user.veqtor_test"
    try:
        os.setxattr(source, name, b"source metadata")
    except OSError as error:
        if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM}:
            pytest.skip("filesystem does not permit test extended attribute")
        raise
    result = _save(source)
    assert os.getxattr(source, name) == b"source metadata"
    assert os.getxattr(result["backup_path"], name) == b"source metadata"


def test_long_unicode_filename_has_valid_backup_name(tmp_path: Path):
    source = tmp_path / ("Договор" * 16 + ".docx")
    source.write_bytes(SOURCE)
    result = _save(source)
    assert Path(result["backup_path"]).read_bytes() == SOURCE
    assert source.read_bytes() == CANDIDATE


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS extended ACL regression")
@pytest.mark.parametrize("location", ["source", "copy_destination", "in_place_parent", "backup_directory"])
def test_darwin_acl_is_refused_without_losing_permissions(source: Path, location: str):
    output = None
    if location == "source":
        protected = source
        rule = "user:nobody deny read"
    elif location == "copy_destination":
        protected = source.parent / "destination"
        protected.mkdir()
        output = protected / "copy.docx"
        rule = "user:nobody deny read,file_inherit"
    elif location == "in_place_parent":
        protected = source.parent
        rule = "user:nobody allow read,file_inherit,directory_inherit"
    else:
        protected = source.parent / ".veqtor-backups"
        protected.mkdir(mode=0o700)
        rule = "user:nobody deny read"
    subprocess.run(["chmod", "+a", rule, str(protected)], check=True, capture_output=True)
    before_acl = subprocess.check_output(["ls", "-lde", str(protected)])
    assert b"user:nobody" in before_acl
    with pytest.raises(ApplyError) as error:
        _save(source, output)
    assert error.value.code == "unsupported_permissions"
    assert error.value.metadata["publication_completed"] is False
    assert source.read_bytes() == SOURCE
    after_acl = subprocess.check_output(["ls", "-lde", str(protected)])
    # ls's first line includes a size/mtime which directory staging can change;
    # the actual native ACL entry must remain byte-for-byte identical.
    assert after_acl.splitlines()[1:] == before_acl.splitlines()[1:]
    assert output is None or not output.exists()
    _no_temporary_files(source.parent)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="native Linux POSIX ACL regression")
def test_linux_posix_acl_is_refused_without_losing_permissions(source: Path):
    entries = [(0x01, 6, 0xFFFFFFFF), (0x02, 4, 65534),
               (0x04, 0, 0xFFFFFFFF), (0x10, 4, 0xFFFFFFFF), (0x20, 0, 0xFFFFFFFF)]
    acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
    try:
        os.setxattr(source, "system.posix_acl_access", acl)
    except OSError as error:
        if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM}:
            pytest.skip("test filesystem does not permit POSIX ACLs")
        raise
    before_acl = os.getxattr(source, "system.posix_acl_access")
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "unsupported_permissions"
    assert source.read_bytes() == SOURCE
    assert os.getxattr(source, "system.posix_acl_access") == before_acl
    _no_temporary_files(source.parent)


@pytest.mark.parametrize("attribute", sorted(publishing._ACL_ATTRIBUTES))
def test_acl_attribute_formats_fail_closed(source: Path, monkeypatch, attribute: str):
    if sys.platform == "darwin":
        monkeypatch.setattr(publishing, "_darwin_attributes", lambda descriptor: {attribute: b"acl"})
    else:
        monkeypatch.setattr(publishing.os, "listxattr", lambda descriptor: [attribute], raising=False)
        monkeypatch.setattr(publishing.os, "getxattr", lambda descriptor, name: b"acl", raising=False)
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == "unsupported_permissions"
    assert source.read_bytes() == SOURCE
    assert not (source.parent / ".veqtor-backups").exists()
    _no_temporary_files(source.parent)


def _native_attribute(path: Path, name: str) -> bytes:
    return bytes.fromhex(subprocess.check_output(["xattr", "-px", name, str(path)]).decode())


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS extended-attribute regression")
@pytest.mark.parametrize("copy", [False, True])
def test_darwin_native_xattrs_preserved_without_python_xattr_api(source: Path, monkeypatch, copy: bool):
    attributes = {
        "com.veqtor.test": b"\x00\xffbinary metadata\n",
        "com.apple.metadata:_kMDItemUserTags": plistlib.dumps(["Synthetic acceptance\n6"], fmt=plistlib.FMT_BINARY),
    }
    for name, value in attributes.items():
        subprocess.run(["xattr", "-wx", name, value.hex(), str(source)], check=True, capture_output=True)
    for name in ("listxattr", "getxattr", "setxattr"):
        monkeypatch.delattr(publishing.os, name, raising=False)
    output = source.parent / "copy.docx" if copy else None
    result = _save(source, output)
    for name, value in attributes.items():
        assert _native_attribute(Path(result["output_path"]), name) == value
        if result["backup_path"]:
            assert _native_attribute(Path(result["backup_path"]), name) == value
    assert source.read_bytes() == (SOURCE if copy else CANDIDATE)
    _no_temporary_files(source.parent)


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS process provenance regression")
@pytest.mark.parametrize("copy", [False, True])
def test_different_process_provenance_does_not_prevent_save(source: Path, monkeypatch, copy: bool):
    tags = plistlib.dumps(["Synthetic tagged document\n4"], fmt=plistlib.FMT_BINARY)
    tag_name = "com.apple.metadata:_kMDItemUserTags"
    subprocess.run(["xattr", "-wx", tag_name, tags.hex(), str(source)], check=True, capture_output=True)
    source_identity = publishing._identity(source.stat())
    attributes = publishing._darwin_attributes
    setter = publishing._set_attributes
    copied_names = []

    def process_provenance(descriptor):
        # The OS can return different provenance even after fsetxattr reports
        # success. Other metadata is read from the real native filesystem API.
        result = attributes(descriptor)
        original = publishing._identity(os.fstat(descriptor)) == source_identity
        result["com.apple.provenance"] = b"original process" if original else b"saving process"
        return result

    def record_setter(descriptor, values):
        copied_names.append(set(values))
        return setter(descriptor, values)

    monkeypatch.setattr(publishing, "_darwin_attributes", process_provenance)
    monkeypatch.setattr(publishing, "_set_attributes", record_setter)
    output = source.parent / "copy.docx" if copy else None
    result = _save(source, output)
    assert source.read_bytes() == (SOURCE if copy else CANDIDATE)
    assert copied_names and all("com.apple.provenance" not in names for names in copied_names)
    assert all(tag_name in names for names in copied_names)
    assert _native_attribute(Path(result["output_path"]), tag_name) == tags
    if result["backup_path"]:
        assert Path(result["backup_path"]).read_bytes() == SOURCE
        assert _native_attribute(Path(result["backup_path"]), tag_name) == tags
    _no_temporary_files(source.parent)


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS metadata failure regression")
@pytest.mark.parametrize("failure", ["read", "write", "readback"])
def test_darwin_xattr_failure_never_replaces_source(source: Path, monkeypatch, failure: str):
    name, value = "com.veqtor.test", b"preserve this metadata"
    subprocess.run(["xattr", "-wx", name, value.hex(), str(source)], check=True, capture_output=True)

    def refused(*args, **kwargs):
        raise OSError(errno.EACCES, "synthetic metadata permission failure")

    if failure == "read":
        monkeypatch.setattr(publishing, "_darwin_attributes", refused)
    elif failure == "write":
        monkeypatch.setattr(publishing, "_set_attributes", refused)
    else:
        monkeypatch.setattr(publishing, "_set_attributes", lambda *args: None)
    with pytest.raises(ApplyError) as error:
        _save(source)
    assert error.value.code == ("unsupported_permissions" if failure == "readback" else "output_unwritable")
    assert error.value.metadata["publication_completed"] is False
    assert source.read_bytes() == SOURCE
    assert _native_attribute(source, name) == value
    assert not (source.parent / ".veqtor-backups").exists()
    _no_temporary_files(source.parent)
