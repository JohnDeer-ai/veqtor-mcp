# SPDX-License-Identifier: Apache-2.0
"""Fictional complete-kit and publication faults, never real contract bytes."""
import dataclasses
import hashlib
import io
import json
import os
import zipfile

from lxml import etree
import pytest

from veqtor_docx import outgoing_package as o, round_snapshots as s
from veqtor_docx._ooxml import w
from veqtor_docx.apply import ApplyError


def fixture_source(tmp_path, demo_dir, *, extra=None):
    with zipfile.ZipFile(demo_dir / 'round-1-outgoing-draft.docx') as z:
        parts = {n: z.read(n) for n in z.namelist()}
    root = etree.fromstring(parts['word/document.xml'])
    body = root.find(w('body'))
    for child in list(body):
        body.remove(child)
    p = etree.SubElement(body, w('p'))
    etree.SubElement(etree.SubElement(p, w('r')), w('t')).text = 'Delivery: '
    change = etree.SubElement(p, w('ins'), {w('id'): '7', w('author'): 'Fictional Counsel'})
    etree.SubElement(etree.SubElement(change, w('r')), w('t')).text = 'ten days.'
    parts['word/document.xml'] = etree.tostring(root)
    parts.update(extra or {})
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, content in parts.items():
            z.writestr(name, content)
    source = tmp_path / 'source.docx'
    source.write_bytes(data.getvalue())
    return source, s.capture_document_snapshot(source)


def publish(plan, path):
    return o.publish_outgoing_package(plan, path, expected_plan_sha256=plan.plan_sha256)


def test_two_files_exact_redline_and_literal_clean_without_internal_files(tmp_path, demo_dir):
    source, snap = fixture_source(tmp_path, demo_dir)
    output = tmp_path / 'outgoing'
    plan = o.prepare_outgoing_package(snap, output, redline_name='Agreement-redline.docx', clean_draft_name='Agreement-clean.docx')
    report = json.loads(plan.report)
    assert not output.exists()
    assert report['source_sha256'] == snap.sha256
    assert report['clean_draft']['accepted_revision_count'] == 1
    assert report['readiness'] == 'prepared_unreviewed'
    assert report['comments_policy'] == report['properties_policy'] == 'preserve_all'
    result = publish(plan, output)
    assert result['status'] == 'published' and result['actual_send'] == 'not_asserted'
    assert sorted(p.name for p in output.iterdir()) == ['Agreement-clean.docx', 'Agreement-redline.docx']
    assert (output / 'Agreement-redline.docx').read_bytes() == snap.payload
    with zipfile.ZipFile(output / 'Agreement-clean.docx') as z:
        body = etree.fromstring(z.read('word/document.xml'))
        assert not list(body.iter(w('ins')))
        assert ''.join(body.itertext()) == 'Delivery: ten days.'
        with zipfile.ZipFile(io.BytesIO(snap.payload)) as original:
            assert z.namelist() == original.namelist()
            assert all(z.read(n) == original.read(n) for n in z.namelist() if n != 'word/document.xml')
    assert source.read_bytes() == snap.payload
    assert o.verify_outgoing_package(plan, output, expected_plan_sha256=plan.plan_sha256)['status'] == 'matched'
    with pytest.raises(s.SnapshotError, match='output_exists'):
        publish(plan, output)


def test_unknown_storage_preserved_in_redline_but_clean_refuses_entire_kit(tmp_path, demo_dir):
    source, snap = fixture_source(tmp_path, demo_dir, extra={'customXml/item999.xml': '<unknown xmlns="urn:fictional:opaque">Keep</unknown>'})
    output = tmp_path / 'outgoing'
    plan = o.prepare_outgoing_package(snap, output, redline_name='Agreement.docx')
    assert json.loads(plan.report)['inventory_before']['blockers']
    publish(plan, output)
    assert (output / 'Agreement.docx').read_bytes() == snap.payload
    selected = tmp_path / 'two-files'
    with pytest.raises(ApplyError, match='clean_copy_unsupported'):
        o.prepare_outgoing_package(snap, selected, redline_name='Redline.docx', clean_draft_name='Clean.docx')
    assert not selected.exists() and source.read_bytes() == snap.payload


@pytest.mark.parametrize('name', ['../x.docx', 'x/y.docx', '.hidden.docx', 'CON.docx', 'A.DOCX', 'X.docx.exe', 'x\x00.docx', 'x .docx', 'x\\y.docx'])
def test_invalid_names_refused_before_destination_exists(tmp_path, demo_dir, name):
    _, snap = fixture_source(tmp_path, demo_dir)
    with pytest.raises(s.SnapshotError):
        o.prepare_outgoing_package(snap, tmp_path / 'out', redline_name=name)
    assert not (tmp_path / 'out').exists()


def test_plan_binds_source_files_names_policy_and_destination(tmp_path, demo_dir):
    _, snap = fixture_source(tmp_path, demo_dir)
    output = tmp_path / 'out'
    with pytest.raises(s.SnapshotError):
        o.prepare_outgoing_package(snap, output, redline_name='File.docx', clean_draft_name='file.docx')
    plan = o.prepare_outgoing_package(snap, output, redline_name='File.docx')
    for bad in [dataclasses.replace(plan, report=b'{}'),
                dataclasses.replace(plan, files=(dataclasses.replace(plan.files[0], payload=b'forged'),))]:
        with pytest.raises(s.SnapshotError, match='outgoing_plan_mismatch'):
            publish(bad, output)
    with pytest.raises(s.SnapshotError, match='outgoing_plan_mismatch'):
        publish(plan, tmp_path / 'different')
    with pytest.raises(s.SnapshotError, match='outgoing_plan_mismatch'):
        o.publish_outgoing_package(plan, output, expected_plan_sha256='0' * 64)
    assert not output.exists()


@pytest.mark.parametrize('failure', ['second_file', 'before_rename', 'after_rename', 'foreign_stage'])
def test_atomic_kit_and_uncertain_outcomes_preserve_foreign_data(tmp_path, demo_dir, monkeypatch, failure):
    _, snap = fixture_source(tmp_path, demo_dir)
    output = tmp_path / 'out'
    plan = o.prepare_outgoing_package(snap, output, redline_name='Redline.docx', clean_draft_name='Clean.docx')
    write = o._write_all
    count = 0

    def write_failure(fd, payload):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError('injected second output failure')
        write(fd, payload)

    rename = o._rename_noreplace

    def rename_failure(fd, stage, name):
        if failure == 'after_rename':
            rename(fd, stage, name)
        elif failure == 'foreign_stage':
            os.rename(stage, 'moved', src_dir_fd=fd, dst_dir_fd=fd)
            (tmp_path / stage).mkdir()
            (tmp_path / stage / 'foreign').write_text('keep')
        raise OSError('injected publication failure')

    monkeypatch.setattr(o, '_write_all' if failure == 'second_file' else '_rename_noreplace',
                        write_failure if failure == 'second_file' else rename_failure)
    with pytest.raises(s.SnapshotError) as caught:
        publish(plan, output)
    assert caught.value.metadata['publication_completed'] == (True if failure == 'after_rename' else None if failure == 'foreign_stage' else False)
    assert output.exists() == (failure == 'after_rename')
    if output.exists():
        assert o.verify_outgoing_package(plan, output, expected_plan_sha256=plan.plan_sha256)['status'] == 'matched'
    elif failure == 'foreign_stage':
        assert next(tmp_path.glob('.veqtor-outgoing-*')).joinpath('foreign').read_text() == 'keep'
        assert len(list((tmp_path / 'moved').iterdir())) == 2
    else:
        assert not list(tmp_path.glob('.veqtor-outgoing-*'))


def test_existing_modified_or_extra_files_are_never_certified_or_deleted(tmp_path, demo_dir):
    _, snap = fixture_source(tmp_path, demo_dir)
    output = tmp_path / 'out'
    plan = o.prepare_outgoing_package(snap, output, redline_name='Redline.docx')
    publish(plan, output)
    (output / 'foreign').write_text('keep')
    with pytest.raises(s.SnapshotError, match='outgoing_mismatch'):
        o.verify_outgoing_package(plan, output, expected_plan_sha256=plan.plan_sha256)
    assert (output / 'foreign').read_text() == 'keep'
    assert hashlib.sha256((output / 'Redline.docx').read_bytes()).hexdigest() == snap.sha256


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'changed'])
def test_existing_member_aliases_and_drift_refuse_verification(tmp_path, demo_dir, kind):
    source, snap = fixture_source(tmp_path, demo_dir)
    output = tmp_path / 'out'
    plan = o.prepare_outgoing_package(snap, output, redline_name='Redline.docx')
    publish(plan, output)
    target = output / 'Redline.docx'
    if kind == 'changed':
        target.write_bytes(b'changed after publication')
    else:
        target.unlink()
        if kind == 'symlink':
            target.symlink_to(source)
        else:
            os.link(source, target)
    with pytest.raises(s.SnapshotError):
        o.verify_outgoing_package(plan, output, expected_plan_sha256=plan.plan_sha256)
    assert source.read_bytes() == snap.payload


def test_destination_race_preserves_preexisting_foreign_directory(tmp_path, demo_dir, monkeypatch):
    _, snap = fixture_source(tmp_path, demo_dir)
    output = tmp_path / 'out'
    plan = o.prepare_outgoing_package(snap, output, redline_name='Redline.docx')
    rename = o._rename_noreplace

    def race(fd, stage, name):
        output.mkdir()
        (output / 'foreign').write_text('keep')
        rename(fd, stage, name)

    monkeypatch.setattr(o, '_rename_noreplace', race)
    with pytest.raises(s.SnapshotError, match='output_exists') as caught:
        publish(plan, output)
    assert caught.value.metadata['publication_completed'] is False
    assert sorted(p.name for p in output.iterdir()) == ['foreign']
    assert not list(tmp_path.glob('.veqtor-outgoing-*'))


def test_output_directory_substituted_during_final_verification_is_uncertain(tmp_path, demo_dir, monkeypatch):
    _, snap = fixture_source(tmp_path, demo_dir)
    output = tmp_path / 'out'
    moved = tmp_path / 'published-moved'
    plan = o.prepare_outgoing_package(snap, output, redline_name='Redline.docx')
    check = o._check_directory
    count = 0

    def race(fd, selected):
        nonlocal count
        count += 1
        check(fd, selected)
        if count == 2:
            output.rename(moved)
            output.mkdir(mode=0o700)
            (output / 'foreign').write_text('keep')

    monkeypatch.setattr(o, '_check_directory', race)
    with pytest.raises(s.SnapshotError, match='publication_uncertain') as caught:
        publish(plan, output)
    assert caught.value.metadata['publication_completed'] is True
    assert caught.value.metadata['output_path_verified'] is False
    assert (output / 'foreign').read_text() == 'keep'
    assert (moved / 'Redline.docx').read_bytes() == snap.payload
    with pytest.raises(s.SnapshotError, match='outgoing_mismatch'):
        o.verify_outgoing_package(plan, output, expected_plan_sha256=plan.plan_sha256)


def test_internal_report_budget_and_private_destination_refuse(tmp_path, demo_dir, monkeypatch):
    _, snap = fixture_source(tmp_path, demo_dir)
    for private in ('.veqtor', '.VEQTOR', '.Veqtor-Backups'):
        with pytest.raises(ApplyError, match='protected_storage_path'):
            o.prepare_outgoing_package(snap, tmp_path / private / 'kit', redline_name='Redline.docx')
    monkeypatch.setattr(o, 'MAX_REPORT_BYTES', 1)
    with pytest.raises(s.SnapshotError, match='resource_limit_exceeded'):
        o.prepare_outgoing_package(snap, tmp_path / 'out', redline_name='Redline.docx')
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('mutation', ['none', 'parent', 'ancestor', 'output', 'parent_permissions'])
def test_recovery_verifies_advertised_path_after_content_check(tmp_path, demo_dir, monkeypatch, mutation):
    _, snap = fixture_source(tmp_path, demo_dir)
    ancestor = tmp_path / 'target'
    parent = ancestor / 'nested'
    parent.mkdir(parents=True, mode=0o700)
    output = parent / 'out'
    moved = tmp_path / 'moved'
    plan = o.prepare_outgoing_package(snap, output, redline_name='Redline.docx')
    fsync = o.os.fsync
    check = o._check_directory
    failed = False
    checks = 0

    def fail_after_publication(fd):
        nonlocal failed
        if output.is_dir() and not failed:
            failed = True
            raise OSError('injected post-rename fsync failure')
        fsync(fd)

    def mutate_during_recovery(fd, selected):
        nonlocal checks
        checks += 1
        check(fd, selected)
        if checks != 2:
            return
        if mutation == 'parent_permissions':
            parent.chmod(0o777)
        elif mutation != 'none':
            selected_path = {'parent': parent, 'ancestor': ancestor, 'output': output}[mutation]
            selected_path.rename(moved)
            output.mkdir(parents=True, mode=0o700)
            (output / 'foreign').write_text('keep')

    monkeypatch.setattr(o.os, 'fsync', fail_after_publication)
    monkeypatch.setattr(o, '_check_directory', mutate_during_recovery)
    try:
        with pytest.raises(s.SnapshotError, match='publication_uncertain') as caught:
            publish(plan, output)
        assert failed and checks == 2
        assert caught.value.metadata['publication_completed'] is True
        assert caught.value.metadata['output_path_verified'] is (mutation == 'none')
        if mutation in {'parent', 'ancestor', 'output'}:
            assert sorted(p.name for p in output.iterdir()) == ['foreign']
            assert (output / 'foreign').read_text() == 'keep'
            retained = {'parent': moved / 'out', 'ancestor': moved / 'nested' / 'out', 'output': moved}[mutation]
        else:
            retained = output
        assert (retained / 'Redline.docx').read_bytes() == snap.payload
    finally:
        if mutation == 'parent_permissions':
            parent.chmod(0o700)
