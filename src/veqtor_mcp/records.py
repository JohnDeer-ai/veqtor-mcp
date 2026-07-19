# SPDX-License-Identifier: Apache-2.0
"""Local decision-record sidecar for the MCP tool layer."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import importlib
import json
import math
import os
import re
import secrets
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from veqtor_docx.contracts import (
    APPLY_OPERATIONS_V1,
    DOCUMENT_PART_V1,
    EXTRACT_REVISION_CATEGORIES_V1,
    INSPECT_CONTAINER_POLICY_V1,
    INSPECT_MODES_V1,
    INSPECT_READING_MODE_V1,
    INSPECT_SEARCH_SCOPE_V1,
    MATCH_SIDES_V1,
    PREFLIGHT_EDIT_STATUSES_V1,
    PREFLIGHT_FAILURE_PHASES_V1,
    PREFLIGHT_POSITION_STATUSES_V1,
    REVISION_COUNT_BASES_V1,
    RESULT_STATUS_ERROR,
    RESULT_STATUS_OK,
    ROUND_TRIP_COMPARISONS_V1,
    ROUND_TRIP_STATUSES_V1,
    VERIFY_VERDICTS_V1,
)
from veqtor_docx._ooxml import (
    UserPathError,
    normalized_internal_package_part_name,
    resolve_user_path,
)
from veqtor_mcp import __version__

SCHEMA_VERSION = "decision_record.v1"
DISABLE_ENV = "VEQTOR_DISABLE_DECISION_RECORD"
SIDECAR_DIR = ".veqtor"
JOURNAL_NAME = "decision-records.jsonl"
GITIGNORE_NAME = ".gitignore"
DEFAULT_MAX_RECORDS = 50
MAX_MAX_RECORDS = 500
COMPACT_SAMPLE_LIMIT = 20
MAX_COMPACT_ID_LENGTH = 32
MAX_JOURNAL_LINE_BYTES = 1_048_576
MAX_JOURNAL_DEPTH = 64
MAX_JOURNAL_NODES = 100_000
MAX_CANONICAL_JSON_NODES = 1_000_000
MAX_JSON_INTEGER_DIGITS = 128
SOURCE_SNAPSHOT_SCHEMA = "source_snapshot.v1"
SOURCE_SNAPSHOT_PREFIX = "source-snapshot-v1-sha256:"
SOURCE_SNAPSHOT_UNAVAILABLE = "source-snapshot-unavailable"
ACCESS_RECORD_TYPE = "access_event.v1"
HEX = frozenset("0123456789abcdef")
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
F_GETPATH_BUFFER_BYTES = 1024
JOURNAL_OPEN_ATTEMPTS = 4
MAX_WORKSPACE_DISCOVERY_DEPTH = 1
MAX_WORKSPACE_DISCOVERY_ENTRIES = 500
MAX_WORKSPACE_DISCOVERY_SECONDS = 1.0
V1_CREATED_AT_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{6})?Z"
)
V1_OK_STATUSES = frozenset({RESULT_STATUS_OK})
PAYLOAD_COMPACT = "compact"
PAYLOAD_FULL = "full"
V1_EXPORT_PAYLOADS = frozenset({PAYLOAD_COMPACT, PAYLOAD_FULL})
EXPORT_RECORDS_SCOPE = "substantive_records_only"
EXPORT_TOTAL_COUNT_SCOPE = "substantive_records_before_cursor"
EXPORT_ACCESS_COUNT_SCOPE = "all_prior_access_events_before_current_export"
V1_EXPORT_RECORDS_SCOPES = frozenset({EXPORT_RECORDS_SCOPE})
V1_EXPORT_TOTAL_COUNT_SCOPES = frozenset({EXPORT_TOTAL_COUNT_SCOPE})
V1_EXPORT_ACCESS_COUNT_SCOPES = frozenset({EXPORT_ACCESS_COUNT_SCOPE})
INSPECT_FIXED_EXCLUDED_PARTS_V1 = (
    "word/header*.xml",
    "word/footer*.xml",
    "word/footnotes.xml",
    "word/endnotes.xml",
    "word/comments*.xml",
)

Clock = Callable[[], datetime]
ExportResultFactory = Callable[[dict[str, Any]], dict[str, Any]]


def _safe_error_text(value: object, *, max_length: int | None = None) -> str:
    try:
        text = str(value)
    except Exception:
        text = type(value).__name__
    text = text.replace("\r", " ").replace("\n", " ")
    text = text.encode("utf-8", errors="backslashreplace").decode("utf-8")
    return text if max_length is None else text[:max_length]


class DecisionRecordError(ValueError):
    """A stable journal boundary failure for read and best-effort write paths."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {_safe_error_text(detail)}")
        self.code = code


@dataclass(frozen=True)
class WorkspaceDiscovery:
    """One closed, path-safe classification of a missing export workspace."""

    state: str
    classification_complete: bool
    candidate_count: int | None = None
    candidate_count_at_least: int | None = None
    suggested_relative_workspace: str | None = None
    stop_reason: str | None = None

    def metadata(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "scope": "direct_children",
            "max_depth": MAX_WORKSPACE_DISCOVERY_DEPTH,
            "entry_limit": MAX_WORKSPACE_DISCOVERY_ENTRIES,
            "time_limit_seconds": MAX_WORKSPACE_DISCOVERY_SECONDS,
            "classification_complete": self.classification_complete,
        }
        if self.candidate_count is not None:
            result["candidate_count"] = self.candidate_count
        if self.candidate_count_at_least is not None:
            result["candidate_count_at_least"] = self.candidate_count_at_least
        if self.suggested_relative_workspace is not None:
            result["suggested_workspace"] = {
                "relative_path": self.suggested_relative_workspace,
                "relative_to": "supplied_workspace",
            }
        if self.stop_reason is not None:
            result["stop_reason"] = self.stop_reason
        return result


_WORKSPACE_DISCOVERY_MESSAGES = MappingProxyType(
    {
        "workspace_uninitialized": (
            "supplied workspace has no decision-record journal"
        ),
        "workspace_mismatch": (
            "supplied workspace has no journal; one direct child does"
        ),
        "workspace_ambiguous": (
            "supplied workspace has no journal; multiple direct children do"
        ),
        "workspace_discovery_incomplete": (
            "workspace discovery could not complete within its safe bounds"
        ),
    }
)


class WorkspaceDiscoveryError(DecisionRecordError):
    """A closed export refusal with structured, path-safe discovery metadata."""

    def __init__(self, discovery: WorkspaceDiscovery) -> None:
        if discovery.state not in _WORKSPACE_DISCOVERY_MESSAGES:
            raise ValueError("invalid workspace discovery state")
        super().__init__(
            discovery.state,
            _WORKSPACE_DISCOVERY_MESSAGES[discovery.state],
        )
        self.state = discovery.state
        self.discovery = discovery
        self.metadata = discovery.metadata()


class _JsonBoundaryError(ValueError):
    """A stable rejection from the bounded journal JSON boundary."""


class _RecordSchemaError(ValueError):
    """A context-neutral rejection from the decision-record schema."""


@dataclass(frozen=True)
class _V1ToolSpec:
    record_type: str
    projection_kind: str


V1_HISTORICAL_TOOL_SPECS: Mapping[str, _V1ToolSpec] = MappingProxyType(
    {
        "list_rounds": _V1ToolSpec("tool_observation.v1", "list_rounds"),
        "extract_redlines": _V1ToolSpec("tool_observation.v1", "extract_redlines"),
        "inspect_document": _V1ToolSpec("inspection.v1", "inspect_document"),
        "verify_quote": _V1ToolSpec("verification.v1", "verify_quote"),
        "preflight_edits": _V1ToolSpec("verification.v1", "preflight_edits"),
        "apply_edits": _V1ToolSpec("decision.v1", "apply_edits"),
        "export_decision_record": _V1ToolSpec(
            ACCESS_RECORD_TYPE, "export_decision_record"
        ),
    }
)
WRITABLE_TOOL_NAMES = frozenset(
    {
        "list_rounds",
        "extract_redlines",
        "inspect_document",
        "verify_quote",
        "preflight_edits",
        "apply_edits",
        "export_decision_record",
    }
)
KNOWN_RECORD_TYPES = frozenset(
    spec.record_type for spec in V1_HISTORICAL_TOOL_SPECS.values()
)


def _historical_tool_spec(tool_name: Any) -> _V1ToolSpec:
    if not isinstance(tool_name, str) or tool_name not in V1_HISTORICAL_TOOL_SPECS:
        raise _RecordSchemaError("invalid tool_name")
    return V1_HISTORICAL_TOOL_SPECS[tool_name]


def _writable_tool_spec(tool_name: Any) -> _V1ToolSpec:
    if not isinstance(tool_name, str) or tool_name not in WRITABLE_TOOL_NAMES:
        raise _RecordSchemaError("invalid tool_name")
    return _historical_tool_spec(tool_name)


def utc_now() -> datetime:
    return datetime.now(UTC)


def workspace_for_folder(folder: str) -> Path:
    try:
        return Path(resolve_user_path(folder))
    except UserPathError as exc:
        raise DecisionRecordError(exc.code, exc.detail) from exc


def workspace_for_file(path: str) -> Path:
    try:
        return Path(resolve_user_path(path)).parent
    except UserPathError as exc:
        raise DecisionRecordError(exc.code, exc.detail) from exc


def journal_path(workspace: str | Path) -> Path:
    return workspace_for_folder(workspace) / SIDECAR_DIR / JOURNAL_NAME


def _filesystem_spelled_workspace(
    fd: int,
    fallback: Path,
    expected_identity: tuple[int, int],
) -> Path:
    """Return the filesystem's spelling for an already-open workspace."""
    # Descriptor-backed paths preserve the directory entry's spelling. Do not
    # lower/casefold here: case-sensitive volumes may contain both names.
    value: str | bytes | None = None
    getpath = getattr(fcntl, "F_GETPATH", None)
    if isinstance(getpath, int):
        try:
            payload = fcntl.fcntl(
                fd,
                getpath,
                b"\0" * F_GETPATH_BUFFER_BYTES,
            )
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, bytes):
            path_bytes, separator, _remainder = payload.partition(b"\0")
            if separator and path_bytes:
                value = path_bytes
    if value is None:
        try:
            value = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            pass
    if value is None:
        return fallback
    try:
        candidate = Path(os.fsdecode(value))
    except (TypeError, ValueError, UnicodeError):
        return fallback
    if not candidate.is_absolute():
        return fallback
    try:
        info = candidate.lstat()
    except OSError:
        return fallback
    if (
        not stat.S_ISDIR(info.st_mode)
        or (info.st_dev, info.st_ino) != expected_identity
    ):
        return fallback
    return candidate


def _canonical_workspace(workspace: str | Path) -> tuple[Path, tuple[int, int]]:
    root = workspace_for_folder(workspace)
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise DecisionRecordError(
            "workspace_missing", f"workspace does not exist: {root}"
        ) from exc
    try:
        info = resolved.lstat()
    except OSError as exc:
        raise DecisionRecordError(
            "workspace_changed", f"workspace changed while being resolved: {resolved}"
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise DecisionRecordError(
            "workspace_not_directory", f"workspace is not a directory: {resolved}"
        )
    identity = (info.st_dev, info.st_ino)
    try:
        fd = _open_workspace_fd(resolved, identity)
    except DecisionRecordError:
        raise
    except OSError as exc:
        raise DecisionRecordError(
            "workspace_unreadable", "workspace cannot be read"
        ) from exc
    try:
        canonical = _filesystem_spelled_workspace(fd, resolved, identity)
    finally:
        os.close(fd)
    return canonical, identity


def _reject_special(path: Path, kind: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise DecisionRecordError(f"{kind}_symlink", f"{path} must not be a symlink")
    if kind == "sidecar" and not stat.S_ISDIR(info.st_mode):
        raise DecisionRecordError(
            "sidecar_not_directory", f"{path} must be a directory"
        )
    if kind == "journal" and not stat.S_ISREG(info.st_mode):
        raise DecisionRecordError("journal_not_file", f"{path} must be a file")
    if kind == "journal" and info.st_nlink != 1:
        raise DecisionRecordError(
            "journal_hardlink", f"{path} must not have multiple hard links"
        )


def _open_workspace_fd(root: Path, expected_identity: tuple[int, int]) -> int:
    try:
        fd = os.open(root, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
            raise DecisionRecordError(
                "workspace_changed",
                f"workspace changed before it could be opened: {root}",
            ) from exc
        raise DecisionRecordError(
            "workspace_unreadable", "workspace cannot be opened"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise DecisionRecordError(
                "workspace_not_directory", f"workspace is not a directory: {root}"
            )
        if (info.st_dev, info.st_ino) != expected_identity:
            raise DecisionRecordError(
                "workspace_changed", f"workspace identity changed before open: {root}"
            )
    except Exception:
        os.close(fd)
        raise
    return fd


def _open_sidecar_fd(root_fd: int, sidecar: Path, *, missing_ok: bool) -> int | None:
    try:
        fd = os.open(
            SIDECAR_DIR, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=root_fd
        )
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except NotADirectoryError as exc:
        try:
            info = sidecar.lstat()
        except OSError:
            info = None
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise DecisionRecordError(
                "sidecar_symlink", f"{sidecar} must not be a symlink"
            ) from exc
        raise DecisionRecordError(
            "sidecar_not_directory", f"{sidecar} must be a directory"
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise DecisionRecordError(
                "sidecar_symlink", f"{sidecar} must not be a symlink"
            ) from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise DecisionRecordError(
                "sidecar_not_directory", f"{sidecar} must be a directory"
            )
        return fd
    except Exception:
        os.close(fd)
        raise


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(fd, payload[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError(errno.EIO, "write returned no progress")
        offset += written


def _write_sidecar_gitignore(sidecar_fd: int, sidecar: Path) -> None:
    temp_name = f".{GITIGNORE_NAME}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW
    try:
        fd = os.open(temp_name, flags, 0o600, dir_fd=sidecar_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise DecisionRecordError(
                    "gitignore_not_file", f"{sidecar / temp_name} must be a file"
                )
            if info.st_nlink != 1:
                raise DecisionRecordError(
                    "gitignore_hardlink",
                    f"{sidecar / temp_name} must not have multiple hard links",
                )
            os.fchmod(fd, 0o600)
            _write_all(fd, b"*\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(
            temp_name,
            GITIGNORE_NAME,
            src_dir_fd=sidecar_fd,
            dst_dir_fd=sidecar_fd,
        )
        try:
            os.fsync(sidecar_fd)
        except OSError:
            try:
                os.unlink(GITIGNORE_NAME, dir_fd=sidecar_fd)
            except OSError:
                pass
            raise
    finally:
        try:
            os.unlink(temp_name, dir_fd=sidecar_fd)
        except FileNotFoundError:
            pass


def _ensure_private_gitignore(sidecar_fd: int, sidecar: Path) -> None:
    flags = os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK
    try:
        fd = os.open(GITIGNORE_NAME, flags, dir_fd=sidecar_fd)
    except FileNotFoundError:
        _write_sidecar_gitignore(sidecar_fd, sidecar)
        return
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise DecisionRecordError(
                "gitignore_symlink", f"{sidecar / GITIGNORE_NAME} must not be a symlink"
            ) from exc
        raise
    try:
        info = os.fstat(fd)
        path = sidecar / GITIGNORE_NAME
        if not stat.S_ISREG(info.st_mode):
            raise DecisionRecordError("gitignore_not_file", f"{path} must be a file")
        if info.st_nlink != 1:
            raise DecisionRecordError(
                "gitignore_hardlink", f"{path} must not have multiple hard links"
            )
        content = os.read(fd, 3)
        if content != b"*\n":
            raise DecisionRecordError(
                "gitignore_invalid", f"{path} must contain exactly '*\\n'"
            )
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
            os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _sidecar_for_write(
    workspace: str | Path,
    *,
    expected_identity: tuple[int, int] | None = None,
):
    if expected_identity is None:
        root, expected_identity = _canonical_workspace(workspace)
    else:
        root = Path(workspace)
    root_fd = _open_workspace_fd(root, expected_identity)
    sidecar = root / SIDECAR_DIR
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX)
        try:
            try:
                os.mkdir(SIDECAR_DIR, mode=0o700, dir_fd=root_fd)
                try:
                    os.fsync(root_fd)
                except OSError:
                    try:
                        os.rmdir(SIDECAR_DIR, dir_fd=root_fd)
                    except OSError:
                        pass
                    raise
            except FileExistsError:
                pass
            sidecar_fd = _open_sidecar_fd(root_fd, sidecar, missing_ok=False)
            assert sidecar_fd is not None
            try:
                os.fchmod(sidecar_fd, 0o700)
                _ensure_private_gitignore(sidecar_fd, sidecar)
                yield sidecar, sidecar_fd
            finally:
                os.close(sidecar_fd)
        finally:
            fcntl.flock(root_fd, fcntl.LOCK_UN)
    finally:
        os.close(root_fd)


@contextmanager
def _sidecar_for_read(
    workspace: str | Path,
    *,
    expected_identity: tuple[int, int] | None = None,
):
    if expected_identity is None:
        root, expected_identity = _canonical_workspace(workspace)
    else:
        root = Path(workspace)
    root_fd = _open_workspace_fd(root, expected_identity)
    sidecar = root / SIDECAR_DIR
    try:
        sidecar_fd = _open_sidecar_fd(root_fd, sidecar, missing_ok=True)
        if sidecar_fd is None:
            yield None
            return
        try:
            yield sidecar, sidecar_fd
        finally:
            os.close(sidecar_fd)
    finally:
        os.close(root_fd)


_INVALID_CHILD_WORKSPACE_CODES = frozenset(
    {
        "sidecar_symlink",
        "sidecar_not_directory",
        "journal_symlink",
        "journal_not_file",
        "journal_hardlink",
    }
)


def _workspace_fd_has_existing_journal(
    workspace_fd: int,
    workspace: Path,
    *,
    invalid_as_missing: bool,
) -> bool:
    sidecar = workspace / SIDECAR_DIR
    try:
        sidecar_fd = _open_sidecar_fd(workspace_fd, sidecar, missing_ok=True)
    except DecisionRecordError as exc:
        if invalid_as_missing and exc.code in _INVALID_CHILD_WORKSPACE_CODES:
            return False
        raise
    if sidecar_fd is None:
        return False
    try:
        try:
            handle = _open_journal_for_read(sidecar_fd, sidecar / JOURNAL_NAME)
        except DecisionRecordError as exc:
            if invalid_as_missing and exc.code in _INVALID_CHILD_WORKSPACE_CODES:
                return False
            raise
        if handle is None:
            return False
        handle.close()
        return True
    finally:
        os.close(sidecar_fd)


def _safe_relative_workspace_component(value: str) -> str | None:
    if (
        not value
        or value in {".", ".."}
        or len(value) > 255
        or "/" in value
        or "\\" in value
        or any(
            ord(char) < 0x20 or ord(char) == 0x7F or 0xD800 <= ord(char) <= 0xDFFF
            for char in value
        )
    ):
        return None
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    return value


def _incomplete_workspace_discovery(stop_reason: str) -> WorkspaceDiscovery:
    return WorkspaceDiscovery(
        state="workspace_discovery_incomplete",
        classification_complete=False,
        stop_reason=stop_reason,
    )


def _discover_direct_child_workspaces(
    root: Path,
    root_fd: int,
    *,
    max_entries: int | None = None,
    time_limit_seconds: float | None = None,
    monotonic: Callable[[], float] | None = None,
) -> WorkspaceDiscovery:
    """Classify valid direct-child journals without following any symlink."""
    entry_limit = (
        MAX_WORKSPACE_DISCOVERY_ENTRIES if max_entries is None else max_entries
    )
    time_limit = (
        MAX_WORKSPACE_DISCOVERY_SECONDS
        if time_limit_seconds is None
        else time_limit_seconds
    )
    now = time.monotonic if monotonic is None else monotonic
    deadline = now() + time_limit
    candidates: list[str | None] = []
    visited = 0

    try:
        scanner = os.scandir(root_fd)
    except OSError as exc:
        raise WorkspaceDiscoveryError(
            _incomplete_workspace_discovery("filesystem_error")
        ) from exc

    try:
        with scanner:
            iterator = iter(scanner)
            while True:
                if now() > deadline:
                    return _incomplete_workspace_discovery("time_limit")
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                except OSError:
                    return _incomplete_workspace_discovery("filesystem_error")

                visited += 1
                if visited > entry_limit:
                    return _incomplete_workspace_discovery("entry_limit")
                if now() > deadline:
                    return _incomplete_workspace_discovery("time_limit")
                try:
                    if entry.name == SIDECAR_DIR:
                        continue
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    child_fd = os.open(
                        entry.name,
                        os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW,
                        dir_fd=root_fd,
                    )
                except (FileNotFoundError, NotADirectoryError):
                    continue
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        continue
                    return _incomplete_workspace_discovery("filesystem_error")

                try:
                    try:
                        is_candidate = _workspace_fd_has_existing_journal(
                            child_fd,
                            root / entry.name,
                            invalid_as_missing=True,
                        )
                    except OSError:
                        return _incomplete_workspace_discovery("filesystem_error")
                finally:
                    os.close(child_fd)

                if not is_candidate:
                    continue
                candidates.append(_safe_relative_workspace_component(entry.name))
                if len(candidates) >= 2:
                    return WorkspaceDiscovery(
                        state="workspace_ambiguous",
                        classification_complete=True,
                        candidate_count_at_least=2,
                    )
                if now() > deadline:
                    return _incomplete_workspace_discovery("time_limit")
    except OSError:
        return _incomplete_workspace_discovery("filesystem_error")

    if not candidates:
        return WorkspaceDiscovery(
            state="workspace_uninitialized",
            classification_complete=True,
            candidate_count=0,
        )
    suggestion = candidates[0]
    if suggestion is None:
        return _incomplete_workspace_discovery("unsafe_relative_name")
    return WorkspaceDiscovery(
        state="workspace_mismatch",
        classification_complete=True,
        candidate_count=1,
        suggested_relative_workspace=suggestion,
    )


def _require_existing_export_workspace(
    root: Path,
    expected_identity: tuple[int, int],
) -> None:
    try:
        root_fd = _open_workspace_fd(root, expected_identity)
    except DecisionRecordError:
        raise
    except OSError as exc:
        raise DecisionRecordError(
            "workspace_unreadable", "workspace cannot be read"
        ) from exc
    try:
        try:
            if _workspace_fd_has_existing_journal(
                root_fd,
                root,
                invalid_as_missing=False,
            ):
                return
        except DecisionRecordError:
            raise
        except OSError as exc:
            raise DecisionRecordError(
                "workspace_unreadable", "workspace cannot be read"
            ) from exc
        discovery = _discover_direct_child_workspaces(root, root_fd)
    finally:
        os.close(root_fd)
    raise WorkspaceDiscoveryError(discovery)


@contextmanager
def _sidecar_for_existing_write(
    workspace: str | Path,
    *,
    expected_identity: tuple[int, int] | None = None,
):
    """Open an initialized sidecar for export without ever recreating it."""
    if expected_identity is None:
        root, expected_identity = _canonical_workspace(workspace)
    else:
        root = Path(workspace)
    root_fd = _open_workspace_fd(root, expected_identity)
    sidecar = root / SIDECAR_DIR
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX)
        try:
            sidecar_fd = _open_sidecar_fd(root_fd, sidecar, missing_ok=True)
            if sidecar_fd is None:
                raise DecisionRecordError(
                    "workspace_changed",
                    "initialized workspace sidecar disappeared before export",
                )
            try:
                os.fchmod(sidecar_fd, 0o700)
                _ensure_private_gitignore(sidecar_fd, sidecar)
                yield sidecar, sidecar_fd
            finally:
                os.close(sidecar_fd)
        finally:
            fcntl.flock(root_fd, fcntl.LOCK_UN)
    finally:
        os.close(root_fd)


def disabled() -> bool:
    value = os.environ.get(DISABLE_ENV, "")
    return value.lower() in {"1", "true", "yes", "on"}


def _format_v1_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _created_at(clock: Clock) -> str:
    return _format_v1_timestamp(clock())


_MAX_JSON_INTEGER_MAGNITUDE = 10**MAX_JSON_INTEGER_DIGITS


def _validate_json_value(
    payload: Any,
    *,
    max_depth: int = MAX_JOURNAL_DEPTH,
    max_nodes: int = MAX_CANONICAL_JSON_NODES,
) -> None:
    stack: list[tuple[bool, Any, int]] = [(True, payload, 0)]
    ancestors: set[int] = set()
    nodes = 0
    while stack:
        entering, item, depth = stack.pop()
        if not entering:
            ancestors.remove(id(item))
            continue

        nodes += 1
        if nodes > max_nodes:
            raise _JsonBoundaryError("JSON value exceeds maximum node count")
        if depth > max_depth:
            raise _JsonBoundaryError("JSON value exceeds maximum depth")

        if isinstance(item, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise _JsonBoundaryError("invalid Unicode scalar value")
            continue
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, int):
            if abs(item) >= _MAX_JSON_INTEGER_MAGNITUDE:
                raise _JsonBoundaryError(
                    f"JSON integer exceeds {MAX_JSON_INTEGER_DIGITS} digits"
                )
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise _JsonBoundaryError("non-finite JSON number")
            continue
        if not isinstance(item, (list, dict)):
            raise _JsonBoundaryError("unsupported JSON value type")

        identity = id(item)
        if identity in ancestors:
            raise _JsonBoundaryError("cyclic JSON value")
        ancestors.add(identity)
        stack.append((False, item, depth))
        if isinstance(item, list):
            for child in reversed(item):
                stack.append((True, child, depth + 1))
            continue
        for key, child in item.items():
            if not isinstance(key, str):
                raise _JsonBoundaryError("JSON object keys must be strings")
            stack.append((True, child, depth + 1))
            stack.append((True, key, depth + 1))


def _parse_json_int(raw: str) -> int:
    digits = raw[1:] if raw.startswith("-") else raw
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise _JsonBoundaryError(
            f"JSON integer exceeds {MAX_JSON_INTEGER_DIGITS} digits"
        )
    return int(raw)


def _parse_json_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise _JsonBoundaryError("non-finite JSON number")
    return value


def _reject_json_constant(_raw: str) -> None:
    raise _JsonBoundaryError("non-finite JSON number")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _JsonBoundaryError("duplicate JSON object key")
        value[key] = item
    return value


def _canonical_json_bytes(
    payload: Any,
    *,
    max_nodes: int = MAX_CANONICAL_JSON_NODES,
) -> bytes:
    _validate_json_value(payload, max_nodes=max_nodes)
    try:
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError, UnicodeError) as exc:
        raise _JsonBoundaryError("JSON serialization failed") from exc


def _journal_json_bytes(record: dict[str, Any]) -> bytes:
    encoded = _canonical_json_bytes(record, max_nodes=MAX_JOURNAL_NODES)
    if len(encoded) > MAX_JOURNAL_LINE_BYTES:
        raise _JsonBoundaryError(
            f"journal record exceeds {MAX_JOURNAL_LINE_BYTES} bytes"
        )
    return encoded


def _stable_digest(payload: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in HEX for char in value)
    )


def _ascii_decimal(value: Any, *, max_length: int = MAX_COMPACT_ID_LENGTH) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= max_length
        and value.isascii()
        and value.isdecimal()
    )


def _loaded_package_roots() -> list[tuple[str, Path]]:
    roots: list[tuple[str, Path]] = []
    for package_name in ("veqtor_docx", "veqtor_mcp"):
        package = importlib.import_module(package_name)
        package_paths = tuple(getattr(package, "__path__", ()))
        if not package_paths:
            raise ImportError(f"{package_name} has no package root")
        for raw_root in package_paths:
            roots.append((package_name, Path(raw_root)))
    return roots


def _raise_walk_error(exc: OSError) -> None:
    raise exc


def _strict_python_sources(root: Path) -> Iterator[Path]:
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"invalid package root: {root}")
    for raw_directory, directories, files in os.walk(
        root,
        topdown=True,
        onerror=_raise_walk_error,
        followlinks=False,
    ):
        directory = Path(raw_directory)
        directories.sort()
        files.sort()
        for name in directories:
            child_info = (directory / name).lstat()
            if stat.S_ISLNK(child_info.st_mode) or not stat.S_ISDIR(child_info.st_mode):
                raise ValueError(f"invalid package directory: {directory / name}")
        for name in files:
            if not name.endswith(".py"):
                continue
            path = directory / name
            file_info = path.lstat()
            if stat.S_ISLNK(file_info.st_mode) or not stat.S_ISREG(file_info.st_mode):
                raise ValueError(f"invalid Python source: {path}")
            yield path


def _source_snapshot_identity(
    roots: list[tuple[str, Path]] | None = None,
) -> str:
    files: list[dict[str, str]] = []
    try:
        package_roots = roots if roots is not None else _loaded_package_roots()
        if not package_roots:
            return SOURCE_SNAPSHOT_UNAVAILABLE
        for package_name, root in sorted(package_roots):
            for path in _strict_python_sources(root):
                content = path.read_bytes()
                package_file = f"{package_name}/{path.relative_to(root).as_posix()}"
                files.append(
                    {
                        "path": package_file,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
        if not files:
            return SOURCE_SNAPSHOT_UNAVAILABLE
        manifest = {
            "schema": SOURCE_SNAPSHOT_SCHEMA,
            "files": sorted(files, key=lambda item: (item["path"], item["sha256"])),
        }
        return f"{SOURCE_SNAPSHOT_PREFIX}{_stable_digest(manifest)}"
    except (ImportError, OSError, ValueError):
        return SOURCE_SNAPSHOT_UNAVAILABLE


SOURCE_SNAPSHOT_IDENTITY = _source_snapshot_identity()


def _record_number(record_id: str) -> int:
    if (
        not isinstance(record_id, str)
        or not record_id.startswith("dr_")
        or not _ascii_decimal(record_id[3:])
    ):
        raise ValueError(record_id)
    return int(record_id[3:])


def _check_record_schema(record: Any) -> int:
    def fail(detail: str) -> None:
        raise _RecordSchemaError(detail)

    if not isinstance(record, dict):
        fail("record is not an object")
    if record.get("schema_version") != SCHEMA_VERSION:
        fail("invalid schema_version")
    record_id = record.get("record_id")
    if not isinstance(record_id, str):
        fail("record_id missing")
    try:
        number = _record_number(record_id)
    except ValueError:
        fail("invalid record_id")
    record_type = record.get("record_type")
    if not isinstance(record_type, str) or record_type not in KNOWN_RECORD_TYPES:
        fail("invalid record_type")
    for key in ("created_at", "tool_name", "workspace"):
        if not isinstance(record.get(key), str) or not record[key]:
            fail(f"{key} missing")
    if record_type != _historical_tool_spec(record["tool_name"]).record_type:
        fail("record_type does not match tool_name")
    if not _is_sha256(record.get("result_sha256")):
        fail("invalid result_sha256")
    if record["result_sha256"] != _stable_digest(record.get("result", {})):
        fail("result_sha256 mismatch")
    if not _is_sha256(record.get("tool_result_sha256")):
        fail("invalid tool_result_sha256")
    for key in ("input", "result", "provenance", "producer"):
        if not isinstance(record.get(key), dict):
            fail(f"{key} missing")
    producer = record["producer"]
    if not isinstance(producer.get("name"), str) or not producer["name"]:
        fail("producer.name missing")
    if not isinstance(producer.get("version"), str) or not producer["version"]:
        fail("producer.version missing")
    if not isinstance(producer.get("build"), str) or not producer["build"]:
        fail("producer.build missing")
    return number


def _validated_record_bytes(record: dict[str, Any]) -> bytes:
    frame = _journal_json_bytes(record)
    _decode_record_payload(frame)
    return frame


def _decode_record_payload(raw: bytes) -> tuple[dict[str, Any], int]:
    if len(raw) > MAX_JOURNAL_LINE_BYTES:
        raise _JsonBoundaryError(
            f"journal record exceeds {MAX_JOURNAL_LINE_BYTES} bytes"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _JsonBoundaryError("invalid UTF-8") from exc
    try:
        record = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
            parse_int=_parse_json_int,
        )
    except json.JSONDecodeError as exc:
        raise _JsonBoundaryError(exc.msg) from exc
    except _JsonBoundaryError:
        raise
    except (RecursionError, ValueError, UnicodeError) as exc:
        raise _JsonBoundaryError("JSON decoder rejected input") from exc

    try:
        _validate_json_value(record, max_nodes=MAX_JOURNAL_NODES)
        number = _check_record_schema(record)
    except (_JsonBoundaryError, _RecordSchemaError):
        raise
    except (RecursionError, TypeError, ValueError, UnicodeError) as exc:
        raise _JsonBoundaryError("invalid record content") from exc
    return record, number


def _decode_record_line(
    raw: bytes,
    path: Path,
    line_no: int,
) -> tuple[dict[str, Any], int]:
    location = f"{path}:{line_no}"
    try:
        return _decode_record_payload(raw)
    except (_JsonBoundaryError, _RecordSchemaError) as exc:
        raise DecisionRecordError("journal_corrupt", f"{location}: {exc}") from exc


def _load_records(handle, path: Path) -> list[dict[str, Any]]:
    handle.seek(0)
    records: list[dict[str, Any]] = []
    last_id = 0
    line_no = 0
    while True:
        raw_line = handle.readline(MAX_JOURNAL_LINE_BYTES + 2)
        if not raw_line:
            break
        line_no += 1
        terminated = raw_line.endswith(b"\n")
        payload = raw_line[:-1] if terminated else raw_line
        if len(payload) > MAX_JOURNAL_LINE_BYTES:
            raise DecisionRecordError(
                "journal_corrupt",
                f"{path}:{line_no}: journal record exceeds "
                f"{MAX_JOURNAL_LINE_BYTES} bytes",
            )
        if not terminated:
            raise DecisionRecordError(
                "journal_corrupt",
                f"{path}:{line_no}: unterminated journal record",
            )
        record, current_id = _decode_record_line(payload, path, line_no)
        if current_id <= last_id:
            raise DecisionRecordError(
                "journal_corrupt",
                f"{path}:{line_no}: record_id is not strictly increasing",
            )
        last_id = current_id
        records.append(record)
    return records


def _next_record_id(records: list[dict[str, Any]]) -> str:
    high = 0
    for record in records:
        raw = record.get("record_id")
        try:
            high = max(high, _record_number(raw))
        except (TypeError, ValueError):
            continue
    return f"dr_{high + 1:03d}"


def _validate_journal_fd(fd: int, path: Path) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise DecisionRecordError("journal_not_file", f"{path} must be a file")
    if info.st_nlink != 1:
        raise DecisionRecordError(
            "journal_hardlink", f"{path} must not have multiple hard links"
        )


def _validate_sidecar_fd(fd: int, path: Path) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise DecisionRecordError(
            "sidecar_not_directory", f"{path} must be a directory"
        )


def _open_journal_for_append(sidecar_fd: int, path: Path):
    create_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND | O_NOFOLLOW
    existing_flags = os.O_RDWR | os.O_APPEND | O_NOFOLLOW
    fd: int | None = None
    created = False
    for attempt in range(JOURNAL_OPEN_ATTEMPTS):
        _validate_sidecar_fd(sidecar_fd, path.parent)
        try:
            fd = os.open(JOURNAL_NAME, create_flags, 0o600, dir_fd=sidecar_fd)
            created = True
            break
        except FileExistsError:
            try:
                fd = os.open(JOURNAL_NAME, existing_flags, dir_fd=sidecar_fd)
                break
            except FileNotFoundError:
                if attempt + 1 == JOURNAL_OPEN_ATTEMPTS:
                    raise
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise DecisionRecordError(
                        "journal_symlink", f"{path} must not be a symlink"
                    ) from exc
                raise
        except FileNotFoundError:
            if attempt + 1 == JOURNAL_OPEN_ATTEMPTS:
                raise
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise DecisionRecordError(
                    "journal_symlink", f"{path} must not be a symlink"
                ) from exc
            raise
    assert fd is not None
    try:
        _validate_journal_fd(fd, path)
        os.fchmod(fd, 0o600)
        if created:
            try:
                os.fsync(sidecar_fd)
            except OSError:
                os.close(fd)
                fd = None
                try:
                    os.unlink(JOURNAL_NAME, dir_fd=sidecar_fd)
                except OSError:
                    pass
                raise
        return os.fdopen(fd, "a+b")
    except Exception:
        if fd is not None:
            os.close(fd)
        raise


def _open_existing_journal_for_append(sidecar_fd: int, path: Path):
    """Open an export journal for append without any create fallback."""
    _validate_sidecar_fd(sidecar_fd, path.parent)
    try:
        fd = os.open(
            JOURNAL_NAME,
            os.O_RDWR | os.O_APPEND | O_NOFOLLOW | O_NONBLOCK,
            dir_fd=sidecar_fd,
        )
    except FileNotFoundError as exc:
        raise DecisionRecordError(
            "workspace_changed",
            "initialized workspace journal disappeared before export",
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise DecisionRecordError(
                "journal_symlink", f"{path} must not be a symlink"
            ) from exc
        raise
    try:
        _validate_journal_fd(fd, path)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a+b")
    except Exception:
        os.close(fd)
        raise


def _open_journal_for_read(sidecar_fd: int, path: Path):
    try:
        fd = os.open(
            JOURNAL_NAME,
            os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK,
            dir_fd=sidecar_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise DecisionRecordError(
                "journal_symlink", f"{path} must not be a symlink"
            ) from exc
        raise
    try:
        _validate_journal_fd(fd, path)
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def _append_locked(sidecar_fd: int, path: Path, record: dict[str, Any]) -> str:
    with _open_journal_for_append(sidecar_fd, path) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            records = _load_records(handle, path)
            return _append_to_loaded_journal(handle, records, record)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _append_to_loaded_journal(
    handle: Any,
    records: list[dict[str, Any]],
    record: dict[str, Any],
) -> str:
    record_id = _next_record_id(records)
    stored_record = {**record, "record_id": record_id}
    try:
        line = _validated_record_bytes(stored_record)
    except (_JsonBoundaryError, _RecordSchemaError) as exc:
        raise DecisionRecordError("record_invalid", str(exc)) from exc
    handle.seek(0, os.SEEK_END)
    handle.write(line + b"\n")
    handle.flush()
    os.fsync(handle.fileno())
    return record_id


def _record_error(exc: BaseException) -> str:
    if isinstance(exc, DecisionRecordError):
        return exc.code
    return "internal_error"


def _base_record(
    *,
    tool_name: str,
    workspace: Path,
    input_payload: dict[str, Any],
    result: dict[str, Any],
    tool_result: dict[str, Any] | None,
    provenance: dict[str, Any],
    clock: Clock,
) -> dict[str, Any]:
    full_result = result if tool_result is None else tool_result
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": _writable_tool_spec(tool_name).record_type,
        "created_at": _created_at(clock),
        "tool_name": tool_name,
        "workspace": str(workspace),
        "producer": {
            "name": "veqtor-mcp",
            "version": __version__,
            "build": SOURCE_SNAPSHOT_IDENTITY,
        },
        "input": input_payload,
        "result": result,
        "result_sha256": _stable_digest(result),
        "tool_result_sha256": _stable_digest(full_result),
        "provenance": provenance,
    }


def _preflight_record(record: dict[str, Any]) -> None:
    _validated_record_bytes({**record, "record_id": "dr_001"})


def write_record(
    *,
    workspace: Path,
    tool_name: str,
    input_payload: dict[str, Any],
    result: dict[str, Any],
    provenance: dict[str, Any],
    tool_result: dict[str, Any] | None = None,
    clock: Clock = utc_now,
) -> dict[str, Any]:
    """Write one journal record and return response metadata."""
    if disabled():
        return {"record_id": None, "record_status": "disabled"}
    try:
        root, expected_identity = _canonical_workspace(workspace)
        try:
            record = _base_record(
                tool_name=tool_name,
                workspace=root,
                input_payload=input_payload,
                result=result,
                tool_result=tool_result,
                provenance=provenance,
                clock=clock,
            )
            _preflight_record(record)
        except (_JsonBoundaryError, _RecordSchemaError) as exc:
            raise DecisionRecordError("record_invalid", str(exc)) from exc
        with _sidecar_for_write(
            root,
            expected_identity=expected_identity,
        ) as (sidecar, sidecar_fd):
            record_id = _append_locked(sidecar_fd, sidecar / JOURNAL_NAME, record)
    except Exception as exc:
        return {
            "record_id": None,
            "record_status": "write_failed",
            "record_error": _record_error(exc),
        }
    return {"record_id": record_id, "record_status": "written"}


def _parse_before_record_id(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return _record_number(value)
    except ValueError as exc:
        raise DecisionRecordError(
            "invalid_before_record_id", "before_record_id must look like dr_NNN"
        ) from exc


def _window_records(
    records: list[dict[str, Any]],
    *,
    limit: int,
    before_record_number: int | None,
    include_access_events: bool,
) -> tuple[list[dict[str, Any]], int, bool, str | None]:
    visible = [
        record
        for record in records
        if include_access_events or record.get("record_type") != ACCESS_RECORD_TYPE
    ]
    if before_record_number is not None:
        visible = [
            record
            for record in visible
            if _record_number(record["record_id"]) < before_record_number
        ]
    selected = visible[-limit:]
    truncated = len(visible) > len(selected)
    next_before = selected[0]["record_id"] if truncated and selected else None
    return selected, len(visible), truncated, next_before


def _path_digest(value: Any) -> dict[str, Any]:
    return {"sha256": _stable_digest({"path": value}), "omitted": True}


def _asserted_digest(value: Any) -> dict[str, Any]:
    return {"sha256": _stable_digest({"asserted": value}), "omitted": True}


def _nonnegative_int(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _list_count(value: Any) -> int | None:
    return len(value) if isinstance(value, list) else None


def _tracked_change_author(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 255:
        return None
    if any(ord(char) < 0x20 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        return None
    return value


def _strict_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _known_value(value: Any, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _error_code(value: Any) -> str | None:
    if (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and value.isascii()
        and value[0].islower()
        and all(char.islower() or char.isdecimal() or char == "_" for char in value)
    ):
        return value
    return None


def _producer_version(value: Any) -> str | None:
    if (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and value.isascii()
        and value[0].isdecimal()
        and all(char.isalnum() or char in ".+-" for char in value)
    ):
        return value
    return None


def _compact_record_id(value: Any) -> str | None:
    try:
        _record_number(value)
    except (TypeError, ValueError):
        return None
    return value


def _compact_created_at(value: Any) -> str | None:
    if not isinstance(value, str) or V1_CREATED_AT_PATTERN.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        return None
    return value if _format_v1_timestamp(parsed) == value else None


def _revision_id(value: Any) -> str | None:
    if _ascii_decimal(value):
        return value
    return None


def _change_unit_id(value: Any) -> str | None:
    if (
        isinstance(value, str)
        and 4 <= len(value) <= MAX_COMPACT_ID_LENGTH
        and value.startswith("cu_")
        and _ascii_decimal(value[3:])
    ):
        return value
    return None


def _part_name(value: Any) -> str | None:
    return value if value == DOCUMENT_PART_V1 else None


def _contains_incomplete_snapshot(value: Any) -> bool:
    if isinstance(value, dict):
        if {
            "count",
            "sha256",
            "sample",
            "truncated",
        }.issubset(value) and value.get("truncated") is True:
            return True
        return any(_contains_incomplete_snapshot(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_incomplete_snapshot(item) for item in value)
    return False


def _bounded_collection(
    value: Any,
    projector: Callable[[Any], Any | None],
    *,
    limit: int = COMPACT_SAMPLE_LIMIT,
) -> dict[str, Any]:
    if not isinstance(value, list):
        return _invalid_bounded_snapshot(value)
    raw = value
    sample: list[Any] = []
    filtered = False
    for item in raw:
        projected = projector(item)
        if projected is None:
            filtered = True
        elif len(sample) < limit:
            sample.append(projected)
            filtered = filtered or _contains_incomplete_snapshot(projected)
        else:
            filtered = True
    return {
        "count": len(raw),
        "sha256": _stable_digest(raw),
        "sample": sample,
        "truncated": filtered or len(sample) != len(raw),
    }


def _invalid_bounded_snapshot(value: Any) -> dict[str, Any]:
    return {
        "count": None,
        "sha256": _stable_digest(value),
        "sample": [],
        "truncated": True,
    }


def _bounded_mapping(
    value: Any,
    allowed_keys: frozenset[str] = EXTRACT_REVISION_CATEGORIES_V1,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _invalid_bounded_snapshot(value)
    raw = value
    items = [
        {"key": key, "value": item}
        for key, item in sorted(raw.items(), key=lambda pair: str(pair[0]))
    ]

    def project(item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        key = item.get("key")
        count = _nonnegative_int(item.get("value"))
        if key not in allowed_keys or count is None:
            return None
        return {"key": key, "value": count}

    snapshot = _bounded_collection(items, project)
    snapshot["sha256"] = _stable_digest(raw)
    return snapshot


def _validated_bounded_snapshot(
    value: Any,
    projector: Callable[[Any], Any | None],
) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not {
        "count",
        "sha256",
        "sample",
        "truncated",
    }.issubset(value):
        return None
    raw_count = value.get("count")
    digest = value.get("sha256")
    raw_sample = value.get("sample")
    declared_truncated = _strict_bool(value.get("truncated"))
    required_keys = {"count", "sha256", "sample", "truncated"}
    if (
        set(value) == required_keys
        and raw_count is None
        and _is_sha256(digest)
        and raw_sample == []
        and declared_truncated is True
    ):
        return {
            "count": None,
            "sha256": digest,
            "sample": [],
            "truncated": True,
        }
    count = _nonnegative_int(raw_count)
    if (
        count is None
        or not _is_sha256(digest)
        or not isinstance(raw_sample, list)
        or declared_truncated is None
        or count < len(raw_sample)
    ):
        return None
    filtered = set(value) != required_keys
    safe_sample: list[Any] = []
    for item in raw_sample:
        projected = projector(item)
        if projected is None:
            filtered = True
            continue
        if projected != item or _contains_incomplete_snapshot(projected):
            filtered = True
        if len(safe_sample) < COMPACT_SAMPLE_LIMIT:
            safe_sample.append(projected)
        else:
            filtered = True
    return {
        "count": count,
        "sha256": digest,
        "sample": safe_sample,
        "truncated": (declared_truncated or filtered or count != len(safe_sample)),
    }


def _revision_ids_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        snapshot = _validated_bounded_snapshot(value, _revision_id)
        return snapshot if snapshot is not None else _invalid_bounded_snapshot(value)
    return _bounded_collection(value, _revision_id)


def _observed_anchor_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None

    if value.get("schema_version") == "paragraph_ref.v1":
        required = {
            "schema_version",
            "ref_type",
            "file_sha256",
            "part_name",
            "paragraph_index",
            "paragraph_text_sha256",
            "reading_mode",
            "container_policy",
        }
        if (
            not required.issubset(value)
            or value.get("ref_type") != "paragraph"
            or not _is_sha256(value.get("file_sha256"))
            or _part_name(value.get("part_name")) != DOCUMENT_PART_V1
            or _nonnegative_int(value.get("paragraph_index")) is None
            or not _is_sha256(value.get("paragraph_text_sha256"))
            or value.get("reading_mode") != INSPECT_READING_MODE_V1
            or value.get("container_policy") != INSPECT_CONTAINER_POLICY_V1
        ):
            return None
        summary: dict[str, Any] = {
            "schema_version": "paragraph_ref.v1",
            "ref_type": "paragraph",
            "file_sha256": value["file_sha256"],
            "part_name": DOCUMENT_PART_V1,
            "paragraph_index": value["paragraph_index"],
            "paragraph_text_sha256": value["paragraph_text_sha256"],
            "reading_mode": INSPECT_READING_MODE_V1,
            "container_policy": INSPECT_CONTAINER_POLICY_V1,
        }
        if "revision_ids" in value:
            summary["revision_ids"] = _revision_ids_summary(value["revision_ids"])
        if "side" in value:
            if value["side"] != "paragraph_current":
                return None
            summary["side"] = "paragraph_current"
        return summary

    change_unit_id = _change_unit_id(value.get("change_unit_id"))
    if change_unit_id is None:
        return None
    summary: dict[str, Any] = {"change_unit_id": change_unit_id}
    schema_version = value.get("schema_version")
    if schema_version is not None:
        if schema_version != "change_unit_anchor.v2":
            return None
        summary["schema_version"] = schema_version
    if "file_sha256" in value:
        if not _is_sha256(value["file_sha256"]):
            return None
        summary["file_sha256"] = value["file_sha256"]
    if "container_policy" in value:
        if value["container_policy"] != INSPECT_CONTAINER_POLICY_V1:
            return None
        summary["container_policy"] = value["container_policy"]
    if "unit_fingerprint_sha256" in value:
        if not _is_sha256(value["unit_fingerprint_sha256"]):
            return None
        summary["unit_fingerprint_sha256"] = value["unit_fingerprint_sha256"]
    v2_marker_fields = {
        "schema_version",
        "container_policy",
        "unit_fingerprint_sha256",
    }
    v2_required_fields = {
        *v2_marker_fields,
        "change_unit_id",
        "file_sha256",
    }
    if any(
        key in value for key in v2_marker_fields
    ) and not v2_required_fields.issubset(value):
        return None
    if "part_name" in value:
        part_name = _part_name(value["part_name"])
        if part_name is None:
            return None
        summary["part_name"] = part_name
    if "revision_ids" in value:
        summary["revision_ids"] = _revision_ids_summary(value["revision_ids"])
    if "side" in value:
        side = _known_value(value["side"], MATCH_SIDES_V1)
        if side is None:
            return None
        summary["side"] = side
    if "clause_anchor" in value:
        clause = value["clause_anchor"]
        digest = _stable_digest(clause) if clause is not None else None
        if "clause_anchor_sha256" in value and value["clause_anchor_sha256"] != digest:
            return None
        summary["clause_anchor_sha256"] = digest
    elif "clause_anchor_sha256" in value:
        digest = value["clause_anchor_sha256"]
        if digest is not None and not _is_sha256(digest):
            return None
        summary["clause_anchor_sha256"] = digest
    return summary


def bounded_observed_anchors(value: Any) -> dict[str, Any]:
    return _bounded_collection(value, _observed_anchor_summary)


def _observed_inspection_ref_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) not in (
        {"paragraph_ref"},
        {"section_ref"},
    ):
        return None
    ref_kind = next(iter(value))
    ref = value[ref_kind]
    if not isinstance(ref, dict):
        return None
    if (
        not _is_sha256(ref.get("file_sha256"))
        or _part_name(ref.get("part_name")) is None
        or ref.get("reading_mode") != INSPECT_READING_MODE_V1
        or ref.get("container_policy") != INSPECT_CONTAINER_POLICY_V1
    ):
        return None
    summary: dict[str, Any] = {
        "file_sha256": ref["file_sha256"],
        "part_name": ref["part_name"],
        "reading_mode": ref["reading_mode"],
        "container_policy": ref["container_policy"],
    }
    if ref_kind == "paragraph_ref":
        if set(ref) != {
            "schema_version",
            "ref_type",
            "file_sha256",
            "part_name",
            "paragraph_index",
            "paragraph_text_sha256",
            "reading_mode",
            "container_policy",
        }:
            return None
        if (
            ref.get("schema_version") != "paragraph_ref.v1"
            or ref.get("ref_type") != "paragraph"
        ):
            return None
        index = _nonnegative_int(ref.get("paragraph_index"))
        if index is None or not _is_sha256(ref.get("paragraph_text_sha256")):
            return None
        summary.update(
            {
                "schema_version": ref["schema_version"],
                "ref_type": ref["ref_type"],
                "paragraph_index": index,
                "paragraph_text_sha256": ref["paragraph_text_sha256"],
            }
        )
    else:
        if set(ref) != {
            "schema_version",
            "ref_type",
            "file_sha256",
            "part_name",
            "heading_paragraph_index",
            "heading_text_sha256",
            "reading_mode",
            "container_policy",
        }:
            return None
        if (
            ref.get("schema_version") != "section_ref.v1"
            or ref.get("ref_type") != "section"
        ):
            return None
        index = _nonnegative_int(ref.get("heading_paragraph_index"))
        if index is None or not _is_sha256(ref.get("heading_text_sha256")):
            return None
        summary.update(
            {
                "schema_version": ref["schema_version"],
                "ref_type": ref["ref_type"],
                "heading_paragraph_index": index,
                "heading_text_sha256": ref["heading_text_sha256"],
            }
        )
    return {ref_kind: summary}


def bounded_observed_inspection_refs(value: Any) -> dict[str, Any]:
    return _bounded_collection(value, _observed_inspection_ref_summary)


def _observed_round_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not _is_sha256(value.get("sha256")):
        return None
    revision_count = _nonnegative_int(value.get("revision_count"))
    if revision_count is None:
        return None
    return {"sha256": value["sha256"], "revision_count": revision_count}


def _observed_applied_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    change_unit_id = _change_unit_id(value.get("change_unit_id"))
    operation = _known_value(value.get("operation"), APPLY_OPERATIONS_V1)
    if (
        change_unit_id is None
        or operation is None
        or "tracked_revision_ids" not in value
    ):
        return None
    return {
        "change_unit_id": change_unit_id,
        "operation": operation,
        "tracked_revision_ids": _revision_ids_summary(value["tracked_revision_ids"]),
    }


def _preflight_edit_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    edit_index = _nonnegative_int(value.get("edit_index"))
    status = value.get("status")
    if (
        edit_index is None
        or not isinstance(status, str)
        or status not in PREFLIGHT_EDIT_STATUSES_V1
    ):
        return None
    summary: dict[str, Any] = {"edit_index": edit_index, "status": status}
    change_unit_id = _change_unit_id(value.get("change_unit_id"))
    if change_unit_id is not None:
        summary["change_unit_id"] = change_unit_id
    operation = _known_value(value.get("operation"), APPLY_OPERATIONS_V1)
    if operation is not None:
        summary["operation"] = operation
    match_count = _nonnegative_int(value.get("match_count"))
    if match_count is not None:
        summary["match_count"] = match_count
    if "position_status" in value:
        position_status = _known_value(
            value.get("position_status"), PREFLIGHT_POSITION_STATUSES_V1
        )
        if position_status is not None:
            summary["position_status"] = position_status
    elif "position_supported" in value:
        # Preserve the compact shape of already-written v0.1 records while
        # new producers emit the closed position_status enum.
        position_supported = _strict_bool(value.get("position_supported"))
        if position_supported is not None:
            summary["position_supported"] = position_supported
    refusal_code = _error_code(value.get("refusal_code"))
    if refusal_code is not None:
        summary["refusal_code"] = refusal_code
    return summary


def _container_coverage_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    count_keys = (
        "indexed_paragraph_count",
        "body_paragraph_count",
        "table_cell_paragraph_count",
        "excluded_subtree_count",
        "excluded_paragraph_count",
    )
    counts = {key: _nonnegative_int(value.get(key)) for key in count_keys}
    excluded = value.get("excluded_by_kind")
    excluded_paragraphs = value.get("excluded_paragraphs_by_kind")
    allowed_exclusions = frozenset(
        {
            "alt_chunk",
            "alternate_content",
            "text_box",
            "nested_paragraph",
            "unknown_container",
        }
    )
    if (
        value.get("schema_version") != INSPECT_CONTAINER_POLICY_V1
        or any(item is None for item in counts.values())
        or not isinstance(excluded, dict)
        or not isinstance(excluded_paragraphs, dict)
        or any(key not in allowed_exclusions for key in excluded)
        or any(key not in allowed_exclusions for key in excluded_paragraphs)
    ):
        return None
    excluded_values = [_nonnegative_int(item) for item in excluded.values()]
    excluded_paragraph_values = [
        _nonnegative_int(item) for item in excluded_paragraphs.values()
    ]
    if (
        any(item is None for item in excluded_values)
        or any(item is None for item in excluded_paragraph_values)
        or counts["indexed_paragraph_count"]
        != counts["body_paragraph_count"] + counts["table_cell_paragraph_count"]
        or counts["excluded_subtree_count"]
        != sum(item for item in excluded_values if item is not None)
        or counts["excluded_paragraph_count"]
        != sum(item for item in excluded_paragraph_values if item is not None)
        or value.get("coverage_complete") is not (counts["excluded_subtree_count"] == 0)
        or value.get("legacy_two_field_anchor_safe")
        is not (counts["excluded_subtree_count"] == 0)
    ):
        return None
    return {
        "schema_version": INSPECT_CONTAINER_POLICY_V1,
        **counts,
        "excluded_by_kind": _bounded_mapping(excluded, allowed_exclusions),
        "excluded_paragraphs_by_kind": _bounded_mapping(
            excluded_paragraphs, allowed_exclusions
        ),
        "coverage_complete": counts["excluded_subtree_count"] == 0,
        "legacy_two_field_anchor_safe": counts["excluded_subtree_count"] == 0,
    }


def _revision_inventory_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") == "revision_inventory.v2":
        scope = _part_name(value.get("scope"))
        count_keys = (
            "tracked_text_revision_elements",
            "total_revision_elements",
            "in_scope_revision_elements",
            "decoded_revision_elements",
            "unsupported_revision_occurrences",
            "unsupported_revision_kind_count",
            "excluded_container_occurrences",
            "excluded_container_kind_count",
        )
        counts = {key: _nonnegative_int(value.get(key)) for key in count_keys}
        unsupported = value.get("unsupported_by_kind")
        excluded = value.get("excluded_by_container")
        container_policy = value.get("container_policy")
        container_summary = _container_coverage_summary(container_policy)
        if (
            scope != DOCUMENT_PART_V1
            or any(item is None for item in counts.values())
            or not isinstance(unsupported, dict)
            or not isinstance(excluded, dict)
            or container_summary is None
        ):
            return None
        unsupported_values = [_nonnegative_int(item) for item in unsupported.values()]
        excluded_values = [_nonnegative_int(item) for item in excluded.values()]
        allowed_exclusions = {
            "alt_chunk",
            "alternate_content",
            "text_box",
            "nested_paragraph",
            "unknown_container",
        }
        if (
            any(item is None for item in unsupported_values)
            or any(item is None for item in excluded_values)
            or any(key not in EXTRACT_REVISION_CATEGORIES_V1 for key in unsupported)
            or any(key not in allowed_exclusions for key in excluded)
            or counts["total_revision_elements"]
            != counts["in_scope_revision_elements"]
            + counts["excluded_container_occurrences"]
            or counts["in_scope_revision_elements"]
            != counts["decoded_revision_elements"]
            + counts["unsupported_revision_occurrences"]
            or sum(item for item in unsupported_values if item is not None)
            != counts["unsupported_revision_occurrences"]
            or sum(item for item in excluded_values if item is not None)
            != counts["excluded_container_occurrences"]
            or len(unsupported) != counts["unsupported_revision_kind_count"]
            or len(excluded) != counts["excluded_container_kind_count"]
            or value.get("partition_valid") is not True
            or value.get("all_in_scope_revision_elements_decoded")
            is not (counts["unsupported_revision_occurrences"] == 0)
            or value.get("all_revision_elements_decoded")
            is not (
                counts["unsupported_revision_occurrences"] == 0
                and counts["excluded_container_occurrences"] == 0
            )
        ):
            return None
        summary = {
            "schema_version": "revision_inventory.v2",
            "scope": DOCUMENT_PART_V1,
            **counts,
            "container_policy": container_summary,
            "unsupported_by_kind": _bounded_mapping(unsupported),
            "excluded_by_container": _bounded_mapping(
                excluded, frozenset(allowed_exclusions)
            ),
            "partition_valid": True,
            "all_in_scope_revision_elements_decoded": (
                counts["unsupported_revision_occurrences"] == 0
            ),
            "all_revision_elements_decoded": (
                counts["unsupported_revision_occurrences"] == 0
                and counts["excluded_container_occurrences"] == 0
            ),
        }
        if "emitted_change_unit_count" in value:
            emitted = _nonnegative_int(value.get("emitted_change_unit_count"))
            if emitted is None:
                return None
            summary["emitted_change_unit_count"] = emitted
        return summary
    if value.get("schema_version") != "revision_inventory.v1":
        return None
    scope = _part_name(value.get("scope"))
    total = _nonnegative_int(value.get("total_revision_elements"))
    decoded = _nonnegative_int(value.get("decoded_revision_elements"))
    unsupported_occurrences = _nonnegative_int(
        value.get("unsupported_revision_occurrences")
    )
    unsupported_kind_count = _nonnegative_int(
        value.get("unsupported_revision_kind_count")
    )
    change_unit_count = _nonnegative_int(value.get("emitted_change_unit_count"))
    unsupported = value.get("unsupported_by_kind")
    if (
        scope != DOCUMENT_PART_V1
        or None
        in {
            total,
            decoded,
            unsupported_occurrences,
            unsupported_kind_count,
            change_unit_count,
        }
        or not isinstance(unsupported, dict)
    ):
        return None
    occurrence_values = [_nonnegative_int(item) for item in unsupported.values()]
    if (
        any(item is None for item in occurrence_values)
        or any(key not in EXTRACT_REVISION_CATEGORIES_V1 for key in unsupported)
        or total != decoded + unsupported_occurrences
        or sum(item for item in occurrence_values if item is not None)
        != unsupported_occurrences
        or len(unsupported) != unsupported_kind_count
        or value.get("partition_valid") is not True
        or value.get("all_revision_elements_decoded")
        is not (unsupported_occurrences == 0)
    ):
        return None
    return {
        "schema_version": "revision_inventory.v1",
        "scope": DOCUMENT_PART_V1,
        "total_revision_elements": total,
        "decoded_revision_elements": decoded,
        "unsupported_revision_occurrences": unsupported_occurrences,
        "unsupported_revision_kind_count": unsupported_kind_count,
        "emitted_change_unit_count": change_unit_count,
        "unsupported_by_kind": _bounded_mapping(unsupported),
        "partition_valid": True,
        "all_revision_elements_decoded": unsupported_occurrences == 0,
    }


def _observed_match_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    part_name = _part_name(value.get("part_name"))
    side = _known_value(value.get("side"), MATCH_SIDES_V1 | {"paragraph_current"})
    if part_name is None or side is None or "revision_ids" not in value:
        return None
    clause = value.get("clause")
    summary = {
        "part_name": part_name,
        "revision_ids": _revision_ids_summary(value["revision_ids"]),
        "side": side,
        "clause_sha256": _stable_digest(clause) if clause is not None else None,
    }
    if side == "paragraph_current":
        paragraph_index = _nonnegative_int(value.get("paragraph_index"))
        if (
            part_name != DOCUMENT_PART_V1
            or paragraph_index is None
            or not _is_sha256(value.get("paragraph_text_sha256"))
            or value.get("reading_mode") != INSPECT_READING_MODE_V1
            or value.get("revision_ids") != []
        ):
            return None
        summary.update(
            {
                "paragraph_index": paragraph_index,
                "paragraph_text_sha256": value["paragraph_text_sha256"],
                "reading_mode": INSPECT_READING_MODE_V1,
            }
        )
    return summary


def _round_trip_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    summary: dict[str, Any] = {}
    status = _known_value(value.get("status"), ROUND_TRIP_STATUSES_V1)
    comparison = _known_value(
        value.get("comparison"),
        ROUND_TRIP_COMPARISONS_V1,
    )
    if status is not None:
        summary["status"] = status
    if comparison is not None:
        summary["comparison"] = comparison
    collateral = value.get("collateral_changes")
    if isinstance(collateral, list):
        summary["collateral_change_count"] = len(collateral)
    return summary


def _preflight_binding_summary(value: Any) -> dict[str, Any]:
    """Project only a self-consistent successful preflight/apply binding."""
    if not isinstance(value, dict):
        return {}
    candidate = value.get("preflight_candidate_sha256")
    output = value.get("output_sha256")
    if (
        value.get("preflight_binding_status") != "verified"
        or not _is_sha256(candidate)
        or not _is_sha256(output)
        or candidate != output
        or value.get("candidate_output_sha256_match") is not True
    ):
        return {}
    return {
        "preflight_binding_status": "verified",
        "preflight_candidate_sha256": candidate,
        "candidate_output_sha256_match": True,
    }


def _inspection_coverage_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("scan_complete") is not True:
        return None
    summary: dict[str, Any] = {"scan_complete": True}
    for key in (
        "body_paragraph_count",
        "nonempty_body_paragraph_count",
        "eligible_item_count",
        "returned_item_count",
        "cursor_offset",
    ):
        parsed = _nonnegative_int(value.get(key))
        if parsed is None:
            return None
        summary[key] = parsed
    truncated = _strict_bool(value.get("output_truncated"))
    if truncated is None:
        return None
    summary["output_truncated"] = truncated
    complete_matches = value.get("complete_literal_match_count")
    if complete_matches is None:
        summary["complete_literal_match_count"] = None
    else:
        parsed_matches = _nonnegative_int(complete_matches)
        if parsed_matches is None:
            return None
        summary["complete_literal_match_count"] = parsed_matches
    closed_lists = {
        "included_parts": [DOCUMENT_PART_V1],
        "included_containers": ["body", "table_cell"],
    }
    for key, expected in closed_lists.items():
        if value.get(key) != expected:
            return None
        summary[key] = list(expected)
    excluded_parts = value.get("excluded_parts")
    if not isinstance(excluded_parts, list) or excluded_parts[
        : len(INSPECT_FIXED_EXCLUDED_PARTS_V1)
    ] != list(INSPECT_FIXED_EXCLUDED_PARTS_V1):
        return None
    dynamic_parts = excluded_parts[len(INSPECT_FIXED_EXCLUDED_PARTS_V1) :]
    safe_dynamic_parts = [
        normalized_internal_package_part_name(part_name) for part_name in dynamic_parts
    ]
    if any(
        part_name is None for part_name in safe_dynamic_parts
    ) or dynamic_parts != sorted(set(dynamic_parts)):
        return None
    # The live inspect result discloses safe package-relative altChunk targets so a
    # caller can understand the exact coverage gap. Those names are controlled by
    # the document, however, and may themselves contain private matter labels. The
    # compact journal projection therefore retains only the fixed public scope and
    # a bounded, re-checkable fingerprint of the complete dynamic tail.
    summary["excluded_parts"] = list(INSPECT_FIXED_EXCLUDED_PARTS_V1)
    summary["excluded_internal_parts"] = {
        "count": len(dynamic_parts),
        "sha256": _stable_digest(dynamic_parts),
        "sample": [],
        "truncated": bool(dynamic_parts),
    }
    container_coverage = _container_coverage_summary(value.get("container_coverage"))
    if container_coverage is None:
        return None
    summary["container_coverage"] = container_coverage
    return summary


def _inspection_limits_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    count_keys = (
        "requested_max_items",
        "max_items",
        "max_phrases",
        "max_phrase_chars",
        "max_total_phrase_chars",
        "max_paragraph_text_chars",
        "max_returned_text_chars",
        "max_indexed_paragraphs",
        "max_aggregate_text_chars",
        "max_literal_match_candidates",
        "max_literal_occurrences_per_candidate",
    )
    summary: dict[str, Any] = {}
    for key in count_keys:
        parsed = _nonnegative_int(value.get(key))
        if parsed is None or parsed == 0:
            return None
        summary[key] = parsed
    if value.get("wall_clock_partial_results") is not False:
        return None
    summary["wall_clock_partial_results"] = False
    return summary


def _summary_result(record: dict[str, Any]) -> dict[str, Any]:
    result = record["result"]
    projection_kind = _historical_tool_spec(record["tool_name"]).projection_kind
    if result.get("status") == RESULT_STATUS_ERROR:
        summary: dict[str, Any] = {"status": RESULT_STATUS_ERROR}
        error_code = _error_code(result.get("error_code"))
        if error_code is not None:
            summary["error_code"] = error_code
        if "error" in result:
            summary["error_sha256"] = _stable_digest(result["error"])
            summary["error_omitted"] = True
        return summary
    if projection_kind == "list_rounds":
        rounds = result.get("rounds")
        summary = {
            "status": _known_value(result.get("status"), V1_OK_STATUSES),
            "folder": _path_digest(result.get("folder")),
            "round_count": _list_count(rounds) if "rounds" in result else None,
            "rounds": (
                _bounded_collection(rounds, _observed_round_summary)
                if "rounds" in result
                else None
            ),
            "skipped_count": (
                _list_count(result["skipped"]) if "skipped" in result else None
            ),
        }
        if "revision_count_basis" in result:
            summary["revision_count_basis"] = _known_value(
                result.get("revision_count_basis"), REVISION_COUNT_BASES_V1
            )
        return summary
    if projection_kind == "extract_redlines":
        summary = {
            "status": _known_value(result.get("status"), V1_OK_STATUSES),
            "path": _path_digest(result.get("path")),
            "file_sha256": result.get("file_sha256")
            if _is_sha256(result.get("file_sha256"))
            else None,
            "part_name": _part_name(result.get("part_name")),
            "revision_count": _nonnegative_int(result.get("revision_count")),
            "change_unit_count": _nonnegative_int(result.get("change_unit_count")),
            "unsupported_revisions": (
                _bounded_mapping(result["unsupported_revisions"])
                if "unsupported_revisions" in result
                else None
            ),
        }
        if "revision_inventory" in result:
            summary["revision_inventory"] = _revision_inventory_summary(
                result.get("revision_inventory")
            )
        if "revision_count_basis" in result:
            summary["revision_count_basis"] = _known_value(
                result.get("revision_count_basis"), REVISION_COUNT_BASES_V1
            )
        return summary
    if projection_kind == "inspect_document":
        mode = _known_value(result.get("mode"), INSPECT_MODES_V1)
        coverage = _inspection_coverage_summary(result.get("coverage"))
        limits = _inspection_limits_summary(result.get("limits"))
        if mode is None or coverage is None or limits is None:
            return {
                "status": _known_value(result.get("status"), V1_OK_STATUSES),
                "compact_projection_complete": False,
                "compact_projection_error": "invalid_inspection_result",
            }
        return {
            "status": _known_value(result.get("status"), V1_OK_STATUSES),
            "path": _path_digest(result.get("path")),
            "file_sha256": (
                result.get("file_sha256")
                if _is_sha256(result.get("file_sha256"))
                else None
            ),
            "part_name": _part_name(result.get("part_name")),
            "mode": mode,
            "search_scope": _known_value(
                result.get("search_scope"), {INSPECT_SEARCH_SCOPE_V1}
            ),
            "reading_mode": _known_value(
                result.get("reading_mode"), {INSPECT_READING_MODE_V1}
            ),
            "container_policy": _known_value(
                result.get("container_policy"), {INSPECT_CONTAINER_POLICY_V1}
            ),
            "has_tracked_text_revisions": _strict_bool(
                result.get("has_tracked_text_revisions")
            ),
            "revision_inventory": _revision_inventory_summary(
                result.get("revision_inventory")
            ),
            "coverage": coverage,
            "limits": limits,
            "section_count": _nonnegative_int(result.get("section_count")),
            "match_count": _nonnegative_int(result.get("match_count")),
            "paragraph_count": _nonnegative_int(result.get("paragraph_count")),
            "next_cursor_present": _strict_bool(result.get("next_cursor_present")),
        }
    if projection_kind == "apply_edits":
        summary = {
            "status": _known_value(result.get("status"), V1_OK_STATUSES),
            "output_sha256": result.get("output_sha256")
            if _is_sha256(result.get("output_sha256"))
            else None,
            "applied": (
                _bounded_collection(result["applied"], _observed_applied_summary)
                if "applied" in result
                else None
            ),
            "round_trip_check": _round_trip_summary(result.get("round_trip_check")),
        }
        if _is_sha256(result.get("source_sha256")):
            summary["source_sha256"] = result["source_sha256"]
        author = _tracked_change_author(result.get("tracked_change_author"))
        if author is not None:
            summary["tracked_change_author"] = author
        summary.update(_preflight_binding_summary(result))
        return summary
    if projection_kind == "preflight_edits":
        return {
            "status": _known_value(result.get("status"), V1_OK_STATUSES),
            "source_sha256": result.get("source_sha256")
            if _is_sha256(result.get("source_sha256"))
            else None,
            "candidate_sha256": result.get("candidate_sha256")
            if _is_sha256(result.get("candidate_sha256"))
            else None,
            "observed_candidate_sha256": result.get("observed_candidate_sha256")
            if _is_sha256(result.get("observed_candidate_sha256"))
            else None,
            "batch_applicable": _strict_bool(result.get("batch_applicable")),
            "blocking_edit_index": _nonnegative_int(result.get("blocking_edit_index")),
            "refusal_code": _error_code(result.get("refusal_code")),
            "failure_phase": _known_value(
                result.get("failure_phase"), PREFLIGHT_FAILURE_PHASES_V1
            ),
            "tracked_change_author": _tracked_change_author(
                result.get("tracked_change_author")
            ),
            "edits": (
                _bounded_collection(result["edits"], _preflight_edit_summary)
                if "edits" in result
                else None
            ),
            "round_trip_check": _round_trip_summary(result.get("round_trip_check")),
        }
    if projection_kind == "verify_quote":
        return {
            "status": _known_value(result.get("status"), V1_OK_STATUSES),
            "verdict": _known_value(result.get("verdict"), VERIFY_VERDICTS_V1),
            "exact": _strict_bool(result.get("exact")),
            "checked_anchor": _observed_anchor_summary(result.get("checked_anchor")),
            "matches": (
                _bounded_collection(result["matches"], _observed_match_summary)
                if "matches" in result
                else None
            ),
            "diff_count": _list_count(result["diff"]) if "diff" in result else None,
        }
    if projection_kind == "export_decision_record":
        next_before = result.get("next_before_record_id")
        summary = {
            "status": _known_value(result.get("status"), V1_OK_STATUSES),
            "total_count": _nonnegative_int(result.get("total_count")),
            "access_count": _nonnegative_int(result.get("access_count")),
            "returned_count": _nonnegative_int(result.get("returned_count")),
            "truncated": _strict_bool(result.get("truncated")),
            "next_before_record_id": (
                None if next_before is None else _compact_record_id(next_before)
            ),
            "payloads": _known_value(result.get("payloads"), V1_EXPORT_PAYLOADS),
        }
        if "records_scope" in result:
            summary.update(
                {
                    "records_scope": _known_value(
                        result.get("records_scope"), V1_EXPORT_RECORDS_SCOPES
                    ),
                    "total_count_scope": _known_value(
                        result.get("total_count_scope"),
                        V1_EXPORT_TOTAL_COUNT_SCOPES,
                    ),
                    "access_events_in_records": _strict_bool(
                        result.get("access_events_in_records")
                    ),
                    "access_count_scope": _known_value(
                        result.get("access_count_scope"),
                        V1_EXPORT_ACCESS_COUNT_SCOPES,
                    ),
                    "access_count_includes_current_export": _strict_bool(
                        result.get("access_count_includes_current_export")
                    ),
                }
            )
        return summary
    raise _RecordSchemaError("invalid tool_name")


def _summary_provenance(record: dict[str, Any]) -> dict[str, Any]:
    provenance = record["provenance"]
    projection_kind = _historical_tool_spec(record["tool_name"]).projection_kind
    summary: dict[str, Any] = {}
    for key in (
        "file_sha256",
        "source_sha256",
        "observed_source_sha256",
        "observed_candidate_sha256",
        "output_sha256",
    ):
        if key in provenance and _is_sha256(provenance[key]):
            summary[key] = provenance[key]
    if _part_name(provenance.get("part_name")) is not None:
        summary["part_name"] = provenance["part_name"]
    verdict = _known_value(provenance.get("verdict"), VERIFY_VERDICTS_V1)
    if verdict is not None:
        summary["verdict"] = verdict
    edit_index = _nonnegative_int(provenance.get("edit_index"))
    if edit_index is not None:
        summary["edit_index"] = edit_index
    failure_phase = _known_value(
        provenance.get("failure_phase"), PREFLIGHT_FAILURE_PHASES_V1
    )
    if failure_phase is not None:
        summary["failure_phase"] = failure_phase
    summary.update(_preflight_binding_summary(provenance))
    tracked_change_author = _tracked_change_author(
        provenance.get("tracked_change_author")
    )
    if tracked_change_author is not None:
        summary["tracked_change_author"] = tracked_change_author
    if "claimed_source_sha256" in provenance:
        summary["claimed_source_sha256"] = _asserted_digest(
            provenance["claimed_source_sha256"]
        )
    for key in ("path", "folder", "workspace", "source_path", "output_path"):
        if key in provenance:
            summary[key] = _path_digest(provenance[key])
    if "checked_anchor" in provenance:
        summary["checked_anchor"] = _observed_anchor_summary(
            provenance["checked_anchor"]
        )
    if "input_anchor" in provenance:
        summary["input_anchor"] = _asserted_digest(provenance["input_anchor"])
    if "anchors" in provenance:
        anchors = provenance["anchors"]
        if isinstance(anchors, dict):
            snapshot = _validated_bounded_snapshot(anchors, _observed_anchor_summary)
            summary["anchors"] = (
                snapshot if snapshot is not None else _invalid_bounded_snapshot(anchors)
            )
        elif isinstance(anchors, list):
            if (
                projection_kind in {"extract_redlines", "verify_quote"}
                and record["result"].get("status") != RESULT_STATUS_ERROR
            ):
                summary["anchors"] = bounded_observed_anchors(anchors)
            else:
                summary["anchors"] = {
                    "count": len(anchors),
                    "sha256": _stable_digest(anchors),
                    "sample": [],
                    "truncated": bool(anchors),
                }
        else:
            summary["anchors"] = _invalid_bounded_snapshot(anchors)
    if "applied" in provenance:
        summary["applied"] = _bounded_collection(
            provenance["applied"], _observed_applied_summary
        )
    if "round_trip_check" in provenance:
        summary["round_trip_check"] = _round_trip_summary(
            provenance["round_trip_check"]
        )
    if projection_kind == "list_rounds":
        if "rounds" in provenance:
            summary["rounds"] = _bounded_collection(
                provenance["rounds"], _observed_round_summary
            )
        if "skipped" in provenance:
            summary["skipped_count"] = _list_count(provenance["skipped"])
    if projection_kind == "inspect_document":
        mode = _known_value(provenance.get("mode"), INSPECT_MODES_V1)
        if mode is not None:
            summary["mode"] = mode
        for key, expected in (
            ("search_scope", INSPECT_SEARCH_SCOPE_V1),
            ("reading_mode", INSPECT_READING_MODE_V1),
            ("container_policy", INSPECT_CONTAINER_POLICY_V1),
        ):
            known = _known_value(provenance.get(key), {expected})
            if known is not None:
                summary[key] = known
        tracked = _strict_bool(provenance.get("has_tracked_text_revisions"))
        if tracked is not None:
            summary["has_tracked_text_revisions"] = tracked
        coverage = _inspection_coverage_summary(provenance.get("coverage"))
        if coverage is not None:
            summary["coverage"] = coverage
        limits = _inspection_limits_summary(provenance.get("limits"))
        if limits is not None:
            summary["limits"] = limits
        if "inspection_refs" in provenance:
            refs = _validated_bounded_snapshot(
                provenance["inspection_refs"],
                _observed_inspection_ref_summary,
            )
            summary["inspection_refs"] = (
                refs
                if refs is not None
                else _invalid_bounded_snapshot(provenance["inspection_refs"])
            )
    if "revision_count_basis" in provenance:
        summary["revision_count_basis"] = _known_value(
            provenance.get("revision_count_basis"), REVISION_COUNT_BASES_V1
        )
    if "revision_inventory" in provenance:
        inventory = _revision_inventory_summary(provenance["revision_inventory"])
        if inventory is not None:
            summary["revision_inventory"] = inventory
    return summary


def _producer_summary(value: Any) -> dict[str, Any]:
    producer = value if isinstance(value, dict) else {}
    summary: dict[str, Any] = {}
    name = producer.get("name")
    if name == "veqtor-mcp":
        summary["name"] = name
    else:
        summary["name"] = "legacy-unvalidated"
        summary["name_sha256"] = _stable_digest({"producer.name": name})
    version = _producer_version(producer.get("version"))
    if version is not None:
        summary["version"] = version
    else:
        summary["version"] = "legacy-unvalidated"
        summary["version_sha256"] = _stable_digest(
            {"producer.version": producer.get("version")}
        )
    build = producer.get("build")
    known_build = build == SOURCE_SNAPSHOT_UNAVAILABLE
    if isinstance(build, str) and build.startswith(SOURCE_SNAPSHOT_PREFIX):
        known_build = _is_sha256(build.removeprefix(SOURCE_SNAPSHOT_PREFIX))
    if known_build:
        summary["build"] = build
    else:
        summary["build"] = "legacy-unvalidated"
        summary["build_sha256"] = _stable_digest({"producer.build": build})
    return summary


def _compact_record(record: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "schema_version": record["schema_version"],
        "record_type": record["record_type"],
        "record_id": record["record_id"],
        "tool_name": record["tool_name"],
        "workspace": _path_digest(record["workspace"]),
        "producer": _producer_summary(record["producer"]),
        "input": {
            "sha256": _stable_digest(record["input"]),
            "omitted": True,
        },
        "result": _summary_result(record),
        "result_sha256": record["result_sha256"],
        "tool_result_sha256": record["tool_result_sha256"],
        "provenance": _summary_provenance(record),
        "payloads": PAYLOAD_COMPACT,
    }
    created_at = _compact_created_at(record["created_at"])
    if created_at is not None:
        compact["created_at"] = created_at
    else:
        compact["created_at"] = "legacy-unvalidated"
        compact["created_at_sha256"] = _stable_digest(
            {"created_at": record["created_at"]}
        )
    return compact


def _compact_records(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return [_compact_record(record) for record in selected]
    except DecisionRecordError:
        raise
    except Exception as exc:
        raise DecisionRecordError(
            "journal_corrupt", "compact projection failed"
        ) from exc


def _read_options(
    max_records: int | None,
    before_record_id: str | None,
    include_payload: bool,
) -> tuple[int, int | None]:
    if type(include_payload) is not bool:
        raise DecisionRecordError(
            "invalid_include_payload", "include_payload must be a boolean"
        )
    if max_records is None:
        limit = DEFAULT_MAX_RECORDS
    elif not isinstance(max_records, int) or max_records < 1:
        raise DecisionRecordError(
            "invalid_max_records", "max_records must be a positive integer"
        )
    else:
        limit = min(max_records, MAX_MAX_RECORDS)
    return limit, _parse_before_record_id(before_record_id)


def _records_snapshot(
    root: Path,
    records: list[dict[str, Any]],
    *,
    limit: int,
    before_record_number: int | None,
    include_access_events: bool,
    include_payload: bool,
) -> dict[str, Any]:
    selected, total, truncated, next_before = _window_records(
        records,
        limit=limit,
        before_record_number=before_record_number,
        include_access_events=include_access_events,
    )
    access_count = sum(
        1 for record in records if record.get("record_type") == ACCESS_RECORD_TYPE
    )
    output_records = selected if include_payload else _compact_records(selected)
    return {
        "workspace": str(root) if include_payload else _path_digest(str(root)),
        "total_count": total,
        "access_count": access_count,
        "truncated": truncated,
        "next_before_record_id": next_before,
        "records": output_records,
        "payloads": PAYLOAD_FULL if include_payload else PAYLOAD_COMPACT,
    }


def _read_records(
    workspace: str,
    max_records: int | None = None,
    before_record_id: str | None = None,
    include_access_events: bool = False,
    include_payload: bool = False,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> dict[str, Any]:
    limit, before_record_number = _read_options(
        max_records, before_record_id, include_payload
    )
    if expected_identity is None:
        root, expected_identity = _canonical_workspace(workspace)
    else:
        root = Path(workspace)
    with _sidecar_for_read(root, expected_identity=expected_identity) as sidecar_info:
        if sidecar_info is None:
            return {
                "workspace": str(root) if include_payload else _path_digest(str(root)),
                "total_count": 0,
                "access_count": 0,
                "truncated": False,
                "next_before_record_id": None,
                "records": [],
                "payloads": PAYLOAD_FULL if include_payload else PAYLOAD_COMPACT,
            }
        sidecar, sidecar_fd = sidecar_info
        path = sidecar / JOURNAL_NAME
        handle = _open_journal_for_read(sidecar_fd, path)
        if handle is None:
            return {
                "workspace": str(root) if include_payload else _path_digest(str(root)),
                "total_count": 0,
                "access_count": 0,
                "truncated": False,
                "next_before_record_id": None,
                "records": [],
                "payloads": PAYLOAD_FULL if include_payload else PAYLOAD_COMPACT,
            }
        with handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                records = _load_records(handle, path)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    return _records_snapshot(
        root,
        records,
        limit=limit,
        before_record_number=before_record_number,
        include_access_events=include_access_events,
        include_payload=include_payload,
    )


def read_records(
    workspace: str,
    max_records: int | None = None,
    before_record_id: str | None = None,
    include_access_events: bool = False,
    include_payload: bool = False,
) -> dict[str, Any]:
    """Read records without exposing raw filesystem failures or paths."""
    try:
        return _read_records(
            workspace,
            max_records,
            before_record_id,
            include_access_events,
            include_payload,
        )
    except DecisionRecordError:
        raise
    except OSError as exc:
        raise DecisionRecordError(
            "workspace_unreadable", "workspace cannot be read"
        ) from exc


def _read_records_for_existing_identity(
    root: Path,
    expected_identity: tuple[int, int],
    max_records: int | None,
    before_record_id: str | None,
) -> dict[str, Any]:
    """Read only the workspace identity already approved for this export."""
    try:
        return _read_records(
            str(root),
            max_records,
            before_record_id,
            expected_identity=expected_identity,
        )
    except DecisionRecordError:
        raise
    except OSError as exc:
        raise DecisionRecordError(
            "workspace_unreadable", "workspace cannot be read"
        ) from exc


def export_records_with_access_event(
    *,
    workspace: Path,
    max_records: int | None,
    before_record_id: str | None,
    input_payload: dict[str, Any],
    result_factory: ExportResultFactory,
    clock: Clock = utc_now,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Atomically snapshot compact records and append the matching access event."""
    limit, before_record_number = _read_options(max_records, before_record_id, False)
    root, expected_identity = _canonical_workspace(workspace)
    _require_existing_export_workspace(root, expected_identity)
    if disabled():
        return (
            _read_records_for_existing_identity(
                root,
                expected_identity,
                max_records,
                before_record_id,
            ),
            {"record_id": None, "record_status": "disabled"},
        )

    snapshot: dict[str, Any] | None = None
    try:
        with _sidecar_for_existing_write(
            root,
            expected_identity=expected_identity,
        ) as (sidecar, sidecar_fd):
            path = sidecar / JOURNAL_NAME
            with _open_existing_journal_for_append(sidecar_fd, path) as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    loaded = _load_records(handle, path)
                    snapshot = _records_snapshot(
                        root,
                        loaded,
                        limit=limit,
                        before_record_number=before_record_number,
                        include_access_events=False,
                        include_payload=False,
                    )
                    result = result_factory(snapshot)
                    record = _base_record(
                        tool_name="export_decision_record",
                        workspace=root,
                        input_payload=input_payload,
                        result=result,
                        tool_result=None,
                        provenance={"workspace": snapshot["workspace"]},
                        clock=clock,
                    )
                    try:
                        _preflight_record(record)
                    except (_JsonBoundaryError, _RecordSchemaError) as exc:
                        raise DecisionRecordError("record_invalid", str(exc)) from exc
                    record_id = _append_to_loaded_journal(handle, loaded, record)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception as exc:
        # Once a snapshot exists, an append or fsync failure has unknown commit
        # status. Return that frozen snapshot and never re-read or retry the append.
        if (
            snapshot is None
            and isinstance(exc, DecisionRecordError)
            and exc.code == "workspace_changed"
        ):
            raise
        if snapshot is None:
            snapshot = _read_records_for_existing_identity(
                root,
                expected_identity,
                max_records,
                before_record_id,
            )
        return snapshot, {
            "record_id": None,
            "record_status": "write_failed",
            "record_error": _record_error(exc),
        }
    return snapshot, {"record_id": record_id, "record_status": "written"}
