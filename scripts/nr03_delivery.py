# SPDX-License-Identifier: Apache-2.0
"""Deterministic delivery of resolved files and the native acceptance boundary."""
import json
from pathlib import Path

from check_codex_acceptance import _file_sha256, _require

DELIVERY_VERSION = "nr03-acceptance-client.v2"
EXPORT_PAGE_LIMIT = 20
BOUNDARY = """Use the complete delivered Veqtor skill and resolved canonical workflow below.
These files are already supplied in full; do not reopen their paths or discover
other skill/workflow files. The ordinary user request follows them.

For this native acceptance conversation, every tool action must use the supplied
Veqtor MCP server, including document/output existence checks and unknown-write
recovery checks. No shell or terminal commands, Python, filesystem/skill discovery,
environment or renderer discovery, unrelated services, settings changes or send.
The external observer performs all rendering and visible-markup inspection; leave
that visual gate open in your result. This boundary also applies to every resumed
turn. Select the appropriate MCP tools independently from the user request and
workflow; no successful tool-call sequence is prescribed.

For action-record exports request max_records at most 20 and follow each returned
next_before_record_id as before_record_id until the terminal page. The count bound
does not guarantee that a large record will fit the client transport. Require
complete, consistent responses; an API truncated=false does not prove transport
completeness. If a response is missing, clipped or otherwise unverifiable, report
incomplete journal evidence separately from any successful Word result. Preserve
the failed evidence; do not claim a complete export, initialize/repair the journal,
resend the contract or repeat the document write to recover the journal.
"""


def delivered_prompt(directory, user, workflow_files):
    parts = [f'<acceptance-client-boundary version="{DELIVERY_VERSION}">\n{BOUNDARY}</acceptance-client-boundary>']
    for name in workflow_files:
        path = Path(directory) / "workflow" / name
        parts.append(f"<delivered-file path={json.dumps(name)} sha256={json.dumps(_file_sha256(str(path)))}>\n"
                     f"{path.read_text()}\n</delivered-file>")
    parts.append("<user-request>\n" + user + "\n</user-request>")
    return "\n\n".join(parts)


def validate_export_limits(calls):
    """Enforce the campaign bound on every turn, including briefs/ancestors."""
    for call in calls:
        if call["tool"] == "export_decision_record":
            limit = call["arguments"].get("max_records")
            _require(type(limit) is int and 1 <= limit <= EXPORT_PAGE_LIMIT,
                     "native export violates the explicit 20-record page bound")
