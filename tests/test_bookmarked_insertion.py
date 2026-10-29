# SPDX-License-Identifier: Apache-2.0
"""The selected required-family navigation point is disclosed, never cleaned silently."""
import copy

import jsonschema
import pytest
from lxml import etree

from veqtor_docx import drafting
from veqtor_docx._ooxml import w
from veqtor_docx.revision_decisions import reference, read_document_revisions
from veqtor_mcp._drafting_contract import APPLY_RESULT, PREFLIGHT_RESULT
from test_clean_copy import _package
from test_counter_revisions import main, parts, save, sha, PROFILE
from test_preserved_text_revisions import ordinary


def fixture(tmp_path, demo_dir):
    path, _ = ordinary(tmp_path, demo_dir, kind="ins")
    root = main(path)
    wrapper = next(n for n in root.iter(w("ins")) if n.get(w("id")) == "801")
    start = etree.SubElement(wrapper, w("bookmarkStart"), {w("id"): "47", w("name"): "_GoBack"})
    end = etree.SubElement(wrapper, w("bookmarkEnd"), {w("id"): "47"})
    wrapper.append(copy.deepcopy(wrapper[0]))
    return path, root, wrapper, start, end


def write(path, root, wrapper, demo_dir, decision, extra=None):
    path.write_bytes(_package(demo_dir, root, extra))
    return {"kind": "decide_revision", "decision": decision,
            "target": {"kind": "text_preserve_paragraphs",
                       "revision_refs": [reference(root, wrapper, sha(path))]}}


@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_disclosed_point_follows_selected_insertion_only(tmp_path, demo_dir, decision):
    path, root, wrapper, start, end = fixture(tmp_path, demo_dir)
    op = write(path, root, wrapper, demo_dir, decision)
    before_parts = parts(path.read_bytes())
    parent = wrapper.getparent(); index = parent.index(wrapper)
    expected = copy.deepcopy(root)
    old = next(n for n in expected.iter(w("ins")) if n.get(w("id")) == "801")
    owner = old.getparent(); owner.remove(old)
    if decision == "accept":
        for offset, child in enumerate(wrapper):
            owner.insert(index+offset, copy.deepcopy(child))
    preflight = drafting.preflight_document_edits(str(path), sha(path), [op], **PROFILE, journal=False)
    assert preflight["batch_applicable"], preflight
    jsonschema.validate(preflight["operations"], PREFLIGHT_RESULT["properties"]["operations"])
    report = read_document_revisions(str(path))
    row = next(r for r in report["revisions"] if r["id"] == "801")
    assert row["supported"] and row["text_target"]["kind"] == "text_preserve_paragraphs"
    result = save(path, [op], journal=True)
    jsonschema.validate(result["operations"], APPLY_RESULT["properties"]["operations"])
    assert etree.tostring(main(path)) == etree.tostring(expected)
    assert [etree.tostring(n) for n in root.iter(w("pPr"))] == [etree.tostring(n) for n in main(path).iter(w("pPr"))]
    assert result["operations"][0]["bookmark_context"]["reject_policy"] == "remove_with_insertion"
    assert {k:v for k,v in parts(path.read_bytes()).items() if k != "word/document.xml"} == {k:v for k,v in before_parts.items() if k != "word/document.xml"}


@pytest.mark.parametrize("decision", ["accept", "reject"])
@pytest.mark.parametrize("problem", ["named", "range", "unclosed", "outside", "duplicate_id", "alias_id", "duplicate_name", "field_consumer", "hyperlink_consumer", "cross_part_consumer", "encoded_consumer", "upper_consumer", "bin_consumer", "binary_part", "property_foreign_attr", "split_consumer", "attrs", "scalar", "tail", "child", "reversed", "nested", "id_long", "id_negative", "id_nonascii", "normal_target", "unknown_run", "pending_properties"])
def test_unsupported_anchor_or_consumer_refuses_without_write(tmp_path, demo_dir, decision, problem):
    path, root, wrapper, start, end = fixture(tmp_path, demo_dir)
    extra = None
    if problem == "named": start.set(w("name"), "BusinessReference")
    elif problem == "range": wrapper.insert(wrapper.index(end), copy.deepcopy(wrapper[0]))
    elif problem == "unclosed": wrapper.remove(end)
    elif problem == "outside": wrapper.remove(end); wrapper.getparent().append(end)
    elif problem in {"duplicate_id", "alias_id"}:
        duplicate = copy.deepcopy(start); duplicate.set(w("name"), "Elsewhere")
        duplicate.set(w("id"), "047" if problem == "alias_id" else "47")
        wrapper.getparent().append(duplicate)
    elif problem == "duplicate_name":
        duplicate = copy.deepcopy(start); duplicate.set(w("id"), "333"); wrapper.getparent().append(duplicate)
    elif problem == "field_consumer": etree.SubElement(wrapper.getparent(), w("fldSimple"), {w("instr"): "REF _GoBack"})
    elif problem == "hyperlink_consumer": etree.SubElement(wrapper.getparent(), w("hyperlink"), {w("anchor"): "_GoBack"})
    elif problem == "cross_part_consumer": extra = {"customXml/consumer.xml": b'<consumer target="_GoBack"/>'}
    elif problem == "encoded_consumer": extra = {"customXml/consumer.xml": '<consumer target="_Go&#66;ack"/>'.encode("utf-16")}
    elif problem in {"upper_consumer", "bin_consumer"}:
        name = "word/header1.XML" if problem == "upper_consumer" else "word/header1.bin"
        extra = {name: '<consumer target="_Go&#66;ack"/>'.encode("utf-16")}
    elif problem == "binary_part": extra = {"word/media/image.png": b"\x89PNG\r\n\x1a\n"}
    elif problem == "property_foreign_attr": wrapper[0].find(w("rPr"))[0].set(w("ascii"), "PRIVATE")
    elif problem == "split_consumer":
        for text in ("REF _Go", "Back"):
            etree.SubElement(etree.SubElement(wrapper.getparent(), w("r")), w("instrText")).text = text
    elif problem == "attrs": start.set(w("colFirst"), "0")
    elif problem == "scalar": start.text = "PRIVATE"
    elif problem == "tail": end.tail = "PRIVATE"
    elif problem == "child": etree.SubElement(start, w("t")).text = "PRIVATE"
    elif problem == "reversed": wrapper.remove(end); wrapper.insert(wrapper.index(start), end)
    elif problem == "nested": wrapper.remove(start); wrapper[0].append(start)
    elif problem.startswith("id_"):
        value = {"id_long":"1"*11,"id_negative":"-1","id_nonascii":"٤٧"}[problem]
        start.set(w("id"),value); end.set(w("id"),value)
    elif problem == "unknown_run": wrapper[0].set("private", "payload")
    elif problem == "pending_properties": etree.SubElement(wrapper[0].find(w("rPr")), w("rPrChange"), {w("id"): "909"})
    op = write(path, root, wrapper, demo_dir, decision, extra)
    if problem == "normal_target": op["target"] = {"kind":"single", "revision_ref":op["target"]["revision_refs"][0]}
    before = path.read_bytes()
    preflight = drafting.preflight_document_edits(str(path), sha(path), [op], **PROFILE, journal=False)
    assert not preflight["batch_applicable"], preflight
    assert preflight["preflight_proof"] is None
    assert path.read_bytes() == before
