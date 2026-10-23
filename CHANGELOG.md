<!-- SPDX-License-Identifier: Apache-2.0 -->

# Changelog

All notable changes to Veqtor MCP are documented here.
Publication dates are authoritative in each version's immutable GitHub Release
through its `published_at` timestamp.

## 0.6.0.dev1

Unreleased paragraph and comment-thread drafting candidate. This entry describes
source behavior; it does not establish installation, native Word acceptance or
public distribution.

- Add explicit tracked internal paragraph deletion and complete paragraph
  structural decision targets, with resolved numbering effects and exact
  neighbor-property preservation.
  Check the whole used abstract-numbering family rather than assuming different
  numbering-instance IDs have independent counters; refuse unproved shared
  continuation or reuse before saving.
  Recognize narrowly validated Word-resave numbering metadata while refusing
  enabled restart-after-break behavior and unknown attributes.
- Validate explicitly selected same-table property decisions as a group before
  applying their exact local patches. Support compatible grid/span and
  grid/omitted-column outcomes without choosing companions automatically.
  Keep standalone inventory feasibility separate from batch feasibility, and
  refuse cell-span rejection with unowned row properties or history rather than
  silently deleting those independent properties to imitate Word.
  Check alignment conflicts against the complete selected row outcome so an
  explicitly removed row alignment permits the matching table rejection in
  either order, while retained or restored conflicts still refuse.
- Require `opentelemetry-api>=1.45.1,<2` for the MCP runtime to exclude the
  vulnerable trace-state parser covered by CVE-2026-114603. Apply the same
  security floor to the locked environment and exact-minimum artifact smoke.
- Add `read_document_revisions` and explicit `decide_revision` accept/reject
  operations, with exact file/part/location/fingerprint references. Support
  isolated insertions, deletions, explicit adjacent replacements and all six
  property domains (`rPr`, `pPr`, `tcPr`, `trPr`, `tblPr`, `tblGrid`) and explicit
  row-level `tblPrEx` companions emitted by Word table formatting. Preserve
  unrelated history and bind the acting reviewer separately from original authors.
  Refuse dependent structural changes, ambiguous chains and incompatible restored
  grids. Check fixed-layout grid/cell widths after the full explicit batch and
  reject ambiguous untracked row-alignment dependencies. Main-story field and
  permission/revision range guards cover block and cross-paragraph boundaries.
  The action journal remains explicitly disabled in this private candidate.
- Insert tracked paragraphs before/after a source paragraph and replace a whole
  supported paragraph using its complete current text as an exact guard.
  Default to minimal word/token changes while retaining unchanged run formatting;
  provide explicit whole mode, whose new text inherits the first edited fragment.
  Preserve paragraph identity/properties when removing all of its text.
- Reply to existing comments under the selected author, read thread membership
  and resolution state, and explicitly resolve/reopen comment threads.
  Preserve original comment bodies and authors; retain refusal for unsupported
  metadata and threaded deletion.
- Select an optional author/initials profile per preflight/apply request without
  changing the server default, restricted to explicitly configured own profiles;
  bind the resolved identity into the save proof.
- Refuse edits crossing range boundaries nested in existing deleted content.
- Deliver an agent protocol through MCP initialization instructions: establish
  authorization for the concrete plan, use minimal edits by default, require
  explicit whole-mode and thread-status instructions, then reread and report.
- Extend the same-file workflow with source-stable references across sequential
  insertions, revision/collateral checks, and the existing exact-backup save path.

## 0.5.0

- Add the fourteen-tool `veqtor.mcp.v0.5.0` surface with configured-author
  drafting, own pending-insertion re-editing, and anchored comment operations.
- Save the working DOCX in place with an exact backup, source-byte checks,
  supported local-filesystem validation and recoverable writer locks.
- Preserve unrelated revisions, comments, formatting history and portable file
  metadata; return schema-valid refusal and recovery details to MCP clients.
- Recover a confirmed exited writer before reaping and distinguish short Word
  owner filenames from those belonging to other documents.
- Package author and comment-initial settings in the macOS extension and bind
  release acceptance to the exact installed artifacts and native Word workflow.

## 0.5.0.dev3

Unreleased author-drafting save stabilization; public `0.4.2` artifacts and
its frozen release/acceptance contract are unchanged.

- Allow standard macOS parent-folder ACLs that do not inherit onto children;
  retain strict file/private-directory ACL checks and kernel-enforced permissions.
- Identify reused process IDs across macOS user boundaries without treating
  permission failures as evidence that a writer has ended.
- Recover complete abandoned lock temporaries and retain stage ownership through
  interrupted cleanup. Preserve unregistered bootstrap files rather than guess
  ownership from a filename.
- Avoid truncated Word owner-file probes for one- and two-character names.

## 0.5.0.dev2

Unreleased author-drafting review fixes; public `0.4.2` artifacts are unchanged.

- Declare structured save errors in the tool output schema and independently
  validate success/refusal/late-publication responses through the TypeScript SDK.
- Preserve the required settings order when enabling Word Track Changes.
- Refuse edits that would extend another author's tracked formatting onto new
  text inside an own insertion; keep same-author formatting history editable.
- Recover recognized process-instance locks and owned temporary saves after
  crashes; recognize same-file case aliases without weakening path checks.
- Give explicit filesystem and lock recovery diagnostics while retaining exact
  backups and refusing unsupported storage rather than weakening publication.

## 0.5.0.dev1

Unreleased author-drafting development build; public `0.4.2` artifacts are unchanged.

- Add `read_document_comments`, `preflight_document_edits`, and `apply_document_edits`.
- Re-edit simple own pending insertions while retaining their author, ID and date.
- Read comments, add anchored comments, and update/delete supported own comments.
- Save to the same working file with mandatory exact-byte backups, stale-source
  checks, cooperative locking and Word owner-file detection; keep create-only copy mode.
- Bind drafting preflight proofs to source, destination, author/initials, operations
  timestamp and candidate bytes. Preserve the legacy counterproposal API and journal.
- Preserve portable file metadata without demanding a copy of macOS provenance;
  recover abandoned save locks and return verified backup recovery details over MCP.
- Resolve modern comment metadata through relationships so renamed parts cannot
  bypass thread-preservation checks.
- Preserve Word page-cache markers, paragraph formatting history and neighboring
  paragraph-mark revisions; support bounded whole-run formatting revisions.
- Require MCP SDK 2.2.0 or later and update the locked SDK pair to 2.2.0.

## 0.4.2

Veqtor v0.4.2 Alpha release contents. Publication is established only by the
matching immutable GitHub Release and verified public PyPI files.

- Ship the eleven-tool `veqtor.mcp.v0.4.2` surface, including saved positions,
  expected-revision mutations and explicit exact-version confirmation.
- Support hash-bound edits in clean body/table paragraphs, preserving existing
  revisions and creating separate tracked Word outputs.
- Deliver the selected-issue next-round prompt and thin Codex skill, with saved
  position recovery, concrete decisions, conditional concessions and fresh
  follow-up sessions.
- Preserve controlled refusal codes in MCP 2.0 and 2.2 transports.
- Freeze complete Python and macOS MCPB inventories, include the workflow and
  document/position contracts in the extension, and verify v0.4.0 upgrade/rollback.

## 0.4.2.dev0

Unreleased development build. Package/producer identity is `0.4.2.dev0`;
the eleven-tool public schema is `veqtor.mcp.v0.4.2`. Frozen v0.4 artifacts
and historical provenance goldens remain unchanged.

- Add independent portable local deal positions with explicit exact-version
  confirmation, confirmation reset on update, withdrawal and complete history.
- Add full bounded reads and atomic expected-revision batches under a separate
  cross-process lock, including first creation and uncertain-commit recovery.
- Verify new documentary bindings against exact bytes/references; disclose
  source freshness independently from saved intentions and business decisions.
- Keep position storage independent of optional provenance and DOCX writes.
- Add a separate native Codex acceptance profile with predeclared complete
  values, fresh client/server sessions, conflicts and relocation checks.

## 0.4.1.dev1

Unreleased development build. Package/producer identity is `0.4.1.dev1`;
additive public schema is `veqtor.mcp.v0.4.1`. Frozen v0.4 release identity,
legacy tool behavior and historical decision-record goldens remain unchanged.

- Add closed full-reference paragraph targets for exact replace and delete-only
  in supported clean body/table paragraphs, including documents with no revisions.
- Refuse pending text, property, structural and paragraph-mark revisions or
  unsupported structure. Share source snapshot, batch planning, proof and atomic
  publication with legacy counter/reinstate operations.
- Prove complete expected paragraph text, original text/run properties, new
  replacement formatting, exact revisions and untouched package/paragraph content.
- Preserve full paragraph target identity in preflight, applied results, private
  journal and compact export, without inventing a source change unit.
- Add a separate NR-01 native acceptance checker, synthetic corpus and next-round
  scenario. Full native/render/package gates are separate from Python tests.

## 0.4.1.dev0

Unreleased development build. Package and producer versions are `0.4.1.dev0`;
the compatible nine-tool contract remains `veqtor.mcp.v0.4`. The frozen v0.4.0
release contract, historical records and published artifacts are unchanged.

- Add local Codex setup, a next-round workflow prompt, and a native-client
  evidence checker with a bounded v0.4.0 CLI compatibility observation.
- Preserve controlled refusal codes through the MCP SDK 2.2 transport using
  its anticipated-error interface. Keep core exception details private,
  preserve existing safe recovery hints, and retain direct Python behavior
  and the nine advertised tool schemas. This source change is not part of the
  immutable published v0.4.0 artifacts.
- Sanitize SDK validation failures before tool entry, including rejected
  values and nested field names; report `invalid_arguments` without private
  diagnostics. Preserve safe Veqtor refusal codes across SDK 2.0 and 2.2.
- Clarify the paragraph/section selection wrapper for `inspect_document` reads
  in its tool description and the next-round prompt.

## 0.4.0

Veqtor v0.4.0 Alpha release contents. The immutable GitHub Release
`published_at` timestamp, not this file, is authoritative for whether and when
publication occurred.

### Added

- The bounded `pending_text_revisions_rejected_v1` literal projection
  primitive and deterministic adjacent-round classifier acceptance coverage
  for the synthetic Stage 3C Fixture 2 corpus.
- The bounded `trace_paragraph_history` tool with immutable direct-DOCX
  capture, exact current/rejected-pending paragraph relationships,
  paragraph-scoped change units, snapshot-bound `ph1` pagination, complete
  Stage 3C limits, and success-only privacy-minimized
  `paragraph_history.v1` provenance.
- Paragraph-side `verify_quote` projection selection and the closed
  `verification_result.v2` result/`verification.v2` journal pair, while keeping
  historical `verification.v1` records readable.
- Release sdist packaging for the implemented
  `CLAUSE_HISTORY_V0.4.md` contract.

### Changed

- The v0.4 server uses MCP Python SDK v2 and negotiates both the
  modern `2026-07-28` and legacy `2025-11-25` wire revisions over local stdio.
- The contract-wide schema marker is `veqtor.mcp.v0.4` on all nine tools,
  including all eight names carried forward from v0.3. Existing tools whose
  own schemas and behavior did not change still report the new surface-wide
  contract version.

- Package and release-candidate identity is `0.4.0`; publication still requires
  the exact release contract, existing-user MCPB acceptance and immutable
  promotion workflow.
- Acceptance evidence v7 explicitly records an existing maintainer user,
  installed-source verification and UV/Python runtime origin. It replaces the
  fresh-user requirement without claiming clean-install or developer-toolchain
  independence; the functional, privacy and extension lifecycle gates remain.
- Release CI builds and smokes wheel, sdist and the nine-tool MCPB surface,
  then independently reproduces the sealed release set.

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
