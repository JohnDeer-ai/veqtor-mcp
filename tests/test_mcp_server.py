# SPDX-License-Identifier: Apache-2.0
"""End-to-end smoke test of the MCP tool surface over an in-memory session."""

from copy import deepcopy
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import jsonschema
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import ValidationError

import veqtor_docx
from veqtor_docx import rounds as rounds_module
from veqtor_docx import generate_demo_rounds
from veqtor_docx.contracts import (
    INSPECTION_CRITICAL_CHECK_IDS_V1,
    INSPECT_FIXED_LIMITS_V1,
    InspectionContractError,
    InspectionContractV1,
    REVISION_COUNT_BASIS_V1,
)
from veqtor_mcp import __version__
from veqtor_mcp import records
from veqtor_mcp import server
from veqtor_mcp._inspection_live import CheckedInspectionError, CheckedInspectionResult
from veqtor_mcp.contracts import (
    ExtractRedlinesResult,
    InspectDocumentResult,
    ListRoundsResult,
    MCP_CONTRACT_META_KEY,
    MCP_CONTRACT_SCHEMA_EXTENSION,
    MCP_CONTRACT_SCHEMA_VERSION,
    RECORD_ERROR_PATTERN,
    RECORD_ID_PATTERN,
)
from veqtor_mcp.server import mcp


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _payload(result) -> dict:
    if isinstance(result.structuredContent, dict):
        data = result.structuredContent
        return data.get("result", data)
    return json.loads(result.content[0].text)


def _error_text(result) -> str:
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def _rewrite_docx_part(path: Path, part_name: str, transform) -> None:
    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()
        parts = {info.filename: archive.read(info.filename) for info in infos}
    parts[part_name] = transform(parts[part_name])
    with zipfile.ZipFile(path, "w") as archive:
        for info in infos:
            archive.writestr(info, parts[info.filename])


def _dummy_preflight_proof() -> dict[str, str]:
    """Schema-valid placeholder for tests that fail before proof evaluation."""
    return {
        "schema_version": "preflight_proof.v1",
        "source_sha256": "0" * 64,
        "edits_sha256": "0" * 64,
        "tracked_change_author": "Veqtor MCP",
        "producer_build": "test-build",
        "candidate_sha256": "0" * 64,
        "proof_sha256": "0" * 64,
    }


def _test_producer() -> dict[str, str]:
    return {
        "name": "veqtor-mcp",
        "version": "0.3.0.dev0",
        "build": "source-snapshot-v1-sha256:" + "0" * 64,
    }


def _inspection_model_payload(result: dict) -> dict:
    return {
        **result,
        "producer": _test_producer(),
        "record_id": None,
        "record_status": "disabled",
    }


def _record_metadata_model_payload(demo_dir: Path, meta: dict) -> dict:
    return {
        **veqtor_docx.list_rounds(str(demo_dir)),
        "producer": _test_producer(),
        **meta,
    }


def _canonical_inspection_result(demo_dir: Path, mode: str = "browse") -> dict:
    path = str(demo_dir / "round-1-outgoing-draft.docx")
    if mode == "read_paragraph":
        browse = veqtor_docx.inspect_document(path, "browse", max_items=1)
        return veqtor_docx.inspect_document(
            path,
            "read",
            selection={"paragraph_ref": browse["paragraphs"][0]["paragraph_ref"]},
        )
    if mode == "literal_search":
        return veqtor_docx.inspect_document(
            path,
            mode,
            phrases=["Except as set out in Clause 14.3"],
            match_basis="normalized_literal",
            max_items=100,
        )
    return veqtor_docx.inspect_document(path, mode, max_items=100)


def _mutate_inspection_invariant(result: dict, mutation: str) -> None:
    coverage = result["coverage"]
    container = coverage["container_coverage"]
    inventory = result["revision_inventory"]
    if mutation == "returned_collection_count":
        coverage["returned_item_count"] = 0
    elif mutation == "cursor_presence":
        result["next_cursor"] = "c1:1:" + "0" * 64
    elif mutation == "page_completion":
        coverage["eligible_item_count"] += 1
    elif mutation == "next_cursor_offset":
        page_end = coverage["cursor_offset"] + coverage["returned_item_count"]
        coverage["eligible_item_count"] += 1
        coverage["output_truncated"] = True
        result["next_cursor"] = f"c1:{page_end + 1}:" + "0" * 64
    elif mutation == "literal_complete_count":
        coverage["complete_literal_match_count"] += 1
    elif mutation == "outer_nested_count":
        coverage["indexed_paragraph_count"] += 1
    elif mutation == "nonempty_indexed_count":
        coverage["nonempty_indexed_paragraph_count"] = (
            coverage["indexed_paragraph_count"] + 1
        )
    elif mutation == "container_indexed_partition":
        container["body_paragraph_count"] += 1
    elif mutation == "excluded_subtree_sum":
        container["excluded_subtree_count"] += 1
    elif mutation == "excluded_paragraph_sum":
        container["excluded_paragraph_count"] += 1
    elif mutation == "coverage_complete_flag":
        container["coverage_complete"] = not container["coverage_complete"]
    elif mutation == "legacy_anchor_flag":
        container["legacy_two_field_anchor_safe"] = not container[
            "legacy_two_field_anchor_safe"
        ]
    elif mutation == "container_snapshot":
        divergent = deepcopy(inventory["container_policy"])
        divergent["indexed_paragraph_count"] += 1
        divergent["body_paragraph_count"] += 1
        inventory["container_policy"] = divergent
    elif mutation == "revision_total_partition":
        inventory["total_revision_elements"] += 1
    elif mutation == "revision_in_scope_partition":
        inventory["decoded_revision_elements"] += 1
    elif mutation == "unsupported_occurrence_sum":
        inventory["unsupported_by_kind"] = {"rPrChange": 1}
    elif mutation == "excluded_occurrence_sum":
        inventory["excluded_by_container"] = {"text_box": 1}
    elif mutation == "unsupported_kind_count":
        inventory["unsupported_revision_kind_count"] += 1
    elif mutation == "excluded_kind_count":
        inventory["excluded_container_kind_count"] += 1
    elif mutation == "partition_valid_flag":
        inventory["partition_valid"] = False
    elif mutation == "all_in_scope_flag":
        inventory["all_in_scope_revision_elements_decoded"] = False
    elif mutation == "all_revision_flag":
        inventory["all_revision_elements_decoded"] = False
    elif mutation == "browse_eligible_nonempty":
        coverage["nonempty_indexed_paragraph_count"] -= 1
    elif mutation == "paragraph_exact_page":
        coverage["eligible_item_count"] = 2
        coverage["output_truncated"] = True
        result["next_cursor"] = "c1:1:" + "0" * 64
    elif mutation == "returned_revision_summary":
        result["has_tracked_text_revisions"] = False
        next(item for key in ("matches", "paragraphs") for item in result.get(key, []))[
            "has_tracked_text_revisions"
        ] = True
    elif mutation == "tracked_revision_total":
        inventory["tracked_text_revision_elements"] = (
            inventory["total_revision_elements"] + 1
        )
    elif mutation == "literal_phrase_count_zero":
        result["phrase_count"] = 0
    elif mutation == "literal_phrase_index_out_of_range":
        result["matches"][0]["phrase_index"] = result["phrase_count"]
    elif mutation == "literal_nested_basis":
        result["matches"][0]["match_basis"] = "exact_literal"
    elif mutation == "limit_requested_relation":
        result["limits"]["max_items"] = result["limits"]["requested_max_items"] - 1
    elif mutation == "limit_indexed_relation":
        result["limits"]["max_indexed_paragraphs"] = (
            coverage["indexed_paragraph_count"] - 1
        )
    elif mutation == "included_parts_scope":
        coverage["included_parts"] = []
    elif mutation == "included_containers_scope":
        coverage["included_containers"] = []
    elif mutation == "global_warning_without_revisions":
        result["has_tracked_text_revisions"] = True
    elif mutation == "decoded_exceeds_tracked":
        inventory["total_revision_elements"] = 1
        inventory["in_scope_revision_elements"] = 1
        inventory["decoded_revision_elements"] = 1
        result["has_tracked_text_revisions"] = True
    elif mutation == "decoded_without_global_warning":
        inventory["tracked_text_revision_elements"] = 1
        inventory["total_revision_elements"] = 1
        inventory["in_scope_revision_elements"] = 1
        inventory["decoded_revision_elements"] = 1
    elif mutation == "returned_text_individual_cap":
        result["paragraphs"][0]["text"] = "x" * 50_001
    elif mutation == "returned_text_aggregate_cap":
        for paragraph in result["paragraphs"][:3]:
            paragraph["text"] = "x" * 40_000
    elif mutation == "excluded_scope_prefix":
        coverage["excluded_parts"] = []
    elif mutation == "excluded_scope_unnormalized":
        coverage["excluded_parts"].append("word/../private.html")
    elif mutation == "excluded_scope_unsorted":
        coverage["excluded_parts"].extend(["word/z.html", "word/a.html"])
    elif mutation == "excluded_scope_duplicate":
        coverage["excluded_parts"].extend(["word/a.html", "word/a.html"])
    else:  # pragma: no cover - the parametrized ledger is closed
        raise AssertionError(f"unknown inspection mutation: {mutation}")


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            ListRoundsResult,
            {
                "folder": "demo",
                "ordering_source": "filename_lexicographic_v1",
                "order_basis": {},
                "rounds": [],
                "skipped": [],
            },
        ),
        (
            ExtractRedlinesResult,
            {
                "path": "demo/round.docx",
                "file_sha256": "0" * 64,
                "part_name": "word/document.xml",
                "revision_count": 0,
                "change_units": [],
                "unsupported_revisions": {},
                "revision_inventory": {},
            },
        ),
    ],
)
def test_revision_count_basis_is_enforced_by_runtime_result_models(
    model,
    payload: dict,
) -> None:
    with pytest.raises(ValidationError) as error:
        model.model_validate(
            {
                **payload,
                "revision_count_basis": "wrong",
                "producer": _test_producer(),
                "record_id": None,
                "record_status": "disabled",
            }
        )
    assert {tuple(item["loc"]) for item in error.value.errors()} == {
        ("revision_count_basis",)
    }


def test_inspect_runtime_result_model_enforces_advertised_mode_shape(
    demo_dir: Path,
) -> None:
    outline = _canonical_inspection_result(demo_dir, "outline")
    literal = _canonical_inspection_result(demo_dir, "literal_search")
    browse = _canonical_inspection_result(demo_dir)
    path = str(demo_dir / "round-1-outgoing-draft.docx")
    paragraph_read = veqtor_docx.inspect_document(
        path,
        "read",
        selection={"paragraph_ref": browse["paragraphs"][0]["paragraph_ref"]},
    )
    section_read = veqtor_docx.inspect_document(
        path,
        "read",
        selection={"section_ref": outline["sections"][0]["section_ref"]},
    )

    for result in (outline, literal, browse, paragraph_read, section_read):
        payload = _inspection_model_payload(result)
        normalized = InspectDocumentResult.model_validate(payload).model_dump(
            mode="json"
        )
        jsonschema.validate(
            instance=normalized,
            schema=InspectDocumentResult.contract_schema,
        )

    additive = deepcopy(browse)
    additive["future_top_level_extension"] = {"version": 1}
    normalized_additive = InspectDocumentResult.model_validate(
        _inspection_model_payload(additive)
    ).model_dump(mode="json")
    assert normalized_additive["future_top_level_extension"] == {"version": 1}
    jsonschema.validate(
        instance=normalized_additive,
        schema=InspectDocumentResult.contract_schema,
    )
    checked_additive = server._validated_success_result("inspect_document", additive)
    assert isinstance(checked_additive, CheckedInspectionResult)
    assert checked_additive.to_dict()["future_top_level_extension"] == {"version": 1}

    invalid = []
    missing_collection = deepcopy(outline)
    missing_collection.pop("sections")
    invalid.append(missing_collection)
    extra_collection = deepcopy(outline)
    extra_collection["paragraphs"] = []
    invalid.append(extra_collection)
    missing_literal_metadata = deepcopy(literal)
    missing_literal_metadata.pop("match_basis")
    invalid.append(missing_literal_metadata)
    missing_selection_kind = deepcopy(paragraph_read)
    missing_selection_kind.pop("selection_kind")
    invalid.append(missing_selection_kind)
    missing_navigation = deepcopy(section_read)
    missing_navigation.pop("section_navigation")
    invalid.append(missing_navigation)
    extra_nested_coverage = deepcopy(browse)
    extra_nested_coverage["coverage"]["future_nested_extension"] = True
    invalid.append(extra_nested_coverage)
    extra_nested_item = deepcopy(browse)
    extra_nested_item["paragraphs"][0]["future_nested_extension"] = True
    invalid.append(extra_nested_item)
    for result in invalid:
        payload = _inspection_model_payload(result)
        normalized = InspectDocumentResult.model_validate(payload).model_dump(
            mode="json"
        )
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(
                instance=normalized,
                schema=InspectDocumentResult.contract_schema,
            )


@pytest.mark.parametrize(
    ("mode", "mutation", "expected_path"),
    [
        ("browse", "returned_collection_count", "coverage.returned_item_count"),
        ("browse", "cursor_presence", "coverage.output_truncated"),
        ("browse", "page_completion", "coverage.output_truncated"),
        ("browse", "next_cursor_offset", "next_cursor"),
        (
            "literal_search",
            "literal_complete_count",
            "coverage.complete_literal_match_count",
        ),
        ("browse", "outer_nested_count", "coverage.indexed_paragraph_count"),
        (
            "browse",
            "nonempty_indexed_count",
            "coverage.nonempty_indexed_paragraph_count",
        ),
        (
            "browse",
            "container_indexed_partition",
            "coverage.container_coverage.indexed_paragraph_count",
        ),
        (
            "browse",
            "excluded_subtree_sum",
            "coverage.container_coverage.excluded_subtree_count",
        ),
        (
            "browse",
            "excluded_paragraph_sum",
            "coverage.container_coverage.excluded_paragraph_count",
        ),
        (
            "browse",
            "coverage_complete_flag",
            "coverage.container_coverage.coverage_complete",
        ),
        (
            "browse",
            "legacy_anchor_flag",
            "coverage.container_coverage.legacy_two_field_anchor_safe",
        ),
        ("browse", "container_snapshot", "revision_inventory.container_policy"),
        (
            "browse",
            "revision_total_partition",
            "revision_inventory.total_revision_elements",
        ),
        (
            "browse",
            "revision_in_scope_partition",
            "revision_inventory.in_scope_revision_elements",
        ),
        (
            "browse",
            "unsupported_occurrence_sum",
            "revision_inventory.unsupported_revision_occurrences",
        ),
        (
            "browse",
            "excluded_occurrence_sum",
            "revision_inventory.excluded_container_occurrences",
        ),
        (
            "browse",
            "unsupported_kind_count",
            "revision_inventory.unsupported_revision_kind_count",
        ),
        (
            "browse",
            "excluded_kind_count",
            "revision_inventory.excluded_container_kind_count",
        ),
        ("browse", "partition_valid_flag", "revision_inventory.partition_valid"),
        (
            "browse",
            "all_in_scope_flag",
            "revision_inventory.all_in_scope_revision_elements_decoded",
        ),
        (
            "browse",
            "all_revision_flag",
            "revision_inventory.all_revision_elements_decoded",
        ),
        (
            "browse",
            "browse_eligible_nonempty",
            "coverage.eligible_item_count",
        ),
        (
            "read_paragraph",
            "paragraph_exact_page",
            "coverage.eligible_item_count",
        ),
        (
            "browse",
            "returned_revision_summary",
            "has_tracked_text_revisions",
        ),
        (
            "browse",
            "tracked_revision_total",
            "revision_inventory.tracked_text_revision_elements",
        ),
        ("literal_search", "literal_phrase_count_zero", "phrase_count"),
        (
            "literal_search",
            "literal_phrase_index_out_of_range",
            "matches.phrase_index",
        ),
        (
            "literal_search",
            "literal_nested_basis",
            "matches.match_basis",
        ),
        ("browse", "limit_requested_relation", "limits.max_items"),
        (
            "browse",
            "limit_indexed_relation",
            "limits.max_indexed_paragraphs",
        ),
        ("browse", "included_parts_scope", "coverage.included_parts"),
        (
            "browse",
            "included_containers_scope",
            "coverage.included_containers",
        ),
        (
            "browse",
            "global_warning_without_revisions",
            "has_tracked_text_revisions",
        ),
        (
            "browse",
            "decoded_exceeds_tracked",
            "revision_inventory.tracked_text_revision_elements",
        ),
        (
            "browse",
            "decoded_without_global_warning",
            "has_tracked_text_revisions",
        ),
        (
            "browse",
            "returned_text_individual_cap",
            "returned_text.max_individual_chars",
        ),
        (
            "browse",
            "returned_text_aggregate_cap",
            "returned_text.max_total_chars",
        ),
        ("browse", "excluded_scope_prefix", "coverage.excluded_parts"),
        ("browse", "excluded_scope_unnormalized", "coverage.excluded_parts"),
        ("browse", "excluded_scope_unsorted", "coverage.excluded_parts"),
        ("browse", "excluded_scope_duplicate", "coverage.excluded_parts"),
    ],
)
def test_inspect_runtime_rejects_cross_field_invariant_mutations(
    demo_dir: Path,
    mode: str,
    mutation: str,
    expected_path: str,
) -> None:
    result = _canonical_inspection_result(demo_dir, mode)
    _mutate_inspection_invariant(result, mutation)

    with pytest.raises(InspectionContractError) as error:
        InspectionContractV1.validate_critical(result)
    assert expected_path in str(error.value)

    with pytest.raises(server._OutputContractError):
        server._validated_success_result("inspect_document", result)


def test_inspect_runtime_accepts_all_modes_and_page_shapes(demo_dir: Path) -> None:
    path = str(demo_dir / "round-1-outgoing-draft.docx")

    def produce(mode: str, **kwargs) -> dict:
        result = veqtor_docx.inspect_document(path, mode, **kwargs)
        server._validated_success_result("inspect_document", result)
        return result

    outline = produce("outline", max_items=100)
    produce(
        "literal_search",
        phrases=["phrase absent from synthetic fixture"],
        match_basis="exact_literal",
    )

    literal_arguments = {
        "phrases": [
            "EXCEPT AS SET OUT IN CLAUSE 14.3",
            "except as set out in clause 14.3",
        ],
        "match_basis": "normalized_casefold_literal",
        "max_items": 1,
    }
    literal_first = produce(
        "literal_search",
        **literal_arguments,
    )
    literal_last = produce(
        "literal_search",
        cursor=literal_first["next_cursor"],
        **literal_arguments,
    )
    assert literal_first["next_cursor"] is not None
    assert literal_last["next_cursor"] is None

    browse_pages = []
    cursor = None
    while True:
        page = produce(
            "browse",
            cursor=cursor,
            max_items=1,
        )
        browse_pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(browse_pages) >= 3
    paragraph_ref = browse_pages[0]["paragraphs"][0]["paragraph_ref"]
    produce(
        "read",
        selection={"paragraph_ref": paragraph_ref},
    )

    section_ref = next(
        section["section_ref"]
        for section in outline["sections"]
        if section["label"] == "14.2"
    )
    section_pages = []
    cursor = None
    while True:
        page = produce(
            "read",
            selection={"section_ref": section_ref},
            cursor=cursor,
            max_items=1,
        )
        section_pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(section_pages) >= 2


def test_inspection_critical_catalog_is_closed_and_bounded() -> None:
    checks = InspectionContractV1.critical_checks
    check_ids = [entry.check_id for entry in checks]

    assert set(check_ids) == INSPECTION_CRITICAL_CHECK_IDS_V1
    assert len(check_ids) == len(set(check_ids))
    assert len({entry.evaluator for entry in checks}) == len(checks)
    assert "shape.mode-and-nested-v1" not in check_ids
    specification = (
        Path(__file__).resolve().parents[1] / "INSPECT_DOCUMENT_V0.3.md"
    ).read_text(encoding="utf-8")
    assert "authoritative producer" in specification
    assert "does not independently prove" in specification
    assert all(
        specification.count(f"`{check_id}`") == 1
        for check_id in INSPECTION_CRITICAL_CHECK_IDS_V1
    )


def test_inspection_producer_pagination_recomposes_without_gaps_or_duplicates(
    demo_dir: Path,
) -> None:
    path = str(demo_dir / "round-1-outgoing-draft.docx")
    outline = veqtor_docx.inspect_document(path, "outline", max_items=100)
    section_ref = next(
        section["section_ref"]
        for section in outline["sections"]
        if section["label"] == "14.2"
    )
    scenarios = (
        ("browse", "paragraphs", {}),
        ("outline", "sections", {}),
        (
            "literal_search",
            "matches",
            {
                "phrases": [
                    "EXCEPT AS SET OUT IN CLAUSE 14.3",
                    "except as set out in clause 14.3",
                ],
                "match_basis": "normalized_casefold_literal",
            },
        ),
        ("read", "paragraphs", {"selection": {"section_ref": section_ref}}),
    )

    for mode, collection_key, arguments in scenarios:
        complete = veqtor_docx.inspect_document(
            path,
            mode,
            max_items=100,
            **arguments,
        )
        expected_items = complete[collection_key]
        cursors_by_offset: dict[int, str] = {}
        for page_size in (1, 2, 3):
            recomposed = []
            cursor = None
            while True:
                request = {
                    **arguments,
                    "max_items": page_size,
                    **({"cursor": cursor} if cursor is not None else {}),
                }
                page = veqtor_docx.inspect_document(path, mode, **request)
                assert page == veqtor_docx.inspect_document(path, mode, **request)
                assert page["coverage"]["cursor_offset"] == len(recomposed)
                page_items = page[collection_key]
                assert page["coverage"]["returned_item_count"] == len(page_items)
                recomposed.extend(page_items)
                next_cursor = page["next_cursor"]
                if next_cursor is None:
                    break
                next_offset = len(recomposed)
                assert (
                    cursors_by_offset.setdefault(next_offset, next_cursor)
                    == next_cursor
                )
                cursor = next_cursor

            assert recomposed == expected_items
            identities = [
                json.dumps(item, ensure_ascii=False, sort_keys=True)
                for item in recomposed
            ]
            assert len(identities) == len(set(identities))


def test_inspection_producer_fixture_binds_sha_refs_and_literal_attribution(
    demo_dir: Path,
) -> None:
    source = demo_dir / "round-1-outgoing-draft.docx"
    path = str(source)
    expected_file_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    browse = veqtor_docx.inspect_document(path, "browse", max_items=100)

    assert browse["file_sha256"] == expected_file_sha256
    paragraphs_by_index = {}
    for paragraph in browse["paragraphs"]:
        ref = paragraph["paragraph_ref"]
        paragraphs_by_index[ref["paragraph_index"]] = paragraph
        assert ref["file_sha256"] == expected_file_sha256
        assert (
            ref["paragraph_text_sha256"]
            == hashlib.sha256(paragraph["text"].encode("utf-8")).hexdigest()
        )

    outline = veqtor_docx.inspect_document(path, "outline", max_items=100)
    for section in outline["sections"]:
        ref = section["section_ref"]
        heading_paragraph = paragraphs_by_index[ref["heading_paragraph_index"]]
        assert ref["file_sha256"] == expected_file_sha256
        assert (
            ref["heading_text_sha256"]
            == heading_paragraph["paragraph_ref"]["paragraph_text_sha256"]
        )

    phrase = "Except as set out in Clause 14.3"
    literal = veqtor_docx.inspect_document(
        path,
        "literal_search",
        phrases=[phrase],
        match_basis="exact_literal",
        max_items=100,
    )
    assert literal["matches"]
    known_refs = {
        json.dumps(item["paragraph_ref"], sort_keys=True)
        for item in browse["paragraphs"]
    }
    for match in literal["matches"]:
        assert match["phrase_index"] == 0
        assert match["match_basis"] == "exact_literal"
        assert json.dumps(match["paragraph_ref"], sort_keys=True) in known_refs
        snippet = match["snippet"]
        assert snippet["text"][snippet["match_start"] : snippet["match_end"]] == phrase


def test_checked_inspection_result_is_private_immutable_and_copied(
    demo_dir: Path,
) -> None:
    with pytest.raises(TypeError):
        CheckedInspectionResult()
    with pytest.raises(TypeError):
        CheckedInspectionError()

    checked = server._validated_success_result(
        "inspect_document",
        _canonical_inspection_result(demo_dir),
    )
    assert isinstance(checked, CheckedInspectionResult)
    with pytest.raises(TypeError):
        checked.view["path"] = "tampered"  # type: ignore[index]
    with pytest.raises(TypeError):
        checked.view["coverage"]["cursor_offset"] = 99  # type: ignore[index]
    with pytest.raises(TypeError):
        checked.view["paragraphs"][0]["text"] = "tampered"  # type: ignore[index]

    mutable = checked.to_dict()
    mutable["coverage"]["cursor_offset"] = 99
    mutable["paragraphs"][0]["text"] = "tampered"
    assert checked.view["coverage"]["cursor_offset"] == 0
    assert checked.view["paragraphs"][0]["text"] != "tampered"


def test_checked_inspection_sink_rejects_raw_result_before_write(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = []
    monkeypatch.setattr(
        records, "_write_record", lambda **kwargs: writes.append(kwargs)
    )

    with pytest.raises(TypeError, match="checked result"):
        records.write_checked_inspection_record(
            workspace=demo_dir,
            input_payload={"path": "private", "mode": "browse"},
            result={"status": "ok"},  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="checked error"):
        records.write_checked_inspection_error_record(
            workspace=demo_dir,
            input_payload={"path": "private", "mode": "browse"},
            error={"status": "error"},  # type: ignore[arg-type]
        )

    assert writes == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "error"),
        ("error_code", "producer_spoof"),
        ("error", "PRIVATE DOCUMENT TEXT"),
    ],
)
def test_checked_inspection_sink_rejects_minted_reserved_fields(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    result = _canonical_inspection_result(demo_dir)
    result[field] = value
    mistakenly_minted = server._checked_inspection_result_from_gate(result)
    writes = []
    monkeypatch.setattr(
        records, "_write_record", lambda **kwargs: writes.append(kwargs)
    )

    assert records.write_checked_inspection_record(
        workspace=demo_dir,
        input_payload={"path": "private", "mode": "browse"},
        result=mistakenly_minted,
    ) == {
        "record_id": None,
        "record_status": "write_failed",
        "record_error": "record_invalid",
    }
    assert writes == []


def test_generic_writer_refuses_all_live_inspection_records(
    tmp_path: Path,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()

    refused = records.write_record(
        workspace=matter,
        tool_name="inspect_document",
        input_payload={"path": "private", "mode": "browse"},
        result={"status": "ok", "mode": "browse"},
        provenance={},
    )
    assert refused == {
        "record_id": None,
        "record_status": "write_failed",
        "record_error": "record_invalid",
    }
    assert (
        records._write_record(
            workspace=matter,
            tool_name="inspect_document",
            input_payload={"path": "private", "mode": "browse"},
            result={"status": "ok", "mode": "browse"},
            provenance={},
        )
        == refused
    )
    assert not (matter / records.SIDECAR_DIR / records.JOURNAL_NAME).exists()

    private_text = "PRIVATE DOCUMENT TEXT /Users/private/matter.docx"
    raw_error = {
        "status": "error",
        "error_code": "output_contract_error",
        "error": "output_contract_error: tool output failed contract validation",
        "paragraphs": [{"text": private_text}],
    }
    for writer in (records.write_record, records._write_record):
        assert (
            writer(
                workspace=matter,
                tool_name="inspect_document",
                input_payload={"path": "private", "mode": "browse"},
                result=raw_error,
                tool_result=raw_error,
                provenance={"private": private_text},
            )
            == refused
        )
    assert not (matter / records.SIDECAR_DIR / records.JOURNAL_NAME).exists()


def test_live_inspection_digest_and_response_share_checked_payload(
    tmp_path: Path,
) -> None:
    matter = tmp_path / "matter"
    generate_demo_rounds(matter)
    response = server.inspect_document(
        str(matter / "round-1-outgoing-draft.docx"),
        "browse",
        max_items=2,
    )
    raw = records.read_records(
        str(matter),
        max_records=1,
        include_payload=True,
    )["records"][0]
    metadata_fields = {"record_id", "record_status", "record_error"}
    returned_payload = {
        key: value for key, value in response.items() if key not in metadata_fields
    }
    canonical_payload = json.dumps(
        returned_payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    legacy_preimage = json.dumps(
        {"status": "ok", **returned_payload},
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert raw["tool_result_sha256"] == hashlib.sha256(canonical_payload).hexdigest()
    assert raw["tool_result_sha256"] != hashlib.sha256(legacy_preimage).hexdigest()
    assert raw["record_type"] == "inspection.v1"
    assert raw["result"]["paragraph_count"] == 2
    assert "paragraphs" not in raw["result"]
    assert response["paragraphs"][0]["text"] not in json.dumps(
        raw["result"], ensure_ascii=False
    )

    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    stored_frame = journal.read_bytes()
    response["coverage"]["cursor_offset"] = 999
    response["paragraphs"][0]["text"] = "post-return mutation"
    assert journal.read_bytes() == stored_frame
    reread = records.read_records(
        str(matter),
        max_records=1,
        include_payload=True,
    )["records"][0]
    assert reread["tool_result_sha256"] == raw["tool_result_sha256"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "error"),
        ("error_code", "producer_spoof"),
        ("error", "PRIVATE DOCUMENT TEXT"),
    ],
)
def test_inspection_success_gate_reserves_error_authority_fields(
    demo_dir: Path,
    field: str,
    value: str,
) -> None:
    result = _canonical_inspection_result(demo_dir)
    result[field] = value

    with pytest.raises(server._OutputContractError):
        server._validated_success_result("inspect_document", result)


def test_inspect_contract_accepts_returned_text_caps_inclusively(
    demo_dir: Path,
) -> None:
    result = _canonical_inspection_result(demo_dir)

    def clear_document_strings(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"text", "heading", "label"} and isinstance(item, str):
                    value[key] = ""
                elif isinstance(item, (dict, list)):
                    clear_document_strings(item)
        elif isinstance(value, list):
            for item in value:
                clear_document_strings(item)

    clear_document_strings(result["paragraphs"])
    result["paragraphs"][0]["text"] = "a" * 50_000
    result["paragraphs"][1]["text"] = "b" * 50_000
    InspectionContractV1.validate_critical(result)


@pytest.mark.parametrize(
    ("top_warning", "unsupported_kind"),
    [(True, "moveFrom"), (False, "rPrChange")],
)
def test_inspect_contract_accepts_nondecoded_revision_boundaries(
    demo_dir: Path,
    top_warning: bool,
    unsupported_kind: str,
) -> None:
    result = _canonical_inspection_result(demo_dir)
    inventory = result["revision_inventory"]
    inventory.update(
        tracked_text_revision_elements=0,
        total_revision_elements=1,
        in_scope_revision_elements=1,
        decoded_revision_elements=0,
        unsupported_revision_occurrences=1,
        unsupported_revision_kind_count=1,
        unsupported_by_kind={unsupported_kind: 1},
        all_in_scope_revision_elements_decoded=False,
        all_revision_elements_decoded=False,
    )
    result["has_tracked_text_revisions"] = top_warning
    InspectionContractV1.validate_critical(result)


def test_inspect_contract_accepts_normalized_sorted_dynamic_excluded_scope(
    demo_dir: Path,
) -> None:
    result = _canonical_inspection_result(demo_dir)
    result["coverage"]["excluded_parts"].extend(
        ["word/chunks/a.html", "word/chunks/z.html"]
    )
    InspectionContractV1.validate_critical(result)


@pytest.mark.anyio
async def test_protocol_initialization_reports_veqtor_version(monkeypatch) -> None:
    initialize = ClientSession.initialize
    observed_results = []

    async def capture_initialize(session):
        result = await initialize(session)
        observed_results.append(result)
        return result

    monkeypatch.setattr(ClientSession, "initialize", capture_initialize)

    async with create_connected_server_and_client_session(mcp._mcp_server):
        pass

    assert len(observed_results) == 1
    assert observed_results[0].serverInfo.name == "veqtor"
    assert observed_results[0].serverInfo.version == __version__


@pytest.mark.anyio
async def test_tool_contracts_are_versioned_typed_and_honestly_annotated() -> None:
    expected_output_core = {
        "list_rounds": {
            "folder",
            "ordering_source",
            "order_basis",
            "revision_count_basis",
            "rounds",
        },
        "extract_redlines": {
            "path",
            "file_sha256",
            "revision_count",
            "revision_count_basis",
            "change_units",
            "unsupported_revisions",
        },
        "inspect_document": {
            "mode",
            "file_sha256",
            "search_scope",
            "reading_mode",
            "container_policy",
            "coverage",
            "limits",
            "next_cursor",
        },
        "preflight_edits": {
            "source_sha256",
            "batch_applicable",
            "candidate_sha256",
            "edits",
            "preflight_proof",
        },
        "apply_edits": {
            "source_sha256",
            "output_sha256",
            "applied",
            "round_trip_check",
            "preflight_binding_status",
            "preflight_candidate_sha256",
            "candidate_output_sha256_match",
        },
        "verify_quote": {
            "verdict",
            "checked_anchor",
            "matches",
            "diff",
        },
        "export_decision_record": {
            "total_count",
            "records",
            "assurance",
            "current_export_event",
        },
    }

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}

    assert set(tools) == records.WRITABLE_TOOL_NAMES
    assert set(server._RESULT_MODELS) == set(tools)
    assert all(callable(getattr(server, name)) for name in tools)
    for name, tool in tools.items():
        registered_tool = mcp._tool_manager.get_tool(name)
        assert registered_tool is not None
        assert registered_tool.fn_metadata.output_model is server._RESULT_MODELS[name]
        assert server._RESULT_MODELS[name].contract_schema == tool.outputSchema
        assert tool.meta == {MCP_CONTRACT_META_KEY: MCP_CONTRACT_SCHEMA_VERSION}
        assert tool.outputSchema is not None
        assert tool.outputSchema[MCP_CONTRACT_SCHEMA_EXTENSION] == (
            MCP_CONTRACT_SCHEMA_VERSION
        )
        assert tool.outputSchema["type"] == "object"
        assert expected_output_core[name] <= set(tool.outputSchema["properties"])
        assert tool.outputSchema["properties"]["producer"] == {
            "type": "object",
            "properties": {
                "name": {"const": "veqtor-mcp"},
                "version": {"type": "string", "minLength": 1},
                "build": {"type": "string", "minLength": 1},
            },
            "required": ["name", "version", "build"],
            "additionalProperties": False,
        }
        assert "producer" in tool.outputSchema["required"]
        assert tool.outputSchema["properties"]["record_id"] == {
            "anyOf": [
                {"type": "string", "pattern": RECORD_ID_PATTERN},
                {"type": "null"},
            ]
        }
        assert tool.outputSchema["properties"]["record_error"] == {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "pattern": RECORD_ERROR_PATTERN,
        }
        assert "record_error" not in tool.outputSchema["required"]
        metadata_variants = tool.outputSchema["allOf"][0]["oneOf"]
        assert [
            item["properties"]["record_status"]["const"] for item in metadata_variants
        ] == ["written", "disabled", "write_failed"]
        assert all(
            item["not"] == {"required": ["record_error"]}
            for item in metadata_variants[:2]
        )
        assert "record_error" in metadata_variants[2]["required"]
        assert tool.annotations is not None
        # Every tool can append local provenance (export appends an access
        # event), so complete calls are neither read-only nor idempotent.
        assert tool.annotations.readOnlyHint is False
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is False
        assert tool.annotations.openWorldHint is False

    for name in ("preflight_edits", "apply_edits"):
        edit_schema = tools[name].inputSchema["properties"]["edits"]["items"]
        assert edit_schema["additionalProperties"] is False
        anchor_variants = edit_schema["properties"]["anchor"]["oneOf"]
        assert len(anchor_variants) == 2
        assert all(item["additionalProperties"] is False for item in anchor_variants)
        assert anchor_variants[0]["required"] == [
            "change_unit_id",
            "file_sha256",
        ]
        assert set(anchor_variants[1]["required"]) == {
            "schema_version",
            "change_unit_id",
            "file_sha256",
            "container_policy",
            "unit_fingerprint_sha256",
        }

    verify_anchor = tools["verify_quote"].inputSchema["properties"]["anchor"]
    assert len(verify_anchor["oneOf"]) == 3
    assert all(item["additionalProperties"] is False for item in verify_anchor["oneOf"])
    assert verify_anchor["oneOf"][2]["properties"]["schema_version"] == {
        "const": "paragraph_ref.v1"
    }
    inspect_schema = tools["inspect_document"].inputSchema
    assert inspect_schema["properties"]["mode"]["enum"] == [
        "outline",
        "literal_search",
        "browse",
        "read",
    ]
    max_items_schema = inspect_schema["properties"]["max_items"]
    assert max_items_schema["type"] == "integer"
    assert max_items_schema["minimum"] == 1
    assert max_items_schema["maximum"] == INSPECT_FIXED_LIMITS_V1["max_items"]
    assert max_items_schema["default"] == server.DEFAULT_INSPECT_MAX_ITEMS
    assert tools["export_decision_record"].inputSchema["properties"]["max_records"][
        "anyOf"
    ] == [
        {
            "type": "integer",
            "minimum": 1,
            "maximum": records.MAX_MAX_RECORDS,
        },
        {"type": "null"},
    ]
    selection_schema = inspect_schema["properties"]["selection"]["anyOf"][0]
    assert selection_schema["additionalProperties"] is False
    assert set(selection_schema["properties"]) == {
        "paragraph_ref",
        "section_ref",
    }
    inspect_output = tools["inspect_document"].outputSchema
    assert inspect_output["additionalProperties"] is True
    assert set(inspect_output["required"]) == {
        "mode",
        "path",
        "file_sha256",
        "part_name",
        "search_scope",
        "reading_mode",
        "container_policy",
        "has_tracked_text_revisions",
        "revision_inventory",
        "coverage",
        "limits",
        "next_cursor",
        "producer",
        "record_id",
        "record_status",
    }
    inspect_output_variants = inspect_output["oneOf"]
    assert [
        item["properties"]["mode"]["const"] for item in inspect_output_variants
    ] == [
        "outline",
        "literal_search",
        "browse",
        "read",
        "read",
    ]
    assert [item["required"] for item in inspect_output_variants] == [
        ["sections"],
        ["matches", "match_basis", "phrase_count"],
        ["paragraphs"],
        ["paragraphs", "selection_kind"],
        ["paragraphs", "selection_kind", "section_navigation"],
    ]
    inspect_coverage = inspect_output["properties"]["coverage"]
    assert inspect_coverage["additionalProperties"] is False
    assert inspect_output["properties"]["limits"]["additionalProperties"] is False
    assert (
        inspect_output["properties"]["revision_inventory"]["additionalProperties"]
        is False
    )
    for collection in ("sections", "matches", "paragraphs"):
        assert (
            inspect_output["properties"][collection]["items"]["additionalProperties"]
            is False
        )
    assert "indexed_paragraph_count" in inspect_coverage["required"]
    assert "nonempty_indexed_paragraph_count" in inspect_coverage["required"]
    assert "body_paragraph_count" not in inspect_coverage["properties"]
    assert "nonempty_body_paragraph_count" not in inspect_coverage["properties"]
    assert inspect_coverage["properties"]["included_parts"] == {
        "const": ["word/document.xml"]
    }
    assert inspect_coverage["properties"]["included_containers"] == {
        "const": ["body", "table_cell"]
    }
    inspect_limits = inspect_output["properties"]["limits"]
    assert inspect_limits["properties"]["requested_max_items"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
    }
    assert inspect_limits["properties"]["max_items"] == {"const": 100}
    apply_schema = tools["apply_edits"].inputSchema
    assert "preflight_proof" in apply_schema["required"]
    assert "preflight_proof" in tools["preflight_edits"].outputSchema["required"]
    proof_schema = apply_schema["properties"]["preflight_proof"]
    assert proof_schema["additionalProperties"] is False
    assert set(proof_schema["required"]) == {
        "schema_version",
        "source_sha256",
        "edits_sha256",
        "tracked_change_author",
        "producer_build",
        "candidate_sha256",
        "proof_sha256",
    }
    diagnostic_schema = tools["preflight_edits"].outputSchema["properties"]["edits"][
        "items"
    ]
    assert diagnostic_schema["properties"]["position_status"]["enum"] == [
        "supported",
        "unsupported",
        "not_evaluated",
    ]
    assert "position_supported" not in diagnostic_schema["properties"]
    assert tools["preflight_edits"].outputSchema["properties"]["failure_phase"][
        "anyOf"
    ][0]["enum"] == [
        "validation",
        "source",
        "matching",
        "planning",
        "surgery",
        "serialization",
        "round_trip",
        "preflight_binding",
        "publication",
    ]
    ordered_filenames = tools["list_rounds"].inputSchema["properties"][
        "ordered_filenames"
    ]
    ordered_sequence_schema = next(
        branch for branch in ordered_filenames["anyOf"] if branch.get("type") == "array"
    )
    assert ordered_sequence_schema["items"] == {"type": "string"}
    assert "ordered_filenames" not in tools["list_rounds"].inputSchema["required"]
    assert tools["export_decision_record"].outputSchema["properties"][
        "current_export_event"
    ]["properties"]["record_status"]["enum"] == [
        "written",
        "disabled",
        "write_failed",
    ]
    for tool_name in ("list_rounds", "extract_redlines"):
        output_schema = tools[tool_name].outputSchema
        assert output_schema["properties"]["revision_count_basis"] == {
            "const": REVISION_COUNT_BASIS_V1
        }
        assert "revision_count_basis" in output_schema["required"]


def test_advertised_sha256_schema_has_exact_length_and_strict_end() -> None:
    schema = InspectDocumentResult.contract_schema["properties"]["file_sha256"]
    assert schema == {
        "type": "string",
        "minLength": 64,
        "maxLength": 64,
        "pattern": r"^[0-9a-f]{64}(?![\s\S])",
    }
    validator = jsonschema.Draft202012Validator(schema)

    assert validator.is_valid("0" * 64)
    for invalid in ("0" * 63, "0" * 65, "A" * 64, "0" * 64 + "\n", "0" * 64 + "\r\n"):
        assert not validator.is_valid(invalid)


@pytest.mark.anyio
@pytest.mark.parametrize("malformed", [True, 1.0, "1"])
@pytest.mark.parametrize(
    ("tool_name", "limit_name"),
    [
        ("inspect_document", "max_items"),
        ("export_decision_record", "max_records"),
    ],
)
async def test_mcp_rejects_coercible_non_integer_limits_before_execution(
    tmp_path: Path,
    tool_name: str,
    limit_name: str,
    malformed: object,
) -> None:
    matter = tmp_path / "matter"
    generate_demo_rounds(matter)
    arguments = (
        {
            "path": str(matter / "round-1-outgoing-draft.docx"),
            "mode": "browse",
            limit_name: malformed,
        }
        if tool_name == "inspect_document"
        else {"workspace": str(matter), limit_name: malformed}
    )

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        response = await session.call_tool(tool_name, arguments)

    assert response.isError is True
    assert "validation error" in _error_text(response).casefold()
    assert not (matter / records.SIDECAR_DIR / records.JOURNAL_NAME).exists()


@pytest.mark.anyio
@pytest.mark.parametrize("max_items", [1, INSPECT_FIXED_LIMITS_V1["max_items"]])
async def test_mcp_strict_integer_limits_accept_real_integers(
    tmp_path: Path,
    max_items: int,
) -> None:
    matter = tmp_path / "matter"
    generate_demo_rounds(matter)

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        inspected = await session.call_tool(
            "inspect_document",
            {
                "path": str(matter / "round-1-outgoing-draft.docx"),
                "mode": "browse",
                "max_items": max_items,
            },
        )
        exported = await session.call_tool(
            "export_decision_record",
            {
                "workspace": str(matter),
                "max_records": records.MAX_MAX_RECORDS,
            },
        )

    assert inspected.isError is False
    inspected_payload = _payload(inspected)
    assert inspected_payload["limits"]["requested_max_items"] == max_items
    assert inspected_payload["coverage"]["returned_item_count"] == min(
        max_items,
        inspected_payload["coverage"]["eligible_item_count"],
    )
    assert exported.isError is False
    assert _payload(exported)["returned_count"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", [0, INSPECT_FIXED_LIMITS_V1["max_items"] + 1])
async def test_mcp_rejects_out_of_range_max_items_before_execution(
    tmp_path: Path,
    invalid: int,
) -> None:
    matter = tmp_path / "matter"
    generate_demo_rounds(matter)

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        response = await session.call_tool(
            "inspect_document",
            {
                "path": str(matter / "round-1-outgoing-draft.docx"),
                "mode": "browse",
                "max_items": invalid,
            },
        )

    assert response.isError is True
    assert "validation error" in _error_text(response).casefold()
    assert not (matter / records.SIDECAR_DIR / records.JOURNAL_NAME).exists()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid",
    [records.MAX_MAX_RECORDS + 1, 10**records.MAX_JSON_INTEGER_DIGITS],
)
async def test_mcp_rejects_max_records_above_advertised_bound_before_execution(
    tmp_path: Path,
    invalid: int,
) -> None:
    matter = tmp_path / "matter"
    generate_demo_rounds(matter)

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        response = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "max_records": invalid},
        )

    assert response.isError is True
    assert "validation error" in _error_text(response).casefold()
    assert not (matter / records.SIDECAR_DIR / records.JOURNAL_NAME).exists()


def test_inspect_server_rejects_adversarial_int_subclass_before_success(
    tmp_path: Path,
) -> None:
    class Bypass(int):
        def __ge__(self, _other: object) -> bool:
            return True

        def __le__(self, _other: object) -> bool:
            return True

        def __gt__(self, _other: object) -> bool:
            return True

    matter = tmp_path / "matter"
    generate_demo_rounds(matter)

    with pytest.raises(veqtor_docx.InspectError) as error:
        server.inspect_document(
            str(matter / "round-1-outgoing-draft.docx"),
            "browse",
            max_items=Bypass(1),
        )

    assert error.value.code == "invalid_limit"
    raw = records.read_records(str(matter), max_records=10, include_payload=True)
    inspection_records = [
        item for item in raw["records"] if item["tool_name"] == "inspect_document"
    ]
    assert [item["result"]["status"] for item in inspection_records] == ["error"]
    assert inspection_records[0]["result"]["error_code"] == "invalid_limit"


@pytest.mark.anyio
async def test_inspect_transport_rejects_sha256_with_terminal_lf_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter = tmp_path / "matter"
    generate_demo_rounds(matter)
    original = server.veqtor_docx.inspect_document

    def invalid_hash_result(*args, **kwargs):
        result = original(*args, **kwargs)
        result["file_sha256"] += "\n"
        return result

    monkeypatch.setattr(server.veqtor_docx, "inspect_document", invalid_hash_result)
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        response = await session.call_tool(
            "inspect_document",
            {
                "path": str(matter / "round-1-outgoing-draft.docx"),
                "mode": "browse",
            },
        )

    assert response.isError is True
    assert "output_contract_error" in _error_text(response)
    raw = records.read_records(str(matter), max_records=10, include_payload=True)
    inspection_records = [
        item for item in raw["records"] if item["tool_name"] == "inspect_document"
    ]
    assert [item["result"]["status"] for item in inspection_records] == ["error"]
    assert inspection_records[0]["result"] == {
        "status": "error",
        "error_code": "output_contract_error",
        "error": "output_contract_error: tool output failed contract validation",
    }
    assert inspection_records[0]["provenance"] == {"failure_phase": "output_validation"}


@pytest.mark.anyio
async def test_inspect_transport_rejects_schema_invalid_nested_output(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = server.veqtor_docx.inspect_document
    sentinel = "PRIVATE_INVALID_OUTPUT_SENTINEL"
    nested_sentinel = "PRIVATE_INVALID_NESTED_SENTINEL"

    def invalid_nested_result(*args, **kwargs):
        result = original(*args, **kwargs)
        result["search_scope"] = sentinel
        result["sections"] = [{"garbage": nested_sentinel}]
        return result

    monkeypatch.setattr(
        server.veqtor_docx,
        "inspect_document",
        invalid_nested_result,
    )
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        response = await session.call_tool(
            "inspect_document",
            {
                "path": str(demo_dir / "round-1-outgoing-draft.docx"),
                "mode": "outline",
            },
        )
        exported_response = await session.call_tool(
            "export_decision_record",
            {"workspace": str(demo_dir), "max_records": 10},
        )

    assert response.isError is True
    error_text = _error_text(response)
    assert "output_contract_error" in error_text
    assert "tool output failed contract validation" in error_text
    assert sentinel not in error_text
    assert nested_sentinel not in error_text
    assert "garbage" not in error_text

    raw = records.read_records(
        str(demo_dir),
        max_records=10,
        include_payload=True,
    )
    inspection_records = [
        item for item in raw["records"] if item["tool_name"] == "inspect_document"
    ]
    assert len(inspection_records) == 1
    assert inspection_records[0]["result"]["status"] == "error"
    assert inspection_records[0]["result"]["error_code"] == ("output_contract_error")
    assert inspection_records[0]["provenance"] == {"failure_phase": "output_validation"}
    assert sentinel not in json.dumps(inspection_records[0], ensure_ascii=False)
    assert nested_sentinel not in json.dumps(inspection_records[0], ensure_ascii=False)

    assert exported_response.isError is False
    exported = _payload(exported_response)
    compact_inspection = next(
        item for item in exported["records"] if item["tool_name"] == "inspect_document"
    )
    assert compact_inspection["result"]["status"] == "error"
    assert compact_inspection["result"]["error_code"] == "output_contract_error"
    assert sentinel not in json.dumps(exported, ensure_ascii=False)
    assert nested_sentinel not in json.dumps(exported, ensure_ascii=False)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "error"),
        ("error_code", "producer_spoof"),
        ("error", "PRIVATE PRODUCER ERROR TEXT"),
    ],
)
async def test_inspect_transport_rejects_producer_error_authority_without_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    matter = tmp_path / "matter"
    generate_demo_rounds(matter)
    source = matter / "round-1-outgoing-draft.docx"
    original = server.veqtor_docx.inspect_document
    private_text = "PRIVATE DOCUMENT TEXT /Users/private/matter.docx"

    def spoofed_result(*args, **kwargs):
        result = original(*args, **kwargs)
        result[field] = value
        result["future_top_level_extension"] = {"paragraphs": [{"text": private_text}]}
        return result

    monkeypatch.setattr(server.veqtor_docx, "inspect_document", spoofed_result)
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        response = await session.call_tool(
            "inspect_document",
            {"path": str(source), "mode": "browse", "max_items": 1},
        )

    assert response.isError is True
    error_text = _error_text(response)
    assert "output_contract_error" in error_text
    assert private_text not in error_text
    assert "producer_spoof" not in error_text
    assert "PRIVATE PRODUCER ERROR TEXT" not in error_text

    raw = records.read_records(str(matter), max_records=10, include_payload=True)
    inspection_records = [
        item for item in raw["records"] if item["tool_name"] == "inspect_document"
    ]
    assert len(inspection_records) == 1
    error_record = inspection_records[0]
    assert error_record["result"] == {
        "status": "error",
        "error_code": "output_contract_error",
        "error": "output_contract_error: tool output failed contract validation",
    }
    serialized = json.dumps(error_record, ensure_ascii=False)
    assert private_text not in serialized
    assert "future_top_level_extension" not in serialized


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mode", "mutation"),
    [
        ("browse", "returned_collection_count"),
        ("browse", "cursor_presence"),
        ("browse", "revision_total_partition"),
        ("browse", "excluded_subtree_sum"),
        ("browse", "container_snapshot"),
        ("browse", "browse_eligible_nonempty"),
        ("browse", "returned_revision_summary"),
        ("literal_search", "literal_nested_basis"),
        ("browse", "limit_requested_relation"),
        ("browse", "global_warning_without_revisions"),
        ("browse", "decoded_exceeds_tracked"),
        ("browse", "decoded_without_global_warning"),
        ("browse", "returned_text_individual_cap"),
        ("browse", "returned_text_aggregate_cap"),
        ("browse", "excluded_scope_prefix"),
        ("browse", "excluded_scope_unnormalized"),
        ("browse", "excluded_scope_unsorted"),
        ("browse", "excluded_scope_duplicate"),
    ],
)
async def test_inspect_transport_rejects_semantic_mismatch_before_success_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    mutation: str,
) -> None:
    matter = tmp_path / "matter"
    generate_demo_rounds(matter)
    source = matter / "round-1-outgoing-draft.docx"
    original = server.veqtor_docx.inspect_document

    def contradictory_result(*args, **kwargs):
        result = original(*args, **kwargs)
        _mutate_inspection_invariant(result, mutation)
        return result

    monkeypatch.setattr(
        server.veqtor_docx,
        "inspect_document",
        contradictory_result,
    )
    arguments: dict = {"path": str(source), "mode": mode}
    if mode == "literal_search":
        arguments.update(
            phrases=["Except as set out in Clause 14.3"],
            match_basis="normalized_literal",
        )
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        response = await session.call_tool("inspect_document", arguments)

    assert response.isError is True
    error_text = _error_text(response)
    assert "output_contract_error" in error_text
    assert "tool output failed contract validation" in error_text

    raw = records.read_records(str(matter), max_records=10, include_payload=True)
    inspection_records = [
        item for item in raw["records"] if item["tool_name"] == "inspect_document"
    ]
    assert [item["result"]["status"] for item in inspection_records] == ["error"]
    assert inspection_records[0]["result"]["error_code"] == "output_contract_error"
    assert inspection_records[0]["provenance"] == {"failure_phase": "output_validation"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("producer", _test_producer()),
        ("record_id", "dr_999"),
        ("record_status", "written"),
        ("record_error", "journal_busy"),
    ],
)
def test_core_result_cannot_override_server_owned_metadata(
    demo_dir: Path,
    field: str,
    value,
) -> None:
    result = veqtor_docx.list_rounds(str(demo_dir))
    result[field] = value

    with pytest.raises(server._OutputContractError):
        server._validated_success_result("list_rounds", result)


@pytest.mark.parametrize(
    "meta",
    [
        {"record_id": "dr_001", "record_status": "written"},
        {"record_id": None, "record_status": "disabled"},
        {
            "record_id": None,
            "record_status": "write_failed",
            "record_error": "journal_busy",
        },
    ],
)
def test_trusted_record_metadata_tuples_are_added_to_live_results(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    meta: dict,
) -> None:
    monkeypatch.setattr(records, "write_record", lambda **_kwargs: dict(meta))
    result = server._with_record(
        tool_name="list_rounds",
        workspace=demo_dir,
        input_payload={"folder": str(demo_dir)},
        result=veqtor_docx.list_rounds(str(demo_dir)),
        provenance={},
    )

    assert result["record_id"] == meta["record_id"]
    assert result["record_status"] == meta["record_status"]
    if meta["record_status"] == "write_failed":
        assert result["record_error"] == meta["record_error"]
    else:
        assert "record_error" not in result


@pytest.mark.parametrize(
    "meta",
    [
        {"record_id": None, "record_status": "written"},
        {
            "record_id": "dr_001",
            "record_status": "written",
            "record_error": "journal_busy",
        },
        {"record_id": "dr_001", "record_status": "disabled"},
        {
            "record_id": None,
            "record_status": "disabled",
            "record_error": "journal_busy",
        },
        {"record_id": None, "record_status": "write_failed"},
        {
            "record_id": "dr_001",
            "record_status": "write_failed",
            "record_error": "journal_busy",
        },
    ],
)
def test_trusted_record_metadata_rejects_every_other_tuple(meta: dict) -> None:
    with pytest.raises(server._OutputContractError):
        server._validated_record_metadata(meta)


@pytest.mark.parametrize(
    "meta",
    [
        {"record_id": "dr_١", "record_status": "written"},
        {"record_id": "dr_1 ", "record_status": "written"},
        {
            "record_id": None,
            "record_status": "write_failed",
            "record_error": "PRIVATE /path and document text",
        },
        {
            "record_id": None,
            "record_status": "write_failed",
            "record_error": " journal_busy",
        },
        {
            "record_id": None,
            "record_status": "write_failed",
            "record_error": "journal-busy",
        },
        {
            "record_id": None,
            "record_status": "write_failed",
            "record_error": "a" * 65,
        },
    ],
    ids=[
        "unicode_record_digits",
        "record_id_whitespace",
        "private_text",
        "record_error_whitespace",
        "record_error_punctuation",
        "record_error_oversize",
    ],
)
def test_trusted_record_metadata_rejects_noncanonical_values(meta: dict) -> None:
    with pytest.raises(server._OutputContractError):
        server._validated_record_metadata(meta)


@pytest.mark.parametrize(
    "meta",
    [
        {"record_id": "dr_1\n", "record_status": "written"},
        {"record_id": "dr_1\r", "record_status": "written"},
        {
            "record_id": None,
            "record_status": "write_failed",
            "record_error": "journal_busy\n",
        },
        {
            "record_id": None,
            "record_status": "write_failed",
            "record_error": "journal_busy\r",
        },
    ],
    ids=["record_id_lf", "record_id_cr", "record_error_lf", "record_error_cr"],
)
def test_record_metadata_domain_rejects_line_endings_in_every_layer(
    demo_dir: Path,
    meta: dict,
) -> None:
    candidate = _record_metadata_model_payload(demo_dir, meta)

    with pytest.raises(ValidationError):
        ListRoundsResult.model_validate(candidate)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(candidate, ListRoundsResult.contract_schema)
    with pytest.raises(server._OutputContractError):
        server._validated_record_metadata(meta)


@pytest.mark.parametrize(
    "meta",
    [
        {"record_id": "dr_001", "record_status": "written"},
        {"record_id": None, "record_status": "disabled"},
        {
            "record_id": None,
            "record_status": "write_failed",
            "record_error": "journal_busy",
        },
    ],
)
def test_record_metadata_domain_accepts_exact_tuples_in_every_layer(
    demo_dir: Path,
    meta: dict,
) -> None:
    candidate = _record_metadata_model_payload(demo_dir, meta)

    ListRoundsResult.model_validate(candidate)
    jsonschema.validate(candidate, ListRoundsResult.contract_schema)
    assert server._validated_record_metadata(meta) == meta


@pytest.mark.parametrize(
    "record_error",
    [
        "internal_error",
        "journal_busy",
        "journal_corrupt",
        "journal_oversize",
        "record_invalid",
        "sidecar_symlink",
        "workspace_unreadable",
    ],
)
def test_trusted_record_metadata_accepts_current_stable_error_codes(
    record_error: str,
) -> None:
    assert server._validated_record_metadata(
        {
            "record_id": None,
            "record_status": "write_failed",
            "record_error": record_error,
        }
    ) == {
        "record_id": None,
        "record_status": "write_failed",
        "record_error": record_error,
    }


@pytest.mark.anyio
async def test_invalid_trusted_record_error_is_sanitized_at_transport(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "PRIVATE /path and document text"
    monkeypatch.setattr(
        records,
        "write_record",
        lambda **_kwargs: {
            "record_id": None,
            "record_status": "write_failed",
            "record_error": sentinel,
        },
    )

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        response = await session.call_tool("list_rounds", {"folder": str(demo_dir)})

    assert response.isError is True
    error_text = _error_text(response)
    assert "output_contract_error" in error_text
    assert sentinel not in error_text
    assert str(demo_dir) not in error_text


@pytest.mark.anyio
async def test_invalid_style_outline_is_error_recorded_before_mcp_validation(
    tmp_path: Path,
) -> None:
    matter = tmp_path / "matter"
    generate_demo_rounds(matter)
    source = matter / "round-1-outgoing-draft.docx"

    def make_outline_negative(styles: bytes) -> bytes:
        marker = b'w:outlineLvl w:val="0"'
        assert marker in styles
        return styles.replace(marker, b'w:outlineLvl w:val="-1"', 1)

    _rewrite_docx_part(source, "word/styles.xml", make_outline_negative)
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        response = await session.call_tool(
            "inspect_document",
            {"path": str(source), "mode": "outline"},
        )

    assert response.isError is True
    assert "file_unextractable" in _error_text(response)
    journal = records.read_records(str(matter), max_records=10, include_payload=True)
    inspection_records = [
        item for item in journal["records"] if item["tool_name"] == "inspect_document"
    ]
    assert len(inspection_records) == 1
    assert set(inspection_records[0]["result"]) == {"status", "error_code", "error"}
    assert inspection_records[0]["result"]["status"] == "error"
    assert inspection_records[0]["result"]["error_code"] == "file_unextractable"


@pytest.mark.anyio
async def test_surrogate_literal_phrase_is_a_stable_mcp_error(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    generate_demo_rounds(matter)
    source = matter / "round-1-outgoing-draft.docx"

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        response = await session.call_tool(
            "inspect_document",
            {
                "path": str(source),
                "mode": "literal_search",
                "phrases": ["bad\ud800phrase"],
                "match_basis": "exact_literal",
            },
        )

    assert response.isError is True
    assert "invalid_phrase" in _error_text(response)
    journal = records.read_records(str(matter), max_records=10, include_payload=True)
    inspection_records = [
        item for item in journal["records"] if item["tool_name"] == "inspect_document"
    ]
    # The transport may reject a non-scalar input before the journaling layer;
    # it must never create a contradictory successful inspection record.
    assert all(item["result"]["status"] == "error" for item in inspection_records)


@pytest.mark.anyio
async def test_list_rounds_accepts_an_explicit_positional_manifest(
    demo_dir: Path,
) -> None:
    ordered_filenames = sorted(
        (path.name for path in demo_dir.glob("*.docx")),
        reverse=True,
    )
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        listed = await session.call_tool(
            "list_rounds",
            {
                "folder": str(demo_dir),
                "ordered_filenames": ordered_filenames,
            },
        )

    assert not listed.isError
    payload = _payload(listed)
    assert [item["filename"] for item in payload["rounds"]] == ordered_filenames
    assert payload["ordering_source"] == "explicit_filename_sequence_v1"
    assert payload["order_basis"] == {
        "kind": "caller_supplied_filename_sequence",
        "lineage_verified": False,
        "round_id_semantics": "position_only",
    }


@pytest.mark.anyio
async def test_verify_quote_rejects_anchor_fields_outside_closed_contract(
    demo_dir: Path,
) -> None:
    source = str(demo_dir / "round-2-counterparty-redline.docx")
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        extracted = _payload(
            await session.call_tool("extract_redlines", {"path": source})
        )
        anchor = {
            "change_unit_id": extracted["change_units"][0]["change_unit_id"],
            "file_sha256": extracted["file_sha256"],
            "unexpected": 1,
        }
        verified = await session.call_tool(
            "verify_quote",
            {"path": source, "anchor": anchor, "quote": "anything"},
        )

    assert verified.isError
    assert "invalid_anchor" in verified.content[0].text


@pytest.mark.anyio
async def test_inspect_document_and_paragraph_verify_are_hash_bound(
    demo_dir: Path,
) -> None:
    source = str(demo_dir / "round-1-outgoing-draft.docx")
    phrase = "Except as set out in Clause 14.3"
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        inspected = await session.call_tool(
            "inspect_document",
            {
                "path": source,
                "mode": "literal_search",
                "phrases": [phrase],
                "match_basis": "exact_literal",
                "max_items": 1,
            },
        )
        assert not inspected.isError
        inspection = _payload(inspected)
        paragraph_ref = inspection["matches"][0]["paragraph_ref"]
        verified = await session.call_tool(
            "verify_quote",
            {
                "path": source,
                "anchor": paragraph_ref,
                "quote": phrase,
            },
        )

    assert not verified.isError
    assert inspection["record_status"] == "written"
    assert inspection["producer"] == server._producer()
    assert inspection["search_scope"] == "word_document_xml_body_v1"
    assert inspection["coverage"]["complete_literal_match_count"] == 1
    verification = _payload(verified)
    assert verification["producer"] == server._producer()
    assert verification["verdict"] == "exact"
    assert verification["checked_anchor"] == paragraph_ref
    assert verification["matches"] == [
        {
            "path": source,
            "part_name": "word/document.xml",
            "revision_ids": [],
            "clause": "14.2 Limitation of Liability",
            "side": "paragraph_current",
            "paragraph_index": paragraph_ref["paragraph_index"],
            "paragraph_text_sha256": paragraph_ref["paragraph_text_sha256"],
            "reading_mode": "accepted_current_v1",
        }
    ]


@pytest.mark.anyio
async def test_verify_quote_accepts_policy_bound_change_unit_anchor(
    demo_dir: Path,
) -> None:
    source = str(demo_dir / "round-2-counterparty-redline.docx")
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        extracted = _payload(
            await session.call_tool("extract_redlines", {"path": source})
        )
        unit = next(
            item
            for item in extracted["change_units"]
            if item["new_text"] == "USD 50,000"
        )
        verified = await session.call_tool(
            "verify_quote",
            {
                "path": source,
                "anchor": unit["anchor"],
                "quote": "USD 50,000",
            },
        )

    assert not verified.isError
    payload = _payload(verified)
    assert payload["verdict"] == "exact"
    assert payload["checked_anchor"] == unit["anchor"]
    assert payload["matches"][0]["side"] == "new"


@pytest.mark.anyio
async def test_tools_are_exposed_and_callable(demo_dir: Path) -> None:
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        tools = await session.list_tools()
        runtime_tools = {tool.name for tool in tools.tools}
        documented_tools = set(
            re.findall(
                r"^## `([^`]+)`$",
                (Path(__file__).parents[1] / "API.md").read_text(encoding="utf-8"),
                flags=re.MULTILINE,
            )
        )
        assert runtime_tools == records.WRITABLE_TOOL_NAMES
        assert documented_tools == runtime_tools
        export_tool = next(
            tool for tool in tools.tools if tool.name == "export_decision_record"
        )
        list_tool = next(tool for tool in tools.tools if tool.name == "list_rounds")
        extract_tool = next(
            tool for tool in tools.tools if tool.name == "extract_redlines"
        )
        apply_tool = next(tool for tool in tools.tools if tool.name == "apply_edits")
        for tool_name in ("preflight_edits", "apply_edits"):
            tool = next(tool for tool in tools.tools if tool.name == tool_name)
            assert "author" not in tool.inputSchema["properties"]
        assert "include_payload" not in export_tool.inputSchema["properties"]
        assert "actual expanded-output limits" in list_tool.description
        assert "split the folder and retry" in list_tool.description
        assert "without returning a partial round list" in list_tool.description
        assert "revision_count_basis" in list_tool.description
        assert "w:ins" in list_tool.description
        assert "revision_count_basis" in extract_tool.description
        assert "revision_inventory.total_revision_elements" in (
            extract_tool.description
        )
        assert "does not accept, reject or remove" in apply_tool.description
        assert "not a tamper-evident audit log" in export_tool.description
        assert "not authentication or a hash chain" in export_tool.description
        assert "access_event.v1" in export_tool.description
        assert "first" in export_tool.description
        assert "access_count" in export_tool.description
        assert re.search(
            r"privacy-minimized\s+compact projection", export_tool.description
        )

        listed = await session.call_tool("list_rounds", {"folder": str(demo_dir)})
        assert not listed.isError
        listed_payload = _payload(listed)
        assert listed_payload["record_status"] == "written"
        assert listed_payload["producer"] == server._producer()
        assert listed_payload["record_id"].startswith("dr_")
        assert listed_payload["revision_count_basis"] == REVISION_COUNT_BASIS_V1
        rounds = listed_payload["rounds"]
        assert len(rounds) == 4

        extracted = await session.call_tool(
            "extract_redlines", {"path": rounds[1]["path"]}
        )
        assert not extracted.isError
        assert _payload(extracted)["revision_count_basis"] == (REVISION_COUNT_BASIS_V1)
        payload = _payload(extracted)
        assert payload["record_status"] == "written"
        assert payload["producer"] == server._producer()
        assert payload["file_sha256"] == rounds[1]["sha256"]
        anchors = {
            u["clause_anchor"]["label"]
            for u in payload["change_units"]
            if u["clause_anchor"]
        }
        assert "14.2" in anchors

        exported = await session.call_tool(
            "export_decision_record", {"workspace": str(demo_dir), "max_records": 2}
        )
        assert not exported.isError
        export_payload = _payload(exported)
        assert export_payload["record_status"] == "written"
        assert export_payload["producer"] == server._producer()
        assert export_payload["total_count"] >= 2
        assert len(export_payload["records"]) <= 2
        assert export_payload["returned_count"] == len(export_payload["records"])
        assert export_payload["payloads"] == "compact"
        assert export_payload["assurance"] == {
            "journal_model": "best_effort_local_provenance",
            "model_payload": "compact_only",
            "raw_journal_visibility": "private_local_only",
            "raw_journal_result": ("tool_specific_summary_not_verbatim_live_response"),
            "compact_projection": "privacy_minimized_view_not_raw_journal",
            "access_event_policy": (
                "raw_journal_only_excluded_from_default_compact_records"
            ),
            "tamper_evident": False,
            "hash_chain": False,
            "record_id_guarantee": "strictly_increasing_only",
            "producer_identity": "python_source_files_snapshot_only",
            "content_hashes": "recheckable_fingerprints_not_authentication",
            "round_trip_scope": (
                "ooxml_semantic_diff_outside_touched_anchors_not_docx_byte_identity"
            ),
        }
        assert export_payload["records_scope"] == "substantive_records_only"
        assert export_payload["total_count_scope"] == (
            "substantive_records_before_cursor"
        )
        assert export_payload["access_events_recorded_locally"] is True
        assert export_payload["access_events_in_records"] is False
        assert export_payload["access_count_scope"] == (
            "all_prior_access_events_before_current_export"
        )
        assert export_payload["access_count_includes_current_export"] is False
        assert export_payload["current_export_event"] == {
            "record_id": export_payload["record_id"],
            "record_type": "access_event.v1",
            "record_status": "written",
            "recorded_locally": True,
            "included_in_records": False,
            "included_in_total_count": False,
            "included_in_access_count": False,
        }


@pytest.mark.anyio
@pytest.mark.parametrize("stale_value", [True, 1, "true", "yes"])
async def test_stale_full_export_argument_never_returns_private_payload(
    tmp_path: Path,
    stale_value: object,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sentinel = "PRIVATE_STALE_FULL_EXPORT_SENTINEL_53"
    assert (
        records.write_record(
            workspace=matter,
            tool_name="verify_quote",
            input_payload={"quote": sentinel},
            result={"status": "ok", "verdict": "not_found"},
            provenance={},
        )["record_status"]
        == "written"
    )

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        exported = await session.call_tool(
            "export_decision_record",
            {
                "workspace": str(matter),
                "max_records": 1,
                "include_payload": stale_value,
            },
        )

    assert not exported.isError
    payload = _payload(exported)
    assert payload["payloads"] == "compact"
    assert sentinel not in json.dumps(payload, ensure_ascii=False)
    full = records.read_records(
        str(matter),
        max_records=10,
        include_access_events=True,
        include_payload=True,
    )
    access = next(
        record
        for record in full["records"]
        if record["tool_name"] == "export_decision_record"
    )
    assert "include_payload" not in access["input"]
    assert access["result"]["payloads"] == "compact"


@pytest.mark.anyio
async def test_invalid_export_cursor_does_not_create_history(tmp_path: Path) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        exported = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "before_record_id": "bad"},
        )

    assert exported.isError
    error_text = "\n".join(
        block.text for block in exported.content if hasattr(block, "text")
    )
    assert "invalid_before_record_id" in error_text
    assert not (matter / records.SIDECAR_DIR).exists()


@pytest.mark.anyio
async def test_export_workspace_discovery_states_are_path_safe_at_mcp_boundary(
    tmp_path: Path,
) -> None:
    async def export_error(workspace: Path) -> str:
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as session:
            result = await session.call_tool(
                "export_decision_record",
                {"workspace": str(workspace), "max_records": 10},
            )
        assert result.isError
        return _error_text(result)

    uninitialized = tmp_path / "PRIVATE_EMPTY_MATTER"
    uninitialized.mkdir()
    text = await export_error(uninitialized)
    assert "workspace_uninitialized" in text
    assert '"candidate_count":0' in text
    assert str(uninitialized) not in text
    assert not (uninitialized / records.SIDECAR_DIR).exists()

    wrong_parent = tmp_path / "PRIVATE_WRONG_PARENT"
    child = wrong_parent / "rounds"
    child.mkdir(parents=True)
    assert (
        records.write_record(
            workspace=child,
            tool_name="list_rounds",
            input_payload={},
            result={"status": "ok"},
            provenance={},
        )["record_status"]
        == "written"
    )
    text = await export_error(wrong_parent)
    assert "workspace_mismatch" in text
    assert '"relative_path":"rounds"' in text
    assert str(wrong_parent) not in text
    assert not (wrong_parent / records.SIDECAR_DIR).exists()

    ambiguous = tmp_path / "PRIVATE_AMBIGUOUS_PARENT"
    for name in ("PRIVATE_ALPHA", "PRIVATE_BETA"):
        candidate = ambiguous / name
        candidate.mkdir(parents=True)
        assert (
            records.write_record(
                workspace=candidate,
                tool_name="list_rounds",
                input_payload={},
                result={"status": "ok"},
                provenance={},
            )["record_status"]
            == "written"
        )
    text = await export_error(ambiguous)
    assert "workspace_ambiguous" in text
    assert '"candidate_count_at_least":2' in text
    assert "PRIVATE_ALPHA" not in text
    assert "PRIVATE_BETA" not in text
    assert str(ambiguous) not in text
    assert not (ambiguous / records.SIDECAR_DIR).exists()


@pytest.mark.anyio
async def test_apply_edits_tool_end_to_end(demo_dir: Path, tmp_path: Path) -> None:
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        source = str(demo_dir / "round-2-counterparty-redline.docx")
        extracted = _payload(
            await session.call_tool("extract_redlines", {"path": source})
        )
        cap = next(
            u
            for u in extracted["change_units"]
            if u["clause_anchor"] and u["clause_anchor"]["label"] == "14.2"
        )
        out = str(tmp_path / "counter.docx")
        edits = [
            {
                "anchor": {
                    "change_unit_id": cap["change_unit_id"],
                    "file_sha256": extracted["file_sha256"],
                },
                "delete_text": " in respect of all claims in aggregate.",
                "insert_text": " per claim.",
            }
        ]
        preflighted = await session.call_tool(
            "preflight_edits",
            {"source_path": source, "edits": edits},
        )
        assert not preflighted.isError
        preflight_payload = _payload(preflighted)
        assert preflight_payload["record_status"] == "written"
        assert preflight_payload["batch_applicable"] is True
        assert (
            preflight_payload["tracked_change_author"]
            == server._tracked_change_author()
        )
        assert (
            preflight_payload["producer"]["build"] == records.SOURCE_SNAPSHOT_IDENTITY
        )
        assert not Path(out).exists()

        applied = await session.call_tool(
            "apply_edits",
            {
                "source_path": source,
                "output_path": out,
                "edits": edits,
                "preflight_proof": preflight_payload["preflight_proof"],
            },
        )
        assert not applied.isError
        payload = _payload(applied)
        assert payload["record_status"] == "written"
        assert payload["source_sha256"] == extracted["file_sha256"]
        assert payload["tracked_change_author"] == server._tracked_change_author()
        assert payload["producer"]["build"] == records.SOURCE_SNAPSHOT_IDENTITY
        assert payload["output_sha256"]
        assert payload["round_trip_check"]["status"] == "passed"
        assert payload["output_sha256"] == preflight_payload["candidate_sha256"]
        assert payload["preflight_binding_status"] == "verified"
        assert (
            payload["preflight_candidate_sha256"]
            == preflight_payload["candidate_sha256"]
        )
        assert payload["candidate_output_sha256_match"] is True
        assert Path(out).exists()

        # Fail-closed surfaces as a tool error and writes nothing.
        broken = await session.call_tool(
            "apply_edits",
            {
                "source_path": source,
                "output_path": str(tmp_path / "never.docx"),
                "edits": [
                    {
                        "anchor": {
                            "change_unit_id": "cu_999",
                            "file_sha256": extracted["file_sha256"],
                        },
                        "delete_text": "x",
                        "insert_text": "y",
                    }
                ],
                "preflight_proof": preflight_payload["preflight_proof"],
            },
        )
        assert broken.isError
        assert not (tmp_path / "never.docx").exists()


@pytest.mark.anyio
async def test_invalid_xml_edit_text_is_recorded_as_preflight_refusal(
    demo_dir: Path,
) -> None:
    source = str(demo_dir / "round-2-counterparty-redline.docx")
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        extracted = _payload(
            await session.call_tool("extract_redlines", {"path": source})
        )
        cap = next(
            unit
            for unit in extracted["change_units"]
            if unit["clause_anchor"] and unit["clause_anchor"]["label"] == "14.2"
        )
        refused = await session.call_tool(
            "preflight_edits",
            {
                "source_path": source,
                "edits": [
                    {
                        "anchor": {
                            "change_unit_id": cap["change_unit_id"],
                            "file_sha256": extracted["file_sha256"],
                        },
                        "delete_text": " in respect of all claims in aggregate.",
                        "insert_text": "USD\u0001 250,000",
                    }
                ],
            },
        )

    assert not refused.isError
    payload = _payload(refused)
    assert payload["batch_applicable"] is False
    assert payload["failure_phase"] == "validation"
    assert payload["refusal_code"] == "invalid_edit"
    assert payload["record_status"] == "written"
    history = records.read_records(
        str(demo_dir),
        max_records=20,
        include_access_events=True,
        include_payload=True,
    )
    recorded = next(
        item
        for item in reversed(history["records"])
        if item["tool_name"] == "preflight_edits"
    )
    assert recorded["result"]["refusal_code"] == "invalid_edit"
    assert recorded["provenance"]["tracked_change_author"] == (
        server._tracked_change_author()
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "insert_text",
    [None, "", False, 0, 3.14, [], {}],
    ids=["null", "empty", "false", "zero", "float", "list", "object"],
)
async def test_mcp_reinstate_preflight_rejects_present_insert_text_without_output(
    demo_dir: Path,
    tmp_path: Path,
    insert_text,
) -> None:
    from veqtor_docx.synthetic import CARVEOUT_DROPPED

    source = str(demo_dir / "round-4-counterparty-reply.docx")
    output = tmp_path / "never.docx"
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        extracted = _payload(
            await session.call_tool("extract_redlines", {"path": source})
        )
        cap = next(
            unit
            for unit in extracted["change_units"]
            if unit["change_type"] == "replace"
        )
        edits = [
            {
                "anchor": {
                    "change_unit_id": cap["change_unit_id"],
                    "file_sha256": extracted["file_sha256"],
                },
                "reinstate_text": CARVEOUT_DROPPED,
                "insert_text": insert_text,
            }
        ]
        preflight = await session.call_tool(
            "preflight_edits", {"source_path": source, "edits": edits}
        )

    assert not preflight.isError
    payload = _payload(preflight)
    assert payload["batch_applicable"] is False
    assert payload["failure_phase"] == "validation"
    assert payload["refusal_code"] == "invalid_edit"
    assert set(payload["edits"][0]) == {
        "edit_index",
        "change_unit_id",
        "status",
        "operation",
        "match_count",
        "target_author",
        "target_revision_ids",
        "position_status",
        "refusal_code",
    }
    assert not output.exists()

    history = records.read_records(str(demo_dir), max_records=50, include_payload=True)[
        "records"
    ]
    preflight_record = next(
        record
        for record in reversed(history)
        if record["tool_name"] == "preflight_edits"
    )
    assert preflight_record["result"]["refusal_code"] == "invalid_edit"


def test_tracked_change_author_environment_is_validated(monkeypatch) -> None:
    server._tracked_change_author.cache_clear()
    monkeypatch.delenv(server.TRACKED_CHANGE_AUTHOR_ENV, raising=False)
    assert server._tracked_change_author_from_environment() == "Veqtor MCP"

    monkeypatch.setenv(server.TRACKED_CHANGE_AUTHOR_ENV, "  John Deer  ")
    assert server._tracked_change_author_from_environment() == "John Deer"

    for invalid in ("   ", "bad\nname", "x" * 256, "\udcff"):
        monkeypatch.setenv(server.TRACKED_CHANGE_AUTHOR_ENV, invalid)
        with pytest.raises(RuntimeError):
            server._tracked_change_author_from_environment()
    server._tracked_change_author.cache_clear()


def test_blank_author_keeps_version_and_makes_doctor_and_startup_diagnostic() -> None:
    env = os.environ.copy()
    env[server.TRACKED_CHANGE_AUTHOR_ENV] = "   "

    version = subprocess.run(
        [sys.executable, "-m", "veqtor_mcp.server", "--version"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    doctor = subprocess.run(
        [sys.executable, "-m", "veqtor_mcp.server", "doctor"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    startup = subprocess.run(
        [sys.executable, "-m", "veqtor_mcp.server"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert version.returncode == 0
    assert version.stdout.strip() == f"veqtor-mcp {__version__}"
    assert "Traceback" not in version.stderr
    assert doctor.returncode == 2
    diagnosis = json.loads(doctor.stdout)
    assert diagnosis["status"] == "error"
    assert diagnosis["tracked_change_author"] is None
    assert diagnosis["configuration_error"]["code"] == ("tracked_change_author_invalid")
    assert "Traceback" not in doctor.stderr
    assert startup.returncode == 2
    assert "configuration error:" in startup.stderr
    assert "Traceback" not in startup.stderr


def test_cli_version_and_doctor(monkeypatch, capsys) -> None:
    monkeypatch.setattr(server.sys, "argv", ["veqtor-mcp", "--version"])
    server.main()
    assert capsys.readouterr().out.strip() == f"veqtor-mcp {__version__}"

    monkeypatch.setattr(server.sys, "argv", ["veqtor-mcp", "doctor"])
    server.main()
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["name"] == "veqtor-mcp"
    assert doctor["version"] == __version__
    assert doctor["build"] == records.SOURCE_SNAPSHOT_IDENTITY
    assert doctor["tracked_change_author"] == server._tracked_change_author()
    assert doctor["configuration_error"] is None
    assert doctor["status"] == "ok"


@pytest.mark.anyio
async def test_tool_errors_are_reported_not_raised(demo_dir: Path) -> None:
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        broken = await session.call_tool(
            "list_rounds", {"folder": str(demo_dir / "nope")}
        )
        assert broken.isError


@pytest.mark.anyio
async def test_round_scan_budget_overrun_is_a_stable_protocol_error(
    demo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rounds_module, "MAX_ROUND_TOTAL_EXPANDED_BYTES", 0)

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        result = await session.call_tool(
            "list_rounds",
            {"folder": str(demo_dir)},
        )

    text = _error_text(result)
    assert result.isError
    assert "resource_limit_exceeded" in text
    assert "aggregate expanded-output limit" in text
    assert "split the folder and retry" in text
    assert '"rounds"' not in text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool_name", "core_owner", "arguments"),
    [
        ("list_rounds", veqtor_docx, {"folder": None}),
        ("extract_redlines", veqtor_docx, {"path": None}),
        ("inspect_document", veqtor_docx, {"path": None, "mode": "browse"}),
        (
            "verify_quote",
            veqtor_docx,
            {
                "path": None,
                "anchor": {"change_unit_id": "cu_001", "file_sha256": "0" * 64},
                "quote": "quote",
            },
        ),
        ("preflight_edits", veqtor_docx, {"source_path": None, "edits": []}),
        (
            "apply_edits",
            veqtor_docx,
            {"source_path": None, "output_path": None, "edits": []},
        ),
        (
            "export_decision_record",
            records,
            {"workspace": None, "max_records": 10},
        ),
    ],
)
async def test_every_unexpected_tool_failure_is_sanitized_and_journaled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    core_owner,
    arguments: dict,
) -> None:
    matter = tmp_path / tool_name
    veqtor_docx.generate_demo_rounds(matter)
    source = str(matter / "round-2-counterparty-redline.docx")
    resolved_arguments = {
        key: (
            str(matter)
            if key in {"folder", "workspace"} and value is None
            else source
            if key in {"path", "source_path"} and value is None
            else str(tmp_path / "never.docx")
            if key == "output_path" and value is None
            else value
        )
        for key, value in arguments.items()
    }
    if tool_name == "apply_edits":
        resolved_arguments["preflight_proof"] = _dummy_preflight_proof()
    sentinel = f"PRIVATE_IMPLEMENTATION_SENTINEL_{tool_name}"
    core_name = (
        tool_name if core_owner is veqtor_docx else "export_records_with_access_event"
    )
    original = getattr(core_owner, core_name)

    def explode(*_args, **_kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(
        core_owner,
        core_name,
        explode,
    )
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        result = await session.call_tool(tool_name, resolved_arguments)
    monkeypatch.setattr(
        core_owner,
        core_name,
        original,
    )

    text = _error_text(result)
    assert result.isError
    assert "internal_error" in text
    assert sentinel not in text
    raw = records.read_records(
        str(matter),
        max_records=20,
        include_access_events=True,
        include_payload=True,
    )
    failure = next(
        record
        for record in reversed(raw["records"])
        if record["tool_name"] == tool_name
        and record["result"].get("error_code") == "internal_error"
    )
    assert set(failure["result"]) == {"status", "error_code", "error"}
    assert failure["result"]["error"] == "unexpected internal failure"
    assert sentinel not in json.dumps(failure, ensure_ascii=False)


@pytest.mark.anyio
async def test_post_core_failure_uses_the_same_sanitized_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = tmp_path / "matter"
    veqtor_docx.generate_demo_rounds(matter)
    sentinel = "PRIVATE_POST_CORE_SENTINEL"

    # Provenance construction is intentionally inside the same boundary as the
    # core call and journal publication.
    def explode(_result):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(server, "_list_rounds_provenance", explode)
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        result = await session.call_tool("list_rounds", {"folder": str(matter)})

    text = _error_text(result)
    assert result.isError
    assert "internal_error" in text
    assert sentinel not in text
    raw = records.read_records(str(matter), max_records=10, include_payload=True)
    assert raw["records"][-1]["result"] == {
        "status": "error",
        "error_code": "internal_error",
        "error": "unexpected internal failure",
    }


@pytest.mark.anyio
async def test_export_permission_failure_never_exposes_the_workspace_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matter = tmp_path / "private-client-matter"
    matter.mkdir()
    sentinel = f"permission denied at {matter}"

    def deny_open(*_args, **_kwargs):
        raise PermissionError(sentinel)

    monkeypatch.setattr(records, "_open_workspace_fd", deny_open)
    with pytest.raises(records.DecisionRecordError) as error:
        records.read_records(str(matter), max_records=10)
    assert error.value.code == "workspace_unreadable"
    assert str(error.value) == "workspace_unreadable: workspace cannot be read"
    assert sentinel not in str(error.value)

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        result = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "max_records": 10},
        )
    text = _error_text(result)
    assert result.isError
    assert "workspace_unreadable" in text
    assert sentinel not in text
    assert str(matter) not in text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("sidecar_kind", "expected_code"),
    [
        ("symlink", "sidecar_symlink"),
        ("file", "sidecar_not_directory"),
    ],
)
async def test_decision_record_refusals_are_path_free_at_mcp_boundary(
    tmp_path: Path,
    sidecar_kind: str,
    expected_code: str,
) -> None:
    matter = tmp_path / "PRIVATE_CLIENT_MATTER"
    matter.mkdir()
    sidecar = matter / ".veqtor"
    if sidecar_kind == "symlink":
        target = tmp_path / "PRIVATE_SIDECAR_TARGET"
        target.mkdir()
        sidecar.symlink_to(target, target_is_directory=True)
    else:
        sidecar.write_text("private", encoding="utf-8")

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        result = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "max_records": 10},
        )

    text = _error_text(result)
    assert result.isError
    assert expected_code in text
    assert "decision-record operation refused" in text
    assert str(matter) not in text
    assert "PRIVATE" not in text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("list_rounds", {"folder": "bad\x00path"}),
        ("extract_redlines", {"path": "bad\x00path"}),
        (
            "verify_quote",
            {
                "path": "bad\x00path",
                "anchor": {"change_unit_id": "cu_001", "file_sha256": "0" * 64},
                "quote": "quote",
            },
        ),
        ("preflight_edits", {"source_path": "bad\x00path", "edits": []}),
        (
            "apply_edits",
            {
                "source_path": "bad\x00path",
                "output_path": "never.docx",
                "edits": [],
                "preflight_proof": _dummy_preflight_proof(),
            },
        ),
        ("export_decision_record", {"workspace": "bad\x00path"}),
    ],
)
async def test_unresolvable_mcp_paths_are_stable_tool_errors(
    tool_name: str, arguments: dict
) -> None:
    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        result = await session.call_tool(tool_name, arguments)

    text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
    assert result.isError
    assert "invalid_path" in text
    assert "embedded null byte" not in text
    assert "lstat" not in text


@pytest.mark.anyio
async def test_surrogate_corrupt_journal_is_a_controlled_tool_error(
    tmp_path: Path,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    meta = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={"folder": str(matter)},
        result={"status": "ok"},
        provenance={},
    )
    assert meta["record_status"] == "written"
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    record = json.loads(journal.read_text(encoding="utf-8"))
    record["producer"]["build"] = "\udcff"
    journal.write_text(json.dumps(record) + "\n", encoding="utf-8")

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        exported = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "max_records": 10},
        )

    error_text = "\n".join(
        block.text for block in exported.content if hasattr(block, "text")
    )
    assert exported.isError
    assert "journal_corrupt" in error_text
    assert "decision-record operation refused" in error_text
    assert str(journal) not in error_text
    assert "surrogates not allowed" not in error_text


@pytest.mark.anyio
async def test_unterminated_journal_is_a_controlled_tool_error(
    tmp_path: Path,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    meta = records.write_record(
        workspace=matter,
        tool_name="list_rounds",
        input_payload={"folder": str(matter)},
        result={"status": "ok"},
        provenance={},
    )
    assert meta["record_status"] == "written"
    journal = matter / records.SIDECAR_DIR / records.JOURNAL_NAME
    journal.write_bytes(journal.read_bytes().removesuffix(b"\n"))

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        exported = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "max_records": 10},
        )

    error_text = "\n".join(
        block.text for block in exported.content if hasattr(block, "text")
    )
    assert exported.isError
    assert "journal_corrupt" in error_text
    assert "decision-record operation refused" in error_text
    assert str(journal) not in error_text
    assert "Extra data" not in error_text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("payload", "reasons", "raw_detail"),
    [
        (
            b"[" * 10_000 + b"0" + b"]" * 10_000,
            ("JSON decoder rejected input", "maximum depth"),
            "maximum recursion depth",
        ),
        (
            b"1" * 5_000,
            ("JSON integer exceeds",),
            "int_max_str_digits",
        ),
    ],
    ids=["decoder_recursion", "oversized_integer"],
)
async def test_decoder_limit_failures_are_controlled_tool_errors(
    tmp_path: Path,
    payload: bytes,
    reasons: tuple[str, ...],
    raw_detail: str,
) -> None:
    matter = tmp_path / "matter"
    matter.mkdir()
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    journal = sidecar / records.JOURNAL_NAME
    journal.write_bytes(payload + b"\n")

    async with create_connected_server_and_client_session(mcp._mcp_server) as session:
        exported = await session.call_tool(
            "export_decision_record",
            {"workspace": str(matter), "max_records": 10},
        )

    error_text = "\n".join(
        block.text for block in exported.content if hasattr(block, "text")
    )
    assert exported.isError
    assert "journal_corrupt" in error_text
    assert "decision-record operation refused" in error_text
    assert not any(reason in error_text for reason in reasons)
    assert raw_detail not in error_text
