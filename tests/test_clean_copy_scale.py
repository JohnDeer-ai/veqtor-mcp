# SPDX-License-Identifier: Apache-2.0
"""Fictional bounded batches, exact selective equivalence and refusal proofs."""

import copy

import pytest
from lxml import etree
from test_clean_copy import (
    _clean,
    _fixture,
    _normal,
    _package,
    _parts,
    _revision,
    _root,
    _row,
)

from veqtor_docx import ApplyError, clean_copy
from veqtor_docx._ooxml import DOCUMENT_PART, W_NS, w


def _run_mode(payload, monkeypatch, batch):
    monkeypatch.setattr(clean_copy, "_BATCH_MIN_REVISIONS", 1 if batch else 100_000)
    return _clean(payload)


@pytest.mark.parametrize("kind", ["ins", "del", "replacement", *_fixture.CASES])
def test_accept_batch_equals_full_root_selective_result(demo_dir, monkeypatch, kind):
    payload = _package(demo_dir, _fixture.source_document(kind))
    legacy, expected = _run_mode(payload, monkeypatch, False)
    batched, actual = _run_mode(payload, monkeypatch, True)
    assert _normal(actual) == _normal(expected)
    assert _parts(batched.payload) == _parts(legacy.payload)
    assert batched.decisions == legacy.decisions


def _large_document(paragraphs=300, revisions=6, table=False):
    rows = []
    expected = []
    identity = 0
    for index in range(paragraphs):
        changes = []
        for item in range(revisions):
            identity += 1
            kind = "ins" if item % 2 else "del"
            text = f"Clause {index} version {item}."
            changes.append(_revision(kind, text, identity))
            if kind == "ins":
                expected.append(text)
        identity += 1
        props = ('<w:pPr><w:spacing w:after="100"/>'
                 f'<w:pPrChange w:id="{identity}"><w:pPr><w:spacing w:after="80"/>'
                 '</w:pPr></w:pPrChange></w:pPr>')
        paragraph = f'<w:p>{props}{"".join(changes)}</w:p>'
        if table:
            paragraph = ('<w:tr><w:trPr><w:trHeight w:val="600" w:hRule="atLeast"/>'
                         '</w:trPr><w:tc><w:tcPr><w:tcW w:w="4000" w:type="dxa"/>'
                         f'</w:tcPr>{paragraph}</w:tc></w:tr>')
        rows.append(paragraph)
    body = ''.join(rows)
    if table:
        body = ('<w:tbl><w:tblPr><w:tblW w:w="4000" w:type="dxa"/>'
                '<w:tblLayout w:type="fixed"/></w:tblPr>'
                '<w:tblGrid><w:gridCol w:w="4000"/></w:tblGrid>' + body + '</w:tbl>')
    return _root(body), expected


@pytest.mark.parametrize("table", [False, True])
def test_thousands_of_revisions_have_bounded_local_proofs(demo_dir, monkeypatch, table):
    root, expected = _large_document(table=table)
    payload = _package(demo_dir, root)
    calls = []
    original = clean_copy.decisions._require_style_dependencies

    def dependency_proof(parts, paragraph):
        calls.append(paragraph)
        return original(parts, paragraph)

    monkeypatch.setattr(clean_copy.decisions, "_require_style_dependencies", dependency_proof)
    # The old bound rejected before attempting even the first revision.
    assert len(etree.tostring(root)) * 2100 * 8 > clean_copy.MAX_CLEAN_VALIDATION_BYTES
    result, actual = _clean(payload)
    assert result.accepted_revision_count == 2100
    assert len(result.decisions) == 2100
    assert [n.text for n in actual.iter(w("t"))] == expected
    assert len(calls) == 1  # The original walker proved these exact reference semantics.
    assert all(n.get(w("after")) == "100" for n in actual.iter(w("spacing")))
    if table:
        assert len(list(actual.iter(w("tr")))) == 300
        assert {n.get(w("val")) for n in actual.iter(w("trHeight"))} == {"600"}


def test_full_root_cloning_is_constant_for_ordinary_batch(demo_dir, monkeypatch):
    root, _ = _large_document(paragraphs=200, revisions=2)
    payload = _package(demo_dir, root)
    real_copy = copy.deepcopy
    whole_copies = []

    def observed(value, *args, **kwargs):
        if isinstance(value, etree._Element) and value.tag == w("document"):
            whole_copies.append(value)
        return real_copy(value, *args, **kwargs)

    monkeypatch.setattr(clean_copy.copy, "deepcopy", observed)
    _clean(payload)
    assert len(whole_copies) == 3  # Source probe, planned outcome, independent reversal.


def test_large_table_property_group_uses_full_final_geometry(demo_dir, monkeypatch):
    root, expected = _large_document(table=True)
    for index, row in enumerate(root.iter(w("tr"))):
        old_row = etree.SubElement(etree.SubElement(row.find(w("trPr")), w("trPrChange"),
                                                 {w("id"): str(10_000 + index)}), w("trPr"))
        etree.SubElement(old_row, w("trHeight"), {w("val"): "500", w("hRule"): "atLeast"})
        props = next(row.iter(w("tcPr")))
        old_cell = etree.SubElement(etree.SubElement(props, w("tcPrChange"),
                                                  {w("id"): str(20_000 + index)}), w("tcPr"))
        etree.SubElement(old_cell, w("tcW"), {w("w"): "3000", w("type"): "dxa"})
    payload = _package(demo_dir, root)
    calls = []
    original = clean_copy.decisions._require_style_dependencies

    def dependency_proof(parts, paragraph):
        calls.append(paragraph)
        return original(parts, paragraph)

    monkeypatch.setattr(clean_copy.decisions, "_require_style_dependencies", dependency_proof)
    result, actual = _clean(payload)
    assert result.accepted_revision_count == 2700
    assert [n.text for n in actual.iter(w("t"))] == expected
    assert len(calls) == 1
    assert {n.get(w("val")) for n in actual.iter(w("trHeight"))} == {"600"}
    assert {n.get(w("w")) for n in actual.iter(w("tcW"))} == {"4000"}
    clean_copy.decisions.validate_fixed_table_widths(next(actual.iter(w("tbl"))))


@pytest.mark.parametrize("position", ["selected", "before", "after", "spanning"])
def test_field_context_refusal_equals_original_selective_path(demo_dir, monkeypatch, position):
    begin = '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    end = '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    selected = _revision("ins", "Added.")
    body = {
        "selected": f'<w:p>{begin}{selected}{end}</w:p>',
        "before": f'<w:p>{end}</w:p><w:p>{selected}</w:p>',
        "after": f'<w:p>{selected}</w:p><w:p>{begin}</w:p>',
        "spanning": f'<w:p>{begin}</w:p><w:p>{selected}</w:p><w:p>{end}</w:p>',
    }[position]
    payload = _package(demo_dir, _root(body))
    for batch in (False, True):
        with pytest.raises(ApplyError, match="revision_dependency_unsupported"):
            _run_mode(payload, monkeypatch, batch)


def test_unrelated_well_formed_field_is_retained(demo_dir, monkeypatch):
    body = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            '<w:r><w:instrText> PAGE </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            '<w:r><w:t>1</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
            f'<w:p>{_revision("ins", "Added.")}</w:p>')
    payload = _package(demo_dir, _root(body))
    legacy, _ = _run_mode(payload, monkeypatch, False)
    batch, _ = _run_mode(payload, monkeypatch, True)
    assert _parts(legacy.payload) == _parts(batch.payload)


@pytest.mark.parametrize("owner", ["run", "paragraph_mark"])
def test_run_format_after_cross_paragraph_field_uses_original_context(demo_dir, monkeypatch, owner):
    changed = '<w:rPr><w:b/><w:rPrChange w:id="9"><w:rPr><w:i/></w:rPr></w:rPrChange></w:rPr>'
    body = '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r></w:p>'
    if owner == "run":
        body += ('<w:p><w:r><w:fldChar w:fldCharType="end"/></w:r><w:r>'
                 + changed + '<w:t>Outside the field.</w:t></w:r></w:p>')
    else:
        body += ('<w:p><w:pPr>' + changed + '</w:pPr>'
                 '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
                 '<w:r><w:t>Field result.</w:t></w:r></w:p>')
    payload = _package(demo_dir, _root(body))
    if owner == "paragraph_mark":
        # That owner starts while the cross-paragraph field is still open.
        for batch in (False, True):
            with pytest.raises(ApplyError, match="revision_dependency_unsupported"):
                _run_mode(payload, monkeypatch, batch)
    else:
        legacy, _ = _run_mode(payload, monkeypatch, False)
        batch, _ = _run_mode(payload, monkeypatch, True)
        assert _parts(legacy.payload) == _parts(batch.payload)


@pytest.mark.parametrize("marker", ["bookmark", "commentRange", "perm"])
def test_source_range_spanning_paragraphs_is_not_lost_by_detachment(demo_dir, monkeypatch, marker):
    body = (f'<w:p><w:{marker}Start w:id="3"/></w:p>'
            f'<w:p>{_revision("del", "Selected.")}</w:p>'
            f'<w:p><w:{marker}End w:id="3"/></w:p>')
    payload = _package(demo_dir, _root(body))
    for batch in (False, True):
        with pytest.raises(ApplyError, match="clean_copy_unsupported|clean_copy_range_dependency|revision_dependency_unsupported"):
            _run_mode(payload, monkeypatch, batch)


def test_excluded_story_endpoint_cannot_close_source_range(demo_dir, monkeypatch):
    body = ('<w:p><w:bookmarkStart w:id="3"/>'
            '<w:pict xmlns:v="urn:schemas-microsoft-com:vml"><v:shape><v:textbox>'
            '<w:txbxContent><w:p><w:bookmarkEnd w:id="3"/></w:p></w:txbxContent>'
            '</v:textbox></v:shape></w:pict>' + _revision("del", "Selected.") +
            '<w:bookmarkEnd w:id="3"/></w:p>')
    payload = _package(demo_dir, _root(body))
    with pytest.raises(ApplyError, match="clean_copy_range_dependency"):
        _run_mode(payload, monkeypatch, True)


@pytest.mark.parametrize("style", ["direct", "table", "elsewhere_in_table"])
def test_problematic_style_remains_a_refusal(demo_dir, monkeypatch, style):
    if style == "direct":
        root = _root('<w:p><w:pPr><w:pStyle w:val="MissingFictionalStyle"/></w:pPr>' + _revision("ins", "Added.") + '</w:p>')
    else:
        root = _fixture.source_document("tblPrChange")
        table = next(root.iter(w("tbl")))
        if style == "table":
            props = table.find(w("tblPr"))
            props.insert(0, etree.Element(w("tblStyle"), {w("val"): "MissingFictionalStyle"}))
        else:
            paragraph = next(table.iter(w("p")))
            paragraph.insert(0, etree.Element(w("pPr")))
            etree.SubElement(paragraph[0], w("pStyle"), {w("val"): "MissingFictionalStyle"})
    payload = _package(demo_dir, root)
    for batch in (False, True):
        with pytest.raises(ApplyError, match="paragraph_structure_unsupported"):
            _run_mode(payload, monkeypatch, batch)


@pytest.mark.parametrize("kind", ["tcPrChange", "trPrChange", "tblPrExChange"])
@pytest.mark.parametrize("style_in_selected_region", [False, True])
def test_local_table_property_batch_preserves_selective_style_scope(
    demo_dir, monkeypatch, kind, style_in_selected_region
):
    root = _fixture.source_document(kind)
    table = next(root.iter(w("tbl")))
    first_row = next(table.iter(w("tr")))
    if kind == "tcPrChange":
        selected, untouched = list(first_row.iter(w("tc")))
    else:
        selected = first_row
        untouched = copy.deepcopy(first_row)
        for change in list(untouched.iter(w(kind))):
            change.getparent().remove(change)
        table.append(untouched)
    paragraph = next((selected if style_in_selected_region else untouched).iter(w("p")))
    props = etree.Element(w("pPr"))
    etree.SubElement(props, w("pStyle"), {w("val"): "MissingFictionalStyle"})
    paragraph.insert(0, props)
    body = root.find(w("body"))
    for index in range(127):
        body.insert(len(body) - 1, _root(
            '<w:p>' + _revision("ins", f"Fictional clause {index}.", 100 + index) + '</w:p>'
        ).find(w("body"))[0])
    payload = _package(demo_dir, root)
    assert len(clean_copy._nodes(root)) == 128
    if style_in_selected_region:
        for batch in (False, True):
            with pytest.raises(ApplyError, match="paragraph_structure_unsupported"):
                _run_mode(payload, monkeypatch, batch)
    else:
        legacy, _ = _run_mode(payload, monkeypatch, False)
        batched, _ = _run_mode(payload, monkeypatch, True)
        assert batched.accepted_revision_count == 128
        assert _parts(batched.payload) == _parts(legacy.payload)


@pytest.mark.parametrize("table_wide_change", [False, True])
def test_mixed_table_batch_uses_union_of_selected_style_regions(demo_dir, monkeypatch, table_wide_change):
    root = _fixture.source_document("tcPrChange")
    table = next(root.iter(w("tbl")))
    first = next(table.iter(w("tr")))
    second = copy.deepcopy(first)
    for change in list(second.iter(w("tcPrChange"))):
        change.getparent().remove(change)
    row_properties = copy.deepcopy(next(_fixture.source_document("tblPrExChange").iter(w("tblPrEx"))))
    row_properties.find(w("tblPrExChange")).set(w("id"), "300")
    second.insert(0, row_properties)
    table.append(second)
    untouched = list(first.iter(w("tc")))[1]
    paragraph = next(untouched.iter(w("p")))
    props = etree.Element(w("pPr"))
    etree.SubElement(props, w("pStyle"), {w("val"): "MissingFictionalStyle"})
    paragraph.insert(0, props)
    if table_wide_change:
        broad = copy.deepcopy(next(_fixture.source_document("tblPrChange").iter(w("tblPrChange"))))
        broad.set(w("id"), "301")
        table.find(w("tblPr")).append(broad)
    body = root.find(w("body"))
    for index in range(126):
        body.insert(len(body) - 1, _root(
            '<w:p>' + _revision("ins", f"Fictional clause {index}.", 100 + index) + '</w:p>'
        ).find(w("body"))[0])
    payload = _package(demo_dir, root)
    if table_wide_change:
        for batch in (False, True):
            with pytest.raises(ApplyError, match="paragraph_structure_unsupported"):
                _run_mode(payload, monkeypatch, batch)
    else:
        legacy, _ = _run_mode(payload, monkeypatch, False)
        batched, _ = _run_mode(payload, monkeypatch, True)
        assert _parts(batched.payload) == _parts(legacy.payload)


def test_table_style_cannot_be_hidden_by_later_text_deletion(demo_dir, monkeypatch):
    root = _fixture.source_document("tblPrChange")
    paragraph = next(root.iter(w("p")))
    for child in list(paragraph):
        paragraph.remove(child)
    node = etree.fromstring(f'<w:del xmlns:w="{W_NS}" w:id="90"><w:r><w:rPr>'
                            '<w:rStyle w:val="MissingFictionalStyle"/></w:rPr>'
                            '<w:delText>Removed.</w:delText></w:r></w:del>')
    paragraph.append(node)
    payload = _package(demo_dir, root)
    for batch in (False, True):
        with pytest.raises(ApplyError, match="paragraph_structure_unsupported"):
            _run_mode(payload, monkeypatch, batch)


def test_preceding_run_history_keeps_original_table_group_stage_order(demo_dir, monkeypatch):
    root = _fixture.source_document("trPrChange")
    table = next(root.iter(w("tbl")))
    # Move the table history to a later row. The earlier run history is now the
    # first property stage and must clear its own old unresolved reference.
    first_row = next(table.iter(w("tr")))
    change = next(first_row.iter(w("trPrChange")))
    first_row.find(w("trPr")).remove(change)
    second_row = copy.deepcopy(first_row)
    table.append(second_row)
    second_row.find(w("trPr")).append(change)
    run = next(first_row.iter(w("r")))
    props = run.find(w("rPr"))
    if props is None:
        props = etree.Element(w("rPr"))
        run.insert(0, props)
    previous = etree.SubElement(etree.SubElement(props, w("rPrChange"), {w("id"): "90"}), w("rPr"))
    etree.SubElement(previous, w("rStyle"), {w("val"): "MissingFictionalStyle"})
    payload = _package(demo_dir, root)
    legacy, _ = _run_mode(payload, monkeypatch, False)
    batch, _ = _run_mode(payload, monkeypatch, True)
    assert _parts(legacy.payload) == _parts(batch.payload)
    assert legacy.decisions == batch.decisions


@pytest.mark.parametrize("position", ["inside", "outside"])
def test_numbering_and_table_style_ancestry_are_retained(demo_dir, monkeypatch, position):
    num = '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
    paragraph = '<w:p>' + num + _revision("ins", "Selected.") + '</w:p>'
    body = '<w:p>' + num + '<w:r><w:t>Before.</w:t></w:r></w:p>' + paragraph
    if position == "inside":
        body = ('<w:tbl><w:tblPr><w:tblStyle w:val="FictionalTableStyle"/></w:tblPr>'
                '<w:tblGrid><w:gridCol w:w="4000"/></w:tblGrid><w:tr><w:tc>'
                '<w:tcPr><w:tcW w:w="4000" w:type="dxa"/></w:tcPr>' + body + '</w:tc></w:tr></w:tbl>')
    numbering = (f'<w:numbering xmlns:w="{W_NS}"><w:abstractNum w:abstractNumId="1">'
                 '<w:multiLevelType w:val="singleLevel"/><w:lvl w:ilvl="0"><w:start w:val="1"/>'
                 '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
                 '<w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num></w:numbering>').encode()
    parts = {"word/numbering.xml": numbering}
    if position == "inside":
        styles = etree.fromstring(_parts(_package(demo_dir, _root('<w:p/>')))["word/styles.xml"])
        etree.SubElement(styles, w("style"), {w("styleId"): "FictionalTableStyle", w("type"): "table"})
        parts["word/styles.xml"] = etree.tostring(styles)
    payload = _package(demo_dir, _root(body), parts)
    legacy, _ = _run_mode(payload, monkeypatch, False)
    batch, actual = _run_mode(payload, monkeypatch, True)
    assert _parts(legacy.payload) == _parts(batch.payload)
    from veqtor_docx.paragraph_numbering import resolve_body_numbering
    assert [row["label"] for row in resolve_body_numbering(_parts(batch.payload), actual, [0, 1])] == ["1.", "2."]


@pytest.mark.parametrize("corruption", ["outside", "inside", "attribute", "tail", "skip", "reorder"])
def test_batch_replay_collateral_fault_never_returns_candidate(demo_dir, monkeypatch, corruption):
    payload = _package(demo_dir, _root(f'<w:p>{_revision("ins", "Added.")}</w:p>'
                                     '<w:p><w:r><w:t>Untouched.</w:t></w:r></w:p>'))
    apply = clean_copy._apply_batch_patch

    def corrupted(root, path, patch):
        if corruption == "skip":
            return
        apply(root, path, patch)
        if corruption == "outside":
            list(root.iter(w("t")))[-1].text = "Unrequested."
        elif corruption == "inside":
            next(root.iter(w("t"))).text = "Unrequested."
        elif corruption == "attribute":
            root.set("unrequested", "yes")
        elif corruption == "tail":
            root.find(w("body"))[0].tail = "Unrequested."
        else:
            body = root.find(w("body"))
            body.insert(0, body[-1])

    monkeypatch.setattr(clean_copy, "_apply_batch_patch", corrupted)
    with pytest.raises(ApplyError, match="round_trip_failed"):
        _run_mode(payload, monkeypatch, True)


def test_planner_collateral_fault_is_caught_by_whole_reverse(demo_dir, monkeypatch):
    payload = _package(demo_dir, _root(f'<w:p>{_revision("ins", "Added.")}</w:p>'
                                     '<w:p><w:r><w:t>Untouched.</w:t></w:r></w:p>'))
    real = clean_copy._local_context

    def corrupted(root, node, budget):
        result = real(root, node, budget)
        list(root.iter(w("t")))[-1].text = "Unrequested."
        return result

    monkeypatch.setattr(clean_copy, "_local_context", corrupted)
    with pytest.raises(ApplyError, match="round_trip_failed"):
        _run_mode(payload, monkeypatch, True)


def test_dependency_mutation_during_cached_validation_refuses(demo_dir, monkeypatch):
    payload = _package(demo_dir, _root(f'<w:p>{_revision("ins", "First.")}</w:p>'
                                     f'<w:p>{_revision("ins", "Second.", 2)}</w:p>'))
    proof = clean_copy.decisions._require_style_dependencies

    def corrupted(parts, paragraph):
        proof(parts, paragraph)
        parts.parts["word/styles.xml"] += b" "

    monkeypatch.setattr(clean_copy.decisions, "_require_style_dependencies", corrupted)
    with pytest.raises(ApplyError, match="round_trip_failed"):
        _run_mode(payload, monkeypatch, True)


def test_resource_budget_has_an_exact_deterministic_boundary(demo_dir, monkeypatch):
    root, _ = _large_document(paragraphs=40, revisions=3)
    payload = _package(demo_dir, root)
    consume = clean_copy._ValidationParts.consume
    spent = []

    def observed(self, amount):
        consume(self, amount)
        spent.append(self.consumed)

    monkeypatch.setattr(clean_copy._ValidationParts, "consume", observed)
    _run_mode(payload, monkeypatch, True)
    required = max(spent)
    monkeypatch.setattr(clean_copy, "MAX_CLEAN_VALIDATION_BYTES", required)
    _run_mode(payload, monkeypatch, True)
    monkeypatch.setattr(clean_copy, "MAX_CLEAN_VALIDATION_BYTES", required - 1)
    with pytest.raises(ApplyError, match="resource_limit_exceeded"):
        _run_mode(payload, monkeypatch, True)


def test_revision_count_bound_remains_in_force(demo_dir, monkeypatch):
    root, _ = _large_document(paragraphs=40, revisions=3)
    payload = _package(demo_dir, root)
    monkeypatch.setattr(clean_copy, "MAX_CLEAN_DECISIONS", 100)
    with pytest.raises(ApplyError, match="resource_limit_exceeded"):
        _run_mode(payload, monkeypatch, True)


def test_cache_reference_change_requires_new_original_proof(demo_dir, monkeypatch):
    payload = _package(demo_dir, _root(f'<w:p>{_revision("ins", "First.")}</w:p>'
                                     '<w:p><w:pPr><w:pStyle w:val="MissingFictionalStyle"/></w:pPr>'
                                     + _revision("ins", "Second.", 2) + '</w:p>'))
    with pytest.raises(ApplyError, match="paragraph_structure_unsupported"):
        _run_mode(payload, monkeypatch, True)


def test_structural_units_keep_original_bounded_proof_before_batch(demo_dir, monkeypatch):
    table = ('<w:tbl><w:tblPr><w:tblW w:w="4000" w:type="dxa"/><w:tblLayout w:type="fixed"/></w:tblPr>'
             '<w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/></w:tblGrid>'
             + _row("Keep") + _row("Added", "ins") + _row("Removed", "del") + '</w:tbl>')
    payload = _package(demo_dir, _root(table + f'<w:p>{_revision("ins", "Ordinary.", 99)}</w:p>'))
    legacy, _ = _run_mode(payload, monkeypatch, False)
    batch, _ = _run_mode(payload, monkeypatch, True)
    assert _parts(legacy.payload) == _parts(batch.payload)
    assert batch.accepted_revision_count == 7


def test_batch_serializer_cannot_alter_verified_main_part(demo_dir, monkeypatch):
    payload = _package(demo_dir, _fixture.source_document("ins"))
    output = clean_copy._output_archive_bytes

    def corrupted(infos, parts):
        bad = dict(parts)
        bad[DOCUMENT_PART] = bad[DOCUMENT_PART].replace(b"Proposed wording.", b"Unrequested wording.")
        return output(infos, bad)

    monkeypatch.setattr(clean_copy, "_output_archive_bytes", corrupted)
    with pytest.raises(ApplyError, match="round_trip_failed"):
        _run_mode(payload, monkeypatch, True)


@pytest.mark.parametrize("case", ["content_control", "duplicate_style", "pending_style", "nested_history"])
def test_batch_keeps_original_unsupported_and_dependency_refusals(demo_dir, monkeypatch, case):
    root = _fixture.source_document("ins")
    extra = {}
    if case == "content_control":
        body = root.find(w("body"))
        paragraph = body[0]
        body.remove(paragraph)
        etree.SubElement(etree.SubElement(body, w("sdt")), w("sdtContent")).append(paragraph)
    elif case in {"duplicate_style", "pending_style"}:
        styles = etree.fromstring(_parts(_package(demo_dir, root))["word/styles.xml"])
        if case == "duplicate_style":
            styles.append(copy.deepcopy(styles.find(w("style"))))
        else:
            defaults = styles.find(w("docDefaults"))
            if defaults is None:
                defaults = etree.SubElement(styles, w("docDefaults"))
            etree.SubElement(defaults, w("rPrChange"), {w("id"): "90"})
        extra["word/styles.xml"] = etree.tostring(styles)
    else:
        run = next(root.iter(w("ins")))[0]
        props = run.find(w("rPr"))
        if props is None:
            props = etree.Element(w("rPr"))
            run.insert(0, props)
        etree.SubElement(etree.SubElement(props, w("rPrChange"), {w("id"): "90"}), w("rPr"))
    payload = _package(demo_dir, root, extra)
    codes = []
    for batch in (False, True):
        with pytest.raises(ApplyError) as caught:
            _run_mode(payload, monkeypatch, batch)
        codes.append(caught.value.code)
    assert codes[0] == codes[1]


def test_nested_paragraph_mark_format_and_property_histories_reverse_exactly(demo_dir, monkeypatch):
    body = ('<w:p><w:pPr><w:spacing w:after="140"/><w:rPr><w:b/>'
            '<w:rPrChange w:id="8"><w:rPr><w:i/></w:rPr></w:rPrChange></w:rPr>'
            '<w:pPrChange w:id="9"><w:pPr><w:spacing w:after="80"/></w:pPr>'
            '</w:pPrChange></w:pPr>' + _revision("ins", "Added.", 10) + '</w:p>')
    payload = _package(demo_dir, _root(body))
    legacy, _ = _run_mode(payload, monkeypatch, False)
    batch, actual = _run_mode(payload, monkeypatch, True)
    assert _parts(legacy.payload) == _parts(batch.payload)
    assert batch.accepted_revision_count == 3
    assert actual.find(f"{w('body')}/{w('p')}/{w('pPr')}/{w('rPr')}/{w('b')}") is not None


def test_grouped_table_geometry_matches_existing_accept_all(demo_dir, monkeypatch):
    source = _fixture.source_document("tblGridChange")
    table = next(source.iter(w("tbl")))
    for index, cell in enumerate(table.iter(w("tc"))):
        props = cell.find(w("tcPr"))
        if props is None:
            props = etree.Element(w("tcPr"))
            cell.insert(0, props)
        current = props.find(w("tcW"))
        if current is None:
            current = etree.SubElement(props, w("tcW"), {w("type"): "dxa"})
        current.set(w("w"), "2000" if index == 0 else "2800")
        old = etree.SubElement(etree.SubElement(props, w("tcPrChange"), {w("id"): str(100 + index)}), w("tcPr"))
        etree.SubElement(old, w("tcW"), {w("w"): "2400", w("type"): "dxa"})
    payload = _package(demo_dir, source)
    legacy, _ = _run_mode(payload, monkeypatch, False)
    batch, _ = _run_mode(payload, monkeypatch, True)
    assert _parts(legacy.payload) == _parts(batch.payload)


def test_invalid_fixed_width_remains_an_original_group_refusal(demo_dir, monkeypatch):
    source = _fixture.source_document("tblGridChange")
    table = next(source.iter(w("tbl")))
    props = table.find(w("tblPr"))
    layout = props.find(w("tblLayout"))
    if layout is None:
        etree.SubElement(props, w("tblLayout"), {w("type"): "fixed"})
    else:
        layout.set(w("type"), "fixed")
    payload = _package(demo_dir, source)
    codes = []
    for batch in (False, True):
        with pytest.raises(ApplyError) as caught:
            _run_mode(payload, monkeypatch, batch)
        codes.append(caught.value.code)
    assert codes == ["fixed_table_width_dependency_unsupported"] * 2
