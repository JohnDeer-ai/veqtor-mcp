# SPDX-License-Identifier: Apache-2.0
"""Closed clean-paragraph address and preservation checks for NR-01."""

from __future__ import annotations

import posixpath

from lxml import etree

from ._ooxml import (
    MOVE_REVISION_TAGS,
    TEXT_REVISION_TAGS,
    UNSUPPORTED_REVISION_TAGS,
    canonical_body_flow_v1,
    parse_xml,
    w,
)
from .contracts import INSPECT_CONTAINER_POLICY_V1, INSPECT_READING_MODE_V1
from .inspect import InspectError, _resolve_paragraph

PARAGRAPH_TARGET_KEYS = frozenset({"kind", "paragraph_ref"})
PARAGRAPH_REF_KEYS = frozenset({
    "schema_version", "ref_type", "file_sha256", "part_name", "paragraph_index",
    "paragraph_text_sha256", "reading_mode", "container_policy",
})
_REVISION_TAGS = TEXT_REVISION_TAGS | MOVE_REVISION_TAGS | UNSUPPORTED_REVISION_TAGS | frozenset(
    w(name) for name in (
        "cellMerge", "tblGridChange", "tblPrExChange", "conflictIns", "conflictDel",
        "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd",
        "customXmlInsRangeStart", "customXmlInsRangeEnd", "customXmlDelRangeStart",
        "customXmlDelRangeEnd", "customXmlMoveFromRangeStart", "customXmlMoveFromRangeEnd",
        "customXmlMoveToRangeStart", "customXmlMoveToRangeEnd",
        "customXmlConflictInsertionRangeStart", "customXmlConflictInsertionRangeEnd",
        "customXmlConflictDeletionRangeStart", "customXmlConflictDeletionRangeEnd",
    )
)
_REVISION_NAMES = frozenset(etree.QName(tag).localname for tag in _REVISION_TAGS)
_RANGE_NAMES = frozenset(name for name in _REVISION_NAMES if "Range" in name)
# A deliberately bounded property vocabulary. Unknown extension properties cannot
# prove absence of pending revisions and therefore do not enter this write path.
_PROPERTY_TAGS = frozenset(w(name) for name in (
    "pPr", "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr",
    "widowControl", "numPr", "ilvl", "numId", "suppressLineNumbers", "pBdr",
    "top", "left", "bottom", "right", "between", "bar", "shd", "tabs", "tab",
    "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct", "topLinePunct",
    "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid", "spacing",
    "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc",
    "textDirection", "textAlignment", "textboxTightWrap", "outlineLvl", "divId",
    "cnfStyle", "rPr", "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps",
    "smallCaps", "strike", "dstrike", "outline", "shadow", "emboss", "imprint",
    "noProof", "vanish", "webHidden", "color", "w", "kern", "position", "sz",
    "szCs", "highlight", "u", "effect", "bdr", "fitText", "vertAlign", "rtl",
    "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath",
    "tblPr", "tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize",
    "tblStyleColBandSize", "tblW", "tblCellSpacing", "tblInd", "tblBorders",
    "insideH", "insideV", "start", "end", "tblLayout", "tblCellMar", "tblLook",
    "tblCaption", "tblDescription", "tblPrEx", "tblGrid", "gridCol", "trPr",
    "cantSplit", "trHeight", "tblHeader", "hidden", "gridBefore", "gridAfter",
    "wBefore", "wAfter", "tcPr", "tcW", "gridSpan", "hMerge", "vMerge",
    "tcBorders", "tl2br", "tr2bl", "noWrap", "tcMar", "tcFitText", "vAlign",
    "hideMark", "headers", "header",
))


def validate_paragraph_target(target: object) -> dict:
    if (not isinstance(target, dict) or set(target) != PARAGRAPH_TARGET_KEYS
            or target.get("kind") != "paragraph"):
        raise InspectError("invalid_edit", "target must be the closed paragraph target")
    ref = target["paragraph_ref"]
    if not isinstance(ref, dict) or set(ref) != PARAGRAPH_REF_KEYS:
        raise InspectError("invalid_reference", "paragraph target requires the complete v1 reference")
    if (type(ref["paragraph_index"]) is not int or ref["paragraph_index"] < 0
            or any(not isinstance(ref[key], str) for key in PARAGRAPH_REF_KEYS - {"paragraph_index"})):
        raise InspectError("invalid_reference", "paragraph reference field types are invalid")
    for key in ("file_sha256", "paragraph_text_sha256"):
        if len(ref[key]) != 64 or any(c not in "0123456789abcdef" for c in ref[key]):
            raise InspectError("invalid_reference", "paragraph reference requires lowercase SHA-256")
    if any(ref[key] != value for key, value in {
        "schema_version": "paragraph_ref.v1", "ref_type": "paragraph",
        "part_name": "word/document.xml", "reading_mode": INSPECT_READING_MODE_V1,
        "container_policy": INSPECT_CONTAINER_POLICY_V1,
    }.items()):
        raise InspectError("reference_mismatch", "paragraph reference type, part or policy is unsupported")
    return ref


def _require_clean(root: etree._Element) -> None:
    if any(isinstance(node.tag, str) and (etree.QName(node).localname in _REVISION_NAMES
           or etree.QName(node).localname.endswith("Change")) for node in root.iter()):
        raise InspectError("paragraph_pending_revisions", "pending revisions apply to the paragraph")


def _require_properties(root: etree._Element) -> None:
    _require_clean(root)
    if any(node.tag not in _PROPERTY_TAGS or (node.text or "").strip()
           for node in root.iter()):
        raise InspectError("paragraph_structure_unsupported", "unsupported paragraph or container properties")


def _require_style_dependencies(parts: dict, paragraph: etree._Element) -> None:
    """Prove referenced formatting definitions; extraction's fallbacks are read-only.

    Style definitions have their own vocabulary (name, basedOn, linked styles,
    numbering, etc.). Do not apply the direct-run property whitelist to them.
    """
    supported = {"styles": "word/styles.xml", "numbering": "word/numbering.xml",
                 "stylesWithEffects": "word/stylesWithEffects.xml"}
    roots = {kind: parse_xml(parts[part]) for kind, part in supported.items() if part in parts}
    # Known pending revisions remain the primary refusal even when another
    # dependency is missing. Missing definitions never erase this evidence.
    for root in roots.values():
        _require_clean(root)

    def unresolved():
        raise InspectError("paragraph_structure_unsupported", "unresolved style or numbering dependency")

    bound = set()
    rels = parts.get("word/_rels/document.xml.rels")
    if rels is not None:
        for rel in parse_xml(rels):
            kind = rel.get("Type", "").rsplit("/", 1)[-1]
            if kind not in supported:
                continue
            target = posixpath.normpath(posixpath.join("word", rel.get("Target", ""))).lstrip("/")
            if (kind in bound or rel.get("TargetMode", "Internal") != "Internal"
                    or target != supported[kind] or target not in parts):
                unresolved()
            bound.add(kind)
    if set(roots) != bound:
        unresolved()

    def definitions(root, tag, key):
        result = {}
        if root is not None:
            for node in root.findall(w(tag)):
                identity = node.get(w(key))
                if not identity or identity in result:
                    unresolved()
                result[identity] = node
        return result

    style_sets = [definitions(roots.get("styles"), "style", "styleId")]
    if "stylesWithEffects" in roots:
        style_sets.append(definitions(roots["stylesWithEffects"], "style", "styleId"))
    nums = definitions(roots.get("numbering"), "num", "numId")
    abstracts = definitions(roots.get("numbering"), "abstractNum", "abstractNumId")
    pending = [paragraph]
    for ancestor in paragraph.iterancestors():
        if ancestor.tag == w("tbl"):
            props = ancestor.find(w("tblPr"))
            if props is not None:
                pending.append(props)
    for kind in ("styles", "stylesWithEffects"):
        root = roots.get(kind)
        if root is not None:
            pending.extend(root.findall(w("docDefaults")))
            pending.extend(node for node in root.findall(w("style"))
                           if node.get(w("default")) in {"1", "true", "on"})

    # Follow basedOn chains separately from cross-links: legitimate paragraph /
    # character or numbering links may be reciprocal, inheritance may not cycle.
    checked_styles, checked_nums = set(), set()

    def require_style(identity):
        if not isinstance(identity, str) or not identity:
            unresolved()
        for set_index, styles in enumerate(style_sets):
            path = set()
            current = identity
            while current is not None:
                if current in path or current not in styles:
                    unresolved()
                if (set_index, current) in checked_styles:
                    break
                path.add(current)
                node = styles[current]
                pending.append(node)
                based = node.findall(w("basedOn"))
                if len(based) > 1:
                    unresolved()
                current = based[0].get(w("val")) if based else None
                if based and not current:
                    unresolved()
            checked_styles.update((set_index, item) for item in path)

    while pending:
        root = pending.pop()
        for node in root.iter():
            if node.tag in {w(name) for name in (
                    "pStyle", "rStyle", "tblStyle", "basedOn", "link", "numStyleLink", "styleLink")}:
                require_style(node.get(w("val")))
            elif node.tag == w("numId"):
                identity = node.get(w("val"))
                if identity == "0" or identity in checked_nums:
                    continue
                if identity not in nums:
                    unresolved()
                num = nums[identity]
                refs = num.findall(w("abstractNumId"))
                if len(refs) != 1 or refs[0].get(w("val")) not in abstracts:
                    unresolved()
                checked_nums.add(identity)
                pending.extend((num, abstracts[refs[0].get(w("val"))]))


def resolve_paragraph_target(snapshot, document: etree._Element, target: dict, parts: dict):
    ref = validate_paragraph_target(target)
    item = _resolve_paragraph(snapshot, ref)
    body = document.find(w("body"))
    flow = canonical_body_flow_v1(body)
    paragraph = flow.paragraphs[item.paragraph_index].element
    _require_clean(paragraph)
    if any(child.tag not in {w("p"), w("tbl"), w("sectPr")} for child in body):
        raise InspectError("paragraph_structure_unsupported", "unsupported body container or range boundary")
    # Range endpoints can be outside the paragraph. No range-history inference.
    if any(isinstance(node.tag, str) and etree.QName(node).localname in _RANGE_NAMES
           for node in document.iter()):
        raise InspectError("paragraph_pending_revisions", "document contains unresolvable revision ranges")
    for section in document.iter(w("sectPr")):
        _require_clean(section)
    _require_style_dependencies(parts, paragraph)
    # A preceding paragraph mark revision can join this paragraph to its sibling.
    for neighbour in (paragraph.getprevious(), paragraph.getnext()):
        if neighbour is not None and neighbour.tag == w("p"):
            props = neighbour.find(w("pPr"))
            if props is not None:
                _require_clean(props)
    for ancestor in paragraph.iterancestors():
        if ancestor is body:
            break
        if ancestor.tag not in {w("tc"), w("tr"), w("tbl")}:
            raise InspectError("paragraph_structure_unsupported", "unsupported enclosing paragraph container")
        for child in ancestor:
            if child.tag in {w("tcPr"), w("trPr"), w("tblPr"), w("tblPrEx"), w("tblGrid")}:
                _require_properties(child)
            elif child.tag not in {w("p"), w("tc"), w("tr"), w("tbl")}:
                _require_clean(child)
                raise InspectError("paragraph_structure_unsupported", "unsupported table container structure")
    if len(paragraph.findall(w("pPr"))) > 1:
        raise InspectError("paragraph_structure_unsupported", "duplicate paragraph properties")
    for child in paragraph:
        if child.tag == w("pPr"):
            _require_properties(child)
        elif child.tag == w("r"):
            if (len(child.findall(w("rPr"))) > 1 or len(child.findall(w("t"))) != 1
                    or any(node.tag not in {w("rPr"), w("t")} for node in child)):
                raise InspectError("paragraph_structure_unsupported", "paragraph requires direct simple text runs")
            props = child.find(w("rPr"))
            if props is not None:
                _require_properties(props)
            if len(child.find(w("t"))):
                raise InspectError("paragraph_structure_unsupported", "nested text markup is unsupported")
        else:
            raise InspectError("paragraph_structure_unsupported", "unsupported inline paragraph structure")
    validate_paragraph_candidate(paragraph, paragraph)
    return paragraph, item.paragraph_index


def _xml_shape(node):
    if node is None:
        return None
    return (node.tag, tuple(sorted(node.attrib.items())), node.text,
            tuple(_xml_shape(child) for child in node), node.tail)


def paragraph_format_signature(paragraph, *, reject_new=False, accept_new=False):
    """Compare original text and per-character run formatting despite run splits."""
    tokens = []
    for child in paragraph:
        if child.tag == w("pPr"):
            continue
        if (reject_new and child.tag == w("ins")) or (accept_new and child.tag == w("del")):
            continue
        flatten = (reject_new and child.tag == w("del")) or (accept_new and child.tag == w("ins"))
        runs = list(child) if flatten else [child]
        for run in runs:
            props = _xml_shape(run.find(w("rPr")))
            attrs = tuple(sorted(run.attrib.items()))
            for atom in run:
                if atom.tag in {w("t"), w("delText")}:
                    tokens.extend((char, attrs, props) for char in atom.text or "")
                elif atom.tag != w("rPr"):
                    tokens.append(("unsupported", _xml_shape(atom)))
    return (tuple(sorted(paragraph.attrib.items())),
            _xml_shape(paragraph.find(w("pPr"))), tuple(tokens))


def paragraph_xml_signature(paragraph):
    """Complete expanded-name XML structure, including zero-text elements."""
    return _xml_shape(paragraph)


def validate_paragraph_candidate(original, candidate):
    """Reject unaccounted inline structure before comparing text/format projections.

    This also serves the independent file checker: a complete extraction inventory
    cannot detect bookmarks, duplicate properties, empty drawings or empty runs.
    """
    def shape(paragraph, revisions):
        position, empty_runs = 0, []
        for index, child in enumerate(paragraph):
            if child.tag == w("pPr") and index == 0:
                _require_properties(child)
                continue
            wrapper = child.tag in {w("del"), w("ins")}
            if wrapper:
                if (not revisions or not len(child)
                        or set(child.attrib) != {w("id"), w("author")}
                        or not child.get(w("id")) or not child.get(w("author"))):
                    raise InspectError("paragraph_structure_unsupported", "unsupported revision structure")
                runs = list(child)
            else:
                runs = [child]
            if (child.text or "").strip() or (child.tail or "").strip():
                raise InspectError("paragraph_structure_unsupported", "unexpected inline text")
            for run in runs:
                text_tag = w("delText") if child.tag == w("del") else w("t")
                tags = [node.tag for node in run]
                if (run.tag != w("r") or tags not in ([text_tag], [w("rPr"), text_tag])
                        or (run.text or "").strip() or (run.tail or "").strip()):
                    raise InspectError("paragraph_structure_unsupported", "unsupported candidate run structure")
                props = run.find(w("rPr"))
                if props is not None:
                    _require_properties(props)
                atom = run.find(text_tag)
                if (len(atom) or set(atom.attrib) - {"{http://www.w3.org/XML/1998/namespace}space"}
                        or (atom.tail or "").strip()):
                    raise InspectError("paragraph_structure_unsupported", "unsupported candidate text structure")
                if wrapper and not atom.text:
                    raise InspectError("paragraph_structure_unsupported", "empty revision run")
                if child.tag != w("ins"):
                    if not atom.text:
                        empty_runs.append((position, _xml_shape(run)))
                    position += len(atom.text or "")
        return empty_runs

    if (candidate.text or "").strip() or shape(original, False) != shape(candidate, True):
        raise InspectError("paragraph_structure_unsupported", "unaccounted paragraph structure")
