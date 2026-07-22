<!-- SPDX-License-Identifier: Apache-2.0 -->

# Known limitations

This file describes development source `0.3.0.dev0` and draft MCP contract
`veqtor.mcp.v0.3`. It does not establish that a `0.3` package, extension or
release exists. Published installation status comes only from matching entries
on PyPI and the immutable GitHub Releases list. The frozen v0.2 release contract
and this development surface remain intentionally narrow.

The frozen v0.2 MCPB v0.4 extension is macOS-only and is public only when the
exact artifact is attached to the matching verified release after clean-Mac
acceptance. Development source `0.3.0.dev0` has no corresponding MCPB. Linux
keeps the CLI setup. There is no Windows extension, catalog listing, automatic
update promise, silent installation or guaranteed in-app rollback. If
published, `0.2.0` is the first public MCPB and has no older public extension to
restore; real upgrade and rollback testing starts with the next extension
release. The first UV activation may download a compatible Python runtime and
locked dependencies, so it is not guaranteed to work offline. MCPB installation
does not add an operating-system filesystem sandbox; Veqtor runs with the
current user's permissions.

## Documents and revisions

- Resource limits are part of the supported contract. Veqtor refuses a DOCX
  larger than 50 MiB, more than 2,000 ZIP members, more than 100 MiB total
  expanded content, an XML member larger than 25 MiB, another member larger
  than 50 MiB, a ZIP central directory larger than 4 MiB, or a member above
  10 MiB whose expansion ratio exceeds 200:1. A generated candidate must also
  remain at or below 50 MiB. Parsed XML parts are limited to 100,000 structural
  items (elements, attributes, namespace declarations, comments and processing
  instructions), and one extraction is limited to 10,000 change units. A
  `list_rounds` scan is limited to 500 candidate DOCX files, 500 MiB of
  aggregate candidate input and 500 MiB of aggregate actual expanded output.
  DEFLATED decoder output and STORED direct-span bytes are charged even if that
  file is later skipped; packages refused during container preflight before any
  member-output processing consume no expanded-output budget.
  Exceeding the shared scan budget refuses the whole call without a partial
  result, so large round folders must be split before retrying.
  Image-heavy, unusually complex or very large legitimate matters may
  therefore be refused. The Alpha does not expose an override for these safety
  defaults. Declared metadata is bounded before decoder creation. Every member,
  including parts a particular tool does not otherwise consume, has its actual
  output, aggregate output and CRC checked: DEFLATED data is streamed in bounded
  chunks with an exact end-of-stream check, while STORED data is a bounded
  direct span.
- The supported DOCX container subset is intentionally narrow: unencrypted,
  non-ZIP64 ZIP packages using only `STORED` or `DEFLATED` members. Standard
  32-bit data descriptors are supported with or without their optional
  signature. LZMA, BZIP2, Zstandard, unknown methods, ZIP64 members, descriptor
  mismatches, or disagreement between local and central raw names, flags,
  methods, CRCs or sizes are refused, as are prefixes, gaps, overlaps and
  trailing compressed data. Other well-formed non-ZIP64 extra fields may differ
  between the local and central records.
- XML parts containing a `DOCTYPE` declaration are refused; Veqtor does not
  load DTDs or expand custom XML entities.
- ZIP packages with duplicate member names are refused as ambiguous; no tool
  silently chooses one duplicate OPC part over another.
- Extraction and writing cover `word/document.xml`. `inspect_document` is
  narrower still: it exposes only the canonical main-body paragraph flow.
  Comments, headers, footers, footnotes and endnotes are not analyzed or edited.
- `inspect_document` and `extract_redlines` require a `w:document` root with
  exactly one direct `w:body`; preflight and apply inherit that refusal before
  editing. `list_rounds` is a bounded scanner and does not make the same full
  body-structure claim.
- Relationship-backed `w:altChunk` content is excluded rather than imported.
  Live inspection and the private raw journal disclose internal target parts as
  package-relative exclusions; external target URLs are not returned. Compact
  export replaces document-controlled target names with a count and full-list
  digest, and always leaves their sample empty. Missing, ambiguous or unsafe
  internal targets refuse inspection.
- Formatting, move, paragraph-mark and structural revision categories are
  counted but not all are converted into editable change units.
- The extractor and inspector report `revision_inventory.v2` so callers can
  check both partitions: `total_revision_elements ==
  in_scope_revision_elements + excluded_container_occurrences`, and
  `in_scope_revision_elements == decoded_revision_elements +
  unsupported_revision_occurrences`. `emitted_change_unit_count` is separate:
  one change unit may represent multiple decoded text-revision elements, so it
  is not another side of either partition. V2 also discloses the canonical
  container/body-flow coverage used for text-bearing revision classification.
- `inspect_document` is bounded retrieval, not semantic contract analysis. Its
  outline, literal-search, browse and read modes do not decide clause meaning,
  infer omitted concepts or search outside the declared main-body scope.
- `accepted_current_v1` does not analyze Word hidden-text formatting such as
  `w:rPr/w:vanish`. Text carrying that formatting remains in the mechanical
  reading and can pass anchored `verify_quote`; callers must not treat the
  reading as a visual-rendering or legal-effect conclusion.
- Complex adjacent or nested OOXML layouts may be refused with a stable error
  rather than rewritten approximately.
- Tracked text revisions may be nested at most two levels. This supports the
  ordinary counter shape (a deletion inside a pending insertion) while deeper
  recursive revision nesting is refused.
- Cyclic paragraph-style inheritance is refused rather than resolved
  approximately.
- Numbering is a navigation aid, not evidence. Numbering templates, computed
  labels and explicit manual labels are capped at 256 characters, and Roman
  labels are supported only for values 1-3999. Word numbering levels 0-8 are
  supported; labels outside those bounds are omitted. Word outline levels 0-8
  create sections, level 9 means body text, and malformed out-of-range outline
  values refuse inspection rather than becoming invalid navigation facts.
- Revision ids are provenance, not unique document addresses. Edits are bound
  to hash-scoped paragraph/group positions and a full change-unit fingerprint;
  duplicate ids are handled structurally. New-id allocation supports ASCII
  decimal spellings of one to ten digits with values through `2147483647` and
  otherwise refuses the document. Leading zeroes are supported only within the
  ten-digit lexical limit. An edit batch is refused atomically if all revision
  ids it needs cannot be allocated within that range.
- There is no accept/reject operation and no silent text rewrite.
- New revisions carry no automatic `w:date`; the decision record is the action
  clock.

## Negotiation interpretation

- The default round order is disclosed as `filename_lexicographic_v1`:
  case-insensitive lexicographic filename order with the exact filename as a
  tie-break. It is not natural-number sorting, chronology or lineage proof. A
  caller may instead supply `ordered_filenames`, but it must name every candidate
  DOCX exactly once and remains only an explicit positional manifest.
- Round Map performs complete exact paragraph comparison in the declared
  main-body scope; it has no semantic, fuzzy, translated, morphological or
  vector clause matcher and no authorship forensics. Its
  `recorded_derivation` edges repeat validated local apply-record assertions
  about document bytes only. They do not prove paragraph lineage, chronology,
  approval, custody, deletion, restoration or first appearance.
- Round Map's permanent `round_map.v1` journal projection is success-only.
  Pre-result Map refusals do not append a failure record or initialize
  `.veqtor`; after a valid map exists, an append failure is reported as
  `record_status: "write_failed"` without discarding the map. The other seven
  tools retain their documented controlled-failure journaling behavior.
- `clause_anchor` and `manual_label` are best-effort navigation aids. Durable
  evidence remains an exact file SHA plus either a complete change-unit anchor
  and verified old/new wording or a hash-bound paragraph reference and verified
  accepted/current projected wording.
- The calling model supplies legal analysis and drafting. Veqtor does not
  establish legal correctness.

## Preflight and apply

- One atomic batch accepts at most 100 edits, at most 20,000 characters of new
  text per edit, and at most 200,000 new characters across the batch. A limit
  refusal writes no output and applies no partial edit.
- A successful preflight proves the shared document-processing pipeline for the
  same source bytes, build, configured author and edits. Apply can still fail if
  the source changes, the output exists, or publication encounters permissions,
  storage or filesystem races.
- Under MCP contract `veqtor.mcp.v0.2` and draft `veqtor.mcp.v0.3`,
  `apply_edits` requires the complete `preflight_proof` returned by a successful
  preflight. The proof binds the source SHA-256, canonical edits digest,
  configured author, producer build and candidate SHA-256; it does not bind the
  destination path. It is an unkeyed drift detector, not authentication, a
  digital signature, a trusted timestamp or proof of who approved the edit. The
  lower-level Python API keeps its v0.1 optional-proof behavior for
  compatibility.
- Development preflight diagnostics use closed `position_status` values
  (`supported`, `unsupported`, `not_evaluated`) and an explicit
  `failure_phase`; other diagnostic facts can still be `null` when processing
  never reached the phase that could establish them. A refusal normally stops
  at the first blocker, so the response is not a complete list of every possible
  blocker in the batch.
- The historical paired counter/reinstate hang report has not been reproduced.
  A 14-operation regression of that shape now reaches a terminal structured
  `edits_overlap` result, but Veqtor does not yet promise a general planner
  timeout, cancellation API or hard wall-clock completion bound.
- Preflight does not create an output DOCX, but normally writes a local
  provenance record under `.veqtor`.
- Author identity is fixed at server start. If it matches a counterparty's
  revision author, some counter/reinstate requests are treated as edits to the
  configured party's own revision and refused.
- Reinstate is a visible tracked insertion before the preserved counterparty
  deletion. It does not perform Word Reject and does not remove that deletion.
- Matching uses the same current-reading atoms as extraction, including tabs,
  breaks and non-breaking hyphens. A span that touches one of those element
  atoms is identified truthfully but refused as `unsupported_run_shape` when
  the v0.1 writer cannot preserve its OOXML semantics.

## Provenance and security

- Decision records are best-effort local provenance, not tamper-evident,
  authenticated, signed or hash-chained audit records.
- The threat model is a non-hostile single-user macOS/Linux workspace. A
  malicious process running as the same user is outside scope.
- The raw journal retains the canonical workspace and caller-supplied paths,
  may retain literal-search phrases and other matter text, and has no automatic
  rotation. Only compact export provides the documented path/phrase
  minimization. The aggregate journal cap is 64 MiB; a larger historical file
  is refused with `journal_oversize` until it is moved aside for manual archive.
- Decision-record workspace and journal locks share a fixed one-second
  monotonic deadline and return `journal_busy` on contention. This bounds lock
  waiting, not total tool runtime or filesystem syscalls. Ordinary document
  operations keep a completed core result with `record_status: write_failed`;
  read/export before a validated snapshot fail closed.
- Reads retain only the requested bounded page but still validate the complete
  journal. Append and export also scan the complete file, so they remain
  O(journal bytes); there is no journal index, automatic repair or rotation.
- Read-only `list_rounds`, `extract_redlines`, `inspect_document`,
  `verify_quote` and `preflight_edits` calls normally append local provenance
  too. Decision-record export normally appends an access event after taking its
  response snapshot, so observation is not side-effect free unless decision
  records are disabled.
- Development-contract export never initializes a journal in an uninitialized
  supplied folder. If exactly one direct child contains a valid journal it
  refuses with `workspace_mismatch` and a safe relative suggestion; multiple
  children refuse as `workspace_ambiguous`. Discovery is limited to direct
  children, excludes the service `.veqtor` directory, and has a 500-entry plus
  cooperative one-second elapsed-time budget checked between filesystem
  operations. The time check cannot interrupt a blocked syscall and is not a
  hard timeout. Hitting a discovery bound refuses as incomplete rather than
  guessing or scanning outside the supplied root.
- Access-event summaries written by builds before 0.1.1 may undercount prior
  access events when multiple exports ran concurrently. Existing journal
  entries are historical evidence and are not rewritten or migrated.
- Journals containing `preflight_edits` require Veqtor 0.1.0 or newer. Downgrade
  to 0.0.0 is unsupported.

## Platforms and delivery

- Supported: macOS/Linux, Python 3.12-3.14, local stdio MCP.
- The public Alpha is community-supported. Security fixes are provided only
  for the latest tagged Alpha; response and fix times are not guaranteed.
- Windows, hosted MCP, OAuth, a custom UI and SLA-backed support are not part of
  v0.1.
