# SPDX-License-Identifier: Apache-2.0
"""Explicit, closed own-message deletion, with graph/metadata preservation."""

from copy import deepcopy

from lxml import etree
import pytest

from test_drafting_comment_threads import (
    AUTHOR,
    OTHER,
    COMMENTS,
    EXTENDED,
    IDENTITIES,
    EXTENSIBLE,
    W14,
    W15,
    _package,
    _modern,
    _assert_unchanged_failure,
)
from veqtor_docx._drafting_comments import (
    add_comment,
    delete_comment_reply,
    delete_comment_thread,
    read_comments,
    reply_comment,
    set_comment_resolved,
)
from veqtor_docx._ooxml import parse_xml, w


def setup_thread(*, own_root=True, renamed=False):
    parts, document = _package()
    comments = parse_xml(parts[COMMENTS])
    comments[0].set(w("author"), AUTHOR if own_root else OTHER)
    parts[COMMENTS] = etree.tostring(comments)
    _modern(parts, rename=renamed)
    reply = reply_comment(parts, document, "0", "Own response.", AUTHOR, "IS")
    return parts, document, reply["id"]


@pytest.mark.parametrize("own_root", [False, True])
@pytest.mark.parametrize("renamed", [False, True])
def test_delete_only_own_leaf_reply_preserves_root_and_other_reply(own_root, renamed):
    parts, document, selected = setup_thread(own_root=own_root, renamed=renamed)
    other = reply_comment(parts, document, "0", "Keep their response.", OTHER, "OR")
    before_document = deepcopy(document)
    for node in list(before_document.iter()):
        if (
            node.tag
            in {w("commentReference"), w("commentRangeStart"), w("commentRangeEnd")}
            and node.get(w("id")) == selected
        ):
            parent = node.getparent()
            parent.remove(node)
            if (
                node.tag == w("commentReference")
                and parent.tag == w("r")
                and all(c.tag == w("rPr") for c in parent)
            ):
                parent.getparent().remove(parent)
    before_comments = parse_xml(parts[COMMENTS])
    expected = [
        etree.tostring(c, method="c14n")
        for c in before_comments
        if c.get(w("id")) != selected
    ]
    before_metadata = {
        name: data for name, data in parts.items() if name.endswith(".xml")
    }
    result = delete_comment_reply(parts, document, selected, AUTHOR)
    assert result["id"] == selected and result["parent_comment_id"] == "0"
    assert result["operation"] == "delete_comment_reply"
    assert etree.tostring(document, method="c14n") == etree.tostring(
        before_document, method="c14n"
    )
    assert [
        etree.tostring(c, method="c14n") for c in parse_xml(parts[COMMENTS])
    ] == expected
    rows = read_comments(parts, document)
    assert [r["id"] for r in rows] == ["0", other["id"]]
    assert rows[1]["parent_comment_id"] == "0"
    assert parts["custom/data.bin"] == b"Opaque data is untouched"
    assert parts["word/styles.xml"] == before_metadata["word/styles.xml"]


@pytest.mark.parametrize("renamed", [False, True])
def test_delete_explicit_wholly_own_thread_preserves_other_thread_and_status(renamed):
    parts, document, selected = setup_thread(renamed=renamed)
    reply2 = reply_comment(
        parts, document, selected, "Another own reply.", AUTHOR, "IS"
    )
    other = add_comment(
        parts,
        document,
        document.find(f"{w('body')}/{w('p')}"),
        "words",
        "Keep their thread.",
        OTHER,
        "OR",
    )
    set_comment_resolved(parts, document, other["id"], True, AUTHOR)
    before_other = next(
        c for c in parse_xml(parts[COMMENTS]) if c.get(w("id")) == other["id"]
    )
    before_bytes = etree.tostring(before_other, method="c14n")
    before_text = "".join(document.itertext())
    result = delete_comment_thread(
        parts, document, "0", [reply2["id"], "0", selected], AUTHOR
    )
    assert result["thread_root_id"] == "0"
    assert {r["id"] for r in result["deleted_comments"]} == {
        "0",
        selected,
        reply2["id"],
    }
    assert all("date" in r for r in result["deleted_comments"])
    rows = read_comments(parts, document)
    assert (
        len(rows) == 1 and rows[0]["resolved"] is True and rows[0]["id"] == other["id"]
    )
    assert etree.tostring(parse_xml(parts[COMMENTS])[0], method="c14n") == before_bytes
    assert "".join(document.itertext()) == before_text
    assert not any(
        n.get(w("id")) in {"0", selected, reply2["id"]}
        for n in document.iter()
        if n.tag
        in {w("commentRangeStart"), w("commentRangeEnd"), w("commentReference")}
    )


@pytest.mark.parametrize(
    "case",
    [
        "foreign_root",
        "foreign_reply",
        "missing_member",
        "extra_member",
        "duplicate_member",
        "reply_as_root",
    ],
)
def test_invalid_thread_selection_never_mutates(case):
    parts, document, reply = setup_thread(own_root=case != "foreign_root")
    ids = ["0", reply]
    if case == "foreign_reply":
        comments = parse_xml(parts[COMMENTS])
        comments[-1].set(w("author"), OTHER)
        parts[COMMENTS] = etree.tostring(comments)
    root = "0"
    if case == "missing_member":
        ids = ["0"]
    if case == "extra_member":
        ids += ["999"]
    if case == "duplicate_member":
        ids += [reply]
    if case == "reply_as_root":
        root = reply
    expected = (
        "comment_author_mismatch"
        if case in {"foreign_root", "foreign_reply"}
        else "invalid_comment"
        if case == "duplicate_member"
        else "unsupported_comment_structure"
        if case == "reply_as_root"
        else "comment_thread_selection_mismatch"
    )
    _assert_unchanged_failure(
        parts,
        document,
        lambda: delete_comment_thread(parts, document, root, ids, AUTHOR),
        expected,
    )


@pytest.mark.parametrize(
    "case", ["root", "foreign", "child", "outside_anchor", "rich_body"]
)
def test_unsupported_reply_deletion_preserves_package(case):
    parts, document, reply = setup_thread()
    if case == "root":
        reply = "0"
    elif case == "foreign":
        comments = parse_xml(parts[COMMENTS])
        comments[-1].set(w("author"), OTHER)
        parts[COMMENTS] = etree.tostring(comments)
    elif case == "child":
        # Word normally flattens replies; a valid nested graph must still not
        # allow removal of the parent while leaving its child.
        child = reply_comment(parts, document, reply, "Keep dependent.", AUTHOR, "IS")
        comments = parse_xml(parts[COMMENTS])
        selected_para = comments[1][-1].get(f"{{{W14}}}paraId")
        extended = parse_xml(parts[EXTENDED])
        child_para = comments[-1][-1].get(f"{{{W14}}}paraId")
        entry = next(n for n in extended if n.get(f"{{{W15}}}paraId") == child_para)
        entry.set(f"{{{W15}}}paraIdParent", selected_para)
        parts[EXTENDED] = etree.tostring(extended)
        assert child["id"] != reply
    elif case == "outside_anchor":
        parts["word/header1.xml"] = (
            f'<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:commentReference w:id="{reply}"/></w:p></w:hdr>'.encode()
        )
    else:
        comments = parse_xml(parts[COMMENTS])
        etree.SubElement(comments[-1][0], w("drawing"))
        parts[COMMENTS] = etree.tostring(comments)
    _assert_unchanged_failure(
        parts,
        document,
        lambda: delete_comment_reply(parts, document, reply, AUTHOR),
        "comment_author_mismatch"
        if case == "foreign"
        else "unsupported_comment_structure",
    )


def test_plain_single_comment_thread_can_be_deleted_without_metadata_creation():
    parts, document = _package()
    comments = parse_xml(parts[COMMENTS])
    comments[0].set(w("author"), AUTHOR)
    parts[COMMENTS] = etree.tostring(comments)
    result = delete_comment_thread(parts, document, "0", ["0"], AUTHOR)
    assert len(result["deleted_comments"]) == 1 and read_comments(parts, document) == []
    assert all(p not in parts for p in (EXTENDED, IDENTITIES, EXTENSIBLE))


@pytest.mark.parametrize("container", ["pPr", "rPr"])
@pytest.mark.parametrize("payload", ["extension", "revision", "xml_comment"])
def test_unknown_or_tracked_nested_properties_refuse_without_mutation(container, payload):
    parts, document, reply = setup_thread()
    comments = parse_xml(parts[COMMENTS])
    paragraph = comments[-1][0]
    parent = paragraph if container == "pPr" else paragraph.find(w("r"))
    properties = etree.Element(w(container))
    parent.insert(0, properties)
    if payload == "xml_comment":
        properties.append(etree.Comment("unknown metadata"))
    else:
        etree.SubElement(properties, w("rPrChange") if payload == "revision" else "{urn:future}attachment")
    parts[COMMENTS] = etree.tostring(comments)
    _assert_unchanged_failure(parts, document,
                              lambda: delete_comment_reply(parts, document, reply, AUTHOR))


@pytest.mark.parametrize("case", ["missing_root", "duplicate_reference", "reversed_range", "different_quote"])
def test_malformed_or_mismatched_reply_anchors_refuse(case):
    parts, document, reply = setup_thread()
    if case == "missing_root":
        node = next(n for n in document.iter(w("commentReference")) if n.get(w("id")) == "0")
        node.getparent().remove(node)
    elif case == "duplicate_reference":
        node = next(n for n in document.iter(w("commentReference")) if n.get(w("id")) == reply)
        node.getparent().append(deepcopy(node))
    else:
        start = next(n for n in document.iter(w("commentRangeStart")) if n.get(w("id")) == reply)
        end = next(n for n in document.iter(w("commentRangeEnd")) if n.get(w("id")) == reply)
        parent = start.getparent()
        parent.remove(start)
        parent.insert(parent.index(end) + (1 if case == "reversed_range" else 0), start)
    _assert_unchanged_failure(parts, document,
                              lambda: delete_comment_reply(parts, document, reply, AUTHOR))


def test_unanchored_leaf_reply_can_be_removed_preserving_root_anchors():
    parts, document, reply = setup_thread()
    for node in list(document.iter()):
        if node.tag in {w("commentRangeStart"), w("commentRangeEnd"), w("commentReference")} and node.get(w("id")) == reply:
            node.getparent().remove(node)
    before = etree.tostring(document)
    delete_comment_reply(parts, document, reply, AUTHOR)
    assert etree.tostring(document) == before
    assert [r["id"] for r in read_comments(parts, document)] == ["0"]


@pytest.mark.parametrize("move_reply", [False, True])
def test_point_reply_must_share_roots_physical_position(move_reply):
    parts, document, reply = setup_thread()
    for node in list(document.iter()):
        if node.tag in {w("commentRangeStart"), w("commentRangeEnd")}:
            node.getparent().remove(node)
    if move_reply:
        reference = next(n for n in document.iter(w("commentReference")) if n.get(w("id")) == reply)
        run = reference.getparent()
        paragraph = run.getparent()
        paragraph.remove(run)
        paragraph.insert(0, run)
        _assert_unchanged_failure(parts, document,
                                  lambda: delete_comment_reply(parts, document, reply, AUTHOR))
    else:
        delete_comment_reply(parts, document, reply, AUTHOR)
        assert [r["id"] for r in read_comments(parts, document)] == ["0"]


@pytest.mark.parametrize("case", ["nonempty_marker", "foreign_insertion", "reference_outside_run"])
def test_unsupported_anchor_placement_refuses(case):
    parts, document, reply = setup_thread()
    node = next(n for n in document.iter(w("commentReference")) if n.get(w("id")) == reply)
    run = node.getparent()
    if case == "nonempty_marker":
        etree.SubElement(node, w("t")).text = "Do not discard."
    elif case == "reference_outside_run":
        run.remove(node)
        run.getparent().append(node)
    else:
        wrapper = etree.Element(w("ins"), {w("author"): OTHER})
        parent = run.getparent()
        parent.replace(run, wrapper)
        wrapper.append(run)
    _assert_unchanged_failure(parts, document,
                              lambda: delete_comment_reply(parts, document, reply, AUTHOR))


@pytest.mark.parametrize("whole_thread", [False, True])
@pytest.mark.parametrize("location", ["reference", "range_start", "range_end", "root", "header", "comment_body"])
def test_protected_numeric_aliases_refuse_before_mutation(whole_thread, location):
    parts, document, reply = setup_thread()
    tag = {"range_start": "commentRangeStart", "range_end": "commentRangeEnd"}.get(location, "commentReference")
    ident = "0" if location == "root" else reply
    original = next(n for n in document.iter(w(tag)) if n.get(w("id")) == ident)
    alias = deepcopy(original)
    alias.set(w("id"), "0" + ident)
    if location == "header":
        header = etree.Element(w("hdr"))
        etree.SubElement(etree.SubElement(header, w("p")), w("r")).append(alias)
        parts["word/header1.xml"] = etree.tostring(header)
    elif location == "comment_body":
        other = add_comment(parts, document, document.find(f"{w('body')}/{w('p')}"),
                            "words", "Other thread.", OTHER, "OR")
        comments = parse_xml(parts[COMMENTS])
        foreign = next(n for n in comments if n.get(w("id")) == other["id"])
        etree.SubElement(foreign[0], w("r")).append(alias)
        parts[COMMENTS] = etree.tostring(comments)
    else:
        original.getparent().insert(original.getparent().index(original) + 1, alias)
    operation = (lambda: delete_comment_thread(parts, document, "0", ["0", reply], AUTHOR)) if whole_thread else (
        lambda: delete_comment_reply(parts, document, reply, AUTHOR))
    _assert_unchanged_failure(parts, document, operation)


@pytest.mark.parametrize("name", ["word/header1.XML", "word/header.data", "word/header"])
@pytest.mark.parametrize("alias", [False, True])
def test_xml_parts_are_checked_independently_of_filename_suffix(name, alias):
    parts, document, reply = setup_thread()
    header = etree.Element(w("hdr"))
    reference = etree.SubElement(etree.SubElement(etree.SubElement(header, w("p")), w("r")), w("commentReference"))
    reference.set(w("id"), ("0" if alias else "") + reply)
    parts[name] = etree.tostring(header)
    types = parse_xml(parts["[Content_Types].xml"])
    etree.SubElement(types, "{http://schemas.openxmlformats.org/package/2006/content-types}Override",
                     PartName="/" + name, ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml")
    parts["[Content_Types].xml"] = etree.tostring(types)
    _assert_unchanged_failure(parts, document,
                              lambda: delete_comment_reply(parts, document, reply, AUTHOR))


@pytest.mark.parametrize("same_position", [False, True])
def test_empty_ranges_require_same_physical_boundary(same_position):
    parts, document, reply = setup_thread()
    paragraph = document.find(f"{w('body')}/{w('p')}")
    for ident in ["0", reply]:
        start = next(n for n in document.iter(w("commentRangeStart")) if n.get(w("id")) == ident)
        end = next(n for n in document.iter(w("commentRangeEnd")) if n.get(w("id")) == ident)
        start.getparent().remove(start)
        end.getparent().remove(end)
        index = 0 if ident == "0" or same_position else len(paragraph) - 2
        paragraph.insert(index, start)
        paragraph.insert(index + 1, end)
    if same_position:
        delete_comment_reply(parts, document, reply, AUTHOR)
        assert [r["id"] for r in read_comments(parts, document)] == ["0"]
    else:
        _assert_unchanged_failure(parts, document,
                                  lambda: delete_comment_reply(parts, document, reply, AUTHOR))


@pytest.mark.parametrize("ranged", [False, True])
def test_boundaries_on_opposite_sides_of_nontext_content_do_not_match(ranged):
    parts, document, reply = setup_thread()
    paragraph = document.find(f"{w('body')}/{w('p')}")
    boundaries = {}
    for ident in ["0", reply]:
        pair = [n for n in document.iter() if n.tag in {w("commentRangeStart"), w("commentRangeEnd")}
                and n.get(w("id")) == ident]
        boundaries[ident] = pair
        for marker in pair:
            marker.getparent().remove(marker)
    atom = etree.Element(w("r"))
    etree.SubElement(atom, w("noBreakHyphen"))
    paragraph.insert(0, atom)
    if ranged:
        for marker in reversed(boundaries["0"]):
            paragraph.insert(0, marker)
        for index, marker in enumerate(boundaries[reply]):
            paragraph.insert(paragraph.index(atom) + 1 + index, marker)
    else:
        for ident in ["0", reply]:
            reference = next(n for n in document.iter(w("commentReference")) if n.get(w("id")) == ident)
            run = reference.getparent()
            run.getparent().remove(run)
            paragraph.insert(paragraph.index(atom) + (0 if ident == "0" else 1), run)
    _assert_unchanged_failure(parts, document,
                              lambda: delete_comment_reply(parts, document, reply, AUTHOR))


def test_consistent_noncanonical_identity_is_not_silently_renamed():
    parts, document, reply = setup_thread()
    alias = "0" + reply
    comments = parse_xml(parts[COMMENTS])
    comments[-1].set(w("id"), alias)
    parts[COMMENTS] = etree.tostring(comments)
    for marker in document.iter():
        if marker.tag in {w("commentReference"), w("commentRangeStart"), w("commentRangeEnd")} and marker.get(w("id")) == reply:
            marker.set(w("id"), alias)
    result = delete_comment_reply(parts, document, alias, AUTHOR)
    assert result["id"] == alias
    assert [r["id"] for r in read_comments(parts, document)] == ["0"]
