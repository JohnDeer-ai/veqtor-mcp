# SPDX-License-Identifier: Apache-2.0
"""Public Stage 3C v0.4 integration and privacy-boundary acceptance."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import jsonschema
import pytest

import veqtor_docx
from veqtor_docx.inspect import _load_snapshot_from_payload, _paragraph_ref
from veqtor_docx.synthetic import CAP_R2, CAP_R3, CAP_R4
from veqtor_mcp import _history_io, records, server
from veqtor_mcp.contracts import ParagraphHistoryResult, VerifyQuoteResult


def _copy_demo(demo_dir: Path, target: Path) -> Path:
    target.mkdir()
    for source in demo_dir.glob("*.docx"):
        shutil.copyfile(source, target / source.name)
    return target


def _files(matter: Path) -> list[Path]:
    return sorted(
        matter.glob("*.docx"),
        key=lambda path: (path.name.casefold(), path.name),
    )


def _paragraph_seed(path: Path, text: str = CAP_R4) -> dict:
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


def _without_record_metadata(result: dict) -> dict:
    return {
        key: value
        for key, value in result.items()
        if key not in {"record_id", "record_status", "record_error"}
    }


def test_public_history_traces_liability_and_preserves_metadata_assurance(
    demo_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter = _copy_demo(demo_dir, tmp_path / "matter")
    seed = _paragraph_seed(_files(matter)[-1])
    monkeypatch.setenv(records.DISABLE_ENV, "1")

    result = server.trace_paragraph_history(
        str(matter),
        seed,
        _lexicographic_order(),
        max_items=100,
    )

    jsonschema.validate(result, ParagraphHistoryResult.contract_schema)
    assert result["record_status"] == "disabled"
    assert result["schema_version"] == "paragraph_history.v1"
    assert [
        observation["resolution"]["reason"]
        for observation in result["observations"][1:]
    ] == ["rejected_projection_unique"] * 3
    selected = result["observations"][0]["selected_paragraph"]
    deletion = next(
        unit for unit in selected["change_units"] if unit["change_type"] == "delete"
    )
    assert deletion["author"] == "53"
    assert "willful misconduct" in deletion["old_text"]
    assert selected["metadata_assurance"]["authorship_verified"] is False
    assert selected["metadata_assurance"]["time_verified"] is False
    assert {
        unit["reference"]["paragraph_index"]
        for unit in selected["change_units"]
    } == {selected["paragraph_ref"]["paragraph_index"]}
    assert all("path" not in unit["reference"] for unit in selected["change_units"])


def test_history_cursor_survives_own_records_and_page_size_changes(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    matter = _copy_demo(demo_dir, tmp_path / "matter")
    seed = _paragraph_seed(_files(matter)[-1])
    order = _lexicographic_order()

    first = server.trace_paragraph_history(
        str(matter), seed, order, max_items=1
    )
    second = server.trace_paragraph_history(
        str(matter), seed, order, cursor=first["next_cursor"], max_items=2
    )
    third = server.trace_paragraph_history(
        str(matter), seed, order, cursor=second["next_cursor"], max_items=100
    )

    observations = (
        first["observations"] + second["observations"] + third["observations"]
    )
    assert [item["position"] for item in observations] == [3, 2, 1, 0]
    assert len({item["observation_id"] for item in observations}) == 4
    assert third["next_cursor"] is None
    assert {item["record_status"] for item in (first, second, third)} == {"written"}
    full = records.read_records(str(matter), max_records=10, include_payload=True)
    history_records = [
        record
        for record in full["records"]
        if record["record_type"] == "paragraph_history.v1"
    ]
    assert [record["result"]["observations_summary"]["count"] for record in history_records] == [
        1,
        2,
        1,
    ]


def test_history_pre_result_failure_creates_no_record_or_sidecar(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    matter = _copy_demo(demo_dir, tmp_path / "matter")
    seed = _paragraph_seed(_files(matter)[-1])
    seed["paragraph_ref"] = {
        **seed["paragraph_ref"],
        "paragraph_text_sha256": "0" * 64,
    }

    with pytest.raises(_history_io.HistoryIOError) as refused:
        server.trace_paragraph_history(str(matter), seed, _lexicographic_order())

    assert refused.value.code == "reference_mismatch"
    assert not (matter / records.SIDECAR_DIR).exists()


def test_history_post_result_publication_failure_preserves_valid_facts(
    demo_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter = _copy_demo(demo_dir, tmp_path / "matter")
    seed = _paragraph_seed(_files(matter)[-1])
    monkeypatch.setattr(
        records,
        "write_record",
        lambda **_kwargs: {
            "record_id": None,
            "record_status": "write_failed",
            "record_error": "journal_busy",
        },
    )

    result = server.trace_paragraph_history(
        str(matter), seed, _lexicographic_order(), max_items=100
    )

    jsonschema.validate(result, ParagraphHistoryResult.contract_schema)
    assert result["record_id"] is None
    assert result["record_status"] == "write_failed"
    assert result["record_error"] == "journal_busy"
    assert len(result["observations"]) == 4


def test_history_unknown_post_result_error_is_normalized(
    demo_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter = _copy_demo(demo_dir, tmp_path / "matter")
    seed = _paragraph_seed(_files(matter)[-1])
    monkeypatch.setattr(
        records,
        "write_record",
        lambda **_kwargs: {
            "record_id": None,
            "record_status": "write_failed",
            "record_error": "made_up_but_valid",
        },
    )

    result = server.trace_paragraph_history(
        str(matter), seed, _lexicographic_order(), max_items=100
    )

    jsonschema.validate(result, ParagraphHistoryResult.contract_schema)
    assert result["record_id"] is None
    assert result["record_status"] == "write_failed"
    assert result["record_error"] == "internal_error"
    assert len(result["observations"]) == 4


def test_history_raw_and_compact_records_are_minimized_and_hash_bound(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    matter = _copy_demo(demo_dir, tmp_path / "matter")
    seed = _paragraph_seed(_files(matter)[-1])

    live = server.trace_paragraph_history(
        str(matter), seed, _lexicographic_order(), max_items=100
    )
    full = records.read_records(str(matter), max_records=10, include_payload=True)
    compact = records.read_records(str(matter), max_records=10)
    raw_record = next(
        record for record in full["records"] if record["tool_name"] == "trace_paragraph_history"
    )
    compact_record = next(
        record
        for record in compact["records"]
        if record["tool_name"] == "trace_paragraph_history"
    )

    assert set(raw_record["result"]) == {
        "status",
        "seed",
        "ordering_source",
        "result_order",
        "filename_manifest_sha256",
        "snapshot",
        "coverage",
        "limits",
        "next_cursor_sha256",
        "observations_summary",
    }
    assert compact_record["result"] == raw_record["result"]
    assert set(compact_record["provenance"]) == {
        "filesystem_snapshot_sha256",
        "full_result_set_sha256",
        "projection_policy_sha256",
        "current_reading_mode",
        "rejected_reading_mode",
        "container_policy",
        "result_order",
    }
    assert raw_record["tool_result_sha256"] == records._stable_digest(
        _without_record_metadata(live)
    )
    encoded_result = json.dumps(raw_record["result"], ensure_ascii=False)
    encoded_compact = json.dumps(compact_record, ensure_ascii=False)
    for private in (str(matter), CAP_R4, CAP_R3, "willful misconduct", '"53"'):
        assert private not in encoded_result
        assert private not in encoded_compact


def test_public_verify_quote_v2_covers_current_rejected_and_change_unit(
    demo_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter = _copy_demo(demo_dir, tmp_path / "matter")
    seed = _paragraph_seed(_files(matter)[-1])
    path = seed["path"]
    anchor = seed["paragraph_ref"]
    monkeypatch.setenv(records.DISABLE_ENV, "1")

    default = server.verify_quote(path, anchor, CAP_R4)
    explicit = server.verify_quote(path, anchor, CAP_R4, "accepted_current_v1")
    rejected = server.verify_quote(
        path,
        anchor,
        CAP_R3,
        "pending_text_revisions_rejected_v1",
    )

    assert default == explicit
    assert default["schema_version"] == "verification_result.v2"
    assert default["checked_projection"]["mode"] == "accepted_current_v1"
    assert rejected["verdict"] == "exact"
    assert rejected["checked_projection"]["mode"] == (
        "pending_text_revisions_rejected_v1"
    )
    assert rejected["matches"][0]["side"] == "paragraph_rejected_pending"
    jsonschema.validate(rejected, VerifyQuoteResult.contract_schema)

    change_path = matter / "round-2-counterparty-redline.docx"
    extraction = veqtor_docx.extract_redlines(str(change_path))
    unit = next(
        item
        for item in extraction["change_units"]
        if (item.get("clause_anchor") or {}).get("label") == "14.2"
    )
    change = server.verify_quote(str(change_path), unit["anchor"], CAP_R2)
    assert change["schema_version"] == "verification_result.v2"
    assert change["checked_projection"] is None
    with pytest.raises(veqtor_docx.VerifyError) as refused:
        server.verify_quote(
            str(change_path),
            unit["anchor"],
            CAP_R2,
            "accepted_current_v1",
        )
    assert refused.value.code == "invalid_projection_selector"


def test_paragraph_verification_v2_compact_export_is_path_and_text_free(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    matter = _copy_demo(demo_dir, tmp_path / "matter")
    seed = _paragraph_seed(_files(matter)[-1])

    live = server.verify_quote(
        seed["path"],
        seed["paragraph_ref"],
        CAP_R3,
        "pending_text_revisions_rejected_v1",
    )
    raw = records.read_records(
        str(matter), max_records=10, include_payload=True
    )["records"][-1]
    compact = records.read_records(str(matter), max_records=10)["records"][-1]

    assert raw["record_type"] == "verification.v2"
    assert raw["result"]["matches"][0]["path"] == seed["path"]
    assert raw["tool_result_sha256"] == records._stable_digest(
        {"status": records.RESULT_STATUS_OK, **_without_record_metadata(live)}
    )
    assert compact["result"]["checked_projection"]["mode"] == (
        "pending_text_revisions_rejected_v1"
    )
    assert set(compact["result"]["matches"]["sample"][0]) == {
        "side",
        "part_name",
        "paragraph_index",
        "paragraph_text_sha256",
        "reading_mode",
        "projection_mode",
        "projection_text_sha256",
    }
    encoded = json.dumps(compact, ensure_ascii=False)
    for private in (
        str(matter),
        Path(seed["path"]).name,
        CAP_R3,
        "14.2 Limitation of Liability",
    ):
        assert private not in encoded


def test_v1_verification_frames_remain_readable_after_v2_append(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    matter = _copy_demo(demo_dir, tmp_path / "matter")
    sidecar = matter / records.SIDECAR_DIR
    sidecar.mkdir()
    golden = Path(__file__).parent / "data" / "decision-records-v1-golden.jsonl"
    (sidecar / records.JOURNAL_NAME).write_bytes(golden.read_bytes())
    seed = _paragraph_seed(_files(matter)[-1])

    result = server.verify_quote(seed["path"], seed["paragraph_ref"], CAP_R4)
    full = records.read_records(str(matter), max_records=records.MAX_MAX_RECORDS, include_payload=True)

    assert result["record_status"] == "written"
    verify_types = [
        record["record_type"]
        for record in full["records"]
        if record["tool_name"] == "verify_quote"
    ]
    assert "verification.v1" in verify_types
    assert "verification.v2" in verify_types
