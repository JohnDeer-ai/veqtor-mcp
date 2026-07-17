<!-- SPDX-License-Identifier: Apache-2.0 -->

# Roadmap

## Product direction

Veqtor is a local open-source trust layer for contract negotiation workflows:
read document history, verify quoted wording, preflight a complete proposal,
apply tracked changes fail-closed, and retain re-checkable provenance.

The calling model decides what to analyze or propose. Veqtor supplies bounded
document facts and deterministic writes; it does not claim legal correctness.

## Current public Alpha

- Local stdio MCP for macOS and Linux with Python 3.12-3.14.
- Deterministic discovery of filename-ordered DOCX rounds.
- Tracked-change extraction from `word/document.xml` with source hashes and
  structural anchors.
- Exact and normalized quote verification.
- Atomic full-pipeline preflight and apply for tracked replace, delete, counter
  and reinstate operations.
- Private local decision records with compact export and explicit assurance
  boundaries.
- Bounded DOCX/ZIP processing and versioned installation from PyPI, with the
  same verified wheel and sdist attached to GitHub Releases.

## Implemented in development (unreleased)

The current source tree keeps package version and public-release state at
`0.1.2`, while advertising the next MCP contract as `veqtor.mcp.v0.2`. The
contract version is an API-schema identifier, not a claim that a new package
version has been published.

Reliable-workflow stages 0 and 1 now provide:

- versioned, closed nested MCP input schemas and explicit output schemas for all
  six tools;
- a successful `preflight_edits` proof that binds the exact source bytes,
  canonical edit payload, configured author, producer build and predicted
  candidate SHA-256; the MCP `apply_edits` call requires that complete proof and
  verifies it before publication;
- explicit per-edit `position_status` and operation-level `failure_phase`
  diagnostics, without using `null` to mean that position was not evaluated;
- `revision_inventory.v1`, with a checked partition between decoded revision
  elements and unsupported occurrences and separate change-unit accounting;
- a complete optional `ordered_filenames` positional manifest for
  `list_rounds`, alongside the disclosed `filename_lexicographic_v1` default;
- fail-closed decision-record export when the supplied folder is uninitialized,
  is a wrong parent of one journal workspace, or has multiple candidate child
  workspaces; and
- a 14-operation counter/reinstate regression that reaches a terminal,
  structured `edits_overlap` result. This does not reproduce the historical
  hang report and does not add timeout or cancellation semantics.

The preflight proof is a deterministic drift binding, not authentication, a
digital signature, a trusted timestamp or tamper evidence.

## Next

- Independently review and exercise the development contract through the real
  MCP boundary before selecting a new release candidate.
- Extend supported OOXML layouts based on reproducible public issues.
- Improve round-to-round navigation without turning probabilistic matching
  into evidence.
- Refine installation, diagnostics and examples from external-user feedback.

### Backlog: Claude Desktop Extension

Package a future Veqtor release as a versioned Claude Desktop Extension
(`.mcpb`) so a non-technical macOS user can install the same local MCP server
without editing JSON or running `uvx` manually. The honest activation flow is:
download the release artifact, open it, review the requested configuration,
enter the tracked-change author name, confirm installation in Claude Desktop,
and try Veqtor on the synthetic demo documents.

Initial scope:

- macOS-only v1; Linux keeps the existing CLI setup until its Desktop path is
  separately supported and tested.
- An MCPB manifest that declares the local `uv` server runtime and exposes
  `VEQTOR_TRACKED_CHANGE_AUTHOR` as required user configuration.
- Validate the package, sign it where the distribution path supports signing,
  publish a checksum, and attach the immutable `.mcpb` to a new GitHub Release.
- Prove a clean Claude Desktop install with all six public tools:
  `list_rounds`, `extract_redlines`, `verify_quote`, `preflight_edits`,
  `apply_edits`, and `export_decision_record`.
- Ship the same disposable four-round demo documents used by the website and a
  copyable first prompt, so activation does not require generating a new corpus
  in Terminal after the extension is installed.
- Document version reporting, upgrades, rollback where available, and complete
  uninstall/cleanup before presenting the extension as the recommended path.
- Consider submission to the Claude Desktop Extensions catalog only after the
  direct-download package and update path have been exercised publicly.

Non-goals and release boundaries:

- Do not describe this as silent or truly one-click installation: the user must
  open the download, review configuration, and approve installation in Claude.
- Do not introduce a hosted MCP service; Word-file processing remains local.
- Do not modify or replace the already published `v0.1.2` artifacts. Ship the
  extension only in a new, independently verified release.

## Outside the Alpha

- Legal advice or autonomous legal judgment.
- A hosted service, account system, OAuth layer or custom chat UI.
- A complete Word editor or silent text rewriting.
- Cryptographic authorship, trusted timestamps or a tamper-proof audit trail.
- Guaranteed semantic clause lineage across negotiation rounds.
- SLA-backed commercial support.

Public priorities are driven by reproducible
[GitHub Issues](https://github.com/JohnDeer-ai/veqtor-mcp/issues). Never attach
real client documents or confidential matter text to an issue.
