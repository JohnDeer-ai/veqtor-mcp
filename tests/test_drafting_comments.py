# SPDX-License-Identifier: Apache-2.0
"""Comment drafting keeps text/formatting and other reviewers' comments intact."""

import pytest
from lxml import etree

from veqtor_docx._drafting_comments import (
    COMMENT_MUTABLE_PARTS,
    add_comment,
    comment_mutable_parts,
    delete_comment,
    read_comments,
    update_comment,
)
from veqtor_docx._ooxml import W_NS, current_text_atom, parse_xml, w
from veqtor_docx.apply import ApplyError

AUTHOR = "Drafting Author"
OTHER = "Other Reviewer"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
W16CEX = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
COMMENT_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
MODERN_RELATIONSHIPS = {
    "commentsExtended": "http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
    "commentsIds": "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds",
    "commentsExtensible": "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible",
}


def _package(*texts):
    document = etree.Element(w("document"), nsmap={"w": W_NS})
    body = etree.SubElement(document, w("body"))
    paragraphs = []
    for text in texts or ("Before selected words after.",):
        paragraph = etree.SubElement(body, w("p"))
        _run(paragraph, text)
        paragraphs.append(paragraph)
    parts = {
        "[Content_Types].xml": f'<Types xmlns="{TYPES}"><Default Extension="xml" ContentType="application/xml"/></Types>'.encode(),
        "word/document.xml": etree.tostring(document),
        "word/styles.xml": f'<w:styles xmlns:w="{W_NS}"/>'.encode(),
    }
    return parts, document, paragraphs


def _run(parent, text, *, bold=False, italic=False):
    run = etree.SubElement(parent, w("r"))
    if bold or italic:
        props = etree.SubElement(run, w("rPr"))
        if bold:
            etree.SubElement(props, w("b"))
        if italic:
            etree.SubElement(props, w("i"))
    etree.SubElement(run, w("t")).text = text
    return run


def _text(paragraph):
    return "".join(value for node in paragraph.iter() if (value := current_text_atom(node, boundary=paragraph)) is not None)


def _add(parts, document, paragraph, quote="selected words", text="Check wording", author=AUTHOR):
    return add_comment(parts, document, paragraph, quote, text, author, "DA")


def _comments(parts):
    return parse_xml(parts["word/comments.xml"])


def _with_modern_metadata(parts, comment_id="0", paraid="0000000A", *, reply=False):
    comments = _comments(parts)
    comment = next(node for node in comments if node.get(w("id")) == comment_id)
    comment[0].set(f"{{{W14}}}paraId", paraid)
    comment.set(w("date"), "2026-01-01T00:00:00Z")
    parts["word/comments.xml"] = etree.tostring(comments)
    parent = f' w15:paraIdParent="{paraid}"' if reply else ""
    parts["word/commentsExtended.xml"] = (
        f'<w15:commentsEx xmlns:w15="{W15}"><w15:commentEx w15:paraId="{paraid}" w15:done="0"/>'
        f'<w15:commentEx w15:paraId="0000000B"{parent} w15:done="1"/></w15:commentsEx>'
    ).encode()
    parts["word/commentsIds.xml"] = (
        f'<w16cid:commentsIds xmlns:w16cid="{W16CID}"><w16cid:commentId w16cid:paraId="{paraid}" w16cid:durableId="00000011"/>'
        '<w16cid:commentId w16cid:paraId="0000000B" w16cid:durableId="00000012"/></w16cid:commentsIds>'
    ).encode()
    parts["word/commentsExtensible.xml"] = (
        f'<w16cex:commentsExtensible xmlns:w16cex="{W16CEX}"><w16cex:commentExtensible w16cex:durableId="00000011"/>'
        '<w16cex:commentExtensible w16cex:durableId="00000012"/></w16cex:commentsExtensible>'
    ).encode()
    rels = parse_xml(parts["word/_rels/document.xml.rels"])
    types = parse_xml(parts["[Content_Types].xml"])
    for name, relationship in MODERN_RELATIONSHIPS.items():
        etree.SubElement(rels, f"{{{RELS}}}Relationship", Id=f"rId{name}", Type=relationship, Target=f"{name}.xml")
        etree.SubElement(types, f"{{{TYPES}}}Override", PartName=f"/word/{name}.xml",
                         ContentType=f"application/vnd.openxmlformats-officedocument.wordprocessingml.{name}+xml")
    parts["word/_rels/document.xml.rels"] = etree.tostring(rels)
    parts["[Content_Types].xml"] = etree.tostring(types)


def test_add_read_update_delete_comment_and_package_metadata():
    parts, document, paragraphs = _package()
    before = _text(paragraphs[0])
    first = _add(parts, document, paragraphs[0])
    assert first == {"id": "0", "author": AUTHOR, "initials": "DA", "text": "Check wording", "paragraph_indexes": [0], "operation": "add_comment"}
    assert _text(paragraphs[0]) == before
    assert read_comments(parts, document) == [{key: value for key, value in first.items() if key != "operation"}]
    rels = parse_xml(parts["word/_rels/document.xml.rels"])
    assert len(rels) == 1
    assert rels[0].get("Type") == COMMENT_REL
    assert rels[0].get("Target") == "comments.xml"
    types = parse_xml(parts["[Content_Types].xml"])
    assert any(node.get("PartName") == "/word/comments.xml" for node in types)
    updated = update_comment(parts, document, "0", "Revised\nsecond line", AUTHOR)
    assert updated["text"] == "Revised\nsecond line"
    assert _text(paragraphs[0]) == before
    deleted = delete_comment(parts, document, "0", AUTHOR)
    assert deleted["text"] == "Revised\nsecond line"
    assert read_comments(parts, document) == []
    assert not list(document.iter(w("commentReference")))
    assert not list(document.iter(w("commentRangeStart")))
    assert _text(paragraphs[0]) == before


def test_new_comments_preserve_other_comments_and_reuse_relationships():
    parts, document, paragraphs = _package("First target", "Second target")
    _add(parts, document, paragraphs[0], "First", "Their comment", OTHER)
    old_comment = etree.tostring(_comments(parts)[0])
    metadata = {name: parts[name] for name in ("word/_rels/document.xml.rels", "[Content_Types].xml")}
    added = _add(parts, document, paragraphs[1], "Second", "Our comment")
    assert added["id"] == "1"
    assert added["paragraph_indexes"] == [1]
    assert etree.tostring(_comments(parts)[0]) == old_comment
    assert all(parts[name] == data for name, data in metadata.items())
    update_comment(parts, document, "1", "Our revised comment", AUTHOR)
    delete_comment(parts, document, "1", AUTHOR)
    assert etree.tostring(_comments(parts)[0]) == old_comment
    assert read_comments(parts, document)[0]["author"] == OTHER
    assert [node.get(w("id")) for node in document.iter(w("commentReference"))] == ["0"]


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_only_own_comments_may_be_changed(operation):
    parts, document, paragraphs = _package()
    _add(parts, document, paragraphs[0], author=OTHER)
    before_parts, before_doc = dict(parts), etree.tostring(document)
    with pytest.raises(ApplyError) as caught:
        if operation == "update":
            update_comment(parts, document, "0", "Changed", AUTHOR)
        else:
            delete_comment(parts, document, "0", AUTHOR)
    assert caught.value.code == "comment_author_mismatch"
    assert parts == before_parts
    assert etree.tostring(document) == before_doc


def test_comment_anchor_spans_formatted_runs_and_preserves_existing_markers():
    parts, document, paragraphs = _package("")
    paragraph = paragraphs[0]
    paragraph.clear()
    _run(paragraph, "Before select", bold=True)
    bookmark = etree.SubElement(paragraph, w("bookmarkStart"))
    bookmark.set(w("id"), "100")
    bookmark.set(w("name"), "Existing")
    _run(paragraph, "ed words after.", italic=True)
    end = etree.SubElement(paragraph, w("bookmarkEnd"))
    end.set(w("id"), "100")
    existing_marker = etree.tostring(bookmark)
    before = _text(paragraph)
    _add(parts, document, paragraph)
    assert _text(paragraph) == before
    assert etree.tostring(next(paragraph.iter(w("bookmarkStart")))) == existing_marker
    text_runs = [node for node in paragraph if node.tag == w("r") and node.find(w("t")) is not None]
    assert [node.find(w("t")).text for node in text_runs] == ["Before ", "select", "ed words", " after."]
    assert all(node.find(f'{w("rPr")}/{w("b")}') is not None for node in text_runs[:2])
    assert all(node.find(f'{w("rPr")}/{w("i")}') is not None for node in text_runs[2:])


def test_own_pending_insertion_can_receive_comment_without_changing_revision():
    parts, document, paragraphs = _package("")
    paragraph = paragraphs[0]
    paragraph.clear()
    insertion = etree.SubElement(paragraph, w("ins"))
    insertion.set(w("id"), "17")
    insertion.set(w("author"), AUTHOR)
    insertion.set(w("date"), "2026-10-16T00:00:00Z")
    _run(insertion, "selected words")
    attributes = dict(insertion.attrib)
    _add(parts, document, paragraph)
    assert dict(insertion.attrib) == attributes
    assert _text(paragraph) == "selected words"
    assert insertion.find(w("commentRangeStart")) is not None
    assert read_comments(parts, document)[0]["paragraph_indexes"] == [0]


@pytest.mark.parametrize("case", ["foreign_insertion", "crossed_container", "field", "repeated", "absent", "deleted_gap"])
def test_unsafe_quotes_refuse_without_mutation(case):
    parts, document, paragraphs = _package("")
    paragraph = paragraphs[0]
    paragraph.clear()
    quote = "selected words"
    expected = "unsupported_comment_structure"
    if case == "foreign_insertion":
        insertion = etree.SubElement(paragraph, w("ins"))
        insertion.set(w("author"), OTHER)
        _run(insertion, quote)
    elif case == "crossed_container":
        _run(paragraph, "selected ")
        insertion = etree.SubElement(paragraph, w("ins"))
        insertion.set(w("author"), AUTHOR)
        _run(insertion, "words")
    elif case == "field":
        field = etree.SubElement(paragraph, w("fldSimple"))
        _run(field, quote)
    elif case == "deleted_gap":
        _run(paragraph, "selected ")
        deletion = etree.SubElement(paragraph, w("del"))
        run = etree.SubElement(deletion, w("r"))
        etree.SubElement(run, w("delText")).text = "hidden"
        _run(paragraph, "words")
    elif case == "repeated":
        _run(paragraph, quote + " " + quote)
        expected = "ambiguous_comment_quote"
    else:
        _run(paragraph, "different")
        expected = "comment_quote_not_found"
    before_parts, before_doc = dict(parts), etree.tostring(document)
    with pytest.raises(ApplyError) as caught:
        _add(parts, document, paragraph)
    assert caught.value.code == expected
    assert parts == before_parts
    assert etree.tostring(document) == before_doc


def test_missing_relationship_is_repaired_and_orphan_ids_not_reused():
    parts, document, paragraphs = _package()
    marker = etree.SubElement(paragraphs[0], w("commentRangeStart"))
    marker.set(w("id"), "40")
    parts["word/_rels/document.xml.rels"] = f'<Relationships xmlns="{RELS}"><Relationship Id="rId1" Type="urn:other" Target="other.xml"/></Relationships>'.encode()
    result = _add(parts, document, paragraphs[0])
    assert result["id"] == "41"
    rels = parse_xml(parts["word/_rels/document.xml.rels"])
    assert len(rels) == 2
    assert rels[1].get("Id") == "rId2"


def test_comment_update_preserves_modern_identity_attributes_and_formatting():
    parts, document, paragraphs = _package()
    _add(parts, document, paragraphs[0])
    _with_modern_metadata(parts)
    comments = _comments(parts)
    paragraph = comments[0][0]
    properties = etree.Element(w("pPr"))
    etree.SubElement(properties, w("pStyle")).set(w("val"), "CommentText")
    paragraph.insert(0, properties)
    paragraph.find(w("r")).insert(0, etree.Element(w("rPr")))
    etree.SubElement(paragraph.find(f'{w("r")}/{w("rPr")}'), w("b"))
    parts["word/comments.xml"] = etree.tostring(comments)
    before_attrs = dict(comments[0].attrib)
    before_paragraph_attrs = dict(paragraph.attrib)
    modern = {name: data for name, data in parts.items() if name in COMMENT_MUTABLE_PARTS and name.startswith("word/comments") and name != "word/comments.xml"}
    doc_before = etree.tostring(document)
    update_comment(parts, document, "0", "Replacement", AUTHOR)
    after = _comments(parts)[0]
    assert dict(after.attrib) == before_attrs
    assert dict(after[0].attrib) == before_paragraph_attrs
    assert after[0].find(f'{w("pPr")}/{w("pStyle")}').get(w("val")) == "CommentText"
    assert after[0].find(f'{w("r")}/{w("rPr")}/{w("b")}') is not None
    assert all(parts[name] == data for name, data in modern.items())
    assert etree.tostring(document) == doc_before


def test_delete_modern_comment_cleans_only_its_identity_records():
    parts, document, paragraphs = _package()
    _add(parts, document, paragraphs[0])
    _with_modern_metadata(parts)
    delete_comment(parts, document, "0", AUTHOR)
    assert read_comments(parts, document) == []
    assert len(parse_xml(parts["word/commentsExtended.xml"])) == 1
    assert parse_xml(parts["word/commentsExtended.xml"])[0].get(f"{{{W15}}}paraId") == "0000000B"
    assert len(parse_xml(parts["word/commentsIds.xml"])) == 1
    assert len(parse_xml(parts["word/commentsExtensible.xml"])) == 1


def test_delete_thread_parent_refuses_and_preserves_all_parts():
    parts, document, paragraphs = _package()
    _add(parts, document, paragraphs[0])
    _with_modern_metadata(parts, reply=True)
    before_parts, before_doc = dict(parts), etree.tostring(document)
    with pytest.raises(ApplyError) as caught:
        delete_comment(parts, document, "0", AUTHOR)
    assert caught.value.code == "unsupported_comment_structure"
    assert parts == before_parts
    assert etree.tostring(document) == before_doc


@pytest.mark.parametrize("kind", ["multiparagraph", "hyperlink", "table", "tracked"])
def test_rich_comment_updates_fail_closed(kind):
    parts, document, paragraphs = _package()
    _add(parts, document, paragraphs[0])
    root = _comments(parts)
    if kind == "multiparagraph":
        etree.SubElement(root[0], w("p"))
    elif kind == "hyperlink":
        hyperlink = etree.SubElement(root[0][0], w("hyperlink"))
        _run(hyperlink, "link")
    elif kind == "table":
        etree.SubElement(root[0], w("tbl"))
    else:
        props = etree.SubElement(root[0][0][0], w("rPr"))
        etree.SubElement(props, w("rPrChange"))
    parts["word/comments.xml"] = etree.tostring(root)
    before = dict(parts)
    with pytest.raises(ApplyError) as caught:
        update_comment(parts, document, "0", "New", AUTHOR)
    assert caught.value.code == "unsupported_comment_structure"
    assert parts == before


def test_outside_body_comment_reference_prevents_delete():
    parts, document, paragraphs = _package()
    _add(parts, document, paragraphs[0])
    parts["word/header1.xml"] = f'<w:hdr xmlns:w="{W_NS}"><w:p><w:r><w:commentReference w:id="0"/></w:r></w:p></w:hdr>'.encode()
    before_parts, before_doc = dict(parts), etree.tostring(document)
    with pytest.raises(ApplyError) as caught:
        delete_comment(parts, document, "0", AUTHOR)
    assert caught.value.code == "unsupported_comment_structure"
    assert parts == before_parts
    assert etree.tostring(document) == before_doc


def test_nonstandard_comments_relationship_prevents_write():
    parts, document, paragraphs = _package()
    parts["word/_rels/document.xml.rels"] = f'<Relationships xmlns="{RELS}"><Relationship Id="rId1" Type="{COMMENT_REL}" Target="comments2.xml"/></Relationships>'.encode()
    before_parts, before_doc = dict(parts), etree.tostring(document)
    with pytest.raises(ApplyError):
        _add(parts, document, paragraphs[0])
    assert parts == before_parts
    assert etree.tostring(document) == before_doc


def test_read_unanchored_comments_and_missing_comment_error():
    parts, document, paragraphs = _package()
    _add(parts, document, paragraphs[0])
    parts["word/comments.xml"] = f'<w:comments xmlns:w="{W_NS}"><w:comment w:id="8" w:author="{OTHER}"><w:p><w:r><w:t>Unanchored</w:t></w:r></w:p></w:comment></w:comments>'.encode()
    assert read_comments(parts, document)[0]["paragraph_indexes"] == []
    with pytest.raises(ApplyError) as caught:
        update_comment(parts, document, "7", "Missing", AUTHOR)
    assert caught.value.code == "comment_not_found"


def test_new_comment_can_overlap_an_existing_range_without_changing_it():
    parts, document, paragraphs = _package("Before selected words after.")
    _add(parts, document, paragraphs[0], "selected", author=OTHER)
    existing = etree.tostring(_comments(parts)[0])
    _add(parts, document, paragraphs[0], "selected words", "Larger range")
    assert etree.tostring(_comments(parts)[0]) == existing
    assert _text(paragraphs[0]) == "Before selected words after."
    assert [node.get(w("id")) for node in paragraphs[0].iter(w("commentRangeStart"))] == ["0", "1"]
    delete_comment(parts, document, "1", AUTHOR)
    assert read_comments(parts, document)[0]["text"] == "Check wording"


def test_invalid_comment_input_does_not_mutate():
    parts, document, paragraphs = _package()
    for quote, text, author in [("", "Text", AUTHOR), ("selected", "", AUTHOR), ("selected", "bad\x00", AUTHOR), ("selected", "Text", "")]:
        before = dict(parts)
        with pytest.raises(ApplyError) as caught:
            _add(parts, document, paragraphs[0], quote, text, author)
        assert caught.value.code == "invalid_comment"
        assert parts == before


@pytest.mark.parametrize("operation", ["read", "update", "delete"])
@pytest.mark.parametrize("binding", ["foreign_target", "missing_relationship", "missing_content_type"])
def test_existing_comments_require_unambiguous_package_binding(operation, binding):
    parts, document, paragraphs = _package()
    _add(parts, document, paragraphs[0])
    if binding == "foreign_target":
        parts["word/_rels/document.xml.rels"] = f'<Relationships xmlns="{RELS}"><Relationship Id="rId1" Type="{COMMENT_REL}" Target="comments2.xml"/></Relationships>'.encode()
    elif binding == "missing_relationship":
        del parts["word/_rels/document.xml.rels"]
    else:
        types = parse_xml(parts["[Content_Types].xml"])
        for node in list(types):
            if node.get("PartName") == "/word/comments.xml":
                types.remove(node)
        parts["[Content_Types].xml"] = etree.tostring(types)
    before_parts, before_doc = dict(parts), etree.tostring(document)
    with pytest.raises(ApplyError) as caught:
        if operation == "read":
            read_comments(parts, document)
        elif operation == "update":
            update_comment(parts, document, "0", "Replacement", AUTHOR)
        else:
            delete_comment(parts, document, "0", AUTHOR)
    assert caught.value.code == "unsupported_comment_structure"
    assert parts == before_parts
    assert etree.tostring(document) == before_doc


@pytest.mark.parametrize("kind", ["shared_paraid", "shared_durableid", "missing_durableid"])
def test_delete_refuses_ambiguous_modern_identity_metadata(kind):
    parts, document, paragraphs = _package("selected words and other words")
    _add(parts, document, paragraphs[0])
    _add(parts, document, paragraphs[0], "other words", "Their comment", OTHER)
    _with_modern_metadata(parts)
    if kind == "shared_paraid":
        root = _comments(parts)
        root[1][0].set(f"{{{W14}}}paraId", "0000000A")
        parts["word/comments.xml"] = etree.tostring(root)
    else:
        root = parse_xml(parts["word/commentsIds.xml"])
        if kind == "shared_durableid":
            root[1].set(f"{{{W16CID}}}durableId", "00000011")
        else:
            del root[0].attrib[f"{{{W16CID}}}durableId"]
        parts["word/commentsIds.xml"] = etree.tostring(root)
    before_parts, before_doc = dict(parts), etree.tostring(document)
    with pytest.raises(ApplyError) as caught:
        delete_comment(parts, document, "0", AUTHOR)
    assert caught.value.code == "unsupported_comment_structure"
    assert parts == before_parts
    assert etree.tostring(document) == before_doc


def test_modern_hex_ids_compare_without_case_and_preserve_other_lexical_values():
    parts, document, paragraphs = _package()
    _add(parts, document, paragraphs[0])
    _with_modern_metadata(parts)
    comments = _comments(parts)
    comments[0][0].set(f"{{{W14}}}paraId", "0000000a")
    parts["word/comments.xml"] = etree.tostring(comments)
    identities = parse_xml(parts["word/commentsIds.xml"])
    identities[0].set(f"{{{W16CID}}}durableId", "A0000011")
    identities[1].set(f"{{{W16CID}}}durableId", "b0000012")
    parts["word/commentsIds.xml"] = etree.tostring(identities)
    extensible = parse_xml(parts["word/commentsExtensible.xml"])
    extensible[0].set(f"{{{W16CEX}}}durableId", "a0000011")
    extensible[1].set(f"{{{W16CEX}}}durableId", "B0000012")
    parts["word/commentsExtensible.xml"] = etree.tostring(extensible)
    delete_comment(parts, document, "0", AUTHOR)
    assert len(parse_xml(parts["word/commentsExtended.xml"])) == 1
    assert parse_xml(parts["word/commentsIds.xml"])[0].get(f"{{{W16CID}}}durableId") == "b0000012"
    assert parse_xml(parts["word/commentsExtensible.xml"])[0].get(f"{{{W16CEX}}}durableId") == "B0000012"


@pytest.mark.parametrize("kind", ["shared_case_paraid", "shared_case_durableid", "malformed_paraid", "malformed_durableid"])
def test_modern_hex_identity_ambiguity_and_malformed_values_refuse(kind):
    parts, document, paragraphs = _package("selected words and other words")
    _add(parts, document, paragraphs[0])
    _add(parts, document, paragraphs[0], "other words", "Their comment", OTHER)
    _with_modern_metadata(parts)
    if kind in ("shared_case_paraid", "malformed_paraid"):
        root = _comments(parts)
        if kind == "shared_case_paraid":
            root[1][0].set(f"{{{W14}}}paraId", "0000000a")
        else:
            root[0][0].set(f"{{{W14}}}paraId", "not-hex")
        parts["word/comments.xml"] = etree.tostring(root)
    else:
        root = parse_xml(parts["word/commentsIds.xml"])
        root[0].set(f"{{{W16CID}}}durableId", "A0000011")
        if kind == "shared_case_durableid":
            root[1].set(f"{{{W16CID}}}durableId", "a0000011")
        else:
            root[0].set(f"{{{W16CID}}}durableId", "invalid")
        parts["word/commentsIds.xml"] = etree.tostring(root)
    before_parts, before_doc = dict(parts), etree.tostring(document)
    with pytest.raises(ApplyError) as caught:
        delete_comment(parts, document, "0", AUTHOR)
    assert caught.value.code == "unsupported_comment_structure"
    assert parts == before_parts
    assert etree.tostring(document) == before_doc


def _rename_modern_parts(parts):
    """Use legal relative, absolute and escaped OPC targets, outside word/ too."""
    names = {
        "commentsExtended": ("word/threadMetadata.xml", "threadMetadata.xml"),
        "commentsIds": ("metadata/identity records.xml", "/metadata/identity%20records.xml"),
        "commentsExtensible": ("metadata/details.xml", "../metadata/details.xml"),
    }
    rels = parse_xml(parts["word/_rels/document.xml.rels"])
    types = parse_xml(parts["[Content_Types].xml"])
    for name, (part_name, target) in names.items():
        parts[part_name] = parts.pop(f"word/{name}.xml")
        next(node for node in rels if node.get("Type") == MODERN_RELATIONSHIPS[name]).set("Target", target)
        next(node for node in types if node.get("PartName") == f"/word/{name}.xml").set("PartName", f"/{part_name}")
    parts["word/_rels/document.xml.rels"] = etree.tostring(rels)
    parts["[Content_Types].xml"] = etree.tostring(types)
    return {name: part_name for name, (part_name, _) in names.items()}


@pytest.mark.parametrize("target_comment", ["0", "1"])
def test_renamed_thread_metadata_protects_parent_and_reply(target_comment):
    parts, document, paragraphs = _package("selected words and other words")
    _add(parts, document, paragraphs[0], author=AUTHOR if target_comment == "0" else OTHER)
    _add(parts, document, paragraphs[0], "other words", "Reply", AUTHOR if target_comment == "1" else OTHER)
    _with_modern_metadata(parts, reply=True)
    comments = _comments(parts)
    comments[1][0].set(f"{{{W14}}}paraId", "0000000B")
    parts["word/comments.xml"] = etree.tostring(comments)
    _rename_modern_parts(parts)
    before_parts, before_doc = dict(parts), etree.tostring(document)
    with pytest.raises(ApplyError) as caught:
        delete_comment(parts, document, target_comment, AUTHOR)
    assert caught.value.code == "unsupported_comment_structure"
    assert parts == before_parts
    assert etree.tostring(document) == before_doc


def test_delete_unthreaded_comment_updates_related_metadata_at_nonstandard_paths():
    parts, document, paragraphs = _package("selected words and other words")
    _add(parts, document, paragraphs[0])
    _add(parts, document, paragraphs[0], "other words", "Their comment", OTHER)
    _with_modern_metadata(parts)
    comments = _comments(parts)
    comments[1][0].set(f"{{{W14}}}paraId", "0000000B")
    parts["word/comments.xml"] = etree.tostring(comments)
    names = _rename_modern_parts(parts)
    foreign_comment = etree.tostring(_comments(parts)[1])
    foreign_metadata = {name: etree.tostring(parse_xml(parts[name])[1]) for name in names.values()}
    bindings = {name: parts[name] for name in ("word/_rels/document.xml.rels", "[Content_Types].xml")}
    assert set(names.values()) <= comment_mutable_parts(parts)
    delete_comment(parts, document, "0", AUTHOR)
    assert etree.tostring(_comments(parts)[0]) == foreign_comment
    for name in names.values():
        root = parse_xml(parts[name])
        assert len(root) == 1
        assert etree.tostring(root[0]) == foreign_metadata[name]
    assert all(parts[name] == data for name, data in bindings.items())
    assert [node.get(w("id")) for node in document.iter(w("commentReference"))] == ["1"]


@pytest.mark.parametrize("extension", list(MODERN_RELATIONSHIPS))
@pytest.mark.parametrize("damage", [
    "empty_target", "external_mode", "external_uri", "bad_escape", "query", "fragment",
    "traversal", "missing_part", "duplicate_type", "duplicate_id", "wrong_part", "missing_relationship",
])
def test_modern_metadata_relationship_errors_refuse_without_mutation(extension, damage):
    parts, document, paragraphs = _package()
    _add(parts, document, paragraphs[0])
    _with_modern_metadata(parts)
    names = _rename_modern_parts(parts)
    rels = parse_xml(parts["word/_rels/document.xml.rels"])
    rel = next(node for node in rels if node.get("Type") == MODERN_RELATIONSHIPS[extension])
    bad_targets = {
        "empty_target": "", "external_uri": "https://example.test/comments.xml",
        "bad_escape": "bad%ZZ.xml", "query": "threadMetadata.xml?q=1",
        "fragment": "threadMetadata.xml#id", "traversal": "../../outside.xml",
        "missing_part": "missing.xml", "wrong_part": "styles.xml",
    }
    if damage in bad_targets:
        rel.set("Target", bad_targets[damage])
    elif damage == "external_mode":
        rel.set("TargetMode", "External")
    elif damage == "duplicate_type":
        etree.SubElement(rels, f"{{{RELS}}}Relationship", Id="rIdDuplicate", Type=rel.get("Type"), Target=rel.get("Target"))
    elif damage == "duplicate_id":
        rel.set("Id", rels[0].get("Id"))
    else:
        rels.remove(rel)
        # A renamed root still prevents deletion even if its type declaration
        # is missing too; identifying metadata cannot depend on filename alone.
        types = parse_xml(parts["[Content_Types].xml"])
        types.remove(next(node for node in types if node.get("PartName") == f"/{names[extension]}"))
        parts["[Content_Types].xml"] = etree.tostring(types)
    parts["word/_rels/document.xml.rels"] = etree.tostring(rels)
    before_parts, before_doc = dict(parts), etree.tostring(document)
    with pytest.raises(ApplyError) as caught:
        delete_comment(parts, document, "0", AUTHOR)
    assert caught.value.code == "unsupported_comment_structure"
    assert parts == before_parts
    assert etree.tostring(document) == before_doc


@pytest.mark.parametrize("binding", ["relationship", "content_type", "duplicate_part"])
def test_unknown_or_conflicting_comment_extensions_refuse(binding):
    parts, document, paragraphs = _package()
    _add(parts, document, paragraphs[0])
    _with_modern_metadata(parts)
    if binding == "duplicate_part":
        parts["metadata/additional.xml"] = parts["word/commentsExtended.xml"]
    else:
        parts["metadata/future.xml"] = b'<future xmlns="urn:future"/>'
        if binding == "relationship":
            rels = parse_xml(parts["word/_rels/document.xml.rels"])
            etree.SubElement(rels, f"{{{RELS}}}Relationship", Id="rIdFuture",
                             Type="http://schemas.microsoft.com/office/2099/relationships/commentsFuture",
                             Target="../metadata/future.xml")
            parts["word/_rels/document.xml.rels"] = etree.tostring(rels)
        else:
            types = parse_xml(parts["[Content_Types].xml"])
            etree.SubElement(types, f"{{{TYPES}}}Override", PartName="/metadata/future.xml",
                             ContentType="application/vnd.ms-word.commentsFuture+xml")
            parts["[Content_Types].xml"] = etree.tostring(types)
    before_parts, before_doc = dict(parts), etree.tostring(document)
    with pytest.raises(ApplyError) as caught:
        delete_comment(parts, document, "0", AUTHOR)
    assert caught.value.code == "unsupported_comment_structure"
    assert parts == before_parts
    assert etree.tostring(document) == before_doc


@pytest.mark.parametrize("timestamp", [
    "2026-10-16T14:30:42Z", "2026-10-16T17:30:42+03:00",
    "2026-10-16T09:30:42-05:00", "2026-10-16T20:00:42+05:30",
])
def test_comment_date_comes_from_explicit_timestamp_and_survives_update(timestamp):
    parts, document, paragraphs = _package()
    add_comment(parts, document, paragraphs[0], "selected words", "Check wording", AUTHOR, "DA", timestamp=timestamp)
    assert _comments(parts)[0].get(w("date")) == timestamp[:19]
    update_comment(parts, document, "0", "Updated wording", AUTHOR)
    assert _comments(parts)[0].get(w("date")) == timestamp[:19]


@pytest.mark.parametrize("timestamp", [
    "", "2026-02-30T00:00:00Z", "2026-01-01", "2026-01-01T00:00:00",
    "2026-01-01T00:00:00+14:01", "2026-01-01T00:00:00-15:00",
    "2026-01-01T00:00:00+03:60", "2026-01-01T00:00:00Z\n", 1,
])
def test_invalid_comment_timestamp_refuses_before_mutation(timestamp):
    parts, document, paragraphs = _package()
    before_parts, before_doc = dict(parts), etree.tostring(document)
    with pytest.raises(ApplyError) as caught:
        add_comment(parts, document, paragraphs[0], "selected words", "Check wording", AUTHOR, "DA", timestamp=timestamp)
    assert caught.value.code == "invalid_comment"
    assert parts == before_parts
    assert etree.tostring(document) == before_doc
