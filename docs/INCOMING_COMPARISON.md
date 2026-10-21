<!-- SPDX-License-Identifier: Apache-2.0 -->

# Incoming-version comparison (development)

`compare_incoming_version` compares two explicitly selected exact DOCX files.
It is a read-only addition to the development MCP surface, not a release,
sending operation, clean-copy exporter or negotiation journal.

## Inputs and capture

Required: `sent_path`, `sent_sha256`, `incoming_path`, `incoming_sha256`.
Both digests are lowercase SHA-256 of the complete package. Obtain them from
inspection or an exact snapshot; never derive them from a filename. Paths may
also name `source.docx` in a persisted snapshot. A source-role name is a client
assertion; it does not prove transmission, receipt, chronology or authorship.
The caller's explicit selection is preserved even when both sources have the
same bytes. Each request captures bounded immutable bytes and rechecks both
hashes. A source change refuses the request.
Snapshot capture retains the foundation's supported local-filesystem and
no-symlink path requirements; use the actual canonical source path.

Optional: `max_items` (default 50, range 1–100) and `cursor`. Follow every
`next_cursor` with the same sources and page size. Cursors bind both snapshots,
the full result, policies and limits. Switching sources, roles or page size
refuses the cursor. The implementation does not select the latest file or write
snapshots automatically. All successful MCP results have `record_status=disabled`.

## Result and complete membership

`incoming_comparison.v1` contains sources, observations, coverage, limits,
limitations, `comparison_id`, typed `items`, pagination and `next_cursor`.
The complete comparison is calculated before pages are returned. Every
canonical main-body paragraph, including empty paragraphs in table cells,
belongs to exactly one comparison group on each side.

The item types are:

| Kind | Contents |
| --- | --- |
| `comparison_group` | Stable group ID, counts on each side, matching basis, observations, explanation and concrete projection comparisons |
| `paragraph` | Group membership, side, exact `paragraph_ref.v1`, complete current/rejected quotes, cell reference, projection availability and revision/property/XML observations |
| `table`, `row`, `cell` | Exact snapshot-bound references, current topology, property-markup digest and structural issues; cells include paragraph indices |
| `package_part` | Part name, both available uncompressed-byte hashes and same/changed/one-sided status; content interpretation is not claimed |

A group and its paragraph members can be on different pages. Read every page
before treating all quotes and memberships as delivered. Repeated/empty blocks
are individual members, not one collapsed paragraph. Group membership counts
must balance the indexed counts; no unmatched block is dropped. Group records
contain counts rather than giant embedded quote arrays, so one large ambiguous
interval remains pageable. Quotes are never truncated.

## Matching and explanation

Matching uses complete Unicode strings without whitespace normalization.
Unique candidates can be supported by current/current, current/rejected,
rejected/current or rejected/rejected equality. A shared repeated value prevents
an arbitrary unique match. Empty/whitespace text is not a global content anchor.
Adjacent anchors in both sequences define a positional interval. A single
paragraph on each side may be compared by that position; this is not proof of
semantic identity or derivation. A one-sided bounded interval shows addition or
removal in that interval. Multiple blocks, splits/joins, ambiguous repeats and
disconnected leftovers remain explicit unresolved groups. Crossing unique
anchors disclose changed order, not a verified Word move operation.

`accepted_current_v1` and `pending_text_revisions_rejected_v1` are text
projections. They are not clean documents, Word rendering or a historical
accept/reject simulation for all structures. Each tested equality specifies
both projection modes, text hashes and equal/different/unavailable status.

| Explanation | Claim |
| --- | --- |
| `visible_in_current_revisions` | Complete incoming rejected text equals the selected sent current text and incoming text revisions are present |
| `prior_revision_decision_compatible` | A concrete checked projection can explain a decision on earlier markup; this is compatibility, not a reconstructed event |
| `not_explained_by_available_markup` | The established positional/exact pair has a difference not explained by the tested available markup, with no unresolved old pending-decision combinations |
| `indeterminate` | Correspondence, projection, move handling or partial old decisions prevent a stronger explanation |
| `not_applicable` | No text-change explanation is claimed; inspect separate markup/property/XML observations |

The engine does not enumerate combinations of partial decisions on old pending
revisions. A remaining unexplained combination is indeterminate. A revision
somewhere in a paragraph does not explain all changes. No category establishes
who edited the file, intent or that Track Changes was turned off.

## Separate observation surfaces

Equal current text does not imply unchanged history. Revision markup and raw
revision identifiers have separate digests; changing only IDs is not presented
as a content change. Revision metadata changes remain visible. Paragraph XML
and property markup are observed separately. Their digests ignore XML prefixes,
Word rsids and paragraph/text IDs, but do not claim rendered equivalence.

`current_table_topology` compares table nesting, current grid-column counts,
row gaps, physical cells, spans/merges and the ordered kinds of cell content.
It excludes rendered widths/heights. Table/row/cell property digests separately
include grid widths, cell widths, row heights and historical property markup.
`table_property_markup_sequence_equal` is null when structural inventory is
incomplete. Equal topology does not prove equal dimensions or editability.
No historical table grid is reconstructed.

All package parts are compared as exact uncompressed bytes. Equal parts with a
different package hash identify repackaging. Changed headers, footers, notes,
styles, comments, relationships or unsupported objects remain visible without
inventing their content. A part-byte difference is not automatically a legal,
visual or semantic change. `document_equivalence` is always `not_claimed`.

## Coverage, budgets and errors

Coverage separates complete partition of indexed paragraphs from complete
supported scope. Excluded paragraph/subtree counts, excluded parts, unavailable
projections and table issues remain explicit. Even complete supported scope
covers canonical main-body text only, not all visible Word content.

Existing DOCX ingress limits are unchanged. Existing inspection limits remain
10,000 paragraphs, 50,000 current characters per paragraph and 2,000,000 current
characters per document. Rejected projections add at most 2,000,000 characters
per document. Each paragraph has at most 1,000 reported revision occurrences;
candidate memberships are capped at 80,000. Reports are capped at 100,000 items
and 64 MiB of serialized core data. Pages have at most 100 items, 100,000 quoted
characters and 2 MiB. Table inventory retains its separate fixed limits.

Global projection validation is reused within each document. Duplicate
candidates are indexed as memberships, never expanded as a Cartesian product.
Interval checks use sorted incoming ranks without rescanning all anchors.

Invalid hashes/cursors, stale sources, unsupported source packaging or exceeded
budgets refuse with an error. A hard-limit refusal is not a successful empty
comparison. An unrepresentable table inventory can instead leave an explicit
partial report with unchanged paragraph membership and package observations.
There are no wall-clock partial results or silent truncation. Cancellation does
not transform incomplete computation into “no differences”.

## Internal API and validation boundary

`veqtor_docx.incoming_comparison` exposes `build_incoming_comparison(sent,
incoming)` using exact `DocumentSnapshot` values and
`page_incoming_comparison(comparison, max_items=50, cursor=None)`.
`compare_incoming_version` is the path/capture adapter used by MCP.

The tests use invented documents and cover I01–I11, partition balance,
properties and excluded scope, pagination, boundary failures and compatibility
with existing paragraph projections. They do not replace acceptance against an
explicitly selected real sent/incoming pair or rendered Word review.
The development installed-wheel smoke calls the new tool through modern and
legacy stdio and an in-memory client, drains its pages, balances memberships
and checks both synthetic source hashes again. Historical version inventories
remain separate.
