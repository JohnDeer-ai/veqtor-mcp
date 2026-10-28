# SPDX-License-Identifier: Apache-2.0
"""Independent segment expectations and fail-closed structural decisions."""

import copy

import jsonschema
import pytest
from lxml import etree

from veqtor_docx import drafting
from veqtor_docx._ooxml import w
from veqtor_docx import revision_decisions as d
from veqtor_docx._paragraph_chain import ParagraphChain
from veqtor_mcp._drafting_contract import OPERATIONS, REVISIONS_RESULT, APPLY_RESULT
from test_clean_copy import _package, _root
from test_counter_revisions import main, save, sha, parts, PROFILE
from test_drafting_text import _p, _r

CASES = [
    (["D", "D", ""], [[0, 1, 2]], [[0], [1], [2]]),
    (["I", "I", ""], [[0], [1], [2]], [[0, 1, 2]]),
    (["D", "I", ""], [[0, 1], [2]], [[0], [1, 2]]),
    (["ID", "I", ""], [[0, 1], [2]], [[0, 1, 2]]),
    (["D", "ID", ""], [[0, 1, 2]], [[0], [1, 2]]),
    (
        ["I"] * 11 + ["ID", "ID", "I", "I", "D", "D", "D", ""],
        [[i] for i in range(11)] + [[11, 12, 13], [14], [15, 16, 17, 18]],
        [list(range(16)), [16], [17], [18]],
    ),
]


def fixture(tmp_path, demo_dir, pattern, *, bookmarks=True):
    root = _root(
        '<w:tbl><w:tblPr><w:tblW w:w="8000" w:type="dxa"/></w:tblPr><w:tblGrid><w:gridCol w:w="8000"/></w:tblGrid><w:tr><w:tc/></w:tr></w:tbl>'
    )
    cell = next(root.iter(w("tc")))
    for index, shape in enumerate(pattern):
        p = _p(_r(f"Part{index}. ", '<w:b/><w:color w:val="123456"/>'))
        props = etree.Element(w("pPr"))
        etree.SubElement(props, w("widowControl"), {w("val"): str(index % 2)})
        etree.SubElement(props, w("spacing"), {w("after"): str(index * 60)})
        etree.SubElement(props, w("ind"), {w("left"): str(index * 120)})
        rp = etree.SubElement(props, w("rPr"))
        etree.SubElement(rp, w("rFonts"), {w("ascii"): "Arial", w("hAnsi"): "Arial"})
        etree.SubElement(rp, w("lang"), {w("val"): "en-US"})
        for offset, kind in enumerate(shape):
            etree.SubElement(
                rp,
                w("ins" if kind == "I" else "del"),
                {w("id"): str(index * 2 + offset + 1), w("author"): "Control"},
            )
        p.insert(0, props)
        p.set("{" + d._W14 + "}paraId", f"{index + 1:08X}")
        cell.append(p)
    if bookmarks:
        cell[0].insert(
            1, etree.Element(w("bookmarkStart"), {w("id"): "1000", w("name"): "Range"})
        )
        cell[-1].append(etree.Element(w("bookmarkEnd"), {w("id"): "1000"}))
        cell[1].insert(
            1, etree.Element(w("bookmarkStart"), {w("id"): "1001", w("name"): "Point"})
        )
        cell[1].insert(2, etree.Element(w("bookmarkEnd"), {w("id"): "1001"}))
    path = tmp_path / "chain.docx"
    path.write_bytes(_package(demo_dir, root))
    return path


def op(path, action="accept"):
    root = main(path)
    nodes = [n for n in root.iter() if n.tag in {w("ins"), w("del")}]
    return {
        "kind": "decide_revision",
        "decision": action,
        "target": {
            "kind": "paragraph_chain",
            "boundary_refs": [d.reference(root, n, sha(path)) for n in nodes],
        },
    }


def semantic(node):
    return [
        node.tag,
        sorted(node.attrib.items()),
        node.text or "",
        [semantic(c) for c in node],
    ]


@pytest.mark.parametrize("pattern,accepted,rejected", CASES)
@pytest.mark.parametrize("action", ["accept", "reject"])
def test_complete_chain_preserves_text_ranges_properties_and_package(
    tmp_path, demo_dir, pattern, accepted, rejected, action
):
    path = fixture(tmp_path, demo_dir, pattern)
    original = main(path)
    old = next(original.iter(w("tc"))).findall(w("p"))
    old_parts = parts(path.read_bytes())
    operation = op(path, action)
    jsonschema.validate([operation], OPERATIONS)
    report = d.read_document_revisions(str(path), max_items=1)
    assert report["revisions"][0]["supported"], report
    assert report["revisions"][0]["structural_target"] == operation["target"]
    jsonschema.validate(
        report["revisions"], REVISIONS_RESULT["properties"]["revisions"]
    )
    result = save(path, [operation], journal=True)
    jsonschema.validate(result["operations"], APPLY_RESULT["properties"]["operations"])
    output = main(path)
    new = next(output.iter(w("tc"))).findall(w("p"))
    groups = accepted if action == "accept" else rejected
    assert len(new) == len(groups)
    assert not any(n.tag in {w("ins"), w("del")} for n in output.iter())
    for p, group in zip(new, groups, strict=True):
        assert "".join(p.itertext()) == "".join(f"Part{i}. " for i in group)
        props = copy.deepcopy(old[group[-1]].find(w("pPr")))
        for mark in props.xpath(".//w:ins|.//w:del", namespaces={"w": w("x")[1:-2]}):
            mark.getparent().remove(mark)
        assert semantic(p.find(w("pPr"))) == semantic(props)
        assert p.attrib == old[group[-1]].attrib
        assert [semantic(n) for n in p if n.tag != w("pPr")] == [
            semantic(n) for i in group for n in old[i] if n.tag != w("pPr")
        ]
    assert {k: v for k, v in old_parts.items() if k != "word/document.xml"} == {
        k: v for k, v in parts(path.read_bytes()).items() if k != "word/document.xml"
    }
    context = result["operations"][0]["paragraph_chain_context"]
    assert (
        context["accepted_segments"] == accepted
        and context["rejected_segments"] == rejected
    )


@pytest.mark.parametrize(
    "problem",
    [
        "partial",
        "reverse",
        "duplicate",
        "unknown_mark",
        "last_mark",
        "inline_revision",
        "field",
        "range",
        "external_bookmark",
        "duplicate_bookmark",
        "unknown_p_attr",
        "unknown_ppr_attr",
        "unknown_property",
        "scalar",
        "run_attr",
        "nested_bookmark",
        "numbered",
        "misplaced_ppr",
        "duplicate_rpr",
        "comment",
        "row",
    ],
)
def test_chain_refusals_do_not_publish(tmp_path, demo_dir, problem):
    path = fixture(tmp_path, demo_dir, ["I", "D", ""])
    root = main(path)
    cell = next(root.iter(w("tc")))
    p = cell[0]
    if problem == "unknown_mark":
        next(root.iter(w("ins"))).set("unknown", "data")
    elif problem == "last_mark":
        etree.SubElement(
            cell[-1].find(f"{w('pPr')}/{w('rPr')}"), w("ins"), {w("id"): "99"}
        )
    elif problem == "inline_revision":
        p.append(_p('<w:ins w:id="99"><w:r><w:t>pending</w:t></w:r></w:ins>')[0])
    elif problem == "field":
        p.append(etree.Element(w("fldSimple"), {w("instr"): "DATE"}))
    elif problem == "range":
        p.append(etree.Element(w("permStart"), {w("id"): "99"}))
    elif problem == "external_bookmark":
        p.remove(p.find(w("bookmarkStart")))
    elif problem == "duplicate_bookmark":
        cell[-1].append(copy.deepcopy(p.find(w("bookmarkStart"))))
    elif problem == "unknown_p_attr":
        p.set("unknown", "data")
    elif problem == "unknown_ppr_attr":
        p[0].set("unknown", "data")
    elif problem == "unknown_property":
        p[0].append(etree.Element(w("framePr")))
    elif problem == "scalar":
        p[0][0].text = "HIDDEN"
    elif problem == "run_attr":
        p.find(w("r")).set("unknown", "data")
    elif problem == "nested_bookmark":
        p.find(w("r")).append(p.find(w("bookmarkStart")))
    elif problem == "numbered":
        etree.SubElement(
            etree.SubElement(p[0], w("numPr")), w("numId"), {w("val"): "1"}
        )
    elif problem == "misplaced_ppr":
        p.append(p[0])
    elif problem == "duplicate_rpr":
        p.find(w("r")).append(copy.deepcopy(p.find(w("r"))[0]))
    elif problem == "comment":
        p.append(etree.Comment("hidden"))
    elif problem == "row":
        etree.SubElement(
            etree.SubElement(cell.getparent(), w("trPr")), w("ins"), {w("id"): "99"}
        )
    path.write_bytes(_package(demo_dir, root))
    operation = op(path)
    refs = operation["target"]["boundary_refs"]
    if problem == "partial":
        refs.pop()
    elif problem == "reverse":
        refs.reverse()
    elif problem == "duplicate":
        refs.append(refs[0])
    before = path.read_bytes()
    report = drafting.preflight_document_edits(
        str(path), sha(path), [operation], **PROFILE, journal=False
    )
    assert not report["batch_applicable"], report
    assert report["preflight_proof"] is None and path.read_bytes() == before


def test_source_refs_and_overlap_are_checked_before_mutation(tmp_path, demo_dir):
    path = fixture(tmp_path, demo_dir, ["I", "D", ""])
    operation = op(path)
    report = drafting.preflight_document_edits(
        str(path), sha(path), [operation, operation], **PROFILE, journal=False
    )
    assert report["refusal_code"] == "revision_operation_overlap"
    save(path, [operation])
    before = path.read_bytes()
    report = drafting.preflight_document_edits(
        str(path), sha(path), [operation], **PROFILE, journal=False
    )
    assert (
        report["refusal_code"] == "revision_reference_mismatch"
        and path.read_bytes() == before
    )


def test_chain_budget_and_exact_patch(tmp_path, demo_dir, monkeypatch):
    path = fixture(tmp_path, demo_dir, ["I", "D", ""])
    root = main(path)
    nodes = d.resolve_target(root, sha(path), op(path)["target"])
    assert isinstance(nodes, ParagraphChain)
    monkeypatch.setattr(d, "MAX_FINGERPRINT_BYTES", 100)
    report = drafting.preflight_document_edits(
        str(path), sha(path), [op(path)], **PROFILE, journal=False
    )
    assert report["refusal_code"] == "resource_limit_exceeded"


@pytest.mark.parametrize(
    "original,counter",
    [
        ("accept", "accept"),
        ("accept", "reject"),
        ("reject", "accept"),
        ("reject", "reject"),
    ],
)
@pytest.mark.parametrize("structure", ["accept", "reject"])
def test_counter_text_choice_precedes_independent_structural_choice(
    tmp_path, demo_dir, original, counter, structure
):
    from test_preserved_paragraph_boundaries import chain
    from test_counter_revisions import operation

    path = chain(tmp_path, demo_dir, bookmark=True)
    save(path, [operation(path, original, counter)])
    report = d.read_document_revisions(str(path))
    row = next(
        r
        for r in report["revisions"]
        if r.get("structural_target", {}).get("kind") == "paragraph_chain"
    )
    before = main(path)
    ps = next(before.iter(w("tc"))).findall(w("p"))
    expected = [[0], [1, 2]] if structure == "accept" else [[0, 1], [2]]
    save(
        path,
        [
            {
                "kind": "decide_revision",
                "decision": structure,
                "target": row["structural_target"],
            }
        ],
    )
    actual = next(main(path).iter(w("tc"))).findall(w("p"))
    assert ["".join(p.itertext()) for p in actual] == [
        "".join(t for i in group for t in ps[i].itertext()) for group in expected
    ]


def test_inherited_numbering_requires_separate_proof(tmp_path, demo_dir):
    path = fixture(tmp_path, demo_dir, ["I", "D", ""])
    root = main(path)
    package = parts(path.read_bytes())
    styles = etree.fromstring(package["word/styles.xml"])
    style = etree.SubElement(
        styles, w("style"), {w("type"): "paragraph", w("styleId"): "NumberedChain"}
    )
    props = etree.SubElement(style, w("pPr"))
    etree.SubElement(etree.SubElement(props, w("numPr")), w("numId"), {w("val"): "1"})
    next(root.iter(w("pPr"))).insert(
        0, etree.Element(w("pStyle"), {w("val"): "NumberedChain"})
    )
    path.write_bytes(
        _package(demo_dir, root, {"word/styles.xml": etree.tostring(styles)})
    )
    result = drafting.preflight_document_edits(
        str(path), sha(path), [op(path)], **PROFILE, journal=False
    )
    assert (
        not result["batch_applicable"]
        and result["refusal_code"] == "paragraph_chain_unsupported"
    )


def test_collateral_mutation_fails_exact_patch_proof(tmp_path, demo_dir, monkeypatch):
    path = fixture(tmp_path, demo_dir, ["I", "D", ""])
    original = d.apply_decision

    def corrupt(*args, **kwargs):
        patch, facts = original(*args, **kwargs)
        args[0].set("unrequested", "hidden")
        return patch, facts

    monkeypatch.setattr(d, "apply_decision", corrupt)
    before = path.read_bytes()
    result = drafting.preflight_document_edits(
        str(path), sha(path), [op(path)], **PROFILE, journal=False
    )
    assert result["refusal_code"] == "round_trip_failed" and path.read_bytes() == before
