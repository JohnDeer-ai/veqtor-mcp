# SPDX-License-Identifier: Apache-2.0
"""extract_redlines against the synthetic rounds: every fact must be exact."""

import json
import subprocess
import sys
import zipfile
from pathlib import Path

from lxml import etree

from veqtor_docx import extract_redlines, inspect_document, verify_quote
from veqtor_docx._ooxml import MC_NS, parse_xml, w
from veqtor_docx.contracts import (
    EXTRACT_REVISION_CATEGORIES_V1,
    REVISION_COUNT_BASIS_V1,
    TEXT_REVISION_SUFFIX_BY_NAME_V1,
)
from veqtor_docx.synthetic import (
    ADVISER_SENTENCE,
    AUDIT_SENTENCE,
    AUTHOR_CP,
    AUTHOR_US,
    CAP_R1,
    CAP_R2,
    CAP_R3,
    CAP_R4,
    CARVEOUT_DROPPED,
    EXPRESS_ROW_FEE,
    EXPRESS_ROW_LABEL,
)


def _round(demo_dir: Path, number: int) -> dict:
    path = sorted(demo_dir.glob("*.docx"))[number - 1]
    return extract_redlines(str(path))


def _rewrite_document(source: Path, target: Path, mutate) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as output:
        for info in original.infolist():
            payload = original.read(info)
            if info.filename == "word/document.xml":
                document = parse_xml(payload)
                mutate(document)
                payload = (
                    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
                    + etree.tostring(document)
                )
            output.writestr(info, payload)


def _run(parent: etree._Element, text: str) -> etree._Element:
    run = etree.SubElement(parent, w("r"))
    node = etree.SubElement(run, w("t"))
    node.text = text
    return run


def _insertion(parent: etree._Element, revision_id: str, text: str) -> etree._Element:
    insertion = etree.SubElement(parent, w("ins"))
    insertion.set(w("id"), revision_id)
    insertion.set(w("author"), "Context Test")
    _run(insertion, text)
    return insertion


def _append_body_block(body: etree._Element, block: etree._Element) -> None:
    section = body.find(w("sectPr"))
    if section is None:
        body.append(block)
    else:
        section.addprevious(block)


def _revision_with_no_break_hyphen(
    parent: etree._Element,
    kind: str,
    revision_id: str,
    left: str,
    right: str,
) -> etree._Element:
    revision = etree.SubElement(parent, w(kind))
    revision.set(w("id"), revision_id)
    revision.set(w("author"), "Context Test")
    run = etree.SubElement(revision, w("r"))
    text_tag = w("t") if kind == "ins" else w("delText")
    first = etree.SubElement(run, text_tag)
    first.text = left
    etree.SubElement(run, w("noBreakHyphen"))
    second = etree.SubElement(run, text_tag)
    second.text = right
    return revision


def test_package_import_does_not_require_optional_lzma_extension() -> None:
    script = """
import importlib.abc
import sys

class BlockLzma(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"lzma", "_lzma"}:
            raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None

sys.meta_path.insert(0, BlockLzma())
sys.modules.pop("lzma", None)
sys.modules.pop("_lzma", None)
sys.modules.pop("zipfile", None)
import veqtor_docx
import zipfile
assert zipfile.lzma is None
assert callable(veqtor_docx.extract_redlines)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_extract_revision_categories_v1_are_frozen() -> None:
    assert dict(TEXT_REVISION_SUFFIX_BY_NAME_V1) == {
        "ins": "Ins",
        "del": "Del",
    }
    assert EXTRACT_REVISION_CATEGORIES_V1 == {
        "moveFrom",
        "moveTo",
        "rPrChange",
        "pPrChange",
        "tblPrChange",
        "trPrChange",
        "tcPrChange",
        "sectPrChange",
        "numberingChange",
        "cellIns",
        "cellDel",
        "paragraphMarkIns",
        "paragraphMarkDel",
        "trPrIns",
        "trPrDel",
        "tcPrIns",
        "tcPrDel",
        "tblPrIns",
        "tblPrDel",
        "sectPrIns",
        "sectPrDel",
    }


def test_clean_round_has_no_changes(demo_dir: Path) -> None:
    result = _round(demo_dir, 1)
    assert result["change_units"] == []
    assert result["revision_count"] == 0
    assert result["revision_count_basis"] == REVISION_COUNT_BASIS_V1
    assert result["unsupported_revisions"] == {}
    assert result["revision_inventory"] == {
        "schema_version": "revision_inventory.v2",
        "scope": "word/document.xml",
        "container_policy": {
            "schema_version": "canonical_body_flow_v1",
            "indexed_paragraph_count": 65,
            "body_paragraph_count": 55,
            "table_cell_paragraph_count": 10,
            "excluded_subtree_count": 0,
            "excluded_paragraph_count": 0,
            "excluded_by_kind": {},
            "excluded_paragraphs_by_kind": {},
            "coverage_complete": True,
            "legacy_two_field_anchor_safe": True,
        },
        "tracked_text_revision_elements": 0,
        "total_revision_elements": 0,
        "in_scope_revision_elements": 0,
        "decoded_revision_elements": 0,
        "unsupported_revision_occurrences": 0,
        "unsupported_revision_kind_count": 0,
        "excluded_container_occurrences": 0,
        "excluded_container_kind_count": 0,
        "emitted_change_unit_count": 0,
        "unsupported_by_kind": {},
        "excluded_by_container": {},
        "partition_valid": True,
        "all_in_scope_revision_elements_decoded": True,
        "all_revision_elements_decoded": True,
    }


def test_round2_units(demo_dir: Path) -> None:
    result = _round(demo_dir, 2)
    units = result["change_units"]
    assert [u["change_type"] for u in units] == [
        "replace",
        "insert",
        "insert",
        "delete",
        "insert",
        "replace",
    ]
    assert {u["author"] for u in units} == {AUTHOR_CP}
    assert {u["date"] for u in units} == {"2026-05-05T09:30:00Z"}

    table_cell, row_label, row_fee, audit, adviser, cap = units
    for unit in units:
        context = unit["paragraph_context"]
        assert len(context["before"]) <= 240
        assert len(context["after"]) <= 240
        assert type(context["truncated_before"]) is bool
        assert type(context["truncated_after"]) is bool
        assert type(unit["reference"]["paragraph_index"]) is int
        assert type(unit["reference"]["group_index"]) is int
    # Tracked change inside a table cell is anchored to the numbered subclause.
    assert (table_cell["old_text"], table_cell["new_text"]) == ("50", "65")
    assert table_cell["clause_anchor"] == {
        "label": "3.3",
        "heading": "Cancellation Charges",
    }

    # An inserted table row yields insert units for its cell content and a
    # structural trPrIns fact (asserted below).
    assert row_label["new_text"] == EXPRESS_ROW_LABEL
    assert row_fee["new_text"] == EXPRESS_ROW_FEE
    assert row_label["clause_anchor"]["label"] == "3.3"

    assert audit["old_text"] == AUDIT_SENTENCE
    assert audit["new_text"] is None
    assert audit["clause_anchor"] == {"label": "7", "heading": "Records and Audit"}

    # One logical insertion split by Word into two adjacent w:ins wrappers
    # must come back as a single unit carrying both revision ids.
    assert adviser["new_text"] == ADVISER_SENTENCE
    assert adviser["old_text"] is None
    assert len(adviser["reference"]["revision_ids"]) == 2
    assert adviser["clause_anchor"] == {
        "label": "9.1",
        "heading": "Confidentiality Obligations",
    }

    assert (cap["old_text"], cap["new_text"]) == (CAP_R1, CAP_R2)
    assert cap["clause_anchor"] == {
        "label": "14.2",
        "heading": "Limitation of Liability",
    }

    # Formatting-only and structural revisions are reported, never dropped:
    # the row insertion marker and the inserted paragraph marks of its cells.
    assert result["unsupported_revisions"] == {
        "rPrChange": 1,
        "trPrIns": 1,
        "paragraphMarkIns": 2,
    }
    assert result["revision_count"] == 12
    assert result["revision_count_basis"] == REVISION_COUNT_BASIS_V1
    inventory = result["revision_inventory"]
    assert inventory["total_revision_elements"] == 13
    assert inventory["decoded_revision_elements"] == 9
    assert inventory["unsupported_revision_occurrences"] == 4
    assert inventory["unsupported_revision_kind_count"] == 3
    assert inventory["emitted_change_unit_count"] == 6
    assert inventory["unsupported_by_kind"] == result["unsupported_revisions"]
    assert inventory["partition_valid"] is True
    assert inventory["all_revision_elements_decoded"] is False
    assert inventory["total_revision_elements"] == (
        inventory["decoded_revision_elements"]
        + inventory["unsupported_revision_occurrences"]
    )


def test_round3_units(demo_dir: Path) -> None:
    result = _round(demo_dir, 3)
    units = result["change_units"]
    assert [u["change_type"] for u in units] == ["insert", "replace", "insert"]
    assert {u["author"] for u in units} == {AUTHOR_US}

    compelled, cap, carveout = units
    # A manually numbered inserted clause anchors to itself.
    assert compelled["clause_anchor"] == {
        "label": "9.5",
        "heading": "Compelled Disclosure",
    }
    assert compelled["new_text"].startswith("9.5 Compelled Disclosure.")
    assert compelled["paragraph_context"] == {
        "before": "",
        "after": "",
        "manual_label": "9.5",
        "truncated_before": False,
        "truncated_after": False,
    }

    assert (cap["old_text"], cap["new_text"]) == (CAP_R2, CAP_R3)
    assert carveout["change_type"] == "insert"
    assert carveout["clause_anchor"]["label"] == "14.2"

    assert result["unsupported_revisions"] == {
        "moveTo": 1,
        "moveFrom": 1,
        "paragraphMarkIns": 1,
        "pPrChange": 1,
    }
    inventory = result["revision_inventory"]
    assert inventory["total_revision_elements"] == 8
    assert inventory["decoded_revision_elements"] == 4
    assert inventory["unsupported_revision_occurrences"] == 4
    assert inventory["unsupported_revision_kind_count"] == 4
    assert inventory["emitted_change_unit_count"] == 3
    assert inventory["unsupported_by_kind"] == result["unsupported_revisions"]
    assert inventory["partition_valid"] is True
    assert inventory["all_revision_elements_decoded"] is False


def test_round4_units(demo_dir: Path) -> None:
    result = _round(demo_dir, 4)
    units = result["change_units"]
    assert [u["change_type"] for u in units] == ["replace", "delete"]
    cap, dropped = units
    assert (cap["old_text"], cap["new_text"]) == (CAP_R3, CAP_R4)
    assert dropped["old_text"] == CARVEOUT_DROPPED
    assert {u["author"] for u in units} == {AUTHOR_CP}


def test_revision_inventory_partitions_mixed_revision_markup_once(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "mixed-revision-inventory.docx"

    def add_revision_markup(document: etree._Element) -> None:
        body = document.find(w("body"))
        assert body is not None
        paragraph = next(body.iter(w("p")))
        ppr = paragraph.find(w("pPr"))
        if ppr is None:
            ppr = etree.Element(w("pPr"))
            paragraph.insert(0, ppr)
        paragraph_mark_properties = etree.SubElement(ppr, w("rPr"))
        etree.SubElement(paragraph_mark_properties, w("ins"))
        etree.SubElement(paragraph_mark_properties, w("rPrChange"))

        table = next(body.iter(w("tbl")))
        table_properties = table.find(w("tblPr"))
        if table_properties is None:
            table_properties = etree.Element(w("tblPr"))
            table.insert(0, table_properties)
        etree.SubElement(table_properties, w("tblPrChange"))

        row = next(table.iter(w("tr")))
        row_properties = row.find(w("trPr"))
        if row_properties is None:
            row_properties = etree.Element(w("trPr"))
            row.insert(0, row_properties)
        etree.SubElement(row_properties, w("del"))

        for kind in ("moveFrom", "moveTo"):
            move = etree.SubElement(paragraph, w(kind))
            move.set(w("id"), f"mixed-{kind}")
            move.set(w("author"), "Inventory Test")
            _run(move, kind)
        _insertion(paragraph, "mixed-ins", "decoded text")

    _rewrite_document(source, target, add_revision_markup)
    result = extract_redlines(str(target))
    inventory = result["revision_inventory"]

    assert inventory == {
        "schema_version": "revision_inventory.v2",
        "scope": "word/document.xml",
        "container_policy": {
            "schema_version": "canonical_body_flow_v1",
            "indexed_paragraph_count": 65,
            "body_paragraph_count": 55,
            "table_cell_paragraph_count": 10,
            "excluded_subtree_count": 0,
            "excluded_paragraph_count": 0,
            "excluded_by_kind": {},
            "excluded_paragraphs_by_kind": {},
            "coverage_complete": True,
            "legacy_two_field_anchor_safe": True,
        },
        "tracked_text_revision_elements": 3,
        "total_revision_elements": 7,
        "in_scope_revision_elements": 7,
        "decoded_revision_elements": 1,
        "unsupported_revision_occurrences": 6,
        "unsupported_revision_kind_count": 6,
        "excluded_container_occurrences": 0,
        "excluded_container_kind_count": 0,
        "emitted_change_unit_count": 1,
        "unsupported_by_kind": {
            "paragraphMarkIns": 1,
            "rPrChange": 1,
            "moveFrom": 1,
            "moveTo": 1,
            "tblPrChange": 1,
            "trPrDel": 1,
        },
        "excluded_by_container": {},
        "partition_valid": True,
        "all_in_scope_revision_elements_decoded": False,
        "all_revision_elements_decoded": False,
    }
    assert result["revision_count"] == 3
    assert result["revision_count_basis"] == REVISION_COUNT_BASIS_V1
    assert result["unsupported_revisions"] == inventory["unsupported_by_kind"]


def test_canonical_body_flow_passes_through_block_and_table_sdt(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "sdt-body-flow.docx"

    def add_content_controls(document: etree._Element) -> None:
        body = document.find(w("body"))
        assert body is not None

        body_sdt = etree.Element(w("sdt"))
        body_content = etree.SubElement(body_sdt, w("sdtContent"))
        body_paragraph = etree.SubElement(body_content, w("p"))
        _insertion(body_paragraph, "801", "Body SDT revision")
        _append_body_block(body, body_sdt)

        cell = next(body.iter(w("tc")))
        cell_sdt = etree.SubElement(cell, w("sdt"))
        cell_content = etree.SubElement(cell_sdt, w("sdtContent"))
        cell_paragraph = etree.SubElement(cell_content, w("p"))
        _insertion(cell_paragraph, "802", "Table SDT revision")

    _rewrite_document(source, target, add_content_controls)
    result = extract_redlines(str(target))
    units = {
        unit["new_text"]: unit
        for unit in result["change_units"]
        if unit["new_text"] in {"Body SDT revision", "Table SDT revision"}
    }

    assert set(units) == {"Body SDT revision", "Table SDT revision"}
    assert units["Body SDT revision"]["reference"]["container_kind"] == "body"
    assert units["Table SDT revision"]["reference"]["container_kind"] == "table_cell"
    policy = result["revision_inventory"]["container_policy"]
    assert policy == {
        "schema_version": "canonical_body_flow_v1",
        "indexed_paragraph_count": 67,
        "body_paragraph_count": 56,
        "table_cell_paragraph_count": 11,
        "excluded_subtree_count": 0,
        "excluded_paragraph_count": 0,
        "excluded_by_kind": {},
        "excluded_paragraphs_by_kind": {},
        "coverage_complete": True,
        "legacy_two_field_anchor_safe": True,
    }


def test_alternate_content_is_pruned_and_revisions_form_closed_v2_partition(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "alternate-content.docx"

    def add_alternate_content(document: etree._Element) -> None:
        body = document.find(w("body"))
        assert body is not None
        outer = etree.Element(w("p"))
        _insertion(outer, "810", "VISIBLE BODY TEXT")
        run = etree.SubElement(outer, w("r"))
        alternate = etree.SubElement(run, f"{{{MC_NS}}}AlternateContent")
        for branch_name, revision_id in (("Choice", "811"), ("Fallback", "812")):
            branch = etree.SubElement(alternate, f"{{{MC_NS}}}{branch_name}")
            text_box = etree.SubElement(branch, w("txbxContent"))
            nested = etree.SubElement(text_box, w("p"))
            _insertion(nested, revision_id, "TEXT BOX ONLY")
        _append_body_block(body, outer)

    _rewrite_document(source, target, add_alternate_content)
    result = extract_redlines(str(target))
    units = [
        unit
        for unit in result["change_units"]
        if unit["new_text"] == "VISIBLE BODY TEXT"
    ]

    assert len(units) == 1
    assert "TEXT BOX ONLY" not in units[0]["paragraph_context"]["before"]
    assert "TEXT BOX ONLY" not in units[0]["paragraph_context"]["after"]
    assert units[0]["anchor"] == {
        "schema_version": "change_unit_anchor.v2",
        "change_unit_id": units[0]["change_unit_id"],
        "file_sha256": result["file_sha256"],
        "container_policy": "canonical_body_flow_v1",
        "unit_fingerprint_sha256": units[0]["anchor"]["unit_fingerprint_sha256"],
    }

    inventory = result["revision_inventory"]
    assert result["revision_count"] == 3
    assert result["unsupported_revisions"] == {}
    assert inventory["tracked_text_revision_elements"] == 3
    assert inventory["total_revision_elements"] == 3
    assert inventory["in_scope_revision_elements"] == 1
    assert inventory["decoded_revision_elements"] == 1
    assert inventory["unsupported_revision_occurrences"] == 0
    assert inventory["excluded_container_occurrences"] == 2
    assert inventory["excluded_by_container"] == {"alternate_content": 2}
    assert inventory["emitted_change_unit_count"] == 1
    assert inventory["partition_valid"] is True
    assert inventory["all_in_scope_revision_elements_decoded"] is True
    assert inventory["all_revision_elements_decoded"] is False
    assert inventory["total_revision_elements"] == (
        inventory["in_scope_revision_elements"]
        + inventory["excluded_container_occurrences"]
    )
    assert inventory["in_scope_revision_elements"] == (
        inventory["decoded_revision_elements"]
        + inventory["unsupported_revision_occurrences"]
    )
    assert inventory["container_policy"] == {
        "schema_version": "canonical_body_flow_v1",
        "indexed_paragraph_count": 66,
        "body_paragraph_count": 56,
        "table_cell_paragraph_count": 10,
        "excluded_subtree_count": 1,
        "excluded_paragraph_count": 2,
        "excluded_by_kind": {"alternate_content": 1},
        "excluded_paragraphs_by_kind": {"alternate_content": 2},
        "coverage_complete": False,
        "legacy_two_field_anchor_safe": False,
    }


def test_unknown_block_container_is_one_fail_visible_exclusion_root(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "unknown-block-container.docx"
    unknown_tag = "{urn:veqtor:test:unknown}container"

    def add_unknown_container(document: etree._Element) -> None:
        body = document.find(w("body"))
        assert body is not None
        unknown = etree.Element(unknown_tag)
        direct_run = etree.SubElement(unknown, w("r"))
        direct_text = etree.SubElement(direct_run, w("t"))
        direct_text.text = "UNKNOWN DIRECT TEXT"
        property_revision = etree.SubElement(unknown, w("rPrChange"))
        property_revision.set(w("id"), "820")
        for revision_id in ("821", "822"):
            paragraph = etree.SubElement(unknown, w("p"))
            _insertion(paragraph, revision_id, "UNKNOWN PARAGRAPH TEXT")
        _append_body_block(body, unknown)

    _rewrite_document(source, target, add_unknown_container)
    result = extract_redlines(str(target))
    inventory = result["revision_inventory"]
    policy = inventory["container_policy"]

    assert result["change_units"] == []
    assert policy["excluded_subtree_count"] == 1
    assert policy["excluded_paragraph_count"] == 2
    assert policy["excluded_by_kind"] == {"unknown_container": 1}
    assert policy["excluded_paragraphs_by_kind"] == {"unknown_container": 2}
    assert policy["coverage_complete"] is False
    assert policy["legacy_two_field_anchor_safe"] is False
    assert inventory["total_revision_elements"] == 3
    assert inventory["in_scope_revision_elements"] == 0
    assert inventory["decoded_revision_elements"] == 0
    assert inventory["unsupported_revision_occurrences"] == 0
    assert inventory["excluded_container_occurrences"] == 3
    assert inventory["unsupported_by_kind"] == {}
    assert inventory["excluded_by_container"] == {"unknown_container": 3}
    assert inventory["partition_valid"] is True

    inspected = inspect_document(
        str(target),
        "literal_search",
        phrases=["UNKNOWN DIRECT TEXT", "UNKNOWN PARAGRAPH TEXT"],
        match_basis="exact_literal",
    )
    assert inspected["matches"] == []
    assert inspected["coverage"]["container_coverage"] == policy
    assert inspected["revision_inventory"] == {
        key: value
        for key, value in inventory.items()
        if key != "emitted_change_unit_count"
    }


def test_unknown_inline_container_with_only_property_revision_is_excluded(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "unknown-inline-property-revision.docx"
    unknown_tag = "{urn:veqtor:test:unknown}container"

    def add_unknown_inline_container(document: etree._Element) -> None:
        body = document.find(w("body"))
        assert body is not None
        paragraph = next(body.iter(w("p")))
        unknown = etree.SubElement(paragraph, unknown_tag)
        property_revision = etree.SubElement(unknown, w("rPrChange"))
        property_revision.set(w("id"), "823")

    _rewrite_document(source, target, add_unknown_inline_container)
    inventory = extract_redlines(str(target))["revision_inventory"]
    policy = inventory["container_policy"]

    assert policy["excluded_subtree_count"] == 1
    assert policy["excluded_paragraph_count"] == 0
    assert policy["excluded_by_kind"] == {"unknown_container": 1}
    assert policy["excluded_paragraphs_by_kind"] == {}
    assert policy["coverage_complete"] is False
    assert policy["legacy_two_field_anchor_safe"] is False
    assert inventory["total_revision_elements"] == 1
    assert inventory["in_scope_revision_elements"] == 0
    assert inventory["unsupported_revision_occurrences"] == 0
    assert inventory["excluded_container_occurrences"] == 1
    assert inventory["unsupported_by_kind"] == {}
    assert inventory["excluded_by_container"] == {"unknown_container": 1}


def test_known_property_subtree_with_text_revision_is_fail_visible(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "property-text-payload.docx"

    def add_illegal_property_payload(document: etree._Element) -> None:
        body = document.find(w("body"))
        assert body is not None
        paragraph = next(body.iter(w("p")))
        properties = paragraph.find(w("pPr"))
        if properties is None:
            properties = etree.Element(w("pPr"))
            paragraph.insert(0, properties)
        insertion = etree.SubElement(properties, w("ins"))
        insertion.set(w("id"), "824")
        insertion.set(w("author"), "Malformed Property Test")
        _run(insertion, "PROPERTY TEXT MUST NOT BE READ")

    _rewrite_document(source, target, add_illegal_property_payload)
    result = extract_redlines(str(target))
    inventory = result["revision_inventory"]
    policy = inventory["container_policy"]

    assert result["change_units"] == []
    assert policy["excluded_subtree_count"] == 1
    assert policy["excluded_by_kind"] == {"unknown_container": 1}
    assert policy["coverage_complete"] is False
    assert policy["legacy_two_field_anchor_safe"] is False
    assert inventory["total_revision_elements"] == 1
    assert inventory["in_scope_revision_elements"] == 0
    assert inventory["decoded_revision_elements"] == 0
    assert inventory["unsupported_revision_occurrences"] == 0
    assert inventory["excluded_container_occurrences"] == 1
    assert inventory["excluded_by_container"] == {"unknown_container": 1}
    assert inventory["partition_valid"] is True

    inspected = inspect_document(
        str(target),
        "literal_search",
        phrases=["PROPERTY TEXT MUST NOT BE READ"],
        match_basis="exact_literal",
    )
    assert inspected["matches"] == []
    assert inspected["coverage"]["container_coverage"] == policy


def test_manual_paragraph_label_is_distinct_from_heading_anchor(
    demo_dir: Path,
) -> None:
    result = _round(demo_dir, 3)
    compelled = result["change_units"][0]

    assert compelled["paragraph_context"]["manual_label"] == "9.5"
    assert compelled["clause_anchor"] == {
        "label": "9.5",
        "heading": "Compelled Disclosure",
    }
    assert all(
        unit["paragraph_context"]["manual_label"] is None
        for unit in result["change_units"][1:]
    )


def test_repeated_insertions_use_element_offsets_not_text_search(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-2-counterparty-redline.docx"
    target = tmp_path / "repeated-context.docx"

    def add_repeated_paragraph(document: etree._Element) -> None:
        body = document.find(w("body"))
        assert body is not None
        paragraph = etree.Element(w("p"))
        _run(paragraph, "Prefix ")
        _insertion(paragraph, "900", "SAME")
        _run(paragraph, " middle ")
        _insertion(paragraph, "901", "SAME")
        _run(paragraph, " suffix")
        body.insert(max(0, len(body) - 1), paragraph)

    _rewrite_document(source, target, add_repeated_paragraph)
    extracted = extract_redlines(str(target))
    first = next(
        unit
        for unit in extracted["change_units"]
        if unit["reference"]["revision_ids"] == ["900"]
    )
    second = next(
        unit
        for unit in extracted["change_units"]
        if unit["reference"]["revision_ids"] == ["901"]
    )

    assert first["paragraph_context"]["before"] == "Prefix "
    assert first["paragraph_context"]["after"] == " middle SAME suffix"
    assert second["paragraph_context"]["before"] == "Prefix SAME middle "
    assert second["paragraph_context"]["after"] == " suffix"


def test_tabs_and_breaks_share_one_reading_and_offset_model(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-2-counterparty-redline.docx"
    target = tmp_path / "structural-whitespace-context.docx"

    def add_structural_whitespace(document: etree._Element) -> None:
        body = document.find(w("body"))
        assert body is not None

        tabbed = etree.Element(w("p"))
        run = _run(tabbed, "5.2")
        etree.SubElement(run, w("tab"))
        text = etree.SubElement(run, w("t"))
        text.text = "Payment"
        etree.SubElement(run, w("tab"))
        tail = etree.SubElement(run, w("t"))
        tail.text = "before "
        _insertion(tabbed, "902", "CHANGED")
        _run(tabbed, " suffix")

        broken = etree.Element(w("p"))
        run = _run(broken, "6.3")
        etree.SubElement(run, w("br"))
        text = etree.SubElement(run, w("t"))
        text.text = "Payment "
        _insertion(broken, "903", "UPDATED")

        insert_at = max(0, len(body) - 1)
        body.insert(insert_at, tabbed)
        body.insert(insert_at + 1, broken)

    _rewrite_document(source, target, add_structural_whitespace)
    extracted = extract_redlines(str(target))
    tabbed = next(
        unit
        for unit in extracted["change_units"]
        if unit["reference"]["revision_ids"] == ["902"]
    )
    broken = next(
        unit
        for unit in extracted["change_units"]
        if unit["reference"]["revision_ids"] == ["903"]
    )

    assert tabbed["paragraph_context"] == {
        "before": "5.2\tPayment\tbefore ",
        "after": " suffix",
        "manual_label": "5.2",
        "truncated_before": False,
        "truncated_after": False,
    }
    assert broken["paragraph_context"] == {
        "before": "6.3\nPayment ",
        "after": "",
        "manual_label": "6.3",
        "truncated_before": False,
        "truncated_after": False,
    }


def test_no_break_hyphen_is_shared_by_context_revisions_and_verification(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-2-counterparty-redline.docx"
    target = tmp_path / "no-break-hyphen.docx"

    def add_no_break_hyphens(document: etree._Element) -> None:
        body = document.find(w("body"))
        assert body is not None

        plain = etree.Element(w("p"))
        run = _run(plain, "Alpha")
        etree.SubElement(run, w("noBreakHyphen"))
        tail = etree.SubElement(run, w("t"))
        tail.text = "Beta "
        _insertion(plain, "904", "Changed")
        _run(plain, " suffix")

        inserted = etree.Element(w("p"))
        _run(inserted, "Before ")
        _revision_with_no_break_hyphen(inserted, "ins", "905", "Insert", "Value")
        _run(inserted, " after")

        deleted = etree.Element(w("p"))
        _run(deleted, "Before ")
        _revision_with_no_break_hyphen(deleted, "del", "906", "Delete", "Value")
        _run(deleted, " after")

        insert_at = max(0, len(body) - 1)
        for offset, paragraph in enumerate((plain, inserted, deleted)):
            body.insert(insert_at + offset, paragraph)

    _rewrite_document(source, target, add_no_break_hyphens)
    extracted = extract_redlines(str(target))
    units = {
        unit["reference"]["revision_ids"][0]: unit
        for unit in extracted["change_units"]
        if unit["reference"]["revision_ids"]
        and unit["reference"]["revision_ids"][0] in {"904", "905", "906"}
    }

    assert units["904"]["paragraph_context"]["before"] == "Alpha-Beta "
    assert units["904"]["paragraph_context"]["after"] == " suffix"
    assert units["905"]["new_text"] == "Insert-Value"
    assert units["905"]["paragraph_context"]["before"] == "Before "
    assert units["905"]["paragraph_context"]["after"] == " after"
    assert units["906"]["old_text"] == "Delete-Value"
    assert units["906"]["paragraph_context"]["before"] == "Before "
    assert units["906"]["paragraph_context"]["after"] == " after"

    for revision_id, quote in (("905", "Insert-Value"), ("906", "Delete-Value")):
        unit = units[revision_id]
        verified = verify_quote(
            str(target),
            {
                "change_unit_id": unit["change_unit_id"],
                "file_sha256": extracted["file_sha256"],
            },
            quote,
        )
        assert verified["verdict"] == "exact"


def test_liability_timeline_chains_across_rounds(demo_dir: Path) -> None:
    """The demo story: each round's cap replaces exactly the previous value."""
    caps = []
    for number in (2, 3, 4):
        for unit in _round(demo_dir, number)["change_units"]:
            anchor = unit["clause_anchor"]
            if (
                unit["change_type"] == "replace"
                and anchor
                and anchor["label"] == "14.2"
            ):
                caps.append(unit)
    assert [(u["old_text"], u["new_text"]) for u in caps] == [
        (CAP_R1, CAP_R2),
        (CAP_R2, CAP_R3),
        (CAP_R3, CAP_R4),
    ]


def test_user_home_paths_are_expanded(demo_dir: Path, monkeypatch) -> None:
    """README tells users to point Claude at ~/veqtor-demo-rounds; a literal
    tilde must work and references must carry the openable expanded path."""
    monkeypatch.setenv("HOME", str(demo_dir.parent))
    tilde_path = f"~/{demo_dir.name}/round-2-counterparty-redline.docx"
    result = extract_redlines(tilde_path)
    assert result["change_units"]
    assert "~" not in result["path"]
    assert "~" not in result["change_units"][0]["reference"]["path"]


def test_references_are_deterministic_and_verifiable(demo_dir: Path) -> None:
    path = sorted(demo_dir.glob("*.docx"))[1]
    first = extract_redlines(str(path))
    second = extract_redlines(str(path))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    ids = [u["change_unit_id"] for u in first["change_units"]]
    assert ids == [f"cu_{i:03d}" for i in range(1, len(ids) + 1)]

    all_revision_ids = [
        rid for u in first["change_units"] for rid in u["reference"]["revision_ids"]
    ]
    assert len(all_revision_ids) == len(set(all_revision_ids))
    for unit in first["change_units"]:
        assert unit["file_sha256"] == first["file_sha256"]
        assert unit["reference"]["part_name"] == "word/document.xml"
        assert unit["reference"]["path"] == str(path)
