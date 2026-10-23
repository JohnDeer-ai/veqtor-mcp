# SPDX-License-Identifier: Apache-2.0
"""Closed numbering proofs for clean whole-paragraph structural projections.

This intentionally does not reuse the citation reader's best-effort counters.
Only explicit, single-level decimal lists are proved. Unsupported formats,
style links, ambiguous definitions and restarts produce a controlled refusal.
Numbering instances are independent; a start override applies at the first
use of its instance, not every time that instance resumes after ordinary text.

Normative references: ISO/IEC 29500-1 17.9.7, 17.9.9, 17.9.11, 17.9.12,
17.9.24, 17.9.26, 17.9.27, as reproduced in Microsoft Learn's LevelOverride,
LevelRestart, LevelText, NumberingId and StartOverrideNumberingValue remarks.
The single-level boundary avoids Word's documented multilevel restart and
paragraph-style ilvl differences (MS-OI29500 2.1.282; MS-OE376 2.1.54).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re

from lxml import etree

from ._ooxml import (
    _CANONICAL_BLOCK_PASS_THROUGH,
    _TEXT_BOX_TAGS,
    DocxError,
    ResourceLimitError,
    parse_xml,
    w,
)
from .apply import ApplyError

MAX_NUMBERING_PARAGRAPHS = 20_000
MAX_NUMBERING_DEFINITIONS = 4_096
MAX_NUMBERING_NODES = 100_000
MAX_NUMBERING_PART_BYTES = 4 * 1024 * 1024
MAX_NUMBERING_STYLE_DEPTH = 64
MAX_NUMBERING_CONTAINER_DEPTH = 128
MAX_NUMBERING_FORMATTING_WORK = 100_000
MAX_NUMBERING_EFFECTS = 1_000
MAX_NUMBERING_VALUE = 2_147_483_647
MAX_NUMBERING_LABEL_CHARS = 256

_DECIMAL = re.compile(r"[0-9]{1,10}\Z", re.ASCII)
_XML_WHITESPACE = " \t\r\n"
_REVISION_NAMES = frozenset({
    "ins", "del", "moveFrom", "moveTo", "numberingChange", "pPrChange",
})


def _refuse(detail: str, *, reason: str = "unsupported_scheme") -> None:
    raise ApplyError("paragraph_numbering_unsupported", detail, reason=reason)


def _bounded(condition: bool, detail: str) -> None:
    if not condition:
        raise ApplyError("resource_limit_exceeded", detail)


def _attributes(node: etree._Element, names: set[str]) -> None:
    if set(node.attrib) - {w(name) for name in names}:
        _refuse("numbering dependency has unsupported attributes", reason="unknown_attributes")


def _one(parent: etree._Element | None, name: str) -> etree._Element | None:
    if parent is None:
        return None
    found = parent.findall(w(name))
    if len(found) > 1:
        _refuse(f"duplicate {name} in numbering dependency", reason="duplicate_definition")
    return found[0] if found else None


def _value(node: etree._Element, *, names: set[str] | None = None) -> str:
    _attributes(node, names or {"val"})
    if len(node) or (node.text and node.text.strip()):
        _refuse("numbering value must be a leaf", reason="invalid_definition")
    value = node.get(w("val"))
    if value is None:
        _refuse("numbering value is missing", reason="missing_value")
    return value


def _integer(value: str | None, *, name: str, maximum: int = MAX_NUMBERING_VALUE) -> int:
    if value is None or _DECIMAL.fullmatch(value) is None or int(value) > maximum:
        _refuse(f"invalid or out-of-range {name}", reason="invalid_value")
    return int(value)


def _id(value: str | None, *, name: str) -> str:
    return str(_integer(value, name=name))


def _definition_identity(value: str | None) -> str | None:
    """Index Int32 lexical aliases without relaxing supported reference syntax.

    Open XML SDK parses definition IDs as Int32Value/XmlConvert.ToInt32.
    Consequently ``+1``, XML-whitespace-wrapped ``1`` and arbitrarily padded
    ``0001`` must participate in the same uniqueness proof. The part-size
    envelope bounds this scan; no unbounded string is passed to ``int()``.
    XML whitespace is explicit so Unicode spaces cannot become aliases.
    """
    if value is None:
        return None
    digits = value.strip(_XML_WHITESPACE)
    negative = digits.startswith("-")
    if digits.startswith(("+", "-")):
        digits = digits[1:]
    if not digits or not digits.isascii() or not digits.isdecimal():
        return None
    significant = digits.lstrip("0")
    if not significant:
        return "0"  # Both lexical -0 and +0 denote the same numeric zero.
    bound = "2147483648" if negative else "2147483647"
    if len(significant) > len(bound) or (
        len(significant) == len(bound) and significant > bound
    ):
        return None
    return ("-" if negative else "") + significant


def _part(parts: Mapping[str, bytes], name: str, expected: str) -> etree._Element | None:
    payload = parts.get(name)
    if payload is None:
        return None
    if not isinstance(payload, bytes):
        _refuse(f"{name} is not an immutable XML payload", reason="invalid_definition")
    _bounded(len(payload) <= MAX_NUMBERING_PART_BYTES, "numbering dependency part is too large")
    try:
        root = parse_xml(payload)
    except ResourceLimitError as exc:
        raise ApplyError("resource_limit_exceeded", "numbering dependency XML exceeds its resource envelope") from exc
    except DocxError as exc:
        raise ApplyError("paragraph_numbering_unsupported", "invalid numbering dependency XML") from exc
    if root.tag != w(expected):
        _refuse(f"{name} has an unexpected root", reason="invalid_definition")
    _bounded(sum(1 for _ in root.iter()) <= MAX_NUMBERING_NODES, "numbering dependency has too many nodes")
    return root


def _paragraphs(root: etree._Element) -> list[etree._Element]:
    if root.tag == w("body"):
        body = root
    elif root.tag == w("document"):
        body = _one(root, "body")
        if body is None:
            _refuse("document body is missing", reason="invalid_projection")
    else:
        _refuse("a clean document or body projection is required", reason="invalid_projection")
    _bounded(sum(1 for _ in body.iter()) <= MAX_NUMBERING_NODES, "numbering projection has too many nodes")
    paragraphs = list(body.iter(w("p")))
    _bounded(len(paragraphs) <= MAX_NUMBERING_PARAGRAPHS, "too many paragraphs in numbering projection")
    return paragraphs


@dataclass(frozen=True)
class _Effective:
    style: str | None
    chain: tuple[str, ...]
    num_id: str | None
    level: int | None


@dataclass(frozen=True)
class _Level:
    abstract_id: str
    start: int
    start_source: str
    template: str
    style: str | None


class _Resolver:
    def __init__(self, parts: Mapping[str, bytes], root: etree._Element):
        self.paragraphs = _paragraphs(root)
        self.styles = _part(parts, "word/styles.xml", "styles")
        self.numbering = _part(parts, "word/numbering.xml", "numbering")
        self._styles: dict[str, list[etree._Element]] = {}
        self._default_styles: list[str] = []
        self._effective: dict[int, _Effective] = {}
        self._levels: dict[str, _Level] = {}
        self._formatting_work = 0
        self._invalid_default_style = False
        if self.styles is not None:
            styles = self.styles.findall(w("style"))
            _bounded(len(styles) <= MAX_NUMBERING_DEFINITIONS, "too many style definitions")
            for style in styles:
                style_id = style.get(w("styleId"))
                if style_id is not None:
                    self._styles.setdefault(style_id, []).append(style)
                    if style.get(w("type")) == "paragraph":
                        flag = style.get(w("default"))
                        if flag in {"1", "true", "on"}:
                            self._default_styles.append(style_id)
                        elif flag is not None and flag not in {"0", "false", "off"}:
                            self._invalid_default_style = True
        self._nums: dict[str, list[etree._Element]] = {}
        self._abstracts: dict[str, list[etree._Element]] = {}
        if self.numbering is not None:
            _bounded(len(self.numbering) <= MAX_NUMBERING_DEFINITIONS, "too many numbering definitions")
            for child in self.numbering:
                if child.tag in {w("num"), w("abstractNum")}:
                    name = "numId" if child.tag == w("num") else "abstractNumId"
                    identifier = _definition_identity(child.get(w(name)))
                    # Index semantic aliases before checking uniqueness. An
                    # unrelated malformed or out-of-range id remains outside
                    # the selected sequence's dependency proof.
                    if identifier is not None:
                        index = self._nums if name == "numId" else self._abstracts
                        index.setdefault(identifier, []).append(child)

    @staticmethod
    def _num_properties(props: etree._Element | None) -> dict[str, int | str]:
        if props is None:
            return {}
        _attributes(props, set())
        if _one(props, "pPrChange") is not None:
            _refuse("numbering requires clean paragraph properties", reason="pending_properties")
        run_props = _one(props, "rPr")
        if run_props is not None and any(node.tag in {w(name) for name in _REVISION_NAMES} for node in run_props.iter()):
            _refuse("numbering requires clean paragraph marks", reason="pending_paragraph_mark")
        num = _one(props, "numPr")
        if num is None:
            return {}
        _attributes(num, set())
        if any(child.tag not in {w("numId"), w("ilvl")} for child in num):
            _refuse("unsupported numbering property or pending numbering revision", reason="unknown_num_properties")
        result: dict[str, int | str] = {}
        identifier = _one(num, "numId")
        level = _one(num, "ilvl")
        if identifier is not None:
            result["num_id"] = _id(_value(identifier), name="numId")
        if level is not None:
            result["level"] = _integer(_value(level), name="ilvl", maximum=8)
        return result

    def effective(self, index: int) -> _Effective:
        if index in self._effective:
            return self._effective[index]
        paragraph = self.paragraphs[index]
        props = _one(paragraph, "pPr")
        style_ref = _one(props, "pStyle")
        style_id = _value(style_ref) if style_ref is not None else None
        if style_id is None:
            if self._invalid_default_style:
                _refuse("invalid default paragraph style flag", reason="invalid_default_style")
            if len(self._default_styles) > 1:
                _refuse("multiple default paragraph styles", reason="duplicate_definition")
            style_id = self._default_styles[0] if self._default_styles else None
        values: dict[str, int | str] = {}
        if self.styles is not None:
            defaults = _one(self.styles, "docDefaults")
            default_props = _one(_one(defaults, "pPrDefault"), "pPr")
            values.update(self._num_properties(default_props))
        chain: list[tuple[str, etree._Element]] = []
        seen: set[str] = set()
        current = style_id
        while current is not None:
            if not current or len(current) > 255:
                _refuse("invalid paragraph style reference", reason="invalid_style")
            if current in seen:
                _refuse("paragraph style inheritance cycle", reason="style_cycle")
            _bounded(len(chain) < MAX_NUMBERING_STYLE_DEPTH, "paragraph style inheritance is too deep")
            seen.add(current)
            matches = self._styles.get(current, [])
            if len(matches) != 1:
                _refuse("paragraph style is missing or duplicated", reason="missing_or_duplicate_style")
            style = matches[0]
            _attributes(style, {"type", "styleId", "default", "customStyle"})
            if any(not isinstance(child.tag, str) or not child.tag.startswith(w("")) for child in style):
                _refuse("external paragraph style properties are unsupported", reason="unknown_style_properties")
            if style.get(w("type")) != "paragraph":
                _refuse("paragraph references a non-paragraph style", reason="invalid_style")
            chain.append((current, style))
            base = _one(style, "basedOn")
            current = _value(base) if base is not None else None
        for _, style in reversed(chain):
            values.update(self._num_properties(_one(style, "pPr")))
        values.update(self._num_properties(props))
        num_id = values.get("num_id")
        level = values.get("level")
        if num_id in {None, "0"}:
            if num_id is None and level is not None:
                _refuse("numbering level has no numbering instance", reason="missing_num_id")
            result = _Effective(style_id, tuple(name for name, _ in reversed(chain)), None, None)
        else:
            # There is only one proved level in the closed scheme. Omitted
            # ilvl therefore has the same unique meaning as explicit zero.
            result = _Effective(style_id, tuple(name for name, _ in reversed(chain)), str(num_id), int(level or 0))
        self._effective[index] = result
        return result

    def _definition(self, index: dict[str, list[etree._Element]], identifier: str, name: str) -> etree._Element:
        matches = index.get(identifier, [])
        if len(matches) != 1:
            _refuse(f"{name} is missing or duplicated", reason="missing_or_duplicate_definition")
        return matches[0]

    def _formatting(self, node: etree._Element) -> None:
        for child in node.iter():
            self._formatting_work += 1
            _bounded(self._formatting_work <= MAX_NUMBERING_FORMATTING_WORK, "numbering formatting proof exceeds its work budget")
            if not isinstance(child.tag, str) or not child.tag.startswith(w("")):
                _refuse("external numbering formatting is unsupported", reason="unknown_formatting")
            if etree.QName(child).localname.endswith("Change") or child.tag == w("numPr"):
                _refuse("numbering formatting contains a dependency or revision", reason="unknown_formatting")
            if any(not attr.startswith(w("")) for attr in child.attrib):
                _refuse("external numbering formatting attributes are unsupported", reason="unknown_attributes")

    def _level(self, node: etree._Element, start_override: int | None = None) -> tuple[int, str, str | None]:
        _attributes(node, {"ilvl", "tplc", "tentative"})
        if _integer(node.get(w("ilvl")), name="level ilvl", maximum=8) != 0:
            _refuse("only level zero is supported", reason="multilevel_numbering")
        allowed = {w(name) for name in (
            "start", "numFmt", "lvlRestart", "pStyle", "isLgl", "suff",
            "lvlText", "lvlJc", "pPr", "rPr",
        )}
        if any(child.tag not in allowed for child in node):
            _refuse("unsupported numbering level definition", reason="unknown_level_properties")
        for name in ("start", "numFmt", "lvlRestart", "pStyle", "isLgl", "suff", "lvlText", "lvlJc", "pPr", "rPr"):
            _one(node, name)
        fmt = _one(node, "numFmt")
        start = _one(node, "start")
        text = _one(node, "lvlText")
        if fmt is None or _value(fmt) != "decimal":
            _refuse("only explicit decimal numbering is supported", reason="unknown_number_format")
        if (start is None and start_override is None) or text is None:
            _refuse("explicit numbering start and text are required", reason="incomplete_level")
        start_value = _integer(_value(start), name="start") if start is not None else start_override
        if start_override is not None:
            start_value = start_override
        template = _value(text)
        if re.findall(r"%[0-9]+", template) != ["%1"] or "%" in template.replace("%1", ""):
            _refuse("numbering text must contain exactly one level-zero token", reason="unknown_number_format")
        _bounded(len(template) <= MAX_NUMBERING_LABEL_CHARS - 10, "numbering label is too long")
        if any(ord(char) < 32 for char in template):
            _refuse("numbering text has control characters", reason="unknown_number_format")
        restart = _one(node, "lvlRestart")
        if restart is not None and _integer(_value(restart), name="lvlRestart") != 0:
            _refuse("nonzero restart references are outside the single-level scheme", reason="ambiguous_restart")
        for name in ("pPr", "rPr"):
            formatting = _one(node, name)
            if formatting is not None:
                self._formatting(formatting)
        suffix = _one(node, "suff")
        if suffix is not None and _value(suffix) not in {"tab", "space", "nothing"}:
            _refuse("unknown numbering suffix", reason="unknown_number_format")
        align = _one(node, "lvlJc")
        if align is not None and _value(align) not in {"left", "center", "right", "start", "end"}:
            _refuse("unknown numbering alignment", reason="unknown_number_format")
        legal = _one(node, "isLgl")
        if legal is not None:
            _attributes(legal, {"val"})
            if len(legal) or legal.get(w("val"), "1") not in {"1", "0", "true", "false", "on", "off"}:
                _refuse("invalid legal numbering flag", reason="invalid_definition")
        style = _one(node, "pStyle")
        return start_value, template, _value(style) if style is not None else None

    def level(self, num_id: str) -> _Level:
        if num_id in self._levels:
            return self._levels[num_id]
        instance = self._definition(self._nums, num_id, "numbering instance")
        _attributes(instance, {"numId"})
        if any(child.tag not in {w("abstractNumId"), w("lvlOverride")} for child in instance):
            _refuse("numbering instance has unsupported properties", reason="unknown_instance_properties")
        abstract_ref = _one(instance, "abstractNumId")
        if abstract_ref is None:
            _refuse("abstract numbering reference is missing", reason="missing_definition")
        abstract_id = _id(_value(abstract_ref), name="abstractNumId")
        abstract = self._definition(self._abstracts, abstract_id, "abstract numbering definition")
        _attributes(abstract, {"abstractNumId"})
        if any(child.tag not in {w(name) for name in ("nsid", "multiLevelType", "tmpl", "name", "lvl")} for child in abstract):
            _refuse("style-linked, picture or unknown numbering is unsupported", reason="linked_or_unknown_numbering")
        for name in ("nsid", "multiLevelType", "tmpl", "name"):
            value = _one(abstract, name)
            if value is not None:
                _value(value)
        multi = _one(abstract, "multiLevelType")
        if multi is not None and _value(multi) != "singleLevel":
            _refuse("multilevel numbering is unsupported", reason="multilevel_numbering")
        levels = abstract.findall(w("lvl"))
        if len(levels) != 1:
            _refuse("exactly one numbering level is required", reason="multilevel_or_duplicate_levels")
        overrides = instance.findall(w("lvlOverride"))
        if len(overrides) > 1:
            _refuse("multiple level overrides are unsupported", reason="duplicate_or_multilevel_override")
        replacement = None
        override_start = None
        if overrides:
            override = overrides[0]
            _attributes(override, {"ilvl"})
            if _integer(override.get(w("ilvl")), name="override ilvl", maximum=8) != 0:
                _refuse("override does not target level zero", reason="level_mismatch")
            if any(child.tag not in {w("startOverride"), w("lvl")} for child in override):
                _refuse("unknown level override property", reason="unknown_override_properties")
            replacement = _one(override, "lvl")
            override_start = _one(override, "startOverride")
            if replacement is None and override_start is None:
                _refuse("empty numbering override", reason="invalid_definition")
        # A complete replacement is the effective level, rather than an
        # additive merge with the abstract level. A startOverride supersedes
        # either level's start (ISO/IEC 29500-1 17.9.27).
        _attributes(levels[0], {"ilvl", "tplc", "tentative"})
        if _integer(levels[0].get(w("ilvl")), name="level ilvl", maximum=8) != 0:
            _refuse("abstract level and override disagree", reason="level_mismatch")
        start_value = _integer(_value(override_start), name="startOverride") if override_start is not None else None
        start, template, style = self._level(replacement if replacement is not None else levels[0], start_value)
        start_source = "start_override" if override_start is not None else "level_override" if replacement is not None else "abstract_start"
        result = _Level(abstract_id, start, start_source, template, style)
        self._levels[num_id] = result
        return result

    def resolve(self, indices: Sequence[int]) -> list[dict]:
        requested = list(indices)
        if any(type(index) is not int or not 0 <= index < len(self.paragraphs) for index in requested):
            raise ApplyError("invalid_edit", "numbering paragraph index is out of range")
        if len(requested) != len(set(requested)):
            raise ApplyError("invalid_edit", "numbering paragraph indices must be unique")
        _bounded(len(requested) <= MAX_NUMBERING_EFFECTS, "too many selected numbering effects")
        for index in requested:
            if self._story_context(index) != "main":
                _refuse("selected paragraph is outside the supported main story", reason="unknown_numbering_container")
        selected = {index: self.effective(index) for index in requested}
        needed = {fact.num_id for fact in selected.values()} - {None}
        definitions = {num_id: self.level(num_id) for num_id in sorted(needed)}
        counters: dict[str, int] = {}
        values: dict[int, int] = {}
        # A foreign unsupported *format* is never parsed. But an earlier
        # unresolved style/numPr could reference a needed instance, so cannot
        # safely be skipped when any selected label depends on its counter.
        if needed:
            last_needed = max(index for index, fact in selected.items() if fact.num_id in needed)
            for index in range(last_needed + 1):
                context = self._story_context(index)
                if context == "separate":
                    # Text boxes are distinct stories, not predecessors in the
                    # main-story numbering sequence even if they reuse numId.
                    continue
                effective = self.effective(index)
                if effective.num_id not in needed:
                    continue
                if context != "main":
                    _refuse("an affected predecessor has an unsupported container", reason="unknown_numbering_container")
                if effective.level != 0:
                    _refuse("affected sequence uses another numbering level", reason="level_mismatch")
                level = definitions[effective.num_id]
                if level.style is not None and effective.style is not None and level.style not in effective.chain:
                    # Direct numPr is allowed on an unrelated paragraph style;
                    # the association only chooses a level for inherited lists.
                    props = _one(self.paragraphs[index], "pPr")
                    if not self._num_properties(props).get("num_id"):
                        _refuse("inherited list has an unmatched level style", reason="style_level_mismatch")
                value = counters.get(effective.num_id, level.start - 1) + 1
                _bounded(value <= MAX_NUMBERING_VALUE, "numbering counter exceeds the supported range")
                counters[effective.num_id] = value
                values[index] = value
        output = []
        for index in requested:
            effective = selected[index]
            level = definitions.get(effective.num_id)
            value = values.get(index)
            output.append({
                "paragraph_index": index,
                "p_style": effective.style,
                "style_chain": list(effective.chain),
                "num_id": effective.num_id,
                "abstract_num_id": level.abstract_id if level else None,
                "ilvl": effective.level,
                "label": level.template.replace("%1", str(value)) if level else None,
                "value": value,
                "start": level.start if level else None,
                "start_source": level.start_source if level else None,
                "restart": "continuous_level_zero" if level else None,
            })
        return output

    def _story_context(self, index: int) -> str:
        context = "main"
        for depth, parent in enumerate(self.paragraphs[index].iterancestors()):
            _bounded(depth < MAX_NUMBERING_CONTAINER_DEPTH, "numbering paragraph container ancestry is too deep")
            if parent.tag == w("body"):
                return context
            if parent.tag in _TEXT_BOX_TAGS:
                return "separate"
            if parent.tag not in _CANONICAL_BLOCK_PASS_THROUGH:
                context = "unknown"
        return context


def resolve_body_numbering(
    parts: Mapping[str, bytes], root: etree._Element, paragraph_indices: Sequence[int],
) -> list[dict]:
    """Prove selected labels in a clean projection, without modifying inputs.

    Indices are zero-based in ``list(body.iter(w('p')))``. Prior table-cell
    paragraphs therefore contribute to a selected instance's counter. Unknown
    schemes in other instances are not parsed, but unresolved prior inheritance
    refuses if it might change a selected counter. Labels include punctuation;
    they are neither citations nor estimates of Word's visual layout.
    """
    return _Resolver(parts, root).resolve(paragraph_indices)


def numbering_effects_for_deletion(
    parts: Mapping[str, bytes], rejected_root: etree._Element,
    accepted_root: etree._Element, deleted_index: int,
) -> dict[str, list[dict]]:
    """Prove local neighbors and globally affected labels for one removed paragraph.

    The caller proves the structural operation and supplies two clean trees.
    This helper only proves numbering and requires the one-paragraph count
    difference. Local neighbors must share the target's direct body/cell
    container; the affected instance still continues across the main story.
    For an insertion, pass the longer clean tree first and the shorter one
    second, then name the returned perspectives at the call site.
    """
    before = _Resolver(parts, rejected_root)
    after = _Resolver(parts, accepted_root)
    if type(deleted_index) is not int or not 0 <= deleted_index < len(before.paragraphs):
        raise ApplyError("invalid_edit", "deleted numbering paragraph index is out of range")
    if len(before.paragraphs) != len(after.paragraphs) + 1:
        _refuse("numbering projections must differ by one paragraph", reason="invalid_projection")
    affected = before.effective(deleted_index).num_id
    selected = {deleted_index}
    owner = before.paragraphs[deleted_index].getparent()
    for index in (deleted_index - 1, deleted_index + 1):
        if (
            0 <= index < len(before.paragraphs)
            # Body/cell document order is global, but a local paragraph
            # boundary never crosses its direct container. A pending mark in
            # the next cell or in body text is not this unit's neighbor.
            and before.paragraphs[index].getparent() is owner
            and before._story_context(index) == "main"
            and before.effective(index).num_id in {None, affected}
        ):
            selected.add(index)
    if affected is not None:
        for index in range(deleted_index + 1, len(before.paragraphs)):
            if before._story_context(index) == "separate":
                continue
            if before.effective(index).num_id == affected:
                selected.add(index)
    indices = sorted(selected)
    return {
        "before": before.resolve(indices),
        "after": after.resolve([index if index < deleted_index else index - 1 for index in indices if index != deleted_index]),
    }
