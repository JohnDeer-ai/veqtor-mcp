<!-- SPDX-License-Identifier: Apache-2.0 -->
# Internal outgoing-package foundation

`veqtor_docx.outgoing_package` prepares a bounded selection of one cumulative
redline and/or one clean draft from a captured exact-byte `DocumentSnapshot`.
This is an internal F8 slice, not a public MCP command or a completed round.

`prepare_outgoing_package(source, destination, redline_name=...,
clean_draft_name=...)` returns immutable candidate files and a bounded UTF-8 JSON
report. Names, destination, source/output hashes and the exact preservation
policy are bound into `plan_sha256`. With both cleanup policies set to
`preserve_all`, the redline preserves every source byte, including unrecognized
custom XML, comments, properties and existing revisions. Selected supported
cleanup changes only the requested comment/property surfaces in the candidate.
A clean draft explicitly accepts all remaining supported revisions through the
existing whole-package clean engine. Unknown/uninspectable markup still refuses;
selecting an unsupported clean member refuses the entire requested kit.

Comment and property policies default to preservation. Explicit supported
cleanup policies transform only the outgoing copies and retain the full preview
in the internal report; see DEAL_STATE.md. Uninspectable dependencies refuse.
The readiness value is always
`prepared_unreviewed`; no confidentiality review, Word acceptance, legal
agreement or actual send is inferred. Exact-byte retention does not freeze
automatically recalculated Word fields or prove identical displayed text.

The caller saves `plan.report` outside the outgoing destination before calling
`publish_outgoing_package(plan, destination, expected_plan_sha256=...)`. Report
persistence belongs to the caller and is not silently assumed to have happened
by this internal engine. The development deal-state workflow performs that
check and binds the plan to the current working/deal basis before publication.

Publication verifies every selected file in a private sibling staging folder
and atomically renames the entire folder into one new destination on the same
supported local filesystem. Existing paths are never overwritten. Filenames
are portable, distinct even on case-insensitive filesystems, and cannot contain
paths. Only selected DOCX files enter the outgoing folder. Stage cleanup removes
only objects demonstrably owned by that operation; unknown entries remain.

A late failure reports `publication_completed` as true, false or unknown and
whether the output path was verified. Call `verify_outgoing_package` with the
same frozen plan before retrying an uncertain operation. It checks exact file
membership and bytes, rejects aliases and extra/changed files, and performs no
repair or deletion. Its `matched` result is an observed match, not a durability
receipt, new publication, readiness decision or evidence of sending.

The development deal-state workflow now persists the complete report, binds
the preview to the selected working/issue/positions basis and exposes explicit
publication/verification commands through `mutate_deal_state`; see DEAL_STATE.md.
Selected full comment-thread/property cleanup and the optional bounded table
below are integrated. Complete first/next-round acceptance and remaining F9
fields are separate work. Final lawyer-approved DOCX remains F10. The present
tests use fictional packages
and publication faults; real acceptance and native Word checks remain distinct.

## Optional selected disagreement table

`disagreement_table` is an optional closed view (`name`, `title`, `language`,
`columns`, `rows`). The engine accepts only sent/incoming quote and proposed-wording
columns, 1–20 rows, and plain strings up to 8,000 characters or explicit nulls.
The MCP layer derives this view from checked original issue state; direct engine
input is only a rendering contract, not proof of quotation provenance.

The new DOCX is reproduced deterministically and included in the same atomic
folder (up to three files, or table only). A v3 internal report freezes the full
view. Without a table, v1/v2 report bytes and behavior remain unchanged. Exact name
collisions, invalid characters and unsupported selected contract copies refuse
the whole kit. Existing publish/recovery verification covers every member.
