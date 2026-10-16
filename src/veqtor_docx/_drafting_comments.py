# SPDX-License-Identifier: Apache-2.0
"""Bounded, in-memory comment operations for the author drafting workflow.

These helpers deliberately do not save a package. The caller owns the source
snapshot, whole-batch transaction and collateral checks. Existing authors,
comment attributes and unrelated anchors are preserved. Replies, nonstandard
comment part locations and rich comment body replacement fail closed.
"""

from __future__ import annotations

from copy import deepcopy
import re

from lxml import etree

from ._ooxml import (
    W_NS,
    canonical_body_flow_v1,
    current_text_atom,
    is_xml_text_compatible,
    iter_canonical_paragraph_nodes,
    parse_xml,
    tracked_change_author_validation_error,
    w,
)
from .apply import ApplyError

_COMMENTS = "word/comments.xml"
_RELS = "word/_rels/document.xml.rels"
_TYPES = "[Content_Types].xml"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_TYPE_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_COMMENT_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
_COMMENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
_W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
_W16CEX = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_MAX_TEXT = 20_000
_MAX_ID = 2_147_483_647
_ANCHOR_TAGS = frozenset(w(tag) for tag in (
    "commentRangeStart", "commentRangeEnd", "commentReference"
))
_RANGE_MARKERS = frozenset(w(tag) for tag in (
    "commentRangeStart", "commentRangeEnd", "bookmarkStart", "bookmarkEnd", "proofErr"
))
COMMENT_MUTABLE_PARTS = frozenset({
    _COMMENTS, _RELS, _TYPES,
    "word/commentsExtended.xml",
    "word/commentsIds.xml",
    "word/commentsExtensible.xml",
})


def _unsupported(detail: str) -> ApplyError:
    return ApplyError("unsupported_comment_structure", detail)


def _xml(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _validate_text(value: str, name: str, *, allow_empty: bool = False) -> None:
    if (not isinstance(value, str) or (not value and not allow_empty)
            or len(value) > _MAX_TEXT or not is_xml_text_compatible(value)):
        raise ApplyError("invalid_comment", f"{name} must be valid nonempty XML text of at most {_MAX_TEXT} characters")


def _validate_author(author: str) -> None:
    if tracked_change_author_validation_error(author):
        raise ApplyError("invalid_comment", "author must be a nonempty valid tracked-change author")


def _validate_comment_location(parts: dict[str, bytes]) -> None:
    if _RELS not in parts:
        return
    root = parse_xml(parts[_RELS])
    if root.tag != f"{{{_REL_NS}}}Relationships":
        raise _unsupported("unexpected document relationships root")
    related = [node for node in root if node.get("Type") == _COMMENT_REL]
    if len(related) > 1:
        raise _unsupported("multiple comments relationships")
    if related and (related[0].get("Target") not in ("comments.xml", "/word/comments.xml")
                    or related[0].get("TargetMode") not in (None, "Internal")):
        raise _unsupported("comments are stored at a nonstandard or external location")


def _comment_root(parts: dict[str, bytes]) -> etree._Element:
    _validate_comment_location(parts)
    if _COMMENTS not in parts:
        return etree.Element(w("comments"), nsmap={"w": W_NS})
    root = parse_xml(parts[_COMMENTS])
    if root.tag != w("comments") or any(child.tag != w("comment") for child in root):
        raise _unsupported("comments.xml is not a plain Word comments collection")
    ids: set[str] = set()
    for comment in root:
        ident = comment.get(w("id"))
        if ident is None or not ident.isascii() or not ident.isdigit() or len(ident) > 10 or int(ident) > _MAX_ID:
            raise _unsupported("comment identifiers must be bounded nonnegative integers")
        if str(int(ident)) in ids:
            raise _unsupported("duplicate comment identifiers")
        ids.add(str(int(ident)))
    return root


def _body_paragraphs(document: etree._Element) -> list:
    body = document.find(w("body"))
    return list(canonical_body_flow_v1(body).paragraphs) if body is not None else []


def _comment_text(comment: etree._Element) -> str:
    paragraphs = []
    for paragraph in comment.iter(w("p")):
        paragraphs.append("".join(
            value for node in iter_canonical_paragraph_nodes(paragraph)
            if (value := current_text_atom(node, boundary=paragraph)) is not None
        ))
    return "\n".join(paragraphs)


def _anchor_indexes(document: etree._Element) -> dict[str, list[int]]:
    indexes: dict[str, set[int]] = {}
    for item in _body_paragraphs(document):
        for node in item.element.iter():
            if (node.tag in _ANCHOR_TAGS
                    and next(node.iterancestors(w("p")), None) is item.element):
                ident = node.get(w("id"))
                if ident is not None:
                    indexes.setdefault(ident, set()).add(item.paragraph_index)
    return {ident: sorted(values) for ident, values in indexes.items()}


def _summary(comment: etree._Element, document: etree._Element,
             indexes: dict[str, list[int]] | None = None) -> dict:
    ident = comment.get(w("id"))
    return {
        "id": ident,
        "author": comment.get(w("author"), ""),
        "initials": comment.get(w("initials"), ""),
        "text": _comment_text(comment),
        "paragraph_indexes": (indexes if indexes is not None else _anchor_indexes(document)).get(ident, []),
    }


def read_comments(parts: dict[str, bytes], document: etree._Element) -> list[dict]:
    """Read classic/modern bodies; zero-based indexes locate main-body anchors."""
    root = _comment_root(parts)
    _require_existing_binding(parts)
    indexes = _anchor_indexes(document)
    return [_summary(comment, document, indexes) for comment in root]


def _owned_comment(parts: dict[str, bytes], comment_id: str, author: str) -> tuple[etree._Element, etree._Element]:
    _validate_author(author)
    if not isinstance(comment_id, str):
        raise ApplyError("invalid_comment", "comment_id must be a string")
    root = _comment_root(parts)
    _require_existing_binding(parts)
    comment = next((node for node in root if node.get(w("id")) == comment_id), None)
    if comment is None:
        raise ApplyError("comment_not_found", "the requested comment does not exist", comment_id=comment_id)
    if comment.get(w("author")) != author:
        raise ApplyError("comment_author_mismatch", "only the configured author's comments can be changed", comment_id=comment_id)
    return root, comment


def _relationships(parts: dict[str, bytes]) -> dict[str, bytes]:
    """Stage the standard comments relationship and content-type declaration."""
    rels = parse_xml(parts[_RELS]) if _RELS in parts else etree.Element(
        f"{{{_REL_NS}}}Relationships", nsmap={None: _REL_NS}
    )
    if rels.tag != f"{{{_REL_NS}}}Relationships":
        raise _unsupported("unexpected document relationships root")
    comment_rels = [node for node in rels if node.get("Type") == _COMMENT_REL]
    if len(comment_rels) > 1:
        raise _unsupported("multiple comments relationships")
    if comment_rels:
        rel = comment_rels[0]
        if rel.get("Target") not in ("comments.xml", "/word/comments.xml") or rel.get("TargetMode") not in (None, "Internal"):
            raise _unsupported("comments are stored at a nonstandard or external location")
    else:
        used_ids = {node.get("Id") for node in rels}
        index = 1
        while f"rId{index}" in used_ids:
            index += 1
        etree.SubElement(rels, f"{{{_REL_NS}}}Relationship", Id=f"rId{index}", Type=_COMMENT_REL, Target="comments.xml")
    if _TYPES not in parts:
        raise _unsupported("DOCX content types are missing")
    types = parse_xml(parts[_TYPES])
    if types.tag != f"{{{_TYPE_NS}}}Types":
        raise _unsupported("unexpected content-types root")
    overrides = [node for node in types if node.get("PartName") == "/word/comments.xml"]
    if len(overrides) > 1 or (overrides and overrides[0].get("ContentType") != _COMMENT_TYPE):
        raise _unsupported("conflicting comments content type")
    if not overrides:
        etree.SubElement(types, f"{{{_TYPE_NS}}}Override", PartName="/word/comments.xml", ContentType=_COMMENT_TYPE)
    changed = {}
    if not comment_rels:
        changed[_RELS] = _xml(rels)
    if not overrides:
        changed[_TYPES] = _xml(types)
    return changed


def _require_existing_binding(parts: dict[str, bytes]) -> None:
    if _COMMENTS in parts and _relationships(parts):
        raise _unsupported("existing comments require their standard relationship and content type")


def _new_id(root: etree._Element, document: etree._Element) -> str:
    values = [int(node.get(w("id"))) for node in root]
    for node in document.iter():
        if node.tag in _ANCHOR_TAGS:
            raw = node.get(w("id"), "")
            if not raw.isascii() or not raw.isdigit() or len(raw) > 10 or int(raw) > _MAX_ID:
                raise _unsupported("comment anchor identifier is invalid")
            values.append(int(raw))
    ident = max(values, default=-1) + 1
    if ident > _MAX_ID:
        raise _unsupported("comment identifier space is exhausted")
    return str(ident)


def _run_text(run: etree._Element) -> str:
    if run.tag != w("r") or any(child.tag not in (w("rPr"), w("t")) for child in run):
        raise _unsupported("the comment quote must lie in simple text runs")
    if len(run.findall(w("rPr"))) > 1 or any(node.tag == w("rPrChange") for node in run.iter()):
        raise _unsupported("tracked run formatting is not supported for comment anchoring")
    return "".join(child.text or "" for child in run if child.tag == w("t"))


def _text_run(run: etree._Element, text: str) -> etree._Element:
    result = deepcopy(run)
    for child in list(result):
        if child.tag != w("rPr"):
            result.remove(child)
    node = etree.SubElement(result, w("t"))
    node.set(_XML_SPACE, "preserve")
    node.text = text
    return result


def _quote_plan(paragraph: etree._Element, quote: str, author: str) -> tuple[etree._Element, list[tuple[etree._Element, int, int]], int, int]:
    atoms = []
    offset = 0
    for node in iter_canonical_paragraph_nodes(paragraph):
        value = current_text_atom(node, boundary=paragraph)
        if value is not None:
            atoms.append((node, value, offset, offset + len(value)))
            offset += len(value)
    current = "".join(item[1] for item in atoms)
    first = current.find(quote)
    if first < 0:
        raise ApplyError("comment_quote_not_found", "the exact comment quote is absent from the paragraph")
    if current.find(quote, first + 1) >= 0:
        raise ApplyError("ambiguous_comment_quote", "the exact comment quote occurs more than once in the paragraph")
    end = first + len(quote)
    covered = [atom for atom in atoms if atom[2] < end and atom[3] > first]
    runs = []
    for node, _, start, stop in covered:
        run = node.getparent()
        if node.tag != w("t") or run is None or run.tag != w("r"):
            raise _unsupported("the comment quote must lie in simple text runs")
        _run_text(run)
        if not runs or runs[-1] is not run:
            runs.append(run)
    container = runs[0].getparent()
    if any(run.getparent() is not container for run in runs):
        raise _unsupported("the comment quote crosses revision or other container boundaries")
    if container is not paragraph:
        if container.tag != w("ins") or container.getparent() is not paragraph or container.get(w("author")) != author:
            raise _unsupported("comment quotes can lie in plain text or one own pending insertion")
    # Keep existing markers and comment-reference runs where they are. Reject
    # invisible revisions or other containers interleaved with the quote.
    children = list(container)
    start_index, end_index = children.index(runs[0]), children.index(runs[-1])
    for child in children[start_index:end_index + 1]:
        if child in runs or child.tag in _RANGE_MARKERS:
            continue
        if child.tag == w("r") and all(node.tag in (w("rPr"), w("commentReference")) for node in child):
            continue
        raise _unsupported("the comment quote crosses unsupported inline markup")
    mapped = []
    for run in runs:
        run_atoms = [atom for atom in atoms if atom[0].getparent() is run]
        mapped.append((run, run_atoms[0][2], run_atoms[-1][3]))
    return container, mapped, first, end


def _comment_paragraph(text: str, template: etree._Element | None = None) -> etree._Element:
    paragraph = deepcopy(template) if template is not None else etree.Element(w("p"))
    template_run = next((child for child in paragraph if child.tag == w("r") and child.find(w("t")) is not None), None)
    properties = deepcopy(template_run.find(w("rPr"))) if template_run is not None and template_run.find(w("rPr")) is not None else None
    for child in list(paragraph):
        if child.tag != w("pPr"):
            paragraph.remove(child)
    run = etree.SubElement(paragraph, w("r"))
    if properties is not None:
        run.append(properties)
    # A single paragraph preserves modern comment paraId relationships. Line
    # breaks in replacement text are native breaks inside that paragraph.
    for index, line in enumerate(text.split("\n")):
        if index:
            etree.SubElement(run, w("br"))
        node = etree.SubElement(run, w("t"))
        node.set(_XML_SPACE, "preserve")
        node.text = line
    return paragraph


def add_comment(parts: dict[str, bytes], document: etree._Element, paragraph: etree._Element,
                quote: str, text: str, author: str, initials: str) -> dict:
    """Anchor a new classic comment to one unique exact current-reading quote."""
    _validate_text(quote, "quote")
    _validate_text(text, "text")
    _validate_author(author)
    _validate_text(initials, "initials", allow_empty=True)
    if not any(item.element is paragraph for item in _body_paragraphs(document)):
        raise _unsupported("the comment target is not a canonical body paragraph")
    root = _comment_root(parts)
    container, mapped, first, end = _quote_plan(paragraph, quote, author)
    staged = _relationships(parts)
    ident = _new_id(root, document)
    comment = etree.SubElement(root, w("comment"))
    comment.set(w("id"), ident)
    comment.set(w("author"), author)
    comment.set(w("initials"), initials)
    comment.append(_comment_paragraph(text))
    staged[_COMMENTS] = _xml(root)
    # Build all replacements before touching the caller's document tree.
    replacements = []
    for run, start, stop in mapped:
        content = _run_text(run)
        local_start, local_end = max(first - start, 0), min(end - start, stop - start)
        nodes = []
        if local_start:
            nodes.append(_text_run(run, content[:local_start]))
        if start <= first < stop:
            marker = etree.Element(w("commentRangeStart"))
            marker.set(w("id"), ident)
            nodes.append(marker)
        nodes.append(_text_run(run, content[local_start:local_end]))
        if start < end <= stop:
            marker = etree.Element(w("commentRangeEnd"))
            marker.set(w("id"), ident)
            nodes.append(marker)
            reference_run = etree.Element(w("r"))
            properties = etree.SubElement(reference_run, w("rPr"))
            style = etree.SubElement(properties, w("rStyle"))
            style.set(w("val"), "CommentReference")
            reference = etree.SubElement(reference_run, w("commentReference"))
            reference.set(w("id"), ident)
            nodes.append(reference_run)
        if local_end < len(content):
            nodes.append(_text_run(run, content[local_end:]))
        replacements.append((run, nodes))
    for run, nodes in replacements:
        index = container.index(run)
        container.remove(run)
        for node in nodes:
            container.insert(index, node)
            index += 1
    parts.update(staged)
    return {**_summary(comment, document), "operation": "add_comment"}


def update_comment(parts: dict[str, bytes], document: etree._Element,
                   comment_id: str, text: str, author: str) -> dict:
    """Replace one's own simple comment body, preserving identity and metadata."""
    _validate_text(text, "text")
    root, comment = _owned_comment(parts, comment_id, author)
    if len(comment) != 1 or comment[0].tag != w("p"):
        raise _unsupported("comment updates currently require one simple paragraph")
    paragraph = comment[0]
    for child in paragraph:
        if child.tag == w("pPr"):
            if any(node.tag == w("pPrChange") for node in child.iter()):
                raise _unsupported("tracked comment paragraph formatting is unsupported")
        elif child.tag == w("r"):
            if any(node.tag not in (w("rPr"), w("t"), w("br"), w("cr"), w("tab"), w("annotationRef")) for node in child):
                raise _unsupported("rich comment bodies cannot be replaced")
            if any(node.tag == w("rPrChange") for node in child.iter()):
                raise _unsupported("tracked comment formatting is unsupported")
        else:
            raise _unsupported("rich comment bodies cannot be replaced")
    replacement = _comment_paragraph(text, paragraph)
    # Retain the annotation marker and its own formatting, where present.
    markers = []
    for run in paragraph.findall(w("r")):
        if run.find(w("annotationRef")) is not None:
            marker = deepcopy(run)
            for node in list(marker):
                if node.tag not in (w("rPr"), w("annotationRef")):
                    marker.remove(node)
            markers.append(marker)
    index = 1 if replacement.find(w("pPr")) is not None else 0
    for marker in markers:
        replacement.insert(index, marker)
        index += 1
    comment.replace(paragraph, replacement)
    parts[_COMMENTS] = _xml(root)
    return {**_summary(comment, document), "operation": "update_comment"}


def _hex_identity(node: etree._Element, attribute: str, *, optional: bool = False) -> str | None:
    """Compare ST_LongHexNumber values semantically, without rewriting XML."""
    value = node.get(attribute)
    if optional and value is None:
        return None
    if value is None or re.fullmatch(r"[0-9A-Fa-f]{8}", value) is None:
        raise _unsupported("modern comment identifiers must contain exactly eight hexadecimal digits")
    return value.upper()


def _metadata_for_delete(parts: dict[str, bytes], comment: etree._Element) -> dict[str, bytes]:
    paragraph_ids = {_hex_identity(node, f"{{{_W14}}}paraId", optional=True)
                     for node in comment.iter(w("p"))}
    paragraph_ids.discard(None)
    for other in comment.getparent():
        if other is not comment and any(
            _hex_identity(node, f"{{{_W14}}}paraId", optional=True) in paragraph_ids
            for node in other.iter(w("p"))
        ):
            raise _unsupported("multiple comments share a modern paragraph identifier")
    staged = {}
    extended = "word/commentsExtended.xml"
    if extended in parts:
        root = parse_xml(parts[extended])
        if root.tag != f"{{{_W15}}}commentsEx" or any(node.tag != f"{{{_W15}}}commentEx" for node in root):
            raise _unsupported("unsupported extended comment metadata")
        identities = [_hex_identity(node, f"{{{_W15}}}paraId") for node in root]
        if len(identities) != len(set(identities)):
            raise _unsupported("duplicate extended comment paragraph identifiers")
        for node, ident in zip(root, identities):
            own = ident in paragraph_ids
            parent = _hex_identity(node, f"{{{_W15}}}paraIdParent", optional=True)
            if parent in paragraph_ids or (own and parent is not None):
                raise _unsupported("threaded comments cannot be deleted by this workflow")
        removed = [node for node, ident in zip(root, identities) if ident in paragraph_ids]
        for node in removed:
            root.remove(node)
        if removed:
            staged[extended] = _xml(root)
    ids_part = "word/commentsIds.xml"
    durable_ids = set()
    if ids_part in parts:
        root = parse_xml(parts[ids_part])
        if root.tag != f"{{{_W16CID}}}commentsIds" or any(node.tag != f"{{{_W16CID}}}commentId" for node in root):
            raise _unsupported("unsupported comment identity metadata")
        para_identities = [_hex_identity(node, f"{{{_W16CID}}}paraId") for node in root]
        durable_identities = [_hex_identity(node, f"{{{_W16CID}}}durableId") for node in root]
        if (len(para_identities) != len(set(para_identities))
                or len(durable_identities) != len(set(durable_identities))):
            raise _unsupported("comment identity metadata is ambiguous")
        removed = [node for node, ident in zip(root, para_identities) if ident in paragraph_ids]
        durable_ids = {durable for ident, durable in zip(para_identities, durable_identities)
                       if ident in paragraph_ids}
        for node in removed:
            root.remove(node)
        if removed:
            staged[ids_part] = _xml(root)
    extensible = "word/commentsExtensible.xml"
    if extensible in parts:
        root = parse_xml(parts[extensible])
        if root.tag != f"{{{_W16CEX}}}commentsExtensible" or any(node.tag != f"{{{_W16CEX}}}commentExtensible" for node in root):
            raise _unsupported("unsupported extensible comment metadata")
        identities = [_hex_identity(node, f"{{{_W16CEX}}}durableId") for node in root]
        if len(identities) != len(set(identities)):
            raise _unsupported("duplicate extensible comment identifiers")
        removed = [node for node, ident in zip(root, identities) if ident in durable_ids]
        for node in removed:
            root.remove(node)
        if removed:
            staged[extensible] = _xml(root)
    return staged


def delete_comment(parts: dict[str, bytes], document: etree._Element,
                   comment_id: str, author: str) -> dict:
    """Delete one's own unthreaded comment and exactly its anchors/metadata."""
    root, comment = _owned_comment(parts, comment_id, author)
    result = {**_summary(comment, document), "operation": "delete_comment"}
    staged = _metadata_for_delete(parts, comment)
    # Do not orphan anchors in headers/footnotes or unknown modern extensions.
    for name, data in parts.items():
        if not name.startswith("word/") or not name.endswith(".xml") or name == "word/document.xml" or name in COMMENT_MUTABLE_PARTS:
            continue
        if name.rsplit("/", 1)[-1].startswith("comments"):
            raise _unsupported("unknown comments metadata prevents safe deletion")
        other = parse_xml(data)
        if any(node.tag in _ANCHOR_TAGS and node.get(w("id")) == comment_id for node in other.iter()):
            raise _unsupported("the comment has anchors outside the main document")
    anchors = [node for node in document.iter() if node.tag in _ANCHOR_TAGS and node.get(w("id")) == comment_id]
    counts = {tag: sum(node.tag == tag for node in anchors) for tag in _ANCHOR_TAGS}
    if counts[w("commentReference")] != 1 or (counts[w("commentRangeStart")], counts[w("commentRangeEnd")]) not in ((0, 0), (1, 1)):
        raise _unsupported("the comment must have exactly one reference and at most one complete range")
    for node in anchors:
        if node.getparent() is None:
            raise _unsupported("detached comment anchor")
    root.remove(comment)
    staged[_COMMENTS] = _xml(root)
    for node in anchors:
        parent = node.getparent()
        parent.remove(node)
        if node.tag == w("commentReference") and parent.tag == w("r") and all(child.tag == w("rPr") for child in parent):
            parent.getparent().remove(parent)
    parts.update(staged)
    return result
