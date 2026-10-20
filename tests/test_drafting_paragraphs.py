# SPDX-License-Identifier: Apache-2.0
"""Synthetic fixtures with independent accept/reject projections."""

import copy
from xml.sax.saxutils import escape

import pytest
from lxml import etree

from veqtor_docx._drafting_paragraphs import (
    MAX_PARAGRAPH_EDIT_STEPS,
    insert_paragraph,
    replace_paragraph,
)
from veqtor_docx._ooxml import w
from veqtor_docx.apply import (
    MAX_REVISION_ID,
    ApplyError,
    _paragraph_segments,
    _reading_text,
    _revision_id_allocator,
    _RevisionIdAllocator,
)

AUTHOR = "Shilov, Ilya"
OTHER = "Synthetic Counterparty"
NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _r(text, props=""):
    props = f"<w:rPr>{props}</w:rPr>" if props else ""
    return f'<w:r>{props}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _ins(text, identity="24", author=AUTHOR):
    return (
        f'<w:ins w:id="{identity}" w:author="{author}" w:date="2026-01-02T03:04:05">'
        f"{_r(text)}</w:ins>"
    )


def _del(text, identity="25", author=OTHER):
    return (
        f'<w:del w:id="{identity}" w:author="{author}">'
        f'<w:r><w:delText xml:space="preserve">{escape(text)}</w:delText></w:r></w:del>'
    )


def _document(markup):
    return etree.fromstring(
        f'<w:document xmlns:w="{NS}"><w:body>{markup}</w:body></w:document>'
    )


def _p(markup):
    return etree.fromstring(f'<w:p xmlns:w="{NS}">{markup}</w:p>')


def _reading(paragraph):
    return _reading_text(_paragraph_segments(paragraph))


def _native_projection(document, accept):
    """Interpret revision semantics without using production readers/writers.

    Rejecting an inserted paragraph mark joins it to its following paragraph;
    the following mark's formatting survives. Tests therefore detect a trailing
    new paragraph whose only mark was incorrectly marked as inserted.
    """
    result = copy.deepcopy(document)
    for paragraph in list(result.iter(w("p"))):
        for revision in list(paragraph):
            if revision.tag not in {w("ins"), w("del")}:
                continue
            if (revision.tag == w("ins")) != accept:
                paragraph.remove(revision)
            else:
                for child in list(revision):
                    for atom in child.iter(w("delText")):
                        atom.tag = w("t")
                    revision.addprevious(child)
                paragraph.remove(revision)
    for paragraph in list(result.iter(w("p"))):
        mark = paragraph.find(f"{w('pPr')}/{w('rPr')}/{w('ins')}")
        if mark is None:
            continue
        if accept:
            mark.getparent().remove(mark)
        else:
            following = paragraph.getnext()
            assert following is not None and following.tag == w("p"), (
                "rejecting an inserted break must have a following paragraph to join"
            )
            index = 1 if following.find(w("pPr")) is not None else 0
            for child in list(paragraph):
                if child.tag != w("pPr"):
                    following.insert(index, child)
                    index += 1
            paragraph.getparent().remove(paragraph)
    return result


def _projected_texts(document, accept):
    return [
        "".join(atom.text or "" for atom in paragraph.iter(w("t")))
        for paragraph in _native_projection(document, accept).iter(w("p"))
    ]


def _check_steps(before, after, mutable, facts):
    # The transaction-level checker is deliberately independent of this helper.
    from veqtor_docx.drafting import _check_revision_preservation, _revision_state

    _check_revision_preservation(
        _revision_state(before), after, mutable, facts, AUTHOR, "2026-10-20T13:14:15+03:00"
    )


def _replace(paragraph, old, new, allocator=None):
    return replace_paragraph(
        paragraph,
        old,
        new,
        AUTHOR,
        allocator or _revision_id_allocator(paragraph),
        timestamp="2026-10-20T13:14:15+03:00",
        verify_step=_check_steps,
    )


@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("table", [False, True])
def test_insert_accept_reject_roundtrip_at_document_and_cell_ends(position, table):
    markup = f'<w:p w:rsidR="AABBCCDD">{_r("Original", "<w:b/>")}</w:p>'
    if table:
        markup = f"<w:tbl><w:tr><w:tc>{markup}</w:tc></w:tr></w:tbl>"
    document = _document(markup)
    anchor = next(document.iter(w("p")))
    allocator = _revision_id_allocator(document)
    inserted, facts = insert_paragraph(
        anchor, position, "New text", AUTHOR, allocator,
        timestamp="2026-10-20T13:14:15+03:00",
    )
    assert _reading(inserted) == "New text"
    assert not inserted.attrib
    assert inserted.find(f"{w('ins')}/{w('r')}/{w('rPr')}/{w('b')}") is not None
    assert _projected_texts(document, True) == (
        ["New text", "Original"] if position == "before" else ["Original", "New text"]
    )
    assert _projected_texts(document, False) == ["Original"]
    assert facts["revision_ids"] == ["101", "102"]
    assert facts["paragraph_mark_location"] == ("inserted" if position == "before" else "anchor")
    for node in document.iter(w("ins")):
        assert node.get(w("author")) == AUTHOR
        assert node.get(w("date")) == "2026-10-20T13:14:15"
    assert allocator.next_value == 103


@pytest.mark.parametrize("position", ["before", "after"])
def test_insert_inherits_style_numbering_and_paragraph_mark_format(position):
    props = (
        '<w:pPr><w:pStyle w:val="ContractClause"/>'
        '<w:numPr><w:ilvl w:val="1"/><w:numId w:val="7"/></w:numPr>'
        '<w:spacing w:after="120"/><w:rPr><w:lang w:val="en-US"/></w:rPr></w:pPr>'
    )
    document = _document(f"<w:p>{props}{_r('Anchor')}</w:p><w:p>{_r('Next')}</w:p>")
    anchor = document.find(f"{w('body')}/{w('p')}")
    before_props = copy.deepcopy(anchor.find(w("pPr")))
    inserted, _ = insert_paragraph(
        anchor, position, "Clause", AUTHOR, _revision_id_allocator(document)
    )
    for paragraph in (anchor, inserted):
        actual = copy.deepcopy(paragraph.find(w("pPr")))
        for mark in actual.findall(f"{w('rPr')}/{w('ins')}"):
            mark.getparent().remove(mark)
        assert etree.tostring(actual) == etree.tostring(before_props)
    rejected = _native_projection(document, False)
    assert rejected.find(f"{w('body')}/{w('p')}/{w('pPr')}/{w('numPr')}/{w('numId')}").get(w("val")) == "7"
    assert _projected_texts(document, False) == ["Anchor", "Next"]


def test_inherited_run_formatting_does_not_duplicate_a_foreign_format_revision():
    formatting = (
        '<w:b/><w:rPrChange w:id="11" w:author="Synthetic Counterparty">'
        "<w:rPr><w:i/></w:rPr></w:rPrChange>"
    )
    document = _document(f"<w:p>{_r('Anchor', formatting)}</w:p>")
    anchor = next(document.iter(w("p")))
    original = etree.tostring(anchor)
    inserted, _ = insert_paragraph(
        anchor, "before", "New", AUTHOR, _revision_id_allocator(document)
    )
    assert etree.tostring(anchor) == original
    assert inserted.find(f"{w('ins')}/{w('r')}/{w('rPr')}/{w('b')}") is not None
    assert not list(inserted.iter(w("rPrChange")))
    assert len(list(document.iter(w("rPrChange")))) == 1


@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("props", [
    "<w:sectPr/>",
    '<w:framePr w:wrap="around"/>',
    '<w:rPr><w:ins w:id="5" w:author="Someone"/></w:rPr>',
    '<w:pPrChange w:id="5" w:author="Someone"><w:pPr/></w:pPrChange>',
    '<w:numPr><w:ins w:id="5" w:author="Someone"/></w:numPr>',
    "<w:unknown/>",
])
def test_insert_refuses_unsafe_properties_without_mutation(position, props):
    document = _document(f"<w:p><w:pPr>{props}</w:pPr>{_r('Anchor')}</w:p>")
    anchor = next(document.iter(w("p")))
    original = etree.tostring(document)
    allocator = _revision_id_allocator(document)
    with pytest.raises(ApplyError) as exc:
        insert_paragraph(anchor, position, "New", AUTHOR, allocator)
    assert exc.value.code == "paragraph_structure_unsupported"
    assert etree.tostring(document) == original
    assert allocator.next_value == 101


@pytest.mark.parametrize("kind", ["comment", "bookmark"])
@pytest.mark.parametrize("position", ["before", "after"])
def test_insert_refuses_to_extend_cross_paragraph_ranges(kind, position):
    start = "commentRangeStart" if kind == "comment" else "bookmarkStart"
    end = "commentRangeEnd" if kind == "comment" else "bookmarkEnd"
    document = _document(
        f'<w:p><w:{start} w:id="8"/>{_r("Start")}</w:p>'
        f'<w:p>{_r("Anchor")}</w:p>'
        f'<w:p>{_r("End")}<w:{end} w:id="8"/></w:p>'
    )
    anchor = list(document.iter(w("p")))[1]
    original = etree.tostring(document)
    with pytest.raises(ApplyError) as exc:
        insert_paragraph(anchor, position, "New", AUTHOR, _revision_id_allocator(document))
    assert exc.value.code == "paragraph_boundary_unsupported"
    assert etree.tostring(document) == original


@pytest.mark.parametrize("wrapper", [
    "<w:r><w:pict><w:txbxContent><w:p>{}</w:p></w:txbxContent></w:pict></w:r>",
    "<w:pPr>{}</w:pPr>",
    "<w:unknown>{}</w:unknown>",
    "<w:p>{}</w:p>",
])
@pytest.mark.parametrize("kind", ["comment", "bookmark"])
def test_hidden_endpoint_cannot_close_a_real_main_story_range(wrapper, kind):
    start = "commentRangeStart" if kind == "comment" else "bookmarkStart"
    end = "commentRangeEnd" if kind == "comment" else "bookmarkEnd"
    hidden = wrapper.format(f'<w:{end} w:id="8"/>')
    document = _document(
        f'<w:p><w:{start} w:id="8"/>{_r("Start")}{hidden}</w:p>'
        f'<w:p>{_r("Anchor")}</w:p>'
        f'<w:p>{_r("End")}<w:{end} w:id="8"/></w:p>'
    )
    anchor = document.find(w("body"))[1]
    original = etree.tostring(document)
    with pytest.raises(ApplyError) as exc:
        insert_paragraph(anchor, "before", "New", AUTHOR, _revision_id_allocator(document))
    assert exc.value.code == "paragraph_boundary_unsupported"
    assert etree.tostring(document) == original


@pytest.mark.parametrize("wrapper", [
    "<w:r><w:pict><w:txbxContent><w:p>{}</w:p></w:txbxContent></w:pict></w:r>",
    "<w:pPr>{}</w:pPr>",
    "<w:unknown>{}</w:unknown>",
])
def test_hidden_start_does_not_create_a_false_main_story_range(wrapper):
    hidden = wrapper.format('<w:bookmarkStart w:id="8" w:name="Hidden"/>')
    document = _document(f'<w:p>{_r("Earlier")}{hidden}</w:p><w:p>{_r("Anchor")}</w:p>')
    anchor = document.find(w("body"))[1]
    inserted, _ = insert_paragraph(
        anchor, "before", "New", AUTHOR, _revision_id_allocator(document)
    )
    assert _reading(inserted) == "New"


@pytest.mark.parametrize("position", ["before", "after"])
def test_insert_outside_a_wholly_anchored_comment_preserves_comment_range(position):
    document = _document(
        f'<w:p><w:commentRangeStart w:id="8"/>{_r("Anchor")}'
        '<w:commentRangeEnd w:id="8"/><w:r><w:commentReference w:id="8"/></w:r></w:p>'
    )
    anchor = next(document.iter(w("p")))
    inserted, _ = insert_paragraph(anchor, position, "New", AUTHOR, _revision_id_allocator(document))
    assert len(list(document.iter(w("commentRangeStart")))) == 1
    assert len(list(document.iter(w("commentRangeEnd")))) == 1
    assert not list(inserted.iter(w("commentReference")))


def test_insert_two_after_paragraphs_in_sequence_rejects_all_original_breaks():
    document = _document(f"<w:p>{_r('Anchor')}</w:p>")
    anchor = next(document.iter(w("p")))
    allocator = _revision_id_allocator(document)
    first, _ = insert_paragraph(anchor, "after", "First", AUTHOR, allocator)
    insert_paragraph(first, "after", "Second", AUTHOR, allocator)
    assert _projected_texts(document, True) == ["Anchor", "First", "Second"]
    assert _projected_texts(document, False) == ["Anchor"]


def test_insert_before_an_own_marked_anchor_keeps_existing_break_identity_once():
    document = _document(f"<w:p>{_r('Anchor')}</w:p>")
    anchor = next(document.iter(w("p")))
    allocator = _revision_id_allocator(document)
    _, first_facts = insert_paragraph(anchor, "after", "After", AUTHOR, allocator)
    original_anchor = etree.tostring(anchor)
    inserted, _ = insert_paragraph(anchor, "before", "Before", AUTHOR, allocator)
    assert etree.tostring(anchor) == original_anchor
    assert not any(
        mark.get(w("id")) == first_facts["paragraph_mark_revision_id"]
        for mark in inserted.iter(w("ins"))
    )
    assert _projected_texts(document, True) == ["Before", "Anchor", "After"]
    assert _projected_texts(document, False) == ["Anchor"]


def test_replace_plain_whole_paragraph_tracks_original_styles_and_retains_properties():
    paragraph = _p(
        '<w:pPr><w:jc w:val="both"/></w:pPr>'
        + _r("Old ", "<w:b/>") + _r("terms.", "<w:i/>")
    )
    props = etree.tostring(paragraph[0])
    facts = _replace(paragraph, "Old terms.", "New clause.")
    assert _reading(paragraph) == "New clause."
    assert etree.tostring(paragraph[0]) == props
    deletion = paragraph.find(w("del"))
    assert [atom.text for atom in deletion.iter(w("delText"))] == ["Old ", "terms."]
    assert deletion[0].find(f"{w('rPr')}/{w('b')}") is not None
    assert deletion[1].find(f"{w('rPr')}/{w('i')}") is not None
    assert [step["operation"] for step in facts["steps"]] == ["delete", "insert"]
    assert _projected_texts(paragraph, False) == ["Old terms."]
    assert _projected_texts(paragraph, True) == ["New clause."]


@pytest.mark.parametrize("new_text", ["New clause.", ""])
def test_mixed_plain_own_and_existing_deletions_have_independent_step_proof(new_text):
    paragraph = _p(
        '<w:pPr><w:jc w:val="both"/></w:pPr>'
        + _r("Plain ") + _ins("own ", "24") + _del("hidden ", "25")
        + _r("and ") + _ins("proposal.", "26")
    )
    hidden = etree.tostring(paragraph.find(w("del")))
    props = etree.tostring(paragraph.find(w("pPr")))
    facts = _replace(paragraph, "Plain own and proposal.", new_text)
    assert _reading(paragraph) == new_text
    assert etree.tostring(paragraph.find(w("pPr"))) == props
    assert etree.tostring(next(n for n in paragraph.findall(w("del")) if n.get(w("id")) == "25")) == hidden
    assert not any(n.get(w("id")) in {"24", "26"} for n in paragraph.findall(w("ins")))
    assert len(facts["steps"]) == 4 + bool(new_text)
    # A rejected proposal restores original accepted text and the earlier hidden
    # foreign deletion. The owner's superseded pending insertions stay removed.
    assert _projected_texts(paragraph, False) == ["Plain hidden and "]
    assert _projected_texts(paragraph, True) == [new_text]
    assert not paragraph.findall(f"{w('pPr')}/{w('rPr')}/{w('del')}")


def test_repeated_whole_own_insertion_edit_keeps_id_date_and_hidden_deletion():
    paragraph = _p(_del("Prior ") + _ins("Own proposal."))
    original_attrs = dict(paragraph.find(w("ins")).attrib)
    original_hidden = etree.tostring(paragraph.find(w("del")))
    allocator = _revision_id_allocator(paragraph)
    _replace(paragraph, "Own proposal.", "Revised proposal.", allocator)
    _replace(paragraph, "Revised proposal.", "Final proposal.", allocator)
    assert dict(paragraph.find(w("ins")).attrib) == original_attrs
    assert etree.tostring(paragraph.find(w("del"))) == original_hidden
    assert allocator.next_value == 101
    _replace(paragraph, "Final proposal.", "", allocator)
    assert not paragraph.findall(w("ins"))
    assert _reading(paragraph) == ""


@pytest.mark.parametrize("position", ["before", "after"])
def test_inserted_paragraph_can_be_replaced_repeatedly_without_losing_mark(position):
    document = _document(f"<w:p>{_r('Anchor')}</w:p>")
    anchor = next(document.iter(w("p")))
    allocator = _revision_id_allocator(document)
    inserted, _ = insert_paragraph(anchor, position, "Initial", AUTHOR, allocator)
    _replace(inserted, "Initial", "Updated", allocator)
    _replace(inserted, "Updated", "Final", allocator)
    assert _projected_texts(document, False) == ["Anchor"]
    assert _projected_texts(document, True) == (
        ["Final", "Anchor"] if position == "before" else ["Anchor", "Final"]
    )
    assert allocator.next_value == 103


def test_replace_empty_paragraph_and_clear_it_retains_paragraph_mark():
    paragraph = _p('<w:pPr><w:rPr><w:i/></w:rPr></w:pPr>')
    original = etree.tostring(paragraph)
    _replace(paragraph, "", "New")
    assert paragraph.find(f"{w('ins')}/{w('r')}/{w('rPr')}/{w('i')}") is not None
    _replace(paragraph, "New", "")
    assert etree.tostring(paragraph) == original


def test_clear_and_refill_inserted_paragraph_retains_break_revision_only_once():
    document = _document(f"<w:p>{_r('Anchor')}</w:p>")
    anchor = next(document.iter(w("p")))
    allocator = _revision_id_allocator(document)
    inserted, facts = insert_paragraph(anchor, "before", "Initial", AUTHOR, allocator)
    mark = inserted.find(f"{w('pPr')}/{w('rPr')}/{w('ins')}")
    attrs = dict(mark.attrib)
    _replace(inserted, "Initial", "", allocator)
    _replace(inserted, "", "Refilled", allocator)
    assert dict(inserted.find(f"{w('pPr')}/{w('rPr')}/{w('ins')}").attrib) == attrs
    assert sum(
        node.get(w("id")) == facts["paragraph_mark_revision_id"]
        for node in document.iter(w("ins"))
    ) == 1
    assert _projected_texts(document, True) == ["Refilled", "Anchor"]
    assert _projected_texts(document, False) == ["Anchor"]


def test_replacement_preserves_whole_paragraph_range_anchors():
    paragraph = _p(
        '<w:commentRangeStart w:id="8"/><w:bookmarkStart w:id="9" w:name="Clause"/>'
        + _r("Original")
        + '<w:bookmarkEnd w:id="9"/><w:commentRangeEnd w:id="8"/>'
        '<w:r><w:commentReference w:id="8"/></w:r>'
    )
    _replace(paragraph, "Original", "Changed")
    assert paragraph[0].tag == w("commentRangeStart")
    assert paragraph[1].tag == w("bookmarkStart")
    assert paragraph[-3].tag == w("bookmarkEnd")
    assert paragraph[-2].tag == w("commentRangeEnd")
    assert paragraph[-1].find(w("commentReference")) is not None


@pytest.mark.parametrize("markup,old,error", [
    (_r("Current"), "wrong", "paragraph_text_mismatch"),
    (_r("Plain ") + _ins("foreign", author=OTHER), "Plain foreign", "foreign_insertion_unsupported"),
    (_r("one") + '<w:bookmarkStart w:id="8" w:name="inner"/>' + _r("two")
     + '<w:bookmarkEnd w:id="8"/>', "onetwo", "paragraph_boundary_unsupported"),
    ('<w:hyperlink w:anchor="here">' + _r("Text") + "</w:hyperlink>", "Text", "paragraph_structure_unsupported"),
    (_r("Plain") + '<w:ins w:id="2" w:author="Shilov, Ilya"><w:del w:id="3" w:author="Other">'
     '<w:r><w:delText>removed</w:delText></w:r></w:del>' + _r("own") + "</w:ins>",
     "Plainown", "overlaps_tracked_changes"),
])
def test_replacement_refusals_are_atomic(markup, old, error):
    paragraph = _p(markup)
    original = etree.tostring(paragraph)
    allocator = _revision_id_allocator(paragraph)
    with pytest.raises(ApplyError) as exc:
        _replace(paragraph, old, "Replacement", allocator)
    assert exc.value.code == error
    assert etree.tostring(paragraph) == original
    assert allocator.next_value == 101


def test_foreign_run_format_revision_on_plain_text_survives_in_deletion():
    formatting = (
        '<w:b/><w:rPrChange w:id="11" w:author="Synthetic Counterparty">'
        "<w:rPr><w:i/></w:rPr></w:rPrChange>"
    )
    paragraph = _p(_r("Original", formatting))
    original = etree.tostring(paragraph.find(f"{w('r')}/{w('rPr')}"))
    _replace(paragraph, "Original", "Changed")
    assert etree.tostring(paragraph.find(f"{w('del')}/{w('r')}/{w('rPr')}")) == original
    assert not list(paragraph.find(w("ins")).iter(w("rPrChange")))


def test_own_mixed_insertion_format_history_refused_not_silently_discarded():
    paragraph = _p(
        _r("Plain ")
        + '<w:ins w:id="24" w:author="Shilov, Ilya">'
        + _r("own", '<w:rPrChange w:id="25" w:author="Other"><w:rPr/></w:rPrChange>')
        + "</w:ins>"
    )
    original = etree.tostring(paragraph)
    with pytest.raises(ApplyError) as exc:
        _replace(paragraph, "Plain own", "Changed")
    assert exc.value.code == "foreign_formatting_unsupported"
    assert etree.tostring(paragraph) == original


@pytest.mark.parametrize("operation", ["insert", "replace"])
def test_allocator_exhaustion_is_atomic_even_after_candidate_step(operation):
    document = _document(f"<w:p>{_r('Original')}</w:p>")
    paragraph = next(document.iter(w("p")))
    original = etree.tostring(document)
    allocator = _RevisionIdAllocator(MAX_REVISION_ID)
    with pytest.raises(ApplyError) as exc:
        if operation == "insert":
            insert_paragraph(paragraph, "after", "New", AUTHOR, allocator)
        else:
            _replace(paragraph, "Original", "Changed", allocator)
    assert exc.value.code == "revision_id_exhausted"
    assert etree.tostring(document) == original
    assert allocator.next_value == MAX_REVISION_ID


def test_independent_step_rejection_rolls_back_everything():
    paragraph = _p(_r("Plain ") + _ins("own"))
    original = etree.tostring(paragraph)
    allocator = _revision_id_allocator(paragraph)
    calls = []

    def refuse(before, after, mutable, facts):
        calls.append(facts)
        if len(calls) == 2:
            raise ApplyError("round_trip_failed", "synthetic independent veto")

    with pytest.raises(ApplyError):
        replace_paragraph(
            paragraph, "Plain own", "Changed", AUTHOR, allocator, verify_step=refuse
        )
    assert len(calls) == 2
    assert etree.tostring(paragraph) == original
    assert allocator.next_value == 101


def test_many_formatted_plain_runs_are_one_bounded_deletion_step():
    paragraph = _p("".join(_r("x", "<w:b/>" if index % 2 else "<w:i/>") for index in range(1000)))
    facts = _replace(paragraph, "x" * 1000, "Replacement")
    assert len(facts["steps"]) == 2
    assert len(paragraph.find(w("del"))) == 1000


def test_too_many_separated_steps_refuse_before_committing():
    paragraph = _p("".join(
        _r("x") + _ins("y", str(index + 1))
        for index in range(MAX_PARAGRAPH_EDIT_STEPS // 2 + 1)
    ))
    original = etree.tostring(paragraph)
    with pytest.raises(ApplyError) as exc:
        _replace(paragraph, "xy" * (MAX_PARAGRAPH_EDIT_STEPS // 2 + 1), "New")
    assert exc.value.code == "resource_limit_exceeded"
    assert etree.tostring(paragraph) == original


@pytest.mark.parametrize("text", ["a\nb", "a\rb", "a\tb", "\x00", None])
@pytest.mark.parametrize("operation", ["insert", "replace"])
def test_invalid_paragraph_text_refused_without_mutation(text, operation):
    document = _document(f"<w:p>{_r('Original')}</w:p>")
    paragraph = next(document.iter(w("p")))
    original = etree.tostring(document)
    allocator = _revision_id_allocator(document)
    with pytest.raises(ApplyError) as exc:
        if operation == "insert":
            insert_paragraph(paragraph, "after", text, AUTHOR, allocator)
        else:
            replace_paragraph(paragraph, "Original", text, AUTHOR, allocator)
    assert exc.value.code == "invalid_edit"
    assert etree.tostring(document) == original
    assert allocator.next_value == 101
