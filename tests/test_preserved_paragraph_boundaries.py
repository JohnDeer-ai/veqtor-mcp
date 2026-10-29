# SPDX-License-Identifier: Apache-2.0
"""Text-only counter choices never resolve pending paragraph structure."""
import copy

import jsonschema
import pytest
from lxml import etree

from veqtor_docx import drafting
from veqtor_docx import ApplyError
from veqtor_docx.clean_copy import prepare_clean_copy
from veqtor_docx._ooxml import w
from veqtor_docx.revision_decisions import read_document_revisions
from veqtor_mcp._drafting_contract import OPERATIONS, REVISIONS_RESULT, APPLY_RESULT
from test_counter_revisions import make, main, operation, parts, save, sha, texts, PROFILE
from test_clean_copy import _package
from test_drafting_text import _p, _r


def chain(tmp_path, demo_dir, *, shape="whole", bookmark=False, count=3):
    path = make(tmp_path, demo_dir, shape)
    root = main(path)
    body = root.find(w("body"))
    selected = body[0]
    body.remove(selected)
    table = etree.Element(w("tbl"))
    props = etree.SubElement(table, w("tblPr"))
    etree.SubElement(props, w("tblW"), {w("w"): "8000", w("type"): "dxa"})
    grid = etree.SubElement(table, w("tblGrid"))
    etree.SubElement(grid, w("gridCol"), {w("w"): "8000"})
    row = etree.SubElement(table, w("tr"))
    cell = etree.SubElement(row, w("tc"))
    cell.append(selected)
    for i in range(1, count):
        cell.append(_p(_r(f"Neighbor {i}")))
    for i, paragraph in enumerate(cell):
        props = etree.Element(w("pPr"))
        etree.SubElement(props, w("spacing"), {w("after"): str(i * 60)})
        paragraph.insert(0, props)
        if i < count - 1:
            rp = etree.SubElement(props, w("rPr"))
            etree.SubElement(rp, w("ins" if i % 2 == 0 else "del"),
                             {w("id"): str(500 + i), w("author"): "Boundary Author"})
    if bookmark:
        selected.insert(1, etree.Element(w("bookmarkStart"), {w("id"): "1000", w("name"): "Control"}))
        cell[-1].append(etree.Element(w("bookmarkEnd"), {w("id"): "1000"}))
    body.insert(0, table)
    path.write_bytes(_package(demo_dir, root))
    return path


def protected(root):
    return [[etree.tostring(n) for n in p if n.tag == w("pPr") or n.tag in {w("bookmarkStart"), w("bookmarkEnd")}]
            for p in root.iter(w("p"))]


@pytest.mark.parametrize("shape", ["suffix", "whole", "delete", "whole_delete"])
@pytest.mark.parametrize("original,counter", [("accept", "accept"), ("accept", "reject"),
                                              ("reject", "accept"), ("reject", "reject")])
@pytest.mark.parametrize("bookmark", [False, True])
def test_four_text_outcomes_keep_all_paragraphs_marks_ranges_and_other_parts(
    tmp_path, demo_dir, shape, original, counter, bookmark
):
    path = chain(tmp_path, demo_dir, shape=shape, bookmark=bookmark)
    before = main(path)
    old_parts = parts(path.read_bytes())
    report = read_document_revisions(str(path))
    jsonschema.validate(report["revisions"], REVISIONS_RESULT["properties"]["revisions"])
    op = operation(path, original, counter)
    assert op["target"]["kind"] == "counter_text_preserve_paragraphs"
    jsonschema.validate([op], OPERATIONS)
    result = save(path, [op], journal=True)
    jsonschema.validate(result["operations"], APPLY_RESULT["properties"]["operations"])
    after = main(path)
    assert protected(after) == protected(before)
    assert texts(after)[1:] == texts(before)[1:]
    expected = {
        ("accept", "reject"): "thirty days",
        ("reject", "reject"): "",
        ("reject", "accept"): {"suffix": "weeks", "whole": "45 days", "delete": "", "whole_delete": ""}[shape],
        ("accept", "accept"): {"suffix": "thirty weeks", "whole": "45 days", "delete": "thirty ", "whole_delete": ""}[shape],
    }[original, counter]
    assert texts(after)[0] == "Pay " + expected + " later"
    new_parts = parts(path.read_bytes())
    assert {k: v for k, v in old_parts.items() if k != "word/document.xml"} == {
        k: v for k, v in new_parts.items() if k != "word/document.xml"}
    context = result["operations"][0]["paragraph_boundary_context"]
    assert context["structural_status"] == "pending"
    assert context["paragraph_count"] == 3 and context["boundary_revision_count"] == 2
    inventory = read_document_revisions(str(path))
    remaining = [r for r in inventory["revisions"] if r["author"] == "Boundary Author"]
    assert len(remaining) == 2
    # Text decisions still leave both marks pending. The new explicit complete
    # chain decision is offered only on its first mark, never applied here.
    assert remaining[0]["supported"]
    assert remaining[0]["structural_target"]["kind"] == "paragraph_chain"
    assert not remaining[1]["supported"]
    with pytest.raises(ApplyError):
        prepare_clean_copy(path.read_bytes(), expected_source_sha256=sha(path))


def test_ordinary_counter_target_does_not_silently_opt_in(tmp_path, demo_dir):
    path = chain(tmp_path, demo_dir)
    op = operation(path)
    op["target"]["kind"] = "counter_insertion"
    before = path.read_bytes()
    result = drafting.preflight_document_edits(str(path), sha(path), [op], **PROFILE, journal=False)
    assert not result["batch_applicable"] and result["refusal_code"] == "paragraph_unit_unsupported"
    assert path.read_bytes() == before


@pytest.mark.parametrize("problem", ["last_mark", "duplicate_mark", "unknown_attribute", "mark_child", "field", "range", "row"])
def test_unknown_or_incomplete_dependency_refused_atomically(tmp_path, demo_dir, problem):
    path = chain(tmp_path, demo_dir)
    root = main(path)
    cell = next(root.iter(w("tc")))
    mark = next(cell.iter(w("ins")))
    if problem == "last_mark":
        cell[-1].find(w("pPr")).append(copy.deepcopy(cell[0].find(f"{w('pPr')}/{w('rPr')}")))
    elif problem == "duplicate_mark":
        mark.getparent().append(copy.deepcopy(mark))
    elif problem == "unknown_attribute":
        mark.set("unexpected", "value")
    elif problem == "mark_child":
        etree.SubElement(mark, w("r"))
    elif problem == "field":
        cell[-1].insert(1, etree.Element(w("fldSimple"), {w("instr"): "DATE"}))
    elif problem == "range":
        cell[-1].insert(1, etree.Element(w("permStart"), {w("id"): "99"}))
    else:
        row = cell.getparent()
        rp = etree.Element(w("trPr"))
        row.insert(0, rp)
        etree.SubElement(rp, w("ins"), {w("id"): "99"})
    path.write_bytes(_package(demo_dir, root))
    op = operation(path)
    op["target"]["kind"] = "counter_text_preserve_paragraphs"
    before = path.read_bytes()
    result = drafting.preflight_document_edits(str(path), sha(path), [op], **PROFILE, journal=False)
    assert not result["batch_applicable"], result
    assert path.read_bytes() == before


def test_complete_component_budget(tmp_path, demo_dir, monkeypatch):
    path = chain(tmp_path, demo_dir)
    op = operation(path)
    from veqtor_docx import _preserved_paragraph_boundaries as b
    monkeypatch.setattr(b, "MAX_PARAGRAPHS", 2)
    result = drafting.preflight_document_edits(str(path), sha(path), [op], **PROFILE, journal=False)
    assert not result["batch_applicable"] and result["refusal_code"] == "resource_limit_exceeded"


@pytest.mark.parametrize("gap", ["none", "comment", "processing_instruction", "bookmark_pair"])
def test_inert_sibling_cannot_hide_overlong_left_component(tmp_path, demo_dir, gap):
    path = chain(tmp_path, demo_dir, count=66)
    root = main(path)
    cell = next(root.iter(w("tc")))
    # Move only the counter text to paragraph 65; all 65 separators stay pending.
    for child in list(cell[0]):
        if child.tag != w("pPr"):
            cell[64].append(child)
    if gap == "bookmark_pair":
        cell.insert(64, etree.Element(w("bookmarkStart"), {w("id"): "1001", w("name"): "Gap"}))
        cell.insert(65, etree.Element(w("bookmarkEnd"), {w("id"): "1001"}))
    elif gap != "none":
        sibling = etree.Comment("inert") if gap == "comment" else etree.ProcessingInstruction("inert", "control")
        cell.insert(64, sibling)
    path.write_bytes(_package(demo_dir, root))
    op = operation(path)
    op["target"]["kind"] = "counter_text_preserve_paragraphs"
    before = path.read_bytes()
    result = drafting.preflight_document_edits(str(path), sha(path), [op], **PROFILE, journal=False)
    assert not result["batch_applicable"]
    assert result["refusal_code"] == ("resource_limit_exceeded" if gap == "none" else "paragraph_context_unsupported")
    assert path.read_bytes() == before


def test_first_cell_properties_are_a_proven_left_boundary(tmp_path, demo_dir):
    path = chain(tmp_path, demo_dir)
    root = main(path)
    next(root.iter(w("tc"))).insert(0, etree.Element(w("tcPr")))
    path.write_bytes(_package(demo_dir, root))
    result = save(path, [operation(path)])
    assert result["operations"][0]["paragraph_boundary_context"]["paragraph_count"] == 3


def test_changed_neighbor_requires_fresh_file_and_references(tmp_path, demo_dir):
    path = chain(tmp_path, demo_dir)
    op, old_sha = operation(path), sha(path)
    root = main(path)
    next(root.iter(w("tc")))[-1].append(_p(_r(" changed"))[0])
    path.write_bytes(_package(demo_dir, root))
    for digest in (old_sha, sha(path)):
        result = drafting.preflight_document_edits(str(path), digest, [op], **PROFILE, journal=False)
        assert not result["batch_applicable"]


@pytest.mark.anyio
async def test_public_preflight_apply_and_recovery_keep_pending_structure(tmp_path, demo_dir, monkeypatch):
    from mcp.client import Client
    from veqtor_mcp import server
    path = chain(tmp_path, demo_dir, bookmark=True)
    monkeypatch.setenv("VEQTOR_OPERATION_JOURNAL", "required")
    monkeypatch.setenv("VEQTOR_TRACKED_CHANGE_AUTHOR", PROFILE["author"])
    monkeypatch.setenv("VEQTOR_COMMENT_INITIALS", PROFILE["initials"])
    before = protected(main(path))
    async with Client(server.mcp) as client:
        result = await client.call_tool("read_document_revisions", {"path": str(path)})
        assert not result.is_error, result
        jsonschema.validate(result.structured_content, REVISIONS_RESULT)
        args = {"source_path": str(path), "file_sha256": sha(path),
                "operations": [operation(path, "accept", "reject")]}
        pre = await client.call_tool("preflight_document_edits", args)
        assert not pre.is_error and pre.structured_content["batch_applicable"], pre
        proof = pre.structured_content["preflight_proof"]
        saved = await client.call_tool("apply_document_edits", {**args, "preflight_proof": proof})
        assert not saved.is_error, saved
        assert saved.structured_content["operations"][0]["paragraph_boundary_context"]["structural_status"] == "pending"
        payload = path.read_bytes()
        recovered = await client.call_tool("recover_document_operation", {"source_path": str(path),
                                           "operation_id": proof["operation_id"]})
        assert not recovered.is_error and recovered.structured_content["recovery_status"] == "existing_result"
        assert path.read_bytes() == payload and protected(main(path)) == before
