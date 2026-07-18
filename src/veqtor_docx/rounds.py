# SPDX-License-Identifier: Apache-2.0
"""Enumerate DOCX negotiation rounds in a folder."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

from ._ooxml import (
    DOCUMENT_PART,
    DocxError,
    ExpandedOutputBudget,
    ExpandedOutputBudgetExceeded,
    ResourceLimitError,
    TEXT_REVISION_TAGS,
    UserPathError,
    ZIP_READ_ERRORS,
    load_validated_docx,
    parse_xml,
    read_docx_payload,
    resolve_user_path,
)
from .contracts import REVISION_COUNT_BASIS_V1

MAX_ROUND_CANDIDATES = 500
MAX_ROUND_TOTAL_INPUT_BYTES = 500 * 1024 * 1024
MAX_ROUND_TOTAL_EXPANDED_BYTES = 500 * 1024 * 1024
_ROUND_EXPANDED_LIMIT = "round_total_expanded_bytes"


class RoundError(DocxError):
    """A controlled list-rounds refusal or per-file skip reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def _round_filename_key(path: Path) -> tuple[str, str]:
    """Return a case-insensitive order with an exact-name tie-break."""
    return path.name.casefold(), path.name


def _order_candidates_by_explicit_sequence(
    candidates: list[Path],
    ordered_filenames: Sequence[str],
) -> list[Path]:
    """Apply a complete caller-supplied filename sequence, or fail closed."""
    if isinstance(ordered_filenames, (str, bytes)) or not isinstance(
        ordered_filenames, Sequence
    ):
        raise RoundError(
            "invalid_round_order",
            "ordered_filenames must be a sequence of filenames",
        )
    filenames = list(ordered_filenames)
    if not all(isinstance(filename, str) for filename in filenames):
        raise RoundError(
            "invalid_round_order",
            "ordered_filenames must contain only filenames",
        )
    if len(filenames) != len(set(filenames)):
        raise RoundError(
            "invalid_round_order",
            "ordered_filenames contains duplicate filenames",
        )

    candidates_by_name = {candidate.name: candidate for candidate in candidates}
    if len(filenames) != len(candidates) or set(filenames) != set(candidates_by_name):
        raise RoundError(
            "invalid_round_order",
            "ordered_filenames must name every candidate DOCX exactly once",
        )
    return [candidates_by_name[filename] for filename in filenames]


def _round_facts(
    path: Path,
    *,
    expanded_budget: ExpandedOutputBudget,
) -> tuple[str, int, int]:
    """sha256, revision count, and size from one byte snapshot of the file."""
    try:
        payload = read_docx_payload(path)
    except ResourceLimitError as exc:
        raise RoundError(exc.code, exc.detail) from exc
    except OSError as exc:
        raise RoundError("file_unreadable", "cannot read DOCX bytes") from exc
    try:
        package = load_validated_docx(
            payload,
            capture=(DOCUMENT_PART,),
            expanded_budget=expanded_budget,
        )
    except ExpandedOutputBudgetExceeded:
        raise
    except ResourceLimitError as exc:
        raise RoundError(exc.code, exc.detail) from exc
    except DocxError as exc:
        code = getattr(exc, "code", "invalid_docx")
        if code not in {"unsupported_compression", "encrypted_docx"}:
            code = "invalid_docx"
        detail = getattr(exc, "detail", "invalid DOCX package")
        raise RoundError(code, detail) from exc
    except ZIP_READ_ERRORS as exc:
        raise RoundError("invalid_docx", "invalid DOCX package") from exc
    if DOCUMENT_PART not in package.member_names:
        raise RoundError(
            "missing_document_part", "DOCX has no main document part"
        )
    document_payload = package.parts[DOCUMENT_PART]
    try:
        document = parse_xml(document_payload)
    except ResourceLimitError as exc:
        raise RoundError(exc.code, exc.detail) from exc
    except DocxError as exc:
        raise RoundError("malformed_xml", "main document XML is malformed") from exc
    count = sum(1 for el in document.iter() if el.tag in TEXT_REVISION_TAGS)
    return hashlib.sha256(payload).hexdigest(), count, len(payload)


def list_rounds(
    folder: str,
    *,
    ordered_filenames: Sequence[str] | None = None,
) -> dict:
    """List DOCX rounds in ``folder`` using a disclosed positional order.

    Filename order remains the deterministic v1 default. A complete explicit
    filename sequence can override that positional order in Python callers;
    it is not treated as verified chronology or lineage. Word lock files
    (``~$*``) are ignored and the scan is non-recursive. Files that cannot be
    read as DOCX end up in ``skipped`` instead of failing the whole call.
    """
    try:
        root = Path(resolve_user_path(folder))
    except UserPathError as exc:
        raise RoundError(exc.code, exc.detail) from exc
    if not root.is_dir():
        raise RoundError("not_a_folder", "folder is not a directory")

    try:
        candidates: list[Path] = []
        candidate_input_bytes = 0
        for path in root.iterdir():
            if (
                not path.is_file()
                or path.suffix.lower() != ".docx"
                or path.name.startswith("~$")
            ):
                continue
            if len(candidates) >= MAX_ROUND_CANDIDATES:
                raise RoundError(
                    "resource_limit_exceeded",
                    "folder contains more than "
                    f"{MAX_ROUND_CANDIDATES} candidate DOCX files",
                )
            candidates.append(path)
            candidate_input_bytes += path.stat().st_size
            if candidate_input_bytes > MAX_ROUND_TOTAL_INPUT_BYTES:
                raise RoundError(
                    "resource_limit_exceeded",
                    "candidate DOCX files exceed the "
                    f"{MAX_ROUND_TOTAL_INPUT_BYTES // (1024 * 1024)} MiB "
                    "aggregate input limit",
                )
        if ordered_filenames is None:
            candidates.sort(key=_round_filename_key)
            ordering_source = "filename_lexicographic_v1"
            order_basis: dict[str, object] = {
                "kind": "filename",
                "rule": "casefold_then_exact",
                "lineage_verified": False,
                "round_id_semantics": "position_only",
            }
        else:
            candidates = _order_candidates_by_explicit_sequence(
                candidates,
                ordered_filenames,
            )
            ordering_source = "explicit_filename_sequence_v1"
            order_basis = {
                "kind": "caller_supplied_filename_sequence",
                "lineage_verified": False,
                "round_id_semantics": "position_only",
            }
    except RoundError:
        raise
    except OSError as exc:
        raise RoundError("folder_unreadable", "cannot enumerate folder") from exc

    rounds: list[dict] = []
    skipped: list[dict] = []
    total_input_bytes = 0
    expanded_budget = ExpandedOutputBudget(
        allowed_bytes=MAX_ROUND_TOTAL_EXPANDED_BYTES,
        limit=_ROUND_EXPANDED_LIMIT,
    )
    for path in candidates:
        try:
            digest, revision_count, input_bytes = _round_facts(
                path,
                expanded_budget=expanded_budget,
            )
        except ExpandedOutputBudgetExceeded as exc:
            raise RoundError(
                exc.code,
                "candidate DOCX files exceed the "
                f"{MAX_ROUND_TOTAL_EXPANDED_BYTES // (1024 * 1024)} MiB "
                "aggregate expanded-output limit; split the folder and retry",
            ) from exc
        except RoundError as exc:
            skipped.append({"filename": path.name, "reason": exc.code})
            continue
        total_input_bytes += input_bytes
        if total_input_bytes > MAX_ROUND_TOTAL_INPUT_BYTES:
            raise RoundError(
                "resource_limit_exceeded",
                "candidate DOCX files exceed the "
                f"{MAX_ROUND_TOTAL_INPUT_BYTES // (1024 * 1024)} MiB "
                "aggregate input limit",
            )
        rounds.append(
            {
                "round_id": f"round-{len(rounds) + 1:03d}",
                "path": str(path),
                "filename": path.name,
                "sha256": digest,
                "revision_count": revision_count,
            }
        )
    return {
        "folder": str(root),
        "ordering_source": ordering_source,
        "order_basis": order_basis,
        "revision_count_basis": REVISION_COUNT_BASIS_V1,
        "rounds": rounds,
        "skipped": skipped,
    }
