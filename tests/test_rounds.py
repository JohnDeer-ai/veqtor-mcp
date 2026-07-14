# SPDX-License-Identifier: Apache-2.0
"""list_rounds folder semantics: deterministic order, honest failure modes."""

import hashlib
import shutil
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from veqtor_docx import RoundError, extract_redlines, list_rounds
from veqtor_docx import _ooxml
from veqtor_docx import rounds as rounds_module


_DOCUMENT_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>Round</w:t></w:r></w:p></w:body>
</w:document>
'''


def _write_round(path: Path, *, compression: int = zipfile.ZIP_DEFLATED) -> None:
    with zipfile.ZipFile(path, "w", compression) as archive:
        archive.writestr("word/document.xml", _DOCUMENT_XML)


def _with_unsupported_document_compression(payload: bytes) -> bytes:
    """Patch both ZIP headers without making the package unparsable."""
    mutated = bytearray(payload)
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        document = archive.getinfo("word/document.xml")
    mutated[document.header_offset + 8 : document.header_offset + 10] = (99).to_bytes(
        2, "little"
    )

    cursor = 0
    central_signature = b"PK\x01\x02"
    while (cursor := payload.find(central_signature, cursor)) >= 0:
        name_length = int.from_bytes(payload[cursor + 28 : cursor + 30], "little")
        extra_length = int.from_bytes(payload[cursor + 30 : cursor + 32], "little")
        comment_length = int.from_bytes(payload[cursor + 32 : cursor + 34], "little")
        name = payload[cursor + 46 : cursor + 46 + name_length].decode("utf-8")
        if name == document.filename:
            mutated[cursor + 10 : cursor + 12] = (99).to_bytes(2, "little")
            return bytes(mutated)
        cursor += 46 + name_length + extra_length + comment_length
    raise AssertionError("document central-directory record not found")


def _with_invalid_utf8_member_name(payload: bytes) -> bytes:
    """Make one central/local member name invalid under its UTF-8 flag."""
    mutated = bytearray(payload)
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        member = archive.getinfo("word/document.xml")

    local = member.header_offset
    local_flags = int.from_bytes(payload[local + 6 : local + 8], "little") | 0x800
    mutated[local + 6 : local + 8] = local_flags.to_bytes(2, "little")
    local_name = local + 30
    mutated[local_name] = 0xFF

    cursor = 0
    while (cursor := payload.find(b"PK\x01\x02", cursor)) >= 0:
        name_length = int.from_bytes(payload[cursor + 28 : cursor + 30], "little")
        extra_length = int.from_bytes(payload[cursor + 30 : cursor + 32], "little")
        comment_length = int.from_bytes(payload[cursor + 32 : cursor + 34], "little")
        name = payload[cursor + 46 : cursor + 46 + name_length].decode("utf-8")
        if name == member.filename:
            central_flags = (
                int.from_bytes(payload[cursor + 8 : cursor + 10], "little") | 0x800
            )
            mutated[cursor + 8 : cursor + 10] = central_flags.to_bytes(2, "little")
            mutated[cursor + 46] = 0xFF
            return bytes(mutated)
        cursor += 46 + name_length + extra_length + comment_length
    raise AssertionError("document central-directory record not found")


def test_public_alpha_round_folder_envelope_is_frozen() -> None:
    assert rounds_module.MAX_ROUND_CANDIDATES == 500
    assert rounds_module.MAX_ROUND_TOTAL_INPUT_BYTES == 500 * 1024 * 1024
    assert rounds_module.MAX_ROUND_TOTAL_EXPANDED_BYTES == 500 * 1024 * 1024


def test_aggregate_round_expansion_limit_is_inclusive_and_resets_per_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_round(tmp_path / "round-1.docx")
    _write_round(tmp_path / "round-2.docx")
    exact_total = 2 * len(_DOCUMENT_XML)
    monkeypatch.setattr(
        rounds_module,
        "MAX_ROUND_TOTAL_EXPANDED_BYTES",
        exact_total,
    )

    first = list_rounds(str(tmp_path))
    second = list_rounds(str(tmp_path))

    assert len(first["rounds"]) == 2
    assert len(second["rounds"]) == 2

    monkeypatch.setattr(
        rounds_module,
        "MAX_ROUND_TOTAL_EXPANDED_BYTES",
        exact_total - 1,
    )
    with pytest.raises(RoundError) as error:
        list_rounds(str(tmp_path))

    assert error.value.code == "resource_limit_exceeded"
    assert "aggregate expanded-output limit" in str(error.value)
    assert "split the folder and retry" in str(error.value)


def test_deflate_budget_refuses_at_the_first_byte_over_limit(tmp_path: Path) -> None:
    source = tmp_path / "round.docx"
    _write_round(source)
    budget = _ooxml.ExpandedOutputBudget(
        allowed_bytes=len(_DOCUMENT_XML) - 1,
        limit="test_expanded_bytes",
    )

    with pytest.raises(_ooxml.ExpandedOutputBudgetExceeded) as error:
        _ooxml.load_validated_docx(
            source.read_bytes(),
            capture=(_ooxml.DOCUMENT_PART,),
            expanded_budget=budget,
        )

    assert budget.consumed_bytes == len(_DOCUMENT_XML)
    assert budget.remaining_bytes == 0
    assert error.value.metadata == {
        "limit": "test_expanded_bytes",
        "allowed_bytes": len(_DOCUMENT_XML) - 1,
        "observed_bytes": len(_DOCUMENT_XML),
        "observed_at_least": True,
    }


def test_lists_rounds_in_filename_order(demo_dir: Path) -> None:
    result = list_rounds(str(demo_dir))
    assert [r["round_id"] for r in result["rounds"]] == [
        "round-001",
        "round-002",
        "round-003",
        "round-004",
    ]
    filenames = [r["filename"] for r in result["rounds"]]
    assert filenames == sorted(filenames)
    assert result["skipped"] == []


def test_hashes_and_revision_counts_match_extraction(demo_dir: Path) -> None:
    for entry in list_rounds(str(demo_dir))["rounds"]:
        payload = Path(entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
        assert entry["revision_count"] == extract_redlines(entry["path"])["revision_count"]


def test_ignores_junk_and_survives_corrupt_files(demo_dir: Path, tmp_path: Path) -> None:
    folder = tmp_path / "deal"
    shutil.copytree(demo_dir, folder)
    (folder / "~$round-2-counterparty-redline.docx").write_bytes(b"\x00\x01lock")
    (folder / "notes.txt").write_text("not a round")
    (folder / "00-broken.docx").write_bytes(b"this is not a zip archive")
    (folder / "sub").mkdir()
    shutil.copy(next(demo_dir.glob("*.docx")), folder / "sub" / "nested.docx")

    result = list_rounds(str(folder))
    assert [r["filename"] for r in result["rounds"]] == [
        "round-1-outgoing-draft.docx",
        "round-2-counterparty-redline.docx",
        "round-3-our-counter.docx",
        "round-4-counterparty-reply.docx",
    ]
    # Valid rounds keep contiguous ids even when a broken file sorts first.
    assert [r["round_id"] for r in result["rounds"]] == [
        "round-001",
        "round-002",
        "round-003",
        "round-004",
    ]
    assert [s["filename"] for s in result["skipped"]] == ["00-broken.docx"]
    assert result["skipped"][0]["reason"] == "invalid_docx"


def test_missing_folder_raises(tmp_path: Path) -> None:
    with pytest.raises(RoundError) as error:
        list_rounds(str(tmp_path / "nope"))
    assert error.value.code == "not_a_folder"


def test_unsupported_zip_compression_is_a_stable_skip(
    demo_dir: Path, tmp_path: Path
) -> None:
    source = demo_dir / "round-2-counterparty-redline.docx"
    broken = tmp_path / "unsupported-compression.docx"
    broken.write_bytes(_with_unsupported_document_compression(source.read_bytes()))

    result = list_rounds(str(tmp_path))

    assert result["rounds"] == []
    assert result["skipped"] == [
        {
            "filename": broken.name,
            "reason": "unsupported_compression",
        }
    ]
    with pytest.raises(rounds_module.DocxError):
        extract_redlines(str(broken))


def test_invalid_utf8_zip_member_name_is_a_stable_skip(
    demo_dir: Path, tmp_path: Path
) -> None:
    source = demo_dir / "round-2-counterparty-redline.docx"
    broken = tmp_path / "invalid-utf8-name.docx"
    broken.write_bytes(_with_invalid_utf8_member_name(source.read_bytes()))

    result = list_rounds(str(tmp_path))

    assert result["rounds"] == []
    assert result["skipped"] == [
        {"filename": broken.name, "reason": "invalid_docx"}
    ]


def test_unreadable_folder_is_a_stable_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_iterdir = Path.iterdir

    def deny(target: Path):
        if target == tmp_path:
            raise PermissionError("private filesystem detail")
        return original_iterdir(target)

    monkeypatch.setattr(Path, "iterdir", deny)
    with pytest.raises(RoundError) as error:
        list_rounds(str(tmp_path))
    assert error.value.code == "folder_unreadable"
    assert str(error.value) == "folder_unreadable: cannot enumerate folder"


def test_candidate_count_is_bounded_before_any_docx_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(3):
        (tmp_path / f"round-{index}.docx").write_bytes(b"placeholder")
    monkeypatch.setattr(rounds_module, "MAX_ROUND_CANDIDATES", 2)

    def fail_read(_path: Path, *, expanded_budget):
        raise AssertionError("candidate was read before folder preflight")

    monkeypatch.setattr(rounds_module, "_round_facts", fail_read)

    with pytest.raises(RoundError) as error:
        list_rounds(str(tmp_path))

    assert error.value.code == "resource_limit_exceeded"
    assert str(error.value) == (
        "resource_limit_exceeded: folder contains more than 2 candidate DOCX files"
    )


def test_aggregate_round_input_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(2):
        (tmp_path / f"round-{index}.docx").write_bytes(b"invaliddoc")
    monkeypatch.setattr(rounds_module, "MAX_ROUND_TOTAL_INPUT_BYTES", 19)

    def fail_read(_path: Path, *, expanded_budget):
        raise AssertionError("candidate was read before aggregate preflight")

    monkeypatch.setattr(rounds_module, "_round_facts", fail_read)

    with pytest.raises(RoundError) as error:
        list_rounds(str(tmp_path))

    assert error.value.code == "resource_limit_exceeded"
    assert "aggregate input limit" in str(error.value)


def test_unexpected_file_failure_is_not_leaked_as_a_successful_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "matter"
    folder.mkdir()
    (folder / "round.docx").write_bytes(b"placeholder")
    sentinel = "private client sentinel"

    def explode(_path: Path, *, expanded_budget):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(rounds_module, "_round_facts", explode)
    with pytest.raises(RuntimeError, match=sentinel):
        list_rounds(str(folder))


def test_user_home_folder_is_expanded(demo_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(demo_dir.parent))
    result = list_rounds(f"~/{demo_dir.name}")
    assert len(result["rounds"]) == 4
    assert "~" not in result["folder"]
    assert all("~" not in r["path"] for r in result["rounds"])
