<!-- SPDX-License-Identifier: Apache-2.0 -->
# Author drafting in one working document

Development version `0.5.0.dev0` adds three tools to the existing eleven-tool
surface. It is not a published release. This workflow supports repeated native
Word edits under the configured author, ordinary anchored comments, and saving
back to the same path with an exact backup. The legacy counterproposal tools
keep their original separate-output and revision-preservation contract.

## Author profile

Configure `VEQTOR_TRACKED_CHANGE_AUTHOR` and optionally `VEQTOR_COMMENT_INITIALS`
in the MCP server environment, then restart that server. For example:

```toml
[mcp_servers.veqtor.env]
VEQTOR_TRACKED_CHANGE_AUTHOR = "Shilov, Ilya"
VEQTOR_COMMENT_INITIALS = "IS"
```

The name is a Word revision/comment author string. The server does not sign in
to that person's account or change Word's own preferences. Match the author in
Word on each computer to continue native editing with that name. Initials
default to the name's initials (given name first for `Family, Given`). The
profile is fixed for a server process, and every successful preflight reports it.

## Tool sequence

1. Read current text and obtain complete `paragraph_ref.v1` references with
   `inspect_document`. Read comments and `file_sha256` with `read_document_comments`.
2. Call `preflight_document_edits(source_path, file_sha256, operations,
   output_path=None)`. A successful response contains `batch_applicable=true`
   and the complete `drafting_preflight.v1` proof. A refused batch reports a
   stable `refusal_code` and, where available, `blocking_operation_index`.
3. Call `apply_document_edits` with exactly those arguments and that proof.
   Omitted output path or the same path means in-place save. Another path means
   a new copy, never overwrite of an existing destination.
4. Reread references after each save. Every reference/hash identifies exact
   bytes, so references from the previous version deliberately become stale.

Operations are closed objects:

```json
{"kind":"replace_text","paragraph_ref":{},"old_text":"30 days","new_text":"45 days"}
{"kind":"add_comment","paragraph_ref":{},"quote":"45 days","text":"Please confirm timing."}
{"kind":"update_comment","comment_id":"3","text":"Timing agreed."}
{"kind":"delete_comment","comment_id":"3"}
```

Replace `{}` with the complete reference from inspection. Operations execute
in the listed order against one in-memory candidate. References identify the
original snapshot's paragraphs; exact text matches address the current reading
at that step. Thus a batch can replace text and then comment on the replacement.
An empty `new_text` deletes. New paragraph insertion, arbitrary insertion-only
operations, formatting changes and moving text are outside this first contract.
There are at most 100 operations, 20,000 characters per text field and 200,000
characters across a batch. Any refusal discards the entire candidate.

## Revision behavior

- Untouched simple text gets ordinary Word deletion/insertion markup.
- Text entirely inside one simple pending insertion by the exact same author
  is changed within that insertion, retaining its author, ID and date. Deleting
  all of its text removes the empty insertion; no extra deletion is created.
- When Word's `_GoBack` cursor bookmark starts inside the replaced span of an
  own insertion and has one matching end after that insertion in the same
  paragraph, its existing start moves to the beginning of the replacement.
  Both bookmark identities/attributes and the end are preserved; the operation
  reports `relocated_bookmarks`. This exception does not permit crossing
  ordinary bookmarks, comment ranges, foreign insertions or paragraph boundaries.
- Supported suffix/whole counterparty insertions can be countered visibly;
  unsupported nested or mixed spans are refused. Another author's proposal is
  not silently rewritten as the configured author's original text.
- Original run formatting and unrelated package parts are preserved. New
  replacement text inherits the first replaced run's formatting. New revision
  IDs follow the document sequence; new revisions omit the optional date.
- Text editing enables Word's Track Changes setting for subsequent manual work.
- Fields, protected documents, unsupported structural revisions/containers and
  ranges crossing the edited span may be refused. A refusal is not permission
  to flatten or accept existing changes.

## Comments

`read_document_comments(path, offset=0, max_items=100)` returns IDs, author,
initials, text, canonical paragraph indexes, total count, file hash and
`next_offset`. All pages must have the same file hash.

Add comments to a unique exact quote in supported simple text or one own
insertion. Updating/deleting requires exact author equality. Other authors'
comments remain readable and unchanged. Existing simple comment updates retain
their identity, author, date, initials and paragraph metadata. Rich/multiparagraph
or threaded comment mutations may be refused. Reply creation and resolve/reopen
are not provided. Modern comment metadata is preserved, with supported matching
metadata removed on deletion. Headless rendering often omits comments; package
relationships and range anchors are checked separately.

## Saving and recovery

Preflight binds source path/hash, destination, operations, author, initials,
producer build and candidate bytes. It writes no document, backup or journal.
Apply regenerates and verifies that same candidate before publication.

In-place apply takes a cooperative exclusive lock, checks Word owner files and
source identity/bytes, stages and synchronizes the candidate, retains the exact
previous source in `.veqtor-backups` beside the working file, and atomically
replaces the working path. The result reports `backup_path`, `backup_sha256`,
`source_sha256` and `output_sha256`. Completed backups survive late failures.
Backups are not automatically deleted. File mode and extended attributes
(including macOS Finder tags) are preserved and checked after copying; unsafe
source symlinks/hardlinks and unsafe backup locations are refused.
Extended access-control lists on source, staging or backup locations are
refused with `unsupported_permissions` before document publication. Do not
remove existing access restrictions to bypass that refusal.

Close Word before asking Veqtor to save; reopen the file afterwards. Owner-file
and last-moment source checks detect observed changes, but Word/cloud-sync
writers do not participate in Veqtor's lock, so this is not cross-application
transactional editing. An error after publication can report
`publication_completed=true`; reread the file before retrying. A changed source
requires inspection and a new preflight, never blind repetition.

The three drafting tools return `record_status=disabled`: immutable legacy
counterproposal records do not describe mutation of an earlier revision.
Backups and per-call results support recovery, not a complete negotiation
history. No existing author names are bulk-renamed by this workflow.
