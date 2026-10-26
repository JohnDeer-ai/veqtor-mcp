# SPDX-License-Identifier: Apache-2.0
"""Closed DATE/TIME cache context for revision decisions, never field editing."""

import copy
import re

from ._ooxml import w

_FIELD_MARKUP = frozenset(
    w(name) for name in ("fldChar", "instrText", "delInstrText", "fldSimple")
)
_ON_OFF = frozenset(("true", "false", "1", "0", "on", "off"))
# A single quoted date/time picture, with no other switch, field, sequence
# reference or escaped instruction. The picture is preserved, not evaluated.
_INSTRUCTION = re.compile(
    r"""\s*(DATE|TIME)\s+\\@\s+"([^"\\{}\x00-\x1f`]{1,128})"\s*"""
)


def validation_context(root, nodes):
    """Return a detached equivalent context and facts, or leave old guards active.

    Only direct text revisions wholly inside one complete, unrevised field
    result qualify. Remove its proven control runs on the validation clone;
    the caller then runs all ordinary source-global/structural/run guards.
    Actual patches retain every field control, property and package setting.
    """
    from . import revision_decisions as d
    from .counter_revisions import _closed_attributes

    parent = nodes[0].getparent()
    markers = [node for node in parent.iter() if node.tag in _FIELD_MARKUP]
    if not markers:
        return None

    def refuse():
        d._fail(
            "revision_dependency_unsupported",
            "only a complete isolated DATE/TIME cached result supports this decision",
        )

    controls = []
    for marker in markers:
        run = marker.getparent()
        if (
            run.tag != w("r")
            or run.getparent() is not parent
            or marker.tag not in {w("fldChar"), w("instrText")}
            or len(marker)
            or (marker.tail or "").strip()
            or (run.text or "").strip()
            or (run.tail or "").strip()
            or set(run.attrib) - {w("rsidR"), w("rsidDel"), w("rsidRPr")}
        ):
            refuse()
        children = list(run)
        if children and children[0].tag == w("rPr"):
            props = children.pop(0)
            _closed_attributes(props)
            if any(
                (node.text or "").strip() or (node.tail or "").strip()
                for node in props.iter()
            ):
                refuse()
        if children != [marker]:
            refuse()
        if marker.tag == w("instrText"):
            if set(marker.attrib) - {"{http://www.w3.org/XML/1998/namespace}space"}:
                refuse()
            if marker.get(
                "{http://www.w3.org/XML/1998/namespace}space", "default"
            ) not in {"default", "preserve"}:
                refuse()
        else:
            allowed = {w("fldCharType")}
            if marker.get(w("fldCharType")) == "begin":
                allowed |= {w("fldLock"), w("dirty")}
            if set(marker.attrib) - allowed or (marker.text or "").strip():
                refuse()
            if any(
                marker.get(w(attr)) not in _ON_OFF
                for attr in ("fldLock", "dirty")
                if w(attr) in marker.attrib
            ):
                refuse()
        controls.append(run)
    # No hidden text/revisions between begin and separator, and no second or
    # nested field. Split instruction runs are deliberately outside this slice.
    if (
        len(markers) != 4
        or [node.tag for node in markers]
        != [w("fldChar"), w("instrText"), w("fldChar"), w("fldChar")]
        or [markers[i].get(w("fldCharType")) for i in (0, 2, 3)]
        != ["begin", "separate", "end"]
    ):
        refuse()
    indexes = [parent.index(run) for run in controls]
    if indexes[1] != indexes[0] + 1 or indexes[2] != indexes[1] + 1:
        refuse()
    if any(
        node.getparent() is not parent
        or not indexes[2] < parent.index(node) < indexes[3]
        for node in nodes
    ):
        refuse()
    instruction = markers[1].text or ""
    match = _INSTRUCTION.fullmatch(instruction)
    if match is None or len(instruction) > 512:
        refuse()
    # Keep a bounded complete-document clone. This does not replace the
    # main-story proof with a paragraph-only proof.
    if len(d._xml(root)) * 6 > d.MAX_FINGERPRINT_BYTES:
        d._fail(
            "resource_limit_exceeded",
            "date result validation exceeds the fixed work budget",
        )
    clone = copy.deepcopy(root)
    owner = clone
    for index in d._path(root, parent):
        owner = owner[index]
    mapped = [owner[parent.index(node)] for node in nodes]
    for index in reversed(indexes):
        owner.remove(owner[index])
    facts = {
        "field_kind": match[1],
        "instruction": instruction,
        "field_locked": markers[0].get(w("fldLock")) in {"true", "1", "on"},
        "decision_scope": "cached_result_only",
        "field_controls": "preserved",
    }
    return clone, mapped, facts
