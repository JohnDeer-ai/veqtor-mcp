# SPDX-License-Identifier: Apache-2.0
"""Dark verification-v2 foundation without a public MCP contract cutover."""

from __future__ import annotations

import hashlib
import io
import inspect
import shutil
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest
import jsonschema
from lxml import etree

from veqtor_docx import VerifyError, extract_redlines, verify_quote
from veqtor_docx._ooxml import w
from veqtor_docx.inspect import _load_snapshot_from_payload, _paragraph_ref
from veqtor_docx.synthetic import CAP_R2, CAP_R3, CAP_R4
from veqtor_mcp import records, server
from veqtor_mcp._verification_v2 import (
    ACCEPTED_CURRENT_MODE,
    PENDING_REJECTED_MODE,
    VERIFICATION_RESULT_SCHEMA_VERSION,
    VERIFICATION_RESULT_V2_OPERATION_SCHEMA,
    build_verification_result_v2,
    validate_verification_result_v2,
)
from veqtor_mcp.contracts import MCP_CONTRACT_SCHEMA_VERSION, VerifyQuoteResult


_RESULT_KEYS = {
    "schema_version",
    "verdict",
    "exact",
    "checked_anchor",
    "checked_projection",
    "matches",
    "diff",
}
_PARAGRAPH_MATCH_KEYS = {
    "path",
    "part_name",
    "revision_ids",
    "clause",
    "side",
    "paragraph_index",
    "paragraph_text_sha256",
    "reading_mode",
    "projection_mode",
    "projection_text_sha256",
}


def _paragraph_fixture(path: Path, phrase: str) -> tuple[bytes, str, dict, str]:
    payload = path.read_bytes()
    label = str(path.resolve())
    snapshot = _load_snapshot_from_payload(payload, path=label)
    paragraph = next(item for item in snapshot.paragraphs if phrase in item.text)
    return payload, label, _paragraph_ref(snapshot, paragraph), paragraph.text


def _change_unit_fixture(path: Path) -> tuple[bytes, str, dict]:
    extraction = extract_redlines(str(path))
    unit = next(
        item
        for item in extraction["change_units"]
        if (item.get("clause_anchor") or {}).get("label") == "14.2"
    )
    return path.read_bytes(), str(path.resolve()), dict(unit["anchor"])


def _with_stray_deleted_text(source: Path, target: Path) -> None:
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        members = {info.filename: archive.read(info) for info in infos}
    document = etree.fromstring(members["word/document.xml"])
    paragraph = next(
        item
        for item in document.iter(w("p"))
        if CAP_R4
        in "".join(
            node.text or ""
            for node in item.iter(w("t"))
        )
    )
    run = etree.SubElement(paragraph, w("r"))
    etree.SubElement(run, w("delText")).text = "stray deleted text"
    members["word/document.xml"] = etree.tostring(
        document,
        encoding="utf-8",
        xml_declaration=True,
    )
    with zipfile.ZipFile(target, "w") as archive:
        for info in infos:
            archive.writestr(info, members[info.filename])


def _with_replaced_paragraph_text(
    source: Path,
    target: Path,
    *,
    phrase: str,
    replacement: str,
) -> None:
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        members = {info.filename: archive.read(info) for info in infos}
    document = etree.fromstring(members["word/document.xml"])
    paragraph = next(
        item
        for item in document.iter(w("p"))
        if phrase in "".join(node.text or "" for node in item.iter(w("t")))
    )
    text_nodes = list(paragraph.iter(w("t")))
    text_nodes[0].text = replacement
    for node in text_nodes[1:]:
        node.text = None
    members["word/document.xml"] = etree.tostring(
        document,
        encoding="utf-8",
        xml_declaration=True,
    )
    with zipfile.ZipFile(target, "w") as archive:
        for info in infos:
            archive.writestr(info, members[info.filename])


def test_default_and_explicit_current_projection_are_identical(demo_dir: Path) -> None:
    payload, path, anchor, paragraph_text = _paragraph_fixture(
        demo_dir / "round-4-counterparty-reply.docx", CAP_R4
    )

    default = build_verification_result_v2(
        payload,
        path=path,
        anchor=anchor,
        quote=CAP_R4,
    )
    explicit = build_verification_result_v2(
        payload,
        path=path,
        anchor=anchor,
        quote=CAP_R4,
        paragraph_projection=ACCEPTED_CURRENT_MODE,
    )

    assert default == explicit
    assert set(default) == _RESULT_KEYS
    assert default["schema_version"] == VERIFICATION_RESULT_SCHEMA_VERSION
    assert default["verdict"] == "exact"
    assert default["exact"] is True
    assert default["diff"] == []
    assert default["checked_projection"] == {
        "schema_version": "verified_paragraph_projection.v1",
        "mode": ACCEPTED_CURRENT_MODE,
        "projection_status": "complete",
        "anchor_reading_mode": ACCEPTED_CURRENT_MODE,
        "anchor_paragraph_text_sha256": anchor["paragraph_text_sha256"],
        "projection_text_sha256": anchor["paragraph_text_sha256"],
        "text_length": len(paragraph_text),
    }
    (match,) = default["matches"]
    assert set(match) == _PARAGRAPH_MATCH_KEYS
    assert match["side"] == "paragraph_current"
    assert match["projection_mode"] == ACCEPTED_CURRENT_MODE
    assert match["projection_text_sha256"] == anchor["paragraph_text_sha256"]
    jsonschema.validate(default, VERIFICATION_RESULT_V2_OPERATION_SCHEMA)


def test_rejected_projection_verifies_its_own_text_and_hash(demo_dir: Path) -> None:
    payload, path, anchor, _paragraph_text = _paragraph_fixture(
        demo_dir / "round-4-counterparty-reply.docx", CAP_R4
    )

    rejected = build_verification_result_v2(
        payload,
        path=path,
        anchor=anchor,
        quote=CAP_R3,
        paragraph_projection=PENDING_REJECTED_MODE,
    )

    assert rejected["verdict"] == "exact"
    projection = rejected["checked_projection"]
    assert projection["mode"] == PENDING_REJECTED_MODE
    assert projection["projection_text_sha256"] != anchor["paragraph_text_sha256"]
    assert len(projection["projection_text_sha256"]) == 64
    assert projection["text_length"] >= len(CAP_R3)
    (match,) = rejected["matches"]
    assert match["side"] == "paragraph_rejected_pending"
    assert match["paragraph_text_sha256"] == anchor["paragraph_text_sha256"]
    assert match["projection_text_sha256"] == projection["projection_text_sha256"]
    jsonschema.validate(rejected, VERIFICATION_RESULT_V2_OPERATION_SCHEMA)

    absent = build_verification_result_v2(
        payload,
        path=path,
        anchor=anchor,
        quote=CAP_R4,
        paragraph_projection=PENDING_REJECTED_MODE,
    )
    assert absent["verdict"] == "not_found"
    assert absent["matches"] == []


def test_change_unit_anchors_reuse_v1_match_and_reject_selector_before_decode(
    demo_dir: Path,
) -> None:
    path = demo_dir / "round-2-counterparty-redline.docx"
    payload, label, v2_anchor = _change_unit_fixture(path)
    legacy_anchor = {
        "change_unit_id": v2_anchor["change_unit_id"],
        "file_sha256": v2_anchor["file_sha256"],
    }

    for anchor in (legacy_anchor, v2_anchor):
        v1 = verify_quote(label, anchor, CAP_R2)
        v2 = build_verification_result_v2(
            payload,
            path=label,
            anchor=anchor,
            quote=CAP_R2,
        )

        assert v2["schema_version"] == VERIFICATION_RESULT_SCHEMA_VERSION
        assert v2["checked_projection"] is None
        assert v2["verdict"] == v1["verdict"]
        assert v2["exact"] == v1["exact"]
        assert v2["checked_anchor"] == v1["checked_anchor"]
        assert v2["matches"] == v1["matches"]
        assert v2["diff"] == v1["diff"]
        jsonschema.validate(v2, VERIFICATION_RESULT_V2_OPERATION_SCHEMA)

    with pytest.raises(VerifyError) as refused:
        build_verification_result_v2(
            b"not a DOCX",
            path=label,
            anchor=v2_anchor,
            quote=CAP_R2,
            paragraph_projection=ACCEPTED_CURRENT_MODE,
        )
    assert refused.value.code == "invalid_projection_selector"


@pytest.mark.parametrize(
    ("mode", "quote"),
    [
        (ACCEPTED_CURRENT_MODE, CAP_R4.replace("fees paid", "fees  paid")),
        (PENDING_REJECTED_MODE, CAP_R3.replace("fifty percent", "fifty  percent")),
    ],
)
def test_paragraph_projection_preserves_normalized_verdict(
    demo_dir: Path,
    mode: str,
    quote: str,
) -> None:
    payload, path, anchor, _text = _paragraph_fixture(
        demo_dir / "round-4-counterparty-reply.docx", CAP_R4
    )

    result = build_verification_result_v2(
        payload,
        path=path,
        anchor=anchor,
        quote=quote,
        paragraph_projection=mode,
    )

    assert result["verdict"] == "normalized"
    assert result["exact"] is False
    assert len(result["matches"]) == 1
    assert result["diff"] == [
        "quote matches after collapsing whitespace and normalizing "
        "typographic quotes/dashes"
    ]


@pytest.mark.parametrize(
    ("field", "error_code"),
    [
        ("file_sha256", "file_sha256_mismatch"),
        ("paragraph_text_sha256", "reference_mismatch"),
    ],
)
def test_paragraph_projection_preserves_stale_reference_errors(
    demo_dir: Path,
    field: str,
    error_code: str,
) -> None:
    payload, path, anchor, _text = _paragraph_fixture(
        demo_dir / "round-4-counterparty-reply.docx", CAP_R4
    )
    stale_anchor = {**anchor, field: "0" * 64}

    with pytest.raises(VerifyError) as stale:
        build_verification_result_v2(
            payload,
            path=path,
            anchor=stale_anchor,
            quote=CAP_R4,
            paragraph_projection=PENDING_REJECTED_MODE,
        )

    assert stale.value.code == error_code


@pytest.mark.parametrize("text_length", [50_000, 50_001])
def test_current_projection_enforces_paragraph_limit_at_exact_boundary(
    demo_dir: Path,
    tmp_path: Path,
    text_length: int,
) -> None:
    path = tmp_path / f"paragraph-{text_length}.docx"
    _with_replaced_paragraph_text(
        demo_dir / "round-1-outgoing-draft.docx",
        path,
        phrase="governed by the laws of England and Wales",
        replacement="X" * text_length,
    )
    payload, label, anchor, paragraph_text = _paragraph_fixture(path, "X" * 16)
    assert len(paragraph_text) == text_length

    if text_length == 50_000:
        result = build_verification_result_v2(
            payload,
            path=label,
            anchor=anchor,
            quote="X" * 16,
        )
        assert result["verdict"] == "exact"
        assert result["checked_projection"]["text_length"] == 50_000
        return

    with pytest.raises(VerifyError) as refused:
        build_verification_result_v2(
            payload,
            path=label,
            anchor=anchor,
            quote="X" * 16,
        )
    assert refused.value.code == "resource_limit_exceeded"
    assert refused.value.metadata == {
        "limit": "paragraph_text_chars",
        "allowed_chars": 50_000,
        "observed_chars": 50_001,
        "claimed_source_sha256": anchor["file_sha256"],
    }


@pytest.mark.parametrize("selector", ["unknown", "", 0, False, []])
def test_invalid_selector_fails_before_docx_decode(
    demo_dir: Path,
    selector: object,
) -> None:
    _payload, label, anchor, _text = _paragraph_fixture(
        demo_dir / "round-4-counterparty-reply.docx", CAP_R4
    )

    with pytest.raises(VerifyError) as refused:
        build_verification_result_v2(
            b"not a DOCX",
            path=label,
            anchor=anchor,
            quote=CAP_R4,
            paragraph_projection=selector,  # type: ignore[arg-type]
        )

    assert refused.value.code == "invalid_projection_selector"


def test_unavailable_rejected_projection_does_not_poison_current(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-4-counterparty-reply.docx"
    changed = tmp_path / "unavailable.docx"
    _with_stray_deleted_text(source, changed)
    payload, label, anchor, _text = _paragraph_fixture(changed, CAP_R4)

    current = build_verification_result_v2(
        payload,
        path=label,
        anchor=anchor,
        quote=CAP_R4,
    )
    assert current["verdict"] == "exact"

    with pytest.raises(VerifyError) as unavailable:
        build_verification_result_v2(
            payload,
            path=label,
            anchor=anchor,
            quote=CAP_R3,
            paragraph_projection=PENDING_REJECTED_MODE,
        )
    assert unavailable.value.code == "paragraph_projection_unavailable"
    assert unavailable.value.metadata["unavailable_reasons"] == [
        "stray_deleted_text"
    ]


def test_builder_uses_only_the_supplied_immutable_bytes(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    captured_path = tmp_path / "captured.docx"
    shutil.copyfile(demo_dir / "round-4-counterparty-reply.docx", captured_path)
    payload, label, anchor, _text = _paragraph_fixture(captured_path, CAP_R4)
    shutil.copyfile(demo_dir / "round-3-our-counter.docx", captured_path)

    result = build_verification_result_v2(
        payload,
        path=label,
        anchor=anchor,
        quote=CAP_R4,
    )

    assert result["verdict"] == "exact"
    assert result["checked_anchor"]["file_sha256"] == hashlib.sha256(
        payload
    ).hexdigest()


def test_byte_builder_classifies_decode_errors_without_path_state(
    tmp_path: Path,
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("placeholder.txt", b"not a DOCX package")
    payload = buffer.getvalue()
    anchor = {
        "change_unit_id": "cu_001",
        "file_sha256": hashlib.sha256(payload).hexdigest(),
    }
    existing_label = tmp_path / "existing.docx"
    existing_label.write_bytes(b"unrelated live bytes")
    missing_label = tmp_path / "missing.docx"

    errors = []
    for label in (existing_label, missing_label):
        with pytest.raises(VerifyError) as refused:
            build_verification_result_v2(
                payload,
                path=str(label),
                anchor=anchor,
                quote="quoted text",
            )
        errors.append((refused.value.code, str(refused.value)))

    assert [code for code, _detail in errors] == [
        "file_unextractable",
        "file_unextractable",
    ]
    assert all("no word/document.xml" in detail for _code, detail in errors)


def test_semantic_validator_rejects_open_or_inconsistent_results(
    demo_dir: Path,
) -> None:
    payload, path, anchor, _text = _paragraph_fixture(
        demo_dir / "round-4-counterparty-reply.docx", CAP_R4
    )
    valid = build_verification_result_v2(
        payload,
        path=path,
        anchor=anchor,
        quote=CAP_R4,
    )

    unexpected = {**valid, "unexpected": True}
    with pytest.raises(VerifyError) as open_result:
        validate_verification_result_v2(unexpected)
    assert open_result.value.code == "output_contract_error"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(unexpected, VERIFICATION_RESULT_V2_OPERATION_SCHEMA)

    inconsistent = deepcopy(valid)
    inconsistent["matches"][0]["projection_text_sha256"] = "0" * 64
    with pytest.raises(VerifyError) as bad_match:
        validate_verification_result_v2(inconsistent)
    assert bad_match.value.code == "output_contract_error"
    # JSON Schema cannot express equality between the projection digest fields;
    # the semantic validator closes that correlation.
    jsonschema.validate(inconsistent, VERIFICATION_RESULT_V2_OPERATION_SCHEMA)


def test_operation_schema_rejects_variant_and_cardinality_mismatches(
    demo_dir: Path,
) -> None:
    payload, path, anchor, _text = _paragraph_fixture(
        demo_dir / "round-4-counterparty-reply.docx", CAP_R4
    )
    current = build_verification_result_v2(
        payload,
        path=path,
        anchor=anchor,
        quote=CAP_R4,
    )

    wrong_side = deepcopy(current)
    wrong_side["matches"][0]["side"] = "paragraph_rejected_pending"
    missing_exact_match = deepcopy(current)
    missing_exact_match["matches"] = []
    for invalid in (wrong_side, missing_exact_match):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, VERIFICATION_RESULT_V2_OPERATION_SCHEMA)
        with pytest.raises(VerifyError) as refused:
            validate_verification_result_v2(invalid)
        assert refused.value.code == "output_contract_error"

    change_payload, change_path, change_anchor = _change_unit_fixture(
        demo_dir / "round-2-counterparty-redline.docx"
    )
    change = build_verification_result_v2(
        change_payload,
        path=change_path,
        anchor=change_anchor,
        quote=CAP_R2,
    )
    change["checked_projection"] = current["checked_projection"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(change, VERIFICATION_RESULT_V2_OPERATION_SCHEMA)


def test_public_v03_surface_and_writer_remain_frozen(demo_dir: Path) -> None:
    _payload, path, anchor, _text = _paragraph_fixture(
        demo_dir / "round-4-counterparty-reply.docx", CAP_R4
    )
    public_result = verify_quote(path, anchor, CAP_R4)

    assert list(inspect.signature(server.verify_quote).parameters) == [
        "path",
        "anchor",
        "quote",
    ]
    assert set(public_result) == {
        "verdict",
        "exact",
        "checked_anchor",
        "matches",
        "diff",
    }
    assert MCP_CONTRACT_SCHEMA_VERSION == "veqtor.mcp.v0.3"
    assert "schema_version" not in VerifyQuoteResult.contract_schema["properties"]
    assert "checked_projection" not in VerifyQuoteResult.contract_schema["properties"]
    assert len(records.WRITABLE_TOOL_NAMES) == 8
    assert "trace_paragraph_history" not in records.WRITABLE_TOOL_NAMES
    assert records.WRITABLE_TOOL_SPECS["verify_quote"].record_type == (
        "verification.v1"
    )
    assert set(records.HISTORICAL_RECORD_SPECS) == {
        (tool_name, spec.record_type)
        for tool_name, spec in records.V1_HISTORICAL_TOOL_SPECS.items()
    }
    assert "verification.v2" not in records.KNOWN_RECORD_TYPES
