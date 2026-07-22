---
name: code-orchestrator
description: Orchestrate one lightweight, bounded Codex Desktop developer/reviewer run with managed worktree tasks and exact-SHA review. Use only when explicitly invoked; never apply it to ordinary coding or review tasks.
---

# Code Orchestrator V1.1

## Purpose and topology

- Run at most one active orchestration run for this repository.
- Only the user-facing Orchestrator invokes this skill. Developer and Reviewer work orders must say: `Do not invoke code-orchestrator or orchestrate other roles.`
- Create the canonical Developer and Reviewer as separate Codex Desktop tasks in managed worktrees (`TOPOLOGY: APP_TASKS`), never as spawned subagents.
- Keep the Orchestrator as coordinator and sole remote publisher; it never edits product code.
- Use no internal subagents by default. At most one bounded read-only specialist may supply leads when a genuinely distinct concurrency, security, privacy, trust-boundary, data-loss, or fail-closed question warrants it. The canonical worker must independently validate any lead and owns its result.

## Start contract and authority

Before dispatching work, record the objective, scope and non-goals, target (`DONE_LOCAL` or `READY_TO_MERGE_PR`), required gates, repository commit policy, and granted authority.

- Orchestrator: create/read/wait/archive tasks; verify and accept exact candidates; push a frozen candidate and create/update a draft PR only when authorized; mark a PR ready only when separately authorized; never edit product code.
- Developer: sole writer; edit and DCO-commit only in its managed worktree; never push or create/update a PR.
- Reviewer: read-only inspection and tests in its managed worktree; never author or stage product changes, create commits, create/update/delete shared refs, or push.
- Merge, release, publication, force-push, history rewrite, and remote-branch deletion always require separate explicit authority.
- Default to `DONE_LOCAL` when push or PR authority is absent. Never infer authority from the target name.
- Escalate decisions that change public behavior, compatibility, material scope, or significant product, security, privacy, trust, or data-loss risk.

Use a short invocation, for example:

```text
$code-orchestrator Bring <objective> to READY_TO_MERGE_PR. You may create and
archive Developer/Reviewer tasks and managed worktrees, create signed local
commits, push frozen candidates, create/update a draft PR, and mark it ready
after all gates pass. Do not merge, release, force-push, rewrite history, or
delete remote branches.
```

## Lean state and communication

Give the run a stable `RUN` and every dispatch a unique `ACTION`. Permit exactly one active writer and keep a `WRITER_GENERATION`. Accept output only when its run, action, role, generation where relevant, and expected Git identity match current state.

Publish a compact canonical card only at run start, a material worker handoff, each frozen candidate, pause/recovery, and final result. A material handoff is the initial Developer, an initial or permitted fresh broad Reviewer, or a worker replacement. Before that worker message, publish one full card carrying the current `ACTION`; the card itself is the durable dispatch record, so do not add an action checkpoint.

Before an ordinary fix, closure, or follow-up message, publish only the one-line `ACTION_CHECKPOINT` below; do not add a full card or separate commentary. For a new task, either dispatch record may use `TASK=pending` until its ID exists. Do not publish cards or checkpoints for waits, triage microsteps, or test progress.

```text
CARD_REV: <n> | RUN/ACTION: <run>/<action> | STATE/TARGET: <state>/<target>
BASE: <B> | HEAD/TREE: <H or ->/<T or ->
TASKS: developer=<id, pending, or -> | reviewer=<id, pending, or -> | writer_generation=<n>
OPEN: <finding IDs or -> | HALT: <reason or ->
GATES/PR: <only current target evidence or ->
NEXT: <one action>
```

```text
ACTION_CHECKPOINT: RUN=<run> | ACTION=<action> | ROLE/TASK=<role>/<task or pending> | EXPECTED=<base or H/T> | NEXT=<dispatch/wait/result>
```

The highest complete `CARD_REV` is canonical for durable run state; the latest dispatch record, whether full card or subsequent checkpoint, is canonical for the in-flight action. `ACTION` detects stale or duplicate output; it does not promise exactly-once execution. Keep the frozen start contract and full findings in their originating tasks; reference task IDs and finding IDs instead of copying long narratives or ledgers. Send commentary only for a material state change, blocker, user decision, or completed gate; aim for no more than 20–30 commentary updates in a normal run.

## Short work orders

Keep Developer work orders near 100–150 words and Reviewer work orders near 120–160 words. Reference the frozen specification instead of pasting it. Include only the objective, exact identity, essential non-goals, authority, checks, and concise return fields. Do not prescribe architecture unless a decision is already frozen.

Developer orders require `EXPECTED_BASE_SHA`, branch, `WRITER_GENERATION`, commit policy, and targeted tests. Results contain base, commit head/tree, changed surface, checks, deviations, and risks. Fix orders link to the Reviewer task and list finding IDs instead of copying the ledger.

Reviewer orders require frozen base/head/tree, specification, read-only boundaries, and severity policy. A blind Reviewer receives no Developer task, completion narrative, prior findings, or suggested search path. Results contain identity, verdict, and concise findings: ID, severity, evidence, reproduction, and closure criterion. Add `ROOT_CLASS` and invariant only for P0/P1 or a recurrent issue.

Every worker order must include:

```text
Do not invoke code-orchestrator or orchestrate other roles. Do not spawn
internal leads unless the Orchestrator explicitly authorizes one bounded
read-only specialist.
```

## Development, review, and gates

Use this normal flow:

```text
DEVELOP -> FIRST_LOCAL_GATE -> FREEZE -> BLIND_REVIEW -> TRIAGE
TRIAGE -> FIX -> FREEZE -> CLOSURE -> TRIAGE
TRIAGE -> FINAL_GATES -> DONE_LOCAL | PR_CI -> READY_TO_MERGE_PR
```

Paused states are `NEEDS_USER`, `NEEDS_RECOVERY`, and `BLOCKED_EXTERNAL`; revoke write authority while paused. Terminated states are `SUPERSEDED` and `ABORTED`. Before another run starts, resume, supersede, or abort any prior active or paused run.

- Let the Developer choose its implementation and use targeted tests while working. For a large specification, use two or three internal milestones in the same Developer task, but do not review each milestone independently.
- Run the repository's standard local suite once before the first freeze. Do not repeat full, minimum-dependency, package, artifact, or reproducibility gates after every fix.
- Use one fresh, independent blind Reviewer for one broad discovery pass on the frozen candidate.
- The Reviewer performs targeted adversarial checks and practical reproductions. Once a P0–P2 is confirmed, finish consolidated discovery and targeted reproductions, but skip full suites and artifact rebuilds on that rejected candidate.
- Return findings to the same Developer for bounded fixes. Fix rounds use targeted regressions and the smallest relevant checks.
- Use the same Reviewer for narrow closure on the new exact candidate when safe. Closure covers the listed findings and directly adjacent regression risk; it is not a new broad review. If the task cannot be reused safely, create a new closure task with the same finding IDs.
- Permit at most one additional fresh broad review in the entire run, and only after a material architecture, public-contract, security, privacy, trust-boundary, or data-loss change invalidates the original review assumptions. Ordinary fixes, test additions, or diff size alone do not trigger it.
- If another material change invalidates review after that allowance is spent, enter `NEEDS_USER` and recommend superseding this run with a new one; never substitute narrow closure or claim readiness.
- After review and closure pass, run each required full/minimum/package/artifact gate once on the accepted tree. Reuse a still-current first local result when it is bound to the same head/tree and policy permits; do not rerun it solely for ceremony. Require only gates named by the target or repository policy.
- A candidate change invalidates identity-bound review, local, and CI evidence. Targeted fix evidence does not claim a final full gate.

If authorized, the Orchestrator may push the exact first frozen candidate and create/update a draft PR so hosted CI runs in parallel with human review. This is early platform evidence, not acceptance; the PR remains draft, and every new head invalidates prior CI for readiness.

Every demonstrated product defect from local or hosted CI becomes a finding with ID, severity, evidence, reproduction, and closure criterion, then goes through normal `TRIAGE`; add `ROOT_CLASS` and invariant when the normal severity rule requires them. If blind discovery is still running, queue non-P0 CI findings and do not interrupt review or dispatch a fix. A confirmed P0 may interrupt discovery, but mark that broad review `INCOMPLETE` and triage all known Reviewer and CI findings together. After the P0 fix and new freeze, run a fresh full `BLIND_REVIEW` for the interrupted review slot; an interrupted initial review is replaced by the run's base review and does not consume the one additional broad-review allowance. Never substitute narrow closure for an incomplete required broad review. Use `BLOCKED_EXTERNAL` only when required external evidence is unavailable.

Finish only with no open P0/P1, every P2 fixed or explicitly accepted by an authorized party, practical regressions for confirmed defects, and current target gates. Put non-blocking P3 items in backlog.

## Design preflight

Before implementing concurrency, security, trust-boundary, data-loss, or fail-closed behavior, spend a short bounded preflight stating:

- the source of truth;
- the linearization or decision point;
- resource and time bounds;
- one or two concrete falsification examples.

The Ultra Orchestrator challenges this note before code. It may request one short independent Ultra adviser only when the risk justifies it; that adviser is not the canonical blind Reviewer. Do not turn the preflight into a second review or a long design document.

## Freeze and reviewer identity

Before any freeze, review dispatch, or push, the Orchestrator verifies expected ancestry, a materially clean Developer worktree, `HEAD == CANDIDATE_SHA`, `HEAD^{tree} == CANDIDATE_TREE`, and commit-policy compliance throughout `EXPECTED_BASE_SHA..CANDIDATE_SHA`. For this repository every commit requires a valid DCO `Signed-off-by` trailer. Freeze or push only after these checks pass. If correction needs an unauthorized history rewrite, enter `NEEDS_USER`.

The Reviewer must not fetch or otherwise update shared refs. The Orchestrator ensures the candidate object already exists in the shared object database. The Reviewer checks out the exact candidate detached and verifies before and after review:

- `HEAD == CANDIDATE_SHA`;
- `HEAD^{tree} == CANDIDATE_TREE`;
- the worktree is materially clean.

Ignored test caches are harmless. Authored or staged product changes, Reviewer commits, changed head/tree, or shared-ref mutations invalidate the review. If the object is unavailable or identity fails, enter `NEEDS_RECOVERY`; only the Orchestrator may perform inspected, repository-specific recovery.

## Triage and architecture decisions

Use `HALT_ARCHITECTURE` only when the substance requires it:

- a required guarantee cannot be made falsifiable and testable;
- a proposed fix creates or depends on a second source of truth;
- a safe fix changes a public/product contract, compatibility promise, security/privacy/trust boundary, or data-loss guarantee beyond authority;
- requirements materially conflict, or no safe implementation exists within current authority.

Finding recurrence alone is not an architecture trigger. After one failed bounded fix, perform one short technical replan. If the same invariant still fails after the next bounded fix, enter `NEEDS_USER` with reason `FIX_BUDGET_EXHAUSTED`, evidence, and a recommendation unless one of the substantive architecture triggers independently applies. Never run an unbounded patch loop.

For a real `HALT_ARCHITECTURE`:

1. Revoke write authority and freeze current identities.
2. Ask the Developer and either the Reviewer or one independent adviser the same neutral question. Each returns at most one recommendation and, only if materially different, one alternative, in no more than 250 words.
3. The Ultra Orchestrator decides when a safe option stays within authority and records acceptance criteria, one or two falsification examples, and a fallback. If the choice changes product authority or accepts material risk, enter `NEEDS_USER` with bounded options and a recommendation.
4. Restore write authority only to the current non-quarantined `WRITER_GENERATION`, then issue one implementation round and freeze the new candidate.
5. If no blind review has completed in this run, use a fresh `BLIND_REVIEW`. Otherwise use narrow closure, except that a decision which invalidated review independence or crossed a listed boundary consumes the one permitted additional fresh broad review.
6. If the same invariant fails again, use the frozen fallback or enter `NEEDS_USER`; do not repeat the council.

If the canonical Reviewer joined architecture work before blind review, use a fresh blind Reviewer after implementation. If the halt arose from a completed blind review, the same Reviewer may perform closure unless the permitted broad-review trigger applies. If that review is `INCOMPLETE`, follow the replacement rule above and perform a fresh full `BLIND_REVIEW`.

## Writer replacement and recovery

Before replacing a Developer, instruct it to stop and confirm it is idle or complete, then archive it. Prefer a fresh Developer after a true architecture pivot only when multiple fix rounds or context compaction create material anchoring risk; do not replace it for ordinary fixes. If safe stop cannot be confirmed, mark its generation `QUARANTINED`, increment `WRITER_GENERATION`, start the replacement from the last accepted SHA in a new worktree/branch, and reject later outputs or refs from the quarantined generation.

On resume, read the highest complete card and latest dispatch record, then inspect the named task before sending anything. If `TASK=pending`, search existing tasks for the same `RUN/ACTION`. If the action exists, wait for or accept its result; dispatch it once only when absent. Verify Git/worktree and PR/CI identities, mark stale evidence, and execute one next action.

A replacement Orchestrator starts read-only and may become active only after the prior Orchestrator is confirmed idle or complete. Otherwise enter `NEEDS_RECOVERY` and ask the user to stop the previous task. Never allow two active Orchestrators for one run.

V1.1 does not promise unattended work while Codex is closed, exactly-once delivery after a crash, or parallel orchestration runs.

## Approval preflight

At run start, the Orchestrator lists likely approval-sensitive operations in each work order; this grants nothing inside another task. Before substantive work, each Developer, Reviewer, or adviser identifies its exact commands and requests its own scoped approvals. Group safe commands where practical and move sandbox-sensitive matrices to hosted CI only when that still satisfies the target. Never broaden authority to avoid approval.

## Thinking effort

- The Orchestrator must run at `Ultra` for the entire run. Verify this before activation or resume; if Ultra is unavailable or cannot be verified, enter `NEEDS_USER` before dispatch or state mutation. Do not downgrade the Orchestrator.
- This permanent Ultra setting is the user's explicit quality/latency choice. Keep routing, waits, status updates, and identity checks mechanical and batched; do not add analysis or commentary merely because Ultra is available.
- Use only `Ultra` and `xhigh` (`Very High` / `Очень высокий`) for workers; never dispatch below `xhigh`.
- Use `xhigh` for ordinary development and narrow closure.
- Use `Ultra` for blind review and for worker tasks dominated by concurrency, security, privacy, trust-boundary, data-loss, or fail-closed reasoning. Use Ultra for closure when it covers a P0/P1 in one of those areas.
- Never change the selected model without explicit user authority.

## PR identity and final report

Track `LOCAL`, `REVIEW`, `HOSTED_CI`, `ARTIFACT`, `DESKTOP`, `RELEASE`, and `PUBLICATION` separately, but only when required. Bind local/review evidence to head `H`, tree `T`, and review base `B0`; record PR head `H`, current base `B`, and the CI-tested synthetic merge `M` for `H + B`.

A base change from review base `B0` to current base `B` requires a bounded human review of the effect of `B0..B`, plus a new synthetic merge and CI for `H + B`. The targeted base-drift review determines the needed human re-review scope but never replaces current CI. After every push, verify that the remote feature ref and PR head resolve to the exact pushed `H`; otherwise enter `NEEDS_RECOVERY`. Declare `READY_TO_MERGE_PR` only as a timestamped snapshot when marking ready is authorized, the PR is no longer draft, its head equals reviewed `H`, its base equals recorded `B`, required checks pass for current `M`, it is mergeable, and repository/DCO policy passes. Without mark-ready authority, enter `NEEDS_USER` rather than claiming readiness. Refresh immediately before merge. Treat a squash-merged main commit as a new identity; PR artifacts are not release artifacts.

Report only the final state, exact head/tree/base, review verdict, open or accepted risks, required gate identities, PR/CI identity when applicable, and one next action. Never collapse local closure, CI, artifact, Desktop, release, and publication into an unqualified `DONE`.
