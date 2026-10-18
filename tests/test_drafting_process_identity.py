# SPDX-License-Identifier: Apache-2.0
"""Process identity must distinguish PID reuse without inferring death from EPERM."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from veqtor_docx import _drafting_fs as native

BOOT = b"cfd1a9d0-3a3c-4db4-a129-e4a4505d81b8"
PID = 123
START = (1_792_040_736, 465_806)


def _identity(start=START):
    return hashlib.sha256(
        uuid.UUID(BOOT.decode()).bytes + b":" + f"{start[0]}:{start[1]}".encode()
    ).hexdigest()


class _Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class _DarwinLibrary:
    def __init__(self, *, proc_error=errno.EPERM, sysctl_error=None,
                 sysctl_size=648, returned_pid=PID, start=START):
        self.proc_error = proc_error
        self.sysctl_error = sysctl_error
        self.sysctl_size = sysctl_size
        self.returned_pid = returned_pid
        self.start = start
        self.fallback_calls = 0
        self.sysctlbyname = _Function(self._boot)
        self.proc_pidinfo = _Function(self._proc)
        self.sysctl = _Function(self._sysctl)

    def _boot(self, name, buffer, length, new, new_length):
        assert name == b"kern.bootsessionuuid" and new is None and new_length == 0
        ctypes.memmove(buffer, BOOT + b"\0", len(BOOT) + 1)
        ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t)).contents.value = len(BOOT) + 1
        return 0

    def _proc(self, pid, flavor, arg, buffer, size):
        assert pid == PID and flavor == 3 and arg == 0
        assert size == ctypes.sizeof(native._DarwinBSDInfo)
        if self.proc_error is not None:
            ctypes.set_errno(self.proc_error)
            return 0
        value = ctypes.cast(buffer, ctypes.POINTER(native._DarwinBSDInfo)).contents
        value.prefix[3] = pid
        value.start_seconds, value.start_microseconds = self.start
        return size

    def _sysctl(self, mib, count, buffer, length, new, new_length):
        self.fallback_calls += 1
        assert list(mib) == [1, 14, 1, PID] and count == 4
        assert new is None and new_length == 0
        if self.sysctl_error is not None:
            ctypes.set_errno(self.sysctl_error)
            return -1
        value = ctypes.cast(buffer, ctypes.POINTER(native._DarwinExternProcPrefix)).contents
        value.pid = self.returned_pid
        value.start_seconds, value.start_microseconds = self.start
        ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t)).contents.value = self.sysctl_size
        return 0


def _install(monkeypatch, **kwargs):
    library = _DarwinLibrary(**kwargs)
    monkeypatch.setattr(native, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(native.ctypes, "CDLL", lambda *args, **kw: library)
    return library


def test_foreign_uid_pid_reuse_is_proved_by_public_start_time(monkeypatch):
    library = _install(monkeypatch)
    assert native.process_instance(PID) == _identity()
    assert native.instance_ended(PID, _identity((START[0] - 1, START[1])))
    assert library.fallback_calls == 2


def test_foreign_uid_same_process_is_never_reclaimed(monkeypatch):
    _install(monkeypatch)
    assert not native.instance_ended(PID, _identity())


def test_current_owner_retains_previous_identity_without_fallback(monkeypatch):
    library = _install(monkeypatch, proc_error=None)
    assert native.process_instance(PID) == _identity()
    assert not native.instance_ended(PID, _identity())
    assert library.fallback_calls == 0


@pytest.mark.parametrize("error", [errno.EPERM, errno.EACCES, errno.EIO, errno.ENOSYS, 0])
def test_failed_public_query_does_not_prove_death(monkeypatch, error):
    _install(monkeypatch, sysctl_error=error)
    assert not native.instance_ended(PID, "0" * 64)


def test_os_proven_absence_from_public_query_allows_recovery(monkeypatch):
    _install(monkeypatch, sysctl_error=errno.ESRCH)
    assert native.instance_ended(PID, "0" * 64)


@pytest.mark.parametrize("kill_error,expected", [
    (None, False), (errno.EPERM, False), (errno.EACCES, False),
    (errno.EIO, False), (errno.ESRCH, True),
])
def test_empty_public_metadata_requires_independent_absence(monkeypatch, kill_error, expected):
    _install(monkeypatch, sysctl_size=0)

    def kill(pid, signal):
        assert pid == PID and signal == 0
        if kill_error is not None:
            raise OSError(kill_error, "cannot inspect PID")

    monkeypatch.setattr(native.os, "kill", kill)
    assert native.instance_ended(PID, "0" * 64) is expected


@pytest.mark.parametrize("parameters", [
    {"sysctl_size": 1}, {"sysctl_size": ctypes.sizeof(native._DarwinExternProcPrefix) - 1},
    {"sysctl_size": 1025}, {"returned_pid": PID + 1},
    {"start": (0, 0)}, {"start": (-1, 0)}, {"start": (START[0], -1)},
    {"start": (START[0], 1_000_000)},
])
def test_malformed_public_metadata_cannot_reclaim_a_lock(monkeypatch, parameters):
    _install(monkeypatch, **parameters)
    assert not native.instance_ended(PID, "0" * 64)


def test_unavailable_public_api_retains_lock(monkeypatch):
    library = _install(monkeypatch)
    del library.sysctl
    assert not native.instance_ended(PID, "0" * 64)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native read-only identity probe")
def test_native_public_and_task_queries_agree_for_current_process():
    seconds, microseconds = native._darwin_sysctl_process_start(
        ctypes.CDLL(None, use_errno=True), os.getpid(),
    )
    library = ctypes.CDLL(None, use_errno=True)
    value = native._DarwinBSDInfo()
    getter = library.proc_pidinfo
    getter.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    getter.restype = ctypes.c_int
    assert getter(os.getpid(), 3, 0, ctypes.byref(value), ctypes.sizeof(value)) == ctypes.sizeof(value)
    assert (seconds, microseconds) == (value.start_seconds, value.start_microseconds)
    mine = native.process_instance(os.getpid())
    assert not native.instance_ended(os.getpid(), mine)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native read-only identity probe")
def test_native_foreign_uid_process_identity_is_readable_without_signals():
    # launchd is a persistent root process; this reads metadata only. A matching
    # identity must retain a lock, while a different start identity proves reuse.
    identity = native.process_instance(1)
    assert identity == native.process_instance(1)
    assert not native.instance_ended(1, identity)
    assert native.instance_ended(1, "0" * 64)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native lock recovery probe")
@pytest.mark.parametrize("same_instance", [False, True])
def test_native_foreign_uid_lock_recovery_requires_different_instance(tmp_path, same_instance):
    from veqtor_docx import ApplyError
    from veqtor_docx import _drafting_io as publishing

    source = tmp_path / "Agreement.docx"
    original, edited = b"original document bytes", b"edited document bytes"
    source.write_bytes(original)
    source.chmod(0o600)
    identity = native.process_instance(1) if same_instance else "0" * 64
    lock = tmp_path / publishing._legacy_lock_name(source.name)
    lock.write_text(f"pid=1 token={'a' * 32} instance={identity}\n")
    lock.chmod(0o600)
    if same_instance:
        with pytest.raises(ApplyError) as error:
            publishing.publish_document(str(source), None, original, edited)
        assert error.value.code == "file_locked"
        assert source.read_bytes() == original and lock.exists()
    else:
        result = publishing.publish_document(str(source), None, original, edited)
        assert source.read_bytes() == edited
        assert Path(result["backup_path"]).read_bytes() == original
        assert not lock.exists()
