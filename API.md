<!-- SPDX-License-Identifier: Apache-2.0 -->

# MCP Tool API

This file defines the public tool surface. Output examples are part of the API
because models use them to decide how to call tools and how to cite results.

Stable error codes cover well-typed but invalid inputs (wrong hash, unknown
anchor, blank quote, unresolvable layout). Type-level rejections — e.g. a
non-object `anchor` or a non-array `edits` sent over MCP — are handled by the
transport's schema validation before a tool runs and are outside that
contract. The current FastMCP transport may ignore unrecognized object
properties; clients must use the advertised tool schema. Strict rejection of
unknown arguments is outside v1.

M3 decision records are written by the server, not by the model. MCP tool calls
write a local JSONL sidecar in `.veqtor/decision-records.jsonl` inside the
existing matter folder, unless disabled by
`VEQTOR_DISABLE_DECISION_RECORD=1`. The sidecar is private (`0700` directory,
`0600` files). Before every append the server validates or restores
`.veqtor/.gitignore`; symlink, hardlink, non-regular, or unexpected ignore
targets are refused before the journal is touched.
The v1 journal is a best-effort local provenance history, not a transactional
audit log: mutation tools may complete with `record_status: "write_failed"`,
so check `record_status` before treating a record as durable. `written` means
the final record with its actual id passed the same bounded-JSON and record
schema checks used on read, was appended as a complete LF-terminated frame, and
the journal file fsync succeeded. Operations that create `.veqtor`, its
`.gitignore`, or the journal also require the relevant directory fsync. This is
not an absolute hardware power-loss guarantee. After low-level storage
failures, `write_failed` means the commit is unknown even if a partial frame
later appears on disk. Controlled fail-closed
`DocxError` refusals attempt to record and are then re-raised; FastMCP error
responses cannot echo their `record_id` in v1. Transport/type validation
errors never reach the tool wrapper and are not recorded. A corrupt journal
(`journal_corrupt`) fails closed on export or further append. The journal is a
sequence of non-empty JSON records, each terminated by one LF; blank frames and
any non-empty unterminated EOF fragment are corrupt. An unterminated fragment
is never completed or discarded automatically because its commit status is
unknown. Each stored JSONL record is limited to 1 MiB of UTF-8 JSON, 64 levels
of nesting, 100,000 JSON nodes, and 128 decimal digits per integer. Invalid
UTF-8 or JSON, duplicate object keys, non-finite numbers, invalid Unicode scalar
values, schema/digest failures, and bound violations are all classified as
`journal_corrupt` without echoing the damaged value. The line-size cap applies
to the stored compact/full record, not to the normalized full tool outcome
covered only by `tool_result_sha256`. Before append, the final record with its
lock-assigned id is serialized to one immutable frame. Those exact bytes pass
the same decoder, bounded-JSON checks, and schema validator used on read, and
the same bytes are appended without reserialization. A new record that read
would reject returns `record_status: "write_failed"` without failing the tool
or changing the journal. Callers must not mutate payload structures during
`write_record`; if they do, the selected snapshot is unspecified, but any
`written` frame remains internally consistent and readable. The current writer
admits only the MCP names in its writable allowlist and derives each
`record_type` from the permanent `decision_record.v1` historical tool spec. An
unknown tool or mismatched pair is `record_invalid` on write and
`journal_corrupt` on read. The five historical `(tool_name, record_type)` pairs
documented by this release, together with their compact projection rules, are
append-only v1 format commitments: a retired tool may leave the writable and
MCP surfaces but remains readable in full and compact exports. Existing pairs
must never be removed or retyped, and v1 read limits must not be narrowed;
incompatible changes require a new `schema_version`. Older servers may reject
records from tools added by a newer release, so downgrade compatibility is not
guaranteed.

Writer timestamps use exactly `YYYY-MM-DDTHH:MM:SSZ` or, when microseconds are
non-zero, `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Compact export returns a timestamp
verbatim only when it matches this grammar and round-trips through the same v1
formatter; other historical strings remain available in full mode but compact
mode emits `legacy-unvalidated` plus a digest.

The permanent pairs introduced by this release are:

- `list_rounds` → `tool_observation.v1`;
- `extract_redlines` → `tool_observation.v1`;
- `verify_quote` → `verification.v1`;
- `apply_edits` → `decision.v1`;
- `export_decision_record` → `access_event.v1`.

Move the damaged JSONL file aside to preserve it for inspection and let the
server start a new one. v1 records are re-verifiable through hashes and anchors
but are not tamper-evident. The v1 lock implementation uses POSIX `fcntl` and is
supported for the local macOS/Linux target. Its threat model is a local,
non-hostile single-user workspace: static unsafe targets and workspace
rebinding during open are refused, but hostile same-user filesystem mutation
is not a supported security boundary. Journal rotation and an aggregate
journal-size cap are outside v1.

## `list_rounds`

Call this when the user points to a folder of contract drafts or asks which
rounds/files are available in a negotiation.

Input:

```json
{
  "folder": "/Users/example/Deals/AcmeDistribution"
}
```

Output. Rounds are sorted by filename (the deterministic v1 round order);
Word lock files (`~$*`) are ignored, the scan is non-recursive, and files
that cannot be read as DOCX are reported in `skipped` instead of failing
the call:

```json
{
  "folder": "/Users/example/Deals/AcmeDistribution",
  "rounds": [
    {
      "round_id": "round-001",
      "path": "/Users/example/Deals/AcmeDistribution/01-initial.docx",
      "filename": "01-initial.docx",
      "sha256": "example",
      "revision_count": 12
    }
  ],
  "skipped": [],
  "record_id": "dr_001",
  "record_status": "written"
}
```

## `extract_redlines`

Call this when the user asks what changed in a DOCX, asks for tracked changes,
or needs anchors before applying a later edit.

Input:

```json
{
  "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx"
}
```

Output. `change_type` is `insert`, `delete`, `replace`, or `counter`; inserts
have `old_text: null`, deletes have `new_text: null`. `old_text`/`new_text`
are contiguous verbatim quotes of the prior/new reading. A `counter` unit is
one author's visible strike inside another author's still-pending insertion:
its `old_text` quotes the countered proposal (not contract text), and the
countered unit carries `countered_by` with the strike's revision ids.
`clause_anchor` is best-effort (`null` when the document offers no
outline/numbering signal; `label` is omitted rather than guessed when
numbering cannot be resolved reliably). Revision markup the tool does not
decode — formatting changes, moves, paragraph-mark revisions — is counted in
`unsupported_revisions`, never silently dropped. `revision_count` is the raw
number of `w:ins`/`w:del` elements in `word/document.xml`. The extractor and
decision-record projector consume one append-only v1 revision-category
contract, so every category the v1 producer can emit is accepted by compact
projection:

```json
{
  "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx",
  "file_sha256": "example",
  "part_name": "word/document.xml",
  "revision_count": 12,
  "change_units": [
    {
      "change_unit_id": "cu_001",
      "file_sha256": "example",
      "change_type": "replace",
      "author": "J. Smith",
      "date": "2026-07-01T09:00:00Z",
      "clause_anchor": {
        "label": "14.2",
        "heading": "Limitation of Liability"
      },
      "old_text": "fees paid in the previous 12 months",
      "new_text": "USD 50,000",
      "reference": {
        "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx",
        "part_name": "word/document.xml",
        "revision_ids": ["17", "18"]
      }
    }
  ],
  "unsupported_revisions": {"rPrChange": 1},
  "record_id": "dr_002",
  "record_status": "written"
}
```

## `verify_quote`

Call this before relying on a quotation in a memo, email, or negotiation summary.
Use anchors returned by `extract_redlines`.
`verdict` is one of `exact`, `normalized`, or `not_found`; `diff` explains any
non-exact result. v1 verifies against the anchored change unit's `new_text`
then `old_text` (`matches[].side` says which); matching is case-sensitive;
`normalized` collapses whitespace runs and typographic quotes/dashes. A hash
mismatch or unknown anchor is an error, never a verdict. Whole-document
search without an anchor is a later slice.

Input:

```json
{
  "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx",
  "anchor": {
    "change_unit_id": "cu_017",
    "file_sha256": "example",
    "part_name": "word/document.xml",
    "revision_ids": ["17", "18"]
  },
  "quote": "USD 50,000"
}
```

Output:

```json
{
  "verdict": "exact",
  "exact": true,
  "checked_anchor": {
    "change_unit_id": "cu_017",
    "file_sha256": "example"
  },
  "matches": [
    {
      "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx",
      "part_name": "word/document.xml",
      "revision_ids": ["17", "18"],
      "clause": "14.2 Limitation of Liability",
      "side": "new"
    }
  ],
  "diff": [],
  "record_id": "dr_003",
  "record_status": "written"
}
```

## `apply_edits`

Call this only after the user asks to prepare or apply counter wording and only
with an anchor produced by `extract_redlines`.

The server applies explicit edits only. Each edit must state the text range to
delete and the replacement text to insert. If the anchor is missing, ambiguous,
bound to a different file hash, or resolved to text that does not exactly match
`delete_text`, the tool returns an error and writes nothing.

`edits` are atomic: if any edit fails validation or application, no edits are
written and no output DOCX is left behind. If the round-trip check fails after
application, the tool returns an error and removes the failed output artifact.
The round-trip check compares OOXML structure outside touched anchor ranges; it
does not require byte-identical DOCX packages.

Three edit forms, all written as visible tracked changes — never silent
rewrites:

- plain replace/delete: `delete_text` lies in untouched text of the anchored
  clause;
- counter: `delete_text` lies entirely inside one counterparty pending
  insertion — written as a strike nested in their insertion (their proposal
  stays visible; extraction reports your `counter` unit and marks theirs
  `countered_by`), with the replacement inserted after theirs;
- reinstate: `{"anchor": ..., "reinstate_text": "..."}` restores text hidden
  inside exactly one counterparty deletion, as a visible insertion placed
  before it.

Several edits may target one paragraph; spans must not overlap (applied right
to left). Adjacent same-author markup merges in extraction, so layouts that
would leave two operations touching are refused with stable codes: a pending
insertion is countered once, with the full replacement (`already_countered`
on any later attempt); no edit may start immediately after a countered
insertion (`edits_overlap`); new markup is never written flush against our
own earlier tracked changes (`adjacent_to_own_revision`) — extend the
neighbouring edit to cover the extra text instead. This guarantee is not an
enumeration: before anything is written, the tool re-extracts the candidate
result, and any layout that would lose or alter a pre-existing change unit
is refused under `adjacent_to_own_revision` even where no specific rule
anticipated it. Spans mixing plain and tracked text, or lying in your own
pending insertion, return `overlaps_tracked_changes`. New revisions are
authored as `Veqtor MCP` and carry no `w:date` (deterministic output).

Input:

```json
{
  "source_path": "/Users/example/Deals/AcmeDistribution/12-current.docx",
  "output_path": "/Users/example/Deals/AcmeDistribution/13-our-counter.docx",
  "edits": [
    {
      "anchor": {
        "change_unit_id": "cu_017",
        "file_sha256": "example"
      },
      "delete_text": "USD 50,000",
      "insert_text": "The aggregate liability cap will equal the fees paid in the previous 12 months, excluding willful misconduct."
    }
  ]
}
```

Output:

```json
{
  "status": "ok",
  "output_path": "/Users/example/Deals/AcmeDistribution/13-our-counter.docx",
  "output_sha256": "example-output",
  "applied": [
    {
      "change_unit_id": "cu_017",
      "deleted_text": "USD 50,000",
      "inserted_text": "The aggregate liability cap will equal the fees paid in the previous 12 months, excluding willful misconduct.",
      "tracked_revision_ids": ["31", "32"]
    }
  ],
  "round_trip_check": {
    "status": "passed",
    "collateral_changes": [],
    "comparison": "ooxml_semantic_diff_outside_touched_anchors"
  },
  "record_id": "dr_004",
  "record_status": "written"
}
```

## `export_decision_record`

Call this when the user asks for an audit trail, negotiation record, or summary
of actions taken by the toolchain. v1 returns JSON records for the workspace;
the host model formats them. There is no PDF/CSV/DOCX export, filtering, or
search in v1.

The export reads `.veqtor/decision-records.jsonl` for the workspace and returns
chronological substantive records. Access events from `export_decision_record`
itself are recorded in the journal but omitted from the default export window.
To protect context and privacy, export is compact by default: verbatim `input`
payloads, paths, clause headings, raw error text and free-form provenance are
replaced by digests. Only format-validated identifiers, hashes and counters
observed by the server remain verbatim; client-asserted claims are digested,
and unused extra anchor fields are not journaled. Repeated facts are bounded
snapshots `{count, sha256, sample, truncated}`; samples contain at most 20
validated items while the digest covers the complete source collection. Large
extract results therefore stay bounded and re-verifiable.
Compact projection never trusts a stored snapshot merely because it already
has this shape. On every export, snapshot metadata is type-checked, every
sample item is projected through the same field allowlist as a raw item, and
the sample is bounded again. Extra fields are discarded and any filtering or
inconsistent count makes `truncated` true. For an already-bounded stored
snapshot, a projected item that differs from the stored item is filtering;
normal derivation of a raw value into an allowed digest field is not. Across
compact v1, the only scalar
values retained verbatim are documented enums, strict booleans, non-negative
counters, format-validated identifiers and timestamps, and SHA-256 values;
other strings are omitted, digested, or represented as `legacy-unvalidated`.
Only the current `producer.build` snapshot format remains verbatim; every older
or unrecognized value is replaced by `legacy-unvalidated` plus its digest in
compact output. Pre-release journals using older build markers are disposable
and are not migrated; archive or remove their `.veqtor` sidecar before a
pre-release demo to avoid mixing legacy and current markers.
Use `include_payload: true` only when the user explicitly needs full local
records as stored in the journal. An `extract_redlines` record is already a
summary and does not contain every old/new text. That explicit mode adds no
smaller response cap; stored entries remain subject to the per-record journal
boundary above and the response remains subject to `max_records`.
Responses are capped to the newest `max_records` entries (default 50). If
`truncated` is true, call again with
`before_record_id: next_before_record_id` to page earlier. `total_count` is the
number of substantive records visible in the current cursor window, not a
global all-time count; `access_count` reports export/access events in the
journal.

Input:

```json
{
  "workspace": "/Users/example/Deals/AcmeDistribution",
  "max_records": 50,
  "before_record_id": null,
  "include_payload": false
}
```

Output:

```json
{
  "workspace": {"sha256": "example-workspace-digest", "omitted": true},
  "total_count": 4,
  "access_count": 1,
  "truncated": false,
  "next_before_record_id": null,
  "records": [
    {
      "schema_version": "decision_record.v1",
      "record_type": "verification.v1",
      "record_id": "dr_001",
      "created_at": "2026-07-09T12:00:00Z",
      "tool_name": "verify_quote",
      "workspace": {"sha256": "example-workspace-digest", "omitted": true},
      "producer": {"name": "veqtor-mcp", "version": "0.0.0", "build": "source-snapshot-v1-sha256:..."},
      "payloads": "compact",
      "input": {"sha256": "example-input-digest", "omitted": true},
      "result": {
        "status": "ok",
        "verdict": "exact",
        "matches": {
          "count": 1,
          "sha256": "example-matches-digest",
          "sample": [{
            "revision_ids": {
              "count": 2,
              "sha256": "example-revision-id-digest",
              "sample": ["17", "18"],
              "truncated": false
            },
            "side": "new"
          }],
          "truncated": false
        }
      },
      "provenance": {
        "file_sha256": "example",
        "anchors": {
          "count": 1,
          "sha256": "example-anchor-digest",
          "sample": [{"change_unit_id": "cu_017"}],
          "truncated": false
        }
      },
      "result_sha256": "example-stored-result-digest",
      "tool_result_sha256": "example-full-tool-result-digest"
    }
  ],
  "record_id": "dr_005",
  "record_status": "written"
}
```

`result_sha256` fingerprints the stored record result (compact summaries for
tools such as `extract_redlines`), not the compact export projection;
`tool_result_sha256` fingerprints the normalized full tool outcome before
record compaction. These are content fingerprints for re-checking and
debugging, not tamper-evidence.

### Canonical JSON v1

`result_sha256`, `tool_result_sha256`, snapshot digests, and compact omission
digests use the project-specific `canonical_json_v1` algorithm. The normative
reference operation, after the documented depth, node, integer and Unicode
scalar validation, is:

```python
json.dumps(
    value,
    allow_nan=False,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
```

Object keys must be strings. Read-time duplicate keys are rejected. Keys are
sorted by Python `str` Unicode code-point order, not by UTF-16 code units.
Neither keys nor values receive NFC, NFD, or any other Unicode normalization.
Non-ASCII scalar values are emitted directly as UTF-8. Quote, reverse-solidus,
and control-character escaping is the Python 3.12 JSON encoding used by the
reference operation (`\b`, `\t`, `\n`, `\f`, `\r`, `\"`, `\\`, with the
remaining U+0000-U+001F values written as lowercase `\u00xx`).

Integers use ordinary decimal JSON notation and are limited to 128 digits.
Finite binary64 values use the exact representation emitted by the Python
3.12 JSON encoder, including its exponent spelling; the sign of `-0.0` is
retained. NaN and infinities are rejected. SHA-256 is calculated over exactly
the resulting UTF-8 bytes.

This is intentionally not RFC 8785 / JCS: JCS uses UTF-16 key ordering and
ECMAScript number serialization. Replacing canonical JSON v1 with JCS or any
other algorithm would invalidate historical digests and therefore requires a
new decision-record schema version.

The frozen composite conformance vector contains `-0.0`, every short control
escape, U+0000, distinct NFC/NFD keys, and U+E000/U+10000 keys. Its canonical
JSON ends with the U+E000 key before U+10000 and has SHA-256
`d2d566113618f299e9638c9b6ecdc13b2a29e3bc7adb9cf8993a95bb7bed42cf`.
The exact input and expected bytes are committed in
`test_v1_canonical_digest_vectors_are_frozen`; external implementations must
match the frozen vectors as well as this algorithm. A separate frozen vector
for U+000B, U+000E, U+000F, U+001A, U+001B, U+001E and U+001F fixes lowercase
hex spelling for non-short control escapes; its SHA-256 is
`e7809d1f4b2bb2e50b32a947d4fca6753d869cf164157806f200d11e2f4d18a7`.

`producer.build` is an eager process-start `source-snapshot-v1-sha256` over
Python source files under the imported `veqtor_mcp` and `veqtor_docx` package
roots. Any discovery, enumeration, read, manifest-serialization, or digest
failure yields `source-snapshot-unavailable`; partial digests are never
presented as complete. This is not a source-commit or Python bytecode identity.
Raw package roots and source entries must not be symlinks; the digest input is a
canonical `source_snapshot.v1` manifest of sorted `{path, sha256}` entries.
