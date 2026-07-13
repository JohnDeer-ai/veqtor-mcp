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

Use GitHub's private vulnerability reporting / Security Advisory flow for
`JohnDeer-ai/veqtor-mcp`. Include the affected version and build identity, a
minimal synthetic reproduction, impact, and whether the issue crosses the
document, filesystem, MCP-context or compact-export privacy boundary.

## Security boundary

Veqtor is a local stdio MCP server for a non-hostile single-user macOS/Linux
workspace. It validates document anchors, source hashes, output publication and
private sidecar targets, but it is not a sandbox against another malicious
process running as the same operating-system user.

Tool results enter the MCP client conversation and may be sent to the selected
model provider. The raw `.veqtor/decision-records.jsonl` journal may contain
verbatim matter text and must be handled as part of the private corpus.

Hashes in the product are re-checkable content fingerprints. They do not
authenticate authors, prevent local tampering, or provide trusted timestamps.
