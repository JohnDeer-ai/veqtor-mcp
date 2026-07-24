# SPDX-License-Identifier: Apache-2.0
"""Bounded seed-centred Round Map implementation for the v0.3 MCP surface."""

from __future__ import annotations

import errno
import hashlib
import heapq
import json
import os
import re
import stat
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Iterable, Iterator

from veqtor_docx._ooxml import (
    ArchiveValidationError,
    DocxError,
    ExpandedOutputBudget,
    ResourceLimitError,
    UserPathError,
    resolve_user_path,
)
from veqtor_docx.inspect import (
    InspectError,
    _Paragraph,
    _Section,
    _Snapshot,
    _load_snapshot_from_payload,
    _paragraph_ref,
    _resolve_paragraph,
    _section_ref,
)

from . import records


ROUND_MAP_LIMITS: dict[str, Any] = {
    "candidate_docx_files": 500,
    "candidate_compressed_input_bytes": 524_288_000,
    "candidate_expanded_bytes": 524_288_000,
    "compressed_bytes_per_docx": 52_428_800,
    "indexed_paragraphs_per_docx": 10_000,
    "accepted_current_chars_per_docx": 2_000_000,
    "journal_apply_records": 10_000,
    "document_nodes": 10_500,
    "document_observations": 500,
    "paragraph_nodes": 10_001,
    "section_nodes": 10_001,
    "recorded_derivation_relationships": 10_000,
    "exact_equality_relationships": 10_000,
    "navigation_relationships": 10_000,
    "resolution_items": 10_500,
    "conflict_items": 10_000,
    "total_map_items": 70_000,
    "sample_items": 20,
    "default_page_items": 50,
    "maximum_page_items": 100,
    "journal_bytes": 67_108_864,
    "wall_clock_partial_results": False,
    "semantic_or_vector_search": False,
}

DEFAULT_MAX_ITEMS = 50
MAX_ITEMS = 100
_DOCUMENT_PART = "word/document.xml"
_MAX_CURSOR_OFFSET_DIGITS = 128
_CURSOR_RE = re.compile(
    rf"^rm1:([1-9][0-9]{{0,{_MAX_CURSOR_OFFSET_DIGITS - 1}}}):"
    rf"([0-9a-f]{{64}})(?![\s\S])"
)
_SHA_RE = re.compile(r"^[0-9a-f]{64}(?![\s\S])")
_PARAGRAPH_REF_KEYS = frozenset(
    {
        "schema_version",
        "ref_type",
        "file_sha256",
        "part_name",
        "paragraph_index",
        "paragraph_text_sha256",
        "reading_mode",
        "container_policy",
    }
)
_SEED_KEYS = frozenset({"schema_version", "path", "paragraph_ref"})
_TYPE_RANK = {
    "document_node": 0,
    "document_observation": 1,
    "paragraph_node": 2,
    "section_node": 3,
    "relationship": 4,
    "resolution": 5,
    "conflict": 6,
}
_STRENGTHENED_FIELDS = (
    "preflight_binding_status",
    "preflight_candidate_sha256",
    "candidate_output_sha256_match",
)
_CURRENT_COMPARISON = "ooxml_semantic_diff_outside_touched_anchors"
_LEGACY_COMPARISON = "exact"
_RECORDED_BASIS_IDENTITY = {
    "schema_version": "recorded_derivation_basis_identity.v1",
    "record_schema_version": "decision_record.v1",
    "tool_name": "apply_edits",
    "record_type": "decision.v1",
    "assurance": "best_effort_local_non_tamper_evident",
    "derivation_scope": "document_bytes_only",
}
_POLICY_VERSIONS = {
    "mcp_contract": "veqtor.mcp.v0.3",
    "item_schema": "round_map_item.v1",
    "reading_mode": "accepted_current_v1",
    "container_policy": "canonical_body_flow_v1",
    "search_scope": "word_document_xml_body_v1",
    "recorded_derivation_basis": "recorded_derivation_basis.v1",
    "exact_equality_basis": "exact_content_equality_basis.v1",
    "navigation_basis": "navigation_candidate_basis.v1",
    "item_order": "type_rank_then_ascii_id_v1",
}


class RoundMapError(DocxError):
    """One sanitized, stable Round Map refusal."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class RoundMapComputation:
    result: dict[str, Any]
    proof: _RoundMapProof

    @property
    def workspace(self) -> Path:
        return Path(self.proof.authority.workspace_path)

    @property
    def workspace_identity(self) -> tuple[int, int]:
        return self.proof.authority.workspace_identity


@dataclass(frozen=True)
class _RoundMapProof:
    authority: _SourceEvidence
    item_fingerprints: tuple[_ItemFingerprint, ...]
    full_result_set_sha256: str
    item_type_counts: tuple[tuple[str, int], ...]
    relationship_counts: tuple[tuple[str, int], ...]
    resolution_counts: tuple[tuple[str, int], ...]
    record_only_document_count: int

    @property
    def filenames(self) -> tuple[str, ...]:
        return _authority_filenames(self.authority)

    @property
    def folder(self) -> str:
        return self.authority.workspace_path

    @property
    def seed_path(self) -> str:
        return self.authority.seed_path

    @property
    def seed_ref(self) -> dict[str, Any]:
        return _authority_seed_ref(self.authority)

    @property
    def seed_document_id(self) -> str:
        return _authority_seed_paragraph(self.authority).document_id

    @property
    def seed_paragraph_id(self) -> str:
        return _authority_seed_paragraph(self.authority).paragraph_id

    @property
    def ordering_source(self) -> str:
        return self.authority.ordering_source

    @property
    def filename_manifest_sha256(self) -> str:
        return _authority_filename_manifest_sha256(self.authority)

    @property
    def filesystem_snapshot_sha256(self) -> str:
        return _authority_filesystem_snapshot_sha256(self.authority)

    @property
    def journal_snapshot_sha256(self) -> str:
        return _authority_journal_snapshot_sha256(self.authority)

    @property
    def relevant_apply_record_count(self) -> int:
        return _authority_relevant_apply_record_count(self.authority)

    @property
    def eligible_derivation_record_count(self) -> int:
        return _authority_eligible_derivation_record_count(self.authority)

    @property
    def rejected_semantic_record_count(self) -> int:
        return len(self.authority.conflicts)


@dataclass(frozen=True)
class _ItemFingerprint:
    item_type: str
    item_id: str
    sha256: str


@dataclass(frozen=True)
class _RecordedEvidence:
    relationship_id: str
    source_id: str
    output_id: str
    supporting_records: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class _ParagraphEvidence:
    paragraph_id: str
    document_id: str
    paragraph_ref_json: bytes
    container_kind: str
    paragraph_text_sha256: str
    role: str


@dataclass(frozen=True)
class _SectionEvidence:
    section_id: str
    document_id: str
    section_ref_json: bytes
    label: str | None
    heading: str | None
    level: int
    label_basis: str | None
    role: str


@dataclass(frozen=True)
class _ObservationEvidence:
    observation_id: str
    document_id: str
    canonical_path: str
    filename: str
    position: int
    byte_length: int
    file_sha256: str
    inspection_coverage_json: bytes


@dataclass(frozen=True)
class _ConflictEvidence:
    conflict_id: str
    reason: str
    affected_document_ids: tuple[str, ...]
    record_sha256: str


@dataclass(frozen=True)
class _SourceEvidence:
    workspace_path: str
    workspace_identity: tuple[int, int]
    seed_path: str
    ordering_source: str
    cursor_offset: int
    page_size: int
    observations: tuple[_ObservationEvidence, ...]
    recorded_relationships: tuple[_RecordedEvidence, ...]
    paragraphs: tuple[_ParagraphEvidence, ...]
    sections: tuple[_SectionEvidence, ...]
    conflicts: tuple[_ConflictEvidence, ...]


@dataclass(frozen=True)
class _ProjectionFacts:
    item_fingerprints: tuple[_ItemFingerprint, ...]
    full_result_set_sha256: str
    page_items: tuple[dict[str, Any], ...]
    item_type_counts: tuple[tuple[str, int], ...]
    relationship_counts: tuple[tuple[str, int], ...]
    resolution_counts: tuple[tuple[str, int], ...]
    record_only_document_count: int
    eligible_item_count: int


@dataclass(frozen=True)
class _EnumeratedCandidate:
    filename: str
    identity: tuple[int, int, int, int]


@dataclass(frozen=True)
class _CapturedCandidate:
    filename: str
    path: str
    position: int
    payload: bytes


@dataclass(frozen=True)
class _CurrentDocument:
    captured: _CapturedCandidate
    snapshot: _Snapshot
    document_id: str
    observation_id: str
    inspection_coverage: dict[str, Any]


@dataclass(frozen=True)
class _ResolvedSeed:
    document: _CurrentDocument
    paragraph: _Paragraph
    paragraph_id: str
    paragraph_ref: dict[str, Any]


@dataclass(frozen=True)
class _ApplyClassification:
    kind: str
    record: dict[str, Any]
    record_sha256: str
    source_id: str | None = None
    output_id: str | None = None
    profile: str | None = None
    reason: str | None = None
    conflict_endpoint_ids: tuple[str, ...] = ()


def _digest(value: Any) -> str:
    return records._stable_digest(value)


_ROUND_MAP_ITEM_CANONICAL_NODES = 4_192


def _freeze_json(value: Any) -> bytes:
    """Keep one deeply immutable canonical copy of a bounded source fact."""
    try:
        return records._canonical_json_bytes(value)
    except Exception as exc:
        raise RoundMapError(
            "evidence_consistency_error", "source fact is not canonical JSON"
        ) from exc


def _thaw_json_object(value: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RoundMapError(
            "output_contract_error", "immutable source fact cannot be decoded"
        ) from exc
    if not isinstance(decoded, dict):
        raise RoundMapError(
            "output_contract_error", "immutable source fact is not an object"
        )
    return decoded


def _streaming_item_set_digest(items: Iterable[dict[str, Any]]) -> str:
    """Hash a legal map incrementally without relaxing the journal JSON ceiling."""
    digest = hashlib.sha256()
    digest.update(b'{"items":[')
    count = 0
    try:
        for item in items:
            if count >= ROUND_MAP_LIMITS["total_map_items"]:
                raise RoundMapError(
                    "resource_limit_exceeded", "complete map exceeds total item limit"
                )
            if count:
                digest.update(b",")
            records._validate_json_value(
                item,
                max_nodes=_ROUND_MAP_ITEM_CANONICAL_NODES,
            )
            digest.update(records._canonical_json_bytes(item))
            count += 1
        digest.update(b'],"schema_version":"round_map_item_set.v1"}')
    except RoundMapError:
        raise
    except Exception as exc:
        raise RoundMapError(
            "output_contract_error", "complete item set is not canonical JSON"
        ) from exc
    return digest.hexdigest()


def _canonical_equal(left: Any, right: Any) -> bool:
    """Compare JSON facts by canonical type and value, never Python coercion."""
    try:
        return records._canonical_json_bytes(left) == records._canonical_json_bytes(
            right
        )
    except Exception:
        return False


def _item_fingerprint(item: dict[str, Any]) -> _ItemFingerprint:
    return _ItemFingerprint(
        item_type=item["item_type"],
        item_id=item["id"],
        sha256=_digest(item),
    )


def _freeze_item_fingerprints(
    items: list[dict[str, Any]],
) -> tuple[_ItemFingerprint, ...]:
    if len(items) > ROUND_MAP_LIMITS["total_map_items"]:
        raise RoundMapError(
            "resource_limit_exceeded", "complete map exceeds total item limit"
        )
    try:
        return tuple(_item_fingerprint(item) for item in items)
    except Exception as exc:
        raise RoundMapError(
            "output_contract_error", "complete item fingerprints cannot be established"
        ) from exc


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None


def _document_id(sha256: str) -> str:
    return f"rm_doc_v1:{sha256}"


def _derived_id(prefix: str, identity: dict[str, Any]) -> str:
    return f"{prefix}:{_digest(identity)}"


def _path_text(value: object, *, code: str) -> str:
    try:
        resolved = resolve_user_path(value)
    except UserPathError as exc:
        raise RoundMapError(code, "path is invalid") from exc
    if not resolved:
        raise RoundMapError(code, "path must not be empty")
    return resolved


def _validate_paragraph_ref(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _PARAGRAPH_REF_KEYS:
        raise RoundMapError("invalid_reference", "paragraph_ref fields are invalid")
    index = value.get("paragraph_index")
    if (
        value.get("schema_version") != "paragraph_ref.v1"
        or value.get("ref_type") != "paragraph"
        or not _is_sha256(value.get("file_sha256"))
        or value.get("part_name") != _DOCUMENT_PART
        or isinstance(index, bool)
        or not isinstance(index, int)
        or index < 0
        or not _is_sha256(value.get("paragraph_text_sha256"))
        or value.get("reading_mode") != "accepted_current_v1"
        or value.get("container_policy") != "canonical_body_flow_v1"
    ):
        raise RoundMapError("invalid_reference", "paragraph_ref is not valid v1")
    return deepcopy(value)


def _validate_inputs(
    folder: object,
    seed: object,
    ordered_filenames: object,
    cursor: object,
    max_items: object,
) -> tuple[str, dict[str, Any], list[str] | None, tuple[int, str] | None, int]:
    folder_text = _path_text(folder, code="invalid_request")
    if not isinstance(seed, dict) or set(seed) != _SEED_KEYS:
        raise RoundMapError("invalid_request", "seed fields are invalid")
    if seed.get("schema_version") != "round_map_seed.v1":
        raise RoundMapError("invalid_request", "seed schema_version is invalid")
    seed_path = _path_text(seed.get("path"), code="invalid_request")
    paragraph_ref = _validate_paragraph_ref(seed.get("paragraph_ref"))

    normalized_order: list[str] | None
    if ordered_filenames is None:
        normalized_order = None
    elif not isinstance(ordered_filenames, list):
        raise RoundMapError("invalid_round_order", "ordered_filenames must be an array")
    else:
        if len(ordered_filenames) > ROUND_MAP_LIMITS["candidate_docx_files"]:
            raise RoundMapError(
                "invalid_round_order", "ordered_filenames exceeds the manifest limit"
            )
        normalized_order = list(ordered_filenames)
        for filename in normalized_order:
            if (
                not isinstance(filename, str)
                or not filename
                or filename in {".", ".."}
                or os.path.basename(filename) != filename
                or (os.path.altsep is not None and os.path.altsep in filename)
                or not filename.casefold().endswith(".docx")
            ):
                raise RoundMapError(
                    "invalid_round_order", "ordered filename is not a direct DOCX name"
                )

    parsed_cursor: tuple[int, str] | None
    if cursor is None:
        parsed_cursor = None
    elif not isinstance(cursor, str) or (match := _CURSOR_RE.fullmatch(cursor)) is None:
        raise RoundMapError("invalid_cursor", "cursor is not a valid rm1 cursor")
    else:
        parsed_cursor = (int(match.group(1)), match.group(2))

    if type(max_items) is not int or not 1 <= max_items <= MAX_ITEMS:
        raise RoundMapError(
            "invalid_request", "max_items must be an integer from 1 through 100"
        )
    return (
        folder_text,
        {
            "schema_version": "round_map_seed.v1",
            "path": seed_path,
            "paragraph_ref": paragraph_ref,
        },
        normalized_order,
        parsed_cursor,
        max_items,
    )


def _candidate_name(name: str) -> bool:
    return not name.startswith("~$") and name.casefold().endswith(".docx")


def _enforce_resource_boundary(observed: int, limit_key: str, detail: str) -> None:
    """Apply one inclusive frozen limit without constructing an over-limit value."""
    if observed > ROUND_MAP_LIMITS[limit_key]:
        raise RoundMapError("resource_limit_exceeded", detail)


def _enumerate_candidates(root_fd: int) -> dict[str, _EnumeratedCandidate]:
    candidates: dict[str, _EnumeratedCandidate] = {}
    try:
        with os.scandir(root_fd) as entries:
            for entry in entries:
                if not _candidate_name(entry.name):
                    continue
                _enforce_resource_boundary(
                    len(candidates) + 1,
                    "candidate_docx_files",
                    "workspace contains more than 500 candidate DOCX files",
                )
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise RoundMapError(
                        "workspace_unreadable", "candidate metadata cannot be read"
                    ) from exc
                candidates[entry.name] = _EnumeratedCandidate(
                    filename=entry.name,
                    identity=(info.st_dev, info.st_ino, info.st_mode, info.st_nlink),
                )
    except RoundMapError:
        raise
    except OSError as exc:
        raise RoundMapError(
            "workspace_unreadable", "workspace cannot be enumerated"
        ) from exc
    return candidates


def _validate_candidate_types(
    candidates: dict[str, _EnumeratedCandidate],
) -> None:
    for candidate in candidates.values():
        _device, _inode, mode, link_count = candidate.identity
        if not stat.S_ISREG(mode) or link_count != 1:
            raise RoundMapError(
                "unsafe_candidate", "candidate is not a one-link regular file"
            )


def _effective_order(
    candidates: dict[str, _EnumeratedCandidate],
    ordered_filenames: list[str] | None,
) -> tuple[list[str], str, dict[str, Any]]:
    if ordered_filenames is None:
        filenames = sorted(candidates, key=lambda value: (value.casefold(), value))
        return (
            filenames,
            "filename_lexicographic_v1",
            {
                "kind": "filename",
                "rule": "casefold_then_exact",
                "lineage_verified": False,
                "round_id_semantics": "position_only",
            },
        )
    if (
        len(ordered_filenames) != len(candidates)
        or len(ordered_filenames) != len(set(ordered_filenames))
        or set(ordered_filenames) != set(candidates)
    ):
        raise RoundMapError(
            "invalid_round_order",
            "ordered_filenames must name every candidate DOCX exactly once",
        )
    return (
        list(ordered_filenames),
        "explicit_filename_sequence_v1",
        {
            "kind": "caller_supplied_filename_sequence",
            "rule": "exact_sequence",
            "lineage_verified": False,
            "round_id_semantics": "position_only",
        },
    )


def _canonical_seed_path(seed_path: str) -> str:
    path = Path(seed_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        parent = path.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        return str(path.absolute())
    return str(parent / path.name)


def _read_candidate(
    root_fd: int,
    candidate: _EnumeratedCandidate,
) -> bytes:
    flags = os.O_RDONLY | records.O_NOFOLLOW | records.O_NONBLOCK
    try:
        fd = os.open(candidate.filename, flags, dir_fd=root_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENXIO, errno.ENODEV}:
            raise RoundMapError(
                "unsafe_candidate", "candidate cannot be opened as a regular file"
            ) from exc
        raise RoundMapError(
            "file_unreadable", "candidate bytes cannot be read"
        ) from exc
    try:
        before = os.fstat(fd)
        opened_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
        )
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RoundMapError(
                "unsafe_candidate", "opened candidate is not a one-link regular file"
            )
        if opened_identity != candidate.identity:
            raise RoundMapError("workspace_changed", "candidate identity changed")
        limit = ROUND_MAP_LIMITS["compressed_bytes_per_docx"]
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        _enforce_resource_boundary(
            len(payload),
            "compressed_bytes_per_docx",
            "candidate exceeds 50 MiB compressed limit",
        )
        after = os.fstat(fd)
        before_tuple = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_tuple = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_tuple != after_tuple or len(payload) != before.st_size:
            raise RoundMapError("workspace_changed", "candidate changed while read")
    except RoundMapError:
        raise
    except OSError as exc:
        raise RoundMapError(
            "file_unreadable", "candidate bytes cannot be read"
        ) from exc
    finally:
        os.close(fd)

    try:
        current = os.stat(candidate.filename, dir_fd=root_fd, follow_symlinks=False)
    except OSError as exc:
        raise RoundMapError(
            "workspace_changed", "candidate changed after read"
        ) from exc
    current_identity = (
        current.st_dev,
        current.st_ino,
        current.st_mode,
        current.st_nlink,
    )
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise RoundMapError("workspace_changed", "candidate changed after read")
    if current_identity != candidate.identity:
        raise RoundMapError(
            "workspace_changed", "candidate identity changed after read"
        )
    return payload


def _capture_workspace(
    folder: str,
    seed_path: str,
    ordered_filenames: list[str] | None,
) -> tuple[
    Path,
    tuple[int, int],
    list[_CapturedCandidate],
    _CapturedCandidate,
    str,
    dict[str, Any],
    str,
]:
    lexical = Path(folder)
    if not lexical.is_absolute():
        lexical = Path.cwd() / lexical
    lexical = Path(os.path.abspath(lexical))
    try:
        initial = lexical.lstat()
    except FileNotFoundError as exc:
        raise RoundMapError("workspace_missing", "workspace does not exist") from exc
    except OSError as exc:
        raise RoundMapError("workspace_unreadable", "workspace cannot be read") from exc
    if stat.S_ISLNK(initial.st_mode) or not stat.S_ISDIR(initial.st_mode):
        raise RoundMapError(
            "workspace_not_directory", "workspace is not a direct directory"
        )
    identity = (initial.st_dev, initial.st_ino)
    try:
        root_fd = os.open(
            lexical,
            os.O_RDONLY | records.O_DIRECTORY | records.O_NOFOLLOW,
        )
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
            raise RoundMapError(
                "workspace_changed", "workspace changed before open"
            ) from exc
        raise RoundMapError(
            "workspace_unreadable", "workspace cannot be opened"
        ) from exc
    try:
        opened = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != identity
        ):
            raise RoundMapError("workspace_changed", "workspace identity changed")
        canonical = records._filesystem_spelled_workspace(root_fd, lexical, identity)
        candidates = _enumerate_candidates(root_fd)
        _validate_candidate_types(candidates)
        filenames, ordering_source, order_basis = _effective_order(
            candidates, ordered_filenames
        )
        canonical_seed = _canonical_seed_path(seed_path)
        candidate_paths = {str(canonical / filename) for filename in filenames}
        if canonical_seed not in candidate_paths:
            raise RoundMapError(
                "seed_not_candidate", "seed path is not a direct candidate DOCX"
            )

        captured: list[_CapturedCandidate] = []
        total_bytes = 0
        for position, filename in enumerate(filenames):
            payload = _read_candidate(root_fd, candidates[filename])
            total_bytes += len(payload)
            _enforce_resource_boundary(
                total_bytes,
                "candidate_compressed_input_bytes",
                "candidate DOCX files exceed aggregate compressed-byte limit",
            )
            captured.append(
                _CapturedCandidate(
                    filename=filename,
                    path=str(canonical / filename),
                    position=position,
                    payload=payload,
                )
            )

        final_names: set[str] = set()
        try:
            with os.scandir(root_fd) as entries:
                for entry in entries:
                    if not _candidate_name(entry.name):
                        continue
                    if len(final_names) >= ROUND_MAP_LIMITS["candidate_docx_files"]:
                        raise RoundMapError(
                            "workspace_changed",
                            "candidate filename set grew beyond its captured bound",
                        )
                    final_names.add(entry.name)
        except RoundMapError:
            raise
        except OSError as exc:
            raise RoundMapError(
                "workspace_changed", "candidate filename set cannot be rechecked"
            ) from exc
        if final_names != set(candidates):
            raise RoundMapError("workspace_changed", "candidate filename set changed")
        try:
            final = lexical.lstat()
        except OSError as exc:
            raise RoundMapError("workspace_changed", "workspace path changed") from exc
        if (
            not stat.S_ISDIR(final.st_mode)
            or (final.st_dev, final.st_ino) != identity
            or records._filesystem_spelled_workspace(root_fd, lexical, identity)
            != canonical
        ):
            raise RoundMapError(
                "workspace_changed", "workspace identity or spelling changed"
            )
        captured_seed = next(
            candidate for candidate in captured if candidate.path == canonical_seed
        )
    finally:
        os.close(root_fd)

    manifest = {
        "schema_version": "round_map_filename_manifest.v1",
        "ordering_source": ordering_source,
        "filenames": filenames,
    }
    manifest_sha256 = _digest(manifest)
    return (
        canonical,
        identity,
        captured,
        captured_seed,
        ordering_source,
        order_basis,
        manifest_sha256,
    )


def _inspection_coverage(snapshot: _Snapshot) -> dict[str, Any]:
    return {
        "schema_version": "round_map_inspection_coverage.v1",
        "scan_complete": True,
        "indexed_paragraph_count": len(snapshot.paragraphs),
        "nonempty_indexed_paragraph_count": sum(
            bool(paragraph.text) for paragraph in snapshot.paragraphs
        ),
        "included_parts": [_DOCUMENT_PART],
        "excluded_parts": list(snapshot.excluded_parts),
        "included_containers": ["body", "table_cell"],
        "container_coverage": deepcopy(snapshot.container_coverage),
    }


def _parse_candidates(
    captured: list[_CapturedCandidate],
) -> list[_CurrentDocument]:
    expanded_budget = ExpandedOutputBudget(
        allowed_bytes=ROUND_MAP_LIMITS["candidate_expanded_bytes"],
        limit="round_map_candidate_expanded_bytes",
    )
    current: list[_CurrentDocument] = []
    payload_by_digest: dict[str, bytes] = {}
    for candidate in captured:
        try:
            snapshot = _load_snapshot_from_payload(
                candidate.payload,
                path=candidate.path,
                expanded_budget=expanded_budget,
                missing_document_part_code="missing_document_part",
                invalid_document_structure_code="invalid_docx",
                invalid_ooxml_value_code="invalid_docx",
            )
        except ResourceLimitError as exc:
            raise RoundMapError(
                "resource_limit_exceeded", "candidate exceeds a processing limit"
            ) from exc
        except ArchiveValidationError as exc:
            raise RoundMapError(
                exc.code, "candidate archive cannot be inspected"
            ) from exc
        except InspectError as exc:
            code = (
                "missing_document_part"
                if exc.code == "missing_document_part"
                else "invalid_docx"
            )
            raise RoundMapError(code, "candidate DOCX cannot be inspected") from exc
        except DocxError as exc:
            raise RoundMapError(
                "invalid_docx", "candidate DOCX cannot be inspected"
            ) from exc
        _enforce_resource_boundary(
            len(snapshot.paragraphs),
            "indexed_paragraphs_per_docx",
            "candidate exceeds the indexed paragraph limit",
        )
        _enforce_resource_boundary(
            sum(len(paragraph.text) for paragraph in snapshot.paragraphs),
            "accepted_current_chars_per_docx",
            "candidate exceeds the accepted-current text limit",
        )
        previous_payload = payload_by_digest.setdefault(
            snapshot.file_sha256, candidate.payload
        )
        if previous_payload != candidate.payload:
            raise RoundMapError(
                "evidence_consistency_error", "equal file hashes have unequal bytes"
            )
        document_id = _document_id(snapshot.file_sha256)
        observation_id = _derived_id(
            "rm_obs_v1",
            {
                "schema_version": "document_observation_identity.v1",
                "document_id": document_id,
                "canonical_path": candidate.path,
            },
        )
        current.append(
            _CurrentDocument(
                captured=candidate,
                snapshot=snapshot,
                document_id=document_id,
                observation_id=observation_id,
                inspection_coverage=_inspection_coverage(snapshot),
            )
        )
    return current


def _record_endpoints(record: dict[str, Any]) -> tuple[str, ...]:
    result = record.get("result")
    provenance = record.get("provenance")
    result = result if isinstance(result, dict) else {}
    provenance = provenance if isinstance(provenance, dict) else {}
    values = (
        provenance.get("source_sha256"),
        result.get("source_sha256"),
        provenance.get("output_sha256"),
        result.get("output_sha256"),
    )
    return tuple(sorted({_document_id(value) for value in values if _is_sha256(value)}))


def _classify_apply_record(record: dict[str, Any]) -> _ApplyClassification:
    record_sha256 = _digest(record)
    result = record.get("result")
    provenance = record.get("provenance")
    result = result if isinstance(result, dict) else {}
    provenance = provenance if isinstance(provenance, dict) else {}
    endpoints = _record_endpoints(record)

    def conflict(reason: str) -> _ApplyClassification:
        return _ApplyClassification(
            kind="conflict",
            record=record,
            record_sha256=record_sha256,
            reason=reason,
            conflict_endpoint_ids=endpoints,
        )

    status = result.get("status")
    if status == "error":
        return _ApplyClassification(
            kind="excluded", record=record, record_sha256=record_sha256
        )
    if status != "ok":
        return conflict("result_status_invalid")

    source = provenance.get("source_sha256")
    if source is None:
        return conflict("missing_source_sha256")
    if not _is_sha256(source):
        return conflict("invalid_source_sha256")
    result_output = result.get("output_sha256")
    provenance_output = provenance.get("output_sha256")
    if result_output is None or provenance_output is None:
        return conflict("missing_output_sha256")
    if not _is_sha256(result_output) or not _is_sha256(provenance_output):
        return conflict("invalid_output_sha256")
    if result_output != provenance_output:
        return conflict("result_output_sha256_mismatch")

    result_round_trip = result.get("round_trip_check")
    provenance_round_trip = provenance.get("round_trip_check")
    if not isinstance(result_round_trip, dict) or not isinstance(
        provenance_round_trip, dict
    ):
        return conflict("round_trip_missing")
    if not _canonical_equal(result_round_trip, provenance_round_trip):
        return conflict("round_trip_fact_mismatch")
    if result_round_trip.get("status") != "passed":
        return conflict("round_trip_failed")
    comparison = result_round_trip.get("comparison")
    if comparison not in {_CURRENT_COMPARISON, _LEGACY_COMPARISON}:
        return conflict("round_trip_comparison_unsupported")
    if result_round_trip.get("collateral_changes") != []:
        return conflict("round_trip_fact_mismatch")

    result_source_present = "source_sha256" in result
    strengthened_present = [
        key in owner for owner in (result, provenance) for key in _STRENGTHENED_FIELDS
    ]
    if comparison == _LEGACY_COMPARISON:
        if result_source_present or any(strengthened_present):
            return conflict("unsupported_legacy_profile")
        profile = "frozen_legacy_v1"
    else:
        if not result_source_present:
            return conflict("unsupported_legacy_profile")
        result_source = result.get("source_sha256")
        if not _is_sha256(result_source):
            return conflict("invalid_source_sha256")
        if result_source != source:
            return conflict("result_source_sha256_mismatch")
        present_count = sum(strengthened_present)
        if present_count == 0:
            profile = "published_v0.1.2_preflightless"
        elif present_count == 6:
            if any(
                not _canonical_equal(result.get(key), provenance.get(key))
                for key in _STRENGTHENED_FIELDS
            ):
                return conflict("strengthened_fact_mismatch")
            if result.get("preflight_binding_status") != "verified":
                return conflict("preflight_binding_status_invalid")
            candidate_sha = result.get("preflight_candidate_sha256")
            if not _is_sha256(candidate_sha) or candidate_sha != provenance_output:
                return conflict("preflight_candidate_sha256_mismatch")
            if result.get("candidate_output_sha256_match") is not True:
                return conflict("candidate_output_sha256_match_invalid")
            profile = "current_v0.3"
        else:
            return conflict("unsupported_legacy_profile")

    return _ApplyClassification(
        kind="valid",
        record=record,
        record_sha256=record_sha256,
        source_id=_document_id(source),
        output_id=_document_id(provenance_output),
        profile=profile,
    )


def _journal_facts(
    workspace: Path,
    workspace_identity: tuple[int, int],
    current_document_ids: set[str],
) -> tuple[
    set[str],
    list[_ApplyClassification],
    list[tuple[_ApplyClassification, tuple[str, ...]]],
]:
    try:
        raw_records = records.read_round_map_apply_records(
            workspace,
            expected_workspace_identity=workspace_identity,
        )
    except records.DecisionRecordError as exc:
        raise RoundMapError(exc.code, "journal snapshot refused") from exc
    classifications = [_classify_apply_record(record) for record in raw_records]
    valid = [item for item in classifications if item.kind == "valid"]
    adjacency: dict[str, set[str]] = defaultdict(set)
    for item in valid:
        assert item.source_id is not None and item.output_id is not None
        adjacency[item.source_id].add(item.output_id)
        adjacency[item.output_id].add(item.source_id)
    included = set(current_document_ids)
    pending = deque(sorted(included))
    while pending:
        node = pending.popleft()
        for neighbor in sorted(adjacency.get(node, ())):
            if neighbor not in included:
                if len(included) >= ROUND_MAP_LIMITS["document_nodes"]:
                    raise RoundMapError(
                        "resource_limit_exceeded",
                        "connected document node count exceeds its fixed limit",
                    )
                included.add(neighbor)
                pending.append(neighbor)

    relevant_valid = [
        item
        for item in valid
        if item.source_id in included or item.output_id in included
    ]
    relevant_conflicts: list[tuple[_ApplyClassification, tuple[str, ...]]] = []
    for item in classifications:
        if item.kind != "conflict":
            continue
        affected = tuple(sorted(set(item.conflict_endpoint_ids) & included))
        if affected:
            relevant_conflicts.append((item, affected))
    return included, relevant_valid, relevant_conflicts


def _paragraph_identity(
    snapshot: _Snapshot, paragraph: _Paragraph
) -> tuple[str, dict[str, Any]]:
    reference = _paragraph_ref(snapshot, paragraph)
    return _derived_id("rm_par_v1", reference), reference


def _section_identity(
    snapshot: _Snapshot, section: _Section
) -> tuple[str, dict[str, Any]]:
    reference = _section_ref(snapshot, section)
    return _derived_id("rm_sec_v1", reference), reference


def _item_id(item: dict[str, Any]) -> str:
    return item["id"]


def _support_sort_key(item: _ApplyClassification) -> int:
    return int(item.record["record_id"].removeprefix("dr_"))


def _support_records(
    support: list[_ApplyClassification],
) -> list[dict[str, str]]:
    ordered = sorted(support, key=_support_sort_key)
    assert all(item.profile is not None for item in ordered)
    return [
        {
            "record_id": item.record["record_id"],
            "record_sha256": item.record_sha256,
            "profile": item.profile,
        }
        for item in ordered
    ]


def _equality_relationship(
    seed_id: str,
    candidate_id: str,
    text_sha256: str,
) -> dict[str, Any]:
    from_id, to_id = sorted((seed_id, candidate_id))
    basis = {
        "schema_version": "exact_content_equality_basis.v1",
        "reading_mode": "accepted_current_v1",
        "container_policy": "canonical_body_flow_v1",
        "part_name": _DOCUMENT_PART,
        "comparison": "complete_unicode_scalar_sequence_v1",
        "full_text_compared": True,
        "paragraph_text_sha256": text_sha256,
    }
    identity = {
        "schema_version": "relationship_identity.v1",
        "relationship_type": "exact_content_equality",
        "from_id": from_id,
        "to_id": to_id,
        "direction": "symmetric",
        "basis_identity": basis,
    }
    return {
        "schema_version": "round_map_item.v1",
        "item_type": "relationship",
        "id": _derived_id("rm_rel_v1", identity),
        "relationship_type": "exact_content_equality",
        "from_id": from_id,
        "to_id": to_id,
        "direction": "symmetric",
        "basis": basis,
        "derivation_recorded": False,
        "lineage_verified": False,
        "chronology_verified": False,
    }


def _navigation_relationship(
    seed_id: str,
    candidate_id: str,
    signals: list[dict[str, str]],
) -> dict[str, Any]:
    basis = {
        "schema_version": "navigation_candidate_basis.v1",
        "signals": signals,
        "evidence_class": "navigation_only",
    }
    identity = {
        "schema_version": "relationship_identity.v1",
        "relationship_type": "navigation_candidate",
        "from_id": seed_id,
        "to_id": candidate_id,
        "direction": "directed",
        "basis_identity": basis,
    }
    return {
        "schema_version": "round_map_item.v1",
        "item_type": "relationship",
        "id": _derived_id("rm_rel_v1", identity),
        "relationship_type": "navigation_candidate",
        "from_id": seed_id,
        "to_id": candidate_id,
        "direction": "directed",
        "basis": basis,
        "derivation_recorded": False,
        "lineage_verified": False,
        "chronology_verified": False,
    }


def _cycle_members(
    document_ids: set[str],
    edge_pairs: set[tuple[str, str]],
) -> set[str]:
    adjacency: dict[str, set[str]] = {node: set() for node in document_ids}
    reverse: dict[str, set[str]] = {node: set() for node in document_ids}
    for source, output in edge_pairs:
        if source != output:
            adjacency[source].add(output)
            reverse[output].add(source)
    visited: set[str] = set()
    finish: list[str] = []
    for start in sorted(document_ids):
        if start in visited:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                finish.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            stack.append((node, True))
            for neighbor in sorted(adjacency[node], reverse=True):
                if neighbor not in visited:
                    stack.append((neighbor, False))
    assigned: set[str] = set()
    cycle_members: set[str] = set()
    for start in reversed(finish):
        if start in assigned:
            continue
        component: set[str] = set()
        stack = [start]
        assigned.add(start)
        while stack:
            node = stack.pop()
            component.add(node)
            for neighbor in reverse[node]:
                if neighbor not in assigned:
                    assigned.add(neighbor)
                    stack.append(neighbor)
        if len(component) > 1:
            cycle_members.update(component)
    return cycle_members


def _resolve_seed_evidence(
    current: list[_CurrentDocument],
    captured_seed: _CapturedCandidate,
    seed_ref: dict[str, Any],
) -> _ResolvedSeed:
    seed_current = next(
        document for document in current if document.captured is captured_seed
    )
    if seed_ref["file_sha256"] != seed_current.snapshot.file_sha256:
        raise RoundMapError(
            "file_sha256_mismatch", "seed reference was produced from different bytes"
        )
    try:
        seed_paragraph = _resolve_paragraph(seed_current.snapshot, seed_ref)
    except InspectError as exc:
        code = (
            "file_sha256_mismatch"
            if exc.code == "file_sha256_mismatch"
            else "reference_mismatch"
        )
        raise RoundMapError(
            code, "seed paragraph reference no longer resolves"
        ) from exc
    for document in current:
        for paragraph in document.snapshot.paragraphs:
            if (
                paragraph.text_sha256 == seed_paragraph.text_sha256
                and paragraph.text != seed_paragraph.text
            ):
                raise RoundMapError(
                    "evidence_consistency_error",
                    "equal paragraph hashes have unequal complete text",
                )
    seed_paragraph_id, resolved_seed_ref = _paragraph_identity(
        seed_current.snapshot, seed_paragraph
    )
    return _ResolvedSeed(
        document=seed_current,
        paragraph=seed_paragraph,
        paragraph_id=seed_paragraph_id,
        paragraph_ref=resolved_seed_ref,
    )


def _relationship_id(
    relationship_type: str,
    from_id: str,
    to_id: str,
    direction: str,
    basis_identity: dict[str, Any],
) -> str:
    return _derived_id(
        "rm_rel_v1",
        {
            "schema_version": "relationship_identity.v1",
            "relationship_type": relationship_type,
            "from_id": from_id,
            "to_id": to_id,
            "direction": direction,
            "basis_identity": basis_identity,
        },
    )


def _derive_source_evidence(
    *,
    workspace: Path,
    workspace_identity: tuple[int, int],
    seed_path: str,
    ordering_source: str,
    cursor_offset: int,
    page_size: int,
    current: list[_CurrentDocument],
    resolved_seed: _ResolvedSeed,
    included_document_ids: set[str],
    derivation_records: list[_ApplyClassification],
    relevant_conflicts: list[tuple[_ApplyClassification, tuple[str, ...]]],
) -> _SourceEvidence:
    """Freeze the sole bounded authority before any live item is assembled."""
    observations = tuple(
        _ObservationEvidence(
            observation_id=document.observation_id,
            document_id=document.document_id,
            canonical_path=document.captured.path,
            filename=document.captured.filename,
            position=document.captured.position,
            byte_length=len(document.captured.payload),
            file_sha256=document.snapshot.file_sha256,
            inspection_coverage_json=_freeze_json(document.inspection_coverage),
        )
        for document in sorted(current, key=lambda item: item.captured.position)
    )

    support_by_edge: dict[tuple[str, str], list[_ApplyClassification]] = defaultdict(
        list
    )
    for classification in derivation_records:
        assert classification.source_id is not None
        assert classification.output_id is not None
        edge = (classification.source_id, classification.output_id)
        if (
            edge not in support_by_edge
            and len(support_by_edge)
            >= ROUND_MAP_LIMITS["recorded_derivation_relationships"]
        ):
            raise RoundMapError(
                "resource_limit_exceeded",
                "recorded derivation relationship count exceeds its fixed limit",
            )
        support_by_edge[edge].append(classification)
    recorded: list[_RecordedEvidence] = []
    for (source_id, output_id), support in support_by_edge.items():
        support_facts = _support_records(support)
        recorded.append(
            _RecordedEvidence(
                relationship_id=_relationship_id(
                    "recorded_derivation",
                    source_id,
                    output_id,
                    "directed",
                    _RECORDED_BASIS_IDENTITY,
                ),
                source_id=source_id,
                output_id=output_id,
                supporting_records=tuple(
                    (
                        fact["record_id"],
                        fact["record_sha256"],
                        fact["profile"],
                    )
                    for fact in support_facts
                ),
            )
        )

    seed_document = resolved_seed.document
    seed_paragraph = resolved_seed.paragraph
    seed_fact = _ParagraphEvidence(
        paragraph_id=resolved_seed.paragraph_id,
        document_id=seed_document.document_id,
        paragraph_ref_json=_freeze_json(resolved_seed.paragraph_ref),
        container_kind=seed_paragraph.container_kind,
        paragraph_text_sha256=resolved_seed.paragraph_ref["paragraph_text_sha256"],
        role="seed",
    )
    paragraphs: dict[str, _ParagraphEvidence] = {seed_fact.paragraph_id: seed_fact}
    for document in current:
        for paragraph in document.snapshot.paragraphs:
            if paragraph.text_sha256 != seed_paragraph.text_sha256:
                continue
            if paragraph.text != seed_paragraph.text:
                raise RoundMapError(
                    "evidence_consistency_error",
                    "equal paragraph hashes have unequal complete text",
                )
            paragraph_id, reference = _paragraph_identity(document.snapshot, paragraph)
            if paragraph_id == seed_fact.paragraph_id:
                continue
            candidate = _ParagraphEvidence(
                paragraph_id=paragraph_id,
                document_id=document.document_id,
                paragraph_ref_json=_freeze_json(reference),
                container_kind=paragraph.container_kind,
                paragraph_text_sha256=reference["paragraph_text_sha256"],
                role="exact_candidate",
            )
            previous = paragraphs.get(paragraph_id)
            if previous is not None and previous != candidate:
                raise RoundMapError(
                    "evidence_consistency_error",
                    "paragraph identity maps to inconsistent source facts",
                )
            if (
                previous is None
                and len(paragraphs) >= ROUND_MAP_LIMITS["paragraph_nodes"]
            ):
                raise RoundMapError(
                    "resource_limit_exceeded",
                    "paragraph node count exceeds its fixed limit",
                )
            paragraphs.setdefault(paragraph_id, candidate)

    sections: dict[str, _SectionEvidence] = {}
    seed_section = seed_document.snapshot.section_by_paragraph.get(
        seed_paragraph.paragraph_index
    )
    if seed_section is not None:
        seed_section_id, seed_section_ref = _section_identity(
            seed_document.snapshot, seed_section
        )
        sections[seed_section_id] = _SectionEvidence(
            section_id=seed_section_id,
            document_id=seed_document.document_id,
            section_ref_json=_freeze_json(seed_section_ref),
            label=seed_section.label,
            heading=seed_section.title,
            level=seed_section.level,
            label_basis=seed_section.label_basis,
            role="seed_navigation",
        )
        for document in current:
            for section in document.snapshot.sections:
                section_id, section_ref = _section_identity(document.snapshot, section)
                if section_id == seed_section_id:
                    continue
                if not (
                    (
                        seed_section.label is not None
                        and section.label == seed_section.label
                    )
                    or (
                        seed_section.title is not None
                        and section.title == seed_section.title
                    )
                ):
                    continue
                candidate = _SectionEvidence(
                    section_id=section_id,
                    document_id=document.document_id,
                    section_ref_json=_freeze_json(section_ref),
                    label=section.label,
                    heading=section.title,
                    level=section.level,
                    label_basis=section.label_basis,
                    role="candidate_navigation",
                )
                previous = sections.get(section_id)
                if previous is not None and previous != candidate:
                    raise RoundMapError(
                        "evidence_consistency_error",
                        "section identity maps to inconsistent source facts",
                    )
                if (
                    previous is None
                    and len(sections) >= ROUND_MAP_LIMITS["section_nodes"]
                ):
                    raise RoundMapError(
                        "resource_limit_exceeded",
                        "section node count exceeds its fixed limit",
                    )
                sections.setdefault(section_id, candidate)

    conflicts: list[_ConflictEvidence] = []
    for classification, affected in relevant_conflicts:
        assert classification.reason is not None
        identity = {
            "schema_version": "conflict_identity.v1",
            "conflict_type": "inconsistent_apply_record",
            "affected_document_ids": list(affected),
            "record_sha256": classification.record_sha256,
        }
        if len(conflicts) >= ROUND_MAP_LIMITS["conflict_items"]:
            raise RoundMapError(
                "resource_limit_exceeded", "conflict item count exceeds its fixed limit"
            )
        conflicts.append(
            _ConflictEvidence(
                conflict_id=_derived_id("rm_conflict_v1", identity),
                reason=classification.reason,
                affected_document_ids=affected,
                record_sha256=classification.record_sha256,
            )
        )

    authority = _SourceEvidence(
        workspace_path=str(workspace),
        workspace_identity=workspace_identity,
        seed_path=seed_path,
        ordering_source=ordering_source,
        cursor_offset=cursor_offset,
        page_size=page_size,
        observations=observations,
        recorded_relationships=tuple(
            sorted(recorded, key=lambda fact: fact.relationship_id)
        ),
        paragraphs=tuple(paragraphs[item_id] for item_id in sorted(paragraphs)),
        sections=tuple(sections[item_id] for item_id in sorted(sections)),
        conflicts=tuple(sorted(conflicts, key=lambda fact: fact.conflict_id)),
    )
    if _authority_document_ids(authority) != set(included_document_ids):
        raise RoundMapError(
            "evidence_consistency_error",
            "journal component differs from immutable evidence authority",
        )
    _validate_authority_limits(authority)
    return authority


def _authority_seed_paragraph(authority: _SourceEvidence) -> _ParagraphEvidence:
    seeds = [fact for fact in authority.paragraphs if fact.role == "seed"]
    if len(seeds) != 1:
        _output_contract_error("immutable authority has invalid seed cardinality")
    return seeds[0]


def _authority_seed_ref(authority: _SourceEvidence) -> dict[str, Any]:
    return _thaw_json_object(_authority_seed_paragraph(authority).paragraph_ref_json)


def _authority_filenames(authority: _SourceEvidence) -> tuple[str, ...]:
    ordered = sorted(authority.observations, key=lambda fact: fact.position)
    return tuple(fact.filename for fact in ordered)


def _authority_document_ids(authority: _SourceEvidence) -> set[str]:
    document_ids = {fact.document_id for fact in authority.observations}
    for fact in authority.recorded_relationships:
        document_ids.add(fact.source_id)
        document_ids.add(fact.output_id)
    return document_ids


def _authority_filename_manifest_sha256(authority: _SourceEvidence) -> str:
    return _digest(
        {
            "schema_version": "round_map_filename_manifest.v1",
            "ordering_source": authority.ordering_source,
            "filenames": list(_authority_filenames(authority)),
        }
    )


def _authority_filesystem_snapshot_sha256(authority: _SourceEvidence) -> str:
    observations = [
        {
            "observation_id": fact.observation_id,
            "canonical_path": fact.canonical_path,
            "filename": fact.filename,
            "position": fact.position,
            "byte_length": fact.byte_length,
            "file_sha256": fact.file_sha256,
            "inspection_coverage_sha256": _digest(
                _thaw_json_object(fact.inspection_coverage_json)
            ),
        }
        for fact in sorted(authority.observations, key=lambda item: item.position)
    ]
    return _digest(
        {
            "schema_version": "round_map_filesystem_snapshot.v1",
            "filename_manifest_sha256": _authority_filename_manifest_sha256(authority),
            "observations": observations,
        }
    )


def _authority_relevant_record_sha256s(authority: _SourceEvidence) -> list[str]:
    record_sha256s = [
        record_sha256
        for fact in authority.recorded_relationships
        for _record_id, record_sha256, _profile in fact.supporting_records
    ]
    record_sha256s.extend(fact.record_sha256 for fact in authority.conflicts)
    return sorted(record_sha256s)


def _authority_journal_snapshot_sha256(authority: _SourceEvidence) -> str:
    return _digest(
        {
            "schema_version": "round_map_relevant_journal_snapshot.v1",
            "record_sha256s": _authority_relevant_record_sha256s(authority),
        }
    )


def _authority_eligible_derivation_record_count(authority: _SourceEvidence) -> int:
    return sum(
        len(fact.supporting_records) for fact in authority.recorded_relationships
    )


def _authority_relevant_apply_record_count(authority: _SourceEvidence) -> int:
    return _authority_eligible_derivation_record_count(authority) + len(
        authority.conflicts
    )


def _validate_authority_limits(authority: _SourceEvidence) -> None:
    document_count = len(_authority_document_ids(authority))
    exact_count = sum(fact.role == "exact_candidate" for fact in authority.paragraphs)
    navigation_count = sum(
        fact.role == "candidate_navigation" for fact in authority.sections
    )
    counts = {
        "candidate_docx_files": len(authority.observations),
        "document_nodes": document_count,
        "document_observations": len(authority.observations),
        "paragraph_nodes": len(authority.paragraphs),
        "section_nodes": len(authority.sections),
        "recorded_derivation_relationships": len(authority.recorded_relationships),
        "exact_equality_relationships": exact_count,
        "navigation_relationships": navigation_count,
        "resolution_items": document_count,
        "conflict_items": len(authority.conflicts),
        "journal_apply_records": _authority_relevant_apply_record_count(authority),
    }
    for limit_key, observed in counts.items():
        _enforce_resource_boundary(
            observed, limit_key, f"{limit_key} exceeds its fixed limit"
        )
    total = (
        document_count
        + len(authority.observations)
        + len(authority.paragraphs)
        + len(authority.sections)
        + len(authority.recorded_relationships)
        + exact_count
        + navigation_count
        + document_count
        + len(authority.conflicts)
    )
    _enforce_resource_boundary(
        total, "total_map_items", "complete map exceeds total item limit"
    )


def _recorded_relationship_from_evidence(fact: _RecordedEvidence) -> dict[str, Any]:
    records_list = [
        {
            "record_id": record_id,
            "record_sha256": record_sha256,
            "profile": profile,
        }
        for record_id, record_sha256, profile in fact.supporting_records
    ]
    profile_counts = {
        "current_count": sum(
            item["profile"] == "current_v0.3" for item in records_list
        ),
        "published_v0_1_2_count": sum(
            item["profile"] == "published_v0.1.2_preflightless" for item in records_list
        ),
        "frozen_legacy_count": sum(
            item["profile"] == "frozen_legacy_v1" for item in records_list
        ),
    }
    nonzero = sum(bool(value) for value in profile_counts.values())
    support_profile = (
        "mixed"
        if nonzero > 1
        else "current_only"
        if profile_counts["current_count"]
        else "published_v0_1_2_only"
        if profile_counts["published_v0_1_2_count"]
        else "frozen_legacy_only"
    )
    basis = {
        "schema_version": "recorded_derivation_basis.v1",
        "record_schema_version": "decision_record.v1",
        "tool_name": "apply_edits",
        "record_type": "decision.v1",
        "assurance": "best_effort_local_non_tamper_evident",
        "derivation_scope": "document_bytes_only",
        "support_profile": support_profile,
        "supporting_records": {
            "count": len(records_list),
            **profile_counts,
            "sha256": _digest(
                {
                    "schema_version": "recorded_derivation_support.v1",
                    "records": records_list,
                }
            ),
            "sample": records_list[: ROUND_MAP_LIMITS["sample_items"]],
            "truncated": len(records_list) > ROUND_MAP_LIMITS["sample_items"],
        },
    }
    return {
        "schema_version": "round_map_item.v1",
        "item_type": "relationship",
        "id": fact.relationship_id,
        "relationship_type": "recorded_derivation",
        "from_id": fact.source_id,
        "to_id": fact.output_id,
        "direction": "directed",
        "basis": basis,
        "derivation_recorded": True,
        "lineage_verified": False,
        "chronology_verified": False,
    }


def _authority_indexes(authority: _SourceEvidence) -> dict[str, Any]:
    document_ids = _authority_document_ids(authority)
    observations_by_document: dict[str, list[_ObservationEvidence]] = defaultdict(list)
    for fact in authority.observations:
        observations_by_document[fact.document_id].append(fact)
    edge_pairs = {
        (fact.source_id, fact.output_id) for fact in authority.recorded_relationships
    }
    incoming: dict[str, set[str]] = defaultdict(set)
    outgoing: dict[str, set[str]] = defaultdict(set)
    for source_id, output_id in edge_pairs:
        incoming[output_id].add(source_id)
        outgoing[source_id].add(output_id)
    exact_by_document: dict[str, set[str]] = defaultdict(set)
    for fact in authority.paragraphs:
        if fact.role == "exact_candidate":
            exact_by_document[fact.document_id].add(fact.paragraph_id)
    navigation_by_document: dict[str, set[str]] = defaultdict(set)
    for fact in authority.sections:
        if fact.role == "candidate_navigation":
            navigation_by_document[fact.document_id].add(fact.section_id)
    conflicts_by_document: dict[str, int] = defaultdict(int)
    for fact in authority.conflicts:
        for document_id in fact.affected_document_ids:
            conflicts_by_document[document_id] += 1
    return {
        "document_ids": document_ids,
        "observations_by_document": observations_by_document,
        "edge_pairs": edge_pairs,
        "incoming": incoming,
        "outgoing": outgoing,
        "exact_by_document": exact_by_document,
        "navigation_by_document": navigation_by_document,
        "conflicts_by_document": conflicts_by_document,
        "cycle_members": _cycle_members(document_ids, edge_pairs),
    }


def _project_resolution(
    authority: _SourceEvidence,
    document_id: str,
    indexes: dict[str, Any],
) -> dict[str, Any]:
    observations = indexes["observations_by_document"].get(document_id, [])
    exact_ids = indexes["exact_by_document"].get(document_id, set())
    navigation_ids = indexes["navigation_by_document"].get(document_id, set())
    conflict_count = indexes["conflicts_by_document"][document_id]
    pruned = any(
        not _thaw_json_object(fact.inspection_coverage_json)["container_coverage"].get(
            "coverage_complete", False
        )
        or _thaw_json_object(fact.inspection_coverage_json)["container_coverage"].get(
            "excluded_subtree_count", 0
        )
        > 0
        for fact in observations
    )
    if conflict_count:
        state, reason = "ambiguous", "recorded_fact_conflict"
    elif len(exact_ids) > 1:
        state, reason = "ambiguous", "multiple_exact_candidates"
    elif len(exact_ids) == 1:
        state, reason = "exact_unique", "one_exact_candidate"
    elif not observations:
        state, reason = "unresolved", "record_only_document"
    elif pruned:
        state, reason = "unresolved", "declared_scope_incomplete"
    elif navigation_ids:
        state, reason = "unresolved", "navigation_only"
    else:
        state, reason = "unresolved", "no_match_in_declared_scope"
    seed_paragraph_id = _authority_seed_paragraph(authority).paragraph_id
    candidate_ids = sorted(exact_ids | navigation_ids)
    return {
        "schema_version": "round_map_item.v1",
        "item_type": "resolution",
        "id": _derived_id(
            "rm_resolution_v1",
            {
                "schema_version": "resolution_identity.v1",
                "seed_paragraph_id": seed_paragraph_id,
                "document_id": document_id,
            },
        ),
        "seed_paragraph_id": seed_paragraph_id,
        "document_id": document_id,
        "state": state,
        "reason": reason,
        "exact_candidate_count": len(exact_ids),
        "navigation_candidate_count": len(navigation_ids),
        "conflict_count": conflict_count,
        "candidate_ids": {
            "count": len(candidate_ids),
            "sha256": _digest(candidate_ids),
            "sample": candidate_ids[: ROUND_MAP_LIMITS["sample_items"]],
            "truncated": len(candidate_ids) > ROUND_MAP_LIMITS["sample_items"],
        },
    }


def _iter_projected_items(authority: _SourceEvidence) -> Iterator[dict[str, Any]]:
    """Project source evidence in frozen type-rank/ASCII-id order."""
    indexes = _authority_indexes(authority)
    incoming = indexes["incoming"]
    outgoing = indexes["outgoing"]
    edge_pairs = indexes["edge_pairs"]
    cycle_members = indexes["cycle_members"]
    observations_by_document = indexes["observations_by_document"]

    for document_id in sorted(indexes["document_ids"]):
        observations = sorted(
            observations_by_document.get(document_id, []),
            key=lambda fact: fact.position,
        )
        is_endpoint = bool(incoming[document_id] or outgoing[document_id])
        state = (
            "record_only"
            if not observations
            else "current_and_recorded"
            if is_endpoint
            else "current"
        )
        yield {
            "schema_version": "round_map_item.v1",
            "item_type": "document_node",
            "id": document_id,
            "file_sha256": (
                observations[0].file_sha256
                if observations
                else document_id.removeprefix("rm_doc_v1:")
            ),
            "observation_state": state,
            "observation_count": len(observations),
            "inspection_coverage": (
                _thaw_json_object(observations[0].inspection_coverage_json)
                if observations
                else None
            ),
            "incoming_recorded_derivation_count": len(incoming[document_id]),
            "outgoing_recorded_derivation_count": len(outgoing[document_id]),
            "topology_flags": {
                "multiple_parents": len(incoming[document_id]) > 1,
                "cycle_member": document_id in cycle_members,
                "self_loop": (document_id, document_id) in edge_pairs,
            },
        }

    for fact in sorted(authority.observations, key=lambda item: item.observation_id):
        yield {
            "schema_version": "round_map_item.v1",
            "item_type": "document_observation",
            "id": fact.observation_id,
            "document_id": fact.document_id,
            "path": fact.canonical_path,
            "filename": fact.filename,
            "position": fact.position,
            "round_id": f"round-{fact.position + 1:03d}",
            "position_basis": authority.ordering_source,
        }

    for fact in authority.paragraphs:
        yield {
            "schema_version": "round_map_item.v1",
            "item_type": "paragraph_node",
            "id": fact.paragraph_id,
            "document_id": fact.document_id,
            "paragraph_ref": _thaw_json_object(fact.paragraph_ref_json),
            "container_kind": fact.container_kind,
            "roles": [fact.role],
        }

    for fact in authority.sections:
        yield {
            "schema_version": "round_map_item.v1",
            "item_type": "section_node",
            "id": fact.section_id,
            "document_id": fact.document_id,
            "section_ref": _thaw_json_object(fact.section_ref_json),
            "label": fact.label,
            "heading": fact.heading,
            "level": fact.level,
            "basis": "word_outline_level_v1",
            "label_basis": fact.label_basis,
            "roles": [fact.role],
        }

    seed_paragraph = _authority_seed_paragraph(authority)
    equality_items = [
        _equality_relationship(
            seed_paragraph.paragraph_id,
            fact.paragraph_id,
            seed_paragraph.paragraph_text_sha256,
        )
        for fact in authority.paragraphs
        if fact.role == "exact_candidate"
    ]
    seed_sections = [
        fact for fact in authority.sections if fact.role == "seed_navigation"
    ]
    navigation_items: list[dict[str, Any]] = []
    if seed_sections:
        seed_section = seed_sections[0]
        for fact in authority.sections:
            if fact.role != "candidate_navigation":
                continue
            signals: list[dict[str, str]] = []
            if seed_section.label is not None and fact.label == seed_section.label:
                signals.append(
                    {
                        "kind": "label_exact_v1",
                        "value_sha256": hashlib.sha256(
                            seed_section.label.encode("utf-8")
                        ).hexdigest(),
                    }
                )
            if (
                seed_section.heading is not None
                and fact.heading == seed_section.heading
            ):
                signals.append(
                    {
                        "kind": "heading_exact_v1",
                        "value_sha256": hashlib.sha256(
                            seed_section.heading.encode("utf-8")
                        ).hexdigest(),
                    }
                )
            navigation_items.append(
                _navigation_relationship(
                    seed_section.section_id, fact.section_id, signals
                )
            )
    relationship_groups = (
        sorted(
            (
                _recorded_relationship_from_evidence(fact)
                for fact in authority.recorded_relationships
            ),
            key=_item_id,
        ),
        sorted(equality_items, key=_item_id),
        sorted(navigation_items, key=_item_id),
    )
    yield from heapq.merge(*relationship_groups, key=_item_id)

    resolutions = [
        _project_resolution(authority, document_id, indexes)
        for document_id in indexes["document_ids"]
    ]
    yield from sorted(resolutions, key=_item_id)

    for fact in authority.conflicts:
        yield {
            "schema_version": "round_map_item.v1",
            "item_type": "conflict",
            "id": fact.conflict_id,
            "conflict_type": "inconsistent_apply_record",
            "reason": fact.reason,
            "affected_document_ids": list(fact.affected_document_ids),
            "record_sha256": fact.record_sha256,
            "edge_emitted": False,
        }


def _projection_facts(authority: _SourceEvidence) -> _ProjectionFacts:
    from jsonschema import Draft202012Validator

    from .round_map_contract import ROUND_MAP_ITEM_SCHEMA

    validator = Draft202012Validator(ROUND_MAP_ITEM_SCHEMA)
    digest = hashlib.sha256()
    digest.update(b'{"items":[')
    fingerprints: list[_ItemFingerprint] = []
    page: list[dict[str, Any]] = []
    item_type_counts = {name: 0 for name in _TYPE_RANK}
    relationship_counts = {
        "recorded_derivation": 0,
        "exact_content_equality": 0,
        "navigation_candidate": 0,
    }
    resolution_counts = {"exact_unique": 0, "ambiguous": 0, "unresolved": 0}
    record_only_count = 0
    previous_key: tuple[int, str] | None = None
    for index, item in enumerate(_iter_projected_items(authority)):
        if index >= ROUND_MAP_LIMITS["total_map_items"]:
            raise RoundMapError(
                "resource_limit_exceeded", "complete map exceeds total item limit"
            )
        if next(validator.iter_errors(item), None) is not None:
            _output_contract_error("source projection violates item schema")
        order_key = (_TYPE_RANK[item["item_type"]], item["id"])
        if previous_key is not None and order_key <= previous_key:
            _output_contract_error("source projection order is invalid")
        previous_key = order_key
        if index:
            digest.update(b",")
        digest.update(records._canonical_json_bytes(item))
        fingerprints.append(_item_fingerprint(item))
        item_type_counts[item["item_type"]] += 1
        if item["item_type"] == "relationship":
            relationship_counts[item["relationship_type"]] += 1
        elif item["item_type"] == "resolution":
            resolution_counts[item["state"]] += 1
        elif (
            item["item_type"] == "document_node"
            and item["observation_state"] == "record_only"
        ):
            record_only_count += 1
        if (
            authority.cursor_offset
            <= index
            < (authority.cursor_offset + authority.page_size)
        ):
            page.append(item)
    digest.update(b'],"schema_version":"round_map_item_set.v1"}')
    return _ProjectionFacts(
        item_fingerprints=tuple(fingerprints),
        full_result_set_sha256=digest.hexdigest(),
        page_items=tuple(page),
        item_type_counts=tuple(sorted(item_type_counts.items())),
        relationship_counts=tuple(sorted(relationship_counts.items())),
        resolution_counts=tuple(sorted(resolution_counts.items())),
        record_only_document_count=record_only_count,
        eligible_item_count=len(fingerprints),
    )


def _cursor_binding(
    *,
    next_offset: int,
    folder: str,
    seed_path: str,
    seed_ref: dict[str, Any],
    filenames: list[str],
    seed_document_id: str,
    seed_paragraph_id: str,
    ordering_source: str,
    filename_manifest_sha256: str,
    filesystem_snapshot_sha256: str,
    journal_snapshot_sha256: str,
    full_result_set_sha256: str,
) -> str:
    payload = {
        "schema_version": "round_map_cursor_binding.v1",
        "next_offset": next_offset,
        "canonical_input": {
            "schema_version": "round_map_canonical_input.v1",
            "folder": folder,
            "seed": {
                "schema_version": "round_map_seed.v1",
                "path": seed_path,
                "paragraph_ref_sha256": _digest(seed_ref),
            },
            "ordered_filenames": filenames,
        },
        "seed_document_id": seed_document_id,
        "seed_paragraph_id": seed_paragraph_id,
        "ordering_source": ordering_source,
        "filename_manifest_sha256": filename_manifest_sha256,
        "filesystem_snapshot_sha256": filesystem_snapshot_sha256,
        "journal_snapshot_sha256": journal_snapshot_sha256,
        "full_result_set_sha256": full_result_set_sha256,
        "limits_sha256": _digest(ROUND_MAP_LIMITS),
        "policy_versions": _POLICY_VERSIONS,
    }
    return _digest(payload)


def _output_contract_error(detail: str) -> None:
    raise RoundMapError("output_contract_error", detail)


def _validate_complete_items(
    items: list[dict[str, Any]], proof: _RoundMapProof
) -> dict[str, Any]:
    """Validate a candidate graph against the one immutable source authority."""
    facts = _projection_facts(proof.authority)
    if (
        facts.item_fingerprints != proof.item_fingerprints
        or facts.full_result_set_sha256 != proof.full_result_set_sha256
        or facts.item_type_counts != proof.item_type_counts
        or facts.relationship_counts != proof.relationship_counts
        or facts.resolution_counts != proof.resolution_counts
        or facts.record_only_document_count != proof.record_only_document_count
    ):
        _output_contract_error("projection metadata differs from source authority")
    if len(items) != facts.eligible_item_count:
        _output_contract_error("complete item count differs from source authority")
    for candidate, expected in zip(
        items, _iter_projected_items(proof.authority), strict=True
    ):
        if not _canonical_equal(candidate, expected):
            _output_contract_error("complete item differs from source authority")
    return {
        "item_type_counts": dict(facts.item_type_counts),
        "relationship_counts": dict(facts.relationship_counts),
        "resolution_counts": dict(facts.resolution_counts),
        "record_only_document_count": facts.record_only_document_count,
    }


def _project_result(
    authority: _SourceEvidence, facts: _ProjectionFacts
) -> dict[str, Any]:
    seed = _authority_seed_paragraph(authority)
    next_offset = authority.cursor_offset + len(facts.page_items)
    next_cursor = None
    if next_offset < facts.eligible_item_count:
        next_cursor = f"rm1:{next_offset}:" + _cursor_binding(
            next_offset=next_offset,
            folder=authority.workspace_path,
            seed_path=authority.seed_path,
            seed_ref=_authority_seed_ref(authority),
            filenames=list(_authority_filenames(authority)),
            seed_document_id=seed.document_id,
            seed_paragraph_id=seed.paragraph_id,
            ordering_source=authority.ordering_source,
            filename_manifest_sha256=_authority_filename_manifest_sha256(authority),
            filesystem_snapshot_sha256=_authority_filesystem_snapshot_sha256(authority),
            journal_snapshot_sha256=_authority_journal_snapshot_sha256(authority),
            full_result_set_sha256=facts.full_result_set_sha256,
        )
    order_basis = (
        {"kind": "filename", "rule": "casefold_then_exact"}
        if authority.ordering_source == "filename_lexicographic_v1"
        else {
            "kind": "caller_supplied_filename_sequence",
            "rule": "exact_sequence",
        }
    )
    relevant_count = _authority_relevant_apply_record_count(authority)
    return {
        "schema_version": "round_map.v1",
        "status": "ok",
        "seed": {
            "document_id": seed.document_id,
            "paragraph_id": seed.paragraph_id,
            "paragraph_ref": _authority_seed_ref(authority),
        },
        "ordering_source": authority.ordering_source,
        "order_basis": {
            **order_basis,
            "lineage_verified": False,
            "round_id_semantics": "position_only",
            "filename_manifest_sha256": _authority_filename_manifest_sha256(authority),
        },
        "snapshot": {
            "schema_version": "round_map_snapshot.v1",
            "filesystem_snapshot_sha256": _authority_filesystem_snapshot_sha256(
                authority
            ),
            "journal_snapshot_sha256": _authority_journal_snapshot_sha256(authority),
            "journal_state": (
                "relevant_apply_records_present"
                if relevant_count
                else "no_relevant_apply_records"
            ),
            "full_result_set_sha256": facts.full_result_set_sha256,
            "filesystem_cross_file_atomic": False,
            "cross_source_atomic": False,
        },
        "items": [deepcopy(item) for item in facts.page_items],
        "coverage": {
            "scan_complete": True,
            "candidate_document_count": len(authority.observations),
            "inspected_document_count": len(authority.observations),
            "record_only_document_count": facts.record_only_document_count,
            "relevant_apply_record_count": relevant_count,
            "eligible_derivation_record_count": (
                _authority_eligible_derivation_record_count(authority)
            ),
            "rejected_semantic_record_count": len(authority.conflicts),
            "eligible_item_count": facts.eligible_item_count,
            "returned_item_count": len(facts.page_items),
            "cursor_offset": authority.cursor_offset,
            "output_truncated": next_cursor is not None,
            "item_type_counts": dict(facts.item_type_counts),
            "relationship_counts": dict(facts.relationship_counts),
            "resolution_counts": dict(facts.resolution_counts),
            "search_scope": "word_document_xml_body_v1",
            "reading_mode": "accepted_current_v1",
            "container_policy": "canonical_body_flow_v1",
            "whole_docx_coverage": False,
            "negative_whole_doc_claims": False,
        },
        "limits": deepcopy(ROUND_MAP_LIMITS),
        "next_cursor": next_cursor,
    }


def _validate_result_invariants(result: dict[str, Any], proof: _RoundMapProof) -> None:
    try:
        facts = _projection_facts(proof.authority)
        if (
            facts.item_fingerprints != proof.item_fingerprints
            or facts.full_result_set_sha256 != proof.full_result_set_sha256
            or facts.item_type_counts != proof.item_type_counts
            or facts.relationship_counts != proof.relationship_counts
            or facts.resolution_counts != proof.resolution_counts
            or facts.record_only_document_count != proof.record_only_document_count
        ):
            _output_contract_error("projection metadata differs from source authority")
        candidate = deepcopy(result)
        candidate.pop("producer", None)
        if not _canonical_equal(candidate, _project_result(proof.authority, facts)):
            _output_contract_error("result differs from immutable source authority")
    except RoundMapError:
        raise
    except Exception as exc:
        raise RoundMapError(
            "output_contract_error", "result invariants cannot be established"
        ) from exc


def validate_computation_result(
    computation: RoundMapComputation, result: dict[str, Any]
) -> None:
    """Fail closed on a normalized result before success publication."""
    _validate_result_invariants(result, computation.proof)


def build_round_map(
    folder: object,
    seed: object,
    *,
    ordered_filenames: object = None,
    cursor: object = None,
    max_items: object = DEFAULT_MAX_ITEMS,
) -> RoundMapComputation:
    """Build immutable source evidence, then project one bounded page."""
    (
        folder_text,
        checked_seed,
        checked_order,
        parsed_cursor,
        checked_max_items,
    ) = _validate_inputs(folder, seed, ordered_filenames, cursor, max_items)
    (
        workspace,
        workspace_identity,
        captured,
        captured_seed,
        ordering_source,
        _,
        _,
    ) = _capture_workspace(folder_text, checked_seed["path"], checked_order)
    current = _parse_candidates(captured)
    resolved_seed = _resolve_seed_evidence(
        current, captured_seed, checked_seed["paragraph_ref"]
    )
    current_ids = {item.document_id for item in current}
    included_ids, derivation_records, relevant_conflicts = _journal_facts(
        workspace, workspace_identity, current_ids
    )
    offset = 0 if parsed_cursor is None else parsed_cursor[0]
    authority = _derive_source_evidence(
        workspace=workspace,
        workspace_identity=workspace_identity,
        seed_path=captured_seed.path,
        ordering_source=ordering_source,
        cursor_offset=offset,
        page_size=checked_max_items,
        current=current,
        resolved_seed=resolved_seed,
        included_document_ids=included_ids,
        derivation_records=derivation_records,
        relevant_conflicts=relevant_conflicts,
    )
    facts = _projection_facts(authority)
    if parsed_cursor is not None:
        supplied_binding = parsed_cursor[1]
        expected_binding = _cursor_binding(
            next_offset=offset,
            folder=authority.workspace_path,
            seed_path=authority.seed_path,
            seed_ref=_authority_seed_ref(authority),
            filenames=list(_authority_filenames(authority)),
            seed_document_id=_authority_seed_paragraph(authority).document_id,
            seed_paragraph_id=_authority_seed_paragraph(authority).paragraph_id,
            ordering_source=authority.ordering_source,
            filename_manifest_sha256=_authority_filename_manifest_sha256(authority),
            filesystem_snapshot_sha256=_authority_filesystem_snapshot_sha256(authority),
            journal_snapshot_sha256=_authority_journal_snapshot_sha256(authority),
            full_result_set_sha256=facts.full_result_set_sha256,
        )
        if supplied_binding != expected_binding:
            raise RoundMapError("cursor_mismatch", "cursor does not bind this map")
        if not 1 <= offset < facts.eligible_item_count:
            raise RoundMapError(
                "invalid_cursor", "cursor offset is outside the result set"
            )
    result = _project_result(authority, facts)
    proof = _RoundMapProof(
        authority=authority,
        item_fingerprints=facts.item_fingerprints,
        full_result_set_sha256=facts.full_result_set_sha256,
        item_type_counts=facts.item_type_counts,
        relationship_counts=facts.relationship_counts,
        resolution_counts=facts.resolution_counts,
        record_only_document_count=facts.record_only_document_count,
    )
    computation = RoundMapComputation(result=result, proof=proof)
    return computation


def _record_summary_projection(result: dict[str, Any]) -> dict[str, Any]:
    items = result["items"]
    sample = [
        {
            "item_type": item["item_type"],
            "id": _item_id(item),
            "item_sha256": _digest(item),
        }
        for item in items[: ROUND_MAP_LIMITS["sample_items"]]
    ]
    next_cursor = result["next_cursor"]
    return {
        "status": "ok",
        "seed": deepcopy(result["seed"]),
        "ordering_source": result["ordering_source"],
        "filename_manifest_sha256": result["order_basis"]["filename_manifest_sha256"],
        "snapshot": deepcopy(result["snapshot"]),
        "coverage": deepcopy(result["coverage"]),
        "limits": deepcopy(result["limits"]),
        "next_cursor_sha256": (
            None if next_cursor is None else _digest({"next_cursor": next_cursor})
        ),
        "items_summary": {
            "count": len(items),
            "sha256": _digest(
                {"schema_version": "round_map_returned_items.v1", "items": items}
            ),
            "sample": sample,
            "truncated": len(items) > ROUND_MAP_LIMITS["sample_items"],
        },
    }


def record_summary(computation: RoundMapComputation) -> dict[str, Any]:
    """Return the exact bounded path-free raw-journal result projection."""
    authority = computation.proof.authority
    page = tuple(
        islice(
            _iter_projected_items(authority),
            authority.cursor_offset,
            authority.cursor_offset + authority.page_size,
        )
    )
    facts = _ProjectionFacts(
        item_fingerprints=computation.proof.item_fingerprints,
        full_result_set_sha256=computation.proof.full_result_set_sha256,
        page_items=page,
        item_type_counts=computation.proof.item_type_counts,
        relationship_counts=computation.proof.relationship_counts,
        resolution_counts=computation.proof.resolution_counts,
        record_only_document_count=computation.proof.record_only_document_count,
        eligible_item_count=len(computation.proof.item_fingerprints),
    )
    return _record_summary_projection(_project_result(authority, facts))


def _record_provenance_projection(result: dict[str, Any]) -> dict[str, Any]:
    snapshot = result["snapshot"]
    coverage = result["coverage"]
    return {
        "filesystem_snapshot_sha256": snapshot["filesystem_snapshot_sha256"],
        "journal_snapshot_sha256": snapshot["journal_snapshot_sha256"],
        "full_result_set_sha256": snapshot["full_result_set_sha256"],
        "reading_mode": coverage["reading_mode"],
        "container_policy": coverage["container_policy"],
        "search_scope": coverage["search_scope"],
    }


def record_provenance(computation: RoundMapComputation) -> dict[str, Any]:
    authority = computation.proof.authority
    return {
        "filesystem_snapshot_sha256": _authority_filesystem_snapshot_sha256(authority),
        "journal_snapshot_sha256": _authority_journal_snapshot_sha256(authority),
        "full_result_set_sha256": computation.proof.full_result_set_sha256,
        "reading_mode": "accepted_current_v1",
        "container_policy": "canonical_body_flow_v1",
        "search_scope": "word_document_xml_body_v1",
    }


def validate_record_projection(
    tool_result: object, result: object, provenance: object
) -> bool:
    """Bind the success-only stored projection to the validated live map."""
    if not isinstance(tool_result, dict):
        return False
    try:
        return _canonical_equal(result, _record_summary_projection(tool_result)) and (
            _canonical_equal(provenance, _record_provenance_projection(tool_result))
        )
    except Exception:
        return False
