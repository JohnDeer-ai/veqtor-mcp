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

## Next

- Improve nested MCP schemas and output schemas for more generic clients.
- Extend supported OOXML layouts based on reproducible public issues.
- Improve round-to-round navigation without turning probabilistic matching
  into evidence.
- Refine installation, diagnostics and examples from external-user feedback.

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
