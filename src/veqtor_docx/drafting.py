# SPDX-License-Identifier: Apache-2.0
"""Sequential author drafting, separate from the immutable counterproposal API.

Every call is bound to one source snapshot. Operations in a batch run in order;
paragraph references address the original snapshot and quotes address the current
reading at that step. Publication requires a matching dry-run proof, and replacing
the working file always retains its previous bytes in a durable backup.
"""

from __future__ import annotations

import copy
import hashlib
import os
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from lxml import etree

from ._drafting_comments import (
    add_comment,
    comment_mutable_parts,
    delete_comment,
    read_comments,
    reply_comment,
    set_comment_resolved,
    update_comment,
)
from ._drafting_paragraphs import insert_paragraph, replace_paragraph
from ._drafting_io import publish_document
from ._drafting_text import annotation_date, parse_drafting_timestamp, replace_text
from ._ooxml import (
    DOCUMENT_PART,
    UserPathError,
    canonical_body_flow_v1,
    is_xml_text_compatible,
    parse_xml,
    read_docx_payload,
    resolve_user_path,
    tracked_change_author_validation_error,
    validate_docx_payload_size,
    w,
)
from ._paragraph_edits import _REVISION_TAGS, validate_paragraph_target
from .apply import (
    DEFAULT_AUTHOR,
    ApplyError,
    _canonical_digest,
    _collateral_outside,
    _output_archive_bytes,
    _paragraph_segments,
    _read_source_archive,
    _reading_text,
    _revision_id_allocator,
)
from .extract import DocxError, _extract_validated
from .inspect import _resolve_paragraph, _snapshot_from_validated

SCHEMA_VERSION = "document_drafting.v1"
PROOF_VERSION = "drafting_preflight.v1"
MAX_OPERATIONS = 100
MAX_TEXT_CHARS = 20_000
MAX_BATCH_CHARS = 200_000
_COMMENT_PARTS = {
    "word/comments.xml",
    "word/commentsExtended.xml",
    "word/commentsIds.xml",
    "word/commentsExtensible.xml",
    "word/people.xml",
    "word/_rels/document.xml.rels",
    "[Content_Types].xml",
}
_SETTINGS_PART = "word/settings.xml"
_RELS_PART = "word/_rels/document.xml.rels"
_CT_PART = "[Content_Types].xml"
_R = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_SETTINGS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"
)
_CT_SETTINGS = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
)
# CT_Settings is an ordered sequence (ISO/IEC 29500-1, A.1). These are the
# settings before trackRevisions; existing elements retain their relative order.
_TRACKING_PREDECESSORS = frozenset(
    w(name)
    for name in (
        "writeProtection", "view", "zoom", "removePersonalInformation",
        "removeDateAndTime", "doNotDisplayPageBoundaries", "displayBackgroundShape",
        "printPostScriptOverText", "printFractionalCharacterWidth", "printFormsData",
        "embedTrueTypeFonts", "embedSystemFonts", "saveSubsetFonts", "saveFormsData",
        "mirrorMargins", "alignBordersAndEdges", "bordersDoNotSurroundHeader",
        "bordersDoNotSurroundFooter", "gutterAtTop", "hideSpellingErrors",
        "hideGrammaticalErrors", "activeWritingStyle", "proofState", "formsDesign",
        "attachedTemplate", "linkStyles", "stylePaneFormatFilter", "stylePaneSortMethod",
        "documentType", "mailMerge", "revisionView",
    )
)
_OP_KEYS = {
    "replace_text": {"kind", "paragraph_ref", "old_text", "new_text"},
    "replace_paragraph": {"kind", "paragraph_ref", "old_text", "new_text"},
    "insert_paragraph": {"kind", "paragraph_ref", "position", "text"},
    "add_comment": {"kind", "paragraph_ref", "quote", "text"},
    "update_comment": {"kind", "comment_id", "text"},
    "delete_comment": {"kind", "comment_id"},
    "reply_comment": {"kind", "comment_id", "text"},
    "set_comment_resolved": {"kind", "comment_id", "resolved"},
}


def _path(value: str) -> str:
    try:
        return os.path.abspath(resolve_user_path(value))
    except UserPathError as exc:
        raise ApplyError(exc.code, exc.detail) from exc


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _new_timestamp() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _proof_timestamp(proof: object) -> str:
    value = proof.get("timestamp") if isinstance(proof, dict) else None
    try:
        parse_drafting_timestamp(value)
    except ValueError as exc:
        raise ApplyError("preflight_binding_mismatch", "preflight timestamp is invalid") from exc
    return value


def _xml(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _profile(author: str, initials: str) -> None:
    if error := tracked_change_author_validation_error(author):
        raise ApplyError("invalid_author", error)
    if (
        not isinstance(initials, str)
        or not initials.strip()
        or len(initials) > 32
        or not is_xml_text_compatible(initials)
    ):
        raise ApplyError(
            "invalid_initials",
            "comment initials must be 1-32 XML-compatible characters",
        )


def _validate_operations(operations: object) -> list[dict]:
    if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_OPERATIONS:
        raise ApplyError("invalid_edit", "supply 1-100 document operations")
    total = 0
    for op in operations:
        if not isinstance(op, dict) or not isinstance(op.get("kind"), str):
            raise ApplyError("invalid_edit", "operation must have a kind")
        keys = _OP_KEYS.get(op["kind"])
        optional = {"mode"} if op["kind"] == "replace_paragraph" else set()
        if keys is None or not keys <= set(op) or set(op) - keys - optional:
            raise ApplyError(
                "invalid_edit", "operation fields do not match the drafting contract"
            )
        if "paragraph_ref" in op:
            validate_paragraph_target(
                {"kind": "paragraph", "paragraph_ref": op["paragraph_ref"]}
            )
        if "mode" in op and op["mode"] not in ("minimal", "whole"):
            raise ApplyError("invalid_edit", "mode must be minimal or whole")
        for key in keys - {"kind", "paragraph_ref"}:
            value = op[key]
            if key == "resolved":
                if type(value) is not bool:
                    raise ApplyError("invalid_edit", "resolved must be a boolean")
                continue
            if key == "position":
                if value not in ("before", "after"):
                    raise ApplyError("invalid_edit", "position must be before or after")
                continue
            if (
                not isinstance(value, str)
                or len(value) > MAX_TEXT_CHARS
                or not is_xml_text_compatible(value)
                or (
                    key != "new_text" and not value
                    and not (op["kind"] == "replace_paragraph" and key == "old_text")
                )
            ):
                raise ApplyError(
                    "invalid_edit", "operation text is empty, invalid or too long"
                )
            total += len(value)
        if op["kind"] == "replace_text" and op["old_text"] == op["new_text"]:
            raise ApplyError(
                "no_change", "replacement must differ from the original text"
            )
    if total > MAX_BATCH_CHARS:
        raise ApplyError(
            "resource_limit_exceeded", "drafting batch text limit exceeded"
        )
    return operations


def _settings(parts: dict[str, bytes]) -> tuple:
    """Resolve the actual settings part before either text or comment writes."""
    rels = (
        parse_xml(parts[_RELS_PART])
        if _RELS_PART in parts
        else etree.Element(f"{{{_R}}}Relationships", nsmap={None: _R})
    )
    matching = [rel for rel in rels if rel.get("Type") == _REL_SETTINGS]
    if len(matching) > 1 or (
        matching
        and (
            matching[0].get("Target") not in {"settings.xml", "/word/settings.xml"}
            or matching[0].get("TargetMode", "Internal") != "Internal"
        )
    ):
        raise ApplyError("unsupported_settings", "nonstandard settings relationship")
    if _SETTINGS_PART in parts and not matching:
        raise ApplyError("unsupported_settings", "orphan settings part")
    if matching and _SETTINGS_PART not in parts:
        raise ApplyError("unsupported_settings", "missing settings part")
    settings = (
        parse_xml(parts[_SETTINGS_PART])
        if _SETTINGS_PART in parts
        else etree.Element(w("settings"), nsmap={"w": w("settings").split("}")[0][1:]})
    )
    if settings.tag != w("settings"):
        raise ApplyError("unsupported_settings", "invalid settings root")
    return rels, matching, settings


def _require_unprotected(parts: dict[str, bytes]) -> None:
    _, _, settings = _settings(parts)
    if settings.find(w("writeProtection")) is not None:
        raise ApplyError("document_protected", "the document declares write protection")
    protections = settings.findall(w("documentProtection"))
    if len(protections) > 1 or any(
        node.get(w("enforcement"), "false") not in {"0", "false", "off"}
        for node in protections
    ):
        raise ApplyError(
            "document_protected", "the document enforces editing restrictions"
        )


def _enable_tracking(parts: dict[str, bytes]) -> None:
    """Enable subsequent native Word edits without changing revision identities."""
    rels, matching, settings = _settings(parts)
    tracks = settings.findall(w("trackRevisions"))
    if len(tracks) > 1:
        raise ApplyError("unsupported_settings", "duplicate tracking setting")
    if tracks and tracks[0].get(w("val"), "true") in {"1", "true", "on"}:
        return
    if tracks:
        tracks[0].set(w("val"), "true")
    else:
        index = max(
            (
                index + 1
                for index, child in enumerate(settings)
                if child.tag in _TRACKING_PREDECESSORS
            ),
            default=0,
        )
        settings.insert(index, etree.Element(w("trackRevisions")))
    parts[_SETTINGS_PART] = _xml(settings)
    if not matching:
        identities = {rel.get("Id") for rel in rels}
        index = 1
        while f"rIdVeqtorSettings{index}" in identities:
            index += 1
        etree.SubElement(
            rels,
            f"{{{_R}}}Relationship",
            Id=f"rIdVeqtorSettings{index}",
            Type=_REL_SETTINGS,
            Target="settings.xml",
        )
        parts[_RELS_PART] = _xml(rels)
        content_types = parse_xml(parts[_CT_PART])
        matches = [
            node
            for node in content_types
            if node.get("PartName") == "/word/settings.xml"
        ]
        if matches:
            if len(matches) != 1 or matches[0].get("ContentType") != _CT_SETTINGS:
                raise ApplyError(
                    "unsupported_settings", "invalid settings content type"
                )
        else:
            etree.SubElement(
                content_types,
                f"{{{_CT}}}Override",
                PartName="/word/settings.xml",
                ContentType=_CT_SETTINGS,
            )
            parts[_CT_PART] = _xml(content_types)


def _has_revision(root: etree._Element) -> bool:
    return any(
        node.tag in _REVISION_TAGS
        or (
            isinstance(node.tag, str) and etree.QName(node).localname.endswith("Change")
        )
        for node in root.iter()
    )


def _require_drafting_context(
    document: etree._Element, targets: set[etree._Element], author: str | None = None
) -> None:
    """Reject structural/protected contexts without banning ordinary redlines.

    A readable paragraph reference establishes identity, not writability. In
    particular, content controls are transparent to inspection and fields and
    revision/permission ranges may start several paragraphs before a target.
    Comments and ordinary bookmarks are deliberately allowed; their local span
    boundaries are handled by the text/comment surgery.
    """
    if not targets:
        return
    body = document.find(w("body"))

    def refuse(detail: str) -> None:
        raise ApplyError("drafting_context_unsupported", detail)

    for paragraph in targets:
        if len(paragraph.findall(w("pPr"))) > 1:
            refuse("duplicate paragraph properties")
        props = paragraph.find(w("pPr"))
        if props is not None:
            # Text surgery never edits paragraph properties or joins paragraphs.
            # Preserve their formatting history verbatim; structural revisions
            # of the target's own paragraph mark still require another workflow.
            for node in props.iter():
                if (node.tag in _REVISION_TAGS or (
                    isinstance(node.tag, str) and etree.QName(node).localname.endswith("Change")
                )) and node.tag not in {w("rPrChange"), w("pPrChange")}:
                    if (
                        node.tag == w("ins")
                        and node.getparent().tag == w("rPr")
                        and node.getparent().getparent() is props
                        and node.get(w("author")) == author
                        and len(node) == 0
                        and set(node.attrib) <= {w("id"), w("author"), w("date")}
                        and node.get(w("id"), "").isascii()
                        and node.get(w("id"), "").isdecimal()
                        and len(props.findall(f"{w('rPr')}/{w('ins')}")) == 1
                    ):
                        continue
                    refuse("the target paragraph mark has a structural revision")
        for ancestor in paragraph.iterancestors():
            if ancestor is body:
                break
            if ancestor.tag not in {w("tc"), w("tr"), w("tbl")}:
                refuse(
                    "content controls or other unsupported containers enclose the target"
                )
            for child in ancestor:
                if child.tag in {
                    w(name) for name in ("tcPr", "trPr", "tblPr", "tblPrEx", "tblGrid")
                } and _has_revision(child):
                    refuse("a table row, cell or table property has pending revisions")
    if any(_has_revision(section) for section in document.iter(w("sectPr"))):
        refuse("section properties have pending revisions")

    # A marker hidden in a text box, property node or unsupported wrapper must
    # never close a real main-story field/range. Track only recognized paths.
    story_tags = {
        w(name)
        for name in (
            "body",
            "tbl",
            "tr",
            "tc",
            "p",
            "r",
            "ins",
            "del",
            "moveFrom",
            "moveTo",
            "hyperlink",
            "smartTag",
            "customXml",
            "bdo",
            "dir",
            "sdt",
            "sdtContent",
        )
    }
    fields: list[bool] = []
    ranges: set[tuple[str, str]] = set()
    seen_ranges: set[tuple[str, str]] = set()
    selected = False
    for event, node in etree.iterwalk(body, events=("start", "end")):
        if node in targets:
            selected = event == "start"
            if selected and (fields or ranges):
                refuse("the target lies inside a field or revision/permission range")
        if event != "start" or not isinstance(node.tag, str):
            continue
        name = etree.QName(node).localname
        is_field = node.tag in {w("fldChar"), w("instrText"), w("fldSimple")}
        is_range = (node.tag in _REVISION_TAGS and "Range" in name) or node.tag in {
            w("permStart"),
            w("permEnd"),
        }
        if not is_field and not is_range:
            continue
        if any(
            ancestor.tag not in story_tags
            for ancestor in node.iterancestors()
            if ancestor is not document
        ):
            continue
        if selected:
            refuse("the target paragraph contains a field or revision/permission range")
        if node.tag == w("fldSimple"):
            continue  # complete field outside the target; does not change the stack
        if node.tag == w("fldChar"):
            kind = node.get(w("fldCharType"))
            if kind == "begin":
                fields.append(False)
            elif kind == "separate" and fields and not fields[-1]:
                fields[-1] = True
            elif kind == "end" and fields:
                fields.pop()
            else:
                refuse("the main story contains malformed field boundaries")
        elif node.tag == w("instrText"):
            if not fields or fields[-1]:
                refuse("the main story contains unscoped field instructions")
        else:
            identity = node.get(w("id"), "")
            if not identity.isascii() or not identity.isdecimal() or len(identity) > 10:
                refuse("the main story contains invalid range identifiers")
            boundary = "Start" if name.endswith("Start") else "End"
            key = (name.removesuffix(boundary), str(int(identity)))
            if boundary == "Start" and key not in seen_ranges:
                ranges.add(key)
                seen_ranges.add(key)
            elif boundary == "End" and key in ranges:
                ranges.remove(key)
            else:
                refuse("the main story contains malformed revision/permission ranges")
    if fields or ranges:
        refuse("the main story contains unclosed fields or revision/permission ranges")


def _revision_signature(node: etree._Element) -> tuple:
    """Revision content/format identity, insensitive only to comment run splits."""
    header = (node.tag, tuple(sorted(node.attrib.items())))
    if node.tag not in {w("ins"), w("del"), w("moveFrom"), w("moveTo")}:
        return header, etree.tostring(node, method="c14n", exclusive=True)
    chunks = []
    for atom in node.iter():
        if atom.tag not in {w("t"), w("delText"), w("tab"), w("br"), w("cr")}:
            continue
        run = next(atom.iterancestors(w("r")), None)
        props = run.find(w("rPr")) if run is not None else None
        formatting = (
            tuple(sorted(run.attrib.items())) if run is not None else (),
            etree.tostring(props, method="c14n", exclusive=True)
            if props is not None
            else b"",
        )
        lineage = []
        for ancestor in atom.iterancestors():
            if ancestor is node:
                break
            if ancestor.tag in _REVISION_TAGS:
                lineage.append((ancestor.tag, tuple(sorted(ancestor.attrib.items()))))
        kind = (atom.tag, formatting, tuple(lineage))
        value = (
            atom.text or ""
            if atom.tag in {w("t"), w("delText")}
            else ("\t" if atom.tag == w("tab") else "\n")
        )
        if chunks and chunks[-1][0] == kind:
            chunks[-1] = (kind, chunks[-1][1] + value)
        else:
            chunks.append((kind, value))
    return header, tuple(chunks)


def _revision_state(document: etree._Element) -> list[tuple[etree._Element, tuple]]:
    return [
        (node, _revision_signature(node))
        for node in document.iter()
        if node.tag in _REVISION_TAGS
    ]


def _check_revision_preservation(
    before: list[tuple],
    document: etree._Element,
    mutable: etree._Element | None,
    applied: dict,
    author: str,
    timestamp: str | None = None,
) -> None:
    """Prove unrelated revisions survived, including hidden deleted text."""
    immutable = Counter(signature for node, signature in before if node is not mutable)
    after = _revision_state(document)
    observed = Counter(signature for _, signature in after)
    if immutable - observed:
        raise ApplyError("round_trip_failed", "an unrelated existing revision changed")
    extras = list((observed - immutable).elements())
    operation = applied.get("operation")
    if operation in {"edit_own_insertion", "counter"}:
        if (
            mutable is None or mutable.tag != w("ins")
            or applied.get("target_revision_id") != mutable.get(w("id"))
            or applied.get("target_author") != mutable.get(w("author"))
            or (operation == "edit_own_insertion" and mutable.get(w("author")) != author)
            or (operation == "counter" and mutable.get(w("author")) == author)
        ):
            raise ApplyError("round_trip_failed", "replacement misidentified a revision's author or identity")
    date = annotation_date(timestamp)
    allowed: list[tuple] = []
    if operation in {"replace", "delete", "counter"}:
        ids = applied["revision_ids"]
        allowed.append(
            (
                (
                    w("del"),
                    tuple(sorted({w("id"): ids[0], w("author"): author,
                                  **({w("date"): date} if date is not None else {})}.items())),
                ),
                applied["old_text"],
            )
        )
        if applied["new_text"]:
            allowed.append(
                (
                    (
                        w("ins"),
                        tuple(sorted({w("id"): ids[1], w("author"): author,
                                      **({w("date"): date} if date is not None else {})}.items())),
                    ),
                    applied["new_text"],
                )
            )
    if operation in {"insert", "insert_paragraph"}:
        for revision_id in applied["revision_ids"]:
            expected_text = (
                "" if operation == "insert_paragraph"
                and revision_id == applied["paragraph_mark_revision_id"]
                else applied["text"] if operation == "insert_paragraph"
                else applied["new_text"]
            )
            allowed.append((
                (w("ins"), tuple(sorted({
                    w("id"): revision_id, w("author"): author,
                    **({w("date"): date} if date is not None else {}),
                }.items()))),
                expected_text,
            ))
    if mutable is not None:
        original_header = (mutable.tag, tuple(sorted(mutable.attrib.items())))
        original_text = "".join(
            atom.text or ""
            for atom in mutable.iter()
            if atom.tag in {w("t"), w("delText")}
        )
        expected = original_text
        if operation == "edit_own_insertion":
            if "target_start" in applied or "target_end" in applied:
                start, end = applied.get("target_start"), applied.get("target_end")
                if (
                    type(start) is not int or type(end) is not int
                    or not 0 <= start <= end <= len(original_text)
                    or original_text[start:end] != applied["old_text"]
                ):
                    raise ApplyError("round_trip_failed", "own revision span does not match its exact address")
                expected = original_text[:start] + applied["new_text"] + original_text[end:]
            else:
                expected = original_text.replace(
                    applied["old_text"], applied["new_text"], 1
                )
        if expected or operation == "counter":
            allowed.append((original_header, expected))
    if len(extras) != len(allowed):
        raise ApplyError(
            "round_trip_failed", "unexpected pending revisions were added or removed"
        )
    for header, text in allowed:
        match = next(
            (
                index
                for index, signature in enumerate(extras)
                if signature[0] == header
                and isinstance(signature[1], tuple)
                and "".join(chunk[1] for chunk in signature[1]) == text
            ),
            None,
        )
        if match is None:
            raise ApplyError(
                "round_trip_failed",
                "a revision's identity or proposal changed unexpectedly",
            )
        extras.pop(match)


@dataclass
class _Candidate:
    source_path: str
    source_payload: bytes
    payload: bytes
    operations: list[dict]
    timestamp: str | None = None


def _check_inserted_paragraph(before, anchor, added, applied) -> None:
    """Allow one new paragraph and its tracked boundary, no anchor rewrite."""
    normalized_anchor = copy.deepcopy(anchor)
    normalized_added = copy.deepcopy(added)
    marked = normalized_anchor if applied["paragraph_mark_location"] == "anchor" else normalized_added
    marks = [
        node for node in marked.findall(f"{w('pPr')}/{w('rPr')}/{w('ins')}")
        if node.get(w("id")) == applied["paragraph_mark_revision_id"]
    ]
    if len(marks) != 1:
        raise ApplyError("round_trip_failed", "new paragraph boundary is not tracked")
    mark_author = marks[0].get(w("author"))
    props = marked.find(w("pPr"))
    run_props = marks[0].getparent()
    run_props.remove(marks[0])
    old_props = before.find(w("pPr"))
    old_run_props = old_props.find(w("rPr")) if old_props is not None else None
    if old_run_props is None and len(run_props) == 0 and not run_props.attrib and not run_props.text:
        props.remove(run_props)
    if old_props is None and len(props) == 0 and not props.attrib and not props.text:
        marked.remove(props)
    if etree.tostring(normalized_anchor) != etree.tostring(before):
        raise ApplyError("round_trip_failed", "paragraph insertion rewrote its anchor")
    inherited = normalized_added.find(w("pPr"))
    expected_inherited = copy.deepcopy(old_props)
    if applied["position"] == "before" and expected_inherited is not None:
        own_marks = expected_inherited.findall(f"{w('rPr')}/{w('ins')}")
        if len(own_marks) == 1 and own_marks[0].get(w("author")) == mark_author:
            own_marks[0].getparent().remove(own_marks[0])
    def serialize(p):
        if p is None or (not len(p) and not p.attrib and not p.text):
            return None
        return etree.tostring(p, method="c14n", exclusive=True)
    if serialize(inherited) != serialize(expected_inherited):
        raise ApplyError("round_trip_failed", "new paragraph did not preserve inherited properties")
    if added.attrib or added.tail is not None:
        raise ApplyError("round_trip_failed", "new paragraph acquired unrelated identity or container text")


def _prepare(
    source_path: str,
    file_sha256: str,
    operations: list[dict],
    author: str,
    initials: str,
    timestamp: str | None = None,
) -> _Candidate:
    _profile(author, initials)
    _validate_operations(operations)
    source = _path(source_path)
    try:
        payload = read_docx_payload(source)
    except OSError as exc:
        raise ApplyError("file_unreadable", "cannot read document") from exc
    if _sha(payload) != file_sha256:
        raise ApplyError(
            "file_sha256_mismatch", "reread the current document before editing"
        )
    package = _read_source_archive(payload, source)
    if any(name.startswith("_xmlsignatures/") for name in package.parts):
        raise ApplyError(
            "signed_document", "editing a digitally signed package is unsupported"
        )
    parts = dict(package.parts)
    _require_unprotected(parts)
    document = parse_xml(parts[DOCUMENT_PART])
    original = copy.deepcopy(document)
    snapshot = _snapshot_from_validated(package, path=source, file_sha256=file_sha256)
    paragraphs = canonical_body_flow_v1(document.find(w("body"))).paragraphs
    allocator = _revision_id_allocator(document)
    resolved: dict[int, int] = {}
    for index, op in enumerate(operations):
        if "paragraph_ref" in op:
            resolved[index] = _resolve_paragraph(
                snapshot, op["paragraph_ref"]
            ).paragraph_index
    source_elements = [p.element for p in paragraphs]
    source_all_elements = list(document.iter(w("p")))
    expected_elements = list(source_elements)
    expected_text = {
        p: _reading_text(_paragraph_segments(p)) for p in source_elements
    }
    expected_properties = {
        p: tuple(etree.tostring(n, method="c14n", exclusive=True)
                 for n in p.findall(w("pPr")))
        for p in source_all_elements
    }
    inserted: set[etree._Element] = set()
    after_cursors: dict[etree._Element, etree._Element] = {}
    touched: set[etree._Element] = set()
    result = []
    allowed_parts = {DOCUMENT_PART}
    for index, op in enumerate(operations):
        try:
            kind = op["kind"]
            revisions_before = _revision_state(document)
            mutable_revision = None
            if kind in {"replace_text", "replace_paragraph", "insert_paragraph", "add_comment"}:
                pindex = resolved[index]
                paragraph = paragraphs[pindex].element
                _require_drafting_context(document, {paragraph}, author)
                touched.add(paragraph)
                if kind == "replace_text":
                    segments = _paragraph_segments(paragraph)
                    start = _reading_text(segments).find(op["old_text"])
                    end = start + len(op["old_text"])
                    containers = {
                        seg.container
                        for seg in segments
                        if seg.start < end and seg.end > start
                    }
                    if len(containers) == 1:
                        mutable_revision = next(iter(containers))
                    applied = replace_text(
                        paragraph, op["old_text"], op["new_text"], author, allocator,
                        timestamp=timestamp,
                    )
                    current = expected_text[paragraph]
                    if current.count(op["old_text"]) != 1:
                        raise ApplyError(
                            "round_trip_failed", "unexpected replacement scope"
                        )
                    expected_text[paragraph] = current.replace(
                        op["old_text"], op["new_text"], 1
                    )
                    applied["paragraph_index"] = pindex
                    allowed_parts |= {_SETTINGS_PART, _RELS_PART, _CT_PART}
                elif kind == "replace_paragraph":
                    verified_paragraph = copy.deepcopy(paragraph)

                    def verify_step(before, after, mutable, step):
                        nonlocal verified_paragraph
                        if etree.tostring(before) != etree.tostring(verified_paragraph):
                            raise ApplyError("round_trip_failed", "replacement steps do not continue the verified paragraph")
                        if "start" in step or "end" in step:
                            start, end = step.get("start"), step.get("end")
                            prior = _reading_text(_paragraph_segments(before))
                            if (
                                type(start) is not int or type(end) is not int
                                or not 0 <= start <= end <= len(prior)
                                or prior[start:end] != step["old_text"]
                                or _reading_text(_paragraph_segments(after))
                                != prior[:start] + step["new_text"] + prior[end:]
                            ):
                                raise ApplyError("round_trip_failed", "paragraph hunk changed text outside its exact address")
                        _check_revision_preservation(
                            _revision_state(before), after, mutable, step, author, timestamp
                        )
                        verified_paragraph = copy.deepcopy(after)

                    applied = replace_paragraph(
                        paragraph, op["old_text"], op["new_text"], author, allocator,
                        mode=op.get("mode", "minimal"), timestamp=timestamp, verify_step=verify_step,
                    )
                    if etree.tostring(paragraph) != etree.tostring(verified_paragraph):
                        raise ApplyError("round_trip_failed", "paragraph differs from verified replacement steps")
                    if expected_text[paragraph] != op["old_text"]:
                        raise ApplyError("round_trip_failed", "unexpected whole paragraph scope")
                    expected_text[paragraph] = op["new_text"]
                    applied["paragraph_index"] = pindex
                    allowed_parts |= {_SETTINGS_PART, _RELS_PART, _CT_PART}
                elif kind == "insert_paragraph":
                    anchor = after_cursors.get(paragraph, paragraph) if op["position"] == "after" else paragraph
                    before_anchor = copy.deepcopy(anchor)
                    added, applied = insert_paragraph(
                        anchor, op["position"], op["text"], author, allocator,
                        timestamp=timestamp,
                    )
                    _check_inserted_paragraph(before_anchor, anchor, added, applied)
                    if op["position"] == "after":
                        after_cursors[paragraph] = added
                    expected_elements.insert(
                        expected_elements.index(anchor) + (op["position"] == "after"),
                        added,
                    )
                    expected_text[added] = op["text"]
                    inserted.add(added)
                    touched.add(anchor)
                    for node in (anchor, added):
                        expected_properties[node] = tuple(
                            etree.tostring(n, method="c14n", exclusive=True)
                            for n in node.findall(w("pPr"))
                        )
                    applied["paragraph_index"] = pindex
                    allowed_parts |= {_SETTINGS_PART, _RELS_PART, _CT_PART}
                else:
                    applied = add_comment(
                        parts,
                        document,
                        paragraph,
                        op["quote"],
                        op["text"],
                        author,
                        initials,
                        timestamp=timestamp,
                    )
                    allowed_parts |= _COMMENT_PARTS
            else:
                existing = next(
                    (
                        item
                        for item in read_comments(parts, document)
                        if item["id"] == op["comment_id"]
                    ),
                    None,
                )
                if existing:
                    current_paragraphs = canonical_body_flow_v1(document.find(w("body"))).paragraphs
                    anchors = {current_paragraphs[i].element for i in existing["paragraph_indexes"]}
                    _require_drafting_context(
                        document, anchors, author,
                    )
                    if kind in {"delete_comment", "reply_comment"}:
                        touched.update(anchors)
                allowed_parts |= _COMMENT_PARTS | comment_mutable_parts(parts)
                if kind == "update_comment":
                    applied = update_comment(
                        parts, document, op["comment_id"], op["text"], author
                    )
                elif kind == "reply_comment":
                    applied = reply_comment(
                        parts, document, op["comment_id"], op["text"], author, initials,
                        timestamp=timestamp,
                    )
                elif kind == "set_comment_resolved":
                    applied = set_comment_resolved(
                        parts, document, op["comment_id"], op["resolved"], author,
                    )
                else:
                    applied = delete_comment(parts, document, op["comment_id"], author)
            if kind != "replace_paragraph":
                _check_revision_preservation(
                    revisions_before, document, mutable_revision, applied, author, timestamp
                )
            result.append({"index": index, **applied})
        except DocxError as exc:
            if hasattr(exc, "metadata"):
                exc.metadata["operation_index"] = index
            raise
    if any(op["kind"] in {"replace_text", "replace_paragraph", "insert_paragraph"} for op in operations):
        _enable_tracking(parts)
    parts[DOCUMENT_PART] = _xml(document)
    infos = list(package.infos)
    existing_names = {info.filename for info in infos}
    infos += [
        zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
        for name in sorted(set(parts) - existing_names)
    ]
    expected_revisions = Counter(signature for _, signature in _revision_state(document))
    candidate = _output_archive_bytes(infos, parts)
    validate_docx_payload_size(candidate, limit="candidate_docx_bytes")
    checked = _read_source_archive(candidate, source)
    _extract_validated(checked, source, _sha(candidate))
    if any(
        checked.parts.get(name) != package.parts.get(name)
        for name in (set(checked.parts) | set(package.parts)) - allowed_parts
    ):
        raise ApplyError("round_trip_failed", "unrelated package content changed")
    reread = parse_xml(checked.parts[DOCUMENT_PART])
    if Counter(signature for _, signature in _revision_state(reread)) != expected_revisions:
        raise ApplyError("round_trip_failed", "serialized revision history differs from the verified candidate")
    after_paragraphs = canonical_body_flow_v1(reread.find(w("body"))).paragraphs
    if [
        _reading_text(_paragraph_segments(p.element)) for p in after_paragraphs
    ] != [expected_text[p] for p in expected_elements]:
        raise ApplyError(
            "round_trip_failed", "document reading differs from requested edits"
        )
    planned_paragraphs = canonical_body_flow_v1(document.find(w("body"))).paragraphs
    if [p.element for p in planned_paragraphs] != expected_elements:
        raise ApplyError("round_trip_failed", "paragraph insertion changed document order")
    final_all = list(document.iter(w("p")))
    reread_all = list(reread.iter(w("p")))
    if len(final_all) != len(reread_all):
        raise ApplyError("round_trip_failed", "serialized paragraph structure changed")
    projected = copy.deepcopy(reread)
    projected_all = list(projected.iter(w("p")))
    for planned, checked_paragraph in zip(final_all, projected_all):
        properties = tuple(
            etree.tostring(n, method="c14n", exclusive=True)
            for n in checked_paragraph.findall(w("pPr"))
        )
        if properties != expected_properties[planned]:
            raise ApplyError("round_trip_failed", "paragraph properties changed during drafting")
        if planned in inserted:
            if checked_paragraph.tail is not None:
                raise ApplyError("round_trip_failed", "inserted paragraph changed container text")
            if etree.tostring(planned) != etree.tostring(checked_paragraph):
                raise ApplyError("round_trip_failed", "inserted paragraph changed on serialization")
            checked_paragraph.getparent().remove(checked_paragraph)
    all_positions = {
        i for i, p in enumerate(source_all_elements) if p in touched
    }
    if _collateral_outside(original, projected, all_positions):
        raise ApplyError(
            "round_trip_failed", "document changed outside the requested paragraphs"
        )
    # Reparse comment linkage as well as text revisions before any filesystem write.
    read_comments(checked.parts, reread)
    return _Candidate(source, payload, candidate, result, timestamp)


def _proof(
    candidate: _Candidate,
    operations: list[dict],
    author: str,
    initials: str,
    producer_build: str,
    output_path: str | None,
) -> dict:
    if not isinstance(producer_build, str) or not producer_build:
        raise ApplyError("preflight_proof_invalid", "producer build is required")
    destination = (
        _path(output_path) if output_path is not None else candidate.source_path
    )
    fields = {
        "schema_version": PROOF_VERSION,
        "source_path": candidate.source_path,
        "source_sha256": _sha(candidate.source_payload),
        "output_path": destination,
        "operations_sha256": _canonical_digest(operations),
        "author": author,
        "initials": initials,
        "producer_build": producer_build,
        "timestamp": candidate.timestamp,
        "candidate_sha256": _sha(candidate.payload),
    }
    return {**fields, "proof_sha256": _canonical_digest(fields)}


def preflight_document_edits(
    source_path: str,
    file_sha256: str,
    operations: list[dict],
    *,
    author: str = DEFAULT_AUTHOR,
    initials: str = "VM",
    producer_build: str,
    output_path: str | None = None,
) -> dict:
    """Dry-run the full sequential batch; no document, backup or journal is written."""
    try:
        candidate = _prepare(source_path, file_sha256, operations, author, initials,
                             timestamp=_new_timestamp())
        proof = _proof(
            candidate, operations, author, initials, producer_build, output_path
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "batch_applicable": True,
            "source_sha256": file_sha256,
            "tracked_change_author": author,
            "comment_initials": initials,
            "operations": candidate.operations,
            "preflight_proof": proof,
            "refusal_code": None,
            "blocking_operation_index": None,
        }
    except DocxError as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "batch_applicable": False,
            "source_sha256": file_sha256,
            "tracked_change_author": author,
            "comment_initials": initials,
            "operations": [],
            "preflight_proof": None,
            "refusal_code": getattr(exc, "code", "docx_error"),
            "blocking_operation_index": getattr(exc, "metadata", {}).get(
                "operation_index"
            ),
        }


def apply_document_edits(
    source_path: str,
    file_sha256: str,
    operations: list[dict],
    preflight_proof: dict,
    *,
    author: str = DEFAULT_AUTHOR,
    initials: str = "VM",
    producer_build: str,
    output_path: str | None = None,
) -> dict:
    """Apply the exact successful preflight candidate, with backup for in-place saves."""
    _validate_operations(operations)
    timestamp = _proof_timestamp(preflight_proof)
    candidate = _prepare(source_path, file_sha256, operations, author, initials,
                         timestamp=timestamp)
    expected = _proof(
        candidate, operations, author, initials, producer_build, output_path
    )
    if not isinstance(preflight_proof, dict) or preflight_proof != expected:
        raise ApplyError(
            "preflight_binding_mismatch", "preflight does not match current inputs"
        )
    publication = publish_document(
        candidate.source_path, output_path, candidate.source_payload, candidate.payload
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        **publication,
        "source_sha256": _sha(candidate.source_payload),
        "output_sha256": _sha(candidate.payload),
        "tracked_change_author": author,
        "comment_initials": initials,
        "operations": candidate.operations,
        "preflight_binding_status": "verified",
    }


def read_document_comments(path: str, *, offset: int = 0, max_items: int = 100) -> dict:
    """Read bounded comment metadata and text from one package snapshot."""
    if (
        type(offset) is not int
        or offset < 0
        or type(max_items) is not int
        or not 1 <= max_items <= 100
    ):
        raise ApplyError("invalid_arguments", "invalid comment page bounds")
    source = _path(path)
    try:
        payload = read_docx_payload(source)
    except OSError as exc:
        raise ApplyError("file_unreadable", "cannot read document") from exc
    package = _read_source_archive(payload, source)
    items = read_comments(package.parts, parse_xml(package.parts[DOCUMENT_PART]))
    selected = items[offset : offset + max_items]
    if sum(len(item["text"]) for item in selected) > MAX_BATCH_CHARS:
        raise ApplyError("resource_limit_exceeded", "reduce comment page size")
    return {
        "schema_version": SCHEMA_VERSION,
        "source_path": source,
        "file_sha256": _sha(payload),
        "comments": selected,
        "total_count": len(items),
        "next_offset": offset + len(selected)
        if offset + len(selected) < len(items)
        else None,
    }
