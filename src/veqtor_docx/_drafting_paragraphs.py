# SPDX-License-Identifier: Apache-2.0
"""Tracked paragraph operations on already-resolved main-story elements.

The caller establishes canonical addressing and package-level writability.
These helpers never save a file; rejected operations do not mutate their
elements or consume revision ids.

A paragraph break is a revision separate from its text (ISO/IEC 29500-1
17.13.5.20). Inserting before an anchor adds the new paragraph's break.
Inserting after an anchor adds the anchor's break and transfers its existing
break/formatting to the new paragraph. This also works at a document or cell
end: rejecting the inserted break joins two paragraphs, instead of leaving an
untracked empty paragraph at the end.
"""

from __future__ import annotations

import copy
from collections.abc import Callable

from lxml import etree

from ._drafting_text import (
    _comment_reference_run,
    _layout_run,
    _new_revision,
    _new_run,
    _simple_run,
    annotation_date,
    replace_text,
)
from ._ooxml import (
    _CANONICAL_BLOCK_PASS_THROUGH,
    _CANONICAL_INLINE_PASS_THROUGH,
    is_xml_text_compatible,
    iter_canonical_paragraph_nodes,
    tracked_change_author_validation_error,
    w,
)
from ._paragraph_edits import _PROPERTY_TAGS, _REVISION_TAGS
from .apply import (
    MAX_NEW_TEXT_CHARS_PER_EDIT,
    ApplyError,
    _paragraph_segments,
    _reading_text,
    _RevisionIdAllocator,
)

_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
MAX_PARAGRAPH_EDIT_STEPS = 256
_EDGE_MARKERS = frozenset(
    w(name)
    for name in (
        "commentRangeStart", "commentRangeEnd", "bookmarkStart", "bookmarkEnd", "proofErr",
    )
)
_RANGE_STARTS = {
    w("commentRangeStart"): "comment",
    w("bookmarkStart"): "bookmark",
    w("permStart"): "permission",
}
_RANGE_ENDS = {
    w("commentRangeEnd"): "comment",
    w("bookmarkEnd"): "bookmark",
    w("permEnd"): "permission",
}
VerifyStep = Callable[
    [etree._Element, etree._Element, etree._Element | None, dict], None
]


def _reading(paragraph: etree._Element) -> str:
    return _reading_text(_paragraph_segments(paragraph))


def _validate_common(
    paragraph: etree._Element, text: str, author: str, timestamp: str | None
) -> str | None:
    if not isinstance(paragraph, etree._Element) or paragraph.tag != w("p"):
        raise ApplyError("invalid_edit", "paragraph must be a resolved Word paragraph")
    if len(paragraph.findall(w("pPr"))) > 1:
        raise ApplyError("paragraph_structure_unsupported", "duplicate paragraph properties")
    if not isinstance(text, str) or not is_xml_text_compatible(text):
        raise ApplyError("invalid_edit", "text must be an XML-compatible string")
    if any(char in text for char in "\t\r\n"):
        raise ApplyError("invalid_edit", "paragraph text must not contain tabs or line breaks")
    if len(text) > MAX_NEW_TEXT_CHARS_PER_EDIT:
        raise ApplyError("resource_limit_exceeded", "text exceeds the per-edit character limit")
    if author_error := tracked_change_author_validation_error(author):
        raise ApplyError("invalid_author", author_error)
    try:
        return annotation_date(timestamp)
    except ValueError as exc:
        raise ApplyError(
            "invalid_edit", "timestamp must be a valid date with an explicit timezone"
        ) from exc


def _has_revision(root: etree._Element) -> bool:
    return any(
        node.tag in _REVISION_TAGS
        or (isinstance(node.tag, str) and etree.QName(node).localname.endswith("Change"))
        for node in root.iter()
    )


def _safe_inherited_properties(
    anchor: etree._Element, position: str, author: str
) -> etree._Element:
    props = anchor.find(w("pPr"))
    if props is None:
        return etree.Element(w("pPr"))
    props = copy.deepcopy(props)
    marks = props.findall(f"{w('rPr')}/{w('ins')}")
    if position == "before" and len(marks) == 1:
        mark = marks[0]
        if (
            mark.get(w("author")) == author
            and not len(mark)
            and set(mark.attrib) <= {w("id"), w("author"), w("date")}
            and mark.get(w("id"), "").isascii()
            and mark.get(w("id"), "").isdecimal()
        ):
            # The anchor keeps its pending break. Only its current formatting
            # is inherited by the new paragraph; its revision id must not copy.
            mark.getparent().remove(mark)
    if (
        _has_revision(props)
        or any(node.tag not in _PROPERTY_TAGS for node in props.iter())
        or props.find(w("framePr")) is not None
        or len(props.findall(w("rPr"))) > 1
    ):
        raise ApplyError(
            "paragraph_structure_unsupported",
            "insertion cannot inherit section, frame, revision or unsupported paragraph properties",
        )
    return props


def _assert_insertion_boundary(anchor: etree._Element, position: str) -> None:
    """Do not silently extend a comment/bookmark across the inserted paragraph."""
    parent = anchor.getparent()
    if parent is None or parent.tag not in {w("body"), w("tc")}:
        raise ApplyError(
            "paragraph_structure_unsupported", "insertion requires a direct body or cell paragraph"
        )
    body = next(anchor.iterancestors(w("body")), None)
    if body is None:
        raise ApplyError("paragraph_structure_unsupported", "insertion requires the main story")
    # Reuse the reader's block/inline boundary. Range markers themselves are
    # zero-width and omitted from its text iterator, so admit only markers whose
    # immediate parent is a recognized, reachable canonical story container.
    # A fake endpoint in a textbox, property subtree or unknown wrapper cannot
    # close an actual main-story range.
    story_containers = set()
    pending = [body]
    while pending:
        container = pending.pop()
        story_containers.add(container)
        for child in container:
            if child.tag in _CANONICAL_BLOCK_PASS_THROUGH:
                pending.append(child)
            elif child.tag == w("p"):
                story_containers.update(
                    node for node in iter_canonical_paragraph_nodes(child)
                    if node.tag == w("p") or node.tag in _CANONICAL_INLINE_PASS_THROUGH
                )
    open_ranges: set[tuple[str, str]] = set()
    for event, node in etree.iterwalk(body, events=("start", "end")):
        if node is anchor and event == ("start" if position == "before" else "end"):
            if open_ranges:
                raise ApplyError(
                    "paragraph_boundary_unsupported",
                    "insertion would extend an existing comment, bookmark or permission range",
                )
            return
        if event != "start" or node.getparent() not in story_containers:
            continue
        if node.tag in _RANGE_STARTS:
            key = (_RANGE_STARTS[node.tag], node.get(w("id"), ""))
            if not key[1] or key in open_ranges:
                raise ApplyError("paragraph_boundary_unsupported", "malformed range boundary")
            open_ranges.add(key)
        elif node.tag in _RANGE_ENDS:
            key = (_RANGE_ENDS[node.tag], node.get(w("id"), ""))
            if key not in open_ranges:
                raise ApplyError("paragraph_boundary_unsupported", "malformed range boundary")
            open_ranges.remove(key)


def _fresh_run(anchor: etree._Element, text: str) -> etree._Element:
    """Copy current character formatting, never another revision's identity."""
    for segment in _paragraph_segments(anchor):
        if segment.text and _simple_run(segment.run):
            run = _new_run(segment.run, text, preserve_format_history=False)
            # Session identifiers describe the old editing session, not this run.
            run.attrib.clear()
            return run
    run = etree.Element(w("r"))
    mark_props = anchor.find(f"{w('pPr')}/{w('rPr')}")
    if mark_props is not None:
        props = copy.deepcopy(mark_props)
        for child in list(props):
            if child.tag in _REVISION_TAGS:
                # Paragraph-break identities and former character properties
                # belong to the existing mark, never to a newly written run.
                props.remove(child)
        if _has_revision(props):
            raise ApplyError(
                "paragraph_structure_unsupported", "paragraph-mark formatting has pending revisions"
            )
        run.append(props)
    atom = etree.SubElement(run, w("t"))
    atom.text = text
    atom.set(_XML_SPACE, "preserve")
    return run


def insert_paragraph(
    anchor: etree._Element,
    position: str,
    text: str,
    author: str,
    allocator: _RevisionIdAllocator,
    *,
    timestamp: str | None = None,
) -> tuple[etree._Element, dict]:
    """Insert one tracked paragraph and return its element plus wire-safe facts.

    Numbering and paragraph formatting continue from the anchor. Section/frame
    paragraphs and tracked paragraph properties fail closed, except that a
    simple own inserted mark may remain on a ``before`` anchor. The new paragraph
    gets no copied paraId/textId/session attributes. Its text and paragraph mark
    have distinct revision ids. For ``after`` the mark belongs to the anchor;
    its former paragraph properties are copied onto the inserted paragraph.
    """
    date = _validate_common(anchor, text, author, timestamp)
    if position not in {"before", "after"}:
        raise ApplyError("invalid_edit", "position must be before or after")
    if not text:
        raise ApplyError("invalid_edit", "inserted paragraph text must be non-empty")
    _assert_insertion_boundary(anchor, position)
    inherited = _safe_inherited_properties(anchor, position, author)
    run = _fresh_run(anchor, text)
    candidate_allocator = _RevisionIdAllocator(allocator.next_value)
    mark_id, text_id = candidate_allocator.reserve(2)

    paragraph = etree.Element(w("p"))
    paragraph.append(inherited)
    insertion = _new_revision("ins", text_id, author, date)
    insertion.append(run)
    paragraph.append(insertion)
    mark_props = inherited if position == "before" else copy.deepcopy(inherited)
    mark_run_props = mark_props.find(w("rPr"))
    if mark_run_props is None:
        # rPr follows all ordinary pPr properties. Section/revision properties
        # that would follow it are deliberately excluded above.
        mark_run_props = etree.SubElement(mark_props, w("rPr"))
    mark_run_props.append(_new_revision("ins", mark_id, author, date))

    if _reading(paragraph) != text:
        raise ApplyError("round_trip_failed", "inserted paragraph reading differs from text")
    if position == "before":
        anchor.addprevious(paragraph)
    else:
        previous_props = anchor.find(w("pPr"))
        if previous_props is not None:
            anchor.replace(previous_props, mark_props)
        else:
            anchor.insert(0, mark_props)
        anchor.addnext(paragraph)
    allocator.next_value = candidate_allocator.next_value
    return paragraph, {
        "operation": "insert_paragraph",
        "position": position,
        "text": text,
        "revision_ids": [mark_id, text_id],
        "paragraph_mark_revision_id": mark_id,
        "text_revision_id": text_id,
        "paragraph_mark_location": "inserted" if position == "before" else "anchor",
    }


def _empty_run(run: etree._Element) -> bool:
    return (
        run.tag == w("r")
        and len(run.findall(w("rPr"))) <= 1
        and all(child.tag == w("rPr") for child in run)
        and not _has_revision(run)
    )


def _validate_replacement_shape(paragraph: etree._Element, author: str) -> list:
    """Return editable direct children; hidden deletions are retained verbatim."""
    editable = []
    for child in paragraph:
        if child.tag == w("pPr") or child.tag == w("del"):
            continue
        if _simple_run(child):
            if child.find(w("t")).text:
                editable.append(child)
            continue
        if child.tag == w("ins"):
            if child.get(w("author")) != author:
                raise ApplyError(
                    "foreign_insertion_unsupported",
                    "whole-paragraph replacement cannot discard another author's insertion",
                )
            if not child.get(w("id")) or not all(
                _simple_run(run) or _layout_run(run) or _empty_run(run) for run in child
            ):
                raise ApplyError(
                    "overlaps_tracked_changes",
                    "own insertion contains nested revisions or unsupported content",
                )
            if any(_has_revision(run) for run in child):
                raise ApplyError(
                    "foreign_formatting_unsupported",
                    "mixed paragraph replacement cannot discard tracked insertion formatting",
                )
            if any(atom.text for atom in child.iter(w("t"))):
                editable.append(child)
            continue
        if (
            child.tag in _EDGE_MARKERS
            or _comment_reference_run(child)
            or _layout_run(child)
            or _empty_run(child)
        ):
            continue
        raise ApplyError(
            "paragraph_structure_unsupported",
            "whole-paragraph replacement requires simple text, own insertions and retained deletions",
        )
    if editable:
        first, last = paragraph.index(editable[0]), paragraph.index(editable[-1])
        for child in list(paragraph)[first : last + 1]:
            if child.tag in _EDGE_MARKERS or _comment_reference_run(child):
                raise ApplyError(
                    "paragraph_boundary_unsupported",
                    "whole-paragraph replacement crosses an interior range or comment anchor",
                )
    return editable


def _verify(
    before: etree._Element,
    after: etree._Element,
    mutable: etree._Element | None,
    step: dict,
    verify_step: VerifyStep | None,
    steps: list[dict],
) -> None:
    if verify_step is not None:
        verify_step(before, after, mutable, step)
    steps.append(step)


def replace_paragraph(
    paragraph: etree._Element,
    old_text: str,
    new_text: str,
    author: str,
    allocator: _RevisionIdAllocator,
    *,
    timestamp: str | None = None,
    verify_step: VerifyStep | None = None,
) -> dict:
    """Replace the entire current reading while retaining the paragraph mark.

    ``old_text`` must match exactly, including spaces; an empty paragraph can be
    filled and ``new_text=''`` deletes visible text while retaining its mark.
    Accepted runs become tracked deletions. Own pending insertions may be
    revised/removed; foreign visible insertions and unsupported interior anchors
    are refused. Existing hidden deletions and paragraph properties survive.

    An optional independent verifier runs after every step on disposable XML.
    Its arguments are a before-paragraph copy, the candidate after the step,
    the mutable own revision from that before copy (or None), and step facts.
    Failure leaves the supplied paragraph and allocator unchanged.
    """
    date = _validate_common(paragraph, new_text, author, timestamp)
    if not isinstance(old_text, str) or not is_xml_text_compatible(old_text):
        raise ApplyError("invalid_edit", "old_text must be an XML-compatible string")
    if _reading(paragraph) != old_text:
        raise ApplyError(
            "paragraph_text_mismatch", "old_text does not equal the paragraph's entire current reading"
        )
    if old_text == new_text:
        raise ApplyError("invalid_edit", "old_text and new_text must differ")

    candidate = copy.deepcopy(paragraph)
    candidate_allocator = _RevisionIdAllocator(allocator.next_value)
    steps: list[dict] = []
    segments = [segment for segment in _paragraph_segments(candidate) if segment.text]
    containers = {segment.container for segment in segments}
    own = next(iter(containers)) if len(containers) == 1 else None
    if (
        old_text
        and own is not None
        and own.tag == w("ins")
        and own.get(w("author")) == author
        and own.getparent() is candidate
    ):
        # Preserve the existing insertion's id/date and native supported
        # bookmarks/formatting when it contains the complete current reading.
        before = copy.deepcopy(candidate)
        mutable = before[candidate.index(own)]
        step = replace_text(
            candidate, old_text, new_text, author, candidate_allocator, timestamp=timestamp
        )
        _verify(before, candidate, mutable, step, verify_step, steps)
    else:
        editable = _validate_replacement_shape(candidate, author)
        if old_text and not editable:
            raise ApplyError("paragraph_structure_unsupported", "no supported visible text found")
        template = _fresh_run(candidate, new_text) if new_text else None
        # Keep zero-width edge anchors on their original side of the replacement.
        insertion_index = candidate.index(editable[0]) if editable else next(
            (index for index, child in enumerate(candidate)
             if child.tag in {w("commentRangeEnd"), w("bookmarkEnd")}
             or _comment_reference_run(child)),
            len(candidate),
        )
        if insertion_index == 0 and candidate.find(w("pPr")) is not None:
            insertion_index = 1
        groups: list[list[etree._Element]] = []
        for child in editable:
            if (
                groups
                and child.tag == w("r")
                and groups[-1][-1].tag == w("r")
                and groups[-1][-1].getnext() is child
            ):
                groups[-1].append(child)
            else:
                groups.append([child])
        if len(groups) + bool(new_text) > MAX_PARAGRAPH_EDIT_STEPS:
            raise ApplyError(
                "resource_limit_exceeded",
                "paragraph replacement has too many separated revision spans",
            )
        for group in groups:
            child = group[0]
            before = copy.deepcopy(candidate)
            child_index = candidate.index(child)
            child_text = "".join(
                atom.text or "" for member in group for atom in member.iter(w("t"))
            )
            if child.tag == w("ins"):
                mutable = before[child_index]
                revision_ids = [child.get(w("id"))]
                # Cached layout markers are not textual history. Keep them once.
                layout = etree.Element(w("r"))
                for marker in list(child.iter(w("lastRenderedPageBreak"))):
                    layout.append(copy.deepcopy(marker))
                candidate.remove(child)
                if len(layout):
                    candidate.insert(child_index, layout)
                step = {
                    "operation": "edit_own_insertion",
                    "old_text": child_text,
                    "new_text": "",
                    "revision_ids": revision_ids,
                    "target_revision_id": revision_ids[0],
                    "target_author": author,
                }
            else:
                mutable = None
                revision_ids = list(candidate_allocator.reserve(1))
                deletion = _new_revision("del", revision_ids[0], author, date)
                candidate.replace(child, deletion)
                for run in group:
                    deletion.append(run)
                    atom = run.find(w("t"))
                    atom.tag = w("delText")
                    atom.set(_XML_SPACE, "preserve")
                step = {
                    "operation": "delete",
                    "old_text": child_text,
                    "new_text": "",
                    "revision_ids": revision_ids,
                    "target_revision_id": None,
                    "target_author": None,
                }
            _verify(before, candidate, mutable, step, verify_step, steps)
        if new_text:
            before = copy.deepcopy(candidate)
            revision_id = candidate_allocator.reserve(1)[0]
            insertion = _new_revision("ins", revision_id, author, date)
            insertion.append(template)
            candidate.insert(insertion_index, insertion)
            step = {
                "operation": "insert",
                "old_text": "",
                "new_text": new_text,
                "revision_ids": [revision_id],
                "target_revision_id": None,
                "target_author": None,
            }
            _verify(before, candidate, None, step, verify_step, steps)
    if _reading(candidate) != new_text:
        raise ApplyError("round_trip_failed", "whole-paragraph replacement reading differs")
    for child in list(paragraph):
        paragraph.remove(child)
    for child in list(candidate):
        paragraph.append(child)
    allocator.next_value = candidate_allocator.next_value
    return {
        "operation": "replace_paragraph",
        "old_text": old_text,
        "new_text": new_text,
        "revision_ids": list(dict.fromkeys(
            revision_id for step in steps for revision_id in step["revision_ids"]
        )),
        "steps": steps,
    }
