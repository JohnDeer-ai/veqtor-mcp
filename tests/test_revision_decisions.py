# SPDX-License-Identifier: Apache-2.0
"""Synthetic selective decisions: exact outcomes, preservation, atomic refusals."""

import copy
import hashlib
import importlib.util
import zipfile
from pathlib import Path

import jsonschema
import pytest
from lxml import etree

_spec = importlib.util.spec_from_file_location(
    "revision_fixture",
    Path(__file__).resolve().parents[1] / "scripts/revision_decisions_smoke.py",
)
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
CASES, PROFILE = _fixture.CASES, _fixture.PROFILE
source_document, write_source, xml = (
    _fixture.source_document,
    _fixture.write_source,
    _fixture.xml,
)
from veqtor_docx import ApplyError, drafting, inspect_document
from veqtor_docx._ooxml import w
from veqtor_docx.revision_decisions import read_document_revisions
from veqtor_mcp._drafting_contract import _APPLIED, OPERATIONS


def make(tmp_path, demo_dir, kind, mutate=None):
    root = source_document(kind)
    if mutate:
        mutate(root)
    path = tmp_path / "working.docx"
    write_source(demo_dir / "round-1-outgoing-draft.docx", path, root)
    return path


def document(path):
    with zipfile.ZipFile(path) as archive:
        return etree.fromstring(archive.read("word/document.xml"))


def op(path, decision="accept", kind=None):
    inventory = read_document_revisions(str(path))
    rows = inventory["revisions"]
    if kind == "replacement":
        refs = {r["kind"]: r["revision_ref"] for r in rows}
        target = {
            "kind": "replacement",
            "deletion_ref": refs["del"],
            "insertion_ref": refs["ins"],
        }
    else:
        row = next((r for r in rows if r["kind"] == kind), rows[0])
        target = {"kind": "single", "revision_ref": row["revision_ref"]}
    return {"kind": "decide_revision", "decision": decision, "target": target}


def preview(path, operations):
    return drafting.preflight_document_edits(
        str(path), hashlib.sha256(path.read_bytes()).hexdigest(), operations, **PROFILE
    )


def save(path, operations):
    before = path.read_bytes()
    dry = preview(path, operations)
    assert dry["batch_applicable"], dry
    assert path.read_bytes() == before
    jsonschema.validate(operations, OPERATIONS)
    jsonschema.validate(dry["operations"], _APPLIED)
    result = drafting.apply_document_edits(
        str(path),
        hashlib.sha256(before).hexdigest(),
        operations,
        dry["preflight_proof"],
        **PROFILE,
    )
    assert Path(result["backup_path"]).read_bytes() == before
    assert result["backup_sha256"] == hashlib.sha256(before).hexdigest()
    jsonschema.validate(result["operations"], _APPLIED)
    return result


def refused(path, operations, code):
    before = path.read_bytes()
    dry = preview(path, operations)
    assert dry["batch_applicable"] is False, dry
    assert dry["refusal_code"] == code, dry
    assert path.read_bytes() == before
    assert not (path.parent / ".veqtor-backups").exists()


def normalized(node):
    return (
        etree.QName(node).localname,
        dict(node.attrib),
        node.text,
        tuple(normalized(n) for n in node),
    )


@pytest.mark.parametrize("kind", CASES)
@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_six_property_domains_exact_outcomes(tmp_path, demo_dir, kind, decision):
    path = make(tmp_path, demo_dir, kind)
    before = document(path)
    result = save(path, [op(path, decision, kind)])
    after = document(path)
    tag, current, previous = CASES[kind]
    # Find the actual selected owner using original history rather than text or ID.
    old = next(before.iter(w(kind)))
    expected = xml(
        f"<w:{tag}>{current if decision == 'accept' else previous}</w:{tag}>"
    )[0]
    parent_path = before.getroottree().getpath(old.getparent())
    actual = after.xpath(parent_path, namespaces=after.nsmap)[0]
    assert normalized(actual) == normalized(expected)
    old.getparent().getparent().replace(old.getparent(), copy.deepcopy(actual))
    assert normalized(before) == normalized(after)
    assert result["operations"][0]["members"][0]["author"] == (
        None if kind == "tblGridChange" else "Fictional Counterparty"
    )
    assert result["operations"][0]["actor_profile"]["name"] == PROFILE["author"]
    assert read_document_revisions(str(path))["total_count"] == 0


@pytest.mark.parametrize("kind", ["ins", "del", "replacement"])
@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_text_decisions_preserve_unselected_author_and_formatting(
    tmp_path, demo_dir, kind, decision
):
    def mutate(root):
        p = root.find(f"{w('body')}/{w('p')}")
        p.append(xml("<w:r><w:rPr><w:b/></w:rPr><w:t> Keep.</w:t></w:r>")[0])
        p.append(
            xml(
                '<w:ins w:id="20" w:author="Untouched Author" w:date="2026-02-01T12:00:00Z"><w:r><w:t>Pending.</w:t></w:r></w:ins>'
            )[0]
        )

    path = make(tmp_path, demo_dir, kind, mutate)
    before = document(path)
    p = before.find(f"{w('body')}/{w('p')}")
    sentinel = [normalized(n) for n in list(p)[-2:]]
    operation = op(path, decision)
    if kind == "replacement":
        refs = read_document_revisions(str(path))["revisions"]
        operation["target"] = {
            "kind": "replacement",
            "deletion_ref": refs[0]["revision_ref"],
            "insertion_ref": refs[1]["revision_ref"],
        }
    save(path, [operation])
    actual = document(path).find(f"{w('body')}/{w('p')}")
    assert [normalized(n) for n in list(actual)[-2:]] == sentinel
    text = "".join(n.text or "" for n in actual.iter(w("t")))
    expected = (
        "Proposed wording."
        if decision == "accept" and kind in {"ins", "replacement"}
        else "Previous wording."
        if decision == "reject" and kind in {"del", "replacement"}
        else ""
    )
    assert text == expected + " Keep.Pending."
    assert len(list(actual.iter(w("ins")))) == 1


@pytest.mark.parametrize("kind", [k for k in CASES if k != "tblGridChange"])
@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_empty_previous_property_set_is_supported(tmp_path, demo_dir, kind, decision):
    def mutate(root):
        old = next(root.iter(w(kind)))[0]
        for child in list(old):
            old.remove(child)

    path = make(tmp_path, demo_dir, kind, mutate)
    save(path, [op(path, decision)])
    assert not list(document(path).iter(w(kind)))


def test_paragraph_base_decision_preserves_independent_mark_and_section_properties(
    tmp_path, demo_dir
):
    def mutate(root):
        props = next(root.iter(w("pPr")))
        history = props[-1]
        props.insert(
            props.index(history),
            xml(
                '<w:rPr><w:b/><w:rPrChange w:id="31" w:author="Separate"><w:rPr><w:i/></w:rPr></w:rPrChange></w:rPr>'
            )[0],
        )
        props.insert(
            props.index(history),
            xml('<w:sectPr><w:pgSz w:w="9000" w:h="12000"/></w:sectPr>')[0],
        )

    path = make(tmp_path, demo_dir, "pPrChange", mutate)
    original = document(path)
    saved = [normalized(next(original.iter(w(tag)))) for tag in ("rPr", "sectPr")]
    save(path, [op(path, "reject", "pPrChange")])
    actual = document(path)
    assert [normalized(next(actual.iter(w(tag)))) for tag in ("rPr", "sectPr")] == saved
    assert read_document_revisions(str(path))["total_count"] == 1


@pytest.mark.parametrize("bad", ["nested", "move", "anchor", "field"])
def test_text_dependencies_refuse_without_backup(tmp_path, demo_dir, bad):
    def mutate(root):
        node = next(root.iter(w("ins")))
        if bad == "nested":
            node.append(
                xml(
                    '<w:del w:id="2" w:author="Other"><w:r><w:delText>counter</w:delText></w:r></w:del>'
                )[0]
            )
        if bad == "move":
            node.tag = w("moveTo")
        if bad == "anchor":
            node.append(xml('<w:bookmarkStart w:id="2" w:name="Anchor"/>')[0])
        if bad == "field":
            node[0].append(xml('<w:fldChar w:fldCharType="begin"/>')[0])

    path = make(tmp_path, demo_dir, "ins", mutate)
    refused(
        path,
        [op(path)],
        "revision_type_unsupported"
        if bad == "move"
        else "revision_dependency_unsupported",
    )


@pytest.mark.parametrize(
    "replacement",
    [
        '<w:b><w:tblW w:w="5"/></w:b>',
        '<w:tabs><w:pStyle w:val="Normal"/></w:tabs>',
        "<w:b/>outside",
        "<w:b/><w:b/>",
        '<w:color w:val="000000"/><w:b/>',
    ],
)
def test_closed_property_grammar_refuses_malformed_previous_tree(
    tmp_path, demo_dir, replacement
):
    def mutate(root):
        old = next(root.iter(w("rPrChange")))[0]
        old.clear()
        old.extend(xml(replacement))

    path = make(tmp_path, demo_dir, "rPrChange", mutate)
    refused(path, [op(path, "reject")], "property_revision_unsupported")


def test_incompatible_previous_grid_reject_refuses_but_accept_remains_possible(
    tmp_path, demo_dir
):
    def mutate(root):
        previous = next(root.iter(w("tblGridChange")))[0]
        previous.remove(previous[-1])

    path = make(tmp_path, demo_dir, "tblGridChange", mutate)
    row = read_document_revisions(str(path))["revisions"][0]
    assert row["supported"]
    assert row["preview"]["reject_refusal_code"] == "table_grid_incompatible"
    refused(path, [op(path, "reject")], "table_grid_incompatible")
    save(path, [op(path, "accept")])


def test_duplicate_ids_are_exactly_addressed_and_old_refs_expire(tmp_path, demo_dir):
    def mutate(root):
        body = root.find(w("body"))
        p = copy.deepcopy(body[0])
        p[0].set(w("id"), "21")
        p[0][0][0].text = "Second."
        body.insert(1, p)

    path = make(tmp_path, demo_dir, "ins", mutate)
    refs = read_document_revisions(str(path))["revisions"]
    assert refs[0]["id"] == refs[1]["id"]
    assert refs[0]["revision_ref"] != refs[1]["revision_ref"]
    second = {
        "kind": "decide_revision",
        "decision": "reject",
        "target": {"kind": "single", "revision_ref": refs[1]["revision_ref"]},
    }
    save(path, [second])
    assert len(list(document(path).iter(w("ins")))) == 1
    assert preview(path, [second])["refusal_code"] == "revision_reference_mismatch"


def test_disjoint_decisions_in_same_paragraph_preserve_exact_spans(tmp_path, demo_dir):
    path = make(tmp_path, demo_dir, "replacement")
    refs = read_document_revisions(str(path))["revisions"]
    operations = [
        {
            "kind": "decide_revision",
            "decision": "reject",
            "target": {"kind": "single", "revision_ref": r["revision_ref"]},
        }
        for r in refs
    ]
    save(path, operations)
    assert "".join(document(path).itertext()) == "Previous wording."


def test_overlap_with_edit_refuses_whole_batch(tmp_path, demo_dir):
    path = make(tmp_path, demo_dir, "ins")
    ref = inspect_document(str(path), "browse")["paragraphs"][0]["paragraph_ref"]
    refused(
        path,
        [
            op(path),
            {
                "kind": "replace_text",
                "paragraph_ref": ref,
                "old_text": "Proposed",
                "new_text": "New",
            },
        ],
        "revision_operation_overlap",
    )


def test_proof_binds_actor_decision_and_fresh_source(tmp_path, demo_dir):
    path = make(tmp_path, demo_dir, "ins")
    operations = [op(path)]
    before = path.read_bytes()
    sha = hashlib.sha256(before).hexdigest()
    dry = preview(path, operations)
    for selected, profile in (
        ([{**operations[0], "decision": "reject"}], PROFILE),
        (operations, {**PROFILE, "author": "Different Counsel"}),
    ):
        with pytest.raises(ApplyError, match="preflight_binding_mismatch"):
            drafting.apply_document_edits(
                str(path), sha, selected, dry["preflight_proof"], **profile
            )
    assert path.read_bytes() == before
    # A successful retry after an unknown result must first compare hashes; old input never publishes twice.
    save(path, operations)
    with pytest.raises(ApplyError, match="file_sha256_mismatch"):
        drafting.apply_document_edits(
            str(path), sha, operations, dry["preflight_proof"], **PROFILE
        )
    assert len(list((path.parent / ".veqtor-backups").glob("*.docx"))) == 1


@pytest.mark.parametrize("scope", ["outside", "spoofed_patch"])
def test_independent_patch_guard_detects_helper_collateral(
    tmp_path, demo_dir, monkeypatch, scope
):
    from veqtor_docx import revision_decisions as decisions

    path = make(tmp_path, demo_dir, "ins")
    original = decisions.apply_decision

    def corrupt(root, *args):
        patch, facts = original(root, *args)
        if scope == "outside":
            root.find(w("body")).set("unrequested", "damage")
        else:
            patch.after[0].set("unrequested", "damage")
            patch.parent[patch.index].set("unrequested", "damage")
        return patch, facts

    monkeypatch.setattr(decisions, "apply_decision", corrupt)
    refused(path, [op(path)], "round_trip_failed")


@pytest.mark.parametrize(
    "kind",
    [
        "rPrChange",
        "pPrChange",
        "tcPrChange",
        "trPrChange",
        "tblPrChange",
        "tblGridChange",
    ],
)
def test_unknown_or_dependent_history_never_partially_decides(tmp_path, demo_dir, kind):
    def mutate(root):
        change = next(root.iter(w(kind)))
        change[0].append(xml('<w:ins w:id="100" w:author="Other"/>')[0])

    path = make(tmp_path, demo_dir, kind, mutate)
    assert read_document_revisions(str(path))["revisions"][0]["supported"] is False
    before = path.read_bytes()
    dry = preview(path, [op(path, "reject", kind)])
    assert dry["batch_applicable"] is False
    assert path.read_bytes() == before
    assert not (path.parent / ".veqtor-backups").exists()


@pytest.mark.parametrize("geometry", ["span", "horizontal", "vertical", "omitted"])
def test_grid_restoration_supports_coherent_merges_and_omitted_columns(
    tmp_path, demo_dir, geometry
):
    def mutate(root):
        table = next(root.iter(w("tbl")))
        row = table.find(w("tr"))
        if geometry == "span":
            row.remove(row[-1])
            row[0].insert(0, xml('<w:tcPr><w:gridSpan w:val="2"/></w:tcPr>')[0])
        elif geometry == "omitted":
            row.remove(row[-1])
            row.insert(0, xml('<w:trPr><w:gridAfter w:val="1"/></w:trPr>')[0])
        else:
            for i, cell in enumerate(row):
                cell.insert(
                    0,
                    xml(
                        f'<w:tcPr><w:{"hMerge" if geometry == "horizontal" else "vMerge"} w:val="{"restart" if i == 0 or geometry == "vertical" else "continue"}"/></w:tcPr>'
                    )[0],
                )
            if geometry == "vertical":
                next_row = copy.deepcopy(row)
                for node in next_row.iter(w("vMerge")):
                    node.set(w("val"), "continue")
                table.append(next_row)

    path = make(tmp_path, demo_dir, "tblGridChange", mutate)
    save(path, [op(path, "reject")])
    assert [n.get(w("w")) for n in document(path).iter(w("gridCol"))] == [
        "2400",
        "2400",
    ]


@pytest.mark.parametrize(
    "geometry", ["orphan_hmerge", "orphan_vmerge", "pending_span", "pending_row"]
)
def test_grid_dependencies_refuse_atomically(tmp_path, demo_dir, geometry):
    def mutate(root):
        table = next(root.iter(w("tbl")))
        row = table.find(w("tr"))
        cell = row[0]
        if geometry.startswith("orphan"):
            cell.insert(
                0,
                xml(
                    f"<w:tcPr><w:{'hMerge' if geometry == 'orphan_hmerge' else 'vMerge'}/></w:tcPr>"
                )[0],
            )
        elif geometry == "pending_span":
            cell.insert(
                0,
                xml(
                    '<w:tcPr><w:gridSpan w:val="1"/><w:tcPrChange w:id="50" w:author="Other"><w:tcPr><w:gridSpan w:val="2"/></w:tcPr></w:tcPrChange></w:tcPr>'
                )[0],
            )
        else:
            row.insert(
                0, xml('<w:trPr><w:ins w:id="50" w:author="Other"/></w:trPr>')[0]
            )

    path = make(tmp_path, demo_dir, "tblGridChange", mutate)
    refused(
        path,
        [op(path, "reject", "tblGridChange")],
        "table_grid_incompatible"
        if geometry.startswith("orphan")
        else "revision_dependency_unsupported",
    )


def test_refuses_unknown_old_style_without_accepting_other_revision(tmp_path, demo_dir):
    def mutate(root):
        next(root.iter(w("rPrChange")))[0].insert(
            0, xml('<w:rStyle w:val="MissingStyle"/>')[0]
        )

    path = make(tmp_path, demo_dir, "rPrChange", mutate)
    refused(path, [op(path, "reject")], "paragraph_structure_unsupported")
    save(path, [op(path, "accept")])


def test_serialized_collateral_inside_touched_paragraph_is_detected(
    tmp_path, demo_dir, monkeypatch
):
    from veqtor_docx._ooxml import DOCUMENT_PART

    def mutate(root):
        root.find(f"{w('body')}/{w('p')}").append(
            xml("<w:r><w:rPr><w:i/></w:rPr><w:t>Unselected.</w:t></w:r>")[0]
        )

    path = make(tmp_path, demo_dir, "ins", mutate)
    original = drafting._output_archive_bytes

    def corrupt(infos, parts):
        altered = dict(parts)
        root = etree.fromstring(parts[DOCUMENT_PART])
        italic = next(root.iter(w("i")))
        italic.tag = w("b")
        altered[DOCUMENT_PART] = etree.tostring(root)
        return original(infos, altered)

    monkeypatch.setattr(drafting, "_output_archive_bytes", corrupt)
    refused(path, [op(path)], "round_trip_failed")


def test_word_owner_lock_blocks_decisions_with_no_backup(tmp_path, demo_dir):
    path = make(tmp_path, demo_dir, "ins")
    before = path.read_bytes()
    operations = [op(path)]
    dry = preview(path, operations)
    (path.parent / ("~$" + path.name)).write_bytes(b"fictional Word owner")
    with pytest.raises(ApplyError, match="file_locked"):
        drafting.apply_document_edits(
            str(path),
            hashlib.sha256(before).hexdigest(),
            operations,
            dry["preflight_proof"],
            **PROFILE,
        )
    assert path.read_bytes() == before
    assert not (path.parent / ".veqtor-backups").exists()


def test_inventory_limits_and_hash_bound_pagination(tmp_path, demo_dir):
    path = make(tmp_path, demo_dir, "replacement")
    page = read_document_revisions(str(path), max_items=1)
    assert page["next_offset"] == 1
    next_page = read_document_revisions(
        str(path), page["file_sha256"], offset=1, max_items=1
    )
    assert next_page["next_offset"] is None
    assert next_page["revisions"][0]["kind"] == "ins"
    for kwargs in ({"max_items": 101}, {"offset": -1}, {"offset": True}):
        with pytest.raises(ApplyError, match="invalid_request"):
            read_document_revisions(str(path), **kwargs)
