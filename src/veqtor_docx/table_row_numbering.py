# SPDX-License-Identifier: Apache-2.0
"""Closed numbering effects for a complete table-row structural unit.

The structural caller supplies clean alternatives with all selected rows
present or absent. This module never edits either alternative. It proves each
removed cell paragraph, nearby retained uses of the same numbering families,
and later affected uses across the main story. Independent abstract lists do
not become dependencies merely because they occur elsewhere in the document.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from lxml import etree

from ._ooxml import w
from .apply import ApplyError
from .paragraph_numbering import MAX_NUMBERING_EFFECTS, _Resolver, _bounded, _refuse

_PENDING_ROW = frozenset({w("ins"), w("del")})
_PENDING_CELL = frozenset({w("cellIns"), w("cellDel"), w("cellMerge")})


def _row(paragraph: etree._Element) -> etree._Element:
    cell = paragraph.getparent()
    row = cell.getparent() if cell is not None else None
    table = row.getparent() if row is not None else None
    if (
        cell is None or cell.tag != w("tc")
        or row is None or row.tag != w("tr")
        or table is None or table.tag != w("tbl")
    ):
        _refuse("selected numbering paragraph is not a direct table-row cell", reason="invalid_projection")
    return row


def _pending_structure(paragraph: etree._Element) -> bool:
    """A pending row/cell may disappear and cannot supply a fixed counter."""
    for parent in paragraph.iterancestors():
        if parent.tag == w("body"):
            return False
        properties = (
            parent.findall(w("trPr")) if parent.tag == w("tr")
            else parent.findall(w("tcPr")) if parent.tag == w("tc")
            else []
        )
        names = _PENDING_ROW if parent.tag == w("tr") else _PENDING_CELL
        if any(node.tag in names for props in properties for node in props):
            return True
    return False


def _guard_pending_counters(resolver: _Resolver, indices: Sequence[int]) -> None:
    selected = {index: resolver.effective(index) for index in indices}
    needed = resolver.related_numbering(indices)
    if not needed:
        return
    last = max(index for index, fact in selected.items() if fact.num_id in needed)
    for index in range(last + 1):
        if resolver._story_context(index) == "separate":
            continue
        if resolver.effective(index).num_id in needed and _pending_structure(resolver.paragraphs[index]):
            _refuse(
                "an affected numbering counter depends on a pending row or cell revision",
                reason="pending_table_structure",
            )


def numbering_effects_for_rows(
    parts: Mapping[str, bytes], longer_root: etree._Element,
    shorter_root: etree._Element, removed_paragraph_indices: Sequence[int],
) -> dict[str, list[dict]]:
    """Prove complete row-removal effects using zero-based body paragraph indices.

    For an insertion, pass the accepted longer tree first and the rejected
    shorter tree second, then rename ``before``/``after`` at the call site.
    Only the caller's selected complete rows differ; retained paragraphs must
    be identical and remain in order. Existing single-level numbering rules,
    resource limits, style inheritance and semantic-ID uniqueness checks apply.
    """
    before = _Resolver(parts, longer_root)
    after = _Resolver(parts, shorter_root)
    removed = list(removed_paragraph_indices)
    if (
        not removed
        or any(type(index) is not int or not 0 <= index < len(before.paragraphs) for index in removed)
        or len(removed) != len(set(removed))
    ):
        raise ApplyError("invalid_edit", "removed row numbering paragraph indices must be valid and unique")
    _bounded(len(removed) <= MAX_NUMBERING_EFFECTS, "too many removed row numbering paragraphs")
    removed_set = set(removed)
    if len(before.paragraphs) != len(after.paragraphs) + len(removed):
        _refuse("row numbering projections have an unexpected paragraph count", reason="invalid_projection")

    selected_rows = {_row(before.paragraphs[index]) for index in removed}
    tables = {row.getparent() for row in selected_rows}
    if len(tables) != 1:
        _refuse("selected row numbering paragraphs must belong to one table", reason="invalid_projection")
    table = next(iter(tables))
    paragraphs_by_identity = {id(paragraph): index for index, paragraph in enumerate(before.paragraphs)}
    row_indices = {
        paragraphs_by_identity[id(paragraph)]
        for row in selected_rows for paragraph in row.iter(w("p"))
    }
    if row_indices != removed_set:
        _refuse("row numbering projection must remove every selected row paragraph", reason="invalid_projection")
    rows = table.findall(w("tr"))
    positions = [index for index, row in enumerate(rows) if row in selected_rows]
    if positions != list(range(min(positions), max(positions) + 1)):
        _refuse("selected row numbering paragraphs must form adjacent rows", reason="invalid_projection")
    if any(_pending_structure(before.paragraphs[index]) for index in removed):
        _refuse("selected row numbering projection must be clean", reason="pending_table_structure")

    # An independently supplied shorter tree must describe exactly the same
    # retained paragraphs. Count agreement alone cannot establish correspondence.
    survivor_map = {}
    for old_index, paragraph in enumerate(before.paragraphs):
        if old_index in removed_set:
            continue
        new_index = len(survivor_map)
        if etree.tostring(paragraph) != etree.tostring(after.paragraphs[new_index]):
            _refuse("retained row-numbering paragraphs differ or changed order", reason="invalid_projection")
        survivor_map[old_index] = new_index

    selected = set(removed)
    affected = before.related_numbering(removed)
    adjacent_rows = [rows[index] for index in (min(positions) - 1, max(positions) + 1) if 0 <= index < len(rows)]
    for row in adjacent_rows:
        for paragraph in row.iter(w("p")):
            index = paragraphs_by_identity[id(paragraph)]
            if before._story_context(index) == "main" and before.effective(index).num_id in affected | {None}:
                selected.add(index)
    if affected:
        for index in range(min(removed) + 1, len(before.paragraphs)):
            if before._story_context(index) == "separate":
                continue
            if before.effective(index).num_id in affected:
                selected.add(index)
    indices = sorted(selected)
    after_indices = [survivor_map[index] for index in indices if index not in removed_set]
    _guard_pending_counters(before, indices)
    _guard_pending_counters(after, after_indices)
    return {"before": before.resolve(indices), "after": after.resolve(after_indices)}
