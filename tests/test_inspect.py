# SPDX-License-Identifier: Apache-2.0
"""Focused contract tests for bounded, hash-bound document inspection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from veqtor_docx import InspectError, inspect_document, verify_quote
from veqtor_docx import inspect as inspect_module
from veqtor_docx._ooxml import ResourceLimitError, parse_xml, w

_PACKAGE_RELATIONSHIPS_NS = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
_OFFICE_RELATIONSHIPS_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_ALT_CHUNK_RELATIONSHIP_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk"
)


def _round(demo_dir: Path, number: int) -> str:
    return str(sorted(demo_dir.glob("*.docx"))[number - 1])


def _rewrite_document(source: Path, target: Path, mutate) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as output:
        for info in original.infolist():
            payload = original.read(info)
            if info.filename == "word/document.xml":
                document = parse_xml(payload)
                mutate(document)
                payload = (
                    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
                    + etree.tostring(document)
                )
            output.writestr(info, payload)


def _rewrite_with_alt_chunk(
    source: Path,
    target: Path,
    *,
    relationship_target: str,
    html_payload: bytes | None,
    html_member_name: str = "word/afchunk.html",
    relationship_type: str = _ALT_CHUNK_RELATIONSHIP_TYPE,
    relationship_id: str = "rIdInspectionAltChunk",
    alt_chunk_relationship_id: str = "rIdInspectionAltChunk",
    additional_relationship_ids: tuple[str, ...] = (),
    target_mode: str | None = None,
    wrap_in_unknown: bool = False,
) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as output:
        for info in original.infolist():
            payload = original.read(info)
            if info.filename == "word/document.xml":
                document = parse_xml(payload)
                body = document.find(w("body"))
                assert body is not None
                alt_chunk = etree.Element(w("altChunk"))
                alt_chunk.set(
                    f"{{{_OFFICE_RELATIONSHIPS_NS}}}id", alt_chunk_relationship_id
                )
                block = alt_chunk
                if wrap_in_unknown:
                    block = etree.Element("{urn:veqtor:test:unknown}container")
                    block.append(alt_chunk)
                section = body.find(w("sectPr"))
                if section is None:
                    body.append(block)
                else:
                    section.addprevious(block)
                payload = (
                    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
                    + etree.tostring(document)
                )
            elif info.filename == "word/_rels/document.xml.rels":
                relationships = parse_xml(payload)
                for rel_id in (relationship_id, *additional_relationship_ids):
                    relationship = etree.SubElement(
                        relationships,
                        f"{{{_PACKAGE_RELATIONSHIPS_NS}}}Relationship",
                    )
                    relationship.set("Id", rel_id)
                    relationship.set("Type", relationship_type)
                    relationship.set("Target", relationship_target)
                    if target_mode is not None:
                        relationship.set("TargetMode", target_mode)
                payload = etree.tostring(
                    relationships,
                    xml_declaration=True,
                    encoding="UTF-8",
                    standalone=True,
                )
            output.writestr(info, payload)
        if html_payload is not None:
            output.writestr(html_member_name, html_payload)


def _run(parent: etree._Element, text: str, *, deleted: bool = False) -> None:
    run = etree.SubElement(parent, w("r"))
    atom = etree.SubElement(run, w("delText") if deleted else w("t"))
    atom.text = text


def _paragraph(text: str = "", *, outline_level: int | None = None) -> etree._Element:
    paragraph = etree.Element(w("p"))
    if outline_level is not None:
        properties = etree.SubElement(paragraph, w("pPr"))
        outline = etree.SubElement(properties, w("outlineLvl"))
        outline.set(w("val"), str(outline_level))
    if text:
        _run(paragraph, text)
    return paragraph


def _replace_body(document: etree._Element, blocks: list[etree._Element]) -> None:
    body = document.find(w("body"))
    assert body is not None
    section_properties = body.find(w("sectPr"))
    for child in list(body):
        if child is not section_properties:
            body.remove(child)
    for block in blocks:
        if section_properties is None:
            body.append(block)
        else:
            section_properties.addprevious(block)


def _revision(
    parent: etree._Element,
    kind: str,
    revision_id: str,
    text: str,
) -> None:
    wrapper = etree.SubElement(parent, w(kind))
    wrapper.set(w("id"), revision_id)
    wrapper.set(w("author"), "Inspection Fixture")
    _run(wrapper, text, deleted=kind == "del")


def test_outline_is_text_free_hash_bound_and_explicit_about_scope(
    demo_dir: Path,
) -> None:
    result = inspect_document(_round(demo_dir, 1), "outline")

    assert result["mode"] == "outline"
    assert result["search_scope"] == "word_document_xml_body_v1"
    assert result["reading_mode"] == "accepted_current_v1"
    assert result["container_policy"] == "canonical_body_flow_v1"
    assert result["has_tracked_text_revisions"] is False
    assert result["revision_inventory"]["schema_version"] == ("revision_inventory.v2")
    assert result["revision_inventory"]["tracked_text_revision_elements"] == 0
    assert result["coverage"]["scan_complete"] is True
    assert result["coverage"]["output_truncated"] is False
    assert result["coverage"]["included_parts"] == ["word/document.xml"]
    assert result["coverage"]["excluded_parts"] == [
        "word/header*.xml",
        "word/footer*.xml",
        "word/footnotes.xml",
        "word/endnotes.xml",
        "word/comments*.xml",
    ]
    assert result["limits"]["wall_clock_partial_results"] is False

    section = next(item for item in result["sections"] if item["label"] == "14.2")
    assert section["heading"] == "Limitation of Liability"
    assert "text" not in section
    assert "snippet" not in section
    assert section["section_ref"] == {
        "schema_version": "section_ref.v1",
        "ref_type": "section",
        "file_sha256": result["file_sha256"],
        "part_name": "word/document.xml",
        "heading_paragraph_index": section["start_paragraph_index"],
        "heading_text_sha256": section["section_ref"]["heading_text_sha256"],
        "reading_mode": "accepted_current_v1",
        "container_policy": "canonical_body_flow_v1",
    }


def test_outline_level_nine_is_body_text_and_invalid_levels_fail_closed(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    body_text = tmp_path / "outline-level-nine.docx"

    def add_body_text_level(document: etree._Element) -> None:
        _replace_body(document, [_paragraph("Not a heading", outline_level=9)])

    _rewrite_document(source, body_text, add_body_text_level)
    result = inspect_document(str(body_text), "outline")
    assert result["sections"] == []

    invalid = tmp_path / "outline-level-negative.docx"

    def add_invalid_level(document: etree._Element) -> None:
        _replace_body(document, [_paragraph("Invalid heading", outline_level=-1)])

    _rewrite_document(source, invalid, add_invalid_level)
    with pytest.raises(InspectError) as error:
        inspect_document(str(invalid), "outline")
    assert error.value.code == "file_unextractable"
    assert (
        error.value.metadata["observed_source_sha256"]
        == hashlib.sha256(invalid.read_bytes()).hexdigest()
    )


def test_empty_outline_heading_is_returned_and_navigation_stays_consistent(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "empty-outline-heading.docx"

    def add_empty_heading(document: etree._Element) -> None:
        _replace_body(
            document,
            [
                _paragraph(outline_level=0),
                _paragraph("Following clause text."),
            ],
        )

    _rewrite_document(source, target, add_empty_heading)
    outline = inspect_document(str(target), "outline")

    assert len(outline["sections"]) == 1
    section = outline["sections"][0]
    assert section["label"] is None
    assert section["heading"] is None
    assert section["start_paragraph_index"] == 0
    assert section["end_paragraph_index_exclusive"] == 2

    browsed = inspect_document(str(target), "browse")
    assert browsed["paragraphs"] == [
        {
            **browsed["paragraphs"][0],
            "text": "Following clause text.",
            "section_navigation": {
                "label": None,
                "heading": None,
                "level": 0,
                "basis": "word_outline_level_v1",
                "label_basis": None,
            },
        }
    ]


def test_document_requires_exactly_one_direct_body(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "two-bodies.docx"

    def add_second_body(document: etree._Element) -> None:
        second_body = etree.SubElement(document, w("body"))
        second_body.append(_paragraph("Text hidden by a second body."))

    _rewrite_document(source, target, add_second_body)
    with pytest.raises(InspectError) as error:
        inspect_document(str(target), "browse")

    assert error.value.code == "file_unextractable"
    assert "exactly one direct w:body" in error.value.detail
    assert (
        error.value.metadata["observed_source_sha256"]
        == hashlib.sha256(target.read_bytes()).hexdigest()
    )


def test_relationship_backed_alt_chunk_is_fail_visible_and_target_is_disclosed(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "alt-chunk.docx"
    governing_law = (
        b"<html><body>This Agreement is governed by New York law.</body></html>"
    )
    _rewrite_with_alt_chunk(
        source,
        target,
        relationship_target="afchunk.html",
        html_payload=governing_law,
    )

    result = inspect_document(
        str(target),
        "literal_search",
        phrases=["governed by New York law"],
        match_basis="exact_literal",
    )

    assert result["matches"] == []
    assert result["coverage"]["scan_complete"] is True
    container_coverage = result["coverage"]["container_coverage"]
    assert container_coverage["excluded_subtree_count"] == 1
    assert container_coverage["excluded_by_kind"] == {"alt_chunk": 1}
    assert container_coverage["coverage_complete"] is False
    assert container_coverage["legacy_two_field_anchor_safe"] is False
    assert "word/afchunk.html" in result["coverage"]["excluded_parts"]


def test_nested_alt_chunk_cannot_bypass_unknown_container_coverage(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "nested-alt-chunk.docx"
    _rewrite_with_alt_chunk(
        source,
        target,
        relationship_target="afchunk.html",
        html_payload=b"<html><body>Nested imported clause.</body></html>",
        wrap_in_unknown=True,
    )

    result = inspect_document(str(target), "outline")

    container_coverage = result["coverage"]["container_coverage"]
    assert container_coverage["excluded_subtree_count"] == 1
    assert container_coverage["excluded_by_kind"] == {"unknown_container": 1}
    assert container_coverage["coverage_complete"] is False
    assert container_coverage["legacy_two_field_anchor_safe"] is False
    assert "word/afchunk.html" in result["coverage"]["excluded_parts"]


def test_external_alt_chunk_target_is_never_disclosed(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "external-alt-chunk.docx"
    private_url = "https://private.example.test/contracts/governing-law.html"
    _rewrite_with_alt_chunk(
        source,
        target,
        relationship_target=private_url,
        target_mode="External",
        html_payload=None,
    )

    result = inspect_document(str(target), "outline")
    serialized = json.dumps(result, sort_keys=True)

    assert all("altChunk:" not in part for part in result["coverage"]["excluded_parts"])
    assert result["coverage"]["container_coverage"]["excluded_by_kind"] == {
        "alt_chunk": 1
    }
    assert result["coverage"]["container_coverage"]["coverage_complete"] is False
    assert private_url not in serialized


def test_missing_internal_alt_chunk_target_fails_closed(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "missing-alt-chunk-target.docx"
    _rewrite_with_alt_chunk(
        source,
        target,
        relationship_target="missing-afchunk.html",
        html_payload=None,
    )

    with pytest.raises(InspectError) as error:
        inspect_document(str(target), "outline")

    assert error.value.code == "file_unextractable"
    assert "internal target is invalid or missing" in error.value.detail
    assert (
        error.value.metadata["observed_source_sha256"]
        == hashlib.sha256(target.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    ("relationship_target", "member_name"),
    [
        ("%FF.html", "word/\ufffd.html"),
        ("%ED%A0%80.html", "word/\ufffd\ufffd\ufffd.html"),
        ("%ZZ.html", "word/%ZZ.html"),
    ],
)
def test_invalid_percent_encoded_alt_chunk_target_fails_closed_before_member_lookup(
    demo_dir: Path,
    tmp_path: Path,
    relationship_target: str,
    member_name: str,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = (
        tmp_path
        / f"invalid-percent-{hashlib.sha256(relationship_target.encode()).hexdigest()[:8]}.docx"
    )
    _rewrite_with_alt_chunk(
        source,
        target,
        relationship_target=relationship_target,
        html_payload=b"<html><body>Must not be disclosed.</body></html>",
        html_member_name=member_name,
    )

    with pytest.raises(InspectError) as error:
        inspect_document(str(target), "outline")

    assert error.value.code == "file_unextractable"
    assert "internal target is invalid or missing" in error.value.detail
    assert member_name not in json.dumps(error.value.metadata, ensure_ascii=False)


def test_literal_replacement_character_alt_chunk_does_not_collide_with_invalid_utf8(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    member_name = "word/\ufffd.html"
    target = tmp_path / "literal-replacement-character.docx"
    _rewrite_with_alt_chunk(
        source,
        target,
        relationship_target="\ufffd.html",
        html_payload=b"<html><body>Literal replacement character.</body></html>",
        html_member_name=member_name,
    )

    result = inspect_document(str(target), "outline")

    assert member_name in result["coverage"]["excluded_parts"]


@pytest.mark.parametrize(
    ("xml_whitespace", "serialized_reference"),
    [
        ("\t", b"&#9;"),
        ("\n", b"&#10;"),
        ("\r", b"&#13;"),
    ],
)
def test_alt_chunk_target_preserves_xsd_any_uri_whitespace_as_a_space(
    demo_dir: Path,
    tmp_path: Path,
    xml_whitespace: str,
    serialized_reference: bytes,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / f"any-uri-{ord(xml_whitespace)}.docx"
    member_name = "word/foo .html"
    _rewrite_with_alt_chunk(
        source,
        target,
        relationship_target=f"foo{xml_whitespace}.html",
        html_payload=b"<html><body>Collapsed anyURI whitespace.</body></html>",
        html_member_name=member_name,
    )

    with zipfile.ZipFile(target) as archive:
        relationships_payload = archive.read("word/_rels/document.xml.rels")
    assert serialized_reference in relationships_payload

    result = inspect_document(str(target), "outline")

    assert member_name in result["coverage"]["excluded_parts"]


def test_alt_chunk_target_collapses_repeated_and_edge_xsd_any_uri_whitespace(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "collapsed-any-uri-whitespace.docx"
    member_name = "word/imports/ governing law.html"
    _rewrite_with_alt_chunk(
        source,
        target,
        relationship_target="  imports/\t \n\r  governing law.html   ",
        html_payload=b"<html><body>Collapsed anyURI whitespace.</body></html>",
        html_member_name=member_name,
    )

    result = inspect_document(str(target), "outline")

    assert member_name in result["coverage"]["excluded_parts"]


def test_alt_chunk_relationship_type_applies_xsd_any_uri_whitespace_collapse(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "collapsed-relationship-type.docx"
    relationship_type = f" \t\n{_ALT_CHUNK_RELATIONSHIP_TYPE}\r  "
    _rewrite_with_alt_chunk(
        source,
        target,
        relationship_target="afchunk.html",
        relationship_type=relationship_type,
        html_payload=b"<html><body>Collapsed relationship Type.</body></html>",
    )

    result = inspect_document(str(target), "outline")

    assert "word/afchunk.html" in result["coverage"]["excluded_parts"]


def test_alt_chunk_relationship_id_applies_xsd_id_whitespace_collapse(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "collapsed-relationship-id.docx"
    _rewrite_with_alt_chunk(
        source,
        target,
        relationship_target="afchunk.html",
        relationship_id="\t\nrIdInspectionAltChunk\r ",
        html_payload=b"<html><body>Collapsed relationship Id.</body></html>",
    )

    result = inspect_document(str(target), "outline")

    assert "word/afchunk.html" in result["coverage"]["excluded_parts"]


def test_alt_chunk_relationship_ids_colliding_after_xsd_collapse_fail_closed(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "ambiguous-collapsed-relationship-ids.docx"
    _rewrite_with_alt_chunk(
        source,
        target,
        relationship_target="afchunk.html",
        additional_relationship_ids=(" \trIdInspectionAltChunk\n ",),
        html_payload=b"<html><body>Ambiguous relationship.</body></html>",
    )

    with pytest.raises(InspectError) as error:
        inspect_document(str(target), "outline")

    assert error.value.code == "file_unextractable"
    assert "relationship is missing or ambiguous" in error.value.detail


def test_alt_chunk_reference_id_is_not_xsd_whitespace_normalized(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "uncollapsed-alt-chunk-reference-id.docx"
    _rewrite_with_alt_chunk(
        source,
        target,
        relationship_target="afchunk.html",
        alt_chunk_relationship_id="\trIdInspectionAltChunk\n",
        html_payload=b"<html><body>Unmatched relationship reference.</body></html>",
    )

    with pytest.raises(InspectError) as error:
        inspect_document(str(target), "outline")

    assert error.value.code == "file_unextractable"
    assert "relationship is missing or ambiguous" in error.value.detail


def test_alt_chunk_target_does_not_collapse_percent_decoded_spaces(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "percent-decoded-spaces.docx"
    member_name = "word/foo  bar.html"
    _rewrite_with_alt_chunk(
        source,
        target,
        relationship_target="foo%20%20bar.html",
        html_payload=b"<html><body>Two encoded spaces.</body></html>",
        html_member_name=member_name,
    )

    result = inspect_document(str(target), "outline")

    assert member_name in result["coverage"]["excluded_parts"]


def test_literal_search_bases_and_complete_count_are_deterministic(
    demo_dir: Path,
) -> None:
    path = _round(demo_dir, 1)
    exact_miss = inspect_document(
        path,
        "literal_search",
        phrases=["EXCEPT AS SET OUT IN CLAUSE 14.3"],
        match_basis="exact_literal",
    )
    assert exact_miss["matches"] == []
    assert exact_miss["coverage"]["complete_literal_match_count"] == 0

    normalized = inspect_document(
        path,
        "literal_search",
        phrases=["Except   as set out in Clause 14.3"],
        match_basis="normalized_literal",
    )
    assert len(normalized["matches"]) == 1
    assert normalized["matches"][0]["snippet"]["match_start"] == 0

    first = inspect_document(
        path,
        "literal_search",
        phrases=[
            "EXCEPT AS SET OUT IN CLAUSE 14.3",
            "except as set out in clause 14.3",
        ],
        match_basis="normalized_casefold_literal",
        max_items=1,
    )
    assert len(first["matches"]) == 1
    assert first["matches"][0]["phrase_index"] == 0
    assert first["coverage"]["eligible_item_count"] == 2
    assert first["coverage"]["complete_literal_match_count"] == 2
    assert first["coverage"]["returned_item_count"] == 1
    assert first["coverage"]["output_truncated"] is True
    assert first["next_cursor"] is not None

    second = inspect_document(
        path,
        "literal_search",
        phrases=[
            "EXCEPT AS SET OUT IN CLAUSE 14.3",
            "except as set out in clause 14.3",
        ],
        match_basis="normalized_casefold_literal",
        cursor=first["next_cursor"],
        max_items=1,
    )
    assert [item["phrase_index"] for item in second["matches"]] == [1]
    assert second["coverage"]["complete_literal_match_count"] == 2
    assert second["coverage"]["cursor_offset"] == 1
    assert second["next_cursor"] is None

    with pytest.raises(InspectError) as error:
        inspect_document(
            path,
            "literal_search",
            phrases=["different query"],
            match_basis="normalized_casefold_literal",
            cursor=first["next_cursor"],
            max_items=1,
        )
    assert error.value.code == "cursor_mismatch"


def test_cursor_binds_semantic_policy_and_complete_result_set(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _round(demo_dir, 1)
    kwargs = {
        "phrases": [
            "EXCEPT AS SET OUT IN CLAUSE 14.3",
            "except as set out in clause 14.3",
        ],
        "match_basis": "normalized_casefold_literal",
        "max_items": 1,
    }
    first = inspect_document(path, "literal_search", **kwargs)
    cursor = first["next_cursor"]
    assert cursor is not None

    original_order_policy = inspect_module._CURSOR_ORDER_POLICY_V1
    monkeypatch.setattr(
        inspect_module,
        "_CURSOR_ORDER_POLICY_V1",
        "canonical_inspection_result_order_v2",
    )
    with pytest.raises(InspectError) as policy_error:
        inspect_document(
            path,
            "literal_search",
            cursor=cursor,
            **kwargs,
        )
    assert policy_error.value.code == "cursor_mismatch"

    monkeypatch.setattr(
        inspect_module,
        "_CURSOR_ORDER_POLICY_V1",
        original_order_policy,
    )
    original_literal_matches = inspect_module._literal_matches

    def reversed_literal_matches(*args, **inner_kwargs):
        return list(reversed(original_literal_matches(*args, **inner_kwargs)))

    monkeypatch.setattr(
        inspect_module,
        "_literal_matches",
        reversed_literal_matches,
    )
    with pytest.raises(InspectError) as result_set_error:
        inspect_document(
            path,
            "literal_search",
            cursor=cursor,
            **kwargs,
        )
    assert result_set_error.value.code == "cursor_mismatch"


def test_browse_and_paragraph_read_resolve_exact_reference(demo_dir: Path) -> None:
    path = _round(demo_dir, 1)
    browse = inspect_document(path, "browse", max_items=1)
    paragraph = browse["paragraphs"][0]
    ref = paragraph["paragraph_ref"]

    read = inspect_document(
        path,
        "read",
        selection={"paragraph_ref": ref},
    )
    assert read["selection_kind"] == "paragraph"
    assert read["paragraphs"] == [paragraph]
    assert read["coverage"]["eligible_item_count"] == 1
    assert read["next_cursor"] is None

    with pytest.raises(InspectError) as error:
        inspect_document(
            path,
            "read",
            selection={
                "paragraph_ref": {
                    **ref,
                    "paragraph_text_sha256": "0" * 64,
                }
            },
        )
    assert error.value.code == "reference_mismatch"

    with pytest.raises(InspectError) as error:
        inspect_document(
            _round(demo_dir, 2),
            "read",
            selection={"paragraph_ref": ref},
        )
    assert error.value.code == "file_sha256_mismatch"
    assert error.value.metadata["claimed_source_sha256"] == ref["file_sha256"]

    with pytest.raises(InspectError) as error:
        inspect_document(
            path,
            "read",
            selection={"paragraph_ref": ref},
            cursor="c1:0:" + "0" * 64,
        )
    assert error.value.code in {"cursor_mismatch", "invalid_cursor"}


def test_section_read_is_paginated_and_bound_to_heading(demo_dir: Path) -> None:
    path = _round(demo_dir, 1)
    outline = inspect_document(path, "outline")
    section = next(item for item in outline["sections"] if item["label"] == "14.2")
    selection = {"section_ref": section["section_ref"]}

    first = inspect_document(path, "read", selection=selection, max_items=1)
    assert first["selection_kind"] == "section"
    assert first["section_navigation"]["label"] == "14.2"
    assert first["coverage"]["eligible_item_count"] >= 2
    assert len(first["paragraphs"]) == 1
    assert first["next_cursor"] is not None

    second = inspect_document(
        path,
        "read",
        selection=selection,
        cursor=first["next_cursor"],
        max_items=10,
    )
    combined = first["paragraphs"] + second["paragraphs"]
    assert len(combined) == first["coverage"]["eligible_item_count"]
    assert all(
        section["start_paragraph_index"]
        <= item["paragraph_ref"]["paragraph_index"]
        < section["end_paragraph_index_exclusive"]
        for item in combined
    )
    assert any("total aggregate liability" in item["text"] for item in combined)
    assert second["next_cursor"] is None


def test_tracked_text_warning_is_present_at_document_and_match_level(
    demo_dir: Path,
) -> None:
    result = inspect_document(
        _round(demo_dir, 2),
        "literal_search",
        phrases=["USD 50,000"],
        match_basis="exact_literal",
    )

    assert result["has_tracked_text_revisions"] is True
    assert result["revision_inventory"]["tracked_text_revision_elements"] > 0
    assert result["revision_inventory"]["partition_valid"] is True
    assert len(result["matches"]) == 1
    assert result["matches"][0]["has_tracked_text_revisions"] is True
    assert result["matches"][0]["section_navigation"]["label"] == "14.2"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"mode": "unknown"}, "invalid_mode"),
        ({"mode": "outline", "phrases": ["x"]}, "invalid_request"),
        ({"mode": "literal_search"}, "phrases_missing"),
        (
            {
                "mode": "literal_search",
                "phrases": ["x"],
                "match_basis": "unknown",
            },
            "match_basis_missing",
        ),
        ({"mode": "read"}, "selection_missing"),
        (
            {
                "mode": "read",
                "selection": {"paragraph_ref": {}, "section_ref": {}},
            },
            "invalid_selection",
        ),
        ({"mode": "browse", "max_items": 0}, "invalid_limit"),
    ],
)
def test_mode_specific_inputs_fail_closed(
    demo_dir: Path,
    kwargs: dict,
    code: str,
) -> None:
    with pytest.raises(InspectError) as error:
        inspect_document(_round(demo_dir, 1), **kwargs)
    assert error.value.code == code


def test_max_items_rejects_adversarial_int_subclass(demo_dir: Path) -> None:
    class Bypass(int):
        def __ge__(self, _other: object) -> bool:
            return True

        def __le__(self, _other: object) -> bool:
            return True

        def __gt__(self, _other: object) -> bool:
            return True

    with pytest.raises(InspectError) as error:
        inspect_document(_round(demo_dir, 1), "browse", max_items=Bypass(1))

    assert error.value.code == "invalid_limit"
    assert error.value.detail == "max_items must be an integer"


def test_literal_search_rejects_surrogate_code_points(demo_dir: Path) -> None:
    with pytest.raises(InspectError) as error:
        inspect_document(
            _round(demo_dir, 1),
            "literal_search",
            phrases=["bad\ud800phrase"],
            match_basis="exact_literal",
        )
    assert error.value.code == "invalid_phrase"


def test_index_caps_refuse_instead_of_returning_partial_results(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inspect_module, "MAX_INDEXED_PARAGRAPHS", 1)

    with pytest.raises(ResourceLimitError) as error:
        inspect_document(_round(demo_dir, 1), "outline")
    assert error.value.limit == "inspect_paragraph_count"
    assert len(error.value.metadata["observed_source_sha256"]) == 64


def test_accepted_current_composes_revision_wrappers_and_text_atoms(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "accepted-current-composite.docx"

    def add_composite(document: etree._Element) -> None:
        paragraph = _paragraph()
        _run(paragraph, "plain")
        _revision(paragraph, "ins", "901", " INS")
        _revision(paragraph, "del", "902", " DELETE")
        _revision(paragraph, "moveTo", "903", " MOVE-TO")
        _revision(paragraph, "moveFrom", "904", " MOVE-FROM")

        atom_run = etree.SubElement(paragraph, w("r"))
        etree.SubElement(atom_run, w("tab"))
        text = etree.SubElement(atom_run, w("t"))
        text.text = "TAB"
        etree.SubElement(atom_run, w("br"))
        text = etree.SubElement(atom_run, w("t"))
        text.text = "BREAK"
        etree.SubElement(atom_run, w("cr"))
        text = etree.SubElement(atom_run, w("t"))
        text.text = "CR"
        etree.SubElement(atom_run, w("noBreakHyphen"))
        text = etree.SubElement(atom_run, w("t"))
        text.text = "HYPHEN"

        # A visible counter is mechanically the same accepted-current pattern:
        # preserve the deletion wrapper and include the adjacent insertion.
        _revision(paragraph, "del", "905", " OLD")
        _revision(paragraph, "ins", "906", " COUNTER")
        _replace_body(document, [paragraph])

    _rewrite_document(source, target, add_composite)
    browsed = inspect_document(str(target), "browse")

    assert browsed["has_tracked_text_revisions"] is True
    assert browsed["coverage"]["indexed_paragraph_count"] == 1
    assert browsed["coverage"]["nonempty_indexed_paragraph_count"] == 1
    assert browsed["paragraphs"] == [
        {
            **browsed["paragraphs"][0],
            "has_tracked_text_revisions": True,
            "text": "plain INS MOVE-TO\tTAB\nBREAK\nCR-HYPHEN COUNTER",
        }
    ]
    assert browsed["revision_inventory"]["tracked_text_revision_elements"] == 4
    assert browsed["revision_inventory"]["unsupported_by_kind"] == {
        "moveTo": 1,
        "moveFrom": 1,
    }


def test_outer_coverage_counts_all_indexed_body_and_table_paragraphs(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "body-and-table-coverage.docx"

    def add_body_and_table(document: etree._Element) -> None:
        table = etree.Element(w("tbl"))
        row = etree.SubElement(table, w("tr"))
        cell = etree.SubElement(row, w("tc"))
        cell.append(_paragraph("Cell paragraph."))
        cell.append(_paragraph())
        _replace_body(document, [_paragraph("Body paragraph."), table])

    _rewrite_document(source, target, add_body_and_table)
    browsed = inspect_document(str(target), "browse")
    coverage = browsed["coverage"]
    containers = coverage["container_coverage"]

    assert coverage["indexed_paragraph_count"] == 3
    assert coverage["nonempty_indexed_paragraph_count"] == 2
    assert "body_paragraph_count" not in coverage
    assert "nonempty_body_paragraph_count" not in coverage
    assert containers["indexed_paragraph_count"] == 3
    assert containers["body_paragraph_count"] == 1
    assert containers["table_cell_paragraph_count"] == 2
    assert containers["indexed_paragraph_count"] == (
        containers["body_paragraph_count"] + containers["table_cell_paragraph_count"]
    )


def test_move_from_and_move_to_each_raise_the_tracked_text_warning(
    demo_dir: Path,
    tmp_path: Path,
    kind: str = "moveFrom",
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"

    for index, revision_kind in enumerate((kind, "moveTo"), start=1):
        target = tmp_path / f"{revision_kind}-warning.docx"

        def add_move(document: etree._Element, revision_kind=revision_kind) -> None:
            paragraph = _paragraph("visible")
            _revision(paragraph, revision_kind, f"91{index}", " moved")
            _replace_body(document, [paragraph])

        _rewrite_document(source, target, add_move)
        result = inspect_document(str(target), "browse")

        assert result["has_tracked_text_revisions"] is True
        assert result["paragraphs"][0]["has_tracked_text_revisions"] is True
        expected = "visible" if revision_kind == "moveFrom" else "visible moved"
        assert result["paragraphs"][0]["text"] == expected


def test_duplicate_heading_labels_keep_distinct_positional_sections_and_empty_gap(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "duplicate-headings.docx"

    def add_duplicate_headings(document: etree._Element) -> None:
        _replace_body(
            document,
            [
                _paragraph("14.2 Repeated Heading", outline_level=0),
                _paragraph(),
                _paragraph("First complete paragraph."),
                _paragraph("14.2 Repeated Heading", outline_level=0),
                _paragraph("Second complete paragraph."),
            ],
        )

    _rewrite_document(source, target, add_duplicate_headings)
    outline = inspect_document(str(target), "outline")
    sections = [
        section
        for section in outline["sections"]
        if section["label"] == "14.2" and section["heading"] == "Repeated Heading"
    ]

    assert len(sections) == 2
    assert [item["start_paragraph_index"] for item in sections] == [0, 3]
    assert [item["end_paragraph_index_exclusive"] for item in sections] == [3, 5]
    assert (
        sections[0]["section_ref"]["heading_text_sha256"]
        == sections[1]["section_ref"]["heading_text_sha256"]
    )
    assert sections[0]["section_ref"] != sections[1]["section_ref"]
    assert all(
        set(section).isdisjoint({"text", "preview", "snippet", "matched_text"})
        for section in sections
    )

    first = inspect_document(
        str(target),
        "read",
        selection={"section_ref": sections[0]["section_ref"]},
    )
    second = inspect_document(
        str(target),
        "read",
        selection={"section_ref": sections[1]["section_ref"]},
    )
    assert [item["text"] for item in first["paragraphs"]] == [
        "14.2 Repeated Heading",
        "First complete paragraph.",
    ]
    assert [item["text"] for item in second["paragraphs"]] == [
        "14.2 Repeated Heading",
        "Second complete paragraph.",
    ]

    # Empty paragraphs stay in positional identity even though browse and
    # section reads omit them. A correctly hash-bound empty ref still resolves.
    empty_ref = {
        "schema_version": "paragraph_ref.v1",
        "ref_type": "paragraph",
        "file_sha256": outline["file_sha256"],
        "part_name": "word/document.xml",
        "paragraph_index": 1,
        "paragraph_text_sha256": hashlib.sha256(b"").hexdigest(),
        "reading_mode": "accepted_current_v1",
        "container_policy": "canonical_body_flow_v1",
    }
    empty = inspect_document(
        str(target),
        "read",
        selection={"paragraph_ref": empty_ref},
    )
    assert empty["paragraphs"][0]["text"] == ""
    assert empty["paragraphs"][0]["paragraph_ref"] == empty_ref


def test_literal_search_returns_overlaps_duplicates_and_all_five_candidates(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "literal-search-completeness.docx"

    def add_search_text(document: etree._Element) -> None:
        _replace_body(
            document,
            [
                *[_paragraph(f"needle occurrence {index}") for index in range(5)],
                _paragraph("duplicate phrase"),
                _paragraph("aaaa"),
            ],
        )

    _rewrite_document(source, target, add_search_text)

    five = inspect_document(
        str(target),
        "literal_search",
        phrases=["needle"],
        match_basis="exact_literal",
        max_items=100,
    )
    assert len(five["matches"]) == 5
    assert [item["paragraph_ref"]["paragraph_index"] for item in five["matches"]] == [
        0,
        1,
        2,
        3,
        4,
    ]
    assert [item["occurrence_count"] for item in five["matches"]] == [1] * 5
    assert five["coverage"]["complete_literal_match_count"] == 5
    assert five["next_cursor"] is None

    duplicate = inspect_document(
        str(target),
        "literal_search",
        phrases=["duplicate", "duplicate"],
        match_basis="exact_literal",
    )
    assert [item["phrase_index"] for item in duplicate["matches"]] == [0, 1]
    assert (
        duplicate["matches"][0]["paragraph_ref"]
        == duplicate["matches"][1]["paragraph_ref"]
    )

    overlapping = inspect_document(
        str(target),
        "literal_search",
        phrases=["aa"],
        match_basis="exact_literal",
    )
    assert len(overlapping["matches"]) == 1
    assert overlapping["matches"][0]["occurrence_count"] == 3
    assert overlapping["matches"][0]["snippet"]["match_start"] == 0
    assert overlapping["matches"][0]["snippet"]["match_end"] == 2

    zero = inspect_document(
        str(target),
        "literal_search",
        phrases=["absent phrase"],
        match_basis="exact_literal",
    )
    assert zero["matches"] == []
    assert zero["coverage"]["scan_complete"] is True
    assert zero["coverage"]["eligible_item_count"] == 0
    assert zero["coverage"]["returned_item_count"] == 0
    assert zero["coverage"]["complete_literal_match_count"] == 0
    assert zero["coverage"]["output_truncated"] is False
    assert zero["next_cursor"] is None


def test_section_refs_fail_closed_when_stale_or_tampered(demo_dir: Path) -> None:
    path = _round(demo_dir, 1)
    outline = inspect_document(path, "outline")
    section_ref = next(
        section["section_ref"]
        for section in outline["sections"]
        if section["label"] == "14.2"
    )

    tampered_refs = [
        ({**section_ref, "heading_text_sha256": "0" * 64}, "reference_mismatch"),
        (
            {
                **section_ref,
                "heading_paragraph_index": section_ref["heading_paragraph_index"] + 1,
            },
            "reference_not_found",
        ),
        ({**section_ref, "reading_mode": "unknown"}, "reference_mismatch"),
        ({**section_ref, "container_policy": "unknown"}, "reference_mismatch"),
        ({**section_ref, "unexpected": "field"}, "invalid_reference"),
    ]
    for tampered, code in tampered_refs:
        with pytest.raises(InspectError) as error:
            inspect_document(
                path,
                "read",
                selection={"section_ref": tampered},
            )
        assert error.value.code == code

    with pytest.raises(InspectError) as stale:
        inspect_document(
            _round(demo_dir, 2),
            "read",
            selection={"section_ref": section_ref},
        )
    assert stale.value.code == "file_sha256_mismatch"
    assert stale.value.metadata["claimed_source_sha256"] == section_ref["file_sha256"]


def test_text_and_index_caps_succeed_at_boundary_then_fail_above(
    demo_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "inspection-boundaries.docx"

    def add_boundary_text(document: etree._Element) -> None:
        _replace_body(
            document,
            [_paragraph("12345"), _paragraph("abcde"), _paragraph("Z")],
        )

    _rewrite_document(source, target, add_boundary_text)
    monkeypatch.setattr(inspect_module, "MAX_INDEXED_PARAGRAPHS", 3)
    monkeypatch.setattr(inspect_module, "MAX_AGGREGATE_TEXT_CHARS", 11)
    monkeypatch.setattr(inspect_module, "MAX_PARAGRAPH_TEXT_CHARS", 5)
    monkeypatch.setattr(inspect_module, "MAX_RETURNED_TEXT_CHARS", 10)

    first = inspect_document(str(target), "browse", max_items=3)
    assert [item["text"] for item in first["paragraphs"]] == ["12345", "abcde"]
    assert first["coverage"]["returned_item_count"] == 2
    assert first["coverage"]["output_truncated"] is True
    assert first["next_cursor"] is not None
    second = inspect_document(
        str(target),
        "browse",
        cursor=first["next_cursor"],
        max_items=3,
    )
    assert [item["text"] for item in second["paragraphs"]] == ["Z"]
    assert second["coverage"]["cursor_offset"] == 2
    assert second["next_cursor"] is None

    monkeypatch.setattr(inspect_module, "MAX_AGGREGATE_TEXT_CHARS", 10)
    with pytest.raises(ResourceLimitError) as aggregate_error:
        inspect_document(str(target), "outline")
    assert aggregate_error.value.limit == "inspect_aggregate_text_chars"

    monkeypatch.setattr(inspect_module, "MAX_AGGREGATE_TEXT_CHARS", 11)
    monkeypatch.setattr(inspect_module, "MAX_INDEXED_PARAGRAPHS", 2)
    with pytest.raises(ResourceLimitError) as paragraph_count_error:
        inspect_document(str(target), "outline")
    assert paragraph_count_error.value.limit == "inspect_paragraph_count"

    monkeypatch.setattr(inspect_module, "MAX_INDEXED_PARAGRAPHS", 3)
    monkeypatch.setattr(inspect_module, "MAX_PARAGRAPH_TEXT_CHARS", 4)
    with pytest.raises(InspectError) as paragraph_text_error:
        inspect_document(str(target), "browse")
    assert paragraph_text_error.value.code == "resource_limit_exceeded"
    assert paragraph_text_error.value.metadata["limit"] == "paragraph_text_chars"

    monkeypatch.setattr(inspect_module, "MAX_PARAGRAPH_TEXT_CHARS", 10)
    monkeypatch.setattr(inspect_module, "MAX_RETURNED_TEXT_CHARS", 4)
    with pytest.raises(InspectError) as response_text_error:
        inspect_document(str(target), "browse")
    assert response_text_error.value.code == "resource_limit_exceeded"
    assert response_text_error.value.metadata["limit"] == "returned_text_chars"

    monkeypatch.setattr(inspect_module, "MAX_RETURNED_TEXT_CHARS", 10)
    monkeypatch.setattr(inspect_module, "MAX_ITEMS", 3)
    assert (
        inspect_document(str(target), "browse", max_items=3)["limits"][
            "requested_max_items"
        ]
        == 3
    )
    with pytest.raises(InspectError) as max_items_error:
        inspect_document(str(target), "browse", max_items=4)
    assert max_items_error.value.code == "invalid_limit"


def test_response_text_cap_counts_outline_and_repeated_navigation_text(
    demo_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "navigation-text-limits.docx"

    def add_section(document: etree._Element) -> None:
        _replace_body(
            document,
            [
                _paragraph("1 LongHeading", outline_level=0),
                _paragraph("A"),
                _paragraph("B"),
            ],
        )

    _rewrite_document(source, target, add_section)
    monkeypatch.setattr(inspect_module, "MAX_PARAGRAPH_TEXT_CHARS", 100)
    monkeypatch.setattr(inspect_module, "MAX_RETURNED_TEXT_CHARS", 12)
    assert len(inspect_document(str(target), "outline")["sections"]) == 1

    monkeypatch.setattr(inspect_module, "MAX_RETURNED_TEXT_CHARS", 11)
    with pytest.raises(InspectError) as outline_error:
        inspect_document(str(target), "outline")
    assert outline_error.value.metadata["limit"] == "returned_text_chars"

    # The first browse item returns its complete paragraph plus the same
    # heading and label as navigation facts: 13 + 11 + 1 characters.
    monkeypatch.setattr(inspect_module, "MAX_RETURNED_TEXT_CHARS", 25)
    first = inspect_document(str(target), "browse", max_items=3)
    assert [item["text"] for item in first["paragraphs"]] == ["1 LongHeading"]
    assert first["coverage"]["output_truncated"] is True
    assert first["next_cursor"] is not None


def test_literal_caps_succeed_at_boundary_then_fail_above(
    demo_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    target = tmp_path / "literal-boundaries.docx"

    def add_literal_text(document: etree._Element) -> None:
        _replace_body(
            document,
            [
                *[_paragraph(f"needle {index}") for index in range(5)],
                _paragraph("aaaa"),
            ],
        )

    _rewrite_document(source, target, add_literal_text)
    monkeypatch.setattr(inspect_module, "MAX_PHRASES", 2)
    monkeypatch.setattr(inspect_module, "MAX_PHRASE_CHARS", 3)
    monkeypatch.setattr(inspect_module, "MAX_TOTAL_PHRASE_CHARS", 6)
    monkeypatch.setattr(inspect_module, "MAX_LITERAL_MATCH_CANDIDATES", 5)
    monkeypatch.setattr(inspect_module, "MAX_LITERAL_OCCURRENCES_PER_CANDIDATE", 3)

    boundary = inspect_document(
        str(target),
        "literal_search",
        phrases=["aa", "zz"],
        match_basis="exact_literal",
    )
    assert boundary["matches"][0]["occurrence_count"] == 3

    phrase_length_boundary = inspect_document(
        str(target),
        "literal_search",
        phrases=["abc"],
        match_basis="exact_literal",
    )
    assert phrase_length_boundary["matches"] == []
    with pytest.raises(InspectError) as phrase_length_error:
        inspect_document(
            str(target),
            "literal_search",
            phrases=["abcd"],
            match_basis="exact_literal",
        )
    assert phrase_length_error.value.code == "resource_limit_exceeded"
    assert phrase_length_error.value.metadata["limit"] == "phrase_chars"

    aggregate_boundary = inspect_document(
        str(target),
        "literal_search",
        phrases=["abc", "def"],
        match_basis="exact_literal",
    )
    assert aggregate_boundary["matches"] == []
    monkeypatch.setattr(inspect_module, "MAX_TOTAL_PHRASE_CHARS", 5)
    with pytest.raises(InspectError) as aggregate_error:
        inspect_document(
            str(target),
            "literal_search",
            phrases=["abc", "def"],
            match_basis="exact_literal",
        )
    assert aggregate_error.value.metadata["limit"] == "total_phrase_chars"

    monkeypatch.setattr(inspect_module, "MAX_TOTAL_PHRASE_CHARS", 6)
    with pytest.raises(InspectError) as phrase_count_error:
        inspect_document(
            str(target),
            "literal_search",
            phrases=["a", "b", "c"],
            match_basis="exact_literal",
        )
    assert phrase_count_error.value.metadata["limit"] == "phrase_count"

    candidates = inspect_document(
        str(target),
        "literal_search",
        phrases=["nee"],
        match_basis="exact_literal",
    )
    assert len(candidates["matches"]) == 5
    monkeypatch.setattr(inspect_module, "MAX_LITERAL_MATCH_CANDIDATES", 4)
    with pytest.raises(InspectError) as candidates_error:
        inspect_document(
            str(target),
            "literal_search",
            phrases=["nee"],
            match_basis="exact_literal",
        )
    assert candidates_error.value.metadata["limit"] == "literal_match_candidates"

    monkeypatch.setattr(inspect_module, "MAX_LITERAL_MATCH_CANDIDATES", 5)
    monkeypatch.setattr(inspect_module, "MAX_LITERAL_OCCURRENCES_PER_CANDIDATE", 2)
    with pytest.raises(InspectError) as occurrence_error:
        inspect_document(
            str(target),
            "literal_search",
            phrases=["aa"],
            match_basis="exact_literal",
        )
    assert occurrence_error.value.metadata["limit"] == (
        "literal_occurrences_per_candidate"
    )


def test_transport_cancellation_is_not_converted_to_partial_success(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cancel(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(inspect_module, "_literal_matches", cancel)
    with pytest.raises(asyncio.CancelledError):
        inspect_document(
            _round(demo_dir, 1),
            "literal_search",
            phrases=["liability"],
            match_basis="exact_literal",
        )


def test_english_clause_9_and_14_2_discovery_read_verify_flow(
    demo_dir: Path,
) -> None:
    path = _round(demo_dir, 1)
    phrases = [
        "Each Party shall keep the other Party's Confidential Information strictly confidential",
        "total aggregate liability under this Agreement shall not exceed",
    ]
    expected_clauses = [
        "9.1 Confidentiality Obligations",
        "14.2 Limitation of Liability",
    ]

    discovery = inspect_document(
        path,
        "literal_search",
        phrases=phrases,
        match_basis="exact_literal",
        max_items=10,
    )
    assert len(discovery["matches"]) == 2
    assert [item["phrase_index"] for item in discovery["matches"]] == [0, 1]
    assert discovery["coverage"]["scan_complete"] is True
    assert discovery["coverage"]["included_parts"] == ["word/document.xml"]
    assert discovery["coverage"]["excluded_parts"] == [
        "word/header*.xml",
        "word/footer*.xml",
        "word/footnotes.xml",
        "word/endnotes.xml",
        "word/comments*.xml",
    ]

    for match, expected_clause in zip(
        discovery["matches"], expected_clauses, strict=True
    ):
        paragraph_ref = match["paragraph_ref"]
        read = inspect_document(
            path,
            "read",
            selection={"paragraph_ref": paragraph_ref},
        )
        assert read["selection_kind"] == "paragraph"
        assert len(read["paragraphs"]) == 1
        paragraph = read["paragraphs"][0]
        assert paragraph["paragraph_ref"] == paragraph_ref
        assert paragraph["section_navigation"]["label"] in {"9.1", "14.2"}

        verified = verify_quote(path, paragraph_ref, paragraph["text"])
        assert verified["verdict"] == "exact"
        assert verified["checked_anchor"] == paragraph_ref
        assert verified["matches"][0]["side"] == "paragraph_current"
        assert verified["matches"][0]["clause"] == expected_clause
