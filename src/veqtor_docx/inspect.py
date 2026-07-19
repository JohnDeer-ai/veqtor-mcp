# SPDX-License-Identifier: Apache-2.0
"""Bounded, hash-bound inspection of operative DOCX body text.

``inspect_document`` deliberately exposes a narrower surface than a generic
document dump.  It scans the supported ``word/document.xml`` body flow once,
then offers four deterministic views over that exact byte snapshot:

* ``outline`` returns structural headings without clause-body text;
* ``literal_search`` searches explicit phrases within one paragraph at a time;
* ``browse`` pages non-empty supported paragraphs; and
* ``read`` resolves one paragraph or structural section reference.

Every paragraph reference binds the exact DOCX SHA-256, structural position,
accepted/current reading policy and paragraph-text digest.  Clause labels and
headings remain navigation facts; they never replace the hash-bound reference.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from lxml import etree

from ._ooxml import (
    DOCUMENT_PART,
    MOVE_REVISION_TAGS,
    ResourceLimitError,
    TEXT_REVISION_TAGS,
    UserPathError,
    canonical_body_flow_v1,
    current_text_atom,
    iter_canonical_paragraph_nodes,
    load_validated_docx,
    parse_xml,
    read_docx_payload,
    resolve_user_path,
    w,
)
from .contracts import (
    INSPECT_CONTAINER_POLICY_V1,
    INSPECT_MATCH_BASES_V1,
    INSPECT_MATCH_EXACT_LITERAL,
    INSPECT_MATCH_NORMALIZED_CASEFOLD_LITERAL,
    INSPECT_MODE_BROWSE,
    INSPECT_MODE_LITERAL_SEARCH,
    INSPECT_MODE_OUTLINE,
    INSPECT_MODE_READ,
    INSPECT_MODES_V1,
    INSPECT_READING_MODE_V1,
    INSPECT_SEARCH_SCOPE_V1,
)
from .extract import (
    DocxError,
    _NumberingCounters,
    _Style,
    _heading_from_text,
    _parse_numbering,
    _parse_styles,
    _resolve_styles,
    classify_revision_inventory_v2,
)

DEFAULT_MAX_ITEMS = 50
MAX_ITEMS = 100
MAX_PHRASES = 20
MAX_PHRASE_CHARS = 2_000
MAX_TOTAL_PHRASE_CHARS = 10_000
MAX_PARAGRAPH_TEXT_CHARS = 50_000
MAX_RETURNED_TEXT_CHARS = 100_000
MAX_INDEXED_PARAGRAPHS = 10_000
MAX_AGGREGATE_TEXT_CHARS = 2_000_000
MAX_LITERAL_MATCH_CANDIDATES = 10_000
MAX_LITERAL_OCCURRENCES_PER_CANDIDATE = 10_000
SNIPPET_RADIUS = 160

_PART_NAME = DOCUMENT_PART
_CURSOR_SCHEMA_V1 = "cursor.v1"
_CURSOR_MATCH_POLICY_V1 = "literal_match_normalization_v1"
_CURSOR_ORDER_POLICY_V1 = "canonical_inspection_result_order_v1"
_CURSOR_RE = re.compile(r"^c1:([0-9]{1,10}):([0-9a-f]{64})$")
_PARAGRAPH_REF_KEYS = frozenset(
    {
        "file_sha256",
        "schema_version",
        "ref_type",
        "part_name",
        "paragraph_index",
        "paragraph_text_sha256",
        "reading_mode",
        "container_policy",
    }
)
_SECTION_REF_KEYS = frozenset(
    {
        "file_sha256",
        "schema_version",
        "ref_type",
        "part_name",
        "heading_paragraph_index",
        "heading_text_sha256",
        "reading_mode",
        "container_policy",
    }
)

_TYPOGRAPHIC = {
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "–": "-",
    "—": "-",
    "‑": "-",
    "\u00a0": " ",
}

__all__ = [
    "DEFAULT_MAX_ITEMS",
    "InspectError",
    "inspect_document",
]


class InspectError(DocxError):
    """Fail-closed inspection refusal with a stable machine code."""

    def __init__(self, code: str, detail: str, **metadata: object) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.metadata = metadata


@dataclass(frozen=True)
class _Paragraph:
    element: etree._Element
    paragraph_index: int
    container_kind: str
    text: str
    text_sha256: str
    has_tracked_text_revisions: bool


@dataclass(frozen=True)
class _Section:
    heading: _Paragraph
    level: int
    label: str | None
    title: str | None
    label_basis: str | None
    end_paragraph_index_exclusive: int


@dataclass(frozen=True)
class _Snapshot:
    path: str
    file_sha256: str
    paragraphs: tuple[_Paragraph, ...]
    sections: tuple[_Section, ...]
    section_by_paragraph: dict[int, _Section]
    container_coverage: dict[str, Any]
    revision_inventory: dict[str, Any]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _accepted_current_text(paragraph: etree._Element) -> tuple[str, bool]:
    """Canonical current reading under the shared supported-container policy."""
    parts: list[str] = []
    has_tracked_text_revisions = False
    for node in iter_canonical_paragraph_nodes(paragraph):
        if node.tag in TEXT_REVISION_TAGS or node.tag in MOVE_REVISION_TAGS:
            has_tracked_text_revisions = True
        contribution = current_text_atom(node, boundary=paragraph)
        if contribution is not None:
            parts.append(contribution)
    return "".join(parts), has_tracked_text_revisions


def _inspection_outline_level(value: int | None) -> int | None:
    """Normalize Word outline levels before they reach the public schema."""
    if value is None or value == 9:
        # ECMA-376 uses 9 for body text rather than a heading level.
        return None
    if 0 <= value <= 8:
        return value
    raise InspectError(
        "file_unextractable",
        "w:outlineLvl must be an integer from 0 through 9",
    )


def _paragraph_properties(
    paragraph: etree._Element,
    styles: dict[str, _Style],
) -> tuple[int | None, tuple[str, int] | None]:
    ppr = paragraph.find(w("pPr"))
    style_id = None
    para_numpr: tuple[str, int] | None = None
    para_outline: int | None = None
    if ppr is not None:
        style_el = ppr.find(w("pStyle"))
        if style_el is not None:
            style_id = style_el.get(w("val"))
        outline_el = ppr.find(w("outlineLvl"))
        if outline_el is not None and (value := outline_el.get(w("val"))) is not None:
            para_outline = int(value)
        numpr_el = ppr.find(w("numPr"))
        if numpr_el is not None:
            num_id_el = numpr_el.find(w("numId"))
            ilvl_el = numpr_el.find(w("ilvl"))
            if num_id_el is not None and num_id_el.get(w("val")):
                para_numpr = (
                    num_id_el.get(w("val")),
                    int(ilvl_el.get(w("val"))) if ilvl_el is not None else 0,
                )

    resolved = styles.get(style_id, _Style())
    outline = para_outline if para_outline is not None else resolved.outline_lvl
    if para_numpr is not None:
        numpr = None if para_numpr[0] == "0" else para_numpr
    elif resolved.num_id:
        numpr = (resolved.num_id, resolved.ilvl or 0)
    else:
        numpr = None
    return _inspection_outline_level(outline), numpr


def _sections(
    paragraphs: tuple[_Paragraph, ...],
    styles: dict[str, _Style],
    numbering_payload: bytes | None,
) -> tuple[tuple[_Section, ...], dict[int, _Section]]:
    numbering = _NumberingCounters(_parse_numbering(numbering_payload))
    headings: list[tuple[_Paragraph, int, str | None, str | None, str | None]] = []

    for paragraph in paragraphs:
        outline, numpr = _paragraph_properties(paragraph.element, styles)
        computed_label = numbering.label(*numpr) if numpr else None
        if outline is None:
            continue
        manual_label, title = _heading_from_text(paragraph.text)
        label = computed_label or manual_label
        label_basis = (
            "word_numbering_v1"
            if computed_label is not None
            else "explicit_heading_text_v1"
            if manual_label is not None
            else None
        )
        headings.append((paragraph, outline, label, title, label_basis))

    built: list[_Section] = []
    for index, (paragraph, level, label, title, label_basis) in enumerate(headings):
        end = len(paragraphs)
        for candidate, candidate_level, *_ in headings[index + 1 :]:
            if candidate_level <= level:
                end = candidate.paragraph_index
                break
        built.append(
            _Section(
                heading=paragraph,
                level=level,
                label=label,
                title=title,
                label_basis=label_basis,
                end_paragraph_index_exclusive=end,
            )
        )

    by_paragraph: dict[int, _Section] = {}
    stack: list[_Section] = []
    section_index = 0
    for paragraph in paragraphs:
        while (
            section_index < len(built)
            and built[section_index].heading.paragraph_index
            == paragraph.paragraph_index
        ):
            section = built[section_index]
            while stack and stack[-1].level >= section.level:
                stack.pop()
            stack.append(section)
            section_index += 1
        if stack:
            by_paragraph[paragraph.paragraph_index] = stack[-1]
    return tuple(built), by_paragraph


def _load_snapshot(path: str) -> _Snapshot:
    try:
        resolved = resolve_user_path(path)
    except UserPathError as exc:
        raise InspectError(exc.code, exc.detail) from exc
    try:
        payload = read_docx_payload(resolved)
    except ResourceLimitError:
        raise
    except OSError as exc:
        raise InspectError("file_unreadable", "cannot read the DOCX") from exc

    file_sha256 = hashlib.sha256(payload).hexdigest()
    try:
        package = load_validated_docx(
            payload,
            capture=(_PART_NAME, "word/styles.xml", "word/numbering.xml"),
        )
        document_payload = package.parts.get(_PART_NAME)
        if document_payload is None:
            raise InspectError("file_unextractable", f"no {_PART_NAME}")
        document = parse_xml(document_payload)
        body = document.find(w("body"))
        if body is None:
            raise InspectError("file_unextractable", "document has no w:body")
        flow = canonical_body_flow_v1(body)
        if len(flow.paragraphs) > MAX_INDEXED_PARAGRAPHS:
            raise ResourceLimitError(
                "inspect_paragraph_count",
                "document has too many supported body paragraphs to index",
                allowed_count=MAX_INDEXED_PARAGRAPHS,
                observed_count=len(flow.paragraphs),
            )
        built_paragraphs: list[_Paragraph] = []
        aggregate_chars = 0
        for item in flow.paragraphs:
            text, has_tracked = _accepted_current_text(item.element)
            aggregate_chars += len(text)
            if aggregate_chars > MAX_AGGREGATE_TEXT_CHARS:
                raise ResourceLimitError(
                    "inspect_aggregate_text_chars",
                    "supported body text exceeds the inspection index limit",
                    allowed_chars=MAX_AGGREGATE_TEXT_CHARS,
                    observed_chars=aggregate_chars,
                    observed_at_least=True,
                )
            built_paragraphs.append(
                _Paragraph(
                    element=item.element,
                    paragraph_index=item.paragraph_index,
                    container_kind=item.container_kind,
                    text=text,
                    text_sha256=_sha256_text(text),
                    has_tracked_text_revisions=has_tracked,
                )
            )
        paragraphs = tuple(built_paragraphs)
        styles = _resolve_styles(_parse_styles(package.parts.get("word/styles.xml")))
        sections, section_by_paragraph = _sections(
            paragraphs,
            styles,
            package.parts.get("word/numbering.xml"),
        )
        container_coverage = dict(flow.container_policy)
        revision_inventory = dict(
            classify_revision_inventory_v2(document, flow)["revision_inventory"]
        )
    except InspectError as exc:
        exc.metadata.setdefault("observed_source_sha256", file_sha256)
        raise
    except DocxError as exc:
        metadata = getattr(exc, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            exc.metadata = metadata
        metadata.setdefault("observed_source_sha256", file_sha256)
        raise
    except (IndexError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise InspectError(
            "file_unextractable",
            "cannot inspect invalid OOXML values",
            observed_source_sha256=file_sha256,
        ) from exc

    return _Snapshot(
        path=resolved,
        file_sha256=file_sha256,
        paragraphs=paragraphs,
        sections=sections,
        section_by_paragraph=section_by_paragraph,
        container_coverage=container_coverage,
        revision_inventory=revision_inventory,
    )


def _paragraph_ref(snapshot: _Snapshot, paragraph: _Paragraph) -> dict[str, Any]:
    return {
        "schema_version": "paragraph_ref.v1",
        "ref_type": "paragraph",
        "file_sha256": snapshot.file_sha256,
        "part_name": _PART_NAME,
        "paragraph_index": paragraph.paragraph_index,
        "paragraph_text_sha256": paragraph.text_sha256,
        "reading_mode": INSPECT_READING_MODE_V1,
        "container_policy": INSPECT_CONTAINER_POLICY_V1,
    }


def _section_ref(snapshot: _Snapshot, section: _Section) -> dict[str, Any]:
    return {
        "schema_version": "section_ref.v1",
        "ref_type": "section",
        "file_sha256": snapshot.file_sha256,
        "part_name": _PART_NAME,
        "heading_paragraph_index": section.heading.paragraph_index,
        "heading_text_sha256": section.heading.text_sha256,
        "reading_mode": INSPECT_READING_MODE_V1,
        "container_policy": INSPECT_CONTAINER_POLICY_V1,
    }


def _navigation(section: _Section | None) -> dict[str, Any] | None:
    if section is None:
        return None
    return {
        "label": section.label,
        "heading": section.title,
        "level": section.level,
        "basis": "word_outline_level_v1",
        "label_basis": section.label_basis,
    }


def _paragraph_item(snapshot: _Snapshot, paragraph: _Paragraph) -> dict[str, Any]:
    return {
        "paragraph_ref": _paragraph_ref(snapshot, paragraph),
        "container_kind": paragraph.container_kind,
        "has_tracked_text_revisions": paragraph.has_tracked_text_revisions,
        "section_navigation": _navigation(
            snapshot.section_by_paragraph.get(paragraph.paragraph_index)
        ),
        "text": paragraph.text,
    }


def _outline_items(snapshot: _Snapshot) -> list[dict[str, Any]]:
    return [
        {
            "section_ref": _section_ref(snapshot, section),
            "label": section.label,
            "heading": section.title,
            "level": section.level,
            "basis": "word_outline_level_v1",
            "label_basis": section.label_basis,
            "start_paragraph_index": section.heading.paragraph_index,
            "end_paragraph_index_exclusive": section.end_paragraph_index_exclusive,
        }
        for section in snapshot.sections
        if section.heading.text
    ]


def _normalized_with_offsets(
    text: str,
    *,
    casefold: bool,
) -> tuple[str, list[int]]:
    """Normalize like verify_quote while retaining original character offsets."""
    output: list[str] = []
    offsets: list[int] = []
    pending_space: int | None = None
    for source_index, char in enumerate(text):
        translated = _TYPOGRAPHIC.get(char, char)
        if translated.isspace():
            if output and pending_space is None:
                pending_space = source_index
            continue
        if pending_space is not None:
            output.append(" ")
            offsets.append(pending_space)
            pending_space = None
        rendered = translated.casefold() if casefold else translated
        output.extend(rendered)
        offsets.extend([source_index] * len(rendered))
    return "".join(output), offsets


def _overlapping_starts(text: str, phrase: str) -> list[int]:
    starts: list[int] = []
    cursor = 0
    while (found := text.find(phrase, cursor)) != -1:
        starts.append(found)
        if len(starts) > MAX_LITERAL_OCCURRENCES_PER_CANDIDATE:
            raise InspectError(
                "resource_limit_exceeded",
                "one paragraph contains too many literal occurrences",
                limit="literal_occurrences_per_candidate",
                allowed_count=MAX_LITERAL_OCCURRENCES_PER_CANDIDATE,
                observed_count=len(starts),
                observed_at_least=True,
            )
        cursor = found + 1
    return starts


def _match_spans(
    text: str,
    phrase: str,
    match_basis: str,
) -> list[tuple[int, int]]:
    if match_basis == INSPECT_MATCH_EXACT_LITERAL:
        return [
            (start, start + len(phrase)) for start in _overlapping_starts(text, phrase)
        ]
    casefold = match_basis == INSPECT_MATCH_NORMALIZED_CASEFOLD_LITERAL
    normalized_text, offsets = _normalized_with_offsets(text, casefold=casefold)
    normalized_phrase, _ = _normalized_with_offsets(phrase, casefold=casefold)
    if not normalized_phrase:
        return []
    spans: list[tuple[int, int]] = []
    for start in _overlapping_starts(normalized_text, normalized_phrase):
        end = start + len(normalized_phrase)
        spans.append((offsets[start], offsets[end - 1] + 1))
    return spans


def _snippet(text: str, span: tuple[int, int]) -> dict[str, Any]:
    start, end = span
    snippet_start = max(0, start - SNIPPET_RADIUS)
    snippet_end = min(len(text), end + SNIPPET_RADIUS)
    return {
        "text": text[snippet_start:snippet_end],
        "match_start": start - snippet_start,
        "match_end": end - snippet_start,
        "truncated_before": snippet_start > 0,
        "truncated_after": snippet_end < len(text),
    }


def _literal_matches(
    snapshot: _Snapshot,
    phrases: list[str],
    match_basis: str,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for paragraph in snapshot.paragraphs:
        if not paragraph.text:
            continue
        for phrase_index, phrase in enumerate(phrases):
            spans = _match_spans(paragraph.text, phrase, match_basis)
            if not spans:
                continue
            if len(matches) >= MAX_LITERAL_MATCH_CANDIDATES:
                raise InspectError(
                    "resource_limit_exceeded",
                    "literal search has too many complete match candidates",
                    limit="literal_match_candidates",
                    allowed_count=MAX_LITERAL_MATCH_CANDIDATES,
                    observed_count=len(matches) + 1,
                    observed_at_least=True,
                )
            matches.append(
                {
                    "phrase_index": phrase_index,
                    "match_basis": match_basis,
                    "occurrence_count": len(spans),
                    "paragraph_ref": _paragraph_ref(snapshot, paragraph),
                    "container_kind": paragraph.container_kind,
                    "has_tracked_text_revisions": (
                        paragraph.has_tracked_text_revisions
                    ),
                    "section_navigation": _navigation(
                        snapshot.section_by_paragraph.get(paragraph.paragraph_index)
                    ),
                    "snippet": _snippet(paragraph.text, spans[0]),
                }
            )
    return matches


def _validate_common_inputs(
    mode: object,
    phrases: object,
    match_basis: object,
    selection: object,
    cursor: object,
    max_items: object,
) -> None:
    if not isinstance(mode, str) or mode not in INSPECT_MODES_V1:
        raise InspectError("invalid_mode", "mode is not supported")
    if isinstance(max_items, bool) or not isinstance(max_items, int):
        raise InspectError("invalid_limit", "max_items must be an integer")
    if not 1 <= max_items <= MAX_ITEMS:
        raise InspectError(
            "invalid_limit",
            f"max_items must be between 1 and {MAX_ITEMS}",
            allowed_count=MAX_ITEMS,
        )
    if cursor is not None and not isinstance(cursor, str):
        raise InspectError("invalid_cursor", "cursor must be a string")

    if mode == INSPECT_MODE_LITERAL_SEARCH:
        if not isinstance(phrases, list) or not phrases:
            raise InspectError(
                "phrases_missing", "literal_search requires a non-empty phrases array"
            )
        if len(phrases) > MAX_PHRASES:
            raise InspectError(
                "resource_limit_exceeded",
                "literal_search has too many phrases",
                limit="phrase_count",
                allowed_count=MAX_PHRASES,
                observed_count=len(phrases),
            )
        total_chars = 0
        for phrase in phrases:
            if not isinstance(phrase, str) or not phrase or not phrase.strip():
                raise InspectError(
                    "invalid_phrase", "every phrase must contain non-whitespace text"
                )
            if any(0xD800 <= ord(character) <= 0xDFFF for character in phrase):
                raise InspectError(
                    "invalid_phrase",
                    "every phrase must be a Unicode scalar sequence",
                )
            if len(phrase) > MAX_PHRASE_CHARS:
                raise InspectError(
                    "resource_limit_exceeded",
                    "literal_search phrase is too long",
                    limit="phrase_chars",
                    allowed_chars=MAX_PHRASE_CHARS,
                    observed_chars=len(phrase),
                )
            total_chars += len(phrase)
        if total_chars > MAX_TOTAL_PHRASE_CHARS:
            raise InspectError(
                "resource_limit_exceeded",
                "literal_search phrases are too large in aggregate",
                limit="total_phrase_chars",
                allowed_chars=MAX_TOTAL_PHRASE_CHARS,
                observed_chars=total_chars,
            )
        if (
            not isinstance(match_basis, str)
            or match_basis not in INSPECT_MATCH_BASES_V1
        ):
            raise InspectError(
                "match_basis_missing",
                "literal_search requires one supported match_basis",
            )
        if selection is not None:
            raise InspectError(
                "invalid_request", "literal_search does not accept selection"
            )
        return

    if phrases is not None or match_basis is not None:
        raise InspectError(
            "invalid_request", f"{mode} does not accept phrases or match_basis"
        )
    if mode == INSPECT_MODE_READ:
        if not isinstance(selection, dict):
            raise InspectError("selection_missing", "read requires one selection")
        if set(selection) not in ({"paragraph_ref"}, {"section_ref"}):
            raise InspectError(
                "invalid_selection",
                "selection must contain exactly one paragraph_ref or section_ref",
            )
    elif selection is not None:
        raise InspectError("invalid_request", f"{mode} does not accept selection")


def _validated_ref(
    value: object,
    *,
    expected_keys: frozenset[str],
    snapshot: _Snapshot,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise InspectError("invalid_reference", "reference fields do not match v1")
    if value.get("file_sha256") != snapshot.file_sha256:
        raise InspectError(
            "file_sha256_mismatch",
            "reference was produced from different DOCX bytes",
            claimed_source_sha256=value.get("file_sha256"),
            observed_source_sha256=snapshot.file_sha256,
        )
    if value.get("part_name") != _PART_NAME:
        raise InspectError("reference_mismatch", "reference part is not supported")
    if value.get("reading_mode") != INSPECT_READING_MODE_V1:
        raise InspectError(
            "reference_mismatch", "reference reading_mode is unsupported"
        )
    if value.get("container_policy") != INSPECT_CONTAINER_POLICY_V1:
        raise InspectError(
            "reference_mismatch", "reference container_policy is unsupported"
        )
    return value


def _resolve_paragraph(snapshot: _Snapshot, value: object) -> _Paragraph:
    ref = _validated_ref(
        value,
        expected_keys=_PARAGRAPH_REF_KEYS,
        snapshot=snapshot,
    )
    if (
        ref.get("schema_version") != "paragraph_ref.v1"
        or ref.get("ref_type") != "paragraph"
    ):
        raise InspectError(
            "reference_mismatch", "paragraph reference type is unsupported"
        )
    index = ref.get("paragraph_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise InspectError("invalid_reference", "paragraph_index is invalid")
    paragraph = next(
        (item for item in snapshot.paragraphs if item.paragraph_index == index),
        None,
    )
    if paragraph is None:
        raise InspectError("reference_not_found", "paragraph is not in supported scope")
    if ref.get("paragraph_text_sha256") != paragraph.text_sha256:
        raise InspectError(
            "reference_mismatch", "paragraph text digest does not match its position"
        )
    return paragraph


def _resolve_section(snapshot: _Snapshot, value: object) -> _Section:
    ref = _validated_ref(
        value,
        expected_keys=_SECTION_REF_KEYS,
        snapshot=snapshot,
    )
    if (
        ref.get("schema_version") != "section_ref.v1"
        or ref.get("ref_type") != "section"
    ):
        raise InspectError(
            "reference_mismatch", "section reference type is unsupported"
        )
    index = ref.get("heading_paragraph_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise InspectError("invalid_reference", "heading_paragraph_index is invalid")
    section = next(
        (item for item in snapshot.sections if item.heading.paragraph_index == index),
        None,
    )
    if section is None:
        raise InspectError("reference_not_found", "section is not in supported outline")
    if ref.get("heading_text_sha256") != section.heading.text_sha256:
        raise InspectError(
            "reference_mismatch", "section heading digest does not match its position"
        )
    return section


def _cursor_binding(
    snapshot: _Snapshot,
    mode: str,
    phrases: list[str] | None,
    match_basis: str | None,
    selection: dict[str, Any] | None,
    result_set_sha256: str,
) -> str:
    value = {
        "cursor_schema": _CURSOR_SCHEMA_V1,
        "file_sha256": snapshot.file_sha256,
        "search_scope": INSPECT_SEARCH_SCOPE_V1,
        "reading_mode": INSPECT_READING_MODE_V1,
        "container_policy": INSPECT_CONTAINER_POLICY_V1,
        "match_policy": _CURSOR_MATCH_POLICY_V1,
        "order_policy": _CURSOR_ORDER_POLICY_V1,
        "mode": mode,
        "phrases": phrases,
        "match_basis": match_basis,
        "selection": selection,
        "result_set_sha256": result_set_sha256,
    }
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _result_set_sha256(items: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        {
            "schema_version": "inspection_result_set.v1",
            "items": items,
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cursor_offset(cursor: str | None, binding: str) -> int:
    if cursor is None:
        return 0
    match = _CURSOR_RE.fullmatch(cursor)
    if match is None:
        raise InspectError("invalid_cursor", "cursor does not match cursor.v1")
    if match.group(2) != binding:
        raise InspectError("cursor_mismatch", "cursor does not match this inspection")
    return int(match.group(1))


def _next_cursor(offset: int, binding: str) -> str:
    return f"c1:{offset}:{binding}"


def _page_items(
    items: list[dict[str, Any]],
    *,
    offset: int,
    max_items: int,
    base_returned_text_chars: int = 0,
) -> tuple[list[dict[str, Any]], int | None]:
    if offset < 0 or offset > len(items):
        raise InspectError("invalid_cursor", "cursor lies outside the result set")
    if base_returned_text_chars > MAX_RETURNED_TEXT_CHARS:
        raise InspectError(
            "resource_limit_exceeded",
            "response metadata exceeds the supported text limit",
            limit="returned_text_chars",
            allowed_chars=MAX_RETURNED_TEXT_CHARS,
            observed_chars=base_returned_text_chars,
        )
    page: list[dict[str, Any]] = []
    returned_chars = base_returned_text_chars
    position = offset
    while position < len(items) and len(page) < max_items:
        item = items[position]
        text_values = list(_returned_document_text_values(item))
        longest_text = max((len(value) for value in text_values), default=0)
        item_text_chars = sum(len(value) for value in text_values)
        if longest_text > MAX_PARAGRAPH_TEXT_CHARS:
            raise InspectError(
                "resource_limit_exceeded",
                "one paragraph exceeds the supported read limit",
                limit="paragraph_text_chars",
                allowed_chars=MAX_PARAGRAPH_TEXT_CHARS,
                observed_chars=longest_text,
            )
        if page and returned_chars + item_text_chars > MAX_RETURNED_TEXT_CHARS:
            break
        if not page and returned_chars + item_text_chars > MAX_RETURNED_TEXT_CHARS:
            raise InspectError(
                "resource_limit_exceeded",
                "one result item exceeds the response text limit",
                limit="returned_text_chars",
                allowed_chars=MAX_RETURNED_TEXT_CHARS,
                observed_chars=returned_chars + item_text_chars,
            )
        page.append(item)
        returned_chars += item_text_chars
        position += 1
    return page, position if position < len(items) else None


def _returned_document_text_values(value: object) -> list[str]:
    """Return every document-derived string that will appear in one response."""
    values: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"text", "heading", "label"} and isinstance(item, str):
                values.append(item)
            elif isinstance(item, (dict, list)):
                values.extend(_returned_document_text_values(item))
    elif isinstance(value, list):
        for item in value:
            values.extend(_returned_document_text_values(item))
    return values


def _limits(max_items: int) -> dict[str, Any]:
    return {
        "requested_max_items": max_items,
        "max_items": MAX_ITEMS,
        "max_phrases": MAX_PHRASES,
        "max_phrase_chars": MAX_PHRASE_CHARS,
        "max_total_phrase_chars": MAX_TOTAL_PHRASE_CHARS,
        "max_paragraph_text_chars": MAX_PARAGRAPH_TEXT_CHARS,
        "max_returned_text_chars": MAX_RETURNED_TEXT_CHARS,
        "max_indexed_paragraphs": MAX_INDEXED_PARAGRAPHS,
        "max_aggregate_text_chars": MAX_AGGREGATE_TEXT_CHARS,
        "max_literal_match_candidates": MAX_LITERAL_MATCH_CANDIDATES,
        "max_literal_occurrences_per_candidate": (
            MAX_LITERAL_OCCURRENCES_PER_CANDIDATE
        ),
        "wall_clock_partial_results": False,
    }


def _inspect_snapshot(
    snapshot: _Snapshot,
    mode: str,
    *,
    phrases: list[str] | None = None,
    match_basis: str | None = None,
    selection: dict[str, Any] | None = None,
    cursor: str | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> dict[str, Any]:
    assert mode in INSPECT_MODES_V1
    assert phrases is None or isinstance(phrases, list)
    assert match_basis is None or isinstance(match_basis, str)
    assert selection is None or isinstance(selection, dict)

    result: dict[str, Any] = {
        "mode": mode,
        "path": snapshot.path,
        "file_sha256": snapshot.file_sha256,
        "part_name": _PART_NAME,
        "search_scope": INSPECT_SEARCH_SCOPE_V1,
        "reading_mode": INSPECT_READING_MODE_V1,
        "container_policy": INSPECT_CONTAINER_POLICY_V1,
        "has_tracked_text_revisions": any(
            paragraph.has_tracked_text_revisions for paragraph in snapshot.paragraphs
        ),
        "revision_inventory": snapshot.revision_inventory,
        "limits": _limits(max_items),
    }

    if mode == INSPECT_MODE_OUTLINE:
        all_items = _outline_items(snapshot)
        result_key = "sections"
    elif mode == INSPECT_MODE_LITERAL_SEARCH:
        assert phrases is not None and match_basis is not None
        all_items = _literal_matches(snapshot, phrases, match_basis)
        result_key = "matches"
        result["match_basis"] = match_basis
        result["phrase_count"] = len(phrases)
    elif mode == INSPECT_MODE_BROWSE:
        all_items = [
            _paragraph_item(snapshot, paragraph)
            for paragraph in snapshot.paragraphs
            if paragraph.text
        ]
        result_key = "paragraphs"
    else:
        assert selection is not None
        if "paragraph_ref" in selection:
            if cursor is not None:
                raise InspectError(
                    "invalid_cursor", "paragraph read does not accept a cursor"
                )
            paragraph = _resolve_paragraph(snapshot, selection["paragraph_ref"])
            all_items = [_paragraph_item(snapshot, paragraph)]
            result["selection_kind"] = "paragraph"
        else:
            section = _resolve_section(snapshot, selection["section_ref"])
            all_items = [
                _paragraph_item(snapshot, paragraph)
                for paragraph in snapshot.paragraphs
                if section.heading.paragraph_index
                <= paragraph.paragraph_index
                < section.end_paragraph_index_exclusive
                and paragraph.text
            ]
            result["selection_kind"] = "section"
            result["section_navigation"] = _navigation(section)
        result_key = "paragraphs"

    result_set_sha256 = _result_set_sha256(all_items)
    binding = _cursor_binding(
        snapshot,
        mode,
        phrases,
        match_basis,
        selection,
        result_set_sha256,
    )
    offset = _cursor_offset(cursor, binding)
    page, next_offset = _page_items(
        all_items,
        offset=offset,
        max_items=max_items,
        base_returned_text_chars=sum(
            len(value)
            for value in _returned_document_text_values(
                result.get("section_navigation")
            )
        ),
    )
    next_cursor = (
        _next_cursor(next_offset, binding) if next_offset is not None else None
    )
    result[result_key] = page
    result["coverage"] = {
        "scan_complete": True,
        "body_paragraph_count": len(snapshot.paragraphs),
        "nonempty_body_paragraph_count": sum(
            bool(paragraph.text) for paragraph in snapshot.paragraphs
        ),
        "eligible_item_count": len(all_items),
        "returned_item_count": len(page),
        "cursor_offset": offset,
        "output_truncated": next_cursor is not None,
        "complete_literal_match_count": (
            len(all_items) if mode == INSPECT_MODE_LITERAL_SEARCH else None
        ),
        "included_parts": ["word/document.xml"],
        "excluded_parts": [
            "word/header*.xml",
            "word/footer*.xml",
            "word/footnotes.xml",
            "word/endnotes.xml",
            "word/comments*.xml",
        ],
        "included_containers": ["body", "table_cell"],
        "container_coverage": snapshot.container_coverage,
    }
    result["next_cursor"] = next_cursor
    return result


def inspect_document(
    path: str,
    mode: str,
    *,
    phrases: list[str] | None = None,
    match_basis: str | None = None,
    selection: dict[str, Any] | None = None,
    cursor: str | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> dict[str, Any]:
    """Inspect one exact DOCX snapshot under a bounded, explicit policy."""
    _validate_common_inputs(mode, phrases, match_basis, selection, cursor, max_items)
    snapshot = _load_snapshot(path)
    try:
        return _inspect_snapshot(
            snapshot,
            mode,
            phrases=phrases,
            match_basis=match_basis,
            selection=selection,
            cursor=cursor,
            max_items=max_items,
        )
    except DocxError as exc:
        metadata = getattr(exc, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            exc.metadata = metadata
        metadata.setdefault("observed_source_sha256", snapshot.file_sha256)
        raise
