# SPDX-License-Identifier: Apache-2.0
"""Distinguish a macOS folder's own ACL from permissions passed to new files."""

import ctypes
import errno
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from veqtor_docx import ApplyError
from veqtor_docx import _drafting_io as publishing


SOURCE = b"validated original document"
CANDIDATE = b"validated edited document"
native_macos = pytest.mark.skipif(sys.platform != "darwin", reason="native macOS ACL semantics")


def _acl(path: Path) -> list[bytes]:
    return subprocess.check_output(["ls", "-lde", str(path)]).splitlines()[1:]


def _chmod(path: Path, rule: str, mode: str = "+a") -> None:
    subprocess.run(["chmod", mode, rule, str(path)], check=True, capture_output=True)


def _clear_acl(path: Path) -> None:
    subprocess.run(["chmod", "-N", str(path)], check=True, capture_output=True)


def _paths(tmp_path: Path, save_mode: str) -> tuple[Path, Path | None, Path]:
    source = tmp_path / "Agreement.docx"
    source.write_bytes(SOURCE)
    source.chmod(0o640)
    if save_mode == "in_place":
        return source, None, tmp_path
    parent = tmp_path / "copies"
    parent.mkdir()
    return source, parent / "Copy.docx", parent


def _save(source: Path, output: Path | None) -> dict:
    return publishing.publish_document(str(source), str(output) if output else None, SOURCE, CANDIDATE)


def _no_temporary_files(parent: Path) -> None:
    assert not list(parent.rglob(".veqtor-*.tmp"))
    assert not list(parent.rglob(".veqtor-*.lock"))
    assert not list(parent.rglob(".veqtor-stage-*"))
    assert not list(parent.rglob(".veqtor-cleanup-*.json"))


@native_macos
@pytest.mark.parametrize("save_mode", ["in_place", "copy"])
@pytest.mark.parametrize("inherited_entry", [False, True])
def test_parent_system_protection_does_not_block_save(tmp_path: Path, save_mode: str, inherited_entry: bool):
    source, output, parent = _paths(tmp_path, save_mode)
    # These protect the folder itself, just like Desktop/Documents/Downloads.
    # An entry marked "inherited" is still unrelated to inheritance to children.
    _chmod(parent, "group:everyone deny delete,writeextattr", "+ai" if inherited_entry else "+a")
    before_acl = _acl(parent)
    try:
        result = _save(source, output)
        assert (output or source).read_bytes() == CANDIDATE
        assert result["save_mode"] == save_mode
        assert _acl(parent) == before_acl
        assert not _acl(output or source)
        if output:
            assert source.read_bytes() == SOURCE
            assert result["backup_path"] is None
        else:
            backup = Path(result["backup_path"])
            assert backup.read_bytes() == SOURCE
            assert result["backup_sha256"] == hashlib.sha256(SOURCE).hexdigest()
            assert not _acl(backup)
        _no_temporary_files(tmp_path)
    finally:
        _clear_acl(parent)


@native_macos
@pytest.mark.parametrize("save_mode", ["in_place", "copy"])
@pytest.mark.parametrize("flags", [
    "file_inherit", "directory_inherit", "file_inherit,directory_inherit",
    "file_inherit,only_inherit", "directory_inherit,limit_inherit",
])
def test_parent_child_inheritance_is_refused(tmp_path: Path, save_mode: str, flags: str):
    source, output, parent = _paths(tmp_path, save_mode)
    _chmod(parent, "group:everyone deny delete")
    _chmod(parent, f"user:nobody allow read,{flags}")
    before_acl = _acl(parent)
    try:
        with pytest.raises(ApplyError) as caught:
            _save(source, output)
        assert caught.value.code == "unsupported_permissions"
        assert caught.value.metadata["publication_completed"] is False
        assert source.read_bytes() == SOURCE
        assert output is None or not output.exists()
        assert _acl(parent) == before_acl
        assert not list(tmp_path.rglob(".veqtor-backups"))
        _no_temporary_files(tmp_path)
    finally:
        _clear_acl(parent)


@native_macos
@pytest.mark.parametrize("save_mode", ["in_place", "copy"])
def test_parent_actual_add_file_denial_is_enforced(tmp_path: Path, save_mode: str):
    source, output, parent = _paths(tmp_path, save_mode)
    _chmod(parent, "group:everyone deny add_file")
    before_acl = _acl(parent)
    try:
        with pytest.raises(ApplyError) as caught:
            _save(source, output)
        assert caught.value.code == "output_unwritable"
        assert caught.value.metadata["publication_completed"] is False
        assert caught.value.metadata["backup_path"] is None
        assert source.read_bytes() == SOURCE
        assert output is None or not output.exists()
        assert _acl(parent) == before_acl
        _no_temporary_files(tmp_path)
    finally:
        _clear_acl(parent)


@native_macos
@pytest.mark.parametrize("save_mode", ["in_place", "copy"])
def test_parent_permission_denial_before_publication_keeps_recovery(tmp_path: Path, save_mode: str):
    source, output, parent = _paths(tmp_path, save_mode)
    # End the writer process after the denied publication. The next process can
    # prove the abandoned stage's owner has ended; a still-live owner remains
    # deliberately protected even if the folder's permissions are restored.
    script = r'''
import json, subprocess, sys
from pathlib import Path
from veqtor_docx import ApplyError
from veqtor_docx import _drafting_io as io
source, output, parent = Path(sys.argv[1]), sys.argv[2] or None, sys.argv[3]
source_current, checks = io._source_current, 0
def deny_publication(*args, **kwargs):
    global checks
    result = source_current(*args, **kwargs)
    checks += 1
    if checks == 2:
        subprocess.run(['chmod', '+a', 'group:everyone deny add_file', parent],
                       check=True, capture_output=True)
    return result
io._source_current = deny_publication
try:
    io.publish_document(str(source), output, source.read_bytes(), b'validated edited document')
except ApplyError as exc:
    print(json.dumps({'checks': checks, 'code': exc.code, 'metadata': exc.metadata}))
else:
    raise AssertionError('late permission denial unexpectedly allowed publication')
'''
    try:
        completed = subprocess.run([sys.executable, "-c", script, str(source),
                                    str(output) if output else "", str(parent)],
                                   check=True, capture_output=True, text=True, timeout=30)
        failure = json.loads(completed.stdout)
        assert failure["checks"] == 2
        assert failure["code"] == "output_unwritable"
        assert failure["metadata"]["publication_completed"] is False
        assert source.read_bytes() == SOURCE
        assert output is None or not output.exists()
        if output:
            assert failure["metadata"]["backup_path"] is None
        else:
            backup = Path(failure["metadata"]["backup_path"])
            assert backup.read_bytes() == SOURCE
            assert failure["metadata"]["backup_path_verified"] is True
            assert failure["metadata"]["backup_sha256"] == hashlib.sha256(SOURCE).hexdigest()
        stages = list(tmp_path.rglob(".veqtor-stage-*"))
        assert len(stages) == 1
        assert {path.name for path in stages[0].iterdir()} == {"owner.json"}
        record = json.loads((stages[0] / "owner.json").read_text())
        assert publishing.instance_ended(record["pid"], record["instance"])
        _clear_acl(parent)
        result = _save(source, output)
        assert (output or source).read_bytes() == CANDIDATE
        if output:
            assert source.read_bytes() == SOURCE
        else:
            assert backup.read_bytes() == SOURCE
            assert Path(result["backup_path"]).read_bytes() == SOURCE
        _no_temporary_files(tmp_path)
    finally:
        _clear_acl(parent)


@native_macos
@pytest.mark.parametrize("save_mode", ["in_place", "copy"])
def test_parent_inheritance_added_before_publication_is_refused(tmp_path: Path, monkeypatch, save_mode: str):
    source, output, parent = _paths(tmp_path, save_mode)
    source_current = publishing._source_current
    checks = 0

    def add_inheritance(*args, **kwargs):
        nonlocal checks
        checks += 1
        if checks == 2:
            _chmod(parent, "user:nobody allow read,file_inherit")
        return source_current(*args, **kwargs)

    monkeypatch.setattr(publishing, "_source_current", add_inheritance)
    try:
        with pytest.raises(ApplyError) as caught:
            _save(source, output)
        assert checks == 2
        assert caught.value.code == "unsupported_permissions"
        assert caught.value.metadata["publication_completed"] is False
        assert source.read_bytes() == SOURCE
        assert output is None or not output.exists()
        if output:
            assert caught.value.metadata["backup_path"] is None
        else:
            backup = Path(caught.value.metadata["backup_path"])
            assert backup.read_bytes() == SOURCE
            assert caught.value.metadata["backup_path_verified"] is True
            assert caught.value.metadata["backup_sha256"] == hashlib.sha256(SOURCE).hexdigest()
        assert _acl(parent)
        _no_temporary_files(tmp_path)
    finally:
        _clear_acl(parent)


@pytest.mark.parametrize("failure", ["validation", "entry", "flagset", "flag", "unterminated"])
def test_unreadable_parent_acl_fails_closed_and_frees_native_object(monkeypatch, failure: str):
    freed = []

    def get_acl(*args):
        return 42

    def release(value):
        freed.append(value)
        return 0

    def validate(*args):
        return -1 if failure == "validation" else 0

    def get_entry(acl, index, result):
        if failure == "entry":
            ctypes.set_errno(errno.EIO)
            return -1
        ctypes.cast(result, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(43)
        return 0

    def get_flagset(entry, result):
        ctypes.cast(result, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(44)
        return -1 if failure == "flagset" else 0

    def get_flag(*args):
        return -1 if failure == "flag" else 0

    native = SimpleNamespace(acl_get_fd_np=get_acl, acl_free=release, acl_valid=validate,
                             acl_get_entry=get_entry, acl_get_flagset_np=get_flagset, acl_get_flag_np=get_flag)
    monkeypatch.setattr(publishing.ctypes, "CDLL", lambda *args, **kwargs: native)
    with pytest.raises(ApplyError) as caught:
        publishing._darwin_acl_present(1, inheritable_only=True)
    assert caught.value.code == "unsupported_permissions"
    assert freed == [42]
