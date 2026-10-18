# SPDX-License-Identifier: Apache-2.0
"""Recovery boundaries before registration and during the final cleanup steps."""

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from veqtor_docx import _drafting_io as publishing


SOURCE = b"unchanged synthetic original\x00\xff"
CANDIDATE = b"edited synthetic document\x00\xfe"


def _source(folder: Path) -> Path:
    source = folder / "Agreement.docx"
    source.write_bytes(SOURCE)
    return source


def _orphan(folder: Path) -> Path:
    stage = folder / f".veqtor-stage-{uuid.uuid4().hex}"
    stage.mkdir(mode=0o700)
    candidate = stage / "candidate.tmp"
    candidate.write_bytes(b"registered, abandoned candidate")
    candidate.chmod(0o600)
    record = {
        "version": 1, "pid": os.getpid(), "instance": "0" * 64,
        "directory": [stage.stat().st_dev, stage.stat().st_ino],
        "file": [candidate.stat().st_dev, candidate.stat().st_ino],
        "size": candidate.stat().st_size,
        "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "uid": os.geteuid(), "gid": os.getegid(), "mode": 0o600,
    }
    manifest = stage / "owner.json"
    manifest.write_text(json.dumps(record))
    manifest.chmod(0o600)
    return stage


CRASH_SCRIPT = r'''
import os, sys
from pathlib import Path
from veqtor_docx import _drafting_io as io
source, phase = Path(sys.argv[1]), sys.argv[2]
def pause():
    print('paused', flush=True)
    sys.stdin.read()
write = io._write_all
def writing(fd, payload):
    if payload.startswith(b'pid='):
        if phase == 'sidecar_before_record':
            pause()
        if phase == 'sidecar_after_record':
            write(fd, payload)
            os.fsync(fd)
            pause()
    if phase == 'stage_during_manifest' and payload.startswith(b'{"version":'):
        os.write(fd, payload[:len(payload)//2])
        os.fsync(fd)
        pause()
    return write(fd, payload)
io._write_all = writing
mkdir = io.os.mkdir
def making(name, *args, **kwargs):
    result = mkdir(name, *args, **kwargs)
    if phase == 'stage_after_mkdir' and str(name).startswith('.veqtor-stage-'):
        pause()
    return result
io.os.mkdir = making
opening = io.os.open
def opened(name, *args, **kwargs):
    result = opening(name, *args, **kwargs)
    if phase == 'stage_after_candidate' and name == 'candidate.tmp':
        pause()
    return result
io.os.open = opened
rename = io._rename_noreplace
def renaming(directory, source, destination, **kwargs):
    result = rename(directory, source, destination, **kwargs)
    if source == 'owner.json' and phase.endswith('after_manifest_move'):
        pause()
    return result
io._rename_noreplace = renaming
rmdir = io.os.rmdir
def removing(name, *args, **kwargs):
    result = rmdir(name, *args, **kwargs)
    if str(name).startswith('.veqtor-stage-') and phase.endswith('after_stage_removal'):
        pause()
    return result
io.os.rmdir = removing
io.publish_document(str(source), None, source.read_bytes(), b'child candidate')
'''


@pytest.mark.parametrize("phase", [
    "sidecar_before_record", "sidecar_after_record", "stage_after_mkdir",
    "stage_after_candidate", "stage_during_manifest",
    "cleanup_after_manifest_move", "cleanup_after_stage_removal",
    "recovery_after_manifest_move", "recovery_after_stage_removal",
])
def test_interrupted_bootstrap_and_cleanup_preserve_documents(tmp_path: Path, phase: str):
    source = _source(tmp_path)
    if phase.startswith("recovery_"):
        _orphan(tmp_path)
    process = subprocess.Popen([sys.executable, "-c", CRASH_SCRIPT, str(source), phase],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "paused"
        process.kill()
        process.wait(timeout=10)
        assert source.read_bytes() == SOURCE
        residual = {}
        if phase == "sidecar_before_record":
            paths = list(tmp_path.glob(".veqtor-lock-*.tmp"))
            assert len(paths) == 1 and paths[0].read_bytes() == b""
            residual = {path: (path.stat().st_ino, path.read_bytes()) for path in paths}
        elif phase.startswith("stage_"):
            stages = list(tmp_path.glob(".veqtor-stage-*"))
            assert len(stages) == 1
            expected = {
                "stage_after_mkdir": set(),
                "stage_after_candidate": {"candidate.tmp"},
                "stage_during_manifest": {"candidate.tmp", "owner.json"},
            }[phase]
            assert {path.name for path in stages[0].iterdir()} == expected
            residual = {stages[0]: (stages[0].stat().st_ino, None)}
            residual.update({path: (path.stat().st_ino, path.read_bytes())
                             for path in stages[0].iterdir()})
        result = publishing.publish_document(str(source), None, SOURCE, CANDIDATE)
        assert source.read_bytes() == CANDIDATE
        assert Path(result["backup_path"]).read_bytes() == SOURCE
        assert all(path.read_bytes() == SOURCE
                   for path in (tmp_path / ".veqtor-backups").glob("*.docx"))
        for path, (inode, payload) in residual.items():
            assert path.stat().st_ino == inode
            if payload is not None:
                assert path.read_bytes() == payload
        assert not list(tmp_path.rglob(".veqtor-cleanup-*.json"))
        assert not list(tmp_path.glob(".veqtor-*.lock"))
        assert set(tmp_path.rglob(".veqtor-stage-*")) == {
            path for path, (_, payload) in residual.items() if payload is None
        }
        assert set(tmp_path.glob(".veqtor-lock-*.tmp")) == {
            path for path in residual if path.name.startswith(".veqtor-lock-")
        }
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


@pytest.mark.parametrize("mutation", [
    "live_owner", "invalid_record", "shared_record", "record_symlink", "record_hardlink",
    "changed_directory", "extra_entry", "directory_symlink", "shared_directory",
])
def test_cleanup_recovery_preserves_unverifiable_objects(tmp_path: Path, mutation: str):
    source = _source(tmp_path)
    stage = _orphan(tmp_path)
    candidate = stage / "candidate.tmp"
    candidate.unlink()
    cleanup = tmp_path / f".veqtor-cleanup-{stage.name.removeprefix('.veqtor-stage-')}.json"
    (stage / "owner.json").rename(cleanup)
    if mutation == "live_owner":
        record = json.loads(cleanup.read_text())
        record["instance"] = publishing.process_instance(os.getpid())
        cleanup.write_text(json.dumps(record))
    elif mutation == "invalid_record":
        cleanup.write_bytes(b"changed, unrelated contents")
    elif mutation == "shared_record":
        cleanup.chmod(0o644)
    elif mutation in {"record_symlink", "record_hardlink"}:
        target = tmp_path / "unrelated-record"
        cleanup.rename(target)
        if mutation == "record_symlink":
            cleanup.symlink_to(target)
        else:
            os.link(target, cleanup)
    elif mutation == "changed_directory":
        stage.rename(tmp_path / "kept-original-directory")
        stage.mkdir(mode=0o700)
    elif mutation == "extra_entry":
        (stage / "user-note").write_text("retain this")
    elif mutation == "directory_symlink":
        stage.rename(tmp_path / "unrelated-directory")
        stage.symlink_to(tmp_path / "unrelated-directory", target_is_directory=True)
    elif mutation == "shared_directory":
        stage.chmod(0o750)
    before = (cleanup.lstat().st_ino, cleanup.read_bytes(), stage.lstat().st_ino)
    result = publishing.publish_document(str(source), None, SOURCE, CANDIDATE)
    assert source.read_bytes() == CANDIDATE
    assert Path(result["backup_path"]).read_bytes() == SOURCE
    assert before == (cleanup.lstat().st_ino, cleanup.read_bytes(), stage.lstat().st_ino)
    if mutation == "extra_entry":
        assert (stage / "user-note").read_text() == "retain this"


def test_existing_cleanup_record_is_never_replaced(tmp_path: Path):
    source = _source(tmp_path)
    stage = _orphan(tmp_path)
    cleanup = tmp_path / f".veqtor-cleanup-{stage.name.removeprefix('.veqtor-stage-')}.json"
    cleanup.write_bytes(b"unrelated existing record")
    before = (cleanup.stat().st_ino, cleanup.read_bytes(), (stage / "owner.json").read_bytes())
    result = publishing.publish_document(str(source), None, SOURCE, CANDIDATE)
    assert Path(result["backup_path"]).read_bytes() == SOURCE
    assert before == (cleanup.stat().st_ino, cleanup.read_bytes(), (stage / "owner.json").read_bytes())


def test_cleanup_record_changed_during_directory_removal_is_retained(tmp_path: Path, monkeypatch):
    source = _source(tmp_path)
    rmdir = publishing.os.rmdir
    changed = False

    def change_record(name, *args, **kwargs):
        nonlocal changed
        result = rmdir(name, *args, **kwargs)
        if not changed and str(name).startswith(".veqtor-stage-"):
            changed = True
            cleanup = f".veqtor-cleanup-{name.removeprefix('.veqtor-stage-')}.json"
            descriptor = os.open(cleanup, os.O_WRONLY | os.O_TRUNC, dir_fd=kwargs["dir_fd"])
            try:
                os.write(descriptor, b"externally changed cleanup record")
            finally:
                os.close(descriptor)
        return result

    monkeypatch.setattr(publishing.os, "rmdir", change_record)
    result = publishing.publish_document(str(source), None, SOURCE, CANDIDATE)
    assert source.read_bytes() == CANDIDATE
    assert Path(result["backup_path"]).read_bytes() == SOURCE
    records = list(tmp_path.rglob(".veqtor-cleanup-*.json"))
    assert changed and len(records) == 1
    assert records[0].read_bytes() == b"externally changed cleanup record"


@pytest.mark.parametrize("payload", [b"", b"pid=123", b"pid=999999999 token=" + b"a" * 32 + b"\n"])
def test_unregistered_sidecar_temporaries_are_preserved(tmp_path: Path, payload: bytes):
    source = _source(tmp_path)
    temporary = tmp_path / f".veqtor-lock-{uuid.uuid4().hex}.tmp"
    temporary.write_bytes(payload)
    temporary.chmod(0o600)
    before = temporary.stat().st_ino
    result = publishing.publish_document(str(source), None, SOURCE, CANDIDATE)
    assert Path(result["backup_path"]).read_bytes() == SOURCE
    assert temporary.stat().st_ino == before and temporary.read_bytes() == payload
