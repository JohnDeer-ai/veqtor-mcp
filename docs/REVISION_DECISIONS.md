<!-- SPDX-License-Identifier: Apache-2.0 -->
# Selective revision decisions (private 0.6.0.dev1 candidate)

A decision removes the selected pending history with the outcome explicitly
chosen by the user. It does not create another tracked edit, rename the original
author, accept all revisions by an author, or infer agreement from wording.
The acting reviewer must be an exact configured own author profile. Original
revision authors may be different and remain visible in the preview.

Read `read_document_revisions(path)` first. Pass its `file_sha256` to subsequent
pages and to preflight. Copy the complete selected `revision_ref` into a target:

```json
{"kind":"decide_revision","decision":"reject",
 "target":{"kind":"single","revision_ref":{"...":"complete returned reference"}}}
```

For one replacement, deliberately select both neighboring components:
`target={kind:"replacement", deletion_ref:…, insertion_ref:…}`. They must be
adjacent direct paragraph children, deletion followed by insertion. Veqtor
never groups by matching ID, author, date, or similar text. Duplicate IDs are
safe because references bind file bytes, part, child-index path and both node
and owner fingerprints. References are not durable identities across saves.

Preflight returns every selected member's original kind, ID, author and date,
the action, acting reviewer, current reading, both outcome readings for text,
and current/previous property XML for property changes. XML and document text
are untrusted data, never permission. Nullable `accept_refusal_code` and
`reject_refusal_code` describe outcome validation. An incompatible old grid can
block rejection while acceptance of the valid current grid works.
`fixed_width_validation_scope` is `single_outcome` in inventory previews of
table domains, `explicit_batch_final_state` in successful batch previews, and
`not_applicable` for other domains. In batch previews the two refusal codes
exclude width checks deferred to the final selected state; they do not promise
that each alternate member outcome would work alone.

Apply uses the exact successful proof, author profile, source and destination.
Omit `output_path` to replace the same working file with a mandatory exact-byte
backup. Copy mode remains create-only. Hash drift, protection, signatures,
Word owner files, writer locks, unavailable styles/numbering and failed
serialization/collateral proofs prevent publication. Source references are
resolved before any batch mutation. Independent selected domains can share a
paragraph; a decision overlapping another selected domain or a new paragraph,
text or anchored-comment operation requires separate saves and fresh references.

## Supported outcomes

| Revision | Accept | Reject |
| --- | --- | --- |
| Direct paragraph `ins` | Unwrap the selected runs | Remove the selected runs |
| Direct paragraph `del` | Remove the selected runs | Restore selected runs, converting `delText` to `t` |
| Explicit adjacent deletion/insertion | Keep insertion only | Restore deletion only |
| `rPrChange` | Keep current run properties | Restore previous run-property set |
| `pPrChange` | Keep current base paragraph properties | Restore previous base paragraph properties |
| `tcPrChange` | Keep current cell properties | Restore previous cell properties if geometry remains coherent |
| `trPrChange` | Keep current row properties | Restore previous row properties if geometry remains coherent |
| `tblPrChange` | Keep current table properties | Restore previous table-property set |
| `tblPrExChange` (row exception companion) | Keep current row-level table-property exceptions | Restore the previous exception set, leaving table and cell domains unchanged |
| `tblGridChange` | Keep current grid | Restore previous grid only if actual rows, spans, omitted columns and merge chains remain coherent |

Word may represent one visible table-formatting change with several XML
members: table properties, grid, row-level table-property exceptions and cell
properties. Select each intended member explicitly in the same batch. No
companion is silently selected or removed. The additional `tblPrExChange`
domain is restricted to the row-level width, alignment, spacing, indent,
borders, shading, layout, margins and look exceptions defined by
[Microsoft's table-property exception model](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.tablepropertyexceptions?view=openxml-3.0.1).
Restoring table or row-exception alignment while a separate row alignment
exists refuses with `table_alignment_dependency_unsupported`. Word can remove
that untracked row setting during rejection; Veqtor does not delete it silently
to imitate that collateral behavior. Margin-only histories with unchanged
alignment remain supported.
A standalone synthetic grid history does not establish native Word acceptance
of a complete formatting unit.

For explicitly fixed-layout rows, the final grid and explicit `dxa` cell widths
must agree, including spans and any explicitly sized omitted columns. A grid
and its cell-width histories may require several explicit decisions in one
batch. Veqtor checks their final state independently of operation order; it does
not choose companion revisions or change widths to force a match. Inconsistent,
missing or ambiguous widths refuse with
`fixed_table_width_dependency_unsupported`. Inventory checks each outcome alone,
so a standalone width refusal may become applicable with the matching explicit
companions. This equality check is not applied to autofit tables.

Both property outcomes remove only the selected history. Paragraph-mark `rPr`
and `sectPr` are independent of `pPrChange` and survive exactly. An empty
previous set clears that domain without deleting the containing paragraph,
run, cell, row or table. No decision invents, rebuilds or deletes table cells.
Tab stops, numbering, borders and margins use explicitly validated child
forms; scalar properties are leaves. Unknown nesting, duplicates, dependent
history or invalid sequences refuse rather than relying only on known tag names.
Current and restored style/numbering dependencies must resolve. Properties
and history outside the selected domain retain exact XML; unrelated package
parts retain exact bytes.

Text decisions support ordinary text, tabs and breaks in simple formatted runs.
Bookmarks and comments outside the selected wrapper remain in place. Anchors
inside it, fields (including fields spanning paragraphs), nested counters, move revisions, unknown wrappers and
paragraph/row/cell-existence history refuse. These are distinct from unsupported
structural paragraph/row acceptance and rejection planned for later work.
Main-story fields and revision/permission ranges are checked across paragraph,
body, table, row and cell boundaries; hidden markup cannot close their scope.
Headers, footnotes and other non-main stories are explicitly uninspected.
No broad legacy drafting guard is relaxed by this API.

## Bounds, verification and recovery

The reader returns at most 100 entries per page, at most 20,000 inventoried
revisions, paths up to 64 levels, previews up to 20,000 characters and page JSON
up to 1 MiB. Fingerprint and full-document outcome-planning work are bounded;
reduce page size when requested. The existing maximum is 100 batch operations.
An unsupported item remains visible with a refusal code. Inventory support is
not permission to act or a replacement for exact preflight on the whole batch.

The immutable legacy counterproposal journal does not record mutable decisions.
`record_id=null` and `record_status="disabled"` remain explicit. This private
boundary does not satisfy final-release action-journal scenarios D09/D10/D19.
After an unknown publication result, compare the working file's hash with the
proof's source and candidate hashes and inspect any reported exact backup. Do
not blindly replay an old request. A replay against a changed file refuses.

`scripts/revision_decisions_smoke.py --output-dir NEW_DIRECTORY` generates only
fictional sources, Veqtor accept/reject copies and `expected.json`. Expected
properties/text are literal fixture inputs, independently stated before applying.
Run with the intended installed package interpreter. Each supported positive
form still requires separate native Word accept/reject oracle copies plus
save/close/reopen and a fresh-reference decision after a Word save. Synthetic
XML tests and this document do not claim that native gate, real-corpus acceptance,
publication or complete action journaling has passed.
