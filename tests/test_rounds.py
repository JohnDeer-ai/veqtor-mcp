# SPDX-License-Identifier: Apache-2.0
"""list_rounds folder semantics: deterministic order, honest failure modes."""

import hashlib
import shutil
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from veqtor_docx import RoundError, extract_redlines, list_rounds
from veqtor_docx import rounds as rounds_module


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


def test_unexpected_file_failure_is_not_leaked_as_a_successful_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "matter"
    folder.mkdir()
    (folder / "round.docx").write_bytes(b"placeholder")
    sentinel = "private client sentinel"

    def explode(_path: Path):
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
