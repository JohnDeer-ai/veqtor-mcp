# SPDX-License-Identifier: Apache-2.0
"""Closed Word ligature formatting in selective text/property decisions."""
import copy

import pytest
from lxml import etree

from test_revision_decisions import (
    document, make, normalized, op, preview, save,
)
from veqtor_docx._ooxml import w
from veqtor_docx.revision_decisions import read_document_revisions

W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
LIGATURES = "{" + W14 + "}ligatures"
VALUE = "{" + W14 + "}val"


def ligature(value):
    node = etree.Element(LIGATURES, nsmap={"w14": W14})
    node.set(VALUE, value)
    return node


def property_history(root, current="standard", previous="none"):
    change = next(root.iter(w("rPrChange")))
    props = change.getparent()
    props.insert(props.index(change), ligature(current))
    change[0].append(ligature(previous))
    return props


@pytest.mark.parametrize("decision", ["accept", "reject"])
@pytest.mark.parametrize("current,previous", [
    ("standard", "none"), ("all", "historical"),
    ("contextualHistoricalDiscretional", "standardContextualHistorical"),
])
def test_ligature_property_outcome_is_exact_and_preserves_other_formatting(
    tmp_path, demo_dir, decision, current, previous,
):
    def mutate(root):
        property_history(root, current, previous)

    path = make(tmp_path, demo_dir, "rPrChange", mutate)
    before = document(path)
    old = next(before.iter(w("rPrChange")))
    selected = old.getparent()
    expected = copy.deepcopy(selected if decision == "accept" else old[0])
    for history in expected.findall(w("rPrChange")):
        expected.remove(history)
    save(path, [op(path, decision, "rPrChange")])
    actual = document(path)
    new = next(actual.iter(LIGATURES)).getparent()
    assert normalized(new) == normalized(expected)
    assert new.find(LIGATURES).get(VALUE) == (current if decision == "accept" else previous)
    # Replace only the selected property set in the independent original.
    selected.getparent().replace(selected, copy.deepcopy(expected))
    assert normalized(actual) == normalized(before)
    assert read_document_revisions(str(path))["total_count"] == 0


@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_paragraph_mark_ligature_history_has_exact_outcome(tmp_path, demo_dir, decision):
    def mutate(root):
        props = property_history(root)
        run = props.getparent()
        paragraph = run.getparent()
        run.remove(props)
        pp = etree.Element(w("pPr"))
        pp.append(props)
        paragraph.insert(0, pp)

    path = make(tmp_path, demo_dir, "rPrChange", mutate)
    before = document(path)
    history = next(before.iter(w("rPrChange")))
    selected = history.getparent()
    expected = copy.deepcopy(selected if decision == "accept" else history[0])
    for old in expected.findall(w("rPrChange")):
        expected.remove(old)
    save(path, [op(path, decision, "rPrChange")])
    selected.getparent().replace(selected, expected)
    assert normalized(document(path)) == normalized(before)
    assert read_document_revisions(str(path))["total_count"] == 0


@pytest.mark.parametrize("kind", ["ins", "del"])
@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_text_outcome_preserves_known_ligature_leaf(tmp_path, demo_dir, kind, decision):
    def mutate(root):
        revision = next(root.iter(w(kind)))
        run = revision.find(w("r"))
        props = etree.Element(w("rPr"))
        etree.SubElement(props, w("b"))
        props.append(ligature("standardContextual"))
        run.insert(0, props)

    path = make(tmp_path, demo_dir, kind, mutate)
    before = document(path)
    revision = next(before.iter(w(kind)))
    expected_properties = normalized(revision.find(f"{w('r')}/{w('rPr')}"))
    save(path, [op(path, decision, kind)])
    after = document(path)
    kept = (kind == "ins") == (decision == "accept")
    leaves = list(after.iter(LIGATURES))
    assert len(leaves) == int(kept)
    if kept:
        assert normalized(leaves[0].getparent()) == expected_properties
    assert read_document_revisions(str(path))["total_count"] == 0


@pytest.mark.parametrize("fault", [
    "missing_value", "unqualified_value", "unknown_value", "extra_attribute",
    "nested_known", "nested_unknown", "text", "tail", "duplicate", "before_legacy",
    "wrong_namespace", "wrong_parent",
])
@pytest.mark.parametrize("kind", ["rPrChange", "ins"])
def test_unproved_ligature_shapes_refuse_without_file_or_backup(
    tmp_path, demo_dir, kind, fault,
):
    def mutate(root):
        if kind == "rPrChange":
            props = property_history(root)
        else:
            run = next(root.iter(w("ins"))).find(w("r"))
            props = etree.Element(w("rPr"))
            etree.SubElement(props, w("b"))
            props.append(ligature("standard"))
            run.insert(0, props)
        leaf = props.find(LIGATURES)
        if fault == "missing_value":
            leaf.attrib.clear()
        elif fault == "unqualified_value":
            leaf.attrib.clear()
            leaf.set("val", "standard")
        elif fault == "unknown_value":
            leaf.set(VALUE, "future")
        elif fault == "extra_attribute":
            leaf.set(w("val"), "standard")
        elif fault.startswith("nested_"):
            etree.SubElement(leaf, w("b") if fault == "nested_known" else "{urn:fictional}payload")
        elif fault == "text":
            leaf.text = "unexpected"
        elif fault == "tail":
            leaf.tail = "unexpected"
        elif fault == "duplicate":
            props.insert(props.index(leaf), copy.deepcopy(leaf))
        elif fault == "before_legacy":
            props.remove(leaf)
            props.insert(0, leaf)
        elif fault == "wrong_namespace":
            leaf.tag = "{urn:fictional}ligatures"
        elif fault == "wrong_parent":
            props.remove(leaf)
            props[0].append(leaf)

    path = make(tmp_path, demo_dir, kind, mutate)
    before = path.read_bytes()
    dry = preview(path, [op(path, kind=kind)])
    assert not dry["batch_applicable"], dry
    assert path.read_bytes() == before
    assert not (tmp_path / ".veqtor-backups").exists()


def test_ligature_support_does_not_open_other_w14_properties(tmp_path, demo_dir):
    def mutate(root):
        props = property_history(root)
        props.find(LIGATURES).tag = "{" + W14 + "}stylisticSets"

    path = make(tmp_path, demo_dir, "rPrChange", mutate)
    before = path.read_bytes()
    assert not preview(path, [op(path, kind="rPrChange")])["batch_applicable"]
    assert path.read_bytes() == before
