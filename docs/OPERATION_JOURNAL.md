<!-- SPDX-License-Identifier: Apache-2.0 -->

# Drafting operation journal

A drafting save uses two private local records, separate from the older
`decision-records.jsonl` provenance log. Records describe requested actions and
observed file states; they do not authenticate a person or prove user approval.
No complete document or chat transcript is copied into these records.

## Sequence and proof migration

`VEQTOR_OPERATION_JOURNAL=required` is the server default. A successful dry run
returns `drafting_preflight.v2`, including a UUID `operation_id` bound with the
source, destination, complete operations, selected profile, timestamp, build and
candidate hash. The dry run writes no document, backup or operation record.
Keep this ID before calling apply so a lost response can be recovered.

Under the existing document writer locks, apply obtains the exact backup for an
in-place save. It then durably publishes `prepared.json`, revalidates the source
and backup, and publishes the candidate. The prepared record retains the closed
`round_operation_intent.v1`, canonical requested operations, observed previews,
proof, version, source path and allocated backup. This includes original revision
authors present in the previews. It does not invent legal reasons or user consent.

Apply observes the actual saved output and backup hashes, then publishes
`result.json`, wrapping `round_operation_result.v1` with a timestamp and a
`recovered` flag. Publication, file verification and journal status are separate.
No joint transaction with the positions store is claimed.

Explicit transitional `VEQTOR_OPERATION_JOURNAL=disabled` preserves v1 proofs and
unjournaled library behavior. V1 is refused in required mode; v2 cannot be applied
with journaling disabled. Changing mode requires a fresh preflight. This switch
never changes the legacy `VEQTOR_DISABLE_DECISION_RECORD` setting. `doctor`
reports both settings separately. Disabled mode cannot satisfy final F1 acceptance.
Direct library callers retain the old default and opt in with `journal=True`.

## Outcomes and recovery

- Failure to commit preparation prevents document publication. A backup may
  already exist; it is retained. There is no successful result record.
- A published and verified document with a failed result-record write reports
  `record_status=write_failed`, with `publication_status=published` and
  `journal_status=incomplete`. The document must not be applied again.
- A post-publication observation or durability failure retains the known
  publication fact and structured recovery guidance instead of claiming no save.
- `recover_document_operation(source_path, operation_id)` never applies edits.
  Matching candidate and exact backup hashes can complete a lost result with
  `recovered=true`. A copy additionally requires its source still to match.
- Missing, changed, unreadable or ambiguous evidence stays unresolved. In
  particular, identical source/candidate hashes cannot prove a lost publication.
  Recovery does not restore backups, move files or search other folders.
- A valid existing result is immutable. Recovery reports it with a separate
  current output hash; later editing does not rewrite the historical result.
  Reusing a prepared ID cannot publish another candidate.

When save journaling completes, `record_id` is the operation UUID and
`record_status=written`. Reads and preflight still have null/disabled record
metadata. The legacy exporter returns legacy records only; it does not claim to
export this separate journal or a complete negotiation round.

## Storage and limits

Records live in the selected source folder at
`.veqtor/operations/<operation_id>/prepared.json` and `result.json`. New
folders are private (`0700`), records are private (`0600`), and the sidecar is
ignored by Git. Existing legacy records are not rewritten or migrated.
Operation records are create-only; unknown versions and invalid, aliased or
inconsistent records refuse. Keep operation folders during program rollback.
Older program versions are not promised to understand the new format.

The plan is limited to 1 MiB, 20,000 JSON nodes and depth 32. Records add at most
64 KiB of envelope overhead; decoding has bounded nodes/depth and rejects duplicate
keys and non-finite numbers. File observations retain the existing 50 MiB compressed
DOCX limit. Supported local filesystems, safe paths, owner/permission checks,
Word owner locks and source-change checks still apply. An uncooperative external
writer cannot be made part of an atomic compare-and-swap protocol.

No new database, cloud transport, sender-state inference, outgoing package or
complete round manager is introduced by this journal.
