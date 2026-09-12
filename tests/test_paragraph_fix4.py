# SPDX-License-Identifier: Apache-2.0
"""Document-spanning inline context and significant whitespace regressions."""
import hashlib
import zipfile

from lxml import etree
import pytest

from veqtor_docx import ApplyError, apply_edits, preflight_edits
from veqtor_docx._ooxml import parse_xml, w
from veqtor_docx.synthetic import generate_demo_rounds
from test_paragraph_edits import AUTHOR, BUILD, apply_module, edit, execute, rewrite, rows, xml

SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def child(parent, name, **attrs):
    return etree.SubElement(parent, w(name), {w(key): value for key, value in attrs.items()})


def context(root, kind, *, target_index=0, unrelated=False):
    body = root.find(w("body"))
    target = list(root.iter(w("p")))[target_index]
    # Non-adjacent boundaries; table targets stay within their table/cell.
    before, after = etree.Element(w("p")), etree.Element(w("p"))
    if kind.startswith("field"):
        child(child(before, "r"), "fldChar", fldCharType="begin")
        child(child(before, "r"), "instrText").text = " DOCPROPERTY Title "
        if kind != "field_code":
            child(child(before, "r"), "fldChar", fldCharType="separate")
        child(child(after, "r"), "fldChar", fldCharType="end")
        if kind == "field_nested":
            child(child(before, "r"), "fldChar", fldCharType="begin")
            child(child(after, "r"), "fldChar", fldCharType="end")
        elif kind == "field_unclosed":
            after.clear()
        elif kind == "field_orphan_end":
            before.clear()
        elif kind == "field_orphan_separate":
            before.clear()
            child(child(before, "r"), "fldChar", fldCharType="separate")
        elif kind == "field_duplicate_separate":
            child(child(before, "r"), "fldChar", fldCharType="separate")
        elif kind == "field_invalid_type":
            before.find(".//" + w("fldChar")).set(w("fldCharType"), "unknown")
        elif kind == "field_other_story":
            outer = child(child(before, "r"), "drawing")
            other = child(child(outer, "txbxContent"), "p")
            for run in list(before)[:-1]:
                other.append(run)
    else:
        child(before, "commentRangeStart", id="0")
        child(after, "commentRangeEnd", id="0")
        child(child(after, "r"), "commentReference", id="0")
        if kind == "comment_unclosed":
            after.clear()
        elif kind == "comment_orphan_end":
            before.clear()
        elif kind == "comment_duplicate":
            child(before, "commentRangeStart", id="00")
        elif kind == "comment_mismatched":
            after.find(w("commentRangeEnd")).set(w("id"), "1")
        elif kind == "comment_missing_id":
            before[0].attrib.clear()
        elif kind == "comment_overlap":
            child(before, "commentRangeStart", id="1")
            child(after, "commentRangeEnd", id="1")
    if unrelated:
        body.insert(0, before)
        body.insert(1, after)
    else:
        parent = target.getparent()
        parent.insert(parent.index(target), before)
        parent.insert(parent.index(target), etree.Element(w("p")))
        parent.insert(parent.index(target) + 1, etree.Element(w("p")))
        parent.insert(parent.index(target) + 2, after)


def refused_with_bound_proof(source, intended, proof, output):
    before = source.read_bytes()
    proof = {**proof, "source_sha256": hashlib.sha256(before).hexdigest(),
             "edits_sha256": apply_module._canonical_digest([intended])}
    proof["proof_sha256"] = apply_module._canonical_digest({
        key: value for key, value in proof.items() if key != "proof_sha256"})
    pre = preflight_edits(str(source), [intended], author=AUTHOR, producer_build=BUILD)
    assert not pre["batch_applicable"] and pre["refusal_code"] == "paragraph_structure_unsupported", pre
    assert pre["preflight_proof"] is None
    with pytest.raises(ApplyError) as caught:
        apply_edits(str(source), str(output), [intended], author=AUTHOR,
                    producer_build=BUILD, preflight_proof=proof)
    assert caught.value.code == "paragraph_structure_unsupported"
    assert caught.value.metadata["observed_source_sha256"] == hashlib.sha256(before).hexdigest()
    assert source.read_bytes() == before and not output.exists()
    assert not list(output.parent.glob("*.veqtor-tmp"))


def comments_package(source):
    with zipfile.ZipFile(source) as archive:
        parts = {info.filename: archive.read(info) for info in archive.infolist()}
    comments = etree.Element(w("comments"))
    for identity in ("0", "1"):
        comment = child(comments, "comment", id=identity, author="Synthetic reviewer")
        child(child(child(comment, "p"), "r"), "t").text = "Comment range test"
    parts["word/comments.xml"] = etree.tostring(comments)
    rels = parse_xml(parts["word/_rels/document.xml.rels"])
    etree.SubElement(rels, "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship",
                     Id="rIdComments", Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
                     Target="comments.xml")
    parts["word/_rels/document.xml.rels"] = etree.tostring(rels)
    types = parse_xml(parts["[Content_Types].xml"])
    etree.SubElement(types, "{http://schemas.openxmlformats.org/package/2006/content-types}Override",
                     PartName="/word/comments.xml",
                     ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml")
    parts["[Content_Types].xml"] = etree.tostring(types)
    with zipfile.ZipFile(source, "w") as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)


@pytest.mark.parametrize("kind", ["field_result", "field_code", "field_nested", "field_unclosed",
    "field_orphan_end", "field_orphan_separate", "field_duplicate_separate", "field_invalid_type",
    "field_other_story", "comment_range", "comment_unclosed", "comment_orphan_end",
    "comment_duplicate", "comment_mismatched", "comment_missing_id", "comment_overlap"])
@pytest.mark.parametrize("target_index", [0, 3])
def test_cross_paragraph_context_refuses_before_publication(tmp_path, kind, target_index):
    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[0]
    proof = preflight_edits(str(source), [edit(source, target_index)], author=AUTHOR, producer_build=BUILD)["preflight_proof"]
    rewrite(source, lambda root: context(root, kind, target_index=target_index))
    if kind.startswith("comment"):
        comments_package(source)
    index = next(row["paragraph_ref"]["paragraph_index"] for row in rows(source)
                 if row["text"].startswith("Payment" if target_index == 0 else "Delivery"))
    refused_with_bound_proof(source, edit(source, index), proof, tmp_path / "never.docx")


@pytest.mark.parametrize("kind", ["field_result", "field_code", "field_nested", "comment_range", "comment_overlap"])
def test_balanced_context_away_from_plain_target_is_allowed(tmp_path, kind):
    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[0]
    rewrite(source, lambda root: context(root, kind, unrelated=True))
    if kind.startswith("comment"):
        comments_package(source)
    _, result = execute(source, tmp_path / "output.docx", [edit(source, 2)])
    assert result["round_trip_check"]["status"] == "passed"


@pytest.mark.parametrize("mode", [None, "default", "preserve", "invalid"])
@pytest.mark.parametrize("location", ["atom", "run", "paragraph", "body", "document"])
def test_source_edge_whitespace_requires_effective_preservation(tmp_path, mode, location):
    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[0]
    proof = preflight_edits(str(source), [edit(source)], author=AUTHOR, producer_build=BUILD)["preflight_proof"]
    def mutate(root):
        atom = root.find(".//" + w("t"))
        atom.text = " " * 20 + atom.text + " " * 20
        atom.attrib.pop(SPACE, None)
        node = {"atom": atom, "run": atom.getparent(), "paragraph": atom.getparent().getparent(),
                "body": root.find(w("body")), "document": root}[location]
        if mode is not None:
            node.set(SPACE, mode)
    rewrite(source, mutate)
    if mode == "preserve":
        _, result = execute(source, tmp_path / "output.docx", [edit(source)])
        assert rows(result["output_path"])[0]["text"] == " " * 20 + "Payment is due within 45 days after receipt." + " " * 20
    else:
        refused_with_bound_proof(source, edit(source), proof, tmp_path / "never.docx")


@pytest.mark.parametrize("mode", [None, "default", "preserve"])
def test_split_preserves_newly_exposed_significant_spaces(tmp_path, mode):
    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[0]
    def mutate(root):
        atom = root.find(".//" + w("t"))
        atom.attrib.pop(SPACE, None)
        if mode is not None:
            atom.set(SPACE, mode)
    rewrite(source, mutate)
    _, result = execute(source, tmp_path / "output.docx", [edit(source)])
    first = xml(result["output_path"]).find(".//" + w("t"))
    assert first.text == "Payment is due within " and first.get(SPACE) == "preserve"


@pytest.mark.parametrize("side", ["original", "deleted", "inserted"])
def test_surgery_space_fault_cannot_become_the_expected_candidate(tmp_path, monkeypatch, side):
    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[0]
    before = source.read_bytes()
    intended = edit(source, 0, "30 days ", "45 days ")
    pre, _ = execute(source, tmp_path / "control.docx", [intended])
    perform = apply_module._apply_plan
    def corrupt(plan, author):
        perform(plan, author)
        node = {"original": plan.paragraph.find(w("r") + "/" + w("t")),
                "deleted": plan.paragraph.find(w("del") + "/" + w("r") + "/" + w("delText")),
                "inserted": plan.paragraph.find(w("ins") + "/" + w("r") + "/" + w("t"))}[side]
        assert node.text.endswith(" ")
        node.attrib.pop(SPACE)
    monkeypatch.setattr(apply_module, "_apply_plan", corrupt)
    refused = preflight_edits(str(source), [intended], author=AUTHOR, producer_build=BUILD)
    assert not refused["batch_applicable"] and refused["refusal_code"] == "round_trip_failed"
    assert refused["preflight_proof"] is None
    output = tmp_path / "never.docx"
    with pytest.raises(ApplyError) as caught:
        apply_edits(str(source), str(output), [intended], author=AUTHOR,
                    producer_build=BUILD, preflight_proof=pre["preflight_proof"])
    assert caught.value.code == "round_trip_failed" and not output.exists()
    assert source.read_bytes() == before


@pytest.mark.parametrize("inherited,local,allowed", [("preserve", "default", False), ("default", "preserve", True)])
def test_local_xml_space_overrides_inherited_mode(tmp_path, inherited, local, allowed):
    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[0]
    proof = preflight_edits(str(source), [edit(source)], author=AUTHOR, producer_build=BUILD)["preflight_proof"]
    def mutate(root):
        root.set(SPACE, inherited)
        atom = root.find(".//" + w("t"))
        atom.text = " " + atom.text + " "
        atom.set(SPACE, local)
    rewrite(source, mutate)
    if allowed:
        execute(source, tmp_path / "output.docx", [edit(source)])
    else:
        refused_with_bound_proof(source, edit(source), proof, tmp_path / "never.docx")
