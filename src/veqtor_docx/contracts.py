# SPDX-License-Identifier: Apache-2.0
"""Versioned output domains shared by DOCX producers and history readers.

The v1 sets are append-only compatibility contracts. Producers use named
values from this module, and decision-record projection accepts the complete
historical set. Removing or renaming a v1 value requires a new schema version.
"""

from __future__ import annotations

from types import MappingProxyType

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
