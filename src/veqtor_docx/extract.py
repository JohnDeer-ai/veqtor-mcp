# SPDX-License-Identifier: Apache-2.0
"""Extract tracked changes from a DOCX file as verifiable change units.

M1 scope: text insertions, deletions and replacements in ``word/document.xml``
(including inside tables). Formatting changes, moves and other revision markup
are counted in ``unsupported_revisions`` instead of being silently dropped —
the toolchain reports document facts honestly or not at all.

Everything here is deterministic: the same file bytes always produce the same
change units, ids and anchors. ``change_unit_id`` values are sequential in
document order; references carry the raw OOXML revision ids and the file hash
so any claim can be re-checked against the source document.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from ._ooxml import (
    DOCUMENT_PART,
    MOVE_REVISION_TAGS,
    TEXT_REVISION_TAGS,
    UNSUPPORTED_REVISION_TAGS,
    DocxError,
    UserPathError,
    ZIP_READ_ERRORS,
    current_text_atom,
    parse_xml,
    resolve_user_path,
    run_text,
    text_atom,
    w,
)
from .contracts import (
    STRUCTURAL_REVISION_PARENT_NAMES_V1,
    TEXT_REVISION_SUFFIX_BY_NAME_V1,
)

_MANUAL_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)*[A-Za-z]?|\([a-z]\)|[A-Z]\.)[.\s]\s*")
_DOTTED_MANUAL_NUMBER_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)+[A-Za-z]?)(?:[.\s])\s*"
)
PARAGRAPH_CONTEXT_RADIUS = 240

__all__ = ["DocxError", "extract_redlines"]


@dataclass
class _Style:
    outline_lvl: int | None = None
    num_id: str | None = None
    ilvl: int | None = None
    based_on: str | None = None


def _read_parts(payload: bytes, path: str, parts: tuple[str, ...]) -> dict[str, bytes | None]:
    """Read the named parts from one in-memory snapshot of the package."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = set(zf.namelist())
            if DOCUMENT_PART not in names:
                raise DocxError(f"no {DOCUMENT_PART} in {path}")
            return {
                part: zf.read(part) if part in names else None for part in parts
            }
    except ZIP_READ_ERRORS as exc:
        raise DocxError(f"cannot open {path}: {exc}") from exc


def _parse_styles(data: bytes | None) -> dict[str, _Style]:
    styles: dict[str, _Style] = {}
    if data is None:
        return styles
    root = parse_xml(data)
    for el in root.findall(w("style")):
        style_id = el.get(w("styleId"))
        if not style_id:
            continue
        style = _Style()
        based = el.find(w("basedOn"))
        if based is not None:
            style.based_on = based.get(w("val"))
        ppr = el.find(w("pPr"))
        if ppr is not None:
            outline = ppr.find(w("outlineLvl"))
            if outline is not None and (val := outline.get(w("val"))) is not None:
                style.outline_lvl = int(val)
            numpr = ppr.find(w("numPr"))
            if numpr is not None:
                num_id = numpr.find(w("numId"))
                ilvl = numpr.find(w("ilvl"))
                if num_id is not None:
                    style.num_id = num_id.get(w("val"))
                style.ilvl = int(ilvl.get(w("val"))) if ilvl is not None else 0
        styles[style_id] = style
    return styles


def _resolve_style(styles: dict[str, _Style], style_id: str | None) -> _Style:
    """Walk the basedOn chain and return the effective outline/numbering."""
    resolved = _Style()
    seen: set[str] = set()
    current = style_id
    while current and current not in seen:
        seen.add(current)
        style = styles.get(current)
        if style is None:
            break
        if resolved.outline_lvl is None:
            resolved.outline_lvl = style.outline_lvl
        if resolved.num_id is None and style.num_id is not None:
            resolved.num_id = style.num_id
            resolved.ilvl = style.ilvl
        current = style.based_on
    return resolved


@dataclass
class _NumberingLevel:
    fmt: str = "decimal"
    text: str = ""
    start: int = 1


@dataclass
class _NumberingInstance:
    levels: dict[int, _NumberingLevel]
    overridden: frozenset[int]


def _parse_numbering(data: bytes | None) -> dict[str, _NumberingInstance]:
    """Map numId -> level definitions plus which levels carry overrides."""
    if data is None:
        return {}
    root = parse_xml(data)
    abstracts: dict[str, dict[int, _NumberingLevel]] = {}
    for abstract in root.findall(w("abstractNum")):
        abstract_id = abstract.get(w("abstractNumId"))
        levels: dict[int, _NumberingLevel] = {}
        for lvl in abstract.findall(w("lvl")):
            ilvl_attr = lvl.get(w("ilvl"))
            if ilvl_attr is None:
                continue
            level = _NumberingLevel()
            fmt = lvl.find(w("numFmt"))
            if fmt is not None:
                level.fmt = fmt.get(w("val")) or "decimal"
            text = lvl.find(w("lvlText"))
            if text is not None:
                level.text = text.get(w("val")) or ""
            start = lvl.find(w("start"))
            if start is not None and (val := start.get(w("val"))) is not None:
                level.start = int(val)
            levels[int(ilvl_attr)] = level
        if abstract_id is not None:
            abstracts[abstract_id] = levels
    nums: dict[str, _NumberingInstance] = {}
    for num in root.findall(w("num")):
        num_id = num.get(w("numId"))
        ref = num.find(w("abstractNumId"))
        if num_id is None or ref is None:
            continue
        overridden = frozenset(
            int(val)
            for override in num.findall(w("lvlOverride"))
            if (val := override.get(w("ilvl"))) is not None
        )
        nums[num_id] = _NumberingInstance(
            levels=abstracts.get(ref.get(w("val")), {}),
            overridden=overridden,
        )
    return nums


def _format_counter(value: int, fmt: str) -> str:
    if fmt == "lowerLetter":
        return chr(ord("a") + (value - 1) % 26)
    if fmt == "upperLetter":
        return chr(ord("A") + (value - 1) % 26)
    if fmt in ("lowerRoman", "upperRoman"):
        numerals = [
            (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"),
            (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"),
            (5, "v"), (4, "iv"), (1, "i"),
        ]
        out = ""
        remaining = value
        for base, glyph in numerals:
            while remaining >= base:
                out += glyph
                remaining -= base
        return out.upper() if fmt == "upperRoman" else out
    return str(value)


class _NumberingCounters:
    """Compute rendered list labels by walking paragraphs in document order.

    A label is emitted only when it cannot be a fabrication: v1 does not
    implement ``lvlOverride``/``startOverride`` restarts, so any referenced
    level that carries an override — or that never actually incremented in
    the document — suppresses the label. A missing label is honest; a wrong
    one would poison every citation built on it.
    """

    def __init__(self, definitions: dict[str, _NumberingInstance]) -> None:
        self._defs = definitions
        self._counters: dict[str, dict[int, int]] = {}

    def label(self, num_id: str, ilvl: int) -> str | None:
        instance = self._defs.get(num_id)
        if instance is None or ilvl not in instance.levels:
            return None
        levels = instance.levels
        counters = self._counters.setdefault(num_id, {})
        counters[ilvl] = counters.get(ilvl, levels[ilvl].start - 1) + 1
        for deeper in [lvl for lvl in counters if lvl > ilvl]:
            del counters[deeper]

        template = levels[ilvl].text or f"%{ilvl + 1}."
        referenced = [int(m) - 1 for m in re.findall(r"%(\d+)", template)]
        for level_index in referenced:
            if level_index in instance.overridden:
                return None
            if level_index not in counters:
                return None
        label = re.sub(
            r"%(\d+)",
            lambda m: _format_counter(
                counters[int(m.group(1)) - 1],
                levels.get(int(m.group(1)) - 1, _NumberingLevel()).fmt,
            ),
            template,
        )
        return label.rstrip(".") or None


@dataclass(frozen=True)
class _ReadingToken:
    node: etree._Element
    start: int
    end: int


@dataclass(frozen=True)
class _ParagraphReading:
    text: str
    offsets_before: dict[int, int]
    tokens: tuple[_ReadingToken, ...]


def _current_node_text(node: etree._Element) -> str:
    """One node's contribution to the accepted/current paragraph reading."""
    return current_text_atom(node) or ""


def _current_paragraph_reading(para: etree._Element) -> _ParagraphReading:
    """Build one canonical string/offset map from the same OOXML traversal."""
    parts: list[str] = []
    offsets_before: dict[int, int] = {}
    tokens: list[_ReadingToken] = []
    offset = 0
    for node in para.iter():
        offsets_before[id(node)] = offset
        contribution = _current_node_text(node)
        if not contribution:
            continue
        start = offset
        parts.append(contribution)
        offset += len(contribution)
        tokens.append(_ReadingToken(node, start, offset))
    return _ParagraphReading("".join(parts), offsets_before, tuple(tokens))


def _paragraph_new_text(para: etree._Element) -> str:
    """Paragraph text as it reads with insertions accepted and deletions gone."""
    return _current_paragraph_reading(para).text


def _manual_label_from_text(text: str, *, dotted_only: bool = False) -> str | None:
    """Return only an explicit leading manual label; never infer one."""
    pattern = _DOTTED_MANUAL_NUMBER_RE if dotted_only else _MANUAL_NUMBER_RE
    match = pattern.match(text.strip())
    return match.group(1).rstrip(".") if match else None


def _heading_from_text(text: str) -> tuple[str | None, str | None]:
    """Split a heading paragraph into (manual number label, heading text)."""
    stripped = text.strip()
    if not stripped:
        return None, None
    match = _MANUAL_NUMBER_RE.match(stripped)
    label = _manual_label_from_text(stripped)
    if match is not None:
        stripped = stripped[match.end():].strip()
    first_sentence = stripped.split(". ", 1)[0].strip().rstrip(".")
    return label, first_sentence[:120] or None


@dataclass
class _Wrapper:
    element: etree._Element
    kind: str  # "ins" | "del"
    author: str
    date: str | None
    rev_id: str | None
    text: str
    nested_in: str | None = None  # containing w:ins id for cross-author counters


def _nested_del_author_differs(node: etree._Element, wrapper: etree._Element) -> bool | None:
    """For a text node under ``wrapper``: None if not inside a nested w:del,
    else True when that deletion's author differs from the wrapper's."""
    for ancestor in node.iterancestors():
        if ancestor is wrapper:
            return None
        if ancestor.tag == w("moveFrom"):
            return False  # moved-away text is hidden regardless of author
        if ancestor.tag == w("del"):
            return (ancestor.get(w("author")) or "") != (wrapper.get(w("author")) or "")
    return None


def _wrapper_text(element: etree._Element, kind: str) -> str:
    """Visible text a revision wrapper contributes to its side of the change.

    An insertion keeps the author's full proposal: text the same author later
    deleted (a retraction) is hidden, but text struck by ANOTHER author — a
    counter — still belongs to the proposal as made; the counter is reported
    as its own change unit. A deletion counts ``w:delText``.
    """
    parts: list[str] = []
    for node in element.iter():
        tag = node.tag
        value = text_atom(node, include_deleted_text=True)
        if value is None:
            continue
        if kind == "ins" and tag != w("delText"):
            if _nested_del_author_differs(node, element) is False:
                continue  # same-author retraction or moved away: hidden
            parts.append(value)
        elif kind == "ins" and tag == w("delText"):
            # Counter-struck text still reads as part of their proposal.
            if _nested_del_author_differs(node, element):
                parts.append(value)
        elif kind == "del" and tag != w("t"):
            parts.append(value)
    return "".join(parts)


def _foreign_nested_dels(ins_element: etree._Element) -> list[etree._Element]:
    """Nested deletions by another author inside a pending insertion."""
    ins_author = ins_element.get(w("author")) or ""
    return [
        el
        for el in ins_element.iter(w("del"))
        if (el.get(w("author")) or "") != ins_author
    ]


def _paragraph_stream(para: etree._Element) -> list[tuple[str, object]]:
    """Flatten a paragraph into ('wrap', _Wrapper) and ('text', str) items.

    Only direct run content counts as separating text; pPr is skipped so the
    paragraph-mark markup never breaks adjacency of run-level revisions.
    """
    items: list[tuple[str, object]] = []

    def walk(node: etree._Element) -> None:
        for child in node:
            tag = child.tag
            if tag == w("pPr"):
                continue
            if tag in TEXT_REVISION_TAGS:
                kind = "ins" if tag == w("ins") else "del"
                items.append(
                    (
                        "wrap",
                        _Wrapper(
                            element=child,
                            kind=kind,
                            author=child.get(w("author")) or "",
                            date=child.get(w("date")),
                            rev_id=child.get(w("id")),
                            text=_wrapper_text(child, kind),
                        ),
                    )
                )
                if kind == "ins":
                    # Cross-author deletions nested inside a pending insertion
                    # are counters: distinct facts with their own author. Emit
                    # them right after their host so an adjacent same-author
                    # replacement insertion can merge into one counter unit.
                    for index, nested in enumerate(_foreign_nested_dels(child)):
                        if index:
                            items.append(("text", " "))  # keep counters apart
                        items.append(
                            (
                                "wrap",
                                _Wrapper(
                                    element=nested,
                                    kind="del",
                                    author=nested.get(w("author")) or "",
                                    date=nested.get(w("date")),
                                    rev_id=nested.get(w("id")),
                                    text=_wrapper_text(nested, "del"),
                                    nested_in=child.get(w("id")),
                                ),
                            )
                        )
            elif tag in MOVE_REVISION_TAGS:
                items.append(("move", child))
            elif tag == w("r"):
                items.append(("text", run_text(child)))
            else:
                # hyperlink, smartTag, bookmark wrappers etc: recurse so the
                # runs inside keep their document order.
                walk(child)

    walk(para)
    return items


def _paragraph_context(
    para: etree._Element,
    group: list[_Wrapper],
    new_text: str,
) -> dict[str, object]:
    """Bounded context around one unit in the paragraph's current reading."""
    del new_text  # element identity, never text uniqueness, locates the unit
    reading = _current_paragraph_reading(para)
    current = reading.text
    group_elements = {id(wrapper.element) for wrapper in group}
    visible_spans = [
        (token.start, token.end)
        for token in reading.tokens
        if any(
            id(ancestor) in group_elements
            for ancestor in token.node.iterancestors()
        )
    ]
    if visible_spans:
        start = min(span[0] for span in visible_spans)
        end = max(span[1] for span in visible_spans)
    else:
        start = min(
            reading.offsets_before.get(id(group[0].element), len(current)),
            len(current),
        )
        end = start
    before_start = max(0, start - PARAGRAPH_CONTEXT_RADIUS)
    after_end = min(len(current), end + PARAGRAPH_CONTEXT_RADIUS)
    return {
        "before": current[before_start:start],
        "after": current[end:after_end],
        "manual_label": _manual_label_from_text(current, dotted_only=True),
        "truncated_before": before_start > 0,
        "truncated_after": after_end < len(current),
    }


def _group_units(items: list[tuple[str, object]]) -> list[list[_Wrapper]]:
    """Group directly adjacent ins/del wrappers into logical change units.

    Only wrappers that touch with no plain run in between merge — that keeps
    every unit's old/new text a contiguous, verbatim quote of one document
    version. Any plain run (even whitespace), a move wrapper or an author
    switch closes the unit. Deterministic by construction.
    """
    groups: list[list[_Wrapper]] = []
    current: list[_Wrapper] = []

    def close() -> None:
        nonlocal current
        if current:
            groups.append(current)
            current = []

    for kind, payload in items:
        if kind == "wrap":
            if current and current[-1].author != payload.author:
                close()
            current.append(payload)
        else:  # plain run or move wrapper breaks adjacency
            close()
    close()
    return groups


def _group_change_fields(group: list[_Wrapper]) -> dict[str, object] | None:
    """Return the canonical, position-independent facts for one group.

    Extraction and edit anchoring share this classifier.  In particular,
    apply must not rediscover a unit by a revision id: OOXML revision ids can
    be duplicated by document merges and by third-party producers.
    """
    ins_text = "".join(item.text for item in group if item.kind == "ins")
    del_text = "".join(item.text for item in group if item.kind == "del")
    countering = any(item.nested_in for item in group)
    if countering:
        change_type = "counter"
    elif ins_text and del_text:
        change_type = "replace"
    elif ins_text:
        change_type = "insert"
    elif del_text:
        change_type = "delete"
    else:
        return None

    dates = [item.date for item in group if item.date]
    countered_by = [
        nested.get(w("id"))
        for item in group
        if item.kind == "ins"
        for nested in _foreign_nested_dels(item.element)
        if nested.get(w("id"))
    ]
    return {
        "change_type": change_type,
        "author": group[0].author,
        "date": min(dates) if dates else None,
        "old_text": del_text or None,
        "new_text": ins_text or None,
        "revision_ids": [item.rev_id for item in group if item.rev_id],
        "countered_by": countered_by,
    }


def extract_redlines(path: str) -> dict:
    """Extract tracked changes from ``path`` as deterministic change units.

    The file is read exactly once; ``file_sha256`` and every extracted fact
    derive from that single byte snapshot, so the hash always names the
    bytes the facts came from.
    """
    try:
        path = resolve_user_path(path)
    except UserPathError as exc:
        raise DocxError(str(exc)) from exc
    # MCP clients pass user-written paths; "~/Deals/x.docx" must just work,
    # and references must carry the openable expanded path.
    try:
        payload = Path(path).read_bytes()
    except OSError as exc:
        raise DocxError(f"cannot read {path}: {exc}") from exc
    return _extract_from_bytes(payload, path)


def _extract_from_bytes(payload: bytes, path: str) -> dict:
    """Extract from an in-memory snapshot; ``path`` is a label for output."""
    file_sha256 = hashlib.sha256(payload).hexdigest()
    try:
        return _extract_snapshot(payload, path, file_sha256)
    except DocxError as exc:
        metadata = getattr(exc, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
            exc.metadata = metadata
        metadata.setdefault("observed_source_sha256", file_sha256)
        raise
    except (IndexError, KeyError, OverflowError, TypeError, ValueError) as exc:
        error = DocxError(f"cannot extract {path}: invalid OOXML value")
        error.metadata = {"observed_source_sha256": file_sha256}
        raise error from exc


def _extract_snapshot(payload: bytes, path: str, file_sha256: str) -> dict:
    """Decode a hash-identified package snapshot behind one error boundary."""
    parts = _read_parts(
        payload, path, (DOCUMENT_PART, "word/styles.xml", "word/numbering.xml")
    )
    document = parse_xml(parts[DOCUMENT_PART])
    styles = _parse_styles(parts["word/styles.xml"])
    numbering = _NumberingCounters(_parse_numbering(parts["word/numbering.xml"]))

    body = document.find(w("body"))
    if body is None:
        raise DocxError(f"no w:body in {path}")

    change_units: list[dict] = []
    unsupported: dict[str, int] = {}
    anchor: dict | None = None

    def bump(key: str) -> None:
        unsupported[key] = unsupported.get(key, 0) + 1

    for paragraph_index, para in enumerate(body.iter(w("p"))):
        ppr = para.find(w("pPr"))
        style_id = None
        para_numpr: tuple[str, int] | None = None
        para_outline: int | None = None
        if ppr is not None:
            style_el = ppr.find(w("pStyle"))
            if style_el is not None:
                style_id = style_el.get(w("val"))
            outline_el = ppr.find(w("outlineLvl"))
            if outline_el is not None and (val := outline_el.get(w("val"))) is not None:
                para_outline = int(val)
            numpr_el = ppr.find(w("numPr"))
            if numpr_el is not None:
                num_id_el = numpr_el.find(w("numId"))
                ilvl_el = numpr_el.find(w("ilvl"))
                if num_id_el is not None and num_id_el.get(w("val")):
                    # numId "0" is the OOXML idiom for "numbering off here",
                    # overriding any numbering the style would contribute.
                    para_numpr = (
                        num_id_el.get(w("val")),
                        int(ilvl_el.get(w("val"))) if ilvl_el is not None else 0,
                    )

        resolved = _resolve_style(styles, style_id)
        outline = para_outline if para_outline is not None else resolved.outline_lvl
        if para_numpr is not None:
            numpr = None if para_numpr[0] == "0" else para_numpr
        elif resolved.num_id:
            numpr = (resolved.num_id, resolved.ilvl or 0)
        else:
            numpr = None

        # Numbering counters advance for every numbered paragraph, whether or
        # not it is a heading; that is how rendered labels stay correct.
        computed_label = numbering.label(*numpr) if numpr else None

        if outline is not None:
            manual_label, heading = _heading_from_text(_paragraph_new_text(para))
            label = computed_label or manual_label
            anchor = (
                {"label": label, "heading": heading}
                if (label or heading)
                else None
            )

        items = _paragraph_stream(para)
        for kind, payload in items:
            if kind == "move":
                bump(etree.QName(payload.tag).localname)

        for group_index, group in enumerate(_group_units(items)):
            fields = _group_change_fields(group)
            if fields is None:
                continue  # empty wrappers carry no reviewable text
            unit = {
                "change_unit_id": f"cu_{len(change_units) + 1:03d}",
                "file_sha256": file_sha256,
                "change_type": fields["change_type"],
                "author": fields["author"],
                "date": fields["date"],
                "clause_anchor": anchor,
                "paragraph_context": _paragraph_context(
                    para, group, str(fields["new_text"] or "")
                ),
                "old_text": fields["old_text"],
                "new_text": fields["new_text"],
                "reference": {
                    "path": path,
                    "part_name": DOCUMENT_PART,
                    "paragraph_index": paragraph_index,
                    "group_index": group_index,
                    "revision_ids": fields["revision_ids"],
                },
            }
            if fields["countered_by"]:
                # This unit's proposal has been struck (fully or in part) by
                # another author; the strikes are separate "counter" units.
                unit["countered_by"] = fields["countered_by"]
            change_units.append(unit)

    # One classification pass over every revision element. Run-level ins/del
    # became change units above; everything else — paragraph marks, inserted
    # or deleted table rows/cells, section markers, formatting changes — is
    # counted here so no revision markup is ever silently dropped.
    revision_count = 0
    for el in document.iter():
        if el.tag in UNSUPPORTED_REVISION_TAGS:
            bump(etree.QName(el.tag).localname)
        elif el.tag in TEXT_REVISION_TAGS:
            revision_count += 1
            parent = el.getparent()
            if parent is None:
                continue
            parent_name = etree.QName(parent.tag).localname
            suffix = TEXT_REVISION_SUFFIX_BY_NAME_V1[
                etree.QName(el.tag).localname
            ]
            grand = parent.getparent()
            if parent_name == "rPr" and grand is not None and grand.tag == w("pPr"):
                bump(f"paragraphMark{suffix}")
            elif parent_name in STRUCTURAL_REVISION_PARENT_NAMES_V1:
                bump(f"{parent_name}{suffix}")

    return {
        "path": path,
        "file_sha256": file_sha256,
        "part_name": DOCUMENT_PART,
        "revision_count": revision_count,
        "change_units": change_units,
        "unsupported_revisions": unsupported,
    }
