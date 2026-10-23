# SPDX-License-Identifier: Apache-2.0
"""Wire contracts retain complete row selection and atomic separator facts."""

import copy

import jsonschema
import pytest

from veqtor_mcp._drafting_contract import (
    DECISION_PREVIEW, DECISION_TARGET, OPERATIONS, _DELETE_TABLE_ROW,
    _INSERT_TABLE_ROW,
)


ROW_REF = {
    "schema_version": "table_row_ref.v1", "ref_type": "row",
    "file_sha256": "a" * 64, "part_name": "word/document.xml",
    "container_policy": "canonical_body_flow_v1",
    "fingerprint_policy": "element_c14n_without_comments_v1",
    "element_sha256": "b" * 64, "table_index": 0, "row_index": 1,
}
REV_REF = {
    "schema_version": "revision_ref.v1", "file_sha256": "a" * 64,
    "part_name": "word/document.xml", "element_path": [0, 1, 0, 0],
    "element_sha256": "c" * 64, "owner_sha256": "d" * 64,
    "fingerprint_policy": "element_c14n_without_comments_v1",
}


def _result(kind="insert_table_row", separator=False):
    result = {
        "index": 0, "operation": kind, "cell_texts": ["Clause", "Пункт"],
        "row_count": 2 if separator else 1, "revision_ids": ["7", "8", "9"],
        "row_numbering": {"accepted": [], "rejected": []},
        "separator_included": separator,
        "row_revision_groups": [{"row_mark_revision_id": "7", "content_revision_ids": ["8", "9"],
                                 "cell_texts": ["Clause", "Пункт"], "role": "clause"}],
    }
    if kind == "insert_table_row":
        result["position"] = "after"
    if separator:
        result["revision_ids"].append("10")
        result["row_revision_groups"].append({"row_mark_revision_id": "10", "content_revision_ids": [],
                                               "cell_texts": ["", ""], "role": "separator"})
    return result


@pytest.mark.parametrize("kind", ["insert_table_row", "delete_table_row"])
@pytest.mark.parametrize("separator", [False, True])
def test_atomic_separator_facts_are_coherent(kind, separator):
    schema = _INSERT_TABLE_ROW if kind == "insert_table_row" else _DELETE_TABLE_ROW
    validator = jsonschema.Draft202012Validator(schema)
    result = _result(kind, separator)
    validator.validate(result)
    malformed = copy.deepcopy(result)
    malformed["separator_included"] = not separator
    assert not validator.is_valid(malformed)
    malformed = copy.deepcopy(result)
    malformed["row_revision_groups"][0]["role"] = "separator"
    assert not validator.is_valid(malformed)


def test_empty_row_content_does_not_require_invented_text_revision():
    target = {"kind": "row_structure", "row_mark_ref": REV_REF, "content_revision_refs": []}
    jsonschema.Draft202012Validator(DECISION_TARGET).validate(target)
    malformed = {**target, "revision_id": "7"}
    assert not jsonschema.Draft202012Validator(DECISION_TARGET).is_valid(malformed)


def test_row_inputs_require_complete_bilingual_text_and_source_bound_reference():
    operation = {"kind": "insert_table_row", "row_ref": ROW_REF,
                 "template_row_ref": ROW_REF, "position": "before", "cell_texts": ["", "Пункт"]}
    validator = jsonschema.Draft202012Validator(OPERATIONS)
    validator.validate([operation])
    for malformed in (
        {**operation, "cell_texts": ["Only one language"]},
        {**operation, "cell_texts": ["One", "Two", "Three"]},
        {**operation, "row_ref": {"row_index": 1}},
        {**operation, "separator": {"position": "after"}},
        {**operation, "infer_separator": True},
    ):
        assert not validator.is_valid([malformed])


def test_row_preview_bound_accounts_for_both_cells_without_expanding_other_kinds():
    preview = {
        "operation": "decide_revision", "decision": "accept", "revision_kind": "row_insertion",
        "members": [{"kind": "ins", "id": "7", "author": "Fictional Author", "date": None}],
        "current_text": "a" * 20_000 + "\n" + "b" * 20_000,
        "accepted_text": None, "rejected_text": None,
        "current_properties": None, "previous_properties": None,
        "accept_refusal_code": None, "reject_refusal_code": None,
        "fixed_width_validation_scope": "not_applicable", "table_validation_scope": "not_applicable",
        "table_group_members": [], "row_numbering": {"accepted": [], "rejected": []},
    }
    validator = jsonschema.Draft202012Validator(DECISION_PREVIEW)
    validator.validate(preview)
    assert not validator.is_valid({**preview, "current_text": preview["current_text"] + "x"})
    assert not validator.is_valid({**preview, "revision_kind": "ins"})
