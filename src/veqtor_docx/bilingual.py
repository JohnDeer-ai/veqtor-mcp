# SPDX-License-Identifier: Apache-2.0
"""Bounded, read-only evidence for explicitly selected bilingual conditions.

The caller selects both cells and exact condition spans. No paragraph adjacency,
translation, obligation correspondence or whole-document equivalence is inferred.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
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
    "numeric_chars": 100, "operator_run": 100,
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
# Tokenize raw numeric-looking syntax before interpreting a declared format.
# Unsupported signs/grouping must remain in one candidate, never expose a
# supported unsigned/trailing substring. Punctuation followed by whitespace is
# a boundary (e.g. clause label "1. 100 USD"), not an invented decimal/group.
_SIGN = r"(?:\+\s*[/∕⁄／]\s*[-−]\s*|[+\-−±﹣－＋–—‒‐‑⁻₋➖]\s*)*"
_SIGN_RUN_CHARACTERS = frozenset("+-−±﹣－＋–—‒‐‑⁻₋➖/∕⁄／")
_NUMBER = rf"{_SIGN}(?:\d+(?:(?:[^\w\s]+|_)\d+|['’ʼ′`]\s*\d+|\s+\d+)*|[.,]\d+)(?:[eE][+\-−]?\d+)?"
_CURRENCY_INFO = {alias.lower(): ("amount", code, None) for code, aliases in [
    ("USD", ("USD",)), ("EUR", ("EUR", "евро", "€")), ("GBP", ("GBP", "£")),
    ("RUB", ("RUB", "руб", "руб.", "рубль", "рубля", "рублей", "рубли", "₽")),
    ("CNY", ("CNY",)), ("CHF", ("CHF",)), ("JPY", ("JPY",)),
    ("CAD", ("CAD",)), ("AUD", ("AUD",)),
    ("ambiguous_currency", ("$", "¥", "доллар", "доллара", "долларов")),
] for alias in aliases}
_UNIT_INFO = dict(_CURRENCY_INFO)
for _unit_name, _forms in {
    "percent": ("%", "percent", "процент", "процента", "процентов", "проценты"),
    "week": ("week", "weeks", "неделя", "недели", "недель", "неделю", "неделе", "неделями", "неделях"),
    "month": ("month", "months", "месяц", "месяца", "месяцев", "месяцы", "месяцу", "месяцем", "месяце", "месяцами", "месяцах"),
    "year": ("year", "years", "год", "года", "лет", "годы", "году", "годом", "годе", "годами", "годах"),
    "hour": ("hour", "hours", "час", "часа", "часов", "часы", "часу", "часом", "часе", "часами", "часах"),
    "kg": ("kg", "кг"), "mg": ("mg", "мг"), "ml": ("ml", "мл"),
    "g": ("g", "gram", "grams", "г", "грамм", "грамма", "граммов", "граммы"),
    "l": ("l", "litre", "litres", "liter", "liters", "л", "литр", "литра", "литров", "литры"),
    "pack": ("pack", "packs", "упаковка", "упаковки", "упаковок", "упаковку", "упаковке", "упаковками", "упаковках"),
    "piece": ("piece", "pieces", "штука", "штуки", "штук", "штуку", "штуке", "штуками", "штуках", "шт", "шт."),
}.items():
    _kind = "percentage" if _unit_name == "percent" else "duration" if _unit_name in {"week", "month", "year", "hour"} else "quantity"
    _UNIT_INFO.update({form: (_kind, _unit_name, None) for form in _forms})
_DAY_FORMS = ("day", "days", "день", "дня", "дней", "дни", "дню", "днем", "днём", "днями", "днях")
_UNIT_INFO.update({form: ("duration", "day", "unspecified") for form in _DAY_FORMS})
for _basis, _attributes in {
    "calendar": ("calendar", "календарный", "календарных", "календарные", "календарного", "календарным", "календарными", "календарном", "календарной", "календарную", "календарное", "календарному"),
    "business": ("business", "working", "рабочий", "рабочих", "рабочие", "рабочего", "рабочим", "рабочими", "рабочем", "рабочей", "рабочую", "рабочее", "рабочему"),
}.items():
    _UNIT_INFO.update({f"{attribute} {day}": ("duration", "day", _basis)
                       for attribute in _attributes for day in _DAY_FORMS})


def _alternatives(aliases):
    return "|".join(re.escape(alias).replace(r"\ ", r"\s+")
                    for alias in sorted(aliases, key=lambda s: (-len(s), s)))


_CURRENCY = _alternatives(_CURRENCY_INFO)
_UNIT = _alternatives(_UNIT_INFO)
_UNIT_REVERSE_TRIE = {}
for _alias in _UNIT_INFO:
    _branch = _UNIT_REVERSE_TRIE
    for _character in reversed(_alias):
        _branch = _branch.setdefault(_character, {})
    _branch[""] = True
_MAX_UNIT_CHARS = max(map(len, _UNIT_INFO))
_SCALE = r"thousand|million|billion|тыс\.?|тысяч(?:а|и)?|млн\.?|миллион(?:а|ов)?|млрд\.?|миллиард(?:а|ов)?"
_VALUE = re.compile(rf"(?<!\d)(?:(?P<prefix>{_CURRENCY})\s*)?(?P<number>{_NUMBER})(?:\s*(?P<scale>{_SCALE})(?!\w))?(?:\s*(?P<unit>{_UNIT})(?!\w))?", re.I)
_SUFFIX_UNIT = re.compile(rf"\s*(?:{_UNIT})(?!\w)", re.I)
_UNKNOWN_WORD = re.compile(r"\s*[^\W\d_][\w]*", re.UNICODE)
# Only ordinary prose punctuation may be ignored outside a parsed value.
# Unicode symbols and other punctuation (including per-mille and prime marks)
# are not a closed unit alias merely because a neighbouring number parses.
_PROSE_PUNCTUATION = frozenset(".,;:!?()[]{}\"'“”‘’«»…。，、；：！？")
_CONTEXT_WRAPPERS = frozenset("()[]{}\"'“”‘’«»")
_SYMBOL_CONTEXT_REASON = "unrecognized_symbolic_numeric_context"
_RATE = re.compile(r"\s*(?:[/∕⁄／÷]\s*|(?:per|за|в)\s+|(?:annually|daily|monthly|годовых|ежедневно)\b)", re.I)
_DATE = re.compile(r"(?<!\d)(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})(?!\d)")
_WORDS = re.compile(r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|ноль|один|одна|одно|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|двадцать|тридцать|сорок|пятьдесят|шестьдесят|семьдесят|восемьдесят|девяносто|сто|двести|триста|тысяч\w*|миллион\w*|миллиард\w*)\b", re.I)
_FORMULA = re.compile(r"[=×÷*^]|\d\s*[-−+±/∕⁄／]\s*\d")
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
    # Exact closed aliases, never a language stem or arbitrary word prefix.
    return _UNIT_INFO.get(" ".join((raw or "").lower().split()), ("number", "number", None))


def _numeric_operator(character):
    name = unicodedata.name(character, "")
    return (character in "+-/±" or unicodedata.category(character) == "Pd" or
            any(word in name for word in ("MINUS", "PLUS", "SOLIDUS", "SLASH", "DIVISION")))


class _LexicalText:
    """A detection view with a reversible raw-coordinate map, never clean text.

    Cf and combining marks are transparent ONLY for finding unsafe context.
    Every candidate touching them remains unsupported and retains its full raw
    quote. Neither Decimal parsing nor the public offsets use cleaned text.
    """

    def __init__(self, raw):
        self.raw = raw
        self.positions, self.controls = [], []
        characters = []
        self.control_counts = [0]
        for index, character in enumerate(raw):
            category = unicodedata.category(character)
            control = category == "Cf" or category.startswith("M")
            self.controls.append(control)
            self.control_counts.append(self.control_counts[-1] + control)
            if not control:
                characters.append(character)
                self.positions.append(index)
        self.text = "".join(characters)
        self.previous_nonspace = [-1]
        previous = -1
        for index, character in enumerate(self.text):
            if not character.isspace():
                previous = index
            self.previous_nonspace.append(previous)

    def raw_span(self, start, end):
        first, last = self.positions[start], self.positions[end - 1] + 1
        while first and self.controls[first - 1]:
            first -= 1
        while last < len(self.raw) and self.controls[last]:
            last += 1
        return first, last

    def raw_slice(self, start, end):
        first, last = self.raw_span(start, end)
        return self.raw[first:last]

    def has_controls(self, first, last):
        return self.control_counts[last] != self.control_counts[first]

    def preceding_unit(self, start):
        # Walk at most the longest closed alias. Whitespace runs are skipped
        # through a single precomputed link, even across thousands of spaces.
        end = self.previous_nonspace[start] + 1
        index, branch, found = end - 1, _UNIT_REVERSE_TRIE, None
        for _ in range(_MAX_UNIT_CHARS):
            if index < 0:
                break
            character = self.text[index]
            first = index
            if character.isspace():
                character = " "
                index = self.previous_nonspace[index + 1]
            else:
                index -= 1
            branch = branch.get(character.lower())
            if branch is None:
                break
            if "" in branch and (first == 0 or not (self.text[first - 1].isalnum() or self.text[first - 1] == "_")):
                found = first, end
        return found


def _skip_spacing(text, position):
    while position < len(text) and (text[position].isspace() or unicodedata.category(text[position]) == "Cf"):
        position += 1
    return position


def _unknown_numeric_symbol(character):
    return (unicodedata.category(character).startswith("N") and not character.isdecimal() or
            not (character.isalnum() or character.isspace() or character in _PROSE_PUNCTUATION))


def _symbol_context(text, start, end, covered):
    """Keep unsupported notation with its value, even across unit wrappers.

    Wrappers are traversed only to discover unsafe notation; ordinary quoted
    amounts stay supported. Covered positions and prose punctuation delimit
    neighbouring values. Each successful extension consumes new characters.
    """
    def bridge(position, direction):
        while (0 <= position < len(text) and not covered[position] and
               (text[position].isspace() or text[position] in _CONTEXT_WRAPPERS)):
            position += direction
        return position

    changed = False
    while start:
        marker = bridge(start - 1, -1)
        # A symbol can precede a separated unit label: "° C 100".
        if marker >= 0 and text[marker].isalpha():
            while marker >= 0 and not covered[marker] and (text[marker].isalnum() or text[marker] == "_"):
                marker -= 1
            marker = bridge(marker, -1)
        if marker < 0 or covered[marker] or not _unknown_numeric_symbol(text[marker]):
            break
        start, changed = marker, True
    while end < len(text):
        marker = bridge(end, 1)
        if marker >= len(text) or covered[marker] or not _unknown_numeric_symbol(text[marker]):
            break
        end, changed = marker + 1, True
        if following := _UNKNOWN_WORD.match(text, end):
            if not any(covered[end:following.end()]):
                end = following.end()
    if changed:
        # Retain the surrounding wrappers, but not unrelated outer whitespace.
        marker = start - 1
        while marker >= 0 and not covered[marker] and (text[marker].isspace() or text[marker] in _CONTEXT_WRAPPERS):
            if text[marker] in _CONTEXT_WRAPPERS:
                start = marker
            marker -= 1
        marker = end
        while marker < len(text) and not covered[marker] and (text[marker].isspace() or text[marker] in _CONTEXT_WRAPPERS):
            if text[marker] in _CONTEXT_WRAPPERS:
                end = marker + 1
            marker += 1
    return start, end, _SYMBOL_CONTEXT_REASON if changed else None


def _lexical_context(lexical, start, end, covered):
    """Retain known dangling units and rate notation in the same raw span."""
    text, reason = lexical.text, None
    marker = start
    while marker and (text[marker - 1].isspace() or unicodedata.category(text[marker - 1]) == "Cf"):
        marker -= 1
    if marker and _numeric_operator(text[marker - 1]) and not covered[marker - 1]:
        start = marker - 1
        reason = "unsupported_numeric_operator_context"
    while prefix := lexical.preceding_unit(start):
        if any(covered[prefix[0]:prefix[1]]):
            break  # That unit belongs to a preceding distinct value.
        start = prefix[0]
        reason = "dangling_or_conflicting_units"
    while suffix := _SUFFIX_UNIT.match(text, end):
        if any(covered[end:suffix.end()]):
            break
        end = suffix.end()
        reason = "dangling_or_conflicting_units"
    rate = _RATE.match(text, end)
    marker = _skip_spacing(text, end)
    if rate or (marker < len(text) and _numeric_operator(text[marker])):
        end = _skip_spacing(text, rate.end() if rate else marker + 1)
        # A denominator can itself contain a digit-written amount or a unit.
        # Keep it inside this unsupported candidate so its suffix cannot be
        # selected as a fresh supported scalar.
        denominator = _VALUE.match(text, end) or _SUFFIX_UNIT.match(text, end) or _UNKNOWN_WORD.match(text, end)
        if denominator:
            end = denominator.end()
        reason = "compound_unit_or_rate_not_supported"
    start, end, symbol_reason = _symbol_context(text, start, end, covered)
    return start, end, symbol_reason or reason


def _identifier_span(text, start, end):
    original = start, end
    while start and (text[start - 1].isalnum() or text[start - 1] == "_"):
        start -= 1
    while end < len(text) and (text[end].isalnum() or text[end] == "_"):
        end += 1
    return start, end, (start, end) != original


def _scale(raw):
    if raw is None:
        return 1
    value = raw.lower()
    if value.startswith(("thousand", "тыс")):
        return 1000
    if value.startswith(("million", "миллион", "млн")):
        return 1_000_000
    return 1_000_000_000


class _ScalarScanner:
    def __init__(self, text, segments):
        self.text, self.segments, self.position = text, segments, 0

    def __iter__(self):
        for start, end in self.segments:
            self.position = start
            while match := _VALUE.search(self.text, self.position, end):
                self.position = match.end()
                yield match

    def advance(self, end):
        self.position = max(self.position, end)


def _bound_scalar_work(text):
    # An unanchored failed search retries the repeated sign prefix at every
    # sign. Bound that work before any scalar regex call, including when a
    # later digit cannot complete the prefix (e.g. a sign run followed by x1).
    # Count operators, not whitespace: normal unit/currency spacing is free.
    operators = 0
    for character in text:
        if character in _SIGN_RUN_CHARACTERS:
            operators += 1
            _bound("operator_run", operators)
        elif not character.isspace():
            operators = 0


def extract_values(text, fmt, *, paragraph_ref, unsafe=False):
    """Exact Python-code-point spans; every lexical digit candidate is retained."""
    lexical = _LexicalText(text)
    text = lexical.text
    _bound_scalar_work(text)
    values, covered = [], bytearray(len(text))

    def add(start, end, kind, normalized=None, unit=None, basis=None, reason=None, scale=1):
        _bound("values", len(values) + 1)
        covered[start:end] = b"\1" * (end - start)
        first, last = lexical.raw_span(start, end)
        if lexical.has_controls(first, last):
            normalized, reason = None, "unsupported_format_or_combining_context"
        if reason == _SYMBOL_CONTEXT_REASON:
            normalized = None
        values.append({"paragraph_ref": paragraph_ref, "start": first, "end": last,
                       "quote": lexical.raw[first:last], "kind": kind, "normalized": normalized,
                       "unit": unit, "day_basis": basis, "scale": str(scale),
                       "status": "unsupported" if reason else "supported", "reason": reason})

    date_candidates = [(match.start(), match.end(), None, "named_date_format_not_supported")
                       for match in _NAMED_DATE.finditer(text)]
    for match in _DATE.finditer(text):
        normalized, reason = _date_value(match[0], fmt["date_order"])
        start, end, embedded = _identifier_span(text, match.start(), match.end())
        if embedded:
            normalized, reason = None, "date_embedded_in_identifier"
        date_candidates.append((start, end, normalized, reason))
    dates = []
    for start, end, normalized, reason in sorted(date_candidates):
        if dates and start < dates[-1][1]:
            old = dates.pop()
            dates.append((old[0], max(old[1], end), None, "overlapping_date_identifiers"))
        else:
            dates.append((start, end, normalized, reason))
    # Reserved date spans are hard lexical boundaries for scalar discovery.
    # Do not skip an entire scalar match merely because its beginning was
    # already a date, and assign amount currencies before date context lookup.
    segments, position = [], 0
    for start, end, _, _ in dates:
        segments.append((position, start))
        position = end
    segments.append((position, len(text)))
    scanner = _ScalarScanner(text, segments)
    for match in scanner:
        if any(covered[match.start():match.end()]):
            continue
        number, reason = _decimal(lexical.raw_slice(match.start("number"), match.end("number")), fmt)
        kind, unit, basis = _unit(match["unit"] or match["prefix"])
        if match["prefix"] and match["unit"]:
            other = _unit(match["prefix"])
            if (kind, unit) != other[:2]:
                reason = "conflicting_units"
        if unit == "ambiguous_currency":
            reason = "ambiguous_currency"
        if basis == "unspecified":
            reason = "day_basis_unspecified"
        start, end, embedded = _identifier_span(text, match.start(), match.end())
        if embedded:
            reason = "number_embedded_in_identifier"
        start, end, context_reason = _lexical_context(lexical, start, end, covered)
        if context_reason:
            reason = context_reason
        if kind == "number" and context_reason != _SYMBOL_CONTEXT_REASON:
            if following := _UNKNOWN_WORD.match(text, end):
                end = following.end()
                reason = "unrecognized_unit_or_numeric_context"
            preceding = lexical.previous_nonspace[start]
            if preceding >= 0 and text[preceding].isalpha():
                reason = "unrecognized_unit_or_numeric_context"
        scale = _scale(match["scale"])
        with localcontext() as context:
            context.prec = LIMITS["numeric_chars"] + 20
            normalized = format(number * scale, "f") if number is not None else None
        add(start, end, kind, normalized, unit, basis, reason, scale)
        scanner.advance(end)
    for start, end, normalized, reason in dates:
        start, end, context_reason = _lexical_context(lexical, start, end, covered)
        add(start, end, "date", normalized, "date", reason=context_reason or reason)
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
    # A selected span must not lose unsupported notation separated from a
    # value by prose punctuation. Unselected notation stays visible without
    # contaminating an independently selected neighbouring condition.
    if values:
        for index, character in enumerate(text):
            if not covered[index] and _unknown_numeric_symbol(character):
                add(index, index + 1, "number", reason=_SYMBOL_CONTEXT_REASON)
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
