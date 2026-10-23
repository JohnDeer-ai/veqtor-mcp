# SPDX-License-Identifier: Apache-2.0
"""Independent two-column row presence, property and atomicity expectations."""
from __future__ import annotations

import copy
import hashlib
import zipfile
from pathlib import Path

import pytest
import jsonschema
from lxml import etree

from veqtor_docx import drafting
from veqtor_docx._ooxml import DOCUMENT_PART, W_NS, w
from veqtor_docx.revision_decisions import read_document_revisions
from veqtor_docx.table_map import build_table_map
from veqtor_mcp._drafting_contract import _APPLIED, REVISIONS_RESULT

PROFILE = {"author": "Fictional Row Counsel", "initials": "RC", "producer_build": "rows-test.v1"}
DATE = "2026-10-23T12:00:00+03:00"


def _p(text, column=0, numbered=False):
    paragraph = etree.Element(w("p"))
    props = etree.SubElement(paragraph, w("pPr"))
    if numbered and column == 0:
        num = etree.SubElement(props, w("numPr"))
        etree.SubElement(num, w("ilvl")).set(w("val"), "0")
        etree.SubElement(num, w("numId")).set(w("val"), "1")
    etree.SubElement(props, w("spacing")).set(w("after"), str(70 + column * 40))
    etree.SubElement(props, w("jc")).set(w("val"), "left" if column == 0 else "right")
    if text:
        run = etree.SubElement(paragraph, w("r"))
        rprops = etree.SubElement(run, w("rPr"))
        etree.SubElement(rprops, w("b" if column == 0 else "i"))
        etree.SubElement(run, w("t")).text = text
    return paragraph


def _row(texts, index, numbered=False):
    row = etree.Element(w("tr"))
    props = etree.SubElement(row, w("trPr"))
    etree.SubElement(props, w("trHeight"), {w("val"): str(400 + index * 40), w("hRule"): "atLeast"})
    etree.SubElement(props, w("cantSplit"))
    for column, text in enumerate(texts):
        cell = etree.SubElement(row, w("tc"))
        cell_props = etree.SubElement(cell, w("tcPr"))
        etree.SubElement(cell_props, w("tcW"), {w("w"): "4000", w("type"): "dxa"})
        borders = etree.SubElement(cell_props, w("tcBorders"))
        etree.SubElement(borders, w("bottom"), {w("val"): "single", w("sz"): "4", w("color"): "000000"})
        cell.append(_p(text, column, numbered))
    return row


def _make(tmp_path, demo_dir, *, numbered=False, empty=False):
    document = etree.Element(w("document"), nsmap={"w": W_NS})
    body = etree.SubElement(document, w("body"))
    table = etree.SubElement(body, w("tbl"))
    props = etree.SubElement(table, w("tblPr"))
    etree.SubElement(props, w("tblW"), {w("w"): "8000", w("type"): "dxa"})
    etree.SubElement(props, w("tblLayout"), {w("type"): "fixed"})
    grid = etree.SubElement(table, w("tblGrid"))
    for _ in range(2):
        etree.SubElement(grid, w("gridCol")).set(w("w"), "4000")
    texts = [["First English", "Первый русский"], ["Middle English", "Средний русский"],
             ["Last English", "Последний русский"], ["", ""]]
    if empty:
        texts[1][1] = ""
    for i, text in enumerate(texts):
        table.append(_row(text, i, numbered=numbered and i < 3))
    body.append(_p("Unrelated final paragraph"))
    path = tmp_path / "working.docx"
    _write(path, demo_dir / "round-1-outgoing-draft.docx", document, numbered=numbered)
    return path


def _write(path, template, document, *, numbered=False):
    with zipfile.ZipFile(template) as source:
        entries = [(entry, source.read(entry)) for entry in source.infolist()]
    numbering = (f'<w:numbering xmlns:w="{W_NS}"><w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="singleLevel"/>'
                 '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
                 '</w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num></w:numbering>').encode()
    with zipfile.ZipFile(path, "w") as target:
        for entry, data in entries:
            target.writestr(entry, etree.tostring(document) if entry.filename == DOCUMENT_PART
                            else numbering if numbered and entry.filename == "word/numbering.xml" else data)


def _document(path):
    with zipfile.ZipFile(path) as package:
        return etree.fromstring(package.read(DOCUMENT_PART))


def _table(path):
    return _document(path).find(f"{w('body')}/{w('tbl')}")


def _refs(path):
    return [r["row_ref"] for r in build_table_map(path.read_bytes())["tables"][0]["rows"]]


def _preview(path, operations):
    return drafting.preflight_document_edits(str(path), hashlib.sha256(path.read_bytes()).hexdigest(), operations, **PROFILE)


def _save(path, operations):
    before = path.read_bytes()
    preview = _preview(path, operations)
    assert preview["batch_applicable"], preview
    assert path.read_bytes() == before
    jsonschema.validate(preview["operations"], _APPLIED)
    result = drafting.apply_document_edits(str(path), hashlib.sha256(before).hexdigest(), operations,
                                          preview["preflight_proof"], **PROFILE)
    assert Path(result["backup_path"]).read_bytes() == before
    jsonschema.validate(result["operations"], _APPLIED)
    return result


def _targets(path):
    result = read_document_revisions(str(path))
    schema = copy.deepcopy(REVISIONS_RESULT)
    for key in ("producer", "record_id", "record_status"):
        schema["properties"].pop(key)
        schema["required"].remove(key)
    jsonschema.validate(result, schema)
    return [r["structural_target"] for r in result["revisions"] if "structural_target" in r]


def _normal(root):
    root = copy.deepcopy(root)
    for tag in ("rPr", "pPr", "trPr"):
        for props in list(root.iter(w(tag))):
            if not len(props) and not props.attrib:
                props.getparent().remove(props)
    for atom in root.iter(w("t")):
        atom.attrib.pop("{http://www.w3.org/XML/1998/namespace}space", None)
    return etree.tostring(root, method="c14n", exclusive=True)


def _expected_inserted_row(template, texts):
    # Literal contract expectation: only approved row/cell/paragraph formatting,
    # one fresh run inherits first fragment's current character formatting.
    result = copy.deepcopy(template)
    result.attrib.clear()
    for cell, text in zip(result.findall(w("tc")), texts, strict=True):
        paragraph = cell.find(w("p"))
        paragraph.attrib.clear()
        run_props = copy.deepcopy(paragraph.find(f"{w('r')}/{w('rPr')}"))
        for child in list(paragraph):
            if child.tag != w("pPr"):
                paragraph.remove(child)
        if text:
            run = etree.SubElement(paragraph, w("r"))
            if run_props is not None:
                run.append(run_props)
            etree.SubElement(run, w("t")).text = text
    return result


@pytest.mark.parametrize("anchor,position", [(0, "before"), (1, "before"), (2, "after")])
@pytest.mark.parametrize("decision", ["accept", "reject"])
@pytest.mark.parametrize("numbered", [False, True])
@pytest.mark.parametrize("separator", [False, True])
def test_insert_row_both_outcomes_exact_count_properties_numbering(tmp_path, demo_dir, anchor, position, decision, numbered, separator):
    path = _make(tmp_path, demo_dir, numbered=numbered)
    original = _document(path)
    refs = _refs(path)
    texts = ["New English clause", ""]  # Mandatory empty-cell positive case.
    operation = {"kind": "insert_table_row", "row_ref": refs[anchor], "position": position,
                 "template_row_ref": refs[1], "cell_texts": texts}
    if separator:
        operation["separator"] = {"template_row_ref": refs[3], "position": "after"}
    result = _save(path, [operation])
    assert result["operations"][0]["row_count"] == 1 + separator
    targets = _targets(path)
    assert len(targets) == 1 + separator
    assert all(t["kind"] == "row_structure" for t in targets)
    _save(path, [{"kind": "decide_revision", "target": target, "decision": decision} for target in targets])
    expected = copy.deepcopy(original)
    if decision == "accept":
        table = expected.find(f"{w('body')}/{w('tbl')}")
        rows = table.findall(w("tr"))
        index = table.index(rows[anchor]) + (position == "after")
        table.insert(index, _expected_inserted_row(rows[1], texts))
        if separator:
            table.insert(index + 1, _expected_inserted_row(rows[3], ["", ""]))
    assert _normal(_document(path)) == _normal(expected)


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("decision", ["accept", "reject"])
@pytest.mark.parametrize("numbered", [False, True])
def test_delete_row_both_outcomes_restore_complete_bilingual_structure(tmp_path, demo_dir, index, decision, numbered):
    path = _make(tmp_path, demo_dir, numbered=numbered, empty=True)
    original = _document(path)
    row = original.find(f"{w('body')}/{w('tbl')}").findall(w("tr"))[index]
    texts = ["".join(p.itertext()) for p in row.findall(f"{w('tc')}/{w('p')}")]
    _save(path, [{"kind": "delete_table_row", "row_ref": _refs(path)[index], "cell_texts": texts}])
    _save(path, [{"kind": "decide_revision", "target": _targets(path)[0], "decision": decision}])
    expected = copy.deepcopy(original)
    if decision == "accept":
        table = expected.find(f"{w('body')}/{w('tbl')}")
        table.remove(table.findall(w("tr"))[index])
    assert _normal(_document(path)) == _normal(expected)


@pytest.mark.parametrize("decision", ["accept", "reject"])
@pytest.mark.parametrize("reverse", [False, True])
def test_selected_empty_separator_is_one_atomic_drafting_and_decision_package(tmp_path, demo_dir, decision, reverse):
    path = _make(tmp_path, demo_dir, numbered=True)
    original = _document(path)
    refs = _refs(path)
    _save(path, [{"kind": "delete_table_row", "row_ref": refs[2], "cell_texts": ["Last English", "Последний русский"],
                  "separator_row_ref": refs[3]}])
    targets = _targets(path)
    assert len(targets) == 2
    if reverse:
        targets.reverse()
    _save(path, [{"kind": "decide_revision", "target": t, "decision": decision} for t in targets])
    expected = copy.deepcopy(original)
    if decision == "accept":
        table = expected.find(f"{w('body')}/{w('tbl')}")
        for row in table.findall(w("tr"))[2:]:
            table.remove(row)
    assert _normal(_document(path)) == _normal(expected)


def _rewrite(path, mutate):
    root = _document(path)
    mutate(root)
    _write(path, path, root)


def _operation(path, kind="insert_table_row"):
    refs = _refs(path)
    if kind == "insert_table_row":
        return {"kind": kind, "row_ref": refs[1], "position": "before", "template_row_ref": refs[1],
                "cell_texts": ["New English", "Новый русский"]}
    return {"kind": kind, "row_ref": refs[1], "cell_texts": ["Middle English", "Средний русский"]}


@pytest.mark.parametrize("kind", ["insert_table_row", "delete_table_row"])
@pytest.mark.parametrize("damage", ["neighbor_text", "neighbor_height", "proposed_paragraph_properties"])
def test_faulty_drafting_helper_cannot_publish_collateral_postimage(tmp_path, demo_dir, monkeypatch, kind, damage):
    path = _make(tmp_path, demo_dir)
    operation = _operation(path, kind)
    original = path.read_bytes()
    from veqtor_docx import table_rows
    real = table_rows.draft_row_operation

    def faulty(*args, **kwargs):
        table, facts = real(*args, **kwargs)
        if damage == "neighbor_text":
            next(table.findall(w("tr"))[0].iter(w("t"))).text = "UNREQUESTED"
        elif damage == "neighbor_height":
            table.findall(w("tr"))[0].find(f"{w('trPr')}/{w('trHeight')}").set(w("val"), "99")
        else:
            marked = next(row for row in table.findall(w("tr"))
                          if row.find(f"{w('trPr')}/{w('ins') if kind == 'insert_table_row' else w('del')}") is not None)
            marked.find(f"{w('tc')}/{w('p')}/{w('pPr')}/{w('spacing')}").set(w("after"), "999")
        return table, facts

    monkeypatch.setattr(table_rows, "draft_row_operation", faulty)
    preview = _preview(path, [operation])
    assert not preview["batch_applicable"]
    assert preview["refusal_code"] == "round_trip_failed", preview
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.bak*"))


@pytest.mark.parametrize("kind", ["insert_table_row", "delete_table_row"])
@pytest.mark.parametrize("malformation", ["none", "bookmark", "field", "foreign_history"])
@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_partial_cell_decisions_never_bypass_pending_row_dependency(tmp_path, demo_dir, kind, malformation, decision):
    path = _make(tmp_path, demo_dir)
    _save(path, [_operation(path, kind)])
    if malformation != "none":
        def mutate(root):
            row = next(r for r in root.iter(w("tr")) if r.find(f"{w('trPr')}/{w('ins') if kind == 'insert_table_row' else w('del')}") is not None)
            paragraph = row.find(f"{w('tc')}/{w('p')}")
            if malformation == "bookmark":
                content = next(c for c in paragraph if c.tag in {w("ins"), w("del")})
                etree.SubElement(content, w("bookmarkStart"), {w("id"): "200", w("name"): "Fictional"})
                etree.SubElement(content, w("bookmarkEnd"), {w("id"): "200"})
            elif malformation == "field":
                run = etree.SubElement(paragraph, w("r"))
                etree.SubElement(run, w("fldChar"), {w("fldCharType"): "begin"})
            else:
                insertion = etree.SubElement(paragraph, w("ins"), {w("id"): "201", w("author"): "Other Fictional Counsel"})
                run = etree.SubElement(insertion, w("r"))
                etree.SubElement(run, w("t")).text = "Unselected foreign text"
        _rewrite(path, mutate)
    inventory = read_document_revisions(str(path))
    root = _document(path)
    content = []
    for item in inventory["revisions"]:
        node = root
        for index in item["revision_ref"]["element_path"]:
            node = node[index]
        if node.getparent().tag == w("p"):
            content.append(item)
    assert content
    before = path.read_bytes()
    preview = _preview(path, [{"kind": "decide_revision", "target": {"kind": "single", "revision_ref": content[0]["revision_ref"]},
                               "decision": decision}])
    assert not preview["batch_applicable"], preview
    assert preview["refusal_code"] == "row_unit_unsupported", preview
    assert path.read_bytes() == before


@pytest.mark.parametrize("value,allowed", [(None, False), ("true", False), ("1", False), ("on", False),
                                           ("false", True), ("0", True), ("off", True), ("maybe", False)])
def test_header_on_off_semantics_are_explicit(tmp_path, demo_dir, value, allowed):
    path = _make(tmp_path, demo_dir)
    def mutate(root):
        row = root.find(f"{w('body')}/{w('tbl')}").findall(w("tr"))[1]
        marker = etree.SubElement(row.find(w("trPr")), w("tblHeader"))
        if value is not None:
            marker.set(w("val"), value)
    _rewrite(path, mutate)
    before = path.read_bytes()
    preview = _preview(path, [_operation(path)])
    assert preview["batch_applicable"] is allowed, preview
    assert path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["stale_ref", "wrong_text", "one_cell", "three_cells", "newline", "header_template",
                                      "multiple_paragraphs", "merge", "nested_table", "only_row", "numbered_separator",
                                      "nonempty_separator", "nonadjacent_separator", "separator_wrong_table"])
def test_unsafe_row_requests_refuse_without_any_publication(tmp_path, demo_dir, mutation):
    path = _make(tmp_path, demo_dir, numbered=mutation == "numbered_separator")
    kind = "delete_table_row" if mutation in {"wrong_text", "only_row", "nonadjacent_separator"} else "insert_table_row"
    if mutation in {"header_template", "multiple_paragraphs", "merge", "nested_table", "only_row", "separator_wrong_table"}:
        def mutate(root):
            table = root.find(f"{w('body')}/{w('tbl')}")
            rows = table.findall(w("tr"))
            if mutation == "header_template":
                etree.SubElement(rows[1].find(w("trPr")), w("tblHeader"))
            elif mutation == "multiple_paragraphs":
                rows[1].find(w("tc")).append(_p("Extra"))
            elif mutation == "merge":
                etree.SubElement(rows[1].find(f"{w('tc')}/{w('tcPr')}"), w("vMerge"), {w("val"): "restart"})
            elif mutation == "nested_table":
                rows[1].find(w("tc")).insert(1, copy.deepcopy(table))
            elif mutation == "only_row":
                for row in rows[1:]:
                    table.remove(row)
            else:
                table.addnext(copy.deepcopy(table))
        _rewrite(path, mutate)
    refs = _refs(path)
    if mutation == "only_row":
        op = {"kind": "delete_table_row", "row_ref": refs[0], "cell_texts": ["First English", "Первый русский"]}
    else:
        op = _operation(path, kind)
    if mutation == "stale_ref":
        op["row_ref"] = {**op["row_ref"], "file_sha256": "0" * 64}
    elif mutation == "wrong_text":
        op["cell_texts"][1] = "Mismatch"
    elif mutation == "one_cell":
        op["cell_texts"] = ["Only"]
    elif mutation == "three_cells":
        op["cell_texts"] = ["One", "Two", "Three"]
    elif mutation == "newline":
        op["cell_texts"][1] = "First\nSecond"
    elif mutation in {"numbered_separator", "nonempty_separator"}:
        op["separator"] = {"template_row_ref": refs[0], "position": "after"}
    elif mutation == "nonadjacent_separator":
        op["row_ref"] = refs[0]
        op["cell_texts"] = ["First English", "Первый русский"]
        op["separator_row_ref"] = refs[3]
    elif mutation == "separator_wrong_table":
        other = build_table_map(path.read_bytes())["tables"][1]["rows"][3]["row_ref"]
        op["separator"] = {"template_row_ref": other, "position": "before"}
    before = path.read_bytes()
    preview = _preview(path, [op])
    assert not preview["batch_applicable"], preview
    assert path.read_bytes() == before


@pytest.mark.parametrize("empty_shape", ["paragraph", "empty_text", "formatted_run"])
@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_delete_empty_cells_preserves_each_valid_empty_run_shape(tmp_path, demo_dir, empty_shape, decision):
    path = _make(tmp_path, demo_dir, empty=True)
    def mutate(root):
        cell = root.find(f"{w('body')}/{w('tbl')}").findall(w("tr"))[1].findall(w("tc"))[1]
        paragraph = cell.find(w("p"))
        if empty_shape != "paragraph":
            run = etree.SubElement(paragraph, w("r"))
            if empty_shape == "formatted_run":
                props = etree.SubElement(run, w("rPr"))
                etree.SubElement(props, w("i"))
            else:
                etree.SubElement(run, w("t"))
    _rewrite(path, mutate)
    original = _document(path)
    _save(path, [{"kind": "delete_table_row", "row_ref": _refs(path)[1], "cell_texts": ["Middle English", ""]}])
    _save(path, [{"kind": "decide_revision", "target": _targets(path)[0], "decision": decision}])
    expected = copy.deepcopy(original)
    if decision == "accept":
        table = expected.find(f"{w('body')}/{w('tbl')}")
        table.remove(table.findall(w("tr"))[1])
    assert _normal(_document(path)) == _normal(expected)


@pytest.mark.parametrize("presence", [True, False])
def test_reject_row_with_empty_properties_does_not_discard_original_container(tmp_path, demo_dir, presence):
    path = _make(tmp_path, demo_dir)
    def mutate(root):
        row = root.find(f"{w('body')}/{w('tbl')}").findall(w("tr"))[1]
        props = row.find(w("trPr"))
        if presence:
            props.clear()
        else:
            row.remove(props)
    _rewrite(path, mutate)
    _save(path, [_operation(path, "delete_table_row")])
    _save(path, [{"kind": "decide_revision", "target": _targets(path)[0], "decision": "reject"}])
    row = _table(path).findall(w("tr"))[1]
    props = row.find(w("trPr"))
    assert props is not None and not len(props)


@pytest.mark.parametrize("value", [[], {}, 0, None])
@pytest.mark.parametrize("separator", [False, True])
def test_malformed_positions_refuse_as_controlled_input_errors(tmp_path, demo_dir, value, separator):
    path = _make(tmp_path, demo_dir)
    op = _operation(path)
    if separator:
        op["separator"] = {"template_row_ref": _refs(path)[3], "position": value}
    else:
        op["position"] = value
    preview = _preview(path, [op])
    assert not preview["batch_applicable"]
    assert preview["refusal_code"] == "invalid_edit"


@pytest.mark.parametrize("mutation", ["omit", "duplicate", "reverse", "mark_only_single", "wrong_mark", "extra_ref"])
def test_complete_ordered_row_unit_is_required_without_auto_selection(tmp_path, demo_dir, mutation):
    path = _make(tmp_path, demo_dir)
    _save(path, [_operation(path)])
    target = _targets(path)[0]
    if mutation == "omit":
        target["content_revision_refs"].pop()
    elif mutation == "duplicate":
        target["content_revision_refs"] *= 2
    elif mutation == "reverse":
        target["content_revision_refs"].reverse()
    elif mutation == "mark_only_single":
        target = {"kind": "single", "revision_ref": target["row_mark_ref"]}
    elif mutation == "wrong_mark":
        target["row_mark_ref"] = target["content_revision_refs"][0]
    else:
        target["content_revision_refs"].append(target["row_mark_ref"])
    before = path.read_bytes()
    preview = _preview(path, [{"kind": "decide_revision", "target": target, "decision": "accept"}])
    assert not preview["batch_applicable"], preview
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["insert_table_row", "delete_table_row"])
@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_unselected_foreign_revision_in_neighboring_row_is_byte_identical(tmp_path, demo_dir, kind, decision):
    path = _make(tmp_path, demo_dir)
    def mutate(root):
        paragraph = root.find(f"{w('body')}/{w('tbl')}/{w('tr')}/{w('tc')}/{w('p')}")
        insertion = etree.SubElement(paragraph, w("ins"), {w("id"): "800", w("author"): "Other Fictional Counsel", w("date"): DATE})
        run = etree.SubElement(insertion, w("r"))
        etree.SubElement(run, w("t")).text = "Unselected foreign proposal"
    _rewrite(path, mutate)
    foreign_before = etree.tostring(next(n for n in _document(path).iter(w("ins")) if n.get(w("id")) == "800"))
    _save(path, [_operation(path, kind)])
    target = next(t for t in _targets(path) if t["kind"] == "row_structure")
    _save(path, [{"kind": "decide_revision", "target": target, "decision": decision}])
    foreign_after = etree.tostring(next(n for n in _document(path).iter(w("ins")) if n.get(w("id")) == "800"))
    assert foreign_after == foreign_before


@pytest.mark.parametrize("row_decisions", [("accept", "reject"), ("reject", "accept")])
@pytest.mark.parametrize("reverse", [False, True])
def test_separate_explicit_row_decisions_allow_selected_mixed_outcomes(tmp_path, demo_dir, row_decisions, reverse):
    path = _make(tmp_path, demo_dir, numbered=True)
    original = _document(path)
    operation = _operation(path)
    operation["separator"] = {"template_row_ref": _refs(path)[3], "position": "before"}
    _save(path, [operation])
    targets = _targets(path)
    assert len(targets) == 2  # Document order: separator, then numbered clause.
    ops = [{"kind": "decide_revision", "target": t, "decision": d} for t, d in zip(targets, row_decisions, strict=True)]
    if reverse:
        ops.reverse()
    _save(path, ops)
    expected = copy.deepcopy(original)
    table = expected.find(f"{w('body')}/{w('tbl')}")
    rows = table.findall(w("tr"))
    index = table.index(rows[1])
    if row_decisions[1] == "accept":
        table.insert(index, _expected_inserted_row(rows[1], operation["cell_texts"]))
    if row_decisions[0] == "accept":
        table.insert(index, _expected_inserted_row(rows[3], ["", ""]))
    assert _normal(_document(path)) == _normal(expected)


@pytest.mark.parametrize("kind", ["insert_table_row", "delete_table_row"])
def test_property_decision_inside_pending_row_refuses_independently(tmp_path, demo_dir, kind):
    path = _make(tmp_path, demo_dir)
    _save(path, [_operation(path, kind)])
    def mutate(root):
        row = next(r for r in root.iter(w("tr")) if r.find(f"{w('trPr')}/{w('ins') if kind == 'insert_table_row' else w('del')}") is not None)
        props = row.find(f"{w('tc')}/{w('p')}/{w('ins') if kind == 'insert_table_row' else w('del')}/{w('r')}/{w('rPr')}")
        history = etree.SubElement(props, w("rPrChange"), {w("id"): "801", w("author"): "Other Fictional Counsel"})
        old = etree.SubElement(history, w("rPr"))
        etree.SubElement(old, w("i"))
    _rewrite(path, mutate)
    revision = next(r for r in read_document_revisions(str(path))["revisions"] if r["kind"] == "rPrChange")
    assert not revision["supported"]
    preview = _preview(path, [{"kind": "decide_revision", "decision": "reject",
                               "target": {"kind": "single", "revision_ref": revision["revision_ref"]}}])
    assert not preview["batch_applicable"]
    assert preview["refusal_code"] == "row_unit_unsupported"


@pytest.mark.parametrize("other", ["replace_text", "add_comment", "other_row_draft"])
def test_drafting_row_table_ownership_prevents_order_dependent_overlap(tmp_path, demo_dir, other):
    from veqtor_docx import inspect_document
    path = _make(tmp_path, demo_dir)
    operations = [_operation(path)]
    paragraph = inspect_document(str(path), "browse", max_items=100)["paragraphs"][0]["paragraph_ref"]
    if other == "replace_text":
        operations.append({"kind": "replace_text", "paragraph_ref": paragraph, "old_text": "First", "new_text": "Changed"})
    elif other == "add_comment":
        operations.append({"kind": "add_comment", "paragraph_ref": paragraph, "quote": "First", "text": "Fictional comment"})
    else:
        operations.append(_operation(path, "delete_table_row"))
    before = path.read_bytes()
    preview = _preview(path, operations)
    assert not preview["batch_applicable"]
    assert preview["refusal_code"] == "table_row_operation_overlap"
    assert path.read_bytes() == before


@pytest.mark.parametrize("row_first", [True, False])
def test_row_drafting_composes_with_unrelated_body_paragraph_insertion(tmp_path, demo_dir, row_first):
    from veqtor_docx import inspect_document
    path = _make(tmp_path, demo_dir)
    _rewrite(path, lambda root: root.find(w("body")).insert(0, _p("Opening outside table")))
    paragraph_ref = inspect_document(str(path), "browse", max_items=100)["paragraphs"][0]["paragraph_ref"]
    row_operation = _operation(path)
    paragraph_operation = {"kind": "insert_paragraph", "paragraph_ref": paragraph_ref,
                           "position": "before", "text": "Additional outside table"}
    _save(path, [row_operation, paragraph_operation] if row_first else [paragraph_operation, row_operation])
    assert len(_table(path).findall(w("tr"))) == 5


def _pending_rows(path, tags):
    def mutate(root):
        table = root.find(f"{w('body')}/{w('tbl')}")
        rows = table.findall(w("tr"))
        for row in rows[len(tags):]:
            table.remove(row)
        for index, (row, tag) in enumerate(zip(rows, tags)):
            etree.SubElement(row.find(w("trPr")), w(tag), {w("id"): str(900 + index * 3), w("author"): "Fictional Original Counsel"})
            for column, paragraph in enumerate(row.iter(w("p"))):
                wrapper = etree.SubElement(paragraph, w(tag), {w("id"): str(901 + index * 3 + column), w("author"): "Fictional Original Counsel"})
                for run in [c for c in paragraph if c.tag == w("r")]:
                    if tag == "del":
                        for atom in run.iter(w("t")):
                            atom.tag = w("delText")
                    wrapper.append(run)
    _rewrite(path, mutate)


@pytest.mark.parametrize("tag,unsafe,safe", [("del", "accept", "reject"), ("ins", "reject", "accept")])
def test_only_row_has_honest_outcome_specific_refusal_and_safe_alternative(tmp_path, demo_dir, tag, unsafe, safe):
    path = _make(tmp_path, demo_dir)
    _pending_rows(path, [tag])
    inventory = read_document_revisions(str(path))
    row = next(item for item in inventory["revisions"] if "structural_target" in item)
    assert row["supported"]
    assert row["preview"][f"{unsafe}_refusal_code"] == "table_row_structure_unsupported"
    assert row["preview"][f"{safe}_refusal_code"] is None
    target = row["structural_target"]
    before = path.read_bytes()
    preview = _preview(path, [{"kind": "decide_revision", "target": target, "decision": unsafe}])
    assert not preview["batch_applicable"]
    assert path.read_bytes() == before
    _save(path, [{"kind": "decide_revision", "target": target, "decision": safe}])
    assert len(_table(path).findall(w("tr"))) == 1


@pytest.mark.parametrize("tags,decisions", [(["del", "del"], ["accept", "accept"]),
                                            (["del", "ins"], ["accept", "reject"])])
@pytest.mark.parametrize("reverse", [False, True])
def test_explicit_batch_must_not_leave_an_empty_table(tmp_path, demo_dir, tags, decisions, reverse):
    path = _make(tmp_path, demo_dir)
    _pending_rows(path, tags)
    operations = [{"kind": "decide_revision", "target": target, "decision": decision}
                  for target, decision in zip(_targets(path), decisions, strict=True)]
    if reverse:
        operations.reverse()
    before = path.read_bytes()
    preview = _preview(path, operations)
    assert not preview["batch_applicable"], preview
    assert preview["refusal_code"] == "table_row_structure_unsupported"
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["insert_table_row", "delete_table_row"])
def test_faulty_numbering_preview_is_rejected_independently(tmp_path, demo_dir, monkeypatch, kind):
    path = _make(tmp_path, demo_dir, numbered=True)
    operation = _operation(path, kind)
    from veqtor_docx import table_rows
    real = table_rows.draft_row_operation
    def faulty(*args, **kwargs):
        table, facts = real(*args, **kwargs)
        facts["row_numbering"] = {"accepted": [], "rejected": []}
        return table, facts
    monkeypatch.setattr(table_rows, "draft_row_operation", faulty)
    before = path.read_bytes()
    preview = _preview(path, [operation])
    assert not preview["batch_applicable"]
    assert preview["refusal_code"] == "round_trip_failed"
    assert path.read_bytes() == before


def _make_two_numbered_columns(tmp_path, demo_dir, *, shared_abstract=False):
    """Literal control: two columns need distinct abstract definitions.

    The deliberately shared variant preserves the formerly mistaken alternating
    family as a negative input. No production resolver constructs expectations.
    """
    path = _make(tmp_path, demo_dir)
    document = _document(path)
    rows = document.find(f"{w('body')}/{w('tbl')}").findall(w("tr"))
    for row in rows[:3]:
        for column, paragraph in enumerate(row.iter(w("p"))):
            props = paragraph.find(w("pPr"))
            num = etree.Element(w("numPr"))
            etree.SubElement(num, w("ilvl"), {w("val"): "0"})
            etree.SubElement(num, w("numId"), {w("val"): str(column + 1)})
            props.insert(0, num)
    # A separate abstract definition, rather than just a different numId,
    # establishes the positive control's independent language sequence.
    second_abstract = "" if shared_abstract else (
        '<w:abstractNum w:abstractNumId="2"><w:multiLevelType w:val="singleLevel"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
        '<w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
    )
    numbering = (
        f'<w:numbering xmlns:w="{W_NS}">'
        '<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="singleLevel"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
        '<w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
        + second_abstract
        + '<w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>'
        + f'<w:num w:numId="2"><w:abstractNumId w:val="{1 if shared_abstract else 2}"/>'
        '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="7"/></w:lvlOverride>'
        '</w:num></w:numbering>'
    ).encode()
    with zipfile.ZipFile(path) as source:
        entries = [(entry, source.read(entry)) for entry in source.infolist()]
    with zipfile.ZipFile(path, "w") as target:
        for entry, data in entries:
            target.writestr(entry, etree.tostring(document) if entry.filename == DOCUMENT_PART
                            else numbering if entry.filename == "word/numbering.xml" else data)
    renamed = path.with_name("working-dual-shared.docx" if shared_abstract else "working-dual-independent.docx")
    path.rename(renamed)
    return renamed


def _label_pairs(facts):
    numbered = [fact for fact in facts if fact["num_id"] is not None]
    assert [fact["num_id"] for fact in numbered] == ["1", "2"] * (len(numbered) // 2)
    assert all(fact["ilvl"] == 0 for fact in numbered)
    return [tuple(fact["label"] for fact in numbered[index:index + 2])
            for index in range(0, len(numbered), 2)]


def _numbered_column_facts(path):
    from veqtor_docx.paragraph_numbering import resolve_body_numbering
    with zipfile.ZipFile(path) as source:
        parts = {entry.filename: source.read(entry) for entry in source.infolist()}
    root = _document(path)
    all_paragraphs = list(root.find(w("body")).iter(w("p")))
    selected = [index for index, paragraph in enumerate(all_paragraphs)
                if paragraph.find(f"{w('pPr')}/{w('numPr')}") is not None]
    return resolve_body_numbering(parts, root, selected)


@pytest.mark.parametrize("kind", ["insert_table_row", "delete_table_row"])
@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_two_independent_numbered_columns_full_public_row_lifecycle(tmp_path, demo_dir, kind, decision):
    path = _make_two_numbered_columns(tmp_path, demo_dir)
    original = _document(path)
    original_rows = original.find(f"{w('body')}/{w('tbl')}").findall(w("tr"))
    with zipfile.ZipFile(path) as source:
        numbering_before = source.read("word/numbering.xml")
    assert _label_pairs(_numbered_column_facts(path)) == [("1.", "7."), ("2.", "8."), ("3.", "9.")]
    operation = _operation(path, kind)
    drafted = _save(path, [operation])
    row_facts = drafted["operations"][0]["row_numbering"]
    expected_accepted = ([("1.", "7."), ("2.", "8."), ("3.", "9."), ("4.", "10.")]
                         if kind == "insert_table_row" else [("1.", "7."), ("2.", "8.")])
    expected_rejected = [("1.", "7."), ("2.", "8."), ("3.", "9.")]
    assert _label_pairs(row_facts["accepted"]) == expected_accepted
    assert _label_pairs(row_facts["rejected"]) == expected_rejected
    pending_rows = _table(path).findall(w("tr"))
    for old_index in (0, 2, 3):
        pending_index = old_index + (kind == "insert_table_row" and old_index >= 1)
        assert _normal(pending_rows[pending_index]) == _normal(original_rows[old_index])
    inventory = read_document_revisions(str(path))
    structural = [item for item in inventory["revisions"] if "structural_target" in item]
    assert len(structural) == 1 and structural[0]["supported"]
    assert structural[0]["preview"]["accept_refusal_code"] is None
    assert structural[0]["preview"]["reject_refusal_code"] is None
    assert structural[0]["preview"]["row_numbering"] == row_facts
    target = _targets(path)[0]
    assert len(target["content_revision_refs"]) == 2
    final = _save(path, [{"kind": "decide_revision", "target": target, "decision": decision}])
    assert final["operations"][0]["row_numbering"] == row_facts
    expected = copy.deepcopy(original)
    table = expected.find(f"{w('body')}/{w('tbl')}")
    if decision == "accept":
        if kind == "insert_table_row":
            table.insert(table.index(table.findall(w("tr"))[1]),
                         _expected_inserted_row(original_rows[1], operation["cell_texts"]))
        else:
            table.remove(table.findall(w("tr"))[1])
    assert _normal(_document(path)) == _normal(expected)
    assert _label_pairs(_numbered_column_facts(path)) == (expected_accepted if decision == "accept" else expected_rejected)
    with zipfile.ZipFile(path) as source:
        assert source.read("word/numbering.xml") == numbering_before
    assert read_document_revisions(str(path))["total_count"] == 0


@pytest.mark.parametrize("kind", ["insert_table_row", "delete_table_row"])
def test_shared_abstract_alternating_columns_refuse_without_source_or_output_changes(tmp_path, demo_dir, kind):
    from veqtor_docx.apply import ApplyError
    path = _make_two_numbered_columns(tmp_path, demo_dir, shared_abstract=True)
    output = tmp_path / "existing-destination.docx"
    output.write_bytes(b"Existing destination must remain byte-identical.")
    before = {item.name: item.read_bytes() for item in tmp_path.iterdir() if item.is_file()}
    operation = _operation(path, kind)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    preview = drafting.preflight_document_edits(str(path), sha, [operation], output_path=str(output), **PROFILE)
    assert not preview["batch_applicable"]
    assert preview["refusal_code"] == "paragraph_numbering_unsupported"
    assert preview["blocking_operation_index"] == 0
    assert preview["preflight_proof"] is None
    assert preview["operations"] == []
    # An invented proof cannot turn the failed numbering projection into a write.
    with pytest.raises(ApplyError, match="paragraph_numbering_unsupported") as caught:
        drafting.apply_document_edits(str(path), sha, [operation], {"timestamp": DATE}, output_path=str(output), **PROFILE)
    assert caught.value.metadata["reason"] == "interleaved_numbering_instances"
    assert {item.name: item.read_bytes() for item in tmp_path.iterdir() if item.is_file()} == before
