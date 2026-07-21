# SPDX-License-Identifier: Apache-2.0
"""Finite conformance sweep for the exported DOCX operation boundary."""

from pathlib import Path

import pytest

import veqtor_docx


INVALID_PATHS = [
    (None, "invalid_path"),
    (17, "invalid_path"),
    (b"bytes", "invalid_path"),
    ("bad\udcffpath", "invalid_path"),
    ("bad\x00path", "invalid_path"),
    ("~veqtor_no_such_user_774/document.docx", "path_unresolvable"),
]


@pytest.mark.parametrize(("invalid_path", "_code"), INVALID_PATHS)
def test_read_operations_reject_invalid_path_types_with_docx_errors(
    invalid_path: object,
    _code: str,
) -> None:
    operations = (
        lambda: veqtor_docx.list_rounds(invalid_path),  # type: ignore[arg-type]
        lambda: veqtor_docx.extract_redlines(invalid_path),  # type: ignore[arg-type]
        lambda: veqtor_docx.inspect_document(  # type: ignore[arg-type]
            invalid_path,
            "outline",
        ),
        lambda: veqtor_docx.verify_quote(  # type: ignore[arg-type]
            invalid_path,
            {"change_unit_id": "cu_001", "file_sha256": "0" * 64},
            "quote",
        ),
    )
    for operation in operations:
        with pytest.raises(veqtor_docx.DocxError):
            operation()


@pytest.mark.parametrize(("invalid_path", "expected_code"), INVALID_PATHS)
def test_edit_operations_reject_invalid_paths_without_output(
    invalid_path: object,
    expected_code: str,
    tmp_path: Path,
) -> None:
    edit = {
        "anchor": {"change_unit_id": "cu_001", "file_sha256": "0" * 64},
        "delete_text": "text",
    }
    preflight = veqtor_docx.preflight_edits(  # type: ignore[arg-type]
        invalid_path, [edit]
    )
    assert preflight["batch_applicable"] is False
    assert preflight["refusal_code"] == expected_code
    assert preflight["failure_phase"] == "validation"

    output = tmp_path / "never.docx"
    with pytest.raises(veqtor_docx.ApplyError) as error:
        veqtor_docx.apply_edits(  # type: ignore[arg-type]
            invalid_path, output, [edit]
        )
    assert error.value.code == expected_code
    assert not output.exists()


@pytest.mark.parametrize(("invalid_output", "expected_code"), INVALID_PATHS)
def test_apply_rejects_invalid_output_paths_before_publication(
    demo_dir: Path,
    invalid_output: object,
    expected_code: str,
) -> None:
    source = demo_dir / "round-2-counterparty-redline.docx"
    extracted = veqtor_docx.extract_redlines(str(source))
    unit = extracted["change_units"][0]
    edit = {
        "anchor": {
            "change_unit_id": unit["change_unit_id"],
            "file_sha256": extracted["file_sha256"],
        },
        "delete_text": unit["new_text"] or unit["old_text"],
    }
    with pytest.raises(veqtor_docx.ApplyError) as error:
        veqtor_docx.apply_edits(  # type: ignore[arg-type]
            str(source), invalid_output, [edit]
        )
    assert error.value.code == expected_code


@pytest.mark.parametrize(("invalid_path", "_code"), INVALID_PATHS)
def test_demo_generator_shares_the_total_path_boundary(
    invalid_path: object,
    _code: str,
) -> None:
    with pytest.raises(veqtor_docx.DocxError):
        veqtor_docx.generate_demo_rounds(invalid_path)  # type: ignore[arg-type]
