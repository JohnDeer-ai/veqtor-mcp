# SPDX-License-Identifier: Apache-2.0
"""Explicit uniform decisions for a complete, text-resolved cell component.

No automatic mixed choice, text decision, numbering rewrite or range deletion.
The native Word controls establish rightmost-paragraph formatting for each
surviving segment. Publication still uses the exact reversible drafting patch.
"""

import copy

from lxml import etree

from . import revision_decisions as d
from ._ooxml import w
from ._preserved_paragraph_boundaries import MAX_PARAGRAPHS, MAX_MARKS, _marks


class ParagraphChain(list):
    """Explicit selection; legacy paragraph targets retain their old grammar."""


def candidates(mark):
    """Offer a complete target only on its first mark; never infer a decision."""
    if not d.is_paragraph_unit([mark]):
        return None
    first = mark.getparent().getparent().getparent()
    if not _marks(first) or _marks(first)[0] is not mark or _marks(first.getprevious()):
        return None
    paragraphs, marks = [first], []
    while _marks(paragraphs[-1]):
        marks.extend(_marks(paragraphs[-1]))
        following = paragraphs[-1].getnext()
        if following is None or following.tag != w("p"):
            return None
        paragraphs.append(following)
        if len(paragraphs) > MAX_PARAGRAPHS or len(marks) > MAX_MARKS:
            d._fail(
                "resource_limit_exceeded", "paragraph chain exceeds its fixed limit"
            )
    nodes = ParagraphChain(marks)
    component(nodes)
    return nodes


def component(nodes):
    if not nodes or len(nodes) > MAX_MARKS or len(set(nodes)) != len(nodes):
        d._fail(
            "paragraph_chain_unsupported", "select all ordered boundary references once"
        )
    mark = nodes[0]
    if not d.is_paragraph_unit([mark]):
        d._fail("paragraph_chain_unsupported", "select direct paragraph boundary marks")
    first = mark.getparent().getparent().getparent()
    parent = first.getparent()
    if parent is None or parent.tag != w("tc") or _marks(first.getprevious()):
        d._fail(
            "paragraph_chain_unsupported", "select a complete component within one cell"
        )
    previous = first.getprevious()
    if (
        previous is not None
        and previous.tag != w("p")
        and not (previous.tag == w("tcPr") and previous.getprevious() is None)
    ):
        d._fail("paragraph_chain_unsupported", "unproven left component boundary")
    paragraphs = [first]
    while _marks(paragraphs[-1]):
        following = paragraphs[-1].getnext()
        if following is None or following.tag != w("p"):
            d._fail("paragraph_chain_unsupported", "component crosses a cell boundary")
        paragraphs.append(following)
        if len(paragraphs) > MAX_PARAGRAPHS:
            d._fail(
                "resource_limit_exceeded", "paragraph chain exceeds its fixed limit"
            )
    marks = [m for p in paragraphs for m in _marks(p)]
    if list(nodes) != marks or not marks:
        d._fail(
            "paragraph_chain_unsupported",
            "boundary references are partial, unordered or foreign",
        )
    return paragraphs


# Only the required, native-checked paragraph property family. Character
# properties use the existing closed counter grammar; unknown data is refused.
_P_ATTRIBUTES = {
    "pStyle": "val",
    "widowControl": "val",
    "autoSpaceDE": "val",
    "autoSpaceDN": "val",
    "spacing": "before after beforeAutospacing afterAutospacing line lineRule beforeLines afterLines",
    "ind": "left right start end firstLine hanging leftChars rightChars firstLineChars hangingChars",
    "jc": "val",
    "keepNext": "val",
    "keepLines": "val",
    "pageBreakBefore": "val",
}
_PARAGRAPH_ATTRIBUTES = {
    w(n) for n in ("rsidR", "rsidRPr", "rsidP", "rsidDel", "rsidRDefault")
}
_PARAGRAPH_ATTRIBUTES |= {"{" + d._W14 + "}" + n for n in ("paraId", "textId")}
# A removed paragraph's mark properties do not survive the exact patch. Unlike
# retained runs they need a per-property grammar, not the union of attributes
# known somewhere in character formatting. Keep this family limited to the
# mandatory document and native controls.
_MARK_RPR_ATTRIBUTES = {
    "rStyle": "val",
    "rFonts": "ascii hAnsi eastAsia cs asciiTheme hAnsiTheme eastAsiaTheme cstheme hint",
    "color": "val themeColor themeTint themeShade",
    "lang": "val eastAsia bidi",
    "spacing": "val",
    "kern": "val",
    "sz": "val",
    "szCs": "val",
    "u": "val color themeColor themeTint themeShade",
    "bCs": "val",
}


def _closed_paragraph(paragraph, marks):
    from .counter_revisions import _closed_attributes
    from ._drafting_paragraphs import _structure_paragraph
    from ._drafting_text import _proof_hint

    if set(paragraph.attrib) - _PARAGRAPH_ATTRIBUTES:
        d._fail("paragraph_chain_unsupported", "unknown paragraph attributes")
    props = paragraph.find(w("pPr"))
    if props is not None:
        if paragraph[0] is not props or props.attrib:
            d._fail(
                "paragraph_chain_unsupported",
                "misplaced or attributed paragraph properties",
            )
        for prop in props:
            if prop.tag == w("rPr"):
                if prop.attrib:
                    d._fail(
                        "paragraph_chain_unsupported",
                        "unknown mark property attributes",
                    )
                for m in prop:
                    if m.tag in {w("ins"), w("del")}:
                        continue
                    allowed = (
                        _MARK_RPR_ATTRIBUTES.get(etree.QName(m).localname)
                        if isinstance(m.tag, str)
                        else None
                    )
                    if (
                        allowed is None
                        or m.tag != w(etree.QName(m).localname)
                        or len(m)
                        or set(m.attrib) - {w(n) for n in allowed.split()}
                    ):
                        d._fail(
                            "paragraph_chain_unsupported",
                            "unsupported paragraph-mark formatting",
                        )
                    d._property_tree(m)
            else:
                allowed = (
                    _P_ATTRIBUTES.get(etree.QName(prop).localname)
                    if isinstance(prop.tag, str)
                    else None
                )
                if (
                    allowed is None
                    or prop.tag != w(etree.QName(prop).localname)
                    or set(prop.attrib) - {w(n) for n in allowed.split()}
                ):
                    d._fail(
                        "paragraph_chain_unsupported",
                        "unsupported paragraph formatting",
                    )
                d._property_tree(prop)
    # Validate ordinary runs separately; do not remove unknown children on the
    # real tree. The detached validation paragraph only omits proven bookmarks
    # and closed, direct spelling hints. Outcomes retain these exact nodes in
    # their original content order; nested or malformed hints still refuse.
    clone = copy.deepcopy(paragraph)
    for node in paragraph:
        if node.tag == w("r"):
            _closed_attributes(node)
    for node in paragraph.iter():
        if (
            not isinstance(node.tag, str)
            or ((node.text or "").strip() and node.tag != w("t"))
            or (node.tail or "").strip()
        ):
            d._fail(
                "paragraph_chain_unsupported",
                "hidden scalar, comment or processing instruction",
            )
        if node.tag == w("rPr") and (
            len({c.tag for c in node}) != len(node)
            or node.getparent().tag == w("r")
            and node.getparent()[0] is not node
        ):
            d._fail("paragraph_chain_unsupported", "ambiguous character formatting")
    for node in list(clone):
        if node.tag in {w("bookmarkStart"), w("bookmarkEnd")} or _proof_hint(node):
            clone.remove(node)
    _structure_paragraph(clone, _marks(clone))
    for mark in marks:
        if (
            len(mark)
            or set(mark.attrib) - {w("id"), w("author"), w("date")}
            or not mark.get(w("id"), "").isascii()
            or not mark.get(w("id"), "").isdecimal()
            or len(mark.get(w("id"), "")) > 10
        ):
            d._fail("paragraph_chain_unsupported", "unsupported boundary mark shape")


def _bookmarks(root, paragraphs):
    from ._drafting_paragraphs import _assert_insertion_boundary

    _assert_insertion_boundary(paragraphs[0], "before")
    _assert_insertion_boundary(paragraphs[-1], "after")
    selected = [
        n
        for p in paragraphs
        for n in p
        if n.tag in {w("bookmarkStart"), w("bookmarkEnd")}
    ]
    opened, seen = set(), set()
    for node in selected:
        allowed = {w("id"), w("name")} if node.tag == w("bookmarkStart") else {w("id")}
        value = node.get(w("id"), "")
        if (
            len(node)
            or set(node.attrib) != allowed
            or not value.isascii()
            or not value.isdecimal()
            or len(value) > 10
            or node.tag == w("bookmarkStart")
            and not node.get(w("name"))
        ):
            d._fail("paragraph_chain_unsupported", "unsupported internal bookmark")
        key = str(int(value))
        if node.tag == w("bookmarkStart"):
            if key in seen:
                d._fail("paragraph_chain_unsupported", "duplicate bookmark identity")
            seen.add(key)
            opened.add(key)
        elif key not in opened:
            d._fail(
                "paragraph_chain_unsupported",
                "bookmark is not contained in the component",
            )
        else:
            opened.remove(key)
    if opened:
        d._fail("paragraph_chain_unsupported", "unclosed internal bookmark")
    # Unknown/duplicate endpoints anywhere cannot silently borrow this pair.
    for node in root.iter():
        if node.tag in {w("bookmarkStart"), w("bookmarkEnd")}:
            value = node.get(w("id"), "")
            if (
                value.isascii()
                and value.isdecimal()
                and len(value) <= 10
                and str(int(value)) in seen
                and node not in selected
            ):
                d._fail(
                    "paragraph_chain_unsupported",
                    "bookmark identity is reused outside the component",
                )
    return len(seen)


def segments(paragraphs, action):
    removed = w("del" if action == "accept" else "ins")
    groups, pending = [], []
    for index, paragraph in enumerate(paragraphs):
        pending.append(index)
        if not any(m.tag == removed for m in _marks(paragraph)):
            groups.append(pending)
            pending = []
    if pending:
        d._fail("paragraph_chain_unsupported", "missing final boundary")
    return groups


def outcomes(root, nodes, parts):
    paragraphs = component(nodes)
    if (3 * len(paragraphs) + 12) * len(d._xml(root)) > d.MAX_FINGERPRINT_BYTES:
        d._fail(
            "resource_limit_exceeded", "paragraph chain exceeds its validation budget"
        )
    for p in paragraphs:
        _closed_paragraph(p, _marks(p))
    bookmark_count = _bookmarks(root, paragraphs)
    # Validate the exact full-document context after removing only the selected
    # proven leaf marks; every other pending dependency remains visible.
    clone = copy.deepcopy(root)
    mapping = dict(zip(root.iter(), clone.iter(), strict=True))
    for mark in nodes:
        mapping[mark].getparent().remove(mapping[mark])
    from .paragraph_numbering import _Resolver

    numbering = _Resolver(parts, clone)
    positions = {p: i for i, p in enumerate(numbering.paragraphs)}
    for p in paragraphs:
        d._main_context(clone, mapping[p])
        d._require_style_dependencies(parts, mapping[p])
        if numbering.effective(positions[mapping[p]]).num_id is not None:
            d._fail(
                "paragraph_chain_unsupported",
                "numbered paragraph chains need a separate numbering proof",
            )
    result, grouped = {}, {}
    parent = paragraphs[0].getparent()
    index = parent.index(paragraphs[0])
    for action in ("accept", "reject"):
        grouped[action] = segments(paragraphs, action)
        output = []
        for group in grouped[action]:
            survivor = copy.deepcopy(mapping[paragraphs[group[-1]]])
            for child in list(survivor):
                if child.tag != w("pPr"):
                    survivor.remove(child)
            for i in group:
                survivor.extend(
                    copy.deepcopy(n) for n in paragraphs[i] if n.tag != w("pPr")
                )
            output.append(survivor)
        projected = copy.deepcopy(root)
        owner = projected
        for offset in d._path(root, parent):
            owner = owner[offset]
        d._replace_span(
            owner,
            index,
            list(owner)[index : index + len(paragraphs)],
            copy.deepcopy(output),
        )
        for p in list(owner)[index : index + len(output)]:
            d._main_context(projected, p)
            d._require_style_dependencies(parts, p)
        result[action] = output

    def text(ps):
        return "\n".join(d._reading_text(d._paragraph_segments(p)) for p in ps)

    facts = dict(
        operation="decide_revision",
        revision_kind="paragraph_chain",
        members=[
            dict(
                kind=etree.QName(n).localname,
                id=n.get(w("id")),
                author=n.get(w("author")),
                date=n.get(w("date")),
            )
            for n in nodes
        ],
        current_text=text(paragraphs),
        accepted_text=text(result["accept"]),
        rejected_text=text(result["reject"]),
        current_properties=None,
        previous_properties=None,
        accept_refusal_code=None,
        reject_refusal_code=None,
        fixed_width_validation_scope="not_applicable",
        table_validation_scope="not_applicable",
        table_group_members=[],
    )
    facts["paragraph_chain_context"] = dict(
        paragraph_count=len(paragraphs),
        boundary_revision_count=len(nodes),
        bookmark_count=bookmark_count,
        accepted_segments=grouped["accept"],
        rejected_segments=grouped["reject"],
        formatting_policy="rightmost_paragraph",
        decision_scope="all_boundary_marks",
    )
    if any(
        len(facts[key]) > d.MAX_PREVIEW_CHARS
        for key in ("current_text", "accepted_text", "rejected_text")
    ):
        d._fail(
            "resource_limit_exceeded", "paragraph chain preview exceeds its fixed limit"
        )
    return paragraphs, result, facts
