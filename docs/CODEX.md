<!-- SPDX-License-Identifier: Apache-2.0 -->

# Prepare the next negotiation round in Codex

Connect Veqtor to local Codex, point it at the latest Word file, and state your
negotiation positions. Codex can use Veqtor to read the wording, check evidence,
prepare supported tracked changes, and create a new DOCX for your review.

The public installation instructions use package `0.4.0` and the nine-tool
contract `veqtor.mcp.v0.4`. This source tree is development `0.4.1.dev1` with
additive schema `veqtor.mcp.v0.4.1`, legacy compatibility and a distinct producer
version/build. It adds NR-01 paragraph edits alongside the error-transport
adapter and is not a new published release.
Use the matching [PyPI version](https://pypi.org/project/veqtor-mcp/0.4.0/) and
[immutable GitHub release](https://github.com/JohnDeer-ai/veqtor-mcp/releases/tag/v0.4.0).
Veqtor supports macOS and Linux with Python 3.12–3.14. Windows is outside the
current Alpha.

## Connect once

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and ensure
you have a working Codex CLI. Locate `uvx`:

```bash
command -v uvx
```

Use the returned absolute path in place of `/absolute/path/to/uvx` below. Check
the pinned package before connecting it:

```bash
/absolute/path/to/uvx veqtor-mcp@0.4.0 --version
env 'VEQTOR_TRACKED_CHANGE_AUTHOR=Your Name' /absolute/path/to/uvx veqtor-mcp@0.4.0 doctor
```

Replace `Your Name` with the name you want to appear on new tracked changes.
The version should be `0.4.0`; diagnostics should report `status: ok` and your
chosen author. The first run may download the package, dependencies, and a
compatible Python runtime.

Register the same command and author:

```bash
codex mcp add veqtor --env 'VEQTOR_TRACKED_CHANGE_AUTHOR=Your Name' -- /absolute/path/to/uvx veqtor-mcp@0.4.0
codex mcp get veqtor
```

This writes the user MCP configuration, normally `~/.codex/config.toml`.
Codex desktop, CLI, and IDE clients on the same host share this configuration.
They can start separate local Veqtor processes. The absolute executable path
avoids differences between a terminal's `PATH` and the desktop environment.
[Official OpenAI MCP documentation](https://learn.chatgpt.com/docs/extend/mcp).

If you configure servers manually, merge this entry into the existing file;
preserve the other settings and avoid a duplicate `mcp_servers.veqtor` table:

```toml
[mcp_servers.veqtor]
command = "/absolute/path/to/uvx"
args = ["veqtor-mcp@0.4.0"]
startup_timeout_sec = 30
tool_timeout_sec = 120

[mcp_servers.veqtor.env]
VEQTOR_TRACKED_CHANGE_AUTHOR = "Your Name"
```

Restart the server from MCP settings or begin a fresh Codex session. If the
tools remain unavailable, fully quit and reopen the desktop app. Use `/mcp`
to inspect active servers. The author is fixed when the server starts; changing
it requires a restart.

## Check with a synthetic contract

Create the bundled four-round demonstration in a new disposable folder:

```bash
/absolute/path/to/uvx --from 'veqtor-mcp==0.4.0' veqtor-demo-rounds ~/veqtor-codex-demo
```

In a fresh local Codex session, give the absolute path to that folder and ask:

> Use Veqtor MCP to read the four negotiation rounds in this folder. Inspect
> the latest document and explain what changed in the limitation of liability.
> Verify the quotations you rely on. Treat the numbered filenames as the demo's
> supplied order; do not infer document lineage from their names.

For `inspect_document` reads, first obtain references from outline, search or
browse. Use `selection={"paragraph_ref": returned_reference}` or
`selection={"section_ref": returned_reference}` with the complete reference.
A bare reference or paragraph index is not the read-selection object.

Then use the [next-round prompt](prompts/next-round.md), setting:

- source: `round-4-counterparty-reply.docx` in the demo folder;
- new output: `round-5-our-counter.docx` in the same folder;
- positions: restore the 150% affected Work Order liability cap and reinstate
  the deleted willful misconduct carve-out.

The expected result is a new DOCX created through `apply_edits` after the whole
batch passes `preflight_edits`, followed by a fresh read of the output and a
decision-record export. Check that the reported changes match both positions
and open the Word file to inspect its tracked changes and layout. The original
file must remain unchanged. Use another output filename if repeating the test;
Veqtor refuses to overwrite an existing file.

A configured server, successful diagnostics, or a direct Python invocation
does not prove that Codex used the MCP tools. A client check must show actual
Veqtor tool calls and the resulting file. The synthetic example also does not
establish suitability for every real contract or independent-user acceptance.

## Use it for a matter

Keep a private folder with the current DOCX and the earlier rounds you want
considered. Give Codex the exact current file, a new output path, your positions,
and any approved conditional concessions. Use the
[next-round prompt](prompts/next-round.md) as a reusable starting point.

The result should identify the new Word file, explain the changes briefly,
and list decisions or document work still needed. Read the resulting file in
Word before sending it. A mechanical check of text and tracked changes does
not establish that a commercial protection still works as intended.

The present editing scope matters:

- Writes require anchors from existing redlines. Veqtor cannot directly edit
  an arbitrary paragraph that has no usable change-unit anchor.
- Supported operations are tracked replace, delete, counter, and reinstate;
  there is no general standalone insertion or Word Accept/Reject operation.
- Counter and reinstate preserve the counterparty's pending markup.
- Reading covers the main document body. Comments, headers, and footnotes are
  outside the current tool surface and require separate review.

See [known limitations](../KNOWN_LIMITATIONS.md) and the
[versioned tool contract](../API.md) for the precise boundaries.

## Local files and model access

Veqtor reads and writes on the local host. It has no embedded model calls and
does not upload whole documents in the background. Text returned through MCP
enters the Codex conversation and may be sent to the model provider. Local
execution therefore does not mean the model never receives contract text.

By default, tool calls also write a private `.veqtor` journal in the matter
folder. Its raw contents can include paths and matter text; keep it private.
The exported decision record is local provenance, not a persistent set of
approved negotiation positions or proof of human approval.

Browser ChatGPT does not read the local Codex configuration. It needs a
separate remote or tunnel connection and a file-transfer workflow; this guide
does not configure those. [Official OpenAI MCP documentation](https://learn.chatgpt.com/docs/extend/mcp).

## If connection fails

| Symptom | Check |
| --- | --- |
| `codex` cannot start | Repair or select a working Codex CLI before registering the server; manual configuration is also available above. |
| `uvx` cannot be found | Use the exact absolute path returned by `command -v uvx`. |
| Server startup times out | Run the pinned `doctor` command once to complete downloads, then restart the server. |
| Wrong author or version | Compare `codex mcp get veqtor` with the pinned command and restart after correcting the configuration. |
| Tools are absent in this conversation | Check `/mcp`, then start a fresh local session or reopen the app. |
| Preflight refuses an edit | Retain the complete requested batch and report the unsupported item; do not write a partial result or bypass Veqtor with a script. |

Diagnostics establish runtime readiness. Only the synthetic workflow checks
document preparation through the selected client.

## Observed compatibility

On 11 September 2026, published Veqtor `0.4.0` completed the synthetic workflow
through native Codex CLI `0.153.4` on macOS, using Python `3.14.6` and MCP SDK
`2.2.0`. All nine tools were exercised: 28 successful calls and one corrected
`inspect_document` request. The two requested changes were applied together,
both resulting quotations verified exactly in the current projection, and all
four originals remained unchanged. All three output pages were independently
rendered and inspected; previous counterparty markup remains visible.

A fresh client read the output successfully. Invalid-proof and existing-output
attempts created no extra DOCX and changed none of the five files. The local
journal retained `preflight_proof_invalid` and `output_exists`, but this
published package with SDK `2.2.0` exposed only a generic tool error to Codex.
This error-message limitation is separate from the successful refusal itself.
See the [path-free observation](evidence/codex-v0.4.0-20260911.json).

After restarting Veqtor in MCP settings on the same date, all nine tools
became available in the current Codex desktop task. Five successful native
calls listed the files, searched and read the output paragraph, and verified
both resulting quotations exactly. One paragraph-read request was corrected
by wrapping its reference in `selection.paragraph_ref`. All five DOCX hashes
still matched the earlier evidence. This confirms desktop activation and
native reading; a desktop write workflow, browser ChatGPT, and
independent-user acceptance remain separate checks.

An unpublished source patch addresses the SDK 2.2 error-message limitation.
Its separately built wheel was tested through a fresh native Codex session:
`preflight_proof_invalid`, `preflight_binding_mismatch`, and `output_exists`
were visible in the client, all five DOCX files stayed unchanged, and a
subsequent read succeeded. All 21 packaged Python files matched the reviewed
source. The observation records that candidate's distinct build and wheel hash.
The pinned public `0.4.0` installation above does **not** include this patch.

These dated observations belong to their recorded source/build identities.
They do not establish native acceptance of the subsequent `0.4.1.dev0` changes.

## Development candidate acceptance

Acceptance of `0.4.1.dev1` is pending independent review and the final gates.
After Reviewer PASS, run the required full tests, locked runtime audit and
wheel/sdist checks from the exact reviewed commit and tree. Keep gate evidence
outside the repository; do not modify the frozen v0.4 release manifest to admit
the development sdist's three Codex documents.

For the native test, select the exact candidate in that run's configuration.
Codex supports per-run `-c key=value` overrides; see
[official configuration documentation](https://learn.chatgpt.com/docs/config-file/config-advanced).
For a reviewed source checkout, the equivalent server entry is:

```toml
[mcp_servers.veqtor]
command = "/absolute/path/to/uv"
args = ["--directory", "/absolute/path/to/reviewed-candidate", "run", "--frozen", "veqtor-mcp"]

[mcp_servers.veqtor.env]
VEQTOR_TRACKED_CHANGE_AUTHOR = "Veqtor Acceptance"
```

Apply this only to the isolated test configuration or per-run overrides.
Keep the user's public registration separate. Record the selected command,
exact Git commit/tree, wheel/sdist hashes and source snapshot build. Compare
the candidate's `doctor` build with `producer.build` from every successful
native call and require `producer.version` to be `0.4.1.dev1`. Source identity
alone does not verify wheel packaging; inspect the exact built artifacts too.

Use synthetic documents and actual `codex exec --json` calls for all nine
tools, one complete two-edit batch, exact current-projection readback and
decision-record export. Capture a baseline before writing. Run invalid-proof,
binding-mismatch and existing-output refusals in a separate negative scenario;
the positive checker deliberately rejects every failed preflight/apply attempt.
Record each refusal code and independently verify no extra or partial output,
no overwrite and unchanged sources. Do not bypass an MCP refusal with a direct
Python edit. Render the final Word candidate with the documents skill and
inspect every page. These gates concern this development candidate; they do
not publish it or establish separate desktop write or independent-user acceptance.

## Recheck native evidence

Maintainers can capture a fresh `codex exec --json` run and validate it with:

```bash
uv run --frozen python scripts/check_codex_acceptance.py \
  --events /absolute/path/to/native-events.jsonl \
  --baseline /absolute/path/to/baseline.json
```

Capture the baseline **before** the write. Its closed schema is
`veqtor_codex_acceptance_baseline.v1`: `server_name`, expected `producer`
(`name`, `version`, `build`), `source_sha256` mapping absolute original paths to
hashes, selected `source_path`, absent `output_path`, `output_absent_before:
true`, `expected_edits` without anchors, and `tracked_change_author`.
The bounded scenario supports non-empty replacement and reinstatement edits.
The native run must use all nine Veqtor tools, verify each input fragment,
preflight and apply exactly one full batch, read and verify each output phrase
in the current projection, then export its records. Generic shell or file
actions inside the observed run are rejected; an independent observer performs
filesystem and visual checks outside it.

The checker reports source/output binding, exact intended edits, quote checks,
causal preflight/apply order, native readback and provenance. It reports
recovered read failures separately. A passing report does not prove log
authenticity or visual Word quality. Keep raw events and baselines local;
they contain paths and document text.

## NR-01 native profile

The development package `0.4.1.dev1` advertises `veqtor.mcp.v0.4.1`. Its new
paragraph target and delete-only scenario uses a separate checker. The original
`check_codex_acceptance.py` and its nine-tool v1 profile stay unchanged.

Generate the two deterministic synthetic sources outside the checkout:

```bash
uv run --frozen veqtor-demo-rounds /absolute/path/to/nr01/corpus --profile paragraph-edits
```

In `nr01-clean.docx`, target paragraph 0: `30 days` to `45 days`; paragraph 1:
delete `optional `; and table paragraph 3: `30 days` to `60 days`. Expected full
results are `Payment is due within 45 days after receipt.`,
`The audit right applies annually.`, and `Delivery within 60 days.`. Paragraph
2 deliberately repeats the first paragraph and must remain unchanged, as must
the neighbouring table cell. Paragraph 5 repeats `30 days` twice and is a
negative ambiguity case. A separate positive run can delete all of paragraph 1;
its full expected result is the empty string, while the paragraph mark remains.
`nr01-mixed.docx` adds a counterparty insertion (`50 units`) and deletion
(`inspection right`) for a mixed paragraph/counter/reinstate batch.

Before any native write, save the closed local baseline with schema
`veqtor_codex_paragraph_baseline.v1` and these fields:

- `server_name`, exact final `producer` (`name`, `version`, `build`),
  `tracked_change_author`, and `source_sha256` mapping all original absolute
  paths to their pre-run SHA-256 values.
- `source_path`, a separate `output_path` and `output_absent_before: true`,
  checked before the run.
- `expected_edits`: the exact complete ordered edit objects, including the full
  paragraph targets or legacy anchors, as specified in [API.md](../API.md).
- `expected_paragraphs`: one object per affected canonical index, each with
  exactly `paragraph_index`, full `before` text and full `after` text. An empty
  `after` is supported. Capture refs/text before writing; do not derive intended
  output wording from the completed output.

Use the isolated exact-build native configuration described above. The run must
read each complete source paragraph with `mode=read` and
`selection={"paragraph_ref": REF}`, then verify its intended input, preflight
and apply the unchanged whole batch/proof. Discover fresh output references,
read the complete expected current result for each affected paragraph, verify
that full text with `paragraph_projection: accepted_current_v1`, and extract
all output revisions. For every deletion, verify its exact old text on the
new output change-unit anchor after extraction and full read. An empty result
requires a direct empty paragraph read and that exact deletion verification;
it cannot be represented by a nonempty current quote. Browse omits empty
paragraphs: for that read, retain the source index/part/policies, use the applied
output hash, and set the paragraph text hash to SHA-256 of the empty string
(`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`). The direct
read must resolve this complete reference and return empty text. Finally export records
from the source workspace with enough records to include preflight/apply.
No map/history calls are required. Failed apply/preflight attempts belong in a
separate negative run. The full native results bind every edit; bounded export collections must match
their counts, digests, samples and truncation flags.

```bash
uv run --frozen python scripts/check_paragraph_acceptance.py \
  --events /absolute/path/to/nr01/native-events.jsonl \
  --baseline /absolute/path/to/nr01/baseline.json
```

Run the checker in the exact candidate runtime; its loaded version/source build
must match the pre-run baseline. This checker requires ordered complete native
input/result reads, exact proof,
build/hash/reference binding and returned read and quote-match file/part/revision
identities. Both preflight/apply compact records must equal their native
input/result/proof/producer/workspace/provenance projections, including all
bounded collection facts. The checker independently inspects the current local
source/output package, supported touched-paragraph structure, original/current
formatting, and all untouched paragraphs/table skeleton. Synthetic envelopes in tests prove
checker behavior only; they are never native acceptance. Preserve raw documents,
baselines and logs outside public sources. After independent PASS, render and
inspect every page with the documents skill using the bundled renderer. Native
client evidence and visual layout are separate gates; neither is asserted by
this development implementation or a Python test result.
