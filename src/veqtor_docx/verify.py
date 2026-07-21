# SPDX-License-Identifier: Apache-2.0
"""Deterministic quote verification against hash-bound read anchors.

M3 first slice. Before a quotation is relied on in a memo, an email or a
negotiation summary, it must be checkable against the document — cheaply,
deterministically and without a model in the loop.

Change-unit anchors verify against ``new_text`` or ``old_text``.  The v0.3
paragraph-reference anchor verifies against one complete
``accepted_current_v1`` paragraph. Matching remains case-sensitive. Verdicts:

- ``exact`` — the quote occurs verbatim;
- ``normalized`` — the quote occurs after collapsing whitespace runs and
  normalizing typographic quotes/dashes/non-breaking spaces (the ways a
  faithful quote drifts when pasted between documents);
- ``not_found`` — neither; ``diff`` states what was checked.

Anything unresolvable — a hash mismatch, an unknown anchor, an empty quote —
raises :class:`VerifyError` with a stable code instead of guessing.
Whole-document search without an anchor is not supported.

Provenance is atomic: the file is read exactly once, and the verdict, the
hash comparison and ``checked_anchor.file_sha256`` all describe that single
byte snapshot — a concurrent file swap cannot split them.
"""

from __future__ import annotations

from pathlib import Path

from .contracts import (
    MATCH_SIDE_NEW,
    MATCH_SIDE_OLD,
    VERIFY_VERDICT_EXACT,
    VERIFY_VERDICT_NORMALIZED,
    VERIFY_VERDICT_NOT_FOUND,
)
from ._ooxml import UserPathError, resolve_user_path
from .extract import (
    CHANGE_UNIT_ANCHOR_SCHEMA_V2,
    DocxError,
    change_unit_fingerprint_sha256,
    extract_redlines,
)
from .inspect import inspect_document

_LEGACY_CHANGE_UNIT_ANCHOR_KEYS = frozenset({"change_unit_id", "file_sha256"})
_V2_CHANGE_UNIT_ANCHOR_KEYS = frozenset(
    {
        "schema_version",
        "change_unit_id",
        "file_sha256",
        "container_policy",
        "unit_fingerprint_sha256",
    }
)
_PARAGRAPH_REF_KEYS = frozenset(
    {
        "schema_version",
        "ref_type",
        "file_sha256",
        "part_name",
        "paragraph_index",
        "paragraph_text_sha256",
        "reading_mode",
        "container_policy",
    }
)
_PARAGRAPH_REF_SCHEMA = "paragraph_ref.v1"
_MATCH_SIDE_PARAGRAPH_CURRENT = "paragraph_current"

# The drift a faithful quotation picks up between Word, chat and e-mail:
# typographic quotes, dashes and non-breaking spaces.
_TYPOGRAPHIC = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "–": "-",
        "—": "-",
        "‑": "-",
        " ": " ",
    }
)


class VerifyError(DocxError):
    """A fail-closed refusal: the message starts with a stable error code."""

    def __init__(self, code: str, detail: str, **metadata: object) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.metadata = metadata


def _normalize(text: str) -> str:
    return " ".join(text.translate(_TYPOGRAPHIC).split())


def _clause_of(unit: dict) -> str | None:
    anchor = unit.get("clause_anchor")
    if not anchor:
        return None
    parts = [anchor.get("label"), anchor.get("heading")]
    joined = " ".join(part for part in parts if part)
    return joined or None


def _section_clause(navigation: object) -> str | None:
    if not isinstance(navigation, dict):
        return None
    parts = [navigation.get("label"), navigation.get("heading")]
    return " ".join(part for part in parts if isinstance(part, str)) or None


def _anchor_kind(anchor: dict) -> str:
    keys = frozenset(anchor)
    if keys < _LEGACY_CHANGE_UNIT_ANCHOR_KEYS:
        missing = sorted(_LEGACY_CHANGE_UNIT_ANCHOR_KEYS - keys)[0]
        raise VerifyError(
            "anchor_missing", f"anchor.{missing} must be a non-empty string"
        )
    if keys == _LEGACY_CHANGE_UNIT_ANCHOR_KEYS:
        return "legacy_change_unit"
    if anchor.get("schema_version") == CHANGE_UNIT_ANCHOR_SCHEMA_V2 and keys < (
        _V2_CHANGE_UNIT_ANCHOR_KEYS
    ):
        missing = sorted(_V2_CHANGE_UNIT_ANCHOR_KEYS - keys)[0]
        raise VerifyError(
            "anchor_missing", f"anchor.{missing} must be a non-empty string"
        )
    if keys == _V2_CHANGE_UNIT_ANCHOR_KEYS:
        if anchor.get("schema_version") != CHANGE_UNIT_ANCHOR_SCHEMA_V2:
            raise VerifyError("invalid_anchor", "anchor schema_version is unsupported")
        return "change_unit_v2"
    if keys == _PARAGRAPH_REF_KEYS:
        if (
            anchor.get("schema_version") != _PARAGRAPH_REF_SCHEMA
            or anchor.get("ref_type") != "paragraph"
        ):
            raise VerifyError(
                "invalid_anchor", "paragraph anchor discriminator is invalid"
            )
        return "paragraph"
    raise VerifyError("invalid_anchor", "anchor fields do not match a supported schema")


def _read_error(
    exc: DocxError,
    *,
    resolved: str,
    claimed_sha: object,
) -> VerifyError:
    code = getattr(exc, "code", None) or (
        "file_unextractable" if Path(resolved).is_file() else "file_unreadable"
    )
    metadata: dict[str, object] = {}
    source_metadata = getattr(exc, "metadata", None)
    if isinstance(source_metadata, dict):
        metadata = dict(source_metadata)
        if isinstance(claimed_sha, str):
            metadata["claimed_source_sha256"] = claimed_sha
    detail = getattr(exc, "detail", None)
    if not isinstance(detail, str):
        detail = str(exc)
    return VerifyError(code, detail, **metadata)


def _verdict(
    *,
    quote: str,
    checked_anchor: dict,
    sides: list[tuple[str, str | None]],
    match,
    not_found_detail: str,
) -> dict:
    for side, text in sides:
        if text and quote in text:
            return {
                "verdict": VERIFY_VERDICT_EXACT,
                "exact": True,
                "checked_anchor": checked_anchor,
                "matches": [match(side)],
                "diff": [],
            }

    normalized_quote = _normalize(quote)
    for side, text in sides:
        if text and normalized_quote and normalized_quote in _normalize(text):
            return {
                "verdict": VERIFY_VERDICT_NORMALIZED,
                "exact": False,
                "checked_anchor": checked_anchor,
                "matches": [match(side)],
                "diff": [
                    "quote matches after collapsing whitespace and normalizing "
                    "typographic quotes/dashes"
                ],
            }

    return {
        "verdict": VERIFY_VERDICT_NOT_FOUND,
        "exact": False,
        "checked_anchor": checked_anchor,
        "matches": [],
        "diff": [not_found_detail],
    }


def _verify_paragraph(
    resolved: str,
    anchor: dict,
    quote: str,
) -> dict:
    claimed_sha = anchor.get("file_sha256")
    try:
        inspection = inspect_document(
            resolved,
            "read",
            selection={"paragraph_ref": anchor},
            max_items=1,
        )
    except DocxError as exc:
        raise _read_error(
            exc,
            resolved=resolved,
            claimed_sha=claimed_sha,
        ) from exc
    paragraphs = inspection.get("paragraphs")
    if not isinstance(paragraphs, list) or len(paragraphs) != 1:
        raise VerifyError(
            "anchor_not_found",
            "paragraph anchor did not resolve to exactly one paragraph",
            claimed_source_sha256=claimed_sha,
            observed_source_sha256=inspection.get("file_sha256"),
        )
    paragraph = paragraphs[0]
    checked_anchor = paragraph["paragraph_ref"]
    text = paragraph["text"]

    def match(side: str) -> dict:
        return {
            "path": resolved,
            "part_name": checked_anchor["part_name"],
            "revision_ids": [],
            "clause": _section_clause(paragraph.get("section_navigation")),
            "side": side,
            "paragraph_index": checked_anchor["paragraph_index"],
            "paragraph_text_sha256": checked_anchor["paragraph_text_sha256"],
            "reading_mode": checked_anchor["reading_mode"],
        }

    return _verdict(
        quote=quote,
        checked_anchor=checked_anchor,
        sides=[(_MATCH_SIDE_PARAGRAPH_CURRENT, text)],
        match=match,
        not_found_detail=(
            "quote does not occur in the anchored paragraph's accepted/current text"
        ),
    )


def verify_quote(path: str, anchor: dict, quote: str) -> dict:
    """Check ``quote`` against one change-unit or paragraph anchor."""
    # A whitespace-only quote is a substring of almost anything; verifying it
    # as `exact` would be a false-positive surface for a trust tool.
    if not isinstance(quote, str) or not _normalize(quote):
        raise VerifyError("quote_missing", "quote must contain non-whitespace text")
    if not isinstance(anchor, dict):
        raise VerifyError("anchor_missing", "anchor must be an object")
    anchor_kind = _anchor_kind(anchor)
    try:
        resolved = resolve_user_path(path)
    except UserPathError as exc:
        raise VerifyError(exc.code, exc.detail) from exc
    claimed_sha = anchor.get("file_sha256")
    required_strings = [("file_sha256", claimed_sha)]
    if anchor_kind != "paragraph":
        required_strings.append(("change_unit_id", anchor.get("change_unit_id")))
    for key, value in required_strings:
        if not isinstance(value, str) or not value:
            raise VerifyError(
                "anchor_missing", f"anchor.{key} must be a non-empty string"
            )

    if anchor_kind == "paragraph":
        return _verify_paragraph(resolved, anchor, quote)

    unit_id = anchor["change_unit_id"]

    # One snapshot for everything: extract_redlines reads the file exactly
    # once and derives file_sha256 from the same bytes as the facts, so the
    # verdict and checked_anchor can never describe different content.
    try:
        extraction = extract_redlines(resolved)
    except DocxError as exc:
        raise _read_error(
            exc,
            resolved=resolved,
            claimed_sha=claimed_sha,
        ) from exc
    actual_sha = extraction["file_sha256"]
    if claimed_sha != actual_sha:
        raise VerifyError(
            "file_sha256_mismatch",
            "anchor was produced from a different file than path",
            claimed_source_sha256=claimed_sha,
            observed_source_sha256=actual_sha,
        )
    container_policy = extraction["revision_inventory"].get("container_policy", {})
    if anchor_kind == "legacy_change_unit" and not container_policy.get(
        "legacy_two_field_anchor_safe", False
    ):
        raise VerifyError(
            "legacy_anchor_ambiguous",
            "the two-field anchor predates canonical container filtering; "
            "re-extract and use the policy-bound v0.3 anchor",
            claimed_source_sha256=claimed_sha,
            observed_source_sha256=actual_sha,
        )
    unit = next(
        (u for u in extraction["change_units"] if u["change_unit_id"] == unit_id),
        None,
    )
    if unit is None:
        raise VerifyError(
            "anchor_not_found",
            f"{unit_id} is not a change unit of the file",
            claimed_source_sha256=claimed_sha,
            observed_source_sha256=actual_sha,
        )

    if anchor_kind == "change_unit_v2":
        observed_anchor = unit.get("anchor") or {}
        observed_fingerprint = change_unit_fingerprint_sha256(unit)
        if anchor.get("container_policy") != container_policy.get("schema_version"):
            raise VerifyError(
                "anchor_policy_mismatch",
                "anchor container policy does not match the source snapshot",
                claimed_source_sha256=claimed_sha,
                observed_source_sha256=actual_sha,
            )
        if (
            anchor.get("unit_fingerprint_sha256") != observed_fingerprint
            or observed_anchor.get("unit_fingerprint_sha256") != observed_fingerprint
        ):
            raise VerifyError(
                "anchor_fingerprint_mismatch",
                "anchor structural/unit fingerprint does not match the source",
                claimed_source_sha256=claimed_sha,
                observed_source_sha256=actual_sha,
            )

    checked_anchor = (
        dict(unit["anchor"])
        if anchor_kind == "change_unit_v2"
        else {"change_unit_id": unit_id, "file_sha256": actual_sha}
    )
    # Deterministic preference: the new reading first, then the prior one.
    sides = [
        (MATCH_SIDE_NEW, unit["new_text"]),
        (MATCH_SIDE_OLD, unit["old_text"]),
    ]

    def match(side: str) -> dict:
        return {
            "path": resolved,
            "part_name": unit["reference"]["part_name"],
            "revision_ids": unit["reference"]["revision_ids"],
            "clause": _clause_of(unit),
            "side": side,
        }

    return _verdict(
        quote=quote,
        checked_anchor=checked_anchor,
        sides=sides,
        match=match,
        not_found_detail=(
            "quote does not occur in the anchored change unit's old or new text"
        ),
    )
