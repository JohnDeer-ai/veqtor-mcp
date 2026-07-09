# SPDX-License-Identifier: Apache-2.0
"""Apply explicit tracked edits to a DOCX, fail-closed, with a round-trip proof.

M2 slice 2. The contract (see API.md and the M2 gate in ROADMAP.md):

- Edits apply only at an anchor produced by the read path: the caller passes a
  ``change_unit_id`` + ``file_sha256`` from :func:`veqtor_docx.extract_redlines`.
- Three edit forms, all visible tracked changes, never silent rewrites:

  * plain replace/delete — ``delete_text`` occurs exactly once in the anchored
    clause's current reading and lies entirely in untouched runs;
  * counter — ``delete_text`` lies entirely inside ONE counterparty pending
    insertion: the strike is written as our ``w:del`` nested in their
    ``w:ins`` (their proposal stays visible), the replacement as our
    insertion right after theirs;
  * reinstate — ``reinstate_text`` matches text hidden inside exactly one
    counterparty deletion in the clause: it is re-inserted as our visible
    insertion placed before their deletion.

- Several edits may target the same paragraph; spans must not overlap and are
  applied right to left so earlier offsets stay valid. Adjacent same-author
  markup merges in extraction (and in Word's review pane), so layouts that
  would leave two of our operations touching are refused with stable codes:
  a pending insertion is countered once, with the full replacement
  (``already_countered`` on a second attempt, in the same or a later call);
  no edit may start immediately after a countered insertion; new markup may
  not be written flush against our own earlier tracked changes
  (``adjacent_to_own_revision``) — extend the neighbouring edit instead.
  These planner rules give early, specific errors, but the guarantee itself
  is systematic: the pre-write round-trip re-extraction refuses ANY layout
  that would lose or alter a pre-existing change unit, under the same stable
  code, whether or not a specific rule anticipated it.
- Anything unresolvable — hash mismatch, unknown anchor, zero or multiple
  matches, spans mixing plain and tracked text, our own pending insertions,
  unusual run shapes — raises :class:`ApplyError` and writes nothing.
- Edits are atomic: the output file appears only after every edit applied and
  the round-trip check passed; a failed check removes the temp artifact.
- Round-trip proof: ``extract_redlines(output)`` must return exactly the prior
  change units plus the proposed edits, and nothing outside the touched
  paragraphs may differ. The structural check is paragraph-granular: an edit
  anchored in a table cell does not exempt the rest of that table.

Determinism: new revisions carry a fixed author, no ``w:date`` (optional in
OOXML), and ids continuing the document's own sequence, so applying the same
edits to the same file always yields byte-identical output.
"""

from __future__ import annotations

import copy
import hashlib
import io
import os
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from ._ooxml import DOCUMENT_PART, MOVE_REVISION_TAGS, TEXT_REVISION_TAGS, parse_xml, w
from .extract import DocxError, _extract_from_bytes, extract_redlines

DEFAULT_AUTHOR = "Veqtor MCP"

_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
# Non-visible elements allowed to sit between covered runs during surgery.
_INERT_TAGS = frozenset({w("bookmarkStart"), w("bookmarkEnd"), w("proofErr")})


class ApplyError(DocxError):
    """A fail-closed refusal: the message starts with a stable error code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


# ---------------------------------------------------------------------------
# Paragraph scanning
# ---------------------------------------------------------------------------


@dataclass
class _Seg:
    node: etree._Element  # the w:t element
    run: etree._Element
    start: int
    end: int
    plain: bool
    container: etree._Element | None  # enclosing pending w:ins, if any


def _paragraph_segments(para: etree._Element) -> list[_Seg]:
    """Char-mapped ``w:t`` segments of the paragraph's current reading.

    The current reading accepts insertions and drops deleted/moved-away text
    (whoever deleted it). ``plain`` marks text no revision wrapper touches
    and whose run is a direct child of the paragraph; ``container`` records
    the enclosing pending insertion for counter edits.
    """
    segments: list[_Seg] = []
    offset = 0
    for node in para.iter(w("t")):
        hidden = False
        container: etree._Element | None = None
        run = node.getparent()
        for ancestor in node.iterancestors():
            if ancestor is para:
                break
            if ancestor.tag in (w("del"), w("moveFrom")):
                hidden = True
                break
            if ancestor.tag == w("ins") or ancestor.tag in MOVE_REVISION_TAGS:
                container = ancestor
        if hidden:
            continue
        text = node.text or ""
        plain = (
            container is None
            and run is not None
            and run.tag == w("r")
            and run.getparent() is para
        )
        segments.append(_Seg(node, run, offset, offset + len(text), plain, container))
        offset += len(text)
    return segments


def _reading_text(segments: list[_Seg]) -> str:
    return "".join(seg.node.text or "" for seg in segments)


def _reading_offset_before(para: etree._Element, element: etree._Element) -> int:
    """Reading offset at which ``element`` sits inside the paragraph."""
    offset = 0
    for node in para.iter():
        if node is element:
            return offset
        if node.tag != w("t"):
            continue
        hidden = False
        for ancestor in node.iterancestors():
            if ancestor is para:
                break
            if ancestor.tag in (w("del"), w("moveFrom")):
                hidden = True
                break
        if not hidden:
            offset += len(node.text or "")
    return offset


def _element_reading_span(
    para: etree._Element, element: etree._Element
) -> tuple[int, int]:
    """Reading-offset span the element's visible text occupies."""
    start = _reading_offset_before(para, element)
    length = 0
    for node in element.iter(w("t")):
        hidden = False
        for ancestor in node.iterancestors():
            if ancestor is element:
                break
            if ancestor.tag in (w("del"), w("moveFrom")):
                hidden = True
                break
        if not hidden:
            length += len(node.text or "")
    return start, start + length


def _run_is_simple(run: etree._Element) -> bool:
    """True when the run holds only rPr and w:t children (safe to split/wrap)."""
    return all(child.tag in (w("rPr"), w("t")) for child in run)


def _split_run_at(segments: list[_Seg], offset: int) -> None:
    """Split the run containing ``offset`` so a run boundary falls exactly there."""
    for seg in segments:
        if seg.start < offset < seg.end:
            if not _run_is_simple(seg.run) or len(seg.run.findall(w("t"))) != 1:
                raise ApplyError(
                    "unsupported_run_shape",
                    "the matched span borders a run with mixed content",
                )
            cut = offset - seg.start
            text = seg.node.text or ""
            right = copy.deepcopy(seg.run)
            seg.node.text = text[:cut]
            right_t = right.find(w("t"))
            right_t.text = text[cut:]
            right_t.set(_XML_SPACE, "preserve")
            seg.node.set(_XML_SPACE, "preserve")
            seg.run.addnext(right)
            return
        if seg.start == offset:
            return
    return


def _next_revision_id(document: etree._Element) -> int:
    highest = 100
    for el in document.iter():
        val = el.get(w("id"))
        if val is not None:
            try:
                highest = max(highest, int(val))
            except ValueError:
                continue
    return highest + 1


def _hidden_del_text(deletion: etree._Element) -> str:
    return "".join(node.text or "" for node in deletion.iter(w("delText")))


def _hosts_our_strike(element: etree._Element, author: str) -> bool:
    """True for a pending insertion that already carries our nested strike."""
    return element.tag == w("ins") and any(
        (nested.get(w("author")) or "") == author
        for nested in element.iter(w("del"))
    )


def _next_non_inert(element: etree._Element) -> etree._Element | None:
    following = element.getnext()
    while following is not None and following.tag in _INERT_TAGS:
        following = following.getnext()
    return following


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class _PlannedEdit:
    anchor_id: str
    paragraph: etree._Element
    op: str  # "plain" | "counter" | "reinstate"
    delete_text: str | None
    insert_text: str
    span: tuple[int, int]  # reading offsets; reinstate uses a zero-width point
    container: etree._Element | None  # counter: their w:ins; reinstate: their w:del
    del_id: str | None
    ins_id: str | None


def _validate_edit_shapes(edits: list) -> None:
    """Reject malformed input with stable error codes before any work.

    MCP clients send loosely typed JSON; none of it may reach the OOXML layer
    as a raw TypeError/AttributeError — fail closed with an error code.
    """
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ApplyError("invalid_edit", f"edits[{index}] must be an object")
        anchor = edit.get("anchor")
        if not isinstance(anchor, dict):
            raise ApplyError(
                "anchor_missing", f"edits[{index}].anchor must be an object"
            )
        for key in ("change_unit_id", "file_sha256"):
            value = anchor.get(key)
            if not isinstance(value, str) or not value:
                raise ApplyError(
                    "anchor_missing",
                    f"edits[{index}].anchor.{key} must be a non-empty string",
                )
        delete_text = edit.get("delete_text")
        reinstate_text = edit.get("reinstate_text")
        if delete_text is not None and reinstate_text is not None:
            raise ApplyError(
                "invalid_edit",
                f"edits[{index}] must use either delete_text or reinstate_text, not both",
            )
        if reinstate_text is not None:
            if not isinstance(reinstate_text, str) or not reinstate_text:
                raise ApplyError(
                    "invalid_edit",
                    f"edits[{index}].reinstate_text must be a non-empty string",
                )
            if edit.get("insert_text"):
                raise ApplyError(
                    "invalid_edit",
                    f"edits[{index}]: reinstate_text re-adds the deleted text "
                    "verbatim and does not combine with insert_text",
                )
        else:
            if not isinstance(delete_text, str) or not delete_text:
                raise ApplyError(
                    "delete_text_missing",
                    f"edits[{index}].delete_text must be a non-empty string",
                )
            insert_text = edit.get("insert_text", "")
            if insert_text is not None and not isinstance(insert_text, str):
                raise ApplyError(
                    "invalid_edit", f"edits[{index}].insert_text must be a string"
                )


def _resolve_anchor_paragraph(document: etree._Element, unit: dict) -> etree._Element:
    """Resolve the anchor unit to its paragraph element."""
    wanted = set(unit["reference"]["revision_ids"])
    for el in document.iter():
        if el.tag in TEXT_REVISION_TAGS and el.get(w("id")) in wanted:
            for ancestor in el.iterancestors(w("p")):
                return ancestor
    raise ApplyError(
        "anchor_not_found",
        f"revision ids of {unit['change_unit_id']} are not present in the document",
    )


def _match_delete_span(
    para: etree._Element, delete_text: str, author: str
) -> tuple[str, tuple[int, int], etree._Element | None]:
    """Locate ``delete_text`` and classify the edit: plain or counter."""
    segments = _paragraph_segments(para)
    reading = _reading_text(segments)

    matches = []
    cursor = reading.find(delete_text)
    while cursor != -1:
        matches.append(cursor)
        cursor = reading.find(delete_text, cursor + 1)
    if not matches:
        raise ApplyError(
            "delete_text_not_found",
            "delete_text does not occur in the anchored clause's current reading",
        )
    if len(matches) > 1:
        raise ApplyError(
            "delete_text_ambiguous",
            f"delete_text occurs {len(matches)} times in the anchored clause",
        )
    span = (matches[0], matches[0] + len(delete_text))

    touched = [s for s in segments if s.start < span[1] and s.end > span[0]]
    if all(s.plain for s in touched):
        return "plain", span, None

    containers = {s.container for s in touched}
    if len(containers) == 1:
        container = containers.pop()
        if (
            container is not None
            and container.tag == w("ins")
            and container.getparent() is para
            and all(s.run.getparent() is container for s in touched)
        ):
            container_author = container.get(w("author")) or ""
            if container_author == author:
                raise ApplyError(
                    "overlaps_tracked_changes",
                    "the matched span lies in your own pending insertion; "
                    "counter edits target the counterparty's proposals",
                )
            return "counter", span, container
    raise ApplyError(
        "overlaps_tracked_changes",
        "the matched span mixes plain text and tracked changes, or sits in "
        "markup the write path does not support",
    )


def _match_reinstate(
    para: etree._Element, reinstate_text: str, author: str
) -> tuple[tuple[int, int], etree._Element]:
    """Locate the single counterparty deletion hiding ``reinstate_text``."""
    hits: list[etree._Element] = []
    total = 0
    for deletion in para.iter(w("del")):
        if deletion.getparent() is not para:
            continue  # nested strikes are counters, not reinstate targets
        if (deletion.get(w("author")) or "") == author:
            continue
        count = _hidden_del_text(deletion).count(reinstate_text)
        if count:
            hits.append(deletion)
            total += count
    if total == 0:
        raise ApplyError(
            "reinstate_text_not_found",
            "reinstate_text does not occur inside a counterparty deletion in "
            "the anchored clause",
        )
    if total > 1 or len(hits) > 1:
        raise ApplyError(
            "reinstate_text_ambiguous",
            "reinstate_text occurs more than once among the clause's deletions",
        )
    deletion = hits[0]
    previous = deletion.getprevious()
    while previous is not None and previous.tag in _INERT_TAGS:
        previous = previous.getprevious()
    if previous is not None and (
        previous.tag in TEXT_REVISION_TAGS or previous.tag in MOVE_REVISION_TAGS
    ):
        raise ApplyError(
            "reinstate_position_unsupported",
            "the counterparty deletion directly follows other tracked markup; "
            "reinstating here would break its grouping",
        )
    point = _reading_offset_before(para, deletion)
    return (point, point), deletion


# ---------------------------------------------------------------------------
# Surgery
# ---------------------------------------------------------------------------


def _new_insertion(ins_id: str, author: str, text: str) -> etree._Element:
    insertion = etree.Element(w("ins"))
    insertion.set(w("id"), ins_id)
    insertion.set(w("author"), author)
    run = etree.SubElement(insertion, w("r"))
    t = etree.SubElement(run, w("t"))
    t.set(_XML_SPACE, "preserve")
    t.text = text
    return insertion


def _wrap_covered_runs(
    parent: etree._Element, plan: _PlannedEdit, author: str
) -> etree._Element:
    """Split runs at the span bounds and wrap the covered runs in our w:del.

    ``parent`` is the paragraph for plain edits or the counterparty's w:ins
    for counter edits; covered runs must be its direct, simple children.
    """
    paragraph = plan.paragraph
    span_start, span_end = plan.span

    segments = _paragraph_segments(paragraph)
    if _reading_text(segments)[span_start:span_end] != plan.delete_text:
        raise ApplyError(
            "round_trip_failed",
            "the matched span moved while applying other edits",
        )
    _split_run_at(segments, span_start)
    segments = _paragraph_segments(paragraph)
    _split_run_at(segments, span_end)
    segments = _paragraph_segments(paragraph)

    covered = [
        s
        for s in segments
        if s.start >= span_start and s.end <= span_end and s.end > s.start
    ]
    if not covered or "".join(s.node.text or "" for s in covered) != plan.delete_text:
        raise ApplyError(
            "unsupported_run_shape",
            "could not align the matched span onto whole runs",
        )
    for seg in covered:
        if not _run_is_simple(seg.run) or seg.run.getparent() is not parent:
            raise ApplyError(
                "unsupported_run_shape",
                "a covered run contains content other than text",
            )

    children = list(parent)
    first_index = children.index(covered[0].run)
    last_index = children.index(covered[-1].run)
    covered_runs = {seg.run for seg in covered}
    for element in children[first_index : last_index + 1]:
        if element not in covered_runs and element.tag not in _INERT_TAGS:
            raise ApplyError(
                "overlaps_tracked_changes",
                "the matched span is interleaved with non-text markup",
            )

    # New markup written flush against our own existing markup would merge
    # with it in extraction (and in Word's review pane), silently mutating a
    # previously written unit. Refuse instead. A countered insertion counts
    # as ours at its right edge: extraction surfaces the nested strikes right
    # after the host, so anything written flush after it merges with them.
    def _neighbour(index: int, step: int) -> etree._Element | None:
        cursor = index + step
        while 0 <= cursor < len(children) and children[cursor].tag in _INERT_TAGS:
            cursor += step
        return children[cursor] if 0 <= cursor < len(children) else None

    for element, before_span in ((_neighbour(first_index, -1), True), (_neighbour(last_index, 1), False)):
        if element is None or not (
            element.tag in TEXT_REVISION_TAGS or element.tag in MOVE_REVISION_TAGS
        ):
            continue
        if (element.get(w("author")) or "") == author or (
            before_span and _hosts_our_strike(element, author)
        ):
            raise ApplyError(
                "adjacent_to_own_revision",
                "the matched span touches our own earlier tracked change; "
                "extend that edit instead of stacking a new one against it",
            )

    deletion = etree.Element(w("del"))
    deletion.set(w("id"), plan.del_id)
    deletion.set(w("author"), author)
    parent.insert(first_index, deletion)
    for seg in covered:
        deletion.append(seg.run)  # moves the run inside w:del
        for t in seg.run.findall(w("t")):
            t.tag = w("delText")
            t.set(_XML_SPACE, "preserve")
    return deletion


def _apply_plan(plan: _PlannedEdit, author: str) -> None:
    if plan.op == "plain":
        deletion = _wrap_covered_runs(plan.paragraph, plan, author)
        if plan.ins_id is not None:
            deletion.addnext(_new_insertion(plan.ins_id, author, plan.insert_text))
    elif plan.op == "counter":
        _wrap_covered_runs(plan.container, plan, author)
        if plan.ins_id is not None:
            # The replacement goes after THEIR insertion, as Word does: their
            # proposal stays visible with our strike; ours reads right after.
            plan.container.addnext(
                _new_insertion(plan.ins_id, author, plan.insert_text)
            )
    else:  # reinstate
        insertion = _new_insertion(plan.ins_id, author, plan.insert_text)
        plan.container.addprevious(insertion)


# ---------------------------------------------------------------------------
# Round-trip proof
# ---------------------------------------------------------------------------


def _collateral_outside(
    original_root: etree._Element,
    mutated_root: etree._Element,
    touched_positions: set[int],
) -> list[str]:
    """Paragraph-granular no-collateral proof.

    Exempting whole top-level body blocks would let a collateral change in
    another row of the same table slip through: an edit anchored in one table
    cell must not excuse the rest of the table. Instead, every untouched
    paragraph (by document-order position) must serialize identically, and
    the document skeleton with all paragraph content masked out must be
    byte-identical too — that covers table rows and cells, section
    properties and block order.
    """
    issues: list[str] = []
    original_paras = list(original_root.iter(w("p")))
    mutated_paras = list(mutated_root.iter(w("p")))
    if len(original_paras) != len(mutated_paras):
        return [
            f"paragraph count changed from {len(original_paras)} to {len(mutated_paras)}"
        ]
    for index, (old_para, new_para) in enumerate(zip(original_paras, mutated_paras)):
        if index in touched_positions:
            continue
        if etree.tostring(old_para) != etree.tostring(new_para):
            issues.append(f"paragraph {index} changed outside the touched anchors")

    def skeleton(root: etree._Element) -> bytes:
        clone = copy.deepcopy(root)
        for para in clone.iter(w("p")):
            para.clear()
        return etree.tostring(clone)

    if skeleton(original_root) != skeleton(mutated_root):
        issues.append("markup outside paragraphs changed")
    return issues


def _unit_content(unit: dict) -> tuple:
    return (
        unit["change_type"],
        unit["author"],
        unit["date"],
        unit["old_text"],
        unit["new_text"],
        tuple(unit["reference"]["revision_ids"]),
    )


def _round_trip_verdict(
    before: list[tuple], after: list[tuple], expected_new: list[tuple]
) -> ApplyError | None:
    """Classify a failed unit-multiset proof.

    The planner refuses every layout it can foresee; this is the systematic
    net behind it. If re-extraction would lose or alter any pre-existing
    unit, the cause is by construction adjacency-merging of markup — report
    it under the same stable code as the planner, whatever the layout was.
    Only a mismatch that leaves the baseline intact (the proposed edits did
    not come back as promised) is a genuine round-trip failure.
    """
    if sorted(after, key=repr) == sorted(before + expected_new, key=repr):
        return None
    baseline_lost = Counter(before) - Counter(after)
    if baseline_lost:
        lost = sum(baseline_lost.values())
        return ApplyError(
            "adjacent_to_own_revision",
            f"re-extraction would alter {lost} pre-existing change unit(s); "
            "the edits sit flush against existing markup — lay them out "
            "apart from it or extend that edit instead",
        )
    return ApplyError(
        "round_trip_failed",
        "extract_redlines(output) does not return exactly the prior units "
        "plus the proposed edits",
    )


def _expected_unit(plan: _PlannedEdit, author: str) -> tuple:
    if plan.op == "reinstate":
        return ("insert", author, None, None, plan.insert_text, (plan.ins_id,))
    revision_ids = [plan.del_id] + ([plan.ins_id] if plan.ins_id else [])
    if plan.op == "counter":
        change_type = "counter"
    else:
        change_type = "replace" if plan.ins_id else "delete"
    return (
        change_type,
        author,
        None,
        plan.delete_text,
        plan.insert_text or None,
        tuple(revision_ids),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def apply_edits(
    source_path: str,
    output_path: str,
    edits: list[dict],
    author: str = DEFAULT_AUTHOR,
) -> dict:
    """Apply explicit tracked edits at read-path anchors; fail closed."""
    source = str(Path(source_path).expanduser())
    output = str(Path(output_path).expanduser())
    # The MCP schema already types `edits` as an array, but this is a public
    # Python API too — a non-list must fail closed, not TypeError.
    if not isinstance(edits, list):
        raise ApplyError("invalid_edit", "edits must be an array of edit objects")
    if not edits:
        raise ApplyError("no_edits", "edits list is empty")
    _validate_edit_shapes(edits)
    if os.path.exists(output):
        raise ApplyError("output_exists", f"refusing to overwrite {output}")

    # One byte snapshot of the source: the sha the anchors are checked
    # against, the baseline units and the parts being rewritten must all
    # describe the same bytes, even if the file changes underneath us.
    try:
        source_payload = Path(source).read_bytes()
    except OSError as exc:
        raise ApplyError("file_unreadable", f"cannot read {source}: {exc}") from exc
    source_sha = hashlib.sha256(source_payload).hexdigest()
    baseline = _extract_from_bytes(source_payload, source)
    units_by_id = {u["change_unit_id"]: u for u in baseline["change_units"]}

    with zipfile.ZipFile(io.BytesIO(source_payload)) as zf:
        infos = zf.infolist()
        parts = {info.filename: zf.read(info.filename) for info in infos}
    # Untouched source bytes for the structural half of the round-trip proof.
    source_document_bytes = parts[DOCUMENT_PART]
    document = parse_xml(parts[DOCUMENT_PART])
    body = document.find(w("body"))
    if body is None:
        raise DocxError(f"no w:body in {source}")

    next_id = _next_revision_id(document)
    planned: list[_PlannedEdit] = []
    for edit in edits:
        anchor = edit["anchor"]
        if anchor["file_sha256"] != source_sha:
            raise ApplyError(
                "file_sha256_mismatch",
                "anchor was produced from a different file than source_path",
            )
        unit = units_by_id.get(anchor["change_unit_id"])
        if unit is None:
            raise ApplyError(
                "anchor_not_found",
                f"{anchor['change_unit_id']} is not a change unit of the source file",
            )
        paragraph = _resolve_anchor_paragraph(document, unit)

        reinstate_text = edit.get("reinstate_text")
        if reinstate_text is not None:
            span, container = _match_reinstate(paragraph, reinstate_text, author)
            ins_id = str(next_id)
            next_id += 1
            planned.append(
                _PlannedEdit(
                    anchor["change_unit_id"], paragraph, "reinstate", None,
                    reinstate_text, span, container, None, ins_id,
                )
            )
            continue

        delete_text = edit["delete_text"]
        insert_text = edit.get("insert_text") or ""
        op, span, container = _match_delete_span(paragraph, delete_text, author)
        del_id = str(next_id)
        next_id += 1
        ins_id = None
        if insert_text:
            ins_id = str(next_id)
            next_id += 1
        planned.append(
            _PlannedEdit(
                anchor["change_unit_id"], paragraph, op, delete_text,
                insert_text, span, container, del_id, ins_id,
            )
        )

    # Same-paragraph edits: spans must not overlap; apply right to left so
    # earlier offsets stay valid while later text shifts.
    by_paragraph: dict[int, list[_PlannedEdit]] = {}
    paragraphs: dict[int, etree._Element] = {}
    for plan in planned:
        key = id(plan.paragraph)
        by_paragraph.setdefault(key, []).append(plan)
        paragraphs[key] = plan.paragraph
    for key, plans in by_paragraph.items():
        targeted_deletions: set[int] = set()
        for plan in plans:
            if plan.op == "reinstate":
                if id(plan.container) in targeted_deletions:
                    raise ApplyError(
                        "edits_overlap",
                        "two edits reinstate text from the same deletion",
                    )
                targeted_deletions.add(id(plan.container))

        # Counter layout constraints. The strike nests inside the countered
        # insertion, the replacement goes right after it, and extraction
        # groups adjacent same-author markup — so combinations that would
        # leave two of our operations touching cannot be laid out
        # unambiguously and are refused rather than written confusingly.
        countered_hosts: set[int] = set()
        for plan in plans:
            if plan.op != "counter":
                continue
            if id(plan.container) in countered_hosts:
                raise ApplyError(
                    "edits_overlap",
                    "one pending insertion can be countered only once; "
                    "consolidate into a single counter that covers the whole "
                    "replacement",
                )
            countered_hosts.add(id(plan.container))
            if any(
                (nested.get(w("author")) or "") == author
                for nested in plan.container.iter(w("del"))
            ):
                # A follow-up counter cannot be laid out either: the strike
                # and replacement would touch our earlier markup and merge.
                raise ApplyError(
                    "already_countered",
                    "this insertion already carries our counter; counter an "
                    "insertion once, with the full replacement text",
                )
            following = _next_non_inert(plan.container)
            following_is_wrapper = following is not None and (
                following.tag in TEXT_REVISION_TAGS
                or following.tag in MOVE_REVISION_TAGS
            )
            if plan.ins_id is not None and following_is_wrapper:
                raise ApplyError(
                    "counter_position_unsupported",
                    "the countered insertion is directly followed by other "
                    "tracked markup; placing the replacement there would "
                    "break its grouping",
                )
            if following_is_wrapper and (following.get(w("author")) or "") == author:
                # Even a pure strike surfaces at the host's right edge in
                # extraction and would merge with our markup right after it.
                raise ApplyError(
                    "adjacent_to_own_revision",
                    "the countered insertion is directly followed by our own "
                    "earlier tracked change; the strike would merge with it",
                )
            host_end = _element_reading_span(plan.paragraph, plan.container)[1]
            for other in plans:
                if other is not plan and other.op == "plain" and other.span[0] == host_end:
                    raise ApplyError(
                        "edits_overlap",
                        "an edit starts immediately after a countered "
                        "insertion; without untouched text between them the "
                        "operations cannot stay distinct — apply it in a "
                        "separate call",
                    )

        ordered = sorted(plans, key=lambda p: (p.span[0], p.span[1]))
        for left, right in zip(ordered, ordered[1:]):
            if left.span[1] > right.span[0] or left.span[0] == right.span[0]:
                raise ApplyError(
                    "edits_overlap",
                    "two edits target overlapping text in the same paragraph",
                )
        for plan in sorted(plans, key=lambda p: p.span[0], reverse=True):
            _apply_plan(plan, author)

    # ------------------------------------------------------------------
    # Write to a temp artifact, prove the round trip, then move into place.
    # ------------------------------------------------------------------
    parts[DOCUMENT_PART] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        + etree.tostring(document)
    )
    # Unique temp name in the output directory: a fixed suffix could clobber
    # an unrelated pre-existing sibling file.
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(output) or ".",
            prefix=os.path.basename(output) + ".",
            suffix=".veqtor-tmp",
        )
        os.close(tmp_fd)
    except OSError as exc:
        raise ApplyError("output_unwritable", str(exc)) from exc
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for info in infos:
                fresh = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                fresh.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(fresh, parts[info.filename])

        result = extract_redlines(tmp_path)

        expected_new = [_expected_unit(plan, author) for plan in planned]
        verdict = _round_trip_verdict(
            list(map(_unit_content, baseline["change_units"])),
            list(map(_unit_content, result["change_units"])),
            expected_new,
        )
        if verdict is not None:
            raise verdict

        touched_elements = list({id(p.paragraph): p.paragraph for p in planned}.values())
        touched_positions = {
            index
            for index, para in enumerate(document.iter(w("p")))
            if any(para is t for t in touched_elements)
        }
        collateral = _collateral_outside(
            parse_xml(source_document_bytes),
            parse_xml(parts[DOCUMENT_PART]),
            touched_positions,
        )
        if collateral:
            raise ApplyError(
                "round_trip_failed",
                f"collateral changes outside the touched anchors: {collateral}",
            )
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    os.replace(tmp_path, output)

    return {
        "status": "ok",
        "output_path": output,
        "applied": [
            {
                "change_unit_id": plan.anchor_id,
                "operation": plan.op if plan.op != "plain" else (
                    "replace" if plan.ins_id else "delete"
                ),
                "deleted_text": plan.delete_text,
                "inserted_text": plan.insert_text or None,
                "tracked_revision_ids": (
                    ([plan.del_id] if plan.del_id else [])
                    + ([plan.ins_id] if plan.ins_id else [])
                ),
            }
            for plan in planned
        ],
        "round_trip_check": {
            "status": "passed",
            "collateral_changes": [],
            "comparison": "ooxml_semantic_diff_outside_touched_anchors",
        },
    }
