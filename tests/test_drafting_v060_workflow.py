# SPDX-License-Identifier: Apache-2.0
"""End-to-end same-file paragraph/thread workflows with synthetic documents."""
import copy
import hashlib
import zipfile
from pathlib import Path

import jsonschema
import pytest
from lxml import etree

from veqtor_docx import drafting, inspect_document
from veqtor_docx._ooxml import w
from veqtor_mcp._drafting_contract import _APPLIED

PROFILE = {"author": "Shilov, Ilya", "initials": "IS", "producer_build": "v060-test"}


def fixture(tmp_path, demo_dir):
    path = tmp_path / "working.docx"
    path.write_bytes((demo_dir / "round-1-outgoing-draft.docx").read_bytes())
    return path


def inspect(path):
    return inspect_document(str(path), "browse", max_items=100)


def ref(path, index=0):
    return inspect(path)["paragraphs"][index]["paragraph_ref"]


def save(path, operations, profile=None):
    before = path.read_bytes()
    arguments = (str(path), hashlib.sha256(before).hexdigest(), operations)
    profile = profile or PROFILE
    preview = drafting.preflight_document_edits(*arguments, **profile)
    assert preview["batch_applicable"], preview
    assert path.read_bytes() == before
    jsonschema.validate(preview["operations"], _APPLIED)
    result = drafting.apply_document_edits(*arguments, preview["preflight_proof"], **profile)
    assert result["output_path"] == str(path)
    assert Path(result["backup_path"]).read_bytes() == before
    jsonschema.validate(result["operations"], _APPLIED)
    return result


def document(path):
    with zipfile.ZipFile(path) as archive:
        return etree.fromstring(archive.read("word/document.xml"))


@pytest.mark.parametrize("position", ["before", "after"])
def test_insert_then_reedit_new_paragraph_same_file(tmp_path, demo_dir, position):
    path = fixture(tmp_path, demo_dir)
    source = ref(path)
    save(path, [{"kind": "insert_paragraph", "paragraph_ref": source,
                 "position": position, "text": "New negotiated clause."}])
    inserted_index = 0 if position == "before" else 1
    save(path, [{"kind": "replace_text", "paragraph_ref": ref(path, inserted_index),
                 "old_text": "negotiated", "new_text": "agreed"}])
    save(path, [{"kind": "add_comment", "paragraph_ref": ref(path, inserted_index),
                 "quote": "agreed", "text": "Confirmed."}])
    rows = drafting.read_document_comments(str(path))["comments"]
    assert rows[-1]["paragraph_indexes"] == [inserted_index]
    assert rows[-1]["author"] == PROFILE["author"]


def test_insert_sequence_does_not_retarget_later_source_references(tmp_path, demo_dir):
    path = fixture(tmp_path, demo_dir)
    source = ref(path)
    save(path, [
        {"kind": "insert_paragraph", "paragraph_ref": source, "position": "after", "text": "First."},
        {"kind": "insert_paragraph", "paragraph_ref": source, "position": "after", "text": "Second."},
        {"kind": "insert_paragraph", "paragraph_ref": source, "position": "before", "text": "Before."},
        {"kind": "replace_text", "paragraph_ref": source, "old_text": "Supply", "new_text": "Services"},
    ])
    from veqtor_docx.apply import _paragraph_segments, _reading_text
    paragraphs = document(path).find(w("body")).findall(w("p"))
    assert [_reading_text(_paragraph_segments(p)) for p in paragraphs[:4]] == [
        "Before.", "Master Development and Services Agreement", "First.", "Second.",
    ]


def test_replace_entire_mixed_paragraph_preserves_original_and_authorship(tmp_path, demo_dir):
    path = fixture(tmp_path, demo_dir)
    save(path, [{"kind": "replace_text", "paragraph_ref": ref(path),
                 "old_text": "Supply", "new_text": "Services"}])
    save(path, [{"kind": "replace_paragraph", "paragraph_ref": ref(path),
                 "old_text": "Master Development and Services Agreement",
                 "new_text": "Replacement agreement title."}])
    para = document(path).find(f"{w('body')}/{w('p')}")
    assert "".join(n.text or "" for n in para.iter(w("t"))) == "Replacement agreement title."
    assert all(n.get(w("author")) == PROFILE["author"] for n in para.iter()
               if n.tag in {w("ins"), w("del")})
    # Original rejected reading survives even across prior own replacements.
    assert "".join(n.text or "" for n in para.iter(w("delText"))) == "Master Development and Supply Agreement"


def test_thread_reply_resolve_reopen_and_insert_then_delete_comment(tmp_path, demo_dir):
    path = fixture(tmp_path, demo_dir)
    other = {**PROFILE, "author": "Counterparty Reviewer", "initials": "CR"}
    save(path, [{"kind": "add_comment", "paragraph_ref": ref(path),
                 "quote": "Supply", "text": "Please explain."}], other)
    root = drafting.read_document_comments(str(path))["comments"][-1]["id"]
    save(path, [{"kind": "reply_comment", "comment_id": root, "text": "Agreed."}])
    rows = drafting.read_document_comments(str(path))["comments"]
    reply = rows[-1]
    assert reply["parent_comment_id"] == root
    assert reply["thread_root_id"] == root
    assert reply["author"] == PROFILE["author"]
    save(path, [{"kind": "set_comment_resolved", "comment_id": reply["id"], "resolved": True}])
    assert all(row["resolved"] for row in drafting.read_document_comments(str(path))["comments"])
    save(path, [{"kind": "set_comment_resolved", "comment_id": root, "resolved": False},
                {"kind": "reply_comment", "comment_id": root, "text": "One more point."}])
    source = ref(path)
    save(path, [{"kind": "add_comment", "paragraph_ref": source,
                 "quote": "Master", "text": "Temporary own note."}])
    own = drafting.read_document_comments(str(path))["comments"][-1]["id"]
    save(path, [
        {"kind": "insert_paragraph", "paragraph_ref": ref(path), "position": "before", "text": "Preface."},
        {"kind": "delete_comment", "comment_id": own},
    ])
    rows = drafting.read_document_comments(str(path))["comments"]
    assert all(row["paragraph_indexes"] == [1] for row in rows)
    assert rows[0]["author"] == "Counterparty Reviewer"
    assert rows[0]["text"] == "Please explain."


def test_failed_later_operation_rolls_back_whole_paragraph_batch(tmp_path, demo_dir):
    path = fixture(tmp_path, demo_dir)
    before = path.read_bytes()
    operations = [
        {"kind": "insert_paragraph", "paragraph_ref": ref(path), "position": "before", "text": "New."},
        {"kind": "replace_paragraph", "paragraph_ref": ref(path), "old_text": "Wrong expected text.", "new_text": "Replacement."},
    ]
    result = drafting.preflight_document_edits(str(path), hashlib.sha256(before).hexdigest(),
                                               operations, **PROFILE)
    assert result["batch_applicable"] is False
    assert result["blocking_operation_index"] == 1
    assert path.read_bytes() == before
    assert not (path.parent / ".veqtor-backups").exists()


def test_insert_proof_rejects_position_drift(tmp_path, demo_dir):
    path = fixture(tmp_path, demo_dir)
    before = path.read_bytes()
    operations = [{"kind": "insert_paragraph", "paragraph_ref": ref(path),
                   "position": "before", "text": "New."}]
    digest = hashlib.sha256(before).hexdigest()
    proof = drafting.preflight_document_edits(str(path), digest, operations, **PROFILE)["preflight_proof"]
    changed = copy.deepcopy(operations)
    changed[0]["position"] = "after"
    from veqtor_docx import ApplyError
    with pytest.raises(ApplyError, match="preflight"):
        drafting.apply_document_edits(str(path), digest, changed, proof, **PROFILE)
    assert path.read_bytes() == before
