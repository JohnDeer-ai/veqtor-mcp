<!-- SPDX-License-Identifier: Apache-2.0 -->

# Release acceptance contract — v0.1 Alpha

This contract defines the finite promotion boundary for Veqtor v0.1. A release
candidate is accepted only when every invariant below passes against one exact
commit. A review finding reopens the release only when it violates an invariant,
contradicts a public claim, or demonstrates concrete privacy/reliability harm
inside the threat model. Other hardening belongs in the post-v0.1 backlog.

## Threat model

The v0.1 release gates protect against:

- accidental inclusion of untracked, ignored, local or private files;
- private paths or configured private markers in public sources or artifacts;
- build-backend drift, malformed archives and ambiguous container structure;
- oversized, sparse or otherwise resource-amplifying archive members;
- interrupted or repeated GitHub release promotion;
- replacement of a published tag or release asset;
- parseable but unsupported OOXML returning an uncontrolled exception.

The v0.1 gates do not claim to protect against a malicious maintainer who can
change code, this contract and release approvals together; a compromised GitHub
or hosted runner; or cryptographic provenance beyond GitHub's immutable-release
attestation. Those require a separate signing and trusted-builder design.

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
- `SHA256SUMS.txt` contains only flat basenames and validates after the three
  release assets are copied into one clean directory.

### I5 — exact, recoverable promotion

- First promotion requires caller SHA, candidate SHA and `main` tip equality.
- Tag creation is create-only and accepts only an exact lightweight tag.
- Every publication attempt requires the full required pre-publication gate set
  to have completed successfully for the same run, attempt and candidate SHA.
  The proof is structural: publication consumes the attempt-scoped artifact
  produced by that same completed gate graph.
- Recovery may use a later attempt of the original workflow run, or a separately
  approved dispatch while caller SHA, candidate SHA and `main` still identify
  the same exact commit.
- Any rerun mode is acceptable only when it reconstructs that full current-
  attempt gate set. **Re-run all jobs** does so predictably; a selective rerun
  of the root `guard` also qualifies when GitHub reruns its complete dependent
  graph. An incomplete rerun has a missing current-attempt job proof or artifact
  and therefore fails closed before publication.
- After `main` advances, only a later attempt of the original workflow run may
  recover, and only when the exact lightweight tag still names a candidate that
  remains an ancestor of `main`.
- The guard accepts recovery only for the exact tag and ancestor relationship
  and never retargets it. Artifact names include both run ID and attempt number,
  so a previous attempt's bits cannot satisfy a later attempt.
- Write-scoped publication consumes the artifacts produced by read-only CI and
  does not generate new release content.

### I6 — immutable Alpha release surface

- The protected `release` environment and immutable releases are configured
  before dispatch.
- The environment provides `RELEASE_ADMIN_READ_TOKEN`, limited to read-only
  Administration access for this repository, so the preflight can verify the
  immutable-release setting without extending the release token's authority.
- Release title, prerelease flag and body equal the versioned Alpha contract.
- Published tag and asset names, sizes and SHA-256 digests equal the approved
  candidate, and the API reports `immutable: true`.
- Versioned changelog sections contain only timeless release contents, without
  a publication status or calendar date. The immutable GitHub Release
  `published_at` timestamp is the authoritative public release date.
- A consumer verifier downloads the public assets without a write token,
  validates the flat checksum manifest and reruns artifact verification.

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

The OOXML mutation ratchet exercises duplicate, moved and oversized revision
ids plus nonconforming run layouts through list, extract, verify, preflight and
apply. A successful apply must create the expected unit in the exact anchored
paragraph; merely returning success or avoiding a raw exception is not enough.

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
- Private dogfood passes against both a used corpus and a clean copy without
  modifying the source corpus.
- The real payment edit is refused in preflight as
  `counter_position_unsupported`; the supported five-edit batch passes
  preflight/apply with zero collateral change and the exact output SHA recorded
  in `scripts/release_contract.py`.

Private and real-corpus evidence stays outside the repository. The reviewer
receives only a path-free JSON packet containing candidate/tree/build ids,
test counts, before/after corpus-tree digests, refusal/status codes, edit counts
and output fingerprints. No filenames, local paths, quotes or document text are
allowed by the packet schema. Validate it against the clean exact candidate:

```bash
uv run --frozen python scripts/check_acceptance_evidence.py \
  --source-root . /secure/external/veqtor-v0.1-acceptance.json
```

The private operator retains raw transcripts and corpus manifests outside git;
the public review records only the validator PASS plus the I1-I8 table.

## Promotion order

```text
test implementation tip
→ create and independently review public squash
→ merge public squash
→ make repository public
→ configure protected release environment
→ enable Immutable Releases and tag policy
→ dispatch exact-SHA workflow
→ run consumer verifier
→ install that release artifact for the demo
```

Once one exact-SHA review passes this contract, the v0.1 scope freezes. A later
candidate must rerun the whole contract; a nonblocking improvement moves to the
next version rather than silently expanding the Alpha release.
