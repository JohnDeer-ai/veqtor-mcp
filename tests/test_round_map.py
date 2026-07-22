# SPDX-License-Identifier: Apache-2.0
"""Stage 3B bounded Round Map core, journal, cursor and privacy acceptance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import zipfile
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from veqtor_docx import generate_demo_rounds, inspect_document
from veqtor_docx._ooxml import (
    EncryptedDocxError,
    UnsupportedCompressionError,
    parse_xml,
    w,
)
from veqtor_mcp import records, server
from veqtor_mcp import round_map as round_map_module
from veqtor_mcp.round_map_contract import (
    ROUND_MAP_RESULT_PROPERTIES,
    ROUND_MAP_RESULT_REQUIRED,
)
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


def test_later_page_candidate_set_and_oversized_replacement_keep_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added = _matter(tmp_path / "added")
    added_seed = _seed(_round(added, 1))
    added_first = build_round_map(str(added), added_seed, max_items=1).result
    shutil.copyfile(_round(added, 2), added / "round-added.docx")
    with pytest.raises(RoundMapError) as added_error:
        build_round_map(
            str(added), added_seed, cursor=added_first["next_cursor"], max_items=1
        )
    assert added_error.value.code == "cursor_mismatch"

    removed = _matter(tmp_path / "removed")
    removed_seed = _seed(_round(removed, 1))
    removed_first = build_round_map(str(removed), removed_seed, max_items=1).result
    _round(removed, 4).unlink()
    with pytest.raises(RoundMapError) as removed_error:
        build_round_map(
            str(removed),
            removed_seed,
            cursor=removed_first["next_cursor"],
            max_items=1,
        )
    assert removed_error.value.code == "cursor_mismatch"

    oversized = _matter(tmp_path / "oversized")
    oversized_seed = _seed(_round(oversized, 1))
    oversized_first = build_round_map(
        str(oversized), oversized_seed, max_items=1
    ).result
    replacement = _round(oversized, 2)
    exact_size = replacement.stat().st_size
    with replacement.open("ab") as handle:
        handle.write(b"x")
    monkeypatch.setitem(ROUND_MAP_LIMITS, "compressed_bytes_per_docx", exact_size)
    with pytest.raises(RoundMapError) as oversized_error:
        build_round_map(
            str(oversized),
            oversized_seed,
            cursor=oversized_first["next_cursor"],
            max_items=1,
        )
    assert oversized_error.value.code == "resource_limit_exceeded"


def test_later_page_ignores_new_valid_non_apply_record(tmp_path: Path) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    first = build_round_map(str(matter), seed, max_items=1).result
    assert first["next_cursor"] is not None
    written = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={},
        result={"status": "ok"},
        provenance={},
    )
    assert written["record_status"] == "written"
    second = build_round_map(
        str(matter), seed, cursor=first["next_cursor"], max_items=1
    ).result
    assert (
        second["snapshot"]["journal_snapshot_sha256"]
        == first["snapshot"]["journal_snapshot_sha256"]
    )
    assert second["coverage"]["cursor_offset"] == 1


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


@pytest.mark.parametrize("failed_lock", [1, 2])
def test_root_and_journal_snapshot_contention_are_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_lock: int,
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
    calls = 0

    @contextmanager
    def busy(fd, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == failed_lock:
            raise records.DecisionRecordError("journal_busy", "simulated contention")
        yield

    monkeypatch.setattr(records, "_bounded_journal_lock", busy)
    with pytest.raises(RoundMapError) as error:
        build_round_map(str(matter), seed)
    assert error.value.code == "journal_busy"
    assert calls == failed_lock


def test_exact_64_mib_aggregate_journal_is_accepted_and_one_byte_over_refuses(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    first = build_round_map(str(matter), seed, max_items=1).result
    assert first["next_cursor"] is not None
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

    accepted = build_round_map(
        str(matter), seed, cursor=first["next_cursor"], max_items=1
    ).result
    assert accepted["snapshot"]["journal_state"] == "no_relevant_apply_records"
    assert accepted["coverage"]["cursor_offset"] == 1
    with journal.open("ab") as handle:
        handle.write(b" ")
    assert journal.stat().st_size == 67_108_865
    with pytest.raises(RoundMapError) as error:
        build_round_map(str(matter), seed, cursor=first["next_cursor"], max_items=1)
    assert error.value.code == "journal_oversize"


def test_total_item_limit_accepts_boundary_and_refuses_one_over() -> None:
    limit = ROUND_MAP_LIMITS["total_map_items"]
    round_map_module._enforce_resource_boundary(limit, "total_map_items", "sentinel")
    with pytest.raises(RoundMapError) as error:
        round_map_module._enforce_resource_boundary(
            limit + 1, "total_map_items", "sentinel"
        )
    assert error.value.code == "resource_limit_exceeded"


def _authority_at_item_class_count(
    limit_key: str,
    observed: int,
) -> round_map_module._SourceEvidence:
    def sha(index: int) -> str:
        return f"{index:064x}"

    def document_id(index: int) -> str:
        return f"rm_doc_v1:{sha(index + 1)}"

    inspection_coverage = round_map_module._freeze_json(
        {
            "schema_version": "round_map_inspection_coverage.v1",
            "scan_complete": True,
            "indexed_paragraph_count": 1,
            "nonempty_indexed_paragraph_count": 1,
            "included_parts": ["word/document.xml"],
            "excluded_parts": [],
            "included_containers": ["body", "table_cell"],
            "container_coverage": {
                "schema_version": "canonical_body_flow_v1",
                "indexed_paragraph_count": 1,
                "body_paragraph_count": 1,
                "table_cell_paragraph_count": 0,
                "excluded_subtree_count": 0,
                "excluded_paragraph_count": 0,
                "excluded_by_kind": {},
                "excluded_paragraphs_by_kind": {},
                "coverage_complete": True,
                "legacy_two_field_anchor_safe": True,
            },
        }
    )

    observations: list[round_map_module._ObservationEvidence] = []
    recorded: list[round_map_module._RecordedEvidence] = []
    paragraphs: list[round_map_module._ParagraphEvidence] = []
    sections: list[round_map_module._SectionEvidence] = []
    conflicts: list[round_map_module._ConflictEvidence] = []

    if limit_key in {"document_nodes", "resolution_items"}:
        observation_count = min(observed, ROUND_MAP_LIMITS["document_observations"])
    elif limit_key == "document_observations":
        observation_count = observed
    else:
        observation_count = 1

    for index in range(observation_count):
        observed_document_index = (
            index if limit_key in {"document_nodes", "resolution_items"} else 0
        )
        canonical_path = f"/synthetic/round-{index:03d}.docx"
        observed_document_id = document_id(observed_document_index)
        observations.append(
            round_map_module._ObservationEvidence(
                observation_id=round_map_module._derived_id(
                    "rm_obs_v1",
                    {
                        "schema_version": "document_observation_identity.v1",
                        "document_id": observed_document_id,
                        "canonical_path": canonical_path,
                    },
                ),
                document_id=observed_document_id,
                canonical_path=canonical_path,
                filename=f"round-{index:03d}.docx",
                position=index,
                byte_length=1,
                file_sha256=sha(observed_document_index + 1),
                inspection_coverage_json=inspection_coverage,
            )
        )

    def recorded_relationship(
        index: int,
        source_id: str,
        output_id: str,
    ) -> round_map_module._RecordedEvidence:
        return round_map_module._RecordedEvidence(
            relationship_id=round_map_module._relationship_id(
                "recorded_derivation",
                source_id,
                output_id,
                "directed",
                round_map_module._RECORDED_BASIS_IDENTITY,
            ),
            source_id=source_id,
            output_id=output_id,
            supporting_records=(
                (f"dr_{index + 1:06d}", sha(100_000 + index), "current_v0.3"),
            ),
        )

    if limit_key in {"document_nodes", "resolution_items"}:
        remaining = observed - observation_count
        paired = remaining // 2
        for index in range(paired):
            source_index = observation_count + (index * 2)
            recorded.append(
                recorded_relationship(
                    index,
                    document_id(source_index),
                    document_id(source_index + 1),
                )
            )
        if remaining % 2:
            recorded.append(
                recorded_relationship(
                    paired,
                    document_id(0),
                    document_id(observation_count + (paired * 2)),
                )
            )
    elif limit_key == "recorded_derivation_relationships":
        recorded.extend(
            recorded_relationship(index, document_id(0), document_id(index + 1))
            for index in range(observed)
        )

    paragraph_count = (
        observed
        if limit_key == "paragraph_nodes"
        else observed + 1
        if limit_key == "exact_equality_relationships"
        else 1
    )
    paragraph_text_sha256 = sha(200_000)
    for index in range(paragraph_count):
        paragraph_ref = {
            "schema_version": "paragraph_ref.v1",
            "ref_type": "paragraph",
            "file_sha256": sha(1),
            "part_name": "word/document.xml",
            "paragraph_index": index,
            "paragraph_text_sha256": paragraph_text_sha256,
            "reading_mode": "accepted_current_v1",
            "container_policy": "canonical_body_flow_v1",
        }
        paragraphs.append(
            round_map_module._ParagraphEvidence(
                paragraph_id=round_map_module._derived_id(
                    "rm_par_v1", paragraph_ref
                ),
                document_id=document_id(0),
                paragraph_ref_json=round_map_module._freeze_json(paragraph_ref),
                container_kind="body",
                paragraph_text_sha256=paragraph_text_sha256,
                role="seed" if index == 0 else "exact_candidate",
            )
        )

    section_count = (
        observed
        if limit_key == "section_nodes"
        else observed + 1
        if limit_key == "navigation_relationships"
        else 0
    )
    for index in range(section_count):
        section_ref = {
            "schema_version": "section_ref.v1",
            "ref_type": "section",
            "file_sha256": sha(1),
            "part_name": "word/document.xml",
            "heading_paragraph_index": index,
            "heading_text_sha256": sha(300_000),
            "reading_mode": "accepted_current_v1",
            "container_policy": "canonical_body_flow_v1",
        }
        sections.append(
            round_map_module._SectionEvidence(
                section_id=round_map_module._derived_id("rm_sec_v1", section_ref),
                document_id=document_id(0),
                section_ref_json=round_map_module._freeze_json(section_ref),
                label="1",
                heading=None,
                level=0,
                label_basis="explicit_heading_text_v1",
                role="seed_navigation" if index == 0 else "candidate_navigation",
            )
        )

    if limit_key == "conflict_items":
        conflicts.extend(
            round_map_module._ConflictEvidence(
                conflict_id=round_map_module._derived_id(
                    "rm_conflict_v1",
                    {
                        "schema_version": "conflict_identity.v1",
                        "conflict_type": "inconsistent_apply_record",
                        "affected_document_ids": [document_id(0)],
                        "record_sha256": sha(400_000 + index),
                    },
                ),
                reason="result_output_sha256_mismatch",
                affected_document_ids=(document_id(0),),
                record_sha256=sha(400_000 + index),
            )
            for index in range(observed)
        )

    return round_map_module._SourceEvidence(
        workspace_path="/synthetic",
        workspace_identity=(1, 1),
        seed_path=observations[0].canonical_path,
        ordering_source="filename_lexicographic_v1",
        cursor_offset=0,
        page_size=100,
        observations=tuple(observations),
        recorded_relationships=tuple(
            sorted(recorded, key=lambda fact: fact.relationship_id)
        ),
        paragraphs=tuple(sorted(paragraphs, key=lambda fact: fact.paragraph_id)),
        sections=tuple(sorted(sections, key=lambda fact: fact.section_id)),
        conflicts=tuple(sorted(conflicts, key=lambda fact: fact.conflict_id)),
    )


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
    label: str,
    limit_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit = ROUND_MAP_LIMITS[limit_key]
    boundary = _authority_at_item_class_count(limit_key, limit)
    round_map_module._validate_authority_limits(boundary)
    facts = round_map_module._projection_facts(boundary)
    if limit_key in {
        "recorded_derivation_relationships",
        "exact_equality_relationships",
        "navigation_relationships",
    }:
        relationship_type = {
            "recorded_derivation_relationships": "recorded_derivation",
            "exact_equality_relationships": "exact_content_equality",
            "navigation_relationships": "navigation_candidate",
        }[limit_key]
        assert dict(facts.relationship_counts)[relationship_type] == limit
    else:
        item_type = {
            "document_nodes": "document_node",
            "document_observations": "document_observation",
            "paragraph_nodes": "paragraph_node",
            "section_nodes": "section_node",
            "resolution_items": "resolution",
            "conflict_items": "conflict",
        }[limit_key]
        assert dict(facts.item_type_counts)[item_type] == limit
    del boundary, facts

    calls: list[tuple[str, int]] = []
    original_enforcement = round_map_module._enforce_resource_boundary

    def enforce_target(observed: int, key: str, detail: str) -> None:
        calls.append((key, observed))
        if key == limit_key:
            original_enforcement(observed, key, detail)

    projected = False

    def unexpected_projection(_authority):
        nonlocal projected
        projected = True
        raise AssertionError(f"{label} over-limit authority reached projection")

    monkeypatch.setattr(
        round_map_module,
        "_enforce_resource_boundary",
        enforce_target,
    )
    monkeypatch.setattr(round_map_module, "_projection_facts", unexpected_projection)
    one_over = _authority_at_item_class_count(limit_key, limit + 1)
    with pytest.raises(RoundMapError) as error:
        round_map_module._validate_authority_limits(one_over)
        round_map_module._projection_facts(one_over)
    assert error.value.code == "resource_limit_exceeded"
    assert (limit_key, limit + 1) in calls
    assert projected is False


@pytest.mark.parametrize(
    "limit_key",
    [
        "candidate_docx_files",
        "candidate_compressed_input_bytes",
        "compressed_bytes_per_docx",
        "indexed_paragraphs_per_docx",
        "accepted_current_chars_per_docx",
    ],
)
def test_every_input_resource_enforcement_seam_is_inclusive_and_refuses_one_over(
    limit_key: str,
) -> None:
    limit = ROUND_MAP_LIMITS[limit_key]
    round_map_module._enforce_resource_boundary(limit, limit_key, "sentinel")
    with pytest.raises(RoundMapError) as error:
        round_map_module._enforce_resource_boundary(limit + 1, limit_key, "sentinel")
    assert error.value.code == "resource_limit_exceeded"


def test_expanded_byte_budget_is_inclusive_and_refuses_one_over() -> None:
    budget = round_map_module.ExpandedOutputBudget(
        allowed_bytes=ROUND_MAP_LIMITS["candidate_expanded_bytes"],
        limit="round_map_candidate_expanded_bytes",
    )
    budget.consume(ROUND_MAP_LIMITS["candidate_expanded_bytes"])
    with pytest.raises(round_map_module.ResourceLimitError):
        budget.consume(1)


@pytest.mark.parametrize(
    "limit_key",
    [
        "compressed_bytes_per_docx",
        "candidate_compressed_input_bytes",
        "candidate_expanded_bytes",
    ],
)
def test_real_package_byte_caps_accept_exact_and_publicly_refuse_one_over(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_key: str,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    candidates = sorted(matter.glob("*.docx"))
    if limit_key == "compressed_bytes_per_docx":
        exact = max(path.stat().st_size for path in candidates)
    elif limit_key == "candidate_compressed_input_bytes":
        exact = sum(path.stat().st_size for path in candidates)
    else:
        exact = 0
        for path in candidates:
            with zipfile.ZipFile(path) as archive:
                exact += sum(info.file_size for info in archive.infolist())

    with monkeypatch.context() as boundary:
        boundary.setitem(ROUND_MAP_LIMITS, limit_key, exact)
        assert build_round_map(str(matter), seed).result["status"] == "ok"
    with monkeypatch.context() as over:
        over.setitem(ROUND_MAP_LIMITS, limit_key, exact - 1)
        with pytest.raises(RoundMapError) as error:
            server.map_rounds(str(matter), seed)
    assert error.value.code == "resource_limit_exceeded"
    assert not (matter / records.SIDECAR_DIR).exists()


def test_real_paragraph_and_character_caps_accept_exact_and_refuse_one_over(
    tmp_path: Path,
) -> None:
    generated = _matter(tmp_path / "generated")

    def make(name: str, body_texts: list[str]) -> tuple[Path, dict]:
        matter = tmp_path / name
        matter.mkdir()
        target = matter / "round.docx"
        shutil.copyfile(_round(generated, 1), target)
        _replace_body_text(target, body_texts)
        return matter, {
            "schema_version": "round_map_seed.v1",
            "path": str(target),
            "paragraph_ref": {
                "schema_version": "paragraph_ref.v1",
                "ref_type": "paragraph",
                "file_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "part_name": "word/document.xml",
                "paragraph_index": 0,
                "paragraph_text_sha256": hashlib.sha256(
                    body_texts[0].encode("utf-8")
                ).hexdigest(),
                "reading_mode": "accepted_current_v1",
                "container_policy": "canonical_body_flow_v1",
            },
        }

    paragraph_exact, paragraph_seed = make("paragraph-exact", ["x"] * 10_000)
    assert (
        build_round_map(str(paragraph_exact), paragraph_seed).result["status"] == "ok"
    )
    paragraph_over, paragraph_over_seed = make("paragraph-over", ["x"] * 10_001)
    with pytest.raises(RoundMapError) as paragraph_error:
        server.map_rounds(str(paragraph_over), paragraph_over_seed)
    assert paragraph_error.value.code == "resource_limit_exceeded"
    assert not (paragraph_over / records.SIDECAR_DIR).exists()

    character_exact, character_seed = make("character-exact", ["x" * 50_000] * 40)
    assert (
        build_round_map(str(character_exact), character_seed).result["status"] == "ok"
    )
    character_over, character_over_seed = make(
        "character-over", ["x" * 50_000] * 40 + ["x"]
    )
    with pytest.raises(RoundMapError) as character_error:
        server.map_rounds(str(character_over), character_over_seed)
    assert character_error.value.code == "resource_limit_exceeded"
    assert not (character_over / records.SIDECAR_DIR).exists()


def test_journal_apply_record_cap_accepts_exact_and_publicly_refuses_one_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    (sidecar / records.GITIGNORE_NAME).write_text("*\n", encoding="utf-8")
    journal = sidecar / records.JOURNAL_NAME
    decoded = 0

    def decode(_payload, _path, _line_no):
        nonlocal decoded
        decoded += 1
        return (
            {"tool_name": "apply_edits", "result": {"status": "error"}},
            decoded,
        )

    monkeypatch.setattr(records, "_decode_record_line", decode)
    journal.write_bytes(b"{}\n" * ROUND_MAP_LIMITS["journal_apply_records"])
    assert build_round_map(str(matter), seed).result["status"] == "ok"

    decoded = 0
    with journal.open("ab") as handle:
        handle.write(b"{}\n")
    before = journal.read_bytes()
    with pytest.raises(RoundMapError) as error:
        server.map_rounds(str(matter), seed)
    assert error.value.code == "resource_limit_exceeded"
    assert journal.read_bytes() == before


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
            text.text = "77.777 PRIVATE_HEADING_SENTINEL"
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
    assert any(item.get("label") == "77.777" for item in mapped["items"])
    exported = server.export_decision_record(str(matter), max_records=100)
    compact = next(
        item for item in exported["records"] if item["tool_name"] == "map_rounds"
    )
    encoded = json.dumps(compact, ensure_ascii=False)
    for sentinel in (
        "PRIVATE_WORKSPACE_SENTINEL",
        "PRIVATE_FILENAME_SENTINEL.docx",
        "77.777",
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


def test_public_candidate_limit_precedes_stale_cursor_and_never_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    first = server.map_rounds(str(matter), seed, max_items=1)
    assert first["next_cursor"] is not None
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    before = journal.read_bytes()
    monkeypatch.setitem(ROUND_MAP_LIMITS, "candidate_docx_files", 3)

    with pytest.raises(RoundMapError) as error:
        server.map_rounds(
            str(matter),
            seed,
            ordered_filenames=["not-the-captured-order.docx"],
            cursor=first["next_cursor"],
        )
    assert error.value.code == "resource_limit_exceeded"
    assert journal.read_bytes() == before


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
        "out_of_range_outline_level",
        "missing_altchunk_relationship",
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
            elif selected in {
                "malformed_outline_level",
                "out_of_range_outline_level",
            }:
                paragraph = next(child for child in body if child.tag == w("p"))
                properties = paragraph.find(w("pPr"))
                if properties is None:
                    properties = etree.Element(w("pPr"))
                    paragraph.insert(0, properties)
                outline = etree.SubElement(properties, w("outlineLvl"))
                outline.set(
                    w("val"),
                    "not-an-integer" if selected == "malformed_outline_level" else "10",
                )
            elif selected == "missing_altchunk_relationship":
                alt_chunk = etree.Element(w("altChunk"))
                alt_chunk.set(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
                    "rIdMissing",
                )
                body.insert(0, alt_chunk)
            else:
                document.append(deepcopy(body))

            return etree.tostring(document, xml_declaration=True, encoding="UTF-8")

        _rewrite_document_xml(target, mutate)
        with pytest.raises(RoundMapError) as error:
            build_round_map(str(matter), seed)
        assert error.value.code == "invalid_docx"
        if case in {"out_of_range_outline_level", "missing_altchunk_relationship"}:
            with pytest.raises(Exception) as stage3a_error:
                inspect_document(str(target), "outline", max_items=100)
            assert getattr(stage3a_error.value, "code", None) == "file_unextractable"

    matter = _matter(tmp_path / "duplicate_archive_member")
    seed = _seed(_round(matter, 2))
    target = _round(matter, 1)
    with zipfile.ZipFile(target, "a", zipfile.ZIP_STORED) as archive:
        archive.writestr("word/document.xml", b"<duplicate/>")
    with pytest.raises(RoundMapError) as archive_error:
        build_round_map(str(matter), seed)
    assert archive_error.value.code == "file_unextractable"


@pytest.mark.parametrize("replacement_exists", [True, False])
def test_round_map_publication_is_bound_to_captured_workspace_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_exists: bool,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    computation = build_round_map(str(matter), seed, max_items=100)
    original = tmp_path / "captured-workspace"
    monkeypatch.setattr(
        round_map_module,
        "build_round_map",
        lambda *_args, **_kwargs: computation,
    )
    pristine_write = records.write_record

    def replace_before_write(**kwargs):
        matter.rename(original)
        if replacement_exists:
            matter.mkdir()
        return pristine_write(**kwargs)

    monkeypatch.setattr(records, "write_record", replace_before_write)
    result = server.map_rounds(str(matter), seed, max_items=100)

    assert {
        key: result[key] for key in computation.result if key != "producer"
    } == computation.result
    assert (result["record_id"], result["record_status"], result["record_error"]) == (
        None,
        "write_failed",
        "workspace_changed",
    )
    assert not (original / records.SIDECAR_DIR).exists()
    assert not (matter / records.SIDECAR_DIR).exists()


@pytest.mark.parametrize(
    "initialization_state",
    ["absent", "sidecar_only", "gitignore_only", "journal_only", "initialized"],
)
@pytest.mark.parametrize("path_race", ["missing", "replacement", "final_symlink"])
def test_round_map_publication_rolls_back_post_open_path_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initialization_state: str,
    path_race: str,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    sidecar = matter / records.SIDECAR_DIR
    gitignore = sidecar / records.GITIGNORE_NAME
    journal = sidecar / records.JOURNAL_NAME
    before: dict[str, tuple[tuple[int, int], int, bytes | None]] = {}

    if initialization_state == "initialized":
        assert (
            server.map_rounds(str(matter), seed, max_items=100)["record_status"]
            == "written"
        )
    elif initialization_state != "absent":
        sidecar.mkdir()
        if initialization_state == "gitignore_only":
            gitignore.write_bytes(b"*\n")
        elif initialization_state == "journal_only":
            journal.touch()

    if sidecar.exists():
        sidecar.chmod(0o751)
        if gitignore.exists():
            gitignore.chmod(0o640)
        if journal.exists():
            journal.chmod(0o640)
        for name, path in {
            "sidecar": sidecar,
            "gitignore": gitignore,
            "journal": journal,
        }.items():
            if not path.exists():
                continue
            info = path.lstat()
            before[name] = (
                (info.st_dev, info.st_ino),
                info.st_mode & 0o7777,
                None if path.is_dir() else path.read_bytes(),
            )

    moved = tmp_path / "captured-workspace"
    pristine_append = records._append_to_scanned_journal
    raced = False

    def race_then_append(handle, scan, record):
        nonlocal raced
        assert raced is False
        matter.rename(moved)
        if path_race == "replacement":
            matter.mkdir()
        elif path_race == "final_symlink":
            matter.symlink_to(moved, target_is_directory=True)
        raced = True
        return pristine_append(handle, scan, record)

    monkeypatch.setattr(records, "_append_to_scanned_journal", race_then_append)
    result = server.map_rounds(str(matter), seed, max_items=100)

    assert raced is True
    assert (result["record_id"], result["record_status"], result["record_error"]) == (
        None,
        "write_failed",
        "workspace_changed",
    )
    moved_sidecar = moved / records.SIDECAR_DIR
    if initialization_state != "absent":
        for name, path in {
            "sidecar": moved_sidecar,
            "gitignore": moved_sidecar / records.GITIGNORE_NAME,
            "journal": moved_sidecar / records.JOURNAL_NAME,
        }.items():
            if name not in before:
                assert not path.exists()
                assert not path.is_symlink()
                continue
            info = path.lstat()
            identity, mode, payload = before[name]
            assert (info.st_dev, info.st_ino) == identity
            assert info.st_mode & 0o7777 == mode
            if payload is not None:
                assert path.read_bytes() == payload
    else:
        assert not moved_sidecar.exists()

    if path_race == "missing":
        assert not matter.exists()
        assert not matter.is_symlink()
    elif path_race == "replacement":
        assert matter.is_dir()
        assert list(matter.iterdir()) == []
    else:
        assert matter.is_symlink()
        assert matter.readlink() == moved


@pytest.mark.parametrize(
    ("parser_error", "expected_code"),
    [
        (EncryptedDocxError("sentinel"), "encrypted_docx"),
        (UnsupportedCompressionError("sentinel"), "unsupported_compression"),
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

    invalid_summary = round_map_module.record_summary(computation)
    invalid_summary["items_summary"]["count"] += 1
    publication = records.write_record(
        workspace=matter,
        tool_name="map_rounds",
        input_payload={},
        result=invalid_summary,
        tool_result=computation.result,
        provenance=round_map_module.record_provenance(computation),
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


def test_self_consistent_output_forgery_cannot_replace_source_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    current = build_round_map(str(matter), seed, max_items=100)
    _current_apply_record(
        matter,
        "c" * 64,
        "b" * 64,
        result_source_sha256="c" * 64,
        result_output_sha256=seed["paragraph_ref"]["file_sha256"],
    )
    conflicted = build_round_map(str(matter), seed, max_items=100)

    def recompute_output_side(result: dict, computation) -> None:
        result["items"].sort(
            key=lambda item: (
                round_map_module._TYPE_RANK[item["item_type"]],
                item["id"],
            )
        )
        observations = sorted(
            _items(result, "document_observation"),
            key=lambda item: item["position"],
        )
        filenames = [item["filename"] for item in observations]
        manifest = round_map_module._digest(
            {
                "schema_version": "round_map_filename_manifest.v1",
                "ordering_source": result["ordering_source"],
                "filenames": filenames,
            }
        )
        result["order_basis"]["filename_manifest_sha256"] = manifest
        documents = {item["id"]: item for item in _items(result, "document_node")}
        source_by_position = {
            item.position: item for item in computation.proof.authority.observations
        }
        result["snapshot"]["filesystem_snapshot_sha256"] = round_map_module._digest(
            {
                "schema_version": "round_map_filesystem_snapshot.v1",
                "filename_manifest_sha256": manifest,
                "observations": [
                    {
                        "observation_id": observation["id"],
                        "canonical_path": observation["path"],
                        "filename": observation["filename"],
                        "position": observation["position"],
                        "byte_length": source_by_position[
                            observation["position"]
                        ].byte_length,
                        "file_sha256": documents[observation["document_id"]][
                            "file_sha256"
                        ],
                        "inspection_coverage_sha256": round_map_module._digest(
                            documents[observation["document_id"]]["inspection_coverage"]
                        ),
                    }
                    for observation in observations
                ],
            }
        )
        record_sha256s = sorted(
            [
                entry["record_sha256"]
                for relationship in _items(result, "relationship")
                if relationship["relationship_type"] == "recorded_derivation"
                for entry in relationship["basis"]["supporting_records"]["sample"]
            ]
            + [item["record_sha256"] for item in _items(result, "conflict")]
        )
        result["snapshot"]["journal_snapshot_sha256"] = round_map_module._digest(
            {
                "schema_version": "round_map_relevant_journal_snapshot.v1",
                "record_sha256s": record_sha256s,
            }
        )
        result["snapshot"]["full_result_set_sha256"] = (
            round_map_module._streaming_item_set_digest(result["items"])
        )

    def forge_document(result: dict, computation) -> None:
        resolution = next(
            item
            for item in _items(result, "resolution")
            if item["reason"] == "no_match_in_declared_scope"
        )
        old_id = resolution["document_id"]
        new_sha = "0" * 64
        new_id = f"rm_doc_v1:{new_sha}"
        document = next(
            item for item in _items(result, "document_node") if item["id"] == old_id
        )
        document["id"] = new_id
        document["file_sha256"] = new_sha
        observation = next(
            item
            for item in _items(result, "document_observation")
            if item["document_id"] == old_id
        )
        observation["document_id"] = new_id
        observation["id"] = round_map_module._derived_id(
            "rm_obs_v1",
            {
                "schema_version": "document_observation_identity.v1",
                "document_id": new_id,
                "canonical_path": observation["path"],
            },
        )
        resolution["document_id"] = new_id
        resolution["id"] = round_map_module._derived_id(
            "rm_resolution_v1",
            {
                "schema_version": "resolution_identity.v1",
                "seed_paragraph_id": resolution["seed_paragraph_id"],
                "document_id": new_id,
            },
        )
        recompute_output_side(result, computation)

    def forge_observation(result: dict, computation) -> None:
        observation = sorted(
            _items(result, "document_observation"), key=lambda item: item["position"]
        )[1]
        observation["filename"] = observation["filename"].replace(
            ".docx", "-forged.docx"
        )
        observation["path"] = str(
            Path(observation["path"]).with_name(observation["filename"])
        )
        observation["id"] = round_map_module._derived_id(
            "rm_obs_v1",
            {
                "schema_version": "document_observation_identity.v1",
                "document_id": observation["document_id"],
                "canonical_path": observation["path"],
            },
        )
        recompute_output_side(result, computation)

    def forge_coverage(result: dict, computation) -> None:
        document = next(
            item
            for item in _items(result, "document_node")
            if item["observation_state"] == "current"
            and item["inspection_coverage"] is not None
        )
        coverage = document["inspection_coverage"]
        coverage["nonempty_indexed_paragraph_count"] -= 1
        recompute_output_side(result, computation)

    def forge_conflict_and_resolution(result: dict, computation) -> None:
        conflict = _items(result, "conflict")[0]
        conflict["reason"] = "missing_source_sha256"
        conflict["record_sha256"] = "1" * 64
        conflict["id"] = round_map_module._derived_id(
            "rm_conflict_v1",
            {
                "schema_version": "conflict_identity.v1",
                "conflict_type": conflict["conflict_type"],
                "affected_document_ids": conflict["affected_document_ids"],
                "record_sha256": conflict["record_sha256"],
            },
        )
        resolution = next(
            item
            for item in _items(result, "resolution")
            if item["reason"] == "recorded_fact_conflict"
        )
        resolution["candidate_ids"]["sha256"] = round_map_module._digest([])
        recompute_output_side(result, computation)

    attempts = (
        (current, forge_document),
        (current, forge_observation),
        (current, forge_coverage),
        (conflicted, forge_conflict_and_resolution),
    )
    for computation, attack in attempts:
        forged = deepcopy(computation.result)
        attack(forged, computation)
        forged_proof = replace(
            computation.proof,
            item_fingerprints=round_map_module._freeze_item_fingerprints(
                forged["items"]
            ),
            full_result_set_sha256=forged["snapshot"]["full_result_set_sha256"],
        )
        with pytest.raises(RoundMapError) as core_error:
            round_map_module._validate_complete_items(forged["items"], forged_proof)
        assert core_error.value.code == "output_contract_error"
        normalized = server._validated_success_result("map_rounds", forged)
        assert isinstance(normalized, dict)
        with pytest.raises(RoundMapError) as result_error:
            round_map_module.validate_computation_result(computation, normalized)
        assert result_error.value.code == "output_contract_error"

        published = False

        def forbidden_write(**_kwargs):
            nonlocal published
            published = True
            raise AssertionError("forged result reached publication")

        monkeypatch.setattr(
            round_map_module,
            "build_round_map",
            lambda *_args, selected=replace(computation, result=forged), **_kwargs: (
                selected
            ),
        )
        monkeypatch.setattr(records, "write_record", forbidden_write)
        with pytest.raises(RoundMapError) as boundary_error:
            server.map_rounds(str(matter), seed, max_items=100)
        assert boundary_error.value.code == "output_contract_error"
        assert published is False


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
@pytest.mark.parametrize("terminal", ["\n", "\r", "\r\n", "\x00", "\u0661"])
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


def test_every_live_stage3b_identity_and_cursor_location_has_an_absolute_end(
    tmp_path: Path,
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    _current_apply_record(
        matter,
        seed["paragraph_ref"]["file_sha256"],
        "b" * 64,
    )
    results = [
        build_round_map(str(matter), seed, max_items=100).result,
        build_round_map(str(matter), seed, max_items=1).result,
    ]

    def paths(value, prefix=()):
        if isinstance(value, dict):
            for key, child in value.items():
                yield from paths(child, (*prefix, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from paths(child, (*prefix, index))
        elif isinstance(value, str) and (
            value.startswith(("rm_", "rm1:", "dr_"))
            or (prefix[-1:] == ("round_id",) and value.startswith("round-"))
            or (
                len(value) == 64
                and all(character in "0123456789abcdef" for character in value)
            )
        ):
            yield prefix

    def assign(root, path, value):
        owner = root
        for key in path[:-1]:
            owner = owner[key]
        owner[path[-1]] = value

    checked = 0
    validator = Draft202012Validator(
        {
            "type": "object",
            "properties": ROUND_MAP_RESULT_PROPERTIES,
            "required": ROUND_MAP_RESULT_REQUIRED,
            "additionalProperties": False,
        }
    )
    for result in results:
        assert validator.is_valid(result)
        for path in paths(result):
            owner = result
            for key in path:
                owner = owner[key]
            for terminal in ("\n", "\r", "\r\n", "\x00", "\u0661"):
                mutated = deepcopy(result)
                assign(mutated, path, owner + terminal)
                assert not validator.is_valid(mutated), (path, repr(terminal))
                checked += 1
    assert checked >= 100


@pytest.mark.parametrize("seed_key", ["document_id", "paragraph_id"])
@pytest.mark.parametrize("terminal", ["\n", "\r", "\r\n", "\x00", "\u0661"])
def test_stored_seed_identity_corruption_never_reaches_compact_projection(
    tmp_path: Path, seed_key: str, terminal: str
) -> None:
    matter = _matter(tmp_path)
    seed = _seed(_round(matter, 1))
    server.map_rounds(str(matter), seed, max_items=100)
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    raw = json.loads(journal.read_text(encoding="utf-8"))
    sentinel = raw["result"]["seed"][seed_key] + terminal
    raw["result"]["seed"][seed_key] = sentinel
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
    exact_path = matter / "03-exact.docx"
    shutil.copyfile(_round(generated, 1), seed_path)
    shutil.copyfile(_round(generated, 2), candidate_path)
    shutil.copyfile(_round(generated, 3), exact_path)
    _replace_body_text(seed_path, ["Exact scoped clause Ω"])
    _replace_body_text(
        candidate_path,
        ["Different in-scope clause"],
        excluded_text="Exact scoped clause Ω",
    )
    _replace_body_text(exact_path, ["Exact scoped clause Ω"])
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
    exact_document_id = "rm_doc_v1:" + _seed(exact_path)["paragraph_ref"]["file_sha256"]
    exact_paragraph_id = next(
        item["id"]
        for item in _items(result, "paragraph_node")
        if item["document_id"] == exact_document_id
    )
    exact_relationships = [
        item
        for item in _items(result, "relationship")
        if item["relationship_type"] == "exact_content_equality"
    ]
    assert any(
        exact_paragraph_id in {item["from_id"], item["to_id"]}
        for item in exact_relationships
    )
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


def _coherent_70k_authority(workspace: Path) -> round_map_module._SourceEvidence:
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
    observations = tuple(
        round_map_module._ObservationEvidence(
            observation_id=round_map_module._derived_id(
                "rm_obs_v1",
                {
                    "schema_version": "document_observation_identity.v1",
                    "document_id": document_ids[position],
                    "canonical_path": str(workspace / filename),
                },
            ),
            document_id=document_ids[position],
            canonical_path=str(workspace / filename),
            filename=filename,
            position=position,
            byte_length=1,
            file_sha256=document_shas[position],
            inspection_coverage_json=round_map_module._freeze_json(inspection_coverage),
        )
        for position, filename in enumerate(filenames)
    )
    recorded = tuple(
        round_map_module._RecordedEvidence(
            relationship_id=round_map_module._relationship_id(
                "recorded_derivation",
                document_ids[0],
                output_id,
                "directed",
                round_map_module._RECORDED_BASIS_IDENTITY,
            ),
            source_id=document_ids[0],
            output_id=output_id,
            supporting_records=(
                (f"dr_{index:06d}", sha(20_000 + index), "current_v0.3"),
            ),
        )
        for index, output_id in enumerate(document_ids[500:], start=1)
    )
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
    paragraphs = [
        round_map_module._ParagraphEvidence(
            paragraph_id=round_map_module._derived_id("rm_par_v1", seed_ref),
            document_id=document_ids[0],
            paragraph_ref_json=round_map_module._freeze_json(seed_ref),
            container_kind="body",
            paragraph_text_sha256=paragraph_text_sha256,
            role="seed",
        )
    ]
    for index in range(10_000):
        document_index = index % 500
        reference = paragraph_ref(document_index, index // 500 + 1)
        paragraphs.append(
            round_map_module._ParagraphEvidence(
                paragraph_id=round_map_module._derived_id("rm_par_v1", reference),
                document_id=document_ids[document_index],
                paragraph_ref_json=round_map_module._freeze_json(reference),
                container_kind="body",
                paragraph_text_sha256=paragraph_text_sha256,
                role="exact_candidate",
            )
        )

    heading_text_sha256 = "e" * 64
    label = "Clause Omega"

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
    sections = [
        round_map_module._SectionEvidence(
            section_id=round_map_module._derived_id("rm_sec_v1", seed_section_ref),
            document_id=document_ids[0],
            section_ref_json=round_map_module._freeze_json(seed_section_ref),
            label=label,
            heading=None,
            level=0,
            label_basis="explicit_heading_text_v1",
            role="seed_navigation",
        )
    ]
    for index in range(9_249):
        document_index = index % 500
        reference = section_ref(document_index, index // 500 + 1)
        sections.append(
            round_map_module._SectionEvidence(
                section_id=round_map_module._derived_id("rm_sec_v1", reference),
                document_id=document_ids[document_index],
                section_ref_json=round_map_module._freeze_json(reference),
                label=label,
                heading=None,
                level=0,
                label_basis="explicit_heading_text_v1",
                role="candidate_navigation",
            )
        )

    info = workspace.lstat()
    authority = round_map_module._SourceEvidence(
        workspace_path=str(workspace),
        workspace_identity=(info.st_dev, info.st_ino),
        seed_path=str(workspace / filenames[0]),
        ordering_source="filename_lexicographic_v1",
        cursor_offset=0,
        page_size=100,
        observations=observations,
        recorded_relationships=tuple(
            sorted(recorded, key=lambda fact: fact.relationship_id)
        ),
        paragraphs=tuple(sorted(paragraphs, key=lambda fact: fact.paragraph_id)),
        sections=tuple(sorted(sections, key=lambda fact: fact.section_id)),
        conflicts=(),
    )
    round_map_module._validate_authority_limits(authority)
    return authority


def _coherent_70k_computation(workspace: Path) -> round_map_module.RoundMapComputation:
    authority = _coherent_70k_authority(workspace)
    facts = round_map_module._projection_facts(authority)
    assert facts.eligible_item_count == 70_000
    proof = round_map_module._RoundMapProof(
        authority=authority,
        item_fingerprints=facts.item_fingerprints,
        full_result_set_sha256=facts.full_result_set_sha256,
        item_type_counts=facts.item_type_counts,
        relationship_counts=facts.relationship_counts,
        resolution_counts=facts.resolution_counts,
        record_only_document_count=facts.record_only_document_count,
    )
    result = round_map_module._project_result(authority, facts)
    computation = round_map_module.RoundMapComputation(result=result, proof=proof)
    return computation


def test_coherent_70000_complete_map_validates_and_publishes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "coherent-70000"
    computation = _coherent_70k_computation(workspace)
    normalized = server._validated_success_result("map_rounds", computation.result)
    assert isinstance(normalized, dict)

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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _coherent_70k_authority(tmp_path / "coherent-70001")
    extra_section = replace(
        authority.sections[0],
        section_id="rm_sec_v1:" + "d" * 64,
    )
    one_over = replace(
        authority,
        sections=(*authority.sections, extra_section),
    )
    called = False

    def unexpected(_item):
        nonlocal called
        called = True
        raise AssertionError("fingerprinting must not begin above the aggregate cap")

    monkeypatch.setattr(round_map_module, "_item_fingerprint", unexpected)
    with pytest.raises(RoundMapError) as error:
        round_map_module._validate_authority_limits(one_over)
        round_map_module._projection_facts(one_over)
    assert error.value.code == "resource_limit_exceeded"
    assert called is False


_HERE = "tests/test_round_map.py"


def _nodeid(test_name: str, *, module: str = _HERE) -> str:
    return f"{module}::{test_name}"


# Frozen fixture.subclause -> callable pytest nodeids only. Letters follow the
# order of atomic claims in each frozen fixture; this is not a prose contract.
ACCEPTANCE_CLAUSE_NODEIDS: dict[str, tuple[str, ...]] = {
    "1.a": (
        _nodeid(
            "test_current_apply_record_creates_only_a_document_recorded_derivation"
        ),
    ),
    "2.a": (
        _nodeid(
            "test_preflightless_published_profile_and_strengthened_hybrid_conflict"
        ),
        _nodeid(
            "test_frozen_golden_profile_full_map_conflict_changes_both_bound_digests"
        ),
    ),
    "3.a": (
        _nodeid("test_failed_apply_and_successful_preflight_create_no_derivation"),
    ),
    "4.a": (
        _nodeid("test_divergent_output_conflict_affects_only_the_current_endpoint"),
    ),
    "5.a": (
        _nodeid(
            "test_exact_equality_and_navigation_never_become_lineage_or_chronology"
        ),
        _nodeid(
            "test_duplicate_navigation_labels_and_headings_preserve_all_candidates"
        ),
    ),
    "6.a": (
        _nodeid("test_equal_hash_signal_is_not_trusted_without_full_text_comparison"),
    ),
    "7.a": (_nodeid("test_explicit_manifest_and_mtimes_remain_position_only"),),
    "8.a": (
        _nodeid(
            "test_duplicate_exact_paragraphs_are_ambiguous_without_arbitrary_choice"
        ),
        _nodeid(
            "test_duplicate_navigation_labels_and_headings_preserve_all_candidates"
        ),
    ),
    "9.a": (
        _nodeid("test_single_navigation_candidate_remains_unresolved"),
        _nodeid("test_navigation_only_candidates_remain_unresolved"),
    ),
    "10.a": (
        _nodeid("test_document_without_exact_or_navigation_candidate_is_unresolved"),
    ),
    "11.a": (_nodeid("test_duplicate_bytes_collapse_document_and_paragraph_identity"),),
    "12.a": (
        _nodeid("test_rename_changes_observation_identity_and_keeps_content_identity"),
    ),
    "13.a": (
        _nodeid(
            "test_branch_cycle_self_loop_multiple_parents_and_duplicate_support_are_preserved"
        ),
    ),
    "14.a": (
        _nodeid(
            "test_branch_cycle_self_loop_multiple_parents_and_duplicate_support_are_preserved"
        ),
    ),
    "15.a": (
        _nodeid(
            "test_branch_cycle_self_loop_multiple_parents_and_duplicate_support_are_preserved"
        ),
        _nodeid("test_support_sample_is_bounded_without_dropping_duplicate_evidence"),
    ),
    "16.a": (
        _nodeid(
            "test_branch_cycle_self_loop_multiple_parents_and_duplicate_support_are_preserved"
        ),
    ),
    "17.a": (
        _nodeid(
            "test_preflightless_published_profile_and_strengthened_hybrid_conflict"
        ),
        _nodeid(
            "test_renamed_recorded_output_is_current_and_new_relevant_record_drifts_cursor"
        ),
    ),
    "18.a": (
        _nodeid(
            "test_nonseed_drift_invalidates_cursor_but_seed_drift_has_specific_error"
        ),
        _nodeid(
            "test_later_page_missing_seed_and_journal_compound_failures_keep_phase_order"
        ),
        _nodeid("test_workspace_set_drift_and_candidate_count_boundary"),
        _nodeid(
            "test_seed_evidence_precedes_journal_and_candidate_safety_precedes_order"
        ),
        _nodeid("test_public_candidate_limit_precedes_stale_cursor_and_never_appends"),
    ),
    "19.a": (
        _nodeid("test_later_page_ignores_new_valid_non_apply_record"),
        _nodeid("test_pagination_allows_page_size_change_and_ignores_own_records"),
        _nodeid(
            "test_later_page_missing_seed_and_journal_compound_failures_keep_phase_order"
        ),
        _nodeid("test_cursor_mismatch_precedes_digest_valid_offset_range"),
    ),
    "20.a": (
        _nodeid("test_root_and_journal_snapshot_contention_are_fail_closed"),
        _nodeid(
            "test_exact_64_mib_aggregate_journal_is_accepted_and_one_byte_over_refuses"
        ),
        _nodeid(
            "test_journal_apply_record_cap_accepts_exact_and_publicly_refuses_one_over"
        ),
        _nodeid("test_round_map_success_writes_once_and_append_failure_is_fail_open"),
        _nodeid("test_round_map_publication_is_bound_to_captured_workspace_identity"),
    ),
    "21.a": (
        _nodeid("test_real_hostile_docx_packages_refuse_the_complete_map"),
        _nodeid(
            "test_round_map_normalizes_main_body_structure_but_not_archive_ambiguity"
        ),
        _nodeid("test_candidate_symlink_and_hardlink_fail_closed"),
        _nodeid("test_one_candidate_parse_refusal_aborts_the_complete_map"),
    ),
    "22.a": (
        _nodeid(
            "test_excluded_container_never_creates_exact_match_or_negative_whole_doc_claim"
        ),
    ),
    "23.a": (
        _nodeid("test_workspace_set_drift_and_candidate_count_boundary"),
        _nodeid(
            "test_every_input_resource_enforcement_seam_is_inclusive_and_refuses_one_over"
        ),
        _nodeid("test_expanded_byte_budget_is_inclusive_and_refuses_one_over"),
        _nodeid(
            "test_real_package_byte_caps_accept_exact_and_publicly_refuse_one_over"
        ),
        _nodeid(
            "test_real_paragraph_and_character_caps_accept_exact_and_refuse_one_over"
        ),
        _nodeid("test_every_frozen_item_class_cap_is_inclusive_and_refuses_one_over"),
        _nodeid("test_total_item_limit_accepts_boundary_and_refuses_one_over"),
        _nodeid("test_coherent_70000_complete_map_validates_and_publishes_once"),
        _nodeid("test_70001_refuses_before_item_fingerprinting"),
        _nodeid("test_default_page_size_and_cancellation_never_create_partial_success"),
    ),
    "24.a": (
        _nodeid(
            "test_pagination_exhaustion_has_exact_union_without_overlap_or_omission"
        ),
    ),
    "25.a": (
        _nodeid(
            "test_compact_export_omits_navigation_body_edit_and_verbatim_input_sentinels"
        ),
        _nodeid(
            "test_sampled_item_id_terminal_newlines_never_reach_raw_or_compact_projection"
        ),
        _nodeid(
            "test_stored_seed_identity_corruption_never_reaches_compact_projection"
        ),
    ),
    "26.a": (
        _nodeid(
            "test_outline_is_text_free_hash_bound_and_explicit_about_scope",
            module="tests/test_inspect.py",
        ),
        _nodeid(
            "test_golden_v1_journal_stays_readable_and_appendable",
            module="tests/test_decision_records.py",
        ),
        _nodeid(
            "test_development_wheel_smoke_binds_metadata_and_eight_tool_surface",
            module="tests/test_release_workflow.py",
        ),
        _nodeid(
            "test_rebuild_comparison_requires_exact_names_and_bytes",
            module="tests/test_reproducible_build.py",
        ),
    ),
}

ACCEPTANCE_CLAUSE_NODEIDS.update(
    {
        "2.b": (
            _nodeid(
                "test_preflightless_published_profile_and_strengthened_hybrid_conflict"
            ),
        ),
        "2.c": (
            _nodeid(
                "test_preflightless_published_profile_and_strengthened_hybrid_conflict"
            ),
        ),
        "2.d": (
            _nodeid(
                "test_preflightless_published_profile_and_strengthened_hybrid_conflict"
            ),
        ),
        "3.b": (
            _nodeid("test_failed_apply_and_successful_preflight_create_no_derivation"),
        ),
        "4.b": (
            _nodeid("test_divergent_output_conflict_affects_only_the_current_endpoint"),
        ),
        "4.c": (
            _nodeid("test_divergent_output_conflict_affects_only_the_current_endpoint"),
        ),
        "4.d": (
            _nodeid("test_divergent_output_conflict_affects_only_the_current_endpoint"),
        ),
        "5.b": (
            _nodeid(
                "test_exact_equality_and_navigation_never_become_lineage_or_chronology"
            ),
        ),
        "5.c": (
            _nodeid(
                "test_duplicate_navigation_labels_and_headings_preserve_all_candidates"
            ),
        ),
        "7.b": (_nodeid("test_explicit_manifest_and_mtimes_remain_position_only"),),
        "7.c": (_nodeid("test_explicit_manifest_and_mtimes_remain_position_only"),),
        "8.b": (
            _nodeid(
                "test_duplicate_navigation_labels_and_headings_preserve_all_candidates"
            ),
        ),
        "9.b": (_nodeid("test_navigation_only_candidates_remain_unresolved"),),
        "11.b": (
            _nodeid("test_duplicate_bytes_collapse_document_and_paragraph_identity"),
        ),
        "11.c": (
            _nodeid("test_duplicate_bytes_collapse_document_and_paragraph_identity"),
        ),
        "12.b": (
            _nodeid(
                "test_nonseed_drift_invalidates_cursor_but_seed_drift_has_specific_error"
            ),
        ),
        "14.b": (
            _nodeid(
                "test_branch_cycle_self_loop_multiple_parents_and_duplicate_support_are_preserved"
            ),
        ),
        "15.b": (
            _nodeid(
                "test_branch_cycle_self_loop_multiple_parents_and_duplicate_support_are_preserved"
            ),
        ),
        "16.b": (
            _nodeid(
                "test_branch_cycle_self_loop_multiple_parents_and_duplicate_support_are_preserved"
            ),
        ),
        "16.c": (
            _nodeid(
                "test_branch_cycle_self_loop_multiple_parents_and_duplicate_support_are_preserved"
            ),
        ),
        "17.b": (
            _nodeid(
                "test_renamed_recorded_output_is_current_and_new_relevant_record_drifts_cursor"
            ),
        ),
        "17.c": (
            _nodeid(
                "test_preflightless_published_profile_and_strengthened_hybrid_conflict"
            ),
        ),
        "18.b": (
            _nodeid(
                "test_renamed_recorded_output_is_current_and_new_relevant_record_drifts_cursor"
            ),
        ),
        "18.c": (
            _nodeid(
                "test_later_page_candidate_set_and_oversized_replacement_keep_precedence"
            ),
        ),
        "18.d": (
            _nodeid(
                "test_nonseed_drift_invalidates_cursor_but_seed_drift_has_specific_error"
            ),
        ),
        "18.e": (
            _nodeid(
                "test_later_page_missing_seed_and_journal_compound_failures_keep_phase_order"
            ),
        ),
        "18.f": (
            _nodeid(
                "test_later_page_missing_seed_and_journal_compound_failures_keep_phase_order"
            ),
        ),
        "18.g": (
            _nodeid(
                "test_later_page_candidate_set_and_oversized_replacement_keep_precedence"
            ),
        ),
        "18.h": (_nodeid("test_workspace_set_drift_and_candidate_count_boundary"),),
        "19.b": (
            _nodeid("test_pagination_allows_page_size_change_and_ignores_own_records"),
        ),
        "19.c": (
            _nodeid("test_pagination_allows_page_size_change_and_ignores_own_records"),
        ),
        "19.d": (
            _nodeid(
                "test_later_page_missing_seed_and_journal_compound_failures_keep_phase_order"
            ),
        ),
        "19.e": (
            _nodeid(
                "test_exact_64_mib_aggregate_journal_is_accepted_and_one_byte_over_refuses"
            ),
        ),
        "20.b": (_nodeid("test_root_and_journal_snapshot_contention_are_fail_closed"),),
        "20.c": (
            _nodeid("test_corrupt_journal_and_semantic_limit_fail_before_success"),
        ),
        "20.d": (
            _nodeid(
                "test_exact_64_mib_aggregate_journal_is_accepted_and_one_byte_over_refuses"
            ),
        ),
        "20.e": (
            _nodeid(
                "test_round_map_success_writes_once_and_append_failure_is_fail_open"
            ),
        ),
        "21.b": (_nodeid("test_real_hostile_docx_packages_refuse_the_complete_map"),),
        "21.c": (_nodeid("test_real_hostile_docx_packages_refuse_the_complete_map"),),
        "21.d": (_nodeid("test_real_hostile_docx_packages_refuse_the_complete_map"),),
        "21.e": (_nodeid("test_candidate_symlink_and_hardlink_fail_closed"),),
        "21.f": (_nodeid("test_candidate_symlink_and_hardlink_fail_closed"),),
        "21.g": (_nodeid("test_one_candidate_parse_refusal_aborts_the_complete_map"),),
        "22.b": (
            _nodeid(
                "test_excluded_container_never_creates_exact_match_or_negative_whole_doc_claim"
            ),
        ),
        "22.c": (
            _nodeid(
                "test_excluded_container_never_creates_exact_match_or_negative_whole_doc_claim"
            ),
        ),
        "23.b": (
            _nodeid(
                "test_real_package_byte_caps_accept_exact_and_publicly_refuse_one_over"
            ),
        ),
        "23.c": (
            _nodeid(
                "test_real_package_byte_caps_accept_exact_and_publicly_refuse_one_over"
            ),
        ),
        "23.d": (
            _nodeid(
                "test_real_package_byte_caps_accept_exact_and_publicly_refuse_one_over"
            ),
        ),
        "23.e": (
            _nodeid(
                "test_real_paragraph_and_character_caps_accept_exact_and_refuse_one_over"
            ),
        ),
        "23.f": (
            _nodeid(
                "test_real_paragraph_and_character_caps_accept_exact_and_refuse_one_over"
            ),
        ),
        "23.g": (
            _nodeid(
                "test_journal_apply_record_cap_accepts_exact_and_publicly_refuses_one_over"
            ),
        ),
        "23.h": (
            _nodeid(
                "test_every_frozen_item_class_cap_is_inclusive_and_refuses_one_over"
            ),
        ),
        "23.i": (
            _nodeid(
                "test_every_frozen_item_class_cap_is_inclusive_and_refuses_one_over"
            ),
        ),
        "23.j": (
            _nodeid(
                "test_every_frozen_item_class_cap_is_inclusive_and_refuses_one_over"
            ),
        ),
        "23.k": (
            _nodeid(
                "test_every_frozen_item_class_cap_is_inclusive_and_refuses_one_over"
            ),
        ),
        "23.l": (
            _nodeid(
                "test_every_frozen_item_class_cap_is_inclusive_and_refuses_one_over"
            ),
        ),
        "23.m": (
            _nodeid(
                "test_every_frozen_item_class_cap_is_inclusive_and_refuses_one_over"
            ),
        ),
        "23.n": (
            _nodeid(
                "test_every_frozen_item_class_cap_is_inclusive_and_refuses_one_over"
            ),
        ),
        "23.o": (
            _nodeid(
                "test_every_frozen_item_class_cap_is_inclusive_and_refuses_one_over"
            ),
        ),
        "23.p": (
            _nodeid(
                "test_every_frozen_item_class_cap_is_inclusive_and_refuses_one_over"
            ),
        ),
        "23.q": (
            _nodeid("test_coherent_70000_complete_map_validates_and_publishes_once"),
            _nodeid("test_70001_refuses_before_item_fingerprinting"),
        ),
        "23.r": (
            _nodeid(
                "test_exact_64_mib_aggregate_journal_is_accepted_and_one_byte_over_refuses"
            ),
        ),
        "23.s": (
            _nodeid(
                "test_support_sample_is_bounded_without_dropping_duplicate_evidence"
            ),
        ),
        "23.t": (
            _nodeid("test_candidate_id_sample_uses_complete_digest_and_bounded_prefix"),
        ),
        "23.u": (
            _nodeid("test_invalid_seed_manifest_cursor_and_limits_use_closed_codes"),
        ),
        "23.v": (
            _nodeid(
                "test_default_page_size_and_cancellation_never_create_partial_success"
            ),
        ),
        "23.w": (
            _nodeid(
                "test_default_page_size_and_cancellation_never_create_partial_success"
            ),
        ),
        "24.b": (
            _nodeid("test_pagination_allows_page_size_change_and_ignores_own_records"),
        ),
        "24.c": (_nodeid("test_cursor_mismatch_precedes_digest_valid_offset_range"),),
        "25.b": (
            _nodeid(
                "test_compact_export_omits_navigation_body_edit_and_verbatim_input_sentinels"
            ),
        ),
        "25.c": (
            _nodeid(
                "test_compact_export_omits_navigation_body_edit_and_verbatim_input_sentinels"
            ),
        ),
        "25.d": (
            _nodeid(
                "test_compact_export_omits_navigation_body_edit_and_verbatim_input_sentinels"
            ),
        ),
        "25.e": (
            _nodeid(
                "test_compact_export_omits_navigation_body_edit_and_verbatim_input_sentinels"
            ),
        ),
        "25.f": (
            _nodeid(
                "test_compact_export_omits_navigation_body_edit_and_verbatim_input_sentinels"
            ),
        ),
        "25.g": (
            _nodeid(
                "test_compact_export_omits_navigation_body_edit_and_verbatim_input_sentinels"
            ),
        ),
        "25.h": (
            _nodeid(
                "test_compact_export_omits_navigation_body_edit_and_verbatim_input_sentinels"
            ),
        ),
        "26.b": (
            _nodeid(
                "test_replace_at_anchor_with_round_trip", module="tests/test_apply.py"
            ),
        ),
        "26.c": (
            _nodeid(
                "test_golden_v1_journal_stays_readable_and_appendable",
                module="tests/test_decision_records.py",
            ),
        ),
        "26.d": (
            _nodeid(
                "test_development_wheel_smoke_binds_metadata_and_eight_tool_surface",
                module="tests/test_release_workflow.py",
            ),
        ),
        "26.e": (
            _nodeid(
                "test_rebuild_comparison_requires_exact_names_and_bytes",
                module="tests/test_reproducible_build.py",
            ),
        ),
    }
)

DESKTOP_READINESS_NODEID = _nodeid(
    "test_round_map_desktop_fixture_text_is_english_and_unicode_safe"
)


def _base_nodeid(nodeid: str) -> str:
    return nodeid.split("[", 1)[0]


def test_acceptance_fixtures_have_clause_level_executable_evidence(
    request: pytest.FixtureRequest,
) -> None:
    subclause_counts = {
        1: 1,
        2: 4,
        3: 2,
        4: 4,
        5: 3,
        6: 1,
        7: 3,
        8: 2,
        9: 2,
        10: 1,
        11: 3,
        12: 2,
        13: 1,
        14: 2,
        15: 2,
        16: 3,
        17: 3,
        18: 8,
        19: 5,
        20: 5,
        21: 7,
        22: 3,
        23: 23,
        24: 3,
        25: 8,
        26: 5,
    }
    expected_clauses = {
        f"{fixture}.{chr(ord('a') + index)}"
        for fixture, count in subclause_counts.items()
        for index in range(count)
    }
    assert set(ACCEPTANCE_CLAUSE_NODEIDS) == expected_clauses
    required = {
        nodeid
        for clause_nodeids in ACCEPTANCE_CLAUSE_NODEIDS.values()
        for nodeid in clause_nodeids
    }
    collected: dict[str, list[pytest.Item]] = {}
    for item in request.session.items:
        collected.setdefault(_base_nodeid(item.nodeid), []).append(item)
    assert required <= set(collected), sorted(required - set(collected))
    for nodeid in required:
        for item in collected[nodeid]:
            assert item.get_closest_marker("skip") is None, nodeid
            assert item.get_closest_marker("skipif") is None, nodeid
            assert item.get_closest_marker("xfail") is None, nodeid


def test_desktop_fixture_27_has_separate_automated_readiness_evidence(
    request: pytest.FixtureRequest,
) -> None:
    collected = {_base_nodeid(item.nodeid) for item in request.session.items}
    assert DESKTOP_READINESS_NODEID in collected


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
