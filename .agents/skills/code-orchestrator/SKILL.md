---
name: code-orchestrator
description: Run one lightweight, bounded Codex Desktop developer/reviewer workflow with managed worktrees and exact-SHA evidence. Use only when explicitly invoked; never apply it to ordinary coding or review.
---

# Code Orchestrator V1.3.1

## Purpose and authority

Automate the user's useful manual loop: develop, freeze, review, return one
findings package, close it, and run only gates whose inputs changed. The
Orchestrator is a thin dispatcher. It never writes product code or substitutes
its own inspection for canonical review.

- Allow one active run per resolved Git common-dir. Before activation, inspect
  named role tasks/worktrees for an active or paused `RUN`; ambiguity is
  `NEEDS_RECOVERY`.
- Record objective, scope/non-goals, target (`DONE_LOCAL` or
  `READY_TO_MERGE_PR`), required gates, commit policy, and user authority.
- Autonomous activation requires user-set `AUTONOMOUS_FULL_ACCESS=YES`, exact
  local project/root/origin/host, `danger-full-access`, and
  `approval_policy=never`. Orchestrator must select `projectKind=local` at the
  exact repository path; snapshot/slingshot projects are forbidden. Its own
  mismatch is `NEEDS_USER` before mutation.
- Create Developer and, unless a qualifying external review is adopted,
  Reviewer as top-level Codex Desktop tasks in managed worktrees. Never use
  internal subagents as canonical roles.
- Developer alone may edit, create commit objects, and advance only its assigned
  writer ref through authorized commits or a permitted base merge. It must not
  modify any other shared refs, remotes, config, hooks, or worktrees, push, or
  manage PRs. Reviewer may inspect/test and change only its private `HEAD`,
  index, and files through authorized `git checkout --detach <exact-H>`. It
  must not author/stage changes, commit, fetch, move shared refs, or push.
- Orchestrator may manage tasks/evidence and, when authorized, push exact H and
  manage draft/ready PR state, but creates no commits. Merge,
  release/publication, force-push, history rewrite, remote deletion, and model
  changes require separate explicit authority.

Orchestrator always runs on `Ultra`. Set Developer explicitly to `xhigh` at
creation and every follow-up; never inherit Orchestrator effort. Discovery and
fresh affected-surface review use `Ultra`; ordinary closure uses `xhigh`.
P0/P1 concurrency, security, privacy, trust, data-loss, or fail-closed closure
uses `Ultra`. An adviser is `Ultra` and allowed only for a genuine architecture
choice. If required effort cannot be selected, enter `NEEDS_USER` before dispatch.

## Minimal state and communication

Use one checkpoint:

```text
RUN | STEP | TARGET | PHASE | REVIEW_BASE | PR_BASE | H/T | DEV | REVIEWER | OPEN |
FIX=0/2 | CI_FIX=0/2 | REVIEW | LOCAL | CI=H+B@M |
ARTIFACT=digest/- | DESKTOP=digest/- | NEXT
```

Give every asynchronous dispatch a monotonic `STEP`. Before creating or
following up a top-level task, append one immutable Orchestrator-task record:

```text
RUN/STEP | ROLE | TASK=pending|<known-task-id> | EXPECTED=<input-H/T>
```

Order and result repeat `RUN/STEP`. The visible order proves dispatch; its
matching result plus independently verified identity proves acceptance. For
`TASK=pending`, the created task containing that order is the binding. Do not
write lifecycle records. Before redispatch, search tasks and Git for the same
`RUN/STEP`; ambiguity is `NEEDS_RECOVERY`. Apply this to every top-level role
dispatch. Publish checkpoints only at start, freeze, verdict, pause/recovery,
and final. Wait for events without polling. Never relay percentages, partial
counts, unchanged waits, or narration that only says work continues. A normal
run has three to five substantive updates; use a sparse heartbeat only if the
environment requires one.

## Workers and orders

Keep workers through ordinary fixes and pauses; do not archive them. Developer
handles fixes, while Reviewer checks out each accepted H detached for closure.
Ordinary replacement follows the rules below; external-review adoption and a
mandatory fresh pivot Reviewer follow their own explicit rules.

The first substantive order separates `EXPECTED` from independently observed
identity. Orchestrator verifies launcher-only project/path/host metadata; worker
reports observed Git root, origin, common-dir, HEAD, worktree root, sandbox, and
approval policy. Repeating expected values without observed evidence does not
pass. Before initial mutation Developer verifies `HEAD == B0`; before a fix it
verifies `HEAD == exact frozen H named as the fix base`, even when that H was
rejected as a candidate. Recheck only after resume/recovery, replacement, or
observable environment/identity change.
Recreate a mismatched worker once through the exact local project; recurrence
is `NEEDS_USER`. An Orchestrator profile mismatch is immediately `NEEDS_USER`.

Every worker order begins exactly:

```text
This is a role task. Execute this order directly; do not invoke or read
repository workflow skills, inspect orchestration history, or create top-level
tasks.
```

Every Reviewer order also states:

```text
You may run git checkout --detach <exact H> in your private worktree; changing
only its private HEAD/index/checked-out files is permitted, while shared refs
remain forbidden.
```

Every initial Developer order also states:

```text
Before semantic acceptance run only targeted tests and fast lint. Defer the
full suite and package/artifact/Desktop gates to final gates unless repository
policy explicitly requires earlier execution. A packaging change may get one
targeted package probe.
```

Keep routing envelopes short: initial 60–120 words and fix/closure 40–100.
Canonical packets and user-named scope are excluded from those limits. Include
identity, objective, authority, targeted checks, and return fields. Reviewer
may use bounded read-only helpers for leads, but reproduces material evidence
and owns the verdict.

When discovery finds P0–P2, the Discovery Reviewer—not Orchestrator—authors the
`CANONICAL_FINDING_PACKET`. Set `PACKET_SOURCE=RUN/STEP@H/T`; separately name
each fix/closure input H/T. Each root finding contains a stable ID, P0/P1/P2
severity, violated invariant or guarantee, reproducible steps or independently
observed evidence, and one or more run-unique criterion IDs with falsifiable closure
conditions; suffix independently verifiable surfaces, for example
`DOC-1.a`–`DOC-1.c`. Reviewer emits `PACKET_CRITERIA` as the exact set of every
criterion ID in that packet. Run-level `REQUIRED_CRITERIA` is the immutable exact
union of `PACKET_CRITERIA` from all validated packets; later packets may add IDs
but never delete or rename prior IDs. Orchestrator only validates these fields,
source, ID uniqueness, verdict coverage, and set equality, then routes the
packet verbatim to Developer and closure; it never authors, reconstructs,
paraphrases, merges, renames, or omits packet content.
Both echo exact `PACKET_SOURCE`. Developer returns evidence and exactly one
disposition per criterion: `FIXED`, `ALREADY_SATISFIED`, or `BLOCKED`. Closure
returns `PASS` or `OPEN` with current-H evidence for the same IDs. A
verdict/packet or source mismatch, invalid disposition, or missing, duplicate,
or unknown ID is `INCOMPLETE`.

For ordinary replacement, act only on persistent profile mismatch, lost
worktree, hang, authority breach, or anchoring on rejected architecture. Never
start a second writer until the old one is confirmed idle/stopped. A separate
worktree or branch is not isolation under Full Access. Otherwise enter
`NEEDS_RECOVERY` and ask the user to stop it. Then quarantine the old task/branch,
replace from the last accepted SHA, and reject late output. No pause may leave
mutating Developer work in flight.

## Flow

```text
DEVELOP -> FREEZE -> (DISCOVERY || DRAFT_CI) -> JOIN
-> FIX if needed -> FREEZE
-> ((DISCOVERY if INCOMPLETE else CLOSURE) || CI) -> JOIN
-> BASE_CHECK/SYNC -> (CURRENT_CI || INVALIDATED_FULL_LOCAL)
-> ARTIFACT_IF_NEW_INPUT_IDENTITY
-> DESKTOP if required -> DONE_LOCAL | READY_TO_MERGE_PR
```

1. Developer runs targeted tests/fast lint and returns a DCO commit, H/T,
   changed surface, checks, and risks. Before semantic acceptance, prohibit the
   full suite and final package/artifact ceremony unless policy explicitly
   requires their earlier execution; packaging changes may get a targeted
   package probe.
2. Orchestrator freezes H/T, ancestry, cleanliness, branch, and commit policy.
3. With push/PR authority, push to a draft PR and run hosted CI in parallel
   with discovery. Early CI is platform evidence, not acceptance.
4. At each `JOIN`, wait for terminal review/closure and CI on the same H. Route
   `FIX` only from validated Reviewer-authored packets. A candidate semantic
   P0–P2 first surfaced by CI or external review without a compatible packet is
   evidence only: before `FIX`, the bound canonical Reviewer independently
   validates it at exact H/T and emits a compatible packet; Orchestrator never
   converts source narrative into packet content. `CI_RECOVERY` is allowed only
   when logs prove a runner, network, hosted-service, or tool-startup outage
   outside repository code/tests on exact H+B@M. A repository assertion,
   product/test-path timeout or error, or product-returned failure remains
   semantic evidence even if a rerun passes. For a proven external outage,
   rerun only the failed job once on unchanged H+B@M. Success returns to `JOIN`;
   another external failure is `BLOCKED_EXTERNAL`. Changed H, B, or M invalidates
   recovery and follows normal flow. A confirmed P0 rejects H and may interrupt
   only to contain risk, but requires its packet before a code fix. If it
   interrupts discovery, record
   `DISCOVERY=INCOMPLETE @ H/T`. After the fix, complete the entire original
   broad scope on new H/T; narrow P0 closure cannot replace it. Use the same
   Reviewer unless the fix is a material pivot, in which case the mandatory
   fresh Reviewer below performs the full rerun. Record `DISCOVERY=PASS` only
   after broad scope completion and closure of its findings. Except for P0, do
   not start another fix while review or CI for H runs.
5. After semantic acceptance, resolve required base drift. Run current CI and
   any invalidated full-local evidence in parallel. Build package/artifact only
   after stable inputs and passing required gates, once per stable package-input
   identity; run Desktop last.

For `DONE_LOCAL`, omit remote, CI, artifact, or Desktop gates not required by
the target or repository policy.

## Review, fixes, and architecture

Discovery receives frozen specification and exact `B0..H/T`, but no Developer
narrative, expected problems, or suggested solution. Repeat every user-named
file, surface, invariant, and acceptance condition in `REQUIRED_NAMED_SCOPE`.
Reviewer echoes each in `COVERED_NAMED_SCOPE` with independently observed
current-H evidence and, when applicable, its criterion ID. An ID alone is not
evidence. Missing, duplicate, unknown, or set-mismatched items make it
`INCOMPLETE`; `docs checked` is not coverage. Do not enumerate unnamed
repository/specification prose. Discovery covers the
relevant surface, tries to falsify guarantees, and returns `PASS` or
reproducible P0–P2 using inspection and targeted adversarial checks. It does
not run package/artifact/Desktop ceremony, but applicable full-suite evidence
already produced at exact H/T may be reused.

Before final gates, a user-named external review of current H/T may replace
canonical discovery only if task evidence proves exact `B0..H/T`, clean
before/after state, full frozen-specification/relevant-surface scope, read-only
operation, no Developer narrative, `Ultra`, and a broad verdict. A qualifying
external `PASS` needs no packet. A findings verdict is adoptable only with a
compatible Reviewer-authored packet; otherwise it remains evidence only until
that Reviewer, or one validation/closure-only replacement if unavailable,
independently validates it at exact H/T and emits the packet. Verify its
reproductions; `PASS` has none. Make the prior Reviewer idle/non-canonical,
preserve findings, and bind the Reviewer that authored the adopted packet for
closure without adding another broad review; an external `PASS` needs no
closure. Older-H/T or narrow review is evidence only. If that
Reviewer is confirmed idle/completed but unavailable because it is archived or
its worktree is gone, create one closure-only task with the original finding
packet and current exact H/T; this is not discovery. Use `NEEDS_RECOVERY` only
for ambiguous provenance, unknown old-task activity, or identity mismatch.

Closure is narrow by scope but deep by risk: verify the invariant, complete
affected call path, adjacent failure modes, regressions, and assertion strength.
Text criteria require approved wording present and stale/contradictory wording
absent. A named file expected to change appears in the delta or has independent
`ALREADY_SATISFIED` evidence. Ordinary fixes stay with the canonical Reviewer.
If later evidence disproves `PASS`, reopen only that criterion, invalidate
readiness, and make that Reviewer non-canonical for it. After the fix use one
fresh closure-only Reviewer; repeat discovery only for a material pivot.
If a fix changes source of truth, linearization, persistence/rollback,
trust/authority boundary, or public contract, create exactly one fresh
independent Ultra Reviewer for broad affected-surface review at new H/T. Its
verdict covers related closure and the pivot; do not duplicate that work in the
prior Reviewer. If discovery was incomplete, include its entire unfinished
scope. A second material pivot in the run is `NEEDS_USER`. Pivot review does not
reset `FIX`/`CI_FIX`; severity or diff size alone does not trigger it.

Before concurrency, persistence, security, trust, data-loss, or fail-closed
work, Developer briefly states source of truth, linearization/commit point,
rollback, bound, and falsification examples. Reviewer challenges them normally.
Use `HALT_ARCHITECTURE` only for conflicting requirements, untestable
guarantees, a second source of truth, contract/trust change beyond authority,
material risk acceptance, or no safe solution. A reproducible P1 with an
internal fix is ordinary; consult an adviser only between distinct safe designs.

Allow two semantic rounds: the consolidated package and one replan/follow-up.
Classify by changed surface: any product/package change consumes `FIX`. Allow
two separate proven test/fixture/oracle-only `CI_FIX` rounds that preserve
product/package inputs, guarantees, coverage, limits, and assertion strength;
assertions may change but not weaken. Profile/recovery does not count and a new
root class does not reset budgets. Exhaustion is `NEEDS_USER` with one
recommendation.

## Evidence and gates

Invalidate evidence by its actual input:

- product/package source: affected review, relevant/full local, and CI; artifact
  only for changed package inputs and Desktop only for changed digest/runtime;
- tests/fixtures only: affected closure/tests/CI; retain product review,
  package, artifact, and Desktop after package-input/artifact byte identity;
- docs outside package: only applicable docs/policy gates;
- non-overlapping base drift: retain feature review/local only after relevant
  feature blobs/test inputs are identical; run targeted drift review and
  current synthetic CI; carry artifact by package-input identity and accepted
  digest, and Desktop by digest plus runtime/config identity;
- changed H with identical relevant tree: repeat ancestry/DCO/remote/CI, while
  tree-bound content evidence may carry;
- changed artifact digest or runtime/configuration: Desktop is stale.

Record exact identities: `DISCOVERY PASS|FINDINGS @ B0..H/T`, `CLOSURE PASS
H1..H2 @ H2/T2`, `CI PASS H+B@M`, `ARTIFACT PASS @ inputs/digest`, and
`DESKTOP PASS @ digest/runtime`. Keep immutable `REVIEW_BASE=B0` separate from
current `PR_BASE=B`; never rerun current evidence ceremonially.

Full-local evidence becomes stale only when relevant product/test inputs
change. If policy permits, a narrow docs test plus current hosted full CI is
sufficient. Reuse an artifact cycle while package-input identity and accepted
digest are unchanged.

At `BASE_CHECK`, require feature-base integration when repository policy demands
an up-to-date branch, targeted drift analysis finds semantic overlap, or the
changed base affects package/test/runtime inputs. Assigned Developer performs
an authorized non-rewriting DCO merge and returns H/T; Orchestrator only
fetches/verifies/pushes H. Conflicts, semantic drift, or missing authority return
to affected review or `NEEDS_USER`. If drift is proven non-overlapping and those
inputs are identical, do not merge solely because the PR reports `BEHIND`;
targeted drift review plus current synthetic CI for `H+B@M` is sufficient.
Complete `BASE_CHECK` before Desktop. After push verify remote ref and PR head H.

## Freeze, recovery, and completion

Before review, push, or acceptance, verify ancestry; clean index and tracked
worktree; `HEAD == H`; `HEAD^{tree} == T`; `merge-base(B0,H) == B0`; assigned
writer branch ref `== H`; and DCO for every commit in current `PR_BASE..H`,
including feature merge commits. After base integration also verify
`merge-base(PR_BASE,H) == PR_BASE`. Untracked/ignored files must be absent or
listed and proven not to be test/build inputs. Reviewer may check out exact H
detached in its private worktree, verifies H/T/clean before and after, and never
fetches or moves shared refs.
Missing objects or identity failure is `NEEDS_RECOVERY`; only Orchestrator
performs inspected recovery.

On resume, read the latest checkpoint and immutable dispatch record; inspect
all possible bound tasks and verify Git plus PR/CI. A visible matching order
means do not resend: wait for or read its result. Accept only a result matching
RUN/STEP/role/input identity after independent evidence verification. Redispatch
with the same STEP only when the order is proven absent; ambiguity is
`NEEDS_RECOVERY`. A replacement Orchestrator stays read-only until the prior one
is idle/complete; never allow two active Orchestrators.

Pause as `NEEDS_USER`, `NEEDS_RECOVERY`, or `BLOCKED_EXTERNAL`; terminate as
`SUPERSEDED` or `ABORTED`. Do not start another run until the prior active or
paused run is resumed, superseded, or aborted.

Semantic acceptance requires
`REQUIRED_CRITERIA == PASS_CRITERIA union AUTHORIZED_ACCEPTED_CRITERIA`, no
missing/duplicate/unknown IDs, and empty `OPEN`; before then, `OPEN` lists every
still-open criterion ID. Complete only with no open P0/P1, every P2 fixed or
authorized-accepted, practical
regressions, `DISCOVERY=PASS`, and current target gates. Any reopened P0–P2
invalidates `READY_TO_MERGE_PR`; when authorized, return the PR to draft first.
`READY_TO_MERGE_PR` is a
timestamped snapshot requiring current PR head H/base B, required checks on
current synthetic M, mergeability, resolved review threads, repository/DCO
policy, and `isDraft=false` after Orchestrator actually marks the PR ready. It
grants no merge authority.

Final starts `COMPLETE` for a reached target, `PAUSED` for `NEEDS_USER` or
`NEEDS_RECOVERY`, `BLOCKED` only for `BLOCKED_EXTERNAL`, and `TERMINATED` for
`SUPERSEDED` or `ABORTED`. Report exact review base, H/T, current B/M,
Developer/Reviewer/adviser task IDs, review/closure scope and verdict, risks,
each required gate identity, PR state, and one next action. Never collapse
local, CI, artifact, Desktop, merge, release, and publication into an
unqualified completion claim.
