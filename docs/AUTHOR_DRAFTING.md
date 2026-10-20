<!-- SPDX-License-Identifier: Apache-2.0 -->
# Author drafting in one working document

The `0.6.0.dev1` source candidate extends the three author-drafting tools added
in `0.5.0` with paragraph insertion, complete paragraph replacement, comment
replies and thread state. Edits use the selected author's name and save back to
the same path with an exact backup. The legacy counterproposal tools keep their
original separate-output and revision-preservation contract.

This is an unreleased development contract. Source changes and synthetic
package checks do not establish installed-client acceptance, native Word
save/reopen behavior or publication. Publication requires the matching verified
PyPI and GitHub release.

## Author profile

Configure the default `VEQTOR_TRACKED_CHANGE_AUTHOR` and optional
`VEQTOR_COMMENT_INITIALS` in the MCP server environment, then restart that server.
For example:

```toml
[mcp_servers.veqtor.env]
VEQTOR_TRACKED_CHANGE_AUTHOR = "Shilov, Ilya"
VEQTOR_COMMENT_INITIALS = "IS"
```

The name is a Word revision/comment author string. The server does not sign in
to that person's account or change Word's own preferences. Match the author in
Word on each computer to continue native editing with that name. Initials
default to the name's initials (given name first for `Family, Given`). The
default profile is fixed for a server process. The Claude Desktop extension asks
for both author and initials during setup.

To choose another author for one batch without restarting the server, supply
`author_profile={"name":"Shilov, Ilya","initials":"IS"}` to both preflight and apply.
Both fields are required; extra fields are refused. The explicit profile affects
only that request. Omitting it, or passing `null`, uses the configured default.
Every successful preflight reports the resolved name and initials, and its proof
binds both. Apply must resolve to the same identity.

Names are matched exactly when deciding whether an existing insertion or comment
belongs to the selected author. Selecting a profile does not rename earlier
revisions, establish account ownership or make another person's Word session
use that name.

## Tool sequence

1. Read current text and obtain complete `paragraph_ref.v1` references with
   `inspect_document`. Read comments and `file_sha256` with `read_document_comments`.
2. Call `preflight_document_edits(source_path, file_sha256, operations,
   output_path=None, author_profile=None)`. A successful response contains `batch_applicable=true`
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
{"kind":"replace_paragraph","paragraph_ref":{},"old_text":"Payment is due within 30 days.","new_text":"Payment is due within 45 days."}
{"kind":"insert_paragraph","paragraph_ref":{},"position":"after","text":"Disputed amounts remain payable once resolved."}
{"kind":"add_comment","paragraph_ref":{},"quote":"45 days","text":"Please confirm timing."}
{"kind":"update_comment","comment_id":"3","text":"Timing agreed."}
{"kind":"reply_comment","comment_id":"3","text":"Agreed, subject to receipt of a valid invoice."}
{"kind":"set_comment_resolved","comment_id":"3","resolved":true}
{"kind":"delete_comment","comment_id":"3"}
```

Replace `{}` with the complete reference from inspection. Operations execute
in the listed order against one in-memory candidate. References identify the
original snapshot's paragraphs; exact text matches address the current reading
at that step. Thus a batch can replace text and then comment on the replacement.
An empty `new_text` deletes the matched text. A whole-paragraph replacement
requires `old_text` to equal the complete current reading, including earlier
operations in the batch; it preserves the paragraph itself and its formatting.
An empty replacement therefore leaves the paragraph mark in place. An empty
`old_text` can fill a blank paragraph. Supported whole-paragraph edits may span
plain text and the selected author's simple pending insertions: plain text is
deleted with tracking, own insertions are edited or removed, and earlier hidden
deletions remain. Visible foreign insertions and interior range/comment anchors
are refused for whole-paragraph replacement.

`insert_paragraph` adds one tracked paragraph before or after the referenced
paragraph. Source references continue to identify their original paragraphs
even when an earlier operation inserts a neighbor. A newly inserted paragraph
needs fresh inspection after saving before it can be addressed by its own
reference. Insertion supports direct body or table-cell paragraphs and inherits
the anchor's clean paragraph properties, including numbering, and the first
supported visible run's current character formatting. The text must be one
nonempty paragraph, without tabs or line breaks. Section/frame properties,
pending paragraph-property revisions, unsupported paragraph properties, and an
insertion boundary inside an existing comment/bookmark/permission range are
refused. Paragraph insertion includes tracked paragraph-boundary markup as
well as tracked text.

Repeated insertions on the same side of one source paragraph retain operation
order: inserting A then B before it gives A, B, anchor; inserting A then B after
it gives anchor, A, B.

Text operations do not move paragraphs, apply arbitrary formatting or accept
existing revisions.
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
  consult the current timezone. Modern comment extensions used for threads are
  separate from the legacy annotation-date display.
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
`next_offset`. Each comment also reports `parent_comment_id` (null for a root),
`thread_root_id`, and the root thread's `resolved` state. `resolved=null` means
existing modern metadata does not provide a mapped state for that root; it is
not an assertion that the thread is open. Replies inherit the root comment's
paragraph indexes. All pages must have the same file hash.

Add comments to a unique exact quote in supported simple text or one own
insertion. Updating/deleting requires exact author equality. Other authors'
comments remain readable; replying to them does not change their author or body.
Existing simple comment updates, including supported own replies, retain their
identity, author, date, initials and paragraph metadata.

`reply_comment` adds a new comment under the selected author and initials.
It may target any author's comment or reply. A reply to a reply joins that
comment's root thread, following Word's flat thread representation. The reply
gets its own document anchors over the root comment's
same quote, preserving the root's anchors and text. Replying to a resolved thread returns
`comment_thread_resolved`; reopen it explicitly before replying.

`set_comment_resolved` sets `resolved=true` or `false` for the root thread,
whether its `comment_id` identifies the root or a reply. It can act on any
author's thread, preserves all comment bodies/authors, and does not create an
authored revision: Word's thread-state metadata has no separate actor field.

Classic comments can acquire the metadata needed for a thread. Existing modern
comment metadata is resolved through package relationships, including
nonstandard internal names. Cycles, duplicate IDs, dangling replies, ambiguous
or unbound metadata, and unsupported task/extension payloads refuse mutation.
Deleting a threaded comment or reply remains unsupported. Deleting a supported
own unthreaded comment removes its matching metadata; rich or multiparagraph
comment-body replacement remains unsupported. Headless rendering often omits
comments; package relationships and range anchors need separate checks, and
native Word thread behavior requires save/reopen validation.
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
