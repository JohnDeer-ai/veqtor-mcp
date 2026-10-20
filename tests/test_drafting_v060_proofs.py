# SPDX-License-Identifier: Apache-2.0
"""Independent transaction checks catch corruption after paragraph helper proofs."""

import hashlib
import io
import zipfile

import pytest
from lxml import etree

from veqtor_docx import ApplyError, drafting, inspect_document
from veqtor_docx._ooxml import DOCUMENT_PART, w


PROFILE = {"author": "Shilov, Ilya", "initials": "IS", "producer_build": "v060-proof-test"}
HIDDEN_ID = "700"


def _fixture(tmp_path, demo_dir):
    path = tmp_path / "working.docx"
    original = (demo_dir / "round-1-outgoing-draft.docx").read_bytes()
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        infos = archive.infolist()
        parts = {info.filename: archive.read(info) for info in infos}
    document = etree.fromstring(parts[DOCUMENT_PART])
    paragraph = document.find(f"{w('body')}/{w('p')}")
    deletion = etree.SubElement(paragraph, w("del"))
    deletion.set(w("id"), HIDDEN_ID)
    deletion.set(w("author"), "Counterparty Reviewer")
    deletion.set(w("date"), "2026-01-02T03:04:05")
    etree.SubElement(etree.SubElement(deletion, w("r")), w("delText")).text = "Earlier hidden proposal"
    parts[DOCUMENT_PART] = etree.tostring(document, xml_declaration=True, encoding="UTF-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for info in infos:
            archive.writestr(info, parts[info.filename])
    path.write_bytes(output.getvalue())
    reference = inspect_document(str(path), "browse", max_items=1)["paragraphs"][0]["paragraph_ref"]
    return path, {
        "kind": "replace_paragraph",
        "paragraph_ref": reference,
        "old_text": "Master Development and Supply Agreement",
        "new_text": "Replacement agreement title.",
    }


def _damage_hidden(root, damage):
    deletion = next(node for node in root.iter(w("del")) if node.get(w("id")) == HIDDEN_ID)
    if damage == "hidden_text":
        next(deletion.iter(w("delText"))).text = "Unrequested hidden rewrite"
    else:
        deletion.set(w("author"), "Unexpected Author")


def _arguments_and_proof(path, operation):
    arguments = (str(path), hashlib.sha256(path.read_bytes()).hexdigest(), [operation])
    dry = drafting.preflight_document_edits(*arguments, **PROFILE)
    assert dry["batch_applicable"], dry
    return arguments, dry["preflight_proof"]


def _assert_refused(path, arguments, proof, phase, *, operation_index):
    original = path.read_bytes()
    if phase == "preflight":
        result = drafting.preflight_document_edits(*arguments, **PROFILE)
        assert result["batch_applicable"] is False
        assert result["refusal_code"] == "round_trip_failed"
        assert result["preflight_proof"] is None
        assert result["operations"] == []
        assert result["blocking_operation_index"] == operation_index
    else:
        with pytest.raises(ApplyError, match="round_trip_failed"):
            drafting.apply_document_edits(*arguments, proof, **PROFILE)
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("phase", ["preflight", "apply"])
@pytest.mark.parametrize("damage", ["hidden_text", "author"])
def test_hidden_revision_damage_after_last_replacement_callback_is_refused(
    tmp_path, demo_dir, monkeypatch, phase, damage
):
    path, operation = _fixture(tmp_path, demo_dir)
    arguments, proof = _arguments_and_proof(path, operation)
    real_replace = drafting.replace_paragraph

    def corrupt_after_verified_steps(paragraph, *args, **kwargs):
        result = real_replace(paragraph, *args, **kwargs)
        # The helper has already called its independent verifier. Damage only
        # deleted history, keeping the requested visible reading unchanged.
        _damage_hidden(paragraph, damage)
        return result

    monkeypatch.setattr(drafting, "replace_paragraph", corrupt_after_verified_steps)
    _assert_refused(path, arguments, proof, phase, operation_index=0)


@pytest.mark.parametrize("phase", ["preflight", "apply"])
@pytest.mark.parametrize("damage", ["hidden_text", "author"])
@pytest.mark.parametrize("timing", ["before_first_step", "between_steps"])
def test_helper_supplied_snapshots_cannot_reset_the_verified_source_baseline(
    tmp_path, demo_dir, monkeypatch, phase, damage, timing
):
    path, operation = _fixture(tmp_path, demo_dir)
    arguments, proof = _arguments_and_proof(path, operation)
    real_replace = drafting.replace_paragraph

    def corrupt_before_snapshot(paragraph, *args, **kwargs):
        if timing == "before_first_step":
            # Both the helper's before and after snapshots will contain this
            # damage; comparing only those two snapshots cannot detect it.
            _damage_hidden(paragraph, damage)
        else:
            real_verify = kwargs["verify_step"]
            calls = 0

            def corrupt_after_first_callback(before, after, mutable, facts):
                nonlocal calls
                real_verify(before, after, mutable, facts)
                calls += 1
                if calls == 1:
                    # The next callback again supplies two equally damaged
                    # snapshots. Its before must match the last verified after.
                    _damage_hidden(after, damage)

            kwargs["verify_step"] = corrupt_after_first_callback
        return real_replace(paragraph, *args, **kwargs)

    monkeypatch.setattr(drafting, "replace_paragraph", corrupt_before_snapshot)
    _assert_refused(path, arguments, proof, phase, operation_index=0)


def test_independent_checker_refuses_foreign_removal_declared_as_own_edit():
    before = etree.Element(w("p"))
    foreign = etree.SubElement(before, w("ins"), {
        w("id"): "9", w("author"): "Counterparty Reviewer",
    })
    etree.SubElement(etree.SubElement(foreign, w("r")), w("t")).text = "Foreign proposal"
    after = etree.Element(w("p"))
    facts = {
        "operation": "edit_own_insertion", "old_text": "Foreign proposal", "new_text": "",
        "revision_ids": ["9"], "target_revision_id": "9",
        "target_author": "Counterparty Reviewer",
    }
    with pytest.raises(ApplyError, match="round_trip_failed"):
        drafting._check_revision_preservation(
            drafting._revision_state(before), after, foreign, facts, PROFILE["author"]
        )


@pytest.mark.parametrize("phase", ["preflight", "apply"])
@pytest.mark.parametrize("damage", ["hidden_text", "author"])
def test_hidden_revision_damage_during_serialization_is_refused(
    tmp_path, demo_dir, monkeypatch, phase, damage
):
    path, operation = _fixture(tmp_path, demo_dir)
    arguments, proof = _arguments_and_proof(path, operation)
    real_archive = drafting._output_archive_bytes

    def corrupt_serialization(infos, parts):
        changed = dict(parts)
        document = etree.fromstring(changed[DOCUMENT_PART])
        _damage_hidden(document, damage)
        changed[DOCUMENT_PART] = etree.tostring(
            document, xml_declaration=True, encoding="UTF-8", standalone=True
        )
        return real_archive(infos, changed)

    monkeypatch.setattr(drafting, "_output_archive_bytes", corrupt_serialization)
    _assert_refused(path, arguments, proof, phase, operation_index=None)


@pytest.mark.parametrize("phase", ["preflight", "apply"])
@pytest.mark.parametrize("damage", ["paragraph_attributes", "container_text"])
def test_insertion_does_not_exempt_untouched_paragraphs_from_collateral_checks(
    tmp_path, demo_dir, monkeypatch, phase, damage
):
    path, replacement = _fixture(tmp_path, demo_dir)
    operation = {"kind": "insert_paragraph", "paragraph_ref": replacement["paragraph_ref"],
                 "position": "before", "text": "New introductory clause."}
    arguments, proof = _arguments_and_proof(path, operation)
    real_insert = drafting.insert_paragraph

    def corrupt_neighbor(anchor, *args, **kwargs):
        added, result = real_insert(anchor, *args, **kwargs)
        untouched = anchor.getparent().findall(w("p"))[-1]
        assert untouched is not anchor and untouched is not added
        if damage == "paragraph_attributes":
            untouched.set(w("rsidR"), "FFFFFFFF")
        else:
            untouched.tail = "Unrequested container text"
        return added, result

    monkeypatch.setattr(drafting, "insert_paragraph", corrupt_neighbor)
    _assert_refused(path, arguments, proof, phase, operation_index=None)
