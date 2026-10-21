# SPDX-License-Identifier: Apache-2.0
"""Native Word reply graph, author identity and lossless metadata boundaries."""

from copy import deepcopy
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from lxml import etree

from veqtor_docx._drafting_comments import (
    add_comment,
    comment_mutable_parts,
    delete_comment,
    read_comments,
    reply_comment,
    set_comment_resolved,
    update_comment,
)
from veqtor_docx._ooxml import W_NS, parse_xml, w
from veqtor_docx.apply import ApplyError

AUTHOR = "Shilov, Ilya"
OTHER = "Other Reviewer"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
CEX = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
COMMENTS = "word/comments.xml"
EXTENDED = "word/commentsExtended.xml"
IDENTITIES = "word/commentsIds.xml"
EXTENSIBLE = "word/commentsExtensible.xml"


def _package():
    document = parse_xml(
        f'<w:document xmlns:w="{W_NS}" xmlns:w14="{W14}"><w:body>'
        '<w:p w14:paraId="00000001"><w:r><w:t>Target words.</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Unchanged paragraph.</w:t></w:r></w:p>'
        '</w:body></w:document>'.encode()
    )
    parts = {
        "[Content_Types].xml": (
            f'<Types xmlns="{TYPES}"><Default Extension="xml" ContentType="application/xml"/>'
            '</Types>'
        ).encode(),
        "word/document.xml": etree.tostring(document),
        "word/styles.xml": f'<w:styles xmlns:w="{W_NS}"/>'.encode(),
        "custom/data.bin": b"Opaque data is untouched",
    }
    add_comment(parts, document, document.find(f'{w("body")}/{w("p")}'),
                "Target", "Their original comment.", OTHER, "OR")
    parts["word/document.xml"] = etree.tostring(document)
    return parts, document


def _modern(parts, *, rename=False):
    comments = parse_xml(parts[COMMENTS])
    comments[0][0].set(f"{{{W14}}}paraId", "0000000A")
    comments[0].set("{urn:preserve}foreign", "kept")
    parts[COMMENTS] = etree.tostring(comments)
    roots = {
        EXTENDED: f'<w15:commentsEx xmlns:w15="{W15}"><w15:commentEx w15:paraId="0000000a" w15:done="0" keep="yes"/></w15:commentsEx>',
        IDENTITIES: f'<w16cid:commentsIds xmlns:w16cid="{CID}"><w16cid:commentId w16cid:paraId="0000000A" w16cid:durableId="00000011"/></w16cid:commentsIds>',
        EXTENSIBLE: f'<w16cex:commentsExtensible xmlns:w16cex="{CEX}"><w16cex:commentExtensible w16cex:durableId="00000011" w16cex:dateUtc="2026-01-01T00:00:00Z"/></w16cex:commentsExtensible>',
    }
    bindings = {
        EXTENDED: "http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
        IDENTITIES: "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds",
        EXTENSIBLE: "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible",
    }
    paths = {}
    rels, types = parse_xml(parts["word/_rels/document.xml.rels"]), parse_xml(parts["[Content_Types].xml"])
    for index, (original, text) in enumerate(roots.items()):
        path = f"metadata/thread-{index}.xml" if rename else original
        paths[original] = path
        parts[path] = text.encode()
        name = original.rsplit("/", 1)[-1]
        etree.SubElement(rels, f"{{{RELS}}}Relationship", Id=f"rIdModern{index}",
                         Type=bindings[original], Target=f"/{path}")
        etree.SubElement(types, f"{{{TYPES}}}Override", PartName=f"/{path}",
                         ContentType=f"application/vnd.openxmlformats-officedocument.wordprocessingml.{name[:-4]}+xml")
    parts["word/_rels/document.xml.rels"] = etree.tostring(rels)
    parts["[Content_Types].xml"] = etree.tostring(types)
    return paths


def _reply(parts, document, comment_id="0", **kwargs):
    return reply_comment(parts, document, comment_id, "Agreed.\nSecond line.", AUTHOR, "IS", **kwargs)


def _assert_unchanged_failure(parts, document, call, code="unsupported_comment_structure"):
    before, before_doc = dict(parts), etree.tostring(document)
    with pytest.raises(ApplyError) as caught:
        call()
    assert caught.value.code == code
    assert parts == before
    assert etree.tostring(document) == before_doc


def test_classic_comment_becomes_real_native_reply_thread_with_root_quote_anchors():
    parts, document = _package()
    original_doc = etree.tostring(document)
    before = parse_xml(parts[COMMENTS])[0]
    original_body = deepcopy(before[0])
    result = _reply(parts, document, timestamp="2026-10-20T15:20:30+03:00")
    assert result == {
        "id": "1", "author": AUTHOR, "initials": "IS",
        "text": "Agreed.\nSecond line.", "paragraph_indexes": [0],
        "parent_comment_id": "0", "thread_root_id": "0", "resolved": False,
        "operation": "reply_comment",
    }
    assert etree.tostring(document) != original_doc
    assert [node.get(w("id")) for node in document.iter(w("commentReference"))] == ["0", "1"]
    assert [node.get(w("id")) for node in document.iter(w("commentRangeStart"))] == ["0", "1"]
    assert [node.get(w("id")) for node in document.iter(w("commentRangeEnd"))] == ["0", "1"]
    comments = parse_xml(parts[COMMENTS])
    assert comments[0].attrib == before.attrib
    assert etree.tostring(comments[0][0][0], method="c14n", exclusive=True) == etree.tostring(
        original_body[0], method="c14n", exclusive=True
    )
    assert comments[1].get(w("date")) == "2026-10-20T15:20:30"
    root_para_id = comments[0][0].get(f"{{{W14}}}paraId")
    reply_para_id = comments[1][0].get(f"{{{W14}}}paraId")
    assert root_para_id != reply_para_id and root_para_id != "00000001"
    for paragraph in (comments[0][0], comments[1][0]):
        for prefix in paragraph.get(f"{{{MC}}}Ignorable").split():
            assert prefix in paragraph.nsmap
    extension = parse_xml(parts[EXTENDED])
    assert extension[0].get(f"{{{W15}}}paraId") == root_para_id
    assert extension[1].get(f"{{{W15}}}paraIdParent") == root_para_id
    assert extension[1].get(f"{{{W15}}}paraId") == reply_para_id
    assert parts["custom/data.bin"] == b"Opaque data is untouched"


@pytest.mark.parametrize("rename", [False, True])
def test_modern_reply_preserves_foreign_bodies_metadata_and_binding_bytes(rename):
    parts, document = _package()
    paths = _modern(parts, rename=rename)
    original_comment = etree.tostring(parse_xml(parts[COMMENTS])[0])
    original_metadata = {path: etree.tostring(parse_xml(parts[path])[0]) for path in paths.values()}
    original_binding = {path: parts[path] for path in ("word/_rels/document.xml.rels", "[Content_Types].xml")}
    reply = _reply(parts, document, timestamp="2026-10-20T15:20:30+03:00")
    assert reply["parent_comment_id"] == "0"
    assert etree.tostring(parse_xml(parts[COMMENTS])[0]) == original_comment
    assert all(etree.tostring(parse_xml(parts[path])[0]) == value for path, value in original_metadata.items())
    assert all(parts[path] == value for path, value in original_binding.items())
    assert set(paths.values()) <= comment_mutable_parts(parts)
    identity = parse_xml(parts[paths[IDENTITIES]])[1]
    extra = parse_xml(parts[paths[EXTENSIBLE]])[1]
    assert extra.get(f"{{{CEX}}}durableId") == identity.get(f"{{{CID}}}durableId")
    assert 0 < int(identity.get(f"{{{CID}}}durableId"), 16) < 0x7FFFFFFF
    assert extra.get(f"{{{CEX}}}dateUtc") == "2026-10-20T12:20:30Z"
    assert extra.get(f"{{{CEX}}}intelligentPlaceholder") is None


def test_reply_to_reply_appends_to_root_and_own_reply_body_can_be_updated():
    parts, document = _package()
    first = _reply(parts, document)
    second = reply_comment(parts, document, first["id"], "Further reply", "Another author", "AA")
    assert second["parent_comment_id"] == "0"
    assert second["thread_root_id"] == "0"
    root_before = etree.tostring(parse_xml(parts[COMMENTS])[0])
    metadata_before = parts[EXTENDED]
    updated = update_comment(parts, document, first["id"], "Changed reply", AUTHOR)
    assert updated["parent_comment_id"] == "0"
    assert updated["paragraph_indexes"] == [0]
    assert etree.tostring(parse_xml(parts[COMMENTS])[0]) == root_before
    assert parts[EXTENDED] == metadata_before
    _assert_unchanged_failure(parts, document, lambda: delete_comment(parts, document, first["id"], AUTHOR))


@pytest.mark.parametrize("prefix", ["w14", "native14"])
def test_reply_on_word_saved_thread_keeps_ignorable_prefix_bound_after_serialization(prefix):
    parts, document = _package()
    _modern(parts)
    old = parse_xml(parts[COMMENTS])
    comments = etree.Element(w("comments"), nsmap={"w": W_NS, prefix: W14, "mc": MC})
    comments.set(f"{{{MC}}}Ignorable", prefix)
    for child in old:
        comments.append(child)
    parts[COMMENTS] = etree.tostring(comments)
    first = _reply(parts, document)
    set_comment_resolved(parts, document, first["id"], True, AUTHOR)
    set_comment_resolved(parts, document, first["id"], False, AUTHOR)
    _reply(parts, document, first["id"])
    # lxml can parse unbound lexical mc:Ignorable prefixes, but Word rejects
    # the package. Check the serialized tree, including each appended reply.
    saved = parse_xml(parts[COMMENTS])
    for node in saved.iter():
        for token in node.get(f"{{{MC}}}Ignorable", "").split():
            assert token in node.nsmap
    assert all(saved[index][0].get(f"{{{MC}}}Ignorable") == prefix for index in (1, 2))
    assert read_comments(parts, document)[-1]["thread_root_id"] == "0"


def test_any_author_can_resolve_or_reopen_root_via_reply_without_reauthoring():
    parts, document = _package()
    _modern(parts)
    reply = _reply(parts, document)
    before = dict(parts)
    result = set_comment_resolved(parts, document, reply["id"], True, AUTHOR)
    assert result["id"] == "0"
    assert result["author"] == OTHER
    assert result["resolved"] is True
    assert {name for name in parts if parts[name] != before[name]} == {EXTENDED}
    assert all(item["resolved"] is True for item in read_comments(parts, document))
    _assert_unchanged_failure(parts, document, lambda: _reply(parts, document), "comment_thread_resolved")
    result = set_comment_resolved(parts, document, "0", False, AUTHOR)
    assert result["resolved"] is False
    assert _reply(parts, document)["id"] == "2"


def test_resolving_classic_comment_bootstraps_bound_extension_and_retains_body():
    parts, document = _package()
    text = read_comments(parts, document)[0]["text"]
    result = set_comment_resolved(parts, document, "0", True, AUTHOR)
    assert result["resolved"] is True
    assert result["text"] == text
    assert result["author"] == OTHER
    assert read_comments(parts, document)[0] == {key: value for key, value in result.items() if key != "operation"}


def test_missing_modern_entry_reports_unknown_resolution_instead_of_false():
    parts, document = _package()
    _modern(parts)
    root = parse_xml(parts[EXTENDED])
    root.remove(root[0])
    parts[EXTENDED] = etree.tostring(root)
    assert read_comments(parts, document)[0]["resolved"] is None
    assert read_comments(parts, document)[0]["parent_comment_id"] is None


@pytest.mark.parametrize("flag,expected", [
    (None, False), ("0", False), ("false", False), ("off", False),
    ("1", True), ("true", True), ("on", True),
])
def test_native_done_lexical_forms(flag, expected):
    parts, document = _package()
    _modern(parts)
    root = parse_xml(parts[EXTENDED])
    if flag is None:
        del root[0].attrib[f"{{{W15}}}done"]
    else:
        root[0].set(f"{{{W15}}}done", flag)
    parts[EXTENDED] = etree.tostring(root)
    assert read_comments(parts, document)[0]["resolved"] is expected


def test_thread_identity_uses_last_paragraph_of_multiparagraph_comment():
    parts, document = _package()
    _modern(parts)
    comments = parse_xml(parts[COMMENTS])
    first = comments[0][0]
    first.set(f"{{{W14}}}paraId", "00000009")
    last = etree.SubElement(comments[0], w("p"))
    last.set(f"{{{W14}}}paraId", "0000000A")
    etree.SubElement(etree.SubElement(last, w("r")), w("t")).text = "Second paragraph"
    parts[COMMENTS] = etree.tostring(comments)
    _reply(parts, document)
    assert parse_xml(parts[EXTENDED])[-1].get(f"{{{W15}}}paraIdParent") == "0000000A"


@pytest.mark.parametrize("damage", [
    "cycle", "missing_parent", "orphan_reply", "duplicate_para", "duplicate_extended",
    "invalid_done", "duplicate_durable", "bad_hex", "not_last_paragraph",
])
def test_malformed_thread_graph_refuses_read_and_write_without_mutation(damage):
    parts, document = _package()
    _modern(parts)
    _reply(parts, document)
    comments, extended = parse_xml(parts[COMMENTS]), parse_xml(parts[EXTENDED])
    if damage == "cycle":
        extended[0].set(f"{{{W15}}}paraIdParent", extended[1].get(f"{{{W15}}}paraId"))
    elif damage == "missing_parent":
        extended[1].set(f"{{{W15}}}paraIdParent", "0000FFFF")
    elif damage == "orphan_reply":
        comments.remove(comments[1])
    elif damage == "duplicate_para":
        comments[1][0].set(f"{{{W14}}}paraId", comments[0][0].get(f"{{{W14}}}paraId"))
    elif damage == "duplicate_extended":
        extended.append(deepcopy(extended[0]))
    elif damage == "invalid_done":
        extended[0].set(f"{{{W15}}}done", "yes")
    elif damage == "bad_hex":
        extended[0].set(f"{{{W15}}}paraId", "abc")
    elif damage == "not_last_paragraph":
        etree.SubElement(comments[0], w("p"))
    else:
        identities = parse_xml(parts[IDENTITIES])
        identities[1].set(f"{{{CID}}}durableId", identities[0].get(f"{{{CID}}}durableId"))
        parts[IDENTITIES] = etree.tostring(identities)
    parts[COMMENTS], parts[EXTENDED] = etree.tostring(comments), etree.tostring(extended)
    _assert_unchanged_failure(parts, document, lambda: read_comments(parts, document))
    _assert_unchanged_failure(parts, document, lambda: _reply(parts, document))
    _assert_unchanged_failure(parts, document, lambda: set_comment_resolved(parts, document, "0", True, AUTHOR))


@pytest.mark.parametrize("damage", ["extension_payload", "task", "unknown_relationship", "invalid_durable"])
def test_unsupported_modern_metadata_refuses_thread_writes(damage):
    parts, document = _package()
    _modern(parts)
    if damage in ("extension_payload", "task"):
        extra = parse_xml(parts[EXTENSIBLE])
        if damage == "task":
            extra[0].set(f"{{{CEX}}}intelligentPlaceholder", "true")
        else:
            etree.SubElement(extra[0], f"{{{CEX}}}extLst")
        parts[EXTENSIBLE] = etree.tostring(extra)
    elif damage == "invalid_durable":
        identities = parse_xml(parts[IDENTITIES])
        identities[0].set(f"{{{CID}}}durableId", "FFFFFFFF")
        parts[IDENTITIES] = etree.tostring(identities)
    else:
        rels = parse_xml(parts["word/_rels/document.xml.rels"])
        etree.SubElement(rels, f"{{{RELS}}}Relationship", Id="rIdFuture",
                         Type="https://example.test/commentsFuture", Target="future.xml")
        parts["word/_rels/document.xml.rels"] = etree.tostring(rels)
    _assert_unchanged_failure(parts, document, lambda: _reply(parts, document))
    _assert_unchanged_failure(parts, document, lambda: set_comment_resolved(parts, document, "0", True, AUTHOR))


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_resolution_requires_real_boolean(value):
    parts, document = _package()
    _assert_unchanged_failure(
        parts, document, lambda: set_comment_resolved(parts, document, "0", value, AUTHOR),
        "invalid_comment",
    )


@pytest.mark.parametrize("kwargs", [{"timestamp": "invalid"}, {"timestamp": "2026-02-30T00:00:00Z"}])
def test_invalid_reply_timestamp_never_mutates(kwargs):
    parts, document = _package()
    _assert_unchanged_failure(parts, document, lambda: _reply(parts, document, **kwargs), "invalid_comment")


def test_comment_thread_survives_actual_zip_save_reopen(tmp_path):
    parts, document = _package()
    paths = _modern(parts, rename=True)
    reply = _reply(parts, document, timestamp="2026-10-20T15:20:30+03:00")
    set_comment_resolved(parts, document, reply["id"], True, AUTHOR)
    parts["word/document.xml"] = etree.tostring(document)
    path = tmp_path / "threaded.docx"
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    with ZipFile(path) as archive:
        reopened = {name: archive.read(name) for name in archive.namelist()}
    assert reopened == parts
    items = read_comments(reopened, parse_xml(reopened["word/document.xml"]))
    assert [(item["id"], item["parent_comment_id"], item["resolved"]) for item in items] == [
        ("0", None, True), ("1", "0", True),
    ]
    assert set(paths.values()) <= comment_mutable_parts(reopened)


def test_reply_inside_own_pending_insertion_keeps_revision_identity_and_text():
    parts, document = _package()
    paragraph = document.find(f'{w("body")}/{w("p")}')
    insertion = etree.Element(w("ins"))
    insertion.set(w("id"), "10")
    insertion.set(w("author"), AUTHOR)
    insertion.set(w("date"), "2026-10-20T12:00:00")
    for child in list(paragraph):
        paragraph.remove(child)
        insertion.append(child)
    paragraph.append(insertion)
    attributes = dict(insertion.attrib)
    text = "".join(insertion.itertext())
    result = _reply(parts, document)
    assert result["parent_comment_id"] == "0"
    assert insertion.attrib == attributes
    assert "".join(insertion.itertext()) == text
    assert [node.get(w("id")) for node in insertion.iter(w("commentReference"))] == ["0", "1"]


@pytest.mark.parametrize("damage", ["foreign_insertion", "unanchored", "reversed_range", "tracked_reference_format"])
def test_unsupported_reply_anchors_refuse_atomically(damage):
    parts, document = _package()
    paragraph = document.find(f'{w("body")}/{w("p")}')
    if damage == "foreign_insertion":
        insertion = etree.Element(w("ins"))
        insertion.set(w("id"), "10")
        insertion.set(w("author"), OTHER)
        for child in list(paragraph):
            paragraph.remove(child)
            insertion.append(child)
        paragraph.append(insertion)
    elif damage == "unanchored":
        for node in list(document.iter()):
            if node.tag in (w("commentRangeStart"), w("commentRangeEnd"), w("commentReference")):
                node.getparent().remove(node)
    elif damage == "reversed_range":
        start = next(document.iter(w("commentRangeStart")))
        paragraph.remove(start)
        paragraph.append(start)
    else:
        reference = next(document.iter(w("commentReference")))
        properties = reference.getparent().find(w("rPr"))
        etree.SubElement(properties, w("rPrChange"))
    _assert_unchanged_failure(parts, document, lambda: _reply(parts, document))
