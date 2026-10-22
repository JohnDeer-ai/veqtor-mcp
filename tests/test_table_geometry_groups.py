# SPDX-License-Identifier: Apache-2.0
"""Independent synthetic outcomes for explicitly selected table geometry groups."""

import copy
import hashlib
import importlib.util
import zipfile
from pathlib import Path

import jsonschema
import pytest
from lxml import etree

from veqtor_docx import ApplyError, drafting
from veqtor_docx._ooxml import DOCUMENT_PART, W_NS, w
from veqtor_docx.revision_decisions import read_document_revisions
from veqtor_mcp._drafting_contract import _APPLIED, OPERATIONS

_spec = importlib.util.spec_from_file_location(
    "table_geometry_fixture",
    Path(__file__).resolve().parents[1] / "scripts/revision_decisions_smoke.py",
)
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
PROFILE = _fixture.PROFILE


def _xml(value):
    return etree.fromstring(f'<w:root xmlns:w="{W_NS}">{value}</w:root>')


def _history(tag, current, previous, identifier, pending):
    if not pending:
        return f"<w:{tag}>{current}</w:{tag}>"
    author = (
        ""
        if tag == "tblGrid"
        else ' w:author="Fictional Geometry Reviewer" w:date="2026-02-03T12:00:00Z"'
    )
    return (
        f'<w:{tag}>{current}<w:{tag}Change w:id="{identifier}"{author}>'
        f"<w:{tag}>{previous}</w:{tag}></w:{tag}Change></w:{tag}>"
    )


def _source(shape, state="pending", *, direction="expand", height=False, defaults=False,
            row_properties=""):
    """Build clean expected states from literal input, not from decision output.

    ``expand`` restores a cell spanning two columns on rejection; ``contract``
    restores a cell spanning one column. Physical cell widths are independent
    of the number of XML grid columns. The row-before case restores one omitted
    column and its width, with the same independent row-height value in both
    snapshots.
    """
    pending = state == "pending"
    previous = state == "previous"
    if shape == "cell_span":
        old_grid, new_grid = (600, 800, 1600), (1400, 1600)
        old_span, new_span = 2, 1
        if direction == "contract":
            old_grid, new_grid = new_grid, old_grid
            old_span, new_span = new_span, old_span
        row_current = (
            '<w:trHeight w:val="480" w:hRule="atLeast"/><w:cantSplit/>'
            if height else ""
        )
        row_current += row_properties
        row_props = f"<w:trPr>{row_current}</w:trPr>"
    else:
        assert shape == "row_before"
        old_grid, new_grid = (600, 1400, 1600), (1400, 1600)
        old_span = new_span = 1
        row_current = (
            '<w:gridBefore w:val="0"/><w:wBefore w:w="0" w:type="dxa"/>'
            '<w:trHeight w:val="480" w:hRule="atLeast"/><w:cantSplit/>'
        )
        row_previous = (
            '<w:gridBefore w:val="1"/><w:wBefore w:w="600" w:type="dxa"/>'
            '<w:trHeight w:val="480" w:hRule="atLeast"/><w:cantSplit/>'
        )
        row_props = _history(
            "trPr", row_previous if previous else row_current, row_previous, 12, pending
        )

    def grid(widths):
        return "".join(f'<w:gridCol w:w="{width}"/>' for width in widths)

    grid_props = _history(
        "tblGrid", grid(old_grid if previous else new_grid), grid(old_grid), 10, pending
    )

    def cell_props(span):
        explicit_span = "" if defaults and span == 1 else f'<w:gridSpan w:val="{span}"/>'
        return (
            '<w:tcW w:w="1400" w:type="dxa"/>'
            + explicit_span
            + '<w:shd w:val="clear" w:fill="D9EAD3"/>'
            '<w:tcMar><w:left w:w="40" w:type="dxa"/></w:tcMar>'
        )

    chosen_first = cell_props(old_span if previous else new_span)
    first_props = (
        _history("tcPr", chosen_first, cell_props(old_span), 11, pending)
        if shape == "cell_span"
        else f"<w:tcPr>{chosen_first}</w:tcPr>"
    )
    # A complete, separately authored history must survive every selected
    # table-property outcome, including its previous properties and timestamp.
    sentinel = (
        '<w:r><w:rPr><w:color w:val="006600"/>'
        '<w:rPrChange w:id="90" w:author="Untouched Reviewer" '
        'w:date="2026-02-02T12:00:00Z"><w:rPr><w:color w:val="660000"/>'
        '</w:rPr></w:rPrChange></w:rPr><w:t>Untouched clause.</w:t></w:r>'
    )
    body = (
        '<w:tbl><w:tblPr><w:tblW w:w="3000" w:type="dxa"/>'
        '<w:tblInd w:w="120" w:type="dxa"/>'
        '<w:tblBorders><w:bottom w:val="single" w:sz="8" w:color="334455"/>'
        '</w:tblBorders><w:tblLayout w:type="fixed"/></w:tblPr>'
        + grid_props
        + "<w:tr>"
        + row_props
        + '<w:tblPrEx><w:tblCellMar><w:top w:w="50" w:type="dxa"/>'
        '<w:bottom w:w="70" w:type="dxa"/></w:tblCellMar></w:tblPrEx>'
        '<w:tc>'
        + first_props
        + '<w:p><w:r><w:t>Fictional item.</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:tcPr><w:tcW w:w="1600" w:type="dxa"/>'
        '<w:tcMar><w:right w:w="60" w:type="dxa"/></w:tcMar></w:tcPr>'
        '<w:p>'
        + sentinel
        + '</w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>Outside table.</w:t></w:r></w:p>'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
        '</w:sectPr>'
    )
    return _xml(f"<w:document><w:body>{body}</w:body></w:document>")[0]


def _make(tmp_path, demo_dir, shape, **kwargs):
    path = tmp_path / "working.docx"
    _fixture.write_source(
        demo_dir / "round-1-outgoing-draft.docx", path, _source(shape, **kwargs)
    )
    return path


def _document(path):
    with zipfile.ZipFile(path) as archive:
        return etree.fromstring(archive.read(DOCUMENT_PART))


def _parts(path):
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def _normalized(node):
    return node.tag, dict(node.attrib), node.text, tuple(_normalized(n) for n in node)


def _operations(path, decision="accept", *, count=2):
    listing = read_document_revisions(str(path))
    selected = [
        row for row in listing["revisions"]
        if row["kind"] in {"tblGridChange", "tcPrChange", "trPrChange"}
    ]
    assert len(selected) == count
    return [
        {
            "kind": "decide_revision",
            "decision": decision,
            "target": {"kind": "single", "revision_ref": row["revision_ref"]},
        }
        for row in selected
    ]


def _preview(path, operations):
    return drafting.preflight_document_edits(
        str(path), hashlib.sha256(path.read_bytes()).hexdigest(), operations, **PROFILE
    )


def _save(path, operations):
    before = path.read_bytes()
    dry = _preview(path, operations)
    assert dry["batch_applicable"], dry
    assert path.read_bytes() == before
    jsonschema.validate(operations, OPERATIONS)
    jsonschema.validate(dry["operations"], _APPLIED)
    result = drafting.apply_document_edits(
        str(path), hashlib.sha256(before).hexdigest(), operations,
        dry["preflight_proof"], **PROFILE,
    )
    assert Path(result["backup_path"]).read_bytes() == before
    assert result["backup_sha256"] == hashlib.sha256(before).hexdigest()
    jsonschema.validate(result["operations"], _APPLIED)
    return dry, result


def _directory(path):
    return {
        str(item.relative_to(path)): item.read_bytes()
        for item in path.rglob("*") if item.is_file()
    }


def _refused(path, operations, code):
    before = _directory(path.parent)
    dry = _preview(path, operations)
    assert dry["batch_applicable"] is False, dry
    assert dry["refusal_code"] == code, dry
    assert _directory(path.parent) == before
    assert not (path.parent / ".veqtor-backups").exists()
    return dry


@pytest.mark.parametrize("shape", ["cell_span", "row_before"])
@pytest.mark.parametrize("decision", ["accept", "reject"])
@pytest.mark.parametrize("reverse", [False, True])
def test_explicit_geometry_group_has_independently_written_outcome(
    tmp_path, demo_dir, shape, decision, reverse
):
    path = _make(tmp_path, demo_dir, shape)
    before_parts = _parts(path)
    operations = _operations(path, decision)
    if reverse:
        operations.reverse()
    dry, result = _save(path, operations)
    expected = _source(shape, "current" if decision == "accept" else "previous")
    assert _normalized(_document(path)) == _normalized(expected)
    # No package part outside the explicitly selected main-story properties
    # may be rewritten by composing their table geometry.
    after_parts = _parts(path)
    assert after_parts.keys() == before_parts.keys()
    assert {
        name: payload for name, payload in after_parts.items() if name != DOCUMENT_PART
    } == {
        name: payload for name, payload in before_parts.items() if name != DOCUMENT_PART
    }
    rows = read_document_revisions(str(path))["revisions"]
    assert len(rows) == 1
    assert rows[0]["kind"] == "rPrChange"
    assert rows[0]["author"] == "Untouched Reviewer"
    for facts in (*dry["operations"], *result["operations"]):
        assert facts["table_validation_scope"] == "explicit_batch_final_state"
        assert facts["fixed_width_validation_scope"] == "explicit_batch_final_state"
        members = facts["table_group_members"]
        assert len(members) == 2
        assert {member["decision"] for member in members} == {decision}
        assert sorted(member["revision_ref"]["element_path"] for member in members) == [
            member["revision_ref"]["element_path"] for member in members
        ]


@pytest.mark.parametrize("decision", ["accept", "reject"])
@pytest.mark.parametrize("defaults", [False, True])
def test_reverse_span_direction_and_implicit_one_have_exact_outcomes(
    tmp_path, demo_dir, decision, defaults
):
    path = _make(tmp_path, demo_dir, "cell_span", direction="contract", defaults=defaults)
    _save(path, _operations(path, decision))
    expected = _source(
        "cell_span", "current" if decision == "accept" else "previous",
        direction="contract", defaults=defaults,
    )
    assert _normalized(_document(path)) == _normalized(expected)


@pytest.mark.parametrize("shape", ["cell_span", "row_before"])
@pytest.mark.parametrize("selected", [0, 1])
def test_incomplete_geometry_rejection_refuses_without_publication(
    tmp_path, demo_dir, shape, selected
):
    path = _make(tmp_path, demo_dir, shape)
    before = _directory(tmp_path)
    operation = _operations(path, "reject")[selected]
    dry = _preview(path, [operation])
    assert not dry["batch_applicable"], dry
    assert dry["refusal_code"] in {
        "revision_dependency_unsupported", "table_grid_incompatible",
        "fixed_table_width_dependency_unsupported",
    }
    assert not (tmp_path / ".veqtor-backups").exists()
    assert _directory(tmp_path) == before


@pytest.mark.parametrize("shape", ["cell_span", "row_before"])
@pytest.mark.parametrize("reverse", [False, True])
def test_incoherent_mixed_geometry_decisions_refuse_final_state(
    tmp_path, demo_dir, shape, reverse
):
    path = _make(tmp_path, demo_dir, shape)
    before = _directory(tmp_path)
    operations = _operations(path, "reject")
    operations[1]["decision"] = "accept"
    if reverse:
        operations.reverse()
    dry = _preview(path, operations)
    assert dry["batch_applicable"] is False, dry
    assert dry["refusal_code"] == "table_grid_incompatible", dry
    assert _directory(tmp_path) == before


@pytest.mark.parametrize("direction", ["expand", "contract"])
def test_independent_height_blocks_unproven_span_rejection_but_not_acceptance(
    tmp_path, demo_dir, direction
):
    path = _make(tmp_path, demo_dir, "cell_span", height=True, direction=direction)
    _refused(path, _operations(path, "reject"), "table_height_dependency_unsupported")
    _save(path, _operations(path, "accept"))
    assert _normalized(_document(path)) == _normalized(
        _source("cell_span", "current", height=True, direction=direction)
    )
    height = next(_document(path).iter(w("trHeight")))
    assert dict(height.attrib) == {w("val"): "480", w("hRule"): "atLeast"}


@pytest.mark.parametrize("direction", ["expand", "contract"])
@pytest.mark.parametrize("properties", [
    "<w:cantSplit/>", "<w:hidden/>", "<w:tblHeader/>", '<w:jc w:val="right"/>',
])
def test_independent_row_properties_refuse_span_rejection_and_survive_acceptance(
    tmp_path, demo_dir, direction, properties
):
    path = _make(
        tmp_path, demo_dir, "cell_span", direction=direction, row_properties=properties
    )
    _refused(
        path, _operations(path, "reject"), "table_row_property_dependency_unsupported"
    )
    _save(path, _operations(path, "accept"))
    assert _normalized(_document(path)) == _normalized(_source(
        "cell_span", "current", direction=direction, row_properties=properties
    ))


def test_unselected_row_history_with_empty_current_properties_stays_independent(
    tmp_path, demo_dir
):
    root = _source("cell_span")
    props = next(root.iter(w("trPr")))
    props.append(_xml(
        '<w:trPrChange w:id="30" w:author="Unselected Row Reviewer" '
        'w:date="2026-02-04T12:00:00Z"><w:trPr><w:cantSplit/>'
        '</w:trPr></w:trPrChange>'
    )[0])
    original_props = _normalized(props)
    path = tmp_path / "working.docx"
    _fixture.write_source(demo_dir / "round-1-outgoing-draft.docx", path, root)
    listing = read_document_revisions(str(path))
    row_ref = next(
        item["revision_ref"] for item in listing["revisions"]
        if item["kind"] == "trPrChange"
    )
    operations = [
        operation for operation in _operations(path, "reject", count=3)
        if operation["target"]["revision_ref"] != row_ref
    ]
    _refused(path, operations, "table_row_property_dependency_unsupported")
    _save(path, [{**operation, "decision": "accept"} for operation in operations])
    assert _normalized(next(_document(path).iter(w("trPr")))) == original_props


@pytest.mark.parametrize("decision", ["accept", "reject"])
@pytest.mark.parametrize("previous", ["", "<w:cantSplit/>", "<w:hidden/>"])
def test_explicit_row_history_owns_independent_flags_in_span_group(
    tmp_path, demo_dir, decision, previous
):
    current = "<w:cantSplit/>"
    root = _source("cell_span", row_properties=current)
    props = next(root.iter(w("trPr")))
    props.append(_xml(
        '<w:trPrChange w:id="30" w:author="Fictional Row Reviewer" '
        'w:date="2026-02-04T12:00:00Z"><w:trPr>'
        + previous + '</w:trPr></w:trPrChange>'
    )[0])
    path = tmp_path / "working.docx"
    _fixture.write_source(demo_dir / "round-1-outgoing-draft.docx", path, root)
    _save(path, _operations(path, decision, count=3))
    assert _normalized(_document(path)) == _normalized(_source(
        "cell_span", "current" if decision == "accept" else "previous",
        row_properties=current if decision == "accept" else previous,
    ))


def test_explicit_row_history_retains_height_on_positive_row_before_rejection(
    tmp_path, demo_dir
):
    path = _make(tmp_path, demo_dir, "row_before")
    _save(path, _operations(path, "reject"))
    row = next(_document(path).iter(w("tr")))
    assert row.find(f"{w('trPr')}/{w('gridBefore')}").get(w("val")) == "1"
    assert dict(row.find(f"{w('trPr')}/{w('trHeight')}").attrib) == {
        w("val"): "480", w("hRule"): "atLeast",
    }


@pytest.mark.parametrize("change", ["decision", "membership", "order"])
def test_group_preflight_proof_binds_exact_selection_and_order(
    tmp_path, demo_dir, change
):
    path = _make(tmp_path, demo_dir, "cell_span")
    operations = _operations(path)
    dry = _preview(path, operations)
    assert dry["batch_applicable"], dry
    changed = copy.deepcopy(operations)
    if change == "decision":
        for operation in changed:
            operation["decision"] = "reject"
    elif change == "membership":
        changed.pop(0)
    else:
        changed.reverse()
    before = _directory(tmp_path)
    with pytest.raises(ApplyError, match="preflight_binding_mismatch"):
        drafting.apply_document_edits(
            str(path), hashlib.sha256(path.read_bytes()).hexdigest(), changed,
            dry["preflight_proof"], **PROFILE,
        )
    assert _directory(tmp_path) == before


def test_group_stale_member_refuses_before_writing(tmp_path, demo_dir):
    path = _make(tmp_path, demo_dir, "row_before")
    operations = _operations(path)
    operations[-1]["target"]["revision_ref"]["owner_sha256"] = "0" * 64
    _refused(path, operations, "revision_reference_mismatch")


def test_group_repeated_apply_cannot_publish_old_plan_twice(tmp_path, demo_dir):
    path = _make(tmp_path, demo_dir, "row_before")
    operations = _operations(path, "reject")
    before = path.read_bytes()
    sha = hashlib.sha256(before).hexdigest()
    dry = _preview(path, operations)
    assert dry["batch_applicable"], dry
    drafting.apply_document_edits(str(path), sha, operations, dry["preflight_proof"], **PROFILE)
    after = _directory(tmp_path)
    with pytest.raises(ApplyError, match="file_sha256_mismatch"):
        drafting.apply_document_edits(
            str(path), sha, operations, dry["preflight_proof"], **PROFILE
        )
    assert _directory(tmp_path) == after
    assert len(list((tmp_path / ".veqtor-backups").glob("*.docx"))) == 1


def test_group_exact_patches_reject_helper_mutation_outside_selection(
    tmp_path, demo_dir, monkeypatch
):
    from veqtor_docx import revision_decisions as decisions

    path = _make(tmp_path, demo_dir, "row_before")
    original = decisions.apply_decision

    def corrupt(root, *args, **kwargs):
        patch, facts = original(root, *args, **kwargs)
        next(n for n in root.iter(w("t")) if n.text == "Untouched clause.").text = "Damaged."
        return patch, facts

    monkeypatch.setattr(decisions, "apply_decision", corrupt)
    _refused(path, _operations(path), "round_trip_failed")


def test_serialization_cannot_drop_unselected_row_height_from_group_acceptance(
    tmp_path, demo_dir, monkeypatch
):
    path = _make(tmp_path, demo_dir, "cell_span", height=True)
    original = drafting._output_archive_bytes

    def corrupt(infos, parts):
        altered = dict(parts)
        root = etree.fromstring(parts[DOCUMENT_PART])
        height = next(root.iter(w("trHeight")))
        height.getparent().remove(height)
        altered[DOCUMENT_PART] = etree.tostring(root)
        return original(infos, altered)

    monkeypatch.setattr(drafting, "_output_archive_bytes", corrupt)
    _refused(path, _operations(path), "round_trip_failed")


def _two_tables(state="pending"):
    root = _source("cell_span", state)
    second = _source("row_before", state).find(f"{w('body')}/{w('tbl')}")
    for node in second.iter():
        if node.get(w("id")) is not None:
            node.set(w("id"), str(int(node.get(w("id"))) + 100))
    root.find(w("body")).insert(1, second)
    return root


@pytest.mark.parametrize("decision", ["accept", "reject"])
@pytest.mark.parametrize("reverse", [False, True])
def test_groups_stay_separate_for_two_tables_with_different_geometry_domains(
    tmp_path, demo_dir, decision, reverse
):
    path = tmp_path / "working.docx"
    _fixture.write_source(demo_dir / "round-1-outgoing-draft.docx", path, _two_tables())
    operations = _operations(path, decision, count=4)
    if reverse:
        operations.reverse()
    _, result = _save(path, operations)
    assert _normalized(_document(path)) == _normalized(
        _two_tables("current" if decision == "accept" else "previous")
    )
    groups = set()
    for facts in result["operations"]:
        refs = facts["table_group_members"]
        assert len(refs) == 2
        paths = tuple(tuple(row["revision_ref"]["element_path"]) for row in refs)
        assert len({path[:2] for path in paths}) == 1
        groups.add(paths)
    assert len(groups) == 2


def test_members_in_different_tables_cannot_complete_each_others_geometry(
    tmp_path, demo_dir
):
    path = tmp_path / "working.docx"
    _fixture.write_source(demo_dir / "round-1-outgoing-draft.docx", path, _two_tables())
    operations = _operations(path, "reject", count=4)
    before = _directory(tmp_path)
    dry = _preview(path, [operations[0], operations[-1]])
    assert dry["batch_applicable"] is False, dry
    assert dry["refusal_code"] in {
        "revision_dependency_unsupported", "table_grid_incompatible",
        "fixed_table_width_dependency_unsupported",
    }
    assert _directory(tmp_path) == before


@pytest.mark.parametrize("shape", ["cell_span", "row_before"])
def test_duplicate_group_member_is_not_an_implicit_second_decision(
    tmp_path, demo_dir, shape
):
    path = _make(tmp_path, demo_dir, shape)
    operations = _operations(path)
    _refused(
        path, operations + [copy.deepcopy(operations[-1])], "revision_operation_overlap"
    )


@pytest.mark.parametrize("placement", ["ancestor", "descendant"])
def test_nested_table_geometry_group_is_outside_supported_scope(
    tmp_path, demo_dir, placement
):
    root = _source("cell_span")
    table = root.find(f"{w('body')}/{w('tbl')}")
    wrapper = _xml(
        '<w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="3000"/></w:tblGrid>'
        '<w:tr><w:tc><w:p><w:r><w:t>Nested context.</w:t></w:r></w:p>'
        '</w:tc></w:tr></w:tbl>'
    )[0]
    if placement == "ancestor":
        body = root.find(w("body"))
        body.replace(table, wrapper)
        wrapper.find(f"{w('tr')}/{w('tc')}").append(table)
        wrapper.find(f"{w('tr')}/{w('tc')}").append(_xml("<w:p/>")[0])
    else:
        cell = table.find(f"{w('tr')}/{w('tc')}")
        cell.append(wrapper)
        cell.append(_xml("<w:p/>")[0])
    path = tmp_path / "working.docx"
    _fixture.write_source(demo_dir / "round-1-outgoing-draft.docx", path, root)
    _refused(path, _operations(path), "revision_dependency_unsupported")


@pytest.mark.parametrize("previous_height", [240, 480])
@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_span_group_may_explicitly_decide_existing_row_height_history(
    tmp_path, demo_dir, previous_height, decision
):
    root = _source("cell_span", height=True)
    row_props = next(root.iter(w("trPr")))
    row_props.append(_xml(
        '<w:trPrChange w:id="30" w:author="Fictional Height Reviewer" '
        'w:date="2026-02-04T12:00:00Z"><w:trPr>'
        f'<w:trHeight w:val="{previous_height}" w:hRule="atLeast"/>'
        '<w:cantSplit/></w:trPr></w:trPrChange>'
    )[0])
    path = tmp_path / "working.docx"
    _fixture.write_source(demo_dir / "round-1-outgoing-draft.docx", path, root)
    _save(path, _operations(path, decision, count=3))
    expected = _source(
        "cell_span", "current" if decision == "accept" else "previous", height=True
    )
    if decision == "reject":
        next(expected.iter(w("trHeight"))).set(w("val"), str(previous_height))
    assert _normalized(_document(path)) == _normalized(expected)


def test_unselected_row_height_history_does_not_authorize_span_rejection(
    tmp_path, demo_dir
):
    root = _source("cell_span", height=True)
    next(root.iter(w("trPr"))).append(_xml(
        '<w:trPrChange w:id="30" w:author="Unselected Height Reviewer">'
        '<w:trPr><w:trHeight w:val="240" w:hRule="atLeast"/>'
        '<w:cantSplit/></w:trPr></w:trPrChange>'
    )[0])
    path = tmp_path / "working.docx"
    _fixture.write_source(demo_dir / "round-1-outgoing-draft.docx", path, root)
    operations = _operations(path, "reject", count=3)
    height_ref = next(
        row["revision_ref"] for row in read_document_revisions(str(path))["revisions"]
        if row["kind"] == "trPrChange"
    )
    selected = [
        operation for operation in operations
        if operation["target"]["revision_ref"] != height_ref
    ]
    assert len(selected) == 2
    _refused(path, selected, "table_height_dependency_unsupported")
