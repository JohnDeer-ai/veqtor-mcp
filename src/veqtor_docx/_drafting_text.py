# SPDX-License-Identifier: Apache-2.0
"""Precise, repeatable tracked-text surgery for the additive drafting API.

Unlike the legacy negotiation API, editing our own pending insertion updates
that insertion instead of manufacturing a second layer of revisions. The
caller supplies a paragraph already resolved against the package's canonical
body flow and performs package-level integrity checks and atomic publication.
This module has no file-system effects. A refused edit changes neither its
paragraph nor its revision-id allocator.
"""

from __future__ import annotations

import copy

from lxml import etree

from ._ooxml import (
    is_xml_text_compatible,
    tracked_change_author_validation_error,
    w,
)
from .apply import (
    ApplyError,
    MAX_NEW_TEXT_CHARS_PER_EDIT,
    _RevisionIdAllocator,
    _paragraph_segments,
    _reading_text,
    _split_run_at,
)

_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_OUTSIDE_MARKERS = frozenset(
    w(name)
    for name in (
        "commentRangeStart",
        "commentRangeEnd",
        "bookmarkStart",
        "bookmarkEnd",
        "proofErr",
    )
)


def _simple_run(run: etree._Element | None) -> bool:
    """One literal atom, unambiguous formatting, and no revision properties."""
    return (
        run is not None
        and run.tag == w("r")
        and len(run.findall(w("t"))) == 1
        and len(run.findall(w("rPr"))) <= 1
        and all(child.tag in (w("t"), w("rPr")) for child in run)
        and not any(
            isinstance(node.tag, str) and etree.QName(node).localname.endswith("Change")
            for node in run.iter()
        )
    )


def _comment_reference_run(run: etree._Element) -> bool:
    """A zero-width reference may remain outside a matched own-insertion span."""
    return (
        run.tag == w("r")
        and len(run.findall(w("commentReference"))) == 1
        and all(child.tag in (w("rPr"), w("commentReference")) for child in run)
    )


def _movable_goback_start(
    paragraph: etree._Element, insertion: etree._Element, marker: etree._Element
) -> bool:
    """Recognize Word's cursor bookmark, never an ordinary document range.

    This narrowly supports a start between runs of our own insertion and its
    unique end later in the same paragraph, outside the insertion. The start
    may move to the replacement's beginning; the end and both identities stay
    intact. Other endpoints, orphan/duplicate ids and cross-paragraph pairs do
    not receive this exception.
    """
    if (
        marker.tag != w("bookmarkStart")
        or marker.get(w("name")) != "_GoBack"
        or marker.getparent() is not insertion
    ):
        return False
    identity = marker.get(w("id"))
    if not identity:
        return False
    starts = [
        node
        for node in paragraph.iter(w("bookmarkStart"))
        if node.get(w("id")) == identity or node.get(w("name")) == "_GoBack"
    ]
    ends = [
        node
        for node in paragraph.iter(w("bookmarkEnd"))
        if node.get(w("id")) == identity
    ]
    return (
        starts == [marker]
        and len(ends) == 1
        and ends[0].getparent() is paragraph
        and paragraph.index(ends[0]) > paragraph.index(insertion)
    )


def _assert_outside_fields(
    paragraph: etree._Element, touched_runs: set[etree._Element]
) -> None:
    """Do not edit a field result merely because its visible run is simple."""
    depth = 0
    # The text-reading traversal intentionally omits field instruction atoms;
    # structural safety must inspect those markers as well.
    for node in paragraph.iter():
        if node.tag == w("fldChar"):
            kind = node.get(w("fldCharType"))
            if kind == "begin":
                depth += 1
            elif kind == "end":
                depth = max(0, depth - 1)
        if node in touched_runs and depth:
            raise ApplyError(
                "unsupported_run_shape", "the matched text is inside a complex field"
            )


def _new_run(template: etree._Element, text: str) -> etree._Element:
    run = copy.deepcopy(template)
    atom = run.find(w("t"))
    atom.text = text
    atom.set(_XML_SPACE, "preserve")
    return run


def _new_revision(kind: str, revision_id: str, author: str) -> etree._Element:
    revision = etree.Element(w(kind))
    revision.set(w("id"), revision_id)
    revision.set(w("author"), author)
    return revision


def _replace_candidate(
    paragraph: etree._Element,
    old_text: str,
    new_text: str,
    author: str,
    allocator: _RevisionIdAllocator,
) -> dict:
    segments = _paragraph_segments(paragraph)
    before = _reading_text(segments)
    start = before.find(old_text)
    if start < 0:
        raise ApplyError(
            "delete_text_not_found",
            "old_text does not occur in the paragraph's current reading",
            match_count=0,
        )
    if before.find(old_text, start + 1) >= 0:
        raise ApplyError(
            "delete_text_ambiguous",
            "old_text occurs more than once in the paragraph's current reading",
            match_count=2,
        )
    end = start + len(old_text)
    touched = [seg for seg in segments if seg.start < end and seg.end > start]
    containers = {seg.container for seg in touched}
    container = next(iter(containers)) if len(containers) == 1 else None
    if all(seg.plain for seg in touched):
        parent = paragraph
        operation = "replace" if new_text else "delete"
    elif (
        len(containers) == 1
        and container is not None
        and container.tag == w("ins")
        and container.getparent() is paragraph
        and all(
            seg.run is not None and seg.run.getparent() is container for seg in touched
        )
    ):
        parent = container
        operation = (
            "edit_own_insertion" if container.get(w("author")) == author else "counter"
        )
        if not container.get(w("id")):
            raise ApplyError(
                "unsupported_run_shape", "the insertion has no revision id"
            )
        # Nested revisions are intentionally not rewritten, including a foreign
        # strike inside our own insertion. Markers wholly outside the edited
        # span can remain in their original position.
        if any(
            child.tag not in _OUTSIDE_MARKERS
            and not _simple_run(child)
            and not (
                operation == "edit_own_insertion" and _comment_reference_run(child)
            )
            for child in parent
        ):
            raise ApplyError(
                "overlaps_tracked_changes",
                "the insertion contains nested revisions or unsupported content",
            )
        if operation == "counter":
            if any(child.tag in _OUTSIDE_MARKERS for child in parent):
                raise ApplyError(
                    "counter_position_unsupported",
                    "a counterparty insertion with range markers cannot be countered",
                )
            insertion_end = max(
                seg.end for seg in segments if seg.container is container
            )
            if end != insertion_end:
                raise ApplyError(
                    "counter_position_unsupported",
                    "a counter edit must end at the end of the counterparty insertion",
                )
    else:
        raise ApplyError(
            "overlaps_tracked_changes",
            "the matched span mixes plain and tracked text or unsupported revision markup",
        )

    touched_runs = {seg.run for seg in touched}
    if any(
        not _simple_run(run) or run.getparent() is not parent for run in touched_runs
    ):
        raise ApplyError(
            "unsupported_run_shape",
            "the matched span requires direct, simple text runs",
        )
    _assert_outside_fields(paragraph, touched_runs)
    children = list(parent)
    first_index = children.index(touched[0].run)
    last_index = children.index(touched[-1].run)
    interleaved = [
        child
        for child in children[first_index : last_index + 1]
        if child not in touched_runs
    ]
    movable_markers = [
        child
        for child in interleaved
        if operation == "edit_own_insertion"
        and _movable_goback_start(paragraph, parent, child)
    ]
    if len(movable_markers) != len(interleaved):
        raise ApplyError(
            "unsupported_run_shape",
            "the matched span crosses a range boundary or non-text markup",
        )

    # Splitting is performed only after all shape checks, on a disposable copy.
    # A split duplicates the original run properties, retaining prefix/suffix
    # styling; new text inherits the first replaced character's run properties.
    _split_run_at(segments, start)
    _split_run_at(_paragraph_segments(paragraph), end)
    covered = [
        seg.run
        for seg in _paragraph_segments(paragraph)
        if seg.start >= start and seg.end <= end and seg.end > seg.start
    ]
    first_index = parent.index(covered[0])
    replacement = _new_run(covered[0], new_text) if new_text else None
    target_revision_id = container.get(w("id")) if container is not None else None
    target_author = container.get(w("author")) if container is not None else None
    if operation == "edit_own_insertion":
        revision_ids = [target_revision_id]
        for run in covered:
            parent.remove(run)
        for marker in movable_markers:
            parent.remove(marker)
            parent.insert(first_index, marker)
            first_index += 1
        if replacement is not None:
            parent.insert(first_index, replacement)
        if not any((atom.text or "") for atom in parent.iter(w("t"))):
            # Preserve any zero-width anchors or empty runs that lay outside
            # the removed text while removing the now-empty revision wrapper.
            for child in list(parent):
                parent.addprevious(child)
            paragraph.remove(parent)
    else:
        revision_ids = list(allocator.reserve(2 if new_text else 1))
        deletion = _new_revision("del", revision_ids[0], author)
        parent.insert(first_index, deletion)
        for run in covered:
            deletion.append(run)
            atom = run.find(w("t"))
            atom.tag = w("delText")
            atom.set(_XML_SPACE, "preserve")
        if replacement is not None:
            insertion = _new_revision("ins", revision_ids[1], author)
            insertion.append(replacement)
            if operation == "counter":
                parent.addnext(insertion)
            else:
                deletion.addnext(insertion)

    after = _reading_text(_paragraph_segments(paragraph))
    if after != before[:start] + new_text + before[end:]:
        raise ApplyError(
            "round_trip_failed",
            "the edited reading differs from the requested replacement",
        )
    result = {
        "operation": operation,
        "old_text": old_text,
        "new_text": new_text,
        "start": start,
        "end": end,
        "revision_ids": revision_ids,
        "target_revision_id": target_revision_id,
        "target_author": target_author,
    }
    if movable_markers:
        result["relocated_bookmarks"] = [
            {
                "name": "_GoBack",
                "id": marker.get(w("id")),
                "position": "replacement_start",
            }
            for marker in movable_markers
        ]
    return result


def replace_text(
    paragraph: etree._Element,
    old_text: str,
    new_text: str,
    author: str,
    allocator: _RevisionIdAllocator,
) -> dict:
    """Replace one unique literal span, retaining native tracked-change markup.

    Accepted plain text becomes deletion/insertion markup; our own pending
    insertion is updated in place (same id, author and date). A simple foreign
    insertion may be countered at its suffix, with our deletion nested inside
    theirs and our replacement immediately after it. Other layouts fail closed.

    The paragraph itself retains its identity and attributes. On success its
    child nodes are replaced with the validated candidate, so callers must not
    retain descendant element references across edits. New text inherits the
    first replaced run's formatting. New revision ids are committed only on
    success. Identical old/new strings are rejected as a no-op.
    """
    if not isinstance(paragraph, etree._Element) or paragraph.tag != w("p"):
        raise ApplyError("invalid_edit", "paragraph must be a resolved Word paragraph")
    if not isinstance(old_text, str) or not old_text:
        raise ApplyError("delete_text_missing", "old_text must be a non-empty string")
    if not isinstance(new_text, str):
        raise ApplyError("invalid_edit", "new_text must be a string")
    if not is_xml_text_compatible(old_text) or not is_xml_text_compatible(new_text):
        raise ApplyError(
            "invalid_edit", "replacement contains characters invalid in XML"
        )
    if any(char in new_text for char in "\t\r\n"):
        raise ApplyError(
            "invalid_edit", "new_text must not introduce tabs or line breaks"
        )
    if old_text == new_text:
        raise ApplyError("invalid_edit", "old_text and new_text must differ")
    if len(new_text) > MAX_NEW_TEXT_CHARS_PER_EDIT:
        raise ApplyError(
            "resource_limit_exceeded", "new_text exceeds the per-edit character limit"
        )
    if author_error := tracked_change_author_validation_error(author):
        raise ApplyError("invalid_author", author_error)
    candidate = copy.deepcopy(paragraph)
    candidate_allocator = _RevisionIdAllocator(allocator.next_value)
    result = _replace_candidate(
        candidate, old_text, new_text, author, candidate_allocator
    )
    for child in list(paragraph):
        paragraph.remove(child)
    for child in list(candidate):
        paragraph.append(child)
    allocator.next_value = candidate_allocator.next_value
    return result
