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

TEXT_REVISION_SUFFIX_BY_NAME_V1 = MappingProxyType(
    {"ins": "Ins", "del": "Del"}
)
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
STRUCTURAL_REVISION_PARENT_NAMES_V1 = frozenset(
    {"trPr", "tcPr", "tblPr", "sectPr"}
)
STRUCTURAL_REVISION_SUFFIXES_V1 = frozenset(
    TEXT_REVISION_SUFFIX_BY_NAME_V1.values()
)
EXTRACT_REVISION_CATEGORIES_V1 = frozenset(
    UNSUPPORTED_REVISION_NAMES_V1
    | MOVE_REVISION_NAMES_V1
    | {
        f"{parent}{suffix}"
        for parent in STRUCTURAL_REVISION_PARENT_NAMES_V1
        for suffix in STRUCTURAL_REVISION_SUFFIXES_V1
    }
    | {
        f"paragraphMark{suffix}"
        for suffix in STRUCTURAL_REVISION_SUFFIXES_V1
    }
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
ROUND_TRIP_STATUSES_V1 = frozenset({ROUND_TRIP_STATUS_PASSED})
ROUND_TRIP_COMPARISON_CURRENT = "ooxml_semantic_diff_outside_touched_anchors"
ROUND_TRIP_COMPARISONS_V1 = frozenset(
    {
        "exact",
        ROUND_TRIP_COMPARISON_CURRENT,
    }
)
