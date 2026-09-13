<!-- SPDX-License-Identifier: Apache-2.0 -->

# NR-02 deal positions contract

Development identity: `0.4.2.dev0`, surface-wide `veqtor.mcp.v0.4.2` (eleven
tools). Frozen releases and historical provenance goldens are unchanged.

`read_deal_positions(folder, include_history=false, check_sources=false)` reads
the entire current set, optionally the entire bounded history. No pagination or
hidden truncation. An absent snapshot returns `state=uninitialized`, null
revision and empty arrays without creating anything, including provenance.
`mutate_deal_positions(folder, expected_revision, operations)` accepts a closed,
nonempty atomic batch. `expected_revision=null` means snapshot absent; otherwise
it must equal the opaque revision returned by a read. Successful publication
returns `state=initialized`, the new revision and the complete current set.

A client chooses stable `pos_` plus 32 lowercase hex IDs. Closed content fields:
`title`, `desired_outcome`, nullable `fallback`, nullable `fallback_conditions`,
nullable `rationale`, `related_position_ids`, `content_origin` (`model_proposal`
or `user_instruction`), `business_decision` (`pending` or `not_required`), and
`sources`. All fields are explicit; empty nullable text is refused. Each source
contains a portable relative `path`, exact byte `file_sha256`, and nullable
`reference` (complete existing paragraph_ref.v1 or change_unit_anchor.v2).
Creation/update verifies source bytes and any reference before accepting it.
Source-free intentions/proposals are valid and are not documentary findings.

Operations: `create(position_id, content)`; `update(position_id,
expected_version, content)` replaces the complete content; `confirm(position_id,
expected_version, user_confirmed=true, statement)` records the client's explicit
assertion about that exact version; `withdraw(position_id, expected_version)`.
Only one operation per ID per batch; no deletion, resurrection or implicit
confirmation. All IDs linked by the resulting batch must exist in that matter;
self links and duplicates refuse. Links convey asserted relationships only.
Version starts at 1 and increments on every content update. Confirmation and
withdrawal are transitions of that content version. New/updated content is
unconfirmed regardless of origin; confirmation records version and statement.
Repeated confirmation/withdrawal refuses. Lifecycle is independently
`active`/`withdrawn`; business status remains independent of confirmation.
Every operation appends its complete resulting position, operation and sequence
to immutable history; earlier text and confirmation remain readable.

Authority is `.veqtor/deal-positions.json`, a closed `deal_positions_store.v1`
snapshot with portable random matter_id, opaque content-hash revision, current
positions and history. Revision hashes the complete snapshot except revision.
Copies are independent; neither absolute paths nor provenance identify a matter.
The optional decision journal is not the store: disabled, missing or corrupt
provenance cannot block storage/readback. These tools do not append provenance;
they return `record_status=disabled`. Existing export semantics stay unchanged.

Limits: 50 positions, 500 history transitions, 20 operations/batch, 10 linked IDs
and 5 sources/position; title 200 chars, outcome/fallback/conditions/rationale
4000 chars each, confirmation statement 2000 chars, relative paths 500 chars.
Snapshot/request JSON: at most 2 MiB UTF-8 each, depth 20 and 50000 nodes.
Source checks: at most 20 distinct files / 100 MiB total / 20 MiB per file per
call; reference inspection also obeys existing DOCX limits. A full result may
duplicate current/history and source status but is bounded by 8 MiB. Exceeding
any limit refuses without deleting history or altering the store.

An existing explicit absolute folder (optionally home-expanded) without dot
components is required. POSIX private metadata directories/files,
no symlink components, hardlinked/special store/source/lock files, or workspace
identity changes are allowed. Descriptors anchor I/O to the captured matter
and metadata directories. A stable `.veqtor/deal-positions.lock` inode is held
under bounded (2 second) nonblocking flock, including first creation. Revision
is checked while holding it. Reads use the same lock if initialized, without
creating files for an absent store. Locks must not be removed by clients.

Publication writes a private exclusive temporary file in the metadata directory,
flushes/fsyncs it, rechecks folder/lock/snapshot identity, and atomically replaces
the snapshot (commit point), then fsyncs the directory. Precommit failure leaves
the prior snapshot unchanged. Any failure once replacement may have occurred
returns `commit_uncertain`: reread revision before deciding what to do; never
blindly retry. Repeating the old expected_revision after a committed write
conflicts. No automatic corruption repair, truncation or initialization over an
invalid/unsupported store. A refused first mutation may leave only private
metadata/lock setup, never a successful empty store.

Read source observations are independent from saved position state: `same_bytes`
(hash checked), `changed`, `unavailable`, or `not_checked`. They cover current
and returned historical bindings. Source failure never suppresses intentions or
history; exhausted source budget yields not_checked for remaining observations.
No current-document inference, fuzzy rebinding or edits to DOCX. Saved text and
confirmation statements are untrusted matter data, not agent instructions or
authority to edit documents. Client assertions do not authenticate a person or
prove corporate/business approval. This is not a tamper-evident audit system.

Closed sanitized refusals: invalid_request, resource_limit_exceeded,
invalid_workspace, unsafe_storage, storage_corrupt, storage_unsupported,
storage_failure, lock_timeout, revision_conflict, position_conflict,
source_unverified, workspace_changed, commit_uncertain. No content/path leakage
in errors. Exact installed-producer native Codex acceptance has its own NR-02
profile: independent predeclared complete values, full structured save/update/
restart reads, fresh client and server sessions, history, concurrent conflicts
including initialization, relocation and source observations. Missing evidence
or prose/weaker substitutes cannot pass. Review precedes final package, full
suite, compatibility and native gates; unit tests do not claim those gates.
