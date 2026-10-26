# SPDX-License-Identifier: Apache-2.0
"""Fictional public preparation, publication, recovery and provenance boundaries."""
import hashlib
import json
import subprocess
import sys
import zipfile

import pytest

from veqtor_docx import inspect_document, outgoing_package as engine, round_snapshots as snapshots
from veqtor_mcp import deal_state as d, positions as p, server
from test_outgoing_package import fixture_source

RID = 'round_' + '4' * 32


def change(folder, state, n, action, **values):
    return server.mutate_deal_state(str(folder), state['revision'] if state else None,
        f'{n:032x}', dict(action=action, round_id=RID, **values))


@pytest.fixture
def case(tmp_path, demo_dir):
    source, snap = fixture_source(tmp_path, demo_dir)
    work = tmp_path / 'working.docx'
    work.write_bytes(snap.payload)
    state = change(tmp_path, None, 1, 'start_round', previous_round_id=None,
                   path=str(source), file_sha256=snap.sha256)
    state = change(tmp_path, state, 2, 'import_working_document', path='working.docx',
                   file_sha256=snap.sha256, incoming_basis_sha256=snap.sha256,
                   user_confirmed=True, statement='Fictional separate working version.')
    return tmp_path, source, work, state


def prepare(case, n=3, **overrides):
    folder, _, _, state = case
    return change(folder, state, n, 'prepare_outgoing_package', **{
        'expected_working_request_id': state['working_documents'][-1]['request_id'],
        'file_sha256': state['working_documents'][-1]['file_sha256'], 'path': 'outgoing',
        'redline_name': 'Redline.docx', 'clean_draft_name': 'Clean.docx',
        'comments_policy': 'preserve_all', 'properties_policy': 'preserve_all', **overrides})


def publish(folder, state, n=4, **overrides):
    row = state['outgoing_packages'][0]
    return change(folder, state, n, 'publish_outgoing_package', **{
        'package_request_id': row['request_id'], 'plan_sha256': row['plan_sha256'],
        'user_confirmed': True, 'statement': 'Publish the reviewed fictional kit.', **overrides})


def verify(folder, state, n=5):
    row = state['outgoing_packages'][0]
    return change(folder, state, n, 'record_outgoing_verification',
        package_request_id=row['request_id'], plan_sha256=row['plan_sha256'])


def raw(folder):
    return (folder / '.veqtor' / d.STORE_NAME).read_bytes()


def test_saved_preview_then_publication_and_cold_resume_keep_roles_separate(case):
    folder, source, work, old = case
    initial = source.read_bytes()
    before = raw(folder)
    server.read_deal_state(str(folder))
    assert raw(folder) == before and json.loads(before)['schema_version'] == 'deal_rounds_store.v3'
    state = prepare(case)
    row = state['outgoing_packages'][0]
    assert row['readiness'] == 'prepared_unreviewed'
    assert hashlib.sha256(row['report_utf8'].encode()).hexdigest() == row['plan_sha256']
    assert row == json.loads(raw(folder))['outgoing_packages'][0]
    assert not (folder / 'outgoing').exists()
    assert not state['outgoing_events']
    state = publish(folder, state)
    assert state['outgoing_events'][0]['status'] == 'published'
    assert sorted(p.name for p in (folder / 'outgoing').iterdir()) == ['Clean.docx', 'Redline.docx']
    assert (folder / 'outgoing' / 'Redline.docx').read_bytes() == initial == source.read_bytes() == work.read_bytes()
    for f in row['files']:
        assert hashlib.sha256((folder / 'outgoing' / f['name']).read_bytes()).hexdigest() == f['sha256']
    state = verify(folder, state)
    assert state['outgoing_events'][-1]['status'] == 'observed_match'
    assert state['rounds'] == old['rounds'] and not state['operations'] and not state['issues']
    script = 'import json,sys;from veqtor_mcp.server import read_deal_state;print(json.dumps(read_deal_state(sys.argv[1])))'
    cold = json.loads(subprocess.check_output([sys.executable, '-c', script, str(folder)], text=True))
    assert cold['outgoing_packages'] == state['outgoing_packages']
    assert cold['outgoing_events'] == state['outgoing_events']
    assert not cold['rounds'][0]['sent_documents']


@pytest.mark.parametrize('override', [{'user_confirmed': False}, {'plan_sha256': 'f' * 64}, {'package_request_id': 'f' * 32}])
def test_bad_confirmation_or_plan_has_no_output_or_state_effect(case, override):
    folder, _, _, _ = case
    state = prepare(case)
    before = raw(folder)
    with pytest.raises(p.PositionError):
        publish(folder, state, **override)
    assert raw(folder) == before and not (folder / 'outgoing').exists()


def test_unregistered_working_change_refuses_and_keeps_report(case):
    folder, _, work, _ = case
    state = prepare(case)
    before = raw(folder)
    work.write_bytes(b'changed in another application')
    with pytest.raises(p.PositionError, match='working_basis_mismatch'):
        publish(folder, state)
    assert raw(folder) == before and not (folder / 'outgoing').exists()


def test_changed_issue_invalidates_plan_and_preserves_v4(case):
    folder, source, _, _ = case
    state = prepare(case)
    para = inspect_document(str(source), 'browse', max_items=1)['paragraphs'][0]
    quote = dict(file_sha256=para['paragraph_ref']['file_sha256'], paragraph_ref=para['paragraph_ref'], start=0, text=para['text'])
    state = change(folder, state, 5, 'save_issue', issue_id='issue_' + '1' * 32, expected_version=None,
        content=dict(title='Check delivery', sent_quote=None, incoming_quote=quote, working_quote=None,
                     position=None, model_assessment=None, proposed_wording=None,
                     remaining_question='Confirm delivery.', operation_ids=[]))
    assert json.loads(raw(folder))['schema_version'] == 'deal_rounds_store.v4'
    with pytest.raises(p.PositionError, match='outgoing_basis_changed'):
        publish(folder, state, n=6)
    assert not (folder / 'outgoing').exists()


def test_published_bytes_survive_failed_state_commit_and_recover_without_new_publication(case, monkeypatch):
    folder, _, _, _ = case
    state = prepare(case)
    before = raw(folder)
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError('injected state failure after publication')
        patch.setattr(p, '_publish', fail)
        with pytest.raises(snapshots.SnapshotError, match='outgoing_state_commit_uncertain') as caught:
            publish(folder, state)
    assert caught.value.metadata['publication_completed'] is True
    assert raw(folder) == before
    files = {f.name: f.read_bytes() for f in (folder / 'outgoing').iterdir()}
    with pytest.raises(snapshots.SnapshotError, match='output_exists'):
        publish(folder, state)
    recovered = verify(folder, state)
    assert [e['status'] for e in recovered['outgoing_events']] == ['observed_match']
    assert {f.name: f.read_bytes() for f in (folder / 'outgoing').iterdir()} == files
    assert not recovered['rounds'][0]['sent_documents']


def test_lost_reply_replays_historical_receipt_without_republishing(case):
    folder, _, _, _ = case
    prepared = prepare(case)
    published = publish(folder, prepared)
    (folder / 'outgoing').rename(folder / 'moved')
    before = raw(folder)
    replay = publish(folder, prepared)
    assert replay['revision'] == published['revision'] and raw(folder) == before
    assert not (folder / 'outgoing').exists()
    assert 'outgoing_receipts_are_historical_observations_not_current_file_checks' in replay['limitations']
    with pytest.raises(snapshots.SnapshotError):
        verify(folder, replay)
    assert raw(folder) == before


def test_transport_rejection_occurs_before_publication(case):
    folder, _, _, _ = case
    state = prepare(case)
    row = state['outgoing_packages'][0]
    before = raw(folder)
    def reject(result):
        raise ValueError('injected transport contract failure')
    with pytest.raises(ValueError, match='transport'):
        d.mutate_deal_state(str(folder), state['revision'], '4' * 32,
            dict(action='publish_outgoing_package', round_id=RID, package_request_id=row['request_id'],
                 plan_sha256=row['plan_sha256'], user_confirmed=True, statement='Fictional approval.'),
            validate_result=reject)
    assert raw(folder) == before and not (folder / 'outgoing').exists()


def test_foreign_member_refuses_recovery_without_cleanup(case):
    folder, _, _, _ = case
    state = publish(folder, prepare(case))
    (folder / 'outgoing' / 'foreign.txt').write_text('keep')
    before = raw(folder)
    with pytest.raises(snapshots.SnapshotError, match='outgoing_mismatch'):
        verify(folder, state)
    assert raw(folder) == before and (folder / 'outgoing' / 'foreign.txt').read_text() == 'keep'


@pytest.mark.parametrize('path', ['../out', '.VEQTOR/out', '.veqtor-backups/out', '/tmp/out', 'x/../out'])
def test_invalid_or_private_output_paths_refuse_before_state_commit(case, path):
    folder, _, _, _ = case
    before = raw(folder)
    with pytest.raises(p.PositionError):
        prepare(case, path=path)
    assert raw(folder) == before


def test_report_tampering_cannot_be_loaded_or_published(case):
    folder, _, _, _ = case
    prepare(case)
    store = json.loads(raw(folder))
    store['outgoing_packages'][0]['report_utf8'] = '{}'
    store['revision'] = p._revision(store)
    (folder / '.veqtor' / d.STORE_NAME).write_bytes(p._json_bytes(store))
    with pytest.raises(p.PositionError, match='storage_corrupt'):
        server.read_deal_state(str(folder))
    assert not (folder / 'outgoing').exists()


def test_unsupported_clean_request_refuses_whole_plan(case, monkeypatch):
    folder, _, _, _ = case
    before = raw(folder)
    def refuse(*args, **kwargs):
        raise snapshots.DocxError('unsupported selected clean fixture')
    monkeypatch.setattr(engine, 'prepare_clean_copy', refuse)
    with pytest.raises(snapshots.DocxError):
        prepare(case)
    assert raw(folder) == before and not (folder / 'outgoing').exists()


def test_prepared_a_then_explicitly_sent_external_b_is_next_round_basis(case):
    folder, source, _, _ = case
    state = publish(folder, prepare(case))
    prepared_sha = state['outgoing_packages'][0]['files'][0]['sha256']
    sent = folder / 'actually-sent.docx'
    sent.write_bytes((folder / 'outgoing' / 'Redline.docx').read_bytes())
    with zipfile.ZipFile(sent, 'a') as archive:
        archive.comment = b'Fictional external save; no native Word claim.'
    sent_sha = hashlib.sha256(sent.read_bytes()).hexdigest()
    assert sent_sha != prepared_sha
    state = change(folder, state, 6, 'record_sent', path=str(sent), file_sha256=sent_sha,
                   user_confirmed=True, statement='This exact fictional B was sent.')
    state = server.mutate_deal_state(str(folder), state['revision'], '7' * 32,
        dict(action='start_round', round_id='round_' + '5' * 32, previous_round_id=RID,
             path=str(source), file_sha256=hashlib.sha256(source.read_bytes()).hexdigest()))
    assert state['rounds'][-1]['sent_basis']['file_sha256'] == sent_sha
    assert state['outgoing_packages'][0]['files'][0]['sha256'] == prepared_sha
    assert json.loads(raw(folder))['schema_version'] == 'deal_rounds_store.v4'


def test_changed_positions_invalidates_prepared_plan(case):
    folder, _, _, _ = case
    state = prepare(case)
    server.mutate_deal_positions(str(folder), None, [dict(op='create', position_id='pos_' + '1' * 32,
        content=dict(title='Delivery', desired_outcome='Agree a delivery date.', fallback=None,
                     fallback_conditions=None, rationale=None, related_position_ids=[],
                     content_origin='model_proposal', business_decision='not_required', sources=[]))])
    before = raw(folder)
    with pytest.raises(p.PositionError, match='outgoing_basis_changed'):
        publish(folder, state)
    assert raw(folder) == before and not (folder / 'outgoing').exists()


def test_lost_receipt_after_state_commit_replays_without_second_publication(case, monkeypatch):
    folder, _, _, _ = case
    prepared = prepare(case)
    original = p._publish
    with monkeypatch.context() as patch:
        def lost_receipt(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('injected lost state receipt')
        patch.setattr(p, '_publish', lost_receipt)
        with pytest.raises(snapshots.SnapshotError, match='outgoing_state_commit_uncertain'):
            publish(folder, prepared)
    state = server.read_deal_state(str(folder))
    assert len(state['outgoing_events']) == 1
    before = raw(folder)
    assert publish(folder, prepared)['revision'] == state['revision']
    assert raw(folder) == before
