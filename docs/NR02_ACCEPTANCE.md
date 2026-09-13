<!-- SPDX-License-Identifier: Apache-2.0 -->

# NR-02 exact-candidate acceptance

Run this only after independent review PASS and the final gate order. Unit tests
use synthetic envelopes and do not establish native Codex acceptance. All raw
outputs, baselines, wheels, environments and synthetic matters belong outside the
checkout, source/test/build inputs and public reports. No permanent MCP settings
are changed. NR-02 performs no DOCX writes, so a repeated complete NR-01 Word
render/lifecycle exercise is not an automatic gate.

## Locked installation and exact source identity

Complete the applicable CONTRIBUTING development gates on the reviewed H/T.
Install the exact built wheel into an external environment containing runtime
requirements exported from the frozen lock with hashes. Use `uv pip sync
--require-hashes` for that export, then `uv pip install --no-deps` for the local
wheel. Run `uv pip check`. Do not use ambient Python or fetch an unlocked runtime.
The new acceptance scripts run with `uv run --frozen python`; the installed
server uses the external environment's absolute interpreter path with `-I`.

```bash
uv run --frozen python scripts/check_position_install.py \
  --source-root /absolute/reviewed-checkout --commit EXACT_H --tree EXACT_T \
  --wheel /private/gates/veqtor_mcp-0.4.2.dev0-py3-none-any.whl \
  --sdist /private/gates/veqtor_mcp-0.4.2.dev0.tar.gz \
  --python /private/gates/environment/bin/python > /private/gates/installation.json
uv run --frozen python scripts/prepare_position_acceptance.py \
  --bundle /private/gates/native --installation /private/gates/installation.json
```

The installation check requires clean exact H/T, the complete development source
inventory, exact wheel and sdist bytes, an installed distribution outside the
checkout, all eleven tools, every installed Python source byte, and its producer
fingerprint. It does not modify frozen v0.4 artifact manifests or goldens.
`prepare_position_acceptance.py` explicitly generates synthetic DOCX setup and
writes a private baseline **before** any position save. Five complete values,
IDs, source hashes/refs, the update and both conflict alternatives are predeclared.
No expected wording is copied from a tool response. Keep the installation report
and baseline intact; receipts bind their hashes and pre-run baseline time.

## Native capture

Use a working native Codex executable. A package-manager shim may be broken;
verify the selected executable's `exec --help`. The capture script requires the
native options `--json`, `--skip-git-repo-check`, `--ignore-user-config` and
`--ephemeral`. Authentication stays available, but user MCP configuration and
previous dialogues are not loaded. Each launch starts a new client and installed
server process with the single `veqtor_nr02` registration. No model override is
made. Use native MCP calls only; shell/file actions by the model fail the checker.

```bash
uv run --frozen python scripts/capture_position_session.py \
  --bundle /private/gates/native --name save \
  --codex /absolute/native/codex --prompt-file /private/gates/prompts/save.txt
```

Write each prompt from the fixed scenario below and `baseline.json`. Give the
client exact operations and folder, requiring native calls in order. A preceding
result may supply **only revision/identity**, never expected wording. Every read
must explicitly send `include_history: true, check_sources: true`. Never replace
full readback with a final narrative, journal export, snippet or save response.
The capture writes private raw JSONL, prompt, stderr and a receipt binding the
command, installed report, baseline, timestamps and before/after DOCX hashes.
It creates files exclusively: retry a failed scenario in a fresh bundle.

The fixed scenario uses IDs in baseline order (1–5):

| Session | Required native calls |
|---|---|
| `save` | Read absent original; create all five exact baseline contents at null revision in one batch; full read. |
| `confirm` | Confirm IDs 1 and 4, version 1, with the baseline statement and `user_confirmed: true`; full read. Business status of ID 4 stays pending. |
| `update` | Replace ID 1 version 1 with `updated_content`; full read. Its current confirmation is null, version 2, with old confirmation/text in history. |
| `restart` | One full read of original. The capture generates a fixed prompt containing only the folder/read flags; do not pass `--prompt-file`. |
| `withdraw` | Withdraw ID 5 version 1; full read. |
| `moved` | One full read after the external move. Capture generates a fixed prompt explicitly selecting the baseline's other `selected_current_file` as current; old references must remain bound to the original file. Omit `--prompt-file`. |
| `changed` | One full read after external synthetic source alteration. |
| `missing` | One full read after external removal of that source. Other DOCX files remain; old refs must not select them. |
| `journal_disabled` | Update source-free ID 3 version 1 with its original baseline content, then full read. Capture sets the disable environment variable to 1. |
| `journal_corrupt` | Update ID 3 version 2 with that same original baseline content, then full read, with the corrupt journal setup below. |
| `other` | One full read of the explicitly separate empty other matter. |
| `conflict_a`, `conflict_b` | Launch concurrently. Both must first read the identical predeclared copied revision. A then updates ID 2 version 1 to conflict content A; B to B. Exactly one succeeds, the other gets native `revision_conflict`. No automatic retry. |
| `conflict_final` | New session fully reads the winner after both attempts finish. |
| `retry` | In a fresh session with the prior result treated as unknown, repeat the exact winning request with its **old** expected revision. Require native conflict, then full read preserving the winner. Controlled post-commit storage uncertainty itself is exercised separately by the fault tests below. |
| `first_a`, `first_b` | Launch concurrently against the first matter with source DOCX but no store. Both must read uninitialized first, then create the five baseline values with ID 2 replaced by their predeclared A/B alternative at expected revision null. Exactly one succeeds. |
| `first_final` | New session fully reads the first-creation winner after both attempts finish. |
| `copy_independent` | After conflicts and journal tests, new session fully rereads moved. It must retain its own ID 3 version 3 history, without the other copy's ID 2 update. |

Concurrent scheduling is observed, not presumed. Receipts must overlap and both
native reads must observe the same revision (null for first creation). If one
client reads after the competing write, that run does not prove the required
race and must be repeated with fresh synthetic setup. Never rebase or rewrite
old evidence to force a pass.

## Explicit external synthetic setup

After `withdraw`, while all relevant processes are idle, copy the original
matter including `.veqtor` to baseline `conflict`, then **move** the original
whole folder to `moved`. The original path must disappear. Do this as labelled
scenario preparation outside native model runs. The first matter's source DOCX
and empty other folder were created by the prepare script.

After `moved`, alter only source files named by initial position bindings in the
moved folder; use explicitly labelled synthetic bytes. Preserve all unbound DOCX
files, including the alternative current document. After `changed`, remove only
those bound source files. After `journal_disabled`, create/replace the optional
moved `.veqtor/decision-records.jsonl` with exactly these synthetic bytes:
`NR-02 synthetic corrupt journal` followed by one LF. Capture `journal_corrupt`.
The receipt requires that journal hash to remain unchanged during the position
commit/read. The authoritative store is never altered during this setup.

Every native capture requires identical before/after DOCX hashes. The checker
also verifies independent baseline source bytes before normal/move stages,
changed/absent old files after labelled setup, and unchanged alternative files.
All copied matters are independent: no synchronization or inferred current-file
selection is part of this feature.

## Checker and non-native failure gates

```bash
uv run --frozen python scripts/check_position_acceptance.py --bundle /private/gates/native
```

The checker requires all 19 sessions, paired ordered native MCP starts/results,
complete identical structured/text payloads, the exact installed producer,
19 distinct client and server session IDs, exact mutations, full predeclared
position/history values, source observations, real conflicts and final rereads.
It rechecks the exact installed artifacts and source tree at verification time.
Missing receipts, extra command overrides, different stores, compact summaries,
missing history or old server/client contexts fail. Raw evidence is not signed
and the checker does not claim authenticity against a malicious evidence author.

The targeted `tests/test_positions.py` fault cases separately exercise controlled
precommit write/fsync/output-validation failure and replace/postcommit/directory
fsync uncertainty, exact old-request retry, lock timeout, workspace replacement,
invalid atomic batches, link/special-file safety, corrupted/unsupported snapshots,
position/history/JSON limits and provenance independence. The native `retry`
case is recovery from an unknown prior response, not evidence of an injected
fsync failure. Both gates are required; native narratives cannot replace those
fault checks. `tests/test_position_acceptance.py` keeps a valid positive control
and removes or weakens each mandatory native item. The normal transport suite
includes the two new tools for both pinned SDK versions under CONTRIBUTING.
