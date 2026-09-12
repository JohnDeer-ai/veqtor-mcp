<!-- SPDX-License-Identifier: Apache-2.0 -->

# NR-01 paragraph edit contract

An edit uses exactly one address: the existing `anchor` (unchanged legacy or
`change_unit_anchor.v2`), or `target: {kind: "paragraph", paragraph_ref: REF}`.
The latter is a closed object containing the complete, closed `paragraph_ref.v1`
returned by inspection. It supports nonempty exact `delete_text` and optional
`insert_text` (empty/absent means delete-only). Reinstate, insertion-only and
additional fields are refused. No change-unit identifier is invented.

The reference is resolved against the same immutable source bytes as extraction,
planning and surgery, using its file hash, part, canonical paragraph index,
paragraph text hash, reading mode and container policy. There is no text-based
fallback. Matching must find exactly one occurrence inside that paragraph.

Admissible targets are clean canonical body/table-cell paragraphs with supported
direct text runs and preserved paragraph/run properties. Every pending text,
format, move/range, paragraph-mark and structural revision applicable to the
paragraph or its enclosing table/row/cell properties is forbidden. Unsupported
containers, fields, comments, content controls, drawings and unprovable property
or inline structure are refused; a text-revision flag alone is insufficient.
Style/numbering dependencies must use their supported internal package locations;
missing, external or alternate targets are unprovable and refused. The paragraph
mark is retained even when all visible text is deleted.

Both address forms share the existing all-or-nothing planning, revision-ID,
surgery, serialization, exact revision multiset, collateral and create-if-absent
publication pipeline. Overlapping edits block the complete batch; existing
adjacency/grouping refusals also apply, including touching operations in a clean
paragraph. New paragraph
edits additionally prove the complete expected current paragraph and preservation
of its rejected/original text and formatting. New replacement text inherits the
first removed run's properties. Existing source files are never rewritten.

Stable refusals cover malformed targets (`invalid_edit`/`invalid_reference`),
stale files (`file_sha256_mismatch`), mismatched policy/text/position
(`reference_mismatch`/`reference_not_found`), pending revisions
(`paragraph_pending_revisions`), unsupported structure
(`paragraph_structure_unsupported`), zero/multiple matches
(`delete_text_not_found`/`delete_text_ambiguous`) and existing overlap/proof/
round-trip/publication errors. Public errors do not expose internal exceptions.

Preflight proof v1 continues to bind the entire ordered JSON edit payload,
including target kind and all reference fields, source, author, producer build
and candidate bytes. Successful paragraph diagnostics/applied entries carry the
full target; their source change-unit ID is absent (diagnostics may use null).
Private decision_record.v1 records add compatible target fields; historical
records and compact golden projections remain unchanged. Compact new entries
retain the full safe structural target and digest document text.

Development identity advances to 0.4.1.dev1 and the additive public schema to
veqtor.mcp.v0.4.1. Frozen v0.4 contracts, release identity and historical fixtures
remain intact. Native acceptance uses a separate NR-01 profile requiring ordered
full input/result reads, exact quote verification, full expected current text,
exact new/prior revisions, source/collateral evidence and export identity. The
old nine-tool native profile remains unchanged. Native/render/package final
gates follow independent review; Python checks do not assert those gates.
