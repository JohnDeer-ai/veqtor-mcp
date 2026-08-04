# SPDX-License-Identifier: Apache-2.0
"""Acceptance coverage for the bounded internal History I/O envelope."""

from __future__ import annotations

import io
import hashlib
import os
import shutil
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from lxml import etree

from veqtor_docx import _projection as projection_module
from veqtor_docx import inspect as inspect_module
from veqtor_docx._ooxml import canonical_body_flow_v1, parse_xml, w
from veqtor_docx._projection import (
    build_paragraph_projection_coverage_v1,
    build_paragraph_projection_v1,
)
from veqtor_docx.inspect import _load_snapshot_from_payload, _paragraph_ref
from veqtor_docx.synthetic import CAP_R4
from veqtor_mcp import _history_io as history_io
from veqtor_mcp import _history_resolution as resolution
from veqtor_mcp import round_map as round_map_module
from veqtor_mcp._history_io import HistoryIOError, build_paragraph_history


def _seed(path: Path, *, text: str = CAP_R4) -> dict:
    snapshot = _load_snapshot_from_payload(path.read_bytes(), path=str(path))
    paragraph = next(item for item in snapshot.paragraphs if text in item.text)
    return {
        "schema_version": "paragraph_history_seed.v1",
        "path": str(path),
        "paragraph_ref": _paragraph_ref(snapshot, paragraph),
    }


def _lexicographic_order() -> dict:
    return {
        "schema_version": "paragraph_history_order.v1",
        "kind": "filename_lexicographic_v1",
    }


def _explicit_order(filenames: list[str]) -> dict:
    return {
        "schema_version": "paragraph_history_order.v1",
        "kind": "explicit_filename_sequence_v1",
        "ordered_filenames": filenames,
    }


def _copy_demo(demo_dir: Path, target: Path) -> Path:
    target.mkdir()
    for source in demo_dir.glob("*.docx"):
        shutil.copyfile(source, target / source.name)
    return target


def _mutate_valid_docx(path: Path) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(path.read_bytes()), "r") as source:
        with zipfile.ZipFile(output, "w") as target:
            for info in source.infolist():
                payload = source.read(info)
                if info.filename == "docProps/core.xml":
                    payload = payload.replace(b"2026-", b"2027-", 1)
                target.writestr(info, payload)
    path.write_bytes(output.getvalue())


def _expanded_docx_bytes(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        return sum(info.file_size for info in archive.infolist())


def _replace_document_body(path: Path, build_body) -> None:
    replacement = path.with_suffix(".replacement")
    with (
        zipfile.ZipFile(path) as source,
        zipfile.ZipFile(replacement, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "word/document.xml":
                document = parse_xml(payload)
                body = next(child for child in document if child.tag == w("body"))
                for child in list(body):
                    body.remove(child)
                build_body(body)
                payload = etree.tostring(
                    document,
                    xml_declaration=True,
                    encoding="UTF-8",
                )
            target.writestr(info, payload)
    os.replace(replacement, path)


def _text_paragraph(parent: etree._Element, text: str) -> etree._Element:
    paragraph = etree.SubElement(parent, w("p"))
    _text_run(paragraph, text)
    return paragraph


def _text_run(parent: etree._Element, text: str) -> etree._Element:
    run = etree.SubElement(parent, w("r"))
    atom = etree.SubElement(run, w("t"))
    atom.text = text
    return run


def _history_pair_with_candidates(
    matter: Path,
    template: Path,
    candidate_count: int,
) -> tuple[Path, Path]:
    matter.mkdir()
    lower = matter / "01-lower.docx"
    higher = matter / "02-higher.docx"
    shutil.copyfile(template, lower)
    shutil.copyfile(template, higher)

    def lower_body(body: etree._Element) -> None:
        for _ in range(candidate_count):
            _text_paragraph(body, "L")

    def higher_body(body: etree._Element) -> None:
        paragraph = etree.SubElement(body, w("p"))
        deletion = etree.SubElement(paragraph, w("del"))
        deletion.set(w("id"), "1")
        deletion.set(w("author"), "A")
        deleted_run = etree.SubElement(deletion, w("r"))
        deleted = etree.SubElement(deleted_run, w("delText"))
        deleted.text = "L"
        insertion = etree.SubElement(paragraph, w("ins"))
        insertion.set(w("id"), "2")
        insertion.set(w("author"), "B")
        _text_run(insertion, "H")

    _replace_document_body(lower, lower_body)
    _replace_document_body(higher, higher_body)
    return lower, higher


def _write_counter_docx(
    path: Path,
    template: Path,
    *,
    outer_id: str,
    counter_id: str,
) -> None:
    if not path.exists():
        shutil.copyfile(template, path)

    def body_builder(body: etree._Element) -> None:
        paragraph = etree.SubElement(body, w("p"))
        _text_run(paragraph, "Seed ")
        insertion = etree.SubElement(paragraph, w("ins"))
        insertion.set(w("id"), outer_id)
        insertion.set(w("author"), "A")
        _text_run(insertion, "Proposal ")
        counter = etree.SubElement(insertion, w("del"))
        counter.set(w("id"), counter_id)
        counter.set(w("author"), "B")
        deleted_run = etree.SubElement(counter, w("r"))
        deleted = etree.SubElement(deleted_run, w("delText"))
        deleted.text = "struck"

    _replace_document_body(path, body_builder)


def _write_nested_revision_docx(
    path: Path,
    template: Path,
    *,
    depth: int,
) -> None:
    shutil.copyfile(template, path)

    def body_builder(body: etree._Element) -> None:
        paragraph = etree.SubElement(body, w("p"))
        _text_run(paragraph, "Seed ")
        parent = paragraph
        for index in range(depth):
            wrapper = etree.SubElement(parent, w("ins"))
            wrapper.set(w("id"), str(index + 1))
            wrapper.set(w("author"), "A")
            parent = wrapper
        _text_run(parent, "Nested")

    _replace_document_body(path, body_builder)


def _write_combined_maxima_docx(path: Path, template: Path) -> None:
    shutil.copyfile(template, path)

    def body_builder(body: etree._Element) -> None:
        for _ in range(20):
            heading = etree.SubElement(body, w("p"))
            properties = etree.SubElement(heading, w("pPr"))
            outline = etree.SubElement(properties, w("outlineLvl"))
            outline.set(w("val"), "0")
            _text_run(heading, "Match")

        selected = etree.SubElement(body, w("p"))
        _text_run(selected, "Current")
        for index in range(20):
            deletion = etree.SubElement(selected, w("del"))
            deletion.set(w("id"), str(index + 1))
            deletion.set(w("author"), "A")
            deleted_run = etree.SubElement(deletion, w("r"))
            deleted_text = etree.SubElement(deleted_run, w("delText"))
            deleted_text.text = "D"
            _text_run(selected, "|")

    _replace_document_body(path, body_builder)


def test_liability_history_is_closed_hash_bound_and_uses_selected_change_units(
    demo_dir: Path,
) -> None:
    files = sorted(demo_dir.glob("*.docx"), key=lambda path: (path.name.casefold(), path.name))
    result = build_paragraph_history(
        str(demo_dir),
        _seed(files[-1]),
        _lexicographic_order(),
        max_items=100,
    ).result

    assert set(result) == {
        "schema_version",
        "status",
        "seed",
        "ordering_source",
        "order_basis",
        "result_order",
        "snapshot",
        "observations",
        "coverage",
        "limits",
        "next_cursor",
    }
    assert "producer" not in result
    assert "record_id" not in result
    assert "record_status" not in result
    assert result["schema_version"] == "paragraph_history.v1"
    assert result["coverage"]["relationship_counts"] == {
        "exact_content_equality": 0,
        "rejected_projection_equality": 3,
    }
    assert [
        observation["resolution"]["reason"]
        for observation in result["observations"][1:]
    ] == ["rejected_projection_unique"] * 3
    assert all(
        observation["selected_paragraph"]["support_to_higher"][0][
            "relationship_type"
        ]
        == "rejected_projection_equality"
        for observation in result["observations"][1:]
    )

    r4 = result["observations"][0]["selected_paragraph"]
    assert r4["metadata_assurance"] == {
        "author_metadata_interpretation": "unverified_document_string",
        "date_metadata_interpretation": "unverified_document_string",
        "authorship_verified": False,
        "time_verified": False,
    }
    deletion = next(unit for unit in r4["change_units"] if unit["change_type"] == "delete")
    assert deletion["author"] == "53"
    assert "willful misconduct" in deletion["old_text"]
    assert "path" not in deletion["reference"]
    assert {
        unit["reference"]["paragraph_index"] for unit in r4["change_units"]
    } == {r4["paragraph_ref"]["paragraph_index"]}
    assert r4["change_units_sha256"] == history_io._digest(
        {
            "schema_version": "paragraph_history_change_units.v1",
            "change_units": r4["change_units"],
        }
    )


def test_both_order_modes_produce_the_same_declared_trace(demo_dir: Path) -> None:
    files = sorted(demo_dir.glob("*.docx"), key=lambda path: (path.name.casefold(), path.name))
    seed = _seed(files[-1])
    lexicographic = build_paragraph_history(
        str(demo_dir), seed, _lexicographic_order(), max_items=100
    ).result
    explicit = build_paragraph_history(
        str(demo_dir),
        seed,
        _explicit_order([path.name for path in files]),
        max_items=100,
    ).result

    assert lexicographic["observations"] == explicit["observations"]
    assert (
        lexicographic["snapshot"]["full_result_set_sha256"]
        == explicit["snapshot"]["full_result_set_sha256"]
    )
    assert (
        lexicographic["snapshot"]["order_binding_sha256"]
        != explicit["snapshot"]["order_binding_sha256"]
    )


def test_candidate_streaming_digest_matches_canonical_json_v1() -> None:
    candidates = [
        {"paragraph_observation_id": "ph_par_obs_v1:" + "1" * 64, "value": 1},
        {"paragraph_observation_id": "ph_par_obs_v1:" + "2" * 64, "value": 2},
    ]
    assert history_io._candidate_set_digest(candidates) == history_io._digest(
        {
            "schema_version": "paragraph_history_candidates.v1",
            "candidates": candidates,
        }
    )


def test_result_set_streaming_digest_matches_canonical_json_v1() -> None:
    observations = [
        {"position": 1, "text": "Ответ"},
        {"position": 0, "text": "東京 😀"},
    ]
    assert history_io._result_set_digest(observations) == history_io._digest(
        {
            "schema_version": "paragraph_history_result_set.v1",
            "result_order": "seed_then_descending_position_v1",
            "observations": observations,
        }
    )


def test_combined_maxima_result_is_digestible_and_stably_pageable(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = tmp_path / "combined-maxima-source.docx"
    _write_combined_maxima_docx(
        source,
        demo_dir / "round-1-outgoing-draft.docx",
    )
    payload = source.read_bytes()
    matter = tmp_path / "combined-maxima"
    matter.mkdir()
    paths = []
    for index in range(500):
        path = matter / f"{index:03d}.docx"
        path.write_bytes(payload)
        paths.append(path)

    seed = _seed(paths[-1], text="Current")
    cursor = None
    positions: list[int] = []
    complete_observations: list[dict] = []
    observation_ids: set[str] = set()
    full_digests: set[str] = set()
    selected_change_units = 0
    page_count = 0
    while True:
        result = build_paragraph_history(
            str(matter),
            seed,
            _lexicographic_order(),
            cursor=cursor,
            max_items=100,
        ).result
        assert result["status"] == "ok"
        assert result["coverage"]["eligible_observation_count"] == 500
        assert result["coverage"]["selected_paragraph_count"] == 500
        assert result["coverage"]["navigation_candidate_count"] == 9_980
        assert result["coverage"]["relationship_counts"] == {
            "exact_content_equality": 499,
            "rejected_projection_equality": 0,
        }
        full_digests.add(result["snapshot"]["full_result_set_sha256"])
        complete_observations.extend(result["observations"])
        for observation in result["observations"]:
            positions.append(observation["position"])
            observation_ids.add(observation["observation_id"])
            selected_change_units += len(
                observation["selected_paragraph"]["change_units"]
            )
        page_count += 1
        cursor = result["next_cursor"]
        if cursor is None:
            break
        assert page_count < 500

    assert positions == list(range(499, -1, -1))
    assert len(observation_ids) == 500
    assert selected_change_units == 10_000
    assert len(full_digests) == 1
    with pytest.raises(ValueError, match="maximum node count"):
        history_io._digest(
            {
                "schema_version": "paragraph_history_result_set.v1",
                "result_order": "seed_then_descending_position_v1",
                "observations": complete_observations,
            }
        )


def test_ten_thousand_positive_candidates_succeed_and_plus_one_refuses(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    template = demo_dir / "round-1-outgoing-draft.docx"
    lower, higher = _history_pair_with_candidates(
        tmp_path / "boundary",
        template,
        10_000,
    )
    result = build_paragraph_history(
        str(lower.parent),
        _seed(higher, text="H"),
        _lexicographic_order(),
        max_items=100,
    ).result
    lower_observation = result["observations"][1]
    assert lower_observation["resolution"]["state"] == "ambiguous"
    assert lower_observation["resolution"]["candidate_count"] == 10_000
    assert lower_observation["candidates"]["count"] == 10_000
    assert len(lower_observation["candidates"]["sample"]) == 20
    assert lower_observation["candidates"]["truncated"] is True

    _, over_higher = _history_pair_with_candidates(
        tmp_path / "one-over",
        template,
        10_001,
    )
    with pytest.raises(HistoryIOError) as error:
        build_paragraph_history(
            str(over_higher.parent),
            _seed(over_higher, text="H"),
            _lexicographic_order(),
        )
    assert error.value.code == "resource_limit_exceeded"
    assert error.value.metadata["limit"] == "indexed_paragraphs_per_docx"
    assert error.value.metadata["allowed_count"] == 10_000
    assert error.value.metadata["observed_count"] == 10_001


def test_cursor_allows_page_size_changes_without_gaps_or_duplicates(
    demo_dir: Path,
) -> None:
    files = sorted(demo_dir.glob("*.docx"), key=lambda path: (path.name.casefold(), path.name))
    seed = _seed(files[-1])
    order = _lexicographic_order()
    first = build_paragraph_history(
        str(demo_dir), seed, order, max_items=1
    ).result
    second = build_paragraph_history(
        str(demo_dir), seed, order, cursor=first["next_cursor"], max_items=2
    ).result
    third = build_paragraph_history(
        str(demo_dir), seed, order, cursor=second["next_cursor"], max_items=100
    ).result

    observations = (
        first["observations"] + second["observations"] + third["observations"]
    )
    assert [item["position"] for item in observations] == [3, 2, 1, 0]
    assert len({item["observation_id"] for item in observations}) == 4
    assert [
        first["coverage"]["cursor_offset"],
        second["coverage"]["cursor_offset"],
        third["coverage"]["cursor_offset"],
    ] == [0, 1, 3]
    assert third["next_cursor"] is None


def test_seed_drift_out_ranks_generic_cursor_mismatch(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    matter = _copy_demo(demo_dir, tmp_path / "matter")
    files = sorted(matter.glob("*.docx"), key=lambda path: (path.name.casefold(), path.name))
    seed_path = files[-1]
    seed = _seed(seed_path)
    first = build_paragraph_history(
        str(matter), seed, _lexicographic_order(), max_items=1
    ).result
    _mutate_valid_docx(seed_path)

    with pytest.raises(HistoryIOError) as error:
        build_paragraph_history(
            str(matter),
            seed,
            _lexicographic_order(),
            cursor=first["next_cursor"],
        )

    assert error.value.code == "file_sha256_mismatch"


def test_nonseed_drift_refuses_an_old_cursor_as_cursor_mismatch(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    matter = _copy_demo(demo_dir, tmp_path / "matter")
    files = sorted(matter.glob("*.docx"), key=lambda path: (path.name.casefold(), path.name))
    seed = _seed(files[-1])
    first = build_paragraph_history(
        str(matter), seed, _lexicographic_order(), max_items=1
    ).result
    _mutate_valid_docx(files[0])

    with pytest.raises(HistoryIOError) as error:
        build_paragraph_history(
            str(matter),
            seed,
            _lexicographic_order(),
            cursor=first["next_cursor"],
        )

    assert error.value.code == "cursor_mismatch"


def test_candidate_drift_after_descriptor_read_refuses_the_complete_capture(
    demo_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter = _copy_demo(demo_dir, tmp_path / "matter")
    files = sorted(matter.glob("*.docx"), key=lambda path: (path.name.casefold(), path.name))
    pristine = history_io._read_candidate_with_descriptor
    calls = 0

    def mutate_after_read(root_fd, candidate):
        nonlocal calls
        payload, descriptor = pristine(root_fd, candidate)
        calls += 1
        if calls == 1:
            _mutate_valid_docx(matter / candidate.filename)
        return payload, descriptor

    monkeypatch.setattr(
        history_io,
        "_read_candidate_with_descriptor",
        mutate_after_read,
    )
    with pytest.raises(HistoryIOError) as error:
        build_paragraph_history(
            str(matter), _seed(files[-1]), _lexicographic_order()
        )

    assert error.value.code == "workspace_changed"


def test_byte_identical_names_remain_distinct_observations(tmp_path: Path) -> None:
    # Reuse one valid synthetic DOCX twice; the exact filename paths are the
    # observation identity even though document and paragraph identities agree.
    from veqtor_docx.synthetic import generate_demo_rounds

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    generate_demo_rounds(source_dir)
    payload = (source_dir / "round-4-counterparty-reply.docx").read_bytes()
    matter = tmp_path / "matter"
    matter.mkdir()
    lower = matter / "01.docx"
    higher = matter / "02.docx"
    lower.write_bytes(payload)
    higher.write_bytes(payload)
    result = build_paragraph_history(
        str(matter), _seed(higher), _lexicographic_order(), max_items=100
    ).result

    first, second = result["observations"]
    assert first["document_id"] == second["document_id"]
    assert first["observation_id"] != second["observation_id"]
    assert (
        first["selected_paragraph"]["paragraph_id"]
        == second["selected_paragraph"]["paragraph_id"]
    )
    assert (
        first["selected_paragraph"]["paragraph_observation_id"]
        != second["selected_paragraph"]["paragraph_observation_id"]
    )


def test_projection_coverage_isolates_sibling_metadata_and_counts_ancestors() -> None:
    document = etree.Element(w("document"))
    body = etree.SubElement(document, w("body"))
    table = etree.SubElement(body, w("tbl"))
    table_properties = etree.SubElement(table, w("tblPr"))
    etree.SubElement(table_properties, w("tblPrChange"))
    row = etree.SubElement(table, w("tr"))
    row_properties = etree.SubElement(row, w("trPr"))
    etree.SubElement(row_properties, w("trPrChange"))
    cell = etree.SubElement(row, w("tc"))
    cell_properties = etree.SubElement(cell, w("tcPr"))
    etree.SubElement(cell_properties, w("tcPrChange"))

    sibling = etree.SubElement(cell, w("p"))
    sibling_properties = etree.SubElement(sibling, w("pPr"))
    etree.SubElement(sibling_properties, w("pPrChange"))
    sibling_run = etree.SubElement(sibling, w("r"))
    sibling_run_properties = etree.SubElement(sibling_run, w("rPr"))
    etree.SubElement(sibling_run_properties, w("rPrChange"))
    sibling_text = etree.SubElement(sibling_run, w("t"))
    sibling_text.text = "Sibling"

    selected_plain = _text_paragraph(cell, "Selected plain")
    selected_own = etree.SubElement(cell, w("p"))
    own_properties = etree.SubElement(selected_own, w("pPr"))
    etree.SubElement(own_properties, w("pPrChange"))
    own_run = etree.SubElement(selected_own, w("r"))
    own_run_properties = etree.SubElement(own_run, w("rPr"))
    etree.SubElement(own_run_properties, w("rPrChange"))
    own_text = etree.SubElement(own_run, w("t"))
    own_text.text = "Selected own"

    flow = canonical_body_flow_v1(body)
    plain_projection = build_paragraph_projection_v1(selected_plain, flow)
    plain_coverage = build_paragraph_projection_coverage_v1(
        selected_plain,
        flow,
        projection_status=plain_projection["projection_status"],
    )
    own_projection = build_paragraph_projection_v1(selected_own, flow)
    own_coverage = build_paragraph_projection_coverage_v1(
        selected_own,
        flow,
        projection_status=own_projection["projection_status"],
    )

    assert plain_coverage["text_neutral_property_revision_count"] == 3
    assert own_coverage["text_neutral_property_revision_count"] == 5


def test_countered_by_is_charged_at_exact_observation_boundary_and_plus_one(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    path = matter / "01-counter.docx"
    template = demo_dir / "round-1-outgoing-draft.docx"
    _write_counter_docx(path, template, outer_id="1", counter_id="2")
    baseline = build_paragraph_history(
        str(matter),
        _seed(path, text="Seed"),
        _lexicographic_order(),
    ).result
    baseline_observation = baseline["observations"][0]
    baseline_count = baseline["coverage"]["returned_verbatim_char_count"]
    units = baseline_observation["selected_paragraph"]["change_units"]
    assert [(unit["change_type"], unit["countered_by"]) for unit in units] == [
        ("insert", ["2"]),
        ("counter", []),
    ]
    fixed_count = baseline_count - 3
    remaining = (
        history_io.HISTORY_LIMITS["returned_verbatim_chars_per_observation"]
        - fixed_count
    )
    counter_length = (remaining - 1) // 2
    outer_length = remaining - 2 * counter_length
    assert outer_length in {1, 2}

    _write_counter_docx(
        path,
        template,
        outer_id="1" * outer_length,
        counter_id="2" * counter_length,
    )
    boundary = build_paragraph_history(
        str(matter),
        _seed(path, text="Seed"),
        _lexicographic_order(),
    ).result
    assert boundary["coverage"]["returned_verbatim_char_count"] == 200_000

    _write_counter_docx(
        path,
        template,
        outer_id="1" * (outer_length + 1),
        counter_id="2" * counter_length,
    )
    with pytest.raises(HistoryIOError) as error:
        build_paragraph_history(
            str(matter),
            _seed(path, text="Seed"),
            _lexicographic_order(),
        )
    assert error.value.code == "resource_limit_exceeded"
    assert error.value.metadata == {
        "limit": "returned_verbatim_chars_per_observation",
        "allowed_chars": 200_000,
        "observed_chars": 200_001,
    }


def test_page_verbatim_boundary_paginates_whole_observations_without_gaps(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = sorted(
        demo_dir.glob("*.docx"), key=lambda path: (path.name.casefold(), path.name)
    )
    seed = _seed(files[-1])
    order = _lexicographic_order()
    baseline = build_paragraph_history(
        str(demo_dir), seed, order, max_items=100
    ).result
    verbatim_counts = [
        history_io._observation_verbatim_chars(observation)
        for observation in baseline["observations"]
    ]
    exact_two = sum(verbatim_counts[:2])

    with monkeypatch.context() as boundary:
        boundary.setitem(
            history_io.HISTORY_LIMITS,
            "returned_verbatim_chars_per_page",
            exact_two,
        )
        first = build_paragraph_history(
            str(demo_dir), seed, order, max_items=100
        ).result
        second = build_paragraph_history(
            str(demo_dir),
            seed,
            order,
            cursor=first["next_cursor"],
            max_items=100,
        ).result
    assert first["coverage"]["returned_verbatim_char_count"] == exact_two
    assert len(first["observations"]) == 2
    assert [
        observation["position"]
        for observation in first["observations"] + second["observations"]
    ] == [3, 2, 1, 0]
    assert second["next_cursor"] is None

    with monkeypatch.context() as one_over:
        one_over.setitem(
            history_io.HISTORY_LIMITS,
            "returned_verbatim_chars_per_page",
            exact_two - 1,
        )
        pages = []
        cursor = None
        while True:
            page = build_paragraph_history(
                str(demo_dir),
                seed,
                order,
                cursor=cursor,
                max_items=100,
            ).result
            pages.append(page)
            cursor = page["next_cursor"]
            if cursor is None:
                break
    assert len(pages[0]["observations"]) == 1
    observations = [
        observation
        for page in pages
        for observation in page["observations"]
    ]
    assert [observation["position"] for observation in observations] == [3, 2, 1, 0]
    assert len({observation["observation_id"] for observation in observations}) == 4
    assert all(
        page["coverage"]["returned_verbatim_char_count"] <= exact_two - 1
        for page in pages
    )


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "directory"])
def test_unsafe_candidate_kinds_fail_closed(
    tmp_path: Path,
    demo_dir: Path,
    unsafe_kind: str,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    seed_path = matter / "02.docx"
    seed_path.write_bytes((demo_dir / "round-4-counterparty-reply.docx").read_bytes())
    unsafe = matter / "01.docx"
    if unsafe_kind == "symlink":
        unsafe.symlink_to(seed_path)
    elif unsafe_kind == "hardlink":
        os.link(seed_path, unsafe)
    else:
        unsafe.mkdir()

    with pytest.raises(HistoryIOError) as error:
        build_paragraph_history(
            str(matter), _seed(seed_path), _lexicographic_order()
        )

    assert error.value.code == "unsafe_candidate"


def test_explicit_order_must_be_complete_duplicate_free_and_seed_last(
    demo_dir: Path,
) -> None:
    files = sorted(demo_dir.glob("*.docx"), key=lambda path: (path.name.casefold(), path.name))
    seed = _seed(files[-1])
    invalid_orders = [
        [path.name for path in files[:-1]],
        [files[0].name, files[0].name, files[2].name, files[3].name],
        [path.name for path in reversed(files)],
    ]
    expected_codes = [
        "invalid_round_order",
        "invalid_round_order",
        "seed_not_last_declared_position",
    ]
    for filenames, expected in zip(invalid_orders, expected_codes, strict=True):
        with pytest.raises(HistoryIOError) as error:
            build_paragraph_history(
                str(demo_dir), seed, _explicit_order(filenames)
            )
        assert error.value.code == expected


@pytest.mark.parametrize(
    "limit_name",
    [
        "candidate_docx_files",
        "candidate_compressed_input_bytes",
        "candidate_expanded_bytes",
        "compressed_bytes_per_docx",
    ],
)
def test_candidate_capture_byte_caps_use_real_history_paths_at_boundary_and_plus_one(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
) -> None:
    files = sorted(
        demo_dir.glob("*.docx"), key=lambda path: (path.name.casefold(), path.name)
    )
    exact_by_limit = {
        "candidate_docx_files": len(files),
        "candidate_compressed_input_bytes": sum(path.stat().st_size for path in files),
        "candidate_expanded_bytes": sum(_expanded_docx_bytes(path) for path in files),
        "compressed_bytes_per_docx": max(path.stat().st_size for path in files),
    }
    exact = exact_by_limit[limit_name]
    shared_round_map_limit = limit_name in {
        "candidate_docx_files",
        "compressed_bytes_per_docx",
    }

    with monkeypatch.context() as boundary:
        boundary.setitem(history_io.HISTORY_LIMITS, limit_name, exact)
        if shared_round_map_limit:
            boundary.setitem(round_map_module.ROUND_MAP_LIMITS, limit_name, exact)
        result = build_paragraph_history(
            str(demo_dir),
            _seed(files[-1]),
            _lexicographic_order(),
            max_items=100,
        ).result
    assert result["status"] == "ok"

    with monkeypatch.context() as one_over:
        one_over.setitem(history_io.HISTORY_LIMITS, limit_name, exact - 1)
        if shared_round_map_limit:
            one_over.setitem(
                round_map_module.ROUND_MAP_LIMITS,
                limit_name,
                exact - 1,
            )
        with pytest.raises(HistoryIOError) as error:
            build_paragraph_history(
                str(demo_dir),
                _seed(files[-1]),
                _lexicographic_order(),
                max_items=100,
            )
    assert error.value.code == "resource_limit_exceeded"
    if limit_name == "candidate_compressed_input_bytes":
        assert error.value.metadata == {
            "limit": limit_name,
            "allowed_bytes": exact - 1,
            "observed_bytes": exact,
        }
    elif limit_name == "candidate_expanded_bytes":
        assert error.value.metadata == {
            "limit": limit_name,
            "allowed_bytes": exact - 1,
            "observed_bytes": exact,
            "observed_at_least": True,
            "observed_source_sha256": hashlib.sha256(
                files[-1].read_bytes()
            ).hexdigest(),
        }
    else:
        assert error.value.metadata == {}


@pytest.mark.parametrize(
    "limit_name",
    [
        "indexed_paragraphs_per_docx",
        "indexed_paragraphs_per_folder",
        "accepted_current_chars_per_paragraph",
        "accepted_current_chars_per_docx",
        "accepted_current_chars_per_folder",
    ],
)
def test_current_text_index_caps_use_real_history_counters_at_boundary_and_plus_one(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
) -> None:
    files = sorted(
        demo_dir.glob("*.docx"), key=lambda path: (path.name.casefold(), path.name)
    )
    snapshots = [
        _load_snapshot_from_payload(path.read_bytes(), path=str(path)) for path in files
    ]
    document_chars = [
        sum(len(paragraph.text) for paragraph in snapshot.paragraphs)
        for snapshot in snapshots
    ]
    exact_by_limit = {
        "indexed_paragraphs_per_docx": max(
            len(snapshot.paragraphs) for snapshot in snapshots
        ),
        "indexed_paragraphs_per_folder": sum(
            len(snapshot.paragraphs) for snapshot in snapshots
        ),
        "accepted_current_chars_per_paragraph": max(
            len(paragraph.text)
            for snapshot in snapshots
            for paragraph in snapshot.paragraphs
        ),
        "accepted_current_chars_per_docx": max(document_chars),
        "accepted_current_chars_per_folder": sum(document_chars),
    }
    exact = exact_by_limit[limit_name]
    unit = "chars" if "chars" in limit_name else "count"
    suffix = "chars" if unit == "chars" else "count"
    seed = _seed(files[-1])

    with monkeypatch.context() as boundary:
        boundary.setitem(history_io.HISTORY_LIMITS, limit_name, exact)
        if limit_name == "indexed_paragraphs_per_docx":
            boundary.setattr(inspect_module, "MAX_INDEXED_PARAGRAPHS", exact)
        elif limit_name == "accepted_current_chars_per_docx":
            boundary.setattr(inspect_module, "MAX_AGGREGATE_TEXT_CHARS", exact)
        result = build_paragraph_history(
            str(demo_dir),
            seed,
            _lexicographic_order(),
            max_items=100,
        ).result
    assert result["status"] == "ok"

    with monkeypatch.context() as one_over:
        one_over.setitem(history_io.HISTORY_LIMITS, limit_name, exact - 1)
        if limit_name == "indexed_paragraphs_per_docx":
            one_over.setattr(inspect_module, "MAX_INDEXED_PARAGRAPHS", exact - 1)
        elif limit_name == "accepted_current_chars_per_docx":
            one_over.setattr(
                inspect_module,
                "MAX_AGGREGATE_TEXT_CHARS",
                exact - 1,
            )
        with pytest.raises(HistoryIOError) as error:
            build_paragraph_history(
                str(demo_dir),
                seed,
                _lexicographic_order(),
                max_items=100,
            )
    assert error.value.code == "resource_limit_exceeded"
    expected_metadata = {
        "limit": limit_name,
        f"allowed_{suffix}": exact - 1,
        f"observed_{suffix}": exact,
    }
    if limit_name == "indexed_paragraphs_per_docx":
        trigger = next(
            path
            for path, snapshot in zip(files, snapshots, strict=True)
            if len(snapshot.paragraphs) == exact
        )
        expected_metadata["observed_source_sha256"] = hashlib.sha256(
            trigger.read_bytes()
        ).hexdigest()
    elif limit_name == "accepted_current_chars_per_docx":
        trigger = next(
            path
            for path, character_count in zip(files, document_chars, strict=True)
            if character_count == exact
        )
        expected_metadata["observed_at_least"] = True
        expected_metadata["observed_source_sha256"] = hashlib.sha256(
            trigger.read_bytes()
        ).hexdigest()
    assert error.value.metadata == expected_metadata


@pytest.mark.parametrize(
    "limit_name",
    [
        "rejected_projection_chars_per_paragraph",
        "rejected_projection_chars_per_docx",
        "rejected_projection_chars_per_folder",
        "decoded_text_chars_per_folder",
        "selected_change_units_per_result",
        "change_units_per_selected_paragraph",
        "change_unit_text_chars_per_selected_observation",
        "change_unit_text_chars_per_result",
    ],
)
def test_selected_projection_and_change_unit_caps_use_materialized_history_surface(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
) -> None:
    files = sorted(
        demo_dir.glob("*.docx"), key=lambda path: (path.name.casefold(), path.name)
    )
    baseline = build_paragraph_history(
        str(demo_dir),
        _seed(files[-1]),
        _lexicographic_order(),
        max_items=100,
    ).result
    selected = [
        observation["selected_paragraph"]
        for observation in baseline["observations"]
        if observation["selected_paragraph"] is not None
    ]
    rejected_chars = [
        len(item["rejected_pending"]["text"])
        for item in selected
        if isinstance(item["rejected_pending"]["text"], str)
    ]
    change_unit_counts = [len(item["change_units"]) for item in selected]
    change_unit_chars = [
        sum(
            len(value)
            for unit in item["change_units"]
            for key in ("old_text", "new_text")
            if isinstance((value := unit[key]), str)
        )
        for item in selected
    ]
    accepted_current_chars = sum(
        len(paragraph.text)
        for path in files
        for paragraph in _load_snapshot_from_payload(
            path.read_bytes(), path=str(path)
        ).paragraphs
    )
    exact_by_limit = {
        "rejected_projection_chars_per_paragraph": max(rejected_chars),
        "rejected_projection_chars_per_docx": max(rejected_chars),
        "rejected_projection_chars_per_folder": sum(rejected_chars),
        "decoded_text_chars_per_folder": (
            accepted_current_chars + sum(rejected_chars) + sum(change_unit_chars)
        ),
        "selected_change_units_per_result": sum(change_unit_counts),
        "change_units_per_selected_paragraph": max(change_unit_counts),
        "change_unit_text_chars_per_selected_observation": max(change_unit_chars),
        "change_unit_text_chars_per_result": sum(change_unit_chars),
    }
    exact = exact_by_limit[limit_name]
    unit = "chars" if "chars" in limit_name else "count"
    suffix = "chars" if unit == "chars" else "count"
    seed = _seed(files[-1])

    with monkeypatch.context() as boundary:
        boundary.setitem(history_io.HISTORY_LIMITS, limit_name, exact)
        if limit_name == "rejected_projection_chars_per_paragraph":
            boundary.setattr(
                projection_module,
                "MAX_REJECTED_PROJECTION_CHARS_PER_PARAGRAPH",
                exact,
            )
        result = build_paragraph_history(
            str(demo_dir),
            seed,
            _lexicographic_order(),
            max_items=100,
        ).result
    assert result["status"] == "ok"

    with monkeypatch.context() as one_over:
        one_over.setitem(history_io.HISTORY_LIMITS, limit_name, exact - 1)
        if limit_name == "rejected_projection_chars_per_paragraph":
            one_over.setattr(
                projection_module,
                "MAX_REJECTED_PROJECTION_CHARS_PER_PARAGRAPH",
                exact - 1,
            )
        with pytest.raises(HistoryIOError) as error:
            build_paragraph_history(
                str(demo_dir),
                seed,
                _lexicographic_order(),
                max_items=100,
            )
    assert error.value.code == "resource_limit_exceeded"
    if limit_name == "rejected_projection_chars_per_paragraph":
        assert error.value.metadata == {
            "limit": limit_name,
            "allowed_count": exact - 1,
            "observed_count": exact,
        }
    else:
        assert error.value.metadata == {
            "limit": limit_name,
            f"allowed_{suffix}": exact - 1,
            f"observed_{suffix}": exact,
        }


def test_navigation_cap_uses_complete_resolver_population_at_boundary_and_plus_one(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = sorted(
        demo_dir.glob("*.docx"), key=lambda path: (path.name.casefold(), path.name)
    )
    baseline = build_paragraph_history(
        str(demo_dir),
        _seed(files[-1]),
        _lexicographic_order(),
        max_items=100,
    ).result
    exact = baseline["coverage"]["navigation_candidate_count"]
    assert exact > 0

    with monkeypatch.context() as boundary:
        boundary.setitem(history_io.HISTORY_LIMITS, "navigation_candidates", exact)
        boundary.setattr(resolution, "MAX_HISTORY_NAVIGATION_CANDIDATES", exact)
        result = build_paragraph_history(
            str(demo_dir),
            _seed(files[-1]),
            _lexicographic_order(),
            max_items=100,
        ).result
    assert result["coverage"]["navigation_candidate_count"] == exact

    with monkeypatch.context() as one_over:
        one_over.setitem(
            history_io.HISTORY_LIMITS,
            "navigation_candidates",
            exact - 1,
        )
        one_over.setattr(
            resolution,
            "MAX_HISTORY_NAVIGATION_CANDIDATES",
            exact - 1,
        )
        with pytest.raises(HistoryIOError) as error:
            build_paragraph_history(
                str(demo_dir),
                _seed(files[-1]),
                _lexicographic_order(),
                max_items=100,
            )
    assert error.value.code == "resource_limit_exceeded"
    assert error.value.metadata == {
        "limit": "navigation_candidates",
        "allowed_count": exact - 1,
        "observed_count": exact,
    }


def test_candidate_sample_boundary_is_exercised_by_complete_history_results(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    template = demo_dir / "round-1-outgoing-draft.docx"
    for candidate_count, truncated in ((20, False), (21, True)):
        _, higher = _history_pair_with_candidates(
            tmp_path / f"candidates-{candidate_count}",
            template,
            candidate_count,
        )
        result = build_paragraph_history(
            str(higher.parent),
            _seed(higher, text="H"),
            _lexicographic_order(),
            max_items=100,
        ).result
        summary = result["observations"][1]["candidates"]
        assert summary["count"] == candidate_count
        assert len(summary["sample"]) == min(candidate_count, 20)
        assert summary["truncated"] is truncated


def test_default_and_maximum_item_caps_page_real_observations(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    template = demo_dir / "round-1-outgoing-draft.docx"
    source = tmp_path / "source.docx"
    shutil.copyfile(template, source)
    _replace_document_body(source, lambda body: _text_paragraph(body, "Seed"))
    payload = source.read_bytes()
    matter = tmp_path / "item-boundaries"
    matter.mkdir()
    paths = []
    for index in range(101):
        path = matter / f"{index:03d}.docx"
        path.write_bytes(payload)
        paths.append(path)
    seed = _seed(paths[-1], text="Seed")

    default_page = build_paragraph_history(
        str(matter), seed, _lexicographic_order()
    ).result
    assert len(default_page["observations"]) == 50
    assert default_page["coverage"]["cursor_offset"] == 0
    assert default_page["next_cursor"] is not None

    maximum_page = build_paragraph_history(
        str(matter), seed, _lexicographic_order(), max_items=100
    ).result
    assert len(maximum_page["observations"]) == 100
    assert maximum_page["next_cursor"] is not None
    tail = build_paragraph_history(
        str(matter),
        seed,
        _lexicographic_order(),
        cursor=maximum_page["next_cursor"],
        max_items=100,
    ).result
    assert len(tail["observations"]) == 1
    assert tail["next_cursor"] is None

    with pytest.raises(HistoryIOError) as error:
        build_paragraph_history(
            str(matter), seed, _lexicographic_order(), max_items=101
        )
    assert error.value.code == "invalid_request"


def test_revision_nesting_boundary_uses_real_history_projection(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    template = demo_dir / "round-1-outgoing-draft.docx"
    boundary = tmp_path / "depth-two"
    boundary.mkdir()
    boundary_path = boundary / "01.docx"
    _write_nested_revision_docx(boundary_path, template, depth=2)
    result = build_paragraph_history(
        str(boundary),
        _seed(boundary_path, text="Seed"),
        _lexicographic_order(),
    ).result
    assert result["status"] == "ok"

    one_over = tmp_path / "depth-three"
    one_over.mkdir()
    one_over_path = one_over / "01.docx"
    _write_nested_revision_docx(one_over_path, template, depth=3)
    with pytest.raises(HistoryIOError) as error:
        build_paragraph_history(
            str(one_over),
            _seed(one_over_path, text="Seed"),
            _lexicographic_order(),
        )
    assert error.value.code == "resource_limit_exceeded"
    assert error.value.metadata == {
        "limit": "revision_nesting_depth",
        "allowed_count": 2,
        "observed_count": 3,
    }


_INCLUSIVE_HARD_LIMITS = (
    "candidate_docx_files",
    "candidate_compressed_input_bytes",
    "candidate_expanded_bytes",
    "compressed_bytes_per_docx",
    "indexed_paragraphs_per_docx",
    "indexed_paragraphs_per_folder",
    "accepted_current_chars_per_paragraph",
    "accepted_current_chars_per_docx",
    "accepted_current_chars_per_folder",
    "rejected_projection_chars_per_paragraph",
    "rejected_projection_chars_per_docx",
    "rejected_projection_chars_per_folder",
    "decoded_text_chars_per_folder",
    "exact_candidate_relationships",
    "navigation_candidates",
    "selected_change_units_per_result",
    "change_units_per_selected_paragraph",
    "change_unit_text_chars_per_selected_observation",
    "change_unit_text_chars_per_result",
    "returned_verbatim_chars_per_observation",
    "returned_verbatim_chars_per_page",
    "revision_nesting_depth",
)


@pytest.mark.parametrize("limit_name", _INCLUSIVE_HARD_LIMITS)
def test_each_nonjournal_history_hard_limit_is_inclusive(limit_name: str) -> None:
    allowed = history_io.HISTORY_LIMITS[limit_name]
    history_io._limit(allowed, limit_name, "boundary")

    with pytest.raises(HistoryIOError) as error:
        history_io._limit(allowed + 1, limit_name, "one over")

    assert error.value.code == "resource_limit_exceeded"
    assert error.value.metadata["limit"] == limit_name


def test_exact_relationship_limit_is_reserved_at_relationship_creation(
    demo_dir: Path,
) -> None:
    payload = (demo_dir / "round-4-counterparty-reply.docx").read_bytes()
    lower_snapshot = _load_snapshot_from_payload(payload, path="lower.docx")
    higher_snapshot = _load_snapshot_from_payload(payload, path="higher.docx")
    lower = resolution.ParagraphHistoryObservation(
        observation_id="rm_obs_v1:" + "1" * 64,
        snapshot=lower_snapshot,
    )
    higher = resolution.ParagraphHistoryObservation(
        observation_id="rm_obs_v1:" + "2" * 64,
        snapshot=higher_snapshot,
    )
    higher_paragraph = next(item for item in higher.snapshot.paragraphs if CAP_R4 in item.text)
    latest = resolution._selected_work(
        higher,
        1,
        higher_paragraph,
        (),
    )
    budget = resolution._ExactCandidateRelationshipBudget()
    for _ in range(49_999):
        budget.reserve()
    candidates, _, _, _ = resolution._exact_candidates(
        lower,
        0,
        latest,
        relationship_budget=budget,
    )
    assert sum(len(candidate.relationships) for candidate in candidates) == 1
    assert budget.count == 50_000

    with pytest.raises(resolution.HistoryResolutionError) as error:
        resolution._exact_candidates(
            lower,
            0,
            latest,
            relationship_budget=budget,
        )
    assert error.value.code == "resource_limit_exceeded"
    assert error.value.metadata == {
        "limit": "exact_candidate_relationships",
        "allowed_count": 50_000,
        "observed_count": 50_001,
    }
    assert resolution.MAX_HISTORY_EXACT_CANDIDATE_RELATIONSHIPS == 50_000


def test_equal_hash_with_unequal_full_text_is_a_consistency_error(
    demo_dir: Path,
) -> None:
    payload = (demo_dir / "round-4-counterparty-reply.docx").read_bytes()
    lower_snapshot = _load_snapshot_from_payload(payload, path="lower.docx")
    higher_snapshot = _load_snapshot_from_payload(payload, path="higher.docx")
    lower = resolution.ParagraphHistoryObservation(
        observation_id="rm_obs_v1:" + "1" * 64,
        snapshot=lower_snapshot,
    )
    higher = resolution.ParagraphHistoryObservation(
        observation_id="rm_obs_v1:" + "2" * 64,
        snapshot=higher_snapshot,
    )
    higher_paragraph = next(item for item in higher.snapshot.paragraphs if CAP_R4 in item.text)
    selected = resolution._selected_work(higher, 1, higher_paragraph, ())
    selected = replace(
        selected,
        selected=replace(
            selected.selected,
            current_text="different complete text",
        ),
    )

    with pytest.raises(resolution.HistoryResolutionError) as error:
        resolution._exact_candidates(lower, 0, selected)

    assert error.value.code == "evidence_consistency_error"


def test_page_item_and_sample_boundaries_are_closed(demo_dir: Path) -> None:
    files = sorted(demo_dir.glob("*.docx"), key=lambda path: (path.name.casefold(), path.name))
    build_paragraph_history(
        str(demo_dir), _seed(files[-1]), _lexicographic_order(), max_items=100
    )
    for invalid in (False, 0, 101):
        with pytest.raises(HistoryIOError) as error:
            build_paragraph_history(
                str(demo_dir),
                _seed(files[-1]),
                _lexicographic_order(),
                max_items=invalid,
            )
        assert error.value.code == "invalid_request"

    candidates = tuple(
        resolution.ParagraphHistoryNavigationCandidate(
            navigation_candidate_id=f"ph_nav_v1:{index:064x}",
            observation_id="rm_obs_v1:" + "1" * 64,
            seed_section_id="rm_sec_v1:" + "2" * 64,
            candidate_section_id=f"rm_sec_v1:{index:064x}",
            section_ref={"index": index},
            label=str(index),
            heading=None,
            level=0,
            label_basis="word_numbering_v1",
            evidence_basis={"schema_version": "navigation_candidate_basis.v1"},
        )
        for index in range(21)
    )
    summary, complete = history_io._navigation_summary(candidates)
    assert len(complete) == 21
    assert summary["count"] == 21
    assert len(summary["sample"]) == 20
    assert summary["truncated"] is True
    assert history_io.DEFAULT_MAX_ITEMS == 50
    assert history_io.MAX_ITEMS == 100


def test_unexpected_preresult_failure_is_sanitized(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = sorted(demo_dir.glob("*.docx"), key=lambda path: (path.name.casefold(), path.name))

    def explode(*args, **kwargs):
        raise RuntimeError("PRIVATE internal detail")

    monkeypatch.setattr(history_io, "_capture_workspace", explode)
    with pytest.raises(HistoryIOError) as error:
        build_paragraph_history(
            str(demo_dir), _seed(files[-1]), _lexicographic_order()
        )

    assert error.value.code == "internal_error"
    assert "PRIVATE" not in str(error.value)
