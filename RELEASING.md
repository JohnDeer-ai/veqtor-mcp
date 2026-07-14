<!-- SPDX-License-Identifier: Apache-2.0 -->

# Release acceptance contract — v0.1 Alpha

This contract defines the finite promotion boundary for Veqtor v0.1. A release
candidate is accepted only when every invariant below passes against one exact
commit. A review finding reopens the release only when it violates an invariant,
contradicts a public claim, or demonstrates concrete privacy/reliability harm
inside the threat model. The validator scripts and workflows are the executable
source of truth when this document and implementation differ.

## Threat model

The v0.1 release gates protect against:

- accidental inclusion of untracked, ignored, local or private files;
- private paths or configured private markers in public sources or artifacts;
- build-backend drift, malformed archives and ambiguous container structure;
- oversized, sparse or otherwise resource-amplifying archive members;
- DOCX/ZIP expansion, member and edit-batch resource amplification;
- interrupted or repeated cross-registry promotion;
- PyPI publication before a durable exact-tag reservation, from an untrusted
  workflow, or with bytes that differ from the approved CI artifacts;
- publication of an immutable GitHub Release before the public PyPI files and
  provenance have been verified;
- replacement of a reserved tag or published release asset;
- parseable but unsupported OOXML returning an uncontrolled exception.

The v0.1 gates do not claim to protect against a malicious maintainer who can
change code, this contract and release approvals together; a compromised GitHub
or hosted runner; or cryptographic provenance beyond GitHub's immutable-release
guarantee and PyPI Trusted Publisher attestations. Those require a separate
signing and trusted-builder design.

## Invariants

### I1 — exact public history

- The candidate is one DCO-signed squash commit over `origin/main`.
- Its tree equals the fully tested implementation tip.
- Implementation-only objects and private markers are absent from a fresh
  single-branch clone.
- `git fsck --full --strict` and Gitleaks pass in that clone.

### I2 — closed artifact identity

- The primary proof is an independent clean rebuild of the exact commit: the
  downloaded build-job wheel and sdist must be byte-identical to that rebuild.
- Wheel and sdist member sets equal the allowlists in
  `scripts/release_contract.py`; extra and missing members both fail.
- Every source-derived member is byte-identical to its approved git blob.
- Complete raw Core Metadata bytes (headers, separator and README body), wheel
  metadata and `RECORD` equal their approved source contracts. Parser-hidden
  preambles, continuations, malformed headers, suffixes and unknown headers
  therefore fail without relying on email-parser interpretation.
- Adding arbitrary files under `src/` cannot add them to either artifact.

Verification:

```bash
uv build --clear
uv run --frozen python scripts/check_reproducible_build.py \
  --source-root . --approved-dir dist
uv run --frozen python scripts/check_release_artifacts.py \
  --source-root . --commit HEAD dist/*.whl dist/*.tar.gz
```

### I3 — bounded, unambiguous containers

- ZIP local headers, central headers and EOCD are checked as one finite layout:
  signatures, versions, flags, methods, timestamps, CRCs, sizes, names, offsets,
  modes and attributes must agree with the release constants; extras, comments,
  prefixes, gaps, encryption, links, duplicate names and trailing bytes fail.
- Gzip is exactly one stream with the release mtime/XFL/OS header, no optional
  fields, a matching CRC/size trailer and no concatenated or trailing bytes.
- Each TAR header is built from the release contract rather than from parsed
  input: approved name/blob size, mode `0644`, uid/gid zero, release mtime,
  regular-file type, empty link/owner/group names and zero device fields.
  Sparse, PAX, GNU, link, special and duplicate members fail; per-member
  alignment padding and terminal padding are zero-filled.
- Compressed size, member count, logical member size and total expanded size
  remain within the release contract.
- Privacy normalization reaches a fixed point within its configured bound.

Independent byte equality is the primary proof that unreviewed container bytes
cannot be promoted. The bounded scanner and adversarial archive tests are
defense-in-depth: malformed inputs must fail before unbounded allocation.

### I4 — reproducible and installable bits

- Build inputs (Python, uv, Hatchling and source epoch) are pinned, and two
  isolated clean builds of the exact tree are byte-identical.
- Twine, current-dependency wheel smoke and minimum-dependency wheel smoke pass.
- Ruff and the exact locked runtime dependency audit pass before build.
- `SHA256SUMS.txt` contains only flat basenames and validates after the three
  release assets are copied into one clean directory.
- One attempt-scoped wheel/sdist pair is byte-identical to the pair consumed by
  GitHub Release publication and PyPI Trusted Publishing.

### I5 — durable exact-tag reservation and recoverable promotion

- First promotion requires caller SHA, candidate SHA and `main` tip equality.
- After the full required pre-publication gate set succeeds for the same run,
  attempt and candidate SHA, the write-scoped `reserve_tag` job creates or
  revalidates one exact lightweight `v<version>` tag. The current-attempt output
  is emitted only after the tag is confirmed to name the approved commit.
- Tag creation is create-only. The protected `v*` ruleset prevents update and
  deletion, so reservation is durable: a later failure reserves that version as
  a recovery anchor and never authorizes deleting or retargeting it.
- PyPI publication structurally depends on the current attempt's reservation.
  The immutable GitHub Release structurally depends on both that reservation
  and successful public PyPI verification.
- Every promotion attempt requires the full current-attempt gate set and the
  attempt-scoped artifacts produced by that completed graph. Recovery may use a
  later attempt of the original workflow run, or a separately approved dispatch
  while caller SHA, candidate SHA and `main` still identify the same exact
  commit.
- Any rerun mode is acceptable only when it reconstructs that full current-
  attempt gate set. **Re-run all jobs** does so predictably; a selective rerun
  of the root `guard` also qualifies when GitHub reruns its complete dependent
  graph. An incomplete rerun has a missing current-attempt job proof or artifact
  and therefore fails closed before reservation or publication.
- After `main` advances, only a later attempt of the original workflow run may
  recover, and only when the exact lightweight tag still names a candidate that
  remains an ancestor of `main`.
- The guard accepts recovery only for the exact tag and ancestor relationship
  and never retargets it. It first inspects the current trusted `main`, then
  detached-checks out the approved candidate before installing or running that
  candidate. Artifact names include both run ID and attempt number, so a
  later attempt must download its own artifacts. Pre-existing public registry
  bytes are accepted only through the explicit equality checks below.
- A PyPI retry may encounter one or both files uploaded by an earlier attempt.
  `skip-existing` is acceptable only because the current consumer verifier
  requires the exact file set, metadata and public bytes to equal the current
  approved artifacts, and separately requires Trusted Publisher provenance for
  the approved repository, workflow and environment. The current verifier uses
  that trust boundary; it does not bind an attestation to a GitHub run ID or
  attempt number.
- Draft recovery enumerates every authenticated release-list page, including
  drafts, and requires at most one release for the exact tag. Creation captures
  the returned release id; asset upload, verification and publication continue
  by that id instead of the published-only tag lookup. Duplicate exact-tag
  drafts fail closed before any release mutation.
- An interrupted draft upload may replace only an expected asset on that exact
  draft. Unexpected asset names, invalid asset ids or ambiguous releases fail
  closed; an already-published immutable release is verified without mutation.
- Every authenticated release API call pins the documented GitHub API version.
  Write-scoped publication consumes artifacts produced by read-only CI and does
  not generate new release content.

### I6 — ordered PyPI and immutable GitHub release surface

- The protected `release` and `pypi` environments, exact PyPI Trusted Publisher,
  protected `v*` ruleset and GitHub Immutable Releases are configured and
  verified before the candidate reaches public `main`.
- The `main` ruleset requires a pull request and the stable `Required CI gate`
  check before merge. That gate succeeds only when the complete test matrix,
  minimum-dependency lane, artifact build and smoke, independent rebuild and
  history secret scan all succeed. Feature branches run this graph through the
  pull-request event only; the direct push event is limited to `main`.
- Ruleset bypass is disabled. While Veqtor has one maintainer, environment
  self-review may remain enabled as an explicit human confirmation rather than
  a second-person approval; it must not be described as independent review.
  Disable self-review when a trusted second release reviewer is available.
- The `release` environment provides `RELEASE_ADMIN_READ_TOKEN`, limited to
  read-only Administration access for this repository, so tag reservation and
  final publication can verify the immutable-release setting without extending
  the release token's authority.
- PyPI trusts only `JohnDeer-ai/veqtor-mcp`, `.github/workflows/release.yml` and
  the protected `pypi` environment. The publish job receives only OIDC
  `id-token: write`; no long-lived PyPI token is stored.
- After exact-tag reservation, PyPI receives the exact attempt-scoped wheel and
  sdist already reproduced by CI. A tokenless consumer verifier downloads both
  public files, requires byte equality with those artifacts, checks their
  Trusted Publisher provenance and runs the version-pinned public `uvx`
  onboarding path.
- Only successful PyPI verification unlocks GitHub Release publication. The
  final job creates or recovers the exact-tag draft, verifies its body and
  assets, publishes it, and requires the API to report `immutable: true`.
- Release title, prerelease flag, body, tag target, asset names, sizes and
  SHA-256 digests equal the approved candidate. A tokenless consumer verifier
  downloads the public GitHub assets, validates the flat checksum manifest and
  reruns artifact verification.
- Versioned changelog sections contain only timeless release contents, without
  a publication status or calendar date. The immutable GitHub Release
  `published_at` timestamp is the authoritative public release date.

### I7 — total DOCX operation boundary

For the supported public Python API and MCP tools, every parseable input produces
either a documented success or a controlled `DocxError`/structured refusal.
Unsupported run layouts never escape as raw Python/lxml exceptions and never
publish an output file. One common MCP boundary covers workspace resolution,
the core operation, provenance projection, journal publication and response
construction. Unexpected internal failures are journaled without their type,
message, path or document content and are replaced by a context-free
`internal_error`; the original exception never crosses the MCP transport.
Expected decision-record filesystem failures use stable codes without absolute
paths.

The DOCX archive ratchet sends forged local/central sizes and CRCs, truncated or
trailing DEFLATE streams, descriptor mismatches, encryption and forbidden
compression methods through list, extract, verify, preflight and apply. It also
proves those production input paths do not fall back to `ZipFile.read` or a
read-mode `ZipFile.open`. The round-folder ratchet proves a single cumulative
actual-output budget across multiple packages, including DEFLATED and STORED
members, uncaptured members and packages rejected after member-output processing
by CRC, XML or required-part checks. It proves container-preflight refusals
before any member-output processing consume zero, while the first attempted
output byte beyond the budget stops filename-ordered work, returns an MCP error
without partial rounds and writes exactly one error decision record. The OOXML
mutation ratchet exercises
duplicate, moved and oversized revision ids, bounded numbering-label fallbacks,
and nonconforming run layouts through the same five paths. A successful apply
must create the expected unit in the exact anchored paragraph; merely returning
success or avoiding a raw exception is not enough.

The finite boundary covers list, extract, verify, preflight, apply, decision-
record export and synthetic-round generation. A path that cannot resolve to a
safe workspace returns a structured path refusal without a journal record,
because there is no trustworthy sidecar location in which to write one.
Synthetic-round generation preflights all four targets, never overwrites any
existing filesystem object, stages same-directory files, publishes no-clobber
and rolls back the complete batch after an expected publication failure.

### I8 — product acceptance

- Public tests pass on Python 3.12, 3.13 and 3.14, including minimum direct
  dependencies.
- Private dogfood passes against both the maintained used corpus and its clean
  copy without modifying either source corpus. Each run must report at least
  four passing private tests; record the observed pass/skip counts and the same
  retained corpus-manifest digest before and after each run.
- The maintained private `payment_preflight` scenario is refused as
  `counter_position_unsupported` with one match. The maintained
  `five_edit_batch` scenario passes preflight and apply for all five edits,
  reports a passing round trip and zero collateral changes, and produces the
  exact output SHA-256 pinned in `scripts/release_contract.py`.
- The installed wheel completes the six-tool synthetic smoke. Its two compact
  exports report access counts 0 then 1, omit the first access event from both
  returned record windows, and keep each current event outside its own
  snapshot.
- A fresh-copy Claude Desktop rehearsal exercises read, verify, preflight,
  apply, re-extract and export against the exact installed candidate. It must
  also explain the difference between the private raw journal and compact
  projection, including both access-event exclusions above.
- Any maintainer-only corpus, transcript and journal evidence stays outside the
  repository. Only the canonical path-free acceptance packet may enter the
  workflow input; it contains digests, counts, stable status codes and runtime
  identity, never filenames, local paths, quotations or document text.

The acceptance packet has one canonical byte representation and is exact-SHA,
tree and build bound. Its executable schema remains in
`scripts/check_acceptance_evidence.py`.

### Construct the v2 acceptance packet

Freeze one clean candidate before collecting evidence. These three values must
come from that checkout and the same `producer_build` value must be copied into
the packet root, `installed_two_export` and `desktop_rehearsal`:

```bash
test -z "$(git status --porcelain --untracked-files=all)"
git rev-parse HEAD
git rev-parse 'HEAD^{tree}'
uv run --frozen python -c \
  'from veqtor_mcp.records import SOURCE_SNAPSHOT_IDENTITY; print(SOURCE_SNAPSHOT_IDENTITY)'
```

Collect every section below against that exact candidate. Do not infer or
pre-fill a passing result: copy observed counts, identities and digests from the
retained evidence.

| Packet section | Required source and accepted value |
| --- | --- |
| `public_matrix` | Required CI lanes for Python 3.12, 3.13, 3.14 and minimum direct dependencies all completed as `passed` for the candidate SHA. |
| `private_dogfood.used` and `.clean` | Run `VEQTOR_PRIVATE_FIXTURE_DIR=... uv run --frozen pytest -m private tests/test_private_dogfood.py` separately for the maintained used corpus and clean copy. Record each pytest pass/skip count and a retained private corpus-manifest SHA-256 before and after; each pair must match. |
| `payment_preflight` | From the maintained private acceptance scenario: `batch_applicable: false`, `refusal_code: "counter_position_unsupported"`, `match_count: 1`. |
| `five_edit_batch` | From the maintained five-edit scenario: applicable preflight, successful apply of five edits, passing round trip, zero collateral changes and the fixed output digest shown below. |
| `installed_two_export` | Copy the JSON fields printed by `scripts/installed_wheel_smoke.py` when run from the installed candidate wheel: access counts 0 then 1, both exclusion booleans `true`, and the installed version/build. |
| `desktop_rehearsal` | Record the exact literals and booleans shown below plus the installed version/build and SHA-256 digests of the retained private transcript and raw journal. |

The following is the complete, type-correct v2 working template. Its repeated
sample digests are deliberately not candidate values and will fail exact-SHA
validation. Replace the candidate SHA/tree/build, all private evidence digests,
and the observed private pass/skip counts. Keep the fixed statuses, booleans,
version and five-edit output digest exactly as shown unless the executable
schema changes in a later release.

<!-- acceptance-v2-template-begin -->
```json
{
  "schema_version": "veqtor_release_acceptance.v2",
  "candidate_sha": "0000000000000000000000000000000000000000",
  "candidate_tree": "1111111111111111111111111111111111111111",
  "producer_build": "source-snapshot-v1-sha256:2222222222222222222222222222222222222222222222222222222222222222",
  "public_matrix": {
    "python_3_12": "passed",
    "python_3_13": "passed",
    "python_3_14": "passed",
    "minimum_direct": "passed"
  },
  "private_dogfood": {
    "used": {
      "passed": 4,
      "skipped": 0,
      "corpus_before_sha256": "3333333333333333333333333333333333333333333333333333333333333333",
      "corpus_after_sha256": "3333333333333333333333333333333333333333333333333333333333333333"
    },
    "clean": {
      "passed": 4,
      "skipped": 0,
      "corpus_before_sha256": "4444444444444444444444444444444444444444444444444444444444444444",
      "corpus_after_sha256": "4444444444444444444444444444444444444444444444444444444444444444"
    }
  },
  "payment_preflight": {
    "batch_applicable": false,
    "refusal_code": "counter_position_unsupported",
    "match_count": 1
  },
  "five_edit_batch": {
    "preflight_applicable": true,
    "apply_status": "ok",
    "applied_count": 5,
    "round_trip_status": "passed",
    "collateral_change_count": 0,
    "output_sha256": "123771a24f4a3f7e3ae6e9e4785c1e5ebd10edb9923ddcec8dcc0d340f886c41"
  },
  "installed_two_export": {
    "first_access_count": 0,
    "second_access_count": 1,
    "first_event_absent_from_windows": true,
    "current_event_outside_own_snapshot": true,
    "runtime_producer_build": "source-snapshot-v1-sha256:2222222222222222222222222222222222222222222222222222222222222222",
    "runtime_version": "0.1.2"
  },
  "desktop_rehearsal": {
    "verdict": "passed",
    "client": "claude_desktop_fresh_copy",
    "fresh_copy": true,
    "event_omitted_from_records": true,
    "current_event_not_in_access_count": true,
    "raw_vs_compact_explained": true,
    "runtime_producer_build": "source-snapshot-v1-sha256:2222222222222222222222222222222222222222222222222222222222222222",
    "runtime_version": "0.1.2",
    "transcript_sha256": "5555555555555555555555555555555555555555555555555555555555555555",
    "raw_journal_sha256": "6666666666666666666666666666666666666666666666666666666666666666"
  }
}
```
<!-- acceptance-v2-template-end -->

Every field is required and exact; v1 packets are rejected. No filenames,
local paths, quotes or document text are allowed by the packet schema. The
packet has one accepted byte representation: UTF-8 JSON produced with sorted
keys, `ensure_ascii=False`, `allow_nan=False`, separators `(",", ":")`, and no
trailing newline or whitespace. After replacing the sample values in a private
working copy of the template, create the canonical compact file with:

```bash
WORKING_PACKET=/secure/external/veqtor-v0.1.2-acceptance.working.json
EVIDENCE_PACKET=/secure/external/veqtor-v0.1.2-acceptance.json
uv run --frozen python - "$WORKING_PACKET" "$EVIDENCE_PACKET" <<'PY'
import json
import sys
from pathlib import Path

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
Path(sys.argv[2]).write_bytes(
    json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
)
PY
```

Only after all required gates have run against the final clean commit, validate
the canonical file against that exact candidate:

```bash
uv run --frozen python scripts/check_acceptance_evidence.py \
  --source-root . /secure/external/veqtor-v0.1.2-acceptance.json
```

The validator rejects every non-canonical representation and prints the SHA-256
of the exact packet bytes. Retain supporting private material outside git.
Before dispatch, capture the same digest from the canonical file:

```bash
EVIDENCE_PACKET=/secure/external/veqtor-v0.1.2-acceptance.json
EVIDENCE_SHA256=$(shasum -a 256 "$EVIDENCE_PACKET" | awk '{print $1}')
```

Dispatch the release with the same path-free packet. After trust, tag and
ancestry checks, the read-only root `guard` detached-checks out the exact
candidate and runs that candidate's validator with its locked dependencies.
This validation completes before reusable CI and any write-scoped publication
job; it is not a boundary that runs before candidate code:

```bash
gh workflow run release.yml \
  -f version=0.1.2 \
  -f commit_sha="$(git rev-parse HEAD)" \
  -f acceptance_evidence="$(<"$EVIDENCE_PACKET")" \
  -f acceptance_evidence_sha256="$EVIDENCE_SHA256"
```

The workflow materializes the string input and verifies this expected digest
before installing candidate dependencies. The candidate validator independently
checks the same digest, canonical bytes, closed schema, exact commit/tree and
runtime source identity before reusable CI begins. No public distribution is
mutated until that CI graph and the current-attempt verifier both pass. The
workflow then reserves the durable exact tag, publishes and verifies PyPI, and
only then publishes the immutable GitHub Release.

## Promotion order

```text
test implementation tip
→ create and independently review public squash
→ require pull requests and `Required CI gate` in the protected `main` ruleset
→ configure protected `release` and `pypi` environments
→ configure the exact pending PyPI Trusted Publisher
→ verify Immutable Releases, tag policy and repository security settings
→ merge public squash
→ dispatch exact-SHA workflow
→ run the full current-attempt gates
→ reserve the protected exact lightweight tag
→ publish and verify PyPI
→ publish and verify the immutable GitHub Release
→ install the exact public PyPI release for the demo
```

If promotion stops after reservation, the protected tag remains the only
permitted recovery anchor. If it stops during or after PyPI publication, a full
rerun must revalidate the same tag and current-attempt artifacts, complete or
verify the exact PyPI file set, and pass the public onboarding smoke before the
GitHub Release can become visible and immutable.

Once one exact-SHA review passes this contract, the v0.1 scope freezes. A later
candidate must rerun the whole contract; a nonblocking improvement moves to the
next version rather than silently expanding the Alpha release.
