# SPDX-License-Identifier: Apache-2.0
"""Independent literal labels for whole bilingual row alternatives."""

import copy

from lxml import etree
import pytest

from veqtor_docx._ooxml import W_NS, w
from veqtor_docx.apply import ApplyError
from veqtor_docx.table_row_numbering import numbering_effects_for_rows


def _xml(text, root="document"):
    return etree.fromstring(f'<w:{root} xmlns:w="{W_NS}">{text}</w:{root}>')


def _p(text="Text", num=None, style=None, level=0):
    props = f'<w:pStyle w:val="{style}"/>' if style else ""
    if num is not None:
        props += f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num}"/></w:numPr>'
    return f'<w:p><w:pPr>{props}</w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _row(left="Left", right="Right", nums=(None, None), styles=(None, None), row_props="", cell_props=""):
    return (f'<w:tr><w:trPr>{row_props}</w:trPr>'
            + ''.join(f'<w:tc><w:tcPr>{cell_props}</w:tcPr>{_p(text, num, style)}</w:tc>'
                      for text, num, style in zip((left, right), nums, styles, strict=True))
            + '</w:tr>')


def _doc(rows, before="", after=""):
    return _xml(f'<w:body>{before}<w:tbl>{"".join(rows)}</w:tbl>{after}</w:body>')


def _parts(styles=None, extra="", right_restart=7, *, shared=False):
    definitions = '''
      <w:abstractNum w:abstractNumId="9"><w:multiLevelType w:val="singleLevel"/>
        <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>
          <w:lvlText w:val="%1."/></w:lvl></w:abstractNum>
      <w:num w:numId="1"><w:abstractNumId w:val="9"/></w:num>
      <w:num w:numId="2"><w:abstractNumId w:val="RIGHT_ABSTRACT"/>
        <w:lvlOverride w:ilvl="0"><w:startOverride w:val="RESTART"/></w:lvlOverride></w:num>
    '''.replace("RESTART", str(right_restart)).replace("RIGHT_ABSTRACT", "9" if shared else "10")
    if not shared:
        definitions += '''<w:abstractNum w:abstractNumId="10"><w:multiLevelType w:val="singleLevel"/>
          <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>
            <w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'''
    parts = {"word/numbering.xml": etree.tostring(_xml(definitions + extra, "numbering"))}
    if styles is not None:
        parts["word/styles.xml"] = etree.tostring(_xml(styles, "styles"))
    return parts


def _records(result, perspective):
    return [(item["paragraph_index"], item["label"]) for item in result[perspective]]


def _refuses(parts, longer, shorter, removed, reason):
    with pytest.raises(ApplyError) as error:
        numbering_effects_for_rows(parts, longer, shorter, removed)
    assert error.value.code == "paragraph_numbering_unsupported"
    assert error.value.metadata["reason"] == reason


@pytest.mark.parametrize("position", [0, 1, 2])
def test_first_middle_last_row_proves_independent_languages_and_restart(position):
    rows = [_row(f"L{i}", f"R{i}", (1, 2)) for i in range(3)]
    longer = _doc(rows)
    shorter = _doc([row for index, row in enumerate(rows) if index != position])
    original = (etree.tostring(longer), etree.tostring(shorter))
    result = numbering_effects_for_rows(_parts(), longer, shorter, [position * 2, position * 2 + 1])
    expected_before = [(0, "1."), (1, "7."), (2, "2."), (3, "8."), (4, "3."), (5, "9.")]
    expected_after = [(0, "1."), (1, "7."), (2, "2."), (3, "8.")]
    # Later affected instances are all shown; the immediate preceding row is
    # shown, while earlier untouched rows need not be repeated.
    assert _records(result, "before") == expected_before[max(0, (position - 1) * 2):]
    assert _records(result, "after") == expected_after[max(0, (position - 1) * 2):]
    assert original == (etree.tostring(longer), etree.tostring(shorter))


def test_shared_instance_counts_both_columns_in_main_story_order():
    rows = [_row(str(index), str(index), (1, 1)) for index in range(3)]
    result = numbering_effects_for_rows(_parts(), _doc(rows), _doc([rows[0], rows[2]]), [2, 3])
    assert _records(result, "before") == list(enumerate(["1.", "2.", "3.", "4.", "5.", "6."]))
    assert _records(result, "after") == list(enumerate(["1.", "2.", "3.", "4."]))


def test_original_alternating_shared_abstract_control_refuses_instead_of_false_independent_labels():
    # This preserves the original synthetic shape that native Word showed as
    # 1/7, 8/9, 10/11. Distinct-abstract positive tests above are separate inputs.
    rows = [_row(f"L{index}", f"R{index}", (1, 2)) for index in range(3)]
    _refuses(_parts(shared=True), _doc(rows), _doc([rows[0], rows[2]]), [2, 3], "interleaved_numbering_instances")


def test_removing_only_restart_row_cannot_hide_later_return_to_other_instance():
    rows = [_row(nums=(1, None)), _row(nums=(2, None)), _row(nums=(1, None))]
    _refuses(_parts(shared=True), _doc(rows), _doc([rows[0], rows[2]]), [2, 3], "interleaved_numbering_instances")


def test_consecutive_shared_family_restart_reports_later_other_instance_effects():
    rows = [_row(nums=(1, None)), _row(nums=(1, None)), _row(nums=(2, None)), _row(nums=(2, None))]
    result = numbering_effects_for_rows(_parts(shared=True), _doc(rows), _doc(rows[1:]), [0, 1])
    assert _records(result, "before") == [(0, "1."), (1, None), (2, "2."), (3, None), (4, "7."), (6, "8.")]
    assert _records(result, "after") == [(0, "1."), (1, None), (2, "7."), (4, "8.")]


@pytest.mark.parametrize("marker", ["ins", "del"])
def test_other_pending_instance_in_same_family_cannot_hide_counter_dependency(marker):
    pending = _row(nums=(2, None), row_props=f'<w:{marker} w:id="4"/>')
    rows = [_row(nums=(1, None)), _row(nums=(1, None)), pending]
    _refuses(_parts(shared=True), _doc(rows), _doc(rows[1:]), [0, 1], "pending_table_structure")


def test_same_family_pending_predecessor_outside_local_rows_is_still_a_dependency():
    pending = '<w:tbl>' + _row(nums=(1, None), row_props='<w:ins w:id="4"/>') + '</w:tbl>'
    rows = [_row(nums=(2, None)), _row(nums=(2, None)), _row(nums=(2, None))]
    _refuses(_parts(shared=True), _doc(rows, pending), _doc(rows[1:], pending), [2, 3], "pending_table_structure")


def test_numbered_left_and_unnumbered_empty_right_stay_asymmetric():
    rows = [_row(f"L{index}", "", (1, None)) for index in range(3)]
    result = numbering_effects_for_rows(_parts(), _doc(rows), _doc(rows[1:]), [0, 1])
    assert [item["label"] for item in result["before"]] == ["1.", None, "2.", None, "3."]
    assert [item["label"] for item in result["after"]] == ["1.", None, "2."]


def test_different_inherited_styles_keep_their_own_numbering_instances():
    styles = '''
      <w:style w:type="paragraph" w:styleId="LeftBase"><w:pPr><w:numPr>
        <w:numId w:val="1"/></w:numPr></w:pPr></w:style>
      <w:style w:type="paragraph" w:styleId="Left"><w:basedOn w:val="LeftBase"/></w:style>
      <w:style w:type="paragraph" w:styleId="Right"><w:pPr><w:numPr>
        <w:numId w:val="2"/></w:numPr></w:pPr></w:style>
    '''
    rows = [_row(styles=("Left", "Right")) for _ in range(3)]
    result = numbering_effects_for_rows(_parts(styles), _doc(rows), _doc(rows[:2]), [4, 5])
    assert _records(result, "before") == [(2, "2."), (3, "8."), (4, "3."), (5, "9.")]
    assert result["before"][0]["style_chain"] == ["LeftBase", "Left"]
    assert result["before"][1]["start_source"] == "start_override"


def test_global_counter_before_and_after_table_is_preserved_and_reported():
    rows = [_row(nums=(1, None)) for _ in range(3)]
    before = _p("Earlier", 1)
    after = _p("Later", 1)
    result = numbering_effects_for_rows(_parts(), _doc(rows, before, after), _doc([rows[0], rows[2]], before, after), [3, 4])
    assert _records(result, "before") == [(1, "2."), (2, None), (3, "3."), (4, None), (5, "4."), (6, None), (7, "5.")]
    assert _records(result, "after") == [(1, "2."), (2, None), (3, "3."), (4, None), (5, "4.")]


@pytest.mark.parametrize("separator_first", [False, True])
def test_selected_clause_and_explicit_unnumbered_separator_are_one_projection(separator_first):
    clause = _row("New left", "New right", (1, 2))
    separator = _row("", "", (0, 0))
    selected = [separator, clause] if separator_first else [clause, separator]
    rows = [_row("Before", "Before", (1, 2)), *selected, _row("After", "After", (1, 2))]
    result = numbering_effects_for_rows(_parts(), _doc(rows), _doc([rows[0], rows[3]]), [2, 3, 4, 5])
    expected = ["1.", "7."] + ([None, None, "2.", "8."] if separator_first else ["2.", "8.", None, None]) + ["3.", "9."]
    assert [item["label"] for item in result["before"]] == expected
    assert [item["label"] for item in result["after"]] == ["1.", "7.", "2.", "8."]


def test_unselected_separator_stays_in_survivor_mapping_without_number():
    rows = [_row(nums=(1, 2)), _row("", "", (0, 0)), _row(nums=(1, 2)), _row(nums=(1, 2))]
    result = numbering_effects_for_rows(_parts(), _doc(rows), _doc(rows[:2] + rows[3:]), [4, 5])
    assert _records(result, "before") == [(2, None), (3, None), (4, "2."), (5, "8."), (6, "3."), (7, "9.")]
    assert _records(result, "after") == [(2, None), (3, None), (4, "2."), (5, "8.")]


def test_unnumbered_direct_suppression_wins_over_numbered_default_style():
    styles = '''<w:style w:type="paragraph" w:styleId="Default" w:default="1">
      <w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style>'''
    rows = [_row(nums=(0, 0)) for _ in range(3)]
    result = numbering_effects_for_rows(_parts(styles), _doc(rows), _doc(rows[:2]), [4, 5])
    assert [item["label"] for item in result["before"]] == [None] * 4
    assert [item["label"] for item in result["after"]] == [None] * 2


def test_independent_unknown_format_does_not_block_affected_sequence():
    foreign = '''<w:abstractNum w:abstractNumId="8"><w:lvl w:ilvl="0">
      <w:start w:val="1"/><w:numFmt w:val="not-known"/><w:lvlText w:val="%1"/></w:lvl></w:abstractNum>
      <w:num w:numId="3"><w:abstractNumId w:val="8"/></w:num>'''
    rows = [_row(nums=(1, 3)) for _ in range(3)]
    # The selected right-hand column is explicitly unnumbered; foreign lists
    # in retained rows have no dependency on either selected cell's instance.
    rows[1] = _row(nums=(1, None))
    result = numbering_effects_for_rows(_parts(extra=foreign), _doc(rows, _p(num=3)), _doc([rows[0], rows[2]], _p(num=3)), [3, 4])
    assert _records(result, "before") == [(1, "1."), (3, "2."), (4, None), (5, "3.")]
    assert _records(result, "after") == [(1, "1."), (3, "2.")]


@pytest.mark.parametrize("marker", ["ins", "del"])
def test_pending_other_row_same_instance_refuses_instead_of_counting_it(marker):
    pending = _row(nums=(1, None), row_props=f'<w:{marker} w:id="4" w:author="Fictional reviewer"/>')
    rows = [pending, _row(nums=(1, None)), _row(nums=(1, None))]
    _refuses(_parts(), _doc(rows), _doc([rows[0], rows[2]]), [2, 3], "pending_table_structure")


@pytest.mark.parametrize("marker", ["cellIns", "cellDel", "cellMerge"])
def test_pending_cell_same_instance_refuses(marker):
    pending = _row(nums=(1, None), cell_props=f'<w:{marker} w:id="4" w:author="Fictional reviewer"/>')
    rows = [pending, _row(nums=(1, None)), _row(nums=(1, None))]
    _refuses(_parts(), _doc(rows), _doc([rows[0], rows[2]]), [2, 3], "pending_table_structure")


def test_pending_unrelated_instance_in_other_table_does_not_block():
    foreign = '<w:tbl>' + _row(nums=(2, 2), row_props='<w:ins w:id="4" w:author="Fictional reviewer"/>') + '</w:tbl>'
    rows = [_row(nums=(1, None)), _row(nums=(1, None)), _row(nums=(1, None))]
    result = numbering_effects_for_rows(_parts(), _doc(rows, foreign), _doc([rows[0], rows[2]], foreign), [4, 5])
    assert _records(result, "before") == [(2, "1."), (3, None), (4, "2."), (5, None), (6, "3."), (7, None)]


def test_later_pending_row_in_affected_instance_also_refuses():
    rows = [_row(nums=(1, None)), _row(nums=(1, None)), _row(nums=(1, None), row_props='<w:del w:id="4"/>')]
    _refuses(_parts(), _doc(rows), _doc(rows[1:]), [0, 1], "pending_table_structure")


def test_uncertain_earlier_style_cannot_silently_skip_an_affected_counter():
    rows = [_row(nums=(1, None)), _row(nums=(1, None)), _row(nums=(1, None))]
    prior = _p(style="MissingStyle")
    _refuses(_parts(), _doc(rows, prior), _doc(rows[1:], prior), [1, 2], "missing_or_duplicate_style")


def test_unsupported_selected_level_is_a_controlled_refusal():
    row = '<w:tr><w:tc>' + _p(num=1, level=1) + '</w:tc><w:tc>' + _p() + '</w:tc></w:tr>'
    _refuses(_parts(), _doc([row, _row()]), _doc([_row()]), [0, 1], "level_mismatch")


@pytest.mark.parametrize("indices", [[], [0, 0], [-1], [6], [False], ["0"], [[]]])
def test_invalid_indices_are_reported(indices):
    rows = [_row() for _ in range(3)]
    with pytest.raises(ApplyError) as error:
        numbering_effects_for_rows({}, _doc(rows), _doc(rows[:2]), indices)
    assert error.value.code == "invalid_edit"


def test_partial_row_removal_is_not_a_valid_numbering_projection():
    longer = _doc([_row(), _row()])
    shorter = copy.deepcopy(longer)
    paragraph = list(shorter.iter(w("p")))[0]
    paragraph.getparent().remove(paragraph)
    _refuses({}, longer, shorter, [0], "invalid_projection")


def test_nonadjacent_rows_are_not_inferred_as_one_unit():
    rows = [_row() for _ in range(3)]
    _refuses({}, _doc(rows), _doc([rows[1]]), [0, 1, 4, 5], "invalid_projection")


@pytest.mark.parametrize("change", ["count", "text", "style", "order"])
def test_retained_projection_must_match_every_survivor(change):
    rows = [_row("A", "B"), _row("C", "D"), _row("E", "F")]
    shorter = _doc([rows[0], rows[2]])
    if change == "count":
        shorter.find(w("body")).append(_xml(_p(), "block")[0])
    elif change == "text":
        list(shorter.iter(w("t")))[0].text = "Changed"
    elif change == "style":
        list(shorter.iter(w("pPr")))[0].append(_xml('<w:pStyle w:val="Changed"/>', "block")[0])
    else:
        row_nodes = list(shorter.iter(w("tr")))
        row_nodes[0].getparent().insert(0, row_nodes[1])
    _refuses({}, _doc(rows), shorter, [2, 3], "invalid_projection")


def test_selected_row_projection_cannot_still_contain_a_pending_mark():
    rows = [_row(row_props='<w:ins w:id="4"/>'), _row()]
    _refuses({}, _doc(rows), _doc(rows[1:]), [0, 1], "pending_table_structure")


def test_selected_paragraphs_cannot_span_different_tables():
    longer = _doc([_row()], after='<w:tbl>' + _row() + '</w:tbl>')
    shorter = _doc([])
    _refuses({}, longer, shorter, [0, 1, 2, 3], "invalid_projection")


def test_body_paragraphs_are_not_a_row_projection():
    longer = _doc([_row()], before=_p())
    _refuses({}, longer, _doc([_row()]), [0], "invalid_projection")
