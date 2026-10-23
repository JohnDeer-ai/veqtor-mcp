# SPDX-License-Identifier: Apache-2.0
"""Independent literal clean outcomes; source bytes and other parts retained."""
import copy
import hashlib
import importlib.util
import io
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from veqtor_docx import ApplyError
from veqtor_docx import clean_copy
from veqtor_docx._ooxml import DOCUMENT_PART, W_NS, w

_spec = importlib.util.spec_from_file_location(
    "clean_source_fixtures", Path(__file__).resolve().parents[1] / "scripts/revision_decisions_smoke.py")
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _package(demo_dir, root, extra=None):
    parts = {}
    with zipfile.ZipFile(demo_dir / "round-1-outgoing-draft.docx") as source:
        parts = {n: source.read(n) for n in source.namelist()}
    parts[DOCUMENT_PART] = etree.tostring(root)
    parts.update(extra or {})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in parts.items():
            target.writestr(name, data)
    return out.getvalue()


def _root(body):
    return etree.fromstring(f'<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>')


def _parts(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {n: archive.read(n) for n in archive.namelist()}


def _clean(payload):
    old = bytes(payload)
    result = clean_copy.prepare_clean_copy(payload, expected_source_sha256=_sha(payload))
    assert payload == old
    assert result.source_sha256 == _sha(old)
    assert result.candidate_sha256 == _sha(result.payload)
    source_parts, output_parts = _parts(payload), _parts(result.payload)
    assert source_parts.keys() == output_parts.keys()
    assert all(output_parts[n] == data for n, data in source_parts.items() if n != DOCUMENT_PART)
    assert result.inventory_after["revisions"] == []
    assert result.inventory_after["blockers"] == []
    return result, etree.fromstring(output_parts[DOCUMENT_PART])


def _normal(root):
    return etree.tostring(root, method="c14n", exclusive=True)


@pytest.mark.parametrize("kind", ["ins", "del", "replacement", *_fixture.CASES])
def test_text_and_all_seven_property_domains(demo_dir, kind):
    source = _fixture.source_document(kind)
    expected = copy.deepcopy(source)
    if kind in {"ins", "del", "replacement"}:
        paragraph = expected.find(f"{w('body')}/{w('p')}")
        for change in list(paragraph):
            if change.tag == w("ins"):
                for run in list(change):
                    paragraph.insert(paragraph.index(change), run)
            paragraph.remove(change)
    else:
        change = next(expected.iter(w(kind)))
        change.getparent().remove(change)
    result, actual = _clean(_package(demo_dir, source))
    assert _normal(actual) == _normal(expected)
    assert result.accepted_revision_count == (2 if kind == "replacement" else 1)


def _revision(kind, text, number=1):
    atom = "t" if kind == "ins" else "delText"
    return (f'<w:{kind} w:id="{number}" w:author="Fictional Other Counsel" w:date="2026-01-01T00:00:00Z">'
            f'<w:r><w:rPr><w:b/></w:rPr><w:{atom}>{text}</w:{atom}></w:r></w:{kind}>')


@pytest.mark.parametrize("kind", ["ins", "del"])
def test_whole_paragraph_with_separator(demo_dir, kind):
    props = '<w:pPr><w:spacing w:after="140"/>' + f'<w:rPr><w:{kind} w:id="8" w:author="Fictional Other Counsel"/></w:rPr></w:pPr>'
    selected = f'<w:p>{props}{_revision(kind, "Selected clause.")}</w:p>'
    following = '<w:p><w:pPr><w:jc w:val="right"/></w:pPr><w:r><w:t>Following clause.</w:t></w:r></w:p>'
    source = _root(selected + following)
    result, actual = _clean(_package(demo_dir, source))
    body = actual.find(w("body"))
    assert len(body) == (2 if kind == "ins" else 1)
    assert list(body.itertext()) == (["Selected clause.", "Following clause."] if kind == "ins" else ["Following clause."])
    assert body[-1].find(f"{w('pPr')}/{w('jc')}").get(w("val")) == "right"
    assert result.accepted_revision_count == 2


def _row(text, mark=None):
    props = '<w:trPr><w:trHeight w:val="640" w:hRule="atLeast"/>'
    if mark:
        props += f'<w:{mark} w:id="90" w:author="Fictional Other Counsel"/>'
    props += '</w:trPr>'
    cells = ''.join('<w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr><w:p>'
                    + (_revision(mark, value, index + 3) if mark else f'<w:r><w:t>{value}</w:t></w:r>')
                    + '</w:p></w:tc>' for index, value in enumerate((text + " EN", text + " RU")))
    return f'<w:tr>{props}{cells}</w:tr>'


@pytest.mark.parametrize("kind", ["ins", "del"])
def test_bilingual_row_whole_both_columns_and_height(demo_dir, kind):
    table = ('<w:tbl><w:tblPr><w:tblW w:w="4000" w:type="dxa"/><w:tblLayout w:type="fixed"/></w:tblPr>'
             '<w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/></w:tblGrid>'
             + _row("Unchanged") + _row("Selected", kind) + '</w:tbl>')
    result, actual = _clean(_package(demo_dir, _root(table)))
    rows = list(actual.iter(w("tr")))
    assert len(rows) == (2 if kind == "ins" else 1)
    assert [''.join(row.itertext()) for row in rows] == (["Unchanged ENUnchanged RU", "Selected ENSelected RU"] if kind == "ins" else ["Unchanged ENUnchanged RU"])
    assert all(row.find(f"{w('trPr')}/{w('trHeight')}").get(w("val")) == "640" for row in rows)
    assert result.accepted_revision_count == 3


def test_more_than_one_public_page_is_not_truncated(demo_dir):
    source = _root(''.join(f'<w:p>{_revision("ins", str(i), i)}</w:p>' for i in range(101)))
    result, actual = _clean(_package(demo_dir, source))
    assert result.accepted_revision_count == 101
    assert [n.text for n in actual.iter(w("t"))] == [str(i) for i in range(101)]


def test_no_revisions_returns_exact_original_zip(demo_dir):
    source = _package(demo_dir, _root('<w:p><w:r><w:t>Unchanged.</w:t></w:r></w:p>'))
    result, _ = _clean(source)
    assert result.payload == source
    assert result.decisions == ()


def test_bound_hash_and_resource_budget(demo_dir, monkeypatch):
    source = _package(demo_dir, _fixture.source_document("ins"))
    with pytest.raises(ApplyError, match="file_sha256_mismatch"):
        clean_copy.prepare_clean_copy(source, expected_source_sha256="0" * 64)
    monkeypatch.setattr(clean_copy, "MAX_CLEAN_VALIDATION_BYTES", 1)
    with pytest.raises(ApplyError, match="resource_limit_exceeded"):
        _clean(source)


def test_revision_in_header_blocks_even_when_body_clean(demo_dir):
    source = _package(demo_dir, _root('<w:p/>'), {
        'word/header1.xml': f'<w:hdr xmlns:w="{W_NS}"><w:p>{_revision("ins", "Header.")}</w:p></w:hdr>'.encode(),
    })
    with pytest.raises(ApplyError, match="clean_copy_unsupported"):
        _clean(source)


@pytest.mark.parametrize("marker", ["bookmark", "commentRange"])
def test_range_enclosing_revision_refuses(demo_dir, marker):
    body = f'<w:p><w:{marker}Start w:id="3"/>{_revision("del", "Selected.")}<w:{marker}End w:id="3"/></w:p>'
    source = _package(demo_dir, _root(body))
    with pytest.raises(ApplyError, match="clean_copy_range_dependency"):
        _clean(source)


def test_unaffected_comment_and_bookmark_markers_remain_exact(demo_dir):
    unaffected = '<w:p><w:bookmarkStart w:id="4" w:name="Keep"/><w:r><w:t>Keep.</w:t></w:r><w:bookmarkEnd w:id="4"/></w:p>'
    source = _package(demo_dir, _root(unaffected + f'<w:p>{_revision("ins", "Added.")}</w:p>'))
    result, actual = _clean(source)
    expected = _root(unaffected).find(f"{w('body')}/{w('p')}")
    assert _normal(actual.find(f"{w('body')}/{w('p')}")) == _normal(expected)
    assert result.accepted_revision_count == 1


def test_unsupported_revision_inside_deleted_content_cannot_disappear(demo_dir):
    revision = _revision("del", "Selected.").replace('<w:rPr><w:b/></w:rPr>', '<w:rPr><w:sectPrChange/></w:rPr>')
    source = _package(demo_dir, _root(f'<w:p>{revision}</w:p>'))
    with pytest.raises(ApplyError, match="clean_copy_unsupported"):
        _clean(source)


def test_partial_mutation_inside_engine_never_hides_later_refusal(demo_dir):
    # The first paragraph is decidable; a later insertion has an unsupported
    # nested known format history. No partial candidate can escape on refusal.
    body = f'<w:p>{_revision("ins", "First.")}</w:p>'
    body += ('<w:p><w:ins w:id="88"><w:r><w:rPr><w:rPrChange w:id="89">'
             '<w:rPr><w:i/></w:rPr></w:rPrChange></w:rPr><w:t>Second.</w:t></w:r></w:ins></w:p>')
    source = _package(demo_dir, _root(body))
    old = bytes(source)
    with pytest.raises(ApplyError):
        _clean(source)
    assert source == old


def test_collateral_mutation_by_decision_helper_blocks_candidate(demo_dir, monkeypatch):
    source = _package(demo_dir, _root(f'<w:p>{_revision("ins", "Added.")}</w:p><w:p><w:r><w:t>Keep.</w:t></w:r></w:p>'))
    actual_apply = clean_copy.decisions.apply_decision

    def corrupted(root, *args, **kwargs):
        result = actual_apply(root, *args, **kwargs)
        list(root.iter(w("t")))[-1].text = "Unrequested change."
        return result

    monkeypatch.setattr(clean_copy.decisions, "apply_decision", corrupted)
    with pytest.raises(ApplyError, match="round_trip_failed"):
        _clean(source)


def test_corrupted_serialization_cannot_change_clean_body(demo_dir, monkeypatch):
    source = _package(demo_dir, _fixture.source_document("ins"))
    original = clean_copy._output_archive_bytes

    def corrupted(infos, parts):
        bad = dict(parts)
        bad[DOCUMENT_PART] = bad[DOCUMENT_PART].replace(b"Proposed wording.", b"Unrequested wording.")
        return original(infos, bad)

    monkeypatch.setattr(clean_copy, "_output_archive_bytes", corrupted)
    with pytest.raises(ApplyError, match="round_trip_failed"):
        _clean(source)


def test_related_table_properties_are_one_final_group(demo_dir):
    source = _fixture.source_document("tblGridChange")
    table = next(source.iter(w("tbl")))
    for index, cell in enumerate(table.iter(w("tc"))):
        props = cell.find(w("tcPr"))
        if props is None:
            props = etree.Element(w("tcPr"))
            cell.insert(0, props)
        current_width = props.find(w("tcW"))
        if current_width is None:
            current_width = etree.SubElement(props, w("tcW"), {w("type"): "dxa"})
        # Current grid and cells agree; the former values need grouped proof.
        current_width.set(w("w"), "2000" if index == 0 else "2800")
        previous = etree.SubElement(etree.SubElement(props, w("tcPrChange"), {w("id"): str(100 + index)}), w("tcPr"))
        etree.SubElement(previous, w("tcW"), {w("w"): "2400", w("type"): "dxa"})
    expected = copy.deepcopy(source)
    for name in ("tblGridChange", "tcPrChange"):
        for node in list(expected.iter(w(name))):
            node.getparent().remove(node)
    result, actual = _clean(_package(demo_dir, source))
    assert result.accepted_revision_count == 3
    assert _normal(actual) == _normal(expected)


def test_several_row_structures_keep_exhaustive_selection(demo_dir):
    table = ('<w:tbl><w:tblPr><w:tblW w:w="4000" w:type="dxa"/><w:tblLayout w:type="fixed"/></w:tblPr>'
             '<w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="2000"/></w:tblGrid>'
             + _row("Keep") + _row("Added", "ins") + _row("Removed", "del") + '</w:tbl>')
    result, actual = _clean(_package(demo_dir, _root(table)))
    assert result.accepted_revision_count == 6
    assert [''.join(row.itertext()) for row in actual.iter(w("tr"))] == ["Keep ENKeep RU", "Added ENAdded RU"]


@pytest.mark.parametrize("kind", ["ins", "del"])
def test_numbered_paragraph_acceptance_uses_existing_numbering(demo_dir, kind):
    num = '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
    before = f'<w:p><w:pPr>{num}</w:pPr><w:r><w:t>First.</w:t></w:r></w:p>'
    target = (f'<w:p><w:pPr>{num}<w:rPr><w:{kind} w:id="3"/></w:rPr></w:pPr>'
              + _revision(kind, "Selected.") + '</w:p>')
    following = f'<w:p><w:pPr>{num}</w:pPr><w:r><w:t>Last.</w:t></w:r></w:p>'
    numbering = (f'<w:numbering xmlns:w="{W_NS}"><w:abstractNum w:abstractNumId="1">'
                 '<w:multiLevelType w:val="singleLevel"/><w:lvl w:ilvl="0"><w:start w:val="1"/>'
                 '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
                 '<w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num></w:numbering>').encode()
    result, actual = _clean(_package(demo_dir, _root(before + target + following), {'word/numbering.xml': numbering}))
    paragraphs = actual.find(w("body")).findall(w("p"))
    assert [''.join(p.itertext()) for p in paragraphs] == (["First.", "Selected.", "Last."] if kind == "ins" else ["First.", "Last."])
    assert all(p.find(f"{w('pPr')}/{w('numPr')}/{w('numId')}").get(w("val")) == "1" for p in paragraphs)
    assert result.accepted_revision_count == 2


@pytest.mark.parametrize("restart", [False, True])
def test_disjoint_numbered_structures_use_complete_accept_context(demo_dir, restart):
    def p(text, kind=None, num="1"):
        props = f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num}"/></w:numPr>'
        if kind:
            props += f'<w:rPr><w:{kind} w:id="3"/></w:rPr>'
        text_xml = _revision(kind, text) if kind else f'<w:r><w:t>{text}</w:t></w:r>'
        return f'<w:p><w:pPr>{props}</w:pPr>{text_xml}</w:p>'

    second = "2" if restart else "1"
    body = p("First.") + p("Added.", "ins") + p("Middle.") + p("Removed.", "del", second) + p("Last.", num=second)
    numbering = (f'<w:numbering xmlns:w="{W_NS}">'
                 + ''.join(f'<w:abstractNum w:abstractNumId="{i}"><w:multiLevelType w:val="singleLevel"/>'
                           f'<w:lvl w:ilvl="0"><w:start w:val="{start}"/><w:numFmt w:val="decimal"/>'
                           '<w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
                           f'<w:num w:numId="{i}"><w:abstractNumId w:val="{i}"/></w:num>' for i, start in ((1, 1), (2, 7)))
                 + '</w:numbering>').encode()
    source = _package(demo_dir, _root(body), {'word/numbering.xml': numbering})
    result, actual = _clean(source)
    assert result.accepted_revision_count == 4
    assert [''.join(p.itertext()) for p in actual.iter(w("p"))] == ["First.", "Added.", "Middle.", "Last."]
    from veqtor_docx.paragraph_numbering import resolve_body_numbering
    labels = resolve_body_numbering(_parts(result.payload), actual, [0, 1, 2, 3])
    assert [row["label"] for row in labels] == ["1.", "2.", "3.", "7." if restart else "4."]


def test_adjacent_overlapping_paragraph_units_remain_explicit_refusal(demo_dir):
    body = ''.join(f'<w:p><w:pPr><w:rPr><w:{kind} w:id="3"/></w:rPr></w:pPr>{_revision(kind, kind)}</w:p>'
                   for kind in ("del", "ins")) + '<w:p><w:r><w:t>Retained.</w:t></w:r></w:p>'
    source = _package(demo_dir, _root(body))
    with pytest.raises(ApplyError, match="paragraph_unit_unsupported|revision_operation_overlap"):
        _clean(source)
