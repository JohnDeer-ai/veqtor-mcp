# SPDX-License-Identifier: Apache-2.0
"""Bounded, in-memory comment operations for the author drafting workflow.

These helpers deliberately do not save a package. The caller owns the source
snapshot, whole-batch transaction and collateral checks. Existing authors,
comment attributes and unrelated anchors are preserved. Native replies use
the Word comment-extension graph. Nonstandard main comment part locations and
rich comment body replacement fail closed.
Modern metadata parts are located through their package relationships.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import timezone

from lxml import etree

from ._drafting_text import annotation_date, parse_drafting_timestamp
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
from .inspect import InspectError, _internal_relationship_part

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
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
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
_MODERN_COMMENT_PARTS = {
    "extended": (
        "http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
        f"{{{_W15}}}commentsEx",
    ),
    "ids": (
        "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds",
        f"{{{_W16CID}}}commentsIds",
    ),
    "extensible": (
        "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible",
        f"{{{_W16CEX}}}commentsExtensible",
    ),
}


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


def _modern_comment_parts(parts: dict[str, bytes]) -> dict[str, str]:
    """Resolve extension identities before deleting any comment or metadata.

    Filenames are not part identities in OPC. Inspect roots as well as the
    relationships so a renamed, unbound or conflicting extension cannot hide a
    reply to the comment being deleted.
    """
    kinds = {relationship: kind for kind, (relationship, _) in _MODERN_COMMENT_PARTS.items()}
    roots = {root: kind for kind, (_, root) in _MODERN_COMMENT_PARTS.items()}
    resolved: dict[str, str] = {}
    if _RELS in parts:
        rels = parse_xml(parts[_RELS])
        if rels.tag != f"{{{_REL_NS}}}Relationships":
            raise _unsupported("unexpected document relationships root")
        seen_ids = set()
        for rel in rels:
            identity = rel.get("Id")
            if rel.tag != f"{{{_REL_NS}}}Relationship" or not identity or identity in seen_ids:
                raise _unsupported("ambiguous document relationships")
            seen_ids.add(identity)
            relationship = rel.get("Type", "")
            kind = kinds.get(relationship)
            if kind is None:
                if relationship != _COMMENT_REL and "comments" in relationship.rsplit("/", 1)[-1].lower():
                    raise _unsupported("unknown comment metadata relationship")
                continue
            if kind in resolved or rel.get("TargetMode") not in (None, "Internal"):
                raise _unsupported("ambiguous or external comment metadata relationship")
            try:
                target = _internal_relationship_part(rel.get("Target", ""))
            except ValueError as exc:
                raise _unsupported("malformed comment metadata target") from exc
            if target is None or target not in parts or target in resolved.values():
                raise _unsupported("missing or ambiguous comment metadata target")
            if parse_xml(parts[target]).tag != _MODERN_COMMENT_PARTS[kind][1]:
                raise _unsupported("comment metadata relationship targets the wrong part")
            resolved[kind] = target

    for name, payload in parts.items():
        if not name.endswith(".xml") or name in (_COMMENTS, "word/document.xml"):
            continue
        if name in COMMENT_MUTABLE_PARTS - {_COMMENTS, _RELS, _TYPES} and name not in resolved.values():
            raise _unsupported("unbound comment metadata part")
        root = parse_xml(payload)
        kind = roots.get(root.tag)
        if kind is not None and resolved.get(kind) != name:
            raise _unsupported("unbound or conflicting comment metadata part")
        if name == _TYPES:
            if root.tag != f"{{{_TYPE_NS}}}Types":
                raise _unsupported("unexpected content-types root")
            for node in root:
                content_type = node.get("ContentType", "")
                if "comments" not in content_type.lower() or content_type == _COMMENT_TYPE:
                    continue
                # An extension declared under a future content type must not be
                # silently left with references to a removed comment either.
                part_name = node.get("PartName", "")
                try:
                    target = _internal_relationship_part(part_name) if part_name.startswith("/") else None
                except ValueError as exc:
                    raise _unsupported("malformed comment metadata content type") from exc
                if target not in resolved.values():
                    raise _unsupported("unbound or unknown comment metadata content type")
    return resolved


def comment_mutable_parts(parts: dict[str, bytes]) -> frozenset[str]:
    """Return the bounded part allowlist for a validated comment deletion."""
    return COMMENT_MUTABLE_PARTS | frozenset(_modern_comment_parts(parts).values())


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
        "parent_comment_id": None,
        "thread_root_id": ident,
        "resolved": False,
    }


def read_comments(parts: dict[str, bytes], document: etree._Element) -> list[dict]:
    """Read classic/modern bodies; zero-based indexes locate main-body anchors."""
    root = _comment_root(parts)
    _require_existing_binding(parts)
    metadata = _thread_metadata(parts, root)
    indexes = _anchor_indexes(document)
    result = []
    for comment in root:
        item = _summary(comment, document, indexes)
        item.update(metadata.summaries[item["id"]])
        # Replies have no document anchors of their own. Expose the root's
        # location so protection and drafting-context checks cover the thread.
        if item["parent_comment_id"] is not None:
            item["paragraph_indexes"] = indexes.get(item["thread_root_id"], [])
        result.append(item)
    return result


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
    if container is not paragraph and (
        container.tag != w("ins") or container.getparent() is not paragraph or container.get(w("author")) != author
    ):
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
                quote: str, text: str, author: str, initials: str,
                *, timestamp: str | None = None) -> dict:
    """Anchor a new classic comment to one unique exact current-reading quote."""
    _validate_text(quote, "quote")
    _validate_text(text, "text")
    _validate_author(author)
    _validate_text(initials, "initials", allow_empty=True)
    try:
        date = annotation_date(timestamp)
    except ValueError as exc:
        raise ApplyError("invalid_comment", "timestamp must be a valid date with an explicit timezone") from exc
    if not any(item.element is paragraph for item in _body_paragraphs(document)):
        raise _unsupported("the comment target is not a canonical body paragraph")
    root = _comment_root(parts)
    _thread_metadata(parts, root)
    container, mapped, first, end = _quote_plan(paragraph, quote, author)
    staged = _relationships(parts)
    ident = _new_id(root, document)
    comment = etree.SubElement(root, w("comment"))
    comment.set(w("id"), ident)
    comment.set(w("author"), author)
    comment.set(w("initials"), initials)
    if date is not None:
        comment.set(w("date"), date)
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
    return {**next(item for item in read_comments(parts, document) if item["id"] == ident),
            "operation": "add_comment"}


def update_comment(parts: dict[str, bytes], document: etree._Element,
                   comment_id: str, text: str, author: str) -> dict:
    """Replace one's own simple comment body, preserving identity and metadata."""
    _validate_text(text, "text")
    root, comment = _owned_comment(parts, comment_id, author)
    _thread_metadata(parts, root)
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
    return {**next(item for item in read_comments(parts, document) if item["id"] == comment_id),
            "operation": "update_comment"}


def _hex_identity(node: etree._Element, attribute: str, *, optional: bool = False) -> str | None:
    """Compare ST_LongHexNumber values semantically, without rewriting XML."""
    value = node.get(attribute)
    if optional and value is None:
        return None
    if value is None or re.fullmatch(r"[0-9A-Fa-f]{8}", value) is None:
        raise _unsupported("modern comment identifiers must contain exactly eight hexadecimal digits")
    return value.upper()


def _on_off(value: str | None) -> bool:
    if value in (None, "0", "false", "off"):
        return False
    if value in ("1", "true", "on"):
        return True
    raise _unsupported("invalid modern comment resolution or follow-up flag")


@dataclass
class _ThreadMetadata:
    paths: dict[str, str]
    trees: dict[str, etree._Element]
    last_paragraphs: dict[str, etree._Element]
    extended: dict[str, etree._Element]
    identities: dict[str, etree._Element]
    extensible: dict[str, etree._Element]
    summaries: dict[str, dict]


def _thread_metadata(parts: dict[str, bytes], comments: etree._Element) -> _ThreadMetadata:
    """Interpret MS-DOCX 2.5.3.1/2.8.3.1 using LAST-paragraph identities.

    Do not silently turn malformed, cyclic or unresolvable reply metadata into
    independent comments. An absent commentEx entry in an existing extension
    has unknown resolution state, whereas done defaults to false on an entry.
    Unreferenced legacy metadata is retained; it cannot establish a parent.
    """
    paths = _modern_comment_parts(parts)
    trees = {kind: parse_xml(parts[path]) for kind, path in paths.items()}
    last_paragraphs = {}
    para_comments = {}
    all_paragraph_ids = set()
    for comment in comments:
        ident = comment.get(w("id"))
        paragraphs = list(comment.iter(w("p")))
        if paragraphs:
            last_paragraphs[ident] = paragraphs[-1]
        for paragraph in paragraphs:
            para_id = _hex_identity(paragraph, f"{{{_W14}}}paraId", optional=True)
            if para_id is not None:
                if para_id in all_paragraph_ids:
                    raise _unsupported("multiple comment paragraphs share a modern identifier")
                all_paragraph_ids.add(para_id)
                if paragraph is paragraphs[-1]:
                    para_comments[para_id] = ident

    extended, identities, extensible = {}, {}, {}
    durable_ids = set()
    for kind, tree in trees.items():
        namespace, child_name, key_attribute = {
            "extended": (_W15, "commentEx", "paraId"),
            "ids": (_W16CID, "commentId", "paraId"),
            "extensible": (_W16CEX, "commentExtensible", "durableId"),
        }[kind]
        mapping = {"extended": extended, "ids": identities, "extensible": extensible}[kind]
        for node in tree:
            if node.tag != f"{{{namespace}}}{child_name}":
                raise _unsupported("unsupported modern comment metadata element")
            identity = _hex_identity(node, f"{{{namespace}}}{key_attribute}")
            if identity in mapping:
                raise _unsupported("duplicate modern comment metadata identifier")
            mapping[identity] = node
            if kind == "extended":
                if len(node):
                    raise _unsupported("unsupported extended comment metadata children")
                _on_off(node.get(f"{{{_W15}}}done"))
                _hex_identity(node, f"{{{_W15}}}paraIdParent", optional=True)
                if identity in all_paragraph_ids and identity not in para_comments:
                    raise _unsupported("comment metadata must identify the last paragraph")
            elif kind == "ids":
                if len(node):
                    raise _unsupported("unsupported comment identity metadata children")
                durable = _hex_identity(node, f"{{{_W16CID}}}durableId")
                if durable in durable_ids:
                    raise _unsupported("duplicate durable comment identifiers")
                durable_ids.add(durable)
            else:
                if any(child.tag != f"{{{_W16CEX}}}extLst" for child in node):
                    raise _unsupported("unknown extensible comment metadata")
                _on_off(node.get(f"{{{_W16CEX}}}intelligentPlaceholder"))

    parents = {}
    own_done = {}
    for comment in comments:
        ident = comment.get(w("id"))
        paragraph = last_paragraphs.get(ident)
        para_id = (_hex_identity(paragraph, f"{{{_W14}}}paraId", optional=True)
                   if paragraph is not None else None)
        entry = extended.get(para_id)
        parent = None
        if entry is not None:
            parent_para_id = _hex_identity(entry, f"{{{_W15}}}paraIdParent", optional=True)
            if parent_para_id is not None:
                parent = para_comments.get(parent_para_id)
                if parent is None or parent == ident or parent_para_id not in extended:
                    raise _unsupported("comment reply parent is missing or ambiguous")
            own_done[ident] = _on_off(entry.get(f"{{{_W15}}}done"))
        else:
            own_done[ident] = None if "extended" in trees else False
        parents[ident] = parent

    # Orphan reply records cannot be treated as harmless metadata: deletion or
    # a new reply must not conceal a thread whose body is absent.
    for para_id, entry in extended.items():
        if (entry.get(f"{{{_W15}}}paraIdParent") is not None
                and para_id not in para_comments):
            raise _unsupported("reply metadata has no corresponding comment body")
    summaries = {}
    for ident, parent in parents.items():
        current, seen = ident, set()
        while parents[current] is not None:
            if current in seen:
                raise _unsupported("cyclic comment reply metadata")
            seen.add(current)
            current = parents[current]
        summaries[ident] = {
            "parent_comment_id": parent,
            "thread_root_id": current,
            "resolved": own_done[current],
        }
    return _ThreadMetadata(paths, trees, last_paragraphs, extended, identities, extensible, summaries)


def _thread_target(parts: dict[str, bytes], comment_id: str, author: str):
    _validate_author(author)
    if not isinstance(comment_id, str):
        raise ApplyError("invalid_comment", "comment_id must be a string")
    comments = _comment_root(parts)
    _require_existing_binding(parts)
    target = next((node for node in comments if node.get(w("id")) == comment_id), None)
    if target is None:
        raise ApplyError("comment_not_found", "the requested comment does not exist", comment_id=comment_id)
    metadata = _thread_metadata(parts, comments)
    root_id = metadata.summaries[comment_id]["thread_root_id"]
    types = parse_xml(parts[_TYPES])
    for kind, path in metadata.paths.items():
        names = {"extended": "commentsExtended", "ids": "commentsIds", "extensible": "commentsExtensible"}
        expected = f"application/vnd.openxmlformats-officedocument.wordprocessingml.{names[kind]}+xml"
        declarations = []
        for node in types:
            name = node.get("PartName", "")
            if not name.startswith("/"):
                continue
            try:
                target_path = _internal_relationship_part(name)
            except ValueError as exc:
                raise _unsupported("invalid modern comment content-type target") from exc
            if target_path == path:
                declarations.append(node)
        if len(declarations) != 1 or declarations[0].get("ContentType") != expected:
            raise _unsupported("modern comment part requires its unambiguous content type")
    # An extension list can contain mentions, tasks or later schema data whose
    # linkage is not supported here. Preserve it by refusing the entire write.
    for entry in metadata.extensible.values():
        if len(entry) or _on_off(entry.get(f"{{{_W16CEX}}}intelligentPlaceholder")):
            raise _unsupported("comment tasks and extensible payloads are not supported for thread writes")
    if metadata.extensible and not metadata.identities:
        raise _unsupported("extensible comments require durable comment identities")
    for identity in metadata.identities.values():
        durable = _hex_identity(identity, f"{{{_W16CID}}}durableId")
        if not 0 < int(durable, 16) < _MAX_ID:
            raise _unsupported("durable comment identifiers are outside the Word range")
    return comments, target, metadata, root_id


def _next_hex_identity(used: set[str]) -> str:
    value = 1
    while f"{value:08X}" in used:
        value += 1
    if value >= _MAX_ID:
        raise _unsupported("modern comment identifier space is exhausted")
    result = f"{value:08X}"
    used.add(result)
    return result


def _paragraph_id_pool(parts: dict[str, bytes], document: etree._Element) -> set[str]:
    used = set()
    for name, payload in parts.items():
        if not name.endswith(".xml") or name == "word/document.xml":
            continue
        for node in parse_xml(payload).iter():
            for namespace in (_W14, _W15, _W16CID):
                value = _hex_identity(node, f"{{{namespace}}}paraId", optional=True)
                if value is not None:
                    used.add(value)
    for node in document.iter():
        value = _hex_identity(node, f"{{{_W14}}}paraId", optional=True)
        if value is not None:
            used.add(value)
    return used


def _set_paragraph_identity(paragraph: etree._Element, value: str) -> None:
    paragraph.set(f"{{{_W14}}}paraId", value)
    # Mark the new extension attribute ignorable for older consumers without
    # changing root namespace declarations or any foreign comment attributes.
    prefix = next(key for key, uri in paragraph.nsmap.items() if key and uri == _W14)
    ignorable = paragraph.get(f"{{{_MC}}}Ignorable", "").split()
    if prefix not in ignorable:
        ignorable.append(prefix)
        paragraph.set(f"{{{_MC}}}Ignorable", " ".join(ignorable))


def _create_extended_part(parts: dict[str, bytes], metadata: _ThreadMetadata,
                          staged: dict[str, bytes]) -> None:
    if "extended" in metadata.trees:
        return
    path = "word/commentsExtended.xml"
    if path in parts:
        raise _unsupported("conflicting comment extension part")
    metadata.paths["extended"] = path
    metadata.trees["extended"] = etree.Element(f"{{{_W15}}}commentsEx", nsmap={"w15": _W15})
    rels = parse_xml(parts[_RELS])
    used = {node.get("Id") for node in rels}
    number = 1
    while f"rId{number}" in used:
        number += 1
    etree.SubElement(rels, f"{{{_REL_NS}}}Relationship",
                     Id=f"rId{number}", Type=_MODERN_COMMENT_PARTS["extended"][0],
                     Target="commentsExtended.xml")
    types = parse_xml(parts[_TYPES])
    if any(node.get("PartName") == f"/{path}" for node in types):
        raise _unsupported("conflicting comment extension content type")
    etree.SubElement(types, f"{{{_TYPE_NS}}}Override", PartName=f"/{path}",
                     ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml")
    staged[_RELS], staged[_TYPES] = _xml(rels), _xml(types)


def _ensure_thread_entry(parts: dict[str, bytes], metadata: _ThreadMetadata, comment_id: str,
                         staged: dict[str, bytes], used: set[str]) -> tuple[str, etree._Element]:
    paragraph = metadata.last_paragraphs.get(comment_id)
    if paragraph is None:
        raise _unsupported("thread comments require a final paragraph")
    para_id = _hex_identity(paragraph, f"{{{_W14}}}paraId", optional=True)
    if para_id is None:
        para_id = _next_hex_identity(used)
        _set_paragraph_identity(paragraph, para_id)
    _create_extended_part(parts, metadata, staged)
    entry = metadata.extended.get(para_id)
    if entry is None:
        entry = etree.SubElement(metadata.trees["extended"], f"{{{_W15}}}commentEx")
        entry.set(f"{{{_W15}}}paraId", para_id)
        entry.set(f"{{{_W15}}}done", "0")
        metadata.extended[para_id] = entry
    return para_id, entry


def _reply_anchor_plan(parts: dict[str, bytes], document: etree._Element,
                       root_id: str, reply_id: str, author: str) -> list[tuple[etree._Element, etree._Element]]:
    """Mirror the root quote: Word drops reply bodies without their own reference."""
    anchors = [node for node in document.iter()
               if node.tag in _ANCHOR_TAGS and node.get(w("id")) == root_id]
    counts = {tag: sum(node.tag == tag for node in anchors) for tag in _ANCHOR_TAGS}
    if counts[w("commentReference")] != 1 or (
        counts[w("commentRangeStart")], counts[w("commentRangeEnd")]
    ) not in ((0, 0), (1, 1)):
        raise _unsupported("reply root requires one reference and at most one complete range")
    if counts[w("commentRangeStart")] and [node.tag for node in anchors] != [
        w("commentRangeStart"), w("commentRangeEnd"), w("commentReference")
    ]:
        raise _unsupported("reply root range and reference are out of order")
    paragraphs = {item.element for item in _body_paragraphs(document)}

    def supported(container: etree._Element | None) -> bool:
        return container in paragraphs or (
            container is not None and container.tag == w("ins")
            and container.getparent() in paragraphs and container.get(w("author")) == author
        )

    for name, payload in parts.items():
        if not name.endswith(".xml") or name in (_COMMENTS, "word/document.xml"):
            continue
        if any(node.tag in _ANCHOR_TAGS and node.get(w("id")) == root_id
               for node in parse_xml(payload).iter()):
            raise _unsupported("reply root has anchors outside the main document")
    plan = []
    for anchor in anchors:
        replacement = deepcopy(anchor)
        replacement.set(w("id"), reply_id)
        if anchor.tag == w("commentReference"):
            run = anchor.getparent()
            if run is None or run.tag != w("r") or not supported(run.getparent()):
                raise _unsupported("reply root reference must be in plain text or one own pending insertion")
            new_run = etree.Element(w("r"))
            properties = run.find(w("rPr"))
            if properties is not None:
                if any(node.tag == w("rPrChange") for node in properties.iter()):
                    raise _unsupported("tracked comment-reference formatting cannot be cloned")
                new_run.append(deepcopy(properties))
            new_run.append(replacement)
            plan.append((run, new_run))
        else:
            if not supported(anchor.getparent()):
                raise _unsupported("reply root range must be in plain text or one own pending insertion")
            plan.append((anchor, replacement))
    return plan


def _apply_reply_anchors(plan: list[tuple[etree._Element, etree._Element]]) -> None:
    for anchor, replacement in plan:
        parent = anchor.getparent()
        parent.insert(parent.index(anchor) + 1, replacement)


def reply_comment(parts: dict[str, bytes], document: etree._Element,
                  comment_id: str, text: str, author: str, initials: str,
                  *, timestamp: str | None = None) -> dict:
    """Append a native Word reply, including to another reviewer's comment.

    A reply to a reply stays in that existing root thread (Word's flat reply
    presentation). Its own document anchors cover the root's quotation, as
    Word otherwise drops the unreferenced reply when saving. MS-DOCX 2.5.3.1
    links commentEx.paraIdParent to the root comment's last paragraph.
    """
    _validate_text(text, "text")
    _validate_text(initials, "initials", allow_empty=True)
    try:
        date = annotation_date(timestamp)
        utc_date = (parse_drafting_timestamp(timestamp).astimezone(timezone.utc)
                    .isoformat(timespec="seconds").replace("+00:00", "Z")) if timestamp is not None else None
    except ValueError as exc:
        raise ApplyError("invalid_comment", "timestamp must be a valid date with an explicit timezone") from exc
    comments, _, metadata, root_id = _thread_target(parts, comment_id, author)
    if metadata.summaries[root_id]["resolved"] is True:
        raise ApplyError("comment_thread_resolved", "reopen the resolved comment thread before replying", comment_id=root_id)
    staged = {}
    used = _paragraph_id_pool(parts, document)
    parent_para_id, _ = _ensure_thread_entry(parts, metadata, root_id, staged, used)
    ident = _new_id(comments, document)
    anchor_plan = _reply_anchor_plan(parts, document, root_id, ident, author)
    comment = etree.SubElement(comments, w("comment"))
    comment.set(w("id"), ident)
    comment.set(w("author"), author)
    comment.set(w("initials"), initials)
    if date is not None:
        comment.set(w("date"), date)
    paragraph = _comment_paragraph(text)
    para_id = _next_hex_identity(used)
    comment.append(paragraph)
    # Resolve mc:Ignorable against the final parent namespaces. Attaching a
    # detached paragraph can remap a generated ns1 attribute prefix to w14;
    # lxml cannot also rewrite the lexical prefix in mc:Ignorable.
    _set_paragraph_identity(paragraph, para_id)
    entry = etree.SubElement(metadata.trees["extended"], f"{{{_W15}}}commentEx")
    entry.set(f"{{{_W15}}}paraId", para_id)
    entry.set(f"{{{_W15}}}paraIdParent", parent_para_id)
    entry.set(f"{{{_W15}}}done", "0")
    if "ids" in metadata.trees:
        durable_used = {_hex_identity(node, f"{{{_W16CID}}}durableId")
                        for node in metadata.identities.values()} | set(metadata.extensible)
        durable = _next_hex_identity(durable_used)
        identity = etree.SubElement(metadata.trees["ids"], f"{{{_W16CID}}}commentId")
        identity.set(f"{{{_W16CID}}}paraId", para_id)
        identity.set(f"{{{_W16CID}}}durableId", durable)
        staged[metadata.paths["ids"]] = _xml(metadata.trees["ids"])
        if "extensible" in metadata.trees:
            extra = etree.SubElement(metadata.trees["extensible"], f"{{{_W16CEX}}}commentExtensible")
            extra.set(f"{{{_W16CEX}}}durableId", durable)
            if utc_date is not None:
                extra.set(f"{{{_W16CEX}}}dateUtc", utc_date)
            staged[metadata.paths["extensible"]] = _xml(metadata.trees["extensible"])
    staged[_COMMENTS] = _xml(comments)
    staged[metadata.paths["extended"]] = _xml(metadata.trees["extended"])
    candidate = {**parts, **staged}
    checked_document = deepcopy(document)
    _apply_reply_anchors(_reply_anchor_plan(parts, checked_document, root_id, ident, author))
    result = next(item for item in read_comments(candidate, checked_document) if item["id"] == ident)
    _apply_reply_anchors(anchor_plan)
    parts.update(staged)
    return {**result, "operation": "reply_comment"}


def set_comment_resolved(parts: dict[str, bytes], document: etree._Element,
                         comment_id: str, resolved: bool, author: str) -> dict:
    """Set the root thread's native Word done flag without rewriting authors."""
    if type(resolved) is not bool:
        raise ApplyError("invalid_comment", "resolved must be a boolean")
    comments, _, metadata, root_id = _thread_target(parts, comment_id, author)
    staged = {}
    used = _paragraph_id_pool(parts, document)
    _, entry = _ensure_thread_entry(parts, metadata, root_id, staged, used)
    entry.set(f"{{{_W15}}}done", "1" if resolved else "0")
    serialized_comments = _xml(comments)
    # Avoid rewriting comments.xml when it already has a usable paragraph ID.
    if etree.tostring(parse_xml(parts[_COMMENTS])) != etree.tostring(comments):
        staged[_COMMENTS] = serialized_comments
    staged[metadata.paths["extended"]] = _xml(metadata.trees["extended"])
    candidate = {**parts, **staged}
    result = next(item for item in read_comments(candidate, document) if item["id"] == root_id)
    parts.update(staged)
    return {**result, "operation": "set_comment_resolved"}


def _metadata_for_delete(parts: dict[str, bytes], comment: etree._Element) -> dict[str, bytes]:
    metadata_parts = _modern_comment_parts(parts)
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
    extended = metadata_parts.get("extended")
    if extended is not None:
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
    ids_part = metadata_parts.get("ids")
    durable_ids = set()
    if ids_part is not None:
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
    extensible = metadata_parts.get("extensible")
    if extensible is not None:
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
    metadata = _thread_metadata(parts, root)
    result = {**_summary(comment, document), **metadata.summaries[comment_id],
              "operation": "delete_comment"}
    staged = _metadata_for_delete(parts, comment)
    mutable_parts = comment_mutable_parts(parts)
    # Do not orphan anchors in headers/footnotes or unknown modern extensions.
    for name, data in parts.items():
        if not name.startswith("word/") or not name.endswith(".xml") or name == "word/document.xml" or name in mutable_parts:
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


def _delete_selected_messages(parts: dict[str, bytes], document: etree._Element,
                              comments: etree._Element, metadata: _ThreadMetadata,
                              selected: set[str], author: str) -> list[dict]:
    """Validate the complete graph and all anchors before publishing a deletion."""
    if not 1 <= len(selected) <= 100:
        raise ApplyError("resource_limit_exceeded", "select at most 100 comment messages")
    chosen = [node for node in comments if node.get(w("id")) in selected]
    if len(chosen) != len(selected):
        raise ApplyError("comment_not_found", "a selected comment message is missing")
    if any(node.get(w("author")) != author for node in chosen):
        raise ApplyError("comment_author_mismatch", "every deleted message must belong to the selected own profile")
    if any(info["parent_comment_id"] in selected and ident not in selected
           for ident, info in metadata.summaries.items()):
        raise _unsupported("a selected message has an unselected dependent reply")
    # The first deletion domain is ordinary comment text. Do not discard a
    # drawing, field, attachment or future body extension whose links are unknown.
    from ._paragraph_edits import _require_properties

    run_children = {w(tag) for tag in ("rPr", "t", "tab", "br", "cr", "annotationRef")}
    for comment in chosen:
        if not len(comment) or any(p.tag != w("p") for p in comment):
            raise _unsupported("message deletion requires plain comment paragraphs")
        for paragraph in comment:
            if any(child.tag not in {w("pPr"), w("r")} for child in paragraph):
                raise _unsupported("unsupported selected comment body")
            for run in paragraph.findall(w("r")):
                if any(child.tag not in run_children for child in run):
                    raise _unsupported("unsupported selected comment run")
            if any(not isinstance(n.tag, str) or etree.QName(n).localname.endswith("Change") for n in paragraph.iter()):
                raise _unsupported("tracked comment formatting is unsupported")
            for properties in paragraph.iter(w("pPr"), w("rPr")):
                try:
                    _require_properties(properties)
                except InspectError as exc:
                    raise _unsupported("unsupported selected comment properties") from exc
    protected_ids = selected | {metadata.summaries[ident]["thread_root_id"] for ident in selected}
    mutable = comment_mutable_parts(parts)
    for name, data in parts.items():
        if not name.endswith(".xml") or name == "word/document.xml" or name in mutable:
            continue
        if name.rsplit("/", 1)[-1].startswith("comments"):
            raise _unsupported("unknown comment metadata prevents safe deletion")
        if any(n.tag in _ANCHOR_TAGS and n.get(w("id")) in protected_ids
               for n in parse_xml(data).iter()):
            raise _unsupported("selected comment anchors exist outside the main document")
    if any(n.tag in _ANCHOR_TAGS and n.get(w("id")) in protected_ids for n in comments.iter()):
        raise _unsupported("selected comment has anchors inside comment bodies")
    ordered = list(document.iter())
    positions = {node: index for index, node in enumerate(ordered)}
    quote_tags = {w(tag) for tag in ("t", "delText", "tab", "br", "cr", "instrText", "delInstrText")}
    body_paragraphs = {item.element for item in _body_paragraphs(document)}

    def supported_container(container):
        return container in body_paragraphs or (
            container is not None and container.tag == w("ins")
            and container.getparent() in body_paragraphs and container.get(w("author")) == author
        )

    def checked_anchors(ident, *, allow_absent=False):
        current = [n for n in ordered if n.tag in _ANCHOR_TAGS and n.get(w("id")) == ident]
        if not current and allow_absent:
            return [], None
        counts = {tag: sum(n.tag == tag for n in current) for tag in _ANCHOR_TAGS}
        if (counts[w("commentReference")] != 1
                or (counts[w("commentRangeStart")], counts[w("commentRangeEnd")]) not in ((0, 0), (1, 1))):
            raise _unsupported("comment requires one reference and a complete optional range")
        for node in current:
            container = node.getparent()
            if len(node):
                raise _unsupported("comment anchors must be empty markers")
            if node.tag == w("commentReference"):
                if container is None or container.tag != w("r"):
                    raise _unsupported("comment reference requires an ordinary run")
                for properties in container.findall(w("rPr")):
                    try:
                        _require_properties(properties)
                    except InspectError as exc:
                        raise _unsupported("unsupported comment-reference properties") from exc
                container = container.getparent()
            if not supported_container(container):
                raise _unsupported("comment anchors require plain text or one own insertion")
        quote = None
        if counts[w("commentRangeStart")]:
            indexes = {n.tag: positions[n] for n in current}
            if not indexes[w("commentRangeStart")] < indexes[w("commentRangeEnd")] < indexes[w("commentReference")]:
                raise _unsupported("selected comment anchors are out of order")
            quote = tuple(n for n in ordered[indexes[w("commentRangeStart")]:indexes[w("commentRangeEnd")]] if n.tag in quote_tags)
        else:
            reference = next(n for n in current if n.tag == w("commentReference"))
            paragraph = next((p for p in reference.iterancestors(w("p"))), None)
            if paragraph is None:
                raise _unsupported("point comment reference has no paragraph")
            # Two point comments need the same physical text position, even
            # when both have no quoted range. Equal text elsewhere is not enough.
            quote = (paragraph, tuple(n for n in ordered[positions[paragraph]:positions[reference]]
                                      if n.tag in quote_tags))
        return current, quote

    anchors = []
    for ident in selected:
        reply = metadata.summaries[ident]["parent_comment_id"] is not None
        current, quote = checked_anchors(ident, allow_absent=reply)
        if reply:
            _, root_quote = checked_anchors(metadata.summaries[ident]["thread_root_id"])
            if current and quote != root_quote:
                raise _unsupported("reply anchors do not cover the root's exact quotation")
        anchors.extend(current)
    summaries = {row["id"]: row for row in read_comments(parts, document)}
    result = [{**summaries[n.get(w("id"))], "date": n.get(w("date"))} for n in chosen]
    if any(len(row["text"]) > _MAX_TEXT for row in result):
        raise ApplyError("resource_limit_exceeded", "selected comment text exceeds the preview limit")
    paragraphs = {_hex_identity(p, f"{{{_W14}}}paraId", optional=True)
                  for n in chosen for p in n.iter(w("p"))}
    paragraphs.discard(None)
    durables = {_hex_identity(n, f"{{{_W16CID}}}durableId")
                for para, n in metadata.identities.items() if para in paragraphs}
    staged = {}
    for kind, tree in metadata.trees.items():
        clone = deepcopy(tree)
        namespace, key, removed = {
            "extended": (_W15, "paraId", paragraphs),
            "ids": (_W16CID, "paraId", paragraphs),
            "extensible": (_W16CEX, "durableId", durables),
        }[kind]
        deleted = [n for n in clone if _hex_identity(n, f"{{{namespace}}}{key}") in removed]
        for node in deleted:
            clone.remove(node)
        if deleted:
            staged[metadata.paths[kind]] = _xml(clone)
    remaining = deepcopy(comments)
    for node in list(remaining):
        if node.get(w("id")) in selected:
            remaining.remove(node)
    staged[_COMMENTS] = _xml(remaining)
    # Prove that no parent or durable link is orphaned before changing the DOM.
    _thread_metadata({**parts, **staged}, remaining)
    for node in anchors:
        parent = node.getparent()
        parent.remove(node)
        if node.tag == w("commentReference") and parent.tag == w("r") and all(c.tag == w("rPr") for c in parent):
            parent.getparent().remove(parent)
    parts.update(staged)
    return result


def delete_comment_reply(parts: dict[str, bytes], document: etree._Element,
                         comment_id: str, author: str) -> dict:
    """Remove only an own reply with no dependent messages."""
    comments, _, metadata, _ = _thread_target(parts, comment_id, author)
    if metadata.summaries[comment_id]["parent_comment_id"] is None:
        raise _unsupported("select a reply, not a root comment")
    deleted = _delete_selected_messages(parts, document, comments, metadata, {comment_id}, author)[0]
    return {**deleted, "operation": "delete_comment_reply"}


def delete_comment_thread(parts: dict[str, bytes], document: etree._Element,
                          comment_id: str, member_comment_ids: list[str], author: str) -> dict:
    """Remove an explicitly enumerated thread owned entirely by one profile."""
    if (not isinstance(member_comment_ids, list) or not 1 <= len(member_comment_ids) <= 100
            or any(not isinstance(ident, str) for ident in member_comment_ids)
            or len(set(member_comment_ids)) != len(member_comment_ids)):
        raise ApplyError("invalid_comment", "select a unique bounded list of thread message IDs")
    comments, _, metadata, root_id = _thread_target(parts, comment_id, author)
    if root_id != comment_id:
        raise _unsupported("thread deletion must explicitly select its root")
    expected = {ident for ident, info in metadata.summaries.items() if info["thread_root_id"] == root_id}
    if set(member_comment_ids) != expected:
        raise ApplyError("comment_thread_selection_mismatch", "select every current thread message explicitly")
    deleted = _delete_selected_messages(parts, document, comments, metadata, expected, author)
    return {"operation": "delete_comment_thread", "thread_root_id": root_id,
            "deleted_comments": deleted}
