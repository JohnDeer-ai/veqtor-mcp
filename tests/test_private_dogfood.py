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
import shutil
import zipfile
from pathlib import Path

import pytest

from veqtor_docx import apply_edits, extract_redlines
from veqtor_docx._ooxml import parse_xml, w
from veqtor_docx.apply import DEFAULT_AUTHOR, _paragraph_segments, _resolve_anchor_paragraph

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
            if unit["change_type"] == "counter" or unit.get("countered_by"):
                # Cross-author counters live in three-party pending state:
                # their old/new text is defined against the proposal, not
                # against the clean/accepted two-version world.
                continue
            old = _squash(unit["old_text"] or "")
            new = _squash(unit["new_text"] or "")
            if len(old) >= 6:
                assert old in clean_text, f"{unit['change_unit_id']} old_text not in clean"
            if len(new) >= 6:
                assert new in accepted_text, f"{unit['change_unit_id']} new_text not in accepted"
    if not triples:
        pytest.skip("no clean/tracked/accepted triples in the private corpus")


def test_apply_edits_on_a_copy_of_a_real_redline(tmp_path: Path) -> None:
    """M2 dogfood: apply a runtime-derived tracked edit to a COPY of a real
    redlined document and prove the round trip. The edit text is derived from
    the document at runtime — no contract text lives in this file — and the
    original corpus file is never opened for writing."""
    applied = 0
    for original in _corpus_files():
        extraction = extract_redlines(str(original))
        if not extraction["change_units"]:
            continue

        working_copy = tmp_path / f"copy-{applied}-{original.name}"
        shutil.copyfile(original, working_copy)
        source = extract_redlines(str(working_copy))
        document = parse_xml(
            zipfile.ZipFile(working_copy).read("word/document.xml")
        )

        # Derive a delete_text candidate: a plain-run substring, unique in its
        # paragraph, taken from the clause of some existing change unit.
        target = None
        for unit in source["change_units"]:
            paragraph = _resolve_anchor_paragraph(document, unit)
            segments = _paragraph_segments(paragraph)
            reading = "".join(seg.node.text or "" for seg in segments)
            for seg in segments:
                text = seg.node.text or ""
                if not seg.plain or len(text) < 28:
                    continue
                candidate = text[3:25].strip()
                if len(candidate) >= 12 and reading.count(candidate) == 1:
                    target = (unit, candidate)
                    break
            if target:
                break
        if target is None:
            continue

        unit, delete_text = target
        out = tmp_path / f"out-{applied}-{original.name}"
        result = apply_edits(
            str(working_copy),
            str(out),
            [
                {
                    "anchor": {
                        "change_unit_id": unit["change_unit_id"],
                        "file_sha256": source["file_sha256"],
                    },
                    "delete_text": delete_text,
                    "insert_text": delete_text.upper(),
                }
            ],
        )
        assert result["round_trip_check"]["status"] == "passed"
        mine = [
            u
            for u in extract_redlines(str(out))["change_units"]
            if u["author"] == DEFAULT_AUTHOR
        ]
        assert len(mine) == 1
        assert mine[0]["old_text"] == delete_text
        applied += 1
        if applied >= 3:
            break
    assert applied >= 1, "no private document accepted a derived edit"
