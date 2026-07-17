<!-- SPDX-License-Identifier: Apache-2.0 -->

# Security policy

## Supported version

Security fixes are provided for the latest tagged Alpha release only. The
unreleased `main` branch and older pre-release builds are not supported release
channels.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or include client
documents, contract wording, local paths, journal contents, credentials or
other private matter data in a report.

Use [GitHub private vulnerability reporting](https://github.com/JohnDeer-ai/veqtor-mcp/security/advisories/new)
for `JohnDeer-ai/veqtor-mcp`. Include the affected version and build identity,
a minimal synthetic reproduction, impact, and whether the issue crosses the
document, resource, filesystem, MCP-context or compact-export privacy boundary.

Private vulnerability reports are the only supported confidential reporting
channel for this community-supported Alpha. Ordinary bugs and feature requests
belong in GitHub Issues and must use synthetic data. No response-time or
fix-time SLA is provided.

## Security boundary

Veqtor is a local stdio MCP server for a single-user macOS/Linux workspace. It
validates document anchors, source hashes, output publication and private
sidecar targets. ZIP metadata, methods, encryption flags and layout are checked
before decoder creation. Every accepted member then has its actual output and
CRC checked: DEFLATED data is streamed through bounded input/output chunks and
must reach its real end of stream, while STORED data is a bounded direct span.
Descriptor boundaries are verified, and no package member is trusted solely
because its central-directory sizes look safe. Parsed XML node
counts are checked before full tree construction. A `list_rounds` folder scan
also has one cumulative 500 MiB actual expanded-output budget: DEFLATED decoder
output and STORED direct-span bytes remain charged when a package is rejected
by a later CRC, XML or required-part check. A container-preflight refusal before
any member-output processing consumes zero; attempting member-output processing
beyond the budget fails the complete call without returning partial rounds.
Edit-batch limits are checked before document mutation or output publication.
These controls do not make Veqtor a sandbox against another malicious process
running as the same operating-system user or prove that every parser dependency
is vulnerability-free.

Tool results enter the MCP client conversation and may be sent to the selected
model provider. The raw `.veqtor/decision-records.jsonl` journal may contain
verbatim matter text and must be handled as part of the private corpus.

Hashes in the product are re-checkable content fingerprints. They do not
authenticate authors, prevent local tampering, or provide trusted timestamps.

A Claude Desktop Extension is official only when its exact versioned `.mcpb`
is attached to the matching immutable GitHub Release and covered by that
release's `SHA256SUMS.txt`. The checksum detects accidental or malicious byte
changes after the approved build; it is not a digital signature, code-signing
certificate or proof of authorship. Do not install MCPB files from issues,
forks, chat attachments or unverified mirrors.
