# SPDX-License-Identifier: Apache-2.0
"""Whole-package clean gating; every fixture is fictional and byte-local."""

import copy
import hashlib
import io
import zipfile

import pytest
from lxml import etree

from veqtor_docx._ooxml import DocxError, ResourceLimitError, W_NS, w
from veqtor_docx import revision_inventory as inventory

CT = "http://schemas.openxmlformats.org/package/2006/content-types"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
ODREL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml."


def xml(content):
    return etree.fromstring(content.encode())


def document(content="<w:p><w:r><w:t>Fictional clause.</w:t></w:r></w:p>"):
    return xml(f'<w:document xmlns:w="{W_NS}"><w:body>{content}</w:body></w:document>')


def package(root=None, extras=(), mutate_types=None, mutate_rels=None):
    types = xml(f'<Types xmlns="{CT}"><Default Extension="rels" '
                'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                f'<Override PartName="/word/document.xml" ContentType="{MAIN}"/></Types>')
    rels = xml(f'<Relationships xmlns="{REL}"><Relationship Id="rId1" '
               f'Type="{ODREL}officeDocument" Target="word/document.xml"/></Relationships>')
    parts = {"word/document.xml": etree.tostring(root if root is not None else document())}
    for name, content_type, data in extras:
        parts[name] = etree.tostring(data) if isinstance(data, etree._Element) else data
        if content_type:
            etree.SubElement(types, f"{{{CT}}}Override", PartName="/" + name, ContentType=content_type)
    if mutate_types:
        mutate_types(types)
    if mutate_rels:
        mutate_rels(rels)
    parts["[Content_Types].xml"] = etree.tostring(types)
    parts["_rels/.rels"] = etree.tostring(rels)
    return archive(parts)


def archive(parts):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
        for name, data in parts.items():
            output.writestr(name, data)
    return buffer.getvalue()


def codes(result):
    return {row["code"] for row in result["blockers"]}


def test_complete_unchanged_package_has_no_revisions_and_exact_digest():
    payload = package()
    before = bytes(payload)
    result = inventory.inventory_package_revisions(payload)
    assert result == {
        "schema_version": "package_revisions.v1", "source_sha256": hashlib.sha256(payload).hexdigest(),
        "parts": [
            {"part_name": "[Content_Types].xml", "content_type": "application/vnd.openxmlformats-package.content-types+xml", "inspection": "xml"},
            {"part_name": "_rels/.rels", "content_type": "application/vnd.openxmlformats-package.relationships+xml", "inspection": "xml"},
            {"part_name": "word/document.xml", "content_type": MAIN, "inspection": "xml"},
        ], "revisions": [], "blockers": [], "complete": True, "clean_eligible": True,
    }
    assert payload == before


@pytest.mark.parametrize("value", [None, "not bytes", bytearray(b"not immutable"), {}])
def test_input_must_be_immutable_snapshot_bytes(value):
    with pytest.raises(DocxError, match="immutable DOCX bytes"):
        inventory.inventory_package_revisions(value)


def test_standard_generated_template_is_covered(demo_dir):
    source = demo_dir / "round-1-outgoing-draft.docx"
    before = source.read_bytes()
    result = inventory.inventory_package_revisions(before)
    assert result["clean_eligible"], result
    assert len(result["parts"]) == 8
    assert result["revisions"] == []
    assert source.read_bytes() == before


def test_interleaved_numbering_instances_are_not_silently_called_clean():
    root = xml(f'<w:numbering xmlns:w="{W_NS}"><w:abstractNum w:abstractNumId="1"/>'
               '<w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>'
               '<w:abstractNum w:abstractNumId="2"/><w:num w:numId="2">'
               '<w:abstractNumId w:val="2"/></w:num></w:numbering>')
    result = inventory.inventory_package_revisions(package(extras=[
        ("word/numbering.xml", WORD + "numbering+xml", root)]))
    assert "invalid_numbering_order" in codes(result)
    assert not result["complete"]
    assert not result["clean_eligible"]


@pytest.mark.parametrize("namespace", [
    "http://schemas.openxmlformats.org/drawingml/2006/main",
    "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes",
])
def test_arbitrary_auxiliary_xml_cannot_borrow_standard_namespace(namespace):
    root = xml(f'<x:futureRevisionContainer xmlns:x="{namespace}">UEsDBA==</x:futureRevisionContainer>')
    result = inventory.inventory_package_revisions(package(extras=[
        ("customXml/future.xml", "application/xml", root)]))
    assert "unsupported_auxiliary_xml" in codes(result)
    assert "unknown_standard_xml_markup" in codes(result)
    assert not result["complete"]
    assert not result["clean_eligible"]


@pytest.mark.parametrize("kind", sorted(inventory._ACCEPTABLE_KINDS))
def test_supported_location_is_type_fact_not_shape_promise(kind):
    # Deliberately malformed decision shape: inventory promises no applicability.
    root = document()
    etree.SubElement(root.find(w("body")), w(kind), {w("id"): "9"})
    result = inventory.inventory_package_revisions(package(root))
    assert result["clean_eligible"]
    assert result["revisions"] == [{"part_name": "word/document.xml", "element_path": [0, 1],
                                   "kind": kind, "supported_location": True}]


@pytest.mark.parametrize("part_name,content_type,root_name", [
    ("word/header1.xml", WORD + "header+xml", "hdr"),
    ("word/footer1.xml", WORD + "footer+xml", "ftr"),
    ("word/footnotes.xml", WORD + "footnotes+xml", "footnotes"),
    ("word/endnotes.xml", WORD + "endnotes+xml", "endnotes"),
    ("word/comments.xml", WORD + "comments+xml", "comments"),
    ("word/styles.xml", WORD + "styles+xml", "styles"),
    ("word/numbering.xml", WORD + "numbering+xml", "numbering"),
    ("orphan/not-xml.bin", "application/xml", "p"),
    ("orphan/not-xml.dat", "application/x-fictional+xml", "p"),
])
def test_revisions_are_found_in_every_part_even_orphan_non_xml_names(part_name, content_type, root_name):
    root = xml(f'<w:{root_name} xmlns:w="{W_NS}"><w:ins w:id="3"/></w:{root_name}>')
    result = inventory.inventory_package_revisions(package(extras=[(part_name, content_type, root)]))
    assert not result["clean_eligible"]
    assert result["revisions"] == [{"part_name": part_name, "element_path": [0],
                                   "kind": "ins", "supported_location": False}]
    assert "unsupported_revision" in codes(result)


@pytest.mark.parametrize("kind", sorted(inventory._REVISION_NAMES - inventory._ACCEPTABLE_KINDS))
def test_all_known_unsupported_revision_kinds_are_inventory_blockers(kind):
    result = inventory.inventory_package_revisions(package(document(f'<w:p><w:{kind}/></w:p>')))
    assert result["complete"] is True
    assert not result["clean_eligible"]
    assert result["revisions"][0]["supported_location"] is False
    assert result["revisions"][0]["kind"] == kind
    assert "unsupported_revision" in codes(result)


def test_deletion_cannot_hide_nested_unsupported_revision():
    result = inventory.inventory_package_revisions(package(document('<w:p><w:del><w:moveFrom/></w:del></w:p>')))
    assert [row["kind"] for row in result["revisions"]] == ["del", "moveFrom"]
    assert result["clean_eligible"] is False


@pytest.mark.parametrize("tag", ["delText", "delInstrText"])
def test_stray_deleted_atoms_are_not_a_revision_free_clean_package(tag):
    result = inventory.inventory_package_revisions(package(document(f"<w:p><w:r><w:{tag}>Fictional deleted data.</w:{tag}></w:r></w:p>")))
    assert result["revisions"] == []
    assert "stray_revision_atom" in codes(result)
    assert result["clean_eligible"] is False


@pytest.mark.parametrize("tag", ["delText", "delInstrText"])
def test_deleted_atom_with_declared_deletion_is_inventoried_for_shape_validation(tag):
    result = inventory.inventory_package_revisions(package(document(f"<w:p><w:del><w:r><w:{tag}>Fictional deleted data.</w:{tag}></w:r></w:del></w:p>")))
    assert "stray_revision_atom" not in codes(result)
    assert result["revisions"][0]["kind"] == "del"


@pytest.mark.parametrize("content", [
    '<w:p><w:futureRevision/></w:p>',
    '<w:p><x:ins xmlns:x="urn:fictional:future-word"/></w:p>',
    '<w:p><x:futureRevision xmlns:x="urn:fictional:future-word"/></w:p>',
    '<w:p><w:r xmlns:x="urn:fictional:future-word" x:unknown="1"/></w:p>',
    '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"><mc:Fallback><w:p/></mc:Fallback></mc:AlternateContent>',
])
def test_unknown_markup_is_not_called_complete_or_clean(content):
    result = inventory.inventory_package_revisions(package(document(content)))
    assert result["complete"] is False
    assert result["clean_eligible"] is False
    assert codes(result) & {"unknown_xml_namespace", "unknown_wordprocessing_markup", "unknown_attribute_namespace"}


def test_benign_word_ids_do_not_mean_extension_elements_are_supported():
    root = document()
    root[0][0].set("{" + inventory._W14 + "}paraId", "12345678")
    assert inventory.inventory_package_revisions(package(root))["clean_eligible"]
    etree.SubElement(root[0][0], "{" + inventory._W14 + "}futureRevision")
    assert not inventory.inventory_package_revisions(package(root))["clean_eligible"]


@pytest.mark.parametrize("ns,root_name,item_name,content_type,attrs", [
    (inventory._W15, "commentsEx", "commentEx", WORD + "commentsExtended+xml", {"paraId": "12345678", "done": "0"}),
    (inventory._W16CID, "commentsIds", "commentId", "application/vnd.ms-word.commentsIds+xml", {"paraId": "12345678", "durableId": "1"}),
    (inventory._W16CEX, "commentsExtensible", "commentExtensible", "application/vnd.ms-word.commentsExtensible+xml", {"durableId": "1", "dateUtc": "2026-01-01T00:00:00Z"}),
])
def test_known_comment_extension_metadata_can_remain_unchanged(ns, root_name, item_name, content_type, attrs):
    root = etree.Element("{" + ns + "}" + root_name, nsmap={"x": ns})
    etree.SubElement(root, "{" + ns + "}" + item_name, {"{" + ns + "}" + k: v for k, v in attrs.items()})
    payload = package(extras=[("word/fictional-comments.xml", content_type, root)])
    result = inventory.inventory_package_revisions(payload)
    assert result["clean_eligible"], result
    etree.SubElement(root, "{" + ns + "}unknown")
    assert not inventory.inventory_package_revisions(package(extras=[("word/fictional-comments.xml", content_type, root)]))["clean_eligible"]


def test_native_word_empty_bibliography_store_is_closed_and_preserved():
    biblio = xml(f'<b:Sources xmlns:b="{inventory._BIBLIOGRAPHY}" SelectedStyle="/Fictional.XSL" StyleName="Fictional" Version="1"/>')
    properties = xml(f'<cx:datastoreItem xmlns:cx="{inventory._CUSTOM_XML}" cx:itemID="{{00000000-0000-0000-0000-000000000001}}">'
                     f'<cx:schemaRefs><cx:schemaRef cx:uri="{inventory._BIBLIOGRAPHY}"/></cx:schemaRefs></cx:datastoreItem>')
    extras = [("customXml/item.xml", "application/xml", biblio),
              ("customXml/itemProps.xml", "application/vnd.openxmlformats-officedocument.customXmlProperties+xml", properties)]
    payload = package(extras=extras)
    assert inventory.inventory_package_revisions(payload)["clean_eligible"]
    etree.SubElement(biblio, "{" + inventory._BIBLIOGRAPHY + "}Sources")
    assert "unsupported_auxiliary_xml" in codes(inventory.inventory_package_revisions(package(extras=extras)))


@pytest.mark.parametrize("mutation", [
    lambda root: root.set("unknown", "future"),
    lambda root: etree.SubElement(root, "{" + inventory._CUSTOM_XML + "}schemaRefs"),
    lambda root: setattr(root, "text", "unexpected data"),
])
def test_custom_xml_properties_unknown_schema_does_not_enter_safe_coverage(mutation):
    root = xml(f'<cx:datastoreItem xmlns:cx="{inventory._CUSTOM_XML}" cx:itemID="fictional"><cx:schemaRefs/></cx:datastoreItem>')
    mutation(root)
    result = inventory.inventory_package_revisions(package(extras=[("customXml/itemProps.xml", "application/vnd.openxmlformats-officedocument.customXmlProperties+xml", root)]))
    assert "unsupported_auxiliary_xml" in codes(result)
    assert not result["complete"]


@pytest.mark.parametrize("namespace,tag,attribute", [
    (inventory._W14, "docId", "val"), (inventory._W14, "defaultImageDpi", "val"),
    (inventory._W15, "docId", "val"),
])
def test_known_word_save_settings_are_preserved_without_revision_inference(namespace, tag, attribute):
    settings = etree.Element(w("settings"), nsmap={"w": W_NS, "x": namespace})
    etree.SubElement(settings, "{" + namespace + "}" + tag, {"{" + namespace + "}" + attribute: "300"})
    result = inventory.inventory_package_revisions(package(extras=[("word/settings.xml", WORD + "settings+xml", settings)]))
    assert result["clean_eligible"], result


@pytest.mark.parametrize("name,content_type,data", [
    ("word/embeddings/object.bin", "application/vnd.openxmlformats-officedocument.oleObject", b"opaque fictional object"),
    ("word/media/spoof.png", "image/png", b"PK\x03\x04fictional office package"),
    ("customXml/uninspected.bin", "application/octet-stream", b"opaque"),
    ("word/media/unknown.svg", "image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
    ("opaque.unknown", None, b"opaque"),
])
def test_opaque_unknown_or_spoofed_parts_block_clean(name, content_type, data):
    result = inventory.inventory_package_revisions(package(extras=[(name, content_type, data)]))
    assert result["complete"] is False
    assert result["clean_eligible"] is False


@pytest.mark.parametrize("content_type,signature", [
    ("image/png", b"\x89PNG\r\n\x1a\n"), ("image/jpeg", b"\xff\xd8\xff"),
    ("image/gif", b"GIF89a"), ("image/tiff", b"II*\x00"), ("image/bmp", b"BM"),
])
def test_known_raster_containers_are_explicitly_accounted_for(content_type, signature):
    result = inventory.inventory_package_revisions(package(extras=[("word/media/image.bin", content_type, signature)]))
    assert result["clean_eligible"]
    assert next(row for row in result["parts"] if row["part_name"] == "word/media/image.bin")["inspection"] == "recognized_raster"


@pytest.mark.parametrize("tag", ["altChunk", "object", "subDoc"])
def test_reference_to_uninspectable_content_blocks_even_without_member(tag):
    result = inventory.inventory_package_revisions(package(document(f"<w:{tag}/>")))
    assert "opaque_content_reference" in codes(result)
    assert not result["complete"]


def test_opaque_custom_property_value_is_not_an_inspected_office_container():
    root = xml('<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
               f'xmlns:vt="{inventory._VTYPES}"><property pid="2" name="Fictional">'
               '<vt:blob>UEsDBA==</vt:blob></property></Properties>')
    result = inventory.inventory_package_revisions(package(extras=[("docProps/custom.xml", "application/vnd.openxmlformats-officedocument.custom-properties+xml", root)]))
    assert "opaque_content_reference" in codes(result)
    assert not result["complete"]


@pytest.mark.parametrize("kind,target,mode,expected", [
    (ODREL + "hyperlink", "https://example.invalid/fictional", "External", None),
    (ODREL + "aFChunk", "https://example.invalid/source.docx", "External", "unsupported_relationship"),
    (ODREL + "image", "https://example.invalid/image.png", "External", "unsupported_external_relationship"),
    (ODREL + "image", "missing.png", "Internal", "missing_relationship_target"),
    (ODREL + "image", "../../escape", "Internal", "missing_relationship_target"),
    (ODREL + "image", "http://[invalid", "Internal", "unsupported_relationship"),
])
def test_relationship_dependencies_are_checked(kind, target, mode, expected):
    rels = xml(f'<Relationships xmlns="{REL}"/>')
    etree.SubElement(rels, f"{{{REL}}}Relationship", Id="rIdFictional", Type=kind, Target=target, TargetMode=mode)
    result = inventory.inventory_package_revisions(package(extras=[("word/_rels/document.xml.rels", "application/vnd.openxmlformats-package.relationships+xml", rels)]))
    if expected is None:
        assert result["clean_eligible"], result
    else:
        assert expected in codes(result)
        assert not result["complete"]


def test_content_type_association_not_filename_drives_xml_inspection():
    root = xml(f'<w:hdr xmlns:w="{W_NS}"><w:ins/></w:hdr>')
    result = inventory.inventory_package_revisions(package(extras=[("odd.body", WORD + "header+xml", root)]))
    assert result["revisions"][0]["part_name"] == "odd.body"
    assert not result["clean_eligible"]


def test_relationship_content_type_with_nonstandard_name_still_checks_duplicates():
    rels = xml(f'<Relationships xmlns="{REL}"><Relationship Id="same" '
               f'Type="{ODREL}hyperlink" Target="https://example.invalid/one" TargetMode="External"/>'
               f'<Relationship Id="same" Type="{ODREL}hyperlink" '
               'Target="https://example.invalid/two" TargetMode="External"/></Relationships>')
    result = inventory.inventory_package_revisions(package(extras=[("fictional.dat", "application/vnd.openxmlformats-package.relationships+xml", rels)]))
    assert "unsupported_relationship" in codes(result)
    assert not result["complete"]


@pytest.mark.parametrize("content_type,root", [
    (WORD + "header+xml", "<wrong/>"),
    ("application/vnd.openxmlformats-package.relationships+xml", f'<Wrong xmlns="{REL}"/>'),
])
def test_declared_xml_schema_cannot_be_replaced_by_different_root(content_type, root):
    result = inventory.inventory_package_revisions(package(extras=[("fictional.dat", content_type, xml(root))]))
    assert "part_content_type_mismatch" in codes(result)
    assert not result["complete"]


def test_main_document_content_type_cannot_be_demoted_to_generic_xml():
    result = inventory.inventory_package_revisions(package(mutate_types=lambda root: root[-1].set("ContentType", "application/xml")))
    assert "unsupported_main_content_type" in codes(result)


@pytest.mark.parametrize("mutation", [
    lambda root: root.clear(),
    lambda root: root[0].set("Target", "docProps/other.xml"),
    lambda root: root.append(copy.deepcopy(root[0])),
])
def test_package_root_has_exactly_one_canonical_main_relationship(mutation):
    result = inventory.inventory_package_revisions(package(mutate_rels=mutation))
    assert "unsupported_main_relationship" in codes(result)
    assert not result["complete"]


def test_duplicate_content_type_declarations_have_controlled_failure():
    with pytest.raises(DocxError):
        inventory.inventory_package_revisions(package(mutate_types=lambda root: root.append(copy.deepcopy(root[0]))))


def test_orphan_xml_missing_content_type_is_still_parsed():
    root = xml(f'<w:hdr xmlns:w="{W_NS}"><w:ins/></w:hdr>')
    result = inventory.inventory_package_revisions(package(extras=[("orphan.xml", None, root)],
        mutate_types=lambda t: t.remove(t[1])))
    assert "missing_part_content_type" in codes(result)
    assert result["revisions"][0]["part_name"] == "orphan.xml"


@pytest.mark.parametrize("content_type,data", [("application/xml", b"<malformed>"),
    ("application/xml", b'<!DOCTYPE a [<!ENTITY x "fictional">]><a>&x;</a>')])
def test_invalid_or_doctype_xml_has_controlled_failure(content_type, data):
    with pytest.raises(DocxError):
        inventory.inventory_package_revisions(package(extras=[("orphan.dat", content_type, data)]))


@pytest.mark.parametrize("constant,value,content", [
    ("MAX_INVENTORY_REVISIONS", 1, "<w:p><w:ins/><w:ins/></w:p>"),
    ("MAX_INVENTORY_FINDINGS", 1, "<w:p><w:future/><w:future/></w:p>"),
    ("MAX_INVENTORY_DEPTH", 1, "<w:p><w:r/></w:p>"),
    ("MAX_INVENTORY_XML_NODES", 1, "<w:p/>"),
    ("MAX_INVENTORY_XML_BYTES", 1, "<w:p/>"),
])
def test_aggregate_work_limits_fail_instead_of_returning_partial_clean(monkeypatch, constant, value, content):
    monkeypatch.setattr(inventory, constant, value)
    with pytest.raises(ResourceLimitError):
        inventory.inventory_package_revisions(package(document(content)))


def test_limit_boundary_is_inclusive_and_does_not_mutate_payload(monkeypatch):
    payload = package(document("<w:p><w:ins/><w:del/></w:p>"))
    monkeypatch.setattr(inventory, "MAX_INVENTORY_REVISIONS", 2)
    before = copy.deepcopy(payload)
    result = inventory.inventory_package_revisions(payload)
    assert len(result["revisions"]) == 2
    assert result["clean_eligible"]
    assert payload == before
