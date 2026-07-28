# SPDX-License-Identifier: Apache-2.0
"""Stage 3C projection-foundation and real Fixture 2 acceptance."""

from __future__ import annotations

import json
import re
import runpy
import tomllib
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from veqtor_docx._ooxml import (
    DOCUMENT_PART,
    MOVE_REVISION_TAGS,
    ResourceLimitError,
    W_NS,
    canonical_body_flow_v1,
    current_text_atom,
    iter_canonical_paragraph_nodes,
    parse_xml,
    pending_text_revisions_rejected_atom,
    w,
)
from veqtor_mcp import records
from veqtor_mcp.contracts import MCP_CONTRACT_SCHEMA_VERSION


ROOT = Path(__file__).parents[1]
SPEC_PATH = ROOT / "CLAUSE_HISTORY_V0.4.md"
_XSD_WHITESPACE = frozenset("\t\n\r ")


def _spec() -> str:
    return SPEC_PATH.read_text(encoding="utf-8")


def _has_non_whitespace(value: str) -> bool:
    return any(character not in _XSD_WHITESPACE for character in value)


def _projection_text(paragraph: etree._Element) -> str:
    return "".join(
        contribution
        for node in iter_canonical_paragraph_nodes(paragraph)
        if (
            contribution := pending_text_revisions_rejected_atom(
                node,
                boundary=paragraph,
            )
        )
        is not None
    )


def _current_text(paragraph: etree._Element) -> str:
    return "".join(
        contribution
        for node in iter_canonical_paragraph_nodes(paragraph)
        if (contribution := current_text_atom(node, boundary=paragraph)) is not None
    )


def _paragraph(parent: etree._Element, text: str = "") -> etree._Element:
    paragraph = etree.SubElement(parent, w("p"))
    if text:
        _run(paragraph, text)
    return paragraph


def _run(
    parent: etree._Element,
    text: str,
    *,
    deleted: bool = False,
) -> etree._Element:
    run = etree.SubElement(parent, w("r"))
    atom = etree.SubElement(run, w("delText") if deleted else w("t"))
    atom.text = text
    return atom


def _wrapper(
    parent: etree._Element,
    kind: str,
    text: str,
) -> etree._Element:
    wrapper = etree.SubElement(parent, w(kind))
    _run(wrapper, text, deleted=kind == "del")
    return wrapper


def _round_paragraphs(path: Path) -> list[tuple[str, str, bool]]:
    with zipfile.ZipFile(path) as archive:
        document = parse_xml(archive.read(DOCUMENT_PART))
    body = document.find(w("body"))
    assert body is not None

    paragraphs = []
    for item in canonical_body_flow_v1(body).paragraphs:
        current = _current_text(item.element)
        rejected = _projection_text(item.element)
        move_visibility_applied = any(
            node.tag in MOVE_REVISION_TAGS
            for node in iter_canonical_paragraph_nodes(item.element)
        )
        paragraphs.append((current, rejected, move_visibility_applied))
    return paragraphs


def _classify_adjacent_pair(
    left: list[tuple[str, str, bool]],
    right: list[tuple[str, str, bool]],
) -> tuple[dict[str, int], int]:
    counts = {
        "unique_current": 0,
        "ambiguous_current": 0,
        "unique_rejected": 0,
        "unmatched": 0,
    }
    rejected_matches_with_move_visibility = 0
    left_current = [
        current for current, _, _ in left if _has_non_whitespace(current)
    ]

    for current, rejected, move_visibility_applied in right:
        if not _has_non_whitespace(current):
            continue
        current_count = sum(candidate == current for candidate in left_current)
        if current_count == 1:
            counts["unique_current"] += 1
            continue
        if current_count > 1:
            counts["ambiguous_current"] += 1
            continue

        rejected_eligible = (
            bool(rejected)
            and _has_non_whitespace(rejected)
            and rejected != current
        )
        rejected_count = (
            sum(candidate == rejected for candidate in left_current)
            if rejected_eligible
            else 0
        )
        if rejected_count == 1:
            counts["unique_rejected"] += 1
            rejected_matches_with_move_visibility += int(move_visibility_applied)
        else:
            counts["unmatched"] += 1

    return counts, rejected_matches_with_move_visibility


def test_development_identity_packages_the_frozen_spec_without_widening_v03() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    release = runpy.run_path(str(ROOT / "scripts" / "release_contract.py"))
    sdist_includes = project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]

    assert project["project"]["version"] == "0.4.0.dev0"
    assert "/CLAUSE_HISTORY_V0.4.md" in sdist_includes
    assert release["VERSION"] == "0.3.0"
    assert "CLAUSE_HISTORY_V0.4.md" not in release["PUBLIC_DOCUMENT_FILES"]
    assert "CLAUSE_HISTORY_V0.4.md" not in release["SDIST_GIT_FILES"]
    assert MCP_CONTRACT_SCHEMA_VERSION == "veqtor.mcp.v0.3"
    assert len(records.WRITABLE_TOOL_NAMES) == 8
    assert "trace_paragraph_history" not in records.WRITABLE_TOOL_NAMES


def test_frozen_clause_history_spec_has_closed_projection_fixture_contract() -> None:
    spec = _spec()
    acceptance = spec.split("## Acceptance fixtures for later implementation", 1)[
        1
    ].split("\n## ", 1)[0]
    fixture_numbers = [
        int(match.group(1))
        for match in re.finditer(r"(?m)^(\d+)\. ", acceptance)
    ]

    assert fixture_numbers == list(range(1, 23))
    assert "`pending_text_revisions_rejected_v1`" in spec
    assert "insertion-like ancestor (`w:ins` or\n`w:moveTo`) always wins" in spec
    assert (
        "Both modes are\nposition-only and return `chronology_verified: false`"
        in spec
    )
    assert "| R1 → R2 | 59 | 2 | 4 | 2 |" in acceptance
    assert "| R2 → R3 | 62 | 2 | 3 | 1 |" in acceptance
    assert "| R3 → R4 | 65 | 2 | 1 | 0 |" in acceptance

    json_blocks = re.findall(r"```json\n(.*?)\n```", spec, re.DOTALL)
    assert json_blocks
    for block in json_blocks:
        assert isinstance(json.loads(block), dict)


def test_rejected_pending_atom_applies_the_closed_literal_visibility_table() -> None:
    root = etree.Element(w("document"), nsmap={"w": W_NS})
    body = etree.SubElement(root, w("body"))
    paragraph = _paragraph(body, "plain")
    _wrapper(paragraph, "ins", " INS")
    _wrapper(paragraph, "del", " DELETE")
    _wrapper(paragraph, "moveTo", " MOVE-TO")
    _wrapper(paragraph, "moveFrom", " MOVE-FROM")

    atoms = etree.SubElement(paragraph, w("r"))
    etree.SubElement(atoms, w("tab"))
    text = etree.SubElement(atoms, w("t"))
    text.text = "TAB"
    etree.SubElement(atoms, w("br"))
    text = etree.SubElement(atoms, w("t"))
    text.text = "BREAK"
    etree.SubElement(atoms, w("cr"))
    text = etree.SubElement(atoms, w("t"))
    text.text = "CR"
    etree.SubElement(atoms, w("noBreakHyphen"))
    text = etree.SubElement(atoms, w("t"))
    text.text = "HYPHEN"

    deletion = etree.SubElement(paragraph, w("del"))
    insertion = etree.SubElement(deletion, w("ins"))
    _run(insertion, " MIXED")
    insertion = etree.SubElement(paragraph, w("ins"))
    deletion = etree.SubElement(insertion, w("del"))
    _run(deletion, " MIXED-DELETE", deleted=True)
    _run(paragraph, " STRAY", deleted=True)

    assert _projection_text(paragraph) == (
        "plain DELETE MOVE-FROM\tTAB\nBREAK\nCR-HYPHEN"
    )


def test_rejected_pending_atom_refuses_combined_revision_depth_three() -> None:
    paragraph = etree.Element(w("p"), nsmap={"w": W_NS})
    outer = etree.SubElement(paragraph, w("moveFrom"))
    middle = etree.SubElement(outer, w("del"))
    inner = etree.SubElement(middle, w("moveTo"))
    _run(inner, "too deep")

    with pytest.raises(ResourceLimitError) as error:
        _projection_text(paragraph)

    assert error.value.metadata == {
        "limit": "revision_nesting_depth",
        "allowed_count": 2,
        "observed_count": 3,
    }


def test_real_fixture_2_adjacent_pair_classifier_matches_frozen_counts(
    demo_dir: Path,
) -> None:
    rounds = sorted(demo_dir.glob("*.docx"))
    expected = [
        {
            "unique_current": 59,
            "ambiguous_current": 2,
            "unique_rejected": 4,
            "unmatched": 2,
        },
        {
            "unique_current": 62,
            "ambiguous_current": 2,
            "unique_rejected": 3,
            "unmatched": 1,
        },
        {
            "unique_current": 65,
            "ambiguous_current": 2,
            "unique_rejected": 1,
            "unmatched": 0,
        },
    ]

    observed = []
    move_visibility_counts = []
    for left_path, right_path in zip(rounds, rounds[1:]):
        counts, move_visibility_count = _classify_adjacent_pair(
            _round_paragraphs(left_path),
            _round_paragraphs(right_path),
        )
        observed.append(counts)
        move_visibility_counts.append(move_visibility_count)

    assert observed == expected
    assert move_visibility_counts == [0, 2, 0]
