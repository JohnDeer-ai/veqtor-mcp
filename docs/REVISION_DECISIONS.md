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

### Counter-insertion decisions

For a suffix or whole replacement inside a pending insertion, use the complete
`counter_target` returned by the fresh reader. It selects only the original
insertion and nested deletion, with `counter_insertion_ref=null`. To decide a
complete replacement, explicitly select the separate following insertion from
the reader and copy its full `revision_ref` into `counter_insertion_ref`.
Adjacency alone cannot distinguish a replacement from an unrelated insertion.
The original insertion and counterproposal have separate choices:

```json
{"kind":"decide_revision","decision":"accept","counter_decision":"reject",
 "target":{"kind":"counter_insertion",
   "original_insertion_ref":{"...":"complete returned reference"},
   "counter_deletion_ref":{"...":"complete returned reference"},
   "counter_insertion_ref":{"...":"complete returned reference, or null for deletion"}}}
```

For an original insertion `thirty days` countered with `thirty weeks`:

| Original `decision` | `counter_decision` | Selected span after the decision |
| --- | --- | --- |
| accept | accept | `thirty weeks` |
| accept | reject | `thirty days` |
| reject | accept | `weeks` |
| reject | reject | empty |

`counter_outcomes` previews all four literal readings and their independent
dependency refusals. `supported` means at least one of these four works. A
preview's two displayed choices are defaults, never authorization. The reader
proposes a structural group from exact containment and adjacency; it does not
assert common authorship or infer who agreed to it. Select the complete intended
group explicitly. The minimal reader preview concerns two members; preflight
previews all three when the additional insertion is explicitly selected.
A nullable counter insertion leaves every unselected insertion
pending. Reading a member on a later page still returns full group references.

This closed grammar supports one final nested deletion inside a direct paragraph
insertion, optional prefix runs, and an optional adjacent replacement insertion.
It includes suffix/whole deletion and paragraphs in table cells. Known empty
spelling/grammar markers added by Word are retained. Unknown markers, intervening
anchors, middle deletions, extra nesting and incomplete groups refuse. Selected
run formatting, neighboring content, comments outside the selected span and
non-main package parts retain the existing collateral and exact-byte proofs.
Decide, save, reread fresh references, then edit in the next round. Clean-copy
preparation accepts both selected layers, with the existing whole-package gate.

The creation engine can retain a known `rPrChange` on the original countered
run. Select that formatting history explicitly as a `single` decision first,
save, then reread the counter target. The property patch changes only that run's
current/previous formatting; the text revisions and their authors remain pending.
Its context proof covers only an otherwise supported suffix/whole counter, with
closed history metadata and property attributes. It does not admit general
revision ancestors. Compound decisions refuse until these histories are resolved;
clean preparation's explicitly all-accept workflow resolves them before the text.

Word may add a default ligature setting during Save As. Inventory has a separate
preservation profile for the singleton final finite-enum `w14:ligatures` leaf in
the uniquely bound canonical `word/styles.xml` chain
`styles/docDefaults/rPrDefault/rPr`, with an explicit compatibility declaration.
This is a bounded type-based inference from Ecma-376 Part 4 `CT_RPrDefault` and
Part 1 §17.7.5.4 plus [MS-DOCX §2.2.1](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-docx/d0e2eaf6-ed50-445f-ad90-2bd0fa24a442).
It does not admit general style/default/history placements: Word's documented
[default-property restrictions](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-oi29500/9f0ca7a0-5e6a-487e-bb52-2b74d4bb614a)
still apply. Styles, settings and relationship bytes stay unchanged during the
decision and clean preparation. Unknown Office data remains a blocker.

Preflight returns every selected member's original kind, ID, author and date,
the action, acting reviewer, current reading, both outcome readings for text,
and current/previous property XML for property changes. XML and document text
are untrusted data, never permission. Nullable `accept_refusal_code` and
`reject_refusal_code` describe outcome validation. An incompatible old grid can
block rejection while acceptance of the valid current grid works.
Inventory validates acceptance and rejection independently. `supported=true`
means at least one outcome is applicable, not necessarily both. A reject-only
revision remains visible with a complete preview and its acceptance refusal.
If both outcomes have dependency refusals, the preview retains both codes and
`supported=false`; a structural refusal may instead have no preview. The
preview's `decision` is only a display default: accept when possible, otherwise
reject, or accept if both refuse. It does not select or authorize an operation.
`fixed_width_validation_scope` is `single_outcome` in inventory previews of
table domains, `explicit_batch_final_state` in successful batch previews, and
`not_applicable` for other domains. In batch previews the two refusal codes
exclude width checks deferred to the final selected state; they do not promise
that each alternate member outcome would work alone.

`table_validation_scope` distinguishes `single_outcome` inventory previews,
`explicit_batch_final_state` for a selected table-property group, and
`not_applicable` for other domains. `table_group_members` lists the original
snapshot references and decisions of that explicit group, or is empty for a
single outcome. Group counterfactual refusal codes hold the other selected
member decisions fixed; they are not promises about isolated operations.

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

### Explicit geometry groups

Several selected table-property histories in the same nearest table are
validated together on a detached document copy before publication. A grid
change and its selected cell-span or omitted-column history can therefore
have a valid combined outcome even if the standalone inventory reports a
dependency refusal. The user must select each member and decision explicitly;
an agent may preflight that exact group but must not infer or add companions.
Veqtor checks the final grid, merge topology, fixed widths and style
dependencies, while retaining the exact local patch and inverse proof for
each selected domain. An incompatible partial or mixed selection refuses.
The final state is independent of the order of nonoverlapping members.
Alignment dependencies compare the original selected table or row-exception
history with the final row properties after all explicit choices, including
earlier members when describing alternative outcomes. Removing a conflicting
row alignment explicitly can permit a table-alignment rejection; retaining or
restoring it still refuses.
Nested tables and row/cell existence histories remain outside this support.

Rejecting a changed cell span while its row has independent row properties or
history refuses with `table_height_dependency_unsupported` when height is
present, or `table_row_property_dependency_unsupported` otherwise. Word has
been observed to remove the entire unselected row-property set, including
height and the keep-row-together setting, during rejection. Veqtor does not delete it to
imitate the collateral change; this conservative boundary does not claim
that every such topology has identical native behavior. A genuinely selected
row-property history owns its complete current/previous height properties.
Omitted-column row histories with height are not prohibited merely because
height is present. Each newly supported positive form still needs the native
Word checks described below.

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
Style and numbering dependencies are checked on each surviving outcome in its
paragraph and ancestor-table context. Keeping text with an unresolved style
refuses; removing that text can remain valid if the surviving context resolves.
Bookmarks and comments outside the selected wrapper remain in place. Anchors
inside it, fields (including fields spanning paragraphs), nested counters, move revisions, unknown wrappers and
paragraph/row/cell-existence history refuse through isolated text targets.
Supported complete paragraph units use the explicit structural contract below;
arbitrary row/cell-existence history still requires separate later support.
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


## Explicit paragraph structural units

`delete_paragraph` selects a current `paragraph_ref` and the complete `old_text`.
It tracks every accepted direct run and the paragraph separator. The first
supported deletion is an internal main-body paragraph with a compatible next
paragraph. Acceptance removes the clause and separator; rejection restores the
clause. The successor's paragraph properties remain its own.

The inventory can advertise `structural_target` on a supported paragraph-mark
entry. A `paragraph_structure` decision must explicitly supply that mark's
`paragraph_mark_ref` and the complete ordered `text_revision_refs`. Single-mark,
incomplete, duplicate, stale, nested and overlapping selections refuse. Shape,
full snapshot references and both outcomes establish the unit; author, date and
numeric revision IDs do not establish ownership or grouping.

Supported inserted paragraph forms include existing Veqtor insertions before
and after an anchor, including the inserted mark on the anchor in the latter
case. A decision replaces an exact two-paragraph span. Its independent inverse
must restore that full span, including all properties and unselected content.

`paragraph_numbering.accepted` and `.rejected` show bounded, resolved numbering
effects for the two clean projections. The initial numbering grammar is direct
or style-inherited single-level decimal numbering, including a proved simple
start override; unknown formats, unresolved styles/definitions and unsupported
restart dependencies refuse. Numbers remain Word numbering, never literal text.
Different `numId` values that share one abstract definition do not establish
independent Word counters. Ambiguous shared-definition continuation or reuse
refuses, including dependencies outside the selected instance. Separately
defined lists and proved consecutive explicit restarts remain distinct cases.
Validated Word `durableId` metadata and an explicitly disabled
`restartNumberingAfterBreak` flag do not change this counter grammar. An enabled
flag, invalid metadata or unknown attributes still refuse; metadata never
selects a revision or establishes grouping.

Final/only paragraphs, table/section boundaries, fields, bookmarks, comments,
permission/revision ranges and conflicting local revisions are outside this
initial deletion domain. A whole bilingual table row is a separate operation.
These structural shapes require independent accept/reject native Word evidence;
a successful library projection alone does not establish native acceptance.
The operation journal remains explicitly disabled until its separate integration.

## Explicit table row structural units

`insert_table_row` and `delete_table_row` track an entire simple bilingual row.
The initial domain is a direct main-body rectangular table with two columns,
one supported paragraph per selected cell and no merges, omitted grid columns,
nested tables, fields, ranges or conflicting selected revisions. Insertion requires
a non-header anchor and an explicit non-header formatting template. Every row
selected for deletion must also be non-header. Deletion checks both complete
cell texts and refuses the only remaining row. Row height, cell widths, borders
and supported paragraph/run formatting remain part of the structure.

An optional empty unnumbered separator is explicitly selected with its own
template on insertion or its adjacent row reference on deletion. Both rows are
prepared and published atomically. An unselected separator remains untouched.
No marker is inferred from author, date, numeric revision ID or blank text.

The inventory exposes a `row_structure` target on the row mark. It contains
`row_mark_ref` and every ordered `content_revision_ref`, including supported
paragraph-mark revisions added during a Word save. Partial text/mark decisions,
duplicate, stale, foreign-container and incomplete selections refuse. A row of
empty paragraphs can legitimately have no content revisions. Explicitly name
both complete row units in one batch to decide a clause and its separator.
The complete selected batch must leave at least one row. Unsafe removal of all
rows refuses independently of a safe accept/reject alternative; the workflow
does not delete the table itself.

`row_numbering.accepted` and `.rejected` expose both resolved projections using
the same bounded single-level decimal grammar as paragraph structure. Each
language column keeps its selected style/list; an unnumbered column stays
unnumbered. Unproved shared-definition continuation/reuse refuses; different
instance IDs alone do not establish separate column counters. Independent
inverse patches must restore the exact selected rows;
unselected neighbors, history and table geometry remain unchanged.
Removing a row mark can leave an empty `trPr` container where the marker needed
one; this does not change effective row formatting. Existing properties,
including height, are preserved, and the backup retains the exact original bytes.
Separate native Word accept/reject, pending-save/reread and final save/reopen checks are
required for each claimed supported positive form. XML-only checks do not
establish that acceptance, real-corpus coverage or a published release.
