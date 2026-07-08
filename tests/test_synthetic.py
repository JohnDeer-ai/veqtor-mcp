# SPDX-License-Identifier: Apache-2.0
"""The synthetic corpus itself must be deterministic and Word-shaped."""

import zipfile
from pathlib import Path

from veqtor_docx import generate_demo_rounds

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
