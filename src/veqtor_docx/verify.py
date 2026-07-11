# SPDX-License-Identifier: Apache-2.0
"""Deterministic quote verification against read-path anchors.

M3 first slice. Before a quotation is relied on in a memo, an email or a
negotiation summary, it must be checkable against the document — cheaply,
deterministically and without a model in the loop.

v1 verifies against the anchored change unit: the quote must occur in the
unit's ``new_text`` or ``old_text``. Matching is case-sensitive. Verdicts:

- ``exact`` — the quote occurs verbatim;
- ``normalized`` — the quote occurs after collapsing whitespace runs and
  normalizing typographic quotes/dashes/non-breaking spaces (the ways a
  faithful quote drifts when pasted between documents);
- ``not_found`` — neither; ``diff`` states what was checked.

Anything unresolvable — a hash mismatch, an unknown anchor, an empty quote —
raises :class:`VerifyError` with a stable code instead of guessing.
Whole-document search without an anchor is a later slice.

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
from .extract import DocxError, extract_redlines

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


def verify_quote(path: str, anchor: dict, quote: str) -> dict:
    """Check ``quote`` against the change unit named by ``anchor``."""
    # A whitespace-only quote is a substring of almost anything; verifying it
    # as `exact` would be a false-positive surface for a trust tool.
    if not isinstance(quote, str) or not _normalize(quote):
        raise VerifyError(
            "quote_missing", "quote must contain non-whitespace text"
        )
    if not isinstance(anchor, dict):
        raise VerifyError("anchor_missing", "anchor must be an object")
    unit_id = anchor.get("change_unit_id")
    claimed_sha = anchor.get("file_sha256")
    for key, value in (("change_unit_id", unit_id), ("file_sha256", claimed_sha)):
        if not isinstance(value, str) or not value:
            raise VerifyError(
                "anchor_missing", f"anchor.{key} must be a non-empty string"
            )

    resolved = str(Path(path).expanduser())
    # One snapshot for everything: extract_redlines reads the file exactly
    # once and derives file_sha256 from the same bytes as the facts, so the
    # verdict and checked_anchor can never describe different content.
    try:
        extraction = extract_redlines(resolved)
    except DocxError as exc:
        code = (
            "file_unextractable" if Path(resolved).is_file() else "file_unreadable"
        )
        metadata: dict[str, object] = {}
        source_metadata = getattr(exc, "metadata", None)
        if isinstance(source_metadata, dict):
            observed_sha = source_metadata.get("observed_source_sha256")
            if isinstance(observed_sha, str):
                metadata = {
                    "claimed_source_sha256": claimed_sha,
                    "observed_source_sha256": observed_sha,
                }
        raise VerifyError(code, str(exc), **metadata) from exc
    actual_sha = extraction["file_sha256"]
    if claimed_sha != actual_sha:
        raise VerifyError(
            "file_sha256_mismatch",
            "anchor was produced from a different file than path",
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

    checked_anchor = {"change_unit_id": unit_id, "file_sha256": actual_sha}
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
        "diff": [
            "quote does not occur in the anchored change unit's old or new text"
        ],
    }
