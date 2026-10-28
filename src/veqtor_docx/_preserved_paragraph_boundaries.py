# SPDX-License-Identifier: Apache-2.0
"""Validate a complete pending separator component without deciding its marks.

The detached context view is used only to validate a text-only counter patch.
It is never published. The real paragraphs, properties, marks and ranges remain
under the drafting engine's exact reversible-patch collateral proof.
"""
import copy
import hashlib

from ._ooxml import w
from . import revision_decisions as d

MAX_PARAGRAPHS = 64
MAX_MARKS = 128


class PreservedText(list):
    """Explicit text-only opt-in; ordinary single/replacement targets stay strict."""


def text_outcomes(root, nodes, parts):
    if len(nodes) not in {1, 2} or (len(nodes) == 2 and
            [n.tag for n in nodes] != [w("del"), w("ins")]):
        d._fail("invalid_revision_target", "select one text revision or an ordered deletion/insertion pair")
    from .counter_revisions import _closed_attributes
    for wrapper in nodes:
        _closed_attributes(wrapper)
        for node in wrapper.iter():
            if ((node.text or "").strip() and node.tag not in {w("t"), w("delText")}
                    or (node.tail or "").strip()):
                d._fail("revision_type_unsupported", "text revision contains hidden scalar content")
            if node.tag == w("rPr"):
                tags = [child.tag for child in node]
                if len(tags) != len(set(tags)) or node.getparent()[0] is not node:
                    d._fail("revision_type_unsupported", "ambiguous run formatting")
    clone, selected, facts = validation_view(root, nodes)
    # Ordinary validation still proves adjacency, direct runs, fields, styles
    # and the full story. Only the validated separators are absent on the clone.
    if len(selected) == 2:
        if selected[0].getparent() is not selected[1].getparent() or selected[0].getnext() is not selected[1]:
            d._fail("revision_dependency_unsupported", "replacement members must be adjacent")
    old, outcomes, preview = d._plan_outcomes(clone, selected, parts)
    if old != selected:
        d._fail("revision_type_unsupported", "only direct text revisions may preserve boundaries")
    facts["decision_scope"] = "text_only"
    preview["paragraph_boundary_context"] = facts
    return list(nodes), outcomes, preview


def _marks(paragraph):
    if paragraph is None or paragraph.tag != w("p"):
        return []
    return [n for n in paragraph.findall(f"{w('pPr')}/{w('rPr')}/*")
            if n.tag in {w("ins"), w("del")}]


def validation_view(root, selected):
    """Return a same-address context view and bounded disclosure, or refuse."""
    paragraph = selected[0].getparent()
    if paragraph is None or paragraph.tag != w("p"):
        d._fail("paragraph_context_unsupported", "counter requires a direct paragraph")
    parent = paragraph.getparent()
    if parent is None or parent.tag != w("tc"):
        d._fail("paragraph_context_unsupported", "preserved separators require one table cell")
    first = paragraph
    count = 1
    while _marks(first.getprevious()):
        first = first.getprevious()
        count += 1
        if count > MAX_PARAGRAPHS:
            d._fail("resource_limit_exceeded", "separator component exceeds its paragraph limit")
    previous = first.getprevious()
    if previous is not None and previous.tag != w("p") and not (
        previous.tag == w("tcPr") and previous.getprevious() is None
    ):
        d._fail("paragraph_context_unsupported", "separator component has an unproven left boundary")
    component = [first]
    while _marks(component[-1]):
        following = component[-1].getnext()
        if following is None or following.tag != w("p") or following.getparent() is not parent:
            d._fail("paragraph_context_unsupported", "separator component crosses a cell boundary")
        component.append(following)
        if len(component) > MAX_PARAGRAPHS:
            d._fail("resource_limit_exceeded", "separator component exceeds its paragraph limit")
    marks = [mark for item in component for mark in _marks(item)]
    if paragraph not in component or not marks:
        d._fail("paragraph_context_unsupported", "explicit preserved-boundary target needs pending separators")
    if len(marks) > MAX_MARKS or (len(component) + 6) * len(d._xml(root)) > d.MAX_FINGERPRINT_BYTES:
        d._fail("resource_limit_exceeded", "separator context exceeds its fixed validation budget")
    for item in component:
        props = item.findall(w("pPr"))
        if len(props) > 1 or props and (item[0] is not props[0] or len(props[0].findall(w("rPr"))) > 1):
            d._fail("paragraph_context_unsupported", "ambiguous paragraph properties")
        local = _marks(item)
        if any(n.tag in d._REVISION_TAGS and n not in local for prop in props for n in prop.iter()):
            d._fail("paragraph_context_unsupported", "other pending paragraph properties require separate decisions")
        if len({n.tag for n in local}) != len(local):
            d._fail("paragraph_context_unsupported", "duplicate paragraph-mark revision kind")
        for mark in local:
            if (len(mark) or (mark.text or "").strip() or (mark.tail or "").strip()
                    or set(mark.attrib) - {w("id"), w("author"), w("date")}
                    or not mark.get(w("id"), "").isascii()
                    or not mark.get(w("id"), "").isdecimal()
                    or len(mark.get(w("id"), "")) > 10):
                d._fail("paragraph_context_unsupported", "unsupported paragraph-mark revision shape")
    # Map before removal: a marked rPr can contain unrelated properties.
    clone = copy.deepcopy(root)
    mapping = dict(zip(root.iter(), clone.iter(), strict=True))
    for mark in marks:
        mapping[mark].getparent().remove(mapping[mark])
    # Do not clear pending rows, fields, permission/move ranges, or anything
    # else to pass validation. Every paragraph of the full component is checked.
    for item in component:
        d._main_context(clone, mapping[item])
    digest = hashlib.sha256()
    for item in component:
        payload = d._xml(item)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    facts = {
        "decision_scope": "counter_text_only",
        "boundary_policy": "preserve",
        "structural_status": "pending",
        "paragraph_count": len(component),
        "boundary_revision_count": len(marks),
        "component_sha256": digest.hexdigest(),
    }
    return clone, [mapping[n] for n in selected], facts
