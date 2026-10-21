# SPDX-License-Identifier: Apache-2.0
"""Synthetic F2/I01-I11 evidence. No private or document-derived fixtures."""
from __future__ import annotations

import hashlib
import io
import zipfile
from collections import Counter
from html import escape
from types import MappingProxyType

import jsonschema
import pytest
from mcp.client import Client

from veqtor_docx import incoming_comparison as compare
from veqtor_docx._ooxml import ResourceLimitError, W_NS
from veqtor_docx._projection import build_document_projections_v1, build_paragraph_projection_v1
from veqtor_docx.inspect import _load_snapshot_from_payload
from veqtor_docx.round_snapshots import DocumentSnapshot, SnapshotError
from veqtor_mcp._incoming_contract import RESULT


def _p(text="", properties=""):
    return f"<w:p>{properties}<w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def _change(old, new):
    return ('<w:p><w:del w:id="1" w:author="Synthetic Reviewer" w:date="2026-01-01T00:00:00Z">'
            f'<w:r><w:delText>{escape(old)}</w:delText></w:r></w:del>'
            '<w:ins w:id="2" w:author="Synthetic Reviewer" w:date="2026-01-01T00:00:00Z">'
            f'<w:r><w:t>{escape(new)}</w:t></w:r></w:ins></w:p>')


def _table(rows):
    return ('<w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/>'
            '</w:tblGrid>' + ''.join('<w:tr>' + ''.join('<w:tc>' + content + '</w:tc>'
                                                     for content in row) + '</w:tr>' for row in rows)
            + '</w:tbl>')


def _snapshot(*blocks, extras=None, comment=b""):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.comment = comment
        archive.writestr(zipfile.ZipInfo("[Content_Types].xml", (2026, 1, 1, 0, 0, 0)),
                         '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                         '<Default Extension="xml" ContentType="application/xml"/>'
                         '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr(zipfile.ZipInfo("word/document.xml", (2026, 1, 1, 0, 0, 0)),
                         f'<w:document xmlns:w="{W_NS}" xmlns:x="urn:synthetic:unknown"><w:body>'
                         + ''.join(blocks) + '<w:sectPr/></w:body></w:document>')
        for name, content in sorted((extras or {}).items()):
            archive.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), content)
    payload = output.getvalue()
    return DocumentSnapshot(payload, hashlib.sha256(payload).hexdigest(), len(payload))


def _all(left, right, max_items=100):
    computation = compare.build_incoming_comparison(left, right)
    items, pages, cursor = [], [], None
    while True:
        page = compare.page_incoming_comparison(computation, max_items=max_items, cursor=cursor)
        jsonschema.Draft202012Validator(RESULT).validate({
            **page, "producer": {"name": "veqtor-mcp", "version": "synthetic", "build": "test"},
            "record_id": None, "record_status": "disabled",
        })
        items.extend(page["items"])
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    for side, source in (("sent", left), ("incoming", right)):
        paragraphs = [i for i in items if i["kind"] == "paragraph" and i["side"] == side]
        actual = _load_snapshot_from_payload(source.payload, path="")
        assert sorted(i["paragraph_ref"]["paragraph_index"] for i in paragraphs) == list(range(len(actual.paragraphs)))
        assert all(i["paragraph_ref"]["file_sha256"] == source.sha256 for i in paragraphs)
        groups = Counter(i["group_id"] for i in paragraphs)
        for group in (i for i in items if i["kind"] == "comparison_group"):
            assert groups[group["group_id"]] == group[side + "_count"]
    return pages[0], items, pages


def _groups(items):
    return [item for item in items if item["kind"] == "comparison_group"]


def test_untracked_replacement_is_explicit_and_not_authorship_evidence():
    page, items, _ = _all(_snapshot(_p("45 days")), _snapshot(_p("30 days")))
    group = _groups(items)[0]
    assert group["explanation"] == "not_explained_by_available_markup"
    assert group["basis"] == "bounded_interval"
    assert {i["text"] for i in items if i["kind"] == "paragraph"} == {"45 days", "30 days"}
    assert page["observations"]["authorship_verified"] is False
    assert page["observations"]["tracking_disabled_verified"] is False


def test_tracked_replacement_has_concrete_projection_equality_and_exact_occurrences():
    _, items, _ = _all(_snapshot(_p("45 days")), _snapshot(_change("45 days", "30 days")))
    group = _groups(items)[0]
    assert group["explanation"] == "visible_in_current_revisions"
    evidence = group["equalities"][1]
    assert evidence["result"] == "equal"
    assert evidence["sent_mode"] == "accepted_current_v1"
    assert evidence["incoming_mode"] == "pending_text_revisions_rejected_v1"
    paragraph = next(i for i in items if i["kind"] == "paragraph" and i["side"] == "incoming")
    assert [o["revision_id"] for o in paragraph["revision_occurrences"]] == ["1", "2"]


@pytest.mark.parametrize("decision", ["old", "new"])
def test_prior_revision_decisions_are_compatible_not_hidden_edits(decision):
    _, items, _ = _all(_snapshot(_change("old", "new")), _snapshot(_p(decision)))
    assert _groups(items)[0]["explanation"] == "prior_revision_decision_compatible"
    assert "revision_markup_changed" in _groups(items)[0]["observations"]


def test_mixed_prior_decisions_not_explained_by_two_projections_remain_indeterminate():
    pending = _change("A", "B").replace('</w:p>', '<w:ins w:id="3"><w:r><w:t>C</w:t></w:r></w:ins></w:p>')
    _, items, _ = _all(_snapshot(pending), _snapshot(_p("AC")))
    assert _groups(items)[0]["explanation"] == "indeterminate"


@pytest.mark.parametrize("position", [0, 1, 2])
def test_completely_rewritten_first_middle_last_blocks_are_never_lost(position):
    old = ["First", "Middle", "Last"]
    new = old.copy()
    new[position] = "Entirely different wording"
    _, items, _ = _all(_snapshot(*map(_p, old)), _snapshot(*map(_p, new)), max_items=1)
    changed = [g for g in _groups(items) if "text_changed" in g["observations"]]
    assert len(changed) == 1
    assert changed[0]["explanation"] == "not_explained_by_available_markup"


def test_repeated_and_empty_blocks_do_not_get_arbitrary_pairs():
    _, items, _ = _all(_snapshot(_p("start"), _p("repeat"), _p("repeat"), *[_p()] * 20, _p("end")),
                       _snapshot(_p("start"), _p("changed"), _p("repeat"), *[_p()] * 21, _p("end")), 3)
    group = next(g for g in _groups(items) if g["sent_count"] > 1)
    assert group["basis"] == "unresolved_interval"
    assert group["explanation"] == "indeterminate"
    assert (group["sent_count"], group["incoming_count"]) == (22, 23)


@pytest.mark.parametrize("left,right", [(["A", "B"], ["AB"]), (["AB"], ["A", "B"])])
def test_split_join_preserve_explicit_unresolved_members(left, right):
    _, items, _ = _all(_snapshot(_p("start"), *map(_p, left), _p("end")),
                       _snapshot(_p("start"), *map(_p, right), _p("end")))
    group = next(g for g in _groups(items) if g["basis"] == "unresolved_interval")
    assert (group["sent_count"], group["incoming_count"]) == (len(left), len(right))
    assert group["explanation"] == "indeterminate"


def test_reordered_unique_blocks_report_order_not_a_verified_move():
    page, _, _ = _all(_snapshot(_p("A"), _p("B"), _p("C")),
                      _snapshot(_p("C"), _p("A"), _p("B")))
    assert page["observations"]["unique_anchor_order_changed"] is True


def test_table_geometry_changes_even_when_full_current_text_sequence_is_identical():
    left = _snapshot(_table([[_p("A"), _p("B")], [_p("C"), _p("D")]]))
    right = _snapshot(_table([[_p("A") + _p("B"), _p("C") + _p("D")]]))
    page, items, _ = _all(left, right)
    assert page["observations"]["current_text_sequence_equal"] is True
    assert page["observations"]["current_table_topology"] == "different"
    assert len([i for i in items if i["kind"] == "row"]) == 3
    assert all(i["cell_ref"] for i in items if i["kind"] == "paragraph")


def test_add_remove_rows_include_empty_cells_and_matching_text():
    _, items, _ = _all(_snapshot(_table([[_p("A"), _p("B")]])),
                       _snapshot(_table([[_p("A"), _p("B")], [_p(), _p("New")]])))
    assert any(g["observations"] == ["added_in_interval"] for g in _groups(items))


def test_zip_repack_is_not_a_content_or_markup_change():
    page, items, _ = _all(_snapshot(_p("Same")), _snapshot(_p("Same"), comment=b"repacked"))
    assert not page["observations"]["package_bytes_equal"]
    assert page["observations"]["all_part_bytes_equal"]
    assert not _groups(items)[0]["observations"]


def test_revision_id_change_is_separate_from_revision_markup_and_text():
    page, items, _ = _all(_snapshot(_change("old", "new")),
                          _snapshot(_change("old", "new").replace('w:id="1"', 'w:id="101"')))
    assert page["observations"]["current_text_sequence_equal"]
    assert _groups(items)[0]["observations"] == ["revision_identifiers_changed"]


@pytest.mark.parametrize("before,after", [
    ('<w:pPr><w:jc w:val="left"/></w:pPr>', '<w:pPr><w:jc w:val="right"/></w:pPr>'),
    ('<w:pPr><w:rPr><w:b/></w:rPr></w:pPr>', '<w:pPr><w:rPr><w:i/></w:rPr></w:pPr>'),
])
def test_equal_text_does_not_hide_property_markup_changes(before, after):
    page, items, _ = _all(_snapshot(_p("Same", before)), _snapshot(_p("Same", after)))
    assert page["observations"]["current_text_sequence_equal"]
    assert "property_markup_changed" in _groups(items)[0]["observations"]
    assert page["observations"]["document_equivalence"] == "not_claimed"


def test_unsupported_parts_and_styles_are_byte_observations_not_semantic_equality():
    old = {"word/header1.xml": f'<w:hdr xmlns:w="{W_NS}">{_p("Old")}</w:hdr>',
           "word/styles.xml": f'<w:styles xmlns:w="{W_NS}"/>'}
    new = {"word/header1.xml": f'<w:hdr xmlns:w="{W_NS}">{_p("New")}</w:hdr>',
           "word/styles.xml": f'<w:styles xmlns:w="{W_NS}"><w:docDefaults/></w:styles>'}
    page, items, _ = _all(_snapshot(_p("Same"), extras=old), _snapshot(_p("Same"), extras=new))
    parts = {i["part_name"]: i for i in items if i["kind"] == "package_part"}
    assert parts["word/header1.xml"]["status"] == parts["word/styles.xml"]["status"] == "changed_bytes"
    assert parts["word/header1.xml"]["content_interpretation"] == "not_claimed"
    assert page["observations"]["document_equivalence"] == "not_claimed"


@pytest.mark.parametrize("unknown", [
    '<x:wrapper><w:p><w:r><w:t>Hidden</w:t></w:r></w:p></x:wrapper>',
    '<w:p><x:wrapper><w:r><w:t>Hidden</w:t></w:r></x:wrapper></w:p>',
])
def test_unknown_content_retains_partition_and_honest_coverage(unknown):
    page, items, _ = _all(_snapshot(_p("A")), _snapshot(_p("A"), unknown))
    assert page["coverage"]["partition_complete"]
    assert not page["coverage"]["supported_scope_complete"]
    assert page["coverage"]["incoming"]["excluded_subtrees"] == 1
    assert page["observations"]["document_equivalence"] == "not_claimed"


def test_source_and_parameter_changes_invalidate_pagination_and_are_read_only(tmp_path):
    left, right = _snapshot(_p("A")), _snapshot(_p("B"))
    paths = [tmp_path / "sent.docx", tmp_path / "incoming.docx"]
    for path, source in zip(paths, (left, right), strict=True):
        path.write_bytes(source.payload)
    first = compare.compare_incoming_version(str(paths[0]), left.sha256, str(paths[1]), right.sha256, max_items=1)
    args = {"max_items": 1, "cursor": first["next_cursor"]}
    with pytest.raises(compare.IncomingComparisonError, match="stale_comparison_cursor"):
        compare.compare_incoming_version(str(paths[1]), right.sha256, str(paths[0]), left.sha256, **args)
    with pytest.raises(compare.IncomingComparisonError, match="stale_comparison_cursor"):
        compare.compare_incoming_version(str(paths[0]), left.sha256, str(paths[1]), right.sha256,
                                         **{**args, "max_items": 2})
    assert paths[0].read_bytes() == left.payload
    paths[1].write_bytes(_snapshot(_p("C")).payload)
    with pytest.raises(SnapshotError):
        compare.compare_incoming_version(str(paths[0]), left.sha256, str(paths[1]), right.sha256, **args)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["incoming.docx", "sent.docx"]


@pytest.mark.parametrize("key,value", [("report_items", 1), ("report_bytes", 100),
                                       ("projection_chars_per_document", 1), ("candidate_memberships", 1),
                                       ("revision_occurrences_per_paragraph", 1)])
def test_work_limits_refuse_instead_of_returning_no_differences(monkeypatch, key, value):
    monkeypatch.setattr(compare, "LIMITS", MappingProxyType({**compare.LIMITS, key: value}))
    with pytest.raises(ResourceLimitError):
        compare.build_incoming_comparison(_snapshot(_change("Old", "New")), _snapshot(_p("New")))


def test_page_budget_keeps_complete_text_and_cursor_can_reach_every_member(monkeypatch):
    source = _snapshot(*[_p("Repeated") for _ in range(100)])
    computation = compare.build_incoming_comparison(source, source)
    monkeypatch.setattr(compare, "LIMITS", MappingProxyType({**compare.LIMITS, "page_text_chars": 16}))
    cursor, total = None, []
    while True:
        page = compare.page_incoming_comparison(computation, max_items=100, cursor=cursor)
        total.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len([i for i in total if i["kind"] == "paragraph"]) == 200
    assert all(i["text"] == "Repeated" for i in total if i["kind"] == "paragraph")


@pytest.mark.parametrize("body", [
    _p(), _p("ordinary"), _change("old", "new"),
    _p("Tabs", '<w:pPr><w:tabs><w:tab w:val="left" w:pos="1440"/></w:tabs></w:pPr>'),
    _p("New paragraph", '<w:pPr><w:rPr><w:ins w:id="4"/></w:rPr></w:pPr>'),
    _table([[_p("A"), _change("B", "C")]]),
])
def test_batched_projections_equal_existing_contract(body):
    snapshot = _load_snapshot_from_payload(_snapshot(body).payload, path="")
    batched = build_document_projections_v1(snapshot.body_flow)
    assert batched == [build_paragraph_projection_v1(p.element, snapshot.body_flow) for p in snapshot.paragraphs]


@pytest.mark.anyio
async def test_mcp_advertises_required_hashes_and_returns_schema_valid_result(tmp_path):
    from veqtor_mcp.server import mcp
    left, right = _snapshot(_p("45")), _snapshot(_p("30"))
    paths = [tmp_path / "sent.docx", tmp_path / "incoming.docx"]
    for path, source in zip(paths, (left, right), strict=True):
        path.write_bytes(source.payload)
    arguments = dict(sent_path=str(paths[0]), sent_sha256=left.sha256,
                     incoming_path=str(paths[1]), incoming_sha256=right.sha256)
    async with Client(mcp) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "compare_incoming_version")
        assert set(tool.input_schema["required"]) == set(arguments)
        result = await client.call_tool("compare_incoming_version", arguments)
        assert not result.is_error
        data = result.structured_content
        if "result" in data:
            data = data["result"]
        jsonschema.Draft202012Validator(RESULT).validate(data)
        assert data["record_status"] == "disabled"
        missing = {k: v for k, v in arguments.items() if k != "sent_sha256"}
        assert (await client.call_tool("compare_incoming_version", missing)).is_error


def test_valid_altchunk_is_visible_but_its_content_is_not_compared():
    relationships = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + (
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk" Target="afchunk.html"/>'
    ) + '</Relationships>'
    chunk = '<w:altChunk xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="rId1"/>'
    page, items, _ = _all(_snapshot(_p("Known")), _snapshot(_p("Known"), chunk, extras={
        "word/_rels/document.xml.rels": relationships, "word/afchunk.html": "<html>Hidden</html>",
    }))
    assert not page["coverage"]["supported_scope_complete"]
    assert "word/afchunk.html" in page["coverage"]["incoming"]["excluded_parts"]
    assert any(i["kind"] == "package_part" and i["part_name"] == "word/afchunk.html" for i in items)


@pytest.mark.parametrize("change", [
    lambda xml: xml.replace('w:w="2000"', 'w:w="3000"'),
    lambda xml: xml.replace('<w:tr>', '<w:tr><w:trPr><w:trHeight w:val="500"/></w:trPr>'),
    lambda xml: xml.replace('<w:tc>', '<w:tc><w:tcPr><w:tcW w:w="4000" w:type="dxa"/></w:tcPr>'),
])
def test_table_dimensions_are_properties_not_topology_or_rendered_equality(change):
    table = _table([[_p("A"), _p("B")]])
    page, items, _ = _all(_snapshot(table), _snapshot(change(table)))
    assert page["observations"]["current_table_topology"] == "same"
    assert page["observations"]["table_property_markup_sequence_equal"] is False
    old = [i["property_markup_sha256"] for i in items if i["kind"] in {"table", "row", "cell"} and i["side"] == "sent"]
    new = [i["property_markup_sha256"] for i in items if i["kind"] in {"table", "row", "cell"} and i["side"] == "incoming"]
    assert old != new


def test_disconnected_remaining_pair_is_not_definitively_explained():
    _, items, _ = _all(_snapshot(_p("A"), _p("old"), _p("B")),
                       _snapshot(_p("B"), _p("new"), _p("A")))
    group = next(g for g in _groups(items) if g["basis"] == "unresolved_correspondence")
    assert group["sent_count"] == group["incoming_count"] == 1
    assert group["explanation"] == "indeterminate"


def test_large_anchor_index_does_not_rescan_pairs_per_interval():
    class CountedPairs(list):
        traversed = 0

        def __iter__(self):
            for item in super().__iter__():
                self.traversed += 1
                yield item
    pairs = CountedPairs((i, i) for i in range(10_000))
    intervals = list(compare._bounded_intervals(pairs, 10_000, 10_000))
    assert len(intervals) == 10_001
    assert pairs.traversed == 10_000


def test_large_document_reuses_global_projection_validation(monkeypatch):
    from veqtor_docx import _projection
    original = _projection._validate_revision_placements
    calls = []

    def checked(flow):
        calls.append(len(flow.paragraphs))
        return original(flow)

    monkeypatch.setattr(_projection, "_validate_revision_placements", checked)
    source = _snapshot(*[_p(f"Synthetic paragraph {i}") for i in range(1000)])
    result = compare.build_incoming_comparison(source, source)
    assert calls == [1000, 1000]
    assert len(result.items) == 3002


@pytest.mark.parametrize("kwargs", [
    {"max_items": 0}, {"max_items": 101}, {"max_items": True}, {"max_items": 1.5},
    {"cursor": "ic1:0:" + "a" * 64}, {"cursor": "ic1:1:" + "a" * 64 + "\n"},
    {"cursor": 1}, {"cursor": "x" * 10000},
])
def test_invalid_page_inputs_refuse_before_source_io(kwargs, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("source must not be opened")
    monkeypatch.setattr(compare, "capture_document_snapshot", forbidden)
    with pytest.raises(compare.IncomingComparisonError):
        compare.compare_incoming_version("sent", "a" * 64, "incoming", "b" * 64, **kwargs)


@pytest.mark.parametrize("value", [None, True, "a" * 63, "A" * 64, "a" * 64 + "\n"])
def test_invalid_hashes_refuse_before_source_io(value, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("source must not be opened")
    monkeypatch.setattr(compare, "capture_document_snapshot", forbidden)
    with pytest.raises(compare.IncomingComparisonError):
        compare.compare_incoming_version("sent", value, "incoming", "b" * 64)


def test_computation_bytes_are_bound_and_page_values_are_detached():
    from dataclasses import replace
    source = _snapshot(_p("Same"))
    result = compare.build_incoming_comparison(source, source)
    with pytest.raises(compare.IncomingComparisonError):
        replace(result, metadata=result.metadata + b" ")
    with pytest.raises(compare.IncomingComparisonError):
        replace(result, items=result.items[:-1])
    page = compare.page_incoming_comparison(result)
    page["coverage"]["partition_complete"] = False
    page["items"].clear()
    fresh = compare.page_incoming_comparison(result)
    assert fresh["coverage"]["partition_complete"] is True
    assert fresh["items"]


@pytest.mark.parametrize("key,limit", [("candidate_memberships", 2),
                                       ("projection_chars_per_document", 3),
                                       ("revision_occurrences_per_paragraph", 2)])
def test_exact_work_limit_passes_and_one_less_refuses(monkeypatch, key, limit):
    source = _snapshot(_change("old", "new")) if key == "revision_occurrences_per_paragraph" else _snapshot(_p("abc"))
    monkeypatch.setattr(compare, "LIMITS", MappingProxyType({**compare.LIMITS, key: limit}))
    compare.build_incoming_comparison(source, source)
    monkeypatch.setattr(compare, "LIMITS", MappingProxyType({**compare.LIMITS, key: limit - 1}))
    with pytest.raises(ResourceLimitError):
        compare.build_incoming_comparison(source, source)


def test_exact_item_budget_and_projection_character_limit(monkeypatch):
    source = _snapshot(_p("A"))
    count = len(compare.build_incoming_comparison(source, source).items)
    monkeypatch.setattr(compare, "LIMITS", MappingProxyType({**compare.LIMITS, "report_items": count}))
    compare.build_incoming_comparison(source, source)
    monkeypatch.setattr(compare, "LIMITS", MappingProxyType({**compare.LIMITS, "report_items": count - 1}))
    with pytest.raises(ResourceLimitError):
        compare.build_incoming_comparison(source, source)


def test_full_length_quotes_cross_page_limits_without_truncation():
    source = _snapshot(_p("x" * 50_000))
    _, items, pages = _all(source, source)
    assert len(pages) >= 2
    assert all(len(i["text"]) == 50_000 for i in items if i["kind"] == "paragraph")
    excessive = _snapshot(_p("x" * 50_001))
    with pytest.raises(ResourceLimitError):
        compare.build_incoming_comparison(excessive, source)


@pytest.mark.anyio
@pytest.mark.parametrize("side", ["sent", "incoming"])
@pytest.mark.parametrize("excluded", [False, True])
@pytest.mark.parametrize("length", [50_000, 50_001])
async def test_mcp_bounds_later_quotes_before_first_page_with_unavailable_projection(
    tmp_path, side, excluded, length,
):
    from veqtor_mcp.server import mcp

    # The first comparison group does not contain the later oversized quote.
    # An unrelated excluded wrapper makes all rejected projections unavailable.
    blocks = [_p("First anchor"), _p("Second anchor"), _p("x" * length)]
    if excluded:
        blocks.append('<x:wrapper>' + _p("Excluded content") + '</x:wrapper>')
    sources = {"sent": _snapshot(_p("First anchor"), _p("Second anchor"), _p("short")),
               "incoming": _snapshot(_p("First anchor"), _p("Second anchor"), _p("short"))}
    sources[side] = _snapshot(*blocks)
    arguments = {"max_items": 1}
    for role, source in sources.items():
        path = tmp_path / f"{role}.docx"
        path.write_bytes(source.payload)
        arguments[role + "_path"] = str(path)
        arguments[role + "_sha256"] = source.sha256

    items = []
    async with Client(mcp) as client:
        while True:
            result = await client.call_tool("compare_incoming_version", arguments)
            if length > 50_000:
                assert not items, "over-limit quotes must refuse before any page succeeds"
                assert result.is_error
                error = " ".join(getattr(content, "text", "") for content in result.content)
                assert "resource_limit_exceeded" in error
                assert "output_contract_error" not in error
                break
            assert not result.is_error
            data = result.structured_content
            if "result" in data:
                data = data["result"]
            jsonschema.Draft202012Validator(RESULT).validate(data)
            assert data["coverage"][side]["unavailable_projections"] == (3 if excluded else 0)
            assert data["coverage"][side]["excluded_paragraphs"] == int(excluded)
            assert data["coverage"]["supported_scope_complete"] is (not excluded)
            items.extend(data["items"])
            if data["next_cursor"] is None:
                break
            arguments["cursor"] = data["next_cursor"]

    if length == 50_000:
        paragraphs = [item for item in items if item["kind"] == "paragraph" and item["side"] == side]
        assert len(paragraphs) == 3
        quote = next(item for item in paragraphs if item["paragraph_ref"]["paragraph_index"] == 2)
        assert quote["text"] == "x" * length
        assert quote["rejected_text"] == (None if excluded else "x" * length)
    for role, source in sources.items():
        assert (tmp_path / f"{role}.docx").read_bytes() == source.payload


def test_structural_revision_unavailable_projection_cannot_prove_equivalence():
    original = _snapshot(_p("Text"))
    structural = _snapshot(_p("Text", '<w:pPr><w:rPr><w:ins w:id="7"/></w:rPr></w:pPr>'))
    page, items, _ = _all(original, structural)
    assert page["observations"]["current_text_sequence_equal"]
    assert not page["coverage"]["supported_scope_complete"]
    assert _groups(items)[0]["explanation"] == "indeterminate"


def test_malformed_table_has_explicit_partial_inventory_and_all_paragraphs():
    broken = _table([[_p("A"), _p("B")]]).replace('<w:tc>', '<w:tc><w:tcPr><w:gridSpan w:val="bad"/></w:tcPr>', 1)
    page, _, _ = _all(_snapshot(_p("A")), _snapshot(broken))
    assert page["coverage"]["incoming"]["indexed_paragraphs"] == 2
    assert page["observations"]["current_table_topology"] == "unavailable"
    assert page["observations"]["table_property_markup_sequence_equal"] is None
    assert page["coverage"]["incoming"]["table_coverage_reason"] == "table_structure_unsupported"


def test_no_new_partial_output_when_table_budget_exceeded(monkeypatch):
    from veqtor_docx import table_map
    monkeypatch.setattr(table_map, "TABLE_MAP_LIMITS", {**table_map.TABLE_MAP_LIMITS, "tables": 0})
    with pytest.raises(ResourceLimitError):
        compare.build_incoming_comparison(_snapshot(_p("A")), _snapshot(_table([[_p("A"), _p()]])))


@pytest.mark.parametrize("mutation", [
    lambda p: p["pagination"].update(returned_items=0),
    lambda p: p["coverage"]["sent"].update(accounted_paragraphs=0),
    lambda p: p["coverage"].update(supported_scope_complete=False),
    lambda p: p["observations"].update(package_bytes_equal=False),
    lambda p: next(i for i in p["items"] if i["kind"] == "paragraph")["paragraph_ref"].update(file_sha256="a" * 64),
    lambda p: next(i for i in p["items"] if i["kind"] == "paragraph").update(text="Spoofed"),
])
def test_mcp_exit_rejects_inconsistent_counts_sources_and_quotes(mutation):
    from veqtor_mcp import server
    source = _snapshot(_p("Same"))
    page = compare.page_incoming_comparison(compare.build_incoming_comparison(source, source))
    mutation(page)
    with pytest.raises(server._OutputContractError):
        server._validated_success_result("compare_incoming_version", page)


def test_wheel_inventory_includes_new_core_contract_and_documentation():
    import tomllib
    from pathlib import Path
    config = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    targets = config["tool"]["hatch"]["build"]["targets"]
    assert "/src/veqtor_docx/incoming_comparison.py" in targets["wheel"]["include"]
    assert "/src/veqtor_mcp/_incoming_contract.py" in targets["wheel"]["include"]
    assert "/docs/INCOMING_COMPARISON.md" in targets["sdist"]["include"]


def test_whole_document_formatting_changes_do_not_become_text_equivalence():
    old = [_p(f"Clause {i}").replace('<w:r>', '<w:r><w:rPr><w:b/></w:rPr>') for i in range(4)]
    new = [p.replace('<w:b/>', '<w:i/>') for p in old]
    page, items, _ = _all(_snapshot(*old), _snapshot(*new), max_items=2)
    assert page["observations"]["current_text_sequence_equal"]
    assert page["observations"]["document_equivalence"] == "not_claimed"
    assert len(_groups(items)) == 4
    assert all("property_markup_changed" in g["observations"] for g in _groups(items))
    assert all(g["explanation"] == "not_applicable" for g in _groups(items))


def test_revision_metadata_changes_with_same_current_text_remain_visible():
    old = _change("old", "new")
    new = old.replace('2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')
    page, items, _ = _all(_snapshot(old), _snapshot(new))
    assert page["observations"]["current_text_sequence_equal"]
    assert "revision_markup_changed" in _groups(items)[0]["observations"]
    assert "revision_identifiers_changed" not in _groups(items)[0]["observations"]


def test_same_text_changed_structure_still_invalidates_cursor(tmp_path):
    left = _snapshot(_table([[_p("A"), _p("B")]]))
    changed = _snapshot(_table([[_p("A") + _p("B"), _p()]]))
    source = tmp_path / "source.docx"
    source.write_bytes(left.payload)
    original = compare.compare_incoming_version(str(source), left.sha256, str(source), left.sha256, max_items=1)
    source.write_bytes(changed.payload)
    with pytest.raises(compare.IncomingComparisonError, match="stale_comparison_cursor"):
        compare.compare_incoming_version(str(source), changed.sha256, str(source), changed.sha256,
                                         max_items=1, cursor=original["next_cursor"])


def test_no_indexed_paragraphs_does_not_hide_excluded_content():
    page, items, _ = _all(_snapshot(), _snapshot('<x:wrapper>' + _p("Excluded") + '</x:wrapper>'))
    assert page["coverage"]["partition_complete"]
    assert page["coverage"]["sent"]["indexed_paragraphs"] == 0
    assert page["coverage"]["incoming"]["indexed_paragraphs"] == 0
    assert page["coverage"]["incoming"]["excluded_paragraphs"] == 1
    assert not page["coverage"]["supported_scope_complete"]
    assert not _groups(items)
    assert any(p["status"] == "changed_bytes" for p in items if p["kind"] == "package_part")
