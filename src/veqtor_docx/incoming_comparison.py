# SPDX-License-Identifier: Apache-2.0
"""Read-only, bounded comparison of two explicitly selected DOCX snapshots.

The result partitions every canonical paragraph, including empty ones. Text
projection equality is evidence about strings, not identity, authorship,
transmission, rendered formatting, clean documents, or disabled Track Changes.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from lxml import etree

from ._ooxml import (
    ArchiveValidationError, DocxError, ResourceLimitError, load_validated_docx, parse_xml, w,
)
from ._projection import (
    MAX_ACCEPTED_CURRENT_CHARS_PER_PARAGRAPH, _enforce_text_limit,
    build_document_projections_v1,
)
from .inspect import _paragraph_ref, _snapshot_from_validated
from .round_snapshots import DocumentSnapshot, capture_document_snapshot
from .table_map import TableMapError, _children, build_table_map

LIMITS = MappingProxyType({
    "max_items": 100, "page_text_chars": 100_000, "page_bytes": 2 * 1024 * 1024,
    "projection_chars_per_document": 2_000_000, "revision_occurrences_per_paragraph": 1000,
    "candidate_memberships": 80_000, "report_items": 100_000,
    "report_bytes": 64 * 1024 * 1024, "wall_clock_partial_results": False,
})
POLICY = "incoming_comparison_policy.v1"
SCHEMA = "incoming_comparison.v1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_CURSOR = re.compile(r"ic1:([1-9][0-9]{0,7}):([0-9a-f]{64})\Z")
_REVISION_NAMES = frozenset({
    "ins", "del", "moveFrom", "moveTo", "moveFromRangeStart", "moveFromRangeEnd",
    "moveToRangeStart", "moveToRangeEnd", "rPrChange", "pPrChange", "tcPrChange",
    "trPrChange", "tblPrChange", "tblPrExChange", "tblGridChange", "sectPrChange",
    "numberingChange", "cellIns", "cellDel", "cellMerge",
})
_REVISION_TAGS = {w(name) for name in _REVISION_NAMES}
_PROPERTY_TAGS = {w(name) for name in (
    "pPr", "rPr", "tcPr", "trPr", "tblPr", "tblGrid", "sectPr", "tblPrEx",
)}
_MODES = ("accepted_current_v1", "pending_text_revisions_rejected_v1")
_LIMITATIONS = [
    "roles_client_selected_transmission_and_chronology_not_verified",
    "authorship_and_disabled_tracking_not_established",
    "projections_are_not_clean_documents_or_rendered_word_text",
    "partial_prior_revision_decisions_not_enumerated",
    "matching_does_not_establish_semantic_identity_or_derivation",
    "property_markup_not_rendered_formatting_equivalence",
    "table_topology_excludes_rendered_dimensions_properties_reported_separately",
    "unsupported_parts_compared_as_bytes_only",
    "read_only_no_negotiation_journal",
]


class IncomingComparisonError(DocxError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bound(key: str, observed: int) -> None:
    if observed > LIMITS[key]:
        raise ResourceLimitError(
            f"incoming_comparison_{key}", "comparison incomplete: fixed limit exceeded",
            allowed_count=LIMITS[key], observed_count=observed,
        )


@dataclass(frozen=True)
class IncomingComparison:
    """Immutable internal carrier; pages are independently detached JSON values."""
    metadata: bytes
    items: tuple[bytes, ...]
    digest: str

    def __post_init__(self):
        if type(self.metadata) is not bytes or type(self.items) is not tuple:
            raise IncomingComparisonError("invalid_comparison", "comparison must retain immutable bytes")
        _bound("report_items", len(self.items))
        size = len(self.metadata)
        digest = hashlib.sha256(self.metadata)
        for item in self.items:
            if type(item) is not bytes:
                raise IncomingComparisonError("invalid_comparison", "comparison item must be immutable")
            size += len(item)
            _bound("report_bytes", size)
            digest.update(len(item).to_bytes(8, "big"))
            digest.update(item)
        if type(self.digest) is not str or self.digest != digest.hexdigest():
            raise IncomingComparisonError("invalid_comparison", "comparison digest does not match its records")


@dataclass
class _Document:
    snapshot: Any
    paragraphs: list[dict]
    parts: dict[str, str]
    structures: list[dict]
    geometry_sha256: str | None
    coverage: dict


def _semantic_digests(root):
    """Linear bottom-up XML observation, independent of prefixes/volatile IDs."""
    digests = {}
    for node in reversed(list(root.iter())):
        if not isinstance(node.tag, str):
            continue
        attributes = []
        for name, value in sorted(node.attrib.items()):
            local = etree.QName(name).localname
            if local.startswith("rsid") or local in {"paraId", "textId"}:
                continue
            if node.tag in _REVISION_TAGS and name == w("id"):
                continue
            attributes.append([name, value])
        text = node.text
        if len(node) and text is not None and not text.strip():
            text = None  # XML indentation, not leaf text such as a Word text atom.
        children = [[digests[c], c.tail if c.tail and c.tail.strip() else None]
                    for c in node if c in digests]
        digests[node] = _sha(_json([node.tag, attributes, text, children]))
    return digests


def _structures(payload, side, snapshot, semantic):
    try:
        mapped = build_table_map(payload)
    except TableMapError as exc:
        return [], {}, None, {"complete": False, "reason": exc.code}
    body = (snapshot.paragraphs[0].element.getroottree().getroot()
            if snapshot.paragraphs else parse_xml(snapshot.body_xml))
    if not semantic:
        semantic = _semantic_digests(body)
    table_nodes = [node for node in body.iter(w("tbl"))
                   if snapshot.body_flow.exclusion_kind_for(node) is None]
    property_hashes = {}

    def properties(node):
        return _sha(_json([[child.tag, semantic[child]] for child in node
                           if child.tag in _PROPERTY_TAGS]))

    for ti, node in enumerate(table_nodes):
        property_hashes[(ti,)] = properties(node)
        rows, _, _ = _children(node, "tr", {w("tblPr"), w("tblGrid")})
        for ri, row in enumerate(rows):
            property_hashes[(ti, ri)] = properties(row)
            cells, _, _ = _children(row, "tc", {w("trPr"), w("tblPrEx")})
            for ci, cell in enumerate(cells):
                property_hashes[(ti, ri, ci)] = properties(cell)
    items, addresses, geometry = [], {}, []
    for table in mapped["tables"]:
        table_item = {"kind": "table", "side": side, "reference": table["table_ref"],
                      "parent_cell_ref": table["parent_cell_ref"],
                      "grid_column_count": table["grid_column_count"],
                      "nesting_depth": table["nesting_depth"], "issues": table["issues"],
                      "property_markup_sha256": property_hashes[(table["table_index"],)]}
        items.append(table_item)
        parent = table["parent_cell_ref"]
        shape = {"parent": None if parent is None else [parent[k] for k in (
            "table_index", "row_index", "cell_index")],
            "grid_column_count": table["grid_column_count"], "rows": []}
        for row in table["rows"]:
            items.append({"kind": "row", "side": side, "reference": row["row_ref"],
                          "property_markup_sha256": property_hashes[(table["table_index"], row["row_index"])],
                          **{k: row[k] for k in ("grid_before", "grid_after", "grid_extent", "issues")}})
            row_shape = {k: row[k] for k in ("grid_before", "grid_after", "grid_extent")}
            row_shape["cells"] = []
            for cell in row["cells"]:
                indices = [r["paragraph_index"] for r in cell["paragraph_refs"]]
                for index in indices:
                    addresses[index] = cell["cell_ref"]
                cell_shape = {k: cell[k] for k in ("grid_start", "grid_span", "h_merge", "v_merge")}
                cell_shape["content_kinds"] = [c["kind"] for c in cell["content"]]
                row_shape["cells"].append(cell_shape)
                items.append({"kind": "cell", "side": side, "reference": cell["cell_ref"],
                              "property_markup_sha256": property_hashes[(table["table_index"], row["row_index"], cell["cell_index"])],
                              **cell_shape, "paragraph_indices": indices, "issues": cell["issues"]})
            shape["rows"].append(row_shape)
        geometry.append(shape)
    return items, addresses, _sha(_json(geometry)), {
        "complete": mapped["coverage"]["complete"],
        "reason": None if mapped["coverage"]["complete"] else "table_inventory_incomplete",
    }


def _document(source: DocumentSnapshot, side: str) -> _Document:
    # Recheck even a deliberately tampered internal carrier before deriving facts.
    source = DocumentSnapshot(source.payload, source.sha256, source.size_bytes)
    package = load_validated_docx(source.payload, capture=None)
    parts = {name: _sha(data) for name, data in package.parts.items()}
    snapshot = _snapshot_from_validated(package, path="", file_sha256=source.sha256)
    del package
    # Current quotes remain reportable when rejected projections are unavailable.
    # Validate every quote before projection checks can take that fallback, and
    # before returning any page whose later items would violate the contract.
    for paragraph in snapshot.paragraphs:
        _enforce_text_limit(
            paragraph.text, limit="accepted_current_chars_per_paragraph",
            allowed_count=MAX_ACCEPTED_CURRENT_CHARS_PER_PARAGRAPH,
        )
    try:
        projections = build_document_projections_v1(snapshot.body_flow)
        projection_error = None
    except ArchiveValidationError:
        # Unattributable markup invalidates explanations, not inventory membership.
        projections = [None] * len(snapshot.paragraphs)
        projection_error = "unattributed_or_invalid_revision_markup"
    root = snapshot.body_flow.paragraphs[0].element.getroottree().getroot() if snapshot.paragraphs else None
    semantic = _semantic_digests(root) if root is not None else {}
    structures, addresses, geometry, table_coverage = _structures(source.payload, side, snapshot, semantic)
    paragraphs, projected_chars = [], 0
    for paragraph, projection in zip(snapshot.paragraphs, projections, strict=True):
        available = projection is not None and projection["projection_status"] == "complete"
        rejected = projection["text"] if available else None
        projected_chars += len(rejected or "")
        _bound("projection_chars_per_document", projected_chars)
        occurrences = []
        properties = []
        for node in paragraph.element.iter():
            if node.tag in _PROPERTY_TAGS:
                properties.append([node.tag, semantic[node]])
            if node.tag in _REVISION_TAGS:
                occurrences.append({
                    "kind": etree.QName(node).localname, "revision_id": node.get(w("id")),
                    "occurrence_index": len(occurrences), "markup_sha256": semantic[node],
                })
                _bound("revision_occurrences_per_paragraph", len(occurrences))
        paragraphs.append({
            "kind": "paragraph", "side": side, "group_id": "",
            "paragraph_ref": _paragraph_ref(snapshot, paragraph),
            "container_kind": paragraph.container_kind,
            "cell_ref": addresses.get(paragraph.paragraph_index), "text": paragraph.text,
            "rejected_text": rejected,
            "projection_status": "complete" if available else "unavailable",
            "projection_reasons": ([projection_error] if projection is None
                                   else projection["unavailable_reasons"]),
            "revision_occurrences": occurrences,
            "revision_markup_sha256": _sha(_json([{k: v for k, v in o.items() if k != "revision_id"}
                                                   for o in occurrences])),
            "revision_ids_sha256": _sha(_json([o["revision_id"] for o in occurrences])),
            "property_markup_sha256": _sha(_json(properties)),
            "paragraph_xml_sha256": semantic[paragraph.element],
        })
    coverage = {
        "indexed_paragraphs": len(paragraphs), "accounted_paragraphs": len(paragraphs),
        "unaccounted_paragraphs": 0, "empty_paragraphs": sum(p["text"] == "" for p in paragraphs),
        "container_coverage_complete": snapshot.container_coverage["coverage_complete"],
        "excluded_subtrees": snapshot.container_coverage["excluded_subtree_count"],
        "excluded_paragraphs": snapshot.container_coverage["excluded_paragraph_count"],
        "excluded_by_kind": snapshot.container_coverage["excluded_by_kind"],
        "excluded_parts": list(snapshot.excluded_parts),
        "unavailable_projections": sum(p["projection_status"] != "complete" for p in paragraphs),
        "table_coverage_complete": table_coverage["complete"],
        "table_coverage_reason": table_coverage["reason"],
    }
    return _Document(snapshot, paragraphs, parts, structures, geometry, coverage)


def _values(paragraph):
    return (paragraph["text"], paragraph["rejected_text"])


def _equalities(left, right):
    return [{"sent_mode": _MODES[i], "incoming_mode": _MODES[j],
             "sent_text_sha256": None if a is None else _sha(a.encode()),
             "incoming_text_sha256": None if b is None else _sha(b.encode()),
             "result": "unavailable" if a is None or b is None else "equal" if a == b else "different"}
            for i, a in enumerate(_values(left)) for j, b in enumerate(_values(right))]


def _explain(left, right):
    equalities = _equalities(left, right)
    observations = []
    for field, label in (("text", "text_changed"),
                         ("revision_markup_sha256", "revision_markup_changed"),
                         ("revision_ids_sha256", "revision_identifiers_changed"),
                         ("property_markup_sha256", "property_markup_changed"),
                         ("paragraph_xml_sha256", "paragraph_xml_changed")):
        if left[field] != right[field]:
            observations.append(label)
    revisions = left["revision_occurrences"] + right["revision_occurrences"]
    unknown = (left["projection_status"] != "complete" or right["projection_status"] != "complete"
               or any(o["kind"].startswith("move") for o in revisions))
    old_pending = any(o["kind"] in {"ins", "del"} for o in left["revision_occurrences"])
    new_pending = any(o["kind"] in {"ins", "del"} for o in right["revision_occurrences"])
    if unknown:
        explanation = "indeterminate"
    elif left["text"] == right["text"]:
        explanation = ("prior_revision_decision_compatible" if old_pending
                       and left["revision_markup_sha256"] != right["revision_markup_sha256"]
                       and not right["revision_occurrences"] else "not_applicable")
    elif new_pending and left["text"] == right["rejected_text"]:
        explanation = "visible_in_current_revisions"
    elif old_pending and (left["rejected_text"] == right["text"] or
                          new_pending and left["rejected_text"] == right["rejected_text"]):
        explanation = "prior_revision_decision_compatible"
    elif old_pending:
        explanation = "indeterminate"
    else:
        explanation = "not_explained_by_available_markup"
    return observations, explanation, equalities


def _bounded_intervals(pairs, left_count, right_count):
    """Adjacent ranks on both sides, O(n log n), with no interval rescans."""
    bounds = [(-1, -1), *pairs, (left_count, right_count)]
    incoming_ranks = {position: rank for rank, position in enumerate(sorted(j for _, j in bounds))}
    for (ai, aj), (bi, bj) in zip(bounds, bounds[1:]):
        if incoming_ranks[bj] == incoming_ranks[aj] + 1:
            yield ai, aj, bi, bj


def _groups(left, right):
    """Unique exact candidates, then bounded intervals; never a fuzzy match."""
    indexes = []
    memberships = 0
    for paragraphs in (left, right):
        index = defaultdict(set)
        for i, paragraph in enumerate(paragraphs):
            for value in set(_values(paragraph)) - {None, ""}:
                if value.strip():
                    index[value].add(i)
                    memberships += 1
                    _bound("candidate_memberships", memberships)
        indexes.append(index)
    # A shared duplicate value marks all its members ambiguous without NxM edges.
    candidates = [defaultdict(set), defaultdict(set)]
    ambiguous = [set(), set()]
    for value in indexes[0].keys() & indexes[1].keys():
        a, b = indexes[0][value], indexes[1][value]
        if len(a) == len(b) == 1:
            i, j = next(iter(a)), next(iter(b))
            candidates[0][i].add(j)
            candidates[1][j].add(i)
        else:
            ambiguous[0].update(a)
            ambiguous[1].update(b)
    pairs = sorted((i, next(iter(js))) for i, js in candidates[0].items()
                   if len(js) == 1 and len(candidates[1][next(iter(js))]) == 1
                   and i not in ambiguous[0] and next(iter(js)) not in ambiguous[1])
    used = [set(), set()]
    result = []

    def group(a, b, basis):
        if used[0].intersection(a) or used[1].intersection(b):
            raise IncomingComparisonError("comparison_invariant_failed", "overlapping paragraph groups")
        used[0].update(a)
        used[1].update(b)
        observations, explanation, equalities = [], "indeterminate", []
        if len(a) == len(b) == 1 and basis in {"unique_projection_equality", "bounded_interval"}:
            observations, explanation, equalities = _explain(left[a[0]], right[b[0]])
        elif basis == "bounded_interval" and (not a or not b):
            observations = ["added_in_interval" if b else "removed_from_interval"]
        else:
            observations = ["ambiguous_or_unmatched_blocks"]
        result.append({"kind": "comparison_group", "group_id": "", "basis": basis,
                       "sent_count": len(a), "incoming_count": len(b),
                       "observations": observations, "explanation": explanation,
                       "equalities": equalities, "_sent": a, "_incoming": b})

    for i, j in pairs:
        group([i], [j], "unique_projection_equality")
    # Only intervals between adjacent anchors in BOTH sequences qualify.
    for ai, aj, bi, bj in _bounded_intervals(pairs, len(left), len(right)):
        a, b = list(range(ai + 1, bi)), list(range(aj + 1, bj))
        if not a and not b:
            continue
        if used[0].intersection(a) or used[1].intersection(b):
            continue
        # Multiple blocks remain one explicit unresolved interval, not guessed pairs.
        group(a, b, "bounded_interval" if len(a) <= 1 and len(b) <= 1 or not a or not b
              else "unresolved_interval")
    # Crossed/moved anchors can leave disconnected tails. Keep both side inventories.
    a = [i for i in range(len(left)) if i not in used[0]]
    b = [j for j in range(len(right)) if j not in used[1]]
    if a or b:
        group(a, b, "unresolved_correspondence")
    order_changed = [j for _, j in pairs] != sorted(j for _, j in pairs)
    result.sort(key=lambda g: (g["_sent"][0] if g["_sent"] else len(left),
                               g["_incoming"][0] if g["_incoming"] else len(right)))
    for number, item in enumerate(result):
        item["group_id"] = f"group_{number}"
        for side, paragraphs in (("_sent", left), ("_incoming", right)):
            for index in item.pop(side):
                paragraphs[index]["group_id"] = item["group_id"]
    if used[0] != set(range(len(left))) or used[1] != set(range(len(right))):
        raise IncomingComparisonError("comparison_invariant_failed", "incomplete paragraph partition")
    return result, order_changed


def build_incoming_comparison(sent: DocumentSnapshot, incoming: DocumentSnapshot) -> IncomingComparison:
    """Compute a complete bounded partition before any page is advertised."""
    if type(sent) is not DocumentSnapshot or type(incoming) is not DocumentSnapshot:
        raise IncomingComparisonError("invalid_comparison_source", "two exact snapshots are required")
    left, right = _document(sent, "sent"), _document(incoming, "incoming")
    groups, order_changed = _groups(left.paragraphs, right.paragraphs)
    part_items = [{"kind": "package_part", "part_name": name,
                   "sent_sha256": left.parts.get(name), "incoming_sha256": right.parts.get(name),
                   "status": "only_incoming" if name not in left.parts else "only_sent"
                   if name not in right.parts else "same_bytes" if left.parts[name] == right.parts[name]
                   else "changed_bytes",
                   "content_interpretation": "not_claimed"}
                  for name in sorted(left.parts.keys() | right.parts.keys())]
    complete = all(d.coverage["container_coverage_complete"] and d.coverage["table_coverage_complete"]
                   and d.coverage["unavailable_projections"] == 0 for d in (left, right))
    structure_complete = all(d.coverage["table_coverage_complete"] for d in (left, right))
    metadata = {
        "schema_version": SCHEMA, "policy": POLICY, "status": "ok",
        "sources": {"sent": {"sha256": sent.sha256, "size_bytes": sent.size_bytes},
                    "incoming": {"sha256": incoming.sha256, "size_bytes": incoming.size_bytes}},
        "coverage": {"sent": left.coverage, "incoming": right.coverage,
                     "partition_complete": True, "supported_scope_complete": complete},
        "observations": {
            "package_bytes_equal": sent.sha256 == incoming.sha256,
            "all_part_bytes_equal": left.parts == right.parts,
            "current_text_sequence_equal": [p["text"] for p in left.paragraphs] ==
                                           [p["text"] for p in right.paragraphs],
            "unique_anchor_order_changed": order_changed,
            "current_table_topology": "unavailable" if not structure_complete else "same"
            if left.geometry_sha256 == right.geometry_sha256 else "different",
            "sent_topology_sha256": left.geometry_sha256, "incoming_topology_sha256": right.geometry_sha256,
            "table_property_markup_sequence_equal": None if not structure_complete else
                [i["property_markup_sha256"] for i in left.structures] ==
                [i["property_markup_sha256"] for i in right.structures],
            "document_equivalence": "not_claimed", "authorship_verified": False,
            "tracking_disabled_verified": False,
        },
        "limitations": _LIMITATIONS, "limits": dict(LIMITS),
    }
    items = [*groups, *left.paragraphs, *right.paragraphs,
             *left.structures, *right.structures, *part_items]
    _bound("report_items", len(items))
    serialized, size = [], len(_json(metadata))
    digest = hashlib.sha256(_json(metadata))
    for item in items:
        encoded = _json(item)
        size += len(encoded)
        _bound("report_bytes", size)
        serialized.append(encoded)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return IncomingComparison(_json(metadata), tuple(serialized), digest.hexdigest())


def _page_inputs(max_items, cursor):
    if type(max_items) is not int or not 1 <= max_items <= LIMITS["max_items"]:
        raise IncomingComparisonError("invalid_comparison_limit", "max_items must be between 1 and 100")
    if cursor is not None and (type(cursor) is not str or not _CURSOR.fullmatch(cursor)):
        raise IncomingComparisonError("invalid_comparison_cursor", "invalid comparison cursor")


def page_incoming_comparison(comparison: IncomingComparison, *, max_items: int = 50,
                             cursor: str | None = None) -> dict:
    _page_inputs(max_items, cursor)
    if type(comparison) is not IncomingComparison:
        raise IncomingComparisonError("invalid_comparison", "immutable comparison required")
    binding = _sha(_json([comparison.digest, max_items, dict(LIMITS), POLICY]))
    offset = 0
    if cursor is not None:
        match = _CURSOR.fullmatch(cursor)
        offset = int(match[1])
        if match[2] != binding or offset >= len(comparison.items):
            raise IncomingComparisonError("stale_comparison_cursor", "cursor does not match this comparison")
    metadata = json.loads(comparison.metadata)
    # Reserve envelope/cursor and bounded server-owned producer metadata too.
    items, chars, size = [], 0, len(comparison.metadata) + 1024
    for encoded in comparison.items[offset:offset + max_items]:
        item = json.loads(encoded)
        item_chars = len(item.get("text", "")) + len(item.get("rejected_text") or "")
        if chars + item_chars > LIMITS["page_text_chars"] or size + len(encoded) > LIMITS["page_bytes"]:
            if not items:
                raise IncomingComparisonError("comparison_page_limit_exceeded", "one complete item exceeds page limits")
            break
        chars += item_chars
        size += len(encoded)
        items.append(item)
    end = offset + len(items)
    result = {**metadata, "comparison_id": comparison.digest, "items": items,
            "pagination": {"total_items": len(comparison.items), "offset": offset,
                           "returned_items": len(items), "has_more": end < len(comparison.items)},
            "next_cursor": f"ic1:{end}:{binding}" if end < len(comparison.items) else None}
    validate_comparison_page(result)
    return result


def compare_incoming_version(sent_path: str, sent_sha256: str, incoming_path: str,
                             incoming_sha256: str, *, max_items: int = 50,
                             cursor: str | None = None) -> dict:
    """Capture explicit paths and verify both hashes on EVERY page request."""
    _page_inputs(max_items, cursor)
    if any(type(value) is not str or not _SHA.fullmatch(value)
           for value in (sent_sha256, incoming_sha256)):
        raise IncomingComparisonError("invalid_comparison_sha256", "two exact lowercase SHA-256 values are required")
    sent = capture_document_snapshot(sent_path, expected_sha256=sent_sha256)
    incoming = capture_document_snapshot(incoming_path, expected_sha256=incoming_sha256)
    return page_incoming_comparison(build_incoming_comparison(sent, incoming),
                                    max_items=max_items, cursor=cursor)


def validate_comparison_page(result: dict) -> None:
    """Critical consistency checks beyond the closed JSON shape, also at MCP exit."""
    def require(condition):
        if not condition:
            raise IncomingComparisonError("comparison_invariant_failed", "inconsistent comparison observations")

    pagination = result["pagination"]
    count, offset, total = pagination["returned_items"], pagination["offset"], pagination["total_items"]
    require(count == len(result["items"]) and 0 <= offset <= offset + count <= total)
    require(pagination["has_more"] == (offset + count < total))
    require((result["next_cursor"] is not None) == pagination["has_more"])
    if result["next_cursor"] is not None:
        cursor = _CURSOR.fullmatch(result["next_cursor"])
        require(cursor is not None and int(cursor[1]) == offset + count)
    coverage = result["coverage"]
    for side in ("sent", "incoming"):
        c = coverage[side]
        require(c["accounted_paragraphs"] == c["indexed_paragraphs"]
                and c["unaccounted_paragraphs"] == 0
                and c["empty_paragraphs"] <= c["indexed_paragraphs"]
                and c["unavailable_projections"] <= c["indexed_paragraphs"])
    require(coverage["supported_scope_complete"] == all(
        coverage[side]["container_coverage_complete"] and coverage[side]["table_coverage_complete"]
        and coverage[side]["unavailable_projections"] == 0 for side in ("sent", "incoming")))
    sources = result["sources"]
    require(result["observations"]["package_bytes_equal"] ==
            (sources["sent"]["sha256"] == sources["incoming"]["sha256"]))
    seen = set()
    for item in result["items"]:
        if item["kind"] == "paragraph":
            ref = item["paragraph_ref"]
            key = (item["side"], ref["paragraph_index"])
            require(key not in seen)
            seen.add(key)
            require(ref["file_sha256"] == sources[item["side"]]["sha256"]
                    and 0 <= ref["paragraph_index"] < coverage[item["side"]]["indexed_paragraphs"]
                    and ref["paragraph_text_sha256"] == _sha(item["text"].encode()))
            require((item["rejected_text"] is not None) == (item["projection_status"] == "complete"))
            if item["cell_ref"] is not None:
                require(item["cell_ref"]["file_sha256"] == ref["file_sha256"])
        elif item["kind"] in {"table", "row", "cell"}:
            require(item["reference"]["file_sha256"] == sources[item["side"]]["sha256"])
        elif item["kind"] == "comparison_group":
            require(item["sent_count"] + item["incoming_count"] > 0)
            if item["basis"] in {"unresolved_interval", "unresolved_correspondence"}:
                require(item["explanation"] == "indeterminate")
            for equality in item["equalities"]:
                a, b = equality["sent_text_sha256"], equality["incoming_text_sha256"]
                require((equality["result"] == "unavailable") == (a is None or b is None))
                if equality["result"] == "equal":
                    require(a == b)
