# SPDX-License-Identifier: Apache-2.0
"""Bounded internal Stage 3C paragraph-history I/O envelope.

This module intentionally has no MCP registration or public-package export.
It captures one complete direct-folder DOCX candidate set, derives every fact
from those retained bytes, executes the exact adjacent resolver, and projects
one closed ``paragraph_history.v1`` page for later server integration.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from veqtor_docx._ooxml import (
    ArchiveValidationError,
    DocxError,
    ExpandedOutputBudget,
    ResourceLimitError,
    pending_text_revisions_rejected_text,
)
from veqtor_docx._projection import build_paragraph_projection_coverage_v1
from veqtor_docx.extract import _extract_from_bytes
from veqtor_docx.inspect import InspectError, _Snapshot, _load_snapshot_from_payload

from . import records
from ._history_resolution import (
    HistoryResolutionError,
    ParagraphHistoryCandidate,
    ParagraphHistoryNavigationCandidate,
    ParagraphHistoryObservation,
    ParagraphHistoryRelationship,
    ParagraphHistoryResolution,
    ParagraphHistorySeed,
    ParagraphHistorySelectedParagraph,
    ParagraphHistoryTrace,
    resolve_paragraph_history,
)
from .round_map import (
    RoundMapError,
    _CapturedDescriptor,
    _EnumeratedCandidate,
    _candidate_name,
    _canonical_seed_path,
    _enumerate_candidates,
    _inspection_coverage,
    _path_text,
    _read_candidate_with_descriptor,
    _validate_candidate_types,
    _validate_paragraph_ref,
)


HISTORY_LIMITS: dict[str, Any] = {
    "candidate_docx_files": 500,
    "candidate_compressed_input_bytes": 524_288_000,
    "candidate_expanded_bytes": 524_288_000,
    "compressed_bytes_per_docx": 52_428_800,
    "indexed_paragraphs_per_docx": 10_000,
    "indexed_paragraphs_per_folder": 100_000,
    "accepted_current_chars_per_paragraph": 50_000,
    "accepted_current_chars_per_docx": 2_000_000,
    "accepted_current_chars_per_folder": 20_000_000,
    "rejected_projection_chars_per_paragraph": 50_000,
    "rejected_projection_chars_per_docx": 50_000,
    "rejected_projection_chars_per_folder": 20_000_000,
    "decoded_text_chars_per_folder": 50_000_000,
    "exact_candidate_relationships": 50_000,
    "navigation_candidates": 10_000,
    "selected_change_units_per_result": 10_000,
    "change_units_per_selected_paragraph": 1_000,
    "change_unit_text_chars_per_selected_observation": 100_000,
    "change_unit_text_chars_per_result": 10_000_000,
    "sample_items": 20,
    "returned_verbatim_chars_per_observation": 200_000,
    "returned_verbatim_chars_per_page": 1_000_000,
    "default_page_items": 50,
    "maximum_page_items": 100,
    "revision_nesting_depth": 2,
    "journal_bytes": 67_108_864,
    "wall_clock_partial_results": False,
    "semantic_or_vector_search": False,
}

DEFAULT_MAX_ITEMS = 50
MAX_ITEMS = 100
_DOCUMENT_PART = "word/document.xml"
_CURSOR_RE = re.compile(r"^ph1:([1-9][0-9]{0,2}):([0-9a-f]{64})$")
_ORDER_INPUT_KEYS = frozenset({"schema_version", "kind"})
_EXPLICIT_ORDER_INPUT_KEYS = frozenset(
    {"schema_version", "kind", "ordered_filenames"}
)
_SEED_KEYS = frozenset({"schema_version", "path", "paragraph_ref"})
_POLICY = {
    "schema_version": "paragraph_history_projection_policy.v1",
    "search_scope": "word_document_xml_body_v1",
    "container_policy": "canonical_body_flow_v1",
    "current_reading_mode": "accepted_current_v1",
    "rejected_reading_mode": "pending_text_revisions_rejected_v1",
    "whitespace_policy": "xsd_whitespace_v1",
    "structural_availability_policy": "paragraph_structural_context_v1",
    "current_equality_basis": "exact_content_equality_basis.v1",
    "rejected_equality_basis": "rejected_projection_equality_basis.v1",
    "navigation_basis": "navigation_candidate_basis.v1",
    "trace_algorithm": "adjacent_backward_unique_v1",
    "result_order": "seed_then_descending_position_v1",
    "cursor_policy": "paragraph_history_cursor_ph1_v1",
    "move_visibility": "literal_wrapper_visibility_v1",
    "move_pairing": "not_attempted",
    "revision_nesting_depth": 2,
}
_XSD_WHITESPACE_V1 = frozenset("\t\n\r ")


class HistoryIOError(DocxError):
    """One sanitized refusal before an internal history result exists."""

    def __init__(self, code: str, detail: str, **metadata: object) -> None:
        self.code = code
        self.detail = detail
        self.metadata = metadata
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ParagraphHistoryComputation:
    """One validated internal page and its immutable capture authority."""

    result: dict[str, Any]
    workspace: Path
    workspace_identity: tuple[int, int]


@dataclass(frozen=True)
class _CapturedHistoryCandidate:
    filename: str
    path: str
    position: int
    payload: bytes
    descriptor: _CapturedDescriptor


@dataclass(frozen=True)
class _HistoryDocument:
    captured: _CapturedHistoryCandidate
    snapshot: _Snapshot
    document_id: str
    observation_id: str
    inspection_coverage: dict[str, Any]


def _digest(value: object) -> str:
    return records._stable_digest(value)


def _derived_id(prefix: str, identity: dict[str, Any]) -> str:
    return f"{prefix}:{_digest(identity)}"


def _document_id(file_sha256: str) -> str:
    return f"rm_doc_v1:{file_sha256}"


def _has_non_whitespace(value: str) -> bool:
    return any(character not in _XSD_WHITESPACE_V1 for character in value)


def _limit(
    observed: int,
    key: str,
    detail: str,
    *,
    unit: str = "count",
    observed_at_least: bool = False,
) -> None:
    allowed = HISTORY_LIMITS[key]
    if observed <= allowed:
        return
    suffix = "bytes" if unit == "bytes" else "chars" if unit == "chars" else "count"
    raise HistoryIOError(
        "resource_limit_exceeded",
        detail,
        limit=key,
        **{f"allowed_{suffix}": allowed, f"observed_{suffix}": observed},
        **({"observed_at_least": True} if observed_at_least else {}),
    )


def _translate_round_map_error(exc: RoundMapError) -> HistoryIOError:
    metadata = getattr(exc, "metadata", {})
    return HistoryIOError(
        exc.code,
        exc.detail,
        **(metadata if isinstance(metadata, dict) else {}),
    )


def _filename(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and os.path.basename(value) == value
        and (os.path.altsep is None or os.path.altsep not in value)
        and value.casefold().endswith(".docx")
    )


def _validate_inputs(
    folder: object,
    seed: object,
    order_basis: object,
    cursor: object,
    max_items: object,
) -> tuple[str, dict[str, Any], dict[str, Any], tuple[int, str] | None, int]:
    try:
        folder_text = _path_text(folder, code="invalid_request")
    except RoundMapError as exc:
        raise _translate_round_map_error(exc) from exc
    if not isinstance(seed, dict) or set(seed) != _SEED_KEYS:
        raise HistoryIOError("invalid_request", "seed fields are invalid")
    if seed.get("schema_version") != "paragraph_history_seed.v1":
        raise HistoryIOError("invalid_request", "seed schema_version is invalid")
    try:
        seed_path = _path_text(seed.get("path"), code="invalid_request")
        paragraph_ref = _validate_paragraph_ref(seed.get("paragraph_ref"))
    except RoundMapError as exc:
        raise _translate_round_map_error(exc) from exc

    if not isinstance(order_basis, dict):
        raise HistoryIOError("invalid_round_order", "order_basis must be an object")
    kind = order_basis.get("kind")
    expected_keys = (
        _ORDER_INPUT_KEYS
        if kind == "filename_lexicographic_v1"
        else _EXPLICIT_ORDER_INPUT_KEYS
    )
    if (
        set(order_basis) != expected_keys
        or order_basis.get("schema_version") != "paragraph_history_order.v1"
        or kind
        not in {"filename_lexicographic_v1", "explicit_filename_sequence_v1"}
    ):
        raise HistoryIOError("invalid_round_order", "order_basis is invalid")
    checked_order = deepcopy(order_basis)
    if kind == "explicit_filename_sequence_v1":
        filenames = checked_order.get("ordered_filenames")
        if not isinstance(filenames, list):
            raise HistoryIOError(
                "invalid_round_order", "ordered_filenames must be an array"
            )
        if len(filenames) > HISTORY_LIMITS["candidate_docx_files"]:
            raise HistoryIOError(
                "invalid_round_order", "ordered_filenames exceeds the manifest limit"
            )
        if any(not _filename(value) for value in filenames):
            raise HistoryIOError(
                "invalid_round_order", "ordered filename is not a direct DOCX name"
            )

    parsed_cursor: tuple[int, str] | None
    if cursor is None:
        parsed_cursor = None
    elif not isinstance(cursor, str) or (match := _CURSOR_RE.fullmatch(cursor)) is None:
        raise HistoryIOError("invalid_cursor", "cursor is not a valid ph1 cursor")
    else:
        offset = int(match.group(1))
        if not 1 <= offset <= 499:
            raise HistoryIOError("invalid_cursor", "cursor offset is outside ph1")
        parsed_cursor = (offset, match.group(2))

    if type(max_items) is not int or not 1 <= max_items <= MAX_ITEMS:
        raise HistoryIOError(
            "invalid_request", "max_items must be an integer from 1 through 100"
        )
    return (
        folder_text,
        {
            "schema_version": "paragraph_history_seed.v1",
            "path": seed_path,
            "paragraph_ref": paragraph_ref,
        },
        checked_order,
        parsed_cursor,
        max_items,
    )


def _effective_order(
    candidates: dict[str, _EnumeratedCandidate],
    order_basis: dict[str, Any],
) -> tuple[list[str], str, dict[str, Any]]:
    kind = order_basis["kind"]
    if kind == "filename_lexicographic_v1":
        filenames = sorted(candidates, key=lambda value: (value.casefold(), value))
        rule = "casefold_then_exact"
    else:
        filenames = list(order_basis["ordered_filenames"])
        if (
            len(filenames) != len(candidates)
            or len(filenames) != len(set(filenames))
            or set(filenames) != set(candidates)
        ):
            raise HistoryIOError(
                "invalid_round_order",
                "ordered_filenames must name every candidate DOCX exactly once",
            )
        rule = "exact_sequence"
    manifest = {
        "schema_version": "round_map_filename_manifest.v1",
        "ordering_source": kind,
        "filenames": filenames,
    }
    return (
        filenames,
        kind,
        {
            "schema_version": "paragraph_history_order_result.v1",
            "kind": kind,
            "rule": rule,
            "position_semantics": "declared_position_only",
            "chronology_verified": False,
            "filename_manifest_sha256": _digest(manifest),
        },
    )


def _capture_workspace(
    folder: str,
    seed_path: str,
    order_basis: dict[str, Any],
) -> tuple[
    Path,
    tuple[int, int],
    list[_CapturedHistoryCandidate],
    _CapturedHistoryCandidate,
    str,
    dict[str, Any],
]:
    lexical = Path(folder)
    if not lexical.is_absolute():
        lexical = Path.cwd() / lexical
    lexical = Path(os.path.abspath(lexical))
    try:
        initial = lexical.lstat()
    except FileNotFoundError as exc:
        raise HistoryIOError("workspace_missing", "workspace does not exist") from exc
    except OSError as exc:
        raise HistoryIOError(
            "workspace_unreadable", "workspace cannot be read"
        ) from exc
    if stat.S_ISLNK(initial.st_mode) or not stat.S_ISDIR(initial.st_mode):
        raise HistoryIOError(
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
            raise HistoryIOError(
                "workspace_changed", "workspace changed before open"
            ) from exc
        raise HistoryIOError(
            "workspace_unreadable", "workspace cannot be opened"
        ) from exc
    try:
        opened = os.fstat(root_fd)
        if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != identity:
            raise HistoryIOError("workspace_changed", "workspace identity changed")
        try:
            canonical = records._filesystem_spelled_workspace(root_fd, lexical, identity)
            candidates = _enumerate_candidates(root_fd)
            _validate_candidate_types(candidates)
        except RoundMapError as exc:
            raise _translate_round_map_error(exc) from exc
        filenames, ordering_source, returned_order = _effective_order(
            candidates, order_basis
        )
        canonical_seed = _canonical_seed_path(seed_path)
        candidate_paths = {str(canonical / filename) for filename in filenames}
        if canonical_seed not in candidate_paths:
            raise HistoryIOError(
                "seed_not_candidate", "seed path is not a direct candidate DOCX"
            )
        if not filenames or canonical_seed != str(canonical / filenames[-1]):
            raise HistoryIOError(
                "seed_not_last_declared_position",
                "seed must name the last declared position",
            )

        captured: list[_CapturedHistoryCandidate] = []
        total_bytes = 0
        for position, filename in enumerate(filenames):
            try:
                payload, descriptor = _read_candidate_with_descriptor(
                    root_fd, candidates[filename]
                )
            except RoundMapError as exc:
                raise _translate_round_map_error(exc) from exc
            total_bytes += len(payload)
            _limit(
                total_bytes,
                "candidate_compressed_input_bytes",
                "candidate DOCX files exceed aggregate compressed-byte limit",
                unit="bytes",
            )
            captured.append(
                _CapturedHistoryCandidate(
                    filename=filename,
                    path=str(canonical / filename),
                    position=position,
                    payload=payload,
                    descriptor=descriptor,
                )
            )

        final_names: set[str] = set()
        try:
            with os.scandir(root_fd) as entries:
                for entry in entries:
                    if not _candidate_name(entry.name):
                        continue
                    if len(final_names) >= HISTORY_LIMITS["candidate_docx_files"]:
                        raise HistoryIOError(
                            "workspace_changed",
                            "candidate filename set grew beyond its captured bound",
                        )
                    final_names.add(entry.name)
        except HistoryIOError:
            raise
        except OSError as exc:
            raise HistoryIOError(
                "workspace_changed", "candidate filename set cannot be rechecked"
            ) from exc
        if final_names != set(candidates):
            raise HistoryIOError("workspace_changed", "candidate filename set changed")
        for candidate in captured:
            try:
                current = os.stat(
                    candidate.filename,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise HistoryIOError(
                    "workspace_changed", "candidate changed after capture"
                ) from exc
            current_descriptor = (
                current.st_dev,
                current.st_ino,
                current.st_mode,
                current.st_nlink,
                current.st_size,
                current.st_mtime_ns,
                current.st_ctime_ns,
            )
            captured_descriptor = candidate.descriptor
            if current_descriptor != (
                captured_descriptor.device,
                captured_descriptor.inode,
                captured_descriptor.mode,
                captured_descriptor.link_count,
                captured_descriptor.size,
                captured_descriptor.mtime_ns,
                captured_descriptor.ctime_ns,
            ):
                raise HistoryIOError(
                    "workspace_changed", "candidate identity changed after capture"
                )
        try:
            final = lexical.lstat()
        except OSError as exc:
            raise HistoryIOError("workspace_changed", "workspace path changed") from exc
        if (
            not stat.S_ISDIR(final.st_mode)
            or (final.st_dev, final.st_ino) != identity
            or records._filesystem_spelled_workspace(root_fd, lexical, identity)
            != canonical
        ):
            raise HistoryIOError(
                "workspace_changed", "workspace identity or spelling changed"
            )
        captured_seed = captured[-1]
    finally:
        os.close(root_fd)
    return (
        canonical,
        identity,
        captured,
        captured_seed,
        ordering_source,
        returned_order,
    )


def _parse_candidates(
    captured: list[_CapturedHistoryCandidate],
) -> tuple[list[_HistoryDocument], int]:
    expanded_budget = ExpandedOutputBudget(
        allowed_bytes=HISTORY_LIMITS["candidate_expanded_bytes"],
        limit="candidate_expanded_bytes",
    )
    documents: list[_HistoryDocument] = []
    payload_by_digest: dict[str, bytes] = {}
    folder_paragraphs = 0
    folder_current_chars = 0
    for candidate in captured:
        try:
            snapshot = _load_snapshot_from_payload(
                candidate.payload,
                path=candidate.path,
                expanded_budget=expanded_budget,
                missing_document_part_code="missing_document_part",
                invalid_document_structure_code="file_unextractable",
                invalid_ooxml_value_code="file_unextractable",
            )
        except ResourceLimitError as exc:
            raise HistoryIOError(
                "resource_limit_exceeded",
                "candidate exceeds a processing limit",
                **getattr(exc, "metadata", {}),
            ) from exc
        except ArchiveValidationError as exc:
            raise HistoryIOError(
                exc.code,
                "candidate archive cannot be inspected",
                **getattr(exc, "metadata", {}),
            ) from exc
        except InspectError as exc:
            raise HistoryIOError(
                exc.code,
                "candidate DOCX cannot be inspected",
                **getattr(exc, "metadata", {}),
            ) from exc
        except DocxError as exc:
            raise HistoryIOError(
                "invalid_docx", "candidate DOCX cannot be inspected"
            ) from exc

        paragraph_count = len(snapshot.paragraphs)
        _limit(
            paragraph_count,
            "indexed_paragraphs_per_docx",
            "candidate exceeds the indexed paragraph limit",
        )
        folder_paragraphs += paragraph_count
        _limit(
            folder_paragraphs,
            "indexed_paragraphs_per_folder",
            "folder exceeds the indexed paragraph limit",
        )
        document_chars = 0
        for paragraph in snapshot.paragraphs:
            paragraph_chars = len(paragraph.text)
            _limit(
                paragraph_chars,
                "accepted_current_chars_per_paragraph",
                "paragraph exceeds the accepted-current character limit",
                unit="chars",
            )
            document_chars += paragraph_chars
        _limit(
            document_chars,
            "accepted_current_chars_per_docx",
            "candidate exceeds the accepted-current character limit",
            unit="chars",
        )
        folder_current_chars += document_chars
        _limit(
            folder_current_chars,
            "accepted_current_chars_per_folder",
            "folder exceeds the accepted-current character limit",
            unit="chars",
        )
        _limit(
            folder_current_chars,
            "decoded_text_chars_per_folder",
            "folder exceeds the decoded-text character limit",
            unit="chars",
        )

        previous_payload = payload_by_digest.setdefault(
            snapshot.file_sha256, candidate.payload
        )
        if previous_payload != candidate.payload:
            raise HistoryIOError(
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
        documents.append(
            _HistoryDocument(
                captured=candidate,
                snapshot=snapshot,
                document_id=document_id,
                observation_id=observation_id,
                inspection_coverage=_inspection_coverage(snapshot),
            )
        )
    return documents, folder_current_chars


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_thaw(item) for item in value]
    return value


def _relationship_result(
    relationship: ParagraphHistoryRelationship,
) -> dict[str, Any]:
    return {
        "schema_version": "paragraph_history_relationship.v1",
        "relationship_id": relationship.relationship_id,
        "relationship_type": relationship.relationship_type,
        "lower_position": relationship.lower_position,
        "higher_position": relationship.higher_position,
        "lower_observation_id": relationship.lower_observation_id,
        "higher_observation_id": relationship.higher_observation_id,
        "lower_paragraph_observation_id": (
            relationship.lower_paragraph_observation_id
        ),
        "higher_paragraph_observation_id": (
            relationship.higher_paragraph_observation_id
        ),
        "lower_paragraph_id": relationship.lower_paragraph_id,
        "higher_paragraph_id": relationship.higher_paragraph_id,
        "comparison_text_sha256": relationship.comparison_text_sha256,
        "basis": _thaw(relationship.basis),
        "derivation_recorded": relationship.derivation_recorded,
        "lineage_verified": relationship.lineage_verified,
        "chronology_verified": relationship.chronology_verified,
        "authorship_verified": relationship.authorship_verified,
        "time_verified": relationship.time_verified,
        "semantic_identity": relationship.semantic_identity,
    }


def _candidate_result(candidate: ParagraphHistoryCandidate) -> dict[str, Any]:
    if len(candidate.evidence_types) != 1 or len(candidate.relationships) != 1:
        raise HistoryIOError(
            "output_contract_error",
            "a history candidate must have exactly one evidence relationship",
        )
    relationship = candidate.relationships[0]
    if candidate.evidence_types[0] != relationship.relationship_type:
        raise HistoryIOError(
            "output_contract_error", "candidate evidence discriminator is inconsistent"
        )
    return {
        "paragraph_observation_id": candidate.paragraph_observation_id,
        "paragraph_id": candidate.paragraph_id,
        "paragraph_ref": _thaw(candidate.paragraph_ref),
        "evidence_type": candidate.evidence_types[0],
        "relationship": _relationship_result(relationship),
    }


def _candidate_summary(
    candidates: tuple[ParagraphHistoryCandidate, ...],
) -> dict[str, Any]:
    complete = [_candidate_result(candidate) for candidate in candidates]
    if complete != sorted(complete, key=lambda item: item["paragraph_observation_id"]):
        raise HistoryIOError(
            "output_contract_error", "history candidates are not canonically ordered"
        )
    sample = deepcopy(complete[: HISTORY_LIMITS["sample_items"]])
    return {
        "schema_version": "paragraph_history_candidate_summary.v1",
        "count": len(complete),
        "sha256": _digest(
            {
                "schema_version": "paragraph_history_candidates.v1",
                "candidates": complete,
            }
        ),
        "sample": sample,
        "truncated": len(complete) > len(sample),
    }


def _navigation_result(
    candidate: ParagraphHistoryNavigationCandidate,
) -> dict[str, Any]:
    return {
        "schema_version": "paragraph_history_navigation_candidate.v1",
        "navigation_candidate_id": candidate.navigation_candidate_id,
        "observation_id": candidate.observation_id,
        "seed_section_id": candidate.seed_section_id,
        "candidate_section_id": candidate.candidate_section_id,
        "section_ref": _thaw(candidate.section_ref),
        "label": candidate.label,
        "heading": candidate.heading,
        "level": candidate.level,
        "outline_basis": "word_outline_level_v1",
        "label_basis": candidate.label_basis,
        "evidence_basis": _thaw(candidate.evidence_basis),
    }


def _navigation_summary(
    candidates: tuple[ParagraphHistoryNavigationCandidate, ...],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    complete = [_navigation_result(candidate) for candidate in candidates]
    if complete != sorted(complete, key=lambda item: item["navigation_candidate_id"]):
        raise HistoryIOError(
            "output_contract_error", "navigation candidates are not canonically ordered"
        )
    sample = deepcopy(complete[: HISTORY_LIMITS["sample_items"]])
    return (
        {
            "schema_version": "paragraph_history_navigation_summary.v1",
            "count": len(complete),
            "sha256": _digest(
                {
                    "schema_version": "paragraph_history_navigation_candidates.v1",
                    "candidates": complete,
                }
            ),
            "sample": sample,
            "truncated": len(complete) > len(sample),
        },
        complete,
    )


def _change_unit_result(unit: dict[str, Any]) -> dict[str, Any]:
    context = unit.get("paragraph_context")
    reference = unit.get("reference")
    anchor = unit.get("anchor")
    clause_anchor = unit.get("clause_anchor")
    if (
        not isinstance(context, dict)
        or not isinstance(reference, dict)
        or not isinstance(anchor, dict)
        or (clause_anchor is not None and not isinstance(clause_anchor, dict))
    ):
        raise HistoryIOError(
            "output_contract_error", "selected change unit is malformed"
        )
    projected_clause = None
    if clause_anchor is not None:
        projected_clause = {
            "label": clause_anchor.get("label"),
            "heading": clause_anchor.get("heading"),
        }
    return {
        "schema_version": "paragraph_history_change_unit.v1",
        "change_unit_id": unit.get("change_unit_id"),
        "file_sha256": unit.get("file_sha256"),
        "change_type": unit.get("change_type"),
        "author": unit.get("author"),
        "date": unit.get("date"),
        "anchor": deepcopy(anchor),
        "clause_anchor": projected_clause,
        "paragraph_context": {
            "before": context.get("before"),
            "after": context.get("after"),
            "manual_label": context.get("manual_label"),
            "truncated_before": context.get("truncated_before"),
            "truncated_after": context.get("truncated_after"),
        },
        "old_text": unit.get("old_text"),
        "new_text": unit.get("new_text"),
        "reference": {
            "part_name": reference.get("part_name"),
            "paragraph_index": reference.get("paragraph_index"),
            "container_kind": reference.get("container_kind"),
            "group_index": reference.get("group_index"),
            "revision_ids": deepcopy(reference.get("revision_ids")),
        },
        "countered_by": deepcopy(unit.get("countered_by", [])),
    }


def _change_unit_numeric_id(unit: dict[str, Any]) -> int:
    value = unit.get("change_unit_id")
    if not isinstance(value, str) or not re.fullmatch(r"cu_[0-9]+", value):
        raise HistoryIOError(
            "output_contract_error", "selected change unit id is invalid"
        )
    return int(value.removeprefix("cu_"))


@dataclass
class _TextBudgets:
    accepted_current_chars: int
    rejected_projection_chars: int = 0
    change_unit_text_chars: int = 0
    selected_change_units: int = 0

    @property
    def decoded_text_chars(self) -> int:
        return (
            self.accepted_current_chars
            + self.rejected_projection_chars
            + self.change_unit_text_chars
        )


def _selected_paragraph_result(
    selected: ParagraphHistorySelectedParagraph,
    document: _HistoryDocument,
    budgets: _TextBudgets,
) -> dict[str, Any]:
    paragraph_index = selected.paragraph_ref["paragraph_index"]
    paragraph = document.snapshot.body_flow.paragraphs[paragraph_index].element
    projection = _thaw(selected.rejected_pending)
    if not isinstance(projection, dict):
        raise HistoryIOError(
            "output_contract_error", "selected projection is malformed"
        )
    rejected_decoded_chars = len(pending_text_revisions_rejected_text(paragraph))
    _limit(
        rejected_decoded_chars,
        "rejected_projection_chars_per_docx",
        "selected projection exceeds the per-DOCX character limit",
        unit="chars",
    )
    budgets.rejected_projection_chars += rejected_decoded_chars
    _limit(
        budgets.rejected_projection_chars,
        "rejected_projection_chars_per_folder",
        "selected projections exceed the folder character limit",
        unit="chars",
    )
    _limit(
        budgets.decoded_text_chars,
        "decoded_text_chars_per_folder",
        "folder exceeds the decoded-text character limit",
        unit="chars",
    )

    try:
        extraction = _extract_from_bytes(
            document.captured.payload,
            document.captured.path,
        )
    except ResourceLimitError as exc:
        raise HistoryIOError(
            "resource_limit_exceeded",
            "selected change units exceed a processing limit",
            **getattr(exc, "metadata", {}),
        ) from exc
    except ArchiveValidationError as exc:
        raise HistoryIOError(
            exc.code,
            "selected change units cannot be extracted",
            **getattr(exc, "metadata", {}),
        ) from exc
    except DocxError as exc:
        raise HistoryIOError(
            "file_unextractable", "selected change units cannot be extracted"
        ) from exc
    source_units = extraction.get("change_units")
    if not isinstance(source_units, list):
        raise HistoryIOError(
            "output_contract_error", "selected change-unit source is malformed"
        )
    units = [
        _change_unit_result(unit)
        for unit in source_units
        if isinstance(unit, dict)
        and isinstance(unit.get("reference"), dict)
        and unit["reference"].get("paragraph_index") == paragraph_index
    ]
    units.sort(key=_change_unit_numeric_id)
    _limit(
        len(units),
        "change_units_per_selected_paragraph",
        "selected paragraph exceeds the change-unit limit",
    )
    budgets.selected_change_units += len(units)
    _limit(
        budgets.selected_change_units,
        "selected_change_units_per_result",
        "result exceeds the selected change-unit limit",
    )
    observation_change_chars = sum(
        len(text)
        for unit in units
        for key in ("old_text", "new_text")
        if isinstance((text := unit[key]), str)
    )
    _limit(
        observation_change_chars,
        "change_unit_text_chars_per_selected_observation",
        "selected observation exceeds the change-unit text limit",
        unit="chars",
    )
    budgets.change_unit_text_chars += observation_change_chars
    _limit(
        budgets.change_unit_text_chars,
        "change_unit_text_chars_per_result",
        "result exceeds the change-unit text limit",
        unit="chars",
    )
    _limit(
        budgets.decoded_text_chars,
        "decoded_text_chars_per_folder",
        "folder exceeds the decoded-text character limit",
        unit="chars",
    )
    projection_coverage = build_paragraph_projection_coverage_v1(
        paragraph,
        document.snapshot.body_flow,
        projection_status=projection["projection_status"],
    )
    relationships = [
        _relationship_result(relationship)
        for relationship in selected.support_to_higher
    ]
    return {
        "schema_version": "paragraph_history_selected_paragraph.v1",
        "paragraph_observation_id": selected.paragraph_observation_id,
        "paragraph_id": selected.paragraph_id,
        "paragraph_ref": _thaw(selected.paragraph_ref),
        "current": {
            "schema_version": "paragraph_current_projection.v1",
            "mode": "accepted_current_v1",
            "text_sha256": selected.current_text_sha256,
            "text_length": len(selected.current_text),
            "has_non_whitespace": _has_non_whitespace(selected.current_text),
            "text": selected.current_text,
        },
        "rejected_pending": projection,
        "support_to_higher": relationships,
        "change_units": units,
        "change_units_sha256": _digest(
            {
                "schema_version": "paragraph_history_change_units.v1",
                "change_units": units,
            }
        ),
        "projection_coverage": projection_coverage,
        "metadata_assurance": {
            "author_metadata_interpretation": "unverified_document_string",
            "date_metadata_interpretation": "unverified_document_string",
            "authorship_verified": False,
            "time_verified": False,
        },
    }


def _resolution_result(
    resolution: ParagraphHistoryResolution,
    candidate_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": "paragraph_history_resolution.v1",
        "state": resolution.state,
        "reason": resolution.reason,
        "candidate_count": resolution.candidate_count,
        "candidate_set_sha256": candidate_sha256,
        "current_candidate_count": resolution.current_candidate_count,
        "rejected_candidate_count": resolution.rejected_candidate_count,
        "higher_rejected_projection_complete": (
            resolution.higher_rejected_projection_complete
        ),
        "propagation_permitted": resolution.propagation_permitted,
    }


def _observation_verbatim_chars(observation: dict[str, Any]) -> int:
    count = len(observation["path"]) + len(observation["filename"])
    count += sum(
        len(value)
        for value in observation["inspection_coverage"]["excluded_parts"]
        if isinstance(value, str)
    )
    selected = observation["selected_paragraph"]
    if isinstance(selected, dict):
        count += len(selected["current"]["text"])
        rejected_text = selected["rejected_pending"]["text"]
        if isinstance(rejected_text, str):
            count += len(rejected_text)
        for unit in selected["change_units"]:
            for key in ("author", "date", "old_text", "new_text"):
                value = unit[key]
                if isinstance(value, str):
                    count += len(value)
            clause = unit["clause_anchor"]
            if isinstance(clause, dict):
                for key in ("label", "heading"):
                    value = clause[key]
                    if isinstance(value, str):
                        count += len(value)
            context = unit["paragraph_context"]
            for key in ("before", "after", "manual_label"):
                value = context[key]
                if isinstance(value, str):
                    count += len(value)
            count += sum(len(value) for value in unit["reference"]["revision_ids"])
    for candidate in observation["navigation_candidates"]["sample"]:
        for key in ("label", "heading"):
            value = candidate[key]
            if isinstance(value, str):
                count += len(value)
    return count


def _materialize_observations(
    trace: ParagraphHistoryTrace,
    documents: list[_HistoryDocument],
    *,
    accepted_current_chars: int,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[int],
    _TextBudgets,
]:
    document_by_id = {document.observation_id: document for document in documents}
    budgets = _TextBudgets(accepted_current_chars=accepted_current_chars)
    observations: list[dict[str, Any]] = []
    all_navigation: list[dict[str, Any]] = []
    verbatim_counts: list[int] = []
    for step in trace.steps:
        document = document_by_id[step.observation_id]
        candidate_summary = _candidate_summary(step.candidates)
        navigation_summary, navigation_complete = _navigation_summary(
            step.navigation_candidates
        )
        all_navigation.extend(navigation_complete)
        selected = (
            None
            if step.selected_paragraph is None
            else _selected_paragraph_result(
                step.selected_paragraph,
                document,
                budgets,
            )
        )
        resolution = (
            None
            if step.resolution is None
            else _resolution_result(step.resolution, candidate_summary["sha256"])
        )
        observation = {
            "schema_version": "paragraph_history_observation.v1",
            "observation_id": document.observation_id,
            "document_id": document.document_id,
            "path": document.captured.path,
            "filename": document.captured.filename,
            "position": document.captured.position,
            "file_sha256": document.snapshot.file_sha256,
            "byte_length": len(document.captured.payload),
            "entry_role": step.entry_role,
            "selected_paragraph": selected,
            "resolution": resolution,
            "candidates": candidate_summary,
            "navigation_candidates": navigation_summary,
            "inspection_coverage": deepcopy(document.inspection_coverage),
        }
        verbatim = _observation_verbatim_chars(observation)
        _limit(
            verbatim,
            "returned_verbatim_chars_per_observation",
            "observation exceeds the returned-verbatim character limit",
            unit="chars",
        )
        observations.append(observation)
        verbatim_counts.append(verbatim)
    return observations, all_navigation, verbatim_counts, budgets


def _descriptor_result(descriptor: _CapturedDescriptor) -> dict[str, int]:
    return {
        "device": descriptor.device,
        "inode": descriptor.inode,
        "mode": descriptor.mode,
        "link_count": descriptor.link_count,
        "size": descriptor.size,
        "mtime_ns": descriptor.mtime_ns,
        "ctime_ns": descriptor.ctime_ns,
    }


def _snapshot_digests(
    workspace_identity: tuple[int, int],
    captured: list[_CapturedHistoryCandidate],
    seed_result: dict[str, Any],
    input_order: dict[str, Any],
    full_observations: list[dict[str, Any]],
) -> dict[str, Any]:
    filesystem_observations = [
        {
            "canonical_path": candidate.path,
            "filename": candidate.filename,
            "position": candidate.position,
            "byte_length": len(candidate.payload),
            "file_sha256": hashlib.sha256(candidate.payload).hexdigest(),
            "descriptor": _descriptor_result(candidate.descriptor),
        }
        for candidate in captured
    ]
    candidate_filenames = sorted(
        (candidate.filename for candidate in captured),
        key=lambda value: (value.casefold(), value),
    )
    full_result_set_sha256 = _digest(
        {
            "schema_version": "paragraph_history_result_set.v1",
            "result_order": "seed_then_descending_position_v1",
            "observations": full_observations,
        }
    )
    return {
        "schema_version": "paragraph_history_snapshot.v1",
        "filesystem_snapshot_sha256": _digest(
            {
                "schema_version": "paragraph_history_filesystem_snapshot.v1",
                "workspace_identity": {
                    "device": workspace_identity[0],
                    "inode": workspace_identity[1],
                },
                "observations": filesystem_observations,
            }
        ),
        "candidate_manifest_sha256": _digest(
            {
                "schema_version": "paragraph_history_candidate_manifest.v1",
                "filenames": candidate_filenames,
            }
        ),
        "seed_binding_sha256": _digest(seed_result),
        "order_binding_sha256": _digest(input_order),
        "projection_policy_sha256": _digest(_POLICY),
        "limits_sha256": _digest(HISTORY_LIMITS),
        "full_result_set_sha256": full_result_set_sha256,
        "result_order": "seed_then_descending_position_v1",
        "filesystem_cross_file_atomic": False,
    }


def _cursor_binding(snapshot: dict[str, Any], next_offset: int) -> str:
    return _digest(
        {
            "schema_version": "paragraph_history_cursor_binding.v1",
            "filesystem_snapshot_sha256": snapshot[
                "filesystem_snapshot_sha256"
            ],
            "seed_binding_sha256": snapshot["seed_binding_sha256"],
            "order_binding_sha256": snapshot["order_binding_sha256"],
            "projection_policy_sha256": snapshot["projection_policy_sha256"],
            "limits_sha256": snapshot["limits_sha256"],
            "full_result_set_sha256": snapshot["full_result_set_sha256"],
            "result_order": "seed_then_descending_position_v1",
            "next_offset": next_offset,
        }
    )


def _page(
    observations: list[dict[str, Any]],
    verbatim_counts: list[int],
    *,
    offset: int,
    max_items: int,
    snapshot: dict[str, Any],
) -> tuple[list[dict[str, Any]], int, str | None]:
    page: list[dict[str, Any]] = []
    page_chars = 0
    position = offset
    while position < len(observations) and len(page) < max_items:
        item_chars = verbatim_counts[position]
        if page and page_chars + item_chars > HISTORY_LIMITS[
            "returned_verbatim_chars_per_page"
        ]:
            break
        if not page:
            _limit(
                page_chars + item_chars,
                "returned_verbatim_chars_per_page",
                "one observation exceeds the page returned-verbatim limit",
                unit="chars",
            )
        page.append(deepcopy(observations[position]))
        page_chars += item_chars
        position += 1
    next_cursor = (
        None
        if position == len(observations)
        else f"ph1:{position}:{_cursor_binding(snapshot, position)}"
    )
    return page, page_chars, next_cursor


def _coverage(
    trace: ParagraphHistoryTrace,
    *,
    document_count: int,
    page_count: int,
    cursor_offset: int,
    next_cursor: str | None,
    page_verbatim_chars: int,
    all_navigation: list[dict[str, Any]],
) -> dict[str, Any]:
    selected = [step.selected_paragraph for step in trace.steps if step.selected_paragraph]
    resolutions = [step.resolution for step in trace.steps if step.resolution]
    relationship_counts = {
        "exact_content_equality": 0,
        "rejected_projection_equality": 0,
    }
    for step in trace.steps:
        for candidate in step.candidates:
            for relationship in candidate.relationships:
                relationship_counts[relationship.relationship_type] += 1
    resolution_counts = {
        state: sum(resolution.state == state for resolution in resolutions)
        for state in ("exact_unique", "ambiguous", "unresolved")
    }
    projection_status_counts = {
        status: sum(
            item.rejected_pending["projection_status"] == status for item in selected
        )
        for status in ("complete", "unavailable")
    }
    projection_text_state_counts = {
        state: sum(item.rejected_pending["text_state"] == state for item in selected)
        for state in ("empty", "nonempty")
    }
    ordered_navigation = sorted(
        all_navigation,
        key=lambda item: (item["observation_id"], item["navigation_candidate_id"]),
    )
    return {
        "scan_complete": True,
        "candidate_document_count": document_count,
        "inspected_document_count": document_count,
        "eligible_observation_count": document_count,
        "returned_observation_count": page_count,
        "cursor_offset": cursor_offset,
        "output_truncated": next_cursor is not None,
        "seed_entry_count": 1,
        "selected_paragraph_count": len(selected),
        "resolution_counts": resolution_counts,
        "blocked_observation_count": sum(
            resolution.reason
            in {"blocked_by_higher_ambiguity", "blocked_by_higher_unresolved"}
            for resolution in resolutions
        ),
        "relationship_counts": relationship_counts,
        "selected_relationship_count": resolution_counts["exact_unique"],
        "evaluated_rejected_projection_count": len(selected),
        "projection_status_counts": projection_status_counts,
        "projection_text_state_counts": projection_text_state_counts,
        "projection_equals_current_count": sum(
            item.rejected_pending["equals_current"] is True for item in selected
        ),
        "navigation_candidate_count": len(ordered_navigation),
        "navigation_candidate_set_sha256": _digest(
            {
                "schema_version": "paragraph_history_all_navigation_candidates.v1",
                "candidates": ordered_navigation,
            }
        ),
        "returned_verbatim_char_count": page_verbatim_chars,
        "search_scope": "word_document_xml_body_v1",
        "current_reading_mode": "accepted_current_v1",
        "rejected_reading_mode": "pending_text_revisions_rejected_v1",
        "container_policy": "canonical_body_flow_v1",
        "whole_docx_coverage": False,
        "negative_whole_doc_claims": False,
        "chronology_verified": False,
        "semantic_identity_verified": False,
        "filesystem_cross_file_atomic": False,
    }


def _raise_resolution_error(exc: BaseException) -> HistoryIOError:
    if isinstance(exc, ResourceLimitError):
        return HistoryIOError(
            "resource_limit_exceeded",
            "history projection exceeds a processing limit",
            **getattr(exc, "metadata", {}),
        )
    if isinstance(exc, ArchiveValidationError):
        return HistoryIOError(
            exc.code,
            "history projection cannot be extracted",
            **getattr(exc, "metadata", {}),
        )
    if isinstance(exc, InspectError):
        code = exc.code
        if code == "reference_not_found":
            code = "reference_mismatch"
        return HistoryIOError(
            code,
            "seed paragraph reference cannot be resolved",
            **getattr(exc, "metadata", {}),
        )
    if isinstance(exc, HistoryResolutionError):
        code = exc.code
        if code == "snapshot_integrity_error":
            code = "evidence_consistency_error"
        elif code == "projection_contract_error":
            code = "output_contract_error"
        return HistoryIOError(code, exc.detail, **getattr(exc, "metadata", {}))
    return HistoryIOError("internal_error", "paragraph history computation failed")


def _validate_result(
    result: dict[str, Any],
    *,
    full_observations: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> None:
    expected_top = {
        "schema_version",
        "status",
        "seed",
        "ordering_source",
        "order_basis",
        "result_order",
        "snapshot",
        "observations",
        "coverage",
        "limits",
        "next_cursor",
    }
    if set(result) != expected_top:
        raise HistoryIOError("output_contract_error", "result fields are not closed")
    if (
        result["schema_version"] != "paragraph_history.v1"
        or result["status"] != "ok"
        or result["result_order"] != "seed_then_descending_position_v1"
        or result["snapshot"] != snapshot
        or result["limits"] != HISTORY_LIMITS
    ):
        raise HistoryIOError("output_contract_error", "result identity is inconsistent")
    expected_full_digest = _digest(
        {
            "schema_version": "paragraph_history_result_set.v1",
            "result_order": "seed_then_descending_position_v1",
            "observations": full_observations,
        }
    )
    if snapshot["full_result_set_sha256"] != expected_full_digest:
        raise HistoryIOError(
            "output_contract_error", "complete result-set digest is inconsistent"
        )
    page = result["observations"]
    coverage = result["coverage"]
    if (
        not isinstance(page, list)
        or coverage["returned_observation_count"] != len(page)
        or coverage["eligible_observation_count"] != len(full_observations)
        or coverage["cursor_offset"] + len(page) > len(full_observations)
        or page
        != full_observations[
            coverage["cursor_offset"] : coverage["cursor_offset"] + len(page)
        ]
        or coverage["output_truncated"] != (result["next_cursor"] is not None)
    ):
        raise HistoryIOError(
            "output_contract_error", "result pagination is inconsistent"
        )
    positions = [observation["position"] for observation in full_observations]
    if positions != list(range(len(full_observations) - 1, -1, -1)):
        raise HistoryIOError(
            "output_contract_error", "result observation order is inconsistent"
        )
    if sum(observation["entry_role"] == "seed" for observation in full_observations) != 1:
        raise HistoryIOError("output_contract_error", "result seed count is invalid")
    for observation in full_observations:
        selected = observation["selected_paragraph"]
        if selected is None:
            continue
        if (
            selected["current"]["text_sha256"]
            != selected["paragraph_ref"]["paragraph_text_sha256"]
            or selected["current"]["text_length"]
            != len(selected["current"]["text"])
            or selected["change_units_sha256"]
            != _digest(
                {
                    "schema_version": "paragraph_history_change_units.v1",
                    "change_units": selected["change_units"],
                }
            )
            or any("path" in unit["reference"] for unit in selected["change_units"])
            or selected["metadata_assurance"]["authorship_verified"] is not False
            or selected["metadata_assurance"]["time_verified"] is not False
        ):
            raise HistoryIOError(
                "output_contract_error", "selected paragraph is inconsistent"
            )


def _build_paragraph_history(
    folder: object,
    seed: object,
    order_basis: object,
    *,
    cursor: object = None,
    max_items: object = DEFAULT_MAX_ITEMS,
) -> ParagraphHistoryComputation:
    """Build one closed internal page without journal or MCP integration."""
    (
        folder_text,
        checked_seed,
        checked_order,
        parsed_cursor,
        checked_max_items,
    ) = _validate_inputs(folder, seed, order_basis, cursor, max_items)
    (
        workspace,
        workspace_identity,
        captured,
        captured_seed,
        ordering_source,
        returned_order,
    ) = _capture_workspace(folder_text, checked_seed["path"], checked_order)
    documents, accepted_current_chars = _parse_candidates(captured)

    if checked_seed["paragraph_ref"]["file_sha256"] != hashlib.sha256(
        captured_seed.payload
    ).hexdigest():
        raise HistoryIOError(
            "file_sha256_mismatch",
            "seed reference was produced from different DOCX bytes",
            claimed_source_sha256=checked_seed["paragraph_ref"]["file_sha256"],
            observed_source_sha256=hashlib.sha256(captured_seed.payload).hexdigest(),
        )
    resolver_observations = [
        ParagraphHistoryObservation(
            observation_id=document.observation_id,
            snapshot=document.snapshot,
        )
        for document in documents
    ]
    resolver_seed = ParagraphHistorySeed(
        observation_id=documents[-1].observation_id,
        paragraph_ref=checked_seed["paragraph_ref"],
    )
    try:
        trace = resolve_paragraph_history(resolver_observations, resolver_seed)
    except (
        HistoryResolutionError,
        InspectError,
        ResourceLimitError,
        ArchiveValidationError,
    ) as exc:
        raise _raise_resolution_error(exc) from exc

    seed_selected = trace.steps[0].selected_paragraph
    if seed_selected is None:
        raise HistoryIOError("output_contract_error", "seed paragraph is absent")
    seed_result = {
        "schema_version": "paragraph_history_seed_result.v1",
        "document_id": documents[-1].document_id,
        "observation_id": documents[-1].observation_id,
        "paragraph_id": seed_selected.paragraph_id,
        "paragraph_observation_id": seed_selected.paragraph_observation_id,
        "position": documents[-1].captured.position,
        "paragraph_ref": _thaw(seed_selected.paragraph_ref),
    }
    full_observations, all_navigation, verbatim_counts, _ = (
        _materialize_observations(
            trace,
            documents,
            accepted_current_chars=accepted_current_chars,
        )
    )
    snapshot = _snapshot_digests(
        workspace_identity,
        captured,
        seed_result,
        checked_order,
        full_observations,
    )
    offset = 0 if parsed_cursor is None else parsed_cursor[0]
    if parsed_cursor is not None:
        supplied_binding = parsed_cursor[1]
        expected_binding = _cursor_binding(snapshot, offset)
        if supplied_binding != expected_binding:
            raise HistoryIOError(
                "cursor_mismatch", "cursor does not bind this paragraph history"
            )
        if not 1 <= offset < len(full_observations):
            raise HistoryIOError(
                "invalid_cursor", "cursor offset is outside the result set"
            )
    page, page_chars, next_cursor = _page(
        full_observations,
        verbatim_counts,
        offset=offset,
        max_items=checked_max_items,
        snapshot=snapshot,
    )
    coverage = _coverage(
        trace,
        document_count=len(documents),
        page_count=len(page),
        cursor_offset=offset,
        next_cursor=next_cursor,
        page_verbatim_chars=page_chars,
        all_navigation=all_navigation,
    )
    result = {
        "schema_version": "paragraph_history.v1",
        "status": "ok",
        "seed": seed_result,
        "ordering_source": ordering_source,
        "order_basis": returned_order,
        "result_order": "seed_then_descending_position_v1",
        "snapshot": snapshot,
        "observations": page,
        "coverage": coverage,
        "limits": deepcopy(HISTORY_LIMITS),
        "next_cursor": next_cursor,
    }
    _validate_result(
        result,
        full_observations=full_observations,
        snapshot=snapshot,
    )
    return ParagraphHistoryComputation(
        result=result,
        workspace=workspace,
        workspace_identity=workspace_identity,
    )


def build_paragraph_history(
    folder: object,
    seed: object,
    order_basis: object,
    *,
    cursor: object = None,
    max_items: object = DEFAULT_MAX_ITEMS,
) -> ParagraphHistoryComputation:
    """Normalize every pre-result failure into the closed internal domain."""
    try:
        return _build_paragraph_history(
            folder,
            seed,
            order_basis,
            cursor=cursor,
            max_items=max_items,
        )
    except HistoryIOError:
        raise
    except ResourceLimitError as exc:
        raise HistoryIOError(
            "resource_limit_exceeded",
            "paragraph history exceeds a processing limit",
            **getattr(exc, "metadata", {}),
        ) from None
    except ArchiveValidationError as exc:
        raise HistoryIOError(
            exc.code,
            "paragraph history cannot be extracted",
            **getattr(exc, "metadata", {}),
        ) from None
    except InspectError as exc:
        raise HistoryIOError(exc.code, "paragraph history cannot be inspected") from None
    except DocxError:
        raise HistoryIOError("invalid_docx", "paragraph history cannot be read") from None
    except Exception:
        raise HistoryIOError(
            "internal_error", "paragraph history computation failed"
        ) from None
