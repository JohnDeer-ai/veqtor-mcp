# SPDX-License-Identifier: Apache-2.0
"""Author workflow acceptance through the public tools, using synthetic contracts."""

import copy
import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from lxml import etree
from mcp.client import Client

from veqtor_docx import drafting, inspect_document
from veqtor_docx._ooxml import w
from veqtor_docx.apply import ApplyError
from veqtor_mcp import server

AUTHOR = "Shilov, Ilya"
PROFILE = {"author": AUTHOR, "initials": "IS", "producer_build": "test-drafting-build"}


def _working(tmp_path, demo_dir):
    path = tmp_path / "working.docx"
    path.write_bytes((demo_dir / "round-1-outgoing-draft.docx").read_bytes())
    return path


def _ref(path, index=0):
    return inspect_document(str(path), "browse", max_items=100)["paragraphs"][index][
        "paragraph_ref"
    ]


def _replace(path, old, new, index=0):
    return {
        "kind": "replace_text",
        "paragraph_ref": _ref(path, index),
        "old_text": old,
        "new_text": new,
    }


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _save(path, operations, **kwargs):
    digest = _hash(path)
    preflight = drafting.preflight_document_edits(
        str(path), digest, operations, **PROFILE, **kwargs
    )
    assert preflight["batch_applicable"], preflight
    result = drafting.apply_document_edits(
        str(path), digest, operations, preflight["preflight_proof"], **PROFILE, **kwargs
    )
    assert result["output_sha256"] == _hash(Path(result["output_path"]))
    return result


def _document(path):
    with zipfile.ZipFile(path) as archive:
        return etree.fromstring(archive.read("word/document.xml"))


def test_same_file_author_reedit_comment_cycle(tmp_path, demo_dir):
    path = _working(tmp_path, demo_dir)
    original = path.read_bytes()
    first = _save(path, [_replace(path, "Supply", "Services")])
    assert first["save_mode"] == "in_place"
    assert Path(first["backup_path"]).read_bytes() == original
    insertion = next(_document(path).iter(w("ins")))
    original_attrs = dict(insertion.attrib)
    assert original_attrs[w("author")] == AUTHOR
    second = _save(path, [_replace(path, "Services", "Manufacturing")])
    own_insertions = [
        node
        for node in _document(path).iter(w("ins"))
        if node.get(w("author")) == AUTHOR
    ]
    assert len(own_insertions) == 1
    assert dict(own_insertions[0].attrib) == original_attrs
    assert "".join(own_insertions[0].itertext()) == "Manufacturing"
    assert second["backup_sha256"] == first["output_sha256"]
    _save(
        path,
        [
            {
                "kind": "add_comment",
                "paragraph_ref": _ref(path),
                "quote": "Master",
                "text": "Please confirm the scope.",
            }
        ],
    )
    listed = drafting.read_document_comments(str(path))
    comment = next(c for c in listed["comments"] if c["author"] == AUTHOR)
    assert comment["initials"] == "IS"
    _save(
        path,
        [
            {
                "kind": "update_comment",
                "comment_id": comment["id"],
                "text": "Scope confirmed; check the title.",
            }
        ],
    )
    assert any(
        c["text"] == "Scope confirmed; check the title."
        for c in drafting.read_document_comments(str(path))["comments"]
    )
    _save(path, [{"kind": "delete_comment", "comment_id": comment["id"]}])
    assert comment["id"] not in {
        c["id"] for c in drafting.read_document_comments(str(path))["comments"]
    }
    assert len(list(path.parent.glob(".veqtor-backups/*.docx"))) == 5
    with zipfile.ZipFile(path) as archive:
        assert (
            etree.fromstring(archive.read("word/settings.xml")).find(
                w("trackRevisions")
            )
            is not None
        )


def test_batch_is_sequential_and_atomic(tmp_path, demo_dir):
    path = _working(tmp_path, demo_dir)
    ref = _ref(path)
    operations = [
        {
            "kind": "replace_text",
            "paragraph_ref": ref,
            "old_text": "Supply",
            "new_text": "Services",
        },
        {
            "kind": "replace_text",
            "paragraph_ref": ref,
            "old_text": "Services",
            "new_text": "Manufacturing",
        },
        {
            "kind": "add_comment",
            "paragraph_ref": ref,
            "quote": "Manufacturing",
            "text": "New scope.",
        },
    ]
    before = path.read_bytes()
    bad = operations + [{"kind": "delete_comment", "comment_id": "99999"}]
    dry = drafting.preflight_document_edits(str(path), _hash(path), bad, **PROFILE)
    assert not dry["batch_applicable"]
    assert path.read_bytes() == before
    assert not (path.parent / ".veqtor-backups").exists()
    result = _save(path, operations)
    assert len(result["operations"]) == 3


@pytest.mark.parametrize(
    "drift",
    ["source", "author", "initials", "destination", "operations", "build", "proof"],
)
def test_preflight_proof_rejects_drift_without_publication(tmp_path, demo_dir, drift):
    path = _working(tmp_path, demo_dir)
    ops = [_replace(path, "Supply", "Services")]
    digest = _hash(path)
    proof = drafting.preflight_document_edits(str(path), digest, ops, **PROFILE)[
        "preflight_proof"
    ]
    profile, kwargs = dict(PROFILE), {}
    if drift == "source":
        path.write_bytes(path.read_bytes() + b" ")
    elif drift in {"author", "initials"}:
        profile[drift] += "X"
    elif drift == "destination":
        kwargs["output_path"] = str(tmp_path / "copy.docx")
    elif drift == "operations":
        ops[0]["new_text"] = "Manufacturing"
    elif drift == "build":
        profile["producer_build"] = "other-build"
    else:
        proof["proof_sha256"] = "0" * 64
    before = path.read_bytes()
    with pytest.raises(ApplyError):
        drafting.apply_document_edits(
            str(path), digest, ops, proof, **profile, **kwargs
        )
    assert path.read_bytes() == before
    assert not (path.parent / ".veqtor-backups").exists()
    assert not (tmp_path / "copy.docx").exists()


def test_copy_mode_preserves_source_and_foreign_parts(tmp_path, demo_dir):
    path = _working(tmp_path, demo_dir)
    original = path.read_bytes()
    out = tmp_path / "copy.docx"
    result = _save(path, [_replace(path, "Supply", "Services")], output_path=str(out))
    assert path.read_bytes() == original
    assert result["backup_path"] is None
    with zipfile.ZipFile(path) as before, zipfile.ZipFile(out) as after:
        for name in before.namelist():
            if name not in {
                "word/document.xml",
                "word/settings.xml",
                "word/_rels/document.xml.rels",
                "[Content_Types].xml",
            }:
                assert before.read(name) == after.read(name)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _payload(result):
    assert not result.is_error, result
    data = result.structured_content or json.loads(result.content[0].text)
    return data.get("result", data)


@pytest.mark.anyio
async def test_mcp_drafting_round_trip(tmp_path, demo_dir, monkeypatch):
    monkeypatch.setenv("VEQTOR_TRACKED_CHANGE_AUTHOR", AUTHOR)
    monkeypatch.setenv("VEQTOR_COMMENT_INITIALS", "IS")
    server._tracked_change_author.cache_clear()
    server._comment_initials.cache_clear()
    server._configured_author_profiles.cache_clear()
    path = _working(tmp_path, demo_dir)
    try:
        async with Client(server.mcp) as client:
            args = {
                "source_path": str(path),
                "file_sha256": _hash(path),
                "operations": [_replace(path, "Supply", "Services")],
            }
            dry = _payload(await client.call_tool("preflight_document_edits", args))
            assert dry["batch_applicable"], dry
            assert dry["tracked_change_author"] == AUTHOR
            saved = _payload(
                await client.call_tool(
                    "apply_document_edits",
                    {**args, "preflight_proof": dry["preflight_proof"]},
                )
            )
            assert saved["save_mode"] == "in_place"
            listed = _payload(
                await client.call_tool("read_document_comments", {"path": str(path)})
            )
            assert listed["file_sha256"] == saved["output_sha256"]
            args = {
                "source_path": str(path),
                "file_sha256": _hash(path),
                "operations": [_replace(path, "Services", "Manufacturing")],
            }
            dry = _payload(await client.call_tool("preflight_document_edits", args))
            assert dry["operations"][0]["operation"] == "edit_own_insertion"
            saved = _payload(
                await client.call_tool(
                    "apply_document_edits",
                    {**args, "preflight_proof": dry["preflight_proof"]},
                )
            )
            assert saved["record_status"] == "disabled"
    finally:
        server._tracked_change_author.cache_clear()
        server._comment_initials.cache_clear()
        server._configured_author_profiles.cache_clear()


def test_invalid_comment_page_bounds(tmp_path):
    for kwargs in ({"offset": True}, {"max_items": 101}, {"offset": -1}):
        with pytest.raises(ApplyError):
            drafting.read_document_comments(str(tmp_path / "absent.docx"), **kwargs)


def test_bad_batch_and_reference_write_nothing(tmp_path, demo_dir):
    path = _working(tmp_path, demo_dir)
    op = _replace(path, "Supply", "Services")
    bad = copy.deepcopy(op)
    bad["paragraph_ref"]["paragraph_index"] = 999
    for operations in ([], [bad], [{**op, "extra": "forbidden"}]):
        result = drafting.preflight_document_edits(
            str(path), _hash(path), operations, **PROFILE
        )
        assert not result["batch_applicable"]
    assert not (path.parent / ".veqtor-backups").exists()


def _rewrite_fixture(path, mutate_document=None, mutate_settings=None):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        parts = {info.filename: archive.read(info) for info in infos}
    if mutate_document:
        document = etree.fromstring(parts["word/document.xml"])
        mutate_document(document)
        parts["word/document.xml"] = etree.tostring(document)
    if mutate_settings:
        drafting._enable_tracking(parts)
        settings = etree.fromstring(parts["word/settings.xml"])
        mutate_settings(settings)
        parts["word/settings.xml"] = etree.tostring(settings)
    known = {info.filename for info in infos}
    with zipfile.ZipFile(path, "w") as archive:
        for info in infos:
            archive.writestr(info, parts[info.filename])
        for name in sorted(set(parts) - known):
            archive.writestr(name, parts[name])


def _field_marker(parent, kind):
    run = etree.SubElement(parent, w("r"))
    etree.SubElement(run, w("fldChar")).set(w("fldCharType"), kind)


@pytest.mark.parametrize("children, expected", [
    (
        '<w:view w:val="print"/><w:zoom w:percent="100"/>'
        '<w:proofState w:spelling="clean"/><w:defaultTabStop w:val="720"/><w:compat/>',
        ["view", "zoom", "proofState", "trackRevisions", "defaultTabStop", "compat"],
    ),
    (
        '<w:activeWritingStyle w:lang="en-US"/><w:activeWritingStyle w:lang="ru-RU"/>'
        '<w:revisionView w:markup="1"/><w:doNotTrackMoves/><w:doNotTrackFormatting/>',
        ["activeWritingStyle", "activeWritingStyle", "revisionView", "trackRevisions",
         "doNotTrackMoves", "doNotTrackFormatting"],
    ),
    ('<w:view w:val="print"/><w:zoom w:percent="100"/>',
     ["view", "zoom", "trackRevisions"]),
    ('<w:compat/><w14:docId w14:val="42"/>', ["trackRevisions", "compat", "docId"]),
    ('<w:zoom w:percent="100"/><w14:docId w14:val="42"/>',
     ["zoom", "trackRevisions", "docId"]),
    ('<x:unknown/><w:zoom w:percent="100"/><x:afterZoom/><w:compat/>',
     ["unknown", "zoom", "trackRevisions", "afterZoom", "compat"]),
    ('', ["trackRevisions"]),
])
def test_tracking_setting_follows_schema_order_preserving_existing_xml(
    demo_dir, children, expected
):
    with zipfile.ZipFile(demo_dir / "round-1-outgoing-draft.docx") as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    drafting._enable_tracking(parts)
    parts["word/settings.xml"] = (
        '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
        'xmlns:x="urn:veqtor:synthetic-settings-extension" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        f'mc:Ignorable="w14 x">{children}</w:settings>'
    ).encode()
    original = etree.fromstring(parts["word/settings.xml"])
    existing = [etree.tostring(child) for child in original]
    untouched_parts = {name: payload for name, payload in parts.items()
                       if name != "word/settings.xml"}
    drafting._enable_tracking(parts)
    settings = etree.fromstring(parts["word/settings.xml"])
    assert [etree.QName(child).localname for child in settings] == expected
    assert dict(settings.attrib) == dict(original.attrib)
    assert [etree.tostring(child) for child in settings
            if child.tag != w("trackRevisions")] == existing
    assert {name: payload for name, payload in parts.items()
            if name != "word/settings.xml"} == untouched_parts
    first = parts["word/settings.xml"]
    drafting._enable_tracking(parts)
    assert parts["word/settings.xml"] == first


def test_existing_disabled_tracking_keeps_its_position(tmp_path, demo_dir):
    path = _working(tmp_path, demo_dir)

    def settings_fixture(settings):
        settings.clear()
        etree.SubElement(settings, w("zoom")).set(w("percent"), "100")
        etree.SubElement(settings, w("trackRevisions")).set(w("val"), "false")
        etree.SubElement(settings, w("compat"))

    _rewrite_fixture(path, mutate_settings=settings_fixture)
    _save(path, [_replace(path, "Supply", "Services")])
    with zipfile.ZipFile(path) as archive:
        settings = etree.fromstring(archive.read("word/settings.xml"))
    assert [etree.QName(child).localname for child in settings] == [
        "zoom", "trackRevisions", "compat"
    ]
    assert settings[1].get(w("val")) == "true"


@pytest.mark.parametrize("new", ["Manufacturing", ""])
def test_foreign_formatting_on_own_insertion_refuses_before_publication(
    tmp_path, demo_dir, new
):
    path = _working(tmp_path, demo_dir)
    _save(path, [_replace(path, "Supply", "Services")])

    def format_insertion(document):
        insertion = next(node for node in document.iter(w("ins"))
                         if node.get(w("author")) == AUTHOR)
        run = insertion.find(w("r"))
        properties = run.find(w("rPr"))
        if properties is None:
            properties = etree.Element(w("rPr"))
            run.insert(0, properties)
        change = etree.SubElement(properties, w("rPrChange"))
        change.set(w("id"), "9001")
        change.set(w("author"), "Synthetic Counterparty")
        change.set(w("date"), "2026-01-01T10:00:00Z")
        etree.SubElement(change, w("rPr"))

    _rewrite_fixture(path, mutate_document=format_insertion)
    before = path.read_bytes()
    backups = {item.name: item.read_bytes()
               for item in (path.parent / ".veqtor-backups").iterdir()}
    dry = drafting.preflight_document_edits(
        str(path), _hash(path), [_replace(path, "Services", new)], **PROFILE
    )
    assert not dry["batch_applicable"]
    assert dry["refusal_code"] == "foreign_formatting_unsupported"
    assert path.read_bytes() == before
    assert {item.name: item.read_bytes()
            for item in (path.parent / ".veqtor-backups").iterdir()} == backups


@pytest.mark.parametrize(
    "mode", ["readOnly", "trackedChanges", "comments", "writeProtection", "invalid"]
)
@pytest.mark.parametrize("kind", ["replace_text", "add_comment"])
def test_document_protection_applies_to_all_drafting_writes(
    tmp_path, demo_dir, mode, kind
):
    path = _working(tmp_path, demo_dir)

    def protect(settings):
        if mode == "writeProtection":
            etree.SubElement(settings, w("writeProtection"))
        else:
            protection = etree.SubElement(settings, w("documentProtection"))
            protection.set(w("edit"), mode)
            protection.set(w("enforcement"), "bogus" if mode == "invalid" else "1")

    _rewrite_fixture(path, mutate_settings=protect)
    op = (
        _replace(path, "Supply", "Services")
        if kind == "replace_text"
        else {
            "kind": "add_comment",
            "paragraph_ref": _ref(path),
            "quote": "Supply",
            "text": "Synthetic note.",
        }
    )
    before = path.read_bytes()
    result = drafting.preflight_document_edits(str(path), _hash(path), [op], **PROFILE)
    assert result["refusal_code"] == "document_protected"
    assert path.read_bytes() == before
    assert not (path.parent / ".veqtor-backups").exists()


def test_explicitly_disabled_document_protection_does_not_block_drafting(
    tmp_path, demo_dir
):
    path = _working(tmp_path, demo_dir)

    def disabled(settings):
        protection = etree.SubElement(settings, w("documentProtection"))
        protection.set(w("edit"), "readOnly")
        protection.set(w("enforcement"), "0")

    _rewrite_fixture(path, mutate_settings=disabled)
    _save(path, [_replace(path, "Supply", "Services")])


@pytest.mark.parametrize(
    "context",
    [
        "field",
        "unclosed_field",
        "unmatched_field_end",
        "fake_field_end",
        "locked_sdt",
        "data_bound_sdt",
        "row_inserted",
        "cell_deleted",
        "paragraph_mark",
        "section_change",
        "move_range",
        "permission_range",
    ],
)
@pytest.mark.parametrize("kind", ["replace_text", "add_comment"])
def test_readable_reference_does_not_authorize_unsafe_context(
    tmp_path, demo_dir, context, kind
):
    path = _working(tmp_path, demo_dir)
    target_index = 0

    def mutate(document):
        nonlocal target_index
        body = document.find(w("body"))
        target = body.find(w("p"))
        if context in {
            "field",
            "unclosed_field",
            "unmatched_field_end",
            "fake_field_end",
        }:
            begin = etree.Element(w("p"))
            if context != "unmatched_field_end":
                _field_marker(begin, "begin")
            body.insert(0, begin)
            target_index = 1
            if context != "unclosed_field":
                end = etree.Element(w("p"))
                if context == "fake_field_end":
                    end_run = etree.SubElement(end, w("r"))
                    drawing = etree.SubElement(end_run, w("drawing"))
                    box = etree.SubElement(drawing, w("txbxContent"))
                    _field_marker(etree.SubElement(box, w("p")), "end")
                else:
                    _field_marker(end, "end")
                if context == "fake_field_end":
                    body.insert(1, end)
                    target_index = 2
                else:
                    body.insert(2, end)
        elif context in {"locked_sdt", "data_bound_sdt"}:
            control = etree.Element(w("sdt"))
            props = etree.SubElement(control, w("sdtPr"))
            if context == "locked_sdt":
                etree.SubElement(props, w("lock")).set(w("val"), "sdtContentLocked")
            else:
                etree.SubElement(props, w("dataBinding")).set(
                    w("xpath"), "/synthetic/value"
                )
            content = etree.SubElement(control, w("sdtContent"))
            content.append(target)
            body.insert(0, control)
        elif context in {"row_inserted", "cell_deleted"}:
            table = etree.Element(w("tbl"))
            row = etree.SubElement(table, w("tr"))
            cell = etree.SubElement(row, w("tc"))
            props = etree.SubElement(
                row if context == "row_inserted" else cell,
                w("trPr" if context == "row_inserted" else "tcPr"),
            )
            revision = etree.SubElement(
                props, w("ins" if context == "row_inserted" else "cellDel")
            )
            revision.set(w("id"), "451")
            revision.set(w("author"), "Synthetic Other")
            cell.append(target)
            body.insert(0, table)
        elif context == "paragraph_mark":
            props = target.find(w("pPr"))
            if props is None:
                props = etree.Element(w("pPr"))
                target.insert(0, props)
            revision = etree.SubElement(etree.SubElement(props, w("rPr")), w("del"))
            revision.set(w("id"), "451")
            revision.set(w("author"), "Synthetic Other")
        elif context == "section_change":
            section = body.find(w("sectPr"))
            if section is None:
                section = etree.SubElement(body, w("sectPr"))
            change = etree.SubElement(section, w("sectPrChange"))
            change.set(w("id"), "451")
            change.set(w("author"), "Synthetic Other")
            etree.SubElement(change, w("sectPr"))
        else:
            prefix = "moveToRange" if context == "move_range" else "perm"
            start = etree.Element(w(prefix + "Start"))
            end = etree.Element(w(prefix + "End"))
            start.set(w("id"), "451")
            end.set(w("id"), "451")
            body.insert(0, start)
            body.insert(2, end)

    _rewrite_fixture(path, mutate_document=mutate)
    # Browse omits empty paragraphs, whereas references retain canonical indexes.
    reference = next(
        item["paragraph_ref"]
        for item in inspect_document(str(path), "browse", max_items=100)["paragraphs"]
        if item["paragraph_ref"]["paragraph_index"] == target_index
    )
    operation = (
        {
            "kind": "replace_text",
            "paragraph_ref": reference,
            "old_text": "Supply",
            "new_text": "Services",
        }
        if kind == "replace_text"
        else {
            "kind": "add_comment",
            "paragraph_ref": reference,
            "quote": "Supply",
            "text": "Synthetic note.",
        }
    )
    before = path.read_bytes()
    result = drafting.preflight_document_edits(
        str(path), _hash(path), [operation], **PROFILE
    )
    assert result["refusal_code"] == "drafting_context_unsupported", result
    assert result["blocking_operation_index"] == 0
    assert path.read_bytes() == before
    assert not (path.parent / ".veqtor-backups").exists()


@pytest.mark.parametrize("location", ["paragraph", "paragraph_mark", "previous_mark", "next_mark"])
def test_native_word_property_history_and_neighbor_marks_survive_reedit(tmp_path, demo_dir, location):
    path = _working(tmp_path, demo_dir)
    _save(path, [_replace(path, "Supply", "Services")])

    def word_save(document):
        body = document.find(w("body"))
        paragraph = body.find(w("p"))
        if location in {"previous_mark", "next_mark"}:
            neighbor = etree.Element(w("p"))
            body.insert(0 if location == "previous_mark" else 1, neighbor)
            paragraph = neighbor
        props = paragraph.find(w("pPr"))
        if props is None:
            props = etree.Element(w("pPr"))
            paragraph.insert(0, props)
        if location == "paragraph":
            change = etree.SubElement(props, w("pPrChange"))
            etree.SubElement(change, w("pPr"))
        else:
            run_props = etree.SubElement(props, w("rPr"))
            change = etree.SubElement(run_props, w("rPrChange" if location == "paragraph_mark" else "del"))
            if location == "paragraph_mark":
                old = etree.SubElement(change, w("rPr"))
                etree.SubElement(old, w("lang")).set(w("val"), "en-US")
        change.set(w("id"), "451")
        change.set(w("author"), "Other Reviewer")
        change.set(w("date"), "2026-10-15T10:00:00Z")

    _rewrite_fixture(path, mutate_document=word_save)
    original_revision = dict(next(_document(path).iter(w("ins"))).attrib)
    properties = [etree.tostring(p, method="c14n") for p in _document(path).iter(w("pPr"))]
    reference = next(p["paragraph_ref"] for p in inspect_document(str(path), "browse", max_items=100)["paragraphs"]
                     if "Services" in p["text"])
    saved = _save(path, [{"kind": "replace_text", "paragraph_ref": reference,
                          "old_text": "Services", "new_text": "Manufacturing"}])
    assert saved["operations"][0]["operation"] == "edit_own_insertion"
    assert dict(next(_document(path).iter(w("ins"))).attrib) == original_revision
    assert [etree.tostring(p, method="c14n") for p in _document(path).iter(w("pPr"))] == properties


@pytest.mark.parametrize("stamp", [
    "2026-10-16T10:00:00Z", "2026-10-16T13:00:00+03:00",
    "2026-10-16T05:00:00-05:00", "2026-10-16T15:30:00+05:30",
])
def test_new_dates_are_bound_to_preflight_and_survive_later_reedit(tmp_path, demo_dir, monkeypatch, stamp):
    path = _working(tmp_path, demo_dir)
    operations = [_replace(path, "Supply", "Services"), {
        "kind": "add_comment", "paragraph_ref": _ref(path),
        "quote": "Services", "text": "Please confirm.",
    }]
    monkeypatch.setattr(drafting, "_new_timestamp", lambda: stamp)
    digest = _hash(path)
    dry = drafting.preflight_document_edits(str(path), digest, operations, **PROFILE)
    assert dry["batch_applicable"], dry
    assert dry["preflight_proof"]["timestamp"] == stamp
    monkeypatch.setattr(drafting, "_new_timestamp", lambda: "2026-10-17T11:00:00-05:00")
    saved = drafting.apply_document_edits(str(path), digest, operations, dry["preflight_proof"], **PROFILE)
    assert saved["output_sha256"] == dry["preflight_proof"]["candidate_sha256"]
    assert {node.get(w("date")) for tag in ("ins", "del") for node in _document(path).iter(w(tag))} == {stamp[:19]}
    with zipfile.ZipFile(path) as archive:
        comment = etree.fromstring(archive.read("word/comments.xml"))[0]
        comment_id = comment.get(w("id"))
        assert comment.get(w("date")) == stamp[:19]
    _save(path, [_replace(path, "Services", "Manufacturing"),
                 {"kind": "update_comment", "comment_id": comment_id, "text": "Confirmed."}])
    assert next(_document(path).iter(w("ins"))).get(w("date")) == stamp[:19]
    with zipfile.ZipFile(path) as archive:
        assert etree.fromstring(archive.read("word/comments.xml"))[0].get(w("date")) == stamp[:19]


@pytest.mark.parametrize("timestamp", [
    None, "", "2026-02-30T10:00:00Z", "2026-10-16T10:00:00Z\n",
    "2026-10-16T10:00:00", "2026-10-16T10:00:00+14:01",
    "2026-10-16T10:00:00-15:00", "2026-10-16T10:00:00+03:60",
])
def test_invalid_proof_timestamp_never_publishes(tmp_path, demo_dir, timestamp):
    path = _working(tmp_path, demo_dir)
    operations = [_replace(path, "Supply", "Services")]
    before = path.read_bytes()
    dry = drafting.preflight_document_edits(str(path), _hash(path), operations, **PROFILE)
    proof = {**dry["preflight_proof"], "timestamp": timestamp}
    with pytest.raises(ApplyError, match="preflight_binding_mismatch"):
        drafting._proof_timestamp(proof)
    with pytest.raises(ApplyError, match="preflight_binding_mismatch"):
        drafting.apply_document_edits(str(path), _hash(path), operations, proof, **PROFILE)
    assert path.read_bytes() == before
    assert not (path.parent / ".veqtor-backups").exists()


def test_changing_only_proof_offset_cannot_reuse_same_wall_time_candidate(tmp_path, demo_dir, monkeypatch):
    path = _working(tmp_path, demo_dir)
    operations = [_replace(path, "Supply", "Services")]
    original = path.read_bytes()
    monkeypatch.setattr(drafting, "_new_timestamp", lambda: "2026-10-16T13:00:00+03:00")
    dry = drafting.preflight_document_edits(str(path), _hash(path), operations, **PROFILE)
    proof = {**dry["preflight_proof"], "timestamp": "2026-10-16T13:00:00-05:00"}
    assert drafting._proof_timestamp(proof) == proof["timestamp"]
    with pytest.raises(ApplyError, match="preflight_binding_mismatch"):
        drafting.apply_document_edits(str(path), _hash(path), operations, proof, **PROFILE)
    assert path.read_bytes() == original
    assert not (path.parent / ".veqtor-backups").exists()


def test_closed_field_before_target_and_bookmark_enclosing_target_are_allowed(
    tmp_path, demo_dir
):
    path = _working(tmp_path, demo_dir)

    def mutate(document):
        body = document.find(w("body"))
        field = etree.Element(w("p"))
        _field_marker(field, "begin")
        _field_marker(field, "end")
        body.insert(0, field)
        start = etree.Element(w("bookmarkStart"))
        start.set(w("id"), "22")
        start.set(w("name"), "SyntheticBookmark")
        end = etree.Element(w("bookmarkEnd"))
        end.set(w("id"), "22")
        body.insert(1, start)
        body.insert(3, end)

    _rewrite_fixture(path, mutate_document=mutate)
    _save(path, [_replace(path, "Supply", "Services", index=1)])


def test_comment_own_insertion_then_reedit_and_remove_text_preserves_comment(
    tmp_path, demo_dir
):
    path = _working(tmp_path, demo_dir)
    _save(path, [_replace(path, "Supply", "New Services")])
    _save(
        path,
        [
            {
                "kind": "add_comment",
                "paragraph_ref": _ref(path),
                "quote": "New Services",
                "text": "Check the chosen name.",
            }
        ],
    )
    comment = next(
        c
        for c in drafting.read_document_comments(str(path))["comments"]
        if c["author"] == AUTHOR
    )
    insertion = next(
        node
        for node in _document(path).iter(w("ins"))
        if node.get(w("author")) == AUTHOR
    )
    attributes = dict(insertion.attrib)
    _save(path, [_replace(path, "Services", "Manufacturing")])
    insertion = next(
        node
        for node in _document(path).iter(w("ins"))
        if node.get(w("author")) == AUTHOR
    )
    assert dict(insertion.attrib) == attributes
    _save(path, [_replace(path, "New Manufacturing", "")])
    document = _document(path)
    assert not [
        node for node in document.iter(w("ins")) if node.get(w("author")) == AUTHOR
    ]
    for tag in ("commentRangeStart", "commentRangeEnd", "commentReference"):
        assert (
            len(
                [
                    node
                    for node in document.iter(w(tag))
                    if node.get(w("id")) == comment["id"]
                ]
            )
            == 1
        )
    assert any(
        item["id"] == comment["id"] and item["paragraph_indexes"] == [0]
        for item in drafting.read_document_comments(str(path))["comments"]
    )
    _save(path, [{"kind": "delete_comment", "comment_id": comment["id"]}])


def test_delete_own_span_crossing_partial_comment_range_refuses_without_orphaning(
    tmp_path, demo_dir
):
    path = _working(tmp_path, demo_dir)
    _save(path, [_replace(path, "Supply", "New Services")])
    _save(
        path,
        [
            {
                "kind": "add_comment",
                "paragraph_ref": _ref(path),
                "quote": "Services",
                "text": "Check this word.",
            }
        ],
    )
    before = path.read_bytes()
    backups = set(path.parent.glob(".veqtor-backups/*"))
    result = drafting.preflight_document_edits(
        str(path), _hash(path), [_replace(path, "New Services", "")], **PROFILE
    )
    assert not result["batch_applicable"]
    assert path.read_bytes() == before
    assert set(path.parent.glob(".veqtor-backups/*")) == backups


@pytest.mark.parametrize(
    "damage", ["hidden_text", "author", "date", "paragraph_properties"]
)
def test_independent_check_detects_collateral_inside_touched_paragraph(
    tmp_path, demo_dir, monkeypatch, damage
):
    path = _working(tmp_path, demo_dir)

    def add_hidden(document):
        paragraph = document.find(w("body")).find(w("p"))
        deletion = etree.SubElement(paragraph, w("del"))
        deletion.set(w("id"), "700")
        deletion.set(w("author"), "Synthetic Other")
        deletion.set(w("date"), "2026-01-01T00:00:00Z")
        etree.SubElement(
            etree.SubElement(deletion, w("r")), w("delText")
        ).text = "Original hidden proposal"

    _rewrite_fixture(path, mutate_document=add_hidden)
    operations = [_replace(path, "Supply", "Services")]
    digest = _hash(path)
    proof = drafting.preflight_document_edits(str(path), digest, operations, **PROFILE)[
        "preflight_proof"
    ]
    assert proof
    real_replace = drafting.replace_text

    def corrupt(paragraph, *args, **kwargs):
        result = real_replace(paragraph, *args, **kwargs)
        foreign = next(
            node for node in paragraph.iter(w("del")) if node.get(w("id")) == "700"
        )
        if damage == "hidden_text":
            next(foreign.iter(w("delText"))).text = "Unrequested hidden change"
        elif damage in {"author", "date"}:
            foreign.set(w(damage), "Unexpected")
        else:
            properties = paragraph.find(w("pPr"))
            if properties is None:
                properties = etree.Element(w("pPr"))
                paragraph.insert(0, properties)
            etree.SubElement(properties, w("keepNext"))
        return result

    monkeypatch.setattr(drafting, "replace_text", corrupt)
    before = path.read_bytes()
    with pytest.raises(ApplyError, match="round_trip_failed"):
        drafting.apply_document_edits(str(path), digest, operations, proof, **PROFILE)
    assert path.read_bytes() == before
    assert not (path.parent / ".veqtor-backups").exists()
