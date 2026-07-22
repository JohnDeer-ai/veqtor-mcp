# SPDX-License-Identifier: Apache-2.0
"""Stage 3B bounded Round Map core, journal, cursor and privacy acceptance."""

from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import zipfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from veqtor_docx import generate_demo_rounds, inspect_document
from veqtor_docx._ooxml import parse_xml, w
from veqtor_mcp import records, server
from veqtor_mcp import round_map as round_map_module
from veqtor_mcp.round_map_contract import ROUND_MAP_ITEM_SCHEMA
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
    input_payload: dict | None = None,
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
        input_payload={} if input_payload is None else input_payload,
        result=result,
        tool_result=result,
        provenance=provenance,
    )
    assert meta["record_status"] == "written"
    return meta


def _items(result: dict, item_type: str) -> list[dict]:
    return [item for item in result["items"] if item["item_type"] == item_type]


def _rewrite_document_xml(path: Path, mutate) -> None:
    replacement = path.with_suffix(".replacement")
    with (
        zipfile.ZipFile(path) as source,
        zipfile.ZipFile(replacement, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "word/document.xml":
                payload = mutate(payload)
            target.writestr(info, payload)
    os.replace(replacement, path)


def _patch_zip_headers(
    path: Path, *, encrypted: bool = False, compression_method: int | None = None
) -> None:
    payload = bytearray(path.read_bytes())
    for signature, flags_offset, method_offset in (
        (b"PK\x03\x04", 6, 8),
        (b"PK\x01\x02", 8, 10),
    ):
        position = 0
        while True:
            position = payload.find(signature, position)
            if position < 0:
                break
            if encrypted:
                flags = struct.unpack_from("<H", payload, position + flags_offset)[0]
                struct.pack_into("<H", payload, position + flags_offset, flags | 0x0001)
            if compression_method is not None:
                struct.pack_into(
                    "<H", payload, position + method_offset, compression_method
                )
            position += len(signature)
    path.write_bytes(payload)


def _replace_body_text(
    path: Path, body_texts: list[str], *, excluded_text: str | None = None
) -> None:
    from lxml import etree

    def mutate(payload: bytes) -> bytes:
        document = parse_xml(payload)
        body = next(child for child in document if child.tag == w("body"))
        for child in list(body):
            body.remove(child)

        def paragraph(parent, value: str) -> None:
            paragraph_element = etree.SubElement(parent, w("p"))
            run = etree.SubElement(paragraph_element, w("r"))
            text = etree.SubElement(run, w("t"))
            text.text = value

        for value in body_texts:
            paragraph(body, value)
        if excluded_text is not None:
            excluded = etree.SubElement(body, "{urn:veqtor:test}excluded")
            paragraph(excluded, excluded_text)
        return etree.tostring(document, xml_declaration=True, encoding="UTF-8")

    _rewrite_document_xml(path, mutate)


def _unwritten_error_code(callable_) -> str:
    with pytest.raises(Exception) as error:
        callable_()
    return getattr(error.value, "code", "")


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


def test_single_navigation_candidate_remains_unresolved(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    paths = sorted(matter.glob("*.docx"))
    seed_path = paths[1]
    for path in paths[2:]:
        path.unlink()
    seed = _seed(seed_path, paragraph_index=60)

    result = build_round_map(str(matter), seed, max_items=100).result
    navigation = [
        item
        for item in _items(result, "relationship")
        if item["relationship_type"] == "navigation_candidate"
    ]
    assert len(navigation) == 1
    resolution = next(
        item
        for item in _items(result, "resolution")
        if item["reason"] == "navigation_only"
    )
    assert (resolution["state"], resolution["navigation_candidate_count"]) == (
        "unresolved",
        1,
    )


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


def test_frozen_and_published_profiles_cover_every_strengthened_presence_case() -> None:
    golden = [
        json.loads(line)
        for line in (
            Path(__file__).parent / "data" / "decision-records-v1-golden.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    legacy = next(record for record in golden if record["record_id"] == "dr_004")
    classified_legacy = round_map_module._classify_apply_record(legacy)
    assert (classified_legacy.kind, classified_legacy.profile) == (
        "valid",
        "frozen_legacy_v1",
    )

    published = json.loads(
        (
            Path(__file__).parent
            / "data"
            / "round-map-v0.1.2-preflightless-apply-record.json"
        ).read_text(encoding="utf-8")
    )
    slots = [
        ("result", "preflight_binding_status", "verified"),
        ("provenance", "preflight_binding_status", "verified"),
        (
            "result",
            "preflight_candidate_sha256",
            published["result"]["output_sha256"],
        ),
        (
            "provenance",
            "preflight_candidate_sha256",
            published["provenance"]["output_sha256"],
        ),
        ("result", "candidate_output_sha256_match", True),
        ("provenance", "candidate_output_sha256_match", True),
    ]
    for count in range(1, 6):
        candidate = deepcopy(published)
        for owner, key, value in slots[:count]:
            candidate[owner][key] = value
        classification = round_map_module._classify_apply_record(candidate)
        assert (classification.kind, classification.reason) == (
            "conflict",
            "unsupported_legacy_profile",
        )
    candidate = deepcopy(published)
    for owner, key, value in slots:
        candidate[owner][key] = value
    candidate["provenance"]["preflight_binding_status"] = "not-verified"
    classification = round_map_module._classify_apply_record(candidate)
    assert (classification.kind, classification.reason) == (
        "conflict",
        "strengthened_fact_mismatch",
    )


def test_frozen_golden_profile_full_map_conflict_changes_both_bound_digests(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    source_sha256 = seed["paragraph_ref"]["file_sha256"]
    golden = next(
        json.loads(line)
        for line in (
            Path(__file__).parent / "data" / "decision-records-v1-golden.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
        if json.loads(line)["record_id"] == "dr_004"
    )
    golden["workspace"] = str(matter)
    golden["provenance"]["source_sha256"] = source_sha256
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    (sidecar / records.GITIGNORE_NAME).write_text("*\n", encoding="utf-8")
    journal = sidecar / records.JOURNAL_NAME

    def write_fixture(record: dict) -> None:
        journal.write_text(
            json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n",
            encoding="utf-8",
        )

    write_fixture(golden)
    eligible = build_round_map(str(matter), seed, max_items=100).result
    edge = next(
        item
        for item in _items(eligible, "relationship")
        if item["relationship_type"] == "recorded_derivation"
    )
    assert (edge["from_id"], edge["to_id"]) == (
        f"rm_doc_v1:{source_sha256}",
        "rm_doc_v1:" + ("c" * 64),
    )
    assert edge["basis"]["support_profile"] == "frozen_legacy_only"

    conflicted_record = deepcopy(golden)
    conflicted_record["result"]["preflight_binding_status"] = "verified"
    conflicted_record["result_sha256"] = records._stable_digest(
        conflicted_record["result"]
    )
    conflicted_record["tool_result_sha256"] = conflicted_record["result_sha256"]
    write_fixture(conflicted_record)
    conflicted = build_round_map(str(matter), seed, max_items=100).result
    assert not any(
        item["relationship_type"] == "recorded_derivation"
        for item in _items(conflicted, "relationship")
    )
    conflict = _items(conflicted, "conflict")
    assert len(conflict) == 1
    assert conflict[0]["reason"] == "unsupported_legacy_profile"
    assert (
        eligible["snapshot"]["journal_snapshot_sha256"]
        != conflicted["snapshot"]["journal_snapshot_sha256"]
    )
    assert (
        eligible["snapshot"]["full_result_set_sha256"]
        != conflicted["snapshot"]["full_result_set_sha256"]
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
        payload,
        *,
        path,
        expanded_budget,
        missing_document_part_code,
        invalid_document_structure_code,
        invalid_ooxml_value_code,
    ):
        nonlocal altered
        snapshot = original(
            payload,
            path=path,
            expanded_budget=expanded_budget,
            missing_document_part_code=missing_document_part_code,
            invalid_document_structure_code=invalid_document_structure_code,
            invalid_ooxml_value_code=invalid_ooxml_value_code,
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
    assert {(item["from_id"], item["to_id"]) for item in derivations} == {
        (f"rm_doc_v1:{a}", f"rm_doc_v1:{b}"),
        (f"rm_doc_v1:{a}", f"rm_doc_v1:{c}"),
        (f"rm_doc_v1:{b}", f"rm_doc_v1:{a}"),
        (f"rm_doc_v1:{c}", f"rm_doc_v1:{b}"),
        (f"rm_doc_v1:{a}", f"rm_doc_v1:{a}"),
    }
    assert result["coverage"]["eligible_derivation_record_count"] == 6
    for edge in derivations:
        support = edge["basis"]["supporting_records"]
        assert support["count"] == len(support["sample"])
        assert support["sha256"] == round_map_module._digest(
            {
                "schema_version": "recorded_derivation_support.v1",
                "records": support["sample"],
            }
        )
        assert edge["basis"]["support_profile"] == "current_only"
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


def test_pagination_exhaustion_has_exact_union_without_overlap_or_omission(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    cursor = None
    returned_ids: list[str] = []
    eligible_count = None
    sizes = (7, 13, 5, 100)
    page_number = 0
    while True:
        page = build_round_map(
            str(matter),
            seed,
            cursor=cursor,
            max_items=sizes[min(page_number, len(sizes) - 1)],
        ).result
        if eligible_count is None:
            eligible_count = page["coverage"]["eligible_item_count"]
        assert page["coverage"]["cursor_offset"] == len(returned_ids)
        page_ids = [item["id"] for item in page["items"]]
        assert set(returned_ids).isdisjoint(page_ids)
        returned_ids.extend(page_ids)
        cursor = page["next_cursor"]
        page_number += 1
        if cursor is None:
            break
    assert eligible_count is not None
    assert len(returned_ids) == eligible_count
    assert len(set(returned_ids)) == eligible_count
    assert page["coverage"]["output_truncated"] is False


def test_default_page_size_and_cancellation_never_create_partial_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = _matter(tmp_path / "generated")
    matter = tmp_path / "default-page"
    matter.mkdir()
    seed_path = matter / "01-seed.docx"
    candidate_path = matter / "02-candidate.docx"
    shutil.copyfile(_round(generated, 1), seed_path)
    shutil.copyfile(_round(generated, 2), candidate_path)
    _replace_body_text(seed_path, ["Repeated bounded clause"])
    _replace_body_text(candidate_path, ["Repeated bounded clause"] * 60)
    seed = _seed(seed_path)
    default_page = build_round_map(str(matter), seed).result
    assert len(default_page["items"]) == ROUND_MAP_LIMITS["default_page_items"]
    assert default_page["next_cursor"] is not None

    cancelled = _matter(tmp_path / "cancelled")
    cancelled_seed = _seed(_round(cancelled, 1))

    def cancel(_captured):
        raise asyncio.CancelledError

    monkeypatch.setattr(round_map_module, "_parse_candidates", cancel)
    with pytest.raises(asyncio.CancelledError):
        server.map_rounds(str(cancelled), cancelled_seed)
    assert not (cancelled / records.SIDECAR_DIR).exists()


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


def test_later_page_missing_seed_and_journal_compound_failures_keep_phase_order(
    tmp_path: Path,
) -> None:
    missing = _matter(tmp_path / "missing")
    missing_seed_path = _round(missing, 1)
    missing_seed = _seed(missing_seed_path)
    missing_first = build_round_map(str(missing), missing_seed, max_items=1).result
    missing_seed_path.unlink()
    sidecar = missing / records.SIDECAR_DIR
    sidecar.mkdir()
    (sidecar / records.JOURNAL_NAME).write_bytes(b"not-json\n")
    with pytest.raises(RoundMapError) as missing_error:
        build_round_map(
            str(missing),
            missing_seed,
            cursor=missing_first["next_cursor"],
            max_items=1,
        )
    assert missing_error.value.code == "seed_not_candidate"

    corrupt = _matter(tmp_path / "corrupt")
    corrupt_seed = _seed(_round(corrupt, 1))
    corrupt_first = build_round_map(str(corrupt), corrupt_seed, max_items=1).result
    corrupt_sidecar = corrupt / records.SIDECAR_DIR
    corrupt_sidecar.mkdir()
    (corrupt_sidecar / records.JOURNAL_NAME).write_bytes(b"not-json\n")
    with pytest.raises(RoundMapError) as corrupt_error:
        build_round_map(
            str(corrupt),
            corrupt_seed,
            cursor=corrupt_first["next_cursor"],
            max_items=1,
        )
    assert corrupt_error.value.code == "journal_corrupt"

    malformed = _matter(tmp_path / "malformed")
    malformed_seed = _seed(_round(malformed, 1))
    malformed_first = build_round_map(
        str(malformed), malformed_seed, max_items=1
    ).result
    _rewrite_document_xml(_round(malformed, 2), lambda payload: payload[:-17])
    with pytest.raises(RoundMapError) as malformed_error:
        build_round_map(
            str(malformed),
            malformed_seed,
            cursor=malformed_first["next_cursor"],
            max_items=1,
        )
    assert malformed_error.value.code == "invalid_docx"


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

    def busy(fd, *_args, **_kwargs):
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
        raise records.DecisionRecordError("journal_busy", "simulated contention")

    monkeypatch.setattr(records, "_bounded_journal_lock", busy)
    with pytest.raises(RoundMapError) as error:
        build_round_map(str(matter), seed)
    assert error.value.code == "journal_busy"


def test_exact_64_mib_aggregate_journal_is_accepted_and_one_byte_over_refuses(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    assert (
        records.write_record(
            workspace=matter,
            tool_name="list_rounds",
            input_payload={},
            result={"status": "ok"},
            provenance={},
        )["record_status"]
        == "written"
    )
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    template = json.loads(journal.read_text(encoding="utf-8"))
    line_size = records.MAX_JOURNAL_BYTES // 64
    lines: list[bytes] = []
    for record_number in range(1, 65):
        record = deepcopy(template)
        record["record_id"] = f"dr_{record_number:03d}"
        encoded = json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        padding = line_size - len(encoded) - 1
        assert 0 <= padding
        lines.append(encoded + (b" " * padding) + b"\n")
    journal.write_bytes(b"".join(lines))
    assert journal.stat().st_size == 67_108_864

    accepted = build_round_map(str(matter), seed, max_items=1).result
    assert accepted["snapshot"]["journal_state"] == "no_relevant_apply_records"
    with journal.open("ab") as handle:
        handle.write(b" ")
    assert journal.stat().st_size == 67_108_865
    with pytest.raises(RoundMapError) as error:
        build_round_map(str(matter), seed, max_items=1)
    assert error.value.code == "journal_oversize"


def test_total_item_limit_accepts_boundary_and_refuses_one_over() -> None:
    at_limit = [{}] * ROUND_MAP_LIMITS["total_map_items"]
    round_map_module._enforce_item_cap(at_limit, "map item", "total_map_items")
    with pytest.raises(RoundMapError) as error:
        round_map_module._enforce_item_cap(
            [*at_limit, {}], "map item", "total_map_items"
        )
    assert error.value.code == "resource_limit_exceeded"


@pytest.mark.parametrize(
    ("label", "limit_key"),
    [
        ("document node", "document_nodes"),
        ("document observation", "document_observations"),
        ("paragraph node", "paragraph_nodes"),
        ("section node", "section_nodes"),
        ("recorded relationship", "recorded_derivation_relationships"),
        ("equality relationship", "exact_equality_relationships"),
        ("navigation relationship", "navigation_relationships"),
        ("resolution", "resolution_items"),
        ("conflict", "conflict_items"),
    ],
)
def test_every_frozen_item_class_cap_is_inclusive_and_refuses_one_over(
    label: str, limit_key: str
) -> None:
    boundary = [{}] * ROUND_MAP_LIMITS[limit_key]
    round_map_module._enforce_item_cap(boundary, label, limit_key)
    with pytest.raises(RoundMapError) as error:
        round_map_module._append_bounded(
            boundary,
            {},
            item_type=label,
            limit_key=limit_key,
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


def test_compact_export_omits_navigation_body_edit_and_verbatim_input_sentinels(
    tmp_path: Path,
) -> None:
    from lxml import etree

    matter = _matter(tmp_path / "PRIVATE_WORKSPACE_SENTINEL")
    paths = sorted(matter.glob("*.docx"))
    private_filename = matter / "PRIVATE_FILENAME_SENTINEL.docx"
    paths[-1].rename(private_filename)

    def replace_section(path: Path, body_text: str) -> None:
        def mutate(payload: bytes) -> bytes:
            document = parse_xml(payload)
            body = next(child for child in document if child.tag == w("body"))
            for child in list(body):
                body.remove(child)
            heading = etree.SubElement(body, w("p"))
            properties = etree.SubElement(heading, w("pPr"))
            outline = etree.SubElement(properties, w("outlineLvl"))
            outline.set(w("val"), "0")
            run = etree.SubElement(heading, w("r"))
            text = etree.SubElement(run, w("t"))
            text.text = "1. PRIVATE_HEADING_SENTINEL"
            paragraph = etree.SubElement(body, w("p"))
            body_run = etree.SubElement(paragraph, w("r"))
            body_atom = etree.SubElement(body_run, w("t"))
            body_atom.text = body_text
            return etree.tostring(document, xml_declaration=True, encoding="UTF-8")

        _rewrite_document_xml(path, mutate)

    seed_path = sorted(matter.glob("*.docx"))[0]
    candidate_path = sorted(matter.glob("*.docx"))[1]
    replace_section(seed_path, "PRIVATE_BODY_TEXT_SENTINEL")
    replace_section(candidate_path, "Different candidate body")
    seed = _seed(seed_path, paragraph_index=1)
    _current_apply_record(
        matter,
        seed["paragraph_ref"]["file_sha256"],
        _seed(candidate_path)["paragraph_ref"]["file_sha256"],
        input_payload={
            "edits": [{"insert_text": "PRIVATE_EDIT_TEXT_SENTINEL"}],
        },
    )
    ordered_filenames = sorted(
        (path.name for path in matter.glob("*.docx")),
        key=lambda value: (value.casefold(), value),
    )
    mapped = server.map_rounds(
        str(matter),
        seed,
        ordered_filenames=ordered_filenames,
        max_items=100,
    )
    assert any(
        item.get("heading") == "PRIVATE_HEADING_SENTINEL" for item in mapped["items"]
    )
    exported = server.export_decision_record(str(matter), max_records=100)
    compact = next(
        item for item in exported["records"] if item["tool_name"] == "map_rounds"
    )
    encoded = json.dumps(compact, ensure_ascii=False)
    for sentinel in (
        "PRIVATE_WORKSPACE_SENTINEL",
        "PRIVATE_FILENAME_SENTINEL.docx",
        "PRIVATE_HEADING_SENTINEL",
        "PRIVATE_BODY_TEXT_SENTINEL",
        "PRIVATE_EDIT_TEXT_SENTINEL",
        seed["path"],
        json.dumps(ordered_filenames, ensure_ascii=False),
    ):
        assert sentinel not in encoded


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


def test_round_map_pre_result_refusals_never_initialize_or_append_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    absent = _matter(tmp_path / "absent")
    absent_seed = _seed(_round(absent, 1))
    assert (
        _unwritten_error_code(
            lambda: server.map_rounds(
                str(absent), {**absent_seed, "schema_version": "wrong"}
            )
        )
        == "invalid_request"
    )
    assert not (absent / records.SIDECAR_DIR).exists()

    existing = _matter(tmp_path / "existing")
    existing_seed = _seed(_round(existing, 1))
    records.write_record(
        workspace=existing,
        tool_name="list_rounds",
        input_payload={"folder": str(existing)},
        result={"status": "error", "error_code": "sentinel", "error": "sentinel"},
        provenance={},
    )
    journal = existing / records.SIDECAR_DIR / records.JOURNAL_NAME
    before = journal.read_bytes()
    assert (
        _unwritten_error_code(
            lambda: server.map_rounds(
                str(existing), {**existing_seed, "schema_version": "wrong"}
            )
        )
        == "invalid_request"
    )
    assert journal.read_bytes() == before

    def explode(*_args, **_kwargs):
        raise RuntimeError("PRIVATE_INTERNAL_SENTINEL")

    monkeypatch.setattr(round_map_module, "build_round_map", explode)
    assert (
        _unwritten_error_code(lambda: server.map_rounds(str(existing), existing_seed))
        == "internal_error"
    )
    assert journal.read_bytes() == before


def test_round_map_success_writes_once_and_append_failure_is_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))

    successful = server.map_rounds(str(matter), seed, max_items=100)
    assert (successful["status"], successful["record_status"]) == ("ok", "written")
    raw = records.read_records(matter, max_records=100, include_payload=True)["records"]
    assert [record["tool_name"] for record in raw] == ["map_rounds"]
    assert raw[0]["record_type"] == "round_map.v1"
    assert raw[0]["result"]["status"] == "ok"

    monkeypatch.setattr(
        records,
        "write_record",
        lambda **_kwargs: {
            "record_id": None,
            "record_status": "write_failed",
            "record_error": "journal_busy",
        },
    )
    failed_append = server.map_rounds(str(matter), seed, max_items=100)
    assert (failed_append["status"], failed_append["record_status"]) == (
        "ok",
        "write_failed",
    )


@pytest.mark.parametrize(
    ("result_value", "provenance_value", "field", "expected_reason"),
    [
        (True, 1, "round_trip_sentinel", "round_trip_fact_mismatch"),
        (1, 1.0, "preflight_binding_status", "strengthened_fact_mismatch"),
        (-0.0, 0.0, "preflight_binding_status", "strengthened_fact_mismatch"),
    ],
)
def test_apply_fact_copies_use_canonical_json_type_and_value_equality(
    result_value, provenance_value, field: str, expected_reason: str
) -> None:
    round_trip = {
        "status": "passed",
        "comparison": "ooxml_semantic_diff_outside_touched_anchors",
        "collateral_changes": [],
    }
    result = {
        "status": "ok",
        "source_sha256": "a" * 64,
        "output_sha256": "b" * 64,
        "round_trip_check": deepcopy(round_trip),
        "preflight_binding_status": "verified",
        "preflight_candidate_sha256": "b" * 64,
        "candidate_output_sha256_match": True,
    }
    provenance = {
        "source_sha256": "a" * 64,
        "output_sha256": "b" * 64,
        "round_trip_check": deepcopy(round_trip),
        "preflight_binding_status": "verified",
        "preflight_candidate_sha256": "b" * 64,
        "candidate_output_sha256_match": True,
    }
    if field == "round_trip_sentinel":
        result["round_trip_check"][field] = result_value
        provenance["round_trip_check"][field] = provenance_value
    else:
        result[field] = result_value
        provenance[field] = provenance_value
    classification = round_map_module._classify_apply_record(
        {"record_id": "dr_001", "result": result, "provenance": provenance}
    )
    assert (classification.kind, classification.reason) == (
        "conflict",
        expected_reason,
    )


def test_candidate_output_match_requires_exact_json_true() -> None:
    round_trip = {
        "status": "passed",
        "comparison": "ooxml_semantic_diff_outside_touched_anchors",
        "collateral_changes": [],
    }
    copied = {
        "source_sha256": "a" * 64,
        "output_sha256": "b" * 64,
        "round_trip_check": round_trip,
        "preflight_binding_status": "verified",
        "preflight_candidate_sha256": "b" * 64,
        "candidate_output_sha256_match": 1,
    }
    classification = round_map_module._classify_apply_record(
        {
            "record_id": "dr_001",
            "result": {"status": "ok", **deepcopy(copied)},
            "provenance": deepcopy(copied),
        }
    )
    assert (classification.kind, classification.reason) == (
        "conflict",
        "candidate_output_sha256_match_invalid",
    )


def test_workspace_replacement_between_filesystem_and_journal_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    original = records.read_round_map_apply_records

    def replace_then_read(workspace, *, expected_workspace_identity=None):
        old = matter.with_name("captured-old")
        matter.rename(old)
        matter.mkdir()
        sidecar = matter / records.SIDECAR_DIR
        sidecar.mkdir()
        (sidecar / records.JOURNAL_NAME).write_bytes(b"PRIVATE_REPLACEMENT_JOURNAL\n")
        return original(
            workspace, expected_workspace_identity=expected_workspace_identity
        )

    monkeypatch.setattr(records, "read_round_map_apply_records", replace_then_read)
    with pytest.raises(RoundMapError) as error:
        build_round_map(str(matter), seed)
    assert error.value.code == "workspace_changed"


def test_seed_evidence_precedes_journal_and_candidate_safety_precedes_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    (sidecar / records.JOURNAL_NAME).write_bytes(b"not-json\n")
    stale = deepcopy(seed)
    stale["paragraph_ref"]["file_sha256"] = "0" * 64
    with pytest.raises(RoundMapError) as seed_error:
        build_round_map(str(matter), stale)
    assert seed_error.value.code == "file_sha256_mismatch"

    original_loader = round_map_module._load_snapshot_from_payload
    altered = False

    def inconsistent_loader(payload, **kwargs):
        nonlocal altered
        snapshot = original_loader(payload, **kwargs)
        if kwargs["path"] != seed["path"] and not altered:
            target_hash = seed["paragraph_ref"]["paragraph_text_sha256"]
            paragraphs = list(snapshot.paragraphs)
            for index, paragraph in enumerate(paragraphs):
                if paragraph.text_sha256 == target_hash:
                    paragraphs[index] = replace(paragraph, text=paragraph.text + "X")
                    altered = True
                    return replace(snapshot, paragraphs=tuple(paragraphs))
        return snapshot

    monkeypatch.setattr(
        round_map_module, "_load_snapshot_from_payload", inconsistent_loader
    )
    with pytest.raises(RoundMapError) as evidence_error:
        build_round_map(str(matter), seed)
    assert altered is True
    assert evidence_error.value.code == "evidence_consistency_error"
    monkeypatch.setattr(
        round_map_module, "_load_snapshot_from_payload", original_loader
    )

    (sidecar / records.JOURNAL_NAME).unlink()
    unsafe = matter / "unsafe.docx"
    unsafe.symlink_to(_round(matter, 2))
    duplicate_order = [path.name for path in sorted(matter.glob("*.docx"))]
    duplicate_order[-1] = duplicate_order[0]
    with pytest.raises(RoundMapError) as safety_error:
        build_round_map(str(matter), seed, ordered_filenames=duplicate_order)
    assert safety_error.value.code == "unsafe_candidate"

    monkeypatch.setitem(ROUND_MAP_LIMITS, "candidate_docx_files", 1)
    with pytest.raises(RoundMapError) as count_error:
        build_round_map(str(matter), seed, ordered_filenames=["missing.docx"])
    assert count_error.value.code == "resource_limit_exceeded"


@pytest.mark.parametrize("digits", [129, 5_000])
def test_cursor_decimal_offset_is_bounded_before_integer_conversion(
    tmp_path: Path, digits: int
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    cursor = "rm1:" + ("9" * digits) + ":" + ("0" * 64)
    with pytest.raises(RoundMapError) as error:
        build_round_map(str(matter), seed, cursor=cursor)
    assert error.value.code == "invalid_cursor"


def test_cursor_mismatch_precedes_digest_valid_offset_range(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    first = build_round_map(str(matter), seed, max_items=1)
    proof = first.proof
    invalid_offset = len(proof.item_fingerprints)
    binding = round_map_module._cursor_binding(
        next_offset=invalid_offset,
        folder=proof.folder,
        seed_path=proof.seed_path,
        seed_ref=proof.seed_ref,
        filenames=list(proof.filenames),
        seed_document_id=proof.seed_document_id,
        seed_paragraph_id=proof.seed_paragraph_id,
        ordering_source=proof.ordering_source,
        filename_manifest_sha256=proof.filename_manifest_sha256,
        filesystem_snapshot_sha256=proof.filesystem_snapshot_sha256,
        journal_snapshot_sha256=proof.journal_snapshot_sha256,
        full_result_set_sha256=first.result["snapshot"]["full_result_set_sha256"],
    )
    with pytest.raises(RoundMapError) as mismatch:
        build_round_map(str(matter), seed, cursor=f"rm1:{invalid_offset}:" + ("0" * 64))
    assert mismatch.value.code == "cursor_mismatch"
    with pytest.raises(RoundMapError) as outside:
        build_round_map(str(matter), seed, cursor=f"rm1:{invalid_offset}:{binding}")
    assert outside.value.code == "invalid_cursor"


def test_round_map_normalizes_main_body_structure_but_not_archive_ambiguity(
    tmp_path: Path,
) -> None:
    for case in (
        "missing_body",
        "duplicate_body",
        "malformed_xml",
        "malformed_outline_level",
    ):
        matter = _matter(tmp_path / case)
        seed = _seed(_round(matter, 2))
        target = _round(matter, 1)

        def mutate(payload: bytes, *, selected=case) -> bytes:
            if selected == "malformed_xml":
                return payload[:-17]
            from lxml import etree

            document = parse_xml(payload)
            body = next(child for child in document if child.tag == w("body"))
            if selected == "missing_body":
                body.tag = w("notBody")
            elif selected == "malformed_outline_level":
                paragraph = next(child for child in body if child.tag == w("p"))
                properties = paragraph.find(w("pPr"))
                if properties is None:
                    properties = etree.Element(w("pPr"))
                    paragraph.insert(0, properties)
                outline = etree.SubElement(properties, w("outlineLvl"))
                outline.set(w("val"), "not-an-integer")
            else:
                document.append(deepcopy(body))

            return etree.tostring(document, xml_declaration=True, encoding="UTF-8")

        _rewrite_document_xml(target, mutate)
        with pytest.raises(RoundMapError) as error:
            build_round_map(str(matter), seed)
        assert error.value.code == "invalid_docx"

    matter = _matter(tmp_path / "duplicate_archive_member")
    seed = _seed(_round(matter, 2))
    target = _round(matter, 1)
    with zipfile.ZipFile(target, "a", zipfile.ZIP_STORED) as archive:
        archive.writestr("word/document.xml", b"<duplicate/>")
    with pytest.raises(RoundMapError) as archive_error:
        build_round_map(str(matter), seed)
    assert archive_error.value.code == "file_unextractable"


@pytest.mark.parametrize(
    ("parser_error", "expected_code"),
    [
        (round_map_module.InspectError("encrypted_docx", "sentinel"), "encrypted_docx"),
        (
            round_map_module.InspectError("unsupported_compression", "sentinel"),
            "unsupported_compression",
        ),
        (
            round_map_module.ResourceLimitError("synthetic_limit", "sentinel"),
            "resource_limit_exceeded",
        ),
    ],
)
def test_one_candidate_parse_refusal_aborts_the_complete_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parser_error: Exception,
    expected_code: str,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))

    def refuse(*_args, **_kwargs):
        raise parser_error

    monkeypatch.setattr(round_map_module, "_load_snapshot_from_payload", refuse)
    with pytest.raises(RoundMapError) as error:
        build_round_map(str(matter), seed)
    assert error.value.code == expected_code


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("encrypted", "encrypted_docx"),
        ("unsupported_compression", "unsupported_compression"),
        ("compression_ratio", "resource_limit_exceeded"),
    ],
)
def test_real_hostile_docx_packages_refuse_the_complete_map(
    tmp_path: Path, case: str, expected_code: str
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 2))
    target = _round(matter, 1)
    if case == "encrypted":
        _patch_zip_headers(target, encrypted=True)
    elif case == "unsupported_compression":
        _patch_zip_headers(target, compression_method=99)
    else:
        replacement = target.with_suffix(".replacement")
        with (
            zipfile.ZipFile(target) as source,
            zipfile.ZipFile(replacement, "w", zipfile.ZIP_DEFLATED) as output,
        ):
            for info in source.infolist():
                output.writestr(info, source.read(info.filename))
            output.writestr("word/resource-bomb.xml", b"0" * (12 * 1024 * 1024))
        os.replace(replacement, target)

    with pytest.raises(RoundMapError) as error:
        build_round_map(str(matter), seed)
    assert error.value.code == expected_code


def test_explicit_order_accepts_platform_direct_backslash_basename(
    tmp_path: Path,
) -> None:
    if os.path.altsep == "\\":
        pytest.skip("backslash is a separator on this platform")
    matter = _matter(tmp_path)
    unusual = matter / "round\\portable.docx"
    shutil.copyfile(_round(matter, 1), unusual)
    seed = _seed(_round(matter, 1))
    filenames = sorted(
        (path.name for path in matter.iterdir() if path.suffix.casefold() == ".docx"),
        key=lambda value: (value.casefold(), value),
    )
    result = build_round_map(
        str(matter), seed, ordered_filenames=filenames, max_items=100
    ).result
    observations = _items(result, "document_observation")
    assert [
        item["filename"] for item in sorted(observations, key=lambda x: x["position"])
    ] == filenames


def test_explicit_manifest_and_mtimes_remain_position_only(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    order = [path.name for path in reversed(sorted(matter.glob("*.docx")))]
    first = build_round_map(
        str(matter), seed, ordered_filenames=order, max_items=100
    ).result
    for index, path in enumerate(matter.glob("*.docx"), start=1):
        os.utime(path, ns=(index, index))
    second = build_round_map(
        str(matter), seed, ordered_filenames=order, max_items=100
    ).result
    assert first["ordering_source"] == "explicit_filename_sequence_v1"
    assert first["order_basis"] == second["order_basis"]
    assert first["snapshot"] == second["snapshot"]
    assert all(
        relationship["lineage_verified"] is False
        and relationship["chronology_verified"] is False
        for relationship in _items(first, "relationship")
    )


def test_workspace_set_drift_and_candidate_count_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stable = tmp_path / "stable-500"
    stable.mkdir()
    source_matter = _matter(tmp_path / "source")
    source = _round(source_matter, 1)
    for index in range(500):
        shutil.copyfile(source, stable / f"round-{index:03d}.docx")
    seed = _seed(stable / "round-000.docx")
    result = build_round_map(str(stable), seed, max_items=100).result
    assert result["coverage"]["candidate_document_count"] == 500

    shutil.copyfile(source, stable / "round-500.docx")
    with pytest.raises(RoundMapError) as initial_over:
        build_round_map(str(stable), seed)
    assert initial_over.value.code == "resource_limit_exceeded"
    (stable / "round-500.docx").unlink()

    pristine_read = round_map_module._read_candidate
    calls = 0

    def grow_after_capture(root_fd, candidate):
        nonlocal calls
        payload = pristine_read(root_fd, candidate)
        calls += 1
        if calls == 500:
            shutil.copyfile(source, stable / "round-500.docx")
        return payload

    monkeypatch.setattr(round_map_module, "_read_candidate", grow_after_capture)
    with pytest.raises(RoundMapError) as final_growth:
        build_round_map(str(stable), seed)
    assert final_growth.value.code == "workspace_changed"

    added = _matter(tmp_path / "within-cap-addition")
    added_seed = _seed(_round(added, 1))
    calls = 0

    def add_after_capture(root_fd, candidate):
        nonlocal calls
        payload = pristine_read(root_fd, candidate)
        calls += 1
        if calls == 4:
            shutil.copyfile(source, added / "new-round.docx")
        return payload

    monkeypatch.setattr(round_map_module, "_read_candidate", add_after_capture)
    with pytest.raises(RoundMapError) as addition:
        build_round_map(str(added), added_seed)
    assert addition.value.code == "workspace_changed"

    smaller = _matter(tmp_path / "smaller")
    smaller_seed = _seed(_round(smaller, 1))
    calls = 0

    def delete_after_capture(root_fd, candidate):
        nonlocal calls
        payload = pristine_read(root_fd, candidate)
        calls += 1
        if calls == 4:
            _round(smaller, 1).unlink()
        return payload

    monkeypatch.setattr(round_map_module, "_read_candidate", delete_after_capture)
    with pytest.raises(RoundMapError) as deletion:
        build_round_map(str(smaller), smaller_seed)
    assert deletion.value.code == "workspace_changed"


def test_output_contract_rejects_cross_field_and_identity_contradictions(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    _current_apply_record(matter, seed["paragraph_ref"]["file_sha256"], "b" * 64)
    computation = build_round_map(str(matter), seed, max_items=100)

    def ordering_tuple(result):
        result["order_basis"]["rule"] = "exact_sequence"

    def record_only_exact(result):
        resolution = next(
            item
            for item in result["items"]
            if item["item_type"] == "resolution"
            and item["reason"] == "record_only_document"
        )
        resolution["state"] = "exact_unique"
        resolution["reason"] = "one_exact_candidate"
        result["coverage"]["resolution_counts"]["unresolved"] -= 1
        result["coverage"]["resolution_counts"]["exact_unique"] += 1

    def observation_identity(result):
        observation = next(
            item
            for item in result["items"]
            if item["item_type"] == "document_observation"
        )
        observation["round_id"] = "round-999"

    def manifest_digest(result):
        result["order_basis"]["filename_manifest_sha256"] = "0" * 64

    def journal_tuple(result):
        result["snapshot"]["journal_state"] = "no_relevant_apply_records"

    def count_tuple(result):
        result["coverage"]["eligible_item_count"] += 1

    for mutate in (
        ordering_tuple,
        record_only_exact,
        observation_identity,
        manifest_digest,
        journal_tuple,
        count_tuple,
    ):
        invalid = deepcopy(computation.result)
        mutate(invalid)
        with pytest.raises(RoundMapError) as error:
            round_map_module.validate_computation_result(computation, invalid)
        assert error.value.code == "output_contract_error"

    invalid_summary = round_map_module.record_summary(computation.result)
    invalid_summary["items_summary"]["count"] += 1
    publication = records.write_record(
        workspace=matter,
        tool_name="map_rounds",
        input_payload={},
        result=invalid_summary,
        tool_result=computation.result,
        provenance=round_map_module.record_provenance(computation.result),
    )
    assert publication == {
        "record_id": None,
        "record_status": "write_failed",
        "record_error": "record_invalid",
    }


def test_independent_computation_evidence_rejects_semantic_item_mutations(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 2), paragraph_index=1)
    _current_apply_record(
        matter,
        seed["paragraph_ref"]["file_sha256"],
        _seed(_round(matter, 3))["paragraph_ref"]["file_sha256"],
    )
    computation = build_round_map(str(matter), seed, max_items=100)
    assert computation.result["coverage"]["eligible_item_count"] == len(
        computation.result["items"]
    )

    def reidentify_relationship(item: dict) -> None:
        identity_basis = (
            round_map_module._RECORDED_BASIS_IDENTITY
            if item["relationship_type"] == "recorded_derivation"
            else item["basis"]
        )
        item["id"] = round_map_module._derived_id(
            "rm_rel_v1",
            {
                "schema_version": "relationship_identity.v1",
                "relationship_type": item["relationship_type"],
                "from_id": item["from_id"],
                "to_id": item["to_id"],
                "direction": item["direction"],
                "basis_identity": identity_basis,
            },
        )

    def zero_support(items: list[dict]) -> None:
        relationship = next(
            item
            for item in items
            if item.get("relationship_type") == "recorded_derivation"
        )
        relationship["basis"]["support_profile"] = "frozen_legacy_only"
        relationship["basis"]["supporting_records"] = {
            "count": 0,
            "current_count": 0,
            "published_v0_1_2_count": 0,
            "frozen_legacy_count": 0,
            "sha256": round_map_module._digest(
                {
                    "schema_version": "recorded_derivation_support.v1",
                    "records": [],
                }
            ),
            "sample": [],
            "truncated": False,
        }

    def unrelated_equality_hash(items: list[dict]) -> None:
        relationship = next(
            item
            for item in items
            if item.get("relationship_type") == "exact_content_equality"
        )
        relationship["basis"]["paragraph_text_sha256"] = "0" * 64
        reidentify_relationship(relationship)

    def duplicate_seed_role(items: list[dict]) -> None:
        candidate = next(
            item
            for item in items
            if item["item_type"] == "paragraph_node"
            and item["roles"] == ["exact_candidate"]
        )
        candidate["roles"] = ["seed"]

    def unrelated_navigation_signal(items: list[dict]) -> None:
        relationship = next(
            item
            for item in items
            if item.get("relationship_type") == "navigation_candidate"
        )
        relationship["basis"]["signals"][0]["value_sha256"] = "0" * 64
        reidentify_relationship(relationship)

    def assert_mutation_rejected(
        selected: round_map_module.RoundMapComputation,
        mutate,
    ) -> None:
        mutated_items = deepcopy(selected.result["items"])
        mutate(mutated_items)
        mutated_items.sort(
            key=lambda item: (
                round_map_module._TYPE_RANK[item["item_type"]],
                item["id"],
            )
        )
        mutated_proof = replace(
            selected.proof,
            item_fingerprints=round_map_module._freeze_item_fingerprints(mutated_items),
            full_result_set_sha256=round_map_module._streaming_item_set_digest(
                mutated_items
            ),
        )
        with pytest.raises(RoundMapError) as complete_error:
            round_map_module._validate_complete_items(mutated_items, mutated_proof)
        assert complete_error.value.code == "output_contract_error"

        mutated_result = deepcopy(selected.result)
        mutate(mutated_result["items"])
        normalized = server._validated_success_result("map_rounds", mutated_result)
        assert isinstance(normalized, dict)
        with pytest.raises(RoundMapError) as normalized_error:
            round_map_module.validate_computation_result(selected, normalized)
        assert normalized_error.value.code == "output_contract_error"

    for mutate in (zero_support, unrelated_equality_hash, duplicate_seed_role):
        assert_mutation_rejected(computation, mutate)

    navigation_seed = _seed(_round(matter, 2), paragraph_index=60)
    navigation_computation = build_round_map(
        str(matter), navigation_seed, max_items=100
    )
    assert_mutation_rejected(navigation_computation, unrelated_navigation_signal)


def test_semantic_mutation_fails_before_round_map_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    _current_apply_record(
        matter,
        seed["paragraph_ref"]["file_sha256"],
        _seed(_round(matter, 2))["paragraph_ref"]["file_sha256"],
    )
    computation = build_round_map(str(matter), seed, max_items=100)
    invalid_result = deepcopy(computation.result)
    relationship = next(
        item
        for item in invalid_result["items"]
        if item.get("relationship_type") == "recorded_derivation"
    )
    relationship["basis"]["support_profile"] = "frozen_legacy_only"
    relationship["basis"]["supporting_records"] = {
        "count": 0,
        "current_count": 0,
        "published_v0_1_2_count": 0,
        "frozen_legacy_count": 0,
        "sha256": round_map_module._digest(
            {
                "schema_version": "recorded_derivation_support.v1",
                "records": [],
            }
        ),
        "sample": [],
        "truncated": False,
    }
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    before = journal.read_bytes()
    monkeypatch.setattr(
        round_map_module,
        "build_round_map",
        lambda *_args, **_kwargs: replace(computation, result=invalid_result),
    )

    with pytest.raises(RoundMapError) as error:
        server.map_rounds(str(matter), seed, max_items=100)
    assert error.value.code == "output_contract_error"
    assert journal.read_bytes() == before


def test_digest_valid_tampered_summary_id_is_rejected_without_projection(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    server.map_rounds(str(matter), seed, max_items=100)
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    raw = json.loads(journal.read_text(encoding="utf-8"))
    sentinel = "PRIVATE/path/and-text/support-id"
    raw["result"]["items_summary"]["sample"][0]["id"] = sentinel
    raw["result_sha256"] = records._stable_digest(raw["result"])
    journal.write_text(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(records.DecisionRecordError) as read_error:
        records.read_records(matter, max_records=100, include_payload=True)
    assert read_error.value.code == "journal_corrupt"
    with pytest.raises(Exception) as compact_error:
        server.export_decision_record(str(matter), max_records=100)
    assert sentinel not in str(compact_error.value)


@pytest.mark.parametrize(
    ("item_type", "id_prefix"),
    [
        ("document_node", "rm_doc_v1"),
        ("document_observation", "rm_obs_v1"),
        ("paragraph_node", "rm_par_v1"),
        ("section_node", "rm_sec_v1"),
        ("relationship", "rm_rel_v1"),
        ("resolution", "rm_resolution_v1"),
        ("conflict", "rm_conflict_v1"),
    ],
)
@pytest.mark.parametrize("terminal", ["\n", "\r\n"])
def test_sampled_item_id_terminal_newlines_never_reach_raw_or_compact_projection(
    tmp_path: Path, item_type: str, id_prefix: str, terminal: str
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    server.map_rounds(str(matter), seed, max_items=100)
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    raw = json.loads(journal.read_text(encoding="utf-8"))
    sample = raw["result"]["items_summary"]["sample"][0]
    sample["item_type"] = item_type
    sentinel = f"{id_prefix}:" + ("a" * 64) + terminal
    sample["id"] = sentinel
    raw["result_sha256"] = records._stable_digest(raw["result"])
    journal.write_text(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(records.DecisionRecordError) as read_error:
        records.read_records(matter, max_records=100, include_payload=True)
    assert read_error.value.code == "journal_corrupt"
    with pytest.raises(Exception) as compact_error:
        server.export_decision_record(str(matter), max_records=100)
    assert sentinel not in str(compact_error.value)


def test_duplicate_exact_paragraphs_are_ambiguous_without_arbitrary_choice(
    tmp_path: Path,
) -> None:
    generated = _matter(tmp_path / "generated")
    matter = tmp_path / "duplicate-paragraphs"
    matter.mkdir()
    seed_path = matter / "01-seed.docx"
    candidate_path = matter / "02-candidate.docx"
    shutil.copyfile(_round(generated, 1), seed_path)
    shutil.copyfile(_round(generated, 2), candidate_path)
    _replace_body_text(seed_path, ["Exact clause Ω"])
    _replace_body_text(candidate_path, ["Exact clause Ω", "Exact clause Ω"])
    seed = _seed(seed_path)

    result = build_round_map(str(matter), seed, max_items=100).result
    candidate_id = "rm_doc_v1:" + _seed(candidate_path)["paragraph_ref"]["file_sha256"]
    resolution = next(
        item
        for item in _items(result, "resolution")
        if item["document_id"] == candidate_id
    )
    assert (
        resolution["state"],
        resolution["reason"],
        resolution["exact_candidate_count"],
    ) == ("ambiguous", "multiple_exact_candidates", 2)


def test_duplicate_navigation_labels_and_headings_preserve_all_candidates(
    tmp_path: Path,
) -> None:
    from lxml import etree

    generated = _matter(tmp_path / "generated")
    matter = tmp_path / "duplicate-navigation"
    matter.mkdir()
    seed_path = matter / "01-seed.docx"
    candidate_path = matter / "02-candidate.docx"
    shutil.copyfile(_round(generated, 1), seed_path)
    shutil.copyfile(_round(generated, 2), candidate_path)

    def replace_sections(path: Path, body_values: list[str]) -> None:
        def mutate(payload: bytes) -> bytes:
            document = parse_xml(payload)
            body = next(child for child in document if child.tag == w("body"))
            for child in list(body):
                body.remove(child)
            for body_value in body_values:
                heading = etree.SubElement(body, w("p"))
                properties = etree.SubElement(heading, w("pPr"))
                outline = etree.SubElement(properties, w("outlineLvl"))
                outline.set(w("val"), "0")
                heading_run = etree.SubElement(heading, w("r"))
                heading_text = etree.SubElement(heading_run, w("t"))
                heading_text.text = "1. Shared heading"
                paragraph = etree.SubElement(body, w("p"))
                run = etree.SubElement(paragraph, w("r"))
                text = etree.SubElement(run, w("t"))
                text.text = body_value
            return etree.tostring(document, xml_declaration=True, encoding="UTF-8")

        _rewrite_document_xml(path, mutate)

    replace_sections(seed_path, ["Unique seed body"])
    replace_sections(candidate_path, ["Candidate A", "Candidate B"])
    seed = _seed(seed_path, paragraph_index=1)
    result = build_round_map(str(matter), seed, max_items=100).result
    navigation = [
        item
        for item in _items(result, "relationship")
        if item["relationship_type"] == "navigation_candidate"
    ]
    candidate_document_id = (
        "rm_doc_v1:" + _seed(candidate_path)["paragraph_ref"]["file_sha256"]
    )
    candidate_sections = {
        item["id"]
        for item in _items(result, "section_node")
        if item["document_id"] == candidate_document_id
    }
    assert len(candidate_sections) == 2
    assert {item["to_id"] for item in navigation} == candidate_sections
    assert all(
        [signal["kind"] for signal in item["basis"]["signals"]]
        == ["label_exact_v1", "heading_exact_v1"]
        for item in navigation
    )
    assert all(item["from_id"] != item["to_id"] for item in navigation)
    assert not any(
        item["relationship_type"] == "exact_content_equality"
        and item["from_id"] == item["to_id"]
        for item in _items(result, "relationship")
    )
    resolution = next(
        item
        for item in _items(result, "resolution")
        if item["document_id"] == candidate_document_id
    )
    assert (
        resolution["state"],
        resolution["reason"],
        resolution["navigation_candidate_count"],
    ) == ("unresolved", "navigation_only", 2)
    assert set(resolution["candidate_ids"]["sample"]) == candidate_sections


def test_rename_changes_observation_identity_and_keeps_content_identity(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed_path = _round(matter, 1)
    renamed_from = _round(matter, 4)
    seed = _seed(seed_path)
    first_order = sorted(
        (path.name for path in matter.glob("*.docx")),
        key=lambda value: (value.casefold(), value),
    )
    first = build_round_map(
        str(matter), seed, ordered_filenames=first_order, max_items=100
    ).result
    renamed_to = matter / "renamed-current.docx"
    renamed_from.rename(renamed_to)
    second_order = [
        renamed_to.name if value == renamed_from.name else value
        for value in first_order
    ]
    second = build_round_map(
        str(matter), seed, ordered_filenames=second_order, max_items=100
    ).result

    first_content = {
        item["id"]: item
        for item in first["items"]
        if item["item_type"] != "document_observation"
    }
    second_content = {
        item["id"]: item
        for item in second["items"]
        if item["item_type"] != "document_observation"
    }
    assert first_content == second_content
    first_observations = {
        item["document_id"]: item for item in _items(first, "document_observation")
    }
    second_observations = {
        item["document_id"]: item for item in _items(second, "document_observation")
    }
    renamed_document_id = next(
        item["document_id"]
        for item in first_observations.values()
        if item["filename"] == renamed_from.name
    )
    assert (
        first_observations[renamed_document_id]["id"]
        != second_observations[renamed_document_id]["id"]
    )


def test_renamed_recorded_output_is_current_and_new_relevant_record_drifts_cursor(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    source = _round(matter, 1)
    output = _round(matter, 2)
    seed = _seed(source)
    output_sha = _seed(output)["paragraph_ref"]["file_sha256"]
    renamed = matter / "renamed-output.docx"
    output.rename(renamed)
    _current_apply_record(matter, seed["paragraph_ref"]["file_sha256"], output_sha)
    result = build_round_map(str(matter), seed, max_items=100).result
    output_node = next(
        item
        for item in _items(result, "document_node")
        if item["id"] == f"rm_doc_v1:{output_sha}"
    )
    assert output_node["observation_state"] == "current_and_recorded"

    fresh = _matter(tmp_path / "cursor")
    fresh_seed = _seed(_round(fresh, 1))
    first = build_round_map(str(fresh), fresh_seed, max_items=1).result
    _current_apply_record(fresh, fresh_seed["paragraph_ref"]["file_sha256"], "c" * 64)
    with pytest.raises(RoundMapError) as drift:
        build_round_map(
            str(fresh), fresh_seed, cursor=first["next_cursor"], max_items=1
        )
    assert drift.value.code == "cursor_mismatch"


def test_excluded_container_never_creates_exact_match_or_negative_whole_doc_claim(
    tmp_path: Path,
) -> None:
    generated = _matter(tmp_path / "generated")
    matter = tmp_path / "excluded-scope"
    matter.mkdir()
    seed_path = matter / "01-seed.docx"
    candidate_path = matter / "02-candidate.docx"
    shutil.copyfile(_round(generated, 1), seed_path)
    shutil.copyfile(_round(generated, 2), candidate_path)
    _replace_body_text(seed_path, ["Exact scoped clause Ω"])
    _replace_body_text(
        candidate_path,
        ["Different in-scope clause"],
        excluded_text="Exact scoped clause Ω",
    )
    seed = _seed(seed_path)
    result = build_round_map(str(matter), seed, max_items=100).result
    candidate_id = "rm_doc_v1:" + _seed(candidate_path)["paragraph_ref"]["file_sha256"]
    resolution = next(
        item
        for item in _items(result, "resolution")
        if item["document_id"] == candidate_id
    )
    assert (resolution["state"], resolution["reason"]) == (
        "unresolved",
        "declared_scope_incomplete",
    )
    assert resolution["exact_candidate_count"] == 0
    assert result["coverage"]["whole_docx_coverage"] is False
    assert result["coverage"]["negative_whole_doc_claims"] is False


def test_document_without_exact_or_navigation_candidate_is_unresolved(
    tmp_path: Path,
) -> None:
    generated = _matter(tmp_path / "generated")
    matter = tmp_path / "no-candidate"
    matter.mkdir()
    seed_path = matter / "01-seed.docx"
    candidate_path = matter / "02-candidate.docx"
    shutil.copyfile(_round(generated, 1), seed_path)
    shutil.copyfile(_round(generated, 2), candidate_path)
    _replace_body_text(seed_path, ["Unique seed clause"])
    _replace_body_text(candidate_path, ["Entirely different clause"])
    seed = _seed(seed_path)
    result = build_round_map(str(matter), seed, max_items=100).result
    candidate_id = "rm_doc_v1:" + _seed(candidate_path)["paragraph_ref"]["file_sha256"]
    resolution = next(
        item
        for item in _items(result, "resolution")
        if item["document_id"] == candidate_id
    )
    assert (
        resolution["state"],
        resolution["reason"],
        resolution["exact_candidate_count"],
        resolution["navigation_candidate_count"],
    ) == ("unresolved", "no_match_in_declared_scope", 0, 0)


def test_candidate_id_sample_uses_complete_digest_and_bounded_prefix(
    tmp_path: Path,
) -> None:
    generated = _matter(tmp_path / "generated")
    matter = tmp_path / "candidate-sample"
    matter.mkdir()
    seed_path = matter / "01-seed.docx"
    candidate_path = matter / "02-candidate.docx"
    shutil.copyfile(_round(generated, 1), seed_path)
    shutil.copyfile(_round(generated, 2), candidate_path)
    _replace_body_text(seed_path, ["Repeated exact clause"])
    _replace_body_text(candidate_path, ["Repeated exact clause"] * 21)
    seed = _seed(seed_path)
    result = build_round_map(str(matter), seed, max_items=100).result
    candidate_id = "rm_doc_v1:" + _seed(candidate_path)["paragraph_ref"]["file_sha256"]
    resolution = next(
        item
        for item in _items(result, "resolution")
        if item["document_id"] == candidate_id
    )
    summary = resolution["candidate_ids"]
    assert summary["count"] == 21
    assert len(summary["sample"]) == 20
    assert summary["truncated"] is True


def _schema_valid_70k_item_groups() -> tuple[list[dict], ...]:
    def sha(index: int) -> str:
        return f"{index:064x}"

    topology = {
        "multiple_parents": False,
        "cycle_member": False,
        "self_loop": False,
    }
    document_nodes = [
        {
            "schema_version": "round_map_item.v1",
            "item_type": "document_node",
            "id": f"rm_doc_v1:{sha(index)}",
            "file_sha256": sha(index),
            "observation_state": "record_only",
            "observation_count": 0,
            "inspection_coverage": None,
            "incoming_recorded_derivation_count": 0,
            "outgoing_recorded_derivation_count": 0,
            "topology_flags": topology,
        }
        for index in range(10_500)
    ]
    observations = [
        {
            "schema_version": "round_map_item.v1",
            "item_type": "document_observation",
            "id": f"rm_obs_v1:{sha(index)}",
            "document_id": f"rm_doc_v1:{sha(index)}",
            "path": f"/bounded/{index}.docx",
            "filename": f"{index}.docx",
            "position": index,
            "round_id": f"round-{index + 1:03d}",
            "position_basis": "filename_lexicographic_v1",
        }
        for index in range(500)
    ]
    paragraph_ref = {
        "schema_version": "paragraph_ref.v1",
        "ref_type": "paragraph",
        "file_sha256": "0" * 64,
        "part_name": "word/document.xml",
        "paragraph_index": 0,
        "paragraph_text_sha256": "1" * 64,
        "reading_mode": "accepted_current_v1",
        "container_policy": "canonical_body_flow_v1",
    }
    paragraphs = [
        {
            "schema_version": "round_map_item.v1",
            "item_type": "paragraph_node",
            "id": f"rm_par_v1:{sha(index)}",
            "document_id": "rm_doc_v1:" + ("0" * 64),
            "paragraph_ref": paragraph_ref,
            "container_kind": "body",
            "roles": ["exact_candidate"],
        }
        for index in range(10_001)
    ]
    section_ref = {
        "schema_version": "section_ref.v1",
        "ref_type": "section",
        "file_sha256": "0" * 64,
        "part_name": "word/document.xml",
        "heading_paragraph_index": 0,
        "heading_text_sha256": "1" * 64,
        "reading_mode": "accepted_current_v1",
        "container_policy": "canonical_body_flow_v1",
    }
    sections = [
        {
            "schema_version": "round_map_item.v1",
            "item_type": "section_node",
            "id": f"rm_sec_v1:{sha(index)}",
            "document_id": "rm_doc_v1:" + ("0" * 64),
            "section_ref": section_ref,
            "label": None,
            "heading": None,
            "level": 0,
            "basis": "word_outline_level_v1",
            "label_basis": None,
            "roles": ["candidate_navigation"],
        }
        for index in range(10_001)
    ]
    recorded_basis = {
        "schema_version": "recorded_derivation_basis.v1",
        "record_schema_version": "decision_record.v1",
        "tool_name": "apply_edits",
        "record_type": "decision.v1",
        "assurance": "best_effort_local_non_tamper_evident",
        "derivation_scope": "document_bytes_only",
        "support_profile": "current_only",
        "supporting_records": {
            "count": 1,
            "current_count": 1,
            "published_v0_1_2_count": 0,
            "frozen_legacy_count": 0,
            "sha256": "0" * 64,
            "sample": [
                {
                    "record_id": "dr_001",
                    "record_sha256": "0" * 64,
                    "profile": "current_v0.3",
                }
            ],
            "truncated": False,
        },
    }
    equality_basis = {
        "schema_version": "exact_content_equality_basis.v1",
        "reading_mode": "accepted_current_v1",
        "container_policy": "canonical_body_flow_v1",
        "part_name": "word/document.xml",
        "comparison": "complete_unicode_scalar_sequence_v1",
        "full_text_compared": True,
        "paragraph_text_sha256": "1" * 64,
    }
    navigation_basis = {
        "schema_version": "navigation_candidate_basis.v1",
        "signals": [{"kind": "label_exact_v1", "value_sha256": "1" * 64}],
        "evidence_class": "navigation_only",
    }

    def relationships(
        relationship_type: str,
        count: int,
        start: int,
        endpoint_prefix: str,
        direction: str,
        basis: dict,
        recorded: bool,
    ) -> list[dict]:
        return [
            {
                "schema_version": "round_map_item.v1",
                "item_type": "relationship",
                "id": f"rm_rel_v1:{sha(start + index)}",
                "relationship_type": relationship_type,
                "from_id": f"{endpoint_prefix}:{sha(0)}",
                "to_id": f"{endpoint_prefix}:{sha(1)}",
                "direction": direction,
                "basis": basis,
                "derivation_recorded": recorded,
                "lineage_verified": False,
                "chronology_verified": False,
            }
            for index in range(count)
        ]

    recorded = relationships(
        "recorded_derivation",
        10_000,
        0,
        "rm_doc_v1",
        "directed",
        recorded_basis,
        True,
    )
    equality = relationships(
        "exact_content_equality",
        10_000,
        10_000,
        "rm_par_v1",
        "symmetric",
        equality_basis,
        False,
    )
    navigation = relationships(
        "navigation_candidate",
        10_000,
        20_000,
        "rm_sec_v1",
        "directed",
        navigation_basis,
        False,
    )
    conflicts = [
        {
            "schema_version": "round_map_item.v1",
            "item_type": "conflict",
            "id": f"rm_conflict_v1:{sha(index)}",
            "conflict_type": "inconsistent_apply_record",
            "reason": "round_trip_fact_mismatch",
            "affected_document_ids": ["rm_doc_v1:" + ("0" * 64)],
            "record_sha256": sha(index),
            "edge_emitted": False,
        }
        for index in range(8_998)
    ]
    return (
        document_nodes,
        observations,
        paragraphs,
        sections,
        recorded,
        equality,
        navigation,
        conflicts,
    )


def test_real_item_finalizer_and_streaming_digest_accept_70000_refuse_70001() -> None:
    groups = _schema_valid_70k_item_groups()
    items = round_map_module._assemble_item_set(groups)
    assert len(items) == 70_000
    validator = Draft202012Validator(ROUND_MAP_ITEM_SCHEMA)
    assert all(validator.is_valid(item) for item in items)
    assert round_map_module._canonical_equal(items, items) is False
    digest = round_map_module._streaming_item_set_digest(items)
    assert len(digest) == 64
    with pytest.raises(records._JsonBoundaryError):
        records._stable_digest(
            {"schema_version": "round_map_item_set.v1", "items": items}
        )
    with pytest.raises(RoundMapError) as over:
        round_map_module._assemble_item_set((items, [deepcopy(items[0])]))
    assert over.value.code == "resource_limit_exceeded"


def _coherent_70k_computation(workspace: Path) -> round_map_module.RoundMapComputation:
    workspace.mkdir()

    def sha(index: int) -> str:
        return f"{index:064x}"

    document_shas = [sha(index + 1) for index in range(10_500)]
    document_ids = [f"rm_doc_v1:{value}" for value in document_shas]
    filenames = tuple(f"round-{index:03d}.docx" for index in range(500))
    inspection_coverage = {
        "schema_version": "round_map_inspection_coverage.v1",
        "scan_complete": True,
        "indexed_paragraph_count": 40,
        "nonempty_indexed_paragraph_count": 40,
        "included_parts": ["word/document.xml"],
        "excluded_parts": [],
        "included_containers": ["body", "table_cell"],
        "container_coverage": {
            "schema_version": "canonical_body_flow_v1",
            "indexed_paragraph_count": 40,
            "body_paragraph_count": 40,
            "table_cell_paragraph_count": 0,
            "excluded_subtree_count": 0,
            "excluded_paragraph_count": 0,
            "excluded_by_kind": {},
            "excluded_paragraphs_by_kind": {},
            "coverage_complete": True,
            "legacy_two_field_anchor_safe": True,
        },
    }

    observations: list[dict] = []
    for position, (filename, document_id) in enumerate(
        zip(filenames, document_ids[:500], strict=True)
    ):
        path = str(workspace / filename)
        observations.append(
            {
                "schema_version": "round_map_item.v1",
                "item_type": "document_observation",
                "id": round_map_module._derived_id(
                    "rm_obs_v1",
                    {
                        "schema_version": "document_observation_identity.v1",
                        "document_id": document_id,
                        "canonical_path": path,
                    },
                ),
                "document_id": document_id,
                "path": path,
                "filename": filename,
                "position": position,
                "round_id": f"round-{position + 1:03d}",
                "position_basis": "filename_lexicographic_v1",
            }
        )

    recorded_relationships: list[dict] = []
    recorded_evidence: list[round_map_module._RecordedEvidence] = []
    record_sha256s: list[str] = []
    for index, output_id in enumerate(document_ids[500:], start=1):
        record_sha256 = sha(20_000 + index)
        classification = round_map_module._ApplyClassification(
            kind="valid",
            record={"record_id": f"dr_{index:06d}"},
            record_sha256=record_sha256,
            source_id=document_ids[0],
            output_id=output_id,
            profile="current_v0.3",
        )
        relationship = round_map_module._recorded_relationship(
            document_ids[0], output_id, [classification]
        )
        recorded_relationships.append(relationship)
        recorded_evidence.append(
            round_map_module._RecordedEvidence(
                relationship_id=relationship["id"],
                source_id=document_ids[0],
                output_id=output_id,
                supporting_records=(
                    (f"dr_{index:06d}", record_sha256, "current_v0.3"),
                ),
            )
        )
        record_sha256s.append(record_sha256)

    paragraph_text_sha256 = "f" * 64

    def paragraph_ref(document_index: int, paragraph_index: int) -> dict:
        return {
            "schema_version": "paragraph_ref.v1",
            "ref_type": "paragraph",
            "file_sha256": document_shas[document_index],
            "part_name": "word/document.xml",
            "paragraph_index": paragraph_index,
            "paragraph_text_sha256": paragraph_text_sha256,
            "reading_mode": "accepted_current_v1",
            "container_policy": "canonical_body_flow_v1",
        }

    seed_ref = paragraph_ref(0, 0)
    seed_paragraph_id = round_map_module._derived_id("rm_par_v1", seed_ref)
    paragraphs = [
        {
            "schema_version": "round_map_item.v1",
            "item_type": "paragraph_node",
            "id": seed_paragraph_id,
            "document_id": document_ids[0],
            "paragraph_ref": seed_ref,
            "container_kind": "body",
            "roles": ["seed"],
        }
    ]
    paragraph_evidence = [
        round_map_module._ParagraphEvidence(
            paragraph_id=seed_paragraph_id,
            document_id=document_ids[0],
            paragraph_text_sha256=paragraph_text_sha256,
            role="seed",
        )
    ]
    exact_relationships: list[dict] = []
    exact_by_document: dict[str, set[str]] = {}
    for index in range(10_000):
        document_index = index % 500
        reference = paragraph_ref(document_index, index // 500 + 1)
        paragraph_id = round_map_module._derived_id("rm_par_v1", reference)
        paragraphs.append(
            {
                "schema_version": "round_map_item.v1",
                "item_type": "paragraph_node",
                "id": paragraph_id,
                "document_id": document_ids[document_index],
                "paragraph_ref": reference,
                "container_kind": "body",
                "roles": ["exact_candidate"],
            }
        )
        paragraph_evidence.append(
            round_map_module._ParagraphEvidence(
                paragraph_id=paragraph_id,
                document_id=document_ids[document_index],
                paragraph_text_sha256=paragraph_text_sha256,
                role="exact_candidate",
            )
        )
        exact_relationships.append(
            round_map_module._equality_relationship(
                seed_paragraph_id, paragraph_id, paragraph_text_sha256
            )
        )
        exact_by_document.setdefault(document_ids[document_index], set()).add(
            paragraph_id
        )

    heading_text_sha256 = "e" * 64
    label = "Clause Omega"
    label_sha256 = hashlib.sha256(label.encode("utf-8")).hexdigest()

    def section_ref(document_index: int, heading_index: int) -> dict:
        return {
            "schema_version": "section_ref.v1",
            "ref_type": "section",
            "file_sha256": document_shas[document_index],
            "part_name": "word/document.xml",
            "heading_paragraph_index": heading_index,
            "heading_text_sha256": heading_text_sha256,
            "reading_mode": "accepted_current_v1",
            "container_policy": "canonical_body_flow_v1",
        }

    seed_section_ref = section_ref(0, 0)
    seed_section_id = round_map_module._derived_id("rm_sec_v1", seed_section_ref)
    sections = [
        {
            "schema_version": "round_map_item.v1",
            "item_type": "section_node",
            "id": seed_section_id,
            "document_id": document_ids[0],
            "section_ref": seed_section_ref,
            "label": label,
            "heading": None,
            "level": 0,
            "basis": "word_outline_level_v1",
            "label_basis": "explicit_heading_text_v1",
            "roles": ["seed_navigation"],
        }
    ]
    section_evidence = [
        round_map_module._SectionEvidence(
            section_id=seed_section_id,
            document_id=document_ids[0],
            label=label,
            heading=None,
            role="seed_navigation",
        )
    ]
    navigation_relationships: list[dict] = []
    navigation_by_document: dict[str, set[str]] = {}
    for index in range(9_249):
        document_index = index % 500
        reference = section_ref(document_index, index // 500 + 1)
        section_id = round_map_module._derived_id("rm_sec_v1", reference)
        sections.append(
            {
                "schema_version": "round_map_item.v1",
                "item_type": "section_node",
                "id": section_id,
                "document_id": document_ids[document_index],
                "section_ref": reference,
                "label": label,
                "heading": None,
                "level": 0,
                "basis": "word_outline_level_v1",
                "label_basis": "explicit_heading_text_v1",
                "roles": ["candidate_navigation"],
            }
        )
        section_evidence.append(
            round_map_module._SectionEvidence(
                section_id=section_id,
                document_id=document_ids[document_index],
                label=label,
                heading=None,
                role="candidate_navigation",
            )
        )
        navigation_relationships.append(
            round_map_module._navigation_relationship(
                seed_section_id,
                section_id,
                [{"kind": "label_exact_v1", "value_sha256": label_sha256}],
            )
        )
        navigation_by_document.setdefault(document_ids[document_index], set()).add(
            section_id
        )

    topology = {
        "multiple_parents": False,
        "cycle_member": False,
        "self_loop": False,
    }
    document_nodes: list[dict] = []
    for index, document_id in enumerate(document_ids):
        current = index < 500
        source = index == 0
        document_nodes.append(
            {
                "schema_version": "round_map_item.v1",
                "item_type": "document_node",
                "id": document_id,
                "file_sha256": document_shas[index],
                "observation_state": (
                    "current_and_recorded"
                    if source
                    else "current"
                    if current
                    else "record_only"
                ),
                "observation_count": 1 if current else 0,
                "inspection_coverage": (
                    deepcopy(inspection_coverage) if current else None
                ),
                "incoming_recorded_derivation_count": 0 if current else 1,
                "outgoing_recorded_derivation_count": 10_000 if source else 0,
                "topology_flags": topology,
            }
        )

    resolutions: list[dict] = []
    for index, document_id in enumerate(document_ids):
        exact_ids = exact_by_document.get(document_id, set())
        navigation_ids = navigation_by_document.get(document_id, set())
        candidate_ids = sorted(exact_ids | navigation_ids)
        identity = {
            "schema_version": "resolution_identity.v1",
            "seed_paragraph_id": seed_paragraph_id,
            "document_id": document_id,
        }
        resolutions.append(
            {
                "schema_version": "round_map_item.v1",
                "item_type": "resolution",
                "id": round_map_module._derived_id("rm_resolution_v1", identity),
                "seed_paragraph_id": seed_paragraph_id,
                "document_id": document_id,
                "state": "ambiguous" if index < 500 else "unresolved",
                "reason": (
                    "multiple_exact_candidates"
                    if index < 500
                    else "record_only_document"
                ),
                "exact_candidate_count": len(exact_ids),
                "navigation_candidate_count": len(navigation_ids),
                "conflict_count": 0,
                "candidate_ids": {
                    "count": len(candidate_ids),
                    "sha256": round_map_module._digest(candidate_ids),
                    "sample": candidate_ids[: ROUND_MAP_LIMITS["sample_items"]],
                    "truncated": len(candidate_ids) > ROUND_MAP_LIMITS["sample_items"],
                },
            }
        )

    all_items = round_map_module._assemble_item_set(
        (
            document_nodes,
            observations,
            paragraphs,
            sections,
            recorded_relationships,
            exact_relationships,
            navigation_relationships,
            resolutions,
        )
    )
    assert len(all_items) == 70_000
    item_type_counts = {
        item_type: sum(item["item_type"] == item_type for item in all_items)
        for item_type in round_map_module._TYPE_RANK
    }
    relationship_counts = {
        relationship_type: sum(
            item["item_type"] == "relationship"
            and item["relationship_type"] == relationship_type
            for item in all_items
        )
        for relationship_type in (
            "recorded_derivation",
            "exact_content_equality",
            "navigation_candidate",
        )
    }
    resolution_counts = {
        state: sum(
            item["item_type"] == "resolution" and item["state"] == state
            for item in all_items
        )
        for state in ("exact_unique", "ambiguous", "unresolved")
    }
    ordering_source = "filename_lexicographic_v1"
    manifest_sha256 = round_map_module._digest(
        {
            "schema_version": "round_map_filename_manifest.v1",
            "ordering_source": ordering_source,
            "filenames": list(filenames),
        }
    )
    filesystem_snapshot_sha256 = round_map_module._digest(
        {
            "schema_version": "round_map_filesystem_snapshot.v1",
            "filename_manifest_sha256": manifest_sha256,
            "observations": [
                {
                    "observation_id": observation["id"],
                    "canonical_path": observation["path"],
                    "filename": observation["filename"],
                    "position": observation["position"],
                    "byte_length": 1,
                    "file_sha256": document_shas[position],
                    "inspection_coverage_sha256": round_map_module._digest(
                        inspection_coverage
                    ),
                }
                for position, observation in enumerate(observations)
            ],
        }
    )
    journal_snapshot_sha256 = round_map_module._digest(
        {
            "schema_version": "round_map_relevant_journal_snapshot.v1",
            "record_sha256s": sorted(record_sha256s),
        }
    )
    full_result_set_sha256 = round_map_module._streaming_item_set_digest(all_items)
    proof = round_map_module._RoundMapProof(
        item_fingerprints=round_map_module._freeze_item_fingerprints(all_items),
        evidence=round_map_module._ComputationEvidence(
            recorded_relationships=tuple(
                sorted(recorded_evidence, key=lambda fact: fact.relationship_id)
            ),
            paragraphs=tuple(
                sorted(paragraph_evidence, key=lambda fact: fact.paragraph_id)
            ),
            sections=tuple(sorted(section_evidence, key=lambda fact: fact.section_id)),
        ),
        full_result_set_sha256=full_result_set_sha256,
        item_type_counts=tuple(sorted(item_type_counts.items())),
        relationship_counts=tuple(sorted(relationship_counts.items())),
        resolution_counts=tuple(sorted(resolution_counts.items())),
        record_only_document_count=10_000,
        filenames=filenames,
        folder=str(workspace),
        seed_path=str(workspace / filenames[0]),
        seed_ref=seed_ref,
        seed_document_id=document_ids[0],
        seed_paragraph_id=seed_paragraph_id,
        ordering_source=ordering_source,
        filename_manifest_sha256=manifest_sha256,
        filesystem_snapshot_sha256=filesystem_snapshot_sha256,
        journal_snapshot_sha256=journal_snapshot_sha256,
        relevant_apply_record_count=10_000,
        eligible_derivation_record_count=10_000,
        rejected_semantic_record_count=0,
    )
    page = all_items[:100]
    next_offset = len(page)
    next_cursor = f"rm1:{next_offset}:" + round_map_module._cursor_binding(
        next_offset=next_offset,
        folder=str(workspace),
        seed_path=str(workspace / filenames[0]),
        seed_ref=seed_ref,
        filenames=list(filenames),
        seed_document_id=document_ids[0],
        seed_paragraph_id=seed_paragraph_id,
        ordering_source=ordering_source,
        filename_manifest_sha256=manifest_sha256,
        filesystem_snapshot_sha256=filesystem_snapshot_sha256,
        journal_snapshot_sha256=journal_snapshot_sha256,
        full_result_set_sha256=full_result_set_sha256,
    )
    result = {
        "schema_version": "round_map.v1",
        "status": "ok",
        "seed": {
            "document_id": document_ids[0],
            "paragraph_id": seed_paragraph_id,
            "paragraph_ref": seed_ref,
        },
        "ordering_source": ordering_source,
        "order_basis": {
            "kind": "filename",
            "rule": "casefold_then_exact",
            "lineage_verified": False,
            "round_id_semantics": "position_only",
            "filename_manifest_sha256": manifest_sha256,
        },
        "snapshot": {
            "schema_version": "round_map_snapshot.v1",
            "filesystem_snapshot_sha256": filesystem_snapshot_sha256,
            "journal_snapshot_sha256": journal_snapshot_sha256,
            "journal_state": "relevant_apply_records_present",
            "full_result_set_sha256": full_result_set_sha256,
            "filesystem_cross_file_atomic": False,
            "cross_source_atomic": False,
        },
        "items": page,
        "coverage": {
            "scan_complete": True,
            "candidate_document_count": 500,
            "inspected_document_count": 500,
            "record_only_document_count": 10_000,
            "relevant_apply_record_count": 10_000,
            "eligible_derivation_record_count": 10_000,
            "rejected_semantic_record_count": 0,
            "eligible_item_count": 70_000,
            "returned_item_count": 100,
            "cursor_offset": 0,
            "output_truncated": True,
            "relationship_counts": relationship_counts,
            "resolution_counts": resolution_counts,
            "item_type_counts": item_type_counts,
            "search_scope": "word_document_xml_body_v1",
            "reading_mode": "accepted_current_v1",
            "container_policy": "canonical_body_flow_v1",
            "whole_docx_coverage": False,
            "negative_whole_doc_claims": False,
        },
        "limits": deepcopy(ROUND_MAP_LIMITS),
        "next_cursor": next_cursor,
    }
    computation = round_map_module.RoundMapComputation(
        result=result,
        workspace=workspace,
        proof=proof,
    )
    round_map_module._validate_complete_items(all_items, proof)
    round_map_module.validate_computation_result(computation, result)
    return computation


def test_coherent_70000_complete_map_validates_and_publishes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "coherent-70000"
    computation = _coherent_70k_computation(workspace)
    normalized = server._validated_success_result("map_rounds", computation.result)
    assert isinstance(normalized, dict)
    round_map_module.validate_computation_result(computation, normalized)

    monkeypatch.setattr(
        round_map_module,
        "build_round_map",
        lambda *_args, **_kwargs: computation,
    )
    published = server.map_rounds(
        str(workspace),
        {
            "schema_version": "round_map_seed.v1",
            "path": computation.proof.seed_path,
            "paragraph_ref": computation.proof.seed_ref,
        },
        max_items=100,
    )
    assert published["coverage"]["eligible_item_count"] == 70_000
    assert published["record_status"] == "written"
    records_page = records.read_records(workspace, max_records=10)
    assert records_page["total_count"] == 1


def test_70001_refuses_before_item_fingerprinting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def unexpected(_item):
        nonlocal called
        called = True
        raise AssertionError("fingerprinting must not begin above the aggregate cap")

    monkeypatch.setattr(round_map_module, "_item_fingerprint", unexpected)
    with pytest.raises(RoundMapError) as error:
        round_map_module._freeze_item_fingerprints(
            [{}] * (ROUND_MAP_LIMITS["total_map_items"] + 1)
        )
    assert error.value.code == "resource_limit_exceeded"
    assert called is False


_HERE = "tests/test_round_map.py"

# Each entry binds one frozen clause to an executable test and to fragments
# that must occur together inside a real ``assert`` expression in that test.
# The ratchet therefore fails if a test is merely renamed/left present while
# its material acceptance assertion is removed.
ACCEPTANCE_FIXTURE_CLAUSE_EVIDENCE = {
    1: (
        (
            "current-edge-pair",
            _HERE,
            "test_current_apply_record_creates_only_a_document_recorded_derivation",
            ("edge['from_id']", "source_sha"),
        ),
        (
            "current-support-profile",
            _HERE,
            "test_current_apply_record_creates_only_a_document_recorded_derivation",
            ("support_profile", "current_only"),
        ),
    ),
    2: (
        (
            "published-profile",
            _HERE,
            "test_preflightless_published_profile_and_strengthened_hybrid_conflict",
            ("support_profile", "published_v0_1_2_only"),
        ),
        (
            "frozen-profile-full-map",
            _HERE,
            "test_frozen_golden_profile_full_map_conflict_changes_both_bound_digests",
            ("support_profile", "frozen_legacy_only"),
        ),
        (
            "legacy-conflict-digest-consequence",
            _HERE,
            "test_frozen_golden_profile_full_map_conflict_changes_both_bound_digests",
            ("full_result_set_sha256", "conflicted"),
        ),
    ),
    3: (
        (
            "failed-and-preflight-no-edge",
            _HERE,
            "test_failed_apply_and_successful_preflight_create_no_derivation",
            ("not any", "recorded_derivation"),
        ),
    ),
    4: (
        (
            "divergent-output-conflict",
            _HERE,
            "test_divergent_output_conflict_affects_only_the_current_endpoint",
            ("affected_document_ids", "current_sha"),
        ),
        (
            "conflict-resolution",
            _HERE,
            "test_divergent_output_conflict_affects_only_the_current_endpoint",
            ("resolution['state']", "resolution['reason']"),
        ),
    ),
    5: (
        (
            "exact-only-not-lineage",
            _HERE,
            "test_exact_equality_and_navigation_never_become_lineage_or_chronology",
            ("relationship['lineage_verified']", "False"),
        ),
        (
            "paragraph-and-section-self-edge-exclusion",
            _HERE,
            "test_duplicate_navigation_labels_and_headings_preserve_all_candidates",
            ("item['from_id'] == item['to_id']", "not any"),
        ),
    ),
    6: (
        (
            "hash-does-not-bypass-text",
            _HERE,
            "test_equal_hash_signal_is_not_trusted_without_full_text_comparison",
            ("error.value.code", "evidence_consistency_error"),
        ),
    ),
    7: (
        (
            "position-only-order",
            _HERE,
            "test_explicit_manifest_and_mtimes_remain_position_only",
            ("first['order_basis']", "second['order_basis']"),
        ),
        (
            "no-chronology-lineage",
            _HERE,
            "test_explicit_manifest_and_mtimes_remain_position_only",
            ("relationship['lineage_verified']", "relationship['chronology_verified']"),
        ),
    ),
    8: (
        (
            "duplicate-paragraph-ambiguity",
            _HERE,
            "test_duplicate_exact_paragraphs_are_ambiguous_without_arbitrary_choice",
            ("multiple_exact_candidates", "2"),
        ),
        (
            "duplicate-navigation-preserved",
            _HERE,
            "test_duplicate_navigation_labels_and_headings_preserve_all_candidates",
            ("item['to_id']", "candidate_sections"),
        ),
    ),
    9: (
        (
            "single-navigation-unresolved",
            _HERE,
            "test_single_navigation_candidate_remains_unresolved",
            ("resolution['state']", "unresolved"),
        ),
        (
            "multiple-navigation-unresolved",
            _HERE,
            "test_navigation_only_candidates_remain_unresolved",
            ("all", "item['state']", "unresolved"),
        ),
    ),
    10: (
        (
            "no-candidate-unresolved-not-deleted",
            _HERE,
            "test_document_without_exact_or_navigation_candidate_is_unresolved",
            ("no_match_in_declared_scope", "0"),
        ),
    ),
    11: (
        (
            "duplicate-bytes-one-node-two-observations",
            _HERE,
            "test_duplicate_bytes_collapse_document_and_paragraph_identity",
            ("node['observation_count']", "2"),
        ),
    ),
    12: (
        (
            "rename-content-stable",
            _HERE,
            "test_rename_changes_observation_identity_and_keeps_content_identity",
            ("first_content", "second_content"),
        ),
        (
            "rename-observation-changes",
            _HERE,
            "test_rename_changes_observation_identity_and_keeps_content_identity",
            ("first_observations", "second_observations", "!="),
        ),
    ),
    13: (
        (
            "branch-exact-edge-pairs",
            _HERE,
            "test_branch_cycle_self_loop_multiple_parents_and_duplicate_support_are_preserved",
            ("item['from_id']", "item['to_id']", "derivations"),
        ),
    ),
    14: (
        (
            "multiple-parent-flags",
            _HERE,
            "test_branch_cycle_self_loop_multiple_parents_and_duplicate_support_are_preserved",
            ("multiple_parents", "True"),
        ),
    ),
    15: (
        (
            "duplicate-support-digest",
            _HERE,
            "test_branch_cycle_self_loop_multiple_parents_and_duplicate_support_are_preserved",
            ("support['sha256']", "recorded_derivation_support.v1"),
        ),
    ),
    16: (
        (
            "cycle-and-self-loop-flags",
            _HERE,
            "test_branch_cycle_self_loop_multiple_parents_and_duplicate_support_are_preserved",
            ("cycle_member", "self_loop"),
        ),
    ),
    17: (
        (
            "renamed-output-current-recorded",
            _HERE,
            "test_renamed_recorded_output_is_current_and_new_relevant_record_drifts_cursor",
            ("observation_state", "current_and_recorded"),
        ),
    ),
    18: (
        (
            "nonseed-cursor-drift",
            _HERE,
            "test_nonseed_drift_invalidates_cursor_but_seed_drift_has_specific_error",
            ("changed.value.code", "cursor_mismatch"),
        ),
        (
            "seed-drift-specific",
            _HERE,
            "test_nonseed_drift_invalidates_cursor_but_seed_drift_has_specific_error",
            ("stale.value.code", "file_sha256_mismatch"),
        ),
        (
            "missing-seed-compound-precedence",
            _HERE,
            "test_later_page_missing_seed_and_journal_compound_failures_keep_phase_order",
            ("missing_error.value.code", "seed_not_candidate"),
        ),
        (
            "in-call-set-drift",
            _HERE,
            "test_workspace_set_drift_and_candidate_count_boundary",
            ("final_growth.value.code", "workspace_changed"),
        ),
    ),
    19: (
        (
            "own-record-does-not-drift",
            _HERE,
            "test_pagination_allows_page_size_change_and_ignores_own_records",
            ("journal_snapshot_sha256", "first", "second"),
        ),
        (
            "corruption-precedes-cursor",
            _HERE,
            "test_later_page_missing_seed_and_journal_compound_failures_keep_phase_order",
            ("corrupt_error.value.code", "journal_corrupt"),
        ),
    ),
    20: (
        (
            "root-contention",
            _HERE,
            "test_journal_snapshot_contention_is_fail_closed",
            ("error.value.code", "journal_busy"),
        ),
        (
            "exact-journal-byte-boundary",
            _HERE,
            "test_exact_64_mib_aggregate_journal_is_accepted_and_one_byte_over_refuses",
            ("journal.stat().st_size", "67108864"),
        ),
        (
            "journal-one-over",
            _HERE,
            "test_exact_64_mib_aggregate_journal_is_accepted_and_one_byte_over_refuses",
            ("error.value.code", "journal_oversize"),
        ),
        (
            "post-result-write-failed",
            _HERE,
            "test_round_map_success_writes_once_and_append_failure_is_fail_open",
            ("failed_append['status']", "failed_append['record_status']"),
        ),
    ),
    21: (
        (
            "real-hostile-package-codes",
            _HERE,
            "test_real_hostile_docx_packages_refuse_the_complete_map",
            ("error.value.code", "expected_code"),
        ),
        (
            "malformed-ooxml-normalization",
            _HERE,
            "test_round_map_normalizes_main_body_structure_but_not_archive_ambiguity",
            ("error.value.code", "invalid_docx"),
        ),
        (
            "unsafe-candidates",
            _HERE,
            "test_candidate_symlink_and_hardlink_fail_closed",
            ("unsafe_hardlink.value.code", "unsafe_candidate"),
        ),
    ),
    22: (
        (
            "excluded-scope-no-false-match",
            _HERE,
            "test_excluded_container_never_creates_exact_match_or_negative_whole_doc_claim",
            ("resolution['exact_candidate_count']", "0"),
        ),
        (
            "no-negative-whole-doc-claim",
            _HERE,
            "test_excluded_container_never_creates_exact_match_or_negative_whole_doc_claim",
            ("negative_whole_doc_claims", "False"),
        ),
    ),
    23: (
        (
            "coherent-70000-published",
            _HERE,
            "test_coherent_70000_complete_map_validates_and_publishes_once",
            ("eligible_item_count", "70000"),
        ),
        (
            "70001-before-fingerprinting",
            _HERE,
            "test_70001_refuses_before_item_fingerprinting",
            ("called", "False"),
        ),
        (
            "all-item-class-caps",
            _HERE,
            "test_every_frozen_item_class_cap_is_inclusive_and_refuses_one_over",
            ("error.value.code", "resource_limit_exceeded"),
        ),
        (
            "support-sample-21",
            _HERE,
            "test_support_sample_is_bounded_without_dropping_duplicate_evidence",
            ("support['count']", "21"),
        ),
        (
            "defaults-and-cancellation",
            _HERE,
            "test_default_page_size_and_cancellation_never_create_partial_success",
            ("records.SIDECAR_DIR", "not"),
        ),
    ),
    24: (
        (
            "pagination-union-exhaustion",
            _HERE,
            "test_pagination_exhaustion_has_exact_union_without_overlap_or_omission",
            ("len(set(returned_ids))", "eligible_count"),
        ),
        (
            "pagination-no-overlap",
            _HERE,
            "test_pagination_exhaustion_has_exact_union_without_overlap_or_omission",
            ("isdisjoint", "page_ids"),
        ),
    ),
    25: (
        (
            "compact-sensitive-sentinels",
            _HERE,
            "test_compact_export_omits_navigation_body_edit_and_verbatim_input_sentinels",
            ("sentinel", "not in encoded"),
        ),
        (
            "sampled-id-newline-corruption",
            _HERE,
            "test_sampled_item_id_terminal_newlines_never_reach_raw_or_compact_projection",
            ("read_error.value.code", "journal_corrupt"),
        ),
    ),
    26: (
        (
            "golden-journal-regression-workflow",
            "tests/test_decision_records.py",
            "test_golden_v1_journal_stays_readable_and_appendable",
            ("full['records']", "stored_records"),
        ),
        (
            "eight-tool-installed-wheel-workflow",
            "tests/test_release_workflow.py",
            "test_development_wheel_smoke_binds_metadata_and_eight_tool_surface",
            ("map_rounds", "smoke"),
        ),
        (
            "package-reproducibility-workflow",
            "tests/test_reproducible_build.py",
            "test_rebuild_comparison_requires_exact_names_and_bytes",
            ("ReproducibilityError", "bytes differ"),
        ),
        (
            "package-stage-workflow",
            "tests/test_mcpb_package.py",
            "test_stage_is_complete_and_refuses_nonempty_destination",
            ("staged", "MCPB_MEMBERS"),
        ),
    ),
}

DESKTOP_READINESS_CLAUSE_EVIDENCE = (
    (
        "english-equality-and-derivation",
        _HERE,
        "test_round_map_desktop_fixture_text_is_english_and_unicode_safe",
        ("recorded_derivation", "relationship_types"),
    ),
    (
        "explicit-unresolved-or-ambiguous",
        _HERE,
        "test_round_map_desktop_fixture_text_is_english_and_unicode_safe",
        ("unresolved", "ambiguous", "any"),
    ),
    (
        "unicode-round-trip",
        _HERE,
        "test_round_map_desktop_fixture_text_is_english_and_unicode_safe",
        ("encode('utf-8')", "decode('utf-8')"),
    ),
)


def _assert_clause_evidence(
    entries: tuple[tuple[str, str, str, tuple[str, ...]], ...],
) -> None:
    root = Path(__file__).parents[1]
    parsed: dict[str, dict[str, ast.FunctionDef]] = {}
    for clause_id, relative_path, test_name, fragments in entries:
        if relative_path not in parsed:
            module = ast.parse((root / relative_path).read_text(encoding="utf-8"))
            parsed[relative_path] = {
                node.name: node
                for node in module.body
                if isinstance(node, ast.FunctionDef)
            }
        function = parsed[relative_path].get(test_name)
        assert function is not None, f"{clause_id}: missing executable {test_name}"
        assertions = [
            ast.unparse(node.test)
            if isinstance(node, ast.Assert)
            else ast.unparse(node)
            for node in ast.walk(function)
            if isinstance(node, (ast.Assert, ast.With))
        ]
        assert any(
            all(fragment in assertion for fragment in fragments)
            for assertion in assertions
        ), f"{clause_id}: material assertion fragments missing: {fragments}"


def test_acceptance_fixtures_have_clause_level_executable_evidence() -> None:
    assert set(ACCEPTANCE_FIXTURE_CLAUSE_EVIDENCE) == set(range(1, 27))
    entries = tuple(
        evidence
        for fixture_evidence in ACCEPTANCE_FIXTURE_CLAUSE_EVIDENCE.values()
        for evidence in fixture_evidence
    )
    clause_ids = [entry[0] for entry in entries]
    assert len(clause_ids) == len(set(clause_ids))
    _assert_clause_evidence(entries)


def test_desktop_fixture_27_has_separate_automated_readiness_evidence() -> None:
    _assert_clause_evidence(DESKTOP_READINESS_CLAUSE_EVIDENCE)


def test_round_map_desktop_fixture_text_is_english_and_unicode_safe(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 2), paragraph_index=1)
    source_sha = seed["paragraph_ref"]["file_sha256"]
    output_sha = _seed(_round(matter, 3))["paragraph_ref"]["file_sha256"]
    _current_apply_record(matter, source_sha, output_sha)
    result = build_round_map(str(matter), seed, max_items=100).result
    encoded = json.dumps(result, ensure_ascii=False)
    relationship_types = {
        item["relationship_type"] for item in _items(result, "relationship")
    }
    assert "recorded_derivation" in relationship_types
    assert "exact_content_equality" in relationship_types
    assert any(
        item["state"] in {"unresolved", "ambiguous"}
        for item in _items(result, "resolution")
    )
    assert "lineage_verified" in encoded
    assert "chronology_verified" in encoded
    assert encoded.encode("utf-8").decode("utf-8") == encoded
    assert not any(0xD800 <= ord(character) <= 0xDFFF for character in encoded)
