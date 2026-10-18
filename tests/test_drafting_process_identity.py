# SPDX-License-Identifier: Apache-2.0
"""Process identity must distinguish PID reuse without inferring death from EPERM."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import select
import signal
import subprocess
import sys
import time
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
                 sysctl_size=648, returned_pid=PID, start=START, status=2, boot=BOOT):
        self.proc_error = proc_error
        self.sysctl_error = sysctl_error
        self.sysctl_size = sysctl_size
        self.returned_pid = returned_pid
        self.start = start
        self.status = status
        self.boot = boot
        self.fallback_calls = 0
        self.sysctlbyname = _Function(self._boot)
        self.proc_pidinfo = _Function(self._proc)
        self.sysctl = _Function(self._sysctl)

    def _boot(self, name, buffer, length, new, new_length):
        assert name == b"kern.bootsessionuuid" and new is None and new_length == 0
        ctypes.memmove(buffer, self.boot + b"\0", len(self.boot) + 1)
        ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t)).contents.value = len(self.boot) + 1
        return 0

    def _proc(self, pid, flavor, arg, buffer, size):
        assert pid == PID and flavor == 3 and arg == 0
        assert size == ctypes.sizeof(native._DarwinBSDInfo)
        if self.proc_error is not None:
            ctypes.set_errno(self.proc_error)
            return 0
        value = ctypes.cast(buffer, ctypes.POINTER(native._DarwinBSDInfo)).contents
        value.prefix[1] = self.status
        value.prefix[3] = self.returned_pid
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
        value.status = bytes([self.status])
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


@pytest.mark.parametrize("proc_error", [None, errno.EPERM])
@pytest.mark.parametrize("status", [1, 2, 3, 4])
def test_darwin_live_states_retain_identity(monkeypatch, proc_error, status):
    _install(monkeypatch, proc_error=proc_error, status=status)
    assert native.process_instance(PID) == _identity()
    assert not native.instance_ended(PID, _identity())


@pytest.mark.parametrize("proc_error", [None, errno.EPERM])
def test_darwin_confirmed_zombie_allows_recovery_before_reaping(monkeypatch, proc_error):
    library = _install(monkeypatch, proc_error=proc_error, status=5)
    with pytest.raises(ProcessLookupError):
        native.process_instance(PID)
    assert native.instance_ended(PID, _identity())
    assert library.fallback_calls == (2 if proc_error is not None else 0)


@pytest.mark.parametrize("proc_error", [None, errno.EPERM])
@pytest.mark.parametrize("parameters", [
    {"returned_pid": PID + 1}, {"start": (0, 0)},
    {"start": (START[0], 1_000_000)}, {"boot": b"invalid-boot"},
    {"status": 0}, {"status": 6}, {"status": 255},
])
def test_darwin_unverified_zombie_metadata_retains_lock(monkeypatch, proc_error, parameters):
    _install(monkeypatch, proc_error=proc_error, **{"status": 5, **parameters})
    assert not native.instance_ended(PID, _identity())


@pytest.mark.parametrize("error", [errno.EPERM, errno.EACCES, errno.EIO])
def test_darwin_inaccessible_zombie_metadata_retains_lock(monkeypatch, error):
    _install(monkeypatch, status=5, sysctl_error=error)
    assert not native.instance_ended(PID, _identity())


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
    seconds, microseconds, status = native._darwin_sysctl_process_info(
        ctypes.CDLL(None, use_errno=True), os.getpid(),
    )
    library = ctypes.CDLL(None, use_errno=True)
    value = native._DarwinBSDInfo()
    getter = library.proc_pidinfo
    getter.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    getter.restype = ctypes.c_int
    assert getter(os.getpid(), 3, 0, ctypes.byref(value), ctypes.sizeof(value)) == ctypes.sizeof(value)
    assert (seconds, microseconds) == (value.start_seconds, value.start_microseconds)
    assert status in {1, 2, 3, 4}
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


def _install_linux(monkeypatch, *, status=b"S", start=b"123456", returned_pid=PID,
                   boot=BOOT, error=None, raw=None):
    fields = [status] + [b"0"] * 18 + [start]
    payload = (str(returned_pid).encode() + b" (worker (arbitrary) name) "
               + b" ".join(fields)) if raw is None else raw

    def read(path):
        if str(path) == "/proc/sys/kernel/random/boot_id":
            return boot
        assert str(path) == f"/proc/{PID}/stat"
        if error is not None:
            raise OSError(error, "cannot inspect process metadata")
        return payload

    monkeypatch.setattr(native, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(Path, "read_bytes", read)


@pytest.mark.parametrize("status", [b"R", b"S", b"D", b"T", b"t", b"X", b"x",
                                    b"K", b"W", b"P", b"I"])
def test_linux_non_zombie_states_preserve_live_identity(monkeypatch, status):
    _install_linux(monkeypatch, status=status)
    identity = hashlib.sha256(uuid.UUID(BOOT.decode()).bytes + b":123456").hexdigest()
    assert native.process_instance(PID) == identity
    assert not native.instance_ended(PID, identity)


def test_linux_confirmed_zombie_allows_recovery_before_reaping(monkeypatch):
    _install_linux(monkeypatch, status=b"Z")
    with pytest.raises(ProcessLookupError):
        native.process_instance(PID)
    assert native.instance_ended(PID, "0" * 64)


@pytest.mark.parametrize("parameters", [
    {"returned_pid": PID + 1}, {"start": b"-1"}, {"start": b"invalid"},
    {"boot": b"invalid-boot"}, {"status": b"?"}, {"status": b"ZZ"},
    {"raw": b"123 (worker) Z"}, {"raw": b"unreadable process"},
    {"raw": str(PID).encode() + b") Z " + b"0 " * 18 + b"123456"},
])
def test_linux_unverified_zombie_metadata_retains_lock(monkeypatch, parameters):
    _install_linux(monkeypatch, **{"status": b"Z", **parameters})
    assert not native.instance_ended(PID, "0" * 64)


@pytest.mark.parametrize("error", [errno.EPERM, errno.EACCES, errno.EIO])
def test_linux_inaccessible_zombie_metadata_retains_lock(monkeypatch, error):
    _install_linux(monkeypatch, status=b"Z", error=error)
    assert not native.instance_ended(PID, "0" * 64)


def _child_exited_without_reaping(pid):
    if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
        ended = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if ended is None:
            return False
        assert ended.si_pid == pid and ended.si_status == 0 and ended.si_code == os.CLD_EXITED
        return True
    # Some macOS Python builds expose WNOWAIT but not waitid. Query the owned
    # child's state independently of Veqtor without consuming its exit status.
    result = subprocess.run(
        ["/bin/ps", "-o", "pid=,ppid=,stat=", "-p", str(pid)],
        check=True, capture_output=True, text=True, timeout=5,
    )
    child_pid, parent_pid, status = result.stdout.split()
    assert int(child_pid) == pid and int(parent_pid) == os.getpid()
    return status.startswith("Z")


@pytest.mark.skipif(not hasattr(os, "fork") or (
    sys.platform != "darwin" and not (hasattr(os, "waitid") and hasattr(os, "WNOWAIT"))),
    reason="native fork and an independent non-reaping process probe are required")
def test_native_save_recovers_zombie_writer_before_parent_reaps(tmp_path):
    from veqtor_docx import ApplyError
    from veqtor_docx import _drafting_io as publishing

    source = tmp_path / "Agreement.docx"
    original, edited = b"original document bytes", b"edited document bytes"
    source.write_bytes(original)
    ready_read, ready_write = os.pipe()
    exit_read, exit_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(ready_read)
        os.close(exit_write)
        try:
            with publishing._directory(str(tmp_path)) as directory:
                with publishing._writer_lock(directory, str(tmp_path), source.name):
                    os.write(ready_write, b"ready")
                    os.read(exit_read, 1)
                    os._exit(0)  # Bypass cleanup, retaining the actual writer's sidecar.
        except BaseException:
            os._exit(2)
    os.close(ready_write)
    os.close(exit_read)
    try:
        assert select.select([ready_read], [], [], 10)[0], "child did not acquire its lock"
        assert os.read(ready_read, 5) == b"ready"
        identity = native.process_instance(pid)
        lock = tmp_path / publishing._legacy_lock_name(source.name)
        assert lock.read_text().endswith(f"instance={identity}\n")
        assert not native.instance_ended(pid, identity)
        with pytest.raises(ApplyError) as error:
            publishing.publish_document(str(source), None, original, edited)
        assert error.value.code == "file_locked"
        assert source.read_bytes() == original and lock.exists()
        os.write(exit_write, b"x")
        deadline = time.monotonic() + 10
        while not _child_exited_without_reaping(pid):
            assert time.monotonic() < deadline, "child did not exit"
            time.sleep(0.01)
        os.kill(pid, 0)  # The PID still exists; no waitpid/reaping has occurred.
        assert native.instance_ended(pid, identity)
        if sys.platform == "darwin":
            *_, status = native._darwin_sysctl_process_info(ctypes.CDLL(None, use_errno=True), pid)
            assert status == 5  # Confirm the real fallback ABI also reports SZOMB.
        result = publishing.publish_document(str(source), None, original, edited)
        assert source.read_bytes() == edited
        assert Path(result["backup_path"]).read_bytes() == original
        assert not lock.exists()
        assert not list(tmp_path.glob(".veqtor-lock-*.tmp"))
        assert not list(tmp_path.glob(".veqtor-stage-*"))
        assert _child_exited_without_reaping(pid)
    finally:
        os.close(ready_read)
        os.close(exit_write)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
