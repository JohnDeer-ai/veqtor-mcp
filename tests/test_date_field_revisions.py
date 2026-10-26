# SPDX-License-Identifier: Apache-2.0
"""Decisions affect cached date text; field controls and settings survive exactly."""

import copy

import jsonschema
import pytest
from lxml import etree

from veqtor_docx import drafting
from veqtor_docx._ooxml import w
from veqtor_docx.clean_copy import prepare_clean_copy
from veqtor_docx.revision_decisions import read_document_revisions
from veqtor_mcp._drafting_contract import DECISION_PREVIEW, APPLY_RESULT
from test_clean_copy import _package
from test_counter_revisions import make, main, operation, parts, save, sha, PROFILE
from test_drafting_text import _p, _r


def field_run(kind, text=None, **attrs):
    run = etree.Element(w("r"))
    atom = etree.SubElement(run, w(kind), {w(k): v for k, v in attrs.items()})
    atom.text = text
    return run


def add_field(p, kind="DATE", locked=None):
    # Existing counter wrappers were produced by Veqtor on fictional text.
    begin = field_run("fldChar", fldCharType="begin", dirty="true")
    if locked is not None:
        begin[0].set(w("fldLock"), locked)
    instruction = (
        ' DATE \\@ "MMMM d, yyyy" '
        if kind == "DATE"
        else " TIME \\@ \"d MMMM yyyy 'г.'\" "
    )
    controls = [
        begin,
        field_run("instrText", instruction),
        field_run("fldChar", fldCharType="separate"),
    ]
    for index, control in enumerate(controls):
        p.insert(index, control)
    p.append(field_run("fldChar", fldCharType="end"))


def fixture(tmp_path, demo_dir, kind="DATE", locked=None, mutation=None):
    path = make(tmp_path, demo_dir)
    root = main(path)
    p = next(root.iter(w("p")))
    add_field(p, kind, locked)
    if mutation:
        mutation(root, p)
    path.write_bytes(_package(demo_dir, root))
    return path


def controls(root):
    return [
        etree.tostring(n, method="c14n")
        for n in root.iter(w("r"))
        if n.find(w("fldChar")) is not None or n.find(w("instrText")) is not None
    ]


@pytest.mark.parametrize("kind", ["DATE", "TIME"])
@pytest.mark.parametrize("locked", [None, "true", "false"])
@pytest.mark.parametrize(
    "original,counter",
    [
        ("accept", "accept"),
        ("accept", "reject"),
        ("reject", "accept"),
        ("reject", "reject"),
    ],
)
def test_four_outcomes_preserve_field_controls(
    tmp_path, demo_dir, kind, locked, original, counter
):
    path = fixture(tmp_path, demo_dir, kind, locked)
    before = parts(path.read_bytes())
    field_controls = controls(main(path))
    report = read_document_revisions(str(path))
    row = next(r for r in report["revisions"] if "counter_target" in r)
    assert row["supported"], row
    jsonschema.validate(row["preview"], DECISION_PREVIEW)
    context = row["preview"]["field_result_context"]
    assert context["field_kind"] == kind
    assert context["field_locked"] == (locked == "true")
    assert context["decision_scope"] == "cached_result_only"
    result = save(path, [operation(path, original, counter)])
    jsonschema.validate(result["operations"], APPLY_RESULT["properties"]["operations"])
    assert result["operations"][0]["field_result_context"] == context
    assert controls(main(path)) == field_controls
    after = parts(path.read_bytes())
    assert {k: v for k, v in before.items() if k != "word/document.xml"} == {
        k: v for k, v in after.items() if k != "word/document.xml"
    }
    text = "".join(main(path).itertext())
    expected = {
        ("accept", "accept"): "thirty weeks",
        ("accept", "reject"): "thirty days",
        ("reject", "accept"): "weeks",
        ("reject", "reject"): "",
    }[original, counter]
    assert "Pay " + expected + " later" in text
    assert "neighbor" in text


@pytest.mark.parametrize("kind", ["DATE", "TIME"])
def test_clean_copy_resolves_remaining_simple_revisions_in_cache(
    tmp_path, demo_dir, kind
):
    path = fixture(tmp_path, demo_dir, kind)
    before = controls(main(path))
    result = prepare_clean_copy(path.read_bytes(), expected_source_sha256=sha(path))
    assert result.inventory_after["revisions"] == []
    root = etree.fromstring(parts(result.payload)["word/document.xml"])
    assert controls(root) == before


def mutate(root, p, case):
    begin, instruction, separate = p[:3]
    if case == "unknown_kind":
        instruction[0].text = " DOCVARIABLE secret "
    elif case == "unknown_switch":
        instruction[0].text += r" \x unknown"
    elif case == "unknown_attribute":
        begin[0].set("{urn:unknown}flag", "1")
    elif case == "unknown_child":
        etree.SubElement(begin[0], w("fldData"))
    elif case == "unknown_lock":
        begin[0].set(w("fldLock"), "perhaps")
    elif case == "extra_control_text":
        etree.SubElement(begin, w("t")).text = "preserve me"
    elif case == "nested":
        p.insert(3, copy.deepcopy(begin))
    elif case == "missing_end":
        p.remove(p[-1])
    elif case == "changed_instruction":
        wrapper = etree.Element(w("ins"), {w("id"): "900", w("author"): "Other"})
        p.replace(instruction, wrapper)
        wrapper.append(instruction)
    elif case == "outside_cache":
        p.remove(separate)
        p.insert(len(p) - 1, separate)
    elif case == "paragraph_mark":
        props = etree.SubElement(etree.SubElement(p, w("pPr")), w("rPr"))
        etree.SubElement(props, w("del"), {w("id"): "901", w("author"): "Other"})
    elif case == "external_field":
        root.find(w("body")).insert(
            0, _p('<w:r><w:fldChar w:fldCharType="begin"/></w:r>')
        )
    elif case == "external_permission":
        root.find(w("body")).insert(0, _p('<w:permStart w:id="901"/>'))
    elif case == "adjacent_field":
        for node in list(p)[3:-1]:
            p.remove(node)
            p.append(node)
    else:
        raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    [
        "unknown_kind",
        "unknown_switch",
        "unknown_attribute",
        "unknown_child",
        "unknown_lock",
        "extra_control_text",
        "nested",
        "missing_end",
        "changed_instruction",
        "outside_cache",
        "paragraph_mark",
        "external_field",
        "external_permission",
        "adjacent_field",
    ],
)
def test_closed_refusals_leave_source_unchanged(tmp_path, demo_dir, case):
    path = fixture(tmp_path, demo_dir, mutation=lambda root, p: mutate(root, p, case))
    before = path.read_bytes()
    op = operation(path)
    result = drafting.preflight_document_edits(str(path), sha(path), [op], **PROFILE)
    assert not result["batch_applicable"], case
    assert path.read_bytes() == before


def test_literal_date_retains_ordinary_semantics(tmp_path, demo_dir):
    path = make(tmp_path, demo_dir)
    root = main(path)
    root.find(w("body")).append(_p(_r("June 8, 2026")))
    path.write_bytes(_package(demo_dir, root))
    result = save(path, [operation(path)])
    assert "field_result_context" not in result["operations"][0]
    assert "June 8, 2026" in "".join(main(path).itertext())


@pytest.mark.parametrize("kind", ["DATE", "TIME"])
@pytest.mark.parametrize("decision", ["accept", "reject"])
@pytest.mark.parametrize("revision_kind", ["ins", "del", "replacement"])
def test_plain_cached_revisions_use_same_explicit_semantics(
    tmp_path, demo_dir, kind, decision, revision_kind
):
    path = fixture(tmp_path, demo_dir, kind)
    root = main(path)
    p = next(root.iter(w("p")))
    for node in list(p)[3:-1]:
        p.remove(node)
    for tag, text in (("del", "Old date"), ("ins", "New date")):
        if revision_kind not in {tag, "replacement"}:
            continue
        wrapper = etree.Element(
            w(tag),
            {
                w("id"): "801" if tag == "ins" else "802",
                w("author"): "Fictional Counsel",
            },
        )
        run = etree.SubElement(wrapper, w("r"))
        etree.SubElement(run, w("t") if tag == "ins" else w("delText")).text = text
        p.insert(len(p) - 1, wrapper)
    path.write_bytes(_package(demo_dir, root))
    before = controls(root)
    rows = [
        row
        for row in read_document_revisions(str(path))["revisions"]
        if row["id"] in {"801", "802"}
    ]
    assert all(
        row["supported"]
        and row["preview"]["field_result_context"]["field_kind"] == kind
        for row in rows
    )
    target = (
        {
            "kind": "replacement",
            "deletion_ref": rows[0]["revision_ref"],
            "insertion_ref": rows[1]["revision_ref"],
        }
        if revision_kind == "replacement"
        else {"kind": "single", "revision_ref": rows[0]["revision_ref"]}
    )
    result = save(
        path, [{"kind": "decide_revision", "target": target, "decision": decision}]
    )
    jsonschema.validate(result["operations"], APPLY_RESULT["properties"]["operations"])
    assert controls(main(path)) == before
    current = "".join(n.text or "" for n in next(main(path).iter(w("p"))).iter(w("t")))
    expected = (
        "New date"
        if decision == "accept" and revision_kind != "del"
        else "Old date"
        if decision == "reject" and revision_kind != "ins"
        else ""
    )
    assert current == expected
