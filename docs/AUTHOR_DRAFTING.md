<!-- SPDX-License-Identifier: Apache-2.0 -->
# Author drafting in one working document

Version `0.5.0` adds three tools to the existing eleven-tool surface.
Publication is established by the matching verified PyPI and GitHub release. This workflow supports repeated native
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
The Claude Desktop extension asks for both author and initials during setup.

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
  IDs follow the document sequence. Preflight captures one timestamp with the
  machine's local UTC offset, binds it into its proof and reuses it during apply.
  New revisions and comments store that timestamp's local wall time, without a
  timezone suffix, in legacy `w:date`, so Word displays the captured local time.
  The proof retains the explicit offset and the actual instant; apply does not
  consult the current timezone. No modern comment extensions are created for dates.
  Updating an existing own insertion or comment retains its original date.
- Word's cached `lastRenderedPageBreak` markers do not block text edits and
  are preserved. Paragraph formatting history and neighboring paragraph-mark
  revisions remain untouched. Whole runs with tracked character formatting can
  be replaced while retaining their history on the deleted text; a replacement
  inherits the current formatting. Splitting a run with tracked formatting, or
  deleting its formatting history inside an own insertion, remains unsupported.
- Re-editing an own insertion preserves same-author character-format history.
  If the touched run carries another author's pending character-format change,
  preflight returns `foreign_formatting_unsupported`: extending that history
  onto replacement text would change the scope of the other author's edit.
  Veqtor does not silently accept or discard that formatting history.
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
are not provided. Modern comment metadata is resolved through package
relationships, including nonstandard internal names. Supported matching metadata
is removed on deletion; missing, external, ambiguous or unbound metadata refuses
the operation. Headless rendering often omits comments; package
relationships and range anchors are checked separately.
If the entire text of an own insertion is deleted, an attached comment remains
with its body, author and references intact at that position. The quoted range
can collapse to a point; this is not deletion of the comment.

## Saving and recovery

Preflight binds source path/hash, destination, operations, author, initials,
producer build, timestamp with explicit timezone and candidate bytes. It writes
no document, backup or journal.
Apply regenerates and verifies that same candidate before publication.

In-place apply takes a cooperative exclusive lock, checks Word owner files and
source identity/bytes, stages and synchronizes the candidate, retains the exact
previous source in `.veqtor-backups` beside the working file, and atomically
replaces the working path. The result reports `backup_path`, `backup_sha256`,
`source_sha256` and `output_sha256`. Completed backups survive late failures.
Backups are not automatically deleted. File mode and extended attributes
(including macOS Finder tags) are preserved and checked after copying. The
system-managed macOS `com.apple.provenance` is excluded from copying/equality;
the original document is not stripped of attributes. Unsafe
source symlinks/hardlinks and unsafe backup locations are refused.
The directory lock is released by the operating system if a writer crashes. A
complete sidecar with PID, token and process-instance identity is published
atomically for compatibility. The identity includes the machine boot and process
start, so a reused PID cannot impersonate the crashed writer. Recognized dead
writers are recovered automatically, including a confirmed exited process
that is still waiting for its parent to reap it. An older PID-only lock is recovered only
when that PID is absent; live, unknown or invalid locks remain protected.
On macOS, a public process-start query can identify a reused PID even when the
usual process query is denied across user accounts. A permission error alone
never proves that the original writer has ended.

Staged bytes are held in private operation directories with an ownership
manifest. A later save recovers verified abandoned complete or partial stages,
including stages inside the backup directory, without deleting the published
document or completed backup. Stages with unexpected entries, changed completed
bytes, links or unverifiable ownership are left untouched. Old unregistered
`.veqtor-*.tmp` files from earlier versions are not deleted by filename alone.
Complete abandoned `.veqtor-lock-*.tmp` ownership records are recovered too.
During cleanup, the stage manifest moves atomically to a `.veqtor-cleanup-*.json`
record beside its directory and remains until directory removal is synchronized.
A later save can finish that cleanup after another crash.

There is a deliberately conservative bootstrap boundary: a crash before a valid
ownership record is written can leave an empty/partial `.veqtor-lock-*.tmp` or a
`.veqtor-stage-*` directory with no valid `owner.json`. These unregistered entries
do not prevent later saves, but cannot be deleted automatically on name alone.
Retain unknown entries; after closing all writers, manually remove only entries
whose origin has been verified. A permission change during cleanup can also
leave a registered stage; a later save recovers it once the owning process has
ended and permissions allow cleanup.
Case-only aliases of the same regular file are recognized by file identity on
case-insensitive filesystems; distinct files and symlinks remain protected.
Extended access-control lists on source, staging or backup locations are
refused with `unsupported_permissions` before document publication. Do not
remove existing access restrictions to bypass that refusal.
The publication parent may have non-inheritable macOS ACL rules, such as the
standard Desktop/Documents/Downloads rule protecting the folder itself from
deletion. File/directory inheritance flags still cause a refusal. Actual create,
rename and delete permissions remain enforced by macOS.

Close Word before asking Veqtor to save; reopen the file afterwards. Owner-file
and last-moment source checks detect observed changes, but Word/cloud-sync
writers do not participate in Veqtor's lock, so this is not cross-application
transactional editing. Publication failures return an MCP error with a closed
`drafting_error.v1` recovery payload: `output_path`, `backup_path`,
`backup_sha256`, `backup_path_verified`, `publication_completed`, and nullable
`lock_path`/`lock_reason`. Both success and structured error shapes are declared
in `apply_document_edits.outputSchema`; errors still carry `isError=true`.
Paths are limited to this requested document and its generated backup; raw
exceptions are not exposed. A false `backup_path_verified` means the reported
path is its last known location (for example, its folder moved externally).
An error after publication reports `publication_completed=true`; reread the
file before retrying. A changed source
requires inspection and a new preflight, never blind repetition.

An unverifiable save sidecar reports its exact bound `lock_path` and
`lock_reason=owner_not_proven_dead`. Do not remove it while a writer might still
be active. Close all Word/Veqtor writers, inspect that specific lock and its
owner, and retain the original document and backups before manual recovery.

Saving is restricted to supported local filesystems: APFS/HFS on macOS;
ext2/3/4, XFS, Btrfs, tmpfs, overlayfs and ZFS on Linux. Required native
capabilities are still checked during publication. Network filesystems,
FAT/exFAT, FUSE and unknown storage return `unsupported_filesystem` with guidance
to use a supported local working folder. This is an explicit support boundary,
not a claim of successful SMB/NFS or removable-drive acceptance. Cloud-sync
folders on a supported local filesystem remain subject to the external-writer
limitation above.

The three drafting tools return `record_status=disabled`: immutable legacy
counterproposal records do not describe mutation of an earlier revision.
Backups and per-call results support recovery, not a complete negotiation
history. No existing author names are bulk-renamed by this workflow.
