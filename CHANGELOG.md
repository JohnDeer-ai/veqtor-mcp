<!-- SPDX-License-Identifier: Apache-2.0 -->

# Changelog

All notable changes to Veqtor MCP are documented here.
Publication dates are authoritative in each version's immutable GitHub Release
through its `published_at` timestamp.

## 0.3.0

Veqtor v0.3.0 Alpha release contents. The immutable GitHub Release
`published_at` timestamp, not this file, is authoritative for whether and when
publication occurred.

### Added

- The Stage 3B `map_rounds` tool: a bounded seed-centred evidence map with
  recorded document-byte derivation, complete exact paragraph equality,
  navigation-only candidates, explicit uncertainty, stateless snapshot-bound
  pagination, and privacy-minimized compact provenance.
- Draft MCP contract `veqtor.mcp.v0.3` and the read-only `inspect_document`
  tool for bounded outline, literal-search, browse and read views over canonical
  main-body text, with hash-bound paragraph and section references.
- `change_unit_anchor.v2` and `revision_inventory.v2` so evidence and revision
  coverage disclose the canonical container/body-flow policy they depend on.

### Changed

- Decision-record workspace and journal locks now share a fixed bounded
  monotonic deadline with stable `journal_busy` outcomes. Ordinary tools keep
  completed results, while direct reads and pre-snapshot export fail closed.
- Journal scans validate the complete stream while retaining only the bounded
  response page. A fixed 64 MiB aggregate cap returns `journal_oversize`,
  blocks mutation, and leaves recovery to non-destructive manual archive;
  post-snapshot export publication failures return the frozen snapshot.
- Successful live responses from all eight tools now carry the
  same bounded producer identity.
- Inspection coverage now names its outer canonical-flow totals
  `indexed_paragraph_count` and `nonempty_indexed_paragraph_count`; compact
  export reads and normalizes the complete historical counter pair without
  rewriting raw journals, while mixed or inconsistent generations fail safe.
- Evidence documentation now states that full paragraph/section references,
  not heading hashes alone, provide identity and that search snippets remain
  navigation output even when untruncated.
- Package identity is `0.3.0`; publication still requires the exact release
  contract, clean-Mac MCPB acceptance and immutable promotion workflow.
- Release CI builds and smokes wheel, sdist and the eight-tool MCPB surface,
  then independently reproduces the sealed release set.
- Compact inspection-record projection remains readable when a historical
  base-schema record lacks the closed v0.3 mode, coverage or limits fields, and
  incomplete v2 anchors are omitted from observed samples as truncated.
- Relationship-backed `w:altChunk` content is now a fail-visible canonical-flow
  exclusion; safe internal target parts are disclosed without returning
  external URLs.
- Compact export replaces document-controlled internal `altChunk` part names
  with a constant-size count/digest projection and an always-empty sample.
- Inspection and extraction now share one fail-closed `w:document` / single
  direct `w:body` structural gate; preflight and apply inherit it before edits.
- Documentation now consistently describes `accepted_current_v1` as a
  mechanical projection, discloses that `w:rPr/w:vanish` is not interpreted,
  and distinguishes private raw-journal payloads from compact export.

## 0.2.0

Veqtor v0.2.0 Alpha release contents. The immutable GitHub Release
`published_at` timestamp, not this file, is authoritative for whether and when
publication occurred.

### Added

- A deterministic macOS Claude Desktop Extension build using MCPB manifest v0.4
  and the host-managed UV runtime. The extension requires explicit
  tracked-change-author configuration, declares the six existing tools and
  includes the deterministic four-round synthetic activation corpus and first
  prompt. Publication requires the clean Desktop acceptance gate.
- A closed MCPB artifact verifier, independent byte-for-byte rebuild gate and
  four-file GitHub Release contract. PyPI remains strictly wheel and sdist;
  `SHA256SUMS.txt` binds those two payloads plus the macOS MCPB.
- Acceptance packet v4 binds clean Desktop activation, exact MCPB SHA-256,
  runtime identity, exact visible and called tool inventories, private session
  and demo-journal digests, demo result, uninstall and same-artifact reinstall
  to the candidate later rebuilt by CI. Upgrade and rollback are explicitly
  marked not applicable for the first public MCPB instead of inventing an
  older extension release.
- MCP contract `veqtor.mcp.v0.2`, advertised in every tool's metadata and
  output schema, with closed nested edit, anchor and preflight-proof inputs and
  explicit structured output schemas for all six tools.
- `preflight_proof.v1` binds the exact source bytes, canonical edits, configured
  tracked-change author, producer build and predicted candidate. The v0.2 MCP
  `apply_edits` input requires the complete proof and verifies it before output
  publication. This is deterministic drift detection, not authentication or a
  digital signature.
- `revision_inventory.v1` exposes total, decoded and unsupported revision
  occurrence counts, unsupported-kind count, emitted change-unit count and the
  checked decoded-plus-unsupported partition.
- `list_rounds` accepts a complete `ordered_filenames` positional manifest and
  reports whether it used that sequence or the disclosed
  `filename_lexicographic_v1` default. Neither mode claims chronology or lineage.
- Decision-record export now requires an already initialized exact workspace.
  Wrong-parent, ambiguous-child, uninitialized and bounded-incomplete discovery
  states fail closed without creating a second sidecar. Direct-child discovery
  excludes the service `.veqtor` directory and uses a cooperative elapsed-time
  budget between filesystem operations, not a hard syscall timeout.
- A regression for the previously reported, but not reproduced, 14-operation
  counter/reinstate hang shape verifies a terminal structured refusal. No
  timeout or cancellation contract is added.

### Changed

- Preflight diagnostics replace nullable `position_supported` with the closed
  `position_status` enum and preserve an explicit `failure_phase` through live
  results and compact decision-record projections.
- Successful MCP apply results state that the preflight binding was verified and
  make the predicted-candidate/output SHA-256 equality explicit.

### Compatibility

- The six MCP tool names and `decision_record.v1` historical pairs remain
  unchanged and readable. The v0.2 MCP input contract intentionally makes
  `preflight_proof` mandatory for `apply_edits`; the lower-level Python function
  retains its optional-proof v0.1 behavior.

## 0.1.2

Veqtor v0.1.2 Alpha release contents.

### Added

- Fail-closed DOCX/ZIP resource limits covering container size, expanded size,
  member count, individual parts, parsed XML node count, extracted change
  units, suspicious compression ratios and round-folder scans bounded by both
  aggregate input and aggregate actual expanded member output. Folder output
  remains charged for packages rejected after member-output processing, and
  exceeding the shared budget returns no partial round list.
- A single payload-aware DOCX archive boundary reconciles security-relevant
  local, central and 32-bit data-descriptor fields, accepts only unencrypted
  `STORED`/`DEFLATED` members, and verifies actual bounded output, CRC and the
  true DEFLATE end-of-stream before any document fact or edit is accepted.
- Safe XML parsing that refuses `DOCTYPE` declarations instead of loading DTDs
  or expanding custom entities.
- Bounded computed and manual numbering labels that omit oversized templates,
  manual labels, levels outside 0-8 and Roman counters outside 1-3999 instead
  of amplifying work.
- Linear-time paragraph-style inheritance resolution with fail-closed cycle
  detection.
- A two-level tracked text-revision nesting boundary that preserves ordinary
  counter markup while refusing recursive text amplification.
- Consistent pre-decoder refusal of duplicate names, encryption, ZIP64 and
  unsupported ZIP methods across list, extract, verify, preflight and apply.
- Atomic edit-batch limits for edit count, per-edit text and total inserted
  text, with stable structured refusals.
- Ordered public promotion that durably reserves the protected exact tag,
  publishes the approved wheel and sdist through PyPI OIDC Trusted Publishing,
  verifies their public bytes, provenance and `uvx` onboarding, and only then
  publishes the matching immutable GitHub Release.
- Clean-room installation and Claude Code registration through `uvx` with the
  Veqtor version pinned, plus explicit user, local and project scope guidance.

### Changed

- MCP initialization now reports the Veqtor package version instead of the
  installed MCP SDK version.
- Minimum supported dependencies are raised to `lxml>=6.1` and `mcp>=1.23.0`.
- Public documentation now separates community-supported Alpha expectations,
  private security reporting and the concise user-facing roadmap from internal
  acceptance details.
- Apply validates and expands the immutable source package once, then reuses
  that exact package snapshot for baseline extraction and mutation planning;
  the serialized candidate remains a separate round-trip validation pass.
- The public project identity names Ilya Shilov as creator and maintainer while
  retaining `JohnDeer-ai` as the GitHub handle.

### Compatibility

- The six MCP tool names and existing `decision_record.v1` historical pairs
  remain unchanged.
- Documents and edit batches inside the new resource and XML-safety boundaries
  retain the v0.1.1 extraction, verification, preflight, apply and
  compact-export behavior.

## 0.1.1

Veqtor v0.1.1 Alpha release contents.

### Added

- Self-describing decision-record export fields for substantive-record scope,
  access-event persistence and current-snapshot count semantics.
- A two-export installed-wheel smoke and release-blocking Claude Desktop
  rehearsal gate for access-event and raw-versus-compact explanations.
- A closed `veqtor_release_acceptance.v2` packet binds both live gates and
  their runtime identities to the exact release candidate; promotion seals its
  canonical bytes with an explicitly approved SHA-256 and validates the packet
  in the read-only guard before reusable CI and publication.

### Changed

- Interrupted GitHub draft promotion now enumerates all release pages, requires
  one exact-tag match and uploads, verifies and publishes by release id.
- Model-facing assurance now distinguishes the live response, private raw
  journal result and privacy-minimized compact projection.
- Decision-record export now takes its response snapshot and appends the
  matching access event under one journal lock, so concurrent exports report
  every access event ordered before their own event.
- Documentation clarifies reinstate behavior, compact `clause_sha256` meaning
  and provenance writes caused by read-only operations.

### Compatibility

- The `decision_record.v1` historical tool pairs and compact projections remain
  backward-readable; new export-result fields are additive.
- The published immutable `v0.1.0` release remains unchanged.

## 0.1.0

Veqtor v0.1.0 Alpha release contents.

### Added

- Full-pipeline `preflight_edits` dry-run using the same validated candidate
  bytes as `apply_edits`.
- Bounded current-paragraph context and conservative manual paragraph labels.
- Server-level `VEQTOR_TRACKED_CHANGE_AUTHOR` configuration.
- Installed-runtime `--version` and `doctor` diagnostics.
- Versioned wheel/sdist release checks and installed-wheel MCP smoke.

### Changed

- `apply_edits` returns observed source SHA, producer identity and resulting
  tracked-change author.
- `export_decision_record` returns an explicit `returned_count`.
- Missing clauses now retain `clause_sha256: null` instead of hashing JSON
  `null` as if it were a clause identity.
- Public metadata now identifies the project as Alpha and states the supported
  Python and operating-system range.
- Preflight diagnostics use one complete field shape and distinguish facts
  not evaluated (`null`) from a completed zero-match result (`0`).
- Paragraph readings and tracked revision text preserve Word
  `w:noBreakHyphen` atoms as `-`.
- Edit matching now shares extraction's text-atom model for tabs, breaks and
  non-breaking hyphens, with fail-closed surgery for unsupported run shapes.
- XML-incompatible edit text is rejected as a structured `invalid_edit`
  validation refusal before OOXML serialization.
- Release promotion validates the trusted `main` commit before execution,
  atomically binds the release tag and scans normalized archive metadata.
- Release privacy checks cover tar ownership fields plus bounded raw archive
  surfaces, while rejecting oversized compressed or expanded artifacts.

### Compatibility

- `0.1.0` reads existing valid `decision_record.v1` journals created by
  `0.0.0`.
- After a journal contains the new `preflight_edits` tool pair, downgrading the
  matter to `0.0.0` is unsupported; the older reader does not know that tool.
