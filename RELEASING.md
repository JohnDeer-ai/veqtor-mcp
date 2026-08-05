<!-- SPDX-License-Identifier: Apache-2.0 -->

# Release acceptance contract — Alpha

This contract defines the finite promotion boundary for Veqtor Alpha. A release
candidate is accepted only when every invariant below passes against one exact
commit. A review finding reopens the release only when it violates an invariant,
contradicts a public claim, or demonstrates concrete privacy/reliability harm
inside the threat model. The validator scripts and workflows are the executable
source of truth when this document and implementation differ.

## Threat model

The Alpha release gates protect against:

- accidental inclusion of untracked, ignored, local or private files in the
  Python distributions or Desktop extension;
- private paths or configured private markers in public sources or artifacts;
- build-backend drift, malformed archives and ambiguous container structure;
- oversized, sparse or otherwise resource-amplifying archive members;
- DOCX/ZIP expansion, member and edit-batch resource amplification;
- interrupted or repeated cross-registry promotion;
- PyPI publication before a durable exact-tag reservation, from an untrusted
  workflow, or with bytes that differ from the approved CI artifacts;
- publication of an immutable GitHub Release before the public PyPI files and
  provenance have been verified;
- replacement of a reserved tag or published wheel, sdist or MCPB asset;
- parseable but unsupported OOXML returning an uncontrolled exception.

The Alpha gates do not claim to protect against a malicious maintainer who can
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
- The MCPB member set equals its separate allowlist. Reviewed source members
  equal the exact candidate git blobs, the four DOCX members equal the
  deterministic synthetic generator, and the manifest, locked UV project,
  required author configuration, nine-tool list and macOS-only compatibility
  equal the extension contract.
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
uv run --frozen python scripts/build_mcpb.py \
  --source-root . --out-dir dist --stage-dir /tmp/veqtor-mcpb-stage
uv run --frozen python scripts/check_mcpb_artifact.py \
  --source-root . --commit HEAD dist/*.mcpb
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

- Build inputs (Python, Node, uv, Hatchling, MCPB CLI and source epoch) are
  pinned. Two isolated clean builds of both the Python distributions and the
  deterministic MCPB are byte-identical.
- Twine, current-dependency wheel smoke and minimum-dependency wheel smoke pass.
- Ruff and the exact locked runtime dependency audit pass before build.
- `SHA256SUMS.txt` contains exactly three flat payload basenames (wheel, sdist
  and macOS MCPB) and validates after those payloads plus the checksum file are
  copied into one clean directory.
- One attempt-scoped wheel/sdist pair is byte-identical to the pair consumed by
  GitHub Release publication and PyPI Trusted Publishing. The attempt-scoped
  MCPB is independently rebuilt and is published only to the GitHub Release.

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
  final job creates or recovers the exact-tag draft, verifies its body and all
  four assets (wheel, sdist, macOS MCPB and checksum file), publishes it, and
  requires the API to report `immutable: true`.
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
  copy without modifying either source corpus. Each run reports at least four
  passing private tests, the observed skip count, and the same retained
  corpus-manifest digest before and after.
- The maintained `payment_preflight` scenario is refused as
  `counter_position_unsupported` with one match. The maintained
  `five_edit_batch` scenario passes preflight and apply for all five edits,
  reports a passing round trip and zero collateral changes, and produces the
  exact output SHA-256 pinned in `scripts/release_contract.py`.
- The installed wheel completes the nine-tool synthetic smoke. Its two compact
  exports report access counts 0 then 1, omit the first access event from both
  returned record windows, and keep each current event outside its own snapshot.
- A Claude Desktop rehearsal runs under a fresh isolated standard macOS user
  profile on the maintainer's Mac. This is not a claim that a separate clean
  physical Mac was used. The user profile has no pre-existing Veqtor state,
  repository checkout or manual server configuration, and the rehearsal does
  not use a developer runtime.
- The exact macOS MCPB is downloaded from the successful `main` CI artifact for
  the accepted commit and runs through Claude Desktop's host-managed UV runtime.
  Acceptance confirms that the extension is enabled and connected, exposes and
  calls exactly nine tools, completes the English bundled prompt, and exercises
  paragraph history, rejected-pending `verify_quote` v2, compact privacy,
  client request abandonment, the MCP cancellation notification, post-abandonment
  session recovery and forced transport-owner process teardown. This does not
  claim that synchronous server work stopped or that an abandoned call produced
  no local side effect.
- The write workflow uses a fresh writable copy of the four bundled DOCX files
  outside the immutable extension. It proves the source hash is unchanged and
  that apply, `list_rounds` and re-extraction agree on the output hash.
- The same fresh user profile performs a real immutable-extension lifecycle:
  public v0.3.0 (eight tools) → candidate v0.4.0 (nine) → public v0.3.0
  (eight) → the same candidate v0.4.0 (nine), with checksum and runtime-version
  checks at every transition. Rollback covers only extension runtime and tool
  surface; the packet must not claim that v0.3 can read or downgrade v0.4
  journal records.
- Any maintainer-only corpus, transcript and journal evidence stays outside the
  repository. Only the canonical path-free acceptance packet may enter the
  workflow input; it contains digests, counts, stable status codes and runtime
  identity, never filenames, local paths, quotations or document text.

The acceptance packet has one canonical byte representation and is exact-SHA,
tree, runtime-build and MCPB-byte bound. Its executable schema is
`scripts/check_acceptance_evidence.py`.

### Construct the v6 acceptance packet

Freeze one clean candidate before collecting evidence. These values must come
from that checkout, and the same `producer_build` must appear at the packet
root and in all three runtime sections:

```bash
test -z "$(git status --porcelain --untracked-files=all)"
git rev-parse HEAD
git rev-parse 'HEAD^{tree}'
uv run --frozen python -c \
  'from veqtor_mcp.records import SOURCE_SNAPSHOT_IDENTITY; print(SOURCE_SNAPSHOT_IDENTITY)'
```

The Desktop candidate must come from CI, not from an unrecorded local rebuild.
After that exact commit is on `main` and its required CI jobs are green,
download the Actions artifact named
`veqtor-mcp-dist-<run_id>-<run_attempt>`. Extract it outside the repository,
confirm that it contains the exact four-file release set, and compare:

```bash
shasum -a 256 veqtor-mcp-0.4.0-macos.mcpb
grep ' veqtor-mcp-0.4.0-macos.mcpb$' SHA256SUMS.txt
```

The two digests must match. Install that exact MCPB in the fresh isolated
standard-user profile and copy its digest into
`desktop_extension.artifact_sha256`. Retain the CI run ID and attempt number
with the private evidence. The later release dispatch passes that accepted
digest back into CI and refuses any independently rebuilt MCPB whose bytes
differ.

Collect every section below against that exact candidate. Do not infer or
pre-fill a passing result: copy observed counts, identities and digests from the
retained evidence.

| Packet section | Required source and accepted value |
| --- | --- |
| `public_matrix` | Required CI lanes for Python 3.12, 3.13, 3.14 and minimum direct dependencies all completed as `passed` for the candidate SHA. |
| `private_dogfood.used` and `.clean` | Run `VEQTOR_PRIVATE_FIXTURE_DIR=... uv run --frozen pytest -m private tests/test_private_dogfood.py` separately for the maintained used corpus and clean copy. Record each pytest pass/skip count and a retained private corpus-manifest SHA-256 before and after; each pair must match. |
| `payment_preflight` | The maintained private scenario is refused with `batch_applicable: false`, `refusal_code: "counter_position_unsupported"` and `match_count: 1`. |
| `five_edit_batch` | Applicable preflight, successful apply of five edits, passing round trip, zero collateral changes and the fixed output digest below. |
| `installed_two_export` | Copy the fields printed by `scripts/installed_wheel_smoke.py` from the installed candidate wheel, including the nine-tool modern/legacy stdio result and compact-export counters. |
| `desktop_rehearsal` | Record the fixed client/fresh-profile values, runtime identity and SHA-256 digests of the retained private transcript and raw journal. |
| `desktop_extension` | Record exact CI artifact provenance; fresh isolated-user conditions; client/OS versions; the nine visible and called tools; history, verify-v2 and privacy results; client abandonment/cancellation-notification/session-recovery status; explicit false server-cancellation and side-effect-absence claims; forced transport-owner process teardown; post-apply hashes; private evidence digests; and the real v0.3→v0.4→v0.3→v0.4 lifecycle. |

### Same-Mac isolated-user rehearsal

The accepted v0.4 rehearsal may use the maintainer's existing physical Mac,
but it must run under a newly created standard macOS user. Record this as a
fresh isolated standard-user profile on `maintainer_mac`; never describe it as
a separate clean physical Mac.

1. After the exact candidate commit reaches `main` and its required CI is
   green, create a standard macOS user through System Settings. Do not copy the
   repository, prior Veqtor state, a manual MCP server configuration or a
   developer runtime into that profile. Record the numeric Claude Desktop and
   macOS versions.
2. In that profile, verify and install the immutable public v0.3.0 MCPB using
   its published checksum. Confirm runtime `0.3.0`, exactly eight tools and a
   passing read-only smoke on a fresh v0.3-compatible workspace. Record the
   checked bytes as `initial_artifact_sha256`.
3. Download the exact v0.4.0 MCPB from the successful `main` CI artifact,
   verify it against that artifact's checksum manifest, upgrade in place and
   confirm runtime `0.4.0` plus exactly nine tools. Record the installed bytes
   as `post_upgrade_artifact_sha256`; they must equal
   `desktop_extension.artifact_sha256`.
4. Run the English bundled demo and collect the required paragraph-history,
   `verify_quote` v2, compact-privacy, abandonment/cancellation-notification,
   session-recovery, teardown and writable-copy results. Keep transcripts,
   journals, filenames, paths and document content outside the repository.
5. Roll back to the immutable v0.3.0 MCPB and test it only against a different
   fresh v0.3-compatible workspace. Do not present a v0.4 `.veqtor` journal or
   claim journal downgrade compatibility. Recheck and record the v0.3 bytes as
   `post_rollback_artifact_sha256`.
6. Reinstall the same exact v0.4.0 candidate, confirm runtime `0.4.0` and all
   nine tools again, recheck and record the bytes as
   `post_reinstall_artifact_sha256`, then uninstall and confirm that its tools
   are absent.
7. Build the canonical v6 packet from the observed values and retained digests,
   validate it against the exact candidate, and preserve the private supporting
   evidence outside git.

`desktop_extension.client_version` must be a public numeric
`MAJOR.MINOR.PATCH[.BUILD]` value, and `platform_version` must be
`MAJOR.MINOR[.PATCH]`. Product names, paths, build labels and free-form OS text
are rejected by the packet validator.

The complete, type-correct v6 working template follows. Its sample SHA/tree,
runtime-build and private digests are placeholders; replace them with observed
values. Fixed statuses, booleans, versions, previous-public MCPB identity and
tool inventories are release-contract values.

<!-- acceptance-v6-template-begin -->
```json
{
  "schema_version": "veqtor_release_acceptance.v6",
  "candidate_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "candidate_tree": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "producer_build": "source-snapshot-v1-sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "public_matrix": {
    "python_3_12": "passed",
    "python_3_13": "passed",
    "python_3_14": "passed",
    "minimum_direct": "passed"
  },
  "private_dogfood": {
    "used": {
      "passed": 4,
      "skipped": 1,
      "corpus_before_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      "corpus_after_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    },
    "clean": {
      "passed": 4,
      "skipped": 1,
      "corpus_before_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      "corpus_after_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
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
    "runtime_producer_build": "source-snapshot-v1-sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "runtime_version": "0.4.0"
  },
  "desktop_rehearsal": {
    "verdict": "passed",
    "client": "claude_desktop_fresh_user_profile",
    "fresh_user_profile": true,
    "event_omitted_from_records": true,
    "current_event_not_in_access_count": true,
    "raw_vs_compact_explained": true,
    "runtime_producer_build": "source-snapshot-v1-sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "runtime_version": "0.4.0",
    "transcript_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "raw_journal_sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
  },
  "desktop_extension": {
    "artifact_sha256": "1111111111111111111111111111111111111111111111111111111111111111",
    "artifact_origin": "successful_main_ci_artifact",
    "installation_channel": "direct_download_mcpb",
    "platform": "darwin",
    "client": "claude_desktop_fresh_user_profile",
    "client_version": "1.0.0",
    "platform_version": "15.5",
    "environment": {
      "kind": "fresh_isolated_standard_macos_user_v1",
      "physical_host": "maintainer_mac",
      "clean_physical_mac_claimed": false,
      "fresh_user_profile": true,
      "preexisting_veqtor_user_state_absent": true,
      "repository_checkout_absent": true,
      "manual_server_configuration_absent": true,
      "developer_runtime_used": false
    },
    "host_managed_uv_runtime_confirmed": true,
    "tracked_change_author_confirmed": true,
    "extension_enabled_confirmed": true,
    "server_connected_confirmed": true,
    "english_scenario_completed": true,
    "visible_tools": [
      "list_rounds",
      "extract_redlines",
      "inspect_document",
      "map_rounds",
      "trace_paragraph_history",
      "preflight_edits",
      "apply_edits",
      "verify_quote",
      "export_decision_record"
    ],
    "called_tools": [
      "list_rounds",
      "extract_redlines",
      "inspect_document",
      "map_rounds",
      "trace_paragraph_history",
      "preflight_edits",
      "apply_edits",
      "verify_quote",
      "export_decision_record"
    ],
    "runtime_producer_build": "source-snapshot-v1-sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "runtime_version": "0.4.0",
    "demo_round_count": 4,
    "bundled_demo_prompt_completed": true,
    "inspection_map": {
      "inspect_browse_status": "passed",
      "inspect_record_status": "written",
      "round_map_schema_version": "round_map.v1",
      "round_map_status": "ok",
      "round_map_record_status": "written",
      "scan_complete": true,
      "candidate_document_count": 5,
      "exact_content_equality_count": 4,
      "navigation_candidate_count": 0,
      "recorded_derivation_count": 1,
      "ambiguous_count": 0,
      "exact_unique_count": 4,
      "unresolved_count": 1,
      "derivation_recorded": true,
      "lineage_verified": false,
      "chronology_verified": false,
      "support_profile": "current_only",
      "supporting_record_count": 1,
      "supporting_current_count": 1
    },
    "history_trace": {
      "schema_version": "paragraph_history.v1",
      "status": "ok",
      "record_status": "written",
      "ordering_source": "filename_lexicographic_v1",
      "result_order": "seed_then_descending_position_v1",
      "candidate_document_count": 4,
      "returned_observation_count": 4,
      "selected_paragraph_count": 4,
      "exact_unique_count": 3,
      "ambiguous_count": 0,
      "unresolved_count": 0,
      "rejected_projection_equality_count": 3,
      "next_cursor_absent": true,
      "seed_deletion_change_unit_present": true,
      "seed_deletion_author_literal_is_53": true,
      "change_units_restricted_to_selected_paragraph": true,
      "authorship_verified": false,
      "time_verified": false,
      "selected_relationships_lineage_verified": false,
      "chronology_verified": false,
      "semantic_identity_verified": false
    },
    "verify_quote_v2": {
      "schema_version": "verification_result.v2",
      "verdict": "exact",
      "exact": true,
      "record_status": "written",
      "checked_projection_schema_version": "verified_paragraph_projection.v1",
      "checked_projection_mode": "pending_text_revisions_rejected_v1",
      "checked_projection_status": "complete",
      "match_count": 1,
      "match_side": "paragraph_rejected_pending",
      "diff_count": 0,
      "checked_anchor_matches_history_seed": true,
      "projection_sha256_matches_history": true
    },
    "compact_privacy": {
      "export_record_status": "written",
      "export_payloads": "compact",
      "history_record_type": "paragraph_history.v1",
      "verification_record_type": "verification.v2",
      "history_record_present": true,
      "verification_record_present": true,
      "history_raw_path_text_author_absent": true,
      "history_compact_path_text_author_absent": true,
      "verification_compact_path_text_clause_absent": true,
      "history_snapshot_digests_match_live": true,
      "verification_projection_hashes_match_live": true
    },
    "stdio_lifecycle": {
      "client_request_abandonment_status": "passed",
      "cancellation_notification_status": "passed",
      "post_cancellation_session_recovery_status": "passed",
      "server_work_cancellation_verified": false,
      "cancelled_request_side_effect_absence_verified": false,
      "process_teardown_status": "passed"
    },
    "post_apply_list_rounds_status": "passed",
    "post_apply_round_count": 5,
    "source_sha256_unchanged": true,
    "output_sha256_matches_list_rounds": true,
    "output_sha256_matches_reextract": true,
    "session_transcript_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
    "demo_journal_sha256": "3333333333333333333333333333333333333333333333333333333333333333",
    "lifecycle": {
      "scenario": "v0.3.0_to_v0.4.0_upgrade_rollback_v1",
      "previous_artifact_source": "immutable_github_release_v0.3.0",
      "previous_artifact_version": "0.3.0",
      "initial_artifact_sha256": "43e939a60c7f13d8d31b61f090b1520cab951732395e078cfb590622ece0c596",
      "initial_checksum_status": "passed",
      "previous_install_status": "passed",
      "previous_visible_tools": [
        "list_rounds",
        "extract_redlines",
        "inspect_document",
        "map_rounds",
        "verify_quote",
        "preflight_edits",
        "apply_edits",
        "export_decision_record"
      ],
      "upgrade_status": "passed",
      "post_upgrade_artifact_sha256": "1111111111111111111111111111111111111111111111111111111111111111",
      "post_upgrade_checksum_status": "passed",
      "post_upgrade_runtime_version": "0.4.0",
      "post_upgrade_visible_tools": [
        "list_rounds",
        "extract_redlines",
        "inspect_document",
        "map_rounds",
        "trace_paragraph_history",
        "preflight_edits",
        "apply_edits",
        "verify_quote",
        "export_decision_record"
      ],
      "rollback_status": "passed",
      "post_rollback_artifact_sha256": "43e939a60c7f13d8d31b61f090b1520cab951732395e078cfb590622ece0c596",
      "post_rollback_checksum_status": "passed",
      "post_rollback_runtime_version": "0.3.0",
      "post_rollback_visible_tools": [
        "list_rounds",
        "extract_redlines",
        "inspect_document",
        "map_rounds",
        "verify_quote",
        "preflight_edits",
        "apply_edits",
        "export_decision_record"
      ],
      "post_rollback_smoke_status": "passed",
      "post_rollback_workspace_kind": "fresh_v03_compatible_workspace_v1",
      "rollback_scope": "extension_runtime_and_tool_surface_only",
      "v04_workspace_presented_to_v03": false,
      "v04_journal_downgrade_claimed": false,
      "candidate_reinstall_status": "passed",
      "post_reinstall_artifact_sha256": "1111111111111111111111111111111111111111111111111111111111111111",
      "post_reinstall_checksum_status": "passed",
      "post_reinstall_runtime_version": "0.4.0",
      "post_reinstall_visible_tools": [
        "list_rounds",
        "extract_redlines",
        "inspect_document",
        "map_rounds",
        "trace_paragraph_history",
        "preflight_edits",
        "apply_edits",
        "verify_quote",
        "export_decision_record"
      ],
      "uninstall_status": "passed",
      "post_uninstall_tools_absent": true
    }
  }
}
```
<!-- acceptance-v6-template-end -->

Every field is required and exact; v1 through v5 packets are rejected. No
filenames, local paths, quotes or document text are allowed by the packet
schema. The packet has one accepted byte representation: UTF-8 JSON produced
with sorted keys, `ensure_ascii=False`, `allow_nan=False`, separators
`(",", ":")`, and no trailing newline or whitespace. After replacing the
sample values in a private working copy, create the canonical compact file with:

```bash
WORKING_PACKET=/secure/external/veqtor-v0.4.0-acceptance.working.json
EVIDENCE_PACKET=/secure/external/veqtor-v0.4.0-acceptance.json
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
  --source-root . /secure/external/veqtor-v0.4.0-acceptance.json
```

The validator rejects every non-canonical representation and prints the SHA-256
of the exact packet bytes. Retain supporting private material outside git.
Before dispatch, capture the same digest:

```bash
EVIDENCE_PACKET=/secure/external/veqtor-v0.4.0-acceptance.json
EVIDENCE_SHA256=$(shasum -a 256 "$EVIDENCE_PACKET" | awk '{print $1}')
```

Dispatch the release with the same path-free packet. After trust, tag and
ancestry checks, the read-only root `guard` detached-checks out the exact
candidate and runs that candidate's validator with locked dependencies:

```bash
gh workflow run release.yml \
  -f version=0.4.0 \
  -f commit_sha="$(git rev-parse HEAD)" \
  -f acceptance_evidence="$(<"$EVIDENCE_PACKET")" \
  -f acceptance_evidence_sha256="$EVIDENCE_SHA256"
```

The workflow verifies the packet digest before candidate execution, then checks
canonical bytes, closed schema, exact commit/tree and runtime-source identity.
No public distribution is mutated until the full current-attempt CI graph and
verifier pass. The workflow then reserves the durable exact tag, publishes and
verifies PyPI, and only then publishes the immutable GitHub Release.


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
→ activate the website's v0.4.0 release copy in a separate docs/site change
→ verify the deployed setup page and every public download/install link
```

Public installation copy follows that external state; it never predicts it.
The immutable README and package metadata use a state-neutral version-selection
rule: `0.4.0` is selected only when both public verifiers expose it; otherwise
the explicit fallback is public `0.3.0`. Before those verifiers pass, website
install commands and download links remain pinned to public v0.3.0, and the
Desktop Extension is labelled a v0.4.0 candidate or preview. After both pass,
a separate docs/site change must activate the public v0.4.0 links and release
wording, deploy them, and smoke the live setup page. That required copy
activation does not amend the tag, replace isolated fresh-user acceptance, or
waive any gate above.

If promotion stops after reservation, the protected tag remains the only
permitted recovery anchor. If it stops during or after PyPI publication, a full
rerun must revalidate the same tag and current-attempt artifacts, complete or
verify the exact PyPI file set, and pass the public onboarding smoke before the
GitHub Release can become visible and immutable.

Once one exact-SHA review passes this contract, that version's scope freezes. A later
candidate must rerun the whole contract; a nonblocking improvement moves to the
next version rather than silently expanding the Alpha release.
