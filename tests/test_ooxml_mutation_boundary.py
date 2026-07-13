# SPDX-License-Identifier: Apache-2.0
"""Adversarial, parseable OOXML must stay inside the total edit boundary."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from lxml import etree

from veqtor_docx import (
    DocxError,
    apply_edits,
    extract_redlines,
    list_rounds,
    preflight_edits,
    verify_quote,
)
from veqtor_docx._ooxml import parse_xml, run_text, w
from veqtor_docx.apply import DEFAULT_AUTHOR, MAX_REVISION_ID


TAIL_OLD = " in respect of all claims in aggregate."
TAIL_NEW = " in respect of all claims arising in any Contract Year."


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


def _cap_unit(path: Path) -> tuple[dict, dict]:
    extracted = extract_redlines(str(path))
    unit = next(
        item
        for item in extracted["change_units"]
        if item["clause_anchor"] and item["clause_anchor"]["label"] == "14.2"
    )
    anchor = {
        "change_unit_id": unit["change_unit_id"],
        "file_sha256": extracted["file_sha256"],
    }
    return unit, anchor


def _duplicate_id(document: etree._Element) -> None:
    wrappers = [
        node for node in document.iter() if node.tag in {w("ins"), w("del")}
    ]
    wrappers[0].set(w("id"), wrappers[-1].get(w("id")))
    paragraph = next(wrappers[0].iterancestors(w("p")))
    run = etree.SubElement(paragraph, w("r"))
    etree.SubElement(run, w("t")).text = " WRONG ANCHOR TOKEN"


def _swap_ids(document: etree._Element) -> None:
    wrappers = [
        node for node in document.iter() if node.tag in {w("ins"), w("del")}
    ]
    first_id = wrappers[0].get(w("id"))
    last_id = wrappers[-1].get(w("id"))
    wrappers[0].set(w("id"), last_id)
    wrappers[-1].set(w("id"), first_id)


def _oversized_id(document: etree._Element) -> None:
    next(document.iter(w("ins"))).set(w("id"), "9" * 4_300)


def _revision_id_max_minus_two(document: etree._Element) -> None:
    next(document.iter(w("ins"))).set(w("id"), str(MAX_REVISION_ID - 2))


def _revision_id_max_minus_one(document: etree._Element) -> None:
    next(document.iter(w("ins"))).set(w("id"), str(MAX_REVISION_ID - 1))


def _revision_id_max(document: etree._Element) -> None:
    next(document.iter(w("ins"))).set(w("id"), str(MAX_REVISION_ID))


def _runless_text(document: etree._Element) -> None:
    insertion = next(document.iter(w("ins")))
    value = run_text(insertion)
    for child in list(insertion):
        insertion.remove(child)
    etree.SubElement(insertion, w("t")).text = value


@pytest.mark.parametrize(
    "mutation",
    [
        _duplicate_id,
        _swap_ids,
        _oversized_id,
        _revision_id_max_minus_two,
        _revision_id_max_minus_one,
        _revision_id_max,
        _runless_text,
    ],
    ids=[
        "duplicate-id",
        "moved-ids",
        "oversized-id",
        "revision-id-max-minus-two",
        "revision-id-max-minus-one",
        "revision-id-max",
        "runless-text",
    ],
)
def test_parseable_ooxml_mutations_have_a_total_positional_boundary(
    demo_dir: Path,
    tmp_path: Path,
    mutation,
) -> None:
    source = demo_dir / "round-2-counterparty-redline.docx"
    matter = tmp_path / mutation.__name__
    matter.mkdir()
    mutated = matter / "round-2-mutated.docx"
    output = matter / "round-3-output.docx"
    _rewrite_document(source, mutated, mutation)

    # All five public document operations are exercised.  A raw exception
    # from any one of them fails this test automatically.
    rounds = list_rounds(str(matter))
    assert len(rounds["rounds"]) == 1
    unit, anchor = _cap_unit(mutated)
    quote = unit["new_text"] or unit["old_text"]
    verified = verify_quote(str(mutated), anchor, quote)
    assert verified["verdict"] == "exact"

    edits = [
        {
            "anchor": anchor,
            "delete_text": TAIL_OLD,
            "insert_text": TAIL_NEW,
        }
    ]
    preflight = preflight_edits(str(mutated), edits)
    assert type(preflight["batch_applicable"]) is bool

    try:
        applied = apply_edits(str(mutated), str(output), edits)
    except DocxError:
        assert preflight["batch_applicable"] is False
        assert not output.exists()
        return

    assert preflight["batch_applicable"] is True
    assert applied["round_trip_check"]["status"] == "passed"
    after = extract_redlines(str(output))
    created = [
        item for item in after["change_units"] if item["author"] == DEFAULT_AUTHOR
    ]
    assert len(created) == 1
    assert created[0]["reference"]["paragraph_index"] == unit["reference"][
        "paragraph_index"
    ]
    assert created[0]["clause_anchor"]["label"] == "14.2"
    assert created[0]["old_text"] == TAIL_OLD
    assert created[0]["new_text"] == TAIL_NEW
    created_ids = created[0]["reference"]["revision_ids"]
    assert len(created_ids) == len(set(created_ids))
    assert all(
        revision_id.isascii()
        and revision_id.isdecimal()
        and int(revision_id) <= MAX_REVISION_ID
        for revision_id in created_ids
    )
