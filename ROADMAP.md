<!-- SPDX-License-Identifier: Apache-2.0 -->

# Roadmap

## Product direction

Veqtor is a local open-source trust layer for contract negotiation workflows:
read document history, verify quoted wording, preflight a complete proposal,
apply tracked changes fail-closed, and retain re-checkable provenance.

The calling model decides what to analyze or propose. Veqtor supplies bounded
document facts and deterministic writes; it does not claim legal correctness.

## Alpha scope

- Local stdio MCP for macOS and Linux with Python 3.12-3.14.
- Deterministic discovery of filename-ordered DOCX rounds.
- Tracked-change extraction from `word/document.xml` with source hashes and
  structural anchors.
- Exact and normalized anchored quote verification, including a bounded
  current/rejected-pending paragraph selector in development v0.4.
- Atomic full-pipeline preflight and apply for tracked replace, delete, counter
  and reinstate operations.
- Private local decision records with compact export and explicit assurance
  boundaries.
- Bounded DOCX/ZIP processing and versioned installation from PyPI, with the
  same verified wheel, sdist and checksum manifest on GitHub Releases.

## Implemented in development source 0.4.0.dev0

Development package `0.4.0.dev0` advertises the nine-tool MCP contract
`veqtor.mcp.v0.4`. The contract version is a surface-wide API-schema identifier:
all nine tools, including the eight names carried forward from v0.3, report
v0.4 even where an individual schema and behavior are otherwise unchanged.
Source version alone is not proof of a published package or release.

Reliable-workflow stages 0 through 3C now provide:

- versioned, closed nested MCP input schemas and explicit output schemas for all
  nine tools;
- bounded accepted/current document inspection with hash-bound paragraph and
  section references;
- a seed-centred Round Map of exact paragraph equality and recorded document-byte
  derivations with explicit unresolved/ambiguous states and no inferred
  chronology or lineage;
- immutable capture of a complete direct-DOCX candidate set and a bounded,
  seed-first paragraph-history trace through exact current or rejected-pending
  projection equality, with paragraph-scoped change units, explicit uncertainty
  and snapshot-bound pagination;
- paragraph-side `verify_quote` v2 selection for accepted/current or
  rejected-pending text, while retaining historical `verification.v1` journal
  compatibility;
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

- Extend supported OOXML layouts based on reproducible public issues.
- Validate the development v0.4 artifact independently before any separate
  release/MCPB/Desktop/publication step; the frozen public v0.3 release remains
  eight-tool `veqtor.mcp.v0.3` until that process is completed.
- Evaluate a separately specified Stage 3D clean-sendable-redline workflow only
  after external-user validation of the read-only Stage 3C evidence surface.
- Refine installation, diagnostics and examples from external-user feedback.

### Frozen Claude Desktop Extension boundary for version 0.3.0

Veqtor can be packaged as a versioned Claude Desktop Extension (`.mcpb`). It is
a public installation path only when the exact artifact passes the documented
clean-Mac gate and appears in the matching immutable release. When published, a
non-technical macOS user can install the same local MCP server without editing
JSON or running `uvx` manually: download the release artifact, open it, review
the requested configuration, enter the tracked-change author name, confirm
installation in Claude Desktop, and try Veqtor on the synthetic demo documents.

Version 0.3.0 scope:

- macOS-only v1; Linux keeps the existing CLI setup until its Desktop path is
  separately supported and tested.
- An MCPB manifest declares the local `uv` server runtime and exposes
  `VEQTOR_TRACKED_CHANGE_AUTHOR` as required user configuration.
- The build is validated and byte-reproducible. Only promotion can bind a
  public artifact to its published checksum; it is not digitally signed.
- The release gate requires a clean Claude Desktop install with all eight public
  tools: `list_rounds`, `extract_redlines`, `inspect_document`, `map_rounds`,
  `verify_quote`, `preflight_edits`, `apply_edits`, and
  `export_decision_record`.
- The package ships the same disposable four-round demo documents used by the website and a
  copyable first prompt, so activation does not require generating a new corpus
  in Terminal after the extension is installed.
- Documentation covers version reporting, upgrades, rollback where available, and complete
  uninstall/cleanup before presenting the extension as the recommended path.

Development `0.4.0.dev0` does not widen this MCPB inventory: its ninth tool and
v0.4 schemas require a separate exact-artifact Desktop/MCPB acceptance before a
future release can claim them. In particular, the frozen
`MCPB_REQUIRED_TOOLS` list remains the eight v0.3 names.

Non-goals and release boundaries:

- Do not describe this as silent or truly one-click installation: the user must
  open the download, review configuration, and approve installation in Claude.
- Do not introduce a hosted MCP service; Word-file processing remains local.
- Do not modify or replace earlier release artifacts. Ship each extension only
  in a new, independently verified release.

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
