# SPDX-License-Identifier: Apache-2.0
"""extract_redlines against the synthetic rounds: every fact must be exact."""

import json
import subprocess
import sys
import zipfile
from pathlib import Path

from lxml import etree

from veqtor_docx import extract_redlines, verify_quote
from veqtor_docx._ooxml import parse_xml, w
from veqtor_docx.contracts import (
    EXTRACT_REVISION_CATEGORIES_V1,
    TEXT_REVISION_SUFFIX_BY_NAME_V1,
)
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
    EXPRESS_ROW_FEE,
    EXPRESS_ROW_LABEL,
)


def _round(demo_dir: Path, number: int) -> dict:
    path = sorted(demo_dir.glob("*.docx"))[number - 1]
    return extract_redlines(str(path))


def _rewrite_document(source: Path, target: Path, mutate) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as output:
        for info in original.infolist():
            payload = original.read(info)
            if info.filename == "word/document.xml":
                document = parse_xml(payload)
                mutate(document)
                payload = (
                    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
                    + etree.tostring(document)
                )
            output.writestr(info, payload)


def _run(parent: etree._Element, text: str) -> etree._Element:
    run = etree.SubElement(parent, w("r"))
    node = etree.SubElement(run, w("t"))
    node.text = text
    return run


def _insertion(
    parent: etree._Element, revision_id: str, text: str
) -> etree._Element:
    insertion = etree.SubElement(parent, w("ins"))
    insertion.set(w("id"), revision_id)
    insertion.set(w("author"), "Context Test")
    _run(insertion, text)
    return insertion


def _revision_with_no_break_hyphen(
    parent: etree._Element,
    kind: str,
    revision_id: str,
    left: str,
    right: str,
) -> etree._Element:
    revision = etree.SubElement(parent, w(kind))
    revision.set(w("id"), revision_id)
    revision.set(w("author"), "Context Test")
    run = etree.SubElement(revision, w("r"))
    text_tag = w("t") if kind == "ins" else w("delText")
    first = etree.SubElement(run, text_tag)
    first.text = left
    etree.SubElement(run, w("noBreakHyphen"))
    second = etree.SubElement(run, text_tag)
    second.text = right
    return revision


def test_package_import_does_not_require_optional_lzma_extension() -> None:
    script = """
import importlib.abc
import sys

class BlockLzma(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"lzma", "_lzma"}:
            raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None

sys.meta_path.insert(0, BlockLzma())
sys.modules.pop("lzma", None)
sys.modules.pop("_lzma", None)
sys.modules.pop("zipfile", None)
import veqtor_docx
import zipfile
assert zipfile.lzma is None
assert callable(veqtor_docx.extract_redlines)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_extract_revision_categories_v1_are_frozen() -> None:
    assert dict(TEXT_REVISION_SUFFIX_BY_NAME_V1) == {
        "ins": "Ins",
        "del": "Del",
    }
    assert EXTRACT_REVISION_CATEGORIES_V1 == {
        "moveFrom",
        "moveTo",
        "rPrChange",
        "pPrChange",
        "tblPrChange",
        "trPrChange",
        "tcPrChange",
        "sectPrChange",
        "numberingChange",
        "cellIns",
        "cellDel",
        "paragraphMarkIns",
        "paragraphMarkDel",
        "trPrIns",
        "trPrDel",
        "tcPrIns",
        "tcPrDel",
        "tblPrIns",
        "tblPrDel",
        "sectPrIns",
        "sectPrDel",
    }


def test_clean_round_has_no_changes(demo_dir: Path) -> None:
    result = _round(demo_dir, 1)
    assert result["change_units"] == []
    assert result["revision_count"] == 0
    assert result["unsupported_revisions"] == {}


def test_round2_units(demo_dir: Path) -> None:
    result = _round(demo_dir, 2)
    units = result["change_units"]
    assert [u["change_type"] for u in units] == [
        "replace",
        "insert",
        "insert",
        "delete",
        "insert",
        "replace",
    ]
    assert {u["author"] for u in units} == {AUTHOR_CP}
    assert {u["date"] for u in units} == {"2026-05-05T09:30:00Z"}

    table_cell, row_label, row_fee, audit, adviser, cap = units
    for unit in units:
        context = unit["paragraph_context"]
        assert len(context["before"]) <= 240
        assert len(context["after"]) <= 240
        assert type(context["truncated_before"]) is bool
        assert type(context["truncated_after"]) is bool
        assert type(unit["reference"]["paragraph_index"]) is int
        assert type(unit["reference"]["group_index"]) is int
    # Tracked change inside a table cell is anchored to the numbered subclause.
    assert (table_cell["old_text"], table_cell["new_text"]) == ("50", "65")
    assert table_cell["clause_anchor"] == {"label": "3.3", "heading": "Cancellation Charges"}

    # An inserted table row yields insert units for its cell content and a
    # structural trPrIns fact (asserted below).
    assert row_label["new_text"] == EXPRESS_ROW_LABEL
    assert row_fee["new_text"] == EXPRESS_ROW_FEE
    assert row_label["clause_anchor"]["label"] == "3.3"

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

    # Formatting-only and structural revisions are reported, never dropped:
    # the row insertion marker and the inserted paragraph marks of its cells.
    assert result["unsupported_revisions"] == {
        "rPrChange": 1,
        "trPrIns": 1,
        "paragraphMarkIns": 2,
    }
    assert result["revision_count"] == 12


def test_round3_units(demo_dir: Path) -> None:
    result = _round(demo_dir, 3)
    units = result["change_units"]
    assert [u["change_type"] for u in units] == ["insert", "replace", "insert"]
    assert {u["author"] for u in units} == {AUTHOR_US}

    compelled, cap, carveout = units
    # A manually numbered inserted clause anchors to itself.
    assert compelled["clause_anchor"] == {"label": "9.5", "heading": "Compelled Disclosure"}
    assert compelled["new_text"].startswith("9.5 Compelled Disclosure.")
    assert compelled["paragraph_context"] == {
        "before": "",
        "after": "",
        "manual_label": "9.5",
        "truncated_before": False,
        "truncated_after": False,
    }

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


def test_manual_paragraph_label_is_distinct_from_heading_anchor(
    demo_dir: Path,
) -> None:
    result = _round(demo_dir, 3)
    compelled = result["change_units"][0]

    assert compelled["paragraph_context"]["manual_label"] == "9.5"
    assert compelled["clause_anchor"] == {
        "label": "9.5",
        "heading": "Compelled Disclosure",
    }
    assert all(
        unit["paragraph_context"]["manual_label"] is None
        for unit in result["change_units"][1:]
    )


def test_repeated_insertions_use_element_offsets_not_text_search(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-2-counterparty-redline.docx"
    target = tmp_path / "repeated-context.docx"

    def add_repeated_paragraph(document: etree._Element) -> None:
        body = document.find(w("body"))
        assert body is not None
        paragraph = etree.Element(w("p"))
        _run(paragraph, "Prefix ")
        _insertion(paragraph, "900", "SAME")
        _run(paragraph, " middle ")
        _insertion(paragraph, "901", "SAME")
        _run(paragraph, " suffix")
        body.insert(max(0, len(body) - 1), paragraph)

    _rewrite_document(source, target, add_repeated_paragraph)
    extracted = extract_redlines(str(target))
    first = next(
        unit
        for unit in extracted["change_units"]
        if unit["reference"]["revision_ids"] == ["900"]
    )
    second = next(
        unit
        for unit in extracted["change_units"]
        if unit["reference"]["revision_ids"] == ["901"]
    )

    assert first["paragraph_context"]["before"] == "Prefix "
    assert first["paragraph_context"]["after"] == " middle SAME suffix"
    assert second["paragraph_context"]["before"] == "Prefix SAME middle "
    assert second["paragraph_context"]["after"] == " suffix"


def test_tabs_and_breaks_share_one_reading_and_offset_model(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-2-counterparty-redline.docx"
    target = tmp_path / "structural-whitespace-context.docx"

    def add_structural_whitespace(document: etree._Element) -> None:
        body = document.find(w("body"))
        assert body is not None

        tabbed = etree.Element(w("p"))
        run = _run(tabbed, "5.2")
        etree.SubElement(run, w("tab"))
        text = etree.SubElement(run, w("t"))
        text.text = "Payment"
        etree.SubElement(run, w("tab"))
        tail = etree.SubElement(run, w("t"))
        tail.text = "before "
        _insertion(tabbed, "902", "CHANGED")
        _run(tabbed, " suffix")

        broken = etree.Element(w("p"))
        run = _run(broken, "6.3")
        etree.SubElement(run, w("br"))
        text = etree.SubElement(run, w("t"))
        text.text = "Payment "
        _insertion(broken, "903", "UPDATED")

        insert_at = max(0, len(body) - 1)
        body.insert(insert_at, tabbed)
        body.insert(insert_at + 1, broken)

    _rewrite_document(source, target, add_structural_whitespace)
    extracted = extract_redlines(str(target))
    tabbed = next(
        unit
        for unit in extracted["change_units"]
        if unit["reference"]["revision_ids"] == ["902"]
    )
    broken = next(
        unit
        for unit in extracted["change_units"]
        if unit["reference"]["revision_ids"] == ["903"]
    )

    assert tabbed["paragraph_context"] == {
        "before": "5.2\tPayment\tbefore ",
        "after": " suffix",
        "manual_label": "5.2",
        "truncated_before": False,
        "truncated_after": False,
    }
    assert broken["paragraph_context"] == {
        "before": "6.3\nPayment ",
        "after": "",
        "manual_label": "6.3",
        "truncated_before": False,
        "truncated_after": False,
    }


def test_no_break_hyphen_is_shared_by_context_revisions_and_verification(
    demo_dir: Path,
    tmp_path: Path,
) -> None:
    source = demo_dir / "round-2-counterparty-redline.docx"
    target = tmp_path / "no-break-hyphen.docx"

    def add_no_break_hyphens(document: etree._Element) -> None:
        body = document.find(w("body"))
        assert body is not None

        plain = etree.Element(w("p"))
        run = _run(plain, "Alpha")
        etree.SubElement(run, w("noBreakHyphen"))
        tail = etree.SubElement(run, w("t"))
        tail.text = "Beta "
        _insertion(plain, "904", "Changed")
        _run(plain, " suffix")

        inserted = etree.Element(w("p"))
        _run(inserted, "Before ")
        _revision_with_no_break_hyphen(
            inserted, "ins", "905", "Insert", "Value"
        )
        _run(inserted, " after")

        deleted = etree.Element(w("p"))
        _run(deleted, "Before ")
        _revision_with_no_break_hyphen(
            deleted, "del", "906", "Delete", "Value"
        )
        _run(deleted, " after")

        insert_at = max(0, len(body) - 1)
        for offset, paragraph in enumerate((plain, inserted, deleted)):
            body.insert(insert_at + offset, paragraph)

    _rewrite_document(source, target, add_no_break_hyphens)
    extracted = extract_redlines(str(target))
    units = {
        unit["reference"]["revision_ids"][0]: unit
        for unit in extracted["change_units"]
        if unit["reference"]["revision_ids"]
        and unit["reference"]["revision_ids"][0] in {"904", "905", "906"}
    }

    assert units["904"]["paragraph_context"]["before"] == "Alpha-Beta "
    assert units["904"]["paragraph_context"]["after"] == " suffix"
    assert units["905"]["new_text"] == "Insert-Value"
    assert units["905"]["paragraph_context"]["before"] == "Before "
    assert units["905"]["paragraph_context"]["after"] == " after"
    assert units["906"]["old_text"] == "Delete-Value"
    assert units["906"]["paragraph_context"]["before"] == "Before "
    assert units["906"]["paragraph_context"]["after"] == " after"

    for revision_id, quote in (("905", "Insert-Value"), ("906", "Delete-Value")):
        unit = units[revision_id]
        verified = verify_quote(
            str(target),
            {
                "change_unit_id": unit["change_unit_id"],
                "file_sha256": extracted["file_sha256"],
            },
            quote,
        )
        assert verified["verdict"] == "exact"


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


def test_user_home_paths_are_expanded(demo_dir: Path, monkeypatch) -> None:
    """README tells users to point Claude at ~/veqtor-demo-rounds; a literal
    tilde must work and references must carry the openable expanded path."""
    monkeypatch.setenv("HOME", str(demo_dir.parent))
    tilde_path = f"~/{demo_dir.name}/round-2-counterparty-redline.docx"
    result = extract_redlines(tilde_path)
    assert result["change_units"]
    assert "~" not in result["path"]
    assert "~" not in result["change_units"][0]["reference"]["path"]


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
