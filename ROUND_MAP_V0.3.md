<!-- SPDX-License-Identifier: Apache-2.0 -->

# Stage 3B bounded Round Map v0.3 specification

## Status and non-implementation boundary

This document closes the contract for the first Stage 3B product slice. It is
a design and acceptance specification, not an implementation claim.

The future MCP tool name is `map_rounds`; its permanent decision-record pair
will be `(map_rounds, round_map.v1)`. Neither the tool, the pair, nor Round Map
runtime code is registered by this foundation batch. The development server
therefore continues to expose exactly seven tools. Implementing this contract,
passing automated tests, passing exact-build Claude Desktop acceptance, and
publishing a release remain separate gates.

The specification reuses draft MCP contract `veqtor.mcp.v0.3`. All nested
objects defined here are closed. The top-level result remains additive under
the existing v0.3 compatibility rule. The current FastMCP transport may ignore
unknown top-level input properties before a tool runs, so callers must use only
the advertised named arguments.

"Read-only" in this document means that Round Map never changes a candidate
DOCX or produces a new DOCX. As with every current live tool, the complete MCP
call may append its own private local decision record. Its MCP annotations are
therefore `readOnlyHint: false` and `idempotentHint: false`, while remaining
non-destructive and closed-world.

## Product model in plain language

Round Map starts with one exact paragraph in one exact DOCX. It looks across a
bounded folder of current DOCX files and the workspace's bounded local journal,
then returns a map of what the available evidence can and cannot say.

It can say:

- a local Veqtor apply record records one document hash as the source and
  another as the output;
- two complete paragraphs are exactly equal under the same accepted/current
  reading policy;
- a label or heading is useful for navigating to a possible counterpart; or
- the available observations are unresolved or ambiguous.

It cannot turn filenames, mtimes, positions, labels, similarity, or a mutable
local journal into authenticated negotiation history. In particular, the
first slice does not automatically claim that wording was first introduced,
deleted, restored, countered, or chronologically earlier. Those user questions
may remain unresolved unless later evidence contracts prove the required fact.

## Closed claim vocabulary

There are three relationship types and a separate resolution state. They must
never be collapsed into a confidence score.

### `recorded_derivation`

A directed document-level relationship from a validated
`(apply_edits, decision.v1)` local record. It is the only relationship type
allowed to state that the journal records source bytes as the input from which
output bytes were produced.

The name is deliberately not `provenance_derivation`. The journal is mutable,
local, unsigned, non-authenticated, non-hash-chained and not tamper-evident.
The edge records Veqtor's local source/output assertion; it does not prove who
approved the edit, a trusted time, complete artifact custody, paragraph
lineage, clause lineage, or global negotiation chronology.

### `exact_content_equality`

A symmetric paragraph-level relationship produced only after comparing the
complete `accepted_current_v1` text of two immutable document snapshots and
finding the Unicode scalar sequences exactly equal. Matching hashes alone do
not bypass the full comparison.

It proves equality of the two observed paragraph texts under the declared
reading/container policy. It does not prove derivation, chronology, semantic
clause identity, continuity, deletion, restoration, or authorship.

### `navigation_candidate`

A directed navigation-only relationship from the seed's structural section to
a section with an exact label and/or exact heading-navigation signal. Here
`label` and `heading` are the exact nullable Stage 3A section-navigation output
fields. In particular, `heading` is the bounded derived navigation title, not
the full accepted heading paragraph or `section_ref.heading_text_sha256`. It
helps a caller decide what to inspect next. It is not evidence of equality,
derivation, chronology, or semantic clause identity and never resolves a
paragraph by itself.

The first slice has no fuzzy, vector, translated, morphological, regular-
expression, or generic similarity relationship.

### Resolution states

Resolution is an item, not an edge. Each document content node in the bounded
map has exactly one seed-relative state:

- `exact_unique`: exactly one non-seed paragraph node in that document has
  complete exact equality with the seed paragraph;
- `ambiguous`: more than one non-seed exact paragraph candidate exists, or a
  reported semantic record conflict prevents one closed interpretation; or
- `unresolved`: there is no non-seed exact candidate in the declared inspected
  scope, the document exists only in records, or only navigation candidates
  exist.

`unresolved` never means deleted. One navigation candidate remains unresolved;
several navigation candidates do not become a forced match. A generic numeric
confidence or automatic best-candidate selection is forbidden.

## Future MCP contract

### Input

The future named arguments are:

```json
{
  "folder": "/Users/example/Deals/Acme",
  "seed": {
    "schema_version": "round_map_seed.v1",
    "path": "/Users/example/Deals/Acme/02-counterparty.docx",
    "paragraph_ref": {
      "schema_version": "paragraph_ref.v1",
      "ref_type": "paragraph",
      "file_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "part_name": "word/document.xml",
      "paragraph_index": 42,
      "paragraph_text_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "reading_mode": "accepted_current_v1",
      "container_policy": "canonical_body_flow_v1"
    }
  },
  "ordered_filenames": null,
  "cursor": null,
  "max_items": 50
}
```

The input rules are closed:

- `folder` resolves to one existing local directory without following a
  caller-controlled symlink as the final workspace target.
- Candidate names are direct basenames with no separator, `.` or `..` and a
  case-insensitive `.docx` suffix. Names beginning `~$` and non-DOCX entries are
  ignored, matching `list_rounds`; all other matching entries are candidates.
  Enumeration uses `lstat`; a candidate symlink, non-regular file, or regular
  file with more than one hard link refuses the complete operation with
  `unsafe_candidate`. Each candidate is opened relative to the validated
  folder descriptor with no-follow semantics. Initial `fstat` must again show
  a regular one-link file and its `(device, inode, mode, link_count)` must agree
  with enumeration; an unsafe opened type/link count is `unsafe_candidate` and
  an identity swap is `workspace_changed`. Its pre/post-read descriptor
  `(device, inode, mode, link_count, size, mtime_ns, ctime_ns)` tuple must agree.
  An immediate post-read per-name `lstat` must still be safe and match the
  opened identity. Any drift through that point is `workspace_changed`. The
  captured byte string is then immutable for the operation. This deliberately
  strengthens `list_rounds` rather than inheriting its current symlink-following
  behavior.
- `seed` contains exactly `schema_version`, `path`, and `paragraph_ref`.
- The seed path must be a direct candidate DOCX in `folder`. It cannot point to
  a journal path, a nested workspace, or a file outside the candidate set.
- The complete `paragraph_ref.v1` must re-resolve against the immutable bytes
  read from that path. A v0.2 two-field change-unit anchor and a
  `section_ref.v1` are not valid seeds.
- `ordered_filenames`, when present, is the complete exact filename manifest
  already defined by `list_rounds`: every candidate appears exactly once and
  no extra name appears. It controls position only.
- With no manifest, `filename_lexicographic_v1` orders by case-folded filename
  with the exact spelling as tie-breaker. Neither order claims chronology.
- `max_items` defaults to 50 and is an integer from 1 through 100; booleans are
  not integers.
- `cursor` is absent/null on the first page and a valid `rm1` cursor later. A
  later-page offset satisfies `1 <= offset < eligible_item_count`; zero and the
  complete-set length are not alternate empty pages.

There is no input for confidence, fuzzy search, chronology, a baseline,
accept/reject disposition, normalization, or output DOCX publication.

### Top-level successful result

The additive top-level object has these required stable fields:

| Field | Exact value/type |
| --- | --- |
| `schema_version` | `round_map.v1` |
| `status` | `ok` |
| `seed` | closed object: `document_id`, `paragraph_id`, complete `paragraph_ref.v1` |
| `ordering_source` | `filename_lexicographic_v1 \| explicit_filename_sequence_v1` |
| `order_basis` | closed object defined below |
| `snapshot` | closed `round_map_snapshot.v1` object defined below |
| `items` | array of the closed tagged union below |
| `coverage` | closed coverage object below |
| `limits` | closed fixed-limits object below |
| `next_cursor` | valid `rm1` cursor or null |
| `producer` | common closed v0.3 producer object |
| `record_id` | `dr_[0-9]+` or null |
| `record_status` | `written \| disabled \| write_failed` |

`items` is one closed tagged union paginated as a single sequence. Independent
node and edge cursors are forbidden because they could expose an internally
inconsistent partial graph. Every successful live response contains the
common producer and record metadata. `record_error` is permitted only with
`record_status: write_failed`; Round Map reuses the exact metadata tuple already
enforced by the current common v0.3 live-result schema.

All fields in the table are required. `record_error` is the only optional
stable top-level field.
`next_cursor` and `record_id` are string-or-null; all other fields are
non-null. `seed` contains exactly `document_id`, `paragraph_id`, and the
complete `paragraph_ref.v1`. `order_basis` contains exactly `kind`, `rule`,
`lineage_verified: false`, `round_id_semantics: position_only`, and
`filename_manifest_sha256`. Its two valid tuples are:

| `ordering_source` | `kind` | `rule` |
| --- | --- | --- |
| `filename_lexicographic_v1` | `filename` | `casefold_then_exact` |
| `explicit_filename_sequence_v1` | `caller_supplied_filename_sequence` | `exact_sequence` |

`snapshot` contains exactly `schema_version: round_map_snapshot.v1`,
`filesystem_snapshot_sha256`, `journal_snapshot_sha256`, `journal_state`,
`full_result_set_sha256`, `filesystem_cross_file_atomic: false`, and
`cross_source_atomic: false`. Both snapshot hashes
and the result-set hash are lowercase SHA-256 strings; even an absent journal
has the same relevant-record digest as a valid journal containing no relevant
apply record. `journal_state` is
`relevant_apply_records_present | no_relevant_apply_records`; it intentionally
describes semantic input, not physical file presence. `items`, `coverage`, and
`limits` use the exact closed shapes below. `producer`, `record_id`,
`record_status`, and conditional `record_error` reuse the common v0.3 live
metadata contract. Compatible releases may add unknown top-level fields only;
callers must ignore them and must not accept extras inside these nested
objects.

The record metadata tuples are exact: `written` requires a non-null record id
and no `record_error`; `disabled` requires null id and no error;
`write_failed` requires null id and a non-empty stable-code `record_error`.

## Snapshot model

### Candidate filesystem snapshot

The implementation must not call public `list_rounds` and then public
`inspect_document` against a path that can change between those calls. For each
candidate it obtains one immutable byte snapshot. The file SHA, DOCX validity,
paragraph text, paragraph/section references, revision/container coverage and
all map facts for that candidate come from those same bytes.

The candidate filename set is enumerated before reading and the exact set of
names (not every earlier file's metadata) is checked again after all reads. A
changed set refuses with `workspace_changed`; it is not a partial success. Each
byte snapshot remains a valid observation even though a file may change after
its immediate per-file check while a later file is read. The original validated
folder path is also re-resolved after capture and must still name the same
directory descriptor identity and filesystem spelling; a rename/replacement is
`workspace_changed`.

There is no point-in-time transaction across different candidate files: one
file can legitimately change after its bytes were captured while another is
being read. `filesystem_cross_file_atomic` is therefore always `false`. The
manifest is a deterministic collection of individually immutable observations,
not a claim that all candidate bytes coexisted at one instant.

`filename_manifest_sha256` is the `canonical_json_v1` SHA-256 of exactly:

```json
{
  "schema_version": "round_map_filename_manifest.v1",
  "ordering_source": "filename_lexicographic_v1",
  "filenames": ["01-draft.docx"]
}
```

Absence of `ordered_filenames` is normalized into the computed complete ordered
list, so the digest never depends on absent-versus-null syntax. The canonical
filesystem snapshot object is exactly:

```json
{
  "schema_version": "round_map_filesystem_snapshot.v1",
  "filename_manifest_sha256": "...",
  "observations": [{
    "observation_id": "rm_obs_v1:...",
    "canonical_path": "/validated/matter/01-draft.docx",
    "filename": "01-draft.docx",
    "position": 0,
    "byte_length": 12345,
    "file_sha256": "...",
    "inspection_coverage_sha256": "..."
  }]
}
```

Observations are in position order. `inspection_coverage_sha256` digests the
complete closed `round_map_inspection_coverage.v1` object; paths use the
canonical rule below; all lengths/positions are non-negative integers. No
mtime, inode or display round id participates. The
`canonical_json_v1` digest of this exact object is
`filesystem_snapshot_sha256`.

### Journal snapshot

The journal snapshot is taken under the journal foundation's bounded monotonic
root-then-journal lock order. A shared root lock first classifies a truly absent
journal without racing journal creation; an existing journal is then fully
stream-validated under a shared journal lock. Lock contention, corruption or
aggregate oversize refuses the map with `journal_busy`, `journal_corrupt`, or
`journal_oversize`.

An absent journal and a fully valid journal with no relevant apply records both
produce `journal_state: no_relevant_apply_records` and the same digest of the
canonical empty relevant-record set. This is not proof that no earlier
external editing occurred. Disabling decision-record publication does not
disable the read needed to build this snapshot.

Every raw record is validated, but the Round Map derivation digest includes
only semantically relevant `apply_edits` records. Every current candidate
document is a graph root. A valid record is relevant when its edge belongs to
a recorded connected component rooted at one of those documents. An
inconsistent non-error record is relevant when its closed
`conflict_endpoint_ids` intersects that rooted set; conflicts do not expand it.
Non-apply records, access events, and the tool's own future `round_map.v1`
record are excluded. This prevents ordinary observation or map journaling from
invalidating the next page. A new apply record that touches the included set
changes `journal_snapshot_sha256` and invalidates an old cursor.

Classification is two-pass and non-circular. The streaming journal scan
validates every raw frame and retains at most 10,000 apply records; one over
refuses with `resource_limit_exceeded` even if it would later prove unrelated.
The implementation classifies all retained successful apply records and builds
the valid document graph, computes components from every current candidate
document to a fixed point, then selects relevant valid/conflicting records by
intersection with that final set. Conflicts never expand the set.

Concretely, `journal_snapshot_sha256` digests exactly
`{schema_version: round_map_relevant_journal_snapshot.v1,
record_sha256s: [...]}`, where `record_sha256s` is the ASCII-sorted complete
array of relevant raw-record SHA-256 values. It does not include physical journal
presence, size, mtime, irrelevant record ids, or the Round Map's own record.
The whole physical journal is nevertheless bounded and validated on every
page. Thus first-page publication may create an own-record-only journal without
changing the stateless second-page snapshot or cursor binding.

Journal paths stored in records are never opened. Current observations are
joined to record-only facts by validated SHA-256 values only.

### No false global transaction

The filesystem snapshot and journal snapshot are individually bounded and
validated under their own rules, but they are not acquired under one global
lock. `cross_source_atomic` is therefore always `false`. The two snapshot
digests state exactly what was observed; they do not claim a simultaneous
filesystem and journal transaction. The filesystem digest also does not imply
cross-file atomicity, as disclosed separately above.

## Stable identity rules

Except for `document_id`, which directly exposes the already hash-bound DOCX
bytes as specified below, generated IDs use lowercase SHA-256 of a closed
identity object serialized with project `canonical_json_v1`. Display fields,
counts, support records, paths, filenames, labels, headings and pagination
position are excluded unless explicitly listed below.

### Document content

`document_id = "rm_doc_v1:" + file_sha256`.

One byte-identical DOCX observed at two paths is one document content node with
two observations. Renaming changes an observation, not document identity.
Changed bytes create a new document node. A hash known only from a valid apply
record creates a `record_only` node; lack of a current observation is not a
deletion claim.

### Document observation

`observation_id` digests exactly:

```json
{
  "schema_version": "document_observation_identity.v1",
  "document_id": "rm_doc_v1:...",
  "canonical_path": "/resolved/current/path.docx"
}
```

The emitted value is `"rm_obs_v1:" + sha256(canonical_json_v1(payload))`.

The positional `round_id` is deliberately excluded, so changing a manifest
does not pretend to change the filesystem observation. A rename creates a new
observation identity because its location changed. `canonical_path` is the
filesystem-spelled absolute path of the validated folder descriptor joined to
the exact direct basename; it is never obtained by resolving a candidate
symlink, because candidate symlinks are refused.

### Paragraph and section

`paragraph_id` digests the complete closed `paragraph_ref.v1` object.
`section_id` digests the complete closed `section_ref.v1` object. Neither a
text hash, heading hash, label, heading, position, nor file hash is sufficient
alone. Their emitted prefixes are `rm_par_v1:` and `rm_sec_v1:` respectively.
Stage 3A reference schemas are reused without alteration.

### Relationship, resolution and conflict

A relationship ID digests its type, endpoint IDs, direction and closed basis
identity. Duplicate apply records supporting the same source/output pair do
not create duplicate edges; support metadata is aggregated outside identity.
For symmetric exact equality, endpoint IDs are sorted before hashing.

The exact payload is:

```json
{
  "schema_version": "relationship_identity.v1",
  "relationship_type": "recorded_derivation",
  "from_id": "rm_doc_v1:...",
  "to_id": "rm_doc_v1:...",
  "direction": "directed",
  "basis_identity": {
    "schema_version": "recorded_derivation_basis_identity.v1",
    "record_schema_version": "decision_record.v1",
    "tool_name": "apply_edits",
    "record_type": "decision.v1",
    "assurance": "best_effort_local_non_tamper_evident",
    "derivation_scope": "document_bytes_only"
  }
}
```

Exact equality uses all fields of `exact_content_equality_basis.v1` as its
`basis_identity`; navigation uses all fields of
`navigation_candidate_basis.v1`. Support counts, samples and profile mixes are
never part of relationship identity. `record_sha256` everywhere in this
contract means lowercase SHA-256 of the complete decoded raw record object
serialized with `canonical_json_v1`, including its `record_id`; it is not
`result_sha256` or `tool_result_sha256`.

A resolution ID digests the seed paragraph ID and target document ID, not the
current state. A conflict ID digests its closed conflict type, affected IDs and
validated source record digest. Changing a state/support snapshot updates the
item under the same logical identity and changes the full result-set digest.
The emitted prefixes are `rm_rel_v1:`, `rm_resolution_v1:`, and
`rm_conflict_v1:`. Resolution identity is exactly
`{schema_version: resolution_identity.v1, seed_paragraph_id, document_id}`;
conflict identity is exactly
`{schema_version: conflict_identity.v1, conflict_type, affected_document_ids,
record_sha256}`, with affected ids sorted.

## Closed item schemas

Every item contains exactly `schema_version: round_map_item.v1`, `item_type`,
the fields listed for its variant, and no extras.

### `document_node`

```json
{
  "schema_version": "round_map_item.v1",
  "item_type": "document_node",
  "id": "rm_doc_v1:...",
  "file_sha256": "...",
  "observation_state": "record_only",
  "observation_count": 0,
  "inspection_coverage": null,
  "incoming_recorded_derivation_count": 1,
  "outgoing_recorded_derivation_count": 2,
  "topology_flags": {
    "multiple_parents": false,
    "cycle_member": false,
    "self_loop": false
  }
}
```

`observation_state` is `current`, `record_only`, or `current_and_recorded`.
It is `record_only` exactly when observation count is zero, `current` when one
or more observations exist and the document is not an endpoint of an emitted
recorded-derivation edge, and `current_and_recorded` otherwise.
`inspection_coverage` is null exactly for `record_only`; otherwise it is this
closed `round_map_inspection_coverage.v1` projection of the Stage 3A snapshot:

```json
{
  "schema_version": "round_map_inspection_coverage.v1",
  "scan_complete": true,
  "indexed_paragraph_count": 73,
  "nonempty_indexed_paragraph_count": 70,
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
}
```

All fields are required. The three lists and `container_coverage` reuse their
exact Stage 3A values and closed vocabularies; the projection omits Stage 3A
page counters and `complete_literal_match_count`. The outer
`indexed_paragraph_count` equals
`container_coverage.indexed_paragraph_count`; the nonempty counter has no
nested counterpart. The fixed excluded-parts prefix shown is always complete,
followed only by Stage 3A's sorted dynamic exclusions. `observation_count`
equals the number of emitted observations for the node. Incoming/outgoing counts equal
the emitted recorded-derivation edges, including a self-loop in both counts;
the three topology flags are exact projections of those edges.
`multiple_parents` is true only for more than one distinct incoming source
document id. `self_loop` is true for an edge from the node to itself.
`cycle_member` is true for membership in a directed cycle of two or more
distinct document ids; a self-loop alone does not set it.

### `document_observation`

```json
{
  "schema_version": "round_map_item.v1",
  "item_type": "document_observation",
  "id": "rm_obs_v1:...",
  "document_id": "rm_doc_v1:...",
  "path": "/Users/example/Deals/Acme/02-counterparty.docx",
  "filename": "02-counterparty.docx",
  "position": 1,
  "round_id": "round-002",
  "position_basis": "filename_lexicographic_v1"
}
```

`position` is zero-based. `position_basis` is
`filename_lexicographic_v1` or `explicit_filename_sequence_v1`.
`round_id` is a display/navigation position only and never appears in a
derivation or chronology basis. It is exactly `round-` plus `position + 1`
zero-padded to at least three ASCII digits, retaining the current
`list_rounds` convention.

### `paragraph_node`

```json
{
  "schema_version": "round_map_item.v1",
  "item_type": "paragraph_node",
  "id": "rm_par_v1:...",
  "document_id": "rm_doc_v1:...",
  "paragraph_ref": {
    "schema_version": "paragraph_ref.v1",
    "ref_type": "paragraph",
    "file_sha256": "...",
    "part_name": "word/document.xml",
    "paragraph_index": 42,
    "paragraph_text_sha256": "...",
    "reading_mode": "accepted_current_v1",
    "container_policy": "canonical_body_flow_v1"
  },
  "container_kind": "body",
  "roles": ["seed"]
}
```

`roles` is exactly `["seed"]` for the one seed node or
`["exact_candidate"]` for any other emitted paragraph. Seed-self exclusion
prevents the same identity from receiving both roles. Paragraph body text and
snippets are never returned. `container_kind` is `body | table_cell`.

### `section_node`

```json
{
  "schema_version": "round_map_item.v1",
  "item_type": "section_node",
  "id": "rm_sec_v1:...",
  "document_id": "rm_doc_v1:...",
  "section_ref": {
    "schema_version": "section_ref.v1",
    "ref_type": "section",
    "file_sha256": "...",
    "part_name": "word/document.xml",
    "heading_paragraph_index": 42,
    "heading_text_sha256": "...",
    "reading_mode": "accepted_current_v1",
    "container_policy": "canonical_body_flow_v1"
  },
  "label": "14.2",
  "heading": "Limitation of Liability",
  "level": 1,
  "basis": "word_outline_level_v1",
  "label_basis": "word_numbering_v1",
  "roles": ["seed_navigation"]
}
```

`label`, `heading`, and `label_basis` are nullable; `basis` is exactly
`word_outline_level_v1`, `label_basis` is null or
`word_numbering_v1 | explicit_heading_text_v1`, and `level` is 0 through 8.
`roles` is exactly `["seed_navigation"]` for the seed section or
`["candidate_navigation"]` for any other emitted section. The strings are live
navigation output, not identity or evidence, and compact provenance omits
them.

### `relationship`

```json
{
  "schema_version": "round_map_item.v1",
  "item_type": "relationship",
  "id": "rm_rel_v1:...",
  "relationship_type": "exact_content_equality",
  "from_id": "rm_par_v1:...",
  "to_id": "rm_par_v1:...",
  "direction": "symmetric",
  "basis": {
    "schema_version": "exact_content_equality_basis.v1",
    "reading_mode": "accepted_current_v1",
    "container_policy": "canonical_body_flow_v1",
    "part_name": "word/document.xml",
    "comparison": "complete_unicode_scalar_sequence_v1",
    "full_text_compared": true,
    "paragraph_text_sha256": "..."
  },
  "derivation_recorded": false,
  "lineage_verified": false,
  "chronology_verified": false
}
```

The allowed endpoint/direction pairs are closed:

- `recorded_derivation`: document → document, `direction: directed`;
- `exact_content_equality`: paragraph ↔ paragraph,
  `direction: symmetric`; and
- `navigation_candidate`: seed section → candidate section,
  `direction: directed`.

Recorded derivation always emits source as `from_id` and output as `to_id`;
navigation always emits seed section then candidate section. Symmetric exact
equality emits the two paragraph ids in ASCII order in both the item and its
identity payload, so equivalent evidence cannot change item bytes.

The boolean claim fields are fixed by type. Only `recorded_derivation` has
`derivation_recorded: true`. Generic `lineage_verified` and
`chronology_verified` are `false` for every type in this slice, preventing a
consumer from upgrading the mutable-journal assertion into verified lineage.
The `basis` discriminator must match `relationship_type` exactly; no other
combination is valid.

### `resolution`

```json
{
  "schema_version": "round_map_item.v1",
  "item_type": "resolution",
  "id": "rm_resolution_v1:...",
  "seed_paragraph_id": "rm_par_v1:...",
  "document_id": "rm_doc_v1:...",
  "state": "unresolved",
  "reason": "navigation_only",
  "exact_candidate_count": 0,
  "navigation_candidate_count": 1,
  "conflict_count": 0,
  "candidate_ids": {
    "count": 1,
    "sha256": "...",
    "sample": ["rm_sec_v1:..."],
    "truncated": false
  }
}
```

The reason vocabulary is:

- `one_exact_candidate` for `exact_unique`;
- `multiple_exact_candidates` or `recorded_fact_conflict` for `ambiguous`;
- `navigation_only`, `no_match_in_declared_scope`,
  `declared_scope_incomplete`, or `record_only_document` for `unresolved`.

`candidate_ids` is exactly the ASCII-sorted unique union of that document's
non-seed exact-candidate paragraph ids and navigation-candidate section ids.
Its `count` is the full length, `sha256` digests the complete array with
`canonical_json_v1`, `sample` is the first at most 20 ids, and `truncated` is
true exactly when `count` exceeds sample length. Conflict document ids are not
candidate ids. One resolution exists per document content node, not per path
alias.

State and reason use this first-match precedence, so no observation can satisfy
two states:

| Condition | `state` | `reason` |
| --- | --- | --- |
| one or more intersecting semantic conflicts | `ambiguous` | `recorded_fact_conflict` |
| no conflict and more than one exact candidate | `ambiguous` | `multiple_exact_candidates` |
| no conflict and exactly one exact candidate | `exact_unique` | `one_exact_candidate` |
| zero exact candidates and record-only document | `unresolved` | `record_only_document` |
| zero exact candidates and pruned declared scope | `unresolved` | `declared_scope_incomplete` |
| zero exact candidates and one or more navigation candidates | `unresolved` | `navigation_only` |
| otherwise | `unresolved` | `no_match_in_declared_scope` |

Several navigation candidates remain `unresolved/navigation_only`; navigation
alone never creates `ambiguous`. For a conflict row, exact/navigation counts
remain factual and candidate ids remain visible even though conflict takes
precedence.

### `conflict`

```json
{
  "schema_version": "round_map_item.v1",
  "item_type": "conflict",
  "id": "rm_conflict_v1:...",
  "conflict_type": "inconsistent_apply_record",
  "reason": "result_output_sha256_mismatch",
  "affected_document_ids": ["rm_doc_v1:..."],
  "record_sha256": "...",
  "edge_emitted": false
}
```

The first-slice conflict type is only `inconsistent_apply_record`. Its exact
reason vocabulary is:

`result_status_invalid | missing_source_sha256 | invalid_source_sha256 |
missing_output_sha256 |
invalid_output_sha256 | result_output_sha256_mismatch | round_trip_missing |
round_trip_failed | round_trip_comparison_unsupported |
round_trip_fact_mismatch | result_source_sha256_mismatch |
preflight_binding_status_invalid | preflight_candidate_sha256_mismatch |
candidate_output_sha256_match_invalid | strengthened_fact_mismatch |
unsupported_legacy_profile`.

The first failing check supplies the single reason. Conflict endpoint
extraction is closed and does not depend on which later semantic checks were
reached. Four raw slots are examined independently:
`provenance.source_sha256`, `result.source_sha256`,
`provenance.output_sha256`, and `result.output_sha256`. A slot contributes
its `rm_doc_v1:` id only when its value is a lowercase SHA-256; absent or
invalid values contribute nothing. Values are deduplicated and ASCII-sorted.
Paths, anchor hashes, `preflight_candidate_sha256`, and every other hash are
never endpoints.

The exact per-reason extraction table is:

| First-failure reason | Examined endpoint slots |
| --- | --- |
| `result_status_invalid` | provenance source; result source; provenance output; result output |
| `missing_source_sha256`; `invalid_source_sha256` | provenance source; result source; provenance output; result output |
| `missing_output_sha256`; `invalid_output_sha256` | provenance source; result source; provenance output; result output |
| `result_output_sha256_mismatch` | provenance source; result source; both divergent output slots |
| `round_trip_missing`; `round_trip_failed`; `round_trip_comparison_unsupported`; `round_trip_fact_mismatch` | provenance source; result source; provenance output; result output |
| `result_source_sha256_mismatch` | both divergent source slots; provenance output; result output |
| `preflight_binding_status_invalid`; `preflight_candidate_sha256_mismatch`; `candidate_output_sha256_match_invalid`; `strengthened_fact_mismatch`; `unsupported_legacy_profile` | provenance source; result source; provenance output; result output |

`conflict_endpoint_ids` is that complete sorted set. Relevance is true only
when its intersection with the final document-node set built from current
roots and valid derivation edges is non-empty. The conflict never expands that
set. `affected_document_ids` is exactly the sorted non-empty intersection;
therefore every affected id is an emitted document node. A relevant conflict's
raw `record_sha256` enters `journal_snapshot_sha256`; its conflict item and
affected resolution facts enter `full_result_set_sha256`, so both cursor
bindings are deterministic. An empty intersection excludes the record from
all three, even when another parseable but disconnected endpoint exists.
`edge_emitted` is always false. Raw record payloads, paths, edit text, author
strings and error text are never repeated.

This synthetic divergence fixture is normative:

```json
{
  "schema_version": "round_map_conflict_endpoint_fixture.v1",
  "current_document_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "record_fields": {
    "provenance_source_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "result_source_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "provenance_output_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "result_output_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "expected": {
    "reason": "result_output_sha256_mismatch",
    "conflict_endpoint_ids": [
      "rm_doc_v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "rm_doc_v1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "rm_doc_v1:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    ],
    "affected_document_ids": [
      "rm_doc_v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ],
    "record_relevant": true,
    "edge_emitted": false,
    "resolution_state": "ambiguous",
    "resolution_reason": "recorded_fact_conflict",
    "exact_candidate_count": 0,
    "navigation_candidate_count": 0,
    "conflict_count": 1,
    "candidate_id_count": 0,
    "journal_snapshot_includes_record": true,
    "full_result_set_includes_conflict_and_resolution": true
  }
}
```

Here only current A is initially in the final document-node set. Result output
A makes the mismatching record relevant and affects A; provenance output B and
source C remain disconnected and are not emitted. A's resolution is
`ambiguous/recorded_fact_conflict` with zero candidates and one conflict.

Valid branching, multiple parents, cycles, self-loops and duplicate support
records are topology, not conflicts and not journal corruption.

## Closed relationship bases

### Recorded derivation basis

```json
{
  "schema_version": "recorded_derivation_basis.v1",
  "record_schema_version": "decision_record.v1",
  "tool_name": "apply_edits",
  "record_type": "decision.v1",
  "assurance": "best_effort_local_non_tamper_evident",
  "derivation_scope": "document_bytes_only",
  "support_profile": "current_only",
  "supporting_records": {
    "count": 1,
    "current_count": 1,
    "published_v0_1_2_count": 0,
    "frozen_legacy_count": 0,
    "sha256": "...",
    "sample": [{
      "record_id": "dr_004",
      "record_sha256": "...",
      "profile": "current_v0.3"
    }],
    "truncated": false
  }
}
```

Supporting-record samples are sorted by numeric record id and capped at 20.
The complete support-list digest participates in result/cursor binding but not
relationship identity. Every eligible record is classified exactly as
`current_v0.3`, `published_v0.1.2_preflightless`, or `frozen_legacy_v1`.
`support_profile` is `current_only`, `published_v0_1_2_only`,
`frozen_legacy_only`, or `mixed` from the three exact counts. The counts sum to
`count`; `mixed` means at least two profile counts are non-zero. The sample
profile is required. The complete list is an array of closed
`{record_id, record_sha256, profile}` objects sorted by numeric record id.
`sha256` is the `canonical_json_v1` SHA-256 of exactly
`{schema_version: recorded_derivation_support.v1, records: [...]}`. `sample` is
the first at most 20 objects in that order, and `truncated` is true exactly when
`count` exceeds sample length.

### Exact equality basis

```json
{
  "schema_version": "exact_content_equality_basis.v1",
  "reading_mode": "accepted_current_v1",
  "container_policy": "canonical_body_flow_v1",
  "part_name": "word/document.xml",
  "comparison": "complete_unicode_scalar_sequence_v1",
  "full_text_compared": true,
  "paragraph_text_sha256": "..."
}
```

Both endpoint references must independently re-resolve from their immutable
snapshots. Equal hashes with unequal full text refuse the relationship and are
reported as an internal consistency failure; the implementation must not
silently treat this as ordinary inequality.

### Navigation basis

```json
{
  "schema_version": "navigation_candidate_basis.v1",
  "signals": [
    {"kind": "label_exact_v1", "value_sha256": "..."},
    {"kind": "heading_exact_v1", "value_sha256": "..."}
  ],
  "evidence_class": "navigation_only"
}
```

`signals` is sorted by `label_exact_v1` then `heading_exact_v1`, contains one
or both kinds exactly once, and hashes the exact non-null Unicode value of the
corresponding Stage 3A `label` or derived `heading` navigation field. Both seed
and candidate must expose equal values for that kind. It never compares or
rehashes the full heading paragraph behind `section_ref`. `value_sha256` is
SHA-256 of the exact string's UTF-8 bytes with no normalization or JSON wrapper,
matching the Stage 3A text-hash byte rule. Section level alone
never creates a candidate. Manual labels, computed numbering labels and
headings retain their disclosed Stage 3A `label_basis`/outline basis; they are
not normalized into semantic identities.

## Recorded-derivation eligibility

The general v1 journal validator establishes frame/schema/digest readability,
not semantic eligibility for a Round Map edge. A record creates a
`recorded_derivation` edge only when all applicable checks pass:

1. `schema_version` is `decision_record.v1` and the permanent pair is exactly
   `(tool_name: apply_edits, record_type: decision.v1)`.
2. `result.status` is exactly `ok`; `error` is an intentional failed-attempt
   exclusion, while a missing/other value is `result_status_invalid`.
3. `provenance.source_sha256` and `provenance.output_sha256` are lowercase
   SHA-256 values.
4. `result.output_sha256` equals `provenance.output_sha256`.
5. `result.round_trip_check` and `provenance.round_trip_check` are both present
   and canonically equal. Their exact facts are `status: passed`, comparison
   `ooxml_semantic_diff_outside_touched_anchors` or historical `exact`, and an
   empty `collateral_changes` array.
6. `provenance.source_sha256` is mandatory. `result.source_sha256`, when the
   selected profile permits it, must be valid and equal to provenance.
7. The three preflight-binding fields follow one exact profile below across
   both `result` and `provenance`; partial copies fail and current-profile
   copies must agree.

Profile classification is exact:

- `current_v0.3` requires `preflight_binding_status`,
  `preflight_candidate_sha256`, and `candidate_output_sha256_match` to be
  present in both `result` and `provenance`, canonically equal, and respectively
  `verified`, the output SHA, and `true`. Its comparison is the current
  `ooxml_semantic_diff_outside_touched_anchors`; `result.source_sha256` is
  present and equals provenance.
- `published_v0.1.2_preflightless` requires the current semantic comparison,
  requires `result.source_sha256` to be present and equal provenance, and
  requires all three strengthened fields to be absent from both `result` and
  `provenance`. This is the exact successful apply shape emitted by tagged
  package `0.1.2` before preflight binding was introduced.
- `frozen_legacy_v1` requires those three strengthened fields to be absent from
  both `result` and `provenance`, requires `result.source_sha256` to be absent,
  and requires the historical `exact` comparison. The provenance source,
  result/provenance output, and equal round-trip copies remain mandatory. This
  is the frozen successful golden-journal profile, not a synthesized upgrade.
- A hybrid, an unsupported comparison, or only some strengthened fields is
  `unsupported_legacy_profile`; no missing value is synthesized and the
  preflightless profile is never inferred when any strengthened field exists.

The normative synthetic raw-record fixture is
`tests/data/round-map-v0.1.2-preflightless-apply-record.json`. Its field shape
was captured from the tagged `v0.1.2` server: the full raw `result` contains
`source_sha256`, its own producer, output path/hash, applied text facts and the
semantic round-trip object; `provenance` contains the matching source/output
hashes and round-trip object; neither copy contains a strengthened field. The
fixture has a valid `decision_record.v1` result digest and must pass the general
journal validator before this profile is considered.
The profile name identifies the published field shape, not authenticated
software provenance: a `producer.version` string alone never qualifies a
record, and every semantic fact above remains mandatory.

For deterministic conflict reasons, implementations execute this exact
first-failure sequence rather than independently guessing a profile:

1. `result.status: error` exits as an excluded failed attempt; anything other
   than `ok` is `result_status_invalid`.
2. Missing then invalid `provenance.source_sha256` yields
   `missing_source_sha256` then `invalid_source_sha256`.
3. Missing then invalid output SHA in either `result` or `provenance` yields
   `missing_output_sha256` then `invalid_output_sha256`; unequal valid copies
   yield `result_output_sha256_mismatch`.
4. A missing/non-object result or provenance round-trip copy is
   `round_trip_missing`; unequal canonical copies are
   `round_trip_fact_mismatch`; non-passed status is `round_trip_failed`;
   an unlisted comparison is `round_trip_comparison_unsupported`; and a
   collateral list other than exactly `[]` is `round_trip_fact_mismatch`.
5. For comparison `exact`, any result-side source SHA or any strengthened field
   in either result/provenance is `unsupported_legacy_profile`; otherwise the
   record is eligible as `frozen_legacy_v1`.
6. For the current comparison, an absent result-side source SHA is
   `unsupported_legacy_profile`; an invalid result source is
   `invalid_source_sha256`; and an unequal valid source is
   `result_source_sha256_mismatch`. Then count the six strengthened-field slots
   (three fields in each of `result` and `provenance`). If all six are absent,
   the record is eligible as `published_v0.1.2_preflightless`. If all six are
   present, unequal copies are `strengthened_fact_mismatch`; equal copies are
   checked in order: binding status must be `verified`
   (`preflight_binding_status_invalid`), candidate SHA must be valid and equal
   output (`preflight_candidate_sha256_mismatch`), and match must be exactly
   true (`candidate_output_sha256_match_invalid`). Passing all checks is
   `current_v0.3`. Any other presence pattern is
   `unsupported_legacy_profile`.

An apply record with `result.status: error` is a recorded failed attempt: it
creates neither an edge nor a semantic conflict and is excluded from the
relevant-record digest/state, so adding one does not invalidate a cursor.
Preflight, inspection, verification, filename order and mtimes likewise never
create derivation edges. A schema-readable record with missing/unknown status,
or a `status: ok` apply record that fails one of the
semantic checks creates one bounded conflict item and no edge. A
schema/digest-corrupt raw record fails the entire journal snapshot with
`journal_corrupt` instead.

## Seed-centred map scope

The complete fact set is computed before pagination:

1. Re-resolve the seed paragraph from its candidate's immutable bytes.
2. Inspect every current candidate under Stage 3A scope and find every complete
   exact paragraph match to the seed. Exclude the seed's identical
   `paragraph_id` itself, but retain distinct equal paragraphs in the same
   document. Deduplicate byte-identical path aliases by paragraph identity.
3. Derive exact label/heading navigation candidates from the seed's containing
   structural section. "Containing" reuses Stage 3A's `section_by_paragraph`
   rule: the innermost active supported outline section (the stack's last
   section), with a heading paragraph belonging to the section it starts.
   Exclude the seed's identical `section_id` itself, but retain distinct
   same-document sections. Navigation candidates do not enter evidence
   traversal. If the seed has no containing structural section, emit no
   section/navigation item rather than inventing a document-level label.
4. Build all semantically valid recorded document edges from the bounded
   journal snapshot.
5. Treat every current candidate document as a component root and include its
   recorded connected component, traversed in both directions.
6. Include record-only endpoints in those components and emit one resolution
   per included document content node.
7. Emit only conflicts whose closed `conflict_endpoint_ids` intersects that
   final set. `affected_document_ids` is exactly the sorted intersection, so
   every id references an emitted document node; disconnected endpoints are
   not emitted and invalid slots never enter the endpoint set.

Traversal uses an explicit visited set and never assumes a DAG. Navigation
does not expand the map. No elapsed-time cutoff may select a subset.

## Branching, cycles, duplicate support and conflicts

- One source with several outputs preserves every outgoing branch.
- One output with several valid sources preserves every incoming edge and sets
  `multiple_parents: true`; no parent is selected automatically.
- Repeated records for the same SHA pair collapse to one edge with a complete
  supporting-record digest and bounded sample.
- Cycles and self-loops are preserved with topology flags. Traversal terminates
  through the visited set; it does not delete an edge to make a timeline.
- Record order is not a trusted chronology. Numeric ids order local journal
  frames only and may contain gaps or have been rewritten by a same-user actor.
- A missing current observation creates `record_only`, not `deleted`.
- A semantically inconsistent readable apply record creates a conflict item,
  no derivation edge, and an ambiguous affected resolution where applicable.

## Deterministic ordering and pagination

The full item set is ordered by fixed type rank, then ASCII stable id:

1. `document_node`
2. `document_observation`
3. `paragraph_node`
4. `section_node`
5. `relationship`
6. `resolution`
7. `conflict`

`full_result_set_sha256` is the `canonical_json_v1` SHA-256 of exactly
`{schema_version: round_map_item_set.v1, items: [...]}`, where `items` is that
complete ordered array of closed items before pagination. No top-level
producer, record metadata, page counters, cursor or `max_items` participates.

The opaque cursor is:

```text
rm1:<next-offset>:<binding-sha256>
```

The digest after the second colon is the `canonical_json_v1` SHA-256 of exactly:

```json
{
  "schema_version": "round_map_cursor_binding.v1",
  "next_offset": 50,
  "canonical_input": {
    "schema_version": "round_map_canonical_input.v1",
    "folder": "/validated/matter",
    "seed": {
      "schema_version": "round_map_seed.v1",
      "path": "/validated/matter/02-counterparty.docx",
      "paragraph_ref_sha256": "..."
    },
    "ordered_filenames": ["01-draft.docx", "02-counterparty.docx"]
  },
  "seed_document_id": "rm_doc_v1:...",
  "seed_paragraph_id": "rm_par_v1:...",
  "ordering_source": "filename_lexicographic_v1",
  "filename_manifest_sha256": "...",
  "filesystem_snapshot_sha256": "...",
  "journal_snapshot_sha256": "...",
  "full_result_set_sha256": "...",
  "limits_sha256": "...",
  "policy_versions": {
    "mcp_contract": "veqtor.mcp.v0.3",
    "item_schema": "round_map_item.v1",
    "reading_mode": "accepted_current_v1",
    "container_policy": "canonical_body_flow_v1",
    "search_scope": "word_document_xml_body_v1",
    "recorded_derivation_basis": "recorded_derivation_basis.v1",
    "exact_equality_basis": "exact_content_equality_basis.v1",
    "navigation_basis": "navigation_candidate_basis.v1",
    "item_order": "type_rank_then_ascii_id_v1"
  }
}
```

`paragraph_ref_sha256` is the `canonical_json_v1` SHA-256 of the complete closed
reference. `limits_sha256` is the same digest of the complete closed fixed
limits object shown below. `folder` and seed path use their validated canonical
spellings. `ordered_filenames` is always the complete effective list,
normalizing input absence/null; `ordering_source` still distinguishes computed
from explicit order. Every field is exact, required and non-null.

`cursor`, raw path spelling and `max_items` are excluded, so a caller may change
page size. `next_offset` is included: editing the visible offset without a
matching digest cannot select an arbitrary page. New relevant apply
facts, valid non-seed candidate byte drift, policy changes, or result-set
changes return `cursor_mismatch`. Seed byte drift remains the earlier
`file_sha256_mismatch`; a missing/renamed seed remains `seed_not_candidate`;
and a newly malformed/oversized candidate retains its earlier DOCX/resource
refusal. A candidate-set change between otherwise valid page calls is
`cursor_mismatch`, while a set change during one snapshot is
`workspace_changed`. In general, current-call validation failures take
precedence over comparison with an old cursor. Any syntactically valid cursor
whose digest does not match the current exact payload—including a modified
offset—also returns `cursor_mismatch`. Malformed/wrong-version cursors, or a
digest-valid offset outside `1 <= offset < eligible_item_count`, return
`invalid_cursor`. The offset uses canonical positive ASCII decimal with no
leading zero. There is no server-side cursor state.

Type-ranked pages are not individually graph-self-contained: a node may be on
an earlier page than an edge or resolution that references it. A client must
aggregate every page under the same binding before treating the graph as
complete. The single cursor prevents independently paginated graph classes; it
does not claim that one page is a complete subgraph.

Every page reports `eligible_item_count`, `returned_item_count`,
`cursor_offset`, `output_truncated`, and `next_cursor`. `null` means complete.
Elapsed time, cancellation and lock timeout never become partial successful
facts.

## Fixed resource limits

The first implementation must expose these exact ratchets in every result:

| Limit | Value |
| --- | ---: |
| candidate DOCX files | 500 |
| candidate compressed input bytes | 500 MiB |
| candidate expanded bytes | 500 MiB |
| compressed bytes per DOCX | 50 MiB |
| indexed paragraphs per DOCX | 10,000 |
| aggregate accepted/current chars per DOCX | 2,000,000 |
| apply records in bounded journal | 10,000 |
| document nodes | 10,500 |
| document observations | 500 |
| paragraph nodes | 10,001 |
| section nodes | 10,001 |
| recorded-derivation relationships | 10,000 |
| exact-equality relationships | 10,000 |
| navigation relationships | 10,000 |
| resolution items | 10,500 |
| conflict items | 10,000 |
| total map items | 70,000 |
| support/candidate sample | 20 |
| returned items per page | default 50, maximum 100 |
| journal bytes | 64 MiB |

Existing ZIP/member/XML/paragraph limits remain applicable in addition to this
table. Every boundary is inclusive. Exceeding any per-file, aggregate, graph,
record or item cap fails the whole operation with `resource_limit_exceeded`
except the journal aggregate, which retains `journal_oversize`. The result
never skips a malformed/oversized candidate or returns a cap-selected subset.
The 20-item sample exposure is not a refusal cap: longer complete lists are
digested and truncated in the sample. `max_items > 100` is `invalid_request`;
the default of 50 is not a boundary.

The document/resolution cap follows from at most 500 current observations plus
at most one newly connected document endpoint per each of 10,000 journal apply
records. The 70,000 aggregate cap is deliberately below the compatible
variant maxima and can be reached without exceeding a lower cap; unlike
120,000, it therefore has a meaningful exact-boundary acceptance fixture.

The closed `limits` object uses exactly these snake-case names and byte values,
not human-readable strings:

```json
{
  "candidate_docx_files": 500,
  "candidate_compressed_input_bytes": 524288000,
  "candidate_expanded_bytes": 524288000,
  "compressed_bytes_per_docx": 52428800,
  "indexed_paragraphs_per_docx": 10000,
  "accepted_current_chars_per_docx": 2000000,
  "journal_apply_records": 10000,
  "document_nodes": 10500,
  "document_observations": 500,
  "paragraph_nodes": 10001,
  "section_nodes": 10001,
  "recorded_derivation_relationships": 10000,
  "exact_equality_relationships": 10000,
  "navigation_relationships": 10000,
  "resolution_items": 10500,
  "conflict_items": 10000,
  "total_map_items": 70000,
  "sample_items": 20,
  "default_page_items": 50,
  "maximum_page_items": 100,
  "journal_bytes": 67108864,
  "wall_clock_partial_results": false,
  "semantic_or_vector_search": false
}
```

## Coverage and absence claims

Every current document node carries its Stage 3A declared-scope coverage. The
top-level closed coverage object contains exactly:

```json
{
  "scan_complete": true,
  "candidate_document_count": 4,
  "inspected_document_count": 4,
  "record_only_document_count": 1,
  "relevant_apply_record_count": 2,
  "eligible_derivation_record_count": 2,
  "rejected_semantic_record_count": 0,
  "eligible_item_count": 25,
  "returned_item_count": 25,
  "cursor_offset": 0,
  "output_truncated": false,
  "relationship_counts": {
    "recorded_derivation": 1,
    "exact_content_equality": 2,
    "navigation_candidate": 2
  },
  "resolution_counts": {
    "exact_unique": 2,
    "ambiguous": 0,
    "unresolved": 3
  },
  "item_type_counts": {
    "document_node": 5,
    "document_observation": 4,
    "paragraph_node": 3,
    "section_node": 3,
    "relationship": 5,
    "resolution": 5,
    "conflict": 0
  },
  "search_scope": "word_document_xml_body_v1",
  "reading_mode": "accepted_current_v1",
  "container_policy": "canonical_body_flow_v1",
  "whole_docx_coverage": false,
  "negative_whole_doc_claims": false
}
```

All fields and every nested count key shown are required non-negative
integers, except the fixed strings and booleans. `candidate_document_count`
counts direct path observations, so it equals `inspected_document_count` and
the `document_observation` item count after a successful fail-closed scan.
`relevant_apply_record_count` counts all relevant non-error apply records whose
valid edge or closed conflict endpoint set intersects the final set, including
eligible `status: ok` records and rejected missing/unknown-status or
inconsistent records; explicit `status: error` attempts are excluded.
`eligible_derivation_record_count + rejected_semantic_record_count` equals it.
Duplicate eligible support records count separately there even if they collapse
to one edge. Rejected count equals the conflict item count.

The sum of `item_type_counts` equals `eligible_item_count`; relationship and
resolution sub-counts equal their corresponding item-type counts; resolution
counts and resolution items equal document nodes. `returned_item_count` equals
the length of the current `items`; `cursor_offset + returned_item_count` is at
most `eligible_item_count`; and `output_truncated` is true exactly when
`next_cursor` is non-null. `record_only_document_count` is at most document
nodes. These are output-contract invariants, not optional diagnostics.

Positive equality facts inside included canonical body flow remain valid when
other containers/parts are excluded. A zero match means only no match in the
declared inspected scope. If coverage prunes a subtree, the reason becomes
`declared_scope_incomplete`; the map never infers deletion or whole-DOCX
absence. Headers, footers, footnotes, endnotes, comments, text boxes,
altChunk, AlternateContent and other Stage 3A exclusions retain their existing
meaning.

Malformed or unsupported candidate packages fail closed; unlike
`list_rounds`, Round Map has no per-file `skipped` success path.

## Privacy-minimized provenance

### Live result

The live response may contain the caller-visible current paths, filenames,
position-only round ids, labels and headings shown in the item schemas. It
never contains paragraph/contract body text, snippets, edit text, raw journal
inputs, stored paths from journal records, raw errors, or author strings.

### Future raw `round_map.v1` record

The private raw JSONL record may retain the canonical workspace and caller
input under the existing private-local policy. Its stored `result` is a bounded
Round Map summary containing exactly `status: ok`, the path-free closed seed,
`ordering_source`, `filename_manifest_sha256`, the closed snapshot, coverage
and limits, `next_cursor_sha256` (nullable), and `items_summary`. The latter is
exactly `{count, sha256, sample, truncated}`; its sample is capped at 20 and
each entry is exactly `{item_type, id, item_sha256}`. It is not the complete
live page.

`items_summary` covers the returned page, not the hidden complete set:
`count == returned_item_count`; `sha256` is the `canonical_json_v1` SHA-256 of
exactly `{schema_version: round_map_returned_items.v1, items: [...]}` using the
page's ordered complete closed items. The sample is the first at most 20 page
items in that same order. Each `item_sha256` digests the corresponding complete
closed item with `canonical_json_v1`; `truncated` is true exactly when count is
larger than sample length. Complete-set identity remains the separate
`full_result_set_sha256`.

`next_cursor_sha256` is null when the live cursor is null; otherwise it is the
`canonical_json_v1` SHA-256 of exactly `{next_cursor: <complete live cursor>}`.
`tool_result_sha256` fingerprints the complete normalized live operation result
before record metadata; `result_sha256` fingerprints the stored summary.

### Compact export

The future compact projection retains the existing `decision_record.v1`
compact envelope, including its outer `record_id`, because that id is required
for journal identity and pagination. For `(map_rounds, round_map.v1)` it emits
exactly `{input, result, provenance}` projections as follows:

- `input` reuses the existing compact path-digest envelope exactly:
  `{sha256, omitted: true}`. Here `sha256` digests the complete canonical raw
  input; it contains no individual input values.
- `result` is the exact stored summary above, except every sample entry remains
  only `{item_type, id, item_sha256}`; supporting raw record ids cannot appear.
- `provenance` is exactly `{filesystem_snapshot_sha256,
  journal_snapshot_sha256, full_result_set_sha256, reading_mode,
  container_policy, search_scope}`.

Within those three projections it must omit or digest:

- workspace paths, seed path, observation paths and filenames;
- `ordered_filenames` and caller input verbatim;
- labels, headings, paragraph/contract text and snippets;
- embedded supporting-record ids, raw record payloads, edit text, authors and
  errors. This does not remove the outer compact envelope's own `record_id`.

The existing compact envelope also retains its already documented producer
identity, digested workspace, and normalized `created_at` metadata (with a
digest only for an invalid legacy timestamp). The new projection adds no
paths, verbatim input, contract text or human-readable navigation strings. Raw
journal privacy and compact-export privacy remain separate assurances.

The compact projection is not a reconstruction of the live result and does
not upgrade the local journal to tamper-evident provenance.

## Failure semantics

| Phase | Condition | Outcome |
| --- | --- | --- |
| Input | malformed top-level argument/seed envelope | `invalid_request` |
| Input | malformed complete `paragraph_ref.v1` | `invalid_reference` |
| Input | malformed or wrong-version cursor | `invalid_cursor` |
| Workspace | missing/not directory/unreadable/identity drift | `workspace_missing`, `workspace_not_directory`, `workspace_unreadable`, or `workspace_changed` |
| Candidate | symlink, non-file or hard link | `unsafe_candidate` |
| Order | incomplete/duplicate/extra manifest | `invalid_round_order` |
| Input | seed path not a direct candidate | `seed_not_candidate` |
| Candidate | bytes cannot be opened/read | `file_unreadable` |
| Filesystem | candidate set changes during snapshot | `workspace_changed` |
| DOCX | malformed package/XML | `invalid_docx` |
| DOCX | missing main document part | `missing_document_part` |
| DOCX | ambiguous ZIP structure | `file_unextractable` |
| DOCX | unsupported compression | `unsupported_compression` |
| DOCX | encrypted/masked member | `encrypted_docx` |
| Resource | non-journal limit exceeded | `resource_limit_exceeded` |
| Seed | file hash drift | `file_sha256_mismatch` |
| Seed | valid ref no longer resolves | `reference_mismatch` |
| Evidence | equal SHA but unequal fully compared text | `evidence_consistency_error`, fail closed |
| Journal snapshot | live lock holder | `journal_busy`, fail closed |
| Journal snapshot | corrupt frame/schema/digest | `journal_corrupt`, fail closed |
| Journal snapshot | aggregate above 64 MiB | `journal_oversize`, fail closed |
| Journal snapshot | unsafe sidecar/journal object | `sidecar_symlink`, `sidecar_not_directory`, `journal_symlink`, `journal_not_file`, or `journal_hardlink`, fail closed |
| Semantic record | readable but inconsistent apply facts | conflict item, no edge |
| Cursor | otherwise-valid file/set, relevant journal, policy or result drift between pages | `cursor_mismatch` |
| Cursor | digest-valid offset outside `1 <= offset < eligible_item_count` | `invalid_cursor` |
| Output | result violates closed contract | `output_contract_error`, fail closed |
| Map record publication | failure after valid snapshots/result | successful map plus `record_status: write_failed` |
| Map record publication | `VEQTOR_DISABLE_DECISION_RECORD` enabled | successful map plus `record_id: null`, `record_status: disabled` |

The deterministic phase order is: validate argument/reference/cursor syntax;
open and enumerate the workspace; enforce candidate count and safe entry
types; validate order and seed membership; capture candidate bytes under the
compressed-byte cap; recheck the candidate set; parse candidates in manifest
order under remaining DOCX caps; re-resolve the seed and compare evidence;
validate/classify the journal; assemble the complete item set; compare a cursor
binding; validate output; then attempt publication. Within candidate parsing,
the first failing manifest position supplies the code. `invalid_request`
applies to the top-level/seed envelope; once that envelope is valid, a malformed
nested reference is always `invalid_reference`. All other DOCX parser failures
are normalized to `invalid_docx`; raw parser/library exception types are never
part of the contract.

Refusals are MCP tool errors, not successful result-union objects. The stable
code is the exception code and prefixes the sanitized message; no candidate
path or document text is required in that message. Consequently producer and
record metadata are guaranteed on successful live responses, not on a refused
transport call.

Publication is ordinary-tool fail-open behavior: once all map facts are bound
to valid snapshots, failure to append the map's own provenance does not erase
the read result. `record_error` is the stable publication code only and is
present exactly for `write_failed`; it is absent for `written` and `disabled`.
This does not permit fail-open when acquiring or validating the journal
snapshot used to build the map.

## Compatibility decisions

- `paragraph_ref.v1`, `section_ref.v1`, `accepted_current_v1`,
  `canonical_body_flow_v1`, and `word_document_xml_body_v1` are reused without
  forks or aliases.
- Historical `inspection.v1` records remain readable, including normalized
  legacy outer coverage counters, but never create derivation edges.
- The frozen golden `exact` profile and the published v0.1.2 preflightless
  semantic profile remain eligible as two distinct, exact legacy profiles.
  Newer fields strengthen validation when present and are never synthesized
  into old records; partial strengthened fields are conflicts.
- Historical raw records and golden fixtures are never rewritten.
- v0.2 two-field anchors remain supported by their existing workflows but are
  not Round Map seeds.
- Until implementation, the MCP and installed-wheel inventories remain seven
  tools and the journal registry has no `(map_rounds, round_map.v1)` pair.
- A later implementation that registers the new permanent pair will retain the
  documented v1 downgrade boundary: older readers may reject a journal
  containing an unknown tool pair.
- Top-level results are additive; nested seeds, refs, nodes, relationships,
  bases, resolutions, conflicts, snapshots, coverage and limits are closed.

## Acceptance fixtures for later implementation

All fixtures must be synthetic and redistributable. The implementation is not
accepted until at least these cases pass:

1. A current valid Veqtor apply record produces one document-level
   `recorded_derivation` edge with current support profile.
2. The frozen golden record and
   `tests/data/round-map-v0.1.2-preflightless-apply-record.json` each produce
   the same edge class with their disclosed distinct profile. With fixture
   source A as a current root and no exact/navigation candidates, the v0.1.2
   record produces exactly A → B, `published_v0_1_2_only`, and B's sole
   resolution is `unresolved/record_only_document` with zero conflicts. Adding
   any one-to-five strengthened slots changes it to one
   `unsupported_legacy_profile` conflict; all six present with a disagreeing
   pair changes it to `strengthened_fact_mismatch`. Both cases emit no edge.
3. A failed apply and a successful preflight create no derivation edge.
4. Result/provenance output-hash disagreement creates one conflict and no edge.
   In the normative A/current, C/source, B/provenance-output,
   A/result-output fixture, only A is affected/emitted; its resolution is
   `ambiguous/recorded_fact_conflict`, B and C do not expand the graph, the raw
   record enters the journal snapshot digest, and the conflict plus resolution
   enter the full-result-set/cursor digest.
5. Two independently created DOCX files with equal complete paragraph text
   create only `exact_content_equality`, never recorded derivation; the seed
   paragraph/section never creates a self-candidate edge.
6. Equal hashes are not trusted without a full-text comparison test seam.
7. Filename `round-1`/`round-2`, mtimes and an explicit manifest remain
   position-only and create no chronology or lineage.
8. Duplicate paragraph text in one document is `ambiguous`; duplicate labels
   and headings never trigger an arbitrary choice.
9. One navigation-only candidate remains `unresolved`; several remain
   `unresolved/navigation_only` facts without a selected paragraph.
10. A document with no exact or navigation candidate is `unresolved`, never
    `deleted`.
11. Identical DOCX bytes at two paths produce one document node, two
    observations and one paragraph identity.
12. Renaming changes only the observation identity. Changing bytes creates a
    new document/paragraph identity and makes the old seed stale.
13. One source with multiple outputs preserves all branches.
14. One output with multiple valid parents preserves all edges and sets
    `multiple_parents` without choosing one.
15. Duplicate supporting apply records collapse to one edge with count/digest.
16. A derivation cycle and self-loop terminate safely and remain visible.
17. A missing recorded output remains `record_only`; a renamed byte-identical
    output still in the candidate set is `current_and_recorded` with a new
    observation. Neither case is deletion.
18. Valid non-seed file drift or a newly relevant apply record between pages
    yields `cursor_mismatch`; so does a stable changed candidate set when the
    seed remains present. Seed drift is `file_sha256_mismatch`, a missing seed
    is `seed_not_candidate`, and malformed/oversized replacement bytes keep
    their earlier DOCX/resource code. In-call set races are `workspace_changed`.
19. A new valid, within-cap non-apply record or the map's own record between
    pages does not invalidate the cursor, including absent-journal to
    own-record-only creation. Corruption/oversize still refuses before cursor
    comparison.
20. Root/journal contention, corruption and aggregate oversize fail closed
    before a map snapshot; publication contention after a valid result is
    visible as `write_failed` without changing facts.
21. One malformed, encrypted, unsupported or resource-exceeding candidate
    refuses the whole map; a symlink or hard-linked candidate is
    `unsafe_candidate`; no candidate is silently skipped.
22. Excluded containers/parts produce no false match or negative whole-DOCX
    claim; positive in-scope exact equality remains valid.
23. Every refusing resource/input cap succeeds exactly at its boundary and
    refuses one over. A 21-item support/candidate list succeeds with a
    20-item sample and `truncated: true`; the default page size is not treated
    as a refusal boundary. Cancellation never becomes a partial success.
24. Pagination has no overlap/omission, permits a changed page size and binds
    the complete ordered result set.
25. Compact export contains no paths, filenames, labels, headings, snippets,
    paragraph/contract text, edit text or verbatim caller input.
26. Existing Stage 3A, v0.2-supported, golden-journal, seven-tool
    installed-wheel and package-reproducibility workflows do not regress.
27. English product acceptance demonstrates exact equality, recorded
    derivation and an explicit unresolved/ambiguous case. Russian coverage is
    limited to raw Unicode preservation and is not a clause-discovery claim.

## Product claim boundaries and non-goals

The first slice is read-only. It does not:

- accept/reject revisions, normalize history or create a sendable DOCX;
- infer semantic clause identity or use semantic/vector search;
- prove a trusted timestamp, actor, approval, custody, journal integrity or
  complete negotiation history;
- convert position, filename, mtime, labels, headings or similarity into
  chronology/lineage;
- claim paragraph lineage from a document-level apply record;
- assert first appearance, deletion, restoration or countering without a later
  evidence contract that proves those events;
- inspect outside the declared Stage 3A main-body container/part policy;
- auto-repair, rotate or authenticate the local journal; or
- claim Russian-language product acceptance.

## Definition of done for the later core

Stage 3B core will be ready for an exact-build product gate only when the
future implementation matches every closed schema and failure boundary above,
all acceptance fixtures and full local quality gates pass, an installed wheel
exposes the intended eight-tool surface, compact privacy is verified, and the
exact artifact passes a separate English Claude Desktop acceptance. None of
those later gates is satisfied merely by committing this specification.
