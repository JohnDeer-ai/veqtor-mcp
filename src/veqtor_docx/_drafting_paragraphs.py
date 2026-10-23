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
import difflib
import re
from collections.abc import Callable

from lxml import etree

from ._drafting_text import (
    _comment_reference_run,
    _assert_outside_fields,
    _layout_run,
    _new_revision,
    _new_run,
    _simple_run,
    _split_run_at,
    _replace_candidate,
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
MAX_DIFF_TOKEN_PAIRS = 1_000_000
_TOKENS = re.compile(r"\w+|\s+|[^\w\s]", re.UNICODE)
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



def _without_mark(paragraph: etree._Element, tag: str) -> None:
    props = paragraph.find(w("pPr"))
    run_props = props.find(w("rPr")) if props is not None else None
    if run_props is not None:
        for mark in list(run_props.findall(w(tag))):
            run_props.remove(mark)
        if not len(run_props) and not run_props.attrib:
            props.remove(run_props)
    if props is not None and not len(props) and not props.attrib:
        paragraph.remove(props)


def _structure_paragraph(paragraph: etree._Element, selected=()) -> None:
    """Closed local grammar; range endpoints in old deletions are still unsafe."""
    chosen = set(selected)
    from .revision_decisions import _PPR, _RPR, _property_tree
    props = paragraph.find(w("pPr"))
    if props is not None:
        ordinary = {w(name) for name in _PPR.split()} | {w("rPr")}
        tags = [child.tag for child in props]
        if len(set(tags)) != len(tags) or any(tag not in ordinary for tag in tags):
            raise ApplyError("paragraph_structure_unsupported", "unsupported or duplicate paragraph property")
        for child in props:
            if child.tag != w("rPr"):
                _property_tree(child)
    for run_props in paragraph.iter(w("rPr")):
        tags = [child.tag for child in run_props if child not in chosen]
        if len(tags) != len(set(tags)) or any(tag not in {w(name) for name in _RPR.split()} for tag in tags):
            raise ApplyError("paragraph_structure_unsupported", "unsupported or duplicate character property")
        for child in run_props:
            if child not in chosen:
                _property_tree(child)
    if len(paragraph.findall(w("pPr"))) > 1:
        raise ApplyError("paragraph_structure_unsupported", "duplicate paragraph properties")
    for node in paragraph.iter():
        if node in chosen:
            continue
        if node.tag in _REVISION_TAGS or (
            isinstance(node.tag, str) and etree.QName(node).localname.endswith("Change")
        ):
            raise ApplyError("paragraph_structure_unsupported", "unselected revision touches the structural unit")
        if node.tag in {w("sectPr"), w("framePr")} or (
            node.tag not in _PROPERTY_TAGS | {w("p"), w("r"), w("t"), w("delText"),
                                             w("tab"), w("br"), w("cr"), w("lastRenderedPageBreak")}
        ):
            raise ApplyError("paragraph_structure_unsupported", "paragraph contains a field, range or unknown structure")
    for child in paragraph:
        if child.tag != w("pPr") and child.tag != w("r") and child not in chosen:
            raise ApplyError("paragraph_structure_unsupported", "unsupported paragraph child")
    for node in paragraph.iter():
        if node is not paragraph and node.tag == w("p"):
            raise ApplyError("paragraph_structure_unsupported", "nested paragraphs are unsupported")
        if node.tag == w("pPr") and node.getparent() is not paragraph:
            raise ApplyError("paragraph_structure_unsupported", "misplaced paragraph properties")
        if node.tag == w("r"):
            if node.getparent() is not paragraph and node.getparent() not in chosen:
                raise ApplyError("paragraph_structure_unsupported", "misplaced run")
            if len(node.findall(w("rPr"))) > 1 or any(atom.tag not in {
                w("rPr"), w("t"), w("delText"), w("tab"), w("br"), w("cr"), w("lastRenderedPageBreak")
            } for atom in node):
                raise ApplyError("paragraph_structure_unsupported", "unsupported run grammar")
        if node.tag in {w("t"), w("delText"), w("tab"), w("br"), w("cr"), w("lastRenderedPageBreak")} and (
            node.getparent().tag != w("r") or len(node)
        ):
            raise ApplyError("paragraph_structure_unsupported", "misplaced or nested text atom")
        if node.tag in {w("t"), w("delText")}:
            run_parent = node.getparent().getparent()
            expected = w("delText") if run_parent in chosen and run_parent.tag == w("del") else w("t")
            if node.tag != expected:
                raise ApplyError("paragraph_structure_unsupported", "text atom does not match its revision context")
    for revision in selected:
        if revision.tag not in {w("ins"), w("del")}:
            raise ApplyError("paragraph_structure_unsupported", "unsupported selected structural revision")
        if revision.getparent() is paragraph and (
            not len(revision) or any(child.tag != w("r") for child in revision)
        ):
            raise ApplyError("paragraph_structure_unsupported", "selected text must contain direct runs")


def paragraph_structure_unit(root: etree._Element, nodes: list) -> dict:
    """Resolve an explicitly complete mark/text unit by shape, never by author."""
    if not 2 <= len(nodes) <= 65 or len(set(nodes)) != len(nodes):
        raise ApplyError("paragraph_unit_unsupported", "select one mark and every related text revision")
    mark, *texts = nodes
    mark_props = mark.getparent()
    props = mark_props.getparent() if mark_props is not None else None
    paragraph = props.getparent() if props is not None else None
    if (
        mark.tag not in {w("ins"), w("del")} or len(mark)
        or mark_props is None or mark_props.tag != w("rPr")
        or props is None or props.tag != w("pPr")
        or paragraph is None or paragraph.tag != w("p")
    ):
        raise ApplyError("paragraph_unit_unsupported", "selected mark is not a simple paragraph separator")
    parent, following = paragraph.getparent(), paragraph.getnext()
    if parent is None or parent.tag not in {w("body"), w("tc")} or (
        following is None or following.tag != w("p")
    ):
        raise ApplyError("paragraph_boundary_unsupported", "a structural mark requires its next paragraph")
    _assert_insertion_boundary(paragraph, "before")
    _assert_insertion_boundary(following, "after")
    candidates = [paragraph] if mark.tag == w("del") else [paragraph, following]
    matched = []
    for content in candidates:
        children = [child for child in content if child.tag != w("pPr")]
        if children == texts and children and all(child.tag == mark.tag for child in children):
            matched.append(content)
    if len(matched) != 1:
        raise ApplyError("paragraph_unit_unsupported", "text references do not name the entire unique paragraph proposal")
    content = matched[0]
    _structure_paragraph(paragraph, [mark, *texts] if content is paragraph else [mark])
    _structure_paragraph(following, texts if content is following else [])
    expected_atom = w("t") if mark.tag == w("ins") else w("delText")
    for text in texts:
        for run in text:
            for atom in run:
                if atom.tag != w("rPr") and atom.tag not in {
                    expected_atom, w("tab"), w("br"), w("cr"), w("lastRenderedPageBreak")
                }:
                    raise ApplyError("paragraph_unit_unsupported", "text revision has incompatible atoms")
    return {"mark": mark, "texts": texts, "paragraph": paragraph,
            "following": following, "content": content,
            "kind": "paragraph_insertion" if mark.tag == w("ins") else "paragraph_deletion"}


def paragraph_structure_outcome(unit: dict, decision: str) -> list:
    """Produce an exact two-paragraph patch, retaining the surviving mark's pPr."""
    original = [unit["paragraph"], unit["following"]]
    copies = [copy.deepcopy(p) for p in original]
    selected = dict(zip((n for p in original for n in p.iter()),
                        (n for p in copies for n in p.iter()), strict=True))
    content = selected[unit["content"]]
    remove_content = (unit["kind"] == "paragraph_insertion") != (decision == "accept")
    if remove_content:
        copies.remove(content)
    else:
        for source in unit["texts"]:
            revision = selected[source]
            index = content.index(revision)
            for offset, run in enumerate(list(revision)):
                for atom in run.iter(w("delText")):
                    atom.tag = w("t")
                content.insert(index + offset, run)
            content.remove(revision)
    # A mark on the retained anchor must be removed as well (insert-after).
    marked = selected[unit["paragraph"]]
    if marked in copies:
        _without_mark(marked, "ins" if unit["kind"] == "paragraph_insertion" else "del")
    return copies


def paragraph_structure_candidates(mark: etree._Element) -> list:
    """Candidate discovery is read-only; the caller must explicitly select all refs."""
    try:
        paragraph = mark.getparent().getparent().getparent()
        if paragraph.tag != w("p"):
            return []
        candidates = [paragraph]
        if mark.tag == w("ins") and paragraph.getnext() is not None:
            candidates.append(paragraph.getnext())
        valid = []
        for content in candidates:
            texts = [n for n in content if n.tag != w("pPr")]
            try:
                paragraph_structure_unit(paragraph.getroottree().getroot(), [mark, *texts])
            except ApplyError:
                continue
            valid.append([mark, *texts])
        return valid[0] if len(valid) == 1 else []
    except (AttributeError, TypeError):
        return []


def delete_paragraph(paragraph: etree._Element, old_text: str, author: str,
                     allocator: _RevisionIdAllocator, *, timestamp=None) -> dict:
    """Track all accepted text and the separator; never silently clear a clause."""
    date = _validate_common(paragraph, old_text, author, timestamp)
    if not old_text or _reading(paragraph) != old_text:
        raise ApplyError("paragraph_text_mismatch", "supply the complete current paragraph text")
    parent, following = paragraph.getparent(), paragraph.getnext()
    if parent is None or parent.tag != w("body") or following is None or following.tag != w("p"):
        raise ApplyError("paragraph_boundary_unsupported", "deletion requires an internal main-body paragraph")
    _assert_insertion_boundary(paragraph, "before")
    _assert_insertion_boundary(following, "after")
    _structure_paragraph(paragraph)
    _structure_paragraph(following)
    if any(child.tag not in {w("pPr"), w("r")} for child in paragraph):
        raise ApplyError("paragraph_structure_unsupported", "deletion requires accepted direct runs")
    candidate = copy.deepcopy(paragraph)
    props = candidate.find(w("pPr"))
    if props is None:
        props = etree.Element(w("pPr"))
        candidate.insert(0, props)
    run_props = props.find(w("rPr"))
    if run_props is None:
        run_props = etree.SubElement(props, w("rPr"))
    candidate_allocator = _RevisionIdAllocator(allocator.next_value)
    mark_id, text_id = candidate_allocator.reserve(2)
    run_props.append(_new_revision("del", mark_id, author, date))
    deletion = _new_revision("del", text_id, author, date)
    for run in list(candidate):
        if run.tag == w("pPr"):
            continue
        for atom in run.iter(w("t")):
            atom.tag = w("delText")
        deletion.append(run)
    candidate.append(deletion)
    # Full validation precedes mutation and revision-id consumption.
    shadow = copy.deepcopy(parent)
    shadow.replace(shadow[parent.index(paragraph)], copy.deepcopy(candidate))
    shadow_p = shadow[parent.index(paragraph)]
    paragraph_structure_unit(shadow, [shadow_p.find(f"{w('pPr')}/{w('rPr')}/{w('del')}"), shadow_p.find(w("del"))])
    paragraph.clear()
    paragraph.attrib.update(candidate.attrib)
    paragraph.text, paragraph.tail = candidate.text, candidate.tail
    paragraph.extend(list(candidate))
    allocator.next_value = candidate_allocator.next_value
    return {"operation": "delete_paragraph", "old_text": old_text,
            "revision_ids": [mark_id, text_id], "paragraph_mark_revision_id": mark_id,
            "text_revision_id": text_id}

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
    _assert_span_boundaries(paragraph, 0, len(_reading(paragraph)))
    return editable


def _assert_span_boundaries(paragraph: etree._Element, start: int, end: int) -> None:
    """Check endpoints in actual canonical XML order, including hidden revisions.

    A legal bookmark endpoint inside a retained deletion contributes no visible
    text, but still divides the current reading's range. Excluded stories and
    properties must not manufacture endpoints in the main paragraph.
    """
    canonical = list(iter_canonical_paragraph_nodes(paragraph))
    containers = {
        node for node in canonical
        if node is paragraph or node.tag in _CANONICAL_INLINE_PASS_THROUGH
    }
    lengths = {seg.node: len(seg.text) for seg in _paragraph_segments(paragraph)}
    offset = 0
    for node in paragraph.iter():
        is_boundary = (
            node.getparent() in containers
            and (node.tag in _EDGE_MARKERS or _comment_reference_run(node))
        )
        if is_boundary and (
            start < offset < end or start == end == offset
        ):
            raise ApplyError(
                "paragraph_boundary_unsupported",
                "replacement crosses an interior range or comment anchor",
            )
        offset += lengths.get(node, 0)


def _minimal_hunks(old_text: str, new_text: str) -> list[tuple[int, int, str]]:
    """Bounded deterministic token diff; never trade a limit for wholesale edits."""
    old = _TOKENS.findall(old_text)
    new = _TOKENS.findall(new_text)
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < min(len(old), len(new)) - prefix
        and old[len(old) - suffix - 1] == new[len(new) - suffix - 1]
    ):
        suffix += 1
    old_end, new_end = len(old) - suffix, len(new) - suffix
    old_middle, new_middle = old[prefix:old_end], new[prefix:new_end]
    if len(old_middle) * len(new_middle) > MAX_DIFF_TOKEN_PAIRS:
        raise ApplyError(
            "resource_limit_exceeded",
            "minimal paragraph diff exceeds its comparison-work limit; use narrower edits",
        )
    offsets = [0]
    for token in old:
        offsets.append(offsets[-1] + len(token))
    hunks = [
        (offsets[prefix + left], offsets[prefix + right], "".join(new_middle[nleft:nright]))
        for tag, left, right, nleft, nright in difflib.SequenceMatcher(
            None, old_middle, new_middle, autojunk=False
        ).get_opcodes()
        if tag != "equal"
    ]
    if len(hunks) > MAX_PARAGRAPH_EDIT_STEPS:
        raise ApplyError("resource_limit_exceeded", "minimal paragraph diff has too many hunks")
    return hunks


def _mutable_before(before: etree._Element, step: dict) -> etree._Element | None:
    if step["operation"] != "edit_own_insertion":
        return None
    return next(
        node for node in before.iter(w("ins"))
        if node.get(w("id")) == step["target_revision_id"]
    )


def _insert_at(
    paragraph: etree._Element, offset: int, text: str, author: str,
    allocator: _RevisionIdAllocator, date: str | None,
) -> dict:
    segments = [seg for seg in _paragraph_segments(paragraph) if seg.text]
    # Prefer the right-hand run at a boundary: inserted text belongs to the
    # hunk's following context. At the paragraph end, use the final run.
    segment = next((seg for seg in segments if seg.start <= offset < seg.end), None)
    if segment is None and segments and offset == segments[-1].end:
        segment = segments[-1]
    if segment is not None:
        if not _simple_run(segment.run):
            raise ApplyError("unsupported_run_shape", "insertion requires a simple text run")
        _assert_outside_fields(paragraph, {segment.run})
        container = segment.container
        if container is not None:
            if (
                container.tag != w("ins") or container.getparent() is not paragraph
                or container.get(w("author")) != author
            ):
                raise ApplyError(
                    "foreign_insertion_unsupported",
                    "minimal paragraph insertion cannot rewrite another author's proposal",
                )
            # Existing own text is already a pending insertion. Revise only its
            # selected run, preserving the wrapper identity and other runs.
            local = offset - segment.start
            return _replace_candidate(
                paragraph, segment.text, segment.text[:local] + text + segment.text[local:],
                author, allocator, date, start_offset=segment.start,
            )
        if not segment.plain or segment.run.getparent() is not paragraph:
            raise ApplyError("unsupported_run_shape", "insertion requires direct paragraph text")
        template = _new_run(segment.run, text, preserve_format_history=False)
        _split_run_at(segments, offset)
        current = [seg for seg in _paragraph_segments(paragraph) if seg.text]
        right = next((seg.run for seg in current if seg.start == offset), None)
        if right is not None:
            index = paragraph.index(right)
        else:
            index = paragraph.index(current[-1].run) + 1
    else:
        # Empty paragraphs still retain all structural XML and edge anchors.
        _validate_replacement_shape(paragraph, author)
        template = _fresh_run(paragraph, text)
        index = next(
            (i for i, child in enumerate(paragraph)
             if child.tag in {w("bookmarkEnd"), w("commentRangeEnd")}
             or _comment_reference_run(child)),
            len(paragraph),
        )
    revision_id = allocator.reserve(1)[0]
    insertion = _new_revision("ins", revision_id, author, date)
    insertion.append(template)
    paragraph.insert(index, insertion)
    return {
        "operation": "insert", "old_text": "", "new_text": text,
        "start": offset, "end": offset, "revision_ids": [revision_id],
        "target_revision_id": None, "target_author": None,
    }


def _replace_minimal(
    candidate: etree._Element, old_text: str, new_text: str, author: str,
    allocator: _RevisionIdAllocator, date: str | None,
    verify_step: VerifyStep | None, steps: list[dict],
) -> None:
    hunks = _minimal_hunks(old_text, new_text)
    for start, end, replacement in reversed(hunks):
        _assert_span_boundaries(candidate, start, end)
        if start == end:
            before = copy.deepcopy(candidate)
            step = _insert_at(candidate, start, replacement, author, allocator, date)
            _verify(before, candidate, _mutable_before(before, step), step, verify_step, steps)
            continue
        touched = [
            seg for seg in _paragraph_segments(candidate)
            if seg.start < end and seg.end > start
        ]
        groups: list[list] = []
        for seg in touched:
            if not _simple_run(seg.run):
                raise ApplyError(
                    "unsupported_run_shape", "minimal paragraph replacement requires simple text runs"
                )
            if seg.container is not None and (
                seg.container.tag != w("ins")
                or seg.container.get(w("author")) != author
            ):
                raise ApplyError(
                    "foreign_insertion_unsupported",
                    "minimal paragraph replacement cannot discard another author's insertion",
                )
            if (
                groups and groups[-1][-1].run.getparent() is seg.run.getparent()
                and groups[-1][-1].run.getnext() is seg.run
            ):
                groups[-1].append(seg)
            else:
                groups.append([seg])
        if len(steps) + len(groups) > MAX_PARAGRAPH_EDIT_STEPS:
            raise ApplyError("resource_limit_exceeded", "paragraph replacement has too many revision spans")
        # Remove later ownership/physical spans first. The first span receives
        # the replacement in its original position, never beyond a hidden
        # deletion or endpoint that survives the edit.
        for index in range(len(groups) - 1, -1, -1):
            group = groups[index]
            left, right = max(start, group[0].start), min(end, group[-1].end)
            before = copy.deepcopy(candidate)
            step = _replace_candidate(
                candidate, _reading(candidate)[left:right],
                replacement if index == 0 else "", author, allocator, date,
                start_offset=left,
            )
            _verify(before, candidate, _mutable_before(before, step), step, verify_step, steps)


def _verify(
    before: etree._Element,
    after: etree._Element,
    mutable: etree._Element | None,
    step: dict,
    verify_step: VerifyStep | None,
    steps: list[dict],
) -> None:
    if len(steps) >= MAX_PARAGRAPH_EDIT_STEPS:
        raise ApplyError("resource_limit_exceeded", "paragraph replacement has too many revision spans")
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
    mode: str = "minimal",
    timestamp: str | None = None,
    verify_step: VerifyStep | None = None,
) -> dict:
    """Replace the entire current reading while retaining the paragraph mark.

    ``old_text`` must match exactly, including spaces; an empty paragraph can be
    filled and ``new_text=''`` deletes visible text while retaining its mark.
    By default only changed word/space/punctuation spans become tracked edits;
    unchanged runs retain their formatting. Explicit ``mode='whole'`` replaces
    all visible text. Own pending insertions may be revised/removed; touched
    foreign insertions and crossed interior anchors are refused. Existing hidden
    deletions and paragraph properties survive. Complex diffs fail closed.

    An optional independent verifier runs after every step on disposable XML.
    Its arguments are a before-paragraph copy, the candidate after the step,
    the mutable own revision from that before copy (or None), and step facts.
    Failure leaves the supplied paragraph and allocator unchanged.
    """
    date = _validate_common(paragraph, new_text, author, timestamp)
    if mode not in ("minimal", "whole"):
        raise ApplyError("invalid_edit", "mode must be minimal or whole")
    if not isinstance(old_text, str) or not is_xml_text_compatible(old_text):
        raise ApplyError("invalid_edit", "old_text must be an XML-compatible string")
    if _reading(paragraph) != old_text:
        raise ApplyError(
            "paragraph_text_mismatch", "old_text does not equal the paragraph's entire current reading"
        )
    if old_text == new_text:
        raise ApplyError("invalid_edit", "old_text and new_text must differ")
    if len(old_text) > MAX_NEW_TEXT_CHARS_PER_EDIT:
        raise ApplyError("resource_limit_exceeded", "old_text exceeds the per-edit character limit")

    candidate = copy.deepcopy(paragraph)
    candidate_allocator = _RevisionIdAllocator(allocator.next_value)
    steps: list[dict] = []
    segments = [segment for segment in _paragraph_segments(candidate) if segment.text]
    containers = {segment.container for segment in segments}
    own = next(iter(containers)) if len(containers) == 1 else None
    if mode == "minimal":
        _replace_minimal(
            candidate, old_text, new_text, author, candidate_allocator, date, verify_step, steps
        )
    elif (
        old_text
        and own is not None
        and own.tag == w("ins")
        and own.get(w("author")) == author
        and own.getparent() is candidate
    ):
        # Preserve the existing insertion's id/date and native supported
        # bookmarks/formatting when it contains the complete current reading.
        _assert_span_boundaries(candidate, 0, len(old_text))
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
        "mode": mode,
        "old_text": old_text,
        "new_text": new_text,
        "revision_ids": list(dict.fromkeys(
            revision_id for step in steps for revision_id in step["revision_ids"]
        )),
        "steps": steps,
    }
