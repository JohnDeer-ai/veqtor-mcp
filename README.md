<!-- SPDX-License-Identifier: Apache-2.0 -->

# Veqtor MCP

[Website](https://veqtor.pro) · [Demo](https://veqtor.pro/demo) ·
[Documentation](https://veqtor.pro/docs) ·
[PyPI](https://pypi.org/project/veqtor-mcp/)

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
[known limitations](https://github.com/JohnDeer-ai/veqtor-mcp/blob/v0.2.0/KNOWN_LIMITATIONS.md)
before using it on a real matter.

Version `0.2.0` advertises MCP contract `veqtor.mcp.v0.2`. Package version and
contract version are separate identities.

## Install for Claude Desktop on macOS

Download `veqtor-mcp-0.2.0-macos.mcpb` from the immutable
[v0.2.0 GitHub Release](https://github.com/JohnDeer-ai/veqtor-mcp/releases/tag/v0.2.0),
open it, approve installation, and enter the name that Word should show as the
author of new tracked changes. No manual JSON edit or separate uv installation
is required. The extension exposes the same six tools and includes four
synthetic demo documents plus the `try_veqtor_demo` prompt.

The first activation may download a compatible Python runtime and the
dependencies pinned in `uv.lock`. The release also includes `SHA256SUMS.txt`
for an optional advanced integrity check. The MCPB is checksum-bound but is not
digitally signed.

## Install for Claude Code

Install [uv](https://docs.astral.sh/uv/) once, then register the exact Veqtor
release for your user account:

```bash
claude mcp add --transport stdio --scope user veqtor -- uvx veqtor-mcp@0.2.0
```

Open a new Claude Code session and verify the registration:

```bash
claude mcp get veqtor
```

`--scope user` makes the private local registration available to you in every
project on this machine. It does not publish the configuration or make Veqtor a
network service. To limit Veqtor to the current project instead, use:

```bash
claude mcp add --transport stdio --scope local veqtor -- uvx veqtor-mcp@0.2.0
```

For team-managed configuration, Claude Code also supports `--scope project`,
which writes `.mcp.json` for review and version control. Do not use project
scope for secrets or matter-specific paths.

To set the tracked-change author when registering the server:

```bash
claude mcp add --transport stdio --scope user veqtor -e VEQTOR_TRACKED_CHANGE_AUTHOR="Your Name" -- uvx veqtor-mcp@0.2.0
```

If unset, the author is `Veqtor MCP`. The value is fixed when the server starts
and cannot be changed by the model per edit.

## Run or install from the terminal

Run diagnostics without a persistent installation:

```bash
uvx veqtor-mcp@0.2.0 doctor
```

For a persistent isolated installation:

```bash
uv tool install "veqtor-mcp==0.2.0"
"$(uv tool dir --bin)/veqtor-mcp" --version
"$(uv tool dir --bin)/veqtor-mcp" doctor
```

Using `uv tool dir --bin` works even before the uv tool directory has been
added to the shell `PATH`. `uv tool update-shell` can add it for later shells.
The versioned wheel and sdist for independent verification are also attached to
the matching [GitHub Release](https://github.com/JohnDeer-ai/veqtor-mcp/releases/tag/v0.2.0).

## Five-minute demo

Create a disposable four-round synthetic negotiation:

```bash
uvx --from "veqtor-mcp==0.2.0" veqtor-demo-rounds ~/veqtor-demo-rounds
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
4. apply only when `batch_applicable` is true, reusing the exact edit payload
   and passing the complete `preflight_proof` returned by that successful
   preflight;
5. re-extract the output and export the decision record.

The proof binds the source bytes, canonical edit
payload, configured author, producer build and predicted candidate hash so apply
can detect drift. It is an unkeyed content binding, not authentication or a
digital signature.

Veqtor never overwrites the source or an existing output file. Use a disposable
demo folder for write recordings; a successful write creates a new DOCX and a
private `.veqtor` provenance folder.

## Manual Claude Desktop fallback

GUI applications may not inherit the shell `PATH`. First run `command -v uvx`,
then use that absolute path in the macOS Claude Desktop configuration at
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "veqtor": {
      "command": "/absolute/path/to/uvx",
      "args": ["veqtor-mcp@0.2.0"],
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

All six names are present in `0.2.0`; the immutable tagged API is authoritative
for the released artifact.

- `list_rounds`: disclosed lexicographic filename order or a complete explicit
  `ordered_filenames` positional manifest; neither is lineage proof.
- `extract_redlines`: tracked-change units with hashes, references, bounded
  paragraph context, conservative manual labels and a revision-inventory
  partition.
- `verify_quote`: anchored `exact`, `normalized`, or `not_found` verification.
- `preflight_edits`: the complete apply pipeline as an in-memory dry-run, with
  closed position/failure diagnostics and a successful drift-binding proof.
- `apply_edits`: atomic tracked replace, delete, counter and reinstate writes;
  the MCP contract requires the complete successful preflight proof.
- `export_decision_record`: compact privacy-aware local provenance.

The complete request, response and error contract is in the versioned
[MCP Tool API](https://github.com/JohnDeer-ai/veqtor-mcp/blob/v0.2.0/API.md).

## Privacy and assurance boundary

The server has no embedded model calls and does not upload whole documents in
the background. Text returned by a tool enters the MCP client conversation and
may be sent to the user's model provider under that provider's terms.

Unless `VEQTOR_DISABLE_DECISION_RECORD=1`, tools append a private local journal
at `<matter>/.veqtor/decision-records.jsonl`. Read-only calls, including
`preflight_edits` and decision-record export, still normally write provenance
there. The raw journal may contain verbatim matter text; do not commit or share
it.

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
| Distribution | Public PyPI plus matching GitHub wheel/sdist, macOS MCPB and checksum manifest |

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
[contributing guide](https://github.com/JohnDeer-ai/veqtor-mcp/blob/v0.2.0/CONTRIBUTING.md).
The exact-SHA immutable publication contract is documented in the
[release guide](https://github.com/JohnDeer-ai/veqtor-mcp/blob/v0.2.0/RELEASING.md).

## Maintainer

Veqtor MCP is an independent open-source project created and maintained by
**Ilya Shilov** ([@JohnDeer-ai](https://github.com/JohnDeer-ai)).
