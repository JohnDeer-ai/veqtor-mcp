# SPDX-License-Identifier: Apache-2.0
"""Veqtor MCP server: deterministic DOCX facts for MCP-compatible clients.

The tools read redlines, verify quotes, apply tracked counter-edits and record
local provenance. Legal interpretation stays with the calling model.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from functools import cache
from typing import Annotated, Any, Callable, Literal

import jsonschema
from mcp.server.fastmcp import FastMCP
from pydantic import Field, StrictInt

import veqtor_docx
from veqtor_docx._ooxml import tracked_change_author_validation_error
from veqtor_docx.apply import DEFAULT_AUTHOR
from veqtor_docx.contracts import INSPECT_FIXED_LIMITS_V1, InspectionContractV1
from veqtor_docx.inspect import DEFAULT_MAX_ITEMS as DEFAULT_INSPECT_MAX_ITEMS
from veqtor_mcp import __version__
from veqtor_mcp import records
from veqtor_mcp import round_map
from veqtor_mcp._inspection_live import (
    CheckedInspectionError,
    CheckedInspectionResult,
    _checked_inspection_error_from_gate,
    _checked_inspection_result_from_gate,
)
from veqtor_mcp.contracts import (
    ApplyEditsResult,
    EditInput,
    ExportDecisionRecordResult,
    ExtractRedlinesResult,
    InspectDocumentResult,
    InspectSelectionInput,
    ListRoundsResult,
    RoundMapResult,
    RoundMapSeedInput,
    PreflightEditsResult,
    PreflightProofInput,
    VerifyAnchorInput,
    VerifyQuoteResult,
    contract_meta,
    is_record_error,
    is_record_id,
    local_journaling_annotations,
)

TRACKED_CHANGE_AUTHOR_ENV = "VEQTOR_TRACKED_CHANGE_AUTHOR"


def _tracked_change_author_from_environment() -> str:
    value = os.environ.get(TRACKED_CHANGE_AUTHOR_ENV, DEFAULT_AUTHOR)
    value = value.strip()
    if error := tracked_change_author_validation_error(value):
        raise RuntimeError(f"{TRACKED_CHANGE_AUTHOR_ENV}: {error}")
    return value


@cache
def _tracked_change_author() -> str:
    """Resolve immutable process configuration lazily after CLI dispatch."""
    return _tracked_change_author_from_environment()


mcp = FastMCP("veqtor")
# FastMCP does not expose the low-level server's product-version argument.
# Without setting it explicitly, MCP initialization reports the installed SDK
# version instead of the Veqtor release version.
mcp._mcp_server.version = __version__


_RESULT_MODELS = {
    "list_rounds": ListRoundsResult,
    "extract_redlines": ExtractRedlinesResult,
    "inspect_document": InspectDocumentResult,
    "map_rounds": RoundMapResult,
    "preflight_edits": PreflightEditsResult,
    "apply_edits": ApplyEditsResult,
    "verify_quote": VerifyQuoteResult,
    "export_decision_record": ExportDecisionRecordResult,
}


def _producer() -> dict[str, str]:
    return {
        "name": "veqtor-mcp",
        "version": __version__,
        "build": records.SOURCE_SNAPSHOT_IDENTITY,
    }


def _ok_result(result: dict[str, Any]) -> dict[str, Any]:
    return (
        result if "status" in result else {"status": records.RESULT_STATUS_OK, **result}
    )


def _error_result(exc: veqtor_docx.DocxError) -> dict[str, Any]:
    return {
        "status": records.RESULT_STATUS_ERROR,
        "error_code": getattr(exc, "code", "docx_error"),
        "error": str(exc),
    }


class _OutputContractError(veqtor_docx.DocxError):
    """A sanitized failure raised before an invalid success can be journaled."""

    code = "output_contract_error"

    def __init__(self) -> None:
        super().__init__(f"{self.code}: tool output failed contract validation")


_SERVER_OWNED_RESULT_FIELDS = frozenset(
    {"producer", "record_id", "record_status", "record_error"}
)
_INSPECTION_RESERVED_RESULT_FIELDS = frozenset({"status", "error_code", "error"})


def _validated_record_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Accept only the three exact metadata tuples emitted by the journal."""
    if not isinstance(meta, dict):
        raise _OutputContractError
    status = meta.get("record_status")
    record_id = meta.get("record_id")
    if status == "written":
        valid = is_record_id(record_id) and set(meta) == {
            "record_id",
            "record_status",
        }
    elif status == "disabled":
        valid = record_id is None and set(meta) == {"record_id", "record_status"}
    elif status == "write_failed":
        record_error = meta.get("record_error")
        valid = (
            record_id is None
            and is_record_error(record_error)
            and set(meta) == {"record_id", "record_status", "record_error"}
        )
    else:
        valid = False
    if not valid:
        raise _OutputContractError
    return dict(meta)


def _validated_success_result(
    tool_name: str,
    result: dict[str, Any],
    *,
    record_id: str | None = None,
    record_status: str = "disabled",
    record_error: str | None = None,
) -> dict[str, Any] | CheckedInspectionResult:
    """Validate and normalize one live result exactly as the MCP boundary does.

    The Pydantic result model is the same return model FastMCP uses.  Its
    advertised JSON Schema is then applied to the normalized model dump, which
    mirrors FastMCP's conversion followed by the low-level MCP output-schema
    gate.  Temporary disabled record metadata makes validation possible before
    a decision-record id exists.
    """
    model = _RESULT_MODELS.get(tool_name)
    if model is None or not isinstance(result, dict):
        raise _OutputContractError
    if set(result) & _SERVER_OWNED_RESULT_FIELDS:
        raise _OutputContractError
    is_inspection = tool_name == "inspect_document"
    if is_inspection and set(result) & _INSPECTION_RESERVED_RESULT_FIELDS:
        raise _OutputContractError

    candidate = {
        **result,
        "producer": _producer(),
        "record_id": record_id,
        "record_status": record_status,
    }
    if record_error is not None:
        candidate["record_error"] = record_error
    try:
        normalized = model.model_validate(candidate).model_dump(
            mode="json",
            by_alias=True,
        )
        jsonschema.validate(instance=normalized, schema=model.contract_schema)
        if is_inspection:
            InspectionContractV1.validate_critical(normalized)
    except Exception:
        # Validation diagnostics may contain document text, private paths or an
        # implementation sentinel.  Neither the journal nor the MCP client may
        # receive those details.
        raise _OutputContractError from None

    normalized.pop("record_id", None)
    normalized.pop("record_status", None)
    normalized.pop("record_error", None)
    if is_inspection:
        return _checked_inspection_result_from_gate(normalized)
    return normalized


def _with_record(
    *,
    tool_name: str,
    workspace,
    input_payload: dict[str, Any],
    result: dict[str, Any],
    provenance: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]],
    record_result: (
        dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]] | None
    ) = None,
) -> dict[str, Any]:
    try:
        normalized = _validated_success_result(tool_name, result)
        if not isinstance(normalized, dict):
            raise _OutputContractError
        result = normalized
    except _OutputContractError as exc:
        try:
            _record_error(
                tool_name=tool_name,
                workspace=workspace,
                input_payload=input_payload,
                exc=exc,
                # Never derive an error record from a result which just failed
                # its public contract.  A malformed required field may already
                # have flowed into the success provenance assembled by the
                # caller before _with_record was entered.
                provenance={"failure_phase": "output_validation"},
            )
        except Exception:
            # Output validation must stay fail-closed even when the optional
            # local journal cannot record the controlled error.
            pass
        raise

    validated_provenance = provenance(result) if callable(provenance) else provenance
    validated_record_result = (
        record_result(result) if callable(record_result) else record_result
    )

    meta = _validated_record_metadata(
        records.write_record(
            workspace=workspace,
            tool_name=tool_name,
            input_payload=input_payload,
            result=(
                _ok_result(result)
                if validated_record_result is None
                else validated_record_result
            ),
            tool_result=_ok_result(result),
            provenance=validated_provenance,
        )
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
    if tool_name == "inspect_document":
        checked_error: CheckedInspectionError = _checked_inspection_error_from_gate(
            error_code=getattr(exc, "code", "docx_error"),
            error=str(exc),
            provenance=provenance or {},
        )
        records.write_checked_inspection_error_record(
            workspace=workspace,
            input_payload=input_payload,
            error=checked_error,
        )
        return
    records.write_record(
        workspace=workspace,
        tool_name=tool_name,
        input_payload=input_payload,
        result=_error_result(exc),
        tool_result=_error_result(exc),
        provenance=provenance or {},
    )


class _InternalOperationError(veqtor_docx.DocxError):
    """Generic journal representation of an unexpected implementation bug."""

    code = "internal_error"


class _McpBoundaryError(veqtor_docx.DocxError):
    """A context-free error safe to expose through FastMCP."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def _record_internal_error(
    *,
    tool_name: str,
    workspace,
    input_payload: dict[str, Any],
    provenance: dict[str, Any],
) -> None:
    # Deliberately omit the original exception type and message: they can
    # contain document text, local paths or library internals.
    _record_error(
        tool_name=tool_name,
        workspace=workspace,
        input_payload=input_payload,
        exc=_InternalOperationError("unexpected internal failure"),
        provenance=provenance,
    )


def _run_tool_boundary(
    *,
    tool_name: str,
    workspace_resolver: Callable[[], Any],
    input_payload_factory: Callable[[], dict[str, Any]],
    internal_provenance_factory: Callable[[], dict[str, Any]],
    operation: Callable[[Any, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Execute the complete MCP tool pipeline behind one safe boundary."""
    workspace = None
    input_payload: dict[str, Any] = {}
    try:
        input_payload = input_payload_factory()
        workspace = workspace_resolver()
        return operation(workspace, input_payload)
    except veqtor_docx.DocxError:
        raise
    except records.WorkspaceDiscoveryError as exc:
        safe_discovery = json.dumps(
            {
                "error_code": exc.code,
                "workspace_discovery": exc.metadata,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        raise _McpBoundaryError(exc.code, safe_discovery) from None
    except records.DecisionRecordError as exc:
        # Journal-layer diagnostics may contain resolved workspace paths.
        # Preserve the stable machine code but never the local detail at the
        # MCP transport boundary.
        raise _McpBoundaryError(exc.code, "decision-record operation refused") from None
    except Exception:
        if workspace is not None:
            try:
                provenance = internal_provenance_factory()
            except Exception:
                provenance = {}
            try:
                _record_internal_error(
                    tool_name=tool_name,
                    workspace=workspace,
                    input_payload=input_payload,
                    provenance=provenance,
                )
            except Exception:
                # Journaling is best effort and must never replace the safe
                # public error boundary with another implementation detail.
                pass
        raise _McpBoundaryError("internal_error", "unexpected tool failure") from None


def _anchor_from_verify(anchor: dict) -> dict[str, Any]:
    """Copy only the closed public change-unit anchor fields.

    Legacy two-field anchors remain accepted for v0.2 clients.  New v0.3
    anchors bind the same unit to the canonical container policy and to the
    complete public unit fingerprint; journaling and edit forwarding must not
    silently downgrade those stronger bindings.
    """
    return {
        key: anchor[key]
        for key in (
            "schema_version",
            "ref_type",
            "change_unit_id",
            "file_sha256",
            "part_name",
            "paragraph_index",
            "paragraph_text_sha256",
            "reading_mode",
            "container_policy",
            "unit_fingerprint_sha256",
        )
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
        "ordering_source": result["ordering_source"],
        "order_basis": result["order_basis"],
        "revision_count_basis": result["revision_count_basis"],
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
            **unit["anchor"],
            "revision_ids": unit["reference"]["revision_ids"],
            "clause_anchor": unit["clause_anchor"],
        }
        for unit in result["change_units"]
    ]
    return {
        "path": result["path"],
        "file_sha256": result["file_sha256"],
        "part_name": result["part_name"],
        "revision_count_basis": result["revision_count_basis"],
        "revision_inventory": result["revision_inventory"],
        "anchors": records.bounded_observed_anchors(anchors),
    }


def _extract_record_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": records.RESULT_STATUS_OK,
        "path": result["path"],
        "file_sha256": result["file_sha256"],
        "part_name": result["part_name"],
        "revision_count": result["revision_count"],
        "revision_count_basis": result["revision_count_basis"],
        "change_unit_count": len(result["change_units"]),
        "unsupported_revisions": result["unsupported_revisions"],
        "revision_inventory": result["revision_inventory"],
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


def _with_checked_inspection_record(
    *,
    workspace,
    input_payload: dict[str, Any],
    result: CheckedInspectionResult,
) -> dict[str, Any]:
    """Send one checked value to the dedicated journal sink and client."""
    if not isinstance(result, CheckedInspectionResult):
        raise TypeError("inspection success requires a checked result")
    meta = _validated_record_metadata(
        records.write_checked_inspection_record(
            workspace=workspace,
            input_payload=input_payload,
            result=result,
        )
    )
    return {**result.to_dict(), **meta}


def _inspect_error_provenance(
    path: str,
    mode: object,
    exc: veqtor_docx.DocxError,
) -> dict[str, Any]:
    provenance: dict[str, Any] = {"path": path, "mode": mode}
    metadata = getattr(exc, "metadata", None)
    if isinstance(metadata, dict):
        for key in ("claimed_source_sha256", "observed_source_sha256"):
            if key in metadata:
                provenance[key] = metadata[key]
    return provenance


def _verify_provenance(
    result: dict[str, Any], anchor: dict[str, Any]
) -> dict[str, Any]:
    return {
        "file_sha256": result["checked_anchor"]["file_sha256"],
        "checked_anchor": result["checked_anchor"],
        "input_anchor": _anchor_from_verify(anchor),
        "anchors": [
            {
                **result["checked_anchor"],
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
        "input_anchor": _anchor_from_verify(anchor) if isinstance(anchor, dict) else {},
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
    return {
        "source_path": source_path,
        "source_sha256": result["source_sha256"],
        "output_path": result["output_path"],
        "output_sha256": result["output_sha256"],
        "tracked_change_author": result["tracked_change_author"],
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
        "preflight_binding_status": result["preflight_binding_status"],
        "preflight_candidate_sha256": result["preflight_candidate_sha256"],
        "candidate_output_sha256_match": result["candidate_output_sha256_match"],
    }


def _preflight_provenance(
    result: dict[str, Any], source_path: str, edits: list[dict]
) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "source_path": source_path,
        "anchors": _anchors_from_edits(edits),
        "batch_applicable": result["batch_applicable"],
        "tracked_change_author": result["tracked_change_author"],
    }
    if result.get("source_sha256") is not None:
        provenance["source_sha256"] = result["source_sha256"]
    if result.get("blocking_edit_index") is not None:
        provenance["edit_index"] = result["blocking_edit_index"]
    if result.get("observed_candidate_sha256") is not None:
        provenance["observed_candidate_sha256"] = result["observed_candidate_sha256"]
    if result.get("failure_phase") is not None:
        provenance["failure_phase"] = result["failure_phase"]
    if result.get("round_trip_check") is not None:
        provenance["round_trip_check"] = result["round_trip_check"]
    proof = result.get("preflight_proof")
    if isinstance(proof, dict):
        provenance["preflight_proof"] = {
            key: proof[key]
            for key in (
                "schema_version",
                "source_sha256",
                "edits_sha256",
                "tracked_change_author",
                "producer_build",
                "candidate_sha256",
                "proof_sha256",
            )
            if key in proof
        }
    return provenance


def _apply_error_provenance(
    source_path: str,
    output_path: str,
    edits: list[dict],
    exc: veqtor_docx.DocxError,
    tracked_change_author: str,
    preflight_proof: object,
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
        "tracked_change_author": tracked_change_author,
    }
    if claimed is not None:
        provenance["claimed_source_sha256"] = claimed
    if isinstance(metadata, dict):
        if metadata.get("observed_source_sha256") is not None:
            provenance["observed_source_sha256"] = metadata["observed_source_sha256"]
        if metadata.get("observed_candidate_sha256") is not None:
            provenance["observed_candidate_sha256"] = metadata[
                "observed_candidate_sha256"
            ]
        if metadata.get("edit_index") is not None:
            provenance["edit_index"] = metadata["edit_index"]
        if metadata.get("failure_phase") is not None:
            provenance["failure_phase"] = metadata["failure_phase"]
        if isinstance(metadata.get("mismatched_fields"), list):
            provenance["mismatched_fields"] = metadata["mismatched_fields"]
    if isinstance(preflight_proof, dict):
        for key in ("schema_version", "proof_sha256", "candidate_sha256"):
            if key in preflight_proof:
                provenance[f"preflight_{key}"] = preflight_proof[key]
    return provenance


def _decision_record_assurance() -> dict[str, Any]:
    return {
        "journal_model": "best_effort_local_provenance",
        "model_payload": "compact_only",
        "raw_journal_visibility": "private_local_only",
        "raw_journal_result": "tool_specific_summary_not_verbatim_live_response",
        "compact_projection": "privacy_minimized_view_not_raw_journal",
        "access_event_policy": (
            "raw_journal_only_excluded_from_default_compact_records"
        ),
        "tamper_evident": False,
        "hash_chain": False,
        "record_id_guarantee": "strictly_increasing_only",
        "producer_identity": "python_source_files_snapshot_only",
        "content_hashes": "recheckable_fingerprints_not_authentication",
        "round_trip_scope": (
            "ooxml_semantic_diff_outside_touched_anchors_not_docx_byte_identity"
        ),
    }


def _decision_record_export_scope() -> dict[str, Any]:
    return {
        "records_scope": records.EXPORT_RECORDS_SCOPE,
        "total_count_scope": records.EXPORT_TOTAL_COUNT_SCOPE,
        "access_events_in_records": False,
        "access_count_scope": records.EXPORT_ACCESS_COUNT_SCOPE,
        "access_count_includes_current_export": False,
    }


@mcp.tool(
    annotations=local_journaling_annotations("List DOCX negotiation rounds"),
    meta=contract_meta(),
    structured_output=True,
)
def list_rounds(
    folder: str,
    ordered_filenames: list[str] | None = None,
) -> ListRoundsResult:
    """List the DOCX negotiation rounds in a local folder.

    Call this when the user points to a folder of contract drafts or asks
    which rounds/files are available in a negotiation. Rounds are sorted by
    filename by default. ``ordered_filenames`` may instead provide the exact,
    complete filename sequence, but that positional order is not evidence of
    chronology or document lineage. Each entry carries the file's sha256 and
    ``revision_count``. The top-level ``revision_count_basis`` declares that
    this count is the raw number of ``w:ins`` and ``w:del`` elements in
    ``word/document.xml``; it is not the broader revision-inventory total or a
    logical change-unit count. Unreadable files are reported in ``skipped``.
    Folder-level candidate, input and actual expanded-output limits bound the
    complete scan. If a shared limit is exceeded, split the folder and retry;
    the call fails without returning a partial round list.
    """

    def operation(workspace, input_payload):
        try:
            result = veqtor_docx.list_rounds(
                folder,
                ordered_filenames=ordered_filenames,
            )
        except veqtor_docx.DocxError as exc:
            _record_error(
                tool_name="list_rounds",
                workspace=workspace,
                input_payload=input_payload,
                exc=exc,
                provenance={"folder": folder},
            )
            raise
        return _with_record(
            tool_name="list_rounds",
            workspace=workspace,
            input_payload=input_payload,
            result=result,
            provenance=_list_rounds_provenance(result),
        )

    return _run_tool_boundary(
        tool_name="list_rounds",
        workspace_resolver=lambda: records.workspace_for_folder(folder),
        input_payload_factory=lambda: {
            "folder": folder,
            **(
                {"ordered_filenames": ordered_filenames}
                if ordered_filenames is not None
                else {}
            ),
        },
        internal_provenance_factory=lambda: {"folder": folder},
        operation=operation,
    )


@mcp.tool(
    annotations=local_journaling_annotations("Extract DOCX redlines"),
    meta=contract_meta(),
    structured_output=True,
)
def extract_redlines(path: str) -> ExtractRedlinesResult:
    """Extract tracked changes from one DOCX as verifiable change units.

    Call this when the user asks what changed in a DOCX, asks for tracked
    changes, or needs clause anchors. Each change unit states the change type
    (insert/delete/replace), author, date, old/new text, a best-effort clause
    anchor, bounded before/after context from the current paragraph reading,
    a conservative explicit manual paragraph label, and a reference (path,
    OOXML part, revision ids, file sha256) that lets any quote be re-checked
    against the document. ``revision_count_basis`` declares that
    ``revision_count`` is the raw number of ``w:ins`` and ``w:del`` elements in
    ``word/document.xml``. It is distinct from both logical change units and
    ``revision_inventory.total_revision_elements``, whose scope also covers
    recognized unsupported revision markup. Revision kinds the tool does not
    decode are counted in ``unsupported_revisions`` rather than silently
    dropped.
    """

    def operation(workspace, input_payload):
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

    return _run_tool_boundary(
        tool_name="extract_redlines",
        workspace_resolver=lambda: records.workspace_for_file(path),
        input_payload_factory=lambda: {"path": path},
        internal_provenance_factory=lambda: {"source_path": path},
        operation=operation,
    )


@mcp.tool(
    annotations=local_journaling_annotations(
        "Inspect a mechanical accepted/current DOCX body reading"
    ),
    meta=contract_meta(),
    structured_output=True,
)
def inspect_document(
    path: str,
    mode: Literal["outline", "literal_search", "browse", "read"],
    phrases: list[str] | None = None,
    match_basis: Literal[
        "exact_literal",
        "normalized_literal",
        "normalized_casefold_literal",
    ]
    | None = None,
    selection: InspectSelectionInput | None = None,
    cursor: str | None = None,
    max_items: Annotated[
        StrictInt,
        Field(ge=1, le=INSPECT_FIXED_LIMITS_V1["max_items"]),
    ] = DEFAULT_INSPECT_MAX_ITEMS,
) -> InspectDocumentResult:
    """Inspect a mechanical accepted/current reading without a whole-document dump.

    ``outline`` returns structural headings without clause-body text.
    ``literal_search`` searches the supplied phrases independently inside each
    supported body paragraph using the explicit ``match_basis``. ``browse``
    pages supported non-empty paragraphs when outline/search discovery is not
    sufficient. ``read`` resolves exactly one hash-bound ``paragraph_ref`` or
    ``section_ref`` from an earlier result. Section reads are cursor-paginated;
    paragraph reads return one full bounded paragraph and reject cursors.

    A literal-search snippet is only a navigation aid, even when neither
    truncation flag is set. Quote only complete text returned by ``read`` and
    confirmed against the same hash-bound reference with ``verify_quote``.

    All modes use the accepted/current reading of ``word/document.xml`` under
    the disclosed canonical body-flow policy. Filename order, clause labels and
    headings remain navigation only; the file and paragraph hashes are the
    evidence anchors. Calls never mutate the DOCX but normally append local
    provenance like every current Veqtor tool.
    """

    def input_payload() -> dict[str, Any]:
        return {
            "path": path,
            "mode": mode,
            **({"phrases": phrases} if phrases is not None else {}),
            **({"match_basis": match_basis} if match_basis is not None else {}),
            **({"selection": selection} if selection is not None else {}),
            **({"cursor": cursor} if cursor is not None else {}),
            "max_items": max_items,
        }

    def operation(workspace, payload):
        try:
            result = veqtor_docx.inspect_document(
                path,
                mode,
                phrases=phrases,
                match_basis=match_basis,
                selection=selection,
                cursor=cursor,
                max_items=max_items,
            )
        except veqtor_docx.DocxError as exc:
            _record_error(
                tool_name="inspect_document",
                workspace=workspace,
                input_payload=payload,
                exc=exc,
                provenance=_inspect_error_provenance(path, mode, exc),
            )
            raise
        try:
            checked = _validated_success_result("inspect_document", result)
            if not isinstance(checked, CheckedInspectionResult):
                raise _OutputContractError
        except _OutputContractError as exc:
            try:
                _record_error(
                    tool_name="inspect_document",
                    workspace=workspace,
                    input_payload=payload,
                    exc=exc,
                    provenance={"failure_phase": "output_validation"},
                )
            except Exception:
                pass
            raise
        return _with_checked_inspection_record(
            workspace=workspace,
            input_payload=payload,
            result=checked,
        )

    return _run_tool_boundary(
        tool_name="inspect_document",
        workspace_resolver=lambda: records.workspace_for_file(path),
        input_payload_factory=input_payload,
        internal_provenance_factory=lambda: {"source_path": path, "mode": mode},
        operation=operation,
    )


@mcp.tool(
    annotations=local_journaling_annotations("Map bounded document rounds"),
    meta=contract_meta(),
    structured_output=True,
)
def map_rounds(
    folder: str,
    seed: RoundMapSeedInput,
    ordered_filenames: list[str] | None = None,
    cursor: str | None = None,
    max_items: Annotated[StrictInt, Field(ge=1, le=100)] = 50,
) -> RoundMapResult:
    """Map bounded exact evidence around one hash-bound paragraph.

    The map reports only recorded document-byte derivation, complete exact
    paragraph equality, navigation candidates and explicit unresolved or
    ambiguous states.  Filenames, positions, mtimes, labels and headings never
    become chronology or lineage.  The local journal is mutable and
    non-tamper-evident; ``recorded_derivation`` states only what a validated
    local apply record says.
    """
    input_payload = {
        "folder": folder,
        "seed": seed,
        "ordered_filenames": ordered_filenames,
        "cursor": cursor,
        "max_items": max_items,
    }
    try:
        computation = round_map.build_round_map(
            folder,
            seed,
            ordered_filenames=ordered_filenames,
            cursor=cursor,
            max_items=max_items,
        )
        normalized = _validated_success_result("map_rounds", computation.result)
        if not isinstance(normalized, dict):
            raise _OutputContractError
        round_map.validate_computation_result(computation, normalized)
        meta = _validated_record_metadata(
            records.write_record(
                workspace=computation.workspace,
                tool_name="map_rounds",
                input_payload=input_payload,
                result=round_map.record_summary(computation),
                tool_result=normalized,
                provenance=round_map.record_provenance(computation),
                expected_workspace_identity=computation.workspace_identity,
            )
        )
        return {**normalized, **meta}
    except (veqtor_docx.DocxError, _OutputContractError):
        raise
    except records.DecisionRecordError as exc:
        raise _McpBoundaryError(exc.code, "decision-record operation refused") from None
    except Exception:
        raise _McpBoundaryError("internal_error", "unexpected tool failure") from None


@mcp.tool(
    annotations=local_journaling_annotations("Preflight tracked edits"),
    meta=contract_meta(),
    structured_output=True,
)
def preflight_edits(
    source_path: str,
    edits: list[EditInput],
) -> PreflightEditsResult:
    """Dry-run an atomic edit batch through the complete DOCX pipeline.

    Call this before ``apply_edits``. It uses the same source snapshot,
    planner, OOXML surgery, candidate serialization, re-extraction, round-trip
    proof and collateral-change check as apply, but keeps the candidate in
    memory and never creates an output DOCX. ``batch_applicable`` is therefore
    authoritative for document-processing failures on the same bytes, build,
    configuration and edit payload. A later apply may still fail if the source
    changes or the output cannot be published. Like other read-only document
    tools, this call records local provenance in the workspace sidecar unless
    decision records are disabled.
    """
    context: dict[str, Any] = {}

    def internal_provenance() -> dict[str, Any]:
        provenance = {
            "source_path": source_path,
            "anchors": _anchors_from_edits(edits) if isinstance(edits, list) else [],
        }
        if "tracked_change_author" in context:
            provenance["tracked_change_author"] = context["tracked_change_author"]
        return provenance

    def operation(workspace, input_payload):
        tracked_change_author = _tracked_change_author()
        context["tracked_change_author"] = tracked_change_author
        try:
            result = veqtor_docx.preflight_edits(
                source_path,
                edits,
                author=tracked_change_author,
                producer_build=records.SOURCE_SNAPSHOT_IDENTITY,
            )
        except veqtor_docx.DocxError as exc:
            _record_error(
                tool_name="preflight_edits",
                workspace=workspace,
                input_payload=input_payload,
                exc=exc,
                provenance=internal_provenance(),
            )
            raise
        return _with_record(
            tool_name="preflight_edits",
            workspace=workspace,
            input_payload=input_payload,
            result=result,
            provenance=_preflight_provenance(result, source_path, edits),
        )

    return _run_tool_boundary(
        tool_name="preflight_edits",
        workspace_resolver=lambda: records.workspace_for_file(source_path),
        input_payload_factory=lambda: {
            "source_path": source_path,
            "edits": _record_edits(edits),
        },
        internal_provenance_factory=internal_provenance,
        operation=operation,
    )


@mcp.tool(
    annotations=local_journaling_annotations("Apply tracked edits"),
    meta=contract_meta(),
    structured_output=True,
)
def apply_edits(
    source_path: str,
    output_path: str,
    edits: list[EditInput],
    preflight_proof: PreflightProofInput,
) -> ApplyEditsResult:
    """Create a new DOCX with the given edits applied as real tracked changes.

    Call this only after the user asks to prepare or apply counter wording,
    and only with anchors produced by ``extract_redlines``. Each edit needs
    ``anchor`` ({change_unit_id, file_sha256}) plus either ``delete_text``
    with optional ``insert_text``, or ``reinstate_text``. ``delete_text``
    must occur exactly once in the anchored clause: in untouched text it
    becomes a plain tracked replace/delete; entirely inside one counterparty
    pending insertion it becomes a visible counter (their proposal stays,
    struck through, with our replacement after it). ``reinstate_text`` adds a
    visible tracked insertion before the preserved counterparty deletion; it
    does not accept, reject or remove that deletion. Several edits may target
    one paragraph if their spans do not overlap. Edits are atomic and fail
    closed: a hash mismatch, missing or ambiguous match, or unsupported overlap
    returns an error and writes nothing. After writing, the server
    re-extracts the output and checks the documented round trip: exactly the
    prior change units plus the proposed edits, with an OOXML semantic diff
    outside the touched anchors. This is not a byte-identity check of the DOCX
    package. The source file is never modified.
    ``preflight_proof`` must be the complete proof returned by the successful
    preflight call for the exact source, edit payload, configured author,
    producer build and candidate. It detects drift; it is not authentication
    or a digital signature.
    """
    context: dict[str, Any] = {}

    def internal_provenance() -> dict[str, Any]:
        provenance = {
            "source_path": source_path,
            "output_path": output_path,
            "anchors": _anchors_from_edits(edits) if isinstance(edits, list) else [],
        }
        if "tracked_change_author" in context:
            provenance["tracked_change_author"] = context["tracked_change_author"]
        if isinstance(preflight_proof, dict):
            provenance["preflight_proof_sha256"] = preflight_proof.get("proof_sha256")
        return provenance

    def operation(workspace, input_payload):
        tracked_change_author = _tracked_change_author()
        context["tracked_change_author"] = tracked_change_author
        try:
            result = veqtor_docx.apply_edits(
                source_path,
                output_path,
                edits,
                author=tracked_change_author,
                preflight_proof=preflight_proof,
                producer_build=records.SOURCE_SNAPSHOT_IDENTITY,
            )
        except veqtor_docx.DocxError as exc:
            error_provenance = (
                _apply_error_provenance(
                    source_path,
                    output_path,
                    edits,
                    exc,
                    tracked_change_author,
                    preflight_proof,
                )
                if isinstance(edits, list)
                else {
                    "source_path": source_path,
                    "output_path": output_path,
                    "anchors": [],
                    "tracked_change_author": tracked_change_author,
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

    return _run_tool_boundary(
        tool_name="apply_edits",
        workspace_resolver=lambda: records.workspace_for_file(source_path),
        input_payload_factory=lambda: {
            "source_path": source_path,
            "output_path": output_path,
            "edits": _record_edits(edits),
            "preflight_proof": preflight_proof,
        },
        internal_provenance_factory=internal_provenance,
        operation=operation,
    )


@mcp.tool(
    annotations=local_journaling_annotations("Verify a DOCX quote"),
    meta=contract_meta(),
    structured_output=True,
)
def verify_quote(
    path: str,
    anchor: VerifyAnchorInput,
    quote: str,
) -> VerifyQuoteResult:
    """Check a quotation against the document before relying on it.

    Call this before using a quote in a memo, email, or negotiation summary.
    ``anchor`` is either a legacy/v2 change-unit anchor from
    ``extract_redlines`` or a ``paragraph_ref.v1`` from ``inspect_document``.
    The verdict is ``exact`` (verbatim in the anchored old/new change text or
    accepted/current paragraph), ``normalized`` (matches after collapsing
    whitespace and typographic quotes/dashes — ``diff`` says so), or
    ``not_found``.
    Matching is case-sensitive and deterministic; a hash mismatch or unknown
    anchor is an error, never a guess. The verdict covers only the anchored
    unit or paragraph, not the whole document or the legal accuracy of the
    quote.
    """

    def internal_provenance() -> dict[str, Any]:
        return {
            "source_path": path,
            "anchor": _anchor_from_verify(anchor) if isinstance(anchor, dict) else None,
        }

    def operation(workspace, input_payload):
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

    return _run_tool_boundary(
        tool_name="verify_quote",
        workspace_resolver=lambda: records.workspace_for_file(path),
        input_payload_factory=lambda: {
            "path": path,
            "anchor": _anchor_from_verify(anchor)
            if isinstance(anchor, dict)
            else anchor,
            "quote": quote,
        },
        internal_provenance_factory=internal_provenance,
        operation=operation,
    )


@mcp.tool(
    annotations=local_journaling_annotations("Export decision records"),
    meta=contract_meta(),
    structured_output=True,
)
def export_decision_record(
    workspace: str,
    max_records: Annotated[
        StrictInt,
        Field(ge=1, le=records.MAX_MAX_RECORDS),
    ]
    | None = None,
    before_record_id: str | None = None,
) -> ExportDecisionRecordResult:
    """Return compact local provenance entries for a matter workspace.

    Call this when the user asks what toolchain actions were performed or
    what re-checkable evidence relates them to document bytes. This is a
    best-effort local provenance history, not a tamper-evident audit log:
    content hashes are fingerprints, not authentication or a hash chain, and
    strictly increasing record ids do not prove that records were not deleted
    or rewritten. ``producer.build`` identifies imported Python source files,
    not the complete binary environment. MCP returns only a compact projection;
    the raw local journal may contain private matter text and is never returned
    by this tool. When local journaling is enabled and its write succeeds, each
    export appends an ``access_event.v1`` to that private raw journal after
    taking the response snapshot. That event is intentionally excluded from
    ``records`` and ``total_count``; it first appears in ``access_count`` on the
    next export, so gaps in record ids are normal. A raw journal record may hold
    a tool-specific result summary, while this response is a privacy-minimized
    compact projection rather than a copy of the raw journal or the complete
    live tool response. ``max_records`` accepts only a strict integer from 1
    through 500; values outside that bounded response window are refused before
    reading or journaling.
    """

    def operation(root, input_payload):
        assurance = _decision_record_assurance()
        export_scope = _decision_record_export_scope()

        def live_result(snapshot, meta):
            current_export_event = {
                "record_id": meta["record_id"],
                "record_type": records.ACCESS_RECORD_TYPE,
                "record_status": meta["record_status"],
                "recorded_locally": meta["record_status"] == "written",
                "included_in_records": False,
                "included_in_total_count": False,
                "included_in_access_count": False,
            }
            return {
                **snapshot,
                "returned_count": len(snapshot["records"]),
                "assurance": assurance,
                **export_scope,
                "access_events_recorded_locally": current_export_event[
                    "recorded_locally"
                ],
                "current_export_event": current_export_event,
            }

        def export_summary(result):
            # The snapshot-dependent live response is validated while the
            # journal lock is held and before the access event is appended.
            # Disabled placeholder metadata has the same public schema as the
            # later written/write_failed metadata.
            _validated_success_result(
                "export_decision_record",
                live_result(
                    result,
                    {"record_id": None, "record_status": "disabled"},
                ),
            )
            return {
                "status": records.RESULT_STATUS_OK,
                "workspace": result["workspace"],
                "total_count": result["total_count"],
                "access_count": result["access_count"],
                "returned_count": len(result["records"]),
                "truncated": result["truncated"],
                "next_before_record_id": result["next_before_record_id"],
                "payloads": result["payloads"],
                "assurance": assurance,
                **export_scope,
            }

        result, meta = records.export_records_with_access_event(
            workspace=root,
            max_records=max_records,
            before_record_id=before_record_id,
            input_payload=input_payload,
            result_factory=export_summary,
        )
        meta = _validated_record_metadata(meta)
        normalized = _validated_success_result(
            "export_decision_record",
            live_result(result, meta),
            record_id=meta["record_id"],
            record_status=meta["record_status"],
            record_error=meta.get("record_error"),
        )
        return {
            **normalized,
            **meta,
        }

    return _run_tool_boundary(
        tool_name="export_decision_record",
        workspace_resolver=lambda: records.workspace_for_folder(workspace),
        input_payload_factory=lambda: {
            "workspace": workspace,
            "max_records": max_records,
            "before_record_id": before_record_id,
        },
        internal_provenance_factory=lambda: {},
        operation=operation,
    )


def main() -> None:
    if sys.argv[1:] == ["--version"]:
        print(f"veqtor-mcp {__version__}")
        return
    if sys.argv[1:] == ["doctor"]:
        supported_python = (3, 12) <= sys.version_info[:2] < (3, 15)
        supported_platform = sys.platform.startswith(("darwin", "linux"))
        configuration_error: dict[str, str] | None = None
        try:
            tracked_change_author = _tracked_change_author()
        except RuntimeError as exc:
            tracked_change_author = None
            configuration_error = {
                "code": "tracked_change_author_invalid",
                "message": str(exc),
            }
        status = (
            "error"
            if configuration_error is not None
            else "ok"
            if supported_python and supported_platform
            else "unsupported"
        )
        print(
            json.dumps(
                {
                    "name": "veqtor-mcp",
                    "version": __version__,
                    "build": records.SOURCE_SNAPSHOT_IDENTITY,
                    "python": platform.python_version(),
                    "platform": sys.platform,
                    "supported_python": supported_python,
                    "supported_platform": supported_platform,
                    "tracked_change_author": tracked_change_author,
                    "configuration_error": configuration_error,
                    "decision_records": (
                        "disabled" if records.disabled() else "enabled"
                    ),
                    "status": status,
                },
                sort_keys=True,
            )
        )
        if (
            configuration_error is not None
            or not supported_python
            or not supported_platform
        ):
            raise SystemExit(2)
        return
    if sys.argv[1:]:
        print(
            "usage: veqtor-mcp [--version|doctor]",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        _tracked_change_author()
    except RuntimeError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    mcp.run()


if __name__ == "__main__":
    main()
