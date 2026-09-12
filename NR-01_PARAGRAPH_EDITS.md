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
Each preparation validates the whole source ZIP once and reuses its retained
package for extraction, inspection, admissibility and surgery. It validates the
serialized candidate once and reuses that complete package for every candidate
check. Packages are never cached across independent preflight/apply calls.

Admissible targets are clean canonical body/table-cell paragraphs with supported
direct text runs and preserved paragraph/run properties. Every pending text,
format, move/range, paragraph-mark and structural revision applicable to the
paragraph or its enclosing table/row/cell properties is forbidden. Unsupported
containers, fields, comments, content controls, drawings and unprovable property
or inline structure are refused; a text-revision flag alone is insufficient.
Style/numbering dependencies must use their supported internal package locations;
missing, external or alternate targets are unprovable and refused. Present parts
need their relationships, and referenced style inheritance/link and numbering
instance/abstract chains must resolve, including defaults. The tolerant reader
cannot supply write authorization for a missing definition. Style definition
elements retain their own vocabulary. A `numStyleLink` must resolve through a
numbering style's own or inherited `numId` to a terminal abstract definition;
missing effective targets and redirection cycles refuse. A terminal `styleLink`
association may be reciprocal and does not create another redirection. The paragraph
mark is retained even when all visible text is deleted.

Both address forms share the existing all-or-nothing planning, revision-ID,
surgery, serialization, exact revision multiset, collateral and create-if-absent
publication pipeline. Overlapping edits block the complete batch; existing
adjacency/grouping refusals also apply, including touching operations in a clean
paragraph. New paragraph
edits additionally prove the complete expected current paragraph and preservation
of its rejected/original text and formatting. New replacement text inherits the
first removed run's properties. Every serialized touched paragraph must match
the complete planned XML structure; zero-text nodes cannot escape comparison.
Paragraph tails belong to the enclosing body/cell, remain in collateral masking,
and must equal their source values. Non-whitespace tails on paragraph targets
are unsupported. The post-surgery planned snapshot cannot authorize tail changes.
Existing source files are never rewritten.

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
exact new/prior revisions, source/collateral evidence and export identity. Native
read paths/parts and quote-match file/part/revision/side identities must bind to
the resolved source or output. The checker independently rejects unaccounted
structure in touched paragraph targets and checks the complete compact record
projection against native input/result/proof/producer/workspace/provenance,
including collection counts, digests, samples and truncation. The
old nine-tool native profile remains unchanged. Native/render/package final
gates follow independent review; Python checks do not assert those gates.
