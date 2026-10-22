# SPDX-License-Identifier: Apache-2.0
"""Synthetic F6 evidence: explicit pairs, conservative coverage and no writes."""
import copy
import hashlib
import json
from collections import Counter
from types import MappingProxyType

import jsonschema
import pytest
from lxml import etree
from mcp.client import Client

from test_table_map import _cell, _p, _payload, _row, _table
from veqtor_docx import bilingual as bi, drafting
from veqtor_docx._ooxml import ResourceLimitError, w
from veqtor_mcp import server
from veqtor_mcp._bilingual_contract import CHECK_RESULT, INVENTORY_RESULT

EN = {"decimal_separator": ".", "grouping_separator": ",", "date_order": "reject_ambiguous"}
RU = {"decimal_separator": ",", "grouping_separator": " ", "date_order": "reject_ambiguous"}


def _setup(left="1,000.50 USD", right="1 000,50 USD", *, body=None):
    payload = _payload(body if body is not None else _table(_row(_cell(left), _cell(right))))
    inventory = bi.read_tables_payload(payload)
    items = inventory["items"]
    table = next(i for i in items if i["kind"] == "table")
    row = next(i for i in items if i["kind"] == "row")
    cells = [i for i in items if i["kind"] == "cell"]
    paragraphs = [i for i in items if i["kind"] == "paragraph"]
    pair = {"pair_id": "payment", "table_ref": table["table_ref"], "row_ref": row["row_ref"],
            "left": {"cell_ref": cells[0]["cell_ref"], "language": "en", "grid_column": 0, "format": copy.deepcopy(EN)},
            "right": {"cell_ref": cells[1]["cell_ref"], "language": "ru", "grid_column": 1, "format": copy.deepcopy(RU)},
            "conditions": []}
    return payload, inventory, pair, paragraphs


def _condition(pair, paragraphs, *, left=None, right=None, condition_id="amount", indices=(0, 1)):
    condition = {"condition_id": condition_id}
    for side, quote, index in zip(("left", "right"), (left, right), indices, strict=True):
        p = paragraphs[index]
        text = p["text"]
        quote = text if quote is None else quote
        start = text.index(quote)
        condition[side] = {"paragraph_ref": p["paragraph_ref"], "start": start, "end": start + len(quote), "quote": quote}
    pair["conditions"].append(condition)
    return condition


def _check(left, right, *, left_fmt=None, right_fmt=None):
    payload, _, pair, paragraphs = _setup(left, right)
    if left_fmt:
        pair["left"]["format"] = left_fmt
    if right_fmt:
        pair["right"]["format"] = right_fmt
    _condition(pair, paragraphs)
    result = bi.check_conditions_payload(payload, [pair])
    bi.validate_bilingual_result(result)
    server._validated_success_result("check_bilingual_conditions", result)
    return result, result["pairs"][0]["conditions"][0]


def test_inventory_preserves_empty_multiple_paragraphs_and_hash_bound_cell_order():
    table = _table(_row(_cell("A", "", "Clause 5.2 refers to section 9."), _cell("Б")))
    payload, inventory, _, paragraphs = _setup(body=table)
    original = bytes(payload)
    assert [p["text"] for p in paragraphs] == ["A", "", "Clause 5.2 refers to section 9.", "Б"]
    assert inventory["counts"]["empty_table_paragraphs"] == 1
    assert inventory["counts"]["text_references"] == 2
    assert inventory["file_sha256"] == hashlib.sha256(payload).hexdigest()
    assert all(p["cell_ref"]["file_sha256"] == inventory["file_sha256"] for p in paragraphs)
    cell = next(i for i in inventory["items"] if i["kind"] == "cell")
    assert cell["paragraph_refs"] == [p["paragraph_ref"] for p in paragraphs[:3]]
    assert [c["kind"] for c in cell["content"]] == ["paragraph"] * 3
    normalized = server._drafting_result("read_document_tables", inventory)
    jsonschema.Draft202012Validator(INVENTORY_RESULT).validate(normalized)
    bi.validate_bilingual_result(inventory)
    assert payload == original


@pytest.mark.parametrize("left,right,status", [
    ("1,000.50 USD", "1 000,50 USD", "numeric_values_and_units_match"),
    ("1,000.50 USD", "1 000,50 EUR", "mismatch"),
    ("1 million USD", "1 000 000 USD", "numeric_values_and_units_match"),
    ("1 million USD", "1 000 USD", "mismatch"),
    ("0.10000000000000000001%", "0,10000000000000000000%", "mismatch"),
    ("12.50%", "12,500%", "numeric_values_and_units_match"),
    (".5%", "0,5%", "numeric_values_and_units_match"),
    (".5%", "5%", "mismatch"),
    ("-.5%", "-0,5%", "numeric_values_and_units_match"),
    ("3 kg", "3 мг", "mismatch"),
    ("45 calendar days", "30 календарных дней", "mismatch"),
    ("30 business days", "30 рабочих дней", "numeric_values_and_units_match"),
    ("30 calendar days", "30 рабочих дней", "mismatch"),
    ("30 days", "30 дней", "unsupported_format"),
    ("$100", "100 USD", "unsupported_format"),
    ("100 cm", "100 mm", "unsupported_format"),
    ("kg 100", "мг 100", "unsupported_format"),
    ("five hundred USD", "пятьсот рублей", "unsupported_format"),
    ("5% per day", "5% в день", "unsupported_format"),
    ("5% annually", "5% годовых", "unsupported_format"),
    ("10 kg/day", "10 кг/день", "unsupported_format"),
    ("2026-11-23", "2026-11-23", "numeric_values_and_units_match"),
    ("01/02/2026", "01/02/2026", "unsupported_format"),
    ("31/02/2026", "31/02/2026", "unsupported_format"),
    ("1 January 2026", "1 января 2026", "unsupported_format"),
    ("one thousand USD", "одна тысяча USD", "unsupported_format"),
    ("Amount = rate * volume", "Сумма = ставка * объем", "unsupported_format"),
    ("10 * 2 USD", "20 USD", "unsupported_format"),
    ("10-5 USD", "5 USD", "unsupported_format"),
    ("Payment on acceptance", "Оплата по приемке", "not_established"),
    (" ", " ", "not_established"),
])
def test_exact_single_condition_results(left, right, status):
    result, condition = _check(left, right)
    assert condition["status"] == status
    assert result["obligation_equivalence"] == "not_claimed"
    assert result["coverage"]["document_wide_numeric_coverage"] == "not_claimed"


def test_explicit_date_order_and_decimal_scale_do_not_round_or_infer_locale():
    left_fmt = {**EN, "date_order": "MDY"}
    right_fmt = {**RU, "date_order": "DMY"}
    _, condition = _check("02/01/2026", "01.02.2026", left_fmt=left_fmt, right_fmt=right_fmt)
    assert condition["status"] == "numeric_values_and_units_match"
    _, condition = _check("02/01/2026", "01.02.2026", left_fmt={**EN, "date_order": "DMY"}, right_fmt=right_fmt)
    assert condition["status"] == "mismatch"
    _, condition = _check("1,000", "1,000", right_fmt=RU)
    assert condition["status"] == "mismatch"  # 1000 versus 1, explicitly selected formats


@pytest.mark.parametrize("group", [" ", "\u00a0", "\u202f"])
def test_explicit_grouping_character(group):
    _, condition = _check("1,000.50 USD", f"1{group}000,50 USD", right_fmt={**RU, "grouping_separator": group})
    assert condition["status"] == "numeric_values_and_units_match"


def test_swapped_obligations_never_match_as_multiset_and_explicit_pairs_find_mismatch():
    payload, _, pair, ps = _setup("Pay in 30 calendar days; notify in 60 calendar days.",
                                 "Оплата в 60 календарных дней; уведомление в 30 календарных дней.")
    _condition(pair, ps)
    result = bi.check_conditions_payload(payload, [pair])["pairs"][0]
    assert result["conditions"][0]["status"] == "not_established"
    assert result["coverage"]["paired_values"] == 0
    assert result["coverage"]["unpaired_values"] == 4
    pair["conditions"] = []
    _condition(pair, ps, left="30 calendar days", right="60 календарных дней", condition_id="payment")
    _condition(pair, ps, left="60 calendar days", right="30 календарных дней", condition_id="notice")
    result = bi.check_conditions_payload(payload, [pair])["pairs"][0]
    assert [c["status"] for c in result["conditions"]] == ["mismatch", "mismatch"]
    assert result["coverage"]["paired_values"] == 4
    assert result["coverage"]["conditions_with_match"] == 0


def test_unselected_values_number_words_and_unknown_formats_remain_visible():
    payload, _, pair, ps = _setup("100 USD; one extra payment; 7 kg", "100 USD; два платежа; 8 кг")
    _condition(pair, ps, left="100 USD", right="100 USD")
    result = bi.check_conditions_payload(payload, [pair])["pairs"][0]
    assert result["conditions"][0]["status"] == "numeric_values_and_units_match"
    assert result["coverage"] == {"values": 6, "unsupported_values": 2, "unpaired_values": 4,
                                  "paired_values": 2, "selected_conditions": 1, "conditions_with_match": 1}
    assert [v["quote"] for v in result["values"]["left"]] == ["100 USD", "one", "7 kg"]
    pair["conditions"] = []
    assert bi.check_conditions_payload(payload, [pair])["pairs"][0]["coverage"]["unpaired_values"] == 6


def test_multiple_paragraphs_do_not_pair_by_neighboring_global_indices():
    payload, _, pair, ps = _setup(body=_table(_row(_cell("100 USD", "30 calendar days"), _cell("100 USD", "60 календарных дней"))))
    _condition(pair, ps, indices=(1, 3))
    result = bi.check_conditions_payload(payload, [pair])["pairs"][0]
    assert result["conditions"][0]["status"] == "mismatch"
    assert result["coverage"]["unpaired_values"] == 2


@pytest.mark.parametrize("mutation", ["stale_hash", "wrong_cell", "wrong_grid", "wrong_quote", "half_value", "duplicate_condition", "duplicate_pair", "extra", "same_separator"])
def test_invalid_or_stale_explicit_selection_refuses(mutation):
    payload, _, pair, ps = _setup()
    condition = _condition(pair, ps)
    pairs = [pair]
    if mutation == "stale_hash":
        pair["left"]["cell_ref"]["file_sha256"] = "0" * 64
    elif mutation == "wrong_cell":
        condition["left"]["paragraph_ref"] = ps[1]["paragraph_ref"]
    elif mutation == "wrong_grid":
        pair["left"]["grid_column"] = 1
    elif mutation == "wrong_quote":
        condition["left"]["quote"] = "wrong"
    elif mutation == "half_value":
        condition["left"].update(start=1, quote=ps[0]["text"][1:])
    elif mutation == "duplicate_condition":
        pair["conditions"].append(copy.deepcopy(condition))
    elif mutation == "duplicate_pair":
        pairs.append(copy.deepcopy(pair))
    elif mutation == "extra":
        pair["left"]["xpath"] = "forbidden"
    elif mutation == "same_separator":
        pair["left"]["format"]["grouping_separator"] = "."
    with pytest.raises(bi.BilingualError):
        bi.check_conditions_payload(payload, pairs)


@pytest.mark.parametrize("kind", ["horizontal", "vertical", "nested", "gap"])
def test_merged_nested_or_gapped_geometry_is_visible_and_refuses_pairing(kind):
    left = _cell("100 USD", horizontal="restart" if kind == "horizontal" else None,
                 vertical="restart" if kind == "vertical" else None)
    if kind == "nested":
        left.insert(0, _table(_row(_cell("Nested"), _cell(""))))
    row = _row(left, _cell("100 USD"), before="1" if kind == "gap" else None)
    payload, inventory, pair, _ = _setup(body=_table(row, columns=3 if kind == "gap" else 2))
    table = next(i for i in inventory["items"] if i["kind"] == "table")
    assert not table["simple_rectangular"]
    with pytest.raises(bi.BilingualError, match="bilingual_pairing_unsupported"):
        bi.check_conditions_payload(payload, [pair])


def test_reference_report_marks_explicit_affected_scope_and_raw_fields_without_updates():
    p = _p("See clause 5.2 and п. 7.3.")
    field = etree.SubElement(p, w("fldSimple"))
    field.set(w("instr"), " REF target ")
    field.append(_p("Displayed"))
    payload = _payload(_table(_row(_cell("A"), _cell("Б"))), p)
    first = bi.read_tables_payload(payload)
    p_ref = next(i["paragraph_ref"] for i in first["items"] if i["kind"] == "paragraph" and i["text"].startswith("See"))
    report = bi.read_tables_payload(payload, affected_paragraph_refs=[p_ref])
    assert report["reference_report"]["target_validity"] == "not_assessed"
    assert report["reference_report"]["semantic_reference_completeness"] == "not_claimed"
    observations = [i for i in report["items"] if i["kind"] in {"text_reference", "field"}]
    assert observations and all(i["within_affected_scope"] for i in observations)
    assert next(i for i in observations if i["kind"] == "field")["instruction"] == " REF target "
    assert any(not i["numeric_scan_supported"] for i in report["items"] if i["kind"] == "paragraph")
    assert report["file_sha256"] == hashlib.sha256(payload).hexdigest()


def test_whole_result_limits_include_long_late_quotes_and_do_not_truncate(monkeypatch):
    payload, inventory, _, _ = _setup("Short", "x" * 40_000)
    assert max(len(i["text"]) for i in inventory["items"] if i["kind"] == "paragraph") == 40_000
    size = len(json.dumps(inventory, ensure_ascii=True, separators=(",", ":")).encode()) + 1024
    monkeypatch.setattr(bi, "LIMITS", MappingProxyType({**bi.LIMITS, "result_bytes": size - 100}))
    with pytest.raises(ResourceLimitError):
        bi.read_tables_payload(payload)


@pytest.mark.parametrize("mutation", ["count", "source", "quote", "false_match"])
def test_mcp_exit_checks_counts_sources_quotes_and_false_matches(mutation):
    result, _ = _check("100 USD", "200 USD")
    pair = result["pairs"][0]
    if mutation == "count":
        pair["coverage"]["values"] += 1
    elif mutation == "source":
        pair["values"]["left"][0]["paragraph_ref"]["file_sha256"] = "0" * 64
    elif mutation == "quote":
        pair["values"]["left"][0]["quote"] = "199 USD"
    else:
        pair["conditions"][0]["status"] = "numeric_values_and_units_match"
        pair["coverage"]["conditions_with_match"] = 1
    with pytest.raises(server._OutputContractError):
        server._validated_success_result("check_bilingual_conditions", result)


def test_s04_existing_atomic_batch_edits_both_columns_and_refuses_partial_change(tmp_path):
    payload, _, pair, ps = _setup("100 USD", "100 USD")
    path = tmp_path / "synthetic.docx"
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    operations = [{"kind": "replace_text", "paragraph_ref": p["paragraph_ref"], "old_text": "100 USD", "new_text": "200 USD"} for p in ps]
    profile = {"author": "Synthetic Reviewer", "initials": "SR", "producer_build": "synthetic-test"}
    invalid = copy.deepcopy(operations)
    invalid[1]["old_text"] = "missing text"
    failed = drafting.preflight_document_edits(str(path), digest, invalid, **profile)
    assert not failed["batch_applicable"]
    assert path.read_bytes() == payload
    good = drafting.preflight_document_edits(str(path), digest, operations, **profile)
    assert good["batch_applicable"], good
    saved = drafting.apply_document_edits(str(path), digest, operations, good["preflight_proof"], **profile)
    assert saved["output_sha256"] != digest
    refreshed = bi.read_document_tables(str(path), saved["output_sha256"])
    assert [p["text"] for p in refreshed["items"] if p["kind"] == "paragraph"] == ["200 USD", "200 USD"]
    assert path.with_name("synthetic.docx").read_bytes() != payload
    with pytest.raises(bi.BilingualError, match="bilingual_reference_mismatch"):
        bi.check_conditions_payload(path.read_bytes(), [pair])


@pytest.mark.anyio
async def test_mcp_tools_are_read_only_closed_schema_valid_and_have_no_journal(tmp_path):
    payload, _, pair, ps = _setup()
    _condition(pair, ps)
    path = tmp_path / "synthetic.docx"
    path.write_bytes(payload)
    async with Client(server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name, args, schema in [
            ("read_document_tables", {"path": str(path)}, INVENTORY_RESULT),
            ("check_bilingual_conditions", {"path": str(path), "file_sha256": hashlib.sha256(payload).hexdigest(), "cell_pairs": [pair]}, CHECK_RESULT),
        ]:
            assert tools[name].annotations.read_only_hint
            assert tools[name].output_schema["additionalProperties"] is False
            result = await client.call_tool(name, args)
            assert not result.is_error, result
            data = result.structured_content
            data = data.get("result", data)
            jsonschema.Draft202012Validator(schema).validate(data)
            assert data["record_status"] == "disabled" and data["record_id"] is None
        missing = await client.call_tool("check_bilingual_conditions", {"path": str(path), "cell_pairs": [pair]})
        assert missing.is_error
    assert path.read_bytes() == payload
    assert Counter(p.name for p in tmp_path.iterdir()) == {"synthetic.docx": 1}


def test_package_inventory_includes_bilingual_core_schema_and_guide():
    import tomllib
    from pathlib import Path
    targets = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]
    for target in ("wheel", "sdist"):
        assert "/src/veqtor_docx/bilingual.py" in targets[target]["include"]
        assert "/src/veqtor_mcp/_bilingual_contract.py" in targets[target]["include"]
    assert "/docs/BILINGUAL_COLUMNS.md" in targets["sdist"]["include"]


REVIEW_UNSUPPORTED = [
    ("−100 USD", "100 USD", "−100 USD"),
    ("﹣100 USD", "100 USD", "﹣100 USD"),
    ("－100 USD", "100 USD", "－100 USD"),
    ("- 100 USD", "100 USD", "- 100 USD"),
    ("±100 USD", "100 USD", "±100 USD"),
    ("+/-100 USD", "100 USD", "+/-100 USD"),
    ("−‎100 USD", "100 USD", "−‎100 USD"),
    ("− USD 100", "100 USD", "− USD 100"),
    ("100 kg mg", "100 kg", "100 kg mg"),
    ("kg 100 USD", "100 USD", "kg 100 USD"),
    ("100 USD kg", "100 USD", "100 USD kg"),
    ("30 calendar days business days", "30 календарных дней", "30 calendar days business days"),
    ("10 kg∕day", "10 kg", "10 kg∕day"),
    ("10 kg⁄day", "10 kg", "10 kg⁄day"),
    ("10 kg／day", "10 kg", "10 kg／day"),
    ("10 kg⧸day", "10 kg", "10 kg⧸day"),
    ("1'000 USD", "1 000 USD", "1'000 USD"),
    ("1 000 USD", "1 000 USD", "1 000 USD"),
    ("1·000 USD", "1 000 USD", "1·000 USD"),
    ("1 23 USD", "23 USD", "1 23 USD"),
    ("1e+000 USD", "0 USD", "1e+000 USD"),
    ("3 hours", "3 части", "3 hours"),
    ("100 hours", "100 частей", "100 hours"),
    ("100 years", "100 годных изделий", "100 years"),
    ("INV2026-11-23", "2026-11-23", "INV2026-11-23"),
    ("2026-11-23A", "2026-11-23", "2026-11-23A"),
]
REVIEW_PARTIAL = [
    ("1'000 USD", "000 USD", "0 USD"),
    ("1 000 USD", "000 USD", "0 USD"),
    ("1 23 USD", "23 USD", "23 USD"),
    ("1·000 USD", "000 USD", "0 USD"),
    ("1_000 USD", "000 USD", "0 USD"),
    ("−100 USD", "100 USD", "100 USD"),
    ("kg 100 USD", "100 USD", "100 USD"),
    ("100 kg mg", "100 kg", "100 kg"),
    ("INV2026-11-23", "2026-11-23", "2026-11-23"),
    ("2026-11-23A", "2026-11-23", "2026-11-23"),
    ("100 USD/200 USD", "200 USD", "200 USD"),
    ("−‎100 USD", "100 USD", "100 USD"),
]


@pytest.mark.parametrize("left,right,raw", REVIEW_UNSUPPORTED)
def test_review_regressions_keep_unsupported_lexical_context(left, right, raw):
    result, condition = _check(left, right)
    assert condition["status"] == "unsupported_format"
    pair = result["pairs"][0]
    assert pair["coverage"]["unsupported_values"] >= 1
    assert pair["coverage"]["paired_values"] == 0
    assert len(pair["values"]["left"]) == 1
    assert pair["values"]["left"][0]["quote"] == raw
    for side, text in (("left", left), ("right", right)):
        for value in pair["values"][side]:
            assert text[value["start"]:value["end"]] == value["quote"]


@pytest.mark.parametrize("source,quote,right", REVIEW_PARTIAL)
def test_review_partial_quotes_cannot_escape_a_maximal_candidate(source, quote, right):
    payload, _, pair, ps = _setup(source, right)
    _condition(pair, ps, left=quote)
    with pytest.raises(bi.BilingualError) as caught:
        bi.check_conditions_payload(payload, [pair])
    assert caught.value.code == "bilingual_partial_value"


@pytest.mark.parametrize("left,right", [
    ("-100 USD", "-100 USD"), ("+100 USD", "100 USD"),
    ("3 hours", "3 часа"), ("3 years", "3 года"),
    ("10 packs", "10 упаковок"), ("5 months", "5 месяцев"),
    ("30 calendar days", "30 календарных дней"),
    ("30 working days", "30 рабочих дней"),
    ("USD 100", "100 USD"), ("Pay 100 USD on acceptance.", "Оплата 100 USD при приемке."),
])
def test_review_positive_controls_preserve_supported_sign_units_and_prose(left, right):
    _, condition = _check(left, right)
    assert condition["status"] == "numeric_values_and_units_match"


def test_distinct_values_clause_labels_and_reference_report_remain_distinct():
    left = "Clause 5.2: 100 kg; 200 mg and 300 USD."
    right = "Пункт 5.2: 100 кг; 200 мг и 300 USD."
    payload, inventory, pair, ps = _setup(left, right)
    for index, (a, b) in enumerate((("100 kg", "100 кг"), ("200 mg", "200 мг"), ("300 USD", "300 USD"))):
        _condition(pair, ps, left=a, right=b, condition_id=f"quantity-{index}")
    result = bi.check_conditions_payload(payload, [pair])["pairs"][0]
    assert all(c["status"] == "numeric_values_and_units_match" for c in result["conditions"])
    assert result["coverage"]["paired_values"] == 6
    assert inventory["counts"]["text_references"] == 2
    # Punctuation followed by whitespace can separate a manual clause label
    # from its amount; it is not silently inserted into a grouping convention.
    payload, _, pair, ps = _setup("1. 100 USD", "1. 100 USD")
    _condition(pair, ps, left="100 USD", right="100 USD")
    result = bi.check_conditions_payload(payload, [pair])["pairs"][0]
    assert result["conditions"][0]["status"] == "numeric_values_and_units_match"
    assert result["coverage"]["unpaired_values"] == 2


@pytest.mark.anyio
async def test_review_regressions_through_real_mcp_and_advertised_schema(tmp_path):
    async with Client(server.mcp) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "check_bilingual_conditions")
        validator = jsonschema.Draft202012Validator(tool.output_schema)
        for index, (left, right, _) in enumerate(REVIEW_UNSUPPORTED):
            payload, _, pair, ps = _setup(left, right)
            _condition(pair, ps)
            source = tmp_path / f"unsupported-{index}.docx"
            source.write_bytes(payload)
            result = await client.call_tool("check_bilingual_conditions", {
                "path": str(source), "file_sha256": hashlib.sha256(payload).hexdigest(), "cell_pairs": [pair]})
            assert not result.is_error, result
            data = result.structured_content
            data = data.get("result", data)
            validator.validate(data)
            assert data["pairs"][0]["conditions"][0]["status"] == "unsupported_format"
            assert data["pairs"][0]["coverage"]["unsupported_values"] >= 1
            assert data["record_status"] == "disabled"
            assert source.read_bytes() == payload
        for index, (text, quote, right) in enumerate(REVIEW_PARTIAL):
            payload, _, pair, ps = _setup(text, right)
            _condition(pair, ps, left=quote)
            source = tmp_path / f"partial-{index}.docx"
            source.write_bytes(payload)
            result = await client.call_tool("check_bilingual_conditions", {
                "path": str(source), "file_sha256": hashlib.sha256(payload).hexdigest(), "cell_pairs": [pair]})
            assert result.is_error
            assert "bilingual_partial_value" in result.content[0].text
            assert source.read_bytes() == payload
