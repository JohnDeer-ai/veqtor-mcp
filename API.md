<!-- SPDX-License-Identifier: Apache-2.0 -->

# MCP Tool API

This file defines the public tool surface. Output examples are part of the API
because models use them to decide how to call tools and how to cite results.

The development source is package `0.3.0.dev0` and advertises draft MCP
contract `veqtor.mcp.v0.3`. Package version, contract and publication status
are separate identities: only matching entries on public PyPI and the
immutable GitHub Releases list establish distribution. This file alone does
not. Every
tool exposes `veqtor.pro/contractSchemaVersion: veqtor.mcp.v0.3` in its MCP
metadata and the same value under `x-veqtor-contract-schema-version` in its
output schema. Nested anchors, edits and preflight proofs are closed objects;
top-level results remain additive where the advertised schema says so.

Stable error codes cover well-typed but invalid inputs (wrong hash, unknown
anchor, blank quote, unresolvable layout). Type-level rejections — e.g. a
non-object `anchor` or a non-array `edits` sent over MCP — are handled by the
transport's schema validation before a tool runs and are outside that
contract. The current FastMCP transport may ignore unrecognized object
properties; clients must use the advertised tool schema. Strict rejection of
unknown top-level arguments remains outside the current contract.

All DOCX-reading tools share one two-stage fail-closed ZIP boundary. Before any
decoder is created, Veqtor bounds the 50 MiB compressed input, 2,000 members,
4 MiB central directory and every declared member/aggregate size, rejects
duplicates, encryption, ZIP64 and compression methods other than `STORED` and
`DEFLATED`, and reconciles the security-relevant central and local fields and
layout. It then validates every member from the immutable input snapshot:
DEFLATED data uses bounded input and output chunks, while STORED data is a
bounded direct span. Both paths count actual output and CRC, and DEFLATE also
requires an exact DEFLATE end of stream. Standard 32-bit data descriptors with
and without their optional signature are accepted only when their CRC, sizes and
boundary agree exactly. Gaps, overlaps, prefixes, trailing compressed data and
disagreement in the raw name, flags, method, CRC or sizes are refused.
Well-formed non-ZIP64 local and central extra fields may differ because they do
not select the decoded member bytes.

The per-package expanded envelope remains 100 MiB total, 25 MiB per XML member,
50 MiB per other member, and at most 200:1 compression for a member larger than
10 MiB. One `list_rounds` call additionally has a 500 MiB aggregate budget for
actual expanded member output across the complete folder scan.
Any XML part Veqtor parses is limited to 100,000 structural items: elements,
attributes, namespace declarations, comments and processing instructions;
extraction is limited to 10,000 change units. Generated candidates must remain
within the same 50 MiB compressed-size envelope. XML `DOCTYPE` declarations are
refused rather than loading DTDs or expanding entities. Tracked text revisions
may be nested at most two levels, preserving the normal
insertion-plus-nested-deletion counter shape without allowing recursive text
amplification.

Limit violations use `resource_limit_exceeded`; a forbidden ZIP method uses
`unsupported_compression`, encryption uses `encrypted_docx`, and structural,
CRC, descriptor or end-of-stream disagreement uses `file_unextractable`. The
Python API exception metadata identifies the safe limit or member measurement;
the MCP error text exposes the stable code and safe detail. `list_rounds`
preserves per-file resource, compression and encryption reasons in `skipped`
and maps structural archive failures to `invalid_docx`, while direct tools
refuse the operation with their corresponding code. Exceeding the shared
folder-scan output budget refuses the whole call instead of returning a partial
round list.

M3 decision records are written by the server, not by the model. MCP tool calls
write a local JSONL sidecar in `.veqtor/decision-records.jsonl` inside the
existing matter folder, unless disabled by
`VEQTOR_DISABLE_DECISION_RECORD=1`. The sidecar is private (`0700` directory,
`0600` files). Before every append the server validates or restores
`.veqtor/.gitignore`; symlink, hardlink, non-regular, or unexpected ignore
targets are refused before the journal is touched.
Read-only calls — list, extract, inspect, verify, preflight and decision-record
export — also normally attempt to append provenance, with the outcome reported in
`record_status`. In particular, export is a read of the document history but
normally writes its own local `access_event.v1` after the response snapshot.
Export requires an existing journal in the exact supplied workspace; it never
initializes an empty sidecar merely because the caller supplied a parent or an
uninitialized folder.
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
errors never reach the tool wrapper and are not recorded. Unexpected failures
anywhere in workspace resolution, the core call, provenance projection,
journal publication or response construction are journaled as a generic
`internal_error` when a safe workspace exists, then replaced with a
context-free MCP error. Their exception type, message, document content and
local paths are never returned to the client. A corrupt journal
(`journal_corrupt`) fails closed on export or further append. The journal is a
sequence of non-empty JSON records, each terminated by one LF; blank frames and
any non-empty unterminated EOF fragment are corrupt. An unterminated fragment
is never completed or discarded automatically because its commit status is
unknown. Each stored JSONL record is limited to 1 MiB of UTF-8 JSON, 64 levels
of nesting, 100,000 JSON nodes, and 128 decimal digits per integer. Invalid
UTF-8 or JSON, duplicate object keys, non-finite numbers, invalid Unicode scalar
values, schema/digest failures, and bound violations are all classified as
`journal_corrupt` without echoing the damaged value. The line-size cap applies
to the stored journal record, not to the normalized full tool outcome
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
`journal_corrupt` on read. The seven historical `(tool_name, record_type)` pairs
documented by this source contract, together with their compact
projection rules, are
append-only v1 format commitments: a retired tool may leave the writable and
MCP surfaces but remains readable through raw local and compact reads. Existing
pairs must never be removed or retyped, and v1 read limits must not be narrowed;
incompatible changes require a new `schema_version`. Older servers may reject
records from tools added by a newer release, so downgrade compatibility is not
guaranteed.

Writer timestamps use exactly `YYYY-MM-DDTHH:MM:SSZ` or, when microseconds are
non-zero, `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Compact export returns a timestamp
verbatim only when it matches this grammar and round-trips through the same v1
formatter; other historical strings remain available through raw local reads,
while compact mode emits `legacy-unvalidated` plus a digest.

The permanent pairs registered by this source contract are:

- `list_rounds` → `tool_observation.v1`;
- `extract_redlines` → `tool_observation.v1`;
- `inspect_document` → `inspection.v1`;
- `verify_quote` → `verification.v1`;
- `preflight_edits` → `verification.v1`;
- `apply_edits` → `decision.v1`;
- `export_decision_record` → `access_event.v1`.

Each existing pair remains forward-compatible for readers that know it. Once a
journal contains a tool unknown to an older reader, downgrade to that reader is
unsupported; it fails closed rather than skipping the unknown historical pair.

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
rounds/files are available in a negotiation. The optional `ordered_filenames`
array is an explicit positional manifest: when present it must name every
candidate DOCX exactly once, with no duplicate, missing or extra name. Invalid
manifests fail closed with `invalid_round_order`; Veqtor never silently falls
back to filename order.

Input:

```json
{
  "folder": "/Users/example/Deals/AcmeDistribution",
  "ordered_filenames": ["01-initial.docx", "02-counterparty.docx"]
}
```

Omit `ordered_filenames` to use `filename_lexicographic_v1`: case-insensitive
lexicographic filename order with the exact filename as a tie-break. This is not
natural-number order (`Round 10` can precede `Round 2`), so zero-pad filenames or
provide a complete manifest when positional order matters. Both modes disclose
`lineage_verified: false` and `round_id_semantics: "position_only"`; neither
order is evidence of chronology, legal sequence or document lineage.
`revision_count_basis` is
`word_document_xml_w_ins_w_del_elements_v1`: every per-file
`revision_count` is the raw number of `w:ins` and `w:del` elements in
`word/document.xml`. It is neither a logical-edit count nor the broader
`revision_inventory.total_revision_elements` reported by extraction.

Word lock files (`~$*`) are ignored, the scan is non-recursive, and files
that cannot be read as DOCX are reported in `skipped` with a stable reason code
instead of failing the call. Unexpected implementation failures are not
converted into successful skips and never expose their exception text. One
scan accepts at most 500 candidate DOCX files, 500 MiB of aggregate candidate
file size at scan time and 500 MiB of aggregate actual expanded output.
DEFLATED decoder output and STORED direct-span bytes are charged, including
output from a file that is later reported in `skipped`; a package refused during
container preflight before any member-output processing consumes no output
budget.
Exceeding a folder-level bound refuses the whole call with
`resource_limit_exceeded`, returns no partial round list and advises splitting
the folder before retrying:

```json
{
  "folder": "/Users/example/Deals/AcmeDistribution",
  "ordering_source": "explicit_filename_sequence_v1",
  "order_basis": {
    "kind": "caller_supplied_filename_sequence",
    "lineage_verified": false,
    "round_id_semantics": "position_only"
  },
  "revision_count_basis": "word_document_xml_w_ins_w_del_elements_v1",
  "rounds": [
    {
      "round_id": "round-001",
      "path": "/Users/example/Deals/AcmeDistribution/01-initial.docx",
      "filename": "01-initial.docx",
      "sha256": "example",
      "revision_count": 12
    },
    {
      "round_id": "round-002",
      "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx",
      "filename": "02-counterparty.docx",
      "sha256": "example",
      "revision_count": 8
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
numbering cannot be resolved reliably). Computed numbering templates, rendered
labels and explicit manual labels are each capped at 256 characters; Roman
counters are rendered
only for values 1-3999. Values outside those navigation-only boundaries omit
the computed label without blocking extraction. Revision markup the tool does not
decode — formatting changes, moves, paragraph-mark revisions — is counted in
`unsupported_revisions`, never silently dropped. `revision_count` is the raw
number of `w:ins`/`w:del` elements in `word/document.xml`. The extractor and
decision-record projector consume one append-only v1 revision-category
contract, so every category the v1 producer can emit is accepted by compact
projection.

`revision_inventory.v2` classifies the same immutable XML snapshot under
`canonical_body_flow_v1`. Its scope is `word/document.xml`, and a valid result
satisfies both closed partitions:

```text
total_revision_elements
  = in_scope_revision_elements + excluded_container_occurrences
in_scope_revision_elements
  = decoded_revision_elements + unsupported_revision_occurrences
unsupported_revision_occurrences
  = sum(unsupported_by_kind.values())
excluded_container_occurrences
  = sum(excluded_by_container.values())
unsupported_revision_kind_count
  = len(unsupported_by_kind)
excluded_container_kind_count
  = len(excluded_by_container)
```

`all_in_scope_revision_elements_decoded` is true exactly when unsupported
in-scope occurrences are zero. `all_revision_elements_decoded` additionally
requires zero excluded-container occurrences. `emitted_change_unit_count` is
deliberately outside both partitions: one change unit can group multiple
decoded `w:ins`/`w:del` elements. The legacy `unsupported_revisions` mapping
remains and equals `revision_inventory.unsupported_by_kind` for the current
producer.

Every emitted unit carries a closed `change_unit_anchor.v2`. It binds
`change_unit_id` and `file_sha256` to `canonical_body_flow_v1` and to a SHA-256
fingerprint of all public unit facts except its local path. The unit reference
also discloses `container_kind: body | table_cell`. The old two-field anchor is
accepted only when container analysis reports
`legacy_two_field_anchor_safe: true`; otherwise anchor-consuming tools refuse
it with `legacy_anchor_ambiguous` rather than reinterpret an ordinal under a
different traversal policy.

Every unit also includes bounded context from the paragraph's
accepted/current reading: at most 240 characters before and after the unit,
truncation flags, and a conservative `manual_label` only when the paragraph
itself begins with a dotted label such as `5.2` or `2.1A`. This label is
independent from the nearest-heading `clause_anchor` and is `null` rather than
guessed:

```json
{
  "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx",
  "file_sha256": "example",
  "part_name": "word/document.xml",
  "revision_count": 2,
  "revision_count_basis": "word_document_xml_w_ins_w_del_elements_v1",
  "change_units": [
    {
      "change_unit_id": "cu_001",
      "file_sha256": "example",
      "anchor": {
        "schema_version": "change_unit_anchor.v2",
        "change_unit_id": "cu_001",
        "file_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "container_policy": "canonical_body_flow_v1",
        "unit_fingerprint_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      },
      "change_type": "replace",
      "author": "J. Smith",
      "date": "2026-07-01T09:00:00Z",
      "clause_anchor": {
        "label": "14.2",
        "heading": "Limitation of Liability"
      },
      "paragraph_context": {
        "before": "...",
        "after": "...",
        "manual_label": "14.2",
        "truncated_before": false,
        "truncated_after": false
      },
      "old_text": "fees paid in the previous 12 months",
      "new_text": "USD 50,000",
      "reference": {
        "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx",
        "part_name": "word/document.xml",
        "paragraph_index": 42,
        "container_kind": "body",
        "group_index": 0,
        "revision_ids": ["17", "18"]
      }
    }
  ],
  "unsupported_revisions": {"rPrChange": 1},
  "revision_inventory": {
    "schema_version": "revision_inventory.v2",
    "scope": "word/document.xml",
    "container_policy": {
      "schema_version": "canonical_body_flow_v1",
      "indexed_paragraph_count": 73,
      "body_paragraph_count": 65,
      "table_cell_paragraph_count": 8,
      "excluded_subtree_count": 0,
      "excluded_paragraph_count": 0,
      "excluded_by_kind": {},
      "excluded_paragraphs_by_kind": {},
      "coverage_complete": true,
      "legacy_two_field_anchor_safe": true
    },
    "tracked_text_revision_elements": 2,
    "total_revision_elements": 3,
    "in_scope_revision_elements": 3,
    "decoded_revision_elements": 2,
    "unsupported_revision_occurrences": 1,
    "unsupported_revision_kind_count": 1,
    "excluded_container_occurrences": 0,
    "excluded_container_kind_count": 0,
    "emitted_change_unit_count": 1,
    "unsupported_by_kind": {"rPrChange": 1},
    "excluded_by_container": {},
    "partition_valid": true,
    "all_in_scope_revision_elements_decoded": false,
    "all_revision_elements_decoded": false
  },
  "record_id": "dr_002",
  "record_status": "written"
}
```

If the DOCX bytes are readable but extraction fails while decoding the OOXML,
the controlled failure record carries `observed_source_sha256` for that exact
snapshot. Malformed numeric style, numbering or paragraph properties do not
escape as raw Python conversion errors.

`paragraph_index` and `group_index` are zero-based structural locators inside
the hash-identified `word/document.xml` snapshot. Apply resolves a unit by
those positions and verifies its complete extracted fingerprint before text
matching. `revision_ids` remain provenance only: OOXML producers may duplicate
or move `w:id` values, so Veqtor never uses an id alone as the edit address.

## `inspect_document`

Call this to find and read unchanged wording in the main body of one DOCX
without asking the model to ingest an unbounded whole-document dump. The tool
does not decide what a clause means. It exposes deterministic navigation,
literal discovery and hash-bound paragraph evidence under the contract defined
in [`INSPECT_DOCUMENT_V0.3.md`](INSPECT_DOCUMENT_V0.3.md).

Input:

```json
{
  "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx",
  "mode": "literal_search",
  "phrases": ["aggregate liability"],
  "match_basis": "normalized_casefold_literal",
  "max_items": 50
}
```

The four modes are closed:

- `outline` pages structural headings and section references without paragraph
  body text;
- `literal_search` searches 1-20 explicit phrases independently inside each
  supported non-empty paragraph and returns a bounded snippet plus a paragraph
  reference;
- `browse` pages complete non-empty supported paragraphs in canonical order;
- `read` re-resolves exactly one `paragraph_ref.v1` or `section_ref.v1` from an
  earlier result. A paragraph read returns one complete paragraph and rejects a
  cursor; a section read may paginate its non-empty member paragraphs.

`literal_search.match_basis` is exactly `exact_literal`,
`normalized_literal`, or `normalized_casefold_literal`. The first is verbatim
and case-sensitive. The second only collapses whitespace and normalizes the
documented typographic quotes/dashes. The third additionally applies
locale-independent Unicode case folding. Search never becomes fuzzy, semantic,
regular-expression or cross-paragraph search.

All modes disclose:

```text
search_scope: word_document_xml_body_v1
reading_mode: accepted_current_v1
container_policy: canonical_body_flow_v1
```

The canonical body flow indexes supported body paragraphs, SDT content and
table-cell paragraphs once in XML order. It prunes text boxes, drawings,
`AlternateContent`, nested paragraphs, unknown text-bearing containers and
malformed text/revision payload inside otherwise non-text Word property
subtrees.
Headers, footers, footnotes, endnotes and comments are separate excluded OPC
parts. `accepted_current_v1` is a mechanical current-reading projection:
inserted/move-to text is included and deleted/move-from text is excluded. It is
not a legal conclusion that the result is operative wording.

Every returned paragraph carries a closed, path-free reference:

```json
{
  "schema_version": "paragraph_ref.v1",
  "ref_type": "paragraph",
  "file_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "part_name": "word/document.xml",
  "paragraph_index": 42,
  "paragraph_text_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "reading_mode": "accepted_current_v1",
  "container_policy": "canonical_body_flow_v1"
}
```

The file hash, canonical position, exact UTF-8 paragraph-text hash and policy
must all re-resolve. A `section_ref.v1` similarly binds the file and policy to
one outline heading's paragraph position and text hash; it identifies a
recomputed structural range, not semantic clause identity or cross-round
lineage.

Word outline levels 0-8 create structural sections; level 9 is body text.
Direct or style-derived values outside 0-9 refuse with `file_unextractable`
before a successful inspection record can be written.

Example literal-search output (record metadata, repeated reference fields and
inventory abbreviated):

```json
{
  "mode": "literal_search",
  "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx",
  "file_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "part_name": "word/document.xml",
  "search_scope": "word_document_xml_body_v1",
  "reading_mode": "accepted_current_v1",
  "container_policy": "canonical_body_flow_v1",
  "has_tracked_text_revisions": true,
  "revision_inventory": {"schema_version": "revision_inventory.v2"},
  "match_basis": "normalized_casefold_literal",
  "phrase_count": 1,
  "matches": [
    {
      "phrase_index": 0,
      "match_basis": "normalized_casefold_literal",
      "occurrence_count": 1,
      "paragraph_ref": {"schema_version": "paragraph_ref.v1"},
      "container_kind": "body",
      "has_tracked_text_revisions": true,
      "section_navigation": {
        "label": "14.2",
        "heading": "Limitation of Liability",
        "level": 1,
        "basis": "word_outline_level_v1",
        "label_basis": "word_numbering_v1"
      },
      "snippet": {
        "text": "...aggregate liability...",
        "match_start": 3,
        "match_end": 22,
        "truncated_before": true,
        "truncated_after": true
      }
    }
  ],
  "coverage": {
    "scan_complete": true,
    "body_paragraph_count": 73,
    "nonempty_body_paragraph_count": 70,
    "eligible_item_count": 1,
    "returned_item_count": 1,
    "cursor_offset": 0,
    "output_truncated": false,
    "complete_literal_match_count": 1,
    "included_parts": ["word/document.xml"],
    "excluded_parts": [
      "word/header*.xml",
      "word/footer*.xml",
      "word/footnotes.xml",
      "word/endnotes.xml",
      "word/comments*.xml"
    ],
    "included_containers": ["body", "table_cell"],
    "container_coverage": {
      "schema_version": "canonical_body_flow_v1",
      "indexed_paragraph_count": 73,
      "body_paragraph_count": 65,
      "table_cell_paragraph_count": 8,
      "excluded_subtree_count": 0,
      "excluded_paragraph_count": 0,
      "excluded_by_kind": {},
      "excluded_paragraphs_by_kind": {},
      "coverage_complete": true,
      "legacy_two_field_anchor_safe": true
    }
  },
  "limits": {
    "requested_max_items": 50,
    "max_items": 100,
    "max_phrases": 20,
    "max_phrase_chars": 2000,
    "max_total_phrase_chars": 10000,
    "max_paragraph_text_chars": 50000,
    "max_returned_text_chars": 100000,
    "max_indexed_paragraphs": 10000,
    "max_aggregate_text_chars": 2000000,
    "max_literal_match_candidates": 10000,
    "max_literal_occurrences_per_candidate": 10000,
    "wall_clock_partial_results": false
  },
  "next_cursor": null,
  "record_id": "dr_003",
  "record_status": "written"
}
```

`scan_complete: true` means the complete declared main-body scope was
classified before success. `output_truncated` only means another explicit
cursor page exists; elapsed time never selects a successful partial result.
The opaque cursor is bound to its cursor schema, the file SHA, search/reading/
container policies, the applicable match and ordering policies, the complete
request, and the SHA-256 of the complete ordered eligible result set.
`max_items` is deliberately excluded so the caller may change the next page
size without changing result identity. A stale or different request, policy,
or result set is `cursor_mismatch`; malformed or out-of-range cursors are
`invalid_cursor`. Deterministic paragraph, aggregate-text, phrase, candidate
and response caps fail closed with `resource_limit_exceeded`.

The call appends `(inspect_document, inspection.v1)` provenance when local
journaling succeeds. Compact export omits paths and paragraph/snippet text,
retains the source hash, mode, policy and coverage facts, and returns a bounded
digest/sample of observed paragraph or section references. It remains
best-effort local provenance, not a tamper-evident audit log.

## `verify_quote`

Call this before relying on a quotation in a memo, email, or negotiation summary.
Use a legacy/v2 change-unit anchor returned by `extract_redlines`, or a
`paragraph_ref.v1` returned by `inspect_document`.
`verdict` is one of `exact`, `normalized`, or `not_found`; `diff` explains any
non-exact result. A change-unit anchor checks `new_text` then `old_text`
(`matches[].side` is `new` or `old`). A paragraph reference checks the complete
`accepted_current_v1` paragraph and reports `side: paragraph_current`, an empty
`revision_ids` list, the paragraph index/text hash and reading mode. Matching
is case-sensitive; `normalized` collapses whitespace runs and typographic
quotes/dashes. A hash, policy, fingerprint or reference mismatch is an error,
never a verdict. Whole-document search without an anchor remains unsupported.

All accepted anchors are closed. A legacy change-unit anchor contains exactly
`change_unit_id` and `file_sha256`. A `change_unit_anchor.v2` additionally
requires its schema version, `container_policy` and
`unit_fingerprint_sha256`. A `paragraph_ref.v1` requires the exact eight fields
documented under `inspect_document`. Additional or partial fields refuse with
`invalid_anchor` rather than being silently ignored. A legacy two-field anchor
also refuses with `legacy_anchor_ambiguous` when canonical container analysis
cannot prove that its ordinal is safe under the v0.3 traversal policy.
Any refusal after the document snapshot is readable, including an OOXML
extraction failure, carries `observed_source_sha256` for the bytes that rejected
the claim. The caller's claimed hash remains asserted input and is digested in
compact history.

Input:

```json
{
  "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx",
  "anchor": {
    "change_unit_id": "cu_017",
    "file_sha256": "example"
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

For unchanged wording, pass the paragraph reference without converting it to a
label-based anchor:

```json
{
  "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx",
  "anchor": {
    "schema_version": "paragraph_ref.v1",
    "ref_type": "paragraph",
    "file_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "part_name": "word/document.xml",
    "paragraph_index": 42,
    "paragraph_text_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "reading_mode": "accepted_current_v1",
    "container_policy": "canonical_body_flow_v1"
  },
  "quote": "Except as set out in Clause 14.3"
}
```

## `preflight_edits`

Call this before `apply_edits` to determine whether one atomic edit batch can
pass the complete document-processing pipeline. Preflight reads one source byte
snapshot, validates and plans every edit, performs the OOXML surgery on a copy,
serializes a candidate DOCX in memory, re-extracts it, and runs the same
round-trip and collateral-change checks as apply. It never creates an output
DOCX. Given the same source bytes, producer build, configured author, and edit
payload, `batch_applicable: true` means apply will not fail in those document-
processing phases; output publication can still fail because the source changed,
the destination exists, permissions are insufficient, or the filesystem races.

A successful MCP preflight returns `preflight_proof.v1`. It contains the source
SHA-256, canonical edit-payload digest, configured author, producer build,
predicted candidate SHA-256 and a SHA-256 over those proof fields. The proof is
an unkeyed deterministic drift binding. It is not authentication, a digital
signature, a trusted timestamp or evidence that a particular person approved
the proposal. It does not bind `output_path`; apply may publish the same verified
candidate under a caller-selected new destination.

The document is read-only, but the tool records its result in the local
`.veqtor` sidecar unless decision records are disabled. A failed batch is a
structured successful tool response, not an MCP error. No edit is partially
committed.

Input uses the same `source_path` and `edits` fields as `apply_edits`, without
an `output_path` or caller-supplied proof.

Output:

```json
{
  "status": "ok",
  "source_path": "/Users/example/Deals/AcmeDistribution/12-current.docx",
  "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "tracked_change_author": "Veqtor MCP",
  "producer": {
    "name": "veqtor-mcp",
    "version": "0.3.0.dev0",
    "build": "source-snapshot-v1-sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
  },
  "batch_applicable": true,
  "candidate_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "observed_candidate_sha256": null,
  "blocking_edit_index": null,
  "refusal_code": null,
  "failure_phase": null,
  "reason": null,
  "edits": [
    {
      "edit_index": 0,
      "change_unit_id": "cu_017",
      "status": "applicable",
      "operation": "counter",
      "match_count": 1,
      "target_author": "J. Smith",
      "target_revision_ids": ["17"],
      "position_status": "supported",
      "refusal_code": null
    }
  ],
  "round_trip_check": {
    "status": "passed",
    "comparison": "ooxml_semantic_diff_outside_touched_anchors",
    "collateral_changes": []
  },
  "preflight_proof": {
    "schema_version": "preflight_proof.v1",
    "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "edits_sha256": "c860914db41854419a6a3a28daf238f2bbd124a4830bcf64e18658fc8256b8bf",
    "tracked_change_author": "Veqtor MCP",
    "producer_build": "source-snapshot-v1-sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "candidate_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "proof_sha256": "f9fe76452a5de5e656970b592208ea5284bb5784c959fcf05ecc5f99bec04a0e"
  }
}
```

A refused edit keeps the same per-edit diagnostic shape and adds a stable code,
for example:

```json
{
  "status": "ok",
  "source_path": "/Users/example/Deals/AcmeDistribution/12-current.docx",
  "source_sha256": "example",
  "tracked_change_author": "Veqtor MCP",
  "producer": {
    "name": "veqtor-mcp",
    "version": "0.3.0.dev0",
    "build": "source-snapshot-v1-sha256:example"
  },
  "batch_applicable": false,
  "candidate_sha256": null,
  "observed_candidate_sha256": null,
  "blocking_edit_index": 0,
  "refusal_code": "counter_position_unsupported",
  "failure_phase": "planning",
  "reason": "counter_position_unsupported: the countered insertion is directly followed by other tracked markup; placing the replacement there would break its grouping",
  "edits": [{
    "edit_index": 0,
    "change_unit_id": "cu_005",
    "status": "blocked",
    "operation": "counter",
    "match_count": 1,
    "target_author": "J. Smith",
    "target_revision_ids": ["78"],
    "position_status": "unsupported",
    "refusal_code": "counter_position_unsupported"
  }],
  "round_trip_check": null,
  "preflight_proof": null
}
```

Per-edit status is `applicable` only after the complete dry-run succeeds.
`blocked` identifies the edit that caused a specific refusal; `planned` means
the edit was matched and planned but the atomic batch failed in a later phase;
`not_evaluated` means processing never reached that edit. The current producer's
closed `failure_phase` vocabulary is `validation`, `source`, `matching`,
`planning`, `surgery`, `serialization`, `round_trip`, `preflight_binding`, and
`publication`; the last two primarily describe apply/binding failures. A late
candidate failure preserves
`observed_candidate_sha256` and a failed `round_trip_check` without presenting
the candidate as applicable.

Every item in `edits` has the same field set. A `null` operation,
`match_count`, or target means that processing never reached the phase that
could establish that fact; `match_count: 0` means matching did run and proved
there were no matches. Position is never nullable: `position_status` is
`supported`, `unsupported`, or `not_evaluated`. For example, validation fails
before text matching and therefore returns:

```json
{
  "edit_index": 0,
  "change_unit_id": "cu_001",
  "status": "blocked",
  "operation": null,
  "match_count": null,
  "target_author": null,
  "target_revision_ids": [],
  "position_status": "not_evaluated",
  "refusal_code": "delete_text_missing"
}
```

The earlier 14-operation paired counter/reinstate hang report remains
unreproduced. The regression exercises that batch shape and asserts
a terminal structured `edits_overlap` result with all 14 diagnostics. It does
not establish a general wall-clock bound and adds no timeout or cancellation
API.

## `apply_edits`

Call this only after the user asks to prepare or apply counter wording and only
with an anchor produced by `extract_redlines`. Under MCP contract
`veqtor.mcp.v0.3`, the complete `preflight_proof` returned by the successful
preflight is a required input. Missing or malformed proof objects are rejected
by the advertised MCP schema or as `preflight_proof_invalid`; a well-formed
proof that does not match the recomputed source, edits, author, build or
candidate fails as `preflight_binding_mismatch`. Both controlled failures occur
without publishing output and use `failure_phase: "preflight_binding"` in the
recorded error provenance.

The proof SHA-256 is unkeyed and can be recomputed by anyone holding the fields.
It detects drift between calls; it is not authentication, a digital signature
or approval evidence. The destination path is deliberately outside the proof.
The lower-level Python `veqtor_docx.apply_edits` function keeps an optional proof
argument for v0.1 compatibility; that bypass is not part of the v0.3 MCP input
contract.

The server applies explicit edits only. Each edit must state the text range to
delete and the replacement text to insert. If the anchor is missing, ambiguous,
bound to a different file hash, or resolved to text that does not exactly match
`delete_text`, the tool returns an error and writes nothing.

Edit anchors are a closed union of the legacy two-field change-unit anchor and
`change_unit_anchor.v2`; paragraph and section references are read evidence and
cannot authorize edits. New callers should pass the exact v2 anchor emitted on
the change unit. A legacy anchor proceeds only when
`legacy_two_field_anchor_safe: true`; otherwise preflight/apply refuse with
`legacy_anchor_ambiguous` before surgery or publication.

`edits` are atomic: if any edit fails validation or application, no final
output DOCX is written. Planning, OOXML surgery, candidate serialization,
re-extraction, round-trip and collateral checks happen in memory before a
temporary publication artifact exists. A failure in those phases therefore
leaves no temp file. Once validation passes, the exact candidate bytes are
written to a temporary artifact for atomic publication.
Final publication is an atomic create-if-absent operation from a temporary
file in the destination directory. If another process creates the destination
after the initial check, apply returns `output_exists` and preserves that file;
it never uses overwrite-style rename semantics.
After a successful hard-link publication, removal of the temporary name is
best-effort. If the operating system refuses that cleanup, apply still returns
success because the requested output has already been atomically published;
the uniquely named temp remains as a second hard link to the same inode and may
be removed later without changing the output bytes or `output_sha256`.
The round-trip check compares OOXML structure outside touched anchor ranges; it
does not require byte-identical DOCX packages. Inside touched ranges it proves
the prior units plus the proposed unit per paragraph position, so the right
revision content appearing in a different clause is a failure rather than a
successful global multiset match.
Any controlled apply refusal after the source byte snapshot is known records
`observed_source_sha256`. Operation-wide failures do not invent an `edit_index`;
that field is present only when one specific edit or plan caused the refusal.
If re-extraction of the in-memory candidate fails after its own SHA is known,
that distinct digest is recorded as `observed_candidate_sha256`; it never
replaces or masquerades as the apply source snapshot.
CRC, local/central/descriptor disagreement, truncated streams or other
controlled structural failures while validating all source archive members use
`file_unextractable`; candidate serialization or publication failures use
`output_unwritable`. Forbidden compression uses `unsupported_compression` and
encrypted members use `encrypted_docx` before any corresponding decoder is
created.
New tracked-change ids are allocated only when every existing `w:id` uses the
supported lexical form: one to ten ASCII decimal digits, with a numeric value
no greater than `2147483647`. Leading zeroes are accepted within that ten-digit
limit; longer spellings are refused even when their numeric value would fit.
Other values return `revision_id_unsupported`; an edit or batch that needs more
ids than remain through `2147483647` returns `revision_id_exhausted` before any
OOXML surgery. Neither condition reaches Python's unbounded integer conversion
path or publishes a partial batch.
Before creating a candidate, apply eagerly validates the whole source package
once and retains those same validated bytes for baseline extraction and OOXML
surgery. Every member is checked even when extraction does not otherwise use
it; duplicate-name lookup is never allowed to substitute the last member's
bytes for an earlier member. The separately serialized candidate is then
validated again as the exact round-trip snapshot that may be published.

Three edit forms, all written as visible tracked changes — never silent
rewrites:

- plain replace/delete: `delete_text` lies in untouched text of the anchored
  clause;
- counter: `delete_text` lies entirely inside one counterparty pending
  insertion — written as a strike nested in their insertion (their proposal
  stays visible; extraction reports your `counter` unit and marks theirs
  `countered_by`), with the replacement inserted after theirs;
- reinstate: `{"anchor": ..., "reinstate_text": "..."}` restores text hidden
  inside exactly one counterparty deletion as a visible tracked insertion
  placed before the preserved deletion. This is not Word Reject: Veqtor does
  not accept, reject or remove the counterparty deletion.

Edit objects use a closed schema. Replace/delete accepts only `anchor`, a
non-empty string `delete_text`, and optional string `insert_text`; an empty
string means delete-only. Reinstate accepts only `anchor` and a non-empty
string `reinstate_text`: the `insert_text` key must be absent. Validation uses
the following stable machine codes; malformed values are never ignored or
coerced:

| Input condition | Refusal code |
| --- | --- |
| `delete_text` absent, `null`, empty, or non-string | `delete_text_missing` |
| `insert_text` present with a non-string value, including `null` | `invalid_edit` |
| `reinstate_text` present but not a non-empty string | `invalid_edit` |
| Unknown edit or anchor field | `invalid_edit` |
| Text containing an XML-incompatible character | `invalid_edit` |
| More than 100 edits in one atomic batch | `resource_limit_exceeded` |
| More than 20,000 new characters in one edit | `resource_limit_exceeded` |
| More than 200,000 new characters across the batch | `resource_limit_exceeded` |

`delete_text_missing` therefore means that no usable non-empty deletion string
was supplied; it is not limited to the physical absence of the JSON key.

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
pending insertion, return `overlaps_tracked_changes`. New revisions use the
server-start author from `VEQTOR_TRACKED_CHANGE_AUTHOR` (default `Veqtor MCP`)
and carry no `w:date`. The MCP input cannot override the author per call.
Matcher readings use the same mappings as extraction for `w:tab`, `w:br`,
`w:cr` and `w:noBreakHyphen`; when the v0.1 surgery path cannot preserve one
of those element atoms, it reports `match_count: 1` and
`unsupported_run_shape` rather than a false zero-match.
All edit text is validated against the XML 1.0 character set before matching;
invalid control characters or Unicode surrogates return a validation-phase
`invalid_edit` refusal and never reach OOXML serialization.

Input:

```json
{
  "source_path": "/Users/example/Deals/AcmeDistribution/12-current.docx",
  "output_path": "/Users/example/Deals/AcmeDistribution/13-our-counter.docx",
  "edits": [
    {
      "anchor": {
        "change_unit_id": "cu_017",
        "file_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      },
      "delete_text": "USD 50,000",
      "insert_text": "The aggregate liability cap will equal the fees paid in the previous 12 months, excluding willful misconduct."
    }
  ],
  "preflight_proof": {
    "schema_version": "preflight_proof.v1",
    "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "edits_sha256": "c860914db41854419a6a3a28daf238f2bbd124a4830bcf64e18658fc8256b8bf",
    "tracked_change_author": "Veqtor MCP",
    "producer_build": "source-snapshot-v1-sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "candidate_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "proof_sha256": "f9fe76452a5de5e656970b592208ea5284bb5784c959fcf05ecc5f99bec04a0e"
  }
}
```

Output:

```json
{
  "status": "ok",
  "source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "output_path": "/Users/example/Deals/AcmeDistribution/13-our-counter.docx",
  "output_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "tracked_change_author": "Veqtor MCP",
  "producer": {
    "name": "veqtor-mcp",
    "version": "0.3.0.dev0",
    "build": "source-snapshot-v1-sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
  },
  "applied": [
    {
      "change_unit_id": "cu_017",
      "operation": "replace",
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
  "preflight_binding_status": "verified",
  "preflight_candidate_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "candidate_output_sha256_match": true,
  "record_id": "dr_004",
  "record_status": "written"
}
```

The current producer always emits
`comparison: "ooxml_semantic_diff_outside_touched_anchors"`. Historical v1
records using `comparison: "exact"` remain readable for compatibility, but
that retired value is only a legacy round-trip-success marker. It is not
emitted by current code, and v1 assigns it no byte-for-byte identity or other
stronger guarantee for the DOCX ZIP package. The current marker states the
documented semantic scope; neither value is a whole-package binary proof.

## `export_decision_record`

Call this when the user asks for an audit trail, negotiation record, or summary
of actions taken by the toolchain. v1 returns JSON records for the workspace;
the host model formats them. There is no PDF/CSV/DOCX export, filtering, or
search in v1.

The export reads `.veqtor/decision-records.jsonl` for the workspace and returns
chronological substantive records. Access events from `export_decision_record`
itself are recorded in the journal but omitted from the MCP `records` array and
`total_count`. The response snapshot is taken first and the current access
event is appended second. Consequently the first export reports
`access_count: 0`, its returned `record_id` may be the next visible id gap, and
the second export reports the first event in `access_count` while still
omitting it from `records`. The explicit scope fields and
`current_export_event` below are authoritative; check `recorded_locally` rather
than assuming a write succeeded.

The supplied workspace must already contain a valid journal. Export never
creates `.veqtor` in an uninitialized folder and never silently switches to a
child. If the exact folder has no journal, Veqtor inspects direct children only,
without following symlinks, within 500 entries and a cooperative one-second
elapsed-time budget checked between filesystem operations. The service
`.veqtor` directory is excluded from child-workspace candidates. The elapsed-
time check cannot interrupt a blocked filesystem call and is not a hard timeout:

- zero child journals: `workspace_uninitialized`;
- exactly one: `workspace_mismatch`, with one path-safe relative suggestion;
- two or more: `workspace_ambiguous`, without exposing child names; or
- an exhausted bound or incomplete scan: `workspace_discovery_incomplete`.

All four states fail closed and create no files in the supplied folder. An
existing exact-workspace journal always wins over child candidates. This is
bounded wrong-parent diagnosis, not recursive matter discovery.

New records store one canonical absolute workspace path before both journal
publication and later compact hashing. Where the host exposes a descriptor-
backed path, Veqtor uses the filesystem's own directory-entry spelling, so case
aliases of one inode on a case-insensitive volume produce one identity without
case-folding distinct directories on case-sensitive volumes. The top-level
compact workspace digest and each new record's workspace digest therefore
identify the same root. Existing legacy records remain readable and are not
rewritten; a record created by a pre-canonicalization build from a relative or
caller-spelled path may retain a digest of that historical spelling.

To protect context and privacy, the MCP export is always compact: verbatim `input`
payloads, paths, clause headings, raw error text and free-form provenance are
replaced by digests. Only format-validated identifiers, hashes and counters
observed by the server remain verbatim; client-asserted claims are digested,
and unused extra anchor fields are not journaled. Repeated facts are bounded
snapshots `{count, sha256, sample, truncated}`; samples contain at most 20
validated items while the digest covers the complete source collection. Large
extract results therefore stay bounded and re-verifiable.
For successful `preflight_edits` and `apply_edits`, the edit anchors originated
as client-asserted input rather than a fresh observation. Their compact
`provenance.anchors` therefore keeps the complete collection digest and count
but deliberately emits `sample: []` and `truncated: true`, even when the count
is one. Validated observed edit outcomes remain available in `result.edits` or
`result.applied`; the empty asserted-anchor sample is a privacy policy, not a
lost observed anchor.
Only a genuine empty list or mapping is represented by `count: 0` with
`truncated: false`. If readable historical JSON stores the wrong container type,
compact projection returns `count: null`, a digest of that original malformed
value, an empty sample and `truncated: true`; it never turns malformed data into
a complete empty snapshot.
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
the bounded server-configured tracked-change author is also retained so a
write's identity can be inspected. Other strings are omitted, digested, or
represented as `legacy-unvalidated`.
Only the current `producer.build` snapshot format remains verbatim; every older
or unrecognized value is replaced by `legacy-unvalidated` plus its digest in
compact output. Pre-release journals using older build markers are disposable
and are not migrated; archive or remove their `.veqtor` sidecar before a
pre-release demo to avoid mixing legacy and current markers.
The raw journal is not available through the MCP surface. It remains readable
locally for diagnostics and historical compatibility, including old access
events whose stored result says `payloads: "full"`. Older clients that still
send `include_payload` do not enable full mode; the unrecognized argument may be
ignored by the current transport and the result remains explicitly
`payloads: "compact"`. An `extract_redlines` record is already a summary and
does not contain every old/new text.
There are three distinct result layers: (1) the live MCP response, (2) the
private raw journal record, whose `result` is a tool-specific normalized
result or summary rather than a verbatim copy of that live response, and (3)
the privacy-minimized compact projection in the exported `records` array. The
raw journal is therefore not a guaranteed superset from which the exact live
response can be reconstructed.
Responses are capped to the newest `max_records` entries (default 50). If
`truncated` is true, call again with
`before_record_id: next_before_record_id` to page earlier. `total_count` is the
number of substantive records visible in the current cursor window, not a
global all-time count; `access_count` reports all access events present before
the current export append and is independent of the substantive cursor.

Every valid compact verification match contains `part_name`, `revision_ids`,
`side` and `clause_sha256`. When a matched clause label/heading exists,
`clause_sha256` is its canonical-JSON digest after that value is omitted for
privacy. When no label or heading exists, the key is still present with value
`null`. It is never a digest of the clause body or the verified quotation.

Input:

```json
{
  "workspace": "/Users/example/Deals/AcmeDistribution",
  "max_records": 50,
  "before_record_id": null
}
```

Output:

```json
{
  "workspace": {"sha256": "example-workspace-digest", "omitted": true},
  "total_count": 1,
  "access_count": 3,
  "returned_count": 1,
  "truncated": false,
  "next_before_record_id": null,
  "payloads": "compact",
  "records_scope": "substantive_records_only",
  "total_count_scope": "substantive_records_before_cursor",
  "access_events_recorded_locally": true,
  "access_events_in_records": false,
  "access_count_scope": "all_prior_access_events_before_current_export",
  "access_count_includes_current_export": false,
  "assurance": {
    "journal_model": "best_effort_local_provenance",
    "model_payload": "compact_only",
    "raw_journal_visibility": "private_local_only",
    "raw_journal_result": "tool_specific_summary_not_verbatim_live_response",
    "compact_projection": "privacy_minimized_view_not_raw_journal",
    "access_event_policy": "raw_journal_only_excluded_from_default_compact_records",
    "tamper_evident": false,
    "hash_chain": false,
    "record_id_guarantee": "strictly_increasing_only",
    "producer_identity": "python_source_files_snapshot_only",
    "content_hashes": "recheckable_fingerprints_not_authentication",
    "round_trip_scope": "ooxml_semantic_diff_outside_touched_anchors_not_docx_byte_identity"
  },
  "records": [
    {
      "schema_version": "decision_record.v1",
      "record_type": "verification.v1",
      "record_id": "dr_001",
      "created_at": "2026-07-09T12:00:00Z",
      "tool_name": "verify_quote",
      "workspace": {"sha256": "example-workspace-digest", "omitted": true},
      "producer": {"name": "veqtor-mcp", "version": "0.3.0.dev0", "build": "source-snapshot-v1-sha256:..."},
      "payloads": "compact",
      "input": {"sha256": "example-input-digest", "omitted": true},
      "result": {
        "status": "ok",
        "verdict": "exact",
        "matches": {
          "count": 1,
          "sha256": "example-matches-digest",
          "sample": [{
            "part_name": "word/document.xml",
            "revision_ids": {
              "count": 2,
              "sha256": "example-revision-id-digest",
              "sample": ["17", "18"],
              "truncated": false
            },
            "side": "new",
            "clause_sha256": null
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
  "current_export_event": {
    "record_id": "dr_005",
    "record_type": "access_event.v1",
    "record_status": "written",
    "recorded_locally": true,
    "included_in_records": false,
    "included_in_total_count": false,
    "included_in_access_count": false
  },
  "record_id": "dr_005",
  "record_status": "written"
}
```

The assurance object is part of the model-facing contract. File and result
hashes let a holder of the relevant bytes re-check content relationships; they
do not authenticate the mutable local journal. Record ids are server-assigned
and strictly increasing, but visible gaps may be normal because access events
are omitted, and ids do not prove that records were not deleted, truncated,
renumbered or rewritten. `producer_identity` covers imported Python source
files only, not the interpreter, dependencies, native libraries, configuration
or complete installed artifact.

`result_sha256` fingerprints the tool-specific result actually stored in the
raw journal, not the later compact export projection. For ordinary document
tools, `tool_result_sha256` fingerprints the normalized operation result before
record metadata and any tool-specific result compaction. For an export access
event, the stored export summary is itself the operation result passed to the
journal writer, so both digests cover that summary; the newly assigned access
event id and final live-response metadata do not yet exist at that point.
These are content fingerprints for re-checking and debugging, not
tamper-evidence or a way to reconstruct omitted content.
For `extract_redlines`, `result_sha256` and `tool_result_sha256` normally differ:
the stored raw-journal result is a bounded summary, while the full operation
result also contains the extracted change units. Equality of these two fields
is not a consistency gate. The full-result fingerprint can also include the
caller's path spelling, so it is not a location-independent document-content
identity.

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

Canonical hashing accepts at least 1,000,000 JSON nodes in v1. This floor is
separate from the 100,000-node limit for a stored journal record and must not
be narrowed under the same canonical-json-v1 contract.

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
The frozen two-file conformance vector hashes `ENGINE = 1\n` as
`c1ff757ec2295bdcca2cd04c50b1462b952f8be7c59f4bd0530163bce3da5a74`
at `veqtor_docx/engine.py` and `SERVER = 1\n` as
`0b580e9a58186f758e87b1cb658319682611f9fdac36264919432f06a66c4768`
at `veqtor_mcp/server.py`. The canonical manifest digest is
`6e6bdfc120d8caded5ff2b08c656ac81dc452cf9b66e9d9da4312001aba9e824`,
so the exact producer identity is
`source-snapshot-v1-sha256:6e6bdfc120d8caded5ff2b08c656ac81dc452cf9b66e9d9da4312001aba9e824`.
The literal manifest bytes and identity are ratcheted in
`test_source_snapshot_hashes_package_source_tree`; changing the schema,
framing, ordering or digest input requires a new snapshot prefix/version.
