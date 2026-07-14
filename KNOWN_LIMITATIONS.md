<!-- SPDX-License-Identifier: Apache-2.0 -->

# Known limitations — v0.1 Alpha

Veqtor v0.1 is a local technical Alpha for early adopters. Its supported
contract is intentionally narrow.

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
- Extraction and writing cover `word/document.xml`. Comments, headers,
  footers, footnotes and endnotes are not analyzed or edited.
- Formatting, move, paragraph-mark and structural revision categories are
  counted but not all are converted into editable change units.
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
  supported; labels outside those bounds are omitted.
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

- One atomic batch accepts at most 100 edits, at most 20,000 characters of new
  text per edit, and at most 200,000 new characters across the batch. A limit
  refusal writes no output and applies no partial edit.
- A successful preflight proves the shared document-processing pipeline for the
  same source bytes, build, configured author and edits. Apply can still fail if
  the source changes, the output exists, or publication encounters permissions,
  storage or filesystem races.
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
- The raw journal may contain matter text and has no rotation or aggregate size
  cap in v0.1.
- Read-only list, extract, verify and preflight calls normally append local
  provenance too. Decision-record export normally appends an access event after
  taking its response snapshot, so observation is not side-effect free unless
  decision records are disabled.
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
