# SPDX-License-Identifier: Apache-2.0
"""Independent, bounded, atomic local deal-position snapshots (POSIX)."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid

import jsonschema

from veqtor_docx._ooxml import DocxError
from veqtor_docx.extract import _extract_from_bytes
from veqtor_docx.inspect import _load_snapshot_from_payload, _resolve_paragraph
from . import _positions_contract as contract

STORE_NAME = "deal-positions.json"
LOCK_NAME = "deal-positions.lock"
MAX_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_NODES = 50000
MAX_DEPTH = 20
LOCK_SECONDS = 2.0
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_TOTAL = 100 * 1024 * 1024
MAX_SOURCE_FILES = 20
SERVER_SESSION_ID = uuid.uuid4().hex
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_VALIDATORS = {name: jsonschema.Draft202012Validator(schema) for name, schema in
               [("store", contract.STORE), ("operations", contract.OPERATIONS),
                ("revision", contract.nullable(contract.SHA))]}


class PositionError(DocxError):
    """Only closed, path/content-free errors cross this boundary."""

    def __init__(self, code):
        self.code = code
        detail = ("outcome uncertain; reread revision before retrying" if code == "commit_uncertain"
                  else "deal-position operation refused")
        super().__init__(f"{code}: {detail}")


def _fail(code):
    raise PositionError(code)


def _json_bytes(value, *, limit=MAX_BYTES, node_limit=None):
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > (MAX_NODES if node_limit is None else node_limit) or depth > MAX_DEPTH:
            _fail("resource_limit_exceeded")
        if isinstance(item, dict):
            if len(item) > MAX_NODES or any(len(str(key)) > limit for key in item):
                _fail("resource_limit_exceeded")
            if any(type(key) is not str for key in item):
                _fail("invalid_request")
            stack.extend((value, depth + 1) for value in item.values())
        elif isinstance(item, list):
            if len(item) > MAX_NODES:
                _fail("resource_limit_exceeded")
            stack.extend((value, depth + 1) for value in item)
        elif isinstance(item, str) and len(item) > limit:
            _fail("resource_limit_exceeded")
        elif item is not None and type(item) not in (str, int, bool):
            _fail("invalid_request")
        elif type(item) is int and abs(item) > 1000000:
            _fail("resource_limit_exceeded")
    try:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        _fail("invalid_request")
    if len(payload) > limit:
        _fail("resource_limit_exceeded")
    return payload


def _validate(name, value, code="invalid_request"):
    if not _VALIDATORS[name].is_valid(value):
        _fail(code)


def _revision(store):
    return hashlib.sha256(_json_bytes({k: v for k, v in store.items() if k != "revision"})).hexdigest()


def _identity(info):
    return info.st_dev, info.st_ino


def _stamp(info):
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _regular(fd, *, private):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _fail("unsafe_storage")
    if private and (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()):
        _fail("unsafe_storage")
    return info


def _directory(fd, *, private=False):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        _fail("unsafe_storage")
    if private and (stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid()):
        _fail("unsafe_storage")
    return info


def _open_root(folder):
    if not isinstance(folder, str) or not folder or len(folder) > 4096 or "\x00" in folder:
        _fail("invalid_workspace")
    expanded = os.path.expanduser(folder)
    if not os.path.isabs(expanded) or any(part in {".", ".."} for part in expanded.split("/")):
        _fail("invalid_workspace")
    path = Path(expanded)
    fd = os.open(path.anchor, _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        _directory(fd)
        return path, fd
    except BaseException:
        os.close(fd)
        raise


def _recheck_root(path, fd):
    try:
        _, observed = _open_root(str(path))
        try:
            if _identity(os.fstat(fd)) != _identity(os.fstat(observed)):
                _fail("workspace_changed")
        finally:
            os.close(observed)
    except OSError:
        _fail("workspace_changed")


def _named_same(parent, name, fd):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError:
        _fail("workspace_changed")
    if _identity(info) != _identity(os.fstat(fd)):
        _fail("workspace_changed")


def _read_file(fd, limit, *, private):
    before = _regular(fd, private=private)
    if before.st_size > limit:
        _fail("resource_limit_exceeded")
    chunks = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > limit:
        _fail("resource_limit_exceeded")
    if _stamp(before) != _stamp(os.fstat(fd)):
        _fail("workspace_changed")
    return data, _stamp(before)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("storage_corrupt")
        result[key] = value
    return result


def _source_path(value):
    if (not value or value.startswith("/") or "\\" in value or
            any(part in {"", ".", "..", ".veqtor"} for part in value.split("/"))):
        _fail("invalid_request")
    return value.split("/")


def _content_semantics(content, pid):
    if pid in content["related_position_ids"]:
        _fail("invalid_request")
    if content["fallback_conditions"] is not None and content["fallback"] is None:
        _fail("invalid_request")
    seen = set()
    for source in content["sources"]:
        _source_path(source["path"])
        if source["reference"] and source["reference"]["file_sha256"] != source["file_sha256"]:
            _fail("invalid_request")
        key = _json_bytes(source)
        if key in seen:
            _fail("invalid_request")
        seen.add(key)


def _validate_store(store):
    if not isinstance(store, dict):
        _fail("storage_corrupt")
    if store.get("schema_version") != "deal_positions_store.v1":
        _fail("storage_unsupported")
    _validate("store", store, "storage_corrupt")
    if not store["positions"] or not store["history"] or _revision(store) != store["revision"]:
        _fail("storage_corrupt")
    prior = {}
    try:
        for seq, event in enumerate(store["history"], 1):
            pos, op = event["position"], event["operation"]
            pid = pos["position_id"]
            old = prior.get(pid)
            _content_semantics(pos["content"], pid)
            if event["sequence"] != seq:
                _fail("storage_corrupt")
            if op == "create":
                valid = old is None and pos["version"] == 1 and pos["confirmation"] is None and pos["lifecycle"] == "active"
            elif old is None or old["lifecycle"] != "active":
                valid = False
            elif op == "update":
                valid = pos["version"] == old["version"] + 1 and pos["confirmation"] is None and pos["lifecycle"] == "active"
            elif op == "confirm":
                conf = pos["confirmation"]
                valid = (old["confirmation"] is None and conf is not None and
                         conf["version"] == old["version"] and pos == {**old, "confirmation": conf})
            else:
                valid = pos == {**old, "lifecycle": "withdrawn"}
            if not valid:
                _fail("storage_corrupt")
            prior[pid] = pos
        if store["positions"] != [prior[pid] for pid in sorted(prior)]:
            _fail("storage_corrupt")
        for event in store["history"]:
            if not set(event["position"]["content"]["related_position_ids"]) <= prior.keys():
                _fail("storage_corrupt")
    except PositionError:
        _fail("storage_corrupt")


def _load(side):
    try:
        fd = os.open(STORE_NAME, _FILE_FLAGS, dir_fd=side)
    except FileNotFoundError:
        return None, None
    try:
        raw, stamp = _read_file(fd, MAX_BYTES, private=True)
        _named_same(side, STORE_NAME, fd)
    finally:
        os.close(fd)
    try:
        store = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: _fail("storage_corrupt"))
        _json_bytes(store)
    except (ValueError, UnicodeError, RecursionError):
        _fail("storage_corrupt")
    _validate_store(store)
    return store, stamp


def _gitignore(side):
    try:
        fd = os.open(".gitignore", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=side)
    except FileExistsError:
        fd = os.open(".gitignore", _FILE_FLAGS, dir_fd=side)
        try:
            data, _ = _read_file(fd, 2, private=True)
            if data != b"*\n":
                _fail("unsafe_storage")
        finally:
            os.close(fd)
    else:
        try:
            _write_all(fd, b"*\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(side)


@contextmanager
def _storage(folder, *, write):
    path = None
    root = side = lock = None
    try:
        try:
            path, root = _open_root(folder)
        except OSError:
            _fail("invalid_workspace")
        if write:
            try:
                os.mkdir(".veqtor", mode=0o700, dir_fd=root)
                os.fsync(root)
            except FileExistsError:
                pass
        try:
            side = os.open(".veqtor", _DIR_FLAGS, dir_fd=root)
        except FileNotFoundError:
            if write:
                _fail("storage_failure")
            _recheck_root(path, root)
            yield None
            return
        _directory(side, private=True)
        try:
            if write:
                try:
                    lock = os.open(LOCK_NAME, _FILE_FLAGS | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=side)
                except FileExistsError:
                    lock = os.open(LOCK_NAME, _FILE_FLAGS, dir_fd=side)
            else:
                lock = os.open(LOCK_NAME, _FILE_FLAGS, dir_fd=side)
        except FileNotFoundError:
            if write:
                # Never treat failed write-side lock setup as an absent read.
                _fail("storage_failure")
            store, _ = _load(side)
            if store is not None:
                _fail("unsafe_storage")
            _recheck_root(path, root)
            _named_same(root, ".veqtor", side)
            yield None
            return
        _regular(lock, private=True)
        deadline = time.monotonic() + LOCK_SECONDS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    _fail("lock_timeout")
                time.sleep(0.01)

        def recheck():
            _recheck_root(path, root)
            _named_same(root, ".veqtor", side)
            _named_same(side, LOCK_NAME, lock)
            _directory(side, private=True)
            _regular(lock, private=True)

        recheck()
        if write:
            _gitignore(side)
            os.fsync(side)
        yield root, side, recheck
    except OSError:
        _fail("storage_failure")
    finally:
        # Closing releases flock. Cleanup never re-labels an already committed write.
        for fd in (lock, side, root):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


class _Sources:
    def __init__(self, root):
        self.root = root
        self.cache = {}
        self.total = 0
        self.verified = set()

    def payload(self, path):
        if path in self.cache:
            value = self.cache[path]
            if isinstance(value, PositionError):
                raise value
            return value
        if len(self.cache) >= MAX_SOURCE_FILES or self.total >= MAX_SOURCE_TOTAL:
            _fail("resource_limit_exceeded")
        parts = _source_path(path)
        parent = os.dup(self.root)
        fd = None
        try:
            for part in parts[:-1]:
                child = os.open(part, _DIR_FLAGS, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent)
            data, _ = _read_file(fd, min(MAX_SOURCE_BYTES, MAX_SOURCE_TOTAL - self.total), private=False)
            _named_same(parent, parts[-1], fd)
            # Reopen the complete relative chain to detect directory replacement.
            check = os.dup(self.root)
            try:
                for part in parts[:-1]:
                    child = os.open(part, _DIR_FLAGS, dir_fd=check)
                    os.close(check)
                    check = child
                if _identity(os.fstat(check)) != _identity(os.fstat(parent)):
                    _fail("source_unverified")
                _named_same(check, parts[-1], fd)
            finally:
                os.close(check)
            self.total += len(data)
            self.cache[path] = data
            return data
        except (OSError, PositionError) as exc:
            error = exc if isinstance(exc, PositionError) else PositionError("source_unverified")
            self.cache[path] = error
            raise error from None
        finally:
            if fd is not None:
                os.close(fd)
            os.close(parent)

    def verify(self, source):
        key = _json_bytes(source)
        if key in self.verified:
            return
        try:
            data = self.payload(source["path"])
            if hashlib.sha256(data).hexdigest() != source["file_sha256"]:
                _fail("source_unverified")
            ref = source["reference"]
            if ref is None:
                self.verified.add(key)
                return
            if ref["schema_version"] == "paragraph_ref.v1":
                snapshot = _load_snapshot_from_payload(data, path=source["path"])
                _resolve_paragraph(snapshot, ref)
            elif not any(unit["anchor"] == ref for unit in _extract_from_bytes(data, source["path"])["change_units"]):
                _fail("source_unverified")
            self.verified.add(key)
        except DocxError as exc:
            if isinstance(exc, PositionError) and exc.code == "resource_limit_exceeded":
                raise
            _fail("source_unverified")


def _result(store, *, history, check, root):
    positions = deepcopy(store["positions"]) if store else []
    events = deepcopy(store["history"]) if store and history else []
    bindings = {}
    for pos in positions + [event["position"] for event in events]:
        for source in pos["content"]["sources"]:
            bindings[_json_bytes(source)] = source
    sources = _Sources(root)
    observations = []
    for key in sorted(bindings):
        source = bindings[key]
        status = "not_checked"
        if check:
            try:
                payload = sources.payload(source["path"])
                status = "same_bytes" if hashlib.sha256(payload).hexdigest() == source["file_sha256"] else "changed"
            except PositionError as exc:
                status = "not_checked" if exc.code == "resource_limit_exceeded" else "unavailable"
        observations.append({"source": source, "status": status})
    result = {
        "schema_version": "deal_positions_result.v1", "status": "ok",
        "state": "initialized" if store else "uninitialized",
        "matter_id": store["matter_id"] if store else None,
        "revision": store["revision"] if store else None,
        "positions": positions, "history": events, "history_included": history,
        "source_observations": observations, "server_session_id": SERVER_SESSION_ID,
    }
    _json_bytes(result, limit=MAX_RESULT_BYTES, node_limit=150000)
    return result


def read_deal_positions(folder, include_history=False, check_sources=False):
    if type(include_history) is not bool or type(check_sources) is not bool:
        _fail("invalid_request")
    with _storage(folder, write=False) as storage:
        if storage is None:
            return _result(None, history=include_history, check=False, root=None)
        root, side, recheck = storage
        store, _ = _load(side)
        result = _result(store, history=include_history, check=check_sources, root=root)
        recheck()
        return result


def _change(store, operations, root):
    candidate = deepcopy(store) if store else {
        "schema_version": "deal_positions_store.v1", "matter_id": uuid.uuid4().hex,
        "revision": "0" * 64, "positions": [], "history": [],
    }
    current = {pos["position_id"]: pos for pos in candidate["positions"]}
    sources = _Sources(root)
    touched = set()
    for operation in operations:
        pid, op = operation["position_id"], operation["op"]
        if pid in touched:
            _fail("invalid_request")
        touched.add(pid)
        old = current.get(pid)
        if op == "create":
            if old is not None:
                _fail("position_conflict")
            pos = {"position_id": pid, "version": 1, "content": deepcopy(operation["content"]),
                   "confirmation": None, "lifecycle": "active"}
        else:
            if old is None or old["version"] != operation["expected_version"] or old["lifecycle"] != "active":
                _fail("position_conflict")
            pos = deepcopy(old)
            if op == "update":
                pos.update(content=deepcopy(operation["content"]), confirmation=None, version=old["version"] + 1)
            elif op == "confirm":
                if old["confirmation"] is not None:
                    _fail("position_conflict")
                pos["confirmation"] = {"version": old["version"], "statement": operation["statement"],
                                       "basis": "client_asserted_user_confirmation"}
            else:
                pos["lifecycle"] = "withdrawn"
        if op in {"create", "update"}:
            _content_semantics(pos["content"], pid)
            for source in pos["content"]["sources"]:
                sources.verify(source)
        current[pid] = pos
        candidate["history"].append({"sequence": len(candidate["history"]) + 1,
                                     "operation": op, "position": deepcopy(pos)})
    if len(current) > 50 or len(candidate["history"]) > 500:
        _fail("resource_limit_exceeded")
    for pos in current.values():
        if not set(pos["content"]["related_position_ids"]) <= current.keys():
            _fail("invalid_request")
    candidate["positions"] = [current[pid] for pid in sorted(current)]
    candidate["revision"] = _revision(candidate)
    _validate_store(candidate)
    return candidate


def _write_all(fd, payload):
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _publish(side, candidate, prior_stamp, recheck):
    raw = _json_bytes(candidate)
    name = ".deal-positions.tmp." + uuid.uuid4().hex
    fd = None
    possible_commit = False
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=side)
        _regular(fd, private=True)
        _write_all(fd, raw)
        os.fsync(fd)
        _named_same(side, name, fd)
        recheck()
        _, observed = _load(side)
        if prior_stamp != observed:
            _fail("workspace_changed")
        possible_commit = True
        os.replace(name, STORE_NAME, src_dir_fd=side, dst_dir_fd=side)
        os.fsync(side)
        _named_same(side, STORE_NAME, fd)
        _regular(fd, private=True)
        recheck()
    except Exception:
        if possible_commit:
            _fail("commit_uncertain")
        raise
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(name, dir_fd=side)
        except OSError:
            pass


def mutate_deal_positions(folder, expected_revision, operations, *, validate_result=None):
    _json_bytes({"expected_revision": expected_revision, "operations": operations})
    _validate("revision", expected_revision)
    _validate("operations", operations)
    with _storage(folder, write=True) as storage:
        root, side, recheck = storage
        store, stamp = _load(side)
        if expected_revision != (store["revision"] if store else None):
            _fail("revision_conflict")
        candidate = _change(store, operations, root)
        result = _result(candidate, history=False, check=False, root=root)
        # Transport validates the complete success before the irreversible point.
        if validate_result is not None:
            result = validate_result(result)
        _publish(side, candidate, stamp, recheck)
        return result
