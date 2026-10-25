# SPDX-License-Identifier: Apache-2.0
"""Separate bound preservation profile, not an extension of all style types."""
import copy

import pytest
from lxml import etree

from veqtor_docx import revision_inventory as inventory
from veqtor_docx._ooxml import W_NS, w
from test_revision_inventory import REL, ODREL, WORD, package


def fixture(value="standardContextual"):
    root = etree.Element(w("styles"), nsmap={"w": W_NS, "w14": inventory._W14, "mc": inventory._MC})
    root.set("{" + inventory._MC + "}Ignorable", "w14")
    current = root
    for tag in ("docDefaults", "rPrDefault", "rPr"):
        current = etree.SubElement(current, w(tag))
    leaf = etree.SubElement(current, "{" + inventory._W14 + "}ligatures",
                            {"{" + inventory._W14 + "}val": value})
    rels = etree.Element("{" + REL + "}Relationships")
    etree.SubElement(rels, "{" + REL + "}Relationship", Id="rIdStyles", Type=ODREL + "styles", Target="styles.xml")
    return root, leaf, rels


def payload(root, rels, name="word/styles.xml", content_type=WORD + "styles+xml"):
    return package(extras=[(name, content_type, root),
        ("word/_rels/document.xml.rels", "application/vnd.openxmlformats-package.relationships+xml", rels)])


@pytest.mark.parametrize("value", sorted(inventory._LIGATURES))
def test_bound_exact_default_leaf_is_inspected_without_mutation(value):
    root, _, rels = fixture(value)
    source = payload(root, rels)
    assert inventory.inventory_package_revisions(source)["clean_eligible"]
    assert source == payload(root, rels)


@pytest.mark.parametrize("fault", ["unknown_value", "attribute", "child", "text", "tail", "duplicate_leaf",
    "duplicate_defaults", "duplicate_rpr", "history", "rtl", "style", "paragraph_default",
    "not_final", "missing_ignorable", "wrong_prefix", "unbound", "duplicate_binding", "external",
    "alias", "wrong_content_type"])
def test_default_profile_refuses_every_widening(fault):
    root, leaf, rels = fixture()
    properties = leaf.getparent()
    name, ct = "word/styles.xml", WORD + "styles+xml"
    if fault == "unknown_value":
        leaf.set("{" + inventory._W14 + "}val", "future")
    elif fault == "attribute":
        leaf.set(w("future"), "1")
    elif fault == "child":
        etree.SubElement(leaf, w("b"))
    elif fault == "text":
        leaf.text = "hidden"
    elif fault == "tail":
        leaf.tail = "hidden"
    elif fault == "duplicate_leaf":
        properties.append(copy.deepcopy(leaf))
    elif fault == "duplicate_defaults":
        root.append(copy.deepcopy(root[0]))
    elif fault == "duplicate_rpr":
        properties.getparent().append(copy.deepcopy(properties))
    elif fault in {"history", "rtl"}:
        properties.insert(0, etree.Element(w("rPrChange" if fault == "history" else "rtl")))
    elif fault == "style":
        properties.getparent().remove(properties)
        etree.SubElement(root, w("style")).append(properties)
    elif fault == "paragraph_default":
        properties.getparent().tag = w("pPrDefault")
    elif fault == "not_final":
        etree.SubElement(properties, w("b"))
    elif fault == "missing_ignorable":
        root.attrib.clear()
    elif fault == "wrong_prefix":
        root.set("{" + inventory._MC + "}Ignorable", "w")
    elif fault == "unbound":
        rels.remove(rels[0])
    elif fault == "duplicate_binding":
        rels.append(copy.deepcopy(rels[0]))
        rels[-1].set("Id", "rIdOther")
    elif fault == "external":
        rels[0].set("TargetMode", "External")
    elif fault == "alias":
        name = "word/fictional-styles.xml"
        rels[0].set("Target", "fictional-styles.xml")
    else:
        ct = "application/vnd.ms-word.stylesWithEffects+xml"
    result = inventory.inventory_package_revisions(payload(root, rels, name, ct))
    assert not result["clean_eligible"]
    assert any(row["code"] == "unsupported_auxiliary_xml" for row in result["blockers"])
