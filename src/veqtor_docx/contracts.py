# SPDX-License-Identifier: Apache-2.0
"""Versioned output domains shared by DOCX producers and history readers.

The v1 sets are append-only compatibility contracts. Producers use named
values from this module, and decision-record projection accepts the complete
historical set. Removing or renaming a v1 value requires a new schema version.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

DOCUMENT_PART_V1 = "word/document.xml"
RESULT_STATUS_OK = "ok"
RESULT_STATUS_ERROR = "error"
REVISION_COUNT_BASIS_V1 = "word_document_xml_w_ins_w_del_elements_v1"
REVISION_COUNT_BASES_V1 = frozenset({REVISION_COUNT_BASIS_V1})

TEXT_REVISION_SUFFIX_BY_NAME_V1 = MappingProxyType({"ins": "Ins", "del": "Del"})
TEXT_REVISION_NAMES_V1 = frozenset(TEXT_REVISION_SUFFIX_BY_NAME_V1)
MOVE_REVISION_NAMES_V1 = frozenset({"moveFrom", "moveTo"})
UNSUPPORTED_REVISION_NAMES_V1 = frozenset(
    {
        "rPrChange",
        "pPrChange",
        "tblPrChange",
        "trPrChange",
        "tcPrChange",
        "sectPrChange",
        "numberingChange",
        "cellIns",
        "cellDel",
    }
)
STRUCTURAL_REVISION_PARENT_NAMES_V1 = frozenset({"trPr", "tcPr", "tblPr", "sectPr"})
STRUCTURAL_REVISION_SUFFIXES_V1 = frozenset(TEXT_REVISION_SUFFIX_BY_NAME_V1.values())
EXTRACT_REVISION_CATEGORIES_V1 = frozenset(
    UNSUPPORTED_REVISION_NAMES_V1
    | MOVE_REVISION_NAMES_V1
    | {
        f"{parent}{suffix}"
        for parent in STRUCTURAL_REVISION_PARENT_NAMES_V1
        for suffix in STRUCTURAL_REVISION_SUFFIXES_V1
    }
    | {f"paragraphMark{suffix}" for suffix in STRUCTURAL_REVISION_SUFFIXES_V1}
)

VERIFY_VERDICT_EXACT = "exact"
VERIFY_VERDICT_NORMALIZED = "normalized"
VERIFY_VERDICT_NOT_FOUND = "not_found"
VERIFY_VERDICTS_V1 = frozenset(
    {
        VERIFY_VERDICT_EXACT,
        VERIFY_VERDICT_NORMALIZED,
        VERIFY_VERDICT_NOT_FOUND,
    }
)
MATCH_SIDE_NEW = "new"
MATCH_SIDE_OLD = "old"
MATCH_SIDES_V1 = frozenset({MATCH_SIDE_NEW, MATCH_SIDE_OLD})

INSPECT_MODE_OUTLINE = "outline"
INSPECT_MODE_LITERAL_SEARCH = "literal_search"
INSPECT_MODE_BROWSE = "browse"
INSPECT_MODE_READ = "read"
INSPECT_MODES_V1 = frozenset(
    {
        INSPECT_MODE_OUTLINE,
        INSPECT_MODE_LITERAL_SEARCH,
        INSPECT_MODE_BROWSE,
        INSPECT_MODE_READ,
    }
)
INSPECT_MATCH_EXACT_LITERAL = "exact_literal"
INSPECT_MATCH_NORMALIZED_LITERAL = "normalized_literal"
INSPECT_MATCH_NORMALIZED_CASEFOLD_LITERAL = "normalized_casefold_literal"
INSPECT_MATCH_BASES_V1 = frozenset(
    {
        INSPECT_MATCH_EXACT_LITERAL,
        INSPECT_MATCH_NORMALIZED_LITERAL,
        INSPECT_MATCH_NORMALIZED_CASEFOLD_LITERAL,
    }
)
INSPECT_SEARCH_SCOPE_V1 = "word_document_xml_body_v1"
INSPECT_READING_MODE_V1 = "accepted_current_v1"
INSPECT_CONTAINER_POLICY_V1 = "canonical_body_flow_v1"
INSPECT_DEFAULT_MAX_ITEMS_V1 = 50
INSPECT_FIXED_LIMITS_V1 = MappingProxyType(
    {
        "max_items": 100,
        "max_phrases": 20,
        "max_phrase_chars": 2_000,
        "max_total_phrase_chars": 10_000,
        "max_paragraph_text_chars": 50_000,
        "max_returned_text_chars": 100_000,
        "max_indexed_paragraphs": 10_000,
        "max_aggregate_text_chars": 2_000_000,
        "max_literal_match_candidates": 10_000,
        "max_literal_occurrences_per_candidate": 10_000,
        "wall_clock_partial_results": False,
    }
)
INSPECT_INCLUDED_PARTS_V1 = (DOCUMENT_PART_V1,)
INSPECT_FIXED_EXCLUDED_PARTS_V1 = (
    "word/header*.xml",
    "word/footer*.xml",
    "word/footnotes.xml",
    "word/endnotes.xml",
    "word/comments*.xml",
)
INSPECT_INCLUDED_CONTAINERS_V1 = ("body", "table_cell")

INSPECTION_CRITICAL_CHECK_IDS_V1 = frozenset(
    {
        "constants.policy-scope-limits-v1",
        "payload.collection-count-v1",
        "page.mode-coverage-v1",
        "page.pagination-v1",
        "page.literal-binding-v1",
        "snapshot.indexed-container-v1",
        "snapshot.container-exclusions-v1",
        "snapshot.revision-partitions-v1",
        "snapshot.revision-flags-v1",
        "snapshot.revision-global-v1",
        "payload.returned-text-caps-v1",
        "snapshot.excluded-scope-v1",
        "payload.returned-revision-warning-v1",
    }
)

_INSPECT_RESULT_CURSOR_RE = re.compile(r"^c1:([0-9]{1,10}):[0-9a-f]{64}$")


class InspectionContractError(ValueError):
    """A precise internal failure for one live inspection invariant."""

    def __init__(self, claim_id: str, path: str) -> None:
        super().__init__(f"inspection contract invariant failed [{claim_id}]: {path}")
        self.claim_id = claim_id
        self.path = path


def normalized_internal_package_part_name(value: object) -> str | None:
    """Validate one normalized, package-relative internal OPC part name.

    Relationship targets are URI-decoded and normalized by their caller before
    reaching this boundary. Live coverage and historical projection share this
    domain: no host paths, traversal or control characters, without imposing a
    narrower display-oriented length or punctuation policy.
    """
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
    ):
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    segments = value.split("/")
    if any(
        not segment
        or segment in {".", ".."}
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in segment)
        for segment in segments
    ):
        return None
    return value


def inspection_excluded_parts_v1(value: object) -> tuple[str, ...] | None:
    """Return the exact fixed prefix and safe sorted dynamic altChunk tail."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    parts = tuple(value)
    fixed_count = len(INSPECT_FIXED_EXCLUDED_PARTS_V1)
    if parts[:fixed_count] != INSPECT_FIXED_EXCLUDED_PARTS_V1:
        return None
    dynamic = parts[fixed_count:]
    if any(
        normalized_internal_package_part_name(part_name) != part_name
        for part_name in dynamic
    ) or dynamic != tuple(sorted(set(dynamic))):
        return None
    return parts


def inspection_returned_document_text_values(value: object) -> tuple[str, ...]:
    """Return every document-derived text/heading/label in one public value."""
    values: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"text", "heading", "label"} and isinstance(item, str):
                values.append(item)
            elif isinstance(item, Mapping) or (
                isinstance(item, Sequence) and not isinstance(item, (str, bytes))
            ):
                values.extend(inspection_returned_document_text_values(item))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            values.extend(inspection_returned_document_text_values(item))
    return tuple(values)


def _freeze_inspection_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_inspection_value(item) for key, item in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze_inspection_value(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class InspectionValidationContext:
    """One deeply immutable result view for bounded runtime checks."""

    result: Mapping[str, Any]


InspectionEvaluator = Callable[[InspectionValidationContext, str], None]


@dataclass(frozen=True, slots=True)
class InspectionCriticalCheck:
    """One explicitly retained consistency or safety check."""

    check_id: str
    evaluator: InspectionEvaluator


def _inspection_failed(claim_id: str, path: str) -> None:
    raise InspectionContractError(claim_id, path)


def _inspection_mapping(value: object, claim_id: str, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _inspection_failed(claim_id, path)
    return value


def _inspection_nonnegative_int(value: object, claim_id: str, path: str) -> int:
    if type(value) is not int or value < 0:
        _inspection_failed(claim_id, path)
    return value


def _inspection_count_mapping(
    value: object,
    claim_id: str,
    path: str,
) -> dict[str, int]:
    mapping = _inspection_mapping(value, claim_id, path)
    result: dict[str, int] = {}
    for key, item in mapping.items():
        if not isinstance(key, str):
            _inspection_failed(claim_id, path)
        result[key] = _inspection_nonnegative_int(item, claim_id, f"{path}.{key}")
    return result


def _inspection_mode_collection(
    result: Mapping[str, Any], claim_id: str
) -> Sequence[Any]:
    mode = result.get("mode")
    if mode == INSPECT_MODE_OUTLINE:
        collection = result.get("sections")
    elif mode == INSPECT_MODE_LITERAL_SEARCH:
        collection = result.get("matches")
    elif mode == INSPECT_MODE_BROWSE:
        collection = result.get("paragraphs")
    elif mode == INSPECT_MODE_READ:
        selection_kind = result.get("selection_kind")
        if selection_kind not in {"paragraph", "section"}:
            _inspection_failed(claim_id, "selection_kind")
        collection = result.get("paragraphs")
    else:
        _inspection_failed(claim_id, "mode")
    if isinstance(collection, (str, bytes)) or not isinstance(collection, Sequence):
        _inspection_failed(claim_id, "mode_collection")
    return collection


def _evaluate_constants(context: InspectionValidationContext, claim_id: str) -> None:
    result = context.result
    coverage = _inspection_mapping(result.get("coverage"), claim_id, "coverage")
    limits = _inspection_mapping(result.get("limits"), claim_id, "limits")
    expected_top_level = {
        "part_name": DOCUMENT_PART_V1,
        "search_scope": INSPECT_SEARCH_SCOPE_V1,
        "reading_mode": INSPECT_READING_MODE_V1,
        "container_policy": INSPECT_CONTAINER_POLICY_V1,
    }
    for key, expected_value in expected_top_level.items():
        if result.get(key) != expected_value:
            _inspection_failed(claim_id, key)
    if coverage.get("scan_complete") is not True:
        _inspection_failed(claim_id, "coverage.scan_complete")
    if coverage.get("included_parts") != INSPECT_INCLUDED_PARTS_V1:
        _inspection_failed(claim_id, "coverage.included_parts")
    if coverage.get("included_containers") != INSPECT_INCLUDED_CONTAINERS_V1:
        _inspection_failed(claim_id, "coverage.included_containers")
    requested = _inspection_nonnegative_int(
        limits.get("requested_max_items"), claim_id, "limits.requested_max_items"
    )
    if not 1 <= requested <= INSPECT_FIXED_LIMITS_V1["max_items"]:
        _inspection_failed(claim_id, "limits.requested_max_items")
    for key, expected_value in INSPECT_FIXED_LIMITS_V1.items():
        actual = limits.get(key)
        if type(actual) is not type(expected_value) or actual != expected_value:
            _inspection_failed(claim_id, f"limits.{key}")
    indexed = _inspection_nonnegative_int(
        coverage.get("indexed_paragraph_count"),
        claim_id,
        "coverage.indexed_paragraph_count",
    )
    if indexed > limits["max_indexed_paragraphs"]:
        _inspection_failed(claim_id, "coverage.indexed_paragraph_count")


def _evaluate_collection_count(
    context: InspectionValidationContext, claim_id: str
) -> None:
    result = context.result
    collection = _inspection_mode_collection(result, claim_id)
    coverage = _inspection_mapping(result.get("coverage"), claim_id, "coverage")
    returned = _inspection_nonnegative_int(
        coverage.get("returned_item_count"),
        claim_id,
        "coverage.returned_item_count",
    )
    if returned != len(collection):
        _inspection_failed(claim_id, "coverage.returned_item_count")


def _evaluate_mode_coverage(
    context: InspectionValidationContext, claim_id: str
) -> None:
    result = context.result
    coverage = _inspection_mapping(result.get("coverage"), claim_id, "coverage")
    indexed = _inspection_nonnegative_int(
        coverage.get("indexed_paragraph_count"),
        claim_id,
        "coverage.indexed_paragraph_count",
    )
    nonempty = _inspection_nonnegative_int(
        coverage.get("nonempty_indexed_paragraph_count"),
        claim_id,
        "coverage.nonempty_indexed_paragraph_count",
    )
    eligible = _inspection_nonnegative_int(
        coverage.get("eligible_item_count"),
        claim_id,
        "coverage.eligible_item_count",
    )
    returned = _inspection_nonnegative_int(
        coverage.get("returned_item_count"),
        claim_id,
        "coverage.returned_item_count",
    )
    mode = result.get("mode")
    if mode == INSPECT_MODE_OUTLINE and eligible > indexed:
        _inspection_failed(claim_id, "coverage.eligible_item_count")
    elif mode == INSPECT_MODE_BROWSE and eligible != nonempty:
        _inspection_failed(claim_id, "coverage.eligible_item_count")
    elif mode == INSPECT_MODE_READ:
        if result.get("selection_kind") == "paragraph":
            if eligible != 1 or returned != 1:
                _inspection_failed(claim_id, "coverage.eligible_item_count")
        elif eligible > nonempty:
            _inspection_failed(claim_id, "coverage.eligible_item_count")


def _evaluate_pagination(context: InspectionValidationContext, claim_id: str) -> None:
    result = context.result
    coverage = _inspection_mapping(result.get("coverage"), claim_id, "coverage")
    limits = _inspection_mapping(result.get("limits"), claim_id, "limits")
    returned = _inspection_nonnegative_int(
        coverage.get("returned_item_count"),
        claim_id,
        "coverage.returned_item_count",
    )
    eligible = _inspection_nonnegative_int(
        coverage.get("eligible_item_count"),
        claim_id,
        "coverage.eligible_item_count",
    )
    offset = _inspection_nonnegative_int(
        coverage.get("cursor_offset"), claim_id, "coverage.cursor_offset"
    )
    requested = _inspection_nonnegative_int(
        limits.get("requested_max_items"), claim_id, "limits.requested_max_items"
    )
    if returned > requested:
        _inspection_failed(claim_id, "coverage.returned_item_count")
    page_end = offset + returned
    if page_end > eligible:
        _inspection_failed(claim_id, "coverage.eligible_item_count")
    truncated = coverage.get("output_truncated")
    if type(truncated) is not bool:
        _inspection_failed(claim_id, "coverage.output_truncated")
    next_cursor = result.get("next_cursor")
    if truncated is not (next_cursor is not None):
        _inspection_failed(claim_id, "coverage.output_truncated")
    if truncated is not (page_end < eligible):
        _inspection_failed(claim_id, "coverage.output_truncated")
    if truncated and returned == 0:
        _inspection_failed(claim_id, "coverage.returned_item_count")
    if next_cursor is not None:
        if not isinstance(next_cursor, str):
            _inspection_failed(claim_id, "next_cursor")
        match = _INSPECT_RESULT_CURSOR_RE.fullmatch(next_cursor)
        if match is None or int(match.group(1)) != page_end:
            _inspection_failed(claim_id, "next_cursor")


def _evaluate_literal_binding(
    context: InspectionValidationContext, claim_id: str
) -> None:
    result = context.result
    coverage = _inspection_mapping(result.get("coverage"), claim_id, "coverage")
    complete = coverage.get("complete_literal_match_count")
    if result.get("mode") != INSPECT_MODE_LITERAL_SEARCH:
        if complete is not None:
            _inspection_failed(claim_id, "coverage.complete_literal_match_count")
        return
    eligible = _inspection_nonnegative_int(
        coverage.get("eligible_item_count"),
        claim_id,
        "coverage.eligible_item_count",
    )
    complete = _inspection_nonnegative_int(
        complete, claim_id, "coverage.complete_literal_match_count"
    )
    if complete != eligible:
        _inspection_failed(claim_id, "coverage.complete_literal_match_count")
    if eligible > INSPECT_FIXED_LIMITS_V1["max_literal_match_candidates"]:
        _inspection_failed(claim_id, "coverage.eligible_item_count")
    match_basis = result.get("match_basis")
    if match_basis not in INSPECT_MATCH_BASES_V1:
        _inspection_failed(claim_id, "match_basis")
    phrase_count = _inspection_nonnegative_int(
        result.get("phrase_count"), claim_id, "phrase_count"
    )
    if not 1 <= phrase_count <= INSPECT_FIXED_LIMITS_V1["max_phrases"]:
        _inspection_failed(claim_id, "phrase_count")
    for match in _inspection_mode_collection(result, claim_id):
        match = _inspection_mapping(match, claim_id, "matches")
        if match.get("match_basis") != match_basis:
            _inspection_failed(claim_id, "matches.match_basis")
        phrase_index = _inspection_nonnegative_int(
            match.get("phrase_index"), claim_id, "matches.phrase_index"
        )
        if phrase_index >= phrase_count:
            _inspection_failed(claim_id, "matches.phrase_index")
        occurrence_count = _inspection_nonnegative_int(
            match.get("occurrence_count"), claim_id, "matches.occurrence_count"
        )
        if not (
            1
            <= occurrence_count
            <= INSPECT_FIXED_LIMITS_V1["max_literal_occurrences_per_candidate"]
        ):
            _inspection_failed(claim_id, "matches.occurrence_count")


def _container_coverage(
    context: InspectionValidationContext, claim_id: str
) -> Mapping[str, Any]:
    coverage = _inspection_mapping(context.result.get("coverage"), claim_id, "coverage")
    return _inspection_mapping(
        coverage.get("container_coverage"),
        claim_id,
        "coverage.container_coverage",
    )


def _revision_inventory(
    context: InspectionValidationContext, claim_id: str
) -> Mapping[str, Any]:
    return _inspection_mapping(
        context.result.get("revision_inventory"), claim_id, "revision_inventory"
    )


def _evaluate_indexed_container(
    context: InspectionValidationContext, claim_id: str
) -> None:
    coverage = _inspection_mapping(context.result.get("coverage"), claim_id, "coverage")
    container = _container_coverage(context, claim_id)
    indexed = _inspection_nonnegative_int(
        coverage.get("indexed_paragraph_count"),
        claim_id,
        "coverage.indexed_paragraph_count",
    )
    nonempty = _inspection_nonnegative_int(
        coverage.get("nonempty_indexed_paragraph_count"),
        claim_id,
        "coverage.nonempty_indexed_paragraph_count",
    )
    nested_indexed = _inspection_nonnegative_int(
        container.get("indexed_paragraph_count"),
        claim_id,
        "coverage.container_coverage.indexed_paragraph_count",
    )
    body = _inspection_nonnegative_int(
        container.get("body_paragraph_count"),
        claim_id,
        "coverage.container_coverage.body_paragraph_count",
    )
    table = _inspection_nonnegative_int(
        container.get("table_cell_paragraph_count"),
        claim_id,
        "coverage.container_coverage.table_cell_paragraph_count",
    )
    if nonempty > indexed:
        _inspection_failed(claim_id, "coverage.nonempty_indexed_paragraph_count")
    if indexed != nested_indexed:
        _inspection_failed(claim_id, "coverage.indexed_paragraph_count")
    if nested_indexed != body + table:
        _inspection_failed(
            claim_id, "coverage.container_coverage.indexed_paragraph_count"
        )


def _evaluate_container_exclusions(
    context: InspectionValidationContext, claim_id: str
) -> None:
    container = _container_coverage(context, claim_id)
    excluded_subtrees = _inspection_nonnegative_int(
        container.get("excluded_subtree_count"),
        claim_id,
        "coverage.container_coverage.excluded_subtree_count",
    )
    excluded_paragraphs = _inspection_nonnegative_int(
        container.get("excluded_paragraph_count"),
        claim_id,
        "coverage.container_coverage.excluded_paragraph_count",
    )
    excluded_by_kind = _inspection_count_mapping(
        container.get("excluded_by_kind"),
        claim_id,
        "coverage.container_coverage.excluded_by_kind",
    )
    excluded_paragraphs_by_kind = _inspection_count_mapping(
        container.get("excluded_paragraphs_by_kind"),
        claim_id,
        "coverage.container_coverage.excluded_paragraphs_by_kind",
    )
    if excluded_subtrees != sum(excluded_by_kind.values()):
        _inspection_failed(
            claim_id, "coverage.container_coverage.excluded_subtree_count"
        )
    if excluded_paragraphs != sum(excluded_paragraphs_by_kind.values()):
        _inspection_failed(
            claim_id, "coverage.container_coverage.excluded_paragraph_count"
        )
    complete = excluded_subtrees == 0
    if container.get("coverage_complete") is not complete:
        _inspection_failed(claim_id, "coverage.container_coverage.coverage_complete")
    if container.get("legacy_two_field_anchor_safe") is not complete:
        _inspection_failed(
            claim_id, "coverage.container_coverage.legacy_two_field_anchor_safe"
        )


def _revision_partition_values(
    context: InspectionValidationContext, claim_id: str
) -> tuple[Mapping[str, Any], dict[str, int]]:
    inventory = _revision_inventory(context, claim_id)
    values = {
        key: _inspection_nonnegative_int(
            inventory.get(key), claim_id, f"revision_inventory.{key}"
        )
        for key in (
            "tracked_text_revision_elements",
            "total_revision_elements",
            "in_scope_revision_elements",
            "decoded_revision_elements",
            "unsupported_revision_occurrences",
            "unsupported_revision_kind_count",
            "excluded_container_occurrences",
            "excluded_container_kind_count",
        )
    }
    return inventory, values


def _evaluate_revision_partitions(
    context: InspectionValidationContext, claim_id: str
) -> None:
    inventory, values = _revision_partition_values(context, claim_id)
    unsupported = _inspection_count_mapping(
        inventory.get("unsupported_by_kind"),
        claim_id,
        "revision_inventory.unsupported_by_kind",
    )
    excluded = _inspection_count_mapping(
        inventory.get("excluded_by_container"),
        claim_id,
        "revision_inventory.excluded_by_container",
    )
    if values["total_revision_elements"] != (
        values["in_scope_revision_elements"] + values["excluded_container_occurrences"]
    ):
        _inspection_failed(claim_id, "revision_inventory.total_revision_elements")
    if values["in_scope_revision_elements"] != (
        values["decoded_revision_elements"] + values["unsupported_revision_occurrences"]
    ):
        _inspection_failed(claim_id, "revision_inventory.in_scope_revision_elements")
    if values["unsupported_revision_occurrences"] != sum(unsupported.values()):
        _inspection_failed(
            claim_id, "revision_inventory.unsupported_revision_occurrences"
        )
    if values["excluded_container_occurrences"] != sum(excluded.values()):
        _inspection_failed(
            claim_id, "revision_inventory.excluded_container_occurrences"
        )
    if values["unsupported_revision_kind_count"] != len(unsupported):
        _inspection_failed(
            claim_id, "revision_inventory.unsupported_revision_kind_count"
        )
    if values["excluded_container_kind_count"] != len(excluded):
        _inspection_failed(claim_id, "revision_inventory.excluded_container_kind_count")
    if inventory.get("partition_valid") is not True:
        _inspection_failed(claim_id, "revision_inventory.partition_valid")
    if inventory.get("container_policy") != _container_coverage(context, claim_id):
        _inspection_failed(claim_id, "revision_inventory.container_policy")


def _evaluate_revision_flags(
    context: InspectionValidationContext, claim_id: str
) -> None:
    inventory, values = _revision_partition_values(context, claim_id)
    no_unsupported = values["unsupported_revision_occurrences"] == 0
    no_excluded = values["excluded_container_occurrences"] == 0
    if inventory.get("all_in_scope_revision_elements_decoded") is not no_unsupported:
        _inspection_failed(
            claim_id, "revision_inventory.all_in_scope_revision_elements_decoded"
        )
    if inventory.get("all_revision_elements_decoded") is not (
        no_unsupported and no_excluded
    ):
        _inspection_failed(claim_id, "revision_inventory.all_revision_elements_decoded")


def _evaluate_revision_global(
    context: InspectionValidationContext, claim_id: str
) -> None:
    _inventory, values = _revision_partition_values(context, claim_id)
    tracked = values["tracked_text_revision_elements"]
    total = values["total_revision_elements"]
    decoded = values["decoded_revision_elements"]
    top_warning = context.result.get("has_tracked_text_revisions")
    if not decoded <= tracked <= total:
        _inspection_failed(
            claim_id, "revision_inventory.tracked_text_revision_elements"
        )
    if decoded > 0 and top_warning is not True:
        _inspection_failed(claim_id, "has_tracked_text_revisions")
    if top_warning is True and total == 0:
        _inspection_failed(claim_id, "has_tracked_text_revisions")


def _evaluate_returned_text_caps(
    context: InspectionValidationContext, claim_id: str
) -> None:
    collection = _inspection_mode_collection(context.result, claim_id)
    values = inspection_returned_document_text_values(
        context.result.get("section_navigation")
    ) + inspection_returned_document_text_values(collection)
    individual_cap = INSPECT_FIXED_LIMITS_V1["max_paragraph_text_chars"]
    aggregate_cap = INSPECT_FIXED_LIMITS_V1["max_returned_text_chars"]
    if any(len(value) > individual_cap for value in values):
        _inspection_failed(claim_id, "returned_text.max_individual_chars")
    if sum(map(len, values)) > aggregate_cap:
        _inspection_failed(claim_id, "returned_text.max_total_chars")


def _evaluate_excluded_scope(
    context: InspectionValidationContext, claim_id: str
) -> None:
    coverage = _inspection_mapping(context.result.get("coverage"), claim_id, "coverage")
    excluded = inspection_excluded_parts_v1(coverage.get("excluded_parts"))
    if excluded is None:
        _inspection_failed(claim_id, "coverage.excluded_parts")


def _evaluate_returned_revision_warning(
    context: InspectionValidationContext, claim_id: str
) -> None:
    if context.result.get("has_tracked_text_revisions") is False and any(
        isinstance(item, Mapping) and item.get("has_tracked_text_revisions") is True
        for item in _inspection_mode_collection(context.result, claim_id)
    ):
        _inspection_failed(claim_id, "has_tracked_text_revisions")


INSPECTION_CRITICAL_CHECKS_V1 = (
    InspectionCriticalCheck("constants.policy-scope-limits-v1", _evaluate_constants),
    InspectionCriticalCheck("payload.collection-count-v1", _evaluate_collection_count),
    InspectionCriticalCheck("page.pagination-v1", _evaluate_pagination),
    InspectionCriticalCheck("page.literal-binding-v1", _evaluate_literal_binding),
    InspectionCriticalCheck(
        "snapshot.indexed-container-v1", _evaluate_indexed_container
    ),
    InspectionCriticalCheck(
        "snapshot.container-exclusions-v1", _evaluate_container_exclusions
    ),
    InspectionCriticalCheck("page.mode-coverage-v1", _evaluate_mode_coverage),
    InspectionCriticalCheck(
        "snapshot.revision-partitions-v1", _evaluate_revision_partitions
    ),
    InspectionCriticalCheck("snapshot.revision-flags-v1", _evaluate_revision_flags),
    InspectionCriticalCheck("snapshot.revision-global-v1", _evaluate_revision_global),
    InspectionCriticalCheck(
        "payload.returned-text-caps-v1", _evaluate_returned_text_caps
    ),
    InspectionCriticalCheck("snapshot.excluded-scope-v1", _evaluate_excluded_scope),
    InspectionCriticalCheck(
        "payload.returned-revision-warning-v1",
        _evaluate_returned_revision_warning,
    ),
)


class InspectionContractV1:
    """Transport-neutral bounded consistency and safety gate."""

    critical_checks = INSPECTION_CRITICAL_CHECKS_V1

    @classmethod
    def validate_critical(cls, serialized_result: Mapping[str, Any]) -> None:
        """Evaluate only the explicitly bounded live-result check catalog."""
        if not isinstance(serialized_result, Mapping):
            raise InspectionContractError("shape.mode-and-nested-v1", "result")
        frozen = _freeze_inspection_value(serialized_result)
        assert isinstance(frozen, Mapping)
        context = InspectionValidationContext(frozen)
        for check in cls.critical_checks:
            check.evaluator(context, check.check_id)


APPLY_OPERATION_REPLACE = "replace"
APPLY_OPERATION_DELETE = "delete"
APPLY_OPERATION_COUNTER = "counter"
APPLY_OPERATION_REINSTATE = "reinstate"
APPLY_OPERATIONS_V1 = frozenset(
    {
        APPLY_OPERATION_REPLACE,
        APPLY_OPERATION_DELETE,
        APPLY_OPERATION_COUNTER,
        APPLY_OPERATION_REINSTATE,
    }
)

ROUND_TRIP_STATUS_PASSED = "passed"
ROUND_TRIP_STATUS_FAILED = "failed"
ROUND_TRIP_STATUSES_V1 = frozenset({ROUND_TRIP_STATUS_PASSED, ROUND_TRIP_STATUS_FAILED})
ROUND_TRIP_COMPARISON_CURRENT = "ooxml_semantic_diff_outside_touched_anchors"
ROUND_TRIP_COMPARISONS_V1 = frozenset(
    {
        "exact",
        ROUND_TRIP_COMPARISON_CURRENT,
    }
)

PREFLIGHT_EDIT_STATUS_APPLICABLE = "applicable"
PREFLIGHT_EDIT_STATUS_BLOCKED = "blocked"
PREFLIGHT_EDIT_STATUS_PLANNED = "planned"
PREFLIGHT_EDIT_STATUS_NOT_EVALUATED = "not_evaluated"
PREFLIGHT_EDIT_STATUSES_V1 = frozenset(
    {
        PREFLIGHT_EDIT_STATUS_APPLICABLE,
        PREFLIGHT_EDIT_STATUS_BLOCKED,
        PREFLIGHT_EDIT_STATUS_PLANNED,
        PREFLIGHT_EDIT_STATUS_NOT_EVALUATED,
    }
)
PREFLIGHT_POSITION_STATUS_SUPPORTED = "supported"
PREFLIGHT_POSITION_STATUS_UNSUPPORTED = "unsupported"
PREFLIGHT_POSITION_STATUS_NOT_EVALUATED = "not_evaluated"
PREFLIGHT_POSITION_STATUSES_V1 = frozenset(
    {
        PREFLIGHT_POSITION_STATUS_SUPPORTED,
        PREFLIGHT_POSITION_STATUS_UNSUPPORTED,
        PREFLIGHT_POSITION_STATUS_NOT_EVALUATED,
    }
)
PREFLIGHT_FAILURE_PHASES_V1 = frozenset(
    {
        "validation",
        "source",
        "matching",
        "planning",
        "surgery",
        "serialization",
        "round_trip",
        "preflight_binding",
        "publication",
    }
)
