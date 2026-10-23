# SPDX-License-Identifier: Apache-2.0
"""Snapshot-addressed revision decisions; no filesystem publication or authority.

The six property domains and row-level table-property exceptions are distinct. In particular, pPrChange does not own
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
    "tblPrExChange",
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
    "tblPrExChange": {w("tr")},
}
_RPR = "rStyle rFonts b bCs i iCs caps smallCaps strike dstrike outline shadow emboss imprint noProof snapToGrid vanish webHidden color spacing w kern position sz szCs highlight u effect bdr shd fitText vertAlign rtl cs em lang eastAsianLayout specVanish oMath"
_PPR = "pStyle keepNext keepLines pageBreakBefore framePr widowControl numPr suppressLineNumbers pBdr shd tabs suppressAutoHyphens kinsoku wordWrap overflowPunct topLinePunct autoSpaceDE autoSpaceDN bidi adjustRightInd snapToGrid spacing ind contextualSpacing mirrorIndents suppressOverlap jc textDirection textAlignment textboxTightWrap outlineLvl divId cnfStyle"
_TCPR = "cnfStyle tcW gridSpan hMerge vMerge tcBorders shd noWrap tcMar textDirection tcFitText vAlign hideMark headers"
_TRPR = "cnfStyle divId gridBefore gridAfter wBefore wAfter trHeight hidden cantSplit tblHeader tblCellSpacing jc"
_TBLPR = "tblStyle tblpPr tblOverlap bidiVisual tblStyleRowBandSize tblStyleColBandSize tblW jc tblCellSpacing tblInd tblBorders shd tblLayout tblCellMar tblLook tblCaption tblDescription"
_TBLPREX = "tblW jc tblCellSpacing tblInd tblBorders shd tblLayout tblCellMar tblLook"
_DOMAINS = {
    kind: frozenset(w(n) for n in names.split())
    for kind, names in zip(
        PROPERTY_KINDS,
        (_RPR, _PPR, _TCPR, _TRPR, _TBLPR, "gridCol", _TBLPREX),
        strict=True,
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
    elif target.get("kind") == "paragraph_structure" and set(target) == {
        "kind", "paragraph_mark_ref", "text_revision_refs"
    }:
        validate_ref(target["paragraph_mark_ref"])
        refs = target["text_revision_refs"]
        if not isinstance(refs, list) or not 1 <= len(refs) <= 64:
            _fail("invalid_revision_target", "select every text revision with the paragraph mark")
        for ref in refs:
            validate_ref(ref)
    elif target.get("kind") == "row_structure" and set(target) == {
        "kind", "row_mark_ref", "content_revision_refs"
    }:
        validate_ref(target["row_mark_ref"])
        refs = target["content_revision_refs"]
        if type(refs) is not list or len(refs) > 128:
            _fail("invalid_revision_target", "select every ordered content revision with the row mark")
        for ref in refs:
            validate_ref(ref)
    else:
        _fail("invalid_revision_target", "select a complete explicit revision unit")


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
        node = resolve(root, sha, target["revision_ref"])
        if is_row_unit([node]):
            _fail("row_unit_unsupported", "select an explicit complete row_structure target")
        return [node]
    if target["kind"] == "row_structure":
        from .table_rows import row_structure_unit
        nodes = [resolve(root, sha, target["row_mark_ref"]),
                 *[resolve(root, sha, ref) for ref in target["content_revision_refs"]]]
        row_structure_unit(root, nodes)
        return nodes
    if target["kind"] == "paragraph_structure":
        from ._drafting_paragraphs import paragraph_structure_unit
        nodes = [resolve(root, sha, target["paragraph_mark_ref"]),
                 *[resolve(root, sha, ref) for ref in target["text_revision_refs"]]]
        paragraph_structure_unit(root, nodes)
        return nodes
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



def is_row_unit(nodes):
    parent = nodes[0].getparent()
    return parent is not None and parent.tag == w("trPr") and nodes[0].tag in {w("ins"), w("del")}


def is_paragraph_unit(nodes):
    parent = nodes[0].getparent()
    return parent is not None and parent.tag == w("rPr") and (
        parent.getparent() is not None and parent.getparent().tag == w("pPr")
    ) and nodes[0].tag in {w("ins"), w("del")}


def decision_domains(nodes):
    if is_row_unit(nodes):
        from .table_rows import row_structure_unit
        return [row_structure_unit(nodes[0].getroottree().getroot(), nodes)["row"]]
    if is_paragraph_unit(nodes):
        from ._drafting_paragraphs import paragraph_structure_unit
        unit = paragraph_structure_unit(nodes[0].getroottree().getroot(), nodes)
        return [unit["paragraph"], unit["following"]]
    return [n.getparent() if n.tag in {w(k) for k in PROPERTY_KINDS} else n for n in nodes]


def decision_region(nodes):
    if is_row_unit(nodes):
        return nodes[0].getparent().getparent()
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
    # Fields, permission and move ranges may begin outside the selected
    # paragraph. Hidden textbox/property markup must never close story state.
    canonical = {
        n
        for item in flow.paragraphs
        for n in iter_canonical_paragraph_nodes(item.element)
    }
    # Range markers may be direct body/table/row/cell children as well as
    # inline paragraph children. Include only canonical block ancestry.
    for paragraph in flow.paragraphs:
        for ancestor in paragraph.element.iterancestors():
            canonical.add(ancestor)
            if ancestor is body:
                break
    active = set()
    seen = set()
    fields = []
    for event, item in etree.iterwalk(body, events=("start", "end")):
        if event == "start" and item is owner and (active or fields):
            _fail(
                "revision_dependency_unsupported",
                "a field, permission or revision range encloses the target",
            )
        if event != "start" or item.getparent() not in canonical:
            continue
        name = etree.QName(item).localname if isinstance(item.tag, str) else ""
        if item.tag in {w("fldChar"), w("instrText"), w("fldSimple")}:
            if overlaps(owner, item):
                _fail("revision_dependency_unsupported", "a field touches the target")
            if item.tag == w("fldSimple"):
                continue
            if item.tag == w("instrText"):
                if not fields or fields[-1]:
                    _fail(
                        "revision_dependency_unsupported", "unscoped field instructions"
                    )
                continue
            kind = item.get(w("fldCharType"))
            if kind == "begin":
                fields.append(False)
            elif kind == "separate" and fields and not fields[-1]:
                fields[-1] = True
            elif kind == "end" and fields:
                fields.pop()
            else:
                _fail(
                    "revision_dependency_unsupported",
                    "malformed main-story field boundaries",
                )
            continue
        if name in {"permStart", "permEnd"} or (
            item.tag in _REVISION_TAGS and "Range" in name
        ):
            identity = item.get(w("id"), "")
            if not identity.isascii() or not identity.isdecimal() or len(identity) > 10:
                _fail(
                    "revision_dependency_unsupported",
                    "invalid main-story range identity",
                )
            base = name.removesuffix("Start").removesuffix("End")
            key = (base, str(int(identity)))
            if name.endswith("Start") and key not in seen:
                active.add(key)
                seen.add(key)
            elif name.endswith("End") and key in active:
                active.remove(key)
            else:
                _fail(
                    "revision_dependency_unsupported",
                    "malformed main-story range boundaries",
                )
            if overlaps(owner, item):
                _fail(
                    "revision_dependency_unsupported",
                    "a permission or revision range touches the target",
                )
    if fields or active:
        _fail("revision_dependency_unsupported", "unclosed main-story field or range")


def _assert_no_pending_row(node):
    for ancestor in (node, *node.iterancestors()):
        if ancestor.tag == w("tr") and any(ancestor.find(f"{w('trPr')}/{w(tag)}") is not None for tag in ("ins", "del")):
            _fail("row_unit_unsupported", "pending row structure requires a complete explicit structural target")


def _validate_text(root, nodes):
    parent = nodes[0].getparent()
    _assert_no_pending_row(parent)
    if parent.tag != w("p") or any(n.getparent() is not parent for n in nodes):
        _fail(
            "revision_dependency_unsupported",
            "only isolated direct paragraph text revisions are supported",
        )
    for candidate in (parent, parent.getprevious()):
        if candidate is None or candidate.tag != w("p"):
            continue
        for tag in ("ins", "del"):
            if candidate.find(f"{w('pPr')}/{w('rPr')}/{w(tag)}") is not None:
                # An unsupported separator unit is still a dependency. Candidate
                # discovery can fail on ranges or foreign history; that must not
                # turn its text into an independently decidable revision.
                _fail("paragraph_unit_unsupported", "paragraph-mark dependencies require a complete supported structural target")
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
    _assert_no_pending_row(node)
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
        if kind in {
            "rPrChange",
            "pPrChange",
            "tcPrChange",
            "tblPrChange",
            "tblPrExChange",
        }:
            names = {
                "rPrChange": _RPR,
                "pPrChange": _PPR,
                "tcPrChange": _TCPR,
                "tblPrChange": _TBLPR,
                "tblPrExChange": _TBLPREX,
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
    from .table_map import _integer, _merge, _merge_issues

    def geometry(props, kind):
        if kind == w("trPrChange"):
            return (_integer(props, "gridBefore", 0), _integer(props, "gridAfter", 0))
        return (
            _integer(props, "gridSpan", 1),
            _merge(props, "hMerge"),
            _merge(props, "vMerge"),
        )

    for node in table.iter():
        if node.tag in {w("cellIns"), w("cellDel"), w("cellMerge")} or (
            node.tag in {w("ins"), w("del")} and node.getparent().tag == w("trPr")
        ):
            _fail(
                "revision_dependency_unsupported",
                "grid depends on a pending row or cell existence change",
            )
        # Word may materialize an unchanged default in previous properties:
        # absent gridAfter and gridAfter=0 are the same geometry. Validate
        # both forms, then compare effective values; preserve all XML exactly.
        if node.tag in {w("tcPrChange"), w("trPrChange")} and (
            len(node) != 1
            or node[0].tag != node.getparent().tag
            or geometry(node.getparent(), node.tag) != geometry(node[0], node.tag)
        ):
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


def decision_table(nodes):
    """Return the unchanged table owner for a selected table-property domain."""
    if etree.QName(nodes[0]).localname not in {
        "tcPrChange",
        "trPrChange",
        "tblPrChange",
        "tblPrExChange",
        "tblGridChange",
    }:
        return None
    return next(nodes[0].iterancestors(w("tbl")), None)


@dataclass(frozen=True)
class _TableSelection:
    node: etree._Element
    decision: str
    revision_ref: dict
    row: etree._Element | None


@dataclass(frozen=True)
class TableDecisionGroup:
    root: etree._Element
    table: etree._Element
    selections: tuple[_TableSelection, ...]

    def facts(self):
        return [
            {"revision_ref": copy.deepcopy(item.revision_ref), "decision": item.decision}
            for item in self.selections
        ]



@dataclass(frozen=True)
class RowDecisionGroup:
    root: etree._Element
    table: etree._Element
    selections: tuple[tuple[tuple[etree._Element, ...], str], ...]
    member_facts: tuple[dict, ...]

    def facts(self):
        return copy.deepcopy(list(self.member_facts))


def table_decision_groups(root, nodes_by_index, operations):
    """Bind explicit table selections before any source nodes are replaced.

    A group is a validation context, not permission to mutate its whole table.
    Each decision still publishes only its independently verified local patch.
    """
    by_table = {}
    row_tables = {}
    for index, nodes in nodes_by_index.items():
        if is_row_unit(nodes):
            row_tables.setdefault(nodes[0].getparent().getparent().getparent(), []).append(index)
            continue
        table = decision_table(nodes)
        if table is not None:
            by_table.setdefault(table, []).append(index)
    groups = {}
    if set(row_tables) & set(by_table):
        _fail("revision_operation_overlap", "row structures and table-property decisions require separate saves")
    for table, indexes in row_tables.items():
        group = RowDecisionGroup(root, table,
            tuple((tuple(nodes_by_index[i]), operations[i]["decision"]) for i in indexes),
            tuple({"revision_ref": copy.deepcopy(operations[i]["target"]["row_mark_ref"]),
                   "decision": operations[i]["decision"]} for i in indexes))
        groups.update((index, group) for index in indexes)
    for table, indexes in by_table.items():
        if len(indexes) < 2:
            continue
        selections = []
        for index in sorted(indexes, key=lambda i: _path(root, nodes_by_index[i][0])):
            node = nodes_by_index[index][0]
            try:
                _validate_property(root, node)
                if (next(table.iterancestors(w("tbl")), None) is not None
                        or any(item is not table for item in table.iter(w("tbl")))):
                    _fail("revision_dependency_unsupported", "nested table groups are unsupported")
            except DocxError as exc:
                if hasattr(exc, "metadata"):
                    exc.metadata["operation_index"] = index
                raise
            selections.append(_TableSelection(
                node, operations[index]["decision"],
                copy.deepcopy(operations[index]["target"]["revision_ref"]),
                next(node.iterancestors(w("tr")), None),
            ))
        group = TableDecisionGroup(root, table, tuple(selections))
        groups.update((index, group) for index in indexes)
    return groups


def _row_property_dependency(node, action, kind, group=None):
    if action != "reject" or kind != "tcPrChange":
        return
    from .table_map import _integer

    # Native Word has removed the entire independent row-property set when
    # rejecting a span history, including height and cantSplit. Until those
    # properties are explicitly owned by a selected row-history domain,
    # exclude this combination instead of deleting content outside the patch.
    if _integer(node.getparent(), "gridSpan", 1) == _integer(node[0], "gridSpan", 1):
        return
    row = next(node.iterancestors(w("tr")), None)
    props = row.find(w("trPr")) if row is not None else None
    if props is None or not (len(props) or props.attrib or (props.text or "").strip()):
        return
    if group is not None and any(
        item.row is row and item.node.tag == w("trPrChange")
        for item in group.selections
    ):
        return  # The row properties are an actual explicitly selected domain.
    height = props.find(w("trHeight")) is not None
    _fail(
        "table_height_dependency_unsupported" if height else "table_row_property_dependency_unsupported",
        "preservation of independent row properties is unproved for this span rejection",
    )


def _group_outcome(root, nodes, decision, group):
    """Compose the explicitly selected final state on a detached document."""
    if group.root is not root or not any(item.node is nodes[0] for item in group.selections):
        _fail("round_trip_failed", "table group does not own this selected revision")
    clone = copy.deepcopy(root)
    mapped = dict(zip(root.iter(), clone.iter(), strict=True))
    for item in group.selections:
        if item.node not in mapped:
            continue  # An earlier verified local patch already decided it.
        action = decision if item.node is nodes[0] else item.decision
        kind = etree.QName(item.node).localname
        _row_property_dependency(item.node, action, kind, group)
        props = item.node.getparent()
        parent = mapped[props.getparent()]
        index = props.getparent().index(props)
        _replace_span(parent, index, [mapped[props]], _outcome([item.node], action, kind))
    # Compare the source-bound alignment histories against the complete chosen
    # row state. Include already-applied members so counterfactual previews do
    # not forget a preceding table decision when this row is planned later.
    for item in group.selections:
        kind = etree.QName(item.node).localname
        if kind in {"tblPrChange", "tblPrExChange"}:
            owner = group.table if kind == "tblPrChange" else item.row
            action = decision if item.node is nodes[0] else item.decision
            _alignment_dependency(item.node, action, kind, mapped[owner])
    return clone, mapped


def validate_table_group(group):
    if any(item.node.tag in {w("tcPrChange"), w("trPrChange"), w("tblGridChange")}
           for item in group.selections):
        _grid(group.table)
    validate_fixed_table_widths(group.table)


def validate_fixed_table_widths(table):
    """Prove explicit fixed-layout widths in the final selected batch state.

    A grid history may require explicitly selected cell histories. Word can
    otherwise regenerate the grid from inconsistent preferred cell widths.
    Autofit widths deliberately have no such equality requirement.
    """
    from .table_map import _integer

    layout = table.find(f"{w('tblPr')}/{w('tblLayout')}")
    layout_type = layout.get(w("type")) if layout is not None else None
    fixed_rows = []
    for row in table.findall(w("tr")):
        override = row.find(f"{w('tblPrEx')}/{w('tblLayout')}")
        effective = override.get(w("type")) if override is not None else layout_type
        if effective == "fixed":
            fixed_rows.append(row)
    if not fixed_rows:
        return
    _grid(table)

    def width(node, *, typed=True):
        value = node.get(w("w")) if node is not None else None
        if (
            value is None
            or len(value) > 10
            or not value.isascii()
            or not value.isdecimal()
            or int(value) <= 0
            or (typed and node.get(w("type")) != "dxa")
        ):
            _fail(
                "fixed_table_width_dependency_unsupported",
                "fixed-layout widths require explicit positive twip values",
            )
        return int(value)

    columns = [
        width(n, typed=False) for n in table.find(w("tblGrid")).findall(w("gridCol"))
    ]
    for row in fixed_rows:
        props = row.find(w("trPr"))
        before = _integer(props, "gridBefore", 0)
        after = _integer(props, "gridAfter", 0)
        cursor = before
        for cell in row.findall(w("tc")):
            cp = cell.find(w("tcPr"))
            span = _integer(cp, "gridSpan", 1)
            widths = cp.findall(w("tcW")) if cp is not None else []
            if len(widths) != 1 or width(widths[0]) != sum(
                columns[cursor : cursor + span]
            ):
                _fail(
                    "fixed_table_width_dependency_unsupported",
                    "fixed-layout grid and explicit cell widths disagree",
                )
            cursor += span
        for omitted, name, expected in (
            (before, "wBefore", sum(columns[:before])),
            (after, "wAfter", sum(columns[len(columns) - after :]) if after else 0),
        ):
            if omitted and width(props.find(w(name))) != expected:
                _fail(
                    "fixed_table_width_dependency_unsupported",
                    "fixed-layout omitted-column width disagrees with the grid",
                )


def _alignment_dependency(node, action, kind, outcome_owner=None):
    if action != "reject" or kind not in {"tblPrChange", "tblPrExChange"}:
        return
    current = node.getparent().find(w("jc"))
    previous = node[0].find(w("jc"))
    current_value = dict(current.attrib) if current is not None else None
    previous_value = dict(previous.attrib) if previous is not None else None
    if current_value == previous_value:
        return
    owner = outcome_owner if outcome_owner is not None else node.getparent().getparent()
    rows = (
        [owner]
        if kind == "tblPrExChange"
        else [
            row
            for row in owner.iter(w("tr"))
            if next(row.iterancestors(w("tbl")), None) is owner
        ]
    )
    if any(row.find(f"{w('trPr')}/{w('jc')}") is not None for row in rows):
        # Native Word can also remove an untracked row alignment when rejecting
        # its table-format unit. Such collateral is outside the selected XML
        # histories; never invent a deletion merely to reproduce Word's result.
        _fail(
            "table_alignment_dependency_unsupported",
            "restoring table alignment conflicts with an independent row alignment",
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
        # Detached deepcopy retains required namespace declarations; attached
        # Word nodes inherit many more. Put the expected nodes in the SAME
        # namespace context, then keep exact serialization comparison (including
        # comments, tails and any unexpected local namespace declaration).
        context = etree.Element("comparison", nsmap=parent.nsmap)
        context.extend(copy.deepcopy(self.after))
        if [etree.tostring(n) for n in present] != [etree.tostring(n) for n in context]:
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



def _paragraph_outcomes(root, nodes, parts):
    from ._drafting_paragraphs import paragraph_structure_unit, paragraph_structure_outcome
    from .paragraph_numbering import numbering_effects_for_deletion
    unit = paragraph_structure_unit(root, nodes)
    old = [unit["paragraph"], unit["following"]]
    parent, index = old[0].getparent(), old[0].getparent().index(old[0])
    outcomes = {action: paragraph_structure_outcome(unit, action) for action in ("accept", "reject")}
    projections = {}
    for action, chosen in outcomes.items():
        clone = copy.deepcopy(root)
        owner = clone
        for i in _path(root, parent):
            owner = owner[i]
        _replace_span(owner, index, list(owner)[index:index + 2], copy.deepcopy(chosen))
        for para in list(owner)[index:index + len(chosen)]:
            _main_context(clone, para)
            _require_style_dependencies(parts, para)
        projections[action] = clone
    longer = "accept" if unit["kind"] == "paragraph_insertion" else "reject"
    shorter = "reject" if longer == "accept" else "accept"
    paragraph_index = list(root.find(w("body")).iter(w("p"))).index(unit["content"])
    numbering = numbering_effects_for_deletion(parts, projections[longer], projections[shorter], paragraph_index)
    facts = {
        "operation": "decide_revision", "revision_kind": unit["kind"],
        "members": [{"kind": etree.QName(n).localname, "id": n.get(w("id")),
                     "author": n.get(w("author")), "date": n.get(w("date"))} for n in nodes],
        "current_text": "\n".join(_reading_text(_paragraph_segments(p)) for p in old),
        "accepted_text": "\n".join(_reading_text(_paragraph_segments(p)) for p in outcomes["accept"]),
        "rejected_text": "\n".join(_reading_text(_paragraph_segments(p)) for p in outcomes["reject"]),
        "current_properties": None, "previous_properties": None,
        "accept_refusal_code": None, "reject_refusal_code": None,
        "fixed_width_validation_scope": "not_applicable", "table_validation_scope": "not_applicable",
        "table_group_members": [],
        "paragraph_numbering": {"accepted": numbering["before" if longer == "accept" else "after"],
                                "rejected": numbering["before" if longer == "reject" else "after"]},
    }
    if any(len(facts[key]) > MAX_PREVIEW_CHARS for key in ("current_text", "accepted_text", "rejected_text")):
        _fail("resource_limit_exceeded", "structural paragraph preview exceeds its text limit")
    return old, outcomes, facts



def _row_outcomes(root, nodes, parts, group=None):
    from .table_rows import row_structure_unit, row_structure_outcome
    from .table_row_numbering import numbering_effects_for_rows
    unit = row_structure_unit(root, nodes)
    row = unit["row"]
    outcomes = {action: row_structure_outcome(unit, action) for action in ("accept", "reject")}
    projections = {}
    refusals = {}
    for action in ("accept", "reject"):
        clone = copy.deepcopy(root)
        mapped = dict(zip(root.iter(), clone.iter(), strict=True))
        selections = group.selections if isinstance(group, RowDecisionGroup) else ((tuple(nodes), action),)
        for chosen, chosen_action in selections:
            if chosen[0] not in mapped:
                continue  # Earlier exact patch already decided this selected unit.
            selected = row_structure_unit(root, chosen)
            target = mapped[selected["row"]]
            parent, index = target.getparent(), target.getparent().index(target)
            if chosen[0] is nodes[0]:
                chosen_action = action
            clean = row_structure_outcome(selected, chosen_action)
            parent.remove(target)
            for offset, output in enumerate(clean):
                parent.insert(index + offset, output)
        table_path = _path(root, row.getparent())
        table = clone
        for index in table_path:
            table = table[index]
        try:
            from .table_rows import _table_shape
            _table_shape(table)
            _main_context(clone, table)
            for output in outcomes[action]:
                for paragraph in output.iter(w("p")):
                    _require_style_dependencies(parts, paragraph)
        except DocxError as exc:
            refusals[action] = getattr(exc, "code", "revision_dependency_unsupported")
        else:
            refusals[action] = None
        projections[action] = clone
    longer = "accept" if unit["kind"] == "row_insertion" else "reject"
    shorter = "reject" if longer == "accept" else "accept"
    # The selected row survives in exactly one clean outcome. Other selected
    # rows use their explicit requested outcomes for both counterfactuals.
    long_root = projections[longer]
    long_table = long_root
    for index in _path(root, row.getparent()):
        long_table = long_table[index]
    # Find the selected clean row by its source-relative row order, accounting
    # for explicitly removed preceding group members.
    preceding_removed = 0
    if isinstance(group, RowDecisionGroup):
        for chosen, decision in group.selections:
            if chosen[0] is nodes[0] or chosen[0] not in set(root.iter()):
                continue
            other = row_structure_unit(root, chosen)
            if other["row"].getparent().index(other["row"]) < row.getparent().index(row) and not row_structure_outcome(other, decision):
                preceding_removed += 1
    clean_row_index = row.getparent().index(row) - preceding_removed
    clean_row = long_table[clean_row_index]
    all_p = list(long_root.find(w("body")).iter(w("p")))
    removed = [all_p.index(p) for p in clean_row.iter(w("p"))]
    numbering = {"before": [], "after": []}
    try:
        numbering = numbering_effects_for_rows(parts, long_root, projections[shorter], removed)
    except DocxError as exc:
        for action in ("accept", "reject"):
            refusals[action] = refusals[action] or getattr(exc, "code", "revision_dependency_unsupported")
    texts = {action: "\n".join(_reading_text(_paragraph_segments(p)) for output in outcomes[action] for p in output.iter(w("p")))
             for action in ("accept", "reject")}
    current = "\n".join(_reading_text(_paragraph_segments(p)) for p in row.iter(w("p")))
    if max(map(len, [current, *texts.values()])) > 40_001:
        _fail("resource_limit_exceeded", "row decision preview exceeds its text limit")
    facts = {"operation": "decide_revision", "revision_kind": unit["kind"],
             "members": [{"kind": etree.QName(n).localname, "id": n.get(w("id")), "author": n.get(w("author")), "date": n.get(w("date"))} for n in nodes],
             "current_text": current, "accepted_text": texts["accept"], "rejected_text": texts["reject"],
             "current_properties": None, "previous_properties": None,
             "accept_refusal_code": refusals["accept"], "reject_refusal_code": refusals["reject"],
             "fixed_width_validation_scope": "not_applicable", "table_validation_scope": "explicit_batch_final_state" if group else "single_outcome",
             "table_group_members": group.facts() if isinstance(group, RowDecisionGroup) else [],
             "row_numbering": {"accepted": numbering["before" if longer == "accept" else "after"],
                               "rejected": numbering["before" if longer == "reject" else "after"]}}
    return [row], outcomes, facts

def _plan_outcomes(root, nodes, parts, defer_fixed_widths=False, table_group=None):
    """Describe and validate each outcome without choosing a write operation."""
    if is_row_unit(nodes):
        return _row_outcomes(root, nodes, parts, table_group)
    if is_paragraph_unit(nodes):
        return _paragraph_outcomes(root, nodes, parts)
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
            if table_group is None:
                _alignment_dependency(nodes[0], action, kind)
            _row_property_dependency(nodes[0], action, kind, table_group)
            # Inspect the actual surviving outcome in its complete document
            # context. Text can retain (or remove) unresolved run styles too;
            # checking the source or only property decisions is insufficient.
            if table_group is None:
                clone = copy.deepcopy(root)
                owner = clone
                for i in _path(root, parent):
                    owner = owner[i]
                _replace_span(
                    owner,
                    index,
                    list(owner)[index : index + len(old_nodes)],
                    copy.deepcopy(result),
                )
            else:
                clone, mapped = _group_outcome(root, nodes, action, table_group)
                owner = mapped[parent]
            if kind in PROPERTY_KINDS:
                table = next(
                    (n for n in (owner, *owner.iterancestors()) if n.tag == w("tbl")),
                    None,
                )
                geometric_kinds = {w("tcPrChange"), w("trPrChange"), w("tblGridChange")}
                if table is not None and (
                    w(kind) in geometric_kinds or table_group is not None and any(
                        item.node.tag in geometric_kinds for item in table_group.selections
                    )
                ):
                    _grid(table)
                if (
                    table is not None
                    and decision_table(nodes) is not None
                    and (table_group is not None or not defer_fixed_widths)
                ):
                    validate_fixed_table_widths(table)
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
    facts = {
        "table_validation_scope": (
            "explicit_batch_final_state" if table_group is not None else "single_outcome"
        ) if decision_table(nodes) is not None else "not_applicable",
        "table_group_members": table_group.facts() if table_group is not None else [],
        "fixed_width_validation_scope": (
            "explicit_batch_final_state" if defer_fixed_widths else "single_outcome"
        )
        if decision_table(nodes) is not None
        else "not_applicable",
        "accept_refusal_code": refusals["accept"],
        "reject_refusal_code": refusals["reject"],
        "operation": "decide_revision",
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
    return old_nodes, outcomes, facts


def plan_decision(root, nodes, decision, parts, defer_fixed_widths=False, table_group=None):
    """Describe both outcomes and select only a validated write operation."""
    if decision not in ("accept", "reject"):
        _fail("invalid_edit", "decision must be accept or reject")
    if isinstance(table_group, RowDecisionGroup):
        if not any(chosen[0] is nodes[0] and action == decision for chosen, action in table_group.selections):
            _fail("round_trip_failed", "selected row decision differs from its explicit group")
    elif table_group is not None and not any(
        item.node is nodes[0] and item.decision == decision
        for item in table_group.selections
    ):
        _fail("round_trip_failed", "selected decision differs from its bound table group")
    old_nodes, outcomes, facts = _plan_outcomes(root, nodes, parts, defer_fixed_widths, table_group)
    refusal = facts[f"{decision}_refusal_code"]
    if refusal is not None:
        _fail(refusal, "the selected outcome has an unsupported dependency")
    facts["decision"] = decision
    parent = old_nodes[0].getparent()
    index = parent.index(old_nodes[0])
    chosen = outcomes[decision]
    return (
        DecisionPatch(parent, index, copy.deepcopy(old_nodes), copy.deepcopy(chosen)),
        chosen,
        facts,
    )


def apply_decision(root, nodes, decision, parts, defer_fixed_widths=False, table_group=None):
    patch, chosen, facts = plan_decision(
        root, nodes, decision, parts, defer_fixed_widths, table_group
    )
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
        structural_target = None
        try:
            planning_bytes += 2 * document_bytes
            if planning_bytes > MAX_FINGERPRINT_BYTES:
                _fail(
                    "resource_limit_exceeded",
                    "reduce revision page size to bound outcome validation",
                )
            selected = [node]
            structural_target = None
            if is_row_unit(selected):
                from .table_rows import row_structure_candidates
                selected = row_structure_candidates(node) or selected
                if row_structure_candidates(node):
                    structural_target = {"kind": "row_structure", "row_mark_ref": ref,
                        "content_revision_refs": [reference(root, n, snapshot.sha256) for n in selected[1:]]}
            elif is_paragraph_unit(selected):
                from ._drafting_paragraphs import paragraph_structure_candidates
                selected = paragraph_structure_candidates(node) or selected
                if len(selected) > 1:
                    structural_target = {"kind": "paragraph_structure", "paragraph_mark_ref": ref,
                                         "text_revision_refs": [reference(root, n, snapshot.sha256) for n in selected[1:]]}
            _, _, preview = _plan_outcomes(root, selected, package.parts)
            # A readable revision can allow only one of the two decisions.
            # Preserve both refusal codes even when neither outcome is safe.
            preview["decision"] = (
                "reject"
                if preview["accept_refusal_code"] is not None
                and preview["reject_refusal_code"] is None
                else "accept"
            )
            if all(
                preview[f"{action}_refusal_code"] is not None
                for action in ("accept", "reject")
            ):
                refusal = preview["accept_refusal_code"]
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
                **({"structural_target": structural_target} if structural_target is not None else {}),
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
