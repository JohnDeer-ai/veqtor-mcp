# SPDX-License-Identifier: Apache-2.0
"""Synthetic structural evidence tests for immutable, read-only table maps."""

from __future__ import annotations

import copy
import hashlib
import io
import zipfile
from collections import Counter

import pytest
from lxml import etree

from veqtor_docx import inspect as inspection
from veqtor_docx import table_map
from veqtor_docx._ooxml import ResourceLimitError, W_NS, w
from veqtor_docx.table_map import TableMapError, build_table_map, resolve_table_ref


def _p(text: str = "") -> etree._Element:
    p = etree.Element(w("p"))
    if text:
        etree.SubElement(etree.SubElement(p, w("r")), w("t")).text = text
    return p


def _cell(
    *paragraphs: str, span: str | None = None,
    horizontal: str | None = None, vertical: str | None = None,
) -> etree._Element:
    cell = etree.Element(w("tc"))
    if span is not None or horizontal is not None or vertical is not None:
        properties = etree.SubElement(cell, w("tcPr"))
        for name, value in (("gridSpan", span), ("hMerge", horizontal), ("vMerge", vertical)):
            if value is not None:
                marker = etree.SubElement(properties, w(name))
                if value != "implicit":
                    marker.set(w("val"), value)
    for text in paragraphs or ("",):
        cell.append(_p(text))
    return cell


def _row(
    *cells: etree._Element, before: str | None = None, after: str | None = None,
) -> etree._Element:
    row = etree.Element(w("tr"))
    if before is not None or after is not None:
        properties = etree.SubElement(row, w("trPr"))
        for name, value in (("gridBefore", before), ("gridAfter", after)):
            if value is not None:
                etree.SubElement(properties, w(name)).set(w("val"), value)
    row.extend(cells)
    return row


def _table(*rows: etree._Element, columns: int | None = 2) -> etree._Element:
    table = etree.Element(w("tbl"))
    etree.SubElement(table, w("tblPr"))
    if columns is not None:
        grid = etree.SubElement(table, w("tblGrid"))
        for _ in range(columns):
            etree.SubElement(grid, w("gridCol")).set(w("w"), "2000")
    table.extend(rows)
    return table


def _payload(*blocks: etree._Element, comment: bytes = b"") -> bytes:
    document = etree.Element(w("document"), nsmap={"w": W_NS})
    body = etree.SubElement(document, w("body"))
    body.extend(blocks)
    etree.SubElement(body, w("sectPr"))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        package.comment = comment
        package.writestr(
            zipfile.ZipInfo("[Content_Types].xml", (2026, 1, 1, 0, 0, 0)),
            b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            b'<Default Extension="xml" ContentType="application/xml"/>'
            b'<Override PartName="/word/document.xml" ContentType="application/vnd.'
            b'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        package.writestr(
            zipfile.ZipInfo("word/document.xml", (2026, 1, 1, 0, 0, 0)),
            etree.tostring(document, xml_declaration=True, encoding="UTF-8"),
        )
    return output.getvalue()


def _two_rows() -> etree._Element:
    return _table(_row(_cell("Left"), _cell("Right")), _row(_cell(""), _cell("")))


def _all_refs(result):
    for table in result["tables"]:
        yield table["table_ref"]
        for row in table["rows"]:
            yield row["row_ref"]
            for cell in row["cells"]:
                yield cell["cell_ref"]


def test_empty_and_multiple_paragraphs_match_inspection_identity():
    payload = _payload(
        _p("Before"),
        _table(_row(_cell("First", "", "Third"), _cell(""))),
        _p("After"),
    )
    before = hashlib.sha256(payload).hexdigest()
    result = build_table_map(payload)
    assert hashlib.sha256(payload).hexdigest() == before == result["file_sha256"]
    assert result["schema_version"] == "table_map.v1"
    assert result["counts"] == {
        "tables": 1, "rows": 1, "cells": 2, "table_paragraphs": 4,
        "empty_table_paragraphs": 2, "nested_tables": 0,
    }
    snapshot = inspection._load_snapshot_from_payload(payload, path="")
    cells = result["tables"][0]["rows"][0]["cells"]
    refs = cells[0]["paragraph_refs"] + cells[1]["paragraph_refs"]
    assert refs == [inspection._paragraph_ref(snapshot, p) for p in snapshot.paragraphs[1:5]]
    assert [r["paragraph_index"] for r in refs] == [1, 2, 3, 4]
    assert result["coverage"]["complete"] is True
    assert result["tables"][0]["simple_rectangular"] is True
    assert result["tables"][0]["mutation_safety"] == "not_assessed"
    assert [x["reference"] for x in cells[0]["content"]] == cells[0]["paragraph_refs"]


def test_no_table_is_complete_empty_inventory():
    result = build_table_map(_payload(_p("Outside")))
    assert result["tables"] == []
    assert all(value == 0 for value in result["counts"].values())
    assert result["coverage"]["complete"] is True


def test_grid_gaps_keep_physical_and_logical_cell_coordinates_distinct():
    payload = _payload(_table(
        _row(_cell("A", span="2"), _cell("B"), before="1", after="1"), columns=5,
    ))
    table = build_table_map(payload)["tables"][0]
    row = table["rows"][0]
    assert (row["grid_before"], row["grid_after"], row["grid_extent"]) == (1, 1, 5)
    assert [(c["cell_index"], c["grid_start"], c["grid_span"]) for c in row["cells"]] == [
        (0, 1, 2), (1, 3, 1),
    ]
    assert row["issues"] == []
    assert table["has_grid_gaps"] and table["has_merges"]
    assert table["simple_rectangular"] is False


def test_missing_grid_and_mismatched_extent_are_visible_not_inferred():
    result = build_table_map(_payload(
        _table(_row(_cell("A")), columns=None),
        _table(_row(_cell("B")), columns=2),
    ))
    first, second = result["tables"]
    assert first["grid_column_count"] is None
    assert first["issues"] == ["missing_or_empty_current_table_grid"]
    assert second["rows"][0]["grid_extent"] == 1
    assert second["rows"][0]["issues"] == ["row_grid_extent_differs_from_table_grid"]
    assert result["coverage"]["complete"] is False
    assert not first["simple_rectangular"] and not second["simple_rectangular"]


@pytest.mark.parametrize("horizontal,vertical", [("restart", None), (None, "restart")])
def test_valid_merge_chains_are_recorded_without_editability_claim(horizontal, vertical):
    if horizontal:
        table = _table(_row(
            _cell("A", horizontal="restart"), _cell("", horizontal="implicit"),
        ))
    else:
        table = _table(
            _row(_cell("A", vertical="restart"), _cell("B")),
            _row(_cell("", vertical="implicit"), _cell("C")),
        )
    result = build_table_map(_payload(table))
    item = result["tables"][0]
    assert item["has_merges"] and not item["simple_rectangular"]
    assert result["coverage"]["complete"] is True
    assert all(not c["issues"] for r in item["rows"] for c in r["cells"])
    assert item["mutation_safety"] == "not_assessed"


@pytest.mark.parametrize("kind", ["horizontal", "vertical"])
def test_orphan_merge_is_visible_in_coverage(kind):
    cell = _cell("A", **{kind: "continue"})
    result = build_table_map(_payload(_table(_row(cell, _cell("B")))))
    issue = (
        "orphan_horizontal_merge" if kind == "horizontal"
        else "unmatched_vertical_merge_range"
    )
    assert result["tables"][0]["rows"][0]["cells"][0]["issues"] == [issue]
    assert not result["coverage"]["complete"]


def test_vertical_merge_requires_same_grid_range_in_immediately_previous_row():
    result = build_table_map(_payload(_table(
        _row(_cell("A", span="2", vertical="restart")),
        _row(_cell("", vertical="continue"), _cell("B")),
    )))
    assert result["tables"][0]["rows"][1]["cells"][0]["issues"] == [
        "unmatched_vertical_merge_range",
    ]


def test_horizontal_chain_does_not_cross_unmerged_cell():
    result = build_table_map(_payload(_table(_row(
        _cell("A", horizontal="restart"), _cell("B"),
        _cell("C", horizontal="continue"),
    ), columns=3)))
    assert result["tables"][0]["rows"][0]["cells"][2]["issues"] == [
        "orphan_horizontal_merge",
    ]


@pytest.mark.parametrize("field,value", [
    ("span", "0"), ("span", "-1"), ("span", "1.5"), ("span", " 2"),
    ("span", ""), ("horizontal", "other"), ("vertical", ""),
])
def test_malformed_geometry_refuses_instead_of_guessing(field, value):
    with pytest.raises(TableMapError) as caught:
        build_table_map(_payload(_table(_row(_cell("A", **{field: value})))))
    assert caught.value.code == "table_structure_unsupported"


def test_duplicate_geometry_refuses():
    cell = _cell("A", span="1")
    etree.SubElement(cell.find(w("tcPr")), w("gridSpan")).set(w("val"), "2")
    with pytest.raises(TableMapError, match="duplicate current table geometry"):
        build_table_map(_payload(_table(_row(cell), columns=1)))


def test_nested_tables_preserve_order_and_do_not_duplicate_paragraphs():
    outer = _cell("Before inner", "After inner")
    nested = _table(_row(_cell("Inner", ""), _cell("")))
    outer.insert(1, nested)
    result = build_table_map(_payload(_table(_row(outer), columns=1)))
    first, second = result["tables"]
    cell = first["rows"][0]["cells"][0]
    assert second["parent_cell_ref"] == cell["cell_ref"]
    assert first["nesting_depth"] == 1 and second["nesting_depth"] == 2
    assert cell["nested_table_refs"] == [second["table_ref"]]
    assert [x["kind"] for x in cell["content"]] == ["paragraph", "table", "paragraph"]
    assert [r["paragraph_index"] for r in cell["paragraph_refs"]] == [0, 4]
    refs = [
        p for t in result["tables"] for r in t["rows"]
        for c in r["cells"] for p in c["paragraph_refs"]
    ]
    assert Counter(r["paragraph_index"] for r in refs) == Counter(range(5))
    assert result["counts"]["table_paragraphs"] == 5
    assert result["counts"]["nested_tables"] == 1
    assert result["coverage"]["unmapped_table_cell_paragraph_count"] == 0
    assert not first["simple_rectangular"] and second["simple_rectangular"]


def test_table_cell_without_final_paragraph_is_not_claimed_simple():
    outer = etree.Element(w("tc"))
    outer.append(_table(_row(_cell("Nested")), columns=1))
    result = build_table_map(_payload(_table(_row(outer), columns=1)))
    assert result["tables"][0]["rows"][0]["cells"][0]["issues"] == [
        "cell_has_no_canonical_paragraph", "cell_lacks_final_canonical_paragraph",
    ]
    assert not result["coverage"]["complete"]


def test_content_controls_preserve_canonical_refs_but_are_visible():
    table = _table(columns=1)
    wrapper = etree.SubElement(table, w("sdt"))
    content = etree.SubElement(wrapper, w("sdtContent"))
    row = etree.SubElement(content, w("tr"))
    cell_wrapper = etree.SubElement(row, w("sdt"))
    etree.SubElement(cell_wrapper, w("sdtContent")).append(_cell("Wrapped", ""))
    result = build_table_map(_payload(table))
    assert result["tables"][0]["issues"] == ["wrapped_rows"]
    assert result["tables"][0]["rows"][0]["issues"] == ["wrapped_cells"]
    assert result["counts"]["table_paragraphs"] == 2
    assert not result["tables"][0]["simple_rectangular"]


def test_unknown_container_does_not_sneak_table_into_canonical_inventory():
    wrapper = etree.Element("{urn:synthetic:unknown}wrapper")
    wrapper.append(_table(_row(_cell("Hidden"), _cell("Hidden"))))
    result = build_table_map(_payload(_two_rows(), wrapper))
    assert result["counts"]["tables"] == 1
    assert result["coverage"]["excluded_table_count"] == 1
    assert not result["coverage"]["complete"]
    assert result["coverage"]["canonical_body_flow"]["excluded_paragraph_count"] == 2


def test_unknown_cell_content_is_not_claimed_complete():
    cell = _cell("Known")
    wrapper = etree.SubElement(cell, "{urn:synthetic:unknown}wrapper")
    wrapper.append(_p("Unknown"))
    result = build_table_map(_payload(_table(_row(cell), columns=1)))
    assert result["counts"]["table_paragraphs"] == 1
    assert "excluded_cell_content" in result["tables"][0]["rows"][0]["cells"][0]["issues"]
    assert not result["coverage"]["complete"]


def test_malformed_nested_cell_is_visible_and_does_not_duplicate_paragraphs():
    outer = _cell("Outer")
    outer.append(_cell("Unexpected inner cell"))
    result = build_table_map(_payload(_table(_row(outer), columns=1)))
    cell = result["tables"][0]["rows"][0]["cells"][0]
    assert cell["issues"] == ["unsupported_cell_block_children"]
    assert result["coverage"]["unmapped_table_cell_paragraph_count"] == 1
    assert not result["coverage"]["complete"]
    assert not result["tables"][0]["simple_rectangular"]


def test_wrapped_cell_paragraphs_still_have_canonical_order():
    cell = _cell("First")
    wrapper = etree.SubElement(cell, w("sdt"))
    content = etree.SubElement(wrapper, w("sdtContent"))
    content.extend([_p("Second"), _p("")])
    result = build_table_map(_payload(_table(_row(cell), columns=1)))
    item = result["tables"][0]["rows"][0]["cells"][0]
    assert [r["paragraph_index"] for r in item["paragraph_refs"]] == [0, 1, 2]
    assert item["issues"] == ["wrapped_cell_content"]
    assert not result["tables"][0]["simple_rectangular"]


def test_row_revision_is_disclosed_separately_from_rectangular_geometry():
    table = _two_rows()
    row = table.find(w("tr"))
    properties = etree.Element(w("trPr"))
    row.insert(0, properties)
    revision = etree.SubElement(properties, w("ins"))
    revision.set(w("id"), "10")
    revision.set(w("author"), "Synthetic Reviewer")
    item = build_table_map(_payload(table))["tables"][0]
    assert item["has_structural_revision_markers"]
    assert item["structural_revision_marker_counts"] == {"ins": 1}
    assert item["simple_rectangular"]
    assert item["mutation_safety"] == "not_assessed"


def test_current_grid_ignores_historical_geometry_but_exposes_revisions():
    table = _table(_row(_cell("Current")), columns=1)
    change = etree.SubElement(table.find(w("tblGrid")), w("tblGridChange"))
    change.set(w("id"), "1")
    change.set(w("author"), "Synthetic Reviewer")
    old_grid = etree.SubElement(change, w("tblGrid"))
    for _ in range(3):
        etree.SubElement(old_grid, w("gridCol"))
    cell = table.find(w("tr") + "/" + w("tc"))
    properties = etree.Element(w("tcPr"))
    cell.insert(0, properties)
    change = etree.SubElement(properties, w("tcPrChange"))
    change.set(w("id"), "2")
    change.set(w("author"), "Synthetic Reviewer")
    etree.SubElement(etree.SubElement(change, w("tcPr")), w("gridSpan")).set(w("val"), "3")
    item = build_table_map(_payload(table))["tables"][0]
    assert item["grid_column_count"] == 1
    assert item["rows"][0]["cells"][0]["grid_span"] == 1
    assert item["tracked_property_revision_counts"] == {"tblGridChange": 1, "tcPrChange": 1}
    assert item["has_tracked_property_revisions"]
    assert item["simple_rectangular"]  # Geometry only; never a mutation promise.
    assert item["mutation_safety"] == "not_assessed"


def test_empty_cells_are_real_geometry_even_when_text_is_identical():
    first = _payload(_table(_row(_cell(""), _cell(""))))
    second = _payload(_table(_row(_cell("", "")), columns=1))
    maps = [build_table_map(p) for p in (first, second)]
    assert maps[0]["counts"]["table_paragraphs"] == maps[1]["counts"]["table_paragraphs"] == 2
    assert [m["counts"]["cells"] for m in maps] == [2, 1]
    assert maps[0]["tables"][0]["table_ref"]["element_sha256"] != (
        maps[1]["tables"][0]["table_ref"]["element_sha256"]
    )


def test_all_three_reference_types_resolve_exact_inventory_items():
    payload = _payload(_two_rows())
    result = build_table_map(payload)
    for ref in _all_refs(result):
        item = resolve_table_ref(payload, ref)
        assert item[f"{ref['ref_type']}_ref"] == ref
    assert build_table_map(payload) == result


def test_repacked_bytes_stale_refs_even_with_unchanged_xml():
    original = _payload(_two_rows())
    changed = _payload(_two_rows(), comment=b"Repacked")
    ref = build_table_map(original)["tables"][0]["table_ref"]
    assert ref["element_sha256"] == build_table_map(changed)["tables"][0]["table_ref"]["element_sha256"]
    with pytest.raises(TableMapError) as caught:
        resolve_table_ref(changed, ref)
    assert caught.value.code == "table_reference_mismatch"


@pytest.mark.parametrize("change", ["fingerprint", "index", "hash"])
def test_wrong_target_or_digest_refuses(change):
    payload = _payload(_two_rows())
    ref = copy.deepcopy(build_table_map(payload)["tables"][0]["rows"][0]["cells"][0]["cell_ref"])
    if change == "fingerprint":
        ref["element_sha256"] = "0" * 64
    elif change == "index":
        ref["row_index"] = 100
    else:
        ref["file_sha256"] = "f" * 64
    with pytest.raises(TableMapError) as caught:
        resolve_table_ref(payload, ref)
    assert caught.value.code == "table_reference_mismatch"


@pytest.mark.parametrize("mutation", [
    {"xpath": ".//w:tc"}, {"table_index": True}, {"row_index": 0},
    {"table_index": -1}, {"table_index": 0.0}, {"element_sha256": "x" * 64},
    {"schema_version": "table_ref.v2"}, {"part_name": "word/header1.xml"},
    {"container_policy": "other"}, {"fingerprint_policy": "other"},
])
def test_reference_shape_is_closed_and_never_accepts_xpath(mutation):
    payload = _payload(_two_rows())
    ref = {**build_table_map(payload)["tables"][0]["table_ref"], **mutation}
    with pytest.raises(TableMapError) as caught:
        resolve_table_ref(payload, ref)
    assert caught.value.code == "invalid_table_reference"


@pytest.mark.parametrize("invalid", [None, [], "table", {"ref_type": []}])
def test_non_reference_inputs_fail_cleanly(invalid):
    with pytest.raises(TableMapError) as caught:
        resolve_table_ref(b"", invalid)
    assert caught.value.code == "invalid_table_reference"


@pytest.mark.parametrize("invalid", [None, "", bytearray(b""), memoryview(b"")])
def test_non_bytes_payload_refuses(invalid):
    with pytest.raises(TableMapError) as caught:
        build_table_map(invalid)
    assert caught.value.code == "invalid_table_map_input"


@pytest.mark.parametrize("limit,payload_factory,allowed", [
    ("tables", lambda: _payload(_two_rows(), _two_rows()), 1),
    ("rows", lambda: _payload(_two_rows()), 1),
    ("cells", lambda: _payload(_two_rows()), 3),
    ("grid_columns", lambda: _payload(_two_rows()), 1),
    ("fingerprint_bytes", lambda: _payload(_two_rows()), 1),
    ("result_bytes", lambda: _payload(_two_rows()), 1),
])
def test_limits_refuse_whole_inventory_including_empty_cells(monkeypatch, limit, payload_factory, allowed):
    monkeypatch.setattr(table_map, "TABLE_MAP_LIMITS", {
        **table_map.TABLE_MAP_LIMITS, limit: allowed,
    })
    with pytest.raises(ResourceLimitError) as caught:
        build_table_map(payload_factory())
    assert caught.value.limit == f"table_map_{limit}"


def test_limit_boundary_is_inclusive(monkeypatch):
    monkeypatch.setattr(table_map, "TABLE_MAP_LIMITS", {
        **table_map.TABLE_MAP_LIMITS, "tables": 1, "rows": 2, "cells": 4,
        "grid_columns": 2,
    })
    assert build_table_map(_payload(_two_rows()))["counts"]["cells"] == 4


def test_nested_table_depth_limit(monkeypatch):
    outer = _cell("Outer")
    outer.insert(0, _table(_row(_cell("Inner")), columns=1))
    monkeypatch.setattr(table_map, "TABLE_MAP_LIMITS", {
        **table_map.TABLE_MAP_LIMITS, "table_nesting_depth": 1,
    })
    with pytest.raises(ResourceLimitError) as caught:
        build_table_map(_payload(_table(_row(outer), columns=1)))
    assert caught.value.limit == "table_map_table_nesting_depth"
