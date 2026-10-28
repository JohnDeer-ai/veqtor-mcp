# SPDX-License-Identifier: Apache-2.0
"""Closed suffix/whole counter-insertion units with two explicit decisions."""
import copy

from lxml import etree

from ._ooxml import DocxError, w
from . import revision_decisions as d
from ._drafting_text import _proof_hint as _proof


def is_unit(nodes):
    return (len(nodes) in {2, 3} and nodes[0].tag == w("ins")
            and nodes[1].tag == w("del") and nodes[1].getparent() is nodes[0])


_RUN_PROPERTIES = frozenset(w(tag) for tag in """
rStyle rFonts b bCs i iCs caps smallCaps strike dstrike outline shadow emboss
imprint noProof snapToGrid vanish webHidden color spacing w kern position sz szCs
highlight u effect bdr shd fitText vertAlign rtl cs em lang eastAsianLayout
specVanish oMath
""".split())
_PROPERTY_ATTRIBUTES = frozenset(w(tag) for tag in """
val ascii hAnsi eastAsia cs asciiTheme hAnsiTheme eastAsiaTheme cstheme hint
themeColor themeTint themeShade color fill themeFill themeFillTint themeFillShade
sz space shadow frame id bidi combine combineBrackets vert vertCompress
""".split())


def _closed_attributes(wrapper, *, allow_history=False):
    for node in wrapper.iter():
        if _proof(node):
            continue
        if node.tag in {w("ins"), w("del")} or (allow_history and node.tag == w("rPrChange")):
            allowed = {w("id"), w("author"), w("date")}
        elif node.tag == w("r"):
            allowed = {w("rsidR"), w("rsidDel"), w("rsidRPr")}
        elif node.tag in {w("t"), w("delText")}:
            allowed = {"{http://www.w3.org/XML/1998/namespace}space"}
        elif node.tag == w("br"):
            allowed = {w("type"), w("clear")}
        elif node.tag in {w("rPr"), w("tab"), w("cr"), w("lastRenderedPageBreak")}:
            allowed = set()
        elif node.tag in _RUN_PROPERTIES:
            allowed = _PROPERTY_ATTRIBUTES
            if node.getparent().tag != w("rPr") or len(node):
                d._fail("counter_unit_unsupported", "counter run property has unsupported placement")
        elif d._known_decision_property(node) and node.tag == d._LIGATURES:
            allowed = {d._LIGATURE_VALUE}
        else:
            d._fail("counter_unit_unsupported", "counter contains unknown run markup")
        if set(node.attrib) - allowed:
            d._fail("counter_unit_unsupported", "counter contains unknown attributes")


def formatting_candidates(history):
    """Cheap ancestry discovery only; never validates or clones a document."""
    props = history.getparent()
    run = props.getparent() if props is not None else None
    wrapper = run.getparent() if run is not None else None
    original = wrapper.getparent() if wrapper is not None and wrapper.tag == w("del") else wrapper
    if original is None or original.tag != w("ins") or run.tag != w("r"):
        return None
    selected = candidates(original)
    if selected is None or wrapper not in selected or props.tag != w("rPr"):
        return None
    return original, selected


def formatting_context(root, history):
    """Prove only a selected run history inside an otherwise closed counter.

    Histories are removed on a detached context-validation clone only. Actual
    source/history bytes remain selected individually by the ordinary property
    patch. No del/ins ancestor is admitted by the general context validator.
    """
    found = formatting_candidates(history)
    if found is None:
        return None
    if 4 * len(d._xml(root)) > d.MAX_FINGERPRINT_BYTES:
        d._fail("resource_limit_exceeded", "counter formatting validation exceeds the fixed work budget")
    original, selected = found
    _closed_attributes(original, allow_history=True)
    histories = list(original.iter(w("rPrChange")))
    for change in histories:
        properties = change.getparent()
        owner = properties.getparent()
        if (properties.tag != w("rPr") or owner.tag != w("r") or owner.getparent() not in selected
                or len(change) != 1 or change[0].tag != w("rPr")
                or len(properties.findall(w("rPrChange"))) != 1 or properties[-1] is not change):
            d._fail("counter_unit_unsupported", "counter formatting history is not an isolated run property set")
    clone = copy.deepcopy(root)
    outer = clone
    for index in d._path(root, original):
        outer = outer[index]
    for change in list(outer.iter(w("rPrChange"))):
        change.getparent().remove(change)
    validate(clone, candidates(outer))
    return original.getparent()


def candidates(node):
    original = node if node.tag == w("ins") else node.getparent()
    if original is None or original.tag != w("ins") or original.getparent() is None:
        return None
    if original.getparent().tag != w("p"):
        return None
    deletions = [child for child in original if child.tag == w("del")]
    if len(deletions) != 1:
        return None
    # Adjacency cannot prove that a following insertion is the replacement:
    # a deletion-only counter can precede an unrelated pending insertion.
    # Its fresh reference must be selected explicitly by the caller instead.
    return [original, deletions[0]]


def target(root, nodes, sha):
    return {"kind": "counter_insertion", "original_insertion_ref": d.reference(root, nodes[0], sha),
            "counter_deletion_ref": d.reference(root, nodes[1], sha),
            "counter_insertion_ref": d.reference(root, nodes[2], sha) if len(nodes) == 3 else None}


def validate(root, nodes, *, preserve_boundaries=False):
    if preserve_boundaries:
        from ._preserved_paragraph_boundaries import validation_view
        clone, selected, boundary_context = validation_view(root, nodes)
        _, _, field_context, _ = validate(clone, selected)
        # Determine the exact original text span, never the detached view.
        parent = nodes[0].getparent()
        old = list(parent)[parent.index(nodes[0]):parent.index(nodes[-1] if len(nodes) == 3 else nodes[0]) + 1]
        return parent, old, field_context, boundary_context

    if not is_unit(nodes):
        d._fail("counter_unit_unsupported", "select the original insertion and its exact counter deletion")
    original, deletion = nodes[:2]
    parent = original.getparent()
    if parent is None or parent.tag != w("p"):
        d._fail("counter_unit_unsupported", "counter unit requires a direct canonical paragraph")
    children = [child for child in original if not _proof(child)]
    if (not children or children[-1] is not deletion or children.count(deletion) != 1
            or any(child.tag != w("r") for child in children[:-1])):
        d._fail("counter_unit_unsupported", "only one complete suffix or whole deletion is supported")
    # Spelling markers are the only allowed non-run children. They are retained
    # in actual outcomes; the validation clone removes only these known hints.
    for wrapper in nodes:
        _closed_attributes(wrapper)
        if ((wrapper.text or "").strip() or (wrapper.tail or "").strip()
                or any(child.tag not in {w("r"), w("del")} and not _proof(child) for child in wrapper)):
            d._fail("counter_unit_unsupported", "counter unit contains unsupported markup")
    if any(child.tag == w("del") for child in deletion):
        d._fail("counter_unit_unsupported", "arbitrary nested deletion is unsupported")
    end = original
    if len(nodes) == 3:
        insertion = nodes[2]
        if insertion.tag != w("ins") or insertion.getparent() is not parent:
            d._fail("counter_unit_unsupported", "counter insertion must share the paragraph")
        end = original.getnext()
        while end is not None and _proof(end):
            end = end.getnext()
        if end is not insertion:
            d._fail("counter_unit_unsupported", "only intervening spelling hints may separate the selected revisions")
    # Reuse the ordinary source-global, structural, field and run checks on a
    # detached equivalent source with the selected nested deletion lifted out.
    clone = copy.deepcopy(root)
    owner = clone
    for index in d._path(root, parent):
        owner = owner[index]
    outer = owner[parent.index(original)]
    nested = outer[list(original).index(deletion)]
    outer.remove(nested)
    owner.insert(owner.index(outer) + 1, nested)
    checked = [nested]
    if any(child.tag == w("r") for child in outer):
        checked.insert(0, outer)
    else:
        owner.remove(outer)
    if len(nodes) == 3:
        # Use its original path/index, not a text match, to avoid ambiguity.
        replacement_index = parent.index(nodes[2]) + (1 if outer.getparent() is owner else 0)
        replacement = owner[replacement_index]
        if replacement.tag != w("ins"):
            d._fail("counter_unit_unsupported", "counter insertion mapping changed")
        checked.append(replacement)
    for wrapper in checked:
        for child in list(wrapper):
            if _proof(child):
                wrapper.remove(child)
    field_context = d._validate_text(clone, checked)
    start_index = parent.index(original)
    old = list(parent)[start_index:parent.index(end) + 1]
    return parent, old, field_context, None


def _restored(children):
    result = copy.deepcopy(list(children))
    for child in result:
        for atom in child.iter(w("delText")):
            atom.tag = w("t")
    return result


def _outcome(nodes, old, original_decision, counter_decision):
    original, deletion = nodes[:2]
    result = []
    if original_decision == "accept":
        for child in original:
            if child is deletion:
                if counter_decision == "reject":
                    result.extend(_restored(deletion))
            else:
                result.append(copy.deepcopy(child))
    result.extend(copy.deepcopy(old[1:-1] if len(nodes) == 3 else old[1:]))
    if len(nodes) == 3 and counter_decision == "accept":
        result.extend(copy.deepcopy(list(nodes[2])))
    return result


def _reading(nodes):
    paragraph = etree.Element(w("p"))
    paragraph.extend(copy.deepcopy(nodes))
    return d._context_text(paragraph)


def evaluate(root, nodes, parts, *, preserve_boundaries=False):
    if len(d._xml(root)) * 4 > d.MAX_FINGERPRINT_BYTES:
        d._fail("resource_limit_exceeded", "counter outcome validation exceeds the fixed work budget")
    parent, old, field_context, boundary_context = validate(root, nodes, preserve_boundaries=preserve_boundaries)
    outcomes = {}
    entries = []
    for original_decision in ("accept", "reject"):
        for counter_decision in ("accept", "reject"):
            after = _outcome(nodes, old, original_decision, counter_decision)
            refusal = None
            try:
                clone = copy.deepcopy(root)
                owner = clone
                for index in d._path(root, parent):
                    owner = owner[index]
                start = parent.index(old[0])
                d._replace_span(owner, start, list(owner)[start:start + len(old)], copy.deepcopy(after))
                d._require_style_dependencies(parts, owner)
            except DocxError as exc:
                refusal = getattr(exc, "code", "revision_dependency_unsupported")
            key = (original_decision, counter_decision)
            outcomes[key] = after
            entries.append({"original_decision": original_decision, "counter_decision": counter_decision,
                            "text": _reading(after), "refusal_code": refusal})
    facts = {"operation": "decide_revision", "revision_kind": "counter_insertion",
             "members": [{"kind": etree.QName(n).localname, "id": n.get(w("id")),
                          "author": n.get(w("author")), "date": n.get(w("date"))} for n in nodes],
             "current_text": _reading(old), "accepted_text": entries[0]["text"],
             "rejected_text": entries[3]["text"], "current_properties": None, "previous_properties": None,
             "accept_refusal_code": entries[0]["refusal_code"], "reject_refusal_code": entries[3]["refusal_code"],
             "table_validation_scope": "not_applicable", "fixed_width_validation_scope": "not_applicable",
             "table_group_members": [], "counter_outcomes": entries, "counter_decision": "accept"}
    if field_context is not None:
        facts["field_result_context"] = field_context
    if boundary_context is not None:
        facts["paragraph_boundary_context"] = boundary_context
    return old, outcomes, facts


def plan(root, nodes, original_decision, counter_decision, parts, *, preserve_boundaries=False):
    if original_decision not in {"accept", "reject"} or counter_decision not in {"accept", "reject"}:
        d._fail("invalid_edit", "both original and counter decisions must be explicit")
    old, outcomes, facts = evaluate(root, nodes, parts, preserve_boundaries=preserve_boundaries)
    selected = next(entry for entry in facts["counter_outcomes"]
                    if entry["original_decision"] == original_decision and entry["counter_decision"] == counter_decision)
    if selected["refusal_code"] is not None:
        d._fail(selected["refusal_code"], "the explicit compound outcome has an unsupported dependency")
    facts.update(decision=original_decision, counter_decision=counter_decision)
    after = outcomes[original_decision, counter_decision]
    return d.DecisionPatch(old[0].getparent(), old[0].getparent().index(old[0]),
                           copy.deepcopy(old), copy.deepcopy(after)), after, facts
