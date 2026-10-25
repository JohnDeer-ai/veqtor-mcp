<!-- SPDX-License-Identifier: Apache-2.0 -->

# Negotiation document roles

This is the first slice of MVP stage 1, available in development source only.
It links exact incoming and explicitly identified sent bytes across sessions.
It does not complete the round manager or public release acceptance.

`read_deal_state` and `mutate_deal_state` use a separate closed
`.veqtor/deal-rounds.json` store under the existing position-store lock. The
original positions format and history remain unchanged. Each new round captures
their matter ID, revision, history sequence and position versions, allowing the
caller to recover conditional concessions from that original history. A changed
current revision is shown separately. Each round reports whether its exact basis
is current, historically available, unavailable, or was never recorded. The reference is not a new confirmation
of those positions. No atomic transaction with document edits is claimed.

`start_round` captures an exact incoming snapshot with the existing snapshot
engine. Without a predecessor it creates a first round with no comparison base.
With an explicitly selected predecessor it fixes that round's latest recorded
sent snapshot as its basis. Later send records cannot silently change an existing
round's basis. Choosing a different basis requires an explicit new round.

`record_sent` records only bytes the user explicitly identifies as actually sent.
The assertion and exact hash are retained, without claiming transport evidence.
Preparing A and then identifying manually changed B as sent makes B the next
round's basis. This tool sends no email and never infers a role from a filename.

Exact snapshots live at `.veqtor/round-documents/<sha256>/`, using the existing
two-file `round_snapshot.v1` format. State reads validate all linked snapshots;
they never fall back to a changed original or silently select another file.
The returned paths and hashes feed the existing incoming comparison tool.
Do not edit these snapshot files. Make a separate working copy using the
existing checked drafting copy save. Managed working-copy links, issues,
operation links, outgoing packages and finalization remain later work.

Retain each request UUID before mutation. The same ID and command return the
committed state without a second round or send record, even after the source
disappears. Different content with the same ID refuses. A new command must match
the current state revision. An uncertain commit requires a fresh state read;
retry only the identical request. Snapshot publication precedes role publication.
A failed role commit can leave a verified, unlinked snapshot; the identical retry
can reuse it. Such a snapshot is never itself evidence of an applied action.
Unknown or corrupt data is retained and refused, never cleaned up automatically.

Limits: 50 rounds, 150 requests/documents, 100 send records per round, 2 MiB state,
and 100 MiB aggregate linked DOCX bytes, in addition to existing per-file ZIP/XML
and supported local-filesystem limits. Aggregate accounting and result-contract
validation occur before a new snapshot is published. Predictable over-limit
refusals do not retain additional snapshots. Crash-orphan reuse remains separate.
Storage is private local data, not
encryption or a forgery-resistant audit trail. No timestamp or legal agreement is
inferred. Snapshot validation is byte integrity, not clean-export eligibility.

Validation uses independently invented demo documents: first round, prepared A
versus actually sent B, immutable next-round basis, a fresh Python process,
conditional-position history, lost replies, stale revisions, corrupt snapshots,
unsafe links/permissions and pre/post-commit failures. This does not establish
Word interoperability, installed-client acceptance or real A/B corpus acceptance.

## Agent continuation in development

1. Read saved state and original positions/history in the selected matter folder.
   Report an unavailable historical basis; never substitute the current position.
2. For a first round use no predecessor and disclose the absence of comparison.
   For the next round explicitly select its predecessor and register the incoming
   bytes. Use the returned frozen sent basis and incoming snapshot with the
   existing comparison tool, consuming its entire supported paginated report.
3. Read the source, comments and revisions. Create a separate working copy for
   the user's concrete authorized plan through the existing checked drafting
   save. Preserve facts, model proposals and legal decisions separately. Reread
   after each overlapping revision decision before addressing the next edit.
4. Recover an uncertain document operation with its existing journal ID. Report
   unresolved actions and unsupported counter-revision decisions; do not alter
   authorship, delete unknown XML or declare a blocked export completed.
5. After the outgoing-package step is implemented and verified, prepare that
   package. Until then explicitly report that the integrated outgoing result is
   unfinished; an individual edited file is not evidence of the whole package.
6. Record actual sent bytes only when the user identifies the exact file sent,
   including any subsequent manual Word save. Continue the next round from this
   snapshot. Legal finalization still requires the separate F10 workflow.

This bridge updates the developing scenario incrementally. The older
`nr03-next-round.v3` selected-issue recipe and its frozen acceptance evidence are
not relabelled as acceptance of the complete MVP round.
