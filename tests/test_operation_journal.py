# SPDX-License-Identifier: Apache-2.0
"""Fault boundaries of the independent two-record store and save integration."""
import hashlib
import errno
import json
import os
import uuid
from pathlib import Path

import pytest

from veqtor_docx import _drafting_io as io
from veqtor_docx import operation_journal as journal
from veqtor_docx.apply import ApplyError

SOURCE = b"old exact package bytes"
CANDIDATE = b"new exact package bytes"


def fixture(tmp_path):
    path = tmp_path / "working.docx"
    path.write_bytes(SOURCE)
    path.chmod(0o600)
    identifier = str(uuid.uuid4())
    store = journal.OperationJournal(identifier, source_path=str(path),
        plan={"operations": [{"kind": "synthetic", "old_author": "Other reviewer"}]},
        author_profile={"name": "Fictional Counsel", "initials": "FC"}, producer="test-build")
    folder = tmp_path / ".veqtor" / "operations" / identifier
    return path, store, folder


def save(path, store, output=None):
    return io.publish_document(str(path), str(output) if output else None,
                               SOURCE, CANDIDATE, journal=store)


def test_durable_two_records_and_exact_backup(tmp_path):
    path, store, folder = fixture(tmp_path)
    result = save(path, store)
    assert path.read_bytes() == CANDIDATE
    assert Path(result["backup_path"]).read_bytes() == SOURCE
    prepared = json.loads((folder / "prepared.json").read_text())
    completed = json.loads((folder / "result.json").read_text())
    assert prepared["intent"]["operation_id"] == store.operation_id
    assert prepared["backup"]["path"] == result["backup_path"]
    assert completed["result"]["observed_output_sha256"] == hashlib.sha256(CANDIDATE).hexdigest()
    assert completed["result"]["backup_verification"] == "matched"
    assert result["operation_journal"]["journal_status"] == "written"
    assert not completed["recovered"]
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in folder.iterdir())
    assert folder.stat().st_mode & 0o777 == 0o700


def test_copy_journal_has_no_backup_and_preserves_source(tmp_path):
    path, store, folder = fixture(tmp_path)
    output = tmp_path / "copy.docx"
    result = save(path, store, output)
    assert path.read_bytes() == SOURCE and output.read_bytes() == CANDIDATE
    assert result["operation_journal"]["backup_verification"] == "not_required"
    assert result["operation_journal"]["verification"] == "matched"
    assert json.loads((folder / "prepared.json").read_text())["backup"] is None


def test_prepare_failure_never_publishes_or_claims_success(tmp_path, monkeypatch):
    path, store, folder = fixture(tmp_path)
    original = journal._write

    def broken(fd, name, payload):
        if name == "prepared.json":
            raise OSError("fictional prepare failure")
        return original(fd, name, payload)

    monkeypatch.setattr(journal, "_write", broken)
    with pytest.raises(ApplyError, match="journal_prepare_failed"):
        save(path, store)
    assert path.read_bytes() == SOURCE and not (folder / "result.json").exists()


def test_result_failure_reports_published_then_recovers_without_reapply(tmp_path, monkeypatch):
    path, store, folder = fixture(tmp_path)
    original = journal._write

    def broken(fd, name, payload):
        if name == "result.json":
            raise OSError("fictional result failure")
        return original(fd, name, payload)

    with monkeypatch.context() as patch:
        patch.setattr(journal, "_write", broken)
        result = save(path, store)
    assert path.read_bytes() == CANDIDATE
    assert result["operation_journal"]["publication_status"] == "published"
    assert result["operation_journal"]["journal_status"] == "incomplete"
    assert not (folder / "result.json").exists()
    before = path.read_bytes()
    backups = {p.name: p.read_bytes() for p in (tmp_path / ".veqtor-backups").iterdir()}
    recovered = journal.recover_operation(str(path), store.operation_id)
    assert recovered["recovered"] and recovered["verification"] == "matched"
    assert recovered["recovery_status"] == "recovered"
    assert json.loads((folder / "result.json").read_text())["recovered"]
    repeat = journal.recover_operation(str(path), store.operation_id)
    assert repeat["recovery_status"] == "existing_result"
    assert path.read_bytes() == before
    assert backups == {p.name: p.read_bytes() for p in (tmp_path / ".veqtor-backups").iterdir()}


def test_interruption_after_publication_leaves_prepared_recoverable(tmp_path, monkeypatch):
    path, store, folder = fixture(tmp_path)

    def stopped(**kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(store, "finish", stopped)
    with pytest.raises(KeyboardInterrupt):
        save(path, store)
    assert path.read_bytes() == CANDIDATE and (folder / "prepared.json").exists()
    assert journal.recover_operation(str(path), store.operation_id)["recovered"]


@pytest.mark.parametrize("mutation", ["output", "missing_backup", "backup", "old_output"])
def test_unproven_outcome_remains_unresolved_without_record_or_reapply(tmp_path, monkeypatch, mutation):
    path, store, folder = fixture(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(store, "finish", lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            result = save(path, store)
    prepared = json.loads((folder / "prepared.json").read_text())
    backup = Path(prepared["backup"]["path"])
    if mutation == "output":
        path.write_bytes(b"different later file")
    elif mutation == "old_output":
        path.write_bytes(SOURCE)
    elif mutation == "missing_backup":
        backup.unlink()
    else:
        backup.write_bytes(b"wrong backup")
    before = path.read_bytes()
    result = journal.recover_operation(str(path), store.operation_id)
    assert result["publication_status"] == "uncertain"
    assert result["recovery_status"] == "unresolved" and not result["recovered"]
    assert not (folder / "result.json").exists() and path.read_bytes() == before


def test_existing_identifier_never_reapplies_even_after_external_revert(tmp_path):
    path, store, _ = fixture(tmp_path)
    save(path, store)
    path.write_bytes(SOURCE)
    another = journal.OperationJournal(store.operation_id, source_path=str(path), plan=store.plan,
                                      author_profile=store.profile, producer=store.producer)
    with pytest.raises(ApplyError, match="operation_recovery_required"):
        save(path, another)
    assert path.read_bytes() == SOURCE


def test_prepublication_failure_records_not_published_and_preserves_source(tmp_path, monkeypatch):
    path, store, folder = fixture(tmp_path)
    monkeypatch.setattr(io.os, "replace", lambda *a, **kw: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(ApplyError) as failure:
        save(path, store)
    assert failure.value.metadata["publication_completed"] is False
    assert failure.value.metadata["operation_journal"]["publication_status"] == "not_published"
    assert path.read_bytes() == SOURCE
    assert json.loads((folder / "result.json").read_text())["result"]["publication_status"] == "not_published"


@pytest.mark.parametrize("copy", [False, True], ids=["replace", "copy-link"])
@pytest.mark.parametrize("effect", ["candidate", "none", "noop"])
def test_eio_publication_stays_unknown_until_observed_recovery(tmp_path, monkeypatch, copy, effect):
    path, store, folder = fixture(tmp_path)
    output = tmp_path / "copy.docx" if copy else path
    candidate = SOURCE if effect == "noop" else CANDIDATE
    original = io.os.link if copy else io.os.replace

    def ambiguous(*args, **kwargs):
        if args[1] != output.name:
            return original(*args, **kwargs)
        if effect != "none":
            original(*args, **kwargs)
        raise OSError(errno.EIO, "fictional unsuccessful publication return")

    with monkeypatch.context() as patch:
        patch.setattr(io.os, "link" if copy else "replace", ambiguous)
        with pytest.raises(ApplyError, match="publication_uncertain") as failed:
            io.publish_document(str(path), str(output) if copy else None,
                                SOURCE, candidate, journal=store)
    metadata = failed.value.metadata
    assert metadata["publication_completed"] is False
    assert metadata["publication_status"] == "uncertain"
    status = metadata["operation_journal"]
    assert status["publication_status"] == "uncertain" and status["journal_status"] == "incomplete"
    expected_hash = hashlib.sha256(output.read_bytes()).hexdigest() if output.exists() else None
    assert status["observed_output_sha256"] == expected_hash
    assert (folder / "prepared.json").exists() and not (folder / "result.json").exists()
    before = output.read_bytes() if output.exists() else None
    recovered = journal.recover_operation(str(path), store.operation_id)
    matched = effect == "candidate"
    assert recovered["recovered"] is matched
    assert recovered["recovery_status"] == ("recovered" if matched else "unresolved")
    assert (folder / "result.json").exists() is matched
    if matched:
        assert journal.recover_operation(str(path), store.operation_id)["recovery_status"] == "existing_result"
    assert (output.read_bytes() if output.exists() else None) == before
    if not copy:
        assert Path(metadata["backup_path"]).read_bytes() == SOURCE


@pytest.mark.parametrize("mutation", ["shared_write", "symlink", "missing", "foreign_owner", "nested_mount"])
def test_recovery_never_certifies_an_unsafe_backup_directory(tmp_path, monkeypatch, mutation):
    from types import SimpleNamespace
    path, store, folder = fixture(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(store, "finish", lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            save(path, store)
    backups = tmp_path / ".veqtor-backups"
    backup_identity = io._identity(backups.stat())
    if mutation == "shared_write":
        backups.chmod(0o777)
    elif mutation in {"symlink", "missing"}:
        target = tmp_path / "retained-backups"
        backups.rename(target)
        if mutation == "symlink":
            backups.symlink_to(target, target_is_directory=True)
    elif mutation == "foreign_owner":
        original = io._at_stat

        def foreign(parent, name):
            value = original(parent, name)
            if name == io._BACKUP_DIRECTORY:
                return SimpleNamespace(st_uid=os.geteuid() + 1, st_mode=value.st_mode,
                                       st_dev=value.st_dev, st_ino=value.st_ino)
            return value

        monkeypatch.setattr(io, "_at_stat", foreign)
    else:
        original = io._supported_filesystem

        def mounted(fd, parent=None):
            if io._identity(os.fstat(fd)) == backup_identity:
                raise ApplyError("unsupported_filesystem", "fictional nested mount")
            return original(fd, parent)

        monkeypatch.setattr(io, "_supported_filesystem", mounted)
    before = path.read_bytes()
    if mutation == "symlink":
        with pytest.raises(ApplyError):
            journal.recover_operation(str(path), store.operation_id)
        assert path.read_bytes() == before and not (folder / "result.json").exists()
        assert backups.is_symlink()
        return
    recovered = journal.recover_operation(str(path), store.operation_id)
    assert recovered["recovery_status"] == "unresolved"
    assert recovered["publication_status"] == "uncertain"
    assert recovered["backup_verification"] == "not_checked"
    assert path.read_bytes() == before and not (folder / "result.json").exists()
    if mutation == "shared_write":
        assert backups.stat().st_mode & 0o777 == 0o777
    elif mutation == "missing":
        assert not backups.exists()


def test_recovery_refuses_native_backup_directory_acl_without_removing_it(tmp_path, monkeypatch):
    import subprocess
    import sys
    if sys.platform != "darwin":
        pytest.skip("native macOS extended ACL regression")
    path, store, folder = fixture(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(store, "finish", lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            save(path, store)
    backups = tmp_path / ".veqtor-backups"
    subprocess.run(["chmod", "+a", "user:nobody deny read", str(backups)],
                   check=True, capture_output=True)
    before = path.read_bytes()
    recovered = journal.recover_operation(str(path), store.operation_id)
    assert recovered["recovery_status"] == "unresolved"
    assert recovered["backup_verification"] == "not_checked"
    assert path.read_bytes() == before and not (folder / "result.json").exists()
    descriptor = os.open(backups, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert io._darwin_acl_present(descriptor)
    finally:
        os.close(descriptor)


def test_legacy_journal_bytes_untouched(tmp_path):
    path, store, _ = fixture(tmp_path)
    side = tmp_path / ".veqtor"
    side.mkdir(mode=0o700)
    legacy = side / "decision-records.jsonl"
    legacy.write_bytes(b"retained fictional legacy bytes\n")
    legacy.chmod(0o600)
    save(path, store)
    assert legacy.read_bytes() == b"retained fictional legacy bytes\n"


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_journal_alias_is_refused_before_publication(tmp_path, alias):
    path, store, folder = fixture(tmp_path)
    other = tmp_path / "unrelated"
    other.mkdir(mode=0o700)
    if alias == "symlink":
        (tmp_path / ".veqtor").symlink_to(other, target_is_directory=True)
    else:
        folder.mkdir(mode=0o700, parents=True)
        # Parents created above must match private storage requirements.
        for p in [folder.parent.parent, folder.parent]:
            p.chmod(0o700)
        original = other / "record"
        original.write_text("{}")
        original.chmod(0o600)
        os.link(original, folder / "prepared.json")
    with pytest.raises(ApplyError):
        save(path, store)
    assert path.read_bytes() == SOURCE


def test_source_change_during_prepared_record_never_gets_overwritten(tmp_path, monkeypatch):
    path, store, folder = fixture(tmp_path)
    original = store.prepare

    def changed(context):
        original(context)
        path.write_bytes(b"concurrent writer bytes")

    monkeypatch.setattr(store, "prepare", changed)
    with pytest.raises(ApplyError, match="source_changed"):
        save(path, store)
    assert path.read_bytes() == b"concurrent writer bytes"
    assert json.loads((folder / "result.json").read_text())["result"]["publication_status"] == "not_published"


def test_backup_change_during_prepared_record_blocks_publication(tmp_path, monkeypatch):
    path, store, _ = fixture(tmp_path)
    original = store.prepare

    def changed(context):
        original(context)
        Path(context["backup_path"]).unlink()

    monkeypatch.setattr(store, "prepare", changed)
    with pytest.raises(ApplyError, match="backup_failed"):
        save(path, store)
    assert path.read_bytes() == SOURCE


def test_observation_failure_is_explicit_known_publication_not_false_success(tmp_path, monkeypatch):
    path, store, folder = fixture(tmp_path)
    original = journal.observed_hash

    def unreadable_output(name):
        if name == str(path) and path.read_bytes() == CANDIDATE:
            raise OSError("unreadable after save")
        return original(name)

    monkeypatch.setattr(journal, "observed_hash", unreadable_output)
    with pytest.raises(ApplyError, match="publication_uncertain") as failed:
        save(path, store)
    assert failed.value.metadata["publication_completed"]
    status = failed.value.metadata["operation_journal"]
    assert status["publication_status"] == "published" and status["verification"] == "not_checked"
    assert path.read_bytes() == CANDIDATE and (folder / "result.json").exists()


@pytest.mark.parametrize("phase", ["prepared", "published"])
def test_actual_process_death_recovery_never_reapplies(tmp_path, phase):
    import subprocess
    import sys
    path, store, folder = fixture(tmp_path)
    code = r"""
import json,sys
from veqtor_docx import operation_journal as journal, _drafting_io as io
path,identifier,phase=sys.argv[1:]
s=journal.OperationJournal(identifier,source_path=path,plan={'operations':[{'kind':'synthetic'}]},
 author_profile={'name':'Fictional Counsel','initials':'FC'},producer='test-build')
original=s.prepare if phase=='prepared'else s.finish
def pause(*a,**kw):
 if phase=='prepared':original(*a,**kw)
 print('READY',flush=True)
 sys.stdin.read()
s.prepare=pause if phase=='prepared'else s.prepare
s.finish=pause if phase=='published'else s.finish
io.publish_document(path,None,b'old exact package bytes',b'new exact package bytes',journal=s)
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(path), store.operation_id, phase],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    try:
        assert child.stdout.readline().strip() == "READY"
        child.kill()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    assert (folder / "prepared.json").exists()
    before = path.read_bytes()
    result = journal.recover_operation(str(path), store.operation_id)
    assert path.read_bytes() == before
    assert result["recovered"] == (phase == "published")
    assert result["recovery_status"] == ("recovered" if phase == "published" else "unresolved")


@pytest.mark.parametrize("source_changed", [False, True])
def test_lost_copy_result_requires_source_and_output_match(tmp_path, monkeypatch, source_changed):
    path, store, folder = fixture(tmp_path)
    output = tmp_path / "copy.docx"
    with monkeypatch.context() as patch:
        patch.setattr(store, "finish", lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            save(path, store, output)
    if source_changed:
        path.write_bytes(b"later source")
    before = output.read_bytes()
    result = journal.recover_operation(str(path), store.operation_id)
    assert result["recovered"] == (not source_changed)
    assert output.read_bytes() == before
    assert (folder / "result.json").exists() == (not source_changed)


def _copy_crashed_after_link(tmp_path, separate_directory=True):
    import subprocess
    import sys

    path, store, folder = fixture(tmp_path)
    destination = tmp_path / "destination" if separate_directory else tmp_path
    destination.mkdir(exist_ok=True)
    output = destination / "copy.docx"
    code = r"""
import sys
from veqtor_docx import operation_journal as journal, _drafting_io as io
source, output, identifier = sys.argv[1:]
store = journal.OperationJournal(identifier, source_path=source,
    plan={'operations':[{'kind':'synthetic'}]},
    author_profile={'name':'Fictional Counsel','initials':'FC'}, producer='test-build')
original = io.os.link
def pause(*args, **kwargs):
    original(*args, **kwargs)
    if args[1] == 'copy.docx':
        print('PUBLISHED', flush=True)
        sys.stdin.read()
io.os.link = pause
io.publish_document(source, output, b'old exact package bytes', b'new exact package bytes', journal=store)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(path), str(output), store.operation_id],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    try:
        assert child.stdout.readline().strip() == "PUBLISHED"
        child.kill()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    assert output.stat().st_nlink == 2
    assert list(destination.glob(".veqtor-stage-*"))
    return path, store, folder, output


@pytest.mark.parametrize("separate_directory", [False, True])
@pytest.mark.parametrize("source_changed", [False, True])
def test_copy_link_crash_recovery_cleans_destination_stage_without_reapplying(
    tmp_path, separate_directory, source_changed
):
    path, store, folder, output = _copy_crashed_after_link(tmp_path, separate_directory)
    if source_changed:
        path.write_bytes(b"later source")
    source_before, output_before = path.read_bytes(), output.read_bytes()
    recovered = journal.recover_operation(str(path), store.operation_id)
    assert recovered["recovered"] is (not source_changed)
    assert recovered["current_output_sha256"] == hashlib.sha256(CANDIDATE).hexdigest()
    assert output.stat().st_nlink == 1
    assert not list(output.parent.glob(".veqtor-stage-*"))
    assert path.read_bytes() == source_before and output.read_bytes() == output_before
    assert (folder / "result.json").exists() is (not source_changed)
    repeat = journal.recover_operation(str(path), store.operation_id)
    assert repeat["recovery_status"] == ("unresolved" if source_changed else "existing_result")
    assert path.read_bytes() == source_before and output.read_bytes() == output_before


@pytest.mark.parametrize("mutation", ["extra_link", "changed_candidate", "symlink_parent", "missing_parent"])
def test_cross_directory_recovery_preserves_unverified_destination_evidence(tmp_path, mutation):
    path, store, folder, output = _copy_crashed_after_link(tmp_path)
    retained = output
    if mutation == "extra_link":
        alias = tmp_path / "external-alias.docx"
        os.link(output, alias)
    elif mutation == "changed_candidate":
        output.write_bytes(b"changed candidate")
    else:
        original = output.parent
        moved = tmp_path / "retained-destination"
        original.rename(moved)
        retained = moved / output.name
        if mutation == "symlink_parent":
            original.symlink_to(moved, target_is_directory=True)
    before = retained.read_bytes()
    stages = sorted(retained.parent.glob(".veqtor-stage-*"))
    if mutation == "symlink_parent":
        with pytest.raises(ApplyError, match="operation_recovery_failed") as refusal:
            journal.recover_operation(str(path), store.operation_id)
        assert isinstance(refusal.value.__cause__, ApplyError)
        assert refusal.value.__cause__.code == "operation_record_invalid"
    else:
        recovered = journal.recover_operation(str(path), store.operation_id)
        assert recovered["recovery_status"] == "unresolved"
        assert recovered["current_output_sha256"] is None
    assert not (folder / "result.json").exists()
    assert retained.read_bytes() == before and path.read_bytes() == SOURCE
    assert sorted(retained.parent.glob(".veqtor-stage-*")) == stages
    if mutation == "extra_link":
        assert alias.read_bytes() == before and retained.stat().st_nlink == 3


def test_cross_directory_recovery_respects_active_destination_writer(tmp_path):
    import fcntl

    path, store, folder, output = _copy_crashed_after_link(tmp_path)
    fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ApplyError, match="operation_recovery_failed") as refusal:
            journal.recover_operation(str(path), store.operation_id)
        assert isinstance(refusal.value.__cause__, ApplyError)
        assert refusal.value.__cause__.code == "file_locked"
        assert output.stat().st_nlink == 2
        assert list(output.parent.glob(".veqtor-stage-*"))
        assert not (folder / "result.json").exists()
    finally:
        os.close(fd)
    assert journal.recover_operation(str(path), store.operation_id)["recovered"]
    assert output.stat().st_nlink == 1 and output.read_bytes() == CANDIDATE
    assert path.read_bytes() == SOURCE


def test_noop_lost_result_cannot_infer_publication(tmp_path, monkeypatch):
    path, store, folder = fixture(tmp_path)
    monkeypatch.setattr(store, "finish", lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        io.publish_document(str(path), None, SOURCE, SOURCE, journal=store)
    result = journal.recover_operation(str(path), store.operation_id)
    assert result["recovery_status"] == "unresolved"
    assert not (folder / "result.json").exists() and path.read_bytes() == SOURCE


@pytest.mark.parametrize("mutation", ["unknown_schema", "duplicate_key", "plan_hash", "bad_time"])
def test_invalid_prepared_record_refuses_without_document_change(tmp_path, monkeypatch, mutation):
    path, store, folder = fixture(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(store, "finish", lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            save(path, store)
    prepared = folder / "prepared.json"
    value = json.loads(prepared.read_text())
    if mutation == "unknown_schema":
        value["schema_version"] = "drafting_operation_prepared.v999"
    elif mutation == "plan_hash":
        value["intent"]["plan"]["operations"][0]["old_author"] = "tampered"
    elif mutation == "bad_time":
        value["created_at"] = "not-a-time"
    if mutation == "duplicate_key":
        prepared.write_text('{"schema_version":"duplicate",' + prepared.read_text()[1:])
    else:
        prepared.write_text(json.dumps(value))
    before = path.read_bytes()
    with pytest.raises(ApplyError):
        journal.recover_operation(str(path), store.operation_id)
    assert path.read_bytes() == before and not (folder / "result.json").exists()


def test_late_result_directory_durability_failure_does_not_overwrite_existing_result(tmp_path, monkeypatch):
    path, store, folder = fixture(tmp_path)
    original = journal._write

    def after_write(fd, name, data):
        original(fd, name, data)
        if name == "result.json":
            raise OSError("late directory durability failure")

    with monkeypatch.context() as patch:
        patch.setattr(journal, "_write", after_write)
        result = save(path, store)
    assert result["operation_journal"]["journal_status"] == "incomplete"
    record = (folder / "result.json").read_bytes()
    recovered = journal.recover_operation(str(path), store.operation_id)
    assert recovered["recovery_status"] == "existing_result"
    assert not recovered["recovered"] and recovered["journal_status"] == "written"
    assert (folder / "result.json").read_bytes() == record


def test_record_limits_block_before_publication(tmp_path, monkeypatch):
    path, store, folder = fixture(tmp_path)
    monkeypatch.setattr(journal, "MAX_RECORD_BYTES", 32)
    with pytest.raises(ApplyError):
        save(path, store)
    assert path.read_bytes() == SOURCE and not (folder / "result.json").exists()
