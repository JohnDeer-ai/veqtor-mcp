# SPDX-License-Identifier: Apache-2.0
"""Snapshot-addressed revision decisions; no filesystem publication or authority.

The six property domains are distinct. In particular, pPrChange does not own
paragraph-mark rPr or sectPr. A decision never creates/relabels a revision.
Exact before/after patches support a second, independent collateral proof.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass

from lxml import etree

from ._ooxml import (
    DOCUMENT_PART,
    DocxError,
    canonical_body_flow_v1,
    iter_canonical_paragraph_nodes,
    parse_xml,
    w,
)
from ._paragraph_edits import (
    _PROPERTY_TAGS,
    _REVISION_TAGS,
    _require_style_dependencies,
)
from .apply import ApplyError, _paragraph_segments, _reading_text
from .round_snapshots import capture_document_snapshot

MAX_REVISIONS = 20_000
MAX_REF_DEPTH = 64
MAX_PREVIEW_CHARS = 20_000
MAX_FINGERPRINT_BYTES = 64 * 1024 * 1024
MAX_PAGE_BYTES = 1_048_576
PROPERTY_KINDS = (
    "rPrChange",
    "pPrChange",
    "tcPrChange",
    "trPrChange",
    "tblPrChange",
    "tblGridChange",
)
REF_KEYS = frozenset(
    {
        "schema_version",
        "file_sha256",
        "part_name",
        "element_path",
        "element_sha256",
        "owner_sha256",
        "fingerprint_policy",
    }
)
_PROPERTY_PARENT = {name: w(name.removesuffix("Change")) for name in PROPERTY_KINDS}
_PROPERTY_OWNERS = {
    "rPrChange": {w("r"), w("pPr")},
    "pPrChange": {w("p")},
    "tcPrChange": {w("tc")},
    "trPrChange": {w("tr")},
    "tblPrChange": {w("tbl")},
    "tblGridChange": {w("tbl")},
}
_RPR = "rStyle rFonts b bCs i iCs caps smallCaps strike dstrike outline shadow emboss imprint noProof snapToGrid vanish webHidden color spacing w kern position sz szCs highlight u effect bdr shd fitText vertAlign rtl cs em lang eastAsianLayout specVanish oMath"
_PPR = "pStyle keepNext keepLines pageBreakBefore framePr widowControl numPr suppressLineNumbers pBdr shd tabs suppressAutoHyphens kinsoku wordWrap overflowPunct topLinePunct autoSpaceDE autoSpaceDN bidi adjustRightInd snapToGrid spacing ind contextualSpacing mirrorIndents suppressOverlap jc textDirection textAlignment textboxTightWrap outlineLvl divId cnfStyle"
_TCPR = "cnfStyle tcW gridSpan hMerge vMerge tcBorders shd noWrap tcMar textDirection tcFitText vAlign hideMark headers"
_TRPR = "cnfStyle divId gridBefore gridAfter wBefore wAfter trHeight hidden cantSplit tblHeader tblCellSpacing jc"
_TBLPR = "tblStyle tblpPr tblOverlap bidiVisual tblStyleRowBandSize tblStyleColBandSize tblW jc tblCellSpacing tblInd tblBorders shd tblLayout tblCellMar tblLook tblCaption tblDescription"
_DOMAINS = {
    kind: frozenset(w(n) for n in names.split())
    for kind, names in zip(
        PROPERTY_KINDS, (_RPR, _PPR, _TCPR, _TRPR, _TBLPR, "gridCol"), strict=True
    )
}

_CONTAINERS = {
    w("numPr"): ("ilvl", "numId"),
    w("tabs"): ("tab",),
    w("pBdr"): ("top", "left", "bottom", "right", "between", "bar"),
    w("tcBorders"): (
        "top",
        "start",
        "left",
        "bottom",
        "right",
        "end",
        "insideH",
        "insideV",
        "tl2br",
        "tr2bl",
    ),
    w("tblBorders"): (
        "top",
        "start",
        "left",
        "bottom",
        "right",
        "end",
        "insideH",
        "insideV",
    ),
    w("tcMar"): ("top", "left", "bottom", "right", "start", "end"),
    w("tblCellMar"): ("top", "left", "bottom", "right", "start", "end"),
    w("headers"): ("header",),
}


def _property_tree(node):
    """Closed child grammar: known scalar names cannot hide arbitrary XML."""
    if (node.text or "").strip() or (node.tail or "").strip():
        _fail(
            "property_revision_unsupported", "property markup contains unexpected text"
        )
    allowed = tuple(w(n) for n in _CONTAINERS.get(node.tag, ()))
    if any(child.tag not in allowed for child in node):
        _fail(
            "property_revision_unsupported",
            "property markup has unsupported child placement",
        )
    tags = [n.tag for n in node]
    if node.tag not in {w("tabs"), w("headers")} and len(set(tags)) != len(tags):
        _fail("property_revision_unsupported", "property container contains duplicates")
    if node.tag == w("numPr") and tags != sorted(tags, key=allowed.index):
        _fail(
            "property_revision_unsupported", "numbering properties have invalid order"
        )
    if node.tag == w("tabs"):
        positions = [n.get(w("pos")) for n in node]
        if None in positions or len(set(positions)) != len(positions):
            _fail("property_revision_unsupported", "tab stops need unique positions")
    for child in node:
        _property_tree(child)


def _fail(code, detail):
    raise ApplyError(code, detail)


def _xml(node):
    return etree.tostring(node, method="c14n", with_comments=False, with_tail=False)


def _digest(node):
    return hashlib.sha256(_xml(node)).hexdigest()


def _path(root, node):
    indexes = []
    while node is not root:
        parent = node.getparent()
        if parent is None or len(indexes) >= MAX_REF_DEPTH:
            _fail(
                "revision_reference_unsupported",
                "revision path is outside the supported depth",
            )
        indexes.append(parent.index(node))
        node = parent
    return list(reversed(indexes))


def validate_ref(ref):
    if type(ref) is not dict or set(ref) != REF_KEYS:
        _fail("invalid_revision_reference", "supply a complete revision_ref.v1")
    if (
        ref["schema_version"] != "revision_ref.v1"
        or ref["part_name"] != DOCUMENT_PART
        or ref["fingerprint_policy"] != "element_c14n_without_comments_v1"
    ):
        _fail("invalid_revision_reference", "revision reference policy is unsupported")
    for key in ("file_sha256", "element_sha256", "owner_sha256"):
        value = ref[key]
        if (
            type(value) is not str
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            _fail("invalid_revision_reference", "revision reference requires SHA-256")
    path = ref["element_path"]
    if (
        type(path) is not list
        or not 1 <= len(path) <= MAX_REF_DEPTH
        or any(type(i) is not int or i < 0 or i > 1_000_000 for i in path)
    ):
        _fail("invalid_revision_reference", "revision path is invalid")


def validate_target(target):
    if type(target) is not dict:
        _fail("invalid_revision_target", "supply an explicit revision target")
    if target.get("kind") == "single" and set(target) == {"kind", "revision_ref"}:
        validate_ref(target["revision_ref"])
    elif target.get("kind") == "replacement" and set(target) == {
        "kind",
        "deletion_ref",
        "insertion_ref",
    }:
        validate_ref(target["deletion_ref"])
        validate_ref(target["insertion_ref"])
    else:
        _fail(
            "invalid_revision_target",
            "select one revision or an explicit deletion/insertion pair",
        )


def reference(root, node, sha):
    return {
        "schema_version": "revision_ref.v1",
        "file_sha256": sha,
        "part_name": DOCUMENT_PART,
        "element_path": _path(root, node),
        "element_sha256": _digest(node),
        "owner_sha256": _digest(node.getparent()),
        "fingerprint_policy": "element_c14n_without_comments_v1",
    }


def resolve(root, sha, ref):
    validate_ref(ref)
    if ref["file_sha256"] != sha:
        _fail(
            "revision_reference_mismatch",
            "reread revision references for this exact file",
        )
    node = root
    for index in ref["element_path"]:
        if index >= len(node):
            _fail("revision_reference_mismatch", "revision location changed")
        node = node[index]
    if (
        node.tag not in _REVISION_TAGS
        or _digest(node) != ref["element_sha256"]
        or _digest(node.getparent()) != ref["owner_sha256"]
    ):
        _fail("revision_reference_mismatch", "revision or owner fingerprint changed")
    return node


def resolve_target(root, sha, target):
    validate_target(target)
    if target["kind"] == "single":
        return [resolve(root, sha, target["revision_ref"])]
    nodes = [
        resolve(root, sha, target[key]) for key in ("deletion_ref", "insertion_ref")
    ]
    if (
        nodes[0].tag != w("del")
        or nodes[1].tag != w("ins")
        or nodes[0].getnext() is not nodes[1]
    ):
        _fail(
            "replacement_unit_unsupported",
            "replacement must name adjacent deletion then insertion under one parent",
        )
    return nodes


def _owner(node):
    if etree.QName(node).localname in PROPERTY_KINDS:
        return node.getparent().getparent()
    return node.getparent()


def overlaps(left, right):
    return (
        left is right or left in right.iterancestors() or right in left.iterancestors()
    )


def decision_region(nodes):
    owner = _owner(nodes[0])
    # Decisions in a paragraph conflict with new edits throughout that paragraph.
    paragraph = next(
        (n for n in (owner, *owner.iterancestors()) if n.tag == w("p")), None
    )
    return paragraph if paragraph is not None else owner


def _text(node):
    value = "".join(
        (n.text or "")
        if n.tag in {w("t"), w("delText")}
        else "\t"
        if n.tag == w("tab")
        else "\n"
        if n.tag in {w("br"), w("cr")}
        else ""
        for n in node.iter()
    )
    if len(value) > MAX_PREVIEW_CHARS:
        _fail(
            "resource_limit_exceeded",
            "selected revision preview exceeds its text limit",
        )
    return value


def _context_text(owner):
    paragraphs = [owner] if owner.tag == w("p") else list(owner.iter(w("p")))
    value = "\n".join(_reading_text(_paragraph_segments(p)) for p in paragraphs)
    if len(value) > MAX_PREVIEW_CHARS:
        _fail("resource_limit_exceeded", "affected text exceeds the preview limit")
    return value


def _main_context(root, owner):
    flow = canonical_body_flow_v1(root.find(w("body")))
    if flow.exclusion_kind_for(owner) is not None:
        _fail(
            "revision_context_unsupported",
            "revision is outside canonical main-story content",
        )
    body = root.find(w("body"))
    node = owner
    while node is not body:
        if node is None or node.tag not in {
            w("p"),
            w("r"),
            w("pPr"),
            w("tc"),
            w("tr"),
            w("tbl"),
        }:
            _fail(
                "revision_context_unsupported",
                "revision has an unsupported owner or ancestor",
            )
        node = node.getparent()
    for ancestor in (owner, *owner.iterancestors()):
        for props in ancestor:
            if props.tag in {w("pPr"), w("trPr"), w("tcPr"), w("tblPr")} and any(
                n.tag
                in {w("ins"), w("del"), w("cellIns"), w("cellDel"), w("cellMerge")}
                for n in props.iter()
            ):
                _fail(
                    "revision_dependency_unsupported",
                    "target belongs to a structural revision",
                )
    # Permission and move ranges may begin outside the selected paragraph.
    canonical = {
        n
        for item in flow.paragraphs
        for n in iter_canonical_paragraph_nodes(item.element)
    }
    active = set()
    for event, item in etree.iterwalk(body, events=("start", "end")):
        if event == "start" and item is owner and active:
            _fail(
                "revision_dependency_unsupported",
                "a permission or revision range encloses the target",
            )
        if event != "start" or item.getparent() not in canonical:
            continue
        name = etree.QName(item).localname if isinstance(item.tag, str) else ""
        if name in {"permStart", "permEnd"} or (
            item.tag in _REVISION_TAGS and "Range" in name
        ):
            base = name.removesuffix("Start").removesuffix("End")
            key = (base, item.get(w("id")))
            if name.endswith("Start"):
                active.add(key)
            else:
                active.discard(key)
            if overlaps(owner, item):
                _fail(
                    "revision_dependency_unsupported",
                    "a permission or revision range touches the target",
                )


def _validate_text(root, nodes):
    parent = nodes[0].getparent()
    if parent.tag != w("p") or any(n.getparent() is not parent for n in nodes):
        _fail(
            "revision_dependency_unsupported",
            "only isolated direct paragraph text revisions are supported",
        )
    _main_context(root, parent)
    for node in nodes:
        if (
            node.tag not in {w("ins"), w("del")}
            or not len(node)
            or (node.text or "").strip()
            or (node.tail or "").strip()
        ):
            _fail(
                "revision_type_unsupported",
                "selected node is not an isolated text revision",
            )
        for child in node:
            if (
                child.tag != w("r")
                or len(child.findall(w("rPr"))) > 1
                or (child.text or "").strip()
                or (child.tail or "").strip()
            ):
                _fail(
                    "revision_dependency_unsupported",
                    "text revision contains a nested revision or anchor",
                )
            for atom in child:
                if (atom.tail or "").strip():
                    _fail(
                        "revision_dependency_unsupported",
                        "run contains text outside a supported atom",
                    )
                expected = w("t") if node.tag == w("ins") else w("delText")
                if atom.tag == w("rPr"):
                    if any(
                        n.tag not in _PROPERTY_TAGS or n.tag in _REVISION_TAGS
                        for n in atom.iter()
                    ):
                        _fail(
                            "revision_dependency_unsupported",
                            "text revision contains pending or unknown formatting",
                        )
                elif atom.tag not in {
                    expected,
                    w("tab"),
                    w("br"),
                    w("cr"),
                    w("lastRenderedPageBreak"),
                } or len(atom):
                    _fail(
                        "revision_dependency_unsupported",
                        "text revision contains unsupported atoms or anchors",
                    )
        _text(node)
    # Even anchors outside the wrapper can be field-linked. Reuse the existing
    # field safety check without its blanket table-property revision veto.
    from ._drafting_text import _assert_outside_fields

    _assert_outside_fields(parent, {run for node in nodes for run in node})


def _validate_property(root, node):
    kind = etree.QName(node).localname
    props = node.getparent()
    if (
        kind not in PROPERTY_KINDS
        or props.tag != _PROPERTY_PARENT[kind]
        or props.getparent().tag not in _PROPERTY_OWNERS[kind]
        or len(props.findall(node.tag)) != 1
        or props[-1] is not node
        or len(node) != 1
        or node[0].tag != props.tag
    ):
        _fail(
            "property_revision_unsupported",
            "property revision has no unique matching previous property set",
        )
    if (node.text or "").strip() or (node.tail or "").strip():
        _fail(
            "property_revision_unsupported", "property history contains unexpected text"
        )
    _main_context(root, props.getparent())
    preserved = {w("rPr"), w("sectPr")} if kind == "pPrChange" else set()
    for old in (False, True):
        current = node[0] if old else props
        if current.attrib or (current.text or "").strip():
            _fail(
                "property_revision_unsupported",
                "property-set attributes or text are unsupported",
            )
        for child in current:
            if child is node:
                continue
            if not old and child.tag in preserved:
                continue
            if child.tag not in _DOMAINS[kind]:
                _fail(
                    "property_revision_unsupported",
                    "property lies outside the selected property's domain",
                )
            _property_tree(child)
            if any(
                n.tag not in _PROPERTY_TAGS
                or n.tag in _REVISION_TAGS
                or (n.text or "").strip()
                for n in child.iter()
            ):
                _fail(
                    "revision_dependency_unsupported",
                    "property history contains an unknown or dependent change",
                )
        tags = [n.tag for n in current if n is not node and n.tag not in preserved]
        if kind in {"rPrChange", "pPrChange", "tcPrChange", "tblPrChange"}:
            names = {
                "rPrChange": _RPR,
                "pPrChange": _PPR,
                "tcPrChange": _TCPR,
                "tblPrChange": _TBLPR,
            }[kind]
            order = [w(n) for n in names.split()]
            if tags != sorted(tags, key=order.index):
                _fail(
                    "property_revision_unsupported",
                    "property sequence is outside the supported grammar",
                )
        if kind != "tblGridChange" and len(tags) != len(set(tags)):
            _fail(
                "property_revision_unsupported",
                "duplicate properties have ambiguous semantics",
            )
    return kind


def _grid(table):
    """Validate row coverage and merge topology against the resulting XML grid."""
    for node in table.iter():
        if node.tag in {w("cellIns"), w("cellDel"), w("cellMerge")} or (
            node.tag in {w("ins"), w("del")} and node.getparent().tag == w("trPr")
        ):
            _fail(
                "revision_dependency_unsupported",
                "grid depends on a pending row or cell existence change",
            )
        if node.tag in {w("tcPrChange"), w("trPrChange")}:
            geometry = {
                w(n)
                for n in ("gridSpan", "hMerge", "vMerge", "gridBefore", "gridAfter")
            }
            current = [_xml(n) for n in node.getparent() if n.tag in geometry]
            old = (
                [_xml(n) for n in node[0] if n.tag in geometry]
                if len(node) == 1
                else None
            )
            if current != old:
                _fail(
                    "revision_dependency_unsupported",
                    "grid depends on an unselected geometry revision",
                )
    grids = table.findall(w("tblGrid"))
    if len(grids) != 1 or not grids[0].findall(w("gridCol")):
        _fail("table_grid_incompatible", "table has no unique nonempty grid")
    count = len(grids[0].findall(w("gridCol")))
    if count > 256:
        _fail("resource_limit_exceeded", "table grid exceeds its column limit")
    for col in grids[0].findall(w("gridCol")):
        width = col.get(w("w"))
        if width is not None and (not width.isascii() or not width.isdecimal()):
            _fail("table_grid_incompatible", "invalid column width")
    from .table_map import _integer, _merge, _merge_issues

    if any(child.tag not in {w("tblPr"), w("tblGrid"), w("tr")} for child in table):
        _fail(
            "table_grid_incompatible",
            "table has unsupported row wrappers or boundaries",
        )
    rows = []
    for row in table.findall(w("tr")):
        if any(child.tag not in {w("trPr"), w("tblPrEx"), w("tc")} for child in row):
            _fail(
                "table_grid_incompatible",
                "row has unsupported cell wrappers or boundaries",
            )
        props = row.find(w("trPr"))
        cursor = _integer(props, "gridBefore", 0)
        cells = []
        for cell in row.findall(w("tc")):
            cp = cell.find(w("tcPr"))
            span = _integer(cp, "gridSpan", 1)
            cells.append(
                {
                    "grid_start": cursor,
                    "grid_span": span,
                    "h_merge": _merge(cp, "hMerge"),
                    "v_merge": _merge(cp, "vMerge"),
                    "issues": [],
                }
            )
            cursor += span
        if not cells or cursor + _integer(props, "gridAfter", 0) != count:
            _fail(
                "table_grid_incompatible",
                "previous properties do not match the actual cells and omitted columns",
            )
        rows.append({"cells": cells})
    if not rows:
        _fail("table_grid_incompatible", "table has no direct rows")
    _merge_issues(rows)
    if any(c["issues"] for r in rows for c in r["cells"]):
        _fail(
            "table_grid_incompatible",
            "restored properties do not match the existing merge chains",
        )


def _outcome(nodes, decision, kind):
    if kind in PROPERTY_KINDS:
        props = copy.deepcopy(nodes[0].getparent())
        change = props.find(w(kind))
        if decision == "accept":
            props.remove(change)
        else:
            previous = list(change[0])
            retained = [
                n
                for n in props
                if kind == "pPrChange" and n.tag in {w("rPr"), w("sectPr")}
            ]
            for n in list(props):
                props.remove(n)
            props.extend(previous)
            props.extend(retained)
        return [props]
    result = []
    for node in nodes:
        keep = (node.tag == w("ins")) == (decision == "accept")
        if keep:
            for child in node:
                clone = copy.deepcopy(child)
                if node.tag == w("del"):
                    for atom in clone.iter(w("delText")):
                        atom.tag = w("t")
                result.append(clone)
    return result


def _replace_span(parent, index, old, new):
    for child in old:
        parent.remove(child)
    for offset, child in enumerate(new):
        parent.insert(index + offset, child)


@dataclass
class DecisionPatch:
    parent: etree._Element
    index: int
    before: list[etree._Element]
    after: list[etree._Element]

    def reverse(self, parent):
        present = list(parent)[self.index : self.index + len(self.after)]
        if [etree.tostring(n) for n in present] != [
            etree.tostring(n) for n in self.after
        ]:
            _fail(
                "round_trip_failed",
                "decided revision differs from its verified postimage",
            )
        _replace_span(parent, self.index, present, copy.deepcopy(self.before))


def _property_view(props, kind):
    clone = copy.deepcopy(props)
    change = clone.find(w(kind))
    if change is not None:
        clone.remove(change)
    # XML is data in the preview, never an instruction or a write request.
    value = etree.tostring(clone, encoding="unicode", with_tail=False)
    if len(value) > MAX_PREVIEW_CHARS:
        _fail("resource_limit_exceeded", "property preview exceeds its character limit")
    return value


def plan_decision(root, nodes, decision, parts):
    """Describe both outcomes, validate dependencies independently, select one."""
    if decision not in ("accept", "reject"):
        _fail("invalid_edit", "decision must be accept or reject")
    kind = etree.QName(nodes[0]).localname
    if len(nodes) == 2 or kind in {"ins", "del"}:
        _validate_text(root, nodes)
        kind = "replacement" if len(nodes) == 2 else kind
        old_nodes = nodes
    else:
        if kind not in PROPERTY_KINDS:
            _fail(
                "revision_type_unsupported",
                "this revision type needs a separate structural decision workflow",
            )
        kind = _validate_property(root, nodes[0])
        old_nodes = [nodes[0].getparent()]
    parent = old_nodes[0].getparent()
    index = parent.index(old_nodes[0])
    outcomes = {}
    refusals = {}
    for action in ("accept", "reject"):
        result = _outcome(nodes, action, kind)
        try:
            if kind in PROPERTY_KINDS:
                # Inspect on a disposable complete document: style and grid
                # dependencies are rooted outside the property subtree itself.
                clone = copy.deepcopy(root)
                owner = clone
                for i in _path(root, parent):
                    owner = owner[i]
                _replace_span(owner, index, [owner[index]], copy.deepcopy(result))
                table = next(
                    (n for n in (owner, *owner.iterancestors()) if n.tag == w("tbl")),
                    None,
                )
                if table is not None and kind in {
                    "tcPrChange",
                    "trPrChange",
                    "tblGridChange",
                }:
                    _grid(table)
                paragraphs = (
                    [owner] if owner.tag == w("p") else list(owner.iter(w("p")))
                )
                if owner.tag in {w("r"), w("pPr")}:
                    paragraphs = [next(owner.iterancestors(w("p")))]
                for para in paragraphs:
                    _require_style_dependencies(parts, para)
        except DocxError as exc:
            refusals[action] = getattr(exc, "code", "revision_dependency_unsupported")
        else:
            refusals[action] = None
        outcomes[action] = result
    if refusals[decision] is not None:
        _fail(
            refusals[decision],
            "the selected outcome has an unsupported property dependency",
        )
    facts = {
        "accept_refusal_code": refusals["accept"],
        "reject_refusal_code": refusals["reject"],
        "operation": "decide_revision",
        "decision": decision,
        "revision_kind": kind,
        "members": [
            {
                "kind": etree.QName(n).localname,
                "id": n.get(w("id")),
                "author": n.get(w("author")),
                "date": n.get(w("date")),
            }
            for n in nodes
        ],
        "current_text": _context_text(decision_region(nodes))
        if kind in PROPERTY_KINDS
        else _text(nodes[-1])
        if kind == "replacement"
        else _text(nodes[0])
        if kind == "ins"
        else "",
        "accepted_text": "".join(_text(n) for n in outcomes["accept"])
        if kind not in PROPERTY_KINDS
        else None,
        "rejected_text": "".join(_text(n) for n in outcomes["reject"])
        if kind not in PROPERTY_KINDS
        else None,
        "current_properties": _property_view(nodes[0].getparent(), kind)
        if kind in PROPERTY_KINDS
        else None,
        "previous_properties": _property_view(outcomes["reject"][0], kind)
        if kind in PROPERTY_KINDS
        else None,
    }
    chosen = outcomes[decision]
    return (
        DecisionPatch(parent, index, copy.deepcopy(old_nodes), copy.deepcopy(chosen)),
        chosen,
        facts,
    )


def apply_decision(root, nodes, decision, parts):
    patch, chosen, facts = plan_decision(root, nodes, decision, parts)
    present = list(patch.parent)[patch.index : patch.index + len(patch.before)]
    _replace_span(patch.parent, patch.index, present, chosen)
    return patch, facts


def read_document_revisions(path, file_sha256=None, *, offset=0, max_items=100):
    if (
        type(offset) is not int
        or offset < 0
        or type(max_items) is not int
        or not 1 <= max_items <= 100
    ):
        _fail("invalid_request", "revision pagination is outside its limits")
    from .round_snapshots import _path as snapshot_path

    path = snapshot_path(path)
    snapshot = capture_document_snapshot(path, file_sha256)
    import json

    from ._ooxml import load_validated_docx

    package = load_validated_docx(snapshot.payload, capture=None)
    root = parse_xml(package.parts[DOCUMENT_PART])
    nodes = [n for n in root.iter() if n.tag in _REVISION_TAGS]
    if len(nodes) > MAX_REVISIONS:
        _fail("resource_limit_exceeded", "revision inventory exceeds its item limit")
    items = []
    fingerprint_bytes = 0
    planning_bytes = 0
    document_bytes = len(_xml(root))
    for node in nodes[offset : offset + max_items]:
        fingerprint_bytes += len(_xml(node)) + len(_xml(node.getparent()))
        if fingerprint_bytes > MAX_FINGERPRINT_BYTES:
            _fail(
                "resource_limit_exceeded",
                "revision reference fingerprint budget exceeded",
            )
        ref = reference(root, node, snapshot.sha256)
        refusal = None
        preview = None
        try:
            planning_bytes += (
                2 * document_bytes
                if etree.QName(node).localname in PROPERTY_KINDS
                else 0
            )
            if planning_bytes > MAX_FINGERPRINT_BYTES:
                _fail(
                    "resource_limit_exceeded",
                    "reduce revision page size to bound outcome validation",
                )
            _, _, preview = plan_decision(root, [node], "accept", package.parts)
        except DocxError as exc:
            refusal = getattr(exc, "code", "revision_dependency_unsupported")
        items.append(
            {
                "revision_ref": ref,
                "kind": etree.QName(node).localname,
                "id": node.get(w("id")),
                "author": node.get(w("author")),
                "date": node.get(w("date")),
                "supported": refusal is None,
                "refusal_code": refusal,
                "preview": preview,
            }
        )
    result = {
        "schema_version": "document_revisions.v1",
        "source_path": str(path),
        "file_sha256": snapshot.sha256,
        "revisions": items,
        "total_count": len(nodes),
        "next_offset": offset + len(items)
        if offset + len(items) < len(nodes)
        else None,
        "coverage": "main_document_only",
        "uninspected_parts": sorted(
            name
            for name in package.parts
            if name.startswith("word/")
            and name.endswith(".xml")
            and name != DOCUMENT_PART
        ),
    }
    if len(json.dumps(result, ensure_ascii=True).encode()) > MAX_PAGE_BYTES:
        _fail(
            "resource_limit_exceeded",
            "revision page exceeds its output budget; request fewer items",
        )
    return result
