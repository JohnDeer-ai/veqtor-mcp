# SPDX-License-Identifier: Apache-2.0
"""Fictional document-envelope data must never disappear behind a clean gate."""
import hashlib
import io
import zipfile

import pytest
from lxml import etree

from veqtor_docx import ApplyError
from veqtor_docx._ooxml import W_NS, w
from veqtor_docx.clean_copy import prepare_clean_copy
from veqtor_docx.revision_inventory import inventory_package_revisions
from test_revision_inventory import ODREL, REL, WORD, archive, document, package


def source():
    links = etree.Element("{" + REL + "}Relationships", nsmap={None: REL})
    etree.SubElement(links, "{" + REL + "}Relationship", Id="rIdSettings",
                     Type=ODREL + "settings", Target="settings.xml")
    return package(document('<w:p><w:ins w:id="1" w:author="Fictional Counsel">'
                            '<w:r><w:t>Fictional clause.</w:t></w:r></w:ins></w:p>'),
                   extras=[("word/settings.xml", WORD + "settings+xml",
                            etree.Element(w("settings"), nsmap={"w": W_NS})),
                           ("word/_rels/document.xml.rels",
                            "application/vnd.openxmlformats-package.relationships+xml", links)])


def replace_part(payload, name, mutate):
    with zipfile.ZipFile(io.BytesIO(payload)) as old:
        parts = {member: old.read(member) for member in old.namelist()}
    parts[name] = mutate(parts[name])
    return archive(parts)


def assert_refused(payload, part):
    before = bytes(payload)
    result = inventory_package_revisions(payload)
    assert not result["complete"]
    assert not result["clean_eligible"]
    assert {"code": "unsupported_auxiliary_xml", "part_name": part,
            "element_path": []} in result["blockers"]
    with pytest.raises(ApplyError) as caught:
        prepare_clean_copy(payload, expected_source_sha256=hashlib.sha256(payload).hexdigest())
    assert caught.value.code == "clean_copy_unsupported"
    assert payload == before


@pytest.mark.parametrize("part", ["word/document.xml", "word/settings.xml",
                                  "[Content_Types].xml", "_rels/.rels"])
@pytest.mark.parametrize("position", ["before", "after", "inside"])
def test_processing_instructions_are_not_covered_by_any_xml_part(part, position):
    instruction = b'<?fictional-office href="unresolved.xml"?>'

    def insert(data):
        if position == "before":
            return instruction + data
        if position == "after":
            return data + instruction
        root = etree.fromstring(data)
        root.insert(0, etree.ProcessingInstruction("fictional-office", 'href="unresolved.xml"'))
        return etree.tostring(root)

    assert_refused(replace_part(source(), part, insert), part)


@pytest.mark.parametrize("position", ["before", "after", "both"])
def test_main_document_comments_outside_root_cannot_be_silently_discarded(position):
    comment = b'<!--Fictional retained provenance-->'
    payload = replace_part(source(), "word/document.xml",
                           lambda data: (comment if position in {"before", "both"} else b"")
                           + data + (comment if position in {"after", "both"} else b""))
    assert_refused(payload, "word/document.xml")


@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16"])
def test_instruction_detection_is_not_an_ascii_byte_search(encoding):
    def insert(data):
        return ('<?xml version="1.0" encoding="' + encoding + '"?>'
                '<?fictional-office audit="retained"?>' + data.decode()).encode(encoding)

    assert_refused(replace_part(source(), "word/document.xml", insert), "word/document.xml")


@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16"])
def test_xml_declaration_and_internal_comments_remain_supported(encoding):
    def insert(data):
        root = etree.fromstring(data)
        root.insert(0, etree.Comment("Fictional retained provenance"))
        return etree.tostring(root, encoding=encoding, xml_declaration=True, standalone=True)

    payload = replace_part(source(), "word/document.xml", insert)
    assert inventory_package_revisions(payload)["clean_eligible"]
    result = prepare_clean_copy(payload, expected_source_sha256=hashlib.sha256(payload).hexdigest())
    assert result.inventory_after["clean_eligible"]
    assert result.accepted_revision_count == 1
    with zipfile.ZipFile(io.BytesIO(result.payload)) as output:
        root = etree.fromstring(output.read("word/document.xml"))
    assert [node.text for node in root if isinstance(node, etree._Comment)] == ["Fictional retained provenance"]
