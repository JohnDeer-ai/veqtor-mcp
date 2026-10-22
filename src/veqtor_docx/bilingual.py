# SPDX-License-Identifier: Apache-2.0
"""Bounded, read-only evidence for explicitly selected bilingual conditions.

The caller selects both cells and exact condition spans. No paragraph adjacency,
translation, obligation correspondence or whole-document equivalence is inferred.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from types import MappingProxyType

import jsonschema

from ._ooxml import DocxError, ResourceLimitError, w
from .inspect import _load_snapshot_from_payload, _paragraph_ref
from .round_snapshots import capture_document_snapshot
from .table_map import build_table_map

LIMITS = MappingProxyType({
    "paragraphs": 2000, "paragraph_chars": 50_000, "text_chars": 500_000, "result_bytes": 8 * 1024 * 1024,
    "cell_pairs": 100, "conditions_per_pair": 100, "values": 20_000,
    "numeric_chars": 100,
})
POLICY = "explicit_bilingual_conditions.v1"
SHA = {"type": "string", "pattern": r"^[0-9a-f]{64}(?![\s\S])"}
COUNT = {"type": "integer", "minimum": 0}
TEXT = {"type": "string"}


def obj(**fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def array(schema, maximum):
    return {"type": "array", "items": schema, "maxItems": maximum}


REFS = {}
for _kind, _schema in (("table", "table_ref.v1"), ("row", "table_row_ref.v1"), ("cell", "table_cell_ref.v1")):
    _fields = dict(schema_version={"const": _schema}, ref_type={"const": _kind}, file_sha256=SHA,
                   part_name={"const": "word/document.xml"}, container_policy={"const": "canonical_body_flow_v1"},
                   fingerprint_policy={"const": "element_c14n_without_comments_v1"},
                   element_sha256=SHA, table_index=COUNT)
    if _kind in {"row", "cell"}:
        _fields["row_index"] = COUNT
    if _kind == "cell":
        _fields["cell_index"] = COUNT
    REFS[_kind] = obj(**_fields)

# Shared with the MCP schema without importing the transport layer.
PARAGRAPH_REF = obj(
    schema_version={"const": "paragraph_ref.v1"}, ref_type={"const": "paragraph"}, file_sha256=SHA,
    part_name={"const": "word/document.xml"}, paragraph_index=COUNT,
    reading_mode={"const": "accepted_current_v1"},
    paragraph_text_sha256=SHA, container_policy={"const": "canonical_body_flow_v1"},
)
FORMAT = obj(
    decimal_separator={"enum": [".", ","]},
    grouping_separator={"enum": [None, ".", ",", " ", "\u00a0", "\u202f"]},
    date_order={"enum": ["DMY", "MDY", "YMD", "reject_ambiguous"]},
)
SIDE = obj(cell_ref=REFS["cell"], language={**TEXT, "minLength": 1, "maxLength": 64},
           grid_column={**COUNT, "maximum": 255}, format=FORMAT)
SPAN = obj(paragraph_ref=PARAGRAPH_REF, start=COUNT, end=COUNT,
           quote={**TEXT, "minLength": 1, "maxLength": 50_000})
CONDITION = obj(condition_id={**TEXT, "minLength": 1, "maxLength": 64}, left=SPAN, right=SPAN)
PAIR = obj(pair_id={**TEXT, "minLength": 1, "maxLength": 64}, table_ref=REFS["table"],
           row_ref=REFS["row"], left=SIDE, right=SIDE,
           conditions=array(CONDITION, LIMITS["conditions_per_pair"]))
SELECTIONS = {**array(PAIR, LIMITS["cell_pairs"]), "minItems": 1}

_REF = re.compile(r"(?:\b(?:clauses?|sections?|articles?|пункт(?:а|е|ы|ов)?|стать(?:я|и|е))\s+|\bп\.\s*|§\s*)\d+(?:\.\d+)*", re.I)
_NUMBER = r"[+-]?(?:\d+(?:[.,]\d+|[ \u00a0\u202f]\d{3}(?!\d))*|[.,]\d+)"
_CURRENCY = r"USD|EUR|GBP|RUB|CNY|CHF|JPY|CAD|AUD|доллар(?:ов|а)?|евро|руб(?:лей|ля|ль)?\.?|[$€£₽¥]"
_SCALE = r"thousand|million|billion|тыс\.?|тысяч(?:а|и)?|млн\.?|миллион(?:а|ов)?|млрд\.?|миллиард(?:а|ов)?"
_DAYS = r"(?:(?:calendar|business|working|календарн\w*|рабоч\w*)\s+)?(?:days?|дн(?:ей|я)|день)"
_UNIT = rf"{_CURRENCY}|%|percent|процент(?:а|ов)?|{_DAYS}|weeks?|недел\w*|months?|месяц\w*|years?|лет|год\w*|hours?|час\w*|kg|mg|ml|кг|мг|мл|grams?|грамм\w*|litres?|liters?|литр\w*|packs?|упаков\w*|pieces?|штук\w*|шт\.?"
_VALUE = re.compile(rf"(?<![\w\d])(?:(?P<prefix>{_CURRENCY})\s*)?(?P<number>{_NUMBER})(?:\s*(?P<scale>{_SCALE})(?!\w))?(?:\s*(?P<unit>{_UNIT})(?!\w))?", re.I)
_DATE = re.compile(r"(?<!\d)(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})(?!\d)")
_WORDS = re.compile(r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|ноль|один|одна|одно|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|двадцать|тридцать|сорок|пятьдесят|шестьдесят|семьдесят|восемьдесят|девяносто|сто|двести|триста|тысяч\w*|миллион\w*|миллиард\w*)\b", re.I)
_FORMULA = re.compile(r"[=×÷*^]|\d\s*[-+/]\s*\d")
_NAMED_DATE = re.compile(r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December|января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+\d{4}\b", re.I)
_LIMITATIONS = [
    "caller_selected_condition_correspondence_not_verified",
    "numeric_agreement_is_not_translation_or_obligation_equivalence",
    "one_supported_value_per_selected_span_required_no_multiset_matching",
    "lexical_digit_extraction_not_complete_language_or_formula_analysis",
    "number_word_candidates_conservative_dictionary_not_full_amounts_in_words",
    "no_currency_conversion_unit_conversion_or_formula_evaluation",
    "canonical_main_body_current_text_only_not_rendered_or_legal_effect",
    "read_only_no_negotiation_journal",
]


class BilingualError(DocxError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(f"{code}: {detail}")


def _fail(code="invalid_bilingual_selection", detail="selection does not identify exact unambiguous snapshot content"):
    raise BilingualError(code, detail)


def _bound(key, observed):
    if observed > LIMITS[key]:
        raise ResourceLimitError(f"bilingual_{key}", "complete bilingual report exceeds fixed limit",
                                 allowed_count=LIMITS[key], observed_count=observed)


def _json_bound(result):
    _bound("result_bytes", len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()) + 1024)
    return result


def _read(payload):
    snapshot = _load_snapshot_from_payload(payload, path="")
    _bound("paragraphs", len(snapshot.paragraphs))
    for paragraph in snapshot.paragraphs:
        _bound("paragraph_chars", len(paragraph.text))
    _bound("text_chars", sum(len(p.text) for p in snapshot.paragraphs))
    mapped = build_table_map(payload)
    refs = [_paragraph_ref(snapshot, p) for p in snapshot.paragraphs]
    return snapshot, mapped, refs


def _paragraph_index(ref, refs):
    if not isinstance(ref, dict) or type(ref.get("paragraph_index")) is not int:
        _fail()
    index = ref["paragraph_index"]
    if not 0 <= index < len(refs) or ref != refs[index]:
        _fail("bilingual_reference_mismatch")
    return index


def _unsafe_paragraph(paragraph):
    # Unsupported field/range/wrapper content cannot silently yield a match on
    # its remaining visible digits. Current tracked text is still the disclosed
    # mechanical reading, never a decision that it is legally effective.
    names = {"fldChar", "fldSimple", "instrText", "sdt", "drawing", "object", "sym",
             "footnoteReference", "endnoteReference", "AlternateContent"}
    return any(isinstance(n.tag, str) and n.tag.rsplit("}", 1)[-1] in names
               for n in paragraph.element.iter())


def read_tables_payload(payload: bytes, *, affected_paragraph_refs=None):
    """Return one complete bounded inventory; never truncate a late quote."""
    snapshot, mapped, refs = _read(payload)
    affected = [] if affected_paragraph_refs is None else affected_paragraph_refs
    if not isinstance(affected, list) or len(affected) > LIMITS["paragraphs"]:
        _fail()
    affected_indices = [_paragraph_index(ref, refs) for ref in affected]
    if len(set(affected_indices)) != len(affected_indices):
        _fail()
    items, owners = [], {}
    for table in mapped["tables"]:
        items.append({"kind": "table", **{k: v for k, v in table.items() if k != "rows"}})
        for row in table["rows"]:
            items.append({"kind": "row", **{k: v for k, v in row.items() if k != "cells"}})
            for cell in row["cells"]:
                items.append({"kind": "cell", **cell})
                for ref in cell["paragraph_refs"]:
                    owners[ref["paragraph_index"]] = cell["cell_ref"]
    reference_count = field_count = 0
    for paragraph, ref in zip(snapshot.paragraphs, refs, strict=True):
        items.append({"kind": "paragraph", "paragraph_ref": ref,
                      "cell_ref": owners.get(paragraph.paragraph_index), "text": paragraph.text,
                      "numeric_scan_supported": not _unsafe_paragraph(paragraph)})
        for match in _REF.finditer(paragraph.text):
            reference_count += 1
            items.append({"kind": "text_reference", "paragraph_ref": ref,
                          "start": match.start(), "end": match.end(), "quote": match[0],
                          "within_affected_scope": paragraph.paragraph_index in affected_indices})
        for node in paragraph.element.iter():
            if node.tag not in {w("fldSimple"), w("fldChar"), w("instrText")}:
                continue
            field_count += 1
            items.append({"kind": "field", "paragraph_ref": ref,
                          "occurrence_index": field_count - 1,
                          "node_kind": node.tag.rsplit("}", 1)[-1],
                          "instruction": node.get(w("instr")) if node.tag == w("fldSimple") else node.text,
                          "field_char_type": node.get(w("fldCharType")),
                          "within_affected_scope": paragraph.paragraph_index in affected_indices})
    result = {
        "schema_version": "bilingual_table_inventory.v1", "policy": POLICY,
        "status": "ok", "file_sha256": snapshot.file_sha256,
        "items": items, "counts": {**mapped["counts"], "paragraphs": len(refs),
                                    "text_references": reference_count, "field_nodes": field_count},
        "coverage": mapped["coverage"],
        "reference_report": {"scope": "canonical_main_body_current_text_and_raw_field_nodes_v1",
                             "scanned_paragraphs": len(refs), "affected_paragraph_refs": affected,
                             "target_validity": "not_assessed", "semantic_reference_completeness": "not_claimed",
                             "fields_include_historical_markup": True},
        "limits": dict(LIMITS), "limitations": list(_LIMITATIONS),
    }
    return _json_bound(result)


def read_document_tables(path, file_sha256=None, *, affected_paragraph_refs=None):
    source = capture_document_snapshot(path, expected_sha256=file_sha256)
    return read_tables_payload(source.payload, affected_paragraph_refs=affected_paragraph_refs)


def _decimal(raw, fmt):
    if len(raw) > LIMITS["numeric_chars"]:
        return None, "numeric_literal_too_long"
    decimal, group = fmt["decimal_separator"], fmt["grouping_separator"]
    if group == decimal:
        _fail("invalid_column_format", "decimal and grouping separators must differ")
    sign = ""
    if raw[:1] in {"+", "-"}:
        sign, raw = raw[0], raw[1:]
    parts = raw.split(decimal)
    if len(parts) > 2 or (len(parts) == 2 and not parts[1].isdigit()):
        return None, "invalid_numeric_format"
    integer = parts[0]
    if integer == "" and len(parts) == 2:
        integer = "0"
    if group and group in integer:
        chunks = integer.split(group)
        if not (chunks[0].isdigit() and 1 <= len(chunks[0]) <= 3 and
                all(c.isdigit() and len(c) == 3 for c in chunks[1:])):
            return None, "invalid_grouping"
        integer = "".join(chunks)
    if not integer.isascii() or not integer.isdigit():
        return None, "invalid_numeric_format"
    try:
        return Decimal(sign + integer + ("." + parts[1] if len(parts) == 2 else "")), None
    except InvalidOperation:
        return None, "invalid_numeric_format"


def _date_value(raw, order):
    parts = re.split(r"[-/.]", raw)
    if len(parts[0]) == 4:
        year, month, day = map(int, parts)
    else:
        a, b, year = map(int, parts)
        if order == "reject_ambiguous":
            if a <= 12 and b <= 12:
                return None, "ambiguous_date"
            order = "DMY" if a > 12 else "MDY"
        if order not in {"DMY", "MDY"}:
            return None, "date_order_mismatch"
        day, month = (a, b) if order == "DMY" else (b, a)
    try:
        return date(year, month, day).isoformat(), None
    except ValueError:
        return None, "invalid_date"


def _unit(raw):
    value = (raw or "").lower().rstrip(".")
    currencies = {"$": "ambiguous_currency", "€": "EUR", "£": "GBP", "₽": "RUB", "¥": "ambiguous_currency"}
    if value.upper() in {"USD", "EUR", "GBP", "RUB", "CNY", "CHF", "JPY", "CAD", "AUD"}:
        return "amount", value.upper(), None
    if value in currencies:
        return "amount", currencies[value], None
    if value.startswith("доллар"):
        return "amount", "ambiguous_currency", None
    if value.startswith("руб"):
        return "amount", "RUB", None
    if value == "евро":
        return "amount", "EUR", None
    if value == "%" or value.startswith(("percent", "процент")):
        return "percentage", "percent", None
    if re.search(r"\b(?:days?|дней|дня|день)\b", value):
        basis = "business" if value.startswith(("business", "working", "рабоч")) else "calendar" if value.startswith(("calendar", "календарн")) else "unspecified"
        return "duration", "day", basis
    for starts, unit in [(("week", "недел"), "week"), (("month", "месяц"), "month"),
                         (("year", "лет", "год"), "year"), (("hour", "час"), "hour"),
                         (("kg", "кг"), "kg"), (("mg", "мг"), "mg"), (("ml", "мл"), "ml"),
                         (("gram", "грамм"), "g"), (("lit", "литр"), "l"),
                         (("pack", "упаков"), "pack"), (("piece", "штук", "шт"), "piece")]:
        if value.startswith(starts):
            return "duration" if unit in {"week", "month", "year", "hour"} else "quantity", unit, None
    return "number", "number", None


def _scale(raw):
    if raw is None:
        return 1
    value = raw.lower()
    if value.startswith(("thousand", "тыс")):
        return 1000
    if value.startswith(("million", "миллион", "млн")):
        return 1_000_000
    return 1_000_000_000


def extract_values(text, fmt, *, paragraph_ref, unsafe=False):
    """Exact Python-code-point spans; every lexical digit candidate is retained."""
    values, covered = [], bytearray(len(text))

    def add(start, end, kind, normalized=None, unit=None, basis=None, reason=None, scale=1):
        _bound("values", len(values) + 1)
        covered[start:end] = b"\1" * (end - start)
        values.append({"paragraph_ref": paragraph_ref, "start": start, "end": end,
                       "quote": text[start:end], "kind": kind, "normalized": normalized,
                       "unit": unit, "day_basis": basis, "scale": str(scale),
                       "status": "unsupported" if reason else "supported", "reason": reason})

    for match in _NAMED_DATE.finditer(text):
        add(match.start(), match.end(), "date", reason="named_date_format_not_supported")
    for match in _DATE.finditer(text):
        normalized, reason = _date_value(match[0], fmt["date_order"])
        add(match.start(), match.end(), "date", normalized, "date", reason=reason)
    for match in _VALUE.finditer(text):
        if any(covered[match.start():match.end()]):
            continue
        number, reason = _decimal(match["number"], fmt)
        kind, unit, basis = _unit(match["unit"] or match["prefix"])
        if match["prefix"] and match["unit"]:
            other = _unit(match["prefix"])
            if (kind, unit) != other[:2]:
                reason = "conflicting_units"
        if unit == "ambiguous_currency":
            reason = "ambiguous_currency"
        if basis == "unspecified":
            reason = "day_basis_unspecified"
        if kind == "number" and (re.match(r"\s*[A-Za-zА-Яа-яЁё]", text[match.end():]) or
                                 re.search(r"[A-Za-zА-Яа-яЁё]\s*$", text[:match.start()])):
            reason = "unrecognized_unit_or_numeric_context"
        if re.match(r"\s*(?:/\s*\w|(?:per|за|в)\s+\w|annually\b|daily\b|monthly\b|годов\w*|ежеднев\w*)", text[match.end():], re.I):
            reason = "compound_unit_or_rate_not_supported"
        scale = _scale(match["scale"])
        with localcontext() as context:
            context.prec = LIMITS["numeric_chars"] + 20
            normalized = format(number * scale, "f") if number is not None else None
        add(match.start(), match.end(), kind, normalized, unit, basis, reason, scale)
    for match in _WORDS.finditer(text):
        if not any(covered[match.start():match.end()]):
            add(match.start(), match.end(), "number_words", reason="number_words_not_supported")
    for match in re.finditer(rf"(?<!\w)(?:{_CURRENCY})(?!\w)", text, re.I):
        if not any(covered[match.start():match.end()]):
            add(match.start(), match.end(), "amount", reason="currency_without_supported_numeric_amount")
    # Digits attached to unknown units/identifiers or unsupported syntax must
    # not disappear merely because the supported token grammar skipped them.
    for match in re.finditer(r"\d+", text):
        if not any(covered[match.start():match.end()]):
            add(match.start(), match.end(), "number", reason="unrecognized_numeric_syntax")
    formula = _FORMULA.search(_DATE.sub(lambda m: " " * len(m[0]), _NAMED_DATE.sub(lambda m: " " * len(m[0]), text)))
    if formula:
        for value in values:
            value.update(status="unsupported", reason="formula_not_evaluated")
        add(formula.start(), formula.end(), "formula", reason="formula_not_evaluated")
    if unsafe:
        for value in values:
            value.update(status="unsupported", reason="unsupported_paragraph_content")
    values.sort(key=lambda value: (value["start"], value["end"]))
    return values


def _cell(mapped, ref, kind):
    try:
        item = mapped["tables"][ref["table_index"]]
        if kind in {"row", "cell"}:
            item = item["rows"][ref["row_index"]]
        if kind == "cell":
            item = item["cells"][ref["cell_index"]]
        if item[f"{kind}_ref"] != ref:
            _fail("bilingual_reference_mismatch")
        return item
    except (IndexError, KeyError, TypeError):
        _fail("bilingual_reference_mismatch")


def check_conditions_payload(payload: bytes, cell_pairs):
    try:
        jsonschema.Draft202012Validator(SELECTIONS).validate(cell_pairs)
    except jsonschema.ValidationError:
        _fail()
    snapshot, mapped, refs = _read(payload)
    pair_ids, selected_cells, reports = set(), set(), []
    total_values = 0
    for pair in cell_pairs:
        if pair["pair_id"] in pair_ids:
            _fail()
        pair_ids.add(pair["pair_id"])
        table, row = _cell(mapped, pair["table_ref"], "table"), _cell(mapped, pair["row_ref"], "row")
        if row["row_ref"]["table_index"] != table["table_index"]:
            _fail()
        if (table["issues"] or table["nesting_depth"] != 1 or row["issues"] or
            row["grid_before"] or row["grid_after"] or row["has_structural_revision_markers"]):
            _fail("bilingual_pairing_unsupported", "selected geometry is ambiguous or unsupported")
        side_values, side_indices = {}, {}
        for side in ("left", "right"):
            selection = pair[side]
            fmt = selection["format"]
            if fmt["decimal_separator"] == fmt["grouping_separator"]:
                _fail("invalid_column_format")
            cell = _cell(mapped, selection["cell_ref"], "cell")
            if (cell["cell_ref"]["table_index"] != table["table_index"] or
                cell["cell_ref"]["row_index"] != row["row_index"]):
                _fail()
            key = (table["table_index"], row["row_index"], cell["cell_index"])
            if key in selected_cells:
                _fail("duplicate_bilingual_cell")
            selected_cells.add(key)
            if (cell["issues"] or cell["grid_span"] != 1 or cell["h_merge"] or cell["v_merge"] or
                cell["nested_table_refs"] or cell["grid_start"] != selection["grid_column"]):
                _fail("bilingual_pairing_unsupported", "selected cells are merged, nested or ambiguous")
            indices = [_paragraph_index(ref, refs) for ref in cell["paragraph_refs"]]
            side_indices[side] = set(indices)
            values = []
            for index in indices:
                p = snapshot.paragraphs[index]
                values.extend(extract_values(p.text, fmt, paragraph_ref=refs[index], unsafe=_unsafe_paragraph(p)))
            for number, value in enumerate(values):
                value.update(value_id=f"{side}_{number}", paired_condition_id=None)
            total_values += len(values)
            _bound("values", total_values)
            side_values[side] = values
        if pair["left"]["grid_column"] == pair["right"]["grid_column"]:
            _fail()
        conditions, condition_ids, used_spans = [], set(), {"left": [], "right": []}
        for condition in pair["conditions"]:
            if condition["condition_id"] in condition_ids:
                _fail()
            condition_ids.add(condition["condition_id"])
            selected = {}
            unsafe = False
            for side in ("left", "right"):
                span = condition[side]
                index = _paragraph_index(span["paragraph_ref"], refs)
                start, end = span["start"], span["end"]
                if (index not in side_indices[side] or not 0 <= start < end <= len(snapshot.paragraphs[index].text)
                    or snapshot.paragraphs[index].text[start:end] != span["quote"]):
                    _fail("bilingual_quote_mismatch")
                if any(index == i and start < b and end > a for i, a, b in used_spans[side]):
                    _fail("overlapping_bilingual_conditions")
                used_spans[side].append((index, start, end))
                values = [v for v in side_values[side] if v["paragraph_ref"] == span["paragraph_ref"]
                          and v["start"] < end and v["end"] > start]
                if any(v["start"] < start or v["end"] > end for v in values):
                    _fail("bilingual_partial_value", "a selected quote cuts a numeric candidate")
                selected[side] = values
                unsafe |= _unsafe_paragraph(snapshot.paragraphs[index])
            a, b = selected["left"], selected["right"]
            status, reason = "not_established", "one_value_per_side_required"
            if unsafe or any(v["status"] == "unsupported" for v in a + b):
                status, reason = "unsupported_format", "unsupported_selected_content"
            elif len(a) == len(b) == 1:
                left, right = a[0], b[0]
                same_number = (left["normalized"] == right["normalized"] if left["kind"] == "date"
                               else Decimal(left["normalized"]) == Decimal(right["normalized"])
                               if right["kind"] != "date" else False)
                same = same_number and all(left[k] == right[k] for k in ("kind", "unit", "day_basis"))
                status, reason = ("numeric_values_and_units_match", "selected_single_values_equal") if same else ("mismatch", "selected_values_or_units_differ")
                for value in a + b:
                    value["paired_condition_id"] = condition["condition_id"]
            conditions.append({**condition, "status": status, "reason": reason,
                               "left_value_ids": [v["value_id"] for v in a],
                               "right_value_ids": [v["value_id"] for v in b]})
        values = [v for side in ("left", "right") for v in side_values[side]]
        reports.append({"pair_id": pair["pair_id"], "table_ref": pair["table_ref"], "row_ref": pair["row_ref"],
                        "left": pair["left"], "right": pair["right"], "conditions": conditions,
                        "values": side_values,
                        "coverage": {"values": len(values), "unsupported_values": sum(v["status"] == "unsupported" for v in values),
                                     "unpaired_values": sum(v["paired_condition_id"] is None for v in values),
                                     "paired_values": sum(v["paired_condition_id"] is not None for v in values),
                                     "selected_conditions": len(conditions),
                                     "conditions_with_match": sum(c["status"] == "numeric_values_and_units_match" for c in conditions)}})
    return _json_bound({"schema_version": "bilingual_condition_check.v1", "policy": POLICY, "status": "ok",
                        "file_sha256": snapshot.file_sha256, "pairs": reports,
                        "coverage": {"selected_cell_pairs": len(reports), "values": total_values,
                                     "document_wide_numeric_coverage": "not_claimed"},
                        "obligation_equivalence": "not_claimed", "limits": dict(LIMITS),
                        "limitations": list(_LIMITATIONS)})


def check_bilingual_conditions(path, file_sha256, cell_pairs):
    source = capture_document_snapshot(path, expected_sha256=file_sha256)
    return check_conditions_payload(source.payload, cell_pairs)


def validate_bilingual_result(result):
    """Critical invariants beyond the closed output shape, including MCP exit."""
    def require(condition):
        if not condition:
            _fail("bilingual_invariant_failed", "inconsistent bilingual evidence")

    digest = result["file_sha256"]
    if result["schema_version"] == "bilingual_table_inventory.v1":
        paragraphs = {i["paragraph_ref"]["paragraph_index"]: i for i in result["items"] if i["kind"] == "paragraph"}
        require(len(paragraphs) == result["counts"]["paragraphs"] == result["reference_report"]["scanned_paragraphs"])
        for item in result["items"]:
            kind = item["kind"]
            ref = item[f"{kind}_ref"] if kind in {"table", "row", "cell"} else item["paragraph_ref"]
            require(ref["file_sha256"] == digest)
            if kind == "paragraph":
                require(ref["paragraph_text_sha256"] == hashlib.sha256(item["text"].encode()).hexdigest())
            elif kind == "text_reference":
                p = paragraphs[ref["paragraph_index"]]
                require(p["paragraph_ref"] == ref and 0 <= item["start"] < item["end"] <= len(p["text"]))
                require(p["text"][item["start"]:item["end"]] == item["quote"])
        for kind, count_key in (("table", "tables"), ("row", "rows"), ("cell", "cells"),
                                ("text_reference", "text_references"), ("field", "field_nodes")):
            require(sum(i["kind"] == kind for i in result["items"]) == result["counts"][count_key])
        return
    require(len(result["pairs"]) == result["coverage"]["selected_cell_pairs"])
    total = 0
    for pair in result["pairs"]:
        require(all(pair[key]["file_sha256"] == digest for key in ("table_ref", "row_ref")))
        values = pair["values"]["left"] + pair["values"]["right"]
        total += len(values)
        coverage = pair["coverage"]
        require(coverage["values"] == len(values))
        require(coverage["unsupported_values"] == sum(v["status"] == "unsupported" for v in values))
        require(coverage["unpaired_values"] == sum(v["paired_condition_id"] is None for v in values))
        require(coverage["paired_values"] + coverage["unpaired_values"] == len(values))
        require(coverage["selected_conditions"] == len(pair["conditions"]))
        require(coverage["conditions_with_match"] == sum(c["status"] == "numeric_values_and_units_match" for c in pair["conditions"]))
        for value in values:
            require(value["paragraph_ref"]["file_sha256"] == digest)
            require((value["status"] == "unsupported") == (value["reason"] is not None))
        for side in ("left", "right"):
            require(pair[side]["cell_ref"]["file_sha256"] == digest)
        for condition in pair["conditions"]:
            chosen = []
            for side in ("left", "right"):
                span = condition[side]
                require(span["paragraph_ref"]["file_sha256"] == digest)
                require(len(span["quote"]) == span["end"] - span["start"])
                ids = condition[f"{side}_value_ids"]
                side_values = [v for v in pair["values"][side] if v["value_id"] in ids]
                require(len(side_values) == len(set(ids)) == len(ids))
                for value in side_values:
                    require(value["paragraph_ref"] == span["paragraph_ref"] and span["start"] <= value["start"] < value["end"] <= span["end"])
                    require(span["quote"][value["start"] - span["start"]:value["end"] - span["start"]] == value["quote"])
                chosen.append(side_values)
            if condition["status"] in {"numeric_values_and_units_match", "mismatch"}:
                require(all(len(v) == 1 and v[0]["status"] == "supported" for v in chosen))
                a, b = chosen[0][0], chosen[1][0]
                same = (all(a[k] == b[k] for k in ("kind", "unit", "day_basis")) and
                        (a["normalized"] == b["normalized"] if a["kind"] == "date" else Decimal(a["normalized"]) == Decimal(b["normalized"])))
                require(same == (condition["status"] == "numeric_values_and_units_match"))
                require(all(v[0]["paired_condition_id"] == condition["condition_id"] for v in chosen))
    require(total == result["coverage"]["values"])
