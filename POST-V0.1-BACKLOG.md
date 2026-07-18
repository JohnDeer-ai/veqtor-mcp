<!-- SPDX-License-Identifier: Apache-2.0 -->

# Post-v0.1 shadow backlog

This is the working product backlog behind the public roadmap. It records
reproducible follow-ups and open product decisions without presenting them as
public commitments. `ROADMAP.md` remains the source for public direction.

## P0 — local provenance liveness

- Replace indefinite blocking `flock` acquisition with one bounded,
  monotonic-deadline helper for journal initialization, append, read and
  export.
- Return a stable `journal_busy` outcome. Ordinary document tools should keep
  the completed core result and report `record_status: write_failed`; export
  should remain fail-closed because it cannot produce a trustworthy snapshot.
- Add process-level contention tests covering every lock path, recovery after
  unlock, no duplicate records and the case where an apply output already
  exists before provenance publication finishes.
- Add an aggregate journal-size policy and rotation/archive design. The current
  append-only journal has no size cap.

## P1 — provenance projection clarity

- Add the same bounded `producer` identity block to live `list_rounds`,
  `extract_redlines` and `verify_quote` responses that preflight/apply already
  return. A read result copied out of its later journal export should still be
  attributable to the package version and Python-source snapshot that produced
  it.
- Give bounded samples an explicit policy/reason field, for example
  `sample_policy: observed_bounded | asserted_input_omitted`. Today an empty
  `sample` with `truncated: true` is overloaded: for successful preflight and
  apply it means intentional omission of client-asserted anchors, not that an
  observed sample was lost to a size limit.
- Consider a location-independent operation-result fingerprint in addition to
  the existing `result_sha256` and `tool_result_sha256`. The current full-result
  fingerprint can include the caller's path spelling, so it is useful for
  replay/debugging but is not a pure document-content identity.
- Define a migration story for path-normalization changes in legacy local
  journals. New records should use one canonical workspace identity, while old
  relative-path records remain historical data rather than being rewritten.

## P1 — Desktop activation diagnostics

- Add a small health/diagnostic surface that distinguishes extension disabled,
  MCP server not connected, request never dispatched, server busy and server
  failure. A Claude Desktop transport timeout must not be presented as a
  Veqtor document-processing fact.
- Keep a bounded read-only single-file diagnostic for separating a genuinely
  slow folder scan from client transport failure, without weakening fail-closed
  write behavior.

## P2 — tracked-change metadata

- Explore an optional caller-supplied revision timestamp that is validated,
  proof-bound and deterministic between preflight and apply. Do not substitute
  the server wall clock silently. Until then, new `w:ins`/`w:del` revisions
  intentionally omit `w:date`, while the decision record remains the action
  clock.
- Preserve source authors exactly, including unusual numeric Word author
  strings. If a friendlier public demo is desired, keep the adversarial numeric
  case in a focused fixture instead of weakening metadata-preservation tests.

## Stage 3 product value

- Add hash-bound paragraph/clause anchors for unchanged operative text, so a
  user can verify and cite important language even when it is not itself a
  tracked change.
- Build a bounded round map whose links are explicitly classified as verified
  evidence or navigation-only inference. Filename order must never become
  implicit chronology or lineage proof.
- Validate the primary user job before choosing the next write mode:
  negotiation-history preservation and a clean sendable redline need separate
  semantics. A sendable deliverable requires explicit accept/reject/normalize
  rules and must not silently collapse the evidence-preserving ledger.
