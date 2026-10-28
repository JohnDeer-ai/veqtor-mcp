# SPDX-License-Identifier: Apache-2.0
"""Resolve ordinary text independently of the still-pending separator chain."""
import copy

import jsonschema
import pytest
from lxml import etree

from veqtor_docx import drafting, ApplyError
from veqtor_docx._ooxml import w
from veqtor_docx.clean_copy import prepare_clean_copy
from veqtor_docx.revision_decisions import reference, read_document_revisions
from veqtor_mcp._drafting_contract import OPERATIONS, REVISIONS_RESULT, APPLY_RESULT
from test_clean_copy import _package
from test_counter_revisions import main, parts, save, sha, texts, PROFILE
from test_drafting_text import _p, _r, _ins
from test_preserved_paragraph_boundaries import chain, protected


def ordinary(tmp_path, demo_dir, *, index=0, kind="replacement"):
    path = chain(tmp_path, demo_dir, bookmark=True)
    root = main(path)
    cell = next(root.iter(w("tc")))
    # Remove the unrelated compound fixture; retain its paragraph properties
    # and both bookmark endpoints exactly.
    for paragraph in cell:
        for node in list(paragraph):
            if node.tag not in {w("pPr"), w("bookmarkStart"), w("bookmarkEnd")}:
                paragraph.remove(node)
        paragraph.insert(1, _p(_r("Keep "))[0])
    selected = cell[index]
    if kind in {"replacement", "del"}:
        selected.append(_p('<w:del w:id="800" w:author="Original"><w:r><w:rPr><w:b/></w:rPr><w:delText>old</w:delText></w:r></w:del>')[0])
    if kind in {"replacement", "ins"}:
        selected.append(_p(_ins(_r("new", '<w:i/><w:color w:val="123456"/>'), revision_id="801"))[0])
    selected.append(_p(_r(" after"))[0])
    path.write_bytes(_package(demo_dir, root))
    nodes = [n for n in main(path).iter() if n.tag in {w("ins"), w("del")} and n.get(w("id")) in {"800", "801"}]
    root = nodes[0].getroottree().getroot()
    target = {"kind": "text_preserve_paragraphs", "revision_refs": [reference(root, n, sha(path)) for n in nodes]}
    return path, target


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("kind", ["ins", "del", "replacement"])
@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_text_choice_preserves_full_component_and_format(tmp_path, demo_dir, index, kind, decision):
    path, target = ordinary(tmp_path, demo_dir, index=index, kind=kind)
    before = main(path)
    before_parts = parts(path.read_bytes())
    before_text = texts(before)
    report = read_document_revisions(str(path))
    jsonschema.validate(report["revisions"], REVISIONS_RESULT["properties"]["revisions"])
    rows = [r for r in report["revisions"] if r["id"] in {"800", "801"}]
    assert all(r["supported"] and r["text_target"]["kind"] == "text_preserve_paragraphs" for r in rows)
    op = {"kind": "decide_revision", "decision": decision, "target": target}
    jsonschema.validate([op], OPERATIONS)
    result = save(path, [op], journal=True)
    jsonschema.validate(result["operations"], APPLY_RESULT["properties"]["operations"])
    after = main(path)
    assert protected(before) == protected(after)
    expected = "new" if decision == "accept" and kind != "del" else "old" if decision == "reject" and kind != "ins" else ""
    before_text[index] = "Keep " + expected + " after"
    assert texts(after) == before_text
    para = next(after.iter(w("tc")))[index]
    if expected:
        run = next(r for r in para.findall(w("r")) if "".join(r.itertext()) == expected)
        assert run.find(f"{w('rPr')}/{w('i' if expected == 'new' else 'b')}") is not None
    after_parts = parts(path.read_bytes())
    assert {k: v for k, v in before_parts.items() if k != "word/document.xml"} == {k: v for k, v in after_parts.items() if k != "word/document.xml"}
    facts = result["operations"][0]["paragraph_boundary_context"]
    assert facts["decision_scope"] == "text_only" and facts["structural_status"] == "pending"
    assert facts["paragraph_count"] == 3 and facts["boundary_revision_count"] == 2
    with pytest.raises(ApplyError):
        prepare_clean_copy(path.read_bytes(), expected_source_sha256=sha(path))


@pytest.mark.parametrize("problem", ["field", "property_history", "foreign_mark", "unknown_mark", "nested_text", "reversed", "nonadjacent", "ordinary_target"])
def test_refusals_leave_source_untouched(tmp_path, demo_dir, problem):
    path, target = ordinary(tmp_path, demo_dir)
    root = main(path)
    cell = next(root.iter(w("tc")))
    p = cell[0]
    if problem == "field":
        cell[-1].append(etree.Element(w("fldSimple"), {w("instr"): "DATE"}))
    elif problem == "property_history":
        etree.SubElement(cell[-1].find(w("pPr")), w("pPrChange"), {w("id"): "90"})
    elif problem == "foreign_mark":
        cell[-1].find(w("pPr")).append(copy.deepcopy(cell[0].find(f"{w('pPr')}/{w('rPr')}")))
    elif problem == "unknown_mark":
        next(cell[1].iter(w("del"))).set("unknown", "x")
    elif problem == "nested_text":
        wrapper = p.find(w("ins"))
        wrapper.append(etree.Element(w("del"), {w("id"): "99"}))
    elif problem == "nonadjacent":
        p.insert(p.index(p.find(w("ins"))), _p(_r("intervening"))[0])
    path.write_bytes(_package(demo_dir, root))
    selected = [n for n in root.iter() if n.get(w("id")) in {"800", "801"}]
    target["revision_refs"] = [reference(root, n, sha(path)) for n in selected]
    if problem == "reversed":
        target["revision_refs"].reverse()
    if problem == "ordinary_target":
        target = {"kind": "replacement", "deletion_ref": target["revision_refs"][0], "insertion_ref": target["revision_refs"][1]}
    before = path.read_bytes()
    result = drafting.preflight_document_edits(str(path), sha(path), [{"kind": "decide_revision", "decision": "accept", "target": target}], **PROFILE, journal=False)
    assert not result["batch_applicable"], result
    assert path.read_bytes() == before


def test_stale_ref_and_two_targets_cannot_retarget_after_mutation(tmp_path, demo_dir):
    path, target = ordinary(tmp_path, demo_dir)
    op = {"kind": "decide_revision", "decision": "accept", "target": target}
    result = drafting.preflight_document_edits(str(path), sha(path), [op, op], **PROFILE, journal=False)
    assert result["refusal_code"] == "revision_operation_overlap"
    save(path, [op])
    before = path.read_bytes()
    result = drafting.preflight_document_edits(str(path), sha(path), [op], **PROFILE, journal=False)
    assert result["refusal_code"] == "revision_reference_mismatch"
    assert path.read_bytes() == before
