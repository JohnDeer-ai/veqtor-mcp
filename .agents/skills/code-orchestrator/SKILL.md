---
name: code-orchestrator
description: Orchestrate one bounded Codex Desktop developer/reviewer run with managed worktree tasks and exact-SHA review. Use only when explicitly invoked; never apply it to ordinary coding or review tasks.
---

# Code Orchestrator V1

## Scope and topology

- Run at most one active orchestration run for this repository.
- Use `TOPOLOGY: APP_TASKS`.
- Create the canonical Developer and Reviewer as separate Codex Desktop tasks in managed worktrees.
- Never use `spawn_agent` to instantiate the canonical Developer or Reviewer.
- Keep the Orchestrator as the only user-facing coordinator and do not let it edit product code.
- Use internal subagents only for bounded read-only leads. The canonical Reviewer must independently validate their evidence and owns the final verdict.

## Start contract

Before dispatching work, record:

- objective, scope, and non-goals;
- target: `DONE_LOCAL` or `READY_TO_MERGE_PR`;
- required gates;
- repository commit policy, including DCO/sign-off requirements;
- authority for tasks, worktrees, local commits, push, draft PR, and marking a PR ready;
- prohibited actions and user-owned decisions.

Default to `DONE_LOCAL` when push or PR authority is absent. Merge, release, publication, force-push, history rewrite, and remote-branch deletion always require separate explicit authority. Escalate changes to public behavior, compatibility, material scope, or significant product, security, privacy, or data-loss risk.

For the standard `READY_TO_MERGE_PR` target, the invocation must expressly authorize creating and archiving Developer/Reviewer tasks and managed worktrees, signed local commits, pushing the accepted feature branch, creating or updating a draft PR, and marking that PR ready after all target gates pass. It must prohibit merge, release, force-push, history rewrite, and remote-branch deletion. Without `mark_ready` authority, use `DONE_LOCAL` or stop at `NEEDS_USER`; do not claim `READY_TO_MERGE_PR`.

Keep the user prompt short. The canonical form is: `$code-orchestrator Bring <objective> to READY_TO_MERGE_PR. You may create/archive Developer and Reviewer tasks and managed worktrees, create signed local commits, push the accepted feature branch, create/update a draft PR, and mark it ready after all gates pass. Do not merge, release, force-push, rewrite history, or delete remote branches.`

## Role authority and ownership

- Orchestrator: create/read/wait/archive tasks; verify and accept exact candidates; push an accepted candidate; create/update a draft PR or mark it ready only when each action is authorized; never edit product code.
- Developer: edit and commit only in its own managed worktree; never push or create/update a PR.
- Reviewer: inspect and run tests in its own managed worktree; never author or stage product changes, create commits, create/update/delete shared refs, or push.
- Permit exactly one active writer for the candidate branch and record `WRITER_GENERATION`.
- Accept worker output only when its `RUN`, `ACTION`, role, and expected identity match the current card.

## Canonical state card

After every transition, publish one complete card in the Orchestrator task. The complete card with the highest `CARD_REV` in the active Orchestrator task is canonical.

```text
CARD_REV: <monotonic integer>
SNAPSHOT_AS_OF: <timestamp>
RUN: <id> | TOPOLOGY: APP_TASKS | TARGET: <target> | STATE: <state>
ACTION: <unique id> | LAST_COMPLETED: <action and identity>
WRITER_GENERATION: <integer>
TASKS: orchestrator=<id> | developer=<id or -> | reviewer=<id or ->
WORKTREES: developer=<path or -> | reviewer=<path or ->
REFS: local_candidate=<ref or -> | remote_pr=<ref or ->
AUTHORITY: tasks=<yes/no> | worktrees=<yes/no> | local_commits=<yes/no> | push=<yes/no> | draft_pr=<yes/no> | mark_ready=<yes/no> | merge=no | release=no
COMMIT_POLICY: <policy and status>
EXECUTION: orchestrator=<model/effort> | developer=<model/effort or -> | reviewer=<model/effort or -> | downgrade=<none or reason>
BASE_SHA: <sha> | CANDIDATE_SHA: <sha or -> | CANDIDATE_TREE: <sha or ->
REVIEW: blind=<status@H/T> | closure=<status@H/T>
PR: url=<url or -> | head=<H or -> | base=<B or ->
CI: status=<status> | tested_merge=<M or -> | run=<url/id or ->
FINDINGS: <open IDs and dispositions only>
HALT: trigger=<trigger or -> | root_class=<class or -> | invariant=<invariant or -> | fallback=<fallback or ->
GATES: <status and bound identity for each target gate>
NEXT: <one action; verify task and evidence before redispatch>
```

`ACTION` detects stale or duplicate results; it does not guarantee exactly-once execution. Keep full finding evidence in worker tasks, not in the card.

## Work orders and results

- Give every worker `RUN`, unique `ACTION`, role, objective, boundaries, authority, and required result fields.
- Give a Developer `EXPECTED_BASE_SHA`, the current `WRITER_GENERATION`, and the repository commit policy; the result commit does not exist yet. Require every new commit to comply when it is created, including a valid `Signed-off-by` trailer when DCO applies.
- Require Developer results to return writer generation, base SHA, commit SHA, tree SHA, commit-policy evidence, checks, deviations, and remaining risks.
- Give a Reviewer the frozen base, `CANDIDATE_SHA`, and `CANDIDATE_TREE`.
- Require Reviewer results to return reviewed head/tree, pre/post identity checks, findings with stable ID, severity, `ROOT_CLASS`, violated invariant, evidence, verification method, and verdict. Keep `ROOT_CLASS` and invariant stable across fix rounds; do not rename them to avoid escalation.
- Treat mismatched or superseded results as stale and never promote them.

## Normal flow and states

Use: `NEW -> DEVELOP -> FREEZE -> BLIND_REVIEW -> TRIAGE`. From `TRIAGE`, pass to `DONE_LOCAL` for a local target or to `PR_CI -> READY_TO_MERGE_PR` for a PR target; route findings through `FIX -> FREEZE -> CLOSURE -> TRIAGE`, and route architectural uncertainty through `HALT_ARCHITECTURE`. If `PR_CI` reveals a candidate defect, record a complete finding with ID, severity, `ROOT_CLASS`, violated invariant, evidence, and verification method, then use `PR_CI -> TRIAGE`. Triage must apply the normal HALT triggers before choosing `FIX` or `HALT_ARCHITECTURE`. After an ordinary fix, use `FIX -> FREEZE -> CLOSURE -> TRIAGE -> PR_CI`; use a fresh broad review instead of closure only when the change meets the existing broad-review triggers. Reserve `BLOCKED_EXTERNAL` for unavailable or missing external evidence, not a demonstrated product defect.

- Active states: `NEW`, `DEVELOP`, `FREEZE`, `BLIND_REVIEW`, `TRIAGE`, `FIX`, `CLOSURE`, `HALT_ARCHITECTURE`, `PR_CI`.
- Paused states: `NEEDS_USER`, `NEEDS_RECOVERY`, `BLOCKED_EXTERNAL`; revoke write authority while paused.
- Completed states: `DONE_LOCAL`, `READY_TO_MERGE_PR`.
- Terminated states: `SUPERSEDED`, `ABORTED`.
- Before starting another run, resume, supersede, or abort any prior active or paused run. Completed and terminated runs do not block a new run.

## Freeze and reviewer identity

Before review, the Orchestrator must verify the expected ancestry, clean Developer worktree, `HEAD == CANDIDATE_SHA`, and `HEAD^{tree} == CANDIDATE_TREE`. It must also verify that every commit in `EXPECTED_BASE_SHA..CANDIDATE_SHA` satisfies the recorded repository commit policy; for this repository, every commit requires a valid DCO `Signed-off-by` trailer. Reject a noncompliant candidate before freeze or review. If correction would require an unauthorized history rewrite, enter `NEEDS_USER`. Freeze only after all checks pass.

The Reviewer must not run `git fetch` or otherwise update shared refs. Before dispatch, the Orchestrator must ensure that the candidate object exists in the repository's shared object database.

The Reviewer must verify that `CANDIDATE_SHA` resolves to a commit, checkout `--detach` that exact SHA in its managed worktree, and verify:

- `HEAD == CANDIDATE_SHA`;
- `HEAD^{tree} == CANDIDATE_TREE`;
- the worktree is materially clean before and after review.

Ignored test caches do not invalidate review. Authored or staged product changes, reviewer commits, changed head/tree, or shared-ref mutations do. If the object is unavailable or identity fails, enter `NEEDS_RECOVERY`; only the Orchestrator may obtain the missing object, using a recovery method chosen after inspecting the actual Git version, configuration, remote policy, and repository state.

## Blind review, fixes, and closure

- Give the fresh blind Reviewer the objective, specification, frozen base/head/tree, scope, and severity policy, but not the Developer's completion narrative.
- Allow one broad discovery pass and require one consolidated finding ledger.
- Return findings to the same Developer for bounded fixes; every accepted fix creates a new candidate identity.
- Use the same Reviewer task for narrow closure when safe. First detach its worktree at the new exact candidate and repeat all identity checks.
- If that worktree cannot be switched safely, create a new closure task with the original finding IDs; do not call it a new blind review.
- Start a fresh broad review only when fixes materially alter architecture or public behavior, cross a new security/trust boundary, substantially expand the diff, or invalidate the original review assumptions.
- Finish only with no open P0/P1, every P2 fixed or explicitly accepted by an authorized party, practical regression coverage for real defects, and all target gates current. Put non-blocking P3 items in backlog.

## Writer replacement and quarantine

Before replacement, instruct the current Developer to stop without further changes and confirm it is idle or complete. Then archive it.

If safe stop cannot be confirmed:

- record the task and all outputs and refs from its generation as `QUARANTINED` without moving those refs;
- increment `WRITER_GENERATION`;
- start the replacement from the last accepted SHA in a new managed worktree and new branch;
- reject all later outputs and refs from the quarantined generation.

Only the Orchestrator may push, and it must push the exact accepted candidate rather than trusting a mutable branch name. After push, verify that the remote branch resolves to the accepted candidate and, once a PR exists, that its head does too; on mismatch enter `NEEDS_RECOVERY`.

## HALT_ARCHITECTURE

Enter `HALT_ARCHITECTURE` during triage when any of these objective triggers applies:

- a P0 or P1 with the same `ROOT_CLASS` and violated invariant recurs after one bounded fix and closure;
- a required guarantee or proof obligation cannot be stated in a falsifiable, testable form;
- a proposed fix creates or depends on a second source of truth for the same invariant;
- a safe fix would change a public/product contract, compatibility promise, trust boundary, security/privacy boundary, or data-loss guarantee beyond recorded authority;
- requirements or acceptance criteria materially conflict, or no safe implementation exists within the current authority.

When a trigger applies, do not start another ordinary `FIX` round. Record the trigger, `ROOT_CLASS`, and invariant in the state card, then treat `HALT_ARCHITECTURE` as a bounded internal phase, not a final answer:

1. Revoke write authority and freeze the current identities.
2. State the violated invariant neutrally and ask Developer and Reviewer the same question independently.
3. Accept at most two options from each, covering source of truth, guarantees, falsification, compatibility, proof obligations, rollback, cost, and fallback.
4. Let the Orchestrator choose only if a safe option stays within existing product authority, then record acceptance criteria plus fallback before code. If every viable option requires new authority, a product-contract choice, or acceptance of material risk, enter `NEEDS_USER` with the bounded options and recommendation.
5. Before implementation, ask both participants for `OK` or one evidenced `BLOCKER`; resolve any evidenced blockers from this single challenge round or enter `NEEDS_USER`.
6. After a decision within authority, reissue write authority only to the current Developer generation, transition to `FIX`, and implement one chosen option. Then freeze the new candidate and use a fresh blind review if discovery has not yet occurred or the architecture work invalidated independence; otherwise perform narrow closure.
7. If the same invariant fails again, use the recorded fallback or ask the user; never run a third experimental patch round.

If a Reviewer joined architecture work before blind review, create a fresh blind Reviewer after implementation. If the halt arose from the blind review, the same Reviewer may perform closure unless the risk surface materially expanded.

## Gates and PR identity

Track `LOCAL`, `REVIEW`, `HOSTED_CI`, `ARTIFACT`, `DESKTOP`, `RELEASE`, and `PUBLICATION` separately as `NOT_REQUIRED`, `NOT_RUN`, `RUNNING`, `PASS`, `FAIL`, or `STALE`. Require only the gates named by the target.

- `DONE_LOCAL` requires current `LOCAL` and `REVIEW` gates.
- `READY_TO_MERGE_PR` requires current `LOCAL`, `REVIEW`, and `HOSTED_CI` gates plus PR mergeability and repository policy satisfaction.
- Require `ARTIFACT`, `DESKTOP`, `RELEASE`, or `PUBLICATION` only when the start contract explicitly names them.
- Bind local and review evidence to candidate head `H`, tree `T`, and review base `B0`.
- For a PR, record current PR head `H`, current base `B`, and CI-tested synthetic merge `M` for `H + B`.
- Any candidate commit change makes prior local, review, and PR CI evidence stale, even if the tree is unchanged.
- A base change requires a new synthetic merge and new CI. Targeted base-drift review controls human re-review scope but never replaces current CI.
- A base-only change may not trigger the current PR workflow. Mark CI `STALE`; never change `H` merely to retrigger CI. If no current run for the new merge starts, enter `BLOCKED_EXTERNAL`, report the missing evidence, and request authority for one specifically named recovery action.
- Declare `READY_TO_MERGE_PR` only as a timestamped snapshot when the PR is no longer draft, PR head equals reviewed `H`, base equals recorded `B`, required checks pass for `M`, the PR is mergeable, repository policy is satisfied, and required DCO/sign-off is satisfied. If marking ready is not authorized, enter `NEEDS_USER` rather than claiming readiness.
- Refresh this snapshot immediately before merge if time passed or head/base/policy changed.
- After squash merge, treat the new main commit as a distinct identity requiring its own push CI. Do not treat PR artifacts as release artifacts without separate exact-ref evidence.

## Recovery and Orchestrator replacement

On resume, read the highest complete `CARD_REV`, inspect the named tasks, verify Git/worktrees and PR/CI identities, search for the current `ACTION` result, mark stale evidence, and execute exactly one next action. Never redispatch `NEXT` before these checks.

A replacement Orchestrator may open in read-only recovery mode, but it must not become active, dispatch work, or mutate run state until the previous Orchestrator is confirmed idle or completed. Use a fork containing completed history or give the replacement the previous task ID and require it to read the latest complete card. Its first canonical card must reproduce the recovered state and increment the prior `CARD_REV`. Archiving alone is not proof that an active turn stopped. If stop cannot be confirmed, report `NEEDS_RECOVERY` without dispatching and ask the user to stop the previous task; never allow two active Orchestrators for one run.

V1 does not promise unattended work while Codex is closed, exactly-once delivery after a crash, or parallel orchestration runs.

## Thinking and final report

- Before activating or resuming a run, verify that the current Orchestrator turn is running at `xhigh` or `Ultra`. Perform this preflight before creating or mutating run state or dispatching workers. If effort is below `xhigh` or cannot be verified, do not activate or resume; report `NEEDS_USER` and ask the user to continue the Orchestrator task at `xhigh` or `Ultra`.
- Use only `Ultra` and `xhigh` (`Very High` / `Очень высокий`) reasoning effort. Never dispatch work below `xhigh`.
- Use `xhigh` for routing, status, identity checks, normal development, and narrow closure.
- Use `Ultra` for blind review, `HALT_ARCHITECTURE`, concurrency, security, trust-boundary, data-loss, and fail-closed reasoning.
- Before an Ultra-required Orchestrator phase, verify that the current turn itself is running at `Ultra`. If the selected model supports `Ultra` but the current turn is only `xhigh`, stop before that phase and ask the user to resume at `Ultra`.
- If `Ultra` is unavailable for the selected model, use `xhigh` and record the permitted downgrade and reason in the work order, result, and state card. If the model supports neither `Ultra` nor `xhigh`, enter `NEEDS_USER` rather than using a lower effort.
- Never change the selected model without explicit user authority.

Report the final run state, exact candidate head/tree, review base and verdict, open or accepted risks, each target gate and evidence identity, PR URL/head/base/merge CI identity when applicable, and one next action. Never collapse local closure, PR CI, artifact, Desktop, release, and publication into a single unqualified `DONE`.
