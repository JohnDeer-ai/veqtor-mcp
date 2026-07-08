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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
