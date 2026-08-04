# SPDX-License-Identifier: Apache-2.0
"""Acceptance coverage for the bounded internal History I/O envelope."""

from __future__ import annotations

import io
import os
import shutil
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from veqtor_docx.inspect import _load_snapshot_from_payload, _paragraph_ref
from veqtor_docx.synthetic import CAP_R4
from veqtor_mcp import _history_io as history_io
from veqtor_mcp import _history_resolution as resolution
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
