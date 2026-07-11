# SPDX-License-Identifier: Apache-2.0
"""Veqtor MCP server: deterministic DOCX facts for MCP-compatible clients.

The tools read redlines, verify quotes, apply tracked counter-edits and record
local provenance. Legal interpretation stays with the calling model.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

import veqtor_docx
from veqtor_mcp import records

mcp = FastMCP("veqtor")


def _ok_result(result: dict[str, Any]) -> dict[str, Any]:
    return (
        result
        if "status" in result
        else {"status": records.RESULT_STATUS_OK, **result}
    )


def _error_result(exc: veqtor_docx.DocxError) -> dict[str, Any]:
    return {
        "status": records.RESULT_STATUS_ERROR,
        "error_code": getattr(exc, "code", "docx_error"),
        "error": str(exc),
    }


def _with_record(
    *,
    tool_name: str,
    workspace,
    input_payload: dict[str, Any],
    result: dict[str, Any],
    provenance: dict[str, Any],
    record_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = records.write_record(
        workspace=workspace,
        tool_name=tool_name,
        input_payload=input_payload,
        result=_ok_result(result) if record_result is None else record_result,
        tool_result=_ok_result(result),
        provenance=provenance,
    )
    return {**result, **meta}


def _record_error(
    *,
    tool_name: str,
    workspace,
    input_payload: dict[str, Any],
    exc: veqtor_docx.DocxError,
    provenance: dict[str, Any] | None = None,
) -> None:
    records.write_record(
        workspace=workspace,
        tool_name=tool_name,
        input_payload=input_payload,
        result=_error_result(exc),
        tool_result=_error_result(exc),
        provenance=provenance or {},
    )


def _anchor_from_verify(anchor: dict) -> dict[str, Any]:
    return {
        key: anchor[key]
        for key in ("change_unit_id", "file_sha256")
        if key in anchor
    }


def _anchors_from_edits(edits: list[dict]) -> list[dict[str, Any]]:
    anchors: list[dict[str, Any]] = []
    for edit in edits:
        if isinstance(edit, dict) and isinstance(edit.get("anchor"), dict):
            anchors.append(_anchor_from_verify(edit["anchor"]))
    return anchors


def _record_edits(edits: Any) -> Any:
    if not isinstance(edits, list):
        return edits
    recorded: list[Any] = []
    for edit in edits:
        if not isinstance(edit, dict):
            recorded.append(edit)
            continue
        item = dict(edit)
        if isinstance(item.get("anchor"), dict):
            item["anchor"] = _anchor_from_verify(item["anchor"])
        recorded.append(item)
    return recorded


def _claimed_source_sha_from_edits(edits: list[dict]) -> str | None:
    for edit in edits:
        if isinstance(edit, dict) and isinstance(edit.get("anchor"), dict):
            value = edit["anchor"].get("file_sha256")
            if isinstance(value, str):
                return value
    return None


def _list_rounds_provenance(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "folder": result["folder"],
        "rounds": [
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "revision_count": item["revision_count"],
            }
            for item in result["rounds"]
        ],
        "skipped": result["skipped"],
    }


def _extract_provenance(result: dict[str, Any]) -> dict[str, Any]:
    anchors = [
        {
            "change_unit_id": unit["change_unit_id"],
            "file_sha256": unit["file_sha256"],
            "revision_ids": unit["reference"]["revision_ids"],
            "clause_anchor": unit["clause_anchor"],
        }
        for unit in result["change_units"]
    ]
    return {
        "path": result["path"],
        "file_sha256": result["file_sha256"],
        "part_name": result["part_name"],
        "anchors": records.bounded_observed_anchors(anchors),
    }


def _extract_record_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": records.RESULT_STATUS_OK,
        "path": result["path"],
        "file_sha256": result["file_sha256"],
        "part_name": result["part_name"],
        "revision_count": result["revision_count"],
        "change_unit_count": len(result["change_units"]),
        "unsupported_revisions": result["unsupported_revisions"],
    }


def _extract_error_provenance(
    path: str,
    exc: veqtor_docx.DocxError,
) -> dict[str, Any]:
    provenance: dict[str, Any] = {"path": path}
    metadata = getattr(exc, "metadata", None)
    if isinstance(metadata, dict):
        observed_sha = metadata.get("observed_source_sha256")
        if observed_sha is not None:
            provenance["observed_source_sha256"] = observed_sha
    return provenance


def _verify_provenance(result: dict[str, Any], anchor: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_sha256": result["checked_anchor"]["file_sha256"],
        "checked_anchor": result["checked_anchor"],
        "input_anchor": _anchor_from_verify(anchor),
        "anchors": [
            {
                "change_unit_id": result["checked_anchor"]["change_unit_id"],
                "revision_ids": match["revision_ids"],
                "side": match["side"],
            }
            for match in result["matches"]
        ],
        "verdict": result["verdict"],
    }


def _verify_error_provenance(
    path: str,
    anchor: object,
    exc: veqtor_docx.DocxError,
) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "path": path,
        "input_anchor": _anchor_from_verify(anchor)
        if isinstance(anchor, dict)
        else {},
    }
    metadata = getattr(exc, "metadata", None)
    if isinstance(metadata, dict):
        for key in ("claimed_source_sha256", "observed_source_sha256"):
            if key in metadata:
                provenance[key] = metadata[key]
    return provenance


def _apply_provenance(
    result: dict[str, Any], source_path: str, edits: list[dict]
) -> dict[str, Any]:
    source_sha = _claimed_source_sha_from_edits(edits)
    return {
        "source_path": source_path,
        "source_sha256": source_sha,
        "output_path": result["output_path"],
        "output_sha256": result["output_sha256"],
        "anchors": _anchors_from_edits(edits),
        "applied": [
            {
                "change_unit_id": item["change_unit_id"],
                "operation": item["operation"],
                "tracked_revision_ids": item["tracked_revision_ids"],
            }
            for item in result["applied"]
        ],
        "round_trip_check": result["round_trip_check"],
    }


def _apply_error_provenance(
    source_path: str,
    output_path: str,
    edits: list[dict],
    exc: veqtor_docx.DocxError,
) -> dict[str, Any]:
    metadata = getattr(exc, "metadata", {})
    claimed = (
        metadata.get("claimed_source_sha256")
        if isinstance(metadata, dict) and "claimed_source_sha256" in metadata
        else _claimed_source_sha_from_edits(edits)
    )
    provenance: dict[str, Any] = {
        "source_path": source_path,
        "output_path": output_path,
        "anchors": _anchors_from_edits(edits) if isinstance(edits, list) else [],
    }
    if claimed is not None:
        provenance["claimed_source_sha256"] = claimed
    if isinstance(metadata, dict):
        if metadata.get("observed_source_sha256") is not None:
            provenance["observed_source_sha256"] = metadata[
                "observed_source_sha256"
            ]
        if metadata.get("observed_candidate_sha256") is not None:
            provenance["observed_candidate_sha256"] = metadata[
                "observed_candidate_sha256"
            ]
        if metadata.get("edit_index") is not None:
            provenance["edit_index"] = metadata["edit_index"]
    return provenance


def _decision_record_assurance() -> dict[str, Any]:
    return {
        "journal_model": "best_effort_local_provenance",
        "model_payload": "compact_only",
        "tamper_evident": False,
        "hash_chain": False,
        "record_id_guarantee": "strictly_increasing_only",
        "producer_identity": "python_source_files_snapshot_only",
        "content_hashes": "recheckable_fingerprints_not_authentication",
        "round_trip_scope": (
            "ooxml_semantic_diff_outside_touched_anchors_not_docx_byte_identity"
        ),
    }


@mcp.tool()
def list_rounds(folder: str) -> dict:
    """List the DOCX negotiation rounds in a local folder.

    Call this when the user points to a folder of contract drafts or asks
    which rounds/files are available in a negotiation. Rounds are sorted by
    filename; each entry carries the file's sha256 and the raw count of
    tracked revisions inside. Unreadable files are reported in ``skipped``.
    """
    workspace = records.workspace_for_folder(folder)
    result = veqtor_docx.list_rounds(folder)
    return _with_record(
        tool_name="list_rounds",
        workspace=workspace,
        input_payload={"folder": folder},
        result=result,
        provenance=_list_rounds_provenance(result),
    )


@mcp.tool()
def extract_redlines(path: str) -> dict:
    """Extract tracked changes from one DOCX as verifiable change units.

    Call this when the user asks what changed in a DOCX, asks for tracked
    changes, or needs clause anchors. Each change unit states the change type
    (insert/delete/replace), author, date, old/new text, a best-effort clause
    anchor, and a reference (path, OOXML part, revision ids, file sha256)
    that lets any quote be re-checked against the document. Revision kinds
    the tool does not decode (formatting, moves) are counted in
    ``unsupported_revisions`` rather than silently dropped.
    """
    workspace = records.workspace_for_file(path)
    input_payload = {"path": path}
    try:
        result = veqtor_docx.extract_redlines(path)
    except veqtor_docx.DocxError as exc:
        _record_error(
            tool_name="extract_redlines",
            workspace=workspace,
            input_payload=input_payload,
            exc=exc,
            provenance=_extract_error_provenance(path, exc),
        )
        raise
    return _with_record(
        tool_name="extract_redlines",
        workspace=workspace,
        input_payload=input_payload,
        result=result,
        provenance=_extract_provenance(result),
        record_result=_extract_record_result(result),
    )


@mcp.tool()
def apply_edits(source_path: str, output_path: str, edits: list[dict]) -> dict:
    """Create a new DOCX with the given edits applied as real tracked changes.

    Call this only after the user asks to prepare or apply counter wording,
    and only with anchors produced by ``extract_redlines``. Each edit needs
    ``anchor`` ({change_unit_id, file_sha256}) plus either ``delete_text``
    with optional ``insert_text``, or ``reinstate_text``. ``delete_text``
    must occur exactly once in the anchored clause: in untouched text it
    becomes a plain tracked replace/delete; entirely inside one counterparty
    pending insertion it becomes a visible counter (their proposal stays,
    struck through, with our replacement after it). ``reinstate_text``
    visibly restores text from a counterparty deletion. Several edits may
    target one paragraph if their spans do not overlap. Edits are atomic and
    fail closed: a hash mismatch, missing or ambiguous match, or unsupported
    overlap returns an error and writes nothing. After writing, the server
    re-extracts the output and checks the documented round trip: exactly the
    prior change units plus the proposed edits, with an OOXML semantic diff
    outside the touched anchors. This is not a byte-identity check of the DOCX
    package. The source file is never modified.
    """
    workspace = records.workspace_for_file(source_path)
    input_payload = {
        "source_path": source_path,
        "output_path": output_path,
        "edits": _record_edits(edits),
    }
    try:
        result = veqtor_docx.apply_edits(source_path, output_path, edits)
    except veqtor_docx.DocxError as exc:
        error_provenance = (
            _apply_error_provenance(source_path, output_path, edits, exc)
            if isinstance(edits, list)
            else {
                "source_path": source_path,
                "output_path": output_path,
                "anchors": [],
            }
        )
        _record_error(
            tool_name="apply_edits",
            workspace=workspace,
            input_payload=input_payload,
            exc=exc,
            provenance=error_provenance,
        )
        raise
    return _with_record(
        tool_name="apply_edits",
        workspace=workspace,
        input_payload=input_payload,
        result=result,
        provenance=_apply_provenance(result, source_path, edits),
    )


@mcp.tool()
def verify_quote(path: str, anchor: dict, quote: str) -> dict:
    """Check a quotation against the document before relying on it.

    Call this before using a quote in a memo, email, or negotiation summary.
    ``anchor`` is {change_unit_id, file_sha256} from ``extract_redlines``.
    The verdict is ``exact`` (verbatim in the anchored change unit's old or
    new text), ``normalized`` (matches after collapsing whitespace and
    typographic quotes/dashes — ``diff`` says so), or ``not_found``.
    Matching is case-sensitive and deterministic; a hash mismatch or unknown
    anchor is an error, never a guess. The verdict covers only the anchored
    change unit, not the whole document or the legal accuracy of the quote.
    """
    workspace = records.workspace_for_file(path)
    input_payload = {
        "path": path,
        "anchor": _anchor_from_verify(anchor) if isinstance(anchor, dict) else anchor,
        "quote": quote,
    }
    try:
        result = veqtor_docx.verify_quote(path, anchor, quote)
    except veqtor_docx.DocxError as exc:
        _record_error(
            tool_name="verify_quote",
            workspace=workspace,
            input_payload=input_payload,
            exc=exc,
            provenance=_verify_error_provenance(path, anchor, exc),
        )
        raise
    return _with_record(
        tool_name="verify_quote",
        workspace=workspace,
        input_payload=input_payload,
        result=result,
        provenance=_verify_provenance(result, anchor),
    )


@mcp.tool()
def export_decision_record(
    workspace: str,
    max_records: int | None = None,
    before_record_id: str | None = None,
) -> dict:
    """Return compact local provenance entries for a matter workspace.

    Call this when the user asks what toolchain actions were performed or
    what re-checkable evidence relates them to document bytes. This is a
    best-effort local provenance history, not a tamper-evident audit log:
    content hashes are fingerprints, not authentication or a hash chain, and
    strictly increasing record ids do not prove that records were not deleted
    or rewritten. ``producer.build`` identifies imported Python source files,
    not the complete binary environment. MCP returns only a compact projection;
    the raw local journal may contain private matter text and is never returned
    by this tool.
    """
    root = records.workspace_for_folder(workspace)
    input_payload = {
        "workspace": workspace,
        "max_records": max_records,
        "before_record_id": before_record_id,
    }
    result = records.read_records(
        workspace,
        max_records,
        before_record_id,
        include_payload=False,
    )
    assurance = _decision_record_assurance()
    export_summary = {
        "status": records.RESULT_STATUS_OK,
        "workspace": result["workspace"],
        "total_count": result["total_count"],
        "access_count": result["access_count"],
        "returned_count": len(result["records"]),
        "truncated": result["truncated"],
        "next_before_record_id": result["next_before_record_id"],
        "payloads": result["payloads"],
        "assurance": assurance,
    }
    meta = records.write_record(
        workspace=root,
        tool_name="export_decision_record",
        input_payload=input_payload,
        result=export_summary,
        provenance={"workspace": result["workspace"]},
    )
    return {**result, "assurance": assurance, **meta}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
