<!-- SPDX-License-Identifier: Apache-2.0 -->

# `inspect_document` v0.3 development specification

## Status and user job

This is the implemented development contract for the first Stage 3 product
slice. The development source is `0.3.0.dev0` and advertises draft MCP contract
`veqtor.mcp.v0.3`. Automated acceptance belongs to the exact development
snapshot under test; this document does not claim that `0.3.0`, the development
snapshot, or any corresponding artifact has been published or has passed a
Claude Desktop product gate.

The user job is narrow:

> Find important wording in the main body of one DOCX, inspect it without a
> whole-document dump, and bind every paragraph returned for citation to the
> exact source bytes.

Version 0.2 can verify only `old_text` or `new_text` after a tracked-change unit
has supplied an anchor. It cannot anchor unchanged text. Version 0.3 adds one
read-only `inspect_document` verb with four bounded modes and hash-bound
paragraph and section references.

The calling model still decides what wording matters and what it means. The
tool does not decide legal effect, infer semantic clause identity, prove round
chronology or lineage, accept/reject tracked changes, or write a DOCX.

This specification and its product acceptance are English-language only. Exact
Unicode text remains mechanically preserved and searchable, but Russian
heading grammar, Russian manual-label recognition, morphology, abbreviations
and localized clause semantics are outside v0.3.

## Contract identity and closed input

The MCP contract identifier is `veqtor.mcp.v0.3`. The tool name is
`inspect_document`; its append-only decision-record pair is
`(inspect_document, inspection.v1)`.

Every nested reference/selection object is closed, and mode-inapplicable
values in the named arguments are refused rather than ignored. As documented
in `API.md`, the current FastMCP transport may ignore unrecognized top-level
arguments before the tool runs; callers must use the advertised named
arguments. The public shape is:

```json
{
  "path": "/Users/example/Deals/Acme/current.docx",
  "mode": "literal_search",
  "phrases": ["aggregate liability"],
  "match_basis": "exact_literal",
  "selection": null,
  "cursor": null,
  "max_items": 50
}
```

Only mode-applicable optional keys are supplied in an actual call:

| Mode | Required | Permitted optional keys | Purpose |
| --- | --- | --- | --- |
| `outline` | `path`, `mode` | `cursor`, `max_items` | Page structural sections without paragraph body text. |
| `literal_search` | `path`, `mode`, `phrases`, `match_basis` | `cursor`, `max_items` | Find explicit phrases and return bounded snippets plus paragraph references. |
| `browse` | `path`, `mode` | `cursor`, `max_items` | Page non-empty supported body paragraphs in canonical order. |
| `read` | `path`, `mode`, `selection` | `cursor` for a section read, `max_items` | Return complete text for one selected paragraph or structural section. |

A paragraph read rejects a cursor because it has exactly one item. A section
read may paginate its ordered paragraphs. `max_items` defaults to 50 and must
be an integer from 1 through 100; booleans are not integers for this contract.

`literal_search.phrases` contains 1-20 non-blank strings. One phrase is limited
to 2,000 Unicode scalar values and the aggregate is limited to 10,000. The
input must not contain isolated UTF-16 surrogate code points; those refuse with
`invalid_phrase` before cursor hashing or document inspection. The closed
`match_basis` vocabulary is:

- `exact_literal`: literal, case-sensitive comparison without normalization;
- `normalized_literal`: bounded whitespace and typographic quote/dash
  normalization, still case-sensitive; and
- `normalized_casefold_literal`: the same normalization followed by Unicode
  case folding, without locale-specific stemming or morphology.

There is no fuzzy, semantic, translated, regular-expression or locale-aware
search mode. A phrase never crosses a paragraph boundary.

`selection` is exactly one of these closed objects:

```json
{"paragraph_ref": {"...": "paragraph_ref.v1"}}
```

```json
{"section_ref": {"...": "section_ref.v1"}}
```

One invalid or stale selection refuses the read. It is not converted to an
empty success.

## Search scope and container policy

### `canonical_body_flow_v1`

Every mode declares:

```text
search_scope: word_document_xml_body_v1
reading_mode: accepted_current_v1
container_policy: canonical_body_flow_v1
```

`canonical_body_flow_v1` starts at `w:document/w:body` in
`word/document.xml` and indexes each supported `w:p` once in XML order. It does
not use unrestricted descendant traversal as paragraph identity. Inspection
and extraction require a `w:document` root with exactly one direct `w:body`;
anything else is refused before paragraph or revision facts are emitted.

The allowlisted pass-through block containers are:

- `w:sdt` through `w:sdtContent`; and
- `w:tbl` through `w:tr` and `w:tc` in XML order.

Supported combinations may nest: an SDT may wrap a paragraph or table, an SDT
may occur inside a table cell, and a table may nest inside a cell. Tables and
SDTs do not become synthetic text. No tab, newline, row delimiter or cell
delimiter is inserted between paragraphs. Paragraphs in cells retain
`container_kind: table_cell`; other supported body paragraphs use `body`.

The following subtrees are pruned from paragraph membership and reading:

- drawings, objects, pict content, VML text boxes and `w:txbxContent`;
- relationship-backed `w:altChunk` imported content;
- `mc:AlternateContent`, choices and fallbacks;
- a paragraph nested inside another paragraph; and
- an unknown container carrying paragraphs, text atoms or revision markup.

Inline `w:sdt`, hyperlink, smart-tag, field, direction, custom-XML and tracked-
revision wrappers are pass-through only under the same allowlist. Paragraph and
run properties are ignored only while they contain legitimate non-text
property or structural-revision markup. A property subtree containing a
paragraph, rendered text atom, move revision or differently nested text
insertion/deletion becomes a fail-visible `unknown_container` exclusion. An
excluded subtree never leaks text into its host paragraph.

`w:altChunk` is fail-visible even though its imported content is not a child of
the XML element. Each occurrence increments the `alt_chunk` container-exclusion
count. A valid existing internal relationship target is added as a normalized,
package-relative entry in `coverage.excluded_parts`; external target URLs are
never disclosed. A missing, ambiguous or unsafe internal relationship target
refuses inspection rather than reporting complete coverage.

Headers, footers, footnotes, endnotes and comments remain excluded OPC parts.
They are listed in `coverage.excluded_parts`; the tool does not claim to inspect
or count their text. A successful main-body scan is therefore not a whole-DOCX
text-coverage claim.

Empty supported paragraphs participate in positional indexing and can be read
by reference, but `browse` omits them.

## Text composition

### `accepted_current_v1`

`accepted_current_v1` is a mechanical tracked-change projection, not a legal
conclusion that wording is operative. In XML order it:

- includes `w:t` outside deletion and move-from wrappers;
- includes `w:t` inside `w:ins` and `w:moveTo`;
- excludes content inside `w:del` and `w:moveFrom`;
- never contributes `w:delText`;
- maps `w:tab` to U+0009;
- maps `w:br` and `w:cr` to U+000A;
- maps `w:noBreakHyphen` to U+002D; and
- performs no whitespace collapse, case folding, Unicode normalization,
  typographic substitution or locale-specific transformation.

The reading does not evaluate fields, render numbering into body text, resolve
images, interpret hidden-text formatting, calculate pagination or reproduce
Word's visual line wrapping. In particular, `w:rPr/w:vanish` is not analyzed:
text carrying that formatting is returned by this mechanical reading and may
be verified by `verify_quote`. Heading labels are separate navigation-only
observations.

Every paragraph result discloses `has_tracked_text_revisions`; the top-level
result discloses whether any indexed paragraph contains supported tracked text
revision wrappers.

## Hash-bound positional references

The v0.3 paragraph reference is both a positional reference and the paragraph
evidence anchor. There is no separate label-based clause anchor.

`paragraph_ref.v1` is closed and path-free:

```json
{
  "schema_version": "paragraph_ref.v1",
  "ref_type": "paragraph",
  "file_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "part_name": "word/document.xml",
  "paragraph_index": 42,
  "paragraph_text_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "reading_mode": "accepted_current_v1",
  "container_policy": "canonical_body_flow_v1"
}
```

`paragraph_index` is zero-based in the complete canonical body-flow index.
`paragraph_text_sha256` is SHA-256 over the exact UTF-8
`accepted_current_v1` text. The file hash, policy fields, position and text hash
must all re-resolve on the immutable snapshot. A reference has no identity
across different file bytes, even when its label or text looks the same.

`section_ref.v1` is also closed and path-free:

```json
{
  "schema_version": "section_ref.v1",
  "ref_type": "section",
  "file_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "part_name": "word/document.xml",
  "heading_paragraph_index": 42,
  "heading_text_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "reading_mode": "accepted_current_v1",
  "container_policy": "canonical_body_flow_v1"
}
```

A structural section begins at one supported outline-heading paragraph and
ends before the next supported heading of the same or higher outline level, or
at the end of canonical body flow. The range is recomputed from the exact file
hash, heading position, heading text hash and policy. It is a navigation range,
not semantic clause identity or cross-round lineage.

Heading navigation uses Word outline levels. Its closed facts are nullable
`label`, nullable `heading`, numeric `level`,
`basis: word_outline_level_v1`, and nullable `label_basis` from
`word_numbering_v1 | explicit_heading_text_v1`. Labels and headings never
replace a hash-bound reference. Levels 0-8 are headings; Word level 9 is body
text and does not create a section. Values outside 0-9 refuse inspection as
invalid OOXML before any successful decision record is written. This applies
to direct paragraph properties and resolved paragraph styles.

## Four mode results

### `outline`

`outline` returns `sections`. Each section contains only:

- `section_ref`;
- nullable `label` and `heading`;
- `level`, `basis` and nullable `label_basis`; and
- `start_paragraph_index` and `end_paragraph_index_exclusive`.

An outline section must not contain paragraph body text, a preview, a snippet
or matched text. Duplicate labels/headings remain separate sections.

### `literal_search`

`literal_search` searches every phrase independently inside every non-empty
supported paragraph. One match candidate represents one `(paragraph,
phrase_index)` pair and contains:

- `phrase_index`, the actual `match_basis` and `occurrence_count`;
- the hash-bound `paragraph_ref`;
- `container_kind` and optional section navigation; and
- one bounded snippet around the first occurrence.

The snippet preserves exact source text, reports match offsets within the
snippet and has explicit before/after truncation flags. Its context radius is
160 Unicode scalar values on each side; the matched phrase itself may be
longer. `occurrence_count` prevents the first snippet from being mistaken for
the only occurrence.

Search output is evidence for discovery, not complete paragraph text. Use the
returned paragraph reference in `read` before presenting surrounding paragraph
wording as a complete quotation. Zero matches is a successful complete scan.

### `browse`

`browse` pages every non-empty supported paragraph in canonical order. Each
item contains:

- `paragraph_ref`;
- `container_kind`;
- `has_tracked_text_revisions`;
- nullable section navigation; and
- complete `text` under `accepted_current_v1`.

The complete text is protected by the per-paragraph and per-response text
caps. `browse` is an explicit fallback when outline/search discovery is not
enough; it is not an unbounded whole-document dump.

### `read`

A paragraph read re-resolves exactly one `paragraph_ref`, rejects a cursor and
returns one complete paragraph item.

A section read re-resolves one `section_ref` and returns its non-empty member
paragraphs in canonical order. It may paginate with the same source-bound
cursor and response text cap as `browse`. The output includes
`selection_kind: section` and the recomputed section navigation. No synthetic
delimiter is inserted between member paragraphs.

Neither read form truncates a paragraph. A stale hash/policy/position or a
paragraph above the supported text cap refuses the operation.

## Deterministic cursor pagination

Potentially long lists use an opaque cursor of the form:

```text
c1:<next-offset>:<binding-sha256>
```

The binding digest covers the cursor schema, immutable source SHA, mode,
`search_scope`, `reading_mode`, `container_policy`, the applicable match or
ordering policy, phrases or selection as applicable, and the SHA-256 of the
complete ordered eligible result set. `max_items` is intentionally excluded so
a caller may change the next page size without changing the result identity or
invalidating the cursor. A cursor from different bytes, policy, request or
eligible result set is `cursor_mismatch`; malformed or out-of-range cursors are
`invalid_cursor`. There is no server-side cursor state.

Every page reports `next_cursor`; `null` means complete. Coverage reports the
eligible item count, returned item count, cursor offset and whether output is
explicitly truncated at this page boundary. Pagination is deterministic and
caller-visible. Elapsed time never selects a subset.

## Coverage and container analysis

Every successful result contains this closed coverage shape:

```json
{
  "scan_complete": true,
  "body_paragraph_count": 73,
  "nonempty_body_paragraph_count": 70,
  "eligible_item_count": 4,
  "returned_item_count": 4,
  "cursor_offset": 0,
  "output_truncated": false,
  "complete_literal_match_count": 4,
  "included_parts": ["word/document.xml"],
  "excluded_parts": [
    "word/header*.xml",
    "word/footer*.xml",
    "word/footnotes.xml",
    "word/endnotes.xml",
    "word/comments*.xml"
  ],
  "included_containers": ["body", "table_cell"],
  "container_coverage": {
    "schema_version": "canonical_body_flow_v1",
    "indexed_paragraph_count": 73,
    "body_paragraph_count": 65,
    "table_cell_paragraph_count": 8,
    "excluded_subtree_count": 1,
    "excluded_paragraph_count": 1,
    "excluded_by_kind": {"text_box": 1},
    "excluded_paragraphs_by_kind": {"text_box": 1},
    "coverage_complete": false,
    "legacy_two_field_anchor_safe": false
  }
}
```

`complete_literal_match_count` is an integer only for `literal_search` and
`null` for other modes. `scan_complete: true` means the complete declared main-
body scope was classified before success. `output_truncated` describes only an
explicit cursor page, not an incomplete scan.

`coverage_complete` is true only when canonical container analysis pruned no
subtree in `word/document.xml`. It does not expand scope to excluded OPC parts.

## Companion `revision_inventory.v2`

V0.3 `extract_redlines` and the compatibility gate use the same immutable
canonical body-flow classification. Its container-aware closed inventory is:

```json
{
  "schema_version": "revision_inventory.v2",
  "scope": "word/document.xml",
  "container_policy": {"...": "canonical body-flow coverage object"},
  "tracked_text_revision_elements": 8,
  "total_revision_elements": 9,
  "in_scope_revision_elements": 7,
  "decoded_revision_elements": 6,
  "unsupported_revision_occurrences": 1,
  "unsupported_revision_kind_count": 1,
  "excluded_container_occurrences": 2,
  "excluded_container_kind_count": 1,
  "unsupported_by_kind": {"rPrChange": 1},
  "excluded_by_container": {"text_box": 2},
  "partition_valid": true,
  "all_in_scope_revision_elements_decoded": false,
  "all_revision_elements_decoded": false,
  "emitted_change_unit_count": 3
}
```

The invariants are:

```text
total_revision_elements
  = in_scope_revision_elements + excluded_container_occurrences
in_scope_revision_elements
  = decoded_revision_elements + unsupported_revision_occurrences
unsupported_revision_occurrences
  = sum(unsupported_by_kind.values())
excluded_container_occurrences
  = sum(excluded_by_container.values())
```

The categories are non-overlapping. A revision under a pruned container is
classified only as excluded-container. `emitted_change_unit_count` remains a
separate grouping count and is not part of either equation.

`inspect_document` exposes this same-snapshot inventory in every inspection
response alongside the simpler tracked-revision warning. This lets the caller
distinguish decoded body revisions, unsupported in-scope markup and revisions
inside excluded containers without making a second file read.

## Conservative v0.2 anchor compatibility gate

The v0.2 change-unit anchor has only `change_unit_id` and `file_sha256`; it does
not identify a paragraph traversal policy. Unrestricted legacy descendant
traversal can number content that `canonical_body_flow_v1` prunes, so the same
ordinal must not be reinterpreted optimistically under v0.3.

Canonical container analysis emits `legacy_two_field_anchor_safe: true` only
when there are zero compatibility-impacting excluded or unknown subtrees in
`word/document.xml`. The unsafe set includes text boxes, AlternateContent,
nested paragraphs and unknown containers carrying paragraphs, revision markup
or plain text atoms. A subtree is not safe merely because it contains no
tracked revision.

When the flag is true, a legacy two-field anchor may proceed through the normal
file-hash, ordinal and complete change-unit fingerprint checks. When false,
`verify_quote`, `preflight_edits` and `apply_edits` refuse it with
`legacy_anchor_ambiguous`. Apply recomputes the gate on its immutable source
snapshot before proof binding or publication; successful verification or
preflight cannot waive it.

This design does not promise a frozen v0.2 extractor running in parallel. New
v0.3 change-unit anchors carry an explicit schema version, container policy and
unit fingerprint. Historical decision records are not rewritten.

## Deterministic limits and no partial timeout contract

The fixed v0.3 inspection limits are:

- existing DOCX/ZIP and XML structural limits;
- 10,000 indexed canonical body paragraphs;
- 2,000,000 Unicode scalar values across indexed paragraph text;
- 50,000 Unicode scalar values in one returned paragraph;
- 100,000 Unicode scalar values across one returned page;
- 1-100 requested items per page, default 50;
- 1-20 phrases;
- 2,000 Unicode scalar values per phrase and 10,000 in aggregate;
- 10,000 literal match candidates; and
- 10,000 occurrences for one paragraph/phrase candidate.

The limits are disclosed in every successful response, including
`requested_max_items` and `wall_clock_partial_results: false`. Exceeding a
deterministic cap fails with no hidden partial result. A transport cancellation
or client timeout is not converted into success and establishes no document
fact. V0.3 defines no wall-clock cutoff that chooses which items appear in a
successful page.

## Common response and provenance

Every successful result contains:

```json
{
  "mode": "literal_search",
  "path": "/Users/example/Deals/Acme/current.docx",
  "file_sha256": "...",
  "part_name": "word/document.xml",
  "search_scope": "word_document_xml_body_v1",
  "reading_mode": "accepted_current_v1",
  "container_policy": "canonical_body_flow_v1",
  "has_tracked_text_revisions": true,
  "revision_inventory": {"...": "revision_inventory.v2"},
  "coverage": {"...": "complete declared-scope and page coverage"},
  "limits": {"...": "fixed v0.3 limits"},
  "next_cursor": null,
  "matches": [{"...": "literal-search match"}],
  "match_basis": "exact_literal",
  "phrase_count": 1,
  "producer": {
    "name": "veqtor-mcp",
    "version": "0.3.0.dev0",
    "build": "source-snapshot-v1-sha256:..."
  },
  "record_id": "dr_001",
  "record_status": "written"
}
```

The mode-specific collection is exactly one of `sections`, `matches` or
`paragraphs`. `read` also returns `selection_kind`; section reads return
section navigation. The output schema must reject a mode-incompatible
collection or field.

The compact decision-record projection omits paths and paragraph/snippet text,
digests the closed input, and retains source hash, mode, policy identifiers,
coverage/limit counters, producer identity and a bounded digest/sample of
observed paragraph or section references. It keeps the existing best-effort,
local, non-tamper-evident assurance boundary. This minimization applies only to
compact export; the private raw JSONL journal retains the canonical workspace,
request paths and literal-search phrases and must remain private.

## Controlled errors

The implementation uses stable codes rather than raw exceptions, including:

- `invalid_mode`, `invalid_request`, `invalid_limit`;
- `phrases_missing`, `match_basis_missing`;
- `selection_missing`, `invalid_reference`, `reference_mismatch`,
  `reference_not_found`;
- `invalid_cursor`, `cursor_mismatch`;
- `resource_limit_exceeded`; and
- `legacy_anchor_ambiguous` on the existing anchor-consuming tools.

Every controlled failure after an immutable source snapshot is readable
includes `observed_source_sha256`. It never exposes raw exception text.

## Definition of done

Automated implementation acceptance requires synthetic, redistributable
fixtures proving:

1. Plain and empty body paragraphs, outline headings, computed numbering and
   manual heading labels produce deterministic canonical positions and refs.
2. Insert, delete, move-to, move-from, counter, tab, break and no-break-hyphen
   cases produce the exact `accepted_current_v1` text.
3. SDTs around paragraphs/tables, SDTs inside cells, nested tables and multiple
   rows/cells pass through once in canonical XML order.
4. Text boxes, drawings/objects, relationship-backed altChunk content,
   AlternateContent, nested paragraphs and unknown text-bearing wrappers are
   pruned, counted and never leak text. Internal altChunk target parts are
   disclosed safely in the live inspection response, while external URLs are
   not. Compact decision-record export never repeats document-controlled target
   names: it retains their count and complete-list digest in the bounded
   `excluded_internal_parts` projection with an empty sample.
5. Headers, footers, footnotes, endnotes and comments stay visibly outside the
   declared scope.
6. Outline contains no paragraph text, preview, snippet or matched-text field;
   duplicate headings remain separate entries.
7. Literal search covers all three match bases, overlapping occurrences,
   duplicate matches, first-occurrence snippets, occurrence counts, phrase
   indices and zero-match complete scans.
8. Browse and section-read cursors produce stable pages with no overlap or
   omission, correct `next_cursor`, source/request binding and exact
   `max_items`/response-text limits. Paragraph read rejects a cursor.
9. Paragraph and section refs re-resolve on identical bytes and refuse changed
   file hashes, policies, positions or text hashes.
10. Paragraph reads return one complete selected paragraph. Section reads
    return complete ordered member paragraphs and never synthesize delimiters.
11. `revision_inventory.v2` satisfies every partition equation for decoded,
    unsupported and excluded revision fixtures and shares the exact container
    analysis reported by inspection.
12. Legacy fixtures with a revision and, separately, plain text inside an
    excluded/unknown subtree, including malformed payload inside a known Word
    property subtree, report `legacy_two_field_anchor_safe: false`; verify,
    preflight and apply refuse two-field anchors and publish no output.
13. A fixture with zero compatibility-impacting excluded/unknown subtrees
    reports the flag true and keeps legacy anchors usable, proving the gate is
    narrow rather than a blanket rejection.
14. Every deterministic cap succeeds at its boundary and refuses above it.
    Simulated transport cancellation never becomes partial successful output.
15. Compact decision-record fixtures prove path/text omission from the compact
    export only, bounded observed reference samples/digests, producer identity
    and append-only readability beside historical v0.1/v0.2 records. The raw
    local JSONL journal deliberately retains the workspace, request paths and
    literal-search phrases and must remain private.
16. An English synthetic contract lets Claude use `outline` or
    `literal_search`, then `read`, to cite unchanged Clause 9 and Clause 14.2
    from exact refs while explaining container/part exclusions. A Russian
    fixture may prove raw Unicode preservation only; it cannot support a claim
    of Russian clause-discovery coverage.

Only after those automated fixtures pass should a Claude Desktop product
acceptance ask a user to discover an unchanged provision, read the complete
hash-bound paragraph or section and explain the declared coverage boundary.
That is separate from MCPB installation and release acceptance.
