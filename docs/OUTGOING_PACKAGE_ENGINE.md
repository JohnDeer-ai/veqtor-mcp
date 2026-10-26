<!-- SPDX-License-Identifier: Apache-2.0 -->
# Internal outgoing-package foundation

`veqtor_docx.outgoing_package` prepares a bounded selection of one cumulative
redline and/or one clean draft from a captured exact-byte `DocumentSnapshot`.
This is an internal F8 slice, not a public MCP command or a completed round.

`prepare_outgoing_package(source, destination, redline_name=...,
clean_draft_name=...)` returns immutable candidate files and a bounded UTF-8 JSON
report. Names, destination, source/output hashes and the exact preservation
policy are bound into `plan_sha256`. The redline preserves every source byte,
including unrecognized custom XML, comments, properties and existing revisions.
A clean draft explicitly accepts all remaining supported revisions through the
existing whole-package clean engine. Unknown/uninspectable markup still refuses;
selecting an unsupported clean member refuses the entire requested kit.

Comments and document properties are always preserved in this slice. Inventory
blockers remain visible in the internal report. The readiness value is always
`prepared_unreviewed`; no confidentiality review, Word acceptance, legal
agreement or actual send is inferred. Exact-byte retention does not freeze
automatically recalculated Word fields or prove identical displayed text.

The caller saves `plan.report` outside the outgoing destination before calling
`publish_outgoing_package(plan, destination, expected_plan_sha256=...)`. Report
persistence belongs to the caller and is not silently assumed to have happened
by this internal engine. A later public workflow must verify it and bind the
plan to the current working/deal basis before publication.

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

Still required for F8: an explicit user-facing preview/confirmation contract,
selected full comment-thread and property cleanup, durable internal reports,
prepared-package links in deal state, and the unified first/next-round recipe.
Final lawyer-approved DOCX remains F10. The present tests use fictional packages
and publication faults; real acceptance and native Word checks remain distinct.
