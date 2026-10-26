# SPDX-License-Identifier: Apache-2.0
"""Public readers must not add optional provenance inside immutable storage."""

import hashlib
from pathlib import Path

import pytest

from veqtor_docx import DocxError, inspect_document
from veqtor_mcp import records, server


def _read(kind, path):
    if kind == "inspect":
        return server.inspect_document(str(path), "browse", max_items=1)
    if kind == "verify":
        para = inspect_document(str(path), "browse", max_items=1)["paragraphs"][0]
        return server.verify_quote(str(path), para["paragraph_ref"], para["text"])
    if kind == "extract":
        return server.extract_redlines(str(path))
    if kind == "preflight":
        return server.preflight_edits(str(path), [])
    if kind == "list":
        return server.list_rounds(str(path.parent))
    if kind in {"map", "history"}:
        para = inspect_document(str(path), "browse", max_items=1)["paragraphs"][0]
        seed = {"path": str(path), "paragraph_ref": para["paragraph_ref"]}
        if kind == "map":
            return server.map_rounds(str(path.parent), {
                "schema_version": "round_map_seed.v1", **seed,
            })
        return server.trace_paragraph_history(str(path.parent), {
            "schema_version": "paragraph_history_seed.v1", **seed,
        }, {"schema_version": "paragraph_history_order.v1",
            "kind": "filename_lexicographic_v1"})
    raise AssertionError(kind)


@pytest.fixture
def saved_round(tmp_path, demo_dir):
    source = tmp_path / "incoming.docx"
    source.write_bytes((demo_dir / "round-1-outgoing-draft.docx").read_bytes())
    state = server.mutate_deal_state(str(tmp_path), None, "1" * 32, {
        "action": "start_round", "round_id": "round_" + "1" * 32,
        "previous_round_id": None, "path": str(source),
        "file_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    })
    return tmp_path, source, state, Path(state["documents"][0]["path"])


@pytest.mark.parametrize("kind", ["inspect", "verify", "extract", "preflight", "list", "map", "history"])
@pytest.mark.parametrize("alias", [False, True])
def test_snapshot_read_keeps_deal_state_readable(saved_round, kind, alias):
    folder, source, state, snapshot = saved_round
    original = source.read_bytes()
    inventory = {p.name: p.read_bytes() for p in snapshot.parent.iterdir()}
    path = snapshot
    if alias:
        link = folder / "read-alias"
        link.symlink_to(snapshot.parent, target_is_directory=True)
        path = link / snapshot.name

    if alias and kind in {"map", "history"}:
        # These readers already refuse symlinked workspace roots.
        with pytest.raises(ValueError, match="workspace_not_directory"):
            _read(kind, path)
    else:
        result = _read(kind, path)
        assert result["record_status"] == "disabled", result
        assert result["record_id"] is None
    assert {p.name: p.read_bytes() for p in snapshot.parent.iterdir()} == inventory
    assert server.read_deal_state(str(folder))["revision"] == state["revision"]
    assert source.read_bytes() == original
    assert not (folder / ".veqtor" / records.JOURNAL_NAME).exists()


@pytest.mark.parametrize("component", [".veqtor", ".VEQTOR", ".veqtor-backups", ".Veqtor-Backups"])
def test_private_storage_read_and_error_do_not_create_journal(tmp_path, demo_dir, component):
    root = tmp_path / component / "retained"
    root.mkdir(parents=True)
    path = root / "source.docx"
    path.write_bytes((demo_dir / "round-1-outgoing-draft.docx").read_bytes())
    before = path.read_bytes()
    result = _read("inspect", path)
    assert result["record_status"] == "disabled"
    with pytest.raises(DocxError):
        _read("extract", root / "missing.docx")
    assert list(root.iterdir()) == [path]
    assert path.read_bytes() == before


def test_ordinary_matter_still_records_reads(saved_round):
    folder, source, _state, _snapshot = saved_round
    result = _read("inspect", source)
    assert result["record_status"] == "written"
    assert (folder / ".veqtor" / records.JOURNAL_NAME).is_file()
    assert server.read_deal_state(str(folder))["status"] == "ok"


def test_unknown_snapshot_member_still_refuses_without_removal(saved_round):
    folder, _source, _state, snapshot = saved_round
    unknown = snapshot.parent / "unknown-data"
    unknown.write_bytes(b"preserve this evidence")
    with pytest.raises(ValueError, match="invalid_manifest"):
        server.read_deal_state(str(folder))
    assert unknown.read_bytes() == b"preserve this evidence"


def test_export_in_retained_private_storage_does_not_append_access_event(tmp_path, demo_dir):
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    source = ordinary / "source.docx"
    source.write_bytes((demo_dir / "round-1-outgoing-draft.docx").read_bytes())
    assert _read("inspect", source)["record_status"] == "written"
    destination = tmp_path / ".veqtor-backups" / "retained"
    destination.parent.mkdir()
    ordinary.rename(destination)
    before = {str(p.relative_to(destination)): p.read_bytes()
              for p in destination.rglob("*") if p.is_file()}
    result = server.export_decision_record(str(destination))
    assert result["record_status"] == "disabled"
    assert {str(p.relative_to(destination)): p.read_bytes()
            for p in destination.rglob("*") if p.is_file()} == before


def test_export_uninitialized_snapshot_stays_uninitialized(saved_round):
    _folder, _source, _state, snapshot = saved_round
    with pytest.raises(DocxError, match="workspace_uninitialized"):
        server.export_decision_record(str(snapshot.parent))
    assert {p.name for p in snapshot.parent.iterdir()} == {"source.docx", "manifest.json"}


def test_directory_moved_into_private_storage_before_append_stays_untouched(
    tmp_path, demo_dir, monkeypatch,
):
    root = tmp_path / "ordinary"
    root.mkdir()
    source = root / "source.docx"
    source.write_bytes((demo_dir / "round-1-outgoing-draft.docx").read_bytes())
    destination = tmp_path / ".veqtor-backups" / "retained"
    destination.parent.mkdir()
    original_open = records._open_workspace_fd
    opened = 0

    def move_after_open(path, identity):
        nonlocal opened
        fd = original_open(path, identity)
        if path == root:
            opened += 1
            if opened == 2:  # Canonical resolution completed; writer opened root.
                root.rename(destination)
        return fd

    monkeypatch.setattr(records, "_open_workspace_fd", move_after_open)
    result = _read("inspect", source)
    assert result["record_status"] == "write_failed"
    assert result["record_error"] == "workspace_changed"
    assert {p.name for p in destination.iterdir()} == {"source.docx"}
