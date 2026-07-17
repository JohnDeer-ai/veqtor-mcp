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
  the round-trip check passed; a failed check attempts to remove the temp
  artifact without letting a cleanup refusal mask the original error.
- Round-trip proof: ``extract_redlines(output)`` must return exactly the prior
  change units plus the proposed edits, and nothing outside the touched
  paragraphs may differ. The structural check is paragraph-granular: an edit
  anchored in a table cell does not exempt the rest of that table.

Determinism: new revisions carry one caller-supplied author (the MCP server
holds its configured value constant for the process), no ``w:date`` (optional
in OOXML), and ids continuing the document's own sequence. Applying the same
edits with the same author to the same file yields byte-identical output.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass

from lxml import etree

from ._ooxml import (
    ArchiveValidationError,
    DOCUMENT_PART,
    MOVE_REVISION_TAGS,
    ResourceLimitError,
    TEXT_REVISION_TAGS,
    UserPathError,
    ValidatedDocx,
    ZIP_READ_ERRORS,
    current_text_atom,
    is_xml_text_compatible,
    load_validated_docx,
    parse_xml,
    read_docx_payload,
    resolve_user_path,
    text_atom,
    tracked_change_author_validation_error,
    validate_docx_payload_size,
    w,
)
from .contracts import (
    APPLY_OPERATION_COUNTER,
    APPLY_OPERATION_DELETE,
    APPLY_OPERATION_REINSTATE,
    APPLY_OPERATION_REPLACE,
    PREFLIGHT_EDIT_STATUS_APPLICABLE,
    PREFLIGHT_EDIT_STATUS_BLOCKED,
    PREFLIGHT_EDIT_STATUS_NOT_EVALUATED,
    PREFLIGHT_EDIT_STATUS_PLANNED,
    PREFLIGHT_POSITION_STATUS_NOT_EVALUATED,
    PREFLIGHT_POSITION_STATUS_SUPPORTED,
    PREFLIGHT_POSITION_STATUS_UNSUPPORTED,
    RESULT_STATUS_OK,
    ROUND_TRIP_COMPARISON_CURRENT,
    ROUND_TRIP_STATUS_FAILED,
    ROUND_TRIP_STATUS_PASSED,
)
from .extract import (
    DocxError,
    _extract_from_bytes,
    _extract_validated,
    _group_change_fields,
    _group_units,
    _paragraph_stream,
)

DEFAULT_AUTHOR = "Veqtor MCP"
_PLAN_OPERATION_PLAIN = "plain"
MAX_REVISION_ID = 2_147_483_647
MAX_REVISION_ID_DIGITS = len(str(MAX_REVISION_ID))
MAX_EDIT_BATCH_SIZE = 100
MAX_NEW_TEXT_CHARS_PER_EDIT = 20_000
MAX_NEW_TEXT_CHARS_PER_BATCH = 200_000
PREFLIGHT_PROOF_SCHEMA_VERSION = "preflight_proof.v1"
_ANCHOR_KEYS = frozenset({"change_unit_id", "file_sha256"})
_DELETE_EDIT_KEYS = frozenset({"anchor", "delete_text", "insert_text"})
_REINSTATE_EDIT_KEYS = frozenset({"anchor", "reinstate_text"})
_PREFLIGHT_PROOF_CONTENT_KEYS = (
    "schema_version",
    "source_sha256",
    "edits_sha256",
    "tracked_change_author",
    "producer_build",
    "candidate_sha256",
)
_PREFLIGHT_PROOF_KEYS = frozenset(
    (*_PREFLIGHT_PROOF_CONTENT_KEYS, "proof_sha256")
)

_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
# Non-visible elements allowed to sit between covered runs during surgery.
_INERT_TAGS = frozenset({w("bookmarkStart"), w("bookmarkEnd"), w("proofErr")})


class ApplyError(DocxError):
    """A fail-closed refusal: the message starts with a stable error code."""

    def __init__(self, code: str, detail: str, **metadata: object) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.metadata = metadata


# ---------------------------------------------------------------------------
# Paragraph scanning
# ---------------------------------------------------------------------------


@dataclass
class _Seg:
    node: etree._Element
    text: str
    run: etree._Element | None
    start: int
    end: int
    plain: bool
    container: etree._Element | None  # enclosing pending w:ins, if any


def _paragraph_segments(para: etree._Element) -> list[_Seg]:
    """Char-mapped text-atom segments of the paragraph's current reading.

    The current reading accepts insertions and drops deleted/moved-away text
    (whoever deleted it). ``plain`` marks text no revision wrapper touches
    and whose run is a direct child of the paragraph; ``container`` records
    the enclosing pending insertion for counter edits.
    """
    segments: list[_Seg] = []
    offset = 0
    for node in para.iter():
        contribution = current_text_atom(node, boundary=para)
        if contribution is None:
            continue
        container: etree._Element | None = None
        run = next(node.iterancestors(w("r")), None)
        for ancestor in node.iterancestors():
            if ancestor is para:
                break
            if ancestor.tag == w("ins") or ancestor.tag in MOVE_REVISION_TAGS:
                container = ancestor
        plain = (
            container is None
            and run is not None
            and run.tag == w("r")
            and run.getparent() is para
        )
        segments.append(
            _Seg(
                node,
                contribution,
                run,
                offset,
                offset + len(contribution),
                plain,
                container,
            )
        )
        offset += len(contribution)
    return segments


def _reading_text(segments: list[_Seg]) -> str:
    return "".join(seg.text for seg in segments)


def _reading_offset_before(para: etree._Element, element: etree._Element) -> int:
    """Reading offset at which ``element`` sits inside the paragraph."""
    offset = 0
    for node in para.iter():
        if node is element:
            return offset
        contribution = current_text_atom(node, boundary=para)
        if contribution is not None:
            offset += len(contribution)
    return offset


def _element_reading_span(
    para: etree._Element, element: etree._Element
) -> tuple[int, int]:
    """Reading-offset span the element's visible text occupies."""
    start = _reading_offset_before(para, element)
    length = 0
    for node in element.iter():
        contribution = current_text_atom(node, boundary=para)
        if contribution is not None:
            length += len(contribution)
    return start, start + length


def _run_is_simple(run: etree._Element | None) -> bool:
    """True when the run holds only rPr and w:t children (safe to split/wrap)."""
    return run is not None and all(
        child.tag in (w("rPr"), w("t")) for child in run
    )


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
            text = seg.text
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


@dataclass
class _RevisionIdAllocator:
    """Reserve bounded revision ids atomically for one planned operation."""

    next_value: int

    @property
    def available(self) -> int:
        return max(0, MAX_REVISION_ID - self.next_value + 1)

    def reserve(self, count: int) -> tuple[str, ...]:
        """Return ``count`` consecutive ids or refuse without advancing."""
        if count < 1:
            raise ValueError("revision id reservations must be positive")
        if count > self.available:
            raise ApplyError(
                "revision_id_exhausted",
                "document has insufficient supported revision ids available "
                "for this edit",
                failure_phase="planning",
                required_revision_ids=count,
                available_revision_ids=self.available,
            )
        first = self.next_value
        self.next_value += count
        return tuple(str(value) for value in range(first, first + count))


def _revision_id_allocator(document: etree._Element) -> _RevisionIdAllocator:
    """Validate existing ids and return the sole allocator for new revisions."""
    highest = 100
    for el in document.iter():
        val = el.get(w("id"))
        if val is not None:
            if (
                not val.isascii()
                or not val.isdecimal()
                or len(val) > MAX_REVISION_ID_DIGITS
            ):
                raise ApplyError(
                    "revision_id_unsupported",
                    "document contains a revision id outside the supported "
                    "decimal range",
                    failure_phase="source",
                )
            numeric = int(val)
            if numeric > MAX_REVISION_ID:
                raise ApplyError(
                    "revision_id_unsupported",
                    "document contains a revision id outside the supported "
                    "decimal range",
                    failure_phase="source",
                )
            highest = max(highest, numeric)
    return _RevisionIdAllocator(highest + 1)


def _reserve_revision_ids(
    allocator: _RevisionIdAllocator,
    count: int,
    *,
    operation: str,
    container: etree._Element | None,
) -> tuple[str, ...]:
    """Reserve ids and attach already-proved match facts to exhaustion."""
    try:
        return allocator.reserve(count)
    except ApplyError as exc:
        exc.metadata.setdefault("operation", operation)
        exc.metadata.setdefault("match_count", 1)
        exc.metadata.setdefault(
            "target_author",
            (container.get(w("author")) or "") if container is not None else None,
        )
        exc.metadata.setdefault(
            "target_revision_ids",
            [container.get(w("id"))]
            if container is not None and container.get(w("id"))
            else [],
        )
        raise


def _hidden_del_text(deletion: etree._Element) -> str:
    return "".join(
        value
        for node in deletion.iter()
        if (value := text_atom(node, include_deleted_text=True)) is not None
    )


def _hidden_del_atom_overlaps(
    deletion: etree._Element,
    span: tuple[int, int],
) -> bool:
    """Whether a hidden-text span touches an atom surgery cannot preserve."""
    offset = 0
    for node in deletion.iter():
        value = text_atom(node, include_deleted_text=True)
        if value is None:
            continue
        start, end = offset, offset + len(value)
        offset = end
        if start < span[1] and end > span[0] and node.tag != w("delText"):
            return True
    return False


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
    edit_index: int
    claimed_source_sha256: str
    paragraph: etree._Element
    paragraph_index: int
    op: str  # _PLAN_OPERATION_PLAIN | counter | reinstate
    delete_text: str | None
    insert_text: str
    span: tuple[int, int]  # reading offsets; reinstate uses a zero-width point
    container: etree._Element | None  # counter: their w:ins; reinstate: their w:del
    del_id: str | None
    ins_id: str | None


@dataclass(frozen=True)
class _PreparedCandidate:
    """A fully validated DOCX candidate that has not been published."""

    source_path: str
    source_sha256: str
    candidate_payload: bytes
    candidate_sha256: str
    applied: list[dict]
    round_trip_check: dict
    edit_diagnostics: list[dict]


def _canonical_digest(value: object) -> str:
    """Hash one JSON value using the v1 canonical representation."""
    try:
        payload = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise ApplyError(
            "preflight_proof_invalid",
            "preflight binding values must be canonical JSON",
            failure_phase="preflight_binding",
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _build_preflight_proof(
    prepared: _PreparedCandidate,
    edits: list[dict],
    author: str,
    producer_build: str,
) -> dict[str, str]:
    if not isinstance(producer_build, str) or not producer_build:
        raise ApplyError(
            "preflight_proof_invalid",
            "producer_build must be a non-empty string",
            failure_phase="preflight_binding",
        )
    content = {
        "schema_version": PREFLIGHT_PROOF_SCHEMA_VERSION,
        "source_sha256": prepared.source_sha256,
        "edits_sha256": _canonical_digest(edits),
        "tracked_change_author": author,
        "producer_build": producer_build,
        "candidate_sha256": prepared.candidate_sha256,
    }
    return {**content, "proof_sha256": _canonical_digest(content)}


def _validated_preflight_proof(
    proof: object,
    prepared: _PreparedCandidate,
    edits: list[dict],
    author: str,
    producer_build: object,
) -> dict[str, str]:
    if not isinstance(proof, dict) or set(proof) != _PREFLIGHT_PROOF_KEYS:
        raise ApplyError(
            "preflight_proof_invalid",
            "preflight_proof must contain exactly the v1 proof fields",
            failure_phase="preflight_binding",
        )
    if any(not isinstance(proof[key], str) for key in _PREFLIGHT_PROOF_KEYS):
        raise ApplyError(
            "preflight_proof_invalid",
            "every preflight_proof field must be a string",
            failure_phase="preflight_binding",
        )
    if proof["schema_version"] != PREFLIGHT_PROOF_SCHEMA_VERSION:
        raise ApplyError(
            "preflight_proof_invalid",
            "unsupported preflight_proof schema_version",
            failure_phase="preflight_binding",
        )
    for key in (
        "source_sha256",
        "edits_sha256",
        "candidate_sha256",
        "proof_sha256",
    ):
        value = proof[key]
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ApplyError(
                "preflight_proof_invalid",
                f"preflight_proof.{key} must be a lowercase SHA-256 hex digest",
                failure_phase="preflight_binding",
            )
    proof_content = {
        key: proof[key] for key in _PREFLIGHT_PROOF_CONTENT_KEYS
    }
    if proof["proof_sha256"] != _canonical_digest(proof_content):
        raise ApplyError(
            "preflight_proof_invalid",
            "preflight_proof digest does not match its fields",
            failure_phase="preflight_binding",
        )
    expected = _build_preflight_proof(
        prepared,
        edits,
        author,
        producer_build,  # type: ignore[arg-type]
    )
    mismatched_fields = [
        key
        for key in _PREFLIGHT_PROOF_CONTENT_KEYS
        if proof[key] != expected[key]
    ]
    if mismatched_fields:
        raise ApplyError(
            "preflight_binding_mismatch",
            "preflight_proof does not match the exact apply inputs and candidate",
            failure_phase="preflight_binding",
            mismatched_fields=mismatched_fields,
            observed_source_sha256=prepared.source_sha256,
            observed_candidate_sha256=prepared.candidate_sha256,
        )
    return expected


def _plan_diagnostic(plan: _PlannedEdit, *, status: str) -> dict:
    return {
        "edit_index": plan.edit_index,
        "change_unit_id": plan.anchor_id,
        "status": status,
        "operation": plan.op if plan.op != _PLAN_OPERATION_PLAIN else (
            APPLY_OPERATION_REPLACE if plan.ins_id else APPLY_OPERATION_DELETE
        ),
        "match_count": 1,
        "target_author": (
            (plan.container.get(w("author")) or "")
            if plan.container is not None
            else None
        ),
        "target_revision_ids": (
            [plan.container.get(w("id"))]
            if plan.container is not None and plan.container.get(w("id"))
            else []
        ),
        "position_status": (
            PREFLIGHT_POSITION_STATUS_SUPPORTED
            if status == PREFLIGHT_EDIT_STATUS_APPLICABLE
            else PREFLIGHT_POSITION_STATUS_NOT_EVALUATED
        ),
        "refusal_code": None,
    }


_PREFLIGHT_DIAGNOSTIC_KEYS = (
    "edit_index",
    "change_unit_id",
    "status",
    "operation",
    "match_count",
    "target_author",
    "target_revision_ids",
    "position_status",
    "refusal_code",
)


def _empty_preflight_diagnostic(
    edit: object,
    edit_index: int,
    status: str,
) -> dict:
    change_unit_id = None
    if isinstance(edit, dict) and isinstance(edit.get("anchor"), dict):
        change_unit_id = edit["anchor"].get("change_unit_id")
    return {
        "edit_index": edit_index,
        "change_unit_id": change_unit_id,
        "status": status,
        "operation": None,
        "match_count": None,
        "target_author": None,
        "target_revision_ids": [],
        "position_status": PREFLIGHT_POSITION_STATUS_NOT_EVALUATED,
        "refusal_code": None,
    }


def _normalize_preflight_diagnostic(
    edit: object,
    edit_index: int,
    status: str,
    facts: object = None,
) -> dict:
    item = _empty_preflight_diagnostic(edit, edit_index, status)
    if isinstance(facts, dict):
        for key in _PREFLIGHT_DIAGNOSTIC_KEYS:
            if key in facts:
                item[key] = facts[key]
    item["edit_index"] = edit_index
    item["status"] = status
    if item["change_unit_id"] is None:
        item["change_unit_id"] = _empty_preflight_diagnostic(
            edit, edit_index, status
        )["change_unit_id"]
    return item


def _claimed_source_sha_from_edit(edit: object) -> str | None:
    if isinstance(edit, dict) and isinstance(edit.get("anchor"), dict):
        value = edit["anchor"].get("file_sha256")
        if isinstance(value, str):
            return value
    return None


def _edit_error_metadata(
    edit: object,
    edit_index: int,
    observed_source_sha256: str | None,
) -> dict[str, object]:
    return {
        "claimed_source_sha256": _claimed_source_sha_from_edit(edit),
        "observed_source_sha256": observed_source_sha256,
        "edit_index": edit_index,
    }


def _attach_edit_metadata(
    exc: ApplyError,
    edit: object,
    edit_index: int,
    observed_source_sha256: str,
) -> ApplyError:
    for key, value in _edit_error_metadata(
        edit, edit_index, observed_source_sha256
    ).items():
        exc.metadata.setdefault(key, value)
    return exc


def _plan_error_metadata(
    plan: _PlannedEdit,
    observed_source_sha256: str,
    planned: list[_PlannedEdit],
    *,
    failure_phase: str = "planning",
) -> dict[str, object]:
    return {
        "claimed_source_sha256": plan.claimed_source_sha256,
        "observed_source_sha256": observed_source_sha256,
        "edit_index": plan.edit_index,
        "preflight_edit": _plan_diagnostic(
            plan, status=PREFLIGHT_EDIT_STATUS_BLOCKED
        ),
        "failure_phase": failure_phase,
        "planned_edits": [
            _plan_diagnostic(item, status=PREFLIGHT_EDIT_STATUS_PLANNED)
            for item in sorted(planned, key=lambda item: item.edit_index)
        ],
    }


def _attach_plan_metadata(
    exc: ApplyError,
    plan: _PlannedEdit,
    observed_source_sha256: str,
    planned: list[_PlannedEdit],
    *,
    failure_phase: str,
) -> ApplyError:
    for key, value in _plan_error_metadata(
        plan,
        observed_source_sha256,
        planned,
        failure_phase=failure_phase,
    ).items():
        exc.metadata.setdefault(key, value)
    return exc


def _attach_observed_source_metadata(
    exc: DocxError,
    observed_source_sha256: str,
) -> DocxError:
    metadata = getattr(exc, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        exc.metadata = metadata
    metadata["observed_source_sha256"] = observed_source_sha256
    return exc


def _attach_pipeline_failure_metadata(
    exc: DocxError,
    planned: list[_PlannedEdit],
    *,
    failure_phase: str,
    candidate_payload: bytes | None = None,
    round_trip_check: dict | None = None,
) -> DocxError:
    """Preserve facts already proved before a later atomic refusal."""
    metadata = getattr(exc, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        exc.metadata = metadata
    metadata.setdefault("failure_phase", failure_phase)
    if planned:
        metadata.setdefault(
            "planned_edits",
            [
                _plan_diagnostic(plan, status=PREFLIGHT_EDIT_STATUS_PLANNED)
                for plan in sorted(planned, key=lambda item: item.edit_index)
            ],
        )
    if candidate_payload is not None:
        metadata.setdefault(
            "observed_candidate_sha256",
            hashlib.sha256(candidate_payload).hexdigest(),
        )
    if round_trip_check is not None:
        metadata.setdefault("round_trip_check", round_trip_check)
    return exc


def _relabel_candidate_snapshot_metadata(
    exc: DocxError,
    observed_source_sha256: str,
) -> DocxError:
    """Keep a failed candidate snapshot distinct from the apply source."""
    metadata = getattr(exc, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
        exc.metadata = metadata
    candidate_sha = metadata.pop("observed_source_sha256", None)
    if isinstance(candidate_sha, str):
        metadata.setdefault("observed_candidate_sha256", candidate_sha)
    metadata["observed_source_sha256"] = observed_source_sha256
    return exc


def _read_source_archive(
    payload: bytes,
    source: str,
) -> ValidatedDocx:
    """Read every source member or return one provenance-bearing refusal."""
    try:
        return load_validated_docx(payload, capture=None)
    except ArchiveValidationError as exc:
        raise ApplyError(exc.code, exc.detail, **exc.metadata) from exc
    except ZIP_READ_ERRORS as exc:
        raise ApplyError(
            "file_unextractable",
            f"cannot read every member of source archive {source}",
        ) from exc


def _output_archive_bytes(
    infos: list[zipfile.ZipInfo],
    parts: dict[str, bytes],
) -> bytes:
    """Serialize the candidate package entirely in memory."""
    buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for info in infos:
                fresh = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                fresh.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(fresh, parts[info.filename])
    except (
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        raise ApplyError(
            "output_unwritable",
            "cannot serialize candidate DOCX archive",
        ) from exc
    return buffer.getvalue()


def _cleanup_temp_artifact(tmp_path: str) -> None:
    """Best-effort cleanup that never replaces the operation's real error."""
    try:
        os.remove(tmp_path)
    except OSError:
        pass


def _publish_output_no_clobber(tmp_path: str, output: str) -> None:
    """Atomically publish a same-directory temp file only if output is absent."""
    try:
        os.link(tmp_path, output)
    except FileExistsError as exc:
        raise ApplyError(
            "output_exists",
            f"refusing to overwrite {output}",
            failure_phase="publication",
        ) from exc
    except OSError as exc:
        raise ApplyError(
            "output_unwritable",
            str(exc),
            failure_phase="publication",
        ) from exc
    _cleanup_temp_artifact(tmp_path)


def _validate_edit_shapes(
    edits: list,
    observed_source_sha256: str | None = None,
) -> None:
    """Reject malformed input with stable error codes before any work.

    MCP clients send loosely typed JSON; none of it may reach the OOXML layer
    as a raw TypeError/AttributeError — fail closed with an error code.
    """
    if len(edits) > MAX_EDIT_BATCH_SIZE:
        raise ApplyError(
            "resource_limit_exceeded",
            f"edit batch contains more than {MAX_EDIT_BATCH_SIZE} edits",
            limit="edit_count",
            allowed_count=MAX_EDIT_BATCH_SIZE,
            observed_count=len(edits),
            observed_source_sha256=observed_source_sha256,
            failure_phase="validation",
        )

    total_new_text_chars = 0
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ApplyError(
                "invalid_edit",
                f"edits[{index}] must be an object",
                **_edit_error_metadata(edit, index, observed_source_sha256),
            )
        anchor = edit.get("anchor")
        if not isinstance(anchor, dict):
            raise ApplyError(
                "anchor_missing",
                f"edits[{index}].anchor must be an object",
                **_edit_error_metadata(edit, index, observed_source_sha256),
            )
        unexpected_anchor_keys = set(anchor) - _ANCHOR_KEYS
        if unexpected_anchor_keys:
            raise ApplyError(
                "invalid_edit",
                f"edits[{index}].anchor contains unsupported fields: "
                f"{', '.join(sorted(map(str, unexpected_anchor_keys)))}",
                **_edit_error_metadata(edit, index, observed_source_sha256),
            )
        for key in ("change_unit_id", "file_sha256"):
            value = anchor.get(key)
            if not isinstance(value, str) or not value:
                raise ApplyError(
                    "anchor_missing",
                    f"edits[{index}].anchor.{key} must be a non-empty string",
                    **_edit_error_metadata(edit, index, observed_source_sha256),
                )
        has_delete_text = "delete_text" in edit
        has_reinstate_text = "reinstate_text" in edit
        if has_delete_text and has_reinstate_text:
            raise ApplyError(
                "invalid_edit",
                f"edits[{index}] must use either delete_text or reinstate_text, not both",
                **_edit_error_metadata(edit, index, observed_source_sha256),
            )
        if has_reinstate_text:
            unexpected_edit_keys = set(edit) - _REINSTATE_EDIT_KEYS
            if unexpected_edit_keys:
                raise ApplyError(
                    "invalid_edit",
                    f"edits[{index}] reinstate contains unsupported fields: "
                    f"{', '.join(sorted(map(str, unexpected_edit_keys)))}",
                    **_edit_error_metadata(edit, index, observed_source_sha256),
                )
            reinstate_text = edit["reinstate_text"]
            if not isinstance(reinstate_text, str) or not reinstate_text:
                raise ApplyError(
                    "invalid_edit",
                    f"edits[{index}].reinstate_text must be a non-empty string",
                    **_edit_error_metadata(edit, index, observed_source_sha256),
                )
            if len(reinstate_text) > MAX_NEW_TEXT_CHARS_PER_EDIT:
                raise ApplyError(
                    "resource_limit_exceeded",
                    "reinstate_text exceeds the per-edit new-text limit",
                    limit="new_text_chars_per_edit",
                    allowed_chars=MAX_NEW_TEXT_CHARS_PER_EDIT,
                    observed_chars=len(reinstate_text),
                    failure_phase="validation",
                    **_edit_error_metadata(edit, index, observed_source_sha256),
                )
            total_new_text_chars += len(reinstate_text)
            if not is_xml_text_compatible(reinstate_text):
                raise ApplyError(
                    "invalid_edit",
                    f"edits[{index}].reinstate_text contains characters "
                    "invalid in XML",
                    **_edit_error_metadata(edit, index, observed_source_sha256),
                )
        else:
            unexpected_edit_keys = set(edit) - _DELETE_EDIT_KEYS
            if unexpected_edit_keys:
                raise ApplyError(
                    "invalid_edit",
                    f"edits[{index}] contains unsupported fields: "
                    f"{', '.join(sorted(map(str, unexpected_edit_keys)))}",
                    **_edit_error_metadata(edit, index, observed_source_sha256),
                )
            delete_text = edit.get("delete_text")
            if not isinstance(delete_text, str) or not delete_text:
                raise ApplyError(
                    "delete_text_missing",
                    f"edits[{index}].delete_text must be a non-empty string",
                    **_edit_error_metadata(edit, index, observed_source_sha256),
                )
            if not is_xml_text_compatible(delete_text):
                raise ApplyError(
                    "invalid_edit",
                    f"edits[{index}].delete_text contains characters invalid "
                    "in XML",
                    **_edit_error_metadata(edit, index, observed_source_sha256),
                )
            insert_text = edit.get("insert_text", "")
            if not isinstance(insert_text, str):
                raise ApplyError(
                    "invalid_edit",
                    f"edits[{index}].insert_text must be a string",
                    **_edit_error_metadata(edit, index, observed_source_sha256),
                )
            if len(insert_text) > MAX_NEW_TEXT_CHARS_PER_EDIT:
                raise ApplyError(
                    "resource_limit_exceeded",
                    "insert_text exceeds the per-edit new-text limit",
                    limit="new_text_chars_per_edit",
                    allowed_chars=MAX_NEW_TEXT_CHARS_PER_EDIT,
                    observed_chars=len(insert_text),
                    failure_phase="validation",
                    **_edit_error_metadata(edit, index, observed_source_sha256),
                )
            total_new_text_chars += len(insert_text)
            if not is_xml_text_compatible(insert_text):
                raise ApplyError(
                    "invalid_edit",
                    f"edits[{index}].insert_text contains characters invalid "
                    "in XML",
                    **_edit_error_metadata(edit, index, observed_source_sha256),
                )
        if total_new_text_chars > MAX_NEW_TEXT_CHARS_PER_BATCH:
            raise ApplyError(
                "resource_limit_exceeded",
                "edit batch exceeds the total new-text limit",
                limit="new_text_chars_per_batch",
                allowed_chars=MAX_NEW_TEXT_CHARS_PER_BATCH,
                observed_chars=total_new_text_chars,
                failure_phase="validation",
                **_edit_error_metadata(edit, index, observed_source_sha256),
            )


def _resolve_anchor_paragraph(
    document: etree._Element, unit: dict
) -> tuple[etree._Element, int]:
    """Resolve one hash-bound unit by structural position and fingerprint.

    ``w:id`` is provenance, not an address: legitimate DOCX files can carry
    duplicate revision ids.  The source hash binds the anchor to exact bytes;
    paragraph/group ordinals locate the unit in those bytes; the canonical
    group facts prove that extraction and application agree on its identity.
    """
    reference = unit.get("reference")
    if (
        not isinstance(reference, dict)
        or reference.get("part_name") != DOCUMENT_PART
    ):
        raise ApplyError("anchor_mismatch", "anchor part is not supported")
    paragraph_index = reference.get("paragraph_index")
    group_index = reference.get("group_index")
    if not isinstance(paragraph_index, int) or not isinstance(group_index, int):
        raise ApplyError("anchor_mismatch", "anchor has no structural locator")

    paragraphs = list(document.iter(w("p")))
    if paragraph_index < 0 or paragraph_index >= len(paragraphs):
        raise ApplyError("anchor_mismatch", "anchor paragraph is not present")
    paragraph = paragraphs[paragraph_index]
    groups = _group_units(_paragraph_stream(paragraph))
    if group_index < 0 or group_index >= len(groups):
        raise ApplyError("anchor_mismatch", "anchor revision group is not present")
    fields = _group_change_fields(groups[group_index])
    expected = (
        unit.get("change_type"),
        unit.get("author"),
        unit.get("date"),
        unit.get("old_text"),
        unit.get("new_text"),
        tuple(reference.get("revision_ids", [])),
    )
    observed = (
        fields.get("change_type") if fields else None,
        fields.get("author") if fields else None,
        fields.get("date") if fields else None,
        fields.get("old_text") if fields else None,
        fields.get("new_text") if fields else None,
        tuple(fields.get("revision_ids", [])) if fields else (),
    )
    if observed != expected:
        raise ApplyError(
            "anchor_mismatch",
            "the structurally located revision group does not match the anchor",
        )
    return paragraph, paragraph_index


def _match_delete_span(
    para: etree._Element, delete_text: str, author: str
) -> tuple[str, tuple[int, int], etree._Element | None]:
    """Locate ``delete_text`` and classify the edit: plain or counter."""
    segments = _paragraph_segments(para)
    reading = _reading_text(segments)

    first_match = reading.find(delete_text)
    if first_match == -1:
        raise ApplyError(
            "delete_text_not_found",
            "delete_text does not occur in the anchored clause's current reading",
            match_count=0,
        )
    second_match = reading.find(delete_text, first_match + 1)
    if second_match != -1:
        raise ApplyError(
            "delete_text_ambiguous",
            "delete_text occurs more than once in the anchored clause",
            match_count=2,
        )
    span = (first_match, first_match + len(delete_text))

    touched = [s for s in segments if s.start < span[1] and s.end > span[0]]
    # Matching follows the canonical current reading even when the underlying
    # OOXML layout is not writable.  Surgery validates the narrower run shape
    # later, so an extracted quote never degrades into a false zero-match.
    if all(s.container is None for s in touched):
        return _PLAN_OPERATION_PLAIN, span, None

    containers = {s.container for s in touched}
    if len(containers) == 1:
        container = containers.pop()
        if (
            container is not None
            and container.tag == w("ins")
            and container.getparent() is para
        ):
            container_author = container.get(w("author")) or ""
            if container_author == author:
                raise ApplyError(
                    "overlaps_tracked_changes",
                    "the matched span lies in your own pending insertion; "
                    "counter edits target the counterparty's proposals",
                    match_count=1,
                    operation=APPLY_OPERATION_COUNTER,
                    target_author=container_author,
                    target_revision_ids=[container.get(w("id"))]
                    if container.get(w("id"))
                    else [],
                )
            return APPLY_OPERATION_COUNTER, span, container
    raise ApplyError(
        "overlaps_tracked_changes",
        "the matched span mixes plain text and tracked changes, or sits in "
        "markup the write path does not support",
        match_count=1,
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
            match_count=0,
            operation=APPLY_OPERATION_REINSTATE,
        )
    if total > 1 or len(hits) > 1:
        raise ApplyError(
            "reinstate_text_ambiguous",
            "reinstate_text occurs more than once among the clause's deletions",
            match_count=total,
            operation=APPLY_OPERATION_REINSTATE,
        )
    deletion = hits[0]
    hidden = _hidden_del_text(deletion)
    matched_at = hidden.find(reinstate_text)
    if _hidden_del_atom_overlaps(
        deletion,
        (matched_at, matched_at + len(reinstate_text)),
    ):
        raise ApplyError(
            "unsupported_run_shape",
            "reinstate_text touches an OOXML text atom the write path cannot "
            "preserve",
            match_count=1,
            operation=APPLY_OPERATION_REINSTATE,
            target_author=deletion.get(w("author")) or "",
            target_revision_ids=[deletion.get(w("id"))]
            if deletion.get(w("id"))
            else [],
        )
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
            match_count=1,
            operation=APPLY_OPERATION_REINSTATE,
            target_author=deletion.get(w("author")) or "",
            target_revision_ids=[deletion.get(w("id"))]
            if deletion.get(w("id"))
            else [],
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
    if not covered or "".join(s.text for s in covered) != plan.delete_text:
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
    if plan.op == _PLAN_OPERATION_PLAIN:
        deletion = _wrap_covered_runs(plan.paragraph, plan, author)
        if plan.ins_id is not None:
            deletion.addnext(_new_insertion(plan.ins_id, author, plan.insert_text))
    elif plan.op == APPLY_OPERATION_COUNTER:
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
        unit["reference"]["paragraph_index"],
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
    if plan.op == APPLY_OPERATION_REINSTATE:
        return (
            plan.paragraph_index,
            "insert",
            author,
            None,
            None,
            plan.insert_text,
            (plan.ins_id,),
        )
    revision_ids = [plan.del_id] + ([plan.ins_id] if plan.ins_id else [])
    if plan.op == APPLY_OPERATION_COUNTER:
        change_type = APPLY_OPERATION_COUNTER
    else:
        change_type = (
            APPLY_OPERATION_REPLACE if plan.ins_id else APPLY_OPERATION_DELETE
        )
    return (
        plan.paragraph_index,
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


def _prepare_candidate(
    source_path: str,
    edits: list[dict],
    author: str = DEFAULT_AUTHOR,
) -> _PreparedCandidate:
    """Run the complete edit pipeline without publishing an output file."""
    try:
        source = resolve_user_path(source_path)
    except UserPathError as exc:
        raise ApplyError(
            exc.code, exc.detail, failure_phase="validation"
        ) from exc
    # The MCP schema already types `edits` as an array, but this is a public
    # Python API too — a non-list must fail closed, not TypeError.
    if not isinstance(edits, list):
        raise ApplyError(
            "invalid_edit",
            "edits must be an array of edit objects",
            failure_phase="validation",
        )
    if not edits:
        raise ApplyError(
            "no_edits", "edits list is empty", failure_phase="validation"
        )
    if author_error := tracked_change_author_validation_error(author):
        raise ApplyError(
            "invalid_author",
            author_error,
            failure_phase="validation",
        )

    # One byte snapshot of the source: the sha the anchors are checked
    # against, the baseline units and the parts being rewritten must all
    # describe the same bytes, even if the file changes underneath us.
    try:
        source_payload = read_docx_payload(source)
    except ResourceLimitError as exc:
        metadata = dict(exc.metadata)
        metadata.setdefault("failure_phase", "source")
        raise ApplyError(exc.code, exc.detail, **metadata) from exc
    except OSError as exc:
        raise ApplyError(
            "file_unreadable",
            f"cannot read {source}: {exc}",
            failure_phase="source",
        ) from exc
    source_sha = hashlib.sha256(source_payload).hexdigest()
    try:
        _validate_edit_shapes(edits, observed_source_sha256=source_sha)
    except DocxError as exc:
        _attach_observed_source_metadata(exc, source_sha)
        metadata = getattr(exc, "metadata", None)
        if isinstance(metadata, dict):
            metadata.setdefault("failure_phase", "validation")
        raise
    try:
        package = _read_source_archive(source_payload, source)
        baseline = _extract_validated(package, source, source_sha)
        units_by_id = {u["change_unit_id"]: u for u in baseline["change_units"]}

        infos = list(package.infos)
        parts = dict(package.parts)
        # Untouched source bytes for the structural half of the round-trip proof.
        source_document_bytes = parts[DOCUMENT_PART]
        document = parse_xml(parts[DOCUMENT_PART])
        body = document.find(w("body"))
        if body is None:
            raise DocxError(f"no w:body in {source}")
    except DocxError as exc:
        _attach_observed_source_metadata(exc, source_sha)
        metadata = getattr(exc, "metadata", None)
        if isinstance(metadata, dict):
            metadata.setdefault("failure_phase", "source")
        raise

    try:
        revision_ids = _revision_id_allocator(document)
    except DocxError as exc:
        _attach_observed_source_metadata(exc, source_sha)
        metadata = getattr(exc, "metadata", None)
        if isinstance(metadata, dict):
            metadata.setdefault("failure_phase", "source")
        raise
    planned: list[_PlannedEdit] = []
    for edit_index, edit in enumerate(edits):
        unit: dict | None = None
        try:
            anchor = edit["anchor"]
            if anchor["file_sha256"] != source_sha:
                raise ApplyError(
                    "file_sha256_mismatch",
                    "anchor was produced from a different file than source_path",
                    claimed_source_sha256=anchor["file_sha256"],
                    observed_source_sha256=source_sha,
                    edit_index=edit_index,
                )
            unit = units_by_id.get(anchor["change_unit_id"])
            if unit is None:
                raise ApplyError(
                    "anchor_not_found",
                    f"{anchor['change_unit_id']} is not a change unit of the source file",
                    claimed_source_sha256=anchor["file_sha256"],
                    observed_source_sha256=source_sha,
                    edit_index=edit_index,
                )
            paragraph, paragraph_index = _resolve_anchor_paragraph(document, unit)

            reinstate_text = edit.get("reinstate_text")
            if reinstate_text is not None:
                span, container = _match_reinstate(paragraph, reinstate_text, author)
                (ins_id,) = _reserve_revision_ids(
                    revision_ids,
                    1,
                    operation=APPLY_OPERATION_REINSTATE,
                    container=container,
                )
                planned.append(
                    _PlannedEdit(
                        anchor["change_unit_id"], edit_index, anchor["file_sha256"],
                        paragraph, paragraph_index,
                        APPLY_OPERATION_REINSTATE,
                        None,
                        reinstate_text,
                        span,
                        container, None, ins_id,
                    )
                )
                continue

            delete_text = edit["delete_text"]
            insert_text = edit.get("insert_text") or ""
            op, span, container = _match_delete_span(paragraph, delete_text, author)
            operation = op if op != _PLAN_OPERATION_PLAIN else (
                APPLY_OPERATION_REPLACE if insert_text else APPLY_OPERATION_DELETE
            )
            reserved = _reserve_revision_ids(
                revision_ids,
                2 if insert_text else 1,
                operation=operation,
                container=container,
            )
            del_id = reserved[0]
            ins_id = reserved[1] if insert_text else None
            planned.append(
                _PlannedEdit(
                    anchor["change_unit_id"], edit_index, anchor["file_sha256"],
                    paragraph, paragraph_index, op, delete_text, insert_text,
                    span, container,
                    del_id, ins_id,
                )
            )
        except ApplyError as exc:
            exc = _attach_edit_metadata(exc, edit, edit_index, source_sha)
            metadata = exc.metadata
            operation = metadata.get("operation")
            if operation is None and isinstance(edit, dict) and "reinstate_text" in edit:
                operation = APPLY_OPERATION_REINSTATE
            preflight_edit = {
                "edit_index": edit_index,
                "change_unit_id": (
                    edit.get("anchor", {}).get("change_unit_id")
                    if isinstance(edit, dict)
                    and isinstance(edit.get("anchor"), dict)
                    else None
                ),
                "status": "blocked",
                "operation": operation,
                "match_count": metadata.get("match_count", 0),
                "target_author": metadata.get("target_author"),
                "target_revision_ids": metadata.get("target_revision_ids", []),
                "position_status": PREFLIGHT_POSITION_STATUS_UNSUPPORTED,
                "refusal_code": None,
            }
            metadata.setdefault("preflight_edit", preflight_edit)
            raise _attach_pipeline_failure_metadata(
                exc, planned, failure_phase="matching"
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
            if plan.op == APPLY_OPERATION_REINSTATE:
                if id(plan.container) in targeted_deletions:
                    raise ApplyError(
                        "edits_overlap",
                        "two edits reinstate text from the same deletion",
                        **_plan_error_metadata(plan, source_sha, planned),
                    )
                targeted_deletions.add(id(plan.container))

        # Counter layout constraints. The strike nests inside the countered
        # insertion, the replacement goes right after it, and extraction
        # groups adjacent same-author markup — so combinations that would
        # leave two of our operations touching cannot be laid out
        # unambiguously and are refused rather than written confusingly.
        countered_hosts: set[int] = set()
        for plan in plans:
            if plan.op != APPLY_OPERATION_COUNTER:
                continue
            if id(plan.container) in countered_hosts:
                raise ApplyError(
                    "edits_overlap",
                    "one pending insertion can be countered only once; "
                    "consolidate into a single counter that covers the whole "
                    "replacement",
                    **_plan_error_metadata(plan, source_sha, planned),
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
                    **_plan_error_metadata(plan, source_sha, planned),
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
                    **_plan_error_metadata(plan, source_sha, planned),
                )
            if following_is_wrapper and (following.get(w("author")) or "") == author:
                # Even a pure strike surfaces at the host's right edge in
                # extraction and would merge with our markup right after it.
                raise ApplyError(
                    "adjacent_to_own_revision",
                    "the countered insertion is directly followed by our own "
                    "earlier tracked change; the strike would merge with it",
                    **_plan_error_metadata(plan, source_sha, planned),
                )
            host_end = _element_reading_span(plan.paragraph, plan.container)[1]
            for other in plans:
                if (
                    other is not plan
                    and other.op == _PLAN_OPERATION_PLAIN
                    and other.span[0] == host_end
                ):
                    raise ApplyError(
                        "edits_overlap",
                        "an edit starts immediately after a countered "
                        "insertion; without untouched text between them the "
                        "operations cannot stay distinct — apply it in a "
                        "separate call",
                        **_plan_error_metadata(other, source_sha, planned),
                    )

        ordered = sorted(plans, key=lambda p: (p.span[0], p.span[1]))
        for left, right in zip(ordered, ordered[1:]):
            if left.span[1] > right.span[0] or left.span[0] == right.span[0]:
                raise ApplyError(
                    "edits_overlap",
                    "two edits target overlapping text in the same paragraph",
                    **_plan_error_metadata(right, source_sha, planned),
                )
        for plan in sorted(plans, key=lambda p: p.span[0], reverse=True):
            try:
                _apply_plan(plan, author)
            except ApplyError as exc:
                raise _attach_plan_metadata(
                    exc,
                    plan,
                    source_sha,
                    planned,
                    failure_phase="surgery",
                ) from exc

    # ------------------------------------------------------------------
    # Serialize in memory and prove the same candidate apply_edits publishes.
    # ------------------------------------------------------------------
    parts[DOCUMENT_PART] = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        + etree.tostring(document)
    )
    candidate_payload: bytes | None = None
    try:
        candidate_payload = _output_archive_bytes(infos, parts)
        validate_docx_payload_size(
            candidate_payload,
            limit="candidate_docx_bytes",
        )
        try:
            result = _extract_from_bytes(candidate_payload, source)
        except DocxError as exc:
            raise _relabel_candidate_snapshot_metadata(
                exc,
                source_sha,
            ) from exc

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
                collateral_changes=collateral,
            )
    except DocxError as exc:
        _attach_observed_source_metadata(exc, source_sha)
        metadata = getattr(exc, "metadata", {})
        collateral_changes = (
            metadata.get("collateral_changes", [])
            if isinstance(metadata, dict)
            else []
        )
        failure_phase = (
            "serialization" if candidate_payload is None else "round_trip"
        )
        round_trip_check = (
            None
            if failure_phase == "serialization"
            else {
                "status": ROUND_TRIP_STATUS_FAILED,
                "collateral_changes": collateral_changes,
                "comparison": ROUND_TRIP_COMPARISON_CURRENT,
            }
        )
        raise _attach_pipeline_failure_metadata(
            exc,
            planned,
            failure_phase=failure_phase,
            candidate_payload=candidate_payload,
            round_trip_check=round_trip_check,
        )

    applied = [
        {
            "change_unit_id": plan.anchor_id,
            "operation": plan.op if plan.op != _PLAN_OPERATION_PLAIN else (
                APPLY_OPERATION_REPLACE if plan.ins_id else APPLY_OPERATION_DELETE
            ),
            "deleted_text": plan.delete_text,
            "inserted_text": plan.insert_text or None,
            "tracked_revision_ids": (
                ([plan.del_id] if plan.del_id else [])
                + ([plan.ins_id] if plan.ins_id else [])
            ),
        }
        for plan in planned
    ]
    diagnostics = [
        _plan_diagnostic(plan, status="applicable")
        for plan in sorted(planned, key=lambda item: item.edit_index)
    ]
    round_trip_check = {
        "status": ROUND_TRIP_STATUS_PASSED,
        "collateral_changes": [],
        "comparison": ROUND_TRIP_COMPARISON_CURRENT,
    }
    return _PreparedCandidate(
        source_path=source,
        source_sha256=source_sha,
        candidate_payload=candidate_payload,
        candidate_sha256=hashlib.sha256(candidate_payload).hexdigest(),
        applied=applied,
        round_trip_check=round_trip_check,
        edit_diagnostics=diagnostics,
    )


def _preflight_failure_result(
    source_path: str,
    edits: object,
    author: str,
    exc: DocxError,
) -> dict:
    metadata = getattr(exc, "metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    failure_phase = metadata.get("failure_phase")
    if failure_phase is None:
        failure_phase = (
            "validation"
            if getattr(exc, "code", None)
            in {
                "anchor_missing",
                "delete_text_missing",
                "invalid_edit",
                "invalid_author",
                "invalid_path",
                "no_edits",
            }
            else "source"
        )
    blocking_index = metadata.get("edit_index")
    blocking_diagnostic = metadata.get("preflight_edit")
    planned_diagnostics = {
        item.get("edit_index"): dict(item)
        for item in metadata.get("planned_edits", [])
        if isinstance(item, dict) and isinstance(item.get("edit_index"), int)
    }
    diagnostics: list[dict] = []
    if isinstance(edits, list):
        for index, edit in enumerate(edits):
            if index == blocking_index and isinstance(blocking_diagnostic, dict):
                item = _normalize_preflight_diagnostic(
                    edit,
                    index,
                    PREFLIGHT_EDIT_STATUS_BLOCKED,
                    blocking_diagnostic,
                )
                item["position_status"] = PREFLIGHT_POSITION_STATUS_UNSUPPORTED
                item["refusal_code"] = getattr(exc, "code", "docx_error")
                diagnostics.append(item)
                continue
            if index in planned_diagnostics:
                diagnostics.append(
                    _normalize_preflight_diagnostic(
                        edit,
                        index,
                        PREFLIGHT_EDIT_STATUS_PLANNED,
                        planned_diagnostics[index],
                    )
                )
                continue
            item = _empty_preflight_diagnostic(
                edit,
                index,
                PREFLIGHT_EDIT_STATUS_BLOCKED
                if index == blocking_index
                else PREFLIGHT_EDIT_STATUS_NOT_EVALUATED,
            )
            if index == blocking_index:
                item["refusal_code"] = getattr(exc, "code", "docx_error")
            diagnostics.append(item)
    try:
        safe_source_path = resolve_user_path(source_path)
    except UserPathError:
        safe_source_path = None
    return {
        "status": RESULT_STATUS_OK,
        "source_path": safe_source_path,
        "source_sha256": metadata.get("observed_source_sha256"),
        "tracked_change_author": (
            None if getattr(exc, "code", None) == "invalid_author" else author
        ),
        "batch_applicable": False,
        "candidate_sha256": None,
        "observed_candidate_sha256": metadata.get(
            "observed_candidate_sha256"
        ),
        "blocking_edit_index": blocking_index,
        "refusal_code": getattr(exc, "code", "docx_error"),
        "failure_phase": failure_phase,
        "reason": str(exc),
        "edits": diagnostics,
        "round_trip_check": metadata.get("round_trip_check"),
        "preflight_proof": None,
    }


def preflight_edits(
    source_path: str,
    edits: list[dict],
    author: str = DEFAULT_AUTHOR,
    *,
    producer_build: str | None = None,
) -> dict:
    """Fully dry-run edits in memory without creating an output DOCX.

    A caller that supplies ``producer_build`` receives a v1 proof binding the
    exact source bytes, canonical edit payload, author, build and predicted
    candidate. The proof is a drift detector, not authentication or a digital
    signature.
    """
    try:
        prepared = _prepare_candidate(source_path, edits, author)
        proof = (
            _build_preflight_proof(prepared, edits, author, producer_build)
            if producer_build is not None
            else None
        )
    except DocxError as exc:
        return _preflight_failure_result(source_path, edits, author, exc)
    return {
        "status": RESULT_STATUS_OK,
        "source_path": prepared.source_path,
        "source_sha256": prepared.source_sha256,
        "tracked_change_author": author,
        "batch_applicable": True,
        "candidate_sha256": prepared.candidate_sha256,
        "observed_candidate_sha256": None,
        "blocking_edit_index": None,
        "refusal_code": None,
        "failure_phase": None,
        "reason": None,
        "edits": prepared.edit_diagnostics,
        "round_trip_check": prepared.round_trip_check,
        "preflight_proof": proof,
    }


def apply_edits(
    source_path: str,
    output_path: str,
    edits: list[dict],
    author: str = DEFAULT_AUTHOR,
    *,
    preflight_proof: dict | None = None,
    producer_build: str | None = None,
) -> dict:
    """Apply explicit tracked edits using the same pipeline as preflight.

    When ``preflight_proof`` is present, every bound field is verified after
    constructing the candidate and before any output artifact is published.
    Omitting it preserves the lower-level Python API's v0.1 behavior; the MCP
    v0.2 boundary requires the proof.
    """
    try:
        output = resolve_user_path(output_path)
    except UserPathError as exc:
        raise ApplyError(
            exc.code, exc.detail, failure_phase="validation"
        ) from exc
    if os.path.exists(output):
        raise ApplyError(
            "output_exists",
            f"refusing to overwrite {output}",
            failure_phase="publication",
        )
    prepared = _prepare_candidate(source_path, edits, author)
    validated_proof = None
    if preflight_proof is not None:
        validated_proof = _validated_preflight_proof(
            preflight_proof,
            prepared,
            edits,
            author,
            producer_build,
        )

    tmp_path: str | None = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(output) or ".",
            prefix=os.path.basename(output) + ".",
            suffix=".veqtor-tmp",
        )
        with os.fdopen(tmp_fd, "wb") as handle:
            handle.write(prepared.candidate_payload)
            handle.flush()
            os.fsync(handle.fileno())
        _publish_output_no_clobber(tmp_path, output)
    except DocxError as exc:
        _attach_observed_source_metadata(exc, prepared.source_sha256)
        if tmp_path is not None:
            _cleanup_temp_artifact(tmp_path)
        raise
    except OSError as exc:
        if tmp_path is not None:
            _cleanup_temp_artifact(tmp_path)
        raise ApplyError(
            "output_unwritable",
            str(exc),
            observed_source_sha256=prepared.source_sha256,
            failure_phase="publication",
        ) from exc
    except BaseException:
        if tmp_path is not None:
            _cleanup_temp_artifact(tmp_path)
        raise

    return {
        "status": RESULT_STATUS_OK,
        "source_sha256": prepared.source_sha256,
        "output_path": output,
        "output_sha256": prepared.candidate_sha256,
        "tracked_change_author": author,
        "applied": prepared.applied,
        "round_trip_check": prepared.round_trip_check,
        "preflight_binding_status": (
            "verified" if validated_proof is not None else "not_provided"
        ),
        "preflight_candidate_sha256": (
            validated_proof["candidate_sha256"]
            if validated_proof is not None
            else None
        ),
        "candidate_output_sha256_match": (
            prepared.candidate_sha256 == validated_proof["candidate_sha256"]
            if validated_proof is not None
            else None
        ),
    }
