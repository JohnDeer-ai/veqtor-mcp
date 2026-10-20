<!-- SPDX-License-Identifier: Apache-2.0 -->

# Negotiation round foundation

This development stage adds internal Python building blocks for exact document
snapshots, table addresses and operation observations. It does not add MCP tools,
accept or reject revisions, edit a contract, or replace the existing journal.
The installed client configuration and public release contract are unchanged.

The product scope and acceptance plan are in the
[release specification](NEGOTIATION_ROUND_RELEASE_SPEC_RU.md) and
[acceptance plan](NEGOTIATION_ROUND_ACCEPTANCE_RU.md).

## Exact document snapshots

`veqtor_docx.round_snapshots` exposes:

- `capture_document_snapshot(path, expected_sha256=None)` to capture one bounded,
  validated DOCX into a frozen `DocumentSnapshot`;
- `persist_snapshot(snapshot, destination)` to publish a new private snapshot
  directory without replacing an existing destination; and
- `load_snapshot(destination)` to validate and reload its exact bytes.

A snapshot contains the original bytes, SHA-256 and byte count. It does not
select a negotiation role, establish transmission, or infer chronology from
the file name. Capture checks source freshness; every later address binds to
the captured bytes rather than a mutable source path.

A persisted snapshot contains `source.docx` and a closed
`round_snapshot.v1` manifest with its hash and size. The original source path,
file name and commercial metadata are not copied into the manifest. The DOCX
itself still contains private matter data and must remain in private storage.
This is exact local capture, not encryption or tamper-proof storage.

The destination parent is explicitly selected and must be on a supported local
filesystem. A private staging directory is created in that parent. Publication
is create-only; a concurrent or pre-existing final destination is never
replaced. A late error must disclose known or uncertain publication rather
than claiming that no output exists. A caller must inspect the actual result
before retrying. Snapshot persistence does not alter the source document.

## Table structure and addresses

`veqtor_docx.table_map.build_table_map(payload)` reads exact DOCX bytes and returns
a bounded `table_map.v1` inventory. `resolve_table_ref(payload, ref)` resolves a
table, row or cell against those same bytes.

The inventory uses canonical body-flow paragraph references, including empty
paragraphs, and describes physical cells and their logical grid positions.
Horizontal and vertical merges, omitted grid columns, nested tables and
multiple paragraphs in a cell remain explicit facts. A nested table is not
mistaken for an additional language column of its parent.

References contain the document hash, part, object position, policy and
structural fingerprint. They are not XPath expressions and are not stable
identifiers across saves. An identical visible string in another cell does not
make a reference reusable. Any changed DOCX requires fresh references, including
a Word save that only changes package metadata.

The map describes the current structure. It does not establish translation
equivalence, choose language columns, compare rounds, or prove that a target is
safe to edit. A simple rectangular geometry is not a mutation capability.
Tracked properties and excluded containers retain explicit coverage limits.
Invalid numeric geometry and exceeded resource limits cause a refusal.
Unsupported or ambiguous structure is disclosed through coverage and issues;
the caller cannot interpret incomplete coverage as a fully understood table.

## Minimal operation observations

`veqtor_docx.round_operations` supplies pure constructors and validators:

- `make_operation_intent` and `validate_operation_intent`;
- `operation_intent_digest`;
- `make_operation_result` and `validate_operation_result`.

An intent uses `round_operation_intent.v1` and contains an operation UUID,
producer identity, source and predicted candidate hashes, exact author profile,
destination, save mode, canonical plan and plan hash. Canonical JSON is bounded
to 1 MiB, 20,000 nodes and depth 32. Non-JSON values, cycles, non-finite numbers,
invalid Unicode and unsupported record fields are rejected.

The constructor canonicalizes JSON and binds the supplied plan; it does not
validate edit kinds, authorize the author or establish user consent. The actual
operation engine must still validate its own closed input schema and successful
preflight proof. These records are not executable proofs.

A result uses `round_operation_result.v1`, binds the entire intent and records:

| Field | Meaning |
| --- | --- |
| `publication_status` | `not_published`, `published` or `uncertain` |
| `observed_output_sha256` | Actual observed bytes, or null if not checked |
| `verification` | `matched`, `mismatch` or `not_checked` against the candidate |
| `backup` | Backup path and actually observed hash (or null), when available |
| `backup_verification` | `matched`, `mismatch`, `not_checked` or `not_required` against the source in the intent |
| `error_code` | Bounded machine code, including errors after publication |

An error-free published replacement requires a backup matching the original
source hash. A missing, unreadable or mismatching backup remains recordable
with an explicit error; publication is not concealed or converted to success.
An observed mismatch is recordable as an explicit error, so a bad result cannot
be disguised as success or suppressed merely because it violates expectations.
An uncertain save remains uncertain even if a candidate hash has been observed.
A known publication followed by an unreadable output is also represented
explicitly.

These functions do not save records, inspect files, issue timestamps, recover
an operation or decide whether retrying is safe. They validate consistency of
caller-supplied observations. They do not provide authentication, trustworthy
event history or an atomic transaction across DOCX, positions and journal.
Persistent prepared/result records and their recovery can be built separately
against this contract.

## Private corpus preparation

Public development uses only independently invented documents. Public source,
test fixtures, issues and PR descriptions must not contain document paths,
names, hashes, quotations or aggregate counts from a real deal.

Acceptance materials belong in a private evidence directory outside both the
repository and the deal's working folders. This restriction does not move the
normal Veqtor journal or position store used alongside a working document.

The following is a blank planning template, not a new persisted runtime schema:

```json
{
  "status": "draft_awaiting_user_pair",
  "case_id": "A",
  "sources": [],
  "selected_sent_source": null,
  "selected_incoming_source": null,
  "user_selected_pair": false,
  "frozen": false,
  "expected_outcomes": []
}
```

The private card may later contain the real sources, exact roles, hashes,
inventory, expected outcomes and local evidence. It must not be copied into
the repository. No real pair is selected or frozen by this implementation.
A received document may serve as an explicitly designated private engineering
sample; that does not establish it as a file actually sent to a counterparty
or make its contents suitable for public synthetic fixtures.

## Verification boundary

Synthetic tests cover unchanged and changed sources, invalid packages,
snapshot publication conflicts and failures, exact roundtrips, table geometry,
empty and nested containers, stale references, bounded records and contradictory
outcomes. Packaging must include the new modules in both wheel and sdist.

This stage requires no native Word mutation acceptance because it performs no
DOCX transformations. Passing its tests does not close future acceptance of
revision decisions, numbering, row operations, clean exports or actual
negotiation rounds. Those require the separately specified installed-client
and Word checks.
