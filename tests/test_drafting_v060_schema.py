# SPDX-License-Identifier: Apache-2.0
"""Whole-paragraph producer contracts reject impossible or unbounded results."""

import copy

import jsonschema
import pytest

from veqtor_docx._drafting_paragraphs import MAX_PARAGRAPH_EDIT_STEPS
from veqtor_mcp._drafting_contract import _PARAGRAPH_STEP, _REPLACE_PARAGRAPH


DELETE = {
    "operation": "delete", "old_text": "Original text.", "new_text": "",
    "revision_ids": ["7"], "target_revision_id": None, "target_author": None,
}
INSERT = {
    "operation": "insert", "old_text": "", "new_text": "Replacement text.",
    "revision_ids": ["8"], "target_revision_id": None, "target_author": None,
}
OWN = {
    "operation": "edit_own_insertion", "old_text": "Earlier proposal.", "new_text": "",
    "revision_ids": ["6"], "target_revision_id": "6", "target_author": "Shilov, Ilya",
}


def _result():
    return {
        "index": 0, "operation": "replace_paragraph", "paragraph_index": 0,
        "old_text": DELETE["old_text"], "new_text": INSERT["new_text"],
        "revision_ids": ["7", "8"], "steps": [copy.deepcopy(DELETE), copy.deepcopy(INSERT)],
    }


@pytest.mark.parametrize("step", [
    DELETE, INSERT, OWN,
    {**OWN, "new_text": "Updated proposal.", "start": 0, "end": 17},
    {**OWN, "new_text": "Updated proposal.", "start": 0, "end": 17,
     "relocated_bookmarks": [{"name": "_GoBack", "id": "9", "position": "replacement_start"}]},
])
def test_actual_paragraph_step_shapes_are_valid(step):
    jsonschema.Draft202012Validator(_PARAGRAPH_STEP).validate(step)


@pytest.mark.parametrize("step", [
    {**DELETE, "operation": "replace"},
    {**DELETE, "operation": "counter"},
    {**DELETE, "old_text": ""},
    {**DELETE, "new_text": "Unexpected insertion"},
    {**DELETE, "target_revision_id": "6"},
    {**DELETE, "target_author": "Unexpected Author"},
    {**DELETE, "revision_ids": []},
    {**DELETE, "revision_ids": ["7", "8"]},
    {**INSERT, "old_text": "Unexpected deletion"},
    {**INSERT, "new_text": ""},
    {**INSERT, "target_revision_id": "6"},
    {**INSERT, "target_author": "Unexpected Author"},
    {**OWN, "old_text": ""},
    {**OWN, "target_revision_id": None},
    {**OWN, "target_author": None},
    {**OWN, "revision_ids": ["6", "7"]},
    {**OWN, "start": 0},
    {**OWN, "end": 17},
    {**OWN, "start": 0, "end": 0},
    {**DELETE, "start": 0, "end": 14},
    {**INSERT, "relocated_bookmarks": [
        {"name": "_GoBack", "id": "9", "position": "replacement_start"}]},
    {**OWN, "unexpected": True},
])
def test_impossible_paragraph_step_shapes_are_rejected(step):
    assert not jsonschema.Draft202012Validator(_PARAGRAPH_STEP).is_valid(step)


@pytest.mark.parametrize("field", ["steps", "revision_ids"])
@pytest.mark.parametrize("count", [0, MAX_PARAGRAPH_EDIT_STEPS + 1])
def test_paragraph_result_enforces_nonempty_bounded_steps_and_ids(field, count):
    result = _result()
    result[field] = ([copy.deepcopy(DELETE) for _ in range(count)]
                     if field == "steps" else [str(index) for index in range(count)])
    assert not jsonschema.Draft202012Validator(_REPLACE_PARAGRAPH).is_valid(result)


def test_paragraph_result_rejects_duplicate_aggregate_revision_ids():
    result = _result()
    result["revision_ids"] = ["7", "7"]
    assert not jsonschema.Draft202012Validator(_REPLACE_PARAGRAPH).is_valid(result)


@pytest.mark.parametrize("result", [
    _result(),
    {"index": 0, "operation": "replace_paragraph", "paragraph_index": 0,
     "old_text": "", "new_text": INSERT["new_text"],
     "revision_ids": ["8"], "steps": [INSERT]},
    {"index": 0, "operation": "replace_paragraph", "paragraph_index": 0,
     "old_text": OWN["old_text"], "new_text": "",
     "revision_ids": ["6"], "steps": [OWN]},
])
def test_plain_replacement_blank_fill_and_own_deletion_results_are_valid(result):
    jsonschema.Draft202012Validator(_REPLACE_PARAGRAPH).validate(result)
