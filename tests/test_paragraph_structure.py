# SPDX-License-Identifier: Apache-2.0
"""Independent synthetic paragraph-count, property and authority expectations."""
import copy
import hashlib
import zipfile
from pathlib import Path

import jsonschema
import pytest
from lxml import etree

from veqtor_docx import drafting, inspect_document
from veqtor_docx._ooxml import DOCUMENT_PART, W_NS, w
from veqtor_docx.revision_decisions import read_document_revisions
from veqtor_mcp._drafting_contract import _APPLIED, REVISIONS_RESULT

PROFILE = {"author": "Fictional Reviewing Counsel", "initials": "FC", "producer_build": "paragraph-test.v1"}


def _root(body):
    return etree.fromstring(f'<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>')


def _p(text, props="", bold=False):
    return (f'<w:p><w:pPr>{props}</w:pPr><w:r>'
            + ('<w:rPr><w:b/></w:rPr>' if bold else '') + f'<w:t>{text}</w:t></w:r></w:p>')


def _write(template, path, root):
    with zipfile.ZipFile(template) as source, zipfile.ZipFile(path, 'w') as target:
        for item in source.infolist():
            target.writestr(item, etree.tostring(root) if item.filename == DOCUMENT_PART else source.read(item))


def _make(tmp_path, demo_dir, *, body=None, numbered=False):
    num = '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>' if numbered else ''
    body = body or (_p('Fictional opening.', num + '<w:jc w:val="center"/>')
                    + _p('Fictional removed clause.', num + '<w:spacing w:after="180"/>', True)
                    + _p('Fictional successor.', num + '<w:jc w:val="right"/>')
                    + _p('Unselected ending.'))
    path = tmp_path / 'working.docx'
    _write(demo_dir / 'round-1-outgoing-draft.docx', path, _root(body))
    if numbered:
        with zipfile.ZipFile(path) as source:
            entries = [(item, source.read(item)) for item in source.infolist()]
        numbering = f"<w:numbering xmlns:w='{W_NS}'><w:abstractNum w:abstractNumId='1'><w:multiLevelType w:val='singleLevel'/><w:lvl w:ilvl='0'><w:start w:val='1'/><w:numFmt w:val='decimal'/><w:lvlText w:val='%1.'/></w:lvl></w:abstractNum><w:num w:numId='1'><w:abstractNumId w:val='1'/></w:num></w:numbering>".encode()
        with zipfile.ZipFile(path, 'w') as target:
            for item, data in entries:
                target.writestr(item, numbering if item.filename == 'word/numbering.xml' else data)
    return path


def _document(path):
    with zipfile.ZipFile(path) as archive:
        return etree.fromstring(archive.read(DOCUMENT_PART))


def _ref(path, index=1):
    return inspect_document(str(path), 'browse', max_items=100)['paragraphs'][index]['paragraph_ref']


def _preview(path, operations):
    return drafting.preflight_document_edits(str(path), hashlib.sha256(path.read_bytes()).hexdigest(), operations, **PROFILE)


def _save(path, operations):
    before = path.read_bytes()
    preview = _preview(path, operations)
    assert preview['batch_applicable'], preview
    assert path.read_bytes() == before
    jsonschema.validate(preview['operations'], _APPLIED)
    result = drafting.apply_document_edits(str(path), hashlib.sha256(before).hexdigest(), operations,
                                          preview['preflight_proof'], **PROFILE)
    assert Path(result['backup_path']).read_bytes() == before
    jsonschema.validate(result['operations'], _APPLIED)
    return result


def _target(path):
    inventory = read_document_revisions(str(path))
    # Raw library inventory lacks only the server's common producer metadata.
    schema = copy.deepcopy(REVISIONS_RESULT)
    for name in ('producer', 'record_id', 'record_status'):
        schema['properties'].pop(name)
        schema['required'].remove(name)
    jsonschema.validate(inventory, schema)
    targets = [item['structural_target'] for item in inventory['revisions'] if 'structural_target' in item]
    assert len(targets) == 1, inventory
    return targets[0]


def _normal(node):
    # Empty pPr/rPr containers have no effective properties; Word may add them.
    node = copy.deepcopy(node)
    for tag in ('rPr', 'pPr'):
        for props in list(node.iter(w(tag))):
            if not len(props) and not props.attrib:
                props.getparent().remove(props)
    return etree.tostring(node, method='c14n', exclusive=True)


@pytest.mark.parametrize('numbered', [False, True])
@pytest.mark.parametrize('decision', ['accept', 'reject'])
def test_delete_complete_clause_and_mark_has_exact_independent_outcomes(tmp_path, demo_dir, numbered, decision):
    path = _make(tmp_path, demo_dir, numbered=numbered)
    original = _document(path)
    original_successor = copy.deepcopy(original.find(w('body'))[2])
    _save(path, [{'kind': 'delete_paragraph', 'paragraph_ref': _ref(path), 'old_text': 'Fictional removed clause.'}])
    pending = _document(path)
    assert len(list(pending.iter(w('p')))) == 4
    assert len(pending.findall(f'.//{w("pPr")}/{w("rPr")}/{w("del")}')) == 1
    assert len(pending.findall(f'.//{w("p")}/{w("del")}')) == 1
    assert _normal(pending.find(w('body'))[2]) == _normal(original_successor)
    _save(path, [{'kind': 'decide_revision', 'decision': decision, 'target': _target(path)}])
    expected = copy.deepcopy(original)
    if decision == 'accept':
        expected.find(w('body')).remove(expected.find(w('body'))[1])
    assert _normal(_document(path)) == _normal(expected)


@pytest.mark.parametrize('position', ['before', 'after'])
@pytest.mark.parametrize('decision', ['accept', 'reject'])
@pytest.mark.parametrize('at_end', [False, True])
def test_existing_insertions_have_complete_explicit_decisions(tmp_path, demo_dir, position, decision, at_end):
    path = _make(tmp_path, demo_dir)
    original = _document(path)
    index = 3 if at_end else 1
    _save(path, [{'kind': 'insert_paragraph', 'paragraph_ref': _ref(path, index),
                  'position': position, 'text': 'New fictional clause.'}])
    target = _target(path)
    assert target['kind'] == 'paragraph_structure'
    _save(path, [{'kind': 'decide_revision', 'decision': decision, 'target': target}])
    expected = copy.deepcopy(original)
    if decision == 'accept':
        anchor = expected.find(w('body'))[index]
        paragraph = copy.deepcopy(anchor)
        for child in list(paragraph):
            if child.tag != w('pPr'):
                paragraph.remove(child)
        run = etree.SubElement(paragraph, w('r'))
        old_run_props = anchor.find(f'{w("r")}/{w("rPr")}')
        if old_run_props is not None:
            run.append(copy.deepcopy(old_run_props))
        etree.SubElement(run, w('t'), {'{http://www.w3.org/XML/1998/namespace}space': 'preserve'}).text = 'New fictional clause.'
        expected.find(w('body')).insert(index + (position == 'after'), paragraph)
    assert _normal(_document(path)) == _normal(expected)


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'wrong_text_ref', 'mark_only'])
def test_complete_refs_required_no_implicit_companions(tmp_path, demo_dir, mutation):
    path = _make(tmp_path, demo_dir)
    _save(path, [{'kind': 'delete_paragraph', 'paragraph_ref': _ref(path), 'old_text': 'Fictional removed clause.'}])
    target = _target(path)
    if mutation == 'missing':
        target['text_revision_refs'] = []
    elif mutation == 'duplicate':
        target['text_revision_refs'] *= 2
    elif mutation == 'wrong_text_ref':
        target['text_revision_refs'] = [target['paragraph_mark_ref']]
    else:
        target = {'kind': 'single', 'revision_ref': target['paragraph_mark_ref']}
    before = path.read_bytes()
    preview = _preview(path, [{'kind': 'decide_revision', 'target': target, 'decision': 'accept'}])
    assert not preview['batch_applicable'], preview
    assert path.read_bytes() == before


@pytest.mark.parametrize('body', [
    _p('Only paragraph.'),
    _p('Before table.') + '<w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="1000"/></w:tblGrid><w:tr><w:tc>' + _p('Cell.') + '</w:tc></w:tr></w:tbl>',
    _p('Section clause.', '<w:sectPr/>') + _p('After.'),
    '<w:p><w:bookmarkStart w:id="1" w:name="Fictional"/><w:r><w:t>Marked.</w:t></w:r><w:bookmarkEnd w:id="1"/></w:p>' + _p('After.'),
    '<w:p><w:r><w:fldChar w:fldCharType="begin"/><w:instrText> PAGE </w:instrText><w:fldChar w:fldCharType="end"/><w:t>Field.</w:t></w:r></w:p>' + _p('After.'),
])
def test_delete_unsafe_boundaries_refuse_before_publication(tmp_path, demo_dir, body):
    path = _make(tmp_path, demo_dir, body=body)
    read = inspect_document(str(path), 'browse', max_items=100)
    paragraph = read['paragraphs'][0]
    before = path.read_bytes()
    preview = _preview(path, [{'kind': 'delete_paragraph', 'paragraph_ref': paragraph['paragraph_ref'], 'old_text': paragraph['text']}])
    assert not preview['batch_applicable'], preview
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('*.bak*'))


@pytest.mark.parametrize('decision', ['accept', 'reject'])
@pytest.mark.parametrize('position', ['before', 'after'])
def test_inserted_cell_paragraph_decisions_keep_mandatory_last_p(tmp_path, demo_dir, decision, position):
    body = ('<w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="1000"/></w:tblGrid><w:tr><w:tc>'
            + _p('Cell anchor.', '<w:jc w:val="right"/>') + '</w:tc></w:tr></w:tbl>' + _p('Tail.'))
    path = _make(tmp_path, demo_dir, body=body)
    _save(path, [{'kind': 'insert_paragraph', 'paragraph_ref': _ref(path, 0),
                  'position': position, 'text': 'New cell clause.'}])
    _save(path, [{'kind': 'decide_revision', 'decision': decision, 'target': _target(path)}])
    cell = _document(path).find(f'.//{w("tc")}')
    assert len(cell.findall(w('p'))) == (2 if decision == 'accept' else 1)
    assert cell[-1].tag == w('p')
    assert [''.join(n.itertext()) for n in cell.findall(f'{w("p")}/{w("r")}/{w("t")}')] == (
        ['New cell clause.', 'Cell anchor.'] if position == 'before' and decision == 'accept' else
        ['Cell anchor.', 'New cell clause.'] if decision == 'accept' else ['Cell anchor.'])


@pytest.mark.parametrize('decision', ['accept', 'reject'])
def test_untouched_foreign_history_outside_unit_is_exact(tmp_path, demo_dir, decision):
    foreign = ('<w:p><w:ins w:id="77" w:author="Fictional Other Reviewer" w:date="2026-01-01T00:00:00Z">'
               '<w:r><w:rPr><w:i/></w:rPr><w:t>Other pending proposal.</w:t></w:r></w:ins></w:p>')
    body = _p('Opening.') + _p('Delete this.') + _p('Keep next.', '<w:jc w:val="right"/>') + foreign
    path = _make(tmp_path, demo_dir, body=body)
    untouched = etree.tostring(_document(path).find(w('body'))[-1])
    _save(path, [{'kind': 'delete_paragraph', 'paragraph_ref': _ref(path), 'old_text': 'Delete this.'}])
    _save(path, [{'kind': 'decide_revision', 'decision': decision, 'target': _target(path)}])
    assert etree.tostring(_document(path).find(w('body'))[-1]) == untouched


def test_structural_group_never_inferred_from_author_date_or_revision_ids(tmp_path, demo_dir):
    path = _make(tmp_path, demo_dir)
    _save(path, [{'kind': 'delete_paragraph', 'paragraph_ref': _ref(path), 'old_text': 'Fictional removed clause.'}])
    pending = _document(path)
    for index, node in enumerate(pending.iter(w('del'))):
        node.set(w('author'), f'Independent Fictional Reviewer {index}')
        node.set(w('id'), str(600 + index * 17))
        node.set(w('date'), f'2026-01-0{index + 1}T00:00:00Z')
    _write(path, tmp_path / 'resaved.docx', pending)
    path = tmp_path / 'resaved.docx'
    target = _target(path)
    result = _save(path, [{'kind': 'decide_revision', 'decision': 'reject', 'target': target}])
    assert [m['author'] for m in result['operations'][0]['members']] == [
        'Independent Fictional Reviewer 0', 'Independent Fictional Reviewer 1']


@pytest.mark.parametrize('selection', ['successor_edit', 'target_edit', 'adjacent_delete'])
def test_overlapping_structural_deletion_refuses_atomically(tmp_path, demo_dir, selection):
    path = _make(tmp_path, demo_dir)
    operations = [{'kind': 'delete_paragraph', 'paragraph_ref': _ref(path), 'old_text': 'Fictional removed clause.'}]
    if selection == 'adjacent_delete':
        operations += [{'kind': 'delete_paragraph', 'paragraph_ref': _ref(path, 2), 'old_text': 'Fictional successor.'}]
    else:
        index = 2 if selection == 'successor_edit' else 1
        old = 'successor' if index == 2 else 'removed'
        operations += [{'kind': 'replace_text', 'paragraph_ref': _ref(path, index), 'old_text': old, 'new_text': 'changed'}]
    before = path.read_bytes()
    preview = _preview(path, operations)
    assert not preview['batch_applicable']
    assert preview['refusal_code'] == 'paragraph_operation_overlap'
    assert path.read_bytes() == before


def test_stale_structural_refs_after_new_snapshot_refuse(tmp_path, demo_dir):
    path = _make(tmp_path, demo_dir)
    _save(path, [{'kind': 'delete_paragraph', 'paragraph_ref': _ref(path), 'old_text': 'Fictional removed clause.'}])
    old_target = _target(path)
    root = _document(path)
    root.find(w('body'))[-1].set(w('rsidR'), '00000001')
    _write(path, tmp_path / 'new.docx', root)
    new_path = tmp_path / 'new.docx'
    preview = _preview(new_path, [{'kind': 'decide_revision', 'decision': 'accept', 'target': old_target}])
    assert not preview['batch_applicable']
    assert preview['refusal_code'] == 'revision_reference_mismatch'


@pytest.mark.parametrize('index', [0, 1, 2])
def test_delete_expected_numbering_and_continuation(tmp_path, demo_dir, index):
    path = _make(tmp_path, demo_dir, numbered=True)
    texts = ['Fictional opening.', 'Fictional removed clause.', 'Fictional successor.']
    result = _save(path, [{'kind': 'delete_paragraph', 'paragraph_ref': _ref(path, index), 'old_text': texts[index]}])
    facts = result['operations'][0]['paragraph_numbering']
    before_labels = {fact['paragraph_index']: fact['label'] for fact in facts['rejected'] if fact['num_id']}
    after_labels = {fact['paragraph_index']: fact['label'] for fact in facts['accepted'] if fact['num_id']}
    assert before_labels == {i: f'{i + 1}.' for i in before_labels}
    assert after_labels == {i: f'{i + 1}.' for i in after_labels}
    assert before_labels[index] == f'{index + 1}.'
    if index < 2:
        assert after_labels[index] == f'{index + 1}.'



@pytest.mark.parametrize('kind', ['bookmark', 'comment', 'permission', 'nested_old_deletion', 'pending_successor'])
def test_structural_units_preserve_range_and_revision_refusal_boundaries(tmp_path, demo_dir, kind):
    path = _make(tmp_path, demo_dir)
    _save(path, [{'kind': 'delete_paragraph', 'paragraph_ref': _ref(path), 'old_text': 'Fictional removed clause.'}])
    root = _document(path)
    target, successor = root.find(w('body'))[1:3]
    if kind == 'pending_successor':
        revision = etree.Element(w('ins'), {w('id'): '90', w('author'): 'Foreign Fictional Reviewer'})
        revision.append(successor[-1])
        successor.append(revision)
    elif kind == 'nested_old_deletion':
        old = target.find(w('del'))
        old.insert(0, etree.Element(w('bookmarkStart'), {w('id'): '4', w('name'): 'Hidden'}))
        old.append(etree.Element(w('bookmarkEnd'), {w('id'): '4'}))
    else:
        tags = {'bookmark': ('bookmarkStart', 'bookmarkEnd'), 'comment': ('commentRangeStart', 'commentRangeEnd'),
                'permission': ('permStart', 'permEnd')}[kind]
        target.insert(1, etree.Element(w(tags[0]), {w('id'): '4'}))
        successor.append(etree.Element(w(tags[1]), {w('id'): '4'}))
    _write(path, tmp_path / 'unsafe.docx', root)
    unsafe = tmp_path / 'unsafe.docx'
    inventory = read_document_revisions(str(unsafe))
    assert not any('structural_target' in item for item in inventory['revisions'])


@pytest.mark.parametrize('position', ['before', 'after'])
@pytest.mark.parametrize('decision', ['accept', 'reject'])
def test_text_alone_cannot_partially_decide_a_paragraph_unit(tmp_path, demo_dir, position, decision):
    path = _make(tmp_path, demo_dir)
    _save(path, [{'kind': 'insert_paragraph', 'paragraph_ref': _ref(path),
                  'position': position, 'text': 'Complete new paragraph.'}])
    target = _target(path)
    before = path.read_bytes()
    preview = _preview(path, [{'kind': 'decide_revision', 'decision': decision,
                              'target': {'kind': 'single', 'revision_ref': target['text_revision_refs'][0]}}])
    assert not preview['batch_applicable']
    assert preview['refusal_code'] == 'paragraph_unit_unsupported'
    assert path.read_bytes() == before


def _replace_part(path, name, payload):
    with zipfile.ZipFile(path) as source:
        entries = [(item, source.read(item)) for item in source.infolist()]
    with zipfile.ZipFile(path, 'w') as target:
        for item, data in entries:
            target.writestr(item, payload if item.filename == name else data)


@pytest.mark.parametrize('decision', ['accept', 'reject'])
@pytest.mark.parametrize('scheme', ['style', 'restart'])
def test_numbered_structural_save_style_inheritance_and_proved_restart(tmp_path, demo_dir, scheme, decision):
    path = _make(tmp_path, demo_dir, numbered=True)
    original = _document(path)
    if scheme == 'style':
        for paragraph in list(original.find(w('body')))[:3]:
            props = paragraph.find(w('pPr'))
            props.remove(props.find(w('numPr')))
            props.insert(0, etree.Element(w('pStyle'), {w('val'): 'FictionalList'}))
        with zipfile.ZipFile(path) as archive:
            styles = etree.fromstring(archive.read('word/styles.xml'))
        style = etree.SubElement(styles, w('style'), {w('type'): 'paragraph', w('styleId'): 'FictionalList'})
        etree.SubElement(style, w('basedOn'), {w('val'): 'VBody'})
        props = etree.SubElement(style, w('pPr'))
        num = etree.SubElement(props, w('numPr'))
        etree.SubElement(num, w('ilvl'), {w('val'): '0'})
        etree.SubElement(num, w('numId'), {w('val'): '1'})
        _replace_part(path, 'word/styles.xml', etree.tostring(styles))
        _write(path, tmp_path / 'styled.docx', original)
        path = tmp_path / 'styled.docx'
    else:
        with zipfile.ZipFile(path) as archive:
            numbering = etree.fromstring(archive.read('word/numbering.xml'))
        override = etree.SubElement(numbering.find(w('num')), w('lvlOverride'), {w('ilvl'): '0'})
        etree.SubElement(override, w('startOverride'), {w('val'): '7'})
        _replace_part(path, 'word/numbering.xml', etree.tostring(numbering))
    result = _save(path, [{'kind': 'delete_paragraph', 'paragraph_ref': _ref(path), 'old_text': 'Fictional removed clause.'}])
    expected_before = ['7.', '8.', '9.'] if scheme == 'restart' else ['1.', '2.', '3.']
    assert [x['label'] for x in result['operations'][0]['paragraph_numbering']['rejected'] if x['num_id']] == expected_before
    _save(path, [{'kind': 'decide_revision', 'decision': decision, 'target': _target(path)}])
    if decision == 'accept':
        original.find(w('body')).remove(original.find(w('body'))[1])
    assert _normal(_document(path)) == _normal(original)


def test_deletion_helper_cannot_change_successor_properties(monkeypatch, tmp_path, demo_dir):
    path = _make(tmp_path, demo_dir)
    implementation = drafting.delete_paragraph
    def corrupt(paragraph, *args, **kwargs):
        facts = implementation(paragraph, *args, **kwargs)
        paragraph.getnext().find(f'{w("pPr")}/{w("jc")}').set(w('val'), 'left')
        return facts
    monkeypatch.setattr(drafting, 'delete_paragraph', corrupt)
    before = path.read_bytes()
    preview = _preview(path, [{'kind': 'delete_paragraph', 'paragraph_ref': _ref(path), 'old_text': 'Fictional removed clause.'}])
    assert not preview['batch_applicable']
    assert preview['refusal_code'] == 'round_trip_failed'
    assert path.read_bytes() == before


def test_structural_decision_inverse_detects_unrelated_mutation(monkeypatch, tmp_path, demo_dir):
    path = _make(tmp_path, demo_dir)
    _save(path, [{'kind': 'delete_paragraph', 'paragraph_ref': _ref(path), 'old_text': 'Fictional removed clause.'}])
    implementation = drafting.decisions.apply_decision
    def corrupt(root, *args, **kwargs):
        result = implementation(root, *args, **kwargs)
        root.find(w('body'))[-1].find(f'{w("r")}/{w("t")}').text = 'Unrequested mutation.'
        return result
    monkeypatch.setattr(drafting.decisions, 'apply_decision', corrupt)
    before = path.read_bytes()
    preview = _preview(path, [{'kind': 'decide_revision', 'decision': 'accept', 'target': _target(path)}])
    assert not preview['batch_applicable']
    assert preview['refusal_code'] == 'round_trip_failed'
    assert path.read_bytes() == before


def test_structural_apply_proof_binds_exact_decision_and_full_members(tmp_path, demo_dir):
    from veqtor_docx import ApplyError
    path = _make(tmp_path, demo_dir)
    _save(path, [{'kind': 'delete_paragraph', 'paragraph_ref': _ref(path), 'old_text': 'Fictional removed clause.'}])
    operations = [{'kind': 'decide_revision', 'decision': 'accept', 'target': _target(path)}]
    preview = _preview(path, operations)
    before = path.read_bytes()
    operations[0]['decision'] = 'reject'
    with pytest.raises(ApplyError, match='preflight'):
        drafting.apply_document_edits(str(path), hashlib.sha256(before).hexdigest(), operations,
                                     preview['preflight_proof'], **PROFILE)
    assert path.read_bytes() == before


@pytest.mark.parametrize('decision', ['accept', 'reject'])
def test_unselected_successor_empty_properties_are_preserved_exactly(tmp_path, demo_dir, decision):
    body = _p('Remove.') + _p('Keep exact empty properties.') + _p('Ending.')
    path = _make(tmp_path, demo_dir, body=body)
    original_successor = etree.tostring(_document(path).find(w('body'))[1])
    _save(path, [{'kind': 'delete_paragraph', 'paragraph_ref': _ref(path, 0), 'old_text': 'Remove.'}])
    _save(path, [{'kind': 'decide_revision', 'decision': decision, 'target': _target(path)}])
    successor = _document(path).find(w('body'))[0 if decision == 'accept' else 1]
    assert etree.tostring(successor) == original_successor


@pytest.mark.parametrize('decision', ['accept', 'reject'])
@pytest.mark.parametrize('reverse', [False, True])
def test_independent_structural_units_apply_in_either_order(tmp_path, demo_dir, decision, reverse):
    body = ''.join(_p(f'Clause {i}.', f'<w:spacing w:after="{i * 30}"/>') for i in range(6))
    path = _make(tmp_path, demo_dir, body=body)
    original = _document(path)
    _save(path, [{'kind': 'delete_paragraph', 'paragraph_ref': _ref(path, index), 'old_text': f'Clause {index}.'}
                 for index in (0, 3)])
    inventory = read_document_revisions(str(path))
    targets = [item['structural_target'] for item in inventory['revisions'] if 'structural_target' in item]
    assert len(targets) == 2, inventory
    operations = [{'kind': 'decide_revision', 'decision': decision, 'target': target} for target in targets]
    if reverse:
        operations.reverse()
    _save(path, operations)
    if decision == 'accept':
        for index in (3, 0):
            original.find(w('body')).remove(original.find(w('body'))[index])
    assert _normal(_document(path)) == _normal(original)


@pytest.mark.parametrize('decision', ['accept', 'reject'])
@pytest.mark.parametrize('dependency', ['bookmark', 'foreign_history', 'ambiguous_content'])
def test_unsupported_insert_after_unit_cannot_decide_only_its_text(tmp_path, demo_dir, decision, dependency):
    mark = '<w:rPr><w:ins w:id="10" w:author="Fictional Reviewer"/></w:rPr>'
    if dependency == 'ambiguous_content':
        anchor = (f'<w:p><w:pPr>{mark}</w:pPr><w:ins w:id="12" w:author="Fictional Other">'
                  '<w:r><w:t>Anchor.</w:t></w:r></w:ins></w:p>')
    else:
        anchor = _p('Anchor.', mark)
        extra = ('<w:bookmarkStart w:id="1" w:name="FictionalBookmark"/>'
                 '<w:bookmarkEnd w:id="1"/>' if dependency == 'bookmark' else
                 '<w:ins w:id="12" w:author="Fictional Other"><w:r><w:t> Other.</w:t></w:r></w:ins>')
        anchor = anchor.replace('</w:p>', extra + '</w:p>')
    proposal = ('<w:p><w:ins w:id="11" w:author="Fictional Reviewer">'
                '<w:r><w:t>Proposed clause.</w:t></w:r></w:ins></w:p>')
    path = _make(tmp_path, demo_dir, body=_p('Opening.') + anchor + proposal + _p('Ending.'))
    before = path.read_bytes()
    inventory = read_document_revisions(str(path))
    item = next(row for row in inventory['revisions'] if row['id'] == '11')
    preview = _preview(path, [{'kind': 'decide_revision', 'decision': decision,
                               'target': {'kind': 'single', 'revision_ref': item['revision_ref']}}])
    assert not preview['batch_applicable']
    assert preview['refusal_code'] == 'paragraph_unit_unsupported'
    assert path.read_bytes() == before
    assert not (tmp_path / '.veqtor-backups').exists()


@pytest.mark.parametrize('decision', ['accept', 'reject'])
def test_unrelated_text_revision_without_separator_dependency_still_saves(tmp_path, demo_dir, decision):
    proposal = ('<w:p><w:r><w:t>Existing text.</w:t></w:r>'
                '<w:ins w:id="11" w:author="Fictional Reviewer"><w:r><w:t> Added.</w:t></w:r></w:ins></w:p>')
    path = _make(tmp_path, demo_dir, body=_p('Opening.') + proposal + _p('Ending.'))
    inventory = read_document_revisions(str(path))
    item = next(row for row in inventory['revisions'] if row['id'] == '11')
    _save(path, [{'kind': 'decide_revision', 'decision': decision,
                 'target': {'kind': 'single', 'revision_ref': item['revision_ref']}}])
    expected = 'Existing text. Added.' if decision == 'accept' else 'Existing text.'
    assert ''.join(_document(path).find(w('body'))[1].itertext()) == expected
