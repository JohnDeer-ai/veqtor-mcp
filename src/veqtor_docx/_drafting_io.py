# SPDX-License-Identifier: Apache-2.0
"""Publish a validated drafting candidate without losing its source snapshot.

In-place saves have a durable, exact-byte backup and an exclusive cooperative
lock. Word's owner file is also checked. These checks detect observed external
changes; they cannot make an uncooperative Word/cloud-sync writer participate
in an atomic compare-and-swap protocol. Callers must close Word before saving.
"""

from __future__ import annotations

import errno
import ctypes
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Iterator

from ._ooxml import UserPathError, resolve_user_path
from .apply import ApplyError
from ._drafting_fs import filesystem_supported, instance_ended, process_instance


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_BACKUP_DIRECTORY = ".veqtor-backups"
_ACL_ATTRIBUTES = frozenset({
    "system.posix_acl_access", "system.posix_acl_default", "system.nfs4_acl",
    "system.richacl", "security.NTACL",
})
_MAX_XATTR_BYTES = 64 * 1024 * 1024
_MAX_XATTR_NAMES_BYTES = 1024 * 1024
_DARWIN_SYSTEM_ATTRIBUTES = frozenset({"com.apple.provenance"})
_UNSUPPORTED_FILESYSTEM_ERRNOS = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})


def _error(code: str, detail: str, **metadata: object) -> ApplyError:
    return ApplyError(code, detail, failure_phase="publication", **metadata)


def _path(value: str) -> str:
    try:
        raw = resolve_user_path(value)
        if not raw:
            raise _error("invalid_path", "document path must not be empty")
        # Resolve the parent only: resolving the leaf would hide a symlink.
        absolute = os.path.abspath(raw)
        return os.path.join(os.path.realpath(os.path.dirname(absolute)),
                            os.path.basename(absolute))
    except UserPathError as exc:
        raise _error(exc.code, exc.detail) from exc


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _version(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode, info.st_nlink, info.st_uid, info.st_gid)


def _regular(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _error("unsafe_path", "document must be a regular file with no symlink or hardlink aliases")


def _at_stat(directory: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=directory, follow_symlinks=False)


def _directory_current(directory: int, path: str) -> None:
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise _error("source_changed", "publication directory is no longer at its captured path") from exc
    if not stat.S_ISDIR(current.st_mode) or _identity(current) != _identity(os.fstat(directory)):
        raise _error("source_changed", "publication directory changed during the operation")


@contextmanager
def _directory(path: str) -> Iterator[int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC)
    try:
        _directory_current(descriptor, path)
        _supported_filesystem(descriptor)
        yield descriptor
    finally:
        os.close(descriptor)


def _supported_filesystem(descriptor: int, parent: int | None = None) -> None:
    try:
        supported = filesystem_supported(descriptor)
    except (OSError, AttributeError) as exc:
        raise _error("unsupported_filesystem", "cannot verify supported local save filesystem") from exc
    if (not supported or (parent is not None
                          and os.fstat(descriptor).st_dev != os.fstat(parent).st_dev)):
        raise _error("unsupported_filesystem", "saving requires a supported local filesystem without nested mounts in private save folders")


def _unlink_owned(directory: int, name: str, identity: tuple[int, int]) -> None:
    """Do not remove an unrelated file after a path was replaced externally."""
    try:
        if _identity(_at_stat(directory, name)) == identity:
            os.unlink(name, dir_fd=directory)
    except OSError:
        pass


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        count = os.write(descriptor, remaining)
        if count <= 0:
            raise OSError(errno.EIO, "short document write")
        remaining = remaining[count:]


def _word_unlocked(directory: int, basename: str) -> None:
    # Depending on the filename/Word version, the owner prefix is prepended or
    # replaces one or two leading characters. Native Word prepends the prefix
    # for one/two-character stems; truncated probes such as '~$1.docx' or
    # '~$.docx' would falsely lock unrelated short documents in the directory.
    stem = os.path.splitext(basename)[0]
    offsets = (0,) if len(stem) <= 2 else (0, 1, 2)
    for name in {"~$" + basename[offset:] for offset in offsets}:
        try:
            _at_stat(directory, name)
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno == errno.ENAMETOOLONG:
                # A prefix may exceed NAME_MAX for an otherwise legal source;
                # such an owner entry cannot exist in this directory.
                continue
            raise
        raise _error("file_locked", "Word's owner file is present; close the document before saving")


def _legacy_lock_name(basename: str) -> str:
    token = hashlib.sha256(unicodedata.normalize("NFC", basename).casefold().encode()).hexdigest()[:24]
    return f".veqtor-{token}.lock"


def _reclaim_dead_lock(directory: int, name: str, *, require_instance: bool = False) -> bool:
    """Under directory flock, reclaim only an unchanged, owned dead-PID lock."""
    try:
        descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | os.O_NONBLOCK,
                             dir_fd=directory)
    except FileNotFoundError:
        return True  # The old writer released its lock before we opened it.
    except OSError:
        return False
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600
                or not 1 <= before.st_size <= 256):
            return False
        payload = os.read(descriptor, 257)
        match = re.fullmatch(rb"pid=([1-9][0-9]{0,9}) token=[0-9a-f]{32}(?: instance=([0-9a-f]{64}))?\n", payload)
        if (not match or (require_instance and match[2] is None)
                or _version(os.fstat(descriptor)) != _version(before)):
            return False
        pid = int(match[1])
        if pid > 2_147_483_647:
            return False
        if match[2] is not None:
            if not instance_ended(pid, match[2].decode()):
                return False
        else:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            except (OSError, OverflowError):
                return False  # Legacy records cannot distinguish reused PIDs.
            else:
                return False
        if _version(_at_stat(directory, name)) != _version(before):
            return False
        _unlink_owned(directory, name, _identity(before))
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)


@contextmanager
def _compatible_sidecar(directory: int, basename: str, path: str) -> Iterator[None]:
    """Publish a complete PID record atomically for earlier O_EXCL writers.

    A crash before publication may leave an inert unique temporary file, never
    an empty active lock. No-replace rename keeps the published inode singly
    linked, so a crash immediately afterward is recoverable by dead-PID checks.
    """
    name = _legacy_lock_name(basename)
    temporary = f".veqtor-lock-{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                         0o600, dir_fd=directory)
    identity = _identity(os.fstat(descriptor))
    try:
        instance = process_instance(os.getpid())
        _write_all(descriptor, f"pid={os.getpid()} token={uuid.uuid4().hex} instance={instance}\n".encode())
        os.fsync(descriptor)
        for attempt in range(2):
            try:
                _rename_noreplace(directory, temporary, name)
                break
            except FileExistsError as exc:
                if attempt or not _reclaim_dead_lock(directory, name):
                    raise _error("file_locked", "another save or an unverifiable legacy save lock exists",
                                 lock_path=os.path.join(path, name),
                                 lock_reason="owner_not_proven_dead") from exc
        yield
    finally:
        os.close(descriptor)
        # Check both names even if an exception interrupted immediately after
        # the native rename returned; never depend on a later Python flag write.
        _unlink_owned(directory, name, identity)
        _unlink_owned(directory, temporary, identity)


def _rename_noreplace(directory: int, source: str, destination: str,
                      *, destination_directory: int | None = None) -> None:
    """Atomically publish an initialized lock without replacing an old writer."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            rename, flag = libc.renameatx_np, 0x00000004  # RENAME_EXCL, sys/stdio.h
        elif sys.platform.startswith("linux"):
            rename, flag = libc.renameat2, 1  # RENAME_NOREPLACE, linux/fs.h
        else:
            raise OSError(errno.ENOTSUP, "atomic lock publication is unsupported")
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
    except (AttributeError, OSError) as exc:
        raise OSError(errno.ENOTSUP, "atomic lock publication is unavailable") from exc
    ctypes.set_errno(0)
    target = directory if destination_directory is None else destination_directory
    if rename(directory, os.fsencode(source), target, os.fsencode(destination), flag) != 0:
        raise OSError(ctypes.get_errno() or errno.EIO, "atomic lock publication failed")


def _recover_lock_temporaries(directory: int) -> None:
    # Only a complete modern ownership record proves an unpublished sidecar is
    # ours. Empty/partial bootstrap files have no such proof and remain intact.
    with os.scandir(directory) as entries:
        for entry in entries:
            if re.fullmatch(r"\.veqtor-lock-[0-9a-f]{32}\.tmp", entry.name):
                _reclaim_dead_lock(directory, entry.name, require_instance=True)


@contextmanager
def _writer_lock(directory: int, path: str, basename: str) -> Iterator[None]:
    """Serialize saves in one directory, including earlier dev0 writers.

    flock belongs to this open directory description and the OS releases it
    after a crash. Keeping the directory itself as the lock object avoids the
    split-lock race between current writers. A compatible sidecar also excludes
    older writers. Only a well-formed, owned sidecar with a provably dead PID
    can be reclaimed under flock; malformed or unverifiable files remain intact.
    """
    _directory_current(directory, path)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
            raise _error("file_locked", "another Veqtor save is active in this folder") from exc
        raise
    try:
        _directory_current(directory, path)
        _recover_lock_temporaries(directory)
        with _compatible_sidecar(directory, basename, path):
            _recover_stages(directory)
            yield
            _directory_current(directory, path)
    finally:
        fcntl.flock(directory, fcntl.LOCK_UN)


def _source_current(directory: int, basename: str, descriptor: int,
                    expected: bytes, version: tuple[int, ...] | None = None) -> os.stat_result:
    _word_unlocked(directory, basename)
    try:
        path_info = _at_stat(directory, basename)
    except FileNotFoundError as exc:
        raise _error("source_changed", "source document disappeared during publication") from exc
    _regular(path_info)
    before = os.fstat(descriptor)
    if _version(path_info) != _version(before) or (version is not None and _version(before) != version):
        raise _error("source_changed", "source identity or metadata changed during publication")
    _regular(before)
    os.lseek(descriptor, 0, os.SEEK_SET)
    observed = bytearray()
    # Read at most one byte beyond the snapshot rather than unbounded new data.
    while len(observed) <= len(expected):
        chunk = os.read(descriptor, min(1024 * 1024, len(expected) + 1 - len(observed)))
        if not chunk:
            break
        observed.extend(chunk)
    after = os.fstat(descriptor)
    if _version(after) != _version(before) or _version(_at_stat(directory, basename)) != _version(after):
        raise _error("source_changed", "source changed while its bytes were being verified")
    if observed != expected:
        raise _error("file_sha256_mismatch", "source bytes no longer match the validated snapshot",
                     expected_source_sha256=hashlib.sha256(expected).hexdigest())
    return after


def _darwin_acl_present(descriptor: int, *, inheritable_only: bool = False) -> bool:
    """Darwin ACLs are native metadata, not entries returned by listxattr.

    ACL_TYPE_EXTENDED is 0x100 in macOS sys/acl.h. acl_get_fd_np returns
    ENOENT when no extended ACL exists. Files and private save directories
    reject any existing ACL. A publication parent only needs the narrower
    inheritance check: its own non-inheritable permissions remain enforced
    by the kernel when creating or replacing children.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        getter, release = libc.acl_get_fd_np, libc.acl_free
        getter.argtypes = (ctypes.c_int, ctypes.c_int)
        getter.restype = ctypes.c_void_p
        release.argtypes = (ctypes.c_void_p,)
        release.restype = ctypes.c_int
    except (AttributeError, OSError) as exc:
        raise _error("unsupported_permissions", "cannot verify native macOS access-control metadata") from exc
    ctypes.set_errno(0)
    acl = getter(descriptor, 0x100)
    if acl:
        try:
            if not inheritable_only:
                return True
            try:
                validate = libc.acl_valid
                entry_getter = libc.acl_get_entry
                flagset_getter = libc.acl_get_flagset_np
                flag_getter = libc.acl_get_flag_np
                validate.argtypes, validate.restype = (ctypes.c_void_p,), ctypes.c_int
                entry_getter.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                         ctypes.POINTER(ctypes.c_void_p))
                entry_getter.restype = ctypes.c_int
                flagset_getter.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
                flagset_getter.restype = ctypes.c_int
                flag_getter.argtypes = (ctypes.c_void_p, ctypes.c_int)
                flag_getter.restype = ctypes.c_int
            except (AttributeError, OSError) as exc:
                raise _error("unsupported_permissions", "cannot verify native macOS ACL inheritance") from exc
            if validate(acl) != 0:
                raise _error("unsupported_permissions", "cannot verify native macOS ACL inheritance")
            # Darwin returns 0 for an entry and -1/EINVAL for an index past
            # the end of a valid ACL. ACL_MAX_ENTRIES is 128 in sys/acl.h.
            for index in range(129):
                entry = ctypes.c_void_p()
                ctypes.set_errno(0)
                result = entry_getter(acl, index, ctypes.byref(entry))
                if result == -1 and ctypes.get_errno() == errno.EINVAL:
                    return False
                if result != 0 or not entry.value:
                    raise _error("unsupported_permissions", "cannot verify native macOS ACL inheritance")
                flags = ctypes.c_void_p()
                if flagset_getter(entry, ctypes.byref(flags)) != 0 or not flags.value:
                    raise _error("unsupported_permissions", "cannot verify native macOS ACL inheritance")
                # FILE_INHERIT / DIRECTORY_INHERIT, not ENTRY_INHERITED:
                # the latter only describes where this parent's entry came from.
                for flag in (1 << 5, 1 << 6):
                    value = flag_getter(flags, flag)
                    if value not in {0, 1}:
                        raise _error("unsupported_permissions", "cannot verify native macOS ACL inheritance")
                    if value:
                        return True
            raise _error("unsupported_permissions", "cannot verify native macOS ACL inheritance")
        finally:
            release(acl)
    error = ctypes.get_errno()
    if error in {errno.ENOENT, errno.ENOTSUP, errno.EOPNOTSUPP}:
        return False
    raise _error("unsupported_permissions", "cannot verify native macOS access-control metadata")


def _check_parent_acl(descriptor: int) -> None:
    if sys.platform != "darwin":
        _attributes(descriptor)
    elif _darwin_acl_present(descriptor, inheritable_only=True):
        raise _error("unsupported_permissions", "inherited access-control lists cannot yet be preserved safely")


def _darwin_xattr_api():
    """Use libSystem's fd API: standard macOS Python has no os.*xattr APIs."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        listing, getter, setter = libc.flistxattr, libc.fgetxattr, libc.fsetxattr
        listing.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
        listing.restype = ctypes.c_ssize_t
        signature = (ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
                     ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int)
        getter.argtypes = setter.argtypes = signature
        getter.restype, setter.restype = ctypes.c_ssize_t, ctypes.c_int
        return listing, getter, setter
    except (AttributeError, OSError) as exc:
        raise _error("unsupported_permissions", "cannot preserve native macOS extended attributes") from exc


def _native_xattr_result(value: int) -> int:
    if value < 0:
        raise OSError(ctypes.get_errno() or errno.EIO, "native extended-attribute operation failed")
    return value


def _darwin_attributes(descriptor: int) -> dict[str, bytes]:
    listing, getter, _ = _darwin_xattr_api()
    size = _native_xattr_result(listing(descriptor, None, 0, 0))
    if size > _MAX_XATTR_NAMES_BYTES:
        raise _error("unsupported_permissions", "extended-attribute names exceed the safe copy limit")
    if size == 0:
        return {}
    buffer = ctypes.create_string_buffer(size)
    count = _native_xattr_result(listing(descriptor, buffer, size, 0))
    if count > size or (count and buffer.raw[count - 1] != 0):
        raise _error("unsupported_permissions", "extended-attribute names changed while reading")
    names = buffer.raw[:count].split(b"\x00")[:-1]
    if len(names) != len(set(names)) or any(not name for name in names):
        raise _error("unsupported_permissions", "extended-attribute names are ambiguous")
    values = {}
    total = 0
    for name in names:
        size = _native_xattr_result(getter(descriptor, name, None, 0, 0, 0))
        total += size
        if total > _MAX_XATTR_BYTES:
            raise _error("unsupported_permissions", "extended attributes exceed the safe copy limit")
        buffer = ctypes.create_string_buffer(max(size, 1))
        count = _native_xattr_result(getter(descriptor, name, buffer, size, 0, 0))
        if count != size:
            raise _error("source_changed", "extended attributes changed while reading")
        values[os.fsdecode(name)] = buffer.raw[:count]
    return values


def _attributes(descriptor: int) -> dict[str, bytes]:
    if sys.platform == "darwin":
        if _darwin_acl_present(descriptor):
            raise _error("unsupported_permissions", "extended access-control lists cannot yet be preserved safely")
        values = _darwin_attributes(descriptor)
    elif hasattr(os, "listxattr"):
        names = os.listxattr(descriptor)
        values = {name: os.getxattr(descriptor, name) for name in names}
    else:
        raise _error("unsupported_permissions", "cannot verify extended attributes on this platform")
    # Linux POSIX ACLs are normally exposed as system.posix_acl_* xattrs.
    # Refuse those (and known richer ACL formats) rather than relying on a
    # chmod/copy ordering that could change their effective permissions.
    if _ACL_ATTRIBUTES.intersection(values):
        raise _error("unsupported_permissions", "extended access-control lists cannot yet be preserved safely")
    if sys.platform == "darwin":
        # macOS manages provenance for the creating process. Setting it may
        # succeed without reproducing its value; it is not user metadata or an
        # ACL. Preserve and verify every other attribute, including Finder tags,
        # quarantine and macl, while leaving provenance to the operating system.
        values = {name: value for name, value in values.items()
                  if name not in _DARWIN_SYSTEM_ATTRIBUTES}
    return values


def _set_attributes(descriptor: int, attributes: dict[str, bytes]) -> None:
    if sys.platform == "darwin":
        _, _, setter = _darwin_xattr_api()
        for name, value in attributes.items():
            buffer = ctypes.create_string_buffer(value)
            _native_xattr_result(setter(descriptor, os.fsencode(name), buffer, len(value), 0, 0))
    else:
        for name, value in attributes.items():
            os.setxattr(descriptor, name, value)


_STAGE_NAME = re.compile(r"\.veqtor-stage-[0-9a-f]{32}\Z")
_CLEANUP_NAME = re.compile(r"\.veqtor-cleanup-([0-9a-f]{32})\.json\Z")
_STAGE_FILE = "candidate.tmp"
_STAGE_MANIFEST = "owner.json"


def _stage_manifest(directory: int, name: str = _STAGE_MANIFEST,
                    *, directory_identity: tuple[int, int] | None = None) -> tuple[dict, os.stat_result] | None:
    """Read a bounded private ownership record without following any link."""
    descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | os.O_NONBLOCK,
                         dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600
                or not 1 <= before.st_size <= 4096):
            return None
        value = json.loads(os.read(descriptor, 4097))
        if (_version(before) != _version(os.fstat(descriptor))
                or _version(before) != _version(_at_stat(directory, name))):
            return None
        keys = {"version", "pid", "instance", "directory", "file", "size", "sha256", "uid", "gid", "mode"}
        if (not isinstance(value, dict) or set(value) != keys
                or type(value["version"]) is not int or value["version"] != 1):
            return None
        if any(type(value[key]) is not int for key in ("pid", "size", "uid", "gid", "mode")):
            return None
        if not 1 <= value["pid"] <= 2_147_483_647 or not 0 <= value["size"] <= 100 * 1024 * 1024:
            return None
        if value["uid"] < 0 or value["gid"] < 0 or not 0 <= value["mode"] <= 0o7777:
            return None
        if any(not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key])
               for key in ("instance", "sha256")):
            return None
        for key in ("directory", "file"):
            if (not isinstance(value[key], list) or len(value[key]) != 2
                    or any(type(number) is not int or number < 0 for number in value[key])):
                return None
        if directory_identity is not None and tuple(value["directory"]) != directory_identity:
            return None
        return value, before
    finally:
        os.close(descriptor)


def _finish_stage_cleanup(parent: int, name: str, directory: int,
                          directory_identity: tuple[int, int], manifest_info: os.stat_result) -> None:
    """Retain the ownership record until the now-empty stage is gone.

    Moving the existing record is atomic. A crash before the move leaves the
    ordinary manifest; a crash afterward leaves a verifiable cleanup record.
    Never replace another cleanup entry or delete a changed/replaced directory.
    """
    if (not _STAGE_NAME.fullmatch(name)
            or _identity(_at_stat(parent, name)) != directory_identity
            or _identity(os.fstat(directory)) != directory_identity
            or set(os.listdir(directory)) != {_STAGE_MANIFEST}
            or _version(_at_stat(directory, _STAGE_MANIFEST)) != _version(manifest_info)):
        return
    cleanup = f".veqtor-cleanup-{name.removeprefix('.veqtor-stage-')}.json"
    _rename_noreplace(directory, _STAGE_MANIFEST, cleanup, destination_directory=parent)
    moved_info = _at_stat(parent, cleanup)
    # Rename changes ctime; every other captured identity/content attribute
    # must still match. Recheck the moved record before its eventual unlink.
    expected, moved = _version(manifest_info), _version(moved_info)
    if expected[:4] + expected[5:] != moved[:4] + moved[5:]:
        return
    try:
        os.fsync(parent)
    except OSError:
        # The empty directory can still be removed. The ownership record must
        # remain unless a subsequent parent sync confirms that removal.
        pass
    if (_identity(_at_stat(parent, name)) != directory_identity
            or os.listdir(directory)):
        return
    os.rmdir(name, dir_fd=parent)
    os.fsync(parent)
    if _version(_at_stat(parent, cleanup)) == _version(moved_info):
        _unlink_owned(parent, cleanup, _identity(manifest_info))
        os.fsync(parent)


def _recover_cleanup(parent: int, name: str) -> None:
    match = _CLEANUP_NAME.fullmatch(name)
    if match is None:
        return
    record = _stage_manifest(parent, name)
    if record is None:
        return
    value, manifest_info = record
    if not instance_ended(value["pid"], value["instance"]):
        return
    stage = f".veqtor-stage-{match[1]}"
    try:
        before = _at_stat(parent, stage)
    except FileNotFoundError:
        before = None
    if before is not None:
        if (not stat.S_ISDIR(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o700
                or _identity(before) != tuple(value["directory"])):
            return
        directory = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC,
                            dir_fd=parent)
        try:
            if _identity(os.fstat(directory)) != _identity(before):
                return
            _supported_filesystem(directory, parent)
            _attributes(directory)
            if (os.listdir(directory)
                    or _identity(_at_stat(parent, stage)) != _identity(before)
                    or _version(_at_stat(parent, name)) != _version(manifest_info)):
                return
            os.rmdir(stage, dir_fd=parent)
        finally:
            os.close(directory)
    # Persist directory removal before losing the last ownership record. This
    # also completes recovery after a crash immediately following rmdir.
    os.fsync(parent)
    if _version(_at_stat(parent, name)) == _version(manifest_info):
        _unlink_owned(parent, name, _identity(manifest_info))
        os.fsync(parent)


def _recover_stage(parent: int, name: str) -> None:
    before = _at_stat(parent, name)
    if (not stat.S_ISDIR(before.st_mode) or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o700):
        return
    directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=parent)
    try:
        if _identity(before) != _identity(os.fstat(directory)):
            return
        _supported_filesystem(directory, parent)
        # An inherited ACL could give other principals access despite mode 700.
        _attributes(directory)
        record = _stage_manifest(directory, directory_identity=_identity(before))
        if record is None:
            return
        value, manifest_info = record
        if not instance_ended(value["pid"], value["instance"]):
            return
        names = set(os.listdir(directory))
        if names not in ({_STAGE_MANIFEST}, {_STAGE_MANIFEST, _STAGE_FILE}):
            return
        file_info = None
        if _STAGE_FILE in names:
            descriptor = os.open(_STAGE_FILE, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | os.O_NONBLOCK,
                                 dir_fd=directory)
            try:
                file_info = os.fstat(descriptor)
                if (not stat.S_ISREG(file_info.st_mode) or _identity(file_info) != tuple(value["file"])
                        or file_info.st_size > value["size"] or file_info.st_nlink not in {1, 2}
                        or file_info.st_uid not in {os.geteuid(), value["uid"]}):
                    return
                _attributes(descriptor)
                if file_info.st_size == value["size"]:
                    digest = hashlib.sha256()
                    remaining = value["size"] + 1
                    while remaining and (chunk := os.read(descriptor, min(remaining, 1024 * 1024))):
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if digest.hexdigest() != value["sha256"]:
                        return  # A complete but changed candidate is not ours to discard.
                elif (file_info.st_nlink != 1 or file_info.st_uid != os.geteuid()
                      or stat.S_IMODE(file_info.st_mode) != 0o600):
                    return
                # A partial write has no final digest. Its exact registered inode
                # is removable only inside this untouched private container,
                # before permissions or publication links could have changed.
                if (_version(file_info) != _version(os.fstat(descriptor))
                        or _version(file_info) != _version(_at_stat(directory, _STAGE_FILE))):
                    return
            finally:
                os.close(descriptor)
        if (_identity(_at_stat(parent, name)) != _identity(before)
                or _version(_at_stat(directory, _STAGE_MANIFEST)) != _version(manifest_info)
                or set(os.listdir(directory)) != names):
            return
        if file_info is not None:
            if _version(_at_stat(directory, _STAGE_FILE)) != _version(file_info):
                return
            _unlink_owned(directory, _STAGE_FILE, _identity(file_info))
            if _STAGE_FILE in os.listdir(directory):
                return
        _finish_stage_cleanup(parent, name, directory, _identity(before), manifest_info)
    finally:
        os.close(directory)


def _recover_stages(directory: int) -> None:
    # The owning save holds this directory's writer lock (or its parent lock
    # for the private backup folder). Old unregistered .tmp files are untouched.
    with os.scandir(directory) as entries:
        for entry in entries:
            if _STAGE_NAME.fullmatch(entry.name) or _CLEANUP_NAME.fullmatch(entry.name):
                try:
                    if _CLEANUP_NAME.fullmatch(entry.name):
                        _recover_cleanup(directory, entry.name)
                    else:
                        _recover_stage(directory, entry.name)
                except (OSError, ApplyError, ValueError, TypeError, KeyError):
                    continue  # Unverifiable contents are retained, never guessed away.


@dataclass(frozen=True)
class _Stage:
    directory: int
    identity: tuple[int, int]
    name: str = _STAGE_FILE


@contextmanager
def _staged(parent: int, payload: bytes, source_info: os.stat_result,
            attributes: dict[str, bytes]) -> Iterator[_Stage]:
    # A private child must not silently bypass restrictions inherited by a
    # direct output in this folder (including file-only ACL inheritance).
    _check_parent_acl(parent)
    name = f".veqtor-stage-{uuid.uuid4().hex}"
    os.mkdir(name, 0o700, dir_fd=parent)
    directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=parent)
    directory_identity = _identity(os.fstat(directory))
    descriptor = None
    identity = None
    manifest_identity = None
    try:
        _supported_filesystem(directory, parent)
        _attributes(directory)
        descriptor = os.open(_STAGE_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                             0o600, dir_fd=directory)
        identity = _identity(os.fstat(descriptor))
        # Ownership is durable before the first document byte is written. The
        # dedicated mode-700 directory makes even interrupted partial writes
        # identifiable without deleting unrelated same-directory temporary files.
        record = {
            "version": 1, "pid": os.getpid(), "instance": process_instance(os.getpid()),
            "directory": list(directory_identity), "file": list(identity),
            "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            "uid": source_info.st_uid, "gid": source_info.st_gid,
            "mode": stat.S_IMODE(source_info.st_mode),
        }
        manifest = os.open(_STAGE_MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                           0o600, dir_fd=directory)
        try:
            manifest_identity = _identity(os.fstat(manifest))
            _write_all(manifest, json.dumps(record, separators=(",", ":")).encode())
            os.fsync(manifest)
        finally:
            os.close(manifest)
        os.fsync(directory)
        os.fsync(parent)
        _attributes(descriptor)
        _write_all(descriptor, payload)
        actual = os.fstat(descriptor)
        if (actual.st_uid, actual.st_gid) != (source_info.st_uid, source_info.st_gid):
            os.fchown(descriptor, source_info.st_uid, source_info.st_gid)
        _set_attributes(descriptor, attributes)
        os.fchmod(descriptor, stat.S_IMODE(source_info.st_mode))
        if _attributes(descriptor) != attributes:
            raise _error("unsupported_permissions", "extended attributes could not be preserved exactly")
        os.fsync(descriptor)
        yield _Stage(directory, identity)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if identity is not None:
            _unlink_owned(directory, _STAGE_FILE, identity)
        try:
            registered = None
            if manifest_identity is not None:
                try:
                    registered = _stage_manifest(directory, directory_identity=directory_identity)
                except (OSError, ValueError, TypeError, KeyError):
                    pass
            if registered is not None and _identity(registered[1]) == manifest_identity:
                _finish_stage_cleanup(parent, name, directory, directory_identity, registered[1])
            else:
                # This live writer still knows the exact inodes it created if
                # initialization failed. Dead-writer recovery cannot infer that
                # ownership for an absent/partial manifest and preserves it.
                if manifest_identity is not None:
                    _unlink_owned(directory, _STAGE_MANIFEST, manifest_identity)
                if _identity(_at_stat(parent, name)) == directory_identity and not os.listdir(directory):
                    os.rmdir(name, dir_fd=parent)
        except OSError:
            pass  # Cleanup must not mask the publication outcome/recovery path.
        finally:
            os.close(directory)


@contextmanager
def _backup_directory(parent: int) -> Iterator[int]:
    try:
        os.mkdir(_BACKUP_DIRECTORY, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    info = _at_stat(parent, _BACKUP_DIRECTORY)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise _error("unsafe_path", "backup directory must be an owned directory without symlinks or shared write access")
    descriptor = os.open(_BACKUP_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC,
                         dir_fd=parent)
    try:
        if _identity(info) != _identity(os.fstat(descriptor)):
            raise _error("source_changed", "backup directory changed while opening")
        _supported_filesystem(descriptor, parent)
        _attributes(descriptor)
        _recover_stages(descriptor)
        # Persist a newly created directory before relying on its backup.
        os.fsync(parent)
        yield descriptor
    finally:
        os.close(descriptor)


def _backup_current(parent: int, descriptor: int) -> None:
    try:
        info = _at_stat(parent, _BACKUP_DIRECTORY)
    except OSError as exc:
        raise _error("source_changed", "backup directory disappeared before publication") from exc
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022 or _identity(info) != _identity(os.fstat(descriptor))):
        raise _error("source_changed", "backup directory changed before publication")
    _supported_filesystem(descriptor, parent)
    _attributes(descriptor)


def _backup(parent: int, directory: int, basename: str, payload: bytes,
            source_info: os.stat_result, attributes: dict[str, bytes]) -> str:
    # Keep the component below NAME_MAX even with a long Unicode document name.
    short_name = os.fsencode(basename)[:140].decode(errors="ignore")
    name = f"{short_name}.{hashlib.sha256(payload).hexdigest()[:16]}.{uuid.uuid4().hex}.docx"
    with _staged(directory, payload, source_info, attributes) as staged:
        _backup_current(parent, directory)
        os.link(staged.name, name, src_dir_fd=staged.directory, dst_dir_fd=directory, follow_symlinks=False)
        try:
            os.fsync(directory)
        except BaseException:
            _unlink_owned(directory, name, staged.identity)
            raise
    return name


def _backup_path_verified(source_parent: str, backup_path: str | None, payload: bytes,
                          parent_identity: tuple[int, int] | None,
                          backup_identity: tuple[int, int] | None) -> bool:
    """Verify the last known recovery path without following replacement links.

    A parent directory may have been renamed after the backup was completed.
    In that case the backup can still exist elsewhere, but this path is not a
    verified recovery location. Never search other directories for it.
    """
    if backup_path is None or parent_identity is None or backup_identity is None:
        return False
    try:
        with _directory(source_parent) as parent:
            if _identity(os.fstat(parent)) != parent_identity:
                return False
            backups = os.open(_BACKUP_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC,
                              dir_fd=parent)
            try:
                directory_identity = _identity(os.fstat(backups))
                name = os.path.basename(backup_path)
                descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | os.O_NONBLOCK,
                                     dir_fd=backups)
                try:
                    before = os.fstat(descriptor)
                    _regular(before)
                    if _identity(before) != backup_identity or before.st_size != len(payload):
                        return False
                    digest = hashlib.sha256()
                    remaining = len(payload)
                    while remaining:
                        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                        if not chunk:
                            return False
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if (_version(os.fstat(descriptor)) != _version(before)
                            or _version(_at_stat(backups, name)) != _version(before)
                            or _identity(_at_stat(parent, _BACKUP_DIRECTORY)) != directory_identity):
                        return False
                    _directory_current(parent, source_parent)
                    return digest.digest() == hashlib.sha256(payload).digest()
                finally:
                    os.close(descriptor)
            finally:
                os.close(backups)
    except (ApplyError, OSError):
        return False


def publish_document(source_path: str, output_path: str | None, source_payload: bytes,
                     candidate_payload: bytes) -> dict:
    """Publish validated bytes, either as a new copy or a backed-up in-place save.

    ``None`` or the same normalized source path chooses ``in_place``. A distinct
    output is always create-only. A completed backup remains available if a
    later save fails. After publication, a durability error reports
    ``publication_completed=True`` rather than pretending the file is unchanged.
    Failure metadata reports ``backup_path_verified=False`` when its last known
    backup path cannot be confirmed (for example, after a directory rename).
    """
    if not isinstance(source_payload, bytes) or not isinstance(candidate_payload, bytes):
        raise _error("invalid_request", "source and candidate payloads must be bytes")
    source = _path(source_path)
    output = source if output_path is None else _path(output_path)
    in_place = output == source
    source_parent, basename = os.path.split(source)
    output_parent, output_name = os.path.split(output)
    backup_path: str | None = None
    parent_identity: tuple[int, int] | None = None
    backup_identity: tuple[int, int] | None = None
    publication_completed = False
    try:
        with _directory(source_parent) as parent, _writer_lock(parent, source_parent, basename):
            parent_identity = _identity(os.fstat(parent))
            _word_unlocked(parent, basename)
            _regular(_at_stat(parent, basename))
            source_descriptor = os.open(basename, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | os.O_NONBLOCK,
                                        dir_fd=parent)
            try:
                source_info = _source_current(parent, basename, source_descriptor, source_payload)
                version = _version(source_info)
                attributes = _attributes(source_descriptor)
                with _directory(output_parent) as destination:
                    if not in_place:
                        try:
                            _at_stat(destination, output_name)
                        except FileNotFoundError:
                            pass
                        else:
                            # Case-insensitive volumes can resolve different
                            # spellings to this very inode. Never infer aliases
                            # from casefold on a case-sensitive filesystem.
                            existing = _at_stat(destination, output_name)
                            if (stat.S_ISREG(existing.st_mode) and existing.st_nlink == 1
                                    and _identity(existing) == _identity(source_info)
                                    and _identity(os.fstat(destination)) == _identity(os.fstat(parent))):
                                in_place = True
                            else:
                                raise _error("output_exists", "refusing to overwrite an existing output")
                    destination_guard = (nullcontext() if _identity(os.fstat(destination)) == _identity(os.fstat(parent))
                                         else _writer_lock(destination, output_parent, output_name))
                    with destination_guard, _staged(destination, candidate_payload, source_info, attributes) as staged:
                        if in_place:
                            try:
                                with _backup_directory(parent) as backups:
                                    backup_name = _backup(parent, backups, basename, source_payload,
                                                          source_info, attributes)
                                    backup_path = os.path.join(source_parent, _BACKUP_DIRECTORY, backup_name)
                                    backup_identity = _identity(_at_stat(backups, backup_name))
                                    _directory_current(parent, source_parent)
                                    _directory_current(destination, output_parent)
                                    _backup_current(parent, backups)
                                    _source_current(parent, basename, source_descriptor, source_payload, version)
                                    _check_parent_acl(destination)
                                    os.replace(staged.name, output_name, src_dir_fd=staged.directory, dst_dir_fd=parent)
                                    publication_completed = True
                            except ApplyError:
                                raise
                            except OSError as exc:
                                code = ("unsupported_filesystem" if exc.errno in _UNSUPPORTED_FILESYSTEM_ERRNOS
                                        else "backup_failed" if backup_path is None else "output_unwritable")
                                raise _error(code, str(exc)) from exc
                        else:
                            _directory_current(parent, source_parent)
                            _directory_current(destination, output_parent)
                            _source_current(parent, basename, source_descriptor, source_payload, version)
                            _check_parent_acl(destination)
                            try:
                                os.link(staged.name, output_name, src_dir_fd=staged.directory,
                                        dst_dir_fd=destination, follow_symlinks=False)
                            except FileExistsError as exc:
                                raise _error("output_exists", "output appeared before publication") from exc
                            publication_completed = True
                        os.fsync(destination)
            finally:
                os.close(source_descriptor)
    except (ApplyError, OSError) as exc:
        if publication_completed:
            exc = _error("publication_uncertain",
                         "document was published, but final durability confirmation failed; verify the working file before retrying",
                         original_code=getattr(exc, "code", "output_unwritable"))
        elif not isinstance(exc, ApplyError):
            code = "unsupported_filesystem" if exc.errno in _UNSUPPORTED_FILESYSTEM_ERRNOS else "output_unwritable"
            exc = _error(code, str(exc))
        exc.metadata.update(output_path=output, backup_path=backup_path,
                            backup_sha256=hashlib.sha256(source_payload).hexdigest() if backup_path else None,
                            backup_path_verified=_backup_path_verified(
                                source_parent, backup_path, source_payload, parent_identity, backup_identity),
                            publication_completed=publication_completed)
        raise exc
    return {
        "output_path": output,
        "backup_path": backup_path,
        "backup_sha256": hashlib.sha256(source_payload).hexdigest() if backup_path else None,
        "save_mode": "in_place" if in_place else "copy",
    }
