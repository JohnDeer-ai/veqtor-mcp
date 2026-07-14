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
before decoder creation. Every accepted member is then streamed through a
bounded `STORED`/`DEFLATED` reader that counts actual output and verifies CRC,
descriptor boundaries and the real DEFLATE end-of-stream; no package member is
trusted solely because its central-directory sizes look safe. Parsed XML node
counts are checked before full tree construction, and edit-batch limits before
document mutation or output publication. These controls do not make Veqtor a
sandbox against another malicious process running as the same operating-system
user or prove that every parser dependency is vulnerability-free.

Tool results enter the MCP client conversation and may be sent to the selected
model provider. The raw `.veqtor/decision-records.jsonl` journal may contain
verbatim matter text and must be handled as part of the private corpus.

Hashes in the product are re-checkable content fingerprints. They do not
authenticate authors, prevent local tampering, or provide trusted timestamps.
