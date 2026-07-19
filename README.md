<!-- SPDX-License-Identifier: Apache-2.0 -->

# Veqtor MCP

[Website](https://veqtor.pro) · [Demo](https://veqtor.pro/demo) ·
[Documentation](https://veqtor.pro/docs) ·
[PyPI](https://pypi.org/project/veqtor-mcp/) ·
[GitHub Releases](https://github.com/JohnDeer-ai/veqtor-mcp/releases)

> **Public technical Alpha:** a local MCP server for verifiable DOCX negotiation
> history and fail-closed tracked-change edits. Supported on macOS and Linux
> with Python 3.12-3.14.

Veqtor gives MCP-compatible AI clients deterministic tools to inspect a
mechanical accepted/current projection of contract wording, read redlines,
verify quotations, dry-run complete
counterproposal batches, create a new DOCX with real tracked changes, and keep
re-checkable local provenance. The model remains the legal-reasoning layer;
Veqtor handles document facts and writes.

Veqtor is not legal advice, a generic Word editor, a hosted service, or a
tamper-evident audit system. Review the
[known limitations](https://github.com/JohnDeer-ai/veqtor-mcp/blob/main/KNOWN_LIMITATIONS.md)
before using it on a real matter.

This source tree is the development-only package `0.3.0.dev0` and advertises
draft MCP contract `veqtor.mcp.v0.3`. No `0.3` package, extension or release is
offered by this statement, and the development version is intentionally absent
from the installation choices below.

Before installing, check both the generic
[PyPI project](https://pypi.org/project/veqtor-mcp/) and the
[GitHub Releases list](https://github.com/JohnDeer-ai/veqtor-mcp/releases), then
select one exact version for every command below:

| Official state | Replace `X.Y.Z` with |
|---|---|
| Both sources expose `0.2.0`, and the GitHub release contains the complete verified asset set | `0.2.0` |
| Otherwise | `0.1.2` |

Do not run the placeholder literally or mix versions within one installation.

## Claude Desktop Extension status

`veqtor-mcp-0.2.0-macos.mcpb` is an official public download only if it appears
inside the verified immutable `v0.2.0` entry in the live GitHub Releases list.
Its presence in source, CI, a branch, chat or issue is not publication. If that
release entry or its checksum manifest is absent, use the non-extension setup
below with the fallback selected above; never guess a release-asset URL.

Opening an official MCPB requests installation approval and the tracked-change
author name. Its first activation may download a compatible Python runtime and
locked dependencies. The artifact is checksum-bound by `SHA256SUMS.txt`, but
not digitally signed.

## Install a verified published version for Claude Code

Install [uv](https://docs.astral.sh/uv/) once, replace `X.Y.Z` using the table
above, then register that exact Veqtor version for your user account:

```bash
claude mcp add --transport stdio --scope user veqtor -- uvx veqtor-mcp@X.Y.Z
```

Open a new Claude Code session and verify the registration:

```bash
claude mcp get veqtor
```

`--scope user` makes the private local registration available to you in every
project on this machine. It does not publish the configuration or make Veqtor a
network service. To limit Veqtor to the current project instead, use:

```bash
claude mcp add --transport stdio --scope local veqtor -- uvx veqtor-mcp@X.Y.Z
```

For team-managed configuration, Claude Code also supports `--scope project`,
which writes `.mcp.json` for review and version control. Do not use project
scope for secrets or matter-specific paths.

To set the tracked-change author when registering the server:

```bash
claude mcp add --transport stdio --scope user veqtor -e VEQTOR_TRACKED_CHANGE_AUTHOR="Your Name" -- uvx veqtor-mcp@X.Y.Z
```

If unset, the author is `Veqtor MCP`. The value is fixed when the server starts
and cannot be changed by the model per edit.

## Run or install from the terminal

Run diagnostics without a persistent installation:

```bash
uvx veqtor-mcp@X.Y.Z doctor
```

For a persistent isolated installation:

```bash
uv tool install "veqtor-mcp==X.Y.Z"
"$(uv tool dir --bin)/veqtor-mcp" --version
"$(uv tool dir --bin)/veqtor-mcp" doctor
```

Using `uv tool dir --bin` works even before the uv tool directory has been
added to the shell `PATH`. `uv tool update-shell` can add it for later shells.
The versioned wheel and sdist for independent verification are also attached to
the matching entry in the generic
[GitHub Releases list](https://github.com/JohnDeer-ai/veqtor-mcp/releases).

## Five-minute demo

Create a disposable four-round synthetic negotiation:

```bash
uvx --from "veqtor-mcp==X.Y.Z" veqtor-demo-rounds ~/veqtor-demo-rounds
```

Then ask Claude:

> Using the veqtor tools, what happened to the limitation of liability across
> the rounds in ~/veqtor-demo-rounds?

For a write workflow, ask:

> Prepare our counterproposal as round-5-our-counter.docx: restore the 150%
> affected Work Order liability cap and reinstate the willful misconduct
> carve-out the counterparty deleted. Verify the wording and preflight the
> complete batch before applying it.

The expected trust sequence is:

1. extract the current redlines;
2. verify every quotation used as evidence;
3. preflight the complete atomic batch;
4. apply only when `batch_applicable` is true — version `0.1.2` reuses the exact
   edit payload, while contract `veqtor.mcp.v0.2` and the draft v0.3 contract
   also pass the complete `preflight_proof` returned by that successful
   preflight;
5. re-extract the output and export the decision record.

In MCP contracts `veqtor.mcp.v0.2` and draft `veqtor.mcp.v0.3`, the proof binds
the source bytes, canonical edit payload, configured author, producer build and
predicted candidate hash so apply can detect drift. It is an unkeyed content
binding, not authentication or a digital signature. Version `0.1.2` does not
emit or accept this field.

Veqtor never overwrites the source or an existing output file. Use a disposable
demo folder for write recordings; a successful write creates a new DOCX and a
private `.veqtor` provenance folder.

## Manual Claude Desktop setup

GUI applications may not inherit the shell `PATH`. First run `command -v uvx`,
then use that absolute path in the macOS Claude Desktop configuration at
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "veqtor": {
      "command": "/absolute/path/to/uvx",
      "args": ["veqtor-mcp@X.Y.Z"],
      "env": {
        "VEQTOR_TRACKED_CHANGE_AUTHOR": "Your Name"
      }
    }
  }
}
```

Quit Claude Desktop fully and reopen it after changing the executable, version,
or environment.

## Tool surface

The descriptions below follow development source `0.3.0.dev0`. They are not an
installation promise. For an installed version, use the API file carried by
that exact artifact or its matching immutable tag.

- `list_rounds`: disclosed lexicographic filename order or a complete explicit
  `ordered_filenames` positional manifest; neither is lineage proof.
- `extract_redlines`: tracked-change units with hashes, references, bounded
  paragraph context, conservative manual labels and a revision-inventory
  partition.
- `inspect_document`: bounded outline, literal-search, browse and read views of
  a mechanical accepted/current projection of main-body text, with every
  returned paragraph bound to the exact file bytes. This reading mode does not
  decide which wording has legal effect.
- `verify_quote`: anchored `exact`, `normalized`, or `not_found` verification.
- `preflight_edits`: the complete apply pipeline as an in-memory dry-run, with
  closed position/failure diagnostics and a successful drift-binding proof.
- `apply_edits`: atomic tracked replace, delete, counter and reinstate writes;
  MCP contracts `veqtor.mcp.v0.2` and draft `veqtor.mcp.v0.3` require the
  complete successful preflight proof; version `0.1.2` reuses the exact edit
  payload without that new field.
- `export_decision_record`: compact privacy-aware local provenance.

The complete request, response and error contract is in the versioned
[MCP Tool API](https://github.com/JohnDeer-ai/veqtor-mcp/blob/main/API.md). The
public `0.1.2` API remains available under its immutable release tag.

## Privacy and assurance boundary

The server has no embedded model calls and does not upload whole documents in
the background. Text returned by a tool enters the MCP client conversation and
may be sent to the user's model provider under that provider's terms.

Unless `VEQTOR_DISABLE_DECISION_RECORD=1`, tools append a private local journal
at `<matter>/.veqtor/decision-records.jsonl`. Read-only calls, including
`preflight_edits` and decision-record export, still normally write provenance
there. The path- and search-phrase-omission guarantee applies only to the
compact `export_decision_record` response. The raw journal retains the
workspace and caller-supplied paths, and may retain search phrases and other
verbatim matter text; do not commit or share it.

Decision-record export requires the exact initialized workspace. In the v0.2
contract a wrong parent is refused without creating a second
sidecar; one direct child journal yields a path-safe relative suggestion and
multiple child journals are reported as ambiguous.

Decision records are best-effort local provenance. Their hashes are
re-checkable fingerprints, not authentication. The supported threat model is a
single-user local workspace. Veqtor bounds and verifies the actual output of
every accepted DOCX/ZIP member (bounded streaming for DEFLATED data and a
bounded direct span for STORED data), but this does not turn it into a sandbox
for hostile same-user processes.

## Supported surface

| Surface | Public Alpha |
|---|---|
| Operating systems | macOS, Linux |
| Python | 3.12, 3.13, 3.14 |
| Transport | local stdio MCP |
| Validated clients | Claude Code, Claude Desktop |
| DOCX part | `word/document.xml` |
| Writes | tracked replace, delete, counter, reinstate |
| Distribution | Versioned PyPI packages with matching immutable GitHub wheel/sdist/checksums; macOS MCPB only when attached to the matching verified release |

Windows, hosted MCP, comments/headers/footnotes, accept/reject, semantic
cross-round lineage and cryptographic audit guarantees are outside the Alpha.

## Support

Veqtor MCP is a community-supported public Alpha. Use
[GitHub Issues](https://github.com/JohnDeer-ai/veqtor-mcp/issues) for
reproducible bugs and feature requests, using synthetic documents only. Never
post client wording, local paths, `.veqtor` journals, credentials, or real
matter files. Report suspected vulnerabilities privately as described in
[security policy](https://github.com/JohnDeer-ai/veqtor-mcp/security/policy).

No response-time, compatibility, uptime, or fix-time SLA is provided.

## Development and releases

```bash
uv lock --check
uv sync --frozen --all-extras
uv run --frozen pytest -q
uvx ruff==0.15.21 check .
LOCKED_REQUIREMENTS="$(mktemp)"
uv export --frozen --no-dev --no-emit-project \
  --format requirements-txt --output-file "$LOCKED_REQUIREMENTS"
uvx pip-audit==2.10.1 --requirement "$LOCKED_REQUIREMENTS" \
  --require-hashes --disable-pip --progress-spinner off
uv build --clear
uv run --frozen python scripts/build_mcpb.py \
  --source-root . --out-dir dist --stage-dir /tmp/veqtor-mcpb-stage
uvx twine check dist/*.whl dist/*.tar.gz
uv run --frozen python scripts/check_release_artifacts.py \
  --source-root . --commit HEAD dist/*.whl dist/*.tar.gz
uv run --frozen python scripts/check_mcpb_artifact.py \
  --source-root . --commit HEAD dist/*.mcpb
npx --yes @anthropic-ai/mcpb@2.1.2 validate \
  /tmp/veqtor-mcpb-stage/manifest.json
```

Contributions use DCO sign-off; see the
[contributing guide](https://github.com/JohnDeer-ai/veqtor-mcp/blob/main/CONTRIBUTING.md).
The exact-SHA immutable publication contract is documented in the
[release guide](https://github.com/JohnDeer-ai/veqtor-mcp/blob/main/RELEASING.md).

## Maintainer

Veqtor MCP is an independent open-source project created and maintained by
**Ilya Shilov** ([@JohnDeer-ai](https://github.com/JohnDeer-ai)).
