<!-- SPDX-License-Identifier: Apache-2.0 -->

# Post-v0.1 shadow backlog

This is the working product backlog behind the public roadmap. It records
reproducible follow-ups and open product decisions without presenting them as
public commitments. `ROADMAP.md` remains the source for public direction.

## P0 — local provenance liveness

- Replace indefinite blocking `flock` acquisition with one bounded,
  monotonic-deadline helper for journal initialization, append, read and
  export.
- Return a stable `journal_busy` outcome. Ordinary document tools should keep
  the completed core result and report `record_status: write_failed`; export
  should remain fail-closed because it cannot produce a trustworthy snapshot.
- Add process-level contention tests covering every lock path, recovery after
  unlock, no duplicate records and the case where an apply output already
  exists before provenance publication finishes.
- Add an aggregate journal-size policy and rotation/archive design. The current
  append-only journal has no size cap.

## P1 — provenance projection clarity

- Add the same bounded `producer` identity block to live `list_rounds`,
  `extract_redlines` and `verify_quote` responses that preflight/apply already
  return. A read result copied out of its later journal export should still be
  attributable to the package version and Python-source snapshot that produced
  it.
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
[`INSPECT_DOCUMENT_V0.3.md`](INSPECT_DOCUMENT_V0.3.md). It remains a
shadow-backlog implementation on the `0.3.0.dev0` development line, not a
published v0.3 commitment, release claim or Claude Desktop acceptance claim.

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

- Build the map only after paragraph evidence is stable. Classify every link by
  its actual claim and basis: a Veqtor source/output record may prove
  derivation; an exact text hash proves content equality but not derivation;
  labels or similarity remain navigation-only candidates.
- Filename order, an explicit positional manifest and a generic confidence
  score must never become implicit chronology or lineage proof. Preserve
  ambiguous and unresolved candidates instead of forcing one chain.

### Stage 3C — validate the next write job

- Validate whether external users primarily need evidence-preserving
  negotiation history or a clean sendable redline before adding another write
  mode.
- Keep the existing history-preserving ledger separate from a future sendable
  deliverable. The latter needs an explicit baseline and complete
  accept/reject/normalize dispositions, a new output artifact and its own
  preflight proof; it must never silently collapse prior negotiation evidence.
