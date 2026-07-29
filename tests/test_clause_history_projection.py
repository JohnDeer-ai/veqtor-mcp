# SPDX-License-Identifier: Apache-2.0
"""Stage 3C projection-foundation and real Fixture 2 acceptance."""

from __future__ import annotations

import hashlib
import json
import re
import runpy
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from lxml import etree

from veqtor_docx import __version__ as development_version
from veqtor_docx._ooxml import (
    ArchiveValidationError,
    CanonicalBodyFlow,
    DOCUMENT_PART,
    MOVE_REVISION_TAGS,
    ResourceLimitError,
    W_NS,
    canonical_body_flow_v1,
    current_text_atom,
    iter_canonical_paragraph_nodes,
    parse_xml,
    pending_text_revisions_rejected_text,
    text_atom,
    w,
)
from veqtor_docx._projection import build_paragraph_projection_v1
from veqtor_docx.synthetic import TITLE_SENTENCE
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
    return pending_text_revisions_rejected_text(paragraph)


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


@dataclass(frozen=True)
class _RoundParagraph:
    current: str
    projection: dict[str, object]
    move_wrapper_count: int
    move_text_contributions: tuple[tuple[tuple[str, ...], str], ...]


@dataclass(frozen=True)
class _RejectedMatch:
    right_index: int
    left_index: int
    current: str
    rejected: str
    move_wrapper_count: int
    move_text_contributions: tuple[tuple[tuple[str, ...], str], ...]


def _move_text_contributions(
    paragraph: etree._Element,
) -> tuple[tuple[tuple[str, ...], str], ...]:
    contributions = []
    for node in iter_canonical_paragraph_nodes(paragraph):
        value = text_atom(node, include_deleted_text=True)
        if value is None:
            continue
        move_ancestors = []
        for ancestor in node.iterancestors():
            if ancestor is paragraph:
                break
            if ancestor.tag in MOVE_REVISION_TAGS:
                move_ancestors.append(etree.QName(ancestor).localname)
        if move_ancestors:
            contributions.append((tuple(move_ancestors), value))
    return tuple(contributions)


def _round_paragraphs(
    path: Path,
    *,
    retype_move_text_atoms: bool = False,
) -> list[_RoundParagraph]:
    with zipfile.ZipFile(path) as archive:
        document = parse_xml(archive.read(DOCUMENT_PART))
    if retype_move_text_atoms:
        for wrapper in document.iter():
            if wrapper.tag not in MOVE_REVISION_TAGS:
                continue
            for node in wrapper.iterdescendants():
                if text_atom(node, include_deleted_text=True) is not None:
                    node.tag = w("instrText")
    body = document.find(w("body"))
    assert body is not None

    paragraphs = []
    flow = canonical_body_flow_v1(body)
    for item in flow.paragraphs:
        current = _current_text(item.element)
        projection = build_paragraph_projection_v1(
            item.element,
            flow,
        )
        move_wrappers = tuple(
            node
            for node in iter_canonical_paragraph_nodes(item.element)
            if node.tag in MOVE_REVISION_TAGS
        )
        paragraphs.append(
            _RoundParagraph(
                current=current,
                projection=projection,
                move_wrapper_count=len(move_wrappers),
                move_text_contributions=_move_text_contributions(item.element),
            )
        )
    return paragraphs


def _classify_adjacent_pair(
    left: list[_RoundParagraph],
    right: list[_RoundParagraph],
) -> tuple[dict[str, int], tuple[_RejectedMatch, ...]]:
    counts = {
        "unique_current": 0,
        "ambiguous_current": 0,
        "unique_rejected": 0,
        "unmatched": 0,
    }
    left_current = [
        (index, paragraph.current)
        for index, paragraph in enumerate(left)
        if _has_non_whitespace(paragraph.current)
    ]
    rejected_matches = []

    for right_index, paragraph in enumerate(right):
        current = paragraph.current
        rejected = paragraph.projection["text"]
        if not _has_non_whitespace(current):
            continue
        current_count = sum(candidate == current for _, candidate in left_current)
        if current_count == 1:
            counts["unique_current"] += 1
            continue
        if current_count > 1:
            counts["ambiguous_current"] += 1
            continue

        rejected_eligible = paragraph.projection["match_eligible"] is True
        rejected_indices = (
            [index for index, candidate in left_current if candidate == rejected]
            if rejected_eligible
            else []
        )
        if len(rejected_indices) == 1:
            counts["unique_rejected"] += 1
            rejected_matches.append(
                _RejectedMatch(
                    right_index=right_index,
                    left_index=rejected_indices[0],
                    current=current,
                    rejected=str(rejected),
                    move_wrapper_count=paragraph.move_wrapper_count,
                    move_text_contributions=paragraph.move_text_contributions,
                )
            )
        else:
            counts["unmatched"] += 1

    return counts, tuple(rejected_matches)


def test_development_identity_packages_the_frozen_spec_without_widening_v03() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    release = runpy.run_path(str(ROOT / "scripts" / "release_contract.py"))
    sdist_includes = project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    api = (ROOT / "API.md").read_text(encoding="utf-8")
    limitations = (ROOT / "KNOWN_LIMITATIONS.md").read_text(encoding="utf-8")

    source_version = project["project"]["version"]
    frozen_version = release["VERSION"]
    assert source_version == development_version
    assert "/CLAUSE_HISTORY_V0.4.md" in sdist_includes
    assert frozen_version == "0.3.0"
    assert "CLAUSE_HISTORY_V0.4.md" not in release["PUBLIC_DOCUMENT_FILES"]
    assert "CLAUSE_HISTORY_V0.4.md" not in release["SDIST_GIT_FILES"]
    frozen_mcp_version = ".".join(frozen_version.split(".")[:2])
    assert MCP_CONTRACT_SCHEMA_VERSION == f"veqtor.mcp.v{frozen_mcp_version}"
    assert len(records.WRITABLE_TOOL_NAMES) == 8
    assert "trace_paragraph_history" not in records.WRITABLE_TOOL_NAMES
    assert re.search(
        rf"development source is package `{re.escape(source_version)}`.*frozen\s+"
        rf"eight-tool MCP contract `veqtor\.mcp\.v{re.escape(frozen_mcp_version)}`",
        api,
        re.DOTALL,
    )
    assert "the v0.3 examples and contracts below\nremain unchanged" in api
    assert f"development source `{source_version}`" in limitations
    assert "frozen v0.3 examples and eight-tool MCP contract" in limitations
    assert f'"version": "{frozen_version}"' in api


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


def test_rejected_pending_text_applies_the_closed_literal_visibility_table() -> None:
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


@pytest.mark.parametrize(
    ("depth", "payload"),
    [
        (3, "empty"),
        (3, "unsupported"),
        (3, "text"),
        (4, "text"),
        (8, "text"),
    ],
)
def test_rejected_pending_paragraph_refuses_exact_structural_depth(
    depth: int,
    payload: str,
) -> None:
    paragraph = etree.Element(w("p"), nsmap={"w": W_NS})
    parent = paragraph
    wrapper_kinds = ("moveFrom", "del", "moveTo", "ins")
    for index in range(depth):
        parent = etree.SubElement(parent, w(wrapper_kinds[index % 4]))
    if payload == "unsupported":
        etree.SubElement(parent, w("instrText")).text = "unsupported"
    elif payload == "text":
        _run(parent, "too deep")

    with pytest.raises(ResourceLimitError) as error:
        _projection_text(paragraph)

    assert error.value.metadata == {
        "limit": "revision_nesting_depth",
        "allowed_count": 2,
        "observed_count": depth,
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
    observed_matches = []
    for left_path, right_path in zip(rounds, rounds[1:]):
        counts, rejected_matches = _classify_adjacent_pair(
            _round_paragraphs(left_path),
            _round_paragraphs(right_path),
        )
        observed.append(counts)
        observed_matches.append(rejected_matches)

    assert observed == expected
    assert [
        [(match.right_index, match.left_index) for match in matches]
        for matches in observed_matches
    ] == [
        [(18, 18), (33, 31), (38, 36), (60, 58)],
        [(26, 26), (27, 27), (61, 60)],
        [(61, 61)],
    ]

    delivery = (
        "Contractor shall deliver each Batch FCA (Incoterms 2020) Contractor's "
        "facility in Hamburg, Germany, unless the Work Order states otherwise."
    )
    risk = "Risk in each Batch passes to Client upon handover to the first carrier."
    causal_move_matches = [
        match for match in observed_matches[1] if match.move_text_contributions
    ]

    move_projections = [
        paragraph.projection
        for paragraph in _round_paragraphs(rounds[2])
        if paragraph.move_wrapper_count
    ]
    assert len(move_projections) == 2
    assert all(
        projection["projection_status"] == "complete"
        and projection["move_wrapper_visibility_applied"] is True
        and projection["move_pairing"] == "not_attempted"
        for projection in move_projections
    )
    assert [
        (
            match.right_index,
            match.left_index,
            match.move_wrapper_count,
            match.move_text_contributions,
            match.current,
            match.rejected,
        )
        for match in causal_move_matches
    ] == [
        (
            26,
            26,
            1,
            ((("moveTo",), TITLE_SENTENCE),),
            delivery + TITLE_SENTENCE,
            delivery,
        ),
        (
            27,
            27,
            1,
            ((("moveFrom",), TITLE_SENTENCE),),
            risk,
            risk + TITLE_SENTENCE,
        ),
    ]

    retyped_right = _round_paragraphs(
        rounds[2],
        retype_move_text_atoms=True,
    )
    assert sum(paragraph.move_wrapper_count for paragraph in retyped_right) == 2
    assert all(
        not paragraph.move_text_contributions
        for paragraph in retyped_right
        if paragraph.move_wrapper_count
    )
    _, retyped_matches = _classify_adjacent_pair(
        _round_paragraphs(rounds[1]),
        retyped_right,
    )
    assert [
        (match.right_index, match.left_index) for match in retyped_matches
    ] == [(61, 60)]


def _single_paragraph_flow(
    paragraph_text: str = "",
) -> tuple[etree._Element, etree._Element, CanonicalBodyFlow]:
    document = etree.Element(w("document"), nsmap={"w": W_NS})
    body = etree.SubElement(document, w("body"))
    paragraph = _paragraph(body, paragraph_text)
    return body, paragraph, canonical_body_flow_v1(body)


def test_projection_builder_preserves_all_unavailable_reasons_in_order() -> None:
    body, paragraph, _ = _single_paragraph_flow("current")
    _run(paragraph, " stray", deleted=True)

    properties = etree.Element(w("pPr"))
    run_properties = etree.SubElement(properties, w("rPr"))
    etree.SubElement(run_properties, w("ins"))
    paragraph.insert(0, properties)

    unknown = etree.SubElement(paragraph, w("unknownTextContainer"))
    _run(unknown, " hidden")
    flow = canonical_body_flow_v1(body)

    projection = build_paragraph_projection_v1(paragraph, flow)

    assert projection == {
        "schema_version": "paragraph_projection.v1",
        "mode": "pending_text_revisions_rejected_v1",
        "projection_status": "unavailable",
        "unavailable_reasons": [
            "stray_deleted_text",
            "existence_affecting_revision",
            "declared_scope_incomplete",
        ],
        "text_state": None,
        "equals_current": None,
        "has_non_whitespace": False,
        "match_eligible": False,
        "projection_text_sha256": None,
        "text_length": None,
        "text": None,
        "move_wrapper_visibility_applied": False,
        "move_pairing": "not_attempted",
    }


def test_projection_builder_attributes_structural_revisions_to_owners_only() -> None:
    document = etree.Element(w("document"), nsmap={"w": W_NS})
    body = etree.SubElement(document, w("body"))
    unaffected = _paragraph(body, "outside")

    table = etree.SubElement(body, w("tbl"))
    table_properties = etree.SubElement(table, w("tblPr"))
    etree.SubElement(table_properties, w("ins"))
    row = etree.SubElement(table, w("tr"))
    row_properties = etree.SubElement(row, w("trPr"))
    etree.SubElement(row_properties, w("del"))
    cell = etree.SubElement(row, w("tc"))
    cell_properties = etree.SubElement(cell, w("tcPr"))
    etree.SubElement(cell_properties, w("ins"))
    etree.SubElement(cell_properties, w("cellDel"))
    affected = _paragraph(cell, "inside")
    paragraph_properties = etree.Element(w("pPr"))
    run_properties = etree.SubElement(paragraph_properties, w("rPr"))
    etree.SubElement(run_properties, w("del"))
    section_properties = etree.SubElement(paragraph_properties, w("sectPr"))
    etree.SubElement(section_properties, w("ins"))
    etree.SubElement(paragraph_properties, w("pPrChange"))
    affected.insert(0, paragraph_properties)

    body_section = etree.SubElement(body, w("sectPr"))
    etree.SubElement(body_section, w("del"))
    flow = canonical_body_flow_v1(body)

    assert build_paragraph_projection_v1(unaffected, flow)[
        "projection_status"
    ] == "complete"
    affected_projection = build_paragraph_projection_v1(affected, flow)
    assert affected_projection["projection_status"] == "unavailable"
    assert affected_projection["unavailable_reasons"] == [
        "existence_affecting_revision"
    ]


def test_projection_builder_refuses_an_unattributable_existence_revision() -> None:
    body, paragraph, _ = _single_paragraph_flow("current")
    etree.SubElement(body, w("cellIns"))
    flow = canonical_body_flow_v1(body)

    with pytest.raises(ArchiveValidationError, match="no attributable") as error:
        build_paragraph_projection_v1(paragraph, flow)

    assert error.value.code == "file_unextractable"


def test_projection_builder_refuses_illegal_property_wrapper_placement() -> None:
    body, paragraph, _ = _single_paragraph_flow("current")
    properties = etree.Element(w("pPr"))
    move = etree.SubElement(properties, w("moveTo"))
    _run(move, "hidden")
    paragraph.insert(0, properties)
    flow = canonical_body_flow_v1(body)

    with pytest.raises(ArchiveValidationError, match="illegally placed") as error:
        build_paragraph_projection_v1(paragraph, flow)

    assert error.value.code == "file_unextractable"


def test_projection_builder_keeps_formatting_only_history_text_neutral() -> None:
    body, paragraph, _ = _single_paragraph_flow("literal")
    paragraph_properties = etree.Element(w("pPr"))
    etree.SubElement(paragraph_properties, w("pPrChange"))
    paragraph.insert(0, paragraph_properties)
    run = paragraph.find(w("r"))
    assert run is not None
    run_properties = etree.Element(w("rPr"))
    etree.SubElement(run_properties, w("rPrChange"))
    run.insert(0, run_properties)
    flow = canonical_body_flow_v1(body)

    projection = build_paragraph_projection_v1(paragraph, flow)

    assert projection["projection_status"] == "complete"
    assert projection["text"] == "literal"
    assert projection["unavailable_reasons"] == []


def test_projection_builder_applies_depth_two_insertion_priority() -> None:
    body, paragraph, _ = _single_paragraph_flow("plain")
    deletion = etree.SubElement(paragraph, w("del"))
    insertion = etree.SubElement(deletion, w("moveTo"))
    _run(insertion, " hidden")
    flow = canonical_body_flow_v1(body)

    projection = build_paragraph_projection_v1(paragraph, flow)

    assert projection["projection_status"] == "complete"
    assert projection["text"] == "plain"
    assert projection["equals_current"] is True
    assert projection["move_wrapper_visibility_applied"] is True
    assert projection["move_pairing"] == "not_attempted"


@pytest.mark.parametrize(
    ("kind", "expected_state", "expected_non_whitespace"),
    [
        ("empty", "empty", False),
        ("whitespace", "nonempty", False),
    ],
)
def test_projection_builder_returns_ineligible_empty_and_whitespace_observations(
    kind: str,
    expected_state: str,
    expected_non_whitespace: bool,
) -> None:
    body, paragraph, _ = _single_paragraph_flow()
    if kind == "empty":
        _wrapper(paragraph, "ins", "new")
    else:
        _wrapper(paragraph, "del", " \t\n\r")
    flow = canonical_body_flow_v1(body)

    projection = build_paragraph_projection_v1(paragraph, flow)

    assert projection["projection_status"] == "complete"
    assert projection["text_state"] == expected_state
    assert projection["has_non_whitespace"] is expected_non_whitespace
    assert projection["match_eligible"] is False


def test_projection_builder_marks_flattened_equal_text_ineligible() -> None:
    body, paragraph, flow = _single_paragraph_flow("flattened wording")

    projection = build_paragraph_projection_v1(paragraph, flow)

    assert projection["projection_status"] == "complete"
    assert projection["text_state"] == "nonempty"
    assert projection["equals_current"] is True
    assert projection["match_eligible"] is False


def test_projection_builder_preserves_unicode_length_and_exact_utf8_hash() -> None:
    text = "Ответственность сторон — 東京 😀⚖️"
    body, paragraph, flow = _single_paragraph_flow(text)

    projection = build_paragraph_projection_v1(paragraph, flow)

    assert projection["text"] == text
    assert projection["text_length"] == len(text)
    assert projection["projection_text_sha256"] == hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def test_projection_builder_accepts_exactly_50000_rejected_unicode_scalars() -> None:
    text = "😀" * 50_000
    body, paragraph, _ = _single_paragraph_flow()
    _wrapper(paragraph, "del", text)
    flow = canonical_body_flow_v1(body)

    projection = build_paragraph_projection_v1(paragraph, flow)

    assert projection["text_length"] == 50_000
    assert projection["text"] == text
    assert projection["projection_text_sha256"] == hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def test_projection_builder_refuses_50001_without_truncation() -> None:
    body, paragraph, _ = _single_paragraph_flow()
    _wrapper(paragraph, "del", "я" * 50_001)
    flow = canonical_body_flow_v1(body)

    with pytest.raises(ResourceLimitError) as error:
        build_paragraph_projection_v1(paragraph, flow)

    assert error.value.metadata == {
        "limit": "rejected_projection_chars_per_paragraph",
        "allowed_count": 50_000,
        "observed_count": 50_001,
    }


def test_projection_builder_enforces_independent_current_text_boundary() -> None:
    body, paragraph, flow = _single_paragraph_flow("a" * 50_000)
    assert build_paragraph_projection_v1(paragraph, flow)["text_length"] == 50_000

    _run(paragraph, "b")
    flow = canonical_body_flow_v1(body)
    with pytest.raises(ResourceLimitError) as error:
        build_paragraph_projection_v1(paragraph, flow)

    assert error.value.metadata == {
        "limit": "accepted_current_chars_per_paragraph",
        "allowed_count": 50_000,
        "observed_count": 50_001,
    }
