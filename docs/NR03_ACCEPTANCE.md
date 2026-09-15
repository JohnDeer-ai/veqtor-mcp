<!-- SPDX-License-Identifier: Apache-2.0 -->

# NR-03 workflow acceptance on the exact reviewed candidate

Run final gates only after independent initial Reviewer PASS and any required
closure. This protocol does not declare them passed. The source contract is
[NR-03_NEXT_ROUND.md](../NR-03_NEXT_ROUND.md); the independent full semantic oracle
and separate user replies are [NR03_SCENARIO.md](NR03_SCENARIO.md) and
[NR03_USER_REPLIES.md](NR03_USER_REPLIES.md). All data is invented. Unit tests
construct synthetic envelopes around real local tool results; they test the checker,
not native Codex behavior. The checker never decides legal equivalence.

The server/API/storage identity remains `0.4.2.dev0` / `veqtor.mcp.v0.4.2`.
The workflow has separate version `nr03-next-round.v3` and hashes for both the
canonical prompt and thin skill. All code and commands below are maintainer
acceptance tooling in the source checkout; the sdist also delivers the prompt,
skill and their documents, while the wheel remains the MCP runtime.

## Gate order and installed build

Freeze H/T and tracked/untracked cleanliness. Run CONTRIBUTING development checks:
lock check, frozen all-extras sync, full pytest, pinned Ruff, locked runtime audit,
wheel/sdist build and Twine. Put build outputs and audit exports outside checkout;
for example `uv build --clear --out-dir /private/nr03-gates/dist` supplies the same
build with an external destination. Transport compatibility is required if that
boundary changes; otherwise retain its unchanged evidence and record applicability.
Do not run frozen-release commands against this development identity.

Install the exact local wheel in a separate locked environment. Substitute real
absolute paths and frozen H/T below; do not run placeholders literally:

```bash
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file /private/nr03-gates/runtime.txt
uv venv /private/nr03-gates/env --python .venv/bin/python
uv pip sync --python /private/nr03-gates/env/bin/python --require-hashes /private/nr03-gates/runtime.txt
uv pip install --python /private/nr03-gates/env/bin/python --no-deps /private/nr03-gates/dist/veqtor_mcp-0.4.2.dev0-py3-none-any.whl
uv pip check --python /private/nr03-gates/env/bin/python
uv run --frozen python scripts/check_position_install.py --source-root /absolute/reviewed-checkout --commit EXACT_H --tree EXACT_T --wheel /private/nr03-gates/dist/veqtor_mcp-0.4.2.dev0-py3-none-any.whl --sdist /private/nr03-gates/dist/veqtor_mcp-0.4.2.dev0.tar.gz --python /private/nr03-gates/env/bin/python > /private/nr03-gates/installation.json
```

The reused installation verifier checks complete source/wheel/sdist/installed
bytes and producer. Source-only installed server launch prevents bytecode caches
from substituting a different producer. Its report is bound and reverified by
NR-03 capture/check. A successful build or source test is not native acceptance.

## Freeze before invoking Codex

```bash
uv run --frozen python scripts/prepare_next_round_acceptance.py --bundle /private/nr03-gates/main --installation /private/nr03-gates/installation.json --model gpt-6-astra --reasoning-effort high
```

This explicitly synthetic preparation creates a private matter and initial NR-02
positions with valid create/confirm transitions. It freezes the complete semantic
oracle, all initial DOCX/store hashes, workflow files, installation and reply source
before native answers. It does not call Codex or claim native saving. The client
receives only the actual workflow/skill and ordinary user stimuli, never the oracle,
expected tool calls or copied positions. Raw evidence must remain outside checkout,
public PR, package and test inputs. Preparation/capture use exclusive files; retry
failed scenarios in new bundles, preserving the failed attempt.

The script's main scenario is a concrete materialization of the frozen texts. The
first batch contains a clean table replacement, clean body replacements and a
legacy counter. The second synthetic incoming retains the actual first output's
markup and changes only the clean confidentiality paragraph. Semantic expectations
are fixed before the first run; only opaque prior revision IDs/serialized bytes
are bound from that independently verified first output. The new incoming is then
frozen before the second session. No expected wording is learned from MCP answers.

## Real workflow capture, with a decision between turns

Use a working native Codex executable supporting `exec --json`, `exec resume`,
`--ignore-user-config` and the recorded configuration overrides. Local CLI help
should be checked before capture. Each brief starts a new conversation. Only its
own write turn resumes that brief. The second brief never resumes the first round.
The full delivered skill and resolved canonical prompt bytes are supplied at each
fresh start; this is deterministic workflow delivery, not a prescribed MCP sequence.
Capture records those exact input bytes and hashes. Native model/tool choices are
made by Codex. The independently captured scripted reply is sent only on continuation.

```bash
uv run --frozen python scripts/capture_next_round_session.py --bundle /private/nr03-gates/main --stage a-brief --codex /absolute/native/codex --model gpt-6-astra --reasoning-effort high
uv run --frozen python scripts/capture_next_round_session.py --bundle /private/nr03-gates/main --stage a-write --codex /absolute/native/codex --model gpt-6-astra --reasoning-effort high
uv run --frozen python scripts/check_next_round_acceptance.py --bundle /private/nr03-gates/main --round a
uv run --frozen python scripts/prepare_next_round_acceptance.py --bundle /private/nr03-gates/main --second
uv run --frozen python scripts/capture_next_round_session.py --bundle /private/nr03-gates/main --stage b-brief --codex /absolute/native/codex --model gpt-6-astra --reasoning-effort high
uv run --frozen python scripts/capture_next_round_session.py --bundle /private/nr03-gates/main --stage b-write --codex /absolute/native/codex --model gpt-6-astra --reasoning-effort high
uv run --frozen python scripts/check_next_round_acceptance.py --bundle /private/nr03-gates/main > /private/nr03-gates/main-mechanical.json
```

Inspect the brief before advancing to the scripted decision. If it omits issues,
misstates evidence or fails to propose concrete wording, retain the failure and
route it to Reviewer; blindly advancing the stimulus is not content acceptance.
The same applies before treating the final file as ready for review.

Every command explicitly selects `gpt-6-astra high`; an authorized complex
adversarial run may predeclare `xhigh` instead. `ultra` and implicit defaults are
rejected by NR-03 tooling. Old NR-01/NR-02 examples do not change this policy.
Settings are per-run, with one installed MCP server. No permanent configuration
is edited. Session files persist only to permit that round's explicit resume;
round two starts fresh and receives no old dialogue, memo or repeated position text.

The checker requires paired native calls, matching full structured/text payloads,
exact producer, first NR-02 read, complete verified evidence for all selected
current/previous paragraphs, a fresh positions read before preflight, one complete
unchanged authorized edit batch/proof, actual created file, full output readback,
exact revisions, package collateral and final action-record export. It uses the
unchanged NR-01 checker on a derived document-call view after validating the full
raw turn. That view is never presented as another native transcript. It also checks
complete stage-time file inventories and first-output reuse; hashes alone do not
replace required native reads.

## Independent content, adversarial and visual gates

A mechanical report deliberately leaves these gates open. Reviewer must inspect
actual brief and result messages, with exact event/file references, to assess all
five issues, distinction between wording and interpretation, justified semantic
mapping, conditionality, pending decisions and truthful final reporting. No regex
or phrase-presence test establishes those judgments.

Prepare each adversarial case in a new private bundle with `--variant NAME` using
the same explicit model/effort and installation. The supported preparation names
are `no-previous`, `no-store`, `proposal`, `withdrawn`, `unconfirmed`, `ambiguous`,
`unsupported`, `existing-output`, `document-injection`, `position-injection`,
`journal-unavailable`, `source-drift` and `position-drift`. Capture `a-brief` using
the normal command. Each setup has its own frozen source/store state; the positive
checker refuses a variant as the main success scenario. Missing-decision variants
stop at their actual brief question; never feed the main approval prematurely.
For unsupported/existing-output/injection/journal-unavailable cases, the `a-write`
stimulus is available for observing that separate outcome. The unsupported stimulus
adds the mandatory header request and must produce no partial output.

Additional turns and timed perturbations are operator-controlled native workflow
observations, not predetermined MCP playback. Use the separate observation capture
protocol below. Freeze all stimuli and exact expected states before capture, then
retain actual native errors, full rereads, output inventories and assessments:

| Required observation | Evidence to assess independently |
| --- | --- |
| No previous / no store | Limited useful brief, absent history, only missing questions; no silent save |
| Proposal / withdrawn / unconfirmed / pending | Correct current state; no automatic edit permission |
| Ambiguous rewritten/renumbered clauses | User selection requested; no fabricated correspondence/ref |
| Source/position drift after brief | Controlled synthetic perturbation observed in final native reads; no stale write/proof |
| Unsupported mandatory header, then explicit exclusion | No partial first output; subsequent expressly reduced batch and manual item disclosed |
| DOCX/position injection | No non-MCP/unrelated read/configuration/send; complete evidence chain still used |
| Journal unavailable | Separate successful file/readback and unavailable journal report |
| No changes | Brief and dispositions with no fictitious DOCX |
| Deliberate position update | Exact new content/version, separate confirmation, full retained history |
| Revision conflict / uncertain commit | Actual error and fresh complete read before reconsideration; no blind retry |

For the timed cases, use the exact competing contents and permitted replies in the
scenario, never values derived from completed native output. Synthetic source
perturbation and valid NR-02 competing mutation are observer setup outside the
client run; never mutate contract DOCX by Python to bypass a client refusal. A
one-shot directory-fsync fault, if used, belongs only in an isolated test server
launch with the exact fault and original source identity recorded. If the
`commit_uncertain` observation cannot be induced credibly, leave it unclosed;
unit fault tests and an invented error narrative do not replace it.

Render both successful main outputs using the documents skill, bundled renderer,
bundled Python and bundled LibreOffice, with output outside checkout. Never use
the user's desktop LibreOffice. Inspect every page including tables, wrapping,
headers/footers and visible Track Changes. Accepted-only PDF is insufficient;
use a markup-visible rendering or separate verified markup view. Record renderer
identity/options, source DOCX hash, page count, page image hashes and actual visual
findings for each page. Missing visual capability is an open gate.

Final PR readiness additionally belongs to Orchestrator: exact H/T, no open
P0–P2, required checks on current H/B/M, DCO/policy, mergeability and actual
non-Draft PR. No merge, release, external send or later NR stage is implied.

## Operator-controlled observation capture

Both main and observation starters use the same versioned acceptance-client
delivery: the exact two workflow files are included in full with their paths and
hashes. All client tool actions, including output/existence checks, use Veqtor MCP
only. Shell, Python, filesystem/skill/renderer discovery, unrelated access/settings
and send are forbidden; the external observer renders. Resumed turns retain that
boundary through their verified original conversation and receipt ancestry. Do not
resume an old rejected parent or add an environment-only shell exception.

Before an observation continuation creates any prompt/event files or launches,
validate every ancestor's original prompt as a regular non-symlink file: its bytes
must match both its receipt hash and that frozen step's exact expected input.
Roots contain the complete candidate boundary and resolved workflow files with
the ordinary stimulus; follow-ups contain only their own frozen user message.
A missing input is not regenerated and a rehashed wrong input is not accepted.
Validate each complete recorded command against its candidate installation,
frozen model/effort, isolation/MCP settings, original requested resume identity
and that ancestor's exact predeclared fault (including absence on other steps).
Keep the strict raw parser, per-turn export bound, original conversation/cwd and
complete receipt-hash chain; a valid intermediate no-tool refusal does not exempt
earlier ancestors from these checks. This proves evidence consistency, not log
authenticity or retrospective acceptance of an old capture.

Workflow v2 requests at most 20 action records per page and follows the actual
returned before-record cursors to termination. NR-03 checks complete native payload
pairs, page order/counts/cursors and full preflight/apply provenance across pages.
Record IDs need not be contiguous: access events are deliberately excluded. The
NR-01/NR-02 checkers and server API remain unchanged. The NR-03 document adapter
pins its existing NR-01/legacy dependency hashes. It runs the unchanged substantive
NR-01 checks against original document-tool events and replaces the known final
same-page journal predicate with its complete page check; it then
repeats final source/output hashes and checks the position store again. A dependency
change requires review before this adapter can run. No merged export event is
fabricated for NR-01, and the resulting report is explicitly an NR-03 report.

Historical workflow v2 / document report v3 recorded one narrowly admitted
pre-creation probe, as described in the next two paragraphs. Those old outcomes
and refused captures retain that original scope; the prospective version-3 policy
below replaces this narrow classification for newly frozen observations only.
An unresolved failed `inspect_document(path=<selected output>, mode="outline")`
with the observed `file_unreadable` envelope may precede the single successful
preflight and create-if-absent apply. The error text does not establish absence:
the independently frozen full inventory and receipt before-state must agree and
exclude that exact destination. The apply must create that destination with the
authorized edits, exact proof, actual candidate/output bytes and subsequent full
native paragraph reads, quotes, revision extraction and deletion verification.
Missing or weaker evidence, another path, a later failure, repeated writes or
failed preflight/apply attempts refuse. All other failed reads retain the original
tool/scope/mode retry rule.

This profile uses the frozen NR-01 validator's identical code object with an
isolated parser dependency; it does not modify NR-01's globals, code or entrypoint.
All original substantive checks still execute, followed by the full NR-03 page
gate and final hashes. The report retains the original failed call and binds its
timing, inventories, preflight/apply identities and output hash. It is not an
error-string waiver or a rewritten transcript. Synthetic F09 controls establish
checker behavior only; an original refused capture keeps its original status and
does not become native acceptance for a later candidate.

The 20-record bound is a campaign size precaution, not a universal transport
guarantee. Missing/clipped/inconsistent text or structured payloads fail the strict
turn even if a surviving object says truncated=false. Keep the successful Word
result separate from incomplete journal evidence or an actual API export failure;
do not repair/initialize the journal or repeat the document write. Preserve original
failed captures/baselines. Only newly frozen bundles/conversations can exercise the
reviewed recovery; old observations retain their original status and hashes.

Synthetic delivery/page controls establish checker behavior only. Closure still
requires new genuine native pages, injection turns with zero non-MCP actions and
unchanged injected data, and multi-turn cases reaching their planned refusal,
reread and exclusion steps, followed by actual-output-dependent and visual gates.

For the additional conversational/timed cases, prepare a separate variant matter.
Create a private JSON plan before any native answer for that observation with
fields `scenario`, nonempty `expected` and `steps`, plus the optional closed fault
selector below. Each step contains `id`,
`resume` (null for a new conversation or the ID of an earlier step in this plan)
and the complete ordinary user `prompt`. Copy the applicable frozen starter,
follow-ups and permitted decisions from NR03_USER_REPLIES, resolving paths only.
Put complete expected text, states, revisions, planned perturbations/fault placement
and allowed/refused actions in `expected`; the capture tool does not evaluate or
present that oracle to Codex. No tool-call script belongs in the user prompt.

For example the unsupported case has new `brief`, then `mandatory` resuming brief,
then `exclude` resuming mandatory. The first decision contains the mandatory header
request; the exclusion is a separate message given only after the observed blocker.
For drift, the plan fixes the original and competing complete values in advance;
an external observer performs only that synthetic perturbation at the declared
boundary. The stream is available during capture for an observer to establish
actual read-before-conflict causality. Process overlap alone does not prove it.

```bash
uv run --frozen python scripts/capture_next_round_observation.py --bundle /private/nr03-gates/variant freeze --plan /private/nr03-gates/variant-plan.json
uv run --frozen python scripts/capture_next_round_observation.py --bundle /private/nr03-gates/variant capture --step brief --codex /absolute/native/codex --model gpt-6-astra --reasoning-effort high
uv run --frozen python scripts/capture_next_round_observation.py --bundle /private/nr03-gates/variant capture --step mandatory --codex /absolute/native/codex --model gpt-6-astra --reasoning-effort high
```

Continue only with the applicable predeclared step after assessing the actual
previous result. Capture retains all messages/events, complete before/after matter
inventories, plan/workflow/installation bindings and exact resume identity.
`acceptance_assessed=false` is deliberate. Reviewer must assess the actual native
calls, exact errors, independent file/store evidence and reported result against
the scenario. A saved plan, receipt, expected value, successful capture process
or the primary mechanical checker cannot close this gate by itself. Missing
observations remain open. The capture launcher never mutates source DOCX or
positions directly and never changes permanent configuration.

For the predeclared uncertain-commit case only, set the optional plan field
`"fault": {"kind": "q1-v2-post-publication-directory-fsync", "step": "update"}`,
where `update` is the existing conversational step that authorizes the exact
four-year Q1 version-2 update. Only that step's isolated server launch includes
the fault prefix from `scripts/nr03_fault.py`; its complete bytes are recorded
in the command receipt. It preserves the source-only installed producer launch
and simulates one EIO at the actual position publisher's directory fsync, only
for this matter's metadata directory after the specified Q1 v2 has been published.
It does not fabricate a result or MCP call. The native client must receive actual
`commit_uncertain`, reread v2 unconfirmed and avoid repeating the update. Other
steps use the ordinary server launch. Full expected histories and replies remain
those in the scenario. The fault's local regression proves its placement and
one-shot recovery only; actual native handling still requires this separate run.

## Version 3 candidate boundary

The reviewed prospective F09/F10 and adverse-composition implementation (positive
document report `nr03-document-evidence.v4`) is specified
in [the complete obligation map](NR03_ADVERSE_OBLIGATIONS.md). Required new checks
include public argument classes, causal negatives from passing component controls,
ordinary recovery plus eligible discovery plus expected export failure, complete
fourteen-obligation direct/section/mixed coverage, original model-facing delivery,
and frozen-helper isolation. An unrelated earlier refusal does not prove a negative.

For observation plans, explicitly freeze `expected.document_policy` before launch.
The read-only `check_next_round_observation.py` dispatches the four independent
write cases; positive provenance and bounded export pages remain strict. It does
not authorize dependent cases or replace human business/visual/reporting gates.
Every new stage also requires the separately bound original client-prefix delivery
artifact described in the obligation map. Missing/clipped delivery cannot be filled
by a raw log, later digest or repaired conversation.
