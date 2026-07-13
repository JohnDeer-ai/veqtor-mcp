<!-- SPDX-License-Identifier: Apache-2.0 -->

# Changelog

All notable changes to Veqtor MCP are documented here.
Publication dates are authoritative in each version's immutable GitHub Release
through its `published_at` timestamp.

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
