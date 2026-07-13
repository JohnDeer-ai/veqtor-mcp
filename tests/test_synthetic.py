# SPDX-License-Identifier: Apache-2.0
"""The synthetic corpus itself must be deterministic and Word-shaped."""

import os
import zipfile
from pathlib import Path

import pytest

from veqtor_docx import SyntheticError, generate_demo_rounds
from veqtor_docx import synthetic

ROUND_FILES = [
    "round-1-outgoing-draft.docx",
    "round-2-counterparty-redline.docx",
    "round-3-our-counter.docx",
    "round-4-counterparty-reply.docx",
]


def test_generates_four_rounds(demo_dir: Path) -> None:
    assert sorted(p.name for p in demo_dir.glob("*.docx")) == ROUND_FILES


def test_generation_is_byte_deterministic(tmp_path: Path) -> None:
    first = generate_demo_rounds(tmp_path / "a")
    second = generate_demo_rounds(tmp_path / "b")
    for left, right in zip(first, second, strict=True):
        assert left.read_bytes() == right.read_bytes()


def test_existing_output_is_never_overwritten_or_partially_published(
    tmp_path: Path,
) -> None:
    out = tmp_path / "matter"
    out.mkdir()
    collision = out / ROUND_FILES[2]
    sentinel = b"USER DOCUMENT SENTINEL"
    collision.write_bytes(sentinel)

    with pytest.raises(SyntheticError) as error:
        generate_demo_rounds(out)

    assert error.value.code == "output_exists"
    assert collision.read_bytes() == sentinel
    assert sorted(path.name for path in out.iterdir()) == [ROUND_FILES[2]]


def test_directory_collision_is_refused_before_any_file_is_created(
    tmp_path: Path,
) -> None:
    out = tmp_path / "matter"
    out.mkdir()
    (out / ROUND_FILES[2]).mkdir()

    with pytest.raises(SyntheticError) as error:
        generate_demo_rounds(out)

    assert error.value.code == "output_exists"
    assert sorted(path.name for path in out.iterdir()) == [ROUND_FILES[2]]


def test_publish_failure_rolls_back_the_entire_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "matter"
    real_link = os.link
    calls = 0
    sentinel = "PRIVATE PUBLISH PATH SENTINEL"

    def fail_third_link(source, target, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise PermissionError(sentinel)
        return real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(synthetic.os, "link", fail_third_link)
    with pytest.raises(SyntheticError) as error:
        generate_demo_rounds(out)

    assert error.value.code == "output_unwritable"
    assert sentinel not in str(error.value)
    assert not out.exists()


def test_non_directory_output_is_a_controlled_refusal(tmp_path: Path) -> None:
    out = tmp_path / "existing-file"
    out.write_bytes(b"sentinel")

    with pytest.raises(SyntheticError) as error:
        generate_demo_rounds(out)

    assert error.value.code == "output_not_directory"
    assert out.read_bytes() == b"sentinel"


def test_uncreatable_output_is_a_controlled_refusal() -> None:
    with pytest.raises(SyntheticError) as error:
        generate_demo_rounds("/dev/null/child")
    assert error.value.code == "output_unwritable"


def test_cli_help_does_not_create_a_help_named_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["veqtor-demo-rounds", "--help"])

    with pytest.raises(SystemExit) as exit_info:
        synthetic.main()

    assert exit_info.value.code == 0
    assert not (tmp_path / "--help").exists()


def test_cli_reports_a_controlled_write_error_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    output = tmp_path / "existing-file"
    output.write_bytes(b"sentinel")
    monkeypatch.setattr("sys.argv", ["veqtor-demo-rounds", str(output)])

    assert synthetic.main() == 2

    captured = capsys.readouterr()
    assert "output_not_directory" in captured.err
    assert "Traceback" not in captured.err
    assert output.read_bytes() == b"sentinel"


def test_no_builtin_heading_styles(demo_dir: Path) -> None:
    """Anchors must survive real firm templates, so the fixtures use custom
    styles with outlineLvl instead of Heading1-9 (the private corpora showed
    zero built-in heading styles)."""
    styles = zipfile.ZipFile(demo_dir / ROUND_FILES[1]).read("word/styles.xml")
    assert b'w:styleId="Heading' not in styles
    assert b"outlineLvl" in styles
    assert b'w:styleId="VLegal2"' in styles


def test_realistic_word_noise(demo_dir: Path) -> None:
    document = zipfile.ZipFile(demo_dir / ROUND_FILES[1]).read("word/document.xml")
    assert document.count(b"w:rsidR") > 50  # rsid attribute noise
    assert b"w:delText" in document  # deletions carry delText, not w:t
    parts = zipfile.ZipFile(demo_dir / ROUND_FILES[1]).namelist()
    assert "word/numbering.xml" in parts
    assert "word/styles.xml" in parts
