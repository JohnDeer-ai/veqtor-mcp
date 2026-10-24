# SPDX-License-Identifier: Apache-2.0
"""Literal body and opaque-data outcomes, including Word save annotations."""
import copy
import hashlib

import pytest
from lxml import etree

from veqtor_docx import clean_copy, revision_inventory as inventory
from veqtor_docx._ooxml import DocxError
from test_revision_inventory import archive, document, xml
from test_user_xml_storage import (
    DS, EXPECTED, W_NS, XSD, fixture, parts, q, set_xml, unsupported,
)

MC = inventory._MC
W14 = inventory._W14


def named_references(package, data, props):
    for name, prop in zip(data, props):
        namespace = etree.QName(etree.fromstring(package[name])).namespace
        set_xml(package, prop, lambda root: etree.SubElement(root[0], q(DS, "schemaRef"),
                                                           attrib={q(DS, "uri"): namespace}))


def root_mc(package, name="word/document.xml", *, token="future", namespace="urn:fictional:unused"):
    root = etree.fromstring(package[name])
    new = etree.Element(root.tag, nsmap={**root.nsmap, token: namespace})
    new.attrib.update(root.attrib)
    new.text = root.text
    new[:] = root[:]
    new.set(q(MC, "Ignorable"), token)
    package[name] = etree.tostring(new)


@pytest.mark.parametrize("named,mc,relocated", [(True, False, False), (False, True, False),
    (True, True, False), (True, True, True)])
@pytest.mark.parametrize("clean", [False, True])
def test_literal_export_keeps_schema_associations_and_root_annotation_exact(named, mc, relocated, clean, monkeypatch):
    package, data, props = fixture(relocated=relocated)
    if named:
        named_references(package, data, props)
    if clean:
        package["word/document.xml"] = etree.tostring(document('<w:p><w:r><w:t>' + EXPECTED + '</w:t></w:r></w:p>'))
    if mc:
        root_mc(package)
        root_mc(package, "word/styles.xml", token="another", namespace="urn:fictional:also-unused")
    # Expected bytes are captured before engine execution, never from Word or
    # from a generated second ZIP with unrelated timestamps.
    before = copy.deepcopy(package)
    source = archive(package)
    monkeypatch.setattr(etree, "XMLSchema", lambda *a, **k: pytest.fail("schema must not be compiled"))
    monkeypatch.setattr(etree, "XSLT", lambda *a, **k: pytest.fail("schema must not be transformed"))
    result = clean_copy.prepare_clean_copy(source, expected_source_sha256=hashlib.sha256(source).hexdigest())
    after = parts(result.payload)
    assert set(after) == set(before)
    assert all(after[n] == v for n, v in before.items() if n != "word/document.xml")
    root = etree.fromstring(after["word/document.xml"])
    assert "".join(root.itertext()) == EXPECTED
    assert not list(root.iter(q(W_NS, "ins"))) and not list(root.iter(q(W_NS, "del")))
    assert root.attrib == etree.fromstring(before["word/document.xml"]).attrib
    assert root.nsmap == etree.fromstring(before["word/document.xml"]).nsmap
    assert result.inventory_after["clean_eligible"]
    assert not result.inventory_after["revisions"]
    for name, prop in zip(data, props):
        assert after[name] == before[name] and after[prop] == before[prop]
    assert b"30" in after[data[0]]  # Body decisions never replace stored values.
    assert package == before and parts(source) == before
    if clean:
        assert result.payload == source  # Includes complete original ZIP bytes.


@pytest.mark.parametrize("namespace", ["urn:fictional:static-schema", "https://example.invalid/schema/data", XSD])
def test_supported_schema_identity_is_preserved_not_validated(namespace):
    package, data, props = fixture(count=1,
        payload=('<u:record xmlns:u="' + namespace + '"><u:value>30</u:value></u:record>').encode())
    named_references(package, data, props)
    root_mc(package)
    before = copy.deepcopy(package)
    source = archive(package)
    out = parts(clean_copy.prepare_clean_copy(source,
        expected_source_sha256=hashlib.sha256(source).hexdigest()).payload)
    assert all(out[n] == v for n, v in before.items() if n != "word/document.xml")
    assert out[data[0]] == before[data[0]]


@pytest.mark.parametrize("mutation", ["missing", "duplicate_list", "duplicate_ref", "mismatched_namespace",
    "unqualified_uri", "whitespace_uri", "extra_attribute", "child", "mixed_text", "tail", "wrong_leaf", "bad_guid"])
def test_named_reference_is_a_closed_association_not_generic_schema_permission(mutation):
    package, data, props = fixture(count=1)
    named_references(package, data, props)
    root_mc(package)
    def change(root):
        refs, ref = root[0], root[0][0]
        if mutation == "missing":
            root.remove(refs)
        elif mutation == "duplicate_list":
            root.append(copy.deepcopy(refs))
        elif mutation == "duplicate_ref":
            refs.append(copy.deepcopy(ref))
        elif mutation == "mismatched_namespace":
            ref.set(q(DS, "uri"), "urn:fictional:different")
        elif mutation == "unqualified_uri":
            ref.set("uri", ref.attrib.pop(q(DS, "uri")))
        elif mutation == "whitespace_uri":
            ref.set(q(DS, "uri"), " " + ref.get(q(DS, "uri")))
        elif mutation == "extra_attribute":
            ref.set("future", "x")
        elif mutation == "child":
            etree.SubElement(ref, "future")
        elif mutation == "mixed_text":
            refs.text = "future"
        elif mutation == "tail":
            ref.tail = "future"
        elif mutation == "wrong_leaf":
            ref.tag = q(DS, "future")
        else:
            root.set(q(DS, "itemID"), "invalid")
    set_xml(package, props[0], change)
    unsupported(package)


@pytest.mark.parametrize("namespace", ["relative", "urn:", "urn:x",
    "https://", "https://example.invalid/schema?x=1", "https://example.invalid/schema#fragment",
    "file:///private/schema.xsd", "http://www.w3.org/1999/XSL/Transform",
    "http://schemas.microsoft.com/office/intelligence/2020/intelligence"])
def test_unsupported_root_association_never_enters_opacity(namespace):
    package, data, props = fixture(count=1, payload=('<u:record xmlns:u="' + namespace + '"/>').encode())
    named_references(package, data, props)
    root_mc(package)
    unsupported(package)


def test_malformed_namespace_is_a_controlled_refusal_before_association_proof():
    package, _, _ = fixture(count=1, payload=b'<u:record xmlns:u="urn:fictional:space here"/>')
    source = archive(package)
    with pytest.raises(DocxError):
        inventory.inventory_package_revisions(source)
    with pytest.raises(DocxError):
        clean_copy.prepare_clean_copy(source, expected_source_sha256=hashlib.sha256(source).hexdigest())
    assert parts(source) == package


@pytest.mark.parametrize("mutation", ["unbound", "empty", "duplicate", "bad_token", "core_namespace",
    "nested", "MustUnderstand", "ProcessContent", "PreserveElements", "AlternateContent",
    "unknown_child", "unknown_attribute", "payload", "properties", "relationships"])
def test_root_mc_never_hides_unknown_content_or_new_consumers(mutation):
    package, data, props = fixture(count=1)
    named_references(package, data, props)
    root_mc(package)
    def change(root):
        if mutation in {"unbound", "empty", "duplicate", "bad_token", "core_namespace"}:
            root.set(q(MC, "Ignorable"), {"unbound": "notBound", "empty": "  ",
                "duplicate": "future future", "bad_token": "future:bad", "core_namespace": "w"}[mutation])
        elif mutation == "nested":
            next(root.iter(q(W_NS, "p"))).set(q(MC, "Ignorable"), "future")
        elif mutation in {"MustUnderstand", "ProcessContent", "PreserveElements"}:
            root.set(q(MC, mutation), "future")
        elif mutation in {"AlternateContent", "unknown_child"}:
            etree.SubElement(root, q(MC if mutation == "AlternateContent" else "urn:fictional:unused", mutation))
        elif mutation == "unknown_attribute":
            root.set(q("urn:fictional:unused", "consumer"), "x")
    set_xml(package, "word/document.xml", change)
    if mutation in {"payload", "properties", "relationships"}:
        name = {"payload": data[0], "properties": props[0], "relationships": "word/_rels/document.xml.rels"}[mutation]
        root_mc(package, name)
    unsupported(package)


@pytest.mark.parametrize("markup", [
    '<w:r><w:instrText>DOCPROPERTY Fictional</w:instrText></w:r>',
    '<w:sdt><w:sdtPr><w:dataBinding w:storeItemID="unrelated"/></w:sdtPr><w:sdtContent/></w:sdt>',
    '<w:customXml/>', '<w:drawing/>', '<w:attachedSchema w:val="urn:fictional:schema"/>',
])
def test_word_annotations_do_not_relax_complete_consumer_exclusion(markup):
    package, data, props = fixture(count=1)
    named_references(package, data, props)
    root_mc(package)
    def add(root):
        next(root.iter(q(W_NS, "p"))).append(xml(f'<w:wrapper xmlns:w="{W_NS}">{markup}</w:wrapper>')[0])
    set_xml(package, "word/document.xml", add)
    unsupported(package)


def test_recognized_extension_keeps_its_actual_value_under_ignorable():
    package, data, props = fixture(count=1)
    named_references(package, data, props)
    root_mc(package, token="w14", namespace=W14)
    def add(root):
        run = next(root.iter(q(W_NS, "r")))
        rpr = etree.Element(q(W_NS, "rPr"))
        etree.SubElement(rpr, q(W14, "ligatures"), attrib={q(W14, "val"): "standardContextual"})
        run.insert(0, rpr)
    set_xml(package, "word/document.xml", add)
    source = archive(package)
    result = clean_copy.prepare_clean_copy(source, expected_source_sha256=hashlib.sha256(source).hexdigest())
    root = etree.fromstring(parts(result.payload)["word/document.xml"])
    assert root.find('.//' + q(W14, "ligatures")).get(q(W14, "val")) == "standardContextual"
    assert "".join(root.itertext()) == EXPECTED
