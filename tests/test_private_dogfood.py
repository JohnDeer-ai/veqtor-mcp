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
import stat
import zipfile
from pathlib import Path

import pytest

from veqtor_docx import apply_edits, extract_redlines, verify_quote
from veqtor_docx._ooxml import parse_xml, w
from veqtor_docx.apply import DEFAULT_AUTHOR, _paragraph_segments, _resolve_anchor_paragraph
from veqtor_mcp import records, server

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


def _runtime_edit_target(path: Path, units: list[dict]) -> tuple[dict, str] | None:
    document = parse_xml(zipfile.ZipFile(path).read("word/document.xml"))
    for unit in units:
        paragraph = _resolve_anchor_paragraph(document, unit)
        segments = _paragraph_segments(paragraph)
        reading = "".join(seg.node.text or "" for seg in segments)
        for seg in segments:
            text = seg.node.text or ""
            if not seg.plain or len(text) < 28:
                continue
            candidate = text[3:25].strip()
            if (
                len(candidate) >= 12
                and candidate.upper() != candidate
                and reading.count(candidate) == 1
            ):
                return unit, candidate
    return None


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
        # Derive a delete_text candidate: a plain-run substring, unique in its
        # paragraph, taken from the clause of some existing change unit.
        target = _runtime_edit_target(working_copy, source["change_units"])
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


def test_verify_quote_confirms_extracted_texts_on_real_redlines() -> None:
    """M3 dogfood: every extracted quote must verify as exact against its own
    anchor — the read path and the verifier must agree on real documents."""
    checked = 0
    for path in _corpus_files():
        extraction = extract_redlines(str(path))
        for unit in extraction["change_units"]:
            text = unit["new_text"] or unit["old_text"]
            if not text or len(text) < 12:
                continue
            outcome = verify_quote(
                str(path),
                {
                    "change_unit_id": unit["change_unit_id"],
                    "file_sha256": extraction["file_sha256"],
                },
                text,
            )
            assert outcome["verdict"] == "exact", unit["change_unit_id"]
            checked += 1
            if checked >= 40:
                break
        if checked >= 40:
            break
    assert checked, "no verifiable change units found in the private corpus"


def test_mcp_recorder_export_workflow_on_private_matter_copy(tmp_path: Path) -> None:
    """M3 slice 2 dogfood: exercise the MCP layer, not only the domain layer.

    One suitable real document is copied into a temporary matter. The sidecar
    journal is expected only beside that copy, and compact export is checked
    against runtime old/new strings of at least 12 characters.
    """
    exercised = False
    for index, original in enumerate(_corpus_files()):
        matter = tmp_path / f"matter-{index}"
        matter.mkdir()
        working_copy = matter / original.name
        shutil.copyfile(original, working_copy)

        extraction = server.extract_redlines(str(working_copy))
        units = extraction["change_units"]
        if not units:
            continue

        quote_target = next(
            (
                (unit, text)
                for unit in units
                for text in (unit["new_text"], unit["old_text"])
                if text and len(text) >= 12
            ),
            None,
        )
        target = _runtime_edit_target(working_copy, units)
        if quote_target is None or target is None:
            continue

        quote_unit, quote = quote_target
        anchor = {
            "change_unit_id": quote_unit["change_unit_id"],
            "file_sha256": extraction["file_sha256"],
        }
        verified = server.verify_quote(str(working_copy), anchor, quote)
        assert verified["record_status"] == "written"
        assert verified["verdict"] == "exact"

        edit_unit, delete_text = target
        output_path = matter / f"mcp-output-{original.name}"
        applied = server.apply_edits(
            str(working_copy),
            str(output_path),
            [
                {
                    "anchor": {
                        "change_unit_id": edit_unit["change_unit_id"],
                        "file_sha256": extraction["file_sha256"],
                    },
                    "delete_text": delete_text,
                    "insert_text": delete_text.upper(),
                }
            ],
        )
        assert applied["record_status"] == "written"
        assert applied["round_trip_check"]["status"] == "passed"
        assert applied["output_sha256"] == extract_redlines(str(output_path))["file_sha256"]

        exported = server.export_decision_record(str(matter), max_records=10)
        assert exported["record_status"] == "written"
        assert exported["payloads"] == "compact"
        assert exported["total_count"] >= 3
        tool_names = [record["tool_name"] for record in exported["records"]]
        assert "extract_redlines" in tool_names
        assert "verify_quote" in tool_names
        assert "apply_edits" in tool_names

        apply_record = next(
            record for record in exported["records"] if record["tool_name"] == "apply_edits"
        )
        assert apply_record["input"]["omitted"] is True
        assert apply_record["provenance"]["output_sha256"] == applied["output_sha256"]
        assert apply_record["provenance"]["round_trip_check"]["status"] == "passed"

        sensitive_values = {
            text
            for unit in units
            for text in (unit["new_text"], unit["old_text"])
            if text and len(text) >= 12
        }
        sensitive_values.update({quote, delete_text, delete_text.upper()})
        encoded = json.dumps(exported, ensure_ascii=False)
        if any(value in encoded for value in sensitive_values):
            pytest.fail("compact export leaked a verbatim private payload")

        sidecar = matter / records.SIDECAR_DIR
        journal = sidecar / records.JOURNAL_NAME
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o700
        assert stat.S_IMODE(journal.stat().st_mode) == 0o600
        assert not (original.parent / records.SIDECAR_DIR).exists()
        exercised = True
        break
    assert exercised, "no private document supported MCP recorder/export dogfood"
