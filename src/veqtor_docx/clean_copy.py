# SPDX-License-Identifier: Apache-2.0
"""Internal, bounded accept-all candidate engine. Never publishes a file.

This is the PR 8 engine, not an outgoing bundle or a statement that a document
is ready to send. Comments, metadata and tracking settings are retained. The
caller must obtain authorization and publish the returned bytes separately.
"""

from __future__ import annotations

import copy
import hashlib
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

from lxml import etree

from . import revision_decisions as decisions
from ._drafting_paragraphs import (
    paragraph_structure_candidates,
    paragraph_structure_outcome,
    paragraph_structure_unit,
)
from ._ooxml import (
    DOCUMENT_PART,
    canonical_body_flow_v1,
    iter_canonical_paragraph_nodes,
    load_validated_docx,
    parse_xml,
    require_single_direct_document_body,
    w,
)
from ._paragraph_edits import _REVISION_TAGS
from .apply import ApplyError, _output_archive_bytes
from .drafting import _require_unprotected
from .revision_inventory import inventory_package_revisions
from .table_rows import (
    row_structure_candidates,
    row_structure_outcome,
    row_structure_unit,
)

# Small and structural selections retain the selective decision proof. Large
# ordinary selections compose bounded local proofs and one exact whole-document
# replay/reversal instead of cloning the whole document for every revision.
MAX_CLEAN_VALIDATION_BYTES = 256 * 1024 * 1024
MAX_CLEAN_DECISIONS = 20_000
_BATCH_MIN_REVISIONS = 128
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


class _ValidationParts(Mapping):
    """Charge actual repeated dependency reads without mutating shared helpers."""

    def __init__(self, parts, initial):
        self.parts = parts
        self.consumed = initial

    def __getitem__(self, name):
        value = self.parts[name]
        self.consume(len(value))
        return value

    def consume(self, amount):
        self.consumed += amount
        if self.consumed > MAX_CLEAN_VALIDATION_BYTES:
            _fail("resource_limit_exceeded", "the clean-copy dependency validation budget was exceeded")

    def __iter__(self):
        return iter(self.parts)

    def __len__(self):
        return len(self.parts)


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
    body = root.find(w("body"))
    flow = canonical_body_flow_v1(body)
    for node in body.iter():
        if flow.exclusion_kind_for(node) is not None:
            continue  # A textbox endpoint cannot close a main-story range.
        if node in affected and (active or node.tag in _RANGES):
            _fail("clean_copy_range_dependency", "a selected decision touches a bookmark or comment range")
        if node.tag in {w("bookmarkStart"), w("commentRangeStart")}:
            active.add((node.tag, node.get(w("id"))))
        elif node.tag in {w("bookmarkEnd"), w("commentRangeEnd")}:
            start = w("bookmarkStart") if node.tag == w("bookmarkEnd") else w("commentRangeStart")
            active.discard((start, node.get(w("id"))))


def _target(root, node, sha):
    from . import counter_revisions
    selected = counter_revisions.candidates(node)
    if selected is not None:
        return selected, counter_revisions.target(root, selected, sha)
    if node.tag in {w("ins"), w("del")} and node.getparent().tag == w("p"):
        # A replacement left outside the minimal counter target is decided in
        # its own later step. It must not discard unknown attributes merely
        # because WML attribute namespaces are broadly inventoried elsewhere.
        counter_revisions._closed_attributes(node)
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


def _ordinary_source_guard(root, nodes):
    """Prove source-global context before any ordinary revision is detached.

    The selective helper remains the authority for global boundary grammar.
    Its detached empty probe cannot overlap a real target; the second traversal
    binds that same canonical boundary state to every actual selected owner.
    No cached context survives a structural change or a different source.
    """
    body = root.find(w("body"))
    flow = canonical_body_flow_v1(body)
    owners = {decisions._owner(node) for node in nodes}
    checked_ancestors = set()
    for owner in owners:
        if flow.exclusion_kind_for(owner) is not None:
            _fail("revision_context_unsupported", "revision is outside canonical main-story content")
        current = owner
        while current is not body:
            if current is None or current.tag not in {
                w("p"), w("r"), w("pPr"), w("tc"), w("tr"), w("tbl"),
            }:
                _fail("revision_context_unsupported", "revision has an unsupported owner or ancestor")
            current = current.getparent()
        for ancestor in (owner, *owner.iterancestors()):
            if ancestor in checked_ancestors:
                continue
            checked_ancestors.add(ancestor)
            for props in ancestor:
                if props.tag in {w("pPr"), w("trPr"), w("tcPr"), w("tblPr")} and any(
                    n.tag in {w("ins"), w("del"), w("cellIns"), w("cellDel"), w("cellMerge")}
                    for n in props.iter()
                ):
                    _fail("revision_dependency_unsupported", "target belongs to a structural revision")

    probe_root = copy.deepcopy(root)
    probe = etree.Element(w("p"))
    probe_root.find(w("body")).insert(0, probe)
    decisions._main_context(probe_root, probe)
    canonical = {n for p in flow.paragraphs for n in iter_canonical_paragraph_nodes(p.element)}
    for paragraph in flow.paragraphs:
        for ancestor in paragraph.element.iterancestors():
            canonical.add(ancestor)
            if ancestor is body:
                break
    fields = 0
    active = set()
    protected = set()
    ranges = set()
    for event, item in etree.iterwalk(body, events=("start", "end")):
        if event != "start":
            continue
        if item in owners and (fields or active):
            _fail("revision_dependency_unsupported", "a field, permission or revision range encloses the target")
        # Bookmark/comment protection has the same exclusion and document-order
        # semantics as _range_guard, including endpoints outside paragraphs.
        if flow.exclusion_kind_for(item) is None:
            if ranges or item.tag in _RANGES:
                protected.add(item)
            if item.tag in {w("bookmarkStart"), w("commentRangeStart")}:
                ranges.add((item.tag, item.get(w("id"))))
            elif item.tag in {w("bookmarkEnd"), w("commentRangeEnd")}:
                start = w("bookmarkStart") if item.tag == w("bookmarkEnd") else w("commentRangeStart")
                ranges.discard((start, item.get(w("id"))))
        if item.getparent() not in canonical:
            continue
        name = etree.QName(item).localname if isinstance(item.tag, str) else ""
        field = item.tag in {w("fldChar"), w("instrText"), w("fldSimple")}
        boundary = name in {"permStart", "permEnd"} or item.tag in _REVISION_TAGS and "Range" in name
        if (field or boundary) and any(ancestor in owners for ancestor in (item, *item.iterancestors())):
            _fail("revision_dependency_unsupported", "a field, permission or revision range touches the target")
        if item.tag == w("fldChar"):
            kind = item.get(w("fldCharType"))
            fields += 1 if kind == "begin" else -1 if kind == "end" else 0
        elif boundary:
            base = name.removesuffix("Start").removesuffix("End")
            key = (base, str(int(item.get(w("id")))))
            if name.endswith("Start"):
                active.add(key)
            else:
                active.remove(key)  # The unmodified helper proved this grammar.
    for node in nodes:
        domain = node.getparent() if etree.QName(node).localname in decisions.PROPERTY_KINDS else node
        if any(item in protected for item in domain.iter()):
            _fail("clean_copy_range_dependency", "a selected decision touches a bookmark or comment range")


_CONTEXT_PROPS = frozenset(w(name) for name in ("pPr", "rPr", "tcPr", "trPr", "tblPr", "tblPrEx", "tblGrid"))
_STYLE_REFS = frozenset(w(name) for name in ("pStyle", "rStyle", "tblStyle", "basedOn", "link", "styleLink", "numStyleLink", "numId"))
_DEPENDENCIES = frozenset(("word/styles.xml", "word/numbering.xml", "word/stylesWithEffects.xml", "word/_rels/document.xml.rels"))


def _needs_full_context(root):
    """Retain selective proofs for contexts the local batch cannot isolate.

    Run formatting can survive beside a field spanning other paragraphs; a
    complete paragraph cannot prove that field's grammar in isolation. Nested
    table histories may also affect an outer table's style dependency outcome.
    Keep the existing bounded whole-context path for these selections.
    Cached DATE/TIME text decisions also require the full source-global proof;
    the local batch guard never removes or ignores field controls.
    """
    nodes = _nodes(root)
    if any(any(ancestor.tag in _REVISION_TAGS for ancestor in node.iterancestors()) for node in nodes):
        return True
    tables = {table for node in nodes if (table := decisions.decision_table([node])) is not None}
    if any(next(table.iterancestors(w("tbl")), None) is not None
           or any(item is not table for item in table.iter(w("tbl"))) for table in tables):
        return True
    paragraphs = {next(node.iterancestors(w("p")), None) for node in nodes}
    return any(node.tag in {w("fldChar"), w("instrText"), w("fldSimple"), w("permStart"), w("permEnd")}
               or node.tag in _REVISION_TAGS and "Range" in etree.QName(node).localname
               for paragraph in paragraphs if paragraph is not None for node in paragraph.iter())


def _local_context(root, node, budget):
    """Keep a complete paragraph or property set and every ancestor property.

    This is only a local grammar input. Source-global fields/ranges, excluded
    owners and structural ancestry have already been proved on the immutable
    full source. Table geometry and formatting dependencies use the full tree,
    never this reduced context; no numbering sequence is inferred from it.
    """
    paragraph = next(node.iterancestors(w("p")), None)
    domain = paragraph if paragraph is not None else node.getparent()
    chain = list(reversed(list(domain.iterancestors())))
    local_root = None
    parent = None
    for position, source in enumerate(chain):
        shell = etree.Element(source.tag, dict(source.attrib), nsmap=source.nsmap)
        shell.text, shell.tail = source.text, source.tail
        if parent is not None:
            parent.append(shell)
        else:
            local_root = shell
        branch = chain[position + 1] if position + 1 < len(chain) else domain
        for child in source:
            if child is not branch and child.tag in _CONTEXT_PROPS:
                shell.append(copy.deepcopy(child))
        parent = shell
    copied = copy.deepcopy(domain)
    parent.append(copied)
    mapped = dict(zip(domain.iter(), copied.iter(), strict=True))
    # A block-property owner needs a canonical descendant for the local helper's
    # story index; its real descendants are checked in the full source/outcome.
    if paragraph is None:
        owner = copied.getparent()
        if owner.tag == w("tbl"):
            owner = etree.SubElement(owner, w("tr"))
        if owner.tag == w("tr"):
            owner = etree.SubElement(owner, w("tc"))
        if owner.tag == w("tc"):
            etree.SubElement(owner, w("p"))
    budget.consume(4 * len(etree.tostring(local_root)))
    return local_root, mapped[node]


class _StyleProofs:
    """Cache only successful existing dependency proofs for exact semantics.

    The existing dependency walker reads these reference tags/val attributes,
    table-property ancestry, and immutable dependency bytes. Text/direct format
    values cannot affect that walk. Distinct/missing references get a fresh
    proof; errors never enter the cache. Byte hashing and every cache-miss read
    share the same deterministic work budget as local and whole-tree proofs.
    """
    def __init__(self, parts):
        self.parts = parts
        self.dependencies = {name: data for name, data in parts.parts.items() if name in _DEPENDENCIES}
        parts.consume(sum(map(len, self.dependencies.values())))
        self.identity = tuple(sorted((name, _sha(data)) for name, data in self.dependencies.items()))
        self.checked = set()

    def require(self, paragraph):
        inputs = [paragraph]
        for table in paragraph.iterancestors(w("tbl")):
            props = table.find(w("tblPr"))
            if props is not None:
                inputs.append(props)
        self.parts.consume(sum(len(etree.tostring(item)) for item in inputs))
        refs = frozenset((node.tag, node.get(w("val"))) for item in inputs for node in item.iter() if node.tag in _STYLE_REFS)
        key = self.identity, refs
        if key not in self.checked:
            try:
                decisions._require_style_dependencies(self.parts, paragraph)
            except decisions.DocxError as exc:
                _fail(getattr(exc, "code", "revision_dependency_unsupported"), "the selected outcome has an unsupported dependency")
            self.checked.add(key)

    def verify_immutable(self):
        current = {name: data for name, data in self.parts.parts.items() if name in _DEPENDENCIES}
        if current != self.dependencies:
            _fail("round_trip_failed", "formatting dependencies changed during clean-copy proof")


def _path_node(root, path):
    current = root
    for index in path:
        current = current[index]
    return current


def _remember_parent_paths(domain, base, revisions, paths, budget):
    """Index only needed parents in one traversal, avoiding sibling rescans.

    Ordinary property outcomes replace one property set with one property set.
    Text outcomes run last and change only children of their stable paragraph.
    Thus stored parent paths remain valid; newly copied nested property parents
    are indexed again from their actual insertion position.
    """
    wanted = {node.getparent().getparent() if etree.QName(node).localname in decisions.PROPERTY_KINDS
              else node.getparent() for node in revisions}
    pending = [(domain, base)]
    while pending:
        node, path = pending.pop()
        if node in wanted:
            paths[node] = path
            budget.consume(8 * len(path))
        pending.extend((child, (*path, index)) for index, child in reversed(list(enumerate(node))))


def _apply_batch_patch(root, path, patch):
    """Replay only the proved local preimage; kept separate for fault tests."""
    parent = _path_node(root, path)
    present = list(parent)[patch.index:patch.index + len(patch.before)]
    context = etree.Element("comparison", nsmap=parent.nsmap)
    context.extend(copy.deepcopy(patch.before))
    if [etree.tostring(n) for n in present] != [etree.tostring(n) for n in context]:
        _fail("round_trip_failed", "a clean batch patch no longer matches its exact preimage")
    decisions._replace_span(parent, patch.index, present, copy.deepcopy(patch.after))


def _ordinary_batch(root, parts):
    """Compose accept-only ordinary patches with one independent global proof."""
    original = etree.tostring(root)
    parts.consume(8 * len(original))
    original_nodes = _nodes(root)
    if any(decisions.is_row_unit([n]) or decisions.is_paragraph_unit([n]) for n in original_nodes):
        _fail("round_trip_failed", "a structural unit cannot enter ordinary batch validation")
    _ordinary_source_guard(root, original_nodes)
    planned = copy.deepcopy(root)
    nodes = _nodes(planned)
    by_table = {}
    for node in nodes:
        table = decisions.decision_table([node])
        if table is not None:
            by_table.setdefault(table, []).append(node)
    table_nodes = [node for selection in by_table.values() for node in selection]
    for table, selection in by_table.items():
        if len(selection) > 1 and (
            next(table.iterancestors(w("tbl")), None) is not None
            or any(n is not table for n in table.iter(w("tbl")))
        ):
            _fail("revision_dependency_unsupported", "nested table groups are unsupported")
    table_properties = set(table_nodes)
    property_order, scheduled_tables = [], set()
    for node in nodes:
        if etree.QName(node).localname not in decisions.PROPERTY_KINDS:
            continue
        table = decisions.decision_table([node])
        if table is None:
            property_order.append(node)
        elif table not in scheduled_tables:
            scheduled_tables.add(table)
            property_order.extend(by_table[table])
    queue = deque(property_order + [n for n in nodes if etree.QName(n).localname not in decisions.PROPERTY_KINDS])
    style_proofs = _StyleProofs(parts)
    parent_paths = {}
    _remember_parent_paths(planned, (), nodes, parent_paths, parts)
    ledger, facts = [], []
    geometric_tables = {decisions.decision_table([n]) for n in table_nodes if n.tag in {w("tcPrChange"), w("trPrChange"), w("tblGridChange")}}
    # Geometry covers the table, while style dependencies follow the selective
    # owners: a cell/row change must not inspect untouched siblings' styles.
    style_regions = {
        table: tuple(dict.fromkeys(n.getparent().getparent() for n in selection))
        for table, selection in by_table.items()
    }
    group_ends = {selection[-1]: table for table, selection in by_table.items()}
    while queue:
        node = queue.popleft()
        if not any(ancestor is planned for ancestor in node.iterancestors()):
            continue  # A preceding property replacement copied a nested history.
        local_root, local_node = _local_context(planned, node, parts)
        kind = etree.QName(node).localname
        if kind in {"ins", "del"}:
            decisions._validate_text(local_root, [local_node])
            old = [node]
        else:
            decisions._validate_property(local_root, local_node)
            region = decisions.decision_region([node])
            parts.consume(len(etree.tostring(region)))
            decisions._context_text(region)
            decisions._property_view(node.getparent(), kind)
            decisions._property_view(decisions._outcome([node], "reject", kind)[0], kind)
            old = [node.getparent()]
        decisions._alignment_dependency(node, "accept", kind)
        decisions._row_property_dependency(node, "accept", kind)
        parent, index = old[0].getparent(), old[0].getparent().index(old[0])
        chosen = decisions._outcome([node], "accept", kind)
        patch = decisions.DecisionPatch(parent, index, copy.deepcopy(old), copy.deepcopy(chosen))
        if parent not in parent_paths:
            _fail("round_trip_failed", "a clean batch parent has no source-bound path")
        path = parent_paths[parent]
        detail = {"revision_kind": kind, "members": [{"kind": kind, "id": node.get(w("id")), "author": node.get(w("author")), "date": node.get(w("date"))}]}
        decisions._replace_span(parent, index, old, chosen)
        ledger.append((path, patch))
        facts.append(detail)
        copied_revisions = []
        for offset, output in enumerate(chosen):
            nested = _nodes(output)
            if nested:
                _remember_parent_paths(output, (*path, index + offset), nested, parent_paths, parts)
                copied_revisions.extend(nested)
        for item in reversed(copied_revisions):
            queue.appendleft(item)
        if node in group_ends:
            # Preserve the selective stage order: a preceding paragraph/run
            # history may resolve its own style before this table group, but a
            # later text decision cannot erase an unproved group dependency.
            table = group_ends[node]
            if table in geometric_tables:
                decisions._grid(table)
            decisions.validate_fixed_table_widths(table)
            checked_paragraphs = set()
            for region in style_regions[table]:
                for paragraph in region.iter(w("p")):
                    if paragraph not in checked_paragraphs:
                        style_proofs.require(paragraph)
                        checked_paragraphs.add(paragraph)
        elif node not in table_properties:
            paragraphs = [parent] if parent.tag == w("p") else list(parent.iter(w("p")))
            if parent.tag in {w("r"), w("pPr")}:
                paragraphs = [next(parent.iterancestors(w("p")))]
            for paragraph in paragraphs:
                style_proofs.require(paragraph)
    if _nodes(planned):
        _fail("round_trip_failed", "ordinary batch did not decide every selected revision")
    style_proofs.verify_immutable()
    reverse = copy.deepcopy(planned)
    for path, patch in reversed(ledger):
        patch.reverse(_path_node(reverse, path))
    if etree.tostring(reverse) != original:
        _fail("round_trip_failed", "clean batch changed content outside its exact composed patches")
    expected = etree.tostring(planned)
    for path, patch in ledger:
        _apply_batch_patch(root, path, patch)
    if etree.tostring(root) != expected:
        _fail("round_trip_failed", "clean batch replay differs from its independently reversed plan")
    return facts


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
    batch = count >= _BATCH_MIN_REVISIONS and not _needs_full_context(root)
    initial = 0 if batch else source_size * count * 8
    # Ordinary batches pay for bounded local proofs and one whole-tree proof;
    # small/structural decisions still pay for their original whole-root work.
    validation_parts = _ValidationParts(parts, 0)
    validation_parts.consume(initial)
    _structural_units(root)  # Validate the complete original structural selection.
    facts = []
    remaining = count
    while remaining:
        if batch and not _structural_units(root):
            facts.extend(_ordinary_batch(root, validation_parts))
            remaining = 0
            break
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
            if batch:
                validation_parts.consume(8 * len(etree.tostring(root)))
            original = etree.tostring(root)
            if decisions.is_row_unit(nodes) or decisions.is_paragraph_unit(nodes):
                expected, detail = _structural_plan(root, nodes, validation_parts)
                _range_guard(root, expected)
                present = list(expected.parent)[expected.index:expected.index + len(expected.before)]
                decisions._replace_span(expected.parent, expected.index, present, copy.deepcopy(expected.after))
                patch = expected
            else:
                from .counter_revisions import is_unit
                counter_options = {"counter_decision": "accept"} if is_unit(nodes) else {}
                expected, _, _ = decisions.plan_decision(root, nodes, "accept", validation_parts, True, groups.get(index), **counter_options)
                _range_guard(root, expected)
                patch, detail = decisions.apply_decision(root, nodes, "accept", validation_parts, True, groups.get(index), **counter_options)
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
