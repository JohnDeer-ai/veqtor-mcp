# SPDX-License-Identifier: Apache-2.0
"""Fictional round continuation: exact work copies, original journals and issues."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import jsonschema
import pytest

from veqtor_docx import drafting, inspect_document, operation_journal as journal
from veqtor_docx._ooxml import DocxError
from veqtor_mcp import deal_state as d, positions as p, server
from veqtor_mcp import _deal_workflow as w
from veqtor_mcp._deal_state_contract import RESULT, STORE_V1

RID = 'round_' + '1' * 32
IID = 'issue_' + '1' * 32
PROFILE = dict(author='Fictional Counsel', initials='FC', producer_build='test-build')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mutate(folder, state, n, command):
    result = server.mutate_deal_state(str(folder), state['revision'] if state else None,
                                      f'{n:032x}', dict(round_id=RID, **command))
    jsonschema.validate(result, RESULT)
    return result


@pytest.fixture
def round_data(tmp_path, demo_dir):
    source = tmp_path / 'incoming.docx'
    source.write_bytes((demo_dir / 'round-1-outgoing-draft.docx').read_bytes())
    state = mutate(tmp_path, None, 1, dict(action='start_round', previous_round_id=None,
                                         path=str(source), file_sha256=sha(source)))
    return tmp_path, source, state


def edit(source, output=None, old='Supply', new='Services'):
    para = inspect_document(str(source), 'browse', max_items=1)['paragraphs'][0]
    args = dict(source_path=str(source), file_sha256=sha(source),
                operations=[dict(kind='replace_text', paragraph_ref=para['paragraph_ref'],
                                 old_text=old, new_text=new)])
    if output is not None:
        args['output_path'] = str(output)
    pre = drafting.preflight_document_edits(**args, **PROFILE, journal=True)
    assert pre['batch_applicable'], pre
    return drafting.apply_document_edits(**args, **PROFILE, journal=True,
                                         preflight_proof=pre['preflight_proof'])


def link_copy(folder, source, state, n=2):
    output = folder / 'working.docx'
    saved = edit(source, output)
    oid = saved['operation_journal']['operation_id']
    state = mutate(folder, state, n, dict(action='link_operation', source_path=str(source), operation_id=oid))
    state = mutate(folder, state, n + 1, dict(action='link_working_document', operation_id=oid))
    return state, output, oid


def quote(path):
    para = inspect_document(str(path), 'browse', max_items=1)['paragraphs'][0]
    return dict(file_sha256=sha(path), paragraph_ref=para['paragraph_ref'], start=0, text=para['text'])


def content(source, working=None, operation_id=None):
    return dict(title='Heading wording', sent_quote=None, incoming_quote=quote(source),
                working_quote=quote(working) if working else None, position=None,
                model_assessment='The proposed heading changes the scope label.',
                proposed_wording='Services agreement', remaining_question='Confirm the intended scope.',
                operation_ids=[operation_id] if operation_id else [])


def store_bytes(folder):
    return (folder / '.veqtor' / d.STORE_NAME).read_bytes()


def test_copy_links_survive_cold_process_and_do_not_modify_sources(round_data):
    folder, source, state = round_data
    original = source.read_bytes()
    state, work, oid = link_copy(folder, source, state)
    before = store_bytes(folder)
    script = 'import json,sys;from veqtor_mcp.server import read_deal_state;print(json.dumps(read_deal_state(sys.argv[1])))'
    loaded = json.loads(subprocess.check_output([sys.executable, '-c', script, str(folder)], text=True))
    assert loaded['working_documents'] == state['working_documents']
    assert loaded['operation_observations'][0]['status'] == 'completed'
    assert loaded['working_observations'][0]['status'] == 'same_bytes'
    assert loaded['rounds'][0]['sent_documents'] == []
    assert loaded['operations'][0]['operation_id'] == oid
    snap = next(r for r in state['documents'] if r['file_sha256'] == sha(work))
    assert Path(snap['path']).read_bytes() == work.read_bytes()
    assert source.read_bytes() == original and store_bytes(folder) == before


def test_existing_v1_is_not_rewritten_on_read_and_upgrades_only_on_link(round_data):
    folder, source, state = round_data
    raw = store_bytes(folder)
    jsonschema.validate(json.loads(raw), STORE_V1)
    server.read_deal_state(str(folder))
    assert store_bytes(folder) == raw
    state, _, _ = link_copy(folder, source, state)
    value = json.loads(store_bytes(folder))
    assert value['schema_version'] == 'deal_rounds_store.v2'
    assert value['rounds'] == json.loads(raw)['rounds']
    assert value['requests'][0] == json.loads(raw)['requests'][0]
    assert state['revision'] == value['revision']


def test_lost_link_reply_replays_without_output_or_journal(round_data):
    folder, source, state = round_data
    saved = edit(source, folder / 'working.docx')
    oid = saved['operation_journal']['operation_id']
    op = dict(action='link_operation', source_path=str(source), operation_id=oid)
    state = mutate(folder, state, 2, op)
    before_work = deepcopy(state)
    work = dict(action='link_working_document', operation_id=oid)
    state = mutate(folder, state, 3, work)
    before = store_bytes(folder)
    (folder / 'working.docx').unlink()
    source.rename(folder / 'moved.docx')
    for n, command in [(2, op), (3, work)]:
        replay = mutate(folder, before_work, n, command)
        assert replay['revision'] == state['revision']
        assert len(replay['working_documents']) == len(replay['operations']) == 1
    assert store_bytes(folder) == before
    assert replay['operation_observations'][0]['status'] == 'completed'  # Journal still exists.
    assert replay['working_observations'][0]['status'] == 'unavailable'


def test_lost_document_result_is_linked_unresolved_then_recovered_without_reapply(round_data, monkeypatch):
    folder, source, state = round_data
    original_write = journal._write

    def interrupted(directory, name, payload):
        if name == 'result.json':
            raise OSError('fictional lost result')
        return original_write(directory, name, payload)

    work = folder / 'working.docx'
    with monkeypatch.context() as patch:
        patch.setattr(journal, '_write', interrupted)
        saved = edit(source, work)
    oid = saved['operation_journal']['operation_id']
    state = mutate(folder, state, 2, dict(action='link_operation', source_path=str(source), operation_id=oid))
    assert state['operation_observations'][0]['status'] == 'prepared_only'
    with pytest.raises(p.PositionError, match='operation_unverified'):
        mutate(folder, state, 3, dict(action='link_working_document', operation_id=oid))
    raw = work.read_bytes()
    recovered = journal.recover_operation(str(source), oid)
    assert recovered['recovered']
    state = mutate(folder, state, 3, dict(action='link_working_document', operation_id=oid))
    assert state['operation_observations'][0]['status'] == 'completed'
    assert work.read_bytes() == raw


@pytest.mark.parametrize('fault', ['changed', 'missing', 'symlink', 'hardlink', 'journal_missing', 'journal_changed'])
def test_live_drift_is_visible_without_discarding_historical_links(round_data, fault):
    folder, source, state = round_data
    state, work, oid = link_copy(folder, source, state)
    before = store_bytes(folder)
    if fault == 'changed':
        work.write_bytes(source.read_bytes())
    if fault == 'missing':
        work.unlink()
    if fault == 'symlink':
        work.unlink()
        work.symlink_to(source)
    if fault == 'hardlink':
        os.link(work, folder / 'alias.docx')
    prepared = folder / '.veqtor' / 'operations' / oid / 'prepared.json'
    if fault == 'journal_missing':
        prepared.unlink()
    if fault == 'journal_changed':
        value = json.loads(prepared.read_text())
        value['intent']['producer'] = 'Different producer'
        prepared.write_text(json.dumps(value))
        result_path = prepared.with_name('result.json')
        completed = json.loads(result_path.read_text())
        completed['result']['intent_sha256'] = journal.records.operation_intent_digest(value['intent'])
        result_path.write_text(json.dumps(completed))
    loaded = server.read_deal_state(str(folder))
    assert loaded['working_documents'] == state['working_documents']
    assert store_bytes(folder) == before
    if fault.startswith('journal_'):
        assert loaded['operation_observations'][0]['status'] == ('intent_changed' if fault == 'journal_changed' else 'unavailable')
    else:
        assert loaded['working_observations'][0]['status'] == ('changed' if fault == 'changed' else 'unavailable')


def test_in_place_chain_needs_selected_working_basis(round_data):
    folder, source, state = round_data
    # An operation on the incoming original cannot turn that path into a working copy.
    saved = edit(source)
    with pytest.raises(p.PositionError, match='working_basis_mismatch'):
        mutate(folder, state, 2, dict(action='link_operation', source_path=str(source),
                                    operation_id=saved['operation_journal']['operation_id']))


def test_following_in_place_operation_keeps_prior_exact_snapshot(round_data):
    folder, source, state = round_data
    state, work, _ = link_copy(folder, source, state)
    first_hash = sha(work)
    saved = edit(work, old='Services', new='Distribution')
    oid = saved['operation_journal']['operation_id']
    state = mutate(folder, state, 4, dict(action='link_operation', source_path=str(work), operation_id=oid))
    state = mutate(folder, state, 5, dict(action='link_working_document', operation_id=oid))
    assert state['working_documents'][0]['file_sha256'] == first_hash != sha(work)
    assert [o['status'] for o in state['working_observations']] == ['changed', 'same_bytes']
    first = next(r for r in state['documents'] if r['file_sha256'] == first_hash)
    assert hashlib.sha256(Path(first['path']).read_bytes()).hexdigest() == first_hash


@pytest.mark.parametrize('fault', ['quote', 'offset', 'ref', 'source_role', 'operation', 'position'])
def test_unverified_issue_data_is_refused_and_preserved(round_data, fault):
    folder, source, state = round_data
    data = content(source)
    if fault == 'quote':
        data['incoming_quote']['text'] = 'Invented wording'
    if fault == 'offset':
        data['incoming_quote']['start'] = 1
    if fault == 'ref':
        data['incoming_quote']['paragraph_ref']['paragraph_text_sha256'] = 'f' * 64
    if fault == 'source_role':
        data['sent_quote'], data['incoming_quote'] = data['incoming_quote'], None
    if fault == 'operation':
        data['operation_ids'] = ['00000000-0000-0000-0000-000000000000']
    if fault == 'position':
        data['position'] = dict(matter_id='1'*32, revision='1'*64, history_sequence=1,
                                position_id='pos_'+'1'*32, version=1)
    before = store_bytes(folder)
    with pytest.raises(p.PositionError):
        mutate(folder, state, 2, dict(action='save_issue', issue_id=IID, expected_version=None, content=data))
    assert store_bytes(folder) == before


def test_legal_decision_is_separate_and_stale_after_changed_basis_or_issue(round_data):
    folder, source, state = round_data
    state, work, oid = link_copy(folder, source, state)
    data = content(source, work, oid)
    state = mutate(folder, state, 4, dict(action='save_issue', issue_id=IID, expected_version=None, content=data))
    assert state['issues'][0]['decision'] is None
    command = dict(action='decide_issue', issue_id=IID, expected_version=1, choice='approve',
                   user_confirmed=True, statement='Use this proposal on this exact version.', basis_sha256=sha(work))
    state = mutate(folder, state, 5, command)
    assert state['issue_observations'][0]['decision_status'] == 'current_basis'
    work.write_bytes(source.read_bytes())
    loaded = server.read_deal_state(str(folder))
    assert loaded['issue_observations'][0]['decision_status'] == 'basis_changed'
    assert loaded['issues'][0]['decision']['choice'] == 'approve'
    state = mutate(folder, state, 6, dict(action='save_issue', issue_id=IID, expected_version=1,
                                        content={**data, 'proposed_wording': 'A revised proposal'}))
    assert state['issues'][0]['version'] == 2 and state['issues'][0]['decision'] is None
    assert len(state['issue_events']) == 3 and state['issue_events'][1]['decision'] is not None
    before = store_bytes(folder)
    with pytest.raises(p.PositionError, match='issue_version_conflict'):
        mutate(folder, state, 7, command)
    assert store_bytes(folder) == before


def test_changed_work_cannot_receive_new_confirmation(round_data):
    folder, source, state = round_data
    state, work, _ = link_copy(folder, source, state)
    state = mutate(folder, state, 4, dict(action='save_issue', issue_id=IID, expected_version=None, content=content(source)))
    digest = sha(work)
    work.write_bytes(source.read_bytes())
    with pytest.raises(p.PositionError, match='working_basis_mismatch'):
        mutate(folder, state, 5, dict(action='decide_issue', issue_id=IID, expected_version=1,
            choice='approve', user_confirmed=True, statement='Approve', basis_sha256=digest))


def test_position_concession_remains_in_original_history_and_invalidates_old_decision(round_data):
    folder, source, state = round_data
    position_id = 'pos_' + '2'*32
    data = dict(title='Scope', desired_outcome='Services', fallback='Supply',
                fallback_conditions='Only if the new annex is agreed', rationale=None,
                related_position_ids=[], content_origin='user_instruction', business_decision='pending', sources=[])
    positions = server.mutate_deal_positions(str(folder), None,
        [dict(op='create', position_id=position_id, content=data)])
    issue = content(source)
    issue['position'] = dict(matter_id=positions['matter_id'], revision=positions['revision'], history_sequence=1,
                             position_id=position_id, version=1)
    state = mutate(folder, state, 2, dict(action='save_issue', issue_id=IID, expected_version=None, content=issue))
    state = mutate(folder, state, 3, dict(action='decide_issue', issue_id=IID, expected_version=1,
        choice='approve', user_confirmed=True, statement='Approve subject to the saved condition.', basis_sha256=sha(source)))
    positions = server.mutate_deal_positions(str(folder), positions['revision'],
        [dict(op='update', position_id=position_id, expected_version=1, content={**data, 'fallback_conditions':'Only if prepaid'})])
    observed = server.read_deal_state(str(folder))
    assert observed['issue_observations'][0] == dict(issue_id=IID, position_status='historical_available', decision_status='basis_changed')
    history = server.read_deal_positions(str(folder), include_history=True)['history']
    assert history[0]['position']['content']['fallback_conditions'] == data['fallback_conditions']


@pytest.mark.parametrize('fault', ['working_sha', 'op_id', 'issue_version', 'event_order', 'dangling'])
def test_closed_store_rejects_broken_links_without_cleanup(round_data, fault):
    folder, source, state = round_data
    state, _, _ = link_copy(folder, source, state)
    state = mutate(folder, state, 4, dict(action='save_issue', issue_id=IID, expected_version=None, content=content(source)))
    path = folder / '.veqtor' / d.STORE_NAME
    value = json.loads(path.read_text())
    if fault == 'working_sha':
        value['working_documents'][0]['file_sha256'] = 'f'*64
    if fault == 'op_id':
        value['working_documents'][0]['operation_id'] = '00000000-0000-0000-0000-000000000000'
    if fault == 'issue_version':
        value['issue_events'][0]['version'] = 2
    if fault == 'event_order':
        value['requests'][1:3] = reversed(value['requests'][1:3])
    if fault == 'dangling':
        value['operations'].append({**value['operations'][0], 'request_id':'f'*32})
    value['revision'] = p._revision(value)
    path.write_bytes(p._json_bytes(value))
    before = path.read_bytes()
    with pytest.raises(p.PositionError, match='storage_corrupt'):
        server.read_deal_state(str(folder))
    assert path.read_bytes() == before


def test_predictable_working_budget_and_result_refusals_never_publish_snapshot(round_data, monkeypatch):
    folder, source, state = round_data
    saved = edit(source, folder / 'working.docx')
    oid = saved['operation_journal']['operation_id']
    state = mutate(folder, state, 2, dict(action='link_operation', source_path=str(source), operation_id=oid))
    directory = folder / '.veqtor' / d.DOCUMENTS
    before = store_bytes(folder)
    names = sorted(directory.iterdir())
    with monkeypatch.context() as patch:
        patch.setattr(d, 'MAX_DOCUMENT_BYTES', source.stat().st_size)
        with pytest.raises(p.PositionError, match='resource_limit_exceeded'):
            mutate(folder, state, 3, dict(action='link_working_document', operation_id=oid))
    def refuse(_):
        raise ValueError('result refused')
    with pytest.raises(ValueError, match='result refused'):
        d.mutate_deal_state(str(folder), state['revision'], f'{3:032x}',
            dict(action='link_working_document', round_id=RID, operation_id=oid), validate_result=refuse)
    assert sorted(directory.iterdir()) == names and store_bytes(folder) == before


def test_observation_budget_is_explicit(round_data, monkeypatch):
    folder, source, state = round_data
    state, _, _ = link_copy(folder, source, state)
    monkeypatch.setattr(w, 'MAX_OPERATION_OBSERVATIONS', 1)
    observed = server.read_deal_state(str(folder), observation_offset=1)
    assert observed['operation_observations'][0]['status'] == 'not_checked'


@pytest.mark.anyio
@pytest.mark.parametrize('mode', ['auto', 'legacy'])
async def test_workflow_commands_through_mcp(round_data, mode):
    from mcp import Client
    folder, source, state = round_data
    saved = edit(source, folder / 'working.docx')
    oid = saved['operation_journal']['operation_id']
    async with Client(server.mcp, mode=mode) as client:
        for n, command in [(2, dict(action='link_operation', source_path=str(source), operation_id=oid)),
                           (3, dict(action='link_working_document', operation_id=oid)),
                           (4, dict(action='save_issue', issue_id=IID, expected_version=None, content=content(source)))]:
            result = await client.call_tool('mutate_deal_state', dict(folder=str(folder),
                expected_revision=state['revision'], request_id=f'{n:032x}', command=dict(round_id=RID, **command)))
            assert not result.is_error, result
            state = result.structured_content
            jsonschema.validate(state, RESULT)
        loaded = await client.call_tool('read_deal_state', dict(folder=str(folder)))
        assert loaded.structured_content['issues'] == state['issues']


def test_explicit_manual_working_save_retains_prior_bytes_and_restarts_decision_basis(round_data):
    folder, source, state = round_data
    state, work, _ = link_copy(folder, source, state)
    state = mutate(folder, state, 4, dict(action='save_issue', issue_id=IID, expected_version=None, content=content(source)))
    state = mutate(folder, state, 5, dict(action='decide_issue', issue_id=IID, expected_version=1,
        choice='approve', user_confirmed=True, statement='Approve this version.', basis_sha256=sha(work)))
    old = state['working_documents'][-1]
    # Fictional stand-in for bytes saved manually by the lawyer. No Word claim.
    work.write_bytes(source.read_bytes())
    manual = dict(action='record_working_version', expected_working_request_id=old['request_id'],
                  file_sha256=sha(work), user_confirmed=True, statement='I saved this exact working version manually.')
    state = mutate(folder, state, 6, manual)
    assert state['working_documents'][-1]['origin'] == 'client_asserted_manual_save'
    assert state['working_documents'][-1]['operation_id'] is None
    assert state['issue_observations'][0]['decision_status'] == 'basis_changed'
    assert state['rounds'][0]['sent_documents'] == []
    work.unlink()
    replay = mutate(folder, state, 6, manual)
    assert replay['revision'] == state['revision']
    assert len(replay['working_documents']) == 2
    assert any(row['file_sha256'] == old['file_sha256'] for row in state['documents'])


@pytest.mark.parametrize('fault', ['missing_previous', 'wrong_previous', 'wrong_hash', 'no_confirmation', 'unchanged'])
def test_manual_save_refuses_ambiguous_or_unconfirmed_version(round_data, fault):
    folder, source, state = round_data
    previous = '0'*32
    digest = sha(source)
    if fault != 'missing_previous':
        state, work, _ = link_copy(folder, source, state)
        previous = state['working_documents'][-1]['request_id']
        digest = sha(work)
        if fault != 'unchanged':
            work.write_bytes(source.read_bytes())
            digest = sha(work)
    if fault == 'wrong_previous':
        previous = '0'*32
    if fault == 'wrong_hash':
        digest = 'f'*64
    before = store_bytes(folder)
    with pytest.raises(DocxError):
        mutate(folder, state, 8, dict(action='record_working_version', expected_working_request_id=previous,
                                    file_sha256=digest, user_confirmed=fault != 'no_confirmation', statement='Manual save'))
    assert store_bytes(folder) == before


def test_missing_backup_blocks_working_result_link_but_keeps_historical_operation(round_data):
    folder, source, state = round_data
    state, work, _ = link_copy(folder, source, state)
    saved = edit(work, old='Services', new='Distribution')
    oid = saved['operation_journal']['operation_id']
    state = mutate(folder, state, 4, dict(action='link_operation', source_path=str(work), operation_id=oid))
    prepared = json.loads((folder / '.veqtor' / 'operations' / oid / 'prepared.json').read_text())
    Path(prepared['backup']['path']).unlink()
    observed = server.read_deal_state(str(folder))
    assert observed['operation_observations'][-1]['status'] == 'completed'
    assert observed['operation_observations'][-1]['backup_current_verified'] is False
    with pytest.raises(p.PositionError, match='operation_unverified'):
        mutate(folder, state, 5, dict(action='link_working_document', operation_id=oid))


def test_read_journal_never_recovers_or_changes_any_record(round_data, monkeypatch):
    folder, source, _ = round_data
    saved = edit(source, folder / 'working.docx')
    oid = saved['operation_journal']['operation_id']
    directory = folder / '.veqtor' / 'operations' / oid
    (directory / 'result.json').unlink()
    baseline = {f.name: f.read_bytes() for f in directory.iterdir()}
    def no_write(*args, **kwargs):
        raise AssertionError('a read tried to write')
    monkeypatch.setattr(journal, '_write', no_write)
    observed = journal.read_operation(str(source), oid)
    assert observed['result'] is None and observed['current_output_sha256'] is not None
    assert {f.name: f.read_bytes() for f in directory.iterdir()} == baseline


def test_next_round_issue_quotes_use_frozen_sent_basis_and_not_prepared_file(round_data, demo_dir):
    folder, source, state = round_data
    state, work, _ = link_copy(folder, source, state)
    actually_sent = folder / 'actually-sent.docx'
    actually_sent.write_bytes((demo_dir / 'round-2-counterparty-redline.docx').read_bytes())
    state = mutate(folder, state, 4, dict(action='record_sent', path=str(actually_sent), file_sha256=sha(actually_sent),
                                        user_confirmed=True, statement='These exact bytes were sent.'))
    next_rid = 'round_' + '2'*32
    def next_command(n, cmd):
        return server.mutate_deal_state(str(folder), state['revision'], f'{n:032x}', dict(round_id=next_rid, **cmd))
    state = next_command(5, dict(action='start_round', previous_round_id=RID, path=str(source), file_sha256=sha(source)))
    data = {**content(source), 'sent_quote':quote(actually_sent)}
    state = next_command(6, dict(action='save_issue', issue_id='issue_'+'2'*32, expected_version=None, content=data))
    assert state['issues'][0]['content']['sent_quote']['file_sha256'] == sha(actually_sent) != sha(work)
    before = store_bytes(folder)
    data['sent_quote'] = quote(work)
    with pytest.raises(p.PositionError, match='issue_source_mismatch'):
        next_command(7, dict(action='save_issue', issue_id='issue_'+'3'*32, expected_version=None, content=data))
    assert store_bytes(folder) == before


def test_linking_actual_operation_to_approved_issue_keeps_the_original_approval(round_data):
    folder, source, state = round_data
    state = mutate(folder, state, 2, dict(action='save_issue', issue_id=IID, expected_version=None, content=content(source)))
    state = mutate(folder, state, 3, dict(action='decide_issue', issue_id=IID, expected_version=1,
        choice='approve', user_confirmed=True, statement='Apply this proposal.', basis_sha256=sha(source)))
    approval = deepcopy(state['issues'][0]['decision'])
    state, _, oid = link_copy(folder, source, state, n=4)
    state = mutate(folder, state, 6, dict(action='link_issue_operation', issue_id=IID, expected_version=1, operation_id=oid))
    assert state['issues'][0]['version'] == 1 and state['issues'][0]['decision'] == approval
    assert state['issues'][0]['content']['operation_ids'] == [oid]
    assert state['operation_observations'][0]['publication_status'] == 'published'
    assert state['rounds'][0]['sent_documents'] == []
    assert state['issues'][0]['content']['remaining_question'] is not None
    assert state['issue_events'][1]['content']['operation_ids'] == []


def test_observation_pages_reach_every_journal_and_latest_working_basis(round_data, monkeypatch):
    folder, source, state = round_data
    state, work, _ = link_copy(folder, source, state)
    saved = edit(work, old='Services', new='Distribution')
    oid = saved['operation_journal']['operation_id']
    state = mutate(folder, state, 4, dict(action='link_operation', source_path=str(work), operation_id=oid))
    state = mutate(folder, state, 5, dict(action='link_working_document', operation_id=oid))
    monkeypatch.setattr(w, 'MAX_OPERATION_OBSERVATIONS', 1)
    first = server.read_deal_state(str(folder))
    second = server.read_deal_state(str(folder), observation_offset=first['observation_page']['next_offset'])
    assert first['observation_page']['next_offset'] == 1
    assert second['observation_page']['next_offset'] is None
    assert first['revision'] == second['revision']
    assert [o['status'] for o in first['operation_observations']] == ['completed', 'not_checked']
    assert [o['status'] for o in second['operation_observations']] == ['not_checked', 'completed']
    assert second['working_observations'][-1]['status'] == 'same_bytes'
    assert first['working_documents'] == second['working_documents']


def test_byte_budget_pages_reach_all_distinct_live_files(round_data, monkeypatch):
    folder, source, state = round_data
    state, first_work, _ = link_copy(folder, source, state)
    second_work = folder / 'second-working.docx'
    saved = edit(source, second_work)
    oid = saved['operation_journal']['operation_id']
    state = mutate(folder, state, 4, dict(action='link_operation', source_path=str(source), operation_id=oid))
    state = mutate(folder, state, 5, dict(action='link_working_document', operation_id=oid))
    state = mutate(folder, state, 6, dict(action='save_issue', issue_id=IID, expected_version=None, content=content(source)))
    state = mutate(folder, state, 7, dict(action='decide_issue', issue_id=IID, expected_version=1,
        choice='approve', basis_sha256=sha(second_work), user_confirmed=True, statement='Approve this wording.'))
    monkeypatch.setattr(p, 'MAX_SOURCE_TOTAL', max(first_work.stat().st_size, second_work.stat().st_size))
    before = store_bytes(folder)
    first = server.read_deal_state(str(folder))
    assert first['observation_page']['next_offset'] == 1
    assert [o['status'] for o in first['working_observations']] == ['same_bytes', 'not_checked']
    assert first['issue_observations'][0]['decision_status'] == 'not_checked'
    second = server.read_deal_state(str(folder), observation_offset=first['observation_page']['next_offset'])
    assert second['observation_page']['next_offset'] is None
    assert second['working_observations'][1]['status'] == 'same_bytes'
    assert second['issue_observations'][0]['decision_status'] == 'current_basis'
    # A file exceeding even a fresh page's bound must not create an endless cursor.
    monkeypatch.setattr(p, 'MAX_SOURCE_BYTES', 1)
    unavailable = server.read_deal_state(str(folder), observation_offset=1)
    assert unavailable['working_observations'][1]['status'] == 'unavailable'
    assert unavailable['observation_page']['next_offset'] is None
    assert unavailable['issue_observations'][0]['decision_status'] == 'unavailable'
    assert store_bytes(folder) == before


def test_restore_missing_original_before_journaled_work_leaves_snapshot_intact(round_data):
    from veqtor_docx import round_snapshots as snapshots
    folder, source, state = round_data
    frozen = Path(state['documents'][0]['path'])
    original = source.read_bytes()
    source.unlink()
    directory_before = {f.name: f.read_bytes() for f in frozen.parent.iterdir()}
    state_before = store_bytes(folder)
    para = inspect_document(str(frozen), 'browse', max_items=1)['paragraphs'][0]
    preflight = drafting.preflight_document_edits(source_path=str(frozen), file_sha256=sha(frozen),
        operations=[dict(kind='replace_text', paragraph_ref=para['paragraph_ref'], old_text='Supply', new_text='Services')],
        output_path=str(folder / 'working.docx'), journal=True, **PROFILE)
    assert not preflight['batch_applicable']
    assert preflight['refusal_code'] == 'protected_storage_path'
    assert store_bytes(folder) == state_before
    assert {f.name: f.read_bytes() for f in frozen.parent.iterdir()} == directory_before

    # Restoration is an explicit local helper, not a synthetic drafting operation.
    restored = folder / 'restored-incoming.docx'
    snapshots.restore_snapshot_copy(frozen.parent, restored, expected_sha256=sha(frozen))
    assert restored.read_bytes() == original
    assert store_bytes(folder) == state_before
    state, work, oid = link_copy(folder, restored, state)
    assert state['operation_observations'][0]['status'] == 'completed'
    assert state['working_observations'][0]['status'] == 'same_bytes'
    assert state['operations'][0]['source_path'] == str(restored)
    assert state['operations'][0]['operation_id'] == oid
    assert state['rounds'][0]['sent_documents'] == []
    assert work.read_bytes() != original
    assert {f.name: f.read_bytes() for f in frozen.parent.iterdir()} == directory_before

def human_copy(folder, source):
    import io
    import zipfile
    out = io.BytesIO()
    with zipfile.ZipFile(source) as z, zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as target:
        for name in z.namelist():
            data = z.read(name)
            if name == 'word/document.xml':
                data = data.replace(b'Supply', b'Manual supply')
            target.writestr(name, data)
    path = folder / 'manual-working.docx'
    path.write_bytes(out.getvalue())
    return path


def import_command(state, path):
    return dict(action='import_working_document', path=path.name, file_sha256=sha(path),
        incoming_basis_sha256=state['rounds'][0]['incoming_sha256'], user_confirmed=True,
        statement='I selected this separate working copy containing my manual edits to this incoming version.')


def test_explicit_initial_manual_import_is_not_a_fabricated_journal_or_send(round_data):
    folder, source, state = round_data
    source_bytes = source.read_bytes()
    work = human_copy(folder, source)
    command = import_command(state, work)
    state = mutate(folder, state, 2, command)
    assert state['schema_version'] == 'deal_state_result.v3'
    assert json.loads(store_bytes(folder))['schema_version'] == 'deal_rounds_store.v3'
    selected = state['working_documents'][0]
    assert selected['origin'] == 'client_asserted_working_import'
    assert selected['operation_id'] is selected['previous_request_id'] is None
    assert selected['incoming_basis_sha256'] == sha(source)
    assert state['operations'] == state['issue_events'] == []
    assert state['rounds'][0]['sent_documents'] == []
    assert not (folder / '.veqtor' / 'operations').exists()
    assert source.read_bytes() == source_bytes
    assert state['working_observations'][0]['status'] == 'same_bytes'
    # Cold restart and a lost-reply retry retain the import after its live path disappears.
    work.unlink()
    script = 'import json,sys;from veqtor_mcp.server import read_deal_state;print(json.dumps(read_deal_state(sys.argv[1])))'
    fresh = json.loads(subprocess.check_output([sys.executable, '-c', script, str(folder)], text=True))
    assert fresh['working_documents'] == state['working_documents']
    assert fresh['working_observations'][0]['status'] == 'unavailable'
    retried = mutate(folder, None, 2, command)
    assert retried['revision'] == state['revision']
    assert len(retried['working_documents']) == 1


def test_manual_import_invalidates_old_decision_and_allows_later_journal_and_manual_save(round_data):
    folder, source, state = round_data
    state = mutate(folder, state, 2, dict(action='save_issue', issue_id=IID, expected_version=None, content=content(source)))
    state = mutate(folder, state, 3, dict(action='decide_issue', issue_id=IID, expected_version=1,
        choice='approve', basis_sha256=sha(source), user_confirmed=True, statement='Approve the incoming wording.'))
    old_decision = state['issues'][0]['decision']
    work = human_copy(folder, source)
    state = mutate(folder, state, 4, import_command(state, work))
    assert state['issues'][0]['decision'] == old_decision
    assert state['issue_observations'][0]['decision_status'] == 'basis_changed'
    saved = edit(work, old='Manual supply', new='Revised manual supply')
    oid = saved['operation_journal']['operation_id']
    state = mutate(folder, state, 5, dict(action='link_operation', source_path=str(work), operation_id=oid))
    state = mutate(folder, state, 6, dict(action='link_working_document', operation_id=oid))
    assert state['working_documents'][1]['previous_request_id'] == state['working_documents'][0]['request_id']
    assert state['working_documents'][1]['origin'] == 'journaled_operation'
    work.write_bytes(source.read_bytes())
    state = mutate(folder, state, 7, dict(action='record_working_version',
        expected_working_request_id=state['working_documents'][-1]['request_id'], file_sha256=sha(work),
        user_confirmed=True, statement='I manually saved this version after the checked edit.'))
    assert len(state['working_documents']) == 3
    assert json.loads(store_bytes(folder))['schema_version'] == 'deal_rounds_store.v3'
    assert len(state['rounds'][0]['sent_documents']) == 0


@pytest.mark.parametrize('fault', ['wrong_basis', 'wrong_hash', 'unconfirmed', 'empty_statement',
                                 'existing_work', 'hardlink', 'symlink', 'parent_alias', 'private_case', 'traversal'])
def test_initial_manual_import_refuses_without_new_snapshot_or_state(round_data, fault):
    folder, source, state = round_data
    work = human_copy(folder, source)
    command = import_command(state, work)
    if fault == 'wrong_basis':
        command['incoming_basis_sha256'] = '0' * 64
    elif fault == 'wrong_hash':
        command['file_sha256'] = '0' * 64
    elif fault == 'unconfirmed':
        command['user_confirmed'] = False
    elif fault == 'empty_statement':
        command['statement'] = ''
    elif fault == 'existing_work':
        state, _, _ = link_copy(folder, source, state)
    elif fault == 'hardlink':
        os.link(work, folder / 'alias.docx')
    elif fault == 'symlink':
        (folder / 'alias.docx').symlink_to(work)
        command['path'] = 'alias.docx'
    elif fault == 'parent_alias':
        (folder / 'alias').symlink_to(folder, target_is_directory=True)
        command['path'] = 'alias/' + work.name
    elif fault == 'private_case':
        command['path'] = '.VEQTOR/' + work.name
    else:
        command['path'] = 'nested/../' + work.name
    before = store_bytes(folder)
    snapshots = sorted((folder / '.veqtor' / d.DOCUMENTS).iterdir())
    with pytest.raises(DocxError):
        mutate(folder, state, 9, command)
    assert store_bytes(folder) == before
    assert sorted((folder / '.veqtor' / d.DOCUMENTS).iterdir()) == snapshots


def test_initial_manual_import_checks_aggregate_snapshot_budget_before_publication(round_data, monkeypatch):
    folder, source, state = round_data
    work = human_copy(folder, source)
    command = import_command(state, work)
    before = store_bytes(folder)
    snapshots = sorted((folder / '.veqtor' / d.DOCUMENTS).iterdir())
    monkeypatch.setattr(d, 'MAX_DOCUMENT_BYTES', source.stat().st_size + work.stat().st_size - 1)
    with pytest.raises(p.PositionError, match='resource_limit_exceeded'):
        mutate(folder, state, 2, command)
    assert store_bytes(folder) == before
    assert sorted((folder / '.veqtor' / d.DOCUMENTS).iterdir()) == snapshots


@pytest.mark.parametrize('field,value', [('incoming_basis_sha256', '0' * 64),
                                       ('previous_request_id', '0' * 32),
                                       ('origin', 'journaled_operation')])
def test_corrupt_initial_import_never_reclassifies_as_verified_history(round_data, field, value):
    folder, source, state = round_data
    work = human_copy(folder, source)
    state = mutate(folder, state, 2, import_command(state, work))
    raw = json.loads(store_bytes(folder))
    raw['working_documents'][0][field] = value
    raw['revision'] = p._revision(raw)
    location = folder / '.veqtor' / d.STORE_NAME
    location.write_text(json.dumps(raw))
    before = location.read_bytes()
    with pytest.raises(p.PositionError, match='storage_corrupt'):
        server.read_deal_state(str(folder))
    assert location.read_bytes() == before


@pytest.mark.anyio
@pytest.mark.parametrize('mode', ['auto', 'legacy'])
async def test_import_working_document_through_mcp(round_data, mode):
    from mcp import Client
    folder, source, state = round_data
    work = human_copy(folder, source)
    async with Client(server.mcp, mode=mode) as client:
        imported = await client.call_tool('mutate_deal_state', dict(folder=str(folder),
            expected_revision=state['revision'], request_id=f'{2:032x}',
            command=dict(round_id=RID, **import_command(state, work))))
        assert not imported.is_error, imported
        result = imported.structured_content
        jsonschema.validate(result, RESULT)
        assert result['working_documents'][0]['origin'] == 'client_asserted_working_import'



@pytest.mark.parametrize('offset', [-1, 101, True, '0'])
def test_invalid_observation_offset_is_refused_without_storage_change(round_data, offset):
    folder, _, _ = round_data
    before = store_bytes(folder)
    with pytest.raises(p.PositionError, match='invalid_request'):
        d.read_deal_state(str(folder), offset)
    assert store_bytes(folder) == before


def test_bilingual_table_quotes_keep_explicit_language_cells_and_exact_source_bytes(round_data):
    import io
    import zipfile
    from lxml import etree
    from veqtor_docx._ooxml import w as tag
    folder, original_source, _ = round_data
    matter = folder / 'bilingual'
    matter.mkdir()
    source = matter / 'incoming.docx'
    # Independent invented EN/RU cells in the existing fictional demo package.
    with zipfile.ZipFile(original_source) as package:
        parts = {i.filename: package.read(i) for i in package.infolist()}
    document = etree.fromstring(parts['word/document.xml'])
    body = document.find(tag('body'))
    body.remove(body[0])
    table = etree.Element(tag('tbl'))
    etree.SubElement(table, tag('tblPr'))
    grid = etree.SubElement(table, tag('tblGrid'))
    for _ in range(2):
        etree.SubElement(grid, tag('gridCol')).set(tag('w'), '4000')
    row = etree.SubElement(table, tag('tr'))
    for text in ['Supply agreement', 'Договор поставки']:
        paragraph = etree.SubElement(etree.SubElement(row, tag('tc')), tag('p'))
        etree.SubElement(etree.SubElement(paragraph, tag('r')), tag('t')).text = text
    body.insert(0, table)
    parts['word/document.xml'] = etree.tostring(document, xml_declaration=True, encoding='UTF-8', standalone=True)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as package:
        for name, data in parts.items():
            package.writestr(name, data)
    source.write_bytes(out.getvalue())
    original = source.read_bytes()
    state = mutate(matter, None, 1, dict(action='start_round', previous_round_id=None, path=str(source), file_sha256=sha(source)))
    paragraphs = inspect_document(str(source), 'browse', max_items=2)['paragraphs']
    for n, paragraph in enumerate(paragraphs, 2):
        data = content(source)
        data['incoming_quote'] = dict(file_sha256=sha(source), paragraph_ref=paragraph['paragraph_ref'], start=0, text=paragraph['text'])
        state = mutate(matter, state, n, dict(action='save_issue', issue_id=f'issue_{n:032x}', expected_version=None, content=data))
    state, _, _ = link_copy(matter, source, state, n=4)
    assert [i['content']['incoming_quote']['text'] for i in state['issues']] == ['Supply agreement', 'Договор поставки']
    assert source.read_bytes() == original
    assert state['working_observations'][0]['status'] == 'same_bytes'
