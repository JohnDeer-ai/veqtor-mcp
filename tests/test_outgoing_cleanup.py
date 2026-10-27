# SPDX-License-Identifier: Apache-2.0
"""Fictional full-thread/property export checks; input and working policies stay intact."""

import json
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from lxml import etree

from veqtor_docx import _drafting_comments as c
from veqtor_docx import outgoing_cleanup as cleanup
from veqtor_docx import outgoing_package as outgoing
from veqtor_docx import round_snapshots as snapshots
from veqtor_docx._ooxml import W_NS, parse_xml, w
from veqtor_docx.apply import ApplyError
from veqtor_docx.revision_inventory import inventory_package_revisions

REL = c._REL_NS
CT = c._TYPE_NS
REMOVE_THREAD = {
    "mode": "remove_threads",
    "threads": [{"root_id": "0", "member_ids": ["0", "1"]}],
}
REMOVE_PROPS = {
    "mode": "remove_selected",
    "fields": [
        {"part": "core", "name": "creator"},
        {"part": "app", "name": "Company"},
        {"part": "custom", "name": "InternalNote"},
    ],
}


def pack(parts):
    out = BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as z:
        for name, value in parts.items():
            z.writestr(name, value)
    return out.getvalue()


def unpack(payload):
    with ZipFile(BytesIO(payload)) as z:
        return {name: z.read(name) for name in z.namelist()}


def fictional_parts(*, comments=True):
    document = parse_xml(
        f'<w:document xmlns:w="{W_NS}"><w:body><w:p><w:r><w:t>Delivery within thirty days.</w:t></w:r></w:p><w:p><w:ins w:id="10" w:author="Fictional Counterparty"><w:r><w:t>Unselected revision.</w:t></w:r></w:ins></w:p></w:body></w:document>'.encode()
    )
    types = etree.Element(f"{{{CT}}}Types", nsmap={None: CT})
    etree.SubElement(
        types,
        f"{{{CT}}}Default",
        Extension="rels",
        ContentType="application/vnd.openxmlformats-package.relationships+xml",
    )
    etree.SubElement(
        types, f"{{{CT}}}Default", Extension="xml", ContentType="application/xml"
    )
    etree.SubElement(
        types,
        f"{{{CT}}}Override",
        PartName="/word/document.xml",
        ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    )
    rels = etree.Element(f"{{{REL}}}Relationships", nsmap={None: REL})
    etree.SubElement(
        rels,
        f"{{{REL}}}Relationship",
        Id="main",
        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
        Target="word/document.xml",
    )
    parts = {"word/document.xml": c._xml(document)}
    values = {
        "core": f'<cp:coreProperties xmlns:cp="{cleanup.CORE}" xmlns:dc="{cleanup.DC}"><dc:creator>Fictional internal author</dc:creator><dc:title>Keep this title</dc:title></cp:coreProperties>',
        "app": f'<Properties xmlns="{cleanup.APP}"><Company>Fictional internal company</Company><Application>Keep application</Application></Properties>',
        "custom": f'<Properties xmlns="{cleanup.CUSTOM}" xmlns:vt="{cleanup.VT}"><property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="2" name="InternalNote"><vt:lpwstr>Fictional private value</vt:lpwstr></property><property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="3" name="PublicCode"><vt:i4>42</vt:i4></property></Properties>',
    }
    mime = {
        "core": "application/vnd.openxmlformats-package.core-properties+xml",
        "app": "application/vnd.openxmlformats-officedocument.extended-properties+xml",
        "custom": "application/vnd.openxmlformats-officedocument.custom-properties+xml",
    }
    for kind, (name, _, relation) in cleanup.PROPS.items():
        parts[name] = values[kind].encode()
        etree.SubElement(
            types, f"{{{CT}}}Override", PartName="/" + name, ContentType=mime[kind]
        )
        etree.SubElement(
            rels, f"{{{REL}}}Relationship", Id=kind, Type=relation, Target=name
        )
    parts["[Content_Types].xml"] = c._xml(types)
    parts["_rels/.rels"] = c._xml(rels)
    if comments:
        paragraph = document.find(f"{w('body')}/{w('p')}")
        c.add_comment(
            parts,
            document,
            paragraph,
            "thirty",
            "[Internal] Consider forty-five days.",
            "Fictional Counsel",
            "FC",
        )
        c.reply_comment(
            parts,
            document,
            "0",
            "Counterparty reply must be shown too.",
            "Fictional Counterparty",
            "CP",
            timestamp="2026-10-27T08:00:00Z",
        )
        c.add_comment(
            parts,
            document,
            paragraph,
            "Delivery",
            "Keep this separate external thread.",
            "Fictional Counterparty",
            "CP",
        )
        c.set_comment_resolved(parts, document, "0", True, "Fictional Counsel")
    parts["word/document.xml"] = c._xml(document)
    assert inventory_package_revisions(pack(parts))["complete"], (
        inventory_package_revisions(pack(parts))["blockers"]
    )
    return parts


def prepare(parts, tmp_path, **kw):
    data = pack(parts)
    snap = snapshots.DocumentSnapshot(data, sha256(data).hexdigest(), len(data))
    return outgoing.prepare_outgoing_package(
        snap, tmp_path / "out", redline_name="Review.docx", **kw
    )


def test_selected_whole_mixed_thread_and_exact_properties_on_outgoing_copy(tmp_path):
    parts = fictional_parts()
    original = deepcopy(parts)
    plan = prepare(
        parts, tmp_path, comments_policy=REMOVE_THREAD, properties_policy=REMOVE_PROPS
    )
    report = json.loads(plan.report)
    assert report["schema_version"] == "outgoing_preparation.v2"
    removed = report["cleanup"]
    assert [m["author"] for m in removed["removed_comments"]] == [
        "Fictional Counsel",
        "Fictional Counterparty",
    ]
    assert (
        removed["removed_comments"][1]["text"]
        == "Counterparty reply must be shown too."
    )
    assert [m["value"] for m in removed["removed_properties"]] == [
        "Fictional internal author",
        "Fictional internal company",
        "Fictional private value",
    ]
    output = unpack(plan.files[0].payload)
    assert parts == original
    before_doc, after_doc = (
        parse_xml(parts["word/document.xml"]),
        parse_xml(output["word/document.xml"]),
    )
    assert [r["id"] for r in c.read_comments(output, after_doc)] == ["2"]
    assert etree.tostring(
        before_doc.find(f".//{w('ins')}"), method="c14n"
    ) == etree.tostring(after_doc.find(f".//{w('ins')}"), method="c14n")
    assert [n.text for n in after_doc.iter(w("t"))] == [
        n.text for n in before_doc.iter(w("t"))
    ]
    assert (
        parse_xml(output["docProps/core.xml"]).find(f"{{{cleanup.DC}}}title").text
        == "Keep this title"
    )
    assert [n.get("name") for n in parse_xml(output["docProps/custom.xml"])] == [
        "PublicCode"
    ]
    assert all(
        output[n] == value
        for n, value in original.items()
        if n not in removed["changed_parts"]
    )
    assert not (tmp_path / "out").exists()
    outgoing.publish_outgoing_package(
        plan, tmp_path / "out", expected_plan_sha256=plan.plan_sha256
    )
    assert (
        outgoing.verify_outgoing_package(
            plan, tmp_path / "out", expected_plan_sha256=plan.plan_sha256
        )["status"]
        == "matched"
    )
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["Review.docx"]


@pytest.mark.parametrize("clean", [False, True])
def test_remove_all_comments_with_clean_or_cumulative_history(tmp_path, clean):
    parts = fictional_parts()
    plan = prepare(
        parts,
        tmp_path,
        comments_policy={"mode": "remove_all"},
        clean_draft_name="Clean.docx" if clean else None,
    )
    assert json.loads(plan.report)["cleanup"]["all_comments_removed"]
    for file in plan.files:
        result = unpack(file.payload)
        assert not list(parse_xml(result[c._COMMENTS]))
        assert not any(
            n.tag in c._ANCHOR_TAGS
            for n in parse_xml(result["word/document.xml"]).iter()
        )
        assert all(
            result[n] == parts[n]
            for n in ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml")
        )
    assert (
        len(
            list(
                parse_xml(unpack(plan.files[0].payload)["word/document.xml"]).iter(
                    w("ins")
                )
            )
        )
        == 1
    )
    if clean:
        assert not list(
            parse_xml(unpack(plan.files[1].payload)["word/document.xml"]).iter(w("ins"))
        )


def test_no_cleanup_retains_exact_historical_report_and_bytes(tmp_path):
    parts = fictional_parts()
    plan = prepare(parts, tmp_path)
    assert plan.files[0].payload == pack(parts)
    assert json.loads(plan.report)["schema_version"] == "outgoing_preparation.v1"
    assert "cleanup" not in json.loads(plan.report)


def test_working_document_foreign_comment_deletion_still_refused():
    parts = fictional_parts()
    before = deepcopy(parts)
    with pytest.raises(ApplyError, match="comment_author_mismatch"):
        c.delete_comment_thread(
            parts,
            parse_xml(parts["word/document.xml"]),
            "0",
            ["0", "1"],
            "Fictional Counsel",
        )
    assert parts == before


@pytest.mark.parametrize(
    "policy",
    [
        {"mode": "remove_threads", "threads": [{"root_id": "0", "member_ids": ["0"]}]},
        {"mode": "remove_threads", "threads": [{"root_id": "1", "member_ids": ["1"]}]},
        {
            "mode": "remove_threads",
            "threads": [{"root_id": "0", "member_ids": ["0", "1", "2"]}],
        },
    ],
)
def test_partial_or_wrong_thread_refused(tmp_path, policy):
    parts = fictional_parts()
    with pytest.raises(ApplyError, match="comment_thread_selection_mismatch"):
        prepare(parts, tmp_path, comments_policy=policy)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "instruction",
    [
        "DOCPROPERTY InternalNote",
        "AUTHOR",
        "TITLE",
        'INCLUDETEXT "other.docx"',
        "UNKNOWN",
    ],
)
@pytest.mark.parametrize("simple", [True, False])
def test_property_consumer_or_unknown_field_refuses(tmp_path, instruction, simple):
    parts = fictional_parts(comments=False)
    doc = parse_xml(parts["word/document.xml"])
    paragraph = doc.find(f"{w('body')}/{w('p')}")
    if simple:
        field = etree.SubElement(paragraph, w("fldSimple"), {w("instr"): instruction})
        etree.SubElement(etree.SubElement(field, w("r")), w("t")).text = "cached"
    else:
        for tag, attrs, text in [
            ("fldChar", {w("fldCharType"): "begin"}, None),
            ("instrText", {}, instruction),
            ("fldChar", {w("fldCharType"): "separate"}, None),
            ("t", {}, "cached"),
            ("fldChar", {w("fldCharType"): "end"}, None),
        ]:
            etree.SubElement(
                etree.SubElement(paragraph, w("r")), w(tag), attrs
            ).text = text
    parts["word/document.xml"] = c._xml(doc)
    with pytest.raises(ApplyError, match="property_dependency_unverified"):
        prepare(parts, tmp_path, properties_policy=REMOVE_PROPS)


def test_automatic_date_instruction_and_locked_setting_unchanged(tmp_path):
    parts = fictional_parts(comments=False)
    doc = parse_xml(parts["word/document.xml"])
    paragraph = doc.find(f"{w('body')}/{w('p')}")
    field = etree.SubElement(
        paragraph,
        w("fldSimple"),
        {w("instr"): 'DATE \\@ "yyyy-MM-dd"', w("fldLock"): "false"},
    )
    etree.SubElement(etree.SubElement(field, w("r")), w("t")).text = "2026-10-27"
    parts["word/document.xml"] = c._xml(doc)
    plan = prepare(parts, tmp_path, properties_policy=REMOVE_PROPS)
    assert (
        unpack(plan.files[0].payload)["word/document.xml"] == parts["word/document.xml"]
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"customXml/unknown.xml": b'<unknown xmlns="urn:future">Keep</unknown>'},
        {"opaque.bin": b"Preserve unknown embedded data"},
        {"word/commentsFuture.xml": b'<unknown xmlns="urn:future"/>'},
    ],
)
def test_unverified_package_refuses_cleanup_but_exact_redline_still_possible(
    tmp_path, extra
):
    parts = fictional_parts()
    parts.update(extra)
    before = deepcopy(parts)
    with pytest.raises(ApplyError, match="outgoing_cleanup_unsupported"):
        prepare(parts, tmp_path, comments_policy={"mode": "remove_all"})
    assert prepare(parts, tmp_path).files[0].payload == pack(parts)
    assert parts == before


def test_orphan_anchor_and_orphan_metadata_refused(tmp_path):
    for variant in ("anchor", "metadata"):
        parts = fictional_parts()
        if variant == "anchor":
            root = parse_xml(parts["word/document.xml"])
            etree.SubElement(
                root.find(f"{w('body')}/{w('p')}"),
                w("commentRangeStart"),
                {w("id"): "999"},
            )
            parts["word/document.xml"] = c._xml(root)
        else:
            root = parse_xml(parts["word/commentsExtended.xml"])
            etree.SubElement(
                root, f"{{{c._W15}}}commentEx", {f"{{{c._W15}}}paraId": "00000999"}
            )
            parts["word/commentsExtended.xml"] = c._xml(root)
        with pytest.raises(ApplyError, match="outgoing_cleanup_unsupported"):
            prepare(parts, tmp_path, comments_policy={"mode": "remove_all"})


@pytest.mark.parametrize(
    "comments,props",
    [
        ({"mode": "remove_resolved"}, "preserve_all"),
        ({"mode": "remove_all", "author": "Fictional Counsel"}, "preserve_all"),
        ({"mode": "remove_threads", "threads": []}, "preserve_all"),
        ("preserve_all", {"mode": "remove_selected", "fields": []}),
        (
            "preserve_all",
            {
                "mode": "remove_selected",
                "fields": [{"part": "core", "name": "unknown"}],
            },
        ),
    ],
)
def test_closed_policy_inputs(comments, props):
    with pytest.raises(ApplyError, match="invalid_cleanup_policy"):
        cleanup.policies(comments, props)


def test_remove_all_on_document_without_comments(tmp_path):
    parts = fictional_parts(comments=False)
    plan = prepare(parts, tmp_path, comments_policy={"mode": "remove_all"})
    assert plan.files[0].payload == pack(parts)
    assert json.loads(plan.report)["cleanup"]["all_comments_removed"]


def test_selected_foreign_insertion_anchors_removed_without_relabeling(tmp_path):
    parts = fictional_parts()
    doc = parse_xml(parts["word/document.xml"])
    paragraph = doc.find(f"{w('body')}/{w('p')}")
    insertion = etree.Element(
        w("ins"), {w("id"): "99", w("author"): "Fictional Counterparty"}
    )
    for node in list(paragraph):
        insertion.append(node)
    paragraph.append(insertion)
    parts["word/document.xml"] = c._xml(doc)
    plan = prepare(
        parts,
        tmp_path,
        comments_policy={"mode": "remove_all"},
        clean_draft_name="Clean.docx",
    )
    redline = parse_xml(unpack(plan.files[0].payload)["word/document.xml"])
    assert [(n.get(w("id")), n.get(w("author"))) for n in redline.iter(w("ins"))] == [
        ("99", "Fictional Counterparty"),
        ("10", "Fictional Counterparty"),
    ]
    assert [n.text for n in redline.iter(w("t"))] == [n.text for n in doc.iter(w("t"))]
    assert not list(
        parse_xml(unpack(plan.files[1].payload)["word/document.xml"]).iter(w("ins"))
    )


@pytest.mark.parametrize(
    "variant",
    [
        "missing",
        "duplicate",
        "alias",
        "numeric_pid_alias",
        "invalid_unselected_pid",
        "noncanonical",
        "vector",
        "linked",
        "unknown_attribute",
    ],
)
def test_selected_property_ambiguity_and_dependencies_refuse(tmp_path, variant):
    parts = fictional_parts(comments=False)
    path = "docProps/custom.xml"
    root = parse_xml(parts[path])
    if variant == "missing":
        root.remove(root[0])
    elif variant == "duplicate":
        node = deepcopy(root[0])
        node.set("pid", "4")
        root.append(node)
    elif variant == "alias":
        root[1].set("name", "internalnote")
    elif variant == "numeric_pid_alias":
        root[1].set("pid", "02")
    elif variant == "invalid_unselected_pid":
        root[1].set("pid", "not-an-integer")
    elif variant == "noncanonical":
        rels = parse_xml(parts["_rels/.rels"])
        next(n for n in rels if n.get("Id") == "custom").set(
            "Target", "docProps/../docProps/custom.xml"
        )
        parts["_rels/.rels"] = c._xml(rels)
    elif variant == "vector":
        root[0][0].tag = f"{{{cleanup.VT}}}vector"
    elif variant == "linked":
        root[0].set("linkTarget", "SomeBookmark")
    else:
        root[0].set("unexpected", "do not drop")
    parts[path] = c._xml(root)
    before = pack(parts)
    with pytest.raises(ApplyError):
        prepare(parts, tmp_path, properties_policy=REMOVE_PROPS)
    assert pack(parts) == before and not (tmp_path / "out").exists()


def test_unambiguous_leading_zero_property_id_is_preserved(tmp_path):
    parts = fictional_parts(comments=False)
    root = parse_xml(parts["docProps/custom.xml"])
    root[1].set("pid", "0000000003")
    parts["docProps/custom.xml"] = c._xml(root)
    plan = prepare(parts, tmp_path, properties_policy=REMOVE_PROPS)
    after = parse_xml(unpack(plan.files[0].payload)["docProps/custom.xml"])
    assert len(after) == 1
    assert after[0].get("name") == "PublicCode"
    assert after[0].get("pid") == "0000000003"


@pytest.mark.parametrize(
    "variant",
    ["custom_prefix", "custom_value_tail", "custom_tail", "core_tail", "app_tail"],
)
def test_unknown_mixed_property_text_refuses_without_losing_data(tmp_path, variant):
    parts = fictional_parts(comments=False)
    kind = variant.split("_")[0]
    path = cleanup.PROPS[kind][0]
    root = parse_xml(parts[path])
    if variant == "custom_prefix":
        root[0].text = "Unknown payload before value"
    elif variant == "custom_value_tail":
        root[0][0].tail = "Unknown payload after value"
    else:
        root[0].tail = "Unknown payload between properties"
    parts[path] = c._xml(root)
    original = deepcopy(parts)
    with pytest.raises(ApplyError, match="outgoing_cleanup_unsupported"):
        prepare(parts, tmp_path, properties_policy=REMOVE_PROPS)
    assert parts == original and not (tmp_path / "out").exists()


def test_property_indentation_is_allowed_without_changing_retained_values(tmp_path):
    parts = fictional_parts(comments=False)
    root = parse_xml(parts["docProps/custom.xml"])
    root[0].text, root[0][0].tail, root[0].tail = "\n  ", "\n", "\n  "
    parts["docProps/custom.xml"] = c._xml(root)
    plan = prepare(parts, tmp_path, properties_policy=REMOVE_PROPS)
    after = parse_xml(unpack(plan.files[0].payload)["docProps/custom.xml"])
    assert len(after) == 1 and after[0].get("name") == "PublicCode"
    assert after[0][0].text == "42"


@pytest.mark.parametrize(
    "target,slot",
    [
        ("comment", "text"),
        ("comment", "tail"),
        ("paragraph", "text"),
        ("paragraph", "tail"),
        ("run", "text"),
        ("run", "tail"),
        ("value", "tail"),
        ("commentRangeStart", "text"),
        ("commentRangeStart", "tail"),
        ("commentRangeEnd", "text"),
        ("commentRangeEnd", "tail"),
        ("commentReference", "text"),
        ("commentReference", "tail"),
        ("reference_run", "text"),
        ("reference_run", "tail"),
        ("metadata", "text"),
        ("metadata", "tail"),
    ],
)
def test_unpreviewed_comment_and_marker_text_refuses(tmp_path, target, slot):
    parts = fictional_parts()
    comments = parse_xml(parts[c._COMMENTS])
    document = parse_xml(parts["word/document.xml"])
    metadata = parse_xml(parts["word/commentsExtended.xml"])
    nodes = {
        "comment": comments[0],
        "paragraph": comments[0][0],
        "run": comments[0].find(f".//{w('r')}"),
        "value": comments[0].find(f".//{w('t')}"),
        "metadata": metadata[0],
    }
    for tag in ("commentRangeStart", "commentRangeEnd", "commentReference"):
        nodes[tag] = next(n for n in document.iter(w(tag)) if n.get(w("id")) == "0")
    nodes["reference_run"] = nodes["commentReference"].getparent()
    setattr(nodes[target], slot, "Unpreviewed payload must survive")
    parts[c._COMMENTS] = c._xml(comments)
    parts["word/document.xml"] = c._xml(document)
    parts["word/commentsExtended.xml"] = c._xml(metadata)
    original = deepcopy(parts)
    with pytest.raises(ApplyError, match="outgoing_cleanup_unsupported"):
        prepare(parts, tmp_path, comments_policy=REMOVE_THREAD)
    assert parts == original and not (tmp_path / "out").exists()


def test_comment_indentation_remains_supported(tmp_path):
    parts = fictional_parts()
    for path in (c._COMMENTS, "word/document.xml", "word/commentsExtended.xml"):
        root = parse_xml(parts[path])
        etree.indent(root, space="  ")
        parts[path] = c._xml(root)
    plan = prepare(parts, tmp_path, comments_policy=REMOVE_THREAD)
    report = json.loads(plan.report)["cleanup"]
    assert len(report["removed_comments"]) == 2
    output = unpack(plan.files[0].payload)
    assert [
        r["id"] for r in c.read_comments(output, parse_xml(output["word/document.xml"]))
    ] == ["2"]


@pytest.mark.parametrize("leaf", ["t", "tab", "br", "cr", "annotationRef"])
def test_nested_comment_text_leaf_is_not_treated_as_ordinary_text(tmp_path, leaf):
    parts = fictional_parts()
    root = parse_xml(parts[c._COMMENTS])
    run = root[0].find(f".//{w('r')}")
    node = run.find(w("t")) if leaf == "t" else etree.SubElement(run, w(leaf))
    etree.SubElement(node, w("t")).text = "Unknown nested payload"
    parts[c._COMMENTS] = c._xml(root)
    original = deepcopy(parts)
    with pytest.raises(ApplyError, match="outgoing_cleanup_unsupported"):
        prepare(parts, tmp_path, comments_policy=REMOVE_THREAD)
    assert parts == original and not (tmp_path / "out").exists()


def test_original_property_field_cannot_be_hidden_by_deleting_its_comment(tmp_path):
    parts = fictional_parts()
    comments = parse_xml(parts[c._COMMENTS])
    field = etree.SubElement(
        comments[0][0], w("fldSimple"), {w("instr"): "DOCPROPERTY InternalNote"}
    )
    etree.SubElement(etree.SubElement(field, w("r")), w("t")).text = "cached value"
    parts[c._COMMENTS] = c._xml(comments)
    with pytest.raises(ApplyError, match="property_dependency_unverified"):
        prepare(
            parts,
            tmp_path,
            comments_policy={"mode": "remove_all"},
            properties_policy=REMOVE_PROPS,
        )


def test_changed_cleanup_policy_invalidates_prepared_plan_before_file_creation(
    tmp_path,
):
    from dataclasses import replace

    plan = prepare(fictional_parts(), tmp_path, comments_policy=REMOVE_THREAD)
    changed = replace(plan, comments_policy={"mode": "remove_all"})
    with pytest.raises(snapshots.SnapshotError, match="outgoing_plan_mismatch"):
        outgoing.publish_outgoing_package(
            changed, tmp_path / "out", expected_plan_sha256=plan.plan_sha256
        )
    assert not (tmp_path / "out").exists()
