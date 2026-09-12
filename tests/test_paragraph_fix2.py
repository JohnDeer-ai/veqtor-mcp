# SPDX-License-Identifier: Apache-2.0
"""Numbering redirect termination and source-bound paragraph-tail preservation."""
from copy import deepcopy
import hashlib

from lxml import etree
import pytest

from veqtor_docx import ApplyError, DocxError, apply_edits, extract_redlines, inspect_document, preflight_edits
from veqtor_docx._ooxml import parse_xml, w
from veqtor_docx.synthetic import generate_demo_rounds
from test_paragraph_edits import AUTHOR, BUILD, apply_module, edit, execute, rewrite, rewrite_parts, rows, xml


def child(parent, tag, **attrs):
    return etree.SubElement(parent, w(tag), {w(key): value for key, value in attrs.items()})


def numbering_source(tmp_path, case):
    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[0]
    original_ref = rows(source)[0]["paragraph_ref"]

    def mutate(parts):
        doc = parse_xml(parts["word/document.xml"])
        styles = parse_xml(parts["word/styles.xml"])
        nums = parse_xml(parts["word/numbering.xml"])
        child(child(doc.find(".//" + w("pPr")), "numPr"), "numId", val="2")
        abstract = child(nums, "abstractNum", abstractNumId="20")
        child(abstract, "numStyleLink", val="ListStyle")
        child(child(nums, "num", numId="2"), "abstractNumId", val="20")
        if case != "missing_style":
            style = child(styles, "style", type="paragraph" if case == "wrong_type" else "numbering",
                          styleId="ListStyle")
            inherited = case in {"inherited", "inherited_without_target", "inherited_wrong_type", "own_zero"}
            if inherited:
                child(style, "basedOn", val="ParentNumbering")
                parent = child(styles, "style", styleId="ParentNumbering",
                               type="character" if case == "inherited_wrong_type" else "numbering")
                if case != "inherited_without_target":
                    child(child(child(parent, "pPr"), "numPr"), "numId", val="1")
            if case == "own_zero":
                child(child(child(style, "pPr"), "numPr"), "numId", val="0")
            if not inherited and case != "without_target":
                number = "2" if case.startswith("self_cycle") else "3" if case == "two_node_cycle" else (
                    "999" if case == "missing_instance" else "1")
                child(child(child(style, "pPr"), "numPr"), "numId", val=number)
            if case == "two_node_cycle":
                other = child(styles, "style", type="numbering", styleId="SecondNumbering")
                child(child(child(other, "pPr"), "numPr"), "numId", val="2")
                child(child(nums, "abstractNum", abstractNumId="30"), "numStyleLink", val="SecondNumbering")
                child(child(nums, "num", numId="3"), "abstractNumId", val="30")
            if "missing_parent" in case:
                child(style, "basedOn", val="AbsentParent")
            if "revision" in case:
                child(child(style, "pPrChange", id="555", author="Other"), "pPr")
            if case == "based_on_cycle":
                child(style, "basedOn", val="ListStyle")
        if case == "missing_abstract":
            nums.find(w("num")).find(w("abstractNumId")).set(w("val"), "999")
        # A reciprocal association on the terminal abstract is legitimate.
        child(nums.find(w("abstractNum")), "styleLink", val="ListStyle")
        parts.update({name: etree.tostring(root) for name, root in (
            ("word/document.xml", doc), ("word/styles.xml", styles), ("word/numbering.xml", nums))})

    rewrite_parts(source, mutate)
    return source, original_ref


@pytest.mark.parametrize("case", [
    "direct", "inherited", "without_target", "inherited_without_target", "wrong_type", "inherited_wrong_type",
    "self_cycle", "two_node_cycle", "missing_style", "missing_instance", "missing_abstract", "own_zero",
    "based_on_cycle", "self_cycle_missing_parent", "self_cycle_revision", "self_cycle_missing_parent_revision",
])
def test_numbering_redirect_must_resolve_to_terminal_definition(tmp_path, case):
    source, original_ref = numbering_source(tmp_path, case)
    before = source.read_bytes()
    if case == "based_on_cycle":
        with pytest.raises(DocxError, match="cycles"):
            rows(source)
        ref = {**original_ref, "file_sha256": hashlib.sha256(before).hexdigest()}
    else:
        ref = rows(source)[0]["paragraph_ref"]
        assert inspect_document(str(source), mode="read", selection={"paragraph_ref": ref})["paragraphs"][0][
            "text"] == "Payment is due within 30 days after receipt."
    edits = [{"target": {"kind": "paragraph", "paragraph_ref": ref},
              "delete_text": "30 days", "insert_text": "45 days"}]
    output = tmp_path / "output.docx"
    if case in {"direct", "inherited"}:
        pre, result = execute(source, output, edits)
        assert result["preflight_binding_status"] == "verified"
        assert result["output_sha256"] == pre["candidate_sha256"]
        output_ref = {**ref, "file_sha256": result["output_sha256"],
                      "paragraph_text_sha256": hashlib.sha256(b"Payment is due within 45 days after receipt.").hexdigest()}
        assert inspect_document(str(output), mode="read", selection={"paragraph_ref": output_ref})[
            "paragraphs"][0]["text"] == "Payment is due within 45 days after receipt."
        units = extract_redlines(str(output))["change_units"]
        assert [(unit["old_text"], unit["new_text"], unit["author"]) for unit in units] == [
            ("30 days", "45 days", AUTHOR)]
    else:
        code = "docx_error" if case == "based_on_cycle" else (
            "paragraph_pending_revisions" if "revision" in case else "paragraph_structure_unsupported")
        pre = preflight_edits(str(source), edits, author=AUTHOR, producer_build=BUILD)
        assert not pre["batch_applicable"] and pre["refusal_code"] == code, pre
        assert pre["preflight_proof"] is None
        with pytest.raises(DocxError) as error:
            apply_edits(str(source), str(output), edits, author=AUTHOR, producer_build=BUILD)
        assert getattr(error.value, "code", "docx_error") == code
        assert not output.exists()
    assert source.read_bytes() == before


def test_removing_masking_defects_never_admits_numbering_cycle(tmp_path):
    source, _ = numbering_source(tmp_path, "self_cycle_missing_parent_revision")
    combined = source.read_bytes()
    for keep_parent, keep_revision in [(True, True), (True, False), (False, True), (False, False)]:
        source.write_bytes(combined)

        def remove_mask(styles):
            style = next(node for node in styles if node.get(w("styleId")) == "ListStyle")
            for tag, keep in (("basedOn", keep_parent), ("pPrChange", keep_revision)):
                if not keep:
                    style.remove(style.find(w(tag)))

        rewrite(source, remove_mask, part="word/styles.xml")
        before = source.read_bytes()
        intended = edit(source)
        inspect_document(str(source), mode="read", selection={"paragraph_ref": intended["target"]["paragraph_ref"]})
        pre = preflight_edits(str(source), [intended], author=AUTHOR, producer_build=BUILD)
        code = "paragraph_pending_revisions" if keep_revision else "paragraph_structure_unsupported"
        assert not pre["batch_applicable"] and pre["refusal_code"] == code and pre["preflight_proof"] is None
        output = tmp_path / "refused.docx"
        with pytest.raises(ApplyError) as error:
            apply_edits(str(source), str(output), [intended], author=AUTHOR, producer_build=BUILD)
        assert error.value.code == code and not output.exists() and source.read_bytes() == before


def mixed_edits(source):
    units = extract_redlines(str(source))["change_units"]
    return [edit(source), edit(source, 1, rows(source)[1]["text"], ""), edit(source, 3),
            {"anchor": units[0]["anchor"], "delete_text": "50 units", "insert_text": "75 units"},
            {"anchor": units[1]["anchor"], "reinstate_text": "inspection right"}]


@pytest.mark.parametrize("fault", ["body_tail", "table_tail", "combined_tails", "legacy_tail",
                                       "whitespace_tail", "paragraph_text"])
def test_surgery_tail_fault_cannot_become_expected_candidate(tmp_path, monkeypatch, fault):
    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[1]
    before = source.read_bytes()
    edits = mixed_edits(source)
    pre, control = execute(source, tmp_path / "control.docx", edits)
    assert len(extract_redlines(control["output_path"])["change_units"]) == 7
    empty_ref = {**edits[1]["target"]["paragraph_ref"], "file_sha256": control["output_sha256"],
                 "paragraph_text_sha256": hashlib.sha256(b"").hexdigest()}
    assert inspect_document(control["output_path"], mode="read", selection={"paragraph_ref": empty_ref})[
        "paragraphs"][0]["text"] == ""
    perform = apply_module._apply_plan

    def corrupt(plan, author):
        perform(plan, author)
        indices = {"body_tail": {0}, "table_tail": {3}, "combined_tails": {0, 3},
                   "legacy_tail": {6}, "whitespace_tail": {0}, "paragraph_text": {0}}[fault]
        if plan.paragraph_index in indices:
            if fault == "paragraph_text":
                plan.paragraph.text = "UNEXPECTED PARAGRAPH TEXT"
            else:
                plan.paragraph.tail = "\n  " if fault == "whitespace_tail" else "UNEXPECTED OUTSIDE PARAGRAPH"

    monkeypatch.setattr(apply_module, "_apply_plan", corrupt)
    refused = preflight_edits(str(source), edits, author=AUTHOR, producer_build=BUILD)
    assert not refused["batch_applicable"] and refused["refusal_code"] == "round_trip_failed"
    assert refused["preflight_proof"] is None
    output = tmp_path / "refused.docx"
    with pytest.raises(ApplyError) as error:
        apply_edits(str(source), str(output), edits, author=AUTHOR, producer_build=BUILD,
                    preflight_proof=pre["preflight_proof"])
    assert error.value.code == "round_trip_failed" and not output.exists() and source.read_bytes() == before


def test_original_whitespace_tails_and_multirun_empty_run_are_preserved(tmp_path):
    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[1]

    def format_source(root):
        paras = list(root.iter(w("p")))
        for index in (0, 3, 6):
            paras[index].tail = "\n  "
        paragraph = paras[0]
        run = paragraph.find(w("r"))
        template = deepcopy(run)
        paragraph.remove(run)
        for text in ("Payment is due within ", "30 ", "days", " after receipt.", ""):
            run = deepcopy(template)
            run.find(w("t")).text = text
            if text == "30 ":
                props = etree.Element(w("rPr"))
                run.insert(0, props)
                child(props, "b")
            paragraph.append(run)

    rewrite(source, format_source)
    before = source.read_bytes()
    _, result = execute(source, tmp_path / "output.docx", mixed_edits(source))
    assert source.read_bytes() == before
    original, output = list(xml(source).iter(w("p"))), list(xml(result["output_path"]).iter(w("p")))
    assert [para.tail for para in original] == [para.tail for para in output]
    assert output[0][-1].find(w("t")).text is None
    assert len(extract_redlines(result["output_path"])["change_units"]) == 7
    assert rows(result["output_path"])[0]["text"] == "Payment is due within 45 days after receipt."


@pytest.mark.parametrize("index", [0, 3])
def test_nonwhitespace_source_tail_is_not_a_clean_paragraph_target(tmp_path, index):
    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[0]
    rewrite(source, lambda root: setattr(list(root.iter(w("p")))[index], "tail", "UNACCOUNTED SOURCE TEXT"))
    intended = edit(source, index)
    ref = intended["target"]["paragraph_ref"]
    inspect_document(str(source), mode="read", selection={"paragraph_ref": ref})
    before = source.read_bytes()
    result = preflight_edits(str(source), [intended], author=AUTHOR, producer_build=BUILD)
    assert not result["batch_applicable"] and result["refusal_code"] == "paragraph_structure_unsupported"
    assert result["preflight_proof"] is None
    with pytest.raises(ApplyError) as error:
        apply_edits(str(source), str(tmp_path / "output.docx"), [intended], author=AUTHOR)
    assert error.value.code == "paragraph_structure_unsupported"
    assert not (tmp_path / "output.docx").exists() and source.read_bytes() == before
