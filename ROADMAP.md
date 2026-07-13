<!-- SPDX-License-Identifier: Apache-2.0 -->

# Roadmap

## Thesis

Local open-source contract toolchain for LLMs: read, verify, apply, record.

The LLM decides what to say or propose. The toolchain reads, verifies, applies,
and records document facts.

## Milestones

### M1 - Read

Demo: ask Claude for negotiation history on a topic from a folder of DOCX rounds
and get a timeline with verifiable references.

Includes:

- Initial MCP tool API.
- One-line install from git.
- Synthetic English-language fixtures, including mixed numbering, long clauses,
  comments, tracked changes, and table-like structures.
- One private real-matter dogfood run.

Fallback: trace v1 may return per-round extracted facts with deterministic
anchors; Claude stitches the story. Full semantic round matching is parking lot.

### M2 - Write

Demo: Claude proposes counter wording and creates a new DOCX with real tracked
changes.

M2 gate:

- Apply only at an anchor produced by the read path.
- Ambiguous or missing anchor returns an explicit error and writes nothing.
- Round-trip check: `extract_redlines(new.docx)` returns exactly the proposed
  edits and no semantic/XML-structural changes outside the touched anchor
  ranges. Byte-identical DOCX packages are not required because Word may
  rewrite package metadata.

### M3 - Trust

Demo: deterministic `verify_quote`, slim decision record, and provenance for
read/write actions.

### M4 - Skill / Public Polish

Demo: a versioned public Alpha that an external user can install, diagnose and
run through negotiation history, full-pipeline preflight, counterproposal and
compact provenance without a development checkout.

v0.1 includes versioned GitHub wheel/sdist artifacts, Alpha metadata, installed-
wheel MCP smoke, bounded paragraph context, configured tracked-change author,
release documentation and a fresh-user Quickstart. PyPI publication remains a
later distribution decision; the versioned GitHub release is canonical for
v0.1.

## Promotion Gates

Each milestone must work with:

- Synthetic public fixtures.
- One private real matter locally.
- Claude Code/Desktop through MCP.
- One-line installation.
- A documented demo command.

## Kill Criteria

- More than two weeks without a working Claude demo.
- A feature cannot be explained as read, verify, apply, or record.
- Generic DOCX editing becomes the product surface instead of negotiation
  history and verifiable decision records.
- M1 requires full semantic matching.
- UI, auth, or hosted infrastructure appears before M4.
- `ROADMAP.md` plus `API.md` exceed 5-6 pages.
- A third process/planning document appears.

## Parking Lot

- Semantic cross-round matching.
- Built-in LLM calls or MCP sampling.
- Hosted service.
- Custom chat UI.
- PyPI Trusted Publishing after the GitHub Alpha channel is proven.
