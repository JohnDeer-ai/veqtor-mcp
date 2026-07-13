<!-- SPDX-License-Identifier: Apache-2.0 -->

# Changelog

All notable changes to Veqtor MCP are documented here.
Publication dates are authoritative in each version's immutable GitHub Release
through its `published_at` timestamp.

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
