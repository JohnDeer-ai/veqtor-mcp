# SPDX-License-Identifier: Apache-2.0
"""Native local-filesystem and process-instance checks for drafting saves."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import sys
import uuid
from pathlib import Path


class _DarwinStatFS(ctypes.Structure):
    # sys/mount.h, __DARWIN_STRUCT_STATFS64 (64-bit macOS ABI).
    _fields_ = [
        ("bsize", ctypes.c_uint32), ("iosize", ctypes.c_int32),
        ("blocks", ctypes.c_uint64), ("bfree", ctypes.c_uint64),
        ("bavail", ctypes.c_uint64), ("files", ctypes.c_uint64),
        ("ffree", ctypes.c_uint64), ("fsid", ctypes.c_int32 * 2),
        ("owner", ctypes.c_uint32), ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32), ("subtype", ctypes.c_uint32),
        ("name", ctypes.c_char * 16), ("mount", ctypes.c_char * 1024),
        ("source", ctypes.c_char * 1024), ("extended", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 7),
    ]


class _DarwinBSDInfo(ctypes.Structure):
    # sys/proc_info.h, proc_bsdinfo / PROC_PIDTBSDINFO.
    _fields_ = [
        ("prefix", ctypes.c_uint32 * 12),
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
        ("suffix", ctypes.c_uint32 * 6),
        ("start_seconds", ctypes.c_uint64), ("start_microseconds", ctypes.c_uint64),
    ]


def filesystem_supported(descriptor: int) -> bool:
    """Allow local implementations whose required save primitives are defined.

    Network, FUSE and unfamiliar filesystems fail closed. In particular, local
    PID identity cannot establish ownership of a network filesystem's lock.
    Capability errors from even an allowed filesystem remain save refusals.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        value = _DarwinStatFS()
        function = getattr(libc, "fstatfs$INODE64", libc.fstatfs)
        function.argtypes = (ctypes.c_int, ctypes.POINTER(_DarwinStatFS))
        function.restype = ctypes.c_int
        if function(descriptor, ctypes.byref(value)):
            raise OSError(ctypes.get_errno() or errno.EIO, "cannot inspect save filesystem")
        return bool(value.flags & 0x1000) and value.name in {b"apfs", b"hfs"}  # MNT_LOCAL
    if sys.platform.startswith("linux"):
        # f_type is the first native long in Linux struct statfs. An oversized
        # aligned buffer accommodates the remaining architecture-specific ABI.
        value = (ctypes.c_long * 128)()
        function = libc.fstatfs
        function.argtypes = (ctypes.c_int, ctypes.c_void_p)
        function.restype = ctypes.c_int
        if function(descriptor, ctypes.byref(value)):
            raise OSError(ctypes.get_errno() or errno.EIO, "cannot inspect save filesystem")
        return value[0] & 0xffffffff in {
            0xef53,       # ext2/3/4
            0x58465342,   # XFS
            0x9123683e,   # Btrfs
            0x01021994,   # tmpfs
            0x794c7630,   # overlayfs (local container working trees)
            0x2fc12fc1,   # ZFS
        }
    return False


def process_instance(pid: int) -> str:
    """Identify a process across PID reuse and machine reboots.

    Unavailable information raises OSError and is never treated as death.
    ProcessLookupError is reserved for a PID proven absent by the OS.
    """
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        boot = ctypes.create_string_buffer(128)
        length = ctypes.c_size_t(len(boot))
        sysctl = libc.sysctlbyname
        sysctl.argtypes = (ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                           ctypes.c_void_p, ctypes.c_size_t)
        sysctl.restype = ctypes.c_int
        if sysctl(b"kern.bootsessionuuid", boot, ctypes.byref(length), None, 0):
            raise OSError(ctypes.get_errno() or errno.EIO, "cannot identify machine boot")
        value = _DarwinBSDInfo()
        getter = libc.proc_pidinfo
        getter.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
        getter.restype = ctypes.c_int
        ctypes.set_errno(0)
        count = getter(pid, 3, 0, ctypes.byref(value), ctypes.sizeof(value))
        if count != ctypes.sizeof(value):
            # proc_pidinfo can return 0 without errno for an absent process.
            os.kill(pid, 0)
            raise OSError(ctypes.get_errno() or errno.EIO, "cannot identify process instance")
        if value.prefix[3] != pid or not value.start_seconds or length.value > len(boot):
            raise OSError(errno.EIO, "invalid process instance information")
        boot_value = boot.value
        start_value = f"{value.start_seconds}:{value.start_microseconds}".encode()
    elif sys.platform.startswith("linux"):
        try:
            boot = Path("/proc/sys/kernel/random/boot_id").read_bytes().strip()
            raw = Path(f"/proc/{pid}/stat").read_bytes()
        except FileNotFoundError:
            os.kill(pid, 0)
            raise OSError(errno.EIO, "process instance information unavailable") from None
        # comm can contain spaces and ')'; the fixed fields follow its last ')'.
        try:
            prefix, fields = raw.rsplit(b")", 1)
            if int(prefix.split(b"(", 1)[0]) != pid:
                raise ValueError
            start = fields.split()[19]  # field 22, counted after pid and comm
            if not boot or int(start) < 0:
                raise ValueError
        except (ValueError, IndexError) as exc:
            raise OSError(errno.EIO, "invalid process instance information") from exc
        boot_value, start_value = boot, start
    else:
        raise OSError(errno.ENOTSUP, "process instance identity is unavailable")
    try:
        boot_identity = uuid.UUID(boot_value.decode("ascii")).bytes
    except (ValueError, UnicodeError) as exc:
        raise OSError(errno.EIO, "invalid machine boot identity") from exc
    return hashlib.sha256(boot_identity + b":" + start_value).hexdigest()


def instance_ended(pid: int, identity: str) -> bool:
    try:
        return process_instance(pid) != identity
    except ProcessLookupError:
        return True
    except (OSError, AttributeError, OverflowError):
        return False
