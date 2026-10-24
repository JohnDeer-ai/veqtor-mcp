# SPDX-License-Identifier: Apache-2.0
"""Fictional role/consumer proofs and independent literal accept-all outcomes."""
import copy
import hashlib
import io
import uuid
import zipfile

import pytest
from lxml import etree

from veqtor_docx import clean_copy, revision_inventory as inventory
from veqtor_docx._ooxml import DocxError, ResourceLimitError, W_NS, w
from test_revision_inventory import CT, ODREL, REL, WORD, archive, codes, document, package, xml

DS = inventory._CUSTOM_XML
PROPS_CT = "application/vnd.openxmlformats-officedocument.customXmlProperties+xml"
RELS_CT = "application/vnd.openxmlformats-package.relationships+xml"
XSD = "http://www.w3.org/2001/XMLSchema"
ORIGINAL = "The fictional notice is 30 days."
EXPECTED = "The fictional notice is 45 days."


def q(ns, local):
    return "{" + ns + "}" + local


def parts(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        return {n: z.read(n) for n in z.namelist()}


def rels_name(owner):
    folder, _, leaf = owner.rpartition("/")
    return (folder + "/" if folder else "") + "_rels/" + leaf + ".rels"


def relationship(parent, ident, kind, target):
    return etree.SubElement(parent, q(REL, "Relationship"), Id=ident, Type=ODREL + kind, Target=target)


def fixture(*, count=2, relocated=False, payload=None, internal=False):
    # These literals, including unchanged opaque '30', are the oracle. No result
    # bytes or private native fixture are used to generate expected data.
    data_names = [f"records/fictional-{i}.xml" if relocated else f"customXml/item{i}.xml" for i in range(count)]
    prop_names = [f"records/identity-{i}.xml" if relocated else f"customXml/itemProps{i}.xml" for i in range(count)]
    main = document('<w:p><w:r><w:t xml:space="preserve">The fictional notice is </w:t></w:r>'
                    '<w:del w:id="1" w:author="Fictional Counsel"><w:r><w:delText>30</w:delText></w:r></w:del>'
                    '<w:ins w:id="2" w:author="Fictional Counsel"><w:r><w:t>45</w:t></w:r></w:ins>'
                    '<w:r><w:t xml:space="preserve"> days.</w:t></w:r></w:p>')
    rels = etree.Element(q(REL, "Relationships"), nsmap={None: REL})
    relationship(rels, "rIdStyle", "styles", "styles.xml")
    extras = [("word/styles.xml", WORD + "styles+xml", xml(f'<w:styles xmlns:w="{W_NS}"/>'))]
    for i, (name, prop) in enumerate(zip(data_names, prop_names)):
        namespace = "urn:veqtor:fictional:user-data:" + str(uuid.uuid4())
        data = (f'<?xml version="1.0"?>\n<case xmlns="{namespace}" note="30"><!-- internal comment -->'
                '<value>Fictional opaque value 30</value></case>\n').encode() if i == 0 else (
                    f'<xs:schema xmlns:xs="{XSD}" targetNamespace="{namespace}">'
                    '<xs:element name="fictional" type="xs:string" default="30"/></xs:schema>').encode()
        if i == 0 and payload is not None:
            data = payload
        prop_root = etree.Element(q(DS, "datastoreItem"), nsmap={"store": DS},
                                  attrib={q(DS, "itemID"): "{" + str(uuid.uuid4()).upper() + "}"})
        etree.SubElement(prop_root, q(DS, "schemaRefs"))
        link = relationship(rels, "rIdFictionalData" + str(i), "customXml", "../" + name)
        target_rels = etree.Element(q(REL, "Relationships"), nsmap={None: REL})
        prop_link = relationship(target_rels, "rIdIdentity", "customXmlProps", prop.rsplit("/", 1)[-1])
        if internal:
            link.set("TargetMode", "Internal")
            prop_link.set("TargetMode", "Internal")
        extras.extend([(name, "application/xml", data), (prop, PROPS_CT, prop_root),
                       (rels_name(name), RELS_CT, target_rels)])
    extras.append(("word/_rels/document.xml.rels", RELS_CT, rels))
    return parts(package(main, extras)), data_names, prop_names


def set_xml(package_parts, name, change):
    root = etree.fromstring(package_parts[name])
    change(root)
    package_parts[name] = etree.tostring(root)


def set_type(package_parts, name, content_type):
    def change(root):
        node = next(n for n in root if n.get("PartName") == "/" + name)
        node.set("ContentType", content_type)
    set_xml(package_parts, "[Content_Types].xml", change)


def unsupported(package_parts):
    source = archive(package_parts)
    snapshot = bytes(source)
    result = inventory.inventory_package_revisions(source)
    assert not result["clean_eligible"]
    assert all(row["inspection"] != "opaque_user_storage" for row in result["parts"])
    with pytest.raises(clean_copy.ApplyError) as caught:
        clean_copy.prepare_clean_copy(source, expected_source_sha256=hashlib.sha256(source).hexdigest())
    assert caught.value.code == "clean_copy_unsupported"
    assert source == snapshot
    return result


@pytest.mark.parametrize("count,relocated,internal", [(1, False, False), (2, False, False),
                                                     (2, True, False), (2, True, True)])
def test_literal_accept_preserves_arbitrary_xml_and_separate_xsd_bytes(count, relocated, internal, monkeypatch):
    source_parts, data, props = fixture(count=count, relocated=relocated, internal=internal)
    literal_before = copy.deepcopy(source_parts)
    monkeypatch.setattr(etree, "XMLSchema", lambda *a, **k: pytest.fail("user schemas must not be compiled"))
    source = archive(source_parts)
    source_root = etree.fromstring(source_parts["word/document.xml"])
    def view(node, original=False):
        if node.tag == w("ins") and original or node.tag == w("del") and not original:
            return ""
        if node.tag in {w("t"), w("delText")}:
            return node.text or ""
        return "".join(view(child, original) for child in node)
    assert view(source_root, True) == ORIGINAL
    assert view(source_root) == EXPECTED
    result = clean_copy.prepare_clean_copy(source, expected_source_sha256=hashlib.sha256(source).hexdigest())
    after = parts(result.payload)
    root = etree.fromstring(after["word/document.xml"])
    assert "".join(n.text or "" for n in root.iter(w("t"))) == EXPECTED
    assert not list(root.iter(w("delText")))
    assert result.accepted_revision_count == 2
    assert source_parts == literal_before
    assert set(after) == set(source_parts)
    assert all(after[n] == v for n, v in source_parts.items() if n != "word/document.xml")
    for inv in [result.inventory_before, result.inventory_after]:
        rows = {r["part_name"]: r for r in inv["parts"]}
        for d, p in zip(data, props):
            assert rows[d]["inspection"] == "opaque_user_storage"
            assert rows[d]["preservation"] == "exact_bytes_only"
            assert rows[d]["properties_part"] == p
    assert not result.inventory_after["revisions"]
    assert "30" in after[data[0]].decode()  # Never substitute business values.


def test_literal_docproperty_word_is_not_a_field_consumer():
    package_parts, _, _ = fixture(count=1)
    set_xml(package_parts, "word/document.xml", lambda root: setattr(next(root.iter(w("t"))), "text", "Literal DOCPROPERTY word: "))
    assert inventory.inventory_package_revisions(archive(package_parts))["clean_eligible"]


@pytest.mark.parametrize("omitted_items", [(0,), (1,), (0, 1)])
@pytest.mark.parametrize("tracked", [False, True])
def test_omitted_schema_references_do_not_prove_explicitly_disabled_schemas(omitted_items, tracked):
    p, _, props = fixture()
    if not tracked:
        p["word/document.xml"] = etree.tostring(document('<w:p><w:r><w:t>' + EXPECTED + '</w:t></w:r></w:p>'))
    for index in omitted_items:
        set_xml(p, props[index], lambda root: root.remove(root[0]))
    result = unsupported(p)
    assert not result["complete"]
    assert "unsupported_user_storage" in codes(result)


@pytest.mark.parametrize("mutation", ["data_mime", "props_mime", "props_root", "missing_main_edge", "wrong_source",
    "duplicate_main", "wrong_alias", "missing_props", "duplicate_props", "shared_props", "extra_data_outgoing",
    "props_outgoing", "orphan_props", "orphan_rels", "external", "query", "encoded", "bad_mode",
    "duplicate_guid", "invalid_guid", "props_unknown_attribute", "props_mixed", "nonempty_schema_refs"])
def test_exact_role_graph_and_closed_properties_refuse(mutation):
    p, data, props = fixture()
    main_rels = "word/_rels/document.xml.rels"
    if mutation == "data_mime":
        set_type(p, data[0], "text/xml")
    elif mutation == "props_mime":
        set_type(p, props[0], "application/xml")
    elif mutation == "props_root":
        set_xml(p, props[0], lambda root: setattr(root, "tag", q(DS, "future")))
    elif mutation in {"missing_main_edge", "wrong_source"}:
        set_xml(p, main_rels, lambda root: root.remove(root[1]))
        if mutation == "wrong_source":
            set_xml(p, "_rels/.rels", lambda root: relationship(root, "rIdWrong", "customXml", data[0]))
    elif mutation in {"duplicate_main", "wrong_alias"}:
        set_xml(p, main_rels, lambda root: relationship(root, "rIdAlias", "customXml" if mutation == "duplicate_main" else "styles", "../" + data[0]))
    elif mutation == "missing_props":
        set_xml(p, rels_name(data[0]), lambda root: root.remove(root[0]))
    elif mutation == "duplicate_props":
        set_xml(p, rels_name(data[0]), lambda root: relationship(root, "rIdDuplicate", "customXmlProps", props[0].split("/")[-1]))
    elif mutation == "shared_props":
        set_xml(p, rels_name(data[1]), lambda root: root[0].set("Target", props[0].split("/")[-1]))
    elif mutation == "extra_data_outgoing":
        set_xml(p, rels_name(data[0]), lambda root: relationship(root, "rIdExtra", "styles", "../word/styles.xml"))
    elif mutation == "props_outgoing":
        p[rels_name(props[0])] = etree.tostring(xml(f'<Relationships xmlns="{REL}"><Relationship Id="rIdUnexpected" Type="{ODREL}styles" Target="../word/styles.xml"/></Relationships>'))
    elif mutation == "orphan_props":
        p["customXml/unbound-properties.xml"] = p[props[0]]
        set_xml(p, "[Content_Types].xml", lambda root: etree.SubElement(root, q(CT, "Override"), PartName="/customXml/unbound-properties.xml", ContentType=PROPS_CT))
    elif mutation == "orphan_rels":
        p["missing/_rels/source.xml.rels"] = p[rels_name(data[0])]
    elif mutation in {"external", "query", "encoded", "bad_mode"}:
        def change(root):
            node = root[1]
            if mutation == "external":
                node.set("TargetMode", "External")
                node.set("Target", "https://example.invalid/fictional.xml")
            elif mutation == "query":
                node.set("Target", node.get("Target") + "?fictional")
            elif mutation == "encoded":
                node.set("Target", node.get("Target").replace("item0", "%69tem0"))
            else:
                node.set("TargetMode", "Future")
        set_xml(p, main_rels, change)
    elif mutation == "duplicate_guid":
        ident = etree.fromstring(p[props[0]]).get(q(DS, "itemID"))
        set_xml(p, props[1], lambda root: root.set(q(DS, "itemID"), ident.lower()))
    elif mutation == "invalid_guid":
        set_xml(p, props[0], lambda root: root.set(q(DS, "itemID"), "fictional"))
    elif mutation == "props_unknown_attribute":
        set_xml(p, props[0], lambda root: root.set("future", "unknown"))
    elif mutation == "props_mixed":
        set_xml(p, props[0], lambda root: setattr(root, "text", "not whitespace"))
    else:
        set_xml(p, props[0], lambda root: etree.SubElement(root[0], q(DS, "schemaRef"), {q(DS, "uri"): "urn:fictional:future-schema"}))
    unsupported(p)


@pytest.mark.parametrize("markup", [
    '<w:fldSimple w:instr="DOCPROPERTY Fictional"/>', '<w:r><w:fldChar w:fldCharType="begin"/></w:r>',
    '<w:r><w:instrText>DOCPROPERTY Fictional</w:instrText></w:r>',
    '<w:del w:id="9"><w:r><w:delInstrText>DOCPROPERTY Fictional</w:delInstrText></w:r></w:del>',
    '<w:sdt><w:sdtPr><w:dataBinding w:storeItemID="unrelated"/></w:sdtPr><w:sdtContent/></w:sdt>',
    '<w:customXml/>', '<w:contentPart/>', '<w:drawing/>', '<w:pict/>',
])
def test_global_consumers_are_not_inferred_absent_from_binding_ids(markup):
    p, _, _ = fixture(count=1)
    set_xml(p, "word/document.xml", lambda root: next(root.iter(w("p"))).append(xml(f'<w:wrapper xmlns:w="{W_NS}">{markup}</w:wrapper>')[0]))
    unsupported(p)


@pytest.mark.parametrize("where", ["main", "payload"])
@pytest.mark.parametrize("namespace,local", [(ODREL.rstrip("/"), "futureRef"), (inventory._MC, "Ignorable")])
def test_all_relationship_and_mc_attributes_are_outside_initial_consumer_proof(where, namespace, local):
    p, data, _ = fixture(count=1)
    name = "word/document.xml" if where == "main" else data[0]
    set_xml(p, name, lambda root: root.set(q(namespace, local), "unrelated"))
    unsupported(p)


@pytest.mark.parametrize("shape", ["root", "child", "attribute", "target_namespace", "declared_only", "xslt", "include", "location"])
def test_office_vocabulary_and_executable_schema_shapes_never_enter_opacity(shape):
    namespace = "http://schemas.microsoft.com/office/intelligence/2020/intelligence"
    if shape == "root":
        data = f'<o:intelligence xmlns:o="{namespace}"/>'
    elif shape == "child":
        data = f'<u:record xmlns:u="urn:fictional:user" xmlns:o="{namespace}"><o:state/></u:record>'
    elif shape == "attribute":
        data = f'<u:record xmlns:u="urn:fictional:user" xmlns:o="{namespace}" o:state="x"/>'
    elif shape == "target_namespace":
        data = f'<xs:schema xmlns:xs="{XSD}" targetNamespace="{namespace}"/>'
    elif shape == "declared_only":
        data = f'<u:record xmlns:u="urn:fictional:user" xmlns:o="{namespace}"/>'
    elif shape == "xslt":
        data = '<x:stylesheet xmlns:x="http://www.w3.org/1999/XSL/Transform" version="1.0"/>'
    elif shape == "include":
        data = f'<xs:schema xmlns:xs="{XSD}"><xs:include schemaLocation="https://example.invalid/schema.xsd"/></xs:schema>'
    else:
        data = '<u:record xmlns:u="urn:fictional:user" xmlns:i="http://www.w3.org/2001/XMLSchema-instance" i:schemaLocation="urn:fictional:user https://example.invalid/schema.xsd"/>'
    p, _, _ = fixture(count=1, payload=data.encode())
    unsupported(p)


@pytest.mark.parametrize("context", ["auxiliary", "story", "opaque"])
def test_other_unknown_context_never_receives_proven_storage_label(context):
    p, _, _ = fixture(count=1)
    if context == "opaque":
        p["future.bin"] = b"opaque unrecognized bytes"
    else:
        p["future.xml"] = b'<u:future xmlns:u="urn:fictional:unknown-context"/>' if context == "auxiliary" else f'<w:futureStory xmlns:w="{W_NS}"/>'.encode()
    result = unsupported(p)
    assert not result["complete"]
    assert "unproved_user_storage_context" in codes(result)


@pytest.mark.parametrize("name", ["[Content_Types].xml", "word/_rels/document.xml.rels", "customXml/item0.xml", "customXml/itemProps0.xml"])
@pytest.mark.parametrize("node", [b'<?fictional instruction?>', b'<!-- outside root -->'])
def test_global_document_envelope_hazards_still_refuse(name, node):
    p, _, _ = fixture(count=1)
    declaration, separator, rest = p[name].partition(b"?>")
    p[name] = declaration + separator + node + rest if p[name].startswith(b"<?xml") else node + p[name]
    unsupported(p)


@pytest.mark.parametrize("namespace", ["urn:fictional:user", W_NS])
def test_payload_revision_walk_remains_global_and_not_exempted(namespace):
    p, _, _ = fixture(count=1, payload=f'<u:record xmlns:u="{namespace}"><u:ins/></u:record>'.encode())
    result = unsupported(p)
    assert "unsupported_revision" in codes(result)
    assert any(r["kind"] == "ins" and not r["supported_location"] for r in result["revisions"])


@pytest.mark.parametrize("target", ["data", "props", "rels", "types", "styles"])
def test_full_package_proof_rejects_collateral_mutations(monkeypatch, target):
    p, data, props = fixture(count=1)
    source = archive(p)
    snapshot = bytes(source)
    original = clean_copy._output_archive_bytes
    def corrupt(infos, values):
        payload = original(infos, values)
        modified = parts(payload)
        name = {"data": data[0], "props": props[0], "rels": "word/_rels/document.xml.rels",
                "types": "[Content_Types].xml", "styles": "word/styles.xml"}[target]
        # Whitespace changes no semantics; complete byte preservation must still fail.
        modified[name] += b"\n"
        return archive(modified)
    monkeypatch.setattr(clean_copy, "_output_archive_bytes", corrupt)
    with pytest.raises(clean_copy.ApplyError) as caught:
        clean_copy.prepare_clean_copy(source, expected_source_sha256=hashlib.sha256(source).hexdigest())
    assert caught.value.code == "round_trip_failed"
    assert source == snapshot


def test_inventory_budget_charges_all_payload_xml_once_and_refuses_one_byte_over(monkeypatch):
    p, _, _ = fixture(count=1)
    source = archive(p)
    byte_count = sum(map(len, p.values()))
    monkeypatch.setattr(inventory, "MAX_INVENTORY_XML_BYTES", byte_count)
    assert inventory.inventory_package_revisions(source)["clean_eligible"]
    monkeypatch.setattr(inventory, "MAX_INVENTORY_XML_BYTES", byte_count - 1)
    with pytest.raises(ResourceLimitError) as caught:
        inventory.inventory_package_revisions(source)
    assert caught.value.limit == "inventory_xml_bytes"


def test_depth_bound_precedes_role_proof(monkeypatch):
    p, _, _ = fixture(count=1, payload=('<record>' * 12 + '</record>' * 12).encode())
    monkeypatch.setattr(inventory, "MAX_INVENTORY_DEPTH", 8)
    with pytest.raises(ResourceLimitError) as caught:
        inventory.inventory_package_revisions(archive(p))
    assert caught.value.limit == "inventory_xml_depth"


def test_documents_without_new_user_stores_do_not_acquire_consumer_refusal():
    source = package(document('<w:p><w:fldSimple w:instr="DOCPROPERTY Fictional"><w:r><w:t>Literal</w:t></w:r></w:fldSimple></w:p>'))
    assert inventory.inventory_package_revisions(source)["clean_eligible"]


@pytest.mark.parametrize("target_kind", ["data", "properties"])
def test_reverse_alias_from_an_existing_external_story_refuses(target_kind):
    p, data, props = fixture(count=1)
    p["word/header1.xml"] = f'<w:hdr xmlns:w="{W_NS}"/>'.encode()
    set_xml(p, "[Content_Types].xml", lambda root: etree.SubElement(root, q(CT, "Override"), PartName="/word/header1.xml", ContentType=WORD + "header+xml"))
    target = data[0] if target_kind == "data" else props[0]
    p["word/_rels/header1.xml.rels"] = f'<Relationships xmlns="{REL}"><Relationship Id="rIdAlias" Type="{ODREL}styles" Target="../{target}"/></Relationships>'.encode()
    unsupported(p)


@pytest.mark.parametrize("part", ["[Content_Types].xml", "word/_rels/document.xml.rels"])
def test_declaration_leaf_children_cannot_hide_consumer_or_unknown_markup(part):
    p, _, _ = fixture(count=1)
    set_xml(p, part, lambda root: etree.SubElement(root[0], "{urn:fictional:unknown}child"))
    unsupported(p)


def test_already_clean_user_storage_preserves_entire_zip():
    p, _, _ = fixture()
    p["word/document.xml"] = etree.tostring(document('<w:p><w:r><w:t>' + EXPECTED + '</w:t></w:r></w:p>'))
    payload = archive(p)
    result = clean_copy.prepare_clean_copy(payload, expected_source_sha256=hashlib.sha256(payload).hexdigest())
    assert result.payload == payload
    assert result.accepted_revision_count == 0


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_stored_user_xml_cannot_hide_dtd_or_entity_declarations(encoding):
    value = '<?xml version="1.0" encoding="' + encoding + '"?><!DOCTYPE record [<!ENTITY content "fictional">]><record>&content;</record>'
    p, _, _ = fixture(count=1, payload=value.encode(encoding))
    with pytest.raises(DocxError):
        inventory.inventory_package_revisions(archive(p))


def rename_part(package_parts, old, new):
    package_parts[new] = package_parts.pop(old)
    set_xml(package_parts, "[Content_Types].xml", lambda root: next(
        n for n in root if n.get("PartName") == "/" + old).set("PartName", "/" + new))


@pytest.mark.parametrize("kind", ["data", "properties", "relationships", "styles"])
def test_ascii_case_equivalent_package_members_cannot_prove_roles(kind):
    p, data, props = fixture()
    if kind == "data":
        alias = "customXml/ITEM0.xml"
        rename_part(p, data[1], alias)
        rename_part(p, rels_name(data[1]), rels_name(alias))
        set_xml(p, "word/_rels/document.xml.rels", lambda root: root[2].set("Target", "../" + alias))
    elif kind == "properties":
        alias = "customXml/ITEMPROPS0.xml"
        rename_part(p, props[1], alias)
        set_xml(p, rels_name(data[1]), lambda root: root[0].set("Target", "ITEMPROPS0.xml"))
    else:
        name = "word/_rels/document.xml.rels" if kind == "relationships" else "word/styles.xml"
        alias = "WORD/_rels/document.xml.rels" if kind == "relationships" else "word/STYLES.xml"
        p[alias] = p[name]
        content_type = RELS_CT if kind == "relationships" else WORD + "styles+xml"
        set_xml(p, "[Content_Types].xml", lambda root: etree.SubElement(
            root, q(CT, "Override"), PartName="/" + alias, ContentType=content_type))
    result = unsupported(p)
    assert not result["complete"]
    assert "ambiguous_opc_part_identity" in codes(result)


@pytest.mark.parametrize("content_type", ["application/xml", "text/xml"])
def test_ascii_case_equivalent_overrides_refuse_even_without_a_second_member(content_type):
    p, _, _ = fixture(count=1)
    set_xml(p, "[Content_Types].xml", lambda root: etree.SubElement(
        root, q(CT, "Override"), PartName="/customXml/ITEM0.xml", ContentType=content_type))
    result = unsupported(p)
    assert not result["complete"]
    assert "ambiguous_opc_override_identity" in codes(result)


def test_distinct_part_names_have_no_new_identity_collision():
    p, data, _ = fixture()
    alias = "customXml/DISTINCT.xml"
    rename_part(p, data[1], alias)
    rename_part(p, rels_name(data[1]), rels_name(alias))
    set_xml(p, "word/_rels/document.xml.rels", lambda root: root[2].set("Target", "../" + alias))
    assert inventory.inventory_package_revisions(archive(p))["clean_eligible"]


@pytest.mark.parametrize("tracked", [False, True])
def test_ambiguous_identity_without_user_stores_refuses_export_even_if_already_clean(tracked):
    styles = f'<w:styles xmlns:w="{W_NS}"/>'.encode()
    body = ('<w:p><w:ins w:id="1" w:author="Fictional Counsel"><w:r><w:t>New fictional text.</w:t></w:r></w:ins></w:p>'
            if tracked else '<w:p><w:r><w:t>Fictional ordinary text.</w:t></w:r></w:p>')
    source = package(document(body), [
        ("word/styles.xml", WORD + "styles+xml", styles),
        ("word/STYLES.xml", WORD + "styles+xml", styles),
        ("word/_rels/document.xml.rels", RELS_CT, xml(f'<Relationships xmlns="{REL}"><Relationship Id="rIdStyle" Type="{ODREL}styles" Target="styles.xml"/></Relationships>')),
    ])
    result = unsupported(parts(source))
    assert "ambiguous_opc_part_identity" in codes(result)
    assert not result["complete"]


@pytest.mark.parametrize("content_type", [WORD + "styles+xml", WORD + "settings+xml"])
def test_case_equivalent_override_identity_refuses_without_user_stores(content_type):
    source = package(document('<w:p><w:r><w:t>Fictional ordinary text.</w:t></w:r></w:p>'), [
        ("word/styles.xml", WORD + "styles+xml", f'<w:styles xmlns:w="{W_NS}"/>'.encode()),
    ])
    p = parts(source)
    set_xml(p, "[Content_Types].xml", lambda root: etree.SubElement(
        root, q(CT, "Override"), PartName="/word/STYLES.xml", ContentType=content_type))
    result = unsupported(p)
    assert "ambiguous_opc_override_identity" in codes(result)
    assert not result["complete"]


def test_singleton_uppercase_style_name_with_exact_reference_remains_supported():
    source = package(document('<w:p><w:r><w:t>Fictional ordinary text.</w:t></w:r></w:p>'), [
        ("word/STYLES.xml", WORD + "styles+xml", f'<w:styles xmlns:w="{W_NS}"/>'.encode()),
        ("word/_rels/document.xml.rels", RELS_CT, xml(f'<Relationships xmlns="{REL}"><Relationship Id="rIdStyle" Type="{ODREL}styles" Target="STYLES.xml"/></Relationships>')),
    ])
    result = clean_copy.prepare_clean_copy(source, expected_source_sha256=hashlib.sha256(source).hexdigest())
    assert result.inventory_before["clean_eligible"]
    assert result.payload == source
