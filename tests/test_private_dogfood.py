# SPDX-License-Identifier: Apache-2.0
"""Local dogfood against private real-matter corpora.

Skipped unless ``VEQTOR_PRIVATE_FIXTURE_DIR`` points at a local folder of
DOCX files. Assertions are structural only — counts, determinism, chain
integrity — never quotes: this test file is public while the corpora are
not, and no contract text may leak into the repository.

Run locally with::

    VEQTOR_PRIVATE_FIXTURE_DIR="/path/to/private/corpus" pytest -m private
"""

import json
import os
import zipfile
from pathlib import Path

import pytest

from veqtor_docx import extract_redlines
from veqtor_docx._ooxml import parse_xml, w

_ENV = "VEQTOR_PRIVATE_FIXTURE_DIR"

pytestmark = [
    pytest.mark.private,
    pytest.mark.skipif(_ENV not in os.environ, reason=f"{_ENV} is not set"),
]


def _corpus_files() -> list[Path]:
    root = Path(os.environ.get(_ENV, "."))
    return sorted(p for p in root.rglob("*.docx") if not p.name.startswith("~$"))


def _visible_text(path: Path) -> str:
    """Document text with insertions accepted, whitespace squashed away."""
    document = parse_xml(zipfile.ZipFile(path).read("word/document.xml"))
    parts: list[str] = []
    for node in document.iter(w("t")):
        ancestors = {a.tag for a in node.iterancestors()}
        if w("del") in ancestors or w("moveFrom") in ancestors:
            continue
        parts.append(node.text or "")
    return "".join("".join(parts).split())


def _squash(text: str) -> str:
    return "".join(text.split())


def test_every_private_docx_extracts_deterministically() -> None:
    files = _corpus_files()
    assert files, "private fixture dir contains no DOCX files"
    for path in files:
        first = extract_redlines(str(path))
        second = extract_redlines(str(path))
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
        for unit in first["change_units"]:
            assert unit["author"], f"unit without author in {path.name}"
            assert unit["old_text"] or unit["new_text"]
        ids = [u["change_unit_id"] for u in first["change_units"]]
        assert len(ids) == len(set(ids))
        revision_ids = [
            rid for u in first["change_units"] for rid in u["reference"]["revision_ids"]
        ]
        assert len(revision_ids) == len(set(revision_ids))


def test_chain_integrity_on_clean_tracked_accepted_triples() -> None:
    """Where a folder holds a clean/tracked/accepted triple, every extracted
    old_text must appear verbatim in the clean version and every new_text in
    the accepted version. This proves the extractor reports real document
    facts, not artifacts of its own grouping."""
    triples = 0
    for path in _corpus_files():
        name = path.name.casefold()
        if "track" not in name or "before" in name:
            continue
        folder = path.parent
        clean = [p for p in folder.glob("*.docx") if "clean" in p.name.casefold()]
        accepted = [p for p in folder.glob("*.docx") if "accepted" in p.name.casefold()]
        if len(clean) != 1 or len(accepted) != 1:
            continue
        triples += 1
        clean_text = _visible_text(clean[0])
        accepted_text = _visible_text(accepted[0])
        for unit in extract_redlines(str(path))["change_units"]:
            old = _squash(unit["old_text"] or "")
            new = _squash(unit["new_text"] or "")
            if len(old) >= 6:
                assert old in clean_text, f"{unit['change_unit_id']} old_text not in clean"
            if len(new) >= 6:
                assert new in accepted_text, f"{unit['change_unit_id']} new_text not in accepted"
    if not triples:
        pytest.skip("no clean/tracked/accepted triples in the private corpus")
