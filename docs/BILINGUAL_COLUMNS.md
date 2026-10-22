<!-- SPDX-License-Identifier: Apache-2.0 -->

# Explicit bilingual column evidence (F6)

The development candidate adds two read-only MCP tools. They do not decide the
translation or legal equivalence of obligations, edit a DOCX, infer language
columns, write a journal, or provide F7 row insertion/deletion.

## Inventory and references

`read_document_tables(path, file_sha256?, affected_paragraph_refs?)` captures one
immutable DOCX. The optional hash binds a follow-up call to the same bytes.
Its `bilingual_table_inventory.v1` result contains a flat `items` inventory:

- `table`, `row`, and physical `cell` records retain the exact hash-bound
  `table_ref.v1`, `table_row_ref.v1`, and `table_cell_ref.v1` references from
  `table_map`. Cell indices are physical XML positions; `grid_start` and
  `grid_span` describe the current column grid. Merges, omitted columns,
  nested tables, structural/property revisions and issues remain visible.
- `paragraph` records include the current mechanical text, `paragraph_ref.v1`,
  owning physical cell, and whether the paragraph has unsupported numeric-scan
  content. All canonical body paragraphs, including empty ones and multiple
  paragraphs in a cell, retain their sequence. Nested paragraphs belong to
  their own cell. There is no pairing by adjacent paragraph indices.
- `text_reference` records give exact current-text candidates such as
  `clause 5.2`, `section 9`, `п. 7.3`, and `§ 4`, with code-point offsets and
  paragraph references. `field` records expose raw `fldSimple`, `fldChar`, and
  `instrText` nodes, their literal instructions and marker types; these can
  include historical revision markup. They are observations, not a resolved
  field model or a claim that a target is valid.

After a structural edit, refresh the inventory and pass **fresh** paragraph
references in `affected_paragraph_refs` to mark the selected affected area.
The reference search still scans every canonical main-body paragraph, and
marks observations in that area. It never renumbers text or rewrites field
codes. `target_validity` is `not_assessed`; semantic reference completeness is
`not_claimed`. Headers, footers, notes, comment text, excluded containers and
implicit semantic references are outside the disclosed scan. Read `coverage`,
including the canonical-flow inventory and excluded parts.

The result is one complete bounded report, with no pagination or truncation.
Every paragraph and the entire encoded result are checked before return,
including a long final paragraph. Limits are 2,000 paragraphs, 50,000 current
characters per paragraph, 500,000 current characters overall and an 8 MiB
encoded result budget (including an envelope reserve). Existing table-map
limits also apply. Exceeding a limit refuses the whole report; it never turns
unread content into a claim of complete review.

## Selected conditions

`check_bilingual_conditions(path, file_sha256, cell_pairs)` requires the exact
hash and full references from the inventory. Each selected cell pair supplies:

- A unique `pair_id`, exact `table_ref` and `row_ref`.
- `left` and `right` selections, each with an exact `cell_ref`, an explicit
  `language` label, `grid_column`, and `format`. A cell must occur only once
  across the request. Both cells must belong to the selected table and row,
  occupy different selected grid columns, and have unmerged, unambiguous
  geometry. Nested cells, grid gaps, unsupported wrappers and structurally
  revised rows refuse pairing. Multiple ordinary paragraphs per cell work.
- A list of explicitly selected `conditions`. Each has a unique
  `condition_id`, and `left`/`right` objects containing `paragraph_ref`,
  `start`, `end`, and an exact nonempty `quote`. Offsets are Python/Unicode
  code points, with an exclusive end. Spans must belong to the selected cell,
  must not overlap on either side, and must not cut a numeric candidate.

`format` requires `decimal_separator` (`.` or `,`),
`grouping_separator` (null, `.`, `,`, ordinary space, NBSP, or narrow NBSP),
and `date_order` (`DMY`, `MDY`, `YMD`, or `reject_ambiguous`). Decimal and
grouping separators must differ. Only the selected grouping character is
accepted; groups must have three digits. Formats are explicit caller choices,
not detected from a language label. Thus `1,000` can mean 1000 or 1 depending
on the specified format. Decimal arithmetic preserves the parsed value and
applies explicit thousand/million/billion scale without binary rounding.

Tokenization retains a maximal digit-looking candidate before format parsing,
including unsupported apostrophes, thin spaces and malformed grouping. These
remain unsupported whole candidates; choosing an inner suffix such as
`000 USD` from `1'000 USD` refuses as a partial value. Ordinary punctuation
followed by whitespace can separate a clause label (`1. 100 USD`) or separate
explicit quantities; it is not invented as a grouping convention. ASCII unary
`+`/`-` immediately before digits are supported. Separated signs, unsupported
Unicode sign notation and plus/minus notation remain visible unsupported
context, preserving the original quote and offsets. No raw text is rewritten.

The bounded EN/RU lexical grammar observes digit-written amounts, explicit
ISO currency codes and selected unambiguous currency names/symbols, percentages,
numeric dates, durations and common mass/volume/package units. Date comparison
uses real calendar validation. ISO year-first dates are supported independently
of the selected order. Non-year-first dates use the explicit order, or remain
unsupported when ambiguous under `reject_ambiguous`. Calendar and business/
working days are distinct. An unspecified day basis does not establish a
match. Ambiguous currency symbols such as `$` and `¥` do not establish a
currency. Units and currencies are not converted.

Recognized units use closed sets of exact EN/RU forms. `3 часа` and `3 года`
are valid hours/years; `3 части`, `100 частей` and `100 годных изделий` are
unrecognized numeric context, never hours/years. Additional known units or day
bases before/after an already typed value (`kg 100 USD`, `100 kg mg`,
`30 calendar days business days`) remain one unsupported context span.
Compound slash/rate notation, including Unicode division slashes, remains
unsupported and cannot expose a separately selectable denominator. Ordinary
prose after a typed amount is retained as prose. A date-shaped component
attached to an identifier (`INV2026-11-23` or `2026-11-23A`) is an unsupported
whole candidate; selecting its inner date refuses as a partial value.

Each selected condition has one of four statuses:

| Status | Meaning |
| --- | --- |
| `numeric_values_and_units_match` | Exactly one supported value on each side has equal Decimal/date value, kind, currency/unit and day basis. This is only numeric evidence for that caller-selected pair. |
| `mismatch` | Exactly one supported value on each side differs in value or units. |
| `not_established` | Either side has zero values or more than one candidate. Narrow the selection into explicit single-value pairs. |
| `unsupported_format` | Selected content contains an unsupported candidate or an unsupported paragraph object/field. |

No multiset comparison is performed. For example, a row containing payment in
30 calendar days and notice in 60 calendar days does not match a translation
that exchanges those numbers. Selecting the entire two-obligation row returns
`not_established`; explicitly selecting payment-to-payment and notice-to-notice
reveals two mismatches. Caller-selected condition correspondence is itself
unverified. Even after all selected values match, `obligation_equivalence`
and document-wide numeric coverage remain `not_claimed`.

`values.left` and `values.right` enumerate every observed numeric candidate
in **all paragraphs of the selected cells**, including values outside selected
conditions. Each retains its paragraph reference, exact quote/offsets, kind,
normalized value, scale, currency/unit, day basis, support status/reason,
`value_id`, and nullable `paired_condition_id`. No values are paired merely
because their order or number is the same. Coverage counts total, unsupported,
paired and unpaired values and selected conditions; unsupported values also
count as unpaired. A successful selected condition does not hide uncovered
values elsewhere in the cells. Empty and text-only spans never match.

Complex formulas/compound rates are not evaluated. Formula markers and digit
candidates remain unsupported observations. Named-month dates and conservative
dictionary candidates for numbers in words remain visible as unsupported;
the dictionary is not complete language understanding or full extraction of
amounts in words. Unrecognized numeric syntax/units remain unsupported. The
report makes no claim to detect every implicit quantity, semantic unit or
cross-reference. Fixed work limits allow at most 100 cell pairs, 100 selected
conditions per pair, and 20,000 observed values. No partial result is returned
when a budget fails.

## Editing both columns (S04)

F6 reuses the existing `preflight_document_edits` / `apply_document_edits`
atomic batch. After showing and authorizing concrete text edits, send both
columns' fresh `paragraph_ref` targets in one operations array. If either side
is inapplicable, preflight refuses the batch and neither side is saved. A
successful apply changes both columns under the same proof and backup. Refresh
all table/row/cell/paragraph references after any save, including a Word save
that leaves the visible paragraph text unchanged. This is not F7 structural
editing and does not close native Word or real-document acceptance.

## Evidence boundary

`tests/test_bilingual.py` uses synthetic DOCX only. It covers the F6 portions
of S04-S08, S11 and S13, schema validation and real in-memory MCP calls,
malformed/stale selection refusals, exact quote and count invariants, long late
quotes, and the existing atomic batch. Development tests and an installed
wheel smoke are technical evidence. Native Word acceptance, chosen real
document pairs, F7 row editing and public release remain separate gates.
