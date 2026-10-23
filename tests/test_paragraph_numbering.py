# SPDX-License-Identifier: Apache-2.0
"""Literal numbering oracles for whole-paragraph clean projections."""

import copy

from lxml import etree
import pytest

from veqtor_docx._ooxml import W_NS, w
from veqtor_docx.apply import ApplyError
from veqtor_docx import paragraph_numbering as numbering


def _xml(content, root="document"):
    return etree.fromstring(f'<w:{root} xmlns:w="{W_NS}">{content}</w:{root}>')


def _paragraph(text="Clause", *, num=None, level=0, style=None, props=""):
    properties = f'<w:pStyle w:val="{style}"/>' if style else ""
    if num is not None:
        properties += (
            '<w:numPr>'
            + (f'<w:ilvl w:val="{level}"/>' if level is not None else "")
            + f'<w:numId w:val="{num}"/></w:numPr>'
        )
    properties += props
    return f'<w:p><w:pPr>{properties}</w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _document(*paragraphs):
    return _xml(f'<w:body>{"".join(paragraphs)}</w:body>')


def _level(start=1, fmt="decimal", text="%1.", *, extra="", ilvl=0):
    return (
        f'<w:lvl w:ilvl="{ilvl}"><w:start w:val="{start}"/>'
        f'<w:numFmt w:val="{fmt}"/><w:lvlText w:val="{text}"/>{extra}</w:lvl>'
    )


def _parts(*, level=None, instances=None, abstract_extra="", styles=None, extra=""):
    abstract = (
        '<w:abstractNum w:abstractNumId="9"><w:multiLevelType w:val="singleLevel"/>'
        f'{level or _level()}{abstract_extra}</w:abstractNum>'
    )
    instances = instances or '<w:num w:numId="1"><w:abstractNumId w:val="9"/></w:num>'
    result = {"word/numbering.xml": etree.tostring(_xml(abstract + instances + extra, "numbering"))}
    if styles is not None:
        result["word/styles.xml"] = etree.tostring(_xml(styles, "styles"))
    return result


def _style(identifier, *, base=None, num=None, level=None, extra="", default=False):
    return (
        f'<w:style w:styleId="{identifier}" w:type="paragraph"'
        + (' w:default="1"' if default else "") + ">"
        + (f'<w:basedOn w:val="{base}"/>' if base else "")
        + '<w:pPr>'
        + ('<w:numPr>'
           + (f'<w:numId w:val="{num}"/>' if num is not None else "")
           + (f'<w:ilvl w:val="{level}"/>' if level is not None else "")
           + '</w:numPr>' if num is not None or level is not None else "")
        + '</w:pPr>' + extra + '</w:style>'
    )


def _labels(parts, root, indices):
    return [item["label"] for item in numbering.resolve_body_numbering(parts, root, indices)]


def _refusal(parts, root, indices, reason):
    with pytest.raises(ApplyError) as raised:
        numbering.resolve_body_numbering(parts, root, indices)
    assert raised.value.code == "paragraph_numbering_unsupported"
    assert raised.value.metadata["reason"] == reason


def test_plain_paragraph_is_explicitly_unumbered_without_parts():
    assert numbering.resolve_body_numbering({}, _document(_paragraph()), [0]) == [{
        "paragraph_index": 0, "p_style": None, "style_chain": [],
        "num_id": None, "abstract_num_id": None, "ilvl": None, "label": None,
        "value": None, "start": None, "start_source": None, "restart": None,
    }]


def test_direct_decimal_continues_across_ordinary_text_and_unordered_queries():
    source = _document(_paragraph(num=1), _paragraph(), _paragraph(num=1), _paragraph(num=1))
    assert _labels(_parts(), source, [3, 0, 1, 2]) == ["3.", "1.", None, "2."]
    facts = numbering.resolve_body_numbering(_parts(), source, [3])[0]
    assert facts["value"] == 3
    assert facts["start"] == 1
    assert facts["start_source"] == "abstract_start"
    assert facts["abstract_num_id"] == "9"
    assert facts["restart"] == "continuous_level_zero"


def test_query_includes_prior_main_story_table_cell_paragraphs():
    source = _document(
        _paragraph(num=1),
        f'<w:tbl><w:tr><w:tc>{_paragraph(num=1)}</w:tc></w:tr></w:tbl>',
        _paragraph(num=1),
    )
    assert _labels(_parts(), source, [2]) == ["3."]


def test_text_box_numbers_do_not_increment_the_main_story_counter():
    source = _document(
        '<w:p><w:r><w:pict><w:txbxContent>' + _paragraph(num=1)
        + '</w:txbxContent></w:pict></w:r></w:p>',
        _paragraph(num=1),
    )
    assert _labels(_parts(), source, [2]) == ["1."]
    _refusal(_parts(), source, [1], "unknown_numbering_container")


def test_unknown_container_of_same_sequence_is_not_silently_counted():
    source = _document('<w:unprovedBlock>' + _paragraph(num=1) + '</w:unprovedBlock>', _paragraph(num=1))
    _refusal(_parts(), source, [1], "unknown_numbering_container")


@pytest.mark.parametrize("start", [0, 7, 2_147_483_646])
def test_explicit_start_is_not_assumed_to_be_one(start):
    assert _labels(_parts(level=_level(start)), _document(_paragraph(num=1), _paragraph(num=1)), [0, 1]) == [f"{start}.", f"{start + 1}."]


def test_shared_abstract_without_explicit_restart_is_not_assumed_independent():
    parts = _parts(instances='''
      <w:num w:numId="1"><w:abstractNumId w:val="9"/></w:num>
      <w:num w:numId="2"><w:abstractNumId w:val="9"/></w:num>''')
    root = _document(*[_paragraph(num=num) for num in (1, 1, 2, 2, 1)])
    _refusal(parts, root, list(range(5)), "implicit_instance_restart")


def test_distinct_abstracts_have_independent_counters_when_their_instances_interleave():
    parts = _parts(instances='''
      <w:num w:numId="1"><w:abstractNumId w:val="9"/></w:num>
      <w:num w:numId="2"><w:abstractNumId w:val="8"/></w:num>''',
      extra='<w:abstractNum w:abstractNumId="8">' + _level() + '</w:abstractNum>')
    root = _document(*[_paragraph(num=num) for num in (1, 1, 2, 2, 1)])
    assert _labels(parts, root, list(range(5))) == ["1.", "2.", "1.", "2.", "3."]


def _shared_restart_parts():
    return _parts(instances='''
      <w:num w:numId="1"><w:abstractNumId w:val="9"/></w:num>
      <w:num w:numId="2"><w:abstractNumId w:val="9"/>
        <w:lvlOverride w:ilvl="0"><w:startOverride w:val="7"/></w:lvlOverride></w:num>''')


@pytest.mark.parametrize("requested", [[0, 1, 2], [2], [0, 2], [3]])
def test_return_to_shared_abstract_instance_refuses_even_when_other_instance_is_not_requested(requested):
    root = _document(*[_paragraph(num=num) for num in (1, 2, 1, 2)])
    _refusal(_shared_restart_parts(), root, requested, "interleaved_numbering_instances")


def test_consecutive_shared_abstract_blocks_with_explicit_restart_stay_supported():
    root = _document(*[_paragraph(num=num) for num in (1, 1, 2, 2)])
    assert _labels(_shared_restart_parts(), root, [0, 1, 2, 3]) == ["1.", "2.", "7.", "8."]


def test_replacement_level_start_is_not_unproved_restart_for_a_second_shared_instance():
    parts = _parts(instances='''
      <w:num w:numId="1"><w:abstractNumId w:val="9"/></w:num>
      <w:num w:numId="2"><w:abstractNumId w:val="9"/>
        <w:lvlOverride w:ilvl="0">''' + _level(start=7) + '''</w:lvlOverride></w:num>''')
    root = _document(_paragraph(num=1), _paragraph(num=2))
    _refusal(parts, root, [0, 1], "implicit_instance_restart")


def test_same_instance_resumes_after_ordinary_text_without_false_return_refusal():
    root = _document(_paragraph(num=1), _paragraph(), _paragraph(num=1))
    assert _labels(_shared_restart_parts(), root, [0, 1, 2]) == ["1.", None, "2."]


def test_interleaving_after_last_requested_label_does_not_change_an_earlier_label():
    root = _document(*[_paragraph(num=num) for num in (1, 2, 1)])
    assert _labels(_shared_restart_parts(), root, [0, 1]) == ["1.", "7."]


def test_unused_instance_sharing_selected_abstract_does_not_block():
    root = _document(_paragraph(num=1), _paragraph(num=1))
    assert _labels(_shared_restart_parts(), root, [0, 1]) == ["1.", "2."]


def test_other_instance_in_text_box_story_does_not_create_main_story_return():
    root = _document(
        _paragraph(num=1),
        '<w:p><w:r><w:pict><w:txbxContent>' + _paragraph(num=2)
        + '</w:txbxContent></w:pict></w:r></w:p>',
        _paragraph(num=1),
    )
    assert _labels(_shared_restart_parts(), root, [0, 3]) == ["1.", "2."]


def test_unsupported_container_of_other_shared_instance_is_not_skipped():
    root = _document(_paragraph(num=1), '<w:unprovedBlock>' + _paragraph(num=2)
                     + '</w:unprovedBlock>', _paragraph(num=1))
    _refusal(_shared_restart_parts(), root, [2], "unknown_numbering_container")


def test_prior_unknown_style_cannot_hide_another_shared_instance():
    root = _document(_paragraph(num=1), _paragraph(style="Missing"), _paragraph(num=1))
    _refusal(_shared_restart_parts(), root, [2], "missing_or_duplicate_style")


def test_structural_effects_include_later_other_instance_in_shared_family():
    before = _document(*[_paragraph(num=num) for num in (1, 1, 2, 2)])
    after = _document(*[_paragraph(num=num) for num in (1, 2, 2)])
    effects = numbering.numbering_effects_for_deletion(_shared_restart_parts(), before, after, 0)
    assert [(item["paragraph_index"], item["label"]) for item in effects["before"]] == [(0, "1."), (1, "2."), (2, "7."), (3, "8.")]
    assert [(item["paragraph_index"], item["label"]) for item in effects["after"]] == [(0, "1."), (1, "7."), (2, "8.")]


def test_removing_only_restart_item_cannot_hide_later_return_to_original_instance():
    before = _document(*[_paragraph(num=num) for num in (1, 2, 1)])
    after = _document(*[_paragraph(num=num) for num in (1, 1)])
    with pytest.raises(ApplyError) as raised:
        numbering.numbering_effects_for_deletion(_shared_restart_parts(), before, after, 1)
    assert raised.value.metadata["reason"] == "interleaved_numbering_instances"


_DURABLE_METADATA = "{http://schemas.microsoft.com/office/word/2016/wordml/cid}durableId"
_SECTION_RESTART = "{http://schemas.microsoft.com/office/word/2012/wordml}restartNumberingAfterBreak"


def _metadata_parts(attribute, value, owner="num", *, parts=None):
    result = dict(parts or _parts())
    root = etree.fromstring(result["word/numbering.xml"])
    root.find(".//" + w(owner)).set(attribute, value)
    result["word/numbering.xml"] = etree.tostring(root)
    return result


@pytest.mark.parametrize("durable", ["1", "930235092", "2147483646", "0001"])
@pytest.mark.parametrize("restart", ["0", "false", "off"])
def test_native_word_numbering_metadata_preserves_supported_labels_and_input(durable, restart):
    parts = _metadata_parts(_DURABLE_METADATA, durable)
    parts = _metadata_parts(_SECTION_RESTART, restart, "abstractNum", parts=parts)
    root = _document(_paragraph(num=1), _paragraph(num=1))
    original = (dict(parts), etree.tostring(root))
    assert _labels(parts, root, [0, 1]) == ["1.", "2."]
    assert original == (dict(parts), etree.tostring(root))


@pytest.mark.parametrize("value", ["0", "2147483647", "2147483648", "-1", "+1", "1.0", "", " 1", "1 ", "١", "A1", "1" * 1000])
def test_durable_metadata_is_bounded_positive_ascii_decimal(value):
    _refusal(_metadata_parts(_DURABLE_METADATA, value), _document(_paragraph(num=1)), [0], "invalid_value")


@pytest.mark.parametrize("value", ["1", "true", "on", "FALSE", "False", "", "no", " 0", "0 "])
def test_enabled_or_invalid_section_restart_is_not_ignored(value):
    _refusal(_metadata_parts(_SECTION_RESTART, value, "abstractNum"), _document(_paragraph(num=1)), [0], "unsupported_section_restart")


@pytest.mark.parametrize(("attribute", "owner"), [
    (_DURABLE_METADATA, "abstractNum"),
    (_DURABLE_METADATA, "lvl"),
    (_SECTION_RESTART, "num"),
    (_SECTION_RESTART, "lvl"),
    (w("durableId"), "num"),
    ("{http://schemas.microsoft.com/office/word/2016/wordml}durableId", "num"),
    ("{http://schemas.microsoft.com/office/word/2012/wordml}durableId", "num"),
    (w("restartNumberingAfterBreak"), "abstractNum"),
    ("{http://schemas.microsoft.com/office/word/2016/wordml/cid}restartNumberingAfterBreak", "abstractNum"),
])
def test_metadata_allowlist_requires_exact_owner_and_namespace(attribute, owner):
    _refusal(_metadata_parts(attribute, "1" if "durableId" in attribute else "0", owner), _document(_paragraph(num=1)), [0], "unknown_attributes")


def test_durable_metadata_does_not_become_counter_or_group_identity():
    parts = _parts(instances='''
      <w:num w:numId="1"><w:abstractNumId w:val="9"/></w:num>
      <w:num w:numId="2"><w:abstractNumId w:val="8"/></w:num>''',
      extra='<w:abstractNum w:abstractNumId="8">' + _level(start=7) + '</w:abstractNum>')
    numbering_root = etree.fromstring(parts["word/numbering.xml"])
    for instance in numbering_root.findall(w("num")):
        instance.set(_DURABLE_METADATA, "17")
    parts["word/numbering.xml"] = etree.tostring(numbering_root)
    root = _document(*[_paragraph(num=number) for number in (1, 2, 1, 2)])
    original = dict(parts)
    assert _labels(parts, root, [0, 1, 2, 3]) == ["1.", "7.", "2.", "8."]
    assert parts == original


def test_unknown_attribute_remains_refusal_beside_allowed_metadata():
    parts = _metadata_parts(_DURABLE_METADATA, "17")
    parts = _metadata_parts("{urn:fictional-unknown}counter", "1", parts=parts)
    _refusal(parts, _document(_paragraph(num=1)), [0], "unknown_attributes")


def test_unused_definition_metadata_does_not_expand_selected_dependencies():
    extra = '<w:num xmlns:unknown="urn:fictional-unknown" w:numId="2" unknown:counter="1"><w:abstractNumId w:val="9"/></w:num>'
    assert _labels(_parts(extra=extra), _document(_paragraph(num=1)), [0]) == ["1."]


@pytest.mark.parametrize("with_replacement", [False, True])
def test_start_override_wins_and_is_used_once_per_instance(with_replacement):
    replacement = _level(start=3, text="(%1)") if with_replacement else ""
    parts = _parts(instances=(
        '<w:num w:numId="1"><w:abstractNumId w:val="9"/>'
        '<w:lvlOverride w:ilvl="0">' + replacement
        + '<w:startOverride w:val="12"/></w:lvlOverride></w:num>'
    ))
    root = _document(_paragraph(num=1), _paragraph(), _paragraph(num=1))
    assert _labels(parts, root, [0, 2]) == (["(12)", "(13)"] if with_replacement else ["12.", "13."])
    assert numbering.resolve_body_numbering(parts, root, [2])[0]["start_source"] == "start_override"


def test_full_level_override_replaces_the_label_and_start():
    parts = _parts(instances=(
        '<w:num w:numId="1"><w:abstractNumId w:val="9"/>'
        '<w:lvlOverride w:ilvl="0">' + _level(start=5, text="Article %1)")
        + '</w:lvlOverride></w:num>'
    ))
    assert _labels(parts, _document(_paragraph(num=1), _paragraph(num=1)), [0, 1]) == ["Article 5)", "Article 6)"]
    assert numbering.resolve_body_numbering(parts, _document(_paragraph(num=1)), [0])[0]["start_source"] == "level_override"


def test_full_override_does_not_parse_a_replaced_unsupported_format():
    parts = _parts(level=_level(fmt="upperRoman"), instances=(
        '<w:num w:numId="1"><w:abstractNumId w:val="9"/>'
        '<w:lvlOverride w:ilvl="0">' + _level(start=5, text="(%1)")
        + '</w:lvlOverride></w:num>'
    ))
    assert _labels(parts, _document(_paragraph(num=1), _paragraph(num=1)), [0, 1]) == ["(5)", "(6)"]


def test_explicit_start_override_supplies_missing_abstract_start():
    level = '<w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
    instances = '''<w:num w:numId="1"><w:abstractNumId w:val="9"/>
      <w:lvlOverride w:ilvl="0"><w:startOverride w:val="8"/></w:lvlOverride></w:num>'''
    assert _labels(_parts(level=level, instances=instances), _document(_paragraph(num=1), _paragraph(num=1)), [0, 1]) == ["8.", "9."]


def test_inherited_numbering_proves_style_chain_and_direct_override():
    parts = _parts(styles=_style("Base", num=1) + _style("Child", base="Base") + _style("Leaf", base="Child"))
    root = _document(_paragraph(style="Leaf"), _paragraph(style="Leaf"), _paragraph(style="Leaf", num=0), _paragraph(style="Child"))
    assert _labels(parts, root, [0, 1, 2, 3]) == ["1.", "2.", None, "3."]
    assert numbering.resolve_body_numbering(parts, root, [0])[0]["style_chain"] == ["Base", "Child", "Leaf"]


def test_default_paragraph_style_can_supply_numbering():
    parts = _parts(styles=_style("DefaultList", num=1, default=True))
    root = _document(_paragraph(), _paragraph(num=0), _paragraph())
    assert _labels(parts, root, [0, 1, 2]) == ["1.", None, "2."]
    assert numbering.resolve_body_numbering(parts, root, [0])[0]["p_style"] == "DefaultList"


def test_document_defaults_can_supply_numbering_and_direct_numid_zero_removes_it():
    parts = _parts(styles='''<w:docDefaults><w:pPrDefault><w:pPr>
      <w:numPr><w:numId w:val="1"/></w:numPr>
      </w:pPr></w:pPrDefault></w:docDefaults>''')
    assert _labels(parts, _document(_paragraph(), _paragraph(num=0), _paragraph()), [0, 1, 2]) == ["1.", None, "2."]


def test_level_style_association_accepts_inherited_base_style():
    parts = _parts(level=_level(extra='<w:pStyle w:val="Base"/>'), styles=_style("Base", num=1) + _style("Leaf", base="Base"))
    assert _labels(parts, _document(_paragraph(style="Leaf")), [0]) == ["1."]


def test_foreign_level_style_association_refuses_inherited_list():
    parts = _parts(level=_level(extra='<w:pStyle w:val="Other"/>'), styles=_style("Base", num=1))
    _refusal(parts, _document(_paragraph(style="Base")), [0], "style_level_mismatch")


def test_direct_instance_overrides_foreign_level_style_association():
    parts = _parts(level=_level(extra='<w:pStyle w:val="Other"/>'), styles=_style("Plain"))
    assert _labels(parts, _document(_paragraph(style="Plain", num=1)), [0]) == ["1."]


def test_clean_deleted_list_item_has_literal_independent_numbering_projections():
    before = _document(_paragraph("A", num=1), _paragraph("B", num=1), _paragraph("C", num=1), _paragraph("D", num=1))
    # An independent expected document, not a call to a structural mutator.
    after = _document(_paragraph("A", num=1), _paragraph("C", num=1), _paragraph("D", num=1))
    result = numbering.numbering_effects_for_deletion(_parts(), before, after, 1)
    assert [(item["paragraph_index"], item["label"]) for item in result["before"]] == [(0, "1."), (1, "2."), (2, "3."), (3, "4.")]
    assert [(item["paragraph_index"], item["label"]) for item in result["after"]] == [(0, "1."), (1, "2."), (2, "3.")]


def test_deleting_first_item_preserves_start_override_for_next_item():
    parts = _parts(instances='''<w:num w:numId="1"><w:abstractNumId w:val="9"/>
      <w:lvlOverride w:ilvl="0"><w:startOverride w:val="20"/></w:lvlOverride></w:num>''')
    before = _document(_paragraph("A", num=1), _paragraph("B", num=1), _paragraph("C", num=1))
    after = _document(_paragraph("B", num=1), _paragraph("C", num=1))
    result = numbering.numbering_effects_for_deletion(parts, before, after, 0)
    assert [item["label"] for item in result["before"]] == ["20.", "21.", "22."]
    assert [item["label"] for item in result["after"]] == ["20.", "21."]


def test_insertion_uses_same_longer_shorter_projection_proof_without_mutation():
    shorter = _document(_paragraph("A", num=1), _paragraph("C", num=1))
    longer = _document(_paragraph("A", num=1), _paragraph("B", num=1), _paragraph("C", num=1))
    parts = _parts()
    original = (dict(parts), etree.tostring(shorter), etree.tostring(longer))
    result = numbering.numbering_effects_for_deletion(parts, longer, shorter, 1)
    assert [item["label"] for item in result["before"]] == ["1.", "2.", "3."]
    assert [item["label"] for item in result["after"]] == ["1.", "2."]
    assert original == (dict(parts), etree.tostring(shorter), etree.tostring(longer))


def test_unrelated_unknown_numbering_is_not_silently_decimal_and_does_not_block():
    foreign = '<w:abstractNum w:abstractNumId="8">' + _level(fmt="not-a-format") + '</w:abstractNum><w:num w:numId="2"><w:abstractNumId w:val="8"/></w:num>'
    parts = _parts(extra=foreign)
    root = _document(_paragraph(num=2), _paragraph(num=1), _paragraph(num=2), _paragraph(num=1))
    assert _labels(parts, root, [1, 3]) == ["1.", "2."]
    _refusal(parts, root, [0], "unknown_number_format")


def test_foreign_unknown_list_between_affected_items_does_not_block_deletion():
    parts = _parts(extra='<w:abstractNum w:abstractNumId="8">' + _level(fmt="bullet") + '</w:abstractNum><w:num w:numId="2"><w:abstractNumId w:val="8"/></w:num>')
    before = _document(_paragraph(num=1), _paragraph(num=2), _paragraph(num=1))
    after = _document(_paragraph(num=2), _paragraph(num=1))
    result = numbering.numbering_effects_for_deletion(parts, before, after, 0)
    assert [(item["paragraph_index"], item["label"]) for item in result["before"]] == [(0, "1."), (2, "2.")]
    assert [(item["paragraph_index"], item["label"]) for item in result["after"]] == [(1, "1.")]


def _table_cell(*paragraphs):
    return '<w:tbl><w:tr><w:tc>' + ''.join(paragraphs) + '</w:tc></w:tr></w:tbl>'


def _pending_mark(kind="ins", identifier=201):
    return (f'<w:rPr><w:{kind} w:id="{identifier}" w:author="Fictional reviewer" '
            'w:date="2026-01-02T12:00:00Z"/></w:rPr>')


@pytest.mark.parametrize("mark", ["ins", "del"])
@pytest.mark.parametrize("position", ["before", "after"])
def test_plain_cross_container_pending_mark_is_not_a_local_numbering_neighbor(mark, position):
    foreign = _paragraph("Independent proposal", props=_pending_mark(mark))
    target = _table_cell(_paragraph("Retained anchor"), _paragraph("Selected content"))
    removed = _table_cell(_paragraph("Retained anchor"))
    before = _document(foreign, target) if position == "before" else _document(target, foreign)
    after = _document(foreign, removed) if position == "before" else _document(removed, foreign)
    selected_index = 2 if position == "before" else 1
    original = (etree.tostring(before), etree.tostring(after))
    result = numbering.numbering_effects_for_deletion({}, before, after, selected_index)
    assert [item["label"] for item in result["before"]] == [None, None]
    assert [item["label"] for item in result["after"]] == [None]
    assert [item["paragraph_index"] for item in result["before"]] == ([1, 2] if position == "before" else [0, 1])
    assert original == (etree.tostring(before), etree.tostring(after))


@pytest.mark.parametrize("mark", ["ins", "del"])
@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("reference", ["direct", "style"])
def test_same_instance_pending_mark_in_another_container_still_refuses(mark, position, reference):
    parts = _parts(styles=_style("List", num=1))
    numbered = {"num": 1} if reference == "direct" else {"style": "List"}
    foreign = _paragraph("Counter dependency", props=_pending_mark(mark), **numbered)
    target = _table_cell(_paragraph("Retained anchor", **numbered), _paragraph("Selected content", **numbered))
    removed = _table_cell(_paragraph("Retained anchor", **numbered))
    before = _document(foreign, target) if position == "before" else _document(target, foreign)
    after = _document(foreign, removed) if position == "before" else _document(removed, foreign)
    with pytest.raises(ApplyError) as raised:
        numbering.numbering_effects_for_deletion(parts, before, after, 2 if position == "before" else 1)
    assert raised.value.code == "paragraph_numbering_unsupported"
    assert raised.value.metadata["reason"] == "pending_paragraph_mark"


@pytest.mark.parametrize("position", ["before", "after"])
def test_unresolved_cross_container_style_remains_a_numbered_sequence_dependency(position):
    unknown = _paragraph("Unresolved potential list", style="Missing")
    target = _table_cell(_paragraph("Retained anchor", num=1), _paragraph("Selected content", num=1))
    removed = _table_cell(_paragraph("Retained anchor", num=1))
    before = _document(unknown, target) if position == "before" else _document(target, unknown)
    after = _document(unknown, removed) if position == "before" else _document(removed, unknown)
    with pytest.raises(ApplyError) as raised:
        numbering.numbering_effects_for_deletion(_parts(), before, after, 2 if position == "before" else 1)
    assert raised.value.metadata["reason"] == "missing_or_duplicate_style"


@pytest.mark.parametrize("position", ["before", "after"])
def test_plain_unit_does_not_claim_a_numbering_effect_for_an_unknown_style_in_another_container(position):
    unknown = _paragraph("Unrelated style", style="Missing")
    target = _table_cell(_paragraph("Retained anchor"), _paragraph("Selected content"))
    removed = _table_cell(_paragraph("Retained anchor"))
    before = _document(unknown, target) if position == "before" else _document(target, unknown)
    after = _document(unknown, removed) if position == "before" else _document(removed, unknown)
    result = numbering.numbering_effects_for_deletion({}, before, after, 2 if position == "before" else 1)
    assert len(result["before"]) == 2
    assert all(item["num_id"] is None for item in result["before"] + result["after"])


@pytest.mark.parametrize("position", ["before", "after"])
def test_pending_mark_in_same_container_remains_a_strict_local_boundary(position):
    pending = _paragraph("Neighbor with unresolved boundary", props=_pending_mark())
    content = _paragraph("Selected content")
    before = _document(pending, content) if position == "before" else _document(content, pending)
    after = _document(pending)
    with pytest.raises(ApplyError) as raised:
        numbering.numbering_effects_for_deletion({}, before, after, 1 if position == "before" else 0)
    assert raised.value.metadata["reason"] == "pending_paragraph_mark"


def _literal_structural_unit(kind, first_id):
    marker = _pending_mark(kind, first_id)
    if kind == "ins":
        return (_paragraph("Fictional retained anchor", props=marker)
                + f'<w:p><w:pPr/><w:ins w:id="{first_id + 1}" w:author="Fictional reviewer" '
                'w:date="2026-01-02T12:00:00Z"><w:r><w:t>Fictional new clause</w:t>'
                '</w:r></w:ins></w:p>')
    return (f'<w:p><w:pPr>{marker}</w:pPr><w:del w:id="{first_id + 1}" '
            'w:author="Fictional reviewer" w:date="2026-01-02T12:00:00Z">'
            '<w:r><w:delText>Fictional old clause</w:delText></w:r></w:del></w:p>'
            + _paragraph("Fictional retained anchor"))


@pytest.mark.parametrize("kind", ["ins", "del"])
@pytest.mark.parametrize("container", ["cell-body", "body-cell", "two-cells"])
def test_literal_plain_structural_unit_proves_both_outcomes_with_independent_pending_unit(kind, container):
    from veqtor_docx._drafting_paragraphs import paragraph_structure_candidates
    from veqtor_docx.revision_decisions import _paragraph_outcomes

    selected = _literal_structural_unit(kind, 101)
    foreign = _literal_structural_unit("ins", 201)
    if container == "cell-body":
        root = _document(_table_cell(selected), foreign)
    elif container == "body-cell":
        root = _document(selected, _table_cell(foreign))
    else:
        root = _document('<w:tbl><w:tr><w:tc>' + selected + '</w:tc><w:tc>'
                         + foreign + '</w:tc></w:tr></w:tbl>')
    original = etree.tostring(root)
    mark = next(node for node in root.iter(w(kind)) if node.get(w("id")) == "101")
    _, outcomes, facts = _paragraph_outcomes(root, paragraph_structure_candidates(mark), {})
    if kind == "ins":
        assert facts["accepted_text"] == "Fictional retained anchor\nFictional new clause"
        assert facts["rejected_text"] == "Fictional retained anchor"
    else:
        assert facts["accepted_text"] == "Fictional retained anchor"
        assert facts["rejected_text"] == "Fictional old clause\nFictional retained anchor"
    for decision in ("accept", "reject"):
        assert all(item["num_id"] is None for item in facts["paragraph_numbering"]["accepted" if decision == "accept" else "rejected"])
        assert not any(node.tag in {w("ins"), w("del")} for paragraph in outcomes[decision] for node in paragraph.iter())
    assert etree.tostring(root) == original


@pytest.mark.parametrize("kind", ["ins", "del"])
def test_structural_unit_still_refuses_foreign_history_inside_its_own_anchor(kind):
    from veqtor_docx._drafting_paragraphs import paragraph_structure_candidates
    from veqtor_docx.revision_decisions import _paragraph_outcomes

    root = _document(_table_cell(_literal_structural_unit(kind, 101)), _literal_structural_unit("ins", 201))
    mark = next(node for node in root.iter(w(kind)) if node.get(w("id")) == "101")
    nodes = paragraph_structure_candidates(mark)
    props = root.find(f'.//{w("p")}/{w("pPr")}')
    history = etree.SubElement(props, w("pPrChange"), {w("id"): "99", w("author"): "Another fictional reviewer"})
    etree.SubElement(history, w("pPr"))
    with pytest.raises(ApplyError) as raised:
        _paragraph_outcomes(root, nodes, {})
    assert raised.value.code == "paragraph_structure_unsupported"


@pytest.mark.parametrize("fmt", ["upperRoman", "decimalZero", "bullet", "none", "madeUp", ""])
def test_unknown_or_nondecimal_format_refuses(fmt):
    _refusal(_parts(level=_level(fmt=fmt)), _document(_paragraph(num=1)), [0], "unknown_number_format")


@pytest.mark.parametrize("template", ["%2.", "%1.%2", "%1 %1", "%10.", "%1%", "Clause"])
def test_unproved_level_text_never_falls_back(template):
    _refusal(_parts(level=_level(text=template)), _document(_paragraph(num=1)), [0], "unknown_number_format")


@pytest.mark.parametrize("fragment,reason", [
    ('<w:lvlPicBulletId w:val="1"/>', "unknown_level_properties"),
    ('<w:legacy w:legacy="1"/>', "unknown_level_properties"),
    ('<w:lvlRestart w:val="1"/>', "ambiguous_restart"),
    ('<w:lvlRestart w:val="8"/>', "ambiguous_restart"),
])
def test_ambiguous_level_features_refuse(fragment, reason):
    _refusal(_parts(level=_level(extra=fragment)), _document(_paragraph(num=1)), [0], reason)


def test_explicit_level_zero_never_restart_is_supported():
    root = _document(_paragraph(num=1), _paragraph(), _paragraph(num=1))
    assert _labels(_parts(level=_level(extra='<w:lvlRestart w:val="0"/>')), root, [0, 2]) == ["1.", "2."]


@pytest.mark.parametrize("extra", ['<w:styleLink w:val="Linked"/>', '<w:numStyleLink w:val="Linked"/>'])
def test_style_link_numbering_refuses(extra):
    _refusal(_parts(abstract_extra=extra), _document(_paragraph(num=1)), [0], "linked_or_unknown_numbering")


@pytest.mark.parametrize("root,reason", [
    (_document(_paragraph(num=1, level=1)), "level_mismatch"),
    (_document(_paragraph(num=1, level=1), _paragraph(num=1)), "level_mismatch"),
    (_document(_paragraph(props='<w:numPr><w:ilvl w:val="0"/></w:numPr>')), "missing_num_id"),
])
def test_level_mismatch_or_orphan_level_is_not_guessed(root, reason):
    _refusal(_parts(), root, [len(list(root.iter(w("p")))) - 1], reason)


@pytest.mark.parametrize("styles,reason", [
    (_style("A", base="B") + _style("B", base="A"), "style_cycle"),
    (_style("A", base="Missing"), "missing_or_duplicate_style"),
    (_style("A") + _style("A"), "missing_or_duplicate_style"),
    ('<w:style w:styleId="A" w:type="character"/>', "invalid_style"),
])
def test_style_resolution_fails_closed(styles, reason):
    _refusal(_parts(styles=styles), _document(_paragraph(style="A")), [0], reason)


def test_missing_style_cannot_be_guessed_away_in_prior_affected_counter():
    root = _document(_paragraph(style="Missing"), _paragraph(num=1))
    _refusal(_parts(), root, [1], "missing_or_duplicate_style")


def test_unrelated_missing_style_does_not_poison_plain_selected_paragraph():
    root = _document(_paragraph(style="Missing"), _paragraph())
    assert _labels({}, root, [1]) == [None]


def test_invalid_default_style_flag_cannot_be_silently_treated_as_false():
    parts = _parts(styles=_style("A", num=1, default=True).replace('w:default="1"', 'w:default="perhaps"'))
    _refusal(parts, _document(_paragraph()), [0], "invalid_default_style")


def test_multiple_default_paragraph_styles_are_ambiguous():
    parts = _parts(styles=_style("A", num=1, default=True) + _style("B", default=True))
    _refusal(parts, _document(_paragraph()), [0], "duplicate_definition")


def test_explicit_style_does_not_depend_on_an_unrelated_default_style_flag():
    styles = _style("A", num=1, default=True).replace('w:default="1"', 'w:default="perhaps"') + _style("Explicit", num=1)
    assert _labels(_parts(styles=styles), _document(_paragraph(style="Explicit")), [0]) == ["1."]


@pytest.mark.parametrize("extra", [
    '<w:num w:numId="1"><w:abstractNumId w:val="9"/></w:num>',
    '<w:num w:numId="01"><w:abstractNumId w:val="9"/></w:num>',
    '<w:abstractNum w:abstractNumId="9">' + _level() + '</w:abstractNum>',
])
def test_duplicate_numeric_definition_ids_refuse(extra):
    _refusal(_parts(extra=extra), _document(_paragraph(num=1)), [0], "missing_or_duplicate_definition")


def _definition_alias(parts, kind, alias):
    """Add a conflicting literal definition; retain XML whitespace as charrefs."""
    result = dict(parts)
    root = etree.fromstring(result["word/numbering.xml"])
    if kind == "instance":
        duplicate = copy.deepcopy(root.find(w("num")))
        duplicate.set(w("numId"), alias)
        override = etree.SubElement(duplicate, w("lvlOverride"), {w("ilvl"): "0"})
        etree.SubElement(override, w("startOverride"), {w("val"): "77"})
    else:
        duplicate = copy.deepcopy(root.find(w("abstractNum")))
        duplicate.set(w("abstractNumId"), alias)
        duplicate.find(f'{w("lvl")}/{w("start")}').set(w("val"), "77")
    root.append(duplicate)
    result["word/numbering.xml"] = etree.tostring(root)
    return result


@pytest.mark.parametrize("kind", ["instance", "abstract"])
@pytest.mark.parametrize("reference", ["direct", "style"])
@pytest.mark.parametrize("alias", [
    "+{}", " {}", "{} ", "\t{}\n", "\r\n+{}\t", "0000000000{}",
    "+0000000000{}", "0" * 10_000 + "{}", "\t+" + "0" * 10_000 + "{}\r\n",
], ids=["plus", "leading-space", "trailing-space", "tab-newline", "signed-xml-whitespace",
        "eleven-digits", "signed-eleven-digits", "long-leading-zeros", "signed-long-leading-zeros"])
def test_all_selected_int32_definition_aliases_participate_in_uniqueness(kind, reference, alias):
    selected = "1" if kind == "instance" else "9"
    parts = _definition_alias(_parts(styles=_style("List", num=1)), kind, alias.format(selected))
    paragraph = _paragraph(num=1) if reference == "direct" else _paragraph(style="List")
    root = _document(paragraph)
    original = (dict(parts), etree.tostring(root))
    _refusal(parts, root, [0], "missing_or_duplicate_definition")
    assert original == (parts, etree.tostring(root))


@pytest.mark.parametrize("kind", ["instance", "abstract"])
@pytest.mark.parametrize("alias", [
    "\u00a0{}", "{}\u00a0", "\u2003{}", "１", "{}x", "-{}",
    "+2147483648", "-2147483649", "9" * 10_000,
], ids=["leading-nbsp", "trailing-nbsp", "em-space", "fullwidth-digit", "trailing-text",
        "negative-other-id", "positive-overflow", "negative-overflow", "long-overflow"])
def test_non_alias_or_out_of_range_unrelated_definition_does_not_block(kind, alias):
    selected = "1" if kind == "instance" else "9"
    parts = _definition_alias(_parts(), kind, alias.format(selected))
    assert _labels(parts, _document(_paragraph(num=1)), [0]) == ["1."]


@pytest.mark.parametrize("reference", ["direct", "style"])
@pytest.mark.parametrize("alias", ["+0", "-0", "-00000000000000", "\t-000\r\n"])
def test_signed_zero_alias_of_selected_abstract_definition_refuses(reference, alias):
    parts = _parts(styles=_style("List", num=1))
    parsed = etree.fromstring(parts["word/numbering.xml"])
    parsed.find(w("abstractNum")).set(w("abstractNumId"), "0")
    parsed.find(f'{w("num")}/{w("abstractNumId")}').set(w("val"), "0")
    parts["word/numbering.xml"] = etree.tostring(parsed)
    parts = _definition_alias(parts, "abstract", alias)
    paragraph = _paragraph(num=1) if reference == "direct" else _paragraph(style="List")
    _refusal(parts, _document(paragraph), [0], "missing_or_duplicate_definition")


def test_numid_zero_suppression_does_not_resolve_any_zero_definition_aliases():
    parts = _parts(styles=_style("List", num=1))
    parts = _definition_alias(parts, "instance", "0")
    parts = _definition_alias(parts, "instance", "-00000000000000")
    assert _labels(parts, _document(_paragraph(style="List", num=0)), [0]) == [None]


@pytest.mark.parametrize("reference", ["direct", "style"])
@pytest.mark.parametrize("value", ["+1", " 1", "1 ", "\t1\n", "00000000001", "-0", "\u00a01"])
def test_definition_identity_fix_does_not_expand_supported_reference_syntax(reference, value):
    parts = _parts(styles=_style("List", num=1))
    root = _document(_paragraph(num=1) if reference == "direct" else _paragraph(style="List"))
    if reference == "direct":
        root.find(f'.//{w("numId")}').set(w("val"), value)
    else:
        styles = etree.fromstring(parts["word/styles.xml"])
        styles.find(f'.//{w("numId")}').set(w("val"), value)
        parts["word/styles.xml"] = etree.tostring(styles)
    _refusal(parts, root, [0], "invalid_value")


@pytest.mark.parametrize("kind", ["instance", "abstract"])
def test_maximum_supported_definition_id_alias_is_detected_without_large_int_conversion(kind):
    parts = _parts()
    parsed = etree.fromstring(parts["word/numbering.xml"])
    if kind == "instance":
        parsed.find(w("num")).set(w("numId"), "2147483647")
        selected_num = "2147483647"
    else:
        parsed.find(w("abstractNum")).set(w("abstractNumId"), "2147483647")
        parsed.find(f'{w("num")}/{w("abstractNumId")}').set(w("val"), "2147483647")
        selected_num = "1"
    parts["word/numbering.xml"] = etree.tostring(parsed)
    parts = _definition_alias(parts, kind, "\t+" + "0" * 10_000 + "2147483647\r")
    _refusal(parts, _document(_paragraph(num=selected_num)), [0], "missing_or_duplicate_definition")


def test_missing_numbering_instance_refuses():
    _refusal(_parts(), _document(_paragraph(num=12)), [0], "missing_or_duplicate_definition")


@pytest.mark.parametrize("change", [
    '<w:numberingChange w:id="4"/>', '<w:ins w:id="4"/>', '<w:ilvl w:val="0"/>',
])
def test_numpr_revision_or_duplicate_refuses(change):
    root = _document(_paragraph(props='<w:numPr><w:numId w:val="1"/><w:ilvl w:val="0"/>' + change + '</w:numPr>'))
    _refusal(_parts(), root, [0], "duplicate_definition" if change.startswith("<w:ilvl") else "unknown_num_properties")


@pytest.mark.parametrize("props,reason", [
    ('<w:pPrChange w:id="4"><w:pPr/></w:pPrChange>', "pending_properties"),
    ('<w:rPr><w:del w:id="4"/></w:rPr>', "pending_paragraph_mark"),
])
def test_resolver_requires_clean_paragraph_properties_and_marks(props, reason):
    _refusal(_parts(), _document(_paragraph(num=1, props=props)), [0], reason)


@pytest.mark.parametrize("where", ["instance", "abstract", "level", "numpr", "style"])
def test_foreign_attributes_are_never_ignored_on_selected_dependencies(where):
    parts = _parts(styles=_style("A", num=1))
    root = _document(_paragraph(style="A"))
    if where == "numpr":
        root.find(f'.//{w("pPr")}').append(etree.Element(w("numPr"), {"{urn:untrusted}restart": "1"}))
    else:
        part_name = "word/styles.xml" if where == "style" else "word/numbering.xml"
        parsed = etree.fromstring(parts[part_name])
        tag = {"instance": "num", "abstract": "abstractNum", "level": "lvl", "style": "style"}[where]
        parsed.find(f'.//{w(tag)}').set("{urn:untrusted}restart", "1")
        parts[part_name] = etree.tostring(parsed)
    _refusal(parts, root, [0], "unknown_attributes")


@pytest.mark.parametrize("value", ["-1", "+1", "1.0", " 1", "１", "2147483648", "999999999999"])
def test_invalid_numeric_identifiers_refuse(value):
    _refusal(_parts(), _document(_paragraph(num=value)), [0], "invalid_value")


def test_part_and_tree_inputs_are_unchanged_after_refusal():
    parts = _parts(level=_level(fmt="madeUp"))
    root = _document(_paragraph(num=1))
    original_parts, original_root = copy.deepcopy(parts), etree.tostring(root)
    _refusal(parts, root, [0], "unknown_number_format")
    assert original_parts == parts
    assert original_root == etree.tostring(root)


@pytest.mark.parametrize("limit,value", [
    ("MAX_NUMBERING_PARAGRAPHS", 1), ("MAX_NUMBERING_NODES", 2),
    ("MAX_NUMBERING_PART_BYTES", 5), ("MAX_NUMBERING_DEFINITIONS", 1),
    ("MAX_NUMBERING_STYLE_DEPTH", 1), ("MAX_NUMBERING_EFFECTS", 1),
])
def test_each_resource_bound_is_a_controlled_refusal(monkeypatch, limit, value):
    parts = _parts(styles=_style("A", base="B") + _style("B", num=1))
    root = _document(_paragraph(style="A"), _paragraph(style="A"))
    monkeypatch.setattr(numbering, limit, value)
    with pytest.raises(ApplyError) as raised:
        numbering.resolve_body_numbering(parts, root, [0, 1])
    assert raised.value.code == "resource_limit_exceeded"


def test_counter_overflow_is_a_controlled_refusal():
    with pytest.raises(ApplyError) as raised:
        numbering.resolve_body_numbering(_parts(level=_level(start=2_147_483_647)), _document(_paragraph(num=1), _paragraph(num=1)), [1])
    assert raised.value.code == "resource_limit_exceeded"


def test_numbering_formatting_proof_has_a_cumulative_work_bound(monkeypatch):
    parts = _parts(level=_level(extra='<w:rPr><w:b/><w:i/></w:rPr>'))
    monkeypatch.setattr(numbering, "MAX_NUMBERING_FORMATTING_WORK", 1)
    with pytest.raises(ApplyError) as raised:
        numbering.resolve_body_numbering(parts, _document(_paragraph(num=1)), [0])
    assert raised.value.code == "resource_limit_exceeded"


def test_container_ancestry_proof_is_bounded(monkeypatch):
    nested = '<w:sdt><w:sdtContent>' + _paragraph(num=1) + '</w:sdtContent></w:sdt>'
    monkeypatch.setattr(numbering, "MAX_NUMBERING_CONTAINER_DEPTH", 1)
    with pytest.raises(ApplyError) as raised:
        numbering.resolve_body_numbering(_parts(), _document(nested), [0])
    assert raised.value.code == "resource_limit_exceeded"


@pytest.mark.parametrize("payload", [b'<not-valid', b'<!DOCTYPE x><w:numbering xmlns:w="' + W_NS.encode() + b'"/>'])
def test_invalid_dependency_xml_is_a_controlled_refusal(payload):
    with pytest.raises(ApplyError) as raised:
        numbering.resolve_body_numbering({"word/numbering.xml": payload}, _document(_paragraph(num=1)), [0])
    assert raised.value.code == "paragraph_numbering_unsupported"


@pytest.mark.parametrize("indices", [[-1], [1], [True], [0, 0], ["0"]])
def test_invalid_queries_refuse(indices):
    with pytest.raises(ApplyError) as raised:
        numbering.resolve_body_numbering({}, _document(_paragraph()), indices)
    assert raised.value.code == "invalid_edit"


def test_deletion_projection_count_mismatch_is_not_inferred():
    root = _document(_paragraph(num=1), _paragraph(num=1))
    with pytest.raises(ApplyError) as raised:
        numbering.numbering_effects_for_deletion(_parts(), root, copy.deepcopy(root), 0)
    assert raised.value.metadata["reason"] == "invalid_projection"
