# SPDX-License-Identifier: Apache-2.0
"""extract_redlines against the synthetic rounds: every fact must be exact."""

import json
from pathlib import Path

from veqtor_docx import extract_redlines
from veqtor_docx.synthetic import (
    ADVISER_SENTENCE,
    AUDIT_SENTENCE,
    AUTHOR_CP,
    AUTHOR_US,
    CAP_R1,
    CAP_R2,
    CAP_R3,
    CAP_R4,
    CARVEOUT_DROPPED,
)


def _round(demo_dir: Path, number: int) -> dict:
    path = sorted(demo_dir.glob("*.docx"))[number - 1]
    return extract_redlines(str(path))


def test_clean_round_has_no_changes(demo_dir: Path) -> None:
    result = _round(demo_dir, 1)
    assert result["change_units"] == []
    assert result["revision_count"] == 0
    assert result["unsupported_revisions"] == {}


def test_round2_units(demo_dir: Path) -> None:
    result = _round(demo_dir, 2)
    units = result["change_units"]
    assert [u["change_type"] for u in units] == ["replace", "delete", "insert", "replace"]
    assert {u["author"] for u in units} == {AUTHOR_CP}
    assert {u["date"] for u in units} == {"2026-05-05T09:30:00Z"}

    table_cell, audit, adviser, cap = units
    # Tracked change inside a table cell is anchored to the numbered subclause.
    assert (table_cell["old_text"], table_cell["new_text"]) == ("50%", "65%")
    assert table_cell["clause_anchor"] == {"label": "3.3", "heading": "Cancellation Charges"}

    assert audit["old_text"] == AUDIT_SENTENCE
    assert audit["new_text"] is None
    assert audit["clause_anchor"] == {"label": "7", "heading": "Records and Audit"}

    # One logical insertion split by Word into two adjacent w:ins wrappers
    # must come back as a single unit carrying both revision ids.
    assert adviser["new_text"] == ADVISER_SENTENCE
    assert adviser["old_text"] is None
    assert len(adviser["reference"]["revision_ids"]) == 2
    assert adviser["clause_anchor"] == {
        "label": "9.1",
        "heading": "Confidentiality Obligations",
    }

    assert (cap["old_text"], cap["new_text"]) == (CAP_R1, CAP_R2)
    assert cap["clause_anchor"] == {"label": "14.2", "heading": "Limitation of Liability"}

    # Formatting-only tracked change is reported, not silently dropped.
    assert result["unsupported_revisions"] == {"rPrChange": 1}
    assert result["revision_count"] == 7


def test_round3_units(demo_dir: Path) -> None:
    result = _round(demo_dir, 3)
    units = result["change_units"]
    assert [u["change_type"] for u in units] == ["insert", "replace", "insert"]
    assert {u["author"] for u in units} == {AUTHOR_US}

    compelled, cap, carveout = units
    # A manually numbered inserted clause anchors to itself.
    assert compelled["clause_anchor"] == {"label": "9.5", "heading": "Compelled Disclosure"}
    assert compelled["new_text"].startswith("9.5 Compelled Disclosure.")

    assert (cap["old_text"], cap["new_text"]) == (CAP_R2, CAP_R3)
    assert carveout["change_type"] == "insert"
    assert carveout["clause_anchor"]["label"] == "14.2"

    assert result["unsupported_revisions"] == {
        "moveTo": 1,
        "moveFrom": 1,
        "paragraphMarkIns": 1,
        "pPrChange": 1,
    }


def test_round4_units(demo_dir: Path) -> None:
    result = _round(demo_dir, 4)
    units = result["change_units"]
    assert [u["change_type"] for u in units] == ["replace", "delete"]
    cap, dropped = units
    assert (cap["old_text"], cap["new_text"]) == (CAP_R3, CAP_R4)
    assert dropped["old_text"] == CARVEOUT_DROPPED
    assert {u["author"] for u in units} == {AUTHOR_CP}


def test_liability_timeline_chains_across_rounds(demo_dir: Path) -> None:
    """The demo story: each round's cap replaces exactly the previous value."""
    caps = []
    for number in (2, 3, 4):
        for unit in _round(demo_dir, number)["change_units"]:
            anchor = unit["clause_anchor"]
            if unit["change_type"] == "replace" and anchor and anchor["label"] == "14.2":
                caps.append(unit)
    assert [(u["old_text"], u["new_text"]) for u in caps] == [
        (CAP_R1, CAP_R2),
        (CAP_R2, CAP_R3),
        (CAP_R3, CAP_R4),
    ]


def test_references_are_deterministic_and_verifiable(demo_dir: Path) -> None:
    path = sorted(demo_dir.glob("*.docx"))[1]
    first = extract_redlines(str(path))
    second = extract_redlines(str(path))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    ids = [u["change_unit_id"] for u in first["change_units"]]
    assert ids == [f"cu_{i:03d}" for i in range(1, len(ids) + 1)]

    all_revision_ids = [
        rid for u in first["change_units"] for rid in u["reference"]["revision_ids"]
    ]
    assert len(all_revision_ids) == len(set(all_revision_ids))
    for unit in first["change_units"]:
        assert unit["file_sha256"] == first["file_sha256"]
        assert unit["reference"]["part_name"] == "word/document.xml"
        assert unit["reference"]["path"] == str(path)
