<!-- SPDX-License-Identifier: Apache-2.0 -->

# MCP Tool API

This file defines the public tool surface. Output examples are part of the API
because models use them to decide how to call tools and how to cite results.

## `list_rounds`

Call this when the user points to a folder of contract drafts or asks which
rounds/files are available in a negotiation.

Input:

```json
{
  "folder": "/Users/example/Deals/AcmeDistribution"
}
```

Output:

```json
{
  "rounds": [
    {
      "round_id": "round-001",
      "path": "/Users/example/Deals/AcmeDistribution/01-initial.docx",
      "filename": "01-initial.docx",
      "sha256": "example",
      "revision_count": 12
    }
  ]
}
```

## `extract_redlines`

Call this when the user asks what changed in a DOCX, asks for tracked changes,
or needs anchors before applying a later edit.

Input:

```json
{
  "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx"
}
```

Output:

```json
{
  "change_units": [
    {
      "change_unit_id": "cu_001",
      "file_sha256": "example",
      "change_type": "replace",
      "author": "J. Smith",
      "date": "2026-07-01T09:00:00Z",
      "clause_anchor": {
        "label": "Section 14.2",
        "heading": "Limitation of Liability"
      },
      "old_text": "fees paid in the previous 12 months",
      "new_text": "USD 50,000",
      "reference": {
        "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx",
        "part_name": "word/document.xml",
        "revision_ids": ["17", "18"]
      }
    }
  ]
}
```

## `trace_clause`

Call this when the user asks about negotiation history, clause evolution, or how
a topic changed across drafting rounds.

Input:

```json
{
  "folder": "/Users/example/Deals/AcmeDistribution",
  "topic": "limitation of liability"
}
```

Output:

```json
{
  "topic": "limitation of liability",
  "trace": [
    {
      "round_id": "round-002",
      "change_unit_id": "cu_017",
      "change_type": "replace",
      "author": "J. Smith",
      "date": "2026-07-01T09:00:00Z",
      "old_text": "fees paid in the previous 12 months",
      "new_text": "USD 50,000",
      "clause_anchor": {
        "label": "Section 14.2",
        "heading": "Limitation of Liability"
      },
      "reference": {
        "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx",
        "part_name": "word/document.xml",
        "revision_ids": ["17", "18"]
      }
    }
  ],
  "limitations": [
    "Trace v1 returns per-round extracted facts; the host model stitches the narrative."
  ]
}
```

## `verify_quote`

Call this before relying on a quotation in a memo, email, or negotiation summary.
Use anchors returned by `extract_redlines` or `trace_clause` whenever available.
`verdict` is one of `exact`, `normalized`, or `not_found`; `diff` explains any
non-exact result.

Input:

```json
{
  "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx",
  "anchor": {
    "change_unit_id": "cu_017",
    "file_sha256": "example",
    "part_name": "word/document.xml",
    "revision_ids": ["17", "18"]
  },
  "quote": "USD 50,000"
}
```

Output:

```json
{
  "verdict": "exact",
  "exact": true,
  "checked_anchor": {
    "change_unit_id": "cu_017",
    "file_sha256": "example"
  },
  "matches": [
    {
      "path": "/Users/example/Deals/AcmeDistribution/02-counterparty.docx",
      "part_name": "word/document.xml",
      "revision_ids": ["17", "18"],
      "clause": "Section 14.2"
    }
  ],
  "diff": []
}
```

## `apply_edits`

Call this only after the user asks to prepare or apply counter wording and only
with an anchor produced by `extract_redlines` or `trace_clause`.

The server applies explicit edits only. Each edit must state the text range to
delete and the replacement text to insert. If the anchor is missing, ambiguous,
bound to a different file hash, or resolved to text that does not exactly match
`delete_text`, the tool returns an error and writes nothing.

`edits` are atomic: if any edit fails validation or application, no edits are
written and no output DOCX is left behind. If the round-trip check fails after
application, the tool returns an error and removes the failed output artifact.
The round-trip check compares OOXML structure outside touched anchor ranges; it
does not require byte-identical DOCX packages.

Input:

```json
{
  "source_path": "/Users/example/Deals/AcmeDistribution/12-current.docx",
  "output_path": "/Users/example/Deals/AcmeDistribution/13-our-counter.docx",
  "edits": [
    {
      "anchor": {
        "change_unit_id": "cu_017",
        "file_sha256": "example"
      },
      "delete_text": "USD 50,000",
      "insert_text": "The aggregate liability cap will equal the fees paid in the previous 12 months, excluding willful misconduct."
    }
  ]
}
```

Output:

```json
{
  "status": "ok",
  "output_path": "/Users/example/Deals/AcmeDistribution/13-our-counter.docx",
  "applied": [
    {
      "change_unit_id": "cu_017",
      "deleted_text": "USD 50,000",
      "inserted_text": "The aggregate liability cap will equal the fees paid in the previous 12 months, excluding willful misconduct.",
      "tracked_revision_ids": ["31", "32"]
    }
  ],
  "round_trip_check": {
    "status": "passed",
    "collateral_changes": [],
    "comparison": "ooxml_semantic_diff_outside_touched_anchors"
  }
}
```

## `export_decision_record`

Call this when the user asks for an audit trail, negotiation record, or summary
of decisions/actions taken by the toolchain.

Input:

```json
{
  "workspace": "/Users/example/Deals/AcmeDistribution"
}
```

Output:

```json
{
  "records": [
    {
      "record_id": "dr_001",
      "action": "apply_counterproposal",
      "source_anchor": "cu_017",
      "output_path": "/Users/example/Deals/AcmeDistribution/13-our-counter.docx"
    }
  ]
}
```
