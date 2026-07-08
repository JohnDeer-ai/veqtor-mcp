# SPDX-License-Identifier: Apache-2.0
"""list_rounds folder semantics: deterministic order, honest failure modes."""

import hashlib
import shutil
from pathlib import Path

import pytest

from veqtor_docx import extract_redlines, list_rounds


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
    assert result["skipped"][0]["reason"]


def test_missing_folder_raises(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        list_rounds(str(tmp_path / "nope"))


def test_user_home_folder_is_expanded(demo_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(demo_dir.parent))
    result = list_rounds(f"~/{demo_dir.name}")
    assert len(result["rounds"]) == 4
    assert "~" not in result["folder"]
    assert all("~" not in r["path"] for r in result["rounds"])
