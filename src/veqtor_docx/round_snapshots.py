# SPDX-License-Identifier: Apache-2.0
"""Internal, unassigned exact-byte DOCX snapshots; no round or sent-role inference.

The store is create-only, not tamper-proof. Its owner can modify it; loading
rechecks the complete manifest and package. Descriptor/version checks detect
observed source drift, not an atomic snapshot of an uncooperative live writer.
Publication uses a private sibling staging directory inside the explicitly
chosen destination parent. Crashed stages are not automatically reclaimed.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ._drafting_io import (
    _attributes,
    _identity,
    _rename_noreplace,
    _supported_filesystem,
    _unlink_owned,
    _version,
    _write_all,
    require_editable_path,
)
from ._ooxml import (
    DOCUMENT_PART,
    MAX_DOCX_INPUT_BYTES,
    DocxError,
    ResourceLimitError,
    UserPathError,
    load_validated_docx,
    parse_xml,
    require_single_direct_document_body,
    resolve_user_path,
)

_SCHEMA = "round_snapshot.v1"
_FILES = frozenset({"source.docx", "manifest.json"})
_MANIFEST_LIMIT = 4096
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class SnapshotError(DocxError):
    """Controlled snapshot refusal, with publication state when applicable."""

    def __init__(self, code: str, detail: str, **metadata: object):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.metadata = metadata


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class DocumentSnapshot:
    payload: bytes
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if (type(self.payload) is not bytes or not _digest(self.sha256)
                or type(self.size_bytes) is not int
                or self.size_bytes != len(self.payload) or self.sha256 != _sha(self.payload)):
            raise SnapshotError("invalid_snapshot", "snapshot bytes, digest and size must agree")


@dataclass(frozen=True)
class SnapshotPublication:
    path: str
    sha256: str
    size_bytes: int
    status: str = "published"


def _path(value: str | os.PathLike[str]) -> str:
    try:
        if not os.fspath(value):
            raise SnapshotError("invalid_path", "path must not be empty")
        raw = resolve_user_path(value)
        if ".." in Path(raw).parts:
            raise SnapshotError("invalid_path", "parent-traversal components are unsupported")
        result = os.path.abspath(raw)
    except (TypeError, ValueError, UserPathError) as exc:
        if isinstance(exc, SnapshotError):
            raise
        raise SnapshotError("invalid_path", "path must be a valid text path") from exc
    if result == os.path.sep:
        raise SnapshotError("invalid_path", "a snapshot target cannot be the filesystem root")
    return result


@dataclass
class _Directory:
    descriptor: int
    chain: list[tuple[int, str, int]]

    def check(self) -> None:
        for parent, name, child in self.chain:
            try:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError as exc:
                raise SnapshotError("source_changed", "a containing directory changed") from exc
            if not stat.S_ISDIR(current.st_mode) or _identity(current) != _identity(os.fstat(child)):
                raise SnapshotError("source_changed", "a containing directory changed")


@contextmanager
def _directory(path: str) -> Iterator[_Directory]:
    """Walk every component without following symlinks; retain the chain."""
    descriptors = []
    chain = []
    try:
        descriptor = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC)
        descriptors.append(descriptor)
        for component in Path(path).parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC,
                            dir_fd=descriptor)
            descriptors.append(child)
            chain.append((descriptor, component, child))
            descriptor = child
        result = _Directory(descriptor, chain)
        result.check()
        _supported_filesystem(descriptor)
        yield result
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _private(descriptor: int, *, directory: bool) -> None:
    info = os.fstat(descriptor)
    expected = 0o700 if directory else 0o600
    if (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != expected
            or (not directory and info.st_nlink != 1)):
        raise SnapshotError("unsafe_snapshot", "stored snapshot requires private ownership and permissions")
    _attributes(descriptor)  # Refuse extended ACLs which can override mode bits.


def _parent_controlled(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise SnapshotError("unsafe_path", "destination parent must be owner-controlled")
    _attributes(descriptor)


def _read_file(parent: int, name: str, *, limit: int, private: bool = False) -> bytes:
    before_path = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(before_path.st_mode):
        raise SnapshotError("unsafe_path", "snapshot input must be a regular file, not a symlink")
    descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _version(before) != _version(before_path):
            raise SnapshotError("source_changed", "file identity changed before capture")
        if private:
            _private(descriptor, directory=False)
            _supported_filesystem(descriptor, parent)
        if before.st_size > limit:
            raise ResourceLimitError("snapshot_input_bytes", "snapshot input exceeds its byte limit",
                                     allowed_bytes=limit, observed_bytes=before.st_size)
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        after_path = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (_version(before) != _version(after) or _version(after) != _version(after_path)
                or len(data) != before.st_size):
            raise SnapshotError("source_changed", "file changed during capture")
        return bytes(data)
    finally:
        os.close(descriptor)


def _validate(payload: bytes) -> None:
    package = load_validated_docx(payload, capture=None)
    if DOCUMENT_PART not in package.parts:
        raise SnapshotError("invalid_docx", "snapshot has no main document part")
    for name, content in package.parts.items():
        if name.casefold().endswith((".xml", ".rels")):
            root = parse_xml(content)
            if name == DOCUMENT_PART:
                require_single_direct_document_body(root)


def capture_document_snapshot(
    path: str | os.PathLike[str], expected_sha256: str | None = None,
) -> DocumentSnapshot:
    """Capture one bounded descriptor reading; hardlinked sources are allowed.

    The returned value holds bytes, never a live path. A supplied expected hash
    must be lowercase SHA-256. Source drift or unsupported ZIP/XML is refused.
    """
    if expected_sha256 is not None and not _digest(expected_sha256):
        raise SnapshotError("invalid_sha256", "expected_sha256 must be lowercase SHA-256")
    source = _path(path)
    parent, name = os.path.split(source)
    try:
        with _directory(parent) as directory:
            payload = _read_file(directory.descriptor, name, limit=MAX_DOCX_INPUT_BYTES)
            directory.check()
        digest = _sha(payload)
        if expected_sha256 is not None and digest != expected_sha256:
            raise SnapshotError("file_sha256_mismatch", "captured bytes do not match the expected snapshot")
        _validate(payload)
        return DocumentSnapshot(payload, digest, len(payload))
    except OSError as exc:
        raise SnapshotError("snapshot_unreadable", "cannot capture the exact document safely") from exc


def _manifest(snapshot: DocumentSnapshot) -> bytes:
    return (json.dumps({"schema_version": _SCHEMA, "sha256": snapshot.sha256,
                        "size_bytes": snapshot.size_bytes}, sort_keys=True, separators=(",", ":"))
            + "\n").encode("utf-8")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest field")
        result[key] = value
    return result


def _load_directory(directory: int) -> DocumentSnapshot:
    _private(directory, directory=True)
    before = _version(os.fstat(directory))
    if set(os.listdir(directory)) != _FILES:
        raise SnapshotError("invalid_manifest", "snapshot directory must contain exactly its two declared files")
    data = _read_file(directory, "manifest.json", limit=_MANIFEST_LIMIT, private=True)
    try:
        manifest = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SnapshotError("invalid_manifest", "snapshot manifest must be unambiguous UTF-8 JSON") from exc
    if (not isinstance(manifest, dict)
            or set(manifest) != {"schema_version", "sha256", "size_bytes"}
            or manifest["schema_version"] != _SCHEMA or not _digest(manifest["sha256"])
            or type(manifest["size_bytes"]) is not int
            or not 0 <= manifest["size_bytes"] <= MAX_DOCX_INPUT_BYTES):
        raise SnapshotError("invalid_manifest", "snapshot manifest does not match round_snapshot.v1")
    payload = _read_file(directory, "source.docx", limit=MAX_DOCX_INPUT_BYTES, private=True)
    result = DocumentSnapshot(payload, manifest["sha256"], manifest["size_bytes"])
    _validate(payload)
    if _version(os.fstat(directory)) != before or set(os.listdir(directory)) != _FILES:
        raise SnapshotError("source_changed", "snapshot directory changed while loading")
    # Recheck both file versions/content: a file may change after its first read.
    if (_read_file(directory, "manifest.json", limit=_MANIFEST_LIMIT, private=True) != data
            or _read_file(directory, "source.docx", limit=MAX_DOCX_INPUT_BYTES, private=True) != payload):
        raise SnapshotError("source_changed", "snapshot files changed while loading")
    return result


def load_snapshot(destination: str | os.PathLike[str]) -> DocumentSnapshot:
    """Validate a private store; this detects corruption, not malicious forgery."""
    try:
        with _directory(_path(destination)) as directory:
            _supported_filesystem(directory.descriptor, directory.chain[-1][0])
            result = _load_directory(directory.descriptor)
            directory.check()
            return result
    except OSError as exc:
        raise SnapshotError("snapshot_unreadable", "cannot load the exact snapshot safely") from exc


def _matches(parent: int, name: str, identity: tuple[int, int]) -> bool:
    try:
        return _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) == identity
    except OSError:
        return False


def _cleanup(parent: int, name: str, descriptor: int, identity: tuple[int, int],
             children: dict[str, tuple[int, int]]) -> None:
    if not _matches(parent, name, identity):
        return
    for child, owned in children.items():
        _unlink_owned(descriptor, child, owned)
    try:
        if _matches(parent, name, identity):
            os.rmdir(name, dir_fd=parent)
    except OSError:
        pass  # Never recursively remove foreign contents or mask the outcome.


def persist_snapshot(snapshot: DocumentSnapshot, destination: str | os.PathLike[str]) -> SnapshotPublication:
    """Publish a new private directory atomically, without replacing anything.

    Repeated destinations conflict even if their bytes match. A late error has
    publication_completed=True (or None if indeterminate), never a false claim
    that no publication occurred. Only this call's owned stage is cleaned up.
    """
    if not isinstance(snapshot, DocumentSnapshot):
        raise SnapshotError("invalid_snapshot", "a captured DocumentSnapshot is required")
    # Public dataclass construction is not a bypass of package validation.
    DocumentSnapshot(snapshot.payload, snapshot.sha256, snapshot.size_bytes)
    _validate(snapshot.payload)
    output = _path(destination)
    parent_path, output_name = os.path.split(output)
    stage_name = ".veqtor-snapshot-" + uuid.uuid4().hex + ".tmp"
    published = False
    stage_fd = None
    stage_identity = None
    children: dict[str, tuple[int, int]] = {}
    try:
        with _directory(parent_path) as parent:
            parent_fd = parent.descriptor
            _parent_controlled(parent_fd)
            try:
                os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise SnapshotError("snapshot_exists", "snapshot destination already exists")
            os.mkdir(stage_name, 0o700, dir_fd=parent_fd)
            try:
                stage_fd = os.open(stage_name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _CLOEXEC,
                                   dir_fd=parent_fd)
                stage_identity = _identity(os.fstat(stage_fd))
                _supported_filesystem(stage_fd, parent_fd)
                _private(stage_fd, directory=True)
                for name, payload in (("source.docx", snapshot.payload), ("manifest.json", _manifest(snapshot))):
                    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                                 0o600, dir_fd=stage_fd)
                    try:
                        children[name] = _identity(os.fstat(fd))
                        _private(fd, directory=False)
                        _write_all(fd, payload)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                if _load_directory(stage_fd) != snapshot:
                    raise SnapshotError("source_changed", "stage does not contain the captured snapshot")
                os.fsync(stage_fd)
                parent.check()
                _parent_controlled(parent_fd)
                if not _matches(parent_fd, stage_name, stage_identity):
                    raise SnapshotError("source_changed", "snapshot stage moved before publication")
                try:
                    _rename_noreplace(parent_fd, stage_name, output_name)
                    published = True
                    os.fsync(parent_fd)
                    parent.check()
                    _parent_controlled(parent_fd)
                    if not _matches(parent_fd, output_name, stage_identity):
                        raise SnapshotError("source_changed", "published snapshot moved")
                except (OSError, DocxError) as exc:
                    completed = (True if published or _matches(parent_fd, output_name, stage_identity)
                                 else False if _matches(parent_fd, stage_name, stage_identity) else None)
                    published = completed
                    try:
                        parent.check()
                        verified_path = _matches(parent_fd, output_name, stage_identity)
                    except (OSError, DocxError):
                        verified_path = False
                    raise SnapshotError(
                        "snapshot_exists" if isinstance(exc, FileExistsError) and completed is False
                        else "publication_uncertain" if completed is not False else "snapshot_write_failed",
                        "snapshot publication did not complete with confirmed durability",
                        publication_completed=completed, output_path=output,
                        output_path_verified=verified_path,
                    ) from exc
            finally:
                if stage_fd is not None:
                    if stage_identity is not None:
                        _cleanup(parent_fd, stage_name, stage_fd, stage_identity, children)
                    os.close(stage_fd)
    except SnapshotError as exc:
        exc.metadata.setdefault("publication_completed", published)
        exc.metadata.setdefault("output_path", output)
        exc.metadata.setdefault("output_path_verified", False)
        raise
    except (OSError, DocxError) as exc:
        raise SnapshotError(
            "publication_uncertain" if published is not False else getattr(exc, "code", "snapshot_write_failed"),
            "snapshot publication has no confirmed completion" if published is not False
            else "snapshot preparation failed before publication",
            publication_completed=published, output_path=output,
        ) from exc
    return SnapshotPublication(output, snapshot.sha256, snapshot.size_bytes)


def restore_snapshot_copy(
    snapshot_directory: str | os.PathLike[str], destination: str | os.PathLike[str],
    *, expected_sha256: str,
) -> SnapshotPublication:
    """Restore exact bytes to one new ordinary file; never edit the snapshot.

    This local helper is not a working-role assignment or a drafting journal.
    Inspect the restored file afresh before a checked drafting copy save. No
    lock, journal, backup or temporary is ever created in the snapshot folder.
    """
    if not _digest(expected_sha256):
        raise SnapshotError("invalid_sha256", "expected_sha256 must be lowercase SHA-256")
    snapshot = load_snapshot(snapshot_directory)
    if snapshot.sha256 != expected_sha256:
        raise SnapshotError("file_sha256_mismatch", "snapshot does not match the selected document")
    output = _path(destination)
    require_editable_path(output)
    folder, name = os.path.split(output)
    if os.path.realpath(folder) == os.path.realpath(_path(snapshot_directory)):
        raise SnapshotError("protected_storage_path", "restore outside the snapshot directory")
    stage = ".veqtor-restore-" + uuid.uuid4().hex + ".tmp"
    published = False
    try:
        with _directory(folder) as parent:
            fd = parent.descriptor
            _parent_controlled(fd)
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise SnapshotError("output_exists", "restoration never replaces an existing destination")
            descriptor = os.open(stage, os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                                 0o600, dir_fd=fd)
            identity = _identity(os.fstat(descriptor))
            try:
                _private(descriptor, directory=False)
                _supported_filesystem(descriptor, fd)
                _write_all(descriptor, snapshot.payload)
                os.fsync(descriptor)
                if _read_file(fd, stage, limit=MAX_DOCX_INPUT_BYTES, private=True) != snapshot.payload:
                    raise SnapshotError("source_changed", "restored stage differs from the snapshot")
                parent.check()
                _parent_controlled(fd)
                if not _matches(fd, stage, identity):
                    raise SnapshotError("source_changed", "restoration stage moved")
                try:
                    _rename_noreplace(fd, stage, name)
                    published = True
                    os.fsync(fd)
                    parent.check()
                    _parent_controlled(fd)
                    if (_read_file(fd, name, limit=MAX_DOCX_INPUT_BYTES, private=True) != snapshot.payload
                            or not _matches(fd, name, identity)):
                        raise SnapshotError("source_changed", "restored file changed after publication")
                except (OSError, DocxError) as exc:
                    completed = (True if published or _matches(fd, name, identity)
                                 else False if _matches(fd, stage, identity) else None)
                    published = completed
                    try:
                        parent.check()
                        verified = (_matches(fd, name, identity)
                                    and _read_file(fd, name, limit=MAX_DOCX_INPUT_BYTES, private=True) == snapshot.payload)
                    except (OSError, DocxError):
                        verified = False
                    raise SnapshotError(
                        "publication_uncertain" if completed is not False
                        else "output_exists" if isinstance(exc, FileExistsError) else "snapshot_write_failed",
                        "restoration did not complete with confirmed durability",
                        publication_completed=completed, output_path=output, output_path_verified=verified,
                    ) from exc
            finally:
                _unlink_owned(fd, stage, identity)
                os.close(descriptor)
    except SnapshotError as exc:
        exc.metadata.setdefault("publication_completed", published)
        exc.metadata.setdefault("output_path", output)
        exc.metadata.setdefault("output_path_verified", False)
        raise
    except (OSError, DocxError) as exc:
        raise SnapshotError("publication_uncertain" if published is not False else "snapshot_write_failed",
                            "cannot restore the selected snapshot safely", publication_completed=published,
                            output_path=output, output_path_verified=False) from exc
    return SnapshotPublication(output, snapshot.sha256, snapshot.size_bytes)
