# SPDX-License-Identifier: Apache-2.0
"""Bounded seed-centred Round Map implementation for the v0.3 MCP surface."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from veqtor_docx._ooxml import (
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
_CURSOR_RE = re.compile(r"^rm1:([1-9][0-9]*):([0-9a-f]{64})$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
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
    workspace: Path


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
        normalized_order = list(ordered_filenames)
        for filename in normalized_order:
            if (
                not isinstance(filename, str)
                or not filename
                or filename in {".", ".."}
                or "/" in filename
                or "\\" in filename
                or not filename.casefold().endswith(".docx")
            ):
                raise RoundMapError(
                    "invalid_round_order", "ordered filename is not a direct DOCX name"
                )
        if len(normalized_order) != len(set(normalized_order)):
            raise RoundMapError(
                "invalid_round_order", "ordered_filenames contains duplicates"
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


def _enumerate_candidates(root_fd: int) -> dict[str, _EnumeratedCandidate]:
    candidates: dict[str, _EnumeratedCandidate] = {}
    try:
        with os.scandir(root_fd) as entries:
            for entry in entries:
                if not _candidate_name(entry.name):
                    continue
                if len(candidates) >= ROUND_MAP_LIMITS["candidate_docx_files"]:
                    raise RoundMapError(
                        "resource_limit_exceeded",
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
    if len(ordered_filenames) != len(candidates) or set(ordered_filenames) != set(
        candidates
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
    except OSError:
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
        if len(payload) > limit:
            raise RoundMapError(
                "resource_limit_exceeded", "candidate exceeds 50 MiB compressed limit"
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
) -> tuple[Path, list[_CapturedCandidate], str, dict[str, Any], str]:
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
            if total_bytes > ROUND_MAP_LIMITS["candidate_compressed_input_bytes"]:
                raise RoundMapError(
                    "resource_limit_exceeded",
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

        final_names = set(_enumerate_candidates(root_fd))
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
    finally:
        os.close(root_fd)

    manifest = {
        "schema_version": "round_map_filename_manifest.v1",
        "ordering_source": ordering_source,
        "filenames": filenames,
    }
    manifest_sha256 = _digest(manifest)
    return canonical, captured, ordering_source, order_basis, manifest_sha256


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
            )
        except ResourceLimitError as exc:
            raise RoundMapError(
                "resource_limit_exceeded", "candidate exceeds a processing limit"
            ) from exc
        except InspectError as exc:
            code = exc.code
            if code not in {
                "missing_document_part",
                "file_unextractable",
                "unsupported_compression",
                "encrypted_docx",
            }:
                code = "invalid_docx"
            raise RoundMapError(code, "candidate DOCX cannot be inspected") from exc
        except DocxError as exc:
            code = getattr(exc, "code", "invalid_docx")
            if code not in {
                "file_unextractable",
                "unsupported_compression",
                "encrypted_docx",
            }:
                code = "invalid_docx"
            raise RoundMapError(code, "candidate DOCX cannot be inspected") from exc
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
    if result_round_trip != provenance_round_trip:
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
                result.get(key) != provenance.get(key) for key in _STRENGTHENED_FIELDS
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
    current_document_ids: set[str],
) -> tuple[
    set[str],
    list[_ApplyClassification],
    list[tuple[_ApplyClassification, tuple[str, ...]]],
    str,
    str,
]:
    try:
        raw_records = records.read_round_map_apply_records(workspace)
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
    relevant = [*relevant_valid, *(item for item, _affected in relevant_conflicts)]
    record_sha256s = sorted(item.record_sha256 for item in relevant)
    journal_sha256 = _digest(
        {
            "schema_version": "round_map_relevant_journal_snapshot.v1",
            "record_sha256s": record_sha256s,
        }
    )
    journal_state = (
        "relevant_apply_records_present" if relevant else "no_relevant_apply_records"
    )
    return included, relevant_valid, relevant_conflicts, journal_sha256, journal_state


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


def _recorded_relationship(
    source_id: str,
    output_id: str,
    support: list[_ApplyClassification],
) -> dict[str, Any]:
    ordered = sorted(support, key=_support_sort_key)
    records_list = [
        {
            "record_id": item.record["record_id"],
            "record_sha256": item.record_sha256,
            "profile": item.profile,
        }
        for item in ordered
    ]
    profile_counts = {
        "current_count": sum(item.profile == "current_v0.3" for item in ordered),
        "published_v0_1_2_count": sum(
            item.profile == "published_v0.1.2_preflightless" for item in ordered
        ),
        "frozen_legacy_count": sum(
            item.profile == "frozen_legacy_v1" for item in ordered
        ),
    }
    nonzero = sum(bool(value) for value in profile_counts.values())
    if nonzero > 1:
        support_profile = "mixed"
    elif profile_counts["current_count"]:
        support_profile = "current_only"
    elif profile_counts["published_v0_1_2_count"]:
        support_profile = "published_v0_1_2_only"
    else:
        support_profile = "frozen_legacy_only"
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
    identity = {
        "schema_version": "relationship_identity.v1",
        "relationship_type": "recorded_derivation",
        "from_id": source_id,
        "to_id": output_id,
        "direction": "directed",
        "basis_identity": _RECORDED_BASIS_IDENTITY,
    }
    return {
        "schema_version": "round_map_item.v1",
        "item_type": "relationship",
        "id": _derived_id("rm_rel_v1", identity),
        "relationship_type": "recorded_derivation",
        "from_id": source_id,
        "to_id": output_id,
        "direction": "directed",
        "basis": basis,
        "derivation_recorded": True,
        "lineage_verified": False,
        "chronology_verified": False,
    }


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


def _enforce_item_cap(
    items: list[dict[str, Any]], item_type: str, limit_key: str
) -> None:
    if len(items) > ROUND_MAP_LIMITS[limit_key]:
        raise RoundMapError(
            "resource_limit_exceeded", f"{item_type} count exceeds its fixed limit"
        )


def _build_items(
    current: list[_CurrentDocument],
    seed_path: str,
    seed_ref: dict[str, Any],
    included_document_ids: set[str],
    derivation_records: list[_ApplyClassification],
    relevant_conflicts: list[tuple[_ApplyClassification, tuple[str, ...]]],
) -> tuple[list[dict[str, Any]], dict[str, Any], str, str, dict[str, int]]:
    by_path = {item.captured.path: item for item in current}
    seed_current = by_path[seed_path]
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
    seed_paragraph_id, resolved_seed_ref = _paragraph_identity(
        seed_current.snapshot, seed_paragraph
    )

    observations_by_document: dict[str, list[_CurrentDocument]] = defaultdict(list)
    for item in current:
        observations_by_document[item.document_id].append(item)
    document_ids = set(included_document_ids)

    support_by_edge: dict[tuple[str, str], list[_ApplyClassification]] = defaultdict(
        list
    )
    for item in derivation_records:
        assert item.source_id is not None and item.output_id is not None
        support_by_edge[(item.source_id, item.output_id)].append(item)
    derivation_relationships = [
        _recorded_relationship(source, output, support)
        for (source, output), support in support_by_edge.items()
    ]
    _enforce_item_cap(
        derivation_relationships,
        "recorded derivation relationship",
        "recorded_derivation_relationships",
    )
    edge_pairs = set(support_by_edge)
    cycle_members = _cycle_members(document_ids, edge_pairs)

    paragraph_nodes_by_id: dict[str, dict[str, Any]] = {
        seed_paragraph_id: {
            "schema_version": "round_map_item.v1",
            "item_type": "paragraph_node",
            "id": seed_paragraph_id,
            "document_id": seed_current.document_id,
            "paragraph_ref": resolved_seed_ref,
            "container_kind": seed_paragraph.container_kind,
            "roles": ["seed"],
        }
    }
    exact_candidate_ids: dict[str, set[str]] = defaultdict(set)
    equality_relationships_by_id: dict[str, dict[str, Any]] = {}
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
            if paragraph_id == seed_paragraph_id:
                continue
            paragraph_nodes_by_id.setdefault(
                paragraph_id,
                {
                    "schema_version": "round_map_item.v1",
                    "item_type": "paragraph_node",
                    "id": paragraph_id,
                    "document_id": document.document_id,
                    "paragraph_ref": reference,
                    "container_kind": paragraph.container_kind,
                    "roles": ["exact_candidate"],
                },
            )
            exact_candidate_ids[document.document_id].add(paragraph_id)
            relationship = _equality_relationship(
                seed_paragraph_id,
                paragraph_id,
                seed_paragraph.text_sha256,
            )
            equality_relationships_by_id[relationship["id"]] = relationship
    paragraph_nodes = list(paragraph_nodes_by_id.values())
    equality_relationships = list(equality_relationships_by_id.values())
    _enforce_item_cap(paragraph_nodes, "paragraph node", "paragraph_nodes")
    _enforce_item_cap(
        equality_relationships,
        "exact equality relationship",
        "exact_equality_relationships",
    )

    section_nodes_by_id: dict[str, dict[str, Any]] = {}
    navigation_candidate_ids: dict[str, set[str]] = defaultdict(set)
    navigation_relationships_by_id: dict[str, dict[str, Any]] = {}
    seed_section = seed_current.snapshot.section_by_paragraph.get(
        seed_paragraph.paragraph_index
    )
    if seed_section is not None:
        seed_section_id, section_ref = _section_identity(
            seed_current.snapshot, seed_section
        )
        section_nodes_by_id[seed_section_id] = {
            "schema_version": "round_map_item.v1",
            "item_type": "section_node",
            "id": seed_section_id,
            "document_id": seed_current.document_id,
            "section_ref": section_ref,
            "label": seed_section.label,
            "heading": seed_section.title,
            "level": seed_section.level,
            "basis": "word_outline_level_v1",
            "label_basis": seed_section.label_basis,
            "roles": ["seed_navigation"],
        }
        for document in current:
            for section in document.snapshot.sections:
                candidate_section_id, candidate_ref = _section_identity(
                    document.snapshot, section
                )
                if candidate_section_id == seed_section_id:
                    continue
                signals: list[dict[str, str]] = []
                if (
                    seed_section.label is not None
                    and section.label == seed_section.label
                ):
                    signals.append(
                        {
                            "kind": "label_exact_v1",
                            "value_sha256": hashlib.sha256(
                                seed_section.label.encode("utf-8")
                            ).hexdigest(),
                        }
                    )
                if (
                    seed_section.title is not None
                    and section.title == seed_section.title
                ):
                    signals.append(
                        {
                            "kind": "heading_exact_v1",
                            "value_sha256": hashlib.sha256(
                                seed_section.title.encode("utf-8")
                            ).hexdigest(),
                        }
                    )
                if not signals:
                    continue
                section_nodes_by_id.setdefault(
                    candidate_section_id,
                    {
                        "schema_version": "round_map_item.v1",
                        "item_type": "section_node",
                        "id": candidate_section_id,
                        "document_id": document.document_id,
                        "section_ref": candidate_ref,
                        "label": section.label,
                        "heading": section.title,
                        "level": section.level,
                        "basis": "word_outline_level_v1",
                        "label_basis": section.label_basis,
                        "roles": ["candidate_navigation"],
                    },
                )
                navigation_candidate_ids[document.document_id].add(candidate_section_id)
                relationship = _navigation_relationship(
                    seed_section_id, candidate_section_id, signals
                )
                navigation_relationships_by_id[relationship["id"]] = relationship
    section_nodes = list(section_nodes_by_id.values())
    navigation_relationships = list(navigation_relationships_by_id.values())
    _enforce_item_cap(section_nodes, "section node", "section_nodes")
    _enforce_item_cap(
        navigation_relationships,
        "navigation relationship",
        "navigation_relationships",
    )

    conflict_items: list[dict[str, Any]] = []
    conflicts_by_document: dict[str, int] = defaultdict(int)
    for classification, affected in relevant_conflicts:
        assert classification.reason is not None
        identity = {
            "schema_version": "conflict_identity.v1",
            "conflict_type": "inconsistent_apply_record",
            "affected_document_ids": list(affected),
            "record_sha256": classification.record_sha256,
        }
        conflict_items.append(
            {
                "schema_version": "round_map_item.v1",
                "item_type": "conflict",
                "id": _derived_id("rm_conflict_v1", identity),
                "conflict_type": "inconsistent_apply_record",
                "reason": classification.reason,
                "affected_document_ids": list(affected),
                "record_sha256": classification.record_sha256,
                "edge_emitted": False,
            }
        )
        for document_id in affected:
            conflicts_by_document[document_id] += 1
    _enforce_item_cap(conflict_items, "conflict item", "conflict_items")

    incoming: dict[str, set[str]] = defaultdict(set)
    outgoing: dict[str, set[str]] = defaultdict(set)
    for source, output in edge_pairs:
        incoming[output].add(source)
        outgoing[source].add(output)
    document_nodes: list[dict[str, Any]] = []
    observation_items: list[dict[str, Any]] = []
    for document in current:
        observation_items.append(
            {
                "schema_version": "round_map_item.v1",
                "item_type": "document_observation",
                "id": document.observation_id,
                "document_id": document.document_id,
                "path": document.captured.path,
                "filename": document.captured.filename,
                "position": document.captured.position,
                "round_id": f"round-{document.captured.position + 1:03d}",
                "position_basis": None,
            }
        )
    for document_id in sorted(document_ids):
        observations = observations_by_document.get(document_id, [])
        is_endpoint = bool(incoming[document_id] or outgoing[document_id])
        if not observations:
            state = "record_only"
            inspection_coverage = None
            file_sha256 = document_id.removeprefix("rm_doc_v1:")
        else:
            state = "current_and_recorded" if is_endpoint else "current"
            inspection_coverage = deepcopy(observations[0].inspection_coverage)
            file_sha256 = observations[0].snapshot.file_sha256
        document_nodes.append(
            {
                "schema_version": "round_map_item.v1",
                "item_type": "document_node",
                "id": document_id,
                "file_sha256": file_sha256,
                "observation_state": state,
                "observation_count": len(observations),
                "inspection_coverage": inspection_coverage,
                "incoming_recorded_derivation_count": len(incoming[document_id]),
                "outgoing_recorded_derivation_count": len(outgoing[document_id]),
                "topology_flags": {
                    "multiple_parents": len(incoming[document_id]) > 1,
                    "cycle_member": document_id in cycle_members,
                    "self_loop": (document_id, document_id) in edge_pairs,
                },
            }
        )
    _enforce_item_cap(document_nodes, "document node", "document_nodes")
    _enforce_item_cap(
        observation_items, "document observation", "document_observations"
    )

    resolution_items: list[dict[str, Any]] = []
    for document_id in sorted(document_ids):
        exact_ids = exact_candidate_ids.get(document_id, set())
        navigation_ids = navigation_candidate_ids.get(document_id, set())
        conflict_count = conflicts_by_document[document_id]
        observations = observations_by_document.get(document_id, [])
        pruned = any(
            not observation.inspection_coverage["container_coverage"].get(
                "coverage_complete", False
            )
            or observation.inspection_coverage["container_coverage"].get(
                "excluded_subtree_count", 0
            )
            > 0
            for observation in observations
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
        candidate_ids = sorted(exact_ids | navigation_ids)
        identity = {
            "schema_version": "resolution_identity.v1",
            "seed_paragraph_id": seed_paragraph_id,
            "document_id": document_id,
        }
        resolution_items.append(
            {
                "schema_version": "round_map_item.v1",
                "item_type": "resolution",
                "id": _derived_id("rm_resolution_v1", identity),
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
        )
    _enforce_item_cap(resolution_items, "resolution item", "resolution_items")

    relationships = [
        *derivation_relationships,
        *equality_relationships,
        *navigation_relationships,
    ]
    all_items = [
        *document_nodes,
        *observation_items,
        *paragraph_nodes,
        *section_nodes,
        *relationships,
        *resolution_items,
        *conflict_items,
    ]
    if len(all_items) > ROUND_MAP_LIMITS["total_map_items"]:
        raise RoundMapError(
            "resource_limit_exceeded", "complete map exceeds total item limit"
        )
    all_items.sort(key=lambda item: (_TYPE_RANK[item["item_type"]], item["id"]))
    type_counts = {
        name: sum(item["item_type"] == name for item in all_items)
        for name in _TYPE_RANK
    }
    relationship_counts = {
        name: sum(
            item["item_type"] == "relationship" and item["relationship_type"] == name
            for item in all_items
        )
        for name in (
            "recorded_derivation",
            "exact_content_equality",
            "navigation_candidate",
        )
    }
    resolution_counts = {
        name: sum(
            item["item_type"] == "resolution" and item["state"] == name
            for item in all_items
        )
        for name in ("exact_unique", "ambiguous", "unresolved")
    }
    counts = {
        "record_only_document_count": sum(
            item["item_type"] == "document_node"
            and item["observation_state"] == "record_only"
            for item in all_items
        ),
        **{f"relationship_{key}": value for key, value in relationship_counts.items()},
        **{f"resolution_{key}": value for key, value in resolution_counts.items()},
    }
    return (
        all_items,
        {
            "relationship_counts": relationship_counts,
            "resolution_counts": resolution_counts,
            "item_type_counts": type_counts,
        },
        seed_current.document_id,
        seed_paragraph_id,
        counts,
    )


def _filesystem_snapshot(
    current: list[_CurrentDocument],
    filename_manifest_sha256: str,
) -> str:
    observations = [
        {
            "observation_id": item.observation_id,
            "canonical_path": item.captured.path,
            "filename": item.captured.filename,
            "position": item.captured.position,
            "byte_length": len(item.captured.payload),
            "file_sha256": item.snapshot.file_sha256,
            "inspection_coverage_sha256": _digest(item.inspection_coverage),
        }
        for item in current
    ]
    return _digest(
        {
            "schema_version": "round_map_filesystem_snapshot.v1",
            "filename_manifest_sha256": filename_manifest_sha256,
            "observations": observations,
        }
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


def _validate_result_invariants(result: dict[str, Any]) -> None:
    coverage = result["coverage"]
    items = result["items"]
    if coverage["returned_item_count"] != len(items):
        raise RoundMapError("output_contract_error", "returned item count mismatch")
    if (
        coverage["candidate_document_count"] != coverage["inspected_document_count"]
        or coverage["candidate_document_count"]
        != coverage["item_type_counts"]["document_observation"]
    ):
        raise RoundMapError("output_contract_error", "candidate coverage mismatch")
    if (
        coverage["eligible_derivation_record_count"]
        + coverage["rejected_semantic_record_count"]
        != coverage["relevant_apply_record_count"]
    ):
        raise RoundMapError("output_contract_error", "journal coverage mismatch")
    if (
        coverage["rejected_semantic_record_count"]
        != coverage["item_type_counts"]["conflict"]
    ):
        raise RoundMapError("output_contract_error", "conflict coverage mismatch")
    if sum(coverage["item_type_counts"].values()) != coverage["eligible_item_count"]:
        raise RoundMapError("output_contract_error", "item type counts mismatch")
    if (
        sum(coverage["relationship_counts"].values())
        != coverage["item_type_counts"]["relationship"]
    ):
        raise RoundMapError("output_contract_error", "relationship counts mismatch")
    if (
        sum(coverage["resolution_counts"].values())
        != coverage["item_type_counts"]["resolution"]
    ):
        raise RoundMapError("output_contract_error", "resolution counts mismatch")
    if (
        coverage["item_type_counts"]["resolution"]
        != coverage["item_type_counts"]["document_node"]
    ):
        raise RoundMapError("output_contract_error", "resolution coverage mismatch")
    if (
        coverage["record_only_document_count"]
        > coverage["item_type_counts"]["document_node"]
    ):
        raise RoundMapError("output_contract_error", "record-only count mismatch")
    if (
        coverage["cursor_offset"] + coverage["returned_item_count"]
        > coverage["eligible_item_count"]
    ):
        raise RoundMapError("output_contract_error", "page coverage mismatch")
    if coverage["output_truncated"] is not (result["next_cursor"] is not None):
        raise RoundMapError("output_contract_error", "cursor truncation mismatch")


def build_round_map(
    folder: object,
    seed: object,
    *,
    ordered_filenames: object = None,
    cursor: object = None,
    max_items: object = DEFAULT_MAX_ITEMS,
) -> RoundMapComputation:
    """Build the complete bounded fact set, then return one stateless page."""
    (
        folder_text,
        checked_seed,
        checked_order,
        parsed_cursor,
        checked_max_items,
    ) = _validate_inputs(folder, seed, ordered_filenames, cursor, max_items)
    (
        workspace,
        captured,
        ordering_source,
        order_basis,
        filename_manifest_sha256,
    ) = _capture_workspace(folder_text, checked_seed["path"], checked_order)
    current = _parse_candidates(captured)
    canonical_seed_path = _canonical_seed_path(checked_seed["path"])
    current_ids = {item.document_id for item in current}
    (
        included_ids,
        derivation_records,
        relevant_conflicts,
        journal_snapshot_sha256,
        journal_state,
    ) = _journal_facts(workspace, current_ids)
    (
        all_items,
        item_counts,
        seed_document_id,
        seed_paragraph_id,
        derived_counts,
    ) = _build_items(
        current,
        canonical_seed_path,
        checked_seed["paragraph_ref"],
        included_ids,
        derivation_records,
        relevant_conflicts,
    )
    for item in all_items:
        if item["item_type"] == "document_observation":
            item["position_basis"] = ordering_source

    filesystem_snapshot_sha256 = _filesystem_snapshot(current, filename_manifest_sha256)
    full_result_set_sha256 = _digest(
        {"schema_version": "round_map_item_set.v1", "items": all_items}
    )
    filenames = [item.captured.filename for item in current]
    if parsed_cursor is None:
        offset = 0
    else:
        offset, supplied_binding = parsed_cursor
        expected_binding = _cursor_binding(
            next_offset=offset,
            folder=str(workspace),
            seed_path=canonical_seed_path,
            seed_ref=checked_seed["paragraph_ref"],
            filenames=filenames,
            seed_document_id=seed_document_id,
            seed_paragraph_id=seed_paragraph_id,
            ordering_source=ordering_source,
            filename_manifest_sha256=filename_manifest_sha256,
            filesystem_snapshot_sha256=filesystem_snapshot_sha256,
            journal_snapshot_sha256=journal_snapshot_sha256,
            full_result_set_sha256=full_result_set_sha256,
        )
        if supplied_binding != expected_binding:
            raise RoundMapError("cursor_mismatch", "cursor does not bind this map")
        if not 1 <= offset < len(all_items):
            raise RoundMapError(
                "invalid_cursor", "cursor offset is outside the result set"
            )
    page = all_items[offset : offset + checked_max_items]
    next_offset = offset + len(page)
    next_cursor = None
    if next_offset < len(all_items):
        binding = _cursor_binding(
            next_offset=next_offset,
            folder=str(workspace),
            seed_path=canonical_seed_path,
            seed_ref=checked_seed["paragraph_ref"],
            filenames=filenames,
            seed_document_id=seed_document_id,
            seed_paragraph_id=seed_paragraph_id,
            ordering_source=ordering_source,
            filename_manifest_sha256=filename_manifest_sha256,
            filesystem_snapshot_sha256=filesystem_snapshot_sha256,
            journal_snapshot_sha256=journal_snapshot_sha256,
            full_result_set_sha256=full_result_set_sha256,
        )
        next_cursor = f"rm1:{next_offset}:{binding}"

    relevant_count = len(derivation_records) + len(relevant_conflicts)
    coverage = {
        "scan_complete": True,
        "candidate_document_count": len(current),
        "inspected_document_count": len(current),
        "record_only_document_count": derived_counts["record_only_document_count"],
        "relevant_apply_record_count": relevant_count,
        "eligible_derivation_record_count": len(derivation_records),
        "rejected_semantic_record_count": len(relevant_conflicts),
        "eligible_item_count": len(all_items),
        "returned_item_count": len(page),
        "cursor_offset": offset,
        "output_truncated": next_cursor is not None,
        **item_counts,
        "search_scope": "word_document_xml_body_v1",
        "reading_mode": "accepted_current_v1",
        "container_policy": "canonical_body_flow_v1",
        "whole_docx_coverage": False,
        "negative_whole_doc_claims": False,
    }
    result = {
        "schema_version": "round_map.v1",
        "status": "ok",
        "seed": {
            "document_id": seed_document_id,
            "paragraph_id": seed_paragraph_id,
            "paragraph_ref": deepcopy(checked_seed["paragraph_ref"]),
        },
        "ordering_source": ordering_source,
        "order_basis": {
            **order_basis,
            "filename_manifest_sha256": filename_manifest_sha256,
        },
        "snapshot": {
            "schema_version": "round_map_snapshot.v1",
            "filesystem_snapshot_sha256": filesystem_snapshot_sha256,
            "journal_snapshot_sha256": journal_snapshot_sha256,
            "journal_state": journal_state,
            "full_result_set_sha256": full_result_set_sha256,
            "filesystem_cross_file_atomic": False,
            "cross_source_atomic": False,
        },
        "items": page,
        "coverage": coverage,
        "limits": deepcopy(ROUND_MAP_LIMITS),
        "next_cursor": next_cursor,
    }
    _validate_result_invariants(result)
    return RoundMapComputation(result=result, workspace=workspace)


def record_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Return the exact bounded path-free raw-journal result projection."""
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


def record_provenance(result: dict[str, Any]) -> dict[str, Any]:
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
