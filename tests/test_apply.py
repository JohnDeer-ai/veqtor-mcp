# SPDX-License-Identifier: Apache-2.0
"""apply_edits: fail-closed application of tracked edits with round-trip proof."""

import hashlib
import importlib
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from veqtor_docx import (
    ApplyError,
    DocxError,
    apply_edits,
    extract_redlines,
    preflight_edits,
)
from veqtor_docx._ooxml import parse_xml, w
from veqtor_docx.apply import DEFAULT_AUTHOR, _collateral_outside
from veqtor_docx.contracts import ROUND_TRIP_COMPARISON_CURRENT

TAIL_OLD = " in respect of all claims in aggregate."
TAIL_NEW = " in respect of all claims arising in any Contract Year."
PREFLIGHT_DIAGNOSTIC_KEYS = {
    "edit_index",
    "change_unit_id",
    "status",
    "operation",
    "match_count",
    "target_author",
    "target_revision_ids",
    "position_supported",
    "refusal_code",
}


@pytest.fixture
def round2(demo_dir: Path) -> Path:
    return demo_dir / "round-2-counterparty-redline.docx"


def _cap_anchor(path: Path) -> dict:
    result = extract_redlines(str(path))
    unit = next(
        u
        for u in result["change_units"]
        if u["clause_anchor"] and u["clause_anchor"]["label"] == "14.2"
    )
    return {"change_unit_id": unit["change_unit_id"], "file_sha256": result["file_sha256"]}


def _edit(anchor: dict, delete_text: str = TAIL_OLD, insert_text: str = TAIL_NEW) -> dict:
    return {"anchor": anchor, "delete_text": delete_text, "insert_text": insert_text}


def _rewrite_document_xml(source: Path, output: Path, mutate) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as target:
        for info in original.infolist():
            payload = original.read(info)
            if info.filename == "word/document.xml":
                document = parse_xml(payload)
                mutate(document)
                payload = (
                    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
                    + etree.tostring(document)
                )
            target.writestr(info, payload)


def _atom_run(parent: etree._Element, text_tag: str, atom_tag: str) -> None:
    run = etree.SubElement(parent, w("r"))
    before = etree.SubElement(run, w(text_tag))
    before.text = "Atom"
    etree.SubElement(run, w(atom_tag))
    after = etree.SubElement(run, w(text_tag))
    after.text = "Value"


def _atom_revision_docx(
    round2: Path,
    output: Path,
    *,
    kind: str,
    atom_tag: str,
) -> None:
    def mutate(document: etree._Element) -> None:
        if kind == "ins":
            wrapper = next(
                element
                for element in document.iter(w("ins"))
                if "USD 50,000"
                in "".join(node.text or "" for node in element.iter(w("t")))
            )
            text_tag = "t"
        else:
            wrapper = next(
                element
                for element in document.iter(w("del"))
                if element.getparent() is not None
                and element.getparent().tag == w("p")
                and next(element.iter(w("delText")), None) is not None
            )
            text_tag = "delText"
        for child in list(wrapper):
            wrapper.remove(child)
        _atom_run(wrapper, text_tag, atom_tag)

    _rewrite_document_xml(round2, output, mutate)


def _runless_atom_revision_docx(
    round2: Path,
    output: Path,
    *,
    atom_tag: str,
) -> None:
    def mutate(document: etree._Element) -> None:
        wrapper = next(
            element
            for element in document.iter(w("ins"))
            if "USD 50,000"
            in "".join(node.text or "" for node in element.iter(w("t")))
        )
        for child in list(wrapper):
            wrapper.remove(child)
        before = etree.SubElement(wrapper, w("t"))
        before.text = "Direct"
        atom = etree.SubElement(wrapper, w(atom_tag))
        if atom_tag == "t":
            atom.text = "Text"
        after = etree.SubElement(wrapper, w("t"))
        after.text = "Value"

    _rewrite_document_xml(round2, output, mutate)


def _runless_plain_atom_docx(
    round2: Path,
    output: Path,
    *,
    atom_tag: str,
) -> None:
    anchor = _cap_anchor(round2)
    extracted = extract_redlines(str(round2))
    unit = next(
        item
        for item in extracted["change_units"]
        if item["change_unit_id"] == anchor["change_unit_id"]
    )
    revision_ids = set(unit["reference"]["revision_ids"])

    def mutate(document: etree._Element) -> None:
        wrapper = next(
            element
            for element in document.iter()
            if element.get(w("id")) in revision_ids
        )
        paragraph = next(wrapper.iterancestors(w("p")))
        before = etree.SubElement(paragraph, w("t"))
        before.text = "Direct"
        atom = etree.SubElement(paragraph, w(atom_tag))
        if atom_tag == "t":
            atom.text = "Text"
        after = etree.SubElement(paragraph, w("t"))
        after.text = "Value"

    _rewrite_document_xml(round2, output, mutate)


def _runless_deletion_atom_docx(
    round2: Path,
    output: Path,
    *,
    atom_tag: str,
) -> None:
    def mutate(document: etree._Element) -> None:
        wrapper = next(
            element
            for element in document.iter(w("del"))
            if element.getparent() is not None
            and element.getparent().tag == w("p")
            and next(element.iter(w("delText")), None) is not None
        )
        for child in list(wrapper):
            wrapper.remove(child)
        before = etree.SubElement(wrapper, w("delText"))
        before.text = "Direct"
        atom = etree.SubElement(wrapper, w(atom_tag))
        if atom_tag == "delText":
            atom.text = "Text"
        after = etree.SubElement(wrapper, w("delText"))
        after.text = "Value"

    _rewrite_document_xml(round2, output, mutate)


def test_replace_at_anchor_with_round_trip(round2: Path, tmp_path: Path) -> None:
    source_bytes = round2.read_bytes()
    out = tmp_path / "counter.docx"
    result = apply_edits(str(round2), str(out), [_edit(_cap_anchor(round2))])

    assert result["status"] == "ok"
    assert result["output_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert result["round_trip_check"]["status"] == "passed"
    assert result["round_trip_check"]["collateral_changes"] == []
    assert result["round_trip_check"]["comparison"] == ROUND_TRIP_COMPARISON_CURRENT
    assert not list(tmp_path.glob("*.veqtor-tmp"))
    assert round2.read_bytes() == source_bytes  # source untouched

    before = extract_redlines(str(round2))
    after = extract_redlines(str(out))
    strip = lambda u: (u["change_type"], u["author"], u["old_text"], u["new_text"])
    assert [strip(u) for u in before["change_units"]] == [
        strip(u) for u in after["change_units"] if u["author"] != DEFAULT_AUTHOR
    ]
    mine = [u for u in after["change_units"] if u["author"] == DEFAULT_AUTHOR]
    assert len(mine) == 1
    assert mine[0]["change_type"] == "replace"
    assert mine[0]["old_text"] == TAIL_OLD
    assert mine[0]["new_text"] == TAIL_NEW
    assert mine[0]["clause_anchor"] == {
        "label": "14.2",
        "heading": "Limitation of Liability",
    }
    assert mine[0]["date"] is None  # no fabricated timestamps
    assert [str(i) for i in result["applied"][0]["tracked_revision_ids"]] == list(
        mine[0]["reference"]["revision_ids"]
    )


def _duplicate_cap_id_in_earlier_clause(round2: Path, output: Path) -> None:
    extracted = extract_redlines(str(round2))
    cap = next(
        unit
        for unit in extracted["change_units"]
        if unit["clause_anchor"] and unit["clause_anchor"]["label"] == "14.2"
    )
    earlier = extracted["change_units"][0]
    duplicate_id = cap["reference"]["revision_ids"][0]

    def mutate(document: etree._Element) -> None:
        paragraphs = list(document.iter(w("p")))
        earlier_para = paragraphs[earlier["reference"]["paragraph_index"]]
        earlier_group = next(
            element
            for element in earlier_para.iter()
            if element.tag in {w("ins"), w("del")}
        )
        earlier_group.set(w("id"), duplicate_id)
        run = etree.Element(w("r"))
        text = etree.SubElement(run, w("t"))
        text.text = " WRONG ANCHOR TOKEN"
        earlier_para.append(run)

    _rewrite_document_xml(round2, output, mutate)


def test_duplicate_revision_id_cannot_redirect_anchor(
    round2: Path, tmp_path: Path
) -> None:
    source = tmp_path / "duplicate-revision-id.docx"
    _duplicate_cap_id_in_earlier_clause(round2, source)
    extracted = extract_redlines(str(source))
    cap = next(
        unit
        for unit in extracted["change_units"]
        if unit["clause_anchor"] and unit["clause_anchor"]["label"] == "14.2"
    )
    anchor = {
        "change_unit_id": cap["change_unit_id"],
        "file_sha256": extracted["file_sha256"],
    }

    refusal = preflight_edits(
        str(source),
        [_edit(anchor, delete_text="WRONG ANCHOR TOKEN", insert_text="WRONG")],
    )

    assert refusal["batch_applicable"] is False
    assert refusal["refusal_code"] == "delete_text_not_found"
    assert refusal["edits"][0]["match_count"] == 0


def test_duplicate_revision_id_still_edits_the_structural_anchor(
    round2: Path, tmp_path: Path
) -> None:
    source = tmp_path / "duplicate-revision-id.docx"
    output = tmp_path / "correct-clause.docx"
    _duplicate_cap_id_in_earlier_clause(round2, source)
    before = extract_redlines(str(source))
    cap = next(
        unit
        for unit in before["change_units"]
        if unit["clause_anchor"] and unit["clause_anchor"]["label"] == "14.2"
    )
    anchor = {
        "change_unit_id": cap["change_unit_id"],
        "file_sha256": before["file_sha256"],
    }

    apply_edits(str(source), str(output), [_edit(anchor)])
    after = extract_redlines(str(output))
    created = next(
        unit
        for unit in after["change_units"]
        if unit["author"] == DEFAULT_AUTHOR
    )

    assert created["reference"]["paragraph_index"] == cap["reference"][
        "paragraph_index"
    ]
    assert created["clause_anchor"]["label"] == "14.2"


@pytest.mark.parametrize(
    ("revision_id", "expected_code"),
    [
        ("9" * 4_300, "revision_id_unsupported"),
        ("0" * 10 + "1", "revision_id_unsupported"),
        ("2147483648", "revision_id_unsupported"),
        ("2147483647", "revision_id_exhausted"),
        ("+101", "revision_id_unsupported"),
        ("١٠١", "revision_id_unsupported"),
    ],
)
def test_revision_id_boundary_is_controlled(
    round2: Path,
    tmp_path: Path,
    revision_id: str,
    expected_code: str,
) -> None:
    source = tmp_path / "revision-id-boundary.docx"

    def mutate(document: etree._Element) -> None:
        wrapper = next(document.iter(w("ins")))
        wrapper.set(w("id"), revision_id)

    _rewrite_document_xml(round2, source, mutate)
    extracted = extract_redlines(str(source))
    cap = next(
        unit
        for unit in extracted["change_units"]
        if unit["clause_anchor"] and unit["clause_anchor"]["label"] == "14.2"
    )
    result = preflight_edits(
        str(source),
        [
            _edit(
                {
                    "change_unit_id": cap["change_unit_id"],
                    "file_sha256": extracted["file_sha256"],
                }
            )
        ],
    )

    assert result["batch_applicable"] is False
    assert result["refusal_code"] == expected_code
    assert result["failure_phase"] == (
        "planning" if expected_code == "revision_id_exhausted" else "source"
    )


def test_ten_digit_leading_zero_revision_id_is_supported(
    round2: Path, tmp_path: Path
) -> None:
    source = tmp_path / "leading-zero-id.docx"
    output = tmp_path / "leading-zero-output.docx"

    def mutate(document: etree._Element) -> None:
        next(document.iter(w("ins"))).set(w("id"), "0000000001")

    _rewrite_document_xml(round2, source, mutate)

    result = apply_edits(
        str(source),
        str(output),
        [_edit(_cap_anchor(source), insert_text="")],
    )

    assert result["status"] == "ok"
    assert output.exists()


def _with_highest_revision_id(
    source: Path,
    output: Path,
    highest: int,
) -> None:
    def mutate(document: etree._Element) -> None:
        next(document.iter(w("ins"))).set(w("id"), str(highest))

    _rewrite_document_xml(source, output, mutate)


def test_last_revision_id_supports_one_id_edit(
    round2: Path, tmp_path: Path
) -> None:
    source = tmp_path / "one-id-capacity.docx"
    output = tmp_path / "one-id-output.docx"
    _with_highest_revision_id(round2, source, 2_147_483_646)

    result = apply_edits(
        str(source),
        str(output),
        [_edit(_cap_anchor(source), insert_text="")],
    )

    assert result["applied"][0]["tracked_revision_ids"] == ["2147483647"]
    created = [
        unit
        for unit in extract_redlines(str(output))["change_units"]
        if unit["author"] == DEFAULT_AUTHOR
    ]
    assert created[0]["reference"]["revision_ids"] == ["2147483647"]


def test_last_two_revision_ids_support_replace(
    round2: Path, tmp_path: Path
) -> None:
    source = tmp_path / "two-id-capacity.docx"
    output = tmp_path / "two-id-output.docx"
    _with_highest_revision_id(round2, source, 2_147_483_645)

    result = apply_edits(
        str(source), str(output), [_edit(_cap_anchor(source))]
    )

    assert result["applied"][0]["tracked_revision_ids"] == [
        "2147483646",
        "2147483647",
    ]


def test_replace_refuses_when_only_one_revision_id_remains(
    round2: Path, tmp_path: Path
) -> None:
    source = tmp_path / "replace-capacity.docx"
    output = tmp_path / "replace-output.docx"
    _with_highest_revision_id(round2, source, 2_147_483_646)
    edits = [_edit(_cap_anchor(source))]

    result = preflight_edits(str(source), edits)

    assert result["batch_applicable"] is False
    assert result["refusal_code"] == "revision_id_exhausted"
    assert result["failure_phase"] == "planning"
    assert result["blocking_edit_index"] == 0
    assert result["edits"][0] == {
        "edit_index": 0,
        "change_unit_id": edits[0]["anchor"]["change_unit_id"],
        "status": "blocked",
        "operation": "replace",
        "match_count": 1,
        "target_author": None,
        "target_revision_ids": [],
        "position_supported": False,
        "refusal_code": "revision_id_exhausted",
    }

    with pytest.raises(ApplyError, match="revision_id_exhausted") as refusal:
        apply_edits(str(source), str(output), edits)
    assert refusal.value.code == "revision_id_exhausted"
    assert refusal.value.metadata["required_revision_ids"] == 2
    assert refusal.value.metadata["available_revision_ids"] == 1
    assert not output.exists()


def test_batch_refuses_atomically_when_second_edit_exhausts_ids(
    round2: Path, tmp_path: Path
) -> None:
    source = tmp_path / "batch-capacity.docx"
    output = tmp_path / "batch-output.docx"
    _with_highest_revision_id(round2, source, 2_147_483_646)
    anchor = _cap_anchor(source)
    edits = [
        _edit(
            anchor,
            delete_text="Except as set out in Clause 14.3",
            insert_text="",
        ),
        _edit(anchor, insert_text=""),
    ]

    result = preflight_edits(str(source), edits)

    assert result["batch_applicable"] is False
    assert result["refusal_code"] == "revision_id_exhausted"
    assert result["failure_phase"] == "planning"
    assert result["blocking_edit_index"] == 1
    assert [item["status"] for item in result["edits"]] == [
        "planned",
        "blocked",
    ]
    assert result["edits"][0]["operation"] == "delete"
    assert result["edits"][1]["operation"] == "delete"
    assert result["edits"][1]["match_count"] == 1

    with pytest.raises(ApplyError, match="revision_id_exhausted") as refusal:
        apply_edits(str(source), str(output), edits)
    assert refusal.value.metadata["edit_index"] == 1
    assert refusal.value.metadata["required_revision_ids"] == 1
    assert refusal.value.metadata["available_revision_ids"] == 0
    assert not output.exists()


def test_pure_deletion(round2: Path, tmp_path: Path) -> None:
    out = tmp_path / "delete-only.docx"
    result = apply_edits(
        str(round2), str(out), [_edit(_cap_anchor(round2), insert_text="")]
    )
    assert result["applied"][0]["inserted_text"] is None
    mine = [
        u
        for u in extract_redlines(str(out))["change_units"]
        if u["author"] == DEFAULT_AUTHOR
    ]
    assert mine[0]["change_type"] == "delete"
    assert mine[0]["new_text"] is None


def test_apply_is_deterministic(round2: Path, tmp_path: Path) -> None:
    a, b = tmp_path / "a.docx", tmp_path / "b.docx"
    apply_edits(str(round2), str(a), [_edit(_cap_anchor(round2))])
    apply_edits(str(round2), str(b), [_edit(_cap_anchor(round2))])
    assert a.read_bytes() == b.read_bytes()


def test_two_edits_in_distinct_paragraphs(round2: Path, tmp_path: Path) -> None:
    result = extract_redlines(str(round2))
    sha = result["file_sha256"]
    cap = _cap_anchor(round2)
    audit = next(
        u
        for u in result["change_units"]
        if u["clause_anchor"] and u["clause_anchor"]["label"] == "7"
    )
    out = tmp_path / "two.docx"
    outcome = apply_edits(
        str(round2),
        str(out),
        [
            _edit(cap),
            {
                "anchor": {"change_unit_id": audit["change_unit_id"], "file_sha256": sha},
                "delete_text": "ten (10) Business Days' notice",
                "insert_text": "five (5) Business Days' notice",
            },
        ],
    )
    assert len(outcome["applied"]) == 2
    mine = [
        u
        for u in extract_redlines(str(out))["change_units"]
        if u["author"] == DEFAULT_AUTHOR
    ]
    assert len(mine) == 2


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda e, sha: {**e, "anchor": {**e["anchor"], "file_sha256": "0" * 64}}, "file_sha256_mismatch"),
        (lambda e, sha: {**e, "anchor": {**e["anchor"], "change_unit_id": "cu_999"}}, "anchor_not_found"),
        (lambda e, sha: {**e, "delete_text": "text that does not exist anywhere"}, "delete_text_not_found"),
        (lambda e, sha: {**e, "delete_text": "aggregate"}, "delete_text_ambiguous"),
        # A span mixing plain text and their pending insertion is unsupported;
        # a span fully inside their insertion is a legitimate counter edit.
        (lambda e, sha: {**e, "delete_text": "exceed USD 50,000"}, "overlaps_tracked_changes"),
        (lambda e, sha: {**e, "delete_text": ""}, "delete_text_missing"),
    ],
)
def test_fail_closed_writes_nothing(round2: Path, tmp_path: Path, mutate, code) -> None:
    edit = _edit(_cap_anchor(round2))
    out = tmp_path / "never.docx"
    with pytest.raises(ApplyError) as err:
        apply_edits(str(round2), str(out), [mutate(edit, None)])
    assert err.value.code == code
    assert not out.exists()
    assert not list(tmp_path.iterdir())  # no temp artifacts either


def test_atomicity_one_bad_edit_writes_nothing(round2: Path, tmp_path: Path) -> None:
    good = _edit(_cap_anchor(round2))
    bad = {**good, "delete_text": "text that does not exist anywhere"}
    out = tmp_path / "never.docx"
    with pytest.raises(ApplyError):
        apply_edits(str(round2), str(out), [good, bad])
    assert not list(tmp_path.iterdir())


def test_two_disjoint_edits_in_same_paragraph(round2: Path, tmp_path: Path) -> None:
    """Same-paragraph edits apply right-to-left; disjoint spans succeed."""
    anchor = _cap_anchor(round2)
    out = tmp_path / "two-in-one.docx"
    result = apply_edits(
        str(round2),
        str(out),
        [
            _edit(anchor),
            _edit(anchor, delete_text="Except as set out", insert_text="Save as provided"),
        ],
    )
    assert result["round_trip_check"]["status"] == "passed"
    mine = [
        u
        for u in extract_redlines(str(out))["change_units"]
        if u["author"] == DEFAULT_AUTHOR
    ]
    assert [(u["old_text"], u["new_text"]) for u in mine] == [
        ("Except as set out", "Save as provided"),
        (TAIL_OLD, TAIL_NEW),
    ]


def test_overlapping_edits_same_paragraph_rejected(round2: Path, tmp_path: Path) -> None:
    anchor = _cap_anchor(round2)
    out = tmp_path / "never.docx"
    with pytest.raises(ApplyError) as err:
        apply_edits(
            str(round2),
            str(out),
            [
                _edit(anchor, delete_text="claims in aggregate", insert_text="X"),
                _edit(anchor, delete_text="in aggregate.", insert_text="Y"),
            ],
        )
    assert err.value.code == "edits_overlap"
    assert not out.exists()


def test_refuses_to_overwrite_output(round2: Path, tmp_path: Path) -> None:
    out = tmp_path / "existing.docx"
    out.write_bytes(b"do not clobber me")
    with pytest.raises(ApplyError) as err:
        apply_edits(str(round2), str(out), [_edit(_cap_anchor(round2))])
    assert err.value.code == "output_exists"
    assert err.value.metadata == {}
    assert out.read_bytes() == b"do not clobber me"


def test_atomic_publish_io_failure_is_stable_and_cleans_tmp(
    round2: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_module = importlib.import_module("veqtor_docx.apply")

    def fail_link(_source: str, _destination: str) -> None:
        raise PermissionError("simulated atomic publish refusal")

    monkeypatch.setattr(apply_module.os, "link", fail_link)
    output = tmp_path / "never.docx"
    observed = hashlib.sha256(round2.read_bytes()).hexdigest()
    with pytest.raises(ApplyError) as err:
        apply_edits(str(round2), str(output), [_edit(_cap_anchor(round2))])

    assert err.value.code == "output_unwritable"
    assert err.value.metadata == {"observed_source_sha256": observed}
    assert not output.exists()
    assert not list(tmp_path.glob("*.veqtor-tmp"))


def test_successful_publish_cleanup_refusal_keeps_valid_output_and_temp_link(
    round2: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_module = importlib.import_module("veqtor_docx.apply")
    cleanup_calls: list[Path] = []

    def deny_cleanup(path: str) -> None:
        cleanup_calls.append(Path(path))
        raise PermissionError("simulated post-publish cleanup refusal")

    monkeypatch.setattr(apply_module.os, "remove", deny_cleanup)
    output = tmp_path / "published.docx"

    result = apply_edits(str(round2), str(output), [_edit(_cap_anchor(round2))])

    assert result["status"] == "ok"
    assert result["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    temp_files = list(tmp_path.glob("*.veqtor-tmp"))
    assert len(temp_files) == 1
    assert cleanup_calls == temp_files
    output_stat = output.stat()
    temp_stat = temp_files[0].stat()
    assert output_stat.st_ino == temp_stat.st_ino
    assert output_stat.st_nlink == 2
    assert temp_stat.st_nlink == 2
    assert extract_redlines(str(output))["file_sha256"] == result["output_sha256"]


def test_counter_and_reinstate_in_one_call(demo_dir: Path, tmp_path: Path) -> None:
    """The M2 demo sentence: 'restore the 150% cap and reinstate the willful
    misconduct carve-out' — one call against the counterparty's round 4."""
    from veqtor_docx.synthetic import CAP_R3, CAP_R4, CARVEOUT_DROPPED

    r4 = demo_dir / "round-4-counterparty-reply.docx"
    result = extract_redlines(str(r4))
    cap = next(u for u in result["change_units"] if u["change_type"] == "replace")
    anchor = {"change_unit_id": cap["change_unit_id"], "file_sha256": result["file_sha256"]}

    out = tmp_path / "round-5-our-counter.docx"
    outcome = apply_edits(
        str(r4),
        str(out),
        [
            {"anchor": anchor, "delete_text": CAP_R4, "insert_text": CAP_R3},
            {"anchor": anchor, "reinstate_text": CARVEOUT_DROPPED},
        ],
    )
    assert outcome["round_trip_check"]["status"] == "passed"
    assert [a["operation"] for a in outcome["applied"]] == ["counter", "reinstate"]

    after = extract_redlines(str(out))
    counter = next(u for u in after["change_units"] if u["change_type"] == "counter")
    assert counter["author"] == DEFAULT_AUTHOR
    assert (counter["old_text"], counter["new_text"]) == (CAP_R4, CAP_R3)

    # Their proposal stays visible, marked as countered by our strike.
    theirs = next(u for u in after["change_units"] if u["change_type"] == "replace")
    assert theirs["new_text"] == CAP_R4
    assert theirs["countered_by"] == counter["reference"]["revision_ids"][:1]

    reinstated = next(u for u in after["change_units"] if u["change_type"] == "insert")
    assert reinstated["new_text"] == CARVEOUT_DROPPED
    # Their deletion is untouched and still reported.
    assert any(
        u["change_type"] == "delete" and u["old_text"] == CARVEOUT_DROPPED
        for u in after["change_units"]
    )

    # Word idiom: our strike is nested inside their pending insertion.
    document = parse_xml(zipfile.ZipFile(out).read("word/document.xml"))
    nested = [
        (d.get(w("author")), d.getparent().get(w("author")))
        for d in document.iter(w("del"))
        if d.getparent().tag == w("ins")
    ]
    assert nested == [(DEFAULT_AUTHOR, "53")]


def test_counter_pure_strike_without_replacement(round2: Path, tmp_path: Path) -> None:
    out = tmp_path / "strike.docx"
    result = apply_edits(
        str(round2),
        str(out),
        [_edit(_cap_anchor(round2), delete_text="USD 50,000", insert_text="")],
    )
    assert result["applied"][0]["operation"] == "counter"
    counter = next(
        u
        for u in extract_redlines(str(out))["change_units"]
        if u["change_type"] == "counter"
    )
    assert counter["old_text"] == "USD 50,000"
    assert counter["new_text"] is None


def test_counter_partial_strike_splits_their_runs(round2: Path, tmp_path: Path) -> None:
    out = tmp_path / "partial.docx"
    apply_edits(
        str(round2),
        str(out),
        [_edit(_cap_anchor(round2), delete_text="50,000", insert_text="250,000")],
    )
    after = extract_redlines(str(out))
    counter = next(u for u in after["change_units"] if u["change_type"] == "counter")
    assert (counter["old_text"], counter["new_text"]) == ("50,000", "250,000")
    # The untouched remainder of their proposal is still their unit's text.
    theirs = next(u for u in after["change_units"] if u.get("countered_by"))
    assert theirs["new_text"] == "USD 50,000"


def test_two_counters_in_one_insertion_refused(round2: Path, tmp_path: Path) -> None:
    """Two disjoint counters inside the same pending insertion cannot be laid
    out unambiguously (both replacements would pile up after the host)."""
    anchor = _cap_anchor(round2)
    out = tmp_path / "never.docx"
    with pytest.raises(ApplyError) as err:
        apply_edits(
            str(round2),
            str(out),
            [
                _edit(anchor, delete_text="USD", insert_text="EUR"),
                _edit(anchor, delete_text="50,000", insert_text="250,000"),
            ],
        )
    assert err.value.code == "edits_overlap"
    assert not list(tmp_path.iterdir())


def test_counter_with_adjacent_plain_edit_refused(round2: Path, tmp_path: Path) -> None:
    """A plain edit starting immediately after the countered insertion would
    merge with the counter's markup in extraction; refuse, do not confuse."""
    anchor = _cap_anchor(round2)
    out = tmp_path / "never.docx"
    with pytest.raises(ApplyError) as err:
        apply_edits(
            str(round2),
            str(out),
            [
                _edit(anchor, delete_text="USD 50,000", insert_text="USD 250,000"),
                _edit(anchor, delete_text=TAIL_OLD, insert_text=" per claim."),
            ],
        )
    assert err.value.code == "edits_overlap"
    assert not list(tmp_path.iterdir())


def test_counter_plus_distant_edit_in_same_paragraph(round2: Path, tmp_path: Path) -> None:
    """With untouched text between the operations, a counter and a plain edit
    coexist in one paragraph and extract as two distinct units."""
    anchor = _cap_anchor(round2)
    out = tmp_path / "combo.docx"
    result = apply_edits(
        str(round2),
        str(out),
        [
            _edit(anchor, delete_text="USD 50,000", insert_text="EUR 60,000"),
            _edit(anchor, delete_text="Except as set out", insert_text="Save as provided"),
        ],
    )
    assert result["round_trip_check"]["status"] == "passed"
    mine = [
        u
        for u in extract_redlines(str(out))["change_units"]
        if u["author"] == DEFAULT_AUTHOR
    ]
    assert [(u["change_type"], u["old_text"], u["new_text"]) for u in mine] == [
        ("replace", "Except as set out", "Save as provided"),
        ("counter", "USD 50,000", "EUR 60,000"),
    ]


def _countered_step1(round2: Path, tmp_path: Path) -> tuple[str, dict]:
    """First call: counter 'USD' -> 'EUR'; returns output path and an anchor."""
    step1 = tmp_path / "step1.docx"
    apply_edits(
        str(round2),
        str(step1),
        [_edit(_cap_anchor(round2), delete_text="USD", insert_text="EUR")],
    )
    result = extract_redlines(str(step1))
    counter = next(u for u in result["change_units"] if u["change_type"] == "counter")
    return str(step1), {
        "change_unit_id": counter["change_unit_id"],
        "file_sha256": result["file_sha256"],
    }


def test_followup_counter_on_countered_insertion_refused(
    round2: Path, tmp_path: Path
) -> None:
    """A second counter on the same insertion cannot be laid out — in the
    same call or any later one. Both variants must be clean planning-time
    refusals, not late round_trip_failed."""
    step1, anchor = _countered_step1(round2, tmp_path)
    for edit in (
        {"anchor": anchor, "delete_text": "50,000", "insert_text": "250,000"},
        {"anchor": anchor, "delete_text": "50,000"},
    ):
        with pytest.raises(ApplyError) as err:
            apply_edits(step1, str(tmp_path / "never.docx"), [edit])
        assert err.value.code == "already_countered"
        assert not (tmp_path / "never.docx").exists()


def test_followup_plain_edit_flush_against_our_replacement_refused(
    round2: Path, tmp_path: Path
) -> None:
    step1, anchor = _countered_step1(round2, tmp_path)
    with pytest.raises(ApplyError) as err:
        apply_edits(
            step1,
            str(tmp_path / "never.docx"),
            [
                {
                    "anchor": anchor,
                    "delete_text": TAIL_OLD,
                    "insert_text": " per claim.",
                }
            ],
        )
    assert err.value.code == "adjacent_to_own_revision"
    assert not (tmp_path / "never.docx").exists()


def test_plain_edit_after_pure_strike_host_refused(round2: Path, tmp_path: Path) -> None:
    """A pure-strike counter leaves no replacement after the host, but the
    strike still surfaces at the host's right edge in extraction — a plain
    edit flush after that host must be refused cleanly, not fail late."""
    step1 = tmp_path / "step1.docx"
    apply_edits(
        str(round2),
        str(step1),
        [_edit(_cap_anchor(round2), delete_text="USD ", insert_text="")],
    )
    result = extract_redlines(str(step1))
    counter = next(u for u in result["change_units"] if u["change_type"] == "counter")
    with pytest.raises(ApplyError) as err:
        apply_edits(
            str(step1),
            str(tmp_path / "never.docx"),
            [
                {
                    "anchor": {
                        "change_unit_id": counter["change_unit_id"],
                        "file_sha256": result["file_sha256"],
                    },
                    "delete_text": TAIL_OLD,
                    "insert_text": " per claim.",
                }
            ],
        )
    assert err.value.code == "adjacent_to_own_revision"
    assert not (tmp_path / "never.docx").exists()


def test_pure_strike_on_host_followed_by_our_markup_refused(
    round2: Path, tmp_path: Path
) -> None:
    """The symmetric order: plain edit first, then a pure-strike counter on
    the host our earlier markup now touches."""
    step1 = tmp_path / "step1.docx"
    apply_edits(
        str(round2),
        str(step1),
        [_edit(_cap_anchor(round2), delete_text=TAIL_OLD, insert_text=" per claim.")],
    )
    result = extract_redlines(str(step1))
    theirs = next(
        u
        for u in result["change_units"]
        if u["author"] != DEFAULT_AUTHOR
        and u["clause_anchor"]
        and u["clause_anchor"]["label"] == "14.2"
    )
    with pytest.raises(ApplyError) as err:
        apply_edits(
            str(step1),
            str(tmp_path / "never.docx"),
            [
                {
                    "anchor": {
                        "change_unit_id": theirs["change_unit_id"],
                        "file_sha256": result["file_sha256"],
                    },
                    "delete_text": "USD 50,000",
                }
            ],
        )
    assert err.value.code == "adjacent_to_own_revision"
    assert not (tmp_path / "never.docx").exists()


def test_round_trip_verdict_classification() -> None:
    """The systematic net: losing a baseline unit is adjacency by definition;
    a missing proposed edit is a genuine round-trip failure."""
    from veqtor_docx.apply import _round_trip_verdict

    baseline = [("replace", "53", None, "a", "b", ("1", "2"))]
    proposed = [("insert", DEFAULT_AUTHOR, None, None, "x", ("9",))]

    assert _round_trip_verdict(baseline, baseline + proposed, proposed) is None

    merged = [("counter", DEFAULT_AUTHOR, None, "ab", "x", ("1", "9"))]
    verdict = _round_trip_verdict(baseline, merged, proposed)
    assert verdict is not None and verdict.code == "adjacent_to_own_revision"

    verdict = _round_trip_verdict(baseline, list(baseline), proposed)
    assert verdict is not None and verdict.code == "round_trip_failed"


def test_followup_edit_with_separating_text_works(round2: Path, tmp_path: Path) -> None:
    """Sequential calls stay a valid recipe when untouched text separates the
    new edit from earlier markup."""
    step1, anchor = _countered_step1(round2, tmp_path)
    out = tmp_path / "step2.docx"
    result = apply_edits(
        str(step1),
        str(out),
        [
            {
                "anchor": anchor,
                "delete_text": "Except as set out",
                "insert_text": "Save as provided",
            }
        ],
    )
    assert result["round_trip_check"]["status"] == "passed"
    units = extract_redlines(str(out))["change_units"]
    assert [
        (u["change_type"], u["old_text"], u["new_text"])
        for u in units
        if u["author"] == DEFAULT_AUTHOR
    ] == [
        ("replace", "Except as set out", "Save as provided"),
        ("counter", "USD", "EUR"),
    ]


def test_counter_own_insertion_refused(round2: Path, tmp_path: Path) -> None:
    first = tmp_path / "first.docx"
    apply_edits(str(round2), str(first), [_edit(_cap_anchor(round2))])
    result = extract_redlines(str(first))
    mine = next(u for u in result["change_units"] if u["author"] == DEFAULT_AUTHOR)
    with pytest.raises(ApplyError) as err:
        apply_edits(
            str(first),
            str(tmp_path / "never.docx"),
            [
                {
                    "anchor": {
                        "change_unit_id": mine["change_unit_id"],
                        "file_sha256": result["file_sha256"],
                    },
                    "delete_text": "Contract Year",
                    "insert_text": "Fiscal Year",
                }
            ],
        )
    assert err.value.code == "overlaps_tracked_changes"


def test_configured_author_collision_is_visible_in_preflight(
    round2: Path,
) -> None:
    result = preflight_edits(
        str(round2),
        [_edit(_cap_anchor(round2), delete_text="USD 50,000")],
        author="53",
    )

    assert result["tracked_change_author"] == "53"
    assert result["batch_applicable"] is False
    assert result["refusal_code"] == "overlaps_tracked_changes"


def test_custom_author_is_shared_by_preflight_output_and_docx(
    round2: Path, tmp_path: Path
) -> None:
    author = "John Deer"
    edits = [_edit(_cap_anchor(round2))]
    preflight = preflight_edits(str(round2), edits, author=author)
    output = tmp_path / "custom-author.docx"
    applied = apply_edits(str(round2), str(output), edits, author=author)

    assert preflight["tracked_change_author"] == author
    assert applied["tracked_change_author"] == author
    assert any(
        unit["author"] == author
        for unit in extract_redlines(str(output))["change_units"]
    )


def test_configured_author_collision_blocks_reinstate(demo_dir: Path) -> None:
    from veqtor_docx.synthetic import CARVEOUT_DROPPED

    r4 = demo_dir / "round-4-counterparty-reply.docx"
    extracted = extract_redlines(str(r4))
    cap = next(u for u in extracted["change_units"] if u["change_type"] == "replace")
    result = preflight_edits(
        str(r4),
        [
            {
                "anchor": {
                    "change_unit_id": cap["change_unit_id"],
                    "file_sha256": extracted["file_sha256"],
                },
                "reinstate_text": CARVEOUT_DROPPED,
            }
        ],
        author="53",
    )

    assert result["batch_applicable"] is False
    assert result["refusal_code"] == "reinstate_text_not_found"


def test_reinstate_text_not_found(demo_dir: Path, tmp_path: Path) -> None:
    r4 = demo_dir / "round-4-counterparty-reply.docx"
    result = extract_redlines(str(r4))
    cap = next(u for u in result["change_units"] if u["change_type"] == "replace")
    with pytest.raises(ApplyError) as err:
        apply_edits(
            str(r4),
            str(tmp_path / "never.docx"),
            [
                {
                    "anchor": {
                        "change_unit_id": cap["change_unit_id"],
                        "file_sha256": result["file_sha256"],
                    },
                    "reinstate_text": "text nobody ever deleted",
                }
            ],
        )
    assert err.value.code == "reinstate_text_not_found"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "insert_text",
    [None, "", False, 0, 3.14, [], {}, "replacement"],
    ids=["null", "empty", "false", "zero", "float", "list", "object", "text"],
)
def test_reinstate_rejects_every_present_insert_text_value(
    demo_dir: Path,
    tmp_path: Path,
    insert_text,
) -> None:
    from veqtor_docx.synthetic import CARVEOUT_DROPPED

    source = demo_dir / "round-4-counterparty-reply.docx"
    extracted = extract_redlines(str(source))
    cap = next(
        unit for unit in extracted["change_units"] if unit["change_type"] == "replace"
    )
    edit = {
        "anchor": {
            "change_unit_id": cap["change_unit_id"],
            "file_sha256": extracted["file_sha256"],
        },
        "reinstate_text": CARVEOUT_DROPPED,
        "insert_text": insert_text,
    }
    output = tmp_path / "never.docx"

    preflight = preflight_edits(str(source), [edit])

    assert preflight["batch_applicable"] is False
    assert preflight["failure_phase"] == "validation"
    assert preflight["refusal_code"] == "invalid_edit"
    assert preflight["blocking_edit_index"] == 0
    assert set(preflight["edits"][0]) == PREFLIGHT_DIAGNOSTIC_KEYS
    assert preflight["edits"][0]["status"] == "blocked"
    assert preflight["edits"][0]["refusal_code"] == "invalid_edit"

    with pytest.raises(ApplyError, match="invalid_edit") as refusal:
        apply_edits(str(source), str(output), [edit])
    assert refusal.value.code == "invalid_edit"
    assert refusal.value.metadata["failure_phase"] == "validation"
    assert not output.exists()


def test_apply_at_table_cell_anchor(round2: Path, tmp_path: Path) -> None:
    """An edit anchored inside a table cell round-trips like any other."""
    result = extract_redlines(str(round2))
    cell_unit = next(
        u
        for u in result["change_units"]
        if u["change_type"] == "replace"
        and u["clause_anchor"]
        and u["clause_anchor"]["label"] == "3.3"
    )
    out = tmp_path / "table-edit.docx"
    outcome = apply_edits(
        str(round2),
        str(out),
        [
            {
                "anchor": {
                    "change_unit_id": cell_unit["change_unit_id"],
                    "file_sha256": result["file_sha256"],
                },
                "delete_text": "%",
                "insert_text": " percent",
            }
        ],
    )
    assert outcome["round_trip_check"]["status"] == "passed"
    mine = [
        u
        for u in extract_redlines(str(out))["change_units"]
        if u["author"] == DEFAULT_AUTHOR
    ]
    assert len(mine) == 1
    assert (mine[0]["old_text"], mine[0]["new_text"]) == ("%", " percent")
    assert mine[0]["clause_anchor"]["label"] == "3.3"


def _document_roots(path: Path) -> tuple:
    payload = zipfile.ZipFile(path).read("word/document.xml")
    return parse_xml(payload), parse_xml(payload)


def _fee_cell_paragraph_index(root) -> int:
    paragraphs = list(root.iter(w("p")))
    return next(
        i
        for i, p in enumerate(paragraphs)
        if any(el.tag == w("delText") and el.text == "50" for el in p.iter())
    )


def test_collateral_proof_catches_changes_in_other_table_rows(round2: Path) -> None:
    """The no-collateral proof must be paragraph-granular: a change smuggled
    into ANOTHER row of the table containing the touched paragraph is
    collateral, not exempt."""
    original, mutated = _document_roots(round2)
    touched_index = _fee_cell_paragraph_index(mutated)
    sneak = next(t for t in mutated.iter(w("t")) if t.text == "75%")
    sneak.text = "76%"
    issues = _collateral_outside(original, mutated, {touched_index})
    assert issues, "a change in a sibling table row must be reported as collateral"


def test_collateral_proof_accepts_changes_only_in_touched_paragraph(round2: Path) -> None:
    original, mutated = _document_roots(round2)
    touched_index = _fee_cell_paragraph_index(mutated)
    paragraphs = list(mutated.iter(w("p")))
    target = next(t for t in paragraphs[touched_index].iter(w("t")) if t.text == "%")
    target.text = "%%"
    assert _collateral_outside(original, mutated, {touched_index}) == []


def test_collateral_proof_catches_structural_changes(round2: Path) -> None:
    original, mutated = _document_roots(round2)
    row = next(mutated.iter(w("tr")))
    row.getparent().remove(row)
    assert _collateral_outside(original, mutated, set())


@pytest.mark.parametrize(
    ("edits", "code"),
    [
        (42, "invalid_edit"),
        (["not an object"], "invalid_edit"),
        ([{"anchor": "cu_001", "delete_text": "x"}], "anchor_missing"),
        ([{"anchor": {"change_unit_id": 5, "file_sha256": "a" * 64}, "delete_text": "x"}], "anchor_missing"),
        ([{"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64}, "delete_text": 42}], "delete_text_missing"),
        ([{"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64}, "delete_text": "x", "insert_text": 42}], "invalid_edit"),
        ([{"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64}, "delete_text": "x", "insert_text": None}], "invalid_edit"),
        ([{"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64}, "delete_text": "x", "insert_text": "bad\x01text"}], "invalid_edit"),
        ([{"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64}, "delete_text": "x", "reinstate_text": "y"}], "invalid_edit"),
        ([{"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64}, "reinstate_text": "y", "insert_text": "z"}], "invalid_edit"),
        ([{"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64}, "reinstate_text": "y", "insert_text": False}], "invalid_edit"),
        ([{"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64}, "reinstate_text": ""}], "invalid_edit"),
        ([{"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64, "extra": "x"}, "delete_text": "x"}], "invalid_edit"),
        ([{"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64}, "delete_text": "x", "extra": "x"}], "invalid_edit"),
    ],
)
def test_malformed_edits_fail_closed_with_error_codes(
    round2: Path, tmp_path: Path, edits, code
) -> None:
    """Loosely typed MCP input must never surface as a raw Python error."""
    out = tmp_path / "never.docx"
    with pytest.raises(ApplyError) as err:
        apply_edits(str(round2), str(out), edits)
    assert err.value.code == code
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "delete_text",
    [None, False, 0, 3.14, [], {}],
    ids=["null", "false", "zero", "float", "list", "object"],
)
def test_documented_unusable_delete_text_returns_delete_text_missing(
    round2: Path,
    tmp_path: Path,
    delete_text,
) -> None:
    edit = {
        "anchor": {
            "change_unit_id": "cu_001",
            "file_sha256": "a" * 64,
        },
        "delete_text": delete_text,
    }
    preflight = preflight_edits(str(round2), [edit])
    output = tmp_path / "never.docx"

    assert preflight["batch_applicable"] is False
    assert preflight["failure_phase"] == "validation"
    assert preflight["refusal_code"] == "delete_text_missing"
    assert preflight["edits"][0]["refusal_code"] == "delete_text_missing"
    with pytest.raises(ApplyError) as refusal:
        apply_edits(str(round2), str(output), [edit])
    assert refusal.value.code == "delete_text_missing"
    assert refusal.value.metadata["failure_phase"] == "validation"
    assert not output.exists()
    assert not list(tmp_path.iterdir())


def test_preexisting_tmp_sibling_is_not_clobbered(round2: Path, tmp_path: Path) -> None:
    out = tmp_path / "counter.docx"
    sibling = tmp_path / "counter.docx.veqtor-tmp"
    sibling.write_text("sentinel")
    apply_edits(str(round2), str(out), [_edit(_cap_anchor(round2))])
    assert sibling.read_text() == "sentinel"
    assert out.exists()


def test_output_is_a_valid_source_for_further_rounds(round2: Path, tmp_path: Path) -> None:
    """The applied file must itself be a first-class read-path citizen."""
    out = tmp_path / "counter.docx"
    apply_edits(str(round2), str(out), [_edit(_cap_anchor(round2))])
    listing_sha = hashlib.sha256(out.read_bytes()).hexdigest()
    reread = extract_redlines(str(out))
    assert reread["file_sha256"] == listing_sha
    assert reread["revision_count"] == extract_redlines(str(round2))["revision_count"] + 2


def test_preflight_candidate_hash_matches_published_output(
    round2: Path, tmp_path: Path
) -> None:
    edits = [_edit(_cap_anchor(round2))]
    preflight = preflight_edits(str(round2), edits)
    out = tmp_path / "counter.docx"
    applied = apply_edits(str(round2), str(out), edits)

    assert preflight["batch_applicable"] is True
    assert set(preflight["edits"][0]) == PREFLIGHT_DIAGNOSTIC_KEYS
    assert preflight["edits"][0]["refusal_code"] is None
    assert preflight["candidate_sha256"] == applied["output_sha256"]
    assert applied["output_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()


def test_preflight_returns_structured_refusal_without_writing(
    round2: Path, tmp_path: Path
) -> None:
    source_before = round2.read_bytes()
    result = preflight_edits(
        str(round2),
        [
            _edit(
                {
                    "change_unit_id": "cu_999",
                    "file_sha256": hashlib.sha256(source_before).hexdigest(),
                }
            )
        ],
    )

    assert result["status"] == "ok"
    assert result["batch_applicable"] is False
    assert result["blocking_edit_index"] == 0
    assert result["refusal_code"] == "anchor_not_found"
    assert result["edits"] == [
        {
            "edit_index": 0,
            "status": "blocked",
            "operation": None,
            "match_count": 0,
            "target_author": None,
            "target_revision_ids": [],
            "position_supported": False,
            "change_unit_id": "cu_999",
            "refusal_code": "anchor_not_found",
        }
    ]
    assert round2.read_bytes() == source_before
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (
            {"anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64}},
            "delete_text_missing",
        ),
        (
            {
                "anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64},
                "delete_text": 42,
            },
            "delete_text_missing",
        ),
        (
            {
                "anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64},
                "delete_text": "x",
                "insert_text": 42,
            },
            "invalid_edit",
        ),
        (
            {
                "anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64},
                "delete_text": "x",
                "reinstate_text": "y",
            },
            "invalid_edit",
        ),
        (
            {
                "anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64},
                "reinstate_text": "y",
                "insert_text": "z",
            },
            "invalid_edit",
        ),
        (
            {
                "anchor": {"change_unit_id": "cu_001", "file_sha256": "a" * 64},
                "reinstate_text": "",
            },
            "invalid_edit",
        ),
    ],
)
def test_validation_refusals_use_complete_unevaluated_diagnostic_shape(
    round2: Path,
    edit: dict,
    code: str,
) -> None:
    result = preflight_edits(str(round2), [edit])

    assert result["batch_applicable"] is False
    assert result["failure_phase"] == "validation"
    assert result["refusal_code"] == code
    assert result["edits"] == [
        {
            "edit_index": 0,
            "change_unit_id": "cu_001",
            "status": "blocked",
            "operation": None,
            "match_count": None,
            "target_author": None,
            "target_revision_ids": [],
            "position_supported": None,
            "refusal_code": code,
        }
    ]


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("delete_text", "bad\x01text"),
        ("insert_text", "bad\x0btext"),
        ("reinstate_text", "bad\ud800text"),
    ],
)
def test_xml_incompatible_edit_text_is_a_structured_validation_refusal(
    round2: Path,
    field: str,
    invalid: str,
) -> None:
    anchor = _cap_anchor(round2)
    if field == "reinstate_text":
        edit = {"anchor": anchor, field: invalid}
    else:
        edit = _edit(anchor)
        edit[field] = invalid

    result = preflight_edits(str(round2), [edit])

    assert result["batch_applicable"] is False
    assert result["failure_phase"] == "validation"
    assert result["refusal_code"] == "invalid_edit"
    assert set(result["edits"][0]) == PREFLIGHT_DIAGNOSTIC_KEYS
    assert result["edits"][0] == {
        "edit_index": 0,
        "change_unit_id": anchor["change_unit_id"],
        "status": "blocked",
        "operation": None,
        "match_count": None,
        "target_author": None,
        "target_revision_ids": [],
        "position_supported": None,
        "refusal_code": "invalid_edit",
    }


def test_xml_whitespace_and_unicode_are_valid_insert_text(
    round2: Path,
) -> None:
    edit = _edit(
        _cap_anchor(round2),
        insert_text="Line one\tLine two\nПрименимо — €250,000",
    )

    result = preflight_edits(str(round2), [edit])

    assert result["batch_applicable"] is True
    assert result["round_trip_check"]["status"] == "passed"


def test_preflight_includes_surgery_and_round_trip_failures(
    round2: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_module = importlib.import_module("veqtor_docx.apply")
    monkeypatch.setattr(
        apply_module,
        "_round_trip_verdict",
        lambda *_args: ApplyError("round_trip_failed", "simulated mismatch"),
    )

    result = preflight_edits(str(round2), [_edit(_cap_anchor(round2))])

    assert result["batch_applicable"] is False
    assert result["refusal_code"] == "round_trip_failed"
    assert result["candidate_sha256"] is None
    assert len(result["observed_candidate_sha256"]) == 64
    assert result["failure_phase"] == "round_trip"
    assert result["blocking_edit_index"] is None
    assert result["edits"][0]["status"] == "planned"
    assert result["edits"][0]["position_supported"] is None
    assert result["round_trip_check"] == {
        "status": "failed",
        "comparison": ROUND_TRIP_COMPARISON_CURRENT,
        "collateral_changes": [],
    }


def test_preflight_preserves_planned_edits_without_inventing_zero_match_target(
    round2: Path,
) -> None:
    good = _edit(_cap_anchor(round2))
    bad = {**good, "delete_text": "text that does not exist anywhere"}

    result = preflight_edits(str(round2), [good, bad, good])

    assert result["batch_applicable"] is False
    assert result["failure_phase"] == "matching"
    assert result["blocking_edit_index"] == 1
    assert result["edits"][0]["status"] == "planned"
    assert result["edits"][0]["match_count"] == 1
    assert set(result["edits"][0]) == PREFLIGHT_DIAGNOSTIC_KEYS
    assert set(result["edits"][1]) == PREFLIGHT_DIAGNOSTIC_KEYS
    assert result["edits"][1] == {
        "edit_index": 1,
        "change_unit_id": bad["anchor"]["change_unit_id"],
        "status": "blocked",
        "operation": None,
        "match_count": 0,
        "target_author": None,
        "target_revision_ids": [],
        "position_supported": False,
        "refusal_code": "delete_text_not_found",
    }
    assert result["edits"][2] == {
        "edit_index": 2,
        "change_unit_id": good["anchor"]["change_unit_id"],
        "status": "not_evaluated",
        "operation": None,
        "match_count": None,
        "target_author": None,
        "target_revision_ids": [],
        "position_supported": None,
        "refusal_code": None,
    }


def test_preflight_reports_counter_position_diagnostics(
    round2: Path, tmp_path: Path
) -> None:
    blocked = tmp_path / "blocked-counter-position.docx"
    with zipfile.ZipFile(round2) as source, zipfile.ZipFile(blocked, "w") as target:
        for info in source.infolist():
            payload = source.read(info)
            if info.filename == "word/document.xml":
                document = parse_xml(payload)
                insertion = next(
                    element
                    for element in document.iter(w("ins"))
                    if "USD 50,000"
                    in "".join(node.text or "" for node in element.iter(w("t")))
                )
                following = etree.Element(w("del"))
                following.set(w("id"), "999")
                following.set(w("author"), "53")
                run = etree.SubElement(following, w("r"))
                deleted = etree.SubElement(run, w("delText"))
                deleted.text = "adjacent revision"
                insertion.addnext(following)
                payload = (
                    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
                    + etree.tostring(document)
                )
            target.writestr(info, payload)

    extracted = extract_redlines(str(blocked))
    unit = next(
        item
        for item in extracted["change_units"]
        if item.get("new_text") and "USD 50,000" in item["new_text"]
    )
    result = preflight_edits(
        str(blocked),
        [
            {
                "anchor": {
                    "change_unit_id": unit["change_unit_id"],
                    "file_sha256": extracted["file_sha256"],
                },
                "delete_text": "USD 50,000",
                "insert_text": "USD 250,000",
            }
        ],
    )

    assert result["batch_applicable"] is False
    assert result["refusal_code"] == "counter_position_unsupported"
    assert result["blocking_edit_index"] == 0
    assert result["edits"] == [
        {
            "edit_index": 0,
            "change_unit_id": unit["change_unit_id"],
            "status": "blocked",
            "operation": "counter",
            "match_count": 1,
            "target_author": "53",
            "target_revision_ids": [insertion.get(w("id"))],
            "position_supported": False,
            "refusal_code": "counter_position_unsupported",
        }
    ]


@pytest.mark.parametrize(
    ("atom_tag", "rendered"),
    [
        ("noBreakHyphen", "-"),
        ("tab", "\t"),
        ("br", "\n"),
    ],
)
def test_counter_matcher_recognizes_extracted_text_atoms_before_refusing_surgery(
    round2: Path,
    tmp_path: Path,
    atom_tag: str,
    rendered: str,
) -> None:
    source = tmp_path / f"counter-{atom_tag}.docx"
    _atom_revision_docx(round2, source, kind="ins", atom_tag=atom_tag)
    extracted = extract_redlines(str(source))
    unit = next(
        item
        for item in extracted["change_units"]
        if item.get("new_text") == f"Atom{rendered}Value"
    )
    result = preflight_edits(
        str(source),
        [
            {
                "anchor": {
                    "change_unit_id": unit["change_unit_id"],
                    "file_sha256": extracted["file_sha256"],
                },
                "delete_text": unit["new_text"],
                "insert_text": "Replacement",
            }
        ],
    )

    assert result["batch_applicable"] is False
    assert result["refusal_code"] == "unsupported_run_shape"
    assert result["failure_phase"] == "surgery"
    assert result["edits"][0]["operation"] == "counter"
    assert result["edits"][0]["match_count"] == 1
    assert result["edits"][0]["target_author"] == unit["author"]
    assert len(result["edits"][0]["target_revision_ids"]) == 1
    assert result["edits"][0]["target_revision_ids"][0] in unit["reference"][
        "revision_ids"
    ]


@pytest.mark.parametrize(
    "atom_tag",
    ["t", "noBreakHyphen", "tab", "br"],
)
def test_runless_insertion_atoms_are_controlled_unsupported_shapes(
    round2: Path,
    tmp_path: Path,
    atom_tag: str,
) -> None:
    source = tmp_path / f"runless-{atom_tag}.docx"
    _runless_atom_revision_docx(round2, source, atom_tag=atom_tag)
    extracted = extract_redlines(str(source))
    unit = next(
        item
        for item in extracted["change_units"]
        if (item.get("new_text") or "").startswith("Direct")
    )

    result = preflight_edits(
        str(source),
        [
            {
                "anchor": {
                    "change_unit_id": unit["change_unit_id"],
                    "file_sha256": extracted["file_sha256"],
                },
                "delete_text": unit["new_text"],
                "insert_text": "Replacement",
            }
        ],
    )

    assert result["batch_applicable"] is False
    assert result["refusal_code"] == "unsupported_run_shape"
    assert result["failure_phase"] == "surgery"
    assert result["edits"][0]["match_count"] == 1
    assert result["edits"][0]["operation"] == "counter"
    assert result["edits"][0]["target_author"] == unit["author"]


@pytest.mark.parametrize("atom_tag", ["t", "noBreakHyphen", "tab", "br"])
def test_runless_plain_atoms_are_controlled_unsupported_shapes(
    round2: Path,
    tmp_path: Path,
    atom_tag: str,
) -> None:
    source = tmp_path / f"runless-plain-{atom_tag}.docx"
    _runless_plain_atom_docx(round2, source, atom_tag=atom_tag)
    anchor = _cap_anchor(source)
    rendered = {"t": "Text", "noBreakHyphen": "-", "tab": "\t", "br": "\n"}[
        atom_tag
    ]
    result = preflight_edits(
        str(source),
        [
            {
                "anchor": anchor,
                "delete_text": f"Direct{rendered}Value",
                "insert_text": "Replacement",
            }
        ],
    )

    assert result["batch_applicable"] is False
    assert result["refusal_code"] == "unsupported_run_shape"
    assert result["edits"][0]["match_count"] == 1
    assert result["edits"][0]["operation"] == "replace"


@pytest.mark.parametrize(
    ("atom_tag", "rendered", "applicable"),
    [
        ("delText", "Text", True),
        ("noBreakHyphen", "-", False),
        ("tab", "\t", False),
        ("br", "\n", False),
    ],
)
def test_runless_deletion_atoms_have_total_reinstate_results(
    round2: Path,
    tmp_path: Path,
    atom_tag: str,
    rendered: str,
    applicable: bool,
) -> None:
    source = tmp_path / f"runless-deletion-{atom_tag}.docx"
    _runless_deletion_atom_docx(round2, source, atom_tag=atom_tag)
    extracted = extract_redlines(str(source))
    text = f"Direct{rendered}Value"
    unit = next(item for item in extracted["change_units"] if item.get("old_text") == text)
    result = preflight_edits(
        str(source),
        [
            {
                "anchor": {
                    "change_unit_id": unit["change_unit_id"],
                    "file_sha256": extracted["file_sha256"],
                },
                "reinstate_text": text,
            }
        ],
    )

    assert result["batch_applicable"] is applicable
    assert result["edits"][0]["match_count"] == 1
    assert result["edits"][0]["operation"] == "reinstate"
    if not applicable:
        assert result["refusal_code"] == "unsupported_run_shape"


@pytest.mark.parametrize(
    ("atom_tag", "rendered"),
    [
        ("noBreakHyphen", "-"),
        ("tab", "\t"),
        ("br", "\n"),
    ],
)
def test_reinstate_matcher_recognizes_atoms_and_refuses_lossy_restoration(
    round2: Path,
    tmp_path: Path,
    atom_tag: str,
    rendered: str,
) -> None:
    source = tmp_path / f"reinstate-{atom_tag}.docx"
    _atom_revision_docx(round2, source, kind="del", atom_tag=atom_tag)
    extracted = extract_redlines(str(source))
    unit = next(
        item
        for item in extracted["change_units"]
        if item.get("old_text") == f"Atom{rendered}Value"
    )
    result = preflight_edits(
        str(source),
        [
            {
                "anchor": {
                    "change_unit_id": unit["change_unit_id"],
                    "file_sha256": extracted["file_sha256"],
                },
                "reinstate_text": unit["old_text"],
            }
        ],
    )

    assert result["batch_applicable"] is False
    assert result["refusal_code"] == "unsupported_run_shape"
    assert result["failure_phase"] == "matching"
    assert result["edits"][0]["operation"] == "reinstate"
    assert result["edits"][0]["match_count"] == 1
    assert result["edits"][0]["target_author"] == unit["author"]
    assert len(result["edits"][0]["target_revision_ids"]) == 1
    assert result["edits"][0]["target_revision_ids"][0] in unit["reference"][
        "revision_ids"
    ]


def test_atom_before_plain_target_does_not_shift_match_offsets(
    round2: Path,
    tmp_path: Path,
) -> None:
    source = tmp_path / "atom-before-target.docx"
    anchor = _cap_anchor(round2)

    extracted = extract_redlines(str(round2))
    unit = next(
        item
        for item in extracted["change_units"]
        if item["change_unit_id"] == anchor["change_unit_id"]
    )
    revision_ids = set(unit["reference"]["revision_ids"])

    def add_prefix(document: etree._Element) -> None:
        wrapper = next(
            element
            for element in document.iter()
            if element.get(w("id")) in revision_ids
        )
        paragraph = next(wrapper.iterancestors(w("p")))
        insert_at = 1 if len(paragraph) and paragraph[0].tag == w("pPr") else 0
        run = etree.Element(w("r"))
        before = etree.SubElement(run, w("t"))
        before.text = "Prefix"
        etree.SubElement(run, w("noBreakHyphen"))
        after = etree.SubElement(run, w("t"))
        after.text = "Value "
        paragraph.insert(insert_at, run)

    _rewrite_document_xml(round2, source, add_prefix)
    updated_anchor = _cap_anchor(source)
    result = preflight_edits(str(source), [_edit(updated_anchor)])

    assert result["batch_applicable"] is True
    assert result["edits"][0]["match_count"] == 1


@pytest.mark.parametrize(
    "author",
    [None, 7, "", "   ", "bad\x01author", "bad\x0bauthor", "\udcff", "x" * 256],
)
def test_public_edit_api_rejects_invalid_authors_without_raw_exceptions(
    round2: Path,
    tmp_path: Path,
    author: object,
) -> None:
    edits = [_edit(_cap_anchor(round2))]
    preflight = preflight_edits(str(round2), edits, author=author)  # type: ignore[arg-type]

    assert preflight["batch_applicable"] is False
    assert preflight["refusal_code"] == "invalid_author"
    assert preflight["failure_phase"] == "validation"
    assert preflight["tracked_change_author"] is None
    with pytest.raises(ApplyError) as error:
        apply_edits(
            str(round2),
            str(tmp_path / "never.docx"),
            edits,
            author=author,  # type: ignore[arg-type]
        )
    assert error.value.code == "invalid_author"
    assert not (tmp_path / "never.docx").exists()


def test_source_parse_failure_after_snapshot_carries_observed_sha(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bogus.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    observed = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "never.docx"

    with pytest.raises(DocxError) as err:
        apply_edits(
            str(source),
            str(output),
            [
                {
                    "anchor": {
                        "change_unit_id": "cu_001",
                        "file_sha256": observed,
                    },
                    "delete_text": "anything",
                    "insert_text": "replacement",
                }
            ],
        )

    assert getattr(err.value, "metadata") == {
        "observed_source_sha256": observed
    }
    assert not output.exists()


def test_temp_creation_failure_after_snapshot_carries_observed_sha(
    round2: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_module = importlib.import_module("veqtor_docx.apply")

    def fail_mkstemp(**_kwargs):
        raise OSError("simulated mkstemp failure")

    monkeypatch.setattr(apply_module.tempfile, "mkstemp", fail_mkstemp)
    output = tmp_path / "never.docx"
    observed = hashlib.sha256(round2.read_bytes()).hexdigest()

    with pytest.raises(ApplyError) as err:
        apply_edits(str(round2), str(output), [_edit(_cap_anchor(round2))])

    assert err.value.code == "output_unwritable"
    assert err.value.metadata == {"observed_source_sha256": observed}
    assert not output.exists()
