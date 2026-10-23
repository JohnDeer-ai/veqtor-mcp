# SPDX-License-Identifier: Apache-2.0
"""Closed, snapshot-addressed bilingual row drafting and structural decisions.

The row mark owns the row only. Every nonempty cell's text is independently
revision marked (ISO/IEC 29500-1 17.13.5.12 and 17.13.5.17). A structural
selection names the row mark and every nested revision in document order;
author, timestamp and numeric ids never establish grouping or authority.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping

from lxml import etree

from ._drafting_paragraphs import (
    _assert_insertion_boundary, _fresh_run, _structure_paragraph, _validate_common,
)
from ._drafting_text import _new_revision
from ._ooxml import canonical_body_flow_v1, is_xml_text_compatible, w
from ._paragraph_edits import _REVISION_TAGS, _require_style_dependencies
from .apply import ApplyError, _paragraph_segments, _reading_text, _RevisionIdAllocator
from .table_map import resolve_table_ref

MAX_ROW_CONTENT_REVISIONS = 128
MAX_CELL_TEXT_CHARS = 20_000


def _fail(detail, code="table_row_structure_unsupported"):
    raise ApplyError(code, detail)


def _reading(paragraph):
    return _reading_text(_paragraph_segments(paragraph))


def _xml(node):
    return etree.tostring(node, method="c14n", with_comments=False, with_tail=False)


def validate_row_operation(op):
    for name in ("row_ref", "template_row_ref", "separator_row_ref"):
        if name in op and (not isinstance(op[name], Mapping) or op[name].get("ref_type") != "row"):
            _fail("supply complete snapshot-bound row references", "invalid_edit")
    texts = op["cell_texts"]
    if (type(texts) is not list or len(texts) != 2
            or any(type(t) is not str or len(t) > MAX_CELL_TEXT_CHARS
                   or not is_xml_text_compatible(t) or any(c in t for c in "\t\r\n") for t in texts)):
        _fail("supply exactly two single-paragraph cell texts", "invalid_edit")
    if "position" in op and op["position"] not in ("before", "after"):
        _fail("position must be before or after", "invalid_edit")
    if "separator" in op:
        separator = op["separator"]
        if (type(separator) is not dict or set(separator) != {"template_row_ref", "position"}
                or separator["position"] not in ("before", "after")
                or not isinstance(separator["template_row_ref"], Mapping)
                or separator["template_row_ref"].get("ref_type") != "row"):
            _fail("select an explicit separator template and before/after position", "invalid_edit")


def resolve_operation(payload, root, op):
    """Resolve all source references before any operation changes XML positions."""
    refs = {name: op[name] for name in ("row_ref", "template_row_ref", "separator_row_ref") if name in op}
    if "separator" in op:
        refs["separator_template"] = op["separator"]["template_row_ref"]
    for ref in refs.values():
        resolve_table_ref(payload, ref)
    table_indexes = {ref["table_index"] for ref in refs.values()}
    if len(table_indexes) != 1:
        _fail("anchor, template and separator must belong to the same table")
    flow = canonical_body_flow_v1(root.find(w("body")))
    tables = [n for n in root.find(w("body")).iter(w("tbl")) if flow.exclusion_kind_for(n) is None]
    table = tables[next(iter(table_indexes))]
    rows = table.findall(w("tr"))
    found = {name: rows[ref["row_index"]] for name, ref in refs.items()}
    return {"table": table, **found}


def _properties(node, allowed, *, repeated=(), selected=()):
    if node is None:
        return
    from .revision_decisions import _property_tree
    if node.attrib or (node.text or "").strip():
        _fail("property-set attributes or text are unsupported")
    names = [child.tag for child in node if child not in selected]
    if any(tag not in {w(n) for n in allowed.split()} for tag in names):
        _fail("unsupported current formatting property")
    if any(names.count(tag) > 1 for tag in set(names) - {w(n) for n in repeated}):
        _fail("duplicate current formatting property")
    for child in node:
        if child not in selected:
            _property_tree(child)


def _one(parent, name):
    found = parent.findall(w(name))
    if len(found) > 1:
        _fail("duplicate table formatting property set")
    return found[0] if found else None


def _table_shape(table):
    from .revision_decisions import _TBLPR, _TRPR, _TCPR, _TBLPREX, validate_fixed_table_widths
    if table.getparent() is None or table.getparent().tag != w("body"):
        _fail("only direct main-body bilingual tables are supported")
    if any(child.tag not in {w("tblPr"), w("tblGrid"), w("tr")} for child in table):
        _fail("table wrappers or range markers are unsupported")
    _properties(_one(table, "tblPr"), _TBLPR)
    grid = _one(table, "tblGrid")
    if (grid is None or len(grid) != 2 or any(c.tag != w("gridCol") or len(c) for c in grid)):
        _fail("the first row workflow requires a two-column rectangular grid")
    rows = table.findall(w("tr"))
    if not rows:
        _fail("table has no rows")
    for row in rows:
        if any(c.tag not in {w("trPr"), w("tblPrEx"), w("tc")} for c in row):
            _fail("wrapped or unknown row content is unsupported")
        props = _one(row, "trPr")
        markers = [c for c in (props if props is not None else ()) if c.tag in {w("ins"), w("del")}]
        _properties(props, _TRPR, selected=markers)
        if props is not None and any(props.find(w(name)) is not None for name in ("gridBefore", "gridAfter", "wBefore", "wAfter")):
            _fail("omitted row grid columns are unsupported")
        _properties(_one(row, "tblPrEx"), _TBLPREX)
        cells = row.findall(w("tc"))
        if len(cells) != 2:
            _fail("each row must have exactly two physical cells")
        for cell in cells:
            cell_props = _one(cell, "tcPr")
            _properties(cell_props, _TCPR)
            if cell_props is not None and any(cell_props.find(w(name)) is not None for name in ("gridSpan", "hMerge", "vMerge")):
                _fail("merged cells are outside the supported row workflow")
            if any(c.tag not in {w("tcPr"), w("p")} for c in cell):
                _fail("nested tables, wrappers and range markers are unsupported")
            if not cell.findall(w("p")):
                _fail("every cell requires its final paragraph")
    geometry = copy.deepcopy(table)
    for row in geometry.findall(w("tr")):
        props = row.find(w("trPr"))
        for marker in list(props) if props is not None else ():
            if marker.tag in {w("ins"), w("del")}:
                props.remove(marker)
    validate_fixed_table_widths(geometry)


def _is_header(row):
    header = row.find(f"{w('trPr')}/{w('tblHeader')}")
    if header is None:
        return False
    if len(header) or set(header.attrib) - {w("val")} or header.get(w("val"), "1") not in {"1", "true", "on", "0", "false", "off"}:
        _fail("unsupported repeating-header boolean value")
    return header.get(w("val"), "1") in {"1", "true", "on"}


def _clean_row(row, parts, *, separator=False):
    if _is_header(row):
        _fail("a repeating header cannot be selected or used as a row template")
    if any(n.tag in _REVISION_TAGS for n in row.iter()):
        _fail("selected and template rows require accepted content and formatting")
    cells = row.findall(w("tc"))
    texts = []
    for cell in cells:
        paragraphs = cell.findall(w("p"))
        if len(paragraphs) != 1:
            _fail("selected and template cells require exactly one paragraph")
        paragraph = paragraphs[0]
        _structure_paragraph(paragraph)
        _require_style_dependencies(parts, paragraph)
        if any(c.tag not in {w("pPr"), w("r")} for c in paragraph):
            _fail("selected and template paragraphs require clean direct runs")
        texts.append(_reading(paragraph))
    _assert_insertion_boundary(cells[0].find(w("p")), "before")
    _assert_insertion_boundary(cells[-1].find(w("p")), "after")
    if separator and any(texts):
        _fail("a selected separator must be an explicitly empty row")
    return texts


def _strip_empty_props(node):
    for tag in ("trPr",):
        for props in list(node.iter(w(tag))):
            if not len(props) and not props.attrib and not (props.text or "").strip():
                props.getparent().remove(props)
    return node


def row_structure_unit(root, nodes):
    if not 1 <= len(nodes) <= MAX_ROW_CONTENT_REVISIONS + 1 or len(set(nodes)) != len(nodes):
        _fail("select one row mark and every nested content revision", "row_unit_unsupported")
    mark, *contents = nodes
    props = mark.getparent()
    row = props.getparent() if props is not None else None
    if (mark.tag not in {w("ins"), w("del")} or len(mark)
            or props is None or props.tag != w("trPr")
            or row is None or row.tag != w("tr")):
        _fail("the selected mark is not a simple row revision", "row_unit_unsupported")
    _table_shape(row.getparent())
    if _is_header(row):
        _fail("header row decisions are outside this structural workflow")
    actual = [n for n in row.iter() if n.tag in _REVISION_TAGS and n is not mark]
    if actual != contents or any(n.tag != mark.tag for n in contents):
        _fail("content references must name every same-direction nested revision in order", "row_unit_unsupported")
    if len(props.findall(mark.tag)) != 1 or any(c.tag in _REVISION_TAGS and c is not mark for c in props):
        _fail("the row has conflicting structural history", "row_unit_unsupported")
    for cell in row.findall(w("tc")):
        paragraphs = cell.findall(w("p"))
        if len(paragraphs) != 1:
            _fail("row structural units require one paragraph per cell")
        paragraph = paragraphs[0]
        selected = [n for n in contents if n in paragraph.iter()]
        _structure_paragraph(paragraph, selected)
        for child in paragraph:
            if child.tag == w("pPr"):
                continue
            if child.tag == w("r") and all(n.tag == w("rPr") or (n.tag == w("t") and not (n.text or "") and not len(n)) for n in child):
                continue  # Native Word can leave an empty run in an empty cell.
            if child not in contents or child.tag != mark.tag:
                _fail("the row contains accepted or unselected cell content", "row_unit_unsupported")
    # A row's structural scope never includes a range crossing either edge.
    first, last = row.findall(w("tc"))
    _assert_insertion_boundary(first.find(w("p")), "before")
    _assert_insertion_boundary(last.find(w("p")), "after")
    return {"mark": mark, "contents": contents, "row": row,
            "kind": "row_insertion" if mark.tag == w("ins") else "row_deletion"}


def row_structure_candidates(mark):
    try:
        row = mark.getparent().getparent()
        nodes = [mark, *[n for n in row.iter() if n.tag in _REVISION_TAGS and n is not mark]]
        row_structure_unit(row.getroottree().getroot(), nodes)
        return nodes
    except (ApplyError, AttributeError, TypeError):
        return []


def row_structure_outcome(unit, decision):
    keep = (unit["kind"] == "row_insertion") == (decision == "accept")
    if not keep:
        return []
    row = copy.deepcopy(unit["row"])
    copied = dict(zip(unit["row"].iter(), row.iter(), strict=True))
    for source in unit["contents"]:
        node = copied[source]
        parent = node.getparent()
        if parent.tag == w("p"):
            index = parent.index(node)
            for offset, run in enumerate(list(node)):
                for atom in run.iter(w("delText")):
                    atom.tag = w("t")
                parent.insert(index + offset, run)
        parent.remove(node)
    marker = copied[unit["mark"]]
    marker.getparent().remove(marker)
    # Retain even an empty row-property set: it may have existed before the
    # mark. Standard row markup cannot distinguish that case from a container
    # materialized only to hold the mark; empty trPr has no effective property.
    return [row]


def _numbering(parts, root, rows):
    from .paragraph_numbering import resolve_body_numbering
    all_p = list(root.find(w("body")).iter(w("p")))
    paragraphs = [p for row in rows for p in row.iter(w("p"))]
    return resolve_body_numbering(parts, root, [all_p.index(p) for p in paragraphs])


def _tracked_row(template, texts, tag, author, date, allocator):
    row = etree.Element(w("tr"))
    for name in ("tblPrEx", "trPr"):
        old = template.find(w(name))
        if old is not None:
            row.append(copy.deepcopy(old))
    props = row.find(w("trPr"))
    if props is None:
        props = etree.Element(w("trPr"))
        row.insert(1 if row.find(w("tblPrEx")) is not None else 0, props)
    mark_id = allocator.reserve(1)[0]
    props.append(_new_revision(tag, mark_id, author, date))
    content_ids = []
    for source_cell, text in zip(template.findall(w("tc")), texts, strict=True):
        cell = etree.SubElement(row, w("tc"))
        cell_props = source_cell.find(w("tcPr"))
        if cell_props is not None:
            cell.append(copy.deepcopy(cell_props))
        source_p = source_cell.find(w("p"))
        paragraph = etree.SubElement(cell, w("p"))
        old_props = source_p.find(w("pPr"))
        if old_props is not None:
            paragraph.append(copy.deepcopy(old_props))
        if text:
            run = _fresh_run(source_p, text)
            ident = allocator.reserve(1)[0]
            wrapper = _new_revision(tag, ident, author, date)
            wrapper.append(run)
            paragraph.append(wrapper)
            content_ids.append(ident)
    return row, {"row_mark_revision_id": mark_id, "content_revision_ids": content_ids,
                 "cell_texts": list(texts)}


def draft_row_operation(root, context, op, author, allocator, parts, *, timestamp=None):
    """Build and independently invert a detached complete-table candidate."""
    table = context["table"]
    _table_shape(table)
    row = context["row_ref"]
    date = _validate_common(row.findall(w("tc"))[0].find(w("p")), "", author, timestamp)
    _clean_row(row, parts)
    from .revision_decisions import _main_context
    _main_context(root, table)
    candidate_root = copy.deepcopy(root)
    mapped = dict(zip(root.iter(), candidate_root.iter(), strict=True))
    candidate_table = mapped[table]
    candidate_allocator = _RevisionIdAllocator(allocator.next_value)
    groups = []
    selected_rows = []
    kind = op["kind"]
    if kind == "insert_table_row":
        template = context["template_row_ref"]
        _clean_row(template, parts)
        separator_template = context.get("separator_template")
        if separator_template is not None:
            _clean_row(separator_template, parts, separator=True)
            if any(f["num_id"] is not None for f in _numbering(parts, root, [separator_template])):
                _fail("a separator template must be unnumbered")
        added, facts = _tracked_row(template, op["cell_texts"], "ins", author, date, candidate_allocator)
        selected_rows.append(added)
        groups.append({**facts, "role": "clause"})
        sequence = [added]
        if separator_template is not None:
            separator, sfacts = _tracked_row(separator_template, ["", ""], "ins", author, date, candidate_allocator)
            selected_rows.append(separator)
            groups.append({**sfacts, "role": "separator"})
            sequence.insert(0 if op["separator"]["position"] == "before" else 1, separator)
        index = candidate_table.index(mapped[row]) + (op["position"] == "after")
        for offset, added_row in enumerate(sequence):
            candidate_table.insert(index + offset, added_row)
    else:
        if _clean_row(row, parts) != op["cell_texts"]:
            _fail("supply the complete current text of both cells", "table_row_text_mismatch")
        chosen = [row]
        separator = context.get("separator_row_ref")
        if separator is not None:
            if separator not in {row.getprevious(), row.getnext()}:
                _fail("the explicitly selected separator must be a different adjacent row")
            _clean_row(separator, parts, separator=True)
            if any(f["num_id"] is not None for f in _numbering(parts, root, [separator])):
                _fail("a selected separator must be unnumbered")
            chosen.append(separator)
        if len(chosen) >= len(table.findall(w("tr"))):
            _fail("deleting the only or every remaining row is unsupported")
        for original_row in chosen:
            target = mapped[original_row]
            props = target.find(w("trPr"))
            if props is None:
                props = etree.Element(w("trPr"))
                target.insert(1 if target.find(w("tblPrEx")) is not None else 0, props)
            mark_id = candidate_allocator.reserve(1)[0]
            props.append(_new_revision("del", mark_id, author, date))
            contents = []
            for paragraph in target.iter(w("p")):
                runs = [c for c in paragraph if c.tag == w("r")]
                if not _reading(paragraph):
                    continue
                ident = candidate_allocator.reserve(1)[0]
                deletion = _new_revision("del", ident, author, date)
                for run in runs:
                    for atom in run.iter(w("t")):
                        atom.tag = w("delText")
                    deletion.append(run)
                paragraph.append(deletion)
                contents.append(ident)
            selected_rows.append(target)
            groups.append({"row_mark_revision_id": mark_id, "content_revision_ids": contents,
                           "cell_texts": op["cell_texts"] if original_row is row else ["", ""],
                           "role": "clause" if original_row is row else "separator"})
    units = [row_structure_unit(candidate_root, row_structure_candidates(r.find(f"{w('trPr')}/{w('ins') if kind == 'insert_table_row' else w('del')}"))) for r in selected_rows]
    projections = {}
    for action in ("accept", "reject"):
        projected = copy.deepcopy(candidate_root)
        mapping = dict(zip(candidate_root.iter(), projected.iter(), strict=True))
        for unit in units:
            old = mapping[unit["row"]]
            parent, index = old.getparent(), old.getparent().index(old)
            parent.remove(old)
            for offset, clean in enumerate(row_structure_outcome(unit, action)):
                parent.insert(index + offset, clean)
        projections[action] = projected
    rejected_table = list(projections["reject"].find(w("body")))[root.find(w("body")).index(table)]
    if _xml(_strip_empty_props(copy.deepcopy(rejected_table))) != _xml(_strip_empty_props(copy.deepcopy(table))):
        _fail("row proposal did not invert to its exact original table", "round_trip_failed")
    from .table_row_numbering import numbering_effects_for_rows
    longer = "accept" if kind == "insert_table_row" else "reject"
    shorter = "reject" if longer == "accept" else "accept"
    # In the longer clean projection row order is unchanged from the proposal.
    indices = [list(candidate_root.find(w("body")).iter(w("p"))).index(p)
               for selected_row in selected_rows for p in selected_row.iter(w("p"))]
    numbering = numbering_effects_for_rows(parts, projections[longer], projections[shorter], sorted(indices))
    facts = {"operation": kind, "cell_texts": list(op["cell_texts"]), "row_count": len(selected_rows),
             "revision_ids": [ident for group in groups for ident in [group["row_mark_revision_id"], *group["content_revision_ids"]]],
             "separator_included": len(selected_rows) == 2, "row_revision_groups": groups,
             "row_numbering": {"accepted": numbering["before" if longer == "accept" else "after"],
                               "rejected": numbering["before" if longer == "reject" else "after"]}}
    if "position" in op:
        facts["position"] = op["position"]
    table.getparent().replace(table, candidate_table)
    allocator.next_value = candidate_allocator.next_value
    return candidate_table, facts


def verify_row_proposal(original_table, candidate_table, op, applied, author, timestamp, parts):
    """Independently verify the published helper result against source and request.

    This is deliberately called by the drafting orchestrator after the mutator
    returns. A monkeypatched or faulty helper cannot hide collateral changes by
    presenting its own postimage as the expected document.
    """
    from ._drafting_text import annotation_date
    _table_shape(candidate_table)
    tag = w("ins") if op["kind"] == "insert_table_row" else w("del")
    date = annotation_date(timestamp)
    expected_header = {w("author"): author, **({w("date"): date} if date is not None else {})}
    if "position" in op and applied.get("position") != op["position"]:
        _fail("row helper position fact differs from the requested operation", "round_trip_failed")
    groups = applied.get("row_revision_groups", [])
    separator = "separator" in op or "separator_row_ref" in op
    if (applied.get("operation") != op["kind"] or applied.get("cell_texts") != op["cell_texts"]
            or applied.get("row_count") != 1 + separator or applied.get("separator_included") is not separator
            or len(groups) != 1 + separator or [g.get("role") for g in groups] != ["clause", *(["separator"] if separator else [])]):
        _fail("row helper facts differ from the explicit requested row package", "round_trip_failed")
    originals = original_table.findall(w("tr"))
    source_rows = [originals[op["row_ref"]["row_index"]]]
    if "separator_row_ref" in op:
        source_rows.append(originals[op["separator_row_ref"]["row_index"]])
    target_rows = []
    for group_index, group in enumerate(groups):
        expected_texts = op["cell_texts"] if group_index == 0 else ["", ""]
        if group.get("cell_texts") != expected_texts:
            _fail("row helper changed the requested cell texts", "round_trip_failed")
        matches = [row for row in candidate_table.findall(w("tr"))
                   if any(n.get(w("id")) == group.get("row_mark_revision_id")
                          for n in row.findall(f"{w('trPr')}/{tag}"))]
        if len(matches) != 1:
            _fail("row helper did not produce each distinct selected row mark", "round_trip_failed")
        row = matches[0]
        mark = next(n for n in row.findall(f"{w('trPr')}/{tag}") if n.get(w("id")) == group["row_mark_revision_id"])
        nodes = [mark, *[n for n in row.iter() if n.tag in _REVISION_TAGS and n is not mark]]
        unit = row_structure_unit(candidate_table.getroottree().getroot(), nodes)
        if [n.get(w("id")) for n in nodes[1:]] != group.get("content_revision_ids"):
            _fail("row helper changed the complete ordered content identities", "round_trip_failed")
        for node in nodes:
            if node.tag != tag or dict(node.attrib) != {w("id"): node.get(w("id")), **expected_header}:
                _fail("row helper changed the chosen revision profile", "round_trip_failed")
        retained = row_structure_outcome(unit, "accept" if tag == w("ins") else "reject")[0]
        if [_reading(p) for p in retained.iter(w("p"))] != expected_texts:
            _fail("row helper changed a requested cell proposal", "round_trip_failed")
        if tag == w("del"):
            expected_row = source_rows[group_index]
        else:
            reference = op["template_row_ref"] if group_index == 0 else op["separator"]["template_row_ref"]
            template = originals[reference["row_index"]]
            expected_row = etree.Element(w("tr"), nsmap=retained.nsmap)
            for name in ("tblPrEx", "trPr"):
                props = template.find(w(name))
                if props is not None:
                    expected_row.append(copy.deepcopy(props))
            for template_cell, text in zip(template.findall(w("tc")), expected_texts, strict=True):
                cell = etree.SubElement(expected_row, w("tc"))
                props = template_cell.find(w("tcPr"))
                if props is not None:
                    cell.append(copy.deepcopy(props))
                template_p = template_cell.find(w("p"))
                paragraph = etree.SubElement(cell, w("p"))
                props = template_p.find(w("pPr"))
                if props is not None:
                    paragraph.append(copy.deepcopy(props))
                if text:
                    paragraph.append(_fresh_run(template_p, text))
        if _xml(_strip_empty_props(copy.deepcopy(retained))) != _xml(_strip_empty_props(copy.deepcopy(expected_row))):
            _fail("row helper changed text, identities or allowed source formatting", "round_trip_failed")
        target_rows.append(row)
    if tag == w("ins"):
        sequence = list(target_rows)
        if separator and op["separator"]["position"] == "before":
            sequence.reverse()
        expected_position = original_table.index(source_rows[0]) + (op["position"] == "after")
        if [candidate_table.index(row) for row in sequence] != list(range(expected_position, expected_position + len(sequence))):
            _fail("inserted rows differ from the requested order or position", "round_trip_failed")
    else:
        if [candidate_table.index(row) for row in target_rows] != [original_table.index(row) for row in source_rows]:
            _fail("deleted rows differ from the requested explicit positions", "round_trip_failed")
    clone = copy.deepcopy(candidate_table)
    mapping = dict(zip(candidate_table.iter(), clone.iter(), strict=True))
    for row in target_rows:
        copied = mapping[row]
        index = clone.index(copied)
        mark = next(n for n in row.findall(f"{w('trPr')}/{tag}") if n.get(w("id")) in {g["row_mark_revision_id"] for g in groups})
        nodes = [mark, *[n for n in row.iter() if n.tag in _REVISION_TAGS and n is not mark]]
        clean = row_structure_outcome(row_structure_unit(candidate_table.getroottree().getroot(), nodes), "reject")
        clone.remove(copied)
        for offset, output in enumerate(clean):
            clone.insert(index + offset, output)
    if _xml(_strip_empty_props(clone)) != _xml(_strip_empty_props(copy.deepcopy(original_table))):
        _fail("row helper changed unselected table content or formatting", "round_trip_failed")
    expected_ids = [ident for g in groups for ident in [g["row_mark_revision_id"], *g["content_revision_ids"]]]
    if applied.get("revision_ids") != expected_ids or len(set(expected_ids)) != len(expected_ids):
        _fail("row helper returned inconsistent revision identities", "round_trip_failed")

    # Numbering is part of the approved preview, not merely descriptive helper
    # metadata. Recompute it from the independently checked complete postimage.
    from .table_row_numbering import numbering_effects_for_rows
    root = candidate_table.getroottree().getroot()
    projections = {}
    for action in ("accept", "reject"):
        projected = copy.deepcopy(root)
        mapped = dict(zip(root.iter(), projected.iter(), strict=True))
        for row in target_rows:
            marker = next(n for n in row.findall(f"{w('trPr')}/{tag}") if n.get(w("id")) in {g["row_mark_revision_id"] for g in groups})
            selected = [marker, *[n for n in row.iter() if n.tag in _REVISION_TAGS and n is not marker]]
            clean = row_structure_outcome(row_structure_unit(root, selected), action)
            old = mapped[row]
            parent, index = old.getparent(), old.getparent().index(old)
            parent.remove(old)
            for offset, output in enumerate(clean):
                parent.insert(index + offset, output)
        projections[action] = projected
    longer = "accept" if tag == w("ins") else "reject"
    shorter = "reject" if longer == "accept" else "accept"
    source_p = list(root.find(w("body")).iter(w("p")))
    indices = sorted(source_p.index(p) for row in target_rows for p in row.iter(w("p")))
    proven = numbering_effects_for_rows(parts, projections[longer], projections[shorter], indices)
    expected_numbering = {"accepted": proven["before" if longer == "accept" else "after"],
                          "rejected": proven["before" if longer == "reject" else "after"]}
    if applied.get("row_numbering") != expected_numbering:
        _fail("row helper numbering facts differ from the independently proved outcomes", "round_trip_failed")
