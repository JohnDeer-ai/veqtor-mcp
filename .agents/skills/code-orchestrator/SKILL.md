---
name: code-orchestrator
description: Run lean Codex Desktop developer/reviewer workflow with managed worktrees and exact-SHA review. Explicit activation only.
---

# Code Orchestrator V1.3.3

## Purpose and authority

Explicit `code-orchestrator` invocation is required:

```text
implement -> review -> forward findings -> fix -> closure -> final gates
```

Orchestrator dispatches; it neither writes product code nor substitutes for
Reviewer. Record objective, scope/non-goals, immutable starting commit SHA
`BASE`, target (`DONE_LOCAL` or `READY_TO_MERGE_PR`), required gates, and
authority; `B` means the current PR-base SHA and may drift. Allow one
orchestrated writer per repository.

Developer and Reviewer are top-level Codex Desktop tasks in separate managed
worktrees of the local repository.

- Developer alone edits and DCO-commits its assigned ref; no push, PR
  management, other-ref/worktree changes, or history rewrite. Only a separately
  authorized non-rewriting base-sync merge on that ref is allowed; never merge
  the PR.
- Reviewer inspects and tests an exact detached candidate; no authored changes,
  stage, commit, fetch, push, or shared-ref movement. Its private detached
  `HEAD` may move to an authorized candidate.
- Orchestrator manages tasks/evidence and, when authorized, pushes H and manages
  Draft/Ready state; it creates no product commits.

Merge/release/publication, force-push, history rewrite, remote deletion, scope
expansion, and material risk acceptance need separate user authority.

Use `Ultra` for Orchestrator and broad review, `xhigh` for Developer and ordinary
closure, and `Ultra` closure for high-risk findings. Never silently change
model or effort.

## Roles and one-time preflight

Before work, verify Git root/origin, `BASE`, and no other orchestrated
writer. Create workers from that local project, never a snapshot or unrelated
directory.

Each first order reports observed root, origin, common-dir, `HEAD`, and tracked
and untracked cleanliness. Developer confirms its authorized input SHA and
non-interactive execution. Mismatch stops before changes and permits one
correctly targeted replacement. Repeat only after replacement, recovery, or
environment change.

Each Developer's first order names its assigned ref and authorized input SHA
(`BASE` initially; the latest independently frozen fix base for a replacement)
and grants one-time bootstrap. Developer may proceed only when its current
worktree has no tracked or untracked changes and owns the assigned ref at that
SHA. If initially detached at that SHA and the ref is absent and unowned, it must
create and switch to the ref, then reverify the ref, `HEAD`, and cleanliness
before tests, installs, or edits. Otherwise stop. A commentary update is not an
authorization barrier.

Use repository commands with locked dependencies; never fall back to system
Python, an ambient environment, or unlocked installs. Include the project-test
command if repository instructions omit it.

Every worker order begins:

```text
This is a role task. Execute this order directly. Do not invoke
code-orchestrator, inspect orchestration history, or create top-level tasks.
```

Orders contain only role, input SHA, scope, authority, checks, and return; never
shorten verbatim Findings.

Recovery note:

```text
SCOPE | BASE | H/T | DEVELOPER | REVIEWER | OPEN FINDING IDs | PR/CI | NEXT
```

Update only at candidate, verdict, blocker, or completion; it is not a state
machine.

## Core loop

1. Developer implements scope, runs targeted tests/fast lint, DCO-commits, and
   returns commit `H`, tree `T`, changed files, checks, and risks. Defer full,
   package, artifact, and Desktop gates unless policy requires them earlier.
   If scope is already satisfied at `BASE`, return unchanged `H/T` with
   `ALREADY_SATISFIED` evidence and no empty commit. A fresh broad Reviewer then
   validates the current H tree against the full scope, not the empty
   `BASE..H` diff.

2. Orchestrator freezes `H/T`: verify HEAD/tree, tracked cleanliness, ancestry,
   assigned writer ref, DCO for Developer-created commits and any policy-covered
   authorized base-sync merge, excluding upstream commits already present in
   authorized `B`, and that extra outputs are not test/build inputs.

3. A fresh Reviewer checks exact `H/T` detached against `BASE` and full scope.
   Give it objective, specification, named scope, and acceptance conditions, but
   no Developer narrative or expected solution. It returns `PASS` or numbered
   P0-P2 findings.

4. Orchestrator forwards the complete Reviewer-authored Findings block verbatim
   to Developer, adding only identity and authority—never summarizing, merging,
   renaming, reconstructing, or omitting content.

5. Developer reproduces/resolves findings, adds practical regressions, and runs
   affected checks. With tracked changes it DCO-commits and returns new H/T;
   otherwise it returns unchanged H/T with `ALREADY_SATISFIED` evidence. It
   answers every ID.

6. The same Reviewer checks resulting H detached: every open ID, fix delta,
   affected paths, adjacent failures, regressions, and new relevant P0-P2. It returns
   per-ID evidence and `PASS` or remaining/new findings.

Before and after every Reviewer verdict, verify H/T and tracked cleanliness;
unexpected change requires recovery and cannot support PASS.

7. Repeat steps 4-6 until Reviewer reports `PASS` with no open P0-P2. Reuse the
   same Developer and Reviewer and keep both idle rather than archived between
   rounds.

Developer normally runs affected checks; Reviewer independently selects
targeted or adversarial checks. Do not repeat a passing full suite on unchanged
`H` and unchanged inputs solely to duplicate another role's evidence. Reviewer
may run it when repository policy, scope, or failure investigation requires.

If Reviewer stops discovery early for a P0, discovery remains incomplete. After
the fix it must finish the entire original broad scope on new H; narrow closure
alone cannot support PASS.

There is no numerical fix budget. Continue while findings close or narrow. If a
root problem returns unchanged or fixes oscillate, require one root-cause
replan; ask the user only for no progress, conflicting requirements, unsafe or
scope/product-changing resolution, material risk, or missing authority. A clear
internal fix needs no architecture council.

## Findings and review integrity

Reviewer owns the Findings block. Each finding contains:

```text
ID and P0/P1/P2 severity
violated guarantee
reproduction or independently observed evidence
falsifiable closure check
```

Give separate obligations stable sub-IDs. Developer answers every ID as
`FIXED`, `ALREADY_SATISFIED`, or `BLOCKED`; Reviewer closes or keeps each open
with current-H evidence. Missing IDs or `docs checked` cannot support PASS. Text
closure proves approved wording present and stale wording absent. Every
user-named surface receives current-H coverage evidence.

If corrected or pre-existing conditions share a helper, state, owner, source of
truth, or path, closure checks one combined counterexample and unaffected
control, or proves coexistence impossible. Isolated PASS is insufficient.

A new closure finding gets a new ID. CI findings and non-qualifying external
findings are raw evidence for the canonical Reviewer to validate and number
before Developer changes code.

Credible evidence disproving PASS reopens only the affected finding or coverage
claim, invalidates Ready, and returns an authorized PR to Draft; unaffected
broad evidence remains valid. The original Reviewer becomes non-canonical only
for that issue; one fresh closure-only Reviewer owns it. A broader failure, or a
fix materially changing source of truth, linearization/commit point,
persistence/rollback, trust boundary, or public contract, requires a fresh
`Ultra` affected-surface review. Diff size or severity alone does not.

A user-named external broad review qualifies with exact H/T, clean read-only
work, no Developer narrative, adequate scope, and `Ultra`. Its `PASS` replaces
discovery. Its findings replace discovery only with a complete numbered
Findings block and that task bound as closure owner; otherwise they remain raw
evidence for the canonical Reviewer. Do not duplicate qualifying broad review.

## Final gates and failures

After PASS, run on H only target/policy-required gates. Ensure one current-H
result for each required local full-suite command; run it if absent or reuse it
when inputs are unchanged. Hosted CI remains separate evidence. Changed inputs
invalidate an already-required gate's evidence; they never activate package,
artifact, or Desktop gates. Parallelize independent gates; Reviewer skips
package/artifact ceremony.
Orchestrator may run non-authoring gates directly when it can preserve the
frozen candidate; otherwise give Developer a gate-only order that forbids edits,
staging, commits, checkout, and ref movement. Verify H/T and tracked cleanliness
before/after; unexpected change requires recovery, not cleanup or PASS.

Reuse evidence with unchanged inputs. A test-only fix invalidates affected tests,
closure, and CI, not unchanged artifacts. Changed package inputs invalidate
artifacts; changed digest/runtime invalidates Desktop. Run Desktop last.

For a PR target, push only accepted H, keep Draft through review, and bind hosted
CI to feature H, base B, and synthetic M. Any H, B, or M change invalidates that
evidence; relevance determines only base integration and affected review.
Base drift requires integration only when repository policy requires it or the
drift overlaps relevant code, tests, packaging, or runtime assumptions; the
Developer performs any authorized non-rewriting DCO merge, followed by affected
review. A `BEHIND` label alone does not prove semantic overlap.

Route gate failures simply:

- An assertion, repository/product/test-path failure, or ambiguous timeout is
  raw Reviewer evidence. Reviewer closes it as non-semantic or creates/reopens
  a numbered finding, then the normal loop resumes.
- A deterministic formatting, lock, or diff-policy failure goes verbatim to
  Developer; any resulting tracked change gets affected closure and gates.
- A proven local runtime-startup, tool, or environment failure outside
  repository/product/test execution requires recovery, not a product finding.
- Retry the same failed hosted job once only when logs prove a runner, network,
  hosted-service, or tool-startup outage on unchanged H+B/M. A green rerun never
  erases an assertion or product/test-path failure. Repeated external failure is
  reported as an external blocker.

A red required gate must pass through retry/recovery or receive an
explicit policy-authorized waiver. Otherwise report the blocker and do not
complete.

Do not wait for CI on a rejected old H. Preserve any available failure logs and
route semantic evidence before final acceptance; results from an old H cannot
satisfy gates for the new candidate.

## Recovery, communication, and completion

On resume, read the working note, worker tasks, Git/worktrees, and live PR/CI
before sending anything. If an existing order or result is visible, do not
redispatch it. Unknown worker activity or identity mismatch requires recovery or
the user's help, not a guessed continuation. A replacement Orchestrator starts
only after the prior one is confirmed idle or completed.

Replace Developer only after the old writer stops; start from the latest
independently frozen SHA authorized as the current fix base, even if review
rejected it, and reject late output. If canonical Reviewer is unavailable, one
fresh replacement inherits unfinished broad scope, every Findings block
verbatim, and all unnumbered gate/CI or non-qualifying external evidence. It
validates raw evidence, authors new IDs when warranted, and owns closure. With
completed discovery it is closure-only; otherwise it finishes inherited broad
scope. Archive workers only after completion.

A normal run sends four substantive user updates: start/resume, meaningful
candidate, consolidated review verdict, and gates/final. Add one only for a
Desktop handoff, blocker, recovery, or required user decision. Do not report
percentages, task binding, polling, or unchanged waits.

Completion requires latest exact H/T, no open P0-P2, Reviewer PASS, and every
required current gate passed or explicitly policy-waived. `DONE_LOCAL` does not
imply CI, artifact, Desktop, merge, release, or publication.
`READY_TO_MERGE_PR` additionally requires the
current PR head H and base B, required checks on current synthetic M,
mergeability, resolved required review threads and repository/DCO policy, and
an actual non-Draft PR. It is a timestamped snapshot and grants no merge
authority.

Initial `ALREADY_SATISFIED` removes the need for a new commit, not the recorded
target. `DONE_LOCAL` retains its applicable local gates. For
`READY_TO_MERGE_PR`, apply the normal target gates to H against current B. If H
has no authorized PR-effective delta against B, report a verified no-op without
`READY_TO_MERGE_PR`. Otherwise continue the ordinary PR flow.

Final report gives task IDs, base/H/T, verdicts, risks, exact gate/PR identities,
and next action; keep local, CI, artifact, Desktop, merge, release, and
publication claims separate.
