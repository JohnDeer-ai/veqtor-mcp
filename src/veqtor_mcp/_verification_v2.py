# SPDX-License-Identifier: Apache-2.0
"""Verification v2 implementation over one caller-owned DOCX snapshot.

The public v0.4 ``verify_quote`` tool delegates here.  The builder closes the
operation result and ensures that paragraph anchors and selected projections
are reconstructed from the same immutable byte string.
"""

from __future__ import annotations

from typing import Any, Callable, NoReturn

from jsonschema import Draft202012Validator, ValidationError

from veqtor_docx._ooxml import UserPathError, read_docx_payload, resolve_user_path
from veqtor_docx._projection import build_paragraph_projection_v1
from veqtor_docx.contracts import (
    INSPECT_READING_MODE_V1,
    MATCH_SIDE_NEW,
    MATCH_SIDE_OLD,
    VERIFY_VERDICT_EXACT,
    VERIFY_VERDICT_NORMALIZED,
    VERIFY_VERDICT_NOT_FOUND,
)
from veqtor_docx.extract import DocxError, _extract_from_bytes
from veqtor_docx.inspect import (
    MAX_PARAGRAPH_TEXT_CHARS,
    InspectError,
    _load_snapshot_from_payload,
    _navigation,
    _paragraph_ref,
    _resolve_paragraph,
)
from veqtor_docx.verify import (
    VerifyError,
    _anchor_kind,
    _clause_of,
    _normalize,
    _resolve_change_unit,
    _section_clause,
)

from .contracts import VERIFICATION_RESULT_V2_OPERATION_SCHEMA


VERIFICATION_RESULT_SCHEMA_VERSION = "verification_result.v2"
VERIFIED_PARAGRAPH_PROJECTION_SCHEMA_VERSION = "verified_paragraph_projection.v1"
ACCEPTED_CURRENT_MODE = INSPECT_READING_MODE_V1
PENDING_REJECTED_MODE = "pending_text_revisions_rejected_v1"
PARAGRAPH_PROJECTION_MODES = frozenset(
    {ACCEPTED_CURRENT_MODE, PENDING_REJECTED_MODE}
)
PARAGRAPH_CURRENT_SIDE = "paragraph_current"
PARAGRAPH_REJECTED_SIDE = "paragraph_rejected_pending"

_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "verdict",
        "exact",
        "checked_anchor",
        "checked_projection",
        "matches",
        "diff",
    }
)
_CHECKED_PROJECTION_KEYS = frozenset(
    {
        "schema_version",
        "mode",
        "projection_status",
        "anchor_reading_mode",
        "anchor_paragraph_text_sha256",
        "projection_text_sha256",
        "text_length",
    }
)
_CHANGE_UNIT_MATCH_KEYS = frozenset(
    {"path", "part_name", "revision_ids", "clause", "side"}
)
_PARAGRAPH_MATCH_KEYS = frozenset(
    {
        "path",
        "part_name",
        "revision_ids",
        "clause",
        "side",
        "paragraph_index",
        "paragraph_text_sha256",
        "reading_mode",
        "projection_mode",
        "projection_text_sha256",
    }
)
_HEX = frozenset("0123456789abcdef")
_VERIFICATION_RESULT_V2_VALIDATOR = Draft202012Validator(
    VERIFICATION_RESULT_V2_OPERATION_SCHEMA
)


def _output_error(detail: str) -> VerifyError:
    return VerifyError("output_contract_error", detail)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def _raise_docx_error(
    exc: DocxError,
    *,
    claimed_sha: object,
) -> NoReturn:
    if isinstance(exc, InspectError):
        metadata = dict(exc.metadata)
        if isinstance(claimed_sha, str):
            metadata.setdefault("claimed_source_sha256", claimed_sha)
        raise VerifyError(exc.code, exc.detail, **metadata) from exc
    code = getattr(exc, "code", None) or "file_unextractable"
    detail = getattr(exc, "detail", None)
    if not isinstance(detail, str):
        detail = str(exc)
    metadata = getattr(exc, "metadata", None)
    controlled_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    if isinstance(claimed_sha, str):
        controlled_metadata.setdefault("claimed_source_sha256", claimed_sha)
    raise VerifyError(code, detail, **controlled_metadata) from exc


def _validated_input(
    payload: object,
    path: object,
    anchor: object,
    quote: object,
    paragraph_projection: object,
) -> tuple[str, str | None]:
    if not isinstance(payload, bytes):
        raise VerifyError("invalid_request", "captured DOCX payload must be bytes")
    if not isinstance(path, str) or not path:
        raise VerifyError("invalid_request", "captured DOCX path label is invalid")
    if not isinstance(quote, str) or not _normalize(quote):
        raise VerifyError("quote_missing", "quote must contain non-whitespace text")
    if not isinstance(anchor, dict):
        raise VerifyError("anchor_missing", "anchor must be an object")
    anchor_kind = _anchor_kind(anchor)
    claimed_sha = anchor.get("file_sha256")
    required_strings = [("file_sha256", claimed_sha)]
    if anchor_kind != "paragraph":
        required_strings.append(("change_unit_id", anchor.get("change_unit_id")))
    for key, value in required_strings:
        if not isinstance(value, str) or not value:
            raise VerifyError(
                "anchor_missing", f"anchor.{key} must be a non-empty string"
            )
    if paragraph_projection is not None and (
        not isinstance(paragraph_projection, str)
        or paragraph_projection not in PARAGRAPH_PROJECTION_MODES
    ):
        raise VerifyError(
            "invalid_projection_selector",
            "paragraph_projection is unsupported",
        )
    if anchor_kind != "paragraph":
        if paragraph_projection is not None:
            raise VerifyError(
                "invalid_projection_selector",
                "paragraph_projection requires a paragraph_ref.v1 anchor",
            )
        return anchor_kind, None
    return anchor_kind, paragraph_projection or ACCEPTED_CURRENT_MODE


def _verdict(
    *,
    quote: str,
    checked_anchor: dict[str, Any],
    checked_projection: dict[str, Any] | None,
    sides: list[tuple[str, str | None]],
    match: Callable[[str], dict[str, Any]],
    not_found_detail: str,
) -> dict[str, Any]:
    for side, text in sides:
        if text and quote in text:
            return {
                "schema_version": VERIFICATION_RESULT_SCHEMA_VERSION,
                "verdict": VERIFY_VERDICT_EXACT,
                "exact": True,
                "checked_anchor": checked_anchor,
                "checked_projection": checked_projection,
                "matches": [match(side)],
                "diff": [],
            }

    normalized_quote = _normalize(quote)
    for side, text in sides:
        if text and normalized_quote and normalized_quote in _normalize(text):
            return {
                "schema_version": VERIFICATION_RESULT_SCHEMA_VERSION,
                "verdict": VERIFY_VERDICT_NORMALIZED,
                "exact": False,
                "checked_anchor": checked_anchor,
                "checked_projection": checked_projection,
                "matches": [match(side)],
                "diff": [
                    "quote matches after collapsing whitespace and normalizing "
                    "typographic quotes/dashes"
                ],
            }

    return {
        "schema_version": VERIFICATION_RESULT_SCHEMA_VERSION,
        "verdict": VERIFY_VERDICT_NOT_FOUND,
        "exact": False,
        "checked_anchor": checked_anchor,
        "checked_projection": checked_projection,
        "matches": [],
        "diff": [not_found_detail],
    }


def _change_unit_result(
    payload: bytes,
    path: str,
    anchor: dict[str, Any],
    quote: str,
    anchor_kind: str,
) -> dict[str, Any]:
    claimed_sha = anchor.get("file_sha256")
    try:
        extraction = _extract_from_bytes(payload, path)
    except DocxError as exc:
        _raise_docx_error(exc, claimed_sha=claimed_sha)
    unit, checked_anchor = _resolve_change_unit(anchor_kind, anchor, extraction)
    sides = [
        (MATCH_SIDE_NEW, unit["new_text"]),
        (MATCH_SIDE_OLD, unit["old_text"]),
    ]

    def match(side: str) -> dict[str, Any]:
        return {
            "path": path,
            "part_name": unit["reference"]["part_name"],
            "revision_ids": unit["reference"]["revision_ids"],
            "clause": _clause_of(unit),
            "side": side,
        }

    return _verdict(
        quote=quote,
        checked_anchor=checked_anchor,
        checked_projection=None,
        sides=sides,
        match=match,
        not_found_detail=(
            "quote does not occur in the anchored change unit's old or new text"
        ),
    )


def _checked_projection(
    checked_anchor: dict[str, Any],
    *,
    mode: str,
    text_sha256: str,
    text_length: int,
) -> dict[str, Any]:
    return {
        "schema_version": VERIFIED_PARAGRAPH_PROJECTION_SCHEMA_VERSION,
        "mode": mode,
        "projection_status": "complete",
        "anchor_reading_mode": checked_anchor["reading_mode"],
        "anchor_paragraph_text_sha256": checked_anchor["paragraph_text_sha256"],
        "projection_text_sha256": text_sha256,
        "text_length": text_length,
    }


def _paragraph_result(
    payload: bytes,
    path: str,
    anchor: dict[str, Any],
    quote: str,
    projection_mode: str,
) -> dict[str, Any]:
    claimed_sha = anchor.get("file_sha256")
    try:
        snapshot = _load_snapshot_from_payload(payload, path=path)
        paragraph = _resolve_paragraph(snapshot, anchor)
        checked_anchor = _paragraph_ref(snapshot, paragraph)
        if projection_mode == ACCEPTED_CURRENT_MODE:
            projection_text = paragraph.text
            if len(projection_text) > MAX_PARAGRAPH_TEXT_CHARS:
                raise InspectError(
                    "resource_limit_exceeded",
                    "one paragraph exceeds the supported read limit",
                    limit="paragraph_text_chars",
                    allowed_chars=MAX_PARAGRAPH_TEXT_CHARS,
                    observed_chars=len(projection_text),
                )
            projection_sha256 = paragraph.text_sha256
            side = PARAGRAPH_CURRENT_SIDE
        else:
            projection = build_paragraph_projection_v1(
                paragraph.element, snapshot.body_flow
            )
            if projection["projection_status"] != "complete":
                raise VerifyError(
                    "paragraph_projection_unavailable",
                    "the rejected-pending paragraph projection is unavailable",
                    claimed_source_sha256=claimed_sha,
                    observed_source_sha256=snapshot.file_sha256,
                    unavailable_reasons=list(projection["unavailable_reasons"]),
                )
            projection_text = projection["text"]
            projection_sha256 = projection["projection_text_sha256"]
            side = PARAGRAPH_REJECTED_SIDE
    except VerifyError:
        raise
    except DocxError as exc:
        _raise_docx_error(exc, claimed_sha=claimed_sha)

    if not isinstance(projection_text, str) or not _is_sha256(projection_sha256):
        raise _output_error("selected paragraph projection is malformed")
    checked_projection = _checked_projection(
        checked_anchor,
        mode=projection_mode,
        text_sha256=projection_sha256,
        text_length=len(projection_text),
    )
    clause = _section_clause(
        _navigation(snapshot.section_by_paragraph.get(paragraph.paragraph_index))
    )

    def match(_side: str) -> dict[str, Any]:
        return {
            "path": path,
            "part_name": checked_anchor["part_name"],
            "revision_ids": [],
            "clause": clause,
            "side": side,
            "paragraph_index": checked_anchor["paragraph_index"],
            "paragraph_text_sha256": checked_anchor["paragraph_text_sha256"],
            "reading_mode": checked_anchor["reading_mode"],
            "projection_mode": projection_mode,
            "projection_text_sha256": projection_sha256,
        }

    detail = (
        "quote does not occur in the anchored paragraph's accepted/current text"
        if projection_mode == ACCEPTED_CURRENT_MODE
        else "quote does not occur in the anchored paragraph's rejected-pending text"
    )
    return _verdict(
        quote=quote,
        checked_anchor=checked_anchor,
        checked_projection=checked_projection,
        sides=[(side, projection_text)],
        match=match,
        not_found_detail=detail,
    )


def validate_verification_result_v2(result: object) -> None:
    """Refuse any result outside the frozen closed operation contract."""
    try:
        _VERIFICATION_RESULT_V2_VALIDATOR.validate(result)
    except ValidationError as exc:
        raise _output_error("verification v2 result violates its operation schema") from exc
    if not isinstance(result, dict) or set(result) != _RESULT_KEYS:
        raise _output_error("verification v2 result fields are not closed")
    if result.get("schema_version") != VERIFICATION_RESULT_SCHEMA_VERSION:
        raise _output_error("verification v2 schema version is invalid")
    verdict = result.get("verdict")
    exact = result.get("exact")
    matches = result.get("matches")
    diff = result.get("diff")
    if (
        verdict
        not in {
            VERIFY_VERDICT_EXACT,
            VERIFY_VERDICT_NORMALIZED,
            VERIFY_VERDICT_NOT_FOUND,
        }
        or not isinstance(exact, bool)
        or not isinstance(matches, list)
        or len(matches) > 1
        or not isinstance(diff, list)
        or not all(isinstance(item, str) for item in diff)
    ):
        raise _output_error("verification v2 verdict payload is invalid")
    if verdict == VERIFY_VERDICT_EXACT:
        if exact is not True or len(matches) != 1 or diff:
            raise _output_error("exact verification v2 result is inconsistent")
    elif verdict == VERIFY_VERDICT_NORMALIZED:
        if exact is not False or len(matches) != 1 or not diff:
            raise _output_error("normalized verification v2 result is inconsistent")
    elif exact is not False or matches or not diff:
        raise _output_error("not-found verification v2 result is inconsistent")

    checked_anchor = result.get("checked_anchor")
    if not isinstance(checked_anchor, dict):
        raise _output_error("verification v2 checked anchor is invalid")
    try:
        anchor_kind = _anchor_kind(checked_anchor)
    except VerifyError as exc:
        raise _output_error("verification v2 checked anchor is invalid") from exc
    checked_projection = result.get("checked_projection")
    if anchor_kind != "paragraph":
        if checked_projection is not None:
            raise _output_error("change-unit result has a paragraph projection")
        if matches:
            match = matches[0]
            if not isinstance(match, dict) or set(match) != _CHANGE_UNIT_MATCH_KEYS:
                raise _output_error("change-unit match fields are not closed")
            if match.get("side") not in {MATCH_SIDE_NEW, MATCH_SIDE_OLD}:
                raise _output_error("change-unit match side is invalid")
        return

    if (
        not isinstance(checked_projection, dict)
        or set(checked_projection) != _CHECKED_PROJECTION_KEYS
        or checked_projection.get("schema_version")
        != VERIFIED_PARAGRAPH_PROJECTION_SCHEMA_VERSION
        or checked_projection.get("mode") not in PARAGRAPH_PROJECTION_MODES
        or checked_projection.get("projection_status") != "complete"
        or checked_projection.get("anchor_reading_mode") != ACCEPTED_CURRENT_MODE
        or checked_projection.get("anchor_paragraph_text_sha256")
        != checked_anchor.get("paragraph_text_sha256")
        or not _is_sha256(checked_projection.get("projection_text_sha256"))
        or isinstance(checked_projection.get("text_length"), bool)
        or not isinstance(checked_projection.get("text_length"), int)
        or checked_projection["text_length"] < 0
    ):
        raise _output_error("checked paragraph projection is invalid")
    if (
        checked_projection["mode"] == ACCEPTED_CURRENT_MODE
        and checked_projection["projection_text_sha256"]
        != checked_anchor.get("paragraph_text_sha256")
    ):
        raise _output_error("current projection does not match its anchor")
    if not matches:
        return
    match = matches[0]
    expected_side = (
        PARAGRAPH_CURRENT_SIDE
        if checked_projection["mode"] == ACCEPTED_CURRENT_MODE
        else PARAGRAPH_REJECTED_SIDE
    )
    if (
        not isinstance(match, dict)
        or set(match) != _PARAGRAPH_MATCH_KEYS
        or match.get("side") != expected_side
        or match.get("part_name") != checked_anchor.get("part_name")
        or match.get("revision_ids") != []
        or match.get("paragraph_index") != checked_anchor.get("paragraph_index")
        or match.get("paragraph_text_sha256")
        != checked_anchor.get("paragraph_text_sha256")
        or match.get("reading_mode") != ACCEPTED_CURRENT_MODE
        or match.get("projection_mode") != checked_projection.get("mode")
        or match.get("projection_text_sha256")
        != checked_projection.get("projection_text_sha256")
    ):
        raise _output_error("paragraph match is inconsistent")


def build_verification_result_v2(
    payload: bytes,
    *,
    path: str,
    anchor: dict[str, Any],
    quote: str,
    paragraph_projection: str | None = None,
) -> dict[str, Any]:
    """Build the v2 operation result from one immutable byte snapshot."""
    anchor_kind, projection_mode = _validated_input(
        payload, path, anchor, quote, paragraph_projection
    )
    if anchor_kind == "paragraph":
        assert projection_mode is not None
        result = _paragraph_result(
            payload,
            path,
            anchor,
            quote,
            projection_mode,
        )
    else:
        result = _change_unit_result(payload, path, anchor, quote, anchor_kind)
    validate_verification_result_v2(result)
    return result


def verify_quote_v2(
    path: str,
    anchor: dict[str, Any],
    quote: str,
    paragraph_projection: str | None = None,
) -> dict[str, Any]:
    """Capture one bounded path snapshot and build the public v2 operation."""
    # Reject malformed input and invalid selector/anchor combinations before
    # touching the filesystem. The byte builder repeats the same closed check
    # over the captured authority before decoding it.
    _validated_input(b"", path, anchor, quote, paragraph_projection)
    try:
        resolved = resolve_user_path(path)
    except UserPathError as exc:
        raise VerifyError(exc.code, exc.detail) from exc
    claimed_sha = anchor.get("file_sha256")
    try:
        payload = read_docx_payload(resolved)
    except DocxError as exc:
        _raise_docx_error(exc, claimed_sha=claimed_sha)
    except OSError as exc:
        metadata = (
            {"claimed_source_sha256": claimed_sha}
            if isinstance(claimed_sha, str)
            else {}
        )
        raise VerifyError(
            "file_unreadable",
            "DOCX file cannot be read",
            **metadata,
        ) from exc
    return build_verification_result_v2(
        payload,
        path=resolved,
        anchor=anchor,
        quote=quote,
        paragraph_projection=paragraph_projection,
    )


__all__ = [
    "ACCEPTED_CURRENT_MODE",
    "PARAGRAPH_PROJECTION_MODES",
    "PENDING_REJECTED_MODE",
    "VERIFICATION_RESULT_SCHEMA_VERSION",
    "VERIFICATION_RESULT_V2_OPERATION_SCHEMA",
    "VERIFIED_PARAGRAPH_PROJECTION_SCHEMA_VERSION",
    "build_verification_result_v2",
    "validate_verification_result_v2",
    "verify_quote_v2",
]
