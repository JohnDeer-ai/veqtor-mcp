# SPDX-License-Identifier: Apache-2.0
"""Synthetic native-edit fixtures for the additive drafting text engine."""

import copy
from datetime import UTC

import pytest
from lxml import etree

from veqtor_docx._drafting_text import (
    annotation_date,
    parse_drafting_timestamp,
    replace_text,
)
from veqtor_docx._ooxml import w
from veqtor_docx.apply import (
    MAX_REVISION_ID,
    ApplyError,
    _match_delete_span,
    _paragraph_segments,
    _reading_text,
    _revision_id_allocator,
    _RevisionIdAllocator,
)

AUTHOR = "Shilov, Ilya"
OTHER = "Synthetic Counterparty"
NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _p(markup: str) -> etree._Element:
    return etree.fromstring(f'<w:p xmlns:w="{NS}">{markup}</w:p>')


def _r(text: str, style: str = "") -> str:
    props = f"<w:rPr>{style}</w:rPr>" if style else ""
    return f'<w:r>{props}<w:t xml:space="preserve">{text}</w:t></w:r>'


def _ins(markup: str, *, author: str = AUTHOR, revision_id: str = "24") -> str:
    return (
        f'<w:ins w:id="{revision_id}" w:author="{author}" '
        f'w:date="2026-01-02T03:04:05Z">{markup}</w:ins>'
    )


def _reading(paragraph: etree._Element) -> str:
    return _reading_text(_paragraph_segments(paragraph))


def _replace(paragraph: etree._Element, old: str, new: str) -> dict:
    return replace_text(paragraph, old, new, AUTHOR, _revision_id_allocator(paragraph))


def test_plain_substring_uses_native_revisions_and_retains_format_and_neighbours():
    paragraph = _p(
        '<w:pPr><w:jc w:val="left"/></w:pPr>'
        + _r("Before.", "<w:i/>")
        + _r(" Pay in 30 days.", '<w:b/><w:color w:val="123456"/>')
        + _r(" After.", "<w:u/>")
    )
    untouched = [etree.tostring(paragraph[i]) for i in (0, 1, 3)]
    result = _replace(paragraph, "30", "45")
    assert _reading(paragraph) == "Before. Pay in 45 days. After."
    assert result["operation"] == "replace"
    assert result["revision_ids"] == ["101", "102"]
    deletion = paragraph.find(w("del"))
    insertion = paragraph.find(w("ins"))
    assert deletion.get(w("author")) == insertion.get(w("author")) == AUTHOR
    assert deletion.find(f"{w('r')}/{w('delText')}").text == "30"
    assert insertion.find(f"{w('r')}/{w('t')}").text == "45"
    for node in (paragraph[2], deletion[0], insertion[0], paragraph[-2]):
        assert node.find(f"{w('rPr')}/{w('b')}") is not None
        assert node.find(f"{w('rPr')}/{w('color')}").get(w("val")) == "123456"
    assert [etree.tostring(paragraph[i]) for i in (0, 1, -1)] == untouched


def test_plain_span_across_styles_retains_deleted_styles_and_surviving_run_fragments():
    paragraph = _p(_r("prefix thirty", "<w:b/>") + _r(" days suffix", "<w:i/>"))
    _replace(paragraph, "thirty days", "forty days")
    deletion = paragraph.find(w("del"))
    assert [node.text for node in deletion.iter(w("delText"))] == ["thirty", " days"]
    assert deletion[0].find(f"{w('rPr')}/{w('b')}") is not None
    assert deletion[1].find(f"{w('rPr')}/{w('i')}") is not None
    assert paragraph[0].find(w("t")).text == "prefix "
    assert paragraph[-1].find(w("t")).text == " suffix"
    assert paragraph[-1].find(f"{w('rPr')}/{w('i')}") is not None
    assert _reading(paragraph) == "prefix forty days suffix"


def test_repeated_own_edits_preserve_wrapper_id_author_date_and_other_revisions():
    paragraph = _p(
        _ins(_r("Foreign proposal"), author=OTHER, revision_id="21")
        + _r(" | ")
        + _ins(_r("Payment in thirty days", "<w:b/>"))
        + _r(" end")
    )
    original_other = etree.tostring(paragraph[0])
    original_attrs = dict(paragraph[2].attrib)
    allocator = _revision_id_allocator(paragraph)
    for old, new in (
        ("thirty", "forty-five"),
        ("forty-five", "sixty"),
        ("in sixty", "within 60"),
    ):
        result = replace_text(paragraph, old, new, AUTHOR, allocator)
        assert result["operation"] == "edit_own_insertion"
        assert result["revision_ids"] == ["24"]
        assert allocator.next_value == 101
    own = paragraph[2]
    assert dict(own.attrib) == original_attrs
    assert len(list(own.iter(w("ins")))) == 1
    assert not list(own.iter(w("del")))
    assert all(run.find(f"{w('rPr')}/{w('b')}") is not None for run in own)
    assert _reading(paragraph) == "Foreign proposal | Payment within 60 days end"
    assert etree.tostring(paragraph[0]) == original_other


def test_own_span_across_runs_keeps_prefix_suffix_and_styles():
    paragraph = _p(_ins(_r("AAA old", "<w:b/>") + _r(" words ZZZ", "<w:i/>")))
    _replace(paragraph, "old words", "new")
    own = paragraph[0]
    assert [node.text for node in own.iter(w("t"))] == ["AAA ", "new", " ZZZ"]
    assert own[0].find(f"{w('rPr')}/{w('b')}") is not None
    assert own[1].find(f"{w('rPr')}/{w('b')}") is not None
    assert own[2].find(f"{w('rPr')}/{w('i')}") is not None


@pytest.mark.parametrize("extra", ["", _r(""), '<w:bookmarkEnd w:id="19"/>'])
def test_delete_entire_own_insertion_removes_empty_wrapper_preserving_other_nodes(
    extra,
):
    paragraph = _p(_r("before ") + _ins(_r("temporary") + extra) + _r(" after"))
    before_run = etree.tostring(paragraph[0])
    after_run = etree.tostring(paragraph[-1])
    result = _replace(paragraph, "temporary", "")
    assert result["operation"] == "edit_own_insertion"
    assert not list(paragraph.iter(w("ins")))
    assert not list(paragraph.iter(w("del")))
    assert etree.tostring(paragraph[0]) == before_run
    assert etree.tostring(paragraph[-1]) == after_run
    assert _reading(paragraph) == "before  after"
    if "bookmarkEnd" in extra:
        assert paragraph.find(w("bookmarkEnd")).get(w("id")) == "19"


def test_new_plain_revision_can_immediately_be_edited_again():
    paragraph = _p(_r("Payment due in 30 days."))
    _replace(paragraph, "30", "45")
    deletion_before = etree.tostring(paragraph.find(w("del")))
    original_own_attrs = dict(paragraph.find(w("ins")).attrib)
    _replace(paragraph, "45", "60")
    _replace(paragraph, "60", "")
    assert _reading(paragraph) == "Payment due in  days."
    assert etree.tostring(paragraph.find(w("del"))) == deletion_before
    assert not paragraph.findall(w("ins"))
    assert original_own_attrs[w("id")] == "102"


def test_plain_deletion_and_adjacent_own_changes_are_supported_without_mutating_old_revisions():
    paragraph = _p(_ins(_r("earlier ")) + _r("remove keep"))
    prior = etree.tostring(paragraph[0])
    result = _replace(paragraph, "remove", "")
    assert result["operation"] == "delete"
    assert result["revision_ids"] == ["101"]
    assert etree.tostring(paragraph[0]) == prior
    assert _reading(paragraph) == "earlier  keep"


@pytest.mark.parametrize("own", [False, True])
def test_comment_and_bookmark_anchors_outside_span_are_preserved(own):
    text = _r("Change this text.")
    if own:
        text = _ins(text)
    paragraph = _p(
        '<w:commentRangeStart w:id="8"/><w:bookmarkStart w:id="9" w:name="target"/>'
        + text
        + '<w:bookmarkEnd w:id="9"/><w:commentRangeEnd w:id="8"/>'
        '<w:r><w:commentReference w:id="8"/></w:r>'
    )
    outside = [etree.tostring(paragraph[i]) for i in (0, 1, -3, -2, -1)]
    _replace(paragraph, "this", "that")
    assert [etree.tostring(paragraph[i]) for i in (0, 1, -3, -2, -1)] == outside
    assert _reading(paragraph) == "Change that text."


@pytest.mark.parametrize(
    "old,new", [("days", "weeks"), ("thirty days", "45 days"), ("days", "")]
)
def test_counterparty_suffix_edit_keeps_their_proposal_and_exact_reading(old, new):
    paragraph = _p(
        _r("Pay ") + _ins(_r("thirty days", "<w:i/>"), author=OTHER) + _r(" later")
    )
    previous_attrs = dict(paragraph[1].attrib)
    result = _replace(paragraph, old, new)
    assert result["operation"] == "counter"
    assert dict(paragraph[1].attrib) == previous_attrs
    assert paragraph[1].find(w("del")).get(w("author")) == AUTHOR
    original = "".join(
        (node.text or "")
        for node in paragraph[1].iter()
        if node.tag in (w("t"), w("delText"))
    )
    assert original == "thirty days"
    assert _reading(paragraph) == ("Pay thirty days later").replace(old, new)
    if new:
        assert paragraph[2].find(f"{w('r')}/{w('rPr')}/{w('i')}") is not None


@pytest.mark.parametrize(
    "markup,old,new,code",
    [
        (_r("repeat repeat"), "repeat", "new", "delete_text_ambiguous"),
        (_r("aaaa"), "aa", "b", "delete_text_ambiguous"),
        (_r("one"), "missing", "two", "delete_text_not_found"),
        (
            _r("plain ") + _ins(_r("tracked")),
            "plain tracked",
            "new",
            "overlaps_tracked_changes",
        ),
        (
            _ins(_r("left")) + _ins(_r("right"), revision_id="25"),
            "leftright",
            "new",
            "overlaps_tracked_changes",
        ),
        (
            _ins(_r("words here"), author=OTHER),
            "words",
            "new",
            "counter_position_unsupported",
        ),
        (
            '<w:hyperlink w:anchor="here">' + _r("words") + "</w:hyperlink>",
            "words",
            "new",
            "overlaps_tracked_changes",
        ),
        (
            _r("first") + '<w:bookmarkStart w:id="2" w:name="middle"/>' + _r("second"),
            "firstsecond",
            "new",
            "unsupported_run_shape",
        ),
        (
            _r("first") + '<w:commentRangeStart w:id="2"/>' + _r("second"),
            "firstsecond",
            "new",
            "unsupported_run_shape",
        ),
        (
            _ins(_r("first") + '<w:commentRangeStart w:id="2"/>' + _r("second")),
            "firstsecond",
            "new",
            "unsupported_run_shape",
        ),
        (
            _ins(_r("first") + _ins(_r("second"), author=OTHER, revision_id="25")),
            "first",
            "new",
            "overlaps_tracked_changes",
        ),
        (
            _ins(
                _r("first")
                + '<w:del w:id="25" w:author="Other"><w:r><w:delText>hidden</w:delText></w:r></w:del>'
            ),
            "first",
            "new",
            "overlaps_tracked_changes",
        ),
        (
            "<w:r><w:t>first</w:t><w:tab/><w:t>second</w:t></w:r>",
            "first",
            "new",
            "unsupported_run_shape",
        ),
        (
            "<w:r><w:t>first</w:t><w:t>second</w:t></w:r>",
            "first",
            "new",
            "unsupported_run_shape",
        ),
        (
            _r(
                "first",
                '<w:rPrChange w:id="27" w:author="Other"/>',
            ),
            "first",
            "new",
            "unsupported_run_shape",
        ),
        (
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            + _r("result")
            + '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
            "result",
            "new",
            "unsupported_run_shape",
        ),
    ],
)
def test_unsafe_or_ambiguous_span_refuses_without_mutation(markup, old, new, code):
    paragraph = _p(markup)
    before = etree.tostring(paragraph)
    allocator = _revision_id_allocator(paragraph)
    next_id = allocator.next_value
    with pytest.raises(ApplyError) as error:
        replace_text(paragraph, old, new, AUTHOR, allocator)
    assert error.value.code == code
    assert etree.tostring(paragraph) == before
    assert allocator.next_value == next_id


@pytest.mark.parametrize(
    "old,new,author,code",
    [
        ("", "new", AUTHOR, "delete_text_missing"),
        (None, "new", AUTHOR, "delete_text_missing"),
        ("old", None, AUTHOR, "invalid_edit"),
        ("old", "old", AUTHOR, "invalid_edit"),
        ("old", "new\x00", AUTHOR, "invalid_edit"),
        ("old", "new\nline", AUTHOR, "invalid_edit"),
        ("old", "new\tcolumn", AUTHOR, "invalid_edit"),
        ("old", "new", " ", "invalid_author"),
        ("old", "new", None, "invalid_author"),
        ("old", "x" * 20001, AUTHOR, "resource_limit_exceeded"),
    ],
)
def test_invalid_inputs_leave_paragraph_and_ids_unchanged(old, new, author, code):
    paragraph = _p(_r("old"))
    before = etree.tostring(paragraph)
    allocator = _revision_id_allocator(paragraph)
    with pytest.raises(ApplyError) as error:
        replace_text(paragraph, old, new, author, allocator)
    assert error.value.code == code
    assert etree.tostring(paragraph) == before
    assert allocator.next_value == 101


def test_exhausted_allocator_does_not_modify_paragraph_or_consume_one_remaining_id():
    paragraph = _p(_r("old"))
    before = etree.tostring(paragraph)
    allocator = _RevisionIdAllocator(MAX_REVISION_ID)
    with pytest.raises(ApplyError, match="revision_id_exhausted"):
        replace_text(paragraph, "old", "new", AUTHOR, allocator)
    assert etree.tostring(paragraph) == before
    assert allocator.next_value == MAX_REVISION_ID


def test_own_edits_need_no_new_ids_and_do_not_change_legacy_refusal():
    paragraph = _p(_ins(_r("own wording")))
    original = copy.deepcopy(paragraph)
    allocator = _RevisionIdAllocator(MAX_REVISION_ID + 1)
    replace_text(paragraph, "wording", "text", AUTHOR, allocator)
    assert _reading(paragraph) == "own text"
    assert allocator.next_value == MAX_REVISION_ID + 1
    with pytest.raises(ApplyError, match="overlaps_tracked_changes"):
        _match_delete_span(original, "wording", AUTHOR)


def _cursor_paragraph(*, author=AUTHOR, name="_GoBack", end=True):
    return _p(
        _r("Before")
        + _ins(
            _r(" Aster-", "<w:b/>")
            + f'<w:bookmarkStart w:id="7" w:name="{name}"/>'
            + _r("Research", "<w:i/>"),
            author=author,
        )
        + ('<w:bookmarkEnd w:id="7"/>' if end else "")
        + _r(" after")
    )


def test_word_goback_inside_own_insertion_moves_to_replacement_start():
    paragraph = _cursor_paragraph()
    original_attrs = dict(paragraph.find(w("ins")).attrib)
    original_marker = etree.tostring(next(paragraph.iter(w("bookmarkStart"))))
    original_end = etree.tostring(paragraph.find(w("bookmarkEnd")))
    result = _replace(paragraph, "Aster-Research", "North Star")
    insertion = paragraph.find(w("ins"))
    assert dict(insertion.attrib) == original_attrs
    assert _reading(paragraph) == "Before North Star after"
    assert [child.tag for child in insertion] == [w("r"), w("bookmarkStart"), w("r")]
    assert insertion[0].find(w("t")).text == " "
    assert insertion[2].find(w("t")).text == "North Star"
    assert insertion[2].find(f"{w('rPr')}/{w('b')}") is not None
    assert etree.tostring(insertion[1]) == original_marker
    assert etree.tostring(paragraph.find(w("bookmarkEnd"))) == original_end
    assert result["relocated_bookmarks"] == [
        {"name": "_GoBack", "id": "7", "position": "replacement_start"}
    ]
    _replace(paragraph, "North Star", "Bright Horizon")
    assert _reading(paragraph) == "Before Bright Horizon after"
    assert dict(paragraph.find(w("ins")).attrib) == original_attrs
    assert len(list(paragraph.iter(w("bookmarkStart")))) == 1
    assert len(list(paragraph.iter(w("bookmarkEnd")))) == 1


def test_delete_entire_own_insertion_keeps_goback_pair_in_same_paragraph():
    paragraph = _cursor_paragraph()
    start_attrs = dict(next(paragraph.iter(w("bookmarkStart"))).attrib)
    end_attrs = dict(paragraph.find(w("bookmarkEnd")).attrib)
    result = _replace(paragraph, " Aster-Research", "")
    assert result["operation"] == "edit_own_insertion"
    assert not paragraph.findall(w("ins"))
    assert not list(paragraph.iter(w("del")))
    assert _reading(paragraph) == "Before after"
    assert dict(paragraph.find(w("bookmarkStart")).attrib) == start_attrs
    assert dict(paragraph.find(w("bookmarkEnd")).attrib) == end_attrs
    assert (
        paragraph.index(paragraph.find(w("bookmarkEnd")))
        == paragraph.index(paragraph.find(w("bookmarkStart"))) + 1
    )


@pytest.mark.parametrize(
    "shape",
    [
        "foreign",
        "ordinary",
        "orphan",
        "duplicate_start",
        "duplicate_end",
        "other_goback",
        "end_before_start",
        "cross_paragraph",
        "plain",
    ],
)
def test_goback_exception_does_not_weaken_other_boundary_guards(shape):
    paragraph = _cursor_paragraph(
        author=OTHER if shape == "foreign" else AUTHOR,
        name="OrdinaryRange" if shape == "ordinary" else "_GoBack",
        end=shape not in {"orphan", "cross_paragraph"},
    )
    if shape == "duplicate_start":
        duplicate = etree.SubElement(paragraph, w("bookmarkStart"))
        duplicate.set(w("id"), "7")
        duplicate.set(w("name"), "DuplicateIdentity")
    elif shape == "duplicate_end":
        etree.SubElement(paragraph, w("bookmarkEnd")).set(w("id"), "7")
    elif shape == "other_goback":
        duplicate = etree.SubElement(paragraph, w("bookmarkStart"))
        duplicate.set(w("id"), "8")
        duplicate.set(w("name"), "_GoBack")
        etree.SubElement(paragraph, w("bookmarkEnd")).set(w("id"), "8")
    elif shape == "end_before_start":
        paragraph.insert(0, paragraph.find(w("bookmarkEnd")))
    elif shape == "cross_paragraph":
        body = etree.Element(w("body"))
        body.append(paragraph)
        following = etree.SubElement(body, w("p"))
        etree.SubElement(following, w("bookmarkEnd")).set(w("id"), "7")
    elif shape == "plain":
        insertion = paragraph.find(w("ins"))
        for child in list(insertion):
            insertion.addprevious(child)
        paragraph.remove(insertion)
    before = etree.tostring(paragraph.getroottree())
    allocator = _revision_id_allocator(paragraph.getroottree().getroot())
    next_id = allocator.next_value
    with pytest.raises(ApplyError) as error:
        replace_text(paragraph, "Aster-Research", "North Star", AUTHOR, allocator)
    assert error.value.code == (
        "counter_position_unsupported"
        if shape == "foreign"
        else "unsupported_run_shape"
    )
    assert etree.tostring(paragraph.getroottree()) == before
    assert allocator.next_value == next_id


def _changed_style(identity="81", author=OTHER):
    return (
        '<w:b/><w:lang w:val="en-US"/>'
        f'<w:rPrChange w:id="{identity}" w:author="{author}" '
        'w:date="2026-01-01T01:02:03Z"><w:rPr><w:i/></w:rPr></w:rPrChange>'
    )


@pytest.mark.parametrize("counter", [False, True])
def test_whole_formatted_run_replacement_keeps_history_only_on_original_text(counter):
    content = _r("existing wording", _changed_style())
    paragraph = _p(_ins(content, author=OTHER) if counter else content)
    history = etree.tostring(next(paragraph.iter(w("rPrChange"))))
    result = _replace(paragraph, "existing wording", "revised wording")
    assert result["operation"] == ("counter" if counter else "replace")
    assert _reading(paragraph) == "revised wording"
    changes = list(paragraph.iter(w("rPrChange")))
    assert len(changes) == 1
    assert etree.tostring(changes[0]) == history
    deletion = next(paragraph.iter(w("del")))
    assert changes[0] in list(deletion.iter())
    assert [atom.text for atom in deletion.iter(w("delText"))] == ["existing wording"]
    assert not list(deletion.iter(w("t")))
    replacement = paragraph[-1]
    assert replacement.tag == w("ins")
    assert not list(replacement.iter(w("rPrChange")))
    assert replacement.find(f"{w('r')}/{w('rPr')}/{w('b')}") is not None
    assert replacement.find(f"{w('r')}/{w('rPr')}/{w('lang')}").get(w("val")) == "en-US"


def test_own_whole_formatted_run_can_be_edited_repeatedly_without_changing_history():
    paragraph = _p(_ins(_r("proposal", _changed_style(author=AUTHOR))))
    wrapper = dict(paragraph[0].attrib)
    history = etree.tostring(next(paragraph.iter(w("rPrChange"))))
    for old, new in [("proposal", "revision"), ("revision", "final draft")]:
        result = _replace(paragraph, old, new)
        assert result["operation"] == "edit_own_insertion"
        assert dict(paragraph[0].attrib) == wrapper
        assert [etree.tostring(node) for node in paragraph.iter(w("rPrChange"))] == [
            history
        ]
        assert not list(paragraph.iter(w("del")))
        assert _reading(paragraph) == new


@pytest.mark.parametrize("new", ["revision", ""])
@pytest.mark.parametrize("format_author", [OTHER, "Shilov,Ilya"])
def test_own_edit_refuses_foreign_format_history_without_mutation(new, format_author):
    paragraph = _p(_ins(_r("proposal", _changed_style(author=format_author))))
    before = etree.tostring(paragraph)
    allocator = _revision_id_allocator(paragraph)
    next_id = allocator.next_value
    with pytest.raises(ApplyError) as error:
        replace_text(paragraph, "proposal", new, AUTHOR, allocator)
    assert error.value.code == "foreign_formatting_unsupported"
    assert "another author's tracked formatting" in str(error.value)
    assert etree.tostring(paragraph) == before
    assert allocator.next_value == next_id


@pytest.mark.parametrize("own", [False, True])
def test_partial_formatted_run_edit_refuses_without_duplicating_format_revision(own):
    content = _r("old words here", _changed_style(author=AUTHOR if own else OTHER))
    paragraph = _p(_ins(content) if own else content)
    before = etree.tostring(paragraph)
    allocator = _revision_id_allocator(paragraph)
    next_id = allocator.next_value
    with pytest.raises(ApplyError, match="unsupported_run_shape"):
        replace_text(paragraph, "words", "text", AUTHOR, allocator)
    assert etree.tostring(paragraph) == before
    assert allocator.next_value == next_id


@pytest.mark.parametrize("old,new", [("one", ""), ("onetwo", "replacement")])
def test_own_edit_cannot_discard_format_history(old, new):
    paragraph = _p(_ins(
        _r("one", _changed_style(author=AUTHOR))
        + _r("two", _changed_style("82", author=AUTHOR))
    ))
    before = etree.tostring(paragraph)
    with pytest.raises(ApplyError, match="unsupported_run_shape"):
        _replace(paragraph, old, new)
    assert etree.tostring(paragraph) == before


def test_unrelated_formatted_run_stays_identical_during_own_edit():
    paragraph = _p(_ins(_r("history", _changed_style()) + _r(" change")))
    history_run = etree.tostring(paragraph[0][0])
    _replace(paragraph, "change", "edit")
    assert etree.tostring(paragraph[0][0]) == history_run
    assert _reading(paragraph) == "history edit"


@pytest.mark.parametrize("own", [False, True])
@pytest.mark.parametrize("position", ["before", "after", "both"])
def test_partial_edit_preserves_page_cache_markers_once(own, position):
    marker = "<w:lastRenderedPageBreak/>"
    run = "<w:r><w:rPr><w:b/></w:rPr>"
    run += marker if position in {"before", "both"} else ""
    run += "<w:t>prefix old suffix</w:t>"
    run += marker if position in {"after", "both"} else ""
    run += "</w:r>"
    paragraph = _p(_ins(run) if own else run)
    count = len(list(paragraph.iter(w("lastRenderedPageBreak"))))
    _replace(paragraph, "old", "new wording")
    assert _reading(paragraph) == "prefix new wording suffix"
    assert len(list(paragraph.iter(w("lastRenderedPageBreak")))) == count
    assert all(
        node.getparent().tag == w("r")
        for node in paragraph.iter(w("lastRenderedPageBreak"))
    )
    for deletion in paragraph.iter(w("del")):
        assert not list(deletion.iter(w("t")))


@pytest.mark.parametrize("own", [False, True])
@pytest.mark.parametrize("new", ["replacement", ""])
def test_whole_span_with_page_markers_keeps_markers_when_text_is_removed(own, new):
    content = (
        "<w:r><w:lastRenderedPageBreak/><w:t>one</w:t></w:r>"
        "<w:r><w:lastRenderedPageBreak/></w:r>"
        "<w:r><w:t>two</w:t><w:lastRenderedPageBreak/></w:r>"
    )
    paragraph = _p(_ins(content) if own else content)
    _replace(paragraph, "onetwo", new)
    assert _reading(paragraph) == new
    assert len(list(paragraph.iter(w("lastRenderedPageBreak")))) == 3
    assert all(
        node.getparent().tag == w("r")
        for node in paragraph.iter(w("lastRenderedPageBreak"))
    )
    if own and not new:
        assert not list(paragraph.iter(w("ins")))
    for deletion in paragraph.iter(w("del")):
        assert not list(deletion.iter(w("t")))


@pytest.mark.parametrize("timestamp", [
    "2026-10-16T12:34:56Z", "2026-10-16T15:34:56+03:00",
    "2026-10-16T07:34:56-05:00", "2026-10-16T18:04:56+05:30",
])
def test_dates_are_added_to_new_revisions_but_existing_own_date_is_preserved(timestamp):
    paragraph = _p(_r("original"))
    replace_text(
        paragraph,
        "original",
        "first draft",
        AUTHOR,
        _revision_id_allocator(paragraph),
        timestamp=timestamp,
    )
    assert [node.get(w("date")) for node in paragraph] == [timestamp[:19], timestamp[:19]]
    original_attrs = dict(paragraph[-1].attrib)
    replace_text(
        paragraph,
        "first draft",
        "revised draft",
        AUTHOR,
        _revision_id_allocator(paragraph),
        timestamp="2026-10-17T01:02:03-05:00",
    )
    assert dict(paragraph[-1].attrib) == original_attrs
    assert _reading(paragraph) == "revised draft"


def test_foreign_insertion_counter_always_marks_deleted_text_as_deltext():
    paragraph = _p(_ins(_r("one ") + _r("two", "<w:i/>"), author=OTHER))
    replace_text(
        paragraph,
        "one two",
        "three",
        AUTHOR,
        _revision_id_allocator(paragraph),
        timestamp="2026-10-16T12:34:56Z",
    )
    deletion = paragraph[0].find(w("del"))
    assert deletion is not None
    assert not list(deletion.iter(w("t")))
    assert "".join(atom.text for atom in deletion.iter(w("delText"))) == "one two"
    assert (
        deletion.get(w("date")) == paragraph[1].get(w("date")) == "2026-10-16T12:34:56"
    )
    assert _reading(paragraph) == "three"


def test_annotation_date_retains_captured_wall_clock_without_losing_proof_instant():
    from datetime import datetime

    stamp = "2026-10-17T00:05:00+05:30"
    assert annotation_date(stamp) == "2026-10-17T00:05:00"
    assert parse_drafting_timestamp(stamp).astimezone(UTC) == datetime(
        2026, 10, 16, 18, 35, tzinfo=UTC
    )


@pytest.mark.parametrize("timestamp", [
    "2026-02-30T00:00:00Z", "2026-10-16T10:00:00",
    "2026-10-16T10:00:00+14:01", "2026-10-16T10:00:00-15:00", 1,
])
def test_invalid_revision_timestamp_refuses_without_mutating_text(timestamp):
    paragraph = _p(_r("original"))
    original = etree.tostring(paragraph)
    allocator = _revision_id_allocator(paragraph)
    next_id = allocator.next_value
    with pytest.raises(ApplyError, match="invalid_edit"):
        replace_text(paragraph, "original", "replacement", AUTHOR, allocator, timestamp=timestamp)
    assert etree.tostring(paragraph) == original
    assert allocator.next_value == next_id


def test_nested_foreign_insertion_is_never_wrapped_wholesale_as_a_deletion():
    paragraph = _p(
        _ins(_r("outer ") + _ins(_r("nested"), author=OTHER, revision_id="25"))
    )
    before = etree.tostring(paragraph)
    with pytest.raises(ApplyError, match="overlaps_tracked_changes"):
        _replace(paragraph, "outer nested", "new")
    assert etree.tostring(paragraph) == before
