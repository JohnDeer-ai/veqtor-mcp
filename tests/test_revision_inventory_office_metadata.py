# SPDX-License-Identifier: Apache-2.0
"""Closed metadata inspection fixtures; all identities and document text are fictional."""
import copy
import hashlib

import pytest
from lxml import etree

from veqtor_docx import revision_inventory as inventory
from veqtor_docx._ooxml import W_NS, w
from test_revision_inventory import REL, ODREL, WORD, codes, document, package

W14 = inventory._W14
W15 = inventory._W15
CID = inventory._W16CID
CEX = inventory._W16CEX
MC = inventory._MC
FORMS = "http://schemas.microsoft.com/sharepoint/v3/contenttype/forms"
PEOPLE_REL = "http://schemas.microsoft.com/office/2011/relationships/people"
IDENTITY_REL = "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds"
EXTENSIBLE_REL = "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible"


def q(namespace, local):
    return "{" + namespace + "}" + local


def auxiliary(root, content_type, relationship, name="word/fictional-metadata.xml", *, links=1, alias=None, alias_source="main"):
    rels = etree.Element(q(REL, "Relationships"), nsmap={None: REL})
    target = "../" + name if name.startswith("customXml/") else name.removeprefix("word/")
    for i in range(links):
        etree.SubElement(rels, q(REL, "Relationship"), Id=f"rIdMetadata{i}", Type=relationship,
                         Target=target)
    extras = [(name, content_type, root)]
    if alias is not None:
        alias_rels = rels if alias_source == "main" else etree.Element(q(REL, "Relationships"), nsmap={None: REL})
        etree.SubElement(alias_rels, q(REL, "Relationship"), Id="rIdWrongAlias", Type=alias, Target=target)
        if alias_source != "main":
            extras.append(("word/_rels/header1.xml.rels", "application/vnd.openxmlformats-package.relationships+xml", alias_rels))
    return package(extras=[*extras,
        ("word/_rels/document.xml.rels", "application/vnd.openxmlformats-package.relationships+xml", rels)])


def inspect_unchanged(payload):
    original = bytes(payload)
    result = inventory.inventory_package_revisions(payload)
    assert payload == original
    assert result["source_sha256"] == hashlib.sha256(original).hexdigest()
    return result


def styles(value=None):
    root = etree.Element(w("styles"), nsmap={"w": W_NS})
    style = etree.SubElement(root, w("style"), {w("type"): "paragraph", w("styleId"): "FictionalStyle"})
    etree.SubElement(style, w("name"), {w("val"): "Fictional Style"})
    locked = etree.SubElement(style, w("locked"))
    if value is not None:
        locked.set(w("val"), value)
    return root


@pytest.mark.parametrize("value", [None, "on", "off"])
def test_style_locked_is_a_closed_on_off_only_leaf(value):
    payload = package(extras=[("word/styles.xml", WORD + "styles+xml", styles(value))])
    assert inspect_unchanged(payload)["clean_eligible"]


@pytest.mark.parametrize("mutation", ["value", "attribute", "child", "duplicate", "parent"])
def test_style_locked_rejects_future_values_and_other_contexts(mutation):
    root = styles()
    node = next(root.iter(w("locked")))
    if mutation == "value":
        node.set(w("val"), "future")
    elif mutation == "attribute":
        node.set(w("future"), "1")
    elif mutation == "child":
        etree.SubElement(node, w("r"))
    elif mutation == "duplicate":
        node.addnext(copy.deepcopy(node))
    else:
        node.getparent().remove(node)
        root.append(node)
    result = inspect_unchanged(package(extras=[("word/styles.xml", WORD + "styles+xml", root)]))
    assert "unsupported_auxiliary_xml" in codes(result)
    assert not result["complete"]


@pytest.mark.parametrize("kind", ["locked", "ligatures"])
def test_duplicate_metadata_has_linear_sibling_traversal(monkeypatch, kind):
    count = 2048
    if kind == "locked":
        root = styles()
        owner = next(root.iter(w("style")))
        leaf = next(root.iter(w("locked")))
        extras = [("word/styles.xml", WORD + "styles+xml", root)]
        main = None
    else:
        main = ligature_document()
        owner = next(main.iter(w("rPr")))
        leaf = next(main.iter(q(W14, "ligatures")))
        extras = []
    for _ in range(count - 1):
        owner.append(copy.deepcopy(leaf))
    payload = package(main, extras) if main is not None else package(extras=extras)
    visits = 0

    class MeteredElement(etree.ElementBase):
        def __iter__(self):
            nonlocal visits
            for child in super().__iter__():
                visits += 1
                assert visits <= 64 * count, "duplicate metadata rescanned its siblings quadratically"
                yield child

    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    parser.set_element_class_lookup(etree.ElementDefaultClassLookup(element=MeteredElement))
    original = inventory.parse_xml

    def parse_metered(data):
        original(data)
        return etree.fromstring(data, parser)

    monkeypatch.setattr(inventory, "parse_xml", parse_metered)
    result = inspect_unchanged(payload)
    assert not result["complete"] and not result["clean_eligible"]
    assert "unsupported_auxiliary_xml" in codes(result)


def web_settings():
    root = etree.Element(w("webSettings"), nsmap={"w": W_NS})
    divs = etree.SubElement(root, w("divs"))
    def div(parent, ident):
        item = etree.SubElement(parent, w("div"), {w("id"): ident})
        for name in ("marLeft", "marRight", "marTop", "marBottom"):
            etree.SubElement(item, w(name), {w("val"): "120"})
        return item
    first = div(divs, "101")
    border = etree.SubElement(first, w("divBdr"))
    for name in ("top", "left", "bottom", "right"):
        etree.SubElement(border, w(name), {w("val"): "single", w("color"): "auto", w("sz"): "8"})
    div(etree.SubElement(first, w("divsChild")), "102")
    return root


def test_nested_web_divisions_have_a_closed_context_and_border_grammar():
    assert inspect_unchanged(package(extras=[("word/webSettings.xml", WORD + "webSettings+xml", web_settings())]))["clean_eligible"]


@pytest.mark.parametrize("mutation", ["missing_margin", "duplicate_border", "future_value", "future_attribute", "oversized_number", "known_wrong_child", "duplicate_id", "wrong_context"])
def test_web_divisions_do_not_hide_unknown_or_misplaced_content(mutation):
    root = web_settings()
    div = next(root.iter(w("div")))
    border = div.find(w("divBdr"))
    if mutation == "missing_margin":
        div.remove(div.find(w("marLeft")))
    elif mutation == "duplicate_border":
        border.append(copy.deepcopy(border[0]))
    elif mutation == "future_value":
        border[0].set(w("val"), "future")
    elif mutation == "future_attribute":
        border[0].set("future", "opaque")
    elif mutation == "oversized_number":
        border[0].set(w("sz"), "9" * 5000)
    elif mutation == "known_wrong_child":
        etree.SubElement(div, w("p"))
    elif mutation == "duplicate_id":
        list(root.iter(w("div")))[1].set(w("id"), "101")
    else:
        root = document()
        root.find(w("body")).append(border)
    payload = package(extras=[("word/webSettings.xml", WORD + "webSettings+xml", root)])
    assert not inspect_unchanged(payload)["clean_eligible"]


def ligature_document(value="standard", trailing=""):
    return document(f'<w:p xmlns:w14="{W14}"><w:r><w:rPr><w:b/><w14:ligatures w14:val="{value}"/>{trailing}</w:rPr><w:t>Fictional office text.</w:t></w:r></w:p>')


@pytest.mark.parametrize("value", sorted(inventory._LIGATURES))
def test_documented_ligatures_are_only_a_final_run_property(value):
    assert inspect_unchanged(package(ligature_document(value)))["clean_eligible"]


def test_ligatures_can_precede_the_single_run_property_change_record():
    root = ligature_document()
    node = next(root.iter(q(W14, "ligatures")))
    change = etree.Element(w("rPrChange"), {w("id"): "1", w("author"): "Fictional Reviewer"})
    original = etree.SubElement(change, w("rPr"))
    etree.SubElement(original, q(W14, "ligatures"), {q(W14, "val"): "none"})
    node.addnext(change)
    result = inspect_unchanged(package(root))
    assert result["clean_eligible"]
    assert [row["kind"] for row in result["revisions"]] == ["rPrChange"]


@pytest.mark.parametrize("history", [False, True])
def test_ligatures_in_current_and_original_paragraph_mark_properties(history):
    original = ('<w:rPrChange w:id="1" w:author="Fictional Reviewer"><w:rPr><w14:ligatures w14:val="none"/></w:rPr></w:rPrChange>' if history else '')
    root = document(f'<w:p xmlns:w14="{W14}"><w:pPr><w:rPr><w14:ligatures w14:val="standard"/>{original}</w:rPr></w:pPr><w:r><w:t>Fictional text.</w:t></w:r></w:p>')
    assert inspect_unchanged(package(root))["clean_eligible"]


@pytest.mark.parametrize("content", [
    '<w:p><w:pPr><w:pPrChange><w:pPr><w:rPr>{}</w:rPr></w:pPr></w:pPrChange></w:pPr></w:p>',
    '<w:p><w:r><w:rPr><w:rPrChange><w:rPr>{}<w:rPrChange><w:rPr/></w:rPrChange></w:rPr></w:rPrChange></w:rPr></w:r></w:p>',
])
def test_ligatures_do_not_open_paragraph_property_history_or_nested_run_history(content):
    leaf = f'<w14:ligatures xmlns:w14="{W14}" w14:val="standard"/>'
    assert not inspect_unchanged(package(document(content.format(leaf))))["clean_eligible"]


@pytest.mark.parametrize("mutation", ["enum", "missing_value", "extra_attribute", "child", "duplicate", "multiple_changes", "following_property", "wrong_parent", "wrong_part"])
def test_ligature_extension_does_not_open_arbitrary_word_2010_content(mutation):
    root = ligature_document()
    node = next(root.iter(q(W14, "ligatures")))
    if mutation == "enum":
        node.set(q(W14, "val"), "future")
    elif mutation == "missing_value":
        node.attrib.clear()
    elif mutation == "extra_attribute":
        node.set(q(W14, "future"), "1")
    elif mutation == "child":
        etree.SubElement(node, w("t")).text = "Fictional hidden content"
    elif mutation == "duplicate":
        node.addnext(copy.deepcopy(node))
    elif mutation == "multiple_changes":
        node.addnext(etree.Element(w("rPrChange")))
        node.addnext(etree.Element(w("rPrChange")))
    elif mutation == "following_property":
        node.addnext(etree.Element(w("i")))
    elif mutation == "wrong_parent":
        node.getparent().remove(node)
        root.find(w("body")).append(node)
    else:
        settings = etree.Element(w("settings"), nsmap={"w": W_NS})
        settings.append(node.getparent())
        assert not inspect_unchanged(package(extras=[("word/settings.xml", WORD + "settings+xml", settings)]))["clean_eligible"]
        return
    assert not inspect_unchanged(package(root))["clean_eligible"]


@pytest.mark.parametrize("owner", ["style", "lvl", "rPrDefault", "tblStylePr", "r"])
def test_ligature_property_requires_valid_owner_ancestry(owner):
    root = document(f'<w:{owner} xmlns:w14="{W14}"><w:rPr><w14:ligatures w14:val="standard"/></w:rPr></w:{owner}>')
    result = inspect_unchanged(package(root))
    assert "unsupported_auxiliary_xml" in codes(result)
    assert not result["clean_eligible"]


@pytest.mark.parametrize("context", [
    '<w:p><w:sdt><w:sdtContent><w:p><w:r>{}</w:r></w:p></w:sdtContent></w:sdt></w:p>',
    '<w:sdt><w:sdtContent><w:r>{}</w:r></w:sdtContent></w:sdt>',
])
def test_ligature_run_properties_require_one_real_paragraph_context(context):
    properties = f'<w:rPr xmlns:w14="{W14}"><w14:ligatures w14:val="standard"/></w:rPr>'
    result = inspect_unchanged(package(document(context.format(properties))))
    assert "unsupported_auxiliary_xml" in codes(result)
    assert not result["clean_eligible"]


@pytest.mark.parametrize("ancestry", [
    ["style", "pPr"], ["docDefaults", "pPrDefault", "pPr"],
    ["style", "tblStylePr", "pPr"],
])
def test_ligature_paragraph_mark_properties_cannot_borrow_style_or_default_ppr_types(ancestry):
    root = etree.Element(w("styles"), nsmap={"w": W_NS, "w14": W14})
    parent = root
    for local in ancestry:
        parent = etree.SubElement(parent, w(local))
    props = etree.SubElement(parent, w("rPr"))
    etree.SubElement(props, q(W14, "ligatures"), {q(W14, "val"): "standard"})
    result = inspect_unchanged(package(extras=[("word/styles.xml", WORD + "styles+xml", root)]))
    assert not result["clean_eligible"]


@pytest.mark.parametrize("root_name,content_type,ancestry", [
    ("styles", WORD + "styles+xml", ["style"]),
    ("styles", WORD + "styles+xml", ["docDefaults", "rPrDefault"]),
    ("styles", WORD + "styles+xml", ["style", "tblStylePr"]),
    ("numbering", WORD + "numbering+xml", ["abstractNum", "lvl"]),
])
def test_ligatures_do_not_extend_style_default_table_style_or_numbering_property_types(root_name, content_type, ancestry):
    root = etree.Element(w(root_name), nsmap={"w": W_NS, "w14": W14})
    parent = root
    for local in ancestry:
        parent = etree.SubElement(parent, w(local))
    properties = etree.SubElement(parent, w("rPr"))
    etree.SubElement(properties, q(W14, "ligatures"), {q(W14, "val"): "standard"})
    assert not inspect_unchanged(package(extras=[("word/fictional-properties.xml", content_type, root)]))["clean_eligible"]


@pytest.mark.parametrize("root_name,content_type,ancestry", [
    ("hdr", WORD + "header+xml", ["p", "r"]),
    ("comments", WORD + "comments+xml", ["comment", "p", "r"]),
    ("footnotes", WORD + "footnotes+xml", ["footnote", "p", "r"]),
    ("endnotes", WORD + "endnotes+xml", ["endnote", "p", "r"]),
])
def test_ligature_supported_story_ancestry(root_name, content_type, ancestry):
    root = etree.Element(w(root_name), nsmap={"w": W_NS, "w14": W14})
    parent = root
    for local in ancestry:
        parent = etree.SubElement(parent, w(local))
    properties = etree.SubElement(parent, w("rPr"))
    etree.SubElement(properties, q(W14, "ligatures"), {q(W14, "val"): "standard"})
    assert inspect_unchanged(package(extras=[("word/fictional-properties.xml", content_type, root)]))["clean_eligible"]


def people():
    root = etree.Element(q(W15, "people"), nsmap={"w15": W15, "mc": MC})
    root.set(q(MC, "Ignorable"), "w15")
    person = etree.SubElement(root, q(W15, "person"), {q(W15, "author"): "Fictional Reviewer"})
    etree.SubElement(person, q(W15, "presenceInfo"), {q(W15, "providerId"): "None", q(W15, "userId"): "Fictional Reviewer"})
    return root


def test_people_part_requires_documented_type_root_and_relationship():
    payload = auxiliary(people(), WORD + "people+xml", PEOPLE_REL)
    assert inspect_unchanged(payload)["clean_eligible"]


def test_duplicate_people_relationships_do_not_create_an_unambiguous_binding():
    result = inspect_unchanged(auxiliary(people(), WORD + "people+xml", PEOPLE_REL, links=2))
    assert "unsupported_relationship" in codes(result)
    assert not result["complete"]


@pytest.mark.parametrize("kind", ["people", "commentsEx", "canonical_ids", "legacy_ids", "canonical_extensible", "legacy_extensible"])
@pytest.mark.parametrize("alias_source", ["main", "header"])
def test_all_incoming_metadata_relationships_require_the_exact_binding(kind, alias_source):
    if kind == "people":
        root, content_type, relationship = people(), WORD + "people+xml", PEOPLE_REL
    else:
        namespace, root_name, child, content_type, relationship = {
            "commentsEx": (W15, "commentsEx", "commentEx", WORD + "commentsExtended+xml", "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"),
            "canonical_ids": (CID, "commentsIds", "commentId", WORD + "commentsIds+xml", IDENTITY_REL),
            "legacy_ids": (CID, "commentsIds", "commentId", "application/vnd.ms-word.commentsIds+xml", IDENTITY_REL),
            "canonical_extensible": (CEX, "commentsExtensible", "commentExtensible", WORD + "commentsExtensible+xml", EXTENSIBLE_REL),
            "legacy_extensible": (CEX, "commentsExtensible", "commentExtensible", "application/vnd.ms-word.commentsExtensible+xml", EXTENSIBLE_REL),
        }[kind]
        root = etree.Element(q(namespace, root_name), nsmap={"meta": namespace})
        attributes = {q(namespace, "paraId"): "00000001"} if namespace != CEX else {}
        if namespace != W15:
            attributes[q(namespace, "durableId")] = "00000002"
        etree.SubElement(root, q(namespace, child), attributes)
    assert inspect_unchanged(auxiliary(root, content_type, relationship))["clean_eligible"]
    result = inspect_unchanged(auxiliary(root, content_type, relationship, alias=ODREL + "styles", alias_source=alias_source))
    assert "unsupported_relationship" in codes(result)
    assert not result["complete"] and not result["clean_eligible"]


@pytest.mark.parametrize("mutation", ["missing_attribute", "extra_attribute", "unknown_child", "duplicate_person", "duplicate_presence", "missing_binding", "wrong_relationship", "wrong_content_type", "wrong_root", "unbound_ignorable"])
def test_people_recognizer_is_closed_and_does_not_infer_identity(mutation):
    root = people()
    kind, content_type, links = PEOPLE_REL, WORD + "people+xml", 1
    if mutation == "missing_attribute":
        root[0][0].attrib.pop(q(W15, "providerId"))
    elif mutation == "extra_attribute":
        root[0].set(q(W15, "future"), "opaque")
    elif mutation == "unknown_child":
        etree.SubElement(root[0][0], w("r"))
    elif mutation == "duplicate_person":
        root.append(copy.deepcopy(root[0]))
    elif mutation == "duplicate_presence":
        root[0].append(copy.deepcopy(root[0][0]))
    elif mutation == "missing_binding":
        links = 0
    elif mutation == "wrong_relationship":
        kind = ODREL + "styles"
    elif mutation == "wrong_content_type":
        content_type = "application/xml"
    elif mutation == "wrong_root":
        root = styles()
    else:
        root.set(q(MC, "Ignorable"), "unbound")
    assert not inspect_unchanged(auxiliary(root, content_type, kind, links=links))["clean_eligible"]


@pytest.mark.parametrize("namespace,root_name,child,attributes,content_type,relationship", [
    (CID, "commentsIds", "commentId", {q(CID, "paraId"): "00000001", q(CID, "durableId"): "00000002"}, WORD + "commentsIds+xml", IDENTITY_REL),
    (CEX, "commentsExtensible", "commentExtensible", {q(CEX, "durableId"): "00000002", q(CEX, "dateUtc"): "2026-01-01T00:00:00Z"}, WORD + "commentsExtensible+xml", EXTENSIBLE_REL),
])
def test_modern_comment_canonical_content_types_keep_closed_identity_roots(namespace, root_name, child, attributes, content_type, relationship):
    root = etree.Element(q(namespace, root_name), nsmap={"meta": namespace})
    etree.SubElement(root, q(namespace, child), attributes)
    assert inspect_unchanged(auxiliary(root, content_type, relationship))["clean_eligible"]
    root[0].set(q(namespace, "future"), "opaque")
    assert not inspect_unchanged(auxiliary(root, content_type, relationship))["clean_eligible"]


@pytest.mark.parametrize("content_type", [WORD + "commentsIds+xml", "application/vnd.ms-word.commentsIds+xml"])
def test_comment_identity_rejects_a_single_hex_digit_even_when_unambiguous(content_type):
    root = etree.Element(q(CID, "commentsIds"), nsmap={"meta": CID})
    etree.SubElement(root, q(CID, "commentId"), {q(CID, "paraId"): "00000001", q(CID, "durableId"): "1"})
    result = inspect_unchanged(auxiliary(root, content_type, IDENTITY_REL))
    assert "unsupported_auxiliary_xml" in codes(result)
    assert not result["clean_eligible"]


@pytest.mark.parametrize("namespace,root_name,child,content_type,relationship", [
    (CID, "commentsIds", "commentId", WORD + "commentsIds+xml", IDENTITY_REL),
    (CEX, "commentsExtensible", "commentExtensible", WORD + "commentsExtensible+xml", EXTENSIBLE_REL),
])
@pytest.mark.parametrize("mutation", ["zero_identity", "signed_limit", "duplicate_hex_case", "generic_xml_type"])
def test_comment_identity_requires_declared_type_and_unique_bounded_identity(namespace, root_name, child, content_type, relationship, mutation):
    root = etree.Element(q(namespace, root_name), nsmap={"meta": namespace})
    attributes = {q(namespace, "durableId"): "000000a1"}
    if namespace == CID:
        attributes[q(namespace, "paraId")] = "00000001"
    node = etree.SubElement(root, q(namespace, child), attributes)
    if mutation == "zero_identity":
        node.set(q(namespace, "durableId"), "00000000")
    elif mutation == "signed_limit":
        node.set(q(namespace, "durableId"), "7FFFFFFF")
    elif mutation == "duplicate_hex_case":
        duplicate = copy.deepcopy(node)
        duplicate.set(q(namespace, "durableId"), "000000A1")
        if namespace == CID:
            duplicate.set(q(namespace, "paraId"), "00000002")
        root.append(duplicate)
    else:
        content_type = "application/xml"
    assert not inspect_unchanged(auxiliary(root, content_type, relationship))["clean_eligible"]


@pytest.mark.parametrize("value", ["2026-01-01 00:00:00Z", "20260101T000000Z", "2026-01-01T00:00:00", "2026-01-01T00:00:00+01:00", "2026-02-30T00:00:00Z"])
def test_comment_date_requires_a_valid_xml_datetime_in_utc(value):
    root = etree.Element(q(CEX, "commentsExtensible"), nsmap={"meta": CEX})
    etree.SubElement(root, q(CEX, "commentExtensible"), {q(CEX, "durableId"): "00000002", q(CEX, "dateUtc"): value})
    assert not inspect_unchanged(auxiliary(root, WORD + "commentsExtensible+xml", EXTENSIBLE_REL))["clean_eligible"]


def forms():
    root = etree.Element(q(FORMS, "FormTemplates"), nsmap={None: FORMS})
    for name in ("Display", "Edit", "New"):
        etree.SubElement(root, q(FORMS, name)).text = "FictionalTemplate"
    return root


def test_documented_sharepoint_form_template_leaves_are_separate_from_custom_fields():
    payload = auxiliary(forms(), "application/xml", ODREL + "customXml", "customXml/fictional-forms.xml")
    assert inspect_unchanged(payload)["clean_eligible"]


@pytest.mark.parametrize("mutation", ["unknown_attribute", "nested_known_word_node", "duplicate", "missing", "unbound"])
def test_form_templates_do_not_open_sharepoint_or_infopath_namespaces(mutation):
    root, links = forms(), 1
    if mutation == "unknown_attribute":
        root.set("future", "opaque")
    elif mutation == "nested_known_word_node":
        etree.SubElement(root[0], w("p"))
    elif mutation == "duplicate":
        root.append(copy.deepcopy(root[0]))
    elif mutation == "missing":
        root.remove(root[-1])
    else:
        links = 0
    assert not inspect_unchanged(auxiliary(root, "application/xml", ODREL + "customXml", "customXml/fictional-forms.xml", links=links))["clean_eligible"]


@pytest.mark.parametrize("namespace,root_name", [
    ("http://schemas.microsoft.com/office/2006/metadata/contentType", "contentTypeSchema"),
    ("http://schemas.microsoft.com/office/2006/metadata/properties", "properties"),
    ("http://schemas.microsoft.com/office/infopath/2007/PartnerControls", "Terms"),
    ("http://schemas.microsoft.com/office/intelligence/2020/intelligence", "intelligence"),
    ("http://www.w3.org/2001/XMLSchema", "schema"),
    ("urn:example:fictional-fields", "FictionalField"),
])
def test_unproved_auxiliary_families_remain_explicit_refusals(namespace, root_name):
    root = etree.Element(q(namespace, root_name), nsmap={None: namespace})
    payload = auxiliary(root, "application/xml", ODREL + "customXml", "customXml/fictional-data.xml")
    result = inspect_unchanged(payload)
    assert not result["complete"] and not result["clean_eligible"]
    assert "unsupported_auxiliary_xml" in codes(result)
