# SPDX-License-Identifier: Apache-2.0
"""Internal, bounded accept-all candidate engine. Never publishes a file.

This is the PR 8 engine, not an outgoing bundle or a statement that a document
is ready to send. Comments, metadata and tracking settings are retained. The
caller must obtain authorization and publish the returned bytes separately.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass

from lxml import etree

from . import revision_decisions as decisions
from ._drafting_paragraphs import (
    paragraph_structure_candidates,
    paragraph_structure_unit,
    paragraph_structure_outcome,
)
from ._ooxml import (
    DOCUMENT_PART,
    load_validated_docx,
    parse_xml,
    require_single_direct_document_body,
    w,
)
from ._paragraph_edits import _REVISION_TAGS
from .apply import ApplyError, _output_archive_bytes
from .drafting import _require_unprotected
from .revision_inventory import inventory_package_revisions
from .table_rows import row_structure_candidates, row_structure_unit, row_structure_outcome

# Outcome validation clones the document. Charge its full serialized size for
# every attempted decision, including both plans and the independent reversal.
# A limit refuses the entire candidate; it never produces a partial clean copy.
MAX_CLEAN_VALIDATION_BYTES = 256 * 1024 * 1024
MAX_CLEAN_DECISIONS = 20_000
_RANGES = frozenset(w(name) for name in (
    "bookmarkStart", "bookmarkEnd", "commentRangeStart", "commentRangeEnd",
    "commentReference",
))


@dataclass(frozen=True)
class CleanCopyCandidate:
    payload: bytes
    source_sha256: str
    candidate_sha256: str
    accepted_revision_count: int
    inventory_before: dict
    inventory_after: dict
    decisions: tuple[dict, ...]


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _nodes(root):
    return [node for node in root.iter() if node.tag in _REVISION_TAGS]


def _fail(code, detail):
    raise ApplyError(code, detail)


def _require_inventory(inventory):
    if inventory["blockers"] or not inventory["complete"]:
        raise ApplyError(
            "clean_copy_unsupported",
            "the entire package must have inspectable, supported revision markup",
            blockers=copy.deepcopy(inventory["blockers"]),
        )


def _range_guard(root, patch):
    """Retain range bindings; changing a touched range needs PR 9 policy.

    Markers outside a deletion may still enclose it. Inspect document order,
    including ranges spanning paragraphs, rather than only descendants.
    """
    affected = {n for item in list(patch.parent)[patch.index:patch.index + len(patch.before)]
                for n in item.iter()}
    active = set()
    for node in root.iter():
        if node in affected and (active or node.tag in _RANGES):
            _fail("clean_copy_range_dependency", "a selected decision touches a bookmark or comment range")
        if node.tag in {w("bookmarkStart"), w("commentRangeStart")}:
            active.add((node.tag, node.get(w("id"))))
        elif node.tag in {w("bookmarkEnd"), w("commentRangeEnd")}:
            start = w("bookmarkStart") if node.tag == w("bookmarkEnd") else w("commentRangeStart")
            active.discard((start, node.get(w("id"))))


def _target(root, node, sha):
    ref = decisions.reference(root, node, sha)
    if decisions.is_row_unit([node]):
        selected = row_structure_candidates(node)
        if not selected:
            _fail("row_unit_unsupported", "the row revision is not a complete supported unit")
        return selected, {"kind": "row_structure", "row_mark_ref": ref,
                          "content_revision_refs": [decisions.reference(root, n, sha) for n in selected[1:]]}
    if decisions.is_paragraph_unit([node]):
        selected = paragraph_structure_candidates(node)
        if not selected:
            _fail("paragraph_unit_unsupported", "the paragraph revision is not a complete supported unit")
        return selected, {"kind": "paragraph_structure", "paragraph_mark_ref": ref,
                          "text_revision_refs": [decisions.reference(root, n, sha) for n in selected[1:]]}
    return [node], {"kind": "single", "revision_ref": ref}


def _stage(root):
    """Resolve fresh units after structural patches; never reuse stale paths."""
    pending = _nodes(root)
    rows = [n for n in pending if decisions.is_row_unit([n])]
    if rows:
        return rows[:1]
    paragraphs = [n for n in pending if decisions.is_paragraph_unit([n])]
    if paragraphs:
        return [paragraphs[-1]]
    properties = [n for n in pending if etree.QName(n).localname in decisions.PROPERTY_KINDS]
    if properties:
        table = decisions.decision_table([properties[0]])
        return [n for n in properties if decisions.decision_table([n]) is table] if table is not None else [properties[0]]
    return pending[:1]


def _structural_units(root):
    units = []
    occupied = set()
    for node in _nodes(root):
        if decisions.is_row_unit([node]):
            nodes = row_structure_candidates(node)
            if not nodes:
                _fail("row_unit_unsupported", "a structural row needs its complete supported content")
            unit = row_structure_unit(root, nodes)
            domain = [unit["row"]]
        elif decisions.is_paragraph_unit([node]):
            nodes = paragraph_structure_candidates(node)
            if not nodes:
                _fail("paragraph_unit_unsupported", "a structural paragraph needs its complete supported content")
            unit = paragraph_structure_unit(root, nodes)
            domain = [unit["paragraph"], unit["following"]]
        else:
            continue
        # An ancestor or identical domain is a structural overlap; do not use
        # an accept-all context to silently make an ambiguous unit decidable.
        descendants = {n for item in domain for n in item.iter()}
        if occupied & descendants:
            _fail("revision_operation_overlap", "clean structural units have overlapping domains")
        occupied.update(descendants)
        units.append((nodes, unit, domain))
    return units


def _structural_plan(root, nodes, parts):
    """Validate one local patch in the explicitly selected accept-all context.

    Other complete, disjoint structural units are accepted only in a detached
    counterfactual tree. Public single-decision guards remain unchanged. Only
    the selected local patch is mapped back to the actual candidate.
    """
    units = _structural_units(root)
    clone = copy.deepcopy(root)
    mapped = dict(zip(root.iter(), clone.iter(), strict=True))
    for selected, _, _ in reversed(units):
        if selected[0] is nodes[0]:
            continue
        copied = [mapped[n] for n in selected]
        if decisions.is_row_unit(copied):
            unit = row_structure_unit(clone, copied)
            old = [unit["row"]]
            outcome = row_structure_outcome(unit, "accept")
        else:
            unit = paragraph_structure_unit(clone, copied)
            old = [unit["paragraph"], unit["following"]]
            outcome = paragraph_structure_outcome(unit, "accept")
        parent, index = old[0].getparent(), old[0].getparent().index(old[0])
        decisions._replace_span(parent, index, old, outcome)
    selected = [mapped[n] for n in nodes]
    planned, _, facts = decisions.plan_decision(clone, selected, "accept", parts)
    inverse = {copy_node: source_node for source_node, copy_node in mapped.items()}
    if planned.parent not in inverse:
        _fail("round_trip_failed", "the structural context replaced the selected parent")
    parent = inverse[planned.parent]
    old = next(domain for selected, _, domain in units if selected[0] is nodes[0])
    index = parent.index(old[0])
    if [etree.tostring(n) for n in copy.deepcopy(old)] != [etree.tostring(n) for n in planned.before]:
        _fail("round_trip_failed", "the structural context changed the selected preimage")
    return decisions.DecisionPatch(parent, index, copy.deepcopy(old), planned.after), facts


def prepare_clean_copy(payload: bytes, *, expected_source_sha256: str) -> CleanCopyCandidate:
    """Accept every supported revision in a detached package or refuse all.

    Supported mutations are confined to canonical main-body units already
    supported by the selective decision engine. Revision-free external stories
    are inspected and retained, while revisions there block this version.
    There is no author argument: accepting revisions never assigns authorship.
    """
    if type(payload) is not bytes or _sha(payload) != expected_source_sha256:
        _fail("file_sha256_mismatch", "supply the exact selected source bytes and SHA-256")
    before = inventory_package_revisions(payload)
    _require_inventory(before)
    package = load_validated_docx(payload, capture=None)
    parts = dict(package.parts)
    _require_unprotected(parts)
    root = parse_xml(parts[DOCUMENT_PART])
    require_single_direct_document_body(root)
    count = len(_nodes(root))
    if count != len(before["revisions"]):
        _fail("clean_copy_unsupported", "not every package revision belongs to the supported main story")
    if count > MAX_CLEAN_DECISIONS:
        _fail("resource_limit_exceeded", "the clean-copy revision limit was exceeded")
    source_size = len(etree.tostring(root))
    # Reject before quadratic validation, not after constructing a partial file.
    if source_size * count * 8 > MAX_CLEAN_VALIDATION_BYTES:
        _fail("resource_limit_exceeded", "the clean-copy outcome validation budget was exceeded")
    _structural_units(root)  # Validate the complete original structural selection.
    facts = []
    remaining = count
    while remaining:
        chosen = _stage(root)
        selections = {}
        operations = []
        for index, node in enumerate(chosen):
            selected, target = _target(root, node, expected_source_sha256)
            selections[index] = selected
            operations.append({"kind": "decide_revision", "decision": "accept", "target": target})
        groups = decisions.table_decision_groups(root, selections, operations)
        touched_tables = {table for nodes in selections.values()
                          if (table := decisions.decision_table(nodes)) is not None}
        for index, nodes in selections.items():
            original = etree.tostring(root)
            if decisions.is_row_unit(nodes) or decisions.is_paragraph_unit(nodes):
                expected, detail = _structural_plan(root, nodes, parts)
                _range_guard(root, expected)
                present = list(expected.parent)[expected.index:expected.index + len(expected.before)]
                decisions._replace_span(expected.parent, expected.index, present, copy.deepcopy(expected.after))
                patch = expected
            else:
                expected, _, _ = decisions.plan_decision(root, nodes, "accept", parts, True, groups.get(index))
                _range_guard(root, expected)
                patch, detail = decisions.apply_decision(root, nodes, "accept", parts, True, groups.get(index))
            if (patch.parent is not expected.parent or patch.index != expected.index
                    or [etree.tostring(n) for n in patch.before] != [etree.tostring(n) for n in expected.before]
                    or [etree.tostring(n) for n in patch.after] != [etree.tostring(n) for n in expected.after]):
                _fail("round_trip_failed", "the clean decision differs from its exact planned patch")
            reverse = copy.deepcopy(root)
            parent_index = list(root.iter()).index(patch.parent)
            patch.reverse(list(reverse.iter())[parent_index])
            if etree.tostring(reverse) != original:
                _fail("round_trip_failed", "a clean decision changed content outside its exact patch")
            facts.append({"revision_kind": detail["revision_kind"], "members": detail["members"]})
        for group in {id(g): g for g in groups.values() if isinstance(g, decisions.TableDecisionGroup)}.values():
            decisions.validate_table_group(group)
        for table in touched_tables:
            decisions.validate_fixed_table_widths(table)
        current = len(_nodes(root))
        if current >= remaining:
            _fail("round_trip_failed", "clean-copy decisions did not make progress")
        remaining = current
    if count:
        parts[DOCUMENT_PART] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        candidate = _output_archive_bytes(package.infos, parts)
    else:
        candidate = payload  # Already clean: preserve even ZIP bytes exactly.
    after = inventory_package_revisions(candidate)
    _require_inventory(after)
    if after["revisions"]:
        _fail("round_trip_failed", "revision markup remained in the candidate package")
    checked = load_validated_docx(candidate, capture=None)
    if checked.parts[DOCUMENT_PART] != parts[DOCUMENT_PART]:
        _fail("round_trip_failed", "the serialized main part differs from the verified candidate")
    if checked.member_names != package.member_names or any(
        checked.parts[name] != data for name, data in package.parts.items() if name != DOCUMENT_PART
    ):
        _fail("round_trip_failed", "clean-copy processing changed an unselected package part")
    return CleanCopyCandidate(candidate, expected_source_sha256, _sha(candidate), count, before, after, tuple(facts))
