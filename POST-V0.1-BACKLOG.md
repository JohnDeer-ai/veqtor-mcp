<!-- SPDX-License-Identifier: Apache-2.0 -->

# Post-v0.1 shadow backlog

This is the working product backlog behind the public roadmap. It records
reproducible follow-ups and open product decisions without presenting them as
public commitments. `ROADMAP.md` remains the source for public direction.

## P0 — local provenance liveness

- Completed in the journal foundation batch: every lock path uses one bounded
  monotonic deadline and stable `journal_busy`; ordinary tools fail open while
  read/export before a trustworthy snapshot fail closed.
- Completed: process-level contention, unlock recovery, unique-id, corruption,
  streaming-page, aggregate-oversize, exact-boundary and already-published
  apply-output tests cover the operational envelope.
- Completed: the aggregate journal cap is 64 MiB and recovery is a manual,
  non-destructive archive. Automatic rotation/indexing remains future work.

## P1 — provenance projection clarity

- Completed in the Stage 3A contract-polish batch: every successful live tool
  response now carries the same bounded `producer` identity block, including
  list, extract, inspect, verify and export responses.
- Give bounded samples an explicit policy/reason field, for example
  `sample_policy: observed_bounded | asserted_input_omitted`. Today an empty
  `sample` with `truncated: true` is overloaded: for successful preflight and
  apply it means intentional omission of client-asserted anchors, not that an
  observed sample was lost to a size limit.
- Consider a location-independent operation-result fingerprint in addition to
  the existing `result_sha256` and `tool_result_sha256`. The current full-result
  fingerprint can include the caller's path spelling, so it is useful for
  replay/debugging but is not a pure document-content identity.
- Define a migration story for path-normalization changes in legacy local
  journals. New records should use one canonical workspace identity, while old
  relative-path records remain historical data rather than being rewritten.

## P1 — Desktop activation diagnostics

- Add a small health/diagnostic surface that distinguishes extension disabled,
  MCP server not connected, request never dispatched, server busy and server
  failure. A Claude Desktop transport timeout must not be presented as a
  Veqtor document-processing fact.
- Keep a bounded read-only single-file diagnostic for separating a genuinely
  slow folder scan from client transport failure, without weakening fail-closed
  write behavior.

## P2 — tracked-change metadata

- Explore an optional caller-supplied revision timestamp that is validated,
  proof-bound and deterministic between preflight and apply. Do not substitute
  the server wall clock silently. Until then, new `w:ins`/`w:del` revisions
  intentionally omit `w:date`, while the decision record remains the action
  clock.
- Preserve source authors exactly, including unusual numeric Word author
  strings. If a friendlier public demo is desired, keep the adversarial numeric
  case in a focused fixture instead of weakening metadata-preservation tests.

## Stage 3 product value

### Stage 3A — clause evidence (`inspect_document` v0.3)

The implemented development slice is specified in
[`INSPECT_DOCUMENT_V0.3.md`](INSPECT_DOCUMENT_V0.3.md). Exact commit
`d2fc8fb30819581e4b795b49c5f7831dff3a81fa` completed its code, package, CI and
Claude Desktop acceptance. That evidence does not accept later polish commits
and is not a published v0.3 commitment or release claim.

- Add one read-only, closed `inspect_document` contract for finding and citing
  unchanged main-body text. Do not call a mechanical tracked-change projection
  legally operative text; v0.3 names the projection `accepted_current_v1`.
- Enumerate paragraphs only through `canonical_body_flow_v1`. Pass through
  supported SDT content and table rows/cells in XML order, including supported
  nesting, without using unrestricted descendant traversal or synthesizing
  table delimiters.
- Exclude text boxes, altChunk, block custom XML, AlternateContent, unknown
  block containers, malformed text/revision payload inside property subtrees
  and non-main parts. Report a fixed `container_policy` and complete coverage
  counts so a successful body inspection cannot be mistaken for whole-DOCX
  text coverage.
- Support only the closed `outline`, `literal_search`, `browse` and `read`
  modes. Outline omits paragraph text; literal search accepts bounded phrases
  and an explicit match basis and returns snippets/anchors; browse uses
  deterministic source-bound cursor pagination; read returns complete selected
  paragraph or section text.
- Use path-free, hash-bound `paragraph_ref.v1` and `section_ref.v1` observations
  as the evidence anchors. Bind exact file bytes, canonical position, reading
  policy and text hash; labels and headings remain navigation-only facts.
- Add a container-aware `revision_inventory.v2` partition across in-scope,
  unsupported and excluded-container revision occurrences. Keep change-unit
  grouping outside the revision-element partition.
- Apply deterministic paragraph, character, phrase, hit, browse-page and read
  caps. The tool contract has no wall-clock cutoff that selects a partial
  success; transport cancellation does not establish a document fact.
- Gate schema-less v0.2 change-unit anchors with canonical container analysis.
  They remain usable only when `legacy_two_field_anchor_safe` is true: zero
  compatibility-impacting excluded/unknown subtrees, including plain-text
  containers. Otherwise quote verification, preflight and apply return
  `legacy_anchor_ambiguous`; apply repeats the gate independently.
- Scope product acceptance to English. Exact Unicode preservation is still a
  mechanical invariant, but Russian heading grammar, morphology and localized
  clause discovery are not v0.3 claims.
- Do not enable paragraph-anchor writes in the first slice. Reusing these
  anchors for new tracked changes requires its own proof-bound write review.
- Definition of done requires only synthetic redistributable fixtures covering
  plain body flow, accepted/current composition, SDT/table nesting, excluded
  containers and parts, outline text omission, literal-search snippets, browse
  cursors, complete reads, positional convergence, anchor drift, both sides of
  the legacy ambiguity gate, inventory equations, every cap, no-partial
  transport behavior, compact provenance and an English Claude citation
  workflow.

### Stage 3B — bounded round map

The closed, non-implemented first-slice contract is specified in
[`ROUND_MAP_V0.3.md`](ROUND_MAP_V0.3.md). It defines future tool `map_rounds`
and record pair `(map_rounds, round_map.v1)` without registering either in the
current seven-tool runtime.

- Classify every relationship by its actual claim and basis: a semantically
  valid local apply record supports document-level `recorded_derivation`;
  complete exact paragraph comparison supports `exact_content_equality`; and
  labels/headings remain `navigation_candidate` facts only.
- Keep `unresolved` and `ambiguous` as explicit resolution states rather than
  fake edges or confidence percentages. Filename order and explicit manifests
  remain position-only and never become chronology or paragraph lineage.

### Stage 3C — validate the next write job

- Validate whether external users primarily need evidence-preserving
  negotiation history or a clean sendable redline before adding another write
  mode.
- Keep the existing history-preserving ledger separate from a future sendable
  deliverable. The latter needs an explicit baseline and complete
  accept/reject/normalize dispositions, a new output artifact and its own
  preflight proof; it must never silently collapse prior negotiation evidence.
