<!-- SPDX-License-Identifier: Apache-2.0 -->

# Veqtor MCP

> **v0.1 Alpha:** a local, developer-oriented MCP toolchain for tracked-change
> negotiation workflows in Word DOCX files. Supported today on macOS and Linux
> with Python 3.12-3.14. Review the [known limitations](KNOWN_LIMITATIONS.md)
> before using it on a real matter.

Veqtor gives MCP-compatible AI clients deterministic tools to read contract
redlines, verify quotations, dry-run proposed counters, create a new DOCX with
real tracked changes, and record re-checkable local provenance. The model
remains the legal-reasoning layer; Veqtor handles document facts and writes.

Veqtor is not legal advice, a generic DOCX editor, a hosted service, or a
tamper-evident audit system.

## Install

Install the versioned GitHub release with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install "git+https://github.com/JohnDeer-ai/veqtor-mcp@v0.1.0"
veqtor-mcp --version
veqtor-mcp doctor
```

`doctor` reports the installed version/build, Python and platform support, the
configured tracked-change author, and whether decision records are enabled.

## Five-minute demo

Generate four synthetic negotiation rounds:

```bash
veqtor-demo-rounds ~/veqtor-demo-rounds
```

Register the installed server with Claude Code:

```bash
claude mcp add veqtor -- ~/.local/bin/veqtor-mcp
```

Then ask:

> Using the veqtor tools, what happened to the limitation of liability across
> the rounds in ~/veqtor-demo-rounds?

For a write workflow, ask the client to:

1. extract the current redlines;
2. verify any wording it will rely on;
3. call `preflight_edits` on the complete atomic batch;
4. call `apply_edits` only if `batch_applicable` is true;
5. re-extract the output and export the decision record.

The synthetic write prompt is:

> Prepare our counterproposal as round-5-our-counter.docx: restore the 150%
> affected Work Order liability cap and reinstate the willful misconduct
> carve-out the counterparty deleted. Preflight the complete batch before
> applying it.

Veqtor never overwrites the source or an existing output file. The synthetic
four-round generator also preflights every target and rolls back the complete
batch if publication cannot finish.

## Claude Desktop

GUI applications do not inherit the shell `PATH`, so use an absolute command
path in `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "veqtor": {
      "command": "/Users/you/.local/bin/veqtor-mcp",
      "env": {
        "VEQTOR_TRACKED_CHANGE_AUTHOR": "John Deer"
      }
    }
  }
}
```

Quit Claude Desktop fully and reopen it after changing the executable or its
environment. The author is fixed for the server process and cannot be changed
by the model per edit. If unset, it defaults to `Veqtor MCP`.

## Tool surface

- `list_rounds`: deterministic filename-ordered local DOCX discovery.
- `extract_redlines`: tracked-change units with hashes, references, bounded
  paragraph context and conservative manual labels.
- `verify_quote`: anchored `exact`, `normalized`, or `not_found` verification.
- `preflight_edits`: the complete apply pipeline as an in-memory dry-run.
- `apply_edits`: atomic tracked replace/delete, counter and reinstate writes.
- `export_decision_record`: compact privacy-aware local provenance.

The complete request, response and error contract is in [API.md](API.md).

## Privacy and assurance boundary

The server has no embedded model calls and does not upload whole documents in
the background. Text returned by a tool enters the MCP client conversation and
may be sent to the user's model provider under that provider's terms.

Unless `VEQTOR_DISABLE_DECISION_RECORD=1`, tools append a private local journal
at `<matter>/.veqtor/decision-records.jsonl`. Read-only document calls,
including `preflight_edits`, still write provenance there. The raw journal may
contain verbatim matter text; do not commit or share it. MCP exposes only a
bounded compact projection.

Decision records are best-effort local provenance. Their hashes are
re-checkable fingerprints, not authentication. The journal is not a hash chain,
not tamper-evident, and not a transactional audit log. The supported threat
model is a non-hostile, single-user local macOS/Linux workspace.

## Supported surface

| Surface | v0.1 Alpha |
|---|---|
| Operating systems | macOS, Linux |
| Python | 3.12, 3.13, 3.14 |
| Transport | local stdio MCP |
| Validated clients | Claude Code, Claude Desktop |
| DOCX part | `word/document.xml` |
| Writes | tracked replace, delete, counter, reinstate |
| Distribution | versioned GitHub source and wheel/sdist release artifacts |

Windows, hosted MCP, comments/headers/footnotes, accept/reject, semantic
cross-round lineage and cryptographic audit guarantees are outside v0.1.

## Development

```bash
uv lock --check
uv sync --frozen --all-extras
uv run --frozen pytest -q
uv build --clear
uvx twine check dist/*
uv run --frozen python scripts/check_release_artifacts.py dist/*
```

The private dogfood suite is opt-in:

```bash
VEQTOR_PRIVATE_FIXTURE_DIR=/path/to/private/corpus uv run --frozen pytest -m private
```

Never add real client documents or derived confidential fixtures to the
repository or its history. Contributions use DCO sign-off; see
[CONTRIBUTING.md](CONTRIBUTING.md). Security reports should follow
[SECURITY.md](SECURITY.md).

## Release status

Release notes are maintained in [CHANGELOG.md](CHANGELOG.md). The manual Release
acceptance boundary and finite stop conditions are defined in
[RELEASING.md](RELEASING.md). The manual Release
workflow accepts one full commit SHA, reruns the complete public matrix,
minimum-version tests, Gitleaks, artifact validation, an independent byte-for-
byte rebuild and installed-wheel smoke,
and verifies that SHA is already in `main`. Private dogfood is a local gate
against the same SHA and is attested through the protected `release`
environment approval. Only the final write-scoped job creates the `v0.1.0` tag
and GitHub Release, using the exact artifacts built by the read-only CI job.
That job rechecks the approved `main` SHA, creates the tag atomically through
the Git Data API, verifies its target and publishes only with `--verify-tag`.
The repository release environment must require reviewer approval and allow
deployments only from `main`; a repository ruleset must prevent updates or
deletion of `v*` tags. GitHub Immutable Releases must be enabled, and the
environment must expose `RELEASE_ADMIN_READ_TOKEN`: a fine-grained token limited
to this repository with read-only Administration permission, used only to
fail closed when that setting is unavailable or disabled. Configure and verify
all protections before the first workflow dispatch: merely referencing an
absent environment from the workflow causes GitHub to create it without
protection rules. After publication, a tokenless consumer check downloads the
three public assets, verifies their bytes and portable checksums, and reruns the
closed artifact-identity check.
