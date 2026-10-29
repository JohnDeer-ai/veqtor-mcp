# SPDX-License-Identifier: Apache-2.0
"""Preserve known spelling hints; do not use stripping as a structural proof."""

import copy

import pytest
from lxml import etree

from veqtor_docx import drafting
from veqtor_docx._ooxml import w
from test_clean_copy import _package
from test_counter_revisions import main, save, sha, parts, PROFILE
from test_paragraph_chain import CASES, fixture, op, semantic


@pytest.mark.parametrize("pattern,accepted,rejected", CASES)
@pytest.mark.parametrize("action", ["accept", "reject"])
def test_chain_preserves_hint_nodes_and_run_order(
    tmp_path, demo_dir, pattern, accepted, rejected, action
):
    path = fixture(tmp_path, demo_dir, pattern)
    root = main(path)
    ps = next(root.iter(w("tc"))).findall(w("p"))
    for index, p in enumerate(ps):
        prefix = "spell" if index % 2 == 0 else "gram"
        p.insert(1, etree.Element(w("proofErr"), {w("type"): prefix + "Start"}))
        p.append(etree.Element(w("proofErr"), {w("type"): prefix + "End"}))
    path.write_bytes(_package(demo_dir, root))
    before = parts(path.read_bytes())
    old_nodes = [semantic(n) for p in ps for n in p if n.tag != w("pPr")]
    result = save(path, [op(path, action)], journal=True)
    assert (
        result["operations"][0]["paragraph_chain_context"]["formatting_policy"]
        == "rightmost_paragraph"
    )
    after = main(path)
    new = next(after.iter(w("tc"))).findall(w("p"))
    groups = accepted if action == "accept" else rejected
    assert len(new) == len(groups)
    assert [semantic(n) for p in new for n in p if n.tag != w("pPr")] == old_nodes
    for p, group in zip(new, groups, strict=True):
        props = copy.deepcopy(ps[group[-1]].find(w("pPr")))
        for mark in list(props.iter()):
            if mark.tag in {w("ins"), w("del")}:
                mark.getparent().remove(mark)
        assert semantic(p.find(w("pPr"))) == semantic(props)
        assert p.attrib == ps[group[-1]].attrib
    assert {k: v for k, v in before.items() if k != "word/document.xml"} == {
        k: v for k, v in parts(path.read_bytes()).items() if k != "word/document.xml"
    }


@pytest.mark.parametrize("action", ["accept", "reject"])
@pytest.mark.parametrize(
    "problem",
    [
        "attribute",
        "value",
        "text",
        "tail",
        "child",
        "missing_type",
        "run_nested",
        "property_nested",
        "foreign_namespace",
    ],
)
def test_unknown_or_misplaced_hints_refuse_without_write(
    tmp_path, demo_dir, action, problem
):
    path = fixture(tmp_path, demo_dir, ["D", "I", ""])
    root = main(path)
    p = next(root.iter(w("p")))
    hint = etree.Element(w("proofErr"), {w("type"): "spellStart"})
    p.append(hint)
    if problem == "attribute":
        hint.set("private", "payload")
    elif problem == "value":
        hint.set(w("type"), "unknown")
    elif problem == "text":
        hint.text = "PRIVATE"
    elif problem == "tail":
        hint.tail = "PRIVATE"
    elif problem == "child":
        etree.SubElement(hint, w("t")).text = "PRIVATE"
    elif problem == "missing_type":
        hint.attrib.clear()
    elif problem == "run_nested":
        p.find(w("r")).append(hint)
    elif problem == "property_nested":
        p.find(w("pPr")).append(hint)
    elif problem == "foreign_namespace":
        hint.tag = "{urn:unknown}proofErr"
    path.write_bytes(_package(demo_dir, root))
    before = path.read_bytes()
    preflight = drafting.preflight_document_edits(
        str(path), sha(path), [op(path, action)], **PROFILE, journal=False
    )
    assert not preflight["batch_applicable"] and preflight["preflight_proof"] is None
    assert path.read_bytes() == before
