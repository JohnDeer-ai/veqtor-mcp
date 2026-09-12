# SPDX-License-Identifier: Apache-2.0
"""NR-01 exact paragraph edits: source, proof, atomicity and identity guarantees."""

import hashlib
import importlib
import json
from pathlib import Path
import zipfile

import jsonschema
from lxml import etree
import pytest

from veqtor_docx import DocxError, ApplyError, apply_edits, extract_redlines, inspect_document, preflight_edits
from veqtor_docx._ooxml import parse_xml, w
from veqtor_docx.synthetic import generate_demo_rounds
from veqtor_mcp import contracts, records, server

apply_module = importlib.import_module("veqtor_docx.apply")
AUTHOR = "NR-01 Counsel"
BUILD = "source-snapshot-v1-sha256:" + "a" * 64


@pytest.fixture
def corpus(tmp_path):
    return generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")


def rows(path):
    return inspect_document(str(path), mode="browse", max_items=100)["paragraphs"]


def edit(path, index=0, delete="30 days", insert="45 days"):
    ref = next(row["paragraph_ref"] for row in rows(path)
               if row["paragraph_ref"]["paragraph_index"] == index)
    return {"target": {"kind": "paragraph", "paragraph_ref": ref},
            "delete_text": delete, "insert_text": insert}


def execute(source, output, edits, *, author=AUTHOR, build=BUILD):
    pre = preflight_edits(str(source), edits, author=author, producer_build=build)
    assert pre["batch_applicable"], pre
    result = apply_edits(str(source), str(output), edits, author=author,
                         producer_build=build, preflight_proof=pre["preflight_proof"])
    return pre, result


def rewrite(path, mutate, *, part="word/document.xml"):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        parts = {info.filename: archive.read(info) for info in infos}
    root = parse_xml(parts[part])
    mutate(root)
    parts[part] = etree.tostring(root)
    with zipfile.ZipFile(path, "w") as archive:
        for info in infos:
            archive.writestr(info, parts[info.filename])


def xml(path):
    with zipfile.ZipFile(path) as archive:
        return parse_xml(archive.read("word/document.xml"))


def assert_refused(source, edits, code):
    before = source.read_bytes()
    output = source.parent / "refused.docx"
    result = preflight_edits(str(source), edits, author=AUTHOR, producer_build=BUILD)
    assert result["batch_applicable"] is False
    assert result["refusal_code"] == code, result
    assert result["preflight_proof"] is None
    with pytest.raises(ApplyError) as error:
        apply_edits(str(source), str(output), edits, author=AUTHOR)
    assert error.value.code == code
    assert not output.exists()
    assert source.read_bytes() == before
    assert inspect_document(str(source), mode="browse")["file_sha256"] == hashlib.sha256(before).hexdigest()
    return result


@pytest.mark.parametrize("index,delete,insert,expected", [
    (0, "30 days", "45 days", "Payment is due within 45 days after receipt."),
    (1, "optional ", "", "The audit right applies annually."),
    (3, "30 days", "45 days", "Delivery within 45 days."),
    (1, "The optional audit right applies annually.", "", ""),
])
def test_clean_replace_delete_table_and_empty_paragraph(corpus, tmp_path, index, delete, insert, expected):
    source = corpus[0]
    original = source.read_bytes()
    before = rows(source)
    assert extract_redlines(str(source))["change_units"] == []
    intended = edit(source, index, delete, insert)
    pre, result = execute(source, tmp_path / "out.docx", [intended])
    assert source.read_bytes() == original
    assert result["output_sha256"] == pre["candidate_sha256"]
    assert result["applied"][0]["target"] == intended["target"]
    assert "change_unit_id" not in result["applied"][0]
    assert pre["edits"][0]["change_unit_id"] is None
    assert pre["edits"][0]["target"] == intended["target"]
    out = Path(result["output_path"])
    out_ref = {**intended["target"]["paragraph_ref"], "file_sha256": result["output_sha256"],
               "paragraph_text_sha256": hashlib.sha256(expected.encode()).hexdigest()}
    read = inspect_document(str(out), mode="read", selection={"paragraph_ref": out_ref})
    assert read["paragraphs"][0]["text"] == expected
    actual_units = extract_redlines(str(out))["change_units"]
    assert len(actual_units) == 1
    unit = actual_units[0]
    assert (unit["old_text"], unit["new_text"], unit["author"], unit["reference"]["paragraph_index"]) == (
        delete, insert or None, AUTHOR, index)
    assert unit["reference"]["revision_ids"] == result["applied"][0]["tracked_revision_ids"]
    original_paras, candidate_paras = list(xml(source).iter(w("p"))), list(xml(out).iter(w("p")))
    assert len(original_paras) == len(candidate_paras) == len(before)
    for i, (a, b) in enumerate(zip(original_paras, candidate_paras)):
        if i != index:
            assert etree.tostring(a) == etree.tostring(b)
    with zipfile.ZipFile(source) as a, zipfile.ZipFile(out) as b:
        assert a.namelist() == b.namelist()
        assert all(a.read(name) == b.read(name) for name in a.namelist() if name != "word/document.xml")


def test_replacement_inherits_run_format_and_preserves_split_format(corpus, tmp_path):
    source = corpus[0]
    def mutate(root):
        run = root.find(".//" + w("r"))
        props = etree.Element(w("rPr"))
        etree.SubElement(props, w("b"))
        etree.SubElement(props, w("color")).set(w("val"), "126633")
        run.insert(0, props)
    rewrite(source, mutate)
    _, result = execute(source, tmp_path / "out.docx", [edit(source)])
    first = xml(source).find(".//" + w("rPr"))
    after = xml(result["output_path"])
    props = after.find(".//" + w("ins") + "/" + w("r") + "/" + w("rPr"))
    assert etree.tostring(first) == etree.tostring(props)


@pytest.mark.parametrize("field,value,code", [
    ("file_sha256", "f" * 64, "file_sha256_mismatch"),
    ("paragraph_text_sha256", "f" * 64, "reference_mismatch"),
    ("paragraph_index", 99, "reference_not_found"),
    ("paragraph_index", 1, "reference_mismatch"),
    ("paragraph_index", True, "invalid_reference"),
    ("reading_mode", "rejected_pending_v1", "reference_mismatch"),
    ("container_policy", "other", "reference_mismatch"),
    ("part_name", "word/header1.xml", "reference_mismatch"),
    ("schema_version", "paragraph_ref.v2", "reference_mismatch"),
    ("ref_type", "section", "reference_mismatch"),
])
def test_reference_refusals(corpus, field, value, code):
    intended = edit(corpus[0])
    intended["target"]["paragraph_ref"][field] = value
    assert_refused(corpus[0], [intended], code)


@pytest.mark.parametrize("mutation", [
    lambda e: e.update(anchor={"change_unit_id": "cu_001", "file_sha256": "a" * 64}),
    lambda e: e.update(reinstate_text="30 days"),
    lambda e: e["target"].update(extra=True),
    lambda e: e["target"].update(kind="change_unit"),
    lambda e: e.update(target=None),
    lambda e: e.update(extra=True),
])
def test_closed_target_shapes(corpus, mutation):
    intended = edit(corpus[0])
    mutation(intended)
    assert_refused(corpus[0], [intended], "invalid_edit")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(intended, contracts.EDIT_INPUT_SCHEMA)


def test_missing_ref_field_and_insertion_only(corpus):
    intended = edit(corpus[0])
    del intended["target"]["paragraph_ref"]["container_policy"]
    assert_refused(corpus[0], [intended], "invalid_reference")
    intended = edit(corpus[0], delete="")
    assert_refused(corpus[0], [intended], "delete_text_missing")


def test_ambiguity_is_local_and_overlapping_occurrences_count(corpus):
    assert_refused(corpus[0], [edit(corpus[0], 5)], "delete_text_ambiguous")
    assert_refused(corpus[0], [edit(corpus[0], delete="not present")], "delete_text_not_found")
    def mutate(root):
        root.find(".//" + w("t")).text = "aaaa"
    rewrite(corpus[0], mutate)
    assert_refused(corpus[0], [edit(corpus[0], delete="aaa")], "delete_text_ambiguous")


@pytest.mark.parametrize("tag", [w(name) for name in (
    "ins", "del", "moveFrom", "moveTo", "rPrChange", "pPrChange", "tblPrChange",
    "trPrChange", "tcPrChange", "sectPrChange", "numberingChange", "cellIns", "cellDel",
    "cellMerge", "tblGridChange", "tblPrExChange", "conflictIns", "conflictDel",
    "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd",
    "customXmlInsRangeStart", "customXmlInsRangeEnd", "customXmlDelRangeStart",
    "customXmlDelRangeEnd", "customXmlMoveFromRangeStart", "customXmlMoveFromRangeEnd",
    "customXmlMoveToRangeStart", "customXmlMoveToRangeEnd",
    "customXmlConflictInsertionRangeStart", "customXmlConflictInsertionRangeEnd",
    "customXmlConflictDeletionRangeStart", "customXmlConflictDeletionRangeEnd",
)])
def test_every_revision_marker_refused_in_target(corpus, tag):
    def mutate(root):
        para = root.find(".//" + w("p"))
        props = para.find(w("pPr"))
        mark_props = etree.SubElement(props, w("rPr"))
        node = etree.SubElement(mark_props, tag)
        node.set(w("id"), "900")
        node.set(w("author"), "Counterparty")
    rewrite(corpus[0], mutate)
    # Even revisions excluded by the read projection must be refused for writing.
    assert_refused(corpus[0], [edit(corpus[0])], "paragraph_pending_revisions")


@pytest.mark.parametrize("container,property_name,revision", [
    ("tc", "tcPr", "cellMerge"), ("tr", "trPr", "ins"),
    ("tbl", "tblPr", "tblPrChange"), ("tbl", "tblGrid", "tblGridChange"),
])
def test_table_ancestor_revision_refused(corpus, container, property_name, revision):
    def mutate(root):
        node = root.find(".//" + w(container))
        props = node.find(w(property_name))
        if props is None:
            props = etree.SubElement(node, w(property_name))
        etree.SubElement(props, w(revision)).set(w("id"), "901")
    rewrite(corpus[0], mutate)
    assert_refused(corpus[0], [edit(corpus[0], 3)], "paragraph_pending_revisions")


@pytest.mark.parametrize("tag", ["hyperlink", "sdt", "fldSimple", "commentRangeStart", "bookmarkStart"])
def test_unsupported_inline_structure_refused(corpus, tag):
    rewrite(corpus[0], lambda root: etree.SubElement(root.find(".//" + w("p")), w(tag)))
    assert_refused(corpus[0], [edit(corpus[0])], "paragraph_structure_unsupported")


def test_pending_text_on_unedited_part_of_paragraph_refused(corpus):
    assert_refused(corpus[1], [edit(corpus[1], 6, "Limit: ", "Cap: ")], "paragraph_pending_revisions")


def test_mixed_batch_and_legacy_counter_reinstate(corpus, tmp_path):
    source = corpus[1]
    units = extract_redlines(str(source))["change_units"]
    edits = [edit(source), edit(source, 3)] + [
        {"anchor": units[0]["anchor"], "delete_text": "50 units", "insert_text": "75 units"},
        {"anchor": units[1]["anchor"], "reinstate_text": "inspection right"},
    ]
    _, result = execute(source, tmp_path / "mixed.docx", edits)
    assert [item["operation"] for item in result["applied"]] == ["replace", "replace", "counter", "reinstate"]
    assert len(extract_redlines(result["output_path"])["change_units"]) == len(units) + 4


def test_bad_or_overlapping_edit_blocks_entire_batch(corpus):
    source = corpus[1]
    anchor = extract_redlines(str(source))["change_units"][0]["anchor"]
    legacy = {"anchor": anchor, "delete_text": "50 units", "insert_text": "75 units"}
    assert_refused(source, [legacy, edit(source, delete="missing")], "delete_text_not_found")
    assert_refused(source, [legacy, edit(source), edit(source, delete="within 30 days")], "edits_overlap")
    assert_refused(source, [legacy, edit(source, 6, "Limit: ", "Cap: ")], "paragraph_pending_revisions")


@pytest.mark.parametrize("mutation", ["author", "build", "order", "text", "kind", "source"])
def test_proof_cannot_be_reused_after_input_changes(corpus, tmp_path, mutation):
    source = corpus[0]
    edits = [edit(source), edit(source, 1, "optional ", "")]
    pre = preflight_edits(str(source), edits, author=AUTHOR, producer_build=BUILD)
    assert pre["batch_applicable"]
    author, build = AUTHOR, BUILD
    if mutation == "author":
        author += " other"
    elif mutation == "build":
        build += " other"
    elif mutation == "order":
        edits.reverse()
    elif mutation == "text":
        edits[0]["insert_text"] = "60 days"
    elif mutation == "kind":
        edits[0]["target"]["kind"] = "other"
    else:
        rewrite(source, lambda root: root.set("changed", "yes"))
    output = tmp_path / "never.docx"
    before = source.read_bytes()
    with pytest.raises(ApplyError):
        apply_edits(str(source), str(output), edits, author=author, producer_build=build,
                    preflight_proof=pre["preflight_proof"])
    assert not output.exists() and source.read_bytes() == before


def test_existing_output_not_overwritten(corpus, tmp_path):
    out = tmp_path / "output.docx"
    out.write_bytes(b"existing")
    with pytest.raises(ApplyError, match="output_exists"):
        execute(corpus[0], out, [edit(corpus[0])])
    assert out.read_bytes() == b"existing"


@pytest.mark.parametrize("fault", ["full_text", "format", "insert_format", "neighbour", "package"])
def test_serialized_candidate_corruption_is_atomic(corpus, monkeypatch, fault):
    original = apply_module._output_archive_bytes
    def corrupt(infos, parts):
        parts = dict(parts)
        if fault == "package":
            parts["docProps/core.xml"] += b" "
        else:
            root = parse_xml(parts["word/document.xml"])
            if fault == "full_text":
                root.find(".//" + w("p") + "/" + w("r") + "/" + w("t")).text = "corrupt collateral"
            elif fault == "insert_format":
                run = root.find(".//" + w("ins") + "/" + w("r"))
                etree.SubElement(etree.SubElement(run, w("rPr")), w("b"))
            elif fault == "format":
                root.find(".//" + w("pPr") + "/" + w("pStyle")).set(w("val"), "Wrong")
            else:
                list(root.iter(w("p")))[2].set("changed", "yes")
            parts["word/document.xml"] = etree.tostring(root)
        return original(infos, parts)
    monkeypatch.setattr(apply_module, "_output_archive_bytes", corrupt)
    assert_refused(corpus[0], [edit(corpus[0])], "round_trip_failed")


def test_shared_immutable_source_read(corpus, tmp_path, monkeypatch):
    source = corpus[0]
    edits = [edit(source)]
    payload = source.read_bytes()
    original = apply_module.read_docx_payload
    calls = []
    def read_once(path):
        calls.append(path)
        data = original(path)
        source.write_bytes(b"external replacement after capture")
        return data
    monkeypatch.setattr(apply_module, "read_docx_payload", read_once)
    result = preflight_edits(str(source), edits, author=AUTHOR, producer_build=BUILD)
    assert len(calls) == 1
    assert result["batch_applicable"]
    assert result["source_sha256"] == hashlib.sha256(payload).hexdigest()


def test_mcp_schema_journal_export_preserve_target(corpus, tmp_path, monkeypatch):
    monkeypatch.delenv("VEQTOR_DISABLE_DECISION_RECORD", raising=False)
    source = corpus[0]
    edits = [edit(source), edit(source, 1, "optional ", "")]
    for intended in edits:
        jsonschema.validate(intended, contracts.EDIT_INPUT_SCHEMA)
    pre = server.preflight_edits(str(source), edits)
    result = server.apply_edits(str(source), str(tmp_path / "output.docx"), edits,
                               preflight_proof=pre["preflight_proof"])
    assert pre["record_status"] == result["record_status"] == "written"
    jsonschema.validate(pre, contracts.PREFLIGHT_EDITS_RESULT_SCHEMA)
    jsonschema.validate(result, contracts.APPLY_EDITS_RESULT_SCHEMA)
    raw = [json.loads(line) for line in records.journal_path(source.parent).read_text().splitlines()]
    applied = next(row for row in raw if row["tool_name"] == "apply_edits")
    assert applied["input"]["edits"] == edits
    assert applied["provenance"]["applied"][0]["target"] == edits[0]["target"]
    export = server.export_decision_record(str(source.parent))
    compact = next(row for row in export["records"] if row["tool_name"] == "apply_edits")
    sample = compact["result"]["applied"]["sample"]
    assert [item["target"] for item in sample] == [item["target"] for item in edits]
    assert all("change_unit_id" not in item for item in sample)
    assert "optional" not in json.dumps(export)


def test_schema_rejects_paragraph_result_disguised_as_change_unit(corpus):
    intended = edit(corpus[0])
    item = {"target": intended["target"], "change_unit_id": "cu_001", "operation": "delete",
            "deleted_text": "30 days", "inserted_text": None, "tracked_revision_ids": ["101"]}
    # A legacy result must not also claim paragraph identity.
    schema = contracts.APPLY_EDITS_RESULT_SCHEMA["properties"]["applied"]["items"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(item, schema)


def test_nonoverlapping_same_paragraph_and_omitted_insertion(corpus, tmp_path):
    source = corpus[0]
    delete = edit(source, 0, "after receipt", "")
    del delete["insert_text"]
    edits = [edit(source), delete]
    _, result = execute(source, tmp_path / "output.docx", edits)
    assert rows(result["output_path"])[0]["text"] == "Payment is due within 45 days ."
    assert [item["operation"] for item in result["applied"]] == ["replace", "delete"]


@pytest.mark.parametrize("case", ["neighbour_mark", "styles", "numbering", "section", "unknown_property", "extension_conflict"])
def test_applicable_revision_and_unknown_property_boundaries(corpus, case):
    source = corpus[0]
    target_index = 1 if case == "neighbour_mark" else 0
    if case in {"styles", "numbering"}:
        rewrite(source, lambda root: etree.SubElement(root, w("rPrChange")), part=f"word/{case}.xml")
    else:
        def mutate(root):
            if case == "section":
                etree.SubElement(root.find(".//" + w("sectPr")), w("sectPrChange"))
                return
            props = root.find(".//" + w("pPr"))
            if case == "neighbour_mark":
                props = etree.SubElement(props, w("rPr"))
            name = {"neighbour_mark": w("del"), "unknown_property": w("futureProperty"),
                    "extension_conflict": "{http://schemas.microsoft.com/office/word/2010/wordml}conflictIns"}[case]
            etree.SubElement(props, name)
        rewrite(source, mutate)
    assert_refused(source, [edit(source, target_index, "optional " if target_index else "30 days")],
                   "paragraph_structure_unsupported" if case == "unknown_property" else "paragraph_pending_revisions")


def test_new_target_mcp_transport_and_controlled_refusal(corpus, tmp_path, monkeypatch):
    import asyncio
    from mcp.client import Client

    monkeypatch.delenv("VEQTOR_DISABLE_DECISION_RECORD", raising=False)
    source = corpus[0]
    original = source.read_bytes()

    async def scenario():
        async with Client(server.mcp) as session:
            good = [edit(source)]
            bad = [edit(source)]
            bad[0]["target"]["paragraph_ref"]["reading_mode"] = "unsupported"
            response = await session.call_tool("preflight_edits", {"source_path": str(source), "edits": bad})
            payload = response.structured_content
            assert payload["batch_applicable"] is False
            assert payload["refusal_code"] == "reference_mismatch"
            assert payload["record_status"] == "written"
            response = await session.call_tool("preflight_edits", {"source_path": str(source), "edits": good})
            pre = response.structured_content
            assert pre["batch_applicable"] is True
            response = await session.call_tool("apply_edits", {
                "source_path": str(source), "output_path": str(tmp_path / "transport.docx"),
                "edits": good, "preflight_proof": pre["preflight_proof"],
            })
            result = response.structured_content
            assert result["applied"][0]["target"] == good[0]["target"]
            assert result["output_sha256"] == pre["candidate_sha256"]
            response = await session.call_tool("inspect_document", {"path": str(source), "mode": "browse"})
            assert response.structured_content["file_sha256"] == hashlib.sha256(original).hexdigest()
    asyncio.run(scenario())


def test_identical_full_deletions_refuse_before_format_planning(corpus):
    source = corpus[0]
    text = rows(source)[0]["text"]
    intended = edit(source, 0, text, "")
    refused = assert_refused(source, [intended, intended], "edits_overlap")
    assert refused["failure_phase"] == "planning"
    assert refused["blocking_edit_index"] == 1


@pytest.mark.parametrize("kind,target,mode", [
    ("styles", "alternate-styles.xml", "Internal"),
    ("numbering", "alternate-numbering.xml", "Internal"),
    ("styles", "https://example.invalid/styles.xml", "External"),
])
def test_unresolved_formatting_dependencies_refuse(corpus, kind, target, mode):
    source = corpus[0]
    def mutate(root):
        rel = next(rel for rel in root if rel.get("Type", "").endswith("/" + kind))
        rel.set("Target", target)
        rel.set("TargetMode", mode)
    rewrite(source, mutate, part="word/_rels/document.xml.rels")
    assert_refused(source, [edit(source)], "paragraph_structure_unsupported")


def rewrite_parts(path, mutate):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        parts = {info.filename: archive.read(info) for info in infos}
    mutate(parts)
    with zipfile.ZipFile(path, "w") as archive:
        for info in infos:
            if info.filename in parts:
                archive.writestr(info, parts[info.filename])


@pytest.mark.parametrize("case", [
    "styles_part_and_rel", "styles_rel", "styles_part", "rels_part",
    "numbering_part_and_rel", "numbering_rel", "missing_parent", "missing_numbering",
    "missing_abstract", "combined_missing", "combined_missing_with_revision",
    "known_property_revision", "cycle", "missing_run_style", "missing_table_style",
    "missing_linked_style", "missing_default_parent", "supported_chain",
])
def test_write_dependency_closure_preserves_tolerant_reading(corpus, tmp_path, case):
    source = corpus[0]
    original_ref = rows(source)[0]["paragraph_ref"]

    def mutate(parts):
        styles = parse_xml(parts["word/styles.xml"])
        base = styles[0]
        parent = etree.SubElement(styles, w("style"), {w("type"): "paragraph", w("styleId"): "Parent"})
        etree.SubElement(parent, w("name"), {w("val"): "Normal inherited style vocabulary"})
        etree.SubElement(etree.SubElement(parent, w("pPr")), w("keepNext"))
        etree.SubElement(base, w("basedOn"), {w("val"): "Parent"})
        num = etree.SubElement(base.find(w("pPr")), w("numPr"))
        etree.SubElement(num, w("numId"), {w("val"): "1"})
        etree.SubElement(num, w("ilvl"), {w("val"): "0"})
        if case in {"missing_parent", "combined_missing", "combined_missing_with_revision"}:
            styles.remove(parent)
        if case in {"missing_numbering", "combined_missing", "combined_missing_with_revision"}:
            num.find(w("numId")).set(w("val"), "999")
        if case == "missing_abstract":
            numbering = parse_xml(parts["word/numbering.xml"])
            numbering.find(w("num")).find(w("abstractNumId")).set(w("val"), "999")
            parts["word/numbering.xml"] = etree.tostring(numbering)
        if case == "cycle":
            etree.SubElement(parent, w("basedOn"), {w("val"): "VBody"})
        if case in {"known_property_revision", "combined_missing_with_revision"}:
            etree.SubElement(etree.SubElement(base.find(w("pPr")), w("pPrChange"),
                                             {w("id"): "500", w("author"): "Other"}), w("pPr"))
        if case == "missing_linked_style":
            etree.SubElement(base, w("link"), {w("val"): "AbsentCharacter"})
        if case == "missing_default_parent":
            default = etree.SubElement(styles, w("style"), {
                w("type"): "character", w("default"): "1", w("styleId"): "DefaultCharacter"})
            etree.SubElement(default, w("basedOn"), {w("val"): "AbsentCharacter"})
        parts["word/styles.xml"] = etree.tostring(styles)
        if case == "missing_run_style":
            document = parse_xml(parts["word/document.xml"])
            run = document.find(".//" + w("r"))
            props = etree.Element(w("rPr"))
            run.insert(0, props)
            etree.SubElement(props, w("rStyle"), {w("val"): "AbsentCharacter"})
            parts["word/document.xml"] = etree.tostring(document)
        if case == "missing_table_style":
            document = parse_xml(parts["word/document.xml"])
            etree.SubElement(document.find(".//" + w("tblPr")), w("tblStyle"), {w("val"): "AbsentTable"})
            parts["word/document.xml"] = etree.tostring(document)
        kind = "styles" if case.startswith("styles_") else "numbering"
        if case.endswith("_part_and_rel") or case in {"styles_rel", "numbering_rel"}:
            rels = parse_xml(parts["word/_rels/document.xml.rels"])
            for rel in list(rels):
                if rel.get("Type").endswith("/" + kind):
                    rels.remove(rel)
            parts["word/_rels/document.xml.rels"] = etree.tostring(rels)
        if case.endswith("_part_and_rel") or case == "styles_part":
            parts.pop(f"word/{kind}.xml")
        if case == "rels_part":
            parts.pop("word/_rels/document.xml.rels")

    rewrite_parts(source, mutate)
    before = source.read_bytes()
    if case == "cycle":
        ref = {**original_ref, "file_sha256": hashlib.sha256(before).hexdigest()}
        with pytest.raises(DocxError, match="cycles"):
            rows(source)
    else:
        ref = rows(source)[3 if case == "missing_table_style" else 0]["paragraph_ref"]
        assert inspect_document(str(source), mode="read", selection={"paragraph_ref": ref})["paragraphs"]
    edits = [{"target": {"kind": "paragraph", "paragraph_ref": ref},
              "delete_text": "30 days", "insert_text": "45 days"}]
    output = tmp_path / "dependency-output.docx"
    if case == "supported_chain":
        _, result = execute(source, output, edits)
        assert result["round_trip_check"]["status"] == "passed"
    else:
        code = "docx_error" if case == "cycle" else (
            "paragraph_pending_revisions" if case in {"known_property_revision", "combined_missing_with_revision"}
            else "paragraph_structure_unsupported")
        pre = preflight_edits(str(source), edits, author=AUTHOR, producer_build=BUILD)
        assert not pre["batch_applicable"] and pre["refusal_code"] == code, pre
        assert pre["preflight_proof"] is None
        with pytest.raises(DocxError) as error:
            apply_edits(str(source), str(output), edits, author=AUTHOR, producer_build=BUILD)
        assert getattr(error.value, "code", "docx_error") == code
        if case == "cycle":
            assert pre["failure_phase"] == "source"
        assert not output.exists()
    assert source.read_bytes() == before


def inject_paragraph_structure(root, fault):
    para = root.find(".//" + w("p"))
    if fault in {"duplicate_ppr", "combined"}:
        etree.SubElement(etree.SubElement(para, w("pPr")), w("pageBreakBefore"))
    if fault in {"drawing", "combined"}:
        etree.SubElement(para, w("drawing"))
    if fault in {"bookmarks", "combined"}:
        etree.SubElement(para, w("bookmarkStart"), {w("id"): "987", w("name"): "UnexpectedBookmark"})
        etree.SubElement(para, w("bookmarkEnd"), {w("id"): "987"})
    if fault == "empty_run":
        etree.SubElement(etree.SubElement(para, w("r")), w("t"))
    if fault == "wrapper_attribute":
        para.find(w("ins")).set(w("unexpected"), "yes")
    if fault == "current_text":
        para.find(w("r") + "/" + w("t")).text = "corrupted unedited prefix "


@pytest.mark.parametrize("fault", ["duplicate_ppr", "drawing", "bookmarks", "combined",
                                       "empty_run", "wrapper_attribute", "current_text"])
def test_serialized_structure_rejected_by_preflight_and_proof_bound_apply(corpus, tmp_path, monkeypatch, fault):
    source = corpus[1]
    before = source.read_bytes()
    units = extract_redlines(str(source))["change_units"]
    edits = [edit(source), edit(source, 1, rows(source)[1]["text"], ""), edit(source, 3),
             {"anchor": units[0]["anchor"], "delete_text": "50 units", "insert_text": "75 units"},
             {"anchor": units[1]["anchor"], "reinstate_text": "inspection right"}]
    pre, control = execute(source, tmp_path / "control.docx", edits)
    assert len(control["applied"]) == 5
    assert len(extract_redlines(control["output_path"])["change_units"]) == 7
    empty_ref = {**edits[1]["target"]["paragraph_ref"], "file_sha256": control["output_sha256"],
                 "paragraph_text_sha256": hashlib.sha256(b"").hexdigest()}
    assert inspect_document(control["output_path"], mode="read", selection={"paragraph_ref": empty_ref})[
        "paragraphs"][0]["text"] == ""
    original = apply_module._output_archive_bytes

    def corrupt(infos, parts):
        changed = dict(parts)
        root = parse_xml(parts["word/document.xml"])
        inject_paragraph_structure(root, fault)
        changed["word/document.xml"] = etree.tostring(root)
        return original(infos, changed)

    monkeypatch.setattr(apply_module, "_output_archive_bytes", corrupt)
    refused = preflight_edits(str(source), edits, author=AUTHOR, producer_build=BUILD)
    assert not refused["batch_applicable"] and refused["refusal_code"] == "round_trip_failed"
    assert refused["round_trip_check"]["status"] == "failed"
    assert refused["preflight_proof"] is None
    output = tmp_path / "refused.docx"
    with pytest.raises(ApplyError) as error:
        apply_edits(str(source), str(output), edits, author=AUTHOR, producer_build=BUILD,
                    preflight_proof=pre["preflight_proof"])
    assert error.value.code == "round_trip_failed"
    assert not output.exists() and source.read_bytes() == before
