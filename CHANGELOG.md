<!-- SPDX-License-Identifier: Apache-2.0 -->

# Changelog

All notable changes to Veqtor MCP are documented here.
Publication dates are authoritative in each version's immutable GitHub Release
through its `published_at` timestamp.

## 0.1.2

Veqtor v0.1.2 Alpha release contents.

### Added

- Fail-closed DOCX/ZIP resource limits covering container size, expanded size,
  member count, individual parts, parsed XML node count, extracted change
  units, bounded round-folder scans and suspicious compression ratios.
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
