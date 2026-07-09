# SPDX-License-Identifier: Apache-2.0
"""Veqtor MCP server: deterministic DOCX facts for MCP-compatible clients.

M1 exposes the read path — ``list_rounds`` and ``extract_redlines``. The
tools return document facts with verifiable references (file hash, OOXML
part, revision ids); legal interpretation stays with the calling model.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

import veqtor_docx

mcp = FastMCP("veqtor")


@mcp.tool()
def list_rounds(folder: str) -> dict:
    """List the DOCX negotiation rounds in a local folder.

    Call this when the user points to a folder of contract drafts or asks
    which rounds/files are available in a negotiation. Rounds are sorted by
    filename; each entry carries the file's sha256 and the raw count of
    tracked revisions inside. Unreadable files are reported in ``skipped``.
    """
    return veqtor_docx.list_rounds(folder)


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
    return veqtor_docx.extract_redlines(path)


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
    re-extracts the output and proves the round trip: exactly the prior
    change units plus the proposed edits, no collateral changes outside the
    touched clauses. The source file is never modified.
    """
    return veqtor_docx.apply_edits(source_path, output_path, edits)


@mcp.tool()
def verify_quote(path: str, anchor: dict, quote: str) -> dict:
    """Check a quotation against the document before relying on it.

    Call this before using a quote in a memo, email, or negotiation summary.
    ``anchor`` is {change_unit_id, file_sha256} from ``extract_redlines``.
    The verdict is ``exact`` (verbatim in the anchored change unit's old or
    new text), ``normalized`` (matches after collapsing whitespace and
    typographic quotes/dashes — ``diff`` says so), or ``not_found``.
    Matching is case-sensitive and deterministic; a hash mismatch or unknown
    anchor is an error, never a guess.
    """
    return veqtor_docx.verify_quote(path, anchor, quote)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
