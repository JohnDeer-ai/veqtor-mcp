# SPDX-License-Identifier: Apache-2.0
"""Stage 3B bounded Round Map core, journal, cursor and privacy acceptance."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from veqtor_docx import generate_demo_rounds, inspect_document
from veqtor_mcp import records, server
from veqtor_mcp import round_map as round_map_module
from veqtor_mcp.round_map import (
    ROUND_MAP_LIMITS,
    RoundMapError,
    build_round_map,
)


def test_round_map_module_can_be_imported_before_records(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import veqtor_mcp.round_map; import veqtor_mcp.records",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def _matter(tmp_path: Path) -> Path:
    matter = tmp_path / "matter"
    generate_demo_rounds(matter)
    return matter


def _round(matter: Path, number: int) -> Path:
    return sorted(matter.glob("*.docx"))[number - 1]


def _seed(path: Path, paragraph_index: int = 0) -> dict:
    browsed = inspect_document(str(path), "browse", max_items=100)
    paragraph = browsed["paragraphs"][paragraph_index]
    return {
        "schema_version": "round_map_seed.v1",
        "path": str(path),
        "paragraph_ref": paragraph["paragraph_ref"],
    }


def _current_apply_record(
    matter: Path,
    source_sha256: str,
    output_sha256: str,
    *,
    result_source_sha256: str | None = None,
    result_output_sha256: str | None = None,
) -> dict:
    round_trip = {
        "status": "passed",
        "comparison": "ooxml_semantic_diff_outside_touched_anchors",
        "collateral_changes": [],
    }
    result = {
        "status": "ok",
        "source_sha256": source_sha256
        if result_source_sha256 is None
        else result_source_sha256,
        "output_sha256": output_sha256
        if result_output_sha256 is None
        else result_output_sha256,
        "round_trip_check": round_trip,
        "preflight_binding_status": "verified",
        "preflight_candidate_sha256": output_sha256,
        "candidate_output_sha256_match": True,
    }
    provenance = {
        "source_sha256": source_sha256,
        "output_sha256": output_sha256,
        "round_trip_check": round_trip,
        "preflight_binding_status": "verified",
        "preflight_candidate_sha256": output_sha256,
        "candidate_output_sha256_match": True,
    }
    meta = records.write_record(
        workspace=matter,
        tool_name="apply_edits",
        input_payload={},
        result=result,
        tool_result=result,
        provenance=provenance,
    )
    assert meta["record_status"] == "written"
    return meta


def _items(result: dict, item_type: str) -> list[dict]:
    return [item for item in result["items"] if item["item_type"] == item_type]


def test_exact_equality_and_navigation_never_become_lineage_or_chronology(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 2), paragraph_index=1)

    result = build_round_map(str(matter), seed, max_items=100).result

    assert result["schema_version"] == "round_map.v1"
    assert result["coverage"]["candidate_document_count"] == 4
    relationships = _items(result, "relationship")
    assert any(
        item["relationship_type"] == "exact_content_equality" for item in relationships
    )
    for relationship in relationships:
        assert relationship["lineage_verified"] is False
        assert relationship["chronology_verified"] is False
        assert relationship["derivation_recorded"] is (
            relationship["relationship_type"] == "recorded_derivation"
        )
    assert result["order_basis"]["lineage_verified"] is False
    assert result["order_basis"]["round_id_semantics"] == "position_only"
    assert all(item["reason"] != "deleted" for item in _items(result, "resolution"))


def test_navigation_only_candidates_remain_unresolved(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 2), paragraph_index=60)

    result = build_round_map(str(matter), seed, max_items=100).result

    assert result["coverage"]["relationship_counts"] == {
        "recorded_derivation": 0,
        "exact_content_equality": 0,
        "navigation_candidate": 3,
    }
    navigation_only = [
        item
        for item in _items(result, "resolution")
        if item["reason"] == "navigation_only"
    ]
    assert len(navigation_only) == 3
    assert all(item["state"] == "unresolved" for item in navigation_only)


def test_current_apply_record_creates_only_a_document_recorded_derivation(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    source = _round(matter, 1)
    output = _round(matter, 2)
    seed = _seed(source)
    source_sha = seed["paragraph_ref"]["file_sha256"]
    output_sha = _seed(output)["paragraph_ref"]["file_sha256"]
    _current_apply_record(matter, source_sha, output_sha)

    result = build_round_map(str(matter), seed, max_items=100).result
    derivations = [
        item
        for item in _items(result, "relationship")
        if item["relationship_type"] == "recorded_derivation"
    ]

    assert len(derivations) == 1
    edge = derivations[0]
    assert edge["from_id"] == f"rm_doc_v1:{source_sha}"
    assert edge["to_id"] == f"rm_doc_v1:{output_sha}"
    assert edge["basis"]["support_profile"] == "current_only"
    assert edge["derivation_recorded"] is True
    assert edge["lineage_verified"] is False
    assert result["coverage"]["eligible_derivation_record_count"] == 1


def test_failed_apply_and_successful_preflight_create_no_derivation(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    source_sha = seed["paragraph_ref"]["file_sha256"]
    failed = {"status": "error", "error_code": "not_applicable"}
    assert (
        records.write_record(
            workspace=matter,
            tool_name="apply_edits",
            input_payload={},
            result=failed,
            provenance={"source_sha256": source_sha, "output_sha256": "b" * 64},
        )["record_status"]
        == "written"
    )
    preflight = {"status": "ok", "source_sha256": source_sha}
    assert (
        records.write_record(
            workspace=matter,
            tool_name="preflight_edits",
            input_payload={},
            result=preflight,
            provenance={"source_sha256": source_sha},
        )["record_status"]
        == "written"
    )

    result = build_round_map(str(matter), seed, max_items=100).result
    assert result["coverage"]["relevant_apply_record_count"] == 0
    assert not any(
        item["relationship_type"] == "recorded_derivation"
        for item in _items(result, "relationship")
    )


def test_preflightless_published_profile_and_strengthened_hybrid_conflict(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    source = _round(matter, 1)
    seed = _seed(source)
    source_sha = seed["paragraph_ref"]["file_sha256"]
    fixture = json.loads(
        (
            Path(__file__).parent
            / "data"
            / "round-map-v0.1.2-preflightless-apply-record.json"
        ).read_text(encoding="utf-8")
    )
    fixture["record_id"] = "dr_001"
    fixture["workspace"] = str(matter)
    fixture["result"]["source_sha256"] = source_sha
    fixture["provenance"]["source_sha256"] = source_sha
    fixture["result_sha256"] = records._stable_digest(fixture["result"])
    fixture["tool_result_sha256"] = fixture["result_sha256"]
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    (sidecar / records.GITIGNORE_NAME).write_text("*\n", encoding="utf-8")
    (sidecar / records.JOURNAL_NAME).write_text(
        json.dumps(fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )

    result = build_round_map(str(matter), seed, max_items=100).result
    edge = next(
        item
        for item in _items(result, "relationship")
        if item["relationship_type"] == "recorded_derivation"
    )
    assert edge["basis"]["support_profile"] == "published_v0_1_2_only"
    output_id = "rm_doc_v1:" + "b" * 64
    output_node = next(
        item for item in _items(result, "document_node") if item["id"] == output_id
    )
    assert output_node["observation_state"] == "record_only"
    output_resolution = next(
        item
        for item in _items(result, "resolution")
        if item["document_id"] == output_id
    )
    assert (output_resolution["state"], output_resolution["reason"]) == (
        "unresolved",
        "record_only_document",
    )

    fixture["result"]["preflight_binding_status"] = "verified"
    fixture["result_sha256"] = records._stable_digest(fixture["result"])
    fixture["tool_result_sha256"] = fixture["result_sha256"]
    (sidecar / records.JOURNAL_NAME).write_text(
        json.dumps(fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    conflicted = build_round_map(str(matter), seed, max_items=100).result
    assert not any(
        item["relationship_type"] == "recorded_derivation"
        for item in _items(conflicted, "relationship")
    )
    conflict = _items(conflicted, "conflict")
    assert len(conflict) == 1
    assert conflict[0]["reason"] == "unsupported_legacy_profile"


def test_divergent_output_conflict_affects_only_the_current_endpoint(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    current = _round(matter, 1)
    seed = _seed(current)
    current_sha = seed["paragraph_ref"]["file_sha256"]
    _current_apply_record(
        matter,
        "c" * 64,
        "b" * 64,
        result_source_sha256="c" * 64,
        result_output_sha256=current_sha,
    )

    result = build_round_map(str(matter), seed, max_items=100).result
    conflicts = _items(result, "conflict")
    assert len(conflicts) == 1
    assert conflicts[0]["reason"] == "result_output_sha256_mismatch"
    assert conflicts[0]["affected_document_ids"] == [f"rm_doc_v1:{current_sha}"]
    assert not any(
        item["id"] in {"rm_doc_v1:" + "b" * 64, "rm_doc_v1:" + "c" * 64}
        for item in _items(result, "document_node")
    )
    resolution = next(
        item
        for item in _items(result, "resolution")
        if item["document_id"] == f"rm_doc_v1:{current_sha}"
    )
    assert (
        resolution["state"],
        resolution["reason"],
        resolution["conflict_count"],
    ) == (
        "ambiguous",
        "recorded_fact_conflict",
        1,
    )


def test_duplicate_bytes_collapse_document_and_paragraph_identity(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    source = _round(matter, 1)
    alias = matter / "zz-identical.docx"
    shutil.copyfile(source, alias)
    seed = _seed(source)

    result = build_round_map(str(matter), seed, max_items=100).result
    document_id = f"rm_doc_v1:{seed['paragraph_ref']['file_sha256']}"
    node = next(
        item for item in _items(result, "document_node") if item["id"] == document_id
    )
    assert node["observation_count"] == 2
    assert (
        sum(
            item["document_id"] == document_id
            for item in _items(result, "document_observation")
        )
        == 2
    )
    assert (
        sum(
            item["document_id"] == document_id
            for item in _items(result, "paragraph_node")
        )
        == 1
    )


def test_equal_hash_signal_is_not_trusted_without_full_text_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter = _matter(tmp_path)
    seed_path = _round(matter, 2)
    seed = _seed(seed_path)
    original = round_map_module._load_snapshot_from_payload
    altered = False

    def inconsistent_snapshot(
        payload, *, path, expanded_budget, missing_document_part_code
    ):
        nonlocal altered
        snapshot = original(
            payload,
            path=path,
            expanded_budget=expanded_budget,
            missing_document_part_code=missing_document_part_code,
        )
        if path != str(seed_path) and not altered:
            paragraphs = list(snapshot.paragraphs)
            target_hash = seed["paragraph_ref"]["paragraph_text_sha256"]
            for index, paragraph in enumerate(paragraphs):
                if paragraph.text_sha256 == target_hash:
                    paragraphs[index] = replace(paragraph, text=paragraph.text + "X")
                    altered = True
                    return replace(snapshot, paragraphs=tuple(paragraphs))
        return snapshot

    monkeypatch.setattr(
        round_map_module, "_load_snapshot_from_payload", inconsistent_snapshot
    )
    with pytest.raises(RoundMapError) as error:
        build_round_map(str(matter), seed)
    assert altered is True
    assert error.value.code == "evidence_consistency_error"


def test_branch_cycle_self_loop_multiple_parents_and_duplicate_support_are_preserved(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    a = seed["paragraph_ref"]["file_sha256"]
    b, c = "b" * 64, "c" * 64
    for source, output in ((a, b), (a, b), (a, c), (b, a), (c, b), (a, a)):
        _current_apply_record(matter, source, output)

    result = build_round_map(str(matter), seed, max_items=100).result
    derivations = [
        item
        for item in _items(result, "relationship")
        if item["relationship_type"] == "recorded_derivation"
    ]
    assert len(derivations) == 5
    duplicate = next(
        item
        for item in derivations
        if item["from_id"] == f"rm_doc_v1:{a}" and item["to_id"] == f"rm_doc_v1:{b}"
    )
    assert duplicate["basis"]["supporting_records"]["count"] == 2
    nodes = {item["id"]: item for item in _items(result, "document_node")}
    assert nodes[f"rm_doc_v1:{a}"]["topology_flags"] == {
        "multiple_parents": True,
        "cycle_member": True,
        "self_loop": True,
    }
    assert nodes[f"rm_doc_v1:{b}"]["topology_flags"]["multiple_parents"] is True
    assert nodes[f"rm_doc_v1:{b}"]["topology_flags"]["cycle_member"] is True


def test_pagination_allows_page_size_change_and_ignores_own_records(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))

    first = server.map_rounds(str(matter), seed, max_items=1)
    assert first["next_cursor"] is not None
    second = server.map_rounds(
        str(matter), seed, cursor=first["next_cursor"], max_items=3
    )

    assert (
        first["snapshot"]["journal_snapshot_sha256"]
        == second["snapshot"]["journal_snapshot_sha256"]
    )
    assert (
        first["snapshot"]["full_result_set_sha256"]
        == second["snapshot"]["full_result_set_sha256"]
    )
    assert {item["id"] for item in first["items"]}.isdisjoint(
        item["id"] for item in second["items"]
    )
    assert second["coverage"]["cursor_offset"] == 1
    assert second["coverage"]["returned_item_count"] == 3


def test_support_sample_is_bounded_without_dropping_duplicate_evidence(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    source_sha = seed["paragraph_ref"]["file_sha256"]
    for _index in range(21):
        _current_apply_record(matter, source_sha, "b" * 64)

    result = build_round_map(str(matter), seed, max_items=100).result
    edge = next(
        item
        for item in _items(result, "relationship")
        if item["relationship_type"] == "recorded_derivation"
    )
    support = edge["basis"]["supporting_records"]
    assert support["count"] == 21
    assert len(support["sample"]) == 20
    assert support["truncated"] is True


def test_nonseed_drift_invalidates_cursor_but_seed_drift_has_specific_error(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed_path = _round(matter, 1)
    other = _round(matter, 2)
    seed = _seed(seed_path)
    first = build_round_map(str(matter), seed, max_items=1).result

    shutil.copyfile(_round(matter, 3), other)
    with pytest.raises(RoundMapError) as changed:
        build_round_map(str(matter), seed, cursor=first["next_cursor"], max_items=1)
    assert changed.value.code == "cursor_mismatch"

    shutil.copyfile(_round(matter, 4), seed_path)
    with pytest.raises(RoundMapError) as stale:
        build_round_map(str(matter), seed, cursor=first["next_cursor"], max_items=1)
    assert stale.value.code == "file_sha256_mismatch"


def test_candidate_symlink_and_hardlink_fail_closed(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    symlink = matter / "unsafe.docx"
    symlink.symlink_to(_round(matter, 2))
    with pytest.raises(RoundMapError) as unsafe_symlink:
        build_round_map(str(matter), seed)
    assert unsafe_symlink.value.code == "unsafe_candidate"
    symlink.unlink()

    hardlink = matter / "unsafe.docx"
    os.link(_round(matter, 2), hardlink)
    with pytest.raises(RoundMapError) as unsafe_hardlink:
        build_round_map(str(matter), seed)
    assert unsafe_hardlink.value.code == "unsafe_candidate"


def test_corrupt_journal_and_semantic_limit_fail_before_success(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    (sidecar / records.JOURNAL_NAME).write_bytes(b"not-json\n")
    with pytest.raises(RoundMapError) as corrupt:
        build_round_map(str(matter), seed)
    assert corrupt.value.code == "journal_corrupt"
    assert ROUND_MAP_LIMITS["journal_apply_records"] == 10_000
    assert ROUND_MAP_LIMITS["total_map_items"] == 70_000
    assert ROUND_MAP_LIMITS["maximum_page_items"] == 100


def test_journal_snapshot_contention_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))

    def busy(*_args, **_kwargs):
        raise records.DecisionRecordError("journal_busy", "simulated contention")

    monkeypatch.setattr(records, "_bounded_journal_lock", busy)
    with pytest.raises(RoundMapError) as error:
        build_round_map(str(matter), seed)
    assert error.value.code == "journal_busy"


def test_total_item_limit_accepts_boundary_and_refuses_one_over() -> None:
    at_limit = [{}] * ROUND_MAP_LIMITS["total_map_items"]
    round_map_module._enforce_item_cap(at_limit, "map item", "total_map_items")
    with pytest.raises(RoundMapError) as error:
        round_map_module._enforce_item_cap(
            [*at_limit, {}], "map item", "total_map_items"
        )
    assert error.value.code == "resource_limit_exceeded"


def test_compact_round_map_record_is_path_and_text_free(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    mapped = server.map_rounds(str(matter), seed, max_items=100)
    assert mapped["record_status"] == "written"

    exported = server.export_decision_record(str(matter), max_records=10)
    compact = next(
        item for item in exported["records"] if item["tool_name"] == "map_rounds"
    )
    encoded = json.dumps(compact, ensure_ascii=False)
    raw = records.read_records(str(matter), max_records=10, include_payload=True)[
        "records"
    ]
    raw_map = next(item for item in raw if item["tool_name"] == "map_rounds")

    assert compact["record_type"] == "round_map.v1"
    assert compact["input"] == {
        "sha256": records._stable_digest(raw_map["input"]),
        "omitted": True,
    }
    assert str(matter) not in encoded
    assert all(path.name not in encoded for path in matter.glob("*.docx"))
    assert set(compact["provenance"]) == {
        "filesystem_snapshot_sha256",
        "journal_snapshot_sha256",
        "full_result_set_sha256",
        "reading_mode",
        "container_policy",
        "search_scope",
    }
    assert all(
        set(sample) == {"item_type", "id", "item_sha256"}
        for sample in compact["result"]["items_summary"]["sample"]
    )


def test_invalid_seed_manifest_cursor_and_limits_use_closed_codes(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    cases = [
        ({**seed, "extra": True}, {}, "invalid_request"),
        ({**seed, "paragraph_ref": {"file_sha256": "0" * 64}}, {}, "invalid_reference"),
        (seed, {"ordered_filenames": [_round(matter, 1).name]}, "invalid_round_order"),
        (seed, {"cursor": "rm2:1:" + "0" * 64}, "invalid_cursor"),
        (seed, {"max_items": True}, "invalid_request"),
        (seed, {"max_items": 101}, "invalid_request"),
    ]
    for candidate_seed, kwargs, code in cases:
        with pytest.raises(RoundMapError) as error:
            build_round_map(str(matter), candidate_seed, **kwargs)
        assert error.value.code == code
