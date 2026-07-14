<!-- SPDX-License-Identifier: Apache-2.0 -->

# Veqtor MCP

> **Public technical Alpha:** a local MCP server for verifiable DOCX negotiation
> history and fail-closed tracked-change edits. Supported on macOS and Linux
> with Python 3.12-3.14.

Veqtor gives MCP-compatible AI clients deterministic tools to read contract
redlines, verify quotations, dry-run complete counterproposal batches, create a
new DOCX with real tracked changes, and keep re-checkable local provenance. The
model remains the legal-reasoning layer; Veqtor handles document facts and
writes.

Veqtor is not legal advice, a generic Word editor, a hosted service, or a
tamper-evident audit system. Review the
[known limitations](https://github.com/JohnDeer-ai/veqtor-mcp/blob/v0.1.2/KNOWN_LIMITATIONS.md)
before using it on a real matter.

## Install for Claude Code

Install [uv](https://docs.astral.sh/uv/) once, then register the exact Veqtor
release for your user account:

```bash
claude mcp add --transport stdio --scope user veqtor -- uvx veqtor-mcp@0.1.2
```

Open a new Claude Code session and verify the registration:

```bash
claude mcp get veqtor
```

`--scope user` makes the private local registration available to you in every
project on this machine. It does not publish the configuration or make Veqtor a
network service. To limit Veqtor to the current project instead, use:

```bash
claude mcp add --transport stdio --scope local veqtor -- uvx veqtor-mcp@0.1.2
```

For team-managed configuration, Claude Code also supports `--scope project`,
which writes `.mcp.json` for review and version control. Do not use project
scope for secrets or matter-specific paths.

To set the tracked-change author when registering the server:

```bash
claude mcp add --transport stdio --scope user veqtor -e VEQTOR_TRACKED_CHANGE_AUTHOR="Your Name" -- uvx veqtor-mcp@0.1.2
```

If unset, the author is `Veqtor MCP`. The value is fixed when the server starts
and cannot be changed by the model per edit.

## Run or install from the terminal

Run diagnostics without a persistent installation:

```bash
uvx veqtor-mcp@0.1.2 doctor
```

For a persistent isolated installation:

```bash
uv tool install "veqtor-mcp==0.1.2"
"$(uv tool dir --bin)/veqtor-mcp" --version
"$(uv tool dir --bin)/veqtor-mcp" doctor
```

Using `uv tool dir --bin` works even before the uv tool directory has been
added to the shell `PATH`. `uv tool update-shell` can add it for later shells.
The versioned wheel and sdist for independent verification are also attached to
the matching [GitHub Release](https://github.com/JohnDeer-ai/veqtor-mcp/releases/tag/v0.1.2).

## Five-minute demo

Create a disposable four-round synthetic negotiation:

```bash
uvx --from "veqtor-mcp==0.1.2" veqtor-demo-rounds ~/veqtor-demo-rounds
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
4. apply only when `batch_applicable` is true;
5. re-extract the output and export the decision record.

Veqtor never overwrites the source or an existing output file. Use a disposable
demo folder for write recordings; a successful write creates a new DOCX and a
private `.veqtor` provenance folder.

## Claude Desktop

GUI applications may not inherit the shell `PATH`. First run `command -v uvx`,
then use that absolute path in the macOS Claude Desktop configuration at
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "veqtor": {
      "command": "/absolute/path/to/uvx",
      "args": ["veqtor-mcp@0.1.2"],
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

- `list_rounds`: deterministic filename-ordered local DOCX discovery.
- `extract_redlines`: tracked-change units with hashes, references, bounded
  paragraph context and conservative manual labels.
- `verify_quote`: anchored `exact`, `normalized`, or `not_found` verification.
- `preflight_edits`: the complete apply pipeline as an in-memory dry-run.
- `apply_edits`: atomic tracked replace, delete, counter and reinstate writes.
- `export_decision_record`: compact privacy-aware local provenance.

The complete request, response and error contract is in the
[MCP Tool API](https://github.com/JohnDeer-ai/veqtor-mcp/blob/v0.1.2/API.md).

## Privacy and assurance boundary

The server has no embedded model calls and does not upload whole documents in
the background. Text returned by a tool enters the MCP client conversation and
may be sent to the user's model provider under that provider's terms.

Unless `VEQTOR_DISABLE_DECISION_RECORD=1`, tools append a private local journal
at `<matter>/.veqtor/decision-records.jsonl`. Read-only calls, including
`preflight_edits` and decision-record export, still normally write provenance
there. The raw journal may contain verbatim matter text; do not commit or share
it.

Decision records are best-effort local provenance. Their hashes are
re-checkable fingerprints, not authentication. The supported threat model is a
single-user local workspace. Veqtor bounds and verifies the actual streamed
output of every accepted DOCX/ZIP member, but this does not turn it into a
sandbox for hostile same-user processes.

## Supported surface

| Surface | Public Alpha |
|---|---|
| Operating systems | macOS, Linux |
| Python | 3.12, 3.13, 3.14 |
| Transport | local stdio MCP |
| Validated clients | Claude Code, Claude Desktop |
| DOCX part | `word/document.xml` |
| Writes | tracked replace, delete, counter, reinstate |
| Distribution | PyPI plus matching GitHub wheel/sdist artifacts |

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
uvx twine check dist/*
uv run --frozen python scripts/check_release_artifacts.py \
  --source-root . --commit HEAD dist/*.whl dist/*.tar.gz
```

Contributions use DCO sign-off; see the
[contributing guide](https://github.com/JohnDeer-ai/veqtor-mcp/blob/v0.1.2/CONTRIBUTING.md).
The exact-SHA immutable publication contract is documented in the
[release guide](https://github.com/JohnDeer-ai/veqtor-mcp/blob/v0.1.2/RELEASING.md).

## Maintainer

Veqtor MCP is an independent open-source project created and maintained by
**Ilya Shilov** ([@JohnDeer-ai](https://github.com/JohnDeer-ai)).
