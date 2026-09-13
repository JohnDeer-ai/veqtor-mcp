# SPDX-License-Identifier: Apache-2.0
"""Reject misplaced boundaries even when every ancestor name is recognized."""
import pytest
from lxml import etree

from veqtor_docx import preflight_edits
from veqtor_docx._ooxml import w
from veqtor_docx.synthetic import generate_demo_rounds
from test_paragraph_edits import AUTHOR, BUILD, edit, execute, rewrite, rows
from test_paragraph_fix4 import child, comments_package, refused_with_bound_proof


def branch(parent, names):
    for name in names:
        parent = child(parent, name)
    return parent


def complete_tables(root):
    for table in root.iter(w("tbl")):
        if table.find(w("tblGrid")) is None:
            grid = etree.Element(w("tblGrid"))
            child(grid, "gridCol", w="2400")
            table.insert(0, grid)
        if table.find(w("tr")) is None:
            child(table, "tr")
        for row in table.findall(w("tr")):
            if row.find(w("tc")) is None:
                child(row, "tc")
            for cell in row.findall(w("tc")):
                if not len(cell) or cell[-1].tag != w("p"):
                    child(cell, "p")


@pytest.mark.parametrize("fake_path", [(), ("r", "r"), ("p", "r")],
                         ids=["direct-p", "nested-r", "nested-p"])
@pytest.mark.parametrize("location", ["body", "cell", "nested-table"])
def test_fake_boundaries_cannot_hide_enclosing_field(tmp_path, fake_path, location):
    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[0]
    proof = preflight_edits(str(source), [edit(source)], author=AUTHOR, producer_build=BUILD)["preflight_proof"]

    def mutate(root):
        body = root.find(w("body"))
        target = body.find(w("p"))
        parent = body
        if location != "body":
            parent = body.find(".//" + w("tc"))
            if location == "nested-table":
                parent = branch(parent, ("tbl", "tr", "tc"))
            parent.append(target)
        before, after = etree.Element(w("p")), etree.Element(w("p"))
        child(child(before, "r"), "fldChar", fldCharType="begin")
        child(child(before, "r"), "instrText").text = " DOCPROPERTY Title "
        child(child(before, "r"), "fldChar", fldCharType="separate")
        child(branch(before, fake_path), "fldChar", fldCharType="end")
        child(branch(after, fake_path), "fldChar", fldCharType="begin")
        child(child(after, "r"), "fldChar", fldCharType="end")
        parent.insert(parent.index(target), before)
        parent.insert(parent.index(target) + 1, after)

    rewrite(source, mutate)
    index = next(row["paragraph_ref"]["paragraph_index"] for row in rows(source)
                 if row["text"].startswith("Payment"))
    refused_with_bound_proof(source, edit(source, index), proof, tmp_path / "never.docx")


def outside_context(root, path, kind):
    body = root.find(w("body"))
    # Everything is after the target. A locally clean target must still refuse
    # if the story contains boundaries whose ancestry cannot be interpreted.
    before = etree.Element(w("p"))
    body.insert(len(body) - 1, before)
    outer = etree.Element(w(path[0]))
    body.insert(len(body) - 1, outer)
    terminal = branch(outer, path[1:])
    after = etree.Element(w("p"))
    body.insert(len(body) - 1, after)
    if kind == "field":
        child(terminal, "fldChar", fldCharType="begin")
        child(terminal, "fldChar", fldCharType="end")
    elif kind == "instruction":
        child(child(before, "r"), "fldChar", fldCharType="begin")
        child(terminal, "instrText").text = " DOCPROPERTY Title "
        child(child(after, "r"), "fldChar", fldCharType="end")
    else:
        child(terminal, "commentRangeStart", id="0")
        child(terminal, "commentRangeEnd", id="0")
        child(child(after, "r"), "commentReference", id="0")
    complete_tables(root)


@pytest.mark.parametrize("kind", ["field", "instruction", "comment"])
@pytest.mark.parametrize("path", [
    ("p", "p"), ("p", "tc", "p"), ("tbl", "p"), ("tbl", "tr", "p"),
    ("tbl", "tr", "tr", "tc", "p"), ("tbl", "tr", "tc", "tbl", "tc", "p"),
    ("tbl", "tr", "tc", "p", "p"),
])
def test_invalid_ancestor_edges_after_target_refuse(tmp_path, kind, path):
    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[0]
    proof = preflight_edits(str(source), [edit(source)], author=AUTHOR, producer_build=BUILD)["preflight_proof"]
    if kind != "comment":
        path += ("r",)
    rewrite(source, lambda root: outside_context(root, path, kind))
    if kind == "comment":
        comments_package(source)
    refused_with_bound_proof(source, edit(source), proof, tmp_path / "never.docx")


@pytest.mark.parametrize("kind", ["field", "instruction", "comment"])
@pytest.mark.parametrize("path", [("p",), ("tbl", "tr", "tc", "p"),
                                  ("tbl", "tr", "tc", "tbl", "tr", "tc", "p")])
def test_valid_ancestry_away_from_target_stays_supported(tmp_path, kind, path):
    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[0]
    if kind != "comment":
        path += ("r",)
    rewrite(source, lambda root: outside_context(root, path, kind))
    if kind == "comment":
        comments_package(source)
    _, result = execute(source, tmp_path / "output.docx", [edit(source)])
    assert result["round_trip_check"]["status"] == "passed"


@pytest.mark.parametrize("kind,path", [
    ("field", ("p",)), ("instruction", ("p",)), ("comment", ("p", "r")),
    ("field", ("tbl", "tr", "tc", "r")),
    ("instruction", ("tbl", "tr", "tc", "r")),
])
def test_invalid_immediate_parent_after_target_refuses(tmp_path, kind, path):
    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[0]
    proof = preflight_edits(str(source), [edit(source)], author=AUTHOR, producer_build=BUILD)["preflight_proof"]
    rewrite(source, lambda root: outside_context(root, path, kind))
    if kind == "comment":
        comments_package(source)
    refused_with_bound_proof(source, edit(source), proof, tmp_path / "never.docx")


@pytest.mark.parametrize("path", [("tbl",), ("tbl", "tr"), ("tbl", "tr", "tc")])
def test_table_level_comment_boundaries_away_from_target_stay_supported(tmp_path, path):
    source = generate_demo_rounds(tmp_path / "corpus", profile="paragraph-edits")[0]
    rewrite(source, lambda root: outside_context(root, path, "comment"))
    comments_package(source)
    _, result = execute(source, tmp_path / "output.docx", [edit(source)])
    assert result["round_trip_check"]["status"] == "passed"
