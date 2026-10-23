# SPDX-License-Identifier: Apache-2.0
"""Durable two-record drafting journal, separate from legacy provenance.

The publisher calls prepare under its document lock, after allocating the exact
backup and before publishing. Recovery observes files; it never reapplies edits.
These private local records describe observations, not authenticated consent.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

from . import _drafting_io as io
from . import round_operations as records
from ._ooxml import MAX_DOCX_INPUT_BYTES
from .apply import ApplyError
from . import __version__

MAX_RECORD_BYTES = records.MAX_PLAN_BYTES + 65_536
PREPARED = "drafting_operation_prepared.v1"
RESULT = "drafting_operation_completed.v1"
STATUS = "operation_journal_status.v1"
_CONTEXT_KEYS = {"source_path", "output_path", "save_mode", "source_sha256",
                 "candidate_sha256", "backup_path", "backup_sha256"}


def _fail(code):
    raise ApplyError(code, "operation journal refused; inspect the operation before retrying")


def _id(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            _fail("invalid_operation_id")
    except (ValueError, AttributeError):
        _fail("invalid_operation_id")
    return value


def _bytes(value):
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > MAX_RECORD_BYTES:
        _fail("operation_record_limit_exceeded")
    return payload


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("operation_record_invalid")
        result[key] = value
    return result


def _bounded(value, depth=0, count=None):
    count = [0] if count is None else count
    count[0] += 1
    if depth > 40 or count[0] > records.MAX_PLAN_NODES + 2048:
        _fail("operation_record_limit_exceeded")
    if type(value) is dict:
        for key, child in value.items():
            _bounded(key, depth + 1, count)
            _bounded(child, depth + 1, count)
    elif type(value) is list:
        for child in value:
            _bounded(child, depth + 1, count)
    elif type(value) is int and value.bit_length() > 425:
        _fail("operation_record_limit_exceeded")
    elif type(value) is float and not math.isfinite(value):
        _fail("operation_record_invalid")


def _regular(fd, *, private=False):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (private and (info.st_uid != os.geteuid()
                             or stat.S_IMODE(info.st_mode) != 0o600))):
        _fail("operation_record_unsafe")
    return info


@contextmanager
def _child(parent, name, *, create):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | io._NOFOLLOW | io._CLOEXEC,
                 dir_fd=parent)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            _fail("operation_record_unsafe")
        io._supported_filesystem(fd, parent)
        io._attributes(fd)
        if io._identity(io._at_stat(parent, name)) != io._identity(info):
            _fail("operation_record_changed")
        yield fd
        if io._identity(io._at_stat(parent, name)) != io._identity(info):
            _fail("operation_record_changed")
    finally:
        os.close(fd)


@contextmanager
def _storage(source, identifier, *, create):
    folder = os.path.dirname(source)
    with io._directory(folder) as parent:
        with _child(parent, ".veqtor", create=create) as side:
            if create:
                try:
                    if _read(side, ".gitignore", raw=True) != b"*\n":
                        _fail("operation_record_unsafe")
                except FileNotFoundError:
                    _write(side, ".gitignore", b"*\n")
            with _child(side, "operations", create=create) as operations:
                with _child(operations, identifier, create=create) as operation:
                    try:
                        fcntl.flock(operation, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        _fail("operation_journal_busy")
                    try:
                        yield operation
                        io._directory_current(parent, folder)
                    finally:
                        fcntl.flock(operation, fcntl.LOCK_UN)


def _write(directory, name, payload):
    temporary = ".journal-" + uuid.uuid4().hex + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | io._NOFOLLOW,
                 0o600, dir_fd=directory)
    identity = io._identity(os.fstat(fd))
    try:
        os.fchmod(fd, 0o600)
        io._attributes(fd)
        io._write_all(fd, payload)
        os.fsync(fd)
        io._rename_noreplace(directory, temporary, name)
        os.fsync(directory)
    finally:
        os.close(fd)
        io._unlink_owned(directory, temporary, identity)


def _read(directory, name, *, raw=False):
    fd = os.open(name, os.O_RDONLY | io._NOFOLLOW | io._CLOEXEC | os.O_NONBLOCK,
                 dir_fd=directory)
    try:
        before = _regular(fd, private=True)
        if before.st_size > MAX_RECORD_BYTES:
            _fail("operation_record_limit_exceeded")
        chunks = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65_536))
            if not chunk:
                break
            remaining -= len(chunk)
            chunks.append(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_RECORD_BYTES:
            _fail("operation_record_limit_exceeded")
        if (io._version(os.fstat(fd)) != io._version(before)
                or io._version(io._at_stat(directory, name)) != io._version(before)):
            _fail("operation_record_changed")
        if raw:
            return payload
        value = json.loads(payload, object_pairs_hook=_unique)
        _bounded(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ApplyError("operation_record_invalid", "operation record is invalid") from exc
    finally:
        os.close(fd)


def _observed_at(parent, name, *, check_attributes=False):
    fd = os.open(name, os.O_RDONLY | io._NOFOLLOW | io._CLOEXEC | os.O_NONBLOCK,
                 dir_fd=parent)
    try:
        before = _regular(fd)
        if check_attributes:
            io._attributes(fd)
        if before.st_size > MAX_DOCX_INPUT_BYTES:
            _fail("operation_file_limit_exceeded")
        digest = hashlib.sha256()
        remaining = MAX_DOCX_INPUT_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        if not remaining:
            _fail("operation_file_limit_exceeded")
        if (io._version(os.fstat(fd)) != io._version(before)
                or io._version(io._at_stat(parent, name)) != io._version(before)):
            _fail("operation_file_changed")
        return digest.hexdigest()
    finally:
        os.close(fd)


def observed_hash(path):
    """Bounded stable path observation, without following leaf/hardlink aliases."""
    path = io._path(path)
    folder, name = os.path.split(path)
    with io._directory(folder) as parent:
        digest = _observed_at(parent, name)
        io._directory_current(parent, folder)
        return digest


def _observed_backup(source, path):
    """Read an existing backup through the publisher's directory safety guards."""
    folder = os.path.dirname(source)
    try:
        with io._directory(folder) as parent:
            backups = os.open(io._BACKUP_DIRECTORY,
                os.O_RDONLY | os.O_DIRECTORY | io._NOFOLLOW | io._CLOEXEC, dir_fd=parent)
            try:
                io._backup_current(parent, backups)
                digest = _observed_at(backups, os.path.basename(path), check_attributes=True)
                io._backup_current(parent, backups)
                io._directory_current(parent, folder)
                return digest
            finally:
                os.close(backups)
    except (ApplyError, OSError):
        return None  # Unsafe/missing evidence is never certified or repaired.


def _observe(path):
    try:
        return observed_hash(path)
    except (ApplyError, OSError):
        return None


def _backup(source, intent, backup):
    if intent["save_mode"] == "create_new":
        if backup is not None:
            _fail("operation_record_invalid")
        return None
    if type(backup) is not dict or set(backup) != {"path", "sha256"}:
        _fail("operation_record_invalid")
    path = backup["path"]
    if (type(path) is not str or io._path(path) != path
            or os.path.dirname(path) != os.path.join(os.path.dirname(source), ".veqtor-backups")
            or backup["sha256"] != intent["source_sha256"]):
        _fail("operation_record_invalid")
    prefix = os.fsencode(os.path.basename(source))[:140].decode(errors="ignore")
    prefix += "." + intent["source_sha256"][:16] + "."
    name = os.path.basename(path)
    token = name[len(prefix):-5]
    if (not name.startswith(prefix) or not name.endswith(".docx") or len(token) != 32
            or any(c not in "0123456789abcdef" for c in token)):
        _fail("operation_record_invalid")
    return {"path": path, "sha256": _observed_backup(source, path)}


def _timestamp(value):
    try:
        return (type(value) is str and len(value) <= 40
                and datetime.fromisoformat(value).utcoffset().total_seconds() == 0)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return False


def _prepared(value, source, identifier):
    if (type(value) is not dict or set(value) != {"schema_version", "created_at", "source_path", "producer_version", "intent", "backup"}
            or value["schema_version"] != PREPARED or value["source_path"] != source
            or type(value["producer_version"]) is not str or not value["producer_version"]
            or not _timestamp(value["created_at"])):
        _fail("operation_record_invalid")
    intent = records.validate_operation_intent(value["intent"])
    if intent["operation_id"] != identifier or io._path(intent["destination"]) != intent["destination"]:
        _fail("operation_record_invalid")
    _backup(source, intent, value["backup"])
    return intent


def _completed(value, intent):
    if (type(value) is not dict or set(value) != {"schema_version", "recorded_at", "recovered", "result"}
            or value["schema_version"] != RESULT or type(value["recovered"]) is not bool
            or not _timestamp(value["recorded_at"])):
        _fail("operation_record_invalid")
    return records.validate_operation_result(intent, value["result"])


def _status(identifier, result, *, written, error=None):
    return {"schema_version": STATUS, "operation_id": identifier,
            "publication_status": result["publication_status"],
            "verification": result["verification"],
            "observed_output_sha256": result["observed_output_sha256"],
            "backup_verification": result["backup_verification"],
            "journal_status": "written" if written else "incomplete",
            "journal_error": error, "operation_error": result["error_code"]}


class OperationJournal:
    def __init__(self, operation_id, *, source_path, plan, author_profile, producer):
        self.operation_id = _id(operation_id)
        self.source_path = io._path(source_path)
        self.plan = json.loads(records.canonical_plan_bytes(plan))
        self.profile = dict(author_profile)
        self.producer = producer
        self.prepared = False
        self.entry = None

    def prepare(self, context):
        try:
            if set(context) != _CONTEXT_KEYS or context["source_path"] != self.source_path:
                _fail("operation_record_invalid")
            intent = records.make_operation_intent(
                operation_id=self.operation_id, plan=self.plan,
                source_sha256=context["source_sha256"], candidate_sha256=context["candidate_sha256"],
                author_profile=self.profile, producer=self.producer,
                destination=context["output_path"],
                save_mode={"in_place": "replace_source", "copy": "create_new"}[context["save_mode"]])
            backup = (None if context["backup_path"] is None else
                      {"path": context["backup_path"], "sha256": context["backup_sha256"]})
            entry = {"schema_version": PREPARED, "created_at": datetime.now(UTC).isoformat(),
                     "source_path": self.source_path, "producer_version": __version__, "intent": intent, "backup": backup}
            _prepared(entry, self.source_path, self.operation_id)
            observed = _backup(self.source_path, intent, backup)
            if backup is not None and observed["sha256"] != intent["source_sha256"]:
                _fail("operation_backup_unverified")
            with _storage(self.source_path, self.operation_id, create=True) as fd:
                try:
                    previous = _read(fd, "prepared.json")
                except FileNotFoundError:
                    with os.scandir(fd) as entries:
                        if next(entries, None) is not None:
                            _fail("operation_record_invalid")
                    _write(fd, "prepared.json", _bytes(entry))
                else:
                    old = _prepared(previous, self.source_path, self.operation_id)
                    _fail("operation_recovery_required" if old == intent else "operation_id_conflict")
            self.entry = entry
            self.prepared = True
        except ApplyError:
            raise
        except (OSError, ValueError, KeyError) as exc:
            raise ApplyError("journal_prepare_failed", "prepared operation could not be saved; document was not published") from exc

    def finish(self, *, publication_completed, observed_output_sha256,
               backup_path_verified, error_code=None, publication_uncertain=False):
        intent = self.entry["intent"]
        try:
            backup = _backup(self.source_path, intent, self.entry["backup"])
        except (ApplyError, OSError):
            original = self.entry["backup"]
            backup = None if original is None else {"path": original["path"], "sha256": None}
        if backup is not None and not backup_path_verified:
            backup["sha256"] = None
        if (publication_completed and (observed_output_sha256 != intent["candidate_sha256"]
                or (backup is not None and backup["sha256"] != intent["source_sha256"]))):
            error_code = error_code or "operation_verification_failed"
        result = records.make_operation_result(
            intent, publication_status=("published" if publication_completed else
                                       "uncertain" if publication_uncertain else "not_published"),
            observed_output_sha256=observed_output_sha256 if publication_completed or publication_uncertain else None,
            backup=backup, error_code=error_code or ("publication_uncertain" if publication_uncertain else None))
        if publication_uncertain:
            # Keep preparation until recovery proves the actual outcome rather
            # than freezing a false negative after an unsuccessful EIO return.
            return _status(self.operation_id, result, written=False, error="operation_outcome_unresolved")
        try:
            with _storage(self.source_path, self.operation_id, create=False) as fd:
                current = _read(fd, "prepared.json")
                if current != self.entry:
                    _fail("operation_record_changed")
                _write(fd, "result.json", _bytes({"schema_version": RESULT,
                    "recorded_at": datetime.now(UTC).isoformat(), "recovered": False, "result": result}))
        except Exception:
            return _status(self.operation_id, result, written=False, error="journal_finalize_failed")
        return _status(self.operation_id, result, written=True)


def recover_operation(source_path, operation_id):
    """Observe and complete one prepared journal record; never edit a document."""
    source = io._path(source_path)
    identifier = _id(operation_id)
    folder, basename = os.path.split(source)
    try:
        with io._directory(folder) as parent, io._writer_lock(parent, folder, basename):
            io._word_unlocked(parent, basename)
            with _storage(source, identifier, create=False) as fd:
                prepared = _read(fd, "prepared.json")
                intent = _prepared(prepared, source, identifier)
                output = _observe(intent["destination"])
                backup = _backup(source, intent, prepared["backup"])
                try:
                    stored = _read(fd, "result.json")
                except FileNotFoundError:
                    stored = None
                if stored is not None:
                    result = _completed(stored, intent)
                    recorded_backup = result["backup"]
                    intended_backup = prepared["backup"]
                    if ((recorded_backup is None) != (intended_backup is None)
                            or (recorded_backup is not None
                                and recorded_backup["path"] != intended_backup["path"])):
                        _fail("operation_record_invalid")
                    os.fsync(fd)
                    meta = _status(identifier, result, written=True)
                    return {**meta, "recovered": stored["recovered"],
                            "current_output_sha256": output, "recovery_status": "existing_result"}
                source_ok = (intent["save_mode"] == "replace_source"
                             or _observe(source) == intent["source_sha256"])
                backup_ok = backup is None or backup["sha256"] == intent["source_sha256"]
                matched = (output == intent["candidate_sha256"] and source_ok and backup_ok
                           and intent["source_sha256"] != intent["candidate_sha256"])
                result = records.make_operation_result(
                    intent, publication_status="published" if matched else "uncertain",
                    observed_output_sha256=output, backup=backup,
                    error_code=None if matched else "operation_outcome_unresolved")
                if not matched:
                    return {**_status(identifier, result, written=False, error="operation_outcome_unresolved"),
                            "recovered": False, "current_output_sha256": output, "recovery_status": "unresolved"}
                _write(fd, "result.json", _bytes({"schema_version": RESULT,
                    "recorded_at": datetime.now(UTC).isoformat(), "recovered": True, "result": result}))
                return {**_status(identifier, result, written=True), "recovered": True,
                        "current_output_sha256": output, "recovery_status": "recovered"}
    except (OSError, ValueError) as exc:
        raise ApplyError("operation_recovery_failed", "operation could not be safely recovered") from exc
