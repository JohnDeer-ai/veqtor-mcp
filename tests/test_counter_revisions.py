# SPDX-License-Identifier: Apache-2.0
"""Engine-created compounds, literal outcomes, exact patches and closed refusals."""

import copy
import hashlib
import io
import zipfile

import jsonschema
import pytest
from lxml import etree

from veqtor_docx import ApplyError, drafting, inspect_document
from veqtor_docx._ooxml import w
from veqtor_docx.clean_copy import prepare_clean_copy
from veqtor_docx.revision_decisions import read_document_revisions
from veqtor_mcp._drafting_contract import OPERATIONS, REVISIONS_RESULT
from veqtor_mcp import server
from test_drafting_text import _p, _r, _ins, _replace, OTHER
from test_clean_copy import _root, _package

PROFILE = {
    "author": "Fictional Reviewing Counsel",
    "initials": "FC",
    "producer_build": "counter-tests.v1",
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parts(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def main(path):
    return etree.fromstring(parts(path.read_bytes())["word/document.xml"])


def texts(root):
    from veqtor_docx.apply import _paragraph_segments, _reading_text

    return [_reading_text(_paragraph_segments(p)) for p in root.iter(w("p"))]


def make(
    tmp_path,
    demo_dir,
    shape="suffix",
    *,
    word_markers=False,
    mutation=None,
    formatted=False,
    comment_style=False,
):
    paragraph = _p(
        _r("Pay ")
        + _ins(
            _r("thirty days", '<w:b/><w:color w:val="006600"/>' if formatted else ""),
            author=OTHER,
        )
        + _r(" later")
    )
    old, new = {
        "suffix": ("days", "weeks"),
        "whole": ("thirty days", "45 days"),
        "delete": ("days", ""),
        "whole_delete": ("thirty days", ""),
    }[shape]
    assert _replace(paragraph, old, new)["operation"] == "counter"
    if word_markers:
        original = next(paragraph.iter(w("ins")))
        marker = etree.Element(w("proofErr"), {w("type"): "spellStart"})
        if original[0].tag == w("r"):
            original.insert(0, copy.deepcopy(marker))
        paragraph.insert(paragraph.index(original) + 1, copy.deepcopy(marker))
    if mutation:
        mutation(paragraph)
    root = _root("")
    root.find(w("body")).append(paragraph)
    root.find(w("body")).append(
        _p(
            _r("Keep ")
            + _ins(_r("neighbor"), author="Unselected Reviewer", revision_id="90")
            + _r(".")
        )
    )
    path = tmp_path / "working.docx"
    payload = _package(demo_dir, root)
    if comment_style:
        package = parts(payload)
        styles = etree.fromstring(package["word/styles.xml"])
        style = etree.SubElement(
            styles,
            w("style"),
            {w("type"): "character", w("styleId"): "CommentReference"},
        )
        etree.SubElement(style, w("name"), {w("val"): "Comment Reference"})
        props = etree.SubElement(style, w("rPr"))
        etree.SubElement(props, w("vertAlign"), {w("val"): "superscript"})
        payload = _package(demo_dir, root, {"word/styles.xml": etree.tostring(styles)})
    path.write_bytes(payload)
    return path


def operation(path, original="accept", counter="accept"):
    report = read_document_revisions(str(path))
    row = next(row for row in report["revisions"] if "counter_target" in row)
    # This fixture deliberately contains a replacement, selected independently
    # from the reader's minimal original/deletion group.
    root = main(path)
    paragraph = next(n for n in root.iter(w("p")) if any(child.tag == w("ins") and child.find(w("del")) is not None for child in n))
    replacement = [child for child in paragraph if child.tag == w("ins") and child.find(w("del")) is None]
    if replacement:
        from veqtor_docx.revision_decisions import reference
        row["counter_target"]["counter_insertion_ref"] = reference(root, replacement[0], sha(path))
    return {
        "kind": "decide_revision",
        "decision": original,
        "counter_decision": counter,
        "target": row["counter_target"],
    }


def save(path, ops, *, journal=False):
    digest = sha(path)
    pre = drafting.preflight_document_edits(
        str(path), digest, ops, **PROFILE, journal=journal
    )
    assert pre["batch_applicable"], pre
    return drafting.apply_document_edits(
        str(path), digest, ops, pre["preflight_proof"], **PROFILE, journal=journal
    )


@pytest.mark.parametrize("shape", ["suffix", "whole", "delete", "whole_delete"])
@pytest.mark.parametrize(
    "original,counter",
    [
        ("accept", "accept"),
        ("accept", "reject"),
        ("reject", "accept"),
        ("reject", "reject"),
    ],
)
@pytest.mark.parametrize("word_markers", [False, True])
def test_explicit_compound_matrix_keeps_neighbors_authors_and_nonmain_parts(
    tmp_path, demo_dir, shape, original, counter, word_markers
):
    path = make(tmp_path, demo_dir, shape, word_markers=word_markers)
    before = path.read_bytes()
    old_parts = parts(before)
    neighbor = etree.tostring(main(path).find(w("body"))[1])
    op = operation(path, original, counter)
    jsonschema.validate([op], OPERATIONS)
    result = save(path, [op], journal=True)
    selected = {
        ("accept", "reject"): "thirty days",
        ("reject", "reject"): "",
        ("reject", "accept"): {
            "suffix": "weeks",
            "whole": "45 days",
            "delete": "",
            "whole_delete": "",
        }[shape],
        ("accept", "accept"): {
            "suffix": "thirty weeks",
            "whole": "45 days",
            "delete": "thirty ",
            "whole_delete": "",
        }[shape],
    }[original, counter]
    assert texts(main(path))[0] == "Pay " + selected + " later"
    assert etree.tostring(main(path).find(w("body"))[1]) == neighbor
    new_parts = parts(path.read_bytes())
    assert set(new_parts) == set(old_parts)
    assert all(
        new_parts[name] == data
        for name, data in old_parts.items()
        if name != "word/document.xml"
    )
    assert open(result["backup_path"], "rb").read() == before
    remaining = read_document_revisions(str(path))["revisions"]
    assert [(row["author"], row["id"][:2]) for row in remaining] == [
        ("Unselected Reviewer", "90")
    ]
    assert result["operations"][0]["counter_decision"] == counter


def test_fresh_reader_shows_four_literal_outcomes_and_complete_refs(tmp_path, demo_dir):
    path = make(tmp_path, demo_dir)
    report = server.read_document_revisions(str(path))
    jsonschema.validate(report, REVISIONS_RESULT)
    row = next(row for row in report["revisions"] if "counter_target" in row)
    assert len(row["preview"]["members"]) == 2
    assert row["counter_target"]["counter_insertion_ref"] is None
    assert [entry["text"] for entry in row["preview"]["counter_outcomes"]] == [
        "thirty ",
        "thirty days",
        "",
        "",
    ]
    assert all(
        entry["refusal_code"] is None for entry in row["preview"]["counter_outcomes"]
    )
    pre = drafting.preflight_document_edits(str(path), sha(path), [operation(path)], **PROFILE)
    assert pre["batch_applicable"]
    assert [entry["text"] for entry in pre["operations"][0]["counter_outcomes"]] == [
        "thirty weeks", "thirty days", "weeks", ""]


@pytest.mark.parametrize("shape", ["suffix", "whole", "delete", "whole_delete"])
@pytest.mark.parametrize("word_markers", [False, True])
def test_clean_accepts_same_compound_with_exact_expected_text_and_retains_parts(
    tmp_path, demo_dir, shape, word_markers
):
    path = make(tmp_path, demo_dir, shape, word_markers=word_markers)
    source = path.read_bytes()
    candidate = prepare_clean_copy(source, expected_source_sha256=sha(path))
    after = parts(candidate.payload)
    original = parts(source)
    expected = {
        "suffix": "Pay thirty weeks later",
        "whole": "Pay 45 days later",
        "delete": "Pay thirty  later",
        "whole_delete": "Pay  later",
    }[shape]
    assert texts(etree.fromstring(after["word/document.xml"])) == [
        expected,
        "Keep neighbor.",
    ]
    assert candidate.inventory_after["revisions"] == []
    assert all(
        after[name] == data
        for name, data in original.items()
        if name != "word/document.xml"
    )
    assert path.read_bytes() == source


def test_group_requires_both_explicit_decisions_and_rejects_overlap_or_stale_refs(
    tmp_path, demo_dir
):
    path = make(tmp_path, demo_dir)
    op = operation(path)
    incomplete = {k: v for k, v in op.items() if k != "counter_decision"}
    assert not jsonschema.Draft202012Validator(OPERATIONS).is_valid([incomplete])
    before = path.read_bytes()
    refused = drafting.preflight_document_edits(
        str(path), sha(path), [incomplete], **PROFILE
    )
    assert not refused["batch_applicable"] and refused["refusal_code"] == "invalid_edit"
    own = {
        "kind": "decide_revision",
        "decision": "accept",
        "target": {
            "kind": "single",
            "revision_ref": op["target"]["counter_deletion_ref"],
        },
    }
    pre = drafting.preflight_document_edits(str(path), sha(path), [op, own], **PROFILE)
    assert (
        not pre["batch_applicable"]
        and pre["refusal_code"] == "revision_operation_overlap"
    )
    save(path, [op])
    pre = drafting.preflight_document_edits(str(path), sha(path), [op], **PROFILE)
    assert (
        not pre["batch_applicable"]
        and pre["refusal_code"] == "revision_reference_mismatch"
    )
    assert before != path.read_bytes()


def test_following_round_can_edit_again_then_clean_without_manual_repair(
    tmp_path, demo_dir
):
    path = make(tmp_path, demo_dir)
    save(path, [operation(path)])
    paragraph = inspect_document(str(path), mode="browse", max_items=10)["paragraphs"][
        0
    ]
    save(
        path,
        [
            {
                "kind": "replace_text",
                "paragraph_ref": paragraph["paragraph_ref"],
                "old_text": "weeks",
                "new_text": "months",
            }
        ],
    )
    candidate = prepare_clean_copy(path.read_bytes(), expected_source_sha256=sha(path))
    assert (
        texts(etree.fromstring(parts(candidate.payload)["word/document.xml"]))[0]
        == "Pay thirty months later"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "middle",
        "extra_nested",
        "unknown_atom",
        "unknown_marker",
        "bookmark_gap",
        "new_nested",
    ],
)
def test_unknown_or_incomplete_shapes_never_disappear_through_rejection(
    tmp_path, demo_dir, fault
):
    def damage(paragraph):
        original = next(paragraph.iter(w("ins")))
        deletion = next(original.iter(w("del")))
        if fault == "middle":
            original.append(
                etree.fromstring(
                    '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:t>not a suffix</w:t></w:r>'
                )
            )
        elif fault == "extra_nested":
            original.insert(0, copy.deepcopy(deletion))
        elif fault == "unknown_atom":
            etree.SubElement(deletion[0], "{urn:fictional:unknown}hidden")
        elif fault == "unknown_marker":
            original.insert(0, etree.Element(w("proofErr"), {w("type"): "unknown"}))
        elif fault == "bookmark_gap":
            paragraph.insert(
                paragraph.index(original) + 1,
                etree.Element(w("bookmarkStart"), {w("id"): "77", w("name"): "Keep"}),
            )
            paragraph.append(etree.Element(w("bookmarkEnd"), {w("id"): "77"}))
        else:
            new = next(
                n for n in paragraph if n.tag == w("ins") and n is not original
            )
            nested = copy.deepcopy(deletion)
            nested.append(copy.deepcopy(deletion))
            new.append(nested)

    path = make(tmp_path, demo_dir, mutation=damage)
    before = path.read_bytes()
    root = main(path)
    paragraph = root.find(w("body"))[0]
    original = next(n for n in paragraph if n.tag == w("ins"))
    deletion = next(original.iter(w("del")))
    replacement = next(n for n in paragraph if n.tag == w("ins") and n is not original)
    from veqtor_docx.revision_decisions import reference

    op = {
        "kind": "decide_revision",
        "decision": "reject",
        "counter_decision": "reject",
        "target": {
            "kind": "counter_insertion",
            "original_insertion_ref": reference(root, original, sha(path)),
            "counter_deletion_ref": reference(root, deletion, sha(path)),
            "counter_insertion_ref": reference(root, replacement, sha(path)),
        },
    }
    pre = drafting.preflight_document_edits(str(path), sha(path), [op], **PROFILE)
    assert not pre["batch_applicable"] and pre["preflight_proof"] is None
    with pytest.raises(ApplyError):
        prepare_clean_copy(before, expected_source_sha256=sha(path))
    assert path.read_bytes() == before
    assert not (tmp_path / ".veqtor-backups").exists()


def test_unselected_comment_in_same_paragraph_and_run_format_survive(
    tmp_path, demo_dir
):
    path = make(tmp_path, demo_dir, formatted=True, comment_style=True)
    neighbor = inspect_document(str(path), mode="browse", max_items=10)["paragraphs"][0]
    save(
        path,
        [
            {
                "kind": "add_comment",
                "paragraph_ref": neighbor["paragraph_ref"],
                "quote": "Pay",
                "text": "Fictional preserved comment.",
            }
        ],
    )
    before = parts(path.read_bytes())
    comment_rows = drafting.read_document_comments(str(path))["comments"]
    assert comment_rows
    save(path, [operation(path)])
    after = parts(path.read_bytes())
    assert drafting.read_document_comments(str(path))["comments"] == comment_rows
    assert all(
        after[name] == data
        for name, data in before.items()
        if name != "word/document.xml"
    )
    selected = main(path).find(w("body"))[0]
    for run in selected:
        if run.tag == w("r") and "".join(run.itertext()).strip() in {"thirty", "weeks"}:
            assert run.find(f"{w('rPr')}/{w('b')}") is not None
    clean = prepare_clean_copy(path.read_bytes(), expected_source_sha256=sha(path))
    assert parts(clean.payload)["word/comments.xml"] == after["word/comments.xml"]


@pytest.mark.parametrize("original,counter", [("accept", "reject"), ("reject", "accept")])
def test_bilingual_fixed_table_counter_preserves_other_cell_geometry_and_format(
    tmp_path, demo_dir, original, counter
):
    path = make(tmp_path, demo_dir, formatted=True)
    root = main(path)
    body = root.find(w("body"))
    selected = body[0]
    body.remove(selected)
    table = etree.fromstring(
        '<w:tbl xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:tblPr><w:tblW w:w="6000" w:type="dxa"/><w:tblLayout w:type="fixed"/></w:tblPr>'
        '<w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid>'
        '<w:tr><w:trPr><w:trHeight w:val="900" w:hRule="atLeast"/></w:trPr>'
        '<w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr></w:tc>'
        '<w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr>'
        '<w:p><w:r><w:t>Неизменённая языковая колонка.</w:t></w:r></w:p></w:tc>'
        '</w:tr></w:tbl>'
    )
    row = table.find(w("tr"))
    row.findall(w("tc"))[0].append(selected)
    body.insert(0, table)
    path.write_bytes(_package(demo_dir, root))
    untouched = etree.tostring(row.findall(w("tc"))[1])
    geometry = [etree.tostring(table[i]) for i in (0, 1)]
    save(path, [operation(path, original, counter)])
    actual = main(path).find(w("body"))[0]
    assert etree.tostring(actual.find(w("tr")).findall(w("tc"))[1]) == untouched
    assert [etree.tostring(actual[i]) for i in (0, 1)] == geometry
    assert texts(actual) == ["Pay " + ("thirty days" if original == "accept" else "weeks") + " later",
                             "Неизменённая языковая колонка."]
    clean = prepare_clean_copy(path.read_bytes(), expected_source_sha256=sha(path))
    assert clean.inventory_after["revisions"] == []


@pytest.mark.anyio
async def test_counter_mcp_transport_pagination_and_lost_reply_recovery(tmp_path, demo_dir, monkeypatch):
    from mcp.client import Client

    path = make(tmp_path, demo_dir)
    monkeypatch.setenv("VEQTOR_OPERATION_JOURNAL", "required")
    monkeypatch.setenv("VEQTOR_TRACKED_CHANGE_AUTHOR", PROFILE["author"])
    monkeypatch.setenv("VEQTOR_COMMENT_INITIALS", PROFILE["initials"])
    async with Client(server.mcp) as client:
        report = await client.call_tool("read_document_revisions", {"path": str(path), "max_items": 1})
        assert not report.is_error, report
        page = report.structured_content
        assert page["next_offset"] == 1
        jsonschema.validate(page, REVISIONS_RESULT)
        nested_page = await client.call_tool("read_document_revisions", {"path": str(path),
            "file_sha256": page["file_sha256"], "offset": 1, "max_items": 1})
        assert not nested_page.is_error, nested_page
        assert nested_page.structured_content["revisions"][0]["counter_target"] == page["revisions"][0]["counter_target"]
        args = {"source_path": str(path), "file_sha256": page["file_sha256"],
                "operations": [{"kind": "decide_revision", "decision": "accept", "counter_decision": "reject",
                    "target": operation(path)["target"]}]}
        pre = await client.call_tool("preflight_document_edits", args)
        assert not pre.is_error, pre
        proof = pre.structured_content["preflight_proof"]
        saved = await client.call_tool("apply_document_edits", {**args, "preflight_proof": proof})
        assert not saved.is_error, saved
        written = path.read_bytes()
        recovered = await client.call_tool("recover_document_operation", {"source_path": str(path),
            "operation_id": proof["operation_id"]})
        assert not recovered.is_error, recovered
        assert recovered.structured_content["recovery_status"] == "existing_result"
        assert path.read_bytes() == written
        assert texts(main(path))[0] == "Pay thirty days later"


def test_counter_reader_charges_four_outcomes_and_exact_group_references(tmp_path, demo_dir, monkeypatch):
    from veqtor_docx import revision_decisions as decisions

    path = make(tmp_path, demo_dir)
    length = len(decisions._xml(main(path)))
    monkeypatch.setattr(decisions, "MAX_FINGERPRINT_BYTES", 3 * length)
    report = read_document_revisions(str(path), max_items=1)
    assert not report["revisions"][0]["supported"]
    assert report["revisions"][0]["refusal_code"] == "resource_limit_exceeded"


@pytest.mark.parametrize("original,counter", [("accept", "accept"), ("accept", "reject"), ("reject", "accept"), ("reject", "reject")])
def test_deletion_only_counter_never_selects_adjacent_unrelated_insertion(tmp_path, demo_dir, original, counter):
    path = make(tmp_path, demo_dir, "delete")
    root = main(path)
    paragraph = root.find(w("body"))[0]
    outer = next(n for n in paragraph if n.tag == w("ins"))
    neighbor = _p(_ins(_r(" + unrelated neighbor"), author="Unselected Neighbor", revision_id="2"))[0]
    outer.addnext(neighbor)
    expected = etree.tostring(neighbor)
    path.write_bytes(_package(demo_dir, root))
    report = read_document_revisions(str(path))
    row = next(r for r in report["revisions"] if "counter_target" in r)
    assert row["counter_target"]["counter_insertion_ref"] is None
    save(path, [{"kind": "decide_revision", "decision": original, "counter_decision": counter, "target": row["counter_target"]}])
    selected = "thirty days" if original == "accept" and counter == "reject" else "thirty " if original == "accept" else ""
    assert texts(main(path))[0] == "Pay " + selected + " + unrelated neighbor later"
    assert etree.tostring(next(main(path).iter(w("ins")))) == expected


@pytest.mark.parametrize("selected", ["original", "deletion", "replacement", "run", "atom", "property"])
@pytest.mark.parametrize("namespace", ["urn:fictional:unknown", "http://schemas.openxmlformats.org/wordprocessingml/2006/main"])
def test_counter_never_discards_unknown_attributes(tmp_path, demo_dir, selected, namespace):
    path = make(tmp_path, demo_dir, formatted=True)
    root = main(path)
    paragraph = root.find(w("body"))[0]
    original = next(n for n in paragraph if n.tag == w("ins"))
    deletion = next(original.iter(w("del")))
    replacement = next(n for n in paragraph if n.tag == w("ins") and n is not original)
    nodes = {"original": original, "deletion": deletion, "replacement": replacement,
             "run": deletion[0], "atom": next(deletion.iter(w("delText"))),
             "property": next(deletion.iter(w("b")))}
    nodes[selected].set("{" + namespace + "}unknownBinding", "preserve")
    path.write_bytes(_package(demo_dir, root))
    source = path.read_bytes()
    op = operation(path, "reject", "reject")
    result = drafting.preflight_document_edits(str(path), sha(path), [op], **PROFILE)
    assert not result["batch_applicable"]
    assert result["refusal_code"] == "counter_unit_unsupported"
    assert path.read_bytes() == source
    with pytest.raises(ApplyError):
        prepare_clean_copy(source, expected_source_sha256=sha(path))


@pytest.mark.parametrize("bad_hint", [False, True])
def test_next_round_across_word_spelling_hints_preserves_nodes_and_refuses_unknown(tmp_path, demo_dir, bad_hint):
    path = make(tmp_path, demo_dir, "whole", word_markers=True)
    save(path, [operation(path)])
    root = main(path)
    paragraph = root.find(w("body"))[0]
    run = next(n for n in paragraph if n.tag == w("r") and n.find(w("t")).text == "45 days")
    run.find(w("t")).text = "45 "
    marker = etree.Element(w("proofErr"), {w("type"): "future" if bad_hint else "spellStart"})
    run.addnext(marker)
    second = copy.deepcopy(run)
    second.find(w("t")).text = "days"
    marker.addnext(second)
    path.write_bytes(_package(demo_dir, root))
    para = inspect_document(str(path), mode="browse", max_items=1)["paragraphs"][0]
    op = {"kind": "replace_text", "paragraph_ref": para["paragraph_ref"], "old_text": "45 days", "new_text": "60 days"}
    source = path.read_bytes()
    pre = drafting.preflight_document_edits(str(path), sha(path), [op], **PROFILE)
    assert pre["batch_applicable"] is not bad_hint
    if not bad_hint:
        save(path, [op])
        assert texts(main(path))[0] == "Pay 60 days later"
        assert any(etree.tostring(n) == etree.tostring(marker) for n in main(path).iter(w("proofErr")))
        assert prepare_clean_copy(path.read_bytes(), expected_source_sha256=sha(path)).inventory_after["revisions"] == []
    else:
        assert source == path.read_bytes()


def test_new_plain_revision_hint_support_cannot_discard_unknown_wrapper_attribute(tmp_path, demo_dir):
    path = make(tmp_path, demo_dir)
    root = main(path)
    neighbor = root.find(w("body"))[1].find(w("ins"))
    neighbor.insert(0, etree.Element(w("proofErr"), {w("type"): "spellStart"}))
    neighbor.set("{urn:fictional:unknown}binding", "must survive")
    path.write_bytes(_package(demo_dir, root))
    report = read_document_revisions(str(path))
    row = next(r for r in report["revisions"] if r["author"] == "Unselected Reviewer")
    assert not row["supported"]
    op = {"kind": "decide_revision", "decision": "reject",
          "target": {"kind": "single", "revision_ref": row["revision_ref"]}}
    before = path.read_bytes()
    assert not drafting.preflight_document_edits(str(path), sha(path), [op], **PROFILE)["batch_applicable"]
    assert path.read_bytes() == before


def with_format_history(tmp_path, demo_dir, *, two=False, fault=None):
    from test_drafting_text import _changed_style

    content = (_r("thirty ", _changed_style(author="Fictional Format Reviewer", identity="81"))
               + _r("days", _changed_style(author="Fictional Format Reviewer", identity="82"))) if two else _r(
                   "thirty days", _changed_style(author="Fictional Format Reviewer", identity="81"))
    paragraph = _p(_r("Pay ") + _ins(content, author=OTHER) + _r(" later"))
    assert _replace(paragraph, "thirty days", "45 days")["operation"] == "counter"
    history = next(paragraph.iter(w("rPrChange")))
    if fault:
        fault(history)
    root = _root("")
    root.find(w("body")).append(paragraph)
    path = tmp_path / "formatted.docx"
    path.write_bytes(_package(demo_dir, root))
    return path


@pytest.mark.parametrize("format_decision", ["accept", "reject"])
@pytest.mark.parametrize("original,counter", [("accept", "accept"), ("accept", "reject"), ("reject", "accept"), ("reject", "reject")])
def test_engine_created_format_history_explicit_prerequisite_then_counter_and_clean(
    tmp_path, demo_dir, format_decision, original, counter
):
    path = with_format_history(tmp_path, demo_dir)
    before = parts(path.read_bytes())
    group = operation(path, original, counter)
    pre = drafting.preflight_document_edits(str(path), sha(path), [group], **PROFILE)
    assert not pre["batch_applicable"]  # No hidden formatting choice.
    row = next(r for r in read_document_revisions(str(path))["revisions"] if r["kind"] == "rPrChange")
    assert row["supported"], row
    save(path, [{"kind": "decide_revision", "decision": format_decision,
                 "target": {"kind": "single", "revision_ref": row["revision_ref"]}}])
    assert not list(main(path).iter(w("rPrChange")))
    save(path, [operation(path, original, counter)])
    expected = "thirty days" if original == "accept" and counter == "reject" else "45 days" if counter == "accept" else ""
    assert texts(main(path)) == ["Pay " + expected + " later"]
    if original == "accept" and counter == "reject":
        selected = next(r for r in main(path).iter(w("r")) if r.find(w("rPr")) is not None)
        assert selected.find(f"{w('rPr')}/{w('i') if format_decision == 'reject' else w('b')}") is not None
    assert all(parts(path.read_bytes())[n] == data for n, data in before.items() if n != "word/document.xml")
    assert prepare_clean_copy(path.read_bytes(), expected_source_sha256=sha(path)).inventory_after["revisions"] == []


def test_clean_explicit_all_accept_handles_multiple_preserved_format_histories(tmp_path, demo_dir):
    path = with_format_history(tmp_path, demo_dir, two=True)
    source = path.read_bytes()
    result = prepare_clean_copy(source, expected_source_sha256=sha(path))
    assert texts(etree.fromstring(parts(result.payload)["word/document.xml"])) == ["Pay 45 days later"]
    assert result.inventory_after["revisions"] == []
    assert path.read_bytes() == source
    rows = read_document_revisions(str(path))["revisions"]
    history = next(r for r in rows if r["kind"] == "rPrChange")
    remaining_before = etree.tostring(list(main(path).iter(w("rPrChange")))[1])
    save(path, [{"kind": "decide_revision", "decision": "reject", "target": {"kind": "single", "revision_ref": history["revision_ref"]}}])
    assert len(list(main(path).iter(w("rPrChange")))) == 1
    assert etree.tostring(next(main(path).iter(w("rPrChange")))) == remaining_before


@pytest.mark.parametrize("fault", ["history_attr", "old_props_attr", "old_leaf_attr", "extra_history", "middle", "unknown_atom"])
def test_format_prerequisite_never_broadens_unknown_history_or_counter_context(tmp_path, demo_dir, fault):
    def damage(history):
        if fault == "history_attr":
            history.set("{urn:fictional:unknown}binding", "preserve")
        elif fault == "old_props_attr":
            history[0].set(w("future"), "preserve")
        elif fault == "old_leaf_attr":
            history[0][0].set("{urn:fictional:unknown}binding", "preserve")
        elif fault == "extra_history":
            history[0].append(copy.deepcopy(history))
        elif fault == "middle":
            outer = next(n for n in history.iterancestors() if n.tag == w("ins"))
            outer.append(_p(_r("unselected trailing text"))[0])
        else:
            etree.SubElement(history.getparent().getparent(), "{urn:fictional:unknown}hidden")
    path = with_format_history(tmp_path, demo_dir, fault=damage)
    source = path.read_bytes()
    root = main(path)
    history = next(root.iter(w("rPrChange")))
    from veqtor_docx.revision_decisions import reference
    op = {"kind": "decide_revision", "decision": "accept", "target": {"kind": "single", "revision_ref": reference(root, history, sha(path))}}
    assert not drafting.preflight_document_edits(str(path), sha(path), [op], **PROFILE)["batch_applicable"]
    assert source == path.read_bytes()
    with pytest.raises(ApplyError):
        prepare_clean_copy(source, expected_source_sha256=sha(path))


def test_counter_format_work_is_charged_before_any_context_clone(tmp_path, demo_dir, monkeypatch):
    from veqtor_docx import counter_revisions, revision_decisions as decisions

    path = with_format_history(tmp_path, demo_dir)
    root = main(path)
    history = next(root.iter(w("rPrChange")))
    monkeypatch.setattr(decisions, "MAX_FINGERPRINT_BYTES", 3 * len(decisions._xml(root)))
    copies = []
    real = counter_revisions.copy.deepcopy
    def meter(node, *args, **kwargs):
        if node is root:
            copies.append(node)
        return real(node, *args, **kwargs)
    monkeypatch.setattr(counter_revisions.copy, "deepcopy", meter)
    with pytest.raises(ApplyError, match="resource_limit_exceeded"):
        counter_revisions.formatting_context(root, history)
    assert not copies
    nodes = [n for n in root.iter() if n.tag in {w("ins"), w("del"), w("rPrChange")}]
    report = read_document_revisions(str(path), offset=nodes.index(history), max_items=1)
    assert not report["revisions"][0]["supported"]
    assert report["revisions"][0]["refusal_code"] == "resource_limit_exceeded"
