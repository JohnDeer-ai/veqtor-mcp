# SPDX-License-Identifier: Apache-2.0
"""Bounded structural inventory of tables in one immutable DOCX snapshot.

Coordinates describe the current XML grid, including omitted grid columns.
They are neither rendered positions nor authority to edit the table. In
particular, a rectangular grid can still have property revisions, comments,
fields or other dependencies which this read-only inventory does not resolve.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from lxml import etree

from ._ooxml import (
    DocxError,
    ResourceLimitError,
    canonical_body_flow_v1,
    parse_xml,
    w,
)
from .contracts import INSPECT_CONTAINER_POLICY_V1
from .inspect import _load_snapshot_from_payload, _paragraph_ref

TABLE_MAP_LIMITS = MappingProxyType({
    "tables": 500,
    "rows": 10_000,
    "cells": 20_000,
    "grid_columns": 256,
    "table_nesting_depth": 8,
    "fingerprint_bytes": 64 * 1024 * 1024,
    "result_bytes": 32 * 1024 * 1024,
})
_PART = "word/document.xml"
_FINGERPRINT_POLICY = "element_c14n_without_comments_v1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_DECIMAL = re.compile(r"[0-9]{1,10}\Z")
_WRAPPERS = {w("sdt"), w("sdtContent")}
_SCHEMAS = {
    "table": "table_ref.v1",
    "row": "table_row_ref.v1",
    "cell": "table_cell_ref.v1",
}
_COMMON_REF_KEYS = frozenset({
    "schema_version", "ref_type", "file_sha256", "part_name",
    "container_policy", "fingerprint_policy", "element_sha256", "table_index",
})
_PROPERTY_REVISION_NAMES = frozenset({
    "rPrChange", "pPrChange", "tcPrChange", "trPrChange", "tblPrChange",
    "tblPrExChange", "tblGridChange", "sectPrChange", "numberingChange",
})
_STRUCTURAL_REVISION_NAMES = frozenset({
    "cellIns", "cellDel", "cellMerge",
})
_LIMITATIONS = [
    "read_only_inventory_not_mutation_authority",
    "current_xml_grid_not_historical_or_rendered_geometry",
    "numbering_and_property_dependencies_not_resolved",
    "only_canonical_main_body_tables",
]


class TableMapError(DocxError):
    """A controlled, path-free table inventory or reference refusal."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _refuse(detail: str) -> None:
    raise TableMapError("table_structure_unsupported", detail)


def _bound(name: str, observed: int) -> None:
    allowed = TABLE_MAP_LIMITS[name]
    if observed > allowed:
        raise ResourceLimitError(
            f"table_map_{name}", "table inventory exceeds its fixed limit",
            allowed_count=allowed, observed_count=observed,
        )


def _one(parent: etree._Element | None, name: str) -> etree._Element | None:
    if parent is None:
        return None
    found = parent.findall(w(name))
    if len(found) > 1:
        _refuse("duplicate current table geometry property")
    return found[0] if found else None


def _integer(parent: etree._Element | None, name: str, default: int) -> int:
    node = _one(parent, name)
    if node is None:
        return default
    raw = node.get(w("val"))
    if not isinstance(raw, str) or not _DECIMAL.fullmatch(raw):
        _refuse("invalid current table grid integer")
    value = int(raw)
    if name == "gridSpan" and value == 0:
        _refuse("a cell grid span must be positive")
    _bound("grid_columns", value)
    return value


def _merge(parent: etree._Element | None, name: str) -> str | None:
    node = _one(parent, name)
    if node is None:
        return None
    value = node.get(w("val"), "continue")
    if value not in {"restart", "continue"}:
        _refuse("invalid current table merge value")
    return value


def _nearest(node: etree._Element, name: str) -> etree._Element | None:
    return next((p for p in node.iterancestors() if p.tag == w(name)), None)


def _children(
    parent: etree._Element, target: str, properties: set[str],
) -> tuple[list[etree._Element], bool, bool]:
    """Visit only explicit block wrappers, never properties or unknown payload."""
    found: list[etree._Element] = []
    wrapped = False
    unknown = False

    def walk(node: etree._Element) -> None:
        nonlocal wrapped, unknown
        for child in node:
            if child.tag == w(target):
                found.append(child)
            elif child.tag == w("sdt"):
                wrapped = True
                content = _one(child, "sdtContent")
                if content is None:
                    _refuse("table block content control lacks content")
                walk(content)
            elif child.tag == w("sdtContent"):
                wrapped = True
                walk(child)
            elif child.tag not in properties:
                unknown = True

    walk(parent)
    return found, wrapped, unknown


def _revision_flags(element: etree._Element) -> dict[str, Any]:
    properties: Counter[str] = Counter()
    structural: Counter[str] = Counter()
    for node in element.iter():
        if not isinstance(node.tag, str):
            continue
        local = etree.QName(node).localname
        if node.tag == w(local) and local in _PROPERTY_REVISION_NAMES:
            properties[local] += 1
        elif node.tag == w(local) and local in _STRUCTURAL_REVISION_NAMES:
            structural[local] += 1
        elif node.tag in {w("ins"), w("del")}:
            parent = node.getparent()
            if parent is not None and parent.tag in {
                w("trPr"), w("tcPr"), w("tblPr"), w("rPr"), w("pPr"),
            }:
                structural[local] += 1
    return {
        "has_tracked_property_revisions": bool(properties),
        "tracked_property_revision_counts": dict(sorted(properties.items())),
        "has_structural_revision_markers": bool(structural),
        "structural_revision_marker_counts": dict(sorted(structural.items())),
    }


class _References:
    def __init__(self, file_sha256: str) -> None:
        self.file_sha256 = file_sha256
        self.fingerprint_bytes = 0

    def make(
        self, kind: str, element: etree._Element, table_index: int,
        row_index: int | None = None, cell_index: int | None = None,
    ) -> dict[str, Any]:
        try:
            encoded = etree.tostring(
                element, method="c14n", with_comments=False, with_tail=False,
            )
        except (etree.C14NError, ValueError) as exc:
            raise TableMapError(
                "table_structure_unsupported", "cannot fingerprint table XML",
            ) from exc
        self.fingerprint_bytes += len(encoded)
        _bound("fingerprint_bytes", self.fingerprint_bytes)
        result = {
            "schema_version": _SCHEMAS[kind], "ref_type": kind,
            "file_sha256": self.file_sha256, "part_name": _PART,
            "container_policy": INSPECT_CONTAINER_POLICY_V1,
            "fingerprint_policy": _FINGERPRINT_POLICY,
            "element_sha256": hashlib.sha256(encoded).hexdigest(),
            "table_index": table_index,
        }
        if row_index is not None:
            result["row_index"] = row_index
        if cell_index is not None:
            result["cell_index"] = cell_index
        return result


def _merge_issues(rows: list[dict[str, Any]]) -> None:
    previous_vertical: dict[tuple[int, int], str | None] = {}
    for row in rows:
        current_vertical: dict[tuple[int, int], str | None] = {}
        horizontal_active = False
        for cell in row["cells"]:
            horizontal = cell["h_merge"]
            vertical = cell["v_merge"]
            interval = (cell["grid_start"], cell["grid_span"])
            if horizontal == "continue" and not horizontal_active:
                cell["issues"].append("orphan_horizontal_merge")
            horizontal_active = horizontal in {"restart", "continue"}
            if vertical == "continue":
                if interval not in previous_vertical:
                    cell["issues"].append("unmatched_vertical_merge_range")
                elif horizontal or previous_vertical[interval]:
                    cell["issues"].append("combined_merge_semantics_not_validated")
            if vertical is not None:
                current_vertical[interval] = horizontal
        previous_vertical = current_vertical


def build_table_map(payload: bytes) -> dict[str, Any]:
    """Inventory current table structure without accessing or writing a path.

    Empty paragraphs retain the exact ``paragraph_ref.v1`` identity used by
    inspection. A cell owns only its immediate canonical content; paragraphs in
    a nested table belong to that nested table's cells and are not duplicated.
    Malformed numeric geometry refuses rather than inventing coordinates.
    """
    if type(payload) is not bytes:
        raise TableMapError("invalid_table_map_input", "DOCX payload must be bytes")
    snapshot = _load_snapshot_from_payload(payload, path="")
    body = parse_xml(snapshot.body_xml)
    flow = canonical_body_flow_v1(body)
    paragraph_by_element = {
        item.element: snapshot.paragraphs[item.paragraph_index]
        for item in flow.paragraphs
    }
    raw_tables = list(body.iter(w("tbl")))
    table_nodes = [
        node for node in raw_tables if flow.exclusion_kind_for(node) is None
    ]
    _bound("tables", len(table_nodes))
    depths = {
        node: 1 + sum(p.tag == w("tbl") for p in node.iterancestors())
        for node in table_nodes
    }
    for depth in depths.values():
        _bound("table_nesting_depth", depth)
    references = _References(snapshot.file_sha256)
    table_refs = {
        node: references.make("table", node, index)
        for index, node in enumerate(table_nodes)
    }
    counts = {
        "tables": len(table_nodes), "rows": 0, "cells": 0,
        "table_paragraphs": 0, "empty_table_paragraphs": 0, "nested_tables": 0,
    }
    tables: list[dict[str, Any]] = []
    cell_refs: dict[etree._Element, dict[str, Any]] = {}
    mapped_paragraphs: set[int] = set()
    parent_cells: list[etree._Element | None] = []
    for table_index, node in enumerate(table_nodes):
        owner = node.getparent()
        while owner is not None and owner.tag in _WRAPPERS:
            owner = owner.getparent()
        if owner is None or owner.tag not in {w("body"), w("tc")}:
            _refuse("table outside a supported body or cell location")
        depth = depths[node]
        parent_cell = owner if owner.tag == w("tc") else None
        parent_cells.append(parent_cell)
        counts["nested_tables"] += parent_cell is not None
        grid = _one(node, "tblGrid")
        grid_columns = len(grid.findall(w("gridCol"))) if grid is not None else None
        if grid_columns is not None:
            _bound("grid_columns", grid_columns)
        issues = [] if grid_columns else ["missing_or_empty_current_table_grid"]
        rows, wrapped, unknown = _children(
            node, "tr", {w("tblPr"), w("tblGrid")},
        )
        if wrapped:
            issues.append("wrapped_rows")
        if unknown:
            issues.append("unsupported_table_block_children")
        if not rows:
            issues.append("table_has_no_rows")
        counts["rows"] += len(rows)
        _bound("rows", counts["rows"])
        result_rows = []
        has_merges = False
        has_gaps = False
        has_nested = False
        for row_index, row in enumerate(rows):
            row_issues = []
            row_ref = references.make("row", row, table_index, row_index)
            properties = _one(row, "trPr")
            before = _integer(properties, "gridBefore", 0)
            after = _integer(properties, "gridAfter", 0)
            has_gaps |= bool(before or after)
            cells, wrapped, unknown = _children(
                row, "tc", {w("trPr"), w("tblPrEx")},
            )
            if wrapped:
                row_issues.append("wrapped_cells")
            if unknown:
                row_issues.append("unsupported_row_block_children")
            if not cells:
                row_issues.append("row_has_no_cells")
            counts["cells"] += len(cells)
            _bound("cells", counts["cells"])
            result_cells = []
            position = before
            for cell_index, cell in enumerate(cells):
                cell_ref = references.make("cell", cell, table_index, row_index, cell_index)
                cell_refs[cell] = cell_ref
                properties = _one(cell, "tcPr")
                span = _integer(properties, "gridSpan", 1)
                horizontal = _merge(properties, "hMerge")
                vertical = _merge(properties, "vMerge")
                has_merges |= span != 1 or horizontal is not None or vertical is not None
                cell_issues = []
                _, wrapped, unknown = _children(
                    cell, "p", {w("tcPr"), w("tbl")},
                )
                if wrapped:
                    cell_issues.append("wrapped_cell_content")
                if unknown:
                    cell_issues.append("unsupported_cell_block_children")
                paragraph_refs = []
                content = []
                nested_refs = []
                for child in cell.iter():
                    if child is cell or _nearest(child, "tc") is not cell:
                        continue
                    if child in paragraph_by_element:
                        paragraph = paragraph_by_element[child]
                        ref = _paragraph_ref(snapshot, paragraph)
                        paragraph_refs.append(ref)
                        content.append({"kind": "paragraph", "reference": ref})
                        mapped_paragraphs.add(paragraph.paragraph_index)
                        counts["table_paragraphs"] += 1
                        counts["empty_table_paragraphs"] += not paragraph.text
                    elif child in table_refs:
                        ref = table_refs[child]
                        nested_refs.append(ref)
                        content.append({"kind": "table", "reference": ref})
                has_nested |= bool(nested_refs)
                if not paragraph_refs:
                    cell_issues.append("cell_has_no_canonical_paragraph")
                if not content or content[-1]["kind"] != "paragraph":
                    cell_issues.append("cell_lacks_final_canonical_paragraph")
                # Unknown canonical exclusions must not become a claim of a
                # fully understood rectangular cell merely because they are
                # absent from the paragraph index.
                if any(
                    flow.exclusion_kind_for(child) is not None
                    for child in cell.iter()
                ):
                    cell_issues.append("excluded_cell_content")
                result_cells.append({
                    "cell_ref": cell_ref, "cell_index": cell_index,
                    "grid_start": position, "grid_span": span,
                    "h_merge": horizontal, "v_merge": vertical,
                    "paragraph_refs": paragraph_refs, "content": content,
                    "nested_table_refs": nested_refs, "issues": cell_issues,
                    **_revision_flags(cell),
                })
                position += span
                _bound("grid_columns", position)
            row_end = position + after
            _bound("grid_columns", row_end)
            if grid_columns is not None and row_end != grid_columns:
                row_issues.append("row_grid_extent_differs_from_table_grid")
            result_rows.append({
                "row_ref": row_ref, "row_index": row_index,
                "grid_before": before, "grid_after": after,
                "grid_extent": row_end, "cells": result_cells,
                "issues": row_issues, **_revision_flags(row),
            })
        _merge_issues(result_rows)
        item_issues = bool(issues) or any(
            row["issues"] or any(cell["issues"] for cell in row["cells"])
            for row in result_rows
        )
        tables.append({
            "table_ref": table_refs[node], "table_index": table_index,
            "nesting_depth": depth, "parent_cell_ref": None,
            "grid_column_count": grid_columns, "rows": result_rows,
            "has_grid_gaps": has_gaps, "has_merges": has_merges,
            "has_nested_tables": has_nested,
            "simple_rectangular": not (
                item_issues or has_gaps or has_merges or has_nested
            ),
            "mutation_safety": "not_assessed",
            "issues": issues, **_revision_flags(node),
        })
    for table, parent_cell in zip(tables, parent_cells, strict=True):
        if parent_cell is not None:
            if parent_cell not in cell_refs:
                _refuse("nested table parent cell is not in the inventory")
            table["parent_cell_ref"] = cell_refs[parent_cell]
    canonical_table_paragraphs = {
        p.paragraph_index for p in snapshot.paragraphs if p.container_kind == "table_cell"
    }
    unmapped = len(canonical_table_paragraphs - mapped_paragraphs)
    issue_count = sum(
        len(t["issues"]) + sum(
            len(row["issues"]) + sum(len(c["issues"]) for c in row["cells"])
            for row in t["rows"]
        ) for t in tables
    )
    result = {
        "schema_version": "table_map.v1",
        "file_sha256": snapshot.file_sha256,
        "part_name": _PART, "container_policy": INSPECT_CONTAINER_POLICY_V1,
        "counts": counts, "tables": tables,
        "coverage": {
            "scope": "canonical_main_body_tables_v1",
            "complete": not (flow.excluded_subtrees or unmapped or issue_count),
            "canonical_body_flow": dict(snapshot.container_coverage),
            "excluded_parts": list(snapshot.excluded_parts),
            "excluded_table_count": len(raw_tables) - len(table_nodes),
            "unmapped_table_cell_paragraph_count": unmapped,
            "structural_issue_count": issue_count,
        },
        "limits": dict(TABLE_MAP_LIMITS), "limitations": list(_LIMITATIONS),
    }
    encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()
    _bound("result_bytes", len(encoded))
    return result


def resolve_table_ref(payload: bytes, ref: object) -> dict[str, Any]:
    """Resolve a closed table, row or cell reference against these exact bytes."""
    if not isinstance(ref, Mapping):
        raise TableMapError("invalid_table_reference", "table reference must be an object")
    kind = ref.get("ref_type")
    if not isinstance(kind, str) or kind not in _SCHEMAS:
        raise TableMapError("invalid_table_reference", "unknown table reference type")
    keys = _COMMON_REF_KEYS | (
        {"row_index", "cell_index"} if kind == "cell"
        else {"row_index"} if kind == "row" else set()
    )
    if set(ref) != keys or any(
        ref.get(key) != value for key, value in {
            "schema_version": _SCHEMAS[kind], "part_name": _PART,
            "container_policy": INSPECT_CONTAINER_POLICY_V1,
            "fingerprint_policy": _FINGERPRINT_POLICY,
        }.items()
    ):
        raise TableMapError("invalid_table_reference", "invalid table reference shape or policy")
    for key in ("file_sha256", "element_sha256"):
        if not isinstance(ref[key], str) or not _SHA.fullmatch(ref[key]):
            raise TableMapError("invalid_table_reference", "invalid table reference digest")
    for key in keys & {"table_index", "row_index", "cell_index"}:
        if type(ref[key]) is not int or ref[key] < 0:
            raise TableMapError("invalid_table_reference", "invalid table reference index")
    if type(payload) is not bytes:
        raise TableMapError("invalid_table_map_input", "DOCX payload must be bytes")
    if ref["file_sha256"] != hashlib.sha256(payload).hexdigest():
        raise TableMapError("table_reference_mismatch", "DOCX bytes changed")
    result = build_table_map(payload)
    try:
        item = result["tables"][ref["table_index"]]
        if kind in {"row", "cell"}:
            item = item["rows"][ref["row_index"]]
        if kind == "cell":
            item = item["cells"][ref["cell_index"]]
    except IndexError as exc:
        raise TableMapError("table_reference_mismatch", "table target does not exist") from exc
    if item[f"{kind}_ref"] != dict(ref):
        raise TableMapError("table_reference_mismatch", "table target fingerprint changed")
    return item
