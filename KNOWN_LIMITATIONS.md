<!-- SPDX-License-Identifier: Apache-2.0 -->

# Known limitations — v0.1 Alpha

Veqtor v0.1 is a local technical Alpha for early adopters. Its supported
contract is intentionally narrow.

## Documents and revisions

- Extraction and writing cover `word/document.xml`. Comments, headers,
  footers, footnotes and endnotes are not analyzed or edited.
- Formatting, move, paragraph-mark and structural revision categories are
  counted but not all are converted into editable change units.
- Complex adjacent or nested OOXML layouts may be refused with a stable error
  rather than rewritten approximately.
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

- Round order is deterministic filename order, not lineage proof.
- There is no semantic cross-round clause matcher or authorship forensics.
- `clause_anchor` and `manual_label` are best-effort navigation aids. Durable
  evidence remains file SHA plus change-unit id, structural paragraph/group
  locator and verified old/new wording.
- The calling model supplies legal analysis and drafting. Veqtor does not
  establish legal correctness.

## Preflight and apply

- A successful preflight proves the shared document-processing pipeline for the
  same source bytes, build, configured author and edits. Apply can still fail if
  the source changes, the output exists, or publication encounters permissions,
  storage or filesystem races.
- Preflight does not create an output DOCX, but normally writes a local
  provenance record under `.veqtor`.
- Author identity is fixed at server start. If it matches a counterparty's
  revision author, some counter/reinstate requests are treated as edits to the
  configured party's own revision and refused.
- Matching uses the same current-reading atoms as extraction, including tabs,
  breaks and non-breaking hyphens. A span that touches one of those element
  atoms is identified truthfully but refused as `unsupported_run_shape` when
  the v0.1 writer cannot preserve its OOXML semantics.

## Provenance and security

- Decision records are best-effort local provenance, not tamper-evident,
  authenticated, signed or hash-chained audit records.
- The threat model is a non-hostile single-user macOS/Linux workspace. A
  malicious process running as the same user is outside scope.
- The raw journal may contain matter text and has no rotation or aggregate size
  cap in v0.1.
- Journals containing `preflight_edits` require Veqtor 0.1.0 or newer. Downgrade
  to 0.0.0 is unsupported.

## Platforms and delivery

- Supported: macOS/Linux, Python 3.12-3.14, local stdio MCP.
- Windows, hosted MCP, OAuth, a custom UI and SLA-backed support are not part of
  v0.1.
